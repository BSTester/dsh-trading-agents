#!/usr/bin/env python3
"""Local-only A-share simulation: signals, risk previews and a simulated ledger.

DSH_HOME (default ~/.dsh) holds the mode and ledger. Only sim mode supports
decide/--apply; these commands NEVER contact a broker or execute a real order.
Prices are daily observations, not executable broker quotes. Stops are checked
on invocation, not continuously; T+1 blocks selling on the acquisition date.

用法：
  python engine.py signal  --ticker 600519 --strategy rsi
  python engine.py decide  --ticker 600519 --strategy rsi [--apply]
  python engine.py report
"""
import argparse
from contextlib import contextmanager, nullcontext
from datetime import date
import json
import math
import os
import sys
from pathlib import Path
from uuid import uuid4

if __package__:
    from .backtest import (load_data, validate_data, ma_cross_signal, rsi_signal,
                           COMMISSION, SLIPPAGE, STAMP_TAX)
else:
    from backtest import (load_data, validate_data, ma_cross_signal, rsi_signal,
                          COMMISSION, SLIPPAGE, STAMP_TAX)

import risk_config  # noqa: E402  （风控参数单一事实来源）

DSH = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
LEDGER = DSH / "quant-ledger.json"
MODE_FILE = DSH / "trading-account-mode"
EXECUTION_SOURCE = "local_simulation"
INITIAL_CASH = 1_000_000.0
# 风控参数已迁移到 risk_config.py（~/.dsh/trading-risk.json，可配置）；
# 下方仅为默认值参考，运行时一律以 risk_config.load() 为准。
DEFAULT_RISK = {"risk_per_trade": 0.01, "stop_atr_mult": 2.0, "max_positions": 5}


def read_mode():
    try:
        value = MODE_FILE.read_text().strip()
    except FileNotFoundError:
        return "sim"
    if value not in ("sim", "live"):
        raise ValueError("Invalid account mode; expected sim or live")
    return value


def require_sim(mode):
    if mode != "sim":
        raise ValueError("Local simulator supports sim mode only; no broker execution")


@contextmanager
def ledger_lock():
    """Serialize the full read/modify/write, using a stable sidecar inode."""
    DSH.mkdir(parents=True, exist_ok=True)
    with LEDGER.with_suffix(".lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def load_ledger(mode):
    require_sim(mode)
    if LEDGER.exists():
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
        return data.get(mode, {"cash": INITIAL_CASH, "positions": {}, "history": []})
    return {"cash": INITIAL_CASH, "positions": {}, "history": []}


def save_ledger(mode, ledger):
    require_sim(mode)
    with ledger_lock():
        _save_ledger(mode, ledger)


@contextmanager
def mode_commit_lock():
    """Share the workbench's exclusive-create lock only for local persistence."""
    lock = DSH / "trading-workbench.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as error:
        raise ValueError("Trading mode lock is busy; retry the local simulation") from error
    try:
        with handle:
            handle.write(str(os.getpid()))
            handle.flush()
            yield
    finally:
        lock.unlink()


def _save_ledger(mode, ledger):
    require_sim(mode)
    DSH.mkdir(parents=True, exist_ok=True)
    with mode_commit_lock():
        require_sim(read_mode())
        all_ledgers = {}
        if LEDGER.exists():
            all_ledgers = json.loads(LEDGER.read_text(encoding="utf-8"))
        all_ledgers[mode] = ledger
        staging = LEDGER.with_name(f".{LEDGER.name}.{uuid4().hex}.pending")
        try:
            with staging.open("x", encoding="utf-8") as stream:
                json.dump(all_ledgers, stream, ensure_ascii=False, indent=1, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, LEDGER)
        finally:
            staging.unlink(missing_ok=True)


def compute_signal(ticker, strategy, fast=5, slow=20, rsi_buy=25, rsi_sell=75):
    df = validate_data(load_data(ticker, "2023-01-01", "auto"))
    if strategy == "ma_cross":
        sig = ma_cross_signal(df, fast, slow)
    elif strategy == "rsi":
        sig = rsi_signal(df, rsi_buy, rsi_sell)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")
    last = int(sig.iloc[-1])
    price = float(df["close"].iloc[-1])
    date = str(df["date"].iloc[-1])
    import pandas as pd
    previous_close = df["close"].shift(1)
    true_range = pd.concat([
        df["high"] - df["low"],
        (df["high"] - previous_close).abs(),
        (df["low"] - previous_close).abs(),
    ], axis=1).max(axis=1)
    atr = float(true_range.tail(14).mean())
    label = "BUY" if last == 1 else ("SELL" if last == -1 else "HOLD")
    return {"ticker": ticker, "strategy": strategy, "date": date, "price": price,
            "signal": label, "atr": atr, "execution_source": EXECUTION_SOURCE}


def decide(ticker, strategy, apply_fill=False, **kw):
    mode = read_mode()
    require_sim(mode)
    with ledger_lock() if apply_fill else nullcontext():
        require_sim(read_mode())
        return _decide(ticker, strategy, apply_fill, **kw)


def _decide(ticker, strategy, apply_fill, **kw):
    mode = "sim"
    ledger = load_ledger(mode)
    s = compute_signal(ticker, strategy, **kw)
    price, atr, sig = s["price"], s["atr"], s["signal"]
    if not math.isfinite(price) or price <= 0 or not math.isfinite(atr) or atr < 0:
        raise ValueError("Price must be positive and ATR finite and nonnegative")
    fill_date = date.fromisoformat(s["date"]).isoformat()
    pos = ledger["positions"].get(ticker)
    order = None
    blocked_reason = None
    risk = risk_config.load()  # 非法配置即拒绝交易，不静默降级
    quotes = {ticker: price}
    eq = equity(ledger, ledger["positions"], quotes)

    if pos is not None and (price <= pos.get("stop", 0) or sig == "SELL"):
        if not sellable(pos, fill_date):
            blocked_reason = "T+1: acquisition date missing or not before the fill date"
        else:
            order = {"action": "SELL", "ticker": ticker, "shares": pos["shares"],
                     "price": price, "stop": None,
                     "reason": "ATR stop triggered" if price <= pos.get("stop", 0)
                               else f"{s['strategy']} sell signal"}
    elif sig == "BUY" and pos is None and len(ledger["positions"]) < risk["max_positions"]:
        stop = price - risk["stop_atr_mult"] * atr
        risk_budget = min(eq * risk["risk_per_trade"], eq * risk["max_position_pct"])
        per_share = max(price - stop, 0.01)
        risk_lots = math.floor(risk_budget / per_share / 100)
        cash_lots = math.floor(ledger["cash"] / (price * (1 + COMMISSION + SLIPPAGE) * 100))
        shares = min(risk_lots, cash_lots) * 100
        if shares >= 100 and 0 < stop < price:
            order = {"action": "BUY", "ticker": ticker, "shares": shares,
                     "price": price, "stop": stop,
                     "reason": f"{s['strategy']} 信号，ATR止损 2x"}

    if order:
        order.update(date=fill_date, execution_source=EXECUTION_SOURCE)
    if order and apply_fill:
        require_sim(read_mode())
        fill_order(mode, ledger, order)
        _save_ledger(mode, ledger)

    return {"mode": mode, "execution_source": EXECUTION_SOURCE, "signal": s,
            "order": order, "applied": bool(order and apply_fill),
            "blocked_reason": blocked_reason,
            "equity": round(equity(ledger, ledger["positions"], quotes), 2)}


def sellable(position, fill_date):
    try:
        return date.fromisoformat(position["date"]) < date.fromisoformat(fill_date)
    except (KeyError, TypeError, ValueError):
        return False


def fill_order(mode, ledger, order):
    require_sim(mode)
    require_sim(read_mode())
    t, shares, price = order["ticker"], order["shares"], order["price"]
    fill_date = date.fromisoformat(order["date"]).isoformat()
    if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
        raise ValueError("Shares must be a positive integer")
    if not math.isfinite(price) or price <= 0:
        raise ValueError("Price must be finite and positive")
    fill = {**order, "date": fill_date, "execution_source": EXECUTION_SOURCE}
    if order["action"] == "BUY":
        if shares % 100 or t in ledger["positions"]:
            raise ValueError("Buy requires whole lots and no existing position")
        stop = order["stop"]
        if not math.isfinite(stop) or not 0 < stop < price:
            raise ValueError("Buy requires a positive stop below entry")
        cost = shares * price
        fee = cost * (COMMISSION + SLIPPAGE)
        if cost + fee > ledger["cash"]:
            raise ValueError("Insufficient simulation cash including fees")
        ledger["cash"] -= cost + fee
        ledger["positions"][t] = {"shares": shares, "entry": price,
                                  "stop": stop, "date": fill_date, "entry_fee": fee}
        ledger["history"].append({**fill, "fee": round(fee, 2)})
    elif order["action"] == "SELL":
        pos = ledger["positions"].get(t)
        if pos is None or shares != pos["shares"]:
            raise ValueError("Sell must close the existing simulation position")
        if not sellable(pos, fill_date):
            raise ValueError("T+1: cannot sell on/before acquisition date or with missing date")
        proceeds = shares * price
        fee = proceeds * (COMMISSION + STAMP_TAX + SLIPPAGE)
        basis = shares * pos["entry"] + pos.get(
            "entry_fee", shares * pos["entry"] * (COMMISSION + SLIPPAGE))
        ret = (proceeds - fee) / basis - 1
        ledger["positions"].pop(t)
        ledger["cash"] += proceeds - fee
        ledger["history"].append({**fill, "fee": round(fee, 2), "return": round(ret, 4)})
    else:
        raise ValueError("Unsupported simulation action")


def equity(ledger, positions, quotes=None):
    quotes = {} if quotes is None else quotes
    value = ledger["cash"]
    for ticker, position in positions.items():
        if ticker not in quotes:
            quotes[ticker] = latest_price(ticker)
        price = quotes[ticker]
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"Invalid quote for {ticker}")
        value += position["shares"] * price
    return value


def latest_price(ticker):
    df = validate_data(load_data(ticker, "2025-01-01", "auto"))
    return float(df["close"].iloc[-1])


def report():
    mode = read_mode()
    if mode == "live":
        print(json.dumps({
            "mode": mode, "execution_source": EXECUTION_SOURCE, "status": "unavailable",
            "cash": None, "equity": None, "total_return": None, "positions": {},
            "trades": None, "win_rate": None, "recent": [],
            "reason": "Local simulator supports sim only; live broker account data is unavailable",
        }, ensure_ascii=False, indent=1))
        return 0
    ledger = load_ledger(mode)
    positions = ledger["positions"]
    eq = equity(ledger, positions)
    sells = [h for h in ledger["history"] if h["action"] == "SELL"]
    wins = [h for h in sells if h.get("return", 0) > 0]
    print(json.dumps({
        "mode": mode, "execution_source": EXECUTION_SOURCE, "status": "available",
        "cash": round(ledger["cash"], 2), "equity": round(eq, 2),
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
