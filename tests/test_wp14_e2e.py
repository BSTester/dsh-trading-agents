"""WP14 任务 6：规则提案 → 检验 → 人工批准 → 次日计划 → 自动执行 端到端。

链路里除外部通道（券商）外**全部是真实实现**，没有为测试方便而设的捷径：

  提案 spec（``momentum_20``——**真实注册因子**，喂构造的可行价格路径）
    → ``rules-validate`` CLI（真实验证门：IC t 检验 + 5 分位分层单调）→ ``passed``
    → Web 审批端点 ``rules-decide``（服务 ``create_handler``，等价于独立 Web 的
      「批准」按钮，``by=web``）→ ``enabled`` + 记录批准人
    → 次日 ``plan-auto`` CLI（真实作业体）→ ``origin=auto`` 冻结计划（``strategy_id=rule_id``）
    → ``auto-execute`` CLI（九守卫，D2 09:36 落在执行窗口内）→ 落 ``execute_plan`` 指令
    → ``daemon.poll_commands``（真实指令轮询）→ ``execute.run`` → 券商收到下单

反证与边界与验收标志一一对应：
  * ``passed`` 但**未批准** → ``plan-auto`` 零消费（跳过「规则未启用」）、零计划、零指令；
  * 未验证（``candidate``）规则经 Web 端点 ``enable`` → 拒绝且库零变化
    （「批准是策略上岗的唯一通道」的可执行证明）。
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from server import app as app_module  # noqa: E402
from server import caches, compute  # noqa: E402
from trading_core import cli, daemon, factors, store, strategies  # noqa: E402

D1 = "2026-09-16"          # 收盘链日：生成次日计划
D2 = "2026-09-17"          # 次日开盘后延迟执行
D0 = "2026-09-15"
BARS = 80
SYMBOLS = ["SH.600001", "SH.600002", "SH.600003", "SH.600004", "SH.600005"]
RULE_ID = "wp14_e2e_momentum"
FACTOR = "momentum_20"     # 真实注册因子：越多越看多，无需网络
TOP_N = 3
#: 交换末两位的日期：让 IC 序列有方差（零方差无法做 t 检验——刻意的门槛语义）
SWAP_DAYS = (65, 68, 71)


def _spec(rule_id=RULE_ID, **over):
    spec = {"rule_id": rule_id,
            "hypothesis": "中期价格动量：过去 20 日相对强势的标的未来 2 日继续占优",
            "factors": [FACTOR], "combine": "zscore_equal_weight",
            "universe": "watchlist.SH", "top_n": TOP_N, "rebalance": "weekly",
            "provenance": {"research_run_id": "R-e2e-01", "created_by": "harness",
                           "created_at": "2026-09-16 10:00:00"}}
    spec.update(over)
    return spec


def _price_rows(days=BARS):
    """横截面日收益序与标的序号同向的价格路径（动量序 = 序号序 → IC 为正）。"""
    dates = [(date.fromisoformat(D1) - timedelta(days=days - 1 - i)).isoformat()
             for i in range(days)]
    closes = {symbol: [100.0] for symbol in SYMBOLS}
    for index in range(1, days):
        ranks = list(range(len(SYMBOLS)))
        if index in SWAP_DAYS:
            ranks[-1], ranks[-2] = ranks[-2], ranks[-1]
        for position, symbol in enumerate(SYMBOLS):
            closes[symbol].append(
                round(closes[symbol][-1] * (1 + 0.005 * (ranks[position] + 1)), 4))
    return dates, closes


class ChainBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = str(self.home / "t.sqlite")
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)
        caches.configure(home=str(self.home))
        self.addCleanup(caches.configure)
        (self.home / "trading-risk.json").write_text("{}", encoding="utf-8")
        (self.home / "trading-account-mode").write_text("sim", encoding="utf-8")
        store.upsert_calendar(self.conn, "SH", [
            {"day": day, "trade_date_type": "WHOLE", "trade_second": 14400}
            for day in (D0, D1, D2)])
        self._write_platform(strategy=RULE_ID)
        self.seed_bars()

    # ---- 夹具 ----

    def _write_platform(self, strategy):
        (self.home / "trading-platform.json").write_text(json.dumps({
            "watchlist": list(SYMBOLS),
            "auto_pipeline": {
                "enabled": True,
                "strategies": [{"market": "SH", "strategy": strategy,
                                "watchlist": "watchlist"}],
                "exec_at": {"SH": "09:35"}, "reconcile_at": "19:00"}},
            ensure_ascii=False), encoding="utf-8")

    def seed_bars(self):
        dates, closes = _price_rows()
        for symbol in SYMBOLS:
            store.upsert_bars(self.conn, symbol, "1d", [
                {"t": day, "o": close, "h": close + 0.5, "l": close - 0.5, "c": close,
                 "v": 1000.0} for day, close in zip(dates, closes[symbol])], source="test")

    def write_spec(self, spec=None):
        path = self.home / "spec.json"
        path.write_text(json.dumps(spec or _spec(), ensure_ascii=False), encoding="utf-8")
        return str(path)

    # ---- 真实入口 ----

    def run_cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv + ["--db", self.db])
        return code, buf.getvalue()

    def run_json(self, argv, expect_code=0):
        code, out = self.run_cli(argv)
        self.assertEqual(code, expect_code, out)
        return json.loads(out)

    def validate(self, spec=None):
        return self.run_json(["rules-validate", "--spec", self.write_spec(spec),
                              "--symbols", ",".join(SYMBOLS), "--home", str(self.home),
                              "--lookback", "20", "--horizon", "2"])

    def handler(self):
        """服务 handler：审批端点绑真实 CLI（进程内跑真库）——等价于 Web 批准按钮。"""
        def runner(command, timeout):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.main(list(command[3:]) + ["--db", self.db])
            import subprocess
            return subprocess.CompletedProcess(command, code, buf.getvalue(), "")

        core = {"rules": lambda status=None: compute.rules_list(status, runner=runner),
                "rules-decide": lambda rule_id, decision: compute.rules_decide(
                    rule_id, decision, runner=runner)}
        return app_module.create_handler(str(self.home), analytics={}, series=None,
                                         core=core)

    def approve_web(self, rule_id=RULE_ID):
        return self.handler()("rules-decide", {"rule_id": rule_id, "decision": "enable"})

    def plan_auto(self, today=D1):
        return self.run_json(["plan-auto", "--market", "SH", "--home", str(self.home),
                              "--today", today])

    def auto_execute(self, now=f"{D2} 09:36:00", today=D2):
        return self.run_json(["auto-execute", "--market", "SH", "--home", str(self.home),
                              "--today", today, "--now", now])

    # ---- 券商假件（唯一假的外部通道） ----

    def _broker(self):
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append((name, dict(args or {})))
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3,
                                      "account_title": "模拟A股"}]}
            if name == "sim_trade_position_list":
                return {"positions": []}
            if name == "sim_trade_cash_info":
                return {"balance": 1_000_000.0, "total_asset": 1_000_000.0}
            if name == "sim_trade_input_order":
                return {"order_id": f"SIM-O{len(calls)}"}
            if name == "sim_trade_history_order_list":
                return {"orders": []}
            raise AssertionError(f"意外券商工具 {name}")

        return call, calls

    def poll(self, call):
        with mock.patch("trading_datasource.futu_mcp.call_tool", call):
            return daemon.poll_commands(self.conn, str(self.home))

    # ---- 观测 ----

    def plans(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT plan_id, as_of, mode, status, origin, market, strategy_id"
            " FROM plans ORDER BY rowid").fetchall()]

    def orders(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT symbol, side, qty, status, broker_order_id FROM orders"
            " ORDER BY rowid").fetchall()]

    def pending(self):
        root = self.home / "trading-commands" / "pending"
        return sorted(path.name for path in root.glob("*.json")) if root.exists() else []


class ProposalToOrderChainTest(ChainBase):
    """主链路：一次挖掘轮的完整交付。"""

    def _approve_and_plan(self):
        self.assertEqual(self.validate()["status"], "passed")
        body = self.approve_web()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"]["status"], "enabled")
        return self.plan_auto()

    def test_validation_passes_and_status_chain_is_traced(self):
        report = self.validate()
        self.assertEqual(report["status"], "passed", report)
        detail = report["validation"]["factors"][FACTOR]
        self.assertGreaterEqual(detail["n"], factors.IC_MIN_SAMPLES)
        self.assertTrue(detail["monotonic"])
        self.assertTrue(detail["passes_gate"])
        row = store.find_rule(self.conn, RULE_ID)
        self.assertEqual(row["status"], "passed")
        self.assertIsNone(row["approved_by"], "验证通过不等于批准")

    def test_web_approval_records_approver_and_enables(self):
        self.validate()
        body = self.approve_web()
        self.assertEqual(body["value"]["status"], "enabled")
        row = store.find_rule(self.conn, RULE_ID)
        self.assertEqual(row["status"], "enabled")
        self.assertEqual(row["approved_by"], "web", "批准人必须留痕（谁批的）")
        self.assertTrue(row["approved_at"])

    def test_next_day_plan_uses_the_approved_rule(self):
        result = self._approve_and_plan()
        self.assertTrue(result["ok"], result)
        self.assertIn("plan", result, result)
        plan = self.plans()[0]
        self.assertEqual(plan["origin"], "auto")
        self.assertEqual(plan["market"], "SH")
        self.assertEqual(plan["strategy_id"], RULE_ID, "计划必须记下用了哪条规则")
        self.assertEqual(plan["as_of"], D1)
        self.assertEqual(plan["status"], "frozen")
        self.assertEqual(len(result["plan"]["orders"]), TOP_N,
                         "top_n=3 的规则应生成 3 单")

    def test_auto_execute_writes_command_then_poll_submits_orders(self):
        self._approve_and_plan()
        out = self.auto_execute()
        self.assertTrue(out["ok"], out)
        self.assertIn("nonce", out, out)
        self.assertEqual(len(self.pending()), 1, "自动执行必须落一张指令文件")
        command = json.loads(
            (self.home / "trading-commands" / "pending" / self.pending()[0]).read_text(
                encoding="utf-8"))
        self.assertEqual(command["type"], "execute_plan")
        self.assertEqual(command["expected_mode"], "sim", "复核字段必须随指令带上")

        call, calls = self._broker()
        results = self.poll(call)
        self.assertTrue(results and results[0]["result"]["ok"], results)
        orders = self.orders()
        self.assertEqual(len(orders), TOP_N, orders)
        self.assertTrue(all(order["status"] == "submitted" for order in orders), orders)
        self.assertTrue(all(order["broker_order_id"] for order in orders),
                        "券商订单号必须回填（提交事实来自券商返回，不是本地推断）")
        placed = [args for name, args in calls if name == "sim_trade_input_order"]
        self.assertEqual(len(placed), TOP_N, "券商真实收到的下单数应与计划单数一致")

    def test_command_is_consumed_into_processed_dir(self):
        self._approve_and_plan()
        self.auto_execute()
        call, _ = self._broker()
        self.poll(call)
        self.assertEqual(self.pending(), [])
        processed = sorted(
            path.name for path in (self.home / "trading-commands" / "processed").glob("*.json"))
        self.assertEqual(len(processed), 1, "消费后必须移入 processed/（幂等留痕）")


class UnapprovedRuleNeverConsumedTest(ChainBase):
    """反证：批准是策略上岗的唯一通道——验证通过但没批准，机器一点也不用。"""

    def test_passed_but_unapproved_yields_no_plan_no_command(self):
        self.assertEqual(self.validate()["status"], "passed")
        result = self.plan_auto()
        self.assertTrue(result["ok"], result)
        self.assertIn("规则未启用", result["skipped"])
        self.assertIn("passed", result["skipped"], "跳过原因要如实说明当前状态")
        self.assertEqual(self.plans(), [], "未批准的规则不得产生任何计划")
        self.assertEqual(self.orders(), [])

        out = self.auto_execute()
        self.assertTrue(out["ok"], out)
        self.assertIn("无 auto 冻结计划", out["skipped"])
        self.assertEqual(self.pending(), [], "无计划则零指令文件")

    def test_candidate_rule_never_consumed(self):
        """连验证门都没走完的提案：不入计划链（提案连门都进不了）。"""
        store.upsert_rule(self.conn, RULE_ID, _spec(), status="candidate")
        result = self.plan_auto()
        self.assertIn("规则未启用", result["skipped"])
        self.assertEqual(self.plans(), [])

    def test_disable_after_enable_stops_consumption(self):
        """停用=可回滚，且**进程内立刻生效**（本用例是 fail-open 缺陷的回归门）。

        缺陷背景：``_resolve_strategy`` 曾把 REGISTRY 当一级事实来源，规则被解析一次就
        常驻进程内——同一进程里 Web 停用后，下一次计划生成仍命中陈旧实例继续下单。
        ``disabled`` 是终态，这等于「停用随时可停」在长驻服务进程里失效。本用例必须
        让规则**先被 plan_auto 解析过一次**（这一步才会注册进 REGISTRY），再停用，
        再解析——只按「启用→停用→解析」写就测不出该缺陷（那条路径不经过注册表）。
        """
        self.validate()
        self.approve_web()
        self.assertIn("plan", self.plan_auto())          # ① 解析一次：实例进入 REGISTRY
        body = self.handler()("rules-decide", {"rule_id": RULE_ID, "decision": "disable"})
        self.assertEqual(body["value"]["status"], "disabled")
        result = self.plan_auto()                        # ② 同一进程内再解析
        self.assertIn("规则未启用", result["skipped"])
        self.assertEqual(len(self.plans()), 1, "停用后不得新增计划")
        self.assertNotIn(RULE_ID, strategies.REGISTRY, "失效实例必须被摘出注册表")


class WebApprovalBoundaryTest(ChainBase):
    """边界：Web 端点的批准只对 passed 放行（防跳过验证门）；协议层先于门拦截。"""

    def test_enable_rejected_for_candidate(self):
        store.upsert_rule(self.conn, RULE_ID, _spec(), status="candidate")
        body = self.approve_web()
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("passed", body["error"]["message"])
        self.assertEqual(store.find_rule(self.conn, RULE_ID)["status"], "candidate",
                         "拒绝必须库零变化")

    def test_enable_rejected_for_failed(self):
        seed_failed = _spec(factors=["no_such_factor"])
        store.upsert_rule(self.conn, RULE_ID, seed_failed, status="failed")
        body = self.approve_web()
        self.assertFalse(body["ok"], body)
        self.assertEqual(store.find_rule(self.conn, RULE_ID)["status"], "failed")

    def test_unregistered_factor_rejected_at_protocol_layer(self):
        """引用未注册因子：协议层就拦下（退出码非零），**连库都不进**。"""
        code, out = self.run_cli(["rules-validate", "--spec", self.write_spec(
            _spec(factors=["no_such_factor"]))])
        body = json.loads(out)
        self.assertEqual(code, 1, out)
        self.assertFalse(body["ok"])
        self.assertTrue(any("未注册" in item for item in body["errors"]), body)
        self.assertIsNone(store.find_rule(self.conn, RULE_ID),
                          "协议非法不得入库（提案连门都进不了）")

    def test_reversed_factor_fails_the_gate_and_cannot_be_enabled(self):
        """方向为负的因子：验证门判 failed（不调阈值硬凑通过），也不能被批准。"""
        reversed_name = "wp14_e2e_reversed"

        def reversed_fn(conn, symbol, as_of):
            return -float(SYMBOLS.index(symbol) + 1) if symbol in SYMBOLS else None

        factors.REGISTRY[reversed_name] = reversed_fn
        self.addCleanup(factors.REGISTRY.pop, reversed_name, None)
        report = self.run_json(["rules-validate",
                                "--spec", self.write_spec(_spec(factors=[reversed_name])),
                                "--symbols", ",".join(SYMBOLS), "--home", str(self.home),
                                "--lookback", "20", "--horizon", "2"])
        self.assertEqual(report["status"], "failed", report)
        reasons = report["validation"]["gate_reasons"]
        self.assertTrue(reasons, report)
        self.assertTrue(any("方向为负" in item or "t 检验" in item for item in reasons),
                        reasons)
        self.assertEqual(store.find_rule(self.conn, RULE_ID)["status"], "failed")
        body = self.approve_web()
        self.assertFalse(body["ok"], "失败的规则不得被批准")
        self.assertEqual(store.find_rule(self.conn, RULE_ID)["status"], "failed")


if __name__ == "__main__":
    unittest.main()
