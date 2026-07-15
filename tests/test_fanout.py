"""Tests for the multi-monitor fan-out copy loop and the deterministic teardown.

Uses in-memory fakes (a BytesIO-ish source + fake pipe writers + fake Popen),
so there is no real yt-dlp/ffplay, no GUI, no network.

    .venv\\Scripts\\python.exe tests\\test_fanout.py
"""

import io
import os
import sys
import time
import queue
import threading
import importlib.util

SRC = os.path.join(os.path.dirname(__file__), '..', 'src')
sys.path.insert(0, SRC)

spec = importlib.util.spec_from_file_location(
    "video_tiler", os.path.join(SRC, "video-tiler.py"))
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)

_failures = []


def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


class FakeStdin:
    """A fake pipe writer. If wedged=True, write() blocks until released, so the
    consumer's queue fills and the reader must retire it (not block forever)."""
    def __init__(self, wedged=False):
        self.buf = bytearray()
        self.closed = False
        self.close_count = 0
        self._wedged = wedged
        self._release = threading.Event()

    def write(self, b):
        if self._wedged:
            self._release.wait(5)
        if self.closed:
            raise ValueError("write to closed file")
        self.buf.extend(b)

    def flush(self):
        if self.closed:
            raise ValueError("flush of closed file")

    def close(self):
        self.close_count += 1
        self.closed = True
        self._release.set()  # unblock any wedged write


class FakeProc:
    # A fake Popen. pid is a very high (nonexistent) number so the real
    # _kill_tree falls through psutil.NoSuchProcess to proc.kill() (a no-op here).
    _next = 2_000_000_000

    def __init__(self, stdin):
        self.stdin = stdin
        self.killed = False
        FakeProc._next += 1
        self.pid = FakeProc._next

    def kill(self):
        self.killed = True

    def poll(self):
        return 0 if self.killed else None


def _make_player():
    class App:
        pass
    return vt.Player(App(), "http://x", 2)


def test_fanout_copies_to_all_and_closes_dead_writer():
    p = _make_player()
    healthy = FakeStdin()
    dead = FakeStdin()
    # 'dead' raises on the FIRST write -> reader should drop+close it, keep feeding 'healthy'.

    def boom(_b):
        raise BrokenPipeError("gone")
    dead.write = boom

    c_ok = {'proc': FakeProc(healthy), 'q': queue.Queue(maxsize=64), 'dead': False}
    c_bad = {'proc': FakeProc(dead), 'q': queue.Queue(maxsize=64), 'dead': False}
    p._consumers = [c_ok, c_bad]
    c_ok['thread'] = threading.Thread(target=p._consumer_writer, args=(c_ok,), daemon=True)
    c_bad['thread'] = threading.Thread(target=p._consumer_writer, args=(c_bad,), daemon=True)
    c_ok['thread'].start()
    c_bad['thread'].start()

    payload = b"".join(bytes([i % 256]) * 1000 for i in range(20))  # 20 KB
    source = io.BytesIO(payload)
    stop = threading.Event()
    p._fanout_stop = stop
    p._fanout(source, stop)   # reads the live p._consumers list

    c_ok['thread'].join(2)
    c_bad['thread'].join(2)
    check("healthy consumer received the whole stream", bytes(healthy.buf) == payload)
    check("dead consumer's writer was closed (not left for GC)", dead.close_count >= 1)
    check("healthy writer thread exited", not c_ok['thread'].is_alive())


def test_fanout_retires_a_wedged_consumer_instead_of_blocking():
    # One consumer wedges (never drains). The reader must retire it (queue full)
    # and finish feeding the healthy one, rather than blocking the whole wall.
    p = _make_player()
    p.FANOUT_QUEUE_MAX = 4   # tiny, so the wedged queue fills fast
    p.FANOUT_PUT_TIMEOUT = 1.0  # retire quickly so the test stays fast
    healthy = FakeStdin()
    wedged = FakeStdin(wedged=True)
    c_ok = {'proc': FakeProc(healthy), 'q': queue.Queue(maxsize=4), 'dead': False}
    c_bad = {'proc': FakeProc(wedged), 'q': queue.Queue(maxsize=4), 'dead': False}
    p._consumers = [c_ok, c_bad]
    for c in (c_ok, c_bad):
        c['thread'] = threading.Thread(target=p._consumer_writer, args=(c,), daemon=True)
        c['thread'].start()

    # Many 64 KB chunks, so the wedged 4-slot queue (plus the one the writer is
    # blocked on) overflows and the reader must retire that consumer.
    payload = bytes(64 * 1024 * 12)  # ~768 KB -> ~12 read chunks
    stop = threading.Event()
    p._fanout_stop = stop
    done = threading.Event()

    def run_reader():
        p._fanout(io.BytesIO(payload), stop)   # reads the live p._consumers list
        done.set()
    threading.Thread(target=run_reader, daemon=True).start()

    finished = done.wait(5)
    # Let the healthy writer finish draining (reader put a None sentinel on EOF).
    c_ok['thread'].join(3)
    wedged.close()  # release the wedged writer so its thread can exit
    c_bad['thread'].join(3)
    check("reader did NOT block on the wedged consumer", finished is True)
    check("wedged consumer was retired (marked dead)", c_bad['dead'] is True)
    check("healthy consumer still got the full stream", bytes(healthy.buf) == payload)


def test_terminate_kills_download_first_then_joins():
    # _terminate must kill yt-dlp BEFORE joining threads, and a second stdin
    # close must be a harmless no-op.
    p = _make_player()
    order = []

    class KillProc:
        def __init__(self, tag, stdin=None):
            self.tag, self.stdin, self.pid = tag, stdin, 12345

    orig_kill = vt._kill_tree
    vt._kill_tree = lambda proc: order.append(getattr(proc, 'tag', 'ffplay'))
    try:
        yt = KillProc('ytdlp')
        s1 = FakeStdin()
        ff = KillProc('ffplay', stdin=s1)
        p.ytdlp_process = yt
        c = {'proc': ff, 'q': queue.Queue(maxsize=8), 'dead': False}
        # an already-finished writer thread
        c['thread'] = threading.Thread(target=lambda: None)
        c['thread'].start()
        c['thread'].join()
        p._consumers = [c]
        p.ffplay_processes = [ff]
        p._fanout_stop = threading.Event()
        p._fanout_thread = None
        p._terminate(join=True)
    finally:
        vt._kill_tree = orig_kill

    check("yt-dlp killed before ffplay", order and order[0] == 'ytdlp')
    check("ffplay also killed", 'ffplay' in order)
    check("state cleared after terminate",
          p.ytdlp_process is None and p.ffplay_processes == [] and p._consumers == [])
    check("ffplay stdin closed during teardown", s1.close_count >= 1)


def _fake_build(store):
    """A _build_consumer stand-in that creates a fully working in-memory
    consumer (FakeStdin + real writer thread) and records it in `store`."""
    def build(self, mon, index, single, muted):
        stdin = FakeStdin()
        c = {'proc': FakeProc(stdin), 'q': queue.Queue(maxsize=self.FANOUT_QUEUE_MAX),
             'mon': mon, 'index': index, 'single': single, 'muted': muted,
             'dead': False, 'full_since': None, 'restarts': 0, 'first_restart': 0.0}
        c['thread'] = threading.Thread(target=self._consumer_writer, args=(c,), daemon=True)
        c['thread'].start()
        store.append(c)
        return c
    return build


def test_windows_spawn_only_after_first_data():
    # The wall must NOT exist until real data arrives (a dead launch then costs
    # zero windows - no black fullscreen flashes on every offline retry), and
    # once the first chunk lands every spawned window gets the FULL stream.
    p = _make_player()
    built = []
    orig_build = vt.Player._build_consumer
    vt.Player._build_consumer = _fake_build(built)
    try:
        p.play_flag = True
        p.ytdlp_process = FakeProc(None)          # download 'running'
        mons = [{'index': i, 'x': 1920 * i, 'y': 0, 'width': 1920, 'height': 1080,
                 'name': 'M%d' % i, 'is_primary': i == 0} for i in range(2)]
        p._spawn_plan = {'targets': mons, 'single': False, 'muted': True}
        stop = threading.Event()
        p._fanout_stop = stop
        check("no windows before any data arrived", p._consumers == [] and not built)

        payload = b"".join(bytes([i % 256]) * 1000 for i in range(30))  # 30 KB
        p._fanout(io.BytesIO(payload), stop)
        for c in built:
            c['thread'].join(3)
        check("windows spawned on the first chunk (both monitors)",
              p._windows_spawned is True and len(p._consumers) == 2)
        check("every spawned window received the byte-exact full stream",
              all(bytes(c['proc'].stdin.buf) == payload for c in built))
        check("first-data flag feeds the stall watchdog thresholds",
              p._got_first_data is True)
    finally:
        vt.Player._build_consumer = orig_build


def test_no_spawn_after_stop_raced_in():
    # A Stop that lands before the first byte must prevent the spawn entirely
    # (nothing published, nothing leaked).
    p = _make_player()
    built = []
    orig_build = vt.Player._build_consumer
    vt.Player._build_consumer = _fake_build(built)
    try:
        p.play_flag = False                        # Stop already happened
        p.ytdlp_process = FakeProc(None)
        p._spawn_plan = {'targets': [{'index': 0, 'x': 0, 'y': 0, 'width': 1920,
                                      'height': 1080, 'name': 'M', 'is_primary': True}],
                         'single': True, 'muted': False}
        stop = threading.Event()
        p._fanout_stop = stop
        p._fanout(io.BytesIO(b"x" * 70000), stop)
        check("stopped player never spawned a window",
              p._consumers == [] and p._windows_spawned is False)
    finally:
        vt.Player._build_consumer = orig_build


if __name__ == '__main__':
    for fn in [test_fanout_copies_to_all_and_closes_dead_writer,
               test_fanout_retires_a_wedged_consumer_instead_of_blocking,
               test_terminate_kills_download_first_then_joins,
               test_windows_spawn_only_after_first_data,
               test_no_spawn_after_stop_raced_in]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
