"""WP8 任务 2 行情方法组（路径来自 ..paths，禁止内联字面量）。"""
from .. import paths
from ..validators import _RestValidators
class OpenApiMarket(_RestValidators):
    """富途行情 OpenAPI（REST）方法组：WP8 任务 2 的 OpenAPI 后端唯一入口。

    每个方法对应一个官方 REST 端点（方法名 = futu_data.OPENAPI_METHODS 的登记值）；
    参数名与官方文档一致（code_list/code/symbol/num/ktype/autype/...）。调用方必须
    先配好凭据（~/.dsh/futu-openapi.json，OAuth 或 AppKey），否则 client.request 抛
    OpenApiError。
    """

    #: 行情快照/报价/基本信息/市场状态批量上限（官方：单次最多 400 个 code）
    MAX_CODE_LIST = 400
    #: K 线单次条数上限（官方：num 默认 370、最大 370）
    MAX_KLINE_NUM = 370
    #: 买卖盘档数上限（官方：num 1..60）
    MAX_ORDER_BOOK_NUM = 60
    #: 逐笔成交条数上限（官方：num 默认 500、最大 750）
    MAX_TICKER_NUM = 750

    #: ktype 枚举（官方文档 cur-kline 页）：1=1分 2=日 3=周 4=月 5=年 6=5分 7=15分
    #: 8=30分 9=60分 10=3分 11=季 14=120分 15=240分 26=10分 29=180分
    KTYPE_VALUES = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15, 26, 29})
    #: autype 枚举：0=不复权 1=前复权 2=后复权 3=前复权含股息 4=后复权含股息
    AUTYPE_VALUES = frozenset({0, 1, 2, 3, 4})
    #: extended_time 枚举：0=默认 1=含盘前盘后（美股 1 分 K） 2=含夜盘
    EXTENDED_TIME_VALUES = frozenset({0, 1, 2})
    #: rt-data 的交易时段枚举（官方文档 rt-data 页，6 值；**仅 rt_data 用**）
    RT_SECTIONS = frozenset({"NORMAL", "FULL", "PREMARKET", "AFTERHOURS",
                             "HK_DARK", "OVERNIGHT"})
    #: capital-flow 的 section 枚举（官方 naming-dictionary#capital-flow-section，4 值）。
    #: **不是 RT_SECTIONS**：网关逐字返回 allowed:[NORMAL, FULL, PREMARKET, AFTERHOURS]
    #: （2026-09-16 实测 OVERNIGHT/HK_DARK → -3 invalid_parameter）；两者复用同一常量
    #: 会把 rt-data 的夜盘枚举漏进 capital-flow，是已复现的 live 缺陷。
    CAPITAL_FLOW_SECTIONS = frozenset({"NORMAL", "FULL", "PREMARKET", "AFTERHOURS"})
    #: rt-ticker 的时段过滤枚举
    TICKER_PERIODS = frozenset({"NORMAL", "BEFORE", "AFTER", "OVERNIGHT"})
    #: trading-days 的市场枚举（官方文档 trading-days 页）
    TRADING_MARKETS = frozenset({"HK", "US", "SH", "SZ", "BJ", "SG", "JP", "CA",
                                 "AU", "JP_FUTURE", "SG_FUTURE"})
    #: option-expiration / option-chain 的 filter_standard 枚举
    FILTER_STANDARDS = frozenset({"ALL", "STANDARD", "NON_STANDARD"})
    #: option-expiration 的 filter_expiration_cycles 取值（官方网关 pattern 逐字，
    #: 2026-09-16 实测；请求形态是**逗号分隔字符串**而非数组，见 _csv_enum）
    EXPIRATION_CYCLES = frozenset({"MONTH", "WEEK", "END_OF_MONTH", "QUARTERLY",
                                   "WEEKMON", "WEEKTUE", "WEEKWED", "WEEKTHU",
                                   "WEEKFRI"})
    #: capital-flow-history 的聚合周期
    FLOW_PERIOD_TYPES = frozenset({"DAY", "WEEK", "MONTH"})
    #: find-news 的资讯类型：1=资讯 2=公告 3=研报
    NEWS_TYPES = frozenset({1, 2, 3})
    #: find-news / find-community 排序：1=热度/阅读量 2=时间
    SEARCH_SORT_TYPES = frozenset({1, 2})
    #: find-news / find-community 语言过滤
    SEARCH_LANGS = frozenset({"zh-CN", "zh-HK", "en", "ja"})
    #: find-community 社区类型：1=讨论 2=话题 3=直播
    COMMUNITY_TYPES = frozenset({1, 2, 3})

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------ 校验助手
    # _codes/_int_in/_enum_in/_date/_body 五个通用校验器来自 _RestValidators
    # （与 OpenApiTrade 共用一份实现）；_merge_pagination 亦已提升至基类
    # （WP12 任务 2 起数据面方法组共用）。

    # ------------------------------------------------------------ 实时行情（realtime）

    def market_snapshot(self, code_list):
        """POST /api/v1.0/quote/snapshot —— 行情快照（批量 1..400，按品类分组字段）。"""
        return self.client.request("POST", paths.QUOTE_SNAPSHOT_PATH,
                                   json_body={"code_list": self._codes(code_list,
                                                                       self.MAX_CODE_LIST)})

    def stock_quote(self, code_list):
        """POST /api/v1.0/quote/stock-quote —— 实时报价（轻量版快照）。"""
        return self.client.request("POST", paths.STOCK_QUOTE_PATH,
                                   json_body={"code_list": self._codes(code_list,
                                                                       self.MAX_CODE_LIST)})

    def order_book(self, code, num=None):
        """POST /api/v1.0/quote/order-book —— 买卖盘（num 1..60，缺省=权限档上限）。"""
        body = {"code": self._codes([code], 1)[0]}
        if num is not None:
            body["num"] = self._int_in(num, 1, self.MAX_ORDER_BOOK_NUM, "num")
        return self.client.request("POST", paths.ORDER_BOOK_PATH, json_body=body)

    def cur_kline(self, symbol, num, ktype=2, autype=1, extended_time=0):
        """GET /api/v1.0/quote/{symbol}/cur-kline —— 当前 K 线（num 必填 1..370）。"""
        query = {
            "num": self._int_in(num, 1, self.MAX_KLINE_NUM, "num"),
            "ktype": self._enum_in(ktype, self.KTYPE_VALUES, "ktype", default=2),
            "autype": self._enum_in(autype, self.AUTYPE_VALUES, "autype", default=1),
            "extended_time": self._enum_in(extended_time, self.EXTENDED_TIME_VALUES,
                                           "extended_time", default=0),
        }
        return self.client.request("GET", paths.CUR_KLINE_PATH.format(symbol=symbol),
                                   query=query)

    def rt_data(self, symbol, request_section="NORMAL"):
        """GET /api/v1.0/quote/{symbol}/rt-data —— 分时数据（时段枚举见 RT_SECTIONS）。"""
        query = {"request_section": self._enum_in(request_section, self.RT_SECTIONS,
                                                  "request_section", default="NORMAL")}
        return self.client.request("GET", paths.RT_DATA_PATH.format(symbol=symbol),
                                   query=query)

    def rt_ticker(self, symbol, num=500, period=None):
        """GET /api/v1.0/quote/{symbol}/rt-ticker —— 逐笔成交（num 1..750；period 列表）。

        ``period`` 是**同名多值**参数：``["BEFORE","AFTER"]`` 出站为
        ``?num=…&period=BEFORE&period=AFTER``（由 ``query_string`` 的 doseq 展开），
        绝不是 ``period=['BEFORE', 'AFTER']`` 的 Python repr（后者实测 -3）。
        """
        query = {"num": self._int_in(num, 1, self.MAX_TICKER_NUM, "num", default=500)}
        if period is not None:
            if not isinstance(period, list) or not period or \
                    any(item not in self.TICKER_PERIODS for item in period):
                raise ValueError(f"period 必须是 {sorted(self.TICKER_PERIODS)} 的非空列表")
            query["period"] = list(period)
        return self.client.request("GET", paths.RT_TICKER_PATH.format(symbol=symbol),
                                   query=query)

    # ------------------------------------------------------------ 基本数据（basic-data）

    def stock_basicinfo(self, code_list):
        """POST /api/v1.0/quote/stock-basicinfo —— 标的基本静态信息（批量 1..400）。"""
        return self.client.request("POST", paths.STOCK_BASICINFO_PATH,
                                   json_body={"code_list": self._codes(code_list,
                                                                       self.MAX_CODE_LIST)})

    def trading_days(self, market, start, end):
        """GET /api/v1.0/quote/trading-days —— 交易日历（market/start/end 全必填）。"""
        query = {
            "market": self._enum_in(market, self.TRADING_MARKETS, "market"),
            "start": self._date(start, "start", required=True),
            "end": self._date(end, "end", required=True),
        }
        if query["start"] > query["end"]:
            raise ValueError("start 不能晚于 end")
        return self.client.request("GET", paths.TRADING_DAYS_PATH, query=query)

    def history_kline(self, symbol, end, start=None, ktype=2, autype=1,
                      num=370, extended_time=0):
        """GET /api/v1.0/quote/{symbol}/history-kline —— 历史 K 线（end 必填；num≤370）。

        信封顶层 pagination 并入返回值（向更早翻页游标，与 MCP 通道同形状）。
        """
        query = {
            "start": self._date(start, "start"),
            "end": self._date(end, "end", required=True),
            "ktype": self._enum_in(ktype, self.KTYPE_VALUES, "ktype", default=2),
            "autype": self._enum_in(autype, self.AUTYPE_VALUES, "autype", default=1),
            "num": self._int_in(num, 1, self.MAX_KLINE_NUM, "num", default=370),
            "extended_time": self._enum_in(extended_time, self.EXTENDED_TIME_VALUES,
                                           "extended_time", default=0),
        }
        d, pagination = self.client.request_meta(
            "GET", paths.HISTORY_KLINE_PATH.format(symbol=symbol), query=query)
        return self._merge_pagination(d, pagination)

    def market_state(self, code_list, is_contain_ba=None, is_contain_overnight=None):
        """POST /api/v1.0/quote/market-state —— 市场状态（批量 1..400，带市场前缀）。"""
        body = self._body({
            "code_list": self._codes(code_list, self.MAX_CODE_LIST),
            "is_contain_ba": is_contain_ba,
            "is_contain_overnight": is_contain_overnight,
        })
        for flag in ("is_contain_ba", "is_contain_overnight"):
            if flag in body and not isinstance(body[flag], bool):
                raise ValueError(f"{flag} 必须是布尔值")
        return self.client.request("POST", paths.MARKET_STATE_PATH,
                                   json_body=body)

    def search_news(self, symbol, size=10, news_type=None, sort_type=None, lang=None):
        """GET /api/v1.0/quote/find-news —— 资讯搜索（官方 search 页的资讯子接口）。"""
        query = {
            "symbol": self._keyword(symbol),
            "size": self._int_in(size, 1, 50, "size", default=10),
            "news_type": self._enum_in(news_type, self.NEWS_TYPES, "news_type",
                                       default=None),
            "sort_type": self._enum_in(sort_type, self.SEARCH_SORT_TYPES, "sort_type",
                                       default=None),
            "lang": self._enum_in(lang, self.SEARCH_LANGS, "lang", default=None),
        }
        return self.client.request("GET", paths.FIND_NEWS_PATH,
                                   query=self._body(query))

    def search_community(self, symbol, size=10, community_type=None, sort_type=None,
                         lang=None):
        """GET /api/v1.0/quote/find-community —— 社区搜索（search 页的社区子接口）。"""
        query = {
            "symbol": self._keyword(symbol),
            "size": self._int_in(size, 1, 50, "size", default=10),
            "community_type": self._enum_in(community_type, self.COMMUNITY_TYPES,
                                            "community_type", default=None),
            "sort_type": self._enum_in(sort_type, self.SEARCH_SORT_TYPES, "sort_type",
                                       default=None),
            "lang": self._enum_in(lang, self.SEARCH_LANGS, "lang", default=None),
        }
        return self.client.request("GET", paths.FIND_COMMUNITY_PATH,
                                   query=self._body(query))

    def _keyword(self, symbol):
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol（搜索关键词）必须是非空字符串")
        return symbol

    # ------------------------------------------------------------ 资金（capital-flow）

    def capital_flow(self, symbol, section="NORMAL"):
        """GET /api/v1.0/quote/{symbol}/capital-flow —— 日内分钟级资金流。

        ``section`` 用 ``CAPITAL_FLOW_SECTIONS``（4 值），**不是** ``RT_SECTIONS``（6 值，
        含 rt-data 专有的 HK_DARK/OVERNIGHT，网关对 capital-flow 拒绝这两个值）。
        """
        query = {"section": self._enum_in(section, self.CAPITAL_FLOW_SECTIONS, "section",
                                          default="NORMAL")}
        return self.client.request("GET", paths.CAPITAL_FLOW_PATH.format(symbol=symbol),
                                   query=query)

    def capital_flow_history(self, symbol, period_type="DAY", start=None, end=None,
                             count=365):
        """GET /api/v1.0/quote/{symbol}/capital-flow/history —— 历史资金流（count 1..1000）。

        信封顶层 pagination 并入返回值（has_more，与 MCP 通道同形状）。
        """
        query = {
            "period_type": self._enum_in(period_type, self.FLOW_PERIOD_TYPES,
                                         "period_type", default="DAY"),
            "start": self._date(start, "start"),
            "end": self._date(end, "end"),
            "count": self._int_in(count, 1, 1000, "count", default=365),
        }
        d, pagination = self.client.request_meta(
            "GET", paths.CAPITAL_FLOW_HISTORY_PATH.format(symbol=symbol), query=query)
        return self._merge_pagination(d, pagination)

    def capital_distribution(self, symbol):
        """GET /api/v1.0/quote/{symbol}/capital-distribution —— 日内资金分布快照。"""
        return self.client.request("GET",
                                   paths.CAPITAL_DISTRIBUTION_PATH.format(symbol=symbol))

    # ------------------------------------------------------------ 衍生品 / 筛选

    def option_expiration(self, symbol, index_option_type=None,
                          filter_standard="ALL", filter_expiration_cycles=None):
        """GET /api/v1.0/quote/{symbol}/option-expiration —— 期权到期日列表。

        ``filter_expiration_cycles`` 官方类型是**逗号分隔字符串**（如 ``"WEEK,MONTH"``），
        传 list 本地拒绝（见 ``_csv_enum``）；若不拒，list 会出站成 Python repr 并被
        网关 -3 拒绝（已实测）。
        """
        query = self._body({
            "index_option_type": index_option_type,
            "filter_standard": self._enum_in(filter_standard, self.FILTER_STANDARDS,
                                             "filter_standard", default="ALL"),
            "filter_expiration_cycles": self._csv_enum(
                filter_expiration_cycles, self.EXPIRATION_CYCLES,
                "filter_expiration_cycles"),
        })
        return self.client.request("GET", paths.OPTION_EXPIRATION_PATH.format(symbol=symbol),
                                   query=query)

    def option_chain(self, symbol, start=None, end=None, index_option_type=None,
                     filter_standard="ALL"):
        """GET /api/v1.0/quote/{symbol}/option-chain —— 期权链（单次最多 20 个到期日）。"""
        query = self._body({
            "start": self._date(start, "start"),
            "end": self._date(end, "end"),
            "index_option_type": index_option_type,
            "filter_standard": self._enum_in(filter_standard, self.FILTER_STANDARDS,
                                             "filter_standard", default="ALL"),
        })
        return self.client.request("GET", paths.OPTION_CHAIN_PATH.format(symbol=symbol),
                                   query=query)

    def option_screen(self, strategy, field_filter=None, sort_obj=None, next_key=None,
                      limit=None, request_exact_data=None, strategy_param=None):
        """POST /api/v1.0/quote/option-screen —— 期权筛选器（strategy 必填对象）。

        信封顶层 pagination 并入返回值（has_more/next_key/total，与 MCP 通道同形状）。
        """
        if not isinstance(strategy, dict) or not strategy:
            raise ValueError("strategy 必须是非空对象（如 {market_category_list: [1]}）")
        body = self._body({
            "strategy": strategy,
            "field_filter": field_filter,
            "sort_obj": sort_obj,
            "next_key": next_key,
            "limit": limit,
            "request_exact_data": request_exact_data,
            "strategy_param": strategy_param,
        })
        if "limit" in body:
            body["limit"] = self._int_in(body["limit"], 0, 1000, "limit")
        for name in ("field_filter", "sort_obj", "strategy_param"):
            if name in body and not isinstance(body[name], dict):
                raise ValueError(f"{name} 必须是对象")
        if "next_key" in body and not isinstance(body["next_key"], str):
            raise ValueError("next_key 必须是字符串（分页游标）")
        d, pagination = self.client.request_meta(
            "POST", paths.OPTION_SCREEN_PATH, json_body=body)
        return self._merge_pagination(d, pagination)


# ---------------------------------------------------------------------------
# OpenApiTrade：交易 REST 方法组（WP8 任务 3）
# ---------------------------------------------------------------------------
# 13 个方法与官方路径/参数逐项对照（2026-09-16 web_fetch 实抓 .md 原文，前缀 /api/v1.0）：
#   下单        POST   /accounts/{acc_id}/orders            body {code! qty! side! order_type!
#                                                            time_in_force! price? session?
#                                                            aux_price? lot_type? remark?
#                                                            order_class? multi_leg_info?}
#   改单        PUT    /accounts/{acc_id}/orders/{order_id}  body {exchange! qty! price! aux_price?}
#   撤单        DELETE /accounts/{acc_id}/orders/{order_id}  query {exchange!}
#   二次确认    POST   /accounts/{acc_id}/order_confirm      body {confirm_id!}
#   最大可交易量 GET    /accounts/{acc_id}/acctradinginfo    query {code! order_type! price? order_id?}
#   未完成订单  GET    /accounts/{acc_id}/orders             query {trd_market! page_flag! page_size? 10..100}
#   历史订单    GET    /accounts/{acc_id}/orders_history     query {trd_market! page_flag! code? start?
#                                                            end? page_size? 10..100}
#   订单详情    POST   /accounts/{acc_id}/orders/detail      body {exchange! order_ids!（<50 个）}
#   当日成交    GET    /accounts/{acc_id}/order_fills        query {trd_market! page_flag! page_size? 10..100}
#   历史成交    GET    /accounts/{acc_id}/fills_history      query {trd_market! page_flag! code? start?
#                                                            end? page_size? 10..50}
#   授权账户    GET    /accounts/authorized_trd_accs         （无路径/查询参数）
#   账户资金    GET    /accounts/{acc_id}/funds              query {currency?}
#   持仓        GET    /accounts/{acc_id}/positions          query {code? pl_ratio_min? pl_ratio_max?}
#
# 信封：交易侧是 ``{"s":"ok","d":...}`` / ``{"s":"error","errcode","errmsg","jump_url"?,
# "need_order_confirm"?,"confirm_id"?}``（**行情侧是另一套**：``{"ret_code":0,"data":...,
# "pagination":...}``，见模块头与 parse_envelope_meta——两者不是同一信封），由 client.request
# 统一解析——need_order_confirm=true 抛 ``OrderConfirmRequired``（订单在券商侧**已挂起**，
# 调 order_confirm 放行；**禁止对原请求重发**）。既非 s 信封也非 ret_code 信封（5xx/429/
# 网关页/非 JSON）→ ``UnexpectedResponse``：**没有业务结论**，写路径落 unknown 先查询，
# 不得当业务拒绝。
#
# 文档与实现的差异登记（不猜，逐条给出源码依据）：
#   * funds 的 ``currency`` 在官方参数表标 Required=Yes，但同页 curl 示例未传该参数
#     → 本层按**可选**处理（按 Required 会与官方示例直接冲突），并在 docstring 登记。
#   * 改单官方明示「Does not support modifying A-share orders」→ **本层只做事实透传**
#     不在传输层预判市场（传输层只忠实实现文档契约；「A 股改单怎么办」是交易闸门的策略，
#     在 platform/server/trading.py 里决定并写明给调用方的替代路径）。
#   * exchange 官方枚举没有北交所（BJ）→ 本层不自行扩枚举（strict：未知值拒绝）；
#     北交所标的的撤单/改单因此会在此被拒（如实暴露，不伪造 exchange 值）。
#   * ``multi_leg_info`` 在 place-order 参数表标类型 ``MultiLegInfo``（对象），而
#     naming-dictionary 的 ``Order.multi_leg_info`` 是 ``list[MultiLegInfo]``（响应侧）
#     → 本层请求侧**对象与对象列表都接受、原样透传**，内键按 naming-dictionary 白名单
#     校验（多腿订单两种形态在真实通道的取舍以实测为准，见 tests/test_wp8_trading.py）。
#   * 时间戳参数（orders_history/fills_history 的 start/end）官方单位是**微秒**
#     → 本层只校验非负整数，不做单位换算（调用方给什么传什么）。
#   * ``positions`` 与 ``order_details`` 的响应 ``d`` 是**数组**（其余是对象）→
#     本层原样返回，不做形状包装。
