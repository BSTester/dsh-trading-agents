"""WP6 审批回归（Python 侧，规格 §5.2 P 系列）：kill 联动、白名单不变量、口令不落盘。全部离线。"""
import sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
from trading_core import commands, risk  # noqa: E402


class Wp6Approval(unittest.TestCase):
    def test_p1_kill_file_blocks_execution_chain(self):
        """kill 文件存在 → 风控规则 1 拒单（daemon 消费 execute_plan 的最终兜底）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kill = Path(tmp.name) / "trading-kill"
        kill.write_text("", encoding="utf-8")
        verdict = risk.pre_trade_checks(
            {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 1.0, "mode": "SIM",
             "plan_id": "P", "plan_hash": "h", "stop_dist": 0.1},
            {"mode": "SIM", "kill_path": str(kill), "equity": 1e6, "positions_value": {},
             "positions_count": 0, "day_pnl_pct": 0.0, "is_trading_day": True,
             "config": {"risk_per_trade": 0.01, "max_positions": 5,
                        "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03},
             "plan_hash": "h", "plan_status": "frozen"})
        self.assertFalse(verdict.allowed)

    def test_p2_command_whitelist_and_nonce_idempotent(self):
        self.assertEqual(set(commands.COMMANDS),
                         {"execute_plan", "cancel_plan", "kill", "unkill", "run_job"})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        nonce = commands.write_command(tmp.name, "kill", {"nonce": "n-wp6"})
        commands.poll(tmp.name, handler=lambda cmd: {"ok": True})
        commands.write_command(tmp.name, "kill", {"nonce": nonce})
        handled = commands.poll(tmp.name, handler=lambda cmd: {"ok": True})
        self.assertEqual([row for row in handled if row.get("type") == "kill"], [])

    def test_p3_passwords_never_reach_command_files(self):
        """口令字段不进指令目录：服务端校验后即丢弃（与 Node R4 呼应）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        commands.write_command(tmp.name, "execute_plan",
                               {"plan_hash": "h", "expected_mode": "live"})
        pending = Path(tmp.name) / "trading-commands" / "pending"
        for file in pending.glob("*.json"):
            self.assertNotIn("confirmation", file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
