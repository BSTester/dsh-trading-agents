"""券商适配器：参数拼装、信封信任 call_tool 归一化、传输失败/业务拒绝分类。全部假通道。"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
# 真实传输异常族（WP13 审查 K1）：分类助手要按「请求是否可能已到券商」分流，
# 而真实传输抛的是 TransportError/UnexpectedResponse/FutuUnavailable，不是 TimeoutError。
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
from trading_core import broker  # noqa: E402
from trading_datasource.futu_mcp import FutuUnavailable  # noqa: E402
from trading_datasource.futu_openapi.errors import (  # noqa: E402
    OpenApiError, TransportError, UnexpectedResponse)


class BrokerTest(unittest.TestCase):
    def test_place_assembles_required_args(self):
        seen = {}

        def fake_call(name, args, timeout=30):
            seen.update(name=name, args=args)
            return {"order_id": "7142358"}

        out = broker.place(fake_call, acc_id="A1", market="SH", symbol="600519",
                           side="BUY", qty=100, price=1580.0, order_type=1)
        self.assertEqual(out["broker_order_id"], "7142358")
        self.assertEqual(seen["name"], "sim_trade_input_order")
        self.assertEqual(seen["args"]["symbol"], "600519")
        self.assertEqual(seen["args"]["order_side"], 1)  # 1=Buy 2=Sell（schema 明文）
        self.assertEqual(seen["args"]["qty"], 100)

    def test_timeout_maps_to_unknown(self):
        def fake_call(name, args, timeout=30):
            raise TimeoutError("timed out")

        out = broker.place(fake_call, acc_id="A1", market="SH", symbol="600519",
                           side="BUY", qty=100, price=1580.0, order_type=1)
        self.assertEqual(out["status"], "unknown")   # 绝不重放，交上层查询
        self.assertIsNone(out.get("broker_order_id"))

    # ---- WP13 审查 K1：真实传输异常也必须落 unknown（旧实现只认 TimeoutError，
    # ---- 而真实 REST/MCP 通道从不抛它 → 可能已到券商的单被落成 rejected 终态）。

    def test_transport_error_maps_to_unknown_and_never_retried(self):
        calls = []

        def fake_call(name, args, timeout=30):
            calls.append(name)
            raise TransportError("连接被重置")

        out = broker.place(fake_call, acc_id="A1", market="SH", symbol="600519",
                           side="BUY", qty=100, price=1580.0, order_type=1)
        self.assertEqual(out["status"], "unknown", "传输失败＝可能已到券商，必须 unknown")
        self.assertEqual(len(calls), 1, "铁律：传输失败绝不重放")
        self.assertIn("连接被重置", out["err"])

    def test_unexpected_response_maps_to_unknown(self):
        def fake_call(name, args, timeout=30):
            raise UnexpectedResponse("502 Bad Gateway")

        out = broker.place(fake_call, acc_id="A1", market="SH", symbol="600519",
                           side="BUY", qty=100, price=1580.0, order_type=1)
        self.assertEqual(out["status"], "unknown",
                         "非信封/5xx 不是业务结论（errors.py 明示可能已到达券商）")

    def test_futu_unavailable_maps_to_unknown(self):
        def fake_call(name, args, timeout=30):
            raise FutuUnavailable("MCP 通道不可用")

        out = broker.place(fake_call, acc_id="A1", market="SH", symbol="600519",
                           side="BUY", qty=100, price=1580.0, order_type=1)
        self.assertEqual(out["status"], "unknown")

    def test_business_openapi_error_still_rejected(self):
        """防矫枉过正：业务信封错误（券商明确拒绝）必须保持 rejected 终态。"""
        def fake_call(name, args, timeout=30):
            raise OpenApiError("参数非法", errcode=-3)

        out = broker.place(fake_call, acc_id="A1", market="SH", symbol="600519",
                           side="BUY", qty=100, price=1580.0, order_type=1)
        self.assertEqual(out["status"], "rejected")
        self.assertIn("参数非法", out["err"])

    def test_is_transport_failure_classification(self):
        self.assertTrue(broker.is_transport_failure(TimeoutError("t")))
        self.assertTrue(broker.is_transport_failure(TransportError("net")))
        self.assertTrue(broker.is_transport_failure(UnexpectedResponse("502")))
        self.assertTrue(broker.is_transport_failure(FutuUnavailable("mcp")))
        self.assertFalse(broker.is_transport_failure(OpenApiError("业务拒绝", errcode=-3)))
        self.assertFalse(broker.is_transport_failure(RuntimeError("资金不足")))

    def test_business_error_maps_to_rejected(self):
        def fake_call(name, args, timeout=30):
            raise RuntimeError("资金不足")

        out = broker.place(fake_call, acc_id="A1", market="SH", symbol="600519",
                           side="BUY", qty=100, price=1580.0, order_type=1)
        self.assertEqual(out["status"], "rejected")
        self.assertIn("资金不足", out["err"])

    def test_sell_side_code(self):
        seen = {}

        def fake_call(name, args, timeout=30):
            seen.update(args=args)
            return {"order_id": "1"}

        broker.place(fake_call, acc_id="A1", market="SZ", symbol="300750",
                     side="SELL", qty=100, price=201.8)
        self.assertEqual(seen["args"]["order_side"], 2)

    def test_positions_passthrough(self):
        def fake_call(name, args, timeout=30):
            assert name == "sim_trade_position_list" and args["market"] == 1
            return {"position_list": [{"code": "600519", "qty": 400}]}

        rows = broker.positions(fake_call, acc_id="A1", market_id=1)
        self.assertEqual(rows[0]["code"], "600519")

    def test_cancel_and_accounts_passthrough(self):
        seen = []

        def fake_call(name, args, timeout=30):
            seen.append((name, args))
            return {"s": "ok"}

        broker.cancel(fake_call, acc_id="A1", market="SH", order_id=714)
        broker.accounts(fake_call)
        self.assertEqual(seen[0], ("sim_trade_cancel_order",
                                   {"acc_id": "A1", "order_id": "714", "market": "SH"}))
        self.assertEqual(seen[1][0], "sim_trade_account_list")

    # ---- positions_and_equity：sim 回归 + live（WP9 任务 4 缺口 2）----

    def test_positions_and_equity_sim_unchanged(self):
        def fake_call(name, args, timeout=30):
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3}]}
            if name == "sim_trade_position_list":
                assert args["market"] == 3  # 缺 market 报 ret=-5（TOOL-LIMITS）
                return {"positions": [{"symbol": "600519", "qty": 400}]}
            if name == "sim_trade_cash_info":
                return {"total_asset": 1_000_000.0}
            raise AssertionError(name)

        positions, equity = broker.positions_and_equity(fake_call, mode="sim", market="SH")
        self.assertEqual(positions, {"SH.600519": {"qty": 400}})
        self.assertEqual(equity, 1_000_000.0)

    def _live_call(self, positions=None, equity="25000.00", account_rows=None,
                   missing_equity=False, fail=None):
        """live 假通道：账户/持仓/资金三工具的官方形状（get-accounts / get-funds 文档）。"""

        def fake_call(name, args, timeout=30):
            if fail and name == fail:
                raise RuntimeError(f"{name} 通道断线")
            if name == "account_authorized_trd_accs":
                return {"accounts": account_rows if account_rows is not None else [
                    {"account_id": "LIVE-1", "enable_market": [4], "acc_type": "margin"}]}
            if name == "account_positions":
                assert "acc_id" in args
                return {"positions": positions if positions is not None else [
                    {"code": "SH.600519", "qty": 400},
                    {"code": "US.AAPL", "qty": 10}]}  # 非本市场持仓须被过滤
            if name == "account_funds":
                return {} if missing_equity else {"total_assets": equity, "currency": "CNH"}
            raise AssertionError(name)

        return fake_call

    def test_positions_and_equity_live_filters_market_and_reads_equity(self):
        """live：enable_market 挑账户 → 持仓按市场前缀过滤 → 权益取官方 total_assets。"""
        positions, equity = broker.positions_and_equity(
            self._live_call(), mode="live", market="SH")
        self.assertEqual(positions, {"SH.600519": {"qty": 400}})  # US.AAPL 被过滤
        self.assertEqual(equity, 25000.0)

    def test_positions_and_equity_live_sums_matching_accounts(self):
        """多个 enable_market 命中的账户：持仓并集、权益求和（单账户失败即抛，不掩盖）。"""
        accounts = [{"account_id": "L1", "enable_market": [4]},
                    {"account_id": "L2", "enable_market": [4]},
                    {"account_id": "L3", "enable_market": [1]}]  # 港股账户不参与 SH

        def fake_call(name, args, timeout=30):
            if name == "account_authorized_trd_accs":
                return {"accounts": accounts}
            if name == "account_positions":
                rows = {"L1": [{"code": "SH.600519", "qty": 400}],
                        "L2": [{"code": "SZ.300750", "qty": 500}]}[args["acc_id"]]
                return {"positions": rows}
            if name == "account_funds":
                return {"total_assets": {"L1": "1000.00", "L2": "2000.00"}[args["acc_id"]]}
            raise AssertionError(name)

        positions, equity = broker.positions_and_equity(fake_call, mode="live", market="SH")
        self.assertEqual(positions, {"SH.600519": {"qty": 400}, "SZ.300750": {"qty": 500}})
        self.assertEqual(equity, 3000.0)

    def test_positions_and_equity_live_missing_equity_raises(self):
        """官方权益字段缺失 → EquityUnavailable（调用方跳过当日计划；权益当 0 会凭空清仓）。"""
        with self.assertRaises(broker.EquityUnavailable):
            broker.positions_and_equity(self._live_call(missing_equity=True),
                                        mode="live", market="SH")

    def test_positions_and_equity_live_nonpositive_equity_raises(self):
        with self.assertRaises(broker.EquityUnavailable):
            broker.positions_and_equity(self._live_call(equity="0.00"),
                                        mode="live", market="SH")

    def test_positions_and_equity_live_no_matching_account_raises(self):
        """无 enable_market 命中账户 → 抛错（不退化到别的市场账户，如实指引）。"""
        with self.assertRaises(RuntimeError):
            broker.positions_and_equity(
                self._live_call(account_rows=[{"account_id": "L", "enable_market": [1]}]),
                mode="live", market="SH")

    def test_positions_and_equity_live_channel_failure_raises(self):
        with self.assertRaises(RuntimeError):
            broker.positions_and_equity(self._live_call(fail="account_positions"),
                                        mode="live", market="SH")

    def test_positions_and_equity_unknown_mode_raises(self):
        with self.assertRaises(RuntimeError):
            broker.positions_and_equity(self._live_call(), mode="paper", market="SH")


if __name__ == "__main__":
    unittest.main(verbosity=2)
