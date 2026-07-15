"""Tests for the Windows kiosk helpers (winkiosk): DPI-aware child env,
keep-awake, single-instance mutex, and the window-rect enforcer's bounded
behaviour. Runs the REAL Windows APIs on Windows (they are all safe/reversible
here); SKIPS cleanly (exit 0) on other platforms.

    .venv\\Scripts\\python.exe tests\\test_winkiosk.py
"""

import os
import sys
import time

SRC = os.path.join(os.path.dirname(__file__), '..', 'src')
sys.path.insert(0, SRC)

import winkiosk  # noqa: E402

_failures = []


def check(name, cond):
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _failures.append(name)


def test_sdl_child_env_requests_pmv2():
    env = winkiosk.sdl_child_env()
    check("child env exists on Windows", isinstance(env, dict))
    check("SDL DPI awareness hint is permonitorv2",
          env.get('SDL_WINDOWS_DPI_AWARENESS') == 'permonitorv2')
    check("the rest of the environment is inherited", 'PATH' in env or 'Path' in env)
    # An operator-set override must win (setdefault semantics).
    os.environ['SDL_WINDOWS_DPI_AWARENESS'] = 'system'
    try:
        check("an explicit operator override is respected",
              winkiosk.sdl_child_env().get('SDL_WINDOWS_DPI_AWARENESS') == 'system')
    finally:
        del os.environ['SDL_WINDOWS_DPI_AWARENESS']


def test_keep_awake_roundtrip():
    # Real SetThreadExecutionState calls; both must succeed and the final state
    # hands power management back to Windows.
    check("keep_awake(True) accepted", winkiosk.keep_awake(True) is True)
    check("keep_awake(False) accepted (state handed back)",
          winkiosk.keep_awake(False) is True)


def test_single_instance_mutex():
    name = "SMTV_VideoTiler_test_mutex_%d" % os.getpid()
    check("first acquire wins", winkiosk.acquire_single_instance(name) is True)
    check("second acquire in the SAME process is refused (mutex already exists)",
          winkiosk.acquire_single_instance(name) is False)


def test_enforce_window_rect_is_bounded_and_safe():
    # A pid that owns no windows: must return False quickly (no hang, no raise).
    t0 = time.time()
    ok = winkiosk.enforce_window_rect(0x7FFFFFF0, 0, 0, 100, 100,
                                      attempts=2, interval=0.05)
    took = time.time() - t0
    check("nonexistent-window enforcement returns False", ok is False)
    check("and is bounded (took %.2fs)" % took, took < 3.0)
    # still_wanted=False must abort immediately.
    t0 = time.time()
    ok = winkiosk.enforce_window_rect(0x7FFFFFF0, 0, 0, 100, 100,
                                      attempts=50, interval=1.0,
                                      still_wanted=lambda: False)
    check("still_wanted=False aborts at once", ok is False and time.time() - t0 < 1.0)


def test_dpi_awareness_reports_a_level():
    # Safe to call here: this test file runs in its own interpreter.
    level = winkiosk.set_dpi_awareness()
    check("set_dpi_awareness reports a level (%s)" % level,
          level in ("permonitorv2", "permonitor", "system", "unaware"))
    check("second call is harmless", winkiosk.set_dpi_awareness() in
          ("permonitorv2", "permonitor", "system", "unaware"))


if __name__ == '__main__':
    if not winkiosk.IS_WINDOWS:
        print("SKIP: winkiosk is Windows-only; nothing to test on this platform")
        sys.exit(0)
    for fn in [test_sdl_child_env_requests_pmv2, test_keep_awake_roundtrip,
               test_single_instance_mutex,
               test_enforce_window_rect_is_bounded_and_safe,
               test_dpi_awareness_reports_a_level]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
