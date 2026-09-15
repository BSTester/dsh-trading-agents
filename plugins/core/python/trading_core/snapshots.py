"""只读快照（WP4）：Host 工作台经 `python -m trading_core snapshot-*` 取数的
唯一通道（Node 不直读 SQLite）。三个函数都只做只读查询，输出 JSON 可序列化。"""
import json

from . import store

_PLAN_COLUMNS = ("plan_id, as_of, mode, strategy_id, target, content_hash, status, created_at")


def _verdict_of(conn, plan_id, symbol):
    """该订单最新一次预检的中文事实：放行 / 规则N拦截（理由）；未预检返回 None。"""
    row = conn.execute(
        "SELECT allowed, rule, reason FROM risk_checks"
        " WHERE plan_id=? AND symbol=? ORDER BY id DESC LIMIT 1",
        (plan_id, symbol)).fetchone()
    if row is None:
        return None
    if row["allowed"]:
        return "预检通过"
    return f"规则{row['rule']}拦截：{row['reason']}"


def _orders_of(conn, plan_id, with_fills=False):
    orders = []
    for o in store.get_orders_by_plan(conn, plan_id):
        order = {"client_order_id": o["client_order_id"], "symbol": o["symbol"],
                 "side": o["side"], "qty": o["qty"], "price": o["price"],
                 "status": o["status"], "broker_order_id": o["broker_order_id"],
                 "risk_verdict": _verdict_of(conn, plan_id, o["symbol"])}
        if with_fills:
            order["fills"] = [{"price": f["price"], "qty": f["qty"], "traded_at": f["traded_at"]}
                              for f in store.fills_by_order(conn, o["client_order_id"])]
        orders.append(order)
    return orders


def plan_snapshot(conn, alert_limit=10):
    from . import alerts
    plans = []
    for p in store.list_plans(conn):
        plans.append({"plan_id": p["plan_id"], "as_of": p["as_of"], "mode": p["mode"],
                      "strategy_id": p["strategy_id"], "target": p["target"],
                      "content_hash": p["content_hash"], "status": p["status"],
                      "created_at": p["created_at"],
                      "orders": _orders_of(conn, p["plan_id"])})
    return {"plans": plans, "alerts": alerts.list_recent(conn, limit=alert_limit)}


def schedule_snapshot(conn, home):
    from pathlib import Path
    from . import daemon
    home = Path(home)
    hb_path = daemon.heartbeat_path(home)
    heartbeat = {}
    if hb_path.exists():
        try:
            heartbeat = json.loads(hb_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            heartbeat = {}  # 心跳文件损坏按无心跳处理，界面显示为失联
    state = store.kv_get(conn, "daemon:state", default={"ran": {}}) or {"ran": {}}
    jobs = sorted(({"job": k, "ran": v} for k, v in state.get("ran", {}).items()),
                  key=lambda row: row["ran"], reverse=True)
    return {"heartbeat": heartbeat,
            "critical": bool(heartbeat.get("critical")),
            "critical_title": heartbeat.get("critical_title"),
            "kill": daemon.kill_path(home).exists(),
            "halt": store.is_halted(conn),
            "jobs": jobs}


def reconcile_snapshot(conn, chain_limit=5):
    from . import tca
    latest = store.kv_get(conn, "reconcile:latest", default={"diffs": [], "at": None}) or {}
    diffs = latest.get("diffs") or []
    try:
        rows = conn.execute(
            "SELECT client_order_id, symbol, day, arrival, filled, side, bps"
            " FROM tca ORDER BY created_at DESC LIMIT 50").fetchall()
        tca_rows = [{"client_order_id": r["client_order_id"], "symbol": r["symbol"],
                     "day": r["day"], "arrival": r["arrival"], "filled": r["filled"],
                     "side": r["side"], "bps": r["bps"]} for r in rows]
    except Exception:  # tca 表尚未创建（无执行记录）——空表口径，不报错
        tca_rows = []
    avg_bps = (sum(r["bps"] for r in tca_rows) / len(tca_rows)) if tca_rows else None
    chain = []
    for p in store.list_plans(conn)[::-1][:chain_limit]:
        chain.append({"plan_id": p["plan_id"], "mode": p["mode"], "status": p["status"],
                      "created_at": p["created_at"],
                      "orders": _orders_of(conn, p["plan_id"], with_fills=True)})
    return {"diffs": diffs, "diffs_at": latest.get("at"),
            "tca": {"rows": tca_rows, "avg_bps": avg_bps}, "chain": chain}
