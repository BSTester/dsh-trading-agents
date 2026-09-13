#!/usr/bin/env python3
"""量化引擎的行情数据层（与工作台 bars.py 同规则）。

单一规则：**富途优先（全市场、分钟/日线），A股长历史回退新浪源**。
本文件与 plugins/workbench/python/bars.py 的路由逻辑保持一致；
两个插件包各自独立，因此此处为自包含副本 —— 修改任一处需同步另一处。

用法（内部）：
  from market_data import load_bars
  bars, source, stale = load_bars("00700.HK", "1d", 300)
"""
import json
import os
import re
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path

DSH = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
BASE_URL = "https://mcp.futunn.com/mcp"
PERIOD_TO_FUTU_KTYPE = {"1m": 1, "5m": 6, "15m": 7, "30m": 8, "60m": 9, "1d": 2}
PERIOD_TO_AKSHARE = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
FUTU_MAX_BARS = 370


def is_a_share(ticker):
    return bool(re.fullmatch(r"\d{6}", str(ticker).split(".")[0]))


def to_futu_symbol(ticker):
    """归一为富途的 MARKET.CODE 格式。"""
    text = str(ticker).strip().upper()
    if re.match(r"^(SH|SZ|BJ|HK|US)\.[A-Z0-9.]+$", text):
        return text
    match = re.match(r"^([A-Z0-9.]+)\.(SH|SZ|BJ|HK|US)$", text)
    if match:
        return f"{match.group(2)}.{match.group(1)}"
    if re.fullmatch(r"\d{6}", text):
        return f"{'SH' if text[0] in '69' else 'BJ' if text[0] in '48' else 'SZ'}.{text}"
    if re.fullmatch(r"\d{1,5}", text):
        return f"HK.{text.zfill(5)}"
    return f"US.{text}"


def _mcp_call(payload, session=None, headers=None, tries=4):
    """带退避重试的 MCP 调用（网络抖动常见）。"""
    last = None
    for attempt in range(tries):
        try:
            request = urllib.request.Request(BASE_URL, data=json.dumps(payload).encode(),
                                             headers=headers, method="POST")
            if session:
                request.add_header("mcp-session-id", session)
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.headers.get("mcp-session-id"), response.read().decode()
        except Exception as error:  # noqa: BLE001
            last = error
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"MCP 调用失败：{str(last)[:120]}")


def futu_headers():
    token_path = DSH / "futu-token"
    if not token_path.exists() or not token_path.read_text().strip():
        raise RuntimeError("缺少富途 token（先完成授权）")
    return {"Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token_path.read_text().strip()}"}


def fetch_futu(ticker, period, limit):
    """富途历史 K 线（全市场）。"""
    headers = futu_headers()
    session, _ = _mcp_call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                       "clientInfo": {"name": "quant-engine", "version": "1"}}},
                           headers=headers)
    _, body = _mcp_call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "quote_history_kline",
                                    "arguments": {"symbol": to_futu_symbol(ticker),
                                                  "ktype": PERIOD_TO_FUTU_KTYPE[period],
                                                  "num": min(limit, FUTU_MAX_BARS),
                                                  "end": date.today().isoformat()}}},
                        session=session, headers=headers)
    data = json.loads(body)
    if "error" in data:
        raise RuntimeError(f"富途错误：{data['error'].get('message', '')[:100]}")
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
        bars.append({"t": stamp, "o": float(row["open"]), "h": float(row["high"]),
                     "l": float(row["low"]), "c": float(row["close"]),
                     "v": float(row.get("volume") or 0)})
    return bars, "futu/quote_history_kline"


def sina_symbol(code):
    prefix = {"6": "sh", "9": "sh", "4": "bj", "8": "bj"}.get(code[0], "sz")
    return f"{prefix}{code}"


def fetch_sina(ticker, period, limit):
    """A股回退源（新浪）：日线可给长历史，分钟受接口限制。"""
    import akshare as ak
    code = str(ticker).split(".")[0]
    if period == "1d":
        df = ak.stock_zh_a_daily(symbol=sina_symbol(code), adjust="qfq")
        time_col = "date"
    else:
        df = ak.stock_zh_a_minute(symbol=sina_symbol(code), period=PERIOD_TO_AKSHARE[period], adjust="qfq")
        time_col = "day"
    df = df.tail(limit)
    bars = [{"t": str(getattr(row, time_col)), "o": float(row.open), "h": float(row.high),
             "l": float(row.low), "c": float(row.close), "v": float(getattr(row, "volume", 0) or 0)}
            for row in df.itertuples(index=False)]
    return bars, "akshare/sina"


def load_bars(ticker, period="1d", limit=300):
    """统一行情入口：富途优先，A股长历史走新浪。返回 (bars, source, stale)。"""
    if period not in PERIOD_TO_FUTU_KTYPE:
        raise ValueError(f"不支持的周期：{period}")
    limit = max(20, min(int(limit), 2000))
    order = []
    if period == "1d" and is_a_share(ticker) and limit > FUTU_MAX_BARS:
        order = [("sina", lambda: fetch_sina(ticker, period, limit)),
                 ("futu", lambda: fetch_futu(ticker, period, limit))]
    elif is_a_share(ticker):
        order = [("futu", lambda: fetch_futu(ticker, period, limit)),
                 ("sina", lambda: fetch_sina(ticker, period, limit))]
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
    raise RuntimeError(f"取数失败：{str(failure)[:160]}")
