"""三市场交易时段 / 节假日：``GET /api/v3/markets/calendar?markets=SH,HK,US``。

口径（**契约字段，前端已按此实现**）::

    {"ok":true,"as_of":"…","timezone":"Asia/Shanghai","holidays_loaded":false,
     "markets":{"SH":{...},"HK":{...},"US":{...}},
     "source":"platform/market_calendar"}

    单市场：{"isTradingDay":bool,"session":"pre|open|lunch|post|closed",
             "open":"09:30","close":"15:00","lunch":["11:30","13:00"],
             "now":"…","nextOpen":"…","holiday":null,"label":"休市（周末）","timezone":"…"}

真实时段（一律用 ``zoneinfo`` 按市场本地时区判定，夏令时自动生效）:

  * **SH/SZ**：09:30–11:30 / 13:00–15:00（``Asia/Shanghai``）
  * **HK**：09:30–12:00 / 13:00–16:00（``Asia/Hong_Kong``）
  * **US**：09:30–16:00（``America/New_York``，无午休——``lunch`` 为空数组，不硬凑一段）

节假日：从可选 JSON 读——路径取环境变量 ``QUANT_MARKET_HOLIDAYS``（值为路径；若值本身就是
JSON 对象则直接解析）或 ``<home>/market-holidays.json``，形如
``{"SH":["2026-10-01",...],"HK":[...],"US":[...]}``。**文件不存在时只按周末判断**，响应里
``holidays_loaded:false`` 并给出说明——绝不把「没读到节假日表」伪装成「表里没有节假日」。

时间可注入：查询串 ``?now=2026-09-20T10:00:00+08:00``（ISO 8601，带偏移）或
``register(..., deps={"now": ...})``，供测试用固定时钟。**只读**：本模块不碰任何写端点。
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = [
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
HOLIDAYS_FILE = "market-holidays.json"
HOLIDAYS_ENV = "QUANT_MARKET_HOLIDAYS"
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
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text[:10], text[:8]):
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def load_holidays(home=None, env=None):
    """读节假日表 → ``(holidays, loaded, note)``。

    ``holidays`` 形如 ``{"SH": {date(...)}}``；``loaded=False`` 时调用方只按周末判断，
    并把原因（文件缺失/坏 JSON/环境变量未设置）如实写进响应。
    """
    env = os.environ if env is None else env
    raw_path = ""
    try:
        raw_path = str(env.get(HOLIDAYS_ENV) or "").strip()
    except Exception:  # noqa: BLE001 —— 假 env 形态不保证
        raw_path = ""
    source = None
    payload = None
    if raw_path.startswith("{"):
        # 环境变量直接给 JSON 内容（部署侧更省一个文件）
        source = f"{HOLIDAYS_ENV}(inline json)"
        try:
            payload = json.loads(raw_path)
        except ValueError as error:
            return {}, False, f"{HOLIDAYS_ENV} 不是合法 JSON：{error}"
    else:
        candidates = []
        if raw_path:
            candidates.append((raw_path, HOLIDAYS_ENV))
        if home:
            candidates.append((os.path.join(str(home), HOLIDAYS_FILE), f"<home>/{HOLIDAYS_FILE}"))
        if not candidates:
            return {}, False, f"{HOLIDAYS_ENV} 未设置且未提供 home 目录 → 只按周末判断"
        for path, label in candidates:
            if not os.path.exists(path):
                continue
            source = path
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError) as error:
                return {}, False, f"{label} 读取失败（{error}）→ 只按周末判断"
            break
        if payload is None:
            looked = "、".join(label for _path, label in candidates)
            return {}, False, f"未找到节假日文件（已找 {looked}）→ 只按周末判断"
    if not isinstance(payload, dict):
        return {}, False, f"{source} 顶层不是对象（{{market: [dates]}}）→ 只按周末判断"
    holidays = {}
    skipped = []
    for market, entries in payload.items():
        if not isinstance(entries, list):
            skipped.append(str(market))
            continue
        dates = set()
        for item in entries:
            parsed = _parse_holiday_date(item)
            if parsed is None:
                skipped.append(f"{market}:{item!r}")
                continue
            dates.add(parsed)
        holidays[str(market).upper()] = dates
    note = f"已加载 {source}"
    if skipped:
        note += f"；忽略 {len(skipped)} 条非法条目（{skipped[0]}…）" if len(skipped) > 1 else f"；忽略非法条目 {skipped[0]}"
    return holidays, True, note


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


def market_state(market, now, holidays, *, tz=None):
    """单市场状态（契约字段齐全）。``now`` 必须是 aware datetime。"""
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


def calendar_payload(markets="SH,HK,US", *, now=None, home=None, env=None, holidays=None,
                     holidays_loaded=None, holidays_note=None):
    """构造 ``/api/v3/markets/calendar`` 的完整响应（纯函数，便于离线单测）。"""
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
    if holidays is None:
        holidays, loaded, note = load_holidays(home=home, env=env)
    else:
        loaded = True if holidays_loaded is None else bool(holidays_loaded)
        note = holidays_note or "注入的节假日表（测试/调用方提供）"
    primary_zone = MARKET_TIMEZONES[wanted[0]]
    payload = {
        "ok": True,
        "as_of": now_iso(),
        "now": moment.astimezone(ZoneInfo(primary_zone)).isoformat(),
        "timezone": primary_zone,
        "markets": {market: market_state(market, moment, holidays) for market in wanted},
        "source": "platform/market_calendar",
        "holidays_loaded": bool(loaded),
        "holidays_note": note,
    }
    if assumed_utc:
        payload["now_note"] = "注入的 now 无时区偏移，按 UTC 解释"
    return payload


def register(app, v3_run, home, deps=None):
    """挂 ``GET /api/v3/markets/calendar``。``deps={"now":…,"env":…,"holidays":…}`` 仅供测试。"""
    deps = deps or {}

    @app.get("/api/v3/markets/calendar")
    async def v3_markets_calendar(markets: str = "SH,HK,US", now: str = ""):
        """三市场交易时段/节假日。``now`` 可注入（ISO 8601，带偏移）以便前端/测试对齐时钟。"""

        def build():
            stamp = now or deps.get("now")
            return calendar_payload(
                markets,
                now=stamp,
                home=home,
                env=deps.get("env"),
                holidays=deps.get("holidays"),
            )

        try:
            return await asyncio.to_thread(build)
        except Exception as error:  # noqa: BLE001 —— 统一信封，不抛 500
            return _error("calendar/internal", f"{type(error).__name__}: {error}")

    app.state.v3_market_calendar = {"routes": ("/api/v3/markets/calendar",)}
    return calendar_payload
