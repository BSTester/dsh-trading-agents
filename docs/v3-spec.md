<!-- 来源：/home/penn/.dsh/sessions/--home-penn-workspace-dsh-trading-agents--/session-adcad99b-79f9-43df-a6a1-2e3586121b63/session.v3.jsonl.zstd 第 1135 行 user/message（seq=1133），逐字提取，未改写。 -->
<!-- 提取时间：2026-09-20 21:22:19 +0800；同日该会话第 484 行（seq=482）、第 6769 行（seq=6767）另有两份副本，正文逐字符一致（仅前置用户口语不同），全量 session 日志中未出现 V1/V2 或其它标称版本的规格原文，故「标称 V3.0」= 头三条元信息 `**文档版本**：V3.0` / `**编制日期**：2026-09-19` / `**文档状态**：正式发布` 唯一的一份，此处保留 seq=1133 副本。 -->
<!-- 说明：这是需求方原文，只读基线，任何实现口径冲突以本文档为准。 -->

# 量化交易决策平台需求规格说明书与系统详细设计文档

**文档版本**：V3.0
**编制日期**：2026-09-19
**文档状态**：正式发布
**适用系统**：Harness-Centric 量化交易决策平台（独立服务 + Harness 大脑）


# 第一部分 需求规格说明书


## 1. 引言

### 1.1 编写目的

本文档定义一套以 DeepSeek Harness 为决策大脑、以独立微服务为业务载体的量化交易决策平台的完整需求与系统设计。平台与 Harness 之间通过 MCP 协议、SDK JSON-RPC 和 Headless CLI 三种通道通信，实现“会话入口用于人工研究、MCP/API 用于工具暴露、Headless 用于系统自动唤醒大脑”的三模协同架构。

### 1.2 核心设计原则

**平台是身体，Harness 是大脑。** 量化交易平台作为独立服务运行，拥有自己的数据库、风控引擎、订单管理系统和可视化前端。Harness 作为决策大脑，负责推理、分析和决策生成。两者通过标准协议松耦合通信，而非将平台逻辑嵌入 Harness 插件。

Harness 的 headless 模式设计目标就是“运行一个自动化任务，打印最终答案然后退出”。这意味着 Harness 天生可以被外部进程以编程方式调用。平台的调度器可以在任意时刻发起一个 headless 子进程来唤醒大脑，捕获结果后继续执行。

### 1.3 术语定义

| 术语 | 定义 |
|------|------|
| Harness / DSH | DeepSeek Harness，Agent 运行时框架，核心理念“一切皆插件” |
| Headless 模式 | Harness 的一次性任务执行模式，创建全新 Session、执行任务、打印结果后退出 |
| Cordis | Harness 底层插件框架，提供 Fiber 生命周期、Effect 撤销栈、类型化 Event |
| Profile | Harness 的运行形态配置，决定加载哪些 Bundle 及其顺序 |
| Bundle | 可分发的配置单元，提供插件与配置层 |
| MCP | Model Context Protocol，Agent 与工具/数据源的交互协议 |
| SDK JSON-RPC | Harness 的进程外 SDK 协议，通过 stdio 的换行分帧 JSON-RPC 驱动 Agent |
| ACP | Agent Client Protocol，驱动 Harness Agent 的另一种协议 |
| Session | 事件溯源的会话日志，Harness 的唯一真源 |
| Agent Loop | 智能体循环，驱动“观察→推理→行动→反馈”的执行闭环 |
| PIT | Point-in-Time，时点数据，消除回测前视偏差 |


## 2. 系统总体架构

### 2.1 架构总览

平台采用 **“一核三通道”** 架构：平台服务为核心，通过 MCP 桥接通道、SDK JSON-RPC 通道、Headless CLI 通道三条路径与 Harness 通信。

```
┌─────────────────────────────────────────────────────────────────────┐
│                    量化交易平台（独立微服务集群）                        │
│                                                                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │ 调度器    │ │ 策略引擎  │ │ 风控引擎  │ │ OMS      │ │ 可视化    │  │
│  │ Scheduler│ │ Strategy │ │ Risk     │ │ Order    │ │ Frontend │  │
│  └────┬─────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘  │
│       │                                                              │
│  ┌────┴──────────────────────────────────────────────────────────┐  │
│  │              Harness 集成网关（Integration Gateway）            │  │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐              │  │
│  │  │ MCP Bridge │  │ SDK Client │  │ Headless   │              │  │
│  │  │ (工具暴露)  │  │ (会话驱动)  │  │ Runner     │              │  │
│  │  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘              │  │
│  └────────┼───────────────┼───────────────┼──────────────────────┘  │
│           │               │               │                         │
└───────────┼───────────────┼───────────────┼─────────────────────────┘
            │               │               │
     ┌──────┴──────┐ ┌──────┴──────┐ ┌──────┴──────┐
     │  MCP 协议    │ │JSON-RPC/stdio│ │ CLI 子进程   │
     │  (stdio/HTTP)│ │              │ │ (stdout/stderr)│
     └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
            │               │               │
┌───────────┴───────────────┴───────────────┴─────────────────────────┐
│                    DeepSeek Harness 运行时                            │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                     Host 组合层                                │   │
│  │  ┌────────────────────────┐  ┌────────────────────────┐      │   │
│  │  │ dsh-cordis-universal-  │  │ dsh-harness-mcp-server │      │   │
│  │  │ adapter (双向桥接)      │  │ (反向 MCP 暴露)         │      │   │
│  │  └───────────┬────────────┘  └────────────────────────┘      │   │
│  └──────────────┼───────────────────────────────────────────────┘   │
│                 │                                                    │
│  ┌──────────────┴───────────────────────────────────────────────┐   │
│  │                     Core Runtime                              │   │
│  │  Agent Loop / Tool Registry / Session / LLM Seam             │   │
│  │  (base bundle: 78 entries — LLM, Agent, Session, Sandbox,    │   │
│  │   Permission, Tools, Goal, Plan, Compaction, Skill, Subagent) │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 三条通信通道

| 通道 | 方向 | 协议 | 适用场景 | 状态性 |
|------|------|------|----------|--------|
| MCP Bridge | 平台→Harness（工具暴露） | MCP stdio/HTTP | Harness Agent 调用平台工具 | 无状态 |
| SDK JSON-RPC | 平台↔Harness（会话驱动） | 换行分帧 JSON-RPC / stdio | 人工交互式研究分析 | 有状态 |
| Headless CLI | 平台→Harness（自动唤醒） | CLI 子进程 | 系统自动触发分析/决策 | 无状态 |

**MCP Bridge**：平台的量化工具（数据查询、因子计算、风控校验、订单执行等）以 MCP 服务器形式暴露，Harness 的 `dsh-cordis-universal-adapter` 插件通过 `listTools()` 发现并注册为 DSH 原生工具，命名规则为 `mcp:<serverId>:<toolName>`。

**SDK JSON-RPC**：平台前端通过 `dsh-sdk-jsonrpc-server` 驱动 Harness 会话，为每个 sessionId 打开一个会话、把用户提示词排入队列，并把每个会话事件与 Agent 状态转换流式发回客户端。

**Headless CLI**：平台调度器通过 `dsh --profile headless "task"` 发起一次性大脑唤醒，创建全新持久化 Session，提交任务作为用户消息，等待静默后刷新 Session，打印最终答案到 stdout 并退出。退出码 0 表示 Agent 轮次在运行时契约下完成，1 表示未完成或直接运行器失败。


## 3. 功能需求

### 3.1 Harness 集成网关

#### FR-GATEWAY-001：MCP Bridge 插件

系统应部署 `@helibeiqi/dsh-cordis-universal-adapter` 作为 Harness Host 组合层的连接器。该插件在 DSH 的 Host 组合层上，把 MCP 生态（服务器、Agent Plugins 1.0 包）接入 `ctx.tools`，同时把 `ctx.tools` 导出为 MCP 服务器，让同一批工具同时以 DSH 原生与 MCP 两种形态暴露。

**Inbound 方向**：连接平台侧 MCP 服务器（stdio 或 Streamable HTTP），`listTools()` 后把每个 MCP 工具注册为 `ctx.tools` 中的 DSH 工具，命名 `mcp:<serverId>:<toolName>`。加载 Agent Plugins 1.0 包时，解析 `plugin.json` + `skills/*/SKILL.md` + `mcp.json`，把 Skill 描述注入系统提示词扩展点。

**Outbound 方向**：通过 `ctx.tools.schemas()` 枚举已注册工具，注册到 `McpServer`，经 `serveStdio`（或 HTTP）暴露给任意 MCP 客户端，命名 `dsh:<toolName>`。

**生命周期管理**：所有连接由状态机（start / stop / update）管理，`update` 时“断开旧 → listTools → 注销旧 → 注册新”，与 DSH HMR 协同；卸载时经 `ctx.effect` 全部回收。

**版本约束**：依赖 `@deepseek-ai/cordis >=0.1.0`（peerDependency）、`@deepseek-ai/dsh-tools >=0.1.0`（peerDependency）、Node.js >=22.19。

安装方式：
```bash
dsh plugin --profile web add dsh-cordis-universal-adapter
```

#### FR-GATEWAY-002：SDK JSON-RPC 会话客户端

系统应部署 `@deepseek-ai/dsh-sdk-jsonrpc-server` 作为 SDK 服务插件，通过 stdio 服务 SDK 协议格式，使平台能够驱动 Harness Agent：为每个 sessionId 打开一个会话、把用户提示词排入队列，并把每个会话事件与 Agent 状态转换流式发回平台。

**运行时组装**：插件需要 agents 服务，其余能力来自外围插件树。已注册的模型适配器优先用于路由；`deepseek-official` 路由会挂载 DeepSeek 适配器。

**SDK 客户端握手**：`initialize` 是运行时就绪边界，服务器由 Loader 组合挂载时，会等待当前插件树完成所有加载任务后再响应。握手返回协议稳定标识 `deepseek-harness-sdk-runtime`。

**Python SDK 集成**：平台后端可使用 `deepseek-harness-sdk`（Python），通过 `DeepSeekHarness` 类启动运行时，指定 `dsh_home`、`cwd`、`provider`、`model`、`reasoning_effort`、`max_tokens` 参数。

```python
from deepseek_harness import DeepSeekHarness

with DeepSeekHarness(
    dsh_home="/path/to/isolated-dsh-home",
    cwd="/path/to/workspace",
    provider="deepseek-official",
    model="deepseek-v4-flash",
    reasoning_effort="max",
    max_tokens=49152,
) as harness:
    result = harness.run("分析当前持仓风险", session_id="quant-001")
    print(result.final_response)
```

#### FR-GATEWAY-003：Headless Runner

系统应实现 Headless Runner，由平台调度器调用 `dsh --profile headless` 命令发起一次性大脑唤醒。

**命令格式**：
```bash
dsh --profile headless "扫描A股新能源板块，评估隔夜新闻情绪影响，输出调仓建议"
```

**运行机制**：进程挂载 base + headless bundle，创建全新持久化 Agent，提交任务作为用户消息，等待静默后刷新 Session，打印最终文本到 stdout 并退出。

**通道契约**：
- stdout：最后一次非空 assistant 文本，后跟换行符
- stderr：成功时为空；失败时输出终端 Agent 错误码/消息或启动诊断信息
- exit 0：最终持久化轮次/结束原因为 completed
- exit 1：最终原因非 completed，或直接运行器失败
- exit 130：首次 SIGINT 后启动优雅关闭

**关键限制**：退出码 0 证明 Agent 轮次在其运行时契约下完成，但不证明所请求的文件、部署、测试或外部效果存在，需要独立验证。

#### FR-GATEWAY-004：Headless 调度器

平台调度器应实现以下触发策略：

**定时触发**：开盘前扫描（08:30）、午间复盘（12:00）、收盘后分析（16:00）。

**事件触发**：突发新闻、持仓异动、风控阈值突破、因子信号反转。

**流水线节点触发**：调仓前决策确认、策略参数变更审核。

调度器需实现**外部熔断保护**：Headless 进程本身没有内置 token/费用熔断器，平台必须在外部设置超时、并发限制和费用预算。超过预算或超时强制 kill 并记录异常。

```python
import subprocess
import json

def wake_brain(task_prompt: str, timeout: int = 300) -> dict:
    """发起一次 headless 大脑唤醒"""
    result = subprocess.run(
        ["dsh", "--profile", "headless", task_prompt],
        capture_output=True,
        text=True,
        timeout=timeout
    )
    return {
        "success": result.returncode == 0,
        "answer": result.stdout,
        "diagnostics": result.stderr,
        "exit_code": result.returncode
    }
```

#### FR-GATEWAY-005：Profile 配置

平台应创建专属 Profile，定义量化分析师的运行时配置。Headless 模式使用 `dsh-base` + `dsh-headless` 组合，其中 `dsh-base` 的 78 个 entry 覆盖 LLM、Agent、Session、持久化、Sandbox、权限、工具、Goal、Plan、Compaction、Skill、Subagent、Workflow 等核心能力。

Profile 目录包含 `package.json`（记录树外插件依赖和 `dsh.profile` manifest）以及 `cordis.patch.yml`（用户 patch 层）。

```yaml
# $DSH_HOME/profiles/quant-headless/package.json
{
  "dsh.profile": {
    "bundles": [
      "@deepseek-ai/dsh-base",
      "@deepseek-ai/dsh-headless",
      "dsh-cordis-universal-adapter",
      "dsh-quant-data-mcp"
    ]
  }
}
```


### 3.2 平台量化工具域（MCP 服务器）

#### FR-TOOLS-001：六大工具域

平台应实现一个 MCP 服务器，暴露以下六大域的量化工具。参考 dsh-quant 的 59 工具·6 域架构：data / alpha / ML / risk / execution / ecosystem。

| 工具域 | 代表工具 | MCP 方法名 | 职责 |
|--------|----------|-----------|------|
| 数据域 | query_quote | mcp:quant_data:query_quote | 行情快照、K线、盘口 |
| 数据域 | query_financial | mcp:quant_data:query_financial | 财务三表、指标 |
| 数据域 | search_news | mcp:quant_data:search_news | 新闻、公告、研报 |
| 因子域 | calc_indicator | mcp:quant_alpha:calc_indicator | 技术指标计算 |
| 因子域 | eval_factor_ic | mcp:quant_alpha:eval_factor_ic | IC/IR分析、分层回测 |
| 回测域 | run_backtest | mcp:quant_ml:run_backtest | 策略回测引擎 |
| 回测域 | param_sweep | mcp:quant_ml:param_sweep | 参数扫描 |
| 风控域 | check_risk | mcp:quant_risk:check_risk | 事前风控校验 |
| 风控域 | calc_var | mcp:quant_risk:calc_var | VaR/CVaR计算 |
| 执行域 | place_order | mcp:quant_exec:place_order | 下单执行 |
| 执行域 | query_position | mcp:quant_exec:query_position | 持仓查询 |
| 治理域 | request_approval | mcp:quant_gov:request_approval | 审批请求 |
| 治理域 | log_decision | mcp:quant_gov:log_decision | 决策日志写入 |

#### FR-TOOLS-002：工具注册规范

平台 MCP 服务器的工具定义应遵循 dsh-quant 的 AI-native 设计原则：

- **工具 schema 注入系统提示词**：每个契约（参数/输出/对齐规则）从模型视角编写
- **等长 null 对齐**：输出与输入等长，头部窗口位置为 `null`，模型按索引对齐
- **规范 JSON + render 分离**：机器读结构，人类读散文
- **全部 isConcurrencySafe**：纯函数、无共享状态，Agent 可并行调用所有工具
- **Skill 层**：`skill/quant-research` 让模型自行加载工作流

#### FR-TOOLS-003：工具粒度控制

平台应向 Harness 暴露的 MCP 工具数量应控制在合理范围。参考 `dsh-quant-data-mcp` 的做法：提供 6 个 A 股数据工具（日线、快照、财务、北向资金等），支持东方财富与腾讯数据源自动回退，所有数据源使用公开免密钥端点。

平台应实现**工具发现代理**：MCP 服务器暴露一个 `list_tools` 入口和一个 `call_tool` 入口，让 Harness Agent 通过间接调用发现具体能力，避免上百个工具 schema 撑爆上下文窗口。


### 3.3 数据层需求

#### FR-DATA-001：富途 OpenAPI 接入

系统应通过富途 OpenAPI 获取行情数据和交易执行能力。OpenD 部署在云端服务器，以自定义 TCP 协议对外暴露接口。系统获取：实时报价、K线数据、盘口深度、逐笔成交、批量市场快照、板块行情。交易执行支持市价单、限价单、条件单、改单/撤单、订单查询、持仓查询。

#### FR-DATA-002：开源数据源补充

| 数据源 | 接入方式 | 覆盖范围 | MCP 命名空间 |
|--------|----------|----------|-------------|
| Tushare Pro | Python SDK + MCP | A股财务、宏观 | mcp:tushare:* |
| AKShare | Python 库 + MCP | 新闻、另类数据 | mcp:akshare:* |
| OpenBB | Python SDK + MCP | 美股基本面 | mcp:openbb:* |
| SEC EDGAR | XBRL API | 美股财报三表 | mcp:sec:* |
| dsh-quant-data-mcp | MCP stdio | A股日线、快照、财务、北向资金 | mcp:quant_data:* |

`dsh-quant-data-mcp` 是零依赖 MCP stdio server 通用模板 + A 股数据开箱示例，只用 Node 内置模块，无需 API key，所有数据源都是公开免密钥端点（东方财富/腾讯）。

#### FR-DATA-003：PIT 数据访问

系统应确保所有历史数据查询遵循 Point-in-Time 原则。平台侧的 `data/cache.py` 作为唯一数据读取接口，确保所有历史数据查询遵循 PIT 原则，防止前视偏差。

#### FR-DATA-004：数据源健康检查

系统应实现数据源健康检查机制。当主数据源不可用时自动降级至备用数据源。参考 `dsh-quant-data-mcp` 的东方财富与腾讯数据源自动回退机制。


### 3.4 策略层需求

#### FR-STRAT-001：自动选股引擎

系统应支持多因子选股框架，涵盖价值、成长、动量、质量、情绪、另类六类因子。参考 dsh-quant 的 PDAT→PAAT→PCPT→PRT→PET 五阶段研究流水线。

#### FR-STRAT-002：策略生成与优化

支持多因子选股策略、机器学习策略（Lasso/LightGBM/MLP）、事件驱动策略、统计套利策略。参数优化支持数千次完整回测和热力图可视化。

#### FR-STRAT-003：NLP 情绪分析引擎

集成实时情绪评分、事件识别、多源情绪融合、情绪因子构建。


### 3.5 执行层需求

#### FR-EXEC-001：OMS（订单管理系统）

基于富途 OpenAPI 构建 OMS，负责将策略信号转化为交易订单并管理全生命周期。

#### FR-EXEC-002：分级审批机制

- **自动执行**：预设风控阈值内的订单，平台直接调用富途交易接口
- **人工确认**：超过阈值的订单，通过 Web/IM 通道请求人工审批
- **强制阻断**：触发风控红线的订单，直接拒绝并记录

#### FR-EXEC-003：风控体系

事前风控：订单合规校验（持仓限制、单笔上限、行业集中度）、资金检查。事中风控：实时监控策略回撤、杠杆率、流动性风险。事后风控：绩效归因分析、最大回撤统计、VaR 计算。参考 dsh-quant 的 risk 域提供 VaR/CVaR/Beta/Alpha/IR + Kupiec 检验。


### 3.6 监控与可视化层需求

#### FR-MON-001：决策逻辑追溯

每笔交易记录完整的决策链路：信号溯源（因子值、模型输出、情绪评分）、决策快照（市场状态和参数配置）、执行链路（订单全生命周期事件）。

#### FR-MON-002：可视化仪表板

系统概览（总资产、收益、夏普比率）、行情图表（TradingView K线图叠加策略信号）、策略分析（因子敏感性热力图）、决策面板（实时展示 AI 分析过程和决策逻辑）、风险监控、交易记录、Harness 会话状态（Agent Loop 运行状态、工具调用统计）。

#### FR-MON-003：Headless 调用日志

平台应记录每次 headless 调用的完整信息：任务提示词、stdout 答案、stderr 诊断信息、退出码、执行耗时、token 消耗估算。这些日志写入平台数据库，不依赖 Harness 的 Session 存储。


## 4. 非功能需求

### 4.1 性能需求

| 指标 | 要求 |
|------|------|
| 行情数据延迟 | < 100ms（富途 Level 2 行情） |
| 订单执行延迟 | < 500ms（信号生成到订单提交） |
| MCP 工具调用延迟 | < 2s（单次工具执行） |
| Headless 调用延迟 | < 30s（单次分析任务，超时可配） |
| SDK 会话首次握手 | < 30s（initialize_timeout_seconds 默认值） |
| 系统可用性 | 99.9% |

### 4.2 安全需求

富途 OpenAPI 的登录密码和交易解锁密码加密存储。API Token 通过环境变量或密钥管理服务注入。所有交易操作记录审计日志。支持多级权限控制。

**Harness 安全**：`dsh-harness-mcp-server` 默认绑定到 127.0.0.1 仅限本机访问，暴露未认证的本地端点。生产环境需增加认证层。

### 4.3 版本兼容性需求

`@deepseek-ai/dsh-tools` 与 MCP 均处于 Developer Preview 阶段，接口可能发生破坏性变更。平台与 Harness 的集成层需做好版本适配和降级预案，锁定已验证的 API 形态。

### 4.4 可扩展性需求

数据源适配器采用插件化设计。策略引擎支持热加载。执行算法可插拔。Harness 的“一切皆插件”原则确保模型、工具、技能、会话、沙箱、存储、循环、调度、UI 均可自由替换和重组。


# 第二部分 系统详细设计


## 5. 平台服务详细设计

### 5.1 服务拆分与部署

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Kubernetes 集群                                  │
│                                                                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │ scheduler-svc    │  │ strategy-svc     │  │ risk-svc         │  │
│  │ (调度器)          │  │ (策略引擎)        │  │ (风控引擎)        │  │
│  │                  │  │                  │  │                  │  │
│  │ • Headless触发   │  │ • 因子计算       │  │ • 事前/事中/事后  │  │
│  │ • 事件监听       │  │ • 信号生成       │  │ • VaR/CVaR       │  │
│  │ • 熔断保护       │  │ • 组合优化       │  │ • 规则热更新     │  │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘  │
│           │                     │                     │            │
│  ┌────────┴─────────────────────┴─────────────────────┴─────────┐  │
│  │                    Kafka 消息总线                              │  │
│  └────────┬─────────────────────┬─────────────────────┬─────────┘  │
│           │                     │                     │            │
│  ┌────────┴─────────┐  ┌───────┴─────────┐  ┌───────┴─────────┐   │
│  │ data-svc         │  │ exec-svc        │  │ gateway-svc     │   │
│  │ (数据管道)        │  │ (OMS)           │  │ (Harness网关)    │   │
│  │                  │  │                  │  │                  │   │
│  │ • 多源采集       │  │ • 订单管理       │  │ • MCP Server    │   │
│  │ • 清洗标准化     │  │ • 富途对接       │  │ • SDK Client    │   │
│  │ • PIT存储        │  │ • 持仓同步       │  │ • HeadlessRunner│   │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘   │
│                                                                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │ web-svc          │  │ Redis Cluster    │  │ PostgreSQL       │  │
│  │ (前端)           │  │ (实时缓存)        │  │ (元数据/决策日志) │  │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.2 Harness 集成网关详细设计

Gateway 服务是平台与 Harness 之间的唯一接口。它包含三个子模块：

#### 5.2.1 MCP Bridge 子模块

MCP Bridge 子模块运行一个 MCP 服务器，暴露平台的量化工具。Harness 侧的 `dsh-cordis-universal-adapter` 插件通过 Inbound 方向连接到此服务器，将每个工具注册为 DSH 原生工具。

**MCP 服务器实现**：参考 `dsh-quant-data-mcp` 的零依赖架构，严格按 MCP 官方的 NDJSON 帧格式实现。

```javascript
// lib/quant-mcp-server.mjs 核心结构
const TOOLS = [
  { name: 'query_quote', description: '查询行情快照', inputSchema: {...} },
  { name: 'query_financial', description: '查询财务数据', inputSchema: {...} },
  { name: 'calc_indicator', description: '计算技术指标', inputSchema: {...} },
  { name: 'run_backtest', description: '运行策略回测', inputSchema: {...} },
  { name: 'check_risk', description: '风控校验', inputSchema: {...} },
  { name: 'place_order', description: '提交订单', inputSchema: {...} },
  // ... 更多工具
]

async function handleCall(toolName, args) {
  switch (toolName) {
    case 'query_quote': return await dataService.queryQuote(args)
    case 'query_financial': return await dataService.queryFinancial(args)
    case 'calc_indicator': return await alphaService.calcIndicator(args)
    case 'run_backtest': return await backtestService.run(args)
    case 'check_risk': return await riskService.check(args)
    case 'place_order': return await execService.placeOrder(args)
  }
}
```

**cordis.patch.yml 注册**（全环境变量化）：

```yaml
# 平台侧 MCP 服务器注册到 Harness
- id: quant-mcp-server
  name: dsh-cordis-universal-adapter
  config:
    servers:
      - id: quant_data
        transport: stdio
        command: !!js process.env.QUANT_MCP_NODE
        args: [!!js process.env.QUANT_MCP_SERVER]
        cwd: !!js process.env.QUANT_MCP_CWD
      - id: quant_tushare
        transport: stdio
        command: !!js process.env.TUSHARE_MCP_NODE
        args: [!!js process.env.TUSHARE_MCP_SERVER]
```

#### 5.2.2 SDK Client 子模块

SDK Client 子模块使用 `deepseek-harness-sdk`（Python）驱动 Harness 会话，用于人工交互式研究。

**启动运行时**：SDK 启动捆绑的 `dsh` CLI，使用 `--profile sdk`；选中的 profile 拥有 JSON-RPC 服务器、Agent 组合、凭证、持久化、工具和关闭行为。每次启动都需要显式的 Harness home。

```python
from deepseek_harness import DeepSeekHarness

# 平台后端会话管理
class HarnessSessionManager:
    def __init__(self, dsh_home: str):
        self.dsh_home = dsh_home
        self.sessions = {}

    def create_session(self, session_id: str, workspace: str):
        harness = DeepSeekHarness(
            dsh_home=self.dsh_home,
            cwd=workspace,
            provider="deepseek-official",
            model="deepseek-v4-flash",
            reasoning_effort="max",
            max_tokens=49152,
        )
        self.sessions[session_id] = harness
        return harness

    def run_task(self, session_id: str, prompt: str):
        harness = self.sessions[session_id]
        result = harness.run(prompt, session_id=session_id)
        # 结果写入平台决策日志
        self._log_decision(session_id, prompt, result)
        return result
```

**自定义插件**：持久化定制属于 dsh profile。初始化随附的 SDK profile 并安装外部 bundle：

```bash
export DSH_HOME=/path/to/isolated-dsh-home
dsh --profile sdk --dump-default-config >/dev/null
dsh plugin --profile sdk add file:/path/to/my-plugin-bundle
```

#### 5.2.3 Headless Runner 子模块

Headless Runner 子模块负责构造和发起 `dsh --profile headless` 调用。

**任务提示词构造**：由于 headless 每次启动全新 Agent，不继承之前的会话历史，平台需要把决策所需的全部上下文打包进任务提示词。

```python
class HeadlessRunner:
    def __init__(self, profile: str = "headless", timeout: int = 300):
        self.profile = profile
        self.timeout = timeout

    def build_prompt(self, task_type: str, context: dict) -> str:
        """构造包含完整上下文的任务提示词"""
        templates = {
            "pre_market_scan": (
                "扫描 {universe} 板块，评估隔夜新闻情绪影响。\n"
                "当前持仓：{positions}\n"
                "风控阈值：{risk_limits}\n"
                "输出调仓建议，包含因子依据。"
            ),
            "breaking_news": (
                "评估以下新闻对当前持仓的影响：\n{news_text}\n"
                "当前持仓：{positions}\n"
                "输出风险等级和应对建议。"
            ),
            "risk_review": (
                "复盘当前组合的风险暴露。\n"
                "持仓详情：{positions}\n"
                "近期市场数据：{market_summary}\n"
                "输出风险归因和改进建议。"
            ),
        }
        return templates[task_type].format(**context)

    def execute(self, prompt: str) -> dict:
        """执行 headless 调用"""
        result = subprocess.run(
            ["dsh", "--profile", self.profile, prompt],
            capture_output=True, text=True, timeout=self.timeout
        )
        return {
            "success": result.returncode == 0,
            "answer": result.stdout,
            "diagnostics": result.stderr,
            "exit_code": result.returncode,
        }
```

**并发控制**：Headless 调用需要设置并发上限（默认 3 个并行），超过上限的请求排队。每个调用设置独立的超时（默认 300s）和 token 预算（默认 200K）。


## 6. Harness 侧配置详细设计

### 6.1 Profile 组装

Harness 的运行实例由 Profile、Bundle 和 Patch 逐层合成。配置树以空根为起点，依次叠加：`dsh.profile.bundles` 中各组合包的 patch → profile 自身的 `cordis.patch.yml` → home 级的 `$DSH_HOME/cordis.patch.yml` → `--patch` 指定的覆盖层。

**Headless Profile 组装**：

```
空配置树
  ↓
dsh-base (78 entries: LLM, Agent, Session, Persistence, Projection,
          Tools, Commands, Sandbox, Permission, Goal, Plan, Compaction,
          Skill, Subagent, Workflow, Settings, Credentials, Telemetry)
  ↓
dsh-headless (code-runtime, headless-startup, headless-runner)
  ↓
dsh-cordis-universal-adapter (MCP 双向桥接)
  ↓
dsh-quant-data-mcp (A股数据)
  ↓
Headless Runtime
```

Headless 模式不会加入 Host、HTTP Server 和浏览器插件等 Web 模式需要的插件，只覆盖少量运行参数。

### 6.2 量化专属 Profile

```yaml
# $DSH_HOME/profiles/quant-headless/cordis.patch.yml
- id: quant-system-prompt
  name: '@deepseek-ai/dsh-system-prompt'
  config:
    sections:
      - title: '角色定义'
        content: |
          你是一名量化交易分析师，为自动化交易系统提供决策建议。
          决策必须基于数据，不得凭直觉预测个股。
          所有交易信号必须有因子依据。
          输出必须结构化，包含：决策、依据、风险等级、建议操作。
      - title: '风控规则'
        content: |
          单笔交易不超过总资产的 2%。
          单一行业暴露不超过 20%。
          最大回撤阈值 15%，触发后必须减仓。

- id: quant-mcp-bridge
  name: dsh-cordis-universal-adapter
  config:
    servers:
      - id: quant_data
        transport: stdio
        command: !!js process.env.QUANT_MCP_NODE
        args: [!!js process.env.QUANT_MCP_SERVER]

- id: quant-headless-runner
  name: '@deepseek-ai/dsh-headless'
  config:
    taskTimeout: 300000
    maxSteps: 50
```

### 6.3 SDK Profile

```yaml
# $DSH_HOME/profiles/sdk/cordis.patch.yml
- id: sdk-jsonrpc-server
  name: '@deepseek-ai/dsh-sdk-jsonrpc-server'
  config:
    maxTokensAsSuccess: false

- id: quant-mcp-bridge
  name: dsh-cordis-universal-adapter
  config:
    servers:
      - id: quant_data
        transport: stdio
        command: !!js process.env.QUANT_MCP_NODE
        args: [!!js process.env.QUANT_MCP_SERVER]
```


## 7. 数据流详细设计

### 7.1 人工研究数据流（SDK 通道）

```
用户在前端输入分析指令
       │
       ▼
gateway-svc 接收指令，创建 session_id
       │
       ▼
SDK Client 通过 JSON-RPC 发送 session/prompt
       │
       ▼
Harness Agent Loop 启动
  ├─ 调用 mcp:quant_data:query_quote 获取行情
  ├─ 调用 mcp:quant_data:query_financial 获取财务
  ├─ 调用 mcp:quant_alpha:calc_indicator 计算指标
  ├─ 调用 mcp:quant_ml:run_backtest 运行回测
  └─ 生成分析结论
       │
       ▼
Harness 将每个事件流式发回 SDK Client
       │
       ▼
gateway-svc 将结果转发到前端渲染
       │
       ▼
决策结果写入平台数据库
```

### 7.2 自动触发数据流（Headless 通道）

```
调度器触发（定时/事件/流水线节点）
       │
       ▼
HeadlessRunner.build_prompt() 构造任务提示词
  （打包持仓快照、市场数据摘要、风控阈值、策略参数）
       │
       ▼
subprocess.run(["dsh", "--profile", "headless", prompt])
       │
       ▼
Harness Headless 运行时启动
  ├─ 挂载 base + headless bundle
  ├─ 创建全新持久化 Agent
  ├─ 提交任务作为用户消息
  ├─ Agent 调用平台 MCP 工具获取数据
  ├─ 生成决策建议
  ├─ 等待静默，刷新 Session
  └─ 打印最终文本到 stdout，退出
       │
       ▼
HeadlessRunner 捕获 stdout/stderr/exit_code
       │
       ▼
gateway-svc 解析结果，写入平台决策日志
       │
       ▼
根据决策结果触发后续流水线
  ├─ 通过 → exec-svc 生成订单
  ├─ 人工确认 → 推送审批请求
  └─ 阻断 → 记录并告警
```

### 7.3 MCP 工具调用数据流

```
Harness Agent Loop 决定调用工具
       │
       ▼
Agent 输出工具调用请求（JSON Schema 格式）
       │
       ▼
dsh-cordis-universal-adapter 拦截请求
       │
       ▼
McpClientBridge 将 DSH 工具调用转发到平台 MCP 服务器
       │
       ▼
平台 MCP 服务器执行具体工具逻辑
  ├─ 数据域 → 调用富途OpenAPI / Tushare / AKShare
  ├─ 因子域 → 调用因子计算引擎
  ├─ 风控域 → 调用风控引擎
  └─ 执行域 → 调用富途交易接口
       │
       ▼
返回结构化 JSON 结果
       │
       ▼
McpClientBridge 将结果注册为 DSH 工具输出
       │
       ▼
Agent 接收结果，继续推理
```


## 8. 部署与运维设计

### 8.1 部署架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Kubernetes 集群                                  │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Node 1: gateway-svc + scheduler-svc                          │  │
│  │  ┌──────────────────────────────────────────────────────┐    │  │
│  │  │ Harness 运行时（独立进程）                             │    │  │
│  │  │ $DSH_HOME/profiles/quant-headless/                    │    │  │
│  │  │ $DSH_HOME/profiles/sdk/                               │    │  │
│  │  └──────────────────────────────────────────────────────┘    │  │
│  │  ┌──────────────────────────────────────────────────────┐    │  │
│  │  │ MCP Server 进程（平台工具暴露）                        │    │  │
│  │  │ lib/quant-mcp-server.mjs                              │    │  │
│  │  └──────────────────────────────────────────────────────┘    │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────┐  ┌────────────────────┐  ┌──────────────┐  │
│  │ data-svc           │  │ strategy-svc       │  │ risk-svc     │  │
│  │ + 富途OpenD        │  │ + 因子引擎         │  │ + 风控规则   │  │
│  │ + Tushare MCP      │  │ + 回测引擎         │  │ + VaR引擎    │  │
│  └────────────────────┘  └────────────────────┘  └──────────────┘  │
│                                                                      │
│  ┌────────────────────┐  ┌────────────────────┐  ┌──────────────┐  │
│  │ exec-svc           │  │ web-svc            │  │ 存储层       │  │
│  │ + OMS              │  │ + React前端        │  │ Redis/PG/S3  │  │
│  │ + 富途交易API      │  │ + TradingView      │  │              │  │
│  └────────────────────┘  └────────────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### 8.2 环境变量配置

| 变量 | 用途 | 必填 |
|------|------|------|
| DSH_HOME | Harness home 目录 | 是 |
| DEEPSEEK_API_KEY | DeepSeek API 密钥 | 是 |
| QUANT_MCP_NODE | MCP 服务器 Node 可执行文件路径 | 是 |
| QUANT_MCP_SERVER | MCP 服务器入口文件路径 | 是 |
| QUANT_MCP_CWD | MCP 服务器工作目录 | 是 |
| QUANT_MCP_LOG | MCP 服务器日志路径 | 否 |
| FUTU_OPEND_HOST | 富途 OpenD 地址 | 是 |
| FUTU_OPEND_PORT | 富途 OpenD 端口 | 是 |
| TUSHARE_TOKEN | Tushare Pro Token | 否 |

### 8.3 监控告警

Prometheus + Grafana 监控以下指标：

| 指标类别 | 具体指标 | 告警阈值 |
|----------|----------|----------|
| Harness 运行 | Headless 调用成功率 | < 95% |
| Harness 运行 | Headless 调用平均耗时 | > 60s |
| Harness 运行 | SDK 会话活跃数 | > 10 |
| 工具调用 | MCP 工具调用延迟 | > 5s |
| 工具调用 | MCP 工具调用失败率 | > 5% |
| 数据源 | 数据源连接状态 | 断连 |
| 数据源 | 数据延迟 | > 5min |
| 交易 | 订单执行延迟 | > 1s |
| 交易 | 风控阻断次数 | 突增 |


## 9. 实施计划

### 9.1 分阶段路线

| 阶段 | 周期 | 交付物 | 验收标准 |
|------|------|--------|----------|
| 第一阶段 | 2-3周 | MCP Bridge 最小验证 | Harness Web UI 可调用平台 3 个工具 |
| 第二阶段 | 2-3周 | Headless Profile 配置 | 命令行 `dsh --profile headless` 正常输出 |
| 第三阶段 | 3-4周 | 平台调度器集成 | 定时/事件触发 headless 调用，结果写入数据库 |
| 第四阶段 | 4-6周 | 全量工具域 + SDK 通道 | 六大域工具注册，前端会话入口可用 |
| 第五阶段 | 持续迭代 | 全流程自动化 | 自适应节奏控制、绩效归因反馈 |

### 9.2 第一阶段详细任务

1. 在平台侧实现 MCP 服务器，暴露 `query_quote`、`query_financial`、`submit_analysis_result` 三个工具
2. 安装 `dsh-cordis-universal-adapter`：
   ```bash
   dsh plugin --profile web add dsh-cordis-universal-adapter
   ```
3. 配置 `cordis.patch.yml` 连接平台 MCP 服务器
4. 在 Harness Web UI 中手工测试 Agent 能否调用这些工具
5. 验证工具调用的参数校验、超时处理、错误返回

### 9.3 第三阶段详细任务

1. 配置 `quant-headless` profile，定义量化分析师 system prompt 和工具白名单
2. 命令行手工验证：`dsh --profile headless "分析某标的"`
3. 在平台调度器中集成 HeadlessRunner
4. 实现定时触发（每日 08:30 开盘扫描）
5. 实现事件触发（突发新闻、持仓异动）
6. 将 headless 返回的结构化结果写入平台数据库
7. 实现外部熔断保护（超时、并发限制、token 预算）


## 10. 关键设计决策

**决策 1：三条通道分工明确。** SDK JSON-RPC 用于有状态的人工交互，MCP Bridge 用于工具暴露，Headless CLI 用于无状态的自动触发。三者互不干扰，各自独立管理生命周期。

**决策 2：Headless 调用不依赖会话状态。** 每次 headless 调用创建全新 Agent 和 Session，结果回写到平台数据库。这样平台的自动化流水线不绑定于 Harness 的会话存储格式。

**决策 3：工具粒度控制。** 平台向 Harness 暴露的 MCP 工具控制在合理数量，通过工具发现代理避免上下文窗口溢出。

**决策 4：外部熔断保护。** Headless 进程本身没有内置 token/费用熔断器，平台在外部设置超时、并发限制和费用预算。

**决策 5：版本锁定与降级预案。** `@deepseek-ai/dsh-tools` 和 MCP 均处于 Developer Preview 阶段，集成层锁定已验证的 API 形态，做好版本适配和降级预案。


## 附录 A：MCP 工具注册代码示例

```typescript
// 平台 MCP 服务器工具定义（参考 dsh-quant 的 AI-native 设计）
import { defineTool } from '@deepseek-ai/dsh-tools'

ctx.tools.register(defineTool({
  name: 'query_financial',
  description: 'Query financial statements for a given stock.',
  parameters: {
    symbol: { type: 'string', required: true, description: 'Stock code e.g. 600519.SH' },
    statement: { type: 'string', required: true,
      description: 'income | balance | cashflow' },
    period: { type: 'string', description: 'Report period e.g. 2025Q3' },
  },
  output: {
    schema: { type: 'object' },
    render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
  },
  async execute(args, exec) {
    return await mcpClient.call('quant_data', 'query_financial', {
      ts_code: args.symbol,
      statement: args.statement,
      period: args.period,
    })
  },
}))
```

## 附录 B：Headless 调用完整示例

```python
# 平台调度器中的 Headless 调用
from datetime import datetime

class PreMarketScanner:
    def __init__(self, runner: HeadlessRunner, db):
        self.runner = runner
        self.db = db

    def scan(self, universe: str = "A股新能源"):
        # 1. 构造上下文
        context = {
            "universe": universe,
            "positions": self.db.get_current_positions(),
            "risk_limits": self.db.get_risk_limits(),
        }

        # 2. 构造提示词
        prompt = self.runner.build_prompt("pre_market_scan", context)

        # 3. 执行 headless 调用
        result = self.runner.execute(prompt)

        # 4. 记录结果
        self.db.log_headless_call({
            "task_type": "pre_market_scan",
            "prompt": prompt,
            "answer": result["answer"],
            "diagnostics": result["diagnostics"],
            "exit_code": result["exit_code"],
            "success": result["success"],
            "timestamp": datetime.now().isoformat(),
        })

        # 5. 根据结果触发后续流水线
        if result["success"]:
            decision = self.parse_decision(result["answer"])
            if decision["action"] == "rebalance":
                self.db.enqueue_rebalance(decision)
        else:
            self.alert(f"Headless 调用失败: exit_code={result['exit_code']}")

    def parse_decision(self, answer: str) -> dict:
        """解析 Harness 返回的结构化决策"""
        # 平台侧解析逻辑
        ...
```

## 附录 C：数据源能力对照

| 数据类型 | 富途 OpenAPI | Tushare Pro | AKShare | dsh-quant-data-mcp |
|----------|-------------|-------------|---------|-------------------|
| 实时行情 | ✅ | ⚠️ | ⚠️ | ❌ |
| 历史K线 | ✅ | ✅ | ✅ | ✅ (A股日线) |
| 财务三表 | ⚠️ | ✅ | ⚠️ | ✅ (财务) |
| 公司公告 | ✅ | ✅ | ✅ | ❌ |
| 新闻资讯 | ✅ | ⚠️ | ✅ | ❌ |
| 社交媒体情绪 | ✅ | ❌ | ⚠️ | ❌ |
| 北向资金 | ❌ | ✅ | ✅ | ✅ |
| 宏观经济 | ❌ | ✅ | ✅ | ❌ |
| PIT 财务数据 | ❌ | ✅ | ❌ | ❌ |
