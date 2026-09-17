"""WP14 任务 4：``rules-validate`` CLI + 规则读写/审批端点 + 研究页候选池。

全部离线（临时 home + 真实 SQLite + 注入 runner）。覆盖计划任务 4 的测试清单：

  * CLI：``rules-list`` 形状；``rules-validate`` 协议非法=非零退出、通过=passed、
    反向因子=failed（原因含「方向为负」）、空库=样本不足 failed、已通过规则不得重复验证、
    ``--spec -`` 走 stdin、walk-forward 未请求时如实记 not_run（**不假装跑过**）；
  * 状态机：人工批准是规则上岗的唯一通道（candidate/failed 调 enable → 拒绝且库零变化）；
  * plan_auto 消费：``enabled`` 规则被加载并生成计划；``candidate`` 规则**零消费**（反证）；
    未知名字仍是「策略未注册」；
  * 端点：``rules`` 只读（空载荷白名单、不进缓存、库零变化）；``rules-decide`` 动作端点
    （字段白名单挡死 spec 字段、业务拒绝落 trading/invalid-operation、真的写库）；
  * 工具面：``rules`` 进面、``rules-decide`` **不进**（模型不得自批）；
    端点 79 / 工具 75 / 端点工具集 ≡ 端点清单 − 排除集。
"""
import io
import json
import subprocess
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

from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402
from server import caches, compute, mcp_tools, store_access  # noqa: E402
from trading_core import cli, factors, planner, rule_engine, store, strategies  # noqa: E402

TODAY = "2026-09-16"
PREV = "2026-09-15"
BARS = 80
SYMBOLS = ["SH.600001", "SH.600002", "SH.600003", "SH.600004", "SH.600005"]
FACTOR = "wp14_approve_fake"
#: 交换末两位的日期（制造 IC 序列方差：零方差序列会被 passes_gate 判「无法做 t 检验」）
SWAP_DAYS = (65, 68, 71)


def _spec(**over):
    spec = {"rule_id": "wp14_momentum_v1", "hypothesis": "测试假设：因子值与未来收益同向",
            "factors": [FACTOR], "combine": "zscore_equal_weight",
            "universe": "watchlist.SH", "top_n": 3, "rebalance": "weekly",
            "provenance": {"research_run_id": "R-approve", "created_by": "harness",
                           "created_at": "2026-09-16 10:00:00"}}
    spec.update(over)
    return spec


def _factor_fn(conn, symbol, as_of):
    """测试因子：值 = 标的在样本池里的序号（1..5，与价格路径的日收益序同向）。"""
    return float(SYMBOLS.index(symbol) + 1) if symbol in SYMBOLS else None


def _price_rows(mode="aligned", swap_days=SWAP_DAYS, days=BARS):
    """设计价格路径（日收益的横截面序决定各期 RankIC）。

    ``_ic_sample_dates`` + ``_forward_return`` 在连续日线下读取的是「评估日之后窗口的
    首尾收盘」，因此把**日收益的序**做成与因子序同向（aligned）/ 反向（reversed），
    再在少数几天交换末两位 → IC 序列 = 1.0×18 + 0.9×3（或全负），既显著又**有方差**
    （零方差序列无法做 t 检验——这是 passes_gate 的刻意语义）。
    """
    dates = [(date.fromisoformat(TODAY) - timedelta(days=days - 1 - i)).isoformat()
             for i in range(days)]
    closes = {symbol: [100.0] for symbol in SYMBOLS}
    for index in range(1, days):
        ranks = list(range(len(SYMBOLS)))
        if mode == "reversed":
            ranks = list(reversed(ranks))
        if index in swap_days:
            ranks[-1], ranks[-2] = ranks[-2], ranks[-1]
        for position, symbol in enumerate(SYMBOLS):
            closes[symbol].append(round(closes[symbol][-1] * (1 + 0.005 * (ranks[position] + 1)),
                                        4))
    return dates, closes


def seed_bars(conn, mode="aligned"):
    dates, closes = _price_rows(mode=mode)
    for symbol in SYMBOLS:
        store.upsert_bars(conn, symbol, "1d", [
            {"t": day, "o": close, "h": close + 0.5, "l": close - 0.5, "c": close,
             "v": 1000.0} for day, close in zip(dates, closes[symbol])], source="test")


def write_watchlist(home, symbols=None):
    path = Path(home) / "trading-platform.json"
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    config["watchlist"] = list(SYMBOLS if symbols is None else symbols)
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")


class ApproveBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.db = str(self.home / "t.sqlite")
        self.conn = store.connect(self.db)
        self.addCleanup(self.conn.close)
        store.migrate(self.conn)
        factors.REGISTRY[FACTOR] = _factor_fn
        self.addCleanup(factors.REGISTRY.pop, FACTOR, None)
        write_watchlist(self.home)

    def write_spec(self, spec=None):
        path = self.home / "spec.json"
        path.write_text(json.dumps(spec or _spec(), ensure_ascii=False), encoding="utf-8")
        return str(path)

    def run_cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv + ["--db", self.db])
        return code, buf.getvalue()

    def run_json(self, argv, expect_code=0):
        code, out = self.run_cli(argv)
        self.assertEqual(code, expect_code, out)
        return json.loads(out)

    def validate(self, spec=None, extra=()):
        return self.run_json(["rules-validate", "--spec", self.write_spec(spec),
                              "--symbols", ",".join(SYMBOLS), "--home", str(self.home),
                              "--lookback", "20", "--horizon", "2", *extra])

    def cli_runner(self, *prefix):
        """``compute._spawn`` 同签名替身：进程内跑**真实** CLI（带测试库路径）。

        比录制型替身更强的门：端点 → compute → CLI → SQLite 全链路真的读写，
        「拒绝且库零变化」才能用真库断言。
        """
        def run(command, timeout):
            argv = list(command[3:]) + list(prefix)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.main(argv)
            return subprocess.CompletedProcess(command, code, buf.getvalue(), "")

        return run


# ---------------------------------------------------------------------------
# ① 提案验证（CLI）
# ---------------------------------------------------------------------------

class RulesValidateCliTest(ApproveBase):
    def test_protocol_error_exits_nonzero_with_error_list(self):
        code, out = self.run_cli(["rules-validate", "--spec", self.write_spec(
            _spec(factors=["no_such_factor"]))])
        body = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(body["ok"])
        self.assertTrue(any("未注册" in e for e in body["errors"]), body)
        self.assertIsNone(store.find_rule(self.conn, "wp14_momentum_v1"),
                          "协议非法不得入库（提案连门都进不了）")

    def test_unreadable_spec_is_reported_not_raised(self):
        body = self.run_json(["rules-validate", "--spec", str(self.home / "missing.json")],
                             expect_code=1)
        self.assertFalse(body["ok"])
        self.assertIn("spec 读取失败", body["error"])

    def test_stdin_spec_is_accepted(self):
        seed_bars(self.conn)
        with mock.patch("sys.stdin", io.StringIO(json.dumps(_spec(), ensure_ascii=False))):
            body = self.run_json(["rules-validate", "--spec", "-",
                                  "--symbols", ",".join(SYMBOLS),
                                  "--lookback", "20", "--horizon", "2"])
        self.assertEqual(body["status"], "passed", body)

    def test_informative_factor_passes_gate(self):
        seed_bars(self.conn)
        body = self.validate()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["status"], "passed")
        report = body["validation"]["factors"][FACTOR]
        self.assertGreaterEqual(report["n"], factors.IC_MIN_SAMPLES)
        self.assertGreater(report["t_stat"], factors.IC_T_THRESHOLD)
        self.assertTrue(report["monotonic"])
        self.assertTrue(report["passes_gate"])
        self.assertEqual(report["gate_reasons"], [])
        # 补充证据在场但不参与门槛（口径在报告里显式标注）
        self.assertIsNotNone(report["half_life"])
        self.assertIn("不参与", body["validation"]["note"])
        # 状态机：candidate → validating → passed 全程留痕
        row = store.find_rule(self.conn, "wp14_momentum_v1")
        self.assertEqual(row["status"], "passed")
        self.assertEqual(row["validation"]["passed"], True)
        self.assertIsNone(row["approved_by"], "验证通过 ≠ 批准：批准人此时必须为空")

    def test_reversed_factor_fails_with_direction_reason(self):
        seed_bars(self.conn, mode="reversed")
        body = self.validate()
        self.assertEqual(body["status"], "failed")
        report = body["validation"]["factors"][FACTOR]
        self.assertFalse(report["passes_gate"])
        self.assertTrue(any("方向为负" in reason for reason in report["gate_reasons"]),
                        report["gate_reasons"])
        self.assertTrue(any("分层不单调" in reason for reason in report["gate_reasons"]),
                        report["gate_reasons"])
        self.assertEqual(store.find_rule(self.conn, "wp14_momentum_v1")["status"], "failed")

    def test_empty_db_is_sample_shortage_not_fabricated_pass(self):
        body = self.validate()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "failed")
        self.assertTrue(any("样本不足" in reason for reason in body["validation"]["gate_reasons"]),
                        body["validation"])
        self.assertNotIn(FACTOR, body["validation"]["factors"],
                         "无样本不得产出任何因子结论")
        self.assertEqual(store.find_rule(self.conn, "wp14_momentum_v1")["status"], "failed")

    def test_empty_watchlist_is_sample_shortage(self):
        write_watchlist(self.home, symbols=[])
        body = self.run_json(["rules-validate", "--spec", self.write_spec(),
                              "--home", str(self.home)])
        self.assertEqual(body["status"], "failed")
        self.assertIn("关注池为空", body["validation"]["gate_reasons"][0])

    def test_approved_rule_cannot_be_silently_revalidated(self):
        seed_bars(self.conn)
        self.validate()
        self.run_json(["rules-decide", "--rule-id", "wp14_momentum_v1",
                       "--decision", "enable", "--by", "web"])
        body = self.run_json(["rules-validate", "--spec", self.write_spec(),
                              "--symbols", ",".join(SYMBOLS)], expect_code=1)
        self.assertFalse(body["ok"])
        self.assertIn("已存在且状态为 enabled", body["error"])
        self.assertEqual(store.find_rule(self.conn, "wp14_momentum_v1")["status"], "enabled",
                         "重复验证不得改动已启用规则的状态")

    def test_validating_rule_can_resume_after_crash(self):
        """中断恢复：validation 中途崩溃留下的 validating 状态可继续验证（状态机允许）。"""
        seed_bars(self.conn)
        store.upsert_rule(self.conn, "wp14_momentum_v1", _spec(), status="validating")
        body = self.validate()
        self.assertEqual(body["status"], "passed", body)
        self.assertEqual(store.find_rule(self.conn, "wp14_momentum_v1")["status"], "passed")

    def test_walkforward_not_requested_is_recorded_honestly(self):
        seed_bars(self.conn)
        body = self.validate()
        self.assertEqual(body["validation"]["walkforward"]["status"], "not_run")
        self.assertIn("未请求", body["validation"]["walkforward"]["reason"])

    def test_walkforward_without_window_is_not_run_with_reason(self):
        seed_bars(self.conn)
        body = self.validate(extra=("--walkforward",))
        self.assertEqual(body["validation"]["walkforward"]["status"], "not_run")
        self.assertIn("--start", body["validation"]["walkforward"]["reason"])

    def test_walkforward_with_window_never_fakes_success(self):
        """给窗口但库内无日历/基准 → 如实记 no_folds/failed，绝不写 ok。"""
        seed_bars(self.conn)
        body = self.validate(extra=("--walkforward", "--start", "2024-01-01",
                                    "--end", "2026-09-16"))
        self.assertIn(body["validation"]["walkforward"]["status"],
                      ("no_folds", "failed"))
        self.assertNotEqual(body["validation"]["walkforward"].get("summary"), {},
                            "没有跑通就不该有 summary")


# ---------------------------------------------------------------------------
# ② 列表与人工批准（CLI）
# ---------------------------------------------------------------------------

class FindRuleTest(ApproveBase):
    """``store.find_rule``：非抛错口径的按 id 查询（分流判定的基础原语）。"""

    def test_missing_returns_none_and_present_returns_row(self):
        self.assertIsNone(store.find_rule(self.conn, "nope"))
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1"), status="candidate")
        row = store.find_rule(self.conn, "r1")
        self.assertEqual(row["status"], "candidate")
        self.assertEqual(row["spec"]["factors"], [FACTOR])
        self.assertIsNone(row["validation"])

    def test_get_rule_still_raises_for_missing(self):
        """与 get_rule 的分工：按 id 操作仍以异常表达缺失（不因新原语改变既有语义）。"""
        self.assertIsNone(store.find_rule(self.conn, "nope"))
        with self.assertRaises(ValueError):
            store.get_rule(self.conn, "nope")


class RulesListCliTest(ApproveBase):
    def test_list_is_empty_structure_on_empty_db(self):
        body = self.run_json(["rules-list"])
        self.assertTrue(body["ok"])
        self.assertEqual(body["rules"], [])

    def test_list_flattens_spec_and_keeps_validation(self):
        seed_bars(self.conn)
        self.validate()
        body = self.run_json(["rules-list"])
        row = body["rules"][0]
        self.assertEqual(row["rule_id"], "wp14_momentum_v1")
        self.assertEqual(row["factors"], [FACTOR])
        self.assertEqual(row["combine"], "zscore_equal_weight")
        self.assertEqual(row["universe"], "watchlist.SH")
        self.assertEqual(row["top_n"], 3)
        self.assertEqual(row["status"], "passed")
        self.assertEqual(row["provenance"]["created_by"], "harness")
        self.assertTrue(row["validation"]["passed"])
        self.assertIsNone(row["approved_by"])


class RulesDecideCliTest(ApproveBase):
    def test_enable_requires_passed(self):
        store.upsert_rule(self.conn, "wp14_momentum_v1", _spec(), status="candidate")
        body = self.run_json(["rules-decide", "--rule-id", "wp14_momentum_v1",
                              "--decision", "enable"], expect_code=1)
        self.assertFalse(body["ok"])
        self.assertIn("仅通过验证的规则可启用", body["error"])
        self.assertEqual(store.find_rule(self.conn, "wp14_momentum_v1")["status"],
                         "candidate", "拒绝时库零变化")

    def test_failed_rule_cannot_be_enabled(self):
        store.upsert_rule(self.conn, "wp14_momentum_v1", _spec(), status="failed")
        body = self.run_json(["rules-decide", "--rule-id", "wp14_momentum_v1",
                              "--decision", "enable"], expect_code=1)
        self.assertFalse(body["ok"])
        self.assertEqual(store.find_rule(self.conn, "wp14_momentum_v1")["status"], "failed")

    def test_unknown_rule_is_rejected(self):
        body = self.run_json(["rules-decide", "--rule-id", "nope", "--decision", "enable"],
                             expect_code=1)
        self.assertFalse(body["ok"])
        self.assertIn("规则不存在", body["error"])

    def test_enable_records_approver_and_disable_keeps_trace(self):
        seed_bars(self.conn)
        self.validate()
        body = self.run_json(["rules-decide", "--rule-id", "wp14_momentum_v1",
                              "--decision", "enable", "--by", "web"])
        self.assertEqual(body["status"], "enabled")
        row = store.find_rule(self.conn, "wp14_momentum_v1")
        self.assertEqual(row["approved_by"], "web")
        self.assertTrue(row["approved_at"])
        body = self.run_json(["rules-decide", "--rule-id", "wp14_momentum_v1",
                              "--decision", "disable", "--by", "web"])
        self.assertEqual(body["status"], "disabled")
        row = store.find_rule(self.conn, "wp14_momentum_v1")
        self.assertEqual(row["status"], "disabled")
        self.assertEqual(row["approved_by"], "web", "停用保留批准的留痕")


# ---------------------------------------------------------------------------
# ③ plan_auto 只消费已启用的规则
# ---------------------------------------------------------------------------

class PlanAutoRuleConsumptionTest(ApproveBase):
    def setUp(self):
        super().setUp()
        write_risk = {}
        (self.home / "trading-risk.json").write_text(json.dumps(write_risk), encoding="utf-8")
        store.upsert_calendar(self.conn, "SH", [
            {"day": day, "trade_date_type": "WHOLE", "trade_second": 14400}
            for day in (PREV, TODAY)])
        self._configure(strategy="wp14_momentum_v1")

    def _configure(self, strategy):
        config = json.loads((self.home / "trading-platform.json").read_text(encoding="utf-8"))
        config["auto_pipeline"] = {
            "enabled": True,
            "strategies": [{"market": "SH", "strategy": strategy, "watchlist": "watchlist"}],
            "exec_at": {"SH": "09:35"}, "reconcile_at": "19:00"}
        (self.home / "trading-platform.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8")

    def _broker(self):
        def call(name, args=None, timeout=30, **kwargs):
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3,
                                      "account_title": "模拟账户"}]}
            if name == "sim_trade_position_list":
                return {"positions": []}
            if name == "sim_trade_cash_info":
                return {"balance": 1_000_000.0, "total_asset": 1_000_000.0}
            raise AssertionError(f"意外工具 {name}")

        return call

    def test_enabled_rule_is_consumed(self):
        seed_bars(self.conn)
        self.validate()
        self.run_json(["rules-decide", "--rule-id", "wp14_momentum_v1",
                       "--decision", "enable", "--by", "web"])
        result = planner.plan_auto(self.conn, str(self.home), "SH", today=TODAY,
                                   broker_call=self._broker())
        self.assertTrue(result["ok"], result)
        self.assertIn("plan", result, result)
        orders = result["plan"]["orders"]
        self.assertEqual(len(orders), 3, "top_n=3 的规则应生成 3 单")
        self.assertTrue(all(order["market"] == "SH" for order in orders))
        row = self.conn.execute("SELECT origin, market, strategy_id FROM plans").fetchone()
        self.assertEqual(row["origin"], "auto")
        self.assertEqual(row["market"], "SH")
        self.assertEqual(row["strategy_id"], "wp14_momentum_v1")

    def test_candidate_rule_is_never_consumed(self):
        seed_bars(self.conn)
        self.validate()  # → passed（仍未批准）
        result = planner.plan_auto(self.conn, str(self.home), "SH", today=TODAY,
                                   broker_call=self._broker())
        self.assertTrue(result["ok"])
        self.assertIn("规则未启用", result["skipped"])
        self.assertIn("passed", result["skipped"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"], 0)
        alerts = [dict(row) for row in self.conn.execute(
            "SELECT level, title FROM alerts").fetchall()]
        self.assertTrue(any(a["title"] == "规则未启用" for a in alerts), alerts)

    def test_disabled_rule_is_never_consumed(self):
        seed_bars(self.conn)
        self.validate()
        self.run_json(["rules-decide", "--rule-id", "wp14_momentum_v1",
                       "--decision", "enable", "--by", "web"])
        self.run_json(["rules-decide", "--rule-id", "wp14_momentum_v1",
                       "--decision", "disable", "--by", "web"])
        result = planner.plan_auto(self.conn, str(self.home), "SH", today=TODAY,
                                   broker_call=self._broker())
        self.assertIn("规则未启用", result["skipped"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"], 0)

    def test_unknown_name_still_reports_unregistered_strategy(self):
        self._configure(strategy="no_such_strategy")
        seed_bars(self.conn)
        result = planner.plan_auto(self.conn, str(self.home), "SH", today=TODAY,
                                   broker_call=self._broker())
        self.assertIn("策略未注册", result["skipped"])

    def test_builtin_strategy_still_wins_over_db_lookup(self):
        """内置策略名与规则表无冲突：注册表命中即用，不去查库（回归）。"""
        self._configure(strategy="watchlist_rsi")
        seed_bars(self.conn)
        self.assertIsNotNone(strategies.REGISTRY.get("watchlist_rsi"))
        result = planner.plan_auto(self.conn, str(self.home), "SH", today=TODAY,
                                   broker_call=self._broker())
        self.assertTrue(result["ok"], result)


# ---------------------------------------------------------------------------
# ④ 端点：只读候选池 + 人工批准
# ---------------------------------------------------------------------------

class RulesEndpointTest(ApproveBase):
    def setUp(self):
        super().setUp()
        caches.configure(home=str(self.home))
        self.addCleanup(caches.configure)
        seed_bars(self.conn)
        self.run_json(["rules-validate", "--spec", self.write_spec(),
                       "--symbols", ",".join(SYMBOLS), "--lookback", "20", "--horizon", "2"])
        self.core = {
            "rules": lambda status=None: compute.rules_list(
                status, runner=self.cli_runner("--db", self.db)),
            "rules-decide": lambda rule_id, decision: compute.rules_decide(
                rule_id, decision, runner=self.cli_runner("--db", self.db))}

    def handler(self, core=None):
        return app_module.create_handler(str(self.home), analytics={}, series=None,
                                         core=self.core if core is None else core)

    def test_read_envelope_lists_rules(self):
        body = self.handler()("rules", {})
        self.assertTrue(body["ok"], body)
        rules = body["value"]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["rule_id"], "wp14_momentum_v1")
        self.assertEqual(rules[0]["status"], "passed")
        self.assertNotIn("cached", body, "批准状态不进缓存（读即最新）")

    def test_read_rejects_unknown_field(self):
        body = self.handler()("rules", {"ticker": "SH.600519"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("Unexpected rules field", body["error"]["message"])

    def test_read_filters_by_status(self):
        store.upsert_rule(self.conn, "wp14_second", _spec(rule_id="wp14_second"),
                          status="candidate")
        all_rules = self.handler()("rules", {})["value"]["rules"]
        self.assertEqual(len(all_rules), 2)
        passed_only = self.handler()("rules", {"status": "passed"})["value"]["rules"]
        self.assertEqual([row["rule_id"] for row in passed_only], ["wp14_momentum_v1"])
        self.assertEqual(self.handler()("rules", {"status": "candidate"})["value"]["rules"][0]
                         ["rule_id"], "wp14_second")

    def test_read_unknown_status_is_reported_with_allowed_values(self):
        body = self.handler()("rules", {"status": "archived"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("未知规则状态", body["error"]["message"])
        self.assertIn("candidate", body["error"]["message"])

    def test_read_is_read_only_on_real_db(self):
        before = self.conn.execute("SELECT COUNT(*) AS n FROM rules").fetchone()["n"]
        self.handler()("rules", {})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM rules").fetchone()["n"], before)

    def test_decide_whitelist_blocks_spec_fields(self):
        before = store.find_rule(self.conn, "wp14_momentum_v1")["status"]
        body = self.handler()("rules-decide", {"rule_id": "wp14_momentum_v1",
                                               "decision": "enable",
                                               "spec": {"top_n": 99}})
        self.assertFalse(body["ok"])
        self.assertIn("Unexpected rules-decide field", body["error"]["message"])
        self.assertEqual(store.find_rule(self.conn, "wp14_momentum_v1")["status"], before)

    def test_decide_enable_writes_through_the_core_bridge(self):
        body = self.handler()("rules-decide", {"rule_id": "wp14_momentum_v1",
                                               "decision": "enable"})
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"]["status"], "enabled")
        row = store.find_rule(self.conn, "wp14_momentum_v1")
        self.assertEqual(row["status"], "enabled")
        self.assertEqual(row["approved_by"], "web", "审批来源默认记 web（端点唯一入口）")

    def test_decide_rejection_is_reported_not_swallowed(self):
        store.update_rule(self.conn, "wp14_momentum_v1", status="disabled")
        body = self.handler()("rules-decide", {"rule_id": "wp14_momentum_v1",
                                               "decision": "enable"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("仅通过验证的规则可启用", body["error"]["message"])
        self.assertEqual(store.find_rule(self.conn, "wp14_momentum_v1")["status"], "disabled")

    def test_endpoints_are_declared_and_not_cached(self):
        self.assertIn("rules", store_access.endpoints())
        self.assertIn("rules-decide", store_access.endpoints())
        self.assertNotIn("rules", caches.CACHE_TTL_MS)
        self.assertNotIn("rules-decide", caches.CACHE_TTL_MS)
        self.assertNotIn("rules", caches.ENDPOINT_SHAPE)

    def test_http_route_reaches_handler(self):
        app = app_module.create_app(home=str(self.home),
                                    dist=str(self.home.parent / "dist-missing"),
                                    analytics={}, core=self.core)
        body = TestClient(app).post("/api/wb/rules", json={}).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"]["rules"][0]["status"], "passed")


# ---------------------------------------------------------------------------
# ⑤ compute 契约与工具面
# ---------------------------------------------------------------------------

class RulesComputeTest(ApproveBase):
    def test_rules_list_command(self):
        runner = self.cli_runner("--db", self.db)
        calls = []
        original = cli.main

        def spy(argv):
            calls.append(list(argv))
            return original(argv)

        with mock.patch.object(cli, "main", spy):
            value = compute.rules_list(runner=runner)
        self.assertEqual(value["rules"], [])
        self.assertEqual(calls[0][0], "rules-list")

    def test_rules_decide_validates_before_spawn(self):
        recorded = []

        def runner(command, timeout):  # pragma: no cover - 不该被调用
            recorded.append(command)
            raise AssertionError("非法参数不得起子进程")

        for bad_decision in ("maybe", "ENABLE", None, 1):
            with self.assertRaises(compute.ComputeError):
                compute.rules_decide("r1", bad_decision, runner=runner)
        for bad_id in ("", "   ", None, 5):
            with self.assertRaises(compute.ComputeError):
                compute.rules_decide(bad_id, "enable", runner=runner)
        self.assertEqual(recorded, [])

    def test_rules_decide_command_shape(self):
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1"), status="passed")
        runner = self.cli_runner("--db", self.db)
        captured = []
        original = cli.main

        def spy(argv):
            captured.append(list(argv))
            return original(argv)

        with mock.patch.object(cli, "main", spy):
            value = compute.rules_decide("r1", "enable", runner=runner)
        self.assertEqual(value["status"], "enabled")
        self.assertEqual(captured[0][:4], ["rules-decide", "--rule-id", "r1", "--decision"])
        self.assertIn("--by", captured[0])

    def test_cli_error_maps_to_compute_error(self):
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1"), status="candidate")
        runner = self.cli_runner("--db", self.db)
        with self.assertRaises(compute.ComputeError) as caught:
            compute.rules_decide("r1", "enable", runner=runner)
        self.assertIn("仅通过验证的规则可启用", str(caught.exception))


class RulesListFilterTest(ApproveBase):
    def test_cli_filters_and_rejects_unknown_status(self):
        store.upsert_rule(self.conn, "r1", _spec(rule_id="r1"), status="passed")
        store.upsert_rule(self.conn, "r2", _spec(rule_id="r2"), status="candidate")
        body = self.run_json(["rules-list"])
        self.assertEqual([row["rule_id"] for row in body["rules"]], ["r1", "r2"])
        body = self.run_json(["rules-list", "--status", "passed"])
        self.assertEqual([row["rule_id"] for row in body["rules"]], ["r1"])
        body = self.run_json(["rules-list", "--status", "archived"], expect_code=1)
        self.assertFalse(body["ok"])
        self.assertIn("未知规则状态", body["error"])

    def test_compute_forwards_status(self):
        runner = self.cli_runner("--db", self.db)
        captured = []
        original = cli.main

        def spy(argv):
            captured.append(list(argv))
            return original(argv)

        with mock.patch.object(cli, "main", spy):
            compute.rules_list("passed", runner=runner)
            compute.rules_list(runner=runner)
        self.assertEqual(captured[0], ["rules-list", "--status", "passed", "--db", self.db])
        self.assertEqual(captured[1], ["rules-list", "--db", self.db])

    def test_unknown_status_maps_to_invalid_operation(self):
        runner = self.cli_runner("--db", self.db)
        with self.assertRaises(compute.ComputeError) as caught:
            compute.rules_list("archived", runner=runner)
        self.assertIn("未知规则状态", str(caught.exception))


class RulesSurfaceTest(ApproveBase):
    def test_tool_and_endpoint_counts(self):
        self.assertEqual(mcp_tools.TOOL_COUNT, 75)
        self.assertEqual(len(mcp_tools.TOOLS), 75)
        self.assertEqual(len(store_access.endpoints()), 79)
        self.assertEqual(len(set(store_access.endpoints())), 79)

    def test_rules_tool_is_visible_and_decide_is_not(self):
        self.assertIn("rules", mcp_tools.TOOL_NAMES)
        self.assertNotIn("rules-decide", mcp_tools.TOOL_NAMES)
        self.assertNotIn("rules_decide", mcp_tools.TOOL_NAMES)
        self.assertIn("rules-decide", mcp_tools.MCP_EXCLUDED_ENDPOINTS)
        self.assertEqual(mcp_tools.ENDPOINT_TOOL_ENDPOINTS["rules"], "rules")

    def test_endpoint_tool_set_equals_endpoints_minus_excluded(self):
        forwarded = set(mcp_tools.ENDPOINT_TOOL_ENDPOINTS.values())
        self.assertEqual(forwarded,
                         set(store_access.endpoints()) - set(mcp_tools.MCP_EXCLUDED_ENDPOINTS))

    def test_rules_tool_accepts_status_filter_only(self):
        definition = {tool.name: tool for tool in mcp_tools.TOOLS}["rules"]
        self.assertEqual([field for field in definition.fields if field != "refresh"],
                         ["status"])

    def test_rule_status_type_matches_rule_engine(self):
        from typing import get_args
        self.assertEqual(set(get_args(mcp_tools._TYPES["rule_status"])),
                         set(rule_engine.RULE_STATUSES))

    def test_tool_budget_unchanged(self):
        self.assertLessEqual(mcp_tools.TOOL_COUNT, 80)


if __name__ == "__main__":
    unittest.main()
