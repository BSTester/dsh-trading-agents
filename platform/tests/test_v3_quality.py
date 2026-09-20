"""``server.v3_quality`` 契约测试：**离线**、注入假 ``v3_run``。

覆盖点（每条都对应一个「如实」的承诺）:
  * 口径：成交率 = 已成交（含部分）/ 总委托；撤单率 = 已撤 / 总委托（用 12 笔混合状态核对）；
  * 滑点：买单成交价高于委托价为正；**卖单**成交价低于委托价为正（方向相反但同为「不利为正」）；
    按名义金额加权，逐日取样进 ``points``；
  * 不跨市场合并：响应只统计 ``groups[].market == market`` 的分组；
  * 无委托 → ``quality/no-orders``；富途不可用 → 保留上游错误码 + 明说**没有开源替代**；
  * 官方状态码表（2/3/4/5/6）之外的状态码进 ``unknownStatus`` 并写入 ``missing``，不猜标签；
  * 派生成交（``derived=true``）与滑点近似口径原样进 ``missing``；
  * ``register`` 只挂 1 条路由，``market`` 白名单外的取值 → ``quality/bad-market``。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_quality -v``
"""
from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from server import v3_quality  # noqa: E402

DAY_MICROS = int(datetime(2026, 9, 18, 2, 0, tzinfo=timezone.utc).timestamp() * 1_000_000)  # SH 10:00
DAY2_MICROS = int(datetime(2026, 9, 17, 2, 0, tzinfo=timezone.utc).timestamp() * 1_000_000)


class FakeApp:
    def __init__(self):
        self.routes = {}
        self.state = types.SimpleNamespace()

    def get(self, path):
        def decorator(func):
            self.routes[path] = func
            return func

        return decorator


class FakeV3Run:
    """记录型 ``v3_run``：按工具名返回预置信封，并把 (name, payload) 记下来。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, dict(payload or {})))
        value = self.values.get(name)
        if value is None:
            return {"ok": False, "error": {"code": "test/unrouted", "message": f"未预置 {name}"}}
        return value


def order(order_id, symbol="601988", side=1, status=4, qty=1000, cum_qty=1000,
          price="10", avg="10", update_time=DAY_MICROS):
    return {
        "order_id": order_id, "symbol": symbol, "side": side, "status": status,
        "qty": str(qty), "cum_qty": str(cum_qty), "price": price, "avg_fill_price": avg,
        "create_time": str(update_time), "update_time": str(update_time),
        "stock_name": "测试标的", "text": "",
    }


def orders_envelope(rows, market="SH", **extra):
    value = {
        "mode": "sim",
        "source": "futu/sim_trade_history_order_list",
        "as_of": "2026-09-20T03:29:18Z",
        "window": {"start": "2026-08-21", "end": "2026-09-20"},
        "groups": [{"acc_id": "3182575", "market": market, "rows": list(rows)}],
        "errors": [],
    }
    value.update(extra)
    return {"ok": True, "value": value}


def deals_envelope(rows, market="SH", derived=True):
    return {"ok": True, "value": {
        "mode": "sim",
        "source": "futu/sim_trade_history_order_list(derived)",
        "as_of": "2026-09-20T03:29:24Z",
        "groups": [{"acc_id": "3182575", "market": market, "rows": list(rows)}],
        "errors": [],
        "derived": derived,
        "note": "由模拟订单派生，非券商成交流水",
    }}


class MetricsTests(unittest.TestCase):
    def _run(self, values):
        return v3_quality.execution_quality(FakeV3Run(values), "SH", "sim")

    def test_counts_and_rates_follow_the_stated_definitions(self):
        rows = (
            [order(f"F{i}", status=4) for i in range(9)]          # 全部成交 9
            + [order("P1", status=3, cum_qty=500)]                # 部分成交 1
            + [order("C1", status=5, cum_qty=0, avg="0"), order("C2", status=5, cum_qty=0, avg="0")]  # 已撤 2
        )
        payload = self._run({"orders_history": orders_envelope(rows), "deals_history": deals_envelope([])})
        self.assertTrue(payload["ok"], payload)
        metrics = payload["metrics"]
        self.assertEqual(metrics["orders"], 12)
        self.assertEqual(metrics["filled"], 9)
        self.assertEqual(metrics["partial"], 1)
        self.assertEqual(metrics["cancelled"], 2)
        self.assertAlmostEqual(metrics["fillRatePct"], (9 + 1) / 12 * 100, places=4)
        self.assertAlmostEqual(metrics["cancelRatePct"], 2 / 12 * 100, places=4)

    def test_slippage_sign_is_adverse_positive_for_both_sides(self):
        rows = [
            order("BUY", side=1, price="10", avg="10.1"),      # 买：成交更高 → +100 bps（不利）
            order("SELL", side=2, price="20", avg="19.9"),     # 卖：成交更低 → +50 bps（不利）
            order("GOOD", side=1, price="10", avg="9.9"),      # 买：成交更低 → -100 bps（有利）
        ]
        payload = self._run({"orders_history": orders_envelope(rows), "deals_history": deals_envelope([])})
        self.assertTrue(payload["ok"], payload)
        expected = (100 * 10100 + 50 * 19900 + (-100) * 9900) / (10100 + 19900 + 9900)
        self.assertAlmostEqual(payload["metrics"]["avgSlippageBps"], round(expected, 4), places=3)
        self.assertAlmostEqual(payload["metrics"]["notional"], 39900.0, places=2)
        self.assertEqual(payload["metrics"]["fillRatePct"], 100.0)
        point = payload["points"][0]
        self.assertEqual(point["t"], "2026-09-18", "更新时刻按市场时区折算成交易日")
        self.assertEqual(point["orders"], 3)
        self.assertAlmostEqual(point["slippageBps"], round(expected, 4), places=3)
        self.assertAlmostEqual(point["notional"], 39900.0, places=2)

    def test_points_are_bucketed_by_day(self):
        rows = [
            order("D1", update_time=DAY2_MICROS, price="10", avg="10.01"),
            order("D2", update_time=DAY_MICROS, price="10", avg="10.02"),
        ]
        payload = self._run({"orders_history": orders_envelope(rows), "deals_history": deals_envelope([])})
        self.assertEqual([point["t"] for point in payload["points"]], ["2026-09-17", "2026-09-18"])

    def test_no_fill_means_null_slippage_not_zero(self):
        rows = [order("C1", status=5, cum_qty=0, avg="0")]
        payload = self._run({"orders_history": orders_envelope(rows), "deals_history": deals_envelope([])})
        self.assertTrue(payload["ok"], payload)
        self.assertIsNone(payload["metrics"]["avgSlippageBps"], "无成交不能把滑点写成 0")
        self.assertEqual(payload["points"], [])
        self.assertTrue(any("无成交" in line for line in payload["missing"]))

    def test_unknown_status_code_is_reported_not_guessed(self):
        rows = [order("X1", status=99, cum_qty=0, avg="0")]
        payload = self._run({"orders_history": orders_envelope(rows), "deals_history": deals_envelope([])})
        self.assertEqual(payload["metrics"]["unknownStatus"], 1)
        self.assertEqual(payload["metrics"]["orders"], 1)
        self.assertTrue(any("不在官方发布表内" in line for line in payload["missing"]))

    def test_contradictory_status_is_kept_and_flagged(self):
        rows = [order("BAD", status=4, qty=1000, cum_qty=300, price="10", avg="10")]
        payload = self._run({"orders_history": orders_envelope(rows), "deals_history": deals_envelope([])})
        self.assertEqual(payload["metrics"]["filled"], 1, "状态码仍是事实，不悄悄改成 partial")
        self.assertTrue(any("累计成交量不足" in line for line in payload["missing"]))

    def test_orders_from_other_markets_are_ignored(self):
        envelope = orders_envelope([order("SH1")])
        envelope["value"]["groups"].append({"acc_id": "9393", "market": "HK",
                                            "rows": [order("HK1", status=5, cum_qty=0, avg="0")]})
        payload = self._run({"orders_history": envelope, "deals_history": deals_envelope([])})
        self.assertEqual(payload["metrics"]["orders"], 1, "不得跨市场合并")
        self.assertEqual(payload["metrics"]["cancelled"], 0)

    def test_derived_deals_and_slippage_note_are_surfaced(self):
        rows = [order("F1", price="10", avg="10.05")]
        payload = self._run({"orders_history": orders_envelope(rows), "deals_history": deals_envelope([])})
        self.assertTrue(any("derived=true" in line for line in payload["missing"]))
        self.assertTrue(any("近似口径" in line for line in payload["missing"]))
        self.assertEqual(payload["sources"]["orders"], "futu/sim_trade_history_order_list")
        self.assertEqual(payload["sources"]["deals"], "futu/sim_trade_history_order_list(derived)")
        self.assertEqual(payload["chain"], [
            {"source": "futu/orders_history", "ok": True, "ms": payload["chain"][0]["ms"]},
            {"source": "futu/deals_history", "ok": True, "ms": payload["chain"][1]["ms"]},
        ])
        self.assertGreaterEqual(payload["chain"][0]["ms"], 0)

    def test_per_account_upstream_errors_are_reported(self):
        envelope = orders_envelope([order("F1")])
        envelope["value"]["errors"] = [{"acc_id": "9393", "reason": "[errcode=-11] rate limit exceeded"}]
        payload = self._run({"orders_history": envelope, "deals_history": deals_envelope([])})
        self.assertTrue(any("上游按账户回报了 1 条失败" in line for line in payload["missing"]))

    def test_mode_and_market_are_passed_through_to_the_tool(self):
        run = FakeV3Run({"orders_history": orders_envelope([order("F1")]),
                         "deals_history": deals_envelope([])})
        v3_quality.execution_quality(run, "hk", "live")
        payloads = [payload for name, payload in run.calls if name == "orders_history"]
        self.assertEqual(payloads[0]["market"], "HK")
        self.assertEqual(payloads[0]["mode"], "live")


class ErrorBranchTests(unittest.TestCase):
    def test_no_orders_returns_quality_no_orders(self):
        payload = v3_quality.execution_quality(
            FakeV3Run({"orders_history": orders_envelope([]), "deals_history": deals_envelope([])}),
            "SH", "sim")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "quality/no-orders")
        self.assertIn("没有委托记录", payload["error"]["message"])

    def test_upstream_failure_keeps_code_and_says_there_is_no_substitute(self):
        payload = v3_quality.execution_quality(
            FakeV3Run({"orders_history": {"ok": False, "error": {
                "code": "trading/openapi-unavailable",
                "message": "mcp 通道下的 live 读取返回 openapi-unavailable"}}}),
            "SH", "live")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "trading/openapi-unavailable")
        self.assertIn("没有开源替代", payload["error"]["message"])
        self.assertEqual(payload["error"]["chain"][0]["source"], "futu/orders_history")

    def test_deals_failure_does_not_break_metrics(self):
        payload = v3_quality.execution_quality(
            FakeV3Run({"orders_history": orders_envelope([order("F1", price="10", avg="10.1")]),
                       "deals_history": {"ok": False, "error": {"code": "trading/futu-error",
                                                                "message": "errcode=-11"}}}),
            "SH", "sim")
        self.assertTrue(payload["ok"], payload)
        self.assertIsNone(payload["sources"]["deals"])
        self.assertTrue(any("成交流水不可用" in line for line in payload["missing"]))
        self.assertAlmostEqual(payload["metrics"]["avgSlippageBps"], 100.0, places=4)

    def test_bad_market(self):
        payload = v3_quality.execution_quality(FakeV3Run(), "MARS", "sim")
        self.assertEqual(payload["error"]["code"], "quality/bad-market")
        self.assertEqual(payload["error"]["supported"], list(v3_quality.MARKET_TRD_CODES))


class RegisterTests(unittest.TestCase):
    def test_register_route_and_envelope(self):
        app = FakeApp()
        v3_quality.register(app, FakeV3Run({"orders_history": orders_envelope([order("F1")]),
                                           "deals_history": deals_envelope([])}), "/tmp")
        self.assertEqual(list(app.routes), ["/api/v3/execution/quality"])
        payload = asyncio.run(app.routes["/api/v3/execution/quality"](market="SH", mode="sim"))
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["market"], "SH")
        self.assertEqual(payload["mode"], "sim")

    def test_route_error_envelope_on_bad_market(self):
        app = FakeApp()
        v3_quality.register(app, FakeV3Run(), "/tmp")
        payload = asyncio.run(app.routes["/api/v3/execution/quality"](market="??", mode="sim"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "quality/bad-market")


if __name__ == "__main__":
    unittest.main()
