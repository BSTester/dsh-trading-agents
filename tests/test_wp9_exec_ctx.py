"""WP9 执行侧 ctx 与退出单过规则 5（规格 §4.2 第 7 点）。

背景与算术（权益 1,000,000 / max_position_pct 25% → 上限 250,000）：

    持仓 1700 股 × 200 元 = 340,000（34%，超上限），全额卖出：
      * ctx 持仓市值空（修复前）：规则 5 → 0 + 340,000 = 340,000 > 250,000 → **拒**
      * ctx 填真实市值（只填空表）：规则 5 → 340,000 + 340,000 = 680,000 → **更严地拒**
      * 方向折算（本修复）：规则 5 → 成交后剩余 0 → **放行**

即「把真实持仓填进 ctx」**本身不足以**让退出单通过——规则 5 的判定式
``持仓市值 + 本单名义`` 只在买入方向成立。本修复因此由两层组成：

  1. ``daemon._positions_ctx``：持仓市值/持仓数取**券商事实**，取不到退回离线保守默认
     并标 ``ctx_source="offline_default"``；
  2. ``execute._order_ctx``：仅在 ``ctx_source=="broker"`` 且该标的持仓已知时，把 ctx
     持仓市值折算为「成交后剩余市值 − 本单名义」，使规则 5 的结果恒等于真实成交后市值。

``risk.py`` 一行未改（硬拦截语义不动），本文件用正反两面钉住：

  * 正：券商持仓可得 → 超上限仓位全额退出放行、减仓到上限内放行；
  * 反：券商持仓不可得（offline_default）→ 同一退出单仍被规则 5 拦；
  * 反：买入方向的超限叠加仍被规则 5 拦（折算只作用于 SELL）；
  * 反：部分减仓后仍超上限 → 仍被拦（折算给出的是**真实**成交后市值，不放水）；
  * 规则 6：真实持仓数使「新增标的」在满仓时被拦。
"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import daemon, oms, store  # noqa: E402

HELD = "SH.600519"
NEW = "SH.601398"
DAY = "2026-09-16"
PREV = "2026-09-15"


def small_atr_bars(conn, symbol, close=200.0, half_range=0.25):
    """铺 20 根窄幅日线：ATR = 0.5 → 止损距离 = 1.0（规则 4 对 1700 股仅计 1,700 元）。

    让规则 4 不成为本文件的主角：SELL/部分减仓的 qty×止损距离必须落在权益×1% 内，
    这样判定才会前进到规则 5——本文件要验的正是规则 5 的方向语义。
    """
    end = date.fromisoformat(PREV)
    days = [(end - timedelta(days=19 - i)).isoformat() for i in range(20)]
    store.upsert_bars(conn, symbol, "1d", [
        {"t": d, "o": close, "h": close + half_range, "l": close - half_range,
         "c": close, "v": 1000.0} for d in days], source="test")


class ExecCtxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        small_atr_bars(self.conn, HELD)
        small_atr_bars(self.conn, NEW)

    def _plan(self, orders, plan_id="PLN-1", mode="sim"):
        """直接登记一份 frozen 计划 + 订单（绕过计划生成，聚焦执行侧判定）。"""
        content_hash = f"hash-{plan_id}"
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,status,"
            "created_at,origin,market) VALUES(?,?,'sim','s','{}',?,'frozen',"
            "'2026-09-16 16:20:00','manual',NULL)", (plan_id, DAY, content_hash))
        self.conn.commit()
        for symbol, side, qty, price in orders:
            oms.register_order(self.conn, plan_id, symbol, symbol.split(".")[0], side,
                               qty, price, mode, content_hash)
        return content_hash

    def _broker(self, positions=None, fail_positions=False):
        """sim 假券商：账户/持仓/资金/下单。fail_positions 模拟持仓查询不可得。"""
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append((name, dict(args or {})))
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3}]}
            if name == "sim_trade_position_list":
                if fail_positions:
                    raise RuntimeError("持仓查询不可得（演练）")
                return {"positions": [{"symbol": s, "qty": q}
                                      for s, q in (positions or {}).items()]}
            if name == "sim_trade_cash_info":
                return {"total_asset": 1_000_000.0}
            if name == "sim_trade_input_order":
                return {"order_id": f"SIM-O{len(calls)}"}
            if name == "sim_trade_history_order_list":
                return {"orders": []}
            raise AssertionError(f"意外券商工具 {name}")

        return call, calls

    def _run(self, conn_plan_hash, positions=None, fail_positions=False, equity=None):
        call, calls = self._broker(positions, fail_positions)
        out = daemon._execute_plan(
            self.conn, str(self.home), {"plan_hash": conn_plan_hash}, broker_call=call,
            equity=equity, today=DAY, calendar_ok=True)
        return out, calls

    @staticmethod
    def _rule_of(conn, plan_id):
        return [dict(r) for r in conn.execute(
            "SELECT rule, allowed, reason FROM risk_checks WHERE plan_id=?"
            " ORDER BY id", (plan_id,)).fetchall()]

    def test_oversized_full_exit_passes_with_broker_positions(self):
        """超上限仓位（34%）全额退出：券商持仓可得 → 规则 5 放行、券商收到卖单。"""
        plan_hash = self._plan([(HELD, "SELL", 1700, 200.0)])
        out, calls = self._run(plan_hash, positions={HELD: 1700})
        self.assertEqual(out["ctx_source"], "broker", out)
        self.assertEqual((out["submitted"], out["blocked"]), (1, 0), out)
        places = [c for c in calls if c[0] == "sim_trade_input_order"]
        self.assertEqual(len(places), 1, calls)
        self.assertEqual(places[0][1]["order_side"], 2)   # broker.place：1=BUY 2=SELL

    def test_offline_default_still_blocks_the_same_exit(self):
        """反证：持仓不可得（offline_default）→ 同一退出单仍被规则 5 拦、券商零调用。

        离线口径下 ctx 持仓市值缺失，规则 5 按「本单全额名义」判定（340,000 > 250,000）
        —— 宁可拒绝，也不用**未知**持仓放宽硬规则。
        """
        plan_hash = self._plan([(HELD, "SELL", 1700, 200.0)])
        out, calls = self._run(plan_hash, fail_positions=True)
        self.assertEqual(out["ctx_source"], "offline_default", out)
        self.assertEqual((out["submitted"], out["blocked"]), (0, 1), out)
        self.assertEqual([c for c in calls if c[0] == "sim_trade_input_order"], [])
        verdicts = self._rule_of(self.conn, "PLN-1")
        self.assertTrue(any(v["rule"] == 5 and not v["allowed"] for v in verdicts), verdicts)

    def test_buy_into_over_cap_position_still_blocked(self):
        """反证：折算只作用于 SELL——买入方向的叠加超限仍被规则 5 拦。"""
        plan_hash = self._plan([(HELD, "BUY", 100, 200.0)])
        out, _ = self._run(plan_hash, positions={HELD: 1500})   # 300,000 已超上限
        self.assertEqual((out["submitted"], out["blocked"]), (0, 1), out)
        verdicts = self._rule_of(self.conn, "PLN-1")
        self.assertTrue(any(v["rule"] == 5 and not v["allowed"] for v in verdicts), verdicts)

    def test_partial_reduce_still_over_cap_is_blocked(self):
        """反证：折算给出**真实**成交后市值——部分减仓后仍超上限 → 仍被拦（不放水）。

        持仓 3000 股 × 200 = 600,000；卖 1000 股后剩 400,000 > 250,000。
        修复前空 ctx 会算成「0 + 200,000 = 200,000 ≤ 上限」而错误放行。
        """
        plan_hash = self._plan([(HELD, "SELL", 1000, 200.0)])
        out, _ = self._run(plan_hash, positions={HELD: 3000})
        self.assertEqual((out["submitted"], out["blocked"]), (0, 1), out)
        verdicts = self._rule_of(self.conn, "PLN-1")
        self.assertTrue(any(v["rule"] == 5 and not v["allowed"] for v in verdicts), verdicts)

    def test_partial_reduce_within_cap_passes(self):
        """正：部分减仓后回到上限内 → 放行（持仓 300,000 → 卖 500 股剩 200,000）。"""
        plan_hash = self._plan([(HELD, "SELL", 500, 200.0)])
        out, calls = self._run(plan_hash, positions={HELD: 1500})
        self.assertEqual((out["submitted"], out["blocked"]), (1, 0), out)
        self.assertEqual(len([c for c in calls if c[0] == "sim_trade_input_order"]), 1)

    def test_rule6_uses_real_positions_count(self):
        """规则 6：真实持仓数（5 = max_positions）→ 买入新标的被拦。

        离线默认下 positions_count=0，这条买入会被放行（持仓数上限看不见存量）——
        真实持仓数接入后该缺口关闭。
        """
        plan_hash = self._plan([(NEW, "BUY", 100, 200.0)])
        held = {f"SH.60000{i}": 100 for i in range(5)}      # 5 个持仓标的（各 20,000 元）
        out, calls = self._run(plan_hash, positions=held)
        rule6_verdicts = [v for v in self._rule_of(self.conn, "PLN-1") if v["rule"] == 6]
        self.assertTrue(rule6_verdicts and not rule6_verdicts[0]["allowed"], rule6_verdicts)
        self.assertEqual((out["submitted"], out["blocked"]), (0, 1), out)
        self.assertEqual([c for c in calls if c[0] == "sim_trade_input_order"], [])

    def test_broker_flat_reports_broker_source_with_zero_count(self):
        """券商说「无持仓」也是**已知事实**：标 broker + 计数 0（不是 offline_default）。"""
        plan_hash = self._plan([(NEW, "BUY", 100, 200.0)])
        out, _ = self._run(plan_hash, positions={})
        self.assertEqual(out["ctx_source"], "broker", out)
        self.assertEqual((out["submitted"], out["blocked"]), (1, 0), out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
