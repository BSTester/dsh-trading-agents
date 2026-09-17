"""交易日历：抓取 quote_trading_days 并落库（规格 §4.1 calendar 表）。

口径（2026-09-14 实测，2026-09-17 官方文档复核）：market 必须大写（SH/HK/US/...），
start/end 必传且格式 ``YYYY-MM-DD``；REST 与 MCP 同一后端，响应同形
（``data.trading_days[]`` = ``{time, trade_date_type, trade_second}``，官方文档
``/api/quote/basic-data/trading-days`` 与 MCP 响应逐字段一致），故无需 adapter。

返回值：sync_calendar 返回落库天数；上游信封异常导致 0 行时静默返回 0，
由调用方（daemon 质量检查）按「日历就绪」语义处理。
实测返回样例：{"trading_days": [{"time": "2026-09-11", "trade_date_type": "WHOLE", "trade_second": 14400}]}

取数通道（WP13 A-1）：经 ``trading_datasource.channel.fetch`` 分派——``futu_channel=openapi``
且有凭据走 REST（``market.trading_days``，``GET /quote/trading-days``），否则 mcp
（无凭据时标注回退）。REST 失败原样上抛，不静默换通道。
"""
from trading_datasource import channel

from . import store


def _mcp_calendar(tool, params):
    """日历的 MCP 取数（``client_name`` 归因字面量保持既有值）。

    惰性导入：避免导入期拉起 MCP 会话；也让「模块级硬编码 MCP 调用」不复存在。
    """
    from trading_datasource.futu_mcp import call_tool  # noqa: PLC0415

    return call_tool(tool, params, client_name="trading-core/calendar")


def _default_fetcher(market, start, end, *, channel_name=None, home=None, client=None,
                     credential_path=None):
    params = {"market": market.upper(), "start": start, "end": end}
    data, _used = channel.fetch("quote_trading_days", params,
                                method="market.trading_days", mcp_call=_mcp_calendar,
                                channel=channel_name, home=home, client=client,
                                credential_path=credential_path)
    return data or {}


def sync_calendar(conn, market, start, end, fetcher=None, *, channel_name=None, home=None,
                  client=None, credential_path=None):
    """同步交易日历。

    ``fetcher`` 注入即完全绕过通道分派（既有离线测试口径，签名 ``(market, start, end)``
    不变）；未注入时经 ``channel.fetch`` 按 ``futu_channel`` 分派。
    """
    market = market.upper()  # 口径：无论注入哪个 fetcher，market 一律大写后才发起调用
    if fetcher is not None:
        payload = fetcher(market, start, end)
    else:
        payload = _default_fetcher(market, start, end, channel_name=channel_name, home=home,
                                   client=client, credential_path=credential_path)
    days = [{"day": d["time"], "trade_date_type": d.get("trade_date_type"),
             "trade_second": d.get("trade_second")}
            for d in (payload.get("trading_days") or [])]
    return store.upsert_calendar(conn, market, days)
