"""WP9 任务 7：指令轮询并入服务内调度器（修复「独立 daemon 未跑则指令无人处理」断点）。

覆盖：poll_commands 消费/告警/永不抛；build_tick 先 tick 后 poll；一段异常不阻断另一段
且落 kv `daemon:last_error`（≤300）；processed/ 幂等不重复执行。全部离线。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
from server import scheduler  # noqa: E402
from trading_core import commands, daemon, store  # noqa: E402


class PollCommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = str(self.tmp.name)
        self.conn = store.connect(str(Path(self.home) / "t.sqlite"))
        store.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_poll_commands_consumes_kill(self):
        """kill 指令被消费：文件移 processed/、kill 文件落地、返回 result。"""
        commands.write_command(self.home, "kill", {"nonce": "k1"})
        results = daemon.poll_commands(self.conn, self.home)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["type"], "kill")
        self.assertEqual(results[0]["result"], {"ok": True})
        self.assertTrue(daemon.kill_path(self.home).exists())
        pending = list(Path(self.home, "trading-commands", "pending").glob("*.json"))
        processed = list(Path(self.home, "trading-commands", "processed").glob("*.json"))
        self.assertEqual(pending, [])
        self.assertEqual([p.name for p in processed], ["k1.json"])

    def test_poll_commands_consumes_execute_plan(self):
        """auto_execute 的生产形态：pending 的 execute_plan 被当轮取走并分派（不再滞留）。

        计划不存在时执行返回 ok:False（拒单语义），这同样要被告警——但关键是文件
        必须离开 pending/，否则每轮重试。
        """
        commands.write_command(self.home, "execute_plan",
                               {"nonce": "p1", "plan_hash": "deadbeef",
                                "expected_mode": "sim"})
        results = daemon.poll_commands(self.conn, self.home)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["type"], "execute_plan")
        self.assertFalse(results[0]["result"]["ok"])
        pending = list(Path(self.home, "trading-commands", "pending").glob("*.json"))
        self.assertEqual(pending, [])
        rows = daemon.alerts.list_recent(self.conn, limit=5)
        self.assertTrue(any(r["title"] == "指令处理失败" for r in rows), rows)

    def test_bad_command_alerts_and_moves_on(self):
        """白名单外指令：不抛出、结果为 ok:False、warn 告警一条、文件仍被移走（不每轮重试）。"""
        pending = Path(self.home, "trading-commands", "pending")
        pending.mkdir(parents=True, exist_ok=True)
        (pending / "bad.json").write_text(
            json.dumps({"type": "rm_rf", "nonce": "bad"}), encoding="utf-8")
        results = daemon.poll_commands(self.conn, self.home)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["result"]["ok"])
        rows = daemon.alerts.list_recent(self.conn, limit=5)
        self.assertTrue(any(r["title"] == "指令处理失败" and r["level"] == "warn"
                            and "rm_rf" in (r["detail"] or "") for r in rows), rows)
        self.assertEqual(list(pending.glob("*.json")), [])

    def test_poll_commands_never_raises_on_io_failure(self):
        """目录级故障：告警后返回空列表（轮询是附加段，不拖垮作业链）。"""
        with mock.patch.object(commands, "poll", side_effect=OSError("disk gone")):
            self.assertEqual(daemon.poll_commands(self.conn, self.home), [])
        rows = daemon.alerts.list_recent(self.conn, limit=5)
        self.assertTrue(any("disk gone" in (r["detail"] or "") for r in rows), rows)

    def test_processed_dedup_no_double_execution(self):
        """同 nonce 文件两次 poll 只执行一次（processed/ 同名跳过）。"""
        commands.write_command(self.home, "kill", {"nonce": "dup"})
        first = daemon.poll_commands(self.conn, self.home)
        second = daemon.poll_commands(self.conn, self.home)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])


class BuildTickPollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = str(self.tmp.name)
        self.conn = store.connect(str(Path(self.home) / "t.sqlite"))
        store.migrate(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _tick(self):
        # 传既有连接对象（不是工厂）：close_after=False，测试随后还能读 kv 断言。
        return scheduler.build_tick(self.home, jobs={}, now=lambda: "2026-09-16 16:00:30",
                                    conn=self.conn)

    def test_tick_runs_jobs_then_poll(self):
        """顺序：先作业链、后指令轮询（auto_execute 当轮写下的指令同轮被取走）。"""
        order = []
        with mock.patch.object(daemon, "tick", side_effect=lambda *a, **k: order.append("tick")), \
             mock.patch.object(daemon, "poll_commands",
                               side_effect=lambda *a, **k: order.append("poll")):
            self._tick()()
        self.assertEqual(order, ["tick", "poll"])

    def test_job_failure_does_not_block_poll(self):
        """作业链抛异常：轮询仍执行，异常落 kv daemon:last_error（≤300），并向上抛给
        Scheduler.last_error → healthz 口径不变）。"""
        polled = []
        with mock.patch.object(daemon, "tick", side_effect=RuntimeError("x" * 500)), \
             mock.patch.object(daemon, "poll_commands",
                               side_effect=lambda *a, **k: polled.append(True)):
            with self.assertRaises(RuntimeError):
                self._tick()()
        self.assertEqual(polled, [True])
        record = store.kv_get(self.conn, "daemon:last_error")
        self.assertIsNotNone(record)
        self.assertEqual(record["at"], "2026-09-16 16:00:30")
        self.assertLessEqual(len(record["error"]), 300)
        self.assertTrue(record["error"].startswith("xxx"))

    def test_poll_failure_does_not_block_jobs(self):
        """反向：轮询抛异常不阻断已完成/后续作业链，同样落 kv 并向上抛。"""
        ticked = []
        with mock.patch.object(daemon, "tick",
                               side_effect=lambda *a, **k: ticked.append(True)), \
             mock.patch.object(daemon, "poll_commands", side_effect=OSError("poll boom")):
            with self.assertRaises(RuntimeError):
                self._tick()()
        self.assertEqual(ticked, [True])
        record = store.kv_get(self.conn, "daemon:last_error")
        self.assertIn("poll boom", record["error"])

    def test_success_keeps_previous_last_error(self):
        """成功不清除最近一次异常记录（healthz 同口径：证据不因恢复而消失）。"""
        store.kv_set(self.conn, "daemon:last_error",
                     {"at": "2026-09-15 09:00:00", "error": "older"})
        with mock.patch.object(daemon, "tick", return_value={"ran": {}}), \
             mock.patch.object(daemon, "poll_commands", return_value=[]):
            self._tick()()
        self.assertEqual(store.kv_get(self.conn, "daemon:last_error")["error"], "older")

    def test_tick_returns_job_state_on_success(self):
        with mock.patch.object(daemon, "tick", return_value={"ran": {"a": 1}}), \
             mock.patch.object(daemon, "poll_commands", return_value=[]):
            self.assertEqual(self._tick()(), {"ran": {"a": 1}})

    def test_conn_object_reused_not_called_as_factory(self):
        """回归：sqlite3.Connection 可调用，必须按连接对象复用（不被当工厂调用后关闭）。"""
        with mock.patch.object(daemon, "tick", return_value={}), \
             mock.patch.object(daemon, "poll_commands", return_value=[]):
            self._tick()()
        store.kv_set(self.conn, "probe", {"alive": True})  # 连接仍可用 → 未被关闭
        self.assertEqual(store.kv_get(self.conn, "probe"), {"alive": True})

    def test_conn_factory_closes_after_tick(self):
        """工厂形态：用完即关（服务默认口径），关闭后不可再操作。"""
        import sqlite3
        factory_conn = store.connect(str(Path(self.home) / "f.sqlite"))
        store.migrate(factory_conn)
        tick = scheduler.build_tick(self.home, jobs={}, now=lambda: "2026-09-16 16:00:30",
                                    conn=lambda: factory_conn)
        with mock.patch.object(daemon, "tick", return_value={}), \
             mock.patch.object(daemon, "poll_commands", return_value=[]):
            tick()
        with self.assertRaises(sqlite3.ProgrammingError):
            factory_conn.execute("SELECT 1")


class CommandFailureTest(unittest.TestCase):
    """失败判定单一实现（轮询告警与 CLI --once 摘要共用）。"""

    def test_two_failure_shapes_and_success(self):
        self.assertEqual(daemon.command_failure({"file": "a.json", "error": "bad json"}),
                         ("a.json", "bad json"))
        self.assertEqual(
            daemon.command_failure({"type": "rm_rf",
                                    "result": {"ok": False, "error": "未知指令 rm_rf"}}),
            ("rm_rf", "未知指令 rm_rf"))
        self.assertEqual(
            daemon.command_failure({"type": "rm_rf", "result": {"ok": False}}),
            ("rm_rf", "指令失败"))
        self.assertIsNone(daemon.command_failure({"type": "kill", "result": {"ok": True}}))


class DaemonRoundTest(unittest.TestCase):
    """CLI 常驻循环一轮：轮询/告警走 daemon 单一实现，ok:False 形态也进摘要。"""

    def test_round_reports_ok_false_failures(self):
        import trading_core.cli as cli
        with tempfile.TemporaryDirectory() as home:
            conn = store.connect(str(Path(home) / "t.sqlite"))
            store.migrate(conn)
            pending = Path(home, "trading-commands", "pending")
            pending.mkdir(parents=True, exist_ok=True)
            (pending / "bad.json").write_text(
                json.dumps({"type": "rm_rf", "nonce": "bad"}), encoding="utf-8")
            commands.write_command(home, "kill", {"nonce": "good"})
            with mock.patch.object(daemon, "tick", return_value={}):
                issues = cli._daemon_round(conn, home)
            conn.close()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["error"], "未知指令 rm_rf")


if __name__ == "__main__":
    unittest.main()
