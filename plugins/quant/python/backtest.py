#!/usr/bin/env python3
"""Legacy CLI entry point; the canonical implementation is trading_datasource.backtest."""
from pathlib import Path
import sys

if __name__ == "__main__":
    repo = Path(__file__).resolve().parents[2]          # plugins/
    sys.path.insert(0, str(repo / "datasource" / "python"))
    from trading_datasource.backtest import main
    sys.exit(main())
