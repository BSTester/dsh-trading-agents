"""store 层单测：六张表、PIT 纪律强制、幂等 upsert、kv 游标。全部离线。"""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import store  # noqa: E402

BARS = [{"t": "2026-09-10", "o": 10.0, "h": 11.0, "l": 9.5, "c": 10.5, "v": 1000.0},
        {"t": "2026-09-11", "o": 10.5, "h": 12.0, "l": 10.2, "c": 11.8, "v": 1200.0}]


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_wal_enabled(self):
        mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_upsert_bars_idempotent(self):
        self.assertEqual(store.upsert_bars(self.conn, "600519", "1d", BARS, "futu/x"), 2)
        self.assertEqual(store.upsert_bars(self.conn, "600519", "1d", BARS, "futu/x"), 2)
        rows = store.read_bars(self.conn, "600519", "1d", as_of="2026-09-11")
        self.assertEqual([r["t"] for r in rows], ["2026-09-10", "2026-09-11"])
        self.assertEqual(rows[-1]["c"], 11.8)

    def test_read_bars_requires_as_of(self):
        store.upsert_bars(self.conn, "600519", "1d", BARS, "futu/x")
        with self.assertRaises(ValueError):
            store.read_bars(self.conn, "600519", "1d", as_of=None)

    def test_read_bars_respects_as_of(self):
        store.upsert_bars(self.conn, "600519", "1d", BARS, "futu/x")
        rows = store.read_bars(self.conn, "600519", "1d", as_of="2026-09-10")
        self.assertEqual([r["t"] for r in rows], ["2026-09-10"])

    def test_fundamentals_pit_by_announced_at(self):
        store.upsert_fundamentals(self.conn, "600519",
                                  [{"field": "revenue", "period_end": "2026-06-30", "value": 1.0}],
                                  "futu/statements")
        # 未合并公告日：PIT 读取必须为空（宁缺毋假）
        self.assertEqual(store.read_fundamentals(self.conn, "600519", as_of="2026-09-14"), [])
        n = store.set_announced_at(self.conn, "600519", "2026-06-30", "2026-08-28", "akshare/yjbb")
        self.assertEqual(n, 1)
        rows = store.read_fundamentals(self.conn, "600519", as_of="2026-08-27")
        self.assertEqual(rows, [])  # 公告前不可见
        rows = store.read_fundamentals(self.conn, "600519", as_of="2026-08-28")
        self.assertEqual(rows[0]["field"], "revenue")
        self.assertEqual(rows[0]["announced_source"], "akshare/yjbb")

    def test_universe_latest_snapshot(self):
        store.store_universe(self.conn, "2026-08-29", "SH.000300", ["600519", "00700"], "t")
        store.store_universe(self.conn, "2026-09-12", "SH.000300", ["600519"], "t")
        snap = store.read_universe(self.conn, as_of="2026-09-13", index_name="SH.000300")
        self.assertEqual(snap["snapshot_date"], "2026-09-12")
        self.assertEqual(snap["symbols"], ["600519"])

    def test_calendar_and_trading_days(self):
        store.upsert_calendar(self.conn, "SH", [
            {"day": "2026-09-11", "trade_date_type": "WHOLE", "trade_second": 14400},
            {"day": "2026-09-12", "trade_date_type": "CLOSE", "trade_second": 0}])
        self.assertTrue(store.is_trading_day(self.conn, "SH", "2026-09-11"))
        self.assertFalse(store.is_trading_day(self.conn, "SH", "2026-09-12"))
        self.assertEqual(store.trading_days(self.conn, "SH", "2026-09-10", "2026-09-12"),
                         ["2026-09-11"])

    def test_is_trading_day_without_calendar_raises(self):
        with self.assertRaises(RuntimeError):
            store.is_trading_day(self.conn, "HK", "2026-09-11")

    def test_kv_roundtrip(self):
        store.kv_set(self.conn, "backfill:daily", {"done": ["600519"], "failed": {}})
        self.assertEqual(store.kv_get(self.conn, "backfill:daily")["done"], ["600519"])
        self.assertIsNone(store.kv_get(self.conn, "nope", default=None))

    def test_adjustments_roundtrip(self):
        rows = [{"ex_date": "2026-06-20", "cum_forward": 12.34, "cum_backward": 0.98,
                 "actions": ["DIVIDEND", "SPLIT"]}]
        self.assertEqual(store.upsert_adjustments(self.conn, "SH.600519", rows, "futu/rehab"), 1)
        got = store.read_adjustments(self.conn, "SH.600519", as_of="2026-09-14")
        self.assertEqual(got[0]["actions"], ["DIVIDEND", "SPLIT"])
        self.assertAlmostEqual(got[0]["cum_forward"], 12.34)
        self.assertEqual(store.read_adjustments(self.conn, "SH.600519", as_of="2026-06-19"), [])
        with self.assertRaises(ValueError):
            store.read_adjustments(self.conn, "SH.600519", as_of=None)

    def test_announced_coverage_groups_by_market_prefix(self):
        store.upsert_fundamentals(self.conn, "SH.600519",
                                  [{"field": "revenue", "period_end": "2026-06-30", "value": 1.0}],
                                  "s")
        store.upsert_fundamentals(self.conn, "HK.00700",
                                  [{"field": "revenue", "period_end": "2026-06-30", "value": 2.0}],
                                  "s")
        store.upsert_fundamentals(self.conn, "bare600519",
                                  [{"field": "revenue", "period_end": "2026-06-30", "value": 3.0}],
                                  "s")
        store.set_announced_at(self.conn, "SH.600519", "2026-06-30", "2026-08-28", "ak")
        cov = store.announced_coverage(self.conn)
        self.assertEqual(cov["SH"], {"total": 1, "with_date": 1})
        self.assertEqual(cov["HK"], {"total": 1, "with_date": 0})
        self.assertEqual(cov["?"], {"total": 1, "with_date": 0})  # 无市场前缀 → ? 桶

    def test_last_bar_date_and_set_announced_at_no_overwrite(self):
        store.upsert_bars(self.conn, "600519", "1d", BARS, "s")
        self.assertEqual(store.last_bar_date(self.conn, "600519", "1d"), "2026-09-11")
        self.assertIsNone(store.last_bar_date(self.conn, "nope", "1d"))
        store.upsert_fundamentals(self.conn, "600519",
                                  [{"field": "revenue", "period_end": "2026-06-30", "value": 1.0}],
                                  "s")
        store.set_announced_at(self.conn, "600519", "2026-06-30", "2026-08-28", "ak1")
        n = store.set_announced_at(self.conn, "600519", "2026-06-30", "2026-09-30", "ak2")
        self.assertEqual(n, 0)  # 已有公告日：重跑合并不得覆写（PIT 关键保证）
        rows = store.read_fundamentals(self.conn, "600519", as_of="2026-09-30")
        self.assertEqual(rows[0]["announced_at"], "2026-08-28")
        self.assertEqual(rows[0]["announced_source"], "ak1")

    def test_resync_fundamentals_keeps_announced_at(self):
        store.upsert_fundamentals(self.conn, "SH.600519",
                                  [{"field": "revenue", "period_end": "2026-06-30", "value": 1.0}],
                                  "futu/statements")
        store.set_announced_at(self.conn, "SH.600519", "2026-06-30", "2026-08-28", "akshare/yjbb")
        store.upsert_fundamentals(self.conn, "SH.600519",
                                  [{"field": "revenue", "period_end": "2026-06-30", "value": 9.9}],
                                  "futu/statements")
        rows = store.read_fundamentals(self.conn, "SH.600519", as_of="2026-09-14")
        self.assertEqual(rows[0]["value"], 9.9)          # 数值刷新
        self.assertEqual(rows[0]["announced_at"], "2026-08-28")  # 公告日保留
        self.assertEqual(rows[0]["announced_source"], "akshare/yjbb")

    def test_valuations_pit(self):
        store.upsert_valuations(self.conn, "SH.600519", "2026-09-13",
                                {"pe": 22.5, "pb": 8.1}, "futu/valuation")
        store.upsert_valuations(self.conn, "SH.600519", "2026-09-14",
                                {"pe": 22.1, "pb": 8.0}, "futu/valuation")
        rows = store.read_valuations(self.conn, "SH.600519", as_of="2026-09-13")
        self.assertEqual(rows["pe"], 22.5)
        with self.assertRaises(ValueError):
            store.read_valuations(self.conn, "SH.600519", as_of=None)

    # ------------------------------------------------------------------
    # v4（WP9）：plans origin/market 补列 + auto 计划查询/过期 helpers
    # ------------------------------------------------------------------

    def _mark_auto(self, plan_id, market):
        self.conn.execute("UPDATE plans SET origin='auto', market=? WHERE plan_id=?",
                          (market, plan_id))
        self.conn.commit()

    def test_plans_origin_market_columns(self):
        # connect() 已跑一次 migrate；再跑两次验证补列幂等（v3 起源库无这两列）
        store.migrate(self.conn)
        store.migrate(self.conn)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(plans)")}
        self.assertIn("origin", cols)
        self.assertIn("market", cols)
        # 既有手工路径（insert_plan 不传 origin/market）→ 默认 manual/NULL
        store.insert_plan(self.conn, "PLN-1", "2026-09-15", "sim", "rsi",
                          {"SH.600519": 0.5}, "hash1")
        row = self.conn.execute(
            "SELECT origin, market FROM plans WHERE plan_id='PLN-1'").fetchone()
        self.assertEqual(row["origin"], "manual")
        self.assertIsNone(row["market"])

    def test_migrate_upgrades_v3_plans_without_columns(self):
        # 模拟 v3 旧库：plans 表无 origin/market；migrate 补列且存量行回填默认值
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE plans(plan_id TEXT PRIMARY KEY, as_of TEXT NOT NULL,"
            " mode TEXT NOT NULL, strategy_id TEXT NOT NULL, target TEXT NOT NULL,"
            " content_hash TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,"
            " approved_at TEXT, approved_by TEXT)")
        conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
            "status,created_at) VALUES('OLD-1','2026-09-10','sim','rsi','{}','h',"
            "'frozen','2026-09-10 16:40:00')")
        try:
            store.migrate(conn)
            store.migrate(conn)  # 幂等
            cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)")}
            self.assertIn("origin", cols)
            self.assertIn("market", cols)
            row = conn.execute(
                "SELECT origin, market FROM plans WHERE plan_id='OLD-1'").fetchone()
            self.assertEqual(row["origin"], "manual")  # 存量行回填默认
            self.assertIsNone(row["market"])
        finally:
            conn.close()

    def test_get_latest_auto_plan(self):
        store.insert_plan(self.conn, "A1", "2026-09-15", "sim", "rsi", {"x": 1}, "h1")
        store.insert_plan(self.conn, "A2", "2026-09-15", "sim", "rsi", {"x": 2}, "h2")
        store.insert_plan(self.conn, "A3", "2026-09-15", "sim", "rsi", {"x": 3}, "h3")
        self._mark_auto("A1", "SH")
        self._mark_auto("A2", "SH")
        self._mark_auto("A3", "HK")
        plan = store.get_latest_auto_plan(self.conn, "SH", "2026-09-15")
        self.assertIsNotNone(plan)
        self.assertEqual(plan["plan_id"], "A2")  # 同 market/as_of 取最新（同秒按 rowid 兜底）
        self.assertEqual(plan["origin"], "auto")
        self.assertEqual(plan["market"], "SH")
        self.assertEqual(plan["target"], {"x": 2})  # target 反序列化（get_plan 口径）
        self.assertIsNone(store.get_latest_auto_plan(self.conn, "SH", "2026-09-16"))
        self.assertIsNone(store.get_latest_auto_plan(self.conn, "US", "2026-09-15"))

    def test_get_latest_auto_plan_filters_origin_and_status(self):
        # 手工计划（origin=manual）即使 market/as_of 匹配也永不命中
        store.insert_plan(self.conn, "M1", "2026-09-15", "sim", "rsi", {}, "h1")
        self.conn.execute("UPDATE plans SET market='SH' WHERE plan_id='M1'")
        self.conn.commit()
        self.assertIsNone(store.get_latest_auto_plan(self.conn, "SH", "2026-09-15"))
        # 非 frozen 状态默认不命中；status 可显式指定
        store.insert_plan(self.conn, "A1", "2026-09-15", "sim", "rsi", {}, "h2")
        self._mark_auto("A1", "SH")
        self.conn.execute("UPDATE plans SET status='executing' WHERE plan_id='A1'")
        self.conn.commit()
        self.assertIsNone(store.get_latest_auto_plan(self.conn, "SH", "2026-09-15"))
        got = store.get_latest_auto_plan(self.conn, "SH", "2026-09-15", status="executing")
        self.assertEqual(got["plan_id"], "A1")

    def test_cancel_stale_auto_plans(self):
        store.insert_plan(self.conn, "OLD-A", "2026-09-14", "sim", "rsi", {}, "h1")
        store.insert_plan(self.conn, "OLD-M", "2026-09-14", "sim", "rsi", {}, "h2")
        store.insert_plan(self.conn, "NEW-A", "2026-09-15", "sim", "rsi", {}, "h3")
        store.insert_plan(self.conn, "DONE-A", "2026-09-14", "sim", "rsi", {}, "h4")
        self._mark_auto("OLD-A", "SH")
        self._mark_auto("NEW-A", "SH")
        self._mark_auto("DONE-A", "SH")
        self.conn.execute("UPDATE plans SET status='done' WHERE plan_id='DONE-A'")
        self.conn.commit()
        cancelled = store.cancel_stale_auto_plans(self.conn, "2026-09-15")
        self.assertEqual(cancelled, ["OLD-A"])  # 仅 frozen 且 as_of<today 的 auto
        statuses = {r["plan_id"]: r["status"] for r in
                    self.conn.execute("SELECT plan_id,status FROM plans")}
        self.assertEqual(statuses["OLD-A"], "cancelled")
        self.assertEqual(statuses["OLD-M"], "frozen")  # 手工计划不动
        self.assertEqual(statuses["NEW-A"], "frozen")  # 当日计划不动（严格 <）
        self.assertEqual(statuses["DONE-A"], "done")   # 非 frozen 不动
        self.assertEqual(store.cancel_stale_auto_plans(self.conn, "2026-09-15"), [])  # 幂等


if __name__ == "__main__":
    unittest.main()
