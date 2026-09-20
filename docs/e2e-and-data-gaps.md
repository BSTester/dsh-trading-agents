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
