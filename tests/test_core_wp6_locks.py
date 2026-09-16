"""WP6 依赖锁定：服务常量与两把口令常量的跨端一致性。全部离线。

（2026-09-16 WP7 面板退役修订：原实现读取 legacy 面板源 `store.js`/`rpc.js` 比对口令字面量；
面板与 Host RPC 删除后，两把口令的唯一落点在服务侧源码，锁定目标随之改为 Python 实现。）
"""
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Wp6Locks(unittest.TestCase):
    def test_confirmation_passwords_unchanged(self):
        # 「确认实盘」= sim→live 的模式切换口令（store 访问层）；
        # 「确认执行」= 已冻结计划的执行口令（app 的 plan-execute 面）。
        store_py = (ROOT / "platform" / "server" / "store_access.py").read_text(encoding="utf-8")
        app_py = (ROOT / "platform" / "server" / "app.py").read_text(encoding="utf-8")
        self.assertIn("确认实盘", store_py)
        self.assertIn("确认执行", app_py)

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
