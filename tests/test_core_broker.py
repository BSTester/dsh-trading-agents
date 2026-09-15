"""券商适配器：参数拼装、信封信任 call_tool 归一化、超时语义。全部假通道。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import broker  # noqa: E402


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
