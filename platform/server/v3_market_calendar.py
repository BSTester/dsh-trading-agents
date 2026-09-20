"""三市场交易时段 / 节假日：``GET /api/v3/markets/calendar?markets=SH,HK,US``。

口径（**契约字段，前端已按此实现**；本轮只**增字段**，既有字段语义一字不改）::

    {"ok":true,"as_of":"…","timezone":"Asia/Shanghai","holidays_loaded":true,
     "markets":{"SH":{...},"HK":{...},"US":{...}},
     "source":"platform/market_calendar",
     "calendar_source":"futu/info_trading_days","calendar_as_of":"…","calendar_note":"…",
     "calendar_complete":true}

    单市场：{"isTradingDay":bool,"session":"pre|open|lunch|post|closed",
             "open":"09:30","close":"15:00","lunch":["11:30","13:00"],
             "now":"…","nextOpen":"…","holiday":null,"label":"休市（周末）","timezone":"…",
             "calendar_source":"futu/info_trading_days","calendar_note":"…",
             "calendar_coverage":{"start":"…","end":"…"},"holidays_in_window":8}

真实时段（一律用 ``zoneinfo`` 按市场本地时区判定，夏令时自动生效）:

  * **SH/SZ**：09:30–11:30 / 13:00–15:00（``Asia/Shanghai``）
  * **HK**：09:30–12:00 / 13:00–16:00（``Asia/Hong_Kong``）
  * **US**：09:30–16:00（``America/New_York``，无午休——``lunch`` 为空数组，不硬凑一段）

节假日（2026-09-21 起改为**自动获取**，实现收敛在 ``server/v3_calendar_source.py``）:

  1. 进程内 TTL 缓存（``QUANT_CALENDAR_TTL_MS``，默认 12h）→
  2. ``<home>/market-calendar.json`` 落盘缓存 →
  3. 富途 ``info_trading_days``（经既有限流器 ``v3_ratelimit``）→
  4. AKShare ``tool_trade_date_hist_sina``（**仅 A 股**；港/美股无开源自历，如实标注）→
  5. ``<home>/market-holidays.json`` 人工兜底表（保留兼容）→ 6. 取不到 → ``None``。

  * **节假日 = 工作日 − 交易日**（``v3_calendar_source.holidays_for``）；**周末不计入
    holidays**（否则会和「休市（周末）」重复计算）。
  * ``holidays_loaded`` 在所有请求的市场都拿到真实日历（自动获取或人工兜底表）时为 ``true``；
    取不到时保持旧行为：**只按周末判断 + ``holidays_loaded:false`` + 原因**——绝不把「没读到
    节假日表」伪装成「表里没有节假日」。
  * ``holidays_loaded`` 与 ``source`` 的既有语义不变（``source`` 仍是
    ``platform/market_calendar``，即「这个响应由本模块组装」）；取数来源写在**新字段**
    ``calendar_source``（每市场另有一份 ``calendar_source``）里。

刷新：``POST /api/v3/markets/calendar/refresh``（**只读语义**：只取数 + 写
``<home>/market-calendar.json``，不涉交易）会强制跳过缓存重取一遍。低频自动刷新的挂法见
``docs/e2e-and-data-gaps.md``（示例：调度器作业 / cron 调该端点 / 直接调
``v3_calendar_source.refresh_cache(home, wb_call=...)``）；正常请求在 TTL 过期后也会**顺带**
刷新缓存，所以即使不挂调度器，缓存也会随访问自然更新（只是没人访问就不更新）。

时间可注入：查询串 ``?now=2026-09-20T10:00:00+08:00``（ISO 8601，带偏移）或
``register(..., deps={"now": ...})``，供测试用固定时钟。**只读**：本模块不碰任何写/交易端点。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from server import v3_calendar_source

__all__ = [
    "CALENDAR_FILE",
    "HOLIDAYS_ENV",
    "HOLIDAYS_FILE",
    "INTRADAY_PHASES",
    "MARKET_SESSIONS",
    "MARKET_TIMEZONES",
    "calendar_payload",
    "load_holidays",
    "market_state",
    "next_open",
    "register",
]

#: 市场 → IANA 时区（SH/SZ/BJ 同为中国内地时段）。
MARKET_TIMEZONES = {
    "SH": "Asia/Shanghai",
    "SZ": "Asia/Shanghai",
    "BJ": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
}

#: 市场 → 交易时段。``lunch`` 为 ``None`` 表示**无午休**（不硬凑一段，响应里是空数组）。
MARKET_SESSIONS = {
    "SH": {"open": "09:30", "close": "15:00", "lunch": ("11:30", "13:00")},
    "SZ": {"open": "09:30", "close": "15:00", "lunch": ("11:30", "13:00")},
    "BJ": {"open": "09:30", "close": "15:00", "lunch": ("11:30", "13:00")},
    "HK": {"open": "09:30", "close": "16:00", "lunch": ("12:00", "13:00")},
    "US": {"open": "09:30", "close": "16:00", "lunch": None},
}

DEFAULT_MARKETS = ("SH", "HK", "US")
#: 人工兜底表（兼容旧部署）与自动缓存文件名；两者都由 ``v3_calendar_source`` 定义（唯一事实源）。
HOLIDAYS_FILE = v3_calendar_source.LEGACY_HOLIDAYS_FILE
HOLIDAYS_ENV = v3_calendar_source.HOLIDAYS_ENV
CALENDAR_FILE = v3_calendar_source.CALENDAR_FILE
#: 找下一个交易日的最长回看天数（连续长假也不会超过这个窗口）
NEXT_OPEN_LOOKAHEAD_DAYS = 30

INTRADAY_PHASES = ("pre", "open", "lunch", "post")


def _error(code, message, **extra):
    payload = {"ok": False, "error": {"code": str(code), "message": str(message)[:300]}}
    payload["error"].update(extra)
    return payload


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_hhmm(text):
    hour, minute = str(text).split(":")
    return time(int(hour), int(minute))


def parse_iso(value):
    """ISO 字符串/datetime → **aware** datetime；无偏移的串按 UTC 解释（并在响应里说明）。

    返回 ``(datetime, assumed_utc)``；无法解析返回 ``(None, False)``。
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc), True
        return value, False
    text = str(value or "").strip()
    if not text:
        return None, False
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None, False
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc), True
    return parsed, False


def _parse_holiday_date(value):
    """节假日条目 → ``date``（只认 ``YYYY-MM-DD`` / ``YYYYMMDD``，其余忽略——不猜）。"""
    return v3_calendar_source._parse_day(str(value or "")[:10]) \
        or v3_calendar_source._parse_day(str(value or "")[:8])


def load_holidays(home=None, env=None):
    """人工兜底节假日表 → ``(holidays, loaded, note)``（实现已收敛到 ``v3_calendar_source``）。

    ``holidays`` 形如 ``{"SH": {date(...)}}``；``loaded=False`` 时调用方只按周末判断，
    并把原因（文件缺失/坏 JSON/环境变量未设置）如实写进响应。函数名与签名保留兼容。
    """
    return v3_calendar_source.load_holidays(home=home, env=env)


def _is_holiday(market, day, holidays):
    return day in (holidays.get(market) or set())


def is_trading_day(market, day, holidays):
    """交易日判定：周一~周五 **且** 不在节假日表里。"""
    if day.weekday() >= 5:
        return False
    return not _is_holiday(market, day, holidays)


def _session_of(market, local_time):
    """本地时刻 → ``pre|open|lunch|post``（不判是否交易日——由调用方在交易日里用）。"""
    spec = MARKET_SESSIONS[market]
    open_at = _parse_hhmm(spec["open"])
    close_at = _parse_hhmm(spec["close"])
    lunch = spec["lunch"]
    current = local_time.time() if isinstance(local_time, datetime) else local_time
    if current < open_at:
        return "pre"
    if current >= close_at:
        return "post"
    if lunch is not None:
        lunch_open = _parse_hhmm(lunch[0])
        lunch_close = _parse_hhmm(lunch[1])
        if lunch_open <= current < lunch_close:
            return "lunch"
    return "open"


def _session_label(session, is_trading):
    if not is_trading:
        return None
    return {
        "pre": "未开盘",
        "open": "交易中",
        "lunch": "午间休市",
        "post": "已收盘",
    }.get(session, "已收盘")


def next_open(market, now, holidays, tz):
    """下一个开市时刻（``YYYY-MM-DDTHH:MM:SS±HH:MM``，市场本地时区）。

    今天还没开盘 → 今天；已开盘/收盘 → 之后第一个交易日。找不到（超出回看窗口）→ ``None``
    （宁可给 null 也不给一个编造的日期）。
    """
    spec = MARKET_SESSIONS[market]
    open_at = _parse_hhmm(spec["open"])
    local_now = now.astimezone(tz)
    for offset in range(0, NEXT_OPEN_LOOKAHEAD_DAYS + 1):
        day = local_now.date() + timedelta(days=offset)
        if not is_trading_day(market, day, holidays):
            continue
        candidate = datetime.combine(day, open_at, tzinfo=tz)
        if offset == 0 and local_now >= candidate:
            continue
        return candidate.isoformat()
    return None


def market_state(market, now, holidays, *, tz=None, calendar=None):
    """单市场状态（契约字段齐全 + 本轮新增的 ``calendar_*`` 说明字段）。

    ``now`` 必须是 aware datetime；``calendar`` 是该市场的日历元信息（``source``/``note``/
    ``coverage``/``holidays_count``），缺省为 ``None``（响应里对应字段为 ``null``，不编造）。
    """
    market = str(market).upper()
    zone = tz or ZoneInfo(MARKET_TIMEZONES[market])
    local_now = now.astimezone(zone)
    today = local_now.date()
    spec = MARKET_SESSIONS[market]
    lunch = list(spec["lunch"]) if spec["lunch"] else []
    holiday = _is_holiday(market, today, holidays)
    trading = is_trading_day(market, today, holidays)
    if trading:
        session = _session_of(market, local_now)
        label = _session_label(session, True)
        holiday_value = None
    else:
        session = "closed"
        holiday_value = today.isoformat() if holiday else None
        label = f"休市（节假日 {holiday_value}）" if holiday else "休市（周末）"
    meta = calendar or {}
    return {
        "isTradingDay": trading,
        "session": session,
        "open": spec["open"],
        "close": spec["close"],
        "lunch": lunch,
        "now": local_now.isoformat(),
        "nextOpen": next_open(market, now, holidays, zone),
        "holiday": holiday_value,
        "label": label,
        "timezone": MARKET_TIMEZONES[market],
        # 新增字段（既有字段一字不改）：该市场这一天的判定用了哪份日历、覆盖到哪。
        "calendar_source": meta.get("source"),
        "calendar_note": meta.get("note"),
        "calendar_coverage": meta.get("coverage"),
        "holidays_in_window": meta.get("holidays_count"),
        "calendar_complete": meta.get("complete"),
    }


def parse_markets(raw):
    """``"SH,HK,US"`` → ``["SH","HK","US"]``；未知市场返回 ``(known, unknown)``。"""
    if isinstance(raw, (list, tuple)):
        items = [str(item) for item in raw]
    else:
        items = str(raw or "").split(",")
    known = []
    unknown = []
    for item in items:
        code = item.strip().upper()
        if not code or code in known:
            continue
        if code in MARKET_SESSIONS:
            known.append(code)
        else:
            unknown.append(code)
    return known, unknown


def _calendar_from_source(wanted, *, home, env, moment, wb_call, source_deps, calendar):
    """自动获取逐市场日历 → ``(holidays, loaded, note, source, as_of, complete, details)``。

    ``calendar`` 非空时直接用它（``{market: 日历结果}``，测试/调用方注入，零网络）；
    否则走 ``v3_calendar_source.resolve_calendar``（缓存 → 富途 → AKShare → 人工兜底）。
    取不到的市场**只按周末判断**，并把真实原因写进 note。
    """
    if calendar is not None:
        resolved = {
            market: {"market": market, "ok": market in calendar, "result": calendar.get(market),
                     "chain": [], "reason": "注入的日历（测试/调用方提供）"}
            for market in wanted
        }
    else:
        first, last = v3_calendar_source.calendar_window(moment.date())
        deps = dict(source_deps or {})
        resolved = v3_calendar_source.resolve_calendar(
            wb_call, home, wanted,
            start=first.isoformat(), end=last.isoformat(),
            ttl_ms=deps.get("ttl_ms"), clock=deps.get("clock"), akshare=deps.get("akshare"),
            env=env, live=deps.get("live"), skip_cache=bool(deps.get("skip_cache")),
            retry=deps.get("retry"),
        )
    holidays = {}
    details = {}
    notes = []
    failures = []
    sources = []
    loaded = True
    complete = True
    data_as_of = None
    for market in wanted:
        detail = resolved.get(market) or {}
        result = detail.get("result")
        if not isinstance(result, dict):
            holidays[market] = set()
            loaded = False
            complete = False
            reason = detail.get("reason") or "未取到交易日历"
            failures.append(f"{market}：{reason}")
            notes.append(f"{market}：未取到交易日历 → 只按周末判断")
            details[market] = {"source": None, "note": f"{reason} → 只按周末判断",
                               "coverage": None, "holidays_count": None, "complete": False}
            continue
        dates = set()
        for item in result.get("holidays") or []:
            parsed = _parse_holiday_date(item)
            if parsed is not None:
                dates.add(parsed)
        holidays[market] = dates
        coverage = result.get("coverage") or {"start": result.get("start"), "end": result.get("end")}
        origin = result.get("origin_source")
        label = result.get("source")
        if origin and origin != label:
            label = f"{label}（原来源 {origin}）"
        piece = (f"{market}：{label}，coverage {coverage.get('start')}~{coverage.get('end')}"
                 + ("" if result.get("complete", True) else "（仅覆盖部分窗口）"))
        if result.get("note"):
            piece += f"；{result['note']}"
        notes.append(piece)
        sources.append(result.get("source"))
        if not result.get("complete", True):
            complete = False
        if result.get("data_as_of") and (data_as_of is None or result["data_as_of"] > data_as_of):
            data_as_of = result["data_as_of"]
        details[market] = {
            "source": result.get("source"),
            "note": result.get("note"),
            "coverage": coverage,
            "holidays_count": len(dates),
            "complete": bool(result.get("complete")),
        }
    note = "；".join(notes) if notes else "未请求任何市场"
    note += "；节假日口径 = 工作日 − 交易日（周末不计入 holidays）"
    if failures:
        note += "；" + "；".join(failures)
    if not loaded:
        note += " → 未取到日历的市场只按周末判断"
    unique = {source for source in sources if source}
    calendar_source = unique.pop() if len(unique) == 1 else ("mixed" if unique else None)
    return holidays, loaded, note, calendar_source, data_as_of, complete, details


def calendar_payload(markets="SH,HK,US", *, now=None, home=None, env=None, holidays=None,
                     holidays_loaded=None, holidays_note=None, wb_call=None,
                     source_deps=None, calendar=None):
    """构造 ``/api/v3/markets/calendar`` 的完整响应（纯函数，便于离线单测）。

    * ``holidays`` 显式注入 → 完全按注入值判定（既有测试/调用方路径，行为不变）；
    * 否则**自动获取**：``wb_call``（生产是 ``app.v3_run``，已过富途限流器）+ ``source_deps``
      （``akshare``/``clock``/``ttl_ms``/``live``/``retry``/``skip_cache``，仅供测试或调用方注入）；
      ``wb_call`` 与 ``akshare`` 都没有时只读缓存与人工兜底表，**不打网络**。
    * ``calendar`` 可直接注入 ``{market: 日历结果}``（跳过取数，零网络）。
    """
    wanted, unknown = parse_markets(markets)
    if unknown:
        return _error("calendar/bad-market",
                      f"未知市场 {unknown}；支持 {sorted(MARKET_SESSIONS)}",
                      supported=sorted(MARKET_SESSIONS))
    if not wanted:
        return _error("calendar/bad-args", "markets 不能为空（如 SH,HK,US）",
                      supported=sorted(MARKET_SESSIONS))
    stamp = now if now is not None else datetime.now(timezone.utc)
    moment, assumed_utc = parse_iso(stamp)
    if moment is None:
        return _error("calendar/bad-now", f"now 需为 ISO 8601 时间串或 datetime，收到 {stamp!r}")
    calendar_source = None
    calendar_as_of = None
    calendar_complete = None
    details = {}
    if holidays is None:
        holidays, loaded, note, calendar_source, calendar_as_of, calendar_complete, details = \
            _calendar_from_source(wanted, home=home, env=env, moment=moment, wb_call=wb_call,
                                  source_deps=source_deps, calendar=calendar)
    else:
        loaded = True if holidays_loaded is None else bool(holidays_loaded)
        note = holidays_note or "注入的节假日表（测试/调用方提供）"
    primary_zone = MARKET_TIMEZONES[wanted[0]]
    payload = {
        "ok": True,
        "as_of": now_iso(),
        "now": moment.astimezone(ZoneInfo(primary_zone)).isoformat(),
        "timezone": primary_zone,
        "markets": {market: market_state(market, moment, holidays, calendar=details.get(market))
                    for market in wanted},
        "source": "platform/market_calendar",
        "holidays_loaded": bool(loaded),
        "holidays_note": note,
        # 新增字段（既有字段语义不变）：本次判定的日历来源与覆盖情况。
        "calendar_source": calendar_source,
        "calendar_as_of": calendar_as_of,
        "calendar_note": note,
        "calendar_complete": calendar_complete,
        "calendar_cache_file": CALENDAR_FILE,
    }
    if assumed_utc:
        payload["now_note"] = "注入的 now 无时区偏移，按 UTC 解释"
    return payload


def build_refresh(home, markets, horizon_days, *, wb_call, source_deps=None, env=None):
    """``POST /api/v3/markets/calendar/refresh`` 的实现（**只读语义**：取数 + 写缓存）。

    只接受已知市场；未知市场返回错误信封（不猜、不静默忽略）。
    """
    known, unknown = parse_markets(markets)
    if unknown:
        return _error("calendar/bad-market",
                      f"未知市场 {unknown}；支持 {sorted(MARKET_SESSIONS)}",
                      supported=sorted(MARKET_SESSIONS))
    if not known:
        return _error("calendar/bad-args", "markets 不能为空（如 SH,HK,US）",
                      supported=sorted(MARKET_SESSIONS))
    deps = dict(source_deps or {})
    try:
        horizon = max(1, int(horizon_days))
    except (TypeError, ValueError):
        return _error("calendar/bad-args", f"horizon_days 需为整数，收到 {horizon_days!r}")
    try:
        payload = v3_calendar_source.refresh_cache(
            home, tuple(known), horizon,
            wb_call=wb_call, akshare=deps.get("akshare"), env=env, clock=deps.get("clock"),
            ttl_ms=deps.get("ttl_ms"), today=deps.get("today"), retry=deps.get("retry"),
        )
    except Exception as error:  # noqa: BLE001 —— 统一信封，不把栈透给前端
        return _error("calendar/refresh-internal", f"{type(error).__name__}: {error}")
    return payload


def register(app, v3_run, home, deps=None):
    """挂 ``GET /api/v3/markets/calendar`` 与 ``POST /api/v3/markets/calendar/refresh``。

    ``deps={"now":…,"env":…,"holidays":…,"calendar":…,"source_deps":…,"calendar_live":…}``
    仅供测试/诊断注入：**传入 deps 时默认关闭实时取数**（``calendar_live=false``），避免单测
    打网络；生产路径 ``register(app, v3_run, home)``（``deps is None``）默认开启
    「缓存 → 富途 → AKShare → 人工兜底表」。刷新端点是**只读语义**（只取数 + 写缓存）。
    """
    deps = deps or {}
    source_deps = dict(deps.get("source_deps") or {})
    if "live" not in source_deps:
        if "calendar_live" in deps:
            source_deps["live"] = bool(deps["calendar_live"])
        elif deps:
            # 测试/诊断注入（deps 非空）→ 默认离线；要真打上游必须显式 calendar_live=true。
            source_deps["live"] = False

    @app.get("/api/v3/markets/calendar")
    async def v3_markets_calendar(markets: str = "SH,HK,US", now: str = ""):
        """三市场交易时段/节假日（自动获取日历；取不到时只按周末判断并说明原因）。

        ``now`` 可注入（ISO 8601，带偏移）以便前端/测试对齐时钟。
        """

        def build():
            stamp = now or deps.get("now")
            return calendar_payload(
                markets,
                now=stamp,
                home=home,
                env=deps.get("env"),
                holidays=deps.get("holidays"),
                wb_call=v3_run,
                source_deps=source_deps,
                calendar=deps.get("calendar"),
            )

        try:
            return await asyncio.to_thread(build)
        except Exception as error:  # noqa: BLE001 —— 统一信封，不抛 500
            return _error("calendar/internal", f"{type(error).__name__}: {error}")

    @app.post("/api/v3/markets/calendar/refresh")
    async def v3_markets_calendar_refresh(markets: str = "SH,HK,US", horizon_days: int = 400):
        """刷新交易日历缓存（**只读语义**：取数 + 写 ``<home>/market-calendar.json``，不涉交易）。

        跳过进程内/落盘缓存直取富途（A 股再降级 AKShare），逐市场返回真实来源与原因；
        取不到实时源的市场**不写占位条目**，缓存保持原样。供调度器/定时任务低频调用。
        """
        return await asyncio.to_thread(
            build_refresh, home, markets, horizon_days,
            wb_call=v3_run, source_deps=source_deps, env=deps.get("env"))

    app.state.v3_market_calendar = {
        "routes": ("/api/v3/markets/calendar", "/api/v3/markets/calendar/refresh"),
        "calendar_file": CALENDAR_FILE,
        "source_deps": source_deps,
    }
    return calendar_payload
