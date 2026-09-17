"""聚合端点的 section 参数装配（E2E 缺陷 1 + 追加 I1，2026-09-17）。

E2E 原始证据：
  * ``derivative_detail {code, section:"future_info"}`` → ``trading/invalid-operation``
    「code_list 必须是 1..400 个标的代码的列表」——适配层把**单标的 code** 传给了
    需要**列表**的传输层方法，该 section 永远失败；
  * ``f10_detail {section:"top_brokers_history"}``（缺 `days_before`）→
    ``trading/futu-unavailable``「OpenAPI 通道不可用：missing 1 required positional
    argument」——**调用方载荷错误被报成通道故障**（用户被引去设置页配凭据）。

因此本文件的断言是「逐 section 出站装配正确 + 缺必填报 invalid-operation 且零通道调用」，
覆盖全部 26 + 4 个 section，防同类漏配再发生。
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _dir in (ROOT / "plugins" / "datasource" / "python",
             ROOT / "plugins" / "core" / "python"):
    sys.path.insert(0, str(_dir))
sys.path.insert(0, str(ROOT / "platform"))

from server import futu_data  # noqa: E402

#: 各 section 必填附加参数的合法样例值（与传输层签名内省结果对照，不写死名单）
SAMPLE_REQUIRED = {"days_before": 30, "leader_name": "张三"}


class _Recorder:
    def __init__(self, calls):
        self._calls = calls

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self._calls.append((name, args, kwargs))
            return {"ok": 1, "rehinds": []}
        return call


class _Groups:
    def __init__(self):
        self.calls = []
        for attr in ("screen", "plate", "short", "basic", "ipo", "watchlist",
                     "derivatives", "f10"):
            setattr(self, attr, _Recorder(self.calls))


class SectionAssemblyTests(unittest.TestCase):
    def setUp(self):
        # 每次用**全新临时 home**：服务的 TTL 缓存有磁盘层（caches.py，条目按 home 落盘），
        # 固定 home 会把替身返回值缓存下来，使后续运行命中缓存、绕过通道调用（假绿）。
        self.home = tempfile.mkdtemp(prefix="wp12_section_params_")
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def _data(self, groups):
        return futu_data.FutuData(call=None, home=self.home,
                                  channel=futu_data.CHANNEL_OPENAPI, dataplane=groups)

    # ---- 逐 section 出站装配 ----

    def test_every_f10_section_places_code_in_first_positional(self):
        required = futu_data.section_required_params()["f10"]
        sections = sorted(futu_data._f10_class().SECTIONS)
        self.assertEqual(sorted(required), sections, "必填参数表必须覆盖全部 section")
        for section in sections:
            with self.subTest(section=section):
                groups = _Groups()
                params = {name: SAMPLE_REQUIRED[name] for name in required[section]}
                payload = {"code": "HK.00700", "section": section, "_refresh": True}
                if params:
                    payload["params"] = params
                envelope = self._data(groups).handle("f10_detail", payload)
                self.assertTrue(envelope["ok"], envelope)
                method, args, kwargs = groups.calls[-1]
                self.assertEqual(method, section)
                self.assertEqual(args, ())
                # 第一个位置参数承载标的：单标的 section 为 symbol
                self.assertEqual(kwargs.pop("symbol"), "HK.00700")
                for name, value in params.items():
                    self.assertEqual(kwargs.get(name), value, name)

    def test_every_derivative_section_places_code_correctly(self):
        required = futu_data.section_required_params()["derivatives"]
        for section in sorted(futu_data.DERIVATIVE_SECTIONS_VALUES):
            with self.subTest(section=section):
                groups = _Groups()
                payload = {"code": "HK.00700", "section": section, "_refresh": True}
                params = {name: SAMPLE_REQUIRED[name] for name in required[section]}
                if params:
                    payload["params"] = params
                envelope = self._data(groups).handle("derivative_detail", payload)
                self.assertTrue(envelope["ok"], envelope)
                method, args, kwargs = groups.calls[-1]
                self.assertEqual(method, section)
                self.assertEqual(args, ())
                if method == "future_info":
                    # 批量方法：单标的 code 包成列表（缺陷 1 的回归钉）
                    self.assertEqual(kwargs.pop("code_list"), ["HK.00700"])
                else:
                    self.assertEqual(kwargs.pop("symbol"), "HK.00700")
                self.assertEqual(kwargs, {}, "不应出现多余实参")

    # ---- 缺必填 → invalid-operation 且零通道调用（I1 的回归钉）----

    def test_missing_required_section_param_is_invalid_operation(self):
        for section, missing in (("top_brokers_history", "days_before"),
                                 ("company_executive_background", "leader_name")):
            with self.subTest(section=section):
                groups = _Groups()
                envelope = self._data(groups).handle(
                            "f10_detail", {"code": "HK.00700", "section": section,
                                   "_refresh": True})
                self.assertFalse(envelope["ok"])
                self.assertEqual(envelope["error"]["code"], "trading/invalid-operation",
                                 envelope)
                self.assertIn(missing, envelope["error"]["message"])
                self.assertEqual(groups.calls, [], "缺必填必须零通道调用")

    def test_supplied_required_param_passes(self):
        groups = _Groups()
        envelope = self._data(groups).handle(
            "f10_detail", {"code": "HK.00700", "section": "top_brokers_history",
                           "params": {"days_before": 30}, "_refresh": True})
        self.assertTrue(envelope["ok"], envelope)
        self.assertEqual(groups.calls[-1][2].get("days_before"), 30)

    def test_unknown_section_param_is_invalid_operation_without_channel_call(self):
        groups = _Groups()
        envelope = self._data(groups).handle(
            "f10_detail", {"code": "HK.00700", "section": "analyst_consensus",
                           "params": {"bogus": 1}, "_refresh": True})
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "trading/invalid-operation")
        self.assertEqual(groups.calls, [])

    def test_first_positional_named_code_list_is_wrapped_not_passed_raw(self):
        """缺陷 1 的最小复现：future_info 收到的是列表而不是裸字符串。"""
        groups = _Groups()
        self._data(groups).handle("derivative_detail",
                                  {"code": "HK.00700", "section": "future_info",
                                   "_refresh": True})
        _, _, kwargs = groups.calls[-1]
        self.assertEqual(kwargs, {"code_list": ["HK.00700"]})


class ToolDescriptionDiscoverabilityTests(unittest.TestCase):
    """必填信息必须在工具描述里可发现（否则模型只能靠试错）。"""

    def _description(self, tool_name):
        from server import mcp_tools
        for definition in mcp_tools.TOOLS:
            if definition.name == tool_name:
                return definition.description
        raise AssertionError(f"工具不存在：{tool_name}")

    def test_f10_description_names_required_section_params(self):
        text = self._description("f10_detail")
        required = futu_data.section_required_params()["f10"]
        for section, names in required.items():
            for name in names:
                self.assertIn(name, text, f"{section} 的必填参数 {name} 未在描述中出现")
                self.assertIn(section, text, f"{section} 未在描述中出现")

    def test_derivative_description_matches_service_contract(self):
        text = self._description("derivative_detail")
        # future_info 由服务按单标的 code 装配成 code_list——描述不应再教模型传 code_list
        self.assertNotIn("params.code_list", text)


if __name__ == "__main__":
    unittest.main()
