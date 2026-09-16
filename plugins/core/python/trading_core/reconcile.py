"""对账（规格 §6.3）：数量不一致即差异；价值口径差异 > 容差为差异；
差异 → 上层置 critical 告警 + 暂停执行（本模块只判定，不自动平仓）。

``daily`` 是 WP9 的 reconcile 作业体（规格 §4.4，补齐原 §8.1 一直未入链的
「每日固定对账」）：拉当日券商订单 + 当前持仓 → 与 OMS 台账比对 →
critical 告警 + halt（**只暂停后续执行，绝不自动平仓**）→ TCA → digest 落 kv。

口径与边界（诚实清单）：

* **只做 sim**：live 对账需工作台 OpenAPI 通道（WP13 通道统一时升级），本作业对
  live 输出 info 告警并跳过——不猜、不编造 live 侧结论；
* **状态码不猜语义**：券商订单 ``status`` 是未公开的整数码（TOOL-LIMITS 明示服务端
  未给出枚举含义），因此**只附两方原文值、不判定状态标签**。可由数量推导的一类不一致
  仍然判定：「券商累计成交已足/未足 与 OMS 状态 filled/partial 互相矛盾」——
  它不需要状态码语义，只用成交数量 + OMS 自身状态；
* **持仓口径**：本地台账 = OMS ``fills`` 派生的**累计**净持仓（买 + 卖 −）；
  只有 OMS 有足迹（任何订单或成交）的标的参与差异判定——券商有持仓而 OMS 无任何
  记录的标的记入 ``untracked``，**不计差异**（多半是平台外建的仓，报差异是噪音）；
* **零写操作**：本模块只调用券商只读工具，任何写类（下单/撤单/改单）都不出现。
"""
import datetime as _dt

from . import store

_TZ8 = _dt.timezone(_dt.timedelta(hours=8))

#: OMS 中「应当能在券商侧查到」的状态（真正到过券商的在途/终态）。
#: draft/frozen 未提交、cancelled/rejected 无需券商佐证——都不核对（否则是噪音）。
BROKER_EXPECTED_STATES = ("submitting", "submitted", "partial", "filled", "unknown")

#: 券商只读工具（对账绝无写调用；出现写类即 bug）。
SIM_READ_TOOLS = ("sim_trade_account_list", "sim_trade_position_list",
                  "sim_trade_history_order_list")

#: 券商订单行 side 码 → 仓库方向（TOOL-LIMITS 实测：1=Buy 2=Sell）。
SIDE_BY_CODE = {1: "BUY", 2: "SELL"}


def _now():
    return _dt.datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def _int_of(value):
    """券商数量字段可能是字符串（TOOL-LIMITS 实测 "200"）；非法/缺失 → None。"""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


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


def local_net_positions(conn):
    """OMS ``fills`` 派生的净持仓（累计口径：买方 +、卖方 −）。"""
    rows = conn.execute(
        "SELECT o.symbol AS symbol, o.side AS side, f.qty AS qty"
        " FROM fills f JOIN orders o ON o.client_order_id=f.client_order_id").fetchall()
    net = {}
    for row in rows:
        sign = 1 if str(row["side"]).upper() == "BUY" else -1
        net[row["symbol"]] = net.get(row["symbol"], 0) + sign * int(row["qty"])
    return {s: q for s, q in net.items() if q != 0}


def footprint_symbols(conn):
    """OMS 有足迹的标的（任何订单或成交记录）。

    用**累计**口径而非「当日增量」：本地台账（fills 净持仓）本就是累计的，拿当日足迹
    去比会把昨日建仓的标的整个排除在核对之外，只减少噪音却漏掉真实差异。
    """
    rows = conn.execute(
        "SELECT symbol FROM orders"
        " UNION SELECT o.symbol AS symbol FROM fills f"
        " JOIN orders o ON o.client_order_id=f.client_order_id").fetchall()
    return {row["symbol"] for row in rows}


def order_status_counts(conn, today):
    """当日 OMS 订单按状态计数（digest 用）。"""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM orders WHERE created_at LIKE ? GROUP BY status",
        (f"{today}%",)).fetchall()
    return {row["status"]: row["n"] for row in rows}


def _broker_order_rows(rows, normalize):
    """券商订单行 → 归一结构（数量转 int；标的走 core 唯一实现）。"""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        side_code = _int_of(row.get("side"))
        out.append({
            "broker_order_id": str(row.get("order_id") or row.get("id") or "").strip(),
            "symbol": normalize(row.get("symbol") or row.get("code")),
            "side": SIDE_BY_CODE.get(side_code, ""),
            "qty": _int_of(row.get("qty")),
            "cum_qty": _int_of(row.get("cum_qty")),
            "status_raw": row.get("status"),
        })
    return out


def _pair_diffs(oms, broker_row, match):
    """两侧同一订单的字段比对（只判可由数量推导的不一致）。"""
    diffs = []
    base = {"symbol": oms["symbol"], "client_order_id": oms["client_order_id"],
            "broker_order_id": broker_row["broker_order_id"], "match": match,
            "broker_status_raw": broker_row["status_raw"], "oms_status": oms["status"],
            "kind": "status_or_qty_diff"}
    oms_qty = _int_of(oms["qty"])
    if broker_row["qty"] is not None and oms_qty is not None and broker_row["qty"] != oms_qty:
        diffs.append({**base, "qty_diff": broker_row["qty"] - oms_qty,
                      "broker_qty": broker_row["qty"], "oms_qty": oms_qty,
                      "reason": "委托数量不一致"})
    # 成交数量的自相矛盾（不需要状态码语义）：券商累计成交已足/未足 vs OMS 是否记成交
    cum, qty, oms_status = broker_row["cum_qty"], broker_row["qty"], oms["status"]
    if cum is not None and qty:
        if cum >= qty and oms_status not in ("filled", "partial"):
            diffs.append({**base, "broker_cum_qty": cum, "oms_cum_qty": 0,
                          "reason": "券商累计成交已足额但 OMS 未记成交"})
        elif oms_status == "filled" and cum < qty:
            diffs.append({**base, "broker_cum_qty": cum, "oms_cum_qty": qty,
                          "reason": "OMS 记已成交但券商累计成交不足"})
    return diffs


def _order_diffs(conn, today, broker_rows):
    """订单级对账：missing_at_broker / missing_in_oms / status_or_qty_diff 三类。

    匹配分三遍（顺序是正确性要求，不是风格）：**先按券商编号强匹配**，再对剩下的本地单
    按 (标的, 方向, 委托数量) 弱匹配，最后把未认领的券商行记为 missing_in_oms。
    若弱匹配先跑，一张编号未知的本地单会「抢走」另一张本地单编号对应的券商行，
    造成一侧漏报、另一侧误报。
    """
    oms_rows = [dict(row) for row in conn.execute(
        "SELECT * FROM orders WHERE created_at LIKE ? ORDER BY rowid",
        (f"{today}%",)).fetchall()]
    by_id = {}
    for idx, row in enumerate(broker_rows):
        if row["broker_order_id"]:
            by_id.setdefault(row["broker_order_id"], idx)
    used, diffs, pending = set(), [], []

    for oms in oms_rows:
        bid = str(oms.get("broker_order_id") or "").strip()
        idx = by_id.get(bid) if bid else None
        if idx is not None and idx not in used:
            used.add(idx)
            diffs.extend(_pair_diffs(oms, broker_rows[idx], "strong"))
        else:
            pending.append(oms)

    for oms in pending:
        if oms.get("status") not in BROKER_EXPECTED_STATES:
            continue      # 未提交/已作废的本地单无需券商佐证
        bid = str(oms.get("broker_order_id") or "").strip()
        oms_qty = _int_of(oms.get("qty"))
        cand = next((i for i, r in enumerate(broker_rows) if i not in used
                     and r["symbol"] == oms["symbol"] and r["side"] == oms["side"]
                     and r["qty"] is not None and r["qty"] == oms_qty), None)
        if cand is None:
            diffs.append({"symbol": oms["symbol"], "client_order_id": oms["client_order_id"],
                          "broker_order_id": bid or None, "kind": "missing_at_broker",
                          "match": "id" if bid else "weak", "oms_status": oms["status"],
                          "oms_qty": oms_qty})
            continue
        used.add(cand)
        diffs.extend(_pair_diffs(oms, broker_rows[cand], "weak"))

    for idx, row in enumerate(broker_rows):
        if idx in used:
            continue
        diffs.append({"symbol": row["symbol"], "broker_order_id": row["broker_order_id"],
                      "side": row["side"], "qty": row["qty"],
                      "broker_status_raw": row["status_raw"], "kind": "missing_in_oms"})
    return diffs


def _sim_broker_state(call, today):
    """一次券商只读扫描：各市场账户 → 订单（当日窗口）+ 当前持仓。

    订单窗口固定为当日：该工具不带 start/end 会静默返回 no data（TOOL-LIMITS 实测），
    日期文本格式沿用 platform/server/trading.py 的 ISO 口径。
    """
    from . import broker as core_broker

    accounts = [a for a in ((core_broker.accounts(call) or {}).get("accounts") or [])
                if isinstance(a, dict) and a.get("account_id")]
    orders, positions = [], {}
    for account in accounts:
        acc_id = str(account["account_id"])
        market_id = account.get("market_id")
        rows = call(core_broker.TOOLS["history"],
                    {"acc_id": acc_id, "start": today, "end": today}, timeout=30) or {}
        orders.extend((rows.get("orders") if isinstance(rows, dict) else rows) or [])
        if market_id is None:
            continue
        data = call(core_broker.TOOLS["positions"],
                    {"acc_id": acc_id, "market": market_id}, timeout=30) or {}
        # 键名二义（TOOL-LIMITS）：positions 与 position_list 两种都实测出现过
        for row in (data.get("positions") or data.get("position_list") or []):
            if not isinstance(row, dict):
                continue
            symbol = core_broker.normalize_symbol(row.get("symbol") or row.get("code"))
            qty = _int_of(row.get("qty"))
            if not symbol or qty is None:
                continue
            positions[symbol] = {"qty": positions.get(symbol, {}).get("qty", 0) + qty}
    return orders, positions


def daily(conn, home, mode=None, today=None, broker_call=None, now=None):
    """reconcile 作业体（规格 §4.4）：对账 → TCA → digest。

    返回契约（**永不抛**——作业失败不拖垮调度链，原因以信封返回并落告警）::

      {"ok": True,  "skipped": <原因>}                       软跳过（live/无账户）
      {"ok": False, "error": <原因>}                         通道/模式失败（fail-closed）
      {"ok": True,  "diffs": [...], "untracked": [...],
       "orders": {...}, "tca": {...}, "digest": {...}, "halted": <bool>}   对账完成

    通道故障一律 fail-closed：**不写任何 kv**（既不写差异也不伪造「无差异」）。
    """
    from . import alerts, planner, tca

    home = str(home)
    today = today or _dt.datetime.now(_TZ8).date().isoformat()
    stamp = (now or _now)()

    if mode is None:
        try:
            mode = planner.read_mode(home)
        except ValueError as error:
            return {"ok": False, "error": str(error)}

    if mode != "sim":
        reason = f"{mode} 对账经工作台端点（本作业本期只对 sim 通道）"
        alerts.emit(conn, home=home, level="info", title="对账跳过", detail=reason[:160])
        return {"ok": True, "skipped": reason}

    if broker_call is None:
        try:
            from trading_datasource.futu_mcp import call_tool
            broker_call = call_tool
        except Exception as error:  # noqa: BLE001 —— 导入失败即通道不可用
            reason = f"券商通道不可用：{str(error)[:120]}"
            alerts.emit(conn, home=home, level="warn", title="对账通道不可用",
                        detail=reason[:160])
            return {"ok": False, "error": reason}

    from . import broker as core_broker

    try:
        orders_raw, broker_positions = _sim_broker_state(broker_call, today)
    except Exception as error:  # noqa: BLE001 —— 通道失败 fail-closed，绝不写「无差异」
        reason = f"券商通道不可用：{str(error)[:120]}"
        alerts.emit(conn, home=home, level="warn", title="对账通道不可用",
                    detail=reason[:160])
        return {"ok": False, "error": reason}

    broker_rows = _broker_order_rows(orders_raw, core_broker.normalize_symbol)
    diffs = _order_diffs(conn, today, broker_rows)

    # 持仓级：本地 = fills 派生净持仓；只核对 OMS 有足迹的标的
    footprint = footprint_symbols(conn)
    local = {s: {"qty": q} for s, q in local_net_positions(conn).items() if s in footprint}
    broker_subset = {s: v for s, v in broker_positions.items() if s in footprint}
    diffs.extend(compare(conn, local, broker_subset))
    untracked = sorted(set(broker_positions) - footprint)

    halted = bool(diffs)
    if halted:
        alerts.emit(conn, home=home, level="critical", title="对账差异",
                    detail=f"{len(diffs)} 条差异（{today}）："
                           + ",".join(sorted({str(d.get('symbol')) for d in diffs}))[:120])
        store.set_halt(conn, True, reason="reconcile_diff")
    else:
        alerts.emit(conn, home=home, level="info", title="对账无差异",
                    detail=f"{today} 订单/持仓与券商一致")

    tca_summary = tca.aggregate(conn)
    digest = {"as_of": today, "mode": mode, "orders": order_status_counts(conn, today),
              "diffs": len(diffs), "untracked": len(untracked), "tca": tca_summary,
              "at": stamp}
    # snapshot-reconcile 的既有取数口径（diffs/at）+ 本任务新增 untracked/mode
    store.kv_set(conn, "reconcile:latest",
                 {"diffs": diffs, "untracked": untracked, "mode": mode, "at": stamp})
    store.kv_set(conn, "daily:digest", digest)
    return {"ok": True, "diffs": diffs, "untracked": untracked,
            "orders": digest["orders"], "tca": tca_summary, "digest": digest,
            "halted": halted}
