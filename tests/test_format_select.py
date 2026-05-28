"""Tests for the pure format-selection logic in src/video-tiler.py.

The module file name has a hyphen, so it is loaded via importlib rather than a
plain import. Importing it does not start the GUI (Tk() is only created in
main(), under __main__), so this stays headless. No network needed.

Run with the project's virtual environment:
    .venv\\Scripts\\python.exe tests\\test_format_select.py
Exits non-zero if any check fails.
"""

import os
import re
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


def _auto_height(n):
    """Parse the first height cap out of select_format('Auto', n)."""
    m = re.match(r"best\[height<=(\d+)\]", vt.select_format('Auto', n))
    return int(m.group(1)) if m else None


def test_manual_quality_wins():
    # A manual choice fixes the cap regardless of how dense the grid is.
    for n in (1, 5, 40):
        f = vt.select_format('720p', n)
        check("720p caps at 720 (n=%d)" % n, f.startswith("best[height<=720]"))
        check("720p still ends in /best (n=%d)" % n, f.endswith("/best"))


def test_auto_density_bands():
    # Sparse grids keep full resolution; only dense grids drop it.
    expected = {1: 1080, 2: 1080, 3: 720, 4: 720, 5: 360, 17: 360,
                18: 240, 35: 240, 36: 144, 64: 144}
    for n, h in expected.items():
        check("Auto n=%d -> %dp" % (n, h), _auto_height(n) == h)


def test_auto_boundaries_are_exact():
    # Guard the exact switch points against off-by-one edits.
    check("boundary 2->1080 / 3->720", _auto_height(2) == 1080 and _auto_height(3) == 720)
    check("boundary 4->720 / 5->360", _auto_height(4) == 720 and _auto_height(5) == 360)
    check("boundary 17->360 / 18->240", _auto_height(17) == 360 and _auto_height(18) == 240)
    check("boundary 35->240 / 36->144", _auto_height(35) == 240 and _auto_height(36) == 144)


def test_single_stream_not_capped_at_360():
    # A single full-screen tile must not be softened to 360p.
    check("Auto n=1 is full resolution (1080)", _auto_height(1) == 1080)


def test_fallback_shape():
    # Must be a real two-tier height fallback ending in a bare /best.
    pat = re.compile(r"^best\[height<=(\d+)\]/best\[height<=(\d+)\]/best$")
    for q in vt.QUALITY_CHOICES:
        f = vt.select_format(q, 3)
        m = pat.match(f)
        check("'%s' has shape H/H2/best" % q, m is not None)
        if m:
            h, h2 = int(m.group(1)), int(m.group(2))
            check("'%s' second tier is H+360" % q, h2 == h + 360)


def test_unknown_quality_is_auto():
    # An unexpected string falls through to the Auto path, not a crash.
    check("unknown quality behaves like Auto (n=3 -> 720)", _auto_height(3) == 720
          and vt.select_format('nonsense', 3) == vt.select_format('Auto', 3))


def test_next_backoff_sequence():
    # 3 -> 6 -> 12 -> 24 -> 30 -> 30 (doubling, capped at 30).
    seq, b = [], 3
    for _ in range(6):
        seq.append(b)
        b = vt.next_backoff(b)
    check("backoff doubles and caps at 30", seq == [3, 6, 12, 24, 30, 30])


if __name__ == '__main__':
    for fn in [test_manual_quality_wins, test_auto_density_bands,
               test_auto_boundaries_are_exact, test_single_stream_not_capped_at_360,
               test_fallback_shape, test_unknown_quality_is_auto,
               test_next_backoff_sequence]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
