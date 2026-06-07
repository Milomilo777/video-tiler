"""Per-window relaunch tests: a single fallen window must be restarted ON ITS
OWN, leaving the rest of the multi-monitor wall untouched (no full teardown, no
blink) - and only escalate to a full reconnect if one window keeps dying. This
guards against the verified #1 freeze cause: 'retiring ONE slow window tears down
the ENTIRE wall, producing a perpetual relaunch-storm'.

In-memory fakes only - no real ffplay/yt-dlp, no GUI, no network.

    .venv\\Scripts\\python.exe tests\\test_relaunch.py
"""

import os
import sys
import queue
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


class FakeProc:
    _next = 3_000_000_000

    def __init__(self):
        FakeProc._next += 1
        self.pid = FakeProc._next
        self.killed = False
        self.stdin = None

    def kill(self):
        self.killed = True

    def poll(self):
        return 0 if self.killed else None


def _mons(n):
    return [{'index': i, 'is_primary': i == 0, 'x': i * 1920, 'y': 0,
             'width': 1920, 'height': 1080, 'name': 'M%d' % i} for i in range(n)]


def _consumer(mon, index):
    return {'proc': FakeProc(), 'q': queue.Queue(maxsize=4), 'mon': mon,
            'index': index, 'single': False, 'muted': index > 0,
            'dead': False, 'full_since': None, 'restarts': 0, 'first_restart': 0.0}


def _player(n_windows):
    class App:
        pass
    p = vt.Player(App(), "http://x", 2)
    p.play_flag = True
    p.ytdlp_process = FakeProc()              # download alive
    mons = _mons(n_windows)
    p._consumers = [_consumer(mons[i], i) for i in range(n_windows)]
    p.ffplay_processes = [c['proc'] for c in p._consumers]
    return p


def test_relaunch_only_the_dead_window():
    killed = []
    built = []
    orig_kill, orig_build = vt._kill_tree, vt.Player._build_consumer
    vt._kill_tree = lambda proc: killed.append(getattr(proc, 'pid', None))

    def fake_build(self, mon, index, single, muted):
        c = _consumer(mon, index)
        c['single'], c['muted'] = single, muted
        built.append(c)
        return c
    vt.Player._build_consumer = fake_build
    try:
        p = _player(3)
        old0, old1, old2 = p._consumers
        p._consumers[1]['dead'] = True        # window #2 retired/fell behind

        ok = p._relaunch_consumer(1)
        check("relaunch reports success (no escalation)", ok is True)
        check("the fallen window was replaced with a FRESH consumer",
              p._consumers[1] is not old1 and p._consumers[1] in built)
        check("the healthy windows are UNTOUCHED (the wall never blinks)",
              p._consumers[0] is old0 and p._consumers[2] is old2)
        check("only the fallen window's ffplay was killed",
              old1['proc'].pid in killed
              and old0['proc'].pid not in killed
              and old2['proc'].pid not in killed)
        check("the ffplay_processes mirror tracks the replacement",
              p.ffplay_processes[1] is p._consumers[1]['proc']
              and p.ffplay_processes[0] is old0['proc'])
        check("the new window carries restart count 1", p._consumers[1]['restarts'] == 1)
    finally:
        vt._kill_tree, vt.Player._build_consumer = orig_kill, orig_build


def test_relaunch_escalates_only_after_repeated_failures():
    orig_kill, orig_build, orig_mono = vt._kill_tree, vt.Player._build_consumer, vt.time.monotonic
    vt._kill_tree = lambda proc: None
    vt.Player._build_consumer = lambda self, mon, index, single, muted: _consumer(mon, index)
    clock = [1000.0]
    vt.time.monotonic = lambda: clock[0]
    try:
        p = _player(1)
        # Within one RESTART_WINDOW, the first MAX_WINDOW_RESTARTS relaunches all
        # succeed (the window gets the benefit of the doubt)...
        for k in range(vt.Player.MAX_WINDOW_RESTARTS):
            p._consumers[0]['dead'] = True
            check("relaunch #%d inside the window succeeds" % (k + 1),
                  p._relaunch_consumer(0) is True)
        # ...but once it has burned through the budget, we ESCALATE (False) so
        # run() falls back to a full reconnect + backoff (a screen that simply
        # cannot play this stream).
        p._consumers[0]['dead'] = True
        check("escalates to a full reconnect once the per-window budget is spent",
              p._relaunch_consumer(0) is False)
        # A stable gap (longer than RESTART_WINDOW) forgives the past failures.
        clock[0] += vt.Player.RESTART_WINDOW + 1.0
        p._consumers[0]['dead'] = True
        check("a long stable gap resets the per-window restart budget",
              p._relaunch_consumer(0) is True)
    finally:
        vt._kill_tree, vt.Player._build_consumer, vt.time.monotonic = orig_kill, orig_build, orig_mono


def test_relaunch_after_stop_does_not_publish():
    # If a Stop landed (play_flag cleared) during the rebuild, the freshly-built
    # window must be torn down, not published into a dead Player.
    orig_kill, orig_build = vt._kill_tree, vt.Player._build_consumer
    killed = []
    vt._kill_tree = lambda proc: killed.append(getattr(proc, 'pid', None))
    new_holder = {}

    def fake_build(self, mon, index, single, muted):
        c = _consumer(mon, index)
        new_holder['c'] = c
        self.play_flag = False            # a Stop races in mid-rebuild
        return c
    vt.Player._build_consumer = fake_build
    try:
        p = _player(2)
        p._consumers[0]['dead'] = True
        ok = p._relaunch_consumer(0)
        check("relaunch is a no-op-safe success when a Stop raced in", ok is True)
        check("the just-built window was killed, not published",
              new_holder['c']['proc'].pid in killed
              and p._consumers[0] is not new_holder['c'])
    finally:
        vt._kill_tree, vt.Player._build_consumer = orig_kill, orig_build


if __name__ == '__main__':
    for fn in [test_relaunch_only_the_dead_window,
               test_relaunch_escalates_only_after_repeated_failures,
               test_relaunch_after_stop_does_not_publish]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
