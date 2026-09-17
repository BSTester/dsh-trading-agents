"""WP12 数据面方法组：筛选 / 板块 / 做空 / 基础数据 / IPO / 自选 / 衍生品。"""
import re

from .. import paths
from ..errors import OpenApiError
from ..validators import _RestValidators
class OpenApiScreen(_RestValidators):
    """全市场筛选方法组（锁定表 §C.3）：条件选股与窝轮筛选器。

    官方筛选是「条件数组 + 取值数组」模型：``screen_queries`` 过滤、``retrieve_queries``
    决定每个命中标的返回哪些列（顺序一一对应）。本层只校验**结构**（每元素恰好一个
    白名单键）；property name / 具体枚举值语义属网关判定（-5），锁定表未给全量字段字典，
    故不在此自行枚举。
    """

    #: screen_queries 的「11 选 1」查询类型（锁定表 §C.3）
    SCREEN_QUERY_KEYS = frozenset({
        "simple_field_query", "plate_query", "simple_property_query",
        "cumulative_property_query", "financial_property_query",
        "indicator_positional_query", "indicator_pattern_query",
        "featured_property_query", "broker_holdings_query", "kline_shape_query",
        "option_query"})
    #: retrieve_queries 的「9 选 1」取值类型（锁定表 §C.3）
    RETRIEVE_QUERY_KEYS = frozenset({
        "basic_property", "simple_property", "cumulative_property",
        "financial_property", "featured_property", "indicator_property",
        "broker_property", "kline_shape_property", "option_property"})
    #: sort/sorts 的 direction：1=升序 2=降序 3=绝对值升序 4=绝对值降序
    SORT_DIRECTIONS = frozenset({1, 2, 3, 4})
    #: user_stock_list_mode：0=不限制 1=自选范围 2=持仓范围
    USER_STOCK_LIST_MODES = frozenset({0, 1, 2})
    #: stock-screen 单页上限（官方 limit 默认 200、最大 300）
    SCREEN_LIMIT_MAX = 300
    #: warrant-screen 的 market_type 枚举（锁定表 §C.3）
    WARRANT_MARKET_TYPES = frozenset({1, 4, 15})
    #: warrant-screen 单页上限
    WARRANT_LIMIT_MAX = 1000

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------ 校验助手（筛选专有）

    def _query_list(self, value, allowed, name, required=True):
        """「N 选 1」查询数组：每元素是恰好一个白名单键的对象。"""
        if value is None:
            if required:
                raise ValueError(f"{name} 必填（查询对象数组，每元素恰好一个查询类型）")
            return None
        if not isinstance(value, list) or not value:
            raise ValueError(f"{name} 必须是非空数组，得到：{value!r}")
        for item in value:
            if not isinstance(item, dict) or len(item) != 1:
                raise ValueError(
                    f"{name} 每个元素必须是恰好一个查询类型的对象"
                    f"（允许：{sorted(allowed)}）")
            key = next(iter(item))
            if key not in allowed:
                raise ValueError(f"{name} 含未支持查询类型：{key!r}"
                                 f"（允许：{sorted(allowed)}）")
        return list(value)

    def _sort_obj(self, value, name):
        """单字段排序对象：``{direction, <property_type>: {...}}``。"""
        if value is None:
            return None
        if not isinstance(value, dict) or not value:
            raise ValueError(f"{name} 必须是非空对象（如 {{direction: 2, "
                             f"simple_property: {{name: 2301}}}}）")
        if "direction" in value:
            self._enum_in(value["direction"], self.SORT_DIRECTIONS,
                          f"{name}.direction")
        return value

    def _sort_list(self, value, name):
        """多字段排序数组（元素同 ``_sort_obj``）。"""
        if value is None:
            return None
        if not isinstance(value, list) or not value:
            raise ValueError(f"{name} 必须是非空数组（元素为排序对象）")
        for item in value:
            self._sort_obj(item, f"{name} 元素")
        return list(value)

    def _id_list(self, value, name):
        """stock_id 整数数组（watchlist_stock_ids / holding_stock_ids）。"""
        if value is None:
            return None
        if not isinstance(value, list) or not value:
            raise ValueError(f"{name} 必须是非空整数数组")
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"{name} 元素必须是非负整数，得到：{item!r}")
        return list(value)

    # ------------------------------------------------------------ 筛选

    def stock_screen(self, screen_queries, retrieve_queries=None, sort=None, sorts=None,
                     next_key=None, limit=None, watchlist_stock_ids=None,
                     holding_stock_ids=None, user_stock_list_mode=None):
        """POST /api/v1.0/quote/stock-screen —— 条件选股（锁定表 §C.3）。

        信封顶层 pagination（total/has_more/next_key）并入返回值。限制：``broker_holdings_query``
        / ``kline_shape_query`` 仅 HK；``option_query`` 需标的有期权（服务端判定）。
        """
        body = self._body({
            "screen_queries": self._query_list(screen_queries, self.SCREEN_QUERY_KEYS,
                                               "screen_queries"),
            "retrieve_queries": self._query_list(retrieve_queries,
                                                 self.RETRIEVE_QUERY_KEYS,
                                                 "retrieve_queries", required=False),
            "sort": self._sort_obj(sort, "sort"),
            "sorts": self._sort_list(sorts, "sorts"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, 1, self.SCREEN_LIMIT_MAX, "limit"),
            "watchlist_stock_ids": self._id_list(watchlist_stock_ids,
                                                 "watchlist_stock_ids"),
            "holding_stock_ids": self._id_list(holding_stock_ids, "holding_stock_ids"),
            "user_stock_list_mode": self._enum_in(user_stock_list_mode,
                                                  self.USER_STOCK_LIST_MODES,
                                                  "user_stock_list_mode", default=None),
        })
        d, pagination = self.client.request_meta("POST", paths.STOCK_SCREEN_PATH,
                                                 json_body=body)
        return self._merge_pagination(d, pagination)

    def warrant_screen(self, market_type=None, is_delay=None, only_count=None,
                       stock_owner=None, screen_groups=None, sorts=None,
                       next_key=None, limit=None):
        """POST /api/v1.0/quote/warrant-screen —— 窝轮筛选器（锁定表 §C.3）。

        **数据端点**：平台策略/风控/执行不引入窝轮品类（规格 §1.1）；本方法只保证 API
        面完整。``market_type`` 1/4/15（锁定表枚举），``limit`` ≤1000。
        ``is_delay/only_count/screen_groups`` 锁定表**只给参数名、未给类型与枚举** →
        原样透传不发明（POST body 走 JSON 序列化，无查询串的多值序列化风险）。
        """
        body = self._body({
            "market_type": self._enum_in(market_type, self.WARRANT_MARKET_TYPES,
                                         "market_type", default=None),
            "is_delay": is_delay,
            "only_count": only_count,
            "stock_owner": self._text(stock_owner, "stock_owner", required=False),
            "screen_groups": screen_groups,
            "sorts": self._sort_list(sorts, "sorts"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, 1, self.WARRANT_LIMIT_MAX, "limit"),
        })
        d, pagination = self.client.request_meta("POST", paths.WARRANT_SCREEN_PATH,
                                                 json_body=body)
        return self._merge_pagination(d, pagination)


class OpenApiPlate(_RestValidators):
    """板块方法组（锁定表 §C.2）：板块列表与板块成分股。

    ``market`` 官方未在本轮核对中给出枚举全量（只给「必填 + 命名市场」），故本层只校验
    **大写市场码形状**（``^[A-Z][A-Z0-9_]*$``），不发明枚举。``plate_class=REGION`` 仅
    SH/SZ 支持：该条件本地可判定，**前置拒绝**（官方 ``-8 unsupported``，零网络往返）。
    """

    #: plate_class 枚举（官方大小写敏感）
    PLATE_CLASSES = frozenset({"ALL", "INDUSTRY", "REGION", "CONCEPT", "OTHER"})
    #: REGION 板块仅支持的市场
    REGION_MARKETS = frozenset({"SH", "SZ"})

    def __init__(self, client):
        self.client = client

    def _market(self, value):
        """市场码：大写字母/下划线形状（不发明枚举，见类 docstring）。"""
        if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", value.strip()):
            raise ValueError(f"market 必须是大写市场码（如 HK/US/SH/SZ），得到：{value!r}")
        return value.strip()

    def plate_list(self, market, plate_class):
        """GET /api/v1.0/quote/plate-list —— 指定市场指定分类的板块列表（锁定表 §C.2）。"""
        market = self._market(market)
        plate_class = self._enum_in(plate_class, self.PLATE_CLASSES, "plate_class")
        if plate_class == "REGION" and market not in self.REGION_MARKETS:
            raise ValueError(f"plate_class=REGION 仅支持 {'/'.join(sorted(self.REGION_MARKETS))}"
                             f"（官方 -8 unsupported），得到 market={market}")
        return self.client.request("GET", paths.PLATE_LIST_PATH,
                                   query={"market": market, "plate_class": plate_class})

    def plate_stock(self, plate_code, sort_field=None, ascend=None, price_type=None,
                    leverage_direction=None, leverage_multiple=None, next_key=None,
                    limit=None):
        """GET /api/v1.0/quote/plate-stock —— 板块成分股（锁定表 §C.2，pagination 并入）。

        ``sort_field/price_type/leverage_*`` 官方未给枚举/区间 → 标量直通（不发明）。
        """
        query = self._body({
            "plate_code": self._text(plate_code, "plate_code"),
            "sort_field": self._scalar(sort_field, "sort_field"),
            "ascend": self._scalar(ascend, "ascend"),
            "price_type": self._scalar(price_type, "price_type"),
            "leverage_direction": self._scalar(leverage_direction, "leverage_direction"),
            "leverage_multiple": self._scalar(leverage_multiple, "leverage_multiple"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._scalar(limit, "limit"),
        })
        d, pagination = self.client.request_meta("GET", paths.PLATE_STOCK_PATH, query=query)
        return self._merge_pagination(d, pagination)


class OpenApiShort(_RestValidators):
    """卖空数据方法组（锁定表 §C.6）：每日卖空成交与空头持仓。

    仅港股/美股可卖空证券（官方 ``-8``）；该条件**前缀本地可判定** → 前置拒绝。
    ``-10 no_data`` 是官方明示「无卖空数据」→ 空而非错（``{"no_data": True}``）。
    HK 是成交维度、US 是持仓维度（官方口径，字段不同——本层原样透传不归一）。
    """

    #: 支持卖空数据的市场（官方：仅 HK/US 可卖空证券）
    SHORT_MARKETS = frozenset({"HK", "US"})
    #: count 上限（官方默认 30、最大 90）
    COUNT_MAX = 90

    def __init__(self, client):
        self.client = client

    def _short_symbol(self, symbol):
        code = self._symbol(symbol)
        market = code.split(".", 1)[0]
        if market not in self.SHORT_MARKETS:
            raise ValueError(f"卖空数据仅支持 {'/'.join(sorted(self.SHORT_MARKETS))} "
                             f"可卖空证券（官方 -8），得到：{code}")
        return code

    def short_daily_volume(self, symbol, count=None):
        """GET /api/v1.0/quote/{symbol}/short/daily-volume —— 每日卖空成交（锁定表 §C.6）。"""
        code = self._short_symbol(symbol)
        query = self._body({"count": self._opt_int(count, 1, self.COUNT_MAX, "count")})
        return self._tolerate_no_data(lambda: self.client.request(
            "GET", paths.SHORT_DAILY_VOLUME_PATH.format(symbol=code), query=query))

    def short_interest(self, symbol, count=None):
        """GET /api/v1.0/quote/{symbol}/short/interest —— 空头持仓（锁定表 §C.6）。"""
        code = self._short_symbol(symbol)
        query = self._body({"count": self._opt_int(count, 1, self.COUNT_MAX, "count")})
        return self._tolerate_no_data(lambda: self.client.request(
            "GET", paths.SHORT_INTEREST_PATH.format(symbol=code), query=query))


class OpenApiBasicData(_RestValidators):
    """基础数据方法组（锁定表 §C.1）：经济日历、所属板块、复权因子。"""

    #: economic-calendar/hot 的 limit 区间（官方 1..20）
    CALENDAR_LIMIT_MAX = 20
    #: economic-calendar/search 的 search_type 枚举（官方 1..4）
    CALENDAR_SEARCH_TYPES = frozenset({1, 2, 3, 4})

    def __init__(self, client):
        self.client = client

    def economic_calendar_hot(self, limit=None, next_key=None, date=None, timezone=None):
        """GET /api/v1.0/quote/economic-calendar/hot —— 热门经济事件（锁定表 §C.1）。"""
        query = self._body({
            "limit": self._opt_int(limit, 1, self.CALENDAR_LIMIT_MAX, "limit"),
            "next_key": self._text(next_key, "next_key", required=False),
            "date": self._date(date, "date"),
            "timezone": self._scalar(timezone, "timezone"),
        })
        d, pagination = self.client.request_meta("GET", paths.ECONOMIC_CALENDAR_HOT_PATH,
                                                 query=query)
        return self._merge_pagination(d, pagination)

    def economic_calendar_search(self, keyword, search_type, limit=None,
                                 next_key=None, time_order_type=None):
        """GET /api/v1.0/quote/economic-calendar/search —— 经济事件搜索（锁定表 §C.1）。

        ``keyword`` 与 ``search_type`` 官方必填。``limit/time_order_type`` 官方未给区间
        → 标量直通（不发明）。
        """
        query = self._body({
            "keyword": self._text(keyword, "keyword"),
            "search_type": self._enum_in(search_type, self.CALENDAR_SEARCH_TYPES,
                                         "search_type"),
            "limit": self._scalar(limit, "limit"),
            "next_key": self._text(next_key, "next_key", required=False),
            "time_order_type": self._scalar(time_order_type, "time_order_type"),
        })
        d, pagination = self.client.request_meta("GET", paths.ECONOMIC_CALENDAR_SEARCH_PATH,
                                                 query=query)
        return self._merge_pagination(d, pagination)

    def owner_plate(self, symbol):
        """GET /api/v1.0/quote/{symbol}/owner-plate —— 标的所属板块（锁定表 §C.1）。

        行业中性化的数据来源；需按 ``plate_type`` 过滤行业类、剔除概念板块（调用方口径）。
        """
        return self.client.request("GET", paths.OWNER_PLATE_PATH.format(
            symbol=self._symbol_in_path(symbol)))

    def rehab(self, symbol, divi_mode=None):
        """GET /api/v1.0/quote/{symbol}/corporate-actions/rehab —— 复权因子（锁定表 §C.1）。

        **路径注意**：文档归属「基本数据」，REST 路径在 ``/corporate-actions/rehab``
        （锁定表 §C.1 的显式提醒）。``divi_mode`` 官方未列枚举 → 标量直通。
        """
        query = self._body({"divi_mode": self._scalar(divi_mode, "divi_mode")})
        return self.client.request("GET", paths.REHAB_PATH.format(
            symbol=self._symbol_in_path(symbol)), query=query)


class OpenApiIpo(_RestValidators):
    """IPO 方法组（锁定表 §C.4）：每市场独立路径，market 为**小写**路径片段。"""

    #: 官方支持的市场（路径片段小写；jp 不在列）
    IPO_MARKETS = frozenset({"hk", "us", "cn", "sg", "my"})

    def __init__(self, client):
        self.client = client

    def ipo_list(self, market, request_type=None):
        """GET /api/v1.0/quote/ipo-list/{market} —— 新股列表（锁定表 §C.4）。

        ``market`` 官方取值**小写**（hk/us/cn/sg/my），是**每市场独立路径**的一部分：
        本层按文档取值严格匹配（与 plate_class/group_type 同口径的大小写敏感纪律），
        不做大小写归一——路径片段与官方枚举逐字一致。

        ``request_type`` 省略时**不代填**：官方各市场默认值不同（HK/US/SG/MY=11、A 股=4），
        省略即由服务端按市场套用默认，本层代填反而会固化错误默认。
        """
        if not isinstance(market, str) or market not in self.IPO_MARKETS:
            raise ValueError(f"market 取值之一：{sorted(self.IPO_MARKETS)}"
                             f"（官方每市场独立路径，小写），得到：{market!r}")
        query = self._body({
            "request_type": self._opt_int(request_type, 1, 11, "request_type"),
        })
        return self.client.request("GET", paths.IPO_LIST_PATH.format(market=market), query=query)


class OpenApiWatchlist(_RestValidators):
    """自选方法组（锁定表 §C.8）：列表/分组只读 + 修改自选。

    ``-9``（用户身份缺失或无效）在三个方法上如实抛出且可读（本层带语义说明）。
    ``modify_user_security`` 是**写类**端点：实现在此，但**不进 MCP 工具面**
    （注册面由 WP12 任务 4 决定；规格 §7.2 的 HTTP-only 档）。
    """

    #: 分组类型枚举（官方大小写敏感）
    GROUP_TYPES = frozenset({"ALL", "CUSTOM", "SYSTEM"})
    #: group_name 官方长度上限
    GROUP_NAME_MAX = 100
    #: code_list 官方上限（修改自选）
    MODIFY_CODES_MAX = 200
    #: -9 的可读说明（锁定表 §C.8：用户身份缺失或无效）
    IDENTITY_NOTE = "用户身份缺失或无效（官方 errcode=-9）"

    def __init__(self, client):
        self.client = client

    def watchlist_list(self, group_name):
        """GET /api/v1.0/quote/user-security —— 自选股列表（锁定表 §C.8，group_name 必填）。"""
        query = {"group_name": self._text_max(group_name, "group_name",
                                              self.GROUP_NAME_MAX)}
        return self._permission_note(
            lambda: self.client.request("GET", paths.WATCHLIST_LIST_PATH, query=query),
            self.IDENTITY_NOTE)

    def watchlist_groups(self, group_type=None):
        """GET /api/v1.0/quote/user-security-group —— 自选分组（锁定表 §C.8）。"""
        query = self._body({
            "group_type": self._enum_in(group_type, self.GROUP_TYPES, "group_type",
                                        default=None),
        })
        return self._permission_note(
            lambda: self.client.request("GET", paths.WATCHLIST_GROUPS_PATH, query=query),
            self.IDENTITY_NOTE)

    def modify_user_security(self, op, code_list, group_name=None):
        """POST /api/v1.0/quote/modify-user-security —— 修改自选（锁定表 §C.8）。

        ``op`` 官方未列枚举（只说明非法值 -3）→ 非空字符串直通，不发明取值集合。
        """
        body = self._body({
            "op": self._text(op, "op"),
            "code_list": self._codes(code_list, self.MODIFY_CODES_MAX),
            "group_name": self._text_max(group_name, "group_name", self.GROUP_NAME_MAX,
                                         required=False),
        })
        return self._permission_note(
            lambda: self.client.request("POST", paths.MODIFY_USER_SECURITY_PATH, json_body=body),
            self.IDENTITY_NOTE)


class OpenApiDerivatives(_RestValidators):
    """衍生品方法组（锁定表 §C.7）：期货信息、相关期货、期权波动率与行权概率。"""

    #: 期货批量上限（官方 code_list ≤400）
    FUTURE_CODES_MAX = 400
    #: option-volatility 的 query_time_period 区间与 hv_time_period 区间（官方）
    QUERY_TIME_PERIOD_RANGE = (1, 5)
    HV_TIME_PERIOD_RANGE = (5, 250)
    #: option-exercise-probability 的 limit 区间（官方 1..1000）
    EXERCISE_LIMIT_RANGE = (1, 1000)
    #: -9 的可读说明（锁定表 §C.7/§D.3：无期权数据查询权限）
    PERMISSION_NOTE = "无期权数据查询权限（官方 errcode=-9）"

    def __init__(self, client):
        self.client = client

    def future_info(self, code_list):
        """POST /api/v1.0/quote/future-info —— 期货合约信息（锁定表 §C.7，批量 ≤400）。"""
        return self.client.request("POST", paths.FUTURE_INFO_PATH,
                                   json_body={"code_list": self._codes(
                                       code_list, self.FUTURE_CODES_MAX)})

    def reference_future(self, symbol):
        """GET /api/v1.0/quote/{symbol}/reference-future —— 相关期货（锁定表 §C.7）。"""
        return self.client.request("GET", paths.REFERENCE_FUTURE_PATH.format(
            symbol=self._symbol_in_path(symbol)))

    def option_volatility(self, symbol, query_time_period=None, hv_time_period=None):
        """GET /api/v1.0/quote/{symbol}/option-volatility —— 隐含/历史波动率（锁定表 §C.7）。

        ``symbol`` 必须是**期权合约**（传正股由服务端 -3 拒绝）。``-10`` 无可用波动率
        → 空而非错。
        """
        query = self._body({
            "query_time_period": self._opt_int(query_time_period,
                                               *self.QUERY_TIME_PERIOD_RANGE,
                                               name="query_time_period"),
            "hv_time_period": self._opt_int(hv_time_period, *self.HV_TIME_PERIOD_RANGE,
                                            name="hv_time_period"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(lambda: self.client.request(
            "GET", paths.OPTION_VOLATILITY_PATH.format(symbol=code), query=query))

    def option_exercise_probability(self, symbol, limit=None):
        """GET /api/v1.0/quote/{symbol}/option-exercise-probability —— 行权概率（§C.7）。

        该端点可能返回官方 ``-9``（用户无期权数据查询权限）：**如实抛出且可读**，
        绝不退化成空数据（锁定表 §D.3 的显式要求）。
        """
        query = self._body({
            "limit": self._opt_int(limit, *self.EXERCISE_LIMIT_RANGE, name="limit"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(lambda: self._permission_note(
            lambda: self.client.request(
                "GET", paths.OPTION_EXERCISE_PROBABILITY_PATH.format(symbol=code),
                query=query),
            self.PERMISSION_NOTE))


# ---------------------------------------------------------------------------
# WP12 任务 3：OpenApiF10 —— 个股深度数据方法组（锁定表 §C.5 的 26 项）
# ---------------------------------------------------------------------------
# 官方已把 llms.txt 的扁平 ``f10/*`` 重组为 7 个命名空间（financials / research /
# valuation / corporate-actions / shareholders / company / top-brokers）；路径与参数名
# **逐项对照锁定表 §C.5**（2026-09-16 逐页核对；llms.txt 的 ``/f10/*.md`` 已实测全部 404，
# 严禁按 llms.txt 猜路径）。纪律与任务 2 七组同构：
#   * 方法签名即参数白名单（未知关键字 Python 直接 TypeError）；枚举/区间/必填本地校验，
#     坏参数**零网络往返**；每个方法只做「校验 + 一次 REST + envelope 解析」，不做业务聚合；
#   * 锁定表**未给出区间/枚举**的字段一律 ``_scalar`` 直通，**不发明上限**——按行处理：
#     例 ``insider_holders.limit`` 表内给了 ≤30，而 ``institutional.limit`` 未给 → 后者直通；
#   * 响应**原样透传**：``pub_trading_day`` / ``period_text`` / ``announced_at`` 等字段不解释、
#     不归一、不丢弃（PIT 钥匙的语义由落库层决定，传输层只搬运）；
#   * ``-10 no_data`` **仅在锁定表显式列出该语义的 section 上**转空（``_tolerate_no_data``）；
#     未列出的 section 与 ``-9``/``-3`` 等一律如实抛出，不重试、不吞错；
#   * 列出 ``next_key`` 的 section 走 ``request_meta`` 并并入信封 ``pagination``
#     （锁定表 §A 全局约定：游标不透明、来自 ``pagination.next_key``），未列出者不伪造分页。
# 本地可判定的 ``-8``（锁定表明示市场集合）前置拒绝：``earnings_price_history`` 仅
# HK/US/SH/SZ；``top_brokers``/``top_brokers_history`` 仅港股。其余 ``-8``（非正股/
# 品类不支持）取决于证券类型，代码字符串本地不可判定 → 交服务端判定并如实抛出。
