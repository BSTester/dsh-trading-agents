"""WP14 任务 5：研究院编排技能 —— 锁定测试。

锁四件事（防「文档与实现漂移」）：

* frontmatter 合法（Harness 技能名规则、description 非空、总长 ≤1024）+ 免责声明句；
* 四子代理分工与硬规则条款在场（三要素、未成熟因子、不得直接下单）；
* **禁用端点清单在场**：五个人在环写工具点名禁止；`rules-decide`/`auto_pipeline`/
  `confirm-decide` 三个「只属于人」的端点既被点名、又**确实不在工具面**（模型自批无路）；
* **工具引用真实性**：SKILL.md 里每个 ``mcp__quantwb__<tool>`` 引用都必须真的在平台
  工具面（``platform/server/mcp_tools.py`` 的 ``TOOLS``）；fin-data / engine 侧引用也要
  在对应插件源码里能找到。

外加**协议样例锁**：SKILL.md 里那段 rules JSON 必须能通过真实的
``rule_engine.validate_spec``——文档写的协议就是解释器认的协议。
"""
import json
import re
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "platform"))
sys.path.insert(0, str(_REPO / "plugins" / "core" / "python"))
sys.path.insert(0, str(_REPO / "plugins" / "datasource" / "python"))

from server import mcp_tools  # noqa: E402
from trading_core import rule_engine  # noqa: E402

SKILL_PATH = _REPO / "skills" / "research-institute" / "SKILL.md"
TEXT = SKILL_PATH.read_text(encoding="utf-8")

HARNESS_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DISCLAIMER = "以上内容基于公开信息整理，不构成投资建议"

SUBAGENTS = ("采集代理", "假设代理", "检验代理", "研报代理")

#: 人在环写工具：必须在技能里被点名禁止，且必须真在工具面（命名真实）。
FORBIDDEN_WRITE_TOOLS = ("mcp__quantwb__trade_place", "mcp__quantwb__trade_modify",
                         "mcp__quantwb__trade_cancel", "mcp__quantwb__plan_execute",
                         "mcp__quantwb__switch_mode")
#: 只属于人的端点：被点名禁止，且**不在**工具面（连名字都不该可调用）。
HUMAN_ONLY_ENDPOINTS = ("rules-decide", "auto_pipeline", "confirm-decide")

JSON_BLOCK = re.compile(r"```json\n(.*?)```", re.DOTALL)
QUANTWB_REF = re.compile(r"mcp__quantwb__([a-z0-9_]+)")


def _frontmatter(text):
    """返回 (frontmatter 文本, 正文)；无 frontmatter 返回 (None, text)。"""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return None, text
    return text[4:end], text[end + 5:]


class SkillFileTest(unittest.TestCase):
    def test_skill_file_exists(self):
        self.assertTrue(SKILL_PATH.is_file(), SKILL_PATH)

    def test_frontmatter_valid(self):
        front, body = _frontmatter(TEXT)
        self.assertIsNotNone(front, "SKILL.md 必须有 YAML frontmatter")
        self.assertLessEqual(len(front), 1024, "frontmatter 总长须 ≤1024 字符")
        name = re.search(r"^name:\s*(\S+)\s*$", front, re.MULTILINE)
        self.assertIsNotNone(name, front)
        self.assertEqual(name.group(1), "research-institute")
        self.assertRegex(name.group(1), HARNESS_SKILL_NAME)
        desc = re.search(r"^description:\s*(.+)$", front, re.MULTILINE)
        self.assertIsNotNone(desc, front)
        self.assertGreater(len(desc.group(1).strip()), 20, "description 太短")
        self.assertTrue(body.strip(), "正文不得为空")

    def test_disclaimer_present(self):
        self.assertIn(DISCLAIMER, TEXT)

    def test_description_states_no_order_no_self_approval(self):
        """描述里就要讲清边界：不直接下单、不直接启用策略。"""
        front, _ = _frontmatter(TEXT)
        self.assertIn("不直接下单", front)
        self.assertIn("不直接启用策略", front)


class SubagentDivisionTest(unittest.TestCase):
    def test_four_subagents_named(self):
        for name in SUBAGENTS:
            self.assertIn(name, TEXT, f"缺少子代理：{name}")

    def test_each_subagent_has_tool_channel(self):
        """每个子代理行都要给出可调用通道（同表一行内出现工具引用）。"""
        for name in SUBAGENTS:
            line = next((ln for ln in TEXT.splitlines() if name in ln), None)
            self.assertIsNotNone(line, name)
            self.assertTrue(
                "mcp__quantwb__" in line or "fin_" in line or "research_publish" in line
                or "rules-validate" in line,
                f"{name} 未给出工具通道：{line}")

    def test_routing_against_other_paths(self):
        """与快路径/12 角色流程的分工必须写明（避免抢跑重型流程）。"""
        self.assertIn("quant-trading", TEXT)
        self.assertIn("trading-agents", TEXT)
        self.assertIn("12 角色", TEXT)


class BoundaryClauseTest(unittest.TestCase):
    def test_evidence_three_elements(self):
        self.assertIn("平台", TEXT)
        self.assertIn("互动数", TEXT)
        self.assertIn("未核实", TEXT)

    def test_immature_factor_clause(self):
        self.assertIn("250 交易日", TEXT)
        for prefix in rule_engine.IMMATURE_FACTOR_PREFIXES:
            self.assertIn(prefix, TEXT, f"未成熟因子域 {prefix} 未写明")

    def test_no_direct_order_clause(self):
        self.assertIn("不得直接下单", TEXT)

    def test_validation_failure_no_retry_clause(self):
        """验证不过不许调阈值重跑（多重检验纪律）。"""
        self.assertIn("不调阈值", TEXT)

    def test_approval_is_web_only_clause(self):
        self.assertIn("Web", TEXT)
        self.assertIn("批准", TEXT)


class ForbiddenEndpointTest(unittest.TestCase):
    def test_write_tools_named_and_real(self):
        for tool in FORBIDDEN_WRITE_TOOLS:
            self.assertIn(tool, TEXT, f"禁用写工具未点名：{tool}")
            name = tool.removeprefix("mcp__quantwb__")
            self.assertIn(name, mcp_tools.TOOL_NAMES, f"点名了不存在的工具：{tool}")

    def test_human_only_endpoints_named_and_absent_from_tools(self):
        for endpoint in HUMAN_ONLY_ENDPOINTS:
            self.assertIn(endpoint, TEXT, f"只属于人的端点未点名：{endpoint}")
            self.assertNotIn(endpoint.replace("-", "_"), mcp_tools.TOOL_NAMES,
                             f"{endpoint} 不得进 MCP 工具面（模型自批通道）")

    def test_rules_read_tool_is_allowed(self):
        """只读的候选池查询是允许的（与批准分离）。"""
        self.assertIn("mcp__quantwb__rules", TEXT)
        self.assertIn("rules", mcp_tools.TOOL_NAMES)


class ToolReferenceTruthTest(unittest.TestCase):
    def test_every_quantwb_reference_exists(self):
        referenced = set(QUANTWB_REF.findall(TEXT))
        self.assertTrue(referenced, "技能里没有任何 quantwb 工具引用")
        missing = sorted(name for name in referenced if name not in mcp_tools.TOOL_NAMES)
        self.assertEqual(missing, [], f"引用了工具面里不存在的工具：{missing}")

    def test_fin_data_references_exist(self):
        source = (_REPO / "plugins" / "fin-data" / "src" / "index.js").read_text(
            encoding="utf-8")
        for tool in ("fin_news", "fin_sentiment"):
            self.assertIn(tool, TEXT)
            self.assertIn(f'"{tool}"', source, f"fin-data 未提供 {tool}")

    def test_engine_references_exist(self):
        source = (_REPO / "plugins" / "engine" / "src" / "tools.js").read_text(
            encoding="utf-8")
        for tool in ("research_publish", "run_trading_analysis"):
            self.assertIn(tool, TEXT)
            self.assertIn(f'"{tool}"', source, f"engine 未提供 {tool}")

    def test_bridge_skill_cross_reference(self):
        self.assertIn("futulast30days-bridge", TEXT)
        self.assertTrue((_REPO / "skills" / "last30days-bridge" / "SKILL.md").is_file())


class SpecExampleTest(unittest.TestCase):
    """文档里的协议样例必须能被真实解释器接受（防文档与实现漂移）。"""

    def setUp(self):
        block = JSON_BLOCK.search(TEXT)
        self.assertIsNotNone(block, "SKILL.md 必须含一个 ```json 规则样例")
        self.spec = json.loads(block.group(1))

    def test_example_passes_real_validator(self):
        ok, errors = rule_engine.validate_spec(self.spec)
        self.assertTrue(ok, f"技能里的协议样例未通过真实校验器：{errors}")

    def test_example_matches_protocol_contract(self):
        self.assertEqual(self.spec["provenance"]["created_by"], rule_engine.PROPOSER)
        self.assertIn(self.spec["combine"], rule_engine.COMBINES)
        self.assertIn(self.spec["rebalance"], rule_engine.REBALANCES)
        self.assertLessEqual(set(self.spec), set(rule_engine.SPEC_FIELDS))
        for name in self.spec["factors"]:
            self.assertNotIn(name, rule_engine.IMMATURE_FACTOR_PREFIXES)

    def test_example_rule_loads_as_strategy(self):
        """样例不只是能校验，还要真能构造出策略实例（可被 plan_auto 消费）。"""
        from trading_core import strategies
        instance = rule_engine.load_rule(self.spec)
        self.assertTrue(hasattr(instance, "universe"))
        self.assertTrue(hasattr(instance, "target_weights"))
        del strategies  # 仅为说明加载路径经 strategies.register_rule 同一实现


if __name__ == "__main__":
    unittest.main()
