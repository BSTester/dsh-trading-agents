"""OMS 状态机：合法迁移白名单、非法迁移拒绝、幂等（同计划同标的方向唯一在途）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import oms, store  # noqa: E402


class OmsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        conn = self.conn
        conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
                     "status,created_at) VALUES('P1','2026-09-13','SIM','s','{}','a3f8',"
                     "'frozen','t')")
        conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _register(self, **kw):
        params = dict(plan_id="P1", symbol="SH.600519", market="SH", side="BUY",
                      qty=100, price=1580.0, mode="SIM", plan_hash="a3f8")
        params.update(kw)
        return oms.register_order(self.conn, **params)

    def test_register_and_transition(self):
        o = self._register()
        self.assertEqual(o["status"], "draft")
        oms.transition(self.conn, o["client_order_id"], "frozen")
        oms.transition(self.conn, o["client_order_id"], "submitting")
        oms.transition(self.conn, o["client_order_id"], "submitted", broker_order_id="714")
        with self.assertRaises(ValueError):
            oms.transition(self.conn, o["client_order_id"], "draft")  # 非法回退

    def test_idempotent_open_order_per_symbol_side(self):
        self._register()
        with self.assertRaises(oms.DuplicateOpenOrder):
            self._register()  # 同计划同标的同方向唯一在途
        # 反方向不受幂等约束（BUY 在途不妨碍 SELL 登记）
        self._register(symbol="SZ.300750", market="SZ", side="SELL")

    def test_unknown_is_terminal_until_query(self):
        o = self._register()
        oms.transition(self.conn, o["client_order_id"], "frozen")
        # 规格 §6.2：unknown 只发生在提交环节（submitting→unknown，提交超时）；
        # 迁出仅允许查询结果（submitted/partial/filled/cancelled），绝不重发。
        oms.transition(self.conn, o["client_order_id"], "submitting")
        oms.transition(self.conn, o["client_order_id"], "unknown")
        with self.assertRaises(ValueError):
            oms.transition(self.conn, o["client_order_id"], "submitting")  # 不允许重发
        oms.transition(self.conn, o["client_order_id"], "submitted", broker_order_id="715")

    def test_unsubmitted_order_can_be_cancelled(self):
        """规格规则 7「撤计划内剩余订单」：draft/frozen（未提交）可本地撤销。"""
        o = self._register()
        oms.transition(self.conn, o["client_order_id"], "cancelled", err="halt")
        self.assertEqual(store.get_orders_by_plan(self.conn, "P1")[0]["status"], "cancelled")

    def test_terminal_state_has_no_exit(self):
        o = self._register()
        oms.transition(self.conn, o["client_order_id"], "frozen")
        oms.transition(self.conn, o["client_order_id"], "submitting")
        oms.transition(self.conn, o["client_order_id"], "rejected", err="拒单")
        with self.assertRaises(ValueError):
            oms.transition(self.conn, o["client_order_id"], "submitting")


if __name__ == "__main__":
    unittest.main(verbosity=2)
