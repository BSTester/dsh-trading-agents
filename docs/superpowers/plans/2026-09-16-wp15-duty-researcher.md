# WP15 值班研究员（L3）实现计划

> **面向 AI 代理的工作者：** 必需子技能：superpowers:subagent-driven-development（推荐）逐任务实现，两阶段审查。步骤 `- [ ]` 跟踪。
> **规格**：`docs/superpowers/specs/2026-09-16-wp9-15-autopipeline-research-institute-design.md` §十。全局约定见索引（§2.6 三层边界）。
> 三套测试保持全绿；提交 `feat(core|platform|install): 摘要`。

**目标**：定时/开机两条路径都能消费同一任务队列：外部定时器 headless 唤醒为主、会话打开补跑兜底；白名单三 kind、结构化 payload、当日幂等、attempts 封顶；L3 全程零交易写通道。

**架构**：`research_tasks` 表状态机（pending/running/done/failed + 超时回收）；L1 的 `enqueue_research` 作业只入队（零 LLM）；执行体是 Harness（headless 或会话），经两个新端点领取/回报；服务进程不调 LLM。

---

### 任务 1：research_tasks 表 + 状态机

**文件**：修改 `plugins/core/python/trading_core/store.py`；测试 `tests/test_wp15_queue.py`。

- [ ] **步骤 1：失败测试**

```python
def test_task_lifecycle(self):
    # enqueue_task(conn, kind, as_of, market, payload) → 当日幂等（唯一索引 kind+as_of+market，
    # 重复入队返回已有 task_id 不新建）
    # claim_task(conn, now) → 取最早 pending 置 running（started_at=now），无则 None
    # finish_task(conn, task_id, ok, result_ref=None, err=None) → done/failed
def test_timeout_recovery(self):
    # running 且 started_at < now-30min → reclaim(conn, now) 置回 pending，attempts+1
    # attempts>=3 → failed + warn 告警（alerts 表断言）
def test_payload_whitelist(self):
    # enqueue 侧校验：kind 不在 {"daily_brief","factor_patrol","mining_round"} → ValueError
    # payload 含自由文本键（除 as_of/market/refs/digest_ref/symbols/factor_list/window 外任何键）→ ValueError
    ...
```

- [ ] **步骤 2：验证失败** → **步骤 3：实现**：表进 `_SCHEMA`（`task_id TEXT PRIMARY KEY, kind, as_of, market, payload, status, attempts INTEGER DEFAULT 0, created_at, started_at, finished_at, result_ref, err`，唯一索引 `(kind, as_of, market)`）；payload 允许键白名单常量 `TASK_PAYLOAD_KEYS`。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): research_tasks 队列状态机（幂等/超时回收/attempts 封顶）"`

### 任务 2：enqueue_research 作业入链

**文件**：修改 `plugins/core/python/trading_core/daemon.py`（`build_jobs` 追加 `enqueue_research` 作业，digest 之后）、新建 `cli.py` 子命令 `enqueue-research`；测试 `tests/test_wp15_enqueue.py`。

- [ ] **步骤 1：失败测试**：
  ① 每交易日：digest ran 后入队 `daily_brief`（payload：as_of/market/refs=[digest kv, sentiment 当日, f10 当日]）+ `factor_patrol`（factor_list=REGISTRY 键, window=120）；
  ② 周一（或本周一已过且本周未入队）：`mining_round`（候选域 refs）；
  ③ 当日重复 tick → 幂等不重复入队；auto_pipeline.enabled=False → **仍入队**（研究任务独立于交易开关——口径写注释），但 L3 执行体在 skill 里会跳过执行吗？**不跳过**：研究与交易解耦，简报/巡检始终产出。测试断言两行为各自成立；
  ④ 入队零 LLM（子进程/网络零调用——假件断言）。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**：`enqueue-research --market SH [--date D]`；refs 组装读 kv 与表（sentiment_snapshots 当日行数、f10_snapshots 当日覆盖率）。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(core): enqueue-research 作业——三 kind 白名单入队（零 LLM，研究与交易开关解耦）"`

### 任务 3：领取/回报端点 + 兜底补跑

**文件**：修改 `platform/server/app.py`/`compute.py`（`research-tasks` 动作端点：`{action: "claim"|"report"|"list"}`；claim 返回 `{task_id, kind, payload}` 并落领取侧**二次白名单校验**（store 层重验 kind/键——双端校验，规格 §10.3）；report `{task_id, ok, result_ref?, err?}`）；`skills/research-institute/SKILL.md` 追加「值班模式」节；测试 `tests/test_wp15_endpoint.py` + skill 测试更新。

- [ ] **步骤 1：失败测试**：
  ① claim：pending → running 返回载荷；队列空 → `{task: null}`（幂等语义明确）；
  ② report ok → done + result_ref；report err → attempts+1，<3 置回 pending，≥3 failed+告警；
  ③ **两端点进 MCP 工具面**（`research_tasks_claim/report`——L3 执行体是 Harness 会话，经 MCP 调用最顺；读+受控写，非交易写；工具计数 74+2=**76** ≤80 断言更新）；`list` 动作 HTTP-only；
  ④ 领取侧对 payload 再校验：被篡改（自由文本键）→ 拒绝领取 + critical 告警（队列即攻击面，规格 §十一.13）；
  ⑤ skill 值班模式节存在性断言（frontmatter/动作清单/三 kind 执行手册各一段：daily_brief 读 refs→research_publish 简报；factor_patrol 读 factor_snapshots→ic_report→衰减告警；mining_round→候选提案→rules-validate）。
- [ ] **步骤 2：验证失败** → **步骤 3：实现**：动作端点模式照 `plan-execute`（不进缓存形状表）；MCP 两工具绑定同一 provider。
- [ ] **步骤 4：通过**；三套绿。
- [ ] **步骤 5：Commit**：`git commit -m "feat(platform): research-tasks 领取/回报端点（双端校验）+skill 值班模式"`

### 任务 4：headless 唤醒脚本 + 定时器交付

**文件**：新建 `scripts/research_duty.sh`（唤醒入口：`dsh --profile headless "值班研究员：调用 research_tasks_claim 逐条执行队列任务直到返回 null，每条按 research-institute 技能值班模式手册处理并 research_tasks_report"`）与 `install/research-duty.timer`+`.service`（systemd 单元，OnCalendar 每交易日 16:50 本地，文档同时给 cron 行）；`docs/RUNBOOK.md`/`docs/HANDOVER.md` 追加；测试 `tests/test_wp15_install.py`（脚本/单元文件存在性+内容断言：脚本含 `--profile headless` 与循环退出条件；timer 含 OnCalendar）。

- [ ] **步骤 1：失败测试** → **步骤 2：实现**（脚本含总时长上限 `timeout 1800` 包裹；无凭据/无 dsh 时退出码非零并打印排查行——RUNBOOK 指引）。
- [ ] **步骤 3：三套绿**。
- [ ] **步骤 4：Commit**：`git commit -m "feat(install): 值班研究员定时器与 headless 唤醒脚本+运维文档"`

### 任务 5：端到端 + 全局验收

**文件**：测试 `tests/test_wp15_e2e.py`；全局验收记录。

- [ ] **步骤 1：e2e（假时钟+假执行体）**：D1 16:20 tick → 队列 3 任务（周一含 mining_round）→ claim×3 模拟执行 report ok → done；定时器不跑 → D2 会话路径 claim 补跑昨日 failed（attempts<3 回 pending）成功；断言全链零交易写通道调用（假 broker 断言零 place）。
- [ ] **步骤 2：三套绿 + 全局验收清单（索引 §三.2）逐项核对**：回归（enabled=false 行为一致）/两日闭环/规则端到端/L3 演练/文档七处修订一致性。
- [ ] **步骤 3：Commit**：`git commit -m "test(platform): L3 队列端到端+全局验收记录"`

### WP15 验收（对照规格 §10.6）

- [ ] 状态机/白名单/双端校验/兜底/边界回归/定时器文档全过；服务进程零 LLM 调用断言；三套全绿。
