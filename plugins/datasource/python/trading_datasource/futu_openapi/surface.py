"""锁定面声明：方法组 (类, 方法) → (HTTP 方法, 路径常量)。

tests/test_wp12_transport.py 用 WP12_TRANSPORT_ENDPOINTS 绑定锁定表；
WP12_F10_ENDPOINTS 同理（详见该测试的 LockTableBindingTests）。
"""
from . import paths

WP12_F10_ENDPOINTS = {
    ("OpenApiF10", "earnings_price_move"): ("GET", paths.F10_EARNINGS_PRICE_MOVE_PATH),
    ("OpenApiF10", "earnings_price_history"): ("GET", paths.F10_EARNINGS_PRICE_HISTORY_PATH),
    ("OpenApiF10", "statements"): ("GET", paths.F10_STATEMENTS_PATH),
    ("OpenApiF10", "revenue_breakdown"): ("GET", paths.F10_REVENUE_BREAKDOWN_PATH),
    ("OpenApiF10", "analyst_consensus"): ("GET", paths.F10_ANALYST_CONSENSUS_PATH),
    ("OpenApiF10", "rating_summary"): ("GET", paths.F10_RATING_SUMMARY_PATH),
    ("OpenApiF10", "morningstar"): ("GET", paths.F10_MORNINGSTAR_PATH),
    ("OpenApiF10", "valuation_detail"): ("GET", paths.F10_VALUATION_DETAIL_PATH),
    ("OpenApiF10", "valuation_plate_stocks"): ("GET", paths.F10_VALUATION_PLATE_STOCKS_PATH),
    ("OpenApiF10", "valuation_index_stocks"): ("GET", paths.F10_VALUATION_INDEX_STOCKS_PATH),
    ("OpenApiF10", "valuation_index_stock_plates"): (
        "GET", paths.F10_VALUATION_INDEX_STOCK_PLATES_PATH),
    ("OpenApiF10", "dividends"): ("GET", paths.F10_DIVIDENDS_PATH),
    ("OpenApiF10", "buybacks"): ("GET", paths.F10_BUYBACKS_PATH),
    ("OpenApiF10", "splits"): ("GET", paths.F10_SPLITS_PATH),
    ("OpenApiF10", "shareholders_overview"): ("GET", paths.F10_SHAREHOLDERS_OVERVIEW_PATH),
    ("OpenApiF10", "holding_changes"): ("GET", paths.F10_HOLDING_CHANGES_PATH),
    ("OpenApiF10", "holder_detail"): ("GET", paths.F10_HOLDER_DETAIL_PATH),
    ("OpenApiF10", "institutional"): ("GET", paths.F10_INSTITUTIONAL_PATH),
    ("OpenApiF10", "insider_holders"): ("GET", paths.F10_INSIDER_HOLDERS_PATH),
    ("OpenApiF10", "insider_trades"): ("GET", paths.F10_INSIDER_TRADES_PATH),
    ("OpenApiF10", "company_profile"): ("GET", paths.F10_COMPANY_PROFILE_PATH),
    ("OpenApiF10", "company_executives"): ("GET", paths.F10_COMPANY_EXECUTIVES_PATH),
    ("OpenApiF10", "company_executive_background"): (
        "GET", paths.F10_COMPANY_EXECUTIVE_BACKGROUND_PATH),
    ("OpenApiF10", "company_operational_efficiency"): (
        "GET", paths.F10_COMPANY_OPERATIONAL_EFFICIENCY_PATH),
    ("OpenApiF10", "top_brokers"): ("GET", paths.F10_TOP_BROKERS_PATH),
    ("OpenApiF10", "top_brokers_history"): ("GET", paths.F10_TOP_BROKERS_HISTORY_PATH),
}


#: WP12 任务 2 数据面端点绑定面：(类名, 方法名) → (HTTP 方法, 路径模板)。
#: 锁定测试 ``tests/test_wp12_transport.py`` 逐条比对锁定表——**新增端点必须先入锁定表
#: 再入本表**（禁止未核对路径落地）。F10 26 项见 ``WP12_F10_ENDPOINTS``（任务 3）、
#: 模拟交易 9 项属任务 4/5。
WP12_TRANSPORT_ENDPOINTS = {
    ("OpenApiScreen", "stock_screen"): ("POST", paths.STOCK_SCREEN_PATH),
    ("OpenApiScreen", "warrant_screen"): ("POST", paths.WARRANT_SCREEN_PATH),
    ("OpenApiPlate", "plate_list"): ("GET", paths.PLATE_LIST_PATH),
    ("OpenApiPlate", "plate_stock"): ("GET", paths.PLATE_STOCK_PATH),
    ("OpenApiShort", "short_daily_volume"): ("GET", paths.SHORT_DAILY_VOLUME_PATH),
    ("OpenApiShort", "short_interest"): ("GET", paths.SHORT_INTEREST_PATH),
    ("OpenApiBasicData", "economic_calendar_hot"): ("GET", paths.ECONOMIC_CALENDAR_HOT_PATH),
    ("OpenApiBasicData", "economic_calendar_search"): ("GET",
                                                       paths.ECONOMIC_CALENDAR_SEARCH_PATH),
    ("OpenApiBasicData", "owner_plate"): ("GET", paths.OWNER_PLATE_PATH),
    ("OpenApiBasicData", "rehab"): ("GET", paths.REHAB_PATH),
    ("OpenApiIpo", "ipo_list"): ("GET", paths.IPO_LIST_PATH),
    ("OpenApiWatchlist", "watchlist_list"): ("GET", paths.WATCHLIST_LIST_PATH),
    ("OpenApiWatchlist", "watchlist_groups"): ("GET", paths.WATCHLIST_GROUPS_PATH),
    ("OpenApiWatchlist", "modify_user_security"): ("POST", paths.MODIFY_USER_SECURITY_PATH),
    ("OpenApiDerivatives", "future_info"): ("POST", paths.FUTURE_INFO_PATH),
    ("OpenApiDerivatives", "reference_future"): ("GET", paths.REFERENCE_FUTURE_PATH),
    ("OpenApiDerivatives", "option_volatility"): ("GET", paths.OPTION_VOLATILITY_PATH),
    ("OpenApiDerivatives", "option_exercise_probability"): (
        "GET", paths.OPTION_EXERCISE_PROBABILITY_PATH),
}


#: WP13 任务 2 模拟交易端点绑定面（锁定表 §C.9，9 项）：(类名, 方法名) → (HTTP 方法, 路径模板)。
#: 锁定测试 ``tests/test_wp13_simtrade.py`` 逐条比对锁定表；**新增/改路径必须先改锁定表**。
#: 前置实测结论（AppKey 凭据可用）与三条文档差异见 ``groups/simtrade.py`` 文件头。
WP13_SIM_TRADE_ENDPOINTS = {
    ("OpenApiSimTrade", "account_list"): ("GET", paths.SIM_TRADE_ACCOUNTS_PATH),
    ("OpenApiSimTrade", "cash_info"): ("GET", paths.SIM_TRADE_CASH_INFO_PATH),
    ("OpenApiSimTrade", "position_list"): ("GET", paths.SIM_TRADE_POSITIONS_PATH),
    ("OpenApiSimTrade", "order_list"): ("GET", paths.SIM_TRADE_ORDERS_PATH),
    ("OpenApiSimTrade", "history_order_list"): ("GET",
                                                paths.SIM_TRADE_HISTORY_ORDERS_PATH),
    ("OpenApiSimTrade", "max_buy_sell"): ("GET", paths.SIM_TRADE_MAX_BUY_SELL_PATH),
    ("OpenApiSimTrade", "input_order"): ("POST", paths.SIM_TRADE_ORDERS_PATH),
    ("OpenApiSimTrade", "modify_order"): ("POST", paths.SIM_TRADE_ORDER_MODIFY_PATH),
    ("OpenApiSimTrade", "cancel_order"): ("POST", paths.SIM_TRADE_ORDER_CANCEL_PATH),
}
