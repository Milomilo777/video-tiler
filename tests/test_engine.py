"""Engine / threading-model guard tests for the Player.

The design rule is: worker threads (Player.run and the methods it calls) must
NEVER read Tk variables - only the plain `opt_*` mirrors the App keeps in sync
on the main thread. We enforce that here with a deliberately minimal fake app
that exposes ONLY those mirrors (no .quality / .mute / .multi_monitor /
.auto_restart_video Tk vars). If the engine regressed to touching a Tk variable,
these calls would raise AttributeError and fail the test.

No GUI, no network, no real yt-dlp/ffmpeg needed.

    .venv\\Scripts\\python.exe tests\\test_engine.py
"""

import os
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


class FakeApp:
    """Mimics only what a worker thread is allowed to read - the plain mirrors.
    Intentionally has NO Tk variables, so any stray .get() call would blow up."""
    def __init__(self):
        self.opt_quality = '480p'
        self.opt_multi_monitor = False
        self.opt_mute = True
        self.opt_auto_restart = False
        self.selected_monitor_indices = [0]


def test_yt_dlp_cmd_uses_mirror_only():
    p = vt.Player(FakeApp(), "https://example.com/watch?v=abc", 4)
    cmd = p._yt_dlp_cmd()
    check("yt-dlp cmd pipes to stdout", cmd[-2:] == ['-o', '-'])
    check("yt-dlp cmd carries the player-client fallbacks",
          'youtube:player_client=' + vt.YT_PLAYER_CLIENTS in cmd)
    fi = cmd.index('-f')
    check("yt-dlp cmd honours the mirrored 480p quality",
          cmd[fi + 1].startswith("best[height<=480]"))


def test_targets_uses_mirror_only():
    targets, multi = vt.Player(FakeApp(), "u", 3)._targets()
    check("multi flag comes from the mirror (False)", multi is False)
    check("single-monitor target resolves to exactly one screen", len(targets) == 1)


def test_alive_requires_all_players():
    class Dead:
        def poll(self):
            return 0       # exited

    class Live:
        def poll(self):
            return None    # running

    p = vt.Player(FakeApp(), "u", 2)
    p.ytdlp_process = Live()
    p.ffplay_processes = [Live(), Live()]
    check("all players alive -> alive", p._alive() is True)
    p.ffplay_processes = [Live(), Dead()]
    check("one dead player -> not alive (will relaunch)", p._alive() is False)
    p.ytdlp_process = Dead()
    p.ffplay_processes = [Live(), Live()]
    check("dead download -> not alive", p._alive() is False)
    p.ytdlp_process = Live()
    p.ffplay_processes = []
    check("no players -> not alive", p._alive() is False)


if __name__ == '__main__':
    for fn in [test_yt_dlp_cmd_uses_mirror_only, test_targets_uses_mirror_only,
               test_alive_requires_all_players]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
