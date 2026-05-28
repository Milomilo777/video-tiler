"""Tests for monitor_utils (the universal multi-monitor logic).

Run with the project's virtual environment:
    .venv\\Scripts\\python.exe tests\\test_monitor_utils.py
Exits non-zero if any check fails. No GUI / network needed.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))
import monitor_utils as mu  # noqa: E402


# Synthetic 3-monitor layout (left 1080p, center 1440p primary, right 1080p)
FAKE = [
    {'index': 0, 'x': 0, 'y': 0, 'width': 2560, 'height': 1440, 'name': 'C', 'is_primary': True},
    {'index': 1, 'x': 2560, 'y': 0, 'width': 1920, 'height': 1080, 'name': 'R', 'is_primary': False},
    {'index': 2, 'x': -1920, 'y': 0, 'width': 1920, 'height': 1080, 'name': 'L', 'is_primary': False},
]

_failures = []


def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def test_primary():
    check("primary_index finds the primary", mu.primary_index(FAKE) == 0)


def test_select_off_is_single():
    sel = mu.select_monitors(FAKE, [0, 1, 2], multi_monitor=False)
    check("multi OFF -> exactly one target", len(sel) == 1)
    check("multi OFF -> the primary", sel[0]['index'] == 0)


def test_select_subset():
    sel = mu.select_monitors(FAKE, [0, 1], multi_monitor=True)
    check("multi ON 2-of-3 -> two targets", len(sel) == 2)
    check("multi ON respects the ticked set", sorted(m['index'] for m in sel) == [0, 1])


def test_select_empty_defaults_all():
    sel = mu.select_monitors(FAKE, [], multi_monitor=True)
    check("multi ON with no selection -> all monitors", len(sel) == 3)


def test_tile_filter_even_and_multiple():
    for w, h in [(2560, 1440), (1920, 1080)]:
        for n in (2, 3, 5, 7, 12):
            vf, ow, oh = mu.tile_filter_for(w, h, n)
            check("N=%d %dx%d output is an NxN multiple" % (n, w, h),
                  ow % n == 0 and oh % n == 0)
            check("N=%d %dx%d tile size is even" % (n, w, h),
                  (ow // n) % 2 == 0 and (oh // n) % 2 == 0)
            check("N=%d %dx%d output fits the monitor" % (n, w, h),
                  ow <= w and oh <= h)
            check("N=%d %dx%d filter has fps*%d and tile" % (n, w, h, n * n),
                  ("fps=source_fps*%d" % (n * n)) in vf and ("tile=%dx%d" % (n, n)) in vf)


def test_window_opts():
    opts = mu.window_opts_for(FAKE[2], 1918, 1078)
    check("window opts are borderless", '-noborder' in opts)
    check("window opts carry the monitor x position",
          '-left' in opts and opts[opts.index('-left') + 1] == '-1920')


def test_real_machine_has_monitors():
    mons = mu.list_monitors()
    check("list_monitors() returns at least one monitor on this machine", len(mons) >= 1)


if __name__ == '__main__':
    for fn in [test_primary, test_select_off_is_single, test_select_subset,
               test_select_empty_defaults_all, test_tile_filter_even_and_multiple,
               test_window_opts, test_real_machine_has_monitors]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
