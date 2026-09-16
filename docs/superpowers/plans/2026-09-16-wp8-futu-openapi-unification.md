# WP8 富途 OpenAPI 统一接入 实现计划

> **面向 AI 代理的工作者：** 必需子技能：superpowers:subagent-driven-development（推荐）逐任务实现，两阶段审查。步骤 `- [ ]` 跟踪。
> **规格**：`docs/superpowers/specs/2026-09-16-wp8-futu-openapi-unification.md`。全局约定见索引。三套测试保持全绿；提交 `feat(scope): 摘要`。

**目标**：富途 OpenAPI（REST+WS）成为工作台的唯一权威富途通道，quantwb 工具面完整覆盖官方交易链路（下单/二次确认/推送/深度数据）。

---

### 任务 1：OpenAPI 客户端与认证（`trading_datasource/futu_openapi.py`）

**文件**：新建 `plugins/datasource/python/trading_datasource/futu_openapi.py`、`futu_openapi_auth.py`；`scripts/futu_auth.py` 加 `--openapi`；配置 `~/.dsh/futu-openapi.json`；测试 `tests/test_wp8_openapi_client.py`（mock HTTP，离线）。

- [ ] 步骤 1：失败测试：①PKCE（code_verifier→S256 challenge）；②AppKey 签名原文构造（timestamp/method/path/query/sha256(body) 五段 `\n` 连接 + Ed25519/RSA base64）；③token 刷新（401→refresh→重试一次）；④限频/错误码映射（`{"s":"error","errcode":-1200,"need_order_confirm":true,"confirm_id":...}` → 结构化异常）；⑤凭据读写 0600。
- [ ] 步骤 2：实现 `OpenApiClient`（`request(method, path, query, body)`；OAuth Bearer 与 AppKey 双模式）；`futu_auth.py --openapi` 完成 OAuth 注册+本地 callback 授权+落盘。
- [ ] 步骤 3：三套绿；`git commit -m "feat(datasource): 富途 OpenAPI 客户端（OAuth2.1+PKCE/AppKey 签名）"`

### 任务 2：行情数据接入与工具面扩展（第一批）

**文件**：`futu_openapi.py` 行情方法；`platform/server/compute.py` 路由表；`app.py`/`mcp_tools.py`/锁定测试。

- [ ] 步骤 1：失败测试（mock runner）：market-snapshot/stock-quote/order-book/cur-kline/rt-data/rt-ticker/history-kline/basicinfo/trading-days/search/market-state → 各自端点/工具（命名与映射表按规格 §四）；TTL/shape/白名单登记。
- [ ] 步骤 2：实现；通道选择：`trading_platform.json` 的 `futu_channel: "openapi"|"mcp"`，openapi 优先、未配置凭据时该组工具返回 `trading/openapi-unavailable`（如实）。
- [ ] 步骤 3：三套绿；`git commit -m "feat(platform): OpenAPI 行情数据接入与工具面扩展"`

### 任务 3：交易链路统一（place/modify/cancel/order-confirm/订单/成交）

**文件**：`trading.py`（FutuBroker 的 live 分支改接 OpenAPI）；`app.py`；`mcp_tools.py`；测试。

- [ ] 步骤 1：失败测试（mock OpenAPI）：①`trade_place` live → `POST /api/v1.0/accounts/{acc_id}/orders`（8 种 order_type/时段/多腿参数透传）；②响应 `need_order_confirm=true` → 业务确认（Web 卡片）批准后自动 `POST .../orders/confirm`（confirm_id）——**两层确认合一**（用户批准即确认本单参数）；拒绝则不调 order-confirm；③modify/cancel/max-qty/open-orders/history-orders/order-details/today-deals/history-deals/get-accounts/get-funds/get-positions 映射；④模拟交易走 WP7 既有路径不回退。
- [ ] 步骤 2：实现；`FutuBroker.supports_live_write = True`（凭据配置时）；工具面数量同步。
- [ ] 步骤 3：三套绿 + sim 冒烟；`git commit -m "feat(platform): 交易链路统一至 OpenAPI（下单/二次确认/订单/成交）"`

### 任务 4：WebSocket 推送（行情 + 交易事件）

**文件**：新建 `platform/server/futu_push.py`；改 `trading.py`（事件→OMS/告警）、`execution.jsx`（实时徽章）、测试。

- [ ] 步骤 1：失败测试（mock WS 帧）：①鉴权→订阅→心跳保活→断线重连；②交易事件（下单/成交/状态）→ OMS 状态迁移 + 告警分级；③行情事件 → 进程内快照缓存（供 quote 工具命中）。
- [ ] 步骤 2：实现：服务 lifespan 启停 `PushClient`；订阅管理 API（Web/MCP 可加订阅）；交易事件与轮询对账互为兜底（推送丢失时 60s 兜底轮询仍在）。
- [ ] 步骤 3：三套绿；`git commit -m "feat(platform): 富途 WS 推送（行情/交易事件→OMS/UI）"`

### 任务 5：通道收尾（MCP 行降级、深度数据第二批、定时扩展可选、文档验收）

- [ ] 深度数据第二批工具（财务/研究/估值/股东/卖空/经纪商/IPO/自选股）按同一模式接入；可选定时作业（资金流异动）。
- [ ] `agent.cordis.yml`：`futu-mcp` 行注释改为「可选只读研究通道（默认 disabled）」并置 disabled: true；persona/RUNBOOK/README/architecture/HANDOVER 同步（权威通道=OpenAPI）。
- [ ] 验收记录（对照规格 §五 逐条）+ 人工清单留位；`git commit -m "docs: WP8 文档与验收记录"`

### 执行顺序

1 → 2 → 3 → 4 → 5 串行；每任务 TDD + 两阶段审查。真实网络验证（真实下单）仍属 P4 人工准入，不在自动化范围。

---

## 附录 A：WS 推送协议事实（2026-09-16 实抓官方文档，任务 4 实现依据）

**行情 WS**（`wss://webapi-quote.futunn.com/ws`）：
- 首帧鉴权：`{"action":"auth","data":{"auth_type":"appkey","credential_id":<AppKeyID>,"authorization":<sig>,"timestamp_ms":<ms>,"nonce":<n>}}`（OAuth 则 `auth_type:"oauth2"`, `authorization:"Bearer <token>"`）。
- **WS 签名原文与 REST 不同**：`{timestamp_ms}\n{nonce}\nWEBSOCKET\nws/auth`（单次连接只用一种鉴权方式；不复用 REST 五段原文）。
- 鉴权成功响应：`{"id","session_id","server_time"}`（`server_time` 微秒）。
- **定时刷新**：至少每 10 分钟、建议每 5 分钟发送 `{"action":"refresh","data":{...同鉴权字段，timestamp/nonce 必须为新值...}}`；超 10 分钟不刷新服务端主动断开；刷新成功 `session_id` 不变。
- 订阅/反订阅：`{"id","action":"subscribe|unsubscribe","quote":[...],"order_book":[...],"ticker":[...],"kline":[{"symbol","period","adjust"}]}`；应答 `{"id","code":0,"message":""}`（`code!=0` 为失败）。
- 推送类型：`QUOTE`/`ORDER_BOOK`/`TICKER`/`KLINE`/`BROKER_QUEUE`（港股衍生，无需订阅字段）/`MARKET_STATE`。
- K 线周期枚举含 1m/3m/5m/10m/15m/30m/60m/120m/180m/240m/1D/1W/1M/1Q/1Y；复权 `none|forward_exclude_dividend|forward_include_dividend|forward`。
- **无业务层 JSON 心跳**：用协议层 ping/pong + 库的空闲/超时检测，禁止发 `{"action":"heartbeat"}`。
- 断线：重连→重新鉴权→按本地订阅意图表重新订阅；首屏与断线补齐用 REST（snapshot/quote/order-book/cur-kline/rt-ticker）。

**交易 WS**（`wss://webapi-trade.futunn.com/ws`）：
- 鉴权同上（先鉴权后推送）；**已核对 `trade_event_push/auth.md`：帧格式与签名原文与行情 WS 完全同构**——`{timestamp_ms}\n{nonce}\nWEBSOCKET\nws/auth`；刷新要求同为「至少 10 分钟、建议 5 分钟」；鉴权成功响应同为 `{id,session_id,server_time}`。**鉴权成功后自动订阅该用户全部交易事件，无需发送订阅请求**。
- 事件类型（10）：`EVENT_NEW`/`EVENT_REPLACED`/`EVENT_CANCELED`/`EVENT_EXPIRED`/`EVENT_FILL`/`EVENT_NEW_REJECTED`/`EVENT_REPLACE_REJECTED`/`EVENT_CANCEL_REJECTED`/`EVENT_FILL_CORRECT`/`EVENT_FILL_CANCEL`。
- **关键约束**：① 断线期间事件**不补发** → 重连后必须经 REST 对账补齐；② 事件**不保证严格顺序** → 不得把推送顺序当作状态机唯一驱动（REST 查询/对账仍为事实来源）；③ 长连接需重连+重新鉴权；④ 需定时 refresh 维持连接。

**任务 4 实现要求（据上述事实）**：`platform/server/futu_push.py` 实现 QuotePushClient（鉴权/刷新/订阅幂等/重连/本地订阅意图表）与 TradePushClient（鉴权/自动接收事件/重连）；交易事件 → OMS 状态迁移 + 告警，但**对账兜底轮询保留**（推送丢失时仍能收敛）；事件仅作加速，不作为唯一事实源。DSH_WP8_SLOW 下提供真实连接冒烟（有凭据时）。

---

## WP8 验收记录（2026-09-16，任务 5）

### 提交链

| 提交 | 内容 |
|---|---|
| `e0f01fe` | 任务 1：OpenAPI 客户端（OAuth2.1+PKCE / AppKey 签名） |
| `3ca9f96` | 任务 2：行情接入 + 通道路由（工具面 → 50） |
| `e61d1f6` | 任务 1 审查修复：空 body 签名 / 传输异常收敛 / OrderConfirmRequired / Retry-After / 授权拒绝回调 |
| `9e279af` | 任务 3：交易链路统一（place/modify/cancel/order-confirm，工具面 → 56） |
| `44c23e3` | 附录 A：WS 协议事实登记 |
| `bacef14` | 任务 2/3 审查修复：capital_flow 枚举 / 多值查询串 / 非信封落 unknown |
| `8ef0302` | 任务 4：WS 推送（行情订阅 / 交易事件→OMS，含重连与对账兜底） |
| `72bb86e` | 服务优先加载仓库数据层（消除已安装副本滞后） |
| `850cd71` | 任务 6：交易能力完整暴露（8 订单类型/时段/GTC/多腿/触发价）+ 推送订阅面（工具面 → **59**） |
| （本提交） | 任务 5：通道收尾与文档验收记录 |

### 全量测试证据（先跑后抄，2026-09-16 本机）

- Python：`Ran 1090 tests … OK (skipped=5)`（`~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests`）
- Node：`# pass 66 / # fail 0`（`node --test tests/*.test.mjs`）
- Web：`# pass 199 / # fail 0`（`npm --prefix platform/web test`）
- Web 构建：`npm --prefix platform/web run build` ✓（vite build 成功产出 dist）
- 前端残留检查：`grep -rn "设计稿\|示例数据\|宁可\|窄门\|规格" platform/web/src` 零命中
- 工具面 **59**（54 端点工具 + 5 维护）、服务端点 **55**（锁定测试同步）

### 真实验证记录（有凭据实调）

- AppKey + Ed25519 实调 GET/POST 均 `200 / ret_code 0`（`scripts/futu_openapi_check.py`）；
- 服务端 `rt_quote` / `capital_flow` / `option_expiration` 走 openapi 通道返回真实数据；
- `DSH_WP8_SLOW=1` 两通道（openapi/mcp）对拍通过；
- WS 行情/交易鉴权 + refresh 真实冒烟通过；
- `orders_open` 在 sim 下如实说明「OpenAPI 交易接口不含模拟账户」。

### 规格 §五 验收对照

| # | 验收标准 | 状态 |
|---|---|---|
| 1 | 配置 OAuth 后既有工具不变；新增行情工具可取真实快照 | **已实现**（`3ca9f96`/`bacef14`；实调 US 标的真实数据通过） |
| 2 | `trade_place` 走 OpenAPI 下单返回 order_id；`need_order_confirm` 两层确认合一；拒绝不调 order-confirm | **已实现**（`9e279af`/`850cd71`；sim 路径实测；live 下单待人工，见已知限制 1） |
| 3 | WS 交易推送实时可见 + OMS 迁移正确；断线自动重连 | **已实现**（`8ef0302`；真实鉴权/刷新冒烟通过；事件体字段未经真实事件验证，见已知限制 4） |
| 4 | `mcp__futu__*` disabled 时全量功能不回退（除 91 工具自由研究） | **已实现**（futu 写通道 WP7 起本就被 policy 拒绝；托管 MCP 行降级可选只读研究） |
| 5 | 全量测试绿 + 工具面锁定测试绿 | **已实现**（本节测试证据） |

### 已知限制（如实登记）

1. **live 券商执行协议已接入但未经真实 live 下单验证**（P4 准入第 11 项：sim→live 冒烟待人工）；
2. 市价类订单（无 price）的风控基准价取**本地日线最近收盘**，取不到即 fail-closed 拒绝（确认卡片披露「风控基准价」）；
3. `side` 暂限 `BUY/SELL`（`SELL_SHORT/BUY_BACK` 未暴露）；`price` 未做 4 位小数校验；AUCTION 的 price 互斥为语义推断；
4. 推送事件体字段名未经真实事件验证（对账兜底轮询保留，事件仅加速）；
5. `futu-mcp` 行（托管 MCP 91 工具）降级为**可选只读研究通道**（preset 默认 disabled——用户可自行启用做自由研究）；写通道唯一在工作台；
6. SkillHub 仅作能力对照，不作为集成通道（其 OpenD 形态与单进程服务冲突）；
7. `toolCallTimeoutMs: 180000`（trade_* 确认 TTL 120s + 取数余量）；
8. 锁定表计数硬编码 ≥7 处（技术债，见规格 §6.2 第 5 项）。

### 人工验收留位（P4 准入前补）

- [ ] sim→live 切换冒烟（`docs/P4-live-trading.md` 第 11 项）：
- [ ] 真实小额定单验证（live place/modify/cancel/confirm 全链）：
- [ ] 真实交易事件到达后的推送事件体字段核对：
- [ ] 长稳观察（reconnects 不异常增长、refresh 保活 ≥1 小时）：
