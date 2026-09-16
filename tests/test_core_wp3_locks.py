"""WP3 依赖锁定：券商工具名与必填参数（tools/list schema 实测口径，2026-09-14）；
store v3（plans/orders/fills/risk_checks）四表迁移与读写函数离线回环。

注：任务 7 的 CLI 子命令测试也落在本文件——tests/test_core_cli.py 是 WP1 既有
文件，不在 WP3 分支的文件所有权清单内（偏差已在 WP3 验收记录披露）。
"""
import io
import json
import sys
import tempfile
import unittest
import datetime as dt
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
from trading_core import broker, cli, store  # noqa: E402


class Wp3Locks(unittest.TestCase):
    def test_broker_tool_names_locked(self):
        self.assertEqual(broker.TOOLS["place"], "sim_trade_input_order")
        self.assertEqual(broker.TOOLS["cancel"], "sim_trade_cancel_order")
        self.assertEqual(broker.TOOLS["positions"], "sim_trade_position_list")
        self.assertEqual(broker.TOOLS["accounts"], "sim_trade_account_list")
        self.assertEqual(broker.TOOLS["history"], "sim_trade_history_order_list")

    def test_place_required_params(self):
        self.assertEqual(set(broker.PLACE_REQUIRED),
                         {"acc_id", "market", "symbol", "order_type", "order_side", "qty"})


class StoreV3Test(unittest.TestCase):
    """v3 四表迁移 + 任务 0 交付的读写函数逐一回环（后续任务只复用不重测）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_schema_version_is_4_with_four_new_tables(self):
        # WP3 落 3（四表）；WP9 起为 4（plans 幂等补 origin/market 列，见 test_core_store）
        self.assertEqual(store.SCHEMA_VERSION, 4)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 4)
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertTrue({"plans", "orders", "fills", "risk_checks"} <= names)

    def test_plan_roundtrip(self):
        store.insert_plan(self.conn, plan_id="P1", as_of="2026-09-13", mode="SIM",
                          strategy_id="s", target={"SH.600519": 0.5}, content_hash="h1")
        plan = store.get_plan(self.conn, "P1")
        self.assertEqual(plan["status"], "frozen")
        self.assertEqual(plan["target"], {"SH.600519": 0.5})
        self.assertEqual([p["plan_id"] for p in store.list_plans(self.conn)], ["P1"])
        store.upsert_plan_status(self.conn, "P1", "executing")
        self.assertEqual(store.get_plan(self.conn, "P1")["status"], "executing")
        with self.assertRaises(ValueError):
            store.get_plan(self.conn, "NOPE")

    def test_order_roundtrip_open_filter_and_update(self):
        store.insert_order(self.conn, client_order_id="C1", plan_id="P1",
                           symbol="SH.600519", market="SH", side="BUY", qty=100,
                           price=1580.0, mode="SIM")
        rows = store.get_orders_by_plan(self.conn, "P1")
        self.assertEqual([r["client_order_id"] for r in rows], ["C1"])
        self.assertEqual([r["status"] for r in store.get_open_orders(self.conn, "P1")], ["draft"])
        store.update_order_status(self.conn, "C1", "submitted", broker_order_id="714")
        row = store.get_orders_by_plan(self.conn, "P1")[0]
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["broker_order_id"], "714")
        store.update_order_status(self.conn, "C1", "filled")
        self.assertEqual(store.get_open_orders(self.conn, "P1"), [])  # filled 非在途

    def test_fill_roundtrip(self):
        store.insert_fill(self.conn, fill_id="F1", client_order_id="C1",
                          price=1580.0, qty=100, traded_at="2026-09-13 09:31:00")
        fills = store.fills_by_order(self.conn, "C1")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["price"], 1580.0)

    def test_risk_check_roundtrip(self):
        store.insert_risk_check(self.conn, plan_id="P1", symbol="SH.600519",
                                rule=5, allowed=False, reason="超限")
        checks = store.risk_checks_by_plan(self.conn, "P1")
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["rule"], 5)
        self.assertFalse(checks[0]["allowed"])

    def test_halt_uses_kv_key(self):
        self.assertFalse(store.is_halted(self.conn))
        store.set_halt(self.conn, True, reason="daily_loss")
        self.assertTrue(store.is_halted(self.conn))
        self.assertEqual(store.kv_get(self.conn, "halt:active")["reason"], "daily_loss")
        store.clear_halt(self.conn)
        self.assertFalse(store.is_halted(self.conn))


class Wp3CliCommands(unittest.TestCase):
    """plan-build / reconcile-diff 子命令（离线 JSON 口径，规格 §2.3）。

    计划原文把本组测试放在 tests/test_core_cli.py；该文件是 WP1 既有文件、
    不在 WP3 分支所有权清单内，故落在本锁定文件（验收记录披露 D4）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def _seed_bars(self, symbol, close, count=20):
        """铺确定性日线（h=c+0.5 / l=c−0.5 → TR=1.0 → ATR=1.0 → 止损距离=2.0）。

        计划按风险预算定量（规格 §4.2 第 5 点，WP9 缺口修复）：加仓标的必须有可算的
        ATR，否则跳过（宁缺毋假）。本测试验证的是 plan-build 的**冻结 JSON 契约**，
        因此补最小 bar 集让权重臂成为约束臂（期望数量与修复前一致）。
        """
        conn = store.connect(self.db)
        try:
            days = [(dt.date(2026, 8, 31) - dt.timedelta(days=count - 1 - i)).isoformat()
                    for i in range(count)]
            store.upsert_bars(conn, symbol, "1d", [
                {"t": d, "o": close, "h": close + 0.5, "l": close - 0.5, "c": close,
                 "v": 1000.0} for d in days], source="test")
        finally:
            conn.close()

    def test_plan_build_freezes_plan_json(self):
        self._seed_bars("SH.600519", 1580.0)
        out = self._run(["plan-build", "--mode", "SIM", "--strategy", "momentum_value_top5",
                         "--target", '{"SH.600519": 0.5}',
                         "--prices", '{"SH.600519": 1580.0}',
                         "--as-of", "2026-09-13", "--db", self.db])
        self.assertEqual(out["status"], "frozen")
        self.assertEqual(len(out["orders"]), 1)
        order = out["orders"][0]
        self.assertEqual((order["symbol"], order["side"], order["qty"]), ("SH.600519", "BUY", 300))
        conn = store.connect(self.db)
        try:
            self.assertEqual(store.get_plan(conn, out["plan_id"])["content_hash"],
                             out["content_hash"])
        finally:
            conn.close()

    def test_reconcile_diff_reports_qty_mismatch(self):
        local = Path(self.tmp.name) / "local.json"
        broker = Path(self.tmp.name) / "broker.json"
        local.write_text('{"SH.600519": {"qty": 300}}', encoding="utf-8")
        broker.write_text('{"SH.600519": {"qty": 320}}', encoding="utf-8")
        out = self._run(["reconcile-diff", "--local", str(local), "--broker", str(broker),
                         "--db", self.db])
        self.assertEqual(out["diffs"][0]["qty_diff"], -20)
        self.assertEqual(out["diffs"][0]["kind"], "qty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
