# V3 接入与授权：接口已对齐，等待注入密钥

本文记录 V3 控制台（**设计稿原样页面 + FastAPI 后端**，无前端构建步骤）**已对齐的数据源接口**、
**需要密钥的位置**、以及**注入后如何生效**。原则：没有密钥时接口不报 500、不发无效请求，
而是返回可读错误（`{ok:false,error:{code,message}}`），页面显式显示「无数据源 + 原因」；
注入后无需改代码，重启服务即生效。

## 一、数据源状态总览

| 数据源 | 用途 | 当前状态 | 需要密钥 | 接口 |
|---|---|---|---|---|
| 工作台工具面（本服务 56 工具） | 行情/持仓/计划/审计/事件/调度 | ✅ 可用 | 否 | `POST /api/wb/<tool>`、MCP `/mcp` |
| 富途 OpenAPI / OpenD | 行情与交易通道 | ✅ 已配置（appkey 模式） | appkey（已配） | `GET /api/v3/settings` 可见渠道与 Bearer 有效期 |
| 富途实时行情权限 | 盘口五档、板块涨跌幅 | ❌ 权限缺口（`errcode=-9 realtime quote permission required`） | 券商侧开通 | 页面标注「无数据源」，不填占位 |
| AKShare | A 股新闻/另类数据 | ✅ 可用（免密钥） | 否 | `GET /api/v3/news` |
| SEC EDGAR | 美股财报三表（XBRL） | ✅ 可用（免密钥） | 否（需可识别 User-Agent） | `GET /api/v3/financials` |
| Tushare Pro | A 股财务/行情 | ⏳ **等待注入（页面可配）** | `TUSHARE_TOKEN` 或页面保存 | `GET /api/v3/tushare`、`/api/v3/credentials` |
| OpenBB | 美股基本面（备选） | ✅ 已安装可用（实测 AAPL 指标） | 视数据商而定 | `GET /api/v3/openbb` |

## 二、需要你提供的东西（按优先级）

### 1. `TUSHARE_TOKEN`（唯一必须的密钥）—— **可在页面上配置**

**方式 A（推荐，无需碰命令行）**：打开 `#/settings`（接入与授权）→「密钥与授权」卡片 →
粘贴 token → **保存** → 点 **测试连通性**（真实调用 Tushare `trade_cal`，返回延迟与上游消息）。

- 凭据落 `<DSH_HOME>/v3-credentials.json`，**0600**，原子写；
- 页面与服务端**都不回显**凭据值，只显示「已配置/未配置 + 来源 + 掩码尾号（…abcd）」；
- 保存后**立即生效**（无需重启）；**环境变量 `TUSHARE_TOKEN` 优先级高于页面配置**；
- 「清除页面配置」只删文件里的值，环境变量不受影响；
- 未配置时 `/api/v3/tushare` 与「测试」按钮都返回 `tushare/no-token`，**不发任何请求**。

**方式 B（部署方）**：环境变量注入后随服务生效：
```bash
export TUSHARE_TOKEN=你的token
cd platform && ~/.dsh/trading-venv/bin/python -m server.run
```

**接口层验证**（两条等价路径）：
```bash
# 页面同款动作
curl -s -X POST localhost:8397/api/v3/credentials -H 'content-type: application/json' \
  -d '{"action":"save","key":"tushare_token","value":"你的token"}'
curl -s -X POST localhost:8397/api/v3/credentials -H 'content-type: application/json' \
  -d '{"action":"test","key":"tushare_token"}'
# 生效后的数据接口
curl -s "localhost:8397/api/v3/tushare?api=income&ts_code=600519.SH&period=20260630"
```
- 消费页面：`接入与授权`（密钥与授权卡片 + 统一授权中心）、`行情与信号`。

### 2. 富途 OpenAPI 凭据与 OAuth 授权 —— **已在页面上可用**
- 位置：既有工作台 **设置页**（`#/settings`）——「富途授权」卡片 + **OAuth 2.1 + PKCE 授权面板**
  （Client ID 可空=自动注册 → 开始授权 → 2s 轮询 → 服务端落盘 `mode=oauth`，0600）。
  对应接口：`/api/wb/openapi_config`（AppKey 模式读写）、`/api/wb/openapi_test`（真实连通性）、
  `/api/wb/openapi_oauth`（start/status/cancel）。
- V3 接入与授权页的「密钥与授权」卡片给出状态与**入口按钮**（跳转到该页），不重复实现第二套授权。
- OAuth 需要本机回调端口 `http://localhost:<port>/callback`，因此必须在能访问该回调的机器上操作。

### 3. 富途实时行情权限（券商侧，非密钥）
- 现盘口/板块涨跌幅走不到，报 `errcode=-9`；开通后无需改代码，`/api/v3/orderbook` 与板块行情即可返回。
- 消费页面：`行情与信号`（盘口五档卡、板块热力）。

### 4. OpenBB（可选依赖）
- 若需要：`~/.dsh/trading-venv/bin/pip install openbb`（体积较大，且需与 Python 3.13 兼容）。
- 未安装时 `/api/v3/openbb` 返回 `openbb/unavailable`，**不发请求**。

## 二·五、前端：Ant Design Pro 工作台（挂在根路径）

V3 工作台是 `platform/web-pro`（Vite + React18 + antd5 + @ant-design/pro-components）的构建产物，
由 FastAPI 在**根路径**直接服务：

```bash
cd platform/web-pro && npm run build        # 产物 dist/（改页面后必须重新构建）
cd platform && ~/.dsh/trading-venv/bin/python -m server.run   # http://127.0.0.1:8397
```

- 入口：`http://127.0.0.1:8397/` → 9 个页面走 hash 路由 `#/<key>`（overview/brain/market/strategy/
  risk/execution/gateway/tools/settings）
- 菜单分组与设计稿导航一致：监控 · 研究 · 交易 · 系统
- 数据只有两条通道：读 `GET /api/v3/*`、写 `POST /api/wb/*`（受约束入口）；缺失数据源显式标注，无占位数据
- 历史 URL `/v3/*`、`/pro/*` 已随旧版删除，但静态兜底会回落工作台首页，不会死链

## 三、页面与接口对应

V3 控制台是 **OpenDesign 设计稿的 HTML/CSS 原样页面**（`platform/web/public/v3/`，Vite 构建原样拷到
`dist/v3/`，由 FastAPI 直接服务；`/` 307 跳转到 `/v3/index.html`）。设计稿的标记与样式**一字未改**
（`platform/tools/compare_with_design.sh` 逐字节校验），真实数据由每页一个 binder（`/v3/<page>.js`）
注入设计稿原有 DOM 节点；取不到的区块显式标注「无数据源」。

| 页面（hash 路由） | 接口 |
|---|---|
| `#/overview` 系统概览 | `/api/v3/overview` |
| `#/brain` 决策大脑 | `/api/v3/brain` |
| `#/market` 行情与信号 | `/api/v3/market`、`/market/watchlist`、`/factors/matrix`、`/plates`、`/orderbook` |
| `#/strategy` 策略与因子 | `/api/v3/strategy`、`/factors/matrix`、`/ml/sweep`、`/ml/backtest` |
| `#/risk` 风险监控 | `/api/v3/risk/analytics`、`/risk`、`/oms/orders`、`/events` |
| `#/execution` 执行与审批 | `/api/v3/execution`、`/oms/orders`、`/audit` |
| `#/gateway` 网关与调度 | `/api/v3/gateway`、`/metrics` |
| `#/tools` 工具域治理 | `/api/v3/tools`、`/metrics` |
| `#/settings` 接入与授权 | `/api/v3/settings`、`/metrics`、`/news`、`/financials`、`/tushare`、`/openbb` |

## 四、部署与自检

```bash
# 无前端构建步骤：V3 控制台就在 platform/web/public/v3/，FastAPI 直接服务该目录

# 服务（构建产物在 platform/web/dist，FastAPI 静态兜底直接打开）
cd platform && ~/.dsh/trading-venv/bin/python -m server.run     # http://127.0.0.1:8397

# 逐页验收（9 路由：无「示例」、无设计稿残留占位数字、关键模块命中、菜单分组可见、无崩溃/白屏）
bash platform/tools/verify_pages.sh http://127.0.0.1:8397
```

## 五、诚实性约定（实现层面强制）

1. 任何取不到的字段：接口返回错误信封，**页面显示「无数据源」+ 原因**（权限缺口/未注入/上游不可用/服务未挂载该通道），绝不用估算值或占位数字顶替。
2. 密钥永不回显：`/api/v3/settings` 的环境变量表只报「是否注入 + 来源」。
3. 数据时点：外部数据源返回体一律带 `as_of` 与 `source`；SEC 财报额外带 `latestEnd/ageDays/stale`（申报结构变化会让旧标签停用）。
4. 交易边界：V3 页面只读；下单/改单/撤单/切模式/执行计划仍只经工作台受约束入口。
