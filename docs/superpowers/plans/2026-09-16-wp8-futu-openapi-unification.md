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
