"""Integration check: the live-tuning flags the engine now passes to ffplay are
actually accepted by the installed ffplay, and the identical-tiles filtergraph
from monitor_utils.tile_filter_for is a valid graph the installed ffmpeg can run.

This shells out to the REAL ffmpeg/ffplay. If they are not installed it SKIPS
cleanly (exit 0) so the deterministic suite stays green without them. ffplay runs
with -nodisp so no window pops up.

    .venv\\Scripts\\python.exe tests\\test_ffplay_flags.py
"""

import os
import sys
import shutil
import tempfile
import subprocess
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


FFMPEG = shutil.which("ffmpeg")
FFPLAY = shutil.which("ffplay")
CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
_ERR_MARKERS = ("Unrecognized option", "Option not found", "not found",
                "Failed to set value", "Error parsing", "Error splitting",
                "Invalid argument")


def _make_clip(path):
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "testsrc2=size=1280x720:rate=30", "-t", "2", "-c:v", "libx264",
         "-preset", "veryfast", "-pix_fmt", "yuv420p", "-f", "mpegts", path],
        check=True, creationflags=CREATE_NO_WINDOW)


def _ffplay_outcome(argv, clip, settle=4.0):
    """Run ffplay on `clip` and report (accepted, stderr). 'accepted' is True if
    ffplay got PAST option parsing - i.e. it either exited 0 or was still playing
    at `settle` (we then kill it). A flag ffplay rejects makes it exit non-zero
    almost immediately with an option-parse error, which is what we test for.
    (We avoid asserting a clean autoexit because -nodisp playback may not reach
    EOF the same way; flag ACCEPTANCE is the property under test.)"""
    with open(clip, "rb") as f:
        p = subprocess.Popen(argv, stdin=f, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
        try:
            _, err = p.communicate(timeout=settle)
            err = (err or b"").decode("utf-8", "ignore")
            accepted = (p.returncode == 0) and not any(m in err for m in _ERR_MARKERS)
            return accepted, err
        except subprocess.TimeoutExpired:
            p.kill()                      # still playing at `settle` -> flags were accepted
            try:
                _, err = p.communicate(timeout=5)
            except Exception:
                err = b""
            err = (err or b"").decode("utf-8", "ignore")
            return (not any(m in err for m in _ERR_MARKERS)), err


def test_engine_ffplay_flags_are_accepted():
    # Pull the engine's REAL ffplay argv, then run it headless (-nodisp instead of
    # the window/-fs placement) against a piped clip and assert a clean autoexit.
    class App:
        pass
    p = vt.Player(App(), "http://x", 3)
    fake_mon = {'index': 0, 'x': 0, 'y': 0, 'width': 1280, 'height': 720,
                'is_primary': True, 'name': 'T'}
    argv = p._ffplay_cmd(fake_mon, True, True)        # real flags incl. -threads 0, -framedrop
    check("engine argv carries -threads 0 before the input",
          '-threads' in argv and argv[argv.index('-threads') + 1] == '0'
          and argv.index('-threads') < argv.index('-'))
    check("engine argv carries -framedrop", '-framedrop' in argv)

    # Headless variant: replace the placement tail (-fs / window opts) with -nodisp.
    vf = argv[argv.index('-vf') + 1]
    run_argv = [FFPLAY, '-threads', '0', '-', '-vf', vf, '-framedrop',
                '-autoexit', '-loglevel', 'error', '-hide_banner', '-an', '-nodisp']

    work = tempfile.mkdtemp(prefix="vt_flags_")
    clip = os.path.join(work, "clip.ts")
    try:
        _make_clip(clip)
        accepted, err = _ffplay_outcome(run_argv, clip)
        check("the installed ffplay ACCEPTS the engine flag set (-threads/-framedrop)",
              accepted)
        check("ffplay reported no option/parse error", not any(m in err for m in _ERR_MARKERS))

        # Control: a bogus flag MUST be rejected (proves the check has teeth).
        bogus = [FFPLAY, '-threads', '0', '-zzbogus', 'x', '-', '-vf', vf,
                 '-framedrop', '-autoexit', '-loglevel', 'error', '-hide_banner',
                 '-an', '-nodisp']
        bad_accepted, bad_err = _ffplay_outcome(bogus, clip, settle=10.0)
        check("a bogus ffplay flag is rejected (control has teeth)",
              (not bad_accepted) and any(m in bad_err for m in _ERR_MARKERS))
    finally:
        try:
            os.remove(clip)
            os.rmdir(work)
        except Exception:
            pass


def test_tile_filtergraph_compiles_and_runs():
    # tile_filter_for must emit a filtergraph ffmpeg can actually build + run.
    work = tempfile.mkdtemp(prefix="vt_vf_")
    clip = os.path.join(work, "clip.ts")
    try:
        _make_clip(clip)
        for n in (2, 5):
            vf, ow, oh = vt.monitor_utils.tile_filter_for(1280, 720, n)
            r = subprocess.run(
                [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", clip,
                 "-vf", vf, "-frames:v", "30", "-f", "null", "-"],
                capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
            err = r.stderr.decode("utf-8", "ignore")
            check("tile filter for N=%d is a valid, runnable graph (rc=%d)" % (n, r.returncode),
                  r.returncode == 0 and not err.strip())
    finally:
        try:
            os.remove(clip)
            os.rmdir(work)
        except Exception:
            pass


if __name__ == '__main__':
    if not FFMPEG or not FFPLAY:
        print("SKIP: ffmpeg/ffplay not on PATH; skipping the real-ffplay flag check.")
        sys.exit(0)
    for fn in [test_engine_ffplay_flags_are_accepted,
               test_tile_filtergraph_compiles_and_runs]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
