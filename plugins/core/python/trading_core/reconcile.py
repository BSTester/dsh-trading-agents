"""对账（规格 §6.3）：数量不一致即差异；价值口径差异 > 容差为差异；
差异 → 上层置 critical 告警 + 暂停执行（本模块只判定，不自动平仓）。"""


def compare(conn, local_positions, broker_positions, value_tolerance=0.005):
    symbols = set(local_positions) | set(broker_positions)
    diffs = []
    for s in sorted(symbols):
        l = local_positions.get(s)
        b = broker_positions.get(s)
        if l is None or b is None:
            diffs.append({"symbol": s, "local": l, "broker": b, "kind": "missing_side"})
            continue
        lq = int(l["qty"] if isinstance(l, dict) else l)
        bq = int(b["qty"] if isinstance(b, dict) else b)
        if lq != bq:
            diffs.append({"symbol": s, "local": lq, "broker": bq, "qty_diff": lq - bq,
                          "kind": "qty"})
        else:
            lv = float(l.get("value", 0) or 0) if isinstance(l, dict) else 0.0
            bv = float(b.get("value", 0) or 0) if isinstance(b, dict) else 0.0
            if lv and bv and abs(lv - bv) > value_tolerance * max(lv, bv):
                diffs.append({"symbol": s, "local_value": lv, "broker_value": bv,
                              "kind": "value"})
    return diffs
