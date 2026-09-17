"""WP15 阶段 A-1：会话补跑的启动纪律与文档一致性（2026-09-16 审查修订）。

背景（A-1）：规格 §10.1/§10.4-C 承诺「会话打开即补跑」的**自动性并不存在**——
队列领取端点、技能手册与文档声明都有，但没有承载启动接线的 instructions 条目。
本测试锁住修复后的两条事实：

  1. **承载**：仓库根 `AGENTS.md`（`@deepseek-ai/dsh-agent-instructions` 的工程链
     首文件）含有启动纪律：首次交互用 `research_tasks_claim` 探测队列，有积压先按
     `research-institute` 技能的值班模式消费再回应用户；
  2. **措辞**：文档与技能手册如实描述其限度（由启动纪律驱动、无 turn 不消费、
     任务不丢只延迟）——**不得再出现「打开会话即/自动补跑」式的无条件承诺**。

grep 式锁的作用是防再漂：句子里的自动性承诺一旦回潮，这里立刻失败。

F-1（2026-09-16 复验补锁）：技能手册的**启动/探测步骤**必须用工具面里真实存在的
`research_tasks_claim` 探测队列，**不得指示调用** `research_tasks_list`——该端点有意
整体排除在 MCP 工具面外（`platform/server/mcp_tools.py::MCP_EXCLUDED_ENDPOINTS`；
队列清单给人看，执行体只需领取）。判据边界：**探测步骤小节内**不得出现工具名
`research_tasks_list`，且该小节必须出现 `research_tasks_claim`；另外整本手册是执行体
面向的，给人看的清单能力一律用连字符端点名 `research-tasks-list` 表述，不得写成
带下划线的 MCP 工具名。

F-2（2026-09-16 复验补锁）：L3 定时器时刻是三处文档与单元文件共享的同一事实，
`install/research-duty.timer` 的 `OnCalendar` 是唯一真源；RUNBOOK / architecture 里
任何 `OnCalendar` 提及都必须与之同刻，且必须至少写明一次该时刻（防「只删不写」）；
timer 注释里的 cron 等价行也必须同刻。任一单边改值即失败。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "AGENTS.md"
ARCH = ROOT / "docs" / "architecture.md"
RUNBOOK = ROOT / "docs" / "RUNBOOK.md"
HANDOVER = ROOT / "docs" / "HANDOVER.md"
SKILL = ROOT / "skills" / "research-institute" / "SKILL.md"
INSTALL = ROOT / "install" / "HARNESS_SETUP.md"
TIMER = ROOT / "install" / "research-duty.timer"

#: timer 的唯一真源句式（单元文件与文档共用同一写法）
ONCAL_RE = re.compile(r"OnCalendar=Mon\.\.Fri\s+(\d{2}:\d{2})")
#: timer 注释里的 cron 等价行：分 时 日 月 周（分钟在前；可带 `#` 注释前缀，行尾可跟命令）
CRON_RE = re.compile(r"^[#\s]*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+1-5\b", re.M)
#: 启动/探测步骤小节的边界：标题到下一个同级或更高级标题
PROBE_SECTION_RE = re.compile(r"^###\s*启动探测.*?(?=^##\s|\Z)", re.M | re.S)

#: 带下划线的 MCP 工具名写法（不存在于工具面）；给人看的 Web 端点是连字符写法
EXCLUDED_TOOL_SPELLING = "research_tasks_list"


def _skill_startup_block():
    """抽取技能手册的「启动探测」小节（F-1 锁的判据边界）。"""
    match = PROBE_SECTION_RE.search(SKILL.read_text(encoding="utf-8"))
    return match.group(0) if match else ""


def _timer_time():
    """timer 的 OnCalendar 时刻（HH:MM）——文档必须与它一致。"""
    match = ONCAL_RE.search(TIMER.read_text(encoding="utf-8"))
    assert match is not None, "install/research-duty.timer 缺少 OnCalendar=Mon..Fri HH:MM"
    return match.group(1)

#: 承载启动纪律的文档集合：描述兜底路径时必须带限定语，不得只讲自动性
QUALIFIED_DOCS = (ARCH, RUNBOOK, HANDOVER, SKILL)

#: 无条件承诺（A-1 之前的措辞）：任何一处出现即失败
FORBIDDEN = (
    "打开会话时补跑",
    "打开会话即补跑",
    "会话打开时补跑",
    "打开会话时由 Harness 补跑",
    "打开会话即",
    "打开即补跑",
    "会自动补跑",
    "自动补跑积压",
    "只是等你打开会话时补跑",
)

#: 限定语：说明补跑由启动纪律驱动 / 非后台自动
QUALIFIERS = ("首次交互", "启动纪律", "AGENTS.md")


class StartupDisciplineTest(unittest.TestCase):
    def test_agents_md_exists_at_project_root(self):
        """工程根（.git 标记处）必须有 AGENTS.md——否则启动纪律无处承载。"""
        self.assertTrue((ROOT / ".git").exists(), "工程根以 .git 标记")
        self.assertTrue(AGENTS.is_file(), f"缺少 {AGENTS}")

    def test_agents_md_carries_the_duty_discipline(self):
        text = AGENTS.read_text(encoding="utf-8")
        # 探针必须是真实存在于工具面的领取工具；清单端点有意不在工具面
        self.assertIn("research_tasks_claim", text)
        self.assertIn("有意不在工具面", text)
        self.assertIn("research-institute", text)
        self.assertIn("值班模式", text)
        # 顺序：先消费积压，再处理用户请求；空队列才跳过
        self.assertIn("task = null", text)
        # 限度必须写明（不得宣称后台自动）
        self.assertIn("没有任何 turn", text)
        self.assertIn("不会消费队列", text)
        self.assertIn("任务不丢", text)

    def test_qualified_docs_state_the_limit_not_just_the_automaticity(self):
        for path in QUALIFIED_DOCS:
            text = path.read_text(encoding="utf-8")
            self.assertTrue(any(q in text for q in QUALIFIERS),
                            f"{path.name} 未写明兜底路径由启动纪律驱动（限定语缺失）")

    def test_no_unconditional_catch_up_promise_anywhere(self):
        targets = list(QUALIFIED_DOCS) + [AGENTS, INSTALL]
        for path in targets:
            text = path.read_text(encoding="utf-8")
            for phrase in FORBIDDEN:
                self.assertNotIn(phrase, text,
                                 f"{path.name} 出现无条件承诺措辞：{phrase}")

    def test_skill_duty_section_names_the_discipline(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertIn("会话首次交互补跑", text)
        self.assertIn("没有任何 turn", text)

    def test_install_doc_tells_operator_where_the_discipline_lives(self):
        text = INSTALL.read_text(encoding="utf-8")
        self.assertIn("AGENTS.md", text)
        self.assertIn("工作目录必须在本仓库内", text)


class DutyProbeAndTimerDriftTest(unittest.TestCase):
    """F-1/F-2 反向防漂移锁（2026-09-16 复验补锁）。"""

    # ---- F-1：启动探测必须指向工具面内的工具 ----

    def test_skill_has_a_delimited_startup_probe_section(self):
        """探测步骤必须自成小节，锁才有清晰判据边界（避免误伤给人看的说明）。"""
        block = _skill_startup_block()
        self.assertTrue(block, "SKILL.md 缺少「### 启动探测」小节")
        self.assertIn("research_tasks_claim", block,
                      "启动探测小节必须以 research_tasks_claim（领取即探测）为准")

    def test_startup_probe_section_does_not_call_the_excluded_tool(self):
        block = _skill_startup_block()
        self.assertNotIn(EXCLUDED_TOOL_SPELLING, block,
                         "启动探测小节指示了工具面外的 research_tasks_list；"
                         "应以 research_tasks_claim 的返回值探测队列")

    def test_manual_never_uses_the_excluded_tool_spelling(self):
        """执行体面向的手册不得把不存在的 MCP 工具名当成可调用工具（给人看的用连字符端点名）。"""
        text = SKILL.read_text(encoding="utf-8")
        self.assertNotIn(EXCLUDED_TOOL_SPELLING, text,
                         "技能手册出现工具面外的工具名 research_tasks_list；"
                         "给人看的清单能力请写 Web 端点 research-tasks-list")

    # ---- F-2：定时器时刻三处同源 ----

    def test_docs_match_the_timer_time(self):
        timer_time = _timer_time()
        for path in (RUNBOOK, ARCH):
            text = path.read_text(encoding="utf-8")
            for found in ONCAL_RE.findall(text):
                self.assertEqual(
                    found, timer_time,
                    f"{path.name} 的 OnCalendar=Mon..Fri {found} 与 timer 的 "
                    f"{timer_time} 不一致（时刻三处同源，单边改值即漂移）")
            self.assertIn(timer_time, text,
                          f"{path.name} 未写明值班唤醒时刻 {timer_time}")

    def test_timer_cron_equivalent_matches_oncalendar(self):
        timer_time = _timer_time()
        text = TIMER.read_text(encoding="utf-8")
        match = CRON_RE.search(text)
        self.assertIsNotNone(match, "timer 注释缺少 cron 等价行（分 时 日 月 周）")
        minute, hour = int(match.group(1)), int(match.group(2))
        expected_hour, expected_minute = (int(part) for part in timer_time.split(":"))
        self.assertEqual(
            (hour, minute), (expected_hour, expected_minute),
            f"timer 的 cron 等价行 {minute} {hour} 与 OnCalendar {timer_time} 不同刻")


if __name__ == "__main__":
    unittest.main()
