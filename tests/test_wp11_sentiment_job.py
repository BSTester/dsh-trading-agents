"""WP11 任务 2：sentiment-snapshot 三源采集作业（规格 §6.1）。

全部离线：子进程 runner 与资讯通道全部注入假件，关注池/日历直接种子。
覆盖计划任务 2 的 ①–⑤ 与边界：

  * ① fin_sentiment 源落库（payload 原文透传）；
  * ② fin_news 源落库（payload 保留真实上游 sources_status——渠道可达性自证）；
  * ③ last30days：引擎在场→落库；缺席→absent 标记且退出码 0（不阻塞链）；
  * ④ 单源失败隔离（该源 absent + warn，其余源照常）；全部失败→warn + ok（不阻塞链）；
  * ⑤ 关注池为空→跳过 + 告警；
  * 边界：未知市场 fail-closed / 脚本未安装（通道缺席）/ 美股链按会话本地日记录 /
    CLI 端到端退出码 / JOBS_DEFAULT 入链时点与关闭态回归。

日期口径（实现期决策，规格未言明处）：观测日 = 该市场本次作业负责的**会话本地日**
（``planner.data_date_for`` 单一实现）；日历缺失时退化为北京日并显式标
``date_source="beijing-fallback"`` 且告警——不静默改口径。
"""
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

import unittest.mock as mock  # noqa: E402

from trading_core import alerts, cli, daemon, sentiment, store  # noqa: E402
from trading_datasource import repo_paths  # noqa: E402

TODAY = "2026-09-16"
WATCHLIST = ["SH.600519", "SZ.300750", "HK.00700"]


class _FakeRunner:
    """假子进程执行体：按脚本名分派返回；fail 集合内的脚本抛错（模拟通道失败）。"""

    def __init__(self, responses=None, fail=()):
        self.responses = responses or {}
        self.fail = set(fail)
        self.calls = []

    def __call__(self, cmd, timeout=None):
        self.calls.append(list(cmd))
        name = Path(cmd[1]).name
        key = {"fin_sentiment.py": "fin_sentiment",
               "fin_news.py": "fin_news",
               "last30days.py": "last30days"}.get(name, name)
        if key in self.fail:
            raise RuntimeError(f"通道失败：{key}")
        return json.dumps(self.responses.get(key, {"ticker": cmd[-1] if cmd else ""}),
                          ensure_ascii=False)

    def scripts(self):
        return [Path(c[1]).name for c in self.calls]


class SentimentJobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        self._install_engines()

    # ---- 种子工具 ----

    def _install_engines(self, fin_data=True, last30days=True):
        """铺/撤「已安装」的引擎脚本路径（内容无关——runner 已注入，不会被真执行）。

        显式 False 撤下对应脚本：缺席用例必须真的看不到文件（setUp 默认全装）。
        """
        base = self.home / "trading-python" / "fin-data"
        last_dir = (self.home / "last30days-skill" / "skills" / "last30days"
                    / "scripts")
        if fin_data:
            base.mkdir(parents=True, exist_ok=True)
            for name in ("fin_sentiment.py", "fin_news.py"):
                (base / name).write_text("# stub\n", encoding="utf-8")
        else:
            shutil.rmtree(base, ignore_errors=True)
        if last30days:
            last_dir.mkdir(parents=True, exist_ok=True)
            (last_dir / "last30days.py").write_text("# stub\n", encoding="utf-8")
        else:
            (last_dir / "last30days.py").unlink(missing_ok=True)

    def _watchlist(self, symbols=None):
        cfg = {"watchlist": WATCHLIST if symbols is None else symbols,
               "auto_pipeline": {"enabled": False}}
        (self.home / "trading-platform.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def _calendar(self, market="SH", days=(TODAY,)):
        store.upsert_calendar(self.conn, market, [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in days])

    def _alert_titles(self, level=None):
        sql = "SELECT level,title FROM alerts" + (" WHERE level=?" if level else "")
        rows = self.conn.execute(sql, (level,) if level else ()).fetchall()
        return [r["title"] for r in rows]

    # ---- ①–③ 三源落库 ----

    def test_fin_sentiment_source_saved(self):
        """① fin_sentiment 落库：payload 原文透传、source/date 正确。"""
        self._watchlist(["SH.600519"])
        self._calendar()
        payload = {"ticker": "SH.600519", "x": [{"n": 3}],
                   "a_share_comment": {"score": 8}, "sources_status": {"akshare": "ok"}}
        runner = _FakeRunner({"fin_sentiment": payload,
                              "fin_news": {"items": []}})
        result = sentiment.run(self.home, "SH", conn=self.conn, runner=runner,
                               today=TODAY)
        self.assertTrue(result["ok"], result)
        rows = {r["source"]: r for r in store.read_sentiments(self.conn, "SH.600519")}
        self.assertIn("fin_sentiment", rows)
        self.assertEqual(rows["fin_sentiment"]["date"], TODAY)
        self.assertEqual(rows["fin_sentiment"]["payload"]["a_share_comment"]["score"], 8)

    def test_fin_news_source_saved_preserves_upstream(self):
        """② 资讯源落库：payload 保留真实上游（渠道自证），source=fin_news。"""
        self._watchlist(["SH.600519"])
        self._calendar()
        news = {"ticker": "SH.600519",
                "items": [{"source": "futu", "title": "回购", "url": "u", "time": "t"}],
                "sources_status": {"futu": "ok"}}
        runner = _FakeRunner({"fin_news": news, "fin_sentiment": {"x": []}})
        sentiment.run(self.home, "SH", conn=self.conn, runner=runner, today=TODAY)
        rows = {r["source"]: r for r in store.read_sentiments(self.conn, "SH.600519")}
        self.assertIn("fin_news", rows)
        self.assertEqual(rows["fin_news"]["payload"]["sources_status"]["futu"], "ok")

    def test_last30days_saved_when_engine_present(self):
        """③ 引擎在场：以 --emit=json 调用并把结构化输出落库。"""
        self._watchlist(["SH.600519"])
        self._calendar()
        runner = _FakeRunner({"last30days": {"topic": "600519", "posts": 4},
                              "fin_sentiment": {"x": []}, "fin_news": {"items": []}})
        sentiment.run(self.home, "SH", conn=self.conn, runner=runner, today=TODAY)
        rows = {r["source"]: r for r in store.read_sentiments(self.conn, "SH.600519")}
        self.assertIn("last30days", rows)
        self.assertEqual(rows["last30days"]["payload"]["posts"], 4)
        last30_calls = [c for c in runner.calls if c[1].endswith("last30days.py")]
        self.assertTrue(last30_calls)
        self.assertIn("--emit=json", last30_calls[0])

    def test_last30days_absent_when_engine_missing(self):
        """③ 引擎缺席：标 absent、退出语义 ok、不落该源行、不阻塞其余源。"""
        self._install_engines(last30days=False)
        self._watchlist(["SH.600519"])
        self._calendar()
        runner = _FakeRunner({"fin_sentiment": {"x": []}, "fin_news": {"items": []}})
        result = sentiment.run(self.home, "SH", conn=self.conn, runner=runner,
                               today=TODAY)
        self.assertTrue(result["ok"], result)
        self.assertIn("last30days", result["absent"])
        rows = store.read_sentiments(self.conn, "SH.600519")
        self.assertNotIn("last30days", [r["source"] for r in rows])

    # ---- ④ 失败隔离 ----

    def test_source_failure_isolated_to_that_source(self):
        """④ 单源全败：该源入 absent + warn 告警，其余源照常落库。"""
        self._watchlist(["SH.600519"])
        self._calendar()
        runner = _FakeRunner({"fin_news": {"items": []}}, fail={"fin_sentiment"})
        result = sentiment.run(self.home, "SH", conn=self.conn, runner=runner,
                               today=TODAY)
        self.assertTrue(result["ok"], result)
        self.assertIn("fin_sentiment", result["absent"])
        self.assertIn("fin_news", [r["source"] for r in
                                   store.read_sentiments(self.conn, "SH.600519")])
        # 告警标题是 pipeline 状态映射的精确键，源名只能进 detail（见 sentiment.emit 注释）
        titles = self._alert_titles("warn")
        self.assertIn("情绪源不可用", titles, titles)
        detail = self.conn.execute(
            "SELECT detail FROM alerts WHERE title='情绪源不可用'").fetchone()["detail"]
        self.assertIn("fin_sentiment", detail)
        self.assertIn("market=SH", detail)
        from trading_core import pipeline
        self.assertEqual(pipeline._ALERT_STATUS.get("情绪源不可用"),
                         ("sentiment_snapshot", "failed"),
                         "告警标题与 pipeline 阶段映射漂移")

    def test_all_sources_fail_is_ok_with_warn(self):
        """④ 全部失败：永不抛、退出语义 ok（不阻塞链）+ warn 告警、零落库。"""
        self._watchlist(["SH.600519"])
        self._calendar()
        runner = _FakeRunner(fail={"fin_sentiment", "fin_news", "last30days"})
        result = sentiment.run(self.home, "SH", conn=self.conn, runner=runner,
                               today=TODAY)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["saved"], 0)
        self.assertEqual(store.read_sentiments(self.conn, "SH.600519"), [])
        self.assertTrue(self._alert_titles("warn"), self._alert_titles())

    # ---- ⑤ 关注池与边界 ----

    def test_empty_watchlist_skips_with_alert(self):
        """⑤ 关注池为空：跳过 + 告警，零子进程调用。"""
        self._watchlist([])
        runner = _FakeRunner()
        result = sentiment.run(self.home, "SH", conn=self.conn, runner=runner,
                               today=TODAY)
        self.assertTrue(result["ok"], result)
        self.assertIn("关注池", result.get("skipped", ""))
        self.assertEqual(runner.calls, [])
        self.assertTrue(self._alert_titles())

    def test_unknown_market_fail_closed(self):
        """未知市场链：fail-closed（非零退出），不静默当成 SH。"""
        self._watchlist(["SH.600519"])
        result = sentiment.run(self.home, "XX", conn=self.conn,
                               runner=_FakeRunner(), today=TODAY)
        self.assertFalse(result["ok"])
        self.assertIn("XX", result["error"])

    def test_illegal_stamp_fail_closed(self):
        """注入口径非法（--today 是人工输入）：fail-closed 返回错误信封，永不抛。"""
        self._watchlist(["SH.600519"])
        result = sentiment.run(self.home, "SH", conn=self.conn, runner=_FakeRunner(),
                               today="2026-09-32")
        self.assertFalse(result["ok"], result)
        self.assertIn("2026-09-32", result["error"])

    def test_scripts_not_installed_marks_sources_absent(self):
        """通道未安装（fin-data/last30days 脚本都不在场）→ 三源全 absent，仍 ok。

        「不在场」必须**同时**排除仓库脚本：`sentiment.scripts_for` 自 2026-09-17 起
        优先 `plugins/fin-data/python/<script>`（开发机上安装副本会滞后，跑副本等于跑旧
        代码），仓库不存在时才回落副本。故这里显式模拟「无仓库 + 空 home」这一真正的
        未安装形态——直接跑在仓库里时 fin-data 脚本总是存在的（那条路径由
        `test_scripts_for_prefers_repo` 覆盖）。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        other = Path(tmp.name)
        (other / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.600519"]}), encoding="utf-8")
        conn = store.connect(str(other / "t.sqlite"))
        self.addCleanup(conn.close)
        with mock.patch.object(repo_paths, "repo_root", lambda: None):
            result = sentiment.run(other, "SH", conn=conn, runner=_FakeRunner(),
                                   today=TODAY)
        self.assertTrue(result["ok"], result)
        for source in ("fin_sentiment", "fin_news", "last30days"):
            self.assertIn(source, result["absent"])

    def test_us_symbol_recorded_on_session_local_date(self):
        """美股链：北京时间 09-17 05:45 的作业记录在会话本地日 09-16（与 bars 同空间）。"""
        self._watchlist(["US.AAPL"])
        self._calendar("US", days=("2026-09-16",))
        runner = _FakeRunner({"fin_sentiment": {"x": []}, "fin_news": {"items": []}})
        result = sentiment.run(self.home, "US", conn=self.conn, runner=runner,
                               today="2026-09-17 05:45:00")
        self.assertTrue(result["ok"], result)
        rows = store.read_sentiments(self.conn, "US.AAPL")
        self.assertTrue(rows, result)
        self.assertEqual(rows[0]["date"], "2026-09-16")
        self.assertEqual(result["date_source"], "session")

    def test_calendar_missing_falls_back_to_beijing_with_flag(self):
        """日历缺失：退化北京日但显式标记 + 告警（不静默改口径）。"""
        self._watchlist(["SH.600519"])
        runner = _FakeRunner({"fin_sentiment": {"x": []}, "fin_news": {"items": []}})
        result = sentiment.run(self.home, "SH", conn=self.conn, runner=runner,
                               today="2026-09-16 16:25:00")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["date"], "2026-09-16")
        self.assertEqual(result["date_source"], "beijing-fallback")
        self.assertTrue(self._alert_titles("info"), self._alert_titles())

    # ---- CLI 端到端 + 入链 ----

    def test_cli_reports_absent_and_exits_zero(self):
        """CLI：脚本未安装的离线环境下如实报三源缺席、退出码 0（不阻塞调度链）。"""
        self._install_engines(fin_data=False, last30days=False)
        self._watchlist(["SH.600519"])
        self._calendar()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["sentiment-snapshot", "--market", "SH",
                             "--home", str(self.home),
                             "--db", str(self.home / "t.sqlite"),
                             "--today", TODAY])
        self.assertEqual(code, 0, buf.getvalue())
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"], payload)
        for source in ("fin_sentiment", "fin_news", "last30days"):
            self.assertIn(source, payload["absent"])

    def test_jobs_default_contains_sentiment_snapshot(self):
        """入链（规格 §4.1 基础链）：每市场链尾、factors_snapshot + 10 分钟、cmd 正确；
        且关闭态 build_jobs 与 JOBS_DEFAULT 逐键相等（禁止开启功能门槛）。"""
        expect = {"SH": ("16:15", "16:25"), "HK": ("16:35", "16:45"),
                  "US": ("05:35", "05:45")}
        for market, (factors_at, snap_at) in expect.items():
            chain = daemon.JOBS_DEFAULT[market]
            names = [j["name"] for j in chain]
            self.assertIn("sentiment_snapshot", names, names)
            job = chain[names.index("sentiment_snapshot")]
            self.assertEqual(job["at"], snap_at, market)
            self.assertEqual(job["cmd"], ["sentiment-snapshot", "--market", market])
            self.assertLess(chain[names.index("factors_snapshot")]["at"], job["at"])
        self.assertEqual(daemon.build_jobs(str(self.home), self.conn),
                         daemon.JOBS_DEFAULT)


if __name__ == "__main__":
    unittest.main()
