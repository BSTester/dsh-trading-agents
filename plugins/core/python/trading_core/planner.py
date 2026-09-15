"""计划生成与冻结（规格 §6.1）：diff 只生成必要的整手订单；冻结后不可变。"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

_TZ8 = timezone(timedelta(hours=8))


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def build_and_freeze(conn, mode, strategy_id, target, broker_positions, prices,
                     as_of, lot=100):
    positions, equity = broker_positions(mode)
    orders, plan_id = [], f"PLN-{as_of.replace('-', '')}-{mode}-{uuid.uuid4().hex[:4].upper()}"
    for symbol, weight in target.items():
        px = prices.get(symbol)
        if not px:
            continue  # 无价（停牌/无行情）：跳过并在审计可见，不猜价
        want_qty = int(equity * weight / px // lot * lot)
        have_qty = positions.get(symbol, {}).get("qty", 0)
        delta = want_qty - have_qty
        if delta == 0:
            continue
        orders.append({"symbol": symbol, "market": symbol.split(".")[0],
                       "side": "BUY" if delta > 0 else "SELL", "qty": abs(delta),
                       "price": px})
    content = json.dumps({"plan_id": plan_id, "as_of": as_of, "mode": mode,
                          "strategy_id": strategy_id, "target": target,
                          "orders": orders}, sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
                 "status,created_at) VALUES(?,?,?,?,?,?,'frozen',?)",
                 (plan_id, as_of, mode, strategy_id, json.dumps(target, ensure_ascii=False),
                  content_hash, _now()))
    conn.commit()
    return {"plan_id": plan_id, "as_of": as_of, "mode": mode, "status": "frozen",
            "orders": orders, "content_hash": content_hash}
