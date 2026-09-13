#!/usr/bin/env python3
"""工作台分析层：权益曲线回放、持仓盯市、相关性矩阵。

数据来源：本地模拟台账 ~/.dsh/quant-ledger.json（只读回放）+ 行情序列（bars.py）。

用法:
  python analytics.py equity --mode sim [--window 250]
  python analytics.py positions --mode sim
  python analytics.py correlation --tickers 600519,000001,601318 [--window 120]
输出: JSON
"""
import argparse
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bars import fetch_a_share, is_a_share  # noqa: E402

DSH = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
LEDGER = DSH / "quant-ledger.json"
INITIAL_CASH = 1_000_000.0
TRADING_DAYS = 252


def load_ledger(mode):
    if not LEDGER.exists():
        return {"cash": INITIAL_CASH, "positions": {}, "history": []}
    try:
        data = json.loads(LEDGER.read_text())
    except (OSError, ValueError):
        return {"cash": INITIAL_CASH, "positions": {}, "history": []}
    section = data.get(mode)
    if not isinstance(section, dict):
        return {"cash": INITIAL_CASH, "positions": {}, "history": []}
    return {"cash": float(section.get("cash", INITIAL_CASH)),
            "positions": section.get("positions") or {},
            "history": section.get("history") or []}


def daily_closes(ticker, window):
    """取日线收盘序列 {date: close}（A股走新浪源）。"""
    if not is_a_share(ticker):
        raise RuntimeError(f"{ticker} 非 A 股，暂不支持日线序列")
    bars, _source = fetch_a_share(ticker, "1d", window)
    return {b["t"]: b["c"] for b in bars}


def equity_curve(mode="sim", window=250):
    """按台账成交记录回放，用日线盯市重建权益曲线。"""
    ledger = load_ledger(mode)
    history = [h for h in ledger["history"] if h.get("date")]
    history.sort(key=lambda h: (h["date"], 0 if h["action"] == "BUY" else 1))
    tickers = sorted({h["ticker"] for h in history} | set(ledger["positions"]))

    closes = {}
    for ticker in tickers:
        try:
            closes[ticker] = daily_closes(ticker, window)
        except Exception:
            closes[ticker] = {}

    dates = sorted({d for series in closes.values() for d in series})
    if not dates:
        return {"mode": mode, "points": [], "count": 0, "note": "无可用于盯市的日线数据"}

    # 只保留首个成交日之后的日期（之前无权益变化）
    first_trade = history[0]["date"] if history else dates[0]
    dates = [d for d in dates if d >= first_trade]

    cash = INITIAL_CASH
    holdings = {}
    points = []
    idx = 0
    for day in dates:
        while idx < len(history) and history[idx]["date"] <= day:
            trade = history[idx]
            shares, price = trade["shares"], trade["price"]
            fee = float(trade.get("fee") or 0)
            if trade["action"] == "BUY":
                cash -= shares * price + fee
                holdings[trade["ticker"]] = holdings.get(trade["ticker"], 0) + shares
            else:
                cash += shares * price - fee
                holdings[trade["ticker"]] = holdings.get(trade["ticker"], 0) - shares
                if holdings[trade["ticker"]] <= 0:
                    holdings.pop(trade["ticker"], None)
            idx += 1
        market = 0.0
        for ticker, shares in holdings.items():
            price = closes.get(ticker, {}).get(day)
            if price is not None:
                market += shares * price
        points.append({"t": day, "equity": round(cash + market, 2)})

    # 回撤序列 + 指标
    peak = -math.inf
    max_dd = 0.0
    final_peak = 0.0
    for point in points:
        peak = max(peak, point["equity"])
        final_peak = peak
        drawdown = point["equity"] / peak - 1 if peak > 0 else 0.0
        point["dd"] = round(drawdown, 5)
        max_dd = min(max_dd, drawdown)

    returns = []
    for i in range(1, len(points)):
        prev, cur = points[i - 1]["equity"], points[i]["equity"]
        if prev > 0:
            returns.append(cur / prev - 1)
    sharpe = 0.0
    if len(returns) > 1:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var)
        if std > 0:
            sharpe = mean / std * math.sqrt(TRADING_DAYS)

    current = points[-1]["equity"]
    return {"mode": mode, "count": len(points), "points": points,
            "initial": INITIAL_CASH, "current": current,
            "total_return": round(current / INITIAL_CASH - 1, 5),
            "max_drawdown": round(max_dd, 5), "sharpe": round(sharpe, 3),
            "trades": len(history),
            "note": "按台账成交记录回放并以日线盯市重建；未计分红除权调整之外的公司行为。"}


def positions_view(mode="sim"):
    ledger = load_ledger(mode)
    rows = []
    market_value = 0.0
    for ticker, pos in ledger["positions"].items():
        price = None
        try:
            closes = daily_closes(ticker, 30)
            if closes:
                price = closes[max(closes)]
        except Exception:
            price = None
        shares = pos.get("shares", 0)
        entry = float(pos.get("entry", 0))
        value = shares * price if price else shares * entry
        pnl = (price - entry) * shares if price else 0.0
        market_value += value
        rows.append({"ticker": ticker, "shares": shares, "entry": round(entry, 3),
                     "price": round(price, 3) if price else None,
                     "value": round(value, 2), "pnl": round(pnl, 2),
                     "pnl_pct": round((price / entry - 1) * 100, 2) if price and entry else None,
                     "stop": pos.get("stop"), "date": pos.get("date")})
    return {"mode": mode, "cash": round(ledger["cash"], 2),
            "market_value": round(market_value, 2),
            "equity": round(ledger["cash"] + market_value, 2),
            "positions": rows, "trades": len(ledger["history"])}


def correlation(tickers, window=120):
    series = {}
    for ticker in tickers:
        closes = daily_closes(ticker, window)
        series[ticker] = closes
    common = None
    for closes in series.values():
        keys = set(closes)
        common = keys if common is None else (common & keys)
    common = sorted(common or [])
    if len(common) < 20:
        return {"error": f"共同交易日不足（{len(common)} 天），无法计算相关性"}

    returns = {}
    for ticker, closes in series.items():
        values = [closes[d] for d in common]
        returns[ticker] = [(values[i] / values[i - 1] - 1) for i in range(1, len(values)) if values[i - 1] > 0]

    matrix = []
    for a in tickers:
        row = []
        for b in tickers:
            xs, ys = returns[a], returns[b]
            n = min(len(xs), len(ys))
            if n < 5:
                row.append(None); continue
            mx = sum(xs[:n]) / n; my = sum(ys[:n]) / n
            cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
            vx = sum((xs[i] - mx) ** 2 for i in range(n))
            vy = sum((ys[i] - my) ** 2 for i in range(n))
            row.append(round(cov / math.sqrt(vx * vy), 3) if vx > 0 and vy > 0 else None)
        matrix.append(row)
    return {"tickers": list(tickers), "matrix": matrix, "window": len(common),
            "as_of": common[-1], "note": "基于共同交易日的日收益率皮尔逊相关系数"}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("equity"); p1.add_argument("--mode", default="sim"); p1.add_argument("--window", type=int, default=250)
    p2 = sub.add_parser("positions"); p2.add_argument("--mode", default="sim")
    p3 = sub.add_parser("correlation"); p3.add_argument("--tickers", required=True); p3.add_argument("--window", type=int, default=120)
    args = ap.parse_args()
    try:
        if args.cmd == "equity":
            print(json.dumps(equity_curve(args.mode, args.window), ensure_ascii=False))
        elif args.cmd == "positions":
            print(json.dumps(positions_view(args.mode), ensure_ascii=False))
        else:
            tickers = [t.strip() for t in args.tickers.split(",") if t.strip()][:8]
            if not tickers:
                raise ValueError("至少一个标的")
            print(json.dumps(correlation(tickers, args.window), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": str(error)[:300]}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
