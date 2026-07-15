"""Stress tests for the playback engine: high-volume fan-out under load with a
wedged window (must never drop a byte to the healthy windows), and many rapid
start/teardown cycles (must not leak worker threads or processes).

All in-memory fakes - no real ffplay/yt-dlp, no GUI, no network.

    .venv\\Scripts\\python.exe tests\\test_stress.py
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


# --------------------------------------------------------------------------- #
#  Fakes
# --------------------------------------------------------------------------- #
class FakeStdin:
    def __init__(self, wedged=False):
        self.buf = bytearray()
        self.closed = False
        self._wedged = wedged
        self._release = threading.Event()

    def write(self, b):
        if self._wedged:
            self._release.wait(10)
        if self.closed:
            raise ValueError("write to closed file")
        self.buf.extend(b)

    def flush(self):
        if self.closed:
            raise ValueError("flush of closed file")

    def close(self):
        self.closed = True
        self._release.set()


class FakeProc:
    _next = 2_500_000_000

    def __init__(self, stdin=None):
        self.stdin = stdin
        self.killed = False
        FakeProc._next += 1
        self.pid = FakeProc._next

    def kill(self):
        self.killed = True

    def poll(self):
        return 0 if self.killed else None


def _bare_player(n=2):
    class App:
        pass
    return vt.Player(App(), "http://x", n)


# --------------------------------------------------------------------------- #
#  1. High-volume fan-out: a wedged window must not starve or corrupt the others
# --------------------------------------------------------------------------- #
def test_high_volume_fanout_no_byte_loss_with_one_wedged():
    p = _bare_player()
    p.FANOUT_QUEUE_MAX = 8
    p.FANOUT_PUT_TIMEOUT = 1.0
    n_healthy = 8
    healthy = [FakeStdin() for _ in range(n_healthy)]
    wedged = FakeStdin(wedged=True)

    consumers = []
    for i, h in enumerate(healthy):
        consumers.append({'proc': FakeProc(h), 'q': queue.Queue(maxsize=8),
                          'dead': False, 'index': i, 'full_since': None})
    consumers.append({'proc': FakeProc(wedged), 'q': queue.Queue(maxsize=8),
                      'dead': False, 'index': n_healthy, 'full_since': None})
    p._consumers = consumers
    for c in consumers:
        c['thread'] = threading.Thread(target=p._consumer_writer, args=(c,), daemon=True)
        c['thread'].start()

    payload = os.urandom(64 * 1024 * 40)   # ~2.5 MB across ~40 read chunks
    stop = threading.Event()
    p._fanout_stop = stop
    done = threading.Event()

    def reader():
        p._fanout(io.BytesIO(payload), stop)
        done.set()
    threading.Thread(target=reader, daemon=True).start()

    finished = done.wait(20)
    for c in consumers[:n_healthy]:
        c['thread'].join(5)
    wedged.close()                          # release the wedged writer to exit
    consumers[n_healthy]['thread'].join(5)

    check("reader finished - one wedged window did NOT deadlock the wall", finished)
    check("ALL %d healthy windows got the byte-exact full stream (nothing dropped)"
          % n_healthy, all(bytes(h.buf) == payload for h in healthy))
    check("the wedged window was retired (marked dead)", consumers[n_healthy]['dead'] is True)
    check("the healthy writer threads all exited",
          all(not c['thread'].is_alive() for c in consumers[:n_healthy]))
    check("the fan-out fed the stall watchdog (progress recorded)", p._last_progress > 0)


# --------------------------------------------------------------------------- #
#  2. Many rapid start/teardown cycles must not leak threads or processes
# --------------------------------------------------------------------------- #
class _Source:
    """A pipe-like stream: yields data until the owning proc is killed, then EOF."""
    def __init__(self, alive):
        self._alive = alive

    def read(self, n=65536):
        if not self._alive():
            return b''
        time.sleep(0.001)                   # don't busy-spin the fan-out
        return b'\0' * min(n, 65536)

    def readline(self):                     # stderr drain: immediate EOF
        return b''

    def close(self):
        pass


class _Sink:
    def write(self, b):
        pass

    def flush(self):
        pass

    def close(self):
        pass


class FakePopen:
    _next = 4_000_000_000

    def __init__(self, cmd, stdout=None, stderr=None, stdin=None,
                 creationflags=0, env=None):
        FakePopen._next += 1
        self.pid = FakePopen._next
        self._killed = False
        self.stdin = _Sink()
        self.stdout = _Source(lambda: not self._killed)
        self.stderr = _Source(lambda: False)

    def poll(self):
        return 0 if self._killed else None

    def kill(self):
        self._killed = True

    def wait(self, timeout=None):
        self._killed = True
        return 0


def test_rapid_start_teardown_cycles_do_not_leak_threads():
    class App:
        opt_quality = 'Auto'
        opt_multi_monitor = True
        opt_mute = True
        opt_auto_restart = True
        selected_monitor_indices = [0, 1]

        def post_ui(self, fn):
            try:
                fn()
            except Exception:
                pass

        def update_status(self, *a, **k):
            pass

        def update_yt_dlp(self, silent=True):
            pass

        def _on_player_finished(self, p):
            pass

    fake_mons = [
        {'index': 0, 'x': 0, 'y': 0, 'width': 1920, 'height': 1080,
         'name': 'A', 'is_primary': True},
        {'index': 1, 'x': 1920, 'y': 0, 'width': 1920, 'height': 1080,
         'name': 'B', 'is_primary': False},
    ]

    orig = (vt.find_executable, vt.subprocess.Popen, vt._kill_tree,
            vt.monitor_utils.list_monitors, vt.Player.ENFORCE_RECT)
    vt.find_executable = lambda name: name
    vt.subprocess.Popen = FakePopen
    vt._kill_tree = lambda proc: proc.kill() if hasattr(proc, 'kill') else None
    vt.monitor_utils.list_monitors = lambda: [dict(m) for m in fake_mons]
    vt.Player.ENFORCE_RECT = False          # fake pids own no real windows

    errors = []
    spawned = [0]
    try:
        p = vt.Player(App(), "http://x", 3)
        p.play_flag = True
        baseline = threading.active_count()
        CYCLES = 25
        for _ in range(CYCLES):
            try:
                p._start()                  # launches fake yt-dlp; windows spawn
                                            # on the first fanned-out chunk
                deadline = time.time() + 5.0
                while not p._windows_spawned and time.time() < deadline:
                    time.sleep(0.005)       # wait for the deferred spawn
                if p._windows_spawned and len(p._consumers) == 2:
                    spawned[0] += 1
                p._terminate(join=True)     # must join every per-cycle thread
            except Exception as e:          # pragma: no cover
                errors.append(repr(e))
                break
        p.play_flag = False
        p._terminate(join=True)
        time.sleep(0.4)                     # let any stragglers wind down
        leaked = threading.active_count() - baseline
    finally:
        (vt.find_executable, vt.subprocess.Popen, vt._kill_tree,
         vt.monitor_utils.list_monitors, vt.Player.ENFORCE_RECT) = orig

    check("no exception across %d rapid start/teardown cycles" % CYCLES, not errors)
    check("the deferred spawn produced the full wall every cycle (%d/%d)"
          % (spawned[0], CYCLES), spawned[0] == CYCLES)
    check("worker threads do not accumulate (join=True really joins them); leaked=%d"
          % leaked, leaked <= 2)


if __name__ == '__main__':
    for fn in [test_high_volume_fanout_no_byte_loss_with_one_wedged,
               test_rapid_start_teardown_cycles_do_not_leak_threads]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
