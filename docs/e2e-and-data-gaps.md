# V3 全量端到端测试与数据源缺口盘点（2026-09-20）

## 一、方法

```bash
~/.dsh/trading-venv/bin/python platform/tools/e2e_probe.py \
  --base http://127.0.0.1:8397 --json /tmp/e2e.json --md /tmp/e2e.md
```

- 覆盖 **32 个 `/api/v3/*` 路由 + 82 个 `/api/wb/<endpoint>` 端点**，共 113 项；
  > 通道口径（2026-09-20 补充）：本节的探针走的是**平台自己的 HTTP 面**（前端与运维口径）。
  > **Agent/调度侧与平台的所有交互只走 MCP**（`/mcp`，见 `docs/v3-integration.md` §一）：
  > `/api/v3/*` 路由已被 `server/v3_mcp.py` **原样桥接**成 `v3_*` MCP 工具
  > （同一 handler 函数对象，因此下面每一条 HTTP 结论都等价于对应 MCP 工具的结论）。
- **只读纪律**：写/交易端点（`switch-mode`、`plan-execute`、`confirm-decide`、`trade_*`、
  `sim_trade_*`、`modify_user_security`、`push_subscribe`、`rules-decide`、`research-tasks-*`、
  `oms/sync`、`strategy/run`）**一律跳过**，不触发；
- 缺参数时按端点参数表补齐（避免把「参数不对」误判成「没有数据源」）；
- 记录 HTTP 状态、耗时、ok、错误码与上游原文、返回体量。

## 二、结果

| 指标 | 数量 |
|---|---|
| 总数 | 113 |
| ✅ 成功（拿到真实数据） | **79** |
| ❌ 业务失败 | 17 |
| ⏭ 跳过（写/交易/需真实业务标识） | 17 |
| 🌐 网络失败 | 0 |

### 17 项失败的真实性质

| 端点 | 错误 | 性质 |
|---|---|---|
| `cur_kline`、`rt_data`、`rt_order_book`、`rt_quote`、`rt_ticker`、`market_snapshot`、`/api/v3/orderbook` | 富途 `errcode=-9`：realtime quote permission required | **真实缺口：券商实时行情权限** |
| `/api/v3/tushare` | `tushare/no-token` | **真实缺口：待注入 token（页面可配）** |
| `/api/v3/spot` | AKShare 上游 `RemoteDisconnected` | **真实缺口：上游不可达** |
| `info_search` | 富途返回载荷不完整（按失败处理） | **真实缺口：上游返回不完整** |
| `option_chain` / `option_screen` | `field_filter` 必须非空对象 | 探针参数（可用，需给 filter） |
| `derivative_detail` | 需期权合约代码（非股票） | 探针参数（先取 `option_chain` 再查） |
| `ipo_list` | `request_type` 枚举 9/10/11 | 探针参数（**已复验 `request_type=9` 成功**） |
| `stock_screen` | `screen_queries` 结构 | 探针参数（需按查询类型构造） |
| `warrant_screen` | `sorts` 非空数组 | 探针参数（**已复验带 sorts 成功**） |
| `watchlist_list` | `group_name not found` | 探针参数（**`watchlist_groups` 已复验成功**，用真实分组名） |
| `trade_max_qty` | `order_type` 枚举（模拟盘 LIMIT/MARKET） | 探针参数（且属交易只读，需权限） |

### 定向复验（确认可用，非缺口）

`watchlist_groups` ✅ · `ipo_list(request_type=9)` ✅ · `warrant_screen(sorts=[…])` ✅ ·
`info_owner_plate` ✅ · `plate_stock` ✅ · `deals_history` ✅ · `orders_history` ✅ ·
`economic_calendar_hot` ✅ · `quote_history_kline_v2`（历史 K 线 139KB）✅ ·
`account_funds` / `account_positions` / `account_orders` / `orders_open` ✅

## 三、缺失数据源清单（按影响面排序）与替代方案

### 缺口 1：富途**实时行情权限**（唯一影响面最大的缺口）

- **影响**：五档盘口、逐笔、实时报价、实时快照、分钟 K（`rt_*`/`cur_kline`/`market_snapshot`），
  以及工作台「行情与信号」页的盘口卡与板块涨跌幅。
- **富途优先（推荐）**：向富途开通对应市场的**实时行情权限**（港股 LV1/LV2、A 股需相应权限）。
  代码侧无需改动——权限开通后这些端点即刻可用。
- **不改权限的降级路径（也已可用）**：日线/历史 K 线走 `quote_history_kline_v2` 与 `series`
  （`futu/quote_history_kline`，实测 120–160 根）；快照用最近收盘 + `as_of` 标注。
- 开源备选（仅作交叉校验，实时性弱）：AKShare 分钟线、efinance、baostock（延迟行情）。

### 缺口 2：`TUSHARE_TOKEN` 未注入（A 股财务/行情）

- **影响**：`/api/v3/tushare`（A 股利润表、日线、每日指标、股票列表）。
- **富途优先（推荐）**：A 股财务改用 **富途 F10 深度数据**（`f10_detail`，26 个 section，
  含分析师一致预期、回购、高管、经营效率等，**实测可用**）+ 历史 K 线；
  这样 A 股财务不依赖第三方 key。
- **保留 Tushare**：页面「接入与授权」已支持页面化配置 token（保存/测试/清除），需要时直接填。
- 开源备选：AKShare 财务接口（`stock_financial_abstract` 等，免密钥）、baostock（财务季频）。

### 缺口 3：A 股全市场快照上游断连（`/api/v3/spot`）

- **影响**：全市场快照（涨幅榜/换手/量比等）。
- **富途优先（推荐）**：改用富途 **`stock_screen`**（全市场条件筛选，可排序分页，**实测可用**）
  与 `plate_stock`（按板块取成分），覆盖「筛选 + 排序」的核心诉求。
- 开源备选：AKShare 的分市场接口（如 `stock_zh_a_spot` 之外的备选）、efinance、baostock、
  Tushare `daily`（需 token）。

### 缺口 4：富途 `info_search`（新闻搜索）返回载荷不完整

- **影响**：按关键词检索资讯/公告。
- **富途优先（推荐）**：调整 `info_search` 参数（`news_type`/`sort_type`/`lang`/`size`）重试，
  或改走 `events`（公司事件，实测可用）与 `f10_detail` 的资讯类 section。
- 开源备选（**当前已在用**）：AKShare 个股新闻（`/api/v3/news`，免密钥，实测可用）。

## 四、不是缺口、但工作台尚未接线的能力（富途已提供，建议补齐）

| 能力 | 富途端点（实测） | 可补齐的功能 |
|---|---|---|
| **成交流水 / 滑点 / 成交率** | `deals_history`、`deals_today`、`orders_history`、`orders_open`、`account_funds` | 「执行与审批」页的成交质量卡（现标注无数据源） |
| **行业分类与行业暴露** | `plate_list`、`plate_stock`、`info_owner_plate` | 「风险监控」页的行业暴露/集中度（现标注无数据源） |
| **A 股/港股财务深度** | `f10_detail`（26 sections） | 基本面因子、研报证据链 |
| **做空数据（HK/US）** | `short_interest`、`short_daily_volume` | 情绪/资金面因子 |
| **期权与衍生品** | `option_expiration`、`option_chain`、`derivative_detail`、`warrant_screen` | 期权链、波动率与窝轮筛选 |
| **经济日历 / IPO** | `economic_calendar_hot`、`economic_calendar_search`、`ipo_list` | 事件驱动研究 |
| **资金流** | `capital_flow`、`capital_flow_history`、`capital_distribution` | 资金面因子（实测 37KB） |

## 五、建议的下一步（按性价比）

1. **接「成交质量」**：用 `deals_history` + `orders_history` 计算成交率/滑点/撤单率（数据已可用）。
2. **接「行业暴露」**：用 `plate_stock` / `info_owner_plate` 建立行业映射，点亮风控页行业红线。
3. **A 股财务去依赖**：把 `f10_detail` 作为 `/api/v3/financials` 的 A 股通道（免 token）。
4. **实时行情**：如确需盘口，走富途权限开通（代码零改动）；否则以「最近收盘 + as_of」呈现。
5. 探针常驻：把 `e2e_probe.py` 纳入回归（写动作仍跳过），每次改动后跑一次确认 79+ 项仍成功。

---

# 第二轮：三处接线 + 开源降级链 + 三市场 + 真实交易时段（2026-09-20 晚）

## 一、本轮新增能力（全部只读）

| 端点 | 内容 | 实测 |
|---|---|---|
| `GET /api/v3/execution/quality?market=SH\|HK\|US` | 成交率 / 撤单率 / 名义金额加权滑点（正=不利）+ 逐日 points | SH `37 单/0 成交/37 撤单`（成交率 0%、撤单率 100%）；**HK `11 单/3 成交/8 撤单`，成交率 27.27%、滑点 −21.78bps、名义 184,900**；US `1 单全撤` |
| `GET /api/v3/risk/industry` | 富途板块行业映射 + 组合权重 + `breach` | 8/8 映射成功，Top=**股份制银行Ⅱ 37.5% > 20% → breach=true**；HK.00700→数码解决方案服务、US.NVDA→半导体 |
| `GET /api/v3/financials?ticker=` | A股/港股走**富途 f10_detail/statements**（免 token）、美股走 SEC | SH.600000 35 行、HK.00700 23 行、AAPL SEC 6 行；三者均带 `chain` 与 `used_source` |
| `GET /api/v3/markets/calendar` | 三市场时段/节假日（zoneinfo，含美股夏令时） | 今日（周六）SH/HK/US 均 `closed` + 「休市（周末）」+ SH `nextOpen 2026-09-21 09:30`；`holidays_loaded=false` |
| `GET /api/v3/sources/status` | 8 条**主源→开源降级链**真实轻量探测 | 可用 6/8；`news` 链实测**真实降级**（futu `info_search` 载荷不完整 → AKShare 命中浦发银行中报新闻）；`snapshot`/`spot` 双源失败（-9 + AKShare 断连）如实入表 |

- **降级链实现**：`v3_fallback.run_chain()` 逐级尝试并记录 `attempts[{source,ok,ms,error}]`，响应永远带实际 `source`/`as_of`；两源都失败→如实报错，**不返回占位数据**；成交质量类**无开源替代**，错误信息里明说。
- **交易时段**：SH 09:30–11:30/13:00–15:00（Asia/Shanghai）、HK 09:30–12:00/13:00–16:00（Asia/Hong_Kong）、US 09:30–16:00（America/New_York，夏令时自动）；节假日来自 `QUANT_MARKET_HOLIDAYS` 或 `<home>/market-holidays.json`，缺失时只按周末判断并标 `holidays_loaded:false`；休市**不隐藏数据**——价格用最近收盘并标 `as_of`。

## 二、三市场验收（`platform/tools/e2e_markets.sh`）

```
[SH] 行情 K 线 SH.600000   ✓ bars=60 最新=9.07    源 futu/quote_history_kline as_of 2026-09-18
[HK] 行情 K 线 HK.00700    ✓ bars=60 最新=419.0   源 futu/quote_history_kline as_of 2026-09-18
[US] 行情 K 线 US.NVDA     ✓ bars=60 最新=222.27  源 futu/quote_history_kline as_of 2026-09-18
[US] 财务（SEC）           ✓ sec/companyconcept lines=6      [SH] 财务（f10）✓ 35 行   [HK] 财务（f10）✓ 23 行
交易日历                    ✓ SH/HK/US closed · holidays_loaded=False
行业暴露                    ✓ SH 5 行业 top 37.5% breach=True · HK/US 单标的映射成功
成交质量                    ✓ SH 37 单 · HK 11 单(3 成交,−21.78bps) · US 1 单
降级链状态                  ✓ 8 链 / 可用 6
页面 market/execution/risk  ✓ 示例=0 无缺模块 无崩溃白屏
```
配套 `platform/tools/e2e_probe.py --markets SH,HK,US`：**HK 7/7、US 7/7、SH 6/7**（唯一失败＝A 股实时快照权限 -9，已知缺口）。

## 三、本轮新发现的真实问题（重要）

1. **富途 OpenAPI 频控 `-12006`（HTTP 403）**：全量探针连续调用时，`deals_history`/`deals_today`/`f10_detail`/`economic_calendar_hot` 会被限流拒绝。**这不是缺数据源**，是调用节奏问题。
   - 影响：密集探测（探针、`/api/v3/sources/status` 的 8 链探测）会临时失败；
   - 缓解：服务端已有 TTL 缓存；建议对富途类调用再加**退避 + 单飞（single-flight）+ 全局限速**，并把 `-12006` 明确映射为「限流，请稍后重试」而不是泛化错误。
2. **`info_search` 返回载荷不完整**（既有）：news 链靠 AKShare 兜住（降级已实测生效）。
3. **A 股实时行情权限 -9**：`market_snapshot`/`rt_*`/`cur_kline` 无权限；**港股/美股快照与五档实测可用**（页面已按市场分别呈现，不再一律标「无数据源」）。
4. **自选池与因子矩阵仍是 A 股口径**（后端未按 `market` 过滤）：页面已按「参数随市场切换 + 原样展示 + 显式标注接口未按市场过滤」处理；后端补 `market` 支持后页面无需改动。

## 四、验收命令汇总

```bash
# 1) 三市场端到端（接口 + 页面）
bash platform/tools/e2e_markets.sh http://127.0.0.1:8397
# 2) 全量只读探针（113 项）/ 三市场分步表
~/.dsh/trading-venv/bin/python platform/tools/e2e_probe.py --json /tmp/e2e.json --md /tmp/e2e.md
~/.dsh/trading-venv/bin/python platform/tools/e2e_probe.py --markets SH,HK,US
# 3) 工作台 10 路由
bash platform/tools/verify_pages.sh http://127.0.0.1:8397
# 4) 测试
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests   # 246 OK
cd .. && node --test tests/*.test.mjs && cd platform/web-pro && npm test        # 73 / 188
```

---

# 第三轮：后端市场过滤（`?market=SH|HK|US`）+ 分市场基准（2026-09-20 深夜）

## 一、市场宇宙与过滤（`server/v3_universe.py`）

- 池子优先级（**都是真实来源**）：`<home>/trading-platform.json` 的 `watchlists.<market>` →
  旧的顶层 `watchlist`（＝A 股口径，**仍按市场过滤**）→ 富途真实持仓 `positions`
  （数值 `market_id` 1=HK/3=A股/100=US，与 `trading_datasource.market_ids` 同一常量；
  未登记的 9/10/11/12/13/16 一律不猜、排除并计数）→ 都空 → `None` → 端点回
  `market/no-universe`（含 `detail` 写明真实原因，不退回全部市场）。
- 标的归一用 `trading_datasource.market.to_futu_symbol`：实测把持仓裸代码正确归为
  `HK.00100` / `SH.603993` / `SZ.002475`（A 股按首位分 SH/SZ/BJ；否则 `series` 取不到数）。
- 进程内 TTL 缓存（`QUOTE_UNIVERSE_TTL_MS`，缺省 60s；失败用更短的负面 TTL），
  避免密集调用触发富途 `-12006`（HTTP 403）。
- 逐端点语义与实测见 `docs/v3-integration.md`；探针 `platform/tools/e2e_probe.py --markets SH,HK,US`
  已加入 watchlist / factors/matrix / risk/analytics / execution / research / risk/industry 六个带
  `market=` 的步骤，并**核对返回条数与市场一致**（为空必须带真实原因，否则记 `probe/market-scope` 失败）。

## 二、各市场基准与降级（`v3_universe.benchmark_for`）

背景：`risk/analytics` 的 beta/alpha/IR 原来固定用 `SH.000300`，给港/美股组合算会得出误导性结论。
现在 `?market=` 时按市场**实测探测**基准，候选逐个用 `series` 试（同一份 TTL 缓存）。

候选实测（2026-09-20，`futu/quote_history_kline`，30 根日 K）：

| 候选 | 结果 | 说明 |
|---|---|---|
| `SH.000300` | ✅ 30 根 | 沪深 300（A 股唯一候选） |
| `HK.800000` | ✅ 30 根 | 恒生指数（HK 首选） |
| `HK.800700` | ✅ 30 根 | 恒生科技指数（HK 降级 1） |
| `HK.02800` | ✅ 30 根 | 盈富基金 ETF（HK 降级 2） |
| `US.SPY` | ✅ 30 根 | 标普 500 ETF（US 首选；与仓库 `market.INDEX_SYMBOLS.sp500_proxy` 同源） |
| `US..IXIC` | ✅ 30 根 | 纳斯达克综合（**双点**写法，US 降级 1） |
| `US.QQQ` | ✅ 30 根 | 纳指 100 ETF（US 降级 2） |
| `US..DJI` | ✅ 30 根 | 道琼斯（双点写法，US 降级 3） |
| `US.SPX` | ❌ `errcode=-7 invalid symbol` | 富途不认（单发复测仍是 -7，**不是**频控） |
| `US.NDX` | ❌ `errcode=-7 invalid symbol` | 同上 |
| `US.DJI` | ❌ `errcode=-7 invalid symbol` | 单点写法无效；要双点 `US..DJI` |

最终 `BENCHMARKS`：

```python
BENCHMARKS = {
    "SH": ("SH.000300",),
    "HK": ("HK.800000", "HK.800700", "HK.02800"),
    "US": ("US.SPY", "US..IXIC", "US.QQQ", "US..DJI"),
}
```

- `risk/analytics?market=` 响应新增 `benchmark`（实际使用的代码）、`benchmarkSource`
  （`futu/quote_history_kline` 等）、`benchmarkNote`（为何选它/为何不可用，含失败候选原文）；
  beta/alpha/IR 与该基准**同源**。
- 该市场候选**全部不可用 → `benchmark=null`** + `benchmarkNote` 列出候选与失败原因，
  同时 `beta`/`alphaAnnPct`/`ir`/`benchmarkAnnReturnPct` 一律 `null`——**不拿 A 股基准硬算**。
- 显式传 `benchmark=` 时原样使用，不做市场改写。
- 不传 `market` 时沿用历史缺省 `SH.000300`（既有行为不变）。
- `ml/sweep`、`ml/backtest` 加 `market` 回显（显式参数优先，否则取标的的市场前缀；单标的端点，
  只作标注，不改取数口径）。

---

# 第四轮：富途限流治理（全局限速 + 单飞 + 退避 + 冷却）+ overview 按市场看成交（2026-09-21）

背景（实测）：连续密集真机请求会让富途返回 **`-12006`（请求过于频繁）**（表现为 HTTP 403），
另有 `-12009` / HTTP 439。治理前这些错误被**原样透传**——例如 `market/no-universe` 的
`error.detail` 里写着「positions 取数失败…[errcode=439]」，用户会误读成「没有数据」。
本轮把富途调用收敛到**一个**限流器，并把限流明确成独立错误码。

## 一、实现与接入（`platform/server/v3_ratelimit.py`，全站唯一实现）

四项治理能力，全部在同一个进程级 limiter 上（`v3_ratelimit.get_limiter()`）：

| 能力 | 行为 | 计数 |
|---|---|---|
| 全局限速 | 令牌桶 `rate_per_sec`（默认 3）+ 突发额度 `burst`（默认 3）；超出的调用**等待** | `throttleWaitMs`（等待毫秒累计） |
| 并发上限 | 同时在飞的真实请求 ≤ `max_concurrency`（默认 2），其余排队（释放时**直接转交**槽） | `inFlight` / `queued` |
| 单飞 | 同 key（`工具名 + 稳定序列化参数`）并发调用**共享同一次真实请求**，其余复用结果 | `coalesced` |
| 退避重试 | 识别到限流 → 指数退避 + 抖动（`base_backoff_ms` 起、`max_backoff_ms` 封顶），最多 `QUANT_FUTU_RETRY`（默认 2）次 | `retries` |
| 冷却 | 连续 `cooldown_after`（默认 3）次限流错误 → `cooldown_ms`（默认 5000）冷却，期内**不发请求**、直接返回限流错误 | `cooldownUntil` |

**接入点（app.py 的 V3 接线处，一处收口）**：

- `v3_run(name, payload)`：命中富途只读集合 → `v3_ratelimit.futu_run(limiter, endpoint, payload, call)`；
  否则（本地台账 `plan`/`equity`/`audit`/`schedule`/`rules`…）**原样直通**，不被无谓限速。
- `_wb_http(endpoint, payload)`：HTTP-only 读端点同一份口径（如数据面 `info_rehab`）。
- 集合 `FUTU_TOOL_NAMES` = store_access 的 `FUTU_ENDPOINTS` + `WP8_MARKET_ENDPOINTS` +
  `WP8_TRADE_ENDPOINTS` + `WP12_ENDPOINTS`（去掉写类 `modify_user_security`）+ WP7 账户只读
  + 实测触达富途的复合端点（`series`/`positions`/`snapshot`/`sources`/`factors`/`ic`/
  `correlation`/`sensitivity`/`events`/`instrument`/`quality`）。
- **写/交易端点永不在集合里**（`switch-mode`/`plan-execute`/`confirm-decide`/`trade_*`/
  `sim_trade_*`/`modify_user_security`/`push_subscribe`…），有单测钉住。
- 运维可加：`QUANT_FUTU_EXTRA_TOOLS=a,b`（逗号分隔）把额外工具名纳入同一限流器。

`/api/v3/metrics` 新增一块（**只加字段，既有字段一字不改**）：

```json
"futu": {"enabled": true, "calls": 40, "coalesced": 4, "retries": 2, "rateLimited": 3,
         "throttleWaitMs": 478.105, "cooldownUntil": 1789891310455,
         "cooldownRemainingMs": 4139, "inFlight": 0, "queued": 0}
```

- `calls` = 进入 limiter 的调用数；`coalesced` = 因单飞**没有发请求**的调用数；
  `throttleWaitMs` = 限速 + 并发槽等待的毫秒累计（不含退避等待，退避看 `retries`）；
  `cooldownUntil` = 冷却结束的**墙钟毫秒**（未冷却为 0），`cooldownRemainingMs` = 剩余毫秒。

## 二、配置项（环境变量，全部带默认；`QUANT_FUTU_RATELIMIT=0` 整体关闭）

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `QUANT_FUTU_RATELIMIT` | `1`（开） | `0`/`false`/`off`/`no` → **整体关闭**（直通、不计数、不等待） |
| `QUANT_FUTU_RATE_PER_SEC` | `3` | 令牌桶速率（个/秒） |
| `QUANT_FUTU_BURST` | `3` | 令牌桶容量（突发额度） |
| `QUANT_FUTU_MAX_CONCURRENCY` | `2` | 同时在飞的真实请求上限 |
| `QUANT_FUTU_BACKOFF_BASE_MS` | `400` | 首次退避基数（指数增长：400 / 800 / 1600…） |
| `QUANT_FUTU_BACKOFF_MAX_MS` | `8000` | 单次退避上限 |
| `QUANT_FUTU_COOLDOWN_MS` | `5000` | 冷却时长 |
| `QUANT_FUTU_COOLDOWN_AFTER` | `3` | 连续 N 次限流错误后进入冷却 |
| `QUANT_FUTU_RETRY` | `2` | 限流错误后的最大重试次数 |
| `QUANT_FUTU_EXTRA_TOOLS` | （空） | 额外声明为富途只读的工具名（逗号分隔） |

非法/越界的环境值**回落默认**（不抛异常、不把服务带崩），有单测。

## 三、错误码映射与统一信封

识别（`is_rate_limit_error` / `upstream_code_of`）：

| 上游写法 | 识别条件 | `detail.upstream` |
|---|---|---|
| `-12006` / `12006` | 数字边界匹配（含 `HTTP 403：{"code":-12006}`） | `-12006` |
| `-12009` / `12009` | 同上 | `-12009` |
| `439` | `errcode=439` / `retcode: 439` / `status=439` / `[439]` | `439` |
| `HTTP 403` | **只在同时含 `12006` 时**才算（裸 403 判为权限类，不误判成限流） | `-12006` |
| `futu/rate-limited` | 内层调用已被本模块限流（外层再看到同一事实） | 内层码优先，无码则 `futu/rate-limited` |
| 文字特征 | `请求过于频繁`/`请求频繁`/`频率限制`/`超出频率`/`限流`/`rate limit`/`too many requests`… | `text` |

重试耗尽（或冷却期拒绝）时**返回**（不抛）统一信封，非限流错误**原样透传**（业务错误一字不改）：

```json
{"ok": false, "error": {"code": "futu/rate-limited",
  "message": "富途接口限流（上游 -12006），已退避重试 2 次仍失败，请稍后重试",
  "detail": {"upstream": "-12006", "retry_after_ms": 5200, "retries": 2, "cooldown": true,
             "reason": "非预期响应（HTTP 403）：b'{\"code\":-12006...}'"},
  "retry_after_ms": 5200}}
```

- `detail.reason` 保留**上游原文**（截断 300 字符）——前端已按
  「富途接口限流… · 真实原因：{…}」渲染，不再出现「取数失败 → 以为没有数据」。
- `retry_after_ms`：冷却中 = **剩余冷却毫秒**；否则 = 建议的**下一次退避量**
  （400→800→1600… 经抖动后取值，封顶 `BACKOFF_MAX_MS`）。
- 计次口径：**每次退避重试也算一次连续限流错误**，所以一次「重试耗尽」的调用本身
  就可能（在 `cooldown_after=3` 时）触发冷却——与任务书样例信封（`retries:2, cooldown:true`）同形。

## 四、离线单测（`platform/tests/test_v3_ratelimit.py`，40 例，注入假时钟/假 sleep/假 rand）

覆盖：令牌桶限速等待与 `throttleWaitMs` 累计；并发上限（4 线程实测峰值 in-flight=2、2 个排队）；
**单飞**（同步 5 并发 → 1 次真实调用；异步 5 并发 → 1 次真实调用）；限流→退避→成功；
指数退避 + 封顶；重试耗尽 → 统一信封；冷却期内不发请求 + `retry_after_ms` 递减 + 到期恢复；
非限流异常照抛 / 业务错误信封原样透传；**`ok=true` 的成功信封绝不被改写成限流错误**
（真机教训，见第五节）；`QUANT_FUTU_RATELIMIT=0` 直通；`stats()` 字段齐全；
嵌套调用不争槽自锁；识别矩阵；只读集合不含写端点；环境变量默认/覆盖/非法回落。
另在 `tests/test_v3_ops.py` 的 `OverviewMarketScopeTests` 里用真实 app.py 分支验 overview 市场过滤。

```
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_ratelimit -v   # 40 OK
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests              # 355 OK
cd . && ~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests                     # 2428 OK
```

## 五、真机实测（TestClient 独立实例，未触碰 8397 服务；只读）

> 全部用 `create_app` + `TestClient`（不进 lifespan，因此不起调度器/推送），同一个
> `DSH_HOME`、同一个真实富途上游。运行脚本：`/tmp/v3_verify_ratelimit.py`（一次性验证脚本，
> 不入库）。

**1) 冷却触发 → 冷却期拒绝（不发请求）→ 到期自动恢复**（默认配置 3/s、burst 3、并发 2、退避 400ms、冷却 5s）：

```
/api/v3/overview?market=US  ok=True 20755ms
  metrics.futu = {"calls":16,"coalesced":0,"retries":2,"rateLimited":3,
                  "throttleWaitMs":478.105,"cooldownRemainingMs":4554}

突发 24 个 series（6 线程，冷却期内）: failed 24/24
  {"code":"futu/rate-limited","message":"富途接口限流冷却中（上游 text），请约 4.5s 后重试",
   "detail":{"upstream":"text","retry_after_ms":4490,"retries":0,"cooldown":true},
   "retry_after_ms":4490}        # 每个 64~118ms 返回 —— **没有发上游请求**
  … 24 个信封的 retry_after_ms 4497 → 4439 随时钟递减

冷却结束复测: ok=True 46ms source=futu/quote_history_kline cooldownRemainingMs=0   ← 自动恢复
```

**2) 这次触发暴露并修掉了一个真问题（重要）**：把 `detail.reason`（上游原文，截断 300 字符）
补全之后就看清了触发源——它**不是**顶层失败，而是 `deals_today` **成功信封**（`ok=true`）的
`value` 里，某个模拟账户分组的取数失败原因带着限流文字：

```
"reason": "{\"ok\": true, \"value\": {\"mode\": \"sim\",
           \"source\": \"futu/sim_trade_order_list(derived)\", ... \"groups\":
           [{\"acc_id\": \"6683018\", \"market\": \"1…
```

即**上游确实在按频率拒绝**（账户级），但外层这次调用本身是成功的。旧实现把它当限流错误
重试并把**已经取到的数据**改写成了限流信封——这是错的。修复两条：

- `is_rate_limit_error`：`ok=true` 的信封**一律不算**限流（成功的 value 里可以有子项的限流说明）；
- `detail.reason`：取不到 `error.message` 时退回收敛后的原文，保证「真实原因」永远可见。

修复后同场景复测（4 轮，含 24 次 series 并发 / 12 次重复并发 / 48 次 25req·s⁻¹ 突发）：
`rateLimited=0, retries=0`，全部成功，`deals_today` 正常返回真实派生数据
（`source=futu/sim_trade_order_list(derived)`）而不再被改写。

**3) 单飞（生产并发下真实合并）**：`coalesced` 多轮非零实测——
`4`、`5`、`6`、`11`、`12`（8 个并发聚合请求把 12 次内层取数合并）、**`27`**
（30 次重复 `f10_detail` + 18 次重复 `execution/quality` 里 27 次内层取数被合并）。

**4) 非限流错误原样透传（真机反证）**：48 次突发里有 9 次失败，全部是
`sec/unknown-ticker`（美股 SEC 业务错误）——`rateLimited=0`、`retries=0`，
既没有重试也没有被改写成限流信封。

**5) 限速与延迟代价**：默认 3/s 下 40~51 次富途调用累计 `throttleWaitMs` 10~45s
（累计值，含并发等待；24 次 series 单次最长约 2s，全部成功）。
`/api/v3/overview?market=US` 端到端耗时对比（同一时段、同一 home、只读）：

| 实例 | 耗时 |
|---|---|
| 运行中的 8397（旧代码，无限流器） | 6.89s / 6.53s（两次） |
| TestClient 新实例（含限流器） | 6.7~7.0s（其中限流等待 `throttleWaitMs` 0.4~0.8s） |

即：overview 的耗时主体是聚合取数本身（`snapshot`/`sources` 探测），限流器一次调用只加
数百毫秒。对延迟敏感的场景可上调 `QUANT_FUTU_RATE_PER_SEC`/`BURST`，或按需用
`QUANT_FUTU_RATELIMIT=0` 关闭。

**6) 未观察到项（如实记录，不编造）**：修复后的 5 轮真机突发（25~30 req·s⁻¹、8~12 并发、
累计 200+ 次只读调用）**没有抓到顶层的上游 `-12006` / `-12009` / `439`**——上游是否限流
取决于当时的整体负载（这台机器上还有运行中的 8397 服务与其他探针在打同一个上游）。
因此：**统一信封的 `-12006` 形态、`retry_after_ms` 递减、冷却与恢复语义由离线单测逐条钉住**
（`tests/test_v3_ratelimit.py`），真机抓到的是 `futu/rate-limited` 冷却/重试耗尽信封与
`metrics.futu` 的真实计数；`coalesced`（单飞）与 `rateLimited`（识别）在真机上都拿到过非零值。

## 六、overview 按市场看成交（`GET /api/v3/overview?market=SH|HK|US`）

- `deals_today` 现在**也按市场过滤**（原来只有 `positions`/`plan`）：口径**复用**
  `v3_universe.market_of_account_label`（其白名单就是 `v3_quality.MARKET_TRD_CODES`），
  不另造第二套映射；实现是 `v3_ops.filter_deals_value`（内部走同一个 `filter_grouped_value`）。
- 响应新增 `filter.deals = {market, kept, excluded, unknownMarketGroups}`：
  `kept`/`excluded` 是**成交笔数**，`unknownMarketGroups` 是无法归因市场的分组数（**不猜**，
  例如 `market_id=9/10/11/12/13/16`）。
- `sections.market_scoped.deals_today` 改为事实描述（「已按账户市场过滤…」），
  `sections.market_scoped.equity` 仍保留「equity 为台账口径，未按市场拆分」。
- **不传 `market` 时行为与历史完全一致**（无 `filter`/`sections`/`market` 字段，`deals_today` 原样透传）。

真机三市场实测（同一实例，只读）：

```
不传 market: groups = HK:0 / SH:0 / 9:0 / 10:0 / 11:0 / 12:0 / 13:0 / US:0 / 16:0   filter.deals = null
?market=SH : groups = [SH 3182575]  filter.deals = {"market":"SH","kept":0,"excluded":0,"unknownMarketGroups":6}
?market=HK : groups = [HK 9393]     filter.deals = {"market":"HK","kept":0,"excluded":0,"unknownMarketGroups":6}
?market=US : groups = [US 11587526] filter.deals = {"market":"US","kept":0,"excluded":0,"unknownMarketGroups":6}
```

（当日模拟盘无成交 → `kept=0`；`excluded=0` 是因为被排除的是**未知市场分组**，
它们计进 `unknownMarketGroups`（`market_id` 9/10/11/12/13/16）而不是按「其他市场」排除。
`unknownMarketGroups` 是**逐次如实计数**：不同轮次实测到 6 / 5 / 1——上游按账户返回时，
个别账户的失败会进 `errors` 而不是 `groups`（工具自身行为），分组集合因此逐次不同，
这里不做任何平滑或补齐。
真机当日无成交，`kept>0` / `excluded>0` / 「分组无 market 声明 → 逐行按标的前缀归因」
这三条路径由 `tests/test_v3_ops.py::OverviewMarketScopeTests` 的夹具逐值钉住。）

兼容性证据：`tests/test_v3_ops.py` 的 `OverviewMarketScopeTests` 用
patch `create_handler` 的离线装配跑**真实 app.py 分支**，断言不传 market 时
`deals_today`/`positions`/`plan` 与原始 value 相等且无 `market`/`filter`/`sections` 字段。

## 七、SQLite 持久化层（`server/v3_db.py`）——平台台账的唯一入口

**动机**：V3 平台台账此前散在 `<home>` 下的 JSONL/JSON 文件里（策略研究轮、OMS 台账、
对账留痕、headless 调用日志、SDK 回合记录）。文件各自原子写、各自读，没有事务、没有
并发保护、没有统一的查询面。本节记录把它们换成 SQLite 之后的 schema、迁移策略、
并发/事务口径与备份方式，以及**迁移前后的兼容性证据**。

### 7.1 范围（改了什么、没改什么）

| 数据 | 迁移前 | 迁移后主存 | 冷备（保留、不改格式） |
| --- | --- | --- | --- |
| 策略研究轮 | `v3-strategy-runs.jsonl` | 表 `strategy_runs` | 同左（双写镜像） |
| OMS 台账 | `v3-oms-orders.json` | 表 `oms_orders` | 同左（双写镜像） |
| 对账留痕 | `v3-oms-sync.jsonl` | 表 `oms_sync` | 同左（双写镜像） |
| headless 调用日志 | `v3-headless-log.jsonl`（当前无写入方） | 表 `headless_log` | 同左 |
| SDK 回合记录 | `v3-sdk-turns.jsonl`（当前无写入方） | 表 `sdk_turns` | 同左 |

**有意不动的东西**：`v3-credentials.json`（0600）**保持文件**，不进数据库；其他 agent 正在
新增的降级链 / 日历 / 宇宙缓存**文件格式一字不动**（如果需要持久化，可用本层的
`get_kv`/`set_kv` 作为**可选**后端，不强制）。

**为什么冷备文件还在写**：迁移要求「无损 + 不破坏既有接口」。库里是主源（读优先走库），
同一份记录继续追加到原文件，于是：① 迁移前的行数/字节数证据可复核；② 回滚只需不看库；
③ 尚未迁移的读者（脚本、外部工具）行为不变；④ 库损坏时读路径可直接回退文件。

### 7.2 路径口径

```
data_dir = 显式 data_dir 参数 → $QUANT_V3_DATA → <home>
db_path  = $QUANT_V3_DB → <data_dir>/v3.db
```

`home` 缺省 `$DSH_HOME` → `~/.dsh`（与 `compute.command_home`、`v3_analytics._home_path` 同口径）。
`data_dir` 与既有 `QUANT_V3_DATA` 一致：迁移源文件与数据库在同一目录解析——「文件在哪、库就在哪」。

### 7.3 schema（`TABLE_SPECS` 是唯一事实来源，DDL 由它生成）

```sql
strategy_runs(id INTEGER PK AUTOINCREMENT, as_of TEXT, market TEXT, universe TEXT/*json*/,
              stages TEXT/*json*/, proposals TEXT/*json*/, payload TEXT NOT NULL, created_at TEXT)
              -- 索引: (market), (as_of)
oms_orders  (id TEXT PRIMARY KEY, ticker TEXT, side TEXT, qty REAL, price REAL, value REAL,
             stage TEXT, risk TEXT/*json*/, plan_id TEXT, history TEXT/*json*/,
             updated_at TEXT, payload TEXT NOT NULL)
             -- 索引: (stage), (ticker), (updated_at)
headless_log(id INTEGER PK AUTOINCREMENT, started_at TEXT, success INTEGER, exit_code INTEGER,
             duration_ms REAL, tokens_estimate INTEGER, payload TEXT NOT NULL)
             -- 索引: (started_at)
sdk_turns   (id INTEGER PK AUTOINCREMENT, at TEXT, session_id TEXT, kind TEXT, code TEXT,
             answer TEXT, tool_calls TEXT/*json*/, route TEXT/*json*/, payload TEXT NOT NULL)
             -- 索引: (session_id), (at)
oms_sync    (id INTEGER PK AUTOINCREMENT, at TEXT, plans INTEGER, orders INTEGER, nav REAL,
             nav_source TEXT, drawdown_pct REAL, industry_source TEXT, stages TEXT/*json*/,
             payload TEXT NOT NULL)          -- 索引: (at)
kv          (namespace TEXT, key TEXT, value TEXT/*json*/, expires_at REAL,
             PRIMARY KEY(namespace, key))
schema_version(version INTEGER, applied_at TEXT)
```

相对建议 schema 只有**一处补充**：`oms_sync`（对账留痕原本也是一个散落的 JSONL，一并收进库）。
每张事件表都同时存 **常用列提取**（供 `where`/索引查询）与整条 **payload JSON**（无损重建原始
记录——`list_events` 读出来的对象与迁移前逐字段一致）。`TABLE_SPECS` 里 `key` 非空的表
（当前只有 `oms_orders`）用 `INSERT ... ON CONFLICT(key) DO UPDATE`，其余是纯追加。

契约（`server/v3_db.py`）：`connect(home, *, data_dir=None)`、`init_db(home, *, data_dir=None)`、
`append_event(home, table, payload) -> int`、`list_events(home, table, *, limit=100, order="desc",
where=None) -> list[dict]`、`get_kv`/`set_kv`、`stats(home) -> dict`、
`migrate_from_files(home, *, data_dir=None) -> dict`；另有 `count_events`、`replace_events`
（整表镜像，OMS 台账用）、`backup_to`、`reset_counters`。

### 7.4 迁移策略（幂等、无损、不删原文件）

1. **建表**：`init_db` 建表/建索引/schema_version（`app.py` 装配时调用一次，幂等）。
2. **逐文件迁移**：`v3-strategy-runs.jsonl`、`v3-oms-orders.json`、`v3-oms-sync.jsonl`、
   `v3-headless-log.jsonl`、`v3-sdk-turns.jsonl`（缺文件 → 跳过并如实记
   `文件不存在或不可读`，不是错误）。
3. **幂等的两层保险**：
   * **文件指纹**（sha1，存 `kv` 命名空间 `migration`，键 = 文件名）：指纹未变且库内行数
     ≥ 文件条数 → 直接跳过（第二次 `init_db` 27.7 ms，零写入）；
   * **payload 内容去重**：指纹变了才逐条比对规范化 payload（`sort_keys` + 紧凑分隔），
     已存在的不再插入。因此「迁移后平台又双写追加了新记录」不会产生重复行；
     「库写失败、只有文件写成功」的尾巴会在下次启动补进库（不重放已有行）。
4. **不删不改原文件**：迁移只读文件；文件字节数/内容与迁移前一致（单测逐字节比对）。
5. **失败隔离**：单个文件坏（JSON 解析失败等）只影响它自己，返回 `error`/`note`，
   其余文件继续迁移；`migrate_from_files` **不抛异常**。

### 7.5 并发与事务

* **WAL** + `PRAGMA busy_timeout`（缺省 5000 ms，`QUANT_V3_DB_TIMEOUT_MS` 覆盖）+
  `synchronous=NORMAL`；先设 busy_timeout 再切 WAL（首次切 WAL 需要写锁，等锁优于立刻报忙）。
* **短事务**：`isolation_level=None`（auto-commit）+ 显式 `BEGIN IMMEDIATE`；只在取锁这一步
  退避重试（10 ms 起、上限 400 ms、5 次），主体跑过之后不再重放（重放会重复副作用），
  COMMIT 仍被占用就抛 `V3DbError`。
* **一次调用一条连接**：`append_event`/`list_events`/`get_kv`/`set_kv` 各自开连接、用完即关，
  连接不跨线程；多进程写同一份库靠 WAL + busy_timeout 协调。
* 实测（临时 home，非 8397 数据）：**10 线程 × 10 次 = 100 条**追加 0 异常、0.403 s、
  库内 100 行 100 个唯一键；**4 进程 × 25 次 = 100 条**追加 0 异常、0.794 s、同样 100 行。
  并发 `migrate_from_files`（4 线程）不产生重复行。
* **未解决限制**：① SQLite 没有「跨表事务」的需求，但**跨进程长事务会拖住其他写者**——
  目前只有短事务；② `busy_timeout` 用尽即失败（写失败不阻断业务，读回退文件）；
  ③ 冷备文件与库是**最后写者胜**：同一毫秒内两个进程写同一条记录，文件是两次 append
  （库是两次 upsert），库里以最后一次为准——台账语义上可接受，但没有做「文件-库」双向
  一致性校验。

### 7.6 任何 DB 异常都不能让接口 500

* 读路径（`OmsLedger.read`、`_strategy_run_records`）：`v3_db` 异常 → 当作「库里没有」→
  **回退只读文件**（迁移前行为）。
* 写路径：库写失败不影响文件写（`append_strategy_run` 两者都失败才回 `persistError`；
  `OmsLedger.write` 库失败静默、文件照写）。
* `stats()` / `init_db()` / `migrate_from_files()` **契约上不抛**：库不可写、库文件损坏
  都收敛成 `ok=false` + `error`，`/api/v3/metrics` 照常 200。
* 单测覆盖：父目录不存在时 `list_events`/`append_event` 抛 `V3DbError`、`stats()` 仍返回
  `ok=false`；把 `v3.db` 换成垃圾字节，`stats()` 仍不抛；把 `list_events` 打成必抛，
  `/api/v3/oms/orders` 照常 200 且回退到文件。

### 7.7 `/api/v3/metrics` 的 `db` 块（只加字段）

```json
"db": {
  "path": "<home>/v3.db", "dataDir": "<home>",
  "sizeBytes": 118784, "walBytes": 0, "exists": true,
  "tables": {"strategy_runs": 4, "oms_orders": 10, "headless_log": 0, "sdk_turns": 0,
             "oms_sync": 2, "kv": 5, "schema_version": 1},
  "writes": 16, "reads": 12, "errors": 0, "schemaVersion": 1,
  "migrated": {"v3-strategy-runs.jsonl": {"rows": 4, "inserted": 4, "at": "…", "fingerprint": "…"}},
  "ok": true
}
```

`writes`/`reads` 是**进程内计数**（与 `mcp`/`wb`/`http` 同一口径，不引 Prometheus）。
不传 `db` 的旧前端不受影响（只加字段，不删不改）。

### 7.8 备份 / 只读快照

```python
from server import v3_db
v3_db.backup_to(home, "/backup/v3-20260920.db")   # 内部 VACUUM INTO，目标必须不存在
```

* `VACUUM INTO` 是 SQLite 官方在线快照：**只读、不停服、WAL 下的已提交数据全部包含**；
* 恢复 = 停止服务 → 用快照替换 `<home>/v3.db`（同时删掉同名 `-wal`/`-shm`）→ 启动
  （`init_db` 幂等，不会重复迁移）；冷备 JSONL/JSON 仍在，必要时可用
  `migrate_from_files` 重新补齐；
* 也可直接 `sqlite3 <home>/v3.db ".backup '<dest>'"`（本机 `sqlite3` CLI 不存在，
  Python 模块自带 3.46.1，所以走 `backup_to`）。

### 7.9 真机验证（临时 home + 临时端口，未触碰 8397 的数据）

方法：`git archive HEAD` 导出未含本次改动的副本作为 **before**；当前工作区作为 **after**；
两边各起一个真服务（`python -m server.run`，`DSH_HOME`=各自的临时 home，
`TRADING_SERVICE_PORT`=内核分配的空闲端口），临时 home 里放**同一份真实 v3 台账副本**
（策略轮 4 条 / OMS 台账 10 单 / 对账留痕 2 条），只发 GET，落盘响应后逐字段深比较。

```
IDENTICAL   /api/v3/strategy                 before=102ms after=27ms
IDENTICAL   /api/v3/strategy?market=SH       before=29ms  after=28ms
IDENTICAL   /api/v3/strategy?market=HK       before=24ms  after=28ms
IDENTICAL   /api/v3/strategy?market=US       before=29ms  after=45ms
IDENTICAL   /api/v3/oms/orders               before=1389ms after=1242ms
IDENTICAL   /api/v3/oms/orders?market=SH     before=1257ms after=930ms
IDENTICAL   /api/v3/research                 before=42ms  after=20ms   （仅 generated_at 不同）
DIFF(3)     /api/v3/brain                    —— 见下
```

* `/api/v3/strategy`、`/api/v3/oms/orders`（含 `?market=SH`）的响应**逐字节相同**
  （`json.dumps(sort_keys=True)` 相等）；`/api/v3/research` 只差 `generated_at` 时间戳。
* `/api/v3/brain` 只有 3 处差异，且都**不是持久化数据**：
  1. `sources.decision` 的来源标注：before 是 `<home>/v3-strategy-runs.jsonl（取最后一条有效记录）`，
     after 是 `sqlite:<home>/v3.db#strategy_runs（冷备 <home>/v3-strategy-runs.jsonl）（…）`
     ——**唯一的、有意的值变化**：字段名/类型/顺序不变，只是如实说明「数据现在来自库」；
     （`decision`/`decisionMarket` 两个数据字段逐字段相同；既有单测只断言该串仍含
     `v3-strategy-runs.jsonl`。）
  2. `sources.workbench.data.checked_at`（工作台探测时间戳）；
  3. `sources.workbench.data.sources[3].detail` 里的临时 home 路径（环境差异）。
* `/api/v3/metrics`：before 键 `{futu, generated_at, http, mcp, ok, oms, sdk, toolDomains,
  toolTotal, wb, workbenchUp}`，after 是同一集合 **+ `db`**（`after ⊇ before`，只加不删）。

真实数据迁移输出（临时 home 副本；**不是** 8397 的数据）：

```
首次 init_db（含建表 + 迁移） 63.0 ms
  v3-strategy-runs.jsonl  rows=4  inserted=4  updated=0  skipped=false
  v3-oms-orders.json      rows=10 inserted=10 updated=0  skipped=false
  v3-oms-sync.jsonl       rows=2  inserted=2  updated=0  skipped=false
  v3-headless-log.jsonl   rows=0  skipped=true  note=文件不存在或不可读
  v3-sdk-turns.jsonl      rows=0  skipped=true  note=文件不存在或不可读
  tables={strategy_runs:4, oms_orders:10, headless_log:0, sdk_turns:0, oms_sync:2, kv:5,
          schema_version:1}  sizeBytes=118784  writes=16
第二次 init_db 27.7 ms
  全部 inserted=0 updated=0 skipped=true          ← 幂等
文件未被改动 = True；库读回 == 文件解析 = True（策略轮 4 / OMS 10 / 对账 2，逐字段相等）
```

并发完整性（临时 home）：

```
10 线程 × 10 次 = 100 条：异常 [] | 库内 100 行 / 100 个唯一键 | 集合完整 True | 0.403 s
 4 进程 × 25 次 = 100 条：异常 [] | 库内 100 行 / 100 个唯一键 | 集合完整 True | 0.794 s
```

### 7.10 注意事项与未解决项

1. **启动副作用（有意）**：`create_app(home)` 会在 `home` 下建 `v3.db` 并迁移——这是
   「启动时初始化」的语义。用默认 `$DSH_HOME` 起服务的进程会写到真实 home；测试必须传
   显式临时 home（仓库既有用例已如此）。本次验证期间 `~/.dsh/v3.db` 已经生成过一次
   （另一并行进程用默认 home 装配 app 所致），源文件毫发无损，且重复 init 是幂等的。
2. **`sources.decision` 的值变了**（唯一一处）：它是来源标注字符串，字段/类型/顺序不变，
   变得**更诚实**（库命中时声明 `sqlite:`）。若某个前端按这个串做正则匹配，需要同步。
3. **`headless_log` / `sdk_turns` 目前没有写入方**：本服务确实没有 SDK/Headless 通道
   （`/api/v3/brain` 如实返回 `status=unavailable`）。表与迁移路径已就绪，一旦将来有
   写入口，直接 `append_event(home, "headless_log", {...})` 即可。
4. **`busy_timeout` 用尽即失败**：极端并发/长事务下写会失败——写失败不回滚业务、读回退
   文件，但**那一刻的追加会只落在冷备文件**，要等下次启动迁移才进库。若要更强保证，
   可把超时调大（`QUANT_V3_DB_TIMEOUT_MS`）或在写路径加重试，本次未做。
5. **`/api/v3/metrics` 的 `writes`/`reads` 是进程内计数**：多 worker/多进程各自计数，
   不是全局值（与 `mcp`/`wb`/`http` 口径一致，不做第二事实源）。
6. `v3_universe.py` / `v3_ratelimit.py` / `v3_calendar_source.py` 等**未改动**；KV 后端是
   可选提供，未接线进去。

测试：`cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_db -v`
（29 个用例：schema/路径/迁移幂等/KV TTL/并发/统计/备份/失败面）。

# 第五轮：ML 策略族（规格 FR-STRAT-002：Lasso / LightGBM / MLP）与动量策略同台可比（2026-09-21）

新增只读端点 `GET /api/v3/ml/models?market=SH|HK|US&ticker=&window=20&horizon=1`，
实现在 `platform/server/v3_ml.py`（纯计算内核）+ `platform/server/v3_analytics.py`（接线）。
取数只经既有限流器 `v3_run("series", {ticker, period:"1d", limit})`；**不触发任何写/交易端点**，
不改前端，不新增系统依赖。

## 一、依赖探测（先探测，再决定实现路线）

```
~/.dsh/trading-venv/bin/python -c "import sklearn; print('sklearn', sklearn.__version__)"
→ ModuleNotFoundError: No module named 'sklearn'
~/.dsh/trading-venv/bin/python -c "import lightgbm; print('lightgbm', lightgbm.__version__)"
→ ModuleNotFoundError: No module named 'lightgbm'
~/.dsh/trading-venv/bin/python -V                      → Python 3.13.5
numpy 2.5.3 / pandas 3.0.6（已装）
```

**两个可选库都没有装** → 按规格**不 pip 安装**（环境不允许、会影响其它模块），三个模型改用
numpy 等价实现。可选库只做「探测 + 存在时优先」：导入/拟合任何失败都回落 numpy，且
`impl` **绝不谎称**用了官方库。响应里的 `backends: {sklearn, lightgbm}` 是**每次请求真实探测**
（`importlib.util.find_spec`）的结果——本部署恒为 `{false, false}`。

## 二、模型实现与 `impl` 标注

| `name` | `impl`（如实） | 实现 | 关键超参（默认） | 与官方库的差异 |
|---|---|---|---|---|
| `lasso` | `numpy-lasso` | **坐标下降**（闭式软阈值）解 `min 1/(2n)‖y−Xb‖² + α‖b‖₁` | `alpha=0.01, epochs=300` | sklearn `Lasso` 同目标函数同 `alpha` 口径，但无对偶间隙早停/warm-start；`lr` **不参与计算**（坐标下降不需要学习率），已记进 `params.lr` 如实标注 |
| `gbdt` | `numpy-gbdt-lite` | **GBDT-lite**：分位数直方图分箱 + 深度 1..3 的 CART + XGBoost 风格增益 `G²/(H+λ)` | `trees=60, depth=2, lr=0.1, bins=32, l2=1.0` | **不是 LightGBM 官方库**：无 GOSS/EFB/直方图减法，精度与速度都不等价 |
| `mlp` | `numpy-mlp` | 单隐层 `tanh` 网络 + **Adam** 全批量优化（输入/输出标准化） | `hidden=16, epochs=300, lr=0.01, seed=0` | 非 sklearn `MLPRegressor`；`seed` 固定 → 可复现 |
| 动量基线 | `momentum-score` | 现有动量打分 `c[t]/c[t−window]−1`（与 `v3_math.backtest_momentum` 的信号、`_strategy_analysis.localMomentum` **同一定义**） | 同一训练样本做单变量 OLS 校准 | 校准只为让 `mse` 与模型同量纲；IC/hit_rate 只在系数符号上与原打分不同 |

`train_lasso/train_gbdt/train_mlp` 的签名与规格逐字一致（`train_mlp` 多一个 `seed=0`
关键字以保证可复现）；`coef` 是**标准化特征空间**的系数（列间的 `|coef|` 因此可比）。

## 三、特征与 PIT 口径

10 列特征，**全部由收盘价推出**（不依赖成交量/基本面，故三市场同一套口径）：

| 列 | 定义（`t` 日为基准，只用 ≤t 的数据） |
|---|---|
| `ret_1` | `c[t]/c[t−1]−1` |
| `ret_5` | `c[t]/c[t−5]−1` |
| `ret_window` | `c[t]/c[t−window]−1`（**与动量基线同一列**） |
| `vol_window` | 近 `window` 日收益的样本标准差 |
| `ma_gap` | `c[t]/mean(c[t−window+1..t])−1` |
| `ret_z` | `(ret_1 − 近 window 日均值)/近 window 日标准差` |
| `rsi_14` | RSI(14)/100（涨跌皆 0 → 0.5） |
| `range_pos` | `(c[t]−min(c[t−window+1..t]))/(max−min)` |
| `skew_window` | 近 `window` 日收益偏度 |
| `mom_gap` | `ret_5 − ret_window` |

**标签**：`y = c[t+horizon]/c[t] − 1`。样本按 `(日期, 标的)` **升序**池化（跨标的合并后仍是时间序）。
历史不足以定义某列 → 该行**丢弃**（不插值、不清零）；恒定价格序列（`range_pos` 分母为 0）
**整段无样本**，如实返回 0 条而不是填 0 顶替。

**PIT 由单测逐元素钉住**（`tests/test_v3_ml.py::PITTests`，4 例）：把 `t` 之后的所有收盘价换成
随机值后，`t` 日特征 `np.testing.assert_array_equal` **逐元素不变**；同一份数据里**标签必须变**
（否则该用例是空转，专门有 `test_the_test_above_is_not_vacuous` 反证）。

## 四、训练/评估协议（「同台可比」的全部含义）

1. **时序留出 70/30**（按 `(日期, 标的)` 升序，不随机切分）：前 70% 训练、后 30% 评估；
   三个模型与基线**共用同一训练集与同一留出集**。
2. `metrics` 一律是**样本外**指标；`metrics_in_sample` 另附仅作对照。指标定义
   （`v3_ml.evaluate`，四个对象逐项同口径）：`mse`、`ic`（预测与真实收益的 Pearson）、
   `hit_rate`（`sign(pred)==sign(y)` 占比，`pred` 恰为 0 记不中）、
   `long_short_ann_pct`（`mean(sign(pred)·y − |Δpos|·cost) × (252/horizon) × 100`）。
3. **按标的切片后加权汇总**（`per_ticker` 附明细）：`long_short_ann_pct` 的换手是「相邻样本之间」
   的仓位变化，多标的池化后逐行算换手会把跨标的切换误记成成本——分组后每个切片内部才是真实时序。
4. `cost_bps`（单边换手成本，基点）默认 0，同时作用于三个模型与基线。
5. 样本 `n_samples < 120` → `{"ok": false, "error": {"code": "ml/insufficient-sample",
   "message": …, "detail": {n_samples, required, window, horizon, tickers}}}`——**不硬跑**。

## 五、真机实测（`create_app` + `TestClient`，同一 `DSH_HOME`、真实数据层与限流器，未重启 8397）

装配方式：`server.app.create_app(home=…)` + `starlette.testclient.TestClient`（不进 lifespan，
因此不起调度器/推送/DB 迁移），走的是与 `server/run.py` 完全相同的装配路径与
`v3_ratelimit.get_limiter()` 全局限流器。**8397 上运行中的进程是改动前启动的**，
所以新路由只能在独立实例里验证（主 agent 重启后 `http://127.0.0.1:8397/api/v3/ml/models` 才生效）。

`window=20&horizon=1&limit=500`，三市场各一例：

| 市场:标的 | `source`（**实测**） | `as_of` | `n_samples` | train/test |
|---|---|---|---|---|
| SH:SH.600519 | `akshare/sina` | 2026-09-17 | 479 | 335 / 144 |
| HK:HK.00700 | `yahoo/auto_adjusted` | 2026-09-17 | 479 | 335 / 144 |
| US:US.NVDA | `yahoo/auto_adjusted` | 2026-09-17 | 479 | 335 / 144 |

> **规格备注一处（重要）**：规格把 `source` 写死为 `futu/quote_history_kline`，但本机实测
> 富途通道当时不可用，降级链给出的是 `akshare/sina`（A 股）与 `yahoo/auto_adjusted`（港/美）。
> 实现**上报真实来源**（`_dominant_source`，逐标的 `sources` 另附），不按规格写死一个可能说谎的值。

样本外指标（真实数字，`n=144`，`cost_bps=0`）：

| 市场:标的 | 模型 | `impl` | mse | ic | hit_rate | long_short_ann_pct |
|---|---|---|---|---|---|---|
| SH.600519 | lasso | numpy-lasso | 0.000219 | −0.0378 | 0.4653 | −29.06 |
| SH.600519 | gbdt | numpy-gbdt-lite | 0.000207 | **+0.1236** | 0.5000 | +23.23 |
| SH.600519 | mlp | numpy-mlp | 0.000351 | −0.0079 | 0.4236 | −19.61 |
| SH.600519 | **动量基线** | momentum-score | 0.000205 | +0.0576 | 0.4722 | −7.62 |
| HK.00700 | lasso | numpy-lasso | 0.000569 | −0.1066 | 0.4583 | −57.97 |
| HK.00700 | gbdt | numpy-gbdt-lite | 0.000588 | −0.0452 | 0.5139 | −78.15 |
| HK.00700 | mlp | numpy-mlp | 0.001178 | **+0.1157** | **0.5625** | +48.12 |
| HK.00700 | **动量基线** | momentum-score | 0.000563 | −0.1153 | 0.4306 | −33.57 |
| US.NVDA | lasso | numpy-lasso | 0.000616 | +0.1304 | 0.4792 | +3.96 |
| US.NVDA | gbdt | numpy-gbdt-lite | 0.000662 | −0.0261 | 0.4722 | −9.94 |
| US.NVDA | mlp | numpy-mlp | 0.001071 | **+0.1833** | 0.5556 | +43.93 |
| US.NVDA | **动量基线** | momentum-score | 0.000624 | +0.0618 | 0.5208 | +55.79 |

`coef_top`（Lasso，标准化空间前 3）：

```
SH.600519  ma_gap −0.434 / ret_5 +0.251 / vol_window +0.106
HK.00700   rsi_14 +0.150 / ma_gap −0.081 / skew_window −0.048
US.NVDA    ret_1 −0.127 / ma_gap −0.111 / ret_z +0.098
```

缺省宇宙（不给 `ticker`，取该市场宇宙前 6 只）：

```
market=SH  ok=true  universe_source=config/trading-platform.json#watchlist        n_samples=2874  tickers=SH.600000/600009/600010/600011/600015/600016
market=HK  ok=true  universe_source=futu/sim_trade_position_list#HK               n_samples=1735  tickers=HK.00100/00981/03986/09961/09988
market=US  ok=true  universe_source=futu/sim_trade_position_list#US               n_samples=958   tickers=US.MSTR/US.NVDA
```

**如实读这些数字**：样本外 `|ic| ≤ 0.18`、三个模型与动量基线**互有胜负、没有稳定赢家**
（SH 上 GBDT 的 IC 最高，HK/US 上是 MLP）；`long_short_ann_pct` 在 −78% ~ +56% 之间剧烈摆动——
144 个样本外样本上的多空年化**不是稳健结论**，只说明「管线是真实在跑、指标是真实算出来的」。
按市场看：SH 三模型都没跑赢动量基线的 IC（0.058），HK/US 上 MLP 明显更高但 mse 更差。
这符合「日频单标的线性/浅层模型接近随机游走」的先验，**没有任何一处数字被调优或挑选过**。

## 六、离线单测（`platform/tests/test_v3_ml.py`，48 例，全部离线、不装依赖）

```
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_ml -v   # 48 OK
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests      # 526 OK
```

覆盖：特征列定义/标签口径/排序/脏数据丢弃/恒定序列 0 样本；**PIT 4 例**（含反证用例）；
Lasso 稀疏真值选出 `{0,3}` 两列且 `nonzero==2`、`alpha=0` 与 `numpy.linalg.lstsq` 对拍、
`intercept` 的原始量纲语义；GBDT/MLP 在非线性合成数据上 mse 优于线性基线（阈值取实测的
一半，留足余量，不写「必胜」）；`impl` 真实性 + JSON 无 NaN；**依赖缺失路径**（monkeypatch
`available_backends` 为 `{false,false}` 走 numpy；patch 成 `{true,true}` 而库实际不存在时
**仍回落 numpy 且不谎称 impl**）；端点的成功/单标的/宇宙缺省/`market/no-universe`/
`market/bad-market`/`ml/insufficient-sample`/上游错误透传/部分标的失败不拖垮整轮/
`limit` 夹取/只读纪律（只调 `series`/`positions`，断言不触达任何写端点）。

## 七、限制与未解决项（如实列出，不掩盖）

1. **未做 purging/embargo**：相邻样本的 `horizon` 标签区间重叠，样本外指标偏乐观；已写进响应 `notes`。
2. **无横截面口径**：只做时序池化回归，未做横截面排序/分组 IC/中性化；单标的时「多空」是时序多空。
3. **无超参搜索、无交叉验证、无多重检验校正**：三组超参是固定默认值，不做「挑最好那组」的幸存者偏差操作。
4. `numpy-gbdt-lite` **不是 LightGBM**，`numpy-mlp` **不是 sklearn MLPRegressor**：速度与精度都不等价，
   规格里「LightGBM」在本环境的真实实现名是 `numpy-gbdt-lite`。
5. `numpy-lasso` 的 `lr` 参数不参与计算（坐标下降闭式更新）；已记进 `params.lr` 并在 docstring 说明，
   不做「假装用了它」。
6. 特征只用收盘价，未纳入成交量/换手/基本面/行业；数据源本身在降级链上（实测 `akshare/sina`、`yahoo/auto_adjusted`，
   非富途）。
7. `cost_bps` 默认 0（毛收益），且换手成本按「相邻样本仓位变化」近似——按标的切片后成立，
   跨标的的真实组合换手仍需组合层口径。
8. **未接前端**（规格只要求后端只读端点）；`platform/tools/e2e_probe.py` 的 `V3_GET` 路由表
   也**有意未加** `ml/models`（该文件正被并行改动，避免冲突）——待并行的改动落定后补一行即可。
9. 8397 上运行中的进程是改动前启动的，新路由需主 agent 重启后才在 `http://127.0.0.1:8397` 生效；
   本轮的端到端证据来自独立 `create_app` + `TestClient` 实例（同 `DSH_HOME`、同数据层、同限流器）。

---

# 第六轮：监控落地（规格 §8.3：Prometheus 抓取 + 告警规则）（2026-09-21）

## 一、先说一个与任务书预设不符的实测事实

任务书预设「`/metrics`（Prometheus 文本）与 `/api/v3/metrics`（JSON）已存在」。
**前者不存在**，实测为 SPA 首页：

```console
$ curl -s -o /dev/null -w '%{http_code} %{content_type}\n' http://127.0.0.1:8397/metrics
200 text/html; charset=utf-8
$ grep -rn 'get("/metrics")' platform/          # 无输出
$ find platform -name 'observability.py'        # 无输出
$ ls platform/deploy                            # No such file or directory
```

原因：`platform/server/app.py` 末尾有 SPA 兜底 `@app.get("/{path:path}")`，任何未注册路径都回
`platform/web-pro` 的 `index.html`。仓库里当时**只有** `/api/v3/metrics`（给人读的 JSON）。

因此本轮**新建**了 Prometheus 文本出口，而不是「补几个指标」。这是本轮最大的一处范围变化，
如实记在最前面。

## 二、新增出口（`platform/server/observability.py`，只读，只加不删）

* `GET /metrics` → `text/plain; version=0.0.4`。注册在 `app.py` 子模块循环里，
  **必须排在 `/{path:path}` 静态兜底之前**（否则被 SPA 兜底吃掉；
  `tests/test_observability.py::test_route_wins_over_spa_catch_all` 守住这条）。
* 指标**只做格式转换**，不新增第二事实源：
  进程内计数 ← `v3_ops.metrics_snapshot()`；富途限流 ← `v3_ratelimit.metrics_view()`；
  OMS 台账 ← `v3_ops.OmsLedger.stage_counts()`；调度器/推送 ← `app.state`（与 `/healthz` 同源）。
* **两条不撒谎的设计约束**（都有单测）：
  1. 没有真实读数就**不导出**该指标，而不是导出 `0`——`0` 会被读成「正常」；
  2. 抓取路径**不发起任何外部探测**（见下）。
* 唯一动到既有文件的改动：`app.py` 模块元组加一个 `"observability"`；
  `v3_fallback.register` 在 `/api/v3/sources/status` 成功后 best-effort 落盘探测结果
  （惰性 import，避免 `v3_fallback → observability → v3_ops → v3_universe → v3_quality → v3_fallback`
  的循环）。**没有删除任何既有行为。**

### 两条容易踩的坑（实测修掉）

* **缓存探测不阻塞抓取**：工作台可达性/回撤用 TTL 缓存（`QUANT_METRICS_PROBE_TTL`，默认 60s）
  + 后台线程刷新，`/metrics` 当次立即返回上一份快照。首次抓取尚无快照时相关指标**缺席**
  （不编造 0）。
* **探测不污染被测计数**：探测调用走 app 级 `v3_run`，**不经过** `v3_ops` 的计数调用器，
  所以 `quantwb_mcp_calls_total` 不会被监控自己灌大——否则失败率被稀释，告警失真。
  `test_probe_does_not_pollute_mcp_counters` 断言这一点。

### 为什么降级链指标不在抓取路径上现探测

探测会真的打富途/akshare/SEC，而富途调用要过限流器——**用监控触发「被限流」告警是自伤**。
所以降级链指标只读 `<DSH_HOME>/v3-datasource-probe.json`（由 `GET /api/v3/sources/status`
的真实探测落盘），并附 `quantwb_datasource_probe_timestamp_seconds` 供规则做新鲜度守卫。
代价是**降级链告警需要一个触发者**（定时器/人跑一次该端点），这条已写进 README §6.2 与
`DataSourceProbeStale` 告警（缓存 > 24h 未刷新时主动报警，避免「监控不响」被误读成「一切正常」）。

## 三、产出物

| 路径 | 内容 |
|---|---|
| `platform/server/observability.py` | Prometheus 文本出口（30+ 指标家族） |
| `platform/tests/test_observability.py` | 41 例契约测试（见下） |
| `platform/deploy/monitoring/prometheus.yml` | scrape 配置（`127.0.0.1:8397`、`/metrics`、15s） |
| `platform/deploy/monitoring/alerts.yml` | **19 条**告警规则（6 组） |
| `platform/deploy/monitoring/README.md` | 一页落地说明 + 指标清单 + 缺口清单 |
| `platform/deploy/monitoring/dump_metrics.py` | 离线导出指标文本，供 `promtool check metrics` 自检 |

## 四、校验结果（都是真跑的命令与真实输出）

镜像可拉（`docker pull prom/prometheus:latest` 成功），因此**规则与配置都真校验了**：

```console
$ docker run --rm -v "$PWD/platform/deploy/monitoring:/etc/prometheus:ro" \
    --entrypoint promtool prom/prometheus check rules /etc/prometheus/alerts.yml
Checking /etc/prometheus/alerts.yml
  SUCCESS: 19 rules found

$ docker run --rm -v "$PWD/platform/deploy/monitoring:/etc/prometheus:ro" \
    --entrypoint promtool prom/prometheus check config /etc/prometheus/prometheus.yml
Checking /etc/prometheus/prometheus.yml
  SUCCESS: 1 rule files found
 SUCCESS: /etc/prometheus/prometheus.yml is valid prometheus config file syntax
Checking /etc/prometheus/alerts.yml
  SUCCESS: 19 rules found
```

指标文本用 `promtool check metrics` 校验，**只剩 2 条有意保留的 lint**：

```console
$ cd platform && ~/.dsh/trading-venv/bin/python -B deploy/monitoring/dump_metrics.py --with-probe-cache > /tmp/metrics.txt
$ docker run --rm -i --entrypoint promtool prom/prometheus check metrics < /tmp/metrics.txt
futu_cooldown_remaining_ms metric names should not contain abbreviated units
futu_throttle_wait_ms_total metric names should not contain abbreviated units
```

保留理由：这两个名字直接对应平台自身字段 `cooldownRemainingMs` / `throttleWaitMs`，
且 `futu_cooldown_remaining_ms` 是规格 §8.3 点名要的规则输入。其余 lint 项已按 Prometheus
约定改正（耗时改**秒**、`_count` 后缀改掉、计数器一律 `_total`）。

**没有**做的事：没装 Prometheus/Grafana/Alertmanager、没改任何 systemd 单元、没建 Grafana 面板
（本机无 Grafana 可校验，故不产出未校验的东西）、没接真实通知通道。

## 五、告警规则与指标的**一致性**由测试守住

`tests/test_observability.py::AlertRulesTests` 用 **AST** 提取 `observability.py` 里
**所有可能输出**的指标名（而非运行时抓取——像 `quantwb_workbench_probe_failed` 只在探测失败
时才出现，运行时抓取会漏掉它），再断言 `alerts.yml` 每个 `expr` 里出现的
`quantwb_*`/`futu_*` 名字都在其中。**规则写错指标名会直接测挂**，不靠人眼核对。
同一组测试还断言：≥6 条规则、每条有 `name`/`expr`/`for`/`severity`、规则名唯一、
探测类指标必带 `time()` 新鲜度守卫、反向「导出的指标必须在规则或 README 里被提到」。

## 六、指标缺口 → 建议新增的指标名（如实说明，不掩盖）

| # | 缺口 | 为什么现在没有 | 建议新增 |
|---|---|---|---|
| 1 | ~~**行业集中度红线（20%）**~~ **已解决（2026-09-20 更正）** | 上一轮写的「平台根本没有行业分类数据源」**已过时**：`GET /api/v3/risk/industry` 能取到真实富途板块行业映射（实测 SH top=股份制银行Ⅱ 37.5% > 20% → `breach=true`）。现在由 `POST /api/v3/metrics/probe/refresh` 落盘、`/metrics` 只读缓存导出 | ✅ `quantwb_risk_industry_pct{scope="max"}`（更正过程见 §九） |
| 2 | ~~单笔占比红线（2%）~~ **已解决（只读口径）** | 台账已落盘 `value` / `nav_used`，可直接换算待确认单的最大单笔占比 | ✅ `quantwb_risk_single_order_pct_max` |
| 3 | 业务确认待作答数 | 只在 `confirmation` 工具返回值里，无进程内状态 | `quantwb_confirmation_pending` |
| 4 | 对账差异行数 | `/api/v3/audit` 随请求计算 | `quantwb_reconcile_diff_rows` |
| 5 | 成交质量/滑点 | `v3_quality` 随请求计算 | `quantwb_execution_slippage_bps` |
| 6 | ~~**降级链探测的自动触发**~~ **已解决（素材，未安装）** | 不是指标缺口而是**运维缺口**：探测不能放在抓取路径上，需要有东西定时跑 `/api/v3/sources/status`。本条产出 `platform/install/quant-v3-probe.{service,timer}`（每 30 分钟同时触发降级链与行业两个探测端点），**未安装到系统**（需运维 `systemctl --user enable --now`） | ✅ 见 §九 |
| 7 | 多 worker 聚合 | `/metrics` 是**单进程**计数；uvicorn 开多 worker 会各算各的 | 当前单进程，暂不需要 |

第 1 条的更正过程与真实数据见 §九。**注意**：`check_order` 的**下单阻断**仍未接行业口径
（`context()["industry_pct"]` 恒 0.0），本轮只做到「观测先行」。

## 七、测试

```bash
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_observability -v   # 41 OK
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_observability \
    tests.test_v3_fallback tests.test_v3_ops tests.test_v3_sources                           # 164 OK
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests               # 527 OK
```

基线口径：本轮开工时 `discover -s tests` 为 **355 passed**（与任务书给的基线一致）；
完工复跑为 **527 OK**。差值来自本轮新增的 41 例与**并行 agent** 同期新增的用例
（`v3_ml` / `v3_db` / `v3_calendar_source` 等）——两者都已含在 527 里。
中途一次 `discover` 曾报 1 个 `test_v3_analytics` 错误，那是并行 agent 正在改该文件造成的
中间态（`KeyError: '/api/v3/ml/models'`），**与本轮改动无关**，复跑已绿。
本轮**没有减少任何既有测试**。

`test_observability.py` 覆盖：**exposition format 结构校验**（HELP/TYPE 在样本前、
**同 family 样本必须连续**——这条最初真被写错过：`_render_push`/`_render_datasources`
逐行交错写两个 family，`promtool` 解析会失败，已修并加断言）、`_total` 只属于计数器、
路由优先于 SPA 兜底、uptime 单调、探测不污染 MCP 计数、TTL 缓存与**过期不阻塞抓取**、
工作台不可达如实报 0 + 原因标签、标签值转义（`\\`/`"`/换行）、推送异常不拖垮抓取、
调度器缺失时**缺席而非 0**、降级链缓存缺失/损坏**如实忽略不猜**、落盘 best-effort。

## 八、限制与未解决项（如实列出）

1. **运行中的 8397 进程是改动前启动的**，`/metrics` 与降级链落盘要主 agent 重启后生效。
   本轮的端到端证据来自独立 `create_app` + `TestClient` 实例（真应用、真接线，只是不跑 lifespan）。
2. `promtool check metrics` 仍有 2 条 `_ms` lint（有意保留，理由见上）。
3. **未接 Alertmanager / 飞书 / 邮件**：告警止于 Prometheus `/alerts` 页面与日志。
4. 降级链告警在**没有任何人跑过 `sources/status`** 时是静默的——`DataSourceProbeStale`
   只会在缓存**曾经存在**且过期后报警；从未探测过则连该告警也不响（README §6.2 第 5 条已写明）。
5. 阈值依据全部写在 `alerts.yml` 注释里（平台自身常量：`LIMITS` 2/20/15、
   `v3_ratelimit.DEFAULTS` 3.0/3/2/400/8000/5000/3/2），但**没有**做过负载下的误报率标定——
   首次上线后需要按真实噪声调整，那些数字是可辩护的起点，不是调优结果。

---

# 第五轮：交易日历/节假日**自动获取**（富途优先）+ AKShare **自动重试与多接口降级**（2026-09-21）

两项**外部数据健壮性加强**，全部只读。此前 `/api/v3/markets/calendar` 只读一份人工
`market-holidays.json`：文件不存在时「只按周末判断」，国庆/中秋/感恩节这类**落在工作日的
休市**会被当成交易日（前端据此显示「交易中」）；`/api/v3/spot` 则是一次调用就放弃，
东财断连即报错。本轮把这两处补齐。

## 一、交易日历自动获取（`platform/server/v3_calendar_source.py`，**唯一实现**）

来源优先级（每一级都进 `chain`，带真实耗时与上游错误原文；命中即返回）:

| 顺序 | 来源 | 覆盖市场 | 说明 |
|---|---|---|---|
| 0 | 进程内 TTL 缓存 | 全部 | `QUANT_CALENDAR_TTL_MS`，默认 12h |
| 1 | `<home>/market-calendar.json` 落盘缓存 | 全部 | TTL 内且覆盖请求窗口才命中（**进程重启后仍能直接供数**） |
| 2 | 富途 `info_trading_days`（经既有 `v3_ratelimit`） | SH/SZ/BJ/HK/US | 市场码实测全部可用；~600–950ms |
| 3 | AKShare `tool_trade_date_hist_sina` | **仅 A 股** | 港/美股**无开源自历**，如实标注 |
| 4 | `<home>/market-holidays.json` 人工兜底表 | 全部 | 保留兼容（老部署只维护这一份） |
| 5 | —— | —— | 取不到 → `resolve_trading_days` 返回 `None` + 逐级真实原因 |

契约函数（`__all__` 已导出）:

```python
resolve_trading_days(wb_call, home, market, *, start, end, ttl_ms=None, clock=None)
    -> {"market","start","end","trading_days":[...],"coverage":{"start","end"},"complete":bool,
        "source","origin_source"?,"served_from"?,"data_as_of","as_of","chain":[...],"note"} | None
resolve_market(...)            # 同上的「带原因」版本：{market, ok, result, chain, reason}
resolve_calendar(...)          # {market: resolve_market 结果}
holidays_for(market, start, end, trading_days)  # 工作日 − 交易日（周末不计入）
refresh_cache(home, markets=("SH","HK","US"), horizon_days=400, *, wb_call=..., ...)
calendar_window(today)         # 默认 [今天−45 天, 今天+400 天]，刷新与请求共用同一个窗口
```

* `source` 取值：`futu/info_trading_days` / `akshare/tool_trade_date_hist_sina` / `cache`
  （内存或落盘命中，原来源见 `origin_source`、命中层级见 `served_from`）/ `file`（人工表）。
  `chain[].source` 更细（`cache/memory`、`cache/disk`、`file/market-holidays.json`）。
* **只有实时源的结果进缓存**：人工兜底表不写 `market-calendar.json`（否则上游恢复后仍被判为
  「没有日历」）；`refresh_cache` 同理，取不到实时源时**连空文件都不生成**。
* `coverage` 是**源的真实覆盖**（AKShare 只到 2026-12-31），请求窗口超出时
  `complete=false`，绝不假装覆盖；`holidays` 只在这个覆盖范围内算。
* AKShare 断连由 `v3_fallback.retry_akshare` 自动重试（见第五节）。

落盘结构（`version` / `updated_at` / `markets` 是契约字段，逐市场另存来源与覆盖）:

```json
{"version": 1, "updated_at": "2026-09-20T09:41:40+00:00",
 "markets": {"SH": {"trading_days": ["2026-08-07", "..."], "holidays": ["2026-09-25", "..."],
                    "source": "futu/info_trading_days", "data_as_of": "...",
                    "start": "2026-08-06", "end": "2027-10-25",
                    "coverage": {"start": "2026-08-06", "end": "2027-10-25"},
                    "complete": true, "note": "..."}}}
```

**探明的市场码（写进文档以免下次再猜）**：富途 `info_trading_days` 的 `market` 接受
`SH` / `SZ` / `BJ` / `HK` / `US`（与 `plugins/datasource/.../groups/market.py` 的
`TRADING_MARKETS` 一致；`US` 实测返回 314 个交易日且缺 2026-09-07＝劳动节）。
AKShare 侧 `dir(akshare)` 里 `trade_date` 系只有 `tool_trade_date_hist_sina` 一个，**没有**
港股/美股日历接口——港美股日历当前只能来自富途。

## 二、`v3_market_calendar` 的行为变化（**只加字段**，既有字段语义不变）

* 节假日判定改为 `holidays_for()`：**工作日 − 交易日**，周末**不再**进 `holidays`
  （旧行为下周末靠 `weekday()` 单独判定，不会重复，但人工表若混入周末会被算两次）。
* `holidays_loaded` 在**所有请求的市场都拿到真实日历**（自动获取或人工表）时为 `true`；
  否则保持旧行为：`false` + 「只按周末判断」+ **逐级真实原因**（含上游错误原文）。
* 新增顶层字段：`calendar_source`（如 `futu/info_trading_days`、`cache`、`mixed`）、
  `calendar_as_of`、`calendar_note`、`calendar_complete`、`calendar_cache_file`；
  每市场新增：`calendar_source`、`calendar_note`、`calendar_coverage`、
  `holidays_in_window`（**只数工作日休市**，周末不算）、`calendar_complete`。
* 顶层 `source` 仍是 `platform/market_calendar`（既有语义 = 「本响应由本模块组装」），
  取数来源在新字段 `calendar_source` 里——不改旧字段是硬约束，前端无需改。
* 取不到日历时行为与历史完全一致（只按周末判断 + `holidays_loaded:false`），
  只是 `holidays_note` 里多了每一级为什么没成。

## 三、刷新端点与「低频自动刷新」怎么挂

```
POST /api/v3/markets/calendar/refresh?markets=SH,HK,US&horizon_days=400
```

**只读语义**：跳过缓存直取富途（A 股再降级 AKShare）+ 合并写
`<home>/market-calendar.json`，**不涉交易**；逐市场返回 `source`/`trading_days`/`holidays`/
`coverage`/`complete`，取不到实时源的市场记 `ok:false` + 原因且不写占位条目；
未知市场返回 `calendar/bad-market` 错误信封。三种挂法（任选其一，都不改交易路径）：

1. **调度器**：在 `server/scheduler.py` 的作业链里加一个「每日一次」的 job，调
   `v3_calendar_source.refresh_cache(home, ("SH","HK","US"), 400, wb_call=<工具面入口>)`；
2. **外部定时器**：cron/`install/*.timer` 每天调一次上面的 POST 端点（与本仓库
   `install/research-duty.timer` 同一套路）；
3. **不挂也行**：TTL（12h）过期后的第一次真实请求会**顺带**刷新缓存并落盘——
   缺点是「没人访问就不更新」，所以生产建议至少挂第 2 种。

真机实测（2026-09-21，独立 TestClient 实例，未触碰 8397）:

```
POST /api/v3/markets/calendar/refresh?markets=SH,HK,US   HTTP 200 ok=True
  path=/home/penn/.dsh/market-calendar.json
  window={'start':'2026-08-06','end':'2027-10-25','lookback_days':45,'horizon_days':400}
  SH: ok=True source=futu/info_trading_days trading_days=312 holidays=6  complete=True
  HK: ok=True source=futu/info_trading_days trading_days=314 holidays=4  complete=True
  US: ok=True source=futu/info_trading_days trading_days=314 holidays=4  complete=True
落盘：version=1 updated_at=2026-09-20T09:41:40+00:00 markets=['HK','SH','US']
  SH holidays=['2026-09-25','2026-10-01','2026-10-02','2026-10-05','2026-10-06','2026-10-07']
  HK holidays=['2026-10-01','2026-10-19','2026-12-25','2027-01-01']
  US holidays=['2026-09-07','2026-11-26','2026-12-25','2027-06-18']
POST refresh?markets=SH,XX → {'ok': False, 'error': {'code': 'calendar/bad-market', ...}}
```

## 四、真机实测（任务 A，真实输出）

**逐市场解析（`resolve_trading_days`，真实富途，窗口 2026-09-01~2026-10-15）**:

```
[SH] 952ms  source=futu/info_trading_days  complete=True  交易日=27
  trading_days = 2026-09-01 … 2026-09-24, 2026-09-28 …（缺 09-25 中秋）
  holidays = ['2026-09-25', '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07']
  chain: cache/memory ✗(cache-miss) → cache/disk ✗(cache-miss) → futu/info_trading_days ✓ 941ms
[HK] 738ms  source=futu/info_trading_days  complete=True  交易日=32
  holidays = ['2026-10-01']            # 09-25 中秋不是港股假期（港股当天照常交易）
[US] 752ms  source=futu/info_trading_days  complete=True  交易日=32
  holidays = ['2026-09-07']            # 劳动节
```

**AKShare 降级腿（真实调用：富途腿注入失败，请求窗口 2026-09-20~2027-06-30）**:

```
[SH] source=akshare/tool_trade_date_hist_sina  coverage=2026-09-20~2026-12-31  complete=False
  holidays=['2026-09-25','2026-10-01','2026-10-02','2026-10-05','2026-10-06','2026-10-07']
  note=akshare.tool_trade_date_hist_sina 全量 8797 行、覆盖 1990-12-19~2026-12-31；窗口内 … 有数据
[HK] result=None（AKShare 腿：calendar/akshare-unsupported-market「港/美股无开源自历」）
```

**路由（`GET /api/v3/markets/calendar?markets=SH,HK,US`，独立实例）**:

```
HTTP 200  2792ms  holidays_loaded=True  calendar_source=futu/info_trading_days
calendar_complete=True  source=platform/market_calendar
  SH: isTradingDay=False label=休市（周末） holidays_in_window=6 coverage=2026-08-06~2027-10-25
  HK: holidays_in_window=4   US: holidays_in_window=4
?now=2026-10-01T10:00:00+08:00（国庆，工作日休市）:
  SH: isTradingDay=False holiday=2026-10-01 label=休市（节假日 2026-10-01） nextOpen=2026-10-08T09:30:00+08:00
  HK: isTradingDay=False holiday=2026-10-01 label=休市（节假日 2026-10-01） nextOpen=2026-10-02T09:30:00+08:00
  US: isTradingDay=True  holiday=None       label=已收盘                    nextOpen=2026-10-01T09:30:00-04:00
```

`nextOpen` 因此自动跨过国庆（SH 2026-10-08）与周末；**周末仍记 `holiday=null` +
「休市（周末）」**（不重复计入节假日）。落盘缓存在**新进程**里直接供数：清空进程内缓存后
再请求，`calendar_source=cache`、`calendar_note` 写明「命中 cache/disk 缓存（原来源
futu/info_trading_days…）」——即服务重启后无需再打富途就有节假日表。

## 五、AKShare 自动重试 + 多接口降级（任务 B）

**新增 `v3_fallback.retry_akshare(call, *, attempts=3, base_ms=1200, max_ms=8000,
sleep=time.sleep, clock=time.monotonic, rand=None, is_empty=None, budget_ms=None)`**
（`run_chain` 的签名与语义**一字未改**；重试是链里某一级的内部行为）：

| 情形 | 行为 |
|---|---|
| `ConnectionError` / `RemoteDisconnected` / `TimeoutError` / HTTP 5xx | 重试，指数退避 + `[0,25%)` 抖动，`max_ms` 封顶 |
| 业务性错误（参数错、`ValueError`、4xx、解析失败…） | **不重试**，一次返回，`error.code=akshare/business-error` |
| 成功但空结果（`None`/空容器/pandas `.empty`） | **不重试**，原样返回（调用方按「空结果视同失败」降级） |
| 全失败 / 预算用尽 | `(None, attempts_meta)`；预算截断的那次记 `stopped='budget'` |

`attempts_meta` 每项 `{attempt, ok, ms, error?, retryable?, wait_ms?}`；响应里
**成功与失败都带 `attempts`**（「重试了几次、每次多久、真实错误」可见）。
`/api/v3/spot` 另带 `chain:[{source,ok,ms,error?,attempts?}]`、`source`、`as_of`、
`market_scope`/`scope_note`；全失败时 `error.message` 带**试过的接口顺序**与**真实错误原文**，
`error.tried`/`error.attempts` 同样可见——**绝不返回占位或空 `rows`**。

多接口降级顺序（`AKSHARE_SPOT_CHAIN`，**先快后慢、全市场优先**）:

```
stock_zh_a_spot_em（全市场，单请求）→ stock_sh_a_spot_em → stock_sz_a_spot_em
→ stock_bj_a_spot_em（东财分市场，单请求）→ stock_zh_a_spot（新浪全市场，分页 ~70 请求，最慢）
```

分市场接口命中时 `market_scope` 如实标「沪市/深市/北交所（分市场接口）」+ `scope_note`
写明**只覆盖该市场，不是全市场快照**。整链有墙钟预算
`QUANT_AKSHARE_SPOT_BUDGET_MS`（默认 45000ms）：预算既在链级检查（未试的接口记
`skipped` + `chain/timeout`），也**传进每次重试**（已花时间达预算即停止重试，记
`stopped='budget'`）——上游单次调用可能要几十秒，只在链级检查预算会被一个慢接口吃光。

## 六、真机实测（任务 B，真实输出）

```
GET /api/v3/spot?limit=3   HTTP 200  52454ms  ok=False
error.code=akshare/stock_zh_a_spot_em
error.message=A 股全市场快照：按顺序试过 5 个接口全部失败。首个接口的真实错误
  akshare/stock_zh_a_spot_em: akshare.stock_zh_a_spot_em 尝试 3 次仍失败（退避 [1438, 2926] ms），
  本接口合计 23725ms：ConnectionError: ('Connection aborted.', RemoteDisconnected(...))；降级链：…
error.tried=[em, sh, sz, bj, sina]
chain:
  akshare/stock_zh_a_spot_em  ok=False 23726ms  attempt#1 6368ms wait 1438ms / #2 6134ms wait 2926ms / #3 6856ms
  akshare/stock_sh_a_spot_em  ok=False 20510ms  attempt#1 5623ms wait 1208ms / #2 5929ms wait 2677ms / #3 5068ms
  akshare/stock_sz_a_spot_em  ok=False  5713ms  attempt#1 5713ms；重试被时间预算（762ms）截断，剩余次数未试
  akshare/stock_bj_a_spot_em  ok=False     0ms  SKIPPED  chain/timeout: 链预算 45.0s 已耗尽，未尝试
  akshare/stock_zh_a_spot     ok=False     0ms  SKIPPED  chain/timeout: 链预算 45.0s 已耗尽，未尝试

GET /api/v3/news?symbol=600519&limit=2  HTTP 200 298ms ok=True source=akshare/stock_news_em
  attempts=[{"attempt": 1, "ok": true, "ms": 209}]    # 一次成功也如实记尝试明细
  rows=2（2026-08-15 中报净利润 445.17 亿 / 2026-09-16 被执行 158 万元回应）
```

即：东财/新浪五条现货接口当前**全部**断连或读超时（与第二轮「缺口 3」一致），
现在每次失败都带**逐接口、逐次尝试的真实耗时与错误原文**，并明确区分「试过失败」
与「预算耗尽没试」。`stock_zh_a_spot`（新浪）单次 50–67s 且分页 ~70 次，因此排到链尾：
排第二时实测 120s，排链尾 + 预算截断后 **52.4s**（`QUANT_AKSHARE_SPOT_BUDGET_MS=15000`
可再压到约 20s，代价是分市场接口可能来不及试）。

## 七、离线单测（既有用例一个未删：40 → 58；18 → 23；新增 21）

```
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest \
  tests.test_v3_calendar_source tests.test_v3_calendar tests.test_v3_sources -v
# Ran 102 tests ... OK   （21 + 23 + 58）

cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests
# Ran 526 tests ... OK   （含并行其他轮的用例）
```

新增覆盖：固定时钟下端到端优先级五级（内存/磁盘缓存、富途命中、富途失败→AKShare
真实覆盖、两者失败→人工表、全无→`None`+逐级原因）；TTL 命中不重复请求、TTL+落盘
过期后重取；落盘/读回一致（`version`/`updated_at`/`markets`/合并写/坏文件）；
`holidays_for` 工作日 − 交易日且**不含周末**；`live=False` 绝不打网络、也不 import akshare
（`setUp` 封死 socket/urllib/httpx + `import_module("akshare")`）；
`refresh_cache` 只写实时源、未知市场拒绝、跳过缓存重取；
`retry_akshare` 首次失败重试成功 / 连续 3 次失败明细 / 业务错误不重试 / 退避与抖动按注入的
假 `sleep` + 假 `clock` 断言 / 5xx 与 4xx 分类 / 空结果不重试 / 预算截断；
`/api/v3/spot` 多接口顺序、分市场 `market_scope` 标注、全失败错误原文与 `tried`、
`missing-func` 如实记录、预算 skipped、成功响应保留契约字段并新增 `attempts`。

## 八、未解决项与注意事项（如实记录）

1. **`/api/v3/spot` 全失败时约 52s**（此前一次调用 ~6s 就报错）：这是「3 次重试 ×
   多接口」的必然代价，且上游单次调用不可中断（新浪实测单次 50–67s，`socket`
   默认超时护栏挡不住分页函数的整体耗时）。可用 `QUANT_AKSHARE_SPOT_BUDGET_MS`
   调低（如 15000 → 约 20s）或去掉链尾的新浪接口；本轮选了「先快后慢 + 预算截断」。
2. **港/美股日历目前只能来自富途**（AKShare 无对应接口）。富途不可用时港美股退化为
   「只按周末判断」并如实标注；A 股还有 AKShare 兜底。
3. **AKShare A 股日历只覆盖到 2026-12-31**（8797 行），请求窗口超出时
   `complete=false` + `coverage` 如实截断——超出覆盖范围的日期不再有日历依据
   （当前 30 天前瞻仍在覆盖内）。
4. **缓存一致性是单机的**：进程内 TTL + `<home>/market-calendar.json`（12h），没有
   多实例失效广播；`refresh_cache` 会合并写（不丢其他市场的条目）。
5. **默认只刷/只查 SH/HK/US**；SZ/BJ 富途同样支持，需要时传
   `markets=SH,SZ,BJ,HK,US`（`POST .../calendar/refresh` 与 `?markets=` 都接受）。
6. **人工兜底表不进缓存**：这是有意的（上游恢复后应立刻用真实日历）。若某部署只想用
   人工表，把 `QUANT_MARKET_HOLIDAYS` 指向它即可，行为与历史一致。
7. `refresh` 是 POST 但没有额外鉴权（本服务其余只读端点同样如此），它只写缓存文件、
   不触发任何交易/写端点；若部署需要，可在反向代理层限制来源。
8. 运行中的 8397 仍是**改动前**的进程：`holidays_loaded` 变 true、刷新端点生效都要等
   主 agent 重启；本轮端到端证据来自独立 `create_app` + `TestClient` 实例（真应用、真接线，
   只是不跑 lifespan）。

---

# 行业集中度监控（2026-09-20 补）：更正上一轮结论 + 两个探测触发者

## 九、更正：「行业集中度红线无法实现」**已过时**

### 9.1 旧结论错在哪

上一轮（本文件 §六 第 1 条）写的是：

> 行业集中度红线（20%）……平台**根本没有行业分类数据源**，
> `v3_ops.INDUSTRY_SOURCE` 常量即 `no-data`，`context()["industry_pct"]` 恒 0.0。
> 导出 0 就是编造，故**不导出、也不编一条永远不响的规则**。

这个结论在写下的当天是对的，**现在不对了**：平台已经有了只读端点
`GET /api/v3/risk/industry?market=SH&limit_pct=20`（`platform/server/v3_industry.py`），
它用富途 `info_owner_plate` 取真实板块行业映射、用平台组合口径算权重。真机实测：

```console
$ curl -fsS 'http://127.0.0.1:8397/api/v3/risk/industry?market=SH&limit_pct=20' | jq '{ok,breach,top,sources,missing}'
{"ok":true,"breach":true,                      # ← 37.5% > 20%，红线**真的**被突破
 "top":{"industry":"股份制银行Ⅱ","weightPct":37.5},
 "sources":{"plate":"futu/info_owner_plate", "weights":"platform/portfolio（自选池等权（8 只））"},
 "missing":[]}
```

所以「导出 0 = 编造」这条纪律**依然成立**，但它推出的是「要接真实数据源」，
不是「永远不导出」。本轮把它接上了。

### 9.2 接法（沿用降级链的同一套纪律）

**抓取路径 `GET /metrics` 绝不发起富途探测**（富途调用过全局限流器，用监控触发
「被限流」告警是自伤）。因此新增一个**探测写入器**，与 `GET /api/v3/sources/status`
同类——只读取数 + 本地落盘，不碰任何交易/写端点：

| 环节 | 实现 |
|---|---|
| 写入器 | `POST /api/v3/metrics/probe/refresh?markets=SH,HK,US&limit_pct=20`（`observability.register`） |
| 走的数据路径 | `observability.probe_industry_exposure` → `v3_industry.industry_exposure`（既有只读实现） |
| 落盘 | `<DSH_HOME>/v3-risk-probe.json`（`record_risk_probe`，best-effort 原子写） |
| 读取 | `observability._render_risk_industry(writer, load_risk_probe(home))` |
| 触发者 | `platform/install/quant-v3-probe.{service,timer}`（每 30 分钟；**未安装**，需运维启用） |

新增指标（`quantwb_risk_industry_*`）：`pct{scope="max"|"SH"|"HK"|"US"}`、
`probe_timestamp_seconds`、`breach`、`limit_pct`、`missing{market}`、
`probe_info{market,industry,source}`、`probe_failed{error}`。三条纪律：

1. 缓存缺失/损坏/无 `generated_at` → 整个 family **缺席**（不是 0）；
2. 缓存超过 `QUANT_RISK_PROBE_MAX_AGE`（默认 6h）→ 只留时间戳（让「过期」可见），
   **不导出取值类指标**——拿 6 小时前的暴露当现在的结论就是编造；
3. 没有市场给出读数 → 不导出 `pct`，但导出 `probe_failed{error}`，让失败可被规则感知。

顺带补了一个**不需要任何外部探测**的红线指标：`quantwb_risk_single_order_pct_max`
（台账里 `stage="manual"` 订单的最大 `value / nav_used`，即 2% 单笔红线方向；
没有 manual 单就不导出）。它**不单独设告警**——与既有 `RiskPendingManualConfirmation`
同源，再报一次只是重复。

### 9.3 真机实测（本轮，真实输出）

用**真实应用**（`create_app(home=$DSH_HOME)`，不跑 lifespan、不发交易）跑一次写入器：
写入器按市场逐级取数，三个市场都拿到了真实读数（HK 5 只 / SH 28 只 / US 2 只）：

```console
$ ~/.dsh/trading-venv/bin/python -B -c "...client.post('/api/v3/metrics/probe/refresh')..."
{"status":200,"ok":true,"written":true,"market":"US","top_industry":"半导体",
 "top_weight_pct":50.0,"breach":true,"source":"futu/info_owner_plate","limit_pct":20.0,
 "error":null,
 "per_market":{"HK":{"ok":true,"top":"半导体","w":40.0},"SH":{"ok":true,
   "top":"股份制银行Ⅱ","w":37.5},"US":{"ok":true,"top":"半导体","w":50.0}}}
```

同一进程紧接着 `GET /metrics`（**只读缓存，无外部调用**）：

```
# TYPE quantwb_risk_industry_pct gauge
quantwb_risk_industry_pct{scope="max"} 50
quantwb_risk_industry_pct{scope="HK"} 40
quantwb_risk_industry_pct{scope="SH"} 37.5
quantwb_risk_industry_pct{scope="US"} 50
quantwb_risk_industry_pct ... # TYPE quantwb_risk_industry_breach gauge
quantwb_risk_industry_breach 1
quantwb_risk_industry_limit_pct 20
quantwb_risk_industry_missing{market="US"} 0
quantwb_risk_industry_probe_info{market="US",industry="半导体",source="futu/info_owner_plate"} 1
quantwb_risk_single_order_pct_max 18.348499     # ← 台账里真有一张待确认单占 18.35%（远超 2%）
```

**无缓存**时（离线 `dump_metrics.py`，临时 home）同一个 grep 命中 0 行——
不是导出 0，是整个 family 缺席。

> ⚠️ 这条实测暴露了一个**平台侧的口径缺口**：真实行业暴露 50% 已远超 20% 红线，
> 但 `v3_ops.OmsLedger.context()["industry_pct"]` 仍是常量 `0.0`，
> 所以 `check_order` **不会**因为行业集中度阻断任何单。本轮的告警是**观测先行**：
> 它响，闸门不响。要让红线真正拦单，需要把 `context()` 接到同一份口径（交易路径改动，
> 本轮**没做**，也不该由监控改动顺手带出）。

### 9.4 告警规则（23 条，`platform/deploy/monitoring/alerts.yml`）

| 告警 | 条件 | 级别 |
|---|---|---|
| `RiskIndustryConcentrationBreached` | `quantwb_risk_industry_pct{scope="max"} > 20` 且 `time() - ..._probe_timestamp_seconds < 21600`，持续 5m | critical |
| `RiskIndustryConcentrationApproaching` | 同守卫，`> 15`，持续 15m | warning |
| `RiskIndustryProbeFailed` | `quantwb_risk_industry_probe_failed == 1` 且新鲜，持续 15m | warning |
| `RiskIndustryProbeStale` | `time() - ..._probe_timestamp_seconds > 21600`，持续 1h | info |

阈值 20 与 `v3_ops.LIMITS["industryPct"]` **同源**（测试断言二者一致，防止改常量后规则漂移）；
`AlertRulesTests` 用 AST 提取出口所有指标名做交叉校验，**指标名写错会直接测挂**。

### 9.5 仍未解决的

1. **下单阻断没接行业口径**（见 9.3 的警告）——观测与闸门之间存在真实落差；
2. **定时器只是素材**：`platform/install/quant-v3-probe.{service,timer}` 已写好并
   `systemd-analyze verify` 通过（exit 0），但**没有 enable/start**。不装的话，
   `DataSourceProbeStale` / `RiskIndustryProbeStale` 会响（有意为之：让静默可见）；
3. **探测代价**：每次刷新会按标的数打富途（SH 28 只），全部过 `v3_ratelimit`；
   嫌勤可把 `OnCalendar` 改成每日一次；
4. **运行中的 8397 仍是改动前的进程**：`POST /api/v3/metrics/probe/refresh` 与新的
   `quantwb_risk_industry_*` 要等主 agent 重启后才在线上生效。本轮的端到端证据来自
   独立 `create_app` + `TestClient`（真应用、真接线、真富途只读，只是不跑 lifespan）。


## 十、NLP 情绪引擎（规格 FR-STRAT-003，2026-09-20）

把资讯变成**可用的情绪因子**：`platform/server/v3_nlp.py`（唯一实现）+ 只读端点
`GET /api/v3/sentiment?symbol=&market=&days=&limit=`。纯标准库 + 无三方依赖
（**不装** jieba/torch/transformers/sklearn）——切分与打分都是自己实现的。

接线（对并发改动最小）：端点由 `v3_nlp.register(app, v3_run, home)` **自带 router** 注册，
`server/app.py` 的 V3 子模块自动装配循环里只多了一个模块名 `"v3_nlp"`（一行）。
取新闻**只惰性复用** `server.v3_sources` 的公开面（`fetch_news` / `Deps` / `detect_market` /
`normalize_a_share_symbol`），`v3_sources.py` / `v3_analytics.py` **一行未改**。

### 10.1 词典：自研 306 条（≥150 的要求）

`DEFAULT_LEXICON: dict[str, float]`，按极性分组构造，**不抓取任何网络词典**：

| 分组 | 条数 | 极性区间 | 例 |
|---|---|---|---|
| `STRONG_POSITIVE` | 34 | +0.60 ~ +1.00 | 一字涨停 0.95、重大利好 0.95、历史新高 0.90、扭亏为盈 0.85、超预期 0.75 |
| `POSITIVE` | 55 | +0.45 ~ +0.80 | 买入评级 0.70、景气度提升 0.70、订单充足 0.65、回购 0.60、增持 0.60 |
| `WEAK_POSITIVE` | 44 | +0.35 ~ +0.60 | 增长 0.55、改善 0.50、纳入指数 0.50、补贴 0.45、合作 0.40 |
| `STRONG_NEGATIVE` | 44 | −1.00 ~ −0.60 | 财务造假 −1.00、退市 −1.00、立案调查 −0.95、债务违约 −0.95、爆仓 −0.90 |
| `NEGATIVE` | 62 | −0.90 ~ −0.50 | 巨亏 −0.90、商誉减值 −0.80、下调评级 −0.75、不及预期 −0.70、减持 −0.60 |
| `WEAK_NEGATIVE` | 67 | −0.80 ~ −0.30 | 危机 −0.80、踩雷 −0.75、停产 −0.75、净流出 −0.55、承压 −0.50 |

（六组合计 306 条，词条互不重复；`NEGATORS` 24 个、`INTENSIFIERS` 42 个
（放大 32 / 削弱 10）不计入这 306。）

说明：分组是**构造时的分类**，合并进 `DEFAULT_LEXICON` 后是一个扁平表（键唯一）。
另有 `NEGATORS`（不/未/未能/无/难以/否认/避免…）与 `INTENSIFIERS`
（放大 >1：大幅 1.6/显著 1.5/急剧 1.7；削弱 <1：略微 0.6/小幅 0.7/有所 0.75）。

两个刻意的构造决定：

1. **复合词整体收**：「不及预期」「超预期」「低于预期」是完整词条，**不靠**「不 + 预期」
   的单字规则复原——中文里这类词的否定语义在词内，拆开就错；
2. **程度词必须入典**：「非常」如果不收，会被切成否定词 `非` + 未登录字，把
   「非常大幅增长」判成负面（这是实测踩到过的坑，词典里已有注释钉住）。

### 10.2 算法与公式

**切分** `segment(text) → list[str]`：最大正向匹配 + 词典优先（词表 = 情绪词典 ∪
否定词 ∪ 程度副词，按长度降序贪心），未命中的汉字串**退化到字符 bigram 并每次前进
1 个字符**（这样"公司业绩大幅增长"里 `大幅`/`增长` 仍会被后续位置捕获），空白/标点各自
成 token（同时是作用域断点），ASCII 字母数字串（`20%`/`1.6T`）不被拆。

**单文档分**（`score_text`）：

```
w = polarity × Π(程度因子) × (被否定 ? −0.65 : 1)      # w 截断到 ±1.5
d = (Σw / Σ|w|) × (1 − e^(−Σ|w| / 2))                 # 方向 × 证据量置信（饱和）
```

`Σw/Σ|w|` 是「有符号占比」（正负相抵自然趋 0），`1−e^(−mass/2)` 是证据量饱和函数
（`mass = Σ|w|`）。修饰词只绑定**最近的一个**词典词条，遇到标点/空白即断开（`MODIFIER_WINDOW=3`）。

**多文档分**（`score_documents`，时间半衰）：

```
w_i = 0.5 ^ (age_i / half_life_hours)                 # 默认 48h：两天前的资讯权重减半
s   = Σ(d_i × w_i) / Σ(w_i)     ← 只对**命中过词典**的文档求和
coverage = scored / documents
```

**因子出口** `sentiment_factor(bars_by_ticker, docs_by_ticker, *, window=20, half_life_hours=48)`：
按每只标的最近 `window` 根 K 线做**PIT 截断**（只用窗口内的资讯），返回
`{"tickers": {t: {"score", "coverage", "documents", "window_from"}}, "as_of", "method", …}`。

### 10.3 口径（必须分辨的三种「没有分」）

| 情形 | `score` | `coverage` | 语义 |
|---|---|---|---|
| 窗口内 0 条资讯 | `null` | `null` | 没材料，`notes` 写「该标的近 N 天无资讯」 |
| 有资讯、无一命中词典 | `null` | `0.0` | 词典读不懂这批文本（如全繁体） |
| 命中但正负相抵 | `0.0` 附近的实数 | >0 | **真的有中性结论** |

**`score=null` 与 `score=0` 语义完全不同**，前端/因子层必须区分——这一条有专门单测
（`test_no_hits_returns_none_not_zero` / `test_empty_documents_return_null_not_zero`）。
缺时间戳的资讯不丢弃、按 `as_of` 计权（`w=1.0`）并在 `undated` 里计数、`notes` 里写明。

### 10.4 真机真实输出（只读；独立 `create_app` + `TestClient`，临时 home，未重启 8397）

四只真实标的、真实 AKShare 资讯（东财 `stock_news_em`，经 `retry_akshare`）：

| 标的 | documents | scored | score | coverage | top_terms |
|---|---|---|---|---|---|
| `600519` 贵州茅台 | 7 | 4 | **−0.1056** | 0.571 | 被执行 −2.10(×3)、净流入 +0.55、承压 −0.50、龙头 +0.40 |
| `SH.600000` 浦发银行 | 7 | 3 | **−0.4100** | 0.429 | 净流出 −1.65(×3)、质押 −1.00(×2)、下跌 −0.50 |
| `US.NVDA` 英伟达 | 8 | 3 | **−0.3116** | 0.375 | 下跌 −1.00(×2)、减持 −0.60、套现 −0.60、上涨 +0.50、走高 +0.45 |
| `HK.00700` 腾讯控股 | 5 | 5 | **+0.6757** | 1.000 | 回购 +17.16(×28)、下跌 −1.50(×3)、上涨 +1.00(×2) |

原始新闻标题（窗口内，逐条可核）：

```text
600519  2026-09-16 18:06:11  贵州茅台被执行158万元？公司回应
600519  2026-09-16 21:17:00  被执行158万元？贵州茅台：系第三方公司内部合同纠纷，法院认定公司不承担任何责任
600519  2026-09-16 16:42:00  主力动向：9月16日特大单净流入367.77亿元      → top_term 净流入
SH.600000 2026-09-18 17:41:00 银行行业今日净流出资金3.15亿元，交通银行等5股净流出资金超3000万元 → 净流出
SH.600000 2026-09-17 21:02:00 盛新锂能：关于控股股东的一致行动人进行股份质押的公告 → 质押
US.NVDA  2026-09-20 00:04:51 黄仁勋减持英伟达？被代扣4.57万股用于缴税 CFO套现765万美元 → 减持/套现
US.NVDA  2026-09-16 23:07:12 美股石油股普跌 西方石油跌5%              → 下跌（行业性，非 NVDA 自身）
HK.00700 2026-09-18 21:25:00 腾讯控股00700.HK连续25日回购，累计回购1017.60万股 → 回购（连续 → 程度因子 1.15）
```

`per_day` 也如实给到日粒度（例：`600519` 09-16 三条 = −0.1861、09-20 两条 = −0.0403；
09-17/09-14 的资讯一条也没命中词典 → 该日 `score=null`，**不是 0**）。

**真实错误路径**（不是构造的）：`SZ.399999`（识别得出市场、上游不认这个代码）返回

```json
{"ok": false, "documents": 0, "score": null,
 "error": {"code": "akshare/stock_news_em",
           "message": "akshare.stock_news_em 业务错误（不重试）：KeyError: 'code'：KeyError: 'code'"},
 "chain": [{"source": "akshare/stock_news_em", "ok": false, ...}]}
```

—— 上游真实异常原文 + 取数链留痕，**不返回 0 分**、不返回空壳成功。

### 10.5 与外部情绪源（工作台 `sentiment-history`）对照

同一台机器上另一条独立链路：WP11 采集作业把 `fin_news`（富途 moomoo）/`fin_sentiment`
（X + Reddit）/`last30days`（Reddit 聚类）落进 `trading.sqlite:sentiment_snapshots`，
由 `POST /api/wb/sentiment-history`（经 `trading_core sentiment-history`）读回。
`~/.dsh/trading-data/trading.sqlite` 现已有 2026-09-18 的 **5 只标的 / 11 条记录**。

以 `SH.600000` 为例（同一天、同一标的）：

| | 本引擎（AKShare 东财） | 外部源（`sentiment-history`） |
|---|---|---|
| 语料 | 近 7 天 7 条，简体新闻（多为**行业/资金流**稿） | 同一采集日 8 条 `fin_news`（富途 moomoo，**繁体**）+ 8 条 X + 9 条 Reddit 聚类 |
| 分数 | `−0.4100`（coverage 0.429） | 源自己不产出分数（只有原始 payload），**无法直接比分数** |
| 命中 | 净流出/质押/下跌 | 引擎对这批繁体标题：`scored=0`、`coverage=0.0`、`score=null` |
| 可解释性 | `top_terms` + `per_day` + `hits`（词/极性/权重） | 只有 `payload.items[]` 原始条目，无可解释分 |

实测差异（三条，都不是猜的）：

1. **字形**：富途给的标题是**繁体**（`浦發銀行…歸母淨利潤…同比增長4.08%`），自研词典是简体，
   因此本引擎对它 `coverage=0`——不是「判成中性」，是 `score=null`（诚实地说读不懂）。
   对照实测：同一句话简体 `净利润…同比增长` → `+0.2404`（命中 `增长`），繁体 → `null`（0 命中）。
   5 只标的 40 条繁体标题合计 `coverage=0.225`（只有 `下降/下滑/增加/提升/事故` 这类
   简繁同形的词能命中）。
2. **召回口径**：AKShare 走的是东财**关键字全文搜索**，会把只提到代码的行业稿算进来
   （`银行行业今日净流出…`、甚至标题里出现「600000**股**」的回购公告），precision 明显偏低；
   富途 `fin_news` 是按标的关键字匹配的新闻流，更贴标的但也更少。
3. **外部源的社交面噪声极大**：`fin_sentiment`/`last30days` 里 X 抓的是 `$SH`（一只美股 ETF）
   与 `$SEDG/$SJM`，Reddit 聚类是「圣经研究」和「希伯来文元音」——与浦发银行毫无关系。
   本引擎**不碰**社交面，因此没有这类噪声，但也因此拿不到社交情绪（能力取舍，不是优点）。

### 10.6 离线单测

`platform/tests/test_v3_nlp.py`，**56 例全绿**（`setUp` 把 `socket.connect` /
`create_connection` / `urllib.request.urlopen` / `httpx.Client` 全部封死，漏注入的取数会
显式 AssertionError，**不打网络**）。覆盖点：词典规模与极性方向、切分（最长匹配/bigram
退化/ASCII 串/自定义词典）、正面/负面判定、`hits` 可解释、**无命中 → None**、
否定翻转与衰减、**标点断作用域**、程度放大（`大幅增长` > `增长`）与削弱、覆盖率计算、
时间半衰（同一对正负文档只交换时间 → 符号跟着翻）、中性文档不稀释分数、
`undated` 计数、`top_terms` 排序、`parse_time` 格式与不猜时间、`sentiment_factor` 的
K 线窗口截断、端点无新闻 → `score=null`、上游失败 → 错误信封 + `chain`、
空 symbol 不触达 akshare、`days`/`limit` 夹紧、`register` 三位置参数契约。

### 10.7 限制（如实列出，不掩盖）

1. **繁体未支持**：词典是简体，港股/美股若走繁体源会 `coverage≈0`（当前 AKShare 源是简体，
   所以线上不受影响；一旦换成富途 `fin_news` 就会撞上，见 10.5-1）；
2. **分词是「最大正向匹配 + bigram 退化」，不是真分词**：未登录词会被切成 bigram，
   因此偶发**跨词误配**（`绩不` 之类不会命中，但像 `增收`/`减亏` 这种切法依赖词典是否收全）；
   没有词性、没有句法，**否定作用域只按「最近词条 + 不跨标点」近似**；
3. **讽刺 / 反语 / 引用**完全不处理：`贵州茅台：法院认定公司不承担任何责任` 里的
   `被执行`（3 次命中）仍按负面计——这是真机输出里 `600519 = −0.1056` 的主要来源，
   语义上该文其实是**澄清**（利好）。词典法读不出这层；
4. **领域词/新词**：词典 306 条覆盖不到的长尾（新业务、监管新词、公司黑话）会漏，
   漏了只体现在 `coverage` 上；`coverage < 0.5` 时 `notes` 会明确提示
   「分数只代表命中词典的 N 篇，不代表全部资讯」；
5. **单字否定词有误伤**：`非`（非公开发行）、`无`（无锡）、`不`（不锈钢）在未登录语境里
   可能把后一个词条误翻转；`低于`/`不足` 同表，靠 `低于预期` 这类复合词条优先匹配缓解；
6. **代码数字误命中**：东财关键字搜索会把标题里出现 `600000股` 的文章算作该标的的资讯
   （实测 `力源信息：8月12日回购公司股份600000股`）——这是**上游召回**问题，本引擎
   只按给定语料打分，不做标的归属校验；因子层用 `coverage` + `documents` 自行把关；
7. **无情绪强度标定**：极性值/程度因子是人工先验，**没有对抗标签做过校准**（没有标注集、
   没有 sklearn），因此分数只可用于横截面排序/阈值分档，**不可当概率读**；
8. **`limit` 只影响请求条数**：`stock_news_em` 实测每页 10 条，`limit>10` 不会拿到更多历史
   （更长的窗口需要接 Tushare/富途历史资讯，本轮没做）。

### 10.8 未接线部分（本轮不做，如实登记）

* 情绪因子**没有进因子矩阵/策略**：`sentiment_factor()` 是可用出口，但
  `/api/v3/factors/matrix` 与 PDAT/PET 流水线尚未消费它（加因子=改策略口径，需人工批准）；
* 富途 `info_search`（`quote_news_search`）实测**恒空**（`docs/TOOL-LIMITS.md` 已登记：
  两侧通道都归一化为 `{"news_list": []}`），所以端点的新闻面**只用 AKShare**，
  没有写「富途主源 + AKShare 兜底」的假降级链（主源恒空时那条链只会制造噪音）；
* 运行中的 8397 进程**还没有这个路由**：`/api/v3/sentiment` 要等主 agent 重启后才在线
  生效（本轮真机证据来自独立 `create_app` + `TestClient`，同一份代码、真实网络、真实只读）。

---

# 行业集中度 / 单笔 / 最大回撤：**下单前闸门真正接入**（2026-09-20 第二轮）

上一轮（§九）只做到「观测先行」：`GET /api/v3/risk/industry` 能取到真实行业暴露、
`/metrics` 也导出了 `quantwb_risk_industry_pct`，但 `v3_ops.OmsLedger.context()["industry_pct"]`
**恒为常量 `0.0`**，所以 `check_order` 不会因为行业集中度拦任何单——告警响、闸门不响。
本轮把这条口径接进判定链，**观测与闸门从此共用同一个事实源**。

## 十一、闸门接法（数据面 / 判定面 / 留痕面）

### 11.1 数据面：`platform/server/v3_risk_gate.py`（新增）

`industry_context(home, market=None, *, max_age_ms=None, wb_call=None)` 是**唯一**的行业
读数入口，按优先级取三级来源，**绝不估算、绝不跨市场合并**：

| 级别 | 来源 | 条件 | `industry_source` |
|---|---|---|---|
| 1 | 落盘探测缓存 `<home>/v3-risk-probe.json` | 缓存新鲜（`now - generated_at <= QUANT_RISK_PROBE_MAX_AGE`，默认 6h） | `cache/futu/info_owner_plate` |
| 2 | 现取一次 `v3_industry.industry_exposure`（板块映射过 `v3_ratelimit`） | 缓存缺失/损坏/过期，且调用方给了 `wb_call` | `fetch/futu/info_owner_plate` |
| 3 | 无读数 | 上面两条都不行 | `no-data`（`industry_pct=None`） |

新鲜度常量与 `/metrics` **同源**（`observability.risk_probe_max_age()`，环境变量
`QUANT_RISK_PROBE_MAX_AGE`，默认 `21600` 秒）——测试 `test_max_age_default_matches_observability`
把这条同源关系钉住，防止闸门与观测各持一套口径。

返回信封（全部可进 `risk` 与 `history` 留痕）：`market`、`industry_pct`、`industry_source`、
`as_of`/`as_of_epoch`/`probe_age_ms`、`stale`、`top_industry`、`missing`、`universe`、
`limit_pct`、`breach`、`reason`、`cache_state`（`fresh`/`stale`/`missing`/`invalid`/
`unreadable`/`fetched`）、`origin`（`cache`/`fetch`/`none`）。

### 11.2 判定面：`v3_ops.check_order`（纯函数，判定顺序固定）

| 顺序 | 条件 | 结果 | 说明 |
|---|---|---|---|
| 1 | NAV ≤ 0 或金额 ≤ 0 | `manual` | 不折算，保守退回人工确认（既有行为） |
| 2 | 单笔占比 > `LIMITS["singlePct"]`（2%） | `manual` | **维持现状** |
| 3 | **行业读数 > `LIMITS["industryPct"]`（20%）** | `blocked_industry`（硬阻断） | **本轮接入** |
| 4 | 回撤 ≥ `LIMITS["drawdownPct"]`（15%） | `blocked`（硬阻断） | **维持现状** |

* **`>` 才阻断**：`industry_pct == 20.0` 不阻断（边界用测试钉住）；
* 行业阻断**单独成态** `stage="blocked_industry"`（`STAGES` 里新增一项，**只加不删**）：
  20% 行业红线与 15% 回撤红线都是硬阻断，但「哪条规则拦的」必须一眼可读，
  否则事后只能靠 reasons 文本猜；`risk.rule` 同步给 `industry-red-line` /
  `drawdown-red-line` / `single-order` / `within-limits`；
* 行业原因原文带齐溯源信息：
  `单一行业暴露 37.5% > 20%（top=股份制银行Ⅱ，来源 cache/futu/info_owner_plate，as_of 2026-09-20T09:58:52.053639+00:00，市场 SH，探测年龄 3463s），强制阻断`；
* **多市场隔离**：台账订单按标的前缀（`v3_universe.market_of_ticker`）归市场，取**该市场**
  的暴露，跨市场不合并；`_industry_contexts` 只为**本次对账真的有订单**的市场现取
  （每个市场至多一次），所以「缓存新鲜」时一次上游都不打、「全是 SH 单」时不会顺手去探测
  HK/US。标的认不出市场前缀（裸代码）→ 取本次读到的各市场**最严**读数，并在
  `industry_reason` 里写明「认不出市场前缀 → 取最严」，不静默合并。

### 11.3 留痕面：`history` / `/api/v3/metrics`

* 台账 `history[]` 每条追加 `rule`、`changed`（是否状态演进）、`reasons`，以及
  `industry_pct`、`industry_source`、`industry_top`、`industry_market`、`industry_as_of`、
  `industry_probe_age_ms`、`industry_missing`、`industry_universe`、`industry_reason`
  ——**既有 `at`/`stage`/`reasons` 一个不动**，历史保留条数 10 → 20；
* `/api/v3/metrics` 新增 `industryGate`（只加字段）：`blockedIndustry`（台账里
  `stage="blocked_industry"` 的单数）、`blockedByIndustry` / `blockedByDrawdown`（按规则计数）、
  `industryPct` / `industrySource` / `industryTop` / `industryAsOf` / `industryProbeAgeMs` /
  `industryMissing` / `perMarket` / `failOpen` / `industryLimitPct`；
  **抓取路径只读落盘缓存、零工具调用**（与 `observability._render_risk_industry` 同一纪律，
  测试断言 `/api/v3/metrics` 不增加任何 `v3_run` 计数）；
* `/api/v3/oms/orders` 视图补 `industry_pct` / `industry_source` / `industry_top` /
  `industry_market` / `industry_as_of` / `industry_missing` / `industry_markets`（逐市场信封）/
  `industry_limit_pct`；对账结果 `oms/sync` 补 `industry_pct` / `industry_top` /
  `industry_as_of` / `industry_missing` / `industry_markets` / `industry_error`。
  `stages`/`orders` 响应结构**不变**（只是 `stages` 里多一个可能的键）。

## 十二、fail-open：为什么缺数据不阻断，以及它的风险

**决策**：行业数据不可用（缓存缺失/损坏/过期且现取失败）时**不阻断**（`industry_pct=None`
按「无读数」处理，回落到单笔/回撤两条既有红线），但**必须显式留痕**：

```
行业暴露数据不可用，未参与阻断（原因：现取失败（探测缓存已过期（93164s > 21600s），
过期读数不作为结论；industry/no-universe: market=SH 既没有配置自选池、也没有真实持仓…
——这不是取数失败，而是该市场没有可分析的标的））
```

`risk.industry_source` 同时写 `no-data`，`industry_reason` 写原始原因。

**理由**：

1. **缺数据 ≠ 超限**。「没有读数」当成 100% 会阻断**一切**（包括与行业集中度无关的
   小单），等于用监控故障停掉整个执行面；当成 0% 又静默放行——两者都是编造，
   故取「不阻断 + 强制留痕 + 指标可告警」这一档；
2. **与现状一致且可回退**：行业接入前该读数恒为 `0.0`（= 不阻断），fail-open 不引入
   *新的* 阻断面，闸门失效时行为回落到改动前；
3. **缺数据的可见性由观测面兜底**：`quantwb_risk_industry_probe_timestamp_seconds` 的
   新鲜度守卫、`RiskIndustryProbeStale`（> 6h，info）、`RiskIndustryProbeFailed`
   （warning）三条告警本来就是为「探测挂了」准备的；本轮把 `industryGate.failOpen`
   也放进 `/api/v3/metrics`，规则可以直接读。

**风险（如实列出，不掩盖）**：

1. **fail-open 窗口内行业红线不设防**：缓存过期 + 富途不可用时，超限的组合照样能过闸门
   （只受单笔 2% 与回撤 15% 约束）。要收口，需把 `QUANT_RISK_PROBE_MAX_AGE` 调小
   （见 §14）或把 `industryGate.failOpen==1` 接成 critical 告警 + 人工冻结；
2. **探测器的权重口径不是「实时持仓权重」**：`industry_exposure` 的权重来自 frozen 计划 /
   自选池等权 / 市场宇宙等权（`sources.weights` 字段写明），不是券商实时持仓市值。
   因此它衡量的是**平台组合口径**的行业集中度，不是账户真实市值口径——这条差异
   在接闸门前就存在，本轮没有改变它，也没有假装它不存在；
3. **单市场映射失败的读数只是下界**：`missing` 非空时真实暴露可能更高，此时 reasons
   会加一句「当前读数是**下界**，真实暴露可能更高」，但**判定仍按读数走**——
   读数 18% + missing 3 只会放行并留痕，不会因为「可能更高」而阻断（这是有意的：
   避免用不确定性当阻断理由）；
4. **unattributed 标的取最严市场**：裸代码订单按各市场最高暴露判定，可能比它的真实
   市场更严（偏保守，不偏宽松）；
5. **运行中的 8397 是改动前的进程**：线上要到主 agent 统一重启后才生效
   （本轮真机证据是「真实缓存 + 构造订单过 `check_order`/`_upsert`」的只读验证，
   见 §13）。

## 十三、真实放行/阻断样例（真机只读，含 reasons 原文）

脚本：`platform/deploy/monitoring/verify_industry_gate.py`（只读 `~/.dsh/v3-risk-probe.json`，
台账用内存构造值，不写盘、不下单）。真机缓存（2026-09-20T09:58:52Z，年龄 0.95h < 6h，
`cache_state=fresh`）实测：

```
$ cd platform && ~/.dsh/trading-venv/bin/python -B deploy/monitoring/verify_industry_gate.py
=== 0. 落盘探测缓存（真实读数） ===
generated_at=1789898332.0536346 (2026-09-20T09:58:52.053639+00:00) limit_pct=20.0
max_age_s=21600 age_h=0.95
  HK: top=半导体 40.0% breach=True source=futu/info_owner_plate missing=0 universe=5
  SH: top=股份制银行Ⅱ 37.5% breach=True source=futu/info_owner_plate missing=0 universe=28
  US: top=半导体 50.0% breach=True source=futu/info_owner_plate missing=0 universe=2

=== 2. 构造订单过「下单前闸门」 ===
[SH-1pct] SH.600000 value=1000 (1.00% NAV) 行业读数=37.5%（股份制银行Ⅱ）
  action=blocked_industry stage=blocked_industry
  reason: 单一行业暴露 37.5% > 20%（top=股份制银行Ⅱ，来源 cache/futu/info_owner_plate，
          as_of 2026-09-20T09:58:52.053639+00:00，市场 SH，探测年龄 3463s），强制阻断
[HK-1pct] HK.00700 value=1000 (1.00% NAV) 行业读数=40.0%（半导体）
  action=blocked_industry stage=blocked_industry
  reason: 单一行业暴露 40.0% > 20%（top=半导体，来源 cache/futu/info_owner_plate，…，市场 HK…），强制阻断
[US-1pct] US.NVDA value=1000 (1.00% NAV) 行业读数=50.0%（半导体）
  action=blocked_industry stage=blocked_industry
  reason: 单一行业暴露 50.0% > 20%（top=半导体，来源 cache/futu/info_owner_plate，…，市场 US…），强制阻断
[RAW-no-prefix] 600000 value=1000 (1.00% NAV) 行业读数=50.0%（半导体）
  action=blocked_industry stage=blocked_industry
  reason: 单一行业暴露 50.0% > 20%（…，探测年龄 3463s），强制阻断
```

> **真实数据的一个诚实现象**：这份缓存里 **SH 37.5% / HK 40% / US 50% 三个市场全部
> 超 20%**，所以真机口径下「放行样例」在当前组合里**不存在**——三市场单子全被硬阻断。
> 放行路径由离线单测覆盖（`test_reading_but_not_breaching_is_auto`：15% → `risk_passed`；
> `test_limit_boundary_equal_is_not_blocked`：20% 整 → 不阻断）。

阻断单的台账留痕（`_upsert` 内存对象，真实字段）：

```json
{
  "id": "AUDIT-SH-40pct",
  "stage": "blocked_industry",
  "industry_pct": 37.5,
  "industry_source": "cache/futu/info_owner_plate",
  "industry_top": "股份制银行Ⅱ",
  "industry_as_of": "2026-09-20T09:58:52.053639+00:00",
  "risk": {"action": "blocked_industry", "rule": "industry-red-line",
           "industry_source": "cache/futu/info_owner_plate", "industry_pct": 37.5,
           "reasons": ["单笔占比 40.00% > 2%，需人工确认",
                       "单一行业暴露 37.5% > 20%（top=股份制银行Ⅱ，来源 cache/futu/info_owner_plate，as_of 2026-09-20T09:58:52.053639+00:00，市场 SH，探测年龄 3463s），强制阻断"]},
  "history": [{"at": "2026-09-20T10:56:08.370122+00:00", "stage": "blocked_industry",
               "rule": "industry-red-line", "changed": true,
               "industry_pct": 37.5, "industry_source": "cache/futu/info_owner_plate",
               "industry_top": "股份制银行Ⅱ", "industry_market": "SH",
               "industry_as_of": "2026-09-20T09:58:52.053639+00:00",
               "industry_probe_age_ms": 3436311.6998672485, "industry_missing": 0,
               "industry_universe": 28, "industry_reason": null,
               "reasons": ["…同上两条…"]}]
}
```

fail-open 真机样例（把最大年龄压到 1ms 模拟过期）：

```
=== 4. fail-open：把最大年龄压到 1ms（模拟缓存过期）→ 不阻断但必须留痕 ===
cache_state=stale industry_pct=None industry_source=no-data
reason=探测缓存已过期（3447s > 0s），过期读数不作为结论
check_order → action=auto（不阻断）
reasons=['行业暴露数据不可用，未参与阻断（原因：探测缓存已过期（3447s > 0s），过期读数不作为结论）']

=== 5. /api/v3/metrics 口径（gate_view，只读缓存、零工具调用） ===
{"blockedIndustry": 0, "blockedByIndustry": 0, "blockedByDrawdown": 0,
 "industryLimitPct": 20.0, "industryPct": 50.0,
 "industrySource": "cache/futu/info_owner_plate", "industryTop": "半导体",
 "industryAsOf": "2026-09-20T09:58:52.053639+00:00", "industryMissing": 0,
 "perMarket": {"HK": 40.0, "SH": 37.5, "US": 50.0}, "failOpen": false}
```

线上服务（**改动前的进程**）对照：`/api/v3/oms/orders` 仍返回旧的
`"industry_source": "no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）"`、
无 `industryGate` 字段；`/api/v3/risk/industry?market=SH&limit_pct=20` 的真实读数
`breach=true, top={"industry":"股份制银行Ⅱ","weightPct":37.5}, missing=[]`——重启后两者
才会来自同一份落盘缓存。

## 十四、怎么调阈值与最大年龄

| 想改什么 | 改哪里 | 影响 |
|---|---|---|
| 行业红线（默认 20%） | `platform/server/v3_ops.py` 的 `LIMITS["industryPct"]` | 闸门判定 + `/metrics` 的 `industryLimitPct`；**探测落盘时的 `limit_pct` 是探测当刻的快照**，改常量后应重跑一次 `POST /api/v3/metrics/probe/refresh` 让两者一致（`tests/test_observability.py` 的 `test_industry_redline_rules_match_the_platform_limit` 把 `alerts.yml` 的 20 与 `LIMITS["industryPct"]` 钉在一起） |
| 单笔红线（默认 2%） | `LIMITS["singlePct"]` | `manual` 档（不阻断） |
| 回撤红线（默认 15%） | `LIMITS["drawdownPct"]` | `blocked` 档（硬阻断） |
| 探测缓存最大可信年龄（默认 6h） | 环境变量 `QUANT_RISK_PROBE_MAX_AGE`（秒） | **同时**影响闸门读数是否可用与 `/metrics` 是否导出取值类指标；调小会让 fail-open 窗口更短但更容易在探测漏跑时进入 fail-open |
| 现取开关 | 调用方是否传 `wb_call` | `OmsLedger.sync` 会传（缓存不可用时现取一次）；`/api/v3/metrics`、`/api/v3/oms/orders` 视图**只读缓存**，永不因看板刷新去打富途 |

> 调阈值时**不要**顺手删/改告警规则里的 `20`：`alerts.yml` 的阈值与 `LIMITS` 由测试
> 交叉校验（改常量后规则会测挂），这是有意为之的防漂移。

## 十五、测试

```bash
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_risk_gate -v   # 28 OK
cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests             # 643 OK（含并行其他轮）
node --test tests/*.test.mjs                                                              # 73 pass
```

基线口径：本轮开工时 `discover -s tests` 为 **545 OK**、`node --test tests/*.test.mjs`
为 **73 pass**；完工复跑为 **643 OK**（= 545 + 本轮新增 28 + 并行其他 agent 同期的用例）
与 **73 pass**（本轮不改前端/JS）。

`tests/test_v3_risk_gate.py` 覆盖：缓存命中阻断（并断言**零上游调用**）、缓存过期 + 现取成功
阻断、现取失败 → fail-open + reasons 写明不可用、`industry_pct == limit` 边界、单笔/回撤不回归、
多市场隔离（SH 超限不影响 HK 单）、裸代码取最严、`history` 留痕字段齐全、`missing` 作下界标注、
`generated_at` 缺失/非法不猜时刻、新鲜度常量与 `observability` 同源、`/api/v3/metrics` 计数
且不触发探测。

## 十六、未解决项（如实记录）

1. **线上生效要等重启**：运行中的 8397 仍是旧进程（`industryGate` / 新 `industry_source`
   都要重启后才有）；
2. ~~**未接前端**：`platform/web-pro/src/pages/execution.jsx` / `risk.jsx` 里仍写着
   「行业上限只做展示与人工核对，未接入自动阻断」~~ —— **已由前端修复轮收口**
   （2026-09-20，见 §十七）：过时文案已删；`stage="blocked_industry"` 与 `blocked` 同等按
   红色阻断渲染；`risk.rule` 机器码翻成中文；`/metrics.industryGate` 的读数（含
   `industrySource` / `industryAsOf` / `industryProbeAgeMs` / `missing`）上屏；
   `failOpen=true` 时概览/网关/风控/执行四页给可见告警，且 `industry_pct=None` 一律显示
   「无读数」而不是 0%；阈值改为「接口字段 → 台账 `risk.reasons` 原文回读 → 内置常量（逐行标注）」三级取值；
3. **探测心跳依赖定时器**：`platform/install/quant-v3-probe.timer` 仍未 enable/start，
   缓存过期即 fail-open（可观测：`failOpen=true` + `RiskIndustryProbeStale`）；
4. **权重口径仍是平台组合口径**（见 §12-2），不是券商实时持仓市值；
5. **回测/研究侧不经过这道闸门**：`check_order` 只服务 OMS 台账登记（`plan` 的 frozen 计划
   订单），工作台 Web 的 `plan_execute` 仍走它自己的口令 + 人工确认链路——本闸门是
   **平台侧登记的判定与留痕**，不是券商端的拒单。

---

# 数字诚实性修复（前端 10 页 + 文档口径，2026-09-20）

**红线口径**：页面上不得出现任何会被读成真实数据的回退值 / 占位数字；不得出现「示例数据」
字样；缺数据必须显式说「不可用 / 加载中」并给出**原因**。真 0 与「没有数据」必须可区分。

**三态判定规则（统一实现 `platform/web-pro/src/lib/stat-core.js`，可被 `node --test` 直测）**

| 状态 | 触发条件 | 页面渲染 | 原因文案 |
|---|---|---|---|
| `loading` | 请求在途（`useV3().loading`） | `—` | 「加载中…」 |
| `error` | 请求抛错或信封 `ok:false` | `—` | 「取数失败：<服务端原文>」 |
| `missing` | 请求成功但字段为 `null`/`undefined`/`""` | `—` | 调用方给出（字段名 + 为什么没有） |
| `value` | 请求成功且字段有值（**含 0**） | 接口返回值 | 无 |

为什么必须显式做：antd `Statistic` 的 `value` 解构默认是 **0**
（`node_modules/antd/es/statistic/Statistic.js`），`value={undefined}` 会被渲染成 `0`；
`value={null}` 走 `String(value)`（`node_modules/antd/es/statistic/Number.js`）被渲染成字面量
`null`（带 `suffix="%"` 就是 `null%`）。**两条都是实测行为，不是推测。**

## 17.1 逐条：修前现象 → 修后行为

| # | 文件:行（修前定位） | 修前渲染什么 | 修后渲染什么 | 判定规则 |
|---|---|---|---|---|
| H1 | `pages/gateway.jsx:93-94` → `:107-109` | metrics 未回来时 MCP 卡显示「今日调用 0 次 / 失败 0 次 / 平均延迟 0 ms」 | 「今日调用 —」「成功率 —」「平均延迟 —」，卡内 `Alert` 列出**每项原因**（加载中 / 取数失败原文 / 字段缺失） | 三态；真 0 仍显示 0 并保留「进程内计数」脚注 |
| H2 | `pages/tools.jsx:197-200` | 4 项 `Statistic` 收 `undefined` → 加载中显示 `0` | 显式 `—`，总览条下方汇总「有 N 项暂无读数（显示「—」，不是 0）」+ 逐项原因 | 三态 |
| H3 | `pages/tools.jsx:248→:259`、`:286` | metrics 失败时每个域/每个工具都显示「今日 0」 | 域卡与工具行「今日 —」；metrics 取到时，计数表里**没有该工具名 = 该工具 0 次调用**（后端只在首次调用建条目），此时才显示真 0 | 三态；缺失=真 0 只在「表语义就是计数器」时成立 |
| H4 | `pages/strategy.jsx:503-506` | 回测指标缺失显示字面量 `null%` / `null` | 显式 `—`，且**同时去掉 `suffix` 与颜色**（避免 `—%` 与假红绿） | 有值才给数字/单位/色 |
| H5 | `pages/overview.jsx:397/406/413` → `:727` | 硬编码 `limit: 2/20/15` 以「上限 x%」当数据渲染 | 逐行标注来源 chip：**接口字段**（行业 ← `metrics.industryGate.industryLimitPct`，退化 `risk/industry.limitPct`）/ **台账原文**（单笔、回撤 ← 订单 `risk.reasons` 回读闸门实际应用值）/ **内置常量**（两者都取不到时，按 `v3_ops.LIMITS` 标注） | 接口字段 → 台账原文 → 内置常量 三级，每级显式上屏 |
| H6 | `pages/overview.jsx:708` | 文案「回撤阈值取自台账，为台账口径」——与代码不符（15 是常量） | 改为逐行来源标注；回撤无 reasons 回读时明确写「默认阈值（内置常量 v3_ops.LIMITS，后端未暴露字段）」 | 不实声明删除；来源必须与取值路径一致 |
| H7 | `pages/overview.jsx:882` | `risk.reasons` 缺失时断言「阈值内」 | 「未返回判定依据（订单 risk.reasons 缺失，本页不代平台下结论）」 | 缺依据不得下结论 |
| H8 | `pages/risk.jsx:835/837`（规则表）+ `:432`（事前风控） | `threshold: "≤ 权益 2%"` + `singleRatio > 2` 判定「正常/超限」——用前端字面量当接口阈值 | 阈值从台账 `risk.reasons` 原文回读（「单笔占比 6.90% > 2%，需人工确认」→ 2%），显示「阈值从台账 risk.reasons 原文回读」；回读不到则「≤ 权益 —（台账 reasons 未回读到阈值；后端默认 2%，为内置常量）」并显示「无数据」，**不再凭空判「正常」** | 判定只用后端读数/阈值；无阈值→`无数据` |
| H9 | `pages/risk.jsx:686` | 图注「满刻度 30%」但 `BarList` 不传 `max`（自动缩放） | 图注与条形**共用同一变量** `barScalePct = max(30, ceil(最大值/10)*10)`，并显示「条形按固定满刻度 N% 绘制……不存在两套刻度」 | 文案与图形同源 |
| H10 | `pages/execution.jsx:34` → `:1042` → `:1066` | `FALLBACK_TTL_SECONDS = 120` 缺失时显示「TTL 120 秒」，把前端常量当服务端 TTL | `FALLBACK_TTL_SECONDS` 已删；缺失时显示「TTL —（未取到 confirmation.ttl_ms）」。实测服务端返回 `ttl_ms=120000` → 显示「TTL 120 秒」（此时是**真实服务端值**） | 只显示服务端值，取不到就说取不到 |
| H11 | `pages/settings.jsx:1314` | 「LIVE 大额订单需双人复核后执行」——`platform/server` 与 `scripts` 全域**无实现** | 「未启用（本仓库 platform/server 与 scripts 全域无「双人复核」实现，接口与配置里也没有对应字段；此处此前宣称 LIVE 大额订单需先过双人复核，属于不存在的能力，已撤下该文案）」 | 不得宣称不存在的能力 |
| H12 | `pages/settings.jsx:1290`；`pages/execution.jsx:395/400/401` | 「单笔 ≤ 2% · 行业 ≤ 20% · 回撤 ≤ 15%（v3_ops.LIMITS）」读起来像接口读数 | 「默认阈值 · 后端 v3_ops.LIMITS 内置常量；本页未从接口读取——实测 /api/v3/oms/orders 不返回单笔/回撤阈值字段，行业上限的真实字段在 /api/v3/risk/industry.limitPct」；execution 的单笔 2% 改为台账 reasons 回读 | 同 H5 |
| H13 | `pages/tools.jsx:73-81` | 用码位 `String.fromCharCode(0x793A,0x4F8B)` 拼词再把上游说明里的该词替换成「样例」 | 替换逻辑与码位构造**整体删除**；`toolDesc` 只做 markdown `**` 强调符归一化，其余逐字等于 `/api/v3/tools` 返回。实测 DOM 现含上游原文「真机验证过的最小示例」，且不再出现「样例」 | 显示文本必须与上游数据一致 |
| H14 | `pages/research.jsx:169-181` | 加载中/取失败时五个计数全显示 `0` | 五个计数三态化（`—` + 逐项原因），并在卡内汇总「有 N 项暂无读数（显示「—」，不是 0）」；`ok:false` 信封也计入失败态 | 三态 |
| H15 | `pages/brain.jsx:252/376/803` | `factorsError` 缺失一律「0 条」；`action_hint` 缺失时编出「经人工审批后执行」 | 「—（PAAT 未返回 factorsError 字段）」/「0 条（接口返回 null）」；「—（接口未返回 action_hint，本页不代平台给执行建议）」 | 缺字段不编内容 |
| H16 | `pages/risk.jsx:549` | 固定断言「equity 台账只有 1 个点位，无法算回撤」 | 台账最大回撤改读 `/api/v3/overview.equity.max_drawdown`（真实字段，实测 0.00%），并显示真实点位个数与来源 | 断言必须由读数派生 |
| H17 | `pages/settings.jsx:202` | `exec_window_minutes` 缺失时表单填 30，无任何标注（会被读成已生效配置） | 该字段旁标注「· 当前为表单默认值 30（配置里未设置）」 | 默认配置必须标「默认值」 |

## 17.2 行业闸门（`blocked_industry`）的前端适配

后端 2026-09-20 新增第三个风控态与 `industryGate` 读数（见 §十一～§十三）后，前端原先按
`stage === "blocked"` 字面量判色，`blocked_industry` 会落成**灰色**——与「行业超限被强制阻断」
的事实相反。本轮统一收口到 `platform/web-pro/src/lib/risk-labels.js`：

| 契约 | 前端行为 |
|---|---|
| `stage="blocked_industry"` | 标签「行业红线阻断」+ **红色**（`stageTone` 与 `blocked` 同级）；「强制阻断」卡与「阻断记录」表都把它计入 |
| `risk.rule` ∈ `industry-red-line` / `drawdown-red-line` / `single-order` / `within-limits` | 翻成「行业红线 / 回撤红线 / 单笔超限 / 阈值内」；未知规则**原样显示**（不硬翻） |
| `industryGate.industryPct === null`（`industry_source="no-data"`） | 显示「无读数 → 行业红线**未参与阻断**（fail-open），不是「暴露 0%」」，**不显示 0%** |
| `industryGate.industrySource/industryAsOf/industryProbeAgeMs/missing` | 逐项上屏（来源 + as_of + 探测年龄；`missing>0` 标注「读数只是下界，真实暴露可能更高」） |
| `industryGate.failOpen === true` | 概览 / 网关 / 风控 / 执行四页给**可见告警**（真实风险窗口，不是装饰） |
| `industryGate` 缺失（旧进程） | 如实显示「未取得 industryGate（本服务进程可能早于行业闸门改动）」——**不猜、不补默认值** |

**运行进程现状（实测）**：8397 仍是闸门改动前的进程，`/api/v3/metrics` **没有** `industryGate`，
`/api/v3/oms/orders` 也**没有** `industry_limit_pct`，`industry_source` 仍是
`no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）`。因此页面当前落在
「未取得 industryGate」这一支，**不得**据此宣称闸门已生效——重启后才会切到真实读数分支。

## 17.3 验证（真实输出）

```
$ cd platform/web-pro && flock /tmp/probuild.lock npm run build
vite v6.4.3 building for production...
✓ 3872 modules transformed.
dist/assets/index-Bpjsc4Nj.js  1,857.84 kB │ gzip: 573.63 kB
✓ built in 1m 22s

$ node --test tests/*.test.mjs
# tests 211
# pass 211
# fail 0
```

* 基线 188 通过 → 现 **211 通过 0 失败**（新增 `tests/stat-honesty.test.mjs` 23 例：
  三态纯函数 + 上述 H1–H17 与闸门 G1–G6 的源码级回归，防止回退值复活）；
* 构建产物已由运行中的 8397 直接服务（`GET /` 引用 `assets/index-Bpjsc4Nj.js`，
  该静态资源 `200`），无需重启服务；
* 真实浏览器（`chromium --headless=new --dump-dom`）逐页取 DOM 复核：`tools` 页出现上游原文
  「真机验证过的最小示例」且「样例」为 0 次；`overview` 三条红线分别显示
  「上限 2% / 台账原文」「上限 20% / 接口字段」「上限 15% / 内置常量」；`risk` 页台账最大回撤
  显示 `0.00%` + 「台账点位 1 个 · 来源 /api/v3/overview.equity.max_drawdown」；`settings` 页
  「双人复核」显示「未启用」；`execution` 页 TTL 显示服务端真实值 120 秒。

## 17.4 用词冲突（如实登记，未擅自改测试）

任务书要求 tools 页加「暂无数据」说明；仓库既有契约测试
`tests/console-parity.test.mjs:75-83` 明确**禁止**页面出现「暂无数据」「演示数据」「mock」「dummy」，
只允许统一措辞「无数据源 + 原因」（`services/api.js` 的 `noSourceText`）。两者冲突时按**更严**
的一侧执行：本轮统一用「暂无读数（显示「—」，不是 0）」+ 逐项原因表达同一语义，
既满足「缺数据必须显式说明并给原因」的红线，也不破坏既有测试不变量。

## 17.5 已知副作用：`platform/tools/verify_pages.sh` 的 DOM 断言

该脚本断言「DOM 无「示例」字样」。H13 改为原样显示上游文本后，tools 页会渲染出
`/api/v3/tools` 原文里的「真机验证过的最小示例」，脚本这一条会**红**。
按用户口径（禁的是「示例数据」这层语义标识，不是上游文本本身）这是预期行为；
该脚本不在本轮允许改动的路径（`platform/web-pro/src/**` 与 `docs/*.md`）内，故仅登记，
建议后续把断言从「DOM 不含该二字」改为「DOM 不含「示例数据」这一语义标识 + 上游原文需带来源标注」。

---

# 数字诚实性（续）：行业闸门**前**的历史判定 —— 视图层归一化，不回写、不重判（2026-09-20）

**红线口径**：页面上不得出现把「当时没有读数」说成「该单行业暴露 0%」的数字，也不得复述
已经被现实验伪的断言。真 0 与「没有数据」必须可区分（与 §17 同一条红线）。

## 18.1 现象（真实数据，实测）

`GET /api/v3/oms/orders` 的 10 笔存量订单（2026-09-19 落盘）带的是**闸门上线前**的判定快照：

```json
{
  "id": "6da8323efdb14620aa3b0fe7c48b5bbe", "ticker": "SZ.002716", "stage": "manual",
  "industry_pct": 0.0,
  "industry_source": "no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）",
  "risk": { "action": "manual", "reasons": ["单笔占比 6.90% > 2%，需人工确认"] },
  "history": [{ "at": "2026-09-19T12:25:45.643351+00:00", "stage": "manual",
                "reasons": ["单笔占比 6.90% > 2%，需人工确认"] }]
}
```

（`risk` 无 `rule`，`history` 条目无 `rule` / `changed` / `industry_*`。）

两处不诚实：

1. 那句「**工具面无行业分类数据源**」**现在已经不成立**——`v3_risk_gate.industry_context`
   已接通 `futu/info_owner_plate` 真实行业映射（实测 SH 37.5% / HK 40% / US 50%，
   `~/.dsh/v3-risk-probe.json`），台账里的这句是**已失效的断言**；
2. `industry_pct: 0.0` 在页面上会被读成「该单行业暴露 0%」，而事实是**当时没有行业读数**。

## 18.2 判据：结构性、可解释，**不猜时间戳**

`platform/server/v3_ops.py:163` 的 `legacy_pre_gate(record)` 认定「闸门前的历史判定」当且仅当：

| # | 判据 | 为什么可信 |
|---|---|---|
| 1 | `industry_source` 含 `LEGACY_INDUSTRY_SOURCE_TEXT`（`"工具面无行业分类数据源"`，`v3_ops.py:153`） | 当前实现只会写 `cache/futu/...` / `fetch/...` / `no-data`，这句话**已不可能由现在的代码产生** |
| 2 | 记录**有分级留痕**（`risk.action` 或非空 `history`），但整条记录没有任何闸门标记（`risk.rule` / `history[].rule` / `history[].industry_*`） | `risk.rule` 与 `history[].rule` / `history[].industry_*` 与行业闸门是**同一次改动**引入的（版本库事实，见下） |

判据 2 的证据（可复核）：

```
$ git show HEAD:platform/server/v3_ops.py | grep -n '"risk": {\|history.append'
844:            "risk": {"action": action, "reasons": list(reasons)},
825:        history.append({"at": stamp, "stage": stage, "reasons": list(reasons)})
```

而当前工作区（闸门改动后）是 `risk` 带 `rule`/`industry_*`、`history` 条目带
`rule`/`changed`/`industry_*`。**因此「有没有闸门标记」与「是不是闸门前的判定」是同一件事**，
不需要也不应该硬编码一个上线时刻去比较 `updated_at`（闸门没有落盘的生效时间标记，猜一个时刻
等于把猜测当证据）。这也是本次任务书留的口子：「若判断闸门前的判定无法与上线后区分，
如实说明」——**能区分**，依据是上面的版本库事实而不是时间戳。

边界：连分级留痕都没有的记录（例如从冷备迁移进来、只有 `id/ticker/stage` 的条目）
**不**判为历史判定，视图也不替它下任何结论（无从谈「含不含行业红线」）。

## 18.3 实现位置

| 层 | 文件:行 | 做了什么 |
|---|---|---|
| 判据 + 视图归一（纯函数） | `platform/server/v3_ops.py:153-226` | `LEGACY_INDUSTRY_SOURCE_TEXT` / `LEGACY_INDUSTRY_SOURCE` / `LEGACY_INDUSTRY_NOTE`、`legacy_pre_gate()`、`order_view()` |
| 接进返回视图 | `platform/server/v3_ops.py:1400` | `OmsLedger.view()` 的 `orders` 逐单过 `order_view()`；`/api/v3/oms/orders` 与 `/api/v3/execution` **同走这一条**，口径不可能漂移 |
| 前端纯函数 | `platform/web-pro/src/lib/risk-labels.js:145-220` | `orderIndustryView(order)`：历史判定 / 真读数 / `null` fail-open / 无字段 四种如实渲染；旧后端兜底只认那句失效断言 |
| 前端渲染 | `platform/web-pro/src/pages/execution.jsx:787, 813-825` | 「风控阈值口径」改用 `orderIndustryView`：主文案 + 悬停说明 + 可见说明行 |

`order_view()` 对**闸门前的历史判定**做三件事（磁盘/库一个字不动）：

* `industry_pct` → `None`（**不是 `0.0`**）；
* `industry_source` → `"历史判定（该单登记于行业闸门上线前，当时无行业读数）"`
  （**不再出现**「工具面无行业分类数据源」）；
* 追加 `industry_note`（说明该判定未包含行业红线）。

对**闸门后的新订单**只**追加**两个自解释标记（不改任何既有字段的值）：
`industry_graded: true` / `legacy_pre_gate: false`。`risk` / `history` 原文一律不动。

## 18.4 改前 / 改后（真实响应片段）

**改前**（运行中的 8397，改动前的进程；`curl -s http://127.0.0.1:8397/api/v3/oms/orders`）：

```json
{"id": "6da8323efdb14620aa3b0fe7c48b5bbe", "ticker": "SZ.002716", "stage": "manual",
 "industry_pct": 0.0,
 "industry_source": "no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）",
 "risk": {"action": "manual", "reasons": ["单笔占比 6.90% > 2%，需人工确认"]}}
```

**改后**（同一笔存量单；把真实台账**复制**到临时 home、用 `TestClient` 走 8397 同一份代码路径，
未触碰运行中的服务与真实数据）：

```json
{"industry_pct": null,
 "industry_source": "历史判定（该单登记于行业闸门上线前，当时无行业读数）",
 "industry_graded": false,
 "legacy_pre_gate": true,
 "industry_note": "该判定未包含行业红线：登记时行业闸门尚未接入，台账没有行业读数——这里的「—」是「当时没读到」，不是「行业暴露 0%」。原始 risk.reasons / history 原样保留，本视图不回写、不重判。",
 "risk": {"action": "manual", "reasons": ["单笔占比 6.90% > 2%，需人工确认"]},
 "history": [{"at": "2026-09-19T12:25:45.643351+00:00", "stage": "manual",
              "reasons": ["单笔占比 6.90% > 2%，需人工确认"]}]}
```

同一次运行里核对：磁盘冷备里的 `industry_pct` 仍是 `0.0`、`history` / `risk` 与视图逐字段相同
（**未回写、未重判**），且视图 JSON 里 `工具面无行业分类数据源` 出现次数为 **0**。

**闸门后的新单（可构造，同一临时 home）**：注入一份新鲜的落盘读数
（SH 37.5% / `futu/info_owner_plate`）后 `sync` 登记一单：

```json
{"id": "CID-NEW", "stage": "blocked_industry",
 "industry_pct": 37.5, "industry_source": "cache/futu/info_owner_plate",
 "industry_as_of": "2026-09-20T12:00:00+00:00", "industry_probe_age_ms": 13.9,
 "industry_graded": true, "legacy_pre_gate": false,
 "risk": {"action": "blocked_industry", "rule": "industry-red-line",
          "reasons": ["单一行业暴露 37.5% > 20%（top=股份制银行Ⅱ，来源 cache/futu/info_owner_plate，as_of 2026-09-20T12:00:00+00:00，市场 SH，探测年龄 0s），强制阻断"]}}
```

真实字段（读数 / 来源 / as_of / 探测年龄 / `rule`）原样，只多了两个标记——
**闸门后的新单不受影响**。

## 18.5 前端渲染改前 → 改后

| 订单 | 改前渲染 | 改后渲染 |
|---|---|---|
| 存量单（`industry_pct=0.0`） | `行业 0.00%（来源 no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对））` | `行业暴露 — · 历史判定（未含行业红线）` + 悬停/说明行：`来源 历史判定（该单登记于行业闸门上线前，当时无行业读数）` + `该判定未包含行业红线：…（原始 reasons/history 原样保留）` |
| 闸门后新单（37.5%） | `行业 37.50%（来源 cache/futu/info_owner_plate · as_of …）` | 不变，另加 `· 探测年龄 14s` |
| 闸门后无读数（`industry_pct=null`） | `行业 无读数（来源 no-data） → 行业红线未参与阻断（fail-open）` | `行业暴露 — · 未参与阻断（fail-open）`（`null` **不**显示 0.00%） |
| 闸门后真实读数为 0 | `行业 0.00%` | 仍 `行业暴露 0.00%`（**真 0 必须保留**，不能一刀切） |
| 台账无 `industry_*` 字段 | `行业 无读数（来源 no-data）` | `行业暴露 — · 台账未记录行业读数`（**不**冒充 fail-open / 历史判定） |

页面**不再**内联 `fmt.pct(current.industry_pct)`：源码级回归断言
`platform/web-pro/tests/legacy-industry.test.mjs:162` 钉住这一点。

「改前」一列是**旧表达式 + 真实 `fmt.pct` + 真实台账值**跑出来的确定结果
（`node -e "import('./src/services/api.js')…"` → `行业 0.00%（来源 no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对））`），
不是事后描述。「改后」一列是**真实页面 DOM**（`chromium --headless=new --dump-dom
http://127.0.0.1:8397/#/execution`，构建产物已由 8397 静态服务、后端仍是旧进程 →
走前端兜底分支）：

```html
<span style="font-size:12px; border-bottom:1px dotted rgb(139,148,158);">行业暴露 — · 历史判定（未含行业红线）</span>
 · 判定规则 未返回 rule 字段 · 单笔风险预算 1.0%（config.risk_per_trade）
<span style="font-size:11px;">来源 历史判定（该单登记于行业闸门上线前，当时无行业读数）</span>
<span style="font-size:11px;">该判定未包含行业红线：登记时行业闸门尚未接入，台账没有行业读数——这里的「—」是「当时没读到」，不是「行业暴露 0%」。原始 risk.reasons / history 原样保留（未回写、未重判）。</span>
```

整页 DOM 里 `行业暴露 0.00%` 出现 **0** 次、`工具面无行业分类数据源` 出现 **0** 次。

## 18.6 测试（真实输出）

```
$ cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests
Ran 660 tests in 125.607s
OK                       # 基线 654 OK, 1 skipped → 660 OK（新增 LegacyIndustryViewTests 6 例）

$ cd platform/web-pro && node --test tests/*.test.mjs
# tests 222
# pass 222
# fail 0                # 基线 211 → 222（新增 tests/legacy-industry.test.mjs 11 例）

$ cd platform/web-pro && flock /tmp/probuild.lock npm run build
✓ built                # vite build 成功
```

新增覆盖：

* Python `platform/tests/test_v3_ops.py:732`（`LegacyIndustryViewTests`）：
  ① 闸门前的旧记录 → 视图 `industry_pct is None` + `legacy_pre_gate` + `industry_graded=false`
  + 文案不含「工具面无行业分类数据源」，且**磁盘文件逐字节不变**；
  ② 落库的存量单 → 库里 `payload` 与 `industry_pct=0.0` 原样（不回写库）；
  ③ 闸门后的新记录（真实读数 37.5% + `rule=industry-red-line`）字段原样，只多两个标记；
  ④ 闸门后无读数 → `no-data` + `industry_graded=true`，**不**被判成历史判定；
  ⑤ 判据是结构性的（加 `risk.rule` / `history[].industry_*` 即不再判为历史；无分级留痕的迁移数据不判）；
  ⑥ `order_view` 返回副本，改视图不动原始记录。
* 前端 `platform/web-pro/tests/legacy-industry.test.mjs`：
  A1–A8 纯函数（旧记录→「—」+ 历史判定且**不出现 0.00%**、后端已归一化的视图同结果、
  真读数照常、`null`→fail-open、**真 0 仍显示 0.00%**、无字段如实说「未记录」、非对象不抛、
  兜底识别不看时间戳）；B1–B3 源码级（执行页确实走 `orderIndustryView`、内联
  `fmt.pct(current.industry_pct)` 不再出现、说明行可见而非只藏悬停）。

## 18.7 未解决项（如实登记）

* **运行中的 8397 仍是改动前的进程**：改动落地后需要由主 agent 统一重启才能看到新视图与
  新构建产物生效；在此之前真实端点仍返回 §18.4 的「改前」形状。前端对旧后端有兜底
  （按那句失效断言识别），所以**即使不重启，存量单也不会再被渲染成 0.00%**。
* **不会重判历史**：视图**只**说明「当时没有行业读数」，不会拿今天的 37.5%/40%/50%
  去回填这 10 笔存量单的 `industry_pct`——那会把「今天的读数」伪装成「当时的判定」。
  若需要「按今天的行业读数回看这批存量单会怎样」，只能作为**新的**只读分析另开一块，
  并显式标注数据时刻，不属于本次归一化。
* 行业读数目前只覆盖平台自选池口径（`cache/futu/info_owner_plate` + 平台组合权重），
  不是全账户持仓的行业暴露；这一缺口见 §三「缺失数据源清单」。

