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
"""
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


if __name__ == "__main__":
    unittest.main()
