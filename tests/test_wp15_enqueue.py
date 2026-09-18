"""WP15 任务 2：``enqueue-research`` 作业入基础链（规格 §10.3–§10.4）。

全部离线：不跑子进程、不联网（入队本身是纯本地动作——「零 LLM」由测试 ⑤ 直接断言）。

覆盖计划任务 2 的 ①–⑤ 与边界：

  * ① 每交易日入队 ``daily_brief``（digest/情绪/F10/做空/板块 引用 + 该市场关注池）
    与 ``factor_patrol``（因子清单 + 窗口）；
  * ② 周度 ``mining_round``：本周首次入队、同周重复不新增、下周新增；
  * ③ 当日幂等：重复入队返回既有 task_id（``created=False``），且**不打断已推进的任务**；
  * ④ ``auto_pipeline.enabled=False`` 时**仍入队**——研究任务与交易开关解耦（基础链）；
  * ⑤ 入队零 LLM/零子进程（``subprocess.run`` 被替换成抛错也照常成功）；
  * 边界：会话未收盘软跳过 / 未知市场 fail-closed / 日历缺失退化标源 + 告警 /
    关注池为空软跳过 / CLI 端到端退出码 / JOBS_DEFAULT 入链时点与关闭态回归 /
    pipeline 阶段与告警标题登记。
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import (autopipeline, cli, daemon, factors, pipeline,  # noqa: E402
                          research_queue, store)

TODAY = "2026-09-16"          # 周三（ISO 2026-W38）
THURSDAY = "2026-09-17"       # 同周
NEXT_MONDAY = "2026-09-21"    # ISO 2026-W39
WATCHLIST = ["SH.600519", "SZ.300750", "HK.00700"]


class EnqueueJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        self._watchlist()

    # ---- 种子工具 ----

    def _watchlist(self, symbols=None, auto=False):
        cfg = {"watchlist": WATCHLIST if symbols is None else symbols,
               "auto_pipeline": {"enabled": auto}}
        (self.home / "trading-platform.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def _calendar(self, days=(TODAY, THURSDAY, NEXT_MONDAY)):
        store.upsert_calendar(self.conn, "SH", [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in days])

    def _seed_snapshots(self, date=TODAY):
        """种当日研究/情绪数据：情绪 2 行、F10 1 行、做空 1 行、板块 1 行。

        F10 的「当日」是**观测日**（``fetched_at`` 日期）——必须显式给观测时刻，
        否则缺省取真实时间，引用行数会落不到被测日期上。
        """
        store.insert_sentiment(self.conn, date, "SH.600519", "fin_sentiment", {"score": 7})
        store.insert_sentiment(self.conn, date, "SH.600519", "fin_news", {"items": []})
        store.insert_f10(self.conn, "SH.600519", "analyst_consensus", {"rating": "BUY"},
                         "2026-06-30", announced_at="2026-08-15",
                         fetched_at=f"{date} 16:30:00")
        store.insert_short(self.conn, "SH.600519", date, {"ratio": 0.5})
        store.insert_plate(self.conn, date, "SH", "INDUSTRY", [{"code": "SH.LIST1"}])

    def _enqueue(self, market="SH", today=TODAY, now=None):
        return research_queue.enqueue(str(self.home), market, conn=self.conn,
                                      today=today, now=now)

    def _alert_titles(self):
        return [r["title"] for r in self.conn.execute("SELECT title FROM alerts").fetchall()]

    def _tasks(self):
        return store.get_tasks(self.conn)

    # ---- ① 每日两类任务 ----

    def test_daily_brief_and_factor_patrol_payloads(self):
        self._calendar()
        self._seed_snapshots()
        result = self._enqueue()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["date"], TODAY)
        self.assertEqual(result["date_source"], "session")
        self.assertEqual(result["market"], "SH")
        self.assertEqual(sorted(t["kind"] for t in self._tasks()),
                         ["daily_brief", "factor_patrol", "mining_round"],
                         "周三本轮含周度任务（本周尚未入队）")

        brief = next(t for t in self._tasks() if t["kind"] == "daily_brief")
        payload = brief["payload"]
        self.assertEqual(payload["as_of"], TODAY)
        self.assertEqual(payload["market"], "SH")
        self.assertEqual(payload["digest_ref"], "kv:daily:digest")
        self.assertEqual(payload["symbols"], ["SH.600519", "SZ.300750"],
                         "只带该市场链的关注池（HK 不进 SH 的简报）")
        refs = {r["source"]: r for r in payload["refs"]}
        self.assertEqual(refs["sentiment_snapshots"]["records"], 2)
        self.assertEqual(refs["f10_snapshots"]["rows"], 1)
        self.assertEqual(refs["short_snapshots"]["rows"], 1)
        self.assertEqual(refs["plate_snapshots"]["rows"], 1)
        for ref in payload["refs"]:
            self.assertEqual(ref["date"], TODAY)

        patrol = next(t for t in self._tasks() if t["kind"] == "factor_patrol")
        self.assertEqual(patrol["payload"]["market"], "SH")
        self.assertEqual(patrol["payload"]["window"], 120)
        self.assertIn("momentum_20", patrol["payload"]["factor_list"])
        self.assertEqual(sorted(patrol["payload"]["factor_list"]),
                         sorted(factors.REGISTRY), "因子清单取自注册表（唯一事实源）")

        # A-3：周度挖掘轮载荷含「候选因子域」——已注册因子基线（挖据的合法引用集与
        # 去重依据；新候选由本轮提出，攒数中的域见 refs 的数据源行数）
        mining = next(t for t in self._tasks() if t["kind"] == "mining_round")
        self.assertTrue(mining["payload"]["factor_list"], "factor_list 不得为空")
        self.assertEqual(sorted(mining["payload"]["factor_list"]),
                         sorted(factors.REGISTRY))
        self.assertTrue(all(name in factors.REGISTRY
                            for name in mining["payload"]["factor_list"]))

    def test_digest_ref_is_self_checked_freshness(self):
        """A-2：refs 的 daily_digest 行显式承载 as_of/fresh_enough——过时摘要必须可见。

        判据是「digest.as_of 不早于研究日」而非「同日」：19:05 北京时间下美股观测日是
        会话本地日 D-1，而 digest 的 as_of 已是北京日 D（更新）——同日判据会误判。
        """
        prev = "2026-09-15"
        self._watchlist(symbols=["SH.600519", "HK.00700", "US.AAPL"])
        self._calendar(days=(prev, TODAY))                       # SH
        store.upsert_calendar(self.conn, "HK", [
            {"day": TODAY, "trade_date_type": "WHOLE", "trade_second": 14400}])
        store.upsert_calendar(self.conn, "US", [
            {"day": prev, "trade_date_type": "WHOLE", "trade_second": 14400}])
        self._seed_snapshots()

        def brief_of(market):
            return next(t for t in self._tasks()
                        if t["kind"] == "daily_brief" and t["market"] == market)

        def digest_ref_of(market):
            return next(r for r in brief_of(market)["payload"]["refs"]
                        if r["source"] == "daily_digest")

        # ① 无 digest → 照常入队（任务不丢），refs 如实标 as_of=None / fresh_enough=False
        self.assertTrue(self._enqueue("SH")["ok"])
        self.assertIsNone(digest_ref_of("SH")["as_of"])
        self.assertFalse(digest_ref_of("SH")["fresh_enough"])
        self.assertIn("当日摘要尚未生成", self._alert_titles())

        # ② digest 比研究日新（美股观测日 = 北京日 − 1 = prev，digest as_of = TODAY）
        store.kv_set(self.conn, "daily:digest",
                     {"as_of": TODAY, "mode": "sim", "orders": {}})
        self.assertTrue(self._enqueue("US")["ok"])
        self.assertEqual(brief_of("US")["payload"]["as_of"], prev, "美股观测日折算")
        self.assertEqual(digest_ref_of("US")["as_of"], TODAY)
        self.assertTrue(digest_ref_of("US")["fresh_enough"])

        # ③ digest 比研究日旧（HK 研究日 = TODAY，digest as_of = prev）→ 告警，不静默
        store.kv_set(self.conn, "daily:digest",
                     {"as_of": prev, "mode": "sim", "orders": {}})
        before = self._alert_titles().count("当日摘要尚未生成")
        self.assertTrue(self._enqueue("HK")["ok"])
        self.assertFalse(digest_ref_of("HK")["fresh_enough"])
        self.assertEqual(self._alert_titles().count("当日摘要尚未生成"), before + 1)

    def test_global_enqueue_time_covers_all_markets_after_digest(self):
        """A-2 时序：19:05 时三市场观测日都已收盘，且当日 digest 不早于任一观测日。"""
        from trading_core import planner
        prev = "2026-09-15"
        self._calendar(days=(prev, TODAY))
        store.upsert_calendar(self.conn, "HK", [
            {"day": prev, "trade_date_type": "WHOLE", "trade_second": 14400},
            {"day": TODAY, "trade_date_type": "WHOLE", "trade_second": 14400}])
        store.upsert_calendar(self.conn, "US", [
            {"day": prev, "trade_date_type": "WHOLE", "trade_second": 14400}])

        stamp = f"{TODAY} 19:05:00"
        expect = {"SH": TODAY, "HK": TODAY, "US": prev}
        for market, research_day in expect.items():
            date, source = planner.observation_date(self.conn, market, stamp)
            self.assertEqual(source, "session", market)
            self.assertEqual(date, research_day, market)

        # 19:00 写下的 digest（as_of=北京今日）覆盖三者：不早于任一观测日
        store.kv_set(self.conn, "daily:digest",
                     {"as_of": TODAY, "mode": "sim", "orders": {}})
        for research_day in expect.values():
            self.assertGreaterEqual(TODAY, research_day)

        # 对照：旧时序（16:35）下 SH 的 digest 只能取到前一日 —— 修复的正是这一点
        store.kv_set(self.conn, "daily:digest",
                     {"as_of": prev, "mode": "sim", "orders": {}})
        early = planner.observation_date(self.conn, "SH", f"{TODAY} 16:35:00")[0]
        self.assertEqual(early, TODAY)
        self.assertLess(prev, early, "16:35 时当日 digest 尚未生成（旧时序的问题）")

    def test_enqueue_research_is_base_global_job_after_reconcile(self):
        """A-2 修订：入队在基础 GLOBAL 链 19:05（对账 19:00 之后），一条作业覆盖三市场；
        关闭态仍与 JOBS_DEFAULT 逐键相等（研究不受交易开关控制）。
        WP18 起链首还有日历同步（18:50）——同为**基础链**作业，理由见 daemon.JOBS_DEFAULT。"""
        chain = daemon.JOBS_DEFAULT[daemon.GLOBAL_CHAIN]
        self.assertEqual([job["name"] for job in chain],
                         ["sync_calendar", "enqueue_research"])
        job = chain[1]
        self.assertEqual(job["at"], "19:05")
        self.assertEqual(job["cmd"], ["enqueue-research", "--market", "SH,HK,US"])
        # 各市场链不再带 enqueue_research（阶段归属随之移到 GLOBAL 链）
        for market in ("SH", "HK", "US"):
            self.assertNotIn("enqueue_research",
                             [j["name"] for j in daemon.JOBS_DEFAULT[market]], market)
        self.assertEqual(daemon.build_jobs(str(self.home), self.conn),
                         daemon.JOBS_DEFAULT)

    def test_enabled_global_chain_orders_reconcile_before_enqueue(self):
        """开启态：reconcile 追加进 GLOBAL 链并按时刻排序——补跑同一轮里 digest 先落库，
        enqueue 才拿得到当日 digest（A-2 的时序保证）；日历同步（18:50）仍排在链首。"""
        self._watchlist(auto=True)
        chain = daemon.build_jobs(str(self.home), self.conn)[daemon.GLOBAL_CHAIN]
        self.assertEqual([job["name"] for job in chain],
                         ["sync_calendar", "reconcile", "enqueue_research"])
        self.assertEqual(chain[1]["at"], "19:00")
        self.assertEqual(chain[2]["at"], "19:05")
        self.assertLess(chain[1]["at"], chain[2]["at"])

    def test_late_reconcile_at_is_rejected_not_wrapped(self):
        """派生时刻跨日回绕 → 配置非法（fail-closed）：宁可拒绝，也不让入队排到对账之前。"""
        base = dict(autopipeline.AUTO_PIPELINE_DEFAULTS)
        for too_late in ("23:55", "23:58", "23:59"):
            with self.assertRaises(ValueError, msg=too_late):
                autopipeline.apply_overlay(base, {"enabled": True, "reconcile_at": too_late})
        # 仍留得下 5 分钟余量的最晚值通过，且装配出的顺序仍是 reconcile → enqueue
        self._watchlist(auto=True)
        merged = autopipeline.apply_overlay(base, {"enabled": True, "reconcile_at": "23:54"})
        (self.home / "trading-platform.json").write_text(
            json.dumps({"watchlist": WATCHLIST, "auto_pipeline": merged}),
            encoding="utf-8")
        chain = daemon.build_jobs(str(self.home), self.conn)[daemon.GLOBAL_CHAIN]
        self.assertEqual([(job["name"], job["at"]) for job in chain],
                         [("sync_calendar", "18:50"),
                          ("reconcile", "23:54"), ("enqueue_research", "23:59")])

    def test_refs_are_empty_but_task_still_enqueued_without_data(self):
        """当日无任何快照数据 → 任务仍然入队（引用行数为 0），不假装有数据。"""
        self._calendar()
        result = self._enqueue()
        self.assertTrue(result["ok"], result)
        brief = next(t for t in self._tasks() if t["kind"] == "daily_brief")
        self.assertEqual(brief["status"], "pending")
        snapshot_refs = [ref for ref in brief["payload"]["refs"]
                         if ref["source"] != "factor_registry"]
        self.assertTrue(all(ref.get("rows", ref.get("records", 0)) == 0
                            for ref in snapshot_refs), snapshot_refs)

    # ---- ② 周度 mining_round ----

    def test_mining_round_weekly_guard(self):
        self._calendar()
        self._enqueue(today=TODAY)
        weeks = [t for t in self._tasks() if t["kind"] == "mining_round"]
        self.assertEqual([t["as_of"] for t in weeks], [TODAY], "本周首次入队")

        self._enqueue(today=THURSDAY)
        weeks = [t for t in self._tasks() if t["kind"] == "mining_round"]
        self.assertEqual([t["as_of"] for t in weeks], [TODAY], "同周不重复入队")

        self._enqueue(today=NEXT_MONDAY)
        weeks = [t["as_of"] for t in self._tasks() if t["kind"] == "mining_round"]
        self.assertEqual(weeks, [TODAY, NEXT_MONDAY], "下周新增一条")

    # ---- ③ 幂等 ----

    def test_idempotent_same_day_and_progress_kept(self):
        self._calendar()
        first = self._enqueue()
        brief = next(t for t in self._tasks() if t["kind"] == "daily_brief")
        store.claim_task(self.conn, "2026-09-16 16:36:00")
        store.finish_task(self.conn, brief["task_id"], ok=True, result_ref="research/1")

        second = self._enqueue()
        self.assertEqual(len(self._tasks()), 3, "重复入队不新建行")
        by_kind = {t["kind"]: t for t in self._tasks()}
        self.assertEqual(by_kind["daily_brief"]["status"], "done",
                         "已推进的任务不因重复入队被打回 pending")
        self.assertEqual(by_kind["daily_brief"]["result_ref"], "research/1")
        created = {t["kind"]: t["created"] for t in second["tasks"]}
        self.assertFalse(created["daily_brief"])
        self.assertFalse(created["factor_patrol"])
        self.assertNotIn("mining_round", created, "本周挖掘轮已入队，不再重复上报")
        first_ids = {t["kind"]: t["task_id"] for t in first["tasks"]}
        second_ids = {t["kind"]: t["task_id"] for t in second["tasks"]}
        self.assertEqual(second_ids["daily_brief"], first_ids["daily_brief"])
        self.assertEqual(second_ids["factor_patrol"], first_ids["factor_patrol"])

    # ---- ④ 与交易开关解耦 ----

    def test_enqueued_even_when_auto_pipeline_disabled(self):
        self._calendar()
        self._watchlist(auto=False)
        disabled = self._enqueue()
        self.assertTrue(disabled["ok"], disabled)
        self.assertEqual(len(self._tasks()), 3, "关闭交易自动流水线不影响研究任务入队")

    # ---- ⑤ 零 LLM / 零子进程 ----

    def test_zero_llm_and_no_subprocess(self):
        self._calendar()
        original = subprocess.run

        def _explode(*args, **kwargs):
            raise AssertionError("入队作业不得开子进程（零 LLM 承诺）")

        subprocess.run = _explode
        try:
            result = self._enqueue()
        finally:
            subprocess.run = original
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self._tasks()), 3)

    # ---- 边界 ----

    def test_session_open_soft_skips_with_alert(self):
        """会话未收盘（SH 北京时间 10:00）→ 软跳过 + info 告警，零任务。"""
        self._calendar()
        result = self._enqueue(today=None, now="2026-09-16 10:00:00")
        self.assertTrue(result["ok"], result)
        self.assertIn("尚未收盘", result["skipped"])
        self.assertEqual(self._tasks(), [])
        self.assertIn("研究任务入队跳过", self._alert_titles())

    def test_empty_watchlist_soft_skips_with_alert(self):
        self._calendar()
        self._watchlist(symbols=[])
        result = self._enqueue()
        self.assertTrue(result["ok"], result)
        self.assertIn("关注池为空", result["skipped"])
        self.assertEqual(self._tasks(), [])
        self.assertIn("研究任务入队跳过", self._alert_titles())

    def test_unknown_market_fail_closed(self):
        result = research_queue.enqueue(str(self.home), "XX", conn=self.conn, today=TODAY)
        self.assertFalse(result["ok"])
        self.assertIn("未知市场链", result["error"])
        self.assertEqual(self._tasks(), [])

    def test_invalid_clock_fail_closed(self):
        result = self._enqueue(today="2026/09/16")
        self.assertFalse(result["ok"])
        self.assertIn("YYYY-MM-DD", result["error"])

    def test_calendar_missing_falls_back_with_alert(self):
        """日历未同步 → 退化北京日，**显式标源**并告警（不静默改口径）。"""
        result = self._enqueue()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["date_source"], "beijing-fallback")
        self.assertEqual(result["date"], TODAY)
        self.assertIn("日期口径退化", self._alert_titles())

    # ---- CLI 与入链 ----

    def test_cli_end_to_end(self):
        self._calendar()
        self._seed_snapshots()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["enqueue-research", "--market", "SH",
                             "--home", str(self.home),
                             "--db", str(self.home / "t.sqlite"),
                             "--today", TODAY])
        self.assertEqual(code, 0, buf.getvalue())
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["date"], TODAY)
        self.assertEqual(len(payload["tasks"]), 3)
        self.assertEqual(len(self._tasks()), 3)

    def test_pipeline_registers_enqueue_stage_and_alert_titles(self):
        self.assertEqual(pipeline._JOB_LABELS["enqueue_research"], "研究任务入队")
        self.assertEqual(pipeline._ALERT_STATUS["研究任务入队跳过"],
                         ("enqueue_research", "skipped"))
        self.assertEqual(pipeline._ALERT_STATUS["研究任务入队失败"],
                         ("enqueue_research", "failed"))


if __name__ == "__main__":
    unittest.main()
