#!/usr/bin/env python3
"""因子库：横截面价量因子计算 + 标准化排序 + IC/ICIR 检验。

因子（价量，日线，A股新浪源）：
  mom_20 / mom_60  动量（区间收益）
  vol_20           年化波动率（方向取负：波动越低越好）
  trend            close/MA20 − 1（趋势偏离）
  rsi_14           RSI（方向取负：超买降分）
  liq_ratio        近20日均量/近60日均量（相对活跃度）
  mdd_60           近60日最大回撤（方向取负）

用法:
  python factors.py snapshot --tickers 600519,000001,601318,600036 --window 120
  python factors.py ic --tickers ... --factor mom_20 --forward 5 --window 250
输出: JSON
"""
import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bars import fetch_a_share  # noqa: E402

FACTOR_SIGN = {"mom_20": 1, "mom_60": 1, "vol_20": -1, "trend": 1, "rsi_14": -1,
               "liq_ratio": 1, "mdd_60": 1,
               # 估值因子（同花顺源）：越低越便宜 → 方向取负
               "pe_ttm": -1, "pb": -1, "peg": -1, "ps": -1}
TRADING_DAYS = 252


def closes_volumes(ticker, window, period="1d"):
    bars, source = fetch_a_share(ticker, period, window)
    if len(bars) < 65:
        raise RuntimeError(f"{ticker} 日线不足（{len(bars)} 根）")
    return bars, source


def rsi(values, n=14):
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        delta = values[i] - values[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[-n:]) / n
    avg_loss = sum(losses[-n:]) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def factor_values(bars, index=None):
    """计算某一天（默认最后一天）的因子值。index 为 bars 中的位置。"""
    end = len(bars) if index is None else index + 1
    closes = [b["c"] for b in bars[:end]]
    volumes = [b["v"] for b in bars[:end]]
    if len(closes) < 65:
        return None
    latest = closes[-1]
    mom_20 = latest / closes[-21] - 1
    mom_60 = latest / closes[-61] - 1
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(len(closes) - 20, len(closes)) if closes[i - 1] > 0]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    vol_20 = math.sqrt(var) * math.sqrt(TRADING_DAYS)
    ma20 = sum(closes[-20:]) / 20
    trend = latest / ma20 - 1
    rsi_14 = rsi(closes)
    liq_ratio = (sum(volumes[-20:]) / 20) / max(sum(volumes[-60:]) / 60, 1e-9)
    peak, mdd = -math.inf, 0.0
    for price in closes[-60:]:
        peak = max(peak, price)
        if peak > 0:
            mdd = min(mdd, price / peak - 1)
    return {"mom_20": mom_20, "mom_60": mom_60, "vol_20": vol_20, "trend": trend,
            "rsi_14": rsi_14, "liq_ratio": liq_ratio, "mdd_60": mdd, "close": latest}


def valuation_values(ticker):
    """估值因子（同花顺源，取最近一行）。源不可用时返回空 dict，不阻塞其余因子。"""
    import akshare as ak
    df = ak.stock_value_em(symbol=str(ticker).split(".")[0])
    if df is None or df.empty:
        return {}
    row = df.tail(1).to_dict("records")[0]

    def pick(*names):
        for name in names:
            value = row.get(name)
            if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                return float(value)
        return None

    return {"pe_ttm": pick("PE(TTM)"), "pb": pick("市净率"),
            "peg": pick("PEG值"), "ps": pick("市销率")}


def zscores(rows, keys):
    """横截面 z-score（截断 ±3），用于合成打分。"""
    stats = {}
    for key in keys:
        values = [row["factors"][key] for row in rows if row["factors"].get(key) is not None]
        if len(values) < 2:
            stats[key] = (0.0, 1.0)
            continue
        mean = sum(values) / len(values)
        var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
        stats[key] = (mean, math.sqrt(var) or 1.0)
    for row in rows:
        row["z"] = {}
        for key in keys:
            value = row["factors"].get(key)
            if value is None:
                row["z"][key] = None
                continue
            mean, std = stats[key]
            row["z"][key] = max(-3.0, min(3.0, (value - mean) / std))
    return stats


def composite(rows, keys):
    for row in rows:
        parts = [row["z"][key] * FACTOR_SIGN[key] for key in keys if row["z"].get(key) is not None]
        row["score"] = round(sum(parts) / len(parts), 4) if parts else None
    ranked = sorted([r for r in rows if r["score"] is not None], key=lambda r: r["score"], reverse=True)
    for position, row in enumerate(ranked, start=1):
        row["rank"] = position
    return ranked


def snapshot(tickers, window):
    rows, sources, failures = [], set(), {}
    for ticker in tickers:
        try:
            bars, source = closes_volumes(ticker, window)
            values = factor_values(bars)
            if values is None:
                raise RuntimeError("样本不足")
            sources.add(source)
            try:
                values.update({k: v for k, v in valuation_values(ticker).items() if v is not None})
                sources.add("akshare/同花顺估值")
            except Exception:
                pass  # 估值缺失不影响价量因子
            rows.append({"ticker": ticker, "factors": values, "as_of": bars[-1]["t"]})
        except Exception as error:
            failures[ticker] = str(error)[:80]
    if len(rows) < 2:
        raise RuntimeError("有效标的不足 2 个，无法做横截面比较")
    keys = list(FACTOR_SIGN)
    zscores(rows, keys)
    ranked = composite(rows, keys)
    for row in rows:
        row["factors"] = {k: (round(v, 5) if isinstance(v, float) else v) for k, v in row["factors"].items()}
    return {"tickers": [r["ticker"] for r in ranked], "rows": ranked, "factors": keys,
            "sources": sorted(sources), "failures": failures, "window": window,
            "note": "价量 + 估值因子横截面 z-score 合成打分（估值源：同花顺；缺失时自动跳过估值维度）。"}


def ic_series(tickers, factor, forward, window):
    if factor not in FACTOR_SIGN:
        raise RuntimeError(f"未知因子 {factor}")
    series = {}
    for ticker in tickers:
        bars, _source = closes_volumes(ticker, window)
        series[ticker] = bars
    length = min(len(b) for b in series.values())
    step = max(forward, 5)
    points = []
    for offset in range(65, length - forward, step):
        xs, ys = [], []
        for ticker, bars in series.items():
            values = factor_values(bars, index=offset)
            if values is None or values.get(factor) is None:
                continue
            entry = bars[offset]["c"]
            exit_price = bars[offset + forward]["c"]
            if entry <= 0:
                continue
            xs.append(values[factor])
            ys.append(exit_price / entry - 1)
        if len(xs) < 3:
            continue
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
        vx = sum((xs[i] - mx) ** 2 for i in range(n))
        vy = sum((ys[i] - my) ** 2 for i in range(n))
        if vx <= 0 or vy <= 0:
            continue
        points.append({"t": series[tickers[0]][offset]["t"], "ic": round(cov / math.sqrt(vx * vy), 4)})
    if len(points) < 5:
        raise RuntimeError(f"有效 IC 样本仅 {len(points)} 个（标的 {len(tickers)} 个），不足 5 个")
    values = [p["ic"] for p in points]
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    std = math.sqrt(var)
    return {"factor": factor, "tickers": tickers, "forward_days": forward,
            "points": points, "count": len(points),
            "mean_ic": round(mean, 4), "ic_std": round(std, 4),
            "icir": round(mean / std, 3) if std > 0 else None,
            "positive_ratio": round(sum(1 for v in values if v > 0) / len(values), 3),
            "note": "横截面 IC（标的数少时噪声大，仅作方向性参考）；|IC|>0.03 且 ICIR>0.5 才具备实用性。"}


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("snapshot")
    p2 = sub.add_parser("ic")
    for parser, with_factor in ((p1, False), (p2, True)):
        parser.add_argument("--tickers", required=True)
        parser.add_argument("--window", type=int, default=250)
        if with_factor:
            parser.add_argument("--factor", default="mom_20")
            parser.add_argument("--forward", type=int, default=5)
    args = ap.parse_args()

    try:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()][:8]
        if len(tickers) < 2:
            raise RuntimeError("至少两个标的")
        if args.window < 80 or args.window > 1000:
            raise RuntimeError("window 需在 80..1000")
        if args.cmd == "snapshot":
            print(json.dumps(snapshot(tickers, args.window), ensure_ascii=False))
        else:
            if not 1 <= args.forward <= 60:
                raise RuntimeError("forward 需在 1..60")
            print(json.dumps(ic_series(tickers, args.factor, args.forward, args.window), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"error": str(error)[:300]}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
