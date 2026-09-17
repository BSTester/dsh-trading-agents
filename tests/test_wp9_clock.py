"""WP9 任务 8：DSH_FAKE_NOW 跨进程假时钟 + execute_plan 券商注入接线。

覆盖：
  * ``now_stamp`` 优先级（显式 > DSH_FAKE_NOW > 真实时间）与非法格式 fail-closed；
  * ``now_fn`` 采样一次（采样后改环境变量不影响已取样本——避免跨分钟抖动）；
  * ``warn_fake_now`` 每进程一次 warn 告警（工作台可见）与无变量时 no-op；
  * CLI ``auto-execute --now`` 非法格式 → 信封 + 退出 1（不抛栈）；
  * ``_default_executor`` 注入券商通道（WP9 e2e 暴露的接线缺口回归）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import cli, clock, daemon  # noqa: E402


class FakeNowTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get(daemon.FAKE_NOW_ENV)
        os.environ.pop(daemon.FAKE_NOW_ENV, None)
        clock._fake_now_alerted = False  # 去重位是模块级：逐用例复位（WP9 拆分后归 clock）

    def tearDown(self):
        if self._saved is None:
            os.environ.pop(daemon.FAKE_NOW_ENV, None)
        else:
            os.environ[daemon.FAKE_NOW_ENV] = self._saved
        clock._fake_now_alerted = False

    # ---- ① 优先级 ----

    def test_explicit_beats_env_beats_real(self):
        os.environ[daemon.FAKE_NOW_ENV] = "2026-09-16 09:35:00"
        self.assertEqual(daemon.now_stamp(), "2026-09-16 09:35:00")
        self.assertEqual(daemon.now_stamp("2026-01-02 03:04:05"), "2026-01-02 03:04:05")
        os.environ.pop(daemon.FAKE_NOW_ENV)
        self.assertNotEqual(daemon.now_stamp(), "2026-09-16 09:35:00")  # 回落真实时间

    # ---- ② 非法格式 fail-closed ----

    def test_invalid_fake_now_raises(self):
        os.environ[daemon.FAKE_NOW_ENV] = "2026/09/16 09:35"
        with self.assertRaises(ValueError) as ctx:
            daemon.now_stamp()
        self.assertIn(daemon.FAKE_NOW_ENV, str(ctx.exception))

    # ---- ③ now_fn 采样一次 ----

    def test_now_fn_samples_once(self):
        os.environ[daemon.FAKE_NOW_ENV] = "2026-09-16 09:35:00"
        sample = daemon.now_fn()
        os.environ[daemon.FAKE_NOW_ENV] = "2026-09-16 16:20:00"
        self.assertEqual(sample(), "2026-09-16 09:35:00")
        self.assertEqual(daemon.now_fn()(), "2026-09-16 16:20:00")

    # ---- ④ 假时钟告警：每进程一次 ----

    def test_warn_fake_now_once_per_process(self):
        from trading_core import store
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            conn = store.connect(str(home / "t.sqlite"))
            self.addCleanup(conn.close)

            os.environ.pop(daemon.FAKE_NOW_ENV, None)
            self.assertFalse(daemon.warn_fake_now(conn, str(home)))  # 未设置：no-op

            os.environ[daemon.FAKE_NOW_ENV] = "2026-09-16 09:35:00"
            self.assertTrue(daemon.warn_fake_now(conn, str(home)))
            self.assertFalse(daemon.warn_fake_now(conn, str(home)))  # 第二次不再打扰

            rows = [dict(r) for r in conn.execute(
                "SELECT level, title, detail FROM alerts").fetchall()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["level"], "warn")
            self.assertIn("假时钟", rows[0]["title"])
            self.assertIn("2026-09-16 09:35:00", rows[0]["detail"])
            self.assertFalse(daemon.warn_fake_now(None, str(home)))  # 无 conn：静默

    # ---- ⑤ CLI --now 非法：信封 + 退出 1 ----

    def test_cli_auto_execute_invalid_now_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch("sys.stdout") as stdout:
                code = cli.main(["auto-execute", "--market", "SH", "--home", str(home),
                                 "--now", "bad-stamp", "--db", str(home / "t.sqlite")])
            self.assertEqual(code, 1)
            payload = json.loads("".join(c.args[0] for c in stdout.write.call_args_list))
            self.assertFalse(payload["ok"])
            self.assertIn("YYYY-MM-DD HH:MM:SS", payload["error"])


class DefaultExecutorBrokerTest(unittest.TestCase):
    """``_default_executor`` 必须注入券商通道（否则自动执行永远停在「未接入」拒绝态）。

    WP13 任务 2 起通道 callable 由 ``trading_datasource.channel.sim_call(home)`` 构造
    （openapi 就绪走 REST，否则原样交 MCP）——本测试随之钉住新接线契约，
    fail-closed 语义（导入失败 → None）保持不变。
    """

    def test_broker_call_injected(self):
        seen = {}
        sentinel = object()

        def fake_execute_plan(conn, home, cmd, **kwargs):
            seen.update(kwargs)
            return {"ok": True}

        fake_channel = mock.Mock()
        fake_channel.sim_call = mock.Mock(name="sim_call", return_value=sentinel)
        with mock.patch.object(daemon, "_execute_plan", fake_execute_plan), \
                mock.patch.dict("sys.modules",
                                {"trading_datasource.channel": fake_channel}):
            daemon._default_executor(None, "/tmp/home", {"plan_hash": "H"})

        self.assertIs(seen.get("broker_call"), sentinel)
        fake_channel.sim_call.assert_called_once_with("/tmp/home")

    def test_import_failure_keeps_none(self):
        seen = {}

        def fake_execute_plan(conn, home, cmd, **kwargs):
            seen.update(kwargs)
            return {"ok": False, "error": "daemon 未接入券商通道（需注入 broker_call）"}

        real_import = __import__

        def failing_import(name, *args, **kwargs):
            if name == "trading_datasource.channel":
                raise ImportError("no channel")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(daemon, "_execute_plan", fake_execute_plan), \
                mock.patch("builtins.__import__", failing_import):
            result = daemon._default_executor(None, "/tmp/home", {"plan_hash": "H"})

        self.assertIsNone(seen.get("broker_call"))       # 保持 None（fail-closed）
        self.assertIn("未接入券商通道", result["error"])  # 由 _execute_plan 如实拒绝


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
