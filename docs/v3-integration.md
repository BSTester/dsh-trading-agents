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

discovery 模式**多花的那一次往返**：`list_tools` 默认一页 20 张卡片 = **5,750 字符**
≈1,438 token（2026-09-21 给卡片加并发/渲染元数据**之前**是 5,081~5,085 字符 ≈1,271 token；
那一次改动只加不减，逐项账见 §1.7.3），命中总数与分页信息一起返回（`has_more` / `next_offset`）。
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

> ⚠️ **边界澄清（2026-09-21 起）**：本节说的「代理不能绕过约束」指**平台服务侧的既有闸门**
> （凭据封死、`/api/v3/*` 无交易写端点）；它**不**等于「代理面上的写工具不可达」——
> 全局 `/mcp` 的 `call_tool` 在缺省 `discovery` 面下仍可转发工作台写工具
> （`trade_place` / `research_tasks_claim` / …），这正是 1.5.5 只读面要解决的漏洞
> （真机后果见 `docs/e2e-and-data-gaps.md` §22.6）。决策 profile 的硬边界在 `/mcp/ro`。

#### 1.5.4 怎么切换（一行）

```bash
# 平台服务进程读这个环境变量（缺省 discovery）
QUANT_MCP_SURFACE=direct scripts/platform_service.sh restart
# 部署级缺省也可写进 <DSH_HOME>/trading-platform.json：
#   {"service": {"mcp_surface": "direct"}}
# 优先级：create_app(mcp_surface=…) > service.mcp_surface > QUANT_MCP_SURFACE > 缺省 discovery
# 非法取值直接报错（不静默退回某个模式）
```

profile 侧（`platform/install/*/cordis.patch.yml` 的 `quant-platform-mcp` 行）**已指向
`/mcp/ro`**（见 1.5.5），不受此开关影响。`QUANT_MCP_SURFACE` 只改**全局 `/mcp`** 的模式。
详见 `platform/install/quant-headless/README.md` 的「一之补：工具面模式」。

#### 1.5.5 只读面 `/mcp/ro`：两个 MCP 端点对照（2026-09-21 漏洞修复）

**问题**：缺省 `discovery` 面下，全局 `/mcp` 的 `tools/list` 只有 6 件，写/交易工具的唯一
到达路径是转发器 `call_tool`；而 Harness 侧白名单钩子按**工具名**匹配
（`mcp__quantwb__trade_place` 等 42 项），对 `mcp__quantwb__call_tool` 既不在名单里、也
**看不到被转发的内层名字**。2026-09-21T00:01Z 的一次线上 quant-headless 决策唤醒经它
**误领了 2 条值班队列任务**（`docs/e2e-and-data-gaps.md` §22.6 有 stderr 与 DB 行证据）。
不能全局禁掉 `call_tool`：官方 `headless` profile 的值班链
（`research_duty.sh` → `research_tasks_claim/report`）走的就是全局 `/mcp`。

**修法**：`app.py` 挂第二个 MCP 端点 **`/mcp/ro`**（同一实现、同一目录，表面恒为
discovery 形态），唯一差异是 `call_tool` 在转发**之前**按**注册表 annotations** 判定
（`readOnlyHint=true` 的内层工具 + 4 件直连保留件；不维护第二份名单）。两个决策 profile
（quant-headless / quant-sdk）的 `quant-platform-mcp` 已指向 `/mcp/ro`。

| | 全局 `/mcp`（值班链/人用） | 只读面 `/mcp/ro`（决策 profile 用） |
|---|---|---|
| `tools/list` | `QUANT_MCP_SURFACE` 决定（缺省 discovery = 6 件） | 恒 6 件（discovery 形态），目录与 `/mcp` **同一份** |
| `call_tool` 转发写类内层工具 | ✅ 可达（服务侧闸门只剩业务确认链） | ❌ `mcp/denied-by-policy`（handler 零调用，先于实参校验） |
| `call_tool` 转发只读 `v3_*` | ✅ | ✅ 响应与 `/mcp` 直连**逐字段一致**（同一函数对象） |
| 直连 4 件（snapshot/admin_status/v3_gateway/v3_tools） | ✅ | ✅（同一函数对象、同一份标注） |
| 非放行工具的检索卡片 | 无特殊标注 | `roCallable=false` + 「只读面不可调用」（防反复尝试） |
| 凭据 save/clear | 桥内封死（`v3/credentials-web-only`） | **双层**：先被 annotations 闸门拒，桥内封死仍在 |
| 鉴权 | token 中间件（`/mcp`） | 同一中间件按 `/mcp/` 前缀覆盖（无旁路） |

**放行判据的口径（为什么有些只读工具也调不了）**：判据现读注册表 annotations
（`mcp_discovery.registry_annotations`）。桥接 `v3_*` 写类（`v3_oms_sync` / `v3_strategy_run` /
`v3_sdk_prompt` / `v3_credentials`）标了 `readOnlyHint=false` → 拒；基础面工具**本来就不发布
annotations** → 一律拒（**fail-closed**：包括 `series` 这类事实上只读的工作台工具）。决策取数
走带只读标注的 `v3_*` 桥接件（`v3_market`/`v3_news`/`v3_events`/`v3_sentiment`/…），
`/mcp/ro` 的 `list_tools` 卡片 `roCallable` 字段是「能不能调」的权威判据。测试：
`platform/tests/test_mcp_discovery.py::ReadonlySurfaceTests`（写类拒绝 + spy 零调用 +
双面等价 + 卡片标注 + token 鉴权）。

#### 1.5.6 `v3_*` 工具表

> ⚠️ **本表是 2026-09-20 的快照（39 行）**，路由表此后继续增长：本 worktree 现为 **51 件**
> （新增 `v3_factors_registry` / `v3_sentiment` / `v3_strategies_event_study` /
> `v3_strategies_stat_arb` / `v3_ml_backtest` / `v3_ml_models` / `v3_ml_sweep` /
> `v3_ops_alerts` / `v3_ops_alerts_rules` / `v3_sdk_status` / `v3_sdk_sessions` / `v3_sdk_prompt` /
> `v3_headless_log` / `v3_headless_schedule` / `v3_macro` / `v3_northbound` /
> `v3_risk_funding_check` / `v3_sources_status` …），且 `v3_tushare` 行已随 Tushare 移除而去掉
> （见 `docs/v3-source-policy.md`）。**权威口径一律由注册表推导**（`v3_mcp.V3Bridge.definitions`），
> 条数与逐件判定用 §1.7 的实测命令现取，不要照抄本表。

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

### 1.7 工具面元数据：`isConcurrencySafe`、`renderable` 与 `format:"text"`

FR-TOOLS-002 的三条子规范落在**注册面**（`platform/server/mcp_tools.py`；判定表与既有渲染器
此后未改，收窄补齐只往里**追加**）：并发安全
判定表 `CONCURRENCY_UNSAFE_TOOLS`、渲染纯函数注册表 `TOOL_RENDERERS`、`format` 参数。
本节记录的是它们**有没有到达调用方**——`tools/list` 的 `_meta` 与 `list_tools` 的卡片，
以及 2026-09-21 补齐的那几处缺口（改动只在 `platform/server/mcp_discovery.py`；同日收窄补齐
把渲染扩到桥接面 `v3_*`，那一轮的改动面见 §1.7.2 末段：`v3_mcp.py` + `mcp_tools.py` 的
**追加段**）。

复现命令（两种面各装配一次真应用，**只读**，不碰线上 8397）：

```bash
cd platform && ~/.dsh/trading-venv/bin/python -B -c "
import asyncio, sys; sys.path.insert(0,'.')
from tests.test_mcp_discovery import build_offline_app
app = build_offline_app('discovery')
for server, label in ((app.state.mcp, '/mcp'), (app.state.mcp_ro, '/mcp/ro')):
    print(label)
    for t in asyncio.run(server.list_tools()):
        print('  ', t.name, t.meta)"
```

实测输出（本 worktree，2026-09-21）：

```console
/mcp
   snapshot {'quantwb.isConcurrencySafe': True}
   admin_status {'quantwb.isConcurrencySafe': False}
   v3_gateway {'quantwb.isConcurrencySafe': True}
   v3_tools {'quantwb.isConcurrencySafe': True}
   list_tools {'quantwb.isConcurrencySafe': True}
   call_tool {'quantwb.isConcurrencySafe': False}
/mcp/ro
   snapshot {'quantwb.isConcurrencySafe': True}
   admin_status {'quantwb.isConcurrencySafe': False}
   v3_gateway {'quantwb.isConcurrencySafe': True}
   v3_tools {'quantwb.isConcurrencySafe': True}
   list_tools {'quantwb.isConcurrencySafe': True}
   call_tool {'quantwb.isConcurrencySafe': True}
```

> 两个面的差异**只有 `call_tool`**：`/mcp` 的它可以转发写类内层工具 → `false`；
> `/mcp/ro` 的它被闸门限制在只读件内 → `true`（与它自己的 `readOnlyHint=true` 同一口径）。

#### 1.7.1 `quantwb.isConcurrencySafe` 的判据与逐值数量

判定**只有一份实现**，本模块不重推（`mcp_discovery.concurrency_meta`）：注册面带了
`readOnlyHint` 就照它（桥接 `v3_*` 与两个代理入口），不带的（基础面 77 件）退回
`mcp_tools.is_concurrency_safe`。

| 面 | 判定来源 | `true` | `false` | 合计 |
|---|---|---|---|---|
| 基础面（工作台 77 件） | `mcp_tools.CONCURRENCY_UNSAFE_TOOLS`（写账户模式 / 交易 / 推送订阅 / 值班队列状态机 / admin 维护面，按家族保守取 false） | **61** | **16** | 77 |
| 桥接面（`v3_*` 51 件） | `v3_mcp.NON_READONLY_PATHS`（写类：落盘研究轮 / 改 OMS 台账 / 改配置 / SDK prompt） | **47** | **4** | 51 |
| **合计** | — | **108** | **20** | **128** |

16 件 false（基础面）：`trade_place`、`trade_modify`、`trade_cancel`、`trade_max_qty`、
`plan_execute`、`switch_mode`、`push_status`、`push_subscribe`、`push_unsubscribe`、
`research_tasks_claim`、`research_tasks_report`、`admin_status`、`admin_runs`、
`admin_cancel_run`、`admin_cancel_stale`、`admin_prune_runs`。
4 件 false（桥接面）：`v3_credentials`、`v3_oms_sync`、`v3_sdk_prompt`、`v3_strategy_run`。

**覆盖面的实测口径（三种面的差别，别混）**：

| 面 | 条数 | 带 `_meta` | 说明 |
|---|---|---|---|
| `direct` 模式的 `/mcp` | 128 | **128** | 已闭环（2026-09-21，主 agent 收口）：`v3_mcp.register` 的 `server.add_tool(...)` 补 `meta=mcp_tools.concurrency_meta(endpoint not in NON_READONLY_PATHS)` → 51 件 `v3_*` 全覆盖，实测 **51/51 带 `_meta`，其中 47 true / 4 false**（写类：`v3_credentials`/`v3_oms_sync`/`v3_strategy_run`/`v3_sdk_prompt`） |
| `discovery` 模式的 `/mcp` | 6 | **6** | 本轮补齐：`v3_gateway`/`v3_tools` 保留件就地补 `_meta`，两个代理入口注册时带上 |
| `/mcp/ro` | 6 | **6** | 本轮补齐：此前**六件全无** `_meta`（`register_readonly` 重新注册时没带） |

**四件 `false` 的判定依据**（写入判定表，非保守猜测）：`v3_credentials`（写/清凭据，桥内封死但语义是写）、
`v3_oms_sync`（重写本地台账）、`v3_strategy_run`（落盘研究轮）、`v3_sdk_prompt`（起 Harness 子进程 + 落审计）。
其余 47 件为只读端点，无共享可变状态被本调用修改，可并行。

#### 1.7.2 `renderable` 与 `format:"text"`：同一工具，两种输出

`format` 是**可选响应形态参数**（缺省 `"json"` = 规范 JSON 信封，机器读；`"text"` = 人类可读
散文渲染）。覆盖面就是 `TOOL_RENDERERS` 注册表的**逐名并集**——当前 **14 件**：

| 面 | 件数 | 逐名 |
|---|---|---|
| 基础面（`mcp_tools` 段内 `@register_renderer`） | 5 | `snapshot`、`series`、`factors`、`ic`、`risk` |
| 桥接面（`v3_mcp` 2026-09-21 扩展，**同一个** `TOOL_RENDERERS`） | 9 | `v3_sentiment`、`v3_factors_matrix`、`v3_risk_analytics`、`v3_risk_industry`、`v3_strategy`、`v3_market`、`v3_orderbook`、`v3_research`、`v3_ops_alerts` |

非 renderable 工具**没有**这个字段，传了按未知参数拒绝（与 schema 封闭性同一后果）。渲染器是
`render(args, value) -> str` **纯函数**：同输入恒同输出，无时钟、无 I/O、无随机（`as_of`/时间戳
一律取自信封原文）。

**三处同源，没有第二套机制**：`v3_mcp._bind` 与 `mcp_tools._bind` 都只在
`mcp_tools.is_renderable(name)` 为真时把 `mcp_tools.format_param()` 追加进签名，响应侧都交给
`mcp_tools.render_tool_result`（它读的就是同一份 `TOOL_RENDERERS`）。因此
**两面**（`direct` 直连签名 / `discovery` 经 `call_tool` 转发）与**两门**（`/mcp`、`/mcp/ro`）
给出的是同一份渲染——`/mcp/ro` 的只读闸门只决定「能不能转发」，不改变渲染。

同一实参的两种输出（**真机数据**：信封取自 8397 线上 `v3_market`，渲染走本 worktree 的生产
渲染器；`format` 缺省 / `text` 两次调用只差这一个字段）：

```console
# format 缺省（json）——规范信封，机器读（线上 call_tool 原样）
$ call_tool(name="v3_market", arguments={"ticker":"SH.600519","period":"1d","limit":60})
{"ok":true,"data":{"ticker":"SH.600519","period":"1d","source":"futu/quote_history_kline",
 "as_of":"2026-09-21","count":60,"bars":[{"t":"2026-06-30","o":1187.0,"h":1195.67,…}, …]}}

# format="text"——同一份数据的人类渲染（纯函数：同输入恒同输出，无时钟/无 I/O）
$ call_tool(name="v3_market", arguments={"ticker":"SH.600519","period":"1d","format":"text"})
SH.600519 1d K 线：共 60 根（2026-06-30 → 2026-09-21；来源 futu/quote_history_kline · as_of 2026-09-21）
- 最新一根：t=2026-09-21 O=1259.0000 H=1259.9500 L=1250.8000 C=1252.5700 V=2501689
- 窗口首根：t=2026-06-30 C=1185.4900
- 窗口累计涨跌：5.66%（首→尾收盘）
```

薄的一层**适配**也在渲染里如实露出（同一 `/api/v3/orderbook` 的 A 股降级链形态）：档位单位、
上游来源与降级原因都写进散文，不把降级说成原生读数：

```console
$ call_tool(name="v3_orderbook", arguments={"ticker":"SH.600519","format":"text"})
SH.600519 贵州茅台 盘口：来源 tencent/qt.gtimg.cn · as_of 2026-09-21T11:59:30+00:00
- 买盘：1252.570×1 / 1252.560×15 / 1252.550×110 / 1252.500×24 / 1252.450×1 手
- 卖盘：1252.860×57 / 1252.970×1 / 1253.000×3 / 1253.120×1 / 1253.130×5 手
- 降级说明：futu 返回 -9（A 股无实时权限）→ 已降级到免密公开源
```

两条约定（都不靠「渲染器写得好不好」保证）：

* **空数据渲染成「无数据源·原因」，不是空串**（`render_no_data`）：九件桥接渲染器逐件覆盖
  （空 `bars` / 无资讯 `documents=0` / 空矩阵 / `run=null` / 空 `exposures` / 空 `alerts` /
  `ok=false` 信封），渲染函数返回空串时 `render_tool_result` 再回退规范 JSON，绝不返回空内容；
* **非 renderable 工具传 `format` 仍被拒**，两面措辞不同、后果同一：

  ```console
  $ call_tool(name="v3_plates", arguments={"market":"SH","format":"text"})   # discovery 面
  {"ok":false,"error":{"code":"mcp/bad-arguments",
   "message":"call_tool('v3_plates') 的实参不合法：不认识参数 ['format']。…"}}

  # direct 面同一实参 → schema 层拒绝（pydantic extra_forbidden），isError=true
  Error executing tool v3_plates: 1 validation error for v3_platesArguments
  format
    Extra inputs are not permitted [type=extra_forbidden, input_value='text', input_type=str]
  ```

**本轮修掉的一个真缺陷**：`format` 是 `mcp_tools._bind` 在**绑定期**追加进签名/schema 的，
`definition.params` 里没有它；而代理的实参白名单来自卡片参数名——于是
`call_tool(name="series", arguments={…,"format":"text"})` 一直被判野字段（直连面同一实参却是
合法的）。修法是让卡片在 renderable 件上补列 `format`（与 schema 同一谓词），并加了两条测试钉死
「卡片参数名集合 ≡ schema `properties` 集合（128 件逐件）」与「代理转发 `format:"text"` 真出散文」。

**2026-09-21 收窄补齐（桥接面）**：上面那条修的是「卡片比 schema 窄」，但桥接面 `v3_mcp._bind`
**根本没往 schema 里加 `format`**，所以 `v3_*` 一件都渲染不了——发现代理的卡片因此**正确地**
标 `renderable=false`（没有假承诺），代价是模型在 discovery 模式下最常用的那批工具（`list_tools`
检索出来的绝大多数是 `v3_*`）享受不到人类渲染。实测（本机 8397，**该进程仍是改动前的代码**，
要重启才会带上本节的行为；重启不在本轮动作内）：

```console
$ call_tool(name="v3_market", arguments={"ticker":"SH.600519","period":"1d","format":"text"})
{"ok":false,"error":{"code":"mcp/bad-arguments",
 "message":"call_tool('v3_market') 的实参不合法：不认识参数 ['format']。…"}}

$ list_tools(keyword="v3_market")   # 卡片：没有 renderable/formats，optional 里也没有 format
{"name":"v3_market",…,"optional":["ticker","period","limit"],…,"readOnly":true,"concurrencySafe":true}
```

补齐后（**本 worktree 装配**：真路由表 / 真桥 / 真 MCP 注册，只有 handle 是假的；与线上进程走
同一段 `v3_mcp._bind → render_tool_result` 代码路径）：

```console
$ call_tool(name="v3_market", arguments={"ticker":"SH.600519","period":"1d","format":"text"})
SH.600519 1d K 线：共 60 根（2026-06-30 → 2026-09-21；来源 futu/quote_history_kline · as_of 2026-09-21）
- 最新一根：t=2026-09-21 O=1259.0000 H=1259.9500 L=1250.8000 C=1252.5700 V=2501689
- 窗口首根：t=2026-06-30 C=1185.4900
- 窗口累计涨跌：5.66%（首→尾收盘）

$ list_tools(keyword="v3_market")   # 卡片：多出 renderable/formats，optional 里多出 format
{"name":"v3_market",…,"optional":["ticker","period","limit","format"],…,
 "renderable":true,"formats":["json","text"]}
```

改动面（都在本轮归属范围内）：`v3_mcp._bind` 追加 `format` 字段 + 响应侧走同一个
`render_tool_result`；`v3_mcp` 新增渲染段（9 条 + 若干共用小工具）；`mcp_tools` **只追加**三个
公开别名（`render_number` / `render_no_data` / `render_error_text`——桥接面与基础面共用同一份
数值/空数据/错误口径，不复制实现）。测试：`tests/test_mcp_discovery.py::BridgeRenderTests`
（9 条：卡片、direct schema、代理散文、空数据、野字段拒绝、`/mcp/ro` 同渲染、纯函数、空串零容忍、
鉴别力）与 `tests/test_mcp_parity.py::RouteParityTests`（`format` 三落点同源 + 封闭性/双射在
`format` 进 schema 之后重测）。鉴别力（mutation）实测：临时摘掉 `v3_market` 的
`@register_renderer` → `BridgeRenderTests` **7 条红**（卡片集合、direct schema、代理散文、
`/mcp/ro`、空数据、纯函数、鉴别力自身），还原即绿；临时去掉 `_bind` 里的 `is_renderable` 闸门
（`format` 无条件进 schema）→ 两个文件各红（`'format' unexpectedly found in … v3_audit`）。

#### 1.7.3 卡片上的三个字段（`list_tools` 的导航面）

discovery 模式下 122/128 件**不在** `tools/list` 上、拿不到 `_meta`，所以卡片必须自己回答
「能不能并发调 / 能不能 text 渲染」。本轮给 `ToolCard` 加了三个键（`mcp_discovery.ToolCard`，
**判定全部读出注册面，没有第二份清单**）：

| 卡片键 | 取值 | 来源 | 出现条件 |
|---|---|---|---|
| `concurrencySafe` | `true`/`false`（**恒在**） | 基础面 `mcp_tools.is_concurrency_safe`；桥接面与 `readOnly` 同一判据 | 每张卡片 |
| `renderable` | `true` | `mcp_tools.is_renderable` | 仅 renderable 件（其余缺键 = 不支持） |
| `formats` | `["json","text"]` | `mcp_tools.FORMAT_JSON` / `FORMAT_TEXT` | 仅 renderable 件 |
| （`optional` 里多出的 `format`） | 字段名 | 同上（与 schema 同集合） | 仅 renderable 件 |

`readOnly` 的口径不变（基础面不发布 `readOnlyHint` → 如实为 `null`，**不发明**）；
`concurrencySafe` 不是发明——基础面本来就发布这个判定（`/mcp` 的 `_meta` 就是它）。

**卡片变大的代价（如实测量）**：默认一页 20 张卡片的 `cards` 数组从 **4,609 → 5,114 字符**
（+505，**+11.0%**；一页 payload 含 note 后为 5,750 字符），其中 `concurrencySafe` 约
+26 字符/张；`renderable`/`formats`/`format` 只在 renderable 件上出现，几乎不占（默认一页 20 张
里当时 0~1 件；桥接面补齐后第一页仍恰好 0 件，**5,114 字符不变**）。全目录 128 件逐卡合计
**30,762 → 34,227 → 35,014 字符**（+3,465 为 `concurrencySafe`+卡片补 `format` 那一轮，
+787 为 9 件桥接渲染器带来的 `renderable`/`formats`/`format`）。取舍：**保留**
`concurrencySafe`（122 件工具的唯一元数据通道），**不给**非 renderable 件写
`renderable:false`/`formats:["json"]`（那是 120+ 行冗余）。

测试：`platform/tests/test_mcp_discovery.py::MetadataExposureTests`（9 条，含鉴别力用例
「临时登记一个渲染器 → 卡片必须跟着翻」，证明字段是推导而非誊抄）。

### 1.8 `skills/quant-research`：五阶段研究工作流如何被加载

**它是什么**：`skills/quant-research/SKILL.md`（2026-09-21 新建）把规格 FR-TOOLS-002 的
「Skill 层」补上——模型在需要时**自行加载**这条工作流，而不是把流程写进系统提示词。
内容 = PDAT→PAAT→PCPT→PRT→PET 五阶段（每阶段的输入 / 真实工具名 / 输出 / **验收**）
+ **禁写边界**（研究侧永不下单、永不启用策略；只读研究产物进研究页与规则候选池）。

**怎么被加载**（机制，可复核）：

1. DSH 的 `skill-filesystem` 插件按配置的根目录扫描，**根下的一层目录** +
   `<目录>/SKILL.md` 即一个技能（根目录下的 `<name>.md` 也算一个）；
2. 两个决策 profile 都配了同一个根（`platform/install/quant-headless/cordis.patch.yml`、
   `platform/install/quant-sdk/cordis.patch.yml`）：

   ```yaml
   - id: skill-filesystem
     config:
       customSkillDirs:
         - /home/penn/workspace/dsh-trading-agents/skills/
         - /home/penn/workspace/dsh-trading-agents/skills/futu-skills/
   ```

3. `skills/quant-research/SKILL.md` 与本仓库既有的 `quant-trading` / `research-institute`
   **同形同根**（都在 `skills/` 下一层），因此**不需要新增任何配置**就随 profile 挂载；
   `skills/futu-skills/` 单独列出来是因为它的子技能（`sentiment/` 等）还要再下一层。
4. front-matter 只有 `name` + `description` 两个必填字段（`name: quant-research` 与目录名一致）；
   **改技能文件不需要重启平台服务**——技能在会话开场按目录现扫。

**与其它技能的分工**（避免模型走错路）：`research-institute` 是「挖因子/提规则/巡检」的研究院
生产线，本技能是它内部那条**五阶段流水线**；`quant-trading` 是秒级信号快路径；
`trading-agents` 是 12 角色深度研报。

**技能内容里的工具名都经过核对**：`v3_factors_matrix` / `v3_factors_registry` / `v3_sentiment` /
`v3_financials` / `v3_market` / `v3_risk_analytics` / `v3_risk_industry` /
`v3_risk_funding_check` / `v3_strategy` / `v3_brain` / `v3_research` … 全部取自
`list_tools` 的**真实目录**（128 件，无手抄）。技能同时写明：只读面 `/mcp/ro` 调不了基础面
工具（`factors`/`ic`/`series`/`rules`… 一律 `mcp/denied-by-policy`），研究取数一律走带只读标注的
`v3_*` 桥接件——这条有实测拒绝信封为证（见 `skills/quant-research/SKILL.md` 的「第一件事」一节）。

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
