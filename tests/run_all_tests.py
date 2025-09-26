"""Simple unified test runner.

Usage:
  python tests/run_all_tests.py

It discovers and executes all pytest tests programmatically.
This avoids remembering the exact pytest invocation and lets you add minimal
logic later (e.g., coverage hooks) in one place.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    try:
        import pytest  # type: ignore
    except ImportError:
        print("pytest not installed. Please install with `pip install pytest`.", file=sys.stderr)
        return 1

    # Ensure repository root on sys.path and import tests.conftest for PYTHONPATH side-effects
    repo_root = Path(__file__).resolve().parents[1]
    tests_dir = repo_root / 'tests'
    # Add repository root so that 'src' package is importable as a subdirectory
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    try:
        import conftest as _tests_conftest  # noqa: F401
    except Exception:
        pass

    # Run pytest on entire tests directory (this file will be ignored by default discovery due to name)
    # Add -q for concise output; rely on pytest discovery (conftest will set PYTHONPATH too)
    args = ['-q', str(tests_dir)]
    return pytest.main(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
