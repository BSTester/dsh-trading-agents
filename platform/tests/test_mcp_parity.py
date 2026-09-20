"""V3 ⇄ MCP 覆盖性（parity）测试：``/api/v3/*`` 路由与 MCP 工具面一一对应且真的能用。

**两种表面模式各有一套完整断言**（规格 FR-TOOLS-003 / §10 决策 3）：

* ``direct``    —— 全部工具全量直暴露（基础 77 + 全部桥接件，向后兼容口径）；
* ``discovery`` —— 只暴露「直连保留 4 件 + ``list_tools`` / ``call_tool`` 两个发现入口」，
  **但代理可达的集合必须与 direct 面一一对应**（发现代理没丢能力）。

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

from server import mcp_discovery, mcp_tools, v3_mcp  # noqa: E402

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
#: 两种模式的工具名面**全部由注册表推导**（不写死桥接条数：路由正在演进，
#: v3_sdk / v3_headless / v3_alerts 并行落地）：
#:   * direct    = 基础注册表 77 + ``len(/api/v3/* 路由)``；
#:   * discovery = 直连保留子集 + 两个代理入口（与路由数无关）。
BASE_TOOLS = 77
DISCOVERY_SURFACE = len(mcp_discovery.DIRECT_KEEP) + len(mcp_discovery.PROXY_NAMES)


def direct_surface(app):
    """direct 模式的精确期望条数 = 基础 77 + 桥接件数（桥接件数从路由表来）。"""
    return BASE_TOOLS + len(app.state.v3_mcp_bridge.names)


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


def build_offline_app(home, raw=None, surface=mcp_discovery.DIRECT):
    """装配一个**离线**的真应用（真路由表 / 真 V3 子模块 / 真 MCP 桥）。

    ``surface`` 显式传入（不读 ``QUANT_MCP_SURFACE``）：同一个进程里两种模式各跑一套断言，
    互不污染；缺省 ``direct`` 保持历史口径。
    """
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
                                    push=_DummyPush(), mcp_surface=surface)
    finally:
        patcher.stop()
    return app, calls


def registered_names(server):
    """注册面工具名（与 ``tools/list`` 同一份 ToolManager）。"""
    return [tool.name for tool in asyncio.run(server.list_tools())]


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


async def _inproc(work, surface):
    """在真 lifespan 下跑一段异步工作（MCP session manager 必须有 lifespan）。"""
    home = Path(tempfile.mkdtemp(prefix="v3-mcp-parity-"))
    try:
        app, calls = build_offline_app(home, surface=surface)
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
# 1. 静态双射（direct 面；两种模式共用同一份桥接定义）
# ---------------------------------------------------------------------------
class RouteParityTests(unittest.TestCase):
    """路由表 ⇄ 工具面：覆盖、命名、只读标记、封闭 schema。"""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="v3-mcp-parity-"))
        # 桥接定义与表面模式无关（两种模式共用同一份 V3Bridge），但注册面只在 direct 下全量；
        # 这里用 direct 装配，断言的是**定义层**的双射（discovery 的注册面在下面单独断言）。
        cls.app, cls.calls = build_offline_app(cls.home, surface=mcp_discovery.DIRECT)
        cls.bridge = cls.app.state.v3_mcp_bridge
        _silence_loggers()

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_every_v3_route_has_a_tool_and_no_orphans(self):
        """每个 ``/api/v3/*`` 路由路径都有对应 MCP 工具，且没有孤儿工具。

        断言的是**路径集合**的双射（不数 ``app.routes`` 的条目数）：同一条路径登记多个
        方法（``/api/v3/credentials`` 有 GET+POST）时条目数会多于路径数，用条目数当期望值
        会把「本来就该有的多方法登记」误判成漂移。桥接工具数 = 路径数，由
        ``_v3_routes`` 的构造保证（每条路径一件工具）。
        """
        route_paths = sorted({route.path for route in self.app.routes
                              if getattr(route, "path", "").startswith("/api/v3/")})
        report = assert_parity(route_paths, [d.endpoint for d in self.bridge.definitions])
        self.assertEqual(report, {"missing_tools": [], "orphan_tools": []})
        self.assertEqual(len(self.bridge.definitions), len(route_paths),
                         "V3 桥接面条数 = /api/v3/* 路由路径数")
        self.assertEqual(sorted(self.bridge.paths), route_paths)
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

    def test_tool_names_do_not_collide_with_the_base_surface(self):
        """既有基础工具的名字/数量一字不动，且与 V3 工具零重名。"""
        self.assertEqual(mcp_tools.TOOL_COUNT, BASE_TOOLS)
        self.assertEqual(len(mcp_tools.TOOLS), BASE_TOOLS)
        self.assertFalse([name for name in mcp_tools.TOOL_NAMES if name.startswith("v3_")])
        self.assertEqual(set(mcp_tools.TOOL_NAMES) & set(self.bridge.names), set())

    def test_readonly_marking_matches_declaration(self):
        readonly = {d.endpoint for d in self.bridge.definitions
                    if d.endpoint not in v3_mcp.NON_READONLY_PATHS}
        self.assertEqual(readonly, set(self.bridge.paths) - set(v3_mcp.NON_READONLY_PATHS))
        # 写类逐名给出（**不数条数**——/api/v3/* 路由在演进）：研究轮落盘 / 本地台账重写 /
        # Harness SDK 下发（起子进程 + 落审计）/ 凭据（且凭据写在桥内被封死）。
        self.assertEqual(v3_mcp.NON_READONLY_PATHS,
                         {"/api/v3/strategy/run", "/api/v3/oms/sync", "/api/v3/sdk/prompt",
                          "/api/v3/credentials"})
        for path in v3_mcp.NON_READONLY_PATHS:
            self.assertIn(path, self.bridge.paths, path)

    def test_no_trade_endpoint_is_bridged(self):
        """桥里绝不能出现交易写端点（下单/改单/撤单/切模式/执行计划）。"""
        forbidden = ("trade", "switch-mode", "plan-execute", "confirm-decide", "sim_trade")
        for path in self.bridge.paths:
            lowered = path.lower()
            self.assertFalse(any(token in lowered for token in forbidden), path)

    def test_registered_schemas_are_closed(self):
        """``additionalProperties:false`` 与基础工具同一套封闭性保证。"""
        manager = self.app.state.mcp._tool_manager  # noqa: SLF001 —— SDK 无公开口子
        for name in self.bridge.names:
            tool = manager._tools[name]  # noqa: SLF001
            self.assertIs(tool.parameters.get("additionalProperties"), False, name)


# ---------------------------------------------------------------------------
# 1b. discovery 表面不变式（精确集合，不放宽成 >=）
# ---------------------------------------------------------------------------
class DiscoverySurfaceTests(unittest.TestCase):
    """discovery 模式 = 4 件直连保留 + 2 个代理入口；代理可达集合 ≡ direct 面。"""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp(prefix="v3-mcp-discovery-parity-"))
        cls.direct, _ = build_offline_app(cls.home, surface=mcp_discovery.DIRECT)
        cls.discovery, _ = build_offline_app(cls.home, surface=mcp_discovery.DISCOVERY)
        cls.proxy = cls.discovery.state.mcp_discovery
        _silence_loggers()

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_direct_surface_count(self):
        names = registered_names(self.direct.state.mcp)
        self.assertEqual(len(names), direct_surface(self.direct))
        self.assertEqual(names[:BASE_TOOLS],
                         [definition.name for definition in mcp_tools.TOOLS])
        self.assertEqual(names[BASE_TOOLS:], list(self.direct.state.v3_mcp_bridge.names))

    def test_discovery_surface_is_exactly_keep_plus_proxies(self):
        names = registered_names(self.discovery.state.mcp)
        self.assertEqual(len(names), DISCOVERY_SURFACE)
        self.assertEqual(names, list(mcp_discovery.DIRECT_KEEP)
                         + list(mcp_discovery.PROXY_NAMES))
        self.assertEqual(self.discovery.state.mcp_surface, mcp_discovery.DISCOVERY)

    def test_proxy_reachable_set_equals_direct_surface(self):
        """发现代理没丢能力：目录（=可转发集合）≡ direct 模式注册面，逐名相等。"""
        direct_names = set(registered_names(self.direct.state.mcp))
        self.assertEqual(direct_names, set(self.proxy.catalog))
        self.assertEqual(len(direct_names), direct_surface(self.direct))
        self.assertEqual(len(self.proxy.catalog), direct_surface(self.direct))

    def test_proxy_routes_to_the_same_function_objects(self):
        """转发的是**同一个函数对象**（不是同源代码）：代理面与直连面同一份实现。

        两种模式各自装配了一个 app（两个进程外实例），所以这里断言的是**本进程内**的同一性：
        代理取到的对象 ≡ 该进程装配时注册进 ToolManager 的那个（``direct`` app 的
        ``bridge.bound`` 与注册面同理，见 ``test_direct_surface_count`` 的顺序断言）。
        """
        manager = self.discovery.state.mcp._tool_manager  # noqa: SLF001
        # v3_* ：代理目标 = V3Bridge.bound（装配时注册的同一个对象）
        for name in ("v3_risk", "v3_metrics", "v3_tools"):
            self.assertIs(self.proxy.target(name), self.proxy.bridge.bound[name], name)
        # v3_tools / v3_gateway 这两个保留件同时也在注册面上 → 与注册面对象逐一同一个
        for name in ("v3_tools", "v3_gateway"):
            self.assertIs(manager._tools[name].fn, self.proxy.bridge.bound[name], name)
        # 基础面：代理目标 = 本进程 BoundTool.fn（保留件同时也在注册面上，为同一对象）
        base_by_name = {tool.name: tool for tool in self.discovery.state.mcp_tools}
        for name in ("snapshot", "series", "admin_status"):
            self.assertIs(self.proxy.target(name), base_by_name[name].fn, name)
        for name in ("snapshot", "admin_status"):
            self.assertIs(manager._tools[name].fn, base_by_name[name].fn, name)

    def test_proxy_only_tools_are_exactly_the_two_entries(self):
        """多出来的两个名字只能是 list_tools / call_tool（不多不少）。"""
        direct_names = set(registered_names(self.direct.state.mcp))
        discovery_names = set(registered_names(self.discovery.state.mcp))
        self.assertEqual(discovery_names - direct_names, set(mcp_discovery.PROXY_NAMES))
        self.assertEqual(direct_names - discovery_names,
                         direct_names - set(mcp_discovery.DIRECT_KEEP))


# ---------------------------------------------------------------------------
# 1c. 只读面 /mcp/ro（2026-09-21）：第二个传输端点，不影响路由⇄工具双射
# ---------------------------------------------------------------------------
class ReadonlySurfaceParityTests(unittest.TestCase):
    """``/mcp/ro`` 的存在不给既有推导式添乱。

    它是一条额外的**传输路由**（SDK Route 精确匹配 ``/mcp/ro``），不在 ``/api/v3/*`` 里，
    因此「``/api/v3/*`` 路由路径 ⇄ 桥接工具 endpoint」的双射推导式一字不改仍然成立；
    两个 MCP 端点的注册面互不相干，能力目录是同一份。
    """

    def test_readonly_endpoint_does_not_disturb_route_tool_bijection(self):
        home = Path(tempfile.mkdtemp(prefix="v3-mcp-ro-parity-"))
        try:
            app, _calls = build_offline_app(home, surface=mcp_discovery.DISCOVERY)
            bridge = app.state.v3_mcp_bridge
            # /mcp/ro 恰好一条传输路由，且不是 /api/v3/* 路由
            ro_routes = [route for route in app.routes
                         if getattr(route, "path", "") == "/mcp/ro"]
            self.assertEqual(len(ro_routes), 1)
            route_paths = sorted({route.path for route in app.routes
                                  if getattr(route, "path", "").startswith("/api/v3/")})
            # 同一个推导式照常成立（本模块第 1 层的那套断言，一字未改）
            report = assert_parity(route_paths, [d.endpoint for d in bridge.definitions])
            self.assertEqual(report, {"missing_tools": [], "orphan_tools": []})
            self.assertNotIn("/mcp/ro", route_paths)
            self.assertEqual(len(bridge.definitions), len(route_paths))
            # 两个端点的注册面互不相干：/mcp（discovery）6 件；/mcp/ro 也 6 件
            expected = list(mcp_discovery.DIRECT_KEEP) + list(mcp_discovery.PROXY_NAMES)
            self.assertEqual(registered_names(app.state.mcp), expected)
            self.assertEqual(registered_names(app.state.mcp_ro), expected)
            # 只读面的目录与 /mcp 面目录是同一份（同一实现、同一目录）
            self.assertEqual(set(app.state.mcp_ro_discovery.catalog),
                             set(app.state.mcp_discovery.catalog))
            # 只读面不随 QUANT_MCP_SURFACE 变：direct 模式下它照样是 6 件 discovery 形态
            direct, _ = build_offline_app(home, surface=mcp_discovery.DIRECT)
            self.assertEqual(registered_names(direct.state.mcp_ro), expected)
            self.assertIsNone(direct.state.mcp_discovery)
            self.assertEqual(direct.state.mcp_ro_discovery.catalog,
                             direct.state.mcp_ro_discovery.catalog)
        finally:
            import shutil
            shutil.rmtree(home, ignore_errors=True)
            _silence_loggers()


# ---------------------------------------------------------------------------
# 2. 真实协议调用（tools/list + tools/call）——两种模式各跑一遍
# ---------------------------------------------------------------------------
class ProtocolCallTests(unittest.TestCase):
    """走真 ``/mcp`` 协议：先本地已启动服务（direct 口径），再 in-process（两种模式）。"""

    def test_direct_tools_list_and_readonly_calls_in_process(self):
        async def work(client, app, calls):
            tools = await client.list_tools()
            names = [tool["name"] for tool in tools]
            # 基础面 + 全部 V3 桥接工具（一次遍历路由表得到，结构性成立）
            self.assertEqual(len(names), direct_surface(app))
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

        results = asyncio.run(_inproc(work, mcp_discovery.DIRECT))
        # 信封里必须带来源/时点（这两条端点的 handler 就是这么产出的，桥不加工）
        self.assertEqual(results["v3_risk"]["data"]["source"], "workbench/risk")
        self.assertEqual(results["v3_risk"]["data"]["as_of"], DAY)
        from server import v3_ops

        self.assertEqual(results["v3_tools"]["total"],
                         len(mcp_tools.TOOLS) + len(v3_ops.V3_LOCAL_TOOLS))
        self.assertIn("toolTotal", results["v3_metrics"])
        self.assertEqual(results["v3_credentials"]["ok"], True)

    def test_discovery_surface_and_proxy_calls_in_process(self):
        """discovery 模式：``tools/list`` 恰 6 件；未直连的工具经 ``call_tool`` 等价可得。"""
        async def work(client, app, calls):
            tools = await client.list_tools()
            names = [tool["name"] for tool in tools]
            self.assertEqual(len(names), DISCOVERY_SURFACE)
            self.assertEqual(names, list(mcp_discovery.DIRECT_KEEP)
                             + list(mcp_discovery.PROXY_NAMES))
            # 直连保留件在工具面上；其余 112 件只能经代理
            self.assertIn("v3_tools", names)
            self.assertNotIn("v3_metrics", names)
            self.assertNotIn("v3_risk", names)

            # 经代理调用未直连的只读工具：与「同一实现在 direct 模式下的注册结果」逐字段相同。
            # 对照物两种模式都取 ``v3_risk`` 的**同一个绑定对象**（``bridge.bound``）在 fake
            # handle 下的确定性输出——不依赖另一个进程，也不依赖哪个表面模式。
            bound = app.state.v3_mcp_bridge.bound["v3_risk"]
            direct_result = await bound()
            direct_envelope = json.loads(direct_result.content[0].text)
            is_error, proxy = await client.call_tool(
                "call_tool", {"name": "v3_risk", "arguments": {}})
            self.assertFalse(is_error)
            self.assertEqual(proxy, direct_envelope)

            # list_tools 检索：能回答「有没有这个能力、名字是什么、必填什么」
            _is_error, page = await client.call_tool(
                "list_tools", {"keyword": "回测", "limit": 5})
            self.assertTrue(page["ok"], page)
            self.assertLessEqual(len(page["cards"]), 5)
            self.assertTrue(page["total"] >= 1)
            for card in page["cards"]:
                self.assertTrue(card["name"])
                self.assertTrue(card["purpose"])
                self.assertIsInstance(card["required"], list)
            return {"direct": direct_envelope, "proxy": proxy}

        out = asyncio.run(_inproc(work, mcp_discovery.DISCOVERY))
        self.assertTrue(out["direct"]["ok"], out["direct"])
        self.assertEqual(out["proxy"], out["direct"])

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
                out = {"tools": len(names)}
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
        # 线上真值：只断言「不是一个荒谬的面」——具体条数取决于该进程启动时的路由表与
        # 表面模式（本机 8397 可能还是改动前的旧进程），因此这里放宽成结构判断：
        # 要么是 discovery 的 6 件，要么 ≥ 基础 77（direct 面）。不写死任何路由数。
        self.assertTrue(outcome["count"] == DISCOVERY_SURFACE or outcome["count"] >= BASE_TOOLS,
                        f"线上 tools/list 条数 {outcome['count']} 既不是 discovery"
                        f"（{DISCOVERY_SURFACE}）也不 ≥ 基础面（{BASE_TOOLS}）")


# ---------------------------------------------------------------------------
# 3. 通道分级：凭据写入在 MCP 面封死（两种模式）
# ---------------------------------------------------------------------------
class CredentialBoundaryTests(unittest.TestCase):
    """``save``/``clear`` 在桥内封死：handler 零调用、磁盘零写入、错误码固定。

    两种表面模式各断言一遍：discovery 模式下**唯一**能到达 ``v3_credentials`` 的路径就是
    ``call_tool`` 代理，封死必须在这条路径上同样生效。
    """

    def _exercise(self, surface, name_of, arguments):
        from server import v3_credentials

        async def work(client, app, calls):
            out = {}
            with unittest.mock.patch.object(v3_credentials, "save") as save_spy, \
                    unittest.mock.patch.object(v3_credentials, "clear") as clear_spy:
                for action in ("save", "clear"):
                    is_error, envelope = await client.call_tool(
                        name_of, arguments(action))
                    out[action] = (is_error, envelope)
                out["save_calls"] = save_spy.call_count
                out["clear_calls"] = clear_spy.call_count
            return out

        return asyncio.run(_inproc(work, surface))

    def _assert_blocked(self, out):
        for action in ("save", "clear"):
            is_error, envelope = out[action]
            self.assertFalse(is_error)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["error"]["code"], v3_mcp.CREDENTIALS_WEB_ONLY_CODE)
            self.assertIn("Web", envelope["error"]["message"])
        self.assertEqual(out["save_calls"], 0, "封死必须发生在 handler 之前")
        self.assertEqual(out["clear_calls"], 0)

    def test_save_and_clear_are_refused_in_direct_mode(self):
        out = self._exercise(
            mcp_discovery.DIRECT, "v3_credentials",
            lambda action: {"action": action, "key": "tushare_token",
                            "value": "should-never-be-written"})
        self._assert_blocked(out)

    def test_save_and_clear_are_refused_through_the_proxy(self):
        """discovery 模式：经 ``call_tool`` 转发的 save/clear 同样封死（不绕过闸门）。"""
        out = self._exercise(
            mcp_discovery.DISCOVERY, "call_tool",
            lambda action: {"name": "v3_credentials",
                            "arguments": {"action": action, "key": "tushare_token",
                                          "value": "should-never-be-written"}})
        self._assert_blocked(out)

    def test_status_stays_available(self):
        async def work(client, app, calls):
            is_error, status_envelope = await client.call_tool("v3_credentials",
                                                               {"action": "status"})
            self.assertFalse(is_error)
            self.assertIn("ok", status_envelope)
            return status_envelope

        envelope = asyncio.run(_inproc(work, mcp_discovery.DIRECT))
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
            app, _calls = build_offline_app(home, surface=mcp_discovery.DIRECT)
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
