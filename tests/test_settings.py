"""Tests for settings persistence: round-trip, atomic write, and degrade-safe
reads (corrupt / non-dict / missing JSON all fall back to defaults).

Headless: points SETTINGS_FILE at a temp path. No GUI / network.

    .venv\\Scripts\\python.exe tests\\test_settings.py
"""

import os
import sys
import tempfile
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


def _with_tmp(fn):
    d = tempfile.mkdtemp(prefix="vt_settings_")
    vt.SETTINGS_FILE = os.path.join(d, "settings.json")
    try:
        fn()
    finally:
        try:
            for f in os.listdir(d):
                os.remove(os.path.join(d, f))
            os.rmdir(d)
        except Exception:
            pass


def test_roundtrip_merges_over_defaults():
    def body():
        vt.write_settings({'divisions': 7, 'theme': 'Dark', 'mute': True})
        got = vt.read_settings()
        check("written values read back", got['divisions'] == 7 and got['theme'] == 'Dark'
              and got['mute'] is True)
        check("unset keys keep their defaults",
              got['url'] == vt.DEFAULT_SETTINGS['url']
              and got['auto_restart'] == vt.DEFAULT_SETTINGS['auto_restart'])
    _with_tmp(body)


def test_missing_file_is_defaults():
    def body():
        # SETTINGS_FILE points at a path that does not exist yet.
        got = vt.read_settings()
        check("missing file -> exactly the defaults", got == dict(vt.DEFAULT_SETTINGS))
    _with_tmp(body)


def test_corrupt_json_degrades_to_defaults():
    def body():
        with open(vt.SETTINGS_FILE, 'w', encoding='utf-8') as f:
            f.write("{ this is not valid json ,,, ")
        got = vt.read_settings()
        check("corrupt JSON -> defaults (no crash)", got == dict(vt.DEFAULT_SETTINGS))
    _with_tmp(body)


def test_non_dict_json_ignored():
    def body():
        with open(vt.SETTINGS_FILE, 'w', encoding='utf-8') as f:
            f.write("[1, 2, 3]")
        got = vt.read_settings()
        check("top-level JSON list -> defaults", got == dict(vt.DEFAULT_SETTINGS))
    _with_tmp(body)


def test_atomic_write_leaves_no_tmp_and_is_valid():
    def body():
        vt.write_settings({'divisions': 9})
        tmp = vt.SETTINGS_FILE + '.tmp'
        check("no leftover .tmp file", not os.path.exists(tmp))
        check("settings file exists and parses", vt.read_settings()['divisions'] == 9)
    _with_tmp(body)


if __name__ == '__main__':
    for fn in [test_roundtrip_merges_over_defaults, test_missing_file_is_defaults,
               test_corrupt_json_degrades_to_defaults, test_non_dict_json_ignored,
               test_atomic_write_leaves_no_tmp_and_is_valid]:
        print(fn.__name__)
        fn()
    print()
    if _failures:
        print("FAILED: %d check(s) -> %s" % (len(_failures), _failures))
        sys.exit(1)
    print("ALL TESTS PASSED")
