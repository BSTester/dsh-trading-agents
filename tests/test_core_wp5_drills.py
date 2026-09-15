"""WP5 演练内核：kill 状态机与 halt 生命周期（离线）。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import drills, risk  # noqa: E402


class DrillsTest(unittest.TestCase):
    def test_kill_lifecycle(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kill = Path(tmp.name) / "trading-kill"
        drills.kill_on(kill)
        v = risk.pre_trade_checks(
            {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 1.0, "mode": "SIM",
             "plan_id": "P", "plan_hash": "h", "stop_dist": 0.1},
            {"mode": "SIM", "kill_path": str(kill), "equity": 1e6, "positions_value": {},
             "positions_count": 0, "day_pnl_pct": 0.0, "is_trading_day": True,
             "config": {"risk_per_trade": 0.01, "max_positions": 5,
                        "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03},
             "plan_hash": "h", "plan_status": "frozen"})
        self.assertFalse(v.allowed)
        drills.kill_off(kill)
        self.assertFalse(kill.exists())

    def test_run_kill_drill_reports_ok(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kill = Path(tmp.name) / "trading-kill"
        result = drills.run_kill_drill(kill)
        self.assertTrue(result["ok"])
        self.assertTrue(result["rejected"])
        self.assertEqual(result["rule"], 1)
        self.assertTrue(result["cleared"])
        self.assertFalse(kill.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
