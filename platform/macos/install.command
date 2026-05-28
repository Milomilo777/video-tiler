#!/usr/bin/env bash
# Video Tiler - macOS installer (double-clickable .command).
#
# Makes a self-contained virtualenv next to the repo, installs the deps +
# yt-dlp, ensures ffmpeg/ffplay are present, and creates a double-clickable
# "Video Tiler.app" in ~/Applications.
#
# Gatekeeper: this app is UNSIGNED (no paid Apple Developer cert). The cleanest
# way to avoid Gatekeeper is to get the repo via `git clone` / `curl` and run
# this from Terminal - files fetched that way are NOT quarantined. As a
# belt-and-braces step this script also strips the quarantine flag. See
# README.md for the full story.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [ ! -f "$REPO_ROOT/src/video-tiler.py" ]; then
  echo "error: src/video-tiler.py not found at $REPO_ROOT - run this from inside the repo checkout." >&2
  exit 1
fi
VENV="$REPO_ROOT/.venv"
APPS_DIR="$HOME/Applications"

say()  { printf '\033[1;36m[video-tiler]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[video-tiler] WARN:\033[0m %s\n' "$*" >&2; }

# ---- de-quarantine the repo so Gatekeeper doesn't block our own scripts ----
xattr -dr com.apple.quarantine "$REPO_ROOT" 2>/dev/null || true

# ---- python (the GUI needs a GOOD Tk; Apple's system Tk 8.5 renders badly) -
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in /usr/local/bin/python3 /opt/homebrew/bin/python3 python3; do
    if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
  done
fi
if [ -z "$PY" ] || ! command -v "$PY" >/dev/null 2>&1; then
  echo "error: python3 not found. Install Python 3.11+ from https://www.python.org/downloads/macos/ (its installer bundles a good Tk)." >&2
  exit 1
fi
say "using Python $("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])') at $("$PY" -c 'import sys;print(sys.executable)')"
TKVER="$("$PY" -c 'import tkinter;print(tkinter.TkVersion)' 2>/dev/null || echo none)"
if [ "$TKVER" = "none" ]; then
  warn "tkinter missing - the GUI won't start. Use the python.org build or 'brew install python-tk'."
elif [ "$TKVER" = "8.5" ]; then
  warn "this Python links Tk 8.5 (Apple's deprecated build) - the GUI will look blurry."
  warn "Prefer the python.org Python (Tk 8.6): https://www.python.org/downloads/macos/"
  warn "then: PYTHON=/usr/local/bin/python3 bash platform/macos/install.command"
else
  say "Tk $TKVER OK"
fi

# ---- venv + deps (requirements.txt skips the Windows-only packages here) ----
say "creating virtualenv at $VENV"
rm -rf "$VENV"
"$PY" -m venv "$VENV"
# shellcheck disable=SC1091
. "$VENV/bin/activate"
python -m pip install --upgrade pip wheel >/dev/null
say "installing dependencies (a few minutes)..."
python -m pip install -r "$REPO_ROOT/requirements.txt"
python -m pip install --upgrade yt-dlp
deactivate

# ---- ffmpeg + ffplay (the tiler PLAYS video, so it needs ffplay too) -------
# Note: brew's ffmpeg ships ffplay; the static evermeet builds do NOT, so on
# macOS Homebrew is the reliable way to get ffplay.
if command -v ffplay >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1; then
  say "system ffmpeg + ffplay found: $(command -v ffplay)"
elif command -v brew >/dev/null 2>&1; then
  say "installing ffmpeg (includes ffplay) via Homebrew..."
  brew install ffmpeg || warn "brew install ffmpeg failed - install it manually."
else
  warn "ffplay not found and no Homebrew. Install Homebrew (https://brew.sh) then:"
  warn "    brew install ffmpeg     # provides ffmpeg, ffprobe AND ffplay"
  warn "Without ffplay the tiler cannot display video."
fi

# ---- launcher: a real .app bundle (no lingering Terminal window) -----------
# Built locally, so it carries no quarantine flag and opens without a prompt.
mkdir -p "$APPS_DIR"
APP="$APPS_DIR/Video Tiler.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cat > "$APP/Contents/MacOS/video-tiler" <<EOF
#!/bin/bash
# Put Homebrew + the venv on PATH so yt-dlp/ffmpeg/ffplay resolve.
export PATH="/opt/homebrew/bin:/usr/local/bin:$VENV/bin:\$PATH"
exec "$VENV/bin/python" "$REPO_ROOT/src/video-tiler.py"
EOF
chmod +x "$APP/Contents/MacOS/video-tiler"
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Video Tiler</string>
  <key>CFBundleDisplayName</key><string>Video Tiler</string>
  <key>CFBundleIdentifier</key><string>com.suprememastertv.videotiler</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>video-tiler</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
EOF
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

say "done. Launch it from ~/Applications/Video Tiler.app"
say "(or run directly:  \"$VENV/bin/python\" \"$REPO_ROOT/src/video-tiler.py\")"
