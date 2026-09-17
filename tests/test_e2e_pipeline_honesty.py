"""流程页诚实性与首启可见性（E2E 缺陷 6 + 7，2026-09-17）。

E2E 原始证据：
  * 关注池为空时 `sync_bars`/`sync_fundamentals`/… **什么都没做**（`_subprocess_runner`
    直接返回 ``{"skipped": "关注池为空"}``、既不告警也不被 `_run_job` 消费），tick 照写
    ran 标记 → 流程页把它们显示成绿色「已完成」且摘要为空——**平台看起来在跑，其实什么
    都没发生**；
  * `~/.dsh/trading-platform.json` 没有 `watchlist` 键 → 全部数据作业静默跳过、队列不入队、
    无计划，而界面上没有任何提示（首启不可见）。
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _dir in (ROOT / "plugins" / "datasource" / "python",
             ROOT / "plugins" / "core" / "python"):
    sys.path.insert(0, str(_dir))
sys.path.insert(0, str(ROOT / "platform"))

from trading_core import alerts as core_alerts  # noqa: E402
from trading_core import daemon, pipeline, store, watchlist  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(store.db_path(str(self.home)))
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)


# ---------------------------------------------------------------------------
# 缺陷 6(a)：作业「跳过」必须留痕，否则流程页只能看到「已完成」
# ---------------------------------------------------------------------------
class SkipIsObservableTests(_Case):
    def _run(self, job, market="SH", runner=None):
        daemon._run_job(self.conn, job, str(self.home), runner=runner, market=market)

    def test_watchlist_empty_skip_emits_warn_alert(self):
        # runner 注入「关注池为空」的跳过标记：默认 runner 会读**真实** `$DSH_HOME` 的
        # 关注池配置（2026-09-17：配置好后本用例会真起子进程联网，既慢又依赖环境）——
        # 被测语义是「跳过必须留痕」，与关注池实际内容无关，故注入假件保持离线确定性。
        self._run({"name": "sync_bars", "at": "16:00",
                   "cmd": ["sync-bars", "--tickers", "@watchlist"]},
                  runner=lambda cmd: {"skipped": "关注池为空"})
        rows = core_alerts.list_recent(self.conn, limit=10)
        self.assertTrue(rows, "跳过必须告警——否则流程页只能看到「已完成」")
        titles = [r["title"] for r in rows]
        self.assertIn(daemon.SKIP_ALERT_TITLE, titles)
        detail = next(r["detail"] for r in rows if r["title"] == daemon.SKIP_ALERT_TITLE)
        self.assertIn("sync_bars", detail)
        self.assertIn("SH", detail)

    def test_self_alerting_jobs_do_not_double_alert(self):
        """情绪/研究/入队作业自己会告警（带更具体的原因）——跳过路径不得重复告警。

        runner 必须注入：默认 runner 会读**真实** `$DSH_HOME`（关注池配置好后会真起子进程
        跑情绪采集——20 标的实测 >7 分钟静默，本用例曾因此挂死）。被测语义与关注池内容无关。
        """
        for name in ("sentiment_snapshot", "research_snapshot", "enqueue_research"):
            with self.subTest(job=name):
                conn = store.connect(store.db_path(str(self.home)))
                self.addCleanup(conn.close)
                daemon._run_job(conn, {"name": name, "at": "16:25",
                                       "cmd": ["sentiment-snapshot", "--market", "SH"]},
                                str(self.home), market="SH",
                                runner=lambda cmd: {"skipped": "关注池为空"})
                rows = [r for r in core_alerts.list_recent(conn, limit=10)
                        if r["title"] == daemon.SKIP_ALERT_TITLE]
                self.assertEqual(rows, [], f"{name} 不应产生重复跳过告警")

    def test_runner_result_is_consumed_not_dropped(self):
        """回归钉：跳过标记此前被 `_run_job` 直接丢弃（既不告警也无返回消费）。"""
        seen = {}

        def runner(cmd):
            seen["cmd"] = list(cmd)
            return {"skipped": "关注池为空"}

        self._run({"name": "factors_snapshot", "at": "16:15",
                   "cmd": ["factors-snapshot", "--tickers", "@watchlist"]}, runner=runner)
        rows = core_alerts.list_recent(self.conn, limit=10)
        self.assertTrue(any(r["title"] == daemon.SKIP_ALERT_TITLE for r in rows))


class PipelineSkippedSemanticsTests(_Case):
    def _state(self, **ran):
        return {"ran": ran}

    def _alerts(self, title, detail="", level="warn"):
        core_alerts.emit(self.conn, home=str(self.home), level=level, title=title,
                         detail=detail)

    def test_ran_but_skipped_stage_is_skipped_not_ok(self):
        """核心诚实性修复：作业跑了但什么都没做 → skipped + 原因，不得报「已完成」。"""
        self._alerts(daemon.SKIP_ALERT_TITLE, detail="job=sync_bars market=SH 关注池为空")
        stage = pipeline._stage("SH", "sync_bars", self._state(**{"SH:sync_bars:2026-09-17":
                                 "2026-09-17 16:00:00"}),
                                core_alerts.list_recent(self.conn, limit=10), "2026-09-17")
        self.assertEqual(stage["status"], "skipped", stage)
        self.assertIn("关注池为空", stage["summary"])

    def test_other_job_skip_alert_does_not_leak(self):
        """detail 里的 job= 决定归属：别的作业跳过不得污染本作业阶段。"""
        self._alerts(daemon.SKIP_ALERT_TITLE, detail="job=sync_bars market=SH 关注池为空")
        stage = pipeline._stage("SH", "factors_snapshot",
                                self._state(**{"SH:factors_snapshot:2026-09-17": "16:15:00"}),
                                core_alerts.list_recent(self.conn, limit=10), "2026-09-17")
        self.assertEqual(stage["status"], "ok", stage)

    def test_stage_without_ran_marker_keeps_old_attribution(self):
        """没跑完的路径不受影响：仍按 _ALERT_STATUS 归因 skipped/failed。"""
        self._alerts("情绪快照跳过", detail="market=SH 关注池为空")
        stage = pipeline._stage("SH", "sentiment_snapshot", self._state(),
                                core_alerts.list_recent(self.conn, limit=10), "2026-09-17")
        self.assertEqual(stage["status"], "skipped", stage)

    def test_all_failed_content_outcome_is_failed(self):
        self._alerts("研究快照全部失败", detail="market=SH")
        stage = pipeline._stage("SH", "research_snapshot",
                                self._state(**{"SH:research_snapshot:2026-09-17": "16:30:00"}),
                                core_alerts.list_recent(self.conn, limit=10), "2026-09-17")
        self.assertEqual(stage["status"], "failed", stage)


# ---------------------------------------------------------------------------
# 缺陷 7：首启可见性（关注池未配置 → 全局提示 + 一次性告警 + 初始化入口）
# ---------------------------------------------------------------------------
class FirstRunVisibilityTests(_Case):
    def test_config_warnings_flags_empty_watchlist(self):
        snapshot = pipeline.pipeline_snapshot(self.conn, str(self.home))
        codes = [w["code"] for w in snapshot.get("config_warnings", [])]
        self.assertIn("watchlist_empty", codes)

    def test_config_warnings_absent_when_watchlist_configured(self):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.600519"]}), encoding="utf-8")
        snapshot = pipeline.pipeline_snapshot(self.conn, str(self.home))
        codes = [w["code"] for w in snapshot.get("config_warnings", [])]
        self.assertNotIn("watchlist_empty", codes)

    def test_warning_carries_actionable_hint(self):
        snapshot = pipeline.pipeline_snapshot(self.conn, str(self.home))
        warning = next(w for w in snapshot["config_warnings"]
                       if w["code"] == "watchlist_empty")
        self.assertIn("watchlist-init", warning["hint"])

    def test_tick_alert_is_emitted_once_per_day(self):
        """每 tick 重复告警会刷屏——当日只发一次（kv 标记）。"""
        jobs = {"SH": []}
        daemon.tick(self.conn, str(self.home), jobs=jobs, now=lambda: "2026-09-17 09:00:00")
        daemon.tick(self.conn, str(self.home), jobs=jobs, now=lambda: "2026-09-17 10:00:00")
        rows = [r for r in core_alerts.list_recent(self.conn, limit=20)
                if r["title"] == daemon.WATCHLIST_EMPTY_ALERT_TITLE]
        self.assertEqual(len(rows), 1, rows)


class WatchlistInitCliTests(_Case):
    def _seed_universe(self):
        for symbol in ("600519", "000001"):
            self.conn.execute(
                "INSERT INTO universe(snapshot_date, symbol, index_name, bias_note, source)"
                " VALUES(?,?,?,?,?)",
                ("2026-09-16", symbol, "SH.000300", "含幸存者偏差", "test"))
        self.conn.commit()

    def test_init_from_index_writes_watchlist_and_keeps_other_keys(self):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"futu_channel": "openapi"}), encoding="utf-8")
        self._seed_universe()
        result = watchlist.init_from_index(str(self.home), "SH.000300",
                                          today="2026-09-17", conn=self.conn)
        self.assertTrue(result["ok"], result)
        config = json.loads((self.home / "trading-platform.json").read_text(encoding="utf-8"))
        self.assertEqual(config["futu_channel"], "openapi", "其他键必须保留")
        self.assertEqual(config["watchlist"], ["SH.600519", "SZ.000001"])

    def test_init_is_idempotent(self):
        self._seed_universe()
        first = watchlist.init_from_index(str(self.home), "SH.000300",
                                          today="2026-09-17", conn=self.conn)
        second = watchlist.init_from_index(str(self.home), "SH.000300",
                                           today="2026-09-17", conn=self.conn)
        self.assertEqual(first["watchlist"], second["watchlist"])
        self.assertTrue(second["unchanged"])

    def test_init_refuses_to_overwrite_non_empty_watchlist(self):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.600519"]}), encoding="utf-8")
        self._seed_universe()
        result = watchlist.init_from_index(str(self.home), "SH.000300",
                                           today="2026-09-17", conn=self.conn)
        self.assertFalse(result["ok"])
        config = json.loads((self.home / "trading-platform.json").read_text(encoding="utf-8"))
        self.assertEqual(config["watchlist"], ["SH.600519"], "非空关注池不得被覆盖")

    def test_init_force_overwrites(self):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.600519"]}), encoding="utf-8")
        self._seed_universe()
        result = watchlist.init_from_index(str(self.home), "SH.000300", force=True,
                                           today="2026-09-17", conn=self.conn)
        self.assertTrue(result["ok"], result)
        config = json.loads((self.home / "trading-platform.json").read_text(encoding="utf-8"))
        self.assertIn("SZ.000001", config["watchlist"])

    def test_init_reports_missing_index_snapshot(self):
        result = watchlist.init_from_index(str(self.home), "HK.800000",
                                           today="2026-09-17", conn=self.conn)
        self.assertFalse(result["ok"])
        self.assertIn("HK.800000", result["error"])




class AlertTitleLockTests(unittest.TestCase):
    """标题是跨模块字面量契约（pipeline 为断环而惰性 import daemon）——漂移即红。"""

    def test_skip_title_matches_daemon_constant(self):
        self.assertIn(daemon.SKIP_ALERT_TITLE, pipeline._CONTENT_OUTCOMES)

    def test_content_outcome_jobs_exist_in_default_chain(self):
        names = {job["name"] for chain in daemon.JOBS_DEFAULT.values() for job in chain}
        for title, (owner, _status, _text) in pipeline._CONTENT_OUTCOMES.items():
            if owner is not None:
                self.assertIn(owner, names, f"{title} 归属的作业 {owner} 不在作业链里")

    def test_every_emit_site_title_is_known(self):
        """登记即契约：本文件断言的两个标题必须真的是 daemon 发出的常量。"""
        self.assertEqual(daemon.SKIP_ALERT_TITLE, "作业跳过：关注池为空")
        self.assertEqual(daemon.WATCHLIST_EMPTY_ALERT_TITLE, "关注池未配置")


if __name__ == "__main__":
    unittest.main()
