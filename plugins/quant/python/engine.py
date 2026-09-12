#!/usr/bin/env python3
"""Legacy CLI entry point; implementation lives in plugins/engine/python."""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    canonical = Path(__file__).resolve().parents[2] / "engine" / "python"
    sys.path.insert(0, str(canonical))
    runpy.run_path(str(canonical / "engine.py"), run_name="__main__")
