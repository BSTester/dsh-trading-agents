# WP8 富途 OpenAPI 统一接入规格（完整交易链路 + 实时推送）

> 状态：按用户指令（2026-09-16）设计——「把富途 API 统一接入工作台，由工作台的 MCP 工具对接 Harness，完整覆盖官方提供的一切交易链路」。
> 前置：WP7（`7ef5049..32ec4fe`）：FastAPI 单进程服务、交易闸门（trade_* 工具）、服务内调度器、policy 写通道收窄。
> 全局约定见 `2026-09-14-platform-plan-index.md`。

## 一、三种开放能力对比与决策

| 能力 | OpenAPI（REST+WS） | 托管 MCP | SkillHub |
|---|---|---|---|
| 行情实时 | 快照/报价/买卖盘/当前K线/分时/逐笔 + **WS 推送** | 子集、轮询 | skills 封装 |
| 深度数据 | 财务/研究/估值/股东/卖空/经纪商/IPO/筛选/自选股/公司行为 | 部分 | 部分 skills |
| 交易 | 下单（LIMIT/MARKET/AUCTION/AUCTION_LIMIT/STOP/STOP_LIMIT/MARKET_IF_TOUCHED/LIMIT_IF_TOUCHED + 美股时段 + 多腿 MLEG + GTC）/改单/撤单/**二次确认（need_order_confirm→order-confirm）**/最大可买卖/订单/成交 | 子集 | 13 种订单类型（OpenD） |
| 推送 | **行情 WS + 交易 WS + 加密货币 WS** | 无 | — |
| 认证 | OAuth 2.1+PKCE（Bearer 2h+refresh）或 AppKey（Ed25519/RSA 签名） | OAuth | OpenD 本地 |
| 市场 | HK/US/SH/SZ/SG/JP/AU/CA/BMS | 子集 | 5 市场 |

**决策**：
1. **OpenAPI 为唯一权威通道**（REST `https://webapi.futunn.com` + WS `wss://webapi-quote|trade.futunn.com/ws`），集成进工作台服务端；
2. 托管 MCP（`mcp__futu__*`）降级为**可选只读研究通道**（preset 行 disabled 默认，未配置时不出现）；
3. SkillHub 不作为集成通道（其 OpenD/本地网关形态与单进程服务冲突），仅作能力与提示词对照；
4. 加密货币交易：接口预留、本期不实现（边界）。

## 二、目标架构

```text
独立工作台（FastAPI 单进程）
  ├ futu_openapi.py：REST 客户端（OAuth 2.1+PKCE / AppKey 签名；限频/重试/错误码映射）
  ├ futu_push.py：WS 行情/交易推送（订阅管理/心跳/事件→OMS/告警/UI）
  ├ 交易闸门（WP7 既有）：mode→风控→kill→业务确认(Web)→ broker=openapi place-order
  │    └ need_order_confirm=true → 业务确认批准后自动 order-confirm（两层确认合一）
  ├ 数据工具面扩展（快照/报价/买卖盘/K线/筛选/深度数据/自选股…按组映射为 quantwb 工具）
  └ 调度器作业扩展（资金流/异动等定时采集，可选）
Harness：quantwb 工具面（覆盖官方全链路）+ 可选 mcp__futu__* 只读研究
```

## 三、认证与凭据

- 首选 **OAuth 2.1 + PKCE**：`futu_auth.py` 扩展 `--openapi`：注册 OAuth client（持久化 client_id 到 `~/.dsh/futu-openapi.json`）→ 本地起 callback 监听（默认 `http://localhost:60355/callback`）→ 打印授权 URL 让用户浏览器完成 → 换 token → 存 `access_token/refresh_token`（0600）；`access_token` 过期自动用 refresh_token 刷新（不轮换）。
- 兼容 **AppKey**：`~/.dsh/futu-openapi.json` 存 app_key/私钥路径/算法（Ed25519/RSA-SHA256），签名原文 = `timestamp_ms\nMETHOD\npath\nquery\nsha256(body)`，头 `X-Api-Key/X-Timestamp/X-Nonce/Authorization(base64)`。
- 服务读凭据失败 → 通道不可用如实报错，不静默降级到 MCP（通道由配置显式选择）。

## 四、覆盖映射与工具面

REST 端点 → quantwb 工具（**分组平铺**，命名 `<域>_<对象>`；工具面预计 33 → **65±**，全部登记进 manifest 与锁定测试）：

| 域 | REST | 工具（示例） |
|---|---|---|
| 实时行情 | market-snapshot/stock-quote/order-book/cur-kline/rt-data/rt-ticker | `quote_snapshot`、`quote_book`、`quote_rt_*` |
| 基本数据 | basicinfo/trading-days/rehab/owner-plate/history-kline/market-state/search/economic-calendar | `info_basicinfo`、`info_trading_days`、`quote_history_kline_v2`… |
| 资金/板块/衍生品/筛选/IPO/自选股 | capital-flow(-history/-distribution)/plate-*/option-chain/stock-screen/ipo-list/user-security* | 对应 `flow_*`、`plate_*`、`deriv_*`、`screen_*`、`ipo_list`、`watchlist_*` |
| 深度数据 | statements/analyst-consensus/morningstar/valuation/dividends/holders/top-brokers/short | `fund_*`、`research_*`、`valuation_*`、`corp_*`、`holders_*`、`short_*` |
> **现状注记（2026-09-16）**：`rt_quote/rt_order_book/capital_flow/capital_flow_history/capital_distribution/option_expiration/option_chain/option_screen` 8 个端点已先行交付（托管 MCP 通道直通，commit `f668204`，服务 skills 已对接）。任务 2/3 实施 OpenAPI 后仅切换这 8 个端点的**后端**（MCP call_tool → REST），工具契约与前端零变化；切换时以通道探针对比两者输出差异后灰度替换。
>
| 交易 | place/modify/cancel/order-confirm/get-max-qty/open-orders/history-orders/order-details/today-deals/history-deals/get-accounts/get-funds/get-positions | WP7 的 `trade_*`/`account_*` 改接 OpenAPI；新增 `trade_confirm`、`trade_max_qty`、`orders_open/orders_history/orders_detail/deals_today/deals_history` |
| 推送 | WS quote/trade | 无独立工具：事件落 OMS/告警/推送缓存，经 `snapshot`/Web 实时可见 |
| 模拟交易/加密货币 | sim-trade 全链 / crypto | sim 走既有 WP7 路径；crypto 预留不实现 |

边界：不改 `trading_datasource.futu_mcp`（MCP 通道保留为 fallback 只读）；加密货币不实现；13 种订单类型以 REST `order_type` 8 枚举为准（SkillHub 的 TWAP/VWAP 等算法单不在 REST 面，如实标注）。

## 五、验收标准

1. 配置 OAuth 后：`factors-history` 等既有工具不变；新增行情工具可取真实快照（如 US.AAPL）；
2. `trade_place`（sim/live）走 OpenAPI 下单：返回 order_id；`need_order_confirm=true` 时 Web 卡片批准后自动 order-confirm，订单进入 broker 状态机；拒绝则不调 order-confirm；
3. WS 交易推送：下单/成交事件在 Web 实时可见且 OMS 状态迁移正确；心跳断线自动重连；
4. `mcp__futu__*` 行 disabled 时全量功能不回退（除 91 工具自由研究）；
5. 全量测试绿 + 新增工具面锁定测试绿。

## 六、执行偏差与覆盖缺口（2026-09-16 审查）

本节登记任务 2/3 落地后的**已知偏差、覆盖缺口与技术债**——都经源码/live 复核，不是猜测；后续任务按此收敛，不在本期暗中补做。

### 6.1 必修项（本批已修，2026-09-16）

| 项 | 事实 | 处置 |
|---|---|---|
| `capital_flow.section` 枚举族用错 | `futu_openapi.capital_flow` 复用 `RT_SECTIONS`（6 值，含 rt-data 专有的 `HK_DARK`/`OVERNIGHT`）；官方 `capital_flow_section` 只有 4 值 `NORMAL/FULL/PREMARKET/AFTERHOURS`。**live 复现**：`section=OVERNIGHT` → `-3 … allowed: [NORMAL, FULL, PREMARKET, AFTERHOURS]` | 新增独立常量 `OpenApiMarket.CAPITAL_FLOW_SECTIONS` 并在本地拒绝；错误消息列出 4 值；`RT_SECTIONS` 保持 6 值只给 `rt_data`。live 复跑：4 值全通、`OVERNIGHT`/`HK_DARK` 本地拒绝 |
| 列表参数被序列化成 Python repr | `query_string()` 缺 `doseq=True`：`rt_ticker(period=["BEFORE","AFTER"])` 出站为 `period=%5B%27BEFORE%27…%5D`。**live 复现**：`-3 … invalid value '['BEFORE', 'AFTER']'` | `doseq=True` 展开同名多值；`rt_ticker.period` 收紧为非空列表。live 复跑：`?num=2&period=BEFORE&period=AFTER` 通过。出站 URL 字符串断言见 `tests/test_wp8_market.py::OpenApiOutboundUrlTest` |
| `filter_expiration_cycles` 类型错 | 官方是**逗号分隔字符串**（网关 pattern `^(MONTH\|WEEK\|…)(,(…))*$`），原先按 list 透传。**live 复现**：list → `-3`；`"WEEK,MONTH"` → 通过 | 收紧为字符串：新增 `_csv_enum`/`EXPIRATION_CYCLES`，**list 一律本地拒绝**（不按文档拼接——拼接会掩盖调用方类型错），`""` 视为未提供，元素空白归一 |
| 5xx/429/非信封在下单路径被误标 `rejected` | `parse_envelope_meta` 对非信封抛 `OpenApiError(errcode=status)`，`_classify_openapi_error` 一律当业务拒绝 → OMS 落 `rejected`（**终态、无出边**），而这类响应「可能已到达券商」 | 新增 `UnexpectedResponse(OpenApiError)`；分类顺序 `OrderConfirmRequired` → `TransportError` → `UnexpectedResponse` → 业务 `OpenApiError`。前两者落 `unknown`（消息含「未知状态：先查询订单，勿重放」），只有真业务信封落 `rejected`；429 的 `Retry-After` 原值进消息与信封 `value.retry_after`。读路径同修：非信封 → `trading/futu-unavailable`（`trading/futu-error` 只留给真业务错误） |

### 6.2 覆盖缺口与登记项（未修，后续任务）

1. **工具面覆盖缺口（本期降级）**：`trade_place` 的工具字段只有 `symbol/side/qty/price/client_order_id`，且 `validate_order` 把 `side` 限为 `BUY/SELL`、`OpenApiBroker.place` 硬编码 `order_type=LIMIT + time_in_force=DAY`。客户端 `OpenApiTrade.place_order` 已支持 **8 种 order_type、4 种 side、美股 session、GTC、多腿 `order_class=MLEG`/`multi_leg_info`、`lot_type`、`remark`、`aux_price`** ——这些在工具面不可达。**明确为「本期降级」**：后续任务补工具字段（含 `validate_order` 与 `OPENAPI_TRADE_FIELDS` 同步扩面）。
2. **改单丢 `aux_price`**：官方 `PUT /orders/{id}` 接受 `aux_price`（触发价），但闸门 `_write_gated`→`validate_order`→`OpenApiBroker.modify` 全链没有该字段（只传 `exchange/qty/price`）。对**在富途客户端建的非限价单**（STOP/STOP_LIMIT/MARKET_IF_TOUCHED/LIMIT_IF_TOUCHED）无法正确改单（触发价沿用旧值）。闸门/工具层缺口，登记待补（源码注释见 `platform/server/trading.py::OpenApiBroker.modify`）。
3. **撤单 `need_order_confirm` 分支属未实测的防御分支**：官方撤单页**没有** `need_order_confirm` 字段；`OpenApiBroker.cancel` 的自动 `order_confirm` 分支是防御性实现，从未在真实通道触发过。真实通道若出现该分支，先以查单复核再定策略。
4. **`9e279af` 的 preset 行为变更（提交信息未说明）**：该提交把 `agent.cordis.yml` 的 `futu-keepalive` 行由 `disabled: true` 改为 `disabled: false`。事实：sim 模式在 `futu_channel=openapi` 下仍依赖 MCP 凭据（sim 路径走富途 MCP），因此保活行需要默认启用——**作为行为变更登记**（影响：默认部署多一条 10 分钟定时检查 + 会热重载组合；见 `docs/HANDOVER.md`）。
5. **锁定表计数硬编码（维护噪声/技术债）**：工具面 56 / 端点 52 这两个数硬编码在 `platform/server/mcp_tools.py`（`TOOL_COUNT = 56` + 文件头与注释里 5 处字符串）、`platform/server/app.py`（注释），以及 **7 个测试文件**各自断言 `56/52`（`test_wp6_mcp`、`test_wp6_service_approval`、`test_wp7_factors_query`、`test_wp7_futu_data`、`test_wp7_trading`、`test_wp8_market`、`test_wp8_trading`）。每次扩面都要逐处改；后续应改为从 `TOOLS`/`endpoints()` 派生。
6. **顺带修正（必修 2 核对出站 URL 时发现，live 已复现）**：`query_string` 曾把 `None` 序列化为字面 `None`（`capital_flow_history` 默认出站 `?start=None&end=None` → `-3 parameter 'start' does not match pattern '^\d{4}-\d{2}-\d{2}$'`）。现按「None = 调用方未提供」整条省略（与请求体 `_body` 同规），live 复跑默认路径通过。登记为**查询串序列化的行为修正**。

### 6.3 信封表述更正（仅注释，代码未变）

原模块头与交易段注释写「交易侧与行情侧同一信封」——**不准确**：交易是 `{"s":"ok","d":…}` / `{"s":"error",errcode,errmsg,…}`，行情是 `{"ret_code":0,"data":{…},"pagination":{…}}`（分页在信封顶层），`parse_envelope_meta` 一直同时兼容两者。现已改正注释并显式登记第三种情形 `UnexpectedResponse`（非信封/5xx/429）。

### 6.4 任务 6 收敛（2026-09-16，计划外补充）

用户「完整官方交易链路」要求新增的 WP8 任务 6 收敛了 6.2 的前两项并新增推送订阅管理面：

1. **工具面覆盖缺口 → 已修**：`trade_place` 补齐官方 place-order 全字段（`order_type` 8 枚举、
   `time_in_force{DAY,GTC}`、美股 `session`（市价单仅 RTH）、`aux_price`（触发类必填，证券 3 位
   小数）、`lot_type`（仅港股）、`remark`（≤64 字节）、`order_class`/`multi_leg_info`（MLEG）），
   `validate_order` 与 `OPENAPI_TRADE_FIELDS`/`TRADE_PLACE_FIELDS` 同步扩面；校验在闸门**字段
   校验层**（风控/确认之前），错误 `trading/invalid-operation` 且带官方允许值；`OpenApiBroker.place`
   不再硬编码 `LIMIT/DAY`（缺省值由适配层补，显式值逐键透传）。**sim 通道只支持限价当日单**，
   扩展字段如实拒绝（`trading/broker-unavailable`，适配层第二道守卫），不静默丢弃。
2. **改单丢 `aux_price` → 已修**：闸门/工具面/`OpenApiBroker.modify` 全链透传 `aux_price`
   （官方 `PUT /orders/{id}` 请求体无 `order_type`，故不引入类型语义）。
5. **锁定表计数 → 同步更新**：工具面 56 → **59**、端点 52 → **55**，7 个测试文件与
   `test_wp6_tables_lock` 的内嵌清单逐处同步（硬编码本身仍保留，改为派生属于独立技术债）。
   新增推送订阅管理面 3 端点/工具：`push_status`（TTL 0，与 `/healthz` 的 push 同形）、
   `push_subscribe`/`push_unsubscribe`（**非交易**：只改本地连接订阅意图，未启用如实返回
   `trading/push-unavailable`）。字段/通道边界登记在 [TOOL-LIMITS 第八节](../../TOOL-LIMITS.md)。

