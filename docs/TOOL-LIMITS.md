# 富途工具与数据源：已知限制

> 来源：2026-09-13 一次全量体检（91 个富途 MCP 工具 + 8 个非富途工具，逐个实调至少一次，
> 判据为 `ret_code` 与接口级错误码，不依赖数据新鲜度）。
> 本文件是**结论的落库版本**——原始报告在某个工作区的 `.tradingagents/` 下，
> 不随仓库分发；把结论放进仓库是为了让后续会话不必重新发现同样的坑。

## 一、最常见的错觉

下面这些问题**都不报错**，只返回 `ret_code: 0` + 空数据。如果不知道，很容易
把它当成"这个标的没有新闻/没有评级"，进而写出看起来正常但依据为空的分析。

| 场景 | 不要用 | 改用 | 证据 |
|---|---|---|---|
| 取新闻 | `quote_news_search` | **`fin_news`** | 前者三种参数（`腾讯` / `00700` / `Tencent+lang=en`）均 `data: []`；后者走富途源 `sources_status.futu="ok"`，正常返回腾讯回购、南向资金等 |
| 取机构评级 | `quote_research_rating_summary` | **`quote_research_analyst_consensus`** | 前者 `HK.00700` 返回 `analyst_rating_summary_list: null, pagination.total: 0`；后者同一标的返回 43 位分析师、目标均价 663.69、strong_buy 79.07% |
| 改单 | 重试 `sim_trade_modify_order` | **撤单 + 重新下单** | 该接口间歇性 `ret_code:-5 backend business error`；同一天 7137731 成功、7137730 与 7137795 失败，与价格是否离谱无关，失败时改价未生效 |
| 查历史订单 | `account_orders_history` **不传时间范围** | **必须传 `start`/`end`** | 不传时静默返回**纯文本** `no data`（不报错、不提示），极易误判成"无历史订单"；补上时间范围后正常返回 |

## 二、行情权限（按市场，不按标的）

| 市场 | 实时 | 盘口 | 备注 |
|---|---|---|---|
| 港股 HK | ✅ | LV2 十档 | |
| 美股 US | ✅ | LV2（NASDAQ/ARCA 双 book） | |
| 沪深 A 股 | ❌ 仅延时 | ❌ | `order_book`/`rt_data`/`rt_ticker`/`stock_quote`/`market_snapshot` 统一 `-9` |
| 日股 JP | ❌ 仅延时 | ❌ | 同上 |
| 港股期货 | ✅ | ≥1 档 | `HK.MCAmain` 正常 |
| 加密货币 | ✅ | 1 档 | `CC.BTC` 正常 |

A 股错误原文：`-9 realtime quote permission required / user has only delayed quote permission
for the requested market`。

**A 股可用替代路径（实测通过）**：`history_kline`、`capital_flow`（分钟级资金流）、
`stock_screen`（全市场 5639 只）、`market_state`。→ 日线与日内量化够用，
**盘中实时报价/盘口/逐笔不可用**。

工作台的 A 股标的卡已按此降级：`quote_stock_quote` 失败后回退到 `quote_stock_basicinfo`
与日线收盘，并在 `note` 里写明"实时快照不可用"。

### 日线长历史：回测的市场覆盖

富途 `quote_history_kline` **单次上限 370 根**，覆盖不了 2023 年起的日线（约 900 根）。
行情路由按"谁能满足这次请求"排序，而不是无条件富途优先：

| 市场 | 请求 ≤ 370 根 | 请求 > 370 根（长历史） |
|---|---|---|
| 沪深 A 股 | 富途 → 新浪 | **新浪**（AKShare）→ 富途 |
| 港股 / 美股 | 富途 → Yahoo | **Yahoo**（复权）→ 富途 |

- 港股在 Yahoo 是 **4 位**代码：`00700.HK` → `0700.HK`（写 5 位会返回空）。转换见
  `market.to_yahoo_symbol`，只有一份实现。
- 回测输出里的 `source` 是**实际使用的源**（`akshare/sina`、`yahoo/auto_adjusted`、
  `futu/quote_history_kline`），另有 `requested_source`、`data_start`/`data_end` 与
  `coverage_note`。请求区间拿不满时会明说，不要拿 `requested_source` 当口径。
- 复权口径不同源不同（新浪 `qfq`、Yahoo `auto_adjust`、富途原始价），跨源比较收益率前先对齐口径。

### A 股判定必须看显式市场标注

`000001.HK` 是港股**长和**（约 70 港元），不是 A 股平安银行（约 10 元）。
仅凭"点号前 6 位数字"判断 A 股会把 6 位港股代码当成 A 股：回测被路由到新浪、
取到 `sz000001` 的行情，**静默回测了另一个标的**（价格差一个数量级，且不报错）。

判定只有一份实现（`market.is_a_share`），其余模块（`events.py`/`fin_news.py`/
`fin_sentiment.py`/`fundamentals.py`）一律 import 它，不再各写一份。
裸 6 位数字仍按 A 股处理——它与港股无法区分，属已知约定。

**A 股专用通道必须自己判市场**，不能只靠调用方记得判。原因同上：`split(".")[0]`
会把 `000001.HK` 变成 `000001`（平安银行）。已加函数级护栏的位置：
`market.fetch_a_share`、`factors.valuation_values` 的同花顺兜底、
`fin_news.akshare_news`、`fin_sentiment.a_share_comment`、`fundamentals.from_akshare`。

**Yahoo 的符号归一也是同一类问题**：Yahoo 港股是 4 位代码，
`00700.HK` 查价格与新闻都返回空（`0700.HK` 分别返回 906 根 / 17 条）。
凡是用 Yahoo 的通道一律走 `market.to_yahoo_symbol`，有测试按"用到 Yahoo"扫描。

### 三市场通道覆盖（2026-09-14 实测）

16 个通道 × 3 市场 = 48 次实跑，全部可用。但**渠道数量因市场而异**，这是设计而非故障：

| 通道 | A 股 | 港股 | 美股 |
|---|---|---|---|
| 日线/分钟行情 | 富途（长历史→新浪） | 富途（长历史→Yahoo） | 富途（长历史→Yahoo） |
| 标的卡片 | 富途（A 股无实时权限→日线收盘降级） | 富途实时 | 富途实时 |
| 财务质量 | 富途 + 备用 | 富途 + Yahoo ROE | 富途 + Yahoo ROE |
| 事件日历 | 分红(富途+新浪)、披露、经济日历 | **仅分红(富途)**、经济日历 | **仅分红(富途)**、经济日历 |
| 财经快讯 | 富途（+东财备用） | 富途 + Yahoo | 富途 + Yahoo |
| 估值因子 | 富途 → 同花顺 | **仅富途** | **仅富途** |
| 舆情 | X + Reddit + 千股千评 | X + Reddit | X + Reddit |

港股/美股的事件与估值渠道比 A 股少：披露预约、千股千评、同花顺估值都是 A 股专属，
不可用时**如实标注渠道状态**（`fail: A股专用` / `empty`），不静默回退到别的标的。

## 三、上下文炸弹（无分页、单次返回极大）

| 工具 | 规模 |
|---|---|
| `quote_financials_earnings_price_history` | 实测约 **336KB**，且无分页参数 |
| `quote_financials_earnings_price_move` | 数十条重复结构 |
| `quote_corporate_actions_rehab` | 全量复权因子，无分页 |

不要随手调用；确有需要时先确认输出规模。

## 四、易错字段名

| 工具 | 正确 | 错误 | 后果 |
|---|---|---|---|
| `quote_combo_option_quote` | `legs` / `quantity` | `leg_list` / `qty` | 错误分两级才暴露，易写成错误请求 |
| `quote_option_screen` | 必须传 `field_filter` | 省略 | 只返回 4 个默认字段，其余全 `null`，误以为数据缺失 |
| `quote_company_executive_background` | `leader_name`（如 `马化腾`） | `display_leader_name` | 查不到 |
| `quote_order_book` | 档数由权限决定 | 假定固定档数 | HK 10 / US 60 / A股 5 / 其余 1 |

### 通道名与返回形状（2026-09-16 实测，富途实时直通交付时复核）

- `quote_option_expiration` **已停用**（tools/call 返回 "tool has been deactivated or does
  not exist"）；实名是 `quote_option_expiration_date`（`symbol` 必填，HK.00700 实测
  `ret_code=0`）。工作台直通层的 `FUTU_TOOLS` 按实测事实登记。
- `quote_order_book` 的 `data` 是**数组**（`[{books: …}]`），不是对象——按对象解包会
  丢数据（工作台 futu_data 已按"对象或数组"透传）。
- `quote_option_chain` 对 `US.AAPL` 实测 `ret=-5 backend business error`（HK.00700 正常）；
  美股个股期权链当前不可依赖，属上游业务错误，工作台按 `trading/futu-error` 如实透出。
- `quote_option_screen` 除 `field_filter` 外 `strategy` 也是必填（缺省 `ret=-3`），且
  `market_category_list` 元素是**整数**类别码（传 "US" 字符串同样 `ret=-3`）。

## 五、不确定项（勿依赖）

- `quote_ipo_list_sg`：新加坡 IPO 列表恒为空（同日 hk/us/my/cn 均有数据）；
  无法区分"确实无新股"与"接口无数据"，**判定为不确定**。
- `quote_referencefuture_list`：`HK.800000` 返回 `reference_list: []`；
  但 `quote_future_info`/`quote_order_book` 对期货代码正常。推测入参格式问题，未定论。

## 六、券商能力限制：以**实际下单**为准，不要写进规则

不要在规则或记忆里预设"某地区 / 某账户不能交易"。券商的能力限制会随账户、
地区、时间与监管变化，**唯一可靠的判据是实际发起一次委托并观察结果**。

- 历史条目里出现过的拒单原文（例如某次 FAILED 订单的 `last_err_msg`）
  只是**当时那一次**的观察，不是长期规则；
- 要确认实盘能否下单，就在用户完成两次确认后**真下一个单**，以返回结果为准；
- 被拒时把原始错误原文连同**日期与账户**记进记忆，供后续判断是否仍然成立；
- 记忆条目必须标注账户模式（sim / live），**两个账户的持仓与盈亏不可混算**
  ——同一个标的在两个账户里的成本与数量可以完全不同。

## 七、按设计拦截的调用（不是故障）

**WP7 收窄（2026-09-16）**：富途**写类**工具——下单/改单/撤单动词
（`*_input_order` / `*_place_order` / `*_order_place` / `*_modify_order` / `*_cancel_order`，
覆盖 `sim_trade_*` 与 `trading_*` 两族，即本体检口径下的 4 个实盘下单工具及对应 sim 工具）
在 Harness 内被 `policy.js` guard **一律拒绝，不分 sim/live**，消息为
「富途写通道已收窄至工作台：请通过工作台交易（quantwb 的 trade_* 工具，或计划执行）；
模式外的账户查询请用工作台工具」。这是**预期行为**：写路径唯一在工作台服务
（`trade_place`/`trade_modify`/`trade_cancel`，业务确认由独立 Web 确认卡片作答）。

**读类不受影响，但保留模式互斥**：9 个实盘账户查询工具（`account_*`）与 sim 查询
（`sim_trade_position_list` / `sim_trade_cash_info` / `sim_trade_history_order_list` /
`sim_trade_max_buy_sell` / `sim_trade_account_list`）仍按模式桶校验——sim 模式拒实盘查询、
live 模式拒 sim 查询，错误信息含「账户模式」；模式外的账户查询请用工作台（quantwb
`account_positions` 等）。

**业务确认的归属（历史教训保留）**：实盘写操作的业务确认（`store.requestConfirmation`，
阻塞等作答、120 秒超时按拒绝）曾是 Node 侧 pre-execute 的职责；WP7 起确认移至**工作台
服务侧**（`store_access.request_confirmation` + Web 确认卡片，服务于 `trade_*` 工具），
Node 侧 store 三方法与 `confirmation`/`confirm-decide` 端点**保留**（legacy 面板过渡期 +
服务侧 Python 移植同语义）。它与 Harness 权限审批的区别这一教训仍然成立：
- 权限审批回答"这个动作准不准做"，由会话的 approval policy 裁决；
  full-access（`policy="never"`）下 `approval.decide()` **直接返回 rejected，连问都不问**，
  表现为 `the user rejected tool ...`——看起来像用户拒绝了，实际没人被问过。
- 业务确认回答"这笔参数对不对"，**与权限档位无关**，任何档位下都必须确认。
因此 policy 对写类的拒绝是 deny（guard 即拒）而非 `{kind:"ask"}`，不会落到审批档位上。

## 八、交易字段与通道边界（WP8 任务 6，2026-09-16）

**官方 place-order 字段在工具面全部可达**（`trade_place`；官方依据：
`/zh-cn/api/trading/trade/place-order.md` 与 naming-dictionary，2026-09-16 实抓原文）：
`order_type` 8 枚举（默认 LIMIT）、`time_in_force{DAY,GTC}`（默认 DAY）、`session` 4 枚举、
`aux_price`、`lot_type`、`remark`、`order_class` + `multi_leg_info`。闸门在**风控/确认之前**
做字段校验（条件必填/枚举/互斥/结构），错误一律 `trading/invalid-operation` 且消息给出官方
允许值。以下边界是**按设计**，不是故障：

- **触发价必填**：`order_type ∈ {STOP, STOP_LIMIT, MARKET_IF_TOUCHED, LIMIT_IF_TOUCHED}`
  必须带 `aux_price`（证券 3 位小数，超出即拒，**不四舍五入**）；非触发类带 `aux_price` 拒。
- **价格互斥**：限价类 `{LIMIT, AUCTION_LIMIT, STOP_LIMIT, LIMIT_IF_TOUCHED}` 必带 `price`；
  市价类 `{MARKET, AUCTION, STOP, MARKET_IF_TOUCHED}` 带 `price` 拒。
- **市价类的风险基准价**：`MARKET`/`AUCTION` 请求体没有价格，闸门用**本地日线最近收盘**
  （`daemon._execute_plan` 的 `price_of` 同款口径）作为风控规则 4/5 的 `qty × price` 基准；
  本地没有该标的日线时**如实拒绝**（`trading/invalid-operation`）——绝不用 0 让风控静默
  失效，也绝不编造价格。确认卡片会把实际使用的「风控基准价」列出。
- **时段/手数**：`session` 仅美股（非美股带 session 拒），且**市价单仅支持 RTH**；
  `lot_type` 仅港股（非港股带 lot_type 拒）。
- **多腿**：`order_class=MLEG` 必须带 `multi_leg_info`，反之 `multi_leg_info` 必须与 MLEG
  同时给出；`multi_leg_info` 的键集/必填/枚举照 naming-dictionary（`option_strategy`/
  `underlying_symbol`/`leg_infos[leg_symbol/leg_exchange/leg_ratio_qty/leg_side/
  leg_security_type]`），坏结构零网络往返即拒。
- **备注**：`remark` UTF-8 编码后 ≤64 字节（中文按 3 字节计）。
- **改单**：官方 `PUT /orders/{id}` 只有 `exchange/qty/price/aux_price`（**没有 order_type**）；
  被改订单是触发类时官方要求带 `aux_price`，闸门透传。
- **sim 通道只支持限价当日单**：`sim_trade_place_order` 的 WP3 锁定口径是
  `order_type=1`（限价）+`price`，因此 sim 下 `order_type≠LIMIT`、`time_in_force≠DAY`、
  `order_class≠NORMAL` 或带 `session/aux_price/lot_type/remark/multi_leg_info` 一律
  `trading/broker-unavailable`（消息「sim 仅支持限价当日单」）——**如实拒绝，不静默丢弃**；
  适配层（`FutuBroker`）还有第二道同样守卫，防绕道直调。
- **推送订阅管理面**（`push_status` / `push_subscribe` / `push_unsubscribe`，非交易）：
  `push_status` 与 `/healthz` 的 `push` 同一实现/同一事实；订阅两端点只改本地连接订阅
  意图（不改模式、不过风控、不产生订单），推送未启用时返回 `trading/push-unavailable`
  （不假装成功），载荷非法返回 `trading/invalid-operation`。幂等：重复提交同一意图不重发
  订阅帧，反订阅从未订阅的标的无副作用。

## 九、live 账户读取口径（WP9 任务 4，2026-09-16 官方文档核对）

live 计划生成（`planner.plan_auto`）与任何需要「券商真实持仓 + 权益」的读路径共用
`trading_core.broker.positions_and_equity`，口径如下（**禁止猜字段**）：

| 环节 | 工具/端点 | 官方字段（核实来源） |
|---|---|---|
| 挑账户 | `account_authorized_trd_accs` / `GET /api/v1.0/accounts/authorized_trd_accs` | `accounts[].account_id`；`accounts[].enable_market: list[int]`（naming-dictionary#enable-market：**1=HK 2=US 4=ChinaStock** 5=Futures 6=SG 12=CA 15=JP 18=KR） |
| 持仓 | `account_positions` / `GET /accounts/{acc_id}/positions` | 行字段 `code`/`qty`（实测口径见 `plugins/workbench/python/positions.py` live 分支）；**该接口不接受 market 参数**，返回账户全部持仓 → 按 `code` 前缀过滤到目标市场链 |
| 权益 | `account_funds` / `GET /api/v1.0/accounts/{acc_id}/funds` | **`total_assets`=总净资产**（官方 get-funds.md 响应表；同页 `securities_assets`/`cash`/`market_val` 是分类口径，**不作权益用**）。官方参数表把 `currency` 标为必填，但同页 curl 示例未传——沿用仓库既有调用（不带 currency），只用 `total_assets` |

**两条硬纪律**（`EquityUnavailable` 与通道故障分列，告警文案不互相冒名）：

1. **权益缺失或非正 → 抛 `EquityUnavailable`**，调用方跳过当日计划并告警「权益不可用」。
   绝不把缺失当 0：权益为 0 会让目标数量全变 0，等于凭空生成清仓单；
2. **`enable_market` 与 sim 的 `market_id` 是两套数字口径**（sim：HK 1/A 股 3/US 100；
   live enable_market：HK 1/US 2/A 股 4），**不互相换算**，各自有镜像常量与锁定测试。

**权益分母口径（已知近似，非保守保证）**：官方资金接口只给账户整体的 `total_assets`，
没有按市场切分的权益。多市场同时生成计划（SH/HK/US 各一份）时，每份计划的分母都是同一笔
账户整体权益——各市场权重之和若都接近 1，**跨市场总敞口可能超过账户权益**。当前依赖风控
8 规则逐单拦截 + live 人工确认把守，不得据此认为跨市场敞口已被自动约束；精确口径需引入
按市场权益拆分或组合级联合约束（后续工作）。

### sim 台账学习路径：对账先同步券商事实，再判差异（WP9 实现期修订，2026-09-16）

sim 通道的成交**不经** WS 交易事件通道（推送只覆盖 live OpenAPI 路径），因此本地台账在
sim 常态下既没有成交记录、订单状态也停在 `submitted`。若对账只做纯比对，会把「本地尚未
学习」系统性误报为差异 → 每日 critical + halt → **自动执行被自己的对账噪声永久熔断**
（实现期实测，非推测）。因此 `reconcile.daily` 在比对之前先同步券商事实，两步都在比对前：

| 步骤 | 消除的假差异 | 口径与边界 |
|---|---|---|
| ① 成交回填 `_backfill_fills` | 持仓级 | 按券商订单历史的累计成交量与**成交均价**补记差额（`bf-` 指纹幂等）；**均价缺失不回填、不编造**（缺口以 `qty_diff` 如实暴露）；累计量回落不删既有 fills |
| ② 状态推进 `_advance_order_states` | 订单级 | 按累计成交量推进（足额→`filled`、部分→`partial`）；**数量口径优先于状态文本**——券商 `status` 是未公开整数码，只附原文不解释 |

- **只前进不回退**：无出边的终态（`filled`/`cancelled`/`rejected`）直接跳过——券商修正使
  累计量回落时不会把 `filled` 拉回 `partial`；
- **非法迁移不绕开状态机**：`submitting → filled` 不在 `oms.TRANSITIONS` 白名单（须先经
  `submitted`）→ 捕获跳过并计入 `orders_advanced.skipped`，该订单随后仍被数量矛盾判据
  暴露为差异（保守方向：宁可停下来给人看，也不替状态机猜路径）；
- **同步后仍存在的差异才是真差异** → critical + halt（只暂停后续执行，绝不自动平仓）；
- 两步都**只读券商、只写本地**：零写类券商调用、永不重放（对账铁律不变）。
