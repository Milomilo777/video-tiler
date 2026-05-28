"""Run every deterministic test file and aggregate the result.

    .venv\\Scripts\\python.exe tests\\run_tests.py

Exits non-zero if any test file fails. No GUI / network needed; monitor and
subprocess dependencies are faked inside the individual tests.
"""

import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
TEST_FILES = [
    "test_monitor_utils.py",
    "test_format_select.py",
    "test_engine.py",
    "test_fanout.py",
    "test_settings.py",
]


def main():
    py = sys.executable
    failed = []
    for name in TEST_FILES:
        path = os.path.join(HERE, name)
        print("\n=== %s ===" % name)
        res = subprocess.run([py, path])
        if res.returncode != 0:
            failed.append(name)
    print("\n" + "=" * 50)
    if failed:
        print("SUITE FAILED: %d/%d file(s) -> %s" % (len(failed), len(TEST_FILES), failed))
        return 1
    print("SUITE PASSED: all %d test files green" % len(TEST_FILES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
