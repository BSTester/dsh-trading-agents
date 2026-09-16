"""WP12 任务 5：F10/做空/板块三张 PIT 表 + research-snapshot 每日作业（规格 §7.3）。

守护四件事：

* **防前视（PIT，核心）**：财报/公告类 section 按 ``announced_at`` 过滤；无披露时点的
  快照类按**观测时点**（``fetched_at`` 当日）过滤——``as_of`` 之后才发生的观测读不到；
  自然期与披露时点都取不到时，``period_end`` 回落观测日并在 payload 显式标注
  「观测时点非数据时点」（规格 §7.3，绝不静默硬套）。
* **幂等**：主键 ``(symbol,section,period_end,fetched_at)``——同一观测时点重跑覆盖不增行，
  不同观测时点各自留痕（观测不可被后来的观测抹掉）。
* **失败隔离**：单端点失败记入 failed 不阻塞其余；``no_data`` 空落库不算失败；
  无凭据软跳过且**零调用**；作业永不抛（与 sentiment/planner 同一作业契约）。
* **只读**：作业调用集 ⊆ 数据面只读方法白名单（本文件用记录替身断言）。

入链（规格 §4.1 基础链）：``research_snapshot`` 在各市场链尾、``sentiment_snapshot``
+ 5 分钟，**不受 auto_pipeline 开关控制**（研究数据积累与交易开关无关）。
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import cli, daemon, pipeline, research_sync, store  # noqa: E402

TODAY = "2026-09-16"
F10_ALLOWED = ("f10", "short_daily_volume", "short_interest", "plate_list")


class _FakeF10:
    def __init__(self, plane):
        self.plane = plane

    def f10(self, symbol, section, **params):
        self.plane.calls.append(("f10", symbol, section))
        self.plane._maybe_fail(("f10", symbol, section))
        return self.plane.responses.get(("f10", symbol, section),
                                        {"section": section, "symbol": symbol})


class _FakeShort:
    def __init__(self, plane):
        self.plane = plane

    def _call(self, method, symbol, **params):
        self.plane.calls.append((method, symbol, None))
        self.plane._maybe_fail((method, symbol, None))
        return self.plane.responses.get((method, symbol, None), {"items": []})

    def short_daily_volume(self, symbol, count=None):
        return self._call("short_daily_volume", symbol, count=count)

    def short_interest(self, symbol, count=None):
        return self._call("short_interest", symbol, count=count)


class _FakePlate:
    def __init__(self, plane):
        self.plane = plane

    def plate_list(self, market, plate_class):
        self.plane.calls.append(("plate_list", market, plate_class))
        self.plane._maybe_fail(("plate_list", market, plate_class))
        return self.plane.responses.get(("plate_list", market, plate_class),
                                        {"plate_list": [{"plate_id": "LIST1"}]})


class _FakeDataPlane:
    """数据面假替身：记录每次调用；``responses``/``fail`` 按 (方法, 标的, section) 分派。"""

    def __init__(self, responses=None, fail=()):
        self.calls = []
        self.responses = responses or {}
        self.fail = set(fail)
        self.f10 = _FakeF10(self)
        self.short = _FakeShort(self)
        self.plate = _FakePlate(self)

    def _maybe_fail(self, key):
        if key in self.fail:
            raise RuntimeError(f"通道失败：{key}")

    def methods(self):
        return {call[0] for call in self.calls}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)

    def _watchlist(self, symbols):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"watchlist": symbols}, ensure_ascii=False), encoding="utf-8")

    def _calendar(self, market="SH", days=(TODAY,)):
        store.upsert_calendar(self.conn, market, [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in days])

    def _titles(self, level=None):
        sql = "SELECT level,title FROM alerts" + (" WHERE level=?" if level else "")
        rows = self.conn.execute(sql, (level,) if level else ()).fetchall()
        return [r["title"] for r in rows]

    def _rows(self, table, **where):
        sql = f"SELECT * FROM {table}"
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        return self.conn.execute(sql, tuple(where.values())).fetchall()


class F10PitTests(_Base):
    """① 防前视 + ② 幂等 + ③ 多期共存（store 层）。"""

    def test_financial_section_gated_by_announced_at(self):
        store.insert_f10(self.conn, "SH.600519", "dividends", {"pub_date": "2026-08-29"},
                         period_end="2026-06-30", announced_at="2026-08-29",
                         fetched_at="2026-08-30 10:00:00")
        self.assertEqual(store.read_f10(self.conn, "SH.600519", "dividends", "2026-08-28"),
                         [])
        rows = store.read_f10(self.conn, "SH.600519", "dividends", "2026-08-29")
        self.assertEqual(len(rows), 1)  # 边界含当日

    def test_snapshot_section_gated_by_observation_day(self):
        store.insert_f10(self.conn, "HK.00700", "institutional", {"period_text": "2026Q2"},
                         period_end="2026Q2", announced_at=None,
                         fetched_at="2026-09-16 16:50:00")
        self.assertEqual(store.read_f10(self.conn, "HK.00700", "institutional", "2026-09-15"),
                         [])
        self.assertEqual(
            len(store.read_f10(self.conn, "HK.00700", "institutional", "2026-09-16")), 1)

    def test_multiple_periods_coexist(self):
        for period in ("2026Q1", "2026Q2"):
            store.insert_f10(self.conn, "HK.00700", "institutional", {"p": period},
                             period_end=period, fetched_at=f"2026-09-0{period[-1]} 10:00:00")
        rows = store.read_f10(self.conn, "HK.00700", "institutional", "2026-12-31")
        self.assertEqual([r["period_end"] for r in rows], ["2026Q2", "2026Q1"])

    def test_same_observation_is_idempotent_other_observation_appends(self):
        for payload in ({"v": 1}, {"v": 2}):
            store.insert_f10(self.conn, "HK.00700", "analyst_consensus", payload,
                             period_end=TODAY, fetched_at="2026-09-16 16:50:00")
        rows = self._rows("f10_snapshots", symbol="HK.00700")
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["payload"])["v"], 2)
        store.insert_f10(self.conn, "HK.00700", "analyst_consensus", {"v": 3},
                         period_end=TODAY, fetched_at="2026-09-17 16:50:00")
        self.assertEqual(len(self._rows("f10_snapshots", symbol="HK.00700")), 2)

    def test_invalid_payload_rejected(self):
        with self.assertRaises(ValueError):
            store.insert_f10(self.conn, "HK.00700", "analyst_consensus", "not-json",
                             period_end=TODAY, fetched_at="2026-09-16 16:50:00")


class ShortPlateTests(_Base):
    def test_short_roundtrip_and_same_day_idempotent(self):
        store.insert_short(self.conn, "HK.00700", TODAY, {"items": [{"n": 1}]},
                           fetched_at="2026-09-16 16:50:00")
        store.insert_short(self.conn, "HK.00700", TODAY, {"items": [{"n": 2}]},
                           fetched_at="2026-09-16 17:50:00")
        rows = self._rows("short_snapshots", symbol="HK.00700")
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["payload"])["items"][0]["n"], 2)
        self.assertEqual(store.read_shorts(self.conn, "HK.00700")[0]["date"], TODAY)
        self.assertEqual(store.read_shorts(self.conn, "HK.00700", before="2026-09-15"), [])

    def test_plate_roundtrip_and_filters(self):
        store.insert_plate(self.conn, TODAY, "HK", "INDUSTRY",
                           {"plate_list": [{"plate_id": "A"}]},
                           fetched_at="2026-09-16 16:50:00")
        store.insert_plate(self.conn, TODAY, "SH", "INDUSTRY",
                           {"plate_list": [{"plate_id": "B"}]},
                           fetched_at="2026-09-16 16:30:00")
        self.assertEqual(len(store.read_plates(self.conn, date=TODAY)), 2)
        self.assertEqual(len(store.read_plates(self.conn, date=TODAY, market="HK")), 1)
        self.assertEqual(store.read_plates(self.conn, date="2026-09-15"), [])

    def test_research_stats_reports_coverage(self):
        store.insert_f10(self.conn, "SH.600519", "dividends", {}, period_end="2026Q2",
                         announced_at="2026-08-29", fetched_at="2026-08-30 10:00:00")
        store.insert_f10(self.conn, "SH.600519", "institutional", {}, period_end=TODAY,
                         announced_at=None, fetched_at="2026-09-16 16:30:00")
        store.insert_short(self.conn, "HK.00700", TODAY, {}, fetched_at="2026-09-16 16:50:00")
        store.insert_plate(self.conn, TODAY, "HK", "INDUSTRY", {},
                           fetched_at="2026-09-16 16:50:00")
        stats = store.research_stats(self.conn)
        self.assertEqual(stats["f10"]["rows"], 2)
        self.assertEqual(stats["f10"]["announced"], 1)
        self.assertEqual(stats["f10"]["announced_ratio"], 0.5)
        self.assertEqual(stats["f10"]["latest"], "2026-09-16")
        self.assertEqual(stats["short"]["rows"], 1)
        self.assertEqual(stats["plate"]["rows"], 1)


class PitFieldsTests(unittest.TestCase):
    """规格 §7.3：PIT 钥匙按 section 语义取值，不统一硬套。"""

    def test_publication_field_wins_and_period_extracted(self):
        period, announced = research_sync.pit_fields(
            {"pub_date": "2026-08-29", "ex_date": "2026-09-10"},
            observation_date=TODAY)
        self.assertEqual(announced, "2026-08-29")
        self.assertEqual(period, "2026-09-10")

    def test_period_only_leaves_announced_none(self):
        period, announced = research_sync.pit_fields({"period_text": "2026Q2"},
                                                     observation_date=TODAY)
        self.assertEqual(period, "2026Q2")
        self.assertIsNone(announced)

    def test_no_natural_fields_falls_back_to_observation_date(self):
        period, announced = research_sync.pit_fields({"rating": "BUY"},
                                                     observation_date=TODAY)
        self.assertEqual(period, TODAY)
        self.assertIsNone(announced)

    def test_list_payload_uses_first_item(self):
        period, announced = research_sync.pit_fields(
            [{"holding_date": "2026-06-30"}, {"holding_date": "2026-03-31"}],
            observation_date=TODAY)
        self.assertEqual(period, "2026-06-30")
        self.assertIsNone(announced)

    def test_nested_list_container_is_searched(self):
        """字段在列表容器内（如 records/items）也要认得出——不猜字段名，只降一层。"""
        period, announced = research_sync.pit_fields(
            {"records": [{"pub_trading_day": "2026-08-29", "period_text": "2026Q2"}]},
            observation_date=TODAY)
        self.assertEqual(announced, "2026-08-29")
        self.assertEqual(period, "2026Q2")


class ResearchSyncJobTests(_Base):
    def _run(self, market, symbols, client, **kwargs):
        self._watchlist(symbols)
        self._calendar(market)
        return research_sync.run(self.home, market, conn=self.conn, client=client,
                                 today=TODAY, sleep_seconds=0, **kwargs)

    def test_all_success_saves_three_tables(self):
        plane = _FakeDataPlane()
        result = self._run("HK", ["HK.00700"], plane)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["sections"]["f10"], len(research_sync.F10_SECTIONS))
        self.assertEqual(result["sections"]["short"], 1)
        self.assertEqual(result["sections"]["plate"], 1)
        self.assertEqual(len(self._rows("f10_snapshots")), len(research_sync.F10_SECTIONS))
        self.assertEqual(len(self._rows("short_snapshots")), 1)
        self.assertEqual(len(self._rows("plate_snapshots")), 1)

    def test_short_endpoints_merge_into_one_row_per_day(self):
        """表主键是 (symbol,date)：两个做空端点**合并**为当日一行（不互相覆盖）。"""
        plane = _FakeDataPlane(responses={
            ("short_daily_volume", "HK.00700", None): {"items": [{"v": 1}]},
            ("short_interest", "HK.00700", None): {"aggregated_short": 42},
        })
        self._run("HK", ["HK.00700"], plane)
        rows = self._rows("short_snapshots", symbol="HK.00700")
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual(payload["short_daily_volume"]["items"][0]["v"], 1)
        self.assertEqual(payload["short_interest"]["aggregated_short"], 42)

    def test_short_partial_failure_keeps_successful_endpoint(self):
        """单端点失败：成功的那个照常落库，失败的进 failed（不整行丢弃）。"""
        plane = _FakeDataPlane(fail={("short_interest", "HK.00700", None)})
        result = self._run("HK", ["HK.00700"], plane)
        self.assertEqual([f["section"] for f in result["failed"]], ["short_interest"])
        payload = json.loads(self._rows("short_snapshots", symbol="HK.00700")[0]["payload"])
        self.assertIn("short_daily_volume", payload)
        self.assertNotIn("short_interest", payload)

    def test_observation_gating_note_attached_and_announced_extracted(self):
        """无披露字段的 section 落观测口径并标注；有 pub_date 的按公告日。"""
        plane = _FakeDataPlane(responses={
            ("f10", "HK.00700", "analyst_consensus"): {"rating": "BUY"},
            ("f10", "HK.00700", "holding_changes"): {"pub_date": "2026-08-29",
                                                     "period_text": "2026Q2"},
        })
        self._run("HK", ["HK.00700"], plane)
        consensus = self._rows("f10_snapshots", symbol="HK.00700",
                               section="analyst_consensus")[0]
        self.assertIsNone(consensus["announced_at"])
        self.assertEqual(consensus["period_end"], TODAY)
        self.assertEqual(json.loads(consensus["payload"])["_observed_note"],
                         research_sync.OBSERVED_NOTE)
        changes = self._rows("f10_snapshots", symbol="HK.00700",
                             section="holding_changes")[0]
        self.assertEqual(changes["announced_at"], "2026-08-29")
        self.assertEqual(changes["period_end"], "2026Q2")

    def test_sh_market_short_is_unsupported_and_not_called(self):
        plane = _FakeDataPlane()
        result = self._run("SH", ["SH.600519"], plane)
        self.assertIn("short", result["unsupported"])
        self.assertNotIn("short_daily_volume", plane.methods())
        self.assertEqual(len(self._rows("short_snapshots")), 0)

    def test_single_section_failure_is_isolated(self):
        plane = _FakeDataPlane(fail={("f10", "HK.00700", "institutional")})
        result = self._run("HK", ["HK.00700"], plane)
        self.assertTrue(result["ok"], result)
        self.assertEqual([f["section"] for f in result["failed"]], ["institutional"])
        self.assertEqual(len(self._rows("f10_snapshots")),
                         len(research_sync.F10_SECTIONS) - 1)

    def test_no_data_is_stored_but_not_failed(self):
        plane = _FakeDataPlane(responses={
            ("f10", "HK.00700", "morningstar"): {"no_data": True},
        })
        result = self._run("HK", ["HK.00700"], plane)
        self.assertEqual(result["failed"], [])
        self.assertTrue(result["ok"], result)

    def test_all_endpoints_failed_emits_warn(self):
        plane = _FakeDataPlane(fail={("f10", "HK.00700", section)
                                     for section in research_sync.F10_SECTIONS}
                               | {("short_daily_volume", "HK.00700", None),
                                  ("short_interest", "HK.00700", None),
                                  ("plate_list", "HK", "INDUSTRY")})
        result = self._run("HK", ["HK.00700"], plane)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["saved"], 0)
        self.assertIn("研究快照全部失败", self._titles("warn"))

    def test_openapi_unconfigured_skips_with_zero_calls(self):
        self._watchlist(["HK.00700"])
        self._calendar("HK")
        result = research_sync.run(self.home, "HK", conn=self.conn, today=TODAY,
                                   credential_path=self.home / "missing.json")
        self.assertTrue(result["ok"], result)
        self.assertIn("skipped", result)
        self.assertIn("openapi", result["absent"])
        self.assertIn("研究数据面未配置", self._titles("info"))
        self.assertEqual(self._rows("f10_snapshots"), [])

    def test_session_open_is_soft_skip(self):
        plane = _FakeDataPlane()
        self._watchlist(["SH.600519"])
        self._calendar("SH")
        result = research_sync.run(self.home, "SH", conn=self.conn, client=plane,
                                   today=f"{TODAY} 09:00:00", sleep_seconds=0)
        self.assertTrue(result["ok"], result)
        self.assertIn("skipped", result)
        self.assertEqual(plane.calls, [])

    def test_empty_watchlist_is_soft_skip(self):
        plane = _FakeDataPlane()
        result = self._run("HK", [], plane)
        self.assertTrue(result["ok"], result)
        self.assertIn("skipped", result)
        self.assertEqual(plane.calls, [])

    def test_unknown_market_fails_closed(self):
        result = research_sync.run(self.home, "XX", conn=self.conn, today=TODAY)
        self.assertFalse(result["ok"])
        self.assertIn("未知市场链", result["error"])

    def test_calls_are_read_only(self):
        plane = _FakeDataPlane()
        self._run("HK", ["HK.00700"], plane)
        self.assertTrue(plane.methods() <= set(F10_ALLOWED), plane.methods())


class ResearchSnapshotCliTests(_Base):
    def test_cli_unconfigured_prints_skipped_and_exits_zero(self):
        self._watchlist(["SH.600519"])
        self._calendar("SH")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["research-snapshot", "--market", "SH",
                             "--home", str(self.home),
                             "--db", str(self.home / "t.sqlite"),
                             "--today", TODAY])
        self.assertEqual(code, 0, buf.getvalue())
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"], payload)

    def test_cli_unknown_market_fails_closed(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["research-snapshot", "--market", "XX",
                             "--home", str(self.home),
                             "--db", str(self.home / "t.sqlite")])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(buf.getvalue())["ok"])

    def test_cli_stats_reports_coverage(self):
        store.insert_f10(self.conn, "SH.600519", "dividends", {}, period_end="2026Q2",
                         announced_at="2026-08-29", fetched_at="2026-08-30 10:00:00")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["research-snapshot", "--stats",
                             "--home", str(self.home),
                             "--db", str(self.home / "t.sqlite")])
        self.assertEqual(code, 0, buf.getvalue())
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["f10"]["announced"], 1)


class ChainIntegrationTests(_Base):
    def test_research_snapshot_is_in_base_chain_after_sentiment(self):
        expect = {"SH": "16:30", "HK": "16:50", "US": "05:50"}
        for market, at in expect.items():
            chain = daemon.JOBS_DEFAULT[market]
            names = [job["name"] for job in chain]
            self.assertIn("research_snapshot", names, names)
            job = chain[names.index("research_snapshot")]
            self.assertEqual(job["at"], at, market)
            self.assertEqual(job["cmd"], ["research-snapshot", "--market", market])
            self.assertLess(chain[names.index("sentiment_snapshot")]["at"], job["at"])

    def test_disabled_auto_pipeline_leaves_base_chain_unchanged(self):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"watchlist": [], "auto_pipeline": {"enabled": False}}),
            encoding="utf-8")
        self.assertEqual(daemon.build_jobs(str(self.home), self.conn),
                         daemon.JOBS_DEFAULT)

    def test_pipeline_registers_research_stage_and_alert_titles(self):
        self.assertEqual(pipeline._JOB_LABELS["research_snapshot"], "研究数据快照")
        for title in ("研究快照跳过", "研究数据面未配置", "研究快照全部失败"):
            self.assertIn(title, pipeline._ALERT_STATUS, title)


if __name__ == "__main__":
    unittest.main()
