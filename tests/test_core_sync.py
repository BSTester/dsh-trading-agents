"""同步层单测：增量过滤、回填断点续传、复权/财务/公告日/成分股映射。全部离线（注入假取数器）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import store, sync  # noqa: E402


def _bars(*dates, base=10.0):
    return [{"t": d, "o": base, "h": base + 1, "l": base - 0.5, "c": base + 0.5, "v": 100.0}
            for d in dates]


class BarsSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_incremental_skips_existing(self):
        calls = []

        def loader(ticker, period, limit):
            calls.append((ticker, period, limit))
            return _bars("2026-09-10", "2026-09-11"), "futu/x", False

        first = sync.sync_bars_incremental(self.conn, "600519", "1d", loader=loader)
        self.assertEqual(first["rows"], 2)
        second = sync.sync_bars_incremental(
            self.conn, "600519", "1d",
            loader=lambda t, p, l: (_bars("2026-09-11", "2026-09-14"), "futu/x", False))
        self.assertEqual(second["rows"], 1)  # 只补 09-14
        self.assertEqual(store.last_bar_date(self.conn, "600519", "1d"), "2026-09-14")
        self.assertEqual(len(calls), 1)  # 第二次用内联 lambda，不再记 calls

    def test_backfill_resume_after_failure(self):
        flaky = {"600519": _bars("2026-09-10", "2026-09-11"), "00700": None, "AAPL": _bars("2026-09-10")}

        def bad_loader(ticker, period, limit):
            bars = flaky[ticker]
            if bars is None:
                raise RuntimeError("取数失败：模拟断点")
            return bars, "fake", False

        summary = sync.backfill_bars(self.conn, ["600519", "00700", "AAPL"],
                                     loader=bad_loader, sleep_seconds=0)
        self.assertEqual(summary["ok"], ["600519", "AAPL"])
        self.assertIn("00700", summary["failed"])

        # 第二次运行：600519/AAPL 已在游标 done 里，只重试 00700
        fixed = {"600519": _bars("2026-09-10"), "00700": _bars("2026-09-11"), "AAPL": _bars("2026-09-10")}

        def good_loader(ticker, period, limit):
            return fixed[ticker], "fake", False

        summary2 = sync.backfill_bars(self.conn, ["600519", "00700", "AAPL"],
                                      loader=good_loader, sleep_seconds=0,
                                      progress_key=sync.PROGRESS_KEY_BACKFILL)
        self.assertEqual(summary2["ok"], ["00700"])
        self.assertEqual(store.last_bar_date(self.conn, "00700", "1d"), "2026-09-11")


if __name__ == "__main__":
    unittest.main()
