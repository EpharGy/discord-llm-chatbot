import os
import sys
from pathlib import Path

# Ensure the project 'src' directory is importable when tests run from CI
# This mirrors the Windows run instruction `$env:PYTHONPATH = "src"`.
root = Path(__file__).resolve().parents[1]
src = root / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))
# Also set PYTHONPATH for any subprocesses that might be spawned during tests
os.environ.setdefault("PYTHONPATH", str(src))
