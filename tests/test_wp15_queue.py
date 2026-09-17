"""WP15 任务 1：``research_tasks`` 队列状态机（规格 §10.2–§10.3）。

全部离线：内存/临时库直接操作，无子进程、无网络。

覆盖计划任务 1 的用例与边界：

  * 入队当日幂等（唯一键 ``kind+as_of+market``，重复入队返回已有 task_id）；
  * ``claim_task`` 取最早 pending 并置 running（``started_at`` 落库）；
  * ``finish_task``：成功 → done + result_ref；失败 → attempts+1，<3 回 pending，≥3 转 failed；
  * ``reclaim_tasks``：running 超时（>30 分钟）回收；attempts≥3 → failed；
  * 载荷白名单：未知 kind / 自由文本键 / 非 dict / 缺 as_of·market 一律 ValueError（fail-closed）；
  * 升级为 failed 时由带 home 的业务层（``research_queue.reclaim``）发 warn 告警——
    状态机在 store（纯数据层，零告警依赖），告警在业务层（与 sentiment/research_sync 同一分层）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import research_queue, store  # noqa: E402

TODAY = "2026-09-16"
NOW = "2026-09-16 16:35:00"


class ResearchQueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)

    # ---- 工具 ----

    def _payload(self, **overrides):
        payload = {"as_of": TODAY, "market": "SH", "refs": [{"source": "sentiment_snapshots",
                                                             "date": TODAY, "records": 3}]}
        payload.update(overrides)
        return payload

    def _enqueue(self, kind="daily_brief", as_of=TODAY, market="SH", created_at=None,
                 **overrides):
        return store.enqueue_task(self.conn, kind, as_of, market, self._payload(**overrides),
                                  created_at=created_at)

    def _alert_titles(self):
        return [r["title"] for r in self.conn.execute("SELECT title FROM alerts").fetchall()]

    # ---- 生命周期 ----

    def test_enqueue_claim_finish_lifecycle(self):
        """入队 → 领取（最早 pending）→ 成功完成（done + result_ref）。"""
        first = self._enqueue(kind="daily_brief", created_at="2026-09-16 16:35:00")
        second = self._enqueue(kind="factor_patrol", created_at="2026-09-16 16:35:01")

        claimed = store.claim_task(self.conn, NOW)
        self.assertEqual(claimed["task_id"], first, "按 created_at 取最早 pending")
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["started_at"], NOW)
        self.assertEqual(claimed["kind"], "daily_brief")
        self.assertEqual(claimed["payload"]["as_of"], TODAY, "payload 反序列化")
        self.assertEqual(claimed["attempts"], 0)

        nxt = store.claim_task(self.conn, "2026-09-16 16:35:30")
        self.assertEqual(nxt["task_id"], second)
        self.assertIsNone(store.claim_task(self.conn, "2026-09-16 16:36:00"),
                          "无 pending 时领取返回 None")

        done = store.finish_task(self.conn, first, ok=True, result_ref="research:report/1")
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["result_ref"], "research:report/1")
        self.assertTrue(done["finished_at"], "完成时刻落库")

    def test_enqueue_idempotent_per_kind_day_market(self):
        """当日幂等：同 (kind, as_of, market) 重复入队返回同一 task_id，不新建行。"""
        first = self._enqueue()
        again = self._enqueue()
        self.assertEqual(first, again)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM research_tasks").fetchone()["n"], 1)

        other_kind = self._enqueue(kind="factor_patrol")
        other_day = self._enqueue(as_of="2026-09-17")
        other_market = self._enqueue(market="HK")
        self.assertEqual(len({first, other_kind, other_day, other_market}), 4,
                         "kind/as_of/market 任一不同即为新任务")

    def test_finish_failure_retries_then_fails(self):
        """失败重试：attempts+1，<3 回 pending；≥3 转 failed 且 finished_at 落库。"""
        task_id = self._enqueue()
        store.claim_task(self.conn, NOW)

        first = store.finish_task(self.conn, task_id, ok=False, err="模型超时")
        self.assertEqual((first["status"], first["attempts"]), ("pending", 1))
        self.assertIsNone(first["started_at"], "回 pending 时清 started_at")
        self.assertEqual(first["err"], "模型超时")

        store.claim_task(self.conn, "2026-09-16 16:40:00")
        second = store.finish_task(self.conn, task_id, ok=False, err="再次失败")
        self.assertEqual((second["status"], second["attempts"]), ("pending", 2))

        store.claim_task(self.conn, "2026-09-16 16:45:00")
        third = store.finish_task(self.conn, task_id, ok=False, err="第三次失败")
        self.assertEqual((third["status"], third["attempts"]), ("failed", 3))
        self.assertTrue(third["finished_at"], "转 failed 记录完成时刻")

    def test_finish_unknown_task_rejected(self):
        with self.assertRaises(ValueError):
            store.finish_task(self.conn, "not-a-task", ok=True)

    # ---- 超时回收 ----

    def test_timeout_recovery_requeues_then_fails(self):
        """超时回收：running 且 started_at 早于 now-30 分钟 → pending、attempts+1；
        再超时且已达上限 → failed，并由 research_queue.reclaim 发 warn 告警。"""
        task_id = self._enqueue(created_at="2026-09-16 15:00:00")
        store.claim_task(self.conn, "2026-09-16 15:00:00")

        fresh = self._enqueue(kind="factor_patrol", created_at="2026-09-16 16:30:00")
        store.claim_task(self.conn, "2026-09-16 16:34:00")

        result = research_queue.reclaim(self.conn, str(self.home), NOW)
        self.assertEqual(result["requeued"], [task_id], "超时任务回 pending")
        self.assertEqual(result["failed"], [], "未达上限不判失败")
        self.assertEqual(result["kept"], [fresh], "未超时任务保持 running")
        row = self.conn.execute("SELECT status,attempts FROM research_tasks"
                                " WHERE task_id=?", (task_id,)).fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("pending", 1))
        self.assertNotIn("研究任务失败", self._alert_titles())

        # 第二次：attempts=1 → claim → 再超时 → attempts=2 仍 pending
        store.claim_task(self.conn, "2026-09-16 16:36:00")
        research_queue.reclaim(self.conn, str(self.home), "2026-09-16 17:10:00")
        # 第三次：attempts=2 → claim → 超时 → attempts=3 → failed + 告警
        store.claim_task(self.conn, "2026-09-16 17:20:00")
        final = research_queue.reclaim(self.conn, str(self.home), "2026-09-16 18:00:00")
        self.assertEqual([t["task_id"] for t in final["failed"]], [task_id])
        self.assertEqual(final["failed"][0]["attempts"], 3)
        self.assertIn("研究任务失败", self._alert_titles())

    def test_reclaim_timeout_window_configurable(self):
        task_id = self._enqueue()
        store.claim_task(self.conn, NOW)
        result = research_queue.reclaim(self.conn, str(self.home), "2026-09-16 16:50:00",
                                        timeout_minutes=60)
        self.assertEqual(result["requeued"], [], "未超出自定义窗口不回收")
        self.assertEqual(store.get_tasks(self.conn, status="running")[0]["task_id"], task_id)

    # ---- 载荷白名单（fail-closed） ----

    def test_payload_whitelist_rejects_free_text_and_unknown_kind(self):
        with self.assertRaises(ValueError) as ctx:
            self._enqueue(kind="arbitrary_prompt")
        self.assertIn("daily_brief", str(ctx.exception), "错误消息列出白名单 kind")

        with self.assertRaises(ValueError) as ctx:
            self._enqueue(prompt="顺手帮我下单")
        self.assertIn("prompt", str(ctx.exception))

        with self.assertRaises(ValueError):
            store.enqueue_task(self.conn, "daily_brief", TODAY, "SH", ["not-a-dict"])
        with self.assertRaises(ValueError):
            store.enqueue_task(self.conn, "daily_brief", "", "SH", self._payload())
        with self.assertRaises(ValueError):
            store.enqueue_task(self.conn, "daily_brief", TODAY, "", self._payload())
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM research_tasks").fetchone()["n"], 0,
            "非法载荷零落库")

    def test_allowed_payload_keys_roundtrip(self):
        """白名单键全部可用（as_of/market/refs/digest_ref/symbols/factor_list/window）。"""
        payload = {"as_of": TODAY, "market": "SH", "refs": [{"source": "kv"}],
                   "digest_ref": "kv:daily:digest", "symbols": ["SH.600519"],
                   "factor_list": ["momentum_20"], "window": 120}
        task_id = store.enqueue_task(self.conn, "factor_patrol", TODAY, "SH", payload)
        row = [t for t in store.get_tasks(self.conn) if t["task_id"] == task_id][0]
        self.assertEqual(row["payload"], payload)

    def test_get_tasks_filter_and_limit(self):
        self._enqueue(kind="daily_brief", created_at="2026-09-16 16:35:00")
        self._enqueue(kind="factor_patrol", created_at="2026-09-16 16:35:01")
        store.claim_task(self.conn, NOW)
        self.assertEqual(len(store.get_tasks(self.conn)), 2)
        self.assertEqual(len(store.get_tasks(self.conn, status="running")), 1)
        self.assertEqual(len(store.get_tasks(self.conn, status="pending")), 1)
        self.assertEqual([t["kind"] for t in store.get_tasks(self.conn, limit=1)],
                         ["daily_brief"], "按 created_at 稳定排序")


if __name__ == "__main__":
    unittest.main()
