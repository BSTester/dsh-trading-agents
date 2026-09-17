"""WP12 任务 3 个股深度数据（F10）方法组：26 端点，响应原样透传。"""
import re

from .. import paths
from ..errors import OpenApiError
from ..validators import _RestValidators
class OpenApiF10(_RestValidators):
    """个股深度数据方法组（锁定表 §C.5，26 个 section）。

    统一分发入口 ``f10(symbol, section, **section_params)``：``section`` 是**枚举白名单**
    （未知 section 本地拒绝、零网络往返），分发到同名专用方法。

    ``symbol`` 的位置随 section 而变（锁定表逐行声明）：多数 section 是**路径片段**
    （``/quote/{symbol}/…``）；``valuation_plate_stocks`` / ``valuation_index_stocks`` /
    ``valuation_index_stock_plates`` 的 symbol 是**查询参数**（板块代码/指数代码，路径里
    没有 symbol）——本层按表放置，不做归一化猜测。
    """

    #: 26 个 section（锁定表 §C.5 逐行；未知取值本地拒绝）
    SECTIONS = frozenset({
        # 财务数据（4）
        "earnings_price_move", "earnings_price_history", "statements", "revenue_breakdown",
        # 研究（3）
        "analyst_consensus", "rating_summary", "morningstar",
        # 估值（4）
        "valuation_detail", "valuation_plate_stocks", "valuation_index_stocks",
        "valuation_index_stock_plates",
        # 公司行为（3）
        "dividends", "buybacks", "splits",
        # 股东持股（6）
        "shareholders_overview", "holding_changes", "holder_detail", "institutional",
        "insider_holders", "insider_trades",
        # 公司信息（4）
        "company_profile", "company_executives", "company_executive_background",
        "company_operational_efficiency",
        # 十大经纪商（2）
        "top_brokers", "top_brokers_history",
    })

    # ---- 逐行区间/枚举（锁定表 §C.5；表内未给出的字段一律不在此声明）----
    #: earnings-price-move 的 count：官方默认 10、最大 50
    COUNT_RANGE = (1, 50)
    #: overview_count：官方默认 8、最大 50（生效值 = min(overview_count, count)）
    OVERVIEW_COUNT_RANGE = (1, 50)
    #: statements 的 statement_type（官方 1~4）
    STATEMENT_TYPES = frozenset({1, 2, 3, 4})
    #: rating-summary 的 rating_dimension_type（官方 1 或 2）
    RATING_DIMENSION_TYPES = frozenset({1, 2})
    #: rating-summary 的 limit（官方 ≤20）
    RATING_LIMIT_RANGE = (1, 20)
    #: valuation-detail 的 valuation_type（官方 ∈[1,2,3]）
    VALUATION_TYPES = frozenset({1, 2, 3})
    #: valuation-detail 的 interval_type（官方 ∈[1..10]）
    INTERVAL_TYPE_RANGE = (1, 10)
    #: valuation/plate-stocks 的 limit（官方 1~50）
    PLATE_STOCKS_LIMIT_RANGE = (1, 50)
    #: buybacks 的 limit（官方 ≤50）
    BUYBACKS_LIMIT_RANGE = (1, 50)
    #: insider-holders 的 limit（官方 ≤30）
    INSIDER_HOLDERS_LIMIT_RANGE = (1, 30)
    #: insider-trades 的 limit（官方 ≤50）
    INSIDER_TRADES_LIMIT_RANGE = (1, 50)
    #: operational-efficiency 的 limit（官方 ≤100）与 financial_type（官方 ∈{7,102}）
    EFFICIENCY_LIMIT_RANGE = (1, 100)
    EFFICIENCY_FINANCIAL_TYPES = frozenset({7, 102})
    #: top-brokers-history 的 days_before（官方必填 1~365）
    DAYS_BEFORE_RANGE = (1, 365)
    #: earnings-price-history 支持的市场（官方 -8：市场不在 HK/US/SH/SZ）
    EARNINGS_HISTORY_MARKETS = frozenset({"HK", "US", "SH", "SZ"})
    #: 十大经纪商支持的市场（官方 -8：非港股）
    HK_ONLY_MARKETS = frozenset({"HK"})

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------ 共用助手

    def _in_markets(self, symbol, markets, name):
        """市场白名单守卫（**本地可判定的 -8**：官方明示市场集合时前置拒绝）。"""
        code = self._symbol_in_path(symbol)
        market = code.split(".", 1)[0]
        if market not in markets:
            raise ValueError(f"{name} 仅支持 {'/'.join(sorted(markets))}"
                             f"（官方 -8），得到：{code}")
        return code

    def _page(self, path, query):
        """带游标的 GET：信封 pagination 并入返回值（锁定表 §A 分页约定）。"""
        d, pagination = self.client.request_meta("GET", path, query=query)
        return self._merge_pagination(d, pagination)

    def _get(self, path):
        """无查询参数的 GET（不传 query，避免伪造空查询）。"""
        return self.client.request("GET", path)

    # ------------------------------------------------------------ 分发

    def f10(self, symbol, section, **section_params):
        """按 section 分发到专用方法（未知 section 本地拒绝，零网络往返）。"""
        if section not in self.SECTIONS:
            raise ValueError(f"section 取值非法：{section!r}"
                             f"（允许：{sorted(self.SECTIONS)}）")
        return getattr(self, section)(symbol, **section_params)

    # ------------------------------------------------------------ 财务数据（4）

    def earnings_price_move(self, symbol, count=None, overview_count=None):
        """GET …/financials/earnings-price-move —— 财报日股价变动（锁定表 §C.5）。"""
        query = self._body({
            "count": self._opt_int(count, *self.COUNT_RANGE, name="count"),
            "overview_count": self._opt_int(overview_count, *self.OVERVIEW_COUNT_RANGE,
                                            name="overview_count"),
        })
        return self.client.request("GET", paths.F10_EARNINGS_PRICE_MOVE_PATH.format(
            symbol=self._symbol_in_path(symbol)), query=query)

    def earnings_price_history(self, symbol):
        """GET …/financials/earnings-price-history —— 财报日历史（锁定表 §C.5）。

        ``-10``（无财报日股价）→ 空而非错；``-8`` 市场不在 HK/US/SH/SZ **本地可判定**
        → 前置拒绝。
        """
        code = self._in_markets(symbol, self.EARNINGS_HISTORY_MARKETS, "财报日股价")
        return self._tolerate_no_data(
            lambda: self._get(paths.F10_EARNINGS_PRICE_HISTORY_PATH.format(symbol=code)))

    def statements(self, symbol, statement_type=None, financial_type=None,
                   currency_code=None, next_key=None, limit=None):
        """GET …/financials/statements —— 财务报表（锁定表 §C.5）。

        ``statement_type`` 官方 1~4；``financial_type``/``currency_code``/``limit``
        表内**未给枚举或区间** → 标量直通（不发明）。``-10`` 该报表期无数据 → 空而非错。
        """
        query = self._body({
            "statement_type": self._enum_in(statement_type, self.STATEMENT_TYPES,
                                            "statement_type", default=None),
            "financial_type": self._scalar(financial_type, "financial_type"),
            "currency_code": self._scalar(currency_code, "currency_code"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._scalar(limit, "limit"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(paths.F10_STATEMENTS_PATH.format(symbol=code), query))

    def revenue_breakdown(self, symbol, date=None, financial_type=None,
                          currency_code=None):
        """GET …/financials/revenue-breakdown —— 营收构成（锁定表 §C.5）。

        ``date`` 官方 yyyy-MM-dd（本层含日历有效性校验）；``-10`` 无营收构成 → 空而非错。
        """
        query = self._body({
            "date": self._date(date, "date"),
            "financial_type": self._scalar(financial_type, "financial_type"),
            "currency_code": self._scalar(currency_code, "currency_code"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(lambda: self.client.request(
            "GET", paths.F10_REVENUE_BREAKDOWN_PATH.format(symbol=code), query=query))

    # ------------------------------------------------------------ 研究（3）

    def analyst_consensus(self, symbol):
        """GET …/research/analyst-consensus —— 分析师共识（锁定表 §C.5）。"""
        return self._get(paths.F10_ANALYST_CONSENSUS_PATH.format(
            symbol=self._symbol_in_path(symbol)))

    def rating_summary(self, symbol, rating_dimension_type=None, next_key=None,
                       limit=None):
        """GET …/research/rating-summary —— 评级汇总（锁定表 §C.5）。

        ``rating_dimension_type`` 官方 1 或 2；``limit`` 官方 ≤20。
        """
        query = self._body({
            "rating_dimension_type": self._enum_in(rating_dimension_type,
                                                   self.RATING_DIMENSION_TYPES,
                                                   "rating_dimension_type", default=None),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, *self.RATING_LIMIT_RANGE, name="limit"),
        })
        code = self._symbol_in_path(symbol)
        return self._page(paths.F10_RATING_SUMMARY_PATH.format(symbol=code), query)

    def morningstar(self, symbol):
        """GET …/research/morningstar —— 晨星评级（锁定表 §C.5）。

        ``-10``（晨星无覆盖）→ 空而非错。
        """
        return self._tolerate_no_data(
            lambda: self._get(paths.F10_MORNINGSTAR_PATH.format(
                symbol=self._symbol_in_path(symbol))))

    # ------------------------------------------------------------ 估值（4）

    def valuation_detail(self, symbol, valuation_type=None, interval_type=None):
        """GET …/valuation/detail —— 估值明细（锁定表 §C.5，两参数均官方枚举）。"""
        query = self._body({
            "valuation_type": self._enum_in(valuation_type, self.VALUATION_TYPES,
                                            "valuation_type", default=None),
            "interval_type": self._opt_int(interval_type, *self.INTERVAL_TYPE_RANGE,
                                           name="interval_type"),
        })
        return self.client.request("GET", paths.F10_VALUATION_DETAIL_PATH.format(
            symbol=self._symbol_in_path(symbol)), query=query)

    def valuation_plate_stocks(self, symbol, valuation_type=None, next_key=None,
                               limit=None, sort_type=None, sort_id=None):
        """GET /quote/valuation/plate-stocks —— 板块估值成分股（锁定表 §C.5）。

        **``symbol`` 是查询参数（板块代码）**，路径无 symbol。``valuation_type``/
        ``sort_type``/``sort_id`` 表内本行未给区间 → 标量直通；``limit`` 官方 1~50；
        ``-10`` 无数据 → 空而非错。
        """
        query = self._body({
            "symbol": self._symbol(symbol),
            "valuation_type": self._scalar(valuation_type, "valuation_type"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, *self.PLATE_STOCKS_LIMIT_RANGE, name="limit"),
            "sort_type": self._scalar(sort_type, "sort_type"),
            "sort_id": self._scalar(sort_id, "sort_id"),
        })
        return self._tolerate_no_data(
            lambda: self._page(paths.F10_VALUATION_PLATE_STOCKS_PATH, query))

    def valuation_index_stocks(self, symbol, valuation_type=None, next_key=None,
                               limit=None, sort_type=None, sort_id=None,
                               filter_security=None):
        """GET /quote/valuation/index-stocks —— 指数成分股估值（锁定表 §C.5，⭐llms.txt 漏列）。

        **``symbol`` 是查询参数（指数代码）**；``limit`` 表内本行未给区间 → 标量直通；
        ``-10`` 无数据 → 空而非错。
        """
        query = self._body({
            "symbol": self._symbol(symbol),
            "valuation_type": self._scalar(valuation_type, "valuation_type"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._scalar(limit, "limit"),
            "sort_type": self._scalar(sort_type, "sort_type"),
            "sort_id": self._scalar(sort_id, "sort_id"),
            "filter_security": self._scalar(filter_security, "filter_security"),
        })
        return self._tolerate_no_data(
            lambda: self._page(paths.F10_VALUATION_INDEX_STOCKS_PATH, query))

    def valuation_index_stock_plates(self, symbol):
        """GET /quote/valuation/index-stock-plates —— 指数所属板块（锁定表 §C.5，⭐漏列）。

        **``symbol`` 是查询参数（指数代码）**；官方本行未给错误码表 → 按通用码处理
        （不把 ``-10`` 吞成空——表内未列该语义）。
        """
        query = {"symbol": self._symbol(symbol)}
        return self.client.request("GET", paths.F10_VALUATION_INDEX_STOCK_PLATES_PATH,
                                   query=query)

    # ------------------------------------------------------------ 公司行为（3）

    def dividends(self, symbol):
        """GET …/corporate-actions/dividends —— 分红派息（锁定表 §C.5，0 成功含空列表）。"""
        return self._get(paths.F10_DIVIDENDS_PATH.format(
            symbol=self._symbol_in_path(symbol)))

    def buybacks(self, symbol, next_key=None, limit=None):
        """GET …/corporate-actions/buybacks —— 回购（锁定表 §C.5，``limit`` ≤50）。"""
        query = self._body({
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, *self.BUYBACKS_LIMIT_RANGE, name="limit"),
        })
        code = self._symbol_in_path(symbol)
        return self._page(paths.F10_BUYBACKS_PATH.format(symbol=code), query)

    def splits(self, symbol):
        """GET …/corporate-actions/splits —— 拆合股（锁定表 §C.5，0 成功含空列表）。"""
        return self._get(paths.F10_SPLITS_PATH.format(symbol=self._symbol_in_path(symbol)))

    # ------------------------------------------------------------ 股东持股（6）

    def shareholders_overview(self, symbol, period_id=None):
        """GET …/shareholders/overview —— 股东概况（锁定表 §C.5）。"""
        query = self._body({"period_id": self._scalar(period_id, "period_id")})
        return self.client.request("GET", paths.F10_SHAREHOLDERS_OVERVIEW_PATH.format(
            symbol=self._symbol_in_path(symbol)), query=query)

    def holding_changes(self, symbol, limit=None, next_key=None, sort_column=None,
                        sort_type=None, filter_type=None, holder_category=None):
        """GET …/shareholders/holding-changes —— 持股变动（锁定表 §C.5）。

        本行参数表内**均未给区间/枚举** → 全部标量直通（不发明）；``-10`` 无持股变动
        → 空而非错。
        """
        query = self._body({
            "limit": self._scalar(limit, "limit"),
            "next_key": self._text(next_key, "next_key", required=False),
            "sort_column": self._scalar(sort_column, "sort_column"),
            "sort_type": self._scalar(sort_type, "sort_type"),
            "filter_type": self._scalar(filter_type, "filter_type"),
            "holder_category": self._scalar(holder_category, "holder_category"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(paths.F10_HOLDING_CHANGES_PATH.format(symbol=code), query))

    def holder_detail(self, symbol, request_type=None, period_id=None, holder_id=None,
                      sort_column=None, sort_type=None, limit=None, next_key=None):
        """GET …/shareholders/holder-detail —— 股东明细（锁定表 §C.5；``-10`` → 空）。"""
        query = self._body({
            "request_type": self._scalar(request_type, "request_type"),
            "period_id": self._scalar(period_id, "period_id"),
            "holder_id": self._scalar(holder_id, "holder_id"),
            "sort_column": self._scalar(sort_column, "sort_column"),
            "sort_type": self._scalar(sort_type, "sort_type"),
            "limit": self._scalar(limit, "limit"),
            "next_key": self._text(next_key, "next_key", required=False),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(paths.F10_HOLDER_DETAIL_PATH.format(symbol=code), query))

    def institutional(self, symbol, limit=None, next_key=None):
        """GET …/shareholders/institutional —— 机构持股（锁定表 §C.5）。

        本行 ``limit`` 表内**未给上限** → 标量直通（与 insider-holders 的 ≤30 逐行区分）。
        """
        query = self._body({
            "limit": self._scalar(limit, "limit"),
            "next_key": self._text(next_key, "next_key", required=False),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(paths.F10_INSTITUTIONAL_PATH.format(symbol=code), query))

    def insider_holders(self, symbol, next_key=None, limit=None):
        """GET …/shareholders/insider-holders —— 内部持股人（锁定表 §C.5，``limit`` ≤30）。"""
        query = self._body({
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, *self.INSIDER_HOLDERS_LIMIT_RANGE, name="limit"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(paths.F10_INSIDER_HOLDERS_PATH.format(symbol=code), query))

    def insider_trades(self, symbol, next_key=None, limit=None, holder_id=None):
        """GET …/shareholders/insider-trades —— 内部交易（锁定表 §C.5，``limit`` ≤50）。"""
        query = self._body({
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, *self.INSIDER_TRADES_LIMIT_RANGE, name="limit"),
            "holder_id": self._scalar(holder_id, "holder_id"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(paths.F10_INSIDER_TRADES_PATH.format(symbol=code), query))

    # ------------------------------------------------------------ 公司信息（4）

    def company_profile(self, symbol):
        """GET …/company/profile —— 公司简介（锁定表 §C.5；``-10`` 无资料 → 空）。"""
        return self._tolerate_no_data(lambda: self._get(
            paths.F10_COMPANY_PROFILE_PATH.format(symbol=self._symbol_in_path(symbol))))

    def company_executives(self, symbol):
        """GET …/company/executives —— 公司高管（锁定表 §C.5；``-10`` 无数据 → 空）。"""
        return self._tolerate_no_data(lambda: self._get(
            paths.F10_COMPANY_EXECUTIVES_PATH.format(symbol=self._symbol_in_path(symbol))))

    def company_executive_background(self, symbol, leader_name):
        """GET …/company/executive-background —— 高管背景（锁定表 §C.5，``leader_name`` 必填）。"""
        query = {"leader_name": self._text(leader_name, "leader_name")}
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(lambda: self.client.request(
            "GET", paths.F10_COMPANY_EXECUTIVE_BACKGROUND_PATH.format(symbol=code),
            query=query))

    def company_operational_efficiency(self, symbol, limit=None, financial_type=None,
                                       currency_code=None, next_key=None):
        """GET …/company/operational-efficiency —— 运营效率（锁定表 §C.5）。

        ``limit`` 官方 ≤100；``financial_type`` 官方 ∈{7,102}；``-10`` 无数据 → 空。
        """
        query = self._body({
            "limit": self._opt_int(limit, *self.EFFICIENCY_LIMIT_RANGE, name="limit"),
            "financial_type": self._enum_in(financial_type,
                                            self.EFFICIENCY_FINANCIAL_TYPES,
                                            "financial_type", default=None),
            "currency_code": self._scalar(currency_code, "currency_code"),
            "next_key": self._text(next_key, "next_key", required=False),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(lambda: self._page(
            paths.F10_COMPANY_OPERATIONAL_EFFICIENCY_PATH.format(symbol=code), query))

    # ------------------------------------------------------------ 十大经纪商（2）

    def top_brokers(self, symbol, date=None):
        """GET …/quote/{symbol}/top-brokers —— 十大经纪商（锁定表 §C.5，**仅港股**）。

        ``date`` 官方 yyyy-MM-dd、缺省取最新；非港股官方 ``-8`` **本地可判定** → 前置拒绝。
        路径以锁定表为准（``/quote/{symbol}/top-brokers``；文档目录 ``top-brokers/`` 是
        文档站路径，**不是** REST 路径——两者不一致，禁止按文档目录推断）。
        """
        code = self._in_markets(symbol, self.HK_ONLY_MARKETS, "十大经纪商")
        query = self._body({"date": self._date(date, "date")})
        return self.client.request("GET", paths.F10_TOP_BROKERS_PATH.format(symbol=code),
                                   query=query)

    def top_brokers_history(self, symbol, days_before):
        """GET …/top-brokers-history —— 十大经纪商历史（锁定表 §C.5，**仅港股**）。

        ``days_before`` 官方**必填 1~365**；非港股官方 ``-8`` 本地可判定 → 前置拒绝。
        """
        code = self._in_markets(symbol, self.HK_ONLY_MARKETS, "十大经纪商历史")
        query = {"days_before": self._int_in(days_before, *self.DAYS_BEFORE_RANGE,
                                             name="days_before")}
        return self.client.request("GET", paths.F10_TOP_BROKERS_HISTORY_PATH.format(symbol=code),
                                   query=query)


#: WP12 任务 3 F10 端点绑定面：(类名, 方法名) → (HTTP 方法, 路径模板)。
#: 锁定测试 ``tests/test_wp12_f10_transport.py`` 逐条比对锁定表 §C.5——**新增端点必须先入
#: 锁定表再入本表**（禁止未核对路径落地）。
