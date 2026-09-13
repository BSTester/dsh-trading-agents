#!/usr/bin/env python3
"""工作台行情序列数据源：分钟级 K 线 + 日线，带本地缓存。

数据源（按市场自动路由）：
  A股  → akshare 新浪源（stock_zh_a_minute 分钟 / stock_zh_a_daily 日线）
  其他 → 富途 MCP 快照降级（仅最新价，图表会标注数据不足）

用法:
  python bars.py --ticker 600519 --period 5m --limit 300
输出: JSON {ticker, period, source, as_of, count, bars:[{t,o,h,l,c,v}]}
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

DSH_HOME = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))
SERIES_DIR = DSH_HOME / "trading-series"

PERIOD_TO_AKSHARE = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}


def is_a_share(ticker):
    return bool(re.fullmatch(r"\d{6}", str(ticker).split(".")[0]))


def sina_symbol(code):
    prefix = {"6": "sh", "9": "sh", "4": "bj", "8": "bj"}.get(code[0], "sz")
    return f"{prefix}{code}"


def cache_path(ticker, period):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{ticker}-{period}")
    return SERIES_DIR / f"{safe}.json"


def read_cache(ticker, period):
    path = cache_path(ticker, period)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data.get("bars"), list):
                return data
        except (OSError, ValueError):
            return None
    return None


def write_cache(ticker, period, payload):
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path(ticker, period)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False))
    temp.replace(path)


def fetch_a_share(ticker, period, limit):
    import akshare as ak

    code = str(ticker).split(".")[0]
    df = None
    if period in PERIOD_TO_AKSHARE:
        df = ak.stock_zh_a_minute(symbol=sina_symbol(code), period=PERIOD_TO_AKSHARE[period], adjust="qfq")
        time_col = "day"
    else:  # 1d
        df = ak.stock_zh_a_daily(symbol=sina_symbol(code), adjust="qfq")
        time_col = "date"

    bars = []
    for row in df.tail(limit).itertuples(index=False):
        get = lambda name: getattr(row, name)
        bars.append({
            "t": str(get(time_col)),
            "o": round(float(get("open")), 4),
            "h": round(float(get("high")), 4),
            "l": round(float(get("low")), 4),
            "c": round(float(get("close")), 4),
            "v": float(get("volume") or 0),
        })
    return bars, "akshare/sina"


def fetch_snapshot_fallback(ticker, limit):
    """非 A股：用富途 MCP 快照给出单点，明确标注数据不足（供图表降级提示）。"""
    import urllib.request

    token_path = DSH_HOME / "futu-token"
    if not token_path.exists():
        raise RuntimeError("缺少富途 token，且该标的非 A 股，无可用分钟数据源")
    token = token_path.read_text().strip()
    base = "https://mcp.futunn.com/mcp"
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}"}

    def call(payload, session=None):
        req = urllib.request.Request(base, data=json.dumps(payload).encode(), headers=headers, method="POST")
        if session:
            req.add_header("mcp-session-id", session)
        with urllib.request.urlopen(req, timeout=25) as resp:
            session_id = resp.headers.get("mcp-session-id")
            body = resp.read().decode()
        return session_id, body

    session, _ = call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                  "clientInfo": {"name": "workbench", "version": "1"}}})
    step = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "quote_stock_quote", "arguments": {"code_list": [ticker]}}}
    _, body = call(step, session)
    data = json.loads(body)
    text = data["result"]["content"][0]["text"]
    quote = json.loads(text)["data"]["quote_list"][0]
    price = float(quote["last_price"])
    stamp = str(quote.get("data_date") or "")
    return [{"t": stamp, "o": price, "h": price, "l": price, "c": price, "v": 0}], "futu/snapshot(单点)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--period", default="5m", choices=["1m", "5m", "15m", "30m", "60m", "1d"])
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    limit = max(20, min(args.limit, 2000))
    cached = None if args.no_cache else read_cache(args.ticker, args.period)

    try:
        if is_a_share(args.ticker):
            bars, source = fetch_a_share(args.ticker, args.period, limit)
        else:
            bars, source = fetch_snapshot_fallback(args.ticker, limit)
    except Exception as error:
        if cached:
            print(json.dumps({"ticker": args.ticker, "period": args.period,
                              "source": cached.get("source") + "(缓存)", "as_of": cached.get("as_of"),
                              "count": len(cached["bars"]), "bars": cached["bars"][-limit:],
                              "stale": True, "error": str(error)[:200]}, ensure_ascii=False))
            return 0
        print(json.dumps({"error": f"取数失败：{str(error)[:200]}"}, ensure_ascii=False))
        return 1

    payload = {"ticker": args.ticker, "period": args.period, "source": source,
               "as_of": bars[-1]["t"] if bars else None, "count": len(bars), "bars": bars}
    if not args.no_cache and bars:
        write_cache(args.ticker, args.period, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
