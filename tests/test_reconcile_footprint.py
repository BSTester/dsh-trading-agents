"""对账持仓足迹口径（缺陷复核，2026-09-19 真机）。

两件被怀疑成缺陷的事，本文件把**真机形状**钉成离线夹具：

* **方向码**：券商订单历史的 ``side`` 是数字码 ``1=Buy / 2=Sell``（两通道同源，
  工作台的「买入/卖出」只是 `platform/server/labels.py` 对同一份码的中文化）。真机证据见
  ``SideCodeTest`` 的 docstring：三张港股单 ``7138916/7138918/7138921`` 的券商原文备注是
  「清仓/减200/减1000」（side=2）→ 本地净持仓为负是**符号正确**的结果，不是「买记成卖」；
  真正缺的是那三只的**建仓腿**（本地库被清空 + 券商 30 天订单窗口到不了建仓那天）。
* **持仓足迹**：只有「本地观察到过建仓（买入成交）」或「本地相信自己已成交」的标的才有
  资格参与持仓级比对。收编行、从未到券商的 draft/frozen、无成交的作废单都**不建立**持仓
  知识——把它们算成足迹，会让券商侧的历史存量持仓从 ``untracked`` 升级成 ``missing_side``
  → critical + halt（实机：手工计划 ``PLN-20260918-sim-6815`` 的 8 张作废卖单）。

反向约束（不得为了消差异而削弱检测）：「本地认为已平仓、券商仍持有」这类**真**单边缺失
必须继续 critical + halt（本地必然有买入成交 → 仍在足迹内）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import reconcile, store  # noqa: E402

TODAY = "2026-09-19"


class _FakeBroker:
    """假券商通道（形状与 tests/test_wp9_reconcile_daily.py 同口径：裸代码 + 字符串数量）。"""

    def __init__(self, orders_by_market=None, positions_by_market=None, accounts=None):
        self.orders = orders_by_market or {}
        self.positions = positions_by_market or {}
        self.accounts = accounts if accounts is not None else [
            {"account_id": "SIM-HK", "market_id": 1},
            {"account_id": "SIM-SH", "market_id": 3},
        ]
        self.calls = []

    def __call__(self, name, args=None, timeout=None):
        self.calls.append({"tool": name, "args": dict(args or {})})
        if name == "sim_trade_account_list":
            return {"accounts": self.accounts}
        if name == "sim_trade_position_list":
            return {"positions": self.positions.get((args or {}).get("market"), [])}
        if name == "sim_trade_history_order_list":
            return {"orders": self.orders.get((args or {}).get("acc_id"), [])}
        if name == "sim_trade_cash_info":
            return {"total_asset": 1_000_000.0}
        raise AssertionError(f"未预期的券商工具：{name}")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        (self.home / "trading-account-mode").write_text("sim", encoding="utf-8")

    # ---- 种子（直接写表：对账只读台账）----

    def _order(self, cid, symbol, side="BUY", qty=100, price=10.0, broker_id=None,
               status="submitted", plan_id="PLN-1", created_at=None):
        now = created_at or f"{TODAY} 09:35:00"
        self.conn.execute(
            "INSERT INTO orders(client_order_id,plan_id,symbol,market,side,qty,price,"
            "status,broker_order_id,mode,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, plan_id, symbol, symbol.split(".", 1)[0], side, qty, price, status,
             broker_id, "sim", now, now))
        self.conn.commit()
        return cid

    def _fill(self, fill_id, cid, qty, price=10.0):
        self.conn.execute(
            "INSERT INTO fills(fill_id,client_order_id,price,qty,traded_at,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (fill_id, cid, price, qty, None, f"{TODAY} 09:36:00"))
        self.conn.commit()

    def _sim_order(self, order_id, symbol, side=1, qty="100", cum_qty="100", status=4,
                   price="10.0", avg_fill_price="10.0"):
        return {"order_id": order_id, "symbol": symbol, "side": side, "qty": qty,
                "cum_qty": cum_qty, "price": price, "avg_fill_price": avg_fill_price,
                "status": status}

    def _daily(self, broker):
        return reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)

    def _kinds(self, result):
        return [d["kind"] for d in result["diffs"]]


# ============================================================================
# ① 方向码：1=Buy / 2=Sell（真机复核：不是「买记成卖」）
# ============================================================================
class SideCodeTest(_Base):
    def test_history_side_code_maps_one_buy_two_sell(self):
        """券商订单历史 ``side`` 数字码 → 仓库方向：1=BUY、2=SELL、其它不猜（空串）。

        真机交叉验证（2026-09-19，本机 sim + `futu_channel=openapi`）：

        * 自动流水线买入单 ``SH.600010``（券商单号 ``7149712``/``7149716``，
          HANDOVER §8.3 记录其为 BUY，且工作台 ``orders_open`` 显示 ``side=1``「买入」）
          在 ``sim_trade_history_order_list`` 里回的是 ``side=1``；
        * 三张港股单 ``7138916``(智谱清仓)/``7138918``(MINIMAX 减200)/``7138921``
          (中芯减1000) 回的是 ``side=2``，券商原文 ``text`` 备注就是减仓/清仓。

        因此 2 → SELL 是**事实**；把 2 改成 BUY 会把真实的卖单读成买单（方向反号）。
        """
        rows = reconcile._broker_order_rows(
            [{"order_id": "1", "symbol": "600519", "side": 1, "qty": "100"},
             {"order_id": "2", "symbol": "600519", "side": 2, "qty": "100"},
             {"order_id": "3", "symbol": "600519", "side": 7, "qty": "100"}],
            lambda value: value)
        self.assertEqual([r["side"] for r in rows], ["BUY", "SELL", ""])

    def test_backfilled_sell_is_negative_and_buy_is_positive(self):
        """回填在**卖出**订单上 → 本地净持仓为负；在**买入**订单上 → 为正。

        这是真机 ``HK.00100 local=-200 / HK.00981 local=-1000`` 的成因复现：负数来自
        「只有减仓腿、没有建仓腿」，不是符号写反。
        """
        self._order("sell-1", "HK.00100", side="SELL", qty=200, broker_id="B-SELL")
        self._fill("bf-sell", "sell-1", 200, 253.0)
        self._order("buy-1", "HK.00981", side="BUY", qty=1000, broker_id="B-BUY")
        self._fill("bf-buy", "buy-1", 1000, 61.4)
        self.assertEqual(reconcile.local_net_positions(self.conn),
                         {"HK.00100": -200, "HK.00981": 1000})


# ============================================================================
# ② 持仓足迹：谁有资格参与持仓级比对
# ============================================================================
class FootprintKnowledgeTest(_Base):
    def test_cancelled_local_plan_orders_do_not_grant_position_knowledge(self):
        """真机 2026-09-19：手工计划的**作废**卖单不得让历史存量持仓变成 missing_side。

        夹具照抄实机形状：本地库被清空后，券商侧 8 只 A 股老仓仍在；本地唯一与它们有关
        的记录是手工计划 ``PLN-20260918-sim-6815`` 的 8 张卖单——``status='cancelled'``、
        ``broker_order_id IS NULL``（kill switch 拦下，从未到券商）、零成交。
        它们**不含任何持仓事实**，却让 8 只标的从 ``untracked``（如实列出、不计差异）
        升级成 ``missing_side``（critical + halt）→ 演练/清库后的存量持仓天天把链锁死。
        """
        for symbol in ("SH.600089", "SZ.002131"):
            self._order(f"c-{symbol}", symbol, side="SELL", qty=500, broker_id=None,
                        status="cancelled", plan_id="PLN-20260918-sim-6815",
                        created_at="2026-09-18 20:50:24")
        broker = _FakeBroker(
            positions_by_market={3: [{"symbol": "600089", "qty": 5200},
                                     {"symbol": "002131", "qty": 6700}]})
        result = self._daily(broker)
        self.assertEqual(result["diffs"], [], result["diffs"])
        self.assertFalse(result["halted"])
        self.assertFalse(store.is_halted(self.conn))
        self.assertEqual(result["untracked"], ["SH.600089", "SZ.002131"])

    def test_draft_order_does_not_grant_position_knowledge(self):
        """``draft``（本地刚生成、从未提交）同样不建立持仓知识。"""
        self._order("d-1", "SH.600000", side="BUY", qty=3300, broker_id=None,
                    status="draft", plan_id="PLN-20260918-SIM-12FB")
        broker = _FakeBroker(positions_by_market={3: [{"symbol": "600000", "qty": 100}]})
        result = self._daily(broker)
        self.assertEqual(result["diffs"], [])
        self.assertEqual(result["untracked"], ["SH.600000"])
        self.assertFalse(result["halted"])

    def test_sell_only_backfilled_footprint_is_untracked_not_a_diff(self):
        """真机港股形状：本地只有**减仓腿**（回填自券商卖出单）→ 不计差异，分类为 untracked。

        夹具 = 券商 09-14 的三张真实卖出单（``side=2``、已全部成交）+ 当时券商持仓：
        ``HK.00100`` 仍持 200（卖出只是「减200」）、``HK.00981`` 仍持 1000（「减1000」）、
        ``HK.02513`` 已清仓（平）。本地库被清空 + 建仓腿在券商 30 天窗口之外 → 本地净持仓
        为 −200/−1000/−100。这不是「两侧事实分歧」，而是**本地账本不完整**：本地没有观察到
        过建仓，无从主张持仓 → 券商侧仓位如实进 ``untracked``；本地那个无法支撑的主张进
        ``local_unbacked``（如实列出、不计差异、不 halt）。
        """
        for cid, broker_id, symbol, qty in (
                ("imp-00100", "7138918", "HK.00100", 200),
                ("imp-00981", "7138921", "HK.00981", 1000),
                ("imp-02513", "7138916", "HK.02513", 100)):
            self._order(cid, symbol, side="SELL", qty=qty, broker_id=broker_id,
                        status="filled", plan_id="reconcile-import",
                        created_at="2026-09-14 00:00:00")
            self._fill(f"bf-{cid}", cid, qty, 100.0)
        broker = _FakeBroker(
            positions_by_market={1: [{"symbol": "00100", "qty": 200},
                                     {"symbol": "00981", "qty": 1000}]})
        result = self._daily(broker)
        self.assertEqual(result["diffs"], [], result["diffs"])
        self.assertFalse(result["halted"])
        self.assertFalse(store.is_halted(self.conn))
        # 券商侧仓位：如实列出、不计差异
        self.assertEqual(result["untracked"], ["HK.00100", "HK.00981"])
        # 本地侧无法支撑的主张：同样如实列出（HK.02513 券商已平，只在这里可见）
        self.assertEqual(result["local_unbacked"], ["HK.00100", "HK.00981", "HK.02513"])

    def test_real_one_sided_missing_still_halts(self):
        """**反向约束**：本地认为已平仓（买入 + 卖出成对）、券商仍持有 → 仍是 critical。

        这条是「不得为了消差异而削弱检测」的守卫：本地有**建仓成交**即重新进入比对，
        净持仓为 0（无条目）vs 券商有量 → ``missing_side`` → critical + halt。
        """
        self._order("b-1", "SH.600519", side="BUY", qty=100, broker_id="B-1")
        self._fill("f-buy", "b-1", 100, 10.0)
        self._order("s-1", "SH.600519", side="SELL", qty=100, broker_id="B-2")
        self._fill("f-sell", "s-1", 100, 11.0)
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", side=1,
                                                         qty="100", cum_qty="100"),
                                         self._sim_order("B-2", "600519", side=2,
                                                         qty="100", cum_qty="100")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        result = self._daily(broker)
        self.assertEqual(self._kinds(result), ["missing_side"], result["diffs"])
        self.assertIsNone(result["diffs"][0]["local"])
        self.assertEqual(result["diffs"][0]["broker"], {"qty": 100})
        self.assertTrue(result["halted"])
        self.assertTrue(store.is_halted(self.conn))
        self.assertEqual(result["untracked"], [])

    def test_broker_contacted_order_without_fills_still_compares(self):
        """本地已记「买入成交过」但 fills 缺失（券商未给均价）→ 缺口必须继续暴露（宁缺毋假）。

        守卫「不要把足迹收窄成只认 fills」：``unknown``（传输失败，可能已成交）与
        ``filled``（本地数量事实）的**买入**订单都算持仓主张，不得因为回填跳过而把差异
        藏进 untracked。
        """
        self._order("u-1", "SH.600519", broker_id="B-U", qty=100, status="unknown")
        broker = _FakeBroker(
            # 券商那张单还在途（cum_qty=0）：券商侧 100 股是**更早**建的仓，与本单无关
            orders_by_market={"SIM-SH": [self._sim_order("B-U", "600519", qty="100",
                                                         cum_qty="0", status=2)]},
            positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        result = self._daily(broker)
        self.assertEqual(self._kinds(result), ["missing_side"], result["diffs"])
        self.assertTrue(result["halted"])

    def test_filled_sell_order_does_not_grant_position_knowledge(self):
        """**卖出**订单（本地相信自己卖过）不是持仓主张：本地从未观察到建仓。

        与上面一条对偶：本地只有卖出主张时不升级成比对对象——否则「卖存量老仓」这件事本身
        （自动流水线的目标外清出、以及清库后回填出来的减仓腿）就会把链熔断。
        夹具：券商那张卖单已全部成交但**没给成交均价** → 回填如实跳过、订单级也无可判矛盾，
        此时本地唯一的记录就是那张 ``filled`` 卖单。
        """
        self._order("s-1", "SH.600089", side="SELL", qty=5200, broker_id="B-S",
                    status="filled", plan_id="PLN-CLEAR")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-S", "600089", side=2,
                                                         qty="5200", cum_qty="5200",
                                                         status=4, avg_fill_price=None,
                                                         price="18.75")]},
            positions_by_market={3: [{"symbol": "600089", "qty": 5200}]})
        result = self._daily(broker)
        self.assertEqual(result["diffs"], [], result["diffs"])
        self.assertFalse(result["halted"])
        self.assertEqual(result["untracked"], ["SH.600089"])
        self.assertEqual(result["fills_backfilled"]["count"], 0)   # 无均价 → 不编造成交
        self.assertEqual(result["local_unbacked"], [])

    def test_digest_and_latest_expose_local_unbacked(self):
        """本地无法支撑的主张要能被运维看到：digest 计数 + ``reconcile:latest`` 明细。"""
        self._order("imp-1", "HK.02513", side="SELL", qty=100, broker_id="7138916",
                    status="filled", plan_id="reconcile-import",
                    created_at="2026-09-14 00:00:00")
        self._fill("bf-1", "imp-1", 100, 729.0)
        broker = _FakeBroker(orders_by_market={"SIM-HK": []}, positions_by_market={1: []})
        result = self._daily(broker)
        self.assertEqual(result["local_unbacked"], ["HK.02513"])
        digest = store.kv_get(self.conn, "daily:digest")
        self.assertEqual(digest["local_unbacked"], 1)
        latest = store.kv_get(self.conn, "reconcile:latest")
        self.assertEqual(latest["local_unbacked"], ["HK.02513"])
        self.assertEqual(latest["diffs"], [])
        self.assertEqual(digest["diffs"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
