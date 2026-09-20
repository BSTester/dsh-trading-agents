"""下单前闸门的**行业口径**读数（行业集中度红线的数据面）。

``v3_ops.OmsLedger`` 做风控分级时需要两件东西：**阈值**（``v3_ops.LIMITS``）与**读数**
（单笔占比 / 回撤 / 行业集中度）。前两个读数来自工具面，行业口径原来恒为常量 ``0.0``
（``v3_ops.INDUSTRY_SOURCE`` 写着 ``no-data``）——即「行业红线写在常量里、从不参与判定」。
本模块把行业读数接到真实数据源上，判定逻辑仍留在 ``v3_ops.check_order``（纯函数）。

读数来源（按优先级，**绝不估算、绝不合并市场**）
------------------------------------------------
1. **落盘探测缓存** ``<home>/v3-risk-probe.json``（``observability.record_risk_probe`` 写、
   ``observability.load_risk_probe`` 读）——与 ``/metrics`` 的 ``quantwb_risk_industry_*``
   **同一份事实源、同一个新鲜度常量**（``QUANT_RISK_PROBE_MAX_AGE``，默认 6h）。缓存新鲜
   （``now - generated_at <= max_age``）即命中，**不打任何上游**。
2. 缓存缺失/损坏/过期且显式允许现取（调用方传了 ``wb_call``）→ 经
   ``v3_industry.industry_exposure`` 现取**一次**（板块映射走 ``v3_ratelimit`` 限流器）。
   成功即回读缓存（若写入器落盘了）以复用落盘口径；缓存仍未更新时直接用本次信封。
3. 两条路都不行 → ``industry_pct=None`` + ``industry_source="no-data"`` + ``reason``。
   **调用方按 fail-open 处理**（不因缺数据阻断一切），但必须在 ``reasons`` 里写明
   「数据不可用、未参与阻断」——缺数据既不能静默放行，也不能变成全量阻断。

返回信封（字段全部可直接进 ``risk`` 与台账 ``history``，供审计留痕）::

    {"market": "SH", "industry_pct": 37.5, "industry_source": "cache/futu/info_owner_plate",
     "as_of": "2026-09-20T09:58:52.053639+00:00", "as_of_epoch": 1789898332.0536346,
     "probe_age_ms": 123.4, "stale": False, "top_industry": "股份制银行Ⅱ",
     "missing": 0, "universe": 28, "limit_pct": 20.0, "breach": True, "reason": None,
     "cache_state": "fresh", "origin": "cache"}

* ``industry_pct`` 为 ``None`` 表示**没有读数**（调用方不得当成 0 处理）；
* ``missing``（上游没取到行业分类的标的数）非空时读数只是**下界**，调用方须在
  ``reasons`` 里标注 —— 见 ``v3_ops.check_order`` 的 ``industry_missing`` 分支；
* ``market`` 为 ``None`` 表示**跨市场取最严**（台账里认不出市场前缀的订单）；
* ``cache_state`` ∈ ``fresh`` / ``stale`` / ``missing`` / ``invalid`` / ``unreadable``；
  ``origin`` ∈ ``cache`` / ``fetch`` / ``none``。

数据诚实性
----------
不猜时刻（``generated_at`` 非正数/非数字 → 缓存视为不可用）、不猜读数（``top_weight_pct``
不是真正的数字 → 该市场 ``no_data``）、不跨市场合并暴露、不把 ``no-data`` 说成 0%。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from server import v3_universe

__all__ = [
    "INDUSTRY_NO_DATA_SOURCE",
    "MARKET_UNKNOWN",
    "industry_context",
    "industry_context_key",
    "industry_trail",
    "market_of_order",
    "no_data_envelope",
    "parse_risk_probe",
]

#: 没有读数时的来源口径（与 ``v3_ops.INDUSTRY_SOURCE`` 同义：**不是** 0%，是「不知道」）
INDUSTRY_NO_DATA_SOURCE = "no-data"

#: 台账订单认不出市场前缀时的分组键（跨市场取最严，**不合并**暴露）
MARKET_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 小工具（只读；不抛错）
# ---------------------------------------------------------------------------
def _now_epoch():
    return time.time()


def _pct(value):
    """只接受真正的数字（``bool`` 不算、字符串不转）；其余 ``None``（不猜读数）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):  # NaN / ±Inf
        return None
    return number


def _positive_epoch(value):
    number = _pct(value)
    return number if number is not None and number > 0 else None


def _count(value):
    """计数类字段：非负整数才认，其余 ``None``（缺数据 ≠ 0）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and float(value).is_integer():
        return int(value)
    return None


def _iso(value):
    """epoch 秒 → ISO8601（UTC）；非正/非数 → ``None``（宁缺勿造）。"""
    number = _positive_epoch(value)
    if number is None:
        return None
    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()


def _max_age_ms(max_age_ms):
    """缓存最大可信年龄（毫秒）；``None``/非法/非正 → ``observability.risk_probe_max_age()``。

    默认值与 ``/metrics`` 的 ``quantwb_risk_industry_*`` **同源**（``QUANT_RISK_PROBE_MAX_AGE``，
    默认 ``21600`` 秒 = 6h）——闸门与观测不允许各持一套新鲜度口径。
    """
    if max_age_ms is not None:
        number = _pct(max_age_ms)
        if number is not None and number > 0:
            return number
    from server import observability  # 局部导入：本模块只读它的缓存/常量
    return observability.risk_probe_max_age() * 1000.0


def _trim(text, limit=200):
    """原因原文截断（审计留痕要能读；上游错误可能很长）。"""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def no_data_envelope(market=None, *, reason, cache_state="missing", max_age_ms=None):
    """统一的「没有读数」信封（``industry_pct=None``，调用方 fail-open）。"""
    return {
        "market": market,
        "industry_pct": None,
        "industry_source": INDUSTRY_NO_DATA_SOURCE,
        "as_of": None,
        "as_of_epoch": None,
        "probe_age_ms": None,
        "stale": False,
        "top_industry": None,
        "missing": None,
        "universe": None,
        "limit_pct": None,
        "breach": None,
        "reason": reason,
        "cache_state": cache_state,
        "origin": "none",
        "max_age_ms": max_age_ms,
    }


# ---------------------------------------------------------------------------
# 缓存解析（与 observability._render_risk_industry 同一份口径，逐字段同义）
# ---------------------------------------------------------------------------
def parse_risk_probe(raw, *, market=None, now=None, max_age_ms=None):
    """解析落盘探测缓存 → 读数信封（**纯函数**，不读文件、不打上游）。

    ``raw`` 为 ``None``（文件缺失/损坏）→ ``cache_state="missing"``；``generated_at``
    非正/非数 → ``cache_state="invalid"``；超过最大年龄 → ``cache_state="stale"``。
    这三种都不给 ``industry_pct``——**拿过期读数当现在的结论就是编造**。
    """
    max_age = _max_age_ms(max_age_ms)
    moment = _now_epoch() if now is None else float(now)
    if not isinstance(raw, dict):
        return no_data_envelope(market, reason="探测缓存不存在或不是 JSON 对象",
                                cache_state="missing", max_age_ms=max_age)
    stamp = _positive_epoch(raw.get("generated_at"))
    if stamp is None:
        return no_data_envelope(market, reason="探测缓存缺少可用的 generated_at（不猜时刻）",
                                cache_state="invalid", max_age_ms=max_age)
    age_ms = max(0.0, (moment - stamp) * 1000.0)
    as_of = raw.get("generated_at_iso")
    as_of = str(as_of) if as_of else _iso(stamp)
    rows = raw.get("per_market")
    rows = rows if isinstance(rows, dict) else {}
    base = {
        "as_of": as_of,
        "as_of_epoch": stamp,
        "probe_age_ms": age_ms,
        "max_age_ms": max_age,
        "limit_pct": _pct(raw.get("limit_pct")),
    }

    def _no_data(reason, cache_state, **extra):
        envelope = no_data_envelope(market, reason=reason, cache_state=cache_state,
                                    max_age_ms=max_age)
        envelope.update(base)
        envelope.update(extra)
        return envelope

    if age_ms > max_age:
        return _no_data(f"探测缓存已过期（{age_ms / 1000.0:.0f}s > "
                        f"{max_age / 1000.0:.0f}s），过期读数不作为结论",
                        "stale", stale=True)
    if market is not None:
        row = rows.get(market)
        if not isinstance(row, dict):
            return _no_data(f"探测缓存里没有 {market} 的读数（探测覆盖 "
                            f"{'、'.join(sorted(str(key) for key in rows)) or '无市场'}）",
                            "missing")
        return _market_row_envelope(row, base, cache_state="fresh")

    usable = [row for row in rows.values()
              if isinstance(row, dict) and row.get("ok")
              and _pct(row.get("top_weight_pct")) is not None]
    if not usable:
        return _no_data("探测缓存里没有任何市场给出可用读数（不拿 0% 顶替）", "missing")
    top = max(usable, key=lambda row: _pct(row["top_weight_pct"]))
    envelope = _market_row_envelope(top, base, cache_state="fresh")
    envelope["market"] = None
    envelope["industry_source"] = f"cache/max({','.join(sorted(str(key) for key in rows))})"
    envelope["reason"] = (f"订单标的认不出市场前缀 → 取各市场最严读数"
                          f"（{top.get('market')} 的 {envelope['top_industry']}）")
    return envelope


def _market_row_envelope(row, base, *, cache_state):
    """一个市场的 ``per_market`` 行 → 读数信封（``ok=false`` 行如实记成无读数）。"""
    market = str(row.get("market") or "")
    weight = _pct(row.get("top_weight_pct"))
    ok = bool(row.get("ok")) and weight is not None
    envelope = {
        "market": market or None,
        "industry_pct": weight if ok else None,
        "industry_source": (f"cache/{row.get('source')}" if ok and row.get("source")
                            else ("cache/unknown" if ok else INDUSTRY_NO_DATA_SOURCE)),
        "as_of": base["as_of"],
        "as_of_epoch": base["as_of_epoch"],
        "probe_age_ms": base["probe_age_ms"],
        "max_age_ms": base["max_age_ms"],
        "stale": False,
        "top_industry": (str(row.get("top_industry"))
                         if ok and row.get("top_industry") is not None else None),
        "missing": _count(row.get("missing")),
        "universe": _count(row.get("universe")),
        "limit_pct": base["limit_pct"],
        "breach": row.get("breach") if isinstance(row.get("breach"), bool) else None,
        "reason": None,
        "cache_state": cache_state,
        "origin": "cache",
    }
    if not ok:
        envelope["industry_source"] = INDUSTRY_NO_DATA_SOURCE
        error = row.get("error")
        envelope["reason"] = (f"{market or '该市场'} 探测行没有可用读数"
                              + (f"（{error}）" if error else "（不拿 0% 顶替）"))
        envelope["breach"] = None
    return envelope


def _read_cache(home):
    """读落盘缓存；返回 ``(raw, state)``。文件缺失/坏 JSON → ``(None, "missing")``。"""
    from server import observability
    path = Path(observability.risk_probe_path(home))
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "unreadable"
    return (raw, "ok") if isinstance(raw, dict) else (None, "invalid")


# ---------------------------------------------------------------------------
# 现取（经 v3_industry → 板块映射过 v3_ratelimit 限流器）
# ---------------------------------------------------------------------------
def _industry_limit_pct():
    """现取时用的 ``limit_pct``：与 ``observability.RISK_PROBE_LIMIT_PCT`` 同源。"""
    from server import observability
    return observability.RISK_PROBE_LIMIT_PCT


def _fetch_exposure(wb_call, home, request_market, *, limit_pct, sleep=None,
                    plate_scan_limit=None):
    """现取一次行业暴露（只读）。返回 ``(envelope, error_text)``；异常不外抛。"""
    from server import v3_industry
    kwargs = {"market": request_market or ""}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if plate_scan_limit is not None:
        kwargs["plate_scan_limit"] = plate_scan_limit
    try:
        return v3_industry.industry_exposure(wb_call, home, limit_pct=limit_pct, **kwargs), None
    except Exception as error:  # noqa: BLE001 —— 现取失败收敛成 no-data，不打断对账
        return None, f"{type(error).__name__}: {error}"


def _envelope_from_exposure(envelope, request_market, *, now=None):
    """``industry_exposure`` 成功信封 → 读数信封。"""
    moment = _now_epoch() if now is None else float(now)
    top = envelope.get("top") if isinstance(envelope.get("top"), dict) else {}
    weight = _pct(top.get("weightPct"))
    sources = envelope.get("sources") if isinstance(envelope.get("sources"), dict) else {}
    missing = envelope.get("missing")
    universe = envelope.get("universe")
    stamp = _positive_epoch(envelope.get("as_of_epoch")) or moment
    as_of = envelope.get("as_of")
    limit_pct = _pct(envelope.get("limitPct"))
    if weight is None:
        out = no_data_envelope(request_market,
                               reason="现取成功信封里没有 top.weightPct（不拿 0% 顶替）",
                               cache_state="fetched")
        out["as_of"] = str(as_of) if as_of else _iso(stamp)
        out["as_of_epoch"] = stamp
        out["probe_age_ms"] = 0.0
        out["limit_pct"] = limit_pct
        return out
    plate = sources.get("plate")
    return {
        "market": request_market or None,
        "industry_pct": weight,
        "industry_source": f"fetch/{plate}" if plate else "fetch/unknown",
        "as_of": str(as_of) if as_of else _iso(stamp),
        "as_of_epoch": stamp,
        "probe_age_ms": 0.0,
        "max_age_ms": None,
        "stale": False,
        "top_industry": None if top.get("industry") is None else str(top.get("industry")),
        "missing": len(missing) if isinstance(missing, list) else None,
        "universe": len(universe) if isinstance(universe, list) else None,
        "limit_pct": limit_pct,
        "breach": (None if limit_pct is None or weight is None else bool(weight > limit_pct)),
        "reason": None,
        "cache_state": "fetched",
        "origin": "fetch",
    }


def _fetch_reason(envelope):
    error = (envelope or {}).get("error") if isinstance(envelope, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code") or "industry/upstream"
    return f"{code}: {error.get('message') or '取数失败'}"[:300]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def industry_context(home, market=None, *, max_age_ms=None, wb_call=None, sleep=None,
                     plate_scan_limit=None, now=None):
    """行业集中度读数（见模块 docstring 的三级来源与信封字段）。

    ``market``：``SH`` / ``HK`` / ``US``（大小写不敏感，``SZ``/``BJ`` 归 ``SH``，与
    ``v3_universe`` 同口径）；``None`` 表示**取各市场最严**（认不出市场前缀的订单）。
    ``wb_call``：现取用的工具面回调（``v3_run``）；给了它就表示**允许现取一次**。
    返回的 ``reason`` 非空 = 没有读数，调用方按 fail-open 处理并在 ``reasons`` 里留痕。
    """
    max_age = _max_age_ms(max_age_ms)
    request_market = None
    if market is not None and str(market).strip():
        request_market = v3_universe.normalize_market(market)
        if request_market is None:
            return no_data_envelope(None, reason=f"market 需为 SH/HK/US，收到 {market!r}",
                                    cache_state="invalid", max_age_ms=max_age)
    raw, state = _read_cache(home)
    if state == "ok":
        envelope = parse_risk_probe(raw, market=request_market, now=now, max_age_ms=max_age)
        if envelope.get("industry_pct") is not None:
            return envelope
        cache_reason = envelope.get("reason")
        cache_state = envelope.get("cache_state")
        cached = envelope
    else:
        cache_reason = {"missing": "探测缓存不存在",
                        "unreadable": "探测缓存不可读或不是合法 JSON",
                        "invalid": "探测缓存不是 JSON 对象"}[state]
        cache_state = state
        cached = None

    if wb_call is None:
        envelope = cached or no_data_envelope(request_market, reason=cache_reason,
                                              cache_state=cache_state, max_age_ms=max_age)
        if envelope.get("industry_pct") is None:
            envelope["reason"] = cache_reason
        return envelope

    exposure, error_text = _fetch_exposure(wb_call, home, request_market,
                                          limit_pct=_industry_limit_pct(), sleep=sleep,
                                          plate_scan_limit=plate_scan_limit)
    if error_text is not None or not isinstance(exposure, dict) or not exposure.get("ok"):
        detail = error_text or _fetch_reason(exposure)
        return no_data_envelope(request_market,
                                reason=_trim(f"现取失败（{cache_reason}；{detail}）"),
                                cache_state=cache_state, max_age_ms=max_age)
    fetched = _envelope_from_exposure(exposure, request_market, now=now)
    if fetched.get("industry_pct") is None:
        return fetched
    # 现取成功后回读缓存：写入器（observability.probe_industry_exposure）会落盘，
    # 闸门与 /metrics 即共用同一份落盘事实源。
    fresh_raw, fresh_state = _read_cache(home)
    if fresh_state == "ok":
        envelope = parse_risk_probe(fresh_raw, market=request_market, now=now,
                                    max_age_ms=max_age)
        if envelope.get("industry_pct") is not None:
            envelope["origin"] = "fetch"
            return envelope
    return fetched


# ---------------------------------------------------------------------------
# 台账侧小工具
# ---------------------------------------------------------------------------
def market_of_order(ticker):
    """订单标的 → 市场口径；认不出前缀 → ``None``（调用方按最严口径处理）。"""
    return v3_universe.market_of_ticker(ticker)


def industry_context_key(market):
    """台账里按订单市场缓存读数的键（``None`` → ``"unknown"``，不跨市场合并）。"""
    return market if market else MARKET_UNKNOWN


def industry_trail(envelope):
    """读数信封 → 台账 ``history`` 行的**扁平**留痕字段（键前缀 ``industry_``，只加不删）。

    阻断/放行都要能事后回答「当时看的是哪个数、哪来的、哪一刻、缺多少」，因此留痕带上
    读数、来源、as_of、探测年龄、缺失数与市场；``context`` 之类的大字段**不进**留痕。
    """
    if not isinstance(envelope, dict):
        return {}
    return {
        "industry_pct": _pct(envelope.get("industry_pct")),
        "industry_source": envelope.get("industry_source") or INDUSTRY_NO_DATA_SOURCE,
        "industry_top": envelope.get("top_industry"),
        "industry_market": envelope.get("market"),
        "industry_as_of": envelope.get("as_of"),
        "industry_probe_age_ms": envelope.get("probe_age_ms"),
        "industry_missing": _count(envelope.get("missing")),
        "industry_universe": _count(envelope.get("universe")),
        "industry_reason": envelope.get("reason"),
    }
