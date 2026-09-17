"""WP8 任务 3 交易/订单/成交/账户方法组（路径来自 ..paths）。"""
from .. import paths
from ..validators import _RestValidators
class OpenApiTrade(_RestValidators):
    """富途交易 OpenAPI（REST）方法组：WP8 任务 3 的 OpenAPI 交易后端唯一入口。

    每个方法对应一个官方 REST 端点；参数名与官方文档一致（code/qty/price/side/
    order_type/time_in_force/session/aux_price/lot_type/remark/order_class/
    multi_leg_info/trd_market/page_flag/page_size/exchange/order_ids/...）。调用方必须
    先配好凭据（``~/.futu-openapi.json``，OAuth 或 AppKey），否则 client.request 抛
    ``OpenApiError``。参数白名单 = 方法签名（未知关键字 Python 直接 TypeError），
    枚举/区间/必填在下单前本地拒绝（ValueError），坏参数零网络往返。
    """

    #: side 枚举（naming-dictionary#trd-side；NONE=未知，不作为下单值）
    SIDES = frozenset({"BUY", "SELL", "SELL_SHORT", "BUY_BACK"})
    #: order_type 枚举（naming-dictionary#order-type；NONE=未知，不作为下单值）
    ORDER_TYPES = frozenset({"LIMIT", "MARKET", "AUCTION", "AUCTION_LIMIT", "STOP",
                             "STOP_LIMIT", "MARKET_IF_TOUCHED", "LIMIT_IF_TOUCHED"})
    #: 必须带触发价的订单类型（place-order 页：aux_price required when order type is ...）
    AUX_PRICE_ORDER_TYPES = frozenset({"STOP", "STOP_LIMIT", "MARKET_IF_TOUCHED",
                                       "LIMIT_IF_TOUCHED"})
    #: time_in_force 枚举（naming-dictionary#time-in-force；NONE=未知）
    TIME_IN_FORCE = frozenset({"DAY", "GTC"})
    #: 美股交易时段枚举（naming-dictionary#trading-session；NONE=未知）——仅美股适用
    SESSIONS = frozenset({"RTH", "RTH+Pre/Post-Mkt", "OVERNIGHT", "ALL_DAY"})
    #: lot_type 枚举（naming-dictionary#lot-type；仅港股适用）
    LOT_TYPES = frozenset({"ODD", "ROUND"})
    #: order_class 枚举（naming-dictionary#order-class）
    ORDER_CLASSES = frozenset({"NORMAL", "MLEG"})
    #: exchange 枚举（naming-dictionary#exchange）
    EXCHANGES = frozenset({"US", "SEHK", "SGX", "SSE", "SZSE", "JP", "CA", "CME",
                           "CBOT", "NYMEX", "COMEX", "CBOE", "HKFE", "KR"})
    #: trd_market 枚举（naming-dictionary#trd-market；NONE=未知）
    TRD_MARKETS = frozenset({"HK", "US", "SG", "HKCC", "CA", "FUTURES", "JP", "KR"})
    #: currency 枚举（naming-dictionary#currency；NONE=未知）
    CURRENCIES = frozenset({"HKD", "USD", "CNH", "JPY", "SGD", "KRW"})
    #: security_type 枚举（naming-dictionary#security-type）
    SECURITY_TYPES = frozenset({"STOCK", "OPTION", "FUTURES", "MULTILEG_OPTION"})
    #: option_strategy 枚举（naming-dictionary#option-strategy；拼写 CalenderSpread 为官方原文）
    OPTION_STRATEGIES = frozenset({
        "Covered", "VerticalSpread", "Straddle", "Strangle", "Collar", "Butterfly",
        "Condor", "IronButterfly", "IronCondor", "CalenderSpread", "DiagonalSpread",
        "Customize"})
    #: MultiLegInfo / OrderLegInfo 的字段白名单（naming-dictionary）
    MULTI_LEG_KEYS = ("option_strategy", "underlying_symbol", "underlying_stock_name",
                      "leg_infos")
    MULTI_LEG_REQUIRED = ("option_strategy", "underlying_symbol", "leg_infos")
    LEG_KEYS = ("leg_symbol", "leg_exchange", "leg_ratio_qty", "leg_side",
                "leg_security_type", "leg_stock_name", "leg_hp_multiplier",
                "leg_avg_fill_price")
    LEG_REQUIRED = ("leg_symbol", "leg_exchange", "leg_ratio_qty", "leg_side",
                    "leg_security_type")
    #: remark 的 UTF-8 字节上限（place-order 页：maximum length 64 bytes）
    REMARK_MAX_BYTES = 64
    #: order_ids 数量上限（order detail 页：length should be less than 50）
    MAX_ORDER_IDS = 49
    #: 分页 page_size 区间（orders/orders_history/order_fills: 10-100；
    #: fills_history: 10-50 —— 官方两页给的上界不同，各自钉死）
    PAGE_SIZE_RANGE = (10, 100)
    PAGE_SIZE_HISTORY_DEALS = (10, 50)

    # _SYMBOL_RE 由 _RestValidators 提供（WP12 任务 2 起共用一份实现）

    def __init__(self, client):
        self.client = client

    # _acc_id / _path_token / _symbol / _text 由 _RestValidators 提供（WP12 任务 2 起
    # 共用一份实现；_acc_id 于 WP13 审查次要项从本类与 OpenApiSimTrade 的重复定义
    # 提升到基类，单一实现）。
    # _micros / _page_flag / _page_size 亦已提升到基类（WP13 任务 2：模拟交易历史订单
    # 同口径使用，两处各写一份迟早会错位）。

    def _remark(self, value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("remark 必须是字符串")
        if len(value.encode("utf-8")) > self.REMARK_MAX_BYTES:
            raise ValueError(f"remark 的 UTF-8 长度不得超过 {self.REMARK_MAX_BYTES} 字节")
        return value

    def _multi_leg(self, value):
        """多腿信息内键白名单 + 必填校验（对象或对象列表都接受，原样透传）。"""
        if value is None:
            return None
        items = value if isinstance(value, list) else [value]
        if not items:
            raise ValueError("multi_leg_info 不能为空")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("multi_leg_info 必须是对象或对象列表")
            unknown = sorted(set(item) - set(self.MULTI_LEG_KEYS))
            if unknown:
                raise ValueError(f"multi_leg_info 含未支持字段：{unknown}")
            for field in self.MULTI_LEG_REQUIRED:
                if item.get(field) in (None, ""):
                    raise ValueError(f"multi_leg_info.{field} 必填")
            self._enum_in(item["option_strategy"], self.OPTION_STRATEGIES,
                          "multi_leg_info.option_strategy")
            legs = item["leg_infos"]
            if not isinstance(legs, list) or not legs:
                raise ValueError("multi_leg_info.leg_infos 必须是至少一条腿的列表")
            for leg in legs:
                if not isinstance(leg, dict):
                    raise ValueError("multi_leg_info.leg_infos 元素必须是对象")
                unknown = sorted(set(leg) - set(self.LEG_KEYS))
                if unknown:
                    raise ValueError(f"multi_leg_info.leg_infos 含未支持字段：{unknown}")
                for field in self.LEG_REQUIRED:
                    if leg.get(field) in (None, ""):
                        raise ValueError(f"multi_leg_info.leg_infos.{field} 必填")
                self._enum_in(leg["leg_exchange"], self.EXCHANGES,
                              "multi_leg_info.leg_infos.leg_exchange")
                self._enum_in(leg["leg_side"], self.SIDES,
                              "multi_leg_info.leg_infos.leg_side")
                self._enum_in(leg["leg_security_type"], self.SECURITY_TYPES,
                              "multi_leg_info.leg_infos.leg_security_type")
        return value

    # ------------------------------------------------------------ 交易（Trade）

    def place_order(self, acc_id, code, qty, side, order_type, time_in_force,
                    price=None, session=None, aux_price=None, lot_type=None,
                    remark=None, order_class=None, multi_leg_info=None):
        """POST /api/v1.0/accounts/{acc_id}/orders —— 下单（官方 place-order 页）。

        需要二次确认时信封抛 ``OrderConfirmRequired``（含 confirm_id/jump_url）：订单在
        券商侧**已挂起**，调用方须调 order_confirm 完成；**禁止对原请求重发**（会重复下单）。
        """
        body = self._body({
            "code": self._symbol(code),
            "qty": self._text(qty, "qty"),
            "side": self._enum_in(side, self.SIDES, "side"),
            "order_type": self._enum_in(order_type, self.ORDER_TYPES, "order_type"),
            "time_in_force": self._enum_in(time_in_force, self.TIME_IN_FORCE,
                                           "time_in_force"),
            "price": self._text(price, "price", required=False),
            "session": self._enum_in(session, self.SESSIONS, "session", default=None),
            "aux_price": self._text(aux_price, "aux_price", required=False),
            "lot_type": self._enum_in(lot_type, self.LOT_TYPES, "lot_type", default=None),
            "remark": self._remark(remark),
            "order_class": self._enum_in(order_class, self.ORDER_CLASSES,
                                         "order_class", default=None),
            "multi_leg_info": self._multi_leg(multi_leg_info),
        })
        if body["order_type"] in self.AUX_PRICE_ORDER_TYPES and "aux_price" not in body:
            raise ValueError(
                f"order_type={body['order_type']} 时 aux_price 必填（官方触发价规则）")
        if body.get("order_class") == "MLEG" and "multi_leg_info" not in body:
            raise ValueError("order_class=MLEG 时 multi_leg_info 必填（官方多腿订单规则）")
        return self.client.request("POST",
                                   paths.ACCOUNT_ORDERS_PATH.format(acc_id=self._acc_id(acc_id)),
                                   json_body=body)

    def modify_order(self, acc_id, order_id, exchange, qty, price, aux_price=None):
        """PUT /api/v1.0/accounts/{acc_id}/orders/{order_id} —— 改单（官方 modify-order 页）。

        官方明示**不支持改 A 股订单**（Does not support modifying A-share orders）：本层
        忠实透传不做市场预判，拒绝/回退策略由交易闸门决定（platform/server/trading.py）。
        """
        body = self._body({
            "exchange": self._enum_in(exchange, self.EXCHANGES, "exchange"),
            "qty": self._text(qty, "qty"),
            "price": self._text(price, "price"),
            "aux_price": self._text(aux_price, "aux_price", required=False),
        })
        return self.client.request(
            "PUT", paths.ACCOUNT_ORDER_DETAIL_PATH.format(
                acc_id=self._acc_id(acc_id),
                order_id=self._path_token(order_id, 'order_id')),
            json_body=body)

    def cancel_order(self, acc_id, order_id, exchange):
        """DELETE /api/v1.0/accounts/{acc_id}/orders/{order_id}?exchange=... —— 撤单。"""
        query = {"exchange": self._enum_in(exchange, self.EXCHANGES, "exchange")}
        return self.client.request(
            "DELETE", paths.ACCOUNT_ORDER_DETAIL_PATH.format(
                acc_id=self._acc_id(acc_id),
                order_id=self._path_token(order_id, 'order_id')),
            query=query)

    def order_confirm(self, acc_id, confirm_id):
        """POST /api/v1.0/accounts/{acc_id}/order_confirm —— 券商侧二次确认。

        下单/改单返回 ``need_order_confirm=true`` 后**唯一**的放行方式；成功后返回
        ``{"order_id": ...}``。对同一 confirm_id 重复调用由券商侧语义兜底（本层不重试）。
        """
        return self.client.request(
            "POST", paths.ACCOUNT_ORDER_CONFIRM_PATH.format(acc_id=self._acc_id(acc_id)),
            json_body={"confirm_id": self._text(confirm_id, "confirm_id")})

    def max_trade_qty(self, acc_id, code, order_type, price=None, order_id=None):
        """GET /api/v1.0/accounts/{acc_id}/acctradinginfo —— 最大可交易量。

        带 order_id 时查该订单的最大可改数量（官方要求两次查询间隔 > 0.5s——本层不睡眠、
        不重试，节奏由调用方控制）。
        """
        query = self._body({
            "code": self._symbol(code),
            "order_type": self._enum_in(order_type, self.ORDER_TYPES, "order_type"),
            "price": self._text(price, "price", required=False),
            "order_id": self._text(order_id, "order_id", required=False),
        })
        return self.client.request(
            "GET", paths.ACCOUNT_TRADING_INFO_PATH.format(acc_id=self._acc_id(acc_id)),
            query=query)

    # ------------------------------------------------------------ 订单（Order）

    def open_orders(self, acc_id, trd_market, page_flag="", page_size=None):
        """GET /api/v1.0/accounts/{acc_id}/orders —— 未完成订单（含最近 24h 已成交/已撤）。

        响应 ``d``：``{orders: [Order], page_flag: str, completed: bool}``。
        """
        query = self._body({
            "trd_market": self._enum_in(trd_market, self.TRD_MARKETS, "trd_market"),
            "page_flag": self._page_flag(page_flag),
            "page_size": self._page_size(page_size, self.PAGE_SIZE_RANGE),
        })
        return self.client.request(
            "GET", paths.ACCOUNT_ORDERS_PATH.format(acc_id=self._acc_id(acc_id)), query=query)

    def history_orders(self, acc_id, trd_market, page_flag="", code=None, start=None,
                       end=None, page_size=None):
        """GET /api/v1.0/accounts/{acc_id}/orders_history —— 历史订单。

        start/end 是**创建时间**的微秒时间戳；官方组合语义（0/0 → 近 90 天）由服务端处理，
        本层不补默认窗口（传什么是什么）。
        """
        query = self._body({
            "trd_market": self._enum_in(trd_market, self.TRD_MARKETS, "trd_market"),
            "page_flag": self._page_flag(page_flag),
            "code": self._symbol(code) if code is not None else None,
            "start": self._micros(start, "start"),
            "end": self._micros(end, "end"),
            "page_size": self._page_size(page_size, self.PAGE_SIZE_RANGE),
        })
        return self.client.request(
            "GET", paths.ACCOUNT_ORDERS_HISTORY_PATH.format(acc_id=self._acc_id(acc_id)),
            query=query)

    def order_details(self, acc_id, exchange, order_ids):
        """POST /api/v1.0/accounts/{acc_id}/orders/detail —— 订单详情（同 exchange 批量）。

        响应 ``d`` 是数组（Order 列表）。order_ids 少于 50 个（官方原文）。
        """
        if not isinstance(order_ids, list) or not 1 <= len(order_ids) <= self.MAX_ORDER_IDS:
            raise ValueError(f"order_ids 必须是 1..{self.MAX_ORDER_IDS} 个订单号的列表")
        ids = [self._path_token(order_id, "order_ids") for order_id in order_ids]
        body = {
            "exchange": self._enum_in(exchange, self.EXCHANGES, "exchange"),
            "order_ids": ids,
        }
        return self.client.request(
            "POST", paths.ACCOUNT_ORDERS_DETAIL_PATH.format(acc_id=self._acc_id(acc_id)),
            json_body=body)

    # ------------------------------------------------------------ 成交（Deal）

    def today_deals(self, acc_id, trd_market, page_flag="", page_size=None):
        """GET /api/v1.0/accounts/{acc_id}/order_fills —— 当日成交。

        响应 ``d``：``{order_fills: [OrderFill], page_flag: str, completed: bool}``。
        """
        query = self._body({
            "trd_market": self._enum_in(trd_market, self.TRD_MARKETS, "trd_market"),
            "page_flag": self._page_flag(page_flag),
            "page_size": self._page_size(page_size, self.PAGE_SIZE_RANGE),
        })
        return self.client.request(
            "GET", paths.ACCOUNT_ORDER_FILLS_PATH.format(acc_id=self._acc_id(acc_id)), query=query)

    def history_deals(self, acc_id, trd_market, page_flag="", code=None, start=None,
                      end=None, page_size=None):
        """GET /api/v1.0/accounts/{acc_id}/fills_history —— 历史成交。

        start/end 是**更新时间**的微秒时间戳（与 history_orders 的创建时间不同）；
        page_size 上界 50（官方 fills_history 页，与 orders 页的 100 不同）。
        """
        query = self._body({
            "trd_market": self._enum_in(trd_market, self.TRD_MARKETS, "trd_market"),
            "page_flag": self._page_flag(page_flag),
            "code": self._symbol(code) if code is not None else None,
            "start": self._micros(start, "start"),
            "end": self._micros(end, "end"),
            "page_size": self._page_size(page_size, self.PAGE_SIZE_HISTORY_DEALS),
        })
        return self.client.request(
            "GET", paths.ACCOUNT_FILLS_HISTORY_PATH.format(acc_id=self._acc_id(acc_id)),
            query=query)

    # ------------------------------------------------------------ 账户（Account）

    def authorized_accounts(self):
        """GET /api/v1.0/accounts/authorized_trd_accs —— 授权交易账户（无参数）。

        响应 ``d``：``{accounts: [Account]}``；Account 含 ``account_id`` / ``security_firm``
        / ``enable_market``（list[int]）/ ``acc_type`` 等。
        """
        return self.client.request("GET", paths.AUTHORIZED_ACCOUNTS_PATH)

    def account_funds(self, acc_id, currency=None):
        """GET /api/v1.0/accounts/{acc_id}/funds —— 账户资金（净资产/购买力等）。

        官方参数表把 ``currency`` 标成必填，但同页 curl 示例未传 → 本层按可选处理
        （登记见文件头差异清单）；该参数只对期货/综合证券账户生效。
        """
        query = self._body({
            "currency": self._enum_in(currency, self.CURRENCIES, "currency",
                                      default=None),
        })
        return self.client.request(
            "GET", paths.ACCOUNT_FUNDS_PATH.format(acc_id=self._acc_id(acc_id)), query=query)

    def positions(self, acc_id, code=None, pl_ratio_min=None, pl_ratio_max=None):
        """GET /api/v1.0/accounts/{acc_id}/positions —— 持仓（响应 ``d`` 是数组）。

        盈亏比例过滤（pl_ratio_min/pl_ratio_max）官方是字符串百分数（如 "10" 表示 ≥+10%）。
        """
        low = self._text(pl_ratio_min, "pl_ratio_min", required=False)
        high = self._text(pl_ratio_max, "pl_ratio_max", required=False)
        if low is not None and high is not None:
            try:
                low_value, high_value = float(low), float(high)
            except ValueError:
                raise ValueError("pl_ratio_min/pl_ratio_max 必须是数值字符串") from None
            if low_value > high_value:
                raise ValueError("pl_ratio_min 不能大于 pl_ratio_max")
        query = self._body({
            "code": self._symbol(code) if code is not None else None,
            "pl_ratio_min": low,
            "pl_ratio_max": high,
        })
        return self.client.request(
            "GET", paths.ACCOUNT_POSITIONS_PATH.format(acc_id=self._acc_id(acc_id)), query=query)


# ---------------------------------------------------------------------------
# WP12 任务 2：富途数据面传输方法组（筛选/板块/做空/基础数据/IPO/自选/衍生品）
# ---------------------------------------------------------------------------
# 路径、参数名、枚举、区间与错误码语义**逐项对照锁定表**
# `docs/superpowers/plans/wp12-endpoint-lock.md`（2026-09-16 官方文档逐页核对，53 条目标；
# llms.txt 的 F10 链接已实测 404，严禁按 llms.txt 猜路径）。纪律与既有方法组同构：
#   * 方法签名即参数白名单（未知关键字 Python 直接 TypeError）；
#   * 枚举/区间/结构本地校验（坏参数**零网络往返**）；
#   * 每个方法只做「校验 + 一次 REST 调用 + envelope 解析」，**不做业务聚合**；
#   * 锁定表未给出枚举/区间的字段**不自行发明**（`_scalar` 直通，服务端 -3/-5 如实抛出）；
#   * `-10 no_data` → 空而非错；`-9` → 如实抛出且可读（基类 `_tolerate_no_data`
#     / `_permission_note`）。
#
# 路径常量集中声明，并由文件末尾 WP12_TRANSPORT_ENDPOINTS 与锁定表绑定
# （tests/test_wp12_transport.py 逐条比对——防路径漂移与「猜路径」回流）。
