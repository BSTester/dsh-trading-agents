"""WP18 A1：日历自动同步 `calendar-sync`——自节流、幂等、逐市场容错与退出码。

**为什么要这条命令**（2026-09-18 实测事实）：`calendar` 表是「交易日白名单」，
`store.is_trading_day` 按白名单查；日历**用尽**（日期超出 `max(day)`）时它返回 False
而不是报错 → 市场链被静默跳过，页面上表现为「市场天天休市」，0 条告警。而此前
**没有任何作业会同步日历**：`daemon.JOBS_DEFAULT` 的 GLOBAL 链只有 `enqueue_research`，
同步只能手工跑 `python -m trading_core calendar --market SH --start … --end …`。

**自节流**：作业每天都会到期，不做节流就是每天打 3 次上游；`ensure_coverage` 先读
`max(day)`，覆盖足够（≥ today + horizon_days）就**不发起任何网络调用**——绝大多数日子是
零网络 no-op。默认 horizon 180 天，比 daemon 的告警阈值（60 天）宽，中间留出「作业连续
失败 ~120 天才会开始告警」的缓冲。
"""
import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from trading_core import calendar, cli, store  # noqa: E402

#: 固定「今天」：用例全部注入，不依赖真实墙钟
TODAY = dt.date(2026, 9, 18)
#: 同步窗口（相对 today）：回看 30 天补漏、前推 400 天覆盖
EXPECT_START = "2026-08-19"
EXPECT_END = "2027-10-23"


def _days(*dates):
    return [{"day": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in dates]


def _payload(*dates):
    return {"trading_days": [{"time": d, "trade_date_type": "WHOLE", "trade_second": 14400}
                             for d in dates]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)
        self.calls = []

    def _fetcher(self, *dates):
        """注入式取数（离线）：记录调用参数并回给定交易日。"""
        def fetch(market, start, end):
            self.calls.append((market, start, end))
            return _payload(*dates)
        return fetch


class EnsureCoverageTest(_Base):
    """单市场逻辑（`calendar.ensure_coverage`）：取数可注入，分支可离线断言。"""

    def test_skips_without_network_when_coverage_sufficient(self):
        store.upsert_calendar(self.conn, "SH", _days("2027-12-31"))
        out = calendar.ensure_coverage(self.conn, "SH", TODAY,
                                       fetcher=self._fetcher("2027-12-31"))
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(out["days"], 0)
        self.assertEqual(out["last_day"], "2027-12-31")
        self.assertEqual(self.calls, [], "覆盖充足必须零网络调用（自节流）")

    def test_throttle_boundary_is_inclusive(self):
        """阈值口径：``last >= today + horizon`` 才跳过（边界当天仍算充足）。"""
        store.upsert_calendar(self.conn, "SH", _days("2027-03-17"))   # today+180
        out = calendar.ensure_coverage(self.conn, "SH", TODAY, horizon_days=180,
                                       fetcher=self._fetcher("2027-03-17"))
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(self.calls, [])

        self.conn.execute("DELETE FROM calendar")                     # 换一个更短的边界重测
        self.conn.commit()
        store.upsert_calendar(self.conn, "SH", _days("2027-03-16"))   # today+179
        out = calendar.ensure_coverage(self.conn, "SH", TODAY, horizon_days=180,
                                       fetcher=self._fetcher("2027-03-16"))
        self.assertEqual(out["status"], "synced")
        self.assertEqual(len(self.calls), 1)

    def test_syncs_when_coverage_short_and_reports_new_boundary(self):
        store.upsert_calendar(self.conn, "SH", _days("2026-10-01"))
        out = calendar.ensure_coverage(self.conn, "SH", TODAY,
                                       fetcher=self._fetcher("2026-09-11", "2027-12-31"))
        self.assertEqual(out["status"], "synced")
        self.assertEqual(out["days"], 2)
        self.assertEqual(out["last_day"], "2027-12-31")
        self.assertEqual(self.calls, [("SH", EXPECT_START, EXPECT_END)])
        self.assertEqual(out["start"], EXPECT_START)
        self.assertEqual(out["end"], EXPECT_END)
        self.assertTrue(store.is_trading_day(self.conn, "SH", "2026-09-11"))

    def test_empty_calendar_is_synced_not_skipped(self):
        """零行市场（`_require_calendar` 会抛的那个态）同样走同步，不是跳过。"""
        out = calendar.ensure_coverage(self.conn, "SH", TODAY, fetcher=self._fetcher())
        self.assertEqual(out["status"], "synced")
        self.assertEqual(len(self.calls), 1)
        # 上游信封异常导致 0 行时如实回报（`sync_calendar` 既有口径：不抛、不假装成功）：
        # 边界仍是 None，下一个 tick 的覆盖告警会继续喊，不在这里硬判失败
        self.assertEqual(out["days"], 0)
        self.assertIsNone(out["last_day"])

    def test_second_call_is_noop_after_coverage_restored(self):
        """幂等 + 节流：第一次同步把边界推到 today+400，第二次直接跳过。"""
        fetch = self._fetcher("2027-12-31")
        first = calendar.ensure_coverage(self.conn, "SH", TODAY, fetcher=fetch)
        second = calendar.ensure_coverage(self.conn, "SH", TODAY, fetcher=fetch)
        self.assertEqual(first["status"], "synced")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(len(self.calls), 1)

    def test_market_is_uppercased_in_call_and_result(self):
        out = calendar.ensure_coverage(self.conn, "sh", TODAY, fetcher=self._fetcher())
        self.assertEqual(out["market"], "SH")
        self.assertEqual(self.calls[0][0], "SH")

    def test_horizon_option_moves_the_throttle_point(self):
        """horizon 是**同步**阈值（默认 180），与 daemon 的 60 天**告警**阈值是两回事。"""
        store.upsert_calendar(self.conn, "SH", _days("2026-12-27"))   # today+100
        fetch = self._fetcher("2026-12-27")
        self.assertEqual(calendar.ensure_coverage(self.conn, "SH", TODAY, horizon_days=180,
                                                  fetcher=fetch)["status"], "synced")
        self.assertEqual(calendar.ensure_coverage(self.conn, "SH", TODAY, horizon_days=60,
                                                  fetcher=fetch)["status"], "skipped")
        self.assertEqual(len(self.calls), 1)

    def test_fetch_failure_propagates_to_caller(self):
        """单市场函数不吞故障：隔离由 CLI（逐市场 try/except）负责，职责单一。"""
        def boom(market, start, end):
            raise RuntimeError("通道不可用")

        with self.assertRaises(RuntimeError):
            calendar.ensure_coverage(self.conn, "SH", TODAY, fetcher=boom)


class CalendarSyncCliTest(_Base):
    """CLI 汇总与退出码：与既有 `sync-bars` 同一口径（0=全成功或部分成功、1=零成功）。"""

    def setUp(self):
        super().setUp()
        self.db = str(self.home / "t.sqlite")

    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(argv + ["--db", self.db])
        return code, json.loads(buf.getvalue())

    def _patch_default_fetcher(self, fail=()):
        """替换**默认取数**（`ensure_coverage` 不注入 fetcher 时走的那条）：CLI 用例离线。"""
        def fetch(market, start, end, **kwargs):
            self.calls.append((market, start, end))
            if market in fail:
                raise RuntimeError(f"{market} 通道不可用")
            return _payload("2027-12-31")
        return mock.patch.object(calendar, "_default_fetcher", fetch)

    def test_all_skipped_exits_zero_with_zero_network(self):
        for market in ("SH", "HK", "US"):
            store.upsert_calendar(self.conn, market, _days("2027-12-31"))
        with mock.patch.object(calendar, "_default_fetcher",
                               side_effect=AssertionError("覆盖充足不得取数")):
            code, out = self._run(["calendar-sync", "--market", "SH,HK,US"])
        self.assertEqual(code, 0)
        self.assertEqual(out["failed"], {})
        self.assertEqual(out["ok_count"], 3)
        self.assertEqual(out["failed_count"], 0)
        self.assertEqual(out["total"], 3)
        self.assertEqual([m["status"] for m in out["markets"].values()],
                         ["skipped", "skipped", "skipped"])
        self.assertEqual(out["today"], dt.date.today().isoformat())
        self.assertEqual(out["horizon_days"], calendar.DEFAULT_HORIZON_DAYS)

    def test_partial_failure_exits_zero_and_isolates_market(self):
        """一个市场通道失败不影响其余市场（逐市场独立），退出码按「部分成功」= 0。"""
        with self._patch_default_fetcher(fail=("US",)):
            code, out = self._run(["calendar-sync", "--market", "SH,HK,US"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(out["markets"]), ["HK", "SH"])
        self.assertEqual(out["ok_count"], 2)
        self.assertEqual(out["failed_count"], 1)
        self.assertIn("通道不可用", out["failed"]["US"])
        self.assertEqual([m["status"] for m in out["markets"].values()],
                         ["synced", "synced"])
        self.assertEqual([c[0] for c in self.calls], ["SH", "HK", "US"],
                         "失败市场不得中断后续市场")

    def test_all_failed_exits_one(self):
        with self._patch_default_fetcher(fail=("SH", "HK", "US")):
            code, out = self._run(["calendar-sync", "--market", "SH,HK,US"])
        self.assertEqual(code, 1)
        self.assertEqual(out["ok_count"], 0)
        self.assertEqual(out["failed_count"], 3)
        self.assertEqual(out["markets"], {})

    def test_horizon_days_flag_is_passed_through(self):
        store.upsert_calendar(self.conn, "SH", _days("2026-12-27"))   # today+100
        with self._patch_default_fetcher():
            code, out = self._run(["calendar-sync", "--market", "SH", "--horizon-days", "60"])
        self.assertEqual(code, 0)
        self.assertEqual(out["horizon_days"], 60)
        self.assertEqual(out["markets"]["SH"]["status"], "skipped")
        self.assertEqual(self.calls, [])

    def test_empty_market_list_fails_closed(self):
        code, out = self._run(["calendar-sync", "--market", " , "])
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("market", out["error"])


if __name__ == "__main__":
    unittest.main()
