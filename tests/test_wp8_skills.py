"""WP8 补遗：富途官方 skills 参考集成（7 个，数据通道对接工作台）——锁定测试。

约束四件事：
* 七个 skill 目录 + README 存在，frontmatter 合法（name 以 ``futu-`` 开头且符合
  Harness 技能名规则，description 为非空中文一句，frontmatter 总长 ≤1024 字符）；
* 数据通道纪律：每个 SKILL.md 都有「数据通道」「工作流程」「安全」三段与免责声明句，
  README 有总览映射表、依赖说明与免责声明；
* 工具引用真实性（核心）：SKILL.md 里出现的每个 ``mcp__quantwb__<tool>`` 引用都必须
  真的在平台工具面（platform/server/mcp_tools.py 的 ``TOOLS``，41 工具）里——WP8 规划
  的行情域 quote_snapshot、screen_*/watchlist_*/fund_* 等**未交付工具不得写成可调用
  引用**，只能以「待交付」文字出现；capital/option 家族已由「富途实时数据直通」
  交付（rt_quote/rt_order_book/capital_flow/capital_flow_history/capital_distribution/
  option_expiration/option_chain/option_screen，工具面 33 → 41），capital-alerts 与
  derivatives-alerts 必须引用新工具且**不再保留** mcp__futu__ 只读降级段；
* 交易安全：trading skill 必须给 Web 确认卡片指引并如实说明 live 写通道未接入；
  任何 skill 都不得指引直连富途下单（富途写类 sim_trade_*/trading_* 动词与
  mcp__futu__ 写动词一律不出现）。
"""
import re
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
# 唯一事实来源：平台工具面清单（导入即自检 TOOL_COUNT 与 TOOLS 一致）。
sys.path.insert(0, str(_REPO / "platform"))

from server import mcp_tools  # noqa: E402

SKILLS_ROOT = _REPO / "skills" / "futu-skills"
README = SKILLS_ROOT / "README.md"

# 目录名 → frontmatter name（Harness 规则 ^[a-z0-9]+(-[a-z0-9]+)*$，本集成统一 futu- 前缀；
# 目录嵌套在 skills/futu-skills/ 下，由 agent.cordis.yml 的 customSkillDirs 单独挂载）。
EXPECTED_SKILLS = {
    "trading": "futu-trading",
    "technical-alerts": "futu-technical-alerts",
    "capital-alerts": "futu-capital-alerts",
    "derivatives-alerts": "futu-derivatives-alerts",
    "news-search": "futu-news-search",
    "sentiment": "futu-sentiment",
    "stock-digest": "futu-stock-digest",
}
HARNESS_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DISCLAIMER = "以上内容基于公开信息整理，不构成投资建议"
CHANNEL_SECTION = "## 数据通道"
WORKFLOW_SECTION = "## 工作流程"
SAFETY_SECTION = "## 安全"

# 规划中未交付的 quantwb 工具前缀（行情域 quote_*、筛选 screen_*、自选股 watchlist_*、
# 深度数据 fund_*/research_*/valuation_*/corp_*/holders_*/short_* 等）：只能以「待交付」
# 文字出现，绝不作为 mcp__quantwb__ 前缀的可调用引用。
# 资金/衍生品家族已由「富途实时数据直通」交付（capital_flow*/capital_distribution/
# option_expiration/option_chain/option_screen/rt_quote/rt_order_book），对应的
# flow_*/deriv_* 规划前缀随之移出禁入清单；confirm_decide 是人工批准通道
# （MCP_EXCLUDED_ENDPOINTS），连名字都禁入。
UNDELIVERED_PREFIXES = ("mcp__quantwb__quote", "mcp__quantwb__screen",
                        "mcp__quantwb__watchlist", "mcp__quantwb__ipo",
                        "mcp__quantwb__plate", "mcp__quantwb__info_",
                        "mcp__quantwb__fund_", "mcp__quantwb__research_",
                        "mcp__quantwb__valuation_", "mcp__quantwb__corp_",
                        "mcp__quantwb__holders_", "mcp__quantwb__short_",
                        "mcp__quantwb__confirm_decide")
# capital/derivatives 两技能已切换到已交付的实时直通工具；引用清单按技能钉死。
DELIVERED_CHANNEL_TOOLS = {
    "capital-alerts": ("capital_flow", "capital_flow_history", "capital_distribution"),
    "derivatives-alerts": ("option_expiration", "option_chain", "option_screen"),
}
# 富途写类工具动词（WP7 收窄族）：skills 里连名字都不该出现，出现即视为下单指引。
FUTU_WRITE_VERBS = re.compile(
    r"mcp__futu__\w*(input_order|place_order|modify_order|cancel_order)\w*")
# 「直接调富途下单」类指引的句式特征（禁止出现；「禁止直连富途下单」这类否定句不含「直接」）。
DIRECT_ORDER_GUIDANCE = re.compile(r"直接[^。\n]*(富途|futu|OpenD)[^。\n]*(下单|下单。|交易)")


def skill_text(sub):
    return (SKILLS_ROOT / sub / "SKILL.md").read_text(encoding="utf-8")


def frontmatter_of(text):
    """解析 ``---`` frontmatter 首层的 name/description（不引 yaml 依赖，够锁定用）。"""
    match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    if match is None:
        return None
    block = match[1]
    name = re.search(r"^name:[ \t]*(\S[^\r\n]*)$", block, re.MULTILINE)
    description = re.search(r"^description:[ \t]*(\S[^\r\n]*)$", block, re.MULTILINE)
    if name is None or description is None:
        return None
    return {"name": name[1].strip(), "description": description[1].strip(),
            "raw": block}


def quantwb_refs(text):
    """提取 ``mcp__quantwb__<tool>`` 引用；尾部 ``_`` 视作通配残留剥掉（防御 ``*_`` 写法）。"""
    return {matched.group(1).rstrip("_")
            for matched in re.finditer(r"mcp__quantwb__([a-z0-9_]+)", text)}


class FutuSkillsInventoryTests(unittest.TestCase):
    """存在性与 frontmatter 合法性。"""

    def test_seven_skill_directories_and_readme_exist(self):
        self.assertTrue(SKILLS_ROOT.is_dir(), "skills/futu-skills/ 目录不存在")
        self.assertTrue(README.is_file(), "缺少 skills/futu-skills/README.md")
        for sub in EXPECTED_SKILLS:
            skill = SKILLS_ROOT / sub / "SKILL.md"
            self.assertTrue(skill.is_file(), f"缺少 {skill.relative_to(_REPO)}")

    def test_frontmatter_valid_and_sections_present(self):
        for sub, expected_name in EXPECTED_SKILLS.items():
            with self.subTest(skill=sub):
                text = skill_text(sub)
                parsed = frontmatter_of(text)
                self.assertIsNotNone(parsed, f"{sub}/SKILL.md 缺少合法 frontmatter")
                self.assertEqual(parsed["name"], expected_name,
                                 f"{sub}/SKILL.md 的 name 必须是 {expected_name}")
                self.assertRegex(parsed["name"], HARNESS_SKILL_NAME,
                                 "name 必须符合 Harness 技能名规则（小写字母/数字/连字符）")
                self.assertGreater(len(parsed["description"]), 0, "description 不得为空")
                self.assertRegex(parsed["description"], r"[一-鿿]",
                                 "description 必须是中文一句")
                self.assertLessEqual(len(parsed["raw"]) + len(parsed["name"])
                                     + len(parsed["description"]), 1024,
                                     "frontmatter 总长不得超过 1024 字符")
                for section in (CHANNEL_SECTION, WORKFLOW_SECTION, SAFETY_SECTION):
                    self.assertIn(section, text, f"{sub}/SKILL.md 缺少「{section}」段")
                self.assertIn(DISCLAIMER, text, f"{sub}/SKILL.md 缺少免责声明句")


class FutuSkillsToolReferenceTests(unittest.TestCase):
    """核心：工具引用必须真实存在于平台工具面。"""

    def test_every_quantwb_reference_exists_in_platform_tools(self):
        for sub in EXPECTED_SKILLS:
            with self.subTest(skill=sub):
                refs = quantwb_refs(skill_text(sub))
                self.assertTrue(refs, f"{sub}/SKILL.md 没有任何 mcp__quantwb__ 工具引用")
                unknown = sorted(refs - set(mcp_tools.TOOL_NAMES))
                self.assertEqual(
                    unknown, [],
                    f"{sub}/SKILL.md 引用了平台工具面里不存在的 quantwb 工具：{unknown}"
                    f"（合法名单见 platform/server/mcp_tools.py 的 TOOLS，共 {mcp_tools.TOOL_COUNT} 个；"
                    "规划未交付的工具只能以「待交付」文字提及，不得写成可调用引用）")

    def test_undelivered_tools_only_mentioned_as_pending_text(self):
        """quote_*/screen_*/confirm_decide 等未交付工具只能以待交付文字出现。"""
        for sub in EXPECTED_SKILLS:
            text = skill_text(sub)
            with self.subTest(skill=sub):
                for prefix in UNDELIVERED_PREFIXES:
                    self.assertNotIn(prefix, text,
                                     f"{sub}/SKILL.md 把未交付/禁入工具写成了 mcp__quantwb__ 引用")

    def test_capital_and_derivatives_use_delivered_realtime_passthrough(self):
        """capital/derivatives 两技能：数据通道已切换为已交付的 quantwb 实时直通工具。

        「富途实时数据直通」交付后，两技能不得再保留 ``mcp__futu__`` 只读降级段，也不得
        再把通道写成「待交付」——技能必须引用新工具并如实按已交付口径工作。
        """
        for sub, tools in DELIVERED_CHANNEL_TOOLS.items():
            text = skill_text(sub)
            refs = quantwb_refs(text)
            with self.subTest(skill=sub):
                for tool in tools:
                    self.assertIn(tool, refs,
                                  f"{sub}/SKILL.md 必须引用已交付的 mcp__quantwb__{tool}")
                self.assertNotIn("mcp__futu__", text,
                                 f"{sub}/SKILL.md 不得再保留 mcp__futu__ 只读降级段")
                self.assertNotIn("待交付", text,
                                 f"{sub}/SKILL.md 的通道已交付，不得再标注待交付")
                self.assertNotIn("WP8 任务 5", text,
                                 f"{sub}/SKILL.md 不得再引用过期的交付来源标注")


class FutuSkillsTradingSafetyTests(unittest.TestCase):
    """交易安全：Web 确认卡片指引 + 禁止直连富途下单。"""

    def test_trading_skill_requires_web_confirmation_card(self):
        text = skill_text("trading")
        for needle in ("确认卡片", "trade_place", "trade_modify", "trade_cancel",
                       "live 写通道"):
            self.assertIn(needle, text, f"trading/SKILL.md 缺少关键指引：{needle}")

    def test_no_skill_directs_futu_ordering(self):
        for sub in EXPECTED_SKILLS:
            text = skill_text(sub)
            with self.subTest(skill=sub):
                for bad in ("sim_trade_input_order", "sim_trade_place_order",
                            "sim_trade_modify_order", "sim_trade_cancel_order",
                            "trading_input_order", "trading_place_order",
                            "trading_modify_order", "trading_cancel_order", "OpenD"):
                    self.assertNotIn(bad, text,
                                     f"{sub}/SKILL.md 出现了富途写类工具/网关指引：{bad}")
                self.assertIsNone(
                    FUTU_WRITE_VERBS.search(text),
                    f"{sub}/SKILL.md 的 mcp__futu__ 引用只能是只读工具，写动词一律禁止")
                self.assertIsNone(
                    DIRECT_ORDER_GUIDANCE.search(text),
                    f"{sub}/SKILL.md 不得含「直接调富途下单」类指引")


class FutuSkillsReadmeTests(unittest.TestCase):
    """README 总览：映射表 + 依赖说明 + 免责声明。"""

    def test_readme_lists_all_skills_with_channel_mapping(self):
        text = README.read_text(encoding="utf-8")
        for sub, name in EXPECTED_SKILLS.items():
            with self.subTest(skill=sub):
                self.assertIn(f"{sub}/SKILL.md", text, "README 必须列出技能路径")
                self.assertIn(name, text, "README 必须列出技能名")
        for needle in ("数据通道", "工作台", "依赖", "免责声明", DISCLAIMER):
            self.assertIn(needle, text, f"README 缺少：{needle}")

    def test_readme_states_channel_discipline_and_update_path(self):
        """通道纪律三条 + 更新路径（install_plugins.py update）必须写进 README。"""
        text = README.read_text(encoding="utf-8")
        self.assertIn("优先 quantwb", text, "通道纪律第一条：优先 quantwb 工具")
        self.assertIn("只读", text, "通道纪律第二条：工作台没有的数据用 mcp__futu__ 只读并标注")
        self.assertIn("trade_", text, "通道纪律第三条：写操作一律 quantwb trade_* + Web 确认")
        self.assertIn("install_plugins.py update", text, "README 必须写明更新路径")


if __name__ == "__main__":
    unittest.main()
