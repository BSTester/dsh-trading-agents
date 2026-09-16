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


def _plans_newest_first(conn):
    """端点取数口径 = 最新在前（store.list_plans 仍按 created_at, plan_id 升序——
    通用访问器语义不变，两处口径差异只在快照端点边界归一）。plan 与 reconcile 的 chain、
    以及 Web「当前计划」= plans[0] 都依赖这一口径：否则多计划并存时会取到最旧计划。

    实现委托 ``store.list_plans_newest_first``（WP10 起流程快照也要同一口径——
    单一实现，不在两个快照模块里各写一遍）。"""
    return store.list_plans_newest_first(conn)


def plan_snapshot(conn, alert_limit=10):
    from . import alerts
    plans = []
    for p in _plans_newest_first(conn):
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


def reconcile_snapshot(conn, chain_limit=5, alert_limit=20):
    from . import alerts
    from . import tca
    latest = store.kv_get(conn, "reconcile:latest", default={"diffs": [], "at": None}) or {}
    diffs = latest.get("diffs") or []
    # WP8 任务 4：推送重连触发的一次对账（WS 事件不补发 → on_reconnect 走 REST 补齐）。
    # 键缺失/坏数据按 None 处理（老库无此键时行为与之前逐字一致）。
    push = store.kv_get(conn, "reconcile:push", default=None)
    if not isinstance(push, dict):
        push = None
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
    for p in _plans_newest_first(conn)[:chain_limit]:
        chain.append({"plan_id": p["plan_id"], "mode": p["mode"], "status": p["status"],
                      "created_at": p["created_at"],
                      "orders": _orders_of(conn, p["plan_id"], with_fills=True)})
    # 规格 §8.3：reconcile 端点输出包含告警列表（调度页展示）
    return {"diffs": diffs, "diffs_at": latest.get("at"),
            "tca": {"rows": tca_rows, "avg_bps": avg_bps},
            "alerts": alerts.list_recent(conn, limit=alert_limit), "chain": chain,
            "push": push}
