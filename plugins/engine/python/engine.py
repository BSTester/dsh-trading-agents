#!/usr/bin/env python3
"""量化交易闭环引擎（P3 骨架）—— 信号 → 风控 → 下单意图 → 台账 → 复盘。

数据：sina 源真实 A股日线（复用 backtest 的信号与成本逻辑）。
账户：严格读 trade_mode 开关（sim/live），本地台账 ~/.dsh/quant-ledger.json
      按模式隔离（两个模式各有独立台账，绝不混用）。
执行：默认只产「下单意图」JSON，不直接下单；由会话内 AI 按模式经 MCP 工具执行，
      或 --apply 落到本地台账（闭环回测验证用）。

用法：
  python engine.py signal  --ticker 600519 --strategy rsi
  python engine.py decide  --ticker 600519 --strategy rsi [--apply]
  python engine.py report
"""
import argparse
import json
import math
import sys
from pathlib import Path

from backtest import load_data, ma_cross_signal, rsi_signal, COMMISSION, SLIPPAGE, STAMP_TAX

DSH = Path.home() / ".dsh"
LEDGER = DSH / "quant-ledger.json"
MODE_FILE = DSH / "trading-account-mode"
INITIAL_CASH = 1_000_000.0
RISK_PER_TRADE = 0.01      # 单笔风险 = 权益的 1%
STOP_ATR_MULT = 2.0        # 止损 = 入场价 - 2*ATR
MAX_POSITIONS = 5


def read_mode():
    if MODE_FILE.exists():
        v = MODE_FILE.read_text().strip().lower()
        if v in ("sim", "live"):
            return v
    return "sim"


def load_ledger(mode):
    if LEDGER.exists():
        data = json.loads(LEDGER.read_text())
        return data.get(mode, {"cash": INITIAL_CASH, "positions": {}, "history": []})
    return {"cash": INITIAL_CASH, "positions": {}, "history": []}


def save_ledger(mode, ledger):
    DSH.mkdir(parents=True, exist_ok=True)
    all_ledgers = {}
    if LEDGER.exists():
        all_ledgers = json.loads(LEDGER.read_text())
    all_ledgers[mode] = ledger
    LEDGER.write_text(json.dumps(all_ledgers, ensure_ascii=False, indent=1))


def compute_signal(ticker, strategy, fast=5, slow=20, rsi_buy=25, rsi_sell=75):
    df = load_data(ticker, "2023-01-01", "sina")
    if strategy == "ma_cross":
        sig = ma_cross_signal(df, fast, slow)
    else:
        sig = rsi_signal(df, rsi_buy, rsi_sell)
    last = int(sig.iloc[-1])
    price = float(df["close"].iloc[-1])
    date = str(df["date"].iloc[-1])
    atr = float((df["high"] - df["low"]).tail(14).mean())
    label = "BUY" if last == 1 else ("SELL" if last == -1 else "HOLD")
    return {"ticker": ticker, "strategy": strategy, "date": date, "price": round(price, 2),
            "signal": label, "atr": round(atr, 2)}


def decide(ticker, strategy, apply_fill=False, **kw):
    mode = read_mode()
    ledger = load_ledger(mode)
    s = compute_signal(ticker, strategy, **kw)
    price, atr, sig = s["price"], s["atr"], s["signal"]
    pos = ledger["positions"].get(ticker)
    order = None

    if sig == "BUY" and pos is None and len(ledger["positions"]) < MAX_POSITIONS:
        stop = price - STOP_ATR_MULT * atr
        risk_budget = (ledger["cash"] + equity(ledger, {t: p for t, p in ledger["positions"].items()})) * RISK_PER_TRADE
        per_share = max(price - stop, 0.01)
        shares = math.floor(risk_budget / per_share / 100) * 100  # 整手
        if shares >= 100 and shares * price <= ledger["cash"]:
            order = {"action": "BUY", "ticker": ticker, "shares": shares,
                     "price": price, "stop": round(stop, 2),
                     "reason": f"{s['strategy']} 信号，ATR止损 2x"}
    elif sig == "SELL" and pos is not None:
        order = {"action": "SELL", "ticker": ticker, "shares": pos["shares"],
                 "price": price, "stop": None,
                 "reason": f"{s['strategy']} 卖出信号"}

    if order and apply_fill:
        fill_order(mode, ledger, order)
        save_ledger(mode, ledger)

    return {"mode": mode, "signal": s, "order": order, "equity": round(equity(ledger, ledger["positions"]), 2)}


def fill_order(mode, ledger, order):
    t, shares, price = order["ticker"], order["shares"], order["price"]
    if order["action"] == "BUY":
        cost = shares * price
        fee = cost * (COMMISSION + SLIPPAGE)
        ledger["cash"] -= cost + fee
        ledger["positions"][t] = {"shares": shares, "entry": price,
                                  "stop": order["stop"], "date": order.get("date", "")}
        ledger["history"].append({**order, "fee": round(fee, 2)})
    else:
        pos = ledger["positions"].pop(t, None)
        if pos:
            proceeds = shares * price
            fee = proceeds * (COMMISSION + STAMP_TAX + SLIPPAGE)
            ledger["cash"] += proceeds - fee
            ret = (price - pos["entry"]) / pos["entry"]
            ledger["history"].append({**order, "fee": round(fee, 2),
                                      "return": round(ret, 4)})


def equity(ledger, positions):
    return ledger["cash"] + sum(p["shares"] * latest_price(t) for t, p in positions.items())


def latest_price(ticker):
    try:
        return float(load_data(ticker, "2025-01-01", "sina")["close"].iloc[-1])
    except Exception:
        return 0.0


def report():
    mode = read_mode()
    ledger = load_ledger(mode)
    positions = ledger["positions"]
    eq = equity(ledger, positions)
    sells = [h for h in ledger["history"] if h["action"] == "SELL"]
    wins = [h for h in sells if h.get("return", 0) > 0]
    print(json.dumps({
        "mode": mode, "cash": round(ledger["cash"], 2), "equity": round(eq, 2),
        "total_return": round(eq / INITIAL_CASH - 1, 4),
        "positions": {t: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in p.items()}
                      for t, p in positions.items()},
        "trades": len(sells), "win_rate": round(len(wins) / len(sells), 4) if sells else 0.0,
        "recent": ledger["history"][-5:],
    }, ensure_ascii=False, indent=1))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("signal", "decide"):
        p = sub.add_parser(name)
        p.add_argument("--ticker", required=True)
        p.add_argument("--strategy", default="rsi", choices=["rsi", "ma_cross"])
        p.add_argument("--fast", type=int, default=5)
        p.add_argument("--slow", type=int, default=20)
        p.add_argument("--rsi-buy", type=int, default=25)
        p.add_argument("--rsi-sell", type=int, default=75)
        if name == "decide":
            p.add_argument("--apply", action="store_true", help="把意图落到本地台账（闭环验证）")
    sub.add_parser("report")
    args = ap.parse_args()

    if args.cmd == "signal":
        print(json.dumps(compute_signal(args.ticker, args.strategy, args.fast, args.slow,
                                        args.rsi_buy, args.rsi_sell), ensure_ascii=False, indent=1))
    elif args.cmd == "decide":
        out = decide(args.ticker, args.strategy, args.apply, fast=args.fast, slow=args.slow,
                     rsi_buy=args.rsi_buy, rsi_sell=args.rsi_sell)
        print(json.dumps(out, ensure_ascii=False, indent=1))
    elif args.cmd == "report":
        return report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
