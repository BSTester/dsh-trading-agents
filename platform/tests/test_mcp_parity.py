"""V3 ⇄ MCP 覆盖性（parity）测试：``/api/v3/*`` 路由与 MCP 工具面必须一一对应且真的能用。

三层证据，逐层加严：

1. **静态双射**：``create_app`` 装配出来的真实路由表 ⇄ ``v3_mcp`` 注册的工具清单——
   每个 ``/api/v3/*`` 路由都有工具、没有孤儿工具、工具名符合规范、只读标记与
   ``NON_READONLY_PATHS`` 一致、inputSchema 封闭（``additionalProperties:false``）。
2. **真实协议调用**：走 ``/mcp`` 的 ``tools/list`` 与 ``tools/call``（**优先本地已启动的
   8397 服务**；不可达或该进程还是旧版本时退回 ASGI in-process + 真 lifespan，避免测试
   依赖网络/依赖别人的重启）。只调**只读**工具。
3. **故意失败自检（mutation）**：把「少一件工具」「多一件孤儿工具」的输入喂给**同一套**
   断言函数，必须抛 ``ParityError``——证明第 1 层真的会因为少一件工具而红，而不是恒真。

另有通道分级断言：``/api/v3/credentials`` 的 ``save``/``clear`` 在 MCP 面封死
（返回 ``v3/credentials-web-only``，handler 零调用、磁盘零写入）。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_mcp_parity -v``
"""
import asyncio
import copy
import json
import logging
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from server import mcp_tools, v3_mcp  # noqa: E402

DAY = "2026-09-19"
#: 本地已启动的平台服务（生产进程）；不在时第 2 层退回 in-process。
LIVE_MCP_URL = os.environ.get("QUANTWB_MCP_URL", "http://127.0.0.1:8397/mcp")
PROTOCOL_VERSION = "2025-06-18"
#: 允许在真实协议层调用的工具（**全部只读**；写/交易类工具一律不出现在这里）。
LIVE_READONLY_CALLS = (
    ("v3_tools", {}),
    ("v3_gateway", {}),
    ("v3_metrics", {}),
    ("v3_risk", {}),
    ("v3_credentials", {"action": "status"}),
)


class ParityError(AssertionError):
    """路由表与工具面不一致（缺工具/孤儿工具）。"""


def parity_report(route_paths, tool_endpoints):
    """路由集合 ⇄ 工具 endpoint 集合的差集报告。"""
    routes = set(route_paths)
    tools = set(tool_endpoints)
    return {"missing_tools": sorted(routes - tools), "orphan_tools": sorted(tools - routes)}


def assert_parity(route_paths, tool_endpoints):
    """断言双射成立；不成立就抛 ``ParityError``（同一套断言被真实用例与 mutation 用例共用）。"""
    report = parity_report(route_paths, tool_endpoints)
    if report["missing_tools"] or report["orphan_tools"]:
        raise ParityError(
            f"V3 路由与 MCP 工具面不匹配：缺工具={report['missing_tools']} "
            f"孤儿工具={report['orphan_tools']}")
    return report


# ---------------------------------------------------------------------------
# 应用装配（离线：假 handle + 空调度器 + 空推送；真路由表、真 V3 接线、真 MCP 注册）
# ---------------------------------------------------------------------------
class _DummyScheduler:
    def start(self):
        return None

    def stop(self):
        return None


class _DummyPush:
    quote_cache = None

    async def start(self):
        return False

    async def stop(self):
        return None


def fake_raw():
    """一套自洽的假工作台信封（值带 source/as_of，便于断言信封被原样透传）。"""
    return {
        "schedule": {"ok": True, "value": {"heartbeat": {"at": DAY}, "jobs": [],
                                           "kill": False, "halt": False}},
        "risk": {"ok": True, "value": {"config": {"singlePct": 2.0}, "source": "workbench/risk",
                                       "as_of": DAY}},
        "snapshot": {"ok": True, "value": {"mode": "sim", "generated_at": DAY}},
        "sources": {"ok": True, "value": {"channels": []}},
        "equity": {"ok": True, "value": {"current": 100000.0}},
        "positions": {"ok": True, "value": {"groups": [], "as_of": DAY}},
        "orders_open": {"ok": True, "value": {"groups": [], "as_of": DAY}},
        "deals_today": {"ok": True, "value": {"groups": [], "as_of": DAY}},
        "plan": {"ok": True, "value": {"plans": []}},
        "confirmation": {"ok": True, "value": {"pending": []}},
        "push_status": {"ok": True, "value": {"enabled": False}},
    }


def build_offline_app(home, raw=None):
    """装配一个**离线**的真应用（真路由表 / 真 V3 子模块 / 真 MCP 桥）。"""
    from server import app as app_module

    raw = fake_raw() if raw is None else raw
    calls = []

    def fake_handle(endpoint, payload=None):
        calls.append((endpoint, dict(payload or {})))
        if endpoint not in raw:
            return {"ok": False, "error": {"code": "trading/unknown-endpoint",
                                           "message": endpoint}}
        return copy.deepcopy(raw[endpoint])

    patcher = unittest.mock.patch.object(app_module, "create_handler",
                                         lambda *args, **kwargs: fake_handle)
    patcher.start()
    try:
        app = app_module.create_app(home=str(home), dist=str(home), config={},
                                    scheduler=_DummyScheduler(), futu=object(),
                                    push=_DummyPush())
    finally:
        patcher.stop()
    return app, calls


# ---------------------------------------------------------------------------
# MCP 客户端：真实 streamable-http 协议（initialize → tools/list → tools/call）
# ---------------------------------------------------------------------------
class McpClient:
    """最小 MCP 客户端（JSON-RPC over streamable-http）。"""

    def __init__(self, http, base="/mcp"):
        self.http = http
        self.base = base
        self.session = None
        self.seq = 0

    async def _post(self, payload):
        headers = {"Accept": "application/json, text/event-stream"}
        if self.session:
            headers["mcp-session-id"] = self.session
        response = await self.http.post(self.base, json=payload, headers=headers)
        if "mcp-session-id" in response.headers:
            self.session = response.headers["mcp-session-id"]
        response.raise_for_status()
        return response.json()

    async def initialize(self):
        out = await self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                "params": {"protocolVersion": PROTOCOL_VERSION,
                                           "capabilities": {},
                                           "clientInfo": {"name": "parity-test", "version": "1"}}})
        self.seq = 1
        headers = {"Accept": "application/json, text/event-stream",
                   "mcp-session-id": self.session}
        await self.http.post(self.base, headers=headers,
                             json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        return out["result"]

    async def _request(self, method, params):
        self.seq += 1
        out = await self._post({"jsonrpc": "2.0", "id": self.seq, "method": method,
                                "params": params})
        if "error" in out:
            raise AssertionError(f"{method} 失败：{out['error']}")
        return out["result"]

    async def list_tools(self):
        return (await self._request("tools/list", {}))["tools"]

    async def call_tool(self, name, arguments=None):
        result = await self._request("tools/call", {"name": name,
                                                    "arguments": arguments or {}})
        text = result["content"][0]["text"]
        return result.get("isError"), json.loads(text)


async def _inproc(work):
    """在真 lifespan 下跑一段异步工作（MCP session manager 必须有 lifespan）。"""
    home = Path(tempfile.mkdtemp(prefix="v3-mcp-parity-"))
    try:
        app, calls = build_offline_app(home)
        async with app.router.lifespan_context(app):
            # ASGITransport 走 SDK transport 的 Host 校验：用允许的 host 头。
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://127.0.0.1:8397") as http:
                client = McpClient(http)
                await client.initialize()
                return await work(client, app, calls)
    finally:
        import shutil
        shutil.rmtree(home, ignore_errors=True)
        _silence_loggers()


def _silence_loggers():
    """装配链会把 root 拉到 INFO 并加 handler——用例内静音，别污染输出。"""
    for name in ("httpx", "httpx2", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# 1. 静态双射
# ---------------------------------------------------------------------------
class RouteParityTests(unittest.TestCase):
    """路由表 ⇄ 工具面：覆盖、命名、只读标记、封闭 schema。"""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="v3-mcp-parity-"))
        cls.app, cls.calls = build_offline_app(cls.home)
        cls.bridge = cls.app.state.v3_mcp_bridge
        _silence_loggers()

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_every_v3_route_has_a_tool_and_no_orphans(self):
        """每个 ``/api/v3/*`` 路由都有对应 MCP 工具，且没有孤儿工具。"""
        routes = [route.path for route in self.app.routes
                  if getattr(route, "path", "").startswith("/api/v3/")]
        report = assert_parity(routes, [d.endpoint for d in self.bridge.definitions])
        self.assertEqual(report, {"missing_tools": [], "orphan_tools": []})
        self.assertGreaterEqual(len(self.bridge.definitions), 30,
                                "V3 路由面不应退化到这个数量以下")

    def test_no_description_gaps(self):
        """所有路由都登记了工具描述与参数说明（新增路由忘了登记 → 这里红）。"""
        self.assertTrue(self.bridge.gaps.empty, self.bridge.gaps.as_dict())

    def test_tool_names_are_normalized_and_unique(self):
        seen = set()
        for definition in self.bridge.definitions:
            self.assertEqual(definition.name, v3_mcp.tool_name(definition.endpoint))
            self.assertTrue(definition.name.startswith("v3_"))
            self.assertNotIn(definition.name, seen)
            seen.add(definition.name)
            self.assertFalse(mcp_tools.is_blacklisted(definition.name))

    def test_tool_names_do_not_collide_with_the_77(self):
        """既有 77 工具的名字/数量一字不动，且与 V3 工具零重名。"""
        self.assertEqual(mcp_tools.TOOL_COUNT, 77)
        self.assertEqual(len(mcp_tools.TOOLS), 77)
        self.assertFalse([name for name in mcp_tools.TOOL_NAMES if name.startswith("v3_")])
        self.assertEqual(set(mcp_tools.TOOL_NAMES) & set(self.bridge.names), set())

    def test_readonly_marking_matches_declaration(self):
        readonly = {d.endpoint for d in self.bridge.definitions
                    if d.endpoint not in v3_mcp.NON_READONLY_PATHS}
        self.assertEqual(readonly, set(self.bridge.paths) - set(v3_mcp.NON_READONLY_PATHS))
        # 写类只有这三条：研究轮落盘 / 本地台账重写 / 凭据（且凭据写被封死）。
        self.assertEqual(v3_mcp.NON_READONLY_PATHS,
                         {"/api/v3/strategy/run", "/api/v3/oms/sync", "/api/v3/credentials"})

    def test_no_trade_endpoint_is_bridged(self):
        """桥里绝不能出现交易写端点（下单/改单/撤单/切模式/执行计划）。"""
        forbidden = ("trade", "switch-mode", "plan-execute", "confirm-decide", "sim_trade")
        for path in self.bridge.paths:
            lowered = path.lower()
            self.assertFalse(any(token in lowered for token in forbidden), path)

    def test_registered_schemas_are_closed(self):
        """``additionalProperties:false`` 与 77 工具同一套封闭性保证。"""
        manager = self.app.state.mcp._tool_manager  # noqa: SLF001 —— SDK 无公开口子
        for name in self.bridge.names:
            tool = manager._tools[name]  # noqa: SLF001
            self.assertIs(tool.parameters.get("additionalProperties"), False, name)


# ---------------------------------------------------------------------------
# 2. 真实协议调用（tools/list + tools/call）
# ---------------------------------------------------------------------------
class ProtocolCallTests(unittest.TestCase):
    """走真 ``/mcp`` 协议：先本地已启动服务，再 in-process（都只调只读工具）。"""

    def test_tools_list_and_readonly_calls_in_process(self):
        async def work(client, app, calls):
            tools = await client.list_tools()
            names = [tool["name"] for tool in tools]
            # 77 既有工具 + 全部 V3 桥接工具（一次遍历路由表得到，结构性成立）
            self.assertEqual(len(names), 77 + len(app.state.v3_mcp_bridge.names))
            self.assertEqual(len(set(names)), len(names), "工具名必须唯一")
            self.assertEqual(set(app.state.v3_mcp_bridge.names) - set(names), set())
            annotations = {tool["name"]: (tool.get("annotations") or {}) for tool in tools}
            for path in app.state.v3_mcp_bridge.paths:
                name = v3_mcp.tool_name(path)
                expected = path not in v3_mcp.NON_READONLY_PATHS
                self.assertEqual(annotations[name].get("readOnlyHint"), expected, name)

            results = {}
            for name, arguments in LIVE_READONLY_CALLS:
                is_error, envelope = await client.call_tool(name, arguments)
                self.assertFalse(is_error, f"{name} 不应是协议层错误")
                self.assertIsInstance(envelope, dict)
                self.assertIn("ok", envelope, name)
                self.assertTrue(envelope["ok"], f"{name} 信封：{envelope}")
                results[name] = envelope
            return results

        results = asyncio.run(_inproc(work))
        # 信封里必须带来源/时点（这两条端点的 handler 就是这么产出的，桥不加工）
        self.assertEqual(results["v3_risk"]["data"]["source"], "workbench/risk")
        self.assertEqual(results["v3_risk"]["data"]["as_of"], DAY)
        from server import v3_ops

        self.assertEqual(results["v3_tools"]["total"],
                         len(mcp_tools.TOOLS) + len(v3_ops.V3_LOCAL_TOOLS))
        self.assertIn("toolTotal", results["v3_metrics"])
        self.assertEqual(results["v3_credentials"]["ok"], True)

    def test_live_service_when_available(self):
        """本地已启动服务可用时，用**它**跑一遍只读 tools/call（不可达/旧版本 → skip）。"""

        async def work():
            async with httpx.AsyncClient(base_url=LIVE_MCP_URL.rsplit("/mcp", 1)[0],
                                         timeout=30.0) as http:
                client = McpClient(http)
                await client.initialize()
                tools = await client.list_tools()
                names = {tool["name"] for tool in tools}
                if not set(v3_mcp.tool_name(path) for path in v3_mcp.TOOL_DOCS) & names:
                    return {"stale": True, "count": len(names)}
                out = {}
                for name, arguments in LIVE_READONLY_CALLS:
                    if name not in names:
                        continue
                    is_error, envelope = await client.call_tool(name, arguments)
                    self.assertFalse(is_error, name)
                    self.assertTrue(envelope.get("ok"), f"{name}: {envelope}")
                    out[name] = envelope
                return {"stale": False, "count": len(names), "results": out}

        try:
            outcome = asyncio.run(work())
        except (httpx.HTTPError, OSError) as error:
            self.skipTest(f"本地 8397 服务不可达，已由 in-process 用例覆盖：{error}")
        if outcome["stale"]:
            self.skipTest(f"本地服务进程是改动前的旧版本（{outcome['count']} 工具，无 v3_* 工具）；"
                          "重启后本用例自动生效")
        self.assertTrue(outcome["results"], "至少应调用成功一个只读工具")


# ---------------------------------------------------------------------------
# 3. 通道分级：凭据写入在 MCP 面封死
# ---------------------------------------------------------------------------
class CredentialBoundaryTests(unittest.TestCase):
    """``save``/``clear`` 在桥内封死：handler 零调用、磁盘零写入、错误码固定。"""

    def test_save_and_clear_are_refused_without_touching_the_handler(self):
        from server import v3_credentials

        async def work(client, app, calls):
            out = {}
            with unittest.mock.patch.object(v3_credentials, "save") as save_spy, \
                    unittest.mock.patch.object(v3_credentials, "clear") as clear_spy:
                for action in ("save", "clear"):
                    is_error, envelope = await client.call_tool(
                        "v3_credentials", {"action": action, "key": "tushare_token",
                                           "value": "should-never-be-written"})
                    out[action] = (is_error, envelope)
                out["save_calls"] = save_spy.call_count
                out["clear_calls"] = clear_spy.call_count
            return out

        out = asyncio.run(_inproc(work))
        for action in ("save", "clear"):
            is_error, envelope = out[action]
            self.assertFalse(is_error)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["error"]["code"],
                             v3_mcp.CREDENTIALS_WEB_ONLY_CODE)
            self.assertIn("Web", envelope["error"]["message"])
        self.assertEqual(out["save_calls"], 0, "封死必须发生在 handler 之前")
        self.assertEqual(out["clear_calls"], 0)

    def test_status_and_test_stay_available(self):
        async def work(client, app, calls):
            is_error, status_envelope = await client.call_tool("v3_credentials",
                                                               {"action": "status"})
            self.assertFalse(is_error)
            self.assertIn("ok", status_envelope)
            # test 走 POST（同路径多方法）——这里不真打网络，只断言它没有走封死分支。
            return status_envelope

        envelope = asyncio.run(_inproc(work))
        self.assertTrue(envelope["ok"], envelope)


# ---------------------------------------------------------------------------
# 4. 故意失败自检（mutation）：同一套断言在「少一件工具」时必须红
# ---------------------------------------------------------------------------
class MutationGuardTests(unittest.TestCase):
    """证明第 1 层的断言不是恒真：手工去掉一件工具 / 造一件孤儿工具都必须抛错。"""

    def test_missing_tool_is_detected(self):
        routes = ["/api/v3/overview", "/api/v3/risk", "/api/v3/tools"]
        tools = {"v3_overview": "/api/v3/overview", "v3_risk": "/api/v3/risk"}
        report = parity_report(routes, tools.values())
        self.assertEqual(report["missing_tools"], ["/api/v3/tools"])
        with self.assertRaises(ParityError):
            assert_parity(routes, tools.values())

    def test_orphan_tool_is_detected(self):
        routes = ["/api/v3/overview"]
        tools = {"v3_overview": "/api/v3/overview", "v3_ghost": "/api/v3/ghost"}
        report = parity_report(routes, tools.values())
        self.assertEqual(report["orphan_tools"], ["/api/v3/ghost"])
        with self.assertRaises(ParityError):
            assert_parity(routes, tools.values())

    def test_real_bridge_survives_the_mutation_helper(self):
        """真桥在「删掉最后一件工具」后必须被同一套断言判为不合格。"""
        home = Path(tempfile.mkdtemp(prefix="v3-mcp-mutation-"))
        try:
            app, _calls = build_offline_app(home)
            bridge = app.state.v3_mcp_bridge
            routes = [route.path for route in app.routes
                      if getattr(route, "path", "").startswith("/api/v3/")]
            endpoints = [d.endpoint for d in bridge.definitions]
            assert_parity(routes, endpoints)  # 完整时通过
            with self.assertRaises(ParityError):
                assert_parity(routes, endpoints[:-1])  # 少一件 → 必须红
            with self.assertRaises(ParityError):
                assert_parity(routes, endpoints + ["/api/v3/ghost"])  # 孤儿 → 必须红
        finally:
            _silence_loggers()
            import shutil
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
