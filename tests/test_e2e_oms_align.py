"""S2 遗留收敛（E2E 追加第 2 项，2026-09-17）。

背景：E2E 探针在旧服务上留下 2 条 `orders` 行 `submitted`，而券商侧早已撤单成功
（`status=5`）。S2 修复只让**新的**撤单回写；历史行过去无法经 API 收敛（再撤会被券商以
终态拒绝；`reconcile` 只按累计成交量推进，`cum_qty=0` 不动）→ 永久阻塞同标的同方向新单、
审计链失真。

两条收敛路径都在这里验证：
  * `reconcile.daily` 的**终态对齐**（券商给出官方已发布的字符串终态时自动收敛）；
  * `reconcile.align_terminal` + CLI `oms-align`（对账覆盖不到时的人工留痕路径）。
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _dir in (ROOT / "plugins" / "datasource" / "python",
             ROOT / "plugins" / "core" / "python"):
    sys.path.insert(0, str(_dir))
sys.path.insert(0, str(ROOT / "platform"))

from trading_core import alerts as core_alerts  # noqa: E402
from trading_core import oms, reconcile, store  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(store.db_path(str(self.home)))
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)

    def seed_order(self, status="submitted", broker_order_id="B-1", qty=100):
        self.conn.execute(
            "INSERT INTO orders(client_order_id,plan_id,symbol,market,side,qty,price,"
            "status,broker_order_id,mode,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("CID-1", "ADHOC", "HK.00700", "HK", "BUY", qty, 10.0, status,
             broker_order_id, "sim", "2026-09-17 10:00:00", "2026-09-17 10:00:00"))
        self.conn.commit()

    def status(self):
        return self.conn.execute("SELECT status FROM orders WHERE client_order_id='CID-1'"
                                 ).fetchone()["status"]

    def broker_row(self, order_status, cum=0, qty=100):
        return reconcile._broker_order_rows(
            [{"order_id": "B-1", "symbol": "HK.00700", "side": 1, "qty": qty,
              "dealt_qty": cum, "order_status": order_status}],
            lambda value: value)[0]


class ReconcileTerminalAlignmentTests(_Case):
    def test_cancelled_all_with_no_fill_aligns_to_cancelled(self):
        """核心回归钉：券商 CANCELLED_ALL + 无成交 + 本地 submitted → 本地 cancelled。"""
        self.seed_order()
        pairs = [(dict(self.conn.execute("SELECT * FROM orders").fetchone()),
                  self.broker_row("CANCELLED_ALL"), "strong")]
        out = reconcile._advance_order_states(self.conn, pairs)
        self.assertEqual(self.status(), "cancelled")
        self.assertGreaterEqual(out["count"], 1, out)

    def test_aligned_order_produces_no_diff(self):
        """对齐后不得再报差异（否则每日 critical + halt 的自锁会以另一种形式回来）。"""
        self.seed_order()
        row = dict(self.conn.execute("SELECT * FROM orders").fetchone())
        broker = self.broker_row("CANCELLED_ALL")
        reconcile._advance_order_states(self.conn, [(row, broker, "strong")])
        diffs = reconcile._pair_diffs(row, broker, "strong")
        self.assertEqual(diffs, [], diffs)

    def test_cancelled_part_with_fill_goes_partial_not_cancelled(self):
        """部分成交后撤单：数量口径优先 → partial（不标 cancelled，延续 P1 口径）。"""
        self.seed_order()
        row = dict(self.conn.execute("SELECT * FROM orders").fetchone())
        reconcile._advance_order_states(self.conn, [(row, self.broker_row("CANCELLED_PART", cum=30),
                                                     "strong")])
        self.assertEqual(self.status(), "partial")

    def test_cancelled_all_with_fill_is_resolved_by_quantity_not_cancelled(self):
        """状态与数量矛盾（字符串说「全撤无成交」却有成交）→ **不标 cancelled**。

        数量口径优先（WP8 P1）：有成交就按成交推进到 ``partial``，绝不把有成交的单标成撤单；
        真正的差异交给 `_pair_diffs`（本场景两侧数量一致，故不产生差异）。
        """
        self.seed_order()
        row = dict(self.conn.execute("SELECT * FROM orders").fetchone())
        reconcile._advance_order_states(self.conn, [(row, self.broker_row("CANCELLED_ALL", cum=30),
                                                     "strong")])
        self.assertEqual(self.status(), "partial")

    def test_out_of_table_integer_status_code_is_not_interpreted(self):
        """**表外**整数状态码不得被解释（保守口径：没有契约就不猜）。"""
        self.seed_order()
        row = dict(self.conn.execute("SELECT * FROM orders").fetchone())
        broker = reconcile._broker_order_rows(
            [{"order_id": "B-1", "symbol": "HK.00700", "side": 1, "qty": 100,
              "cum_qty": 0, "status": 99}], lambda value: value)[0]
        reconcile._advance_order_states(self.conn, [(row, broker, "strong")])
        self.assertEqual(self.status(), "submitted", "表外的码不得被解释成终态")

    def test_sim_status_5_is_documented_cancelled_and_aligns(self):
        """官方 `sim-trade/order-list.md` 逐值发布：``5=已撤``——这是契约，不是猜。

        证据来源：官方文档字段说明「status int 2=已提交 3=部分成交 4=全部成交 5=已撤
        6=拒绝」（2026-09-17 核对）。因此模拟交易整数码**可以**解释；表外的码仍然不解释。
        """
        self.seed_order()
        row = dict(self.conn.execute("SELECT * FROM orders").fetchone())
        broker = reconcile._broker_order_rows(
            [{"order_id": "B-1", "symbol": "HK.00700", "side": 1, "qty": 100,
              "cum_qty": "0", "status": 5}], lambda value: value)[0]
        self.assertEqual(broker["status_code"], 5)
        reconcile._advance_order_states(self.conn, [(row, broker, "strong")])
        self.assertEqual(self.status(), "cancelled")

    def test_sim_status_5_with_partial_fill_goes_partial(self):
        """``5=已撤`` 且已有部分成交 → 数量口径给 ``partial``（有成交就不能标撤单）。"""
        self.seed_order()
        row = dict(self.conn.execute("SELECT * FROM orders").fetchone())
        broker = reconcile._broker_order_rows(
            [{"order_id": "B-1", "symbol": "HK.00700", "side": 1, "qty": 100,
              "cum_qty": "30", "status": 5}], lambda value: value)[0]
        reconcile._advance_order_states(self.conn, [(row, broker, "strong")])
        self.assertEqual(self.status(), "partial")

    def test_failed_status_aligns_to_rejected(self):
        self.seed_order(status="submitting")
        row = dict(self.conn.execute("SELECT * FROM orders").fetchone())
        reconcile._advance_order_states(self.conn, [(row, self.broker_row("FAILED"), "strong")])
        self.assertEqual(self.status(), "rejected")

    def test_filled_order_is_never_downgraded_by_terminal_alignment(self):
        self.seed_order(status="filled", broker_order_id="B-9")
        row = dict(self.conn.execute("SELECT * FROM orders").fetchone())
        broker = self.broker_row("CANCELLED_ALL")
        broker["broker_order_id"] = "B-1"
        reconcile._advance_order_states(self.conn, [(row, broker, "strong")])
        self.assertEqual(self.status(), "filled", "终态无出边：不得回退")


class AlignTerminalTests(_Case):
    def test_align_cancelled_writes_alert_with_reason_and_operator(self):
        self.seed_order()
        out = reconcile.align_terminal(self.conn, str(self.home), "B-1", "cancelled",
                                       "券商早已撤单（E2E 探针遗留）", operator="human")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.status(), "cancelled")
        rows = [r for r in core_alerts.list_recent(self.conn, limit=10)
                if r["title"] == "订单终态人工对齐"]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("human", rows[0]["detail"])
        self.assertIn("E2E 探针遗留", rows[0]["detail"])

    def test_refuses_filled_downgrade(self):
        self.seed_order(status="filled")
        out = reconcile.align_terminal(self.conn, str(self.home), "B-1", "cancelled", "想改")
        self.assertFalse(out["ok"], out)
        self.assertIn("非法迁移", out["error"])
        self.assertEqual(self.status(), "filled")

    def test_refuses_unknown_order_without_fabricating(self):
        out = reconcile.align_terminal(self.conn, str(self.home), "GHOST", "cancelled", "对齐")
        self.assertFalse(out["ok"], out)
        self.assertIn("不伪造", out["error"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)

    def test_refuses_non_terminal_target(self):
        self.seed_order()
        for target in ("filled", "partial", "submitted", "draft"):
            with self.subTest(target=target):
                out = reconcile.align_terminal(self.conn, str(self.home), "B-1", target, "x")
                self.assertFalse(out["ok"], out)
        self.assertEqual(self.status(), "submitted")

    def test_requires_reason(self):
        self.seed_order()
        for reason in ("", "   ", None):
            with self.subTest(reason=reason):
                out = reconcile.align_terminal(self.conn, str(self.home), "B-1",
                                               "cancelled", reason)
                self.assertFalse(out["ok"], out)
                self.assertIn("reason", out["error"])
        self.assertEqual(self.status(), "submitted")

    def test_does_not_touch_the_broker(self):
        """纪律：人工对齐只写本地 OMS + 告警，绝不触达券商。"""
        import inspect
        source = inspect.getsource(reconcile.align_terminal)
        for forbidden in ("place", "cancel_order", "call_tool", "broker_call"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_cli_entry_point(self):
        """CLI 边界：拒绝路径返回非零（`__main__` 把它交给 sys.exit）。"""
        from trading_core import cli
        code = cli.main(["oms-align", "--broker-order-id", "GHOST", "--status", "cancelled",
                         "--reason", "x", "--db", str(store.db_path(str(self.home)))])
        self.assertEqual(code, 1, "拒绝路径必须非零退出")

    def test_cli_success_path_aligns_and_exits_zero(self):
        from trading_core import cli
        self.seed_order()
        code = cli.main(["oms-align", "--broker-order-id", "B-1", "--status", "cancelled",
                         "--reason", "券商早已撤单", "--operator", "e2e",
                         "--db", str(store.db_path(str(self.home)))])
        self.assertEqual(code, 0)
        self.assertEqual(self.status(), "cancelled")


if __name__ == "__main__":
    unittest.main()
