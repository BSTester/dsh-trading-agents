#!/usr/bin/env python3
"""量化回测引擎 —— 历史K线 + 策略 + 绩效指标（含成本建模）。

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

COMMISSION = 0.0003   # 双边佣金
STAMP_TAX = 0.001     # 卖出印花税（A股）
SLIPPAGE = 0.001      # 双边滑点


def load_data(ticker, start, source):
    """加载日线 OHLCV。source: stooq(默认,港美+多市场) / akshare(A股) / yahoo。"""
    if source == "stooq":
        import pandas as pd
        symbol = ticker.lower().replace("-", ".")
        url = f"https://stooq.com/q/d/l/?s={symbol}&d1={start}&i=d"
        df = pd.read_csv(url)
        df = df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                                "Low": "low", "Close": "close", "Volume": "volume"})
        df["date"] = df["date"].astype(str)
        return df[["date", "open", "high", "low", "close", "volume"]].dropna()
    if source == "synth":
        import pandas as pd, numpy as np
        n = 300
        rng = np.random.default_rng(42)
        rets = rng.normal(0.0005, 0.02, n)
        close = 100 * np.cumprod(1 + rets)
        open_ = np.roll(close, 1); open_[0] = 100
        dates = pd.date_range("2024-01-01", periods=n, freq="B")
        return pd.DataFrame({"date": dates.strftime("%Y-%m-%d"),
                             "open": open_, "high": close * 1.01, "low": close * 0.99,
                             "close": close, "volume": 1e6}).round(4)
    if source == "yahoo":
        import yfinance as yf
        df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
        df = df.reset_index()
        df = df.rename(columns={"Date": "date", "Open": "open", "High": "high",
                                "Low": "low", "Close": "close", "Volume": "volume"})
        return df[["date", "open", "high", "low", "close", "volume"]].dropna()
    import akshare as ak
    df = ak.stock_zh_a_hist(symbol=ticker.split(".")[0], period="daily",
                            start_date=start.replace("-", ""), adjust="qfq")
    df = df.rename(columns={"日期": "date", "开盘": "open", "最高": "high",
                            "最低": "low", "收盘": "close", "成交量": "volume"})
    df["date"] = df["date"].astype(str)
    return df[["date", "open", "high", "low", "close", "volume"]]


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
    """单标的多头回测（A股 T+0 简化，纯信号驱动）。"""
    sig = signal_fn(df)
    trades, eq = [], []
    pos = 0
    cash = 1_000_000.0
    entry_price = 0.0
    entry_date = None
    equity_curve = []

    for i in range(len(df)):
        price = float(df["close"].iloc[i])
        date = str(df["date"].iloc[i])
        s = int(sig.iloc[i])

        if s == 1 and pos == 0:
            lots = math.floor(cash / (price * 100))  # A股整手
            if lots > 0:
                cost = price * 100 * lots
                fee = cost * (COMMISSION + SLIPPAGE)
                cash -= cost + fee
                pos = lots * 100
                entry_price = price
                entry_date = date
                trades.append({"type": "buy", "date": date, "price": price,
                               "shares": pos, "fee": round(fee, 2)})
        elif s == -1 and pos > 0:
            proceeds = price * pos
            fee = proceeds * (COMMISSION + STAMP_TAX + SLIPPAGE)
            cash += proceeds - fee
            ret = (price - entry_price) / entry_price
            trades.append({"type": "sell", "date": date, "price": price,
                           "shares": pos, "fee": round(fee, 2), "return": round(ret, 4),
                           "hold_days": None})
            pos = 0
            entry_price = 0.0

        mv = cash + pos * price
        equity_curve.append(mv)

    # 期末平仓
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
    ap.add_argument("--source", default="stooq", choices=["stooq", "akshare", "yahoo", "synth"])
    ap.add_argument("--fast", type=int, default=5)
    ap.add_argument("--slow", type=int, default=20)
    ap.add_argument("--rsi-buy", type=int, default=30)
    ap.add_argument("--rsi-sell", type=int, default=70)
    args = ap.parse_args()

    df = load_data(args.ticker, args.start, args.source)
    if len(df) < 60:
        print(json.dumps({"error": f"数据不足（{len(df)} 条）"}))
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
        "ticker": args.ticker, "strategy": label, "source": args.source,
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
