"""WP13 任务 2：模拟交易九端点 REST 化 + ``core_broker`` sim 通道分派。

**唯一事实源**：`docs/superpowers/plans/wp12-endpoint-lock.md` §C.9（9 条路径已核对）+ 本
任务的真机实测（2026-09-16，AppKey 凭据）：

* 鉴权**可用**（锁定表事前登记的「登录态 header」风险实测不成立）——
  ``GET /sim-trade/accounts`` 返回 9 个模拟账户；
* 三条官方页面差异：``orders``/``history-orders``/``max-buy-sell`` 的 ``market`` 实测必填
  （页面未列）；``time_begin``/``time_end`` 必须为微秒 int；``qty``/``price`` 是字符串。

本文件守五件事：

* **出站逐字一致**：九个方法的 (method, path, query/body) 与锁定表一致（``RecordingClient``
  入参断言，不经过序列化层）；
* **坏参数本地拒绝、零网络往返**：枚举/区间/字节数/路径片段非法 → ``ValueError``；
* **通道分派语义**：openapi 就绪走 REST、默认/无凭据回退 MCP、未知工具名原样转 MCP
  （绝不吞掉 live 调用）；REST 异常原样上抛（不静默换通道）；
* **翻译表正确**：链名 → market_id、日期 → 微秒、qty 数值 → 字符串、撤单丢弃多余 market；
* **市场口径唯一实现**：``trading_core.broker.MARKET_IDS`` /
  ``OPENAPI_ENABLE_MARKET`` 与 ``trading_datasource.market_ids`` 的规范常量
  **是同一对象**（WP13 审查 M1：镜像已收敛，测试断言同一性而非等值）。
"""
import contextlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "tests"))

from test_wp8_market import RecordingClient  # noqa: E402  （复用既有替身，不另造一套）

from trading_datasource import channel  # noqa: E402
from trading_datasource.futu_openapi import (  # noqa: E402
    WP13_SIM_TRADE_ENDPOINTS,
    OpenApiClient,
    OpenApiSimTrade,
)
from trading_datasource.futu_openapi.errors import (  # noqa: E402
    OpenApiError, TransportError, UnexpectedResponse)
from trading_datasource.market_ids import SIM_MARKET_IDS, sim_market_id  # noqa: E402
from trading_core import broker as core_broker  # noqa: E402

LOCK = ROOT / "docs" / "superpowers" / "plans" / "wp12-endpoint-lock.md"
ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(GET|POST|PUT|DELETE)\s+`(/api/v1\.0/[^`]+)`")


def lock_sim_pairs():
    """锁定表 §C.9 段落的 (方法, 路径模板) 集合。"""
    section, pairs = "", set()
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            section = line[4:].strip()
        if not section.startswith("C.9") or not line.startswith("|"):
            continue
        found = ROW_RE.match(line)
        if found:
            pairs.add((found.group(2), found.group(3)))
    return pairs


def make_sim(d=None, error=None):
    return OpenApiSimTrade(RecordingClient(d=d, error=error))


def openapi_home(tmp, *, ready=True):
    """建一个 ``futu_channel=openapi`` 的临时 home；``ready=False`` 时凭据路径指向不存在文件。"""
    path = Path(tmp) / "trading-platform.json"
    path.write_text(json.dumps({"futu_channel": "openapi"}), encoding="utf-8")
    cred = str(Path(tmp) / "futu-openapi.json")
    if not ready and Path(cred).exists():  # pragma: no cover —— 防御：临时目录不该有
        Path(cred).unlink()
    return str(tmp), cred


class SimTradeTransportTests(unittest.TestCase):
    """九个方法逐条出站断言（锁定表 §C.9）。"""

    def test_account_list(self):
        sim = make_sim()
        sim.account_list()
        self.assertEqual(sim.client.calls,
                         [("request", "GET", "/api/v1.0/sim-trade/accounts", None, None)])

    def test_cash_info(self):
        sim = make_sim()
        sim.cash_info("9393")
        self.assertEqual(sim.client.calls,
                         [("request", "GET", "/api/v1.0/sim-trade/9393/cash-info", None, None)])

    def test_position_list_carries_market(self):
        sim = make_sim()
        sim.position_list("3182575", market=3)
        self.assertEqual(sim.client.calls,
                         [("request", "GET", "/api/v1.0/sim-trade/3182575/positions",
                           {"market": 3}, None)])

    def test_order_list_carries_market(self):
        sim = make_sim()
        sim.order_list("3182575", market=3)
        self.assertEqual(sim.client.calls,
                         [("request", "GET", "/api/v1.0/sim-trade/3182575/orders",
                           {"market": 3}, None)])

    def test_history_order_list_micros_and_pagination(self):
        sim = make_sim()
        sim.history_order_list("3182575", market=3, time_begin=1785513600000000,
                               time_end=1788192000000000, page_size=5, next_key="abc")
        self.assertEqual(sim.client.calls,
                         [("request", "GET", "/api/v1.0/sim-trade/3182575/history-orders",
                           {"market": 3, "time_begin": 1785513600000000,
                            "time_end": 1788192000000000, "page_size": 5, "next_key": "abc"},
                           None)])

    def test_max_buy_sell(self):
        sim = make_sim()
        sim.max_buy_sell("3182575", symbol="603993", order_type=1, market=3, price=16.8)
        self.assertEqual(sim.client.calls,
                         [("request", "GET", "/api/v1.0/sim-trade/3182575/max-buy-sell",
                           {"market": 3, "symbol": "603993", "order_type": 1,
                            "price": "16.8"}, None)])

    def test_input_order_stringifies_qty_and_price(self):
        sim = make_sim()
        sim.input_order("3182575", market=3, symbol="603993", order_type=1, order_side=1,
                        qty=100, price=16.8, text="测试")
        self.assertEqual(sim.client.calls,
                         [("request", "POST", "/api/v1.0/sim-trade/3182575/orders", None,
                           {"market": 3, "symbol": "603993", "order_type": 1,
                            "order_side": 1, "qty": "100", "price": "16.8", "text": "测试"})])

    def test_input_order_market_order_omits_price(self):
        sim = make_sim()
        sim.input_order("3182575", market=3, symbol="603993", order_type=3, order_side=2,
                        qty=100)
        body = sim.client.calls[0][4]
        self.assertNotIn("price", body)
        self.assertEqual(body["order_type"], 3)

    def test_modify_order_carries_market(self):
        sim = make_sim()
        sim.modify_order("3182575", "7138401", new_qty=200, new_price=17.0, market=3)
        self.assertEqual(sim.client.calls,
                         [("request", "POST",
                           "/api/v1.0/sim-trade/3182575/orders/7138401/modify", None,
                           {"market": 3, "new_qty": "200", "new_price": "17.0"})])

    def test_cancel_order_carries_market(self):
        # 真机实测：官方 cancel 页写「无请求体」，实际缺 market 报
        # missing required field in body: market（TOOL-LIMITS 登记）
        sim = make_sim()
        sim.cancel_order("3182575", "7138401", market=3)
        self.assertEqual(sim.client.calls,
                         [("request", "POST",
                           "/api/v1.0/sim-trade/3182575/orders/7138401/cancel", None,
                           {"market": 3})])


class SimTradeValidationTests(unittest.TestCase):
    """坏参数本地拒绝且零网络往返（签名即白名单 + 枚举/区间/字节数）。"""

    def assert_rejected(self, call, *args, **kwargs):
        sim = make_sim()
        with self.assertRaises(ValueError):
            call(sim, *args, **kwargs)
        self.assertEqual(sim.client.calls, [], "坏参数必须零网络往返")

    def test_market_must_be_int_like(self):
        self.assert_rejected(lambda s: s.position_list("1", market="HK"))

    def test_order_type_and_side_enums(self):
        self.assert_rejected(lambda s: s.input_order("1", market=3, symbol="603993",
                                                     order_type=2, order_side=1, qty=100))
        self.assert_rejected(lambda s: s.input_order("1", market=3, symbol="603993",
                                                     order_type=1, order_side=5, qty=100))

    def test_text_byte_limit(self):
        long_text = "中" * 34  # UTF-8 102 字节 > 100
        self.assert_rejected(lambda s: s.input_order("1", market=3, symbol="603993",
                                                     order_type=1, order_side=1, qty=100,
                                                     text=long_text))

    def test_symbol_and_acc_id_shape(self):
        self.assert_rejected(lambda s: s.position_list("bad/id", market=3))
        self.assert_rejected(lambda s: s.input_order("1", market=3, symbol="60 3993",
                                                     order_type=1, order_side=1, qty=100))

    def test_cancel_and_modify_require_market(self):
        self.assert_rejected(lambda s: s.cancel_order("1", "7"))
        self.assert_rejected(lambda s: s.modify_order("1", "7", new_qty=100))

    def test_input_order_requires_market(self):
        self.assert_rejected(lambda s: s.input_order("1", market=None, symbol="603993",
                                                     order_type=1, order_side=1, qty=100))

    def test_history_rejects_string_time(self):
        self.assert_rejected(lambda s: s.history_order_list("1", market=3,
                                                            time_begin="2026-08-01"))

    def test_page_size_bounds(self):
        self.assert_rejected(lambda s: s.history_order_list("1", market=3, page_size=0))


class SimCallAdapterTests(unittest.TestCase):
    """``channel.sim_call`` 的通道分派与翻译表。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_openapi_channel_routes_to_rest(self):
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(d={"accounts": []})
        call = channel.sim_call(home, client=recording, credential_path=cred)
        call("sim_trade_account_list", {})
        self.assertEqual(recording.calls,
                         [("request", "GET", "/api/v1.0/sim-trade/accounts", None, None)])

    def test_mcp_channel_delegates_untouched(self):
        mcp_seen = []

        def fake_mcp(tool, params, timeout=30):
            mcp_seen.append((tool, params, timeout))
            return {"ok": True}

        call = channel.sim_call(self.tmp.name, mcp_call=fake_mcp)
        self.assertEqual(call("sim_trade_account_list", {}), {"ok": True})
        self.assertEqual(mcp_seen, [("sim_trade_account_list", {}, 30)])
        self.assertEqual(call("sim_trade_cash_info", {"acc_id": "1"}, timeout=15), {"ok": True})
        self.assertEqual(mcp_seen[-1], ("sim_trade_cash_info", {"acc_id": "1"}, 15))

    def test_openapi_without_credentials_falls_back_to_mcp(self):
        home, cred = openapi_home(self.tmp.name, ready=False)
        mcp_seen = []
        call = channel.sim_call(home, mcp_call=lambda t, p, timeout=30: mcp_seen.append(t),
                                credential_path=cred)
        call("sim_trade_account_list", {})
        self.assertEqual(mcp_seen, ["sim_trade_account_list"])

    def test_unknown_tool_always_goes_to_mcp(self):
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(d={})
        mcp_seen = []
        call = channel.sim_call(home, client=recording, credential_path=cred,
                                mcp_call=lambda t, p, timeout=30: mcp_seen.append(t))
        call("account_positions", {"acc_id": "1"}, timeout=30)
        call("account_authorized_trd_accs", {})
        self.assertEqual(mcp_seen, ["account_positions", "account_authorized_trd_accs"])
        self.assertEqual(recording.calls, [], "live 工具绝不能被 REST 分支吃掉")

    def test_input_order_translates_chain_market_and_numeric_qty(self):
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(d={"order_id": "1"})
        call = channel.sim_call(home, client=recording, credential_path=cred)
        call("sim_trade_input_order", {"acc_id": "3182575", "market": "SH",
                                       "symbol": "603993", "order_type": 1,
                                       "order_side": 1, "qty": 100, "price": 16.8})
        self.assertEqual(recording.calls[0][3], None)
        self.assertEqual(recording.calls[0][4]["market"], 3)
        self.assertEqual(recording.calls[0][4]["qty"], "100")
        self.assertEqual(recording.calls[0][4]["price"], "16.8")

    def test_input_order_unknown_chain_rejected_locally(self):
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(d={})
        call = channel.sim_call(home, client=recording, credential_path=cred)
        with self.assertRaises(ValueError):
            call("sim_trade_input_order", {"acc_id": "1", "market": "XX", "symbol": "1",
                                           "order_type": 1, "order_side": 1, "qty": 1})
        self.assertEqual(recording.calls, [])

    def test_cancel_forwards_market_in_body(self):
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(d={})
        call = channel.sim_call(home, client=recording, credential_path=cred)
        call("sim_trade_cancel_order", {"acc_id": "3182575", "market": 3, "order_id": "7"})
        self.assertEqual(recording.calls,
                         [("request", "POST",
                           "/api/v1.0/sim-trade/3182575/orders/7/cancel", None,
                           {"market": 3})])

    def test_history_resolves_market_from_accounts_and_converts_dates(self):
        home, cred = openapi_home(self.tmp.name)

        class AccountsThenHistory(RecordingClient):
            def request(self, method, path, query=None, json_body=None):
                if path == "/api/v1.0/sim-trade/accounts":
                    self.calls.append(("request", method, path, query, json_body))
                    return {"accounts": [{"account_id": "3182575", "market_id": 3}]}
                return super().request(method, path, query=query, json_body=json_body)

        recording = AccountsThenHistory(d={"orders": []})
        call = channel.sim_call(home, client=recording, credential_path=cred)
        call("sim_trade_history_order_list", {"acc_id": "3182575",
                                              "start": "2026-08-01", "end": "2026-08-02"})
        self.assertEqual([c[2] for c in recording.calls],
                         ["/api/v1.0/sim-trade/accounts",
                          "/api/v1.0/sim-trade/3182575/history-orders"])
        query = recording.calls[1][3]
        self.assertEqual(query["market"], 3)
        # 起点=当日 00:00；**终点=当日 23:59:59.999999**（实测：取 00:00 时区间宽度为零，
        # 当天订单一律查不到——见 to_micros_end 的 docstring 与 TOOL-LIMITS §九）
        self.assertEqual(query["time_begin"], channel.to_micros("2026-08-01"))
        self.assertEqual(query["time_end"], channel.to_micros_end("2026-08-02"))
        self.assertGreater(query["time_end"] - query["time_begin"], 24 * 3600 * 1_000_000)

    def test_rest_error_propagates_without_channel_switch(self):
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(error=ValueError("backend business error"))
        mcp_seen = []
        call = channel.sim_call(home, client=recording, credential_path=cred,
                                mcp_call=lambda t, p, timeout=30: mcp_seen.append(t))
        with self.assertRaises(ValueError):
            call("sim_trade_account_list", {})
        self.assertEqual(mcp_seen, [], "REST 失败绝不静默改走 MCP")


class SimCallBrokerIntegrationTests(unittest.TestCase):
    """``core_broker`` 既有签名在 REST 通道下的端到端形状（调用方零改动）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_broker_place_through_rest(self):
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(d={"order_id": "999"})
        call = channel.sim_call(home, client=recording, credential_path=cred)
        out = core_broker.place(call, acc_id="3182575", market=3, symbol="SH.603993",
                                side="BUY", qty=100, price=16.8)
        self.assertEqual(out["status"], "submitted")
        self.assertEqual(out["broker_order_id"], "999")
        body = recording.calls[0][4]
        self.assertEqual(body["symbol"], "603993")  # broker.place 的 lstrip 口径不变
        self.assertEqual(body["order_side"], 1)
        self.assertEqual(body["qty"], "100")

    def test_broker_place_timeout_is_unknown_and_never_retried(self):
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(error=TimeoutError("timed out"))
        call = channel.sim_call(home, client=recording, credential_path=cred)
        out = core_broker.place(call, acc_id="1", market=3, symbol="SH.603993",
                                side="BUY", qty=100, price=16.8)
        self.assertEqual(out["status"], "unknown")
        self.assertEqual(len(recording.calls), 1, "铁律：超时只发一次，绝不重放")

    # ---- WP13 审查 K1：真实传输异常不是 TimeoutError，旧用例是假阴性。
    # ---- 这里的假件注入的是**真实传输会抛的类型**（REST 的 TransportError /
    # ---- UnexpectedResponse），它们经 sim_call → REST 分支 → broker.place 全程。

    def test_broker_place_transport_error_is_unknown(self):
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(error=TransportError("连接被重置"))
        call = channel.sim_call(home, client=recording, credential_path=cred)
        out = core_broker.place(call, acc_id="1", market=3, symbol="SH.603993",
                                side="BUY", qty=100, price=16.8)
        self.assertEqual(out["status"], "unknown",
                         "REST 传输失败＝请求可能已到券商，必须 unknown 而非 rejected")
        self.assertEqual(len(recording.calls), 1, "铁律：绝不重放")

    def test_broker_place_unexpected_response_is_unknown(self):
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(error=UnexpectedResponse("502 Bad Gateway"))
        call = channel.sim_call(home, client=recording, credential_path=cred)
        out = core_broker.place(call, acc_id="1", market=3, symbol="SH.603993",
                                side="BUY", qty=100, price=16.8)
        self.assertEqual(out["status"], "unknown",
                         "非信封/5xx 不是业务结论（errors.py 明示可能已到券商）")

    def test_broker_place_business_error_still_rejected(self):
        """防矫枉过正：券商明确拒绝（业务信封 errcode）必须保持 rejected。"""
        home, cred = openapi_home(self.tmp.name)
        recording = RecordingClient(error=OpenApiError("资金不足", errcode=-3))
        call = channel.sim_call(home, client=recording, credential_path=cred)
        out = core_broker.place(call, acc_id="1", market=3, symbol="SH.603993",
                                side="BUY", qty=100, price=16.8)
        self.assertEqual(out["status"], "rejected")
        self.assertIn("资金不足", out["err"])

    def test_broker_place_timeout_reaches_rest_branch(self):
        """M2：REST 分支必须兑现调用方的 timeout（过去被静默丢弃）。"""
        home, cred = openapi_home(self.tmp.name)
        recording = ScopeRecordingClient(d={"order_id": "999"})
        call = channel.sim_call(home, client=recording, credential_path=cred)
        core_broker.place(call, acc_id="1", market=3, symbol="SH.603993",
                          side="BUY", qty=100, price=16.8, timeout=7)
        self.assertEqual(recording.timeouts, [7],
                         "sim_call 的 timeout 必须经 timeout_scope 传到 REST 调用链")

    def test_broker_accounts_and_positions_shape(self):
        home, cred = openapi_home(self.tmp.name)

        class TwoStep(RecordingClient):
            def request(self, method, path, query=None, json_body=None):
                if path.endswith("/accounts"):
                    self.calls.append(("request", method, path, query, json_body))
                    return {"accounts": [{"account_id": "3182575", "market_id": 3}]}
                self.calls.append(("request", method, path, query, json_body))
                return {"positions": [{"symbol": "600089", "qty": "100"}]}

        recording = TwoStep()
        call = channel.sim_call(home, client=recording, credential_path=cred)
        rows = core_broker.positions(call, acc_id="3182575", market_id=3)
        self.assertEqual(rows, [{"symbol": "600089", "qty": "100"}])


class ScopeRecordingClient(RecordingClient):
    """记录 ``timeout_scope`` 覆盖值的替身（M2：REST 分支的 timeout 兑现）。"""

    def __init__(self, d=None, pagination=None, error=None):
        super().__init__(d=d, pagination=pagination, error=error)
        self.timeouts = []

    def timeout_scope(self, timeout):
        self.timeouts.append(timeout)
        return contextlib.nullcontext()


class TimeoutPlumbingTests(unittest.TestCase):
    """传输层超时兑现（WP13 审查 M2）：``request(timeout=)`` / ``timeout_scope`` /
    实例缺省三级优先，最终落到 http 传输；4 参老替身继续工作。"""

    class _Cred:
        def __init__(self, data):
            self._data = dict(data)

        def load(self):
            return dict(self._data)

        def save(self, data):
            self._data = dict(data)

    class _TimeoutHttp:
        """**5 参**假传输：捕获每次调用的 timeout（老替身是 4 参，两者都要兼容）。"""

        def __init__(self):
            self.timeouts = []

        def __call__(self, method, url, headers=None, body=None, timeout=None):
            self.timeouts.append(timeout)
            return 200, json.dumps({"s": "ok", "d": {"ok": 1}}).encode("utf-8"), {}

    def _client(self, http, **kwargs):
        cred = self._Cred({"mode": "oauth", "access_token": "t",
                           "expires_at": 9_999_999_999_999})
        return OpenApiClient(cred, http=http, host="https://example.invalid", **kwargs)

    def test_default_timeout_reaches_transport(self):
        http = self._TimeoutHttp()
        self._client(http).request("GET", "/api/v1.0/x")
        self.assertEqual(http.timeouts, [30.0], "缺省 30 秒必须落到传输（原为硬编码）")

    def test_request_timeout_override(self):
        http = self._TimeoutHttp()
        self._client(http).request("GET", "/api/v1.0/x", timeout=7)
        self.assertEqual(http.timeouts, [7.0])

    def test_timeout_scope_override(self):
        http = self._TimeoutHttp()
        client = self._client(http)
        with client.timeout_scope(3):
            client.request("GET", "/api/v1.0/x")
            client.request("GET", "/api/v1.0/y")   # 块内多次请求都吃覆盖值
        client.request("GET", "/api/v1.0/z")       # 出块恢复缺省
        self.assertEqual(http.timeouts, [3.0, 3.0, 30.0])

    def test_timeout_scope_rejects_bad_value(self):
        client = self._client(self._TimeoutHttp())
        for bad in (0, -1, "5", True, None):
            with self.assertRaises(ValueError, msg=f"{bad!r} 必须被拒"):
                client.timeout_scope(bad)

    def test_four_arg_transport_still_works(self):
        """兼容契约：老 4 参替身不接收 timeout（签名判定），行为与改造前一致。"""
        seen = []

        def four_arg(method, url, headers=None, body=None):
            seen.append(method)
            return 200, json.dumps({"s": "ok", "d": {"ok": 1}}).encode("utf-8"), {}

        client = self._client(four_arg)
        with client.timeout_scope(5):
            self.assertEqual(client.request("GET", "/api/v1.0/x"), {"ok": 1})
        self.assertEqual(seen, ["GET"])

    def test_call_openapi_ignores_timeout_for_scope_less_fake(self):
        """注入替身没有 ``timeout_scope`` 时忽略 timeout（不炸、不静默改通道）。"""
        recording = RecordingClient(d={"order_id": "1"})
        out = channel.call_openapi(recording, "simtrade.input_order",
                                   {"acc_id": "1", "market": 3, "symbol": "603993",
                                    "order_type": 1, "order_side": 1, "qty": 100,
                                    "price": 16.8}, timeout=7)
        self.assertEqual(out, {"order_id": "1"})
        self.assertEqual(len(recording.calls), 1)


class MarketIdsLockTests(unittest.TestCase):
    """市场口径常量：core 与规范模块是**同一对象**（WP13 审查 M1 收敛镜像——
    不再靠等值断言维护重复）。"""

    def test_broker_market_ids_is_canonical_object(self):
        self.assertIs(core_broker.MARKET_IDS, SIM_MARKET_IDS)

    def test_broker_openapi_enable_market_is_canonical_object(self):
        from trading_datasource.market_ids import OPENAPI_ENABLE_MARKET
        self.assertIs(core_broker.OPENAPI_ENABLE_MARKET, OPENAPI_ENABLE_MARKET)

    def test_sim_market_id_normalizes(self):
        self.assertEqual(sim_market_id("SH"), 3)
        self.assertEqual(sim_market_id("SZ"), 3)
        self.assertEqual(sim_market_id("BJ"), 3)
        self.assertEqual(sim_market_id("HK"), 1)
        self.assertEqual(sim_market_id("US"), 100)
        self.assertEqual(sim_market_id(9), 9)
        self.assertEqual(sim_market_id("9"), 9)
        self.assertIsNone(sim_market_id("XX"))
        self.assertIsNone(sim_market_id(None))


class LockTableBindingTests(unittest.TestCase):
    """绑定面与锁定表 §C.9 逐条一致（防路径漂移）。"""

    def test_surface_matches_lock_table(self):
        self.assertEqual(set(WP13_SIM_TRADE_ENDPOINTS.values()), lock_sim_pairs())

    def test_nine_endpoints(self):
        self.assertEqual(len(WP13_SIM_TRADE_ENDPOINTS), 9)

    def test_adapter_route_table_covers_expected_tools(self):
        self.assertEqual(set(channel.SIM_TOOL_ROUTES),
                         {"sim_trade_account_list", "sim_trade_position_list",
                          "sim_trade_cash_info", "sim_trade_input_order",
                          "sim_trade_cancel_order", "sim_trade_history_order_list",
                          "sim_trade_max_buy_sell"})

    def test_to_micros_rejects_unparsable(self):
        with self.assertRaises(ValueError):
            channel.to_micros("not-a-date")
        with self.assertRaises(ValueError):
            channel.to_micros([1])
        with self.assertRaises(ValueError):
            channel.to_micros_end([1])
        self.assertIsNone(channel.to_micros(None))
        self.assertIsNone(channel.to_micros_end(None))

    def test_to_micros_end_covers_the_whole_day(self):
        begin = channel.to_micros("2026-09-17")
        end = channel.to_micros_end("2026-09-17")
        self.assertEqual(begin, 1789574400000000)  # 2026-09-17 00:00:00 +08:00
        self.assertEqual(end - begin, 86_399_999_999)  # 差 1µs 即全天
        # int 直通（调用方已给精确微秒，不做日界改写）
        self.assertEqual(channel.to_micros_end(123), 123)


if __name__ == "__main__":
    unittest.main()
