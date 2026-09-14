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


if __name__ == "__main__":
    unittest.main()
