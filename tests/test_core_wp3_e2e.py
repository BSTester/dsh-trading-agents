"""WP3 端到端：规格 §6.2 状态机全景 + 熔断/kill 语义。全部假 broker。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import execute, oms, risk, store  # noqa: E402


class E2eTest(unittest.TestCase):
    def test_full_state_machine(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,"
                     "content_hash,status,created_at) VALUES('P1','2026-09-13','SIM',"
                     "'s','{}','h1','frozen','t')")
        conn.commit()
        for i, sym in enumerate(("SH.600519", "SZ.300750", "SH.601899")):
            o = oms.register_order(conn, "P1", sym, sym.split(".")[0], "BUY",
                                   100, 100.0 + i, "SIM", "h1")
            oms.transition(conn, o["client_order_id"], "frozen")

        calls = {"n": 0}

        def broker_call(name, args, timeout=30):
            calls["n"] += 1
            if calls["n"] == 2:
                raise TimeoutError("sim timeout")   # 第二单 → unknown
            return {"order_id": str(710000 + calls["n"])}

        ctx = {"mode": "SIM", "kill_path": "/nonexistent", "equity": 1_000_000.0,
               "positions_value": {}, "positions_count": 0, "day_pnl_pct": -0.001,
               "is_trading_day": True,
               "config": {"risk_per_trade": 0.01, "max_positions": 5,
                          "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03}}
        result = execute.run(conn, "P1", "h1", ctx, broker_call,
                             price_of=lambda s: 100.0, stop_dist_of=lambda s: 5.0)
        self.assertEqual(result["submitted"], 2)
        self.assertEqual(result["blocked"], 0)
        # unknown 迁出：查询后转 submitted
        unk = [o for o in store.get_orders_by_plan(conn, "P1") if o["status"] == "unknown"]
        self.assertEqual(len(unk), 1)
        oms.transition(conn, unk[0]["client_order_id"], "submitted", broker_order_id="719999")

        # kill switch 生效后：新预检全拒
        with tempfile.TemporaryDirectory() as d:
            kill = Path(d) / "trading-kill"
            kill.write_text("")
            v = risk.pre_trade_checks(
                {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 100.0,
                 "mode": "SIM", "plan_id": "P1", "plan_hash": "h1", "stop_dist": 5.0},
                dict(ctx, kill_path=str(kill), plan_hash="h1", plan_status="frozen"))
            self.assertFalse(v.allowed)
            self.assertEqual(v.rule, 1)

    def test_halt_cancels_remaining_orders(self):
        """规格规则 7 后半：熔断 → 撤计划内剩余未提交订单 + 置 halt。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,"
                     "content_hash,status,created_at) VALUES('P1','2026-09-13','SIM',"
                     "'s','{}','h1','frozen','t')")
        conn.commit()
        for sym in ("SH.600519", "SZ.300750"):
            o = oms.register_order(conn, "P1", sym, sym.split(".")[0], "BUY",
                                   100, 100.0, "SIM", "h1")
            oms.transition(conn, o["client_order_id"], "frozen")
        ctx = {"mode": "SIM", "kill_path": "/nonexistent", "equity": 1_000_000.0,
               "positions_value": {}, "positions_count": 0,
               "day_pnl_pct": -0.031,  # 已超 3% 熔断线
               "is_trading_day": True,
               "config": {"risk_per_trade": 0.01, "max_positions": 5,
                          "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03}}
        result = execute.run(conn, "P1", "h1", ctx,
                             broker_call=lambda name, args, timeout=30: {"order_id": "1"},
                             price_of=lambda s: 100.0, stop_dist_of=lambda s: 5.0)
        self.assertTrue(result["halted"])
        self.assertTrue(store.is_halted(conn))
        statuses = {o["symbol"]: o["status"] for o in store.get_orders_by_plan(conn, "P1")}
        self.assertEqual(set(statuses.values()), {"cancelled"})  # 余单全部撤销
        checks = store.risk_checks_by_plan(conn, "P1")
        self.assertTrue(all(c["rule"] == 7 and not c["allowed"] for c in checks))


if __name__ == "__main__":
    unittest.main(verbosity=2)
