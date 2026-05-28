# Architecture

Compact map of how Video Tiler works. For usage see `README.md`.

## Files
| Path | Role |
|------|------|
| `src/video-tiler.py` | Everything: the `App` (Tkinter GUI) and the `Player` (playback engine). |
| `src/monitor_utils.py` | Pure, GUI-free monitor detection + per-monitor tile filter + window options. Unit-tested. |
| `tests/` | Deterministic tests: geometry (`test_monitor_utils`), format selection (`test_format_select`), engine/threading guards (`test_engine`). |
| `requirements.txt` | Pure cross-platform deps (`yt-dlp`, `screeninfo`, `psutil`, `appdirs`). |
| `run.bat` / `compile_windows.bat` | Run from source / build a Windows exe. |
| `platform/macos/` | macOS installer, Gatekeeper unblock helper, README (groundwork). |
| `VERSION` | Plain version string the update check reads. |

## Playback engine (`Player`)
One live stream is downloaded **once** and shown as an N×N grid of **identical**
tiles. The identical-tiles trick is `fps=source_fps*N²,tile=NxN` (duplicate each
frame N² times, then tile) — done by `monitor_utils.tile_filter_for`.

- **One window per monitor.** A single window spanning several monitors is
  unreliable, so each selected monitor gets its own `ffplay` covering exactly
  that screen. The one download is fanned out to every `ffplay` stdin by a
  background thread (`_fanout`). Only the first window keeps audio (others get
  `-an`) to avoid echo. One monitor → `ffplay` reads the download directly.
- **Liveness by `poll()`.** We own the `Popen` handles, so health is just
  `ytdlp.poll() is None and all(ffplay.poll() is None)`. It requires **every**
  player window to be alive (not just one), so a single dead screen in a
  multi-monitor wall triggers a clean relaunch instead of going black. No
  process-tree scanning, no window hunting → cheap, reliable, cross-platform.
- **Reconnect.** On any process exit, `run()` tears down, then retries with
  **exponential backoff** (3s→30s). A session that lasted ≥60s resets the
  backoff and failure counters.
- **Self-heal.** After 2 consecutive quick failures it runs `yt-dlp -U` once —
  the usual cause of YouTube breakage is a yt-dlp fix not yet installed.
- **Robust extraction.** yt-dlp is invoked with multiple fallback player
  clients (`default,android,tv,ios`), `--retries`, `--socket-timeout`, and a
  height-based `-f` selector that always ends in `/best`.

## Threading model
Tkinter is single-threaded. The rule here: **worker threads never touch Tk** —
not widgets *and not Tk variables* (`BooleanVar.get()` etc. are not thread-safe
either). To write to the GUI, workers push callables onto `App._ui_queue`; the
main loop drains it via `_pump_ui_queue` (an `after` loop). To *read* options,
workers read plain-attribute mirrors (`App.opt_quality`, `opt_multi_monitor`,
`opt_mute`, `opt_auto_restart`) that the main thread keeps in sync via
`_sync_runtime_options` (called on every settings change). `Player.run`,
`update_yt_dlp`, and `check_for_updates` all post UI work through
`app.post_ui(...)`. Titles are fetched async, so the GUI never blocks on the
network.

## Logging
A small `RotatingFileHandler` (512 KB × 2) writes to `videotiler.log` in the
per-user data dir — the only window into what an unattended kiosk did when no
console is attached (windowed/frozen builds). Key lifecycle events are logged:
start (url/divisions/windows/multi/mute), drops + backoff, self-heal, and stop.
On Windows, all helper subprocesses are launched with `CREATE_NO_WINDOW` so the
kiosk never flashes a console.

## Settings & state
Every GUI choice (url, urls, divisions, auto-restart, multi-monitor, selected
monitors, mute, quality, autoplay, run-at-startup, theme) persists to
`settings.json` in the per-user data dir, loaded on launch.

## Updates
`Tools > Update yt-dlp` (manual), self-heal on failure (auto), and a quiet
launch check against `VERSION` on GitHub that only *suggests* a new app version
(opens the releases page). `Run at Windows startup` writes an HKCU Run entry
pointing at the current interpreter/exe — preferring `pythonw.exe` so login does
not flash a console (no-op off Windows).

## Extending
- New per-monitor behaviour → `monitor_utils` (keep it GUI-free + tested).
- New setting → add to `DEFAULT_SETTINGS` and the save/load methods.
- Keep all cross-thread GUI work going through `post_ui`.
