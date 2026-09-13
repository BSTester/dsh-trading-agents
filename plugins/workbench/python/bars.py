#!/usr/bin/env python3
"""工作台行情序列 CLI —— 路由与缓存实现均在 trading_datasource.market（唯一实现）。

本文件只保留命令行入口与缓存回退，供 series.js 与工作台各分析模块调用。

用法:
  python bars.py --ticker 600519 --period 5m --limit 300
输出: JSON {ticker, period, source, as_of, count, bars:[{t,o,h,l,c,v}]}
"""
import argparse
import json
import sys

from trading_datasource.market import (
    PERIOD_TO_FUTU_KTYPE, FUTU_MAX_BARS, is_a_share, load_bars, normalize_limit,
    read_cache, to_futu_symbol, write_cache)

__all__ = ["PERIOD_TO_FUTU_KTYPE", "FUTU_MAX_BARS", "is_a_share", "load_bars",
           "normalize_limit", "read_cache", "to_futu_symbol", "write_cache"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--period", default="5m", choices=["1m", "5m", "15m", "30m", "60m", "1d"])
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    limit = normalize_limit(args.limit)
    cached = None if args.no_cache else read_cache(args.ticker, args.period)

    try:
        bars, source, stale = load_bars(args.ticker, args.period, limit, cached=cached)
    except RuntimeError as error:
        print(json.dumps({"error": str(error)[:200]}, ensure_ascii=False))
        return 1

    payload = {"ticker": args.ticker, "period": args.period, "source": source,
               "as_of": bars[-1]["t"] if bars else None, "count": len(bars),
               "bars": bars[-limit:]}
    if stale:
        payload["stale"] = True
    elif not args.no_cache and bars:
        write_cache(args.ticker, args.period, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
