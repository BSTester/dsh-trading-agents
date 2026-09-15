"""WP4 依赖锁定：指令白名单、RPC 端点名、桌面通知命令探测。全部离线。"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import commands, daemon  # noqa: E402


class Wp4Locks(unittest.TestCase):
    def test_command_whitelist_exactly_five(self):
        self.assertEqual(set(commands.COMMANDS),
                         {"execute_plan", "cancel_plan", "kill", "unkill", "run_job"})

    def test_daemon_paths(self):
        import os
        home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
        self.assertTrue(str(daemon.heartbeat_path(home)).endswith("trading-daemon.json"))
        self.assertTrue(str(daemon.commands_dir(home)).endswith("trading-commands"))

    def test_notify_command_probe(self):
        # 平台无桌面通知命令时必须返回 None（可选能力，缺失不报错）
        cmd = __import__("trading_core.platform_notify", fromlist=["x"]).detect()
        self.assertTrue(cmd is None or isinstance(cmd, str))


if __name__ == "__main__":
    unittest.main()
