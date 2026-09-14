"""同步层单测：增量过滤、回填断点续传、复权/财务/公告日/成分股映射。全部离线（注入假取数器）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
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
        self.assertEqual(summary2["failed"], {})  # 成功后 failed 必须清空

    def test_incremental_needed_estimation(self):
        cases = []
        loader = lambda t, p, limit: (cases.append(limit) or (_bars("2026-09-14"), "x", False))

        sync.sync_bars_incremental(self.conn, "600519", "1d", loader=loader)  # 首次
        self.assertEqual(cases[-1], 370)

        sync.sync_bars_incremental(self.conn, "600519", "1d",
                                   loader=lambda t, p, l: (_bars("2099-01-01"), "x", False))
        # 上一次把 last 推到 2099（未来）：since 为负 → max(…,5) 兜底
        cases.clear()
        sync.sync_bars_incremental(self.conn, "600519", "1d",
                                   loader=lambda t, p, l: (cases.append(l) or ([], "x", False)))
        self.assertEqual(cases[-1], 5)

        # 手工把 last 拨回很旧：last_bar_date=MAX(ts)，须先清掉 2099 行，upsert 旧 K 线才生效
        self.conn.execute("DELETE FROM bars WHERE symbol='600519' AND period='1d'")
        self.conn.commit()
        store.upsert_bars(self.conn, "600519", "1d", _bars("2020-01-01"), "x")
        cases.clear()
        sync.sync_bars_incremental(self.conn, "600519", "1d",
                                   loader=lambda t, p, l: (cases.append(l) or ([], "x", False)))
        self.assertEqual(cases[-1], 370)  # 很旧的 last → 上限封顶

    def test_incremental_rejects_intraday(self):
        with self.assertRaises(ValueError):
            sync.sync_bars_incremental(self.conn, "600519", "5m", loader=lambda t, p, l: ([], "x", False))


REHAB_SAMPLE = {"rehabs": [
    {"ex_div_date": "2026-06-20", "cum_forward_adj_factorA": 12.34,
     "cum_backward_adj_factorA": 0.98, "action_types": ["DIVIDEND"], "per_cash_div": 2.0},
    {"ex_div_date": "2025-06-20", "cum_forward_adj_factorA": 12.10,
     "cum_backward_adj_factorA": 0.97, "action_types": ["DIVIDEND"], "per_cash_div": 1.8}]}

STATEMENTS_SAMPLE = {"report_list": [
    {"date_time": 1782748800000, "financial_type": 2, "fiscal_year": 2026,
     "item_list": [{"display_name": "Total Operating Revenue", "data": 37575159697.98},
                   {"display_name": "Net Profit", "data": 1890123456.78},
                   {"display_name": "Gross Profit", "data": 3012345678.9},
                   {"display_name": "Diluted EPS", "data": 15.06}]}]}


class AdjustmentsFundamentalsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_adjustments_mapping(self):
        seen = {}

        def fetcher(name, args):
            seen["args"] = args
            return REHAB_SAMPLE

        n = sync.sync_adjustments(self.conn, "600519", fetcher=fetcher)
        self.assertEqual(n, 2)
        self.assertEqual(seen["args"]["symbol"], "SH.600519")   # futu 格式
        rows = store.read_adjustments(self.conn, "SH.600519", as_of="2026-09-14")
        self.assertEqual(rows[-1]["cum_forward"], 12.34)
        self.assertEqual(rows[-1]["actions"], ["DIVIDEND"])

    def test_fundamentals_aliases_and_pit_invisible(self):
        seen = {}

        def fetcher(name, args):
            seen["args"] = args
            return STATEMENTS_SAMPLE

        n = sync.sync_fundamentals(self.conn, "600519", fetcher=fetcher)
        self.assertEqual(n, 4)  # revenue/net_profit/gross_profit/diluted_eps 各一期
        self.assertEqual(seen["args"]["symbol"], "SH.600519")
        # 富途无公告日：PIT 读取必须不可见，直到任务 6 的合并作业补上
        self.assertEqual(store.read_fundamentals(self.conn, "600519", as_of="2026-09-14"), [])
        store.set_announced_at(self.conn, "SH.600519", "2026-06-30", "2026-08-28", "ak")
        rows = store.read_fundamentals(self.conn, "SH.600519", as_of="2026-08-28")
        self.assertEqual({r["period_end"] for r in rows}, {"2026-06-30"})  # UTC+8 换算锁定

    def test_alias_fallback_and_type_guard(self):
        sample = {"report_list": [
            {"date_time": 1782748800000, "item_list": [
                {"display_name": "Operating Revenue", "data": 100.0},   # 首别名缺失，次选命中
                {"display_name": "Net Profit", "data": "N/A"}]}]}       # 非数值 → 缺指标

        n = sync.sync_fundamentals(self.conn, "600519", fetcher=lambda n_, a_: sample)
        self.assertEqual(n, 1)
        store.set_announced_at(self.conn, "SH.600519", "2026-06-30", "2026-08-28", "ak")
        rows = store.read_fundamentals(self.conn, "SH.600519", as_of="2026-09-14")
        self.assertEqual([r["field"] for r in rows], ["revenue"])


if __name__ == "__main__":
    unittest.main()
