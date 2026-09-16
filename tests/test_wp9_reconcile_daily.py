"""WP9 任务 6b：reconcile-daily（对账 → TCA → digest）。

全部离线：券商通道注入假 call，日期显式注入，OMS 台账直接种子。
覆盖任务 6b 的六组：
  * ① 订单级三类 diff（missing_at_broker / missing_in_oms / status_or_qty_diff）+ 一致零 diff；
  * ② 持仓级：有足迹且一致 → 零 diff；有足迹不一致 → diff；券商有但 OMS 无足迹 → untracked 不计 diff；
  * ③ 有 diff → critical 告警 + halt 置位，且**全程零写类券商调用**（只暂停不平仓）；
  * ④ digest 落 kv 且字段齐全（orders 计数 / diffs / untracked / tca）；
  * ⑤ live 模式 → info 告警 + 跳过 + 零券商调用；
  * ⑥ 券商通道抛错 → ok=False（CLI 退出 1）且**不写** reconcile:latest（不伪造「无差异」）。
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

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

    def _sim_order(self, order_id, symbol, side=1, qty="100", cum_qty="100", status=4):
        """券商订单行形状（TOOL-LIMITS 实测口径：裸代码 + 字符串数量 + 整数状态码）。"""
        return {"order_id": order_id, "symbol": symbol, "side": side, "qty": qty,
                "cum_qty": cum_qty, "price": "10.0", "avg_fill_price": "10.0",
                "status": status, "create_time": "1768550082000000"}

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

    def test_broker_filled_but_oms_not_recorded_is_diff(self):
        """数量可推导的一类状态不一致：券商累计成交已足额，OMS 却仍记在途。"""
        self._order("o-1", "SH.600519", broker_id="B-1", qty=100, status="submitted")
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("B-1", "600519", qty="100",
                                                         cum_qty="100")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 100}]})
        result = reconcile.daily(self.conn, self.home, broker_call=broker, today=TODAY)
        kinds = self._kinds(result["diffs"])
        self.assertEqual(kinds.get("status_or_qty_diff"), 1, result["diffs"])
        self.assertIn("券商累计成交已足额", result["diffs"][0]["reason"])
        self.assertEqual(result["diffs"][0]["broker_cum_qty"], 100)

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
