# Video Tiler on macOS

> Status: **groundwork, not yet validated on a real Mac.** The code is
> cross-platform Python (Tkinter + yt-dlp + ffmpeg/ffplay) and these scripts
> follow the same approach used for the Whisper project, but no one has run
> them on macOS yet - treat as beta and report back.

The app runs from source on macOS. It is **unsigned** (no paid Apple Developer
certificate), so macOS Gatekeeper needs a one-time nudge - see below.

## Requirements

- **Python 3.11+ with a good Tk.** Easiest: the official installer from
  <https://www.python.org/downloads/macos/> (bundles Tk 8.6). With Homebrew:
  `brew install python python-tk`. Do **not** rely on Apple's built-in
  `python3` for the GUI - it links the deprecated Tk 8.5 and renders a
  blurry window (the installer detects this and warns).
- **ffmpeg *and* ffplay.** The tiler *plays* video, so it needs `ffplay`,
  which is included in Homebrew's ffmpeg: `brew install ffmpeg`. (The static
  evermeet.cx builds ship ffmpeg/ffprobe but **not** ffplay, so Homebrew is
  the reliable route on macOS.)

## Install

Get the repo **via Terminal** (this matters for Gatekeeper - see below),
then run the installer:

```bash
git clone https://github.com/Milomilo777/video-tiler.git
cd video-tiler
bash platform/macos/install.command
```

The installer makes a `.venv`, installs the deps + `yt-dlp`, ensures
ffmpeg/ffplay (via Homebrew), and creates a double-clickable
`~/Applications/Video Tiler.app` (built locally, so it isn't quarantined).

You can also just run it directly:

```bash
.venv/bin/python src/video-tiler.py
```

## Gatekeeper (unsigned app) - why and how

macOS tags files **downloaded by a browser** with a `com.apple.quarantine`
flag; Gatekeeper then blocks unsigned apps. Cleanest first:

1. **Get the code without quarantine.** Files fetched via `git clone` or
   `curl` are *not* quarantined, so the launchers just work.
2. **Strip the flag** (if you downloaded a zip in a browser):
   ```bash
   xattr -dr com.apple.quarantine /path/to/video-tiler
   ```
   or double-click `platform/macos/unblock.command`.
3. **Open Anyway:** try to open it, dismiss the warning, then
   **System Settings > Privacy & Security > "Open Anyway"**.

Do **not** disable Gatekeeper globally (`spctl --master-disable`) - it is an
unnecessary system-wide security downgrade.

## What still needs a real Mac

- Playback liveness is read from the subprocess handles (`Popen.poll()`), which
  is fully cross-platform - there is no Windows-only code path on macOS anymore.
- ffplay window placement for multi-monitor uses `-left/-top/-noborder`;
  behaviour across macOS Spaces/displays should be verified.
- "Run at Windows startup" is a no-op on macOS (use a LaunchAgent instead); the
  toggle simply reports it could not be set.
