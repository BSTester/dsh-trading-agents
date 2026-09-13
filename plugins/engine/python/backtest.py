#!/usr/bin/env python3
"""兼容入口：回测实现已统一到 trading_datasource.backtest（唯一实现）。

保留本文件是因为：
  1. engine 的工具层按脚本路径调用 `backtest.py`；
  2. 既有代码可能 `import backtest` 直接取用策略/成本常量，故在此完整再导出。
"""
import sys

from trading_datasource.backtest import (  # noqa: F401 - 兼容再导出
    COMMISSION, EXECUTION_SOURCE, SLIPPAGE, STAMP_TAX, load_data, ma, ma_cross_signal,
    main, metrics, rsi, rsi_signal, run, validate_data)

__all__ = ["COMMISSION", "EXECUTION_SOURCE", "SLIPPAGE", "STAMP_TAX", "load_data", "ma",
           "ma_cross_signal", "main", "metrics", "rsi", "rsi_signal", "run", "validate_data"]

if __name__ == "__main__":
    sys.exit(main())
