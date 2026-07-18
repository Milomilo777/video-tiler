"""Video Tiler - a Tkinter GUI that plays one live stream as an N x N grid of
identical tiles, optionally across several monitors, for www.suprememastertv.com.

Design highlights (see ARCHITECTURE.md):
  * Playback liveness is read directly from the subprocess handles we own
    (Popen.poll()), not by scanning the OS process tree or hunting windows -
    so it is cheap, reliable, and cross-platform.
  * Worker threads never touch Tkinter; they post callables onto a queue that
    the main loop drains. This is the only thread-safe way to update the GUI.
  * Reconnect uses exponential backoff and self-heals (updates yt-dlp) after
    repeated failures - the usual cause of YouTube breakage.
"""

import os
import sys
import json
import time
import queue
import random
import shutil
import socket
import logging
import threading
import subprocess
import webbrowser
import collections
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont

import yt_dlp
import psutil

# monitor_utils / winkiosk sit next to this file; ensure the folder is importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor_utils
import winkiosk


# --------------------------------------------------------------------------- #
#  Constants
# --------------------------------------------------------------------------- #
def _read_version(default="1.1"):
    """Single source of truth: read the VERSION file shipped next to the code
    (or bundled into a PyInstaller build) so the in-app version, the update
    check, and the packaged build never drift apart."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(base, 'VERSION'), os.path.join(os.path.dirname(base), 'VERSION')):
        try:
            with open(cand, 'r', encoding='utf-8') as f:
                v = f.read().strip()
            if v:
                return v
        except Exception:
            pass
    return default


APP_NAME = "videotiler"
PROGRAM_VERSION = _read_version()
PROGRAM_AUTHOR = "Bluesun"
AUTHOR_EMAIL = "smtv.bot@gmail.com"
AUTHOR_WEBSITE = "https://github.com/translation-robot/video-tiler"

DEFAULT_URL = "https://www.youtube.com/watch?v=ZzWBpGwKoaI"
WHY_TILING_URL = "https://suprememastertv.com/en1/v/245875177398.html"
SUPPORTED_WEB_SITES = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"
SOURCE_CODE_GITHUB = "https://github.com/Milomilo777/video-tiler"
UPDATE_VERSION_URL = "https://raw.githubusercontent.com/Milomilo777/video-tiler/master/VERSION"
RELEASES_URL = "https://github.com/Milomilo777/video-tiler/releases"

# Robust YouTube extraction: try several player clients so one breaking does not
# break playback. Ignored by non-YouTube extractors, so it is safe to always pass.
YT_PLAYER_CLIENTS = "default,android,tv,ios"

QUALITY_CHOICES = ['Auto', '1080p', '720p', '480p', '360p', '240p', '144p']
THEME_CHOICES = ['Light', 'Dark']

DEFAULT_SETTINGS = {
    'url': DEFAULT_URL,
    'urls': [DEFAULT_URL, "https://x.com/i/broadcasts/1LyxBgjebwOKN"],
    'divisions': 3,
    'auto_restart': True,
    'multi_monitor': False,
    'selected_monitor_indices': [],
    'mute': False,
    'quality': 'Auto',
    'autoplay': False,
    'run_at_startup': False,
    'theme': 'Light',
}

# Windows "run at login" registry location (per-user, reversible).
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "SMTV_VideoTiler"

APP_DATA_DIR = None
SETTINGS_FILE = None
LOG_FILE = None

# On Windows, keep helper processes (yt-dlp / ffmpeg / ffplay) from flashing a
# console window - important for an unattended kiosk. No effect on other OSes.
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

log = logging.getLogger(APP_NAME)
log.addHandler(logging.NullHandler())


# --------------------------------------------------------------------------- #
#  Executable / path discovery
# --------------------------------------------------------------------------- #
def add_to_path():
    """Make the script/exe directory (and a sibling 'bin') discoverable."""
    base = os.path.dirname(os.path.abspath(sys.argv[0]))
    for d in (base, os.path.join(base, 'bin')):
        if os.path.isdir(d) and d not in os.environ.get('PATH', ''):
            os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')


def find_executable(name):
    """Locate yt-dlp / ffmpeg / ffplay on PATH, next to this file, or in ./bin.

    Deliberately NOT cached: tool paths are re-resolved on each playback start
    so a transient absence (e.g. a sibling bin folder on a network share that
    briefly drops, or yt-dlp.exe mid self-update) self-recovers on reconnect.
    """
    found = shutil.which(name)
    if found:
        return found
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(base, name), os.path.join(base, 'bin', name)):
        # On Windows shutil.which already adds .exe; here also try the bare path.
        for p in (cand, cand + '.exe'):
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


OFFLINE_VIDEO_FILENAME = 'offline.mp4'


def find_offline_video():
    """Path to the local fallback video played while the internet is down.
    Checked next to the exe/script (same sibling-file convention as
    yt-dlp.exe/ffmpeg.exe/ffplay.exe - where compile_windows.bat copies it for
    a packaged build), then in the repo's assets/ folder (so a source run
    finds it too). None if absent, in which case offline playback just falls
    back to the blank probing wait it always had."""
    candidates = [
        os.path.dirname(os.path.abspath(sys.argv[0])),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'assets'),
    ]
    for base in candidates:
        p = os.path.join(base, OFFLINE_VIDEO_FILENAME)
        if os.path.isfile(p):
            return p
    return None


def is_valid_stream_url(url):
    """Only accept http(s) URLs. This guards yt-dlp against argument injection
    (a value starting with '-' would otherwise be read as an option such as
    --exec / --config-location) and rejects junk from a tampered settings file."""
    if not isinstance(url, str):
        return False
    u = url.strip()
    return u.lower().startswith(("http://", "https://"))


def _looks_like_pip_ytdlp(update_output):
    """True if `yt-dlp -U` output indicates a pip/package-manager/source install
    (which -U cannot self-update). yt-dlp prints a message telling the user to
    update via their installer; we match its specific phrases. Deliberately NOT
    a bare 'pip' substring (which would false-match e.g. 'broken pipe')."""
    s = (update_output or "").lower()
    needles = ("with pip", "via pip", "use pip", "pip or your package manager",
               "package manager", "tarball", "setup.py", "you installed",
               "use that to update", "not a self-contained")
    return any(n in s for n in needles)


# --------------------------------------------------------------------------- #
#  Settings persistence (single JSON; remembers every GUI choice)
# --------------------------------------------------------------------------- #
def _init_paths():
    global APP_DATA_DIR, SETTINGS_FILE, LOG_FILE
    try:
        import appdirs
        APP_DATA_DIR = appdirs.user_data_dir(APP_NAME)
    except Exception:
        APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".videotiler")
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    SETTINGS_FILE = os.path.join(APP_DATA_DIR, 'settings.json')
    LOG_FILE = os.path.join(APP_DATA_DIR, 'videotiler.log')
    _init_logging()


def _init_logging():
    """A small rotating log in the data dir - the only window into what an
    unattended kiosk did when no console is attached (windowed/frozen builds).
    If the file handler can't be created (locked by a second instance, read-only
    data dir), fall back to stderr and say so, rather than running blind."""
    if log.handlers and any(not isinstance(h, logging.NullHandler) for h in log.handlers):
        return
    log.setLevel(logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s', '%Y-%m-%d %H:%M:%S')
    try:
        handler = RotatingFileHandler(
            LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding='utf-8')
        handler.setFormatter(fmt)
        log.addHandler(handler)
    except Exception as e:
        try:
            sh = logging.StreamHandler()
            sh.setFormatter(fmt)
            log.addHandler(sh)
            log.warning("could not open log file %s (%s); logging to stderr only", LOG_FILE, e)
        except Exception:
            pass
    log.info("=== Video Tiler %s starting (pid %s) ===", PROGRAM_VERSION, os.getpid())


def read_settings():
    data = dict(DEFAULT_SETTINGS)
    if not SETTINGS_FILE or not os.path.exists(SETTINGS_FILE):
        return data
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            data.update(saved)
        else:
            log.warning("settings file is not a JSON object; using defaults: %s", SETTINGS_FILE)
    except Exception as e:
        # Don't silently lose all config: say so, and keep the bad file for a
        # human to inspect rather than overwriting it blindly.
        log.warning("could not read settings (%s); using defaults: %s", e, SETTINGS_FILE)
    return data


def write_settings(data):
    """Write atomically (tmp + os.replace) so a crash mid-write can never leave a
    truncated settings.json that wipes the kiosk's config. os.replace is atomic
    on Windows/POSIX, so a reader always sees the whole old OR the whole new file.

    Deliberately NO os.fsync here: this runs on the Tk main thread on every
    checkbox toggle and right before Play, and a synchronous disk barrier can
    stall the GUI for hundreds of ms on a slow/SMR laptop disk under AV load. A
    kiosk can re-derive the last few unsynced settings after a power loss; that
    is a far better trade than freezing the UI on every interaction."""
    if not SETTINGS_FILE:
        return
    tmp = SETTINGS_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        log.warning("could not write settings (%s): %s", e, SETTINGS_FILE)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Windows "run at startup" (optional, reversible, per-user)
# --------------------------------------------------------------------------- #
def _startup_command():
    """Command Windows runs at login - the current interpreter/exe, so the
    startup launch keeps the same working environment (venv or frozen build).
    Prefers pythonw.exe over python.exe so the kiosk does not flash a console
    window at every login."""
    if getattr(sys, 'frozen', False):
        return '"{}"'.format(sys.executable)
    interpreter = sys.executable
    pythonw = os.path.join(os.path.dirname(interpreter), 'pythonw.exe')
    if os.path.isfile(pythonw):
        interpreter = pythonw
    return '"{}" "{}"'.format(interpreter, os.path.abspath(__file__))


def set_run_at_startup(enabled):
    """Add/remove this app from the current user's Windows startup."""
    try:
        import winreg
    except Exception:
        return False  # not Windows
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE)
        try:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, _startup_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(key)
        return True
    except Exception as e:
        log.warning("run-at-startup change failed: %s", e)
        return False


# --------------------------------------------------------------------------- #
#  Format selection (pure - no network probe)
# --------------------------------------------------------------------------- #
AUTO_QUALITY_RUNGS = [1080, 720, 480, 360, 240, 144]


def select_format(quality, divisions, cpu_pressure=0):
    """yt-dlp -f selector. Manual quality wins; Auto lowers resolution as the
    grid gets denser (a 50x50 tile needs far less than 1080p). Always ends in
    /best so playback never fails just because a resolution is unavailable.

    cpu_pressure (Auto only) steps the chosen height down one rung per level.
    It is raised by the engine ONLY on measured evidence that this machine
    cannot decode the current resolution (windows repeatedly wedging /
    relaunch storms) - so a weak laptop degrades to a resolution it can
    sustain instead of stuttering or freezing, while a capable machine keeps
    full quality. A manual choice is never overridden."""
    heights = {'1080p': 1080, '720p': 720, '480p': 480, '360p': 360, '240p': 240, '144p': 144}
    h = heights.get(quality)
    if h is None:  # Auto: sparse grids want full resolution (a single tile fills
        # the screen); only dense grids can drop resolution without looking soft.
        if divisions <= 2:
            h = 1080
        elif divisions <= 4:
            h = 720
        elif divisions <= 17:
            h = 360
        elif divisions <= 35:
            h = 240
        else:
            h = 144
        if cpu_pressure > 0:
            idx = AUTO_QUALITY_RUNGS.index(h) if h in AUTO_QUALITY_RUNGS else 0
            h = AUTO_QUALITY_RUNGS[min(idx + int(cpu_pressure), len(AUTO_QUALITY_RUNGS) - 1)]
    return "best[height<={h}]/best[height<={h2}]/best".format(h=h, h2=h + 360)


def next_backoff(prev, cap=30):
    """Exponential reconnect backoff, capped. Pure (so it is unit-testable)."""
    return min(prev * 2, cap)


def jittered(seconds, frac=0.25):
    """Add up to +frac random jitter to a wait, so thousands of retry cycles
    never phase-lock with AV scans / other periodic load on a weak laptop."""
    return seconds * (1.0 + random.uniform(0.0, frac))


def stream_host(url, default="www.youtube.com"):
    """Hostname of the stream URL (probe target), with a safe default."""
    try:
        host = urllib.parse.urlsplit(url).hostname
        return host or default
    except Exception:
        return default


def internet_ok(url, timeout=3.0):
    """Cheap connectivity probe: can we open a TCP connection to the stream's
    host (or a well-known fallback)? Used to keep the reconnect loop from
    churning the whole yt-dlp/ffplay pipeline for HOURS while the internet is
    down - one SYN every probe interval instead of spawning and killing a
    process tree, which is what wore weak laptops into a freeze. A false
    "online" (captive portal) is harmless: the real attempt fails and backs
    off exactly as before."""
    candidates = [
        (stream_host(url), 443),
        ("1.1.1.1", 443),   # plain IP: works even while DNS is still down
        ("8.8.8.8", 53),
    ]
    for host, port in candidates:
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            return True
        except OSError:
            continue
        except Exception:
            continue
    return False


def clamp_divisions(raw, lo=1, hi=64, default=3):
    """Clamp a grid-divisions value to [lo, hi]; non-integers fall back to
    default. Guards the ffmpeg tile=NxN filter from garbage input."""
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


def fetch_title_async(app, url, callback):
    """Fetch a stream's title off the GUI thread, then deliver it via post_ui.
    Lives at module level (not on Player) so the title probe doesn't build a
    throwaway Player or scan PATH for executables it never uses."""
    def worker():
        title = url
        try:
            if is_valid_stream_url(url):
                opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False, process=False)
                    title = (info or {}).get('title') or url
        except Exception:
            pass
        try:
            app.post_ui(lambda: callback(title))
        except Exception:
            pass
    threading.Thread(target=worker, daemon=True).start()


def _kill_tree(proc):
    """Kill a process and all its children (cross-platform via psutil)."""
    if not proc:
        return
    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
        parent.kill()
    except psutil.Error:
        try:
            proc.kill()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
#  Playback engine
# --------------------------------------------------------------------------- #
class Player:
    """Owns one download and the per-monitor ffplay windows it feeds.

    run() is the worker-thread entry point: it (re)starts the pipeline, watches
    the process handles, and reconnects with exponential backoff. It touches the
    GUI only through app.post_ui(). stop() tears everything down.
    """

    HEALTHY_SECONDS = 60    # a session this long resets backoff + self-heal
    HEAL_AFTER_FAILS = 2    # consecutive failures before we update yt-dlp
    REHEAL_EVERY = 20       # re-arm self-heal every N failures so a yt-dlp fix
                            # shipped days later is eventually picked up
    OFFLINE_AFTER_FAILS = 10  # surface an explicit "offline" status past this
    ANNOUNCE_AFTER = 2.0    # only show "Playing" once a session survives this long
    # Per-window ring buffer that feeds each ffplay. This queue - NOT an ffplay
    # flag - is the real playback buffer: deep enough (128 x 64 KiB = 8 MiB,
    # ~14 s at a 4.5 Mbit/s stream) to ride out a bursty HLS segment and a brief
    # CPU/render hiccup without back-pressuring the one download. Every monitor
    # (even a single one) is fed through it, so a "running but not reading"
    # (wedged) ffplay still fills the queue and gets retired - something poll()
    # alone can never see.
    FANOUT_QUEUE_MAX = 128
    FANOUT_PUT_TIMEOUT = 4.0   # a window whose queue stays full THIS long is wedged
    # Per-window self-heal: a single screen that dies/wedges is relaunched on its
    # own, WITHOUT blacking out the rest of the wall. Only if one window keeps
    # dying this many times within RESTART_WINDOW seconds do we give up on the
    # targeted relaunch and escalate to a full reconnect (backoff + self-heal).
    MAX_WINDOW_RESTARTS = 4
    RESTART_WINDOW = 30.0
    # Stall watchdog: if NO bytes arrive from the download for this long while
    # everything still *looks* alive (yt-dlp + its hidden ffmpeg still running,
    # ffplay still up), the pipeline has silently gone quiet - a freeze that
    # poll()-based liveness can never see. Treat it as a drop and reconnect.
    # Comfortably longer than the HLS segment cadence so a normal quiet gap
    # between segment bursts never false-trips it. Before the FIRST byte the
    # startup threshold applies instead: extraction alone can take >30s on a
    # slow laptop + slow link, and tripping at 30s there produced a
    # reconnect-forever loop that looked like "it never works".
    STALL_TIMEOUT = 30.0
    STARTUP_STALL_TIMEOUT = 90.0
    # While the internet is down we probe (one TCP SYN) instead of launching
    # the pipeline; this is how often. Cheap enough to keep small, so playback
    # resumes within ~half a minute of connectivity returning.
    OFFLINE_PROBE_INTERVAL = 30.0
    # While a local fallback video is covering the wall (see find_offline_video),
    # there's no rush - the screens aren't blank, so probe far less often to
    # avoid burning cycles on a laptop that may be offline for hours.
    OFFLINE_FALLBACK_PROBE_INTERVAL = 180.0
    # Measured CPU-overload learning (Auto quality only): if this many windows
    # were retired as wedged within one session, or a window relaunch-storm
    # escalated, the next session steps the Auto resolution down one rung.
    WEDGES_TO_PRESSURE = 2
    MAX_CPU_PRESSURE = 3
    # Verify/enforce each placed window's rectangle after spawn (Windows).
    # Class-level so tests that fake Popen can switch it off.
    ENFORCE_RECT = (os.name == 'nt')

    def __init__(self, app, url, divisions):
        self.app = app
        self.url = url
        self.divisions = max(1, int(divisions))
        self.title = url
        self.yt_dlp_path = find_executable('yt-dlp')
        self.ffmpeg_path = find_executable('ffmpeg')
        self.ffplay_path = find_executable('ffplay')
        self.tools_ok = bool(self.yt_dlp_path and self.ffmpeg_path and self.ffplay_path)
        self.play_flag = False
        # All of the following are touched from both the worker and the GUI
        # thread, so every read/mutation goes through self._lock (an RLock,
        # because _start() calls _terminate()).
        self._lock = threading.RLock()
        self.ytdlp_process = None
        self.ffplay_processes = []
        self._consumers = []        # multi-monitor: one {proc,q,thread,dead} each
        self._fanout_thread = None  # reader: yt-dlp stdout -> consumer queues
        self._fanout_stop = None    # threading.Event signalling the reader to stop
        self._stderr_thread = None  # drains yt-dlp stderr into _stderr_tail
        self._stderr_tail = None    # last lines of yt-dlp stderr (for diagnosis)
        # Threads a non-blocking (GUI) Stop could not join are parked here for the
        # worker's own terminal _terminate(join=True) to drain - so a Stop never
        # silently voids the bounded-lifetime guarantee.
        self._pending_joins = []
        self._last_progress = 0.0   # monotonic time of the last bytes read from yt-dlp
        self._got_first_data = False   # first byte seen this session (stall thresholds)
        self._windows_spawned = False  # ffplay windows exist (they spawn on first data)
        self._spawn_plan = None        # {'targets','single','muted'} for the deferred spawn
        self._wedge_retires = 0        # windows retired as wedged this session (CPU signal)
        self._cpu_pressure = 0         # sticky Auto-quality step-down (0..MAX_CPU_PRESSURE)
        self._fail_count = 0
        self._healed = False

    # ---- title (async; never blocks the GUI) ------------------------------ #
    def fetch_title_async(self, callback):
        def on_title(t):
            self.title = t
            callback(t)
        fetch_title_async(self.app, self.url, on_title)

    # ---- command building ------------------------------------------------- #
    def _yt_dlp_cmd(self):
        # Plain-attribute mirrors (set on the main thread) - workers must never
        # read Tk variables directly. See App._sync_runtime_options.
        quality = getattr(self.app, 'opt_quality', 'Auto')
        # NOTE: no '-4' (forcing IPv4 dead-loops the kiosk on IPv6-only links;
        # let yt-dlp choose). No '--quiet' so real errors reach the stderr we
        # capture. The URL goes AFTER a '--' end-of-options marker so a value
        # starting with '-' can never be parsed as a yt-dlp option (injection).
        return [
            self.yt_dlp_path,
            '--extractor-args', 'youtube:player_client=' + YT_PLAYER_CLIENTS,
            '--no-warnings',
            '--retries', '10', '--socket-timeout', '15',
            '-f', select_format(quality, self.divisions, self._cpu_pressure),
            '-o', '-',
            '--', self.url,
        ]

    def _targets(self):
        monitors = monitor_utils.list_monitors()
        multi = bool(getattr(self.app, 'opt_multi_monitor', False))
        selected = list(getattr(self.app, 'selected_monitor_indices', []))
        return monitor_utils.select_monitors(monitors, selected, multi), multi

    @staticmethod
    def _uses_fs(mon, single):
        """True when this target gets bare -fs: ONLY the lone primary screen at
        the origin. ffplay's -fs fullscreens on whatever screen its window
        happens to open on (the primary), so anything else must be a placed
        borderless window or playback lands on the wrong screen."""
        primary_origin = bool(mon.get('is_primary')) or (
            mon.get('x', 0) == 0 and mon.get('y', 0) == 0)
        return single and primary_origin

    def _ffplay_cmd(self, mon, single, muted):
        vf, _ow, _oh = monitor_utils.tile_filter_for(mon['width'], mon['height'], self.divisions)
        # Placed windows get the monitor's EXACT size, not the tile-floored
        # filter output (which can run a few px short of the screen and leave a
        # desktop sliver - "not quite fullscreen"). ffplay letterboxes the
        # <=2 px aspect difference invisibly.
        win = (['-fs'] if self._uses_fs(mon, single)
               else monitor_utils.window_opts_for(mon, mon['width'], mon['height']))
        audio = ['-an'] if muted else []
        # -threads 0 (auto): ffplay decodes single-threaded by DEFAULT, and
        #   software H.264 decode is the dominant CPU cost here - so on a weak
        #   multi-core laptop this is the single biggest win. It is a decoder
        #   option and must precede the '-' input.
        # -framedrop: under CPU pressure, drop LATE frames so a slow window snaps
        #   back to the live edge instead of drifting ever further behind. It
        #   drops at the decoder (after demux), so the byte container is never
        #   corrupted (honours the never-drop-bytes invariant). A no-op on a
        #   machine that comfortably keeps up.
        return ([self.ffplay_path, '-threads', '0', '-', '-vf', vf, '-framedrop',
                 '-autoexit', '-loglevel', 'warning', '-hide_banner'] + audio + win)

    def _fallback_ffplay_cmd(self, mon, single, muted, video_path):
        """Same window placement/quality flags as the live tile, but reads the
        local fallback file directly instead of the yt-dlp stdin pipe, and
        loops it forever (-loop 0) so it keeps covering the wall for however
        long the internet stays down."""
        vf, _ow, _oh = monitor_utils.tile_filter_for(mon['width'], mon['height'], self.divisions)
        win = (['-fs'] if self._uses_fs(mon, single)
               else monitor_utils.window_opts_for(mon, mon['width'], mon['height']))
        audio = ['-an'] if muted else []
        return ([self.ffplay_path, '-loop', '0', video_path, '-vf', vf, '-framedrop',
                 '-autoexit', '-loglevel', 'warning', '-hide_banner'] + audio + win)

    def _build_fallback_consumer(self, mon, single, muted, video_path):
        """Spawn one looping ffplay window playing the local fallback file.
        Raises if ffplay cannot launch (caller decides how to degrade)."""
        proc = subprocess.Popen(
            self._fallback_ffplay_cmd(mon, single, muted, video_path),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
            env=winkiosk.sdl_child_env())
        if self.ENFORCE_RECT and not self._uses_fs(mon, single):
            c = {'proc': proc, 'mon': mon}
            self._enforce_rect_async(c)
        return proc

    def _play_offline_fallback(self, video_path):
        """Cover every target monitor with the looping local fallback video
        while the internet is down, waking up only to probe connectivity every
        OFFLINE_FALLBACK_PROBE_INTERVAL. Returns True once the internet is back
        (caller should reconnect), False if playback stopped instead (either a
        Stop arrived, or the fallback windows themselves could not be spawned -
        the caller degrades to the blank probing wait in that case)."""
        targets, multi = self._targets()
        muted = bool(getattr(self.app, 'opt_mute', False))
        single = len(targets) <= 1
        procs = []
        try:
            for i, mon in enumerate(targets):
                procs.append(self._build_fallback_consumer(
                    mon, single, muted or i > 0, video_path))
        except Exception as e:
            log.warning("could not start offline fallback video: %s", e)
            for p in procs:
                _kill_tree(p)
            return False

        log.info("no internet connectivity; playing local fallback video on "
                 "%d window(s) - checking connectivity every ~%.0fs",
                 len(procs), self.OFFLINE_FALLBACK_PROBE_INTERVAL)
        self.app.post_ui(lambda: self.app.update_status(
            "No internet connection - playing offline video until it returns...",
            color='#b06a00'))
        try:
            online = False
            while self.play_flag:
                self._wait_backoff(jittered(self.OFFLINE_FALLBACK_PROBE_INTERVAL))
                if not self.play_flag:
                    break
                # A fallback window that died on its own (rare) is relaunched in
                # place so the wall never goes black while still waiting.
                for i, p in enumerate(procs):
                    if p.poll() is not None:
                        try:
                            procs[i] = self._build_fallback_consumer(
                                targets[i], single, muted or i > 0, video_path)
                        except Exception:
                            pass
                if internet_ok(self.url):
                    online = True
                    break
            return online
        finally:
            for p in procs:
                _kill_tree(p)

    # ---- process lifecycle ------------------------------------------------ #
    def _build_consumer(self, mon, index, single, muted):
        """Spawn one ffplay window for `mon` plus the bounded queue + writer
        thread that feeds it. This is the unit the fan-out distributes to and the
        supervisor relaunches. Raises if ffplay cannot launch."""
        proc = subprocess.Popen(
            self._ffplay_cmd(mon, single, muted),
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            # SDL >= 2.24 honours this and applies our physical-pixel geometry
            # 1:1 on scaled (125%/150%) monitors; older builds ignore it and
            # the rect enforcer below corrects them instead.
            env=winkiosk.sdl_child_env())
        c = {'proc': proc, 'q': queue.Queue(maxsize=self.FANOUT_QUEUE_MAX),
             'mon': mon, 'index': index, 'single': single, 'muted': muted,
             'dead': False, 'full_since': None, 'restarts': 0, 'first_restart': 0.0}
        c['thread'] = threading.Thread(target=self._consumer_writer, args=(c,), daemon=True)
        c['thread'].start()
        if self.ENFORCE_RECT and not self._uses_fs(mon, single):
            self._enforce_rect_async(c)
        return c

    def _enforce_rect_async(self, c):
        """Verify (and if needed force) this window to exactly cover its
        monitor, off the spawning thread. Bounded, daemon, and it aborts as
        soon as the window is retired/killed - so teardown never waits on it."""
        mon, proc = c['mon'], c['proc']

        def still_wanted():
            return (not c.get('dead')) and proc.poll() is None and self.play_flag

        def worker():
            try:
                winkiosk.enforce_window_rect(
                    proc.pid, mon['x'], mon['y'], mon['width'], mon['height'],
                    attempts=10, interval=1.0, still_wanted=still_wanted)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _start(self):
        self._terminate(join=True)   # clean slate (handles any previous run)
        if not self.play_flag:
            return                   # a stop arrived; do not launch anything

        # Resolve tools + launch OUTSIDE the lock: find_executable scans PATH
        # (slow if a sibling 'bin' is on a dropped network share) and Popen has
        # cost; doing it under the lock would make a GUI Stop block on it. We
        # only take the lock to PUBLISH the handles. Re-resolve every start so a
        # transient tool absence self-recovers.
        yt_path = find_executable('yt-dlp')
        ff_path = find_executable('ffplay')
        if not yt_path or not ff_path:
            raise RuntimeError("yt-dlp/ffplay not found on PATH")
        self.yt_dlp_path, self.ffplay_path = yt_path, ff_path

        targets, multi = self._targets()
        muted = bool(getattr(self.app, 'opt_mute', False))
        single = len(targets) <= 1
        log.info("start: url=%s divisions=%d windows=%d multi=%s muted=%s",
                 self.url, self.divisions, len(targets), multi, muted)

        ytdlp = subprocess.Popen(
            self._yt_dlp_cmd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW)
        # Drain yt-dlp's stderr into a small ring buffer so the log can say WHY
        # a session dropped (HTTP 403, format gone, geo-block, ...).
        stderr_tail = collections.deque(maxlen=40)
        stderr_thread = threading.Thread(target=self._drain_stderr,
                                         args=(ytdlp.stderr, stderr_tail), daemon=True)
        stderr_thread.start()

        # The ffplay windows are NOT spawned here. They spawn on the FIRST byte
        # of real data (see _fanout/_spawn_windows): a launch that never yields
        # data (internet down, bad URL, YouTube hiccup) then costs one yt-dlp
        # process and zero windows - no black fullscreen wall flashing on every
        # retry, and an order of magnitude less process churn on a weak laptop.
        stop_event = threading.Event()
        try:
            # Arm the stall watchdog from launch, so cold-start counts against
            # it (with the more generous STARTUP threshold until data flows).
            self._last_progress = time.monotonic()
            self._got_first_data = False
            self._windows_spawned = False
            fanout_thread = threading.Thread(
                target=self._fanout, args=(ytdlp.stdout, stop_event), daemon=True)
        except Exception:
            stop_event.set()
            _kill_tree(ytdlp)
            raise

        # Publish under the lock, THEN start the reader (so the spawn hook in
        # _fanout always sees the published pipeline). If a Stop landed during
        # the launch, tear the fresh pipeline down instead of orphaning it.
        with self._lock:
            published = self.play_flag
            if published:
                self.ytdlp_process = ytdlp
                self._stderr_tail = stderr_tail
                self._stderr_thread = stderr_thread
                self._consumers = []
                self.ffplay_processes = []
                self._spawn_plan = {'targets': targets, 'single': single, 'muted': muted}
                self._fanout_stop = stop_event
                self._fanout_thread = fanout_thread
        if not published:
            stop_event.set()
            _kill_tree(ytdlp)
            return
        fanout_thread.start()

    def _drain_stderr(self, stream, tail):
        """Keep the last lines of a subprocess's stderr for diagnosis."""
        try:
            for raw in iter(stream.readline, b''):
                try:
                    line = raw.decode('utf-8', 'ignore').rstrip()
                except Exception:
                    line = str(raw)
                if line:
                    tail.append(line)
        except Exception:
            pass

    def _retire(self, c):
        """Retire one fallen-behind consumer. KILL the ffplay first (so a writer
        blocked in stdin.write faults out immediately - closing the write end
        does nothing to a process that has stopped reading), THEN close the pipe.
        Killing also flips poll(), so _alive() trips and the wall relaunches."""
        c['dead'] = True
        _kill_tree(c['proc'])
        try:
            if c['proc'].stdin:
                c['proc'].stdin.close()
        except Exception:
            pass

    def _spawn_windows(self, stop_event):
        """Deferred window spawn: build every ffplay window the moment the FIRST
        byte of real data arrives (called from the fan-out thread). Returns True
        if the wall is (already) up. Building outside the lock (Popen cost),
        publishing under it; if a Stop/teardown raced in, the fresh windows are
        killed instead of published."""
        with self._lock:
            if self._consumers:            # already up (or a test pre-built them)
                self._windows_spawned = True
                return True
            plan = self._spawn_plan
            if plan is None or not self.play_flag:
                return bool(self._consumers)

        consumers = []
        try:
            # Only the first window keeps audio (rest get -an) to avoid echo.
            for i, mon in enumerate(plan['targets']):
                consumers.append(self._build_consumer(
                    mon, i, plan['single'], plan['muted'] or i > 0))
        except Exception as e:
            log.warning("could not spawn the player window(s): %s", e)
            for c in consumers:
                _kill_tree(c['proc'])
                try:
                    c['q'].put_nowait(None)
                except Exception:
                    pass
            return False

        with self._lock:
            ok = (self.play_flag and self.ytdlp_process is not None
                  and self._fanout_stop is stop_event and not stop_event.is_set())
            if ok:
                self._consumers = consumers
                self.ffplay_processes = [c['proc'] for c in consumers]
                self._windows_spawned = True
        if not ok:
            for c in consumers:
                _kill_tree(c['proc'])
                try:
                    c['q'].put_nowait(None)
                except Exception:
                    pass
            return False
        log.info("first data arrived; spawned %d window(s)", len(consumers))
        return True

    def _fanout(self, source, stop_event):
        """Reader: copy the one download into every live consumer's queue. Bytes
        are NEVER dropped (that would corrupt a window's container); a window that
        cannot keep up has its queue fill and, if it stays full for
        FANOUT_PUT_TIMEOUT, is RETIRED (killed). Retired/dead windows are skipped
        and the supervisor in run() relaunches them ON THEIR OWN. Reads the live
        consumer list each chunk (under the lock) so a relaunched window is picked
        up without restarting the reader. The windows themselves are spawned on
        the FIRST chunk (see _spawn_windows).
        """
        try:
            while not stop_event.is_set():
                chunk = source.read(65536)
                if not chunk:
                    break                      # yt-dlp ended -> full reconnect
                self._last_progress = time.monotonic()   # feed the stall watchdog
                self._got_first_data = True
                if not self._windows_spawned:
                    if not self._spawn_windows(stop_event):
                        break                  # spawn failed/raced -> reconnect
                with self._lock:
                    consumers = list(self._consumers)
                if not consumers:
                    break
                live = 0
                for c in consumers:
                    if c.get('dead'):
                        continue
                    if self._deliver(c, chunk, stop_event):
                        live += 1
                if live == 0:
                    break                      # every window dead/wedged
        except Exception:
            pass
        finally:
            # Tell every writer to finish (sentinel); harmless if its queue is full.
            for c in list(self._consumers):
                try:
                    c['q'].put_nowait(None)
                except Exception:
                    pass

    def _deliver(self, c, chunk, stop_event):
        """Hand one chunk to one window. Retry in short slices (so a Stop is
        responsive and the loop can re-check) and retire the window only if its
        queue has been continuously full for FANOUT_PUT_TIMEOUT - a transient
        burst drains within milliseconds and never trips it. Returns True if the
        chunk was queued (window still live)."""
        while not stop_event.is_set():
            try:
                c['q'].put(chunk, timeout=0.2)
                c['full_since'] = None
                return True
            except queue.Full:
                now = time.monotonic()
                if c.get('full_since') is None:
                    c['full_since'] = now
                elif now - c['full_since'] >= self.FANOUT_PUT_TIMEOUT:
                    log.warning("fan-out: window #%d fell behind; retiring it "
                                "(it will be relaunched on its own)",
                                c.get('index', 0) + 1)
                    self._wedge_retires += 1   # CPU-overload evidence (see run())
                    self._retire(c)
                    return False
        return False

    def _consumer_writer(self, c):
        """One per ffplay window: drain its queue to that window's stdin."""
        q, stdin = c['q'], c['proc'].stdin
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                stdin.write(chunk)
                stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        except Exception:
            pass
        finally:
            try:
                stdin.close()
            except Exception:
                pass

    def _alive(self):
        # The session is fundamentally alive if the download is running and AT
        # LEAST ONE window is still playing. A single dead/retired window no
        # longer condemns the whole wall: run()'s supervisor relaunches it on its
        # own (see _relaunch_consumer), so the healthy screens never blink. Only
        # the download dying, or EVERY window dying, ends the session.
        if not self.ytdlp_process or self.ytdlp_process.poll() is not None:
            return False
        if not self._consumers:
            return False
        return any((not c.get('dead')) and c['proc'].poll() is None
                   for c in self._consumers)

    def _stalled(self):
        """True if no bytes have arrived from the download for the stall
        threshold while everything still looks alive - a silent freeze (e.g.
        yt-dlp's hidden HLS ffmpeg wedged on a stalled segment) that poll()
        liveness cannot detect. Until the FIRST byte the generous startup
        threshold applies (extraction on a slow laptop + slow link can
        legitimately take longer than a mid-stream gap ever should)."""
        limit = self.STALL_TIMEOUT if self._got_first_data else self.STARTUP_STALL_TIMEOUT
        return (time.monotonic() - self._last_progress) > limit

    def _dead_window_indices(self):
        """Indices of windows that were retired (dead flag) or whose ffplay
        exited - the ones the supervisor should relaunch on their own."""
        with self._lock:
            return [i for i, c in enumerate(self._consumers)
                    if c.get('dead') or c['proc'].poll() is not None]

    def _relaunch_consumer(self, i):
        """Restart ONE fallen window in place, leaving the rest of the wall
        untouched (the fan-out picks up the replacement on its next chunk).
        Returns False if this window has restarted too many times within
        RESTART_WINDOW (so run() escalates to a full reconnect)."""
        with self._lock:
            if i >= len(self._consumers):
                return True
            old = self._consumers[i]
            now = time.monotonic()
            first = old.get('first_restart') or 0.0
            if not first or (now - first) > self.RESTART_WINDOW:
                restarts, first = 0, now      # fresh window of opportunity
            else:
                restarts = old.get('restarts', 0)
            if restarts >= self.MAX_WINDOW_RESTARTS:
                return False                  # a screen that simply cannot play
            mon, single, muted, index = old['mon'], old['single'], old['muted'], old['index']
        # Kill the old window and build its replacement OUTSIDE the lock (Popen
        # cost), then publish it. The old proc was already retired/exited.
        _kill_tree(old['proc'])
        try:
            new = self._build_consumer(mon, index, single, muted)
        except Exception as e:
            log.warning("window #%d relaunch failed: %s", index + 1, e)
            return False
        new['restarts'], new['first_restart'] = restarts + 1, first
        with self._lock:
            if i < len(self._consumers) and self.play_flag and self.ytdlp_process:
                self._consumers[i] = new
                self.ffplay_processes = [c['proc'] for c in self._consumers]
                published = True
            else:
                published = False
        if not published:
            _kill_tree(new['proc'])
            try:
                new['q'].put_nowait(None)
            except Exception:
                pass
            return True
        log.info("relaunched window #%d on its own (restart %d/%d)",
                 index + 1, restarts + 1, self.MAX_WINDOW_RESTARTS)
        return True

    def _death_reason(self):
        """Short human note for the log: which handle ended (call BEFORE
        _terminate, while the handles are still valid)."""
        if not self.ytdlp_process or self.ytdlp_process.poll() is not None:
            tail = list(self._stderr_tail or [])[-3:]
            extra = " [yt-dlp: {}]".format(" | ".join(tail)) if tail else ""
            return "the download ended" + extra
        if self._stalled():
            if not self._got_first_data:
                return "no data ever arrived (waited {:.0f}s)".format(
                    self.STARTUP_STALL_TIMEOUT)
            return "the stream went silent (no data for {:.0f}s)".format(self.STALL_TIMEOUT)
        dead = sorted({c.get('index', i) + 1 for i, c in enumerate(self._consumers)
                       if c.get('dead') or c['proc'].poll() is not None})
        if dead:
            return "player window(s) #{} kept failing".format(
                ",".join(map(str, dead)))
        return "an unknown reason"

    def _terminate(self, join=True):
        """Tear down the pipeline. The fast steps (signal + kill + close) run
        under the lock; the (potentially blocking) thread joins run OUTSIDE the
        lock and only when join=True - so the GUI thread (stop_video) never
        blocks on them. Every process is KILLED BEFORE its stdin is closed, so a
        writer blocked in stdin.write to a wedged pipe faults out immediately
        (closing the write end alone would not unblock a process that stopped
        reading; killing it makes the next write fail at once).
        """
        with self._lock:
            if self._fanout_stop:
                self._fanout_stop.set()
            # Kill the download first (the fan-out reader then sees EOF).
            if self.ytdlp_process:
                _kill_tree(self.ytdlp_process)
                self.ytdlp_process = None
            # Kill each player, THEN close its stdin and wake any idle writer.
            for c in self._consumers:
                _kill_tree(c['proc'])
                try:
                    if c['proc'].stdin:
                        c['proc'].stdin.close()
                except Exception:
                    pass
                try:
                    c['q'].put_nowait(None)
                except Exception:
                    pass
            for p in self.ffplay_processes:
                _kill_tree(p)
                if p.stdin:
                    try:
                        p.stdin.close()
                    except Exception:
                        pass
            threads = ([self._fanout_thread, self._stderr_thread]
                       + [c.get('thread') for c in self._consumers])
            self.ffplay_processes = []
            self._consumers = []
            self._spawn_plan = None      # a raced deferred spawn must not publish
            self._fanout_thread = None
            self._fanout_stop = None
            self._stderr_thread = None
            cur = threading.current_thread()
            if join:
                # Drain anything a previous non-blocking Stop parked for us, too.
                threads = threads + self._pending_joins
                self._pending_joins = []
            else:
                # A non-blocking (GUI) Stop cannot join here; park the still-live
                # threads so the worker's own terminal _terminate(join=True) joins
                # them - the bounded-lifetime guarantee survives a Stop.
                self._pending_joins.extend(
                    t for t in threads if t and t.is_alive() and t is not cur)
                threads = []

        if join:
            for t in threads:
                if t and t.is_alive() and t is not cur:
                    t.join(timeout=2)
                    if t.is_alive():
                        log.warning("a worker thread did not exit within 2s; continuing")

    # ---- worker entry point ---------------------------------------------- #
    def _wait_backoff(self, backoff):
        waited = 0.0
        while self.play_flag and waited < backoff:
            time.sleep(0.25)
            waited += 0.25

    def run(self):
        self.play_flag = True
        backoff = 3
        try:
            while self.play_flag:
                escalated = False
                # The whole body is guarded: a failure to launch (missing tool mid
                # run, a monitor-enumeration glitch, an exe-swap during self-heal)
                # must fall through to the backoff/reconnect path, never kill the
                # worker thread (which would freeze the UI with Play disabled).
                try:
                    self._start()
                    if not self.play_flag:
                        break
                    started = time.time()
                    announced = False
                    # Supervise: keep the download + every window alive. A single
                    # fallen window is relaunched ON ITS OWN so the rest of the wall
                    # never blinks; we escalate to a full reconnect only if the
                    # download dies, every window dies, or one window keeps dying
                    # (a screen that simply cannot play this stream).
                    while self.play_flag:
                        if not self.ytdlp_process or self.ytdlp_process.poll() is not None:
                            break                     # download ended
                        if self._stalled():
                            log.info("no data while still 'alive' -> treating as "
                                     "a stall and reconnecting")
                            break                     # silent freeze -> reconnect
                        dead = self._dead_window_indices()
                        if dead:
                            for i in dead:
                                if not self._relaunch_consumer(i):
                                    escalated = True
                            if escalated:
                                break
                        # Windows only exist once the first data arrived; before
                        # that the stall watchdog bounds the wait.
                        if self._windows_spawned and not self._consumers:
                            break
                        if (not announced and self._windows_spawned
                                and (time.time() - started) >= self.ANNOUNCE_AFTER):
                            announced = True
                            self.app.post_ui(lambda: self.app.update_status(
                                "Playing '{}'".format(self.title), color='black'))
                        time.sleep(0.4)
                    if not self.play_flag:
                        break
                    ran_for = time.time() - started
                    reason = self._death_reason()
                except Exception as e:
                    ran_for = 0.0
                    reason = "could not start playback: {}".format(e)
                    log.warning(reason)
                    # Don't promise a retry the auto-restart setting won't deliver.
                    will_retry = getattr(self.app, 'opt_auto_restart', True)
                    msg = ("Could not start playback - retrying..." if will_retry
                           else "Could not start playback.")
                    self.app.post_ui(lambda m=msg: self.app.update_status(m, color='#b06a00'))

                try:
                    self._terminate(join=True)
                except Exception:
                    log.exception("teardown failed; continuing to the reconnect path")

                # Measured CPU-overload learning (Auto quality only): windows that
                # repeatedly wedge (queue full -> retired) or a relaunch storm
                # mean THIS machine cannot decode the current resolution. Step
                # the Auto quality down one rung for the next session so a weak
                # laptop settles at a rate it can sustain instead of looping.
                if self._wedge_retires >= self.WEDGES_TO_PRESSURE or escalated:
                    if getattr(self.app, 'opt_quality', 'Auto') == 'Auto':
                        if self._cpu_pressure < self.MAX_CPU_PRESSURE:
                            self._cpu_pressure += 1
                            log.info("windows kept falling behind (wedges=%d, "
                                     "escalated=%s); lowering Auto quality one rung "
                                     "(pressure %d/%d)", self._wedge_retires, escalated,
                                     self._cpu_pressure, self.MAX_CPU_PRESSURE)
                    else:
                        log.info("windows kept falling behind but quality is fixed "
                                 "at %s; consider a lower manual quality or Auto",
                                 getattr(self.app, 'opt_quality', '?'))
                self._wedge_retires = 0

                if ran_for >= self.HEALTHY_SECONDS:
                    # A good long session: forget past failures. (_cpu_pressure is
                    # deliberately sticky: it reflects the hardware, not the network.)
                    backoff, self._fail_count, self._healed = 3, 0, False

                if not getattr(self.app, 'opt_auto_restart', True):
                    log.info("playback ended (ran %.0fs, %s); auto-restart off -> stop",
                             ran_for, reason)
                    break

                # Connectivity gate: while the internet is DOWN, probe with one
                # TCP SYN per interval instead of launching + killing the whole
                # yt-dlp/ffplay pipeline over and over. Hours of outage then cost
                # nothing (no process churn, no psutil scans, no window flashes,
                # no pointless yt-dlp self-updates), and playback resumes within
                # one probe interval of the connection returning.
                if not internet_ok(self.url):
                    offline_video = find_offline_video()
                    if offline_video:
                        online = self._play_offline_fallback(offline_video)
                        if not self.play_flag:
                            break
                        if online:
                            log.info("connectivity restored; reconnecting now")
                            continue
                        # fallback windows themselves failed to spawn - degrade
                        # to the blank probing wait below instead of tight-looping.
                    log.info("no internet connectivity (probe failed); probing "
                             "every ~%.0fs - nothing is spawned while offline",
                             self.OFFLINE_PROBE_INTERVAL)
                    self.app.post_ui(lambda: self.app.update_status(
                        "No internet connection - waiting for it to return...",
                        color='#b06a00'))
                    while self.play_flag:
                        self._wait_backoff(jittered(self.OFFLINE_PROBE_INTERVAL))
                        if internet_ok(self.url):
                            break
                    if not self.play_flag:
                        break
                    log.info("connectivity restored; reconnecting now")
                    continue

                self._fail_count += 1
                log.info("playback dropped after %.0fs (%s; failure #%d); backoff %ds",
                         ran_for, reason, self._fail_count, backoff)

                # Self-heal: repeated quick failures WHILE ONLINE usually mean
                # YouTube changed something, so update yt-dlp (its maintainers
                # ship the fix). Re-arm periodically so a fix shipped days into
                # an outage is still picked up - not healed only once ever on a
                # stream that never recovers. The connectivity gate above means
                # this never runs while offline (an offline `yt-dlp -U` is
                # useless churn and, interrupted, can corrupt the binary - after
                # which playback never works again even when the net returns).
                if self._fail_count % self.REHEAL_EVERY == 0:
                    self._healed = False
                if self._fail_count >= self.HEAL_AFTER_FAILS and not self._healed:
                    self._healed = True
                    log.info("self-heal: updating yt-dlp after repeated failures")
                    self.app.update_yt_dlp(silent=True)

                # Surface an explicit offline state instead of a forever-"Reconnecting"
                # flicker, so a passer-by can tell the wall is down, not buffering.
                if self._fail_count >= self.OFFLINE_AFTER_FAILS:
                    self.app.post_ui(lambda b=backoff: self.app.update_status(
                        "Stream appears offline - retrying every {}s".format(b), color='#b06a00'))
                else:
                    self.app.post_ui(lambda b=backoff: self.app.update_status(
                        "Reconnecting in {}s...".format(b), color='#b06a00'))

                self._wait_backoff(jittered(backoff))
                backoff = next_backoff(backoff)  # exponential, capped
        except Exception:
            # Belt-and-braces: run() must NEVER die silently (a dead worker with
            # play_flag still set is a black wall forever). Anything reaching
            # here is a bug; log it loudly and end the session cleanly - the
            # App-level watchdog restarts playback.
            log.exception("player worker crashed unexpectedly")
        finally:
            try:
                self._terminate(join=True)
            except Exception:
                log.exception("final teardown failed")
            self.app.post_ui(lambda: self.app._on_player_finished(self))

    def stop(self, join=False):
        # Signal + teardown. play_flag is cleared synchronously (cheap) so the
        # worker's run loop also winds down; the actual teardown (which includes
        # psutil process-tree kills - NOT free on Windows, ~one OS process scan
        # per handle) runs OFF the Tk main thread so the GUI never hitches on a
        # Stop / Play / window switch. on_closing passes join=True so the app
        # blocks until every helper process is gone (no orphaned ffplay/yt-dlp
        # left running after the window closes).
        if self.play_flag:
            log.info("stop requested")
        self.play_flag = False
        if join:
            self._terminate(join=True)
        else:
            threading.Thread(target=self._terminate, kwargs={'join': False},
                             daemon=True).start()


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    # Two restrained palettes; one subtle accent for the primary action.
    THEMES = {
        'Light': {'bg': '#f4f5f7', 'fg': '#1f2937', 'field': '#ffffff',
                  'sub': '#e4e7eb', 'sub_fg': '#374151', 'info': '#6b7280',
                  'status': '#e9ebef', 'border': '#d1d5db'},
        'Dark':  {'bg': '#1f2228', 'fg': '#e6e6e6', 'field': '#2b2f36',
                  'sub': '#353a42', 'sub_fg': '#e6e6e6', 'info': '#9aa0a6',
                  'status': '#2b2f36', 'border': '#3a3f47'},
    }
    ACCENT = '#3b6ea5'
    ACCENT_HOVER = '#4a7fb8'

    def __init__(self):
        super().__init__()
        self.title("Video Tiler")
        self.player = None
        self.video_thread = None
        self.play_flag = False
        self.theme_var = tk.StringVar(value='Light')
        self.selected_monitor_indices = [m['index'] for m in monitor_utils.list_monitors()]
        # The user's DESIRED monitor set, kept unfiltered across an undock so a
        # temporarily-absent screen is not dropped from settings (see
        # _merged_monitor_selection). selected_monitor_indices is the resolved,
        # currently-available subset the player actually uses.
        self._desired_monitor_indices = list(self.selected_monitor_indices)
        self._last_title_url = None

        # Plain-attribute mirrors of the GUI options. Worker threads read THESE
        # (never the Tk variables, which are not thread-safe). Kept in sync on
        # the main thread by _sync_runtime_options.
        self.opt_auto_restart = True
        self.opt_multi_monitor = False
        self.opt_mute = False
        self.opt_quality = 'Auto'

        # Thread-safe GUI updates: workers put callables here; the main loop drains.
        self._ui_queue = queue.Queue()

        # Serialize yt-dlp self-updates so the Tools menu and the automatic
        # self-heal can never run two concurrent `yt-dlp -U` that race-overwrite
        # (and corrupt) the same on-disk binary.
        self._update_lock = threading.Lock()
        self._ytdlp_updating = False

        try:
            icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'img', 'app.ico')
            self.iconbitmap(icon)
        except Exception:
            pass

        self.create_menu()
        self.create_widgets()
        self.load_all_settings()
        # Fixed control surface: a kiosk shouldn't be able to shrink the window
        # and clip the Play/Stop buttons or status bar out of reach.
        try:
            self.resizable(False, False)
        except Exception:
            pass
        self._closing = False
        self._pump_after_id = self.after(50, self._pump_ui_queue)

        if not self.url_entry.get():
            self.url_entry.set(DEFAULT_URL)
        self.update_video_title()

        if self.autoplay.get():
            self.after(2500, lambda: self.play_video(from_autoplay=True))
        self.after(4000, lambda: self.check_for_updates(silent=True))
        # Kiosk watchdog: if the worker thread ever dies while playback is
        # wanted (should be impossible - run() is hardened - but a kiosk must
        # survive even "impossible"), restart playback instead of showing a
        # black wall forever. Also log health metrics so a field freeze report
        # comes with evidence (memory/threads growth) instead of guesswork.
        self.after(60000, self._watchdog_tick)
        self.after(600000, self._log_health)

    # ---- kiosk watchdog + health telemetry -------------------------------- #
    def _watchdog_tick(self):
        if getattr(self, '_closing', False):
            return
        try:
            if (self.play_flag and self.player is not None
                    and self.video_thread is not None
                    and not self.video_thread.is_alive()):
                log.error("watchdog: playback wanted but the worker thread is "
                          "dead; restarting playback")
                self.play_video(from_autoplay=True)
        except Exception:
            log.exception("watchdog tick failed")
        self.after(60000, self._watchdog_tick)

    def _log_health(self):
        if getattr(self, '_closing', False):
            return
        try:
            proc = psutil.Process()
            with proc.oneshot():
                rss_mb = proc.memory_info().rss / (1024 * 1024)
                n_threads = proc.num_threads()
                handles = proc.num_handles() if hasattr(proc, 'num_handles') else -1
            log.info("health: rss=%.0fMB threads=%d handles=%d playing=%s",
                     rss_mb, n_threads, handles, self.play_flag)
        except Exception:
            pass
        self.after(600000, self._log_health)

    # ---- thread-safe UI marshaling --------------------------------------- #
    def post_ui(self, fn):
        self._ui_queue.put(fn)

    def _pump_ui_queue(self):
        if self._closing:
            return
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self._pump_after_id = self.after(50, self._pump_ui_queue)

    # ---- widgets ---------------------------------------------------------- #
    def create_widgets(self):
        self.video_title_label = tk.Label(self, text="Video Title", font=("Helvetica", 12))
        self.video_title_label.grid(row=1, column=1, columnspan=4, padx=10, pady=10, sticky='w')

        tk.Label(self, text="Video URL:", font=("Helvetica", 12)).grid(
            row=2, column=1, padx=10, pady=10, sticky='w')
        self.url_entry = ttk.Combobox(self, values=list(DEFAULT_SETTINGS['urls']), width=50)
        self.url_entry.set('')
        self.url_entry.grid(row=2, column=2, columnspan=3, padx=10, pady=10, sticky='w')

        tk.Label(self, text="Grid divisions:", font=("Helvetica", 12)).grid(
            row=3, column=1, padx=10, pady=10, sticky='w')
        vcmd = (self.register(self._validate_divisions), '%P')
        self.divisions_spinbox = tk.Spinbox(self, from_=1, to=64, increment=1, width=5,
                                            validate='key', validatecommand=vcmd)
        self.divisions_spinbox.grid(row=3, column=2, padx=10, pady=10, sticky='w')
        # Clamp the visible value to what is actually used (1..64) on blur, so
        # the box never shows a number we silently overrode.
        self.divisions_spinbox.bind("<FocusOut>", lambda e: self._clamp_divisions_display())

        self.stop_button = tk.Button(self, text="■  Stop", command=self.stop_video,
                                     width=9, font=("Helvetica", 10), relief='flat',
                                     bd=0, cursor='hand2', highlightthickness=0)
        self.stop_button.grid(row=3, column=3, padx=8, pady=8)
        self.play_button = tk.Button(self, text="▶  Play", command=self.play_video,
                                     width=9, font=("Helvetica", 10), relief='flat',
                                     bd=0, cursor='hand2', highlightthickness=0)
        self.play_button.grid(row=3, column=4, padx=8, pady=8)

        # Options row
        self.auto_restart_video = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="Auto Restart", variable=self.auto_restart_video,
                       command=self.save_all_settings).grid(row=4, column=1, padx=10, pady=8, sticky='w')
        self.multi_monitor = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Multi-monitor", variable=self.multi_monitor,
                       command=self.save_all_settings).grid(row=4, column=2, padx=10, pady=8, sticky='w')
        self.mute = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Mute", variable=self.mute,
                       command=self.save_all_settings).grid(row=4, column=3, padx=10, pady=8, sticky='w')
        self.choose_monitors_button = tk.Button(self, text="Monitors…", command=self.choose_monitors)
        self.choose_monitors_button.grid(row=4, column=4, padx=10, pady=8)

        # Quality + kiosk row
        tk.Label(self, text="Quality:", font=("Helvetica", 11)).grid(
            row=5, column=1, padx=10, pady=6, sticky='e')
        self.quality = ttk.Combobox(self, values=QUALITY_CHOICES, width=8, state='readonly')
        self.quality.set('Auto')
        self.quality.grid(row=5, column=2, padx=10, pady=6, sticky='w')
        self.quality.bind("<<ComboboxSelected>>", lambda e: self.save_all_settings())
        self.autoplay = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Auto-play on launch", variable=self.autoplay,
                       command=self.save_all_settings).grid(row=5, column=3, padx=10, pady=6, sticky='w')
        self.run_at_startup = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Run at Windows startup", variable=self.run_at_startup,
                       command=self.on_toggle_startup).grid(row=5, column=4, padx=10, pady=6, sticky='w')

        self.monitors_info_label = tk.Label(self, text="", font=("Helvetica", 9), anchor='w')
        self.monitors_info_label.grid(row=6, column=1, columnspan=5, padx=10, pady=(0, 2), sticky='w')
        self.status_bar = tk.Label(self, text="Status: Ready", bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.grid(row=7, column=1, columnspan=5, padx=10, pady=10, sticky='ew')

        self.url_entry.bind("<FocusOut>", lambda e: self.update_video_title())
        self.url_entry.bind("<Return>", lambda e: self.play_video())
        # Bind shortcuts to THIS window (not bind_all) so they don't leak into
        # the modal monitor chooser / Identify overlays, where Esc/F5/space
        # would otherwise fire playback actions behind the dialog.
        self.bind("<Escape>", lambda e: self.stop_video())
        self.bind("<F5>", lambda e: self.play_video())
        self.bind("<space>", self._space_shortcut)

        for r in range(8):
            self.grid_rowconfigure(r, weight=1 if r == 0 else 0)
        for c in range(5):
            self.grid_columnconfigure(c, weight=1)

        self.refresh_monitor_info()
        self.apply_theme()

    def create_menu(self):
        menubar = tk.Menu(self)
        mf = tkfont.Font(family="Helvetica", size=12)
        mfs = tkfont.Font(family="Helvetica", size=8)

        view_menu = tk.Menu(menubar, tearoff=0)
        theme_menu = tk.Menu(view_menu, tearoff=0)
        for t in THEME_CHOICES:
            theme_menu.add_radiobutton(label=t, variable=self.theme_var, value=t,
                                       command=self.on_theme_change, font=mf)
        view_menu.add_cascade(label="Theme", menu=theme_menu, font=mf)
        menubar.add_cascade(label="View", menu=view_menu, font=mfs)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Update yt-dlp now", command=self.update_yt_dlp, font=mf)
        tools_menu.add_command(label="Check for updates",
                               command=lambda: self.check_for_updates(False), font=mf)
        menubar.add_cascade(label="Tools", menu=tools_menu, font=mfs)

        about_menu = tk.Menu(menubar, tearoff=0)
        about_menu.add_command(label="Supported video sites",
                               command=lambda: webbrowser.open(SUPPORTED_WEB_SITES), font=mf)
        about_menu.add_command(label="Why Tiling",
                               command=lambda: webbrowser.open(WHY_TILING_URL), font=mf)
        about_menu.add_command(label="Source code",
                               command=lambda: webbrowser.open(SOURCE_CODE_GITHUB), font=mf)
        about_menu.add_command(label="Help", command=self.show_help, font=mf)
        menubar.add_cascade(label="About", menu=about_menu, font=mfs)
        self.config(menu=menubar)

    # ---- monitor chooser -------------------------------------------------- #
    def identify_monitors(self):
        wins = []
        for m in monitor_utils.list_monitors():
            w = tk.Toplevel(self)
            w.overrideredirect(True)
            w.geometry("{w}x{h}+{x}+{y}".format(w=m['width'], h=m['height'], x=m['x'], y=m['y']))
            w.configure(bg='black')
            try:
                w.attributes('-topmost', True)
            except Exception:
                pass
            tk.Label(w, text=str(m['index'] + 1), fg='#39d0ff', bg='black',
                     font=("Helvetica", 240, "bold")).pack(expand=True)
            wins.append(w)
        self.after(2500, lambda: [w.destroy() for w in wins])

    def choose_monitors(self):
        monitors = monitor_utils.list_monitors()
        dlg = tk.Toplevel(self)
        dlg.title("Select monitors")
        dlg.transient(self)
        dlg.grab_set()
        dlg.bind("<Escape>", lambda e: dlg.destroy())  # Esc cancels the dialog
        tk.Label(dlg, text="Tick the monitors to use for tiled playback:",
                 font=("Helvetica", 11)).pack(padx=12, pady=(12, 6), anchor='w')
        rows = []
        for m in monitors:
            var = tk.BooleanVar(value=(m['index'] in self.selected_monitor_indices))
            tk.Checkbutton(dlg, text=monitor_utils.describe(m), variable=var).pack(
                padx=18, pady=2, anchor='w')
            rows.append((m['index'], var))

        def set_all(value):
            for _, v in rows:
                v.set(value)

        def apply_sel():
            chosen = [idx for idx, v in rows if v.get()]
            if not chosen:
                messagebox.showwarning("Monitors", "Please tick at least one monitor.")
                return
            self.selected_monitor_indices = chosen
            # An explicit choice updates the DESIRED set (what we persist). The
            # chooser only lists attached monitors, so this intentionally reflects
            # the screens present now.
            self._desired_monitor_indices = list(chosen)
            if len(chosen) > 1:
                self.multi_monitor.set(True)
            self.save_all_settings()
            dlg.destroy()

        helpers = tk.Frame(dlg)
        helpers.pack(pady=(8, 0))
        tk.Button(helpers, text="Select all", command=lambda: set_all(True)).pack(side=tk.LEFT, padx=6)
        tk.Button(helpers, text="Select none", command=lambda: set_all(False)).pack(side=tk.LEFT, padx=6)
        tk.Button(helpers, text="Identify", command=self.identify_monitors).pack(side=tk.LEFT, padx=6)
        btns = tk.Frame(dlg)
        btns.pack(pady=12)
        tk.Button(btns, text="OK", width=10, command=apply_sel).pack(side=tk.LEFT, padx=10)
        tk.Button(btns, text="Cancel", width=10, command=dlg.destroy).pack(side=tk.LEFT, padx=10)
        # Apply the active theme so the chooser doesn't pop as a light-mode box.
        self._style_subtree(dlg)

    def refresh_monitor_info(self):
        try:
            mons = monitor_utils.list_monitors()
            sel = [m for m in mons if m['index'] in self.selected_monitor_indices]
            txt = ", ".join("#{}".format(m['index'] + 1) for m in sel) or "none"
            self.monitors_info_label.config(
                text="Detected {n} monitor(s).  Selected for multi-monitor: {s}".format(
                    n=len(mons), s=txt))
        except Exception:
            pass

    # ---- settings --------------------------------------------------------- #
    def _sync_runtime_options(self):
        """Copy the GUI options into plain attributes that worker threads may
        read safely. MUST be called on the main thread (it touches Tk vars)."""
        try:
            self.opt_auto_restart = bool(self.auto_restart_video.get())
            self.opt_multi_monitor = bool(self.multi_monitor.get())
            self.opt_mute = bool(self.mute.get())
            self.opt_quality = self.quality.get()
        except Exception:
            pass

    def _merged_monitor_selection(self):
        """The monitor selection to persist. We save the user's DESIRED set
        (`_desired_monitor_indices`), which is kept UNFILTERED across an undock,
        not the runtime-resolved set. A docked kiosk laptop saves [0,1,2]; run
        undocked it can only resolve [0], but the desired [0,1,2] is preserved so
        re-docking restores the wall instead of silently wiping it on the next
        checkbox toggle or window close. Only an explicit Monitors... choice
        changes the desired set."""
        desired = [i for i in getattr(self, '_desired_monitor_indices', []) if isinstance(i, int)]
        if desired:
            return sorted(set(desired))
        return sorted({i for i in self.selected_monitor_indices if isinstance(i, int)})

    def save_all_settings(self):
        self._sync_runtime_options()
        try:
            url = self.url_entry.get().strip()
            urls = list(self.url_entry['values'])
            if url and url not in urls:
                urls.append(url)
            urls = urls[-20:]   # bounded history: a years-old kiosk can't bloat it
            self.url_entry['values'] = urls
            write_settings({
                'url': url, 'urls': urls,
                'divisions': self._safe_divisions(),
                'auto_restart': bool(self.auto_restart_video.get()),
                'multi_monitor': bool(self.multi_monitor.get()),
                'selected_monitor_indices': self._merged_monitor_selection(),
                'mute': bool(self.mute.get()),
                'quality': self.quality.get(),
                'autoplay': bool(self.autoplay.get()),
                'run_at_startup': bool(self.run_at_startup.get()),
                'theme': self.theme_var.get(),
            })
        except Exception as e:
            log.warning("save settings failed: %s", e)
        self.refresh_monitor_info()

    def load_all_settings(self):
        data = read_settings()
        try:
            # Keep the FULL saved selection as the desired set (unfiltered), so a
            # screen absent this session survives to the next dock; resolve the
            # runtime selection to whatever is actually attached right now.
            raw_sel = [i for i in data.get('selected_monitor_indices', []) if isinstance(i, int)]
            if raw_sel:
                self._desired_monitor_indices = raw_sel
            avail = [m['index'] for m in monitor_utils.list_monitors()]
            sel = [i for i in raw_sel if i in avail]
            if sel:
                self.selected_monitor_indices = sel
            d = int(data.get('divisions', 3))
            if 1 <= d <= 64:
                self.divisions_spinbox.delete(0, tk.END)
                self.divisions_spinbox.insert(0, str(d))
            self.auto_restart_video.set(bool(data.get('auto_restart', True)))
            self.multi_monitor.set(bool(data.get('multi_monitor', False)))
            self.mute.set(bool(data.get('mute', False)))
            # Only trust http(s) URLs from the settings file - a tampered file
            # must not be able to plant a yt-dlp argument-injection payload that
            # autoplay would then launch hands-free at the next login.
            urls = data.get('urls') or list(self.url_entry['values'])
            urls = [u for u in urls if is_valid_stream_url(u)]
            if urls:
                self.url_entry['values'] = urls
            if data.get('quality') in QUALITY_CHOICES:
                self.quality.set(data['quality'])
            self.autoplay.set(bool(data.get('autoplay', False)))
            self.run_at_startup.set(bool(data.get('run_at_startup', False)))
            if data.get('theme') in self.THEMES:
                self.theme_var.set(data['theme'])
                self.apply_theme()
            if is_valid_stream_url(data.get('url')):
                self.url_entry.set(data['url'])
        except Exception as e:
            log.warning("load settings failed: %s", e)
        self._sync_runtime_options()
        self.refresh_monitor_info()

    def on_toggle_startup(self):
        if not set_run_at_startup(bool(self.run_at_startup.get())):
            messagebox.showwarning("Run at startup", "Could not change the Windows startup setting.")
            self.run_at_startup.set(False)
        self.save_all_settings()

    # ---- theme ------------------------------------------------------------ #
    def on_theme_change(self):
        self.apply_theme()
        self.save_all_settings()

    def _style_subtree(self, root):
        """Apply the active palette to a widget tree (used for the main window
        and for dialogs created later, e.g. the monitor chooser, so they don't
        pop as a light-mode box in Dark theme)."""
        p = self.THEMES.get(self.theme_var.get(), self.THEMES['Light'])
        BG, FG, FIELD, SUB = p['bg'], p['fg'], p['field'], p['sub']

        def style_widget(w):
            cls = w.winfo_class()
            try:
                if cls == "Label":
                    w.configure(bg=BG, fg=FG)
                elif cls in ("Frame", "Toplevel", "Tk"):
                    w.configure(bg=BG)
                elif cls == "Button":
                    w.configure(bg=SUB, fg=FG, activebackground=SUB, activeforeground=FG)
                elif cls == "Checkbutton":
                    w.configure(bg=BG, fg=FG, selectcolor=FIELD,
                                activebackground=BG, activeforeground=FG)
                elif cls == "Spinbox":
                    w.configure(bg=FIELD, fg=FG, buttonbackground=SUB,
                                insertbackground=FG, relief='flat', highlightthickness=0)
            except tk.TclError:
                pass
            for c in w.winfo_children():
                style_widget(c)

        style_widget(root)

    def apply_theme(self, theme=None):
        if theme:
            self.theme_var.set(theme)
        p = self.THEMES.get(self.theme_var.get(), self.THEMES['Light'])
        BG, FG, FIELD, SUB, SUB_FG = p['bg'], p['fg'], p['field'], p['sub'], p['sub_fg']

        try:
            self.configure(bg=BG)
            self._style_subtree(self)
            # disabledforeground must stay legible: while playing, the Play
            # button is disabled, and Tk's default grey disabled text is nearly
            # invisible on the slate accent. Use a light tint instead.
            self.play_button.configure(bg=self.ACCENT, fg='white',
                                       activebackground=self.ACCENT_HOVER, activeforeground='white',
                                       disabledforeground='#dce7f4')
            self.stop_button.configure(bg=SUB, fg=SUB_FG, activebackground=SUB, activeforeground=SUB_FG)
            self.status_bar.configure(bg=p['status'], fg=FG)
            self.monitors_info_label.configure(bg=BG, fg=p['info'])
            style = ttk.Style()
            try:
                style.theme_use('clam')
            except Exception:
                pass
            style.configure("TCombobox", fieldbackground=FIELD, background=SUB,
                            foreground=FG, arrowcolor=FG, bordercolor=p['border'])
            style.map("TCombobox", fieldbackground=[('readonly', FIELD)], foreground=[('readonly', FG)])
        except Exception:
            pass

    # ---- updates ---------------------------------------------------------- #
    def update_yt_dlp(self, silent=False):
        # Single-flight guard: the Tools menu and the automatic self-heal must
        # never run two concurrent `yt-dlp -U` against the same binary - on
        # Windows a self-replacing exe written by one process while another
        # replaces it can be left truncated/locked, after which every launch
        # fails (looks like a permanent freeze/offline).
        with self._update_lock:
            if self._ytdlp_updating:
                if not silent:
                    self.post_ui(lambda: self.update_status(
                        "A yt-dlp update is already running."))
                return
            self._ytdlp_updating = True

        def worker():
            path = find_executable('yt-dlp')
            if not path:
                log.warning("update yt-dlp: not found on PATH")
                if not silent:
                    self.post_ui(lambda: messagebox.showwarning(
                        "Update yt-dlp", "yt-dlp was not found on PATH."))
                return
            try:
                self.post_ui(lambda: self.update_status("Updating yt-dlp...", color='#b06a00'))
                res = subprocess.run([path, '-U'], capture_output=True, text=True,
                                     timeout=180, creationflags=CREATE_NO_WINDOW)
                out = ((res.stdout or '') + (res.stderr or '')).strip()
                ok = res.returncode == 0
                # `yt-dlp -U` only self-updates a STANDALONE binary. A pip /
                # console-script install refuses it - detect that and update via
                # pip instead, so the self-heal actually does something there.
                # NOT in a frozen build: there sys.executable is video-tiler.exe
                # (the PyInstaller bootloader ignores '-m pip' and would just
                # re-launch a SECOND kiosk), so we tell the operator instead.
                if not ok and _looks_like_pip_ytdlp(out):
                    if getattr(sys, 'frozen', False):
                        log.warning("yt-dlp is a pip/source install but this is a frozen "
                                    "build - cannot pip-update; update yt-dlp manually")
                    else:
                        log.info("yt-dlp -U is a no-op on a pip install; updating via pip")
                        self.post_ui(lambda: self.update_status("Updating yt-dlp (pip)...", color='#b06a00'))
                        # --isolated makes pip IGNORE environment variables
                        # (PIP_INDEX_URL/PIP_EXTRA_INDEX_URL) and pip.ini, so an
                        # attacker who can set a kiosk env var or drop a config
                        # cannot silently redirect this unattended, recurring
                        # upgrade to a malicious package index.
                        res = subprocess.run(
                            [sys.executable, '-m', 'pip', 'install', '--isolated',
                             '--upgrade', 'yt-dlp'],
                            capture_output=True, text=True, timeout=300, creationflags=CREATE_NO_WINDOW)
                        out = ((res.stdout or '') + (res.stderr or '')).strip()
                        ok = res.returncode == 0
                if ok:
                    log.info("yt-dlp update finished ok")
                else:
                    log.warning("yt-dlp update FAILED (rc=%s): %s", res.returncode, out[-300:])
                self.post_ui(lambda: self.update_status(
                    "yt-dlp update finished." if ok else "yt-dlp update failed."))
                if not silent:
                    self.post_ui(lambda: messagebox.showinfo("Update yt-dlp", out[-800:] or "Done."))
            except Exception as e:
                log.warning("yt-dlp update error: %s", e)
                self.post_ui(lambda: self.update_status("yt-dlp update failed."))
                if not silent:
                    self.post_ui(lambda: messagebox.showwarning(
                        "Update yt-dlp", "Update failed:\n{}".format(e)))

        def runner():
            try:
                worker()
            finally:
                with self._update_lock:
                    self._ytdlp_updating = False
        threading.Thread(target=runner, daemon=True).start()

    def check_for_updates(self, silent=False):
        def worker():
            try:
                req = urllib.request.Request(UPDATE_VERSION_URL, headers={'User-Agent': 'video-tiler'})
                with urllib.request.urlopen(req, timeout=8) as r:
                    remote = r.read().decode('utf-8', 'ignore').strip()
            except Exception:
                if not silent:
                    self.post_ui(lambda: messagebox.showinfo("Updates", "Could not reach the update server."))
                return
            if remote and remote != PROGRAM_VERSION:
                if silent:
                    # NEVER pop a modal on the quiet launch check: an unattended
                    # kiosk has nobody to dismiss it, and a modal inside the UI
                    # pump stalls every status update until it is closed.
                    log.info("update available: %s (running %s)", remote, PROGRAM_VERSION)
                    self.post_ui(lambda: self.update_status(
                        "Update {} available - see Tools menu.".format(remote)))
                    return

                def offer():
                    if messagebox.askyesno(
                            "Update available",
                            "A newer version ({}) is available (you have {}).\n\n"
                            "Open the download page?".format(remote, PROGRAM_VERSION)):
                        webbrowser.open(RELEASES_URL)
                self.post_ui(offer)
            elif not silent:
                self.post_ui(lambda: messagebox.showinfo(
                    "Updates", "You are on the latest version ({}).".format(PROGRAM_VERSION)))
        threading.Thread(target=worker, daemon=True).start()

    # ---- playback control ------------------------------------------------- #
    def update_status(self, message, color='black'):
        self.status_bar.config(text="Status: {}".format(message), fg=color)

    def _space_shortcut(self, event=None):
        try:
            w = self.focus_get()
            if w is not None and w.winfo_class() in ('Entry', 'TEntry', 'TCombobox', 'Spinbox'):
                return
        except Exception:
            pass
        self.stop_video() if self.play_flag else self.play_video()

    def update_video_title(self, force=False):
        url = self.url_entry.get().strip()
        # Only probe http(s) URLs (also avoids running the extractor on junk).
        if not is_valid_stream_url(url):
            return
        # Debounce: <FocusOut> fires often, so skip a refetch when the URL has
        # not changed since the last fetch we kicked off.
        if not force and url == self._last_title_url:
            return
        self._last_title_url = url
        # Fetch the title directly (no throwaway Player / executable scans).
        fetch_title_async(self, url, lambda t: self.video_title_label.config(text=t))

    def _validate_divisions(self, proposed):
        """Spinbox key-validation: allow empty (mid-edit) or up to 3 digits."""
        return proposed == '' or (proposed.isdigit() and len(proposed) <= 3)

    def _clamp_divisions_display(self):
        """Write the clamped 1..64 value back so the box matches what is used."""
        d = self._safe_divisions()
        if self.divisions_spinbox.get().strip() != str(d):
            self.divisions_spinbox.delete(0, tk.END)
            self.divisions_spinbox.insert(0, str(d))

    def _safe_divisions(self):
        return clamp_divisions(self.divisions_spinbox.get())

    def play_video(self, from_autoplay=False):
        """Start playback. from_autoplay=True marks an UNATTENDED start (kiosk
        autoplay / watchdog): those paths must never block on a modal error box
        nobody is there to dismiss - they log, show the status bar, and retry."""
        self.stop_video()
        url = self.url_entry.get().strip()
        if not is_valid_stream_url(url):
            if from_autoplay:
                log.warning("autoplay: no valid URL configured")
                self.update_status("No valid video URL configured.", color='#b06a00')
            else:
                messagebox.showerror("URL", "Please enter a valid http(s) video URL.")
            return
        divisions = self._safe_divisions()
        self._clamp_divisions_display()
        self.save_all_settings()  # also syncs the runtime-option mirrors
        self.player = Player(self, url, divisions)
        if not self.player.tools_ok:
            self.player = None
            if from_autoplay:
                # A kiosk boot race (bin folder on a slow disk/share, AV still
                # scanning the exes) must retry, not park on an error forever.
                log.warning("autoplay: yt-dlp/ffmpeg/ffplay not found yet; retrying in 30s")
                self.update_status("Player tools not found - retrying in 30s...",
                                   color='#b06a00')
                self.after(30000, lambda: self.play_video(from_autoplay=True))
            else:
                messagebox.showerror(
                    "Missing tools",
                    "yt-dlp, ffmpeg and ffplay must be on PATH (or next to the app).")
            return
        self.play_flag = True
        # While a show is wanted, the machine must not idle into sleep or blank
        # the display (an unattended kiosk that slept "froze" for good - even
        # the network recovery could not wake it). Restored on Stop/close; an
        # explicit lid-close/power-button still wins, as it should.
        winkiosk.keep_awake(True)
        self.play_button.config(state=tk.DISABLED)
        self.update_status("Starting...", color='#b06a00')
        self.player.fetch_title_async(lambda t: self.video_title_label.config(text=t))
        self.video_thread = threading.Thread(target=self.player.run, daemon=True)
        self.video_thread.start()

    def stop_video(self):
        if self.player:
            self.player.stop()
        self.play_flag = False
        winkiosk.keep_awake(False)
        self.play_button.config(state=tk.NORMAL)
        self.update_status("Ready")

    def _on_player_finished(self, player):
        """Called on the main thread when a Player's run() loop exits. Only the
        currently-active player may flip the UI back, so a stale old player
        (already replaced by a new Play) can't re-enable the button mid-playback."""
        if player is not self.player:
            return
        self.play_flag = False
        winkiosk.keep_awake(False)
        self.play_button.config(state=tk.NORMAL)
        self.update_status("Stopped.")

    def show_help(self):
        messagebox.showinfo("Help", (
            "HOW TO USE\n"
            "  1. Pick or paste a video URL (YouTube, X, etc.).\n"
            "  2. Set the grid size (e.g. 5 = a 5x5 grid of identical tiles).\n"
            "  3. Press Play (or Enter). Every tile shows the same live frame;\n"
            "     only ONE stream is downloaded.\n\n"
            "STOPPING\n"
            "  Single screen: the player is full screen - press Esc/'q' on it, or\n"
            "  Alt+Tab back here and press Stop.\n"
            "  Multi-monitor: the players cover this window, so click this app on\n"
            "  the taskbar (or Alt+Tab to it) THEN press Esc/Stop. Closing one\n"
            "  player window just relaunches the whole wall (it does NOT stop);\n"
            "  turn off 'Auto Restart' first if you want closing a window to stop.\n\n"
            "MULTI-MONITOR\n"
            "  Tick 'Multi-monitor' and use 'Monitors...' to choose screens (e.g. 2\n"
            "  of 3). One download is fanned out to one window per screen.\n\n"
            "KEYBOARD\n"
            "  Esc = Stop    F5 = Play    Space = Play / Stop\n\n"
            "OPTIONS\n"
            "  Quality forces a resolution (Auto picks by tile count). Auto-play and\n"
            "  Run-at-startup enable kiosk mode. Theme (View menu) and every choice\n"
            "  are remembered. Reconnect is automatic with backoff, and yt-dlp\n"
            "  self-updates after repeated failures (Tools > Update yt-dlp to force).\n\n"
            "LOG\n"
            "  Activity is logged to:\n  {log}\n\n"
            "PLATFORM\n"
            "  Tested on Windows; macOS/Linux supported on a best-effort basis.\n\n"
            "Version: {v}\nEmail: {e}\nWebsite: {w}".format(
                v=PROGRAM_VERSION, e=AUTHOR_EMAIL, w=AUTHOR_WEBSITE, log=LOG_FILE)))

    def on_closing(self):
        # Stop the self-rescheduling UI pump BEFORE destroy so its pending
        # after-callback can't fire against a torn-down window (a harmless but
        # noisy "invalid command name ..._pump_ui_queue" TclError otherwise).
        self._closing = True
        try:
            if self._pump_after_id is not None:
                self.after_cancel(self._pump_after_id)
        except Exception:
            pass
        self.save_all_settings()
        self.play_flag = False
        winkiosk.keep_awake(False)
        if self.player:
            # Block until every helper process is gone, so closing the window
            # never leaves an orphaned ffplay/yt-dlp running (the async Stop used
            # by the button is the wrong choice here - the process is exiting).
            self.player.stop(join=True)
        self.destroy()


def main():
    _init_paths()
    add_to_path()
    # DPI awareness must be set BEFORE the first window / monitor enumeration:
    # with it, every coordinate in the app is a true physical pixel, so windows
    # land exactly fullscreen even on mixed 125%/150% multi-monitor setups.
    log.info("dpi awareness: %s", winkiosk.set_dpi_awareness())
    # One kiosk, one instance: run-at-startup plus a manual double-click must
    # not race two walls (double decode load + fullscreen fights = "freeze").
    if not winkiosk.acquire_single_instance():
        log.warning("another Video Tiler instance is already running; exiting")
        return
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
