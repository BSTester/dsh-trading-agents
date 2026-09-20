# 仓库工作约定（DSH agent instructions）

本文件由 `@deepseek-ai/dsh-agent-instructions` 在会话开始时自动加载（工程根 = 本仓库，
以 `.git` 标记）。这里只写**每个会话都必须知道的纪律**；领域细节在 `docs/`（架构
`docs/architecture.md`、运维 `docs/RUNBOOK.md`、交接 `docs/HANDOVER.md`）。

## 一、值班研究员队列：会话首次交互的兜底纪律（L3）

平台有一个研究任务队列（`research_tasks`，由基础链作业 `enqueue_research` 每交易日入队：
每日资讯简报 `daily_brief`、因子巡检 `factor_patrol`、周度挖掘轮 `mining_round`）。
**主**消费路径是外部定时器（`install/research-duty.timer` → `scripts/research_duty.sh` →
`dsh --profile headless`）；本纪律是**兜底**路径：定时器没装、机器关机、headless 被杀时，
任务不会自动消失，只会积压。

**会话开始或恢复后的首次交互里：**

1. 调用 `research_tasks_claim` 探测队列（`research_tasks_list` **有意不在工具面**——队列
   清单给人看，执行体只需领取）；
2. 返回 `task` 非空 → **先进入 `research-institute` 技能的「值班模式」消费积压**：循环
   `claim` → 按任务手册处理 → `research_tasks_report`，直到返回 `task = null`，然后再处理
   用户的请求；产出只进研究页与规则候选池，**不碰交易、不启用策略**；
3. 返回 `task = null` → 队列为空，直接处理用户请求（不空转、不为此打断用户）。

**领取后必须回报（2026-09-16 审查 R2 修订）**：`claim` 到的每一条任务都要以
`research_tasks_report` 收尾——处理完传 `ok=true` + `result_ref`；**做不了或要放弃也必须
回报 `ok=false` 并写清原因**，绝不「领了就走」。理由：领取而未回报的任务会停在 `running`，
要等 30 分钟超时回收才回队列；连续三次超时会被判 failed——哪怕它从未被真正尝试过。
**被打断是可以的，静默离开队列不行。**

**重复回报是安全的（同次审查 R1）**：终态（`done`/`failed`）任务再回报会**原样返回、
不改状态、不重复告警**——工具调用重试或网络重放不会把已完成的任务拉回队列，
因此重试回报不必先查状态。

**限度（不得对外宣称自动）**：这不是后台自动动作——**没有任何 turn 的会话不会消费队列**
（任务不丢、只延迟）。平台服务未运行、或队列端点不可达时，如实说明拿不到队列状态，
不要臆测队列里有没有任务。

## 二、常驻纪律

- **分支**：本仓库直接提交 `main`（既有惯例）；每次提交保持三套测试全绿。
- **测试**：`~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests`、
  `node --test tests/*.test.mjs`、`cd platform/web-pro && npm test`（工作台是 antd pro 单页，
  由 FastAPI 挂在根路径 `/`；构建串行：`flock /tmp/probuild.lock npm run build`）。
- **交易边界**：任何下单/改单/撤单/切模式/执行计划都只经工作台受约束入口；
  研究侧（值班模式、研究院）**永不下单、永不启用策略**——启用只能由人在 Web 点批准。
- **数据诚实**：取不到数据就说取不到，标注来源与 as_of，不用估算值替代。
