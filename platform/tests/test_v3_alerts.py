"""``server/v3_alerts.py`` 契约测试（规格 §8.3 的平台内规则求值器）。

全部离线：用**构造的指标模型 + 构造的窗口历史**驱动求值器，不依赖运行中的 8397、
不打网络、不写任何文件（唯一的真实输入是 ``deploy/monitoring/alerts.yml`` 本身）。

覆盖点逐条：
  * **三态**：firing / pending / ok / no-data / unsupported 各自可达，且
    ``no-data``（指标缺席）**绝不等于** ok——「看不到」不是「正常」；
  * **阈值边界**：``>`` 在等于阈值时不算命中，``>=`` 才算；规则写什么就按什么求值；
  * **真实规则文件**：解析 ``deploy/monitoring/alerts.yml`` 不抛错，规则条数与结构对得上，
    且每条都在支持子集内（有 ``unsupported`` 时必须是显式状态）；
  * **§8.3 五条补齐规则有鉴别力**：用构造指标能真的把它们推到 firing，换个正常值就不响；
  * **分位数口径**：最近秩（不插值）、样本不足即不导出；
  * **Prometheus 文本**：本地解析器与格式校验器（``promtool`` 的离线替代）；
  * **端点**：``GET /api/v3/ops/alerts`` / ``.../rules`` 是只读、幂等、三态可见。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_alerts -v``
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import observability, v3_alerts  # noqa: E402

DEPLOY = ROOT / "deploy" / "monitoring"
ALERTS = DEPLOY / "alerts.yml"

#: 规格 §8.3 点名的 9 类监控 → 本仓库里接住它们的规则名（逐条可核对）。
SPEC83_RULES = {
    "Headless 调用成功率 < 95%": "HarnessHeadlessSuccessRateLow",
    "Headless 调用平均耗时 > 60s": "HarnessHeadlessDurationHigh",
    "SDK 会话活跃数 > 10": "HarnessSdkActiveSessionsHigh",
    "MCP 工具调用延迟 > 5s": "ToolMcpCallLatencyP95High",
    "MCP 工具调用失败率 > 5%": "QuantMcpToolCallFailureRateHigh",
    "数据源连接状态（断连）": "DataSourceChainUnavailable",
    "数据延迟 > 5min": "DataSourceDataDelayHigh",
    "订单执行延迟 > 1s": "TradeOrderExecutionLatencyHigh",
    "风控阻断次数（突增）": "RiskBlockedSpike",
}


def build_engine(**kwargs):
    """用真实规则文件建一个求值器（默认不采集：模型由测试注入）。"""
    kwargs.setdefault("collect", None)
    return v3_alerts.AlertsEngine(v3_alerts.load_rules(), **kwargs)


def states(results):
    return {item["rule"]: item["state"] for item in results}


def by_name(results, name):
    for item in results:
        if item["rule"] == name:
            return item
    raise AssertionError(f"结果里没有规则 {name}")


def hourly_history(now, hours=2, step=15.0, value=None):
    """构造 ``现在往前 hours 小时`` 的窗口历史：``value(t) -> 数值``。"""
    out = {}
    points = int(hours * 3600 / step)
    for index in range(points + 1):
        moment = now - hours * 3600 + index * step
        out[moment] = value(moment) if callable(value) else value
    return out


# ---------------------------------------------------------------------------
# 1. 真实规则文件（解析不抛错 + 结构 + 覆盖 §8.3）
# ---------------------------------------------------------------------------
class RulesFileTests(unittest.TestCase):
    def test_real_alerts_file_parses(self):
        rules = v3_alerts.load_rules()
        self.assertGreaterEqual(len(rules), 30, "规则条数异常偏少（应含 §8.3 补齐项）")
        for rule in rules:
            self.assertTrue(rule["name"], f"缺 alert 名：{rule}")
            self.assertTrue(rule["expr"], f"{rule['name']} 缺 expr")
            self.assertRegex(rule["for"], r"^\d+(\.\d+)?(ms|s|m|h|d|w)$",
                             f"{rule['name']} 的 for 不合法：{rule['for']!r}")
            self.assertIn(rule["severity"], ("critical", "warning", "info"))
            self.assertEqual(rule["source_file"], str(ALERTS))

    def test_rule_names_are_unique(self):
        names = [rule["name"] for rule in v3_alerts.load_rules()]
        self.assertEqual(len(names), len(set(names)), "告警名重复")

    def test_mini_yaml_parser_matches_the_file_structure(self):
        """内置 YAML 子集解析器的结构断言（venv 里没有 PyYAML，不能靠它兜底）。"""
        document = v3_alerts._MiniYaml(ALERTS.read_text(encoding="utf-8")).parse()
        groups = document["groups"]
        self.assertEqual(len(groups), 8)
        for group in groups:
            self.assertTrue(group["name"])
            self.assertIsInstance(group["rules"], list)
            self.assertTrue(group["rules"])
            for rule in group["rules"]:
                self.assertIn("alert", rule)
                self.assertIn("labels", rule)
                self.assertIn("annotations", rule)
        # 折叠块（>-）必须被折成一行（否则表达式里会带换行，解析器不认）
        toolface = [rule for group in groups for rule in group["rules"]
                    if rule["alert"] == "QuantWorkbenchToolfaceUnreachable"][0]
        self.assertNotIn("\n", toolface["expr"])
        self.assertIn("and", toolface["expr"])

    def test_mini_yaml_parser_refuses_unknown_structure(self):
        """读不懂的结构必须**报错**，不能静默读成半个文件。"""
        with self.assertRaises(v3_alerts.YamlSubsetError):
            v3_alerts._MiniYaml("groups:\n  - name: x\n   bad: indent\n").parse()

    def test_every_rule_is_inside_the_supported_subset(self):
        rules = v3_alerts.load_rules()
        unsupported = {rule["name"]: rule["unsupported_reason"]
                       for rule in rules if not rule["supported"]}
        # 允许存在不支持项（那就必须显式报 unsupported），但**当前文件应在子集内**：
        # 反过来说，若将来有人写了聚合/absent()，这里会先亮出来，再由求值器报 unsupported。
        self.assertEqual(unsupported, {},
                         f"规则文件里出现了超出支持子集的表达式：{unsupported}")

    def test_spec83_nine_topics_have_rules(self):
        names = {rule["name"] for rule in v3_alerts.load_rules()}
        missing = {topic: rule for topic, rule in SPEC83_RULES.items() if rule not in names}
        self.assertEqual(missing, {}, f"规格 §8.3 点名的监控项没有对应规则：{missing}")

    def test_rules_file_is_the_one_prometheus_reads(self):
        """两个求值方共用一份文件：v3_alerts 读的就是 prometheus.yml 的 rule_files。"""
        prometheus = (DEPLOY / "prometheus.yml").read_text(encoding="utf-8")
        self.assertIn("- alerts.yml", prometheus)
        self.assertEqual(v3_alerts.rules_path(), ALERTS)


# ---------------------------------------------------------------------------
# 2. 表达式子集：支持什么、拒绝什么、边界怎么算
# ---------------------------------------------------------------------------
class ExpressionSubsetTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        self.model = v3_alerts.build_model({
            "m": [({"job": "a"}, 5.0)],
            "n": [3.0],
            "counter": [100.0],
        })

    def evaluate(self, expr, history=None):
        return v3_alerts.evaluate_expression(expr, self.model, history or {}, self.now)

    def test_unsupported_constructs_are_explicit(self):
        """超出子集必须显式 unsupported——绝不静默当成 ok。"""
        for expr in ("sum(m) > 1", "absent(m)", "m or n", "m =~ \"a.*\"",
                     "quantile(0.9, m) > 1", "irate(counter[5m]) > 1",
                     "m > bool 1", "m unless n"):
            with self.assertRaises(v3_alerts.UnsupportedExpression, msg=expr):
                v3_alerts.parse_expression(expr)

    def test_comparison_boundary_uses_the_rule_operator(self):
        """``>`` 在等于阈值时**不算**命中；``>=`` 才算（按规则字面语义求值）。"""
        truthy = lambda expr: self.evaluate(expr)["truthy"]  # noqa: E731
        self.assertFalse(truthy("m > 5"))
        self.assertFalse(truthy("m < 5"))
        self.assertTrue(truthy("m >= 5"))
        self.assertTrue(truthy("m <= 5"))
        self.assertTrue(truthy("m == 5"))
        self.assertFalse(truthy("m != 5"))
        self.assertFalse(truthy("m == 6"))

    def test_equal_comparison_keeps_the_matched_value(self):
        """``x == 0`` 命中时保留 lhs 的值（Prometheus 语义）：向量非空即为真。"""
        model = v3_alerts.build_model({"flag": [0.0]})
        outcome = v3_alerts.evaluate_expression("flag == 0", model, {}, self.now)
        self.assertTrue(outcome["truthy"])
        self.assertEqual(outcome["value"], 0.0)

    def test_label_matchers_and_and_guard(self):
        """``and`` 默认要求标签集**完全相同**（PromQL 语义）；``chain`` 相同才算关联上。"""
        model = v3_alerts.build_model({
            "available": [({"chain": "kline"}, 0.0)],
            "stamp": [({"chain": "kline"}, self.now - 10)],
        })
        expr = 'available{chain="kline"} == 0 and (time() - stamp) < 300'
        self.assertTrue(v3_alerts.evaluate_expression(expr, model, {}, self.now)["truthy"])
        stale = 'available{chain="kline"} == 0 and (time() - stamp) < 5'
        self.assertFalse(v3_alerts.evaluate_expression(stale, model, {}, self.now)["truthy"])

    def test_and_without_matching_labels_is_flagged_as_a_rule_defect(self):
        """两侧标签集不兼容时给出警告（规则会永远不命中），并支持 ``on()`` 显式关联。"""
        model = v3_alerts.build_model({
            "pct": [({"scope": "max"}, 37.5)],
            "stamp": [self.now - 10],
        })
        broken = 'pct{scope="max"} > 20 and (time() - stamp) < 21600'
        outcome = v3_alerts.evaluate_expression(broken, model, {}, self.now)
        self.assertFalse(outcome["truthy"])
        self.assertTrue(any("不可能命中" in note for note in outcome["notes"]))
        fixed = 'pct{scope="max"} > 20 and on() (time() - stamp) < 21600'
        self.assertTrue(v3_alerts.evaluate_expression(fixed, model, {}, self.now)["truthy"])

    def test_offset_shifts_the_lookup_into_the_past(self):
        """``offset`` 是支持的：取「求值时刻 - offset」的那个历史瞬时值（不是现在的值）。

        注意 PromQL 语义是**瞬时**偏移：``offset 5m`` 取的是 5 分钟前的那个样本
        （``<= now - 300`` 里最新的一个），不是「窗口更早那一端」。
        """
        model = v3_alerts.build_model({"g": [99.0]})
        history = {("g", ()): [(self.now - 600, 1.0), (self.now - 300, 2.0)]}
        self.assertEqual(
            v3_alerts.evaluate_expression("g offset 5m", model, history, self.now)["value"], 2.0)
        self.assertEqual(
            v3_alerts.evaluate_expression("g offset 10m", model, history, self.now)["value"], 1.0)

    def test_threshold_extraction_reports_metric_and_operator(self):
        outcome = self.evaluate('m{job="a"} > 5')
        self.assertEqual(outcome["thresholds"],
                         [{"metric": "m", "operator": ">", "threshold": 5.0}])

    def test_rate_and_increase_over_constructed_history(self):
        history = {("counter", ()): [(self.now - 600, 0.0), (self.now, 60.0)]}
        outcome = v3_alerts.evaluate_expression("increase(counter[10m]) > 0", self.model,
                                                history, self.now)
        self.assertTrue(outcome["truthy"])
        self.assertAlmostEqual(outcome["value"], 60.0, places=6)
        self.assertAlmostEqual(
            v3_alerts.evaluate_expression("rate(counter[10m])", self.model, history,
                                          self.now)["value"], 0.1, places=6)

    def test_history_coverage_below_floor_is_a_gap(self):
        """只观测了窗口的一小段 → 抛 HistoryGap（不外推），由上层翻成 no-data。"""
        history = {("n", ()): [(self.now - 30, 1.0), (self.now, 2.0)]}
        with self.assertRaises(v3_alerts.HistoryGap):
            v3_alerts.evaluate_expression("increase(n[1h]) > 0", self.model, history, self.now)

    def test_range_function_runs_per_series(self):
        """范围函数逐序列求值（多序列不该被判成 unsupported）。"""
        model = v3_alerts.build_model({"c": [({"channel": "q"}, 0.0), ({"channel": "t"}, 0.0)]})
        history = {
            ("c", (("channel", "q"),)): [(self.now - 600, 0.0), (self.now, 4.0)],
            ("c", (("channel", "t"),)): [(self.now - 600, 0.0), (self.now, 0.0)],
        }
        outcome = v3_alerts.evaluate_expression("increase(c[10m]) > 2", model, history, self.now)
        self.assertTrue(outcome["truthy"])
        self.assertEqual(outcome["series"], [{"labels": {"channel": "q"}, "value": 4.0}])


# ---------------------------------------------------------------------------
# 3. 三态 + for 时钟
# ---------------------------------------------------------------------------
class EngineStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = {"now": 1_800_000_000.0}
        self.engine = build_engine(clock=lambda: self.clock["now"])

    def run_rules(self, **metrics):
        model = v3_alerts.build_model(metrics)
        return self.engine.evaluate(model, now=self.clock["now"])

    def test_missing_metric_is_no_data_not_ok(self):
        results = self.run_rules(quantwb_up=[1.0])
        item = by_name(results, "FutuRateLimited")
        self.assertEqual(item["state"], "no-data")
        self.assertIn("futu_rate_limited_total", item["evidence"]["missing_metrics"])
        self.assertIn("不是 ok", item["evidence"]["reason"])

    def test_condition_false_is_ok(self):
        results = self.run_rules(quantwb_scheduler_alive=[1.0])
        item = by_name(results, "QuantSchedulerThreadDead")
        self.assertEqual(item["state"], "ok")
        self.assertEqual(item["value"], None)

    def test_for_clock_pending_then_firing(self):
        model_metrics = {"quantwb_scheduler_alive": [0.0]}
        first = self.run_rules(**model_metrics)
        pending = by_name(first, "QuantSchedulerThreadDead")
        self.assertEqual(pending["state"], "pending")
        self.assertGreater(pending["for_remaining_seconds"], 0)
        self.clock["now"] += 301.0  # for: 5m
        second = self.run_rules(**model_metrics)
        firing = by_name(second, "QuantSchedulerThreadDead")
        self.assertEqual(firing["state"], "firing")
        self.assertEqual(firing["condition_since"], pending["condition_since"],
                         "condition_since 应是条件首次为真的时刻（pending 的起点）")
        self.assertGreater(firing["since"], pending["since"],
                           "since 是进入当前状态的时刻（Prometheus ActiveAt 语义）")
        # 恢复 → ok，且 since 变成「转为 ok 的时刻」
        self.clock["now"] += 60.0
        recovered = by_name(self.run_rules(quantwb_scheduler_alive=[1.0]),
                            "QuantSchedulerThreadDead")
        self.assertEqual(recovered["state"], "ok")
        self.assertNotEqual(recovered["since"], firing["since"])

    def test_for_zero_fires_immediately(self):
        results = self.run_rules(quantwb_push_connected=[({"channel": "quote"}, 0.0)],
                                 quantwb_push_enabled=[1.0])
        item = by_name(results, "QuantPushChannelDisconnected")
        # for: 5m → 第一轮是 pending；换成 for=0m 的规则（RiskBlockedOrderAppeared 需要历史）。
        self.assertEqual(item["state"], "pending")
        immediate = build_engine(clock=lambda: self.clock["now"])
        immediate.rules = [rule for rule in immediate.rules
                           if rule["name"] == "RiskPendingManualConfirmation"]
        immediate._parse(immediate.rules)
        # RiskPendingManualConfirmation 是 for: 30m；换一条真正 for=0m 的：
        immediate.rules = [{"name": "Immediate", "expr": "quantwb_oms_orders > 0",
                            "for": "0m", "for_seconds": 0.0, "severity": "warning",
                            "domain": "risk", "group": "t", "labels": {}, "annotations": {},
                            "summary": None, "source_file": "inline", "metrics_required": [],
                            "supported": True, "unsupported_reason": None}]
        immediate._parse(immediate.rules)
        model = v3_alerts.build_model({"quantwb_oms_orders": [({"stage": "manual"}, 1.0)]})
        outcome = immediate.evaluate(model, now=self.clock["now"])
        self.assertEqual(outcome[0]["state"], "firing")

    def test_unsupported_rule_reports_unsupported(self):
        engine = build_engine(clock=lambda: self.clock["now"])
        engine.rules = [{"name": "Bad", "expr": "sum(quantwb_up) > 1", "for": "0m",
                         "for_seconds": 0.0, "severity": "warning", "domain": "t",
                         "group": "t", "labels": {}, "annotations": {}, "summary": None,
                         "source_file": "inline", "metrics_required": [], "supported": True,
                         "unsupported_reason": None}]
        engine._parse(engine.rules)
        outcome = engine.evaluate(v3_alerts.build_model({"quantwb_up": [1.0]}),
                                 now=self.clock["now"])
        self.assertEqual(outcome[0]["state"], "unsupported")
        self.assertIn("聚合函数", outcome[0]["evidence"]["reason"])
        self.assertIn("supported_subset", outcome[0]["evidence"])

    def test_history_gap_is_no_data_with_reason(self):
        results = self.run_rules(quantwb_mcp_calls_total=[10.0], quantwb_mcp_errors_total=[1.0])
        item = by_name(results, "QuantMcpToolCallFailureRateHigh")
        self.assertEqual(item["state"], "no-data")
        self.assertIn("窗口历史不足", item["evidence"]["reason"])

    def test_external_rule_is_no_data_with_judged_by_note(self):
        results = self.run_rules(quantwb_up=[1.0])
        item = by_name(results, "QuantWorkbenchDown")
        self.assertEqual(item["state"], "no-data")
        self.assertIn("外部抓取器", item["evidence"]["judged_by"])

    def test_summary_counts_cover_every_state(self):
        results = self.run_rules(quantwb_up=[1.0])
        counts = v3_alerts.summary_counts(results)
        self.assertEqual(counts["total"], len(results))
        self.assertEqual(counts["total"],
                         counts["firing"] + counts["pending"] + counts["ok"]
                         + counts["no-data"] + counts["unsupported"])


# ---------------------------------------------------------------------------
# 4. §8.3 补的每一条都**真的会响**（构造指标 + 构造历史）
# ---------------------------------------------------------------------------
class Spec83CompletionTests(unittest.TestCase):
    def setUp(self):
        self.clock = {"now": 1_800_000_000.0}
        self.engine = build_engine(clock=lambda: self.clock["now"])

    def drive(self, rule_name, metrics, *, history=None, advance=1200.0, rounds=2):
        """跑 ``rounds`` 轮（推进时钟）直到规则进入终态，返回该规则结果。"""
        model = v3_alerts.build_model(metrics)
        if history:
            self.engine.observe(model, self.clock["now"])  # 先占位（history 由下面直接注入）
        for _ in range(rounds):
            results = self.engine.evaluate(model, now=self.clock["now"])
            self.clock["now"] += advance
        return by_name(results, rule_name), by_name(
            self.engine.evaluate(model, now=self.clock["now"]), rule_name)

    def test_headless_success_rate_fires_below_95(self):
        low = {"quantwb_headless_success_rate": [0.90],
               "quantwb_headless_calls": [20.0]}
        state, _ = self.drive("HarnessHeadlessSuccessRateLow", low)
        self.assertEqual(state["state"], "firing")
        self.assertEqual(state["value"], 0.90)
        self.assertEqual(state["threshold"], 0.95)
        self.assertEqual(state["operator"], "<")
        # 反向：95% 恰好达标 → 不响（< 才算超限）
        other = build_engine(clock=lambda: self.clock["now"])
        for _ in range(3):
            other.evaluate(v3_alerts.build_model({"quantwb_headless_success_rate": [0.95]}),
                           now=self.clock["now"])
            self.clock["now"] += 1200
        healed = by_name(other.evaluate(
            v3_alerts.build_model({"quantwb_headless_success_rate": [0.95]}),
            now=self.clock["now"]), "HarnessHeadlessSuccessRateLow")
        self.assertEqual(healed["state"], "ok")

    def test_headless_duration_fires_above_60s(self):
        state, _ = self.drive("HarnessHeadlessDurationHigh",
                              {"quantwb_headless_call_duration_seconds": [75.0]})
        self.assertEqual(state["state"], "firing")
        self.assertEqual(state["threshold"], 60.0)
        slow = build_engine(clock=lambda: self.clock["now"])
        for _ in range(3):
            slow.evaluate(v3_alerts.build_model(
                {"quantwb_headless_call_duration_seconds": [60.0]}), now=self.clock["now"])
            self.clock["now"] += 1200
        self.assertEqual(by_name(slow.evaluate(v3_alerts.build_model(
            {"quantwb_headless_call_duration_seconds": [60.0]}), now=self.clock["now"]),
            "HarnessHeadlessDurationHigh")["state"], "ok")

    def test_sdk_active_sessions_fires_above_10(self):
        state, _ = self.drive("HarnessSdkActiveSessionsHigh",
                              {"quantwb_sdk_active_sessions": [({"source": "sdk_turns"}, 11.0)]})
        self.assertEqual(state["state"], "firing")
        self.assertEqual(state["threshold"], 10.0)
        quiet = build_engine(clock=lambda: self.clock["now"])
        for _ in range(3):
            quiet.evaluate(v3_alerts.build_model(
                {"quantwb_sdk_active_sessions": [({"source": "sdk_turns"}, 10.0)]}),
                now=self.clock["now"])
            self.clock["now"] += 600
        self.assertEqual(by_name(quiet.evaluate(v3_alerts.build_model(
            {"quantwb_sdk_active_sessions": [({"source": "sdk_turns"}, 10.0)]}),
            now=self.clock["now"]), "HarnessSdkActiveSessionsHigh")["state"], "ok")

    def test_mcp_latency_p95_fires_above_5s(self):
        state, _ = self.drive("ToolMcpCallLatencyP95High", {
            "quantwb_call_duration_p95_seconds": [({"scope": "mcp-tool-http"}, 6.5)],
            "quantwb_call_duration_window_samples": [({"scope": "mcp-tool-http"}, 40.0)]})
        self.assertEqual(state["state"], "firing")
        self.assertEqual(state["metric"], "quantwb_call_duration_p95_seconds")
        self.assertEqual(state["threshold"], 5.0)
        # 5.0 恰好等于阈值 → 不响（规则写的是 > 5）
        fast = build_engine(clock=lambda: self.clock["now"])
        metrics = {"quantwb_call_duration_p95_seconds": [({"scope": "mcp-tool-http"}, 5.0)]}
        for _ in range(3):
            fast.evaluate(v3_alerts.build_model(metrics), now=self.clock["now"])
            self.clock["now"] += 600
        self.assertEqual(by_name(fast.evaluate(v3_alerts.build_model(metrics),
                                               now=self.clock["now"]),
                                 "ToolMcpCallLatencyP95High")["state"], "ok")

    def test_order_execution_latency_fires_above_1s(self):
        state, _ = self.drive("TradeOrderExecutionLatencyHigh",
                              {"quantwb_order_execution_latency_max_seconds": [1.4]})
        self.assertEqual(state["state"], "firing")
        self.assertEqual(state["threshold"], 1.0)
        # 没有读数时是 no-data（不是 ok）
        absent = build_engine(clock=lambda: self.clock["now"])
        result = by_name(absent.evaluate(v3_alerts.build_model({"quantwb_up": [1.0]}),
                                         now=self.clock["now"]),
                         "TradeOrderExecutionLatencyHigh")
        self.assertEqual(result["state"], "no-data")

    def test_data_delay_fires_above_5min_with_fresh_probe(self):
        now = self.clock["now"]
        metrics = {"quantwb_datasource_data_age_seconds": [({"chain": "kline"}, 900.0)],
                   "quantwb_datasource_probe_timestamp_seconds": [({"chain": "kline"}, now)]}
        state, _ = self.drive("DataSourceDataDelayHigh", metrics)
        self.assertEqual(state["state"], "firing")
        self.assertEqual(state["threshold"], 300.0)
        # 探测缓存过期（> 24h）→ 守卫不成立 → 不响（也不拿旧结论下判断）
        fresh = build_engine(clock=lambda: self.clock["now"])
        stale_metrics = {
            "quantwb_datasource_data_age_seconds": [({"chain": "kline"}, 900.0)],
            "quantwb_datasource_probe_timestamp_seconds": [({"chain": "kline"},
                                                           self.clock["now"] - 90000.0)]}
        for _ in range(3):
            fresh.evaluate(v3_alerts.build_model(stale_metrics), now=self.clock["now"])
            self.clock["now"] += 600
        self.assertEqual(by_name(fresh.evaluate(v3_alerts.build_model(stale_metrics),
                                                now=self.clock["now"]),
                                 "DataSourceDataDelayHigh")["state"], "ok")

    def spike_engine(self, profile):
        """只保留 RiskBlockedSpike（``for`` 归零：``for`` 时钟由 EngineStateTests 覆盖）。"""
        now = self.clock["now"]
        history = {("quantwb_risk_blocked_total", ()): []}
        for index in range(int(2 * 3600 / 15) + 1):
            moment = now - 7200 + index * 15
            history[("quantwb_risk_blocked_total", ())].append((moment, profile(moment)))
        history[("quantwb_risk_blocked_total", ())].append((now, profile(now)))
        rules = []
        for rule in v3_alerts.load_rules():
            if rule["name"] == "RiskBlockedSpike":
                rule = dict(rule, for_seconds=0.0)
                rules.append(rule)
        engine = v3_alerts.AlertsEngine(rules, None, clock=lambda: self.clock["now"])
        engine._history = history
        return engine, now, profile(now)

    def test_risk_blocked_spike_uses_history_and_discriminates(self):
        now = self.clock["now"]
        # 前一小时 +0 单、近一小时 +4 单 → 突增（≥3 且 ≥3×）
        def spiking(moment):
            return 1.0 if moment <= now - 3600 else 1.0 + 4.0 * (moment - (now - 3600)) / 3600

        engine, moment, value = self.spike_engine(spiking)
        results = engine.evaluate(v3_alerts.build_model({"quantwb_risk_blocked_total": [value]}),
                                  now=moment, observe=False)
        spike = by_name(results, "RiskBlockedSpike")
        self.assertEqual(spike["state"], "firing")
        self.assertEqual(spike["threshold"], 3.0)
        self.assertEqual(spike["operator"], ">=")
        self.assertEqual(spike["value"], 4.0)

        # 基线同样高（每小时稳定 +4 单）→ 不算突增
        engine2, moment2, value2 = self.spike_engine(
            lambda item: 4.0 * (item - (now - 7200)) / 3600)
        results2 = engine2.evaluate(
            v3_alerts.build_model({"quantwb_risk_blocked_total": [value2]}),
            now=moment2, observe=False)
        self.assertEqual(by_name(results2, "RiskBlockedSpike")["state"], "ok")

        # 只有 2 单 → 不达绝对下限（规格没给数字，3 是运维约定，写在规则注释里）
        engine3, moment3, value3 = self.spike_engine(
            lambda item: 1.0 if item <= now - 3600 else 1.0 + 2.0 * (item - (now - 3600)) / 3600)
        results3 = engine3.evaluate(
            v3_alerts.build_model({"quantwb_risk_blocked_total": [value3]}),
            now=moment3, observe=False)
        self.assertEqual(by_name(results3, "RiskBlockedSpike")["state"], "ok")


# ---------------------------------------------------------------------------
# 5. 分位数口径（最近秩、样本不足不导出）
# ---------------------------------------------------------------------------
class PercentileTests(unittest.TestCase):
    def test_nearest_rank_returns_an_observed_value(self):
        values = [0.1, 0.2, 0.3, 0.4, 1.0]
        self.assertEqual(observability.percentile_nearest_rank(values, 0.50), 0.3)
        self.assertEqual(observability.percentile_nearest_rank(values, 0.95), 1.0)
        self.assertEqual(observability.percentile_nearest_rank(values, 1.0), 1.0)
        self.assertEqual(observability.percentile_nearest_rank([], 0.95), None)
        for quantile in (0.5, 0.95, 0.99):
            self.assertIn(observability.percentile_nearest_rank(values, quantile), values,
                          "最近秩必须返回窗口里真实出现过的样本（不插值）")

    def test_window_needs_enough_samples(self):
        window = observability.LatencyWindow(window=900, min_samples=3)
        window.record("mcp-tool-http", 1.0)
        window.record("mcp-tool-http", 2.0)
        self.assertIsNone(window.percentiles("mcp-tool-http"), "样本不足必须不导出")
        window.record("mcp-tool-http", 3.0)
        stats = window.percentiles("mcp-tool-http")
        self.assertEqual(stats["p50"], 2.0)
        self.assertEqual(stats["p95"], 3.0)

    def test_window_prunes_and_rejects_junk(self):
        window = observability.LatencyWindow(window=10.0, min_samples=1)
        self.assertTrue(window.record("headless-log", 1.0, at=100.0))
        self.assertFalse(window.record("headless-log", float("nan")), "NaN 不该进窗口")
        self.assertFalse(window.record("headless-log", -1.0), "负数不该进窗口")
        self.assertEqual(window.count("headless-log", now=105.0), 1)
        self.assertEqual(window.count("headless-log", now=200.0), 0, "窗口外的样本应被裁掉")


# ---------------------------------------------------------------------------
# 6. Prometheus 文本：本地解析器 + 格式校验器（promtool 的离线替代）
# ---------------------------------------------------------------------------
class PrometheusTextTests(unittest.TestCase):
    SAMPLE = (
        "# HELP quantwb_up 1 = alive\n"
        "# TYPE quantwb_up gauge\n"
        "quantwb_up 1\n"
        "# HELP quantwb_mcp_calls_total calls\n"
        "# TYPE quantwb_mcp_calls_total counter\n"
        "quantwb_mcp_calls_total 3\n"
        'quantwb_tool_calls_total{tool="plan",note="a\\"b"} 2\n'
    )

    def test_parse_roundtrip(self):
        model = v3_alerts.parse_prometheus_text(self.SAMPLE)
        self.assertTrue(model.present("quantwb_up"))
        self.assertEqual(model.scalar("quantwb_up"), 1.0)
        self.assertEqual(model.scalar("quantwb_tool_calls_total",
                                      (("tool", "plan"),)), 2.0)
        self.assertEqual(model.select("quantwb_tool_calls_total",
                                      (("tool", "plan"),))[0].label_dict()["note"], 'a"b')

    def test_parse_rejects_broken_lines(self):
        with self.assertRaises(v3_alerts.PrometheusParseError):
            v3_alerts.parse_prometheus_text("quantwb_up notanumber\n")
        with self.assertRaises(v3_alerts.PrometheusParseError):
            v3_alerts.parse_prometheus_text("this is not a sample\n")

    def test_checker_flags_structural_problems(self):
        problems = v3_alerts.check_prometheus_text(self.SAMPLE)
        self.assertEqual([item for item in problems if "不连续" in item], [])
        broken = ("# TYPE a gauge\na 1\na 2\n"
                  "# TYPE a gauge\na 3\n"
                  "b 1\n"
                  "# TYPE c gauge\n")
        found = " ".join(v3_alerts.check_prometheus_text(broken))
        self.assertIn("重复 TYPE", found)
        self.assertIn("缺 # HELP/# TYPE 声明", found)
        self.assertIn("声明了 TYPE/HELP 却没有样本", found)

    def test_checker_warns_about_ms_units(self):
        problems = v3_alerts.check_prometheus_text(
            "# TYPE futu_cooldown_remaining_ms gauge\nfutu_cooldown_remaining_ms 0\n")
        self.assertTrue(any("abbreviated units" in item for item in problems))

    def test_real_metrics_text_passes_local_checker(self):
        """真实出口文本必须过本地校验器（不连续 family / 缺声明都算违规）。"""
        home = Path(tempfile.mkdtemp(prefix="alerts-metrics-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        text = observability.build_metrics_text(str(home), app=None)
        problems = [item for item in v3_alerts.check_prometheus_text(text)
                    if "abbreviated units" not in item]
        self.assertEqual(problems, [], f"出口文本有结构性问题：{problems}")
        model = v3_alerts.parse_prometheus_text(text)
        self.assertTrue(model.present("quantwb_up"))
        self.assertTrue(model.present("quantwb_call_duration_window_seconds"))


# ---------------------------------------------------------------------------
# 7. 端点：只读、幂等、三态可见
# ---------------------------------------------------------------------------
class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="alerts-home-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self.clock = {"now": 1_800_000_000.0}
        self.model = None

        def collect():
            return self.model

        self.app = FastAPI()
        self.routes = v3_alerts.register(
            self.app, None, str(self.home),
            deps={"collect": collect, "sampler": False, "clock": lambda: self.clock["now"]})
        self.client = TestClient(self.app)

    def test_registers_exactly_two_read_only_routes(self):
        self.assertEqual(self.routes, ("/api/v3/ops/alerts", "/api/v3/ops/alerts/rules"))
        methods = {}
        for route in self.app.routes:
            path = getattr(route, "path", None)
            if path in self.routes:
                methods[path] = set(getattr(route, "methods", set()))
        self.assertEqual(methods, {path: {"GET"} for path in self.routes},
                         "求值端点必须是只读 GET")

    def test_register_is_idempotent(self):
        again = v3_alerts.register(self.app, None, str(self.home),
                                  deps={"sampler": False})
        self.assertEqual(again, self.routes)

    def test_alerts_endpoint_reports_three_states(self):
        self.model = v3_alerts.build_model({
            "quantwb_scheduler_alive": [0.0],          # → pending/firing
            "quantwb_scheduler_last_error": [0.0],     # → ok（== 1 不成立）
            # 其余指标缺席 → no-data（20 条左右）
        })
        body = self.client.get("/api/v3/ops/alerts").json()
        self.assertTrue(body["ok"])
        self.assertIn("非 Grafana", body["source"])
        self.assertEqual(body["rules_file"], str(ALERTS))
        counts = body["summary"]
        self.assertEqual(counts["total"], len(body["alerts"]))
        seen = {item["state"] for item in body["alerts"]}
        self.assertIn("no-data", seen)
        self.assertIn("ok", seen)
        self.assertIn("pending", seen)
        sample = body["alerts"][0]
        for key in ("rule", "severity", "expr", "value", "threshold", "state", "since",
                    "evidence"):
            self.assertIn(key, sample)
        # 推进时钟 → 至少一条 firing
        self.clock["now"] += 400
        self.client.get("/api/v3/ops/alerts")
        self.clock["now"] += 400
        body = self.client.get("/api/v3/ops/alerts").json()
        self.assertIn("firing", {item["state"] for item in body["alerts"]})
        fired = [item for item in body["alerts"] if item["state"] == "firing"]
        self.assertTrue(all(item["since"] for item in fired), "firing 必须有 since")

    def test_state_filter_and_unknown_state(self):
        self.model = v3_alerts.build_model({"quantwb_scheduler_alive": [1.0]})
        only_ok = self.client.get("/api/v3/ops/alerts?state=ok").json()
        self.assertTrue(all(item["state"] == "ok" for item in only_ok["alerts"]))
        bogus = self.client.get("/api/v3/ops/alerts?state=nonsense").json()
        self.assertEqual(bogus["summary"]["total"], len(bogus["alerts"]),
                         "非法 state 不该过滤掉任何东西")

    def test_collect_failure_is_an_honest_error_envelope(self):
        app = FastAPI()

        def boom():
            raise RuntimeError("采集器炸了")

        v3_alerts.register(app, None, str(self.home),
                           deps={"collect": boom, "sampler": False})
        body = TestClient(app).get("/api/v3/ops/alerts").json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "alerts/no-model")
        self.assertIn("采集器炸了", body["error"]["message"])

    def test_rules_endpoint_lists_the_shared_rule_file(self):
        body = self.client.get("/api/v3/ops/alerts/rules").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["count"], len(v3_alerts.load_rules()))
        self.assertEqual(body["rules_file"], str(ALERTS))
        self.assertTrue(all(item["supported"] for item in body["rules"]))
        self.assertIn("rate", body["supported_functions"])

    def test_real_app_exposes_the_alert_routes(self):
        """生产装配路径：create_app 之后两个端点必须存在（且不被 SPA 兜底吃掉）。"""
        from server.app import create_app  # 局部 import：只在需要真装配时付代价

        app = create_app(home=str(self.home))
        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/api/v3/ops/alerts", paths)
        self.assertIn("/api/v3/ops/alerts/rules", paths)
        self.assertEqual(app.state.v3_alerts["rules_file"], str(ALERTS))
        body = TestClient(app).get("/api/v3/ops/alerts/rules").json()
        self.assertEqual(body["count"], len(v3_alerts.load_rules()))


# ---------------------------------------------------------------------------
# 8. 规则文件与指标出口对得上（新增指标必须真的被导出）
# ---------------------------------------------------------------------------
class MetricsCoverageTests(unittest.TestCase):
    def declared(self):
        import ast

        tree = ast.parse((ROOT / "server" / "observability.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("gauge", "counter") and node.args \
                    and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                names.add(node.args[0].value)
        return names

    def test_every_metric_referenced_by_rules_is_exported(self):
        declared = self.declared()
        problems = []
        for rule in v3_alerts.load_rules():
            try:
                referenced = v3_alerts.required_metrics(v3_alerts.parse_expression(rule["expr"]))
            except v3_alerts.UnsupportedExpression:
                continue
            for name in sorted(referenced):
                if (name.startswith("quantwb_") or name.startswith("futu_")) \
                        and name not in declared:
                    problems.append(f"{rule['name']} 引用了出口不存在的指标 {name}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_spec83_completion_metrics_appear_in_real_output(self):
        """§8.3 补齐的指标必须真的出现在出口文本里（不是只写在文档里）。"""
        home = Path(tempfile.mkdtemp(prefix="alerts-out-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        # 数据延迟指标来自降级链探测缓存（含 as_of 与每级 attempts 的真实耗时）
        observability.record_datasource_probe(str(home), [{
            "key": "kline", "label": "K 线", "primary": "futu/quote_history_kline",
            "fallback": "akshare/stock_zh_a_hist", "available": True,
            "last_source": "futu/quote_history_kline", "chain_size": 2, "error": None,
            "as_of": "2026-09-20T00:00:00+00:00",
            "attempts": [{"source": "futu/quote_history_kline", "ok": True, "ms": 120}],
        }])
        text = observability.build_metrics_text(str(home), app=None)
        for name in ("quantwb_headless_window_seconds", "quantwb_headless_log_rows",
                     "quantwb_sdk_session_window_seconds",
                     "quantwb_sdk_active_sessions_source_missing",
                     "quantwb_call_duration_window_seconds", "quantwb_call_duration_min_samples",
                     "quantwb_call_duration_window_samples", "quantwb_risk_blocked_total",
                     "quantwb_datasource_data_age_seconds"):
            self.assertIn(f"# TYPE {name} ", text, f"出口缺少 {name}")
        model = v3_alerts.parse_prometheus_text(text)
        # SDK 无事实来源时：**不导出**活跃会话数，只导出「来源缺失」标记
        self.assertFalse(model.present("quantwb_sdk_active_sessions"))
        # 空 home 里没有已提交订单 → 订单延迟指标**按设计缺席**（不是导出 0）
        self.assertFalse(model.present("quantwb_order_execution_latency_max_seconds"))
        self.assertTrue(model.present("quantwb_sdk_active_sessions_source_missing"))

    def test_observability_feeds_percentile_window(self):
        """观测模块的取样喂给了分位数窗口（口径同一份，不是各算各的）。"""
        window = observability.reset_latency_window(window=3600, min_samples=1)
        self.addCleanup(observability.reset_latency_window)
        self.assertTrue(window.record("datasource-probe", 0.5))
        stats = window.percentiles("datasource-probe")
        self.assertEqual(stats["p50"], 0.5)


if __name__ == "__main__":
    unittest.main()
