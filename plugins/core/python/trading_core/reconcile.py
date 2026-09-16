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
* **先同步券商事实，再判差异**（修复 sim 系统性假差异与两条自锁熔断路径）：sim 通道的
  成交**不经** WS 交易事件通道（``platform/server/trading.py::_record_fill`` 只在推送
  路径写 ``fills``），因此本地台账在 sim 常态下既没有成交、订单状态也停在 ``submitted``。
  若对账只做纯比对，会把「本地尚未学习」系统性误报为差异 → 每日 critical + ``set_halt``
  → **自动执行被自己的对账噪声永久熔断**（模拟盘全自动的前提因此不成立）。同步分两步，
  都在比对之前完成，且**只读券商、只写本地**：

  1. **成交回填**（``_backfill_fills``，消除持仓级假差异）：按券商订单历史的累计成交量与
     均价补记 ``fills`` 差额，让本地净持仓真的等于券商事实——回填只认券商订单历史，
     历史存量持仓仍是 ``untracked``；
  2. **状态推进**（``_advance_order_states``，消除订单级假差异）：按券商累计成交量推进
     OMS 订单状态（足额→``filled``、部分→``partial``），让订单级判定看到同步后的状态
     ——否则「券商累计成交已足额但 OMS 未记成交」在 sim 下必然触发。

  同步后仍存在的差异才是真差异（critical + halt）。两步都不向券商写、都不重放。
* **零写操作**：本模块只调用券商只读工具，任何写类（下单/撤单/改单）都不出现。
"""
import datetime as _dt
import hashlib

from . import oms, store

_TZ8 = _dt.timezone(_dt.timedelta(hours=8))

#: OMS 中「应当能在券商侧查到」的状态（真正到过券商的在途/终态）。
#: draft/frozen 未提交、cancelled/rejected 无需券商佐证——都不核对（否则是噪音）。
BROKER_EXPECTED_STATES = ("submitting", "submitted", "partial", "filled", "unknown")

#: 券商只读工具（对账绝无写调用；出现写类即 bug）。
SIM_READ_TOOLS = ("sim_trade_account_list", "sim_trade_position_list",
                  "sim_trade_history_order_list")

#: 券商订单行 side 码 → 仓库方向（TOOL-LIMITS 实测：1=Buy 2=Sell）。
SIDE_BY_CODE = {1: "BUY", 2: "SELL"}


def _int_of(value):
    """券商数量字段可能是字符串（TOOL-LIMITS 实测 "200"）；非法/缺失 → None。"""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float_of(value):
    """券商价格字段同样的字符串口径（TOOL-LIMITS 实测 "10.0"）；非法/缺失 → None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
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
    """券商订单行 → 归一结构（数量转 int、价格转 float；标的走 core 唯一实现）。"""
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
            "price": _float_of(row.get("price")),
            "avg_fill_price": _float_of(row.get("avg_fill_price")),
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


def _match_orders(conn, today, broker_rows):
    """本地当日订单 ↔ 券商订单行匹配，返回 ``(pairs, unmatched_oms, unmatched_broker)``。

    ``pairs`` 元素为 ``(oms_row, broker_row, match)``；``match`` ∈ {"strong", "weak"}。

    匹配分三遍（顺序是正确性要求，不是风格）：**先按券商编号强匹配**，再对剩下的本地单
    按 (标的, 方向, 委托数量) 弱匹配，最后把未认领的券商行记为 missing_in_oms。
    若弱匹配先跑，一张编号未知的本地单会「抢走」另一张本地单编号对应的券商行，
    造成一侧漏报、另一侧误报。

    成交回填（``_backfill_fills``）与订单级差异（``_order_diffs``）**必须共用这里的匹配
    结果**：两处各写一份匹配迟早会错位，回填就会把钱记到别的订单上。
    """
    oms_rows = [dict(row) for row in conn.execute(
        "SELECT * FROM orders WHERE created_at LIKE ? ORDER BY rowid",
        (f"{today}%",)).fetchall()]
    by_id = {}
    for idx, row in enumerate(broker_rows):
        if row["broker_order_id"]:
            by_id.setdefault(row["broker_order_id"], idx)
    used, pairs, pending = set(), [], []

    for oms in oms_rows:
        bid = str(oms.get("broker_order_id") or "").strip()
        idx = by_id.get(bid) if bid else None
        if idx is not None and idx not in used:
            used.add(idx)
            pairs.append((oms, broker_rows[idx], "strong"))
        else:
            pending.append(oms)

    unmatched_oms = []
    for oms in pending:
        if oms.get("status") not in BROKER_EXPECTED_STATES:
            continue      # 未提交/已作废的本地单无需券商佐证
        oms_qty = _int_of(oms.get("qty"))
        cand = next((i for i, r in enumerate(broker_rows) if i not in used
                     and r["symbol"] == oms["symbol"] and r["side"] == oms["side"]
                     and r["qty"] is not None and r["qty"] == oms_qty), None)
        if cand is None:
            unmatched_oms.append(oms)
            continue
        used.add(cand)
        pairs.append((oms, broker_rows[cand], "weak"))

    unmatched_broker = [r for i, r in enumerate(broker_rows) if i not in used]
    return pairs, unmatched_oms, unmatched_broker


def _order_diffs(conn, today, broker_rows, matched=None):
    """订单级对账：missing_at_broker / missing_in_oms / status_or_qty_diff 三类。

    ``matched`` 由调用方传入时复用（``daily`` 与成交回填共用一次匹配）。
    """
    pairs, unmatched_oms, unmatched_broker = (
        matched if matched is not None else _match_orders(conn, today, broker_rows))

    diffs = []
    for oms, broker_row, match in pairs:
        diffs.extend(_pair_diffs(oms, broker_row, match))

    for oms in unmatched_oms:
        bid = str(oms.get("broker_order_id") or "").strip()
        diffs.append({"symbol": oms["symbol"], "client_order_id": oms["client_order_id"],
                      "broker_order_id": bid or None, "kind": "missing_at_broker",
                      "match": "id" if bid else "weak", "oms_status": oms["status"],
                      "oms_qty": _int_of(oms.get("qty"))})

    for row in unmatched_broker:
        diffs.append({"symbol": row["symbol"], "broker_order_id": row["broker_order_id"],
                      "side": row["side"], "qty": row["qty"],
                      "broker_status_raw": row["status_raw"], "kind": "missing_in_oms"})
    return diffs


def _fill_fingerprint(broker_order_id, cum_qty, price):
    """回填成交的确定性指纹：同一累计成交状态重复运行不会重复落库。

    前缀 ``bf-`` 让回填来源在 ``fills.fill_id`` 上可辨认（审计时一眼分得清
    「WS 逐笔成交」与「对账聚合成交」）。
    """
    raw = f"{broker_order_id}|{cum_qty}|{price}"
    return "bf-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _backfill_fills(conn, pairs):
    """对账前按券商订单历史回填 ``fills``（让本地台账等于券商事实）。

    为什么必须回填：sim 通道成交不经 WS 交易事件通道，``fills`` 基本为空 → 本地净持仓
    恒为 0 → 有 OMS 足迹的持仓全部报 ``qty_diff`` → critical + halt → 自动执行被自己的
    对账噪声永久熔断（模拟盘全自动的前提因此不成立）。

    口径与边界（诚实清单）：

    * **只补差额**：回填量 = ``cum_qty − 本地既有 fills 合计``。既有 WS 逐笔成交不被
      重复计数；重复运行差额为 0 即跳过（幂等）；累计成交增长时按增量补记；
    * **聚合成交，非逐笔**：券商订单历史没有逐笔明细，因此一条 fill 聚合该单的增量成交
      （qty=差额，price=成交均价）。**数量维度准确**（累计成交数量是券商原文），故本模块
      的数量判据成立；成本/TCA 口径应按「聚合」理解，结果里如实标注；
    * **价格缺失不回填**：均价不可得 → 跳过并计数（``skipped``），绝不拿委托价或
      任何编造值充当成交价——缺口以 ``qty_diff`` 如实暴露（宁缺毋假）；
    * **不处理成交修正**：``cum_qty`` 回落（券商修正）时不回删既有 fills，只跳过——
      绝不猜券商的修正意图；
    * **零券商写操作**：只读订单历史（本函数不调用券商通道，数据由调用方传入）。
    """
    inserted, skipped = [], []
    for oms, broker_row, _match in pairs:
        cum = broker_row["cum_qty"]
        if not cum or cum <= 0:
            continue      # 未成交部分不回填
        cid = oms["client_order_id"]
        existing = sum(int(r["qty"]) for r in store.fills_by_order(conn, cid))
        delta = int(cum) - existing
        if delta <= 0:
            continue      # 已记满（或券商修正回落）：不重复计数、不回删
        price = broker_row["avg_fill_price"]
        if price is None or price <= 0:
            skipped.append({"client_order_id": cid,
                            "broker_order_id": broker_row["broker_order_id"],
                            "cum_qty": int(cum), "reason": "券商未给成交均价"})
            continue
        fill_id = _fill_fingerprint(
            broker_row["broker_order_id"] or cid, int(cum), price)
        if conn.execute("SELECT 1 FROM fills WHERE fill_id=?",
                        (fill_id,)).fetchone() is not None:
            continue      # 同一累计状态的指纹已落库（兜底幂等）
        store.insert_fill(conn, fill_id, cid, float(price), delta)
        inserted.append({"client_order_id": cid,
                         "broker_order_id": broker_row["broker_order_id"],
                         "qty": delta, "price": float(price), "fill_id": fill_id})
    return {"count": len(inserted), "skipped": len(skipped), "aggregate": True,
            "note": "聚合成交（非逐笔）：券商订单历史无逐笔成交明细",
            "orders": inserted, "skipped_orders": skipped}


def _advance_order_states(conn, pairs):
    """对账「先同步券商事实」的第二步：按券商累计成交量推进 OMS 订单状态。

    为什么必须推进：sim 通道的成交不经 WS 交易事件通道，OMS 状态会停在 ``submitted``
    （或 ``unknown``）；而 ``_pair_diffs`` 的「券商累计成交已足额但 OMS 未记成交」判据
    在 sim 下必然成立 → 每日 critical + ``set_halt`` → 自动执行被自己的对账噪声永久
    熔断。把券商事实先学进本地，判定才是在比「两侧真实状态」，而不是比「本地学没学会」。

    口径与边界（诚实清单）：

    * **数量口径优先于状态文本**（延续 WP8 P1 遗留项）：券商 ``cum_qty`` 是原文数量事实，
      券商 ``status`` 是未公开整数码——因此只按 ``cum_qty`` 与委托量的关系推进，
      **绝不解释状态码**；
    * **只前进不回退**：目标状态由数量关系决定（足额→``filled``、部分→``partial``），
      无出边的终态（``filled``/``cancelled``/``rejected``，即 ``TRANSITIONS`` 里没有该键
      或为空）直接跳过——券商修正使累计量回落时不会把 ``filled`` 拉回 ``partial``；
    * **非法迁移不崩、不掩盖**：``submitting → filled`` 不在状态机白名单（须先经
      ``submitted``），此类订单抛 ``ValueError`` 被捕获并计入 ``skipped``——**不绕开
      状态机**；它们随后仍会被 ``_pair_diffs`` 的数量矛盾判据暴露为差异（保守方向）；
    * **就地刷新共享匹配结构**：``pairs`` 是回填与订单级判定共用的唯一匹配结果，
      推进成功后把新状态写回 ``order["status"]``——紧随其后的判定必须看到**同步后**的
      状态（规格：同步后仍存在的差异才是真差异），否则刚推进到 ``filled`` 的单会被用
      旧状态再判一次，自锁熔断原样复现；
    * **零券商写操作**：本函数只读 ``pairs``（券商数据由调用方传入）、只写本地 OMS。
    """
    advanced, skipped = [], []
    for order, broker_row, _match in pairs:
        cum = broker_row["cum_qty"]
        if not cum or cum <= 0:
            continue                                  # 未成交：状态不动
        oms_qty = _int_of(order["qty"])
        if oms_qty is None or oms_qty <= 0:
            continue                                  # 委托量不可解：不猜目标状态
        if not oms.TRANSITIONS.get(order["status"]):
            continue                                  # 终态（无出边）：只前进不回退
        target = "filled" if int(cum) >= oms_qty else "partial"
        if order["status"] == target:
            continue                                  # 已是目标态：无需迁移
        cid = order["client_order_id"]
        before = order["status"]
        try:
            oms.transition(conn, cid, target)
        except ValueError as error:                   # 非法迁移：跳过并计数，不崩
            skipped.append({"client_order_id": cid, "from": before, "to": target,
                            "broker_cum_qty": int(cum), "oms_qty": oms_qty,
                            "reason": str(error)[:120]})
            continue
        order["status"] = target                      # 共享结构就地刷新（见 docstring）
        advanced.append({"client_order_id": cid, "from": before, "to": target,
                         "broker_cum_qty": int(cum), "oms_qty": oms_qty})
    return {"count": len(advanced), "skipped": len(skipped),
            "orders": advanced, "skipped_orders": skipped}


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
       "orders": {...}, "tca": {...}, "digest": {...},
       "fills_backfilled": {...}, "orders_advanced": {...}, "halted": <bool>}  对账完成

    ``fills_backfilled`` 是「券商订单历史 → fills」的回填摘要（``count`` 落库条数、
    ``skipped`` 无均价跳过数、``note`` 标注聚合成交非逐笔）；``orders_advanced`` 是
    「按券商累计成交量推进 OMS 状态」的摘要（``count`` 推进条数、``skipped`` 非法迁移
    跳过数）。两步都在判定之前完成（先同步券商事实，再判差异）。

    通道故障一律 fail-closed：**不写任何 kv**（既不写差异也不伪造「无差异」）。
    """
    from . import alerts, planner, tca

    home = str(home)
    # 时钟口径唯一实现在 daemon（显式 now > DSH_FAKE_NOW > 真实时间）；非法假时钟
    # fail-closed——对账按错误日期跑会产出误导性的「无差异」。
    from . import daemon
    try:
        stamp = (now or daemon.now_fn())()
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    today = today or stamp[:10]

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

    # 「先同步券商事实，再判差异」——顺序是正确性要求（规格 §4.4）：
    #   ① 回填成交（fills）→ ② 推进订单状态 → ③ 订单级判定 → ④ 持仓级比对。
    # ①② 都只读券商、只写本地：sim 通道的成交与状态都不经 WS 事件通道，不先把券商
    # 事实学进本地，③④ 就会把「本地尚未学习」判成差异 → critical + halt → 自锁熔断。
    # 匹配结果三处共用（一次匹配，避免两处错位把钱/状态记到别的订单上）。
    matched = _match_orders(conn, today, broker_rows)
    backfill = _backfill_fills(conn, matched[0])
    advance = _advance_order_states(conn, matched[0])
    diffs = _order_diffs(conn, today, broker_rows, matched=matched)

    # 持仓级：本地 = fills 派生净持仓（含本次回填）；只核对 OMS 有足迹的标的
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
    backfill_digest = {"count": backfill["count"], "skipped": backfill["skipped"],
                       "aggregate": True, "note": backfill["note"]}
    # 状态推进摘要：``count`` 推进条数、``skipped`` 非法迁移跳过数（不掩盖——
    # 跳过的会被订单级判定暴露为差异，见 _advance_order_states docstring）
    advance_digest = {"count": advance["count"], "skipped": advance["skipped"]}
    # 熔断可见性（2026-09-16 修订 I3）：digest 记录**实际熔断状态与原因**（可能来自
    # 更早的一次对账差异），否则自动执行守卫只会 info 级跳过、页面看不出停摆原因。
    # 本函数不自动清除已生效的 halt——恢复永远由人工 clear_halt 决定（先查明原因）。
    digest = {"as_of": today, "mode": mode, "orders": order_status_counts(conn, today),
              "diffs": len(diffs), "untracked": len(untracked), "tca": tca_summary,
              "fills_backfilled": backfill_digest, "orders_advanced": advance_digest,
              **store.halt_summary(conn), "at": stamp}
    # snapshot-reconcile 的既有取数口径（diffs/at）+ 本任务新增 untracked/mode
    store.kv_set(conn, "reconcile:latest",
                 {"diffs": diffs, "untracked": untracked, "mode": mode, "at": stamp})
    store.kv_set(conn, "daily:digest", digest)
    return {"ok": True, "diffs": diffs, "untracked": untracked,
            "orders": digest["orders"], "tca": tca_summary, "digest": digest,
            "fills_backfilled": backfill, "orders_advanced": advance, "halted": halted}
