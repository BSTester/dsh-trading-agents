"""WP6 依赖锁定：服务常量与两把口令常量的跨语言一致性。全部离线。"""
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Wp6Locks(unittest.TestCase):
    def test_confirmation_passwords_unchanged(self):
        store_js = (ROOT / "plugins" / "workbench" / "src" / "store.js").read_text(encoding="utf-8")
        rpc_js = (ROOT / "plugins" / "workbench" / "src" / "rpc.js").read_text(encoding="utf-8")
        self.assertIn("确认实盘", store_js)
        self.assertIn("确认执行", rpc_js)

    def test_service_defaults(self):
        config = (ROOT / "platform" / "server" / "config.py").read_text(encoding="utf-8")
        self.assertIn("8397", config)
        self.assertIn("trading-platform.json", config)
        manifest = (ROOT / "platform" / "server" / "mcp_tools.py").read_text(encoding="utf-8")
        self.assertIn("trading/live-switch-web-only", manifest)

    def test_command_whitelist_still_five(self):
        sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
        from trading_core import commands  # noqa: E402
        self.assertEqual(set(commands.COMMANDS),
                         {"execute_plan", "cancel_plan", "kill", "unkill", "run_job"})


if __name__ == "__main__":
    unittest.main()
