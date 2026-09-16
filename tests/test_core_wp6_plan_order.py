"""WP6 回归：计划端点口径必须「最新在前」（评审 H1）。

事实链：store.list_plans 是 ORDER BY created_at, plan_id **升序**（通用访问器语义，
本次不反转）；snapshots 端点在边界翻转为最新在前。Web「当前计划」= plans[0] 与
audit 的 chain[0] 都吃这个口径——若端点透传升序，多计划并存时会取到最旧计划，
执行入口就会把最旧计划的 hash 提交给 daemon（daemon 不拒绝陈旧计划）。
全部离线：临时 sqlite + 两个 created_at 不同的计划。
"""
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import cli, store  # noqa: E402

EARLY = ("P_EARLY", "2026-09-13 09:00:00", "hash-early", "frozen")
LATE = ("P_LATE", "2026-09-13 11:00:00", "hash-late", "frozen")


class PlanOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        # 插入顺序刻意「晚的在前」，确保断言不依赖插入顺序，只依赖 created_at
        for plan_id, created_at, plan_hash, status in (LATE, EARLY):
            self._seed(plan_id, created_at, plan_hash, status)

    def _seed(self, plan_id, created_at, plan_hash, status):
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
            "status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (plan_id, "2026-09-13", "SIM", "s", '{"SH.600519": 0.5}', plan_hash,
             status, created_at))
        self.conn.commit()
        self.conn.execute(
            "INSERT INTO orders(client_order_id,plan_id,symbol,market,side,qty,price,"
            "status,broker_order_id,mode,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"c-{plan_id}", plan_id, "SH.600519", "SH", "BUY", 100, 100.0,
             "draft", None, "SIM", created_at, created_at))
        self.conn.commit()

    def _cli(self, argv):
        buffer = StringIO()
        with redirect_stdout(buffer):
            code = cli.main([*argv, "--db", str(self.home / "t.sqlite")])
        self.assertEqual(code, 0)
        return json.loads(buffer.getvalue())

    def test_store_accessor_stays_ascending(self):
        """通用访问器语义不反转：升序仍在 store 层，差异只在端点边界归一。"""
        self.assertEqual([p["plan_id"] for p in store.list_plans(self.conn)],
                         ["P_EARLY", "P_LATE"])

    def test_plan_snapshot_first_plan_is_newest(self):
        """H1：plans[0] 必须是最新计划，其 hash 才是可提交给 daemon 的执行口径。"""
        out = self._cli(["snapshot-plan"])
        self.assertEqual([p["plan_id"] for p in out["plans"]], ["P_LATE", "P_EARLY"])
        self.assertEqual(out["plans"][0]["content_hash"], "hash-late")
        self.assertEqual(out["plans"][0]["created_at"], "2026-09-13 11:00:00")
        # Web 执行入口取 plans[0].content_hash；这里断言它 != 最旧计划的 hash
        self.assertNotEqual(out["plans"][0]["content_hash"], "hash-early")

    def test_plan_snapshot_single_plan_unaffected(self):
        """单计划场景保持原行为（既有 test_core_wp4_snapshots 的用例不受影响）。"""
        self.conn.execute("DELETE FROM orders WHERE plan_id='P_EARLY'")
        self.conn.execute("DELETE FROM plans WHERE plan_id='P_EARLY'")
        self.conn.commit()
        out = self._cli(["snapshot-plan"])
        self.assertEqual([p["plan_id"] for p in out["plans"]], ["P_LATE"])

    def test_reconcile_chain_first_is_newest(self):
        """既有行为不回归：chain[0] 同样是（且必须仍是）最新计划。"""
        out = self._cli(["snapshot-reconcile"])
        self.assertEqual([plan["plan_id"] for plan in out["chain"]], ["P_LATE", "P_EARLY"])
        self.assertEqual(out["chain"][0]["created_at"], "2026-09-13 11:00:00")
        # chain 展开的订单也属于该最新计划（不是最旧计划的订单）
        self.assertEqual([o["client_order_id"] for o in out["chain"][0]["orders"]], ["c-P_LATE"])


if __name__ == "__main__":
    unittest.main()
