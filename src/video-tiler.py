import yt_dlp
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont
import webbrowser
from screeninfo import get_monitors
from prettytable import PrettyTable
import os
import shutil
import sys
import traceback
import psutil  # For process handling
import time
import appdirs
# Windows-only window-detection helpers; optional so the app still imports on macOS/Linux.
try:
    import pygetwindow as gw
except Exception:
    gw = None
try:
    import win32process
except Exception:
    win32process = None
import json
import urllib.request
import monitor_utils


# Left to do:
# Add a loop check box
# Load and save number of divisions on startup and playing video
# Check layout + menu size is too small
# Auto play on start

# Constants for program version and author
APP_NAME = 'videotiler'
PROGRAM_VERSION = "1.0"
PROGRAM_AUTHOR = "Bluesun"
AUTHOR_EMAIL = "smtv.bot@gmail.com"
AUTHOR_WEBSITE = "https://github.com/translation-robot/video-tiler"

# URL for "Why Tiling" page
WHY_TILING_URL = "https://suprememastertv.com/en1/v/245875177398.html"
SUPPORTED_WEB_SITES = "https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md"
SOURCE_CODE_GITHUB = "https://github.com/translation-robot/video-tiler"
# Update checking: a plain VERSION file in the repo, and the releases page to send users to.
UPDATE_VERSION_URL = "https://raw.githubusercontent.com/translation-robot/video-tiler/master/VERSION"
RELEASES_URL = "https://github.com/translation-robot/video-tiler/releases"

json_configuration_url='https://raw.githubusercontent.com/translation-robot/video-tiler/main/src/configuration/configuration.json'

# File to store the number of divisions
DIVISIONS_FILE_NAME = 'divisions.txt'

DEFAULT_URL = "https://www.youtube.com/watch?v=ZzWBpGwKoaI"

DefaultJsonConfiguration = """{
    "streaming_url_array": ["https://www.youtube.com/watch?v=ZzWBpGwKoaI", "https://x.com/i/broadcasts/1gqGvNDqqZgGB"],
    "streaming_url_user_added_array": []
}"""

def add_to_path():
    # Get the directory of the current script
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    
    # Add the script directory to PATH
    if script_dir not in os.environ['PATH']:
        os.environ['PATH'] = script_dir + os.pathsep + os.environ['PATH']
    
    # Check if the 'bin' subdirectory exists
    bin_dir = os.path.join(script_dir, 'bin')
    if os.path.isdir(bin_dir):
        # Add the 'bin' subdirectory to PATH
        if bin_dir not in os.environ['PATH']:
            os.environ['PATH'] = bin_dir + os.pathsep + os.environ['PATH']
            
            
def find_executable(executable_name):
    """Find the executable in PATH, script directory, or bin directory."""
    def is_executable(path):
        return os.path.isfile(path) and os.access(path, os.X_OK)

    # Check PATH
    path = shutil.which(executable_name)
    if path and is_executable(path):
        return path

    # Check the directory of the script or executable
    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    exe_path = os.path.join(base_dir, executable_name)
    if is_executable(exe_path):
        return exe_path

    # Check in a 'bin' directory
    bin_dir = os.path.join(base_dir, 'bin', executable_name)
    if is_executable(bin_dir):
        return bin_dir

    return None

# Get the application directory
app_data_dir = appdirs.user_data_dir(APP_NAME)
DIVISIONS_FILE = os.path.join(app_data_dir, DIVISIONS_FILE_NAME)

# Ensure the application directory exists
os.makedirs(app_data_dir, exist_ok=True)

def read_divisions():
    """Read the number of divisions from a file."""
    if os.path.exists(DIVISIONS_FILE):
        with open(DIVISIONS_FILE, 'r') as file:
            content = file.read().strip()
            if content.isdigit():
                return int(content)
    return 3  # Default number of divisions if the file does not exist or is invalid

def write_divisions(divisions):
    """Write the number of divisions to a file."""
    with open(DIVISIONS_FILE, 'w') as file:
        file.write(str(divisions))


# Single JSON file that remembers every GUI choice between launches.
SETTINGS_FILE = os.path.join(app_data_dir, 'settings.json')

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

QUALITY_CHOICES = ['Auto', '1080p', '720p', '480p', '360p', '240p', '144p']


def quality_to_format(quality):
    """Translate a quality label into a yt-dlp -f selector, or None for Auto."""
    heights = {'1080p': 1080, '720p': 720, '480p': 480, '360p': 360, '240p': 240, '144p': 144}
    h = heights.get(quality)
    if not h:
        return None
    return "best[height<={h}]/best[height<={h2}]/best".format(h=h, h2=h + 120)


# ---- Optional "run at Windows startup" support (per-user, reversible) ----
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "SMTV_VideoTiler"


def _startup_command():
    """Command Windows should run at login to start this app.

    Uses the *currently running* interpreter/executable so the startup launch
    has the same working environment (venv or frozen build) - launching a bare
    'python' could miss the installed dependencies.
    """
    if getattr(sys, 'frozen', False):
        return '"{}"'.format(sys.executable)
    return '"{}" "{}"'.format(sys.executable, os.path.abspath(__file__))


def set_run_at_startup(enabled):
    """Add or remove this app from the current user's Windows startup. Returns True on success."""
    try:
        import winreg
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
        print("run-at-startup change failed: {}".format(e))
        return False


def read_settings():
    """Load persisted settings, merged over the defaults."""
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
    """Persist the given settings dict (best effort)."""
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


class TimerWindow:
    def __init__(self, parent, title, question, duration):
        self.parent = tk.Toplevel(parent)  # Create a separate window
        self.parent.title(title)  # Set the window title dynamically
        self.duration = duration
        self.remaining = duration
        
        self.question_var = tk.StringVar(value=question)
        self.do_not_ask_var = tk.BooleanVar(value=False)  # Checkbox state
        self.result = None  # Store the result of OK/Cancel
        self.expired = False  # Track if the timer expired

        # Create UI elements
        self.label = tk.Label(self.parent, textvariable=self.question_var)
        self.label.pack(pady=20)

        self.timer_label = tk.Label(self.parent, text=f"Time remaining: {self.remaining} seconds")
        self.timer_label.pack(pady=10)

        #self.checkbox = tk.Checkbutton(self.parent, text="Do not ask again", variable=self.do_not_ask_var)
        #self.checkbox.pack(pady=10)

        #self.ok_button = tk.Button(self.parent, text="OK", command=self.ok)
        #self.ok_button.pack(side=tk.LEFT, padx=20)

        self.cancel_button = tk.Button(self.parent, text="Cancel", command=self.cancel)
        self.cancel_button.pack(side=tk.RIGHT, padx=20)

        self.update_timer()
        
    def update_timer(self):
        if self.remaining > 0:
            self.remaining -= 1
            self.timer_label.config(text=f"Time remaining: {self.remaining} seconds")
            self.parent.after(1000, self.update_timer)  # Call this function again after 1 second
        else:
            self.expired = True  # Mark as expired
            self.cancel()  # Automatically call cancel when time is up

    def ok(self):
        self.result = True  # OK was pressed
        self.parent.destroy()

    def cancel(self):
        self.result = False  # Cancel was pressed
        self.parent.destroy()
        
        
class YouTubeVideo:
    def __init__(self, parent, url, divisions=None, verbose=True):
        self.parent = parent  # Store reference to the Tkinter parent (App instance)
        self.url = url
        self.timer_window = None
        self.ytdlp_process = None
        self.ytdlp_process = None
        self.ytdlp_is_valid = False
        
        try:
            if divisions is None:
                self.divisions = read_divisions()
            else:
                self.divisions = divisions
        except:
            self.divisions = 3
            write_divisions(self.divisions)
        
        self.verbose = verbose
        self.format = None
        self.title = ""
        self.process = None
        self.process_pid = None
        self.yt_dlp_path = find_executable('yt-dlp')
        self.ffmpeg_path = find_executable('ffmpeg')
        self.ffplay_path = find_executable('ffplay')
        self.play_flag = None
        self.ffplay_processes = []   # one ffplay per active monitor
        self._fanout_thread = None

        if not self.yt_dlp_path or not self.ffmpeg_path or not self.ffplay_path:
            raise FileNotFoundError("One or more required executables (yt-dlp, ffmpeg, ffplay) not found.")

        try:
            self._get_video_info()
            self._get_screen_resolution()
            self.ytdlp_is_valid = True
        except Exception as e:
            # Handle errors here (e.g., invalid URL, video not available)
            print(f"Error creating YouTube video: {e}")
            return
        #self._choose_format()

    def _get_video_info(self):
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(self.url, download=False)
            self.title = result.get('title', 'Unknown Title')

    def _get_screen_resolution(self):
        monitor = get_monitors()[0]
        self.screen_width = monitor.width
        self.screen_height = monitor.height
        
    def _choose_format(self):
        self.format = None
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(self.url, download=False)
            formats = result.get('formats', [])
            #print(formats)
            #input("Formats listed (all)")

            # Filter out vp09 codecs
            video_audio_formats = [
                f for f in formats 
                if 'vcodec' in f and 'acodec' in f 
                and not f.get('vcodec', '').startswith('vp09')  # Exclude vp09 codecs
                and f.get('vcodec') != 'none' 
                and f.get('acodec') != 'none'
            ]
            video_formats = [
                f for f in formats 
                if f.get('vcodec') and not f['vcodec'].startswith('vp09')  # Exclude vp09 codecs
                and f.get('acodec') == 'none'
                and f.get('vcodec') is not None
            ]
            audio_formats = [
                f for f in formats 
                if f.get('vcodec') == 'none'
                #    and 'acodec' in f  # Exclude vp09 codecs
            ]
            
            #print("Audio codecs")
            #print(audio_formats)
            #input("Formats audio (all)")

            # Sort formats
            video_audio_formats.sort(key=lambda x: (x.get('height', 0), x.get('width', 0)))
            video_formats.sort(key=lambda x: (x.get('height', 0), x.get('width', 0)))
            audio_formats.sort(key=lambda x: x.get('abr') or 0)

            # Prepare pretty table
            if self.verbose:
                table = PrettyTable()
                table.field_names = ["Format ID", "Resolution", "Type", "VCodec", "ACodec", "Bitrate (kbps)"]

                for f in video_audio_formats:
                    table.add_row([
                        f['format_id'],
                        f.get('resolution', 'Unknown'),
                        "Video+Audio",
                        f.get('vcodec', 'Unknown'),
                        f.get('acodec', 'Unknown'),
                        f.get('abr', 'N/A') if 'abr' in f else 'N/A'
                    ])
                
                for f in video_formats:
                    table.add_row([
                        f['format_id'],
                        f.get('resolution', 'Unknown'),
                        "Video",
                        f.get('vcodec', 'Unknown'),
                        'N/A',
                        'N/A'
                    ])
                
                for f in audio_formats:
                    table.add_row([
                        f['format_id'],
                        "Audio only",
                        "Audio",
                        'N/A',
                        f.get('acodec', 'Unknown'),
                        f.get('abr', 'N/A') if 'abr' in f else 'N/A'
                    ])
                
                print(table)

            tile_width = self.screen_width / self.divisions
            tile_height = self.screen_height / self.divisions

            selected_format = None

            # Step 1: Prefer a format that has both video and audio
            for f in video_audio_formats:
                if f.get('width', 0) >= tile_width and f.get('height', 0) >= tile_height:
                    selected_format = f
                    break

            # Step 2: If no suitable video+audio format, combine separate video and audio formats
            if not selected_format or selected_format is None:
                selected_video_format = None
                selected_audio_format = None

                for f in video_formats:
                    if f.get('width', 0) >= tile_width and f.get('height', 0) >= tile_height:
                        selected_video_format = f
                        break

                if selected_video_format is None and video_formats:
                    selected_video_format = video_formats[-1]

                if audio_formats:
                    selected_audio_format = audio_formats[-1]  # Choosing the highest bitrate available

                if selected_video_format and selected_audio_format:
                    selected_format = {
                        'format_id': f"{selected_video_format['format_id']}+{selected_audio_format['format_id']}",
                        'resolution': f"{selected_video_format.get('width', 'Unknown')}x{selected_video_format.get('height', 'Unknown')}",
                    }

            if selected_format:
                self.format = selected_format['format_id']
                if self.verbose:
                    print(f"Screen resolution: {self.screen_width}x{self.screen_height}")
                    print(f"Tile resolution: {tile_width}x{tile_height}")
                    print(f"Selected format ID: {self.format} - {selected_format['resolution']}")
            else:
                print("No suitable format found.")

    def check_timer_result(self, timer_window):
        self.parent.wait_window(timer_window.parent)  # Wait for the TimerWindow to close

        if timer_window.expired:
            print("The timer expired before user response.")
        elif timer_window.result:  # If OK was pressed
            print("User chose to restart the video.")
        else:
            print("User chose to cancel.")
            self.parent.update_status("Ready")
            
    def _is_ffmpeg_descendant_with_window(self, timeout=30):
        current_pid = psutil.Process().pid
        start_time = time.time()

        def has_window(pid):
            # Real window detection is Windows-only; on macOS/Linux treat a live player as visible.
            if gw is None or win32process is None:
                return True
            try:
                for window in gw.getAllWindows():
                    if window._hWnd:  # Ensure the window handle is valid
                        _, window_pid = win32process.GetWindowThreadProcessId(window._hWnd)
                        if window_pid == pid:
                            return True
            except Exception:
                return True
            return False

        while (time.time() - start_time) < timeout:
            # Get the current process
            current_process = psutil.Process(current_pid)

            # Check for yt-dlp child processes
            yt_dlp_exists = any('yt-dlp' in (child.name() or '').lower() for child in current_process.children(recursive=True))

            if not yt_dlp_exists:
                print("no yt-dlp process exists")
                return False  # Return False immediately if no yt-dlp child process exists

            # Iterate over the children of the current process to check for ffplay
            for child in current_process.children(recursive=True):
                try:
                    # Check for yt-dlp child processes
                    ffplay_exists = any('ffplay' in (child.name() or '').lower() for child in current_process.children(recursive=True))

                    if not ffplay_exists:
                        print("no yt-dlp process exists")
                        return False  # Return False immediately if no yt-dlp child process exists
                        
                    # Check if the child process is ffplay
                    if 'ffplay' in (child.name() or '').lower():
                        if has_window(child.pid):  # Use child.pid to check for the window
                            return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            time.sleep(1)  # Check every second

        return False



    def _fanout(self, source, stdins):
        """Copy the single download to every ffplay stdin (multi-monitor mode)."""
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
                        try:
                            stdins.remove(s)
                        except ValueError:
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

    def _launch_players(self):
        """Start one ffplay per active monitor, fed by the single yt-dlp download.

        - One monitor  -> ffplay reads the download directly.
        - Many monitors -> a fan-out thread copies the download to every ffplay,
          so there is still only ONE network download.
        Only the first window keeps audio; the rest are muted to avoid echo.
        """
        try:
            monitors = monitor_utils.list_monitors()
            multi = bool(self.parent.multi_monitor.get())
            selected = list(self.parent.selected_monitor_indices)
            mute = bool(self.parent.mute.get())
        except Exception:
            monitors = monitor_utils.list_monitors()
            multi, selected, mute = False, [], False

        targets = monitor_utils.select_monitors(monitors, selected, multi)
        base_flags = ['-autoexit', '-loglevel', 'error', '-hide_banner']

        # Kill any players left over from a previous attempt so they can't linger
        # as orphan windows (e.g. after a partial failure on one monitor).
        for p in (self.ffplay_processes or []):
            try:
                p.kill()
            except Exception:
                pass
        self.ffplay_processes = []

        if len(targets) <= 1:
            mon = targets[0]
            vf, ow, oh = monitor_utils.tile_filter_for(mon['width'], mon['height'], self.divisions)
            win = monitor_utils.window_opts_for(mon, ow, oh) if multi else ['-fs']
            audio = ['-an'] if mute else []
            cmd = [self.ffplay_path, '-', '-vf', vf] + base_flags + audio + win
            print(cmd)
            proc = subprocess.Popen(cmd, stdin=self.ytdlp_process.stdout, stderr=subprocess.PIPE)
            self.ffplay_processes.append(proc)
        else:
            stdins = []
            for i, mon in enumerate(targets):
                vf, ow, oh = monitor_utils.tile_filter_for(mon['width'], mon['height'], self.divisions)
                win = monitor_utils.window_opts_for(mon, ow, oh)
                audio = [] if (i == 0 and not mute) else ['-an']
                cmd = [self.ffplay_path, '-', '-vf', vf] + base_flags + audio + win
                print(cmd)
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                self.ffplay_processes.append(proc)
                stdins.append(proc.stdin)
            self._fanout_thread = threading.Thread(
                target=self._fanout, args=(self.ytdlp_process.stdout, stdins), daemon=True)
            self._fanout_thread.start()

        self.ffplay_process = self.ffplay_processes[0]
        self.process_pid = self.ffplay_process.pid

    def play_video(self):
        print("play_video")
        self.play_flag = True
        #if not self.format:
        #    print("No suitable format found.")
        #    return
        self._choose_format()
        
        try:
            write_divisions(self.divisions)
        except:
            pass

        useragent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"


        while self.play_flag:  # Check play flag to 
            self.url = self.parent.url_entry.get()
            self.divisions = int(self.parent.divisions_spinbox.get())
            write_divisions(self.divisions)
            # yt-dlp command (a manual Quality choice overrides the auto-picked format)
            try:
                quality_fmt = quality_to_format(self.parent.quality.get())
            except Exception:
                quality_fmt = None
            chosen_fmt = quality_fmt or self.format
            if chosen_fmt is not None:
                yt_dlp_command = [
                    self.yt_dlp_path, self.url, '--user-agent', useragent, '-4', '-f', chosen_fmt, '-o', '-',
                    '--quiet', '--no-warnings'
                ]
            else:
                #self.format
                #print(f"Using format {self.format}") '--user-agent', useragent, 
                yt_dlp_command = [
                    self.yt_dlp_path, self.url, '-4', '-f', 'bestvideo+bestaudio/best', '-o', '-',
                    '--quiet', '--no-warnings'
                ]
            
            # (ffplay windows are built per active monitor in _launch_players, below)

        
        
        
            if self.ytdlp_process:
                self.ytdlp_process.terminate()
                self.ytdlp_process.wait()
                exit_code = self.ytdlp_process.returncode
                print(f"yt-dlp process exited with exit code {exit_code}")
                print("Previous yt-dlp process terminated.")
                
            # Use subprocess.PIPE to handle the pipe
            print(yt_dlp_command)
            self.ytdlp_process = subprocess.Popen(
                yt_dlp_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            
            self._launch_players()
            
            print(f"Players started on {len(self.ffplay_processes)} monitor(s).")

            # Monitor yt-dlp process
            ffplay_alive = False
            while self.play_flag:  # Continue to monitor if play flag is set
                try:
                    print("Loop 1")
                    if self._is_ffmpeg_descendant_with_window():
                        if ffplay_alive == False:
                            print("ffmpeg window is running.")
                        ffplay_alive = True
                    else:
                        if ffplay_alive == True:
                            print("No descendant ffmpeg process with a window found.")
                        ffplay_alive = False
                        self.parent.play_button.config(state=tk.NORMAL)
                        
                    
                    # If ffplay is not running but yt-dlp is, restart yt-dlp and ffplay
                    if not ffplay_alive:
                        # Self-heal: a repeated failure usually means YouTube changed something,
                        # so quietly update yt-dlp once (the GitHub-maintained fix) before retrying.
                        self._fail_count = getattr(self, '_fail_count', 0) + 1
                        if self._fail_count >= 2 and not getattr(self, '_healed', False):
                            self._healed = True
                            try:
                                self.parent.update_yt_dlp(silent=True)
                            except Exception:
                                pass
                        #self.timer_window = TimerWindow(self, self.parent, title="Action Required", question="Video was stopped, do you want to restart it?", duration=10)
                        if self.parent.auto_restart_video.get() == True:
                            if self.play_flag == True:
                                print("Starting OK/Cancell window")
                                self.timer_window = TimerWindow(parent=self.parent, title="Action Required", question="Video was stopped and will be restarted automatically.", duration=10)
                            else:
                                return
                        else:
                            self.parent.update_status(f"Ready")
                            return
                        
                        #self.parent.root.wait_window(timer_window.parent)
                        self.parent.wait_window(self.timer_window.parent)
                        if self.timer_window.expired:
                            print("The timer expired before user response.")
                            self.timer_window.result = True
                        # Access the result and the checkbox value after the window has been closed
                        if self.timer_window.result:  # If OK was pressed
                            print("User chose to restart the video.")
                        else:
                            print("User chose to cancel or timer expired.")
                            self.parent.update_status(f"Ready")
                                        
                            self.ytdlp_process.wait()
                            exit_code = self.ytdlp_process.returncode
                            print(f"yt-dlp process exited with exit code {exit_code}")
                            print("Previous yt-dlp process terminated.")
                            return

                        # Check if "Do not ask again" was selected
                        if self.timer_window.do_not_ask_var:
                            print("User checked 'Do not ask again'.")
                        else:
                            print("User did not check 'Do not ask again'.")
                            
                        
                        print("ffplay process terminated. Restarting yt-dlp and ffplay...")
                        #self.stop_video()  # Ensure old processes are terminated
                        break
    
                    if self.ytdlp_process.poll() is not None:  # Check if process is terminated
                        #self.timer_window = TimerWindow(self, self.parent, title="Action Required", question="Video was stopped, do you want to restart it?", duration=10)
                        #self.timer_window = TimerWindow(parent=self.parent, title="Action Required", question="Video was stopped, do you want to restart it?", duration=10)
                        print("yt-dlp process terminated. Restarting...")
                        
                        exit_code = self.ytdlp_process.returncode
                        print(f"yt-dlp process exited with exit code {exit_code}")
                        print("Previous yt-dlp process terminated.")
                        self.play_video
                        break
                except:
                    var = traceback.format_exc()
                    print(var)
                    print("Sleep 1")
                time.sleep(1)  # Check every second
        self.parent.play_button.config(state=tk.NORMAL)

    def stop_video(self):
        self.play_flag = False
        if self.timer_window is not None:
            try:
                self.timer_window.parent.destroy()
            except Exception:
                pass

        def _kill(pid):
            try:
                p = psutil.Process(pid)
                for child in p.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                p.kill()
            except psutil.NoSuchProcess:
                pass
            except Exception:
                pass

        # Close every ffplay window we opened (one per monitor)
        for proc in list(getattr(self, 'ffplay_processes', []) or []):
            _kill(proc.pid)
        # Stop the single download
        if getattr(self, 'ytdlp_process', None):
            _kill(self.ytdlp_process.pid)
        # Fallback to whatever process_pid pointed at
        if self.process_pid:
            _kill(self.process_pid)

        self.ffplay_processes = []
        self.process = None
        self.process_pid = None
        self.play_flag = False  # Stop the play flag

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video Tiler")
        self.is_ffmpeg_visible = False
        #self.geometry("600x300")  # Set window size to 600x300
        
        # Set the app icon
        try:
            bin_path = os.path.dirname(os.path.abspath(__file__))
            self.icon_path = os.path.join(bin_path, 'img', 'app.ico')
            self.iconbitmap(self.icon_path)
        except Exception as e:
            print(f"Failed to set app icon: {e}")
        
        self.yt_video = None
        self.video_thread = None
        self.play_flag = False  # Flag to track play state
        self.theme_var = tk.StringVar(value='Light')

        self.create_menu()
        self.create_widgets()
        self.load_saved_divisions()
        self.load_all_settings()

        # Initialize with default video
        self.initialize_default_video()

        # Kiosk: auto-start playback if the user enabled it
        if self.autoplay.get():
            self.after(2500, self.play_video)

        # Quietly look for a newer version on launch (suggests only; never forces)
        self.after(4000, lambda: self.check_for_updates(silent=True))

    def initialize_default_video(self):
        if not self.url_entry.get():
            self.url_entry.set(DEFAULT_URL)
        self.after(1, self.update_video_title)
            #self.play_video()  # Automatically start playing the default video

    def create_widgets(self):
        # Video title label
        self.video_title_label = tk.Label(self, text="Video Title", font=("Helvetica", 12))
        self.video_title_label.grid(row=1, column=1, columnspan=4, padx=10, pady=10, sticky='w')

        # URL Entry
        self.url_label = tk.Label(self, text="Video URL:", font=("Helvetica", 12))
        self.url_label.grid(row=2, column=1, padx=10, pady=10, sticky='w')

        array_url = ["https://www.youtube.com/watch?v=ZzWBpGwKoaI", "https://x.com/i/broadcasts/1LyxBgjebwOKN"]

        #self.url_entry = tk.Entry(self, width=50)
        
        self.url_entry = ttk.Combobox(self, values=array_url, width=50)
        self.url_entry.set('')  # Optional: Set default text
        self.url_entry.grid(row=2, column=2, columnspan=3, padx=10, pady=10, sticky='w')


        # Spinbox for divisions
        self.divisions_label = tk.Label(self, text="Grid divisions:", font=("Helvetica", 12))
        self.divisions_label.grid(row=3, column=1, padx=10, pady=10, sticky='w')

        self.divisions_spinbox = tk.Spinbox(self, from_=1, to=50, increment=1, width=5)
        self.divisions_spinbox.grid(row=3, column=2, padx=10, pady=10, sticky='w')

        # Buttons
        self.stop_button = tk.Button(self, text="■  Stop", command=self.stop_video, width=9,
                                     font=("Helvetica", 10), relief='flat', bd=0, cursor='hand2',
                                     highlightthickness=0)
        self.stop_button.grid(row=3, column=3, padx=8, pady=8)

        self.play_button = tk.Button(self, text="▶  Play", command=self.play_video, width=9,
                                     font=("Helvetica", 10), relief='flat', bd=0, cursor='hand2',
                                     highlightthickness=0)
        self.play_button.grid(row=3, column=4, padx=8, pady=8)

        # ---- Options row ----
        self.auto_restart_video = tk.BooleanVar(value=True)
        self.auto_restart_checkbutton = tk.Checkbutton(self, text="Auto Restart", variable=self.auto_restart_video, command=self.save_all_settings)
        self.auto_restart_checkbutton.grid(row=4, column=1, padx=10, pady=10, sticky='w')

        # Tile across all / selected monitors
        self.multi_monitor = tk.BooleanVar(value=False)
        self.selected_monitor_indices = [m['index'] for m in monitor_utils.list_monitors()]
        self.multi_monitor_checkbutton = tk.Checkbutton(self, text="Multi-monitor", variable=self.multi_monitor, command=self.save_all_settings)
        self.multi_monitor_checkbutton.grid(row=4, column=2, padx=10, pady=10, sticky='w')

        # Mute audio
        self.mute = tk.BooleanVar(value=False)
        self.mute_checkbutton = tk.Checkbutton(self, text="Mute", variable=self.mute, command=self.save_all_settings)
        self.mute_checkbutton.grid(row=4, column=3, padx=10, pady=10, sticky='w')

        # Pick which monitors to use
        self.choose_monitors_button = tk.Button(self, text="Monitors…", command=self.choose_monitors)
        self.choose_monitors_button.grid(row=4, column=4, padx=10, pady=10)

        # ---- Quality + kiosk options row ----
        self.quality_label = tk.Label(self, text="Quality:", font=("Helvetica", 11))
        self.quality_label.grid(row=5, column=1, padx=10, pady=6, sticky='e')
        self.quality = ttk.Combobox(self, values=QUALITY_CHOICES, width=8, state='readonly')
        self.quality.set('Auto')
        self.quality.grid(row=5, column=2, padx=10, pady=6, sticky='w')
        self.quality.bind("<<ComboboxSelected>>", lambda e: self.save_all_settings())

        self.autoplay = tk.BooleanVar(value=False)
        self.autoplay_checkbutton = tk.Checkbutton(self, text="Auto-play on launch",
                                                   variable=self.autoplay, command=self.save_all_settings)
        self.autoplay_checkbutton.grid(row=5, column=3, padx=10, pady=6, sticky='w')

        self.run_at_startup = tk.BooleanVar(value=False)
        self.run_at_startup_checkbutton = tk.Checkbutton(self, text="Run at Windows startup",
                                                         variable=self.run_at_startup, command=self.on_toggle_startup)
        self.run_at_startup_checkbutton.grid(row=5, column=4, padx=10, pady=6, sticky='w')

        # Detected-monitor info line
        self.monitors_info_label = tk.Label(self, text="", font=("Helvetica", 9), fg="gray30", anchor='w')
        self.monitors_info_label.grid(row=6, column=1, columnspan=5, padx=10, pady=(0, 2), sticky='w')
        self.refresh_monitor_info()

        
        # Status bar
        self.status_bar = tk.Label(self, text="Status: Ready", bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.grid(row=7, column=1, columnspan=5, padx=10, pady=10, sticky='ew')

        # Bind URL entry change to update video title; Enter starts playback
        self.url_entry.bind("<FocusOut>", self.update_video_title)
        self.url_entry.bind("<Return>", lambda e: self.play_video())

        # Keyboard shortcuts: Esc = stop, F5 = play, Space = play/pause (ignored while typing a URL)
        self.bind_all("<Escape>", lambda e: self.stop_video())
        self.bind_all("<F5>", lambda e: self.play_video())
        self.bind_all("<space>", self._space_shortcut)

        # Configure grid resizing
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0)
        self.grid_rowconfigure(2, weight=0)
        self.grid_rowconfigure(3, weight=0)
        self.grid_rowconfigure(4, weight=0)
        self.grid_rowconfigure(5, weight=0)
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=1)
        self.grid_columnconfigure(3, weight=1)
        self.grid_columnconfigure(4, weight=1)

        self.apply_theme()

    def _space_shortcut(self, event=None):
        # Don't hijack the spacebar while the user is in any text input field
        try:
            w = self.focus_get()
            if w is not None and w.winfo_class() in ('Entry', 'TEntry', 'TCombobox', 'Spinbox'):
                return
        except Exception:
            pass
        if self.play_flag:
            self.stop_video()
        else:
            self.play_video()

    # Two restrained palettes; one subtle accent for the primary action, neutral for the rest.
    THEMES = {
        'Light': {
            'bg': '#f4f5f7', 'fg': '#1f2937', 'field': '#ffffff', 'sub': '#e4e7eb',
            'sub_fg': '#374151', 'info': '#6b7280', 'status': '#e9ebef', 'border': '#d1d5db',
        },
        'Dark': {
            'bg': '#1f2228', 'fg': '#e6e6e6', 'field': '#2b2f36', 'sub': '#353a42',
            'sub_fg': '#e6e6e6', 'info': '#9aa0a6', 'status': '#2b2f36', 'border': '#3a3f47',
        },
    }
    ACCENT = '#3b6ea5'        # restrained slate-blue for the primary (Play) action
    ACCENT_HOVER = '#4a7fb8'

    def apply_theme(self, theme=None):
        if theme is None:
            theme = self.theme_var.get()
        p = self.THEMES.get(theme, self.THEMES['Light'])
        BG, FG, FIELD = p['bg'], p['fg'], p['field']
        SUB, SUB_FG = p['sub'], p['sub_fg']

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
            # Primary action: a single subtle accent. Secondary: neutral. No bright green/red.
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
            style.map("TCombobox", fieldbackground=[('readonly', FIELD)],
                      foreground=[('readonly', FG)])
        except Exception:
            pass

    def refresh_monitor_info(self):
        try:
            mons = monitor_utils.list_monitors()
            sel = [m for m in mons if m['index'] in self.selected_monitor_indices]
            sel_txt = ", ".join("#{}".format(m['index'] + 1) for m in sel) or "none"
            self.monitors_info_label.config(
                text="Detected {n} monitor(s).  Selected for multi-monitor: {s}".format(
                    n=len(mons), s=sel_txt))
        except Exception:
            pass

    def identify_monitors(self):
        """Flash a big number on each monitor so the user can tell which is which."""
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

    def save_all_settings(self):
        """Persist every GUI choice so the next launch starts the same way."""
        try:
            url = self.url_entry.get().strip()
            # Remember any new URL the user typed, so it stays in the dropdown
            urls = list(self.url_entry['values'])
            if url and url not in urls:
                urls.append(url)
                self.url_entry['values'] = urls
            write_settings({
                'url': url,
                'urls': urls,
                'divisions': int(self.divisions_spinbox.get()),
                'auto_restart': bool(self.auto_restart_video.get()),
                'multi_monitor': bool(self.multi_monitor.get()),
                'selected_monitor_indices': list(self.selected_monitor_indices),
                'mute': bool(self.mute.get()),
                'quality': self.quality.get(),
                'autoplay': bool(self.autoplay.get()),
                'run_at_startup': bool(self.run_at_startup.get()),
                'theme': self.theme_var.get(),
            })
        except Exception:
            pass
        self.refresh_monitor_info()

    def on_toggle_startup(self):
        ok = set_run_at_startup(bool(self.run_at_startup.get()))
        if not ok:
            messagebox.showwarning(
                "Run at startup",
                "Could not change the Windows startup setting.")
            self.run_at_startup.set(False)
        self.save_all_settings()

    def load_all_settings(self):
        """Apply persisted choices to the widgets on startup."""
        data = read_settings()
        try:
            avail = [m['index'] for m in monitor_utils.list_monitors()]
            sel = [i for i in data.get('selected_monitor_indices', []) if i in avail]
            if sel:
                self.selected_monitor_indices = sel
            d = int(data.get('divisions', 3))
            if 1 <= d <= 50:
                self.divisions_spinbox.delete(0, tk.END)
                self.divisions_spinbox.insert(0, d)
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
        except Exception:
            pass
        self.refresh_monitor_info()

    def create_menu(self):
        menubar = tk.Menu(self)

        # Define a custom font for the menu items
        menu_font = tkfont.Font(family="Helvetica", size=12)
        menu_font_small = tkfont.Font(family="Helvetica", size=8)

        about_menu = tk.Menu(menubar, tearoff=0)

        # Add commands with zero padding
        about_menu.add_command(label="Supported video sites", command=self.open_supported_video_site_list, font=menu_font)
        about_menu.add_command(label="Why Tiling", command=self.open_why_tiling, font=menu_font)
        about_menu.add_command(label="Source code", command=self.open_source_code_web_site, font=menu_font)
        about_menu.add_command(label="Help", command=self.show_help, font=menu_font)

        # View menu: let the user choose the theme (not imposed)
        view_menu = tk.Menu(menubar, tearoff=0)
        theme_menu = tk.Menu(view_menu, tearoff=0)
        theme_menu.add_radiobutton(label="Light", variable=self.theme_var, value="Light",
                                   command=self.on_theme_change, font=menu_font)
        theme_menu.add_radiobutton(label="Dark", variable=self.theme_var, value="Dark",
                                   command=self.on_theme_change, font=menu_font)
        view_menu.add_cascade(label="Theme", menu=theme_menu, font=menu_font)
        menubar.add_cascade(label="View", menu=view_menu, font=menu_font_small)

        # Tools menu: keep yt-dlp current and check for app updates
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Update yt-dlp now", command=self.update_yt_dlp, font=menu_font)
        tools_menu.add_command(label="Check for updates", command=lambda: self.check_for_updates(False), font=menu_font)
        menubar.add_cascade(label="Tools", menu=tools_menu, font=menu_font_small)

        menubar.add_cascade(label="About", menu=about_menu, font=menu_font_small)

        # Configure the menu bar with zero padding (if applicable)
        self.config(menu=menubar)

    def on_theme_change(self):
        self.apply_theme()
        self.save_all_settings()
        
    def show_help(self):
        help_text = (
            "HOW TO USE\n"
            "  1. Pick or paste a video URL (YouTube, X, etc.).\n"
            "  2. Set the grid size (e.g. 5 = a 5x5 grid of identical tiles).\n"
            "  3. Press Play (or Enter). Every tile shows the same live frame;\n"
            "     only ONE stream is downloaded.\n\n"
            "STOPPING THE VIDEO\n"
            "  The player opens full screen. Press Esc or 'q' on the video window,\n"
            "  or Alt+Tab back to this window and press Stop.\n\n"
            "MULTI-MONITOR\n"
            "  Tick 'Multi-monitor' and use 'Monitors...' to choose which screens\n"
            "  (e.g. 2 of 3). One download is fanned out to one window per screen.\n\n"
            "KEYBOARD\n"
            "  Esc = Stop    F5 = Play    Space = Play/Pause\n\n"
            "OPTIONS\n"
            "  Quality forces a resolution; Auto picks per tile size.\n"
            "  Auto-play / Run at Windows startup enable kiosk mode.\n"
            "  Theme (View menu) and every choice are remembered between launches.\n\n"
            "UPDATES\n"
            "  Tools > Update yt-dlp fixes most YouTube playback breakage.\n"
            "  The app also updates yt-dlp automatically after repeated failures.\n\n"
            "PLATFORM\n"
            "  Tested on Windows. macOS/Linux are supported on a best-effort basis\n"
            "  (window-focus detection is Windows-only and degrades gracefully).\n\n"
            f"Version: {PROGRAM_VERSION}\nEmail: {AUTHOR_EMAIL}\nWebsite: {AUTHOR_WEBSITE}"
        )
        messagebox.showinfo("Help", help_text)

    def open_why_tiling(self):
        webbrowser.open(WHY_TILING_URL)
        
    def open_supported_video_site_list(self):
        webbrowser.open(SUPPORTED_WEB_SITES)
        
    def open_source_code_web_site(self):
        webbrowser.open(SOURCE_CODE_GITHUB)


    def update_video_title(self, event=None):
        url = self.url_entry.get()
        if url != (self.yt_video.url if self.yt_video else ""):
            if self.yt_video:
                self.stop_video()  # Stop any currently playing video
            self.yt_video = YouTubeVideo(self, url, int(self.divisions_spinbox.get()))
            self.after(100, self._update_title_label)

    def _update_title_label(self):
        if self.yt_video:
            self.video_title_label.config(text=f"{self.yt_video.title}")

    def play_video(self):
        self.stop_video()  # Stop any currently playing video
        self.is_ffmpeg_visible = False
        self.play_button.config(state=tk.DISABLED)
        
        # Start video playback in a separate thread
        url = self.url_entry.get()
        divisions = int(self.divisions_spinbox.get())
        self.save_all_settings()  # remember the latest choices

        self.yt_video = YouTubeVideo(self, url, divisions)
        if self.yt_video.ytdlp_is_valid == False:
            print("Video URL is not valid")
            messagebox.showerror("URL Error", f"URL '{url}' does not seem to be a valid video.")
            self.play_button.config(state=tk.NORMAL)
            return
            
        
        # Show temporary starting message
        try:
            self.update_status(f"Starting video player '{self.yt_video.title}'", color='blue')
            self._update_title_label()
        except:
            self.update_status(f"Ready")
            
        self.play_flag = True  # Set play flag
        self.video_thread = threading.Thread(target=self.yt_video.play_video)
        self.video_thread.start()

        # Update status after 17 seconds
        self.after(35000, lambda: self.update_status(f"Playing video '{self.yt_video.title}'"))
        self._update_title_label()

    def stop_video(self):
        if self.yt_video:
            self.yt_video.stop_video()
            self.update_status("Ready")
            self.play_flag = False  # Unset play flag
        else:
            print("No video instance to stop.")

    def update_status(self, message, color='black'):
        self.status_bar.config(text=f"Status: {message}", fg=color)

    def update_yt_dlp(self, silent=False):
        """Update the bundled yt-dlp executable. Runs in the background.

        yt-dlp is the part that breaks when YouTube changes, so this is the main
        self-repair. Also called automatically after repeated playback failures.
        """
        def worker():
            path = find_executable('yt-dlp')
            if not path:
                if not silent:
                    self.after(0, lambda: messagebox.showwarning(
                        "Update yt-dlp", "yt-dlp executable was not found on PATH."))
                return
            try:
                self.after(0, lambda: self.update_status("Updating yt-dlp...", color='blue'))
                res = subprocess.run([path, '-U'], capture_output=True, text=True, timeout=180)
                out = ((res.stdout or '') + (res.stderr or '')).strip()
                self.after(0, lambda: self.update_status("yt-dlp update finished."))
                if not silent:
                    self.after(0, lambda: messagebox.showinfo(
                        "Update yt-dlp", out[-800:] if out else "Done."))
            except Exception as e:
                self.after(0, lambda: self.update_status("yt-dlp update failed."))
                if not silent:
                    self.after(0, lambda: messagebox.showwarning(
                        "Update yt-dlp", "Update failed:\n{}".format(e)))
        threading.Thread(target=worker, daemon=True).start()

    def check_for_updates(self, silent=False):
        """Check GitHub for a newer app version, then report and suggest (never forces)."""
        def worker():
            try:
                req = urllib.request.Request(UPDATE_VERSION_URL, headers={'User-Agent': 'video-tiler'})
                with urllib.request.urlopen(req, timeout=8) as r:
                    remote = r.read().decode('utf-8', 'ignore').strip()
            except Exception:
                if not silent:
                    self.after(0, lambda: messagebox.showinfo(
                        "Updates", "Could not reach the update server."))
                return
            if remote and remote != PROGRAM_VERSION:
                def offer():
                    if messagebox.askyesno(
                            "Update available",
                            "A newer version ({new}) is available (you have {cur}).\n\n"
                            "Open the download page?".format(new=remote, cur=PROGRAM_VERSION)):
                        webbrowser.open(RELEASES_URL)
                self.after(0, offer)
            elif not silent:
                self.after(0, lambda: messagebox.showinfo(
                    "Updates", "You are on the latest version ({}).".format(PROGRAM_VERSION)))
        threading.Thread(target=worker, daemon=True).start()

    def load_saved_divisions(self):
        try:
            with open(DIVISIONS_FILE, "r") as file:
                saved_divisions = int(file.read().strip())
                if 1 <= saved_divisions <= 50:
                    self.divisions_spinbox.delete(0, tk.END)
                    self.divisions_spinbox.insert(0, saved_divisions)
                else:
                    self.divisions_spinbox.delete(0, tk.END)
                    self.divisions_spinbox.insert(0, 3)
        except FileNotFoundError:
            self.divisions_spinbox.delete(0, tk.END)
            self.divisions_spinbox.insert(0, 3)

    def on_closing(self):
        self.save_all_settings()  # remember choices for next launch
        self.stop_video()  # Ensure the video is stopped before closing
        self.destroy()

if __name__ == "__main__":
    add_to_path()
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
