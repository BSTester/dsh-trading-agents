"""WP9 结构性缺口修复的专项测试：定量与风控规则 4/5 自洽（8 条硬规则不动）。

规格 §4.2 第 5 点（2026-09-16 修订）要求的四件事，本文件逐条覆盖：

  * 指标层：``indicators.atr/stop_distance`` 的数学与「宁缺毋假」边界（bar 不足、
    ATR 为 0、倍数非正 → None），以及 PIT 过滤（as_of 之后的 bar 必须看不见）；
  * 策略层：``watchlist_rsi`` 的单票权重上限（max_position_pct）与标的数上限
    （max_positions），确定性截断、向下取整、自定义风控配置生效；
  * 计划层：``planner.build_and_freeze`` 的风险预算定量（min(权重臂, 预算臂)）、
    ATR 缺失/预算不足一手 → 跳过并在结果列出、减仓不受预算限制、**已持仓不因预算
    被误卖**（上限只约束增量）；
  * 反证：硬规则未被削弱——超限订单仍被规则 4 拦下（端到端反证在 test_wp9_e2e）。
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import indicators, planner, strategies, store  # noqa: E402

SYMBOL = "SH.600519"


def bars_with_tr(conn, symbol, close, half_range, count=20, last="2026-08-31"):
    """铺 ``count`` 根日线：h=c+half_range / l=c−half_range / o=c。

    相邻收盘相同 → TR = 2×half_range（首项 h−l 最大）→ ATR = 2×half_range。
    """
    end = date.fromisoformat(last)
    days = [(end - timedelta(days=count - 1 - i)).isoformat() for i in range(count)]
    store.upsert_bars(conn, symbol, "1d", [
        {"t": d, "o": close, "h": close + half_range, "l": close - half_range,
         "c": close, "v": 1000.0} for d in days], source="test")


class IndicatorsTest(unittest.TestCase):
    """指标层：ATR 数学 + 宁缺毋假边界 + PIT。"""

    def setUp(self):
        self.conn = store.connect(":memory:")
        store.migrate(self.conn)
        self.addCleanup(self.conn.close)

    def test_true_ranges_and_atr_math(self):
        bars = [{"t": f"2026-08-{d:02d}", "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.0, "v": 1.0}
                for d in range(1, 16)]
        # 每根 TR = max(2.0, 0, 0) = 2.0；15 根 → 14 个 TR
        self.assertEqual(len(indicators.true_ranges(bars)), 14)
        self.assertAlmostEqual(indicators.atr(bars), 2.0)

    def test_atr_gap_true_range_uses_prev_close(self):
        """跳空：TR 取 |high−prev_close| / |low−prev_close| 的最大者。"""
        bars = [{"t": "2026-08-01", "o": 10.0, "h": 10.0, "l": 10.0, "c": 10.0, "v": 1.0},
                {"t": "2026-08-02", "o": 20.0, "h": 20.0, "l": 20.0, "c": 20.0, "v": 1.0}]
        self.assertEqual(indicators.true_ranges(bars), [10.0])

    def test_atr_none_when_bars_insufficient(self):
        """不足 period+1 根 → None（宁可算不出，不可用截断样本冒充）。"""
        bars = [{"t": f"2026-08-{d:02d}", "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.0, "v": 1.0}
                for d in range(1, 15)]  # 14 根 < 15
        self.assertIsNone(indicators.atr(bars))
        self.assertIsNone(indicators.atr([]))

    def test_atr_none_when_zero(self):
        """ATR=0 视为不可用：0 止损会让规则 4 的风险额恒为 0（静默废除硬规则）。"""
        bars = [{"t": f"2026-08-{d:02d}", "o": 10.0, "h": 10.0, "l": 10.0, "c": 10.0, "v": 1.0}
                for d in range(1, 16)]
        self.assertIsNone(indicators.atr(bars))

    def test_atr_period_must_be_positive(self):
        with self.assertRaises(ValueError):
            indicators.atr([{"t": "d", "o": 1, "h": 1, "l": 1, "c": 1}], period=0)

    def test_stop_distance_is_pit_and_scaled(self):
        bars_with_tr(self.conn, SYMBOL, 100.0, 1.0)  # 20 根，末日 2026-08-31
        # ATR = 2.0 → 止损距离 = 2.0 × 2.0 = 4.0
        self.assertAlmostEqual(
            indicators.stop_distance(self.conn, SYMBOL, "2026-08-31", 2.0), 4.0)
        # PIT：as_of 只留 11 根（< 15）→ 算不出 → None
        self.assertIsNone(indicators.stop_distance(self.conn, SYMBOL, "2026-08-20", 2.0))

    def test_stop_distance_invalid_multiplier(self):
        bars_with_tr(self.conn, SYMBOL, 100.0, 1.0)
        self.assertIsNone(indicators.stop_distance(self.conn, SYMBOL, "2026-08-31", 0.0))
        self.assertIsNone(indicators.stop_distance(self.conn, SYMBOL, "2026-08-31", None))


class StrategyCapTest(unittest.TestCase):
    """策略层：watchlist_rsi 尊重 max_position_pct / max_positions（规格 §4.2 第 5 点）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        self.strategy = strategies.REGISTRY["watchlist_rsi"]

    def _watchlist(self, symbols):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"watchlist": symbols}), encoding="utf-8")

    def _risk(self, **overlay):
        (self.home / "trading-risk.json").write_text(
            json.dumps(overlay), encoding="utf-8")

    def _weights(self, symbols, buys):
        self._watchlist(symbols)
        with mock.patch.object(type(self.strategy), "signal",
                               lambda self, conn, symbol, as_of: "BUY" if symbol in buys
                               else "HOLD"):
            return self.strategy.target_weights(self.conn, "2026-09-13", home=str(self.home))

    def test_two_buys_capped_at_max_position_pct(self):
        """N=2 → 等权基数 0.5 被单票上限 0.25 压住（否则规则 5 必拒）。"""
        weights = self._weights(["SH.600519", "SZ.300750"], {"SH.600519", "SZ.300750"})
        self.assertEqual(weights, {"SH.600519": 0.25, "SZ.300750": 0.25})

    def test_ten_buys_equal_weight_below_cap(self):
        """N=10 → 1/10 = 0.1 < 0.25：等权基数生效（不被上限改写），仍受 max_positions=5 截断。"""
        symbols = [f"SH.60000{i}" for i in range(10)]
        weights = self._weights(symbols, set(symbols))
        self.assertEqual(list(weights), sorted(symbols)[:5])
        self.assertTrue(all(w == 0.1 for w in weights.values()), weights)
        self.assertLessEqual(sum(weights.values()), 1.0 + 1e-9)

    def test_max_positions_truncates_deterministically(self):
        """N=8 且 max_positions=5 → 按标的代码升序取前 5，每只 1/8 = 0.125。"""
        symbols = [f"SH.60000{i}" for i in range(8)]
        weights = self._weights(symbols, set(symbols))
        self.assertEqual(list(weights), sorted(symbols)[:5])
        self.assertTrue(all(w == 0.125 for w in weights.values()), weights)

    def test_custom_risk_config_applies(self):
        """trading-risk.json 覆盖生效（max_position_pct=0.4 / max_positions=2）。"""
        self._risk(max_position_pct=0.4, max_positions=2)
        symbols = ["SH.600519", "SZ.300750", "SH.601899"]
        weights = self._weights(symbols, set(symbols))
        # 1/3 = 0.3333… < 0.4 → 基数生效；截断到 2 只（升序前二）
        self.assertEqual(list(weights), sorted(symbols)[:2])
        self.assertTrue(all(w == 0.3333 for w in weights.values()), weights)

    def test_floor_rounding_never_exceeds_cap(self):
        """向下取整：权重不会因四舍五入略超 max_position_pct（规则 5 严格大于判定）。"""
        self._risk(max_position_pct=0.12345)
        weights = self._weights(["SH.600519", "SZ.300750"], {"SH.600519", "SZ.300750"})
        self.assertTrue(all(w <= 0.12345 for w in weights.values()), weights)
        self.assertEqual(weights["SH.600519"], 0.1234)

    def test_invalid_risk_config_raises(self):
        """配置非法如实上抛（由作业入口 fail-closed 处理，不静默回退默认值）。"""
        self._risk(nope=1)
        with self.assertRaises(ValueError):
            self._weights(["SH.600519"], {"SH.600519"})

    def test_degenerate_caps_produce_no_targets(self):
        """退化配置 fail-safe：cap≤0 不得产出负权重；limit≤0 不得因切片语义产出意外目标。"""
        for overlay in ({"max_position_pct": 0.0}, {"max_position_pct": -0.5},
                        {"max_positions": 0}, {"max_positions": -1}):
            with self.subTest(overlay=overlay):
                self._risk(**overlay)
                self.assertEqual(
                    self._weights(["SH.600519", "SZ.300750"],
                                  {"SH.600519", "SZ.300750"}), {})


class PlannerRiskBudgetTest(unittest.TestCase):
    """计划层：按风险预算定量（权重降级为上限）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        self.addCleanup(self.conn.close)

    def _freeze(self, target, price=80.0, equity=1_000_000.0, positions=None,
                risk_config=None, as_of="2026-09-13"):
        return planner.build_and_freeze(
            self.conn, mode="sim", strategy_id="s", target=target,
            broker_positions=lambda _mode: (positions or {}, equity),
            prices={SYMBOL: price}, as_of=as_of,
            **({"risk_config": risk_config} if risk_config else {}))

    def test_risk_budget_caps_weight_sizing(self):
        """规格 §4.2 第 5 点的原始算例：止损距离 5%（价 80 → 4.0）→ 2500 股。

        权重定量 = 1e6 × 0.25 ÷ 80 = 3125 → 整手 3100；预算 = floor(1e4 ÷ 4.0 ÷ 100)
        × 100 = 2500 → 取 2500（预算臂约束）。
        """
        bars_with_tr(self.conn, SYMBOL, 80.0, 1.0)  # ATR = 2.0 → 止损距离 = 4.0 = 价的 5%
        plan = self._freeze({SYMBOL: 0.25})
        self.assertEqual(len(plan["orders"]), 1, plan)
        self.assertEqual(plan["orders"][0]["qty"], 2500)
        self.assertEqual(plan["orders"][0]["side"], "BUY")

    def test_weight_arm_binds_when_stop_is_tiny(self):
        """止损距离极小 → 预算臂远大于权重臂 → 权重定量生效（min 两臂都可达）。"""
        bars_with_tr(self.conn, SYMBOL, 80.0, 0.01)  # ATR = 0.02 → 止损距离 = 0.04
        plan = self._freeze({SYMBOL: 0.25})
        self.assertEqual(plan["orders"][0]["qty"], 3100)  # 1e6 × 0.25 ÷ 80 → 整手 3100

    def test_missing_atr_skips_symbol_and_reports(self):
        """无 bar → 算不出 ATR → 跳过并在 skipped 列出（不生成必被规则 4 拦的徒劳单）。"""
        plan = self._freeze({SYMBOL: 0.25})
        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["skipped"], [f"{SYMBOL}(无ATR)"])
        self.assertEqual(store.get_orders_by_plan(self.conn, plan["plan_id"]), [])

    def test_budget_below_one_lot_skips_symbol(self):
        """预算连一手都买不起 → 跳过并列出（不生成 0 股单、不静默）。"""
        bars_with_tr(self.conn, SYMBOL, 80.0, 50.0)  # ATR = 100 → 止损距离 200
        plan = self._freeze({SYMBOL: 0.25})
        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["skipped"], [f"{SYMBOL}(风险预算不足一手)"])

    def test_increase_is_capped_not_target(self):
        """已持仓低于目标：加仓量取 min(增量, 预算)——目标未达即「权重降级为上限」。"""
        bars_with_tr(self.conn, SYMBOL, 80.0, 1.0)  # 预算 2500
        plan = self._freeze({SYMBOL: 0.4}, positions={SYMBOL: {"qty": 1000}})
        # 权重定量 = 1e6 × 0.4 ÷ 80 = 5000 → 增量 4000 → min(4000, 2500) = 2500
        self.assertEqual([(o["side"], o["qty"]) for o in plan["orders"]], [("BUY", 2500)])

    def test_held_position_above_budget_is_not_force_sold(self):
        """**上限只约束增量**：持仓已达目标时预算不得把目标压低成非预期卖出。

        直译 `qty = min(权重定量, 预算)` 会把目标 5000 压到 2500 → 凭空卖 2500 股。
        """
        bars_with_tr(self.conn, SYMBOL, 80.0, 1.0)  # 预算 2500 < 目标 5000
        plan = self._freeze({SYMBOL: 0.4}, positions={SYMBOL: {"qty": 5000}})
        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["skipped"], [])

    def test_reduce_is_not_capped_by_budget(self):
        """减仓不受风险预算限制（减少敞口不是新增风险）。"""
        bars_with_tr(self.conn, SYMBOL, 80.0, 1.0)  # 预算 2500
        plan = self._freeze({SYMBOL: 0.1}, positions={SYMBOL: {"qty": 5000}})
        # 目标 = int(1e6 × 0.1 ÷ 80 // 100 × 100) = 1200 → 减仓 3800（> 预算，照常执行）
        self.assertEqual([(o["side"], o["qty"]) for o in plan["orders"]], [("SELL", 3800)])

    def test_risk_config_override_applies(self):
        """risk_config 覆盖生效：放宽 risk_per_trade 后预算臂不再约束。"""
        bars_with_tr(self.conn, SYMBOL, 80.0, 1.0)  # 默认预算 2500
        plan = self._freeze({SYMBOL: 0.25},
                            risk_config={"risk_per_trade": 0.5})
        self.assertEqual(plan["orders"][0]["qty"], 3100)  # 预算臂 → 权重臂

    def test_partial_risk_config_merges_defaults(self):
        """部分字段覆盖按「缺省补默认」合并（不重写配置读取实现）。"""
        bars_with_tr(self.conn, SYMBOL, 80.0, 1.0)
        plan = self._freeze({SYMBOL: 0.25}, risk_config={"max_positions": 3})
        self.assertEqual(plan["orders"][0]["qty"], 2500)  # stop_atr_mult 仍取默认 2.0


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
