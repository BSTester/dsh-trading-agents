"""风控八规则：逐条拒绝 + 计划一致性（规格 §七）。kill 文件用 tmp 路径注入。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import risk, store  # noqa: E402


def _ctx(**kw):
    base = {"mode": "SIM", "kill_path": kw.pop("kill_path", "/nonexistent-kill"),
            "equity": 1_000_000.0, "positions_value": {}, "positions_count": 2,
            "day_pnl_pct": -0.01, "is_trading_day": True, "conn": None,
            "plan_hash": "a3f8", "plan_status": "frozen",
            "config": {"risk_per_trade": 0.01, "max_positions": 5,
                       "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03},
            **kw}
    return base


ORDER = {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 1580.0,
         "mode": "SIM", "plan_id": "P1", "plan_hash": "a3f8", "stop_dist": 50.0}


class RiskTest(unittest.TestCase):
    def test_all_pass(self):
        verdict = risk.pre_trade_checks(ORDER, _ctx())
        self.assertTrue(verdict.allowed)

    def test_rule1_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "trading-kill"
            p.write_text("")
            v = risk.pre_trade_checks(ORDER, _ctx(kill_path=str(p)))
        self.assertFalse(v.allowed)
        self.assertEqual(v.rule, 1)

    def test_rule2_mode_mismatch(self):
        v = risk.pre_trade_checks(ORDER, _ctx(mode="LIVE"))
        self.assertEqual(v.rule, 2)

    def test_rule3_not_trading_day(self):
        v = risk.pre_trade_checks(ORDER, _ctx(is_trading_day=False))
        self.assertEqual(v.rule, 3)

    def test_rule4_risk_per_trade(self):
        bad = dict(ORDER, stop_dist=20000.0)  # 风险 2万 > 1% 权益
        self.assertEqual(risk.pre_trade_checks(bad, _ctx()).rule, 4)

    def test_rule5_max_position_pct(self):
        v = risk.pre_trade_checks(ORDER, _ctx(positions_value={"SH.600519": 260000.0}))
        self.assertEqual(v.rule, 5)  # 成交后 41.8万 > 25% × 100万

    def test_rule6_max_positions(self):
        v = risk.pre_trade_checks(ORDER, _ctx(positions_count=5))
        self.assertEqual(v.rule, 6)

    def test_rule7_daily_loss_halt(self):
        v = risk.pre_trade_checks(ORDER, _ctx(day_pnl_pct=-0.031))
        self.assertEqual(v.rule, 7)

    def test_rule8_plan_consistency(self):
        v = risk.pre_trade_checks(dict(ORDER, plan_hash="dead"), _ctx())
        self.assertEqual(v.rule, 8)
        v2 = risk.pre_trade_checks(ORDER, _ctx(plan_status="expired"))
        self.assertEqual(v2.rule, 8)

    def test_rule8_accepts_frozen_lifecycle_states(self):
        """规格 §6.1：frozen→approved→executing 是同一冻结内容的合法生命周期；
        executing 期间逐单预检必须放行，否则执行编排自锁（WP3 验收记录披露 D1）。"""
        for status in ("frozen", "approved", "executing"):
            verdict = risk.pre_trade_checks(ORDER, _ctx(plan_status=status))
            self.assertTrue(verdict.allowed, status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
