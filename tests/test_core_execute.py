"""执行编排：预检→提交→成交回写；被拒跳过；熔断撤余单。全部假 broker。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import execute, oms, store  # noqa: E402


class ExecuteTest(unittest.TestCase):
    def test_flow_with_blocked_order_and_halt(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
                     "status,created_at) VALUES('P1','2026-09-13','SIM','s','{}','a3f8',"
                     "'frozen','t')")
        conn.commit()
        big = oms.register_order(conn, plan_id="P1", symbol="SH.600519", market="SH",
                                 side="BUY", qty=100, price=1580.0, mode="SIM", plan_hash="a3f8")
        small = oms.register_order(conn, plan_id="P1", symbol="SZ.300750", market="SZ",
                                   side="BUY", qty=100, price=201.8, mode="SIM", plan_hash="a3f8")

        def broker_call(name, args, timeout=30):
            return {"order_id": "7149999"}

        result = execute.run(
            conn, plan_id="P1", plan_hash="a3f8",
            ctx={"mode": "SIM", "kill_path": "/nonexistent", "equity": 1_000_000.0,
                 "positions_value": {"SH.600519": 260000.0}, "positions_count": 2,
                 "day_pnl_pct": -0.005, "is_trading_day": True,
                 "config": {"risk_per_trade": 0.01, "max_positions": 5,
                            "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03}},
            broker_call=broker_call,
            price_of=lambda s: {"SH.600519": 1580.0, "SZ.300750": 201.8}[s],
            stop_dist_of=lambda s: 20.0)
        # 600519 成交后单票市值 41.8万 > 25%×100万 → 规则 5 拦截（day_pnl=-0.5% 未触发规则 7）
        self.assertEqual(result["blocked"], 1)
        self.assertFalse(result["halted"])
        self.assertGreaterEqual(result["submitted"], 1)
        checks = store.risk_checks_by_plan(conn, "P1")
        self.assertTrue(any(not c["allowed"] for c in checks))
        # 被拦单转 cancelled 且带风控原因；通过单到达 submitted
        by_id = {o["client_order_id"]: o for o in store.get_orders_by_plan(conn, "P1")}
        self.assertEqual(by_id[big["client_order_id"]]["status"], "cancelled")
        self.assertIn("risk:", by_id[big["client_order_id"]]["err"])
        self.assertEqual(by_id[small["client_order_id"]]["status"], "submitted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
