"""Frontend / GUI smoke tests: drive the REAL Tkinter App (no mocked widgets) to
prove the window builds, the controls behave, settings save off the hot path
(no fsync stall), rapid Play/Stop leaves the UI consistent, the stale-player
guard holds, and an undock does not wipe the saved monitor wall.

This needs a display. If Tk cannot initialise (a truly headless box), the file
SKIPS cleanly (exit 0) so the suite stays green in CI; on the dev desktop it runs
for real. No network, no real yt-dlp/ffplay (Player is replaced by a fake).

    .venv\\Scripts\\python.exe tests\\test_frontend.py
"""

import os
import sys
import tempfile
import importlib.util

SRC = os.path.join(os.path.dirname(__file__), '..', 'src')
sys.path.insert(0, SRC)

spec = importlib.util.spec_from_file_location(
    "video_tiler", os.path.join(SRC, "video-tiler.py"))
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)

import tkinter as tk

_failures = []


def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


FAKE_MONS_3 = [
    {'index': 0, 'x': 0, 'y': 0, 'width': 1920, 'height': 1080, 'name': 'A', 'is_primary': True},
    {'index': 1, 'x': 1920, 'y': 0, 'width': 1920, 'height': 1080, 'name': 'B', 'is_primary': False},
    {'index': 2, 'x': 3840, 'y': 0, 'width': 1920, 'height': 1080, 'name': 'C', 'is_primary': False},
]


class FakePlayer:
    """Stands in for the real Player so play_video()/stop_video() exercise the
    GUI state machine without spawning yt-dlp/ffplay or touching the network."""
    last = None

    def __init__(self, app, url, divisions):
        self.app, self.url, self.divisions = app, url, divisions
        self.tools_ok = True
        self.play_flag = False
        self.stops = 0
        FakePlayer.last = self

    def fetch_title_async(self, cb):
        pass

    def run(self):
        self.play_flag = True               # worker no-op (finishes immediately)

    def stop(self, join=False):
        self.stops += 1
        self.play_flag = False


def _make_app(monitors=FAKE_MONS_3):
    """Build a real App with network/disk side effects neutralised."""
    vt.fetch_title_async = lambda *a, **k: None      # no title network probe
    vt.monitor_utils.list_monitors = lambda: [dict(m) for m in monitors]
    d = tempfile.mkdtemp(prefix="vt_fe_")
    vt.SETTINGS_FILE = os.path.join(d, "settings.json")
    app = vt.App()
    app.check_for_updates = lambda *a, **k: None      # never reach out for updates
    try:
        app.withdraw()                                # keep the window off-screen
    except Exception:
        pass
    app.update()
    return app, d


def _cleanup(app, d):
    try:
        app.on_closing()        # representative teardown (cancels pump, stops, destroys)
    except Exception:
        pass
    try:
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
        os.rmdir(d)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
def test_app_builds_and_saves_without_fsync():
    app, d = _make_app()
    try:
        check("core widgets exist (Play/Stop/status/url)",
              all(hasattr(app, w) for w in
                  ('play_button', 'stop_button', 'status_bar', 'url_entry')))
        # Toggling an option saves settings - and must NOT fsync on the GUI thread.
        called = {'fsync': False}
        orig = vt.os.fsync

        def boom(fd):
            called['fsync'] = True
        vt.os.fsync = boom
        try:
            app.mute.set(True)
            app.save_all_settings()
            app.update()
        finally:
            vt.os.fsync = orig
        check("a settings save did NOT fsync on the GUI thread", called['fsync'] is False)
        check("settings were actually persisted", os.path.exists(vt.SETTINGS_FILE)
              and vt.read_settings()['mute'] is True)
    finally:
        _cleanup(app, d)


def test_rapid_play_stop_keeps_button_consistent():
    orig_player = vt.Player
    vt.Player = FakePlayer
    app, d = _make_app()
    try:
        errors = []
        for _ in range(30):
            try:
                app.play_video()
                app.update()
                app.stop_video()
                app.update()
            except Exception as e:           # pragma: no cover
                errors.append(repr(e))
                break
        check("30 rapid Play/Stop cycles raise nothing", not errors)
        check("Play button is re-enabled after Stop",
              str(app.play_button['state']) == 'normal')
        check("play_flag is cleared after Stop", app.play_flag is False)
    finally:
        vt.Player = orig_player
        _cleanup(app, d)


def test_on_player_finished_ignores_stale_player():
    orig_player = vt.Player
    vt.Player = FakePlayer
    app, d = _make_app()
    try:
        app.play_video()                     # creates the CURRENT player, disables Play
        app.update()
        current = app.player
        check("Play disabled while playing", str(app.play_button['state']) == 'disabled')
        # A stale (already-replaced) player's run() finishing must NOT flip the UI.
        stale = FakePlayer(app, "u", 3)
        app._on_player_finished(stale)
        check("a stale player's finish is ignored (Play stays disabled)",
              str(app.play_button['state']) == 'disabled' and app.player is current)
        # The current player's finish re-enables Play.
        app._on_player_finished(current)
        check("the current player's finish re-enables Play",
              str(app.play_button['state']) == 'normal' and app.play_flag is False)
    finally:
        vt.Player = orig_player
        _cleanup(app, d)


def test_undock_preserves_saved_monitor_wall():
    # Docked: the user configured a 3-screen wall.
    app, d = _make_app(monitors=FAKE_MONS_3)
    try:
        vt.write_settings(dict(vt.DEFAULT_SETTINGS, selected_monitor_indices=[0, 1, 2]))
        app.load_all_settings()
        check("docked: all three screens resolved",
              sorted(app.selected_monitor_indices) == [0, 1, 2])

        # Undock: only monitor 0 is attached now.
        vt.monitor_utils.list_monitors = lambda: [dict(FAKE_MONS_3[0])]
        app.load_all_settings()
        check("undocked: runtime selection collapses to the one attached screen",
              app.selected_monitor_indices == [0])
        check("undocked: the DESIRED wall is preserved unfiltered",
              app._desired_monitor_indices == [0, 1, 2])

        # Any interaction saves; the saved wall must NOT be wiped to [0].
        app.mute.set(True)
        app.save_all_settings()
        saved = vt.read_settings()['selected_monitor_indices']
        check("undocked save PRESERVES the [0,1,2] wall (no silent data loss)",
              sorted(saved) == [0, 1, 2])
    finally:
        _cleanup(app, d)


def test_theme_switch_applies_cleanly():
    app, d = _make_app()
    try:
        ok = True
        for theme in ('Dark', 'Light', 'Dark'):
            try:
                app.apply_theme(theme)
                app.update()
            except Exception:
                ok = False
        check("switching themes applies without error", ok)
    finally:
        _cleanup(app, d)


if __name__ == '__main__':
    try:
        _probe = tk.Tk()
        _probe.destroy()
    except Exception as e:
        print("SKIP: Tk is not available in this environment (%s)" % e)
        sys.exit(0)

    for fn in [test_app_builds_and_saves_without_fsync,
               test_rapid_play_stop_keeps_button_consistent,
               test_on_player_finished_ignores_stale_player,
               test_undock_preserves_saved_monitor_wall,
               test_theme_switch_applies_cleanly]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
