# 量化交易决策平台 V3.0（Harness-Centric）

「平台是身体，Harness 是大脑」的 V3.0 实现：本服务是平台身体（调度器/风控/OMS/Harness 集成网关/Web），
DeepSeek Harness 通过三条通道接入。**数据来源完全复用既有项目配置，一个不删：**

- 既有工作台 `http://127.0.0.1:8397` 的 77 个 `POST /api/wb/{tool}` 工具（行情/因子/风控/账户/治理）——
  V3.0 六域工具 = 对它的受控代理 + 少量一级高频工具（FR-TOOLS-003 工具发现代理）；
- 富途 OpenAPI/OpenD 与 MCP Bearer（`~/.dsh/futu-openapi.json`、`~/.dsh/futu-token`，只读引用）；
- `~/.dsh/trading-platform.json`（自选池、自动流水线、服务配置）；
- `~/.dsh/trading-venv`（akshare/pandas 等 Python 数据面）。

**交易边界不变**：`trade_place/trade_modify/trade_cancel` 不设一级工具，`call_tool` 直通时确认闸门仍在
既有工作台 Web（确认卡 + TTL）；`switch_mode` 只接受 live→sim，sim→live 由用户在 8397 Web 输口令完成。

## 运行

```bash
cd platform-v3
npm start                 # 平台服务（API + 9 页 UI），默认 http://127.0.0.1:8407
npm run start:mcp         # 平台 MCP 服务器（stdio，NDJSON），供 dsh-cordis-universal-adapter / 任意 MCP 客户端拉起
npm test                  # node --test
```

环境变量：`QUANT_V3_PORT`（默认 8407）、`WORKBENCH_BASE`（默认 http://127.0.0.1:8397）、
`DSH_BIN`、`QUANT_HEADLESS_PROFILE`（默认 headless）、`QUANT_HEADLESS_TIMEOUT_MS`（默认 300000）、
`QUANT_HEADLESS_CONCURRENCY`（默认 3）、`QUANT_HEADLESS_TOKEN_BUDGET`（默认 200000）、
`QUANT_SDK_ENABLED`（SDK 通道开关）、`QUANT_SDK_PROFILE`（默认 sdk）、`QUANT_SDK_PROVIDER`/`QUANT_SDK_MODEL`/
`QUANT_SDK_EFFORT`/`QUANT_SDK_MAX_TOKENS`/`QUANT_SDK_CWD`（SDK 握手路由参数）。

## 三通道

| 通道 | 入口 | 状态 |
|---|---|---|
| MCP Bridge | `server/mcp/run.mjs`（stdio）+ `POST /api/v3/mcp`（单消息 JSON-RPC） | running |
| SDK JSON-RPC | `server/gateway/sdk.mjs`（按 `dsh-sdk-protocol` 线协议实现）| 已实现；缺模型密钥或未部署 sdk profile 时如实 pending |
| Headless CLI | `POST /api/v3/headless/run` + 调度器（08:30 / 12:00 / 16:00），外部熔断（并发 3 / 300s / 200K token） | ready |

SDK 通道用 `dsh --profile sdk`（`sdk` 是 **shipped profile**，由 `@deepseek-ai/dsh-sdk-app` bundle 提供）拉起隔离运行时：
握手校验 `serverInfo.name === deepseek-harness-sdk-runtime`，`session/prompt` 入队并回收 `session.event` /
`session.status` 通知。运行时 home 用 `QUANT_SDK_HOME` 独立指定（缺省 `DSH_HOME`），避免与线上 home 混用。

**握手不需要模型密钥**（真实运行时实测可在无密钥下完成 `initialize`），只有 `session/prompt` 走到模型调用时
才需要：缺密钥时该轮以 `MISSING_CREDENTIAL` 结束，通道状态里的 `lastTurn` 会带上运行时原文与错误码，
`sessionWarning` 同步提示。真实会话请在启动环境注入 `DEEPSEEK_API_KEY`（或 `ZAI_CODING_CN_API_KEY`）。

```bash
QUANT_SDK_ENABLED=1 QUANT_SDK_HOME=$PWD/platform-v3/data/sdk-home npm start
curl -sX POST localhost:8407/api/v3/sdk/start          # → serverInfo: deepseek-harness-sdk-runtime
curl -sX POST localhost:8407/api/v3/sdk/prompt -H 'content-type: application/json' \
     -d '{"sessionId":"quant-001","text":"分析当前持仓风险"}'
curl -s localhost:8407/api/v3/gateway | jq .channels.sdk.lastTurn
```

## 研究流水线与回测（strategy-svc）

`server/strategy/` 实现规格的 PDAT→PAAT→PCPT→PRT→PET 五阶段：

- **PDAT** 取 workbench `series` 真实富途日 K（自选池前 8 只，默认）；
- **PAAT** 取 workbench `factors` 的真实多因子 z 值（横截面 8 只需 ~40s，故单独放宽超时）；
  因子不可用时退化为「本地 K 线动量横截面 z」并在 `stages.PAAT.scoreSource` 如实标注；
- **PCPT/PRT/PET** 排名 → 等权目标权重（受单笔上限约束）→ 产出调仓建议**提案**（含依据与风险等级）；
- 提案不落单：执行仍走既有工作台受约束入口（`trade_place` + Web 确认卡）。

回测引擎 `server/strategy/backtest.mjs`：单标的动量 long/flat 向量化回测 + 参数网格扫描。
**PIT 对齐**：t 日持仓只由 ≤ t-1 的收盘价决定，无前视；指标口径为持仓日基准
（`heldDays` / `flatDays` / `winRatePct` / `signalFlips`），空仓日不计入胜率。
局限：等权、无滑点与佣金建模，属于轻量自研引擎，与 workbench 的因子回测互补。

## API（节选）

`GET /healthz`、`GET /api/v3/overview|brain|market|strategy|risk|execution|gateway|tools|settings`、
`POST /api/v3/strategy/run`、`GET /api/v3/strategy/last`、`POST /api/v3/ml/backtest`、
`POST /api/v3/ml/param_sweep`、`POST /api/v3/headless/run`、`POST /api/v3/risk/check`、
`GET /api/v3/sdk`、`POST /api/v3/sdk/start`、`POST /api/v3/sdk/prompt`、`POST /api/v3/mcp`。

MCP 工具面：`list_tools` / `call_tool`（六域发现代理，覆盖既有 77 工具）+ 一级工具
`query_quote`、`market_snapshot`、`query_order_book`、`query_capital_flow`、`query_financial`、`stock_screen`、
`eval_factor_ic`、`list_factors`、`factor_sensitivity`、`sentiment_history`、`calc_indicator`、`check_risk`、
`query_position`、`account_funds`、`orders_open`、`deals_today`、`request_approval`、`audit_trail`、
`research_tasks_claim` + 本地计算工具 `run_backtest`、`param_sweep`、`strategy_run`。

## 监控与审计（§8.3 / §4.2）

`GET /metrics` 暴露 Prometheus 文本指标（进程内计数 + 实时分量），字段与规格的告警项一一对应：

| 指标 | 用途 / 告警阈值（规格 §8.3） |
|---|---|
| `quant_v3_headless_calls_total{result}` + `quant_v3_headless_duration_ms_sum` | Headless 调用成功率 < 95%、平均耗时 > 60s |
| `quant_v3_sdk_channel_ready` | SDK 会话就绪度（活跃数 > 10 由上层会话表看） |
| `quant_v3_mcp_tool_calls_total{tool,result}` + `quant_v3_mcp_tool_duration_ms_sum` | MCP 工具调用延迟 > 5s、失败率 > 5% |
| `quant_v3_wb_calls_total{tool,result}` + `quant_v3_wb_call_duration_ms_sum` | 数据源（workbench 77 工具）连接与延迟 |
| `quant_v3_workbench_up` | 数据源断连 |
| `quant_v3_oms_orders{stage}` | 待人工确认/阻断突增 |
| `quant_v3_headless_active` / `quant_v3_headless_queued` | 熔断并发与排队 |

**审计**：所有变更类 API（POST，`/api/v3/mcp` 除外）与**每次 MCP 工具调用**（含 `trade_*` 直通）
追加写入 `data/audit.jsonl`（时间、动作、状态、耗时、源地址）。

**常驻运行**：`install/quant-v3.service` 为 systemd 单元模板（默认 127.0.0.1:8407，
日志 `~/.dsh/quant-v3.log`，SDK 通道默认关闭）。

## 部署（角色拆分 / 容器 / K8s）

同一镜像按 `QUANT_V3_ROLE` 挂载不同子系统，对应规格 §5.1 的服务拆分：

| 角色 | 提供 | 说明 |
|---|---|---|
| `all` | 全部 | 单机默认，行为与既有部署一致 |
| `gateway` | HTTP API + MCP 工具面 + `/metrics` | 不提供静态 UI；可多副本 |
| `web` | 9 页控制台 UI + 只读 API | 可多副本 |
| `scheduler` | 调度循环 + Headless 通道 | **单副本语义**（同日同规则只触发一次），只暴露 `/healthz` |

角色控制是真实生效的（实测：`scheduler` 角色请求 `/api/v3/tools` 返回 404，`gateway` 角色请求
`/index.html` 返回 `role/not-serving-ui`）。

**配置来源与运行时 home 分离**：`QUANT_CONFIG_HOME` 只用于读取既有配置
（`trading-platform.json` 自选池/流水线、`futu-token`、`futu-openapi.json`），`DSH_HOME` 仍是运行时
自己的 home（凭据库、会话）。容器化时把宿主机配置目录**只读**挂进来即可复用全部数据来源：

```bash
cd platform-v3
docker build -f deploy/Dockerfile -t quant-platform-v3:3.0.0-alpha.1 .
docker run -d --name quant-v3 --network host \
  -e QUANT_V3_ROLE=gateway -e WORKBENCH_BASE=http://127.0.0.1:8397 \
  -e QUANT_CONFIG_HOME=/config -v "$HOME/.dsh:/config:ro" \
  quant-platform-v3:3.0.0-alpha.1
curl -s localhost:8407/healthz && curl -s "localhost:8407/api/v3/market/watchlist?n=3"
```

- `deploy/docker-compose.yml`：单机最小形态（gateway + scheduler，共享数据卷）。
- **调度器镜像必须用 glibc 基础镜像**：`node:22-alpine` 是 musl，缺 glibc 下的 `node-pty` 原生模块，
  dsh 基础 bundle 导入 `dsh-subprocess-local` 时直接抛 `Failed to load native module: pty`
  （实测：scheduler 容器内 headless 调用 exit=1，诊断即此错误）。因此拆两个镜像：
  `deploy/Dockerfile`（alpine，供 gateway/web，不需要 dsh）与
  `deploy/Dockerfile.scheduler`（bookworm-slim + 编译工具 + 全局 dsh，供 scheduler）。
  容器内跑 headless 还需要**预置运行时 home**：`DSH_HOME` 里要有 `profiles/`（可从配置卷初始化
  复制）与模型凭据；二者缺一，headless 会以 exit 1 如实失败并写明诊断。
- 启动自检：`QUANT_V3_SCHEDULER_SMOKE=1` 会在服务启动后立即触发首条规则，把
  调度 → Headless → 记录 → 熔断计数整条链路真跑一次（用于部署验收）。
- `deploy/k8s/platform-v3.yaml`：ConfigMap + Secret + 三个 Deployment/Service（gateway×2、web×1、
  scheduler×1，含探针与资源限额）。**既有工作台（8397）不在集群内**——它是数据源与唯一执行入口，
  集群内 pod 通过 `WORKBENCH_BASE` 指向它；交易边界不因容器化改变。

```
sudo cp install/quant-v3.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now quant-v3
systemctl status quant-v3 && curl -s localhost:8407/metrics | head
```

## 目录

```
server/          服务端（零依赖 Node ≥22.19）
  mcp/           平台 MCP 服务器（stdio + HTTP 端点共用核心；含本地计算工具）
  gateway/       三通道：headless.mjs（Headless Runner + 调度器 + 外部熔断）、sdk.mjs（SDK JSON-RPC 客户端）
  strategy/      PDAT→PET 研究流水线（pipeline.mjs）+ 回测与参数扫描引擎（backtest.mjs）
  risk.mjs       风控分级（自动/人工/红线阻断）    oms.mjs  订单台账 + 与工作台对账（无下单入口）
  observability.mjs  指标（/metrics）与审计（data/audit.jsonl）
install/         systemd 单元模板
web/             9 页控制台 UI（OpenDesign 设计稿）+ app.js 实时数据层
test/            node --test（34 例，含协议桩与集成测试）
data/            运行时数据（gitignore）：headless 调用日志、OMS 台账、策略研究轮、审计日志
```
