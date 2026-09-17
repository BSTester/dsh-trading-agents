"""WP9 受管集合与退出路径（规格 §4.2 第 6 点）：自动流水线能减仓/清仓。

修复前的缺口：``planner.build_and_freeze`` 只遍历 ``target`` 的键，策略不再返回的
已持仓标的永远不生成 SELL —— **只买不退**。本文件覆盖：

  * ``build_and_freeze(managed=…)``：managed 缺席 + 券商持有 → 全额 SELL（清仓），
    不受风险预算约束；无持仓 → 无单；managed **之外**的持仓不动；``managed=None``
    保持既有 target-only 行为；产出顺序确定（target 原序在前）；
  * ``plan_auto``：managed = 该市场关注池 ∩ 策略 universe；universe 为空 → 退化为
    target-only 并在结果标注；清仓标的的价格只为**实际持有**者补取；
  * 反证：managed 缺席且无持仓 → 零订单（不制造空卖）。
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import daemon, planner, strategies, store  # noqa: E402

HELD = "SH.600519"
OTHER = "SH.601398"

TODAY = "2026-09-16"
PREV = "2026-09-15"


def bars_with_tr(conn, symbol, close, half_range, count=20, last=PREV):
    """铺 ``count`` 根日线（h=c+half_range / l=c−half_range / o=c）→ ATR = 2×half_range。"""
    end = date.fromisoformat(last)
    days = [(end - timedelta(days=count - 1 - i)).isoformat() for i in range(count)]
    store.upsert_bars(conn, symbol, "1d", [
        {"t": d, "o": close, "h": close + half_range, "l": close - half_range,
         "c": close, "v": 1000.0} for d in days], source="test")


class PlannerManagedTest(unittest.TestCase):
    """计划层：managed ∪ target 求 diff，managed 缺席的已持仓标的清仓。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        self.addCleanup(self.conn.close)

    def _freeze(self, target, managed=None, positions=None, prices=None,
                equity=1_000_000.0):
        return planner.build_and_freeze(
            self.conn, mode="sim", strategy_id="s", target=target,
            broker_positions=lambda _mode: (positions or {}, equity),
            prices=prices if prices is not None else {HELD: 200.0},
            as_of=TODAY, origin="auto", market="SH", managed=managed)

    def test_absent_managed_holding_is_fully_exited(self):
        """managed 缺席 + 券商持有 1700 股 → 全额 SELL 1700（清仓）。

        名义 340,000 = 权益 34%（远超 max_position_pct 25%）仍然全额卖出：清仓是
        **减少敞口**，不受单笔风险预算约束（与减仓同口径）。修复前该标的根本不进 diff，
        持仓会永远留在账上。
        """
        plan = self._freeze({}, managed=[HELD], positions={HELD: {"qty": 1700}})
        self.assertEqual([(o["side"], o["qty"], o["price"]) for o in plan["orders"]],
                         [("SELL", 1700, 200.0)], plan)

    def test_absent_managed_without_holding_makes_no_order(self):
        """managed 缺席但券商无持仓 → 零订单（不制造空卖）。"""
        plan = self._freeze({}, managed=[HELD], positions={})
        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(store.get_orders_by_plan(self.conn, plan["plan_id"]), [])

    def test_managed_none_keeps_target_only_behavior(self):
        """managed=None（缺省）＝只遍历 target 的既有语义：缺席标的**不**产生单。

        这条同时是对缺口的文档化反证：没有受管集合时已持仓标的不会退出。
        """
        plan = self._freeze({}, managed=None, positions={HELD: {"qty": 1700}})
        self.assertEqual(plan["orders"], [])

    def test_holdings_outside_managed_are_untouched(self):
        """受管集合之外的持仓一律不动（不清理用户手工持有的标的）。"""
        plan = self._freeze({}, managed=[HELD],
                            positions={HELD: {"qty": 300}, OTHER: {"qty": 900}},
                            prices={HELD: 200.0, OTHER: 5.0})
        self.assertEqual([o["symbol"] for o in plan["orders"]], [HELD])
        self.assertEqual([o["side"] for o in plan["orders"]], ["SELL"])

    def test_target_orders_precede_exit_orders(self):
        """产出顺序确定：target 原序在前，managed 缺席者按代码升序在后。

        确定性是 content_hash（计划指纹）可比、审计可复核的前提。
        """
        plan = self._freeze({OTHER: 0.1}, managed=[HELD],
                            positions={OTHER: {"qty": 9000}, HELD: {"qty": 300}},
                            prices={HELD: 200.0, OTHER: 5.0})
        # OTHER 目标 1e6×0.1÷5 = 20000 → 持有 9000 → 加仓（需 ATR，无 bar → 跳过）；
        # HELD 缺席 → 清仓 300。顺序上 OTHER（target）先于 HELD（managed 缺席）。
        self.assertEqual([o["symbol"] for o in plan["orders"]], [HELD])
        self.assertIn(f"{OTHER}(无ATR)", plan["skipped"])

    def test_exit_reported_in_skipped_is_empty_normally(self):
        """正常清仓不产生 skipped 噪音（skipped 只承载「本可下单但被跳过」）。"""
        plan = self._freeze({}, managed=[HELD], positions={HELD: {"qty": 100}})
        self.assertEqual(plan["skipped"], [])


class _TwinStrategy:
    """plan_auto 测试替身：universe 与 target_weights 可控（模拟组合/单标的策略）。"""

    id = "wp9_twin"

    def __init__(self, universe_symbols, weights):
        self._universe = list(universe_symbols)
        self._weights = dict(weights)

    def universe(self, conn, as_of, home=None):
        return list(self._universe)

    def target_weights(self, conn, as_of, home=None):
        return dict(self._weights)


class PlanAutoManagedTest(unittest.TestCase):
    """plan_auto：受管集合计算 ← 关注池 ∩ 策略 universe，结果如实标注。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        store.upsert_calendar(self.conn, "SH", [
            {"day": day, "trade_date_type": "WHOLE", "trade_second": 14400}
            for day in (PREV, TODAY)])
        bars_with_tr(self.conn, HELD, 200.0, 0.25, last=TODAY)
        bars_with_tr(self.conn, OTHER, 5.0, 0.25, last=TODAY)
        self._config_written = False

    def _config(self, watchlist, strategy):
        (self.home / "trading-platform.json").write_text(json.dumps({
            "watchlist": watchlist,
            "auto_pipeline": {
                "enabled": True,
                "strategies": [{"market": "SH", "strategy": strategy,
                                "watchlist": "SH"}],
                "exec_at": {"SH": "09:35"}, "reconcile_at": "19:00"}},
            ensure_ascii=False), encoding="utf-8")

    def _broker(self, positions):
        """sim 假通道。资金响应**必须带现金字段**（``balance``/``max_power_long``）：
        2026-09-17 现金封顶修订后「现金不可得 → 不生成买单」，只给权益的假件会把买入
        用例打成零订单——那是假件不完备，不是语义回归（口径见
        ``tests/test_wp17_cash_cap.py``）。
        """
        def call(name, args=None, timeout=30, **kwargs):
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3}]}
            if name == "sim_trade_position_list":
                return {"positions": [{"symbol": s, "qty": v["qty"]}
                                      for s, v in positions.items()]}
            if name == "sim_trade_cash_info":
                return {"total_asset": 1_000_000.0, "balance": "1000000.0",
                        "max_power_long": "1000000.0"}
            raise AssertionError(f"意外券商工具 {name}")
        return call

    def _run(self, strategy_id, universe, weights, watchlist, positions):
        strategies.REGISTRY[strategy_id] = _TwinStrategy(universe, weights)
        self.addCleanup(strategies.REGISTRY.pop, strategy_id, None)
        self._config(watchlist, strategy_id)
        return planner.plan_auto(self.conn, str(self.home), "SH", today=TODAY,
                                 broker_call=self._broker(positions))

    def test_dropped_holding_is_exited_and_managed_counted(self):
        """策略不再给权重的已持仓标的 → 清仓；结果标注受管标的数。"""
        out = self._run("wp9_twin", [HELD], {}, [HELD], {HELD: {"qty": 700}})
        self.assertTrue(out["ok"], out)
        orders = out["plan"]["orders"]
        self.assertEqual([(o["side"], o["qty"]) for o in orders], [("SELL", 700)], orders)
        self.assertEqual(out["managed"], 1)

    def test_universe_outside_watchlist_not_managed(self):
        """managed = 关注池 ∩ universe：universe 里的关注池外标的**不**进受管集合。"""
        out = self._run("wp9_twin", [HELD, OTHER], {}, [HELD], {OTHER: {"qty": 900}})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["plan"]["orders"], [])       # OTHER 不受管 → 不动
        self.assertEqual(out["managed"], 1)

    def test_empty_universe_falls_back_to_target_only(self):
        """universe 为空（单标的策略）→ managed=None，行为与今天一致并如实标注。"""
        # 权重 2% → 1e6×0.02÷200 = 100 股 = 1 手（1% 会不足一手，测不出买入路径）
        out = self._run("wp9_twin", [], {HELD: 0.02}, [HELD], {OTHER: {"qty": 900}})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["managed"], "target-only")
        self.assertEqual([o["side"] for o in out["plan"]["orders"]], ["BUY"])

    def test_exit_order_is_registered_in_oms(self):
        """清仓单与买入单同样「冻结即登记」进 OMS——否则 execute.run 取不到单。

        注：``no_price`` 分支（清仓标的取不到价格）在当前链路不可达——数据就绪门要求
        关注池每个标的的最后一根 bar 等于最近交易日，而价格读的是同一批 bar；该分支
        是防御性的，本文件不为它编造不可达的测试。
        """
        out = self._run("wp9_twin", [HELD], {}, [HELD], {HELD: {"qty": 900}})
        self.assertTrue(out["ok"], out)
        rows = store.get_orders_by_plan(self.conn, out["plan"]["plan_id"])
        self.assertEqual([(r["symbol"], r["side"], r["qty"], r["status"])
                          for r in rows], [(HELD, "SELL", 900, "draft")], rows)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
