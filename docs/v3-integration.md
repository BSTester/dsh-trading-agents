# V3 接入与授权：接口已对齐，等待注入密钥

本文记录 V3 控制台（Ant Design Pro 前端 + FastAPI 后端）**已对齐的数据源接口**、
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
| Tushare Pro | A 股财务/行情 | ⏳ **等待注入** | `TUSHARE_TOKEN` | `GET /api/v3/tushare` |
| OpenBB | 美股基本面（备选） | ⏳ 依赖未安装 | 视数据商而定 | `GET /api/v3/openbb` |

## 二、需要你提供的东西（按优先级）

### 1. `TUSHARE_TOKEN`（唯一必须的密钥）
- **注入方式**（任选，推荐第一种）：
  ```bash
  # 方式 A：写进服务环境（推荐：随服务生命周期）
  #   在启动脚本 / systemd unit / shell 里 export，然后重启服务
  export TUSHARE_TOKEN=你的token
  cd platform && ~/.dsh/trading-venv/bin/python -m server.run

  # 方式 B：会话内临时验证
  TUSHARE_TOKEN=你的token curl -s "http://127.0.0.1:8397/api/v3/tushare?api=income&ts_code=600519.SH&period=20260630"
  ```
- **验证命令**：注入后应返回 `{ok:true,source:"tushare/income",rows:[...]}`；未注入时返回
  `{ok:false,error:{code:"tushare/no-token",message:"TUSHARE_TOKEN 未注入（环境变量或配置）"}}`
  且**不会发出任何网络请求**。
- 消费页面：`接入与授权`（统一授权中心会由「未注入」变为「已注入」）、`行情与信号`。

### 2. 富途实时行情权限（券商侧，非密钥）
- 现盘口/板块涨跌幅走不到，报 `errcode=-9`；开通后无需改代码，`/api/v3/orderbook` 与板块行情即可返回。
- 消费页面：`行情与信号`（盘口五档卡、板块热力）。

### 3. OpenBB（可选依赖）
- 若需要：`~/.dsh/trading-venv/bin/pip install openbb`（体积较大，且需与 Python 3.13 兼容）。
- 未安装时 `/api/v3/openbb` 返回 `openbb/unavailable`，**不发请求**。

## 三、页面与接口对应

| 页面（hash 路由） | 接口 |
|---|---|
| `#/v3-overview` 系统概览 | `/api/v3/overview` |
| `#/v3-brain` 决策大脑 | `/api/v3/brain` |
| `#/v3-market` 行情与信号 | `/api/v3/market`、`/market/watchlist`、`/factors/matrix`、`/plates`、`/orderbook` |
| `#/v3-strategy` 策略与因子 | `/api/v3/strategy`、`/factors/matrix`、`/ml/sweep`、`/ml/backtest` |
| `#/v3-risk` 风险监控 | `/api/v3/risk/analytics`、`/risk`、`/oms/orders`、`/events` |
| `#/v3-execution` 执行与审批 | `/api/v3/execution`、`/oms/orders`、`/audit` |
| `#/v3-gateway` 网关与调度 | `/api/v3/gateway`、`/metrics` |
| `#/v3-tools` 工具域治理 | `/api/v3/tools`、`/metrics` |
| `#/v3-settings` 接入与授权 | `/api/v3/settings`、`/metrics`、`/news`、`/financials`、`/tushare`、`/openbb` |

## 四、部署与自检

```bash
# 前端构建（产物由 FastAPI 直接服务）
cd platform/web && npm run build

# 服务（构建产物在 platform/web/dist，FastAPI 静态兜底直接打开）
cd platform && ~/.dsh/trading-venv/bin/python -m server.run     # http://127.0.0.1:8397

# 逐页验收（无「示例」、无占位数字、真实值命中、无错误态）
bash platform/tools/verify_v3_pages.sh http://127.0.0.1:8397
```

## 五、诚实性约定（实现层面强制）

1. 任何取不到的字段：接口返回错误信封，**页面显示「无数据源」+ 原因**（权限缺口/未注入/上游不可用/服务未挂载该通道），绝不用估算值或占位数字顶替。
2. 密钥永不回显：`/api/v3/settings` 的环境变量表只报「是否注入 + 来源」。
3. 数据时点：外部数据源返回体一律带 `as_of` 与 `source`；SEC 财报额外带 `latestEnd/ageDays/stale`（申报结构变化会让旧标签停用）。
4. 交易边界：V3 页面只读；下单/改单/撤单/切模式/执行计划仍只经工作台受约束入口。
