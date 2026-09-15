"""执行编排（规格 §6.2/§七）：逐单 pre_trade_checks → broker.place → 状态机。
被拒订单跳过并落 risk_checks；熔断触发时撤销计划内未提交订单。"""
from datetime import datetime, timedelta, timezone

from . import broker, oms, risk, store

_TZ8 = timezone(timedelta(hours=8))


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


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
    for o in orders:
        order = {"symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
                 "price": o["price"] or price_of(o["symbol"]), "mode": o["mode"],
                 "plan_id": plan_id, "plan_hash": plan_hash,
                 "stop_dist": stop_dist_of(o["symbol"])}
        verdict = risk.pre_trade_checks(order, dict(ctx, plan_hash=plan_hash,
                                                    plan_status="executing"))
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
        out = broker.place(broker_call, acc_id=ctx.get("acc_id", "SIM"),
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
        for o in store.get_open_orders(conn, plan_id):
            if o["status"] in ("draft", "frozen"):
                oms.transition(conn, o["client_order_id"], "cancelled", err="halt")
    return {"submitted": submitted, "blocked": blocked, "halted": halted}
