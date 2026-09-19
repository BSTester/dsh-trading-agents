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
`QUANT_SDK_ENABLED`（SDK 通道，需要 SDK 服务插件时开启）。

## 三通道

| 通道 | 入口 | 状态 |
|---|---|---|
| MCP Bridge | `server/mcp/run.mjs`（stdio）+ `POST /api/v3/mcp`（单消息 JSON-RPC） | running |
| SDK JSON-RPC | 需要 `@deepseek-ai/dsh-sdk-jsonrpc-server` + `deepseek-harness-sdk`（当前部署未装，状态如实标注 pending） | pending |
| Headless CLI | `POST /api/v3/headless/run` + 调度器（08:30 / 12:00 / 16:00），外部熔断（并发 3 / 300s / 200K token） | ready |

## API（节选）

`GET /healthz`、`GET /api/v3/overview|brain|market|strategy|risk|execution|gateway|tools|settings`、
`POST /api/v3/headless/run`、`POST /api/v3/risk/check`、`POST /api/v3/mcp`。

MCP 工具面：`list_tools` / `call_tool`（六域发现代理，覆盖既有 77 工具）+ 一级工具
`query_quote`、`market_snapshot`、`query_order_book`、`query_capital_flow`、`query_financial`、`stock_screen`、
`eval_factor_ic`、`list_factors`、`factor_sensitivity`、`sentiment_history`、`calc_indicator`、`check_risk`、
`query_position`、`account_funds`、`orders_open`、`deals_today`、`request_approval`、`audit_trail`、
`research_tasks_claim`。

## 目录

```
server/          服务端（零依赖 Node ≥22.19）
  mcp/           平台 MCP 服务器（stdio + HTTP 端点共用核心）
  gateway/       Headless Runner + 调度器（外部熔断）
  risk.mjs       风控分级（自动/人工/阻断）+ OMS 状态机
web/             9 页控制台 UI（OpenDesign 设计稿落地）
test/            node --test
data/            运行时数据（gitignore）：headless 调用日志、OMS 台账（FR-MON-003）
```
