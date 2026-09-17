# WP15 值班研究员（L3）实现计划

> **面向 AI 代理的工作者：** 必需子技能：superpowers:subagent-driven-development（推荐）逐任务实现，两阶段审查。步骤 `- [ ]` 跟踪。
> **规格**：`docs/superpowers/specs/2026-09-16-wp9-15-autopipeline-research-institute-design.md` §十。全局约定见索引（§2.6 三层边界）。
> 三套测试保持全绿；提交 `feat(core|platform|install): 摘要`。

**目标**：定时/开机两条路径都能消费同一任务队列：外部定时器 headless 唤醒为主、会话打开补跑兜底；白名单三 kind、结构化 payload、当日幂等、attempts 封顶；L3 全程零交易写通道。

**架构**：`research_tasks` 表状态机（pending/running/done/failed + 超时回收）；L1 的 `enqueue_research` 作业只入队（零 LLM）；执行体是 Harness（headless 或会话），经两个新端点领取/回报；服务进程不调 LLM。

---

### 任务 1：research_tasks 表 + 状态机

**文件**：修改 `plugins/core/python/trading_core/store.py`；测试 `tests/test_wp15_queue.py`。

- [x] **步骤 1：失败测试**

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

- [x] **步骤 2：验证失败** → **步骤 3：实现**：表进 `_SCHEMA`（`task_id TEXT PRIMARY KEY, kind, as_of, market, payload, status, attempts INTEGER DEFAULT 0, created_at, started_at, finished_at, result_ref, err`，唯一索引 `(kind, as_of, market)`）；payload 允许键白名单常量 `TASK_PAYLOAD_KEYS`。
- [x] **步骤 4：通过**；三套绿。
- [x] **步骤 5：Commit**：`git commit -m "feat(core): research_tasks 队列状态机（幂等/超时回收/attempts 封顶）"`

### 任务 2：enqueue_research 作业入链

**文件**：修改 `plugins/core/python/trading_core/daemon.py`（`build_jobs` 追加 `enqueue_research` 作业，digest 之后）、新建 `cli.py` 子命令 `enqueue-research`；测试 `tests/test_wp15_enqueue.py`。

- [x] **步骤 1：失败测试**：
  ① 每交易日：digest ran 后入队 `daily_brief`（payload：as_of/market/refs=[digest kv, sentiment 当日, f10 当日]）+ `factor_patrol`（factor_list=REGISTRY 键, window=120）；
  ② 周一（或本周一已过且本周未入队）：`mining_round`（候选域 refs）；
  ③ 当日重复 tick → 幂等不重复入队；auto_pipeline.enabled=False → **仍入队**（研究任务独立于交易开关——口径写注释），但 L3 执行体在 skill 里会跳过执行吗？**不跳过**：研究与交易解耦，简报/巡检始终产出。测试断言两行为各自成立；
  ④ 入队零 LLM（子进程/网络零调用——假件断言）。
- [x] **步骤 2：验证失败** → **步骤 3：实现**：`enqueue-research --market SH [--date D]`；refs 组装读 kv 与表（sentiment_snapshots 当日行数、f10_snapshots 当日覆盖率）。
- [x] **步骤 4：通过**；三套绿。
- [x] **步骤 5：Commit**：`git commit -m "feat(core): enqueue-research 作业——三 kind 白名单入队（零 LLM，研究与交易开关解耦）"`

### 任务 3：领取/回报端点 + 兜底补跑

**文件**：修改 `platform/server/app.py`/`compute.py`（`research-tasks` 动作端点：`{action: "claim"|"report"|"list"}`；claim 返回 `{task_id, kind, payload}` 并落领取侧**二次白名单校验**（store 层重验 kind/键——双端校验，规格 §10.3）；report `{task_id, ok, result_ref?, err?}`）；`skills/research-institute/SKILL.md` 追加「值班模式」节；测试 `tests/test_wp15_endpoint.py` + skill 测试更新。

- [x] **步骤 1：失败测试**：
  ① claim：pending → running 返回载荷；队列空 → `{task: null}`（幂等语义明确）；
  ② report ok → done + result_ref；report err → attempts+1，<3 置回 pending，≥3 failed+告警；
  ③ **两端点进 MCP 工具面**（`research_tasks_claim/report`——L3 执行体是 Harness 会话，经 MCP 调用最顺；读+受控写，非交易写；工具计数 74+2=**76** ≤80 断言更新）；`list` 动作 HTTP-only；
  ④ 领取侧对 payload 再校验：被篡改（自由文本键）→ 拒绝领取 + critical 告警（队列即攻击面，规格 §十一.13）；
  ⑤ skill 值班模式节存在性断言（frontmatter/动作清单/三 kind 执行手册各一段：daily_brief 读 refs→research_publish 简报；factor_patrol 读 factor_snapshots→ic_report→衰减告警；mining_round→候选提案→rules-validate）。
- [x] **步骤 2：验证失败** → **步骤 3：实现**：动作端点模式照 `plan-execute`（不进缓存形状表）；MCP 两工具绑定同一 provider。
- [x] **步骤 4：通过**；三套绿。
- [x] **步骤 5：Commit**：`git commit -m "feat(platform): research-tasks 领取/回报端点（双端校验）+skill 值班模式"`

### 任务 4：headless 唤醒脚本 + 定时器交付

**文件**：新建 `scripts/research_duty.sh`（唤醒入口：`dsh --profile headless "值班研究员：调用 research_tasks_claim 逐条执行队列任务直到返回 null，每条按 research-institute 技能值班模式手册处理并 research_tasks_report"`）与 `install/research-duty.timer`+`.service`（systemd 单元，OnCalendar 每交易日 **19:20** 本地——排在 `reconcile`(19:00) → digest → `enqueue_research`(19:05) 之后（审查 A-2 修订），文档同时给 cron 行）；`docs/RUNBOOK.md`/`docs/HANDOVER.md` 追加；测试 `tests/test_wp15_install.py`（脚本/单元文件存在性+内容断言：脚本含 `--profile headless` 与循环退出条件；timer 含 OnCalendar 与时刻依据链）。

- [x] **步骤 1：失败测试** → **步骤 2：实现**（脚本含总时长上限 `timeout 1800` 包裹；无凭据/无 dsh 时退出码非零并打印排查行——RUNBOOK 指引）。
- [x] **步骤 3：三套绿**。
- [x] **步骤 4：Commit**：`git commit -m "feat(install): 值班研究员定时器与 headless 唤醒脚本+运维文档"`

### 任务 5：端到端 + 全局验收

**文件**：测试 `tests/test_wp15_e2e.py`；全局验收记录。

- [x] **步骤 1：e2e（假时钟+假执行体）**：D1 16:20 tick → 队列 3 任务（周一含 mining_round）→ claim×3 模拟执行 report ok → done；定时器不跑 → D2 会话路径 claim 补跑昨日 failed（attempts<3 回 pending）成功；断言全链零交易写通道调用（假 broker 断言零 place）。
- [x] **步骤 2：三套绿 + 全局验收清单（索引 §三.2）逐项核对**：回归（enabled=false 行为一致）/两日闭环/规则端到端/L3 演练/文档七处修订一致性。
- [x] **步骤 3：Commit**：`git commit -m "test(platform): L3 队列端到端+全局验收记录"`

### WP15 验收（对照规格 §10.6）

- [x] 状态机/白名单/双端校验/兜底/边界回归/定时器文档全过；服务进程零 LLM 调用断言；三套全绿。

---

## 全局验收记录（WP15 任务 5，2026-09-17）

### 一、三套测试（终态）

| 套件 | 命令 | 结果 |
|---|---|---|
| Python | `~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests` | **Ran 1969 tests — OK (skipped=5)** |
| Node 根 | `node --test tests/*.test.mjs` | **66 pass / 0 fail** |
| Web | `cd platform/web && npm test` | **241 pass / 0 fail** |

WP15 新增测试：`tests/test_wp15_install.py`（8 项：脚本契约 / 单元接线 / 三条失败路径真跑 / 超时中止 / 日志落盘）、
`tests/test_wp15_e2e.py`（4 项：全队列消费 / headless 被杀次日回收重试 / 零交易写+未批准规则零消费 / 失败重试到 failed+告警）。

### 二、全局验收清单（索引 §三.2）逐项结论

| # | 验收项 | 结论 | 证据 |
|---|---|---|---|
| 1 | `auto_pipeline.enabled=false` 时交易行为零变化 | ✅ 通过 | `test_wp9_build_jobs.py::test_disabled_equals_jobs_default`（与 `JOBS_DEFAULT` 逐键逐值相等）、`test_wp9_auto_execute.py::test_disabled_silently_skips`、`test_wp9_e2e.py::test_disabled_pipeline_produces_no_artifacts`（同一时间线零自动产物）；入队侧另有关闭态回归（`test_wp15_enqueue.py` ③：研究任务与交易开关解耦） |
| 2 | 假时钟 sim 两交易日零人工闭环 | ✅ 通过 | `test_wp9_e2e.py::test_full_chain_fills_end_to_end`、`::test_small_weight_order_submits_end_to_end`、`::test_exit_chain_sells_dropped_holding_end_to_end`、`::test_oversized_order_still_blocked_by_risk_rules`（风控仍拦） |
| 3 | 规则「提案→检验→批准→执行」端到端 | ✅ 通过 | `test_wp14_e2e.py`（含停用即时生效的 fail-open 修复回归）、`test_wp14_approve.py`（批准仅经服务内 Web 端点） |
| 4 | L3「入队→领取→兜底补跑」演练 | ✅ 通过 | `test_wp15_e2e.py` 四项（含 D1 领走不回报 → D2 回收重试成功；全链零交易写反证）；定时器唤醒契约见 `test_wp15_install.py`（假 dsh + 假 /healthz 真跑脚本） |
| 5 | 文档七处修订一致性 | ✅ 通过（本次补齐） | 见下节 |

### 三、文档修订核对（本次实际改动）

| 文档 | 状态 | 本次动作 |
|---|---|---|
| `docs/architecture.md` | ✅ | L3 状态由「任务 4/5 待交付」改为**已交付**（题注 + 三层任务模型节）；组件职责表**新增 L3 唤醒入口行**；修正陈旧计数（端点 77→**82**、工具 74→**77**＝72 端点工具+5 维护，取自 `mcp_tools.TOOL_COUNT` / `store_access.endpoints()` 实测） |
| `README.md` | ✅ | 新增「WP15：值班研究员（L3 定时研究任务）」节（怎么开、做什么、没跑成不丢） |
| `docs/RUNBOOK.md` | ✅ | 新增「值班研究员（L3）」节（组件与路径 / systemd 启停 / cron 等价行 / 时刻依据 / 兜底语义 / 7 行排查表 + SQL 入口） |
| `docs/HANDOVER.md` | ✅ | 接手注意新增 L3 运维条目（不自己拉服务、任务只延后不丢、载荷白名单 fail-closed 不要绕过） |
| `docs/P4-live-trading.md` | ✅ | 新增第 12 项：**live 计划会被自动生成并冻结，但 live 执行永远人工**（口令+逐单风控），本清单其余条件一项不减 |
| `docs/TOOL-LIMITS.md` | ✅（WP12/WP13 已登记） | 本次无新数据端点，未改动 |
| `docs/OPENAPI-FEASIBILITY.md` | ✅（WP12 已并入附录 A 摘要） | 本次无改动 |

### 四、安装副本与运行时核对

- **改动前**：`~/.dsh/trading-python/core/trading_core/` 缺 `research_queue.py`、`rule_engine.py`（WP14/WP15 新模块），其余模块齐。
- **执行的命令**（外科式刷新，只解包 Python 数据层，不碰 web profile、不走 pnpm）：
  复用 `scripts/install_plugins.py` 的 `extract_python` + `npm pack`（本地目录，离线）对
  `UNIFIED_PYTHON = (datasource, fin-data, core)` 三个包重解，输出
  `{"refreshed": [["datasource", …], ["fin-data", …], ["core", …]]}`。
  （未跑 `install`：该路径会经 `dsh plugin add` 走 pnpm 改 web profile，超出本次所需。）
- **改动后**：`research_queue.py`/`rule_engine.py`/`autopipeline.py`/`pipeline.py`/`watchlist.py` 全部就位；
  venv `.pth`（`dsh-trading-python.pth`）完好，含 `datasource` 与 `core` 两行。
- **解析机制（实测读码结论）**：
  - **服务进程**：`platform/server/run.py` 启动时把仓库的 `plugins/datasource/python`、
    `plugins/core/python` **插到 sys.path 最前**（WP8 修正，注释写明副本滞后曾导致
    ModuleNotFound/AttributeError）→ 服务永远用仓库代码；
  - **作业子进程**：`daemon._subprocess_runner` 跑 `[sys.executable, "-m", "trading_core", …]`，
    经 venv `.pth` 解析到 `~/.dsh/trading-python/core` **副本** → 副本必须刷新（本节即是）。
- **前端构建产物**：`platform/web/dist/index.html` 与唯一 bundle 构建于 13:36，晚于最新源文件
  （`research.jsx` 13:35）；bundle 内含 WP14「候选池」与 WP10「流程」界面串 → **产物是最新的，无需重建**（WP15 未改前端）。

### 五、遗留与诚实边界

1. **本机未安装定时器单元**：交付物是单元文件 + 脚本 + 运维文档；`systemctl --user enable --now research-duty.timer` 属运维动作，需按实际安装路径（单元内 `%h/dsh-trading-agents` 为示例）执行。
2. **未跑真实 headless 值班**：契约测试用**假 dsh + 假 /healthz** 真跑脚本（验证参数拼装、失败路径、超时、日志），但一次真实的 `dsh --profile headless` 值班会消耗模型额度且需服务在线——留作人工演练（RUNBOOK「值班研究员（L3）」手动演练两行命令即是入口）。
3. **`timeout` 依赖 coreutils**：缺失时脚本不静默失败，会打一行警告并**无上限运行**（systemd 侧 `TimeoutStartSec=2100` 仍兜底）；Windows/Git Bash 场景建议改走会话内补跑路径。
4. **L3 的时效依赖外部条件**（规格 §十一.11）：定时器/机器/会话任一不在，任务**延后不丢**——这是「服务进程不承载 LLM 循环」的代价，已写进 RUNBOOK 与 HANDOVER。
