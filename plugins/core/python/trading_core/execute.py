"""执行编排（规格 §6.2/§七）：逐单 pre_trade_checks → broker.place → 状态机。
被拒订单跳过并落 risk_checks；熔断触发时撤销计划内未提交订单。"""
from datetime import datetime, timedelta, timezone

from . import broker, oms, risk, store

_TZ8 = timezone(timedelta(hours=8))


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def _order_ctx(ctx, order, plan_hash):
    """逐单 ctx：卖出方向把持仓市值折算成规则 5 能算出**真实成交后市值**的口径。

    规则 5 的判定式是 ``持仓市值 + 本单名义``（= 成交后单票市值），该式只在**买入**
    方向成立。卖出方向上它把减仓当成新建仓：卖 1700 股 × 200 元时给出
    ``持仓市值 + 340,000``——持仓 34 万（> 权益×25%）的退出单会被判成「成交后 68 万」
    而拒，正是「自动流水线能加仓、不能减仓」的根因。``risk.py`` 在本 WP 冻结
    （硬拦截语义不动），因此折算落在调用侧：

        成交后剩余市值 = max(0, 持仓市值 − 本单名义)
        ctx 持仓市值  = 成交后剩余市值 − 本单名义     # 规则会再加回一个本单名义
        → 规则 5 的结果 = 成交后剩余市值（真实语义：清仓为 0、减仓为剩余）

    **只在券商持仓可得时折算**（``ctx_source == "broker"`` 且该标的在持仓表内）：
    持仓未知时保持既有离线保守口径——规则 5 按「本单全额名义」判定，退出大仓位会被
    拒（宁可拒绝，也不用未知持仓放宽硬规则）。折算不改动规则本身，也不改变
    ``positions_value`` 的键集合：规则 6 的「该标的是否已持仓」判定照常成立。
    """
    order_ctx = dict(ctx, plan_hash=plan_hash, plan_status="executing")
    if order["side"] != "SELL" or ctx.get("ctx_source") != "broker":
        return order_ctx
    held = ctx.get("positions_value", {}).get(order["symbol"])
    if held is None:
        return order_ctx
    notional = order["qty"] * order["price"]
    remaining = max(0.0, float(held) - notional)
    order_ctx["positions_value"] = dict(ctx["positions_value"],
                                        **{order["symbol"]: remaining - notional})
    return order_ctx


def _order_account(ctx, broker_call, market, cache):
    """下单账户：``ctx["acc_id"]`` 显式指定优先，否则按市场解析真实模拟账户。

    为什么必须有这一步（2026-09-17 实机缺陷）：旧实现在这里下发 ``"SIM"`` 占位符。
    MCP 通道下上游忽略它，openapi 通道下 REST 把它拼进 URL
    （``POST /api/v1.0/sim-trade/SIM/orders``）→ 券商 ``-3 invalid parameter``——
    自动执行链的订单**从未到达模拟账户**。解析口径见 ``broker.sim_account_for_market``；
    解析不到（未知市场/账户表不可用）保留占位符，由券商如实报错——**不猜账户，
    也不静默丢弃订单**。``cache`` 按市场缓存（逐单查账户表会放大通道往返）。
    """
    explicit = ctx.get("acc_id")
    if explicit:
        return explicit
    if market not in cache:
        try:
            cache[market] = broker.sim_account_for_market(broker_call, market) or None
        except Exception:  # noqa: BLE001 —— 账户表不可用不改变既有行为（照旧尝试下单）
            cache[market] = None
    return cache[market] or broker.PLACEHOLDER_ACCOUNT


def run(conn, plan_id, plan_hash, ctx, broker_call, price_of, stop_dist_of):
    plan = store.get_plan(conn, plan_id)
    if plan["status"] not in ("frozen", "approved") or plan["content_hash"] != plan_hash:
        raise ValueError("计划未冻结或 hash 不匹配（规则 8 前置校验）")
    store.upsert_plan_status(conn, plan_id, "executing")
    orders = store.get_orders_by_plan(conn, plan_id)
    # 规格 §6.2：draft → frozen → submitting。计划入口已校验 frozen+hash，
    # 在途 draft 单在此统一升到 frozen（订单归属已冻结计划的登记动作）。
    for o in orders:
        if o["status"] == "draft":
            oms.transition(conn, o["client_order_id"], "frozen")
    blocked = submitted = 0
    halted = False
    accounts = {}
    for o in orders:
        order = {"symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
                 "price": o["price"] or price_of(o["symbol"]), "mode": o["mode"],
                 "plan_id": plan_id, "plan_hash": plan_hash,
                 "stop_dist": stop_dist_of(o["symbol"])}
        verdict = risk.pre_trade_checks(order, _order_ctx(ctx, order, plan_hash))
        store.insert_risk_check(conn, plan_id, o["symbol"], verdict.rule,
                                verdict.allowed, verdict.reason)
        if not verdict.allowed:
            blocked += 1
            if verdict.rule == 7:
                halted = True
            oms.transition(conn, o["client_order_id"], "cancelled",
                           err="risk:" + verdict.reason)
            continue
        oms.transition(conn, o["client_order_id"], "submitting")
        out = broker.place(broker_call,
                           acc_id=_order_account(ctx, broker_call, o["market"], accounts),
                           market=o["market"], symbol=o["symbol"].split(".")[-1],
                           side=o["side"], qty=o["qty"], price=o["price"])
        if out["status"] == "submitted":
            oms.transition(conn, o["client_order_id"], "submitted",
                           broker_order_id=out["broker_order_id"])
            submitted += 1
        else:
            oms.transition(conn, o["client_order_id"], out["status"], err=out.get("err"))
            if out["status"] == "rejected":
                store.kv_set(conn, "last_reject", {"order": o["client_order_id"],
                                                   "err": out.get("err")})
    if halted:
        store.set_halt(conn, True, reason="daily_loss")
        # 撤余单经 oms.cancel_pending（唯一实现，2026-09-16 修订 I2）：本处的
        # draft/frozen 撤销与 cancel_plan / 计划过期为同一口径，三处共用一份实现。
        oms.cancel_pending(conn, plan_id, err="halt")
    return {"submitted": submitted, "blocked": blocked, "halted": halted}
