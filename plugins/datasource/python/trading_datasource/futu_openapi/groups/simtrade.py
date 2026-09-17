"""WP13 任务 2 模拟交易方法组（锁定表 §C.9；路径来自 ..paths）。

**鉴权实测结论（2026-09-16，本机 AppKey 凭据，先于实现，不可跳过）**：

锁定表 §C.9 事前登记的风险是「官方文档写明 ``uid`` 由**登录态 header** 自动透传，而本仓库
凭据是 AppKey 签名 / OAuth 服务端凭据，兼容性未验证」。实测结果：**可用**。

  * ``GET /api/v1.0/sim-trade/accounts`` → ``ret_code=0``，返回 9 个模拟账户
    （``9393`` 港股 ``market_id=1``、``3182575`` A 股 ``market_id=3``、``11587526`` 美股
    融资融券 ``market_id=100``，另有港股期权 ``9``、期货 ``10/11/12/13``、日股 ``16``）；
  * ``GET .../3182575/cash-info`` → ``{balance, mv, total_asset, ...}``（字段名
    ``total_asset``**无 s**，与 live ``get-funds`` 的 ``total_assets`` 是两个端点）；
  * ``GET .../3182575/positions?market=3`` → 真实持仓（``positions[]``）；
  * ``GET .../3182575/orders?market=3`` → ``{orders: []}``；
  * ``GET .../3182575/history-orders?market=3&time_begin=<微秒>&time_end=<微秒>`` →
    真实历史订单（``create_time`` 亦为微秒，如 ``1789351326000000``）；
  * ``GET .../3182575/max-buy-sell?market=3&symbol=603993&order_type=1&price=16.80`` →
    ``{max_cash_buy_qty_round_lot, max_sell_qty_round_lot, ...}``。

**实测与官方页面的差异（四条，已在 docs/TOOL-LIMITS.md 登记）**：

  1. ``orders``（今日订单）、``history-orders``、``max-buy-sell`` 官方页面参数表**未列
     ``market``**，实测**必填**——缺参报 ``missing required parameter: market``；
  2. ``history-orders`` 的 ``time_begin``/``time_end`` 官方标注「int（微秒）」，
     实测传 ``YYYY-MM-DD`` 字符串报 ``parameter 'time_begin' must be an integer``；
  3. ``input_order`` 的 ``qty``/``price`` 官方类型是 **string**（示例 ``"100"``/``"400"``），
     与 MCP 通道的 int/float 不同形——翻译在 ``channel.sim_call`` 一处完成。
  4. **``cancel_order`` 与 ``modify_order`` 实测要求 body 携带 ``market``**
     （官方 cancel 页写「无请求体」）：缺参报 ``missing required field in body: market``。
     写路径真机验证（2026-09-16 09:35 CST，A 股模拟账户 3182575）：
     ``input_order`` 挂单 100 股 603993 @16.90 成功（``order_id=7147945``，当日订单可见），
     随后经本方法组撤单成功。``market`` 是账户列表的 ``market_id``（int），不是交易所前缀
     字符串（示例 ``{"market":1,...}``）；链名 → 数字的归一由
     ``trading_datasource.market_ids.sim_market_id`` 负责，本层只收 int。

本层只做「参数校验 + 一次 REST + 信封解析」，**不做业务聚合**（与其他方法组同纪律）；
模拟交易官方**无错误码表**，故不编造错误码映射——错误由 ``client.request`` 按信封如实抛出。
"""
from .. import paths
from ..validators import _RestValidators


class OpenApiSimTrade(_RestValidators):
    """模拟交易方法组：账户 / 资金 / 持仓 / 订单 / 下单 / 改单 / 撤单 / 最大买卖量。"""

    #: 模拟下单的 order_type 枚举（官方 input-order 页：1=限价 3=市价；比 live 八种窄）
    ORDER_TYPES = frozenset({1, 3})
    #: 模拟下单的 order_side 枚举（官方 input-order 页：1=买 2=卖 3=卖空 4=买回）
    ORDER_SIDES = frozenset({1, 2, 3, 4})
    #: text（备注）UTF-8 字节上限（官方 input-order 页：≤100 字节）
    TEXT_MAX_BYTES = 100
    #: market（market_id）本地防呆区间：官方注册表未给上界，只挡明显非法值，
    #: 真实枚举交给后端判定（不编造市场枚举——实测已见 1/3/9/10/11/12/13/16/100）
    MARKET_RANGE = (1, 1000)
    #: page_size 本地防呆区间：官方给默认 50、**未给上界**；超界本地拒绝而非发给后端
    PAGE_SIZE_BOUNDS = (1, 1000)

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------ 校验助手（模拟交易专有）

    def _acc_id(self, acc_id):
        """acc_id 进路径：非空字符串且不含路径分隔符/空白。"""
        return self._path_token(acc_id, "acc_id")

    def _market(self, value, name="market"):
        """market_id：int（或数字字符串）。返回 int；None → None（缺参由后端如实报）。"""
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"{name} 必须是整数 market_id（账户列表字段），收到 bool")
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        return self._int_in(value, self.MARKET_RANGE[0], self.MARKET_RANGE[1], name)

    def _code(self, value):
        """证券代码（裸代码，如 ``600089``/``00700``）。

        **不做前缀归一**：调用方契约是裸代码（``trading_core.broker.place`` 已
        ``symbol.lstrip("SH.ZBJ.")``）；此处重复一遍归一就是第二份实现，反而会掩盖
        调用方口径漂移。只挡明显非法（空/含空白/含路径分隔符）。
        """
        if not isinstance(value, str) or not value.strip():
            raise ValueError("symbol 必须是非空字符串（裸代码，如 600089）")
        text = value.strip()
        if any(char in text for char in "/?#") or any(char.isspace() for char in text):
            raise ValueError(f"symbol 含非法字符（不得含 / ? # 或空白）：{text[:40]!r}")
        return text

    def _text_bytes(self, value, name, max_bytes, required=False):
        """text/备注：UTF-8 字节数上限（官方按字节，不是字符）。"""
        text = self._text(value, name, required=required)
        if text is not None and len(text.encode("utf-8")) > max_bytes:
            raise ValueError(f"{name} 的 UTF-8 字节数不得超过 {max_bytes}：{text[:40]!r}")
        return text

    # ------------------------------------------------------------ 读端点

    def account_list(self):
        """GET /api/v1.0/sim-trade/accounts —— 模拟账户列表（无入参）。

        响应 ``d``：``{accounts: [{account_id, broker_id, market_id, account_type,
        account_title, ...}]}``。``market_id`` 就是后续各端点的 ``market``。
        """
        return self.client.request("GET", paths.SIM_TRADE_ACCOUNTS_PATH)

    def cash_info(self, acc_id):
        """GET /api/v1.0/sim-trade/{acc_id}/cash-info —— 账户资金。

        响应 ``d``：``{balance, hold, max_power_long, total_asset, mv, long_mv,
        short_mv, unrealized_profit, realized_profit}``（``total_asset`` 无 s）。
        """
        return self.client.request(
            "GET", paths.SIM_TRADE_CASH_INFO_PATH.format(acc_id=self._acc_id(acc_id)))

    def position_list(self, acc_id, market=None):
        """GET /api/v1.0/sim-trade/{acc_id}/positions —— 持仓列表。

        ``market`` 官方标选填，但**实测不带会报业务错**（A 股账户实测：
        ``OpenApiError backend business error``；带 ``market=3`` 即返回持仓）——
        故调用方应始终带上（core 侧本就以 market_id 调用）。
        响应 ``d``：``{positions: [{symbol, stock_name, qty, qty_avbl, cost_price, ...}]}``。
        """
        query = self._body({"market": self._market(market)})
        return self.client.request(
            "GET", paths.SIM_TRADE_POSITIONS_PATH.format(acc_id=self._acc_id(acc_id)),
            query=query)

    def order_list(self, acc_id, market=None):
        """GET /api/v1.0/sim-trade/{acc_id}/orders —— 今日订单。

        ``market`` 官方页面未列，**实测必填**（缺参报 ``missing required parameter:
        market``）。响应 ``d``：``{orders: [{order_id, side, symbol, order_type, status,
        qty, cum_qty, price, avg_fill_price, create_time, update_time, text, ...}]}``。
        """
        query = self._body({"market": self._market(market)})
        return self.client.request(
            "GET", paths.SIM_TRADE_ORDERS_PATH.format(acc_id=self._acc_id(acc_id)),
            query=query)

    def history_order_list(self, acc_id, market=None, time_begin=None, time_end=None,
                           page_size=None, next_key=None):
        """GET /api/v1.0/sim-trade/{acc_id}/history-orders —— 历史订单（时间范围 + 分页）。

        ``time_begin``/``time_end`` 是**微秒** int（官方单位；实测传日期字符串被拒）。
        响应 ``d``：``{orders: [...], pagination: {has_more, next_key}}``。
        """
        query = self._body({
            "market": self._market(market),
            "time_begin": self._micros(time_begin, "time_begin"),
            "time_end": self._micros(time_end, "time_end"),
            "page_size": self._page_size(page_size, self.PAGE_SIZE_BOUNDS),
            "next_key": self._text(next_key, "next_key", required=False),
        })
        return self.client.request(
            "GET", paths.SIM_TRADE_HISTORY_ORDERS_PATH.format(acc_id=self._acc_id(acc_id)),
            query=query)

    def max_buy_sell(self, acc_id, symbol, order_type, market=None, price=None,
                     order_id=None):
        """GET /api/v1.0/sim-trade/{acc_id}/max-buy-sell —— 最大可买可卖。

        官方页面只列 ``symbol``/``order_type``/``price``/``order_id``，**实测 ``market``
        必填**。响应 ``d``：``{max_cash_buy_qty_round_lot, max_margin_buy_qty_round_lot,
        max_sell_qty_round_lot, max_sell_short_qty, max_buy_back_qty, required_im_long,
        required_im_short}``（数量为字符串）。
        """
        query = self._body({
            "market": self._market(market),
            "symbol": self._code(symbol),
            "order_type": self._enum_in(order_type, self.ORDER_TYPES, "order_type"),
            "price": self._text(price, "price", required=False),
            "order_id": self._text(order_id, "order_id", required=False),
        })
        return self.client.request(
            "GET", paths.SIM_TRADE_MAX_BUY_SELL_PATH.format(acc_id=self._acc_id(acc_id)),
            query=query)

    # ------------------------------------------------------------ 写端点

    def input_order(self, acc_id, market, symbol, order_type, order_side, qty,
                    price=None, text=None):
        """POST /api/v1.0/sim-trade/{acc_id}/orders —— 模拟下单（限价/市价）。

        ``market`` 必填且是 **int market_id**（账户列表字段）；``qty``/``price`` 官方类型
        是 string（内部转字符串）；``order_type`` 1=限价 3=市价、``order_side``
        1=买 2=卖 3=卖空 4=买回。响应 ``d``：``{order_id}``。

        **能力边界不在这里放宽**：sim 只支持限价当日单的仓库存量口径由
        ``platform/server/trading.py`` 的 ``_sim_options_guard`` 把关（本层是传输层，
        按官方字段全集收参，不做业务取舍——放宽/收紧都在闸门层）。
        """
        body = self._body({
            "market": self._market(market),
            "symbol": self._code(symbol),
            "order_type": self._enum_in(order_type, self.ORDER_TYPES, "order_type"),
            "order_side": self._enum_in(order_side, self.ORDER_SIDES, "order_side"),
            "qty": self._text(qty, "qty"),
            "price": self._text(price, "price", required=False),
            "text": self._text_bytes(text, "text", self.TEXT_MAX_BYTES),
        })
        if body.get("market") is None:
            raise ValueError("market 必填（模拟账户列表的 market_id）")
        return self.client.request(
            "POST", paths.SIM_TRADE_ORDERS_PATH.format(acc_id=self._acc_id(acc_id)),
            json_body=body)

    def modify_order(self, acc_id, order_id, new_qty, new_price=None, market=None):
        """POST /api/v1.0/sim-trade/{acc_id}/orders/{order_id}/modify —— 原生改单。

        **``market`` 实测必填**（body，官方页面未列）：缺参报
        ``missing required field in body: market``（与撤单端点同款，2026-09-16 真机实测）。

        **仓库现状不使用本方法**：TOOL-LIMITS 记录 MCP ``sim_trade_modify_order``
        间歇性 ``-5``，故 sim 改单一律「撤旧重下」（``core_broker`` 注释与
        ``FutuBroker.modify``）。本方法按实测字段实现以保持 API 面完整；是否切换为原生
        改单取决于真机实测（见 docs/TOOL-LIMITS.md「模拟交易 REST」行的待办）。
        """
        body = self._body({
            "market": self._market(market),
            "new_qty": self._text(new_qty, "new_qty", required=False),
            "new_price": self._text(new_price, "new_price", required=False),
        })
        if body.get("market") is None:
            raise ValueError("market 必填（模拟账户列表的 market_id；实测后端要求 body 携带）")
        return self.client.request(
            "POST",
            paths.SIM_TRADE_ORDER_MODIFY_PATH.format(
                acc_id=self._acc_id(acc_id), order_id=self._path_token(order_id, "order_id")),
            json_body=body)

    def cancel_order(self, acc_id, order_id, market=None):
        """POST /api/v1.0/sim-trade/{acc_id}/orders/{order_id}/cancel —— 撤单。

        **``market`` 实测必填**（body，官方页面未列且示例无请求体）：缺参报
        ``missing required field in body: market``（2026-09-16 真机实测，见
        docs/TOOL-LIMITS.md）。调用方（``core_broker.cancel``）本就传 market，
        故这里是「按实测收紧」而非新增调用方负担。
        """
        body = self._body({"market": self._market(market)})
        if body.get("market") is None:
            raise ValueError("market 必填（模拟账户列表的 market_id；实测后端要求 body 携带）")
        return self.client.request(
            "POST",
            paths.SIM_TRADE_ORDER_CANCEL_PATH.format(
                acc_id=self._acc_id(acc_id), order_id=self._path_token(order_id, "order_id")),
            json_body=body)
