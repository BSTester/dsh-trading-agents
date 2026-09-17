"""WP17-A：模拟盘全自动闭环的计划期缺口（现金封顶 + 结构性预警）。

背景（2026-09-17 实机）：本机 A 股模拟账户「权益 81.2 万 / 可用现金 5.53 万 / 已持 8 只」
而 ``max_positions=5``。计划定量只看权益 → 买单必被券商以资金不足拒，自动流水线
**结构性跑不起来**。本文件钉住计划侧的两条修复：

* ① **买入按可用现金封顶**（``planner.build_and_freeze(broker_cash=...)``）：定量 =
  ``min(权重定量, 风险预算, 现金可买整手数)``；多买单按计划顺序**共享同一笔现金**逐单
  扣减；现金不可得 → 一笔买单都不生成（卖单照常、**绝不用权益冒充现金**）；卖单不受现金
  约束；调用方未提供现金事实（``broker_cash=None``）时既有语义逐字不变；
* ①b **现金事实的唯一入口**（``broker.positions_equity_cash``）：字段优先级、缺字段不猜、
  与权益同源于**一次**账户查询；
* ② **计划期结构性预警**（``planner.plan_auto``）：持仓数超限/现金封顶/现金不可得/零订单
  都要在计划期说清，零订单计划**不得**在流程页显示成「已完成」。

对账侧的收编收敛见 ``test_wp17_reconcile_import.py`` 与 ``test_wp9_reconcile_daily.py``。
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import (broker as core_broker, pipeline, planner,  # noqa: E402
                          store, strategies)

TODAY = "2026-09-16"
PREV = "2026-09-15"


def seed_bars(conn, symbol, close, count=20, last="2026-08-31", days=None):
    """铺确定性日线：h=c+0.5 / l=c−0.5 / o=c → 每根 TR=1.0 → ATR(14)=1.0。

    止损距离 = ATR × 2.0 = 2.0 → 风险预算 = floor(权益 × 1% ÷ 2.0 ÷ 100) × 100
    = 权益 × 0.005 股（权益 100 万 → 5000 股）。``last`` 缺省固定 2026-08-31，
    早于本文件所有 as_of，使 PIT 过滤后仍留足 15 根；``days`` 显式给出时按传入日期铺
    （plan_auto 的数据就绪门要求最后一根落在当次会话日）。
    """
    if days is None:
        end = date.fromisoformat(last)
        days = [(end - timedelta(days=count - 1 - i)).isoformat() for i in range(count)]
    store.upsert_bars(conn, symbol, "1d", [
        {"t": d, "o": close, "h": close + 0.5, "l": close - 0.5, "c": close, "v": 1000.0}
        for d in days], source="test")


# ============================================================================
# ① 买入按可用现金封顶（planner.build_and_freeze 单元层）
# ============================================================================
class CashCapTest(unittest.TestCase):
    """现金约束的定量口径：共享一笔现金、卖单不受限、缺现金不生成买单。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        self.addCleanup(self.conn.close)

    def _plan(self, target, prices, positions=None, cash=..., equity=1_000_000.0):
        """建计划。``cash=...``（Ellipsis）表示**不提供现金事实**（旧语义路径）。"""
        positions = positions or {}
        kwargs = {}
        if cash is not ...:
            kwargs["broker_cash"] = lambda _mode: cash
        return planner.build_and_freeze(
            self.conn, mode="sim", strategy_id="wp17_fixed", target=target,
            broker_positions=lambda _mode: (positions, equity), prices=prices,
            as_of="2026-09-13", origin="auto", market="SH", **kwargs)

    def test_cash_caps_buy_qty(self):
        """现金不足 → 买入量被压到买得起的整手数，并如实列入 ``cash.capped``。"""
        seed_bars(self.conn, "SH.601398", 5.0)
        plan = self._plan({"SH.601398": 0.5}, {"SH.601398": 5.0}, cash=10_000.0)

        # 权重定量 10 万股、风险预算 5000 股 → 现金 1 万只买得起 2000 股
        self.assertEqual([o["qty"] for o in plan["orders"]], [2000])
        self.assertEqual(plan["cash"], {"available": 10_000.0, "remaining": 0.0,
                                        "unavailable": False, "capped": ["SH.601398"]})
        self.assertEqual(plan["skipped"], [])

    def test_cash_ample_does_not_change_legacy_qty(self):
        """现金充足 → 定量与无现金约束时逐字一致（不引入新的上限臂）。"""
        seed_bars(self.conn, "SH.601398", 5.0)
        capped = self._plan({"SH.601398": 0.5}, {"SH.601398": 5.0}, cash=1_000_000.0)
        legacy = self._plan({"SH.601398": 0.5}, {"SH.601398": 5.0})

        self.assertEqual([o["qty"] for o in capped["orders"]], [5000])  # 风险预算臂
        self.assertEqual([o["qty"] for o in capped["orders"]],
                         [o["qty"] for o in legacy["orders"]])
        self.assertEqual(capped["cash"]["capped"], [])
        self.assertEqual(capped["cash"]["remaining"], 1_000_000.0 - 5000 * 5.0)

    def test_multiple_buys_share_one_cash_pool(self):
        """多买单按计划顺序共享同一笔现金：逐单扣减，总买入金额不超现金。"""
        seed_bars(self.conn, "SH.601398", 5.0)
        seed_bars(self.conn, "SH.601288", 5.0)
        plan = self._plan({"SH.601398": 0.5, "SH.601288": 0.5},
                          {"SH.601398": 5.0, "SH.601288": 5.0}, cash=30_000.0)

        qty = {o["symbol"]: o["qty"] for o in plan["orders"]}
        # 第一单吃掉 2.5 万 → 只剩 5000 → 第二单 1000 股（**不是**各按全额 3 万定量）
        self.assertEqual(qty, {"SH.601398": 5000, "SH.601288": 1000})
        self.assertEqual(plan["cash"]["remaining"], 0.0)
        spent = sum(o["qty"] * o["price"] for o in plan["orders"])
        self.assertLessEqual(spent, 30_000.0)
        self.assertEqual(plan["cash"]["capped"], ["SH.601288"])

    def test_cash_unavailable_generates_no_buy_but_keeps_sells(self):
        """现金字段全缺 → 不生成买单（绝不用权益冒充现金），卖单照常。"""
        seed_bars(self.conn, "SH.601398", 5.0)
        seed_bars(self.conn, "SH.600519", 100.0)
        plan = self._plan({"SH.601398": 0.5, "SH.600519": 0.0},
                          {"SH.601398": 5.0, "SH.600519": 100.0},
                          positions={"SH.600519": {"qty": 300}}, cash=None)

        self.assertEqual([(o["symbol"], o["side"], o["qty"]) for o in plan["orders"]],
                         [("SH.600519", "SELL", 300)])
        self.assertEqual(plan["skipped"], ["SH.601398(现金不可得)"])
        self.assertEqual(plan["cash"]["available"], None)
        self.assertTrue(plan["cash"]["unavailable"])
        self.assertEqual(plan["cash"]["remaining"], None)

    def test_zero_cash_still_allows_sells(self):
        """现金为 0 也不限制减仓（减少敞口不是新增资金需求）。"""
        seed_bars(self.conn, "SH.601398", 5.0)
        seed_bars(self.conn, "SH.600519", 100.0)
        plan = self._plan({"SH.601398": 0.5, "SH.600519": 0.0},
                          {"SH.601398": 5.0, "SH.600519": 100.0},
                          positions={"SH.600519": {"qty": 300}}, cash=0.0)

        self.assertEqual([(o["symbol"], o["side"], o["qty"]) for o in plan["orders"]],
                         [("SH.600519", "SELL", 300)])
        self.assertEqual(plan["skipped"], ["SH.601398(现金不足一手)"])
        self.assertEqual(plan["cash"]["remaining"], 0.0)

    def test_no_cash_fact_keeps_legacy_result_shape(self):
        """``broker_cash=None``（离线/诊断路径）→ 返回字典**无** ``cash`` 键，语义不变。"""
        seed_bars(self.conn, "SH.601398", 5.0)
        plan = self._plan({"SH.601398": 0.5}, {"SH.601398": 5.0})

        self.assertNotIn("cash", plan)
        self.assertEqual([o["qty"] for o in plan["orders"]], [5000])


# ============================================================================
# ①b 现金事实的唯一入口：字段优先级 / 缺字段不猜 / 单次账户查询
# ============================================================================
class BrokerCashSnapshotTest(unittest.TestCase):
    """``broker.positions_equity_cash``：现金与权益同源于**一次**账户查询。"""

    def _sim(self, cash_info, positions=None):
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append(name)
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3}]}
            if name == "sim_trade_position_list":
                return {"positions": positions or []}
            if name == "sim_trade_cash_info":
                return cash_info
            raise AssertionError(f"意外工具 {name}")

        return call, calls

    def _live(self, funds, missing_equity=False):
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append(name)
            if name == "account_authorized_trd_accs":
                return {"accounts": [{"account_id": "L1", "enable_market": [4]}]}
            if name == "account_positions":
                return {"positions": [{"code": "600519", "qty": 400},
                                      {"code": "HK.00700", "qty": 5}]}  # 跨市场须被过滤
            if name == "account_funds":
                return {} if missing_equity else funds
            raise AssertionError(f"意外工具 {name}")

        return call, calls

    def test_sim_cash_prefers_max_power_long(self):
        """sim：``max_power_long``（券商口径最大可买）优先于 ``balance``。"""
        call, calls = self._sim({"total_asset": "812231.816", "balance": "55257.816",
                                 "max_power_long": "50000.0"})
        positions, equity, cash = core_broker.positions_equity_cash(call, "sim", "SH")

        self.assertEqual(cash, 50_000.0)
        self.assertEqual(equity, 812_231.816)
        # 现金与权益同源：账户列表与资金各只查一次（不重复往返）
        self.assertEqual(calls.count("sim_trade_account_list"), 1)
        self.assertEqual(calls.count("sim_trade_cash_info"), 1)

    def test_sim_cash_falls_back_to_balance(self):
        call, _ = self._sim({"total_asset": "812231.816", "balance": "55257.816"})
        self.assertEqual(core_broker.positions_equity_cash(call, "sim", "SH")[2],
                         55_257.816)

    def test_sim_cash_missing_is_none_not_equity(self):
        """现金字段全缺 → ``None``（**绝不用权益冒充现金**），权益照常给出。"""
        call, _ = self._sim({"total_asset": "812231.816"})
        positions, equity, cash = core_broker.positions_equity_cash(call, "sim", "SH")
        self.assertEqual(equity, 812_231.816)
        self.assertIsNone(cash)
        self.assertIsNone(core_broker.available_cash(call, "sim", "SH"))

    def test_sim_equity_missing_returns_none_equity(self):
        """sim 多账户聚合任一账户缺权益字段 → ``equity=None``（拒绝给出不可信的合计）。

        与 live 的 ``EquityUnavailable`` 面孔不同、语义相同：调用方必须显式处理两种结局
        （``plan_auto`` 都按跳过当日计划），**绝不把 None 当 0**（那会凭空生成清仓单）。
        """
        call, _ = self._sim({"max_power_long": "50000.0"})  # 权益两字段（total_asset/balance）皆缺
        positions, equity, cash = core_broker.positions_equity_cash(call, "sim", "SH")
        self.assertIsNone(equity)
        self.assertEqual(cash, 50_000.0)
        self.assertEqual(positions, {})

    def test_live_cash_field_priority(self):
        """live：``power`` → ``available_funds`` → ``cash``（官方 get-funds 已发布字段）。"""
        for field, value in (("power", "111.0"), ("available_funds", "222.0"),
                             ("cash", "333.0")):
            funds = {"total_assets": "1000000.00", field: value}
            call, _ = self._live(funds)
            positions, equity, cash = core_broker.positions_equity_cash(call, "live", "SH")
            self.assertEqual(cash, float(value), field)
            self.assertEqual(equity, 1_000_000.0)
            self.assertEqual(positions, {"SH.600519": {"qty": 400}})  # 港股被市场前缀过滤

    def test_live_cash_missing_is_none(self):
        call, _ = self._live({"total_assets": "1000000.00"})
        self.assertIsNone(core_broker.positions_equity_cash(call, "live", "SH")[2])

    def test_live_equity_missing_raises(self):
        call, _ = self._live({}, missing_equity=True)
        with self.assertRaises(core_broker.EquityUnavailable):
            core_broker.positions_equity_cash(call, "live", "SH")

    def test_two_tuple_wrapper_and_available_cash_share_one_source(self):
        """既有调用方零改动：``positions_and_equity`` 仍是二元组；现金走同一实现。"""
        call, _ = self._sim({"total_asset": "812231.816", "balance": "55257.816"},
                            positions=[{"symbol": "600519", "qty": 400}])
        pair = core_broker.positions_and_equity(call, "sim", "SH")
        self.assertEqual(pair, ({"SH.600519": {"qty": 400}}, 812_231.816))
        self.assertEqual(core_broker.available_cash(call, "sim", "SH"), 55_257.816)


# ============================================================================
# ② 计划期结构性预警（plan_auto 层：页面不得把「没做成」显示成「已完成」）
# ============================================================================
class _FixedStrategy:
    """测试替身：固定权重 + 固定 universe（信号判定由 test_core_strategies 覆盖）。"""

    id = "wp17_fixed"
    weights = {}
    universe_symbols = []

    def universe(self, conn, as_of, home=None):
        return list(self.universe_symbols)

    def target_weights(self, conn, as_of, home=None):
        return dict(self.weights)


class PlanAutoStructuralWarningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        strategies.REGISTRY["wp17_fixed"] = _FixedStrategy()
        self.addCleanup(strategies.REGISTRY.pop, "wp17_fixed", None)
        (self.home / "trading-account-mode").write_text("sim", encoding="utf-8")

    # ---- 种子工具 ----

    def _config(self, watchlist):
        cfg = {"watchlist": watchlist,
               "auto_pipeline": {
                   "enabled": True,
                   "strategies": [{"market": "SH", "strategy": "wp17_fixed"}],
                   "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
                   "reconcile_at": "19:00"}}
        (self.home / "trading-platform.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def _calendar(self):
        store.upsert_calendar(self.conn, "SH", [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400}
            for d in (PREV, TODAY)])

    def _bars(self, symbol, close=5.0):
        days = [(date.fromisoformat(TODAY) - timedelta(days=19 - i)).isoformat()
                for i in range(20)]
        seed_bars(self.conn, symbol, close, days=days)

    def _broker(self, positions=None, cash=1_000_000.0):
        """sim 假通道：现金走 ``max_power_long``/``balance``（与 core 口径同源）。

        ``cash=None`` → 资金响应**不含现金字段**（构造「现金不可得」）。
        """
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append((name, args))
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "SIM-SH", "market_id": 3,
                                      "account_title": "模拟账户"}]}
            if name == "sim_trade_position_list":
                return {"positions": positions or []}
            if name == "sim_trade_cash_info":
                info = {"total_asset": 812_231.816}
                if cash is not None:
                    info.update({"balance": str(cash), "max_power_long": str(cash)})
                return info
            raise AssertionError(f"意外工具 {name}")

        return call, calls

    def _alerts(self, level=None):
        rows = [dict(r) for r in self.conn.execute(
            "SELECT level, title, detail FROM alerts ORDER BY id").fetchall()]
        return [a for a in rows if level is None or a["level"] == level]

    def _titles(self, level=None):
        return [a["title"] for a in self._alerts(level)]

    # ---- 用例 ----

    def test_baseline_cash_ample_emits_no_warning(self):
        """无结构性约束时零预警（否则预警就成了噪声，运维不再看）。"""
        _FixedStrategy.weights = {"SH.601398": 0.5}
        _FixedStrategy.universe_symbols = ["SH.601398"]
        self._config(["SH.601398"])
        self._calendar()
        self._bars("SH.601398", 5.0)
        call, _ = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["warnings"], [])
        # 权益 812231.816 → 风险预算 floor(812231.816×1%÷2.0÷100)×100 = 4000 股（约束臂）
        self.assertEqual([o["qty"] for o in result["plan"]["orders"]], [4000])
        self.assertEqual(self._titles("warn"), [])

    def test_insufficient_cash_warns_capped(self):
        """现金封顶 → warn「计划预警：现金封顶」+ 计划数字如实（页面看得见被压低）。"""
        _FixedStrategy.weights = {"SH.601398": 0.5}
        _FixedStrategy.universe_symbols = ["SH.601398"]
        self._config(["SH.601398"])
        self._calendar()
        self._bars("SH.601398", 5.0)
        call, _ = self._broker(cash=10_000.0)

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["cash"], 10_000.0)
        self.assertEqual([o["qty"] for o in result["plan"]["orders"]], [2000])
        self.assertEqual(result["plan"]["cash"]["capped"], ["SH.601398"])
        self.assertIn("计划预警：现金封顶", self._titles("warn"))
        self.assertTrue(any("现金封顶" in w for w in result["warnings"]),
                        result["warnings"])

    def test_cash_unavailable_warns_and_produces_no_orders(self):
        """现金不可得 + 无卖单 → 零订单计划：既报现金不可得，也报「无可执行订单」。"""
        _FixedStrategy.weights = {"SH.601398": 0.5}
        _FixedStrategy.universe_symbols = ["SH.601398"]
        self._config(["SH.601398"])
        self._calendar()
        self._bars("SH.601398", 5.0)
        call, _ = self._broker(cash=None)

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["cash"])
        self.assertEqual(result["plan"]["orders"], [])
        self.assertIn("SH.601398(现金不可得)", result["no_atr"])
        titles = self._titles("warn")
        self.assertIn("计划预警：现金不可得", titles)
        self.assertIn("计划跳过：无可执行订单", titles)  # 「没做成」不得显示成「已完成」

    def test_positions_over_limit_warns_even_with_orders(self):
        """持仓数已达上限且计划含新建仓 → warn（有订单也照样预警：执行时必被规则 6 拦）。"""
        _FixedStrategy.weights = {"SH.601398": 0.5}
        _FixedStrategy.universe_symbols = ["SH.601398"]
        self._config(["SH.601398"])
        self._calendar()
        self._bars("SH.601398", 5.0)
        held = [{"symbol": s, "qty": 100} for s in
                ("SH.600000", "SH.600036", "SH.601318", "SH.600030", "SH.601166")]
        call, _ = self._broker(positions=held)

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["plan"]["orders"]), 1)  # 计划本身照常生成
        self.assertIn("计划预警：持仓数超限", self._titles("warn"))
        self.assertTrue(any("持仓 5 只" in w for w in result["warnings"]),
                        result["warnings"])
        self.assertNotIn("计划跳过：无可执行订单", self._titles("warn"))

    def test_alert_attribution_maps_are_locked(self):
        """四条标题的归因是硬编码字面量（页面据此判阶段状态）——锁住，防漂移。"""
        self.assertEqual(pipeline._ALERT_STATUS["计划跳过：无可执行订单"],
                         ("build_plan", "skipped"))
        for title in ("计划预警：现金不可得", "计划预警：现金封顶", "计划预警：持仓数超限"):
            job, status, summary = pipeline._CONTENT_OUTCOMES[title]
            self.assertEqual(job, "build_plan")
            self.assertIsNone(status)      # 作业确实跑完：不改状态，只补摘要
            self.assertTrue(summary)
        self.assertEqual(pipeline._CONTENT_OUTCOMES["计划跳过：无可执行订单"][1], "skipped")


if __name__ == "__main__":
    unittest.main()
