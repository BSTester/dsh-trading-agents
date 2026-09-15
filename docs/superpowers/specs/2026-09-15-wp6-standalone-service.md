# WP6 独立服务化规格（MCP 工具面 / Ant Design Pro 迁移 / 审批回归）

> 状态：按 `docs/superpowers/plans/2026-09-14-platform-plan-index.md` §WP6 立项（2026-09-15 用户决策）产出；
> 前端形态经用户确认（2026-09-15）：**Vite + antd5 + @ant-design/pro-components**。
> 全局约定（UI 文案规范 2.1 / 依赖锁定协议 2.2 / 工程约定 2.3）见 `2026-09-14-platform-plan-index.md`，本规格同等受其约束。
> 实现计划：`docs/superpowers/plans/2026-09-15-wp6-standalone-service.md`（本规格 §八 验收标准为其验收门）。

## 一、目标与非目标

### 1.1 目标（立项三条的落地口径）

| # | 立项原文 | 本规格落地口径 |
|---|---|---|
| 1 | 工作台一切能力经 MCP 暴露给 Harness（与面板完全对等，非子集） | 6a：独立 Node 服务进程，MCP 工具面 = **25 个工具**（§3：20 端点对等 + 5 维护动作），工具实现直接复用面板同一个 `createRpcHandler` 实例——对等性由代码结构保证，不靠人工对照 |
| 2 | 工作台 UI 用 Ant Design Pro 重实现（脱离 Harness 面板宿主，独立 Web） | 6b：同一服务进程托管 HTTP API + 静态前端（11 页签全量重实现），浏览器不再依赖 Harness Connection |
| 3 | Harness 定位不变：入口 = 决策与操作确认；固定信息收集由服务定时跑 | 6c：preset 行替换 + 审批回归（§五）。固定信息收集 = 既有 daemon 作业链（WP4 已交付），服务自身不做新调度；Harness 会话内逐笔确认、原生审批、口令门槛一条不少 |

### 1.2 非目标（明确不做）

1. **不做第二个聊天入口**：独立 Web 无聊天、无逐单下单/撤单表单、无 shell/LLM/token 接口（边界与 `architecture.md` 一致，逐单表单仍然不做）。
2. **不迁移执行核心**：trading_core（Python）、daemon、指令目录协议原样不动；WP6 只新增「服务进程」这层皮。
3. **不改变 live 准入**：实盘准入永远按 `docs/P4-live-trading.md` 人工评估，WP6 不自动升级任何实盘能力。
4. **不删除 Harness 内工作台 Host**：`tradingWorkbench` 服务是 engine 账户策略（`policy.js`）的锚，必须留在 Harness 进程内（§5.4 说明）。
5. **不做多用户/鉴权体系**：单操作者单机，仅 loopback + 可选静态 token（§4.7）。

## 二、总体架构（WP6 后拓扑）

```text
┌─ Harness 进程（不动） ─────────────────────────────┐
│ engine 工具（8 个对话工具） + policy.js 三层审批     │
│ workbench Host 插件（tradingWorkbench 服务锚）       │
│   └ Connection RPC 20 端点（legacy 面板过渡期保留）  │
└──────────────┬────────────────────────────────────┘
               │ 同一份数据文件（无网络）
┌─ 独立服务进程（WP6 新增，node platform/server/start.mjs）─┐
│ createRpcHandler（与 Host 同一实现，同一 TTL 缓存）        │
│   ├ MCP streamable-http  http://127.0.0.1:8397/mcp        │──► Harness 新增 mcp-client 行
│   ├ HTTP API             POST /api/wb/<endpoint>           │──► Ant Design Pro 前端
│   ├ 静态托管             GET /  → platform/web/dist        │   （独立浏览器页）
│   └ 维护动作             WorkbenchStore（admin_* 工具）     │
└──────────────┬────────────────────────────────────────┘
               │ pycore 子进程 / 指令目录 / SQLite 只读快照
┌─ trading_core + daemon（不动）─────────────────────────┐
│ 16 个 CLI 子命令、5 种白名单指令、心跳/kill/告警文件      │
└────────────────────────────────────────────────────────┘
```

关键决策：**MCP 服务端用 Node 实现**（`@modelcontextprotocol/sdk`），因为面板的全部业务逻辑（store、analytics provider、series provider、corebridge、commandbus、TTL 缓存、字段白名单校验）都是 Node 模块——复用它们即天然对等；Python 侧经既有 `pycore` 子进程桥取数，无新增通道。trading-venv 不装 Python MCP SDK（2026-09-15 探测：venv 无 `mcp` 包，亦无需安装）。

## 三、Part A：MCP 工具面清单（= 工作台全功能盘点）

### 3.1 盘点方法与对等性定义

- 盘点基准（2026-09-15，代码为证）：`plugins/workbench/src/endpoints.js` 20 端点、`client.js` 11 页签、`scripts/workbench_admin.mjs` 5 维护动作、`store.js` snapshot/switchMode。
- **对等**定义为：面板能做的每一件事，在 MCP 面上存在一个工具，且二者走**同一个 handler 函数**（`createRpcHandler` 返回的 `(endpoint, payload) => {ok, value|error}`）。工具面新增能力 = 维护动作 5 项（原仅 shell 脚本可达）。
- 工具面**白名单之外无任何工具**：无任意执行、无 shell、无文件读写、无 LLM、无 token 读取。tools/list 快照测试锁定总数与名单（§5.2 T6）。

### 3.2 端点 → 工具映射总表（20 个）

服务名 `quantwb`；Harness 内呈现为 `mcp__quantwb__<tool>`。`refresh?: boolean` 映射为载荷 `_refresh`（显式旁路 TTL 缓存）；输出为 handler 原样 envelope `{ok, value?, cached?, cached_at?, error?{code,message}}`。TTL 沿用 `CACHE_TTL_MS`（同一缓存实例，分钟级不重复取数）。

| # | 工具名 | 输入（* 必填） | 输出最小字段 | 错误码 | TTL |
|---|---|---|---|---|---|
| 1 | `snapshot` | — | mode, generated_at, runs, reports, previews, activity, trade_summary, broker, in_flight, pending_observations, endpoints | — | 0 |
| 2 | `switch_mode` ⚠ | mode*, expected_mode*, confirmation? | 实际 mode, order_authorized: false | trading/invalid-operation | 0 |
| 3 | `series` | ticker*, period(1m/5m/15m/30m/60m/1d), limit(20..2000) | ticker, bars | trading/series-unavailable | 10m |
| 4 | `equity` | mode, window | mode, points | trading/analytics-unavailable | 5m |
| 5 | `positions` | mode, window | mode, groups | trading/analytics-unavailable | 5m |
| 6 | `correlation` | tickers*, window | matrix | trading/analytics-unavailable | 30m |
| 7 | `sensitivity` | ticker, strategy, metric, fast_grid, slow_grid, buy_grid, sell_grid, start | ticker, matrix | trading/analytics-unavailable | 60m |
| 8 | `risk` | — | config | trading/analytics-unavailable | 15m |
| 9 | `trades` | mode, limit | trades | trading/analytics-unavailable | 5m |
| 10 | `events` | ticker*, days | ticker, events | trading/analytics-unavailable | 60m |
| 11 | `factors` | tickers*, window | tickers | trading/analytics-unavailable | 30m |
| 12 | `ic` | tickers*, factor, forward, window | points | trading/analytics-unavailable | 30m |
| 13 | `audit` | — | 计划→订单→成交三级链路 + 信号/响应链 | —（直读） | 2m |
| 14 | `sources` | no_probe | sources | trading/analytics-unavailable | 5m |
| 15 | `instrument` | ticker* | ticker + 标的解析信息 | trading/analytics-unavailable | 10m |
| 16 | `quality` | ticker* | ticker + 质量报告 | trading/analytics-unavailable | 60m |
| 17 | `plan` | — | plans, alerts, mode | trading/core-unavailable | 1m |
| 18 | `plan_execute` ⚠ | plan_hash*（action=execute 时）, expected_mode*, confirmation?, action(execute/cancel/kill/unkill) | {queued: true, nonce, action} | trading/invalid-operation | 0（不缓存） |
| 19 | `schedule` | — | heartbeat, jobs | trading/core-unavailable | 30s |
| 20 | `reconcile` | — | diffs, tca | trading/core-unavailable | 5m |

⚠ = 动作类工具（§5.1 审批链 A3/A4 的载体）。**通道分级（防模型自填口令）**：

- `mcp__quantwb__switch_mode` **仅限 sim→sim**：`mode:"live"` 一律拒绝（`trading/live-switch-web-only`）——模型可见的通道不得持有 live 切换能力，与 `quant_switch`「模型不能代替用户确认实盘」同一条不变量；**live 切换只能由用户在独立 Web 操作**（Web 的 `/api/wb/switch-mode` 保留口令流程）。
- `mcp__quantwb__plan_execute` 保留口令入口：这是规格 §8.3 承诺的对话侧等价入口——用户在对话中逐笔确认后由模型携带口令调用，与面板同 handler 同风控；live 切换不可经 MCP 完成，因此 MCP 侧 plan-execute 前的 live 状态必然源自用户的 Web 操作。

两者的口令与 `expected_mode` 复核均在**服务端 handler 内**执行（`store.switchMode` / `plan-execute` 分支）——「模式切换不授权交易」「live 双口令」不变量跨入口成立的机制保证。

错误语义：业务失败（`ok:false` + `trading/*` 错误码）作为**正常工具结果**返回（isError=false，文本为 JSON），模型可读码降级；仅 handler 之外的程序异常才以 isError=true 返回。

### 3.3 页签盘点表（11 页签 → 工具组合）

| 页签 | 现有组件 | 数据来源（工具） | 纯前端能力（6b 重实现项） |
|---|---|---|---|
| 行情 | MarketView + KLineCard | snapshot, series, instrument | K 线蜡烛图 + 十字准星悬停、周期切换（日线默认）、标的解析 |
| 信号 | SignalView + PreviewSummary | snapshot（previews 派生） | 信号卡、量化预览摘要 |
| 组合 | PortfolioView | positions, equity, snapshot | 按账户小计（不跨币种合并）、权益曲线、`as_of`/stale 标注 |
| 风险 | RiskView | risk, positions, equity | 风控配置卡、账户风险行、组合风险指标 |
| 因子 | FactorsView + QualityCard | factors, ic, quality | 因子表、IC 序列、参数敏感性热力图 |
| 执行 | ExecutionView + TradeSummaryCard | snapshot, trades | 去重订单事实（只归纳不推测）、动作行、原始响应审计区 |
| 研究 | ResearchView + ReportDetail | snapshot（runs/reports） | Markdown 研报渲染、来源列表、run 状态 |
| 事件 | EventsView | events | 事件时间线 |
| 计划 | PlanTab | plan, plan_execute | 目标 vs 实际 diff 表、逐单预检、执行窄门（live 口令输入）、状态轮询 |
| 调度 | ScheduleTab | schedule, reconcile, plan_execute(kill/unkill) | 心跳状态点（>5 分钟标红）、作业表、告警列表、kill 开关 |
| 审计 | AuditView + AuditChainCard + SourcesCard | audit, sources, reconcile | 三级链路展开、原始响应核对区 |

结论：每个页签的全部数据均可由 §3.2 的 20 个工具组合取得——**MCP 面能完整重建面板**，对等性成立。

### 3.4 维护工具（5 个，来自 `workbench_admin.mjs` 能力提升）

| # | 工具名 | 输入 | 行为 |
|---|---|---|---|
| 21 | `admin_status` | — | 数据文件路径与 runs/reports/previews/activity 计数 |
| 22 | `admin_runs` | — | 全部 run 及状态与年龄 |
| 23 | `admin_cancel_run` | run_id* | 取消指定 run（标记 cancelled，保留记录） |
| 24 | `admin_cancel_stale` | hours?（默认 2） | 取消超时仍 running 的 run |
| 25 | `admin_prune_runs` | hours?（默认 2） | 删除超时孤儿 run（无研报者） |

实现走 `WorkbenchStore` 直接方法（`cancelRun/cancelStaleRuns/pruneAbandonedRuns`），不经 RPC handler（它们是 Node 侧数据维护，与 20 端点同库同锁）。原 shell 脚本保留（薄封装同一方法）。

### 3.5 不入工具面的能力（防止面面俱到变成面面俱漏）

| 能力 | 归属 | 理由 |
|---|---|---|
| run_trading_analysis / research_publish / research_cancel / trading_status / quant_signal / quant_backtest / quant_report / quant_switch | Harness 原生工具（engine） | 会话绑定（session 归属校验）、审批锚；MCP 重复暴露会绕开 session 校验 |
| daemon 16 个 CLI 子命令、作业链管理 | trading_core / daemon | 运维域；`run_job` 等指令仍走白名单指令文件，不开 MCP 直调 |
| 富途账户/交易工具 | futu-mcp 行（不变） | 券商通道唯一入口，policy.js 三层审批锚定 |
| kill 文件手工清理 | RUNBOOK | `unkill` = 人工确认，不做自动化 |

注：规格 §8.3 承诺的对话侧 `plan_execute` 等价入口在现有代码中不存在（2026-09-15 grep 零命中）——WP6 的 `mcp__quantwb__plan_execute` 补上该缺口，且与面板同 handler 同风控。

### 3.6 协议与命名规范

- 传输：MCP **streamable-http**（与 preset 既有 futu-mcp 行同构）；HTTP API 与 MCP 同进程不同路径（`/mcp` 与 `/api/wb/*`）。
- 服务与预设行：`serverName: quantwb`；`failOnStartupError: false`（服务未启动时其余能力不受影响，与 futu 行同策略）；`toolCallTimeoutMs: 120000`（冷启动 factors 实测 25s，留余量）。
- 输入校验：每个工具的 inputSchema 按 §3.2 输入列生成（additionalProperties: false），超集字段在 schema 层拒绝；handler 内白名单校验保持不变（双保险）。
- 工具命名：读工具沿用端点名；动作工具 snake_case 动词（`switch_mode`、`plan_execute`）；维护工具 `admin_` 前缀。禁用名黑名单（exec/shell/file/token）由锁定测试断言。

### 3.7 依赖锁定表（2026-09-15 实测探测）

| 依赖 | 锁定 | 探测证据 |
|---|---|---|
| Node | ≥ 22（本机 v22.23.2） | `node --version` |
| `@modelcontextprotocol/sdk` | ^1.30.0（lockfile 提交） | `npm view` = 1.30.0，registry 可达 |
| `antd` | ^5（配 ProComponents 兼容矩阵，不追 6.x） | `npm view antd` 6.6.4 存在；ProComponents 2.8.x 对 antd5 为成熟组合 |
| `@ant-design/pro-components` | ^2.8.10 | `npm view` = 2.8.10 |
| `react` | ^18.3（ProComponents peer 稳妥区） | 组件库 peer 约定 |
| `vite` | 安装时锁最新稳定（lockfile 提交） | registry 可达 |
| Python MCP SDK | 不采用 | trading-venv 无 `mcp` 包；Node 侧复用 handler 决策（§二） |
| MCP 传输 | streamable-http | 与 preset futu-mcp 行同构，dsh-mcp-client 支持 |
| 端口/绑定 | 默认 8397 / 127.0.0.1，`~/.dsh/trading-platform.json` `{"service":{"port","token","host"}}` 可覆盖 | 新增约定（文件缺失取默认） |

锁定测试：`tests/wp6-locks.test.mjs`（25 工具名单与黑名单、端点集 = ENDPOINTS、目录/端口常量）+ `tests/test_core_wp6_locks.py`（指令白名单仍 5 种、config 路径与端口默认、口令字符串常量）。上游 schema 变化时人工重跑核验并更新本表。

## 四、Part B：Ant Design Pro 迁移方案（6b）

### 4.1 形态决策

**Vite + React 18 + antd5 + @ant-design/pro-components**（用户已确认）。理由：

1. 保留 Ant Design Pro 的 UI 体系（ProLayout 导航框架、ProTable/ProCard/StatisticCard 业务组件、ProForm 表单），即「Ant Design Pro 重实现」的实质；
2. 不引入 UmiJS max 全家桶（1200+ 包、mock 约定、own 路由编译体系）——本前端是单操作者本地页，收益不抵复杂度；
3. 与仓库「轻依赖、可离线测试」的工程约定兼容；升级路径：组件体系不变，未来如需 Umi 生态再迁壳。

### 4.2 信息架构与组件映射

```
platform/web/
  package.json / vite.config.mjs（dev proxy → 127.0.0.1:8397）
  src/
    app.jsx（ProLayout：菜单 = 11 页签 + 顶部模式徽章）
    services/api.js（POST /api/wb/<endpoint>；envelope 解包；_refresh；错误→message）
    pages/{market,signal,portfolio,risk,factors,execution,research,events,plan,schedule,audit}/
    charts/{kline,line,heatmap}.jsx（canvas 移植）
      geometry.js（barIndexAt / tooltipLeft / compactNumber 纯函数，node --test 直测）
```

| 现有实现 | 迁移为 |
|---|---|
| 页签数组 + switch 分支 | ProLayout 菜单路由（11 页） |
| Card/Paged 列表 | ProCard + ProTable（分页内建，替 Paged） |
| 权益/IC 折线（canvas 自绘 LineChart） | 移植（保留自绘，逻辑已有测试） |
| K 线蜡烛 + 悬停十字准星 | 移植 `useCanvasChart`/`drawKLineHover` 及几何纯函数 |
| 相关性/敏感性热力图 HeatmapChart | 移植 |
| Markdown 研报渲染（自研 parser） | 保留自研 parser 移植（有测试；不新增 markdown 依赖） |
| 模式徽章 / 口令输入 / 免责声明 | Modal confirm + Input（口令型），样式随 antd |

### 4.3 数据层与缓存语义（与现状逐条对齐）

- 两层缓存变一层半：Host TTL 缓存**不变**（同一 handler）；客户端内存缓存改为 `services/api.js` 的按 (endpoint,payload) 内存 Map（TTL 参照现 client.js 常量），`刷新`按钮 = 清空 + 5 秒穿透（`refresh: true`）。
- `snapshot` 60 秒兜底轮询、面板关闭停止轮询的语义保留（页面级 `useEffect` 清理）。
- 缺失端点自检语义转译为 HTTP 404 + `snapshot.endpoints` 声明比对：前端对未声明端点直接显示「服务版本陈旧，请重启服务」，不发起请求。
- 业务失败（`ok:false`）展示错误码 + message（如「该单被规则5拦截…」），不弹全局错误掩盖降级数据。

### 4.4 文案规范（全局约定 2.1 强制）

页面字符串仍只允许三类：数据事实、操作反馈、安全与合规必需（SIM/LIVE 徽章、口令输入、一行免责声明）。设计要求、规范引用、实现原则一律写注释。验收时对 `platform/web/src` 提取字符串字面量 grep「规格|设计稿|示例|宁可|窄门|token」= 0（注释除外）。

### 4.5 执行窄门 UI（不变量清单）

1. 模式切换：sim→live 必须输入「确认实盘」；`expected_mode` 随请求；成功后显示 `order_authorized: false`；
2. 计划执行：frozen 状态才可执行；live 必须输入「确认执行」；提交后仅显示「已提交，等待 daemon 回写状态…」并轮询 `plan`；
3. kill 开关：激活/解除均走 `plan_execute` 的 kill/unkill 动作；解除文案注明「人工确认后解除」；
4. 全站页头常驻 SIM/LIVE 徽章 + 一行免责声明。

### 4.6 构建与托管

- 开发：`npm run dev`（Vite，proxy `/api` → 127.0.0.1:8397）；生产：`npm run build` → `platform/web/dist`，由服务进程静态托管（`GET /`）。
- 服务进程零框架（`node:http` + MCP SDK 的 StreamableHTTPServerTransport 挂 Node req/res），路由仅：`POST /api/wb/:endpoint`、`GET /healthz`、`GET /mcp`+`POST /mcp`（streamable-http）、静态 dist。
- 启停：`node platform/server/start.mjs`；RUNBOOK 增加 systemd unit 样例与端口/token 配置说明。

### 4.7 安全边界变化声明（诚实清单）

1. **脱离 Connection 信任**：现面板继承 Harness Connection 的认证与生命周期；独立 Web 是本地 HTTP 服务，认证边界变为 **loopback 绑定 + 可选静态 Bearer token**（配置 `service.token` 后 API/MCP 均要求 Authorization）。这是明示的能力降级（相对 Connection），换来的是脱离面板宿主的自由；单机单操作者前提下可接受。
2. **浏览器不含任何券商凭据**：前端只持有一个本地服务 token（可选）；券商 token 仍只在服务进程所在主机的文件里。
3. **服务进程能力面**：只能读写既有数据文件（store JSON、指令目录、SQLite 只读快照），无 shell、无任意执行；`switch-mode`/`plan-execute` 口令校验在服务端，浏览器绕不过。
4. **同机其他进程可访问 8397**：loopback 不防本机进程；文档明示，token 为可选加固，不做虚假承诺。
5. **敏感字段脱敏沿用** `store.sanitize`（token/secret/password/authorization/cookie → [redacted]，64KB 截断），MCP/HTTP 输出同源。

## 五、Part C：审批回归方案（6c）

### 5.1 审批面盘点（迁移必须保持的 8 条链条）

| 链 | 内容 | 现状锚点 |
|---|---|---|
| A1 | 账户模式互斥：sim 模式拒 `account_*`/`trading_*`；live 模式拒 `sim_trade_*` | `policy.js` guard |
| A2 | live 写操作原生审批：`trading_*` 在 pre-execute 强制 `{kind:"ask"}`；`sim_trade_*` 永不 ask | `policy.js` pre-execute |
| A3 | 模式切换双保险：live 需口令「确认实盘」+ `expected_mode` 一致 + 无在途租约；切换不授权下单；**MCP 通道 switch_mode 仅限 sim→sim**（模型自填口令被通道规则封死） | `store.switchMode` + manifest 通道规则 |
| A4 | 计划执行窄门：live 需口令「确认执行」+ `plan_hash` + `expected_mode` 复核；成功仅 `queued`；daemon 侧 kill 文件 + 风控 8 规则兜底 | rpc plan-execute 分支 + commands + risk |
| A5 | `quant_switch` 只能切 sim；模型不能代替用户确认实盘 | engine tools |
| A6 | 在途租约：`enterBrokerCall` 期间拒绝模式切换 | store 租约 |
| A7 | 工具面白名单封闭：MCP 25 工具无 shell/exec/token 类；HTTP 20 端点白名单外 404 语义（Unknown operation） | manifest 锁定测试 |
| A8 | 前端无下单面：AntD Web 仅可调 `/api/wb/*` 20 端点，无券商直连代码 | 静态检查 + 代码评审 |

WP6 新增入口（MCP `switch_mode`/`plan_execute`、HTTP 同名路径）**必须复用同一 handler**，使 A3/A4 在新入口上零新增逻辑——这是回归方案的核心架构手段。

### 5.2 自动回归矩阵（三层）

**Node 层**（`tests/wp6-approval-regression.test.mjs`，离线，临时 DSH_HOME）：

| 用例 | 断言 |
|---|---|
| R1 | sim 下 guard 拒 `mcp__futu__account_positions`（消息含「账户模式」）；live 下拒 `sim_trade_*`（经 `installTradingPolicy` 假 ctx 驱动） |
| R2 | live 下 `trading_*` pre-execute 返回 `ask` 且 reason 含「真实账户操作」；`sim_trade_*` 放行不 ask |
| R3 | `switch_mode`：无口令 live 拒绝；错口令拒绝；对口令成功且 `order_authorized:false`；`expected_mode` 过期拒绝；租约期间拒绝（WorkbenchBusyError）；**MCP manifest 的 switch_mode 对 mode:"live" 无论口令一律拒绝（trading/live-switch-web-only）** |
| R4 | `plan_execute`：live 无口令拒绝；带口令 → `{queued:true,nonce}`；指令文件落盘含 `plan_hash/expected_mode` 且**不含口令字段**；action 四映射到白名单指令 |
| R5 | manifest：恰 25 工具；端点工具集 ≡ ENDPOINTS；无黑名单名（exec/shell/file/token/write_file） |
| R6 | HTTP 与 MCP 同源：两入口对同一 payload 结果一致（同一 handler 实例断言；实现位置：`platform/tests/mcp.test.mjs` S3 末断言） |

**Python 层**（`tests/test_core_wp6_approval.py`，离线）：

| 用例 | 断言 |
|---|---|
| P1 | kill 文件存在时 execute_plan 被风控规则 1 拒（复用 WP4 e2e 驱动风格）；unkill 后恢复 |
| P2 | 指令白名单恒 5 种；processed/ nonce 幂等不回归 |
| P3 | 服务 config 锁定：端口默认 8397、路径常量、口令常量与 Node 侧锁定表一致（读 `tests/wp6-locks.test.mjs` 同源常量的 Python 镜像） |

**协议层**（`platform/tests/mcp-smoke.test.mjs`，loopback 集成，临时 DSH_HOME）：

| 用例 | 断言 |
|---|---|
| S1 | 起真实服务进程 → SDK Client initialize → tools/list 恰 25 且与 manifest 一致 |
| S2 | call snapshot → ok；call switch_mode(live, confirmation=「确认实盘」) → ok:false（trading/live-switch-web-only）且模式仍 sim |
| S3 | call plan_execute(未知 plan) → ok:false；HTTP /api/wb/snapshot 与 MCP snapshot 同值 |
| S4 | 未声明端点 POST /api/wb/not-an-endpoint → 4xx 语义，无旁路 |

### 5.3 人工会话回归清单（preset 更新后，新会话执行，记录进 WP6 验收记录）

1. 新建 Harness 会话：`mcp__quantwb__*` 25 工具出现（tools 列表核对）；
2. sim 下调 `mcp__quantwb__switch_mode(live, expected=sim)`（带不带口令各试一次）→ 一律拒绝（trading/live-switch-web-only），模式不变；
3. 独立 Web 切 live：口令「确认实盘」→ 页头 LIVE 徽章；`quant_switch` 仍拒 live；
4. live 下对话请求一个 `mcp__futu__trading_*` 查询/写工具 → Harness 原生审批卡出现（ask）——确认与拒绝各演练一次；
5. 独立 Web 计划页 live 执行：无口令拒；带口令 → queued；`scripts/drills.sh` kill 演练联动拒单。

### 5.4 preset 行替换明细（6c）

| 文件 | 变更 |
|---|---|
| `agent.cordis.yml` | 新增 `quant-platform-mcp` 行（dsh-mcp-client → `http://127.0.0.1:8397/mcp`，serverName quantwb，failOnStartupError: false，toolCallTimeoutMs: 120000）；persona 两条修订：工作台指向独立 Web URL（服务默认 `http://127.0.0.1:8397`），并新增「MCP 工具面与面板对等；live 口令门槛经 MCP 同样生效」 |
| `plugins/workbench/cordis.patch.yml` | **保持不动**：`trading-workbench` 行是 `tradingWorkbench` 服务与 policy 审批的进程内锚，移除即破坏 A1/A2/A6 |
| legacy 面板（shell.overlay） | 过渡期保留（同库数据，不冲突）；AntD Web 验收通过后另行提交移除，不在 WP6 范围内 |
| `preset.yml` | 描述行补「独立工作台 + MCP 工具面」表述 |

### 5.5 回归失败处置

- Node/Python 用例失败 → 修复服务或前端实现，**禁止**放宽断言；
- 人工清单第 3/4/5 步任一失败（口令绕过、审批缺失、kill 失效）→ 判 WP6 验收失败，回滚 preset 行变更（`agent.cordis.yml` 还原即回到纯面板形态），修复后重跑全矩阵。

## 六、测试与验收标准（对齐全局约定 2.3 与规格 §九风格）

1. 全量离线套件全绿：`~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'`；`node --test tests/*.test.mjs platform/tests/*.test.mjs`（含 mcp-smoke，loopback）；
2. §5.2 矩阵 R1–R6 / P1–P3 / S1–S4 逐条对应提交留档；
3. 独立 Web 11 页签在真实数据（或如实降级态）下可用；文案规范 grep 自查 0 命中；
4. MCP tools/list = 25 且无黑名单工具（对等性与封闭性双断言）；
5. 人工会话回归清单 5 步留痕；
6. 文档修订（§七）与实现一致（抽查每个文档提到的路径/端点存在）；
7. live 准入仍按 P4 清单，人工评估项保持未勾选。

## 七、需同步修订的文档

| 文档 | 修订内容 |
|---|---|
| `docs/architecture.md` | 组件职责表加「独立服务进程 + AntD Web」；拓扑图补 MCP/HTTP 面；安全边界声明（§4.7） |
| `docs/RUNBOOK.md` | 服务启停、systemd unit 样例、端口/token 配置、mcp-smoke 排障 |
| `README.md` | 目录结构加 `platform/`；bootstrap 说明（`npm install --prefix platform`、前端构建） |
| `docs/HANDOVER.md` | 交接注意加：服务进程与 Harness 进程的双进程数据约定、legacy 面板过渡策略 |
| `docs/P4-live-trading.md` | 实盘准入清单加「独立 Web + MCP 入口的审批回归 5 步通过」 |
| `2026-09-14-platform-plan-index.md` | WP6 行补规格/计划文件指针与验收记录位置 |

## 八、风险与诚实清单

1. **双进程并发写 store**：服务进程与 Harness 进程共享 `trading-workbench.json`；既有原子写 + 独占锁跨进程成立，但「两处同时切换模式」的竞态窗口由 `expected_mode` 复核兜底（后到者拒绝）。
2. **MCP 工具是模型可直接调用的**：`plan_execute` 对模型可见，口令是门槛（用户在对话中逐笔确认后由模型携带，规格 §8.3 设计如此）；`switch_mode` 经 MCP 被封在 sim→sim（§3.2 通道分级），live 切换只有用户的 Web 通道。这是半自动模式的固有边界，与 daemon 通道同级，不宣称达到实盘安全保证。
3. **npm 离线环境**：bootstrap 需联网一次；lockfile 提交后可复现安装。
4. **ProComponents 版本漂移**：antd 锁 5.x + lockfile；升级属显式变更。
5. **K 线/热力图移植失真**：几何纯函数带测试移植，视觉回归靠人工清单比对。
6. **本规格不改台账/审计保留口径**：各列表仍最近 100 项，完整审计仍在 Harness 会话。
