"""WP18 A2/A3/A4：日历同步作业入链 + 覆盖不足告警 + 「日历已用尽」不再静默。

三条事实与三条改动（2026-09-18 实测）：

  * **A2**：`daemon.JOBS_DEFAULT` 的 GLOBAL 链原先只有 `enqueue_research`，
    **没有任何作业同步日历**；日历用尽只能靠人工发现。新增 `sync_calendar`（18:50，
    早于 reconcile 19:00 与 enqueue_research 19:05）——日历必须在当日对账/计划之前就位，
    这样「日历刚用尽」也能在当晚自愈。同一轮 tick 内 **GLOBAL 链必须先于市场链**处理，
    否则「本轮刚同步好的日历」对本轮市场链不可见。
  * **A3**：`max(day) < today + 60` 时 `alerts.emit(level="warn", title="日历覆盖不足")`，
    每市场每日至多一条（kv 去重，不每轮 tick 刷屏）。
  * **A4**：`today > max(day)`（覆盖用尽）时 `is_trading_day` 返回 False 而不报错，
    市场链被静默跳过、运维只能看到「市场天天休市」。新增 `title="日历已用尽"` 的 warn；
    **真实休市（日期在覆盖范围内但不在白名单）保持静默**——那个语义是刻意的
    （`planner.plan_auto` 的软跳过同样不告警：真实休市不是故障）。
"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from trading_core import alerts, autopipeline, daemon, pipeline, store  # noqa: E402

TODAY = "2026-09-18"


def _day(day):
    return {"day": day, "trade_date_type": "WHOLE", "trade_second": 14400}


def _shift(base, days):
    return (dt.date.fromisoformat(base) + dt.timedelta(days=days)).isoformat()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)
        # 关注池非空：否则 `_warn_empty_watchlist` 会额外发一条首启告警，干扰断言
        (self.home / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.600519"]}), encoding="utf-8")

    def _titles(self):
        return [row["title"] for row in alerts.list_recent(self.conn, limit=50)]

    def _titles_of(self, title):
        return [t for t in self._titles() if t == title]


# ---------------------------------------------------------------------------
# A2：作业入链与链序
# ---------------------------------------------------------------------------
class CalendarJobChainTest(_Base):
    def test_sync_calendar_is_base_global_job_before_reconcile(self):
        chain = daemon.JOBS_DEFAULT[daemon.GLOBAL_CHAIN]
        self.assertEqual([job["name"] for job in chain],
                         ["sync_calendar", "enqueue_research"])
        job = chain[0]
        self.assertEqual(job["at"], "18:50")
        self.assertEqual(job["cmd"], ["calendar-sync", "--market", "SH,HK,US"])
        # 时刻依据（写进注释的理由必须能被断言）：早于 reconcile 19:00 与入队 19:05
        self.assertLess(job["at"], "19:00")
        # 基础链作业：与交易开关解耦（GLOBAL 链不查市场日历）
        self.assertEqual(daemon.build_jobs(str(self.home), self.conn),
                         daemon.JOBS_DEFAULT)

    def test_enabled_global_chain_keeps_calendar_first(self):
        (self.home / "trading-platform.json").write_text(json.dumps({
            "watchlist": ["SH.600519"],
            "auto_pipeline": {"enabled": True, "reconcile_at": "19:00"}}),
            encoding="utf-8")
        chain = daemon.build_jobs(str(self.home), self.conn)[daemon.GLOBAL_CHAIN]
        self.assertEqual([job["name"] for job in chain],
                         ["sync_calendar", "reconcile", "enqueue_research"])
        times = [job["at"] for job in chain]
        self.assertEqual(times, sorted(times))

    def test_global_chain_is_processed_before_market_chains(self):
        """显式定序：不靠 jobs dict 的插入顺序（本用例刻意把市场链放在前面）。"""
        seen = []
        store.upsert_calendar(self.conn, "SH", [_day(TODAY)])
        jobs = {"SH": [{"name": "sync_bars", "at": "00:00",
                        "fn": lambda ctx: seen.append("SH") or {}}],
                daemon.GLOBAL_CHAIN: [{"name": "sync_calendar", "at": "00:00",
                                       "fn": lambda ctx: seen.append("GLOBAL") or {}}]}
        daemon.tick(self.conn, home=str(self.home), jobs=jobs,
                    now=lambda: f"{TODAY} 19:00:00")
        self.assertEqual(seen, ["GLOBAL", "SH"])

    def test_calendar_synced_this_tick_unblocks_market_chain_in_same_tick(self):
        """自愈语义：本轮 GLOBAL 链刚补齐日历 → 同一轮市场链不再按旧日历被跳过。

        初始日历只到昨天（覆盖未用尽，所以是「真实休市」态：`is_trading_day` 返回 False
        且不报错）——正好复现「日历用尽后市场天天休市」的现场。
        """
        store.upsert_calendar(self.conn, "SH", [_day(_shift(TODAY, -1))])
        seen = []

        def sync_calendar_job(ctx):
            seen.append("sync_calendar")
            store.upsert_calendar(ctx["conn"], "SH", [_day(TODAY)])
            return {}

        jobs = {daemon.GLOBAL_CHAIN: [{"name": "sync_calendar", "at": "00:00",
                                       "fn": sync_calendar_job}],
                "SH": [{"name": "sync_bars", "at": "00:00",
                        "fn": lambda ctx: seen.append("sync_bars") or {}}]}
        daemon.tick(self.conn, home=str(self.home), jobs=jobs,
                    now=lambda: f"{TODAY} 18:55:00")
        self.assertEqual(seen, ["sync_calendar", "sync_bars"])


# ---------------------------------------------------------------------------
# A3：覆盖不足告警
# ---------------------------------------------------------------------------
class CoverageWarningTest(_Base):
    def test_short_coverage_warns_and_still_runs_chain(self):
        store.upsert_calendar(self.conn, "SH", [_day(TODAY), _day(_shift(TODAY, 30))])
        ran = []
        jobs = {"SH": [{"name": "sync_bars", "at": "00:00",
                        "fn": lambda ctx: ran.append("x") or {}}]}
        daemon.tick(self.conn, home=str(self.home), jobs=jobs,
                    now=lambda: f"{TODAY} 16:30:00")
        self.assertEqual(ran, ["x"], "覆盖不足只是预警：链照常跑")
        rows = [r for r in alerts.list_recent(self.conn, limit=50)
                if r["title"] == daemon.CALENDAR_SHORT_ALERT_TITLE]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "warn")
        self.assertIn("SH", rows[0]["detail"])
        self.assertIn(_shift(TODAY, 30), rows[0]["detail"])
        self.assertIn("calendar-sync", rows[0]["detail"])

    def test_short_coverage_warns_at_most_once_per_market_per_day(self):
        store.upsert_calendar(self.conn, "SH", [_day(TODAY), _day(_shift(TODAY, 30))])
        jobs = {"SH": []}
        for hour in ("16:30", "17:30", "18:30"):
            daemon.tick(self.conn, home=str(self.home), jobs=jobs,
                        now=lambda hour=hour: f"{TODAY} {hour}:00")
        self.assertEqual(len(self._titles_of(daemon.CALENDAR_SHORT_ALERT_TITLE)), 1)

    def test_two_markets_warn_independently(self):
        store.upsert_calendar(self.conn, "SH", [_day(TODAY), _day(_shift(TODAY, 30))])
        store.upsert_calendar(self.conn, "HK", [_day(TODAY), _day(_shift(TODAY, 10))])
        jobs = {"SH": [], "HK": []}
        daemon.tick(self.conn, home=str(self.home), jobs=jobs,
                    now=lambda: f"{TODAY} 16:30:00")
        rows = [r for r in alerts.list_recent(self.conn, limit=50)
                if r["title"] == daemon.CALENDAR_SHORT_ALERT_TITLE]
        self.assertEqual(len(rows), 2)
        self.assertEqual({("SH" if "SH" in r["detail"] else "HK") for r in rows},
                         {"SH", "HK"})

    def test_ample_coverage_is_silent(self):
        store.upsert_calendar(self.conn, "SH", [_day(TODAY), _day(_shift(TODAY, 400))])
        daemon.tick(self.conn, home=str(self.home), jobs={"SH": []},
                    now=lambda: f"{TODAY} 16:30:00")
        self.assertEqual(self._titles_of(daemon.CALENDAR_SHORT_ALERT_TITLE), [])
        self.assertEqual(self._titles_of(daemon.CALENDAR_EXHAUSTED_ALERT_TITLE), [])

    def test_missing_calendar_keeps_only_the_not_synced_alert(self):
        """零行市场走既有的「日历未同步」——不叠加覆盖告警（两条同义告警是噪声）。"""
        daemon.tick(self.conn, home=str(self.home), jobs={"SH": []},
                    now=lambda: f"{TODAY} 16:30:00")
        self.assertEqual(len(self._titles_of("日历未同步")), 1)
        self.assertEqual(self._titles_of(daemon.CALENDAR_SHORT_ALERT_TITLE), [])
        self.assertEqual(self._titles_of(daemon.CALENDAR_EXHAUSTED_ALERT_TITLE), [])


# ---------------------------------------------------------------------------
# A4：日历用尽不再静默 / 真实休市保持静默
# ---------------------------------------------------------------------------
class CalendarExhaustedTest(_Base):
    def _jobs(self, ran):
        return {"SH": [{"name": "sync_bars", "at": "00:00",
                        "fn": lambda ctx: ran.append("x") or {}}]}

    def test_exhausted_calendar_warns_and_skips_chain(self):
        store.upsert_calendar(self.conn, "SH", [_day(_shift(TODAY, -1))])
        ran = []
        daemon.tick(self.conn, home=str(self.home), jobs=self._jobs(ran),
                    now=lambda: f"{TODAY} 16:30:00")
        self.assertEqual(ran, [], "覆盖用尽：市场链被跳过（既有语义，不静默）")
        rows = [r for r in alerts.list_recent(self.conn, limit=50)
                if r["title"] == daemon.CALENDAR_EXHAUSTED_ALERT_TITLE]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["level"], "warn")
        self.assertIn("SH", rows[0]["detail"])
        self.assertIn(_shift(TODAY, -1), rows[0]["detail"])
        self.assertIn("calendar-sync", rows[0]["detail"])

    def test_exhausted_warns_at_most_once_per_day(self):
        store.upsert_calendar(self.conn, "SH", [_day(_shift(TODAY, -1))])
        jobs = {"SH": []}
        for hour in ("09:00", "16:30", "23:30"):
            daemon.tick(self.conn, home=str(self.home), jobs=jobs,
                        now=lambda hour=hour: f"{TODAY} {hour}:00")
        self.assertEqual(len(self._titles_of(daemon.CALENDAR_EXHAUSTED_ALERT_TITLE)), 1)

    def test_real_holiday_stays_silent(self):
        """覆盖范围内但不在白名单 = 真实休市：**不告警**（刻意的静默语义）。"""
        store.upsert_calendar(self.conn, "SH", [_day(_shift(TODAY, -1)),
                                                _day(_shift(TODAY, 90))])
        ran = []
        daemon.tick(self.conn, home=str(self.home), jobs=self._jobs(ran),
                    now=lambda: f"{TODAY} 16:30:00")
        self.assertEqual(ran, [])
        self.assertEqual(self._titles_of(daemon.CALENDAR_EXHAUSTED_ALERT_TITLE), [])
        self.assertEqual(self._titles_of(daemon.CALENDAR_SHORT_ALERT_TITLE), [])

    def test_exhausted_is_not_double_reported_as_short(self):
        """两条告警**互斥**：用尽时只发更精确的「日历已用尽」，不再叠加「覆盖不足」
        （同一天同一市场两条同义 warn 是纯噪声；`max(day)` 为负天数也没法读出意义）。"""
        store.upsert_calendar(self.conn, "SH", [_day(_shift(TODAY, -5))])
        daemon.tick(self.conn, home=str(self.home), jobs={"SH": []},
                    now=lambda: f"{TODAY} 16:30:00")
        self.assertEqual(len(self._titles_of(daemon.CALENDAR_EXHAUSTED_ALERT_TITLE)), 1)
        self.assertEqual(self._titles_of(daemon.CALENDAR_SHORT_ALERT_TITLE), [])

    def test_warn_calendar_uses_last_day_helper(self):
        """`tick` 不写裸 SQL：上界一律经 `store.calendar_last_day`。"""
        self.assertIsNone(store.calendar_last_day(self.conn, "SH"))
        store.upsert_calendar(self.conn, "SH", [_day("2026-09-01"), _day("2026-09-30")])
        self.assertEqual(store.calendar_last_day(self.conn, "sh"), "2026-09-30")


# ---------------------------------------------------------------------------
# 字面量锁（pipeline 归因表按标题精确匹配，标题必须稳定）
# ---------------------------------------------------------------------------
class CalendarAlertTitleLockTest(unittest.TestCase):
    def test_titles_are_stable_literals(self):
        self.assertEqual(daemon.CALENDAR_SHORT_ALERT_TITLE, "日历覆盖不足")
        self.assertEqual(daemon.CALENDAR_EXHAUSTED_ALERT_TITLE, "日历已用尽")

    def test_exhausted_is_registered_as_chain_skip(self):
        """用尽 = 该市场链整条被跳过 → 与「日历未同步」同一张链层表。"""
        self.assertEqual(pipeline._CHAIN_ALERT_STATUS["日历已用尽"], "skipped")

    def test_shortage_is_registered_as_inert_chain_notice(self):
        """覆盖不足**不改变阶段状态**（链照常跑）——登记只为标题锁与「刻意惰性」的书面化。"""
        self.assertIn("日历覆盖不足", pipeline._CHAIN_NOTICE_ALERT_TITLES)

    def test_notice_title_does_not_change_stage_status(self):
        alerts_ = [{"level": "warn", "title": "日历覆盖不足",
                    "detail": "market=SH 最后交易日 2026-10-18", "created_at": "x"}]
        stage = pipeline._stage("SH", "sync_bars", {"ran": {}}, alerts_, TODAY)
        self.assertEqual(stage["status"], "pending")

    def test_sync_calendar_stage_is_registered_in_pipeline(self):
        self.assertEqual(pipeline._JOB_LABELS["sync_calendar"], "日历同步")
        self.assertEqual(pipeline._CHAIN_ALERT_STATUS.get("日历覆盖不足"), None)


if __name__ == "__main__":
    unittest.main()
