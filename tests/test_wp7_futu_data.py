"""WP8 富途实时数据直通（8 端点/工具：rt_quote/rt_order_book/capital_flow/capital_flow_history/
capital_distribution/option_expiration/option_chain/option_screen）。

全部离线：富途通道用计数替身（FakeCall，同 FutuBroker 的注入口径），服务路由用
替身 FutuData（duck-type `.handle(endpoint, payload)`）。覆盖：

* 通道注入：成功透传（参数/返回逐键）；`ret_code!=0` 与 `s=="error"` 文本信封 →
  ``trading/futu-error``（消息含 ret_code/errmsg 截断）；token 缺失 →
  ``trading/futu-unavailable`` + 指向 ``scripts/futu_auth.py``；网络故障 → unavailable；
  A 股实时 -9 → 错误消息附「A 股实时无权限：可用 capital_flow / history-kline 替代」；
* 参数白名单/必填：code 归一同 series 的 ticker 规则（``to_futu_symbol``）；rt_quote
  codes 1..10；capital_flow_history days 1..1000（默认 30 → 上游 count）；option_screen
  必须带非空 ``field_filter``（缺省时上游只回 4 个默认字段、其余全 null，见
  docs/TOOL-LIMITS.md）与非空 ``strategy``，内键白名单校验；
* 路由契约：8 端点进 ``store_access.endpoints()`` 白名单、字段白名单在 handle 层拒绝、
  实时零缓存（不进 CACHE_TTL_MS/ENDPOINT_SHAPE、响应不带 cached 字段、连调两次
  全部触达通道）、错误 envelope 原样透传；
* 工具面（WP8 任务 6 起 59）：8 工具 ∈ TOOLS、映射端点同名、字段集与 HTTP 白名单
  同形、``confirm_decide`` 仍禁入、描述注明「服务端经富途实时获取；A 股实时受限见
  错误消息」。
* ``option_expiration`` 的上游实名是 ``quote_option_expiration_date``（2026-09-16 实测
  tools/list：``quote_option_expiration`` 返回 "tool has been deactivated or does not
  exist"，``quote_option_expiration_date`` ret_code=0）——按通道实测事实锁定。
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from server import app as app_module  # noqa: E402
from server import caches, futu_data, mcp_tools, store_access  # noqa: E402
from trading_datasource.futu_mcp import FutuUnavailable  # noqa: E402

FUTU_ENDPOINTS = ("rt_quote", "rt_order_book", "capital_flow", "capital_flow_history",
                  "capital_distribution", "option_expiration", "option_chain", "option_screen")


class FakeCall:
    """富途通道计数替身：记录每次 (工具名, 入参)；error 注入时抛 FutuUnavailable。"""

    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"echo": "data"} if result is None else result
        self.error = error

    def __call__(self, name, arguments, timeout=30, auto_refresh=True, client_name=None):
        self.calls.append((name, dict(arguments)))
        if self.error is not None:
            raise self.error
        if isinstance(self.result, list):
            return list(self.result)  # 通道实测：order_book 的 data 是数组
        return dict(self.result)

    def count(self):
        return len(self.calls)


def make_futu(result=None, error=None):
    return futu_data.FutuData(call=FakeCall(result=result, error=error))


class ChannelPassthroughTest(unittest.TestCase):
    """通道注入：参数归一/白名单/必填 + 错误分类（业务/凭证/网络三分）。"""

    def test_capital_flow_passthrough_arguments_and_result(self):
        futu = make_futu(result={"fl": [{"time": "09:30", "in": 1}]})
        out = futu.handle("capital_flow", {"code": "00700.HK"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"], {"fl": [{"time": "09:30", "in": 1}]})
        self.assertEqual(futu._call.calls,
                         [("quote_capital_flow", {"symbol": "HK.00700"})])

    def test_rt_quote_maps_codes_to_code_list_with_series_ticker_rule(self):
        futu = make_futu()
        out = futu.handle("rt_quote", {"codes": ["600519", "US.AAPL"]})
        self.assertTrue(out["ok"], out)
        self.assertEqual(futu._call.calls,
                         [("quote_stock_quote", {"code_list": ["SH.600519", "US.AAPL"]})])

    def test_rt_quote_codes_bounds_1_to_10(self):
        for codes in ([], ["HK.00700"] * 11, "HK.00700", [42]):
            futu = make_futu()
            out = futu.handle("rt_quote", {"codes": codes})
            self.assertFalse(out["ok"], codes)
            self.assertEqual(out["error"]["code"], "trading/invalid-operation", codes)
            self.assertEqual(futu._call.count(), 0, codes)

    def test_rt_order_book_normalizes_and_validates_code(self):
        futu = make_futu()
        out = futu.handle("rt_order_book", {"code": "700"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(futu._call.calls, [("quote_order_book", {"code": "HK.00700"})])
        futu = make_futu()
        out = futu.handle("rt_order_book", {"code": "腾讯"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")
        self.assertEqual(futu._call.count(), 0)

    def test_rt_order_book_list_payload_passthrough(self):
        """通道实测：quote_order_book 的 data 是数组（[{books:…}]），list 原样透传不报形状错。"""
        books = [{"books": [{"ask_list": [{"price": 434.8}], "bid_list": []}]}]
        futu = make_futu(result=books)
        out = futu.handle("rt_order_book", {"code": "HK.00700"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"], books)

    def test_capital_flow_history_days_maps_to_count_default_30(self):
        futu = make_futu()
        self.assertTrue(futu.handle("capital_flow_history", {"code": "SH.600519"})["ok"])
        self.assertEqual(futu._call.calls,
                         [("quote_capital_flow_history", {"symbol": "SH.600519", "count": 30})])
        self.assertTrue(futu.handle("capital_flow_history", {"code": "SH.600519", "days": 7})["ok"])
        self.assertEqual(futu._call.calls[-1][1]["count"], 7)

    def test_capital_flow_history_days_range(self):
        for days in (0, -1, 1001, "7", 1.5, True):
            futu = make_futu()
            out = futu.handle("capital_flow_history", {"code": "SH.600519", "days": days})
            self.assertFalse(out["ok"], days)
            self.assertEqual(out["error"]["code"], "trading/invalid-operation", days)
            self.assertEqual(futu._call.count(), 0, days)

    def test_capital_distribution_and_option_expiration_passthrough(self):
        futu = make_futu()
        self.assertTrue(futu.handle("capital_distribution", {"code": "SZ.000001"})["ok"])
        self.assertEqual(futu._call.calls,
                         [("quote_capital_distribution", {"symbol": "SZ.000001"})])
        self.assertTrue(futu.handle("option_expiration", {"code": "HK.00700"})["ok"])
        # 上游实名（实测）：quote_option_expiration 已停用，quote_option_expiration_date 可用
        self.assertEqual(futu._call.calls[-1],
                         ("quote_option_expiration_date", {"symbol": "HK.00700"}))

    def test_option_chain_field_filter_optional_and_forwarded(self):
        futu = make_futu()
        self.assertTrue(futu.handle("option_chain", {"code": "US.AAPL"})["ok"])
        self.assertEqual(futu._call.calls[-1], ("quote_option_chain", {"symbol": "US.AAPL"}))
        ff = {"last_price": True, "volume": True}
        self.assertTrue(futu.handle("option_chain", {"code": "US.AAPL", "field_filter": ff})["ok"])
        self.assertEqual(futu._call.calls[-1],
                         ("quote_option_chain", {"symbol": "US.AAPL", "field_filter": ff}))
        futu = make_futu()
        for bad in ({}, "last_price", []):
            out = futu.handle("option_chain", {"code": "US.AAPL", "field_filter": bad})
            self.assertFalse(out["ok"], bad)
            self.assertEqual(out["error"]["code"], "trading/invalid-operation", bad)
            self.assertEqual(futu._call.count(), 0, bad)

    def test_option_screen_requires_nonempty_field_filter(self):
        for screen in ({}, {"strategy": {"market_category_list": [1]}},
                       {"strategy": {"market_category_list": [1]}, "field_filter": {}},
                       {"strategy": {"market_category_list": [1]}, "field_filter": "last_price"}):
            futu = make_futu()
            out = futu.handle("option_screen", {"filter": screen})
            self.assertFalse(out["ok"], screen)
            self.assertEqual(out["error"]["code"], "trading/invalid-operation", screen)
            self.assertIn("field_filter", out["error"]["message"], screen)
            self.assertEqual(futu._call.count(), 0, screen)

    def test_option_screen_requires_strategy_and_whitelists_inner_keys(self):
        futu = make_futu()
        out = futu.handle("option_screen",
                          {"filter": {"field_filter": {"last_price": True}}})
        self.assertFalse(out["ok"])
        self.assertIn("strategy", out["error"]["message"])
        self.assertEqual(futu._call.count(), 0)
        out = futu.handle("option_screen", {"filter": {
            "strategy": {"market_category_list": [1]}, "field_filter": {"last_price": True},
            "bogus_key": 1}})
        self.assertFalse(out["ok"])
        self.assertIn("bogus_key", out["error"]["message"])
        self.assertEqual(futu._call.count(), 0)
        screen = {"strategy": {"market_category_list": [1], "filter_group_list": []},
                  "field_filter": {"last_price": True, "volume": True},
                  "limit": 50, "sort_obj": {"sort_field": "volume", "is_asc": False}}
        out = futu.handle("option_screen", {"filter": screen})
        self.assertTrue(out["ok"], out)
        self.assertEqual(futu._call.calls, [("quote_option_screen", screen)])

    def test_option_screen_limit_and_next_key_types(self):
        for patch in ({"limit": "50"}, {"limit": 1.5}, {"limit": 1001}, {"next_key": 3}):
            futu = make_futu()
            screen = {"strategy": {"market_category_list": [1]},
                      "field_filter": {"last_price": True}}
            screen.update(patch)
            out = futu.handle("option_screen", {"filter": screen})
            self.assertFalse(out["ok"], patch)
            self.assertEqual(futu._call.count(), 0, patch)

    def test_business_ret_code_error_maps_to_futu_error(self):
        # 通道事实：业务错误可能包在 isError=false 的文本 JSON 里（ret_code!=0），
        # futu_mcp 把它抛成 FutuUnavailable——直通层必须识别成业务错误，不冒充成功也不算不可用。
        futu = make_futu(error=FutuUnavailable(
            "quote_option_screen: ret=-3 strategy.market_category_list[0]: must be an integer"))
        out = futu.handle("option_screen", {"filter": {"field_filter": {"last_price": True},
                                                       "strategy": {"market_category_list": ["US"]}}})
        self.assertFalse(out["ok"])
        error = out["error"]
        self.assertEqual(error["code"], "trading/futu-error")
        self.assertIn("ret_code=-3", error["message"])
        self.assertIn("must be an integer", error["message"])
        self.assertEqual(error["details"], {"ret_code": -3})

    def test_status_error_maps_to_futu_error(self):
        futu = make_futu(error=FutuUnavailable("account_xxx: s=error token 授权已失效"))
        out = futu.handle("capital_flow", {"code": "HK.00700"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/futu-error")

    def test_token_missing_maps_to_unavailable_with_auth_pointer(self):
        futu = make_futu(error=FutuUnavailable("缺少富途 token（未授权）"))
        out = futu.handle("capital_flow", {"code": "HK.00700"})
        self.assertFalse(out["ok"])
        error = out["error"]
        self.assertEqual(error["code"], "trading/futu-unavailable")
        self.assertIn("futu_auth.py", error["message"], "token 缺失必须指向安装/授权入口")

    def test_network_failure_maps_to_unavailable(self):
        futu = make_futu(error=FutuUnavailable("MCP 请求失败：timed out"))
        out = futu.handle("capital_flow", {"code": "HK.00700"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/futu-unavailable")
        self.assertNotIn("futu-error", out["error"]["code"])

    def test_a_share_rt_error_message_carries_alternative_paths(self):
        futu = make_futu(error=FutuUnavailable(
            "quote_stock_quote: ret=-9 realtime quote permission required"))
        out = futu.handle("rt_quote", {"codes": ["SH.600519"]})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/futu-error")
        self.assertIn("A 股实时无权限：可用 capital_flow / history-kline 替代",
                      out["error"]["message"])
        self.assertEqual(out["error"]["details"], {"ret_code": -9})

    def test_unknown_endpoint_rejected(self):
        futu = make_futu()
        out = futu.handle("capital_flow2", {"code": "HK.00700"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")
        self.assertEqual(futu._call.count(), 0)


class FakeFutu:
    """路由替身：记录 (endpoint, payload)，返回固定 envelope。"""

    def __init__(self, envelope=None):
        self.calls = []
        self.envelope = envelope if envelope is not None else {"ok": True, "value": {"echo": 1}}

    def handle(self, endpoint, payload):
        self.calls.append((endpoint, dict(payload)))
        return self.envelope


MIN_PAYLOAD = {
    "rt_quote": {"codes": ["HK.00700"]},
    "rt_order_book": {"code": "HK.00700"},
    "capital_flow": {"code": "HK.00700"},
    "capital_flow_history": {"code": "HK.00700"},
    "capital_distribution": {"code": "HK.00700"},
    "option_expiration": {"code": "HK.00700"},
    "option_chain": {"code": "HK.00700"},
    "option_screen": {"filter": {"strategy": {"market_category_list": [1]},
                                 "field_filter": {"last_price": True}}},
}


class RoutingContractTest(unittest.TestCase):
    """HTTP/MCP 共用的 handle：8 端点白名单、实时零缓存、错误 envelope。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.futu = FakeFutu()
        # staticmethod 包装：类属性存函数会被实例访问绑定成方法（多出 self 实参）
        cls.handler = staticmethod(app_module.create_handler(cls.tmp.name, futu=cls.futu))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def call(self, endpoint, payload):
        return self.handler(endpoint, payload)

    def test_all_eight_endpoints_route_to_futu_provider(self):
        for endpoint, payload in MIN_PAYLOAD.items():
            out = self.call(endpoint, payload)
            self.assertTrue(out["ok"], endpoint)
            self.assertEqual(self.futu.calls[-1], (endpoint, payload), endpoint)

    def test_field_whitelist_rejects_before_provider(self):
        before = len(self.futu.calls)
        out = self.call("rt_quote", {"codes": ["HK.00700"], "ticker": "HK.00700"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")
        self.assertEqual(len(self.futu.calls), before, "白名单外字段不得触达通道")

    def test_unknown_endpoint_stays_unknown(self):
        out = self.call("capital_flow_v2", {"code": "HK.00700"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")

    def test_realtime_queries_never_cached(self):
        before = len(self.futu.calls)
        for _ in range(2):
            out = self.call("capital_flow", {"code": "HK.00700"})
            self.assertTrue(out["ok"])
            self.assertNotIn("cached", out, "实时直通响应不得带缓存字段")
            self.assertNotIn("cached_at", out)
        self.assertEqual(len(self.futu.calls), before + 2, "实时查询连调两次必须两次触达通道")

    def test_error_envelope_passthrough_without_rewrap(self):
        failing = FakeFutu(envelope={"ok": False,
                                     "error": {"code": "trading/futu-error",
                                               "message": "boom", "details": {}}})
        handler = app_module.create_handler(self.tmp.name, futu=failing)
        out = handler("capital_flow", {"code": "HK.00700"})
        self.assertEqual(out, failing.envelope)

    def test_endpoints_registered_in_manifest(self):
        endpoints = store_access.endpoints()
        for endpoint in FUTU_ENDPOINTS:
            self.assertIn(endpoint, endpoints, endpoint)
        self.assertEqual(tuple(store_access.FUTU_ENDPOINTS), FUTU_ENDPOINTS)
        # WP8 任务 2 起 FUTU_TOOLS 覆盖全部 17 个富途直通端点（8 直通 + 9 行情）
        self.assertEqual(tuple(futu_data.FUTU_TOOLS),
                         FUTU_ENDPOINTS + tuple(store_access.WP8_MARKET_ENDPOINTS))

    def test_realtime_endpoints_absent_from_ttl_and_shape_tables(self):
        for endpoint in FUTU_ENDPOINTS:
            self.assertNotIn(endpoint, caches.CACHE_TTL_MS, endpoint)
            self.assertNotIn(endpoint, caches.ENDPOINT_SHAPE, endpoint)
            self.assertEqual(caches.CACHE_TTL_MS.get(endpoint, 0), 0, endpoint)


class ToolSurfaceTest(unittest.TestCase):
    """MCP 工具面 59（WP8 任务 6 起）：8 工具进面、字段同形、描述注明实时直通与 A 股受限。"""

    def test_tool_surface_is_56(self):
        self.assertEqual(mcp_tools.TOOL_COUNT, 59)
        self.assertEqual(len(mcp_tools.TOOLS), 59)
        names = {tool.name for tool in mcp_tools.TOOLS}
        self.assertLessEqual(set(FUTU_ENDPOINTS), names)
        self.assertNotIn("confirm_decide", names)
        self.assertNotIn("confirm-decide", names)

    def test_new_tools_map_to_same_named_endpoints(self):
        for tool in mcp_tools.TOOLS:
            if tool.name in FUTU_ENDPOINTS:
                self.assertEqual(tool.kind, "endpoint", tool.name)
                self.assertEqual(tool.endpoint, tool.name, tool.name)
        self.assertEqual(set(mcp_tools.ENDPOINT_TOOL_ENDPOINTS.values()),
                         set(store_access.endpoints()) - mcp_tools.MCP_EXCLUDED_ENDPOINTS)

    def test_field_sets_match_http_whitelist(self):
        expected_fields = {
            "rt_quote": ("codes",),
            "rt_order_book": ("code",),
            "capital_flow": ("code",),
            "capital_flow_history": ("code", "days"),
            "capital_distribution": ("code",),
            "option_expiration": ("code",),
            "option_chain": ("code", "field_filter"),
            "option_screen": ("filter",),
            # WP8 任务 2：OpenAPI 行情接入的 9 个增量端点（与 tests/test_wp8_market.py
            # 同表；这里只钉这 8 个既有端点的字段不受任务 2 影响，另断言表键集一致）
            "market_snapshot": ("codes",),
            "cur_kline": ("code", "num", "ktype", "autype", "extended_time"),
            "rt_data": ("code", "request_section"),
            "rt_ticker": ("code", "num", "period"),
            # 进 TTL 缓存的 5 个工具带 refresh 旁路参数（同其他缓存工具的房规）
            "info_basicinfo": ("codes", "refresh"),
            "info_trading_days": ("market", "start", "end", "refresh"),
            "info_search": ("keyword", "size", "news_type", "sort_type", "lang", "refresh"),
            "info_market_state": ("codes", "is_contain_ba", "is_contain_overnight", "refresh"),
            "quote_history_kline_v2": ("code", "start", "end", "ktype", "autype",
                                       "num", "extended_time", "refresh"),
        }
        definitions = {tool.name: tool for tool in mcp_tools.TOOLS}
        for name, fields in expected_fields.items():
            self.assertEqual(definitions[name].fields, fields, name)
            # 首字段必填（cur_kline 的 num、quote_history_kline_v2 的 end 等官方必填
            # 字段由 tests/test_wp8_market.py 的 test_required_fields_follow_official_docs 钉）
            self.assertEqual(
                [param.name for param in definitions[name].params if param.required][0],
                fields[0], f"{name} 的首字段必填")
        # HTTP 白名单不含 refresh（载荷层 refresh 映射为 _refresh 旁路）
        http_fields = {name: tuple(f for f in fields if f != "refresh")
                       for name, fields in expected_fields.items()}
        self.assertEqual(list(app_module.FUTU_FIELDS), list(http_fields))
        for name, fields in http_fields.items():
            self.assertEqual(tuple(app_module.FUTU_FIELDS[name]), fields, name)

    def test_descriptions_state_realtime_passthrough_and_a_share_limit(self):
        for tool in mcp_tools.TOOLS:
            if tool.name not in FUTU_ENDPOINTS:
                continue
            self.assertIn("富途实时", tool.description, tool.name)
        for name in ("rt_quote", "rt_order_book"):
            tool = next(tool for tool in mcp_tools.TOOLS if tool.name == name)
            self.assertIn("A 股", tool.description, name)

    def test_upstream_tool_mapping_is_locked(self):
        """endpoint → 富途 MCP 工具名全表（WP8 任务 2 增至 17 键；新 9 键的
        2026-09-16 实测依据见 tests/test_wp8_market.py::test_nine_new_endpoints_route_to_mcp_tools）。"""
        self.assertEqual(futu_data.FUTU_TOOLS, {
            "rt_quote": "quote_stock_quote",
            "rt_order_book": "quote_order_book",
            "capital_flow": "quote_capital_flow",
            "capital_flow_history": "quote_capital_flow_history",
            "capital_distribution": "quote_capital_distribution",
            "option_expiration": "quote_option_expiration_date",
            "option_chain": "quote_option_chain",
            "option_screen": "quote_option_screen",
            "market_snapshot": "quote_market_snapshot",
            "cur_kline": "quote_cur_kline",
            "rt_data": "quote_rt_data",
            "rt_ticker": "quote_rt_ticker",
            "info_basicinfo": "quote_stock_basicinfo",
            "info_trading_days": "quote_trading_days",
            "info_search": "quote_news_search",
            "info_market_state": "quote_market_state",
            "quote_history_kline_v2": "quote_history_kline",
        })

    def test_sdk_signature_accepts_object_and_list_payloads(self):
        """签名即契约：str_list/object 字段能生成签名并分发（不触达真通道）。"""
        calls = []

        def recording(endpoint, payload):
            calls.append((endpoint, dict(payload)))
            return {"ok": True, "value": {}}

        tools = {tool.name: tool for tool in mcp_tools.build_tools(recording, None)}
        result = tools["rt_quote"].call({"codes": ["HK.00700"]})
        self.assertFalse(result.is_error)
        self.assertEqual(mcp_tools.result_payload(result), {"ok": True, "value": {}})
        self.assertEqual(calls[-1], ("rt_quote", {"codes": ["HK.00700"]}))
        screen = {"strategy": {"market_category_list": [1]},
                  "field_filter": {"last_price": True}}
        result = tools["option_screen"].call({"filter": screen})
        self.assertFalse(result.is_error)
        self.assertEqual(calls[-1], ("option_screen", {"filter": dict(screen)}))
        # business 失败（来自 handle 的信封）按 isError=false 正常返回（规格 §3.2 错误语义）
        result = tools["capital_flow"].call({"code": "HK.00700"})
        self.assertFalse(result.is_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
