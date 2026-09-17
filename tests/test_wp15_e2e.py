"""WP15 任务 5：L3 队列端到端 + 边界反证（规格 §10.4–§10.6）。

两条消费路径（外部定时器 headless 唤醒 / 会话打开补跑）消费**同一张表**，本测试把两条
都真跑一遍，并做两项反证：

  * **D1 定时路径**：``enqueue_research`` 入队 → ``research-tasks-claim`` 逐条领取 →
    按 kind 模拟执行 → ``research-tasks-report(ok=True)`` → 全部 ``done``；
  * **定时器没跑 / headless 被杀**：D1 领走但**不回报**（任务停 running）→ D2 领取时
    先回收（``reclaim``）再成功处理 → 任务不丢、只延后；
  * **边界反证 1（零交易写）**：把交易写路径（``broker.place``/``cancel`` 与
    ``TradeGate._write``）替换成「一调就炸」，整条 L3 链路照常跑完——研究侧够不到交易侧；
  * **边界反证 2（研究产物不进 auto_pipeline）**：跑完 L3 后规则表无 ``enabled`` 行、
    启用的策略清单不因研究产物变化——未批准规则零消费。

全部离线：临时 home + 真实 SQLite；时间用 ``now`` 显式注入（不依赖系统时钟）。
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
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from server import compute  # noqa: E402
from trading_core import autopipeline, broker, research_queue, store  # noqa: E402

TODAY = "2026-09-16"            # 周三（ISO 2026-W38，本轮含周度任务）
TOMORROW = "2026-09-17"         # 周四
NOW = "2026-09-16 16:40:00"
NEXT_MORNING = "2026-09-17 09:10:00"   # > TASK_TIMEOUT_MINUTES(30) 之后
WATCHLIST = ["SH.600519", "SZ.300750"]


class DutyQueueEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = str(Path(self.tmp.name))
        self.conn = store.connect(str(store.db_path(self.home)))
        self.addCleanup(self.conn.close)
        self._config()
        self._calendar()
        self._seed_snapshots()

    # ---- 种子 ----

    def _config(self, auto=False):
        (Path(self.home) / "trading-platform.json").write_text(
            json.dumps({"watchlist": WATCHLIST,
                        "auto_pipeline": {"enabled": auto}}, ensure_ascii=False),
            encoding="utf-8")

    def _calendar(self):
        store.upsert_calendar(self.conn, "SH", [
            {"day": day, "trade_date_type": "WHOLE", "trade_second": 14400}
            for day in (TODAY, TOMORROW)])

    def _seed_snapshots(self, date=TODAY):
        store.insert_sentiment(self.conn, date, "SH.600519", "fin_sentiment", {"score": 7})
        store.insert_f10(self.conn, "SH.600519", "analyst_consensus", {"rating": "BUY"},
                         "2026-06-30", announced_at="2026-08-15",
                         fetched_at=f"{date} 16:30:00")
        store.insert_short(self.conn, "SH.600519", date, {"ratio": 0.5})
        store.insert_plate(self.conn, date, "SH", "INDUSTRY", [{"code": "SH.LIST1"}])

    def _enqueue(self, now=NOW, today=TODAY):
        return research_queue.enqueue(self.home, "SH", conn=self.conn, today=today, now=now)

    def _tasks(self, status=None):
        return store.get_tasks(self.conn, status=status)

    def _claim(self, now=NOW):
        return compute.research_tasks_claim(home=self.home, now=now)

    def _report(self, task_id, ok=True, **kwargs):
        return compute.research_tasks_report(task_id, ok, home=self.home, **kwargs)

    def _drain(self, now=NOW, fail_first=False):
        """模拟一次完整值班：领到 null 为止。返回 (处理的 kind 列表, 领取到的任务列表)。"""
        handled, tasks, first = [], [], True
        while True:
            claimed = self._claim(now=now)
            task = claimed["task"]
            if task is None:
                return handled, tasks
            tasks.append(task)
            handled.append(task["kind"])
            ok = not (fail_first and first)
            first = False
            result = self._report(task["task_id"], ok=ok,
                                  result_ref=f"research:brief:{task['task_id']}" if ok else None,
                                  err=None if ok else "模拟执行失败")
            if ok:
                self.assertEqual(result["task"]["status"], "done")

    # ---- ① D1 定时路径：入队 → 领取 → 回报 → 全 done ----

    def test_duty_path_drains_queue(self):
        enqueued = self._enqueue()
        self.assertTrue(enqueued["ok"], enqueued)
        self.assertEqual(sorted(t["kind"] for t in enqueued["tasks"]),
                         ["daily_brief", "factor_patrol", "mining_round"],
                         "周三首轮：每日两类 + 周度一类")

        handled, tasks = self._drain()

        self.assertEqual(sorted(handled), ["daily_brief", "factor_patrol", "mining_round"])
        self.assertEqual(self._tasks(status="pending"), [])
        self.assertEqual(self._tasks(status="running"), [])
        done = self._tasks(status="done")
        self.assertEqual(len(done), 3)
        for task in done:
            self.assertTrue(task["result_ref"].startswith("research:brief:"))
        # 载荷全程只含白名单键（队列即攻击面的正面断言）
        for task in tasks:
            self.assertLessEqual(set(task["payload"]), set(store.TASK_PAYLOAD_KEYS))

    # ---- ② 定时器没跑 / headless 被杀：D2 补跑回收后成功 ----

    def test_killed_headless_is_reclaimed_and_retried_next_day(self):
        self._enqueue()
        first = self._claim(now=NOW)["task"]
        self.assertIsNotNone(first)
        self.assertEqual(first["status"], "running")
        # 模拟 headless 被杀：不回报。队列里留下一条 running。
        self.assertEqual(len(self._tasks(status="running")), 1)

        # 次日会话打开补跑：领取时顺带回收（claim 的 reclaim-first 语义）
        claimed = self._claim(now=NEXT_MORNING)
        self.assertEqual(claimed["reclaimed"]["requeued"], [first["task_id"]])
        task = claimed["task"]
        self.assertEqual(task["task_id"], first["task_id"], "回收后仍是同一条任务")
        self.assertEqual(task["timeouts"], 1, "回收计 timeouts，但不判 failed（<3）")
        self.assertEqual(task["attempts"], 0, "回收**不**消耗 attempts（R2：headless 被杀≠尝试失败）")
        self.assertIn("超时", task["err"] or "")

        self._report(task["task_id"], ok=True, result_ref="research:brief:recovered")
        self.assertEqual(self._tasks(status="done")[0]["result_ref"], "research:brief:recovered")
        # 剩下的任务继续被补跑消费掉
        handled, _ = self._drain(now=NEXT_MORNING)
        self.assertEqual(self._tasks(status="pending"), [])
        self.assertEqual(self._tasks(status="running"), [])
        self.assertNotIn("daily_brief", handled)  # 首条已在上面的补跑里处理

    # ---- ③ 边界反证：零交易写 + 未批准规则零消费 ----

    def test_boundary_no_trading_writes_and_no_auto_enable(self):
        def _explode(*_args, **_kwargs):
            raise AssertionError("L3 研究链路触达了交易写路径")

        writes = [mock.patch.object(broker, "place", _explode),
                  mock.patch.object(broker, "cancel", _explode)]
        for patcher in writes:
            patcher.start()
            self.addCleanup(patcher.stop)

        from server import trading as trading_module
        trade_gate = mock.patch.object(trading_module.TradeGate, "_write", _explode)
        trade_gate.start()
        self.addCleanup(trade_gate.stop)

        before_strategies = autopipeline.auto_pipeline_config(self.home)["strategies"]

        self._enqueue()
        self._drain()

        self.assertEqual(len(self._tasks(status="done")), 3)
        # 研究产物不启用任何规则：规则表无 enabled 行，启用策略清单不变
        self.assertEqual(store.get_rules(self.conn, status="enabled"), [])
        self.assertEqual(store.get_rules(self.conn), [],
                         "L3 只产研究产物，不写规则表")
        self.assertEqual(autopipeline.auto_pipeline_config(self.home)["strategies"],
                         before_strategies)
        # 全链结束后队列里没有 running 残留（不会卡住下一次值班）
        self.assertEqual(self._tasks(status="running"), [])

    # ---- ④ 失败路径：如实回报 → attempts 累计 → 达上限 failed 且不无限重试 ----

    def test_failed_reports_retry_then_stop(self):
        self._enqueue()
        target = self._claim(now=NOW)["task"]
        self._report(target["task_id"], ok=False, err="第一轮失败")
        after = store.get_tasks(self.conn, status="pending")
        retried = [t for t in after if t["task_id"] == target["task_id"]]
        self.assertEqual(len(retried), 1, "未达上限回 pending 等下一次值班")
        self.assertEqual(retried[0]["attempts"], 1)

        # 再失败两次 → attempts=3 判 failed（不再回队列）
        self._claim(now=NOW)
        self._report(target["task_id"], ok=False, err="第二轮失败")
        self._claim(now=NOW)
        self._report(target["task_id"], ok=False, err="第三轮失败")
        failed = store.get_tasks(self.conn, status="failed")
        self.assertEqual([t["task_id"] for t in failed], [target["task_id"]])
        titles = [r["title"] for r in
                  self.conn.execute("SELECT title FROM alerts").fetchall()]
        self.assertIn("研究任务失败", titles, "达上限要留告警，不能静默")


if __name__ == "__main__":
    unittest.main()
