"""WP9 代码质量审查 S1：daemon 职责拆分的结构守护测试。

拆分的验收不是「文件变小」，而是三条不变式：
  1. **兼容面不变**：既有调用方用 ``daemon.X`` 取得的名字必须仍是同一个对象
     （不是复制品）——否则出现「两处实现各改一份」的漂移；
  2. **依赖方向无环**：``clock``/``autopipeline``/``autopilot`` 在**模块 import 期**
     不得拉起 ``trading_core.daemon``（逆向依赖只能函数内延迟导入）；
  3. **归属正确**：JOBS_DEFAULT 与 kill 文件约定仍在 daemon（拆分方案明示保留），
     时钟/装配/自动执行分别归新模块。

零行为变化由既有 1382 项测试承担（本文件只断言结构）。
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import autopilot, autopipeline, clock, daemon  # noqa: E402


class ReexportIdentityTest(unittest.TestCase):
    """拆出去的实现必须是**同一对象**再导出，不许留副本。"""

    def test_clock_names_are_same_object(self):
        for name in ("FAKE_NOW_ENV", "now_stamp", "now_fn", "warn_fake_now",
                     "_real_now"):
            self.assertIs(getattr(daemon, name), getattr(clock, name), name)

    def test_autopipeline_names_are_same_object(self):
        for name in ("AUTO_PIPELINE_DEFAULTS", "GLOBAL_CHAIN", "auto_pipeline_config",
                     "build_jobs"):
            self.assertIs(getattr(daemon, name), getattr(autopipeline, name), name)

    def test_autopilot_names_are_same_object(self):
        self.assertIs(daemon.auto_execute, autopilot.auto_execute)


class OwnershipTest(unittest.TestCase):
    """归属：拆分方案明示 daemon 保留的，不许跟着搬走。"""

    def test_jobs_default_stays_in_daemon(self):
        self.assertEqual(list(daemon.JOBS_DEFAULT), ["SH", "HK", "US"])
        self.assertTrue(daemon.JOBS_DEFAULT["SH"])
        self.assertFalse(hasattr(autopipeline, "JOBS_DEFAULT"))

    def test_path_conventions_stay_in_daemon(self):
        # kill 文件约定 / 心跳 / 指令目录 / 平台配置读：唯一实现留在 daemon
        self.assertTrue(str(daemon.kill_path("/tmp/home")).endswith("trading-kill"))
        self.assertTrue(str(daemon.heartbeat_path("/tmp/home")).endswith(
            "trading-daemon.json"))
        self.assertTrue(str(daemon.commands_dir("/tmp/home")).endswith("trading-commands"))
        for name in ("kill_path", "heartbeat_path", "commands_dir", "write_heartbeat",
                     "platform_config", "risk_config", "RISK_DEFAULTS", "tick",
                     "handle_command", "poll_commands", "command_failure",
                     "_execute_plan", "_default_executor", "_positions_ctx",
                     "_last_close", "resolve_command", "_run_job", "_subprocess_runner"):
            self.assertTrue(callable(getattr(daemon, name)) or
                            isinstance(getattr(daemon, name), dict), name)

    def test_dedup_bit_lives_in_clock(self):
        """假时钟去重位随 warn_fake_now 迁到 clock（模块属性重绑定无法代理，
        故两处测试的复位目标同步改为 clock）。"""
        self.assertIn("_fake_now_alerted", vars(clock))
        self.assertNotIn("_fake_now_alerted", vars(daemon))


class NoModuleLevelCycleTest(unittest.TestCase):
    """模块 import 期不得拉起 daemon——逆向依赖只能延迟到函数内。

    子进程断言（同进程里其它测试早已 import 过 daemon，无法证明 import 顺序）。
    """

    def _import_then_probe(self, module):
        env = dict(os.environ)
        core = str(ROOT / "plugins" / "core" / "python")
        env["PYTHONPATH"] = core + os.pathsep + env.get("PYTHONPATH", "")
        code = (
            f"import trading_core.{module} as m, sys; "
            "print('DAEMON_LOADED' if 'trading_core.daemon' in sys.modules else 'CLEAN')"
        )
        out = subprocess.run([sys.executable, "-c", code], env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_clock_does_not_pull_daemon(self):
        self.assertEqual(self._import_then_probe("clock"), "CLEAN")

    def test_autopipeline_does_not_pull_daemon(self):
        self.assertEqual(self._import_then_probe("autopipeline"), "CLEAN")

    def test_autopilot_does_not_pull_daemon(self):
        self.assertEqual(self._import_then_probe("autopilot"), "CLEAN")

    def test_daemon_first_import_still_works(self):
        """反向：入口从 daemon 开始（服务/CLI 的真实路径）必须能完成 import。"""
        env = dict(os.environ)
        core = str(ROOT / "plugins" / "core" / "python")
        env["PYTHONPATH"] = core + os.pathsep + env.get("PYTHONPATH", "")
        code = (
            "import trading_core.daemon as d; "
            "assert d.build_jobs and d.auto_execute and d.now_stamp; print('OK')"
        )
        out = subprocess.run([sys.executable, "-c", code], env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "OK")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
