"""交易日历：抓取 quote_trading_days 并落库（规格 §4.1 calendar 表）。

口径（2026-09-14 实测）：market 必须大写（SH/HK/US/...），start/end 必传。
返回值：sync_calendar 返回落库天数；上游信封异常导致 0 行时静默返回 0，
由调用方（daemon 质量检查）按「日历就绪」语义处理。
实测返回样例：{"trading_days": [{"time": "2026-09-11", "trade_date_type": "WHOLE", "trade_second": 14400}]}
"""
from trading_datasource.futu_mcp import call_tool
from . import store


def _default_fetcher(market, start, end):
    data = call_tool("quote_trading_days",
                     {"market": market.upper(), "start": start, "end": end},
                     client_name="trading-core/calendar")
    return data or {}


def sync_calendar(conn, market, start, end, fetcher=None):
    fetcher = fetcher or _default_fetcher
    market = market.upper()  # 口径：无论注入哪个 fetcher，market 一律大写后才发起调用
    payload = fetcher(market, start, end)
    days = [{"day": d["time"], "trade_date_type": d.get("trade_date_type"),
             "trade_second": d.get("trade_second")}
            for d in (payload.get("trading_days") or [])]
    return store.upsert_calendar(conn, market, days)
