"""planner：目标权重 vs 券商实际 → 订单 diff → 冻结（hash）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import planner, store  # noqa: E402


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_freeze_plan_hash_stable_and_diff(self):
        def broker_positions(mode):
            return {"SH.600519": {"qty": 400, "price": 1580.0}}, 1_000_000.0

        plan = planner.build_and_freeze(
            self.conn, mode="SIM", strategy_id="momentum_value_top5",
            target={"SH.600519": 0.5, "SZ.300750": 0.3},
            broker_positions=broker_positions, prices={"SH.600519": 1580.0, "SZ.300750": 201.8},
            as_of="2026-09-13", lot=100)
        self.assertEqual(plan["status"], "frozen")
        self.assertEqual(len(plan["orders"]), 2)
        again = planner.build_and_freeze(
            self.conn, mode="SIM", strategy_id="momentum_value_top5",
            target={"SH.600519": 0.5, "SZ.300750": 0.3},
            broker_positions=broker_positions, prices={"SH.600519": 1580.0, "SZ.300750": 201.8},
            as_of="2026-09-13", lot=100)
        self.assertNotEqual(plan["content_hash"], again["content_hash"])  # 含 plan_id，防重放

    def test_missing_price_is_skipped(self):
        """无价（停牌/无行情）跳过不猜价；订单方向与整手取整口径。"""

        def broker_positions(mode):
            return {}, 1_000_000.0

        plan = planner.build_and_freeze(
            self.conn, mode="SIM", strategy_id="s",
            target={"SH.600519": 0.5, "SZ.300750": 0.3},
            broker_positions=broker_positions, prices={"SH.600519": 1580.0},
            as_of="2026-09-13", lot=100)
        self.assertEqual([o["symbol"] for o in plan["orders"]], ["SH.600519"])
        order = plan["orders"][0]
        self.assertEqual(order["side"], "BUY")
        self.assertEqual(order["qty"], 300)  # 50万/1580=316.5 → 整手 300
        self.assertEqual(order["market"], "SH")


if __name__ == "__main__":
    unittest.main(verbosity=2)
