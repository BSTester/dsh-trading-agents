"""目标外持仓自动清出（规格 §4.2 第 6 点修订，2026-09-17）：账户向策略组合收敛。

背景（实机）：A 股模拟账户持 8 只，其中数只在**关注池之外**——``managed`` 机制刻意不清理
受管集合之外的持仓（不碰用户手工持仓），而 ``max_positions=5`` 又让策略既不买（规则 6 拦）
也不卖（持仓全在 managed 之外）→ **结构性死锁**。本修订加一个**默认关闭**的开关
``exit_outside_target``：开启后自动计划把「券商持仓 − 当日策略 target 键」视为目标权重 0
（清出），让账户收敛到策略组合。

钉住的口径（改动前必读）：

  * **开关**（``~/.dsh/trading-platform.json`` **顶层**键）：缺失=false（不改变任何既有
    行为）；存在则必须是真布尔，字符串/数字/None 一律 ``ValueError``（fail-closed）；
    ``plan_auto`` 软跳过 + 告警（作业契约「永不抛」）；
  * **只在自动路径生效**：``plan_auto`` 应用；``plan-build`` 手工路径不进这段逻辑；
  * **收敛集合** = 券商持仓 − 当日 target 键；**硬守卫：target 为空一律不收敛**（否则
    「今日选不出标的」会变成「清空全部持仓」）+ 告警；
  * **价格回退**：本地最近收盘优先 → 券商持仓**标记价**（sim ``cur_price`` / live
    ``nominal_price``）→ 两处都拿不到则记 ``skipped`` 并告警（**不猜价、不生成假单**）；
    来源如实标注（``close`` / ``broker_mark``），绝不把券商标记价伪装成本地收盘价；
  * **可卖数量**：券商给了可用数量（sim ``qty_avbl`` / live ``can_sell_qty``）→ 卖
    ``min(qty, available)``；``available=0`` → ``skipped``，**不生成注定被拒的单**；
    字段缺失 → 按既有口径用全部 qty；
  * **形状兼容**：``broker.positions_equity_cash(..., with_marks=False)`` 缺省仍是
    ``{symbol: {"qty": n}}``（153 处既有断言/消费方逐字不变）；
    ``build_and_freeze(exit_symbols=None)`` 缺省时返回体**无** ``exits`` 键、diff 行为
    逐字不变。
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import (broker as core_broker, pipeline, planner,  # noqa: E402
                          strategies, store)

TODAY = "2026-09-16"
PREV = "2026-09-15"
IN_TARGET = "SH.600519"   # 当日策略 target 里的标的（关注池内，有本地 K 线）
OUT_A = "SH.601179"       # 目标外持仓 A
OUT_B = "SH.601899"       # 目标外持仓 B


def seed_bars(conn, symbol, close=5.0, count=20, last=TODAY):
    """铺确定性日线：h=c+0.5 / l=c−0.5 / o=c → ATR(14)=1.0 → 止损距离=2.0。"""
    end = date.fromisoformat(last)
    days = [(end - timedelta(days=count - 1 - i)).isoformat() for i in range(count)]
    store.upsert_bars(conn, symbol, "1d", [
        {"t": d, "o": close, "h": close + 0.5, "l": close - 0.5, "c": close,
         "v": 1000.0} for d in days], source="test")


# ============================================================================
# ① 开关读取（~/.dsh/trading-platform.json 顶层键，fail-closed）
# ============================================================================
class ExitSwitchConfigTest(unittest.TestCase):
    """缺失=false；真布尔才被接受；其余一律 ValueError（宁可不跑，不静默降级）。"""

    def test_missing_key_is_false(self):
        self.assertFalse(planner.exit_outside_target_enabled({}))

    def test_other_top_level_keys_do_not_enable(self):
        """与 futu_channel/watchlist/sentiment_budget_seconds 同级：别的键不误开本功能。"""
        self.assertFalse(planner.exit_outside_target_enabled(
            {"futu_channel": "openapi", "watchlist": [IN_TARGET]}))

    def test_explicit_false_is_false(self):
        self.assertFalse(planner.exit_outside_target_enabled(
            {planner.EXIT_OUTSIDE_TARGET_CONFIG_KEY: False}))

    def test_true_is_true(self):
        self.assertTrue(planner.exit_outside_target_enabled(
            {planner.EXIT_OUTSIDE_TARGET_CONFIG_KEY: True}))

    def test_non_boolean_values_are_rejected(self):
        """字符串/数字/None/容器都不是「真正的布尔」——静默当 False 会掩盖写错的配置。"""
        for bad in ("true", "false", "1", 1, 0, None, [], {}, "True"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                planner.exit_outside_target_enabled(
                    {planner.EXIT_OUTSIDE_TARGET_CONFIG_KEY: bad})

    def test_config_key_name_is_the_published_one(self):
        """键名是跨文件的字面量契约（README/RUNBOOK/设计文档都写它）——锁住防漂移。"""
        self.assertEqual(planner.EXIT_OUTSIDE_TARGET_CONFIG_KEY, "exit_outside_target")


# ============================================================================
# ② 券商持仓标记价/可用数量（broker.positions_equity_cash 的可选开关）
# ============================================================================
class BrokerMarksSnapshotTest(unittest.TestCase):
    """``with_marks=False`` 缺省形状逐字不变；True 才附带 price/mark_field/available。"""

    def _sim(self, rows):
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append(name)
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3}]}
            if name == "sim_trade_position_list":
                return {"positions": rows}
            if name == "sim_trade_cash_info":
                return {"total_asset": 812_231.816, "balance": "55257.816"}
            raise AssertionError(f"意外工具 {name}")

        return call, calls

    def _live(self, rows):
        def call(name, args=None, timeout=30, **kwargs):
            if name == "account_authorized_trd_accs":
                return {"accounts": [{"account_id": "L1", "enable_market": [4]}]}
            if name == "account_positions":
                return {"positions": rows}
            if name == "account_funds":
                return {"total_assets": "1000000.00", "power": "100000.00"}
            raise AssertionError(f"意外工具 {name}")

        return call

    def test_default_shape_is_qty_only(self):
        """缺省（既有调用方/测试）：字典形状**逐字**是 ``{"qty": n}``。"""
        call, calls = self._sim([{"symbol": "SH.601179", "qty": 2000,
                                  "cur_price": 12.5, "qty_avbl": 1500}])
        positions, equity, _cash = core_broker.positions_equity_cash(call, "sim", "SH")

        self.assertEqual(positions, {"SH.601179": {"qty": 2000}})
        self.assertEqual(equity, 812_231.816)
        self.assertEqual(calls.count("sim_trade_account_list"), 1)  # 仍只查一次账户列表

    def test_with_marks_exposes_sim_price_and_available(self):
        """sim：``cur_price`` → price、``qty_avbl`` → available（workbench 同字段口径）。"""
        call, _ = self._sim([{"symbol": "SH.601179", "qty": 2000, "cur_price": "12.5",
                              "qty_avbl": "1500"}])
        positions, _equity, _cash = core_broker.positions_equity_cash(
            call, "sim", "SH", with_marks=True)

        self.assertEqual(positions, {"SH.601179": {"qty": 2000, "price": 12.5,
                                                   "mark_field": "cur_price",
                                                   "available": 1500}})

    def test_missing_mark_fields_are_none_not_guessed(self):
        """字段缺失 → None（**不猜**）：price None = 无标记价；available None = 不限制。"""
        call, _ = self._sim([{"symbol": "SH.601179", "qty": 2000}])
        positions, _equity, _cash = core_broker.positions_equity_cash(
            call, "sim", "SH", with_marks=True)

        self.assertEqual(positions, {"SH.601179": {"qty": 2000, "price": None,
                                                   "mark_field": None, "available": None}})

    def test_zero_price_is_treated_as_missing(self):
        """价格 0 不是有效标记价（拿它下单 = 生成不可能成交的单）→ None。"""
        call, _ = self._sim([{"symbol": "SH.601179", "qty": 2000, "cur_price": "0"}])
        positions, _equity, _cash = core_broker.positions_equity_cash(
            call, "sim", "SH", with_marks=True)

        self.assertIsNone(positions["SH.601179"]["price"])

    def test_available_alias_is_accepted(self):
        """设计文稿写的 ``available`` 并非实测键名（REST/MCP 都是 ``qty_avbl``）——
        两者都接受：主候选是实测字段，别名作为次级候选，避免因键名分歧静默失效。"""
        call, _ = self._sim([{"symbol": "SH.601179", "qty": 2000, "available": "60"}])
        positions, _equity, _cash = core_broker.positions_equity_cash(
            call, "sim", "SH", with_marks=True)

        self.assertEqual(positions["SH.601179"]["available"], 60)

    def test_qty_avbl_wins_over_alias(self):
        call, _ = self._sim([{"symbol": "SH.601179", "qty": 2000, "qty_avbl": "5",
                              "available": "60"}])
        positions, _equity, _cash = core_broker.positions_equity_cash(
            call, "sim", "SH", with_marks=True)

        self.assertEqual(positions["SH.601179"]["available"], 5)

    def test_live_marks_use_nominal_price_and_can_sell_qty(self):
        """live：``nominal_price`` → price、``can_sell_qty`` → available；跨市场仍被过滤。"""
        call = self._live([{"code": "600519", "qty": 400, "nominal_price": "1800.5",
                            "can_sell_qty": 300},
                           {"code": "HK.00700", "qty": 5, "nominal_price": "400.0"}])
        positions, _equity, _cash = core_broker.positions_equity_cash(
            call, "live", "SH", with_marks=True)

        self.assertEqual(positions, {"SH.600519": {"qty": 400, "price": 1800.5,
                                                   "mark_field": "nominal_price",
                                                   "available": 300}})

    def test_with_marks_keeps_two_tuple_wrapper_untouched(self):
        """既有二元封装 ``positions_and_equity`` 不带标记（调用方零改动）。"""
        call, _ = self._sim([{"symbol": "SH.601179", "qty": 2000, "cur_price": 12.5}])
        positions, equity = core_broker.positions_and_equity(call, "sim", "SH")

        self.assertEqual(positions, {"SH.601179": {"qty": 2000}})
        self.assertEqual(equity, 812_231.816)


# ============================================================================
# ③ build_and_freeze(exit_symbols=...)：显式清出集合
# ============================================================================
class BuildAndFreezeExitSymbolsTest(unittest.TestCase):
    """缺省逐字不变；传入 exit_symbols 时把「目标外持仓」按目标权重 0 处理并如实回报。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        self.addCleanup(self.conn.close)

    def _freeze(self, target, prices=None, positions=None, managed=None,
                exit_symbols=None, equity=1_000_000.0):
        return planner.build_and_freeze(
            self.conn, mode="sim", strategy_id="s", target=target,
            broker_positions=lambda _mode: (positions or {}, equity),
            prices=prices if prices is not None else {OUT_A: 10.0},
            as_of=TODAY, origin="auto", market="SH", managed=managed,
            exit_symbols=exit_symbols)

    def test_default_keeps_legacy_result_and_diff(self):
        """``exit_symbols=None``：返回体无 exits 键、行为与既有 managed-only 逐字一致。"""
        legacy = self._freeze({}, managed=None, positions={OUT_A: {"qty": 300}})
        managed = self._freeze({}, managed=[OUT_A], positions={OUT_A: {"qty": 300}})

        self.assertNotIn("exits", legacy)
        self.assertEqual(legacy["orders"], [])                       # 不清理手工持仓
        self.assertEqual([(o["side"], o["qty"]) for o in managed["orders"]],
                         [("SELL", 300)])                            # managed 路径不变

    def test_exit_symbols_clear_holdings_outside_managed(self):
        """本修订要消除的缺口：managed 之外的持仓，只有 exit_symbols 能让他们进 diff。"""
        plan = self._freeze({}, positions={OUT_A: {"qty": 300}}, exit_symbols=[OUT_A])

        self.assertEqual([(o["symbol"], o["side"], o["qty"], o["price"])
                          for o in plan["orders"]], [(OUT_A, "SELL", 300, 10.0)])
        self.assertEqual(plan["exits"], [OUT_A])

    def test_exit_order_is_registered_in_oms(self):
        plan = self._freeze({}, positions={OUT_A: {"qty": 300}}, exit_symbols=[OUT_A])
        rows = store.get_orders_by_plan(self.conn, plan["plan_id"])

        self.assertEqual([(r["symbol"], r["side"], r["qty"], r["status"]) for r in rows],
                         [(OUT_A, "SELL", 300, "draft")])

    def test_exits_are_sorted_and_deduped_with_managed(self):
        """确定性与去重：exits 排序回报；与 managed 重叠只算一次。"""
        plan = self._freeze({}, managed=[OUT_B], prices={OUT_A: 10.0, OUT_B: 20.0},
                            positions={OUT_B: {"qty": 100}, OUT_A: {"qty": 50}},
                            exit_symbols=[OUT_B, OUT_A])

        self.assertEqual(plan["exits"], [OUT_A, OUT_B])
        self.assertEqual([(o["symbol"], o["qty"]) for o in plan["orders"]],
                         [(OUT_A, 50), (OUT_B, 100)])

    def test_exit_without_holding_makes_no_order(self):
        plan = self._freeze({}, positions={}, exit_symbols=[OUT_A])

        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(plan["exits"], [OUT_A])

    def test_exit_without_price_is_skipped_not_guessed(self):
        """两处都无价 → 记 skipped（不猜价、不生成假单）。"""
        plan = self._freeze({}, prices={}, positions={OUT_A: {"qty": 300}},
                            exit_symbols=[OUT_A])

        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["skipped"], [f"{OUT_A}(无价,无法清出)"])

    def test_missing_price_for_target_symbol_stays_silent(self):
        """回归锁：无价分支只对**清出**标的记 skipped，既有 target 路径逐字不变。"""
        plan = self._freeze({IN_TARGET: 0.1}, prices={}, positions={})

        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["skipped"], [])

    def test_available_zero_is_skipped(self):
        """T+N 不可卖：不生成注定被券商拒的单。"""
        plan = self._freeze({}, positions={OUT_A: {"qty": 300, "available": 0}},
                            exit_symbols=[OUT_A])

        self.assertEqual(plan["orders"], [])
        self.assertEqual(plan["skipped"], [f"{OUT_A}(T+N不可卖)"])

    def test_available_caps_sell_quantity(self):
        plan = self._freeze({}, positions={OUT_A: {"qty": 300, "available": 120}},
                            exit_symbols=[OUT_A])

        self.assertEqual([(o["side"], o["qty"]) for o in plan["orders"]], [("SELL", 120)])

    def test_available_missing_sells_full_qty(self):
        """字段缺失 → 按既有口径用全部 qty（不因缺字段把清仓变成不清）。"""
        plan = self._freeze({}, positions={OUT_A: {"qty": 300}}, exit_symbols=[OUT_A])

        self.assertEqual([(o["side"], o["qty"]) for o in plan["orders"]], [("SELL", 300)])

    def test_available_cap_applies_to_target_reductions_too(self):
        """减仓同受可卖量约束（否则同样生成注定被拒的单）。"""
        plan = self._freeze({IN_TARGET: 0.0}, prices={IN_TARGET: 5.0},
                            positions={IN_TARGET: {"qty": 400, "available": 250}})

        self.assertEqual([(o["side"], o["qty"]) for o in plan["orders"]], [("SELL", 250)])

    def test_target_orders_precede_exit_orders(self):
        """顺序契约：target 原序在前，清出集合按代码升序在后（确定性，可复核）。"""
        plan = self._freeze({IN_TARGET: 0.1}, prices={IN_TARGET: 5.0, OUT_A: 10.0,
                                                      OUT_B: 20.0},
                            positions={OUT_A: {"qty": 50}, OUT_B: {"qty": 70}},
                            exit_symbols=[OUT_B, OUT_A])

        self.assertEqual([o["symbol"] for o in plan["orders"]],
                         [OUT_A, OUT_B])  # IN_TARGET 无 bars → 加仓被跳过（无ATR）
        self.assertEqual(plan["exits"], [OUT_A, OUT_B])


# ============================================================================
# ④ plan_auto 端到端：开关 / 收敛集合 / 价格来源 / 可卖量 / 告警与归因
# ============================================================================
class _FixedStrategy:
    """测试替身：固定权重 + 固定 universe。"""

    id = "exit_target_fixed"
    weights = {}
    universe_symbols = []

    def universe(self, conn, as_of, home=None):
        return list(self.universe_symbols)

    def target_weights(self, conn, as_of, home=None):
        return dict(self.weights)


class PlanAutoExitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        strategies.REGISTRY["exit_target_fixed"] = _FixedStrategy()
        self.addCleanup(strategies.REGISTRY.pop, "exit_target_fixed", None)
        (self.home / "trading-account-mode").write_text("sim", encoding="utf-8")
        store.upsert_calendar(self.conn, "SH", [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400}
            for d in (PREV, TODAY)])
        seed_bars(self.conn, IN_TARGET, close=5.0)

    # ---- 种子工具 ----

    def _config(self, watchlist, exit_flag=...):
        """``exit_flag=...``（Ellipsis）= 配置里**不写**该键（缺失态）。"""
        cfg = {"watchlist": watchlist,
               "auto_pipeline": {
                   "enabled": True,
                   "strategies": [{"market": "SH", "strategy": "exit_target_fixed"}],
                   "exec_at": {"SH": "09:35"},
                   "reconcile_at": "19:00"}}
        if exit_flag is not ...:
            cfg[planner.EXIT_OUTSIDE_TARGET_CONFIG_KEY] = exit_flag
        (self.home / "trading-platform.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def _broker(self, rows, cash=1_000_000.0):
        def call(name, args=None, timeout=30, **kwargs):
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "SIM-SH", "market_id": 3}]}
            if name == "sim_trade_position_list":
                return {"positions": rows}
            if name == "sim_trade_cash_info":
                info = {"total_asset": 1_000_000.0}
                if cash is not None:
                    info.update({"balance": str(cash), "max_power_long": str(cash)})
                return info
            raise AssertionError(f"意外券商工具 {name}")

        return call

    def _run(self, rows, weights=None, universe=None, watchlist=None, exit_flag=...):
        _FixedStrategy.weights = {IN_TARGET: 0.2} if weights is None else dict(weights)
        _FixedStrategy.universe_symbols = ([IN_TARGET] if universe is None
                                           else list(universe))
        self._config(watchlist if watchlist is not None else [IN_TARGET],
                     exit_flag=exit_flag)
        return planner.plan_auto(self.conn, str(self.home), "SH", today=TODAY,
                                 broker_call=self._broker(rows))

    def _titles(self, level=None):
        rows = self.conn.execute("SELECT level, title FROM alerts ORDER BY id").fetchall()
        return [r["title"] for r in rows if level is None or r["level"] == level]

    def _holds(self, *rows):
        return [dict(r) for r in rows]

    # ---- 用例 ----

    def test_switch_missing_is_bytewise_todays_behavior(self):
        """键缺失 = 默认关：无清仓单、converge 如实标注关闭、不发新告警。"""
        rows = self._holds({"symbol": OUT_A, "qty": 2000, "cur_price": 12.5, "qty_avbl": 2000},
                           {"symbol": OUT_B, "qty": 800, "cur_price": 9.0, "qty_avbl": 800})
        missing = self._run(rows)
        explicit_off = self._run(rows, exit_flag=False)

        for result in (missing, explicit_off):
            self.assertTrue(result["ok"], result)
            self.assertEqual([o["side"] for o in result["plan"]["orders"]], ["BUY"])
            self.assertEqual(result["converge"],
                             {"enabled": False, "symbols": [], "prices": {},
                              "skipped": [], "empty_target": False})
        # 逐字一致：订单/跳过/落库目标三处与显式 False 完全相同
        self.assertEqual(missing["plan"]["orders"], explicit_off["plan"]["orders"])
        self.assertEqual(missing["plan"]["skipped"], explicit_off["plan"]["skipped"])
        targets = [r["target"] for r in self.conn.execute(
            "SELECT target FROM plans ORDER BY rowid").fetchall()]
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0], targets[1])
        self.assertNotIn(planner.EXIT_ALERT_TITLE, self._titles("warn"))
        self.assertNotIn(planner.EXIT_EMPTY_TARGET_ALERT_TITLE, self._titles("warn"))
        self.assertNotIn(planner.EXIT_CONFIG_ALERT_TITLE, self._titles("warn"))

    def test_switch_on_with_local_close_clears_full_quantity(self):
        """开关开 + 目标外持仓有本地收盘 → 全额清仓单，价格来源标注为本地收盘。"""
        seed_bars(self.conn, OUT_A, close=10.0)
        rows = self._holds({"symbol": OUT_A, "qty": 2000, "cur_price": 12.5, "qty_avbl": 2000})

        result = self._run(rows, exit_flag=True)

        self.assertTrue(result["ok"], result)
        sells = [o for o in result["plan"]["orders"] if o["side"] == "SELL"]
        self.assertEqual([(o["symbol"], o["qty"], o["price"]) for o in sells],
                         [(OUT_A, 2000, 10.0)])   # 本地收盘 10.0 **优先于**券商标记 12.5
        self.assertEqual(result["converge"]["symbols"], [OUT_A])
        self.assertEqual(result["converge"]["prices"][OUT_A],
                         {"price": 10.0, "source": "close", "field": None})
        self.assertEqual(result["plan"]["exits"], [OUT_A])
        self.assertIn(planner.EXIT_ALERT_TITLE, self._titles("warn"))
        self.assertTrue(any("目标外清出" in w for w in result["warnings"]), result["warnings"])

    def test_switch_on_without_local_bar_uses_broker_mark(self):
        """无本地 K 线但有券商标记价（实测的 8 只持仓行情缺口）→ 仍能清出，来源如实标注。"""
        rows = self._holds({"symbol": OUT_A, "qty": 2000, "cur_price": "12.5", "qty_avbl": 2000})

        result = self._run(rows, exit_flag=True)

        self.assertTrue(result["ok"], result)
        sells = [o for o in result["plan"]["orders"] if o["side"] == "SELL"]
        self.assertEqual([(o["symbol"], o["qty"], o["price"]) for o in sells],
                         [(OUT_A, 2000, 12.5)])
        self.assertEqual(result["converge"]["prices"][OUT_A],
                         {"price": 12.5, "source": "broker_mark", "field": "cur_price"})
        self.assertIn(planner.EXIT_ALERT_TITLE, self._titles("warn"))

    def test_switch_on_without_any_price_is_skipped_not_guessed(self):
        """两处都无价 → skipped + 告警，不生成订单（宁可不清，也不猜价）。"""
        rows = self._holds({"symbol": OUT_A, "qty": 2000})   # 无 cur_price、无本地 K 线

        result = self._run(rows, exit_flag=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual([o for o in result["plan"]["orders"] if o["side"] == "SELL"], [])
        self.assertIn(f"{OUT_A}(无价,无法清出)", result["no_atr"])
        self.assertIn(OUT_A, result["no_price"])
        self.assertEqual(result["converge"]["prices"], {})
        self.assertIn(planner.EXIT_ALERT_TITLE, self._titles("warn"))

    def test_switch_on_available_zero_is_skipped(self):
        """T+N 不可卖（available=0）→ skipped + 告警，不生成注定被拒的单。"""
        rows = self._holds({"symbol": OUT_A, "qty": 2000, "cur_price": 12.5, "qty_avbl": 0})

        result = self._run(rows, exit_flag=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual([o for o in result["plan"]["orders"] if o["side"] == "SELL"], [])
        self.assertIn(f"{OUT_A}(T+N不可卖)", result["no_atr"])
        self.assertIn(planner.EXIT_ALERT_TITLE, self._titles("warn"))

    def test_switch_on_available_caps_sell(self):
        rows = self._holds({"symbol": OUT_A, "qty": 2000, "cur_price": 12.5, "qty_avbl": 800})

        result = self._run(rows, exit_flag=True)

        sells = [o for o in result["plan"]["orders"] if o["side"] == "SELL"]
        self.assertEqual([(o["symbol"], o["qty"]) for o in sells], [(OUT_A, 800)])

    def test_empty_target_does_not_clear_anything(self):
        """硬守卫：target 为空 → 一律不收敛（否则「今日选不出标的」= 清空全部持仓）。"""
        rows = self._holds({"symbol": OUT_A, "qty": 2000, "cur_price": 12.5, "qty_avbl": 2000},
                           {"symbol": OUT_B, "qty": 800, "cur_price": 9.0, "qty_avbl": 800})

        result = self._run(rows, weights={}, exit_flag=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["plan"]["orders"], [])
        self.assertEqual(result["converge"],
                         {"enabled": True, "symbols": [], "prices": {},
                          "skipped": [], "empty_target": True})
        self.assertIn(planner.EXIT_EMPTY_TARGET_ALERT_TITLE, self._titles("warn"))
        self.assertNotIn(planner.EXIT_ALERT_TITLE, self._titles("warn"))

    def test_empty_target_without_switch_adds_no_alert(self):
        """开关关闭时 target 为空是既有情形，不因本修订新增告警（默认态零变化）。"""
        rows = self._holds({"symbol": OUT_A, "qty": 2000, "cur_price": 12.5})

        result = self._run(rows, weights={}, exit_flag=...)

        self.assertTrue(result["ok"], result)
        self.assertNotIn(planner.EXIT_EMPTY_TARGET_ALERT_TITLE, self._titles("warn"))

    def test_switch_on_with_nothing_outside_target_is_silent(self):
        """没有目标外持仓 = 正常态：不产单、不告警（否则运维不再看告警）。"""
        result = self._run([], exit_flag=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["converge"],
                         {"enabled": True, "symbols": [], "prices": {},
                          "skipped": [], "empty_target": False})
        self.assertNotIn(planner.EXIT_ALERT_TITLE, self._titles("warn"))

    def test_non_boolean_switch_fails_closed_with_alert(self):
        """配置非法 → 软跳过（作业契约「永不抛」）+ 告警 + **当日不生成计划**。"""
        rows = self._holds({"symbol": OUT_A, "qty": 2000, "cur_price": 12.5})

        result = self._run(rows, exit_flag="true")

        self.assertTrue(result["ok"], result)
        self.assertIn(planner.EXIT_OUTSIDE_TARGET_CONFIG_KEY, result["skipped"])
        self.assertIn(planner.EXIT_CONFIG_ALERT_TITLE, self._titles("warn"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 0)

    def test_live_channel_marks_feed_the_same_exit_path(self):
        """live 通道同一条路径：``nominal_price``/``can_sell_qty`` 归一后照常清出。"""
        def call(name, args=None, timeout=30, **kwargs):
            if name == "account_authorized_trd_accs":
                return {"accounts": [{"account_id": "LIVE-1", "enable_market": [4]}]}
            if name == "account_positions":
                return {"positions": [{"code": "601179", "qty": 500,
                                       "nominal_price": "12.5", "can_sell_qty": 300}]}
            if name == "account_funds":
                return {"total_assets": "1000000.00", "power": "100000.00"}
            raise AssertionError(f"意外券商工具 {name}")

        _FixedStrategy.weights = {IN_TARGET: 0.2}
        _FixedStrategy.universe_symbols = [IN_TARGET]
        self._config([IN_TARGET], exit_flag=True)
        (self.home / "trading-account-mode").write_text("live", encoding="utf-8")

        result = planner.plan_auto(self.conn, str(self.home), "SH", today=TODAY,
                                   broker_call=call)

        self.assertTrue(result["ok"], result)
        sells = [o for o in result["plan"]["orders"] if o["side"] == "SELL"]
        self.assertEqual([(o["symbol"], o["qty"], o["price"]) for o in sells],
                         [(OUT_A, 300, 12.5)])   # 500 股里只有 300 可卖（T+N）
        self.assertEqual(result["converge"]["prices"][OUT_A]["source"], "broker_mark")

    def test_alert_attribution_maps_are_locked(self):
        """三条新标题的归因是硬编码字面量——锁住，防 pipeline 归因静默漂移。"""
        self.assertEqual(pipeline._CONTENT_OUTCOMES[planner.EXIT_ALERT_TITLE],
                         ("build_plan", None, "清出目标外持仓"))
        self.assertEqual(pipeline._CONTENT_OUTCOMES[planner.EXIT_EMPTY_TARGET_ALERT_TITLE],
                         ("build_plan", None, "目标为空：未执行目标外清出"))
        self.assertEqual(pipeline._CONTENT_OUTCOMES[planner.EXIT_CONFIG_ALERT_TITLE],
                         ("build_plan", "skipped", "exit_outside_target 配置非法：当日未生成计划"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
