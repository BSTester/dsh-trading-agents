"""WP9 任务 6b：reconcile-daily（对账 → TCA → digest）。

全部离线：券商通道注入假 call，日期显式注入，OMS 台账直接种子。
覆盖任务 6b 的六组：
  * ① 订单级三类 diff（missing_at_broker / missing_in_oms / status_or_qty_diff）+ 一致零 diff；
  * ② 持仓级：有足迹且一致 → 零 diff；有足迹不一致 → diff；券商有但 OMS 无足迹 → untracked 不计 diff；
  * ③ 有 diff → critical 告警 + halt 置位，且**全程零写类券商调用**（只暂停不平仓）；
  * ④ digest 落 kv 且字段齐全（orders 计数 / diffs / untracked / tca）；
  * ⑤ live 模式 → info 告警 + 跳过 + 零券商调用；
  * ⑥ 券商通道抛错 → ok=False（CLI 退出 1）且**不写** reconcile:latest（不伪造「无差异」）；
  * ⑦ **对账前从券商订单历史回填 fills**（修复 sim 系统性假差异与自锁熔断）：
    回填后本地台账等于券商事实 → 一致即零 diff；真差异不放过；幂等（连跑两次不重复落库）；
    均价不可得 → 不回填、不编造价格（缺口以 qty_diff 如实暴露）；cum_qty=0 不产生 fills；
    历史存量持仓仍是 untracked；
  * ⑧ **按累计成交量推进 OMS 状态**（修复订单级自锁熔断路径）：足额 → filled、
    部分 → partial、cum=0 不动、终态不回退、非法迁移跳过并计数且仍暴露为差异；
    推进后真差异照旧 critical + halt。
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import alerts, cli, oms, reconcile, store  # noqa: E402

TODAY = "2026-09-16"
MODE_FILE = "trading-account-mode"
#: 券商只读工具白名单（本任务只允许这些；任何写类出现即失败）
READ_TOOLS = {"sim_trade_account_list", "sim_trade_position_list",
              "sim_trade_history_order_list", "sim_trade_cash_info"}


class _FakeBroker:
    """假券商通道：记录每次调用（工具名+参数），按市场返回种子数据。"""

    def __init__(self, orders_by_market=None, positions_by_market=None, accounts=None,
                 fail_on=None):
        self.orders = orders_by_market or {}
        self.positions = positions_by_market or {}
        self.accounts = accounts if accounts is not None else [
            {"account_id": "SIM-SH", "market_id": 3},
            {"account_id": "SIM-HK", "market_id": 1},
            {"account_id": "SIM-US", "market_id": 100},
        ]
        self.fail_on = fail_on
        self.calls = []

    def __call__(self, name, args=None, timeout=None):
        self.calls.append({"tool": name, "args": dict(args or {})})
        if self.fail_on and name == self.fail_on:
            raise RuntimeError("通道故障（假件）")
        if name == "sim_trade_account_list":
            return {"accounts": self.accounts}
        if name == "sim_trade_position_list":
            market_id = (args or {}).get("market")
            return {"positions": self.positions.get(market_id, [])}
        if name == "sim_trade_history_order_list":
            acc = (args or {}).get("acc_id")
            return {"orders": self.orders.get(acc, [])}
        if name == "sim_trade_cash_info":
            return {"total_asset": 1_000_000.0}
        raise AssertionError(f"未预期的券商工具：{name}")

    def tools(self):
        return [c["tool"] for c in self.calls]


class ReconcileDailyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        self._set_mode("sim")

    # ---- 种子工具 ----

    def _set_mode(self, mode):
        (self.home / MODE_FILE).write_text(mode, encoding="utf-8")

    def _order(self, cid, symbol, side="BUY", qty=100, price=10.0, broker_id=None,
               status="submitted", mode="sim", created_at=None, plan_id="PLN-1"):
        """直接种 OMS 订单行（不经过状态机：对账只读台账，不校验迁移合法性）。"""
        now = created_at or f"{TODAY} 09:35:00"
        self.conn.execute(
            "INSERT INTO orders(client_order_id,plan_id,symbol,market,side,qty,price,"
            "status,broker_order_id,mode,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, plan_id, symbol, symbol.split(".", 1)[0], side, qty, price, status,
             broker_id, mode, now, now))
        self.conn.commit()
        return cid

    def _fill(self, fill_id, cid, qty, price=10.0, created_at=None):
        self.conn.execute(
            "INSERT INTO fills(fill_id,client_order_id,price,qty,traded_at,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (fill_id, cid, price, qty, None, created_at or f"{TODAY} 09:36:00"))
        self.conn.commit()

    def _sim_order(self, order_id, symbol, side=1, qty="100", cum_qty="100", status=4,
                   price="10.0", avg_fill_price="10.0"):
        """券商订单行形状（TOOL-LIMITS 实测口径：裸代码 + 字符串数量 + 整数状态码）。

        ``price``（委托价）/``avg_fill_price``（成交均价）可传 None：用于构造
        「成交明细不可得」的退化场景（回填必须如实跳过而非编造价）。
        """
        return {"order_id": order_id, "symbol": symbol, "side": side, "qty": qty,
                "cum_qty": cum_qty, "price": price, "avg_fill_price": avg_fill_price,
                "status": status, "create_time": "1768550082000000"}

    def _fill_count(self):
        return self.conn.execute("SELECT COUNT(*) AS n FROM fills").fetchone()["n"]

    def _order_status(self, cid):
        return self.conn.execute("SELECT status FROM orders WHERE client_order_id=?",
                                 (cid,)).fetchone()["status"]

    def _levels(self):
        return [r["level"] for r in self.conn.execute(
            "SELECT level FROM alerts ORDER BY id").fetchall()]

    def _kinds(self, diffs):
        counts = {}
        for d in diffs:
            counts[d["kind"]] = counts.get(d["kind"], 0) + 1
        return counts

    # ---- ① 订单级三类 diff ----

    def test_order_level_three_diff_kinds(self):
        self._order("o-miss", "SH.600519", broker_id="B-MISS", qty=100)   # 券商无此单
        self._order("o-match", "SH.600519", broker_id="B-1", qty=100)     # 完全一致
        self._order("o-qty", "SH.600519", broker_id="B-2", qty=200)       # 委托数量不一致
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [
                self._sim_order("B-1", "600519", qty="100", cum_qty="0"),   # 在途未成
                self._sim_order("B-2", "600519", qty="300", cum_qty="0"),
                self._sim_order("B-3", "600519", qty="50", cum_qty="0"),    # OMS 无此单
            ]},
            positions_by_market={3: []})

        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)

        self.assertTrue(result["ok"], result)
        counts = self._kinds(result["diffs"])
        self.assertEqual(counts.get("missing_at_broker"), 1, result["diffs"])
        self.assertEqual(counts.get("missing_in_oms"), 1, result["diffs"])
        self.assertEqual(counts.get("status_or_qty_diff"), 1, result["diffs"])
        # 强匹配优先：o-match 的 B-1 不得被 o-miss 的弱匹配抢走（否则两侧同时误报）
        missing = next(d for d in result["diffs"] if d["kind"] == "missing_at_broker")
        self.assertEqual(missing["client_order_id"], "o-miss")
        self.assertEqual(missing["match"], "id")
        # 差异里必须带得上两方原文值（可核对）
        qty_diff = next(d for d in result["diffs"] if d["kind"] == "status_or_qty_diff")
        self.assertEqual(qty_diff["broker_qty"], 300)
        self.assertEqual(qty_diff["oms_qty"], 200)
        self.assertIn("broker_status_raw", qty_diff)

    def test_order_level_consistent_is_clean(self):
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="filled")
        broker = _FakeBroker(orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519")]},
                             positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        self._fill("F1", "o-1", 100)
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["diffs"], [])

    def test_broker_filled_advances_oms_status_not_a_diff(self):
        """⑧ 语义变更（WP9 订单级自锁熔断修复）：券商累计成交已足额 → 对账**先学习
        券商事实**（推进 OMS 到 ``filled``），再判差异——因此本场景是「同步」而非「差异」。

        变更前的期望（保留在此供对照，勿回退）：判 ``status_or_qty_diff``
        （reason「券商累计成交已足额但 OMS 未记成交」）+ critical + ``set_halt``。
        但 sim 通道的成交不经 WS 交易事件通道，OMS 状态必然停在 ``submitted``——
        纯比对会把「本地尚未学习」系统性误报为差异，每日 halt 把自动执行**永久熔断**
        （模拟盘全自动的前提因此不成立，实现期实测证实）。对账的职责是先同步券商事实、
        再判差异：只有同步后仍存在的差异才是真差异。
        """
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="submitted")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="100")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        # 同步消解了该场景：零差异、零 critical、不 halt
        self.assertEqual(result["diffs"], [], result["diffs"])
        self.assertFalse(result["halted"])
        self.assertFalse(store.is_halted(self.conn))
        self.assertNotIn("critical", self._levels())
        # 状态确实被券商事实推进（数量口径：cum 100 >= 委托 100）
        self.assertEqual(result["orders_advanced"]["count"], 1)
        self.assertEqual(result["orders_advanced"]["skipped"], 0)
        step = result["orders_advanced"]["orders"][0]
        self.assertEqual((step["from"], step["to"]), ("submitted", "filled"))
        self.assertEqual((step["broker_cum_qty"], step["oms_qty"]), (100, 100))
        self.assertEqual(self._order_status("o-1"), "filled")
        # 摘要进 digest（流程页/运维可见），且全程零写类券商调用
        self.assertEqual(store.kv_get(self.conn, "daily:digest")["orders_advanced"],
                         {"count": 1, "skipped": 0})
        self.assertTrue(set(broker.tools()) <= READ_TOOLS, broker.tools())

    def test_partial_fill_advances_to_partial_without_diff(self):
        """⑧ 部分成交 → OMS ``partial``；数量与 fills 一致 → 零差异。"""
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="submitted")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="30")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 30}]})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(self._order_status("o-1"), "partial")
        self.assertEqual(result["orders_advanced"]["count"], 1)
        self.assertEqual(result["orders_advanced"]["orders"][0]["to"], "partial")
        # 回填 30 股 → 本地净持仓 == 券商持仓 → 零差异
        self.assertEqual(reconcile.local_net_positions(self.conn), {"SH.600519": 30})
        self.assertEqual(result["diffs"], [], result["diffs"])
        self.assertFalse(store.is_halted(self.conn))

    def test_zero_cum_qty_does_not_touch_status(self):
        """⑧ 券商累计成交 0（在途未成）→ OMS 状态不动、零差异。"""
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="submitted")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="0")]},
            positions_by_market={3: []})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["orders_advanced"]["count"], 0)
        self.assertEqual(self._order_status("o-1"), "submitted")
        self.assertEqual(result["diffs"], [], result["diffs"])
        self.assertFalse(store.is_halted(self.conn))

    def test_terminal_filled_order_is_not_retransitioned(self):
        """⑧ 只前进不回退：已 ``filled`` 的订单不再迁移（且不算作跳过噪音）。"""
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="filled")
        self._fill("F1", "o-1", 100)
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="100")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["orders_advanced"]["count"], 0)
        self.assertEqual(result["orders_advanced"]["skipped"], 0)
        self.assertEqual(result["orders_advanced"]["orders"], [])
        self.assertEqual(self._order_status("o-1"), "filled")
        self.assertEqual(result["diffs"], [], result["diffs"])

    def test_illegal_transition_is_skipped_counted_and_still_a_diff(self):
        """⑧ 非法迁移不崩、不掩盖、**不绕开状态机**：``submitting → filled`` 不在
        ``oms.TRANSITIONS`` 白名单（须先经 ``submitted``）→ 捕获跳过并计数；该订单随后
        仍被数量矛盾判据暴露为真差异（critical + halt，保守方向：宁可停下来给人看，
        也不用「多步走」替状态机猜路径）。
        """
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="submitting")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="100")]},
            positions_by_market={3: []})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["orders_advanced"]["count"], 0)
        self.assertEqual(result["orders_advanced"]["skipped"], 1)
        skipped = result["orders_advanced"]["skipped_orders"][0]
        self.assertEqual((skipped["from"], skipped["to"]), ("submitting", "filled"))
        self.assertIn("非法迁移", skipped["reason"])
        self.assertEqual(self._order_status("o-1"), "submitting")   # 状态未被硬改
        # 同步消解不了 → 差异照旧（不掩盖）
        kinds = self._kinds(result["diffs"])
        self.assertEqual(kinds.get("status_or_qty_diff"), 1, result["diffs"])
        self.assertTrue(store.is_halted(self.conn))

    def test_real_position_diff_still_halts_after_advance(self):
        """⑧ 同步不掩盖真差异：券商 cum 100 但持仓 200 ≠ 本地 100 → critical + halt。"""
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="submitted")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="100")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 200}]})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(self._order_status("o-1"), "filled")       # 订单级同步成功
        pos_diffs = [d for d in result["diffs"] if d["kind"] == "qty"]
        self.assertEqual(len(pos_diffs), 1, result["diffs"])
        self.assertEqual((pos_diffs[0]["local"], pos_diffs[0]["broker"]), (100, 200))
        self.assertTrue(result["halted"])
        self.assertTrue(store.is_halted(self.conn))

    def test_missing_in_oms_survives_advance(self):
        """⑧ 券商有单、OMS 完全无对应行 → 同步无从推进（无本地行可改）→ 仍 missing_in_oms。"""
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-9", "600519", qty="100",
                                                         cum_qty="100")]},
            positions_by_market={3: []})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["orders_advanced"]["count"], 0)
        kinds = self._kinds(result["diffs"])
        self.assertEqual(kinds.get("missing_in_oms"), 1, result["diffs"])
        self.assertTrue(store.is_halted(self.conn))

    # ---- ② 持仓级 ----

    def test_position_level_footprint_and_untracked(self):
        self._order("o-a", "SH.600519", broker_id="B-A", qty=400)
        self._fill("F-a", "o-a", 400)
        self._order("o-b", "SZ.300750", broker_id="B-B", qty=300)
        self._fill("F-b", "o-b", 300)
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-A", "600519", qty="400"),
                                         self._sim_order("B-B", "300750", qty="300")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 400},   # 一致
                                     {"symbol": "300750", "qty": 400},   # 数量差异
                                     {"symbol": "002594", "qty": 500}]})  # 无 OMS 足迹
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        pos_diffs = [d for d in result["diffs"] if d["kind"] in ("qty", "missing_side", "value")]
        self.assertEqual(len(pos_diffs), 1, result["diffs"])
        self.assertEqual(pos_diffs[0]["symbol"], "SZ.300750")
        self.assertEqual(pos_diffs[0]["local"], 300)
        self.assertEqual(pos_diffs[0]["broker"], 400)
        # 券商有持仓但 OMS 无足迹：如实列出，**不计差异**
        self.assertEqual(result["untracked"], ["SZ.002594"])

    # ---- ③ 差异 → critical 告警 + halt，且零写类调用 ----

    def test_diff_triggers_alert_and_halt_without_writes(self):
        self._order("o-miss", "SH.600519", broker_id="B-MISS", qty=100)
        broker = _FakeBroker(orders_by_market={"SIM-SH": []}, positions_by_market={3: []})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertTrue(result["diffs"])
        self.assertTrue(store.is_halted(self.conn))          # 只暂停后续执行
        self.assertTrue(result["halted"])
        rows = self.conn.execute(
            "SELECT level, title FROM alerts ORDER BY id").fetchall()
        self.assertIn(("critical", "对账差异"),
                      [(r["level"], r["title"]) for r in rows])
        # 铁律：对账只读、绝不自动平仓（无任何写类券商调用）
        self.assertTrue(set(broker.tools()) <= READ_TOOLS, broker.tools())

    def test_clean_does_not_halt(self):
        broker = _FakeBroker(positions_by_market={3: []})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["diffs"], [])
        self.assertFalse(store.is_halted(self.conn))
        self.assertFalse(result["halted"])

    # ---- ④ digest ----

    def test_digest_kv_fields(self):
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="filled")
        self._order("o-2", "SH.600519", broker_id=None, qty=100, status="cancelled")
        broker = _FakeBroker(orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519")]},
                             positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        self._fill("F1", "o-1", 100)
        reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        digest = store.kv_get(self.conn, "daily:digest")
        self.assertEqual(digest["as_of"], TODAY)
        self.assertEqual(digest["mode"], "sim")
        self.assertEqual(digest["orders"], {"filled": 1, "cancelled": 1})
        self.assertEqual(digest["diffs"], 0)
        self.assertEqual(digest["untracked"], 0)
        self.assertIn("avg_bps", digest["tca"])
        self.assertIn("at", digest)
        latest = store.kv_get(self.conn, "reconcile:latest")
        self.assertEqual(latest["diffs"], [])
        self.assertEqual(latest["untracked"], [])
        self.assertEqual(latest["mode"], "sim")

    # ---- ⑤ live 跳过 ----

    def test_live_mode_skips_without_broker_calls(self):
        self._set_mode("live")
        broker = _FakeBroker()
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertTrue(result["ok"])
        self.assertIn("live", result["skipped"])
        self.assertEqual(broker.tools(), [])
        rows = [(r["level"], r["title"]) for r in self.conn.execute(
            "SELECT level, title FROM alerts ORDER BY id").fetchall()]
        self.assertIn("info", [r[0] for r in rows])
        self.assertIsNone(store.kv_get(self.conn, "reconcile:latest"))

    # ---- ⑥ 通道故障 fail-closed ----

    def test_broker_failure_is_closed_and_writes_nothing(self):
        sentinel = {"diffs": [{"symbol": "SENTINEL"}], "at": "2026-09-01 00:00:00"}
        store.kv_set(self.conn, "reconcile:latest", sentinel)
        broker = _FakeBroker(fail_on="sim_trade_account_list")
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertFalse(result["ok"])
        self.assertIn("通道", result["error"])
        # 不伪造「无差异」：既有 kv 原样保留
        self.assertEqual(store.kv_get(self.conn, "reconcile:latest"), sentinel)
        self.assertIsNone(store.kv_get(self.conn, "daily:digest"))

    # ---- ⑦ 对账前从券商订单历史回填 fills（修复 sim 系统性假差异 + 自锁熔断）----

    def test_backfill_makes_local_ledger_match_broker(self):
        """① 关键修复证据：OMS 有足迹但无任何 fills（sim 常态——成交不经 WS 事件通道）
        + 券商订单历史有已成交单（cum_qty/均价）+ 券商持仓与之一致。

        修复前：本地净持仓恒为 0 → qty_diff → critical → halt（自动执行被对账噪声永久熔断）；
        修复后：回填聚合成交 → 本地 == 券商 → 零 diff、零 critical、不 halt。
        """
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="filled")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="100")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["diffs"], [], result["diffs"])
        self.assertFalse(result["halted"])
        self.assertFalse(store.is_halted(self.conn))
        self.assertNotIn("critical", self._levels())
        # 回填真实发生：一条聚合成交（数量=差额、价格=券商成交均价），并如实标注非逐笔
        self.assertEqual(result["fills_backfilled"]["count"], 1)
        self.assertTrue(result["fills_backfilled"]["aggregate"])
        self.assertIn("聚合成交", result["fills_backfilled"]["note"])
        fills = store.fills_by_order(self.conn, "o-1")
        self.assertEqual([(int(f["qty"]), float(f["price"])) for f in fills], [(100, 10.0)])
        # 本地台账 == 券商事实
        self.assertEqual(reconcile.local_net_positions(self.conn), {"SH.600519": 100})

    def test_backfill_without_avg_price_skips_and_keeps_gap_visible(self):
        """均价/委托价都不可得 → **不回填、不编造价格**：缺口以 qty_diff 如实暴露（宁缺毋假）。

        这条同时是「回填才是消除假差异的原因」的反证：拿掉价格，同场景立刻恢复差异。
        """
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="filled")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="100", price=None,
                                                         avg_fill_price=None)]},
            positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["fills_backfilled"]["count"], 0)
        self.assertEqual(result["fills_backfilled"]["skipped"], 1)
        self.assertEqual(self._fill_count(), 0)
        # 本地净持仓为空 → 该标的一侧缺失，差异以 missing_side 如实暴露（本地 None vs 券商 100）
        pos_diffs = [d for d in result["diffs"]
                     if d["kind"] in ("qty", "missing_side")]
        self.assertEqual(len(pos_diffs), 1, result["diffs"])
        self.assertIsNone(pos_diffs[0]["local"])
        self.assertEqual(pos_diffs[0]["broker"], {"qty": 100})
        self.assertTrue(store.is_halted(self.conn))

    def test_backfill_does_not_mask_real_position_diff(self):
        """② 回填后持仓仍不一致 → qty_diff 且 halt（真差异不放过）。"""
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="filled")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="100")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 200}]})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        pos_diffs = [d for d in result["diffs"] if d["kind"] == "qty"]
        self.assertEqual(len(pos_diffs), 1, result["diffs"])
        self.assertEqual((pos_diffs[0]["local"], pos_diffs[0]["broker"]), (100, 200))
        self.assertTrue(store.is_halted(self.conn))
        self.assertTrue(result["halted"])

    def test_backfill_is_idempotent(self):
        """③ 同一份券商数据连跑两次 → fills 行数不变、净持仓不变、第二次仍零 diff。"""
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="filled")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="100")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        first = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        rows_after_first = self._fill_count()
        net_after_first = reconcile.local_net_positions(self.conn)
        second = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(first["fills_backfilled"]["count"], 1)
        self.assertEqual(second["fills_backfilled"]["count"], 0)   # 差额为 0 → 不重复落库
        self.assertEqual(self._fill_count(), rows_after_first)
        self.assertEqual(reconcile.local_net_positions(self.conn), net_after_first)
        self.assertEqual(second["diffs"], [])

    def test_backfill_counts_only_delta_against_existing_fills(self):
        """既有逐笔成交（WS 路径）不被重复计数：只补 cum_qty 与本地合计的差额。"""
        self._order("o-1", "SH.600519", broker_id="B-1", qty=300, status="partial")
        self._fill("F-partial", "o-1", 100)          # WS 逐笔已记 100
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="300",
                                                         cum_qty="300")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 300}]})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["fills_backfilled"]["count"], 1)
        self.assertEqual(reconcile.local_net_positions(self.conn), {"SH.600519": 300})
        fills = store.fills_by_order(self.conn, "o-1")
        self.assertEqual(sum(int(f["qty"]) for f in fills), 300)
        self.assertEqual(result["diffs"], [])

    def test_zero_cum_qty_orders_produce_no_fills(self):
        """⑤ 未成交（cum_qty=0）的订单不产生 fills。"""
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="submitted")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="0")]},
            positions_by_market={3: []})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["fills_backfilled"]["count"], 0)
        self.assertEqual(self._fill_count(), 0)
        self.assertFalse(store.is_halted(self.conn))

    def test_backfill_leaves_legacy_holdings_untracked(self):
        """④ 历史存量持仓（券商有、订单历史与 fills 皆无）→ untracked、不计差异、不 halt。

        回填只认「有订单历史」的成交，因此不会把平台外建的仓误判成差异。
        """
        broker = _FakeBroker(orders_by_market={"SIM-SH": []},
                             positions_by_market={3: [{"symbol": "002594", "qty": 500}]})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertEqual(result["diffs"], [])
        self.assertEqual(result["untracked"], ["SZ.002594"])
        self.assertEqual(result["fills_backfilled"]["count"], 0)
        self.assertFalse(store.is_halted(self.conn))

    def test_backfill_is_read_only_on_broker_side(self):
        """回填只读券商订单历史：全程零写类券商调用（对账铁律不变）。"""
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="filled")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="100")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        self.assertTrue(set(broker.tools()) <= READ_TOOLS, broker.tools())

    def test_cli_channel_failure_exits_one_and_writes_nothing(self):
        """⑥ 通道失败 → CLI 退出 1 且不写 reconcile:latest（回归：fail-closed）。

        经真实 CLI 路径验证退出码映射：打桩券商通道令其抛错（daily 惰性导入该函数）。
        """
        db = self.home / "cli.sqlite"
        out = io.StringIO()
        with mock.patch("trading_datasource.futu_mcp.call_tool",
                        side_effect=RuntimeError("通道故障（假件）")):
            with contextlib.redirect_stdout(out):
                code = cli.main(["reconcile-daily", "--db", str(db),
                                 "--home", str(self.home), "--today", TODAY])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("通道", out.getvalue())
        conn2 = store.connect(str(db))
        self.addCleanup(conn2.close)
        self.assertIsNone(store.kv_get(conn2, "reconcile:latest"))
        self.assertIsNone(store.kv_get(conn2, "daily:digest"))

    # ---- CLI 层 ----

    def test_cli_live_skips_with_exit_zero(self):
        """live 在券商调用之前跳过：退出 0 + 如实打印跳过原因（零网络）。"""
        self._set_mode("live")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["reconcile-daily", "--db", str(self.home / "cli.sqlite"),
                             "--home", str(self.home), "--today", TODAY])
        self.assertEqual(code, 0, out.getvalue())
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIn("live", payload["skipped"])

    def test_cli_invalid_mode_is_closed(self):
        """模式文件非法：fail-closed 非零退出（不静默按 sim 对账）。"""
        (self.home / MODE_FILE).write_text("bogus", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["reconcile-daily", "--db", str(self.home / "cli.sqlite"),
                             "--home", str(self.home), "--today", TODAY])
        self.assertEqual(code, 1, out.getvalue())
        self.assertIn("账户模式非法", out.getvalue())


if __name__ == "__main__":
    unittest.main()
