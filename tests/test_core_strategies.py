"""策略注册表：单标的策略移植 + 横截面策略 target_weights。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
# 信号函数唯一实现住 datasource（计划测试骨架缺这行：不插入则 strategies 的
# trading_datasource.backtest 导入无法解析）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
from trading_core import store, strategies  # noqa: E402


class StrategiesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        bars = [{"t": f"2026-{m:02d}-{d:02d}", "o": 10, "h": 10.5, "l": 9.5,
                 "c": 10 + (i % 40) * 0.05, "v": 100}
                for i, (m, d) in enumerate([(m, d) for m in range(1, 10) for d in range(1, 29)])]
        for s in ("SH.600519", "SH.000858", "SZ.300750", "SH.601899", "SH.600036",
                  "SZ.000333", "SH.601318", "SZ.002415"):
            store.upsert_bars(self.conn, s, "1d", bars, "test")
        store.store_universe(self.conn, "2026-09-13", "SH.000300",
                             [s.split(".")[1] for s in ("SH.600519", "SH.000858", "SZ.300750",
                                                        "SH.601899", "SH.600036",
                                                        "SZ.000333", "SH.601318", "SZ.002415")], "t")
        for day in ("2026-09-12", "2026-09-13"):
            store.upsert_valuations(self.conn, "SH.600519", day, {"pe_ttm": 20.0}, "t")
            store.upsert_valuations(self.conn, "SH.000858", day, {"pe_ttm": 12.0}, "t")
            store.upsert_valuations(self.conn, "SZ.300750", day, {"pe_ttm": 30.0}, "t")
            store.upsert_valuations(self.conn, "SH.601899", day, {"pe_ttm": 10.0}, "t")
            store.upsert_valuations(self.conn, "SH.600036", day, {"pe_ttm": 6.0}, "t")

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_cross_section_strategy_topn_equal_weight(self):
        strat = strategies.REGISTRY["momentum_value_top5"]
        w = strat.target_weights(self.conn, "2026-09-13")
        self.assertLessEqual(len(w), 5)
        self.assertLessEqual(sum(w.values()), 1.0 + 1e-9)
        self.assertTrue(all(v > 0 for v in w.values()))

    def test_single_ticker_strategy_signal(self):
        strat = strategies.REGISTRY["rsi"]
        self.assertIn(strat.signal(self.conn, "SH.600519", "2026-09-13"), ("BUY", "SELL", "HOLD"))

    def test_universe_maps_bare_codes_to_futu(self):
        strat = strategies.REGISTRY["momentum_value_top5"]
        universe = strat.universe(self.conn, "2026-09-13")
        self.assertIn("SH.600519", universe)
        self.assertIn("SZ.300750", universe)
        self.assertEqual(len(universe), 8)


if __name__ == "__main__":
    unittest.main()
