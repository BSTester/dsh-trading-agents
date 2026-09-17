"""作业失败可见性（E2E 缺陷 F-a）+ 摘要去重（F-c），2026-09-17 实机取证。

原始证据：
  * `daemon._subprocess_runner` 返回 `subprocess.run(...).returncode`（**整数**），而
    `_run_job` 只消费 `isinstance(result, dict) and result.get("skipped")` → **非零退出码
    被完全忽略**，`tick` 又无条件写 ran 标记 → 流程页把「整批都没同步」显示成
    「行情同步已完成」；
  * 实机复现：`sync-bars --tickers <20 只>` 因瞬时网络超时抛
    `RuntimeError: 取数失败：网络传输失败（TimeoutError）` 并非零退出；
  * F-c：`sync_bars` 的 summary 变成「当日未执行：关注池为空：关注池为空」
    （告警标题与 detail 各说了一遍同样的原因）。
"""

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _dir in (ROOT / "plugins" / "datasource" / "python",
             ROOT / "plugins" / "core" / "python"):
    sys.path.insert(0, str(_dir))

from trading_core import alerts as core_alerts  # noqa: E402
from trading_core import daemon, pipeline, store  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(store.db_path(str(self.home)))
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    # 公共工具：跑一个 cmd 形式作业并返回**本次调用新增**的告警标题
    # （同一测试内的多次调用会累积告警，按 id 增量取才不会互相污染）
    def _run(self, result, name="sync_bars", market="SH"):
        before = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM alerts").fetchone()[0]
        job = {"name": name, "at": "16:00", "cmd": [name, "--x", "1"]}
        daemon._run_job(self.conn, job, str(self.home),
                        runner=lambda cmd: result, market=market)
        return [r["title"] for r in self.conn.execute(
            "SELECT title FROM alerts WHERE id > ? ORDER BY id", (before,)).fetchall()]

    def _mark_ran(self, name="sync_bars", market="SH", date="2026-09-17", at="16:00:05"):
        state = store.kv_get(self.conn, "daemon:state", default={"ran": {}})
        state["ran"][f"{market}:{name}:{date}"] = at
        store.kv_set(self.conn, "daemon:state", state)

    def _stage(self, name="sync_bars", market="SH", date="2026-09-17"):
        state = store.kv_get(self.conn, "daemon:state", default={"ran": {}})
        alerts = [dict(r) for r in self.conn.execute(
            "SELECT level,title,detail,created_at FROM alerts ORDER BY id DESC").fetchall()]
        return pipeline._stage(market, name, state, alerts, date)


class SubprocessRunnerParsingTests(_Case):
    """`_subprocess_runner` 的输出捕获与摘要解析（部分失败告警的基础，F-b）。

    CLI 用 ``json.dumps(..., indent=1)`` 打印**多行** JSON，故解析必须能从整段输出里认出
    那个对象；这条路径此前无覆盖（部分失败能否被发现全靠它）。
    """

    def test_last_json_object_handles_pretty_multiline_and_noise(self):
        pretty = '前置日志\n{\n "ok": [],\n "failed": {"SH.600519": "超时"},\n "failed_count": 1\n}\n'
        self.assertEqual(daemon._last_json_object(pretty),
                         {"ok": [], "failed": {"SH.600519": "超时"}, "failed_count": 1})
        self.assertIsNone(daemon._last_json_object("没有 JSON 的日志\n"))
        self.assertIsNone(daemon._last_json_object(""))
        # 后一个对象优先（作业可能打印多段）
        two = '{"a": 1}\n{"b": 2}\n'
        self.assertEqual(daemon._last_json_object(two), {"b": 2})

    def test_subprocess_runner_returns_structured_result(self):
        import subprocess
        fake = subprocess.CompletedProcess(
            args=["python"], returncode=1,
            stdout='{"ok": [], "failed": {"SH.600519": "boom"}, "failed_count": 1}\n',
            stderr="RuntimeError: 取数失败\n")
        with mock.patch.object(daemon.subprocess, "run", return_value=fake):
            out = daemon._subprocess_runner(["sync-bars", "--tickers", "SH.600519"])
        self.assertEqual(out["exit_code"], 1)
        self.assertEqual(out["summary"]["failed"], {"SH.600519": "boom"})
        self.assertTrue(out["tail"], "保留输出尾部供告警引用")

    def test_subprocess_runner_skip_marker_is_unchanged(self):
        """关注池为空时仍是跳过标记（不跑子进程、不报失败）。"""
        with mock.patch.object(daemon, "resolve_command", return_value=None):
            self.assertEqual(daemon._subprocess_runner(["sync-bars", "--tickers", "@watchlist"]),
                             {"skipped": "关注池为空"})


class NonZeroExitTests(_Case):
    def test_nonzero_exit_emits_warn_alert_with_code_and_tail(self):
        titles = self._run({"exit_code": 1,
                            "tail": ["RuntimeError: 取数失败：网络传输失败（TimeoutError）"]})
        self.assertIn(daemon.JOB_FAILED_ALERT_TITLE, titles,
                      "非零退出必须告警——否则流程页只能看到「已完成」")
        row = next(r for r in core_alerts.list_recent(self.conn, limit=20)
                   if r["title"] == daemon.JOB_FAILED_ALERT_TITLE)
        self.assertEqual(row["level"], "warn")
        self.assertIn("job=sync_bars", row["detail"])
        self.assertIn("market=SH", row["detail"])
        self.assertIn("exit=1", row["detail"])
        self.assertIn("TimeoutError", row["detail"], "detail 应附输出尾部，便于直接定位")

    def test_nonzero_exit_stage_is_failed_not_ok(self):
        self._run({"exit_code": 1, "tail": ["boom"]})
        self._mark_ran()
        stage = self._stage()
        self.assertEqual(stage["status"], "failed", "跑了但失败 → 阶段必须是 failed")
        self.assertIn("当日失败", stage["summary"])
        self.assertNotIn("已完成", stage["summary"])

    def test_zero_exit_emits_no_failure_alert_and_stage_ok(self):
        titles = self._run({"exit_code": 0, "tail": ["ok"]})
        self.assertNotIn(daemon.JOB_FAILED_ALERT_TITLE, titles)
        self._mark_ran()
        self.assertEqual(self._stage()["status"], "ok")

    def test_legacy_int_returncode_is_consumed(self):
        """向后兼容：旧式注入 runner 直接返回整数 returncode，也必须被消费。"""
        self.assertIn(daemon.JOB_FAILED_ALERT_TITLE, self._run(1))
        self.assertNotIn(daemon.JOB_FAILED_ALERT_TITLE, self._run(0))

    def test_runner_returning_none_is_tolerated(self):
        """既有注入风格（`runner=lambda cmd: seen.append(cmd)` 返回 None）不得报失败。"""
        titles = self._run(None)
        self.assertNotIn(daemon.JOB_FAILED_ALERT_TITLE, titles)


class PartialFailureTests(_Case):
    def _partial(self):
        return {"exit_code": 0,
                "summary": {"ok": [{"ticker": "SH.600000"}], "ok_count": 19,
                            "failed": {"SH.600519": "取数失败：超时"}, "failed_count": 1,
                            "total": 20},
                "tail": ["{...}"]}

    def test_partial_failure_warns_but_stage_stays_ok(self):
        titles = self._run(self._partial())
        self.assertIn(daemon.JOB_PARTIAL_ALERT_TITLE, titles,
                      "部分失败必须可见（warn），否则「19 成功 1 失败」无人知晓")
        row = next(r for r in core_alerts.list_recent(self.conn, limit=20)
                   if r["title"] == daemon.JOB_PARTIAL_ALERT_TITLE)
        self.assertIn("SH.600519", row["detail"])
        self._mark_ran()
        stage = self._stage()
        # 作业确实跑了（19 只成功）→ 状态仍 ok，但摘要必须写明部分失败
        self.assertEqual(stage["status"], "ok")
        self.assertIn("部分失败", stage["summary"])
        self.assertIn("SH.600519", stage["summary"])

    def test_no_partial_alert_when_summary_has_no_failures(self):
        titles = self._run({"exit_code": 0, "summary": {"ok": [], "failed": {},
                                                        "ok_count": 3, "failed_count": 0},
                            "tail": []})
        self.assertNotIn(daemon.JOB_PARTIAL_ALERT_TITLE, titles)


class FnJobFailureTests(_Case):
    def test_fn_exception_is_alerted_and_round_continues(self):
        """fn 形式抛异常：不得拖垮整轮 tick，也不得静默。"""
        def boom(_ctx):
            raise RuntimeError("fn 炸了")

        self.conn.execute("INSERT INTO calendar(market, day, trade_date_type)"
                          " VALUES('SH','2026-09-17','MORNING')")
        self.conn.commit()
        jobs = {"SH": [{"name": "enqueue_research", "at": "00:00", "fn": boom},
                       {"name": "sync_bars", "at": "00:00",
                        "cmd": ["sync-bars", "--tickers", "SH.600000"]}]}
        # runner 注入：整轮**离线**（否则 cmd 作业会真起 CLI 子进程 → 联网 + 受安装副本版本影响）
        with self.assertRaises(RuntimeError) as caught:
            daemon.tick(self.conn, str(self.home), jobs=jobs,
                        now=lambda: "2026-09-17 23:59:00",
                        runner=lambda cmd: {"exit_code": 0, "summary": None, "tail": []})
        self.assertIn("enqueue_research", str(caught.exception),
                      "作业失败必须在整轮结束后上抛（healthz/--once 可见性）")
        rows = core_alerts.list_recent(self.conn, limit=20)
        titles = [r["title"] for r in rows]
        self.assertIn(daemon.JOB_ERROR_ALERT_TITLE, titles, "fn 异常必须告警")
        detail = next(r["detail"] for r in rows if r["title"] == daemon.JOB_ERROR_ALERT_TITLE)
        self.assertIn("enqueue_research", detail)
        self.assertIn("fn 炸了", detail, "异常原因必须在 detail 里")
        state = store.kv_get(self.conn, "daemon:state", default={"ran": {}})
        ran = [k for k in state["ran"] if "2026-09-17" in k]
        self.assertTrue(any("enqueue_research" in k for k in ran),
                        "失败作业也要落 ran 标记（否则下一轮重复执行）")
        self.assertTrue(any("sync_bars" in k for k in ran),
                        "同一轮后续作业必须继续执行（不能被一个失败作业拖垮）")

    def test_failure_beats_skip_in_status_rank(self):
        """同一作业既有跳过又有失败告警 → failed（失败优先）。"""
        daemon._run_job(self.conn,
                        {"name": "sync_bars", "at": "16:00",
                         "cmd": ["sync-bars", "--tickers", "@watchlist"]},
                        str(self.home), runner=lambda cmd: {"skipped": "关注池为空"},
                        market="SH")
        self._run({"exit_code": 1, "tail": ["boom"]})
        self._mark_ran()
        self.assertEqual(self._stage()["status"], "failed")


class RoundCompletionTests(_Case):
    """整轮语义：失败作业不拖垮同轮其它作业、标记照落，且失败**在轮末上抛**（F-a）。"""

    def _cal(self):
        self.conn.execute("INSERT INTO calendar(market, day, trade_date_type)"
                          " VALUES('SH','2026-09-17','MORNING')")
        self.conn.commit()

    def test_nonzero_exit_marks_ran_then_raises_after_round(self):
        self._cal()
        jobs = {"SH": [{"name": "sync_bars", "at": "00:00", "cmd": ["sync-bars"]},
                       {"name": "quality", "at": "00:00", "cmd": ["quality"]}]}
        seen = []

        def runner(cmd):
            seen.append(cmd[0])
            return {"exit_code": 1, "tail": ["boom"]} if cmd[0] == "sync-bars" \
                else {"exit_code": 0, "summary": None, "tail": []}

        with self.assertRaises(RuntimeError):
            daemon.tick(self.conn, str(self.home), jobs=jobs,
                        now=lambda: "2026-09-17 23:59:00", runner=runner)
        self.assertEqual(seen, ["sync-bars", "quality"], "一个作业失败不得跳过同轮其余作业")
        state = store.kv_get(self.conn, "daemon:state", default={"ran": {}})
        ran = [k for k in state["ran"] if "2026-09-17" in k]
        self.assertTrue(any("sync_bars" in k for k in ran), "失败作业也要落标记（避免重复执行）")
        self.assertTrue(any("quality" in k for k in ran), "成功作业的标记不得因同轮失败而丢失")
        titles = [r["title"] for r in core_alerts.list_recent(self.conn, limit=20)]
        self.assertIn(daemon.JOB_FAILED_ALERT_TITLE, titles)

    def test_all_success_does_not_raise(self):
        self._cal()
        jobs = {"SH": [{"name": "sync_bars", "at": "00:00", "cmd": ["sync-bars"]}]}
        daemon.tick(self.conn, str(self.home), jobs=jobs,
                    now=lambda: "2026-09-17 23:59:00",
                    runner=lambda cmd: {"exit_code": 0, "summary": None, "tail": []})
        titles = [r["title"] for r in core_alerts.list_recent(self.conn, limit=20)]
        self.assertNotIn(daemon.JOB_FAILED_ALERT_TITLE, titles)


class JobTimeoutTests(_Case):
    """900s 子进程超时（`subprocess.run(timeout=900)` → `TimeoutExpired`）：

    实机新发现（控制者口径，2026-09-17）：超时此前会从 `_subprocess_runner` 抛出并**中断该
    市场链余下所有作业**，且当日余下作业不留痕。现在与其它作业异常同路径处理——
    告警 + 阶段 failed + **继续链上后续作业** + 轮末上抛（不整链中断）。
    """

    def _cal(self):
        self.conn.execute("INSERT INTO calendar(market, day, trade_date_type)"
                          " VALUES('SH','2026-09-17','MORNING')")
        self.conn.commit()

    def test_timeout_alerts_continues_chain_and_surfaces(self):
        import subprocess
        self._cal()
        jobs = {"SH": [{"name": "sync_bars", "at": "00:00", "cmd": ["sync-bars"]},
                       {"name": "quality", "at": "00:00", "cmd": ["quality"]}]}
        seen = []

        def runner(cmd):
            seen.append(cmd[0])
            if cmd[0] == "sync-bars":
                raise subprocess.TimeoutExpired(cmd="sync-bars", timeout=900)
            return {"exit_code": 0, "summary": None, "tail": []}

        with self.assertRaises(RuntimeError) as caught:
            daemon.tick(self.conn, str(self.home), jobs=jobs,
                        now=lambda: "2026-09-17 23:59:00", runner=runner)
        self.assertEqual(seen, ["sync-bars", "quality"], "超时不得中断链上后续作业")
        rows = core_alerts.list_recent(self.conn, limit=20)
        detail = next(r["detail"] for r in rows if r["title"] == daemon.JOB_ERROR_ALERT_TITLE)
        self.assertIn("TimeoutExpired", detail)
        self.assertIn("sync_bars", detail)
        self.assertIn("sync_bars", str(caught.exception), "超时必须在轮末上抛可见")
        state = store.kv_get(self.conn, "daemon:state", default={"ran": {}})
        ran = [k for k in state["ran"] if "2026-09-17" in k]
        self.assertTrue(any("sync_bars" in k for k in ran), "超时作业也要落标记（当日不重复执行）")
        self.assertTrue(any("quality" in k for k in ran), "后续作业标记不得丢失")


class SummaryDedupTests(_Case):
    def test_skip_summary_does_not_repeat_the_reason(self):
        """F-c：标题与 detail 说同一件事时，摘要不得写两遍。"""
        daemon._run_job(self.conn,
                        {"name": "sync_bars", "at": "16:00",
                         "cmd": ["sync-bars", "--tickers", "@watchlist"]},
                        str(self.home), runner=lambda cmd: {"skipped": "关注池为空"},
                        market="SH")
        self._mark_ran()
        summary = self._stage()["summary"]
        self.assertEqual(summary, "当日未执行：关注池为空", f"实际：{summary!r}")
        self.assertEqual(summary.count("关注池为空"), 1)

    def test_extra_detail_is_still_appended(self):
        """detail 带来 text 未表达的信息时仍要拼接（去重不能吃掉事实）。"""
        daemon._run_job(self.conn,
                        {"name": "sync_bars", "at": "16:00",
                         "cmd": ["sync-bars", "--tickers", "@watchlist"]},
                        str(self.home),
                        runner=lambda cmd: {"skipped": "关注池为空（或全部停牌）"},
                        market="SH")
        self._mark_ran()
        summary = self._stage()["summary"]
        self.assertEqual(summary, "当日未执行：关注池为空（或全部停牌）", f"实际：{summary!r}")


if __name__ == "__main__":
    unittest.main()
