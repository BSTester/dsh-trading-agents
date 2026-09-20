# V3 全量端到端测试与数据源缺口盘点（2026-09-20）

## 一、方法

```bash
~/.dsh/trading-venv/bin/python platform/tools/e2e_probe.py \
  --base http://127.0.0.1:8397 --json /tmp/e2e.json --md /tmp/e2e.md
```

- 覆盖 **32 个 `/api/v3/*` 路由 + 82 个 `/api/wb/<endpoint>` 端点**，共 113 项；
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

