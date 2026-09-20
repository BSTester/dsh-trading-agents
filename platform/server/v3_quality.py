"""成交质量：``GET /api/v3/execution/quality?market=SH&mode=sim``。

口径（**写在代码里，也写进响应里**——前端原样展示，不做二次换算）:

  * **成交率** = 已成交（含部分成交）/ 总委托；
  * **撤单率** = 已撤 / 总委托；
  * **滑点** = (成交均价 − 委托价) / 委托价，**按市场分别计算，不跨市场/币种合并**。
    买单成交价高于委托价为正（不利）；卖单成交价低于委托价为正（不利）——即
    ``slippageBps`` 恒为「正 = 不利」的口径；
  * 富途**没有**逐笔委托价格明细（模拟盘的成交流水还是由委托派生的），所以滑点是
    「成交均价 vs 委托价」的近似——这一点原文写进 ``missing``，不假装是逐笔 TCA。

数据来源：工具面 ``orders_history``（委托状态是唯一事实源）+ ``deals_history``（派生成交，
用于名义金额核对）。**成交类数据没有开源替代**（AKShare/Tushare 都拿不到委托状态与成交
回报）——富途不可用时如实报错并在错误信息里写明这一点，绝不返回估算值。

数据不足（窗口内一笔委托都没有）→ ``{ok:false,error:{code:'quality/no-orders',...}}``。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from server import v3_fallback

__all__ = [
    "MARKET_TRD_CODES",
    "ORDER_STATUS",
    "execution_quality",
    "register",
]

#: 官方逐值发布的模拟盘订单状态码（``docs/TOOL-LIMITS.md`` §模拟交易订单状态码，2026-09-17 核对）：
#: ``2=已提交 3=部分成交 4=全部成交 5=已撤 6=拒绝``。**表外的码不解释**（进 unknown，不猜标签）。
ORDER_STATUS = {
    2: "submitted",
    3: "partial",
    4: "filled",
    5: "cancelled",
    6: "rejected",
}

#: 市场 → 交易时段时区（与 v3_market_calendar 同一份口径，用于把微秒时间戳折算成交易日）。
MARKET_TZ = {
    "SH": "Asia/Shanghai",
    "SZ": "Asia/Shanghai",
    "BJ": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "US": "America/New_York",
}

#: 允许的 ``market`` 取值（工具面 trd_market 枚举 + A 股三个前缀；A 股走 sim 等价端点实测可用）。
MARKET_TRD_CODES = ("SH", "SZ", "BJ", "HK", "US", "SG", "CA", "JP", "KR", "FUTURES", "HKCC")

SLIPPAGE_NOTE = (
    "滑点为近似口径：柜台上游不提供逐笔委托价格明细（模拟盘成交流水还是由委托派生的），"
    "按『成交均价 vs 委托价』计算；买单成交价高于委托价为正（不利），"
    "卖单成交价低于委托价为正（不利）"
)
NO_SUBSTITUTE_NOTE = (
    "成交质量类数据没有开源替代：AKShare/Tushare 均不提供逐笔委托状态与成交回报，"
    "富途不可用时只能如实报错（不返回估算值）"
)


def _error(code, message, **extra):
    payload = {"ok": False, "error": {"code": str(code), "message": str(message)[:400]}}
    payload["error"].update(extra)
    return payload


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _value_of(envelope):
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return None
    value = envelope.get("value")
    return value if isinstance(value, dict) else None


def _error_of(envelope):
    if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
        error = dict(envelope["error"])
        return {"code": str(error.get("code") or "quality/upstream"),
                "message": str(error.get("message") or "上游返回 ok=false")}
    return {"code": "quality/upstream", "message": "上游工具调用失败（无 error 明细）"}


def _number(value):
    """订单字段是字符串；非数值返回 ``None``（**不返回 0 冒充**）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if value != value else float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _rows_for_market(envelope, market):
    """从 ``orders_history``/``deals_history`` 的信封里取出**该市场**的行（不跨市场合并）。

    返回 ``(rows, groups_seen, errors)``；``errors`` 是上游按账户回报的失败清单（原样保留）。
    """
    value = _value_of(envelope) or {}
    groups = value.get("groups") if isinstance(value.get("groups"), list) else []
    rows = []
    seen = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        label = str(group.get("market") or "").upper()
        seen.append(label)
        if label != market:
            continue
        for row in group.get("rows") or []:
            if isinstance(row, dict):
                rows.append(row)
    upstream_errors = value.get("errors") if isinstance(value.get("errors"), list) else []
    return rows, seen, [item for item in upstream_errors if isinstance(item, dict)]


def _micros_to_date(value, tz):
    """微秒时间戳（字符串/整数）→ 该市场时区下的 ``YYYY-MM-DD``；非法返回 ``None``。"""
    number = _number(value)
    if number is None or number <= 0:
        return None
    try:
        moment = datetime.fromtimestamp(number / 1_000_000, tz=timezone.utc).astimezone(tz)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.date().isoformat()


def _classify(row):
    """单笔委托 → ``(state, cum_qty, qty, price, avg, slippage_bps, notional)``。

    ``state`` 取自官方状态码；**表外码给 ``unknown``**（不猜）。数量口径优先于状态文本：
    ``status=4`` 但累计成交不足委托量 → 记进 ``contradictions``（见调用方），不悄悄改正。
    """
    status = _number(row.get("status"))
    state = ORDER_STATUS.get(int(status)) if status is not None and float(status).is_integer() else None
    if state is None:
        state = "unknown"
    cum_qty = _number(row.get("cum_qty"))
    qty = _number(row.get("qty"))
    price = _number(row.get("price"))
    avg = _number(row.get("avg_fill_price"))
    side = _number(row.get("side"))
    filled_qty = cum_qty if cum_qty is not None and cum_qty > 0 else None
    slippage = None
    notional = None
    if filled_qty is not None and price and price > 0 and avg is not None and avg > 0:
        raw = (avg - price) / price
        # 卖单(side=2)方向相反：成交价低于委托价才是不利 → 取负号，保持「正 = 不利」
        slippage = (-raw if side == 2 else raw) * 10000.0
        notional = filled_qty * avg
    return {
        "state": state,
        "status": int(status) if status is not None and float(status).is_integer() else None,
        "cum_qty": cum_qty,
        "qty": qty,
        "price": price,
        "avg_fill_price": avg,
        "side": side,
        "filled_qty": filled_qty,
        "slippage_bps": slippage,
        "notional": notional,
        "symbol": str(row.get("symbol") or "").strip() or None,
        "order_id": str(row.get("order_id") or "").strip() or None,
        "update_time": row.get("update_time"),
    }


def _weighted(points, key, weight_key="notional"):
    """按名义金额加权的均值；权重全缺时退化为**简单平均**并在 notes 里说明。"""
    weighted_sum = 0.0
    weight_total = 0.0
    simple = []
    for point in points:
        value = point.get(key)
        if value is None:
            continue
        weight = point.get(weight_key)
        simple.append(value)
        if weight is not None and weight > 0:
            weighted_sum += value * weight
            weight_total += weight
    if weight_total > 0:
        return weighted_sum / weight_total, "notional-weighted"
    if simple:
        return sum(simple) / len(simple), "simple-average"
    return None, None


def execution_quality(v3_run, market="SH", mode="sim", *, timeout=v3_fallback.PROBE_CHAIN_TIMEOUT):
    """成交质量（可注入 ``v3_run``；路由只是它的异步外壳）。契约见模块 docstring。"""
    code = str(market or "SH").strip().upper()
    if code not in MARKET_TRD_CODES:
        return _error("quality/bad-market", f"market 需为 {list(MARKET_TRD_CODES)} 之一，收到 {market!r}",
                      supported=list(MARKET_TRD_CODES))
    wanted_mode = str(mode or "sim").strip().lower() or "sim"

    def orders_probe():
        return v3_run("orders_history", {"market": code, "page_size": 100, "mode": wanted_mode})

    def deals_probe():
        return v3_run("deals_history", {"market": code, "page_size": 50, "mode": wanted_mode})

    orders, orders_source, orders_attempts = v3_fallback.run_chain(
        [("futu/orders_history", orders_probe)], timeout=timeout
    )
    # 成交流水是**补充**（名义金额/派生成交说明），它失败不拖垮整条口径，但要如实标注。
    deals, deals_source, deals_attempts = v3_fallback.run_chain(
        [("futu/deals_history", deals_probe)], timeout=timeout
    )
    chain = v3_fallback.attempts_chain(orders_attempts) + v3_fallback.attempts_chain(deals_attempts)

    if orders is None:
        error = v3_fallback.last_error(orders_attempts)
        return _error(
            error.get("code") or "quality/upstream",
            f"券商委托历史不可用（{error.get('code')}：{error.get('message')}）。{NO_SUBSTITUTE_NOTE}",
            market=code,
            mode=wanted_mode,
            upstream=error,
            chain=chain,
        )

    orders_value = _value_of(orders) or {}
    rows, groups_seen, upstream_errors = _rows_for_market(orders, code)
    if not rows:
        message = f"{code} 在 {orders_value.get('window') or '该窗口'} 内没有委托记录 → 无法计算成交率/撤单率/滑点"
        if groups_seen:
            message += f"（上游返回的市场分组：{groups_seen}）"
        return _error("quality/no-orders", message, market=code, mode=wanted_mode, chain=chain)

    parsed = [_classify(row) for row in rows]
    counts = {"filled": 0, "partial": 0, "cancelled": 0, "rejected": 0, "submitted": 0, "unknown": 0}
    contradictions = []
    for item in parsed:
        counts[item["state"]] = counts.get(item["state"], 0) + 1
        if item["state"] == "filled" and item["qty"] and item["cum_qty"] is not None:
            if item["cum_qty"] < item["qty"]:
                contradictions.append(
                    {"order_id": item["order_id"], "qty": item["qty"], "cum_qty": item["cum_qty"]}
                )

    total = len(parsed)
    filled_with_partial = counts["filled"] + counts["partial"]
    fill_rate = filled_with_partial / total * 100.0
    cancel_rate = counts["cancelled"] / total * 100.0

    filled = [item for item in parsed if item["filled_qty"] is not None]
    avg_slippage, weight_method = _weighted(filled, "slippage_bps")
    notional_total = sum(item["notional"] or 0.0 for item in filled) or 0.0

    tz = ZoneInfo(MARKET_TZ.get(code, "UTC"))
    buckets = {}
    for item in filled:
        day = _micros_to_date(item["update_time"], tz) or "unknown"
        bucket = buckets.setdefault(day, {"t": day, "slippage_sum": 0.0, "weight": 0.0,
                                          "slippage_simple": [], "notional": 0.0, "orders": 0})
        bucket["orders"] += 1
        bucket["notional"] += item["notional"] or 0.0
        if item["slippage_bps"] is not None:
            bucket["slippage_simple"].append(item["slippage_bps"])
            if item["notional"]:
                bucket["slippage_sum"] += item["slippage_bps"] * item["notional"]
                bucket["weight"] += item["notional"]
    points = []
    for day in sorted(buckets):
        bucket = buckets[day]
        if bucket["weight"] > 0:
            slip = bucket["slippage_sum"] / bucket["weight"]
        elif bucket["slippage_simple"]:
            slip = sum(bucket["slippage_simple"]) / len(bucket["slippage_simple"])
        else:
            slip = None
        points.append({
            "t": day,
            "slippageBps": round(slip, 4) if slip is not None else None,
            "notional": round(bucket["notional"], 2),
            "orders": bucket["orders"],
        })

    missing = [SLIPPAGE_NOTE]
    if not filled:
        missing.append("该市场窗口内无成交记录 → 滑点与名义金额不可计算（不填 0 冒充）")
    elif weight_method == "simple-average":
        missing.append("部分成交记录缺名义金额 → 滑点按简单平均（未按金额加权）")
    if counts["unknown"]:
        missing.append(
            f"{counts['unknown']} 笔委托的状态码不在官方发布表内（2/3/4/5/6）→ 未计入任何一类，不猜标签"
        )
    if contradictions:
        missing.append(
            f"{len(contradictions)} 笔委托状态为『全部成交』但累计成交量不足委托量 → 按数量口径保留原值，不悄悄改正"
        )
    if deals is None:
        error = v3_fallback.last_error(deals_attempts)
        missing.append(f"成交流水不可用（{error.get('code')}：{error.get('message')}）→ 笔数与名义金额按委托的累计成交量计算")
    else:
        deals_value = _value_of(deals) or {}
        if deals_value.get("derived"):
            missing.append(f"成交流水由委托派生（derived=true，非券商独立成交流水）：{deals_value.get('note') or ''}".strip())
        deals_rows, _seen, _errs = _rows_for_market(deals, code)
        if not deals_rows:
            missing.append("成交流水在该市场窗口内为空 → 名义金额全部按委托的累计成交量×成交均价计算")

    sources = {
        "orders": (orders_value.get("source") if isinstance(orders_value.get("source"), str) else None)
                  or "futu/orders_history",
        "deals": (_value_of(deals) or {}).get("source") or None,
    }
    if upstream_errors:
        missing.append(
            f"上游按账户回报了 {len(upstream_errors)} 条失败（首条：{upstream_errors[0].get('reason')}）"
        )

    return {
        "ok": True,
        "market": code,
        "mode": (orders_value.get("mode") or wanted_mode),
        "as_of": orders_value.get("as_of") or now_iso(),
        "window": orders_value.get("window"),
        "metrics": {
            "orders": total,
            "filled": counts["filled"],
            "partial": counts["partial"],
            "cancelled": counts["cancelled"],
            "rejected": counts["rejected"],
            "submitted": counts["submitted"],
            "unknownStatus": counts.get("unknown", 0),
            "fillRatePct": round(fill_rate, 4),
            "cancelRatePct": round(cancel_rate, 4),
            "avgSlippageBps": round(avg_slippage, 4) if avg_slippage is not None else None,
            "notional": round(notional_total, 2),
        },
        "definitions": {
            "fillRatePct": "已成交（含部分成交）/ 总委托",
            "cancelRatePct": "已撤 / 总委托",
            "avgSlippageBps": "按名义金额加权的『成交均价 vs 委托价』（正 = 不利）",
            "notional": "累计成交量 × 成交均价（仅本市场，不跨市场/币种合并）",
        },
        "points": points,
        "sources": sources,
        "missing": missing,
        "chain": chain,
    }


def register(app, v3_run, home, deps=None):
    """挂 ``GET /api/v3/execution/quality``。``deps={"timeout":…}`` 仅供测试。"""
    deps = deps or {}

    @app.get("/api/v3/execution/quality")
    async def v3_execution_quality(market: str = "SH", mode: str = "sim"):
        """成交质量（只读 GET）：委托/成交全部来自券商历史，无开源替代。"""
        try:
            return await asyncio.to_thread(execution_quality, v3_run, market, mode,
                                           timeout=deps.get("timeout", v3_fallback.PROBE_CHAIN_TIMEOUT))
        except Exception as error:  # noqa: BLE001 —— 统一信封，不抛 500
            return _error("quality/internal", f"{type(error).__name__}: {error}")

    app.state.v3_quality = {"routes": ("/api/v3/execution/quality",)}
    return execution_quality
