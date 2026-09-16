"""WP6 服务锁定（补遗 A）：config 默认值/env 覆盖/坏 JSON；工具面契约常量。全部离线。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
from server.config import DEFAULTS, load_config  # noqa: E402


class Wp6ServiceLocks(unittest.TestCase):
    def test_defaults_frozen_values(self):
        self.assertEqual(DEFAULTS, {"port": 8397, "host": "127.0.0.1", "token": None})

    def test_env_overrides_without_file(self):
        """缺配置文件时 env 同样生效（冒烟测试的关键路径）。"""
        with tempfile.TemporaryDirectory() as home:
            previous = os.environ.get("TRADING_SERVICE_PORT")
            try:
                os.environ["TRADING_SERVICE_PORT"] = "0"
                self.assertEqual(load_config(home)["port"], 0)
                os.environ["TRADING_SERVICE_PORT"] = "99999"
                self.assertEqual(load_config(home)["port"], 8397)
                os.environ["TRADING_SERVICE_PORT"] = ""
                self.assertEqual(load_config(home)["port"], 8397)
            finally:
                if previous is None:
                    os.environ.pop("TRADING_SERVICE_PORT", None)
                else:
                    os.environ["TRADING_SERVICE_PORT"] = previous

    def test_file_overrides_and_bad_json(self):
        with tempfile.TemporaryDirectory() as home:
            target = Path(home) / "trading-platform.json"
            target.write_text(json.dumps({"service": {"port": 7000, "host": "127.0.0.1"}}), encoding="utf-8")
            self.assertEqual(load_config(home)["port"], 7000)
            target.write_text("{oops", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(home)

    def test_tool_surface_contract_markers(self):
        """工具面清单文件含通道分级错误码与业务确认排除常量（任务 D/2026-09-15 修订后仍锁定）。"""
        manifest = (ROOT / "platform" / "server" / "mcp_tools.py").read_text(encoding="utf-8")
        self.assertIn("trading/live-switch-web-only", manifest)
        # 不变式 1（规格 §5.1 A7）：confirm-decide 绝不进 MCP 工具面——常量名与端点名都在源码里
        self.assertIn("MCP_EXCLUDED_ENDPOINTS", manifest)
        # WP12 任务 4：数据面三端点并入排除集（窝轮数据/写用户自选/同步内部复权）
        self.assertIn('frozenset({"confirm-decide", "openapi_config", "openapi_test",\n'
                      '                                    "openapi_oauth", "auto_pipeline",',
                      manifest)
        for name in ("warrant_screen", "modify_user_security", "info_rehab"):
            self.assertIn(f'"{name}"', manifest)
        # WP8 任务 7：设置页端点与 confirm-decide 同属有意排除集（凭据读写是人工动作）；
        # OAuth 集成增补 openapi_oauth（授权流程 start/status/cancel 同为人工动作）
        # WP10 任务 2：auto_pipeline（自动流水线是否自动下单的总开关）同理——模型若能拨
        # 开关就等于能自己启动无人确认的执行链，故与凭据/批准同类，只在 Web 设置页可达


if __name__ == "__main__":
    unittest.main()
