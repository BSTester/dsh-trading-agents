"""WP6 补遗 B2 差分测试：交易概要 / 审计链 / 中文标签的 Python 移植。

对照基准是 plugins/workbench/src/broker_trades.js、plugins/workbench/src/audit.js 与
plugins/workbench/src/labels.js 的**实际运行结果**，不是对 JS 源码的二次解读：

  * 每个 fixture 的 Python 字典由本文件顶部的夹具函数构造（与 tests/broker-trades.test.mjs /
    tests/audit.test.mjs 的 entry()/ORDER/envelope 形状逐字对应）；
  * 同一组 fixture 送进 Node 侧真实函数，把返回的完整 JSON 快照压进 _NODE_REFERENCE_B64；
  * test_node_reference_parity 对每个 fixture 做 `self.assertEqual(python_out, node_out)` 递归比较，
    test_node_shape_parity 再递归比对键集**与叶子数值类型**（integer / float 分开）。

Node 参照物是一次性探针（不入库）的产物，可复现方式：

  1. 用 Python 导入本模块，把夹具函数 `_activity() / _audits() / _max_entries_cases() /
     _field_cases() / _label_cases()` 的返回值 `json.dumps` 成 fixtures.json（maxEntries 组
     只在输入里**存在** `maxEntries` 键时才写这个键，见 _run()）；
  2. 探针 .mjs 用 pathToFileURL `import` 上面三个 JS 模块，按分组调用
     `summarizeBrokerActivity` / `buildAuditChain({snapshot,trades,maxEntries})` /
     `extractBrokerFields` / `zh(table, value[, fallback])`；每组先过一次
     `JSON.parse(JSON.stringify(...))` 再算 shapeOf()——JS 里值为 undefined 的键会被
     stringify 丢掉、NaN/Infinity 会变成 null，参照物描述的是**线上 JSON** 的形状；
     shapeOf 的叶子分类是 `Number.isInteger(v) && Math.abs(v) < 1e21 ? "integer" : "float"`
     （对齐 JSON.stringify 的整数/指数形式切换点，见 platform/server/_js.py:json_number）；
  3. `node <probe>.mjs fixtures.json node-out.json`；
  4. `base64.b64encode(zlib.compress(json.dumps(json.load(open('node-out.json')),
     ensure_ascii=False, separators=(',', ':')).encode(), 9))` 得到 _NODE_REFERENCE_B64；
  5. 复算校验：把第 4 步的结果与内嵌串逐字段比对（审查者在 /tmp/wp6probe/ 留下的
     probe.mjs/regen.mjs + verify_reference.py 就是这套流程的现成实现）。

参照物有七个分组：summaries / summaryShapes / audits / auditShapes / fields /
auditsMaxEntries / labels。auditsMaxEntries 是 audit.js `maxEntries` 参数的等价用例
（buildAuditChain({...input, maxEntries: n})）。

补遗 B 审查点：F1 `{"s":null}` 判失败、F2 content 取**首个** text part、F3 array index 上界
4294967294、F6 Number("0b101")/("0o17")/拒绝 "1_000"、F7 整数值返回 int（形状守护分
integer/float）、F8/F9 String(number)（1e-7 而非 1e-07）与 toFixed 平局、F10 空数组真值、
F11 缺失 vs 显式 null 的模板、F12 `(b.atMs ?? 0)` 的 null 排序、F13 Object.entries 整数键提前。

有意差异（Python 比 Node 更保守的地方，已单独用注释与断言钉住，不计入等值比较的 fixture）：
  * 环状 value：audit.js 靠 JSON.parse 爆栈 + visited<200 兜底；Python 用幂等已访问集合直接
    有界终止，两者最终都只产出 ticker/status/orderId = null（test_cyclic_value_terminates）。
  * activity 里出现 null 元素：JS `entry.is_error`（broker_trades.js:163）会抛 TypeError，
    Python 侧 _js.field() 取不到字段、按「无名查询」计数，不抛错（test_null_activity_entry_is_tolerated）。
  * orders 数组里出现 null 元素：JS `raw.qty`（broker_trades.js:87）会抛 TypeError，Python
    侧无 order_id 直接丢弃（test_null_order_element_is_dropped）。
  * audit 的 activity 里出现非对象元素：**JS 也不抛**（audit.js:108 早退，见 audit_chain.py
    有意差异 5），两边都过滤掉，差异用例 activity_non_objects 反而证明了这一点。
  * 非数组入参（undefined/null/{}）两边都返回空概要，但 Node 只覆盖 undefined/null；Python 额外
    容忍 dict/字符串/整数（test_non_array_inputs_are_empty）。
  * `zh` 的 fallback：Python 的 None 同时表示「未传 fallback」与 JS 的 null fallback，
    服务侧没有传 null fallback 的调用点（labels.py 有意差异 3）。
  * maxEntries 的 null：Node 显式 null 得空时间线，Python 的 None 按缺省 120 处理
    （audit_chain.py 有意差异 7，理由是 `payload.get("maxEntries")` 的调用方安全），
    见 test_max_entries_null_is_the_documented_difference。
"""
import base64
import json
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
from server import _js  # noqa: E402
from server import audit_chain as ac  # noqa: E402
from server import labels as labels_py  # noqa: E402
from server import summary  # noqa: E402

LABELS_JS = ROOT / "plugins" / "workbench" / "src" / "labels.js"

_ENTRY_SEQ = [0]


# ---------------------------------------------------------------- fixture 构造
def _entry(tool, payload, extra=None):
    """broker-trades.test.mjs:11-22 entry()：一条 activity 记录（形状与 store 记录一致）。

    id 用递增序号而不是 Math.random()，保证与探针（Node）侧生成完全相同的载荷。
    """
    extra = extra or {}
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False,
                                                              separators=(",", ":"))
    _ENTRY_SEQ[0] += 1
    entry_id = extra.get("id", f"{tool}-{_ENTRY_SEQ[0] - 1}")
    at = extra.get("at", "2026-09-12T10:10:19.517Z")
    return {
        "id": entry_id,
        "at": at,
        "kind": "broker_response",
        "tool": f"mcp__futu__{tool}",
        "mode": "sim",
        "is_error": extra.get("is_error", False),
        "value": {"content": [{"type": "text", "text": text}]},
    }


def _envelope(payload):
    """audit.test.mjs:100：MCP 信封（业务 JSON 是 value.content[].text 里的**字符串**）。"""
    return {"value": {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False,
                                                                     separators=(",", ":"))}]}}


_ORDER = {
    "ret_code": 0, "ret_msg": "success",
    "data": {"orders": [{
        "order_id": "6526051", "symbol": "09961", "stock_name": "携程集团-S",
        "side": 2, "qty": "200", "price": "465.2", "avg_fill_price": "465.2", "cum_qty": "200",
        "status": 4, "create_time": "1768550082000000", "update_time": "1768550371000000",
    }]},
}
_RAW = _ORDER["data"]["orders"][0]


def _order_entry(raw, at=None, entry_id=None):
    extra = {} if entry_id is None else {"id": entry_id}
    if at is not None:
        extra["at"] = at
    return _entry("sim_trade_history_order_list",
                  {"ret_code": 0, "ret_msg": "success", "data": {"orders": [raw]}}, extra)


_SIGNAL_SNAPSHOT = {
    "mode": "sim", "generated_at": "2026-09-11T10:00:00Z", "in_flight": 0, "pending_observations": 0,
    "previews": [
        {"id": "s1", "kind": "signal", "at": "2026-09-10T09:35:00Z",
         "value": {"ticker": "600519", "signal": "BUY", "price": 1275.16, "strategy": "rsi"}},
        {"id": "s2", "kind": "backtest", "at": "2026-09-10T09:40:00Z",
         "value": {"ticker": "600519", "summary": {}}},
    ],
    "activity": [
        {"id": "a1", "at": "2026-09-10T09:36:20Z", "tool": "mcp__futu__sim_trade_input_order",
         "is_error": False,
         "value": {"data": {"code": "SH.600519", "order_id": "998877", "status": "SUBMITTED"}}},
        {"id": "a2", "at": "2026-08-01T09:00:00Z", "tool": "mcp__futu__trading_order_place",
         "is_error": True, "value": {"code": "SH.000001"}},
    ],
}
_SIGNAL_TRADES = {"mode": "sim", "trades": [
    {"date": "2026-09-11", "action": "BUY", "ticker": "600519", "shares": 500, "price": 1275.16,
     "fee": 191.27, "reason": "rsi 信号"},
]}

_AUDIT_ACTIVITY = [
    {"id": "facts", "at": "2026-09-10T09:00:00Z", "tool": "mcp__futu__sim_trade_history_order_list",
     "is_error": False,
     **_envelope({"ret_code": 0, "data": {"orders": [{"symbol": "00700", "order_id": "7137795"}]}})},
    {"id": "cancel", "at": "2026-09-10T09:05:00Z", "tool": "mcp__futu__sim_trade_cancel_order",
     "is_error": False, **_envelope({"ret_code": 0, "data": {"order_id": "7137795"}})},
    {"id": "modify", "at": "2026-09-10T09:06:00Z", "tool": "mcp__futu__sim_trade_modify_order",
     "is_error": True, **_envelope({"ret_code": -5, "ret_msg": "backend business error"})},
    {"id": "place", "at": "2026-09-10T09:07:00Z", "tool": "mcp__futu__sim_trade_input_order",
     "is_error": False, **_envelope({"ret_code": 0, "data": {"order_id": "7137796"}})},
    {"id": "cash", "at": "2026-09-10T09:08:00Z", "tool": "mcp__futu__sim_trade_cash_info",
     "is_error": False, **_envelope({"ret_code": 0, "data": {"cash": 100}})},
    {"id": "positions", "at": "2026-09-10T09:09:00Z", "tool": "mcp__futu__sim_trade_position_list",
     "is_error": False, **_envelope({"ret_code": 0, "data": {"positions": []}})},
]


def _activity():
    """与 Node 探针逐字对应的 activity fixtures（键顺序即出场顺序）。"""
    return {
        "order_facts": [_entry("sim_trade_history_order_list", _ORDER, {"id": "e1"})],
        "side_buy": [_entry("sim_trade_history_order_list",
                            {"ret_code": 0, "data": {"orders": [{**_RAW, "side": 1}]}}, {"id": "e2"})],
        "fill_states": [
            _order_entry({**_RAW, "order_id": "f1", "qty": "100", "cum_qty": "100"}, entry_id="f1"),
            _order_entry({**_RAW, "order_id": "f2", "qty": "100", "cum_qty": "40"}, entry_id="f2"),
            _order_entry({**_RAW, "order_id": "f3", "qty": "100", "cum_qty": "0"}, entry_id="f3"),
            _order_entry({**_RAW, "order_id": "f4", "qty": "0", "cum_qty": "0"}, entry_id="f4"),
        ],
        "dedupe_keeps_last": [
            _order_entry({**_RAW, "status": 2, "cum_qty": "0"}, "2026-09-12T10:09:00.000Z", "early"),
            _order_entry({**_RAW, "status": 4, "cum_qty": "200"}, "2026-09-12T10:12:00.000Z", "late"),
        ],
        "cancel_modify_linkage": [
            _entry("sim_trade_input_order", {"ret_code": 0, "data": {"order_id": "7137731"}},
                   {"id": "o1", "at": "2026-09-12T10:00:00.000Z"}),
            _entry("sim_trade_modify_order", {"ret_code": 0, "data": {"order_id": "7137731"}},
                   {"id": "o2", "at": "2026-09-12T10:01:00.000Z"}),
            _entry("sim_trade_modify_order", {"ret_code": 0, "data": {"order_id": "7137731"}},
                   {"id": "o3", "at": "2026-09-12T10:02:00.000Z"}),
            _entry("sim_trade_cancel_order", {"ret_code": 0, "data": {"order_id": "7137731"}},
                   {"id": "o4", "at": "2026-09-12T10:03:00.000Z"}),
            _entry("sim_trade_history_order_list", {"ret_code": 0, "data": {"orders": [
                {**_RAW, "order_id": "7137731", "cum_qty": "0", "status": 5}]}},
                {"id": "o5", "at": "2026-09-12T10:04:00.000Z"}),
        ],
        "failed_cancel_not_cancelled": [
            _entry("sim_trade_cancel_order", {"ret_code": -5, "ret_msg": "backend business error"},
                   {"is_error": True, "id": "c1"}),
            _order_entry({**_RAW, "order_id": "1", "cum_qty": "0"}, entry_id="c2"),
        ],
        "readonly_queries_count": [
            _entry("sim_trade_account_list", {"ret_code": 0, "data": {}}, {"id": "q1"}),
            _entry("sim_trade_cash_info", {"ret_code": 0, "data": {"mv": "1"}}, {"id": "q2"}),
            _entry("sim_trade_max_buy_sell", {"ret_code": 0, "data": {}}, {"id": "q3"}),
        ],
        "query_ties_and_unknown": [
            _entry("sim_trade_cash_info", {"ret_code": 0, "data": {}}, {"id": "t1"}),
            _entry("sim_trade_account_list", {"ret_code": 0, "data": {}}, {"id": "t2"}),
            _entry("sim_trade_max_buy_sell", {"ret_code": 0, "data": {}}, {"id": "t3"}),
            _entry("sim_trade_max_buy_sell", {"ret_code": 0, "data": {}}, {"id": "t4"}),
            {"id": "t5", "at": "2026-09-12T10:10:19.517Z", "kind": "event", "mode": "sim"},
            {"id": "t6", "at": "2026-09-12T10:10:19.517Z", "kind": "event", "mode": "sim",
             "tool": 7, "is_error": False},
        ],
        "input_order_action": [
            _entry("sim_trade_input_order", {"ret_code": 0, "ret_msg": "success",
                                             "data": {"order_id": "7137730"}}, {"id": "i1"})],
        "cancel_modify_actions": [
            _entry("sim_trade_cancel_order", {"ret_code": 0, "data": {"order_id": "1"}},
                   {"id": "m1", "at": "2026-09-12T11:00:00.000Z"}),
            _entry("sim_trade_modify_order", {"ret_code": 0, "data": {"order_id": "2"}},
                   {"id": "m2", "at": "2026-09-12T11:01:00.000Z"}),
        ],
        "failed_actions_detail": [
            _entry("sim_trade_modify_order", {"ret_code": 0, "ret_msg": "error", "error": {},
                                              "data": {}}, {"is_error": True, "id": "d1"}),
            _entry("sim_trade_account_list", "Error: MCP error -32603: internal error",
                   {"is_error": True, "id": "d2"}),
        ],
        "s_d_envelope": [
            _entry("sim_trade_input_order", {"s": "ok", "d": {"order_id": "999"}}, {"id": "s1"})],
        "ret_code_error": [
            _entry("sim_trade_input_order", {"ret_code": -3, "ret_msg": "invalid parameter"},
                   {"is_error": True, "id": "r1"})],
        "s_not_ok": [
            _entry("sim_trade_input_order", {"s": "error", "d": {"order_id": "1"}}, {"id": "r2"})],
        "non_object_payload": [_entry("sim_trade_input_order", "[1,2,3]", {"id": "r3"})],
        "non_numeric_time": [_order_entry({**_RAW, "create_time": "abc"}, entry_id="n1")],
        "missing_order_id": [
            _entry("sim_trade_history_order_list", {"ret_code": 0, "data": {
                "orders": [{"symbol": "09988", "qty": "1", "cum_qty": "1"}]}}, {"id": "n2"})],
        "orders_sorted_desc": [
            _entry("sim_trade_history_order_list", {"ret_code": 0, "data": {"orders": [
                {**_RAW, "order_id": "1", "create_time": "1700000000000000"},
                {**_RAW, "order_id": "2", "create_time": "1768550082000000"}]}}, {"id": "n3"})],
        "numeric_coercions": [
            _entry("sim_trade_history_order_list", {"ret_code": 0, "data": {"orders": [
                {"order_id": "t1", "symbol": "X", "qty": " 12 ", "cum_qty": "0x10", "price": "",
                 "avg_fill_price": None, "side": "2", "status": "4", "create_time": "0"},
                {"order_id": "t2", "symbol": "Y", "qty": "abc", "cum_qty": "1", "price": "2.5",
                 "avg_fill_price": "3", "side": 3, "status": True, "create_time": "-1"},
                {"order_id": 42, "symbol": "Z", "qty": 1, "cum_qty": 1, "price": 10,
                 "avg_fill_price": 10, "side": 1, "status": 0, "create_time": None},
                {"order_id": "t4", "qty": "2", "cum_qty": "4", "avg_fill_price": "1.005"},
                {"order_id": "t5", "qty": "2", "cum_qty": "0.1", "avg_fill_price": "0.05"},
                {"order_id": "t6", "qty": "2", "cum_qty": "0.1", "avg_fill_price": "0.15"},
            ]}}, {"id": "n4"})],
        # F6：Number() 的字面量语义（0b/0o/0x、拒绝下划线、BOM 与 Python 特有空白）
        "numeric_string_forms": [
            _entry("sim_trade_history_order_list", {"ret_code": 0, "data": {"orders": [
                {"order_id": "b1", "symbol": "X", "qty": "0b101", "cum_qty": "0b101",
                 "price": "0o17", "avg_fill_price": "0o17"},
                {"order_id": "b2", "symbol": "Y", "qty": "1_000", "cum_qty": "1",
                 "price": "0x1_0", "avg_fill_price": "2"},
                {"order_id": "b3", "symbol": "Z", "qty": "1e2", "cum_qty": "0.5e1",
                 "price": " 12 ", "avg_fill_price": "-0x10"},
                {"order_id": "b4", "symbol": "W", "qty": "\ufeff12", "cum_qty": "\u001c12",
                 "price": "01", "avg_fill_price": ".5"},
            ]}}, {"id": "nf1"})],
        # F4：cum_qty × avg_fill_price × 100 恰为 0.5 -> Math.round 得 0.01（Python round 得 0）
        "amount_half_up": [
            _entry("sim_trade_history_order_list", {"ret_code": 0, "data": {"orders": [
                {"order_id": "h1", "symbol": "X", "qty": "1", "cum_qty": "1",
                 "avg_fill_price": "0.005"},
                {"order_id": "h2", "symbol": "Y", "qty": "1", "cum_qty": "1",
                 "avg_fill_price": "0.015"},
                {"order_id": "h3", "symbol": "Z", "qty": "1", "cum_qty": "0.1",
                 "avg_fill_price": "-0.05"},
            ]}}, {"id": "hu1"})],
        # 金额溢出为 Infinity -> JSON.stringify 写 null（Python 不能写出 Infinity 字面量）
        "non_finite_amount": [
            _entry("sim_trade_history_order_list", {"ret_code": 0, "data": {"orders": [
                {"order_id": "o1", "symbol": "X", "qty": "1e308", "cum_qty": "1e308",
                 "avg_fill_price": "1e308"},
            ]}}, {"id": "nf2"})],
        # F1：s 显式为 null（JS `!== undefined` -> String(null)="null" != "ok" -> 失败）
        "s_null_marker": [
            _entry("sim_trade_input_order", {"s": None, "d": {"order_id": "sn-1"}}, {"id": "sm1"})],
        # F2：首个 type=text 的 part 的 text 不是字符串 -> 失败（不得继续往后找）
        "first_text_part_not_string": [{
            "id": "fts1", "at": "2026-09-12T10:10:19.517Z", "kind": "broker_response",
            "tool": "mcp__futu__sim_trade_input_order", "mode": "sim", "is_error": False,
            "value": {"content": [{"type": "text", "text": None},
                                  {"type": "text",
                                   "text": '{"ret_code":0,"data":{"order_id":"nst-1"}}'}]},
        }],
        # F8：`ret=${ret_code}` 走 JS String(number)：1.0 是 "1" 不是 "1.0"
        "ret_code_float": [
            _entry("sim_trade_input_order", {"ret_code": 1.0, "ret_msg": "boom"},
                   {"is_error": True, "id": "rc1"})],
        # F3：array index 上界是 2^32-2（4294967294 仍是整数键，4294967295 不是）
        "query_numeric_keys": [
            _entry("1", {"ret_code": 0, "data": {}}, {"id": "qk1"}),
            _entry("10", {"ret_code": 0, "data": {}}, {"id": "qk2"}),
            _entry("2", {"ret_code": 0, "data": {}}, {"id": "qk3"}),
            _entry("abc", {"ret_code": 0, "data": {}}, {"id": "qk4"}),
            _entry("01", {"ret_code": 0, "data": {}}, {"id": "qk5"}),
            _entry("4294967294", {"ret_code": 0, "data": {}}, {"id": "qk6"}),
            _entry("4294967295", {"ret_code": 0, "data": {}}, {"id": "qk7"}),
        ],
        # tool 字段的 String() 强转：true -> "true"、[] -> ""、7 -> "7"
        "tool_name_coercions": [
            {"id": "tn1", "at": "2026-09-12T10:10:19.517Z", "kind": "event", "mode": "sim",
             "tool": True, "is_error": False},
            {"id": "tn2", "at": "2026-09-12T10:10:19.517Z", "kind": "event", "mode": "sim",
             "tool": [], "is_error": False},
            {"id": "tn3", "at": "2026-09-12T10:10:19.517Z", "kind": "event", "mode": "sim",
             "tool": 7, "is_error": False},
        ],
        "hostile_entries": ["x", [], {"tool": "mcp__futu__sim_trade_cash_info"}],
        "non_dict_elements": ["x", [], 42, True],
        "empty": [],
    }


def _audits():
    """与 Node 探针逐字对应的 buildAuditChain 输入（snapshot/trades，必要时带 maxEntries）。"""
    return {
        "signal_chain": {"snapshot": _SIGNAL_SNAPSHOT, "trades": _SIGNAL_TRADES},
        "readonly_filter": {"snapshot": {**_SIGNAL_SNAPSHOT, "activity": _AUDIT_ACTIVITY},
                            "trades": {"trades": []}},
        "extract_envelope": {"snapshot": {"previews": [], "activity": [
            {"id": "facts", "at": "2026-09-10T09:00:00Z",
             "tool": "mcp__futu__sim_trade_history_order_list", "is_error": False,
             **_envelope({"ret_code": 0, "data": {"orders": [
                 {"symbol": "00700", "order_id": "7137795", "status": "5"}]}})}]},
            "trades": {}},
        "no_plan": {"snapshot": {"previews": [], "activity": [
            {"id": "facts", "at": "2026-09-10T09:00:00Z",
             "tool": "mcp__futu__sim_trade_history_order_list", "is_error": False,
             **_envelope({"ret_code": 0, "data": {"orders": [
                 {"symbol": "00700", "order_id": "7137795"}]}})},
            {"id": "cash", "at": "2026-09-10T09:08:00Z",
             "tool": "mcp__futu__sim_trade_cash_info", "is_error": False,
             **_envelope({"ret_code": 0, "data": {}})}]}, "trades": {}},
        "fallback_ids": {"snapshot": {"previews": [
            {"id": None, "kind": "signal", "at": None,
             "value": {"ticker": "sh.600519", "signal": "buy", "signal_label": "  ",
                       "price": None, "strategy_label": "双均线", "strategy": "ma_cross"}},
            {"id": "kept", "kind": "signal", "at": "2026-09-10",
             "value": {"ticker": "00700", "signal": "WHAT", "price": 0}},
            {"kind": "signal", "at": "2026-09-10T09:00:00Z",
             "value": {"ticker": "600519", "signal": "sell", "price": 1}}], "activity": []},
            "trades": {"trades": [
                {"date": "2026-09-11", "action": "BUY", "action_label": "人工买入",
                 "ticker": "600519", "shares": 0, "price": 0, "fee": 0, "return": 0,
                 "reason": None},
                {"date": None, "action": "WHAT", "ticker": None, "shares": None, "price": None},
                {"date": "2026-09-11T09:00:00+08:00", "action": "SELL", "ticker": "600519",
                 "shares": 1, "price": 2, "fee": None, "return": 0.123456}]}},
        "window_boundary": {"snapshot": {"previews": [
            {"id": "s", "kind": "signal", "at": "2026-09-10T00:00:00Z",
             "value": {"ticker": "600519", "signal": "BUY", "price": 1}}], "activity": [
            {"id": "edge_exact", "at": "2026-09-17T00:00:00Z",
             "tool": "mcp__futu__sim_trade_input_order", "is_error": False,
             **_envelope({"ret_code": 0, "data": {"order_id": "1", "symbol": "600519"}})},
            {"id": "edge_over", "at": "2026-09-17T00:00:00.001Z",
             "tool": "mcp__futu__sim_trade_input_order", "is_error": False,
             **_envelope({"ret_code": 0, "data": {"order_id": "2", "symbol": "600519"}})},
            {"id": "before", "at": "2026-09-09T23:59:00Z",
             "tool": "mcp__futu__sim_trade_input_order", "is_error": False,
             **_envelope({"ret_code": 0, "data": {"order_id": "3", "symbol": "600519"}})},
        ]}, "trades": {"trades": [
            {"date": "2026-09-17", "action": "BUY", "ticker": "600519", "shares": 1, "price": 1},
            {"date": "not-a-date", "action": "BUY", "ticker": "600519", "shares": 1, "price": 1}]}},
        "order_named_unknown": {"snapshot": {"previews": [], "activity": [
            {"id": "u1", "at": "2026-09-10T09:00:00Z", "tool": "mcp__futu__trading_order_place",
             "is_error": False, "value": {"code": "SH.000001"}},
            {"id": "u2", "at": "2026-09-10T09:01:00Z", "tool": "mcp__futu__order_query",
             "is_error": False, "value": {"code": "SH.000001"}},
            {"id": "u3", "at": "2026-09-10T09:02:00Z", "tool": "mcp__futu__order_summary",
             "is_error": False, "value": {}},
            {"id": "u4", "at": "2026-09-10T09:03:00Z", "tool": "mcp__futu__order_account",
             "is_error": False, "value": {}},
            {"id": "u5", "at": "2026-09-10T09:04:00Z", "tool": "OTHER", "is_error": False,
             "value": {}},
            {"id": "u6", "at": "2026-09-10T09:05:00Z", "tool": "mcp__futu__trading_order_place",
             "is_error": True, "value": {}}]}, "trades": {}},
        "empty": {"snapshot": {}, "trades": {}},
        # F11：trades 行缺 shares/price 键 -> JS 插 "undefined"；显式 null -> "null"
        "trade_missing_optional_keys": {
            "snapshot": {"previews": [], "activity": []},
            "trades": {"trades": [
                {"date": "2026-09-11", "action": "BUY", "ticker": "600519"},
                {"date": "2026-09-11", "action": "SELL", "ticker": None, "shares": None,
                 "price": None, "fee": None, "return": None},
            ]}},
        # F9：`(t.return * 100).toFixed(2)` 的平局（0.00125 -> "0.13"）与 ToNumber 强转
        "to_fixed_tie": {
            "snapshot": {"previews": [], "activity": []},
            "trades": {"trades": [
                {"date": "2026-09-11", "action": "BUY", "ticker": "600519", "shares": 1, "price": 1,
                 "return": 0.00125},
                {"date": "2026-09-11", "action": "SELL", "ticker": "600519", "shares": 1, "price": 1,
                 "return": "0.5"},
                {"date": "2026-09-11", "action": "BUY", "ticker": "600519", "shares": 1, "price": 1,
                 "return": "abc"},
            ]}},
        # F10：`strategy_label || strategy` 是**真值**判断（[] 为真），`??` 只看 null/undefined；
        #      同时覆盖 `String(number)`（1e-7 -> "1e-7"）
        "falsy_strategy_label": {
            "snapshot": {"previews": [
                {"id": "f1", "kind": "signal", "at": "2026-09-10T00:00:00Z",
                 "value": {"ticker": "600519", "signal": "BUY", "price": 1e-7,
                           "strategy_label": [], "strategy": "ma_cross"}},
                {"id": "f2", "kind": "signal", "at": "2026-09-11T00:00:00Z",
                 "value": {"ticker": "00700", "signal": "BUY", "price": 1,
                           "strategy_label": "", "strategy": "rsi"}},
                {"id": "f3", "kind": "signal", "at": "2026-09-12T00:00:00Z",
                 "value": {"ticker": "600519", "signal": "SELL", "price": 1,
                           "strategy_label": 0, "strategy": "rsi"}}], "activity": []},
            "trades": {}},
        # F12：`(b.atMs ?? 0) - (a.atMs ?? 0)`——null 当 0，负数 atMs 排在 null 之后
        "negative_epoch_and_null_at": {
            "snapshot": {"previews": [], "activity": [
                {"id": "null_at", "at": None, "tool": "mcp__futu__sim_trade_input_order",
                 "is_error": False, "value": {"data": {"order_id": "n1"}}}]},
            "trades": {"trades": [
                {"date": "1960-01-01", "action": "BUY", "ticker": "600519", "shares": 1,
                 "price": 1}]}},
        # F7：`Date.parse` 的时间值是整数毫秒，亚毫秒按 floor 落到毫秒（含 1970 年前）
        "sub_millisecond_at": {
            "snapshot": {"previews": [
                {"id": "sm", "kind": "signal", "at": "2026-09-10T09:35:00.123456Z",
                 "value": {"ticker": "600519", "signal": "BUY", "price": 1}}], "activity": []},
            "trades": {"trades": [
                {"date": "1960-01-01T00:00:00.0005Z", "action": "BUY", "ticker": "600519",
                 "shares": 1, "price": 1},
                {"date": "2026-09-10T09:35:00.1234567Z", "action": "SELL", "ticker": "600519",
                 "shares": 1, "price": 1}]}},
        # 有意差异 8：`signal-${index}` 的下标是**过滤后**的位置（audit.js:68-70 先 filter 后 map）
        "signal_index_after_filter": {
            "snapshot": {"previews": [
                {"id": "b1", "kind": "backtest", "at": "2026-09-10T00:00:00Z",
                 "value": {"ticker": "600519"}},
                {"kind": "signal", "at": "2026-09-10T01:00:00Z",
                 "value": {"ticker": "600519", "signal": "BUY", "price": 1}}], "activity": []},
            "trades": {}},
        # audit.js:114 `id: a.id`：字段缺失时 JS 是 undefined，JSON.stringify 丢掉整个键
        "missing_activity_id": {
            "snapshot": {"previews": [], "activity": [
                {"at": "2026-09-10T09:00:00Z", "tool": "mcp__futu__sim_trade_input_order",
                 "is_error": False, "value": {"data": {"order_id": "1"}}}]},
            "trades": {}},
        # 有意差异 5：audit 里的非对象元素在 JS 也在 108 行早退，两边都过滤
        "activity_non_objects": {
            "snapshot": {"previews": [], "activity": [
                None, "order", 42, True,
                {"id": "ok", "at": "2026-09-10T09:00:00Z",
                 "tool": "mcp__futu__sim_trade_input_order", "is_error": False,
                 "value": {"data": {"order_id": "1"}}}]},
            "trades": {}},
        "shared_signal": {"snapshot": {"previews": [
            {"id": "s1", "kind": "signal", "at": "2026-09-10T00:00:00Z",
             "value": {"ticker": "600519", "signal": "BUY", "price": 1}},
            {"id": "s2", "kind": "signal", "at": "2026-09-10T06:00:00Z",
             "value": {"ticker": "600519", "signal": "BUY", "price": 1}},
            {"id": "s3", "kind": "signal", "at": "2026-09-09T00:00:00Z",
             "value": {"ticker": "600519", "signal": "SELL", "price": 1}},
            {"id": "s4", "kind": "signal", "at": "2026-09-10T00:00:00Z", "value": []}],
            "activity": [
                {"id": "o1", "at": "2026-09-10T09:00:00Z",
                 "tool": "mcp__futu__sim_trade_input_order", "is_error": False,
                 **_envelope({"ret_code": 0, "data": {"order_id": "1", "symbol": "600519"}})}]},
            "trades": {"trades": []}},
    }


def _max_entries_cases():
    """maxEntries 的等价用例（JS 的 ToIntegerOrInfinity + slice 端点语义）。

    `maxEntries: null` **不在**这张差分表里：那是 audit_chain.py 有意差异 7（Python 的 None
    一律按「没传」-> 120，见 test_max_entries_null_is_the_documented_difference）。
    """
    base = _audits()["signal_chain"]
    return {
        "two": {**base, "maxEntries": 2},
        "zero": {**base, "maxEntries": 0},            # 与 JS 的 null 同结果，可显式表达
        "negative": {**base, "maxEntries": -1},       # 去掉最后一条
        "fractional": {**base, "maxEntries": 2.7},    # ToIntegerOrInfinity -> 2
        "numeric_string": {**base, "maxEntries": "2"},
        "boolean_true": {**base, "maxEntries": True},
        "empty_list": {**base, "maxEntries": []},     # ToNumber([]) -> 0
        "nan_like_object": {**base, "maxEntries": {}},  # ToNumber({}) -> NaN -> 0
        "no_key": {k: v for k, v in base.items()},    # 没传 -> 默认 120
    }


def _field_cases():
    """extractBrokerFields 的用例（audit.test.mjs:59-64/102-121 的输入）。"""
    return {
        "prefix_strip": {"data": {"code": "HK.00700", "order_id": 42, "status": "FILLED"}},
        "suffix_strip": {"symbol": "600519.SH"},
        "non_object": "not an object",
        "envelope": _envelope({"ret_code": 0, "ret_msg": "success",
                               "data": {"orders": [{"symbol": "00700", "order_id": "7137795",
                                                    "status": "5"}]}})["value"],
        "sd_envelope": _envelope({"s": "ok", "d": {"symbol": "HK.09988", "order_id": 42}})["value"],
        "raw_scan": {"code": "SH.600519", "order_id": "1"},
        "null_value": None,
        "array_value": {"items": [{"id": 7, "state": "OPEN"}]},
        "boolean_id": {"id": True, "code": 42, "status": 5},
        # F13：Object.entries 的整数样式键提前（先扫键 "2" 的子树 -> ticker 取 BBB）
        "numeric_string_keys": {"10": {"symbol": "AAA"}, "2": {"symbol": "BBB"}},
        "mixed_key_order": {"b": {"symbol": "FIRST"}, "2": {"symbol": "SECOND"},
                            "10": {"symbol": "THIRD"}, "01": {"symbol": "FOURTH"}},
    }


def _label_cases():
    """labels.js:36-46 zh() 的用例：table / value / fallback（hasFallback 区分「没传」）。

    期望值同样是 Node 真实运行结果（参照物的 labels 分组），不是对源码的二次解读——
    hasOwnProperty 会把 value 强制成对象键，这是 Python 的 `value in dict` 做不到的。
    """
    def case(table, value, fallback=None, has_fallback=False):
        return {"table": table, "value": value, "fallback": fallback,
                "hasFallback": has_fallback}

    return [
        case("SIDE", "1"), case("SIDE", 1), case("SIDE", 1.0), case("SIDE", 2),
        case("SIDE", True), case("SIDE", False), case("SIDE", "true"),
        case("SIDE", "01"), case("SIDE", "1.0"), case("SIDE", 3), case("SIDE", 2.5),
        case("SIDE", [1]), case("SIDE", []), case("SIDE", {"a": 1}),
        case("SIDE", "1", "其他", True),
        case("ACTION", "BUY"), case("ACTION", "buy"), case("ACTION", "Buy"),
        case("ACTION", "WHAT"), case("ACTION", "WHAT", "其他", True),
        case("ACTION", None), case("ACTION", None, "其他", True),
        case("NO_SUCH_TABLE", "X"),
        case("SIGNAL", "hold"), case("METRIC", "SHARPE"), case("TRADE_TYPE", "Buy"),
    ]


def _activity_cases_meta():
    """用户可读的 case 说明与 JS 出处（断言失败时一起打印）。"""
    return {
        "order_facts": "broker_trades.js:130-142 订单事实 + test 33-45 字段",
        "side_buy": "broker_trades.js:26/91-97 SIDE_LABELS：1=买入",
        "fill_states": "broker_trades.js:71-76 fillState 四态",
        "dedupe_keeps_last": "broker_trades.js:140-141 按 order_id 保留最后观测",
        "cancel_modify_linkage": "broker_trades.js:168-179 撤单/改单生命周期补全",
        "failed_cancel_not_cancelled": "broker_trades.js:148 ok = parsed.ok && !is_error",
        "readonly_queries_count": "broker_trades.js:161-163 查询只计数",
        "query_ties_and_unknown": "broker_trades.js:186-190 queries 排序与 unknown",
        "input_order_action": "broker_trades.js:146-159 动作记录 order_id",
        "cancel_modify_actions": "ORDER_ACTIONS 三个动作标签",
        "failed_actions_detail": "broker_trades.js:149-156 失败原因与 errors 计数",
        "s_d_envelope": "broker_trades.js:46-53 s/d 信封",
        "ret_code_error": "broker_trades.js:46-48 ret_code != 0",
        "s_not_ok": "broker_trades.js:49-51 s !== ok",
        "non_object_payload": "broker_trades.js:45 返回不是对象",
        "non_numeric_time": "broker_trades.js:79-84 microTime 不编造时间",
        "missing_order_id": "broker_trades.js:93/138-139 无 order_id 丢弃",
        "orders_sorted_desc": "broker_trades.js:181-182 orders 按时间倒序",
        "numeric_coercions": "broker_trades.js:65-68 numeric + 86-110 orderRow 原文语义",
        "numeric_string_forms": "broker_trades.js:65-68 Number()：0b/0o/0x、拒绝 1_000 下划线、BOM 空白",
        "amount_half_up": "broker_trades.js:103 Math.round 半数向 +∞（0.005×100 恰为 0.5）",
        "non_finite_amount": "broker_trades.js:103 金额溢出 -> JSON.stringify 写 null",
        "s_null_marker": "broker_trades.js:49 `parsed.s !== undefined`：显式 null 判失败",
        "first_text_part_not_string": "broker_trades.js:36-37 find 首个 text part 后判类型",
        "ret_code_float": "broker_trades.js:47 `${parsed.ret_code}` 走 String(number)",
        "query_numeric_keys": "broker_trades.js:189 对象整数样式键提前（2^32-2 上界）",
        "tool_name_coercions": "broker_trades.js:57 `String(entry?.tool ?? '')`",
        "hostile_entries": "broker_trades.js:119/126-164 非对象行按「无名查询」处理",
        "non_dict_elements": "broker_trades.js:119 rows 里的非对象元素",
        "empty": "broker_trades.js:166-174 空输入零事实",
    }


def _audit_cases_meta():
    return {
        "signal_chain": "audit.js:67-188 三级链（信号→下单→成交）",
        "readonly_filter": "audit.js:101-108 READ_ONLY_QUERY 过滤只读查询",
        "extract_envelope": "audit.js:35-60 解开 MCP 信封取标的",
        "no_plan": "audit.js:96 只收与订单有关的响应",
        "fallback_ids": "audit.js:70-95 id 兜底/标签回退/toFixed",
        "window_boundary": "audit.js:13/142-152 7 天窗口边界（含与含前）",
        "order_named_unknown": "audit.js:107-112 order 命名但只读后缀被排除",
        "empty": "audit.js:66-73 空输入",
        "trade_missing_optional_keys": "audit.js:90 缺失 -> undefined 与显式 null 的模板差异",
        "to_fixed_tie": "audit.js:92 `(t.return * 100).toFixed(2)` 平局与 ToNumber 强转",
        "falsy_strategy_label": "audit.js:77 `||` 真值 + `??` 空值合并（空数组为真）",
        "negative_epoch_and_null_at": "audit.js:166 `(b.atMs ?? 0) - (a.atMs ?? 0)` null 当 0",
        "sub_millisecond_at": "audit.js:18-23 Date.parse 的时间值是整数毫秒（亚毫秒 floor）",
        "signal_index_after_filter": "audit.js:68-70 filter 后再 map，兜底 id 用过滤后下标",
        "missing_activity_id": "audit.js:114 `id: a.id` 缺失 -> JSON.stringify 丢键",
        "activity_non_objects": "audit.js:104-108 非对象元素在 108 行早退（不抛）",
        "shared_signal": "audit.js:141-152 取最近的不晚于它的信号",
    }


# ---------------------------------------------------------------- Node 参照物
# 生成方式见文件头。压缩后存入，避免 8 万字符的 JSON 字面量把测试文件淹没。
_NODE_REFERENCE_B64 = (
    "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"
    "tz7/b4Px+OHx2ffng9qa1vX/l/ti9oXg=="
)


def _node_reference():
    return json.loads(zlib.decompress(base64.b64decode(_NODE_REFERENCE_B64)).decode("utf-8"))


def _leaf_kind(value):
    """叶子类型描述（与 Node 探针的 shapeOf 对叶子端的取名一致）。

    数值分 integer/float 两种：JS 侧按 `Number.isInteger(v) && Math.abs(v) < 1e21` 判定
    （即 JSON.stringify 是否走整数形式），Python 侧按 int/float 判定。补遗 B F7 要求所有
    **计算产出**的整数值返回 int，这里就是那道防回归的闸门。
    """
    if value is None:
        return "null"
    if value is True or value is False:
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    raise AssertionError(f"形状守护遇到意外叶子：{value!r}")


def _assert_keys(owner, where, expected, actual):
    """递归键集比较：expected 来自 Node，actual 是 Python 输出。"""
    if isinstance(expected, dict):
        owner.assertIsInstance(actual, dict, f"{where}: Python 侧不是对象")
        owner.assertEqual(set(actual), set(expected), f"{where}: 键集合不同")
        for key in expected:
            _assert_keys(owner, f"{where}.{key}", expected[key], actual[key])
        return
    if isinstance(expected, list):
        owner.assertIsInstance(actual, list, f"{where}: Python 侧不是数组")
        owner.assertEqual(len(actual), len(expected), f"{where}: 数组长度不同")
        for index, item in enumerate(expected):
            _assert_keys(owner, f"{where}[{index}]", item, actual[index])
        return
    # 叶子：Node 侧的类型描述（string/number/boolean/null/...）必须与 Python 侧一致
    owner.assertEqual(expected, _leaf_kind(actual), f"{where}: 叶子类型不同")


class NodeParityBase(unittest.TestCase):
    """差分底座：Python 输出必须与 Node 参照物逐字段相等（含键集递归守护）。"""

    @classmethod
    def setUpClass(cls):
        cls.reference = _node_reference()


class SummaryDifferentialTest(NodeParityBase):
    def test_node_reference_parity(self):
        """每组 activity fixture：Python summarize() == Node summarizeBrokerActivity()。"""
        reference = self.reference["summaries"]
        cases = _activity()
        self.assertEqual(set(cases), set(reference), "fixture 名与参照物不一致（探针未同步）")
        for name, activity in cases.items():
            with self.subTest(case=name, source=_activity_cases_meta()[name]):
                self.assertEqual(summary.summarize(activity), reference[name])

    def test_node_shape_parity(self):
        """递归键集守护：每个 fixture 的键树（含数组元素与 null 位置）与 Node 相同。"""
        reference = self.reference["summaryShapes"]
        for name, activity in _activity().items():
            with self.subTest(case=name):
                _assert_keys(self, f"summaries.{name}", reference[name], summary.summarize(activity))


class AuditDifferentialTest(NodeParityBase):
    def _run(self, name, inputs):
        # 键不存在 == JS 的 undefined（默认 120）；键存在且为 null == JS 的 null（0 条）
        max_entries = inputs["maxEntries"] if "maxEntries" in inputs else None
        return ac.build_audit_chain(inputs["snapshot"], inputs.get("trades"), max_entries)

    def test_node_reference_parity(self):
        """每组 snapshot/trades：Python build_audit_chain() == Node buildAuditChain()。"""
        reference = self.reference["audits"]
        cases = _audits()
        self.assertEqual(set(cases), set(reference), "fixture 名与参照物不一致（探针未同步）")
        for name, inputs in cases.items():
            with self.subTest(case=name, source=_audit_cases_meta()[name]):
                self.assertEqual(self._run(name, inputs), reference[name])

    def test_node_shape_parity(self):
        reference = self.reference["auditShapes"]
        for name, inputs in _audits().items():
            with self.subTest(case=name):
                _assert_keys(self, f"audits.{name}", reference[name], self._run(name, inputs))

    def test_max_entries_parameter(self):
        """audit.js:167 `.slice(0, maxEntries)`：截断、null、负数、小数、字符串、布尔与缺省。"""
        reference = self.reference["auditsMaxEntries"]
        for name, inputs in _max_entries_cases().items():
            with self.subTest(case=name):
                self.assertEqual(self._run(name, inputs), reference[name])
        cases = _max_entries_cases()
        lengths = {name: len(self._run(name, inputs)["entries"])
                   for name, inputs in cases.items()}
        self.assertEqual(lengths["empty_list"], 0)
        self.assertEqual(lengths["nan_like_object"], 0)
        self.assertEqual(lengths["no_key"], 4)          # 缺省 120，四条全留
        self.assertEqual(lengths["two"], 2)
        self.assertEqual(lengths["negative"], 3)
        self.assertEqual(lengths["boolean_true"], 1)
        self.assertEqual(lengths["fractional"], 2)
        self.assertEqual(lengths["numeric_string"], 2)

    def test_max_entries_null_is_the_documented_difference(self):
        """有意差异 7：Node 实测 `maxEntries: null` -> `.slice(0, null)` -> 0 条；
        Python 的 None 按「没传」-> 120 -> 4 条（理由见 audit_chain.py：`payload.get(...)`
        在客户端没传字段时也是 None，当 null 用会静默清空时间线）。要 null 语义传 0。
        """
        inputs = {**_audits()["signal_chain"], "maxEntries": None}
        self.assertEqual(len(self._run("explicit_null", inputs)["entries"]), 4)
        self.assertEqual(len(self._run("zero", {**_audits()["signal_chain"], "maxEntries": 0})["entries"]), 0)

    def test_extract_broker_fields(self):
        """audit.js:35-60 extractBrokerFields：前缀/后缀剥除、信封解开、非对象回退。"""
        reference = self.reference["fields"]
        cases = _field_cases()
        self.assertEqual(set(cases), set(reference), "fixture 名与参照物不一致（探针未同步）")
        for name, value in cases.items():
            with self.subTest(case=name):
                self.assertEqual(ac.extract_broker_fields(value), reference[name])

    def test_extract_field_key_sets(self):
        for name in self.reference["fields"]:
            with self.subTest(case=name):
                keys = ac.extract_broker_fields(_field_cases()[name]).keys()
                self.assertEqual(set(keys), {"ticker", "status", "orderId"})

    def test_label_lookup_parity(self):
        """labels.js:36-46 zh()：hasOwnProperty 的键强制转换也是差分项。"""
        reference = self.reference["labels"]
        cases = _label_cases()
        self.assertEqual(len(cases), len(reference), "labels fixture 数与参照物不一致")
        for index, case in enumerate(cases):
            with self.subTest(case=(case["table"], case["value"], case["fallback"])):
                result = (labels_py.zh(case["table"], case["value"], case["fallback"])
                          if case["hasFallback"]
                          else labels_py.zh(case["table"], case["value"]))
                expected = reference[index]
                if expected["has"]:
                    self.assertEqual(result, expected["value"])
                else:
                    # JS 返回 undefined；Python 用 None 表示（没有用例返回 JS null）
                    self.assertIsNone(result)


class SummaryBehaviourTest(unittest.TestCase):
    """Node 套件已声明的行为，逐条钉成 Python 断言（不依赖参照物，读起来即规格）。"""

    def test_order_facts_fields(self):
        summary_result = summary.summarize(_activity()["order_facts"])
        self.assertEqual(len(summary_result["orders"]), 1)
        row = summary_result["orders"][0]
        self.assertEqual(row["order_id"], "6526051")
        self.assertEqual(row["symbol"], "09961")
        self.assertEqual(row["name"], "携程集团-S")
        self.assertEqual(row["side"], "卖出")          # schema 明文：order_side 1=Buy 2=Sell
        self.assertEqual(row["qty"], 200)
        self.assertEqual(row["filled_qty"], 200)
        self.assertEqual(row["fill"], "全部成交")
        self.assertEqual(row["amount"], 93040)         # 200 × 465.2，全部来自券商原文
        self.assertEqual(row["status_code"], 4)        # 状态原码保留，不猜标签
        # F7：计算产出的整数值必须是 int（JSON.stringify 写 93040 而不是 93040.0）
        for key in ("qty", "filled_qty", "side_code", "status_code", "amount"):
            self.assertIsInstance(row[key], int, f"{key} 应为 int")
        self.assertIsInstance(row["price"], float)   # 465.2 不是整数，保持 float
        self.assertEqual(row["ordered_at"], "2026-01-16T07:54:42.000Z")
        self.assertEqual(row["updated_at"], "2026-01-16T07:59:31.000Z")

    def test_side_one_is_buy(self):
        row = summary.summarize(_activity()["side_buy"])["orders"][0]
        self.assertEqual(row["side"], "买入")

    def test_fill_state_from_quantities_only(self):
        orders = summary.summarize(_activity()["fill_states"])["orders"]
        by_id = {row["order_id"]: row for row in orders}
        self.assertEqual(by_id["f1"]["fill"], "全部成交")
        self.assertEqual(by_id["f2"]["fill"], "部分成交")
        self.assertEqual(by_id["f3"]["fill"], "未成交")
        self.assertEqual(by_id["f4"]["fill"], "未知")   # qty 0 -> 未知，而不是「未成交」
        for row in orders:
            self.assertIsInstance(row["status_code"], (int, float))

    def test_amount_uses_js_math_round_not_bankers_rounding(self):
        """broker_trades.js:103 `Math.round(x*100)/100`：0.1×0.05×100 = 0.5 -> 0.01（不是 0）。"""
        rows = {row["order_id"]: row for row in
                summary.summarize(_activity()["numeric_coercions"])["orders"]}
        self.assertEqual(rows["t4"]["amount"], 4.02)
        self.assertEqual(rows["t5"]["amount"], 0.01)   # Python round() 会得 0.0
        self.assertEqual(rows["t6"]["amount"], 0.02)

    def test_amount_half_up_on_exact_tie(self):
        """F4 鉴别力：cum_qty×avg_fill_price×100 **恰为** 0.5 时才区分 Math.round 与 round()。

        numeric_coercions 的 t5 是 0.1×0.05×100 = 0.5000000000000001，银行家舍入也进到 1，
        所以它证明不了 _js_round。这里 h1 是 1×0.005×100 = 0.5（二进制精确），JS 得 0.01、
        Python round() 得 0.0；h3 是 0.1×(-0.05)×100 = -0.5000000000000001 -> -0.01。
        """
        rows = {row["order_id"]: row for row in
                summary.summarize(_activity()["amount_half_up"])["orders"]}
        self.assertEqual(rows["h1"]["amount"], 0.01)
        self.assertEqual(rows["h2"]["amount"], 0.02)
        self.assertEqual(rows["h3"]["amount"], -0.01)

    def test_non_finite_amount_serializes_as_null(self):
        """broker_trades.js:103 溢出成 Infinity 时，JSON.stringify 写的是 null（F7）。"""
        row = summary.summarize(_activity()["non_finite_amount"])["orders"][0]
        self.assertIsNone(row["amount"])
        self.assertEqual(row["fill"], "全部成交")
        self.assertEqual(row["qty"], 1e308)          # 文件透传的大数仍是 float
        self.assertIsInstance(row["qty"], float)

    def test_ret_code_uses_js_number_string(self):
        """F8：`ret=${ret_code} ${ret_msg ?? ""}`——JSON 里的 1.0 是 JS 数值 1，文案 "ret=1"."""
        action = summary.summarize(_activity()["ret_code_float"])["actions"][0]
        self.assertFalse(action["ok"])
        self.assertEqual(action["detail"], "ret=1 boom")

    def test_explicit_null_s_marker_fails(self):
        """F1：broker_trades.js:49 是 `parsed.s !== undefined`，显式 null 也算「存在」."""
        actions = summary.summarize(_activity()["s_null_marker"])["actions"]
        self.assertEqual(len(actions), 1)
        self.assertFalse(actions[0]["ok"])
        self.assertIsNone(actions[0]["order_id"])
        self.assertEqual(actions[0]["detail"], "s=null")
        self.assertEqual(summary.summarize(_activity()["s_null_marker"])["counts"]["errors"], 1)

    def test_content_uses_first_text_part(self):
        """F2：audit 同款 find 语义——首个 text part 的 text 不是字符串就失败，不继续往后找."""
        entry = _activity()["first_text_part_not_string"][0]
        self.assertEqual(summary.business_data(entry), {"ok": False, "reason": "无文本内容"})
        action = summary.summarize(_activity()["first_text_part_not_string"])["actions"][0]
        self.assertFalse(action["ok"])
        self.assertEqual(action["detail"], "无文本内容")

    def test_numeric_follows_js_number_literals(self):
        """F6：0b101=5、0o17=15、1_000/-0x10/0x1_0 是 NaN、BOM 是空白而 \\x1c 不是。"""
        rows = {row["order_id"]: row for row in
                summary.summarize(_activity()["numeric_string_forms"])["orders"]}
        self.assertEqual((rows["b1"]["qty"], rows["b1"]["filled_qty"], rows["b1"]["price"]), (5, 5, 15))
        self.assertEqual(rows["b1"]["amount"], 75)
        self.assertIsNone(rows["b2"]["qty"])          # "1_000" -> NaN（Python float() 会给 1000）
        self.assertIsNone(rows["b2"]["price"])        # "0x1_0" -> NaN
        self.assertEqual(rows["b2"]["avg_fill_price"], 2)
        self.assertEqual((rows["b3"]["qty"], rows["b3"]["filled_qty"]), (100, 5))
        self.assertIsNone(rows["b3"]["avg_fill_price"])   # "-0x10" -> NaN（不带符号的非十进制）
        self.assertEqual(rows["b4"]["qty"], 12)           # "\ufeff12"：JS 把 BOM 当空白
        self.assertIsNone(rows["b4"]["filled_qty"])       # "\x1c12"：JS 不把 \x1c 当空白

    def test_query_tool_order_array_index_upper_bound(self):
        """F3：array index 是 0 ≤ n < 2^32-1，所以 4294967294 仍提前、4294967295 不提前。"""
        tools = summary.summarize(_activity()["query_numeric_keys"])["queries"]["tools"]
        self.assertEqual([row["tool"] for row in tools],
                         ["1", "2", "10", "4294967294", "abc", "01", "4294967295"])

    def test_tool_name_uses_string_coercion(self):
        result = summary.summarize(_activity()["tool_name_coercions"])
        self.assertEqual([row["tool"] for row in result["queries"]["tools"]],
                         ["7", "true", "unknown"])
        self.assertEqual(summary.tool_name({"tool": True}), "true")
        self.assertEqual(summary.tool_name({"tool": []}), "")

    def test_dedupe_keeps_last_observation(self):
        result = summary.summarize(_activity()["dedupe_keeps_last"])
        self.assertEqual(len(result["orders"]), 1, "重复订单不得重复计数")
        self.assertEqual(result["orders"][0]["status_code"], 4)
        self.assertEqual(result["orders"][0]["fill"], "全部成交")
        self.assertEqual(result["counts"]["order_responses"], 2)
        self.assertEqual(result["counts"]["orders"], 1)

    def test_cancel_and_modify_lifecycle(self):
        row = summary.summarize(_activity()["cancel_modify_linkage"])["orders"][0]
        self.assertTrue(row["cancelled"])
        self.assertEqual(row["modified_count"], 2)
        self.assertEqual(row["fill"], "未成交")

    def test_failed_cancel_is_not_cancelled(self):
        result = summary.summarize(_activity()["failed_cancel_not_cancelled"])
        self.assertFalse(result["orders"][0]["cancelled"])
        self.assertEqual(result["counts"]["errors"], 1)

    def test_readonly_queries_are_counted_not_listed(self):
        result = summary.summarize(_activity()["readonly_queries_count"])
        self.assertEqual(result["orders"], [])
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["queries"]["count"], 3)
        self.assertEqual(sorted(row["tool"] for row in result["queries"]["tools"]),
                         ["sim_trade_account_list", "sim_trade_cash_info", "sim_trade_max_buy_sell"])
        self.assertEqual(result["counts"]["responses"], 3)

    def test_query_tool_order_follows_js_key_enumeration(self):
        """计数并列时按插入序；`tool: 7` 被 String() 成 "7"（JS 对象整数键提前枚举）。"""
        tools = summary.summarize(_activity()["query_ties_and_unknown"])["queries"]["tools"]
        self.assertEqual(tools, [{"tool": "sim_trade_max_buy_sell", "count": 2},
                                 {"tool": "7", "count": 1},
                                 {"tool": "sim_trade_cash_info", "count": 1},
                                 {"tool": "sim_trade_account_list", "count": 1},
                                 {"tool": "unknown", "count": 1}])

    def test_action_records_order_id(self):
        result = summary.summarize(_activity()["input_order_action"])
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["action"], "下单")
        self.assertTrue(result["actions"][0]["ok"])
        self.assertEqual(result["actions"][0]["order_id"], "7137730")
        self.assertEqual(result["orders"], [], "下单响应本身不是订单事实")

    def test_cancel_and_modify_actions_are_labelled(self):
        actions = summary.summarize(_activity()["cancel_modify_actions"])["actions"]
        self.assertEqual({row["action"] for row in actions}, {"撤单", "改单"})
        # 按 at 倒序（两者的 at 相隔 1 分钟）
        self.assertEqual([row["action"] for row in actions], ["改单", "撤单"])

    def test_failed_actions_keep_reason_and_count_errors(self):
        result = summary.summarize(_activity()["failed_actions_detail"])
        self.assertEqual(len(result["actions"]), 1)
        self.assertFalse(result["actions"][0]["ok"])
        self.assertTrue(result["actions"][0]["detail"], "失败必须带原因")
        self.assertEqual(result["counts"]["errors"], 2)
        self.assertEqual(result["queries"]["count"], 1)

    def test_business_error_is_not_success(self):
        action = summary.summarize(_activity()["ret_code_error"])["actions"][0]
        self.assertFalse(action["ok"])
        self.assertIn("invalid parameter", action["detail"])

    def test_unparseable_time_is_not_invented(self):
        self.assertIsNone(summary.summarize(_activity()["non_numeric_time"])["orders"][0]["ordered_at"])

    def test_missing_order_id_rows_are_dropped(self):
        self.assertEqual(summary.summarize(_activity()["missing_order_id"])["orders"], [])

    def test_orders_sorted_by_time_desc(self):
        orders = summary.summarize(_activity()["orders_sorted_desc"])["orders"]
        self.assertEqual([row["order_id"] for row in orders], ["2", "1"])

    def test_empty_input_has_no_facts(self):
        result = summary.summarize([])
        self.assertEqual(result["orders"], [])
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["queries"], {"count": 0, "tools": []})
        self.assertEqual(result["counts"], {"responses": 0, "order_responses": 0, "orders": 0,
                                            "actions": 0, "errors": 0})
        self.assertIn("不是券商成交推送", result["notice"])
        self.assertEqual(result["notice"], summary.NOTICE)

    def test_non_array_inputs_are_empty(self):
        """Node 套件只覆盖 undefined/null；Python 是服务端，额外容忍 dict/字符串/整数。"""
        for value in (None, {}, "x", 7, True):
            with self.subTest(value=value):
                result = summary.summarize(value)
                self.assertEqual(result["orders"], [])
                self.assertEqual(result["queries"]["count"], 0)
                self.assertEqual(result["counts"]["responses"], 0)

    def test_null_activity_entry_is_tolerated(self):
        """有意差异：broker_trades.js:163 对 null 元素 `entry.is_error` 抛 TypeError，
        Python 侧 _js.field() 取不到字段 -> 按无名查询计数，服务进程不该因一条脏记录 500。"""
        result = summary.summarize([None])
        self.assertEqual(result["queries"], {"count": 1, "tools": [{"tool": "unknown", "count": 1}]})
        self.assertEqual(result["counts"], {"responses": 1, "order_responses": 0, "orders": 0,
                                            "actions": 0, "errors": 0})

    def test_null_order_element_is_dropped(self):
        """有意差异：broker_trades.js:87 `raw.qty` 对 null 结算行抛 TypeError（没有可选链），
        Python 侧取不到 order_id 直接丢弃（F5 的同一族注释）。"""
        activity = [_entry("sim_trade_history_order_list", {"ret_code": 0, "data": {
            "orders": [None, {"order_id": "ok-1", "symbol": "X", "qty": "1", "cum_qty": "1"}]}},
            {"id": "no1"})]
        result = summary.summarize(activity)
        self.assertEqual([row["order_id"] for row in result["orders"]], ["ok-1"])
        self.assertEqual(result["counts"]["errors"], 0)

    def test_business_data_envelopes(self):
        one = {"value": {"content": [{"type": "text", "text": '{"ret_code":0,"data":{"a":1}}'}]}}
        self.assertEqual(summary.business_data(one), {"ok": True, "data": {"a": 1}})
        sd = {"value": {"content": [{"type": "text", "text": '{"s":"ok","d":{"b":2}}'}]}}
        self.assertEqual(summary.business_data(sd), {"ok": True, "data": {"b": 2}})
        # data 显式为 null 时回退到 d（JS `parsed.data ?? parsed.d ?? {}`）
        nulls = {"value": {"content": [{"type": "text", "text": '{"data":null,"d":{"c":3}}'}]}}
        self.assertEqual(summary.business_data(nulls), {"ok": True, "data": {"c": 3}})
        self.assertEqual(summary.business_data({"value": {"content": []}}),
                         {"ok": False, "reason": "无文本内容"})
        self.assertEqual(summary.business_data({"value": {"content": [{"type": "image"}]}}),
                         {"ok": False, "reason": "无文本内容"})
        self.assertEqual(summary.business_data(
            {"value": {"content": [{"type": "text", "text": "Error: MCP error -32603"}]}}),
            {"ok": False, "reason": "Error: MCP error -32603"})

    def test_tool_name_strips_only_the_mcp_prefix(self):
        self.assertEqual(summary.tool_name({"tool": "mcp__futu__sim_trade_cash_info"}),
                         "sim_trade_cash_info")
        self.assertEqual(summary.tool_name({"tool": "OTHER"}), "OTHER")
        self.assertEqual(summary.tool_name({}), "")
        self.assertEqual(summary.tool_name({"tool": 7}), "7")
        self.assertEqual(summary.tool_name(None), "")

    def test_action_label_matches_three_suffixes(self):
        self.assertEqual(summary.action_label("sim_trade_input_order"), "下单")
        self.assertEqual(summary.action_label("sim_trade_modify_order"), "改单")
        self.assertEqual(summary.action_label("sim_trade_cancel_order"), "撤单")
        self.assertIsNone(summary.action_label("sim_trade_history_order_list"))


class AuditBehaviourTest(unittest.TestCase):
    def test_signal_order_fill_chain(self):
        chain = ac.build_audit_chain(_SIGNAL_SNAPSHOT, _SIGNAL_TRADES)
        self.assertEqual(chain["stats"]["signals"], 1, "只有 signal 预览进入链路")
        self.assertEqual(chain["stats"]["orders"], 2)
        self.assertEqual(chain["stats"]["fills"], 1)
        self.assertEqual(chain["stats"]["linked"], 2)
        self.assertEqual(chain["stats"]["unlinked"], 1, "8 月那笔超出 7 天窗口")
        self.assertEqual(chain["stats"]["order_kinds"], {"下单": 1, "订单工具": 1})
        self.assertEqual(chain["stats"]["link_rule"], "同标的、信号时间在前且间隔 ≤ 7 天")

        fill = next(entry for entry in chain["entries"] if entry["kind"] == "fill")
        self.assertEqual(fill["signal_id"], "s1")
        self.assertTrue(fill["linked"])
        self.assertEqual(fill["lag_hours"], 14)
        self.assertEqual(fill["detail"], "买入 500 @ 1275.16 · 费用 191.27")

        error = next(entry for entry in chain["entries"] if entry["kind"] == "order-error")
        self.assertFalse(error["linked"])
        self.assertIsNone(error["signal_id"])
        self.assertEqual(error["at"], "2026-08-01T09:00:00Z")

    def test_signals_are_origin_and_sorted_newest_first(self):
        chain = ac.build_audit_chain(_SIGNAL_SNAPSHOT, _SIGNAL_TRADES)
        signal = next(entry for entry in chain["entries"] if entry["kind"] == "signal")
        self.assertTrue(signal["origin"])
        self.assertEqual(signal["source"], "quant_signal")
        self.assertEqual(signal["source_label"], "量化信号")
        self.assertEqual(signal["detail"], "买入 @ 1275.16 · rsi")
        stamps = [entry["atMs"] or 0 for entry in chain["entries"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True), "应按时间倒序")

    def test_readonly_queries_are_not_order_responses(self):
        chain = ac.build_audit_chain({**_SIGNAL_SNAPSHOT, "activity": _AUDIT_ACTIVITY},
                                     {"trades": []})
        self.assertEqual(chain["stats"]["orders"], 4, "现金/持仓查询不是交易事实")
        self.assertEqual(chain["stats"]["order_kinds"],
                         {"订单查询": 1, "撤单": 1, "改单": 1, "下单": 1})
        kinds = sorted(entry["kind"] for entry in chain["entries"] if entry["kind"] != "signal")
        self.assertEqual(kinds, ["order", "order-cancel", "order-error", "order-facts"])

    def test_ticker_backfilled_by_order_id(self):
        chain = ac.build_audit_chain({**_SIGNAL_SNAPSHOT, "activity": _AUDIT_ACTIVITY},
                                     {"trades": []})
        cancel = next(entry for entry in chain["entries"] if entry["kind"] == "order-cancel")
        self.assertEqual(cancel["ticker"], "00700")
        self.assertEqual(cancel["ticker_from"], "order_id")
        failed = next(entry for entry in chain["entries"] if entry["kind"] == "order-error")
        self.assertIsNone(failed["ticker"], "失败改单没有 order_id，如实留空而不是猜")
        facts = next(entry for entry in chain["entries"] if entry["kind"] == "order-facts")
        self.assertEqual(facts["action"], "订单查询")
        self.assertNotIn("ticker_from", facts)

    def test_order_named_tools_and_readonly_suffixes(self):
        chain = ac.build_audit_chain(**_audits()["order_named_unknown"])
        kinds = sorted(entry["kind"] for entry in chain["entries"])
        self.assertEqual(kinds, ["order-error", "order-other"])
        other = next(entry for entry in chain["entries"] if entry["kind"] == "order-other")
        self.assertEqual(other["action"], "订单工具")
        self.assertEqual(other["detail"], "trading_order_place")
        self.assertEqual(chain["stats"]["orders"], 2, "order_query/_summary/_account 被排除")

    def test_empty_input_returns_empty_chain(self):
        chain = ac.build_audit_chain({}, {})
        self.assertEqual(chain["entries"], [])
        self.assertEqual(chain["stats"], {"signals": 0, "orders": 0, "order_kinds": {},
                                          "fills": 0, "linked": 0, "unlinked": 0,
                                          "link_rule": "同标的、信号时间在前且间隔 ≤ 7 天"})
        self.assertEqual(ac.build_audit_chain({}, {}), chain, "空 dict 必须等价于空输入")

    def test_trades_without_snapshot_still_returns_chain(self):
        """audit.test.mjs:87-95 的等价物：台账可读、快照空时链路仍在。"""
        chain = ac.build_audit_chain({}, _SIGNAL_TRADES)
        self.assertEqual(chain["stats"]["fills"], 1)
        self.assertEqual(chain["stats"]["orders"], 0)
        self.assertEqual(chain["entries"][0]["kind"], "fill")

    def test_cyclic_value_terminates_without_fields(self):
        """有意差异：JS 靠 JSON.parse 爆栈 + visited<200 兜底，Python 用幂等集合有界终止。"""
        cyclic = {}
        cyclic["self"] = cyclic
        chain = ac.build_audit_chain(
            {"previews": [{}], "activity": [{"tool": "order", "value": cyclic}]}, {})
        self.assertEqual(chain["stats"]["orders"], 1)
        self.assertIsNone(chain["entries"][0]["ticker"])
        self.assertIsNone(chain["entries"][0]["order_id"])

    def test_trade_detail_distinguishes_missing_from_null(self):
        """F11：audit.js:90 `${t.shares} @ ${t.price}` 对缺失键插 "undefined"，显式 null 插 "null"."""
        chain = ac.build_audit_chain(**_audits()["trade_missing_optional_keys"])
        details = [entry["detail"] for entry in chain["entries"]]
        self.assertIn("买入 undefined @ undefined", details)
        self.assertIn("卖出 null @ null", details)

    def test_to_fixed_ties_round_like_js(self):
        """F9：`(t.return * 100).toFixed(2)`——0.00125 -> "0.13"（Python f-string 会得 "0.12"），
        并且 `* 100` 是 ToNumber 强转（"0.5" -> 50、"abc" -> NaN -> "NaN"）。"""
        chain = ac.build_audit_chain(**_audits()["to_fixed_tie"])
        details = [entry["detail"] for entry in chain["entries"]]
        self.assertIn("买入 1 @ 1 · 收益 0.13%", details)
        self.assertIn("卖出 1 @ 1 · 收益 50.00%", details)
        self.assertIn("买入 1 @ 1 · 收益 NaN%", details)

    def test_falsy_strategy_label_uses_js_truthiness(self):
        """F10：`strategy_label || strategy` 里 [] 为真，而 `${strategy_label ?? strategy}` 取 [] -> ""."""
        chain = ac.build_audit_chain(**_audits()["falsy_strategy_label"])
        details = {entry["id"]: entry["detail"] for entry in chain["entries"]}
        self.assertEqual(details["f1"], "买入 @ 1e-7 · ")   # [] 为真 -> 拼 " · "，模板给空串
        self.assertEqual(details["f2"], "买入 @ 1 · ")      # "" || "rsi" -> 真，但 ?? 取 ""
        self.assertEqual(details["f3"], "卖出 @ 1 · 0")     # 0 || "rsi" -> 真，?? 取 0

    def test_null_at_ms_sorts_as_zero(self):
        """F12：audit.js:166 `(b.atMs ?? 0) - (a.atMs ?? 0)`——null 当 0，排在负数之后."""
        chain = ac.build_audit_chain(**_audits()["negative_epoch_and_null_at"])
        self.assertEqual([entry["id"] for entry in chain["entries"]], ["null_at", "fill-0-1960-01-01"])
        self.assertEqual(chain["entries"][0]["atMs"], None)
        self.assertEqual(chain["entries"][1]["atMs"], -315619200000)

    def test_signal_index_counts_filtered_previews(self):
        """有意差异 8：audit.js:68-70 先 filter 再 map，所以兜底 id 的下标是过滤后的位置."""
        chain = ac.build_audit_chain(**_audits()["signal_index_after_filter"])
        self.assertEqual([entry["id"] for entry in chain["entries"] if entry["kind"] == "signal"],
                         ["signal-0"])

    def test_missing_activity_id_omits_the_key(self):
        """audit.js:114 `id: a.id` 缺失 -> undefined -> JSON.stringify 丢掉整个键（F11 同族）."""
        chain = ac.build_audit_chain(**_audits()["missing_activity_id"])
        self.assertEqual(len(chain["entries"]), 1)
        self.assertNotIn("id", chain["entries"][0])

    def test_extract_broker_fields_uses_object_entries_order(self):
        """F13：audit.js:45 Object.entries 把整数样式键提前，决定谁先提供 symbol/id."""
        found = ac.extract_broker_fields(_field_cases()["numeric_string_keys"])
        self.assertEqual(found["ticker"], "BBB")   # 键 "2" 先于 "10"
        mixed = ac.extract_broker_fields(_field_cases()["mixed_key_order"])
        self.assertEqual(mixed["ticker"], "SECOND")   # "2" -> "10" -> 然后才是 "b"/"01"

    def test_extract_broker_fields_returns_fixed_keys(self):
        found = ac.extract_broker_fields({"data": {"code": "HK.00700", "order_id": 42,
                                                   "status": "FILLED"}})
        self.assertEqual(found, {"ticker": "00700", "status": "FILLED", "orderId": "42"})
        self.assertEqual(ac.extract_broker_fields({"symbol": "600519.SH"})["ticker"], "600519")
        self.assertIsNone(ac.extract_broker_fields("not an object")["ticker"])
        # 字段别名与大小写：CODE / ORDER_ID 都能命中（audit.js:46 `key.toLowerCase()`）
        aliases = ac.extract_broker_fields({"CODE": "SZ.000001", "ORDER_ID": 9, "State": "NEW"})
        self.assertEqual(aliases, {"ticker": "000001", "status": "NEW", "orderId": "9"})

    def test_time_parsing_is_iso_only(self):
        """有意差异 2：只认 ISO-8601（Node Date.parse 还认 RFC 2822 等）。

        返回值是 int（Date.parse 的毫秒是整数，JSON.stringify 不带小数点，补遗 B F7）。
        """
        self.assertEqual(ac._to_ms("2026-09-10"), 1788998400000)
        self.assertEqual(ac._to_ms("2026-09-10T09:35:00Z"), 1789032900000)
        self.assertEqual(ac._to_ms("2026-09-10T09:35:00+08:00"), 1789004100000)
        for stamp in ("2026-09-10", "2026-09-10T09:35:00Z"):
            self.assertIsInstance(ac._to_ms(stamp), int)
        # 亚毫秒与 1970 年前的向下取整（Node 实测：.0005 -> 整毫秒、.123456 -> 123）
        self.assertEqual(ac._to_ms("2026-09-10T09:35:00.123456Z"), 1789032900123)
        self.assertEqual(ac._to_ms("2026-09-10T09:35:00.1234567Z"), 1789032900123)
        self.assertEqual(ac._to_ms("1960-01-01T00:00:00.0005Z"), -315619200000)
        for hostile in (None, "", 0, "not-a-date", "2026/09/10", "10 Sep 2026 09:35:00 GMT"):
            with self.subTest(value=hostile):
                self.assertIsNone(ac._to_ms(hostile))


class JsHelperTest(unittest.TestCase):
    """platform/server/_js.py：三个模块共用的 JS 语义助手。

    期望值全部来自实际运行 Node 的结果（探针见文件头说明），不是对 JS 规范的转述：
    `String(1e-7)`、`(0.125).toFixed(2)`、`Number("0b101")`、`Object.keys` 的枚举序。
    """

    def test_js_number_str_matches_js_string(self):
        cases = {
            1e-7: "1e-7", 1e-6: "0.000001", 1e-5: "0.00001", 1e-4: "0.0001",
            0.1 + 0.2: "0.30000000000000004", 100.0: "100", -0.0: "0", 2.5: "2.5",
            -2.5: "-2.5", 1e20: "100000000000000000000", 1e21: "1e+21",
            5e-324: "5e-324", 1.7976931348623157e308: "1.7976931348623157e+308",
            123456789012345680000: "123456789012345680000", 1 / 3: "0.3333333333333333",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(_js.js_number_str(value), expected)
        self.assertEqual(_js.js_number_str(100), "100")          # int 输入不走浮点分支
        self.assertEqual(_js.js_number_str(float("nan")), "NaN")
        self.assertEqual(_js.js_number_str(float("inf")), "Infinity")
        # Python 的 repr() 会写出 "1e-07"（前导零的指数），JS 从不这样写
        self.assertNotIn("e-0", _js.js_number_str(1e-7))

    def test_fixed_matches_to_fixed(self):
        cases = [(0.125, "0.13"), (-0.125, "-0.13"), (0.135, "0.14"), (1.005, "1.00"),
                 (0.00125, "0.00"), (-0.00125, "-0.00"), (-0.001, "-0.00"), (0.0, "0.00"),
                 (100, "100.00"), (2.5, "2.50"), (1e21, "1e+21"), (1e22, "1e+22"),
                 (float("nan"), "NaN"), (float("-inf"), "-Infinity")]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_js.fixed(value), expected)
        # 平局（双精度里恰好是 .5）时 Python 的 f-string 是银行家舍入，必须分叉：
        # JS "0.13" / f"{0.125:.2f}" 是 "0.12"。0.135 不是平局（实际是 0.13500...0888），两边都是 0.14。
        self.assertEqual(_js.fixed(0.125), "0.13")
        self.assertEqual(f"{0.125:.2f}", "0.12")

    def test_numeric_matches_js_number(self):
        cases = {"0b101": 5, "0B101": 5, "0o17": 15, "0O17": 15, "0x1f": 31, "0X1F": 31,
                 "1_000": None, "0x1_0": None, "0b_101": None, "0o_17": None,
                 "-0x10": None, "+0x10": None, "0b2": None, "0o8": None,
                 " 12 ": 12, "1e2": 100, "0.5e1": 5, "": 0, "  ": 0, ".5": 0.5, "1.": 1,
                 "+1": 1, "-1.5": -1.5, "inf": None, "Infinity": None, "+Infinity": None,
                 "-Infinity": None, "nan": None, "\ufeff12": 12, "\u001c12": None,
                 "1,000": None, "1e": None, "0x": None, "0b": None, "0o": None, "01": 1,
                 "１２": None, "١٢": None, "1e1000": None}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(_js.numeric(text), expected)
        self.assertEqual(_js.numeric(None), 0)         # Number(null) === 0
        self.assertEqual(_js.numeric(True), 1)         # Number(true) === 1
        self.assertIsInstance(_js.numeric("200"), int)  # F7：整数值是 int
        self.assertIsInstance(_js.numeric(1.5), float)

    def test_template_and_stringify(self):
        self.assertEqual(_js.template(None), "null")
        self.assertEqual(_js.stringify(None), "")
        self.assertEqual(_js.template(_js.UNDEFINED), "undefined")
        self.assertEqual(_js.stringify(_js.UNDEFINED), "")
        self.assertEqual(_js.template([]), "")                       # String([]) === ""
        self.assertEqual(_js.template([1, 2]), "1,2")                # String([1,2]) === "1,2"
        self.assertEqual(_js.template([None]), "")                   # join 把 null 当空串
        self.assertEqual(_js.template([{}, [1, [2]]]), "[object Object],1,2")
        self.assertEqual(_js.template({}), "[object Object]")
        self.assertEqual(_js.template(True), "true")
        self.assertEqual(_js.template(1e-7), "1e-7")
        self.assertEqual(_js.js_nullish(0, "x"), 0)                  # ?? 不回退假值
        self.assertEqual(_js.js_nullish([], "x"), [])
        self.assertIsNone(_js.js_nullish(None, _js.UNDEFINED))

    def test_truthy_follows_js_not_python(self):
        """空数组/空对象在 JS 里为真——summary 与 audit_chain 曾各写了一份相反的实现。"""
        for value in ([], {}, [0], {"a": None}, "x", 1, -1):
            with self.subTest(value=value):
                self.assertTrue(_js.truthy(value))
        for value in (None, False, 0, 0.0, "", float("nan"), _js.UNDEFINED):
            with self.subTest(value=value):
                self.assertFalse(_js.truthy(value))

    def test_object_entry_order(self):
        self.assertEqual(_js.object_entry_order({"10": 1, "2": 2, "abc": 3, "01": 4,
                                                 "4294967294": 5, "4294967295": 6, "0": 7}),
                         ["0", "2", "10", "4294967294", "abc", "01", "4294967295"])
        # 非字符串键按 ToPropertyKey 强转后判定（Python dict 允许 int/bool 键）
        self.assertEqual(_js.object_entry_order({10: 1, 2: 2, "b": 3}), [2, 10, "b"])

    def test_js_round_is_half_up(self):
        self.assertEqual(_js.js_round(0.5), 1)      # Python round(0.5) === 0
        self.assertEqual(_js.js_round(-0.5), 0)     # JS Math.round(-0.5) 是 -0
        self.assertEqual(_js.js_round(2.5), 3)
        self.assertEqual(_js.js_round(-2.5), -2)
        self.assertEqual(_js.js_round(float("inf")), float("inf"))

    def test_json_number_is_json_stringify_typed(self):
        self.assertEqual(_js.json_number(93040.0), 93040)
        self.assertIsInstance(_js.json_number(93040.0), int)
        self.assertEqual(_js.json_number(0.01), 0.01)
        self.assertEqual(_js.json_number(1e20), 100000000000000000000)
        self.assertEqual(_js.json_number(1e21), 1e21)      # >= 1e21 时 JS 写指数形式
        self.assertIsInstance(_js.json_number(1e21), float)
        self.assertIsNone(_js.json_number(float("inf")))   # JSON.stringify(Infinity) === "null"
        self.assertIsNone(_js.json_number(float("nan")))


class LabelsMirrorTest(unittest.TestCase):
    """labels.py 是 labels.js 的第四份镜像：逐表比对键值，防止平台侧漂移。"""

    TABLES = ["SIGNAL", "ACTION", "SIDE", "TRADE_TYPE", "TRADE_STATUS", "RATING",
              "SOURCE_STATUS", "EXECUTION_TIMING", "END_POSITION_POLICY", "EXECUTION_SOURCE",
              "RISK_CONFIG", "METRIC", "GRID_AXIS", "STRATEGY"]

    @staticmethod
    def _parse_js_object(source, name):
        """复刻 tests/test_labels.py:33-56 的解析（键一律按字符串比对）。"""
        import re
        match = re.search(rf"(?:export\s+const\s+{name}\s*=|{name}\s*:)\s*\{{", source)
        assert match is not None, f"labels.js 缺少 {name}"
        start = match.end() - 1
        depth, end = 0, -1
        for index in range(start, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
        body = source[start + 1:end]
        return {key: value for key, value in
                re.findall(r'(\d+|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"([^"]*)"', body)}

    def test_tables_match_labels_js(self):
        source = LABELS_JS.read_text(encoding="utf-8")
        for name in self.TABLES:
            with self.subTest(table=name):
                parsed = self._parse_js_object(source, name)
                mirror = {str(key): str(value)
                          for key, value in getattr(labels_py, name).items()}
                self.assertEqual(parsed, mirror,
                                 f"{name} 在 labels.js 与 platform/server/labels.py 之间不一致")

    def test_zh_lookup_semantics(self):
        self.assertEqual(labels_py.zh("ACTION", "BUY"), "买入")
        self.assertEqual(labels_py.zh("ACTION", "buy"), "买入")     # 大小写不敏感
        self.assertEqual(labels_py.zh("ACTION", "WHAT"), "WHAT")    # 未知值原样返回
        self.assertEqual(labels_py.zh("ACTION", "WHAT", "其他"), "其他")
        self.assertIsNone(labels_py.zh("ACTION", None))
        self.assertEqual(labels_py.zh("ACTION", None, "其他"), "其他")
        self.assertEqual(labels_py.zh("NO_SUCH_TABLE", "X"), "X")
        # 查不到时返回原值本身（JS 里 hasOwnProperty({}, {a:1}) 查的是 "[object Object]"）
        self.assertEqual(labels_py.zh("ACTION", {"a": 1}, "其他"), "其他")
        self.assertEqual(labels_py.zh("ACTION", {"a": 1}), {"a": 1})

    def test_zh_uses_has_own_property_key_coercion(self):
        """labels.js:39 `hasOwnProperty(dict, value)` 会把 value 强制成对象键。

        `zh("SIDE", "1")` 命中 SIDE[1]（JS 键 "1"），而 `zh("SIDE", true)` 查的是键 "true"，
        不命中 -> 返回 true 本身。Python 的 `True in {1: "买入"}` 会因 hash(True)==hash(1)
        错误命中，这一条就是那道闸门（Node 实测见参照物的 labels 分组）。
        """
        self.assertEqual(labels_py.zh("SIDE", "1"), "买入")
        self.assertEqual(labels_py.zh("SIDE", 1), "买入")
        self.assertEqual(labels_py.zh("SIDE", 1.0), "买入")
        self.assertIs(labels_py.zh("SIDE", True), True)       # 不是「买入」
        self.assertIs(labels_py.zh("SIDE", False), False)
        self.assertEqual(labels_py.zh("SIDE", "true"), "true")
        self.assertEqual(labels_py.zh("SIDE", "01"), "01")    # 键不是规范数字串，不回退
        self.assertEqual(labels_py.zh("SIDE", 3), 3)
        self.assertEqual(labels_py.zh("SIDE", 2.5), 2.5)
        self.assertEqual(labels_py.zh("SIDE", [1]), "买入")   # ToPropertyKey([1]) === "1"
        self.assertEqual(labels_py.zh("SIDE", []), [])        # ToPropertyKey([]) === ""
        self.assertEqual(labels_py.zh("SIDE", {"a": 1}), {"a": 1})

    def test_labeled_prefers_explicit_label(self):
        self.assertEqual(labels_py.labeled("BUY", "人工买入", "ACTION"), "人工买入")
        self.assertEqual(labels_py.labeled("BUY", "  ", "ACTION"), "买入")   # 空白不算标签
        self.assertEqual(labels_py.labeled("buy", None, "ACTION"), "买入")
        self.assertEqual(labels_py.labeled("WHAT", None, "ACTION"), "WHAT")


if __name__ == "__main__":
    unittest.main()
