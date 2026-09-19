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
  只有**能支撑持仓主张**的标的参与差异判定（口径见 ``footprint_symbols``：本地观察到过
  建仓的**买入成交**，或本地相信自己**买入**过的订单）——其余券商仓位记入
  ``untracked``、本地那些无法支撑的主张记入 ``local_unbacked``，两者都**如实列出、
  不计差异、不 halt**（多半是平台外建的仓，报差异是噪音）；
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

from . import alerts, oms, store

_TZ8 = _dt.timezone(_dt.timedelta(hours=8))

#: OMS 中「应当能在券商侧查到」的状态（真正到过券商的在途/终态）。
#: draft/frozen 未提交、cancelled/rejected 无需券商佐证——都不核对（否则是噪音）。
BROKER_EXPECTED_STATES = ("submitting", "submitted", "partial", "filled", "unknown")

#: 本地订单中「本地相信自己**买入**过、且可能已经成交」的状态——即使 ``fills`` 缺失
#: （例如券商没给成交均价，回填如实跳过），本地 OMS 也已经记下「这单买入成交过」，
#: 持仓缺口必须继续可见。**持仓知识只来自「买入主张」**：买入成交（建仓腿），或
#: 这里的买入订单；从未到券商的 ``draft``/``frozen``、无成交的 ``cancelled``/``rejected``，
#: 以及**卖出**订单都不构成持仓主张——把它们算成足迹会让券商侧的历史存量持仓被误判成
#: ``missing_side``（实机 2026-09-19：手工计划 ``PLN-20260918-sim-6815`` 的 9 张**从未
#: 提交**的作废单——1 张买入 + 8 张卖出——把 8 只历史存量持仓从 ``untracked`` 升级成
#: ``missing_side`` →
#: critical + halt）。
POSITION_CLAIM_STATES = ("partial", "filled", "unknown")

#: 券商只读工具（对账绝无写调用；出现写类即 bug）。
SIM_READ_TOOLS = ("sim_trade_account_list", "sim_trade_position_list",
                  "sim_trade_history_order_list")

#: 券商订单行 side 码 → 仓库方向（TOOL-LIMITS 实测：1=Buy 2=Sell）。
SIDE_BY_CODE = {1: "BUY", 2: "SELL"}

#: 官方 **已发布** 的订单终态 → OMS 目标状态（`naming-dictionary#order-status`，
#: 2026-09-17 现场核对官方文档）。**只认字符串枚举**：托管 MCP 的未公开整数码
#: （TOOL-LIMITS 明示禁止猜标签）不在表内，遇到即「不确定」→ 不迁移、交给差异判定。
#:
#: * ``CANCELLED_ALL``（全部撤单、无成交）→ ``cancelled``；
#: * ``CANCELLED_PART``（部分成交、剩余已撤）→ ``partial``（**数量口径优先于状态文本**，
#:   延续 WP8 P1 遗留项：有成交就不能标 cancelled；该单因此仍非终态，属已知遗留）；
#: * ``FAILED``（服务端拒绝）→ ``rejected``。
BROKER_TERMINAL_STATUS = {
    "CANCELLED_ALL": "cancelled",
    "CANCELLED_PART": "partial",
    "FAILED": "rejected",
}

#: 官方**已发布**的模拟交易订单状态码（`sim-trade/order-list.md`「字段说明」原文，
#: 2026-09-17 现场核对：``2=已提交 3=部分成交 4=全部成交 5=已撤 6=拒绝``）。
#:
#: 与上面那张表的区别：模拟交易 REST/托管 MCP 返回的是**整数** ``status``，而这张表是官方
#: 文档逐值列出的契约——因此解释它**不再是猜标签**（本仓库此前「禁止猜整数码」是因为当时
#: 没有契约可用）。对账只查模拟交易订单工具（``SIM_READ_TOOLS``），本表即适用。
#: 表外的整数码一律视为不确定（不迁移，交差异判定）。
SIM_ORDER_STATUS = {2: "submitted", 3: "partial", 4: "filled", 5: "cancelled", 6: "rejected"}

#: 收编行（券商有、OMS 无的订单被导入台账）在 ``orders.plan_id`` 上的专用标记：与真实
#: 计划号永不冲突，审计时一眼可辨「这条是收编进台账的历史单，不是某次计划下的单」。
#: 定义在常量区（而非紧邻 ``_import_broker_only_orders``）：``footprint_symbols`` 也要
#: 用它（收编行**不建立持仓知识**，见该函数 docstring）。
IMPORT_PLAN_ID = "reconcile-import"


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
    """本地台账**能支撑持仓主张**的标的（= 有资格参与持仓级比对的标的）。

    持仓主张只有一种：**本地买入过**。它有两个来源（少一个就会把真差异藏起来）：

      * **买入成交**（``fills`` 挂在 BUY 订单上）：本地台账观察到过**建仓**事件——这是
        「本地认为该标的持有多少」唯一的事实基础。**只有卖出成交不算**：那说明券商侧该
        仓位是平台外建仓（本地库被清空、或建仓腿早于券商订单窗口），本地无从主张它持有
        什么（实机 2026-09-19：港股 ``HK.00100``/``HK.00981``/``HK.02513`` 只有三张回填
        自券商历史的**减仓/清仓**成交，本地净持仓 −200/−1000/−100 是账本残缺，不是两侧
        事实分歧）；
      * **本地相信自己买入过的订单**（``POSITION_CLAIM_STATES`` + ``side=BUY``）：即便
        ``fills`` 缺失（券商未给成交均价 → 回填如实跳过），本地 OMS 已记「买入成交过」，
        缺口必须继续可见（宁缺毋假：见 ``tests/test_wp9_reconcile_daily.py`` 的同名用例）。

    **不建立持仓知识**的三类（都是本仓库踩过的自锁熔断）：

      * 收编行（``IMPORT_PLAN_ID``）：那是本模块自己写下的记账产物，只回答「这张券商单本地
        为什么没有」（2026-09-18 实机：收编零成交的 ``SH.603993`` 让历史存量从 ``untracked``
        升级成 ``missing_side``——收敛路径用自己的产物制造新差异）；
      * 从未到过券商的本地单（``draft``/``frozen``）与无成交的作废单（``cancelled``/
        ``rejected``）：本地单方面取消/作废，券商侧什么都没发生过。**这条是必须的**：
        自动流水线在 16:20 建好次日计划、次日 09:35 才执行，而 19:00 的对账看到的正是
        一批 ``draft`` 单——若它们算足迹，凡是「券商持存量老仓 + 关注池内」的标的
        每天都会报 ``missing_side``（2026-09-19 实机：9 张作废单让 8 只老仓报 missing_side）；
      * **卖出**订单与只有卖出成交的标的：本地从未观察到建仓，无从主张持仓（这些标的的
        本地主张随 ``local_unbacked`` 如实列出）。

    用**累计**口径而非「当日增量」：本地台账（fills 净持仓）本就是累计的，拿当日足迹
    去比会把昨日建仓的标的整个排除在核对之外，只减少噪音却漏掉真实差异。

    **真实单边缺失不受影响**：「本地认为已平仓、券商仍持有」的标的必然有买入成交 → 仍在
    足迹内 → 本地净持仓为 0（``local_net_positions`` 丢掉零值）→ 与券商有量比出
    ``missing_side`` → critical + halt（``tests/test_reconcile_footprint.py`` 的对偶用例）。

    **已知边界（故意的保守方向）**：本地只要有过买入主张就永久进入比对，哪怕它对某标的的
    历史只知一角（例如券商另有清库前的存量、本地只知新买的那一笔）——那种不一致会报
    ``qty`` 差异并熔断。宁可报警也不放过真差异；反之，「本地连买入主张都没有」才归
    ``untracked``/``local_unbacked``。
    """
    marks = ",".join("?" * len(POSITION_CLAIM_STATES))
    rows = conn.execute(
        "SELECT o.symbol AS symbol FROM fills f"           # ① 建仓腿：买入成交
        " JOIN orders o ON o.client_order_id=f.client_order_id"
        " WHERE UPPER(o.side) = ?"
        " UNION SELECT symbol FROM orders"                 # ② 本地相信自己买入过（fills 缺口）
        " WHERE plan_id != ? AND UPPER(side) = ? AND status IN (" + marks + ")",
        ("BUY", IMPORT_PLAN_ID, "BUY", *POSITION_CLAIM_STATES)).fetchall()
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
            # 累计成交：官方 REST 用 dealt_qty，托管 MCP 用 cum_qty——两者都是数量事实
            "cum_qty": _int_of(row.get("cum_qty")
                               if row.get("cum_qty") is not None else row.get("dealt_qty")),
            "price": _float_of(row.get("price")),
            "avg_fill_price": _float_of(row.get("avg_fill_price")
                                        or row.get("dealt_avg_price")),
            "status_raw": row.get("status") if row.get("status") is not None
                          else row.get("order_status"),
            # 整数状态码原样保留（模拟交易文档已发布 2/3/4/5/6 的取值，见 SIM_ORDER_STATUS）
            "status_code": _int_of(row.get("status")),
            # 官方 **已发布** 的字符串状态（naming-dictionary#order-status）。只有它进终态
            # 对齐判定；托管 MCP 的未公开整数码一律不解释（沿用既有保守口径）。
            "order_status": (str(row.get("order_status")).strip().upper()
                             if isinstance(row.get("order_status"), str) else None),
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


def _import_status(broker_row, cum):
    """可导入的 OMS 状态；**不确定返回 None**（不导入 → 仍按差异暴露，保守）。

    只认两份已发布契约（``SIM_ORDER_STATUS`` / ``BROKER_TERMINAL_STATUS``）：
    ``submitted`` 这类非终态照实导入（在途单进台账才能被后续对账收敛）；
    表外取值、或枚举与数量事实矛盾（如「已撤」却有成交且非 ``CANCELLED_ALL``）→ None。
    """
    code = broker_row.get("status_code")
    if isinstance(code, int) and not isinstance(code, bool):
        name, explicit_no_fill = SIM_ORDER_STATUS.get(code), False
    else:
        text = str(broker_row.get("order_status") or "").strip().upper()
        name, explicit_no_fill = BROKER_TERMINAL_STATUS.get(text), (text == "CANCELLED_ALL")
    if name is None:
        return None
    if name == "submitted":
        return "submitted"
    return _mapped_terminal(name, cum, broker_row, explicit_no_fill)


def _import_broker_only_orders(conn, mode, unmatched_broker, today=None):
    """把「券商有、OMS 无」的订单**收编进 OMS 台账**（缺失差异的收敛路径）。

    为什么需要（实机 2026-09-17）：券商侧存在一张本地从未登记的单（历史遗留/收编前下的
    单）→ 每日对账报 ``missing_in_oms`` → critical + ``set_halt`` → 自动闭环天天被自己
    的历史差异锁死；而 ``oms-align`` 要求本地已有该单号，**没有收敛入口**。

    ``today``：本次对账的日期，收编行按它落 ``created_at``（缺省墙钟=生产路径二者相同）。
    **必须传**：订单匹配按 ``created_at LIKE <今天>%`` 取台账，收编行若按墙钟落日期，重放
    历史日期时它会落在窗口外——刚收编的行仍被判成 ``missing_in_oms``（critical + halt
    原样复现），同时污染今天的订单窗口。

    OMS 的定位是「与券商往来的真实订单状态」的权威台账，因此收编是**记账**而非下单：
    * **只读券商**：本函数不产生任何写类券商调用；
    * **幂等**：``client_order_id`` 由券商单号派生（``imp-<sha1(broker_order_id)[:24]>``），
      且先按「该编号或该券商单号是否已存在」判定——重复运行不产生重复行；
    * **不猜状态**：只认已发布枚举（``_import_status``），表外/矛盾一律不导入、保留为差异；
    * **来源可辨**：``plan_id`` 固定 ``IMPORT_PLAN_ID``、``err`` 以 ``reconcile-import:``
      开头并带券商编号与原始状态码（审计链能回答「这行从哪来」）；
    * 收编成功的订单**不再计为差异**（配对在重新匹配后成立），因此不再触发 critical/halt。
    """
    imported, skipped = [], []
    for row in unmatched_broker or []:
        bid = str(row.get("broker_order_id") or "").strip()
        if not bid:
            skipped.append({"broker_order_id": None,
                            "reason": "券商行无订单号：无法建立确定性指纹"})
            continue
        cid = "imp-" + hashlib.sha1(bid.encode("utf-8")).hexdigest()[:24]
        exists = conn.execute(
            "SELECT 1 FROM orders WHERE client_order_id=? OR broker_order_id=?",
            (cid, bid)).fetchone()
        if exists is not None:
            continue  # 幂等：已收编过，或本地本就有这张券商单
        cum = _int_of(row.get("cum_qty")) or 0
        status = _import_status(row, cum)
        if status is None:
            skipped.append({"broker_order_id": bid,
                            "reason": "状态未发布或与数量事实矛盾：不猜，保留为差异"})
            continue
        symbol = str(row.get("symbol") or "")
        if not symbol:
            skipped.append({"broker_order_id": bid, "reason": "券商行无标的"})
            continue
        store.insert_order(conn, cid, IMPORT_PLAN_ID, symbol, symbol.split(".", 1)[0],
                           row.get("side"), _int_of(row.get("qty")) or 0,
                           _float_of(row.get("price")), mode, status=status,
                           broker_order_id=bid,
                           created_at=f"{today} 00:00:00" if today else None)
        conn.execute("UPDATE orders SET err=? WHERE client_order_id=?",
                     (f"reconcile-import: broker_order_id={bid} "
                      f"broker_status={row.get('status_raw')}", cid))
        conn.commit()
        imported.append({"broker_order_id": bid, "client_order_id": cid,
                         "symbol": symbol, "status": status})
    return {"imported": imported, "skipped": skipped}


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
        # —— 终态对齐（E2E S2 遗留，2026-09-17）——
        # 券商已给**已发布**的终态时，本地仍在途的单必须跟着收敛，否则 `orders` 里留下
        # 永久 `submitted` 的幽灵行：闸门在途查重会一直挡住同标的同方向新单，审计链也失真。
        # 判据只认两件事：①官方字符串枚举；②数量事实不矛盾（全撤必须无成交）。
        # 任何不确定（未公开整数码、状态与数量矛盾、迁移不在合法路径）→ **不猜**，
        # 保持原状并交给 `_pair_diffs` 报差异（保守方向）。
        terminal = _terminal_target(broker_row, cum)
        if terminal is not None:
            before = order["status"]
            outcome, why = _try_terminal(conn, order, terminal, broker_row)
            if outcome:
                # 记录形状与数量口径推进**统一**（消费方不必分辨两条路径）
                outcome.setdefault("broker_cum_qty", int(cum) if cum else 0)
                outcome.setdefault("oms_qty", _int_of(order["qty"]))
                advanced.append(outcome)
            elif why:
                skipped.append({"client_order_id": order["client_order_id"],
                                "from": before, "to": terminal,
                                "broker_order_id": broker_row["broker_order_id"],
                                "broker_cum_qty": int(cum) if cum else 0,
                                "oms_qty": _int_of(order["qty"]), "reason": why})
            continue
        # 终态不确定（表外码、或状态与数量矛盾）→ 落到下面的数量口径推进；无成交则不动
        if not cum or cum <= 0:
            continue                                  # 未成交且无终态事实：状态不动
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


def _mapped_terminal(mapped, cum, broker_row, explicit_no_fill):
    """已发布枚举值 + 数量事实 → OMS 目标；矛盾/不确定一律 ``None``。

    ``explicit_no_fill``：官方字符串 ``CANCELLED_ALL`` 的语义就是「**全部撤单、无成交**」
    ——若同时有累计成交，两侧事实自相矛盾，**不猜**（留给差异判定）。模拟交易的整数码
    ``5`` 只标「已撤」、不区分是否部分成交，故 ``5`` + 有成交时按**数量口径**给 ``partial``
    （有成交就不能标撤单，延续 WP8 P1）。
    """
    if mapped is None or mapped == "submitted":
        return None
    if mapped == "cancelled":
        if not cum:
            return "cancelled"
        return None if explicit_no_fill else "partial"
    if mapped == "partial":
        return "partial" if cum else None
    if mapped == "filled":
        qty = broker_row.get("qty")
        return "filled" if (cum and qty and cum >= qty) else None
    return mapped                                     # rejected


def _terminal_target(broker_row, cum):
    """券商订单状态 → OMS 目标状态；**不确定返回 None**（不猜）。

    只认两份**已发布**契约：官方 live 的字符串 ``order_status``（``BROKER_TERMINAL_STATUS``）
    与官方模拟交易的整数 ``status``（``SIM_ORDER_STATUS``，`sim-trade/order-list.md` 逐值列出）。
    表外的取值（含托管 MCP 历史遗留的未标注整数）一律视为不确定。
    """
    text = str(broker_row.get("order_status") or "").strip().upper()
    if text:
        return _mapped_terminal(BROKER_TERMINAL_STATUS.get(text), cum, broker_row,
                                explicit_no_fill=(text == "CANCELLED_ALL"))
    code = broker_row.get("status_code")
    if isinstance(code, int) and not isinstance(code, bool):
        return _mapped_terminal(SIM_ORDER_STATUS.get(code), cum, broker_row,
                                explicit_no_fill=False)
    return None


def _try_terminal(conn, order, target, broker_row):
    """按合法路径迁移；返回 ``(记录, 跳过原因)``。

    三种结果（消费方据此分类，**不制造噪音**）：

      * 已在目标态 → ``(None, None)``：什么都没发生，既不算推进也不算跳过；
      * 非法迁移（如 ``submitting → filled`` 须先经 ``submitted``）→ ``(None, "非法迁移 …")``：
        计入 ``skipped`` 并**保留状态机给的原因**（不绕开状态机、不替它猜多步路径）；
      * 迁移成功 → ``(记录, None)``，并**就地刷新**共享结构（紧随的 `_pair_diffs` 必须看到
        同步后的状态，否则对齐过的单会被用旧状态再判一次）。
    """
    if order["status"] == target:
        return None, None
    if not oms.TRANSITIONS.get(order["status"]):
        return None, None                             # 本地已是终态：不回退，也不算跳过
    cid = order["client_order_id"]
    before = order["status"]
    try:
        oms.transition(conn, cid, target,
                       err=f"reconcile:{broker_row.get('order_status') or 'terminal'}")
    except ValueError as error:
        return None, f"非法迁移 {before} → {target}：{error}"
    order["status"] = target
    return ({"client_order_id": cid, "from": before, "to": target,
             "broker_order_id": broker_row["broker_order_id"],
             "broker_order_status": broker_row.get("order_status")}, None)


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
      {"ok": True,  "diffs": [...], "untracked": [...], "local_unbacked": [...],
       "orders": {...}, "tca": {...}, "digest": {...},
       "fills_backfilled": {...}, "orders_advanced": {...}, "halted": <bool>}  对账完成

    ``untracked`` 是「券商有仓位、本地无从判断」的标的；``local_unbacked`` 是「本地有
    fills 净持仓、但没有任何建仓买入成交（因此无法支撑持仓主张）」的标的（两者都如实列出、
    **不计差异**、不 halt——见 ``footprint_symbols``）。

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

    from . import broker as core_broker

    if broker_call is None:
        # WP13 任务 3：默认通道走 channel 分派（``futu_channel: openapi`` 且凭据就绪 →
        # REST；否则回退 mcp）——与 ``planner.plan_auto`` / ``daemon._default_executor``
        # 同一口径。曾经这里是裸 ``futu_mcp.call_tool``：openapi 通道下「下单执行走
        # REST、对账读券商事实走 MCP」的通道分裂由本任务实测暴露，在此收口。
        # sim_call 只接管模拟交易 7 个工具（本作业 mode=sim），live 不在本作业范围。
        try:
            broker_call = core_broker.sim_call(home)
        except Exception as error:  # noqa: BLE001 —— 导入失败即通道不可用
            reason = f"券商通道不可用：{str(error)[:120]}"
            alerts.emit(conn, home=home, level="warn", title="对账通道不可用",
                        detail=reason[:160])
            return {"ok": False, "error": reason}

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
    # ③ 收编「券商有、OMS 无」的订单（缺失差异的收敛路径，见 _import_broker_only_orders）：
    #    必须在差异判定**之前**，且导入后**重新匹配**——否则刚收编的行仍会被判成 missing_in_oms，
    #    每日 critical + halt 的老问题原样复现。
    imported = _import_broker_only_orders(conn, mode, matched[2], today=today)
    if imported["imported"]:
        alerts.emit(conn, home=home, level="warn", title="订单导入：券商独有",
                    detail=f"收编 {len(imported['imported'])} 条券商订单进 OMS 台账（"
                           + ",".join(str(x["broker_order_id"])
                                      for x in imported["imported"])[:120] + "）")
        matched = _match_orders(conn, today, broker_rows)
    backfill = _backfill_fills(conn, matched[0])
    advance = _advance_order_states(conn, matched[0])
    diffs = _order_diffs(conn, today, broker_rows, matched=matched)

    # 持仓级：本地 = fills 派生净持仓（含本次回填）；只核对**能支撑持仓主张**的标的
    # （footprint：有建仓买入成交，或本地相信自己买入过的订单——见 footprint_symbols）。
    footprint = footprint_symbols(conn)
    net = local_net_positions(conn)
    local = {s: {"qty": q} for s, q in net.items() if s in footprint}
    broker_subset = {s: v for s, v in broker_positions.items() if s in footprint}
    diffs.extend(compare(conn, local, broker_subset))
    untracked = sorted(set(broker_positions) - footprint)
    # 本地账本**无法支撑**的持仓主张（只有减仓/平仓腿、没有任何建仓买入成交）：本地账本
    # 内容不完整，不是「两侧事实分歧」，因此**如实列出、不计差异、不 halt**（与 untracked
    # 同级）。清库重建/本地历史早于券商订单窗口时这里会**非空**——它是「本地为什么是负
    # 持仓」的可见答案，绝不是让自己闭嘴。
    local_unbacked = sorted(s for s in net if s not in footprint)

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
              "diffs": len(diffs), "untracked": len(untracked),
              # 本地无法支撑的持仓主张（只有减仓腿；如实列出、不计差异——见 footprint_symbols）
              "local_unbacked": len(local_unbacked), "tca": tca_summary,
              "fills_backfilled": backfill_digest, "orders_advanced": advance_digest,
              # 收编计数（2026-09-17）：券商独有订单被导入台账的条数（0 = 无需收敛）
              "orders_imported": len(imported["imported"]),
              "import_skipped": len(imported["skipped"]),
              **store.halt_summary(conn), "at": stamp}
    # snapshot-reconcile 的既有取数口径（diffs/at）+ untracked/mode/local_unbacked
    store.kv_set(conn, "reconcile:latest",
                 {"diffs": diffs, "untracked": untracked, "local_unbacked": local_unbacked,
                  "mode": mode, "at": stamp})
    store.kv_set(conn, "daily:digest", digest)
    return {"ok": True, "diffs": diffs, "untracked": untracked,
            "local_unbacked": local_unbacked,
            "orders": digest["orders"], "tca": tca_summary, "digest": digest,
            "fills_backfilled": backfill, "orders_advanced": advance, "halted": halted}

#: 允许经 `align_terminal` 人工对齐的目标状态（只允许**终态**，且不含 `filled`——
#: 「已成交」是对券商事实的断言，必须由对账按数量口径得出，不能由人一键写成）。
ALIGN_ALLOWED_TARGETS = ("cancelled", "rejected")


def align_terminal(conn, home, broker_order_id, status, reason, operator="cli"):
    """受约束的**一次性**订单终态对齐（E2E S2 遗留入口，2026-09-17）。

    存在理由：对账（`daily`）已能按官方终态枚举收敛，但**对账覆盖不到的历史行**（例如
    S2 修复之前撤单成功却没回写的幽灵 `submitted`）过去只能手改库——那是不可审计操作。
    本入口给它一条留痕路径。

    纪律（每条都有测试）：
      * 目标**只允许** `cancelled`/`rejected`（`ALIGN_ALLOWED_TARGETS`）；
      * 必须命中本地 `orders` 行（按 `broker_order_id`）——无对应行**拒绝且不伪造**；
      * 必须走 `oms.TRANSITIONS` 的**合法路径**（`filled` 等终态无出边 → 自然拒绝，
        因此**不可能把已成交的单降级**）；
      * `reason` 必填（空/纯空白拒绝）——审计要能回答「为什么改」；
      * 成功写一条 **warn 告警**（含券商单号、from→to、原因、操作者）；
      * 只写本地 OMS 与告警，**绝不触达券商**（不撤单、不下单、不重放）。
    返回 `{"ok": True, ...}` 或 `{"ok": False, "error": str}`（调用方据此非零退出）。
    """
    target = str(status or "").strip().lower()
    if target not in ALIGN_ALLOWED_TARGETS:
        return {"ok": False,
                "error": f"status 只允许 {'/'.join(ALIGN_ALLOWED_TARGETS)}（收到 {status!r}）"
                         "；成交类状态必须由对账按数量口径得出"}
    if not str(reason or "").strip():
        return {"ok": False, "error": "reason 必填：审计需要知道为什么人工对齐"}
    row = conn.execute(
        "SELECT client_order_id, status FROM orders WHERE broker_order_id=?",
        (str(broker_order_id),)).fetchone()
    if row is None:
        return {"ok": False,
                "error": f"本地没有 broker_order_id={broker_order_id} 的订单行"
                         "（不伪造：先确认该单是否属于本台账）"}
    before = row["status"]
    try:
        oms.transition(conn, row["client_order_id"], target,
                       err=f"align:{operator}:{str(reason)[:80]}")
    except ValueError as error:
        return {"ok": False,
                "error": f"非法迁移 {before} → {target}：{error}"
                         "（终态不可回退；已成交的单不能人工降级）"}
    alerts.emit(conn, home=str(home), level="warn", title="订单终态人工对齐",
                detail=f"broker_order_id={broker_order_id} "
                       f"{before}→{target}；原因：{str(reason)[:120]}；操作者：{operator}")
    return {"ok": True, "client_order_id": row["client_order_id"],
            "from": before, "to": target, "broker_order_id": str(broker_order_id),
            "operator": operator}

