"""Windows kiosk-hardening helpers (ctypes only, GUI-free, unit-testable).

Everything here is a safe no-op on non-Windows platforms, so the caller never
needs an `if os.name == 'nt'` guard. These helpers close the field-reported
failure modes of an unattended video wall:

  * set_dpi_awareness()      - Per-Monitor-v2 DPI awareness, so monitor
    coordinates are TRUE physical pixels. Without it, Windows DPI
    virtualization feeds scaled ("logical") coordinates to a DPI-unaware
    process, and a window placed with them on a 125%/150% monitor is the
    wrong size - the classic "the wall is not quite fullscreen" bug.
  * sdl_child_env()          - env for ffplay so SDL (>= 2.24) also opts into
    Per-Monitor-v2 and interprets our physical-pixel -left/-top/-x/-y exactly.
  * enforce_window_rect()    - belt-and-braces: verify a child's window really
    covers its monitor and SetWindowPos it into place if not (covers older SDL
    builds that ignore the env hint, work-area clamps, taskbar overlap).
  * keep_awake()             - SetThreadExecutionState so an unattended kiosk
    never idles into sleep / display-off mid-show ("after a few hours the
    system froze and never came back" = the machine went to sleep).
  * acquire_single_instance() - a named mutex so run-at-startup plus a manual
    double-click can never run two competing walls (two players fighting over
    fullscreen + double decode load reads as a freeze on a weak laptop).
"""

import os
import time
import logging

log = logging.getLogger("videotiler")

IS_WINDOWS = (os.name == 'nt')

# SetThreadExecutionState flags (winbase.h)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

ERROR_ALREADY_EXISTS = 183

# Keep a reference to the single-instance mutex handle for the process
# lifetime; Windows releases it automatically at exit.
_mutex_handle = None


def set_dpi_awareness():
    """Make THIS process DPI-aware (best available level) BEFORE any window or
    monitor enumeration exists, so every coordinate we read or pass around is a
    physical pixel. Returns the level achieved (for the log)."""
    if not IS_WINDOWS:
        return "n/a"
    import ctypes
    try:
        # Per-Monitor v2 (Windows 10 1703+): exact physical pixels everywhere,
        # correct behaviour when monitors have different scale factors.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(-4)):  # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            return "permonitorv2"
    except Exception:
        pass
    try:
        # Per-Monitor v1 (Windows 8.1+).
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "permonitor"
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return "system"
    except Exception:
        return "unaware"


def sdl_child_env():
    """Environment for ffplay children: ask SDL (>= 2.24) for Per-Monitor-v2
    DPI awareness so the physical-pixel geometry we pass is applied 1:1 (older
    SDL ignores the variable, which enforce_window_rect then covers). Returns
    None off Windows so Popen(env=None) inherits as usual."""
    if not IS_WINDOWS:
        return None
    env = os.environ.copy()
    env.setdefault('SDL_WINDOWS_DPI_AWARENESS', 'permonitorv2')
    return env


def keep_awake(on):
    """Tell Windows the system + display are required (on=True) or hand back
    normal power management (on=False). MUST be called from a long-lived thread
    (the Tk main thread): ES_CONTINUOUS is per-thread state. Cannot and should
    not override an explicit lid-close/power-button - that is the user's call."""
    if not IS_WINDOWS:
        return False
    import ctypes
    try:
        flags = ES_CONTINUOUS
        if on:
            flags |= ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception:
        return False


def acquire_single_instance(name="SMTV_VideoTiler_single_instance"):
    """True if this process now owns the app's named mutex; False if another
    instance already holds it. Errs on the side of running (returns True) if
    the mutex machinery itself fails."""
    global _mutex_handle
    if not IS_WINDOWS:
        return True
    import ctypes
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception:
        return True


def _pid_visible_windows(pid):
    """Top-level visible window handles owned by `pid` (Windows only)."""
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    hwnds = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return hwnds


def enforce_window_rect(pid, x, y, w, h, attempts=10, interval=1.0,
                        still_wanted=None, tolerance=2):
    """Make sure the (single) visible window of process `pid` exactly covers
    the rectangle x,y,w,h (physical pixels), correcting it with SetWindowPos if
    needed. Returns True once verified within `tolerance` px.

    This is the deterministic backstop for "sometimes a monitor is not quite
    fullscreen": whatever the child's DPI awareness or SDL version did to the
    requested geometry, our (DPI-aware) process re-asserts the exact physical
    rectangle and TOPMOST z-order. Safe to call for a window that never appears
    (bounded by `attempts`); `still_wanted` lets the caller abort early when
    the window was retired/killed."""
    if not IS_WINDOWS:
        return False
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    HWND_TOPMOST = -1
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040

    for attempt in range(max(1, int(attempts))):
        if still_wanted is not None and not still_wanted():
            return False
        try:
            ok = False
            for hwnd in _pid_visible_windows(pid):
                r = wt.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    continue
                if (abs(r.left - x) <= tolerance and abs(r.top - y) <= tolerance
                        and abs((r.right - r.left) - w) <= tolerance
                        and abs((r.bottom - r.top) - h) <= tolerance):
                    ok = True
                    continue
                log.info("window of pid %s is %dx%d at (%d,%d); forcing to "
                         "%dx%d at (%d,%d)", pid,
                         r.right - r.left, r.bottom - r.top, r.left, r.top,
                         w, h, x, y)
                user32.SetWindowPos(hwnd, HWND_TOPMOST, int(x), int(y),
                                    int(w), int(h),
                                    SWP_NOACTIVATE | SWP_SHOWWINDOW)
                ok = False  # verify on the next pass
            if ok:
                return True
        except Exception:
            pass
        if attempt + 1 < attempts:
            time.sleep(interval)
    return False
