"""WP6 补遗 B2 差分测试：交易概要 / 审计链的 Python 移植。

对照基准是 plugins/workbench/src/broker_trades.js 与 plugins/workbench/src/audit.js 的**实际
运行结果**，不是对 JS 源码的二次解读：

  * 每个 fixture 的 Python 字典由本文件顶部的夹具函数构造（与 tests/broker-trades.test.mjs /
    tests/audit.test.mjs 的 entry()/ORDER/envelope 形状逐字对应）；
  * 同一组 fixture 送进 Node 侧真实函数，把返回的完整 JSON 快照压进 _NODE_REFERENCE_B64；
  * test_node_reference_parity 对每个 fixture 做 `self.assertEqual(python_out, node_out)` 递归比较，
    test_node_shape_parity 再递归比对键集（含 null 的位置与数组元素形状）。

Node 参照物是一次性探针（不入库）的产物，可复现方式：

  1. 写一个 .mjs：构造与下面 _activity()/_snapshot() 完全相同的 fixture，调用
     summarizeBrokerActivity / buildAuditChain / extractBrokerFields，`JSON.stringify(out)` 到 stdout；
  2. `node <probe>.mjs > node-out.json`；
  3. `base64.b64encode(zlib.compress(json.dumps(json.load(open('node-out.json')),
     ensure_ascii=False, separators=(',', ':')).encode(), 9))` 得到 _NODE_REFERENCE_B64。

参照物里 auditsMaxEntries 是 audit.js `maxEntries` 参数的等价用例（buildAuditChain({...input, maxEntries: n})）。

有意差异（Python 比 Node 更保守的三处，已单独用注释与断言钉住，不计入等值比较的 fixture）：
  * 环状 value：audit.js 靠 JSON.parse 爆栈 + visited<200 兜底；Python 用幂等已访问集合直接
    有界终止，两者最终都只产出 ticker/status/orderId = null（test_cyclic_value_terminates）。
  * activity 里出现 null 元素：JS `entry.is_error`（broker_trades.js:163）会抛 TypeError，
    Python 侧 _field() 取不到字段、按「无名查询」计数，不抛错（test_null_activity_entry_is_tolerated）。
  * 非数组入参（undefined/null/{}）两边都返回空概要，但 Node 只覆盖 undefined/null；Python 额外
    容忍 dict/字符串/整数（test_non_array_inputs_are_empty）。
"""
import base64
import json
import sys
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
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
    return {"two": {**_audits()["signal_chain"], "maxEntries": 2},
            "zero": {**_audits()["signal_chain"], "maxEntries": 0}}


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
    }


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
        "shared_signal": "audit.js:141-152 取最近的不晚于它的信号",
    }


# ---------------------------------------------------------------- Node 参照物
# 生成方式见文件头。压缩后存入，避免 8 万字符的 JSON 字面量把测试文件淹没。
_NODE_REFERENCE_B64 = (
    "eNrtXVtv3MYVfvevWGzRp2qFGXKvAgKkRVMkQNIWsIq2KQqC2qUkVlxyQ3Jtq0EAx40vaeQ4SJQL7ARpAitVA9RK0CC2FRcB"
    "+lMScSU9+S+UtyWHw8vytkvu7hiGLc0MyZnznducmTnz6oVKVRn2+6zMc0p1rfLqhUqlKsk9TmY22a46LrLLjF//ZPxqFTpN"
    "+Z5eUW02qCZowOqKXafs9jckwagBnU7TLRfZPmeUjt45Pj186/zeDe3e57WL7mN8z6zWbn+g3Tz2FDNdyayjxoWvqLvGrwCM"
    "CzZ5QeB6jK98IPNd48l6s7HqPM1e2mKMB5jg2r40FFW9tEODuucDZu+uH57/7XB0652T4/tuH1VWHSrjXtZXUCLpvWKNt1Up"
    "QDVrANZgcx201hr1tTq1CgB42XnLcNBj1bDmnTUaepsrHCd62nZqkFqHYM3421ltwJbbtsuKXc6gkN56kxUUblzRl3r8Jq9/"
    "tGsPGpgVrxn//tlsVNXZgZdEkwXskleGHMI2xuvHD1uvraqSJFgPGL+/Zj1lNkIekjlloL/XfA+0H7TYKrQGLXC7Nf4qJ8uS"
    "2QQgXxUl1cK4qgM2+ui90RfXzr54/XT/68rzrCxyilI5++c17ehT7dZXp3ff0I72zq/uaw8PtOsPtfdua8f72n/fPT3+z9nV"
    "vdEnB0+f7J08uj366Ei79Uh7/4bFBKO3D8+vvv70yT3tzpdnR9+NPj04O/r85LvrZw8+G73/ld5eu/Whzs8/Xr128ugt7fb7"
    "J4/eHr17X//h7Oih9vGh042TJ3fPjj45efRv7cGe/mbt8bennz04e3Bff7B6wR6OJQwbw90CxfPk8Vfa9YNA8YREPIl4LrN4"
    "mlxrcBuXyIBuFmA7YYhwwnyEUy+lllU4V8KBpsoDdD0XnGG7Cdo4zjrI2q0by4wzXR6cc4HZJ8qjj79cZnzrs8d3xuiefnpA"
    "/Ke8/Kd6qP9Ux/2n+jL7Tz2uNxxwzA7HDRRGYBWVhCHmeJ5DrQHgbVt2OaVC5ZQi8xxETi0cGRO1XUbgxR12i0siqy1It1p0"
    "iWS1GC+pUbCIgnqkiKryMFxCqSgJ9QAf+GXa92XrcZNsJmNWV2IwjrSD9bPHqSxvEt9pw4mqvGs/L9WrQc5dYBepqC7uP55a"
    "F+n4XYQFdZGK30UQ0UVLFU2ni7DqY9D8DUYjS2CsvkSBMR0qQ3dYdkMfG4PqmdhmY+kNxuLOvSYqkoBPx7EY4lAQPDrE00dX"
    "icic+kytUdlguzuc2KtsDBXeZG9TOAM1THcmGobKomEgrmHgwmoYmWN7kijsMjYEDtfhyiX5dILGsbOYwWZWq9wgo8L3GVVm"
    "dVXCds1ndc9Yn8KujNuN3wetAtt4Rr2nyyrbDC9uSlle0mevGEuGjKJLafh7zP/iMiUdypQAZ0qwzPMkg6V2GdVgR1bsMUNx"
    "R5Qui7kwZTMxU0bzATWBn1qF83E2oRrTPh8BaBIBiCUAvDgYqoxFJcdaT2b+bM7AxFkFSD2r4Gdi82EW7oLLGoZySZCZwWDq"
    "mTWVmrX6cefUMHpOHRE8ST+b7hfs68bge2rpZtP22BkHu2kr1nC+jzvL0u5/ffbNQSCL9ZKwGJySS57ICaByVdPU4m5aZHoM"
    "J17iBGnAFWv+O51OahWoENNfqum+asbEGCs+UyRXJYow0RVevMQKfK8yYGW2z6lccHBJnkNugwuswYyQtQlu+flMeSY8ZilT"
    "hK3Kw1aiJDLSxl+4rsoM2F1BYnvlYrAE1lGmiXUsF2OJw75O/i6j8n2ObKPKexsVKivkYAg5GDJBIPu8ovDiFoMIWvYlgFmg"
    "ttTRa4sKjCLJhnj3OKWbRJFSRIUuxWEAYion40zXoA5cfZ2i1iC9RgFyMiC5KqaWWRWPvdmuxMndkGWeUAlVg0T0Dz7xzHL4"
    "igo+TNnExRGEiiKYvDssf28VrShIfapBdvKPE8Hx9BwBhvYCgzbzQoMjQ602QrGhfdjQiY5MwblDpR6EysuTRSZ+dgAYDxYY"
    "LjHQLzIwjcyA+ZOZoPOHqUUGLbf9iOATwzg26IN+dHSj3fCfC18FVAqIPKGpuUGpUQRKYBUmwkn3xfww6YUwxanu+YSpOR8w"
    "wUCYqMWCqbzud3OZ3e9tSVF5gWOMkD/PKTPdVh26c5PKc/8p2f88pXWIHt9VGU7g+pyo5sM49ayMU0+DeZ1gHgtzrj9Qd4uN"
    "bwMC1SSoLpgjslNA7l7cZgeZ00AqumkQtwJ8Kaxi7FHh7e0ZZEDx2G8xokAbyIYZy5PCSz1uFl45dqfwcp+/5Wsw9rmCPhfU"
    "b4/HhT/kDZBij3rDofh7Xe8Lq0F9reqGLjMc6+g+v7817lEWt8s7quRyij3vF9egBgHlbpexCkeG0dH6RNmmY6Yki4T5CfMv"
    "BPOnTGFI+H9++X+FoEpQJagSVAmqxAMp3gPJlASQSAGRgoWQgswp9ogkLJokBJz7wL7pnvLAKiIYwzzp4esgcpzI29xzJNLh"
    "2ZBj66R7C9K9OKd6FjUgkk/qOqKOiToOlFhjawWR1/zkdYqJ4MIoFrb/wAOW/zWTti/M5PFEa+AlhXyKadYWEXLCcFkZLr+0"
    "ZsTHK99kO2MiMTKrIBwXd1aRTwqvhXA7iVXJI08Wke5yzEHyyE5FZpNlkMlsmZ8IhsVjmF+aJYJmOdBMm9uIBGHTBmE9XEzW"
    "hosVgalkE1pQWmXL4UMUBtkqR1Alm4TK6gtlyYpDpGAKzpCnak71mncMcfBE2hM0iZUiiBYmn0hNNjSRF00fSzRCQpAkSBIk"
    "CZLE/5/s/08jLU8pdzwtwvLudJLhkOX0MHrnl4hm8bSHnf6FHfZ4p9O62doSWYHpbrO8u5/NVS3eSIJlPQ1jUgM1Jw+cm2h3"
    "hxedFq5R8maNg0jFS2bStVa7A9r1NjD+jOv07u9wsnk1BQAN2AlYNrOyjlYaAFSerUCq1ViFzcr/HlbOvnl8un9YgR24SjkX"
    "2Bo7hBVr2U5W+MrJ959pdx66Vksayha5BKnLCjXdSm0h1tOmkXMlGFaODRCsg84a3VhDs1cbR7lMy4feriKwW8y2/mWTCPUg"
    "n8X6IusnsMksk+95CexZc41C75BEQKCpThsFwXuLWrvdcskZAx83Exuyc9IA6CfWu4wfLQfkmYu/+8VLL6yvP/fLAEQ2ZEn/"
    "Uk3aUDj5kk5DbwtGYDc4K6GomVDJyrj09Mktb2qmp0/enCWaIBxMxQ+m9eFo0LwdwEBLKTkeqdGlIoD4rwxZUWWwDuKUP795"
    "R9v7ABMpSea3TI1i0CiCt6lg3q55r7NyOfzswec6h1vptQJJ1q4BaJDMvC81iGSNRgtTNiHXIbmUNFvDAEoa/O0uyg0EVifb"
    "jFnYm5PUZWG03GFYTzpOlGON1j5f25BOxLpZb49MU2/RwUDSfc7RSkiiZw+K43yMNoOY1gP9itP78VeGolOEtmHkoWClcH9n"
    "b/SPm6d33/jx6usWW44+/Pb8w2+0jw+1N2+fPNrXfz6/u1/54c37lVZFu/8vy3T6T5PoPVE5OaZZJJLtIYdXGHIwXKAVTilI"
    "UWGGy7z9u9P0Wy5UQOJYLfxFcyrfQVhZe+ATamLsYuBAyJoRkMFmEh08CS17G7+XvxYIIit8EgIRVhl6L3ggRo0ojEC0WDWq"
    "QcayBUCkP2ifvMBFqzEL3Kx+Mpuy1HfoZwypKFytjJvBsHrrcB/IShMajS0Ixxa0p4LtNq+okmwLonnx9qwRLp1zVI90jjxg"
    "Ii6SLbpoiaVwkRLXsQp0oADuQAG/A1WfhgPFXdGZwQh64QcbJgQWlkoWkPnvUsgFCLscMp5cpObwqUwRRMmY7YmEsZdUyS8U"
    "M+s0ETbY7o5OTiVJDJhCYsAOJ/4MtNcQ9okVEg572BMhbteTzoatq9wqUJ8PUwaDjva/Pb33d31uvEo3fuqPDHv4KElE2Pyl"
    "RsWJJGLCOjku3IxwHUsShz8+1h4e2JEHIxYPkCg8QOhuXAA5L2RvRERw8e8miPYk0NXx+fvZCiwiwLPDDdSkNAgYebvTCZXs"
    "MEv0++d/vm4wWhHDtuEHsYbuud3JGnJwhPni86uTg3o/XN03xEm7s6d9cvP0+Psihm8qHViLoWrijj04smNCbFTp4/bsMMmq"
    "N4rwFuiwG0gwbwH3COiiIuCX9R5Jl5kNaSj2WHk3plNgEJ2RLiGEzxb2ba2b+lL/q1sOGKg2m4A2I79wJfIy7kzLldQixhNx"
    "56GVyHloRWGRYsECeoxYdtdgsk8AUrhi7XBymqzPXdE5O3fefzkWrYMvJ8/E93D2q/Ezx01J7sVFwBLly8RYsyvCnm9wm5Kc"
    "0zod6KxTtHF5ezh16HoY09L5MC29sMoa1kRJrbE1Y+9tHGUd0DrSBZ2tji5FZJ6Ot22BDgzXUDGcM3oazpnVW2Nvuj/tYbSD"
    "Nmzmvdsm2ypigvnAAu60CZL0Ychev5qkbnPZEEoVJib7oVJFg6kY0WBn9xOVOhpMTWXxzrOFGlEoiakQc5YbZ6RgGiNVtlnj"
    "hIvtXcVToVKum3FTrt3Mr4MfI1rbTOzh0xH+fYowbTMKFmvWNUcOfpptiYs1xVHoZBTQpzHRFIAUKG+oPlU8EsbcvxvPUsHp"
    "62/0LIv3IuMUB1qwk4oOk2CJ3wKzwVlsgZ+3dDkCax+a9c09n4Kf0XTYxHcKFVWsgXWBXXYw8Z3CRHVq1FHaRFQLS5eXhJwR"
    "Z3eTUzqcoJjEzSe9p8qlaWnn6CmHBqXjK88iU0FchR0oR3jKUxObo/Rn4huFmOcRJ5gInMTYXCeIzR1Dgn3JHaa3HDErAU84"
    "1gXJnZn5aAcRvUJUuofr51T0yq/iCJUXw0HBjjYQHMrgKM6vScf2rOKkds4n+CrGxxTwCp+PUJQTkPJ4AuHjReLjopgv2ckB"
    "wnOE57LzXIoN/uWKiXk5Yc4jYoRmZGpNxulLbuiMspA4XBnGGG8inknC59CkFmAv021/J75aySIKxNAS7iSOzZKtGRCtF0nQ"
    "TM7VsrkeZQtcpt/5TVaclm9dj8TqEsTqSrNDIfX267mZXaXZdk28V+K9krAcGSc6zqXw++yNzps8Jzh9qw5kbpO/whhtB44C"
    "DUybYl/xYpT+6oUXXxxnWLcG/ILZmTqF3Hw+3Ix6Mbq93X2zc7IKeat9fgq/i9v/Tvfh+O/zrdtPGnrDP+pxrjJ36D0m6r1G"
    "zvo4A/eQU2YvM0oXWexNQ8oqRO9+FATmEisMuXwoycoyu5vghdXf/Pa5XwcQ0+2hLaLoBcVpe+i5r+Il9spzngtvqupliVxY"
    "QS6sIBdW5HdhBcn9X/0rJ0tpZl+LSxZdDV947f/8kpt5"
)


def _node_reference():
    return json.loads(zlib.decompress(base64.b64decode(_NODE_REFERENCE_B64)).decode("utf-8"))


def _leaf_kind(value):
    """叶子类型描述（与 Node 探针的 shapeOf 对叶子端的取名一致）。"""
    if value is None:
        return "null"
    if value is True or value is False:
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
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
        """22 组 activity fixture：Python summarize() == Node summarizeBrokerActivity()。"""
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
        return ac.build_audit_chain(inputs["snapshot"], inputs.get("trades"),
                                    inputs.get("maxEntries"))

    def test_node_reference_parity(self):
        """9 组 snapshot/trades：Python build_audit_chain() == Node buildAuditChain()。"""
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
        """audit.js:167 `.slice(0, maxEntries)`：2 条 / 0 条截断与 Node 一致。"""
        reference = self.reference["auditsMaxEntries"]
        for name, inputs in _max_entries_cases().items():
            with self.subTest(case=name):
                self.assertEqual(self._run(name, inputs), reference[name])

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
        Python 侧 _field() 取不到字段 -> 按无名查询计数，服务进程不该因一条脏记录 500。"""
        result = summary.summarize([None])
        self.assertEqual(result["queries"], {"count": 1, "tools": [{"tool": "unknown", "count": 1}]})
        self.assertEqual(result["counts"], {"responses": 1, "order_responses": 0, "orders": 0,
                                            "actions": 0, "errors": 0})

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
        """有意差异 2：只认 ISO-8601（Node Date.parse 还认 RFC 2822 等）。"""
        self.assertEqual(ac._to_ms("2026-09-10"), 1788998400000.0)
        self.assertEqual(ac._to_ms("2026-09-10T09:35:00Z"), 1789032900000.0)
        self.assertEqual(ac._to_ms("2026-09-10T09:35:00+08:00"), 1789004100000.0)
        for hostile in (None, "", 0, "not-a-date", "2026/09/10", "10 Sep 2026 09:35:00 GMT"):
            with self.subTest(value=hostile):
                self.assertIsNone(ac._to_ms(hostile))


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
        # 有意差异 1：不可哈希的值不抛 TypeError，按 fallback 处理
        self.assertEqual(labels_py.zh("ACTION", {"a": 1}, "其他"), "其他")

    def test_labeled_prefers_explicit_label(self):
        self.assertEqual(labels_py.labeled("BUY", "人工买入", "ACTION"), "人工买入")
        self.assertEqual(labels_py.labeled("BUY", "  ", "ACTION"), "买入")   # 空白不算标签
        self.assertEqual(labels_py.labeled("buy", None, "ACTION"), "买入")
        self.assertEqual(labels_py.labeled("WHAT", None, "ACTION"), "WHAT")


if __name__ == "__main__":
    unittest.main()
