"""
tests/test_smoke.py
--------------------
A pytest entry point over the project's own offline suite.

smoke_test.py (repo root) is deliberately a single self-contained runner with
inline HTML/JSON fixtures, not a pytest suite — see CONTRIBUTING.md for why.
This wraps it as one test rather than reimplementing its ~90 checks as pytest
asserts, so `pytest` and `python3 smoke_test.py` exercise the exact same code
path instead of two suites that can silently drift apart. Run
`python3 smoke_test.py` directly for per-check PASS/FAIL output; this test
only reports pass/fail as a whole, with that output attached on failure.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_smoke_suite():
    result = subprocess.run(
        [sys.executable, "smoke_test.py"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
