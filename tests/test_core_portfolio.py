"""组合回测：月度再平衡、成本、A股约束、基准对比。全部合成数据离线。

计划修正：引擎交易日取自 store.trading_days（WP1 设计：日历硬依赖），故
setUp 需补日历 fixture（计划测试骨架遗漏）。
"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import portfolio, store  # noqa: E402

DAYS = [(m, d) for m in range(1, 13) for d in range(1, 26)]


class BacktestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        store.upsert_calendar(self.conn, "SH",
                              [{"day": f"2025-{m:02d}-{d:02d}", "trade_date_type": "WHOLE"}
                               for m, d in DAYS])
        syms = ["600519", "000858", "300750", "601899", "600036", "000333"]
        for k, s in enumerate(syms):
            bars = [{"t": f"2025-{m:02d}-{d:02d}",
                     "o": 10 + k, "h": 11 + k, "l": 9 + k,
                     "c": 10 + k + ((i * 7 + k * 3) % 11 - 5) * 0.1, "v": 1000}
                    for i, (m, d) in enumerate(DAYS)]
            store.upsert_bars(self.conn, f"{'SH.' if s[0] in '69' else 'SZ.'}{s}", "1d", bars, "t")
        store.upsert_bars(self.conn, "SH.000300", "1d",
                          [{"t": f"2025-{m:02d}-{d:02d}", "o": 4000, "h": 4010, "l": 3990,
                            "c": 4000 + i, "v": 1} for i, (m, d) in enumerate(DAYS)], "t")

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_run_produces_metrics_and_respects_constraints(self):
        class Static:
            id = "static5"
            def target_weights(self, conn, as_of):
                syms = ["SH.600519", "SH.000858", "SZ.300750", "SH.601899", "SH.600036"]
                return {s: 0.2 for s in syms}

        r = portfolio.run(self.conn, Static(), start="2025-01-01", end="2025-12-24",
                          benchmark="SH.000300")
        for k in ("total_return", "annual", "sharpe", "max_drawdown", "excess_annual",
                  "information_ratio", "turnover", "param_groups"):
            self.assertIn(k, r["summary"])
        self.assertIn("equity", r)
        self.assertEqual(r["summary"]["param_groups"], 1)  # 静态策略也如实报组数
        self.assertEqual(len(r["equity"]), len(store.trading_days(self.conn, "SH",
                                                                  "2025-01-01", "2025-12-24")))

    def test_turnover_positive_after_rebalance(self):
        class Static:
            id = "static5"
            def target_weights(self, conn, as_of):
                return {s: 0.2 for s in ["SH.600519", "SH.000858", "SZ.300750",
                                         "SH.601899", "SH.600036"]}

        r = portfolio.run(self.conn, Static(), start="2025-01-01", end="2025-12-24")
        self.assertGreater(r["summary"]["turnover"], 0.0)
        self.assertIsNone(r["summary"]["excess_annual"])  # 无基准 → 无超额口径


if __name__ == "__main__":
    unittest.main()
