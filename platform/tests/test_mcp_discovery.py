"""工具发现代理测试（规格 FR-TOOLS-003 / §10 决策 3）。

覆盖四层，逐层加严：

1. **检索真有用**：``list_tools`` 按 ``domain``（六域 + workbench）/``prefix``/``keyword``
   过滤，分页（默认 ≤20、上限 50、``next_offset``）、空结果给可执行提示、坏域报错；
   断言的是「输出能回答有没有这个能力、名字是什么、必填什么」——不是「返回了一个 dict」。
2. **转发等价**：``call_tool`` 与直连是**同一个函数对象**（``is`` 同一性）+ 同一实参下
   响应文本**逐字段相同**；必填缺失/野字段在代理层被拒（与直连 schema 层拒绝同一后果）。
3. **约束不绕过**：``v3_credentials`` 的 ``save``/``clear`` 经代理路径依然封死
   （``v3/credentials-web-only``、handler 零调用、磁盘零写入）；交易类工具不在目录里，
   代理因此不可能凭空造出交易能力。
4. **只读面 /mcp/ro**（2026-09-21 漏洞修复）：与 ``/mcp`` 同为 6 件；``call_tool`` 对写类
   （``trade_place`` / ``research_tasks_claim`` / ``v3_oms_sync`` / ``v3_strategy_run`` /
   ``admin_prune_runs`` 等）回 ``mcp/denied-by-policy`` 且 **handler 零调用**（spy 断言）；
   只读工具正常转发且与 ``/mcp`` 直连逐字段一致；凭据两条路径都仍然封死；卡片对写类标注
   「只读面不可调用」；放行集由注册表 annotations 推导（不维护第二份名单）。
5. **鉴别力（mutation）**：**真实**删掉一条 ``/api/v3/*`` 路由 → 目录与代理面必须
   同时变小、探针必须报出缺的那件；同一套探针在完整装配下必须通过。这条证明第 1/2 层
   不是恒真断言。

只读验证：所有真实调用只打只读工具（``v3_gateway`` / ``v3_tools`` / ``v3_risk`` /
``v3_credentials?action=status``），不触发 ``trade_*``/``sim_trade_*``/``plan-execute``/
``confirm-decide``/``switch-mode``。写类工具的「拒绝」用例**只断言被挡在闸门**（spy 证明
实现零调用），从不真正执行写动作。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_mcp_discovery -v``
"""
import asyncio
import copy
import json
import logging
import re
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from server import app as app_module  # noqa: E402
from server import mcp_discovery, mcp_tools, v3_credentials, v3_mcp  # noqa: E402

DAY = "2026-09-19"
PROTOCOL_VERSION = "2025-06-18"
#: direct 模式 = 工作台基础 77 + V3 桥接面（``/api/v3/*`` 路由条数）——**条数从桥推**，
#: 不写死（路由正在演进：v3_sdk / v3_headless / v3_alerts 并行落地）。
BASE_TOOLS = 77
#: discovery 模式 = 直连保留 4 件 + 2 个代理入口（精确集合，不放宽成 >=；与路由数无关）。
DISCOVERY_SURFACE = len(mcp_discovery.DIRECT_KEEP) + len(mcp_discovery.PROXY_NAMES)


def direct_surface(app):
    """direct 模式的精确期望条数 = 基础 77 + ``/api/v3/*`` 桥接件数（推导式）。"""
    return BASE_TOOLS + len(app.state.v3_mcp_bridge.names)


# ---------------------------------------------------------------------------
# 离线装配（与 test_mcp_parity 同一手法：真路由表 / 真 V3 接线 / 真 MCP 注册）
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
    return {
        "schedule": {"ok": True, "value": {"heartbeat": {"at": DAY}, "jobs": [],
                                           "kill": False, "halt": False}},
        "risk": {"ok": True, "value": {"config": {"singlePct": 2.0}, "source": "workbench/risk",
                                       "as_of": DAY}},
        "snapshot": {"ok": True, "value": {"mode": "sim", "generated_at": DAY}},
        "sources": {"ok": True, "value": {"channels": []}},
    }


def build_offline_app(surface, home=None, raw=None):
    """装配一个**离线**的真应用（真路由表 / 真 V3 子模块 / 真 MCP 桥）。"""
    home = Path(home or tempfile.mkdtemp(prefix="v3-mcp-discovery-"))
    raw = fake_raw() if raw is None else raw

    def fake_handle(endpoint, payload=None):
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
    return app


def registry_names(server):
    """装配后注册面的工具名（与 tools/list 同一份 ToolManager）。"""
    return [tool.name for tool in asyncio.run(server.list_tools())]


def call(server, name, arguments=None):
    """直接调注册面（不过协议层）——返回 ``CallToolResult``。"""
    return asyncio.run(server.call_tool(name, arguments or {}))


def envelope(result):
    return json.loads(result.content[0].text)


def _normalize(value):
    """把临时目录绝对路径归一化成占位符（两个 app 各用不同 tmp home，仅此一处不可比）。

    只替换 ``/tmp/<prefix>-<随机>`` 这种临时目录前缀，不动任何业务字段；比较仍然逐字段。
    """
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"/tmp/[A-Za-z0-9._-]+", "<tmp>", value)
    return value


def _silence_loggers():
    for name in ("httpx", "httpx2", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# MCP 客户端：真协议（in-process ASGI + 真 lifespan，与 parity 测试同源）
# ---------------------------------------------------------------------------
class McpClient:
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
                                           "clientInfo": {"name": "discovery-test",
                                                          "version": "1"}}})
        self.seq = 1
        await self.http.post(self.base, headers={"Accept": "application/json, text/event-stream",
                                                 "mcp-session-id": self.session},
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

    async def tool_result(self, name, arguments=None):
        result = await self._request("tools/call", {"name": name,
                                                    "arguments": arguments or {}})
        return result.get("isError"), json.loads(result["content"][0]["text"])

    async def call_tool(self, name, arguments=None):
        """走代理入口 ``call_tool``，返回（协议层 isError, 代理返回的信封）。"""
        payload = {"name": name}
        if arguments is not None:
            payload["arguments"] = arguments
        is_error, body = await self.tool_result("call_tool", payload)
        return is_error, body

    async def list_tools_entry(self, **query):
        _is_error, body = await self.tool_result("list_tools", query)
        return body


def inproc(surface, work, home=None, raw=None, base="/mcp"):
    """在真 lifespan 下跑一段异步工作（MCP session manager 必须有 lifespan）。

    ``base``：MCP 端点路径（``/mcp`` 缺省；只读面传 ``/mcp/ro``）。
    """
    app = build_offline_app(surface, home=home, raw=raw)
    try:
        async def runner():
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://127.0.0.1:8397") as http:
                    client = McpClient(http, base=base)
                    await client.initialize()
                    return await work(client, app)

        return asyncio.run(runner())
    finally:
        _silence_loggers()


# ---------------------------------------------------------------------------
# 1. 表面不变式 + 检索
# ---------------------------------------------------------------------------
class SurfaceInvariantTests(unittest.TestCase):
    """discovery 模式的暴露集合**精确**等于「直连保留 4 件 + 2 个代理入口」。"""

    @classmethod
    def setUpClass(cls):
        cls.app = build_offline_app(mcp_discovery.DISCOVERY)
        cls.proxy = cls.app.state.mcp_discovery
        _silence_loggers()

    def test_surface_is_exactly_keep_plus_proxies(self):
        names = registry_names(self.app.state.mcp)
        self.assertEqual(len(names), DISCOVERY_SURFACE)
        self.assertEqual(names, list(mcp_discovery.DIRECT_KEEP)
                         + list(mcp_discovery.PROXY_NAMES))
        self.assertEqual(len(set(names)), len(names), "工具名必须唯一")
        self.assertEqual(self.app.state.mcp_surface, mcp_discovery.DISCOVERY)

    def test_direct_keep_names_exist_in_the_registries(self):
        """4 件直连保留名必须真实存在于既有注册表（写错一个就装配期炸，不是静默少给）。"""
        catalog = self.proxy.catalog
        for name in mcp_discovery.DIRECT_KEEP:
            self.assertIn(name, catalog, name)
        self.assertEqual(len(catalog),
                         BASE_TOOLS + len(self.proxy.bridge.names),
                         "目录 = 两个既有注册表的并集（基础 77 + 全部桥接件），不重复、不遗漏")

    def test_direct_mode_is_unchanged_and_backward_compatible(self):
        app = build_offline_app(mcp_discovery.DIRECT)
        names = registry_names(app.state.mcp)
        self.assertEqual(len(names), direct_surface(app))
        self.assertIsNone(app.state.mcp_discovery)
        self.assertEqual(app.state.mcp_surface, mcp_discovery.DIRECT)
        # direct 模式不额外注册代理入口（名字面一字未变）
        for proxy_name in mcp_discovery.PROXY_NAMES:
            self.assertNotIn(proxy_name, names)

    def test_proxy_reaches_the_whole_direct_surface(self):
        """代理可达集合 ≡ direct 面（发现代理没丢能力）。"""
        direct = build_offline_app(mcp_discovery.DIRECT)
        direct_names = set(registry_names(direct.state.mcp))
        self.assertEqual(len(direct_names), direct_surface(direct))
        self.assertEqual(set(self.proxy.catalog), direct_names)

    def test_readonly_hints_on_the_six_exposed_tools(self):
        """暴露面的只读标注与**直接注册时同一份口径**（不替工具发明新元数据）。

        * ``list_tools`` 是检索：只读；``call_tool`` 能转发写类：不是只读；
        * ``v3_gateway`` / ``v3_tools`` 是桥接件：``readOnlyHint`` 与 direct 模式逐字段相同；
        * ``snapshot`` / ``admin_status`` 是基础件：基础面在 MCP 上**本来就不带 annotations**
          （``mcp_tools.register`` 不传），discovery 模式也不替它补一个——如实为 ``None``。
        """
        tools = {tool.name: tool for tool in asyncio.run(self.app.state.mcp.list_tools())}
        self.assertIs(tools["list_tools"].annotations.read_only_hint, True)
        self.assertIs(tools["call_tool"].annotations.read_only_hint, False)
        bridge = self.app.state.v3_mcp_bridge
        for name in ("v3_gateway", "v3_tools"):
            path = dict((d.name, d.endpoint) for d in bridge.definitions)[name]
            self.assertIs(tools[name].annotations.read_only_hint,
                          path not in v3_mcp.NON_READONLY_PATHS, name)
        for name in ("snapshot", "admin_status"):
            self.assertIsNone(tools[name].annotations, name)

    def test_bad_surface_value_is_rejected(self):
        """拼错的模式名**抛错**，绝不静默退回某个模式（否则「切了没生效」查不出来）。"""
        with self.assertRaises(mcp_discovery.SurfaceError):
            mcp_discovery.resolve_surface("disco")
        with self.assertRaises(mcp_discovery.SurfaceError):
            mcp_discovery.resolve_surface("Direct_")  # 下划线拼法不是合法取值
        with self.assertRaises(mcp_discovery.SurfaceError):
            mcp_discovery.resolve_surface("none")
        # 大小写与首尾空白宽容
        self.assertEqual(mcp_discovery.resolve_surface(" DIRECT "), mcp_discovery.DIRECT)
        self.assertEqual(mcp_discovery.resolve_surface(" discovery "), mcp_discovery.DISCOVERY)
        # 空值/缺省 → 规格要求的方向（discovery）
        self.assertEqual(mcp_discovery.resolve_surface(""), mcp_discovery.DEFAULT_SURFACE)
        self.assertEqual(mcp_discovery.resolve_surface(None), mcp_discovery.DEFAULT_SURFACE)
        self.assertEqual(mcp_discovery.DEFAULT_SURFACE, mcp_discovery.DISCOVERY)


class SearchTests(unittest.TestCase):
    """``list_tools`` 检索真有用：域/前缀/关键词/分页/空结果/坏域。"""

    @classmethod
    def setUpClass(cls):
        cls.app = build_offline_app(mcp_discovery.DISCOVERY)
        cls.proxy = cls.app.state.mcp_discovery
        cls.direct_names = set(registry_names(build_offline_app(mcp_discovery.DIRECT).state.mcp))
        _silence_loggers()

    def search(self, **query):
        return self.proxy.search(**query)

    def test_card_shape_answers_name_purpose_required(self):
        page = self.search(keyword="v3_ml_backtest")
        self.assertTrue(page["ok"], page)
        self.assertEqual(page["total"], 1)
        card = page["cards"][0]
        self.assertEqual(card["name"], "v3_ml_backtest")
        self.assertTrue(card["purpose"], "卡片必须有一句话用途")
        self.assertEqual(card["required"], ["ticker"])
        self.assertIn("rebalanceDays", card["optional"])
        # 卡片**不含**完整 schema：没有 properties/inputSchema 这类键
        for forbidden in ("inputSchema", "properties", "parameters", "schema"):
            self.assertNotIn(forbidden, card)

    def test_keyword_matches_name_and_purpose(self):
        by_name = self.search(keyword="orderbook")
        self.assertIn("v3_orderbook", {card["name"] for card in by_name["cards"]})
        # 「盘口」只出现在用途（描述）里，不在任何工具名里 → 证明关键词也匹配用途
        by_purpose = self.search(keyword="盘口")
        names = {card["name"] for card in by_purpose["cards"]}
        self.assertIn("v3_orderbook", names)
        self.assertNotIn("orderbook", names)  # 名字里没有「盘口」二字
        # 多词是 AND
        both = self.search(keyword="回测 参数")
        self.assertTrue(all("回测" in card["purpose"] or "回测" in card["name"]
                            or "参数" in card["purpose"] or "参数" in card["name"]
                            for card in both["cards"]))

    def test_domain_filter_covers_six_domains_plus_workbench(self):
        for domain in ("data", "alpha", "ml", "risk", "execution", "ecosystem",
                       mcp_discovery.WORKBENCH_DOMAIN):
            page = self.search(domain=domain, limit=mcp_discovery.MAX_PAGE)
            self.assertTrue(page["ok"], domain)
            self.assertTrue(all(card["domain"] == domain for card in page["cards"]), domain)
        self.assertIn("ml", self.search(domain="ml")["domains"])
        self.assertIn("workbench", self.search(domain="workbench")["domains"])

    def test_prefix_filter(self):
        page = self.search(prefix="v3_ml")
        self.assertEqual({card["name"] for card in page["cards"]},
                         {"v3_ml_backtest", "v3_ml_models", "v3_ml_sweep"})
        admin = self.search(prefix="admin_")
        self.assertTrue(admin["cards"])
        self.assertTrue(all(card["name"].startswith("admin_") for card in admin["cards"]))
        self.assertFalse(self.search(prefix="no_such_prefix_")["cards"])

    def test_pagination_default_and_cap(self):
        first = self.search()
        self.assertEqual(first["total"], direct_surface(self.app))
        self.assertEqual(first["count"], mcp_discovery.DEFAULT_PAGE)
        self.assertEqual(len(first["cards"]), mcp_discovery.DEFAULT_PAGE)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], mcp_discovery.DEFAULT_PAGE)
        second = self.search(offset=first["next_offset"])
        self.assertFalse({card["name"] for card in first["cards"]}
                         & {card["name"] for card in second["cards"]},
                         "两页不得重叠")
        # 上限 50：给 500 也只回 50（防「换个姿势一次拉全量」）
        self.assertEqual(self.search(limit=500)["limit"], mcp_discovery.MAX_PAGE)
        self.assertEqual(len(self.search(limit=500)["cards"]), mcp_discovery.MAX_PAGE)
        # 一直翻到底：并集 ≡ direct 面（分页不漏不重；条数由注册表推）
        seen, offset = [], 0
        while True:
            page = self.search(offset=offset)
            seen.extend(card["name"] for card in page["cards"])
            if not page["has_more"]:
                break
            offset = page["next_offset"]
        self.assertEqual(len(seen), direct_surface(self.app))
        self.assertEqual(set(seen), self.direct_names)
        # offset 越界 → 空页（不是报错，也不是回退到第一页）
        tail = self.search(offset=direct_surface(self.app) + 10)
        self.assertEqual(tail["cards"], [])
        self.assertFalse(tail["has_more"])

    def test_empty_result_has_an_actionable_hint(self):
        page = self.search(keyword="绝不存在的能力zzz")
        self.assertTrue(page["ok"], "空结果是正常结果，不是错误")
        self.assertEqual(page["total"], 0)
        self.assertEqual(page["cards"], [])
        self.assertIn("hint", page)
        self.assertIn("list_tools", page["hint"])
        self.assertTrue(any(domain in page["hint"] for domain in ("data", "alpha", "ml")))

    def test_unknown_domain_is_an_explicit_error(self):
        page = self.search(domain="nope")
        self.assertFalse(page["ok"])
        self.assertEqual(page["error"]["code"], "mcp/unknown-domain")
        self.assertIn("data", page["error"]["message"])

    def test_list_tools_over_the_wire(self):
        """真协议：``list_tools`` 入口可达，返回同一份检索结果。"""
        async def work(client, app):
            return (await client.list_tools_entry(domain="ml"),
                    await client.list_tools_entry(keyword="盘口"))

        ml, orderbook = inproc(mcp_discovery.DISCOVERY, work)
        self.assertTrue(ml["ok"], ml)
        self.assertEqual({card["name"] for card in ml["cards"]},
                         {"v3_ml_backtest", "v3_ml_models", "v3_ml_sweep"})
        self.assertIn("v3_orderbook", {card["name"] for card in orderbook["cards"]})


# ---------------------------------------------------------------------------
# 2. 转发等价
# ---------------------------------------------------------------------------
class ForwardingEquivalenceTests(unittest.TestCase):
    """``call_tool`` 与直连：同一个函数对象 + 同一实参下逐字段相同。

    对照物取**同一进程里的那个函数对象**（``proxy.target(name)``）：代理若自己重写一份实现、
    或改了信封，这里的逐字段比较立刻红。跨进程比 ``is`` 没有意义（每个 app 各自 build 一次），
    所以同一性断言只比「本进程注册面 vs 代理目标」。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = build_offline_app(mcp_discovery.DISCOVERY)
        cls.proxy = cls.app.state.mcp_discovery
        _silence_loggers()

    def test_proxy_targets_are_the_same_function_objects(self):
        """``is`` 同一性：代理转发目标 = 本进程注册进 ToolManager 的同一个函数对象。"""
        manager = self.app.state.mcp._tool_manager  # noqa: SLF001
        # 桥接面：代理目标 = V3Bridge.bound（装配时注册的同一个对象）
        for name in ("v3_gateway", "v3_tools", "v3_risk", "v3_metrics"):
            self.assertIs(self.proxy.target(name), self.proxy.bridge.bound[name], name)
        # 桥接保留件同时也在注册面上 → 注册面对象与 bound 也是同一个
        for name in ("v3_gateway", "v3_tools"):
            self.assertIs(manager._tools[name].fn, self.proxy.bridge.bound[name], name)
        # 基础面：代理目标 = 本进程 BoundTool.fn；保留件同时在注册面上（同一对象）
        base_by_name = {tool.name: tool for tool in self.app.state.mcp_tools}
        for name in ("snapshot", "series", "admin_status", "trade_place"):
            self.assertIs(self.proxy.target(name), base_by_name[name].fn, name)
        for name in ("snapshot", "admin_status"):
            self.assertIs(manager._tools[name].fn, base_by_name[name].fn, name)

    def test_forwarded_results_are_field_identical(self):
        """同一实参：**同一个函数对象直调** vs 经代理，响应文本逐字段相同。

        （对照物就是代理要转发的那个对象——所以这条断言真正钉的是「代理没有第二份实现、
        没有改写信封、没有吞字段」。``refresh`` 默认语义也一起覆盖。）
        """
        cases = (
            ("v3_tools", {}),
            ("v3_risk", {}),
            ("v3_credentials", {"action": "status"}),
            ("snapshot", {}),
            ("snapshot", {"refresh": True}),
            ("v3_tools", {"domain": "risk"}),
        )
        for name, arguments in cases:
            with self.subTest(tool=name, arguments=arguments):
                target = self.proxy.target(name)
                filled = {field: mcp_tools.UNSET for field in self.proxy.catalog[name].optional}
                filled.update(arguments)
                direct_result = target(**filled)
                if hasattr(direct_result, "__await__"):
                    direct_result = asyncio.run(direct_result)
                proxy_result = call(self.app.state.mcp, "call_tool",
                                    {"name": name, "arguments": arguments})
                self.assertEqual(direct_result.is_error, proxy_result.is_error)
                self.assertEqual(envelope(direct_result), envelope(proxy_result))

    def test_forwarding_over_the_wire_matches_direct_call(self):
        """真协议：``call_tool`` 的信封与同一对象的直调相同。"""
        async def work(client, app):
            target = app.state.mcp_discovery.target("v3_tools")
            direct_result = await target()
            direct_envelope = json.loads(direct_result.content[0].text)
            _is_error, via_proxy = await client.call_tool("v3_tools", {})
            return direct_envelope, via_proxy

        direct_envelope, via_proxy = inproc(mcp_discovery.DISCOVERY, work)
        self.assertEqual(direct_envelope, via_proxy)

    def test_business_failure_keeps_iserror_false(self):
        """业务失败仍是正常工具结果（与直连口径一致）——用一个业务上失败的只读调用触发。"""
        async def runner():
            return await self.proxy.call("v3_credentials", {"action": "save"})

        result, card, target = asyncio.run(runner())
        # save 被封死：是**代理转发的业务失败**（isError=false + 错误信封），不是协议错误
        self.assertIsNotNone(card)
        self.assertIsNotNone(target)
        self.assertFalse(result.is_error)
        body = envelope(result)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], v3_mcp.CREDENTIALS_WEB_ONLY_CODE)

    def test_missing_required_argument_is_refused(self):
        async def runner():
            return await self.proxy.call("v3_ml_backtest", {})

        result, card, target = asyncio.run(runner())
        self.assertIsNone(target, "实参不合法时不得触达实现")
        body = envelope(result)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "mcp/bad-arguments")
        self.assertIn("ticker", body["error"]["message"])
        self.assertFalse(result.is_error, "业务失败不是协议错误")

    def test_unknown_argument_is_refused(self):
        async def runner():
            return await self.proxy.call("v3_gateway", {"market": "SH"})

        result, _card, target = asyncio.run(runner())
        self.assertIsNone(target)
        body = envelope(result)
        self.assertEqual(body["error"]["code"], "mcp/bad-arguments")
        self.assertIn("market", body["error"]["message"])


# ---------------------------------------------------------------------------
# 3. 约束不绕过
# ---------------------------------------------------------------------------
class ConstraintBoundaryTests(unittest.TestCase):
    """凭据封死与交易边界在代理路径下依然成立。"""

    @classmethod
    def setUpClass(cls):
        cls.app = build_offline_app(mcp_discovery.DISCOVERY)
        cls.proxy = cls.app.state.mcp_discovery
        _silence_loggers()

    def test_credentials_save_and_clear_stay_blocked_via_proxy(self):
        from server import v3_credentials

        async def runner():
            out = {}
            with unittest.mock.patch.object(v3_credentials, "save") as save_spy, \
                    unittest.mock.patch.object(v3_credentials, "clear") as clear_spy:
                for action in ("save", "clear"):
                    result, _card, _target = await self.proxy.call(
                        "v3_credentials", {"action": action, "key": "tushare_token",
                                           "value": "should-never-be-written"})
                    out[action] = result
                out["save_calls"] = save_spy.call_count
                out["clear_calls"] = clear_spy.call_count
            return out

        out = asyncio.run(runner())
        for action in ("save", "clear"):
            result = out[action]
            self.assertFalse(result.is_error)
            body = envelope(result)
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"]["code"], v3_mcp.CREDENTIALS_WEB_ONLY_CODE)
            self.assertIn("Web", body["error"]["message"])
        self.assertEqual(out["save_calls"], 0, "封死必须发生在 handler 之前")
        self.assertEqual(out["clear_calls"], 0)

    def test_no_trade_tool_is_reachable_through_the_proxy(self):
        """代理可达集合里没有任何交易写工具（``/api/v3/*`` 面本来就没有）。"""
        forbidden = ("trade_place", "trade_modify", "trade_cancel", "switch_mode",
                     "plan_execute", "confirm_decide", "sim_trade")
        for name in forbidden:
            if name in self.proxy.catalog:  # 基础面里存在的（switch_mode/plan_execute）只在目录里
                self.assertNotEqual(
                    self.proxy.catalog[name].readonly, True,
                    f"{name} 是写类工具，卡片不得标只读")
        # 桥接面里绝无交易路径（与 direct 模式同一条断言）
        for path in self.proxy.bridge.paths:
            lowered = path.lower()
            self.assertFalse(any(token in lowered
                                 for token in ("trade", "switch-mode", "plan-execute",
                                               "confirm-decide", "sim_trade")), path)

    def test_unknown_tool_error_mentions_list_tools(self):
        async def runner():
            return await self.proxy.call("no_such_tool_zzz", {})

        result, card, target = asyncio.run(runner())
        self.assertIsNone(card)
        self.assertIsNone(target)
        self.assertFalse(result.is_error, "未知工具是业务失败（isError=false），与直连同口径")
        body = envelope(result)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "mcp/unknown-tool")
        self.assertIn("no_such_tool_zzz", body["error"]["message"])
        self.assertIn("list_tools", body["error"]["message"])

    def test_unknown_tool_over_the_wire(self):
        async def work(client, app):
            return await client.call_tool("no_such_tool_zzz", {})

        is_error, body = inproc(mcp_discovery.DISCOVERY, work)
        self.assertFalse(is_error)
        self.assertEqual(body["error"]["code"], "mcp/unknown-tool")
        self.assertIn("list_tools", body["error"]["message"])


# ---------------------------------------------------------------------------
# 3b. 只读面 /mcp/ro（2026-09-21 漏洞修复：call_tool 的服务侧硬边界）
# ---------------------------------------------------------------------------
class _CallSpy:
    """包住原函数对象的调用计数器（async 函数：原样返回协程，由调用方 await）。"""

    def __init__(self, fn):
        self.fn = fn
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.fn(*args, **kwargs)


class _SpyTargets:
    """把 call_tool 会转发的目标函数对象换成计数器（进 with 装上、出 with 原样还原）。

    桥接面换 ``bridge.bound[name]``（同一 dict 槽位，代理每次现读）；基础面换
    ``BoundTool.fn``（``patch.object`` 实例属性）。读数 ``calls(name)`` 即该实现的
    真实被调次数——拒绝路径必须是 0。
    """

    def __init__(self, testcase, names):
        self.testcase = testcase
        self.names = tuple(names)
        self.spies = {}
        self._restores = []

    def __enter__(self):
        for name in self.names:
            if name in self.testcase.bridge.bound:
                original = self.testcase.bridge.bound[name]
                spy = _CallSpy(original)
                self.testcase.bridge.bound[name] = spy
                self._restores.append(
                    lambda n=name, o=original: self.testcase.bridge.bound.__setitem__(n, o))
            else:
                bound = self.testcase.base_by_name[name]
                spy = _CallSpy(bound.fn)
                patcher = unittest.mock.patch.object(bound, "fn", spy)
                patcher.start()
                self._restores.append(patcher.stop)
            self.spies[name] = spy
        return self

    def __exit__(self, *exc):
        for restore in self._restores:
            restore()
        return False

    def calls(self, name):
        return self.spies[name].calls


class ReadonlySurfaceTests(unittest.TestCase):
    """/mcp/ro：同一实现、同一目录，call_tool 只放行注册表标注只读的内层工具。

    背景（platform/server/mcp_discovery.py 模块 docstring「只读面」）：缺省 discovery 面下
    写/交易工具的唯一到达路径是 ``call_tool`` 转发，而 Harness 白名单钩子按工具名匹配、
    看不到被转发的内层名字——2026-09-21T00:01Z 一次线上决策唤醒经它误领了 2 条值班任务。
    本面把这条边界下沉到**服务侧**：判据现读注册表 annotations（``readOnlyHint=True`` ∪
    ``DIRECT_KEEP``），不维护第二份名单。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = build_offline_app(mcp_discovery.DISCOVERY)
        cls.proxy = cls.app.state.mcp_ro_discovery
        cls.bridge = cls.app.state.v3_mcp_bridge
        cls.base_by_name = {tool.name: tool for tool in cls.app.state.mcp_tools}
        _silence_loggers()

    # -- spy 装置见 _SpyTargets（模块级，本类与 mutation 用例共用语义）--------------

    def test_ro_surface_is_exactly_the_same_six_tools(self):
        """/mcp/ro 的 tools/list 与 /mcp 同为 6 件，逐名相同、顺序相同。"""
        main_names = registry_names(self.app.state.mcp)
        ro_names = registry_names(self.app.state.mcp_ro)
        self.assertEqual(ro_names, list(mcp_discovery.DIRECT_KEEP)
                         + list(mcp_discovery.PROXY_NAMES))
        self.assertEqual(ro_names, main_names)
        self.assertEqual(len(ro_names), DISCOVERY_SURFACE)

    def test_ro_surface_annotations_mirror_the_registry(self):
        """直连保留件复用同一份标注；两个代理入口的标注按本面语义如实给出。"""
        tools = {tool.name: tool for tool in asyncio.run(self.app.state.mcp_ro.list_tools())}
        self.assertIs(tools["list_tools"].annotations.read_only_hint, True)
        # 只读面的 call_tool **只会**转发只读工具（闸门在绑定里）→ 如实标 readOnlyHint=True
        self.assertIs(tools["call_tool"].annotations.read_only_hint, True)
        bridge = self.bridge
        for name in ("v3_gateway", "v3_tools"):
            self.assertIs(tools[name].annotations.read_only_hint, True, name)
        for name in ("snapshot", "admin_status"):
            self.assertIsNone(tools[name].annotations, name)
        # 直连保留件是**同一个函数对象**（不是同源代码的复制品）
        for name in ("v3_gateway", "v3_tools"):
            self.assertIs(self.proxy.target(name), bridge.bound[name], name)
        for name in ("snapshot", "admin_status"):
            self.assertIs(self.proxy.target(name), self.base_by_name[name].fn, name)

    def test_allow_set_is_derived_from_registry_annotations(self):
        """放行集 = 注册表 annotations 快照的 readOnlyHint=True ∪ DIRECT_KEEP（无第二份名单）。

        对照物必须是**快照本身**（``proxy.ro_annotations``，discovery 移出注册面之前取）——
        装配后 /mcp 注册面只剩 6 件，从现读注册表反推会得到假差集（这正是要快照的原因）。
        桥接件再与 ``NON_READONLY_PATHS`` 交叉核对（两份同源声明的方向必须一致）。
        """
        self.assertTrue(self.proxy.ro_annotations, "快照不得为空")
        self.assertEqual(self.proxy.ro_allowed,
                         mcp_discovery.readonly_allow_set(self.proxy.ro_annotations))
        # 抽查方向：只读桥接件放行；桥接写类与基础写类一律不放行
        for name in ("v3_sentiment", "v3_risk", "v3_metrics"):
            self.assertIn(name, self.proxy.ro_allowed, name)
        for name in ("v3_oms_sync", "v3_strategy_run", "v3_sdk_prompt", "v3_credentials",
                     "trade_place", "research_tasks_claim", "admin_prune_runs",
                     "switch_mode", "plan_execute"):
            self.assertNotIn(name, self.proxy.ro_allowed, name)
        # 判据确实来自 annotations，而不是目录里的 readOnly 卡片字段：
        # 基础面只读工具（series）无标注 → 也不放行（fail-closed，README 有说明）
        self.assertIsNone(self.proxy.ro_annotations.get("series"))
        self.assertNotIn("series", self.proxy.ro_allowed)
        # 桥接面交叉核对：放行 ⟺ endpoint 不在 NON_READONLY_PATHS（同源声明的两个落点）
        path_of = {d.name: d.endpoint for d in self.bridge.definitions}
        for name, endpoint in path_of.items():
            self.assertEqual(name in self.proxy.ro_allowed,
                             endpoint not in v3_mcp.NON_READONLY_PATHS, name)

    def test_write_class_tools_are_denied_with_zero_handler_calls(self):
        """写类内层工具 → ``mcp/denied-by-policy``，且 **handler 零调用**（spy 断言）。"""
        write_class = ("trade_place", "research_tasks_claim", "v3_oms_sync",
                       "v3_strategy_run", "admin_prune_runs", "switch_mode")
        async def runner():
            out = {}
            with _SpyTargets(self, write_class) as spy:
                for name in write_class:
                    result = await self.app.state.mcp_ro.call_tool(
                        "call_tool", {"name": name, "arguments": {}})
                    out[name] = result
                out["calls"] = {name: spy.calls(name) for name in write_class}
            return out

        out = asyncio.run(runner())
        for name in write_class:
            with self.subTest(tool=name):
                result = out[name]
                self.assertFalse(result.is_error, "策略拒绝是业务失败信封，不是协议错误")
                body = envelope(result)
                self.assertFalse(body["ok"])
                self.assertEqual(body["error"]["code"], mcp_discovery.DENIED_BY_POLICY_CODE)
                self.assertIn(mcp_discovery.DENIED_BY_POLICY_MESSAGE, body["error"]["message"])
                self.assertIn("工作台", body["error"]["message"])
        for name, calls in out["calls"].items():
            self.assertEqual(calls, 0, f"{name} 的 handler 必须零调用（拒绝先于实现）")

    def test_denial_precedes_argument_validation(self):
        """空载荷的写调用也是策略拒绝（不是 mcp/bad-arguments）——闸门在最外层。"""
        async def runner():
            return await self.app.state.mcp_ro.call_tool("call_tool",
                                                         {"name": "trade_place",
                                                          "arguments": {}})

        result = asyncio.run(runner())
        body = envelope(result)
        self.assertEqual(body["error"]["code"], mcp_discovery.DENIED_BY_POLICY_CODE)

    def test_unknown_tool_stays_unknown_on_ro(self):
        async def runner():
            return await self.app.state.mcp_ro.call_tool("call_tool",
                                                         {"name": "no_such_tool_zzz", "arguments": {}})

        result = asyncio.run(runner())
        body = envelope(result)
        self.assertEqual(body["error"]["code"], "mcp/unknown-tool")
        self.assertNotIn(mcp_discovery.DENIED_BY_POLICY_CODE, json.dumps(body, ensure_ascii=False))

    def test_readonly_tools_forward_identically_on_both_surfaces(self):
        """只读工具经 /mcp/ro 转发与经 /mcp 转发（以及同一函数对象直调）**逐字段一致**。

        用例取**确定性**只读工具（``v3_tools`` / ``v3_risk`` / ``snapshot``——假 handle 下
        输出恒定）。``v3_metrics``（调用计数器）与 ``v3_gateway``（``generated_at`` 微秒）
        两次调用之间必然不同，属被测工具自身的时变输出，不进逐字段对照。
        """
        readonly_cases = (("v3_tools", {}), ("v3_risk", {}), ("snapshot", {}),
                          ("snapshot", {"refresh": True}))
        async def runner():
            out = {"ro": {}, "main": {}, "direct": {}}
            for name, arguments in readonly_cases:
                out["ro"][name] = await self.app.state.mcp_ro.call_tool(
                    "call_tool", {"name": name, "arguments": arguments})
                out["main"][name] = await self.app.state.mcp.call_tool(
                    "call_tool", {"name": name, "arguments": arguments})
                target = self.proxy.target(name)
                filled = {field: mcp_tools.UNSET for field in self.proxy.catalog[name].optional}
                filled.update(arguments)
                direct = target(**filled)
                if hasattr(direct, "__await__"):
                    direct = await direct
                out["direct"][name] = direct
            return out

        out = asyncio.run(runner())
        for name, _arguments in readonly_cases:
            with self.subTest(tool=name):
                ro_body = envelope(out["ro"][name])
                self.assertTrue(ro_body["ok"], ro_body)
                self.assertEqual(ro_body, envelope(out["main"][name]), name)
                self.assertEqual(ro_body, envelope(out["direct"][name]), name)

    def test_ro_over_the_wire_denies_write_and_forwards_readonly(self):
        """真协议（/mcp/ro 端点）：写类被拒、只读放行、列表卡片带 roCallable 标注。"""
        async def work(client, app):
            out = {}
            # McpClient.call_tool(name, …) 的 name 就是**内层**名（助手自己套 call_tool 外壳）
            out["denied"] = await client.call_tool("research_tasks_claim", {})
            out["forwarded"] = await client.call_tool("v3_tools", {})
            _is_error, page = await client.tool_result("list_tools", {"keyword": "v3_oms_sync"})
            out["cards"] = page["cards"]
            _is_error, ro_page = await client.tool_result("list_tools",
                                                          {"keyword": "v3_oms_orders"})
            out["read_only_cards"] = ro_page["cards"]
            out["surface"] = page.get("surface")
            return out

        out = inproc(mcp_discovery.DISCOVERY, work, base="/mcp/ro")
        is_error, body = out["denied"]
        self.assertFalse(is_error)
        self.assertEqual(body["error"]["code"], mcp_discovery.DENIED_BY_POLICY_CODE)
        self.assertIn(mcp_discovery.DENIED_BY_POLICY_MESSAGE, body["error"]["message"])
        is_error, forwarded = out["forwarded"]
        self.assertFalse(is_error)
        self.assertTrue(forwarded["ok"], forwarded)
        self.assertEqual(out["surface"], {"path": mcp_discovery.READONLY_SURFACE_PATH,
                                          "readOnlyOnly": True})
        self.assertEqual([card["name"] for card in out["cards"]], ["v3_oms_sync"])
        for card in out["cards"]:
            self.assertIs(card["roCallable"], False, card)
            self.assertIn(mcp_discovery.RO_NOT_CALLABLE, card["roNote"])
        # 对照：只读兄弟件（v3_oms_orders）的卡片放行且无标注
        self.assertEqual([card["name"] for card in out["read_only_cards"]], ["v3_oms_orders"])
        for card in out["read_only_cards"]:
            self.assertIs(card["roCallable"], True, card)
            self.assertNotIn("roNote", card)

    def test_ro_cards_mark_write_class_not_callable(self):
        """只读面检索页：写类卡片标注「只读面不可调用」；/mcp 面的卡片**没有**这些字段。"""
        write_names = ("trade_place", "research_tasks_claim", "v3_oms_sync",
                       "v3_strategy_run", "admin_prune_runs")
        for name in write_names:
            page = self.proxy.search(keyword=name, ro=True)
            self.assertEqual(page["total"], 1, name)
            card = page["cards"][0]
            self.assertIs(card["roCallable"], False, name)
            self.assertIn(mcp_discovery.RO_NOT_CALLABLE, card["roNote"])
            self.assertIn("工作台", card["roNote"])
        # 只读桥接件：可调用、无标注
        page = self.proxy.search(keyword="v3_sentiment", ro=True)
        card = page["cards"][0]
        self.assertIs(card["roCallable"], True)
        self.assertNotIn("roNote", card)
        # 对照：/mcp 面的检索页不加 roCallable/roNote（表面行为一字不变）
        plain = self.app.state.mcp_discovery.search(keyword="trade_place")
        self.assertNotIn("roCallable", plain["cards"][0])
        self.assertNotIn("roNote", plain["cards"][0])
        self.assertNotIn("surface", plain)
        # 「看到」不等于「能调」：只读面检索页仍覆盖全目录
        self.assertEqual(self.proxy.search(ro=True)["total"],
                         self.app.state.mcp_discovery.search()["total"])

    def test_credentials_blocked_on_both_surfaces_by_two_distinct_layers(self):
        """凭据 save/clear：/mcp/ro 被 annotations 闸门拒绝；/mcp 被桥内封死——都零 handler。"""
        from server import v3_credentials

        async def runner():
            out = {}
            with unittest.mock.patch.object(v3_credentials, "save") as save_spy, \
                    unittest.mock.patch.object(v3_credentials, "clear") as clear_spy:
                for surface_key, server in (("ro", self.app.state.mcp_ro),
                                            ("main", self.app.state.mcp)):
                    for action in ("save", "clear"):
                        result = await server.call_tool(
                            "call_tool",
                            {"name": "v3_credentials",
                             "arguments": {"action": action, "key": "tushare_token",
                                           "value": "should-never-be-written"}})
                        out[f"{surface_key}/{action}"] = envelope(result)
                out["save_calls"] = save_spy.call_count
                out["clear_calls"] = clear_spy.call_count
            return out

        out = asyncio.run(runner())
        for action in ("save", "clear"):
            # 只读面：annotations 闸门在最外层（readOnlyHint=False → 拒绝）
            body = out[f"ro/{action}"]
            self.assertEqual(body["error"]["code"], mcp_discovery.DENIED_BY_POLICY_CODE)
            self.assertIn(mcp_discovery.DENIED_BY_POLICY_MESSAGE, body["error"]["message"])
            # /mcp 面：桥内封死分支不变（v3/credentials-web-only）
            body = out[f"main/{action}"]
            self.assertEqual(body["error"]["code"], v3_mcp.CREDENTIALS_WEB_ONLY_CODE)
            self.assertIn("Web", body["error"]["message"])
        self.assertEqual(out["save_calls"], 0, "两条路径都必须零 handler 调用")
        self.assertEqual(out["clear_calls"], 0)

    def test_research_tasks_list_is_absent_by_design(self):
        """``research_tasks_list`` 有意不在工具面（队列清单给人看）→ 只读放行不适用于它。"""
        self.assertNotIn("research_tasks_list", self.proxy.catalog)

    def test_main_surface_keeps_forwarding_write_class(self):
        """对照（边界只在 /mcp/ro）：/mcp 的 call_tool 对写类**没有**策略拒绝（现状不变）。"""
        async def runner():
            return await self.app.state.mcp.call_tool("call_tool",
                                                      {"name": "research_tasks_claim",
                                                       "arguments": {}})

        result = asyncio.run(runner())
        body = envelope(result)
        self.assertNotEqual(body["error"]["code"], mcp_discovery.DENIED_BY_POLICY_CODE)

    def test_ro_surface_requires_auth_when_token_is_set(self):
        """token 鉴权的 ``/mcp/`` 前缀判定覆盖 /mcp/ro：无 Bearer → 401（不出现未鉴权旁路）。

        ``build_offline_app`` 传的 ``config={}`` 与中间件闭包里读的是**同一个 dict**，
        这里就地填上 token 再走一遍中间件。
        """
        app = build_offline_app(mcp_discovery.DISCOVERY)
        app.state.config["token"] = "secret"

        async def runner():
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://127.0.0.1:8397") as http:
                    unauth = await http.post(
                        "/mcp/ro", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": PROTOCOL_VERSION,
                                                    "capabilities": {}}},
                        headers={"Accept": "application/json, text/event-stream"})
                    authed = await http.post(
                        "/mcp/ro", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": PROTOCOL_VERSION,
                                                    "capabilities": {}}},
                        headers={"Accept": "application/json, text/event-stream",
                                 "Authorization": "Bearer secret"})
                    return unauth.status_code, authed.status_code

        unauth_status, authed_status = asyncio.run(runner())
        self.assertEqual(unauth_status, 401)
        self.assertEqual(authed_status, 200)


# ---------------------------------------------------------------------------
# 4. 鉴别力（mutation）：真实删掉一条路由 → 断言必须红
# ---------------------------------------------------------------------------
def coverage_probe(catalog, bridge):
    """唯一事实来源探针：目录是否精确覆盖「基础注册表 ∪ 桥接定义」。

    返回 ``(ok, report)``——这是**生产装配期真用的那份判定**（``DiscoveryProxy`` 的目录
    直接来自同一对注册表）。mutation 用例把它跑在「真删一条路由」的桥上，必须报红。
    """
    expected = {definition.name for definition in mcp_tools.TOOLS} | set(bridge.names)
    actual = set(catalog)
    report = {"missing": sorted(expected - actual), "orphan": sorted(actual - expected)}
    return (not report["missing"] and not report["orphan"]), report


class MutationGuardTests(unittest.TestCase):
    """证明上面的断言不是恒真：真删一条 ``/api/v3/*`` 路由后，目录/代理面必须同时变小。"""

    @classmethod
    def setUpClass(cls):
        cls.app = build_offline_app(mcp_discovery.DISCOVERY)
        cls.proxy = cls.app.state.mcp_discovery
        _silence_loggers()

    def test_intact_assembly_passes_the_probe(self):
        ok, report = coverage_probe(self.proxy.catalog, self.proxy.bridge)
        self.assertTrue(ok, report)
        self.assertEqual(len(self.proxy.catalog),
                         BASE_TOOLS + len(self.proxy.bridge.names))

    def test_really_removing_a_route_shrinks_the_catalog_and_the_probe_goes_red(self):
        """真删：从装配好的 app 上摘掉 ``/api/v3/risk/analytics`` 路由 → 桥接面少一件。

        两件事同时断言，才叫「真删」：
        * 新桥的**工具名集合**比原桥少了那一件（``len`` 与名字一起缩水）；
        * 拿旧目录（未删时的 118 件）与新桥做覆盖性比对 → 探针报**缺件**（方向正确）。
        """
        victim = "/api/v3/risk/analytics"
        saved = [route for route in self.app.routes if getattr(route, "path", "") == victim]
        self.assertTrue(saved, "被删的路由必须真实存在（否则这条用例自己就是恒真的）")
        for route in saved:
            self.app.routes.remove(route)
        try:
            trimmed = v3_mcp.V3Bridge(self.app)
            self.assertEqual(len(trimmed.names), len(self.proxy.bridge.names) - 1)
            self.assertNotIn(v3_mcp.tool_name(victim), trimmed.names)
            ok, report = coverage_probe(self.proxy.catalog, trimmed)
            self.assertFalse(ok, "删掉一条路由后，探针必须报红")
            # 旧目录（未删的装配）仍有那一件、新桥没有 → 差集落在 orphan 方向（目录多了一件）
            self.assertEqual(report["orphan"], [v3_mcp.tool_name(victim)])
            self.assertEqual(report["missing"], [])
            # 旧目录（未删的装配）仍然有那一件 → 差集方向正确，不是恒空
            self.assertIn(v3_mcp.tool_name(victim), self.proxy.catalog)
            # 真删也反映在「代理可达集合」上：用新桥重建目录 → 少一件、且代理找不到它
            trimmed_catalog = mcp_discovery.build_catalog(mcp_tools.TOOLS, trimmed)
            self.assertEqual(set(self.proxy.catalog) - set(trimmed_catalog),
                             {v3_mcp.tool_name(victim)})
        finally:
            self.app.routes.extend(saved)

    def test_really_removing_a_tool_from_the_catalog_is_detected(self):
        """真删：目录里去掉一件 → 可达集合不再等于 direct 面；同一份代理也真找不到它。"""
        direct_names = set(registry_names(build_offline_app(mcp_discovery.DIRECT).state.mcp))
        trimmed = dict(self.proxy.catalog)
        victim = "v3_risk"
        self.assertIn(victim, trimmed)
        trimmed.pop(victim)
        self.assertNotEqual(set(trimmed), direct_names)
        self.assertEqual(direct_names - set(trimmed), {victim})
        # 代理确认真找不到它了（不是只在集合运算里少了一件）
        proxy = mcp_discovery.DiscoveryProxy(list(self.proxy.base_tools.values()),
                                            self.proxy.bridge)
        proxy.catalog = trimmed

        async def runner():
            return await proxy.call(victim, {})

        result, card, target = asyncio.run(runner())
        self.assertIsNone(card)
        self.assertIsNone(target)
        self.assertEqual(envelope(result)["error"]["code"], "mcp/unknown-tool")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
