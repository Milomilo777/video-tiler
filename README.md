# Video Tiler

[![Latest release](https://img.shields.io/github/v/release/Milomilo777/video-tiler?label=release)](https://github.com/Milomilo777/video-tiler/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Milomilo777/video-tiler/total)](https://github.com/Milomilo777/video-tiler/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS*-lightgrey)](#macos)

## _A video tiler designed for www.suprememastertv.com_

> **AI agents / new contributors:** read [`PROJECT_INDEX.md`](PROJECT_INDEX.md)
> first — a one-page map of the repo (subsystems, entry points, gotchas) that
> saves a full re-scan.

## _Why a video tiler?_
Multiply the benefits of the Supreme Master TV live stream:
https://suprememastertv.com/en1/v/245875177398.html

One live stream is downloaded **once** and shown as an N×N grid of **identical**
tiles, optionally across **multiple monitors** (e.g. a wall of TVs via an HDMI
hub). From a core i7, tiling can go up to 64×64 tiles.

## Features
* Graphical interface to choose any video URL, the number of tiles, and auto-restart
* Built-in list of SMTV streams (YouTube, X, etc.) and it remembers URLs you add
* **Identical synchronized tiles** from a **single download** (light `fps*N²` method)
* **Multi-monitor**: tile across all or selected screens (e.g. 2 of 3); one download
  is fanned out to one player window per monitor (a single spanning window is
  unreliable, so this uses one `ffplay` per screen)
* Manual **quality** selector, **mute**, **auto-restart**, and **kiosk** options
  (auto-play on launch, run at Windows startup)
* **Light / Dark theme** (your choice) and **all settings are remembered**
* Keyboard shortcuts: `Esc` = stop, `F5` = play, `Space` = play/stop
* **Self-updating**: `Tools > Update yt-dlp`, an automatic yt-dlp update after
  repeated playback failures, and a non-intrusive "new version available" check
* **Kiosk-grade "never freeze" hardening** (v1.2):
  * While the internet is down, a cheap connectivity probe replaces the
    reconnect churn — nothing is spawned, and playback resumes within ~30 s of
    the connection returning (even after hours offline)
  * The machine is kept **awake** while a show is wanted (an unattended kiosk
    that idled into Windows sleep looked "frozen forever")
  * **Exact fullscreen on every monitor**, including mixed 125%/150% DPI
    scaling (Per-Monitor-v2 awareness + a per-window geometry enforcer)
  * Player windows open only once real data arrives (no black fullscreen
    flashes on failed retries); a watchdog restarts a dead playback worker
  * On weak CPUs, **Auto** quality steps down automatically when the machine
    measurably cannot keep up (a manual quality choice is never overridden)
  * Single-instance guard: run-at-startup plus a double-click can't run two
    competing walls
  * **Offline fallback video**: if `assets/offline.mp4` (or `offline.mp4` next
    to the exe) is present, it plays on a loop across every monitor while the
    internet is down instead of a blank wall, checked back only every ~3 min
* Cross-platform code (Windows tested; macOS/Linux best-effort)

## Requirements
* Python 3.11+ (with Tk for the GUI)
* `yt-dlp`, `ffmpeg`, and `ffplay` available on `PATH` (or next to `src/`, or in a
  sibling `bin/` folder). On macOS, `brew install ffmpeg` provides all three.
* Python packages: see `requirements.txt`

## Run from source
```bash
pip install -r requirements.txt
python src/video-tiler.py        # or double-click run.bat on Windows
```

## Build a Windows executable
```bat
compile_windows.bat
```
This installs the requirements + PyInstaller and produces a standalone `.exe`
in `dist\` (icon: `src\img\app.ico`), and copies `assets\offline.mp4` next to
it. Place `yt-dlp.exe`, `ffmpeg.exe` and `ffplay.exe` on `PATH` or next to the
executable.

### Offline fallback video
`assets/offline.mp4` is what plays, on a loop, while the internet is down (see
Features above). This file's content is kept in sync by hand from Supreme
Master TV's own feed:
https://suprememastertv.com/en1/max/
To change it, replace `assets/offline.mp4` with an updated export from that
page and rebuild (or drop a new `offline.mp4` next to an already-built exe).

## macOS
See [`platform/macos/`](platform/macos/) for the installer, a double-clickable
app bundle, and a Gatekeeper unblock helper. (Groundwork - not yet validated on
a real Mac.)

## How to use
1. Pick or paste a video URL and set the grid size (e.g. `5` → a 5×5 grid).
2. Press **Play** (or Enter). Every tile shows the same live frame.
3. The player opens full screen - press `Esc`/`q` on it, or Alt+Tab back and
   press **Stop**.
4. For several screens: tick **Multi-monitor**, then **Monitors…** to choose
   which ones (the **Identify** button flashes each monitor's number).

### Stopping a multi-monitor wall
The per-screen players are borderless and on top, so they cover this window.
To stop: click **Video Tiler** on the taskbar (or Alt+Tab to it), then press
**Esc** or **Stop**. Closing a single player window does **not** stop playback —
with **Auto Restart** on (the default) it just relaunches the whole wall; turn
**Auto Restart** off first if you want closing a window to end playback.

## Logs / troubleshooting
Activity (start, drops, reconnect/backoff, self-heal, stop, and the last lines
of yt-dlp's own error output) is written to a rotating `videotiler.log`:

* **Windows:** `%LOCALAPPDATA%\videotiler\videotiler\videotiler.log`
* **macOS:** `~/Library/Application Support/videotiler/videotiler.log`
* **Fallback (no `appdirs`):** `~/.videotiler/videotiler.log`

The exact path is also shown in **About → Help**.

## Architecture
See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design (playback engine,
threading model, reconnect/self-heal, settings).

## Problems
* The PyInstaller build can be falsely flagged as a virus by some antivirus
  engines despite containing none.

## Download Windows binaries
[**Latest release**](https://github.com/Milomilo777/video-tiler/releases/latest) —
grab the all-in-one `.zip` (exe + ffmpeg + ffplay + yt-dlp + offline fallback
video, nothing else to install) or the standalone `.exe` alone.

Mirror: https://1drv.ms/u/c/25c35a16b8db8a90/EdKeHDg5-cxHvJYThwMSF5EBTtZlF8aWVQVJhSDBnC0LGw?e=BVbqYh

## Contributing
Bug reports and feature requests are welcome via
[Issues](https://github.com/Milomilo777/video-tiler/issues) — templates guide
you through the useful details (version, OS, log lines). Pull requests are
welcome too; [`ARCHITECTURE.md`](ARCHITECTURE.md) and
[`PROJECT_INDEX.md`](PROJECT_INDEX.md) are the fastest way to get oriented
before changing the playback engine.

## Acknowledgments
* Original project concept and first version: the
  [translation-robot](https://github.com/translation-robot) team, built for
  Supreme Master TV's live stream.
* The v1.2/v1.3 reliability rewrite (kiosk hardening, self-heal, offline
  fallback) and this repo's documentation were built with
  [Claude](https://claude.com/claude-code) (Anthropic) as an AI pair
  programmer.
