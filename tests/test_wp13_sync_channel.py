"""WP13 任务 1 单测：通道分派助手 + sync 五项迁 OpenAPI（全离线）。

覆盖三类：
  1. **通道判定**（``channel.channel_of``/``openapi_ready``）及其在 server/core 两侧的委托
     一致性——同一个答案，不再需要人工同步的两份镜像；
  2. **分派语义**：openapi 优先 / 无凭据回退 mcp 且标注 / mcp 通道不被注入的 client 影响 /
     openapi 失败**不静默换通道** / 两通道失败上抛 / 未知方法本地拒绝；
  3. **双通道产出同形**：五项取数（复权/财报/估值/分红/经济日历）同一份假数据经两通道 →
     落库行或输出行逐字段相等（落库口径零变化），含 announced_at 语义。

假件手法沿用 ``tests/test_wp12_transport.py``：传输层 ``request`` 返回的是**已解包的
价值对象**（信封由 ``parse_envelope`` 在客户端内处理），故假 client 回放价值形状。
"""
import datetime as _dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "workbench" / "python"))
sys.path.insert(0, str(ROOT / "platform"))

from trading_datasource import channel  # noqa: E402
from trading_datasource.futu_openapi import OpenApiError, paths  # noqa: E402
from trading_core import factors, store, sync  # noqa: E402

import events as events_mod  # noqa: E402  （workbench 脚本：分红/经济日历）

# ---------------------------------------------------------------- 假数据（两通道同形）

REHAB = {"rehabs": [
    {"ex_div_date": "2026-06-20", "cum_forward_adj_factorA": 12.34,
     "cum_backward_adj_factorA": 0.98, "action_types": ["DIVIDEND"]},
    {"ex_div_date": "2025-06-20", "cum_forward_adj_factorA": 12.10,
     "cum_backward_adj_factorA": 0.97, "action_types": ["DIVIDEND"]}]}

STATEMENTS = {"report_list": [
    {"date_time": 1782748800000, "financial_type": 2, "fiscal_year": 2026,
     "item_list": [{"display_name": "Total Operating Revenue", "data": 37575159697.98},
                   {"display_name": "Net Profit", "data": 1890123456.78},
                   {"display_name": "Gross Profit", "data": 3012345678.9},
                   {"display_name": "Diluted EPS", "data": 15.06}]}]}

VALUATION_BY_TYPE = {
    1: {"trend": {"current_value": 21.5, "valuation_percentile": 33.0}},
    2: {"trend": {"current_value": 3.25, "valuation_percentile": 41.5}},
    3: {"trend": {"current_value": 6.75, "valuation_percentile": 55.5}}}

DIVIDENDS = {"dividend_list": [
    {"ex_date": "2026/06/20", "pub_date": "2026/03/28", "dividend_per_share": 2.5,
     "currency": "CNY", "fiscal_year": 2025, "process": "实施"},
    {"ex_date": "2025/06/20", "pub_date": "2025/03/28", "dividend_per_share": 2.0,
     "currency": "CNY", "fiscal_year": 2024, "process": "实施"}]}

CALENDAR = {"economic_calendar_list": [
    {"date": "2026-09-16", "title": "美国 CPI 年率", "previous": "2.9", "predictive": "3.0"},
    {"date": "2026-09-17", "event": "美联储议息", "previous": "5.0", "predictive": "4.75"}]}

SYMBOL = "SH.600519"


class RecordingClient:
    """按路径回放价值对象并记录调用（与 WP12 传输层测试同口径）。

    ``responses`` 的值可以是 dict，也可以是 ``callable(query) -> dict``（估值端点按
    ``valuation_type`` 查询参数区分 3 次调用）。
    """

    def __init__(self, responses=None, default=None):
        self.responses = dict(responses or {})
        self.default = {} if default is None else default
        self.calls = []

    def _reply(self, path, query):
        out = self.responses.get(path, self.default)
        return out(query) if callable(out) else out

    def request(self, method, path, query=None, json_body=None):
        self.calls.append((method, path, query))
        out = self._reply(path, query)
        if isinstance(out, Exception):
            raise out
        return out

    def request_meta(self, method, path, query=None, json_body=None):
        self.calls.append((method, path, query))
        out = self._reply(path, query)
        if isinstance(out, Exception):
            raise out
        return out, None


def _home(root, futu_channel=None, raw=None):
    """临时 home：写 trading-platform.json（``raw`` 优先，便于测坏 JSON）。"""
    path = Path(root) / "trading-platform.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    elif futu_channel is not None:
        path.write_text(json.dumps({"futu_channel": futu_channel}), encoding="utf-8")
    return str(root)


def _no_credentials(root):
    """指向一个**不存在**的凭据文件：回退用例的确定性（不读本机真实凭据）。"""
    return str(Path(root) / "absent-credentials.json")


class ChannelOfTests(unittest.TestCase):
    def test_defaults_and_legal_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(channel.channel_of(tmp), "mcp")  # 缺文件
            self.assertEqual(channel.channel_of(_home(tmp, "openapi")), "openapi")
            self.assertEqual(channel.channel_of(_home(tmp, "mcp")), "mcp")
            self.assertEqual(channel.channel_of(_home(tmp, raw=json.dumps({"x": 1}))), "mcp")
            self.assertEqual(channel.channel_of(_home(tmp, "OPENAPI")), "mcp")  # 大小写敏感

    def test_bad_json_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = _home(tmp, raw="{not json")
            with self.assertRaises(ValueError):
                channel.channel_of(home)

    def test_server_and_core_delegate_to_the_same_answer(self):
        """两侧入口都委托共享实现：通道判定只有一个答案（不再是两份镜像）。"""
        from server import futu_data
        from trading_core import research_sync
        with tempfile.TemporaryDirectory() as tmp:
            for value in ("openapi", "mcp"):
                home = _home(tmp, value)
                self.assertEqual(futu_data.load_channel(home), channel.channel_of(home))
            cred = Path(tmp) / "cred.json"
            cred.write_text(json.dumps({"mode": "oauth", "access_token": "t"}), encoding="utf-8")
            for path in (str(cred), _no_credentials(tmp)):
                self.assertEqual(futu_data.openapi_ready(path),
                                 channel.openapi_ready(path))
                self.assertEqual(research_sync.openapi_ready(path),
                                 channel.openapi_ready(path))


class FetchDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        # 两个 home 必须不同目录：同目录会互相覆盖通道配置
        self.dir_mcp = Path(self.root) / "mcp"
        self.dir_api = Path(self.root) / "api"
        self.dir_mcp.mkdir()
        self.dir_api.mkdir()
        self.home_mcp = _home(self.dir_mcp, "mcp")
        self.home_api = _home(self.dir_api, "openapi")

    def tearDown(self):
        self.tmp.cleanup()

    def test_openapi_preferred_when_channel_openapi(self):
        client = RecordingClient({paths.REHAB_PATH.format(symbol=SYMBOL): REHAB})
        data, used = channel.fetch("quote_corporate_actions_rehab",
                                   {"symbol": SYMBOL, "divi_mode": "include_divi"},
                                   method="basic.rehab", home=self.home_api,
                                   client=client)
        self.assertEqual(used, "openapi")
        self.assertEqual(data, REHAB)
        self.assertEqual(client.calls, [("GET", paths.REHAB_PATH.format(symbol=SYMBOL),
                                         {"divi_mode": "include_divi"})])

    def test_openapi_params_override_mcp_params(self):
        """参数形状差异由调用点显式给出：openapi_params 覆盖 params。"""
        client = RecordingClient({paths.ECONOMIC_CALENDAR_HOT_PATH: CALENDAR})
        seen = {}

        def fake_mcp(tool, params):
            seen.update({"tool": tool, "params": params})
            return {}

        _, used = channel.fetch("quote_economic_calendar_hot", {"date": "20260916"},
                                method="basic.economic_calendar_hot",
                                openapi_params={"date": "2026-09-16"},
                                home=self.home_api, client=client,
                                mcp_call=fake_mcp)
        self.assertEqual(used, "openapi")
        self.assertEqual(seen, {})  # 未走 mcp
        self.assertEqual(client.calls[0][2], {"date": "2026-09-16"})

    def test_fallback_to_mcp_when_credentials_absent(self):
        client = RecordingClient({paths.REHAB_PATH.format(symbol=SYMBOL): REHAB})
        seen = {}

        def fake_mcp(tool, params):
            seen.update({"tool": tool, "params": params})
            return REHAB

        data, used = channel.fetch("quote_corporate_actions_rehab", {"symbol": SYMBOL},
                                   method="basic.rehab", home=self.home_api,
                                   client=None, credential_path=_no_credentials(self.root),
                                   mcp_call=fake_mcp)
        self.assertEqual(used, "mcp(fallback)")
        self.assertEqual(data, REHAB)
        self.assertEqual(seen["tool"], "quote_corporate_actions_rehab")
        self.assertEqual(client.calls, [])  # 明知无凭据不发注定失败的 REST 请求

    def test_mcp_channel_ignores_injected_client(self):
        """通道配置是开关：``futu_channel=mcp`` 时注入的 client 不得被使用。"""
        client = RecordingClient({paths.REHAB_PATH.format(symbol=SYMBOL): REHAB})
        data, used = channel.fetch("quote_corporate_actions_rehab", {"symbol": SYMBOL},
                                   method="basic.rehab", home=self.home_mcp,
                                   client=client,
                                   mcp_call=lambda tool, params: {"rehabs": []})
        self.assertEqual(used, "mcp")
        self.assertEqual(data, {"rehabs": []})
        self.assertEqual(client.calls, [])

    def test_openapi_failure_does_not_silently_switch_channel(self):
        """REST 失败**如实上抛**：静默换通道会把权限/参数/限频问题伪装成 MCP 行为。"""
        boom = OpenApiError("invalid_parameter", errcode=-3)
        client = RecordingClient({paths.REHAB_PATH.format(symbol=SYMBOL): boom})
        called = {"mcp": 0}

        def fake_mcp(tool, params):
            called["mcp"] += 1
            return REHAB

        with self.assertRaises(OpenApiError):
            channel.fetch("quote_corporate_actions_rehab", {"symbol": SYMBOL},
                          method="basic.rehab", home=self.home_api,
                          client=client, mcp_call=fake_mcp)
        self.assertEqual(called["mcp"], 0)

    def test_both_channels_fail_raises(self):
        def boom(tool, params):
            raise RuntimeError("mcp down")

        with self.assertRaises(RuntimeError):
            channel.fetch("quote_corporate_actions_rehab", {"symbol": SYMBOL},
                          method="basic.rehab", home=self.home_mcp,
                          mcp_call=boom)

    def test_unknown_method_rejected_locally(self):
        client = RecordingClient({paths.REHAB_PATH.format(symbol=SYMBOL): REHAB})
        for bad in ("rehab", "nope.rehab", "basic.nope"):
            with self.subTest(method=bad):
                with self.assertRaises(ValueError):
                    channel.fetch("quote_corporate_actions_rehab", {"symbol": SYMBOL},
                                  method=bad, home=self.home_api, client=client)
        self.assertEqual(client.calls, [])  # 本地拒绝：零网络往返

    def test_adapter_normalizes_when_shapes_differ(self):
        client = RecordingClient({paths.REHAB_PATH.format(symbol=SYMBOL): {"rehabs": []}})
        data, _ = channel.fetch("quote_corporate_actions_rehab", {"symbol": SYMBOL},
                                method="basic.rehab", home=self.home_api,
                                client=client,
                                adapter=lambda d: {"rehabs": d.get("rehabs") or [], "seen": True})
        self.assertEqual(data, {"rehabs": [], "seen": True})


class DualChannelEquivalenceTests(unittest.TestCase):
    """五项取数：同一份假数据经两通道 → 落库行/输出行逐字段相等（落库口径零变化）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        # 两个 home 必须是**不同目录**：同目录会被后写者覆盖通道配置（真机网络上真踩过）
        self.dir_mcp = Path(self.root) / "mcp"
        self.dir_api = Path(self.root) / "api"
        self.dir_mcp.mkdir()
        self.dir_api.mkdir()
        self.home_mcp = _home(self.dir_mcp, "mcp")
        self.home_api = _home(self.dir_api, "openapi")
        self.conn_mcp = store.connect(str(Path(self.root) / "mcp.sqlite"))
        self.conn_api = store.connect(str(Path(self.root) / "api.sqlite"))

    def tearDown(self):
        self.conn_mcp.close()
        self.conn_api.close()
        self.tmp.cleanup()

    def _mcp(self, payload):
        return lambda tool, params: payload

    def test_rehab_rows_identical(self):
        sync.sync_adjustments(self.conn_mcp, "600519", fetcher=self._mcp(REHAB))
        sync.sync_adjustments(self.conn_api, "600519", home=self.home_api,
                              client=RecordingClient(
                                  {paths.REHAB_PATH.format(symbol=SYMBOL): REHAB}))
        rows_mcp = store.read_adjustments(self.conn_mcp, SYMBOL, as_of="2026-09-14")
        rows_api = store.read_adjustments(self.conn_api, SYMBOL, as_of="2026-09-14")
        self.assertEqual(rows_mcp, rows_api)
        self.assertEqual(rows_api[-1]["cum_forward"], 12.34)
        self.assertEqual(rows_api[-1]["actions"], ["DIVIDEND"])

    def test_fundamentals_rows_identical_and_announced_at_unchanged(self):
        sync.sync_fundamentals(self.conn_mcp, "600519", fetcher=self._mcp(STATEMENTS))
        sync.sync_fundamentals(self.conn_api, "600519", home=self.home_api,
                               client=RecordingClient(
                                   {paths.F10_STATEMENTS_PATH.format(symbol=SYMBOL): STATEMENTS}))
        # 富途无公告日 → 两通道下 PIT 读取同样不可见（announced_at 落 NULL 语义不变）
        for conn in (self.conn_mcp, self.conn_api):
            self.assertEqual(store.read_fundamentals(conn, "600519", as_of="2026-09-14"), [])
        store.set_announced_at(self.conn_mcp, SYMBOL, "2026-06-30", "2026-08-28", "ak")
        store.set_announced_at(self.conn_api, SYMBOL, "2026-06-30", "2026-08-28", "ak")
        self.assertEqual(store.read_fundamentals(self.conn_mcp, "600519", as_of="2026-08-28"),
                         store.read_fundamentals(self.conn_api, "600519", as_of="2026-08-28"))

    def test_statements_items_container_tolerated(self):
        """REST 对「数组+分页」归一会用 ``items``（_merge_pagination 契约）——同样能落库。"""
        payload = {"items": STATEMENTS["report_list"], "pagination": {"next_key": "-1"}}
        n = sync.sync_fundamentals(self.conn_api, "600519", home=self.home_api,
                                   client=RecordingClient(
                                       {paths.F10_STATEMENTS_PATH.format(symbol=SYMBOL): payload}))
        self.assertEqual(n, 4)

    def test_valuation_values_identical(self):
        def fake_mcp(tool, params):
            return VALUATION_BY_TYPE[params["valuation_type"]]

        path = paths.F10_VALUATION_DETAIL_PATH.format(symbol=SYMBOL)
        client = RecordingClient({path: lambda query: VALUATION_BY_TYPE[query["valuation_type"]]})
        values_mcp, source_mcp = factors.valuation_values("600519", fetcher=fake_mcp,
                                                          akshare_module=_NoAkshare)
        values_api, source_api = factors.valuation_values("600519", home=self.home_api,
                                                          client=client,
                                                          akshare_module=_NoAkshare)
        self.assertEqual(values_mcp, values_api)
        self.assertEqual(values_api, {"pe_ttm": 21.5, "pe_ttm_pct": 33.0, "pb": 3.25,
                                      "pb_pct": 41.5, "ps": 6.75, "ps_pct": 55.5})
        self.assertIn("futu/quote_valuation_detail", source_api)
        self.assertEqual(source_mcp, source_api)
        self.assertEqual(len(client.calls), 3)  # 三个 valuation_type 各一次

    def test_dividends_rows_identical(self):
        with patch.object(channel, "_mcp_call", lambda: self._mcp(DIVIDENDS)):
            mcp_rows = events_mod.futu_dividends("600519", home=self.home_mcp)
        api_rows = events_mod.futu_dividends(
            "600519", home=self.home_api,
            client=RecordingClient({paths.F10_DIVIDENDS_PATH.format(symbol=SYMBOL): DIVIDENDS}))
        self.assertEqual(mcp_rows, api_rows)
        self.assertEqual(api_rows[0]["date"], "2026-06-20")
        self.assertEqual(api_rows[0]["source"], "futu/quote_corporate_actions_dividends")
        self.assertEqual(set(api_rows[0]), {"date", "type", "detail", "announced", "source"})

    def test_economic_calendar_rows_identical_and_date_shapes(self):
        captured = {}

        def fake_mcp(tool, params):
            captured["params"] = params
            return CALENDAR

        with patch.object(channel, "_mcp_call", lambda: fake_mcp):
            mcp_rows = events_mod.futu_economic_calendar(
                home=self.home_mcp, today=_dt.date(2026, 9, 16))
        client = RecordingClient({paths.ECONOMIC_CALENDAR_HOT_PATH: CALENDAR})
        api_rows = events_mod.futu_economic_calendar(
            home=self.home_api, client=client, today=_dt.date(2026, 9, 16))
        self.assertEqual(mcp_rows, api_rows)
        self.assertEqual(len(api_rows), 2)
        self.assertEqual(captured["params"], {"date": "20260916"})   # mcp 口径
        self.assertEqual(client.calls[0][2], {"date": "2026-09-16"})  # REST 口径（锁定表 §C.1）

    def test_calendar_best_effort_across_channels(self):
        """两条通道各自的取数失败都收敛为「该源缺席」，不抛给上层。"""
        from trading_datasource.futu_mcp import FutuUnavailable
        from trading_datasource.futu_openapi import TransportError

        with patch.object(channel, "_mcp_call",
                          lambda: (lambda tool, params: (_ for _ in ()).throw(
                              FutuUnavailable("down")))):
            self.assertEqual(events_mod.futu_economic_calendar(home=self.home_mcp), [])
        client = RecordingClient({paths.ECONOMIC_CALENDAR_HOT_PATH: TransportError("down")})
        self.assertEqual(events_mod.futu_economic_calendar(home=self.home_api,
                                                           client=client), [])

    def test_bad_config_is_not_masked_as_empty_calendar(self):
        """坏 JSON 是配置问题：**不得**伪装成「今天没有经济事件」。"""
        home = _home(self.root, raw="{not json")
        with self.assertRaises(ValueError):
            events_mod.futu_economic_calendar(home=home)


class _NoAkshare:
    """akshare 替身：估值用例只关心富途通道（回退分支用不到 → 属性访问即失败）。"""

    def __getattr__(self, name):  # pragma: no cover —— 富途取到值时不触达
        raise AssertionError(f"不应触达 akshare 回退：{name}")


if __name__ == "__main__":
    unittest.main()
