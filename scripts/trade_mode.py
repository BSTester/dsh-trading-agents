#!/usr/bin/env python3
"""账户模式开关 —— 模拟盘/实盘互斥隔离。

原理：写一个状态文件 ~/.dsh/trading-account-mode（sim 或 live）。
所有交易/持仓/资金操作前都必须读取它，并只用对应账户类型的工具；
两个账户类型绝不并存操作。默认 sim（安全）。

用法：
  python trade_mode.py              # 查看当前模式
  python trade_mode.py sim          # 切到模拟盘（默认）
  python trade_mode.py live         # 切到实盘（危险，调用方须先获用户明确确认）
"""
import sys
from pathlib import Path

MODE_FILE = Path.home() / ".dsh" / "trading-account-mode"
ALLOWED = {"sim", "live"}


def read_mode():
    if MODE_FILE.exists():
        v = MODE_FILE.read_text().strip().lower()
        if v in ALLOWED:
            return v
    return "sim"


def write_mode(mode):
    MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MODE_FILE.write_text(mode + "\n")
    try:
        MODE_FILE.chmod(0o600)
    except OSError:
        pass


def main():
    if len(sys.argv) < 2:
        print(read_mode())
        return 0
    mode = sys.argv[1].lower()
    if mode not in ALLOWED:
        print(f"非法模式 {mode!r}（允许 sim/live）", file=sys.stderr)
        return 1
    write_mode(mode)
    print(mode)
    return 0


if __name__ == "__main__":
    sys.exit(main())
