"""交易日历 / 节假日**自动获取 + 缓存**（全站唯一实现）。

背景（2026-09-21 实测）
-----------------------
``v3_market_calendar`` 原来只读 ``<home>/market-holidays.json`` 这一份人工表，文件不存在时
「只按周末判断」——国庆/中秋/感恩节这类**落在工作日**的休市会被当成交易日。本轮把交易日历
接成自动获取：**工作日 − 交易日 = 节假日**，周末**不进**节假日集合（不重复计算）。

来源优先级（每一级都记进 ``chain``，含真实耗时与上游错误原文）
--------------------------------------------------------------

====================  =========================================  ==========================
顺序                   来源                                       覆盖市场
====================  =========================================  ==========================
0                    进程内 TTL 缓存（``QUANT_CALENDAR_TTL_MS``，默认 12h）  全部
1                    ``<home>/market-calendar.json`` 落盘缓存     全部（TTL 内且覆盖窗口够）
2                    富途 ``info_trading_days``（经既有限流器）     SH/SZ/BJ/HK/US（实测全部可用）
3                    AKShare ``tool_trade_date_hist_sina``        **仅 A 股**；港/美股如实标注无开源自历
4                    ``<home>/market-holidays.json`` 人工兜底表    全部（保留兼容旧行为）
====================  =========================================  ==========================

* 命中即返回（``run_chain`` 语义）；全失败 → ``resolve_trading_days`` 返回 ``None``，由调用方
  退化成「只按周末判断」并把每一级的真实原因写进响应——**不把「没取到」伪装成「表里没有」**。
* ``source`` 取值：``futu/info_trading_days`` / ``akshare/tool_trade_date_hist_sina`` /
  ``cache``（进程内或落盘缓存命中，原来源见 ``origin_source``）/ ``file``（人工兜底表）。
  ``chain`` 里的 ``source`` 更细（``cache/memory`` / ``cache/disk`` / ``file/market-holidays.json``）。
* **缓存只写「实时源」的结果**（富途/AKShare）：人工兜底表不进缓存，避免以后一直拿旧表，
  也避免上游恢复后仍被判为「没有日历」。
* 取数**只读**：本模块只调只读工具/公开数据接口，写盘只写 ``<home>`` 下的缓存 JSON。

真机结论（2026-09-21，写进 ``docs/e2e-and-data-gaps.md``）
---------------------------------------------------------
* 富途 ``info_trading_days`` 的市场码：``SH``/``SZ``/``BJ``/``HK``/``US`` **全部可用**
  （``plugins/datasource/.../market.py`` 的 ``TRADING_MARKETS`` 与之一致；
  实测 SH/HK/US/SZ/BJ 各 600~740ms，US 缺 2026-09-07 = 劳动节）。
* AKShare 只有 ``tool_trade_date_hist_sina``（A 股），实测 8797 行、覆盖 1990-12-19~2026-12-31，
  ``trade_date`` 是 ``datetime.date``；**没有**港股/美股日历接口（``dir(akshare)`` 里
  ``trade_date`` 系只有这一个）→ 港美股如实记「无开源自历」。
"""
from __future__ import annotations

import importlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import ModuleType

__all__ = [
    "AKSHARE_CALENDAR_FUNC",
    "AKSHARE_MARKETS",
    "CALENDAR_FILE",
    "CALENDAR_LOOKAHEAD_DAYS",
    "CALENDAR_LOOKBACK_DAYS",
    "CALENDAR_VERSION",
    "DEFAULT_TTL_MS",
    "FUTU_CALENDAR_TOOL",
    "FUTU_MARKET_CODES",
    "HOLIDAYS_ENV",
    "LEGACY_HOLIDAYS_FILE",
    "TTL_ENV",
    "calendar_window",
    "clear_memory_cache",
    "holidays_for",
    "load_calendar_file",
    "load_holidays",
    "now_iso",
    "read_memory_cache",
    "refresh_cache",
    "resolve_calendar",
    "resolve_market",
    "resolve_trading_days",
    "write_calendar_file",
]

#: 交易日历缓存文件名（本模块的唯一落盘产物；只读语义的缓存，不涉交易）。
CALENDAR_FILE = "market-calendar.json"
#: 人工兜底表（保留兼容：老部署只维护这一份；自动获取失败时才用得到）。
LEGACY_HOLIDAYS_FILE = "market-holidays.json"
HOLIDAYS_ENV = "QUANT_MARKET_HOLIDAYS"
#: 进程内缓存的 TTL（毫秒）环境变量。
TTL_ENV = "QUANT_CALENDAR_TTL_MS"
DEFAULT_TTL_MS = 12 * 60 * 60 * 1000
CALENDAR_VERSION = 1

#: 取窗口的回看/前瞻天数：回看覆盖「上一个长假」便于前端展示，前瞻覆盖 ``nextOpen``（30 天）。
CALENDAR_LOOKBACK_DAYS = 45
CALENDAR_LOOKAHEAD_DAYS = 400

#: 富途 ``info_trading_days`` 工具名与市场码（实测 SH/SZ/BJ/HK/US 全部可用）。
FUTU_CALENDAR_TOOL = "info_trading_days"
FUTU_MARKET_CODES = {"SH": "SH", "SZ": "SZ", "BJ": "BJ", "HK": "HK", "US": "US"}

#: AKShare 交易日历接口与它覆盖的市场（``tool_trade_date_hist_sina`` 只有 A 股）。
AKSHARE_CALENDAR_FUNC = "tool_trade_date_hist_sina"
AKSHARE_MARKETS = frozenset({"SH", "SZ", "BJ"})

#: 进程内 TTL 缓存：``market -> {"entry": {...}, "expires_at": monotonic 秒}``。
_MEMORY = {}
_MEMORY_LOCK = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _error_text(error):
    message = str(error).strip() or repr(error)
    return f"{type(error).__name__}: {message}"


def _failure(code, message):
    return {"ok": False, "error": {"code": str(code), "message": str(message)[:400]}}


# ── 日期工具 ────────────────────────────────────────────────────────────────────


def _parse_day(value):
    """``2026-10-01`` / ``20261001`` / ``date`` / ``datetime`` → ``date``；识别不出返回 ``None``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    digits = text.replace("-", "").replace("/", "")
    if len(digits) >= 8 and digits[:8].isdigit():
        try:
            return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def _day_set(values):
    out = set()
    for item in values or ():
        day = _parse_day(item)
        if day is not None:
            out.add(day)
    return out


def calendar_window(today=None, *, lookback_days=None, lookahead_days=None):
    """取日历窗口 ``(start, end)``：默认「今天 − 45 天」~「今天 + 400 天」。

    ``refresh_cache`` 与 ``v3_market_calendar`` 共用同一个窗口函数，缓存覆盖判定才一致
    （不一致会让每次请求都判「缓存窗口不够」而反复打上游）。
    """
    base = _parse_day(today) or date.today()
    back = CALENDAR_LOOKBACK_DAYS if lookback_days is None else int(lookback_days)
    ahead = CALENDAR_LOOKAHEAD_DAYS if lookahead_days is None else int(lookahead_days)
    return base - timedelta(days=max(0, back)), base + timedelta(days=max(0, ahead))


def holidays_for(market, start, end, trading_days):
    """``[start, end]`` 内的节假日 = **工作日 − 交易日**（周末不重复计入）。

    ``market`` 是契约参数（每个市场一份表）；计算只用 ``start``/``end``/``trading_days``。
    识别不出日期或 ``start > end`` → ``[]``（宁可为空也不编造）。
    """
    first, last = _parse_day(start), _parse_day(end)
    if first is None or last is None or first > last:
        return []
    traded = _day_set(trading_days)
    out = []
    cursor = first
    while cursor <= last:
        if cursor.weekday() < 5 and cursor not in traded:
            out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def _ttl_ms(ttl_ms, env):
    """TTL（毫秒）：显式参数 > ``QUANT_CALENDAR_TTL_MS`` > 默认；非法值回落默认。"""
    if ttl_ms is not None:
        try:
            return max(0, int(float(ttl_ms)))
        except (TypeError, ValueError):
            return DEFAULT_TTL_MS
    raw = None
    try:
        raw = env.get(TTL_ENV)
    except Exception:  # noqa: BLE001 —— 假 env 形态不保证
        raw = None
    if raw in (None, ""):
        return DEFAULT_TTL_MS
    try:
        return max(0, int(float(str(raw).strip())))
    except (TypeError, ValueError):
        return DEFAULT_TTL_MS


# ── 人工兜底表（原 v3_market_calendar.load_holidays，逐字搬来，保持兼容）──────────


def _parse_holiday_date(value):
    """节假日条目 → ``date``（只认 ``YYYY-MM-DD`` / ``YYYYMMDD``，其余忽略——不猜）。"""
    return _parse_day(str(value or "")[:10]) or _parse_day(str(value or "")[:8])


def load_holidays(home=None, env=None):
    """读人工兜底节假日表 → ``(holidays, loaded, note)``。

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
            candidates.append((os.path.join(str(home), LEGACY_HOLIDAYS_FILE),
                               f"<home>/{LEGACY_HOLIDAYS_FILE}"))
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


# ── 缓存（进程内 + 落盘）────────────────────────────────────────────────────────


def clear_memory_cache():
    """清空进程内缓存（测试用；生产无需调用）。"""
    with _MEMORY_LOCK:
        _MEMORY.clear()


def read_memory_cache(market, start, end, *, clock=None, ttl_ms=None, env=None):
    """进程内缓存 → ``(entry, error)``；未命中/过期/窗口不够时 ``entry=None`` 并给原因。"""
    clock = time.monotonic if clock is None else clock
    ttl = _ttl_ms(ttl_ms, os.environ if env is None else env)
    with _MEMORY_LOCK:
        stored = _MEMORY.get(str(market).upper())
    if not stored:
        return None, "进程内缓存无该市场"
    if clock() >= stored.get("expires_at", 0.0):
        return None, f"进程内缓存已过期（TTL {ttl}ms）"
    entry = stored.get("entry") or {}
    if not _covers(entry, start, end):
        return None, (f"进程内缓存窗口 {entry.get('start')}~{entry.get('end')} "
                      f"不覆盖请求 {start}~{end}")
    return entry, None


def _remember(entry, *, clock, ttl_ms):
    with _MEMORY_LOCK:
        _MEMORY[str(entry.get("market") or "").upper()] = {
            "entry": dict(entry),
            "expires_at": clock() + ttl_ms / 1000.0,
        }


def _covers(entry, start, end):
    first, last = _parse_day(start), _parse_day(end)
    have_first, have_last = _parse_day(entry.get("start")), _parse_day(entry.get("end"))
    if None in (first, last, have_first, have_last):
        return False
    return have_first <= first and have_last >= last


def read_calendar_file(home):
    """落盘缓存 → ``(payload, error)``；文件缺失/坏 JSON/结构不符都给原因（不抛）。"""
    if not home:
        return None, "未提供 home 目录"
    path = os.path.join(str(home), CALENDAR_FILE)
    if not os.path.exists(path):
        return None, f"未找到落盘缓存 <home>/{CALENDAR_FILE}"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as error:
        return None, f"<home>/{CALENDAR_FILE} 读取失败（{_error_text(error)}）"
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), dict):
        return None, f"<home>/{CALENDAR_FILE} 结构不符（需 {{version,updated_at,markets}}）"
    return payload, None


def load_calendar_file(home, market, start, end, *, ttl_ms=None, env=None):
    """落盘缓存里取某市场 → ``(entry, error)``；过期/窗口不够/无该市场都算未命中。

    ``updated_at`` 是**墙钟** ISO（跨进程重启后仍要判新鲜度），因此新鲜度按墙钟算，
    不跟着注入的单调时钟走；TTL 用 ``ttl_ms``。
    """
    ttl = _ttl_ms(ttl_ms, os.environ if env is None else env)
    payload, error = read_calendar_file(home)
    if error is not None:
        return None, error
    market = str(market).upper()
    entry = payload["markets"].get(market)
    if not isinstance(entry, dict):
        return None, f"落盘缓存里没有 {market}"
    updated = _parse_moment(payload.get("updated_at"))
    if updated is None:
        return None, f"落盘缓存 updated_at 非法（{payload.get('updated_at')!r}）"
    age_ms = int((datetime.now(timezone.utc) - updated).total_seconds() * 1000)
    if age_ms > ttl:
        return None, f"落盘缓存已过期（updated_at {payload.get('updated_at')}，TTL {ttl}ms）"
    if not _covers(entry, start, end):
        return None, (f"落盘缓存窗口 {entry.get('start')}~{entry.get('end')} "
                      f"不覆盖请求 {start}~{end}")
    entry = dict(entry)
    entry["cache_age_ms"] = age_ms
    return entry, None


def _parse_moment(text):
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def write_calendar_file(home, entries, *, path=None):
    """把 ``{market: entry}`` **合并**写进 ``<home>/market-calendar.json``；返回 ``(path, error)``。

    合并（而不是覆盖）是为了不丢掉本次没刷新的市场（例如只刷 SH 时保留 HK/US 的旧条目）。
    写盘失败**不影响取数**：错误原样返回给调用方，由它记进响应。
    """
    target = path or (os.path.join(str(home), CALENDAR_FILE) if home else "")
    if not target:
        return "", "未提供 home 目录，跳过落盘"
    markets = {}
    if not path:
        existing, _error = read_calendar_file(home)
        if isinstance(existing, dict):
            markets.update({str(key).upper(): value for key, value in existing["markets"].items()
                            if isinstance(value, dict)})
    for market, entry in (entries or {}).items():
        markets[str(market).upper()] = dict(entry)
    payload = {
        "version": CALENDAR_VERSION,
        "updated_at": now_iso(),
        "markets": markets,
        "note": ("交易日历缓存（工作日 − 交易日 = 节假日；周末不计入）。"
                 "来源与取数时间逐市场见 source/data_as_of；本文件由 "
                 "v3_calendar_source.write_calendar_file 维护，只读语义。"),
    }
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
    except OSError as error:
        return target, f"写入 {target} 失败（{_error_text(error)}）"
    return target, None


# ── 取数（富途 / AKShare）──────────────────────────────────────────────────────


def fetch_futu_trading_days(wb_call, market, start, end):
    """富途 ``info_trading_days`` → ``{"ok":true,"trading_days":[...],"coverage":{...}}``。

    ``wb_call`` 是工具面入口（生产是 ``app.v3_run``，已过 ``v3_ratelimit`` 全局限流器）。
    上游失败/形状不符 → 失败信封（含上游 error 原文），**不吞、不编造**。
    """
    code = FUTU_MARKET_CODES.get(str(market).upper())
    if not code:
        return _failure("calendar/unknown-market",
                        f"富途交易日历未登记市场 {market!r}（已登记 {sorted(FUTU_MARKET_CODES)}）")
    if wb_call is None:
        return _failure("calendar/no-callable", "未注入 wb_call（工具面入口），无法调富途")
    payload = {"market": code, "start": str(start), "end": str(end)}
    try:
        envelope = wb_call(FUTU_CALENDAR_TOOL, payload)
    except Exception as error:  # noqa: BLE001 —— 工具面异常也如实进链
        return _failure("calendar/futu-call-failed", _error_text(error))
    if not isinstance(envelope, dict):
        return _failure("calendar/futu-bad-envelope",
                        f"{FUTU_CALENDAR_TOOL} 返回非信封对象：{type(envelope).__name__}")
    if not envelope.get("ok"):
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        return _failure(error.get("code") or "calendar/futu-failed",
                        error.get("message") or f"{FUTU_CALENDAR_TOOL} 返回 ok=false")
    value = envelope.get("value") if isinstance(envelope.get("value"), dict) else {}
    rows = value.get("trading_days")
    if not isinstance(rows, list):
        return _failure("calendar/futu-unexpected-shape",
                        f"{FUTU_CALENDAR_TOOL} 的 value.trading_days 不是数组（"
                        f"{type(rows).__name__}）")
    days = sorted(_day_set([row.get("time") for row in rows if isinstance(row, dict)]))
    if not days:
        return _failure("calendar/futu-empty",
                        f"富途 {FUTU_CALENDAR_TOOL} 在 {start}~{end}（market={code}）返回 0 个交易日"
                        "（空结果不当可用）")
    return {"ok": True, "trading_days": [day.isoformat() for day in days],
            "coverage": {"start": days[0].isoformat(), "end": days[-1].isoformat()},
            "raw_rows": len(rows)}


def _akshare_module(injected):
    """注入的假模块优先（测试）；否则惰性 import（生产）。"""
    if injected is not None:
        if callable(injected) and not isinstance(injected, ModuleType):
            return injected()
        return injected
    return importlib.import_module("akshare")


def fetch_akshare_trading_days(market, start, end, *, akshare=None, retry=None):
    """AKShare ``tool_trade_date_hist_sina`` → 同上；**仅 A 股**，港/美股如实标注不支持。

    该接口无参数、返回全量历史（实测 8797 行，覆盖 1990-12-19~2026-12-31），因此按窗口
    过滤，并把**真实覆盖范围**放进 ``coverage``——请求窗口超出源范围时不假装覆盖。
    """
    from server import v3_fallback, v3_sources  # 局部导入：避免模块级依赖环

    code = str(market).upper()
    if code not in AKSHARE_MARKETS:
        return _failure(
            "calendar/akshare-unsupported-market",
            f"AKShare {AKSHARE_CALENDAR_FUNC} 只有 A 股日历，不覆盖 {code}；"
            "港/美股无开源自历（本环境已确认 akshare 无 trade_date 系港美股接口）")
    try:
        module = _akshare_module(akshare)
    except Exception as error:  # noqa: BLE001
        return _failure("calendar/akshare-missing", f"akshare 不可用：{_error_text(error)}")
    func = getattr(module, AKSHARE_CALENDAR_FUNC, None)
    if not callable(func):
        return _failure("calendar/akshare-missing-func",
                        f"akshare.{AKSHARE_CALENDAR_FUNC} 不存在（版本不兼容？）")

    timeout = float(getattr(v3_sources, "DEFAULT_TIMEOUT", 25.0))

    def call():
        with v3_sources._SocketTimeoutGuard(timeout):
            return func()

    options = dict(retry or {})
    value, meta = v3_fallback.retry_akshare(call, **options)
    if value is None:
        first = next((item for item in meta if item.get("error")), {})
        detail = (first.get("error") or {}).get("message") or "无错误明细"
        return _failure(
            "calendar/akshare-failed",
            f"akshare.{AKSHARE_CALENDAR_FUNC} 重试 {len(meta)} 次仍失败：{detail}；"
            f"attempts={json.dumps(meta, ensure_ascii=False)[:600]}")
    rows = v3_sources._rows_from_frame(value)
    all_days = sorted(_day_set([row.get("trade_date") for row in rows if isinstance(row, dict)]))
    if not all_days:
        return _failure("calendar/akshare-empty",
                        f"akshare.{AKSHARE_CALENDAR_FUNC} 返回 0 行日历数据（空结果不当可用）")
    first, last = _parse_day(start), _parse_day(end)
    days = [day for day in all_days if first <= day <= last]
    if not days:
        return _failure("calendar/akshare-out-of-range",
                        f"akshare.{AKSHARE_CALENDAR_FUNC} 覆盖 {all_days[0]}~{all_days[-1]}，"
                        f"与请求窗口 {start}~{end} 无交集")
    coverage = {"start": max(first, all_days[0]).isoformat(),
                "end": min(last, all_days[-1]).isoformat()}
    return {"ok": True, "trading_days": [day.isoformat() for day in days],
            "coverage": coverage, "raw_rows": len(rows),
            "source_note": (f"akshare.{AKSHARE_CALENDAR_FUNC} 全量 {len(rows)} 行、"
                            f"覆盖 {all_days[0]}~{all_days[-1]}；窗口内 "
                            f"{coverage['start']}~{coverage['end']} 有数据")}


# ── 解析（缓存 → 富途 → AKShare → 人工兜底 → None）─────────────────────────────


def resolve_market(wb_call, home, market, *, start, end, ttl_ms=None, clock=None,
                   akshare=None, env=None, live=None, skip_cache=False, retry=None):
    """单市场解析（``resolve_trading_days`` 的带原因版本）。

    返回 ``{"market","ok","result","chain","reason"}``：``result`` 就是契约里那个 dict
    （取不到时为 ``None``），``chain`` 是逐级尝试的真实时间线，``reason`` 是全失败时给调用方
    写进响应的一句原因（含每一级的错误原文）——调用方据此退化成「只按周末判断」。

    ``live`` 缺省 = 「有 ``wb_call``（生产工具面）或注入了 ``akshare``（测试）」；
    两者都没有时**只读缓存与人工兜底表、绝不打网络**（离线单测据此安全）。
    """
    from server import v3_fallback  # 局部导入：避免模块级依赖环

    code = str(market or "").upper()
    first, last = _parse_day(start), _parse_day(end)
    if first is None or last is None or first > last:
        reason = f"start/end 需为 YYYY-MM-DD 且 start<=end（收到 {start!r}~{end!r}）"
        return {"market": code, "ok": False, "result": None, "chain": [],
                "reason": reason}
    env = os.environ if env is None else env
    clock = time.monotonic if clock is None else clock
    ttl = _ttl_ms(ttl_ms, env)
    live_enabled = bool((wb_call is not None) or (akshare is not None)) if live is None else bool(live)
    start_iso, end_iso = first.isoformat(), last.isoformat()

    def memory_link():
        entry, error = read_memory_cache(code, start_iso, end_iso, clock=clock,
                                         ttl_ms=ttl, env=env)
        if entry is None:
            return _failure("calendar/cache-miss", error)
        return _cached_result(code, entry, "cache/memory")

    def disk_link():
        entry, error = load_calendar_file(home, code, start_iso, end_iso, ttl_ms=ttl, env=env)
        if entry is None:
            return _failure("calendar/cache-miss", error)
        return _cached_result(code, entry, "cache/disk")

    def futu_link():
        out = fetch_futu_trading_days(wb_call, code, start_iso, end_iso)
        if not out.get("ok"):
            return out
        return _live_result(code, out, "futu/info_trading_days",
                            f"富途 {FUTU_CALENDAR_TOOL} 返回 {out.get('raw_rows')} 行交易日"
                            f"（market={FUTU_MARKET_CODES.get(code)}）")

    def akshare_link():
        out = fetch_akshare_trading_days(code, start_iso, end_iso, akshare=akshare, retry=retry)
        if not out.get("ok"):
            return out
        return _live_result(code, out, "akshare/tool_trade_date_hist_sina",
                            out.get("source_note") or "AKShare A 股日历")

    def file_link():
        holidays, loaded, note = load_holidays(home=home, env=env)
        if not loaded:
            return _failure("calendar/legacy-file", note)
        picked = holidays.get(code)
        extra = "" if picked is not None else "（该市场无条目 → 视为无节假日）"
        holiday_days = sorted(picked or ())
        holiday_set = set(holiday_days)
        trading = [day.isoformat() for day in _workdays(first, last) if day not in holiday_set]
        if not trading:
            return _failure("calendar/legacy-file-empty",
                            f"人工兜底表 {note}{extra}，window {start_iso}~{end_iso} 内没有任何工作日")
        return {
            "ok": True,
            "source": "file",
            "trading_days": trading,
            "coverage": {"start": start_iso, "end": end_iso},
            "complete": True,
            "holidays": [day.isoformat() for day in holiday_days],
            "note": (f"{note}{extra}；交易日 = 工作日 − 表内节假日"
                     f"（人工表只覆盖 {len(holiday_days)} 个节假日，不含临时休市）"),
            "cached": False,
        }

    chain = []
    if not skip_cache:
        chain.extend([("cache/memory", memory_link), ("cache/disk", disk_link)])
    if live_enabled and wb_call is not None:
        chain.append(("futu/info_trading_days", futu_link))
    if live_enabled:
        chain.append(("akshare/tool_trade_date_hist_sina", akshare_link))
    chain.append((f"file/{LEGACY_HOLIDAYS_FILE}", file_link))

    value, used, attempts = v3_fallback.run_chain(chain, clock=clock)
    attempts = v3_fallback.attempts_chain(attempts)
    if value is None:
        reason = (f"未取到交易日历（尝试：{v3_fallback.describe_attempts(attempts)}）")
        return {"market": code, "ok": False, "result": None, "chain": attempts, "reason": reason}

    result = dict(value)
    days = sorted(_day_set(result.get("trading_days")))
    coverage = result.get("coverage") or {"start": start_iso, "end": end_iso}
    result.update({
        "market": code,
        "start": start_iso,
        "end": end_iso,
        "trading_days": [day.isoformat() for day in days],
        "coverage": coverage,
        "source": result.get("source") or used,
        "as_of": now_iso(),
        "chain": attempts,
    })
    if result.get("complete") is None:
        result["complete"] = (coverage.get("start") == start_iso and coverage.get("end") == end_iso)
    if not result.get("holidays"):
        result["holidays"] = holidays_for(code, coverage.get("start"), coverage.get("end"),
                                          result["trading_days"])
    if not result.get("data_as_of"):
        result["data_as_of"] = result.get("as_of")
    if result["source"] in ("futu/info_trading_days", "akshare/tool_trade_date_hist_sina"):
        # 只缓存实时源的结果：人工兜底表不进缓存（否则上游恢复后仍被判为「没有日历」）。
        entry = _entry_of(code, result)
        _remember(entry, clock=clock, ttl_ms=ttl)
        result["cache_path"], result["cache_error"] = write_calendar_file(home, {code: entry})
        result["cached"] = True
    return {"market": code, "ok": True, "result": result, "chain": attempts, "reason": None}


def _workdays(first, last):
    cursor = first
    while cursor <= last:
        if cursor.weekday() < 5:
            yield cursor
        cursor += timedelta(days=1)


def _entry_of(market, result):
    """结果 → 缓存条目（落盘结构与进程内缓存共用一份）。"""
    return {
        "market": market,
        "trading_days": list(result.get("trading_days") or []),
        "holidays": list(result.get("holidays") or []),
        "source": result.get("source"),
        "data_as_of": result.get("data_as_of"),
        "start": result.get("start"),
        "end": result.get("end"),
        "coverage": dict(result.get("coverage") or {}),
        "complete": bool(result.get("complete")),
        "note": result.get("note"),
    }


def _cached_result(market, entry, served_from):
    age = entry.get("cache_age_ms")
    return {
        "ok": True,
        "source": "cache",
        "origin_source": entry.get("source"),
        "served_from": served_from,
        "trading_days": list(entry.get("trading_days") or []),
        "holidays": list(entry.get("holidays") or []),
        "coverage": dict(entry.get("coverage") or {}),
        "complete": bool(entry.get("complete")),
        "data_as_of": entry.get("data_as_of"),
        "cache_age_ms": age,
        "note": (f"命中{served_from} 缓存（原来源 {entry.get('source')}，"
                 f"数据 data_as_of {entry.get('data_as_of')}"
                 + (f"，已缓存 {age}ms" if age is not None else "") + "）"),
        "cached": False,
    }


def _live_result(market, out, source, note):
    coverage = out.get("coverage") or {}
    return {
        "ok": True,
        "source": source,
        "trading_days": list(out.get("trading_days") or []),
        "coverage": coverage,
        "complete": None,  # 由 resolve_market 按请求窗口补齐
        "note": note,
        "cached": False,
    }


def resolve_trading_days(wb_call, home, market, *, start, end, ttl_ms=None, clock=None,
                         **kwargs):
    """契约入口：单个市场的交易日历（升序去重）或 ``None``。

    优先级：进程内 TTL 缓存 → 落盘缓存 → 富途 ``info_trading_days`` → AKShare（A 股）→
    ``<home>/market-holidays.json`` 人工兜底 → ``None``。额外关键字（``akshare``/``env``/
    ``live``/``skip_cache``/``retry``）透传给 ``resolve_market``，详见它的 docstring。
    """
    return resolve_market(wb_call, home, market, start=start, end=end, ttl_ms=ttl_ms,
                          clock=clock, **kwargs)["result"]


def resolve_calendar(wb_call, home, markets=("SH", "HK", "US"), *, start=None, end=None,
                     today=None, ttl_ms=None, clock=None, **kwargs):
    """多市场解析 → ``{market: resolve_market 的返回值}``（窗口缺省用 ``calendar_window``）。"""
    if start is None or end is None:
        first, last = calendar_window(today)
        start = start or first.isoformat()
        end = end or last.isoformat()
    return {
        str(market).upper(): resolve_market(wb_call, home, market, start=start, end=end,
                                            ttl_ms=ttl_ms, clock=clock, **kwargs)
        for market in markets
    }


def refresh_cache(home, markets=("SH", "HK", "US"), horizon_days=400, *, wb_call=None,
                  akshare=None, env=None, clock=None, ttl_ms=None, today=None,
                  lookback_days=None, retry=None):
    """刷新落盘缓存（**只取数 + 写缓存**，不涉交易）：供调度器/定时任务或
    ``POST /api/v3/markets/calendar/refresh`` 调用。

    跳过缓存直取实时源（``skip_cache=True``）；只把**实时源**的成功结果写进
    ``<home>/market-calendar.json``（人工兜底表不进缓存）。返回逐市场小结，取不到的市场如实
    记 ``ok:false`` + 原因，不写占位条目。

    ``wb_call`` 是工具面入口（生产传 ``app.v3_run``）；缺省时只有注入了 ``akshare`` 才会走
    AKShare，否则本次刷新只能读人工兜底表——如实报「未取到实时源」。
    """
    first, last = calendar_window(today, lookback_days=lookback_days,
                                  lookahead_days=horizon_days)
    start_iso, end_iso = first.isoformat(), last.isoformat()
    entries = {}
    summary = {}
    for market in markets:
        detail = resolve_market(wb_call, home, market, start=start_iso, end=end_iso,
                                ttl_ms=ttl_ms, clock=clock, akshare=akshare, env=env,
                                skip_cache=True, retry=retry)
        code = detail["market"]
        result = detail.get("result")
        if result is None:
            summary[code] = {"ok": False, "source": None, "chain": detail["chain"],
                             "reason": detail["reason"]}
            continue
        if result["source"] not in ("futu/info_trading_days", "akshare/tool_trade_date_hist_sina"):
            summary[code] = {"ok": False, "source": result["source"], "chain": detail["chain"],
                             "reason": f"本次只拿到 {result['source']}（非实时源），未写入缓存"}
            continue
        entry = _entry_of(code, result)
        entries[code] = entry
        summary[code] = {"ok": True, "source": result["source"], "chain": detail["chain"],
                         "trading_days": len(entry["trading_days"]),
                         "holidays": len(entry["holidays"]),
                         "coverage": entry["coverage"], "complete": entry["complete"]}
    path = os.path.join(str(home), CALENDAR_FILE) if home else ""
    error = None
    if entries:
        path, error = write_calendar_file(home, entries)
    ok = bool(entries)
    payload = {
        "ok": ok,
        "as_of": now_iso(),
        "path": path,
        "window": {"start": start_iso, "end": end_iso,
                   "lookback_days": max(0, int(CALENDAR_LOOKBACK_DAYS if lookback_days is None
                                                else lookback_days)),
                   "horizon_days": max(0, int(horizon_days))},
        "markets": summary,
        "note": ("刷新 = 只读取数 + 写 <home>/market-calendar.json（只读语义，不涉交易）；"
                 "只写入实时源（富途/AKShare）的结果，人工兜底表不写入缓存"),
    }
    if error:
        payload["write_error"] = error
    if not ok:
        payload["error"] = {
            "code": "calendar/refresh-no-live-source",
            "message": ("本次刷新没有拿到任何实时源（富途/AKShare）的交易日历 → 缓存未更新；"
                        "逐市场原因见 markets[].reason"),
        }
    return payload
