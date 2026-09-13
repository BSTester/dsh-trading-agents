#!/usr/bin/env python3
"""参数敏感性分析：对策略参数网格逐一回测，输出指标矩阵（供热力图）。

用法:
  python sensitivity.py --ticker 600519 --strategy ma_cross \
      --fast-grid 3,5,10,15 --slow-grid 10,20,30,60 --metric total_return
  python sensitivity.py --ticker 600519 --strategy rsi \
      --buy-grid 20,25,30,35 --sell-grid 65,70,75,80 --metric sharpe
输出: JSON {ticker, strategy, metric, rows:[...], cols:[...], matrix:[[...]], best:{...}, note}
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trading_datasource.backtest import (  # noqa: E402
    load_data, ma_cross_signal, metrics, rsi_signal, run)

METRICS = ("total_return", "annualized", "sharpe", "max_drawdown", "win_rate")


def evaluate(df, strategy, params):
    if strategy == "ma_cross":
        fast, slow = params
        if fast >= slow:
            return None
        signal = lambda d: ma_cross_signal(d, fast, slow)
    else:
        buy, sell = params
        if buy >= sell:
            return None
        signal = lambda d: rsi_signal(d, buy, sell)

    trades, curve, _final = run(df, signal)
    summary = metrics(curve)
    sells = [t for t in trades if t["type"] == "sell"]
    wins = [t for t in sells if t.get("return", 0) > 0]
    summary["trades"] = len(sells)
    summary["win_rate"] = round(len(wins) / len(sells), 4) if sells else 0.0
    return summary


def grid(text):
    values = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        number = int(part)
        if number <= 0 or number > 500:
            raise ValueError(f"参数越界：{number}")
        values.append(number)
    if len(values) < 2 or len(values) > 8:
        raise ValueError("每组参数需 2..8 个取值")
    return values


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--strategy", default="ma_cross", choices=["ma_cross", "rsi"])
    ap.add_argument("--fast-grid", default="3,5,10,15")
    ap.add_argument("--slow-grid", default="10,20,30,60")
    ap.add_argument("--buy-grid", default="20,25,30,35")
    ap.add_argument("--sell-grid", default="65,70,75,80")
    ap.add_argument("--metric", default="total_return", choices=METRICS)
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--source", default="auto")
    args = ap.parse_args()

    try:
        rows = grid(args.fast_grid if args.strategy == "ma_cross" else args.buy_grid)
        cols = grid(args.slow_grid if args.strategy == "ma_cross" else args.sell_grid)
        df, used_source = load_data(args.ticker, args.start, args.source)
        if len(df) < 60:
            raise ValueError(f"数据不足（{len(df)} 条）")

        matrix, best = [], None
        for row in rows:
            line = []
            for col in cols:
                params = (row, col)
                summary = evaluate(df, args.strategy, params)
                value = None if summary is None else summary.get(args.metric)
                line.append(value)
                if value is not None and (best is None or value > best["value"]):
                    best = {"row": row, "col": col, "value": round(value, 5),
                            "trades": summary.get("trades"), "win_rate": summary.get("win_rate")}
            matrix.append(line)

        if best is None:
            raise ValueError("所有参数组合均无有效结果")

        print(json.dumps({
            "ticker": args.ticker, "strategy": args.strategy, "metric": args.metric,
            "row_label": "fast" if args.strategy == "ma_cross" else "rsi_buy",
            "col_label": "slow" if args.strategy == "ma_cross" else "rsi_sell",
            "rows": rows, "cols": cols, "matrix": matrix, "best": best,
            "bars": len(df), "as_of": str(df["date"].iloc[-1]),
            "note": "样本内网格搜索，存在过拟合风险；上线前需做样本外/滚动前推验证。",
        }, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": str(error)[:300]}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
