# V3 接入与授权：Agent/调度侧只走平台 MCP + 待注入密钥

本文分两块：

1. **Agent/调度侧与「量化交易决策平台 V3」之间的唯一交互面** —— 平台 MCP
   （`http://127.0.0.1:8397/mcp`）：调用约定、认证与口令、只读 vs 写入、错误信封、工具清单，
   以及**第三方 adapter 被弃用的真实报错证据**；
2. **数据源与密钥现状** —— 已对齐的接口、需要密钥的位置、注入后如何生效。

原则不变：没有密钥时接口不报 500、不发无效请求，而是返回可读错误
（`{ok:false,error:{code,message}}`），页面/工具调用显式显示「无数据源 + 原因」；
注入后无需改代码，重启服务即生效。

---

## 一、Agent/调度侧的唯一交互面：平台 MCP

### 1.1 端点与调用约定

* **端点**：`POST http://127.0.0.1:8397/mcp`（也有 `GET`，用于 SDK 的会话/流式语义）。
  传输是 **MCP streamable-http**（`mcp` SDK 2.2.0 的 `streamable_http_app(json_response=True)`），
  挂在 FastAPI 主 app 的 router 上（不是 `Mount`，因此裸 `/mcp` 不会 307 跳转）。
* **协议序列**（实测可用，非推测）：`initialize` → `notifications/initialized` →
  `tools/list` / `tools/call`；会话 id 由服务端在响应头 `mcp-session-id` 给出，
  后续请求带上即可。`curl` 探针：

  ```bash
  # 1) 握手（记下响应头里的 mcp-session-id）
  curl -s -D - -X POST http://127.0.0.1:8397/mcp \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'

  # 2) 列工具 / 调工具（<SID> 用上一步的值）
  curl -s -X POST http://127.0.0.1:8397/mcp -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' -H 'mcp-session-id: <SID>' \
    -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
  curl -s -X POST http://127.0.0.1:8397/mcp -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' -H 'mcp-session-id: <SID>' \
    -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"v3_risk","arguments":{}}}'
  ```

* **工具面 = 两段，同一个 `/mcp`**：
  * **既有工作台工具 77 个**（`snapshot`、`series`、`positions`、`trade_place`…）——
    名字与行为**一字未改**；
  * **`/api/v3/*` 路由桥接出的 `v3_*` 工具**（随路由表自动增减；本 worktree 41 个）；
  * **两个发现代理入口**（只有 `discovery` 模式直连可见）：`list_tools`（检索，返回精简卡片）
    与 `call_tool`（转发到同一实现）。见 §1.5.1–1.5.4。
* **表面模式**（`QUANT_MCP_SURFACE=discovery|direct`，缺省 `discovery`）：决定上面这些工具
  **哪些直接进 `tools/list`**。`direct` = 全部直连（老行为）；`discovery` = 6 件
  （4 件直连保留 + 两个入口），其余经 `call_tool` 间接可达——**省的是 schema token，不是能力**。
* **桥接实现**：`platform/server/v3_mcp.py`（装配点在
  `platform/server/app.py` 的 `v3_bridge, v3_tool_names = v3_mcp.register(app.state.mcp, app)`）。
  **唯一事实来源是 FastAPI 路由表**：遍历 `app.routes` 里所有 `/api/v3/*` 路由，
  每条造一件工具；工具调用体只有一件事——**拿到该路由的 `endpoint` 函数对象，用实参调用它，
  把返回的信封原样交回**。因此：

  * **不复制业务逻辑**：HTTP 面与 MCP 面调的是同一个函数对象（同一份字段适配、同一份市场过滤）；
  * **不绕过限流与缓存**：富途限流器（`server/v3_ratelimit`，令牌桶+并发上限+单飞+退避+冷却）、
    各子模块的 TTL/落盘缓存、SQLite 台账都在 handler 内部，桥接层碰不到也不需要碰；
  * **覆盖性由构造保证**：工具集 = 路由表的一次遍历；`platform/tests/test_mcp_parity.py`
    再对「路由 ⇄ 工具」双射断言一次（缺工具/孤儿工具都必须红）。
  * **永不阻断平台启动**：路由表里出现新路由而工具面描述还没登记时，桥**降级**
    （取 handler docstring 兜底、未知注解按 `str`、保守标记只读）并记进 `DescriptionGaps`，
    由 parity 测试断言「欠账为空」——漂移在测试里红，而不是让服务起不来。

### 1.2 认证与口令

| 面 | 约束 |
|---|---|
| `POST /mcp` 与 `/api/*` | 配置了 `token` 时**必须** `Authorization: Bearer <token>`；`/healthz` 豁免。`/mcp` 的判定是「等值或前缀」，SDK 将来加子路径也不会漏出未鉴权旁路 |
| 实盘执行（`plan_execute`） | live 需对话口令 **「确认执行」**，且服务端 `confirmation` 闸门仍复核；模型只能发起，人必须逐笔确认 |
| sim→live 切换 | **只能由人在独立 Web 输入口令「确认实盘」**。`switch_mode` 工具收 `mode=live` 时直接返回 `trading/live-switch-web-only`，**不触达 handler / 不触达 store** |
| 交易写（`trade_place/modify/cancel`） | 过完整闸门链（模式文件 → 风控 8 规则 → kill → 业务确认），提交后需在独立 Web 确认卡片批准，TTL 120s 超时自动拒绝（fail-closed） |
| **有意不进工具面**（HTTP-only / 人工动作） | `confirm-decide`（唯一能批准实盘操作的通道）、`openapi_config` / `openapi_test` / `openapi_oauth`（经纪商凭据读写与授权）、`auto_pipeline`（自动流水线总开关）、`rules-decide`（策略上岗批准）、`research-tasks-list`（队列清单给人看）、`warrant_screen` / `modify_user_security` / `info_rehab` |
| `/api/v3/credentials` 的 `save` / `clear` | **在 MCP 面封死**：返回 `v3/credentials-web-only`，**先于 endpoint 调用判定**（handler 零调用、磁盘零写入）。数据源凭据的写入只能由人在 Web 设置页完成——与经纪商凭据同一条不变量。`status`（读状态）与 `test`（只读连通性测试）保留 |

`/api/v3/*` 里**没有任何交易写端点**：下单/改单/撤单/切模式/执行计划全部只在 `/api/wb/*`
与工作台 Web；桥接不新增、也不转发任何交易工具（parity 测试里有一条断言专门钉死这点）。

### 1.3 只读 vs 写入

只读工具在 `tools/list` 里带官方标注 `annotations.readOnlyHint = true`。当前只有 3 件不是只读：

| 工具 | 路由 | 为什么不是只读 |
|---|---|---|
| `v3_strategy_run` | `POST /api/v3/strategy/run` | 跑一轮 PDAT→PET 研究流水线并**落盘**研究轮记录（产物只是调仓提案，**不下单**） |
| `v3_oms_sync` | `POST /api/v3/oms/sync` | **重写本地 OMS 台账**（登记/分级/对账，不产生订单） |
| `v3_credentials` | `GET`/`POST /api/v3/credentials` | 同路径带 POST；其写动作 `save`/`clear` 已封死（见 1.2） |

两个看起来像写、实际是**只读语义**的 POST 端点照实标只读（取数 + 写本地缓存，不涉交易，
handler 自己的 docstring 也是这么写的）：`v3_markets_calendar_refresh`（刷新交易日历缓存）、
`v3_metrics_probe_refresh`（风险探测落盘，供 `/metrics` 只读）。

### 1.4 错误信封

* **业务失败是正常工具结果**：`isError=false`，文本是紧凑 JSON
  `{"ok":false,"error":{"code":"…","message":"…","details":{}}}`。错误码沿用平台既有码
  （`market/no-universe`、`tushare/no-token`、`futu/rate-limited`、`akshare/internal`…），
  **不翻译、不吞原因**。
* **handler 之外的程序异常**才是 `isError=true` + `trading/tool-failed` 信封。
* **成功**：`{"ok":true, …}`，凡有数据源的一律带 `source` 与 `as_of`（例如
  `v3_risk` → `data.source / data.as_of`；`v3_sources_status` → `as_of` + 每条链的尝试结果）。
* **缺数据 → 明确「无数据源 · 原因」**：不返回 0 分/空数组冒充成功
  （`v3_sentiment` 无资讯时 `score:null` + `notes`；`v3_watchlist` 无池子时
  `market/no-universe` + `detail`；`v3_orderbook` 无行情权限时原样透传 `errcode=-9`）。
* **二进制**：`v3_research_report_pdf` 的成功响应是 PDF——桥以标准信封回元数据
  （`content_type`/`bytes`/`filename`），正文 ≤1 MiB 时内联 `content_base64`，超过只给元数据
  （正文用 HTTP GET `/api/v3/research/report.pdf` 取）；渲染失败仍走标准错误信封。

### 1.5 工具清单（`v3_*`，实测 `tools/list` 导出）

生成方式（**不是手抄**，路由/描述/只读标记都来自注册时的同一份定义）：

```bash
cd platform && ~/.dsh/trading-venv/bin/python -B -c "
import sys; sys.path.insert(0,'.')
from server import app as appmod, v3_mcp
app = appmod.create_app(home='/tmp/probe-home')
for d in app.state.v3_mcp_bridge.definitions:
    print(d.name, d.endpoint,
          '只读' if d.endpoint not in v3_mcp.NON_READONLY_PATHS else '写入')"
```

下表在**两种表面模式**下都成立——表里列的是**能力**（`call_tool` 可达集合 ≡ 这个集合）。
「本模式是否直连」一列说明它出现在 `/mcp` 的 `tools/list` 里的方式：

| 直连口径 | 含义 |
|---|---|
| `direct` | `tools/list` 里就有这件工具（表面模式 `direct`） |
| 代理 | `tools/list` 里只有 `list_tools`/`call_tool`；这件工具经 `call_tool` 转发（表面模式 `discovery`，**默认**） |

#### 1.5.1 两种表面模式：成本对比（规格 FR-TOOLS-003 / §10 决策 3）

```bash
# 测量脚本（两种模式各装配一次真应用，走真 MCP 协议取 tools/list；只读，不碰线上 8397）
cd platform && ~/.dsh/trading-venv/bin/python -B tools/mcp_surface_report.py
# 机器可读：加 --json
```

本 worktree 实测（`tools/list` 的 `json.dumps(tools)` 字符数；token 按 4 字符/token
**估算**——真实分词器不在依赖里，**字符数是硬数字**）。**`direct` 的条数会随并行开发演进**
（`/api/v3/*` 路由新增：v3_sdk / v3_headless / v3_alerts…），因此下表给的是实测区间与
比值；`discovery` 恒为 6（4 件直连保留 + 两个入口，与路由数无关）：

| 模式 | `tools/list` 条数 | 字符数 | ≈token | 说明 |
|---|---|---|---|---|
| `direct` | 77 + 全部 `v3_*` 桥接件（实测 **118 → 125**，随路由表涨） | **79,624 → 86,930** | **≈19.9k → 21.7k** | 全量直暴露，向后兼容 |
| `discovery`（**默认**） | **6** | **3,248** | **≈812** | 省 **≈96%**（保留 3.7%~4.1%） |

discovery 模式**多花的那一次往返**：`list_tools` 默认一页 20 张卡片 = 5,081 字符
≈1,270 token，命中总数与分页信息一起返回（`has_more` / `next_offset`）。
因此单轮净收益 ≈18.3k~20.5k token；**要连续检索 15 页以上（300+ 件）才会把省下的吃回去**，
而工具面总量只有一百多件。

**取舍（诚实写清）**：省的是 **context**，多的是 **往返**。

* 首轮少 ≈19k token，且这个开销在 `direct` 下是**每一轮**的固定成本（工具 schema 进
  系统提示词）；长会话里这是持续收益。
* 代价是要多做 1~2 次工具往返（先 `list_tools` 检索、再 `call_tool` 调用），且模型必须
  先知道「有 `list_tools` 这个东西」——所以发现代理必须**直连保留**（见下）。
* 一轮里要用的工具越多，`direct` 的相对劣势越小；但要连续用几十件不同工具的场景在
  研究/取数工作流里是少数，且那种场景下 `direct` 的 2 万 token 仍然是每轮成本。
* 结论：**默认 `discovery`**，需要弱模型/排障/极限少往返时切 `direct`（一行环境变量）。

#### 1.5.2 discovery 模式直连保留哪 4 件、为什么

| 直连保留 | 为什么留它 |
|---|---|
| `snapshot` | 工作台全量快照：一次调用看清账户模式/在途/缺口——「账户面探活」 |
| `admin_status` | 本地运行台账与维护状态——「本地服务面探活」 |
| `v3_gateway` | 通道状态（mcp/sdk/headless）、调度心跳、作业历史——「通道面探活」 |
| `v3_tools` | 六域工具**目录**（data/alpha/ml/risk/execution/ecosystem）——「能力面速览」 |

四件都是只读、无副作用的「开胃菜」，合起来覆盖「先探活再看详情」的四个方向；合计 schema
约千级字符（6 件共 3,248 字符含两个代理入口）。反例：`series` / `positions` / `trade_place`
这类高频**业务**工具不直连——数量多、schema 大，且调用前本就该先检索（一次 `list_tools`
就能拿到必填参数名）。

#### 1.5.3 发现代理能不能绕过约束？——不能（同一份实现 + 同一套闸门）

* **同一份实现**：`call_tool` 转发的是**注册进 MCPServer 的同一个函数对象**
  （基础面 `BoundTool.fn`、桥接面 `v3_mcp.V3Bridge.bound[name]`），不是同源代码——
  `platform/tests/test_mcp_discovery.py` 用 `is` 同一性 + 同一实参响应的逐字段相等断言。
* **凭据封死依然生效**：`v3_credentials` 的 `save`/`clear` 封死在
  `V3Bridge.__call__`（`v3/credentials-web-only`），**先于 endpoint 判定**；经 `call_tool`
  转发时同样零调用 handler、零磁盘写入（测试里两种路径各断言一次）。
* **无交易写能力**：`/api/v3/*` 面本来就没有下单/改单/撤单/切模式/执行计划端点，代理按
  目录转发，因此**不可能凭空造出**交易能力；`tools/list` 与代理目录都不含
  `confirm_decide`（人工批准通道）。
* **未知工具不静默**：`call_tool(name='不存在')` → `mcp/unknown-tool`（isError=false 的
  业务失败信封）+ 「请先用 list_tools 检索」提示；实参名/必填不对 → `mcp/bad-arguments`。

#### 1.5.4 怎么切换（一行）

```bash
# 平台服务进程读这个环境变量（缺省 discovery）
QUANT_MCP_SURFACE=direct scripts/platform_service.sh restart
# 部署级缺省也可写进 <DSH_HOME>/trading-platform.json：
#   {"service": {"mcp_surface": "direct"}}
# 优先级：create_app(mcp_surface=…) > service.mcp_surface > QUANT_MCP_SURFACE > 缺省 discovery
# 非法取值直接报错（不静默退回某个模式）
```

profile 侧（`platform/install/quant-headless/cordis.patch.yml` 的 `quant-platform-mcp` 行）
不需要改：两种模式都是同一个 `/mcp`。详见 `platform/install/quant-headless/README.md`
的「一之补：工具面模式」。

#### 1.5.5 `v3_*` 工具表

| 工具 | 路由 | 只读 | 入参 | 本模式是否直连（`discovery`） |
|---|---|---|---|---|
| `v3_audit` | `/api/v3/audit` | 只读 | window | 代理 |
| `v3_brain` | `/api/v3/brain` | 只读 | market | 代理 |
| `v3_credentials` | `/api/v3/credentials` | 写入（写动作封死） | action,key,value | 代理 |
| `v3_events` | `/api/v3/events` | 只读 | ticker,window,days | 代理 |
| `v3_execution` | `/api/v3/execution` | 只读 | market | 代理 |
| `v3_execution_quality` | `/api/v3/execution/quality` | 只读 | market,mode | 代理 |
| `v3_factors_matrix` | `/api/v3/factors/matrix` | 只读 | tickers,factor,forward_days,forward,market | 代理 |
| `v3_financials` | `/api/v3/financials` | 只读 | ticker,statement,periods | 代理 |
| `v3_gateway` | `/api/v3/gateway` | 只读 | - | **直连保留** |
| `v3_market` | `/api/v3/market` | 只读 | ticker,period,limit | 代理 |
| `v3_market_watchlist` | `/api/v3/market/watchlist` | 只读 | n,market | 代理 |
| `v3_markets_calendar` | `/api/v3/markets/calendar` | 只读 | markets,now | 代理 |
| `v3_markets_calendar_refresh` | `/api/v3/markets/calendar/refresh` | 只读（只写缓存） | markets,horizon_days | 代理 |
| `v3_metrics` | `/api/v3/metrics` | 只读 | - | 代理 |
| `v3_metrics_probe_refresh` | `/api/v3/metrics/probe/refresh` | 只读（只写缓存） | markets,limit_pct | 代理 |
| `v3_ml_backtest` | `/api/v3/ml/backtest` | 只读 | ticker,window,rebalanceDays,limit,market | 代理 |
| `v3_ml_models` | `/api/v3/ml/models` | 只读 | market,ticker,window,horizon,limit,cost_bps | 代理 |
| `v3_ml_sweep` | `/api/v3/ml/sweep` | 只读 | ticker,windows,rebalance,limit,market | 代理 |
| `v3_news` | `/api/v3/news` | 只读 | symbol,limit | 代理 |
| `v3_oms_orders` | `/api/v3/oms/orders` | 只读 | market | 代理 |
| `v3_oms_sync` | `/api/v3/oms/sync` | 写入（改本地台账） | - | 代理 |
| `v3_openbb` | `/api/v3/openbb` | 只读 | symbol | 代理 |
| `v3_orderbook` | `/api/v3/orderbook` | 只读 | ticker | 代理 |
| `v3_overview` | `/api/v3/overview` | 只读 | market | 代理 |
| `v3_plates` | `/api/v3/plates` | 只读 | market,plate_class | 代理 |
| `v3_research` | `/api/v3/research` | 只读 | market | 代理 |
| `v3_research_report_pdf` | `/api/v3/research/report.pdf` | 只读 | id,ticker | 代理 |
| `v3_research_tasks` | `/api/v3/research/tasks` | 只读 | market | 代理 |
| `v3_risk` | `/api/v3/risk` | 只读 | - | 代理 |
| `v3_risk_analytics` | `/api/v3/risk/analytics` | 只读 | limit,confidence,benchmark,weights,market | 代理 |
| `v3_risk_industry` | `/api/v3/risk/industry` | 只读 | tickers,market,limit_pct | 代理 |
| `v3_sentiment` | `/api/v3/sentiment` | 只读 | symbol,market,days,limit | 代理 |
| `v3_settings` | `/api/v3/settings` | 只读 | - | 代理 |
| `v3_sources_status` | `/api/v3/sources/status` | 只读 | keys | 代理 |
| `v3_spot` | `/api/v3/spot` | 只读 | limit | 代理 |
| `v3_strategy` | `/api/v3/strategy` | 只读 | market | 代理 |
| `v3_strategy_run` | `/api/v3/strategy/run` | 写入（落盘研究轮） | topN,window,market,universe | 代理 |
| `v3_tools` | `/api/v3/tools` | 只读 | domain | **直连保留** |
| `v3_tushare` | `/api/v3/tushare` | 只读 | api,ts_code,period,limit | 代理 |

**数量口径（五个不同的东西，别混；下方每个数字都附本轮实测命令）**

| 口径 | 数量 | 是什么 | 本轮实测怎么数出来的（2026-09-20，服务 `http://127.0.0.1:8397`） |
|---|---|---|---|
| `/api/wb/*` HTTP 端点 | **82** | 既有工作台工具面的 HTTP 端点总数（`snapshot.endpoints` 声明的那一份） | `curl -s -X POST /api/wb/snapshot -d '{}'` → `value.endpoints` 长度 = 82 |
| MCP `/mcp` 工具（**取决于表面模式 `QUANT_MCP_SURFACE`**） | `direct` = **工作台 77 + 全部 `v3_*` 桥接件**（本 worktree 125，随路由表涨）；`discovery`（**默认**）= **6** | 表面模式决定 `tools/list` 条数；两种模式**能力集合相同**（discovery 经 `call_tool` 间接可达） | `cd platform && ~/.dsh/trading-venv/bin/python -B tools/mcp_surface_report.py`（两模式各走真 MCP 协议量一次） |
| 同上，**schema 成本**（进系统提示词的那一份） | `direct` **86,930 字符**（≈21,732 token @4 字符/token）；`discovery` **3,248 字符**（≈812 token） | 工具名 + 描述 + 完整 inputSchema 的 JSON 文本长度 | 同上脚本；**字符数是硬数字**，token 是估算（真实分词器不在依赖里） |
| `/api/v3/*` HTTP 路由（源码快照） | **41 → 48**（本 worktree；随并行开发演进） | FastAPI 上登记的 V3 路由**路径**条数 = `v3_*` 桥接工具数 | `grep -rhoE '@app\.(get\|post)\("/api/v3[^"]*"' platform/server \| sort -u \| wc -l`；**测试里不写死这个数**（期望值一律由路由表推导） |
| 六域工具目录条目（`/api/v3/tools`、`/metrics.toolTotal`、`/api/v3/gateway.channels.mcp.tools_domain_catalog`） | **82** | 工作台 77 工具 + 5 个 V3 本地计算（`v3_ops.V3_LOCAL_TOOLS`）——**这是「工具目录」不是 HTTP 端点，也不随表面模式变** | `curl -s /api/v3/tools \| jq .total` = 82；域分布 data 32 / alpha 7 / ml 2 / risk 4 / execution 18 / ecosystem 19 |
| `/metrics` 分口径 | `scope="mcp"` = **当前模式的实际条数**（direct 随路由涨 / discovery 6）/ `scope="domain"` **82** | 桥接后用 Prometheus 标签区分两个口径（HELP 已写明不可相加/替代）；`mcp` 真读注册表，随表面模式变 | `curl -s /metrics \| grep '^quantwb_tools'` |

**为什么两个「82」不是同一个东西**：`/api/wb/*` 的 82 是**工作台 HTTP 端点表**的行数；
六域工具目录的 82 是**目录条目**（77 个工作台工具 + 5 个 V3 本地计算工具，其中
`run_backtest` / `param_sweep` / `strategy_run` / `calc_var` / `search_news` 没有对应的
`/api/wb/*` 端点）。两者数值相同纯属巧合，**不可互相替代**。
桥接工具是同能力的 MCP 出口，不重复计入六域目录。
**注意 `channels.mcp.tools` 的语义已更正**：它现在等于 `tools_total`（MCP 注册表真值，
随表面模式变：`direct` 随路由表 / `discovery` 6），
六域目录口径请读 `tools_domain_catalog`（82）；`/api/v3/metrics` 同时给
`toolTotal=82`（目录）与 `mcpToolTotal`（MCP 面，当前模式的实际条数）。
两者不可相加/替代；`mcpToolTotal` 随 `QUANT_MCP_SURFACE` 变，`toolTotal` 不变。
历史文档曾出现「56 工具」的写法，本轮按上表实测值统一。
覆盖性与数量由 `platform/tests/test_mcp_parity.py` 钉死（见 §六）。

### 1.6 为什么不用第三方 adapter —— 弃用证据（三段真实报错）

规格 `FR-GATEWAY-001` 曾点名 `@helibeiqi/dsh-cordis-universal-adapter` 作 Harness 侧双向桥。
**结论：该包在本部署（dsh `0.1.5-rc.2`）不可用**，三段错误都在隔离的临时 `DSH_HOME`
（未碰线上 `~/.dsh`）里复现过，且都是真实运行输出，不是配置疏忽：

```console
# 故障 1：缺 pnpm-workspace.yaml → 安装直接失败（pnpm 默认 autoInstallPeers 去 registry 装 peer）
$ DSH_HOME=$TMP dsh plugin --profile quant-headless add @helibeiqi/dsh-cordis-universal-adapter
ERR_PNPM_NO_MATCHING_VERSION  No matching version found for
  @deepseek-ai/dsh-tools@>=0.1.0 while fetching it from https://registry.npmjs.org/
The latest release of @deepseek-ai/dsh-tools is "0.0.1-rc.1".
Other releases are: * next: 0.1.5-rc.2   * alpha: 0.1.6-alpha.2

# 故障 2：补上 pnpm-workspace.yaml 后能装（+15 包），但 profile 直接起不来
#          —— 它自带的 cordis.patch.yml 里的行名**未加 scope**
$ DSH_HOME=$TMP dsh --profile quant-headless "hi"
Error: dsh: plugin tree failed to load: failed to apply loader entry include (cordis:include):
  failed to import loader entry dsh-cordis-universal-adapter (dsh-cordis-universal-adapter):
  Cannot find package 'dsh-cordis-universal-adapter' imported from …/profiles/quant-headless/
Error [ERR_MODULE_NOT_FOUND]: Cannot find package 'dsh-cordis-universal-adapter'
#   （实测：用户层 patch 改不掉已存在行的 name，只有 config/disabled 会被覆盖 → 该修法无效）

# 故障 3：按它 README 手动 insert（显式 scoped 包名）后行名解析通过，仍导入失败
$ DSH_HOME=$TMP dsh --profile quant-headless --patch /tmp/adapter-manual.yml "…"
Error: dsh: plugin tree failed to load: failed to apply loader entry include (cordis:include):
  failed to import loader entry dsh-cordis-universal-adapter (@helibeiqi/dsh-cordis-universal-adapter):
  The requested module '@deepseek-ai/dsh-llm' does not provide an export named 'CallId'
#   逐字核实：dsh-llm 0.1.5-rc.2 只导出 ToolCallId；该包 head -1 lib/mcp-server.js 是
#   `import { CallId } from '@deepseek-ai/dsh-llm';`（旧名）。其 engines 只约束 node>=22.19，
#   没有 dsh 版本区间，破坏性改名没有任何声明层面的拦截。
```

**替代 = 平台已经用的那条路，且本轮实测通过**：平台侧把工具面以 **MCP 服务器 + `/mcp`**
暴露，Harness/profile 侧用官方 **`@deepseek-ai/dsh-mcp-client` 一行**挂载
（`platform/install/quant-headless/cordis.patch.yml` 的 `quant-platform-mcp`）。
**双方交互只此一条，不挂任何第三方 adapter/桥接包**；该行已装在共享目录
`~/.dsh/profiles/node_modules/@deepseek-ai/dsh-mcp-client`，无需额外装包。
需要用 adapter 的话，得等包作者发一版对齐 dsh 0.1.5-rc.2 的适配（至少：patch 行名带 scope、
改用 `ToolCallId`、声明 dsh 版本区间）。

---

## 二、数据源状态总览

| 数据源 | 用途 | 当前状态 | 需要密钥 | 接口（Agent 侧一律走 MCP，下表的 HTTP 路径是平台前端/运维口径） |
|---|---|---|---|---|
| 工作台工具面（77 工具 + 全部 `v3_*` 桥接件） | 行情/持仓/计划/审计/事件/调度/研究/风控 | ✅ 可用 | 否 | `POST /api/wb/<tool>`、`GET /api/v3/*`、**MCP `/mcp`**（两种表面模式，见 §1.5.1） |
| 富途 OpenAPI / OpenD | 行情与交易通道 | ✅ 已配置（appkey 模式） | appkey（已配） | MCP `v3_settings`（等价 `GET /api/v3/settings`） |
| 富途实时行情权限 | 盘口五档、板块涨跌幅 | ❌ 权限缺口（`errcode=-9 realtime quote permission required`） | 券商侧开通 | MCP `v3_orderbook` / `v3_plates`；页面标注「无数据源」，不填占位 |
| AKShare | A 股新闻/另类数据 | ✅ 可用（免密钥） | 否 | MCP `v3_news`（等价 `GET /api/v3/news`） |
| SEC EDGAR | 美股财报三表（XBRL） | ✅ 可用（免密钥） | 否（需可识别 User-Agent） | MCP `v3_financials` |
| Tushare Pro | A 股财务/行情 | ⏳ **等待注入（页面可配）** | `TUSHARE_TOKEN` 或页面保存 | MCP `v3_tushare`、`v3_credentials`（只读状态/测试） |
| OpenBB | 美股基本面（备选） | ✅ 已安装可用（实测 AAPL 指标） | 视数据商而定 | MCP `v3_openbb` |

> **SEC EDGAR：文档/接口/实现三方口径不一致（按实测写，接口侧待后端修）**
>
> 上表按**实测**写 ✅ 可用：`curl -s "http://127.0.0.1:8397/api/v3/financials?ticker=AAPL&statement=income&periods=2"`
> 返回 `{"ok":true,"source":"sec/companyconcept(us-gaap XBRL)","chain":[{"source":"sec/companyconcept(us-gaap XBRL)","ok":true,...}]}`
> ——即 `v3_financials` 走的是真实 SEC companyconcept XBRL 面，**不是占位**。
>
> 但 `GET /api/v3/settings` 的 `data_sources` 里 SEC EDGAR 仍被**写死**为
> `available:false` + `"无数据源：本服务未实现 SEC EDGAR 客户端"`（`platform/server/v3_ops.py`
> 的设置页数据源清单），于是「接入与授权」页会显示「不可用」，与实测和本文档矛盾。
> 这是**后端字段的事实性错误**（连同 Tushare 的可用性判据要求 `tushare` **包**可导入，
> 而实现走 HTTP `http://api.tushare.pro`、并不需要该包），
> **由主 agent 在 `platform/server/**` 侧修**；本轮（前端 + 文档）只把口径按实测写死并登记矛盾，
> 不动后端。修好后本文档与页面会同时转正。

## 三、需要你提供的东西（按优先级）

### 1. `TUSHARE_TOKEN`（唯一必须的密钥）—— **可在页面上配置**

**方式 A（推荐，无需碰命令行）**：打开 `#/settings`（接入与授权）→「密钥与授权」卡片 →
粘贴 token → **保存** → 点 **测试连通性**（真实调用 Tushare `trade_cal`，返回延迟与上游消息）。

- 凭据落 `<DSH_HOME>/v3-credentials.json`，**0600**，原子写；
- 页面与服务端**都不回显**凭据值，只显示「已配置/未配置 + 来源 + 掩码尾号（…abcd）」；
- 保存后**立即生效**（无需重启）；**环境变量 `TUSHARE_TOKEN` 优先级高于页面配置**；
- 「清除页面配置」只删文件里的值，环境变量不受影响；
- 未配置时 `/api/v3/tushare` 与「测试」按钮都返回 `tushare/no-token`，**不发任何请求**；
- **Agent 侧不能写这个值**：MCP 工具 `v3_credentials` 只提供 `status` / `test`，
  `save` / `clear` 一律返回 `v3/credentials-web-only`（人工 Web 动作，见 §1.2）。

**方式 B（部署方）**：环境变量注入后随服务生效：
```bash
export TUSHARE_TOKEN=你的token
cd platform && ~/.dsh/trading-venv/bin/python -m server.run
```

**接口层验证**（页面同款动作；Agent 侧请改用 MCP 的 `v3_credentials` / `v3_tushare`）：
```bash
curl -s -X POST localhost:8397/api/v3/credentials -H 'content-type: application/json' \
  -d '{"action":"save","key":"tushare_token","value":"你的token"}'
curl -s -X POST localhost:8397/api/v3/credentials -H 'content-type: application/json' \
  -d '{"action":"test","key":"tushare_token"}'
curl -s "localhost:8397/api/v3/tushare?api=income&ts_code=600519.SH&period=20260630"
```
- 消费页面：`接入与授权`（密钥与授权卡片 + 统一授权中心）、`行情与信号`。

### 2. 富途 OpenAPI 凭据与 OAuth 授权 —— **已在页面上可用**
- 位置：既有工作台 **设置页**（`#/settings`）——「富途授权」卡片 + **OAuth 2.1 + PKCE 授权面板**
  （Client ID 可空=自动注册 → 开始授权 → 2s 轮询 → 服务端落盘 `mode=oauth`，0600）。
  对应接口：`/api/wb/openapi_config`（AppKey 模式读写）、`/api/wb/openapi_test`（真实连通性）、
  `/api/wb/openapi_oauth`（start/status/cancel）——**三者有意不在 MCP 工具面**（凭据管理是人工动作）。
- V3 接入与授权页的「密钥与授权」卡片给出状态与**入口按钮**（跳转到该页），不重复实现第二套授权。
- OAuth 需要本机回调端口 `http://localhost:<port>/callback`，因此必须在能访问该回调的机器上操作。

### 3. 富途实时行情权限（券商侧，非密钥）
- 现盘口/板块涨跌幅走不到，报 `errcode=-9`；开通后无需改代码，`v3_orderbook` 与板块行情即可返回。
- 消费页面：`行情与信号`（盘口五档卡、板块热力）。

### 4. OpenBB（可选依赖）
- 若需要：`~/.dsh/trading-venv/bin/pip install openbb`（体积较大，且需与 Python 3.13 兼容）。
- 未安装时 `v3_openbb` 返回 `openbb/unavailable`，**不发请求**。

## 四、前端：平台 UI 自身仍走 HTTP（Agent/调度侧不走）

V3 工作台是 `platform/web-pro`（Vite + React18 + antd5 + @ant-design/pro-components）的构建产物，
由 FastAPI 在**根路径**直接服务：

```bash
cd platform/web-pro && npm run build        # 产物 dist/（改页面后必须重新构建）
cd platform && ~/.dsh/trading-venv/bin/python -m server.run   # http://127.0.0.1:8397
```

- 入口：`http://127.0.0.1:8397/` → 9 个页面走 hash 路由 `#/<key>`
  （overview/brain/market/strategy/risk/execution/gateway/tools/settings）
- 菜单分组与设计稿导航一致：监控 · 研究 · 交易 · 系统
- **平台前端自己的两条通道**：读 `GET /api/v3/*`、写 `POST /api/wb/*`（受约束入口）；
  这是**平台前端内部**的实现细节——**Agent/调度侧一律走 MCP**（`/mcp`），不直连 REST。
  同一份能力两条出口不会产生第二个事实源：`/api/v3/*` 与 `v3_*` MCP 工具调的是同一个
  handler 函数对象（§1.1）。
- 缺失数据源显式标注，无占位数据
- 历史 URL `/v3/*`、`/pro/*` 已随旧版删除，但静态兜底会回落工作台首页，不会死链

## 五、页面与接口对应

| 页面（hash 路由） | HTTP 接口（前端用） | MCP 工具（Agent 用） |
|---|---|---|
| `#/overview` 系统概览 | `/api/v3/overview` | `v3_overview` |
| `#/brain` 决策大脑 | `/api/v3/brain` | `v3_brain` |
| `#/market` 行情与信号 | `/api/v3/market`、`/market/watchlist`、`/factors/matrix`、`/plates`、`/orderbook`、`/sentiment` | `v3_market`、`v3_market_watchlist`、`v3_factors_matrix`、`v3_plates`、`v3_orderbook`、`v3_sentiment` |
| `#/strategy` 策略与因子 | `/api/v3/strategy`、`/factors/matrix`、`/ml/sweep`、`/ml/backtest`、`/ml/models` | `v3_strategy`、`v3_factors_matrix`、`v3_ml_sweep`、`v3_ml_backtest`、`v3_ml_models` |
| `#/risk` 风险监控 | `/api/v3/risk/analytics`、`/risk`、`/risk/industry`、`/oms/orders`、`/events`、`/execution/quality` | `v3_risk_analytics`、`v3_risk`、`v3_risk_industry`、`v3_oms_orders`、`v3_events`、`v3_execution_quality` |
| `#/execution` 执行与审批 | `/api/v3/execution`、`/oms/orders`、`/audit` | `v3_execution`、`v3_oms_orders`、`v3_audit` |
| `#/gateway` 网关与调度 | `/api/v3/gateway`、`/metrics` | `v3_gateway`、`v3_metrics` |
| `#/tools` 工具域治理 | `/api/v3/tools`、`/metrics` | `v3_tools`、`v3_metrics` |
| `#/settings` 接入与授权 | `/api/v3/settings`、`/metrics`、`/news`、`/financials`、`/tushare`、`/openbb` | `v3_settings`、`v3_metrics`、`v3_news`、`v3_financials`、`v3_tushare`、`v3_openbb` |

## 六、部署与自检

```bash
# 服务（构建产物在 platform/web-pro/dist，FastAPI 静态兜底直接打开）
cd platform && ~/.dsh/trading-venv/bin/python -m server.run     # http://127.0.0.1:8397

# 逐页验收（9 路由：无「示例」、无设计稿残留占位数字、关键模块命中、菜单分组可见、无崩溃/白屏）
bash platform/tools/verify_pages.sh http://127.0.0.1:8397

# MCP 覆盖性 + 真实 tools/call（不依赖网络：优先本地服务，旧进程/不可达则 ASGI in-process）
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_mcp_parity -v
```

`tests/test_mcp_parity.py` 断言四件事：**①** 每个 `/api/v3/*` 路由都有对应 MCP 工具且无孤儿
工具（含名字规范、只读标记、`additionalProperties:false` 封闭 schema）；**②** 既有 77 工具
名字/数量一字不动、与 `v3_*` 零重名；**③** 真实 `tools/list` 条数 = 77 + 桥接数，并对若干
只读工具做真实 `tools/call`（信封必须 `ok=true` 且带 `source`/`as_of` 时原样保留）；
**④** 「故意失败」自检——把少一件工具/多一件孤儿工具的输入喂给同一套断言函数，
必须抛 `ParityError`（证明断言不是恒真）。

## 七、诚实性约定（实现层面强制）

1. 任何取不到的字段：接口返回错误信封，**调用方显示「无数据源」+ 原因**（权限缺口/未注入/上游不可用/服务未挂载该通道），绝不用估算值或占位数字顶替。
2. 密钥永不回显：`v3_settings`（`/api/v3/settings`）的环境变量表只报「是否注入 + 来源」；凭据值在 MCP 面完全不可写。
3. 数据时点：外部数据源返回体一律带 `as_of` 与 `source`；SEC 财报额外带 `latestEnd/ageDays/stale`（申报结构变化会让旧标签停用）。
4. 交易边界：`/api/v3/*` 只读（三个写入例外见 §1.3）；下单/改单/撤单/切模式/执行计划仍只经工作台受约束入口与人工确认。
5. 通道边界：**Agent/调度侧与平台的所有交互只走 MCP**；平台前端自身用 `/api/v3/*` HTTP 是前端内部实现，不构成第二条 Agent 通道。
