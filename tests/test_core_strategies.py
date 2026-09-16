"""策略注册表：单标的策略移植 + 横截面策略 target_weights。"""
import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
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


    def test_watchlist_rsi_registered(self):
        self.assertIn("watchlist_rsi", strategies.REGISTRY)

    def test_watchlist_rsi_universe_reads_config(self):
        # 关注池是平面列表（daemon.resolve_command 的 ",".join 口径）
        (Path(self.tmp.name) / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.600519", "SZ.300750", "HK.00700"]}),
            encoding="utf-8")
        strat = strategies.REGISTRY["watchlist_rsi"]
        universe = strat.universe(self.conn, "2026-09-13", home=self.tmp.name)
        self.assertEqual(universe, ["SH.600519", "SZ.300750", "HK.00700"])
        # 未配置关注池 → 空（调用方按空处理）
        empty = tempfile.mkdtemp()
        self.assertEqual(strat.universe(self.conn, "2026-09-13", home=empty), [])

    def test_watchlist_rsi_universe_defaults_home_from_dsh_home(self):
        (Path(self.tmp.name) / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.600519"]}), encoding="utf-8")
        strat = strategies.REGISTRY["watchlist_rsi"]
        with mock.patch.dict(os.environ, {"DSH_HOME": self.tmp.name}):
            self.assertEqual(strat.universe(self.conn, "2026-09-13"), ["SH.600519"])

    def test_watchlist_rsi_equal_weight_on_buy(self):
        (Path(self.tmp.name) / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.600519", "SZ.300750", "SH.601899"]}),
            encoding="utf-8")
        strat = strategies.REGISTRY["watchlist_rsi"]
        signals = {"SH.600519": "BUY", "SZ.300750": "HOLD", "SH.601899": "BUY"}
        with mock.patch.object(type(strat), "signal",
                               lambda self, conn, symbol, as_of: signals[symbol]):
            weights = strat.target_weights(self.conn, "2026-09-13", home=self.tmp.name)
        # BUY 两个 → 等权基数 1/2 = 0.5，但受单票上限 max_position_pct(默认 0.25) 约束
        # → 每只 0.25（规格 §4.2 第 5 点：策略不生成规则 5 注定拒绝的目标）；
        # HOLD 不入表（减仓由 planner 持仓 diff 处理）
        self.assertEqual(weights, {"SH.600519": 0.25, "SH.601899": 0.25})
        self.assertLessEqual(sum(weights.values()), 1.0 + 1e-9)

    def test_watchlist_rsi_all_hold_returns_empty(self):
        (Path(self.tmp.name) / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.600519", "SZ.300750"]}), encoding="utf-8")
        strat = strategies.REGISTRY["watchlist_rsi"]
        with mock.patch.object(type(strat), "signal",
                               lambda self, conn, symbol, as_of: "HOLD"):
            self.assertEqual(strat.target_weights(self.conn, "2026-09-13", home=self.tmp.name), {})

    def test_watchlist_rsi_short_history_skipped(self):
        # 真实信号路径（不打桩）：bar 不足 25 根 → HOLD（宁缺毋假）→ 不入权重
        bars = [{"t": f"2026-08-{d:02d}", "o": 10, "h": 10.5, "l": 9.5, "c": 10.0, "v": 100}
                for d in range(1, 6)]
        store.upsert_bars(self.conn, "SH.601899", "1d", bars, "test")
        (Path(self.tmp.name) / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.601899", "SH.600519"]}), encoding="utf-8")
        strat = strategies.REGISTRY["watchlist_rsi"]
        self.assertEqual(strat.signal(self.conn, "SH.601899", "2026-09-13"), "HOLD")
        weights = strat.target_weights(self.conn, "2026-09-13", home=self.tmp.name)
        self.assertNotIn("SH.601899", weights)

    def test_watchlist_rsi_empty_watchlist(self):
        empty = tempfile.mkdtemp()
        strat = strategies.REGISTRY["watchlist_rsi"]
        self.assertEqual(strat.target_weights(self.conn, "2026-09-13", home=empty), {})


if __name__ == "__main__":
    unittest.main()
