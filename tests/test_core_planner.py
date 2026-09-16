"""planner：目标权重 vs 券商实际 → 订单 diff → 冻结（hash）+ 订单登记进 OMS。

定量自洽（规格 §4.2 第 5 点）：计划按风险预算定量，因此**加仓标的必须有可算的 ATR**
（``indicators.stop_distance``）。本文件的用例验证 hash/diff/登记链路，统一用
``seed_bars`` 铺 20 根确定性 bar → ATR=1.0 → 止损距离=2.0 → 风险预算 5000 股，
使**权重臂**成为约束臂（期望数量与修复前一致）。
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import execute, planner, store  # noqa: E402

#: execute.run 的风控 ctx（与 test_core_execute 同口径的最小通过集）
RISK_CTX = {"mode": "sim", "kill_path": "/nonexistent", "equity": 1_000_000.0,
            "positions_value": {}, "positions_count": 0, "day_pnl_pct": 0.0,
            "is_trading_day": True,
            "config": {"risk_per_trade": 0.01, "max_positions": 5,
                       "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03}}


def seed_bars(conn, symbol, close, count=20, last="2026-08-31"):
    """铺确定性日线：h=c+0.5 / l=c−0.5 / o=c → 每根 TR=1.0 → ATR(14)=1.0。

    最后一天固定在 2026-08-31（早于本文件所有 as_of），使 PIT 过滤后仍留足 15 根。
    """
    end = date.fromisoformat(last)
    days = [(end - timedelta(days=count - 1 - i)).isoformat() for i in range(count)]
    store.upsert_bars(conn, symbol, "1d", [
        {"t": d, "o": close, "h": close + 0.5, "l": close - 0.5, "c": close, "v": 1000.0}
        for d in days], source="test")


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_freeze_plan_hash_stable_and_diff(self):
        seed_bars(self.conn, "SH.600519", 1580.0)
        seed_bars(self.conn, "SZ.300750", 201.8)

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
        seed_bars(self.conn, "SH.600519", 1580.0)

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

    # ---- 订单登记进 OMS（冻结即登记，execute.run 的唯一取单来源）----

    def test_freeze_registers_orders_in_oms(self):
        """冻结后 orders 表行与返回 orders 逐条一致（symbol/market/side/qty/price/mode/status）。"""
        seed_bars(self.conn, "SH.600519", 100.0)
        seed_bars(self.conn, "SZ.300750", 200.0)

        def broker_positions(mode):
            return {}, 1_000_000.0

        plan = planner.build_and_freeze(
            self.conn, mode="sim", strategy_id="s",
            target={"SH.600519": 0.5, "SZ.300750": 0.3},
            broker_positions=broker_positions,
            prices={"SH.600519": 100.0, "SZ.300750": 200.0}, as_of="2026-09-13")

        rows = store.get_orders_by_plan(self.conn, plan["plan_id"])
        self.assertEqual(len(rows), len(plan["orders"]))
        self.assertEqual(len(rows), 2)
        for row, order in zip(rows, plan["orders"]):
            self.assertEqual(row["symbol"], order["symbol"])
            self.assertEqual(row["market"], order["market"])
            self.assertEqual(row["side"], order["side"])
            self.assertEqual(row["qty"], order["qty"])
            self.assertEqual(row["price"], order["price"])
            self.assertEqual(row["mode"], "sim")
            self.assertEqual(row["status"], "draft")
            self.assertTrue(row["client_order_id"])

    def test_frozen_plan_executes_through_oms(self):
        """冻结即登记的链路打通：execute.run 能取到单并提交（人工执行与 WP9 自动执行同路）。"""
        seed_bars(self.conn, "SH.600519", 100.0)

        def broker_positions(mode):
            return {}, 1_000_000.0

        plan = planner.build_and_freeze(
            self.conn, mode="sim", strategy_id="s", target={"SH.600519": 0.02},
            broker_positions=broker_positions, prices={"SH.600519": 100.0},
            as_of="2026-09-13")

        self.assertEqual(len(plan["orders"]), 1)
        self.assertEqual(plan["orders"][0]["qty"], 200)  # 2万/100 = 200 股
        result = execute.run(
            self.conn, plan_id=plan["plan_id"], plan_hash=plan["content_hash"],
            ctx=dict(RISK_CTX),
            broker_call=lambda name, args, timeout=30: {"order_id": "X-1"},
            price_of=lambda symbol: 100.0, stop_dist_of=lambda symbol: 20.0)

        self.assertEqual(result["blocked"], 0)
        self.assertEqual(result["submitted"], 1)
        status = [o["status"] for o in store.get_orders_by_plan(self.conn, plan["plan_id"])]
        self.assertEqual(status, ["submitted"])

    def test_plan_without_orders_registers_nothing(self):
        """目标与持仓一致 → 零订单计划照常冻结，orders 表零行。"""
        seed_bars(self.conn, "SH.600519", 100.0)

        def broker_positions(mode):
            return {"SH.600519": {"qty": 5000, "price": 100.0}}, 1_000_000.0

        plan = planner.build_and_freeze(
            self.conn, mode="sim", strategy_id="s", target={"SH.600519": 0.5},
            broker_positions=broker_positions, prices={"SH.600519": 100.0},
            as_of="2026-09-13")

        self.assertEqual(plan["status"], "frozen")
        self.assertEqual(plan["orders"], [])
        self.assertEqual(store.get_orders_by_plan(self.conn, plan["plan_id"]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
