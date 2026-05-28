"""Engine / threading-model / state-machine tests for the Player and App.

Loaded via importlib (the module file name has a hyphen). Importing it does not
start the GUI. No network, no real yt-dlp/ffmpeg, no display needed - monitors
are monkeypatched to a synthetic layout so results don't depend on hardware.

    .venv\\Scripts\\python.exe tests\\test_engine.py
"""

import os
import re
import sys
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


# Synthetic 3-monitor layout (left 1080p, center 1440p primary, right 1080p),
# already in left-to-right order with spatial indices.
FAKE = [
    {'index': 0, 'x': -1920, 'y': 0, 'width': 1920, 'height': 1080, 'name': 'L', 'is_primary': False},
    {'index': 1, 'x': 0, 'y': 0, 'width': 2560, 'height': 1440, 'name': 'C', 'is_primary': True},
    {'index': 2, 'x': 2560, 'y': 0, 'width': 1920, 'height': 1080, 'name': 'R', 'is_primary': False},
]
vt.monitor_utils.list_monitors = lambda: [dict(m) for m in FAKE]


class FakeApp:
    """Mimics only what a worker thread is allowed to read - the plain mirrors -
    plus the small surface Player.run touches. NO Tk variables."""
    def __init__(self, multi=False):
        self.opt_quality = '480p'
        self.opt_multi_monitor = multi
        self.opt_mute = True
        self.opt_auto_restart = True
        self.selected_monitor_indices = [0, 1, 2]
        self.status_msgs = []
        self.finished = False
        self.heals = 0

    def post_ui(self, fn):
        try:
            fn()
        except Exception:
            pass

    def update_status(self, msg, color='black'):
        self.status_msgs.append(msg)

    def update_yt_dlp(self, silent=True):
        self.heals += 1

    def _on_player_finished(self, player):
        self.finished = True


# ---- command building (uses opt_* mirrors only) --------------------------- #
def test_yt_dlp_cmd():
    p = vt.Player(FakeApp(), "https://example.com/watch?v=abc", 4)
    cmd = p._yt_dlp_cmd()
    check("URL is passed AFTER a -- end-of-options marker (injection guard)",
          cmd[-2:] == ['--', "https://example.com/watch?v=abc"])
    check("pipes to stdout", '-o' in cmd and cmd[cmd.index('-o') + 1] == '-')
    check("does NOT force IPv4 (-4 removed)", '-4' not in cmd)
    check("does NOT pass --quiet (so errors reach captured stderr)", '--quiet' not in cmd)
    check("carries the player-client fallbacks",
          'youtube:player_client=' + vt.YT_PLAYER_CLIENTS in cmd)
    fi = cmd.index('-f')
    check("honours the mirrored 480p quality", cmd[fi + 1].startswith("best[height<=480]"))


def test_targets_uses_mirror_and_count():
    single = vt.Player(FakeApp(multi=False), "u", 3)._targets()
    check("multi OFF -> one target", single[1] is False and len(single[0]) == 1)
    multi = vt.Player(FakeApp(multi=True), "u", 3)._targets()
    check("multi ON -> all three targets", multi[1] is True and len(multi[0]) == 3)


def test_ffplay_cmd_single_is_fullscreen():
    p = vt.Player(FakeApp(), "u", 3)
    single = p._ffplay_cmd(FAKE[1], True, True)
    check("single window uses -fs (true fullscreen)", '-fs' in single)
    multi = p._ffplay_cmd(FAKE[0], False, True)
    check("multi window is borderless placed (no -fs)", '-fs' not in multi and '-noborder' in multi)
    check("muted window gets -an", '-an' in single)


def test_alive_requires_all_players():
    class Dead:
        def poll(self):
            return 0

    class Live:
        def poll(self):
            return None

    p = vt.Player(FakeApp(), "u", 2)
    p.ytdlp_process = Live()
    p.ffplay_processes = [Live(), Live()]
    check("all players alive -> alive", p._alive() is True)
    p.ffplay_processes = [Live(), Dead()]
    check("one dead/retired player -> not alive (relaunch)", p._alive() is False)
    p.ytdlp_process = Dead()
    p.ffplay_processes = [Live(), Live()]
    check("dead download -> not alive", p._alive() is False)
    p.ytdlp_process = Live()
    p.ffplay_processes = []
    check("no players -> not alive", p._alive() is False)
    # A retired consumer (dead flag) must trip _alive immediately, without
    # waiting for a wedged ffplay to notice EOF and exit.
    p.ytdlp_process = Live()
    p.ffplay_processes = [Live(), Live()]
    p._consumers = [{'dead': False}, {'dead': True}]
    check("a retired consumer -> not alive (even if its process still polls live)",
          p._alive() is False)
    p._consumers = []


# ---- URL validation (argument-injection guard) ---------------------------- #
def test_url_validation():
    ok = vt.is_valid_stream_url
    check("https accepted", ok("https://youtu.be/x"))
    check("http accepted", ok("http://x"))
    check("dash-option rejected", not ok("--exec=calc.exe https://y"))
    check("file scheme rejected", not ok("file:///etc/passwd"))
    check("None rejected", not ok(None))
    check("empty rejected", not ok("   "))


# ---- clamp_divisions ------------------------------------------------------- #
def test_clamp_divisions():
    cd = vt.clamp_divisions
    check("clamps 999 -> 64", cd('999') == 64)
    check("clamps 0 -> 1", cd('0') == 1)
    check("non-int '' -> default 3", cd('') == 3)
    check("non-int 'abc' -> default 3", cd('abc') == 3)
    check("None -> default 3", cd(None) == 3)
    check("in-range passes through", cd('32') == 32)


# ---- the reconnect / backoff / self-heal state machine -------------------- #
def test_run_state_machine():
    app = FakeApp()
    p = vt.Player(app, "http://x", 3)
    # Make every session fail instantly, with no real processes or sleeps.
    p._start = lambda: None
    p._alive = lambda: False
    p._terminate = lambda join=True: None
    p._death_reason = lambda: "test"
    iters = [0]

    def fake_wait(backoff):
        iters[0] += 1
        if iters[0] >= 25:
            p.play_flag = False
    p._wait_backoff = fake_wait

    orig_sleep = vt.time.sleep
    vt.time.sleep = lambda s: None
    try:
        p.run()
    finally:
        vt.time.sleep = orig_sleep

    backoffs = [int(re.search(r'in (\d+)s', m).group(1))
                for m in app.status_msgs if 'Reconnecting in' in m]
    check("backoff sequence starts 3,6,12,24,30", backoffs[:5] == [3, 6, 12, 24, 30])
    check("self-heal fires twice (arm at 2, re-arm at REHEAL_EVERY=20)", app.heals == 2)
    check("offline status is surfaced past the threshold",
          any('offline' in m.lower() for m in app.status_msgs))
    check("worker reports finished at the end", app.finished is True)


def test_run_healthy_session_resets():
    # A session that lasts >= HEALTHY_SECONDS must reset the failure/heal state.
    app = FakeApp()
    p = vt.Player(app, "http://x", 3)
    p._fail_count = 5
    p._healed = True
    p._start = lambda: None
    p._alive = lambda: False
    p._terminate = lambda join=True: None
    p._death_reason = lambda: "ok"
    p._wait_backoff = lambda b: setattr(p, 'play_flag', False)
    # Back-date 'started' so ran_for >= HEALTHY_SECONDS on the first session:
    # 1st time.time() (started) = 0, 2nd (ran_for) = HEALTHY_SECONDS + 1.
    calls = {'n': 0}
    orig_time, orig_sleep = vt.time.time, vt.time.sleep

    def fake_time():
        calls['n'] += 1
        return 0.0 if calls['n'] == 1 else float(vt.Player.HEALTHY_SECONDS + 1)
    vt.time.time = fake_time
    vt.time.sleep = lambda s: None
    try:
        p.run()
    finally:
        vt.time.time, vt.time.sleep = orig_time, orig_sleep
    # Reset to 0, then the single trailing failure increments it back to 1.
    check("healthy session reset the failure counter (5 -> 0 -> 1)", p._fail_count == 1)
    check("healthy session re-armed self-heal (_healed False)", p._healed is False)


if __name__ == '__main__':
    for fn in [test_yt_dlp_cmd, test_targets_uses_mirror_and_count,
               test_ffplay_cmd_single_is_fullscreen, test_alive_requires_all_players,
               test_url_validation, test_clamp_divisions, test_run_state_machine,
               test_run_healthy_session_resets]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
