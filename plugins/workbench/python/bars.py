#!/usr/bin/env python3
"""工作台行情序列数据源：分钟级 K 线 + 日线，带本地缓存。

数据源（自动路由）：
  全部市场分钟级（1/5/15/30/60m）→ 富途 MCP quote_history_kline（实测 A股/港股/美股均可用）
  日线 → 富途优先；A股需要长历史（>370 根）时走 akshare 新浪源
  任一源失败 → 回退另一源；仍失败 → 使用本地缓存并标注 stale

用法:
  python bars.py --ticker 600519 --period 5m --limit 300
输出: JSON {ticker, period, source, as_of, count, bars:[{t,o,h,l,c,v}]}
"""
import argparse
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

DSH_HOME = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))
SERIES_DIR = DSH_HOME / "trading-series"
BASE_URL = "https://mcp.futunn.com/mcp"

PERIOD_TO_AKSHARE = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
PERIOD_TO_FUTU_KTYPE = {"1m": 1, "5m": 6, "15m": 7, "30m": 8, "60m": 9, "1d": 2}
FUTU_MAX_BARS = 370


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


def to_futu_symbol(ticker):
    """把各种写法归一为富途的 MARKET.CODE 格式。"""
    text = str(ticker).strip().upper()
    if re.match(r"^(SH|SZ|BJ|HK|US)\.[A-Z0-9.]+$", text):
        return text
    match = re.match(r"^([A-Z0-9.]+)\.(SH|SZ|BJ|HK|US)$", text)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    if re.fullmatch(r"\d{6}", text):
        return f"{'SH' if text[0] in '69' else 'BJ' if text[0] in '48' else 'SZ'}.{text}"
    if re.fullmatch(r"\d{1,5}", text):  # 纯数字 1~5 位按港股代码处理（如 700 / 0700 / 00700）
        return f"HK.{text.zfill(5)}"
    return f"US.{text}"


def _mcp_call(payload, session=None, headers=None, tries=4):
    """带退避重试的 MCP JSON-RPC 调用（网络抖动常见）。"""
    import time
    import urllib.request
    last = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(BASE_URL, data=json.dumps(payload).encode(),
                                             headers=headers, method="POST")
            if session:
                request.add_header("mcp-session-id", session)
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.headers.get("mcp-session-id"), response.read().decode()
        except Exception as error:  # noqa: BLE001 - 网络层异常统一重试
            last = error
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"MCP 调用失败：{str(last)[:120]}")


def futu_headers():
    token_path = DSH_HOME / "futu-token"
    if not token_path.exists():
        raise RuntimeError("缺少富途 token（先完成授权）")
    return {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token_path.read_text().strip()}"}


def fetch_futu(ticker, period, limit):
    """富途 MCP 历史 K 线（多市场、分钟/日线）。"""
    headers = futu_headers()
    session, _ = _mcp_call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "workbench", "version": "1"}}},
                           headers=headers)
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "quote_history_kline",
                          "arguments": {"symbol": to_futu_symbol(ticker),
                                        "ktype": PERIOD_TO_FUTU_KTYPE[period],
                                        "num": min(limit, FUTU_MAX_BARS),
                                        "end": date.today().isoformat()}}}
    _, body = _mcp_call(payload, session=session, headers=headers)
    data = json.loads(body)
    if "error" in data:
        raise RuntimeError(f"富途返回错误：{data['error'].get('message', '')[:100]}")
    inner = json.loads(data["result"]["content"][0]["text"])
    if inner.get("ret_code") != 0:
        raise RuntimeError(f"富途 ret={inner.get('ret_code')} {inner.get('ret_msg')}")
    rows = (inner.get("data") or {}).get("kline_list") or []
    if not rows:
        raise RuntimeError("富途返回空 K 线")
    bars = []
    for row in rows:
        if period == "1d":
            stamp = str(row.get("date") or "")
            stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) == 8 else stamp
        else:
            ms = row.get("time_key")
            stamp = (datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
                     if isinstance(ms, (int, float)) else str(row.get("date") or ""))
        bars.append({"t": stamp, "o": round(float(row["open"]), 4), "h": round(float(row["high"]), 4),
                     "l": round(float(row["low"]), 4), "c": round(float(row["close"]), 4),
                     "v": float(row.get("volume") or 0)})
    return bars, "futu/quote_history_kline"



def load_bars(ticker, period, limit, cached=None):
    """按市场与周期自动路由的 K 线加载（供各分析模块复用）。

    分钟级：富途（全市场）；日线：富途优先，A股长历史走新浪；失败回退本地缓存。
    返回 (bars, source, stale)。
    """
    order = []
    if period == "1d" and is_a_share(ticker) and limit > FUTU_MAX_BARS:
        order = [("sina", lambda: fetch_a_share(ticker, period, limit)),
                 ("futu", lambda: fetch_futu(ticker, period, limit))]
    elif is_a_share(ticker):
        order = [("futu", lambda: fetch_futu(ticker, period, limit)),
                 ("sina", lambda: fetch_a_share(ticker, period, limit))]
    else:
        order = [("futu", lambda: fetch_futu(ticker, period, limit))]

    failure = None
    for _label, producer in order:
        try:
            bars, source = producer()
            if bars:
                return bars, source, False
        except Exception as error:  # noqa: BLE001
            failure = failure or error
    if cached is not None:
        return cached.get("bars", []), str(cached.get("source")) + "(缓存)", True
    raise RuntimeError(f"取数失败：{str(failure)[:160]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--period", default="5m", choices=["1m", "5m", "15m", "30m", "60m", "1d"])
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    limit = max(20, min(args.limit, 2000))
    cached = None if args.no_cache else read_cache(args.ticker, args.period)

    # 数据源路由：分钟级一律优先富途（覆盖港美股）；日线富途优先，
    # 但 A 股需要长历史（>富途单次上限）时直接用新浪源。
    order = []
    if args.period == "1d" and is_a_share(args.ticker) and limit > FUTU_MAX_BARS:
        order = [("sina", lambda: fetch_a_share(args.ticker, args.period, limit)),
                 ("futu", lambda: fetch_futu(args.ticker, args.period, limit))]
    elif is_a_share(args.ticker):
        order = [("futu", lambda: fetch_futu(args.ticker, args.period, limit)),
                 ("sina", lambda: fetch_a_share(args.ticker, args.period, limit))]
    else:
        order = [("futu", lambda: fetch_futu(args.ticker, args.period, limit))]

    bars, source, failure = None, None, None
    for _label, producer in order:
        try:
            bars, source = producer()
            break
        except Exception as error:  # 依次回退
            failure = failure or error
    if bars is None:
        error = failure or RuntimeError("无可用数据源")
        if cached:  # 全部数据源失败时回退到本地缓存，并标注 stale
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
