"""``--dataplane`` 逐方法真机自检的离线测试（scripts/futu_openapi_check.py）。

审查问题 3：规格 §7.4 要求「每个新方法真机连通性自检」，而脚本此前只有单请求
``--method/--path/--body`` 形态——53 个新端点没有逐方法入口。本文件钉住：
端点覆盖（锁定表 §C.1–C.7，排除需 OAuth 用户登录态的自选 3 项）、分类语义
（``-10`` → no_data 而非 failed；业务码如实分类）、未配置凭据如实报错且退出非零。
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from trading_datasource import futu_openapi as fo  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "futu_openapi_check", ROOT / "scripts" / "futu_openapi_check.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


#: 假 client 的缺省返回值：传输层 `request()` 返回的是**已解包的价值对象**（信封由
#: `parse_envelope` 在客户端内处理），因此这里回价值形状而非原始信封。
DEFAULT_VALUE = {"ok": 1, "plate_list": [], "stock_list": [], "items": [],
                 "news_list": [], "rehabs": [], "group_list": []}


class FakeClient:
    """按路径回放价值对象/异常；记录请求路径（离线驱动分类逻辑）。

    同时实现 ``request`` 与 ``request_meta``——筛选/板块/经济日历搜索等方法的信封顶层
    ``pagination`` 经后者取回。
    """

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.paths = []

    def _value(self, path):
        outcome = self.responses.get(path, DEFAULT_VALUE)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def request(self, method, path, query=None, json_body=None):
        self.paths.append(path)
        return self._value(path)

    def request_meta(self, method, path, query=None, json_body=None):
        self.paths.append(path)
        return self._value(path), None


def _run(client=None, argv=("--dataplane", "--json"), credential_path=None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = check.run_dataplane(list(argv), client=client, credential_path=credential_path)
    return code, out.getvalue()


class DataplaneItemsTest(unittest.TestCase):
    def test_items_cover_lock_table_excluding_watchlist(self):
        """锁定表 §C.1–C.4 + §C.5 的 26 个 F10 section + §C.6 + §C.7 的 4 项。"""
        names = [name for name, _call in check.dataplane_items(FakeClient())]
        # 11 个单方法端点（9 项直通非自选 + info_rehab + warrant_screen）
        # + F10 的 26 个 section + 衍生品 4 项
        self.assertEqual(len(names), 11 + 26 + 4)
        for expected in ("economic_calendar_hot", "economic_calendar_search",
                         "info_owner_plate", "info_rehab", "plate_list", "plate_stock",
                         "stock_screen", "warrant_screen", "ipo_list",
                         "short_daily_volume", "short_interest",
                         "derivative.future_info", "derivative.reference_future",
                         "derivative.option_volatility",
                         "derivative.option_exercise_probability"):
            self.assertIn(expected, names, expected)
        for section in fo.OpenApiF10.SECTIONS:
            self.assertIn(f"f10.{section}", names, section)
        # §C.8 自选三项需 OAuth 用户登录态；§C.9 模拟交易属 WP13——都不在本自检内
        for excluded in ("watchlist_list", "watchlist_groups", "modify_user_security"):
            self.assertNotIn(excluded, names)

    def test_every_item_is_callable_with_minimal_args(self):
        """最小合法参数本地校验可通过（不因缺参在本地就 ValueError）。"""
        client = FakeClient()
        for name, call in check.dataplane_items(client):
            outcome = check.call_item(call)
            self.assertIn(outcome["class"], ("ok", "no_data", "business"), f"{name}: {outcome}")


class DataplaneClassificationTest(unittest.TestCase):
    def test_ok_no_data_business_are_distinguished(self):
        client = FakeClient({
            "/api/v1.0/quote/plate-list": {"ret_code": 0, "data": {"plate_list": []}},
            "/api/v1.0/quote/US.AAPL/short/daily-volume":
                fo.OpenApiError("no data", errcode=-10),
            "/api/v1.0/quote/economic-calendar/hot":
                fo.OpenApiError("invalid parameter", errcode=-3),
        })
        code, raw = _run(client)
        payload = json.loads(raw)
        by_name = {item["name"]: item for item in payload["items"]}
        self.assertEqual(by_name["plate_list"]["class"], "ok")
        self.assertEqual(by_name["short_daily_volume"]["class"], "no_data")
        self.assertEqual(by_name["economic_calendar_hot"]["class"], "business")
        self.assertEqual(by_name["economic_calendar_hot"]["code"], -3)
        self.assertGreaterEqual(payload["counts"]["no_data"], 1)
        self.assertEqual(code, 0, "no_data / business 都算「通道可达」，不影响退出码")

    def test_transport_failure_is_unavailable_and_fails_the_run(self):
        client = FakeClient({
            "/api/v1.0/quote/plate-list": fo.TransportError("connection reset"),
        })
        code, raw = _run(client)
        payload = json.loads(raw)
        by_name = {item["name"]: item for item in payload["items"]}
        self.assertEqual(by_name["plate_list"]["class"], "unavailable")
        self.assertEqual(payload["counts"]["unavailable"], 1)
        self.assertEqual(code, 1)

    def test_local_parameter_rejection_is_param_and_fails_the_run(self):
        """自检自身的参数错误（本地 ValueError）必须显式失败，不得混成业务码。"""
        broken = [("broken", lambda: (_ for _ in ()).throw(ValueError("本地拒绝")))]
        original = check.dataplane_items
        check.dataplane_items = lambda _client, _option=None: broken
        try:
            code, raw = _run(FakeClient())
        finally:
            check.dataplane_items = original
        payload = json.loads(raw)
        self.assertEqual(payload["items"][0]["class"], "param")
        self.assertEqual(code, 1)


class DataplaneCredentialsTest(unittest.TestCase):
    def test_missing_credentials_reports_unconfigured_and_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "futu-openapi.json"  # 不存在 = 未配置
            code, raw = _run(client=None, credential_path=str(missing))
        payload = json.loads(raw)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["stage"], "credentials")
        self.assertIn("未配置", payload["error"])
        self.assertEqual(code, 2)

    def test_empty_credential_object_also_counts_as_unconfigured(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "futu-openapi.json"
            empty.write_text("{}\n", encoding="utf-8")
            code, raw = _run(client=None, credential_path=str(empty))
        self.assertEqual(json.loads(raw)["stage"], "credentials")
        self.assertEqual(code, 2)


class DataplaneHumanOutputTest(unittest.TestCase):
    def test_text_mode_lists_every_item_with_class_and_code(self):
        code, text = _run(FakeClient(), argv=("--dataplane",))
        self.assertEqual(code, 0)
        self.assertIn("plate_list", text)
        self.assertIn("ok=", text)
        self.assertEqual(len(text.strip().splitlines()) >= 43, True, "每项一行 + 汇总")


if __name__ == "__main__":
    unittest.main()
