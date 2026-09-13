#!/usr/bin/env python3
"""价格序列数据源 —— **全仓库唯一**的行情路由。

路由规则（单一事实来源）：
  分钟级（1/5/15/30/60m）→ 富途 MCP（A股/港股/美股实测均可用）
  日线           → 富途优先；A 股需要长历史（> FUTU_MAX_BARS）时直接用新浪源
  任一源失败     → 依次回退下一源；全部失败且给了 cached → 返回缓存并标注 stale

此前 workbench/bars.py 与 engine/market_data.py 各有一份逐字相同的路由实现
（91 行完全重复），只有日志文案不同 —— 修改一处不会传导到另一处。

用法：
  from trading_datasource.market import load_bars
  bars, source, stale = load_bars("00700.HK", "1d", 300)
"""
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

from .futu_mcp import FutuUnavailable, call_tool

DSH_HOME = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
SERIES_DIR = DSH_HOME / "trading-series"

PERIOD_TO_FUTU_KTYPE = {"1m": 1, "5m": 6, "15m": 7, "30m": 8, "60m": 9, "1d": 2}
PERIOD_TO_AKSHARE = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
FUTU_MAX_BARS = 370          # 富途 quote_history_kline 单次上限
MAX_BARS = 2000
MIN_BARS = 20


def is_a_share(ticker):
    """A 股判定。

    必须尊重**显式**市场标注：`000001.HK` 是港股长和，不是平安银行。
    此前只看点号前的数字，于是 6 位港股代码被当成 A 股：回测被路由到新浪，
    拿 `sz000001` 的行情算了一遍，静默回测了另一个标的（价格差了一个数量级）。
    裸 6 位数字仍按 A 股处理（历史约定，无法与港股区分）。
    """
    return bool(re.fullmatch(r"(?:SH|SZ|BJ)\.[A-Z0-9.]+", to_futu_symbol(ticker)))


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


def to_yahoo_symbol(ticker):
    """富途符号 → Yahoo 符号；无法映射返回 None。

    Yahoo 的港股是 **4 位**代码（0700.HK）：拿 5 位的 00700.HK 去查会返回空。
    这是唯一实现，fundamentals 也复用它，不再各存一份。
    """
    if not str(ticker or "").strip():
        return None
    symbol = to_futu_symbol(ticker)
    market, _, code = symbol.partition(".")
    if market == "HK":
        return f"{code.lstrip('0').zfill(4)}.HK"
    if market == "SH":
        return f"{code}.SS"
    if market in ("SZ", "BJ"):
        return f"{code}.SZ"
    return code


def sina_symbol(code):
    prefix = {"6": "sh", "9": "sh", "4": "bj", "8": "bj"}.get(code[0], "sz")
    return f"{prefix}{code}"


def normalize_limit(limit):
    return max(MIN_BARS, min(int(limit), MAX_BARS))


# ---- 本地缓存（全部源都失败时的兜底，标注 stale）----

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


# ---- 各数据源 ----

def fetch_futu(ticker, period, limit):
    """富途历史 K 线（全市场、分钟/日线）。"""
    data = call_tool("quote_history_kline",
                     {"symbol": to_futu_symbol(ticker),
                      "ktype": PERIOD_TO_FUTU_KTYPE[period],
                      "num": min(limit, FUTU_MAX_BARS),
                      "end": date.today().isoformat()},
                     client_name="trading-datasource/market")
    rows = (data or {}).get("kline_list") or []
    if not rows:
        raise FutuUnavailable("富途返回空 K 线")
    bars = []
    for row in rows:
        if period == "1d":
            stamp = str(row.get("date") or "")
            stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) == 8 else stamp
        else:
            ms = row.get("time_key")
            stamp = (datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
                     if isinstance(ms, (int, float)) else str(row.get("date") or ""))
        bars.append({"t": stamp, "o": round(float(row["open"]), 4),
                     "h": round(float(row["high"]), 4), "l": round(float(row["low"]), 4),
                     "c": round(float(row["close"]), 4),
                     "v": float(row.get("volume") or 0)})
    return bars, "futu/quote_history_kline"


def fetch_a_share(ticker, period, limit):
    """A 股备用源（AKShare 的新浪通道）：日线可给长历史，分钟受接口限制。"""
    import akshare as ak
    code = str(ticker).split(".")[0]
    if period in PERIOD_TO_AKSHARE:
        frame = ak.stock_zh_a_minute(symbol=sina_symbol(code),
                                     period=PERIOD_TO_AKSHARE[period], adjust="qfq")
        time_col = "day"
    else:  # 1d
        frame = ak.stock_zh_a_daily(symbol=sina_symbol(code), adjust="qfq")
        time_col = "date"
    bars = []
    for row in frame.tail(limit).itertuples(index=False):
        bars.append({"t": str(getattr(row, time_col)),
                     "o": round(float(getattr(row, "open")), 4),
                     "h": round(float(getattr(row, "high")), 4),
                     "l": round(float(getattr(row, "low")), 4),
                     "c": round(float(getattr(row, "close")), 4),
                     "v": float(getattr(row, "volume", 0) or 0)})
    return bars, "akshare/sina"


def fetch_yahoo(ticker, period, limit):
    """港美股长历史通道（Yahoo，复权价）。

    富途 quote_history_kline 单次上限 370 根，覆盖不了 2023 年起的日线；
    Yahoo 可给 900+ 根。A 股不用它（新浪已够），因此只在港美股长历史时启用。
    """
    if period != "1d":
        raise ValueError("Yahoo 通道仅用于日线长历史")
    import yfinance as yf
    symbol = to_yahoo_symbol(ticker)
    if symbol is None:
        raise ValueError("空标的无法映射到 Yahoo 代码")
    frame = yf.download(symbol, period="max", progress=False, auto_adjust=True)
    if frame is None or frame.empty:
        raise RuntimeError(f"Yahoo 无 {symbol} 的日线数据")
    if getattr(frame.columns, "nlevels", 1) > 1:
        frame.columns = frame.columns.get_level_values(0)
    bars = []
    for stamp, row in frame.tail(limit).iterrows():
        bars.append({"t": str(stamp)[:10],
                     "o": round(float(row["Open"]), 4), "h": round(float(row["High"]), 4),
                     "l": round(float(row["Low"]), 4), "c": round(float(row["Close"]), 4),
                     "v": float(row.get("Volume") or 0)})
    if not bars:
        raise RuntimeError("Yahoo 返回空 K 线")
    return bars, "yahoo/auto_adjusted"


def route(ticker, period, limit):
    """返回 [(标签, 取数函数), ...] —— 渠道优先级只在这里定义一次。"""
    # 富途单次上限 370 根：请求更长历史时，富途根本无法满足，
    # 因此长历史源排在前面（A 股新浪 / 港美股 Yahoo），富途作后备。
    if period == "1d" and limit > FUTU_MAX_BARS:
        if is_a_share(ticker):
            return [("sina", lambda: fetch_a_share(ticker, period, limit)),
                    ("futu", lambda: fetch_futu(ticker, period, limit))]
        return [("yahoo", lambda: fetch_yahoo(ticker, period, limit)),
                ("futu", lambda: fetch_futu(ticker, period, limit))]
    if is_a_share(ticker):
        return [("futu", lambda: fetch_futu(ticker, period, limit)),
                ("sina", lambda: fetch_a_share(ticker, period, limit))]
    # 港美股：Yahoo 只有日线，分钟级不得挂一个必然失败的通道
    if period == "1d":
        return [("futu", lambda: fetch_futu(ticker, period, limit)),
                ("yahoo", lambda: fetch_yahoo(ticker, period, limit))]
    return [("futu", lambda: fetch_futu(ticker, period, limit))]


def load_bars(ticker, period="1d", limit=300, cached=None):
    """统一行情入口：富途优先，A 股长历史走新浪。返回 (bars, source, stale)。"""
    if period not in PERIOD_TO_FUTU_KTYPE:
        raise ValueError(f"不支持的周期：{period}")
    limit = normalize_limit(limit)

    failure = None
    for _label, producer in route(ticker, period, limit):
        try:
            bars, source = producer()
            if bars:
                return bars, source, False
        except Exception as error:  # noqa: BLE001 - 依次回退下一源
            failure = failure or error
    if cached is not None and cached.get("bars"):
        return cached["bars"], str(cached.get("source")) + "(缓存)", True
    raise RuntimeError(f"取数失败：{str(failure)[:160]}")
