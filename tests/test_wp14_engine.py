"""WP14 任务 2：规则解释器（spec → Strategy 协议实例）。全部离线。

覆盖：zscore 等权合成与 top_n 选取；缺值标的跳过（完整样本口径）；ic_weighted
（|RankIC| 归一、不足 30 日拒绝）；daily/weekly/monthly 再平衡沿用上次目标；
风控上限与 ``watchlist_rsi`` 同口径（共用 ``strategies.risk_capped_weights``）；
``register_rule`` 后注册表可被 ``plan_auto`` 消费；``load_rule`` 二次校验；
universe 市场片段解析 fail-closed。
"""
import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
from trading_core import rule_engine, store, strategies  # noqa: E402

SYMBOLS = ["SH.600519", "SH.600036", "SH.601318", "SH.601899", "SH.600858"]


def _spec(**over):
    spec = {"rule_id": "rule_x", "hypothesis": "测试假设：动量与估值同向",
            "factors": ["f1", "f2"], "combine": "zscore_equal_weight",
            "universe": "watchlist.SH", "top_n": 3, "rebalance": "daily",
            "provenance": {"research_run_id": "R1", "created_by": "harness",
                           "created_at": "2026-09-16 10:00:00"}}
    spec.update(over)
    return spec


def _registry(values_by_factor):
    """注入用因子注册表：``{factor: {symbol: value|None}}`` → ``{name: fn}``。"""
    def make(table):
        def fn(conn, symbol, as_of):
            return table.get(symbol)
        return fn
    return {name: make(table) for name, table in values_by_factor.items()}


class RuleEngineTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.conn = store.connect(str(Path(self.home) / "t.sqlite"))
        store.migrate(self.conn)
        self.write_pool(SYMBOLS)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def write_pool(self, symbols, key="watchlist"):
        path = Path(self.home) / "trading-platform.json"
        config = {}
        if path.exists():
            config = json.loads(path.read_text(encoding="utf-8"))
        config[key] = list(symbols)
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def write_risk(self, **over):
        (Path(self.home) / "trading-risk.json").write_text(
            json.dumps(over, ensure_ascii=False), encoding="utf-8")

    def load(self, spec, values):
        return rule_engine.load_rule(spec, registry=_registry(values))


class ZScoreSelectionTest(RuleEngineTestBase):
    def test_top_n_selection_and_equal_weight(self):
        # f1/f2 同向：A>B>C>D>E 确定性降序 → top3 = A/B/C
        values = {"f1": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0])),
                  "f2": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0]))}
        strat = self.load(_spec(), values)
        weights = strat.target_weights(self.conn, "2026-09-14", home=self.home)
        self.assertEqual(sorted(weights), sorted(SYMBOLS[:3]))
        # 默认风控 cap=0.25、limit=5；base=1/3 → min(0.3333,0.25)=0.25
        self.assertTrue(all(v == 0.25 for v in weights.values()), weights)

    def test_missing_value_symbol_skipped(self):
        # C 的 f2 缺值 → 整只跳过（完整样本口径，宁缺毋假）→ top3 = A/B/D
        values = {"f1": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0])),
                  "f2": {"SH.600519": 5.0, "SH.600036": 4.0, "SH.601318": None,
                         "SH.601899": 2.0, "SH.600858": 1.0}}
        strat = self.load(_spec(), values)
        weights = strat.target_weights(self.conn, "2026-09-14", home=self.home)
        self.assertNotIn("SH.601318", weights)
        self.assertEqual(sorted(weights), sorted([SYMBOLS[0], SYMBOLS[1], SYMBOLS[3]]))

    def test_all_missing_returns_empty(self):
        values = {"f1": {s: None for s in SYMBOLS}, "f2": {s: None for s in SYMBOLS}}
        strat = self.load(_spec(), values)
        self.assertEqual(strat.target_weights(self.conn, "2026-09-14", home=self.home), {})

    def test_too_few_complete_samples_returns_empty(self):
        # 完整样本 <3：横截面 z 无定义 → 不产生目标（等现金）
        values = {"f1": {"SH.600519": 5.0, "SH.600036": 4.0},
                  "f2": {"SH.600519": 5.0, "SH.600036": 4.0}}
        strat = self.load(_spec(), values)
        self.assertEqual(strat.target_weights(self.conn, "2026-09-14", home=self.home), {})

    def test_weights_never_exceed_cap(self):
        values = {"f1": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0])),
                  "f2": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0]))}
        strat = self.load(_spec(top_n=5), values)
        self.write_risk(max_position_pct=0.1, max_positions=2)
        weights = strat.target_weights(self.conn, "2026-09-14", home=self.home)
        # top_n=5 全部入选 → base=1/5=0.2 → min(0.2,0.1)=0.1；limit=2 按打分顺序截断
        self.assertEqual(sorted(weights), sorted(SYMBOLS[:2]))
        self.assertTrue(all(abs(v - 0.1) < 1e-9 for v in weights.values()), weights)


class RiskCapSharedTest(RuleEngineTestBase):
    def test_shared_helper_is_single_implementation(self):
        # 同一实现：直接调 strategies.risk_capped_weights 与解释器结果一致
        self.assertTrue(hasattr(strategies, "risk_capped_weights"))
        # 5 只、cap=0.25 → base=1/5=0.2 < cap，取 0.2
        self.assertEqual(
            strategies.risk_capped_weights(SYMBOLS, home=self.home),
            {"SH.600519": 0.2, "SH.600036": 0.2, "SH.601318": 0.2,
             "SH.601899": 0.2, "SH.600858": 0.2})
        values = {"f1": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0])),
                  "f2": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0]))}
        strat = self.load(_spec(top_n=3), values)
        weights = strat.target_weights(self.conn, "2026-09-14", home=self.home)
        self.assertEqual(weights, strategies.risk_capped_weights(SYMBOLS[:3], home=self.home))

    def test_degenerate_config_yields_no_targets(self):
        self.write_risk(max_position_pct=0.0)
        self.assertEqual(strategies.risk_capped_weights(SYMBOLS, home=self.home), {})
        self.write_risk(max_positions=0)
        self.assertEqual(strategies.risk_capped_weights(SYMBOLS, home=self.home), {})

    def test_watchlist_rsi_still_uses_shared_cap(self):
        # 既有策略行为不回归：cap=0.1、limit=2 → 两只各 0.1
        self.write_risk(max_position_pct=0.1, max_positions=2)
        bars = [{"t": f"2026-08-{d:02d}", "o": 10, "h": 10.5, "l": 9.5,
                 "c": 10 + d * 0.1, "v": 100} for d in range(1, 29)]
        for symbol in SYMBOLS:
            store.upsert_bars(self.conn, symbol, "1d", bars, "test")
        weights = strategies.REGISTRY["watchlist_rsi"].target_weights(
            self.conn, "2026-08-28", home=self.home, market="SH")
        self.assertTrue(all(v <= 0.1 + 1e-9 for v in weights.values()), weights)
        self.assertLessEqual(len(weights), 2)


class RebalanceTest(RuleEngineTestBase):
    def _mutable(self):
        f1 = dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0]))
        f2 = dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0]))
        return {"f1": f1, "f2": f2}, (f1, f2)

    def test_weekly_reuses_within_same_iso_week(self):
        values, (f1, f2) = self._mutable()
        strat = self.load(_spec(rebalance="weekly"), values)
        first = strat.target_weights(self.conn, "2026-09-14", home=self.home)  # 周一
        # 同周改数据：沿用上次目标（不重算）
        self._reverse((f1, f2))
        second = strat.target_weights(self.conn, "2026-09-16", home=self.home)  # 同周三
        self.assertEqual(first, second)
        # 跨周 → 重算（数据已反转 → 入选集合反转）
        third = strat.target_weights(self.conn, "2026-09-21", home=self.home)  # 下周一
        self.assertNotEqual(first, third)

    def _reverse(self, tables):
        """把因子值反转（两个因子都反转，合成打分才真正翻向）。"""
        for table in tables:
            for i, s in enumerate(SYMBOLS):
                table[s] = float(i)

    def test_monthly_reuses_within_same_month(self):
        values, (f1, f2) = self._mutable()
        strat = self.load(_spec(rebalance="monthly"), values)
        first = strat.target_weights(self.conn, "2026-09-14", home=self.home)
        self._reverse((f1, f2))
        second = strat.target_weights(self.conn, "2026-09-30", home=self.home)
        self.assertEqual(first, second)
        third = strat.target_weights(self.conn, "2026-10-08", home=self.home)
        self.assertNotEqual(first, third)

    def test_daily_recomputes_every_day(self):
        values, (f1, f2) = self._mutable()
        strat = self.load(_spec(rebalance="daily"), values)
        first = strat.target_weights(self.conn, "2026-09-14", home=self.home)
        self._reverse((f1, f2))
        second = strat.target_weights(self.conn, "2026-09-15", home=self.home)
        self.assertNotEqual(first, second)


class ICLWeightedTest(RuleEngineTestBase):
    def _bars_for_forward_returns(self, symbols, days=50):
        bars = [{"t": f"2026-{1 + (i // 28):02d}-{1 + (i % 28):02d}", "o": 10, "h": 10,
                 "l": 9, "c": 10.0, "v": 100} for i in range(days)]
        for idx, symbol in enumerate(symbols):
            rows = [dict(bar, c=10.0 + i * (0.1 + idx * 0.05)) for i, bar in enumerate(bars)]
            store.upsert_bars(self.conn, symbol, "1d", rows, "test")
        return [bar["t"] for bar in bars]

    def test_requires_min_history(self):
        values = {"f1": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0])),
                  "f2": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0]))}
        strat = self.load(_spec(combine="ic_weighted"), values)
        with self.assertRaises(ValueError) as ctx:
            strat.target_weights(self.conn, "2026-09-14", home=self.home)
        self.assertIn("30 日历史", str(ctx.exception))

    def test_ic_weights_favour_predictive_factor(self):
        # 注入 30 日快照与固定前向收益：f1 与收益完全同序（IC=+1）、f2 反序（IC=-1）
        # → 两者 |IC| 相等 → 归一权重各 0.5（验证的是「按 |IC| 归一」而非方向）
        dates = [f"2026-09-{d:02d}" for d in range(1, 31)]
        forward = {s: 0.01 * (i + 1) for i, s in enumerate(SYMBOLS)}
        snapshots = [{"date": d, "payload": {"tickers": {
            s: {"f1": float(i), "f2": float(len(SYMBOLS) - i)} for i, s in enumerate(SYMBOLS)}}
        } for d in dates]
        weights = rule_engine._ic_factor_weights(
            self.conn, ["f1", "f2"], snapshots=lambda: snapshots,
            fwd_return=lambda conn, symbol, as_of, horizon, end: forward.get(symbol))
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)
        self.assertAlmostEqual(weights["f1"], 0.5, places=9)
        self.assertAlmostEqual(weights["f2"], 0.5, places=9)

    def test_ic_weighted_end_to_end_with_store_history(self):
        # 真实路径：30 日快照落库 + bars 供前向收益 → 可算出权重并产出目标。
        # 评估日必须晚于最新快照（前向收益要看得到未来 20 根，PIT 上限 = 评估日）。
        dates = self._bars_for_forward_returns(SYMBOLS[:3], days=60)
        for day in dates[:30]:
            payload = {"date": day, "tickers": {
                s: {"f1": float(len(SYMBOLS[:3]) - j), "f2": float(j)}
                for j, s in enumerate(SYMBOLS[:3])}}
            store.save_factor_snapshot(self.conn, day, payload)
        values = {"f1": {s: 1.0 for s in SYMBOLS[:3]}, "f2": {s: 1.0 for s in SYMBOLS[:3]}}
        strat = self.load(_spec(combine="ic_weighted", top_n=2), values)
        weights = strat.target_weights(self.conn, dates[49], home=self.home)
        self.assertTrue(weights, "应产出目标权重")
        self.assertLessEqual(len(weights), 2)
        self.assertLessEqual(sum(weights.values()), 1.0 + 1e-9)


class RegistrationTest(RuleEngineTestBase):
    def test_register_rule_is_consumable(self):
        values = {"f1": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0])),
                  "f2": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0]))}
        spec = _spec(rule_id="rule_reg_1")
        instance = strategies.register_rule(spec, registry=_registry(values))
        self.assertIs(strategies.REGISTRY["rule_reg_1"], instance)
        weights = strategies.REGISTRY["rule_reg_1"].target_weights(
            self.conn, "2026-09-14", home=self.home, market="SH")
        self.assertTrue(weights)
        # plan_auto 按签名注入 home/market —— 两个参数都必须存在且被接受
        params = inspect.signature(instance.target_weights).parameters
        self.assertIn("home", params)
        self.assertIn("market", params)

    def test_market_mismatch_is_fail_closed(self):
        values = {"f1": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0])),
                  "f2": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0]))}
        strat = self.load(_spec(), values)
        with self.assertRaises(ValueError) as ctx:
            strat.target_weights(self.conn, "2026-09-14", home=self.home, market="US")
        self.assertIn("市场", str(ctx.exception))

    def test_load_rule_revalidates(self):
        tampered = _spec(combine="magic_ml")
        with self.assertRaises(ValueError) as ctx:
            rule_engine.load_rule(tampered, registry=_registry({"f1": {}, "f2": {}}))
        self.assertIn("combine", str(ctx.exception))
        with self.assertRaises(ValueError):
            rule_engine.load_rule(_spec(factors=["ghost"]), registry=_registry({"f1": {}}))


class UniverseParseTest(RuleEngineTestBase):
    def test_market_slice_and_full_pool(self):
        values = {"f1": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0])),
                  "f2": dict(zip(SYMBOLS, [5.0, 4.0, 3.0, 2.0, 1.0]))}
        self.write_pool(["HK.00700", "US.AAPL"], key="pool2")
        strat = self.load(_spec(universe="watchlist"), values)
        self.assertEqual(strat.universe(self.conn, "2026-09-14", home=self.home), SYMBOLS)
        self.assertEqual(strat.universe(self.conn, "2026-09-14", home=self.home,
                                        market="HK"), [])
        strat2 = rule_engine.load_rule(_spec(rule_id="r2", universe="pool2.HK"),
                                       registry=_registry(values))
        self.assertEqual(strat2.universe(self.conn, "2026-09-14", home=self.home),
                         ["HK.00700"])

    def test_unknown_market_segment_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            rule_engine.load_rule(_spec(universe="watchlist.XX"),
                                  registry=_registry({"f1": {}, "f2": {}}))
        self.assertIn("市场", str(ctx.exception))

    def test_missing_custom_pool_rejected(self):
        strat = rule_engine.load_rule(_spec(universe="nope.SH"),
                                      registry=_registry({"f1": {}, "f2": {}}))
        with self.assertRaises(ValueError) as ctx:
            strat.universe(self.conn, "2026-09-14", home=self.home)
        self.assertIn("关注池", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
