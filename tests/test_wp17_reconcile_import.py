"""WP17-B：对账收编券商独有订单（``missing_in_oms`` 的收敛路径）。

背景（2026-09-17/18 实机）：券商侧存在一张本地从未登记的历史探针单
（``7147945``/``SH.603993``，已撤、零成交）→ 每日对账报 ``missing_in_oms`` → critical +
``set_halt`` → 自动闭环天天被自己的历史差异锁死；而 ``oms-align`` 要求本地已有该单号，
**没有收敛入口**。本文件钉住：

* ③ **收编**（``reconcile._import_broker_only_orders``）：只在已发布枚举内导入、幂等、
  只读券商、来源可辨；收编后重新匹配 → 不再 critical/halt；
* ③b 两条配套口径（缺一条就收敛不掉）：收编行按**对账日期**落 ``created_at``（不是墙钟，
  否则重放历史日期时刚收编的行落在匹配窗口外）；收编行**不建立持仓知识**（零成交的收编单
  不得把历史存量持仓从 ``untracked`` 升级成 ``missing_side``，有成交的经 fills 回填照常
  进足迹）。

计划侧的现金封顶与结构性预警见 ``test_wp17_cash_cap.py``。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from trading_core import reconcile, store  # noqa: E402

TODAY = "2026-09-16"
#: 券商只读工具白名单：收编路径**只读不写**（出现任何写类工具即失败）
READ_TOOLS = {"sim_trade_account_list", "sim_trade_position_list",
              "sim_trade_history_order_list", "sim_trade_cash_info"}


# ============================================================================
# ③ 券商独有订单收编（reconcile.daily：missing_in_oms 的收敛路径）
# ============================================================================
class _FakeBroker:
    """假券商通道：记录每次调用（工具名 + 参数），按市场返回种子数据。"""

    def __init__(self, orders_by_market=None, positions_by_market=None):
        self.orders = orders_by_market or {}
        self.positions = positions_by_market or {}
        self.calls = []

    def __call__(self, name, args=None, timeout=None):
        self.calls.append({"tool": name, "args": dict(args or {})})
        if name == "sim_trade_account_list":
            return {"accounts": [{"account_id": "SIM-SH", "market_id": 3}]}
        if name == "sim_trade_position_list":
            return {"positions": self.positions.get((args or {}).get("market"), [])}
        if name == "sim_trade_history_order_list":
            return {"orders": self.orders.get((args or {}).get("acc_id"), [])}
        if name == "sim_trade_cash_info":
            return {"total_asset": 1_000_000.0}
        raise AssertionError(f"未预期的券商工具：{name}")

    def tools(self):
        return [c["tool"] for c in self.calls]


class ReconcileImportTest(unittest.TestCase):
    """券商有、OMS 无的订单 → 收编进台账 → 不再 critical/halt（自动链不再自锁）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        (self.home / "trading-account-mode").write_text("sim", encoding="utf-8")

    def _sim_order(self, order_id, symbol, side=1, qty="100", cum_qty="0", status=5,
                   price="10.0", avg_fill_price=None):
        """券商订单行（TOOL-LIMITS 实测：裸代码 + 字符串数量 + 整数状态码）。

        默认 ``status=5``（已撤）+ ``cum_qty=0``：**无成交**的终态单——收编只需状态与
        数量事实，不涉及成交回填，最干净地暴露「收编是否收敛」本身。
        """
        return {"order_id": order_id, "symbol": symbol, "side": side, "qty": qty,
                "cum_qty": cum_qty, "price": price, "avg_fill_price": avg_fill_price,
                "status": status, "create_time": "1768550082000000"}

    def _daily(self, broker):
        return reconcile.daily(self.conn, str(self.home), broker_call=broker,
                               today=TODAY)

    def _titles(self, level=None):
        rows = [dict(r) for r in self.conn.execute(
            "SELECT level, title, detail FROM alerts ORDER BY id").fetchall()]
        return [a["title"] for a in rows if level is None or a["level"] == level]

    def _imported_rows(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT client_order_id, plan_id, symbol, side, qty, status,"
            " broker_order_id, err, created_at FROM orders ORDER BY rowid").fetchall()]

    def test_broker_only_order_converges_without_diff_or_halt(self):
        """修复前的错态：券商独有的已撤单 → missing_in_oms → critical + halt（天天自锁）。"""
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("7147945", "603993",
                                                         qty="100", status=5)]},
            positions_by_market={3: []})

        result = self._daily(broker)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["diffs"], [])
        self.assertFalse(result["halted"])
        self.assertFalse(store.halt_summary(self.conn)["halted"])
        self.assertIn("订单导入：券商独有", self._titles("warn"))
        self.assertNotIn("对账差异", self._titles("critical"))
        self.assertEqual(result["digest"]["orders_imported"], 1)
        self.assertEqual(result["digest"]["import_skipped"], 0)
        # 收编是记账：来源可辨（专用 plan_id + err 带券商编号与原始状态码）
        rows = self._imported_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["plan_id"], reconcile.IMPORT_PLAN_ID)
        self.assertEqual(rows[0]["broker_order_id"], "7147945")
        self.assertEqual(rows[0]["symbol"], "SH.603993")
        self.assertEqual(rows[0]["status"], "cancelled")
        self.assertIn("reconcile-import:", rows[0]["err"])
        self.assertIn("7147945", rows[0]["err"])
        self.assertTrue(rows[0]["created_at"].startswith(TODAY),
                        rows[0]["created_at"])
        # 只读券商：收编路径不得产生任何写类调用
        self.assertTrue(set(broker.tools()) <= READ_TOOLS, broker.tools())

    def test_import_is_idempotent(self):
        """连跑两次：不重复落库、第二次不再报导入、差异仍为零（收敛是稳态）。"""
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("7147945", "603993")]},
            positions_by_market={3: []})

        first = self._daily(broker)
        second = self._daily(_FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("7147945", "603993")]},
            positions_by_market={3: []}))

        self.assertEqual(first["digest"]["orders_imported"], 1)
        self.assertEqual(second["digest"]["orders_imported"], 0)
        self.assertEqual(second["diffs"], [])
        self.assertFalse(second["halted"])
        self.assertEqual(len(self._imported_rows()), 1)
        self.assertEqual(self._titles("warn").count("订单导入：券商独有"), 1)

    def test_unpublished_status_is_not_guessed_and_stays_a_diff(self):
        """表外状态码 → 不导入（不猜状态），仍按 missing_in_oms 暴露 + critical + halt。"""
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("9999999", "603993",
                                                         status=99)]},
            positions_by_market={3: []})

        result = self._daily(broker)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["digest"]["orders_imported"], 0)
        self.assertEqual(result["digest"]["import_skipped"], 1)
        self.assertEqual([d["kind"] for d in result["diffs"]], ["missing_in_oms"])
        self.assertTrue(result["halted"])
        self.assertIn("对账差异", self._titles("critical"))
        self.assertEqual(self._imported_rows(), [])

    def test_broker_order_without_id_is_not_imported(self):
        """无订单号的券商行 → 无法建确定性指纹 → 不导入（留作差异，不静默吞）。"""
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("", "603993")]},
            positions_by_market={3: []})

        result = self._daily(broker)

        self.assertEqual(result["digest"]["orders_imported"], 0)
        self.assertEqual(result["digest"]["import_skipped"], 1)
        self.assertEqual([d["kind"] for d in result["diffs"]], ["missing_in_oms"])

    def test_import_does_not_upgrade_legacy_holding_into_a_position_diff(self):
        """真机实况（2026-09-18 复盘）：收编行**不得**改变持仓级的「本地是否知道该标的」。

        实机：券商持 ``SH.603993`` 2100 股（历史存量，OMS 无任何成交记录）→ 此前按
        ``untracked`` 如实列出、**不计差异**。收编那张已撤且零成交的 ``7147945`` 后，
        该标的因「有一行订单」进了持仓足迹 → 本地 fills 净持仓为 0 → 报 ``missing_side``
        → **又一次** critical + halt：收敛路径用自己的记账产物制造了新差异，闭环照样锁死。

        口径：收编行只回答「这张券商单本地为什么没有」，**不建立本地持仓知识**——持仓
        知识只来自 fills。因此零成交的收编行不改持仓分类；**有成交**的收编单经回填产生
        fills 后照常进入足迹（见 ``test_imported_filled_order_enters_position_footprint``）。
        """
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("7147945", "603993",
                                                         qty="100", status=5)]},
            positions_by_market={3: [{"symbol": "603993", "qty": 2100}]})

        result = self._daily(broker)

        self.assertEqual(result["digest"]["orders_imported"], 1)
        self.assertEqual(result["diffs"], [])            # 不因自己的记账产物产生差异
        self.assertFalse(result["halted"])
        self.assertEqual(result["untracked"], ["SH.603993"])  # 分类不变：仍是历史存量

    def test_imported_filled_order_enters_position_footprint(self):
        """收编单**确有成交** → 回填 fills 后照常进足迹，持仓差异不被放过。"""
        broker = _FakeBroker(
            orders_by_market={"SIM-SH": [self._sim_order("7147946", "600519", qty="400",
                                                         cum_qty="400", status=4,
                                                         avg_fill_price="10.0")]},
            positions_by_market={3: [{"symbol": "600519", "qty": 500}]})

        result = self._daily(broker)

        self.assertEqual(result["digest"]["orders_imported"], 1)
        self.assertEqual(result["digest"]["fills_backfilled"]["count"], 1)
        kinds = {d["kind"] for d in result["diffs"]}
        self.assertIn("qty", kinds, result["diffs"])  # 本地 400 vs 券商 500：如实报

if __name__ == "__main__":
    unittest.main()
