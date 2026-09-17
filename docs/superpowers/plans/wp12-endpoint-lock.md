# WP12 富途数据面端点依赖锁定表

> **用途**：WP12/WP13 的唯一输入。传输层方法（WP12 任务 2/3）、服务端点面与工具面
> （任务 4）、PIT 落库（任务 5）、通道迁移（WP13 任务 1/2）**只允许引用本表内的路径与
> 参数名**；表外内容一律视为未核对，禁止落地。
>
> **核对方法**：`llms.txt` 的文档链接已实测漂移（见 §D.2），本轮改为从官方文档站
> **侧边栏提取真实路径**后逐页取 `.md` 正文（48 份），用脚本抽取「方法+路径 / 请求参数 /
> 响应字段 / 错误码」四要素。核对日期 **2026-09-16**——**下表所有行的核对日期均为该日**
> （表头统一声明，逐行不再重复该列）。
> **基址**：`https://webapi.futunn.com`；文档基址 `https://open.futunn.com`（下表「文档」列
> 为文档基址下的相对路径）。
>
> **状态图例**：✅ 已核对（路径/参数/响应/错误码四要素齐备）｜⚠️ 已核对但带注意事项
> （见备注与 §C）｜🚫 范围外不做｜⛔ BLOCKED（文档缺失或不可核对）

## A. 全局约定（官方原文口径）

| 项 | 约定 |
|---|---|
| 认证 | OAuth Bearer 优先；AppKey 签名可用（仓库 WP8 已实现双模式） |
| 时间 | 多为 Unix 毫秒时间戳，**部分字段用秒**（如 `pub_trading_day`）；日期字段 `YYYY-MM-DD` 按标的所在市场时区 |
| 比率 | 百分比数值（`1.23` = 1.23%） |
| 分页 | 列表接口统一 `next_key` / `limit`；游标不透明，`"-1"` 表示无更多页 |
| 无数据语义 | 合法但无数据 → `ret_code=0` + 空数组；**部分端点另有 `-10 no_data`**（下表逐行标注） |
| 限频 | 官方建议批量请求、缓存静态数据、错开调用、429 指数退避 |

## B. 已覆盖（WP8 已实现，仅登记定位）

| 族 | 端点 | 定位 |
|---|---|---|
| 交易 13 项 | place-order / modify-order / cancel-order / order-confirm / get-max-qty / get-funds / get-accounts / get-positions / get-open-orders / get-history-orders / get-order-details / get-today-deals / get-history-deals | `trading_datasource/futu_openapi.py` 的 `OpenApiTrade`（WP8 任务 3） |
| 实时行情 6 项 | stock-quote / order-book / cur-kline / rt-data / rt-ticker / market-snapshot | `OpenApiQuote`（WP8 任务 2） |
| 基础数据 5 项 | stock-basicinfo / trading-days / history-kline / market-state / **find-news + find-community（即官方「搜索」页）** | `OpenApiQuote` + 服务端点；「搜索」见 §C.1 |
| 资金流 3 项 | capital-flow / capital-flow-history / capital-distribution | 同上 |
| 衍生品 2 项 / 筛选 1 项 | option-chain / option-expiration / option-screen | 同上 |
| 推送 | 行情 WS + 交易事件 WS | `server/futu_push.py`（WP8 任务 4） |

## C. 本轮核对（新接入目标）

### C.1 基础数据（4）

| 端点 | 服务/工具名 | 方法 + 路径 | 关键参数 | 响应关键字段 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|---|---|
| 经济日历热门 | `economic_calendar_hot` | GET `/api/v1.0/quote/economic-calendar/hot` | `limit`(1~20)、`next_key`、`date`、`timezone` 全选填 | `event_text, previous, predictive, announce, star, event_time, country, currency, unit, unique_id, detail_url` | `0` 成功（含空列表）／`-3` limit 越界／`-4` 后端业务错误 | ✅ | `/zh-cn/api/quote/basic-data/economic-calendar-hot` |
| 经济日历搜索 | `economic_calendar_search` | GET `/api/v1.0/quote/economic-calendar/search` | **`keyword` 必填**、**`search_type` 必填**(1~4)、`limit`、`next_key`、`time_order_type` | 同 economic-calendar-hot | `-3` 缺 keyword/search_type 或枚举越界／`-4` 后端错误 | ✅ | `/zh-cn/api/quote/basic-data/economic-calendar-search` |
| 所属板块 | `info_owner_plate` | GET `/api/v1.0/quote/{symbol}/owner-plate` | `symbol` 路径必填（形如 `MARKET.CODE`，≤32 字符且含 `.`） | `name, sc_name, tc_name, plate_code, plate_name, plate_sc_name, plate_tc_name, plate_type` | `-3` symbol 缺失/格式非法／`-7` 解析不到证券／`-4` 网关映射失败 | ✅ | `/zh-cn/api/quote/basic-data/owner-plate` |
| 复权因子 | `f10_detail(section="rehab")` 或 sync 内部 | GET `/api/v1.0/quote/{symbol}/corporate-actions/rehab` | `symbol` 路径必填、`divi_mode` 选填 | `rehabs[]{ex_div_date, action_types, desc_sc/tc/en, forward_adj_factorA/B, backward_adj_factorA/B, cum_*}` | `0` 成功（含空数组）／`-3` divi_mode 枚举非法／`-7` symbol 无法解析 | ✅ | `/zh-cn/api/quote/basic-data/rehab` |

> **路径注意**：复权因子的文档归属「基本数据」，但 **REST 路径在 `/corporate-actions/rehab`**——
> 迁移 sync 时按本表路径，不按文档目录。

### C.2 板块（2）

| 端点 | 服务/工具名 | 方法 + 路径 | 关键参数 | 响应关键字段 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|---|---|
| 板块列表 | `plate_list` | GET `/api/v1.0/quote/plate-list` | **`market` 必填**、**`plate_class` 必填**∈{ALL,INDUSTRY,REGION,CONCEPT,OTHER} | `data.plate_list[]{code, plate_id, plate_name, sc_name, tc_name}` | `-3` 缺参/枚举非法（大小写敏感）／`-8` REGION 但市场非 SH/SZ／`-5` 后端失败 | ✅ | `/zh-cn/api/quote/plate/plate-list` |
| 板块成分股 | `plate_stock` | GET `/api/v1.0/quote/plate-stock` | **`plate_code` 必填**、`sort_field`、`ascend`、`price_type`、`leverage_direction`、`leverage_multiple`、`next_key`、`limit` | `stock_list[]{code, stock_id, stock_name, sc_name, tc_name, stock_type, lot_size, list_time}` + `pagination{total,has_more,next_key}` | `0` 成功（空列表 + total=0）／`-3` 缺参/正则不符/枚举非法／`-7` 板块不存在或下线／`-4` 网关构造失败 | ✅ | `/zh-cn/api/quote/plate/plate-stock` |

### C.3 全市场筛选（2）

| 端点 | 服务/工具名 | 方法 + 路径 | 关键参数 | 响应关键字段 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|---|---|
| 股票筛选 | `stock_screen` | POST `/api/v1.0/quote/stock-screen` | **`screen_queries` 必填**（11 选 1 查询类型：simple_field/plate/simple_property/cumulative_property/financial_property/indicator_positional/indicator_pattern/featured_property/broker_holdings/kline_shape/option）、`retrieve_queries`（9 选 1）、`sort`/`sorts`（direction 1~4）、`next_key`、`limit`(默认 200 最大 300)、`watchlist_stock_ids`、`holding_stock_ids`、`user_stock_list_mode`(0/1/2) | `items[]{code, name, sc_name, tc_name, results[]}`（results 与 retrieve_queries 等长，`res.ival/dval/sval` + `result_type` 1=double/2=int/3=string）+ `pagination{total,has_more,next_key}` | `-3` 缺 screen_queries / limit>300 / next_key 非法 / user_stock_list_mode 越界／`-5` property name 或 market 非法／`-6` BFF 出参映射失败 | ✅ | `/zh-cn/api/quote/screening/stock-screen` |
| 窝轮筛选 | HTTP-only（工具面外） | POST `/api/v1.0/quote/warrant-screen` | `market_type`∈[1,4,15]、`is_delay`、`only_count`、`stock_owner`、`screen_groups`、`sorts`、`next_key`、`limit`(≤1000) 全选填 | `code, name, sc_name, tc_name, stock_owner, type, issuer, status, maturity_time, list_time, last_trade_time …` | `-3` market_type/limit/next_key 非法／`-7` stock_owner 无法解析／`-2`/`-4`/`-6` 网关内部错误 | ✅ | `/zh-cn/api/quote/screening/warrant-screen` |

> **筛选限制**：`broker_holdings_query` / `kline_shape_query` **仅 HK**；`option_query` 需标的
> 有期权。区间写法 `lower={value,includes}` / `upper={value,includes}`，value 已按字段倍率预放大。

### C.4 IPO（1）

| 端点 | 服务/工具名 | 方法 + 路径 | 关键参数 | 响应关键字段 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|---|---|
| 新股列表 | `ipo_list` | GET `/api/v1.0/quote/ipo-list/{market}`，`market`∈{hk,us,cn,sg,my}（**每市场独立端点**） | `request_type` 选填：HK/US/SG/MY 默认 11（9=可认购/10=待上市/11=即将上市）；**A 股默认 4**（1=预告/2=可申购/3=公布中签/4=待上市/5=全部） | `code, name, sc_name, tc_name, list_time, list_timestamp, ipo_price_min/max, list_price, lot_size, entrance_price, apply_start/end_timestamp …` | `-3` request_type 非整数/非法／`-5` 后端失败或超时 | ✅ | `/zh-cn/api/quote/ipo/ipo-list` |

### C.5 个股深度数据（26 项，`f10_detail(symbol, section)` 聚合）

> 官方已把原 llms.txt 的扁平 `f10/*` **重组为 7 个分类命名空间**；下表 section 名即
> `f10_detail` 的枚举取值。**其中 2 项不在 llms.txt 清单内**（见 §D.2）。

**财务数据（4）**

| section | 方法 + 路径 | 关键参数 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|
| `earnings_price_move` | GET `/api/v1.0/quote/{symbol}/financials/earnings-price-move` | `count`(默认 10 最大 50)、`overview_count`(默认 8 最大 50，生效=min(overview_count,count)) | `-3` count 越界／`-7` invalid_symbol／`-8` 非正股／`-4`/`-6` 内部错误。**响应**：`records[]{fiscal_year, financial_type, period_text, pub_trading_day(_str), pub_type, day_offset, trading_day(_str), open/close/highest/lowest/last_close_price, option_iv, option_hv, volume, volume_precision}` + `overview_avg_earnings_day_change_pct` | ✅ | `/zh-cn/api/quote/financials/earnings-price-move` |
| `earnings_price_history` | GET `/api/v1.0/quote/{symbol}/financials/earnings-price-history` | `symbol` | `-3` 格式非法／`-7` 查不到证券／`-8` 市场不在 HK/US/SH/SZ／`-10` 无财报日股价／`-6` 内部错误。**响应**：`fiscal_year, financial_type, period_text, is_current, pub_trading_day(_str), pub_time(_str), pub_type, predict_vola_ratio_newest/highest, option_iv_crush …` | ✅ | `/zh-cn/api/quote/financials/earnings-price-history` |
| `statements` | GET `/api/v1.0/quote/{symbol}/financials/statements` | `statement_type`(1~4)、`financial_type`、`currency_code`、`next_key`、`limit` | `-3` statement_type 不在 [1,2,3,4] 等／`-7` 无法解析／`-10` 该报表期无数据／`-4`/`-6` 内部错误。**响应**：`date_time, fiscal_year, financial_type, structure, structure_name, period_text, currency_code, accounting_standards, auditor_report, item_list[]{field_id, display_name, value_type, data}` | ✅ | `/zh-cn/api/quote/financials/statements` |
| `revenue_breakdown` | GET `/api/v1.0/quote/{symbol}/financials/revenue-breakdown` | `date`、`financial_type`、`currency_code` | `-3` date 越界/枚举非法／`-7` 无法解析／`-10` 无营收构成／`-5` 网关或 RPC 异常。**响应**：`period, currency_code, breakdown_list[]{type, item_list[]{name, main_oper_income, ratio}}, screen_date_list[]{date, period_text, financial_type}` | ✅ | `/zh-cn/api/quote/financials/revenue-breakdown` |

**研究（3）**

| section | 方法 + 路径 | 关键参数 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|
| `analyst_consensus` | GET `/api/v1.0/quote/{symbol}/research/analyst-consensus` | `symbol` | 响应含综合评级/覆盖分析师数/各档占比/目标价区间（WP12 任务 5 落库关注：评级与目标价） | ✅ | `/zh-cn/api/quote/research/analyst-consensus` |
| `rating_summary` | GET `/api/v1.0/quote/{symbol}/research/rating-summary` | `rating_dimension_type`(1 或 2)、`next_key`、`limit`(≤20) | `-3` 枚举非法/limit>20／`-7` 查不到／`-2`/`-4`/`-6` 内部错误。**响应**：`inst_rating_summary_list[]{institution_info, rating_item_list[]}, analyst_rating_summary_list[]{analyst_info, rating_item_list[]}` + pagination | ✅ | `/zh-cn/api/quote/research/rating-summary` |
| `morningstar` | GET `/api/v1.0/quote/{symbol}/research/morningstar` | `symbol` | `-3` 格式非法／`-7` 查不到／`-10` 晨星无覆盖／`-2`/`-4`/`-6` 内部错误。**响应**：`rating_type, star_rating, star_update_time(_str), fair_value, economic_moat_label/type, uncertainty_label/type, capital_allocation_label/type, financial_health_label/type` | ✅ | `/zh-cn/api/quote/research/morningstar` |

**估值（4）**

| section | 方法 + 路径 | 关键参数 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|
| `valuation_detail` | GET `/api/v1.0/quote/{symbol}/valuation/detail` | `valuation_type`∈[1,2,3]、`interval_type`∈[1..10] | `-3` 枚举非法／`-7` 查不到／`-2`/`-4`/`-6` 内部错误。**响应**：`valuation_type, last_update_time(_str), trend, market_distribution, plate_distribution, profit_growth_rate` | ✅ | `/zh-cn/api/quote/valuation/detail` |
| `valuation_plate_stocks` | GET `/api/v1.0/quote/valuation/plate-stocks` | **`symbol`（查询，板块代码）必填**、`valuation_type`、`next_key`、`limit`(1~50)、`sort_type`、`sort_id` | `-3` 缺参/枚举非法/limit 越界／`-7` 板块不存在／`-10` 无数据／`-2`/`-5` 内部错误。**响应**：`stock_list[]{symbol, name, sc_name, tc_name, valuation_val, forward_value, valuation_percentile, market_cap}` + pagination | ✅ | `/zh-cn/api/quote/valuation/plate-stocks` |
| `valuation_index_stocks` ⭐新 | GET `/api/v1.0/quote/valuation/index-stocks` | **`symbol`（查询，指数代码）必填**、`valuation_type`、`next_key`、`limit`、`sort_type`、`sort_id`、`filter_security` | `-3` 参数非法／`-7` 指数不存在／`-10` 无数据。**响应**：同 valuation_plate_stocks | ✅ | `/zh-cn/api/quote/valuation/index-stocks` |
| `valuation_index_stock_plates` ⭐新 | GET `/api/v1.0/quote/valuation/index-stock-plates` | **`symbol`（查询，指数代码）必填** | **响应**：`plate_list[]{symbol, name, sc_name, tc_name}`（无错误码表，按通用码处理） | ✅ | `/zh-cn/api/quote/valuation/index-stock-plates` |

**公司行为（3）**

| section | 方法 + 路径 | 关键参数 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|
| `dividends` | GET `/api/v1.0/quote/{symbol}/corporate-actions/dividends` | `symbol` | `0` 成功（含空列表）／`-3` 格式非法／`-7` 无法解析／`-2`/`-4`/`-6` 内部错误。**响应**：`total_dividend_count, total_dividend_money, fiscal_year, statement, dividend_per_share, currency, payout_ratio, dividend_type, process, ex_date, record_date, dividend_payable_date, pub_date` | ✅ | `/zh-cn/api/quote/corporate-actions/dividends` |
| `buybacks` | GET `/api/v1.0/quote/{symbol}/corporate-actions/buybacks` | `next_key`、`limit`(≤50) | `-3` limit>50／`-7` 不存在／`-5`/`-6` 内部错误。**响应**：`publ_date, end_date, buy_back_money, currency, buy_back_sum, percentage, high_price, cumulative_sum, cumulative_percentage, share_type, advance_date, start_date, event_proce_desc, buy_back_mode …` | ✅ | `/zh-cn/api/quote/corporate-actions/buybacks` |
| `splits` | GET `/api/v1.0/quote/{symbol}/corporate-actions/splits` | `symbol` | `0` 成功（含空列表）／`-3` 格式非法／`-7` 无法解析／`-2`/`-4`/`-6` 内部错误。**响应**：`split_list[]{dir_deci_pub_date(_str), reform_type, rate, ex_date(_str), sm_deci_date(_str), scheme_statement, new_par_value, temp_share_code, temp_share_abbr_name, new_trade_unit, shares_after_effect}` | ✅ | `/zh-cn/api/quote/corporate-actions/splits` |

**股东持股（6）**

| section | 方法 + 路径 | 关键参数 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|
| `shareholders_overview` | GET `/api/v1.0/quote/{symbol}/shareholders/overview` | `period_id` | `-3` period_id 非法／`-7` 无法解析／`-2`/`-4`/`-6` 内部错误。**响应**：`name, holder_pct, holder_id, static_date(_str), period_id, period_text` | ✅ | `/zh-cn/api/quote/shareholders/overview` |
| `holding_changes` | GET `/api/v1.0/quote/{symbol}/shareholders/holding-changes` | `limit`、`next_key`、`sort_column`、`sort_type`、`filter_type`、`holder_category` | `-3` 参数越界／`-7` 查不到／`-10` 无持股变动。**响应**：`name, holder_id, holder_type(_id), period_text, holding_date(_str), share_change_num, share_num, share_ratio, share_ratio_change, shares_change_price` | ✅ | `/zh-cn/api/quote/shareholders/holding-changes` |
| `holder_detail` | GET `/api/v1.0/quote/{symbol}/shareholders/holder-detail` | `request_type`、`period_id`、`holder_id`、`sort_column`、`sort_type`、`limit`、`next_key` | `-3` 类型/越界/枚举非法／`-7` 无法解析／`-10` 无股东明细／`-2`/`-4`/`-6` 内部错误。**响应**：`period_text, holder_id, name, holder_quantity(_change), holder_pct(_change), holding_date(_str), close_price, price_change_pct, source_group_name, update_time(_str)` | ✅ | `/zh-cn/api/quote/shareholders/holder-detail` |
| `institutional` | GET `/api/v1.0/quote/{symbol}/shareholders/institutional` | `limit`、`next_key` | `-3` 格式/越界／`-7` 无法解析／`-10` 无机构持股。**响应**：`period_text, institution_quantity(_change), holder_quantity(_change), holder_pct(_change), close_price, open_price, last_close_price, update_time(_str)` | ✅ | `/zh-cn/api/quote/shareholders/institutional` |
| `insider_holders` | GET `/api/v1.0/quote/{symbol}/shareholders/insider-holders` | `next_key`、`limit`(≤30) | `-3` limit>30／`-7` 解析不到。**响应**：`holder_id, name, title, holder_quantity, holder_pct, insider_total_count, insider_bought_count, insider_sold_count` + pagination | ✅ | `/zh-cn/api/quote/shareholders/insider-holders` |
| `insider_trades` | GET `/api/v1.0/quote/{symbol}/shareholders/insider-trades` | `next_key`、`limit`(≤50)、`holder_id` | `-3` limit>50／`-7` 查不到／`-2`/`-5`/`-6` 内部错误。**响应**：`trades[]{holder_id, name, title, trade_shares, min_trade_date(_str), min_price, security_holder_quantity, transaction_type, source_group_name, is_proposed_sale_of_securities}` + pagination | ✅ | `/zh-cn/api/quote/shareholders/insider-trades` |

**公司信息（4）**

| section | 方法 + 路径 | 关键参数 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|
| `company_profile` | GET `/api/v1.0/quote/{symbol}/company/profile` | `symbol` | `-3` 格式非法／`-7` 无法解析／`-8` 品类不支持／`-10` 无公司资料。**响应**：`name, value, field_type, attribute_type` | ✅ | `/zh-cn/api/quote/company/profile` |
| `company_executives` | GET `/api/v1.0/quote/{symbol}/company/executives` | `symbol` | `-3` 格式非法／`-7` 不存在／`-10` 无高管数据。**响应**：`leader_name, display_leader_name, position_name, leader_gender, leader_age, highest_education, begin_date(_str), issue_date(_str), shares, annual_salary, annual_salary_currency` | ✅ | `/zh-cn/api/quote/company/executives` |
| `company_executive_background` | GET `/api/v1.0/quote/{symbol}/company/executive-background` | **`leader_name` 必填** | `-3` 缺 leader_name／`-7` 查不到／`-10` leader_name 不匹配或无背景／`-4`/`-6` 内部错误。**响应**：`brief_background` | ✅ | `/zh-cn/api/quote/company/executive-background` |
| `company_operational_efficiency` | GET `/api/v1.0/quote/{symbol}/company/operational-efficiency` | `limit`(≤100)、`financial_type`∈{7,102}、`currency_code`、`next_key` | `-3` limit/枚举非法／`-7` 无法解析／`-8` 品类不支持／`-10` 无数据。**响应**：`fiscal_year, financial_type, period_text, end_date(_str), employee_num(_yoy), income_per_capita(_yoy), profit_per_capita(_yoy), net_profit_per_capita(_yoy), currency_code` | ✅ | `/zh-cn/api/quote/company/operational-efficiency` |

**十大经纪商（2）**

| section | 方法 + 路径 | 关键参数 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|
| `top_brokers` | GET `/api/v1.0/quote/{symbol}/top-brokers` | `symbol`（**仅港股**）、`date`(YYYY-MM-DD，缺省最新) | `-3` 格式非法／`-7` 查不到／`-8` 非港股／`-2`/`-4`/`-6` 内部错误。**响应**：`is_real_time, data_time(_str), sec_volume, sec_turnover, buy_brokers[]{net_vol, broker_id, broker_name, broker_code, hold_ratio, avg_price}, sell_brokers[]` | ✅ | `/zh-cn/api/quote/top-brokers/top-brokers` |
| `top_brokers_history` ⭐新 | GET `/api/v1.0/quote/{symbol}/top-brokers-history` | `symbol`（仅港股）、**`days_before` 必填**(1~365) | 同上（文档与实时端点共用错误码表） | ✅ | `/zh-cn/api/quote/top-brokers/top-brokers-history` |

### C.6 卖空数据（2）

| 端点 | 服务/工具名 | 方法 + 路径 | 关键参数 | 响应关键字段 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|---|---|
| 每日卖空成交 | `short_daily_volume` | GET `/api/v1.0/quote/{symbol}/short/daily-volume` | `count`(默认 30 最大 90) | HK（**成交维度**）：`timestamp(_str), shares_traded, turnover, short_sell_shares_traded, short_sell_turnover, open/close/last_close_price, daily_trade_avg_ratio` + data 级 `aggregated_short, aggregated_short_ratio`；US（**持仓维度**）：`total_shares_short, nasdaq_shares_short, nyse_shares_short, short_percent, volume …` | `-3` count 越界/格式非法／`-7` invalid_symbol／`-8` 市场或品类不支持（仅 HK/US 可卖空证券）／`-10` no_data（**视为无卖空数据而非错误**） | ✅ | `/zh-cn/api/quote/short/daily-volume` |
| 空头持仓 | `short_interest` | GET `/api/v1.0/quote/{symbol}/short/interest` | `count` | `timestamp(_str), aggregated_short, aggregated_short_ratio, close_price, last_close_price, avg_cost, avg_daily_short_volume, shares_short, short_percent, avg_daily_share_volume, days_to_cover` | `-3` count 越界/格式非法／`-7` 标的不存在／`-8` 市场或品类不支持／`-10` 无空头持仓数据／`-5` 内部错误 | ✅ | `/zh-cn/api/quote/short/interest` |

### C.7 衍生品（4，`derivative_detail(symbol, section)` 聚合）

| section | 方法 + 路径 | 关键参数 | 响应/错误码 | 状态 | 文档 |
|---|---|---|---|---|---|
| `future_info` | POST `/api/v1.0/quote/future-info` | **`code_list` 请求体必填**（非空、≤400 项） | `0` 成功（**所有 code 被静默丢弃时也返回空数组**）／`-3` 缺失/为空/超 400。**响应**：`code, name, owner, exchange, type, size, size_unit, price_currency, price_unit, min_change, min_change_unit, trade_time, time_zone, last_trade_time …` | ✅ | `/zh-cn/api/quote/derivatives/future-info` |
| `reference_future` | GET `/api/v1.0/quote/{symbol}/reference-future` | `symbol` | `0` 成功（非期货或无关联时 `reference_list=[]`）／`-3` 缺失/超长(>32)/格式非法／`-7` 不存在。**响应**：`code, stock_name, sc_name, tc_name, stock_type, lot_size, future_valid, future_main_contract, future_last_trade_time, list_time` | ✅ | `/zh-cn/api/quote/derivatives/reference-future` |
| `option_volatility` | GET `/api/v1.0/quote/{symbol}/option-volatility` | `symbol`（**期权合约**）、`query_time_period`(1~5)、`hv_time_period`(5~250) | `-3` 传正股/枚举越界／`-7` 无法识别合约／`-10` 无可用波动率／`-4` 内部错误。**响应**：`timestamp, implied_volatility, history_volatility, volatility_premium, average_impvol, impvol_status, analysis` | ✅ | `/zh-cn/api/quote/derivatives/option-volatility` |
| `option_exercise_probability` | GET `/api/v1.0/quote/{symbol}/option-exercise-probability` | `symbol`（**期权合约**）、`limit`(1~1000) | `-3` 传正股/limit 越界/格式非法／`-10` 无行权概率数据／**`-9` 用户无期权数据查询权限**／`-4` 内部错误。**响应**：`timestamp, security_price, strike_probability` | ✅ | `/zh-cn/api/quote/derivatives/option-exercise-probability` |

### C.8 自选（3）

| 端点 | 服务/工具名 | 方法 + 路径 | 关键参数 | 响应关键字段 | 错误码 | 状态 | 文档 |
|---|---|---|---|---|---|---|---|
| 获取自选股 | `watchlist_list` | GET `/api/v1.0/quote/user-security` | **`group_name` 必填**（≤100） | `code, name, sc_name, tc_name, lot_size, stock_type, stock_child_type, stock_owner, option_type, strike_time, strike_price, listing_date, stock_id, main_contract …` | `0` 成功（空分组空数组）／`-3` 缺参/超长/分组不存在／**`-9` 用户身份缺失或无效**／`-5` 后端失败 | ✅ | `/zh-cn/api/quote/watchlist/user-security` |
| 获取自选股分组 | `watchlist_groups` | GET `/api/v1.0/quote/user-security-group` | `group_type`∈{ALL,CUSTOM,SYSTEM}（**大小写敏感**） | `group_list[]{group_name, group_type}` | `-3` group_type 非法／`-9` 用户身份缺失/无效／`-5` 后端失败 | ✅ | `/zh-cn/api/quote/watchlist/user-security-group` |
| 修改自选股 | HTTP-only，**仅 Web 用户操作，不进 MCP 工具面** | POST `/api/v1.0/quote/modify-user-security` | **`op` 必填**、**`code_list` 必填**（非空 ≤200）、`group_name`(≤100) | `result_code` | `-3` 缺 op/code_list 空或超 200/group_name 超长/op 非法／`-7` 查无此证券／`-8` 对系统或虚拟分组非法增删／`-9` 用户身份缺失/无效／`-5` 内部错误 | ✅ | `/zh-cn/api/quote/watchlist/modify-user-security` |

### C.9 模拟交易（9）⚠️

> **✅ 鉴权模型已实测（WP13 任务 2，2026-09-16）**：官方文档称 `uid` 由**登录态 header**
> 自动透传，本仓库 AppKey（Ed25519 签名）凭据**实测兼容**——`GET /api/v1.0/sim-trade/accounts`
> 返回 9 个模拟账户，写路径（挂单→改单→撤单）全链路真机通过，故 sim 链路已按本表迁移到
> REST（`futu_channel=openapi`）。**追加实测差异**：`orders`/`history-orders`/`max-buy-sell`
> 的 `market` 必填（页面未列）；`history-orders` 时间窗需微秒 int；`cancel_order`/
> `modify_order` 需 body 携带 `market`（cancel 页写「无请求体」）。四条差异与证据见
> `docs/TOOL-LIMITS.md` §九。
> ③ 官方模拟交易文档**未提供错误码表**（下表「错误码」列如实标「文档未列」）。

| 端点 | 方法 + 路径 | 关键参数 | 响应关键字段 | 状态 | 文档 |
|---|---|---|---|---|---|
| 账户列表 | GET `/api/v1.0/sim-trade/accounts` | 无入参（uid 由登录态 header） | `accounts[]{account_id, broker_id, market_id(1=HK 2=US 3=US_OPTION 9=HKCC 18=CA), intra_account_id, account_type, account_title}` | ⚠️ | `/zh-cn/api/sim-trade/account-list` |
| 账户资金 | GET `/api/v1.0/sim-trade/{acc_id}/cash-info` | `acc_id` 路径 | `balance, hold, max_power_long, total_asset, mv, long_mv, short_mv, unrealized_profit, realized_profit` | ⚠️ | `/zh-cn/api/sim-trade/cash-info` |
| 持仓列表 | GET `/api/v1.0/sim-trade/{acc_id}/positions` | `acc_id` 路径、`market` 查询选填 | `pstn_id, pstn_type, market, symbol, stock_name, qty, qty_avbl, cost_price, buy_avg_price, cur_price, mv, profit, profit_ratio` | ⚠️ | `/zh-cn/api/sim-trade/position-list` |
| 今日订单 | GET `/api/v1.0/sim-trade/{acc_id}/orders` | `acc_id` 路径 | `order_id, side, symbol, stock_name, order_type, status, qty, cum_qty, price, avg_fill_price, aux_price, create_time, update_time, text` | ⚠️ | `/zh-cn/api/sim-trade/order-list` |
| 历史订单 | GET `/api/v1.0/sim-trade/{acc_id}/history-orders` | `acc_id` 路径、`time_begin`、`time_end`、`page_size`、`next_key` | `orders[]` + `pagination{has_more, next_key}`（页面响应字段表未列表，字段取自响应示例） | ⚠️ | `/zh-cn/api/sim-trade/history-order-list` |
| 最大可买可卖 | GET `/api/v1.0/sim-trade/{acc_id}/max-buy-sell` | `acc_id` 路径、**`symbol` 必填**、**`order_type` 必填**、`price`、`order_id` | `max_cash_buy_qty_round_lot, max_margin_buy_qty_round_lot, max_sell_qty_round_lot, max_sell_short_qty, max_buy_back_qty, required_im_long, required_im_short` | ⚠️ | `/zh-cn/api/sim-trade/max-buy-sell` |
| 下单 | POST `/api/v1.0/sim-trade/{acc_id}/orders` | `acc_id` 路径；body：**`market`/`symbol`/`order_type`/`order_side`/`qty` 必填**、`price`、`text` | `order_id` | ⚠️ | `/zh-cn/api/sim-trade/input-order` |
| 改单 | POST `/api/v1.0/sim-trade/{acc_id}/orders/{order_id}/modify` | `acc_id`、`order_id` 路径；body：`new_qty`、`new_price` | `order_id` | ⚠️ | `/zh-cn/api/sim-trade/modify-order` |
| 撤单 | POST `/api/v1.0/sim-trade/{acc_id}/orders/{order_id}/cancel` | `acc_id`、`order_id` 路径 | `order_id` | ⚠️ | `/zh-cn/api/sim-trade/cancel-order` |

### C.10 范围外（🚫 不做，登记在案）

| 族 | 端点 | 理由 |
|---|---|---|
| 加密货币交易 | `/api/v1.0/crypto-trading/*`（授权账户/总余额、下单/改单/撤单/购买力、活跃订单/历史订单/订单详情、成交明细/历史成交、交易推送 6 页） | 独立资产类别与账户体系，超出三市场（A/H/US 证券·ETF·期权）范围；纳入需另立规格并重做风控与准入（规格 §1.1） |

## D. 特殊发现与冲突（供 WP12/WP13 调整计划）

### D.1 官方「搜索」页 = 资讯 + 社区搜索，**不存在证券代码搜索端点**

`/zh-cn/api/quote/basic-data/search` 页面标题为「搜索」，正文明确：「包含两个子接口：
**find-news（资讯搜索）、find-community（社区搜索）**」。即：

- 官方**没有**按代码/名称检索证券的 REST 端点（`-7 invalid_symbol` 的处置建议写的是
  「用 search 类接口确认代码合法性」，但该 search 实为资讯/社区搜索——文档此处措辞不精确）；
- 仓库既有 `info_search` → `quote_news_search` → `/quote/find-news` 的映射**正确**，
  且 `/quote/find-community` 亦已接入；
- **影响**：WP12 计划任务 4 的直通工具清单里 `info_search_stock` **不存在，须删除**
  （该行由本表纠正）；直通工具数由 12 降为 **11**，工具面预算随之为
  59+1(WP11)+11+2=**73**（仍 ≤80）。

### D.2 `llms.txt` 与官方文档站的三处漂移（已实测）

| # | 漂移 | 事实 |
|---|---|---|
| 1 | F10 族目录 | llms.txt 的 `/api/quote/f10/*.md` **全部 404**；官方已重组为 7 个命名空间：`financials/`、`research/`、`valuation/`、`corporate-actions/`、`shareholders/`、`company/`、`top-brokers/` |
| 2 | llms.txt 漏列 2 个端点 | `GET /api/v1.0/quote/valuation/index-stocks`（指数成分股估值）、`GET /api/v1.0/quote/valuation/index-stock-plates`（指数所属板块）——**均在本轮核对中确认存在并有完整文档** |
| 3 | 经纪商端点拆分 | llms.txt 只列 `top-brokers` 一项；实际为**两个端点**（`top-brokers` 实时 + `top-brokers-history` 历史） |

### D.3 与仓库既有实现的差异（如实登记）

| 项 | 仓库现状 | 官方口径 | 处置 |
|---|---|---|---|
| 复权因子路径 | `trading_datasource.futu_mcp` 经托管 MCP | REST 路径为 `/quote/{symbol}/corporate-actions/rehab`（文档归属「基本数据」） | WP13 任务 1 迁移时按 REST 路径实现 |
| 账户资金总净资产字段 | WP9 已核实 `total_assets` | 模拟资金端点字段为 `total_asset`（**无 s**） | 两者是不同端点（live get-funds vs sim cash-info），**不冲突**；WP13 实现 sim 时用 `total_asset` |
| 模拟交易改单 | 仓库注释记录 `sim_trade_modify_order` 间歇性 `-5`，故 sim 改单走「撤旧重下」 | REST 提供原生 `.../orders/{order_id}/modify`（`new_qty`/`new_price`） | 保持现状（撤旧重下）；是否切原生取决于 WP13 实测，登记 `docs/TOOL-LIMITS.md` |
| 期权数据权限 | —— | `option-exercise-probability` 可能返回 **`-9` 无期权数据查询权限** | WP12 实现时把 `-9` 映射为如实拒绝（不重试、不当空数据） |

## E. 覆盖统计（2026-09-16）

| 项 | 数量 | 说明 |
|---|---|---|
| ✅ 已核对（新接入目标） | **53** | 基础数据 4 + 板块 2 + 筛选 2 + IPO 1 + 个股深度 26 + 卖空 2 + 衍生品 4 + 自选 3 + 模拟交易 9 |
| ⚠️ 已核对但带注意事项 | **3 行** | 模拟交易 9 行整体（登录态鉴权模型待实测，§C.9）；均**已核对路径与参数**，不属于 BLOCKED |
| ⛔ BLOCKED | **0** | 48 份文档全部取到；无路径靠猜 |
| 🚫 范围外 | 1 族 | 加密货币交易（§C.10） |
| 已覆盖（WP8，仅登记） | 30 | 交易 13 + 实时行情 6 + 基础数据 5（含 find-news/find-community）+ 资金流 3 + 衍生品 2 + 筛选 1 |

**BLOCKED 清单**：无。

**结论**：WP12 任务 2/3 可按本表落地传输层方法组；任务 4 的直通工具清单需按 §D.1 删除
`info_search_stock`；任务 5 的 F10 落库 section 以 §C.5 的 26 项为准；WP13 任务 2 的模拟交易
REST 迁移**先实测鉴权模型**（§C.9）。
