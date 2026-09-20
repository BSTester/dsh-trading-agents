# V3.0 能力对齐审计（「其他能力都对齐了吗？」）

> 审计对象：`量化交易决策平台需求规格说明书与系统详细设计文档` **V3.0**（2026-09-19），
> 全文见会话记录（用户消息，31,324 字符）。**以规格条目为唯一尺子**。
>
> **审计快照**：`git rev-parse --short HEAD` = **8b75fcc**；工作区同时有并行 agent 的未提交改动
> （`platform/server/{app,mcp_tools,v3_ops}.py` 已改，`v3_nlp.py` / `v3_risk_gate.py` / `v3_mcp.py`
> 未跟踪），**运行中的 8397 进程早于这些改动**。下文 `文件:行` 均为**该快照**的工作区行号，
> 并附**符号名**以便行号漂移后重新定位；凡「运行进程」与「工作区」不一致处**逐条注明**。
>
> **只读纪律**：全程仅 `GET` 与只读 `POST /api/wb/<只读端点>`（`POST` 是工具面读端点的调用方式，
> 用来查 `/api/wb/rt_quote` 等；**未调用**任何 `trade_*`/`sim_trade_*`/`plan-execute`/
> `confirm-decide`/`switch-mode`/`rules-decide`）。未重启服务、未改任何代码或配置。

---

## 一、汇总表

规格条目共 **31 条**：功能需求 21（FR-\*）+ 非功能需求 4（NFR-\*）+ 设计条目 6（DES-\*）。

| 状态 | 条数 | 占 31 条比例 | 占可评条目（31−2 不适用−1 未验证 = 28） |
|---|---|---|---|
| ✅ 对齐 | **5** | 16.1% | 17.9% |
| 🟡 部分对齐 | **21** | 67.7% | 75.0% |
| ❌ 缺失 | **2** | 6.5% | 7.1% |
| ⚪ 不适用（用户显式改写/豁免） | **2** | 6.5% | — |
| ❓ 未验证 | **1** | 3.2% | — |
| 🚧 已知限制（非规格条目，单列） | **1** | — | — |

- **已对齐 + 部分对齐 = 26/28 = 92.9%**；**完全缺失 2 条**（`FR-GATEWAY-002` SDK JSON-RPC 会话通道、
  `FR-TOOLS-002` 工具注册规范）。
- **部分对齐的 21 条**里，多数是「能力在、口径或覆盖面不足」：14 条属**子项缺失**（如六类因子缺
  3 类、策略族缺事件驱动/统计套利、Headless 触发缺 08:30/12:00），7 条属**通道/接线未挂载**
  （SDK、Headless、NLP、行业闸门读数）。
- **已知限制 1 条**：A 股实时行情权限（`errcode=-9 realtime quote permission required`），
  按用户口径**不计入缺口计数**，单独归入「已知限制」。

### 与「用户本轮点名的三件事」对账

| 用户要求 | 规格条目 | 现状 |
|---|---|---|
| 接入红线闸门 | 派生自 FR-EXEC-002/003 | **工作区已接**（`v3_ops.py` 引入 `v3_risk_gate`，`check_order` 用真实行业读数）；**运行进程仍是 `no-data`**（实测 `/api/v3/oms/orders` → `industry_source="no-data…按 0% 不阻断"`）→ 需重启才生效 |
| 自研 NLP | FR-STRAT-003 | **已写**（`platform/server/v3_nlp.py`，自研中文金融词典 + 否定/程度修饰 + 时间半衰，路由 `GET /api/v3/sentiment`）；**未提交、运行进程未挂载**（实测 `/api/v3/sentiment` 返回 SPA 的 `index.html`）；工作区 `app.py:921` 已把它加进装配列表 |
| A 股权限可忽略 | FR-DATA-001 降级 | 已按「最近收盘 + `as_of`」降级，页面显式标注；**归为已知限制，不重复计缺口** |
| adapter 去掉、走平台 MCP、所有交互走 MCP | FR-GATEWAY-001 | **用户豁免**：规格点名 `@helibeiqi/dsh-cordis-universal-adapter`，实测在本部署 **0.1.5-rc.2 不可用**（三段真实错误见 `docs/v3-integration.md` §1.6）；替代路径（`dsh-mcp-client` 挂 `/mcp`）已在用，且工作区新增 `v3_mcp.py` 把 `/api/v3/*` 路由**反向暴露为 MCP 工具**（`app.py` 的 `v3_mcp.register(app.state.mcp, app)`） |

---

## 二、逐条明细表

> 证据口径：`实测` = 本次对 `http://127.0.0.1:8397` 的真实只读调用（时间 2026-09-20 18:44–18:52 +08:00）；
> `测试` = 仓库内测试用例名（`platform/tests/*.py`，未在本轮重跑，按题面「测试名」口径引用）。
> 未取得证据的一律写「未验证」。

### A. Harness 集成网关（规格 §3.1）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-GATEWAY-001** | 部署 `@helibeiqi/dsh-cordis-universal-adapter` 作双向 MCP 桥 | ⚪ **不适用** | 替代：`platform/install/quant-headless/cordis.patch.yml`（`quant-platform-mcp` 行，走 `@deepseek-ai/dsh-mcp-client`）；工作区新增 `platform/server/v3_mcp.py:1` + `app.py:1026` 反向暴露 | 平台自身 `/mcp`（MCP streamable-http） | 实测 `GET /api/v3/gateway` → `channels.mcp={"status":"running","protocol":"MCP streamable-http（/mcp…）","tools":82}`；`docs/v3-integration.md` §1.6 记录 adapter 在本部署**三处硬错误**（`pnpm-workspace.yaml` 缺失 / scope 缺失 / `CallId`→`ToolCallId` 改名）→ **用户显式「adapter 可以去掉」**；注意 `channels.mcp.tools=82` 是**六域工具目录**条目数，`tools/list` 实际条数是 77 + `v3_*` 桥接工具（口径见 `docs/v3-integration.md` §1.5） |
| **FR-GATEWAY-002** | 部署 `@deepseek-ai/dsh-sdk-jsonrpc-server`，平台可驱动 Harness 会话（`initialize` / `session/prompt` / 事件流） | ❌ **缺失** | 无 | 无 | 实测 `GET /api/v3/brain` → `sources.sdk="本服务未挂载 SDK JSON-RPC 通道"`；`GET /api/v3/gateway` → `channels.sdk={"status":"unavailable","protocol":"换行分帧 JSON-RPC / stdio（未挂载）"}`；`~/.dsh/trading-venv/bin/pip list` **无 `deepseek-harness-sdk`**、仓库无任何 SDK 客户端代码（`grep -rl "deepseek_harness\|jsonrpc" platform/server` 命中 0） |
| **FR-GATEWAY-003** | 实现 Headless Runner（`dsh --profile headless`，stdout/stderr/exit 0/1/130 契约） | 🟡 **部分** | **有**：`scripts/research_duty.sh:5,16,90`（外置 shell + systemd timer，真跑 `dsh --profile headless` 并按退出码 fail-loud）**缺**：平台服务内无 HeadlessRunner 子模块 | `dsh --profile headless "<task>"` | 实测 `GET /api/v3/gateway` → `channels.headless={"status":"unavailable","reason":"本服务未挂载 Headless CLI 子通道（无 dsh --profile headless 子进程调度）"}`；`platform/install/quant-headless/README.md` §四表格给出**真跑退出码 0**的两次实测记录（dump-config 88 行、一次性任务列出 9 本地工具 + 77 MCP 工具；当时 `/api/v3/*` 桥接尚未落地，现在同一行会多出 `v3_*` 工具） |
| **FR-GATEWAY-004** | Headless 调度器：定时（08:30/12:00/16:00）+ 事件 + 流水线节点 + **外部熔断**（超时/并发/费用预算） | 🟡 **部分** | 定时链：`plugins/core/python/trading_core/daemon.py:65-105`（SH 16:00/16:05/16:10/16:15/16:25/16:30，HK 16:30-16:50，US 05:30-05:50，GLOBAL 18:50/19:05）；外置唤醒 `install/research-duty.timer`（`Mon..Fri 19:20`）；超时预算 `scripts/research_duty.sh:16`（`DSH_DUTY_TIMEOUT=1800`） | `trading_core` 调度链 + systemd | `systemctl --user list-timers` 实取：仅 1 个 `research-duty.timer`（NEXT Mon 2026-09-21 19:20）；`daemon.py` 链中**无 08:30 开盘前扫描、无 12:00 午间复盘**；`GET /api/v3/gateway` → `scheduler.recent` 为真实作业历史（19:05 `enqueue_research`、19:00 `reconcile`、18:50 `sync_calendar`…），`headless={"today":{"total":0},"breaker":null,"status":"unavailable"}` → **无并发上限、无 token/费用熔断** |
| **FR-GATEWAY-005** | 专属 Profile（`dsh-base` + `dsh-headless`，`package.json` + `cordis.patch.yml`） | ✅ **对齐** | `platform/install/quant-headless/package.json:1`（bundles=`@deepseek-ai/dsh-base`,`@deepseek-ai/dsh-headless`）+ `cordis.patch.yml`（11,898 B）+ `cordis.yml` + `pnpm-workspace.yaml` | DSH 组合树 | `README.md` §四：临时 `DSH_HOME` 真跑，`--dump-config` **退出码 0 / 88 行**，`personaPrefix` 已是量化分析师文本、16 行 `disabled` 白名单生效、`quant-platform-mcp` 已 insert；偏差：规格里的 `dsh-cordis-universal-adapter`/`dsh-quant-data-mcp` 两行被 `dsh-mcp-client` 替代（同上，用户豁免 adapter） |

### B. 平台量化工具域（规格 §3.2）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-TOOLS-001** | 六大工具域 data/alpha/ML/risk/execution/ecosystem | ✅ **对齐** | `platform/server/v3_ops.py:73`（`DOMAINS`）+ `:151`（`domain_of`）+ `:1497`（`GET /api/v3/tools`） | 工作台工具面 82 工具 + 4 个 V3 本地计算工具 | 实测 `GET /api/v3/tools` → `{"ok":true,"total":82,"domains":{data:32,alpha:7,ml:2,risk:4,execution:18,ecosystem:19}}`；`/metrics` → `quantwb_build_info{version="3.0",tools="82",domains="6"} 1` |
| **FR-TOOLS-002** | 工具定义遵循 AI-native 规范：schema 注入系统提示词 / 等长 null 对齐 / 规范 JSON + render 分离 / 全部 `isConcurrencySafe` / Skill 层 | ❌ **缺失** | `platform/server/mcp_tools.py` 仅做端点代理（`BoundTool(definition=…, handle=…)`，`:1358`） | 无 | `grep -n "isConcurrencySafe\|render\|annotations\|readOnlyHint" platform/server/mcp_tools.py` **0 命中**；工具输出是端点原始信封 JSON（无 `render` 分离、无等长 null 对齐），无 `skill/quant-research` 层 |
| **FR-TOOLS-003** | 工具数量控制在合理范围 + **工具发现代理**（`list_tools` / `call_tool` 两个间接入口） | 🟡 **部分** | 域分组/过滤：`v3_ops.py:1497`（`?domain=`）；工作区新增 `v3_mcp.py`（路由→MCP 工具桥，**按路由造工具**） | 同 FR-TOOLS-001 | 实测 `/api/v3/tools` 与 `/metrics` 均报 **82 个工具直接暴露**（`/mcp` 一次 `tools/list` 即 82 条 schema）；仓库无「发现代理」工具（`grep "list_tools\|call_tool"` 命中的是 MCP SDK 协议方法，非规格所指代理工具） |

### C. 数据层（规格 §3.3）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-DATA-001** | 富途 OpenAPI：实时报价/K线/盘口/逐笔/快照/板块 + 交易（市价/限价/**条件单**/改撤/查询/持仓） | 🟡 **部分** | 行情：`v3_market.py:55/79/133/147`；交易：`trading.py`（`SIM_MAX_QTY_ORDER_TYPES={LIMIT,MARKET}` `:121`）；f10 财报：`v3_sources.py`（`GET /api/v3/financials`） | 富途 OpenAPI（appkey 模式）/ 富途远程 MCP；`channel=openapi`（`trading-platform.json(futu_channel)`） | 实测可用：`/api/v3/orderbook?ticker=HK.00700` ✅（HK 实时有权限）；`/api/v3/financials?ticker=600519` → `source="futu/f10_detail/statements"` ✅；实测不可用：`POST /api/wb/rt_quote{codes:[SH.600000]}` → `{"code":"trading/futu-error","message":"富途业务错误（errcode=-9）：realtime quote permission required"}` → **A 股实时归已知限制**；**条件单**在代码中无实现（`grep 条件单 → 0`） |
| **FR-DATA-002** | 补 5 个开源源：Tushare / AKShare / OpenBB / SEC EDGAR / `dsh-quant-data-mcp` | 🟡 **部分（4/5）** | `platform/server/v3_sources.py:1648/1657/1670/1692/1705`（news/spot/financials/tushare/openbb）；降级链 `v3_fallback.py:1` | 实测：`akshare/stock_news_em`、`sec/companyconcept(us-gaap XBRL)`、`openbb/equity.fundamental.metrics`、`futu/f10_detail`；Tushare 走 `http://api.tushare.pro`（`v3_sources.py:94`） | 逐源实测：`/api/v3/news?symbol=600519` → `source=akshare/stock_news_em` ✅；`/api/v3/financials?ticker=AAPL` → `source=sec/companyconcept(us-gaap XBRL)`，`chain=[{source:…,ok:true,ms:5793}]` ✅；`/api/v3/openbb?symbol=AAPL` → `source=openbb/equity.fundamental.metrics` ✅；`/api/v3/tushare` → `{"ok":false,"error":{"code":"tushare/no-token","message":"TUSHARE_TOKEN 未注入…"}}`（实现有、token 无）；**`dsh-quant-data-mcp` 全仓库 0 引用**（`grep -rn` 无命中）→ 该子项未实现 |
| **FR-DATA-003** | 所有历史查询遵循 PIT；平台侧 `data/cache.py` 为唯一读取接口 | 🟡 **部分** | PIT 纪律：`v3_analytics.py:978`（回测 `t` 日只用 `≤t-1`）、`:1045`（ML 特征只用 `≤t`）、`v3_math.py:356`；缓存层：`platform/server/caches.py:1`（TTL 两级缓存，**非** `data/cache.py`） | 富途日 K + 本地缓存 | PIT：测试 `test_v3_ml.py::test_labels_are_forward_returns_not_contemporaneous`、`test_v3_analytics.py`（`test_align_*`）；规格点名的 **`data/cache.py` 不存在**（`ls platform/data/cache.py` 无此文件）→ 路径与「唯一读取接口」定位未按规格落地（现为 `caches.py` + 各模块自取） |
| **FR-DATA-004** | 数据源健康检查；主源不可用自动降级备用源 | ✅ **对齐** | `platform/server/v3_fallback.py:1`（`run_chain`）+ `:646`（`GET /api/v3/sources/status`）；探测定时器素材 `platform/install/quant-v3-probe.{service,timer}` | 逐级真实上游 | 实测 `/api/v3/sources/status` → `{"ok":true,"as_of":"2026-09-20T10:49:04Z","note":"所有降级都带 source 标注；两源都失败时如实报错（不返回占位数据）"}`；`/api/v3/spot` 全链失败体含 `chain=[{source:"akshare/stock_zh_a_spot_em",ok:false,ms:22443,attempts:[…]}]` + `error.message` 列明「按顺序试过 5 个接口全部失败」；测试 `test_v3_fallback.py`（16 例，含 `test_all_failed_returns_none_none_and_full_timeline`） |

### D. 策略层（规格 §3.4）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-STRAT-001** | 多因子选股：价值/成长/动量/质量/情绪/另类 **六类**；PDAT→PAAT→PCPT→PRT→PET 五阶段 | 🟡 **部分** | 五阶段：`v3_analytics.py:789`（`run_pipeline`，`:850-880` 逐阶段）；因子：`plugins/workbench/python/factors.py:27`（`FACTOR_SIGN`：mom_20/mom_60/vol_20/trend/rsi_14 + 估值） | 富途日 K + `workbench/factors` | 实测 `/api/v3/brain` → `decision.stages` 真取到 `{"PDAT":{"bars":240,"universe":["US.NVDA","US.MSTR"]},"PAAT":{"analyzed":2,"scoreSource":"workbench/factors(z)"},"PCPT":{"longs":[…]},"PRT":{"capped":true,"weightPctPerName":2.0},"PET":{"proposals":2}}`；**六类因子只覆盖动量/波动/趋势/RSI + 估值（价值）**：成长、质量、另类**无实现**（`grep 成长\|质量\|另类` 在 server/ 无因子实现）；情绪类由 FR-STRAT-003 的 `v3_nlp` 补（工作区） |
| **FR-STRAT-002** | 策略生成与优化：多因子 / ML（Lasso/LightGBM/MLP）/ **事件驱动** / **统计套利**；数千次回测 + 热力图 | 🟡 **部分** | `v3_ml.py:1`（特征 + Lasso/GBDT/MLP 纯计算核）、`v3_analytics.py:1030`（同口径评估）、`:1188`（参数网格热力图）；端点 `v3_analytics.py:1185/1199/1213` | 富途日 K（`series`）；实测 ML 端点落在 `akshare/sina` | 实测 `/api/v3/ml/models?ticker=SH.600519` → `{"ok":true,"source":"akshare/sina","as_of":"2026-09-11","sources":{"SH.600519":"akshare/sina"}}`；`/api/v3/ml/sweep`、`POST /api/v3/ml/backtest` 均在探针表内且返回真实回测；**事件驱动、统计套利两类策略 0 实现**（`grep 统计套利\|event_driven\|pairs → 0`）；LightGBM 以 `numpy-gbdt-lite` 替代（`docs/e2e-and-data-gaps.md` 第五轮 §二如实标注 `impl`）；测试 `test_v3_ml.py`（48 例，含 PIT 反证） |
| **FR-STRAT-003** | NLP 情绪：实时情绪评分 / **事件识别** / 多源情绪融合 / 情绪因子构建 | 🟡 **部分**（工作区已写，未挂载） | `platform/server/v3_nlp.py:1`（词典 `:154`、否定/程度 `:162`、时间半衰 `:197`、路由 `:936 GET /api/v3/sentiment`）；工作区 `app.py:921` 已加入装配列表 | 资讯（`akshare/stock_news_em`，经 `retry_akshare`） | **运行进程未挂载**：实测 `GET /api/v3/sentiment` 与 `/api/v3/nlp` 均返回 SPA 兜底 HTML（`<!DOCTYPE html>…`），非 JSON → 该能力**当前不可用**；测试 `platform/tests/test_v3_nlp.py`（55 例，含 `test_route_is_registered`、`test_no_news_returns_null_score_not_zero`）为工作区新增、未提交；**「事件识别」无独立实现**（`)` 只有事件极性词表，无事件抽取/分类器） |

### E. 执行层（规格 §3.5）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-EXEC-001** | 基于富途 OpenAPI 的 OMS，信号→订单全生命周期 | 🟡 **部分** | `v3_ops.py`（`OmsLedger`）、`v3_db.py:119`（`orders`/`oms_orders` 落库）、`v3_ops.py:1574`（`GET /api/v3/oms/orders`）、`:1586`（`POST /api/v3/oms/sync`） | 台账（sim 模拟盘）+ 富途 `sim_trade_*`；`nav_source="sim-ledger(equity.current)"` | 实测 `/api/v3/oms/orders` → `{"ok":true,"nav":…,"nav_source":"sim-ledger(equity.current)","drawdown_source":"sim-ledger(max_drawdown)","stages":{"manual":10},"orders":[…]}`；**live 写协议未做真实下单验证**（`docs/P4-live-trading.md` 自述），故只能算部分 |
| **FR-EXEC-002** | 分级审批：阈值内自动执行 / 超阈值人工确认 / 红线强制阻断 | 🟡 **部分** | `v3_ops.py:318`（`check_order` 纯函数，`:94` `LIMITS={"singlePct":2.0,"industryPct":20.0,"drawdownPct":15.0}`，`:97` `STAGES`） | 台账 NAV + 回撤 + 行业读数 | 实测 `/api/v3/oms/orders` → `stages={"manual":10}`（当前无 auto/blocked 行）；测试 `test_v3_ops.py::test_check_order_thresholds`（10 行）；**强制阻断的行业读数在运行进程仍是 `no-data`**：实测 `industry_source="no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）"` → 行业红线**不下闸**；工作区已改（`v3_ops.py:66` `import v3_risk_gate`、`:861` `industry_context`、`:948` 传入 `check_order`），测试 `test_v3_risk_gate.py`（工作区新增）→ **待重启生效** |
| **FR-EXEC-003** | 风控：事前（持仓限制/单笔上限/行业集中度/资金）+ 事中（回撤/杠杆/流动性）+ 事后（归因/最大回撤/VaR）+ VaR/CVaR/Beta/Alpha/IR/Kupiec | ✅ **对齐** | `v3_math.py:188`（`kupiec_pof`）、`:300-340`（`beta`/`alphaAnnPct`/`ir`）、`v3_analytics.py:447`（`risk_analytics`）、`:1134`（`GET /api/v3/risk/analytics`）；行业：`v3_industry.py:508` | 富途日 K + `sim-ledger` 权益 + 富途板块 | 实测 `/api/v3/risk/analytics?limit=60` → `{"ok":true,"sources":{"kline":"futu/quote_history_kline","nav":"sim-ledger(equity.current)","errors":[]},"benchmarkSource":…}`；`/api/v3/risk/industry?market=SH` → `sources={"plate":"futu/info_owner_plate","weights":"platform/portfolio（自选池等权（8 只））","nav":"sim-ledger(equity.current)"}`；测试 `test_v3_analytics.py::test_var_uses_the_historical_quantile_and_cvar_the_tail_mean`、`::test_beta_estimator_is_population_cov_over_sample_variance`。**杠杆/流动性**两项事中口径未见实现（`grep` 无对应读数）→ 该两条子项并入本行的部分说明 |

### F. 监控与可视化（规格 §3.6）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **FR-MON-001** | 每笔交易记录完整决策链：信号溯源（因子值/模型输出/情绪评分）+ 决策快照 + 执行链路 | 🟡 **部分** | `v3_analytics.py:789`（stage 落盘）+ `v3_ops.py:1629`（`GET /api/v3/brain`）；执行链：`v3_ops.py:1616`（`/api/v3/audit`）+ `:1591`（`/api/v3/events`）；落库 `v3_db.py:156` 等 | 台账 + 富途事件/公告 + 审计链 | 实测 `/api/v3/brain` → `decision.proposals[].basis="综合动量 z=0.7071（mom_20=0.36952）"` + `stages`（PDAT…PET）+ `asOf`/`market` → **信号溯源有（当前只有动量因子）、决策快照有**；**模型输出与情绪评分未进决策链**（`basis` 无 ML/情绪项；`v3_nlp` 未挂载）→ 部分 |
| **FR-MON-002** | 7 类仪表板：系统概览/行情图表/策略分析/决策面板/风险监控/交易记录/**Harness 会话状态（Agent Loop、工具调用统计）** | 🟡 **部分** | 前端 10 页：`platform/web-pro/src/pages/{overview,brain,market,strategy,risk,execution,gateway,tools,settings,research}.jsx`；后端 `app.py:943`（overview）+ 各页端点（见 §五） | 全部经 `GET /api/v3/*` / `POST /api/wb/*` | 前 6 类**对齐**（页面与端点逐页对应，见 §五表格）；**Harness 会话状态只有「不可用 + 原因」**：实测 `/api/v3/brain` → `sdk.status="unavailable"`、`headless.status="unavailable"`、`turns":[]`，Agent Loop 运行状态**取不到**；工具调用统计**有**（`/metrics` → `quantwb_mcp_calls_total 19`、`quantwb_tool_calls_total{tool="audit"} 1`…） |
| **FR-MON-003** | 记录每次 headless 调用（提示词/stdout/stderr/退出码/耗时/token 估算）并写平台数据库 | 🟡 **部分** | 存储层已建：`v3_db.py:156`（`headless_log` 表）+ `:218`（JSONL 冷备映射）；**生产者缺失**（FR-GATEWAY-003 未挂载） | 无（表空） | 实测 `/api/v3/brain` → `headless={"today":{"total":0,"success":0,"failed":0,"avgMs":0,"killed":0},"breaker":null,"status":"unavailable"}` → **无任何 headless 调用记录**；`docs/e2e-and-data-gaps.md` 第四轮 §7.3 登记该表 schema（`headless_log(id,started_at,success,exit_code,duration_ms,tokens_estimate,payload)`） |

### G. 非功能需求（规格 §4）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **NFR-PERF §4.1** | 行情延迟<100ms、下单<500ms、MCP 调用<2s、Headless<30s、SDK 握手<30s、可用性 99.9% | ❓ **未验证** | — | — | 本次为**只读审计**，未做压测；且 A 股实时行情权限缺失（`-9`）使「行情延迟」指标在当前权限下不可测；`/metrics` 只有 `quantwb_mcp_call_duration_seconds`（进程内均值，实测 `1.592`s，为**均值非 P95**，gateway 页自注「metrics 无 P95 口径」）。**故按「未验证」如实记账，不猜** |
| **NFR-SEC §4.2** | 密码/token 加密存储、API token 经环境变量或密钥管理注入、全交易操作审计、多级权限、Harness MCP 仅绑 127.0.0.1 | 🟡 **部分** | `platform/server/v3_credentials.py:55`（原子写 + 0600）、`app.py:775`（token 中间件：`/api/*` 与 `/mcp` 需 Bearer）、`config.py:13`（`host:"127.0.0.1"`）；凭据 `~/.dsh/v3-credentials.json` 0600 | 页面上报「是否注入 + 来源」，不回显值 | 实测 `/api/v3/credentials?action=status&key=tushare_token` → `{"ok":true,"keys":[…]"已配置/未配置…掩码尾号"}`（**无明文**）；`/api/v3/settings` → env 表逐项 `injected/source`；页面核查：`settings.jsx` 凭据/env/审计表**不回显密钥**（§五）。缺口：**多级权限控制未见实现**（无角色/权限模型），「加密存储」实为 0600 文件权限（非加密） |
| **NFR-COMPAT §4.3** | `@deepseek-ai/dsh-tools` 与 MCP 处 Developer Preview，需版本适配与降级预案 | 🟡 **部分** | 预案证据：`docs/v3-integration.md` §1.6（adapter 三段失败 → 替代方案）；`docs/architecture.md` 有意差异清单；`platform/tests/test_mcp_parity.py`（MCP 面覆盖率+封闭 schema 的**自动化**门禁） | — | 有**真实降级案例与文档**；MCP 工具面已有自动化 parity/schema 测试，但**无 CI 门禁、无 dsh 版本矩阵** → 部分 |
| **NFR-EXT §4.4** | 数据源适配器插件化、策略引擎热加载、执行算法可插拔 | 🟡 **部分** | 数据源：`v3_sources.py:Deps`（可注入 fake，`:348-360`）；降级链 `v3_fallback.run_chain` 可编排 | — | 测试 `test_v3_sources.py`（58 例，Deps 注入）+ `test_v3_fallback.py`（16 例）证明**数据源与降级链可插拔**；**策略热加载、执行算法可插拔未见实现/证据**（策略为进程内函数）→ 部分 |

### H. 系统设计条目（规格 §5/§6/§8/§10）

| 编号 | 需求摘要 | 状态 | 实现位置 | 真实数据来源 | 验证证据 |
|---|---|---|---|---|---|
| **DES-5.1** | K8s 多服务（scheduler/strategy/risk/data/exec/gateway/web）+ Kafka + Redis + PostgreSQL | ⚪ **不适用** | 单容器 + SQLite：`platform/server/v3_db.py:119`；`platform/deploy/` | SQLite `<DSH_HOME>/v3.db` + 文件冷备 | **用户显式改写**（「不用集群，单容器就行」）；`docs/e2e-and-data-gaps.md` 第四轮 §7.4/§7.5 登记迁移与并发证据（10 线程×10 / 4 进程×25 全 0 异常）；与规格的 K8s/Kafka/Redis/PG 架构**不一致但为用户授权** |
| **DES-5.2** | Gateway 三子模块（MCP Bridge / SDK Client / Headless Runner） | 🟡 **部分** | 同 FR-GATEWAY-001/002/003 | — | 实测 `/api/v3/gateway`：MCP `running`，SDK `unavailable`，Headless `unavailable` → **1/3 挂载**（本行与 FR-GATEWAY-002/003 同源，不重复计分） |
| **DES-6** | Profile 组装顺序（bundles patch → profile `cordis.patch.yml` → home patch → `--patch`）；量化 system prompt（单笔 2%/行业 20%/回撤 15%） | ✅ **对齐** | `platform/install/quant-headless/cordis.patch.yml`（`quant-system-prompt` 段）+ `package.json` | DSH 组合 | `README.md` §四实测 `--dump-config` 退出码 0，`personaPrefix` 为量化分析师文本；阈值 2%/20%/15% 与 `v3_ops.py:94` `LIMITS` **逐项一致**（规格 §6.2 同值） |
| **DES-8.2** | 环境变量表（DSH_HOME / DEEPSEEK_API_KEY / QUANT_MCP_NODE / QUANT_MCP_SERVER / QUANT_MCP_CWD / QUANT_MCP_LOG / FUTU_OPEND_HOST / FUTU_OPEND_PORT / TUSHARE_TOKEN） | 🟡 **部分** | `platform/server/app.py` 设置页数据面 + `v3_ops.py` 设置组装 | 环境变量实测探测 | 实测 `/api/v3/settings.env` → `DSH_HOME injected=true`；`DEEPSEEK_API_KEY / QUANT_MCP_NODE / QUANT_MCP_SERVER / QUANT_MCP_CWD / QUANT_MCP_LOG / FUTU_OPEND_HOST / FUTU_OPEND_PORT / TUSHARE_TOKEN` **全部 `injected=false`**。原因不同：MCP 侧改走 `@deepseek-ai/dsh-mcp-client`（不需要 QUANT_MCP_* 三件套）；富途改走 **appkey 模式**（实测 `futu.openapi.mode="appkey"`，不需要 OpenD host/port）；`DEEPSEEK_API_KEY` 与平台服务无关（服务不承载 LLM 循环）。**规格 §8.2 的变量表已过时，但替代路径都工作** → 部分 |
| **DES-8.3** | Prometheus + Grafana；9 类指标与告警阈值（成功率<95%、耗时>60s、SDK 会话>10、工具延迟>5s、失败率>5%、数据源断连、数据延迟>5min、订单延迟>1s、风控阻断突增） | 🟡 **部分** | `platform/server/observability.py:819`（`GET /metrics`）、`:830`（`POST /api/v3/metrics/probe/refresh`）；规则 `platform/deploy/monitoring/{prometheus.yml,alerts.yml,dump_metrics.py}` | 进程内计数 + 只读缓存（**抓取路径零外部请求**） | 实测 `GET /metrics` → `text/plain`（`quantwb_up 1`、`quantwb_build_info{version="3.0",tools="82",domains="6"} 1`、`quantwb_mcp_calls_total 19`、`quantwb_mcp_errors_total 0`、`quantwb_mcp_call_duration_seconds 1.592`、`quantwb_http_requests_total 15`…，共 124 行 HELP/样本）；`alerts.yml` 实测 **23 条 `alert:` 规则**（与提交信息一致，`promtool check rules SUCCESS`）；**Grafana 未部署**、**9 类里「SDK 会话活跃数」无法覆盖**（SDK 未挂载）、「Headless 成功率/耗时」恒空（Headless 未挂载）→ 部分 |
| **DES-10** | 五项关键决策（三通道分工 / Headless 不依赖会话状态 / 工具粒度控制 / 外部熔断保护 / 版本锁定与降级预案） | 🟡 **部分** | 同 FR-GATEWAY-002/003/004、FR-TOOLS-003、NFR-COMPAT | — | 决策 1 **部分**（通道 1/3 挂载）；决策 2 **不成立**（Headless 未挂载，但 `scripts/research_duty.sh` 确实每次新建一次性会话）；决策 3 **未落地**（82 工具直暴露）；决策 4 **部分**（超时有，并发/预算无）；决策 5 **部分**（有案例无门禁） |

---

## 三、缺口清单

### 3.1 规格要求但未实现（真缺口）

| # | 缺什么 | 影响 | 建议补齐方式 | 工作量粗估 |
|---|---|---|---|---|
| G1 | **SDK JSON-RPC 通道**（FR-GATEWAY-002）：无 `dsh-sdk-jsonrpc-server` 挂载，无 SDK 客户端代码，未安装 `deepseek-harness-sdk` | 「平台前端驱动 Harness 会话」的**人工研究交互通道整条缺**；`决策大脑`页 SDK 区块恒为「不可用」；规格 §7.1 的人工研究数据流走不通 | 在 `quant-headless` 同法建 `sdk` profile（`@deepseek-ai/dsh-sdk-jsonrpc-server` + `dsh-mcp-client`），平台侧加 `HarnessSessionManager`（`initialize`/`session/prompt`/事件流），端点 `POST /api/v3/session/prompt` | **大**（3–5 人日；含 profile 验证 + 事件流前端） |
| G2 | **Headless 通道与外部熔断**（FR-GATEWAY-003/004）：服务内无 HeadlessRunner；无 08:30/12:00 触发；无事件触发；无并发/费用预算；`breaker=null` | 自动唤醒大脑只剩外置 `research-duty.timer`（周一至五 19:20 单一作业）；L3 日志（FR-MON-003）恒空；规格 §7.2 自动触发数据流不成立 | 把 `scripts/research_duty.sh` 的逻辑收进服务（`HeadlessRunner` + 并发信号量 + 超时 kill + token 预算 + 落 `headless_log`），调度链补 08:30/12:00 两个 job，事件触发挂 `events/risk` 信号 | **中**（2–3 人日，服务内 runner 约 300 行 + 测试） |
| G3 | **工具注册规范**（FR-TOOLS-002）：无 `isConcurrencySafe`、无 `render` 分离、无等长 null 对齐、无 `skill/quant-research` | 工具输出是原始信封 JSON，模型侧对齐成本高；与规格「AI-native」目标不符 | 在 `mcp_tools` 的 `BoundTool` 上加注解与 `render`；或在工作区 `v3_mcp.py` 桥层为只读路由补 `readOnlyHint`/`render` | **中**（1–2 人日，逐域改写输出契约） |
| G4 | **工具发现代理**（FR-TOOLS-003）：82 工具直暴露，无 `list_tools`/`call_tool` 间接入口 | 上下文窗口压力（规格决策 3 明确要避免） | 增加 `quant_discover{list_tools,call_tool}` 两个元工具，其余工具按域**按需**注册 | **小–中**（0.5–1 人日） |
| G5 | **因子三类缺失**（FR-STRAT-001）：成长、质量、另类因子无实现 | 选股只吃动量/波动/趋势/RSI/估值；`basis` 单薄（实测只有 `mom_20`） | 复用富途 `f10_detail`（26 sections）建成长/质量因子；另类用 `capital_flow`/`short_interest`/期权 | **中**（2–3 人日 + 数据校验） |
| G6 | **事件驱动 / 统计套利策略**（FR-STRAT-002） | 策略族只有动量基线 + ML 三模型 + 参数扫描 | 事件驱动可挂 `events`/`economic_calendar_hot`；统计套利需配对协整（既有 `correlation` 可作基础） | **大**（各 2–4 人日，含 PIT 回测口径） |
| G7 | **NLP 未挂载 + 事件识别缺失**（FR-STRAT-003） | 情绪因子进不了决策链；`/api/v3/sentiment` 当前 404→SPA | 提交并重启（工作区已注册）；补事件抽取/分类（规则或词表 + 模式） | **小**（挂载 0.5 人日）/ **中**（事件识别 1–2 人日） |
| G8 | **行业红线在下单闸门仍是 no-data**（FR-EXEC-002，运行进程） | 行业集中度红线**不下闸**，只在观测侧告警；`blocked_industry` 恒 0 | 工作区已接（`v3_ops` 引入 `v3_risk_gate`）→ **只需重启验证** + 补一条端到端断言 | **小**（0.5 人日，含测试） |
| G9 | **`dsh-quant-data-mcp`**（FR-DATA-002 第 5 源） | A 股免密钥数据面缺一路；现由富途/AKShare 覆盖，实际影响小 | 若要严格对齐：接入该 MCP stdio server 并加进 profile bundles；否则建议**在规格层面注销该条** | **小**（0.5 人日）或**规格修订** |
| G10 | **`data/cache.py` 唯一读取接口**（FR-DATA-003 命名） | PIT 纪律分散在 `v3_analytics/v3_ml/v3_math`，没有单一入口可审计 | 把 `caches.py` 升级为 `data/cache.py` 语义（PIT 读取唯一入口）并让各模块经它取数 | **中**（1–2 人日，回归面大） |
| G11 | **多级权限控制**（NFR-SEC §4.2） | 只有「token 网关 + 页面口令闸门」，无角色/权限分级 | 在 `/api/*` 中间件加角色（只读/研究/交易）与端点白名单 | **中**（1–2 人日） |
| G12 | **性能与非功能未验证**（NFR-PERF §4.1） | 六项延迟/可用性指标无实测基线 | 加只读压测脚本（探针 + 百分位统计）并写入 `/metrics` P95 | **小**（0.5–1 人日） |

### 3.2 规格未要求、但比预期弱（不是缺口，是欠账）

| # | 弱在哪 | 影响 | 建议 | 工作量 |
|---|---|---|---|---|
| W1 | **前端硬编码阈值**（`overview.jsx:397/406/413`、`risk.jsx:835/837`、`settings.jsx:1290`、`execution.jsx:395/400`） | 后端改 `LIMITS` 后页面仍显示旧值；`overview.jsx:708` 甚至声称「回撤阈值取自台账」（**不实**） | 改为读 `/api/v3/oms/orders` 的 `industry_limit_pct` 等字段，或新增 `GET /api/v3/risk/limits` | **小**（0.5 人日） |
| W2 | **加载/失败态渲染成 0**（`gateway.jsx:93-94→107-109`、`tools.jsx:197-200/248/286`、`research.jsx:169-181`） | 请求未回来时页面显示「今日调用 0 次 / 失败 0 次 / 0 ms」，与真实 0 不可区分 | 统一 `fmt.dash` 或显式 `loading`/`error` 分支（`undefined` 在 antd `Statistic` 会渲染成 `0`，见 §五） | **小**（0.5 人日） |
| W3 | **`strategy.jsx:503-506` 渲染字面量 `null`** | 回测指标缺失时卡片显示 `null%`（antd `Statistic` 对 `null` 走 `String(value)`） | 传 `"—"` 或用 `formatter` | **极小**（0.2 人日） |
| W4 | **固定文案当成事实**：`settings.jsx:1314`「LIVE 大额订单需双人复核」（**无实现**）、`execution.jsx:34/1042/1066` TTL 回退 120s、`brain.jsx:252`「0 条」、`risk.jsx:686`「满刻度 30%」、`risk.jsx:548-549` 固定「台账只有 1 个点位」 | 用户会把未实现的风控/参数当既有控制 | 加「未实现/未取到」标注，或删除该文案 | **小**（0.5 人日） |
| W5 | **`/api/v3/settings` 数据源状态自相矛盾**（`v3_ops.py:1323-1325` 写死 `SEC EDGAR available:false`＋「本服务未实现 SEC EDGAR 客户端」；`:1328-1330` 要求 `tushare` **包**可导入才算可用） | 与事实相反：`/api/v3/financials?ticker=AAPL` 实测 `source=sec/companyconcept(us-gaap XBRL)`；Tushare 走 HTTP 不需要包 → 即使配了 token 也可能显示「不可用」 | SEC 改为按 `v3_sources` 真实探测；Tushare 可用性判据去掉 `_module_available('tushare')` | **小**（0.3 人日） |
| W6 | **`docs/v3-integration.md` 已过时**：§三/§四称页面在 `platform/web/public/v3/`、`/` 307 跳 `/v3/index.html`、`bash platform/tools/verify_pages.sh` | 该目录**已不存在**（`ls platform/web` → No such file）；现为 `platform/web-pro`（AntD Pro，10 页）→ 文档把人引到错路径 | 更新 §二·五/§三/§四 | **小**（0.3 人日） |
| W7 | **事件驱动/统计套利以外的因子覆盖**（见 G5）与 **杠杆/流动性事中口径**（FR-EXEC-003 子项） | 事中风控只覆盖回撤 | 明确「本期不做」或补读数 | **中** |

---

## 四、过度实现 / 偏离清单

| # | 项 | 与规格的关系 | 证据 |
|---|---|---|---|
| O1 | **82 工具目录 + 116 MCP 工具面 / 6 域** | 规格参考 dsh-quant「59 工具·6 域」，本实现六域目录 **82** 条；MCP 面 **116** 件 | `/api/v3/tools` → `total:82`；`/metrics` → `quantwb_tools{scope="domain"} 82` 与 `{scope="mcp"} 116`；MCP `tools/list` = 116（`v3_*` 39 件）。域数一致（6）✅ |
| O2 | **V3→MCP 反向桥**（`v3_mcp.py`，把 39 条 `/api/v3/*` 路由暴露成 MCP 工具） | 规格只要「一个平台 MCP server 暴露量化工具」；本实现是**把已存在的 HTTP 面镜像成 MCP**（同一函数对象，无第二份业务逻辑） | `app.py:1032` `v3_mcp.register(app.state.mcp, app)`；**已重启生效**：`tools/list` = 116（77 + 39），只读标注 36 件；对应用户指令「所有跟平台的交互都走 mcp 接口」。adapter 已从 `quant-headless` 全部安装材料中删除 |
| O3 | **SQLite 持久化替代 PostgreSQL/Redis** | 规格 §5.1 要 PG + Redis；本实现单文件 SQLite + 文件冷备 | 用户授权（「数据库可先用 sqllite」）；`v3_db.py:119` `TABLE_SPECS`；`docs/e2e-and-data-gaps.md` 第四轮 §7 |
| O4 | **富途限流治理**（令牌桶 + 单飞 + 退避 + 冷却 + 统一 `futu/rate-limited` 信封） | 规格未要求 | `platform/server/v3_ratelimit.py`（833 行）+ `test_v3_ratelimit.py`（40 例）；`app.py:906` `is_futu_endpoint` 分流 |
| O5 | **三市场过滤 + 分市场基准 + 交易日历/节假日自动获取** | 规格未要求（只泛提「交易日历」） | `?market=SH|HK|US`（`app.py:984-1022`）；`v3_calendar_source.py`（785 行，链：进程内 TTL → 磁盘 → 富途 `info_trading_days` → AKShare → 人工兜底）；实测 `/api/v3/markets/calendar` → `source="platform/market_calendar"`、`holidays_loaded`、`calendar_source` |
| O6 | **研报页 + PDF/Markdown 导出** | 规格 §3.6 未列研报页 | `v3_research.py:121/160/229`（含服务端 PDF 生成）；前端 `pages/research.jsx`（第 10 页，规格 7 类里没有） |
| O7 | **ML 用 numpy 实现替代 sklearn/LightGBM** | 规格点名 Lasso/LightGBM/MLP | `v3_ml.py:1`（实现名 `numpy-lasso`/`numpy-gbdt-lite`/`numpy-mlp`），`docs/e2e-and-data-gaps.md` 第五轮如实标注 `impl`；**口径偏离但声明诚实** |
| O8 | **`source` 字段不按规格写死** | 规格备注写死 `source=futu/quote_history_kline` | 实现上报**真实来源**（`_dominant_source`，逐标的 `sources` 另附）；文档已登记此有意偏离 |
| O9 | **外置 `research-duty.timer` 承担 Headless 唤醒** | 规格要平台调度器内实现 | `install/research-duty.timer`（`Mon..Fri 19:20`，实测已安装且 active）；服务内通道仍 `unavailable` → **能力达成但归属与规格不同** |
| O10 | **既有 WP6–WP26 能力原样保留**（工作台 82 端点、审计链、OMS、L3 队列…） | 规格是「完全重构」，实现选择**复用既有工具面同一 handle** | `app.py` 注释「与既有 `/api/wb/*` 共存…不新造第二事实源」——**偏离「重构」字面、保留能力**，另有 docs 登记 |
| O11 | **指标名与规格不同** | 规格点名 Prometheus+Grafana 与 9 类指标 | 实际指标族前缀 `quantwb_*`（自研 `observability.py`），Grafana 未部署；`alerts.yml` 23 条规则覆盖规格 9 类中的 7 类 |

---

## 五、诚实性核查（前端逐页「每个数字是否有真实来源」）

抽查 10 页（覆盖用户点名的 5–8 页要求）：`overview / brain / market / strategy / risk / execution /
gateway / tools / settings / research`。结论：**未发现「示例数据」字样，未发现编造的业务数字**
（如假的行情、假的收益），但发现 **12 处「固定文案/回退值被当作真实数据展示」**，逐条点名如下。

### 5.1 已点名问题（文件:行 + 为什么会被误读）

| # | 位置 | 内容 | 为什么会被误读 |
|---|---|---|---|
| H1 | `platform/web-pro/src/pages/gateway.jsx:93-94` → `:107-109` | `Number(metrics.mcp?.calls ?? 0)`、`errors ?? 0`、`Number(metrics.mcp?.avgMs ?? 0)`；MCP 卡在 `:215` **无条件渲染**（只判 `gateway.loading`） | `/api/v3/metrics` 加载中或失败时，卡片显示「今日调用 0 次 · 失败 0 次 · 平均延迟 0 ms」——与「真的 0」不可区分 |
| H2 | `pages/tools.jsx:197-200` | `value={… : undefined}` / `tools.loading ? undefined : localCount` | **实测 antd 行为**：`Statistic` 默认 `value = 0`（`node_modules/antd/es/statistic/Statistic.js:25`），`undefined` 渲染成 **`0`** → 加载中「工具域/一级/直通/今日调用」四项全 0 |
| H3 | `pages/tools.jsx:248`→`:259`、`:286` | `Number(callsByTool[tool?.name] ?? 0)` 渲染成「今日 0」 | metrics 失败时 `callsByTool={}`，每个域/工具都显示「今日 0」 |
| H4 | `pages/strategy.jsx:503-506` | `value={fin(metrics.X) ? Number(metrics.X) : null}` | **实测 antd 行为**：`Number.js` 走 `String(value)` → `null` 渲染成字面量 **`null`**（带 `suffix="%"` 即 `null%`） |
| H5 | `pages/overview.jsx:397 / :406 / :413`（渲染 `:727`「上限 {row.limit}%」） | 硬编码 `limit: 2 / 20 / 15` | 以「数据」形态展示风控上限；后端真值在 `v3_ops.py:94`，且 `/api/v3/oms/orders` 已返回 `industry_limit_pct`——页面**忽略接口值** |
| H6 | `pages/overview.jsx:708` | 文案「回撤阈值取自台账，为台账口径」 | **与代码不符**：15 是 `:413` 的常量，不是台账读数（不实声明） |
| H7 | `pages/overview.jsx:882` | `(order.risk?.reasons) \|\| ["阈值内"]` | `risk.reasons` 缺失时断言「阈值内」= 凭空给出风控结论 |
| H8 | `pages/risk.jsx:835 / :837` | `threshold: "≤ 权益 2%（OMS check_order 口径）"` + `singleRatio > 2` 决定「正常/超限」标签 | 判定用的 2% 是前端字面量，非接口阈值（同页 `:432` 反而诚实写「未从台账回读到阈值」） |
| H9 | `pages/risk.jsx:686` | 「满刻度 30%」 | `:706` 的 BarList **不传 max**（自动缩放）→ 图上满刻度不是 30% |
| H10 | `pages/execution.jsx:34` → `:1042` → `:1066` | `FALLBACK_TTL_SECONDS = 120` → 「TTL 120 秒」 | `confirmation.ttl_ms` 缺失时把前端默认值当**服务端 TTL** 展示 |
| H11 | `pages/settings.jsx:1314` | 「LIVE 大额订单需双人复核后执行（阈值由风控引擎统一下发）」 | `platform/server`、`scripts` 全域**无「双人复核」实现** → 用户会以为存在该控制 |
| H12 | `pages/settings.jsx:1290`；`pages/execution.jsx:395/400` | 「单笔 ≤ 2% · 行业 ≤ 20% · 回撤 ≤ 15%（v3_ops.LIMITS）」 | 同为前端硬编码阈值（H5 同族） |

补充（非数字、但属文本改写）：
- `pages/tools.jsx:73-81` 用**码位构造** `U+793A U+4F8B`（「示例」）并把上游工具说明里的该词替换成「**样例**」（`:359` 也作用于探测返回文本）。源码注释自述是「页面文本审计要求页面不出现该类字样」。
  - 事实核查：**页面确实不出现「示例数据」**；但页面会出现同义字「样例」，且**工具说明文本不再逐字等于上游返回**。这与 `docs/v3-integration.md` §五「不用占位数据」的精神接近、与「逐字保留」不一致，**建议改为对上游原文加引号并标注来源，而不是替换词**。
- 未发现 `mock`/`demo`/`假数据`/`TBD` 等字样；`dataDomain.jsx:50/77/120/145` 的 `600519`/`AAPL` 是**按需查询的真实标的默认值**（非展示指标），`settings.jsx:200` 的 `exec_window_minutes` 默认 `30` 会**回写**（`:585`）——属「默认配置」而非「编造数据」，但建议标注「默认值」。

### 5.2 后端诚实性问题（同属「数字来源」问题，按题面要求一并点名）

| # | 位置 | 内容 | 证据 |
|---|---|---|---|
| B1 | `platform/server/v3_ops.py:1323-1325` | 写死 `{"name":"SEC EDGAR…","available":False,"detail":"无数据源：本服务未实现 SEC EDGAR 客户端…"}` | 与事实矛盾：实测 `GET /api/v3/financials?ticker=AAPL&statement=income&periods=2` → `{"ok":true,"source":"sec/companyconcept(us-gaap XBRL)","chain":[{"source":"sec/companyconcept(us-gaap XBRL)","ok":true,"ms":5793}]}`；`docs/v3-integration.md` §一也写 SEC EDGAR ✅ 可用 → **文档/接口/实现三方矛盾** |
| B2 | `platform/server/v3_ops.py:1328-1330` | Tushare 可用性 = `token and _module_available("tushare")` | 实现在 `v3_sources.py:94` 走 **HTTP** `http://api.tushare.pro`，**不需要包**；若只注 token 而包不在，页面会误报「不可用」 |

### 5.3 诚实性做得好的地方（反向证据，避免只列缺点）

- `market.jsx` **干净**：缺失字段一律显式「无数据源」（`:538/:572/:584/:596/:963-965/:1213`），每块带 `source + as_of`；`plates` 接口自注「板块涨跌幅需富途实时行情权限；无权限时只有板块清单，不填占位」（实测 `/api/v3/plates` 返回体 `note` 原文）。
- `brain.jsx:989`、`overview.jsx:803` 明确写「不用设计稿里的占位 session / 轮次 / token 顶替」；`risk.jsx`/`execution.jsx`/`strategy.jsx` 各有「无数据源项（逐项写明原因，不填占位数字）」专卡。
- 所有图表序列均派生自接口状态（已逐一核对调用点：`strategy.jsx:441/508`、`gateway.jsx:442`、`tools.jsx:447`、`brain.jsx:705`、`risk.jsx:157/706/770`、`overview.jsx:220/818`、`market.jsx:870`），**无静态图表数组**。
- 写入口（`strategy/run`、`ml/backtest`、`oms/sync`、`plan-execute`、`confirm-decide`、`switch-mode`、`credentials`）**全部在点击处理器内**，加载/刷新路径只发读请求。

### 5.4 页面 ↔ 端点对应（本次实测覆盖）

| 页面 | 端点 |
|---|---|
| `overview` | `/api/v3/overview?market`、`/oms/orders`、`/strategy`、`/metrics`、`/brain`、`/settings`、`/audit?window=120` |
| `brain` | `/api/v3/brain`、`/strategy`、`/factors/matrix`、`/overview`、`/metrics`、`/audit`、`/news` |
| `market` | `/api/v3/market`、`/market/watchlist`、`/factors/matrix`、`/plates`、`/orderbook`、`/markets/calendar`、`/metrics`、`/brain`、`/overview`、`/news`、`/financials`、`/tushare`、`/openbb`、`/spot` |
| `strategy` | `/api/v3/strategy`、`/factors/matrix`、`/ml/sweep`；`POST /ml/backtest`、`POST /strategy/run`（点击） |
| `risk` | `/api/v3/risk/analytics`、`/risk`、`/risk/industry`、`/oms/orders`、`/execution`、`/audit`、`/events` |
| `execution` | `/api/v3/execution`、`/oms/orders`、`/metrics`、`/audit`、`/risk`、`/execution/quality`、`/risk/industry`；`POST /oms/sync`、`/api/wb/plan-execute`、`/api/wb/confirm-decide`（点击） |
| `gateway` | `/api/v3/gateway`、`/metrics` |
| `tools` | `/api/v3/tools`、`/metrics`、`/settings`、`/gateway`；`POST /api/wb/series`（点击探测） |
| `settings` | `/api/v3/settings`、`/metrics`、`/overview`、`/oms/orders`、`/audit`、`/credentials`、`/sources/status`；写：`POST /api/v3/credentials`、`/api/wb/{switch-mode,openapi_config,openapi_test,openapi_oauth,auto_pipeline}`（点击） |
| `research` | `/api/v3/research`、`/research/tasks`、`/research/report.pdf?id=` |

---

## 六、与既有文档的口径对齐与矛盾

**口径一致（不重复造结论）**：
- `docs/e2e-and-data-gaps.md`：A 股实时权限、Tushare 未注入、AKShare 全市场快照上游断连、`info_search` 载荷不完整 —— 本轮实测**复现一致**（`-9` / `tushare/no-token` / `RemoteDisconnected`）。
- 该文档第六轮「行业集中度红线已解决，但 `check_order` 的 `industry_pct` 仍恒 0.0（观测先行，闸门未接）」——本轮**逐字复现**（实测 `industry_source="no-data…按 0% 不阻断"`），并补充：**工作区已接闸门、待重启**（新事实）。
- `docs/v3-integration.md` §五「取不到就报错误信封、页面显示无数据源 + 原因」——前端抽查**基本成立**（market 页最佳），但有 §五.1 的 12 处回退值例外。

**矛盾（按题面要求点名）**：
1. `docs/v3-integration.md` §一 写 **SEC EDGAR ✅ 可用**；`platform/server/v3_ops.py:1323-1325`（并被 `/api/v3/settings` 直出到「接入与授权」页）写 **❌ 无数据源/本服务未实现**。**实测支持前者**（`source=sec/companyconcept(us-gaap XBRL)`）→ **接口状态字段与实现矛盾，需修 `v3_ops`**。
2. `docs/v3-integration.md` §三/§四 写前端在 `platform/web/public/v3/`、`/` 307 跳 `/v3/index.html`、逐页脚本 `verify_pages.sh`；**仓库中 `platform/web/` 不存在**（`ls` → No such file），现为 `platform/web-pro`（AntD Pro，根路径、hash 路由）。→ **文档过时**。
3. `docs/v3-integration.md` §一 表格写「工作台工具面（本服务 **56** 工具）」；`docs/architecture.md` 写 **82 端点 / 77 MCP 工具**；本轮 `/metrics` 与 `/api/v3/tools` 实测 **82**。→ 三处数字不一致，最新真值 **82**。
4. `AGENTS.md`「二、常驻纪律」写 `cd platform/web && npm test`；`platform/web` 不存在 → 测试命令应指向 `platform/web-pro`（现 `package.json` 在其下）。

---

## 七、本次未能验证的条目与原因

| 条目 | 为什么没验证 |
|---|---|
| **NFR-PERF §4.1**（6 项延迟/可用性） | 只读审计不做压测；且 A 股实时权限缺失使「行情延迟」在当前权限下不可测。`/metrics` 只有进程内均值（`quantwb_mcp_call_duration_seconds 1.592`），**无 P95**，不足以判定阈值 |
| **FR-DATA-001 的「条件单」** | 需要在真实通道下单才能验证；本轮**禁止调用任何写/交易端点**。代码侧 `grep 条件单` 0 命中 → 仅能说「未见实现」，不能说「一定没有」 |
| **FR-EXEC-001 的 live 全生命周期** | live 写协议自述「未经真实 live 下单验证」（`docs/P4-live-trading.md`），且本轮禁止交易调用 |
| **FR-GATEWAY-004 的事件触发 / 费用预算** | 无配置面可读（`GET /api/v3/gateway` 只给 `scheduler.rules=[]` 与心跳）；`headless.breaker=null` |
| **工作区未提交改动的最终形态**（`v3_nlp`/`v3_risk_gate`/`v3_mcp`、`app.py`/`v3_ops.py` 改动） | 审计期间并行 agent 仍在改（18:38→18:52 快照内多次变化）；且**运行中的 8397 进程早于这些改动**，故「工作区已实现」与「线上可用」必须分开陈述 |
| **`test_*` 是否当前全绿** | 未在本轮重跑测试套件（避免写临时文件与干扰并行 agent）；测试名按题面「证据 = 测试用例名」引用，**未断言其通过**（`docs/e2e-and-data-gaps.md` 第六轮 §七记录 `discover -s tests` = 527 OK，为历史基线） |
| **`/mcp` 的 82 工具 schema 细节** | 未做 MCP 握手（`initialize`+`tools/list`）以省配额；工具数以网关自报 + `/metrics` 为准 |

---

## 八、一页结论

1. **「其他能力」整体对齐度：可评 28 条中 26 条对齐或部分对齐（92.9%），完全缺失仅 2 条**——
   `FR-GATEWAY-002`（SDK JSON-RPC 会话通道）与 `FR-TOOLS-002`（AI-native 工具注册规范）。
2. **最大结构性缺口是「大脑三通道只通了 1 条」**：MCP 通（82 工具），SDK 与 Headless 均 `unavailable`
   （实测接口自报原因）。这直接连带 `FR-MON-002` 的 Harness 会话状态、`FR-MON-003` 的 headless 日志、
   `DES-5.2`、`DES-10` 决策 1/2/4 一起降为「部分」。
3. **本轮三件点名事项**：红线闸门**已接但未上线**（待重启）、自研 NLP**已写但未挂载**（待提交重启）、
   A 股实时权限**按已知限制处理**（页面如实标注、不填占位）。
4. **诚实性**：无「示例数据」字样、无编造业务数字；但有 **12 处回退值/固定文案会被读成真实数据**
   （H1–H12，重点：`gateway.jsx:93-94`、`tools.jsx:197-200`、`strategy.jsx:503-506`、
   `overview.jsx:397/406/413/+708`、`settings.jsx:1314`），以及 2 处**后端状态字段与事实相反**
   （`v3_ops.py:1323-1325` SEC、`:1328-1330` Tushare）。建议按 §三 3.2 的 W1–W5 一次性收敛（约 2 人日）。
