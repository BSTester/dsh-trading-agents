"""WP4 只读快照子命令（snapshot-plan/schedule/reconcile）：Host RPC 的唯一取数通道。
全部离线：临时库 + 注入心跳文件。"""
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import alerts, cli, daemon, oms, store  # noqa: E402


class SnapshotTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)

    def _seed_plan_with_order(self, plan_id="P1", plan_hash="h1", status="frozen"):
        self.conn.execute(
            "INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
            (plan_id, "2026-09-13", "SIM", "s", '{"SH.600519": 0.5}', plan_hash,
             status, "2026-09-13 10:00:00"))
        self.conn.commit()
        o = oms.register_order(self.conn, plan_id, "SH.600519", "SH", "BUY", 100, 100.0,
                               "SIM", plan_hash)
        oms.transition(self.conn, o["client_order_id"], "frozen")
        oms.transition(self.conn, o["client_order_id"], "submitting")
        oms.transition(self.conn, o["client_order_id"], "submitted", broker_order_id="9")
        store.insert_fill(self.conn, "F1", o["client_order_id"], 100.5, 100, "2026-09-13 10:01:00")
        return o

    def _cli(self, argv):
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = cli.main([*argv, "--db", str(self.home / "t.sqlite")])
        self.assertEqual(code, 0)
        return json.loads(buffer.getvalue())


class SnapshotPlanTest(SnapshotTestBase):
    def test_plan_snapshot_lists_plan_orders_verdicts_and_alerts(self):
        self._seed_plan_with_order()
        self.conn.execute(
            "INSERT INTO risk_checks(plan_id,symbol,rule,allowed,reason,checked_at)"
            " VALUES('P1','SH.600519',0,1,'','2026-09-13 10:00:01')")
        self.conn.commit()
        alerts.emit(self.conn, home=str(self.home), level="warn", title="缺口", detail="缺 2 日")
        out = self._cli(["snapshot-plan"])
        self.assertEqual(out["plans"][0]["plan_id"], "P1")
        self.assertEqual(out["plans"][0]["target"], {"SH.600519": 0.5})
        order = out["plans"][0]["orders"][0]
        self.assertEqual(order["status"], "submitted")
        self.assertEqual(order["broker_order_id"], "9")
        self.assertIn("通过", order["risk_verdict"])
        self.assertEqual(out["alerts"][0]["title"], "缺口")

    def test_plan_snapshot_blocked_verdict_names_rule(self):
        self._seed_plan_with_order(status="frozen")
        self.conn.execute(
            "INSERT INTO risk_checks(plan_id,symbol,rule,allowed,reason,checked_at)"
            " VALUES('P1','SH.600519',4,0,'单笔风险 12000 > 权益×0.01','2026-09-13 10:00:01')")
        self.conn.commit()
        out = self._cli(["snapshot-plan"])
        verdict = out["plans"][0]["orders"][0]["risk_verdict"]
        self.assertIn("规则4", verdict)
        self.assertIn("拦截", verdict)


class SnapshotScheduleTest(SnapshotTestBase):
    def test_schedule_snapshot_reports_heartbeat_jobs_kill_halt(self):
        daemon.write_heartbeat(self.home, {"heartbeat": "2026-09-14 16:00:30",
                                           "last_job": "sync_bars", "next": ""})
        store.kv_set(self.conn, "daemon:state", {"ran": {"SH:sync_bars:2026-09-14": "2026-09-14 16:00:30"}})
        store.set_halt(self.conn, True, reason="daily_loss")
        daemon.kill_path(self.home).touch()
        out = self._cli(["snapshot-schedule", "--home", str(self.home)])
        self.assertEqual(out["heartbeat"]["last_job"], "sync_bars")
        self.assertEqual(out["jobs"][0]["job"], "SH:sync_bars:2026-09-14")
        self.assertTrue(out["kill"])
        self.assertTrue(out["halt"])
        self.assertFalse(out["critical"])


class SnapshotReconcileTest(SnapshotTestBase):
    def test_reconcile_snapshot_outputs_diffs_tca_chain(self):
        self._seed_plan_with_order()
        store.kv_set(self.conn, "reconcile:latest",
                     {"diffs": [{"symbol": "SH.600519", "kind": "qty", "local": 100, "broker": 99}],
                      "at": "2026-09-13 10:05:00"})
        from trading_core import tca
        tca.record(self.conn, "c1", "SH.600519", arrival=100.0, filled=100.5, side="BUY")
        out = self._cli(["snapshot-reconcile"])
        self.assertEqual(out["diffs"][0]["kind"], "qty")
        self.assertEqual(out["tca"]["rows"][0]["symbol"], "SH.600519")
        chain = out["chain"][0]
        self.assertEqual(chain["plan_id"], "P1")
        self.assertEqual(chain["orders"][0]["client_order_id"],
                         self.conn.execute("SELECT client_order_id FROM orders").fetchone()[0])
        self.assertEqual(chain["orders"][0]["fills"][0]["price"], 100.5)

    def test_reconcile_diff_cli_persists_latest_diffs(self):
        local = str(self.home / "local.json")
        broker = str(self.home / "broker.json")
        Path(local).write_text(json.dumps({"SH.600519": {"qty": 100}}), encoding="utf-8")
        Path(broker).write_text(json.dumps({"SH.600519": {"qty": 90}}), encoding="utf-8")
        out = self._cli(["reconcile-diff", "--local", local, "--broker", broker])
        self.assertEqual(len(out["diffs"]), 1)
        latest = store.kv_get(self.conn, "reconcile:latest")
        self.assertEqual(latest["diffs"][0]["kind"], "qty")
        self.assertTrue(latest["at"])


if __name__ == "__main__":
    unittest.main()
