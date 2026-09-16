"""WP8 任务 3：交易链路统一至富途 OpenAPI（下单/改单/撤单/二次确认合一/订单/成交/账户）。

全部离线：OpenApiTrade 的 client 与闸门的 broker 后端都用替身（零网络）。覆盖：

  * OpenApiTrade 13 方法的官方路径/参数逐项断言（HTTP 方法 + path + query + body），
    含 8 种 order_type、4 种 side、美股 session（含 ``RTH+Pre/Post-Mkt``）、多腿
    ``multi_leg_info``（对象与对象列表、内键白名单、MLEG 必填）、触发价必填规则、
    分页区间（orders 10..100 / fills_history 10..50）、``order_ids`` <50、枚举拒绝、
    坏参数零网络往返；
  * 两层确认合一：``need_order_confirm`` → 人工批准（业务确认）之后由 broker 自动
    ``order_confirm``，**下单只发一次**；confirm 失败 / 信封缺 confirm_id / 传输异常
    → ``unknown`` 且**绝不重发下单**；业务错误信封 → ``rejected`` 且**不发 confirm**；
  * 改单策略：非 A 股走官方原生 ``PUT`` 改单（不再撤旧重下）；A 股如实拒绝（官方
    modify-order 页明示不支持）；撤单的 exchange 映射（SH→SSE / HK→SEHK / US→US）；
    北交所（官方交换枚举无 BJ）如实拒绝；
  * 闸门集成（TradeGate + 真实 store_access 语义的假确认）：live 批准 → 券商下单一次
    + 自动 confirm 一次 + OMS submitted；业务确认拒绝 → broker 零调用；
  * 未配置 OpenAPI（默认 mcp 通道 / channel=openapi 但无凭据）：live 写在**业务确认
    之前**拒绝（trading/broker-unavailable），确认零调用、broker 零触达、不落 OMS/风控行
    —— WP7 行为零回归；sim 路径始终委托 WP7 FutuBroker（与通道无关）；
  * 6 个新只读端点/工具：channel=openapi+live → REST 映射（逐账户/逐市场分发）；
    mcp 通道、sim 模式、缺凭据 → ``trading/openapi-unavailable``（指引授权）；
    参数白名单（HTTP 层拒未知字段、非法 mode → invalid-operation）；实时直通不进缓存；
  * account_positions/orders/funds 在 channel=openapi 时切 REST（MCP/sim 路径不变）；
  * 工具面 56 + 端点清单 52 + 工具字段集 ≡ app.OPENAPI_TRADE_FIELDS ≡ 端点名单锁定，
    且 mcp_tools 的 order_type/trd_market/exchange 取值域 ≡ OpenApiTrade 的枚举常量。

官方文档记录（2026-09-16 web_fetch 实抓 .md 原文，前缀 ``/api/v1.0``）：

  下单        POST   /accounts/{acc_id}/orders           body {code! qty! side! order_type!
                                                          time_in_force! price? session?
                                                          aux_price? lot_type? remark?
                                                          order_class? multi_leg_info?}
  改单        PUT    /accounts/{acc_id}/orders/{order_id} body {exchange! qty! price! aux_price?}
  撤单        DELETE /accounts/{acc_id}/orders/{order_id} query {exchange!}
  二次确认    POST   /accounts/{acc_id}/order_confirm     body {confirm_id!}
  最大可交易量 GET    /accounts/{acc_id}/acctradinginfo   query {code! order_type! price? order_id?}
  未完成订单  GET    /accounts/{acc_id}/orders            query {trd_market! page_flag! page_size? 10..100}
  历史订单    GET    /accounts/{acc_id}/orders_history    query {trd_market! page_flag! code?
                                                          start? end? page_size? 10..100}
  订单详情    POST   /accounts/{acc_id}/orders/detail     body {exchange! order_ids!（<50）}
  当日成交    GET    /accounts/{acc_id}/order_fills       query {trd_market! page_flag! page_size? 10..100}
  历史成交    GET    /accounts/{acc_id}/fills_history     query {trd_market! page_flag! code?
                                                          start? end? page_size? 10..50}
  授权账户    GET    /accounts/authorized_trd_accs        （无参数）
  账户资金    GET    /accounts/{acc_id}/funds             query {currency?}
  持仓        GET    /accounts/{acc_id}/positions         query {code? pl_ratio_min? pl_ratio_max?}

枚举（naming-dictionary 原文）：side{BUY,SELL,SELL_SHORT,BUY_BACK}；order_type{LIMIT,MARKET,
AUCTION,AUCTION_LIMIT,STOP,STOP_LIMIT,MARKET_IF_TOUCHED,LIMIT_IF_TOUCHED}；time_in_force{DAY,
GTC}；session{RTH,RTH+Pre/Post-Mkt,OVERNIGHT,ALL_DAY}（仅美股）；lot_type{ODD,ROUND}（仅港股）；
order_class{NORMAL,MLEG}；trd_market{...}；exchange{US,SEHK,SGX,SSE,SZSE,JP,CA,CME,CBOT,
NYMEX,COMEX,CBOE,HKFE,KR}（**无北交所**）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import get_args
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from server import app as app_module  # noqa: E402
from server import caches, mcp_tools, store_access, trading  # noqa: E402
from trading_core import store as core_store  # noqa: E402
from trading_datasource.futu_openapi import (  # noqa: E402
    OpenApiError, OpenApiTrade, OrderConfirmRequired, TransportError)

ORDER = {"symbol": "US.AAPL", "side": "BUY", "qty": 100, "price": 150.5}
A_SHARE_ORDER = {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 123.5}
RISK_CONFIG = {"risk_per_trade": 0.01, "max_positions": 5, "max_position_pct": 0.25,
               "daily_loss_limit_pct": 0.03}

#: 授权账户替身：LIVE-1 只开美股（2），LIVE-2 开港股 + A 股（1/4）
DEFAULT_ACCOUNTS = {"accounts": [
    {"account_id": "LIVE-1", "enable_market": [2], "acc_type": "margin"},
    {"account_id": "LIVE-2", "enable_market": [1, 4], "acc_type": "cash"},
]}


def fixed_ctx(**over):
    """注入用 risk ctx：全部合法默认（交易日 True），equity 足以放下示例订单。"""
    def builder(conn, mode, order, operation, home, today):
        ctx = {"mode": mode, "kill_path": str(Path(home) / "trading-kill"),
               "equity": 10_000_000.0, "positions_value": {}, "positions_count": 0,
               "day_pnl_pct": 0.0, "is_trading_day": True, "config": dict(RISK_CONFIG),
               "plan_hash": None, "plan_status": "approved"}
        ctx.update(over)
        return ctx
    return builder


class RecordingClient:
    """OpenApiClient 替身：记录 ``(method, path, query, json_body)``，按序回放预设响应。

    ``responses`` 的元素是返回值或 Exception（抛出）；用尽后回 ``d``（缺省空 dict）。
    """

    def __init__(self, responses=None, d=None):
        self.calls = []
        self.responses = list(responses or [])
        self.d = {} if d is None else d

    def request(self, method, path, query=None, json_body=None):
        self.calls.append((method, path, query, json_body))
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return self.d


def make_trade(responses=None, d=None):
    client = RecordingClient(responses=responses, d=d)
    return OpenApiTrade(client), client


class FakeTradeBackend:
    """OpenApiTrade 替身（broker 层）：按方法名记录 ``(name, kwargs)``，回放预设值/异常。

    plan 的值可以是返回值、Exception（抛出）或 ``callable(**kwargs)``（动态返回）。
    ``authorized_accounts`` 未登记时返回 DEFAULT_ACCOUNTS。
    """

    def __init__(self, **plan):
        self.calls = []
        self.plan = plan

    def _call(self, name, **kwargs):
        self.calls.append((name, kwargs))
        value = self.plan.get(name)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(**kwargs)
        return {} if value is None else value

    def names(self):
        return [name for name, _ in self.calls]

    def count(self, name):
        return self.names().count(name)

    def authorized_accounts(self):
        self.calls.append(("authorized_accounts", {}))
        value = self.plan.get("authorized_accounts")
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return DEFAULT_ACCOUNTS if value is None else value

    def place_order(self, **kwargs):
        return self._call("place_order", **kwargs)

    def modify_order(self, **kwargs):
        return self._call("modify_order", **kwargs)

    def cancel_order(self, **kwargs):
        return self._call("cancel_order", **kwargs)

    def order_confirm(self, **kwargs):
        return self._call("order_confirm", **kwargs)

    def max_trade_qty(self, acc_id, code, order_type, price=None, order_id=None):
        return self._call("max_trade_qty", acc_id=acc_id, code=code,
                          order_type=order_type, price=price, order_id=order_id)

    def open_orders(self, acc_id, trd_market, page_flag="", page_size=None):
        return self._call("open_orders", acc_id=acc_id, trd_market=trd_market,
                          page_flag=page_flag, page_size=page_size)

    def history_orders(self, acc_id, trd_market, page_flag="", code=None, start=None,
                       end=None, page_size=None):
        return self._call("history_orders", acc_id=acc_id, trd_market=trd_market,
                          page_flag=page_flag, code=code, start=start, end=end,
                          page_size=page_size)

    def order_details(self, acc_id, exchange, order_ids):
        return self._call("order_details", acc_id=acc_id, exchange=exchange,
                          order_ids=order_ids)

    def today_deals(self, acc_id, trd_market, page_flag="", page_size=None):
        return self._call("today_deals", acc_id=acc_id, trd_market=trd_market,
                          page_flag=page_flag, page_size=page_size)

    def history_deals(self, acc_id, trd_market, page_flag="", code=None, start=None,
                      end=None, page_size=None):
        return self._call("history_deals", acc_id=acc_id, trd_market=trd_market,
                          page_flag=page_flag, code=code, start=start, end=end,
                          page_size=page_size)

    def account_funds(self, acc_id, currency=None):
        return self._call("account_funds", acc_id=acc_id, currency=currency)

    def positions(self, acc_id, code=None, pl_ratio_min=None, pl_ratio_max=None):
        return self._call("positions", acc_id=acc_id, code=code,
                          pl_ratio_min=pl_ratio_min, pl_ratio_max=pl_ratio_max)


class RecordingLegacy:
    """FutuBroker 替身：记录委托调用（验证 sim/mcp 路径原样委托）。"""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "submitted", "broker_order_id": "SIM-1"}

    def _record(self, name, *args):
        self.calls.append((name, args))
        return dict(self.result)

    def place(self, order, mode):
        return self._record("place", order, mode)

    def modify(self, order, mode):
        return self._record("modify", order, mode)

    def cancel(self, order, mode):
        return self._record("cancel", order, mode)

    def positions(self, mode):
        return self._record("positions", mode)

    def orders(self, mode):
        return self._record("orders", mode)

    def funds(self, mode):
        return self._record("funds", mode)


class FakeConfirm:
    """假确认：request 直接返回预定结论（绝不阻塞）。"""

    def __init__(self, outcome=None):
        self.requests = []
        self.outcome = outcome or {"decision": "approved", "id": "c1",
                                   "reason": "用户在工作台确认"}

    def request(self, home, tool, mode, args, session_id, ttl_ms):
        self.requests.append({"tool": tool, "mode": mode, "args": args,
                              "session_id": session_id, "ttl_ms": ttl_ms})
        return dict(self.outcome)

    def view(self, home):  # pragma: no cover —— 假件不读视图
        return None

    def decide(self, home, confirmation_id, decision):  # pragma: no cover
        raise AssertionError("FakeConfirm 不提供 decide")


# ---------------------------------------------------------------------------
# 一、OpenApiTrade：官方路径/参数逐项（任务 A）
# ---------------------------------------------------------------------------
class OpenApiTradeDocTest(unittest.TestCase):
    """13 个方法的 (HTTP 方法, path, query, body) 逐项对照官方文档。"""

    def test_place_order_posts_official_body(self):
        trade, client = make_trade()
        trade.place_order("123", "US.AAPL", 100, "BUY", "LIMIT", "DAY", price=150.5,
                          session="RTH", lot_type="ROUND", remark="r-1")
        self.assertEqual(client.calls, [(
            "POST", "/api/v1.0/accounts/123/orders", None,
            {"code": "US.AAPL", "qty": "100", "side": "BUY", "order_type": "LIMIT",
             "time_in_force": "DAY", "price": "150.5", "session": "RTH",
             "lot_type": "ROUND", "remark": "r-1"})])

    def test_all_eight_order_types_and_four_sides(self):
        """8 种 order_type × 4 种 side 都能构造出合法请求体（触发价按官方规则带上）。"""
        self.assertEqual(len(OpenApiTrade.ORDER_TYPES), 8)
        self.assertEqual(len(OpenApiTrade.SIDES), 4)
        for order_type in sorted(OpenApiTrade.ORDER_TYPES):
            with self.subTest(order_type=order_type):
                trade, client = make_trade()
                kwargs = {"aux_price": 9.5} \
                    if order_type in OpenApiTrade.AUX_PRICE_ORDER_TYPES else {}
                trade.place_order("1", "US.AAPL", "10", "BUY", order_type, "DAY",
                                  price=1.5, **kwargs)
                body = client.calls[0][3]
                self.assertEqual(body["order_type"], order_type)
        for side in sorted(OpenApiTrade.SIDES):
            with self.subTest(side=side):
                trade, client = make_trade()
                trade.place_order("1", "US.AAPL", 10, side, "MARKET", "DAY")
                self.assertEqual(client.calls[0][3]["side"], side)
                self.assertNotIn("price", client.calls[0][3])

    def test_us_sessions_and_lot_type_and_order_class(self):
        """美股 session 四枚举（含 ``RTH+Pre/Post-Mkt`` 的斜杠值）与 order_class/lot_type。"""
        for session in sorted(OpenApiTrade.SESSIONS):
            with self.subTest(session=session):
                trade, client = make_trade()
                trade.place_order("1", "US.AAPL", 10, "BUY", "LIMIT", "DAY",
                                  price=1, session=session, lot_type="ODD",
                                  order_class="NORMAL")
                body = client.calls[0][3]
                self.assertEqual(body["session"], session)
                self.assertEqual(body["lot_type"], "ODD")
                self.assertEqual(body["order_class"], "NORMAL")

    def test_multi_leg_info_object_and_list(self):
        """多腿：对象与对象列表都透传，内键按 naming-dictionary 白名单校验。"""
        leg = {"leg_symbol": "US.AAPL250926C235000", "leg_exchange": "US",
               "leg_ratio_qty": "1", "leg_side": "BUY", "leg_security_type": "OPTION"}
        multi = {"option_strategy": "Straddle", "underlying_symbol": "AAPL",
                 "leg_infos": [leg]}
        for value, expected in ((multi, multi), ([multi], [multi])):
            with self.subTest(value=value):
                trade, client = make_trade()
                trade.place_order("1", "US.AAPL", 1, "BUY", "LIMIT", "DAY", price=1,
                                  order_class="MLEG", multi_leg_info=value)
                self.assertEqual(client.calls[0][3]["multi_leg_info"], expected)
                self.assertEqual(client.calls[0][3]["order_class"], "MLEG")

    def test_place_order_official_constraints_rejected_locally(self):
        """官方规则本地化：触发价必填、MLEG 必带多腿、枚举/内键非法即拒（零网络往返）。"""
        cases = [
            ({"order_type": "STOP_LIMIT"}, "aux_price"),
            ({"order_type": "MARKET_IF_TOUCHED"}, "aux_price"),
            ({"order_class": "MLEG"}, "multi_leg_info"),
        ]
        for over, needle in cases:
            with self.subTest(over=over):
                trade, client = make_trade()
                kwargs = {"acc_id": "1", "code": "US.AAPL", "qty": 1, "side": "BUY",
                          "order_type": "LIMIT", "time_in_force": "DAY", "price": 1}
                kwargs.update(over)
                with self.assertRaises(ValueError) as caught:
                    trade.place_order(**kwargs)
                self.assertIn(needle, str(caught.exception))
                self.assertEqual(client.calls, [])
        bad = [
            {"side": "HOLD"},
            {"order_type": "NONE"},
            {"time_in_force": "IOC"},
            {"session": "PRE"},
            {"lot_type": "HALF"},
            {"order_class": "BRACKET"},
            {"remark": "x" * 65},
            {"multi_leg_info": {"option_strategy": "Nope", "underlying_symbol": "AAPL",
                                "leg_infos": [{"leg_symbol": "US.AAPL", "leg_exchange": "US",
                                               "leg_ratio_qty": "1", "leg_side": "BUY",
                                               "leg_security_type": "OPTION"}]}},
            {"multi_leg_info": {"option_strategy": "Straddle", "underlying_symbol": "AAPL",
                                "leg_infos": [{"leg_symbol": "US.AAPL", "leg_exchange": "US",
                                               "leg_ratio_qty": "1", "leg_side": "BUY",
                                               "leg_security_type": "OPTION",
                                               "leg_typo": 1}]}},
        ]
        for over in bad:
            with self.subTest(over=over):
                trade, client = make_trade()
                kwargs = {"acc_id": "1", "code": "US.AAPL", "qty": 1, "side": "BUY",
                          "order_type": "LIMIT", "time_in_force": "DAY", "price": 1}
                kwargs.update(over)
                with self.assertRaises(ValueError):
                    trade.place_order(**kwargs)
                self.assertEqual(client.calls, [])

    def test_modify_order_puts_exchange_qty_price(self):
        trade, client = make_trade()
        trade.modify_order("123", "O/1" if False else "O-1", "SEHK", 200, 155.25,
                           aux_price=150)
        self.assertEqual(client.calls, [(
            "PUT", "/api/v1.0/accounts/123/orders/O-1", None,
            {"exchange": "SEHK", "qty": "200", "price": "155.25", "aux_price": "150"})])

    def test_cancel_order_deletes_with_exchange_query(self):
        trade, client = make_trade()
        trade.cancel_order("123", "O-1", "US")
        self.assertEqual(client.calls,
                         [("DELETE", "/api/v1.0/accounts/123/orders/O-1",
                           {"exchange": "US"}, None)])

    def test_order_confirm_posts_confirm_id(self):
        trade, client = make_trade()
        trade.order_confirm("123", "CF-9")
        self.assertEqual(client.calls, [("POST", "/api/v1.0/accounts/123/order_confirm",
                                         None, {"confirm_id": "CF-9"})])

    def test_max_trade_qty_gets_official_query(self):
        trade, client = make_trade()
        trade.max_trade_qty("123", "US.AAPL", "LIMIT", price=150.5, order_id="O-1")
        self.assertEqual(client.calls, [(
            "GET", "/api/v1.0/accounts/123/acctradinginfo",
            {"code": "US.AAPL", "order_type": "LIMIT", "price": "150.5",
             "order_id": "O-1"}, None)])

    def test_open_orders_gets_market_page_flag_and_size(self):
        trade, client = make_trade()
        trade.open_orders("123", "US", page_flag="", page_size=100)
        self.assertEqual(client.calls, [(
            "GET", "/api/v1.0/accounts/123/orders",
            {"trd_market": "US", "page_flag": "", "page_size": 100}, None)])
        self.assertEqual(OpenApiTrade.PAGE_SIZE_RANGE, (10, 100))
        for bad in (9, 101, True, "50"):
            with self.subTest(page_size=bad):
                trade, client = make_trade()
                with self.assertRaises(ValueError):
                    trade.open_orders("123", "US", page_size=bad)
                self.assertEqual(client.calls, [])

    def test_history_orders_micros_and_code_filter(self):
        trade, client = make_trade()
        trade.history_orders("123", "HK", page_flag="PG", code="hk.00700",
                             start=1700000000000000, end=1800000000000000, page_size=50)
        self.assertEqual(client.calls, [(
            "GET", "/api/v1.0/accounts/123/orders_history",
            {"trd_market": "HK", "page_flag": "PG", "code": "HK.00700",
             "start": 1700000000000000, "end": 1800000000000000, "page_size": 50}, None)])
        trade, client = make_trade()
        with self.assertRaises(ValueError):  # 微秒时间戳必须非负整数
            trade.history_orders("123", "HK", start=-1)
        with self.assertRaises(ValueError):
            trade.history_orders("123", "HK", end=1.5)

    def test_order_details_posts_exchange_and_ids_under_50(self):
        trade, client = make_trade()
        trade.order_details("123", "US", ["O-1", "O-2"])
        self.assertEqual(client.calls, [(
            "POST", "/api/v1.0/accounts/123/orders/detail", None,
            {"exchange": "US", "order_ids": ["O-1", "O-2"]})])
        with self.assertRaises(ValueError) as caught:
            make_trade()[0].order_details("123", "US", [f"O-{i}" for i in range(50)])
        self.assertIn("49", str(caught.exception))

    def test_today_deals_and_history_deals_ranges(self):
        trade, client = make_trade()
        trade.today_deals("123", "US", page_size=10)
        self.assertEqual(client.calls, [(
            "GET", "/api/v1.0/accounts/123/order_fills",
            {"trd_market": "US", "page_flag": "", "page_size": 10}, None)])
        trade, client = make_trade()
        trade.history_deals("123", "US", code="US.AAPL", start=1, end=2, page_size=50)
        self.assertEqual(client.calls, [(
            "GET", "/api/v1.0/accounts/123/fills_history",
            {"trd_market": "US", "page_flag": "", "code": "US.AAPL", "start": 1,
             "end": 2, "page_size": 50}, None)])
        # 官方 fills_history 上界 50（与 orders 页的 100 不同）
        self.assertEqual(OpenApiTrade.PAGE_SIZE_HISTORY_DEALS, (10, 50))
        with self.assertRaises(ValueError):
            make_trade()[0].history_deals("123", "US", page_size=51)

    def test_authorized_accounts_has_no_parameters(self):
        trade, client = make_trade(d={"accounts": []})
        self.assertEqual(trade.authorized_accounts(), {"accounts": []})
        self.assertEqual(client.calls,
                         [("GET", "/api/v1.0/accounts/authorized_trd_accs", None, None)])

    def test_account_funds_currency_optional(self):
        trade, client = make_trade()
        trade.account_funds("123")
        trade.account_funds("123", currency="USD")
        self.assertEqual(client.calls[0], ("GET", "/api/v1.0/accounts/123/funds", {}, None))
        self.assertEqual(client.calls[1], ("GET", "/api/v1.0/accounts/123/funds",
                                           {"currency": "USD"}, None))
        with self.assertRaises(ValueError):
            make_trade()[0].account_funds("123", currency="EUR")

    def test_positions_filters_and_shape(self):
        trade, client = make_trade(d=[{"code": "US.AAPL"}])
        self.assertEqual(trade.positions("123"), [{"code": "US.AAPL"}])
        self.assertEqual(client.calls, [("GET", "/api/v1.0/accounts/123/positions",
                                         {}, None)])
        trade, client = make_trade()
        trade.positions("123", code="us.aapl", pl_ratio_min="-10", pl_ratio_max="20")
        self.assertEqual(client.calls[0][2], {"code": "US.AAPL", "pl_ratio_min": "-10",
                                              "pl_ratio_max": "20"})
        with self.assertRaises(ValueError):
            make_trade()[0].positions("123", pl_ratio_min="20", pl_ratio_max="10")

    def test_acc_id_and_order_id_path_injection_rejected(self):
        for acc_id in ("", "1/2", "1 2", None, 3):
            with self.subTest(acc_id=acc_id):
                trade, client = make_trade()
                with self.assertRaises(ValueError):
                    trade.cancel_order(acc_id, "O-1", "US")
                self.assertEqual(client.calls, [])
        with self.assertRaises(ValueError):
            make_trade()[0].cancel_order("123", "O/1", "US")

    def test_envelope_error_classes_are_shared_with_order_confirm_flag(self):
        """need_order_confirm 的信封在 client 层就是 OrderConfirmRequired（含 confirm_id）。"""
        need = OrderConfirmRequired("需要确认", errcode=-1200, need_order_confirm=True,
                                    confirm_id="CF-1", jump_url="https://j")
        trade, _client = make_trade(responses=[need])
        with self.assertRaises(OrderConfirmRequired) as caught:
            trade.place_order("1", "US.AAPL", 1, "BUY", "LIMIT", "DAY", price=1)
        self.assertEqual(caught.exception.confirm_id, "CF-1")
        self.assertEqual(caught.exception.jump_url, "https://j")


# ---------------------------------------------------------------------------
# 二、OpenApiBroker：两层确认合一 / 改单策略 / 只读分发（任务 B）
# ---------------------------------------------------------------------------
class OpenApiBrokerWriteTest(unittest.TestCase):
    def setUp(self):
        self.need = OrderConfirmRequired("需要确认", errcode=-1200,
                                         need_order_confirm=True, confirm_id="CF-1")

    def test_need_confirm_is_auto_confirmed_after_approval_and_places_once(self):
        """批准后自动 order_confirm：下单恰好一次，confirm 恰好一次，返回 submitted。"""
        backend = FakeTradeBackend(place_order=self.need,
                                   order_confirm={"order_id": "O-NEW"})
        broker = trading.OpenApiBroker(trade=backend)
        self.assertTrue(broker.supports_live_write)
        out = broker.place(dict(ORDER), "live")
        self.assertEqual(out["status"], "submitted")
        self.assertTrue(out["confirmed"])
        self.assertEqual(out["broker_order_id"], "O-NEW")
        self.assertEqual(out["acc_id"], "LIVE-1")
        self.assertEqual(backend.count("place_order"), 1, "下单只发一次")
        self.assertEqual(backend.count("order_confirm"), 1)
        self.assertEqual(backend.calls[-1], ("order_confirm",
                                             {"acc_id": "LIVE-1", "confirm_id": "CF-1"}))

    def test_confirm_failure_is_honest_failure_and_never_re_places(self):
        """confirm 失败（业务/传输）→ unknown + 只发一次下单（绝不重发）。"""
        for error in (OpenApiError("confirm 已过期", errcode=-1), TransportError("超时")):
            with self.subTest(error=type(error).__name__):
                backend = FakeTradeBackend(place_order=self.need, order_confirm=error)
                broker = trading.OpenApiBroker(trade=backend)
                out = broker.place(dict(ORDER), "live")
                self.assertEqual(out["status"], "unknown")
                self.assertIn("勿重发下单", out["err"])
                self.assertEqual(backend.count("place_order"), 1)
                self.assertEqual(backend.count("order_confirm"), 1)

    def test_confirm_id_missing_is_unknown_without_confirm_call(self):
        backend = FakeTradeBackend(place_order=OrderConfirmRequired(
            "需要确认", errcode=-1200, need_order_confirm=True))
        out = trading.OpenApiBroker(trade=backend).place(dict(ORDER), "live")
        self.assertEqual(out["status"], "unknown")
        self.assertIn("confirm_id", out["err"])
        self.assertNotIn("order_confirm", backend.names())
        self.assertEqual(backend.count("place_order"), 1)

    def test_business_rejection_never_calls_confirm(self):
        backend = FakeTradeBackend(place_order=OpenApiError("资金不足", errcode=-2000))
        out = trading.OpenApiBroker(trade=backend).place(dict(ORDER), "live")
        self.assertEqual(out["status"], "rejected")
        self.assertIn("资金不足", out["err"])
        self.assertNotIn("order_confirm", backend.names())

    def test_transport_error_on_place_is_unknown(self):
        backend = FakeTradeBackend(place_order=TransportError("连接重置"))
        out = trading.OpenApiBroker(trade=backend).place(dict(ORDER), "live")
        self.assertEqual(out["status"], "unknown")
        self.assertIn("先查询，不重放", out["err"])
        self.assertEqual(backend.count("place_order"), 1)

    def test_place_uses_limit_day_and_gate_params(self):
        backend = FakeTradeBackend(place_order={"order_id": "O-1"})
        trading.OpenApiBroker(trade=backend).place(dict(ORDER), "live")
        self.assertEqual(backend.calls[1], ("place_order", {
            "acc_id": "LIVE-1", "code": "US.AAPL", "qty": 100, "price": 150.5,
            "side": "BUY", "order_type": "LIMIT", "time_in_force": "DAY"}))

    def test_unknown_market_prefix_raises_value_error(self):
        backend = FakeTradeBackend(place_order={"order_id": "O-1"})
        broker = trading.OpenApiBroker(trade=backend)
        with self.assertRaises(ValueError):
            broker.place({"symbol": "XX.AAPL", "side": "BUY", "qty": 1, "price": 1}, "live")
        self.assertEqual(backend.names(), [])

    def test_modify_uses_native_put_for_non_a_share(self):
        """改单策略：官方原生改单支持良好 → 直接 PUT，不再走「撤旧重下」。"""
        backend = FakeTradeBackend(modify_order={})
        broker = trading.OpenApiBroker(trade=backend)
        out = broker.modify({"order_id": "O-1", **ORDER}, "live")
        self.assertEqual(out["status"], "submitted")
        self.assertEqual(out["broker_order_id"], "O-1")
        self.assertEqual(backend.count("modify_order"), 1)
        self.assertNotIn("cancel_order", backend.names())
        self.assertNotIn("place_order", backend.names())
        self.assertEqual(backend.calls[1], ("modify_order", {
            "acc_id": "LIVE-1", "order_id": "O-1", "exchange": "US", "qty": 100,
            "price": 150.5}))

    def test_modify_a_share_is_refused_with_alternative_path(self):
        """官方 modify-order 页明确不支持 A 股 → 如实拒绝并指出替代路径（不换语义）。"""
        backend = FakeTradeBackend(modify_order={})
        out = trading.OpenApiBroker(trade=backend).modify(
            {"order_id": "O-1", **A_SHARE_ORDER}, "live")
        self.assertEqual(out["status"], "rejected")
        self.assertIn("不支持 A 股", out["err"])
        self.assertIn("trade_cancel", out["err"])
        self.assertEqual(backend.names(), [])

    def test_modify_confirm_required_also_auto_confirms(self):
        backend = FakeTradeBackend(modify_order=self.need, order_confirm={"order_id": "O-1"})
        out = trading.OpenApiBroker(trade=backend).modify({"order_id": "O-1", **ORDER},
                                                         "live")
        self.assertEqual(out["status"], "submitted")
        self.assertTrue(out["confirmed"])
        self.assertEqual(backend.count("order_confirm"), 1)
        self.assertNotIn("place_order", backend.names())

    def test_cancel_maps_exchange_and_returns_cancelled(self):
        backend = FakeTradeBackend(cancel_order={})
        broker = trading.OpenApiBroker(trade=backend)
        for symbol, exchange in (("US.AAPL", "US"), ("HK.00700", "SEHK"),
                                 ("SH.600519", "SSE"), ("SZ.000001", "SZSE")):
            with self.subTest(symbol=symbol):
                backend.calls.clear()
                out = broker.cancel({"symbol": symbol, "order_id": "O-1"}, "live")
                self.assertEqual(out["status"], "cancelled")
                self.assertEqual(backend.calls[-1][1]["exchange"], exchange)

    def test_cancel_bj_refused_and_transport_unknown(self):
        backend = FakeTradeBackend(cancel_order={})
        out = trading.OpenApiBroker(trade=backend).cancel(
            {"symbol": "BJ.430047", "order_id": "O-1"}, "live")
        self.assertEqual(out["status"], "rejected")
        self.assertIn("exchange 枚举", out["err"])
        self.assertEqual(backend.names(), [])
        backend = FakeTradeBackend(cancel_order=TransportError("超时"))
        out = trading.OpenApiBroker(trade=backend).cancel(
            {"symbol": "US.AAPL", "order_id": "O-1"}, "live")
        self.assertEqual(out["status"], "unknown")

    def test_sim_mode_delegates_to_legacy_regardless_of_channel(self):
        legacy = RecordingLegacy()
        broker = trading.OpenApiBroker(channel="openapi", trade=FakeTradeBackend(),
                                       legacy=legacy)
        self.assertEqual(broker.place(dict(ORDER), "sim")["broker_order_id"], "SIM-1")
        broker.positions("sim")
        broker.orders("sim")
        broker.funds("sim")
        self.assertEqual([name for name, _ in legacy.calls],
                         ["place", "positions", "orders", "funds"])

    def test_mcp_channel_never_reaches_openapi_backend(self):
        backend = FakeTradeBackend(place_order={"order_id": "O-1"})
        legacy = RecordingLegacy()
        broker = trading.OpenApiBroker(trade=backend, channel="mcp", legacy=legacy)
        self.assertFalse(broker.supports_live_write)
        self.assertEqual(broker.place(dict(ORDER), "live")["broker_order_id"], "SIM-1")
        self.assertEqual(backend.names(), [], "mcp 通道不得触达 OpenAPI 后端")

    def test_openapi_channel_without_credentials_raises_unavailable(self):
        broker = trading.OpenApiBroker(home=None, channel="openapi",
                                       credential_path="/nonexistent/futu-openapi.json",
                                       legacy=RecordingLegacy())
        self.assertFalse(broker.supports_live_write)
        with self.assertRaises(trading.OpenApiUnavailable):
            broker.place(dict(ORDER), "live")


class OpenApiBrokerReadTest(unittest.TestCase):
    """6 个只读端点 + account_* 的 OpenAPI 分发（逐账户/逐市场）。"""

    def setUp(self):
        self.backend = FakeTradeBackend(
            max_trade_qty={"max_cash_buy": "100", "max_position_sell": "50"},
            open_orders={"orders": [{"order_id": "O-1"}], "page_flag": "PG",
                         "completed": False},
            history_orders={"orders": [{"order_id": "O-2"}], "page_flag": "",
                            "completed": True},
            order_details=[{"order_id": "O-3"}],
            today_deals={"order_fills": [{"deal_id": "D-1"}], "page_flag": "",
                         "completed": True},
            history_deals={"order_fills": [{"deal_id": "D-2"}], "page_flag": "",
                           "completed": True},
            account_funds={"total_assets": "25000.00"},
            positions=[{"code": "US.AAPL", "qty": "100"}])
        self.broker = trading.OpenApiBroker(trade=self.backend)

    def test_trade_max_qty_maps_by_symbol_market(self):
        out = self.broker.trade_max_qty({"code": "US.AAPL", "order_type": "LIMIT",
                                         "price": 150.5}, "live")
        self.assertEqual(out["source"], "futu/openapi:acctradinginfo")
        self.assertEqual(out["groups"], [{"acc_id": "LIVE-1", "market": "US",
                                          "code": "US.AAPL",
                                          "max": {"max_cash_buy": "100",
                                                  "max_position_sell": "50"}}])
        self.assertEqual(out["errors"], [])
        self.assertEqual(self.backend.calls[-1][1], {"acc_id": "LIVE-1",
                                                     "code": "US.AAPL",
                                                     "order_type": "LIMIT",
                                                     "price": 150.5, "order_id": None})

    def test_trade_max_qty_surfaces_missing_account(self):
        backend = FakeTradeBackend(authorized_accounts={"accounts": []})
        out = trading.OpenApiBroker(trade=backend).trade_max_qty(
            {"code": "US.AAPL", "order_type": "LIMIT"}, "live")
        self.assertEqual(out["groups"], [])
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("没有可交易 US 市场", out["errors"][0]["reason"])

    def test_orders_open_fans_out_by_market_and_pages(self):
        out = self.broker.orders_open({"market": "US", "page_flag": "", "page_size": 50},
                                      "live")
        self.assertEqual(out["source"], "futu/openapi:orders")
        self.assertEqual(out["groups"], [{"acc_id": "LIVE-1", "market": "US",
                                          "rows": [{"order_id": "O-1"}],
                                          "page_flag": "PG", "completed": False}])
        self.assertEqual(self.backend.calls[-1][1], {"acc_id": "LIVE-1",
                                                     "trd_market": "US",
                                                     "page_flag": "", "page_size": 50})

    def test_orders_history_passes_micros_and_code(self):
        out = self.broker.orders_history({"market": "US", "code": "US.AAPL",
                                          "start": 1, "end": 2, "page_flag": "PG",
                                          "page_size": 100}, "live")
        self.assertEqual(out["source"], "futu/openapi:orders_history")
        self.assertEqual(self.backend.calls[-1][1], {"acc_id": "LIVE-1",
                                                     "trd_market": "US",
                                                     "page_flag": "PG",
                                                     "code": "US.AAPL", "start": 1,
                                                     "end": 2, "page_size": 100})

    def test_orders_detail_queries_every_authorized_account(self):
        out = self.broker.orders_detail({"exchange": "US", "order_ids": ["O-3"]}, "live")
        self.assertEqual([group["acc_id"] for group in out["groups"]],
                         ["LIVE-1", "LIVE-2"])
        self.assertEqual(out["groups"][0]["rows"], [{"order_id": "O-3"}])
        self.assertEqual(self.backend.calls[-1][1], {"acc_id": "LIVE-2", "exchange": "US",
                                                     "order_ids": ["O-3"]})

    def test_deals_today_and_history(self):
        today = self.broker.deals_today({"market": "US"}, "live")
        self.assertEqual(today["source"], "futu/openapi:order_fills")
        self.assertEqual(today["groups"][0]["rows"], [{"deal_id": "D-1"}])
        history = self.broker.deals_history({"market": "US", "page_size": 50}, "live")
        self.assertEqual(history["source"], "futu/openapi:fills_history")
        self.assertEqual(history["groups"][0]["rows"], [{"deal_id": "D-2"}])

    def test_invalid_market_rejected_before_network(self):
        with self.assertRaises(ValueError):
            self.broker.orders_open({"market": "NONE"}, "live")
        with self.assertRaises(ValueError):
            self.broker.orders_open({}, "live")
        self.assertEqual(self.backend.names(), [], "坏参数零网络往返")

    def test_per_account_failure_is_reported_not_masked(self):
        def fail(acc_id, code=None, pl_ratio_min=None, pl_ratio_max=None):
            if acc_id == "LIVE-1":
                raise OpenApiError("账户无权限", errcode=-9)
            return [{"code": "US.AAPL"}]
        backend = FakeTradeBackend(positions=fail)
        out = trading.OpenApiBroker(trade=backend).positions("live")
        self.assertEqual(out["groups"], [{"acc_id": "LIVE-2",
                                          "market": [1, 4],
                                          "positions": [{"code": "US.AAPL"}]}])
        self.assertEqual(out["errors"][0]["acc_id"], "LIVE-1")

    def test_account_queries_switch_to_rest_on_openapi_channel(self):
        positions = self.broker.positions("live")
        self.assertEqual(positions["source"], "futu/openapi:positions")
        self.assertEqual(positions["groups"][0]["acc_id"], "LIVE-1")
        orders = self.broker.orders("live")
        self.assertEqual(orders["source"], "futu/openapi:orders_history")
        # LIVE-1 只有 US；LIVE-2 有 HK + HKCC 两个市场 → 逐市场各一组
        self.assertEqual([(g["acc_id"], g["market"]) for g in orders["groups"]],
                         [("LIVE-1", "US"), ("LIVE-2", "HK"), ("LIVE-2", "HKCC")])
        funds = self.broker.funds("live")
        self.assertEqual(funds["source"], "futu/openapi:funds")
        self.assertEqual(funds["groups"][0]["cash"], {"total_assets": "25000.00"})

    def test_read_tools_require_openapi_live_channel(self):
        cases = [
            (trading.OpenApiBroker(channel="mcp", trade=self.backend), "openapi:openapi-unavailable"),
            (trading.OpenApiBroker(trade=self.backend), "sim"),
        ]
        for broker, label in cases:
            for name in trading.OPENAPI_TRADE_ENDPOINTS:
                with self.subTest(broker=label, name=name):
                    payload = {"code": "US.AAPL", "order_type": "LIMIT"} \
                        if name == "trade_max_qty" else {"market": "US"}
                    if name == "orders_detail":
                        payload = {"exchange": "US", "order_ids": ["O-1"]}
                    mode = "live" if label.startswith("openapi") else "sim"
                    with self.assertRaises(trading.OpenApiUnavailable) as caught:
                        getattr(broker, name)(payload, mode)
                    self.assertTrue(str(caught.exception))


# ---------------------------------------------------------------------------
# 三、闸门集成：不变式（live 必须过业务确认；默认路径零变化）
# ---------------------------------------------------------------------------
class GateOpenApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.set_mode("live")
        self.confirm = FakeConfirm()
        self.need = OrderConfirmRequired("需要确认", errcode=-1200,
                                         need_order_confirm=True, confirm_id="CF-1")
        self.backend = FakeTradeBackend(place_order=self.need,
                                        order_confirm={"order_id": "O-NEW"})
        self.broker = trading.OpenApiBroker(trade=self.backend)

    def set_mode(self, mode):
        (self.home / "trading-account-mode").write_text(f"{mode}\n")

    def write_channel(self, channel):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"futu_channel": channel}))

    def gate(self, **kw):
        kw.setdefault("broker", self.broker)
        kw.setdefault("confirm", self.confirm)
        kw.setdefault("ctx_builder", fixed_ctx())
        return trading.TradeGate(str(self.home), **kw)

    def order_row(self, cid):
        conn = core_store.connect(core_store.db_path(str(self.home)))
        try:
            return conn.execute("SELECT * FROM orders WHERE client_order_id=?",
                                (cid,)).fetchone()
        finally:
            conn.close()

    def counts(self):
        conn = core_store.connect(core_store.db_path(str(self.home)))
        try:
            return (conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM risk_checks").fetchone()[0])
        finally:
            conn.close()

    def test_live_place_approved_confirms_once_and_marks_submitted(self):
        out = self.gate().place(dict(ORDER, client_order_id="CID-1"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"]["status"], "submitted")
        self.assertTrue(out["value"]["confirmed"])
        self.assertEqual(out["value"]["client_order_id"], "CID-1")
        # 业务确认（唯一人工批准）恰好一次，且摘要是券商参数口径
        self.assertEqual(len(self.confirm.requests), 1)
        self.assertEqual(self.confirm.requests[0]["tool"], "trade_input_order")
        self.assertEqual(self.confirm.requests[0]["mode"], "live")
        # 券商：下单一次 + 自动二次确认一次
        self.assertEqual(self.backend.count("place_order"), 1)
        self.assertEqual(self.backend.count("order_confirm"), 1)
        row = self.order_row("CID-1")
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["broker_order_id"], "O-NEW")

    def test_live_confirm_rejected_means_zero_broker_calls(self):
        self.confirm.outcome = {"decision": "rejected", "id": "c1", "reason": "不批准"}
        out = self.gate().place(dict(ORDER, client_order_id="CID-2"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/order-rejected")
        self.assertIn("业务确认未通过", out["error"]["message"])
        self.assertEqual(self.backend.names(), [], "确认未通过不得触达券商")
        self.assertEqual(self.order_row("CID-2")["status"], "cancelled")

    def test_confirm_failure_leaves_oms_unknown_not_submitted(self):
        self.backend.plan["order_confirm"] = OpenApiError("confirm 过期", errcode=-1)
        out = self.gate().place(dict(ORDER, client_order_id="CID-3"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"]["status"], "unknown")
        self.assertEqual(self.order_row("CID-3")["status"], "unknown")
        self.assertEqual(self.backend.count("place_order"), 1, "绝不重发下单")

    def test_modify_and_cancel_go_through_gate_on_openapi(self):
        self.backend.plan["modify_order"] = {}
        out = self.gate().modify({"order_id": "O-1", **ORDER}, session_id="s")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"]["status"], "submitted")
        self.assertEqual(len(self.confirm.requests), 1)
        self.backend.plan["cancel_order"] = {}
        out = self.gate().cancel({"order_id": "O-1", "symbol": "US.AAPL"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"]["status"], "cancelled")

    def test_default_channel_without_openapi_refuses_live_before_confirm(self):
        """默认 mcp 通道（无 futu_channel）→ 仍是 WP7 的 FutuBroker：live 写先置拒绝。"""
        gate = trading.TradeGate(str(self.home), confirm=self.confirm, ctx_builder=fixed_ctx())
        self.assertIsInstance(gate.broker, trading.FutuBroker)
        self.assertFalse(gate.broker.supports_live_write)
        cases = [("place", dict(ORDER)), ("modify", {"order_id": "O-1", **ORDER}),
                 ("cancel", {"order_id": "O-1", "symbol": "US.AAPL"})]
        for op, payload in cases:
            with self.subTest(op=op):
                out = getattr(gate, op)(payload)
                self.assertFalse(out["ok"])
                self.assertEqual(out["error"]["code"], "trading/broker-unavailable")
                self.assertIn("当前仅 sim 可交易", out["error"]["message"])
        self.assertEqual(self.confirm.requests, [], "确认零调用")
        self.assertEqual(self.counts(), (0, 0), "不落 OMS/风控行")

    def test_openapi_channel_without_credentials_refuses_live_before_confirm(self):
        """channel=openapi 但凭据缺失 → supports_live_write=False，同样在确认前拒绝。"""
        self.write_channel("openapi")
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)}):
            gate = trading.TradeGate(str(self.home), confirm=self.confirm,
                                     ctx_builder=fixed_ctx())
            self.assertIsInstance(gate.broker, trading.OpenApiBroker)
            self.assertFalse(gate.broker.supports_live_write)
            out = gate.place(dict(ORDER, client_order_id="CID-NOCRED"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/broker-unavailable")
        self.assertEqual(self.confirm.requests, [])
        self.assertEqual(self.counts(), (0, 0))

    def test_openapi_channel_with_credentials_declares_live_write(self):
        """channel=openapi + 凭据可用 → supports_live_write=True（能力是动态属性）。"""
        self.write_channel("openapi")
        (self.home / "futu-openapi.json").write_text(json.dumps(
            {"mode": "oauth", "access_token": "t", "refresh_token": "r"}))
        with mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)}):
            broker = trading.default_broker(str(self.home))
            self.assertIsInstance(broker, trading.OpenApiBroker)
            self.assertTrue(broker.supports_live_write)
            (self.home / "futu-openapi.json").unlink()
            self.assertFalse(broker.supports_live_write, "凭据消失后能力随之收回")

    def test_sim_write_uses_sim_path_even_on_openapi_channel(self):
        """sim 语义不因通道切换而改变（REST 交易面只覆盖实盘业务账户）。"""
        legacy = RecordingLegacy()
        broker = trading.OpenApiBroker(trade=self.backend, legacy=legacy)
        self.set_mode("sim")
        out = self.gate(broker=broker).place(dict(ORDER, client_order_id="CID-SIM"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"]["broker_order_id"], "SIM-1")
        self.assertEqual([name for name, _ in legacy.calls], ["place"])
        self.assertEqual(self.confirm.requests, [], "sim 不发起业务确认")
        self.assertEqual(self.backend.names(), [], "sim 不得触达 OpenAPI 后端")


# ---------------------------------------------------------------------------
# 四、HTTP 路由 + 6 个新只读端点的闸门信封
# ---------------------------------------------------------------------------
class HttpRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "trading-account-mode").write_text("live\n")
        self.calls = []
        calls = self.calls

        class RecordingGate:
            def __getattr__(gate_self, name):
                def call(payload):
                    calls.append((name, dict(payload)))
                    return {"ok": True, "value": {"routed": name}}
                return call

        self.handle = app_module.create_handler(str(self.home), analytics={}, series=None,
                                                core={}, trade=RecordingGate())

    def test_new_endpoints_route_to_gate(self):
        cases = [
            ("trade_max_qty", {"code": "US.AAPL", "order_type": "LIMIT"}),
            ("orders_open", {"market": "US"}),
            ("orders_history", {"market": "HK", "code": "HK.00700"}),
            ("orders_detail", {"exchange": "US", "order_ids": ["O-1"]}),
            ("deals_today", {"market": "US"}),
            ("deals_history", {"market": "US", "page_size": 50}),
        ]
        for endpoint, payload in cases:
            with self.subTest(endpoint=endpoint):
                out = self.handle(endpoint, dict(payload))
                self.assertTrue(out["ok"], out)
                self.assertEqual(out["value"]["routed"], endpoint)
                self.assertEqual(self.calls[-1], (endpoint, payload))

    def test_unknown_field_rejected_by_whitelist(self):
        """载荷字段白名单在 HTTP 层（值域/模式由闸门与 OpenApiTrade 校验）。"""
        out = self.handle("orders_open", {"market": "US", "extra": 1})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")
        self.assertEqual(self.calls, [], "白名单拒绝不得触达闸门")

    def test_read_endpoints_are_not_cached(self):
        for endpoint, payload in (("orders_open", {"market": "US"}),
                                  ("deals_today", {"market": "US"})):
            self.handle(endpoint, dict(payload))
            self.handle(endpoint, dict(payload))
        self.assertEqual(len(self.calls), 4, "实时直通端点不得进缓存")
        for endpoint in trading.OPENAPI_TRADE_ENDPOINTS:
            self.assertNotIn(endpoint, caches.CACHE_TTL_MS, endpoint)
            self.assertNotIn(endpoint, caches.ENDPOINT_SHAPE, endpoint)

    def test_openapi_unavailable_envelope_points_to_auth(self):
        """mcp 通道下 6 个只读端点返回 openapi-unavailable（指引 scripts/futu_auth.py）。"""
        gate = trading.TradeGate(str(self.home), confirm=FakeConfirm(),
                                 ctx_builder=fixed_ctx())
        self.assertIsInstance(gate.broker, trading.FutuBroker)
        for endpoint in trading.OPENAPI_TRADE_ENDPOINTS:
            payload = {"code": "US.AAPL", "order_type": "LIMIT"} \
                if endpoint == "trade_max_qty" else {"market": "US"}
            if endpoint == "orders_detail":
                payload = {"exchange": "US", "order_ids": ["O-1"]}
            with self.subTest(endpoint=endpoint):
                out = getattr(gate, endpoint)(payload)
                self.assertFalse(out["ok"])
                self.assertEqual(out["error"]["code"], "trading/openapi-unavailable")
                self.assertIn("futu_auth.py", out["error"]["message"])

    def test_sim_mode_read_tool_points_to_account_alternatives(self):
        (self.home / "trading-account-mode").write_text("sim\n")
        gate = trading.TradeGate(str(self.home), broker=trading.OpenApiBroker(
            trade=FakeTradeBackend()), ctx_builder=fixed_ctx())
        out = gate.orders_open({"market": "US"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/openapi-unavailable")
        self.assertIn("account_orders", out["error"]["message"])

    def test_invalid_parameter_and_mode_map_to_invalid_operation(self):
        gate = trading.TradeGate(str(self.home), broker=trading.OpenApiBroker(
            trade=FakeTradeBackend()), ctx_builder=fixed_ctx())
        # page_size 区间由 OpenApiTrade 本地校验（见 OpenApiTradeDocTest），
        # 这里覆盖闸门层的三类：market 枚举非法 / mode 非法 / 必填 market 缺失
        for payload in ({"market": "NONE"}, {"market": "US", "mode": "yolo"}, {}):
            with self.subTest(payload=payload):
                out = gate.orders_open(payload)
                self.assertFalse(out["ok"])
                self.assertEqual(out["error"]["code"], "trading/invalid-operation")


# ---------------------------------------------------------------------------
# 五、工具面/端点清单/字段集锁定（56 / 52）
# ---------------------------------------------------------------------------
class SurfaceLockTest(unittest.TestCase):
    def test_tool_surface_is_56(self):
        self.assertEqual(mcp_tools.TOOL_COUNT, 56)
        self.assertEqual(len(mcp_tools.TOOLS), 56)
        names = {tool.name for tool in mcp_tools.TOOLS}
        self.assertLessEqual(set(trading.OPENAPI_TRADE_ENDPOINTS), names)
        self.assertNotIn("trade_confirm", names, "券商二次确认不得成为模型可调用工具")
        self.assertNotIn("confirm_decide", names)

    def test_endpoint_registry_is_52_with_new_tail(self):
        endpoints = store_access.endpoints()
        self.assertEqual(len(endpoints), 52)
        self.assertEqual(len(set(endpoints)), 52)
        self.assertEqual(list(store_access.WP8_TRADE_ENDPOINTS),
                         list(trading.OPENAPI_TRADE_ENDPOINTS))
        self.assertEqual(endpoints[-6:], list(trading.OPENAPI_TRADE_ENDPOINTS))

    def test_tool_fields_match_http_whitelist(self):
        definitions = {tool.name: tool for tool in mcp_tools.TOOLS}
        for endpoint, allowed in app_module.OPENAPI_TRADE_FIELDS.items():
            with self.subTest(endpoint=endpoint):
                self.assertEqual(tuple(definitions[endpoint].fields), tuple(allowed))
        self.assertEqual(tuple(app_module.OPENAPI_TRADE_FIELDS),
                         tuple(trading.OPENAPI_TRADE_ENDPOINTS))

    def test_endpoint_tool_set_still_equals_endpoints_minus_excluded(self):
        forwarded = set(mcp_tools.ENDPOINT_TOOL_ENDPOINTS.values())
        self.assertEqual(forwarded,
                         set(store_access.endpoints()) - mcp_tools.MCP_EXCLUDED_ENDPOINTS)
        self.assertNotIn("confirm-decide", forwarded)

    def test_published_enum_domains_match_client_constants(self):
        """schema 的 order_type/trd_market/exchange 取值域 ≡ OpenApiTrade 的枚举常量。"""
        self.assertEqual(set(get_args(mcp_tools._TYPES["order_type"])),
                         set(OpenApiTrade.ORDER_TYPES))
        self.assertEqual(set(get_args(mcp_tools._TYPES["trd_market"])),
                         set(OpenApiTrade.TRD_MARKETS))
        self.assertEqual(set(get_args(mcp_tools._TYPES["exchange"])),
                         set(OpenApiTrade.EXCHANGES))

    def test_read_tools_are_declared_against_the_openapi_channel(self):
        """工具描述必须写明通道前提，免得模型在 mcp 部署下反复试错。"""
        for tool in mcp_tools.TOOLS:
            if tool.name in trading.OPENAPI_TRADE_ENDPOINTS:
                self.assertIn("openapi", tool.description.lower(), tool.name)
                self.assertIn("account_", tool.description, tool.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
