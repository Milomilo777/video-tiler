"""MANUAL real end-to-end smoke: run the ACTUAL Player engine against the real
live stream and confirm it plays steadily - no freeze, no relaunch storm, no
silent stall - then stops cleanly.

This is NOT part of the deterministic suite (run_tests.py): it needs network +
real yt-dlp/ffplay and it briefly opens a SMALL corner ffplay window (a fake
640x360 non-primary monitor, so it does not take over your screen with -fs).

    .venv\\Scripts\\python.exe tests\\smoke_live_playback.py [url] [seconds]

Exit 0 = played fine. Exit 2 = a real problem. Exit 3 = environment/network not
available (skipped).
"""

import os
import sys
import time
import shutil
import threading
import importlib.util

SRC = os.path.join(os.path.dirname(__file__), '..', 'src')
sys.path.insert(0, SRC)
spec = importlib.util.spec_from_file_location("video_tiler", os.path.join(SRC, "video-tiler.py"))
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)

URL = sys.argv[1] if len(sys.argv) > 1 else vt.DEFAULT_URL
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 25

# A small, NON-primary fake monitor so the single window is a placed 636x356 box
# in the corner (window_opts_for), not a screen-grabbing -fs fullscreen.
SMALL = [{'index': 0, 'x': 60, 'y': 60, 'width': 640, 'height': 360,
          'name': 'smoke', 'is_primary': False}]


class SmokeApp:
    opt_quality = '240p'           # light: fast start, low CPU
    opt_multi_monitor = False
    opt_mute = True
    opt_auto_restart = True
    selected_monitor_indices = [0]

    def __init__(self):
        self.status = []
        self.finished = False

    def post_ui(self, fn):
        try:
            fn()
        except Exception:
            pass

    def update_status(self, msg, color='black'):
        self.status.append(msg)
        print("   status:", msg)

    def update_yt_dlp(self, silent=True):
        print("   (self-heal update requested - skipped in smoke)")

    def _on_player_finished(self, player):
        self.finished = True


def main():
    if not (shutil.which("yt-dlp") and shutil.which("ffplay") and shutil.which("ffmpeg")):
        print("SKIP: yt-dlp/ffmpeg/ffplay not on PATH.")
        return 3

    vt.monitor_utils.list_monitors = lambda: [dict(m) for m in SMALL]
    app = SmokeApp()
    player = vt.Player(app, URL, 3)        # 3x3 grid in a small window
    if not player.tools_ok:
        print("SKIP: tools not resolvable.")
        return 3

    print("Playing %s for ~%ds (small corner window)..." % (URL, SECONDS))
    t = threading.Thread(target=player.run, daemon=True)
    t.start()

    deadline = time.monotonic() + SECONDS
    relaunches = 0
    prev_first_proc = None
    alive_samples = 0
    progressing = 0
    last_seen_progress = -1.0
    try:
        while time.monotonic() < deadline:
            time.sleep(1.0)
            with player._lock:
                cons = list(player._consumers)
            alive = player._alive()
            if alive:
                alive_samples += 1
            # count how often the first window's process object changes (a relaunch)
            first = cons[0]['proc'] if cons else None
            if first is not None and prev_first_proc is not None and first is not prev_first_proc:
                relaunches += 1
            prev_first_proc = first
            if player._last_progress != last_seen_progress:
                progressing += 1
                last_seen_progress = player._last_progress
            print("   t+%2ds alive=%s windows=%d stalled=%s relaunches=%d"
                  % (int(SECONDS - (deadline - time.monotonic())), alive, len(cons),
                     player._stalled(), relaunches))
    finally:
        player.stop(join=True)
        t.join(5)

    # ---- verdict ----
    ok = True
    def say(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    say("playback was alive for most of the run (>=60%% of samples)",
        alive_samples >= int(0.6 * SECONDS))
    say("bytes kept flowing from the download (progress advanced repeatedly)",
        progressing >= 3)
    say("no relaunch storm (few or no whole-window restarts)", relaunches <= 2)
    say("worker reported a clean finish after stop", app.finished is True)
    say("no thread left running after stop", not t.is_alive())
    print()
    print("RESULT:", "OK" if ok else "PROBLEM")
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
