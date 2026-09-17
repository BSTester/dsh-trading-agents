"""E2E 取证发现的缺陷修复（2026-09-17，后端批次）。

逐条对应 E2E 的原始证据：
  * **缺陷 2**：券商/上游业务错误丢失错误码 → OMS `err` 只剩「backend business error」，
    无法判断是价格越界、权限还是限频；
  * **缺陷 3**：被券商拒的单仍返回 `ok:true`（调用方/页面据此认为请求成功）；
  * **S2**：`trade_cancel` 成功不回写 OMS → 幽灵在途单永久阻塞同标的同方向新单；
  * **缺陷 4**：`correlation`/`factors`/`ic` 的**载荷错误**被归到 `analytics-unavailable`
    （引擎不可用），把调用方错误说成服务故障；
  * **缺陷 5**：只读设置类端点用 POST 无 body（无 Content-Type）→ 415，读都读不到。
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _dir in (ROOT / "plugins" / "datasource" / "python",
             ROOT / "plugins" / "core" / "python"):
    sys.path.insert(0, str(_dir))
sys.path.insert(0, str(ROOT / "platform"))

from trading_datasource.futu_openapi import OpenApiError  # noqa: E402

from server import app as app_module  # noqa: E402
from server import compute, trading  # noqa: E402

from trading_core import oms as core_oms  # noqa: E402
from trading_core import store as core_store  # noqa: E402

ORDER = {"symbol": "HK.00700", "side": "BUY", "qty": 100, "price": 123.5}
RISK_CONFIG = {"risk_per_trade": 0.01, "stop_atr_mult": 2.0, "max_positions": 5,
               "daily_loss_limit_pct": 0.03, "max_position_pct": 0.25}


def fixed_ctx(**over):
    def builder(conn, mode, order, operation, home, today):
        ctx = {"mode": mode, "kill_path": str(Path(home) / "trading-kill"),
               "equity": 10_000_000.0, "positions_value": {}, "positions_count": 0,
               "day_pnl_pct": 0.0, "is_trading_day": True, "config": dict(RISK_CONFIG),
               "plan_hash": None, "plan_status": "approved"}
        ctx.update(over)
        return ctx
    return builder


class _Confirm:
    def request(self, *args, **kwargs):
        return {"decision": "approved", "id": "C1"}


class _Broker:
    """闸门替身：按脚本返回 place/modify/cancel 结论，并记录调用。"""

    supports_live_write = True

    def __init__(self):
        self.calls = []
        self.place_result = {"status": "submitted", "broker_order_id": "B-1"}
        self.cancel_result = {"status": "cancelled", "order_id": "B-1"}
        self.modify_result = {"status": "submitted", "broker_order_id": "B-2"}

    def place(self, order, mode):
        self.calls.append(("place", dict(order)))
        return dict(self.place_result)

    def modify(self, order, mode):
        self.calls.append(("modify", dict(order)))
        return dict(self.modify_result)

    def cancel(self, order, mode):
        self.calls.append(("cancel", dict(order)))
        return dict(self.cancel_result)


class _GateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "trading-account-mode").write_text("sim\n")
        self.broker = _Broker()

    def gate(self, **kw):
        kw.setdefault("broker", self.broker)
        kw.setdefault("confirm", _Confirm())
        kw.setdefault("ctx_builder", fixed_ctx())
        return trading.TradeGate(str(self.home), **kw)

    def rows(self):
        conn = core_store.connect(core_store.db_path(str(self.home)))
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM orders ORDER BY rowid")]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 缺陷 2：错误码必须跟着错误走
# ---------------------------------------------------------------------------
class ErrorCodeFidelityTests(unittest.TestCase):
    def test_str_carries_upstream_error_code(self):
        error = OpenApiError("backend business error", errcode=-3)
        text = str(error)
        self.assertIn("-3", text)
        self.assertIn("backend business error", text)

    def test_str_without_code_is_unchanged(self):
        self.assertEqual(str(OpenApiError("无码错误")), "无码错误")

    def test_transport_and_business_codes_both_visible(self):
        self.assertIn("503", str(OpenApiError("非预期响应", errcode=503)))


class OrderErrCarriesCodeTests(unittest.TestCase):
    """core_broker.place 写进 OMS 的 err 必须可诊断（缺陷 2 的下游落点）。"""

    def test_core_broker_place_err_keeps_code(self):
        from trading_core import broker as core_broker

        def call(tool, args, timeout=None):
            raise OpenApiError("backend business error", errcode=-3)

        out = core_broker.place(call, acc_id="A1", market="HK", symbol="00700",
                                side="BUY", qty=100, price=1.0)
        self.assertEqual(out["status"], "rejected")
        self.assertIn("-3", out["err"])


# ---------------------------------------------------------------------------
# 缺陷 3：拒单不是「请求成功」
# ---------------------------------------------------------------------------
class RejectedOrderEnvelopeTests(_GateCase):
    def test_rejected_place_returns_ok_false_and_keeps_oms_row(self):
        self.broker.place_result = {"status": "rejected", "broker_order_id": None,
                                    "err": "[errcode=-3] backend business error"}
        out = self.gate().place(dict(ORDER, client_order_id="CID-REJ"))
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["error"]["code"], "trading/order-rejected")
        self.assertIn("-3", out["error"]["message"])
        rows = self.rows()
        self.assertEqual(len(rows), 1, "OMS 行必须保留作审计事实")
        self.assertEqual(rows[0]["status"], "rejected")
        self.assertIn("-3", rows[0]["err"] or "")

    def test_submitted_place_still_ok_true(self):
        out = self.gate().place(dict(ORDER, client_order_id="CID-OK"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"]["status"], "submitted")
        self.assertEqual(self.rows()[0]["status"], "submitted")

    def test_unknown_semantics_unchanged(self):
        """unknown 仍是「可能已到券商」——不得被本次修复改成失败。"""
        self.broker.place_result = {"status": "unknown", "broker_order_id": None,
                                    "err": "传输超时（先查询，不重放）"}
        out = self.gate().place(dict(ORDER, client_order_id="CID-UNK"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"]["status"], "unknown")
        self.assertEqual(self.rows()[0]["status"], "unknown")


# ---------------------------------------------------------------------------
# S2：撤单/改单必须回写 OMS（否则幽灵在途单阻塞后续下单）
# ---------------------------------------------------------------------------
class CancelOmsWriteBackTests(_GateCase):
    def test_cancel_marks_oms_row_cancelled(self):
        gate = self.gate()
        gate.place(dict(ORDER, client_order_id="CID-P1"))
        out = gate.cancel({"symbol": "HK.00700", "order_id": "B-1",
                           "client_order_id": "CID-C1"})
        self.assertTrue(out["ok"], out)
        row = self.rows()[0]
        self.assertEqual(row["status"], "cancelled",
                         "撤单成功必须回写 OMS（E2E S2：此前永久停在 submitted）")

    def test_same_symbol_side_can_be_placed_again_after_cancel(self):
        gate = self.gate()
        gate.place(dict(ORDER, client_order_id="CID-P1"))
        gate.cancel({"symbol": "HK.00700", "order_id": "B-1",
                     "client_order_id": "CID-C1"})
        self.broker.place_result = {"status": "submitted", "broker_order_id": "B-9"}
        again = gate.place(dict(ORDER, client_order_id="CID-P2"))
        self.assertTrue(again["ok"], again)
        self.assertEqual([r["status"] for r in self.rows()], ["cancelled", "submitted"])

    def test_different_inflight_order_still_blocks(self):
        """反证：另一个真正在途的单仍必须阻塞（修复不得削弱在途去重）。"""
        gate = self.gate()
        gate.place(dict(ORDER, client_order_id="CID-P1"))
        self.broker.place_result = {"status": "submitted", "broker_order_id": "B-2"}
        out = gate.place(dict(ORDER, client_order_id="CID-P2"))
        self.assertFalse(out["ok"], out)
        self.assertIn("在途", out["error"]["message"])

    def test_cancel_rejected_keeps_oms_status(self):
        gate = self.gate()
        gate.place(dict(ORDER, client_order_id="CID-P1"))
        self.broker.cancel_result = {"status": "rejected", "order_id": "B-1",
                                     "err": "撤单被拒"}
        out = gate.cancel({"symbol": "HK.00700", "order_id": "B-1",
                           "client_order_id": "CID-C1"})
        self.assertFalse(out["ok"], out)
        self.assertEqual(self.rows()[0]["status"], "submitted", "不得伪造成 cancelled")

    def test_cancel_success_without_oms_row_is_alerted_not_silent(self):
        alerts = []
        gate = self.gate()
        gate.place(dict(ORDER, client_order_id="CID-P1"))
        # 把 broker_order_id 改成一个 OMS 里不存在的值 → 券商成功但本地无对应行
        self.broker.cancel_result = {"status": "cancelled", "order_id": "GHOST-9"}
        out = gate.cancel({"symbol": "HK.00700", "order_id": "GHOST-9",
                           "client_order_id": "CID-C1"})
        self.assertTrue(out["ok"], out)
        conn = core_store.connect(core_store.db_path(str(self.home)))
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT title, level FROM alerts WHERE title LIKE '%撤单%'")]
        finally:
            conn.close()
        self.assertTrue(rows, "券商成功但无 OMS 行必须告警，不得静默")
        self.assertIn(rows[0]["level"], ("info", "warn"))


class ModifyOldOrderWriteBackTests(_GateCase):
    def test_modify_is_not_blocked_by_the_order_it_replaces(self):
        """E2E S2 同类缺口：在途去重把「被替换的那笔」也当阻塞者 → 改单永远无法发起。"""
        gate = self.gate()
        gate.place(dict(ORDER, client_order_id="CID-P1"))
        out = gate.modify({"symbol": "HK.00700", "side": "BUY", "qty": 200,
                           "price": 124.0, "order_id": "B-1",
                           "client_order_id": "CID-M0"})
        self.assertTrue(out["ok"], out)

    def test_modify_cancels_the_replaced_order_row(self):
        """改单 = 撤旧重下：旧单的 OMS 行必须落 cancelled（否则同 S2 的幽灵单）。"""
        gate = self.gate()
        gate.place(dict(ORDER, client_order_id="CID-P1"))
        self.broker.modify_result = {"status": "submitted", "broker_order_id": "B-2"}
        out = gate.modify({"symbol": "HK.00700", "side": "BUY", "qty": 200,
                           "price": 124.0, "order_id": "B-1",
                           "client_order_id": "CID-M1"})
        self.assertTrue(out["ok"], out)
        by_broker = {r["broker_order_id"]: r["status"] for r in self.rows()}
        self.assertEqual(by_broker.get("B-1"), "cancelled", by_broker)
        self.assertEqual(by_broker.get("B-2"), "submitted", by_broker)


# ---------------------------------------------------------------------------
# 缺陷 4：载荷错误 ≠ 引擎不可用
# ---------------------------------------------------------------------------
class AnalyticsErrorClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = str(self.tmp.name)

    def test_payload_error_maps_to_invalid_operation(self):
        def provider(payload, force=False):
            raise compute.ComputeError("tickers must list 2..8 symbols")

        handle = app_module.create_handler(self.home, analytics={"correlation": provider})
        envelope = handle("correlation", {"tickers": []})
        self.assertFalse(envelope["ok"], envelope)
        self.assertEqual(envelope["error"]["code"], "trading/invalid-operation",
                         "载荷错误不得报成 analytics-unavailable（缺陷 4）")

    def test_engine_failure_still_maps_to_analytics_unavailable(self):
        def provider(payload, force=False):
            raise RuntimeError("子进程失败：python 退出码 1")

        handle = app_module.create_handler(self.home, analytics={"correlation": provider})
        envelope = handle("correlation", {"tickers": ["SH.600519", "HK.00700"]})
        self.assertFalse(envelope["ok"], envelope)
        self.assertEqual(envelope["error"]["code"], "trading/analytics-unavailable")

    def test_real_compute_validation_is_invalid_operation(self):
        """真实 provider 的校验路径（走 compute 的 tickers 校验）。"""
        handle = app_module.create_handler(self.home)
        envelope = handle("correlation", {"tickers": ["SH.600519"]})
        self.assertFalse(envelope["ok"], envelope)
        self.assertEqual(envelope["error"]["code"], "trading/invalid-operation", envelope)


# ---------------------------------------------------------------------------
# 缺陷 5：只读端点的空 body POST 不应 415
# ---------------------------------------------------------------------------
class EmptyBodyReadEndpointTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "trading-account-mode").write_text("sim\n")
        self.client = TestClient(app_module.create_app(home=str(self.home)))

    def test_post_readonly_endpoint_without_content_type_is_not_415(self):
        resp = self.client.post("/api/wb/openapi_config")
        self.assertNotEqual(resp.status_code, 415, resp.text)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("ok", resp.json())

    def test_get_still_works(self):
        resp = self.client.get("/api/wb/openapi_config")
        self.assertEqual(resp.status_code, 200)

    def test_wrong_content_type_with_body_is_still_415(self):
        """反证：带了 body 却声明成非 JSON 仍是媒体类型错误。"""
        resp = self.client.post("/api/wb/openapi_config", content=b'{"a":1}',
                                headers={"content-type": "text/plain"})
        self.assertEqual(resp.status_code, 415, resp.text)


# ---------------------------------------------------------------------------
# 追加 I4：option_screen 的最小可用载荷必须可发现（真机验证过）
# ---------------------------------------------------------------------------
class OptionScreenDiscoverabilityTests(unittest.TestCase):
    """`option_screen` 是 WP8 行情类端点（走 OpenApiMarket，不在数据面组里），
    因此替身注入在 `market=` 这一层（与 tests/test_wp8_market.py 的 RecordingMarket 同口径）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = str(self.tmp.name)

    def _data(self):
        from server import futu_data

        class _Market:
            def __init__(self):
                self.calls = []

            def __getattr__(self, name):
                def method(*args, **kwargs):
                    self.calls.append((name, kwargs))
                    return {"option_list": []}
                return method

        market = _Market()
        data = futu_data.FutuData(call=None, home=self.home,
                                  channel=futu_data.CHANNEL_OPENAPI, market=market)
        return data, market.calls

    def test_documented_minimal_example_is_accepted(self):
        data, calls = self._data()
        example = {
            "filter": {
                "strategy": {"market_category_list": [0],
                             "filter_group_list": [{"option_list": [
                                 {"indicator_type": 1003,
                                  "indicator_value": {"value_list": [1]}}]}]},
                "field_filter": {"option_type": 1, "volume": 1, "implied_volatility": 1},
                "limit": 3,
            }
        }
        envelope = data._envelope("option_screen", example)
        self.assertTrue(envelope["ok"], envelope)
        name, kwargs = calls[-1]
        self.assertEqual(name, "option_screen")
        self.assertEqual(kwargs["field_filter"],
                         {"option_type": 1, "volume": 1, "implied_volatility": 1})
        self.assertEqual(kwargs["strategy"]["market_category_list"], [0])
        self.assertEqual(kwargs["limit"], 3)

    def test_empty_list_field_filter_is_rejected_locally(self):
        """E2E I4 原始证据：field_filter={"option_type": []} 被原样透传 → 上游 -3。"""
        data, calls = self._data()
        envelope = data._envelope("option_screen", {"filter": {
            "strategy": {"market_category_list": [0]},
            "field_filter": {"option_type": []}}})
        self.assertFalse(envelope["ok"], envelope)
        self.assertEqual(envelope["error"]["code"], "trading/invalid-operation")
        self.assertEqual(calls, [], "坏载荷必须零通道调用")
        self.assertIn("option_type", envelope["error"]["message"])

    def test_zero_and_empty_object_placeholders_are_rejected(self):
        for bad in (0, {}, None, "", []):
            with self.subTest(value=bad):
                data, calls = self._data()
                envelope = data._envelope("option_screen", {"filter": {
                    "strategy": {"market_category_list": [0]},
                    "field_filter": {"volume": bad}}})
                self.assertFalse(envelope["ok"], (bad, envelope))
                self.assertEqual(calls, [])

    def test_nested_placeholder_is_accepted(self):
        """官方口径：嵌套字段按 proto 字段名声明（非空对象）。"""
        data, calls = self._data()
        envelope = data._envelope("option_screen", {"filter": {
            "strategy": {"market_category_list": [1]},
            "field_filter": {"underlying_info": {"iv": 1, "hv": 1}}}})
        self.assertTrue(envelope["ok"], envelope)
        self.assertEqual(calls[-1][1]["field_filter"],
                         {"underlying_info": {"iv": 1, "hv": 1}})

    def test_error_message_carries_a_working_example(self):
        data, _ = self._data()
        envelope = data._envelope("option_screen", {"filter": {
            "strategy": {"market_category_list": [0]},
            "field_filter": {"option_type": 0}}})
        self.assertFalse(envelope["ok"])
        self.assertIn("filter_group_list", envelope["error"]["message"])

    def test_tool_description_contains_the_example(self):
        from server import mcp_tools
        description = next(t.description for t in mcp_tools.TOOLS
                           if t.name == "option_screen")
        self.assertIn("market_category_list", description)
        self.assertIn("indicator_type", description)

if __name__ == "__main__":
    unittest.main()
