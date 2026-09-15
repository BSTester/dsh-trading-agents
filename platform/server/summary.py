# WP6 补遗任务 B2：交易概要 Python 移植。逐行对应 plugins/workbench/src/broker_trades.js（202 行）。
#
#   broker_trades.js:17-21   ORDER_ACTIONS   -> ORDER_ACTIONS
#   broker_trades.js:24      ORDER_SOURCE    -> ORDER_SOURCE
#   broker_trades.js:26      SIDE_LABELS     -> SIDE_LABELS
#   broker_trades.js:33-54   businessData    -> business_data()
#   broker_trades.js:56-58   toolName        -> tool_name()
#   broker_trades.js:60-63   actionLabel     -> action_label()
#   broker_trades.js:65-68   numeric         -> _js.numeric()
#   broker_trades.js:71-76   fillState       -> _fill_state()
#   broker_trades.js:79-84   microTime       -> _micro_time()
#   broker_trades.js:86-110  orderRow        -> _order_row()
#   broker_trades.js:118-202 summarizeBrokerActivity -> summarize()
#
# 纯标准库、无 IO。输出键集与合作 Node 版逐字一致（tests/test_wp6_summary_audit.py 做递归键
# 集守护）。入参是已按模式过滤且倒序的 activity 列表——与 store.js:217
# `summarizeBrokerActivity(activity)` 的入参一致（store.js:209 的 `.filter(...).reverse()`）。
#
# JS 语义助手（真值、空值合并、模板字符串、Number()、Math.round、对象键枚举序）统一收在
# server/_js.py，不再在本模块内重复实现——重复实现正是补遗 B 移植审查抓到的漂移来源
# （summary 与 audit_chain 对空数组真值给了两种答案）。逐条语义与出处见 _js.py。
#
# 有意差异（诚实边界，均与 Node 行为区分并已核对）：
#   1. 正则：JS `$` 同时匹配「串尾」与「尾随换行前」，Python `$` 同样如此，故 ORDER_ACTIONS /
#      ORDER_SOURCE 用 re.search 逐条等价；JS 的 `.test()` 在不带 g 标志时无 lastIndex 状态。
#   2. 时间：JS `new Date(ms)` 对超出 ±8.64e15 的输入产出 Invalid Date，本实现同样返回 None
#      （datetime.fromtimestamp 抛错即视为不可解析）。微秒→毫秒用整数除法（JS `Math.floor`
#      对负数向下取整，这里 micros<=0 已提前返回 None，故无差异）。
#   3. 排序：JS `localeCompare` 对 ASCII/Unicode 默认按码点比较，与 Python 的字符串 `<`
#      一致；`Array.prototype.sort` 自 ES2019 起稳定，本实现同样用稳定排序。
#   4. JS 对象键序把「整数样式的键」提前并按数值升序枚举（Array index：0 ≤ n < 2^32-1）；
#      查询计数表因此按该规则还原枚举顺序（_js.object_entry_order），否则 queries.tools 的
#      并列项次序会漂移。
#   5. `Number()` 语义区分「字段缺失」与「显式 null」：缺失是 undefined -> NaN -> null
#      （_js.present()），显式 null 是 0（_js.numeric()）。这两条在 orderRow 里结果不同
#      （发现于差分用例 numeric_coercions）。
#   6. `Math.round` 是半数向 +∞（_js.js_round），Python 内置 round() 是银行家舍入，0.005 这类
#      ×100 后恰为 .5 的值会分叉（JS 得 0.01，round() 得 0）。
#   7. `parsed` 是数组时 JS 仍算 object（`typeof [] === "object"`），`.data` 取到 undefined -> {}，
#      于是 envelope 解析**成功**；Python 里 list 没有 .get()，_js.field() 显式按「数组无业务
#      字段」处理，保持与 Node 相同的 ok=True/data={}。
#   8. JS 模板字符串 `${v}` 对 null 产出 "null"（_js.template）、带 `?? ""` 的位置产出 ""
#      （_js.stringify）；数字走 JS 的 String(number)（`${1e-7}` 是 "1e-7"）。
#   9. activity 里出现 null 元素：JS `entry.is_error`（broker_trades.js:163）抛 TypeError，
#      本实现按「无名查询」计数而不抛错（服务进程不应因一条脏记录 500），见
#      test_null_activity_entry_is_tolerated。orders 数组里的 null 元素同理：
#      broker_trades.js:87 `raw.qty` 在 JS 里抛 TypeError，本实现取不到字段 -> 无 order_id -> 丢弃。
import json
import math
import re
from datetime import datetime, timezone

from server import _js

# broker_trades.js:17-21：下单/改单/撤单——这些才会改变券商侧状态。
ORDER_ACTIONS = [
    {"match": re.compile(r"input_order$"), "label": "下单"},
    {"match": re.compile(r"modify_order$"), "label": "改单"},
    {"match": re.compile(r"cancel_order$"), "label": "撤单"},
]

# broker_trades.js:24：唯一能提供订单事实的工具。
ORDER_SOURCE = re.compile(r"history_order_list$")

# broker_trades.js:26：schema 明文 order_side 1=Buy 2=Sell。
SIDE_LABELS = {1: "买入", 2: "卖出"}

# broker_trades.js:199-200：原文照抄（服务端改写会让两个实现的概要出现假差异）。
NOTICE = ("交易概要由 Harness 观察到的富途工具响应归纳而来，不是券商成交推送；"
          "只读查询仅计数不列出。下单与撤单请在 Harness 会话中完成并确认。")



def _fill_state(qty, cum_qty):
    """broker_trades.js:71-76：由委托数量与已成交数量推导成交情况，不猜状态码。"""
    if qty is None or qty <= 0 or cum_qty is None:
        return "未知"
    if cum_qty >= qty:
        return "全部成交"
    if cum_qty > 0:
        return "部分成交"
    return "未成交"


def _micro_time(value):
    """broker_trades.js:79-84：券商时间戳是微秒；无法解析时返回 None，不编造时间。"""
    micros = _js.numeric(value)
    if micros is None or micros <= 0:
        return None
    millis = math.floor(micros / 1000)
    if millis > 8.64e15 or millis < -8.64e15:
        return None  # JS: 超出 Date 表示范围（±8.64e15 ms）-> Invalid Date -> null
    try:
        moment = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None  # JS: Invalid Date -> NaN -> null
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def business_data(entry):
    """broker_trades.js:33-54 businessData：兼容 ret_code/data 与 s/d 两种信封。

    业务 JSON 是**字符串**，藏在 value.content[].text 里（富途返回格式）。
    """
    content = _js.field(_js.field(entry, "value"), "content")
    if not isinstance(content, list):
        return {"ok": False, "reason": "无内容"}
    # broker_trades.js:36-37：`content.find(part => part?.type === "text")?.text` 先命中的
    # **第一个** text part 就定结果，再判 `typeof text !== "string"`。不能「跳过非字符串继续
    # 往后找」——那会把本该失败的 text=null 信封判成成功（补遗 B F2）。
    text = None
    for part in content:
        if _js.field(part, "type") == "text":
            text = _js.field(part, "text")
            break
    if not isinstance(text, str):
        return {"ok": False, "reason": "无文本内容"}
    try:
        parsed = json.loads(text)
    except ValueError:
        # 工具层错误（如 MCP error -32603）是纯文本，如实报出而不是丢掉
        return {"ok": False, "reason": text[:160]}
    if not isinstance(parsed, (dict, list)):
        # broker_trades.js:45 `!parsed || typeof parsed !== "object"`：JS 里数组也是 object，
        # 所以下面的 `.data` / `.d` 在数组上取到 undefined，最终落到 {}。Python 的 list
        # 没有 .get()，因此显式按「数组 = 没有业务字段」处理（有意差异：Node 会静默成功）。
        return {"ok": False, "reason": "返回不是对象"}
    ret_code = _js.field(parsed, "ret_code")
    if isinstance(ret_code, (int, float)) and not isinstance(ret_code, bool) and ret_code != 0:
        # `${parsed.ret_code} ${parsed.ret_msg ?? ""}`：数字按 JS String(number)（1.0 -> "1"）
        reason = f"ret={_js.template(ret_code)} {_js.stringify(_js.field(parsed, 'ret_msg'))}"
        return {"ok": False, "reason": reason.strip()}
    # broker_trades.js:49 `parsed.s !== undefined`：**显式 null 也算「存在」**，
    # String(null)="null" !== "ok" -> 失败（补遗 B F1，差分用例 s_null_marker）。
    if _js.present(parsed, "s"):
        marker = _js.field(parsed, "s")
        if _js.template(marker).lower() != "ok":
            return {"ok": False, "reason": f"s={_js.template(marker)}"}
    # `parsed.data ?? parsed.d ?? {}`：data 显式为 null 时回退到 d；数组上取不到字段 -> {}
    data = _js.js_nullish(_js.field(parsed, "data"), _js.field(parsed, "d"))
    return {"ok": True, "data": data if data is not None else {}}


def tool_name(entry):
    """broker_trades.js:56-58：`String(entry?.tool ?? "").replace(/^mcp__futu__/, "")`。"""
    tool = _js.stringify(_js.field_or_undefined(entry, "tool"))
    return re.sub(r"^mcp__futu__", "", tool)


def action_label(name):
    """broker_trades.js:60-63 actionLabel：命中下单/改单/撤单返回中文，否则 None。"""
    for row in ORDER_ACTIONS:
        if row["match"].search(name):
            return row["label"]
    return None


def _order_row(raw, seen_at):
    """broker_trades.js:86-110 orderRow：订单字段直接取券商原文，不重算价格、不补默认值。"""
    def number(key):
        # `numeric(raw.qty)`：字段缺失时 JS 取到 undefined -> NaN -> null；显式 null 是 0
        return _js.numeric(_js.field(raw, key)) if _js.present(raw, key) else None

    qty = number("qty")
    cum_qty = number("cum_qty")
    avg_fill = number("avg_fill_price")
    side_code = number("side")
    amount = None
    if cum_qty is not None and avg_fill is not None:
        # `${...}`：`Math.round(cumQty * avgFill * 100) / 100`。JSON 层面非有限值会变成 null
        # （JSON.stringify(NaN/Infinity) === "null"），_js.json_number 统一处理。
        amount = _js.json_number(_js.js_round(cum_qty * avg_fill * 100) / 100)
    return {
        "order_id": _js.stringify(_js.field(raw, "order_id")),   # `String(raw.order_id ?? "")`
        "symbol": _js.stringify(_js.field(raw, "symbol")),   # `String(raw.symbol ?? "")`
        "name": _js.stringify(_js.field(raw, "stock_name")),  # `String(raw.stock_name ?? "")`
        # `SIDE_LABELS[sideCode] ?? null`：数值键命中；未命中/None 用 get 的 None
        "side": SIDE_LABELS.get(side_code),
        "side_code": side_code,
        "qty": qty,
        "filled_qty": cum_qty,
        "price": number("price"),
        "avg_fill_price": avg_fill,
        "amount": amount,
        "fill": _fill_state(qty, cum_qty),
        "status_code": number("status"),
        "ordered_at": _micro_time(_js.field(raw, "create_time")),
        "updated_at": _micro_time(_js.field(raw, "update_time")),
        "seen_at": seen_at if seen_at is not None else None,
    }


def _order_sort_key(row):
    """broker_trades.js:181-182：`String(b.ordered_at ?? b.seen_at ?? "").localeCompare(...)` 倒序。

    `??` 是空值合并而非逻辑或：ordered_at 为 "" 时**不**回退到 seen_at（有意差异 6：
    用 _js.js_nullish 明确区分 None 与假值，Python 的 `or` 做不到这一点）。
    """
    return _js.template(_js.js_nullish(row.get("ordered_at"), row.get("seen_at"), ""))



def summarize(activity):
    """broker_trades.js:118-202 summarizeBrokerActivity：把 activity 归纳为交易概要。

    :param activity: store.snapshot() 的 activity（已按模式过滤 + 倒序）
    :returns: {"orders", "actions", "queries", "counts", "notice"}
    """
    rows = activity if isinstance(activity, list) else []
    orders_by_id = {}
    actions = []
    query_tools = {}
    errors = 0
    order_responses = 0

    for entry in rows:
        name = tool_name(entry)
        parsed = business_data(entry)

        if ORDER_SOURCE.search(name):
            if not parsed["ok"]:
                errors += 1
                continue
            order_responses += 1
            raw_orders = _js.field(parsed["data"], "orders")
            order_list = raw_orders if isinstance(raw_orders, list) else []
            for raw in order_list:
                row = _order_row(raw, _js.field(entry, "at"))
                if not row["order_id"]:
                    continue
                # 同一订单会在多次查询里重复出现，且状态会演进：保留**最后一次观测**
                orders_by_id[row["order_id"]] = {**orders_by_id.get(row["order_id"], {}), **row}
            continue

        label = action_label(name)
        if label:
            ok = parsed["ok"] and not _js.truthy(_js.field(entry, "is_error"))
            data = parsed["data"] if parsed["ok"] else None
            order_id = _js.field(data, "order_id") if parsed["ok"] else None
            actions.append({
                "at": _js.field(entry, "at") if _js.field(entry, "at") is not None else None,
                "action": label,
                # `parsed.ok ? (parsed.data?.order_id ?? null) : null`
                "order_id": _js.js_nullish(order_id) if parsed["ok"] else None,
                "ok": ok,
                "detail": "" if ok else _js.js_nullish(parsed.get("reason"), "失败"),
                "entry_id": _js.field(entry, "id") if _js.field(entry, "id") is not None else None,
            })
            if not ok:
                errors += 1
            continue

        # 其余一律视为查询：只计数，不逐条铺开
        tool = name if name else "unknown"
        query_tools[tool] = query_tools.get(tool, 0) + 1
        if _js.truthy(_js.field(entry, "is_error")):
            errors += 1

    # 用**我们自己记录到的**动作补全生命周期：撤单/改单成功过就标注出来。
    cancelled = {row["order_id"] for row in actions
                 if row["ok"] and row["action"] == "撤单" and row["order_id"]}
    modified = {}
    for row in actions:
        if row["ok"] and row["action"] == "改单" and row["order_id"]:
            modified[row["order_id"]] = modified.get(row["order_id"], 0) + 1
    for order in orders_by_id.values():
        order["cancelled"] = order["order_id"] in cancelled
        order["modified_count"] = modified.get(order["order_id"], 0)

    orders = sorted(orders_by_id.values(), key=_order_sort_key, reverse=True)

    # JS 对象键枚举序（有意差异 4）：整数样式键（array index）优先且按数值升序
    tool_rows = [{"tool": tool, "count": query_tools[tool]}
                 for tool in _js.object_entry_order(query_tools)]
    # `sort((a, b) => b.count - a.count)`：稳定排序保持同计数项的插入顺序
    tool_rows.sort(key=lambda row: -row["count"])

    return {
        "orders": orders,
        "actions": sorted(actions, key=lambda row: _js.template(_js.js_nullish(row.get("at"), "")),
                          reverse=True),
        "queries": {
            "count": sum(query_tools.values()),
            "tools": tool_rows,
        },
        "counts": {
            "responses": len(rows),
            "order_responses": order_responses,
            "orders": len(orders),
            "actions": len(actions),
            "errors": errors,
        },
        "notice": NOTICE,
    }


__all__ = [
    "NOTICE", "ORDER_ACTIONS", "ORDER_SOURCE", "SIDE_LABELS", "action_label", "business_data",
    "summarize", "tool_name",
]
