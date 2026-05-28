"""Tests for the pure format-selection logic in src/video-tiler.py.

The module file name has a hyphen, so it is loaded via importlib rather than a
plain import. Importing it does not start the GUI (Tk() is only created in
main(), under __main__), so this stays headless. No network needed.

Run with the project's virtual environment:
    .venv\\Scripts\\python.exe tests\\test_format_select.py
Exits non-zero if any check fails.
"""

import os
import sys
import importlib.util

SRC = os.path.join(os.path.dirname(__file__), '..', 'src')
sys.path.insert(0, SRC)

spec = importlib.util.spec_from_file_location(
    "video_tiler", os.path.join(SRC, "video-tiler.py"))
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)

_failures = []


def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def test_manual_quality_wins():
    # A manual choice fixes the cap regardless of how dense the grid is.
    for n in (1, 5, 40):
        f = vt.select_format('720p', n)
        check("720p caps at 720 (n=%d)" % n, f.startswith("best[height<=720]"))
        check("720p still ends in /best (n=%d)" % n, f.endswith("/best"))


def test_auto_scales_with_density():
    # Auto should lower the resolution as the grid gets denser.
    low = vt.select_format('Auto', 3)     # sparse -> 360p band
    mid = vt.select_format('Auto', 20)    # denser -> 240p band
    high = vt.select_format('Auto', 50)   # very dense -> 144p band
    check("Auto sparse uses 360", "best[height<=360]" in low)
    check("Auto medium uses 240", "best[height<=240]" in mid)
    check("Auto dense uses 144", "best[height<=144]" in high)


def test_always_has_best_fallback():
    # Playback must never fail just because an exact resolution is missing.
    for q in vt.QUALITY_CHOICES:
        f = vt.select_format(q, 3)
        check("'%s' ends in /best" % q, f.endswith("/best"))
        check("'%s' has a graceful 2-step fallback" % q, f.count("/best") >= 2 or f == "best")


def test_unknown_quality_is_auto():
    # An unexpected string falls through to the Auto path, not a crash.
    f = vt.select_format('nonsense', 3)
    check("unknown quality behaves like Auto (360)", "best[height<=360]" in f)


if __name__ == '__main__':
    for fn in [test_manual_quality_wins, test_auto_scales_with_density,
               test_always_has_best_fallback, test_unknown_quality_is_auto]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
