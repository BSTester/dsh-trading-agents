# V3 数据源政策：除富途授权外，全部免密钥公开端点

> **生效日**：2026-09-21。本文是数据渠道**准入与退出**的唯一政策文本；实现位置以本仓库
> 当日树为准（`file:line` 可逐条核对）。政策文本与代码不一致时，以代码为准并回来改本文
> （本文的每一行都来自读代码 + 真机只读实测，不是转述）。
>
> 相关文档：缺口补齐前后对照与真机输出见 `docs/e2e-and-data-gaps.md` 第二十六节；
> 能力对齐矩阵见 `docs/v3-capability-alignment.md`。

---

## 一、政策（三条硬规则 + 一个例外）

1. **准入 = 免密钥**。任何需要 token / api key / 账号密码的第三方数据渠道，**一律不进代码**：
   不注册凭据、不写探测、不进降级链、不留「配置好就能用」的惰性分支。
   理由：密钥渠道在无人值守的运行里必然退化成「静默取不到」——它既不能被 CI 验证，
   也无法在真机上区分「权限问题」与「网络问题」，与仓库「数据诚实」纪律冲突。
2. **唯一例外 = 富途（授权使用）**。富途走 OpenAPI/MCP 授权（`~/.dsh/futu-token`、
   `futu-openapi.json`），是**明确授权的商业数据源**，不属于「第三方公开端点」范畴；
   其凭据由 `scripts/futu_auth.py` 安装，不由 `server/v3_credentials.py` 管理。
   量化后果：**只有富途这一条链会因权限缺口（如 A 股实时 `-9`）失败**，其余链的失败
   一律是上游/网络的真实故障，可如实归因。
3. **诚实 = 每个响应都能回答「数从哪来、什么时候、降级到哪一级」**：
   `source` / `as_of`（取数时刻）/ `quote_time`（上游行情时间）/ `delay`（延时口径）/
   `chain`（逐级 `{source,ok,ms,error}`）/ `attempts`（重试明细）都是契约字段；
   取不到就返回 `ok:false` + 真实错误原文，**不用估算值、不填 0 占位、不拿不同口径互相冒充**。

政策在代码里的落点（可核对的四份「同一套纪律」）：

| 落点 | 位置 | 承担什么 |
|---|---|---|
| 凭据注册表为空 | `platform/server/v3_credentials.py:23`（`REGISTRY` 空）+ `:105`（status 的 `note`）、`:114`（save 拒绝新数据类凭据） | 「无密钥渠道」这件事在凭据层是**可断言的事实**，不是口号 |
| Tushare Pro 整体移除 | `platform/server/v3_sources.py:98`（政策注释，原 `TUSHARE_APIS` 已删） | 需 token 的渠道不留惰性分支 |
| 免密扩展源独立模块 | `platform/server/v3_sources_ext.py:1-36`（模块 docstring = 数据源政策正文） | 新免密源的落点与口径声明 |
| 降级链与状态探测 | `platform/server/v3_fallback.py:1-20`（`run_chain` 纪律）+ `:55`（8 条缺省链） | 「按顺序试 + 逐级留痕 + 全失败如实报错」收敛成一份实现 |

---

## 二、逐源清单（实现位置 / 端点 / 免密 / 覆盖 / 降级位次 / 失败信封）

### 2.1 A 股实时行情 · 盘口（本轮新增，公开三源降级链）

链顺序的**唯一事实来源**是 `platform/server/v3_sources_ext.py:74` 的 `PUBLIC_QUOTE_SOURCES`；
每源一次请求、失败**不重试**（东财对突发会断连，重试只会加重）→ 立即换下一家。

| 位次 | 源 | 端点 | 免密 | 实现位置 | 覆盖 | 失败信封 |
|---|---|---|---|---|---|---|
| ① | 东方财富 push2 | `GET https://push2.eastmoney.com/api/qt/stock/get?secid={1\|0}.{code}&fields=…&invt=2&fltt=2` | ✅ | `v3_sources_ext.py:242`（字段表 `:237`） | 沪/深六位数字；报价 11 字段 + **五档** | `sec/network`（走共享 `Deps.json_get`，`:218` → `v3_sources.py:313`）或 `eastmoney/push2`（`data:null`/无 `f43`） |
| ② | 腾讯行情 | `GET https://qt.gtimg.cn/q={sh\|sz}{code}`（GBK 文本） | ✅ | `v3_sources_ext.py:281` | 沪/深；报价 + 五档（量单位手） | `tencent/qt.gtimg.cn`（解析失败）/ `quote/network`（文本口，`:190`） |
| ③ | 新浪行情 | `GET https://hq.sinajs.cn/list={sh\|sz}{code}`（GBK，**必须带 Referer**，否则 403） | ✅ | `v3_sources_ext.py:326` | 沪/深；报价 + 五档（量单位**股**，`/100` 换算成手）；**无涨跌额/涨跌幅字段 → 给 `null`，不从价差推算**（`:359`） | `sina/hq.sinajs.cn` / `quote/network` |

预算与保护（`v3_sources_ext.py:62-66`、`:86-92`）：

| 参数 | 值 | 语义 |
|---|---|---|
| `QUOTE_TIMEOUT` | 5.0s | 单源超时（降级链的意义是快） |
| `QUOTE_COOLDOWN_S` | 60.0s | 源失败后的**进程内冷却**：冷却期内的源记 `quote/cooldown` 并 `skipped`，不发请求（不假装试过，`:392-396`） |
| `QUOTE_CHAIN_TIMEOUT` | 20.0s | 整链墙钟预算，超预算的剩余源记 skipped |
| 覆盖边界 | `A_SHARE_PUBLIC_MARKETS = ("SH","SZ")`（`:77`） | **北交所不猜前缀映射**：`BJ.*` 与 HK/US 一律 `quote/unsupported-market`（`:414`） |
| 取数口 | 文本走 httpx，缺库才退 urllib（`:170-190`） | python-urllib 客户端指纹会被东财 WAF 直接拒连（requests/httpx 正常）——故东财 JSON 也统一走 httpx |

**接入方式：降级不是替换**（`platform/server/futu_data.py:87-108` 钩子 + `v3_sources_ext.py:458-511`）：

* 只在富途返回 **-9（A 股无实时权限）** 且请求**全部**是沪/深标的时才启用
  （`futu_data.py:948-959` `_is_public_fallback_eligible`；`details.ret_code/errcode == -9`）；
* HK/US、混合列表、非 -9 的业务错误**一字不变**——原错误原样上抛；
* 命中时响应带 `source` / `delay` / `as_of` / `futu_fallback{reason,policy,upstream_error}`；
* 公开链也全失败 → 返回 `None`，**原始 -9 错误原样上抛**（降级失败不编造）。

富途侧入口：`futu_data.py:972`（`rt_quote`）、`:1015`（`rt_order_book`）、
`:961`（`_public_fallback_value`，降级层异常不覆盖上游真实错误）。

### 2.2 北向资金（本轮新增）

| 部件 | 源 | 端点（AKShare 函数） | 免密 | 实现位置 | 失败信封 |
|---|---|---|---|---|---|
| 当日四方板块 | AKShare（东财） | `stock_hsgt_fund_flow_summary_em()` | ✅ | `v3_sources_ext.py:527` | `akshare/stock_hsgt_fund_flow_summary_em`（带 `attempts`） |
| 沪股通历史 | AKShare（东财） | `stock_hsgt_hist_em(symbol="沪股通")` | ✅ | `v3_sources_ext.py:567` | `akshare/stock_hsgt_hist_em` |
| 深股通历史 | 同上 | `stock_hsgt_hist_em(symbol="深股通")` | ✅ | 同上 | 同上 |
| 部件汇总 | — | — | — | `v3_sources_ext.py:601`（`fetch_northbound`） | 三部件全失败 → `northbound/all-parts-failed` + `errors[]`；任一部件的真实错误逐条留在 `errors` |

**披露事实（政策要求「不把占位当真实值」的典型）**：沪深港交易所自 **2024-08-19** 起不再
披露北向当日净买入。2026-09-21 独立复核（直接调 `akshare 1.18.96`）：

```
沪股通 rows: 2757  最后 3 行：2026-09-17 NaN / 2026-09-18 NaN / 2026-09-21 NaN
深股通 rows: 2281  最后 3 行：同上
last non-null net_buy date: 2024-08-16（沪深一致）
```

因此响应里北向板块的 `net_buy: 0.0` 是**上游占位值**：代码以 `net_buy_disclosed: false`
显式标注（`v3_sources_ext.py:554`），并在 `net_buy_disclosure` 块写明最后披露日
（`:632-636`）；南向（港股通）不受影响，实测有真实值。

> ⛔ **当前实现缺陷（2026-09-21 实测）**：`_northbound_hist` 调用了未导入的 `_head`
> （`v3_sources_ext.py:583`；模块顶部 `:45-57` 的 import 清单里没有它，`v3_sources.py:547`
> 才有定义）→ `/api/v3/northbound` 整条路由返回
> `{"ok": false, "error": {"code": "northbound/internal", "message": "NameError: name '_head' is not defined"}}`。
> 部件本身是好的（`_northbound_summary` 单独调用返回真实四板块数据）。修法二选一：
> 在 `:45-57` 补 `_head`，或 `:583` 直接写 `_rows_from_frame(frame)[:100000]`。

### 2.3 宏观经济（本轮新增：主源 + 第二源口径并列）

| 腿 | 源 | 端点 | 免密 | 实现位置 | 覆盖 | 失败信封 |
|---|---|---|---|---|---|---|
| 主源 | AKShare（金十整理的国家统计局/央行口径） | `macro_china_cpi` / `macro_china_ppi` / `macro_china_pmi` / `macro_china_shrzgm` / `macro_china_money_supply` | ✅ | `v3_sources_ext.py:660`（白名单）→ `:721`（`_macro_nbs`） | CPI/PPI/PMI/社融/M2（中文别名 `社融`/`货币供应量`，`:676`） | `akshare/<func>`（带 `attempts`）；未知指标 → `macro/unknown-indicator` |
| 第二源（对照面） | OpenBB `economy.cpi(provider="oecd")`（免密 `openbb-oecd`） | `openbb.economy.cpi` | ✅ | `v3_sources_ext.py:765`（`_macro_oecd`；口径声明 `:681-684`） | **只有 CPI 有中国同口径序列**；其余如实 `oecd/no-series`（`:685-688`） | `oecd/no-series` / `openbb/unavailable` / `openbb/import-failed` / `openbb/capability-missing` / `openbb/economy.cpi` / `openbb/no-rows` |

两口径的口径纪律（`:821` `fetch_macro`）：`compare=oecd` 时才并列 `oecd` 块，块内自带
`caliber`（OECD 标准化口径，`value` 是小数、`value_pct = value×100` 显式换算），
**不冒充 NBS 口径**；主源失败且指标为 CPI 时自动降到 OECD 腿，`source`/`caliber` 如实换标并
保留 `nbs_error`。缺省不触发 openbb（冷启动 import 实测 32~57s，见 `:690-693`、`:1013`）。

### 2.4 既有免密源（存量，政策复核后保留）

| 源 | 端点 | 免密 | 实现位置 | 覆盖 | 降级位次 |
|---|---|---|---|---|---|
| SEC EDGAR | `https://www.sec.gov/files/company_tickers.json`、`https://data.sec.gov/api/xbrl/companyconcept/CIK…/us-gaap/{tag}.json` | ✅（需 UA） | `v3_sources.py:88-90`、`class SecSource :1087` | 美股三表（XBRL，含 `stale` 判据 `:85`） | 美股财务**主源**（`v3_fallback.py:77`） |
| AKShare 个股资讯 | `stock_news_em` | ✅ | `fetch_news`（`v3_sources.py:551`） | A 股个股资讯 | 资讯主源（`/api/v3/news`） |
| AKShare 全市场快照 | `stock_zh_a_spot_em` → `stock_sh/sz/bj_a_spot_em` → `stock_zh_a_spot`（新浪，分页最慢排最后） | ✅ | `AKSHARE_SPOT_CHAIN`（`v3_sources.py:393`）、`fetch_spot :595` | A 股全市场 / 分市场（响应 `market_scope` 写明，不冒充全市场） | 快照链降级源（主源是富途 `market_snapshot`）；链预算 `QUANT_AKSHARE_SPOT_BUDGET_MS=15000` |
| AKShare 财务 | `stock_financial_abstract`（A 股）/ `stock_financial_hk_report_em`（港股） | ✅ | `fetch_financials_akshare`（`v3_sources.py:832`）、链接表 `:1458-1461` | A 股/港股三表 | 财务链**第 2 级**（主源富途 `f10_detail/statements`） |
| Yahoo（yfinance） | `yfinance` 季度 `balance_sheet`/`income_stmt`（经 `trading_datasource.fundamentals.load_returns`） | ✅ | `v3_fundamentals_sync.py:65`（`SOURCE_YAHOO`）、`:117-128` | roe/roa/equity/total_assets/net_income（离线落库通道） | 不进在线链：**离线**写 `trading-data/fundamentals`，矩阵只读本地库 |
| 东财披露注册表 | `https://datacenter-web.eastmoney.com/api/data/v1/get`（`reportName=RPT_LICO_FN_CPD`，取 `NOTICE_DATE`） | ✅ | `v3_fundamentals_sync.py:155-208`（`:167` 端点） | A 股各报告期**真实公告日** | roe/roa 落库时的 `announced_at` 唯一来源（`SOURCE_NOTICE_DATE :67`） |
| OpenBB yfinance provider | `openbb.equity.fundamental.metrics(symbol=…, provider="yfinance")` | ✅ | `v3_sources.py:1334`（`:1359` provider） | 美股基本面 | 美股财务链降级源（`v3_fallback.py:78`） |
| 富途（**授权例外**） | OpenAPI / MCP 全端点（`f10_detail`、`quote_history_kline`、`market_snapshot`、`rt_quote`、`rt_order_book`、`capital_flow*`、`short_interest`…） | ❌ 需授权 | `futu_data.py`（端点映射 `:118`/`:156`/`:441`…） | A 股/港股/美股行情、财务、资金面、做空… | 各链**主源**；A 股实时 `-9` → 公开链降级（§2.1） |

### 2.5 失败信封命名表（写文档/排障时按这个对号）

| 信封码 | 出处 | 含义 |
|---|---|---|
| `sec/network` | `v3_sources.py:313`（`Deps.json_get` 兜底；`:232` 是模块级 `http_get_json` 的同名兜底） | JSON 取数网络失败。**注意**：东财 push2 走这个口，所以它的网络失败报文前缀是 `sec/`（历史命名，非 SEC 专用）——排障时别误判成 SEC 的问题 |
| `quote/network` / `quote/http` | `v3_sources_ext.py:187`、`:190` | 文本口（腾讯/新浪）失败 |
| `quote/cooldown` | `v3_sources_ext.py:395` | 该源刚失败过，冷却期内跳过（`skipped`，不假装试过） |
| `quote/unsupported-market` | `v3_sources_ext.py:415` | 公开链不覆盖（北交所/HK/US）——不猜前缀映射 |
| `eastmoney/push2` / `tencent/qt.gtimg.cn` / `sina/hq.sinajs.cn` | 各源解析分支 | 拿到了响应但字段不足/无数据（真实上游缺档） |
| `akshare/missing` / `akshare/missing-func` / `akshare/no-rows` / `akshare/<func>` | `v3_sources_ext.py:532/535/562/539` 等 | akshare 缺席 / 版本不兼容 / 空结果 / 上游失败（带 `attempts`） |
| `northbound/all-parts-failed` · `northbound/internal` | `:614` / `:1005` | 三部件全失败（`errors[]` 逐条） / 路由级未预期异常 |
| `macro/unknown-indicator` · `macro/all-sources-failed` · `macro/internal` | `:835` / `:857` / `:1019` | 指标名不在白名单 / 两腿都不可用 / 路由级异常 |
| `oecd/no-series` · `openbb/*` | `:768` 等 | 该指标无 OECD 中国同口径序列 / openbb 缺席·导入失败·能力缺失·provider 错·空结果 |

---

## 三、Tushare 移除记录（2026-09-21）

### 3.1 原因（一句）

**Tushare Pro 需要 `TUSHARE_TOKEN`，属政策 §一.1 排除的密钥渠道**；且它的历史形态本身
自相矛盾——实现走 HTTP `POST http://api.tushare.pro`（不需要 `tushare` 包），而设置页的
可用性判据却要求「`tushare` 包可导入」，导致「只有 token 没装包」时页面误报不可用
（该矛盾在 `docs/v3-capability-alignment.md` 的 W5/B2 已登记）。留着它意味着留一条
**永远无法在无人值守环境验证**的通道，因此整体移除而非降级处理。

### 3.2 移除面（逐条可核对的删除清单）

| 文件 | 删除的符号 / 行为 |
|---|---|
| `platform/server/v3_sources.py` | `TUSHARE_ENDPOINT`、`TUSHARE_APIS`（5 个 api 表）、`TUSHARE_STATEMENT_APIS`、`http_post_json`、`_urllib_post_json`、`Deps.json_post`、`fetch_tushare`、`normalize_tushare_code`、`/api/v3/financials` 降级链**第 3 级** `tushare_link`、路由 `GET /api/v3/tushare` |
| `platform/server/v3_credentials.py` | 凭据注册表条目 `tushare_token`、`TUSHARE_ENDPOINT`、`resolve_tushare_token`、`test_tushare`、`action=test` 的 tushare 分支（注册表现为空，`:23`） |
| `platform/server/v3_ops.py` | `ENV_KEYS` 里的 `TUSHARE_TOKEN`、`TUSHARE_PROBE_URL`、`_tushare_row`、「Tushare Pro」数据源行（5 行 → 4 行） |
| `platform/server/v3_mcp.py` | `TOOL_DOCS["/api/v3/tushare"]`、`PARAM_DOCS["api"/"ts_code"]`、`PARAM_DOCS_OVERRIDES[("/api/v3/tushare","period")]`、env 文档里的 TUSHARE_TOKEN |
| `platform/tests/*` | `test_v3_sources` 的 tushare 小节（改为断言 `/api/v3/tushare` **不在**路由表）、`test_v3_credentials` 的 tushare 用例（改为断言空注册表 + 未知键拒绝）、`test_v3_ops` 的 ENV_KEYS / 数据源行断言 |

> 范围外（**本轮未动，也不在政策要求内**）：`platform/server/mcp_tools.py` 的 298 行改动是
> 工具面并发元数据（`quantwb.isConcurrencySafe`），与数据源政策无关；
> `platform/web-pro/*` 与 `platform/tools/e2e_probe.py` 的残留 tushare 引用见 §三.4。

### 3.3 能力替代映射（移除后每项能力去哪了）

| 原 Tushare 能力 | 替代通道 | 免密？ | 位置 |
|---|---|---|---|
| A 股利润表/资产负债表/现金流量表（`income/balancesheet/cashflow`） | 富途 `f10_detail/statements`（主源）→ AKShare `stock_financial_abstract`（降级） | ✅（降级腿） | `v3_sources.py:1458-1461` |
| A 股日线（`daily`） | 富途 `quote_history_kline`（主源）→ AKShare `stock_zh_a_hist`（降级） | ✅ | `v3_fallback.py:55-61`（`kline` 链） |
| 每日指标（`daily_basic`：pe/pb/ps/换手/量比） | 富途估值快照（`valuation_detail`）+ 腾讯行情字段（pe_ttm/pb/换手/量比） | ✅（腾讯腿） | `FACTOR_REGISTRY` 的 pe_ttm/pb/ps 条目（`v3_analytics.py` 内，行号随并发改动漂移，以符号名为准）、`v3_sources_ext.py:309-315` |
| 股票列表（`stock_basic`） | 富途 `plate_stock`/自选池 + `market/universe`（本地宇宙表） | — | `v3_fallback.py:51`（`PROBE_PLATE_CODE` 探针）、`v3_analytics.py` 宇宙解析 |
| roe/roa 等回报指标 | **Yahoo（yfinance）季度财报离线落库** + 东财 `NOTICE_DATE` 定披露日 | ✅ | `v3_fundamentals_sync.py:65-67`、`:155-245` |
| 北向资金 | AKShare `stock_hsgt_fund_flow_summary_em` + `stock_hsgt_hist_em` | ✅ | `v3_sources_ext.py:527/567`（⛔ 见 §2.2 缺陷） |
| 宏观（CPI/PPI/PMI/社融/M2） | AKShare macro 五函数（主） + OpenBB OECD（CPI 第二源） | ✅ | `v3_sources_ext.py:660/721/765` |

### 3.4 残留引用清单（`grep -rni tushare`，排除 `docs/v3-spec.md`/`node_modules`/`dist`/`.git`）

截止 2026-09-21 19:20，全树（大小写不敏感）命中 **20 个文件**——其中 `docs/v3-source-policy.md`
（本文）是政策文本自身，`docs/e2e-and-data-gaps.md` 的第二十六节是本轮验证记录；其余 18 个逐条说明：

**A. 有意保留（历史记录 / 负向断言 / 事实性对比，不该删）**

| 位置 | 保留理由 |
|---|---|
| `platform/server/v3_sources.py:3`（模块 docstring）、`:98`（`TUSHARE_APIS` 位置的政策注释）、`:1439`（财务链注释「Tushare 已按数据源政策移除」） | 政策注释：写明「为什么没有它」，防止后来者再加回来 |
| `platform/server/v3_credentials.py:8`（模块 docstring 的政策段）、`:182`（test 动作「不再有 tushare 连通性测试」） | 同上（凭据面） |
| `platform/server/v3_ops.py:125`（`ENV_KEYS` 处的政策注记）、`:1831`（「2026-09-20 修正：SEC 与 Tushare 两条曾与之相反」）、`:2016`（「Tushare Pro 行已随…移除」） | 同上（设置页/探测面），且是**历史修正记录** |
| `platform/server/v3_quality.py:14`、`:62` | 事实性对比：「成交类数据没有开源替代（AKShare/Tushare 都拿不到委托状态与成交回报）」——用来说明**能力缺口**，不是在引用 Tushare 取数 |
| `platform/requirements.txt:27` | 注释「Tushare Pro 不需要额外依赖（纯 httpx），但需要 TUSHARE_TOKEN」——历史说明（该依赖项本就空） |
| `platform/tests/test_v3_sources.py:8/637/650`、`test_v3_credentials.py:5/60/64/72`、`test_v3_ops.py:405/413/428/429/449/452` | **负向断言**（`assertNotIn("/api/v3/tushare", app.routes)`、空注册表、未知键拒绝、`TUSHARE_TOKEN` 不在 `ENV_KEYS`、数据源行不含 Tushare）：删掉这些用例等于删掉「不许回来」的守卫 |
| `platform/tests/test_mcp_discovery.py:559/900`、`test_mcp_parity.py:601/610` | 用 `tushare_token` 当**任意未注册键**的样例（验证凭据面回 `unknown-key`），与数据源政策无关 |
| `docs/v3-capability-alignment.md`（8 处：`:22/298/331/342/382/438/467/513`） | 历史对齐矩阵 / 审查记录（记录当时的缺口与后续修正），改它等于篡改历史证据 |
| `docs/e2e-probe-latest.json`（6 处）、`docs/e2e-and-data-gaps.md` 第一轮小节（`:36/66/68/72/81/1545`） | 上一轮的**真机探针快照**与缺口记录，保留可对比；本轮新增的第二十六节已经在 26.2 里标明该缺口「已由政策移除解决」 |

**B. 待清理（应删/应改，交主 agent；本轮未动任何代码）**

| 文件:行 | 现状 | 建议 |
|---|---|---|
| `platform/server/v3_mcp.py:378` | 凭据工具的**参数文档**仍写 `_p("key", "str", "凭据键名（缺省 tushare_token）")` | 改为「凭据键名（当前注册表为空：数据渠道一律免密钥，任何键回 unknown-key）」——否则 Agent 侧仍会以为有个默认 tushare 键 |
| `platform/web-pro/src/pages/settings.jsx:8` | 文件头注释仍写「Tushare token 可配置（保存/测试/清除）」 | 改为「统一授权中心（当前注册表为空：数据渠道一律免密钥）」 |
| `platform/web-pro/src/pages/settings.jsx:46` | `ENV_HINTS` 仍有 `TUSHARE_TOKEN: "Tushare Pro 行情 / 财务数据"` | 删除该键（后端 `ENV_KEYS` 已移除，前端提示表不该留） |
| `platform/web-pro/src/pages/settings.jsx:508` | 注释小标题「Tushare token 的页面化配置」 | 改标题（该段已无实际凭据行） |
| `platform/web-pro/src/pages/settings.jsx:1076` | 文案「统一授权中心（Tushare token 等）为全局口径」 | 删「Tushare token 等」 |
| `platform/web-pro/src/pages/settings.jsx:1275` | 文案「其中 TUSHARE_TOKEN 的已配置状态见上方…」 | 删该句 |
| `platform/web-pro/src/pages/settings.jsx:1497` | `title="清除页面配置的 Tushare token"` | 改通用文案（按钮对应的注册表已空） |
| `platform/web-pro/src/pages/overview.jsx:11` | 注释「…AKShare / SEC EDGAR / Tushare 真实探测」 | 删 Tushare |
| `platform/web-pro/src/pages/overview.jsx:1105` | 文案「来源 /api/v3/settings（富途 / AKShare / SEC EDGAR / Tushare 探测）」 | 删 Tushare（后端已只报 4 行） |
| `platform/tools/e2e_probe.py:50`（凭据 status 的 key）、`:57`（端点清单里的 `"tushare"`）、`:95`（`"tushare": {...}` 载荷）、`:125`（凭据探测调用） | 探针仍按**已删除**的 `/api/v3/tushare` 与 `tushare_token` 契约打点 | 按后端新契约删除这 4 处（否则每次探针都会记一条「未知端点 / unknown-key」的假缺口） |
| `docs/v3-integration.md`（15 处：`:98/290/368/380/381/387/390/394/396/402/406/409/411/412/464`） | 含 `/api/v3/tushare` 端点表、`TUSHARE_TOKEN` 配置章节、`v3_tushare` 工具行、`curl` 示例 | 该文件正由另一 agent 改写，**本轮未动**；改写时需整段删除（不是改词） |

**C. 无需改动（转述里点名、实际已清理）**

| 文件:行 | 事实 |
|---|---|
| `platform/web-pro/src/components/dataDomain.jsx:116-118` | 只剩 `TushareBlockRemoved()` 占位函数与一段说明注释，**已无任何调用**（`DataDomainCard` 里已换成「A 股财务（免密离线通道：东财 yjbb → 本地库）」文案） |
| `platform/web-pro/src/pages/market.jsx:1283` | 注释已改为「数据域：把后端外部数据源端点（news/financials/openbb/spot）全部对到界面」——**无 tushare 字样** |

---

## 四、OpenBB 处置结论

### 4.1 现状（两处使用，都在免密 provider 上）

| 用途 | 调用 | 位置 | provider | 免密 |
|---|---|---|---|---|
| 美股基本面 | `obb.equity.fundamental.metrics(symbol=…, provider="yfinance")`（取不到则降级 income/balance/cash_flow 入口，`v3_sources.py:1346-1352`） | `fetch_openbb`（`v3_sources.py:1334`；provider 写死处 `:1359`） | `yfinance` | ✅ |
| 宏观第二源 | `obb.economy.cpi(country="china", provider="oecd")` | `v3_sources_ext.py:765` | `oecd`（`openbb-oecd`） | ✅ |
| 状态探测 | **不探测**：`v3_fallback.py:562-567` 显式记「未探测 + 原因」 | `v3_fallback.py:562` | — | — |

### 4.2 provider 分类（政策判据）

* **可启用（免密钥）**：`yfinance`、`sec`、`oecd`、`imf`、`fed`。
  其中本仓库实际用到两个：`yfinance`（美股基本面）、`oecd`（宏观 CPI 第二源）。
  `sec` 的职能已由**自研 SEC 客户端**（`v3_sources.SecSource`，直连 `data.sec.gov`）承担——
  不需要经 OpenBB 转一手（少一层依赖、错误归因更清楚）；`imf` / `fed` **当前没有调用点**，
  属于「政策允许但未使用」。
* **一律不启用（需 key）**：`fmp`、`tiingo`、`intrinio`、`tradingeconomics`。
  判据同 §一.1：需要 key 的 provider 在无人值守环境不可验证，且会在响应里制造
  「provider-error / credential」这类与数据事实无关的噪音。仓库内**零引用**
  （2026-09-21 `grep -rn "fmp|tiingo|intrinio|tradingeconomics" platform/` 无命中）。

### 4.3 对 `GET /api/v3/openbb` 的建议：**收缩（保留但降级为对照面），不建议现在移除**

三条路的取舍与理由：

| 选项 | 结论 | 理由 |
|---|---|---|
| 移除端点 | ❌ 不建议（现在） | 它是美股财务链的**降级第 2 级**（`v3_fallback.py:78`）；直接删会让 `financials_us` 链只剩 SEC 一级（SEC 的 XBRL 标签会因申报结构变化 stale，`v3_sources.py:85`），失去唯一交叉校验面 |
| **收缩** | ✅ **建议** | ① 把 `provider` 收敛到免密白名单（当前只有 `yfinance`，已是事实，需**写死在代码里**并对其它 provider 直接拒绝）；② 端点保持惰性（未安装 `openbb` 时不发任何请求 → `openbb/unavailable`，`v3_sources.py:1336-1340`）；③ 状态探测继续「不探测」（`v3_fallback.py:562`），避免 44-57s 冷启动拖垮 `/api/v3/sources/status`；④ 文档里明确标注它是**慢依赖**（冷启动实测 36.5s，本机 2026-09-21 复核）而非首屏路径 |
| 保留现状（不动） | ⚠️ 可接受但不达标 | 现状已满足「免密」政策，但缺两条硬化：provider 白名单未写死、宏观 OECD 腿当前返回 `openbb/no-rows`（见下）——不修就是「文档说有第二源、实际拿不到」 |

**必须先修的实测缺陷（收缩的前提）**：`/api/v3/macro?indicator=cpi&compare=oecd` 实测
`oecd` 块为 `{"code": "openbb/no-rows", "message": "economy.cpi(oecd, china) 未返回可解析的月度序列"}`。
根因已定位（本机 openbb 实测）：`obb.economy.cpi(country="china", provider="oecd")` 的
`to_dataframe()` 把**报告期放在 DataFrame 的 index 上**（`index.name == "date"`，
columns = `['country','value','expenditure']`），而 `_rows_from_frame` 用
`df.to_dict("records")`（`v3_sources.py:521`）**会丢掉 index** → `_macro_oecd` 取
`raw.get("date")` 恒为 `None`（`v3_sources_ext.py:797`）→ 全行被跳过 → `openbb/no-rows`。
修法（二选一，属 `platform/**`，本轮未改）：`df.reset_index()` 后再转 records；
或改用 `res.results`（实测每个元素带 `date=datetime.date(…)`、`country`、`value`、`expenditure`）。

---

## 五、如何新增一个免密源（步骤清单）

> 目标：加完之后，「免密 + 覆盖 + 口径 + 降级位次 + 失败信封」五件事都能在代码里被指出来，
> 且**不引入任何密钥**。

**第 0 步：准入判据（先回答，再写代码）**

1. 该端点是否**无需任何凭据**即可请求？（需要 key → 停，按政策 §一.1 不采纳）
2. 上游给的**延时口径**是什么（实时 L1 快照 / 延迟 15 分钟 / 日频）？——必须能逐字写进 `delay`。
3. 覆盖边界在哪（哪些市场/哪些标的**不覆盖**）？——不覆盖的要**明确拒绝**，不猜映射。
4. 单位与字段语义（手 vs 股、元 vs 亿元、同比 % vs 小数）能否从上游文档或实测确定？
5. 单请求耗时/失败模式（断连？WAF？限流？）——决定超时、是否重试、是否要冷却。
6. **能在本机真跑一次**并留下证据吗？（跑不通就不要进链）

**第 1 步：选落点**

* 只是给**既有端点**加一条降级源 → 改对应模块的链定义（如 `v3_sources.py:1458-1461`）。
* 是**新端点** → 优先新建/追加到 `platform/server/v3_sources_ext.py` 这类**扩展模块**，
  由既有 `register` 末尾追加装配（`v3_sources.py:1558-1562`，**不改 `app.py` 的封闭接线清单**）；
  模块可能缺失时不阻断主装配（同处 `except ModuleNotFoundError`）。

**第 2 步：实现取数函数（照抄既有形状）**

```python
def fetch_xxx(deps, ...):
    # 1) 取数：一切外部调用都带超时；deps 注入口让测试能封网络
    result = deps.base.json_get(url, headers=..., timeout=XXX_TIMEOUT)   # 或 deps.text_get
    if not result.get("ok"):
        return result                      # 真实错误原样上传，不吞
    # 2) 解析：字段缺失 = 失败（不填 0、不猜字段、不从别的字段推算）
    # 3) 成功载荷：source / as_of / 口径字段齐全
    return {"ok": True, "source": "<vendor>/<endpoint>", "as_of": now_iso(), ...}
```

* **绝不抛异常出模块**：失败一律 `envelope_error(code, message)`（`v3_sources.py:148`）。
* **重试策略要写清**：akshare 走 `retry_akshare`（指数退避 + 抖动 + 预算，`v3_fallback.py`）；
  对「突发就断连」的上游（东财）**不重试 + 进程内冷却**（`v3_sources_ext.py:86-92`）。
* **常量集中**：超时/冷却/预算/字段表/白名单都提成模块级常量并写清理由（便于测试断言与后人调参）。

**第 3 步：接链与标注**

* 用 `v3_fallback.run_chain([...], timeout=CHAIN_BUDGET)`，并把 `attempts_chain(attempts)`
  放进成功响应（`value["chain"]`）与失败响应（`payload["chain"]/["tried"]`）——
  见 `v3_sources_ext.py:420-435` 的完整写法。
* 若新源是**降级不是替换**（只在主源特定错误下启用），必须把启用条件写成**可判定的谓词**
  并把上游原始错误保留在响应里（照 `futu_data.py:948-969` + `v3_sources_ext.py:458-511`）。

**第 4 步：注册路由与状态探测**

* 路由：`@app.get("/api/v3/<name>")`，处理体只做「参数归一 + `asyncio.to_thread(fetch_*)` +
  `try/except → envelope`」（`v3_sources_ext.py:995-1019`），**不在路由里做业务**。
* 把路由写回 `app.state.<module>`（`v3_sources_ext.py:1021-1026`），并让上游 `register`
  合并进 `app.state.v3_sources["routes"]`（`v3_sources.py:1561`）。
* 状态探测：向 `EXT_CHAIN_SPECS` 加一条静态描述 + `build_ext_probes` 加一组探测
  （`v3_sources_ext.py:876-978`），**只读探测、不进缺省探测集**（缺省 8 条被既有单测锁定，
  `v3_fallback.py:642-648`）。
  ⚠️ 已知坑：`v3_fallback._ext_chain_rows` 目前以 `sources_deps=` 调用
  `build_ext_probes`，而函数签名是 `deps=`（`v3_sources_ext.py:922`）→ 任何 `?keys=<扩展链名>`
  都会返回 `sources/internal: TypeError`。加新链前先修这个传参名（或把签名改成 `sources_deps`）。
* MCP 桥：在 `v3_mcp.py` 的 `TOOL_DOCS` / `PARAM_DOCS`（必要时 `PARAM_DOCS_OVERRIDES`）
  补条目，页面与 Agent 才看得到口径说明（`v3_mcp.py:121/156/330-331` 是本轮范例）。

**第 5 步：测试（缺一不可）**

1. **封网络**：注入假 `fetch`/假 akshare（`v3_sources.py` 的 `Deps` 注入口就是为此存在）。
2. **降级顺序**：第一源失败 → 第二源命中，`chain` 里逐级 `ok/ms` 齐全。
3. **不伪造**：上游给 `"-"`/空档/`null` 时**字段省略或为 null**，不得为 0。
4. **全失败**：每级真实错误都在 `error`/`chain` 里，且**没有**编造的成功。
5. **预算/冷却**：超预算的源记 `skipped`；冷却中的源不发请求。
6. 运行完整回归（`cd platform && python -B -m unittest discover -s tests`）+ 三套测试全绿。

**第 6 步：文档**

* 在本文件 §二 的表里加一行（源/端点/免密/覆盖/降级位次/失败信封），
  在 `docs/e2e-and-data-gaps.md` 记一条**真机输出**（响应片段 + `as_of` + 耗时）；
* 若该源引入了新的失败信封码，补进 §2.5 的表。

---

## 六、本政策下的当前状态（2026-09-21 收尾核验）

| 能力 | 政策合规 | 运行状态 |
|---|---|---|
| A 股实时/盘口公开降级链 | ✅ 免密 | ✅ 实测可用（腾讯命中；东财断连后自动降级） |
| 北向资金 | ✅ 免密 | ⛔ **不可用**：`_head` NameError（§2.2） |
| 宏观（NBS 主源） | ✅ 免密 | ✅ 实测 5/5 指标可用 |
| 宏观（OECD 第二源） | ✅ 免密 | ⛔ **返回 `openbb/no-rows`**（根因见 §4.3） |
| roe/roa（Yahoo + 东财披露日） | ✅ 免密 | 🟡 5 标的覆盖 3/5（详见 `docs/e2e-and-data-gaps.md` 第二十六节） |
| Tushare Pro | — | ✅ 已整体移除（政策 §三） |
| OpenBB 需 key 的 provider | ✅ 零引用 | ✅ 未启用（政策 §4.2） |
