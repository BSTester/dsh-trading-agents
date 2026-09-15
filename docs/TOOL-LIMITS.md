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

sim 模式下 9 个实盘账户查询工具与 4 个实盘下单工具会被 `policy.js` 拒绝；
错误信息里含「账户模式」，这是**预期行为**，要操作实盘需先在工作台显式切换模式。

实盘写操作另有一道**业务确认**（`store.requestConfirmation`）：工具会阻塞，
等用户在工作台点确认/拒绝，最长 120 秒，超时按拒绝处理，返回的是一句
「实盘操作未获确认（…）。这不是权限问题…」。

**注意它与 Harness 权限审批的区别**（曾把两者混为一谈，导致 full-access 档位下
实盘永远下不出去）：
- 权限审批回答"这个动作准不准做"，由会话的 approval policy 裁决；
  full-access（`policy="never"`）下 `approval.decide()` **直接返回 rejected，连问都不问**，
  表现为 `the user rejected tool ...`——看起来像用户拒绝了，实际没人被问过。
- 业务确认回答"这笔参数对不对"，由交易插件自己发起、工作台界面作答，
  **与权限档位无关**，任何档位下都必须确认。

因此 `policy.js` 对实盘写操作**不再返回 `{kind:"ask"}`**，也就不会落到审批档位上。
