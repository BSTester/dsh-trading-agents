"""离线端到端：假渠道驱动 full pipeline，验收口径对齐规格 §十 WP1。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import calendar, quality, store, sync  # noqa: E402

TICKERS = ["600519", "000858"]
DAYS = ["2026-09-10", "2026-09-11", "2026-09-14"]


def _bars(dates):
    return [{"t": d, "o": 1.0, "h": 2.0, "l": 0.5, "c": 1.5, "v": 9.0} for d in dates]


class PipelineTest(unittest.TestCase):
    def test_full_pipeline_zero_gaps_and_coverage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        self.addCleanup(conn.close)

        # 1) 日历（假 fetcher）
        n_days = calendar.sync_calendar(
            conn, "SH", DAYS[0], DAYS[-1],
            fetcher=lambda m, s, e: {"trading_days": [
                {"time": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in DAYS]})
        self.assertEqual(n_days, 3)

        # 2) 回填（假 loader，模拟一次失败后续传）
        state = {"calls": 0}

        def loader(ticker, period, limit):
            state["calls"] += 1
            if ticker == "000858" and state["calls"] == 2:
                raise RuntimeError("模拟瞬时失败")
            return _bars(DAYS), "fake/src", False

        sync.backfill_bars(conn, TICKERS, loader=loader, sleep_seconds=0)
        summary = sync.backfill_bars(conn, TICKERS, loader=loader, sleep_seconds=0,
                                     progress_key=sync.PROGRESS_KEY_BACKFILL)
        self.assertEqual(set(summary["failed"]), set())

        # 3) 财务 + 公告日合并
        statements = {"report_list": [
            {"date_time": 1782748800000, "item_list": [
                {"display_name": "Total Operating Revenue", "data": 5.0},
                {"display_name": "Net Profit", "data": 1.0},
                {"display_name": "Gross Profit", "data": 2.0},
                {"display_name": "Diluted EPS", "data": 0.5}]}]}

        import pandas as pd

        class FakeAk:
            @staticmethod
            def stock_yjbb_em(date):
                return pd.DataFrame([{"股票代码": t, "公告日期": "2026-08-28"} for t in TICKERS])

        for t in TICKERS:
            sync.sync_fundamentals(conn, t, fetcher=lambda n, a: statements)
        merge = sync.merge_announcements_akshare(conn, "20260630", akshare_module=FakeAk)
        self.assertEqual(merge["matched"], 8)

        # 4) 质量报告：零缺口 + 覆盖率 100%
        report = quality.full_report(conn, "SH", TICKERS, DAYS[0], DAYS[-1])
        self.assertEqual(report["gaps"], {t: [] for t in TICKERS})
        self.assertEqual(report["announced_coverage"],
                         {"SH": {"total": 4, "with_date": 4},
                          "SZ": {"total": 4, "with_date": 4}})

        # 5) PIT 纪律抽查：公告日之前读不到财务
        self.assertEqual(store.read_fundamentals(conn, "600519", as_of="2026-08-27"), [])


if __name__ == "__main__":
    unittest.main()
