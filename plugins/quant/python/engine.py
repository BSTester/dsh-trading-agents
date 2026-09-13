#!/usr/bin/env python3
"""Legacy CLI entry point; the canonical implementation is plugins/engine/python/engine.py."""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    repo = Path(__file__).resolve().parents[2]          # plugins/
    canonical = repo / "engine" / "python"
    sys.path.insert(0, str(repo / "datasource" / "python"))   # 统一数据层
    sys.path.insert(0, str(canonical))                        # risk_config 等同级模块
    runpy.run_path(str(canonical / "engine.py"), run_name="__main__")
