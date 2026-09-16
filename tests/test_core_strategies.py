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
        # 市场维度（K1）：SH 链含 SZ/BJ，HK/US 各自分片互不混入
        self.assertEqual(strat.universe(self.conn, "2026-09-13", home=self.tmp.name,
                                        market="SH"), ["SH.600519", "SZ.300750"])
        self.assertEqual(strat.universe(self.conn, "2026-09-13", home=self.tmp.name,
                                        market="HK"), ["HK.00700"])
        self.assertEqual(strat.universe(self.conn, "2026-09-13", home=self.tmp.name,
                                        market="US"), [])
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


class WatchlistMarketScopingTest(unittest.TestCase):
    """K1 反证：三市场关注池下每市场各自计数与截断（旧实现在全市场范围算分母/截断，

    ``sorted()`` 下 ``U>S>H`` 使美股恒被截掉——该市场只可能产生退出单，永不建仓）。
    """

    SYMBOLS = ("SH.600519", "SH.601899", "HK.00005", "HK.00700",
               "US.AAPL", "US.MSFT")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        self.addCleanup(self.conn.close)
        bars = [{"t": f"2026-{m:02d}-{d:02d}", "o": 10, "h": 10.5, "l": 9.5,
                 "c": 10 + (i % 40) * 0.05, "v": 100}
                for i, (m, d) in enumerate([(m, d) for m in range(1, 10)
                                            for d in range(1, 29)])]
        for symbol in self.SYMBOLS:
            store.upsert_bars(self.conn, symbol, "1d", bars, "test")
        self.strat = strategies.REGISTRY["watchlist_rsi"]
        self._config({"watchlist": list(self.SYMBOLS)})

    def _config(self, payload):
        (self.home / "trading-platform.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _risk(self, **overlay):
        (self.home / "trading-risk.json").write_text(
            json.dumps(overlay), encoding="utf-8")

    def _weights(self, **kwargs):
        with mock.patch.object(type(self.strat), "signal",
                               lambda self, conn, symbol, as_of: "BUY"):
            return self.strat.target_weights(self.conn, "2026-09-13",
                                             home=str(self.home), **kwargs)

    def test_us_not_starved_by_max_positions(self):
        """三市场各 2 只全 BUY、max_positions=5 → 美股两只都在（旧实现恒为 0）。"""
        weights = self._weights(market="US")
        self.assertEqual(weights, {"US.AAPL": 0.25, "US.MSFT": 0.25})
        self.assertEqual(self._weights(market="HK"),
                         {"HK.00005": 0.25, "HK.00700": 0.25})
        self.assertEqual(self._weights(market="SH"),
                         {"SH.600519": 0.25, "SH.601899": 0.25})

    def test_denominator_counts_only_this_market(self):
        """分母是本市场 BUY 数（上限放宽到 0.5 后可见）：2 只 → 各 0.5，而非 6 只的 1/6。"""
        self._risk(max_position_pct=0.5)
        self.assertEqual(self._weights(market="US"), {"US.AAPL": 0.5, "US.MSFT": 0.5})

    def test_truncation_is_per_market(self):
        """10 只美股 + max_positions=5 → 只截断本市场，每只 1/10（不被其他市场挤占）。"""
        extra = [f"US.0000{i}" for i in range(8)]
        bars = [{"t": f"2026-{m:02d}-{d:02d}", "o": 10, "h": 10.5, "l": 9.5,
                 "c": 10 + (i % 40) * 0.05, "v": 100}
                for i, (m, d) in enumerate([(m, d) for m in range(1, 10)
                                            for d in range(1, 29)])]
        for symbol in extra:
            store.upsert_bars(self.conn, symbol, "1d", bars, "test")
        self._config({"watchlist": list(self.SYMBOLS) + extra})
        us_all = sorted([s for s in self.SYMBOLS if s.startswith("US.")] + extra)
        weights = self._weights(market="US")
        self.assertEqual(list(weights), us_all[:5])
        self.assertTrue(all(w == 0.1 for w in weights.values()), weights)


class WatchlistNamedPoolTest(unittest.TestCase):
    """I1 反证：``watchlist`` 选池子（缺省 watchlist），指定池不存在 → fail-closed。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        self.addCleanup(self.conn.close)
        bars = [{"t": f"2026-{m:02d}-{d:02d}", "o": 10, "h": 10.5, "l": 9.5,
                 "c": 10 + (i % 40) * 0.05, "v": 100}
                for i, (m, d) in enumerate([(m, d) for m in range(1, 10)
                                            for d in range(1, 29)])]
        for symbol in ("US.AAPL", "US.MSFT", "SH.600519"):
            store.upsert_bars(self.conn, symbol, "1d", bars, "test")
        self.strat = strategies.REGISTRY["watchlist_rsi"]

    def _config(self, payload):
        (self.home / "trading-platform.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_named_pool_selects_symbols(self):
        """显式池键生效：us_pool 只有 AAPL，缺省池的 MSFT 不入权重。"""
        self._config({"watchlist": ["US.MSFT"], "us_pool": ["US.AAPL"]})
        with mock.patch.object(type(self.strat), "signal",
                               lambda self, conn, symbol, as_of: "BUY"):
            weights = self.strat.target_weights(self.conn, "2026-09-13",
                                                home=str(self.home),
                                                market="US", watchlist="us_pool")
        self.assertEqual(weights, {"US.AAPL": 0.25})

    def test_missing_explicit_pool_fails_closed(self):
        """指定的池键不存在 → ValueError（fail-closed：不静默换池子）。"""
        self._config({"watchlist": ["US.MSFT"]})
        with self.assertRaises(ValueError):
            self.strat.universe(self.conn, "2026-09-13", home=str(self.home),
                                market="US", watchlist="nope_pool")

    def test_missing_default_pool_is_legal_empty(self):
        """缺省池不存在 = 合法空池（历史配置可能根本没有该键），不抛错。"""
        self._config({"other": ["US.MSFT"]})
        self.assertEqual(self.strat.universe(self.conn, "2026-09-13",
                                             home=str(self.home), market="US"), [])


if __name__ == "__main__":
    unittest.main()
