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
import shutil
import logging
import threading
import subprocess
import webbrowser
import urllib.request
from logging.handlers import RotatingFileHandler

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont

import yt_dlp
import psutil

# monitor_utils sits next to this file; ensure its folder is importable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor_utils


# --------------------------------------------------------------------------- #
#  Constants
# --------------------------------------------------------------------------- #
APP_NAME = "videotiler"
PROGRAM_VERSION = "1.1"
PROGRAM_AUTHOR = "Bluesun"
AUTHOR_EMAIL = "smtv.bot@gmail.com"
AUTHOR_WEBSITE = "https://github.com/translation-robot/video-tiler"

DEFAULT_URL = "https://www.youtube.com/watch?v=ZzWBpGwKoaI"
WHY_TILING_URL = "https://suprememastertv.com/en1/v/245875177398.html"
SUPPORTED_WEB_SITES = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"
SOURCE_CODE_GITHUB = "https://github.com/translation-robot/video-tiler"
UPDATE_VERSION_URL = "https://raw.githubusercontent.com/translation-robot/video-tiler/master/VERSION"
RELEASES_URL = "https://github.com/translation-robot/video-tiler/releases"

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
    """Locate yt-dlp / ffmpeg / ffplay on PATH, next to this file, or in ./bin."""
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
    unattended kiosk did when no console is attached (windowed/frozen builds)."""
    if any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        return
    log.setLevel(logging.INFO)
    try:
        handler = RotatingFileHandler(
            LOG_FILE, maxBytes=512 * 1024, backupCount=2, encoding='utf-8')
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)-7s %(message)s', '%Y-%m-%d %H:%M:%S'))
        log.addHandler(handler)
    except Exception:
        pass
    log.info("=== Video Tiler %s starting (pid %s) ===", PROGRAM_VERSION, os.getpid())


def read_settings():
    data = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            data.update(saved)
    except Exception:
        pass
    return data


def write_settings(data):
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
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
def select_format(quality, divisions):
    """yt-dlp -f selector. Manual quality wins; Auto lowers resolution as the
    grid gets denser (a 50x50 tile needs far less than 1080p). Always ends in
    /best so playback never fails just because a resolution is unavailable."""
    heights = {'1080p': 1080, '720p': 720, '480p': 480, '360p': 360, '240p': 240, '144p': 144}
    h = heights.get(quality)
    if h is None:  # Auto
        h = 360 if divisions <= 17 else (240 if divisions <= 35 else 144)
    return "best[height<={h}]/best[height<={h2}]/best".format(h=h, h2=h + 360)


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

    HEALTHY_SECONDS = 60   # a session this long resets backoff + self-heal
    HEAL_AFTER_FAILS = 2   # consecutive failures before we update yt-dlp

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
        self.ytdlp_process = None
        self.ffplay_processes = []
        self._fanout_thread = None
        self._fail_count = 0
        self._healed = False

    # ---- title (async; never blocks the GUI) ------------------------------ #
    def fetch_title_async(self, callback):
        def worker():
            title = self.url
            try:
                opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(self.url, download=False, process=False)
                    title = info.get('title') or self.url
            except Exception:
                pass
            self.title = title
            self.app.post_ui(lambda: callback(title))
        threading.Thread(target=worker, daemon=True).start()

    # ---- command building ------------------------------------------------- #
    def _yt_dlp_cmd(self):
        # Plain-attribute mirrors (set on the main thread) - workers must never
        # read Tk variables directly. See App._sync_runtime_options.
        quality = getattr(self.app, 'opt_quality', 'Auto')
        return [
            self.yt_dlp_path, self.url,
            '--extractor-args', 'youtube:player_client=' + YT_PLAYER_CLIENTS,
            '-4', '--quiet', '--no-warnings',
            '--retries', '10', '--socket-timeout', '15',
            '-f', select_format(quality, self.divisions),
            '-o', '-',
        ]

    def _targets(self):
        monitors = monitor_utils.list_monitors()
        multi = bool(getattr(self.app, 'opt_multi_monitor', False))
        selected = list(getattr(self.app, 'selected_monitor_indices', []))
        return monitor_utils.select_monitors(monitors, selected, multi), multi

    def _ffplay_cmd(self, mon, multi, muted):
        vf, ow, oh = monitor_utils.tile_filter_for(mon['width'], mon['height'], self.divisions)
        win = monitor_utils.window_opts_for(mon, ow, oh) if multi else ['-fs']
        audio = ['-an'] if muted else []
        return [self.ffplay_path, '-', '-vf', vf,
                '-autoexit', '-loglevel', 'error', '-hide_banner'] + audio + win

    # ---- process lifecycle ------------------------------------------------ #
    def _start(self):
        self._terminate()
        self.ytdlp_process = subprocess.Popen(
            self._yt_dlp_cmd(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)

        targets, multi = self._targets()
        muted = bool(getattr(self.app, 'opt_mute', False))
        log.info("start: url=%s divisions=%d windows=%d multi=%s muted=%s",
                 self.url, self.divisions, len(targets), multi, muted)

        if len(targets) <= 1:
            # Single window reads the download directly.
            proc = subprocess.Popen(
                self._ffplay_cmd(targets[0], multi, muted),
                stdin=self.ytdlp_process.stdout, stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW)
            self.ffplay_processes = [proc]
        else:
            # One window per monitor; fan the single download out to all of them.
            # Only the first keeps audio (the rest are muted) to avoid echo.
            stdins = []
            self.ffplay_processes = []
            for i, mon in enumerate(targets):
                proc = subprocess.Popen(
                    self._ffplay_cmd(mon, multi, muted or i > 0),
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW)
                self.ffplay_processes.append(proc)
                stdins.append(proc.stdin)
            self._fanout_thread = threading.Thread(
                target=self._fanout, args=(self.ytdlp_process.stdout, stdins), daemon=True)
            self._fanout_thread.start()

    def _fanout(self, source, stdins):
        """Copy the one download to every ffplay stdin."""
        try:
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                for s in list(stdins):
                    try:
                        s.write(chunk)
                        s.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        # This player went away: drop it AND close its writer
                        # now, so it is never left for the GC to finalize.
                        try:
                            stdins.remove(s)
                        except ValueError:
                            pass
                        try:
                            s.close()
                        except Exception:
                            pass
                if not stdins:
                    break
        except Exception:
            pass
        finally:
            for s in list(stdins):
                try:
                    s.close()
                except Exception:
                    pass

    def _alive(self):
        # Require the download AND every player window to be alive. Using "all"
        # (not "any") means a single dead monitor in a multi-screen wall is
        # detected and triggers a clean relaunch, instead of leaving that screen
        # black while the others keep playing.
        if not self.ytdlp_process or self.ytdlp_process.poll() is not None:
            return False
        if not self.ffplay_processes:
            return False
        return all(p.poll() is None for p in self.ffplay_processes)

    def _terminate(self):
        # Kill the download FIRST so the fan-out thread sees EOF on its source,
        # exits its loop, and closes the pipe writers in its own `finally`. Then
        # join it, so the writers are released deterministically by our code -
        # never left for the garbage collector, which would try to flush to a
        # dead pipe and print an OSError from the finalizer on Windows.
        if self.ytdlp_process:
            _kill_tree(self.ytdlp_process)
            self.ytdlp_process = None
        t = self._fanout_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2)
        self._fanout_thread = None
        for p in self.ffplay_processes:
            # Belt-and-braces: the fan-out already closed these on EOF; a second
            # close is a harmless no-op (and covers the single-window path).
            if p.stdin:
                try:
                    p.stdin.close()
                except Exception:
                    pass
            _kill_tree(p)
        self.ffplay_processes = []

    # ---- worker entry point ---------------------------------------------- #
    def run(self):
        self.play_flag = True
        backoff = 3
        while self.play_flag:
            self._start()
            started = time.time()
            self.app.post_ui(lambda: self.app.update_status(
                "Playing '{}'".format(self.title), color='black'))
            # Watch the handles we own - cheap and reliable.
            while self.play_flag and self._alive():
                time.sleep(0.4)
            if not self.play_flag:
                break

            ran_for = time.time() - started
            self._terminate()
            if ran_for >= self.HEALTHY_SECONDS:
                # A good long session: forget past failures.
                backoff, self._fail_count, self._healed = 3, 0, False

            if not getattr(self.app, 'opt_auto_restart', True):
                log.info("playback ended (ran %.0fs); auto-restart off -> stop", ran_for)
                break

            # Self-heal: repeated quick failures usually mean YouTube changed
            # something, so update yt-dlp once (its maintainers ship the fix).
            self._fail_count += 1
            log.info("playback dropped after %.0fs (failure #%d); backoff %ds",
                     ran_for, self._fail_count, backoff)
            if self._fail_count >= self.HEAL_AFTER_FAILS and not self._healed:
                self._healed = True
                log.info("self-heal: updating yt-dlp after repeated failures")
                self.app.update_yt_dlp(silent=True)

            self.app.post_ui(lambda b=backoff: self.app.update_status(
                "Reconnecting in {}s...".format(b), color='#b06a00'))
            waited = 0.0
            while self.play_flag and waited < backoff:
                time.sleep(0.25)
                waited += 0.25
            backoff = min(backoff * 2, 30)  # exponential, capped

        self._terminate()
        self.app.post_ui(lambda: self.app._on_player_finished(self))

    def stop(self):
        if self.play_flag:
            log.info("stop requested")
        self.play_flag = False
        self._terminate()


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

        try:
            icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'img', 'app.ico')
            self.iconbitmap(icon)
        except Exception:
            pass

        self.create_menu()
        self.create_widgets()
        self.load_all_settings()
        self.after(50, self._pump_ui_queue)

        if not self.url_entry.get():
            self.url_entry.set(DEFAULT_URL)
        self.update_video_title()

        if self.autoplay.get():
            self.after(2500, self.play_video)
        self.after(4000, lambda: self.check_for_updates(silent=True))

    # ---- thread-safe UI marshaling --------------------------------------- #
    def post_ui(self, fn):
        self._ui_queue.put(fn)

    def _pump_ui_queue(self):
        try:
            while True:
                fn = self._ui_queue.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(50, self._pump_ui_queue)

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
        self.divisions_spinbox = tk.Spinbox(self, from_=1, to=64, increment=1, width=5)
        self.divisions_spinbox.grid(row=3, column=2, padx=10, pady=10, sticky='w')

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
        self.bind_all("<Escape>", lambda e: self.stop_video())
        self.bind_all("<F5>", lambda e: self.play_video())
        self.bind_all("<space>", self._space_shortcut)

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

    def save_all_settings(self):
        self._sync_runtime_options()
        try:
            url = self.url_entry.get().strip()
            urls = list(self.url_entry['values'])
            if url and url not in urls:
                urls.append(url)
                self.url_entry['values'] = urls
            write_settings({
                'url': url, 'urls': urls,
                'divisions': self._safe_divisions(),
                'auto_restart': bool(self.auto_restart_video.get()),
                'multi_monitor': bool(self.multi_monitor.get()),
                'selected_monitor_indices': list(self.selected_monitor_indices),
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
            avail = [m['index'] for m in monitor_utils.list_monitors()]
            sel = [i for i in data.get('selected_monitor_indices', []) if i in avail]
            if sel:
                self.selected_monitor_indices = sel
            d = int(data.get('divisions', 3))
            if 1 <= d <= 64:
                self.divisions_spinbox.delete(0, tk.END)
                self.divisions_spinbox.insert(0, str(d))
            self.auto_restart_video.set(bool(data.get('auto_restart', True)))
            self.multi_monitor.set(bool(data.get('multi_monitor', False)))
            self.mute.set(bool(data.get('mute', False)))
            urls = data.get('urls') or list(self.url_entry['values'])
            if urls:
                self.url_entry['values'] = urls
            if data.get('quality') in QUALITY_CHOICES:
                self.quality.set(data['quality'])
            self.autoplay.set(bool(data.get('autoplay', False)))
            self.run_at_startup.set(bool(data.get('run_at_startup', False)))
            if data.get('theme') in self.THEMES:
                self.theme_var.set(data['theme'])
                self.apply_theme()
            if data.get('url'):
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

    def apply_theme(self, theme=None):
        p = self.THEMES.get(theme or self.theme_var.get(), self.THEMES['Light'])
        BG, FG, FIELD, SUB, SUB_FG = p['bg'], p['fg'], p['field'], p['sub'], p['sub_fg']

        def style_widget(w):
            cls = w.winfo_class()
            try:
                if cls == "Label":
                    w.configure(bg=BG, fg=FG)
                elif cls in ("Frame", "Toplevel", "Tk"):
                    w.configure(bg=BG)
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

        try:
            self.configure(bg=BG)
            style_widget(self)
            self.play_button.configure(bg=self.ACCENT, fg='white',
                                       activebackground=self.ACCENT_HOVER, activeforeground='white')
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
        def worker():
            path = find_executable('yt-dlp')
            if not path:
                if not silent:
                    self.post_ui(lambda: messagebox.showwarning(
                        "Update yt-dlp", "yt-dlp was not found on PATH."))
                return
            try:
                self.post_ui(lambda: self.update_status("Updating yt-dlp...", color='#b06a00'))
                res = subprocess.run([path, '-U'], capture_output=True, text=True,
                                     timeout=180, creationflags=CREATE_NO_WINDOW)
                out = ((res.stdout or '') + (res.stderr or '')).strip()
                self.post_ui(lambda: self.update_status("yt-dlp update finished."))
                if not silent:
                    self.post_ui(lambda: messagebox.showinfo("Update yt-dlp", out[-800:] or "Done."))
            except Exception as e:
                self.post_ui(lambda: self.update_status("yt-dlp update failed."))
                if not silent:
                    self.post_ui(lambda: messagebox.showwarning(
                        "Update yt-dlp", "Update failed:\n{}".format(e)))
        threading.Thread(target=worker, daemon=True).start()

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
        if not url:
            return
        # Debounce: <FocusOut> fires often, so skip a refetch when the URL has
        # not changed since the last fetch we kicked off.
        if not force and url == self._last_title_url:
            return
        self._last_title_url = url
        probe = Player(self, url, self._safe_divisions())
        probe.fetch_title_async(lambda t: self.video_title_label.config(text=t))

    def _safe_divisions(self):
        try:
            return max(1, min(64, int(self.divisions_spinbox.get())))
        except Exception:
            return 3

    def play_video(self):
        self.stop_video()
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("URL", "Please enter a video URL.")
            return
        divisions = self._safe_divisions()
        self.save_all_settings()  # also syncs the runtime-option mirrors
        self.player = Player(self, url, divisions)
        if not self.player.tools_ok:
            self.player = None
            messagebox.showerror("Missing tools",
                                 "yt-dlp, ffmpeg and ffplay must be on PATH (or next to the app).")
            return
        self.play_flag = True
        self.play_button.config(state=tk.DISABLED)
        self.update_status("Starting...", color='#b06a00')
        self.player.fetch_title_async(lambda t: self.video_title_label.config(text=t))
        self.video_thread = threading.Thread(target=self.player.run, daemon=True)
        self.video_thread.start()

    def stop_video(self):
        if self.player:
            self.player.stop()
        self.play_flag = False
        self.play_button.config(state=tk.NORMAL)
        self.update_status("Ready")

    def _on_player_finished(self, player):
        """Called on the main thread when a Player's run() loop exits. Only the
        currently-active player may flip the UI back, so a stale old player
        (already replaced by a new Play) can't re-enable the button mid-playback."""
        if player is not self.player:
            return
        self.play_flag = False
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
            "  The player is full screen - press Esc or 'q' on it, or Alt+Tab back\n"
            "  here and press Stop.\n\n"
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
            "PLATFORM\n"
            "  Tested on Windows; macOS/Linux supported on a best-effort basis.\n\n"
            "Version: {v}\nEmail: {e}\nWebsite: {w}".format(
                v=PROGRAM_VERSION, e=AUTHOR_EMAIL, w=AUTHOR_WEBSITE)))

    def on_closing(self):
        self.save_all_settings()
        self.stop_video()
        self.destroy()


def main():
    _init_paths()
    add_to_path()
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
