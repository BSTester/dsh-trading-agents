"""WP16：6 个交易只读端点的**模拟盘读能力**（模式感知）。

用户诉求（2026-09-17 原话）：「``deals_today`` 只覆盖实盘业务账户…sim 模式请用 account_*
—— **api 不能模拟盘吗，工作台需要支持模拟盘，模拟盘是全自动交易的**。」

修前：这 6 个端点（``orders_open``/``orders_history``/``orders_detail``/``deals_today``/
``deals_history``/``trade_max_qty``）只在 ``channel=openapi`` 且 ``mode=live`` 可用；
sim 一律 ``trading/openapi-unavailable`` +「sim 模式请用 account_*」——对 sim 是**死路**。
修后（本文件锁住）：

* ``mode == sim`` → ``sim_trade_*`` 等价实现（当日订单/历史订单/最大买卖量；成交**由订单
  派生**并在响应里标 ``derived=True`` + note，绝不冒充券商成交流水）；
* ``mode == live`` → 行为逐字不变（openapi 通道走 REST；mcp 通道仍如实拒绝并指引授权）。

测试全是离线替身（假 sim call + ``RecordingClient``），不触网络。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
sys.path.insert(0, str(ROOT / "tests"))

from test_wp8_trading import FakeConfirm, fixed_ctx  # noqa: E402  （复用既有替身）
from trading_datasource import channel  # noqa: E402
from server import trading  # noqa: E402
from trading_core import broker as core_broker  # noqa: E402

#: 模拟账户（market_id 口径见 trading_datasource/market_ids.py，真机实测）
SIM_ACCOUNTS = {"accounts": [{"account_id": "9393", "market_id": 1},
                             {"account_id": "3182575", "market_id": 3},
                             {"account_id": "11587526", "market_id": 100}]}

#: 当日订单：在途（2/3）+ 已成交（4）+ 已撤（5，含部分成交）——覆盖四种过滤/派生路径
TODAY_ORDERS = {"orders": [
    {"order_id": "O-OPEN", "symbol": "SH.600000", "side": "BUY", "status": 2,
     "qty": "100", "cum_qty": "0", "price": "9.06", "avg_fill_price": "0",
     "create_time": "2026-09-17 09:35:00"},
    {"order_id": "O-PART", "symbol": "SH.600009", "side": "BUY", "status": 3,
     "qty": "200", "cum_qty": "100", "price": "10.00", "avg_fill_price": "9.99",
     "update_time": "2026-09-17 10:01:00"},
    {"order_id": "O-FILLED", "symbol": "SH.600010", "side": "BUY", "status": 4,
     "qty": "100", "cum_qty": "100", "price": "5.00", "avg_fill_price": "5.01",
     "update_time": "2026-09-17 10:30:00"},
    {"order_id": "O-CANCELLED", "symbol": "SH.600015", "side": "SELL", "status": 5,
     "qty": "300", "cum_qty": "0", "price": "12.00", "avg_fill_price": "0"},
    {"order_id": "O-CANCEL-PART", "symbol": "SH.600016", "side": "SELL", "status": 5,
     "qty": "300", "cum_qty": "50", "price": "7.00", "avg_fill_price": "7.01"},
]}
HIST_ORDERS = {"orders": [
    {"order_id": "H-FILLED", "symbol": "SH.600000", "side": "BUY", "status": 4,
     "qty": "100", "cum_qty": "100", "price": "8.50", "avg_fill_price": "8.49",
     "update_time": "2026-09-10 14:00:00"},
    {"order_id": "H-REJECTED", "symbol": "SH.600000", "side": "BUY", "status": 6,
     "qty": "100", "cum_qty": "0", "price": "8.00", "avg_fill_price": "0"},
]}


#: 账户 → 市场链；订单行按 symbol 前缀归属市场（模拟账户只持有本市场订单）
ACC_CHAIN = {"9393": "HK", "3182575": "SH", "11587526": "US"}
SYMBOL_CHAIN = {"SH": "SH", "SZ": "SH", "BJ": "SH", "HK": "HK", "US": "US"}


def _rows_of_chain(rows, acc_id):
    chain = ACC_CHAIN.get(str(acc_id))
    return [row for row in rows
            if SYMBOL_CHAIN.get(str(row.get("symbol", "")).split(".", 1)[0]) == chain]


class FakeSimCall:
    """假 sim 通道（与 ``futu_mcp.call_tool`` 同签名）；记录每次 (tool, params)。"""

    def __init__(self, accounts=None, today=None, hist=None, max_buy=None, error=None):
        self.accounts = SIM_ACCOUNTS if accounts is None else accounts
        self.today = TODAY_ORDERS if today is None else today
        self.hist = HIST_ORDERS if hist is None else hist
        self.max_buy = {"max_cash_buy_qty_round_lot": 1200} if max_buy is None else max_buy
        self.error = error or {}
        self.calls = []

    def __call__(self, tool, params, timeout=None):
        self.calls.append((tool, dict(params)))
        if tool in self.error:
            raise RuntimeError(self.error[tool])
        acc = str(params.get("acc_id"))
        if tool == core_broker.TOOLS["accounts"]:
            return self.accounts
        if tool == core_broker.TOOLS["orders"]:
            return {"orders": _rows_of_chain(self.today.get("orders", []), acc)}
        if tool == core_broker.TOOLS["history"]:
            return {"orders": _rows_of_chain(self.hist.get("orders", []), acc)}
        if tool == core_broker.TOOLS["max_buy_sell"]:
            return self.max_buy
        raise AssertionError(f"未预期的工具：{tool}")

    def tools(self):
        return [tool for tool, _ in self.calls]


class SimReadTest(unittest.TestCase):
    """6 个端点 × sim：正常路径的形状与出站工具名（逐条断言）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "trading-account-mode").write_text("sim\n")
        self.call = FakeSimCall()
        self.broker = trading.FutuBroker(call=self.call, home=str(self.home))

    def rows_of(self, out):
        return {g["acc_id"]: [r.get("order_id") for r in g.get("rows", [])]
                for g in out["groups"]}

    # ---- orders_open：当日订单按在途码过滤 ----
    def test_orders_open_keeps_only_open_statuses(self):
        out = self.broker.orders_open({"market": "SH"}, "sim")
        self.assertEqual(self.rows_of(out), {"3182575": ["O-OPEN", "O-PART"]})
        self.assertEqual(out["filter"], "status in {2,3}")
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["mode"], "sim")
        # 出站：当日订单接口 + market_id（实测必填）
        self.assertEqual(self.call.calls[-1][0], core_broker.TOOLS["orders"])
        self.assertEqual(self.call.calls[-1][1]["market"], 3)

    def test_orders_open_market_filter_selects_single_account(self):
        out = self.broker.orders_open({"market": "US"}, "sim")
        self.assertEqual(list(self.rows_of(out)), ["11587526"])

    def test_orders_open_without_market_covers_all_sim_accounts(self):
        out = self.broker.orders_open({}, "sim")
        self.assertEqual(sorted(self.rows_of(out)), ["11587526", "3182575", "9393"])

    # ---- orders_history：沿用 30 天窗口 ----
    def test_orders_history_window_and_rows(self):
        out = self.broker.orders_history({"market": "SH"}, "sim")
        self.assertEqual(self.rows_of(out), {"3182575": ["H-FILLED", "H-REJECTED"]})
        self.assertEqual(out["window"]["end"], __import__("datetime").date.today().isoformat())
        self.assertEqual(self.call.calls[-1][0], core_broker.TOOLS["history"])
        self.assertIn("start", self.call.calls[-1][1])

    def test_orders_history_honours_explicit_window(self):
        self.broker.orders_history({"start": "2026-09-01", "end": "2026-09-10"}, "sim")
        params = self.call.calls[-1][1]
        self.assertEqual((params["start"], params["end"]), ("2026-09-01", "2026-09-10"))

    # ---- orders_detail：当日 + 历史里查找；找不到如实标记 ----
    def test_orders_detail_finds_and_reports_unknown(self):
        out = self.broker.orders_detail({"order_ids": ["O-OPEN", "NOPE"]}, "sim")
        self.assertEqual(out["missing"], ["NOPE"])
        self.assertIn("未在模拟账本中找到", out["note"])
        found = {g["acc_id"]: [r["order_id"] for r in g["rows"]] for g in out["groups"]}
        self.assertEqual(found, {"3182575": ["O-OPEN"]})

    def test_orders_detail_accepts_single_string_order_id(self):
        out = self.broker.orders_detail({"order_ids": "H-FILLED"}, "sim")
        self.assertEqual(out["missing"], [])
        self.assertEqual({g["acc_id"]: [r["order_id"] for r in g["rows"]]
                          for g in out["groups"]}, {"3182575": ["H-FILLED"]},
                         "历史单只出现在持有它的市场账户（不跨账户重复）")

    def test_orders_detail_requires_order_ids(self):
        with self.assertRaises(ValueError):
            self.broker.orders_detail({}, "sim")

    def test_orders_detail_dedups_order_present_in_today_and_history(self):
        """当日列表与历史窗口重叠（真机 7149712 同日两处都返回）→ 详情不得重复出行。"""
        overlap = {"orders": [{"order_id": "O-BOTH", "symbol": "SH.600010", "side": "BUY",
                               "status": 2, "qty": "100", "cum_qty": "0", "price": "2.11"}]}
        call = FakeSimCall(today=overlap, hist=overlap)
        broker = trading.FutuBroker(call=call, home=str(self.home))
        out = broker.orders_detail({"order_ids": ["O-BOTH"]}, "sim")
        self.assertEqual([r["order_id"] for g in out["groups"] for r in g["rows"]],
                         ["O-BOTH"], "同一订单号在当日与历史里各出现一次 → 详情只出一行")
        self.assertEqual(out["missing"], [])

    # ---- deals_*：由订单派生并如实标注 ----
    def test_deals_today_derives_only_filled_orders(self):
        out = self.broker.deals_today({"market": "SH"}, "sim")
        fills = out["groups"][0]["rows"]
        self.assertEqual([f["order_id"] for f in fills],
                         ["O-PART", "O-FILLED", "O-CANCEL-PART"])
        part = fills[0]
        self.assertEqual(part["filled_qty"], 100)
        self.assertEqual(part["avg_price"], "9.99")
        self.assertEqual(part["status"], "partial")
        self.assertTrue(out["derived"], "派生成交必须如实标注 derived")
        self.assertIn("派生", out["note"])
        self.assertNotIn("O-OPEN", [f["order_id"] for f in fills], "零成交不下发")

    def test_deals_history_derives_from_history(self):
        out = self.broker.deals_history({}, "sim")
        fills = [f for g in out["groups"] for f in g["rows"]]
        self.assertEqual([f["order_id"] for f in fills], ["H-FILLED"],
                         "被拒（6）且零成交的单不成交")
        self.assertTrue(out["derived"])
        self.assertEqual(fills[0]["status"], "filled")
        self.assertEqual(fills[0]["avg_price"], "8.49")

    # ---- trade_max_qty：按标的市场选账户 + 契约翻译（真机暴露的两个坑） ----
    def test_trade_max_qty_selects_account_by_symbol_market(self):
        out = self.broker.trade_max_qty({"code": "SH.600000", "order_type": "LIMIT",
                                         "price": 9.06}, "sim")
        self.assertEqual([g["acc_id"] for g in out["groups"]], ["3182575"])
        self.assertEqual(out["groups"][0]["max"]["max_cash_buy_qty_round_lot"], 1200)
        params = self.call.calls[-1][1]
        # 真机（2026-09-17）：symbol 必须裸代码（带前缀 -5 backend business error）
        self.assertEqual((params["symbol"], params["market"]), ("600000", 3))
        # 工具面是字符串枚举，模拟盘只认 1=限价 / 3=市价 → 适配层翻译
        self.assertEqual(params["order_type"], 1)
        self.assertEqual(params["price"], 9.06)

    def test_trade_max_qty_maps_market_order_type_to_sim_enum(self):
        self.broker.trade_max_qty({"code": "SH.600000", "order_type": "MARKET",
                                   "price": 9.06}, "sim")
        self.assertEqual(self.call.calls[-1][1]["order_type"], 3)

    def test_trade_max_qty_rejects_order_type_outside_sim_enum(self):
        for value in ("STOP", "AUCTION", "yolo", 5):
            with self.subTest(order_type=value):
                with self.assertRaises(ValueError) as caught:
                    self.broker.trade_max_qty({"code": "SH.600000", "order_type": value,
                                               "price": 9.06}, "sim")
                self.assertIn("order_type", str(caught.exception))

    def test_trade_max_qty_requires_order_type(self):
        with self.assertRaises(ValueError):
            self.broker.trade_max_qty({"code": "SH.600000", "price": 9.06}, "sim")

    def test_trade_max_qty_requires_price_because_venue_returns_zeros(self):
        """实测：缺 price 时模拟盘回全 0（限价/市价都一样）——全 0 不是答案，如实拒绝。"""
        with self.assertRaises(ValueError) as caught:
            self.broker.trade_max_qty({"code": "SH.600000", "order_type": "LIMIT"}, "sim")
        self.assertIn("price", str(caught.exception))
        self.assertEqual(self.call.calls, [], "缺参本地拒绝，零通道往返")

    def test_trade_max_qty_requires_symbol(self):
        with self.assertRaises(ValueError):
            self.broker.trade_max_qty({}, "sim")

    def test_trade_max_qty_reports_missing_account_for_unmapped_market(self):
        out = self.broker.trade_max_qty({"code": "JP.7203", "order_type": "LIMIT",
                                         "price": 100.0}, "sim")
        self.assertEqual(out["groups"], [])
        self.assertTrue(out["errors"], "没有对应市场模拟账户时如实报错，不伪造")

    # ---- 失败隔离 ----
    def test_single_account_failure_is_recorded_not_masked(self):
        call = FakeSimCall()
        original = call.__call__

        def flaky(tool, params, timeout=None):
            if tool == core_broker.TOOLS["orders"] and str(params.get("acc_id")) == "9393":
                call.calls.append((tool, dict(params)))
                raise RuntimeError("模拟通道抖动")
            return original(tool, params, timeout)

        self.broker = trading.FutuBroker(call=flaky, home=str(self.home))
        out = self.broker.orders_open({}, "sim")
        self.assertEqual([e["acc_id"] for e in out["errors"]], ["9393"])
        self.assertEqual(sorted(self.rows_of(out)), ["11587526", "3182575"],
                         "单账户失败不掩盖其他账户")


class GateEnvelopeTest(unittest.TestCase):
    """闸门信封：sim 不再返回 openapi-unavailable（修前该断言必红）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "trading-account-mode").write_text("sim\n")
        self.call = FakeSimCall()
        self.gate = trading.TradeGate(str(self.home), broker=None, confirm=FakeConfirm(),
                                      ctx_builder=fixed_ctx())
        self.gate.broker = trading.FutuBroker(call=self.call, home=str(self.home))

    def test_all_six_reads_ok_on_sim_and_never_openapi_unavailable(self):
        cases = {
            "orders_open": {"market": "SH"},
            "orders_history": {"market": "SH"},
            "orders_detail": {"order_ids": ["O-OPEN"]},
            "deals_today": {"market": "SH"},
            "deals_history": {"market": "SH"},
            "trade_max_qty": {"code": "SH.600000", "order_type": "LIMIT", "price": 9.06},
        }
        for name, payload in cases.items():
            with self.subTest(endpoint=name):
                out = getattr(self.gate, name)(payload)
                self.assertTrue(out["ok"], out)
                self.assertNotEqual(out.get("error", {}).get("code"),
                                    trading.OPENAPI_UNAVAILABLE_CODE)
                self.assertEqual(out["value"]["mode"], "sim")

    def test_gate_maps_value_error_to_invalid_operation(self):
        out = self.gate.orders_detail({})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")

    def test_gate_maps_channel_failure_to_broker_unavailable(self):
        broker = trading.FutuBroker(
            call=FakeSimCall(accounts={"accounts": []},
                             error={core_broker.TOOLS["orders"]: "网络不通"}),
            home=str(self.home))
        gate = trading.TradeGate(str(self.home), broker=broker, confirm=FakeConfirm(),
                                 ctx_builder=fixed_ctx())
        out = gate.orders_open({"market": "SH"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["value"]["errors"], "单账户失败如实进 errors，不 500")


class OpenApiChannelSimDelegationTest(unittest.TestCase):
    """openapi 通道下 sim 读：委托 sim 等价实现，**绝不触达 REST**。"""

    def test_sim_reads_delegate_to_legacy_not_rest(self):
        calls = []
        legacy_broker = trading.FutuBroker(call=FakeSimCall(), home="/tmp")
        broker = trading.OpenApiBroker(trade=_ExplodingBackend(), legacy=legacy_broker,
                                       channel="openapi")
        for name, payload in (("orders_open", {"market": "SH"}),
                              ("deals_today", {"market": "SH"}),
                              ("trade_max_qty", {"code": "SH.600000",
                                                 "order_type": "LIMIT", "price": 9.06})):
            with self.subTest(endpoint=name):
                out = getattr(broker, name)(payload, "sim")
                self.assertEqual(out["mode"], "sim")
        self.assertEqual(calls, [])

    def test_sim_read_route_internal_guard_is_explicit(self):
        broker = trading.OpenApiBroker(trade=_ExplodingBackend(), channel="openapi")
        with self.assertRaises(trading.OpenApiUnavailable):
            broker._read_route("orders_open", "sim")


class LiveRegressionTest(unittest.TestCase):
    """live 行为逐字不变（mcp 通道仍拒绝并指引；openapi 缺凭据仍指引授权）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_mcp_channel_live_reads_still_refused(self):
        broker = trading.FutuBroker(call=FakeSimCall(), home=str(self.home))
        for name in ("orders_open", "orders_history", "orders_detail", "deals_today",
                     "deals_history", "trade_max_qty"):
            with self.subTest(endpoint=name):
                with self.assertRaises(trading.OpenApiUnavailable) as ctx:
                    getattr(broker, name)({}, "live")
                self.assertIn("需要 OpenAPI 交易通道", str(ctx.exception))

    def test_openapi_live_without_credentials_still_hints_authorization(self):
        # 注意：注入 trade 替身会被 ``_openapi_ready`` 视为「凭据就绪」（测试确定性），
        # 因此这里**不注入 trade**——只钉通道与不存在的凭据路径，走真实凭据判定。
        broker = trading.OpenApiBroker(channel="openapi",
                                       credential_path=str(self.home / "missing.json"))
        with self.assertRaises(trading.OpenApiUnavailable) as ctx:
            broker.orders_open({}, "live")
        self.assertIn("OpenAPI 凭据缺失", str(ctx.exception))


class ChannelRouteTest(unittest.TestCase):
    """模拟交易通道：新增「当日订单」路由（sim 读闭环缺的那一环）。"""

    def test_today_orders_route_is_registered(self):
        self.assertEqual(channel.SIM_TOOL_ROUTES.get("sim_trade_order_list"),
                         "simtrade.order_list")

    def test_today_orders_rest_branch_passes_market(self):
        # 走真实翻译路径：注入记录型假 client（call_openapi 会实例化 OpenApiSimTrade
        # 并调 client.request），断言出站方法与 market（实测必填）。
        client = _RecordingRestClient(d={"orders": []})
        home = _openapi_home(self.home_path())
        call = channel.sim_call(home, client=client,
                                credential_path=str(self.home_path() / "cred.json"),
                                mcp_call=lambda *a, **k: {"orders": []})
        out = call("sim_trade_order_list", {"acc_id": "3182575", "market": 3})
        self.assertEqual(out, {"orders": []})
        self.assertEqual(len(client.calls), 1)
        method, path, query, _body = client.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(path, "/api/v1.0/sim-trade/3182575/orders")
        self.assertEqual((query or {}).get("market"), 3)

    def home_path(self):
        if not hasattr(self, "_home"):
            self._home = tempfile.TemporaryDirectory()
            self.addCleanup(self._home.cleanup)
        return Path(self._home.name)


def _openapi_home(tmp):
    """``futu_channel=openapi`` 的临时 home（REST 分支需要凭据就绪）。"""
    (tmp / "trading-platform.json").write_text(json.dumps({"futu_channel": "openapi"}),
                                               encoding="utf-8")
    (tmp / "cred.json").write_text(json.dumps(
        {"mode": "oauth", "access_token": "t", "refresh_token": "r"}), encoding="utf-8")
    return str(tmp)


class _RecordingRestClient:
    """记录型 ``OpenApiClient`` 替身：记下 (method, path, query, body) 并回固定 ``d``。"""

    def __init__(self, d=None):
        self.d = d if d is not None else {}
        self.calls = []

    def request(self, method, path, query=None, json_body=None):
        self.calls.append((method, path, query, json_body))
        return self.d


class _ExplodingBackend:
    """OpenAPI 交易后端替身：任何调用都炸——用于证明 sim 路径绝不触达 REST。"""

    def __getattr__(self, name):
        def boom(*args, **kwargs):
            raise AssertionError(f"sim 路径不得触达 OpenAPI 后端：{name}")
        return boom


if __name__ == "__main__":
    unittest.main()
