"""Universal multi-monitor helpers for the video tiler.

Kept free of any GUI / tkinter import so the logic can be unit-tested on its own.
The playback design is one yt-dlp download fanned out to one ffplay per selected
monitor; each ffplay covers exactly one physical monitor (which is reliable),
instead of a single window trying to span several monitors (which is not).
"""

from screeninfo import get_monitors


def list_monitors():
    """Return all monitors as plain dicts, ordered left-to-right by x position.

    'index' is assigned AFTER the left-to-right sort, so it is the spatial
    position (0 = left-most), which is far more stable across reboots / driver
    updates / hotplug than the OS enumeration order would be - a saved monitor
    selection then keeps pointing at the same physical screen.

    get_monitors() can RAISE (e.g. ScreenInfoError on a headless / no-display /
    RDP session, or a transient failure during a monitor hotplug), not just
    return an empty list. We catch that so the single-display fallback below
    always fires and the app/playback worker never crashes on detection.
    """
    try:
        raw = list(get_monitors())
    except Exception:
        raw = []

    mons = []
    for m in raw:
        try:
            mons.append({
                'x': int(m.x),
                'y': int(m.y),
                'width': int(m.width),
                'height': int(m.height),
                'name': (getattr(m, 'name', None) or ''),
                'is_primary': bool(getattr(m, 'is_primary', False)),
            })
        except Exception:
            pass

    mons.sort(key=lambda mm: mm['x'])
    out = []
    for i, m in enumerate(mons):
        m['index'] = i
        if not m['name']:
            m['name'] = 'Display {}'.format(i + 1)
        out.append(m)

    if not out:
        # Fallback so the app still runs if detection fails for any reason.
        out.append({'index': 0, 'x': 0, 'y': 0, 'width': 1920, 'height': 1080,
                    'name': 'Display 1', 'is_primary': True})
    return out


def primary_index(monitors):
    """Index of the primary monitor (falls back to the left-most one)."""
    for m in monitors:
        if m['is_primary']:
            return m['index']
    return monitors[0]['index']


def select_monitors(monitors, selected_indices, multi_monitor):
    """Decide which monitors actually get a player window.

    - multi_monitor off  -> a single monitor (the primary, or the first ticked one).
    - multi_monitor on   -> every ticked monitor (or all monitors if none ticked).
    """
    by_index = {m['index']: m for m in monitors}
    if not multi_monitor:
        if selected_indices:
            for idx in selected_indices:
                if idx in by_index:
                    return [by_index[idx]]
        return [by_index[primary_index(monitors)]]
    chosen = [by_index[idx] for idx in selected_indices if idx in by_index]
    return chosen or list(monitors)


def tile_filter_for(width, height, divisions):
    """Build the identical-tiles ffplay -vf for one monitor of the given size.

    Uses the light fps*N^2 method: duplicate each frame N^2 times then tile NxN so
    every cell shows the same frame. Returns (filter_string, out_width, out_height).
    Tile size is floored to even numbers (codec/scaler friendly); the output is the
    largest exact NxN multiple that fits the monitor.
    """
    n = max(1, int(divisions))
    tw = max(2, int(width) // n)
    th = max(2, int(height) // n)
    tw -= tw % 2
    th -= th % 2
    vf = ("scale=w={tw}:h={th}:flags=neighbor,"
          "fps=source_fps*{m},tile={n}x{n}").format(tw=tw, th=th, m=n * n, n=n)
    return vf, tw * n, th * n


def window_opts_for(monitor, out_w, out_h, always_on_top=True):
    """ffplay options to place a borderless window exactly over one monitor."""
    opts = ['-noborder']
    if always_on_top:
        opts.append('-alwaysontop')
    opts += ['-left', str(monitor['x']), '-top', str(monitor['y']),
             '-x', str(int(out_w)), '-y', str(int(out_h))]
    return opts


def describe(monitor):
    """Human-readable one-line label for a monitor (used in the chooser dialog)."""
    tag = ' [primary]' if monitor['is_primary'] else ''
    return 'Monitor {n}: {w}x{h} @ ({x},{y}){tag}'.format(
        n=monitor['index'] + 1, w=monitor['width'], h=monitor['height'],
        x=monitor['x'], y=monitor['y'], tag=tag)
