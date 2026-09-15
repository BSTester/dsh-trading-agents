"""WP3 依赖锁定：券商工具名与必填参数（tools/list schema 实测口径，2026-09-14）；
store v3（plans/orders/fills/risk_checks）四表迁移与读写函数离线回环。

注：任务 7 的 CLI 子命令测试也落在本文件——tests/test_core_cli.py 是 WP1 既有
文件，不在 WP3 分支的文件所有权清单内（偏差已在 WP3 验收记录披露）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
from trading_core import broker, store  # noqa: E402


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

    def test_schema_version_is_3_with_four_new_tables(self):
        self.assertEqual(store.SCHEMA_VERSION, 3)
        self.assertEqual(self.conn.execute("PRAGMA user_version").fetchone()[0], 3)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
