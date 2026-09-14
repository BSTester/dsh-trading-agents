"""质量层单测：缺口检测、新鲜度、announced_at 覆盖率、跨源交叉校验。全部离线。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
from trading_core import quality, store, sync  # noqa: E402


class QualityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        store.upsert_calendar(self.conn, "SH", [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400}
            for d in ("2026-09-10", "2026-09-11", "2026-09-14")])
        store.upsert_bars(self.conn, "600519", "1d",
                          _bars("2026-09-10", "2026-09-14"), "futu/x")  # 缺 09-11

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_gap_report_finds_missing_day(self):
        gaps = quality.gap_report(self.conn, "SH", "600519", "2026-09-10", "2026-09-14")
        self.assertEqual(gaps, ["2026-09-11"])

    def test_freshness_counts_calendar_days(self):
        info = quality.freshness(self.conn, "600519", "1d", today="2026-09-14")
        self.assertEqual(info["last"], "2026-09-14")
        self.assertEqual(info["days_behind"], 0)

    def test_cross_source_mismatch_detection(self):
        fresh = _bars("2026-09-14", base=99.0)  # 与库内 close 10.5 差异巨大

        def loader(ticker, period, limit):
            return fresh, "sina/test", False

        bad = quality.cross_source_check(self.conn, "600519", "1d", sample=1, loader=loader)
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0]["date"], "2026-09-14")

    def test_cross_source_within_tolerance_ignored(self):
        fresh = _bars("2026-09-14", base=10.03)  # c=10.53，与库内 10.5 差 0.03 < 0.5% 容差

        def loader(ticker, period, limit):
            return fresh, "sina/test", False

        self.assertEqual(
            quality.cross_source_check(self.conn, "600519", "1d", sample=1, loader=loader), [])

    def test_cross_source_skips_dates_missing_in_fresh(self):
        fresh = _bars("2026-09-13", base=99.0)  # fresh 无 09-14：跳过而非误报

        def loader(ticker, period, limit):
            return fresh, "sina/test", False

        self.assertEqual(
            quality.cross_source_check(self.conn, "600519", "1d", sample=1, loader=loader), [])

    def test_announced_coverage(self):
        sync.sync_fundamentals(self.conn, "600519", fetcher=lambda n, a: STATEMENTS_SAMPLE)
        store.set_announced_at(self.conn, "SH.600519", "2026-06-30", "2026-08-28", "akshare/yjbb")
        cov = quality.announced_coverage(self.conn)
        self.assertEqual(cov["SH"], {"total": 4, "with_date": 4})


def _bars(*dates, base=10.0):
    return [{"t": d, "o": base, "h": base + 1, "l": base - 0.5, "c": base + 0.5, "v": 100.0}
            for d in dates]


STATEMENTS_SAMPLE = {"report_list": [
    {"date_time": 1782748800000, "financial_type": 2, "fiscal_year": 2026,
     "item_list": [{"display_name": "Total Operating Revenue", "data": 1.0},
                   {"display_name": "Net Profit", "data": 2.0},
                   {"display_name": "Gross Profit", "data": 3.0},
                   {"display_name": "Diluted EPS", "data": 4.0}]}]}


if __name__ == "__main__":
    unittest.main()
