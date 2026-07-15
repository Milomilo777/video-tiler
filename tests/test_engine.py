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
_ORIG_INTERNET_OK = vt.internet_ok   # run() tests stub it; keep the real one


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


class _Dead:
    def poll(self):
        return 0


class _Live:
    def poll(self):
        return None


def _consumer(proc, dead=False, **extra):
    c = {'proc': proc, 'dead': dead, 'index': extra.get('index', 0)}
    c.update(extra)
    return c


def test_alive_requires_ytdlp_and_any_window():
    # New semantics: the session is alive while the download runs AND at least
    # one window still plays. A single dead/retired window no longer condemns the
    # whole wall - run()'s supervisor relaunches it on its own.
    p = vt.Player(FakeApp(), "u", 2)
    p.ytdlp_process = _Live()
    p._consumers = [_consumer(_Live()), _consumer(_Live())]
    check("download + all windows alive -> alive", p._alive() is True)
    # One window retired (dead flag) while the other plays: STILL alive.
    p._consumers = [_consumer(_Live()), _consumer(_Dead(), dead=True)]
    check("one retired window -> wall still alive (targeted relaunch, no blink)",
          p._alive() is True)
    # Every window dead -> not alive (escalate to a full reconnect).
    p._consumers = [_consumer(_Dead(), dead=True), _consumer(_Dead(), dead=True)]
    check("all windows dead -> not alive", p._alive() is False)
    # Dead download -> not alive regardless of the windows.
    p.ytdlp_process = _Dead()
    p._consumers = [_consumer(_Live())]
    check("dead download -> not alive", p._alive() is False)
    # No windows at all -> not alive.
    p.ytdlp_process = _Live()
    p._consumers = []
    check("no windows -> not alive", p._alive() is False)


def test_dead_window_indices():
    p = vt.Player(FakeApp(), "u", 3)
    p.ytdlp_process = _Live()
    p._consumers = [_consumer(_Live(), index=0),                 # healthy
                    _consumer(_Live(), dead=True, index=1),      # retired
                    _consumer(_Dead(), index=2)]                 # exited on its own
    check("retired + exited windows are flagged for relaunch",
          p._dead_window_indices() == [1, 2])
    p._consumers = [_consumer(_Live(), index=0), _consumer(_Live(), index=1)]
    check("a fully healthy wall has no dead windows",
          p._dead_window_indices() == [])


def test_stall_watchdog():
    # A silent freeze (no bytes for STALL_TIMEOUT while processes still look
    # alive) must be detectable - poll() liveness alone cannot see it.
    p = vt.Player(FakeApp(), "u", 2)
    p._got_first_data = True          # mid-stream semantics (30s threshold)
    p._last_progress = vt.time.monotonic()
    check("fresh data flow -> not stalled", p._stalled() is False)
    p._last_progress = vt.time.monotonic() - (vt.Player.STALL_TIMEOUT + 1.0)
    check("no data past STALL_TIMEOUT -> stalled (reconnect)", p._stalled() is True)
    # With the download still 'alive' but silent, _death_reason names the stall.
    p.ytdlp_process = _Live()
    p._consumers = [_consumer(_Live())]
    check("a stall is reported as the death reason",
          "silent" in p._death_reason().lower())


def test_startup_stall_threshold_is_generous():
    # Before the FIRST byte, the generous STARTUP threshold applies: extraction
    # on a slow laptop + slow link can take >30s, and tripping the 30s stall
    # there caused a reconnect-forever loop ("it never works").
    p = vt.Player(FakeApp(), "u", 2)
    check("startup threshold is longer than the mid-stream one",
          vt.Player.STARTUP_STALL_TIMEOUT > vt.Player.STALL_TIMEOUT)
    p._last_progress = vt.time.monotonic() - (vt.Player.STALL_TIMEOUT + 5.0)
    check("31s of silence BEFORE any data -> not yet stalled", p._stalled() is False)
    p._last_progress = vt.time.monotonic() - (vt.Player.STARTUP_STALL_TIMEOUT + 1.0)
    check("past the startup threshold -> stalled", p._stalled() is True)
    p.ytdlp_process = _Live()
    p._consumers = []
    check("the never-any-data case is reported distinctly",
          "no data ever arrived" in p._death_reason().lower())


def test_ffplay_cmd_live_flags():
    # The CPU/live-latency flags that keep a weak laptop near the live edge.
    p = vt.Player(FakeApp(), "u", 3)
    cmd = p._ffplay_cmd(FAKE[1], True, True)
    check("-threads 0 (multicore decode) precedes the '-' input",
          '-threads' in cmd and cmd[cmd.index('-threads') + 1] == '0'
          and cmd.index('-threads') < cmd.index('-'))
    check("-framedrop present (shed late frames -> stay near live, no drift)",
          '-framedrop' in cmd)
    check("still autoexits + stays quiet", '-autoexit' in cmd and '-hide_banner' in cmd)


def test_ffplay_single_monitor_placement():
    # A single PRIMARY-at-origin target uses true fullscreen.
    p = vt.Player(FakeApp(), "u", 2)
    prim = p._ffplay_cmd(FAKE[1], True, True)        # FAKE[1] is primary at x=0
    check("single primary target -> -fs (true fullscreen)",
          '-fs' in prim and '-noborder' not in prim)
    # A single NON-primary target must be POSITIONED, not bare -fs (which would
    # fullscreen on the primary screen instead).
    nonprim = p._ffplay_cmd(FAKE[2], True, True)     # right monitor at x=2560
    check("single non-primary target -> placed borderless window, not -fs",
          '-fs' not in nonprim and '-noborder' in nonprim and '-left' in nonprim
          and str(FAKE[2]['x']) in nonprim)


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
    vt.internet_ok = lambda url, timeout=3.0: True   # tests never touch the net
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
    vt.internet_ok = lambda url, timeout=3.0: True   # tests never touch the net
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


# ---- connectivity gate: offline must probe, not churn --------------------- #
def test_run_offline_gate_spawns_nothing_and_recovers():
    # While the internet is down the reconnect loop must NOT count failures,
    # NOT self-heal, and NOT keep launching the pipeline - it waits on a cheap
    # probe. When the probe passes again, normal reconnection resumes.
    app = FakeApp()
    online = {'v': False}
    vt.internet_ok = lambda url, timeout=3.0: online['v']
    p = vt.Player(app, "http://x", 3)
    starts = [0]

    def fake_start():
        starts[0] += 1
    p._start = fake_start
    p._terminate = lambda join=True: None
    p._death_reason = lambda: "test"
    waits = [0]

    def fake_wait(seconds):
        waits[0] += 1
        if waits[0] == 6:
            online['v'] = True          # the net comes back mid-loop
        if waits[0] >= 9:
            p.play_flag = False
    p._wait_backoff = fake_wait

    orig_sleep = vt.time.sleep
    vt.time.sleep = lambda s: None
    try:
        p.run()
    finally:
        vt.time.sleep = orig_sleep

    # Trace: start#1 fails -> offline -> 6 probe-waits (no launches, no failure
    # counting) -> online -> start#2..#4 fail normally (3 counted failures, one
    # self-heal at failure #2) -> stopped by the 9th wait.
    check("offline probe-waits launched NOTHING (starts stayed at 1 while down)",
          starts[0] == 4)
    check("offline iterations did not count as stream failures",
          p._fail_count == 3)
    check("self-heal ran only while online", app.heals == 1)
    check("an explicit no-internet status was shown",
          any('internet' in m.lower() for m in app.status_msgs))
    check("worker finished cleanly", app.finished is True)


# ---- Auto-quality CPU-pressure step-down ----------------------------------- #
def test_select_format_cpu_pressure():
    sf = vt.select_format
    check("pressure 0 keeps the Auto choice",
          sf('Auto', 1, 0).startswith("best[height<=1080]"))
    check("pressure 1 steps Auto down one rung (1080 -> 720)",
          sf('Auto', 1, 1).startswith("best[height<=720]"))
    check("pressure 2 steps Auto down two rungs (1080 -> 480)",
          sf('Auto', 1, 2).startswith("best[height<=480]"))
    check("pressure clamps at the lowest rung (144)",
          sf('Auto', 64, 3).startswith("best[height<=144]"))
    check("a MANUAL quality is never overridden by pressure",
          sf('720p', 1, 3).startswith("best[height<=720]"))


# ---- exact-fullscreen window geometry -------------------------------------- #
def test_placed_window_gets_exact_monitor_size():
    # A placed (non -fs) window must be the monitor's EXACT size - the
    # tile-floored filter size can run a few px short and leave a desktop
    # sliver ("not quite fullscreen" on some monitors).
    p = vt.Player(FakeApp(multi=True), "u", 7)   # 7 divisions floors unevenly
    cmd = p._ffplay_cmd(FAKE[0], False, True)    # left 1920x1080 monitor
    check("-x carries the monitor's full width",
          '-x' in cmd and cmd[cmd.index('-x') + 1] == '1920')
    check("-y carries the monitor's full height",
          '-y' in cmd and cmd[cmd.index('-y') + 1] == '1080')
    check("-left/-top carry the monitor origin",
          cmd[cmd.index('-left') + 1] == '-1920' and cmd[cmd.index('-top') + 1] == '0')


# ---- connectivity helpers --------------------------------------------------- #
def test_stream_host_and_jitter():
    check("host extracted from the stream URL",
          vt.stream_host("https://www.youtube.com/watch?v=abc") == "www.youtube.com")
    check("garbage URL falls back to the default host",
          vt.stream_host(None) == "www.youtube.com")
    vals = [vt.jittered(30.0) for _ in range(200)]
    check("jitter stays within [30, 37.5]",
          all(30.0 <= v <= 30.0 * 1.25 for v in vals))
    check("jitter actually varies", len({round(v, 6) for v in vals}) > 1)


# ---- offline fallback video ------------------------------------------------ #
def test_find_offline_video_prefers_sibling_over_repo_assets():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sib = os.path.join(d, vt.OFFLINE_VIDEO_FILENAME)
        with open(sib, 'wb') as f:
            f.write(b'x')
        orig_argv0 = sys.argv[0]
        sys.argv[0] = os.path.join(d, 'video-tiler.exe')
        try:
            check("a sibling copy next to the exe wins over the repo assets/ copy",
                  vt.find_offline_video() == sib)
        finally:
            sys.argv[0] = orig_argv0


def test_find_offline_video_falls_back_to_repo_assets():
    orig_argv0 = sys.argv[0]
    sys.argv[0] = os.path.join(os.path.dirname(__file__), 'no_such_dir_xyz', 'video-tiler.exe')
    try:
        p = vt.find_offline_video()
        check("no sibling copy -> finds the repo's assets/offline.mp4",
              p is not None and os.path.isfile(p)
              and os.path.basename(p) == vt.OFFLINE_VIDEO_FILENAME)
    finally:
        sys.argv[0] = orig_argv0


def test_find_offline_video_none_when_absent_everywhere():
    orig_argv0 = sys.argv[0]
    orig_name = vt.OFFLINE_VIDEO_FILENAME
    sys.argv[0] = os.path.join(os.path.dirname(__file__), 'no_such_dir_xyz', 'video-tiler.exe')
    vt.OFFLINE_VIDEO_FILENAME = 'this_file_does_not_exist_anywhere.mp4'
    try:
        check("no file anywhere -> None (degrades to the blank probing wait)",
              vt.find_offline_video() is None)
    finally:
        sys.argv[0] = orig_argv0
        vt.OFFLINE_VIDEO_FILENAME = orig_name


def test_fallback_ffplay_cmd_loops_local_file():
    p = vt.Player(FakeApp(), "u", 3)
    single = p._fallback_ffplay_cmd(FAKE[1], True, True, "C:/x/offline.mp4")
    check("loops the file forever (-loop 0)",
          '-loop' in single and single[single.index('-loop') + 1] == '0')
    check("reads the local file directly (not stdin)", "C:/x/offline.mp4" in single)
    check("single window still uses -fs", '-fs' in single)
    check("muted window gets -an", '-an' in single)
    multi = p._fallback_ffplay_cmd(FAKE[0], False, False, "C:/x/offline.mp4")
    check("multi window is placed, not -fs", '-fs' not in multi and '-noborder' in multi)
    check("unmuted window keeps audio (no -an)", '-an' not in multi)


class _MutedFalseApp(FakeApp):
    def __init__(self):
        super().__init__(multi=True)
        self.opt_mute = False


def test_play_offline_fallback_spawns_per_monitor_and_recovers():
    # Fallback windows are built per target monitor (only the first keeps
    # audio, same convention as the live wall), probed every
    # OFFLINE_FALLBACK_PROBE_INTERVAL, and torn down once the internet is
    # back - the caller is told to reconnect.
    app = _MutedFalseApp()
    p = vt.Player(app, "http://x", 3)
    built, killed = [], []
    orig_build, orig_kill, orig_internet = (
        vt.Player._build_fallback_consumer, vt._kill_tree, vt.internet_ok)

    def fake_build(self, mon, single, muted, video_path):
        built.append((mon['index'], single, muted, video_path))
        return _Live()
    vt.Player._build_fallback_consumer = fake_build
    vt._kill_tree = lambda proc: killed.append(proc)

    online = {'v': False}
    vt.internet_ok = lambda url, timeout=3.0: online['v']
    waits = []

    def fake_wait(seconds):
        waits.append(seconds)
        if len(waits) == 3:
            online['v'] = True
    p._wait_backoff = fake_wait
    p.play_flag = True   # _play_offline_fallback is normally only called while playing

    try:
        result = p._play_offline_fallback("C:/x/offline.mp4")
    finally:
        vt.Player._build_fallback_consumer, vt._kill_tree, vt.internet_ok = (
            orig_build, orig_kill, orig_internet)

    check("one fallback window spawned per monitor", len(built) == 3)
    check("only the first window kept audio", [b[2] for b in built] == [False, True, True])
    check("every window got the fallback file path",
          all(b[3] == "C:/x/offline.mp4" for b in built))
    check("probes at ~OFFLINE_FALLBACK_PROBE_INTERVAL (3 min), not the fast 30s gate",
          all(seconds >= vt.Player.OFFLINE_FALLBACK_PROBE_INTERVAL for seconds in waits))
    check("returns True once online (caller reconnects)", result is True)
    check("all fallback windows were torn down once online", len(killed) == 3)


def test_play_offline_fallback_stops_cleanly_without_reconnecting():
    app = _MutedFalseApp()
    p = vt.Player(app, "http://x", 3)
    killed = []
    orig_build, orig_kill, orig_internet = (
        vt.Player._build_fallback_consumer, vt._kill_tree, vt.internet_ok)
    vt.Player._build_fallback_consumer = lambda self, mon, single, muted, video_path: _Live()
    vt._kill_tree = lambda proc: killed.append(proc)
    vt.internet_ok = lambda url, timeout=3.0: False   # net never returns

    def fake_wait(seconds):
        p.play_flag = False   # a Stop arrives while still offline
    p._wait_backoff = fake_wait
    p.play_flag = True

    try:
        result = p._play_offline_fallback("C:/x/offline.mp4")
    finally:
        vt.Player._build_fallback_consumer, vt._kill_tree, vt.internet_ok = (
            orig_build, orig_kill, orig_internet)

    check("returns False when stopped instead of reconnecting", result is False)
    check("fallback windows are still torn down on stop", len(killed) == 3)


def test_play_offline_fallback_degrades_when_spawn_fails():
    # If ffplay itself can't launch the fallback file, the caller must fall
    # back to the original blank probing wait rather than looping forever.
    app = _MutedFalseApp()
    p = vt.Player(app, "http://x", 3)
    killed = []
    orig_build, orig_kill = vt.Player._build_fallback_consumer, vt._kill_tree

    def fake_build_fail(self, mon, single, muted, video_path):
        raise RuntimeError("ffplay not found")
    vt.Player._build_fallback_consumer = fake_build_fail
    vt._kill_tree = lambda proc: killed.append(proc)
    try:
        result = p._play_offline_fallback("C:/x/offline.mp4")
    finally:
        vt.Player._build_fallback_consumer, vt._kill_tree = orig_build, orig_kill

    check("returns False so the caller degrades to the blank-wait probe", result is False)
    check("nothing was spawned, so nothing needed killing", killed == [])


def test_run_uses_offline_fallback_video_when_present():
    # Wiring test: run()'s connectivity gate must reach for the fallback video
    # (if find_offline_video() found one) BEFORE showing the blank
    # "waiting for it to return" status, and resume normal reconnection once
    # the fallback path itself reports the internet is back.
    app = FakeApp()
    online = {'v': False}
    vt.internet_ok = lambda url, timeout=3.0: online['v']
    orig_find = vt.find_offline_video
    vt.find_offline_video = lambda: "C:/x/offline.mp4"
    p = vt.Player(app, "http://x", 3)
    starts = [0]

    def fake_start():
        starts[0] += 1
    p._start = fake_start
    p._alive = lambda: False
    p._terminate = lambda join=True: None
    p._death_reason = lambda: "test"
    fallback_calls = []

    def fake_fallback(video_path):
        fallback_calls.append(video_path)
        online['v'] = True   # connectivity "returns" while the fallback played
        return True
    p._play_offline_fallback = fake_fallback
    waits = [0]

    def fake_wait(seconds):
        waits[0] += 1
        if waits[0] >= 5:
            p.play_flag = False
    p._wait_backoff = fake_wait

    orig_sleep = vt.time.sleep
    vt.time.sleep = lambda s: None
    try:
        p.run()
    finally:
        vt.time.sleep = orig_sleep
        vt.find_offline_video = orig_find

    check("the discovered fallback video was used",
          fallback_calls == ["C:/x/offline.mp4"])
    check("the blank 'waiting for it to return' status was never shown "
          "(the fallback video covered the wall instead)",
          not any('waiting for it to return' in m.lower() for m in app.status_msgs))
    check("run() kept reconnecting after the fallback reported online",
          starts[0] >= 2)


def test_internet_ok_uses_tcp_probe():
    # NB: earlier run() tests stub vt.internet_ok, so probe the ORIGINAL.
    probe = _ORIG_INTERNET_OK
    calls = []
    orig = vt.socket.create_connection

    class _Sock:
        def close(self):
            pass

    def fake_ok(addr, timeout=None):
        calls.append(addr)
        return _Sock()

    def fake_fail(addr, timeout=None):
        calls.append(addr)
        raise OSError("unreachable")
    try:
        vt.socket.create_connection = fake_ok
        check("first candidate reachable -> online after ONE probe",
              probe("https://example.com/live") is True and len(calls) == 1
              and calls[0][0] == "example.com")
        calls.clear()
        vt.socket.create_connection = fake_fail
        check("all candidates unreachable -> offline",
              probe("https://example.com/live") is False and len(calls) == 3)
    finally:
        vt.socket.create_connection = orig


if __name__ == '__main__':
    for fn in [test_yt_dlp_cmd, test_targets_uses_mirror_and_count,
               test_ffplay_cmd_single_is_fullscreen, test_ffplay_cmd_live_flags,
               test_ffplay_single_monitor_placement,
               test_alive_requires_ytdlp_and_any_window, test_dead_window_indices,
               test_stall_watchdog, test_startup_stall_threshold_is_generous,
               test_url_validation, test_clamp_divisions, test_run_state_machine,
               test_run_healthy_session_resets,
               test_run_offline_gate_spawns_nothing_and_recovers,
               test_select_format_cpu_pressure,
               test_placed_window_gets_exact_monitor_size,
               test_find_offline_video_prefers_sibling_over_repo_assets,
               test_find_offline_video_falls_back_to_repo_assets,
               test_find_offline_video_none_when_absent_everywhere,
               test_fallback_ffplay_cmd_loops_local_file,
               test_play_offline_fallback_spawns_per_monitor_and_recovers,
               test_play_offline_fallback_stops_cleanly_without_reconnecting,
               test_play_offline_fallback_degrades_when_spawn_fails,
               test_run_uses_offline_fallback_video_when_present,
               test_stream_host_and_jitter, test_internet_ok_uses_tcp_probe]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
