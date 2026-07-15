# Architecture

Compact map of how Video Tiler works. For usage see `README.md`.

## Files
| Path | Role |
|------|------|
| `src/video-tiler.py` | Everything: the `App` (Tkinter GUI) and the `Player` (playback engine). |
| `src/monitor_utils.py` | Pure, GUI-free monitor detection + per-monitor tile filter + window options. Unit-tested. |
| `src/winkiosk.py` | Windows kiosk hardening (ctypes-only, GUI-free, no-op off Windows): Per-Monitor-v2 DPI awareness, SDL DPI env for ffplay, window-rect enforcement, keep-awake, single-instance mutex. Unit-tested. |
| `tests/` | Deterministic tests: geometry (`test_monitor_utils`), format selection (`test_format_select`), engine/threading/state + ffplay flags + stall watchdog (`test_engine`), fan-out copy/retire/teardown (`test_fanout`), per-window relaunch + escalation (`test_relaunch`), settings round-trip/atomicity/no-fsync (`test_settings`), high-volume fan-out + start/teardown leak (`test_stress`), Windows kiosk helpers (`test_winkiosk`, self-skips off Windows), real Tk GUI smoke (`test_frontend`, self-skips headless), real ffplay flag acceptance (`test_ffplay_flags`, self-skips without ffmpeg). Plus a manual real-stream playback smoke (`smoke_live_playback.py`, not in the suite). |
| `make_version_info.py` | Generates the Windows exe version resource from `VERSION` (so the build is version-stamped, not blank → fewer SmartScreen/AV blocks). |
| `requirements.txt` | Pure cross-platform deps (`yt-dlp`, `screeninfo`, `psutil`, `appdirs`). |
| `run.bat` / `compile_windows.bat` | Run from source / build a Windows exe. |
| `platform/macos/` | macOS installer, Gatekeeper unblock helper, README (groundwork). |
| `VERSION` | Plain version string the update check reads. |

## Playback engine (`Player`)
One live stream is downloaded **once** and shown as an N×N grid of **identical**
tiles. The identical-tiles trick is `fps=source_fps*N²,tile=NxN` (duplicate each
frame N² times, then tile) — done by `monitor_utils.tile_filter_for`.

- **Every window is buffered (even a single one).** Each selected monitor gets
  its own `ffplay`, fed by a reader thread (`_fanout`) plus **one writer thread
  per window**, each drawing from a bounded ring queue (`FANOUT_QUEUE_MAX` × 64
  KiB ≈ 8 MiB, ~14 s at a typical bitrate). The single-monitor case is **not** a
  special direct `ytdlp.stdout → ffplay.stdin` pipe: that wired ffplay to a
  ~4 KiB OS pipe with no slack and no wedge detection, so any render hiccup
  back-pressured the live download (drift) with no safety valve (freeze). The
  ring queue — not an ffplay flag — *is* the playback buffer: it absorbs bursty
  HLS segments and brief CPU hiccups, and a "running but not reading" (wedged)
  ffplay still fills it. Bytes are **never dropped** (that corrupts the
  container); a window whose queue stays full for `FANOUT_PUT_TIMEOUT` is
  **retired** instead. Only the first window keeps audio (others `-an`).
- **ffplay is tuned for a CPU-bound live wall.** `-threads 0` (auto): ffplay
  decodes single-threaded by default and software H.264 decode is the dominant
  cost, so this is the biggest win on a weak multi-core laptop. `-framedrop`:
  under CPU pressure drop *late* frames so a slow window snaps back to the live
  edge instead of drifting forever — it drops at the decoder (post-demux), so
  the byte container stays intact. Placement: bare `-fs` only when the lone
  target is the primary screen at the origin; any other single screen is a
  **placed** borderless window (else `-fs` lands on the wrong monitor).
- **Per-window self-heal (no relaunch storm).** A single fallen/retired window
  is rebuilt **on its own** (`_relaunch_consumer`) — the healthy screens never
  blink. The old all-or-nothing rule (one slow screen → tear the whole wall down
  → backoff → relaunch → repeat) is what turned a merely-slow laptop into a
  perpetual black/flicker loop. A full teardown + reconnect happens only if the
  **download** dies, **every** window dies, or one window keeps dying
  (`MAX_WINDOW_RESTARTS` within `RESTART_WINDOW` → escalate).
- **Windows spawn on the FIRST byte, not at launch.** `_start` launches only
  yt-dlp; the ffplay windows are built by the fan-out thread the moment the
  first chunk of real data arrives (`_spawn_windows`). A launch that never
  yields data (internet down, dead URL) costs one process and **zero windows**
  — no black fullscreen wall flashing on every retry, and an order of
  magnitude less process churn per failed attempt.
- **Liveness + stall watchdog.** We own the `Popen` handles, so the session is
  alive while the download runs **and at least one** window plays. A retired
  window trips its `dead` flag immediately (no waiting on a wedged ffplay to
  notice EOF). Because poll() cannot see a *silent* freeze (yt-dlp and its hidden
  HLS `ffmpeg` still running but no bytes flowing), a **stall watchdog** treats
  no data for `STALL_TIMEOUT` (30 s, comfortably past the segment cadence) as a
  drop and reconnects. Before the first byte the generous
  `STARTUP_STALL_TIMEOUT` (90 s) applies instead — extraction on a slow laptop
  + slow link can legitimately exceed 30 s, and tripping there produced a
  reconnect-forever loop.
- **Reconnect + connectivity gate.** On any drop, `run()` tears down, then
  retries with **exponential backoff** (3s→30s, `next_backoff`, jittered so
  thousands of cycles never phase-lock with AV scans). A session that lasted
  ≥60s resets the backoff and failure counters. But first, a **connectivity
  probe** (`internet_ok`: one TCP SYN to the stream host, falling back to
  well-known IPs so it works while DNS is down): while the internet is OUT the
  loop only re-probes every ~30 s — it does **not** launch yt-dlp, spawn
  windows, run psutil kill-scans, or count failures. Hours of outage cost
  almost nothing (this churn is what wore weak laptops into a freeze), and
  playback resumes within one probe interval of connectivity returning.
  `run()`'s body is wrapped in try/except so a failed launch (a tool that
  vanished mid-run, a monitor enumeration glitch, an exe-swap during self-heal)
  falls through to reconnect instead of killing the worker; a second outer
  guard makes even an "impossible" crash end the session cleanly (and the App
  watchdog restarts playback). Tool paths are re-resolved on every start.
- **Offline fallback video.** If `find_offline_video()` finds `offline.mp4`
  next to the exe/script (or in the repo's `assets/`), the connectivity gate
  plays it on a **looping** (`-loop 0`), muted-per-window-policy `ffplay` per
  target monitor instead of sitting on a blank wall — so passers-by see
  content, not a dead screen, during an outage. Because the wall isn't blank,
  there's no rush: connectivity is re-probed only every
  `OFFLINE_FALLBACK_PROBE_INTERVAL` (~3 min, vs ~30 s for the blank-wait path).
  A fallback window that dies on its own is relaunched in place. As soon as
  `internet_ok` succeeds, every fallback window is killed and the normal
  reconnect resumes. No `offline.mp4` present → falls straight back to the
  original blank probing wait.
- **Measured CPU-pressure quality step-down (Auto only).** Windows that
  repeatedly wedge (queue full → retired) or a relaunch storm are *evidence*
  this machine cannot decode the current resolution. Each such session bumps
  `_cpu_pressure` (bounded), and `select_format` steps the **Auto** height one
  rung down per level for the next session — a weak laptop settles at a rate it
  can sustain instead of stuttering/looping; a manual quality choice is never
  overridden (it is only logged).
- **Offline state.** After many sub-healthy failures the status escalates to an
  explicit "Stream appears offline" instead of a forever-"Reconnecting" flicker,
  and "Playing" is only shown once a session has survived a couple of seconds.
- **Self-heal.** After 2 consecutive quick failures **while online** (the
  connectivity gate means it never runs during an outage — an offline `-U` is
  useless churn and, interrupted, can corrupt the binary, after which playback
  never works again even when the net returns) it runs `yt-dlp -U`, and
  **re-arms periodically** (every N failures) so a fix shipped days into an
  outage is still picked up — not healed only once ever. On a pip/console-script
  install (where `-U` is a no-op) it detects that and updates via
  `python -m pip install --isolated --upgrade yt-dlp`; `--isolated` ignores
  `PIP_INDEX_URL`/`pip.ini` so this unattended, recurring kiosk upgrade cannot be
  redirected to a hostile index. Updates are **single-flight** (a lock guards
  `update_yt_dlp`) so the Tools menu and the auto self-heal can never run two
  concurrent `-U` that race-corrupt the on-disk binary. The return code is
  checked and logged.
- **Robust extraction.** yt-dlp is invoked with multiple fallback player
  clients (`default,android,tv,ios`), `--retries`, `--socket-timeout`, and a
  height-based `-f` selector that always ends in `/best`. The URL is passed
  after a `--` end-of-options marker and only `http(s)` URLs are accepted, so a
  value starting with `-` can never be parsed as a yt-dlp option (injection).
  yt-dlp's stderr is captured into a small ring buffer and the last lines are
  logged when a session drops, so the log can say *why* it broke.

## Kiosk hardening (`winkiosk`, Windows; safe no-ops elsewhere)
An unattended video wall on a random laptop fails in ways a desktop app never
sees. `src/winkiosk.py` (ctypes-only, GUI-free, tested by `test_winkiosk`)
closes the field-reported ones:

- **Sleep is the #1 "freeze".** With the wall down (offline retry loop) nothing
  inhibits Windows' idle timers, so after a few hours the machine **sleeps** —
  which reads as "the system froze and never recovered, even when the internet
  came back". While playback is wanted, the app holds
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED |
  ES_DISPLAY_REQUIRED)` (`keep_awake`, called on the long-lived Tk thread; the
  state is per-thread). Released on Stop/close; an explicit lid-close/power
  button still wins, as it should.
- **Exact fullscreen on scaled monitors.** The process opts into
  **Per-Monitor-v2 DPI awareness** *before* Tk/monitor enumeration
  (`set_dpi_awareness`), so `screeninfo` coordinates are true physical pixels;
  ffplay children get `SDL_WINDOWS_DPI_AWARENESS=permonitorv2` (honoured by
  SDL ≥ 2.24) so they interpret that geometry 1:1; placed windows are sized to
  the monitor's **exact** width/height (not the tile-floored filter size, which
  could leave a few-px desktop sliver); and a bounded **rect enforcer**
  (`enforce_window_rect`, per spawned window, aborts when the window dies)
  verifies the result and `SetWindowPos`-forces it if an older SDL ignored the
  hint. Together these fix "sometimes a monitor is not quite fullscreen" on
  mixed 125%/150% multi-monitor setups.
- **Single instance.** A named mutex (`acquire_single_instance`) stops
  run-at-startup plus a manual double-click from racing two walls (double
  decode load + two players fighting over fullscreen reads as a freeze).
- **App watchdog + health telemetry.** Every 60 s the GUI checks that the
  worker thread is still alive while playback is wanted and restarts it if not
  (belt-and-braces; `run()` is already double-guarded). Every 10 min it logs
  RSS / thread count / handle count, so a field "it froze after N hours" report
  comes with evidence instead of guesswork.
- **No modals on unattended paths.** The quiet launch update-check reports via
  the status bar + log (a modal inside the UI pump would stall every status
  update forever on a kiosk); autoplay with missing tools retries every 30 s
  instead of parking on an error box nobody will dismiss.

## Concurrency
`Player` state (`ytdlp_process`, `ffplay_processes`, the consumer list and
fan-out threads) is shared between the GUI thread (Stop) and the worker thread
(`run`/`_start`), so every mutation/read goes through an `RLock`. `_terminate`
does the fast steps (signal, kill yt-dlp, close pipes, kill ffplay) under the
lock and the **blocking thread joins outside** it. `stop()` runs the **whole
teardown off the Tk main thread** (a short daemon thread): the kills use psutil
`children(recursive=True)`, which is *not* free on Windows (≈one OS process scan
per handle), so doing them on the GUI thread hitched every Stop/Play/close. Only
`on_closing` passes `join=True` (a blocking teardown) so the process does not
exit leaving orphaned `ffplay`/`yt-dlp`. Threads a non-blocking Stop could not
join are parked in `_pending_joins` for the worker's own terminal
`_terminate(join=True)` to drain, so the bounded-lifetime guarantee survives a
Stop. The UI pump (`_pump_ui_queue`) cancels its pending `after` on close so no
callback fires against a destroyed window.

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
`settings.json` in the per-user data dir, loaded on launch. Writes are
**atomic** (`tmp` + `os.replace`) so a crash mid-write can't truncate the file,
and a parse failure is logged (not silently swallowed). There is **no `os.fsync`**
on this path — it runs on the Tk main thread on every checkbox toggle and before
Play, and a synchronous disk barrier can stall the GUI for hundreds of ms on a
slow/SMR laptop disk; `os.replace` already gives crash-atomic visibility, and a
kiosk can re-derive the last few unsynced settings. Only `http(s)` URLs are
trusted from the file. The **monitor selection** is stored as the user's
*desired* set (`_desired_monitor_indices`, unfiltered) separately from the
runtime-resolved set, so running a docked kiosk laptop **undocked** (fewer
screens) no longer silently overwrites the saved wall with the one attached
screen — re-docking restores it. `PROGRAM_VERSION` is read from the bundled
`VERSION` file (single source of truth for the in-app version, the update check,
the macOS bundle, and the Windows exe version resource generated by
`make_version_info.py`).

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
