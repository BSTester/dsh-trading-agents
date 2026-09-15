"""OMS（规格 §6.2）：状态机白名单 + 幂等三件套 + unknown 只查不重放。

白名单相对规格图的两点补充（WP3 验收记录披露 D2）：
- draft/frozen → cancelled：规格规则 7「日内熔断 → 撤计划内剩余订单」要求
  未提交订单可本地撤销（不涉及券商调用）；
- unknown 仅可从 submitting 进入（提交超时/异常），迁出仅凭查询结果。
"""
import uuid
from datetime import datetime, timedelta, timezone

_TZ8 = timezone(timedelta(hours=8))

STATES = {"draft", "frozen", "submitting", "submitted", "partial", "filled",
          "cancelled", "rejected", "unknown"}
TRANSITIONS = {
    "draft": {"frozen", "cancelled"},
    "frozen": {"submitting", "cancelled"},
    "submitting": {"submitted", "unknown", "rejected"},
    "submitted": {"partial", "filled", "cancelled"},
    "partial": {"partial", "filled", "cancelled"},
    "unknown": {"submitted", "partial", "filled", "cancelled"},  # 仅查询结果可迁出
}
OPEN_STATES = {"draft", "frozen", "submitting", "submitted", "partial", "unknown"}


class DuplicateOpenOrder(Exception):
    pass


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def register_order(conn, plan_id, symbol, market, side, qty, price, mode, plan_hash):
    row = conn.execute(
        "SELECT 1 FROM orders WHERE plan_id=? AND symbol=? AND side=? AND status IN "
        f"({','.join('?' * len(OPEN_STATES))})",
        (plan_id, symbol, side, *OPEN_STATES)).fetchone()
    if row:
        raise DuplicateOpenOrder(f"{symbol} {side} 已有在途单（幂等三件套之二）")
    cid = uuid.uuid4().hex
    now = _now()
    conn.execute("INSERT INTO orders(client_order_id,plan_id,symbol,market,side,qty,price,"
                 "status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, plan_id, symbol, market, side, qty, price, "draft", mode, now, now))
    conn.commit()
    return {"client_order_id": cid, "status": "draft"}


def transition(conn, client_order_id, to_state, broker_order_id=None, err=None):
    if to_state not in STATES:
        raise ValueError(f"未知状态 {to_state}")
    row = conn.execute("SELECT status FROM orders WHERE client_order_id=?",
                       (client_order_id,)).fetchone()
    if not row:
        raise ValueError(f"订单不存在 {client_order_id}")
    if to_state not in TRANSITIONS.get(row["status"], set()):
        raise ValueError(f"非法迁移 {row['status']} → {to_state}")
    conn.execute("UPDATE orders SET status=?, broker_order_id=COALESCE(?,broker_order_id),"
                 " err=COALESCE(?,err), updated_at=? WHERE client_order_id=?",
                 (to_state, broker_order_id, err, _now(), client_order_id))
    conn.commit()
