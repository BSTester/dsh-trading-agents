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
