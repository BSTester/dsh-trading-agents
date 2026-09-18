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

覆盖维护（WP18，2026-09-18）：``ensure_coverage`` 是**日历自动同步作业**的单市场逻辑
（CLI ``calendar-sync`` 逐市场调用）。背景：日历表是「交易日白名单」，
``store.is_trading_day`` 在日期超出 ``max(day)`` 时返回 False 而**不报错**——日历用尽
会让市场链被静默跳过（页面表现为「市场天天休市」）。而此前**没有任何作业会同步日历**，
只能人工跑本模块的 CLI 子命令。
"""
import datetime as dt

from trading_datasource import channel

from . import store

#: 同步窗口（相对 today）：向前回看 30 天补漏（刚过去的交易日若有缺行可补齐），
#: 向后前推 400 天（约 270 个交易日）——覆盖一年多的交易日历，为「作业连续失败」留缓冲。
SYNC_LOOKBACK_DAYS = 30
SYNC_LOOKAHEAD_DAYS = 400
#: 自节流阈值：``max(day) >= today + horizon_days`` 就**跳过该市场、不发起任何网络调用**。
#: 理由：作业每天都会到期，不节流就是每天 3 次上游调用；节流后绝大多数日子是零网络的
#: no-op。默认 180 天**比 daemon 的 60 天告警阈值宽 3 倍**——正常的同步失败要持续 ~120 天
#: 才会开始触发「日历覆盖不足」，不会因为一次偶发失败就喊。
DEFAULT_HORIZON_DAYS = 180


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


def ensure_coverage(conn, market, today, horizon_days=DEFAULT_HORIZON_DAYS, fetcher=None,
                    **kwargs):
    """单市场「确保覆盖」：覆盖充足 → 跳过（**零网络调用**）；否则按窗口同步一次。

    ``today`` 接受 ``datetime.date`` 或 ``YYYY-MM-DD`` 字符串（CLI 传 ``date.today()``）。
    ``fetcher`` 注入即离线可测（签名同 ``sync_calendar`` 的 ``(market, start, end)``），
    其余关键字原样透传给 ``sync_calendar``（通道参数）。

    返回**逐市场结果信封**（CLI 汇总用，字段稳定）::

      {"market", "status": "skipped"|"synced", "days", "last_day", "horizon_days"(, "start","end")}

    * ``skipped``：``max(day) >= today + horizon_days``——覆盖充足，不去打扰上游；
    * ``synced``：按 ``[today - SYNC_LOOKBACK_DAYS, today + SYNC_LOOKAHEAD_DAYS]`` 幂等
      upsert（``INSERT OR REPLACE``，重复窗口不会产生重复行），``days`` = 实际落库行数、
      ``last_day`` = 同步后的新边界（**重读自库**，不是拿窗口上界冒充）。

    **不吞故障**：取数/通道失败原样上抛，由调用方（CLI/作业）逐市场隔离——本函数只负责
    一个市场，把「一个市场失败不影响其他市场」的编排留给调用方，两处不重复判定。
    上游信封异常导致 0 行时 ``days=0`` 且边界不变（``sync_calendar`` 既有口径：不抛、
    不假装成功）；覆盖仍不足会由下一轮 tick 的覆盖告警继续喊，不在这里硬判失败。
    """
    market = market.upper()
    if not isinstance(today, dt.date):
        today = dt.date.fromisoformat(str(today))
    last = store.calendar_last_day(conn, market)
    if last is not None and dt.date.fromisoformat(last) >= today + dt.timedelta(days=horizon_days):
        return {"market": market, "status": "skipped", "days": 0, "last_day": last,
                "horizon_days": horizon_days}
    start = today - dt.timedelta(days=SYNC_LOOKBACK_DAYS)
    end = today + dt.timedelta(days=SYNC_LOOKAHEAD_DAYS)
    days = sync_calendar(conn, market, start.isoformat(), end.isoformat(),
                         fetcher=fetcher, **kwargs)
    return {"market": market, "status": "synced", "days": days,
            "last_day": store.calendar_last_day(conn, market),
            "start": start.isoformat(), "end": end.isoformat(),
            "horizon_days": horizon_days}
