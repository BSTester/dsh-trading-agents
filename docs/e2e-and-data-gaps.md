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
