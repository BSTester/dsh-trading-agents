"""WP4 指令端到端（离线）：指令目录 → daemon 分派 → 状态机推进 → 快照 JSON。
规格 §8.2/§九「指令队列端到端」的自动化部分；3 交易日 runbook 为人工步骤，
结果记入计划文件验收记录（不在本测试范围）。"""
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import alerts, cli, commands, daemon, oms, store  # noqa: E402


class Wp4E2eTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        # 冻结计划 + 一笔冻结订单（执行后应推进到 submitted）
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
            "status,created_at) VALUES('P1','2026-09-13','SIM','s',"
            "'{\"SH.600519\": 0.5}','h1','frozen','2026-09-13 10:00:00')")
        self.conn.commit()
        o = oms.register_order(self.conn, "P1", "SH.600519", "SH", "BUY", 100, 100.0, "SIM", "h1")
        oms.transition(self.conn, o["client_order_id"], "frozen")

    def _poll(self, executor=None):
        """真实轮询 + 真实分派；execute_plan 的券商通道按计划注入假通道（离线）。"""
        def handler(cmd):
            return daemon.handle_command(
                self.conn, str(self.home), cmd,
                executor=executor or (lambda conn, home, cmd: daemon._execute_plan(
                    conn, home, cmd,
                    broker_call=lambda name, args, timeout=30: {"order_id": "1"},
                    equity=1_000_000.0, today="2026-09-14", calendar_ok=True)))
        return commands.poll(str(self.home), handler=handler)

    def _cli_json(self, argv):
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = cli.main([*argv, "--db", str(self.home / "t.sqlite")])
        self.assertEqual(code, 0)
        return json.loads(buffer.getvalue())

    def test_command_pipeline_end_to_end(self):
        # 1) kill：指令文件 → 轮询消费 → kill 文件存在，调度快照可见
        commands.write_command(str(self.home), "kill", {})
        out = self._poll()
        self.assertTrue(out[0]["result"]["ok"])
        self.assertTrue(daemon.kill_path(self.home).exists())
        schedule = self._cli_json(["snapshot-schedule", "--home", str(self.home)])
        self.assertTrue(schedule["kill"])
        # 2) unkill：解除后 kill 文件删除（执行前置条件复位）
        commands.write_command(str(self.home), "unkill", {})
        self._poll()
        self.assertFalse(daemon.kill_path(self.home).exists())
        # 3) execute_plan：真实分派 + 假券商通道 → 状态机推进
        commands.write_command(str(self.home), "execute_plan",
                               {"plan_hash": "h1", "expected_mode": "SIM"})
        out = self._poll()
        self.assertTrue(out[0]["result"]["ok"], out[0])
        order = self.conn.execute("SELECT status FROM orders WHERE plan_id='P1'").fetchone()
        self.assertEqual(order["status"], "submitted")
        self.assertEqual(store.get_plan(self.conn, "P1")["status"], "executing")
        # 4) 快照 JSON（Host pycore 取数口径）反映最新状态
        plan = self._cli_json(["snapshot-plan"])
        self.assertEqual(plan["plans"][0]["status"], "executing")
        self.assertEqual(plan["plans"][0]["orders"][0]["status"], "submitted")
        # 5) 对账差异 → critical 告警 → 心跳标志位（工作台红点依据）
        store.kv_set(self.conn, "reconcile:latest",
                     {"diffs": [{"symbol": "SH.600519", "kind": "qty", "local": 100, "broker": 99}],
                      "at": "2026-09-13 10:05:00"})
        alerts.emit(self.conn, home=str(self.home), level="critical",
                    title="对账差异", detail="SH.600519 数量差 1")
        hb = json.loads(daemon.heartbeat_path(self.home).read_text())
        self.assertTrue(hb["critical"])
        self.assertEqual(hb["critical_title"], "对账差异")
        reconcile = self._cli_json(["snapshot-reconcile"])
        self.assertEqual(reconcile["diffs"][0]["kind"], "qty")
        self.assertEqual(reconcile["alerts"][0]["level"], "critical")
        self.assertEqual(reconcile["chain"][0]["plan_id"], "P1")
        # 6) processed 去重：同 nonce 指令不会二次执行（幂等三件套的队列侧）
        self.assertEqual(self._poll(), [])

    def test_execute_plan_refuses_when_kill_active(self):
        """kill switch 生效时 execute_plan 被风控规则 1 拒绝：订单取消并留痕。"""
        daemon.kill_path(self.home).touch()
        commands.write_command(str(self.home), "execute_plan",
                               {"plan_hash": "h1", "expected_mode": "SIM"})
        out = self._poll()
        self.assertTrue(out[0]["result"]["ok"])
        statuses = {row["status"] for row in self.conn.execute(
            "SELECT status FROM orders WHERE plan_id='P1'").fetchall()}
        self.assertEqual(statuses, {"cancelled"})
        checks = self.conn.execute(
            "SELECT rule, allowed FROM risk_checks WHERE plan_id='P1'").fetchall()
        self.assertEqual([(c["rule"], c["allowed"]) for c in checks], [(1, 0)])


if __name__ == "__main__":
    unittest.main()
