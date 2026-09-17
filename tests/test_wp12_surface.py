"""WP12 任务 4：富途数据面服务面接线 + MCP 工具面三档。

覆盖（锁定表 `docs/superpowers/plans/wp12-endpoint-lock.md` §C/§D.1 为唯一事实源）：
  * 端点面 16 项（直通 14 + 聚合 2）在 `store_access.endpoints()` 内且载荷白名单齐备；
  * 每端点经 `FutuData.handle` 可达：适配到**正确的数据面方法组方法**与参数（假替身断言）；
  * 聚合端点 section 白名单（f10_detail 26 项取自传输层常量、derivative_detail 4 项）；
  * mcp 通道对数据面端点如实拒绝（不猜上游工具名），openapi 通道缺凭据如实报不可用；
  * 缓存：TTL 15 项、形状仅 4 项文档明示包装键、写类不进表；
  * 工具面三档：直通 11 + 聚合 2 = 13（TOOL_COUNT 74），三端点 HTTP-only 不进工具面，
    「端点工具集 ≡ 端点清单 − 有意排除集」不变式成立。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 数据层取**仓库内**代码（与 platform/server/run.py 同一优先级）：venv 的
# dsh-trading-python.pth 会把 $DSH_HOME/trading-python 的安装副本加进 sys.path，
# 副本滞后会让新增方法组导入不到（WP8 实测踩过），故仓库路径插到最前。
for _dir in (ROOT / "plugins" / "datasource" / "python",
             ROOT / "plugins" / "core" / "python"):
    sys.path.insert(0, str(_dir))
sys.path.insert(0, str(ROOT / "platform"))

from server import app as app_module  # noqa: E402
from server import caches, futu_data, mcp_tools, store_access  # noqa: E402

#: 直通工具（进 MCP 工具面，一工具一端点）
DIRECT_TOOLS = ("stock_screen", "plate_list", "plate_stock", "short_daily_volume",
                "short_interest", "ipo_list", "economic_calendar_hot",
                "economic_calendar_search", "info_owner_plate", "watchlist_list",
                "watchlist_groups")
#: 聚合工具（section 枚举分派，不逐端点铺开）
AGGREGATE_TOOLS = ("f10_detail", "derivative_detail")
#: HTTP-only（进端点面、进排除集、绝不进工具面）
HTTP_ONLY = ("warrant_screen", "modify_user_security", "info_rehab")

#: 每端点最小合法载荷（服务面必填/形状校验的最小满足）
MINIMAL_PAYLOADS = {
    "economic_calendar_hot": {},
    "economic_calendar_search": {"keyword": "CPI", "search_type": 1},
    "info_owner_plate": {"code": "SH.600519"},
    "info_rehab": {"code": "SH.600519"},
    "plate_list": {"market": "HK", "plate_class": "INDUSTRY"},
    "plate_stock": {"plate_code": "HK.LIST23618"},
    "stock_screen": {"screen_queries": [{"simple_field_query": {}}]},
    "warrant_screen": {"market_type": 1},
    "ipo_list": {"market": "hk"},
    "short_daily_volume": {"code": "US.AAPL"},
    "short_interest": {"code": "US.AAPL"},
    "watchlist_list": {"group_name": "全部"},
    "watchlist_groups": {"group_type": "CUSTOM"},
    "modify_user_security": {"op": "add", "code_list": ["HK.00700"]},
    "f10_detail": {"code": "HK.00700", "section": "analyst_consensus"},
    "derivative_detail": {"code": "HK.00700", "section": "reference_future"},
}

#: 端点 → 期望调用的 (方法组属性, 方法名)
EXPECTED_CALLS = {
    "economic_calendar_hot": ("basic", "economic_calendar_hot"),
    "economic_calendar_search": ("basic", "economic_calendar_search"),
    "info_owner_plate": ("basic", "owner_plate"),
    "info_rehab": ("basic", "rehab"),
    "plate_list": ("plate", "plate_list"),
    "plate_stock": ("plate", "plate_stock"),
    "stock_screen": ("screen", "stock_screen"),
    "warrant_screen": ("screen", "warrant_screen"),
    "ipo_list": ("ipo", "ipo_list"),
    "short_daily_volume": ("short", "short_daily_volume"),
    "short_interest": ("short", "short_interest"),
    "watchlist_list": ("watchlist", "watchlist_list"),
    "watchlist_groups": ("watchlist", "watchlist_groups"),
    "modify_user_security": ("watchlist", "modify_user_security"),
    # f10_detail 直调 section 方法（不再经 f10() 分发器）——分发器形参是 **section_params，
    # 没有键契约，正是聚合 params 无法前置校验的原因；section 白名单仍以传输层类常量为
    # 单一事实源（见 test_aggregate_sections_are_whitelisted）。
    "f10_detail": ("f10", "analyst_consensus"),
    "derivative_detail": ("derivatives", "reference_future"),
}


class _Recorder:
    """方法组替身：任何属性访问都返回「记录调用并回一个对象」的可调用。"""

    def __init__(self, calls):
        self._calls = calls

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self._calls.append((name, args, kwargs))
            # 形状表登记过的端点在 value 层缓存前会做最小形状校验——替身回一份
            # 覆盖全部登记形状键的载荷，保证校验通过（多余键无害）。
            return {"ok": 1, "rehabs": [], "plate_list": [], "stock_list": [],
                    "group_list": []}
        return call


class _Groups:
    """DataPlaneGroups 的假件（八方法组各一个记录器）。"""

    def __init__(self):
        self.calls = []
        for attr in ("screen", "plate", "short", "basic", "ipo", "watchlist",
                     "derivatives", "f10"):
            setattr(self, attr, _Recorder(self.calls))


def _data(home, channel=futu_data.CHANNEL_OPENAPI, groups=None, call=None):
    """构造 FutuData：注入数据面替身（openapi 通道）或 mcp 替身。"""
    return futu_data.FutuData(call=call, home=home, channel=channel,
                              dataplane=groups if groups is not None else _Groups())


class DataPlaneEndpointSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.home = str(ROOT / "tests" / "_tmp_wp12_surface")

    def test_endpoint_surface_is_16_and_declared(self):
        self.assertEqual(len(futu_data.DATAPLANE_ENDPOINTS), 16)
        self.assertEqual(tuple(store_access.WP12_ENDPOINTS),
                         tuple(futu_data.DATAPLANE_ENDPOINTS))
        declared = store_access.endpoints()
        for name in futu_data.DATAPLANE_ENDPOINTS:
            self.assertIn(name, declared, name)
        # 端点清单尾部就是 WP12 段（顺序与常量一致）
        self.assertEqual(declared[-16:], list(store_access.WP12_ENDPOINTS))

    def test_http_field_whitelist_covers_every_endpoint(self):
        for name in futu_data.DATAPLANE_ENDPOINTS:
            self.assertIn(name, app_module.FUTU_FIELDS, name)
            self.assertIn(name, futu_data.FutuData._METHODS, name)
        # 白名单外字段一律拒绝（浅白名单在同一层）
        with self.assertRaises(app_module.WorkbenchError):
            app_module._check_fields("stock_screen", {"bogus": 1},
                                     app_module.FUTU_FIELDS["stock_screen"])

    def test_every_endpoint_reaches_the_right_group_method(self):
        for name in futu_data.DATAPLANE_ENDPOINTS:
            groups = _Groups()
            data = _data(self.home, groups=groups)
            # 走 _envelope 而非 handle：绕开 value 层缓存（缓存本身由 WP8/WP6 用例覆盖），
            # 这里钉的是「端点 → 方法组方法」的分派与参数透传。
            envelope = data._envelope(name, MINIMAL_PAYLOADS[name])
            self.assertTrue(envelope.get("ok"), f"{name}: {envelope}")
            self.assertIsInstance(envelope["value"], dict, name)
            self.assertEqual(envelope["value"].get("ok"), 1, name)
            attr, method = EXPECTED_CALLS[name]
            called = [c[0] for c in groups.calls]
            self.assertIn(method, called, f"{name} 未调用 {attr}.{method}（实际 {called}）")
            # 关键必填参数确实传下去了
            args, kwargs = next((c[1], c[2]) for c in groups.calls if c[0] == method)
            if name == "info_owner_plate":
                self.assertEqual(kwargs, {"symbol": "SH.600519"})
            if name == "plate_list":
                self.assertEqual(kwargs, {"market": "HK", "plate_class": "INDUSTRY"})
            if name == "ipo_list":
                self.assertEqual(kwargs, {"market": "hk", "request_type": None})
            if name == "watchlist_list":
                self.assertEqual(kwargs, {"group_name": "全部"})

    def test_aggregate_sections_are_whitelisted(self):
        groups = _Groups()
        data = _data(self.home, groups=groups)
        # derivative_detail：4 项白名单，非法 section 在本地拒绝（零通道调用）
        bad = data._envelope("derivative_detail", {"code": "HK.00700", "section": "nope"})
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], futu_data.PARAM_CODE)
        self.assertEqual(groups.calls, [], "坏 section 不得触达方法组")
        good = data._envelope("derivative_detail",
                              {"code": "HK.00700", "section": "option_volatility"})
        self.assertTrue(good["ok"])
        self.assertEqual(groups.calls[0][0], "option_volatility")
        # f10_detail：section 白名单以传输层 OpenApiF10.SECTIONS 为单一事实源
        from trading_datasource.futu_openapi import OpenApiF10  # noqa: PLC0415
        self.assertEqual(len(OpenApiF10.SECTIONS), 26)
        bad_f10 = data._envelope("f10_detail", {"code": "HK.00700", "section": "nope"})
        self.assertFalse(bad_f10["ok"])
        self.assertEqual(bad_f10["error"]["code"], futu_data.PARAM_CODE)
        for section in sorted(OpenApiF10.SECTIONS):
            envelope = data._envelope("f10_detail", {"code": "HK.00700", "section": section})
            self.assertTrue(envelope["ok"], f"{section}: {envelope}")

    def test_required_fields_are_enforced_locally(self):
        data = _data(self.home)
        for name, payload in (("plate_list", {"market": "HK"}),
                              ("stock_screen", {}),
                              ("modify_user_security", {"op": "add"}),
                              ("economic_calendar_search", {"keyword": "CPI"}),
                              ("short_daily_volume", {})):
            envelope = data._envelope(name, payload)
            self.assertFalse(envelope["ok"], name)
            self.assertEqual(envelope["error"]["code"], futu_data.PARAM_CODE, name)

    def test_mcp_channel_reports_dataplane_unsupported(self):
        calls = []

        def fake_call(tool, arguments, timeout=None, auto_refresh=None):
            calls.append((tool, arguments))
            return {}

        data = _data(self.home, channel=futu_data.CHANNEL_MCP, call=fake_call)
        for name in futu_data.DATAPLANE_ENDPOINTS:
            envelope = data._envelope(name, MINIMAL_PAYLOADS[name])
            self.assertFalse(envelope["ok"], name)
            self.assertEqual(envelope["error"]["code"], futu_data.UNAVAILABLE_CODE, name)
            self.assertIn("openapi", envelope["error"]["message"], name)
        self.assertEqual(calls, [], "mcp 通道不得对数据面端点发起任何上游调用")

    def test_openapi_channel_without_credentials_is_reported(self):
        data = futu_data.FutuData(home=self.home, channel=futu_data.CHANNEL_OPENAPI,
                                  credential_path=str(ROOT / "tests" / "_no_creds.json"))
        envelope = data._envelope("plate_list", {"market": "HK", "plate_class": "ALL"})
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], futu_data.OPENAPI_UNAVAILABLE_CODE)


class _StrictF10:
    """带真实签名的 F10 替身：让聚合端点的 params 键校验有契约可依（并记录调用）。"""

    def __init__(self, calls):
        self._calls = calls

    def analyst_consensus(self, symbol, lang=None, limit=None):
        self._calls.append(("analyst_consensus", symbol, {"lang": lang, "limit": limit}))
        return {"ok": 1}


class _StrictDerivatives:
    """带真实签名的衍生品替身（同上）。"""

    def __init__(self, calls):
        self._calls = calls

    def option_volatility(self, symbol, count=None):
        self._calls.append(("option_volatility", symbol, {"count": count}))
        return {"ok": 1}


class _StrictGroups:
    """只实现被测聚合路径所需方法的替身（签名与真实方法组一致）。"""

    def __init__(self):
        self.calls = []
        self.f10 = _StrictF10(self.calls)
        self.derivatives = _StrictDerivatives(self.calls)


class DataPlaneAggregateParamTests(unittest.TestCase):
    """聚合端点 ``params`` 的键契约：载荷错误不得被归类成「通道不可用」。

    审查实证：未经校验的 params 展开进传输层方法 → TypeError → 被 _fetch_openapi 的
    兜底 except 归成 ``trading/futu-unavailable``，界面据此把用户引向「去设置页配凭据」，
    而真实原因是参数拼写错误（应为 ``trading/invalid-operation``）。
    """

    def setUp(self):
        self.home = str(ROOT / "tests" / "_tmp_wp12_surface")

    def _data(self, groups=None):
        return futu_data.FutuData(home=self.home, channel=futu_data.CHANNEL_OPENAPI,
                                  dataplane=groups if groups is not None else _StrictGroups())

    def test_f10_unknown_param_is_invalid_operation(self):
        groups = _StrictGroups()
        envelope = self._data(groups)._envelope(
            "f10_detail", {"code": "HK.00700", "section": "analyst_consensus",
                           "params": {"bogus_key": 1}})
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], futu_data.PARAM_CODE)
        self.assertIn("bogus_key", envelope["error"]["message"])
        self.assertEqual(groups.calls, [], "坏 params 不得触达方法组")

    def test_derivative_unknown_param_is_invalid_operation(self):
        groups = _StrictGroups()
        envelope = self._data(groups)._envelope(
            "derivative_detail", {"code": "HK.00700", "section": "option_volatility",
                                  "params": {"bogus_key": 1}})
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], futu_data.PARAM_CODE)
        self.assertIn("bogus_key", envelope["error"]["message"])
        self.assertEqual(groups.calls, [], "坏 params 不得触达方法组")

    def test_aggregate_params_must_be_object(self):
        for endpoint, section in (("f10_detail", "analyst_consensus"),
                                  ("derivative_detail", "option_volatility")):
            envelope = self._data()._envelope(
                endpoint, {"code": "HK.00700", "section": section, "params": [1]})
            self.assertFalse(envelope["ok"], endpoint)
            self.assertEqual(envelope["error"]["code"], futu_data.PARAM_CODE, endpoint)

    def test_valid_aggregate_params_are_passed_through(self):
        groups = _StrictGroups()
        envelope = self._data(groups)._envelope(
            "f10_detail", {"code": "HK.00700", "section": "analyst_consensus",
                           "params": {"limit": 5}})
        self.assertTrue(envelope["ok"], envelope)
        self.assertEqual(groups.calls,
                         [("analyst_consensus", "HK.00700", {"lang": None, "limit": 5})])

    def test_channel_really_unavailable_stays_unavailable(self):
        """反证：真正不可用（无凭据）仍归 openapi-unavailable，不被参数校验吞掉。"""
        data = futu_data.FutuData(home=self.home, channel=futu_data.CHANNEL_OPENAPI,
                                  credential_path=str(ROOT / "tests" / "_no_creds.json"))
        envelope = data._envelope("f10_detail", {"code": "HK.00700",
                                                 "section": "analyst_consensus",
                                                 "params": {"limit": 5}})
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], futu_data.OPENAPI_UNAVAILABLE_CODE)


class DataPlaneMethodBindingTests(unittest.TestCase):
    """方法组公开方法必须全部有锁定绑定（防内联字面量路径绕过锁定表/注册表）。"""

    def test_every_group_public_method_is_bound(self):
        from trading_datasource import futu_openapi as fo  # noqa: PLC0415
        for attr, expected in futu_data.DATAPLANE_GROUP_METHODS.items():
            cls = getattr(fo, futu_data.DATAPLANE_GROUP_CLASSES[attr])
            public = {name for name in dir(cls)
                      if not name.startswith("_") and callable(getattr(cls, name))}
            self.assertEqual(public, set(expected), attr)

    def test_f10_sections_cover_every_section_method(self):
        cls = futu_data._f10_class()
        public = {name for name in dir(cls)
                  if not name.startswith("_") and callable(getattr(cls, name))}
        self.assertEqual(public - {"f10"}, set(cls.SECTIONS))

    def test_derivative_sections_cover_every_method(self):
        from trading_datasource.futu_openapi import OpenApiDerivatives  # noqa: PLC0415
        public = {name for name in dir(OpenApiDerivatives)
                  if not name.startswith("_") and callable(getattr(OpenApiDerivatives, name))}
        self.assertEqual(public, set(futu_data.DERIVATIVE_SECTIONS.values()))


class DataPlaneCacheTests(unittest.TestCase):
    def test_ttl_and_shape_registration(self):
        cached = set(futu_data.CACHED_FUTU_ENDPOINTS)
        for name in futu_data.DATAPLANE_ENDPOINTS:
            if name == "modify_user_security":
                self.assertNotIn(name, caches.CACHE_TTL_MS)
                self.assertNotIn(name, cached)
                continue
            self.assertIn(name, caches.CACHE_TTL_MS, name)
            self.assertIn(name, cached, name)
        self.assertEqual(caches.CACHE_TTL_MS["plate_list"], 6 * 60 * 60_000)
        self.assertEqual(caches.CACHE_TTL_MS["short_interest"], 60 * 60_000)
        self.assertEqual(caches.CACHE_TTL_MS["stock_screen"], 5 * 60_000)
        # 形状只登记文档明示包装键的 4 项，且这些键必须真的出现在合法响应里
        # （形状写错的后果是端点恒判「载荷不完整」——这里用形状合法载荷反向钉住）
        wp12_shapes = {name: caches.ENDPOINT_SHAPE[name] for name in
                       futu_data.DATAPLANE_ENDPOINTS if name in caches.ENDPOINT_SHAPE}
        self.assertEqual(wp12_shapes, {"plate_list": ["plate_list"],
                                       "plate_stock": ["stock_list"],
                                       "info_rehab": ["rehabs"],
                                       "watchlist_groups": ["group_list"]})
        for name, fields in wp12_shapes.items():
            payload = {field: [] for field in fields}
            self.assertTrue(caches.matches_shape(name, payload), name)


class DataPlaneToolSurfaceTests(unittest.TestCase):
    def test_three_tiers_and_budget(self):
        self.assertEqual(mcp_tools.TOOL_COUNT, 74)
        self.assertEqual(len(mcp_tools.TOOLS), 74)
        names = set(mcp_tools.TOOL_NAMES)
        for name in DIRECT_TOOLS + AGGREGATE_TOOLS:
            self.assertIn(name, names, name)
        for name in HTTP_ONLY:
            self.assertNotIn(name, names, name)
            self.assertIn(name, mcp_tools.MCP_EXCLUDED_ENDPOINTS, name)
        # 端点工具集 ≡ 端点清单 − 有意排除集（对等性）
        forwarded = set(mcp_tools.ENDPOINT_TOOL_ENDPOINTS.values())
        self.assertEqual(forwarded,
                         set(store_access.endpoints()) - set(mcp_tools.MCP_EXCLUDED_ENDPOINTS))
        # 预算硬约束
        self.assertLessEqual(mcp_tools.TOOL_COUNT, 80)

    def test_tool_fields_match_http_whitelist(self):
        definitions = {tool.name: tool for tool in mcp_tools.TOOLS}
        for name in DIRECT_TOOLS + AGGREGATE_TOOLS:
            declared = list(app_module.FUTU_FIELDS[name])
            fields = [f for f in definitions[name].fields if f != "refresh"]
            # 工具字段 = HTTP 白名单逐项同形（外加进缓存工具带的 refresh 旁路）
            self.assertEqual(fields, declared, name)

    def test_aggregate_tool_descriptions_name_every_section(self):
        from trading_datasource.futu_openapi import OpenApiF10  # noqa: PLC0415
        description = {tool.name: tool.description for tool in mcp_tools.TOOLS}
        for section in OpenApiF10.SECTIONS:
            self.assertIn(section, description["f10_detail"], section)
        for section in futu_data.DERIVATIVE_SECTIONS:
            self.assertIn(section, description["derivative_detail"], section)


if __name__ == "__main__":
    unittest.main()
