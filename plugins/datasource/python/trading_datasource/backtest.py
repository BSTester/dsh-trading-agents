#!/usr/bin/env python3
"""量化回测引擎 —— 历史K线 + 策略 + 绩效指标（含成本建模）。

本模块是**唯一**的回测实现，由 engine 与 workbench 共同使用（此前两个插件各有一份 278 行副本，
仅差一行 import，长期有漂移风险）。

策略：
  ma_cross   双均线金叉/死叉（快线上穿慢线买入，下穿卖出）
  rsi        RSI 均值回归（超卖买入、超买卖出）

成本建模：佣金 0.03%（双边）+ 印花税 0.1%（卖出，A股）+ 滑点 0.1%（双边）

用法：
  python backtest.py --ticker 600519 --strategy ma_cross --fast 5 --slow 20
  python backtest.py --ticker AAPL --strategy rsi --rsi-buy 30 --rsi-sell 70 --source yahoo

输出：JSON {summary:{...}, trades:[...]}
"""
import argparse
import json
import math
import sys
from datetime import date

from .market import MAX_BARS, is_a_share, to_futu_symbol  # noqa: E402  （A 股判定/符号归一的唯一实现）
# 注意：本模块自己有一个字符串常量 EXECUTION_SOURCE，标签表必须改名导入，
# 否则会把它覆盖成字典（曾因此报 "string indices must be integers"）。
from .labels import (  # noqa: E402  （中文标签唯一事实来源）
    EXECUTION_SOURCE as EXECUTION_SOURCE_ZH, EXECUTION_TIMING, END_POSITION_POLICY,
    trade_status_label, trade_type_label)

COMMISSION = 0.0003   # 双边佣金
STAMP_TAX = 0.001     # 卖出印花税（A股）
SLIPPAGE = 0.001      # 双边滑点
EXECUTION_SOURCE = "local_simulation"
EXECUTION_SOURCE_LABEL = EXECUTION_SOURCE_ZH["local_simulation"]


def validate_data(df):
    """Reject unusable daily OHLCV instead of silently dropping bad observations."""
    import numpy as np
    import pandas as pd

    required = ["date", "open", "high", "low", "close", "volume"]
    if df.empty or any(column not in df.columns for column in required):
        raise ValueError("Nonempty daily OHLCV data with date/open/high/low/close/volume required")
    df = df[required].copy().reset_index(drop=True)
    dates = pd.to_datetime(df["date"], errors="coerce")
    days = dates.dt.normalize()
    if dates.isna().any() or days.duplicated().any() or not days.is_monotonic_increasing:
        raise ValueError("Daily dates must be valid, unique and strictly increasing")
    numeric = df[required[1:]].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(numeric.to_numpy(dtype=float)).all():
        raise ValueError("OHLCV values must be finite numbers")
    if (numeric[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("OHLC prices must be positive")
    if (numeric["volume"] < 0).any():
        raise ValueError("Volume must be nonnegative")
    if ((numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any()
            or (numeric["low"] > numeric[["open", "close"]].min(axis=1)).any()):
        raise ValueError("OHLC high/low must enclose open and close")
    df[required[1:]] = numeric
    df["date"] = days.dt.strftime("%Y-%m-%d")
    return df


def _shortfall_note(data_start, requested_start):
    """行情起点晚于请求起点时的说明；小差异只是节假日，不算异常。"""
    from datetime import date as _date
    try:
        gap = (_date.fromisoformat(data_start) - _date.fromisoformat(requested_start)).days
    except ValueError:
        return ""
    if gap <= 20:
        return ""
    return f"；请求起点 {requested_start} 早于可取到的最早行情，前 {gap} 天未参与回测"


def bars_to_frame(bars, start=None):
    """行情字典 → 校验过的日线 DataFrame（唯一的字典→表转换实现）。"""
    import pandas as pd
    frame = pd.DataFrame([{"date": b["t"], "open": b["o"], "high": b["h"],
                           "low": b["l"], "close": b["c"], "volume": b["v"]} for b in bars])
    if start is not None and not frame.empty:
        frame = frame[frame["date"] >= start].reset_index(drop=True)
    return frame


def load_data(ticker, start, source):
    """加载并校验日线 OHLCV。返回 (DataFrame, 实际使用的数据源标签)。

    返回实际来源而不是请求值：面板上写 "auto" 等于没写 —— 用户无从知道这次
    回测的行情到底来自富途、新浪还是 Yahoo，也就无法判断口径（复权方式不同）。
    数据源错误向调用方传播。
    """
    if source not in ("auto", "stooq", "synth", "yahoo", "sina", "akshare"):
        raise ValueError(f"Unsupported data source: {source}")
    if source == "auto":
        # 统一行情入口：富途优先（全市场）；A 股长历史走新浪，港美股长历史走 Yahoo。
        from datetime import date as _date
        from .market import load_bars
        needed = max(120, int((_date.today() - _date.fromisoformat(start)).days * 0.72) + 40)
        # 上限取共享的 MAX_BARS（2000，约 8 年），而不是拍脑袋的 900：
        # 否则请求 2010 年起的回测会被悄悄截到最近三年。
        bars, used, _stale = load_bars(ticker, "1d", min(needed, MAX_BARS))
        return bars_to_frame(bars, start), used
    if source == "stooq":
        import pandas as pd
        symbol = ticker.lower().replace("-", ".")
        url = f"https://stooq.com/q/d/l/?s={symbol}&d1={start}&i=d"
        df = pd.read_csv(url)
        df = df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                                "Low": "low", "Close": "close", "Volume": "volume"})
        df["date"] = df["date"].astype(str)
        return validate_data(df), "stooq"
    if source == "synth":
        import pandas as pd, numpy as np
        n = 300
        rng = np.random.default_rng(42)
        rets = rng.normal(0.0005, 0.02, n)
        close = 100 * np.cumprod(1 + rets)
        open_ = np.roll(close, 1); open_[0] = 100
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        return validate_data(pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"), "open": open_,
            "high": np.maximum(open_, close) * 1.01,
            "low": np.minimum(open_, close) * 0.99,
            "close": close, "volume": 1e6,
        }).round(4)), "synth（合成数据，仅供自检）"
    if source == "yahoo":
        # 复用共享层的 Yahoo 实现与符号归一（港股的 Yahoo 代码是 4 位）
        from .market import fetch_yahoo
        bars, used = fetch_yahoo(ticker, "1d", 2000)
        return bars_to_frame(bars, start), used
    if not is_a_share(ticker):
        raise ValueError(f"{source} 仅支持 A 股；{ticker} 请用 --source auto / yahoo")
    import akshare as ak
    code = to_futu_symbol(ticker).split(".")[1]
    if source == "sina":
        prefix = {"6": "sh", "9": "sh", "4": "bj", "8": "bj"}.get(code[0], "sz")
        df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}",
                                 start_date=start.replace("-", ""), adjust="qfq")
        df = df.rename(columns={"date": "date", "open": "open", "high": "high",
                                "low": "low", "close": "close", "volume": "volume"})
        df["date"] = df["date"].astype(str)
        return validate_data(df), "akshare/sina"
    df = ak.stock_zh_a_hist(symbol=code, period="daily",
                            start_date=start.replace("-", ""), adjust="qfq")
    df = df.rename(columns={"日期": "date", "开盘": "open", "最高": "high",
                            "最低": "low", "收盘": "close", "成交量": "volume"})
    df["date"] = df["date"].astype(str)
    return validate_data(df), "akshare/东方财富"


def ma(series, n):
    return series.rolling(n).mean()


def rsi(series, n=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, 1e-12)
    return 100 - 100 / (1 + rs)


def ma_cross_signal(df, fast, slow):
    f, s = ma(df["close"], fast), ma(df["close"], slow)
    return (f > s).astype(int).diff().fillna(0)  # +1 金叉买入，-1 死叉卖出


def rsi_signal(df, buy, sell):
    r = rsi(df["close"])
    sig = [0] * len(df)
    for i in range(1, len(df)):
        if r.iloc[i] < buy and r.iloc[i - 1] >= buy:
            sig[i] = 1
        elif r.iloc[i] > sell and r.iloc[i - 1] <= sell:
            sig[i] = -1
    import pandas as pd
    return pd.Series(sig, index=df.index)


def run(df, signal_fn):
    """Long-only daily simulation: previous-bar signals fill at the next open.

    Buy whole lots with costs included, enforce T+1, and mark remaining holdings
    to the last close without inventing an end-of-test liquidation.
    """
    import pandas as pd

    df = validate_data(df)
    sig = signal_fn(df)
    if (not isinstance(sig, pd.Series) or len(sig) != len(df)
            or not sig.index.equals(df.index) or not sig.isin([-1, 0, 1]).all()):
        raise ValueError("Signals must align with data and contain only -1, 0, 1")
    trades = []
    pos = 0
    cash = 1_000_000.0
    entry_cost = 0.0
    entry_date = None
    equity_curve = []

    for i in range(len(df)):
        price = float(df["open"].iloc[i])
        close = float(df["close"].iloc[i])
        date = str(df["date"].iloc[i])
        s = int(sig.iloc[i - 1]) if i else 0
        signal_date = str(df["date"].iloc[i - 1]) if i else None

        if s == 1 and pos == 0:
            lots = math.floor(cash / (price * 100 * (1 + COMMISSION + SLIPPAGE)))
            if lots > 0:
                cost = price * 100 * lots
                fee = cost * (COMMISSION + SLIPPAGE)
                cash -= cost + fee
                pos = lots * 100
                entry_cost = cost + fee
                entry_date = date
                trades.append({"type": "buy", "type_label": trade_type_label("buy"),
                               "date": date, "price": price,
                               "signal_date": signal_date, "status": "open",
                               "status_label": trade_status_label("open"),
                               "execution_source": EXECUTION_SOURCE,
                               "execution_source_label": EXECUTION_SOURCE_LABEL,
                               "shares": pos, "fee": round(fee, 2)})
        elif s == -1 and pos > 0 and date > entry_date:
            proceeds = price * pos
            fee = proceeds * (COMMISSION + STAMP_TAX + SLIPPAGE)
            cash += proceeds - fee
            ret = (proceeds - fee) / entry_cost - 1
            trades[-1]["status"] = "closed"
            trades[-1]["status_label"] = trade_status_label("closed")
            trades.append({"type": "sell", "type_label": trade_type_label("sell"),
                           "date": date, "price": price,
                           "signal_date": signal_date, "execution_source": EXECUTION_SOURCE,
                           "execution_source_label": EXECUTION_SOURCE_LABEL,
                           "shares": pos, "fee": round(fee, 2), "return": round(ret, 4),
                           "hold_days": (pd.Timestamp(date) - pd.Timestamp(entry_date)).days})
            pos = 0
            entry_cost = 0.0

        mv = cash + pos * close
        equity_curve.append(mv)

    # Mark to market only: an open buy remains explicitly open in the trade log.
    final = cash + pos * float(df["close"].iloc[-1])
    equity_curve[-1] = final

    return trades, equity_curve, final


def metrics(equity_curve, initial=1_000_000.0):
    import pandas as pd
    eq = pd.Series(equity_curve)
    rets = eq.pct_change().dropna()
    total_return = eq.iloc[-1] / initial - 1
    n = len(rets)
    sharpe = (rets.mean() / rets.std() * math.sqrt(252)) if rets.std() > 0 else 0.0
    dd = (eq / eq.cummax() - 1).min()
    annual = (1 + total_return) ** (252 / n) - 1 if n > 0 else 0.0
    return {"total_return": round(total_return, 4), "annualized": round(annual, 4),
            "sharpe": round(sharpe, 3), "max_drawdown": round(dd, 4),
            "bars": n, "final_equity": round(eq.iloc[-1], 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--strategy", default="ma_cross", choices=["ma_cross", "rsi"])
    ap.add_argument("--source", default="auto", choices=["auto", "sina", "akshare", "stooq", "yahoo", "synth"])
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=20)
    ap.add_argument("--rsi-buy", type=int, default=30)
    ap.add_argument("--rsi-sell", type=int, default=70)
    args = ap.parse_args()

    df, used_source = load_data(args.ticker, args.start, args.source)
    if len(df) < 60:
        print(json.dumps({"error": f"数据不足（{len(df)} 条）"}, ensure_ascii=False))
        return 1

    if args.strategy == "ma_cross":
        signal = lambda d: ma_cross_signal(d, args.fast, args.slow)
        label = f"ma_cross({args.fast},{args.slow})"
    else:
        signal = lambda d: rsi_signal(d, args.rsi_buy, args.rsi_sell)
        label = f"rsi({args.rsi_buy},{args.rsi_sell})"

    trades, eq, final = run(df, signal)
    buys = [t for t in trades if t["type"] == "buy"]
    sells = [t for t in trades if t["type"] == "sell"]
    wins = [t for t in sells if t.get("return", 0) > 0]

    out = {
        "ticker": args.ticker, "strategy": label,
        # 实际来源，不是请求值：面板写 "auto" 等于没写，用户无从判断口径
        "source": used_source, "requested_source": args.source,
        "data_start": str(df["date"].iloc[0]), "data_end": str(df["date"].iloc[-1]),
        "requested_start": args.start,
        # 富途单次上限 370 根、各源历史长度不同，可能拿不到请求的全部区间；
        # 静默缩短回测区间会让人以为覆盖了整段时间，必须显式说明。
        "coverage_note": (f"实际行情区间 {df['date'].iloc[0]} → {df['date'].iloc[-1]}"
                          + _shortfall_note(str(df["date"].iloc[0]), args.start)),
        "execution_source": EXECUTION_SOURCE,
        "execution_source_label": EXECUTION_SOURCE_LABEL,
        # 口径说明此前以英文枚举直接展示，界面读不懂，这里一并给中文
        "execution_timing": "previous_bar_next_open",
        "execution_timing_label": EXECUTION_TIMING["previous_bar_next_open"],
        "end_position_policy": "mark_to_market_no_liquidation",
        "end_position_policy_label": END_POSITION_POLICY["mark_to_market_no_liquidation"],
        "open_positions": [
            {**trade, "ticker": args.ticker,
             "type_label": trade_type_label(trade.get("type")),
             "status_label": trade_status_label(trade.get("status")),
             "mark_price": float(df["close"].iloc[-1]),
             "mark_date": str(df["date"].iloc[-1]),
             "market_value": round(trade["shares"] * float(df["close"].iloc[-1]), 2)}
            for trade in trades if trade.get("status") == "open"
        ],
        "bars": len(df),
        "summary": {**metrics(eq),
                    "trades": len(sells),
                    "win_rate": round(len(wins) / len(sells), 4) if sells else 0.0},
        "recent_trades": trades[-6:],
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
