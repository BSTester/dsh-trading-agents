"""WP6 补遗任务 D 协议冒烟（规格 §5.2 协议层 S1–S4；loopback 集成）。

起**真实** uvicorn 线程（``TRADING_SERVICE_PORT=0`` + 临时 ``DSH_HOME``），用官方 ``mcp``
Python 客户端的 streamable-http 传输连 ``/mcp``，逐条钉死：

  * S1 —— initialize → tools/list 的**表面**，两种模式各自精确且期望值全程推导：
    **direct** ≡ 工作台基础 77 件（``mcp_tools.TOOLS``，逐名 + 逐件 inputSchema
    字段集/必填集 ≡ 规格清单，additionalProperties:false，一字未变）++ **V3 桥接面**
    （``/api/v3/*`` 路由经 ``v3_mcp.register`` 桥接，逐名 ≡ ``tool_name(route)``、条数 =
    路由条数——**不写死**，路由正在演进）+ **discovery**（默认）≡ 4 件直连保留 ++
    ``list_tools`` / ``call_tool``（代理可达集合 ≡ direct 面）；
    工具面**不含** ``confirm_decide``（人工批准通道，规格 §5.1 A7），含只读的 ``confirmation``；
    另有取值域断言：``series.limit`` 的 ``20..2000`` 与 ``series.period`` 的六值枚举
    （可选字段落在 ``anyOf`` 的基类型分支上，见 ``non_null_branch``），基础面与桥接面
    逐件同样校验；
  * S2 —— call snapshot → ok；call switch_mode(live, confirmation=「确认实盘」) →
    ``trading/live-switch-web-only``，随后 store 模式仍 sim、模式文件未被创建；
  * S3 —— call plan_execute(plan_hash="nope") → queued+nonce（校验在 daemon），指令文件落盘；
    HTTP ``POST /api/wb/snapshot`` 与 MCP ``snapshot`` 稳定字段同值；
  * S4 —— HTTP 未声明端点 → 404；MCP 未知名 → 客户端报错（不触达 handle）。

另加一条超出 S1–S4 的线格式用例：程序异常（数据文件损坏）经 MCP 回 isError=true +
``trading/tool-failed``，业务失败回 isError=false + ``trading/*`` 信封（规格 §3.2 错误语义）；
以及 ``/mcp`` 沿用 HTTP 的 token 中间件（配了 token 就 401，``/mcp/`` 子路径前缀同样受保护，
healthz 仍豁免）。

SDK 适配（mcp 2.2.0 实测）：客户端传输是 ``mcp.client.streamable_http.streamable_http_client``
（2.x 由 ``streamablehttp_client`` 更名），yield **两元组** ``(read, write)``；``ClientSession``
的 ``initialize`` / ``list_tools`` / ``call_tool`` 都是协程；``CallToolResult`` 的字段名是
``is_error``（线格式别名才是 ``isError``）。
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402

from server import app as app_module  # noqa: E402
from server import caches, mcp_discovery, mcp_tools, run, store_access, v3_mcp  # noqa: E402
from server.config import load_config  # noqa: E402

START_TIMEOUT = 20
STABLE_SNAPSHOT_FIELDS = ("mode", "version", "endpoints", "in_flight", "notice")
NULL_BRANCH = {"type": "null"}

# ---------------------------------------------------------------------------
# 仓库级锁定契约：MCP 面 = 工作台基础工具面 + V3 桥接面（2026-09-20 起）
# **两种表面模式各自的期望值都由同一推导式给出**（规格 FR-TOOLS-003 / §10 决策 3）
# ---------------------------------------------------------------------------
# ``V3_BRIDGE_TOOLS`` **不写死**：桥接面 = ``/api/v3/*`` 路由表的一次遍历
# （``v3_mcp.V3Bridge.definitions``），路由数正在演进（v3_sdk / v3_headless / v3_alerts
# 在并行落地），写死任何一个数字都会把兄弟 agent 的成果判红。因此：
#   * direct    —— ``tools/list`` ≡ 基础 77 件 ++ ``bridge.names``，**顺序也锁定**；
#   * discovery —— ``tools/list`` ≡ 直连保留子集 ++ ``list_tools``/``call_tool``；
#   * 桥接面 ⇄ 路由表 bijection 由 ``test_s1_initialize_and_tool_surface`` 逐名比对；
#   * 只读数也**按路由里声明的只读集合推导**（``v3_mcp.NON_READONLY_PATHS``），不写死。
# ``mcp_tools.TOOL_COUNT`` **仍是基础注册表的 77**，不是 MCP 面总数。
BASE_TOOLS = 77           # 工作台基础工具面（mcp_tools.TOOLS；逐名 + 逐件 schema 锁定）
DISCOVERY_KEEP = list(mcp_discovery.DIRECT_KEEP)          # 4 件直连保留
DISCOVERY_PROXY = list(mcp_discovery.PROXY_NAMES)         # list_tools / call_tool
DISCOVERY_SURFACE = len(DISCOVERY_KEEP) + len(DISCOVERY_PROXY)   # 6（与路由数无关）
#: 桥接面里**不**标只读的写类工具（逐名钉死，防「悄悄把写类标成只读」；条数不写死——
#: 路由在演进，断言的是「这个集合 ≡ NON_READONLY_PATHS 的映射」）。
V3_NON_READONLY = frozenset({"v3_strategy_run", "v3_oms_sync", "v3_credentials",
                             "v3_sdk_prompt"})
V3_PREFIX = "v3_"
V3_ROUTE_PREFIX = "/api/v3/"


def v3_route_paths(app):
    """``/api/v3/*`` 路由表（去重排序）——桥接面的唯一事实来源，两种模式的期望值都从它推。"""
    return sorted({route.path for route in app.routes
                   if getattr(route, "path", "").startswith(V3_ROUTE_PREFIX)})


def declared_readonly_bridge_names(app):
    """按路由声明推导出的桥接只读工具名集合（``NON_READONLY_PATHS`` 是唯一口径）。"""
    return {v3_mcp.tool_name(path) for path in v3_route_paths(app)
            if path not in v3_mcp.NON_READONLY_PATHS}


def published_fields(definition):
    """发布 ``inputSchema`` 的字段集 ≡ 定义字段集 ∪（renderable 件的 ``format``）。

    ``format`` 是 FR-TOOLS-002 子规范②在**绑定期**追加到签名末尾的唯一动态字段
    （``mcp_tools._bind`` 与 ``v3_mcp._bind`` 同形），**不在** ``ToolDefinition.fields``
    里——后者是工具清单字段集（与规格 §3.2/§3.4 表逐项对平的那一份，见
    ``test_wp6_service_approval.test_input_fields_match_the_spec_table``），发布面因此
    比清单多且**仅多**这一个字段。两处由同一个谓词决定（``mcp_tools.is_renderable`` →
    唯一注册表 ``TOOL_RENDERERS``），所以这里也用它推导：写死「哪些工具可渲染」的名单
    等于把注册表抄成第二份清单。断言仍是**集合相等**（多一件、少一件都红），不放宽。
    """
    fields = set(definition.fields)
    if mcp_tools.is_renderable(definition.name):
        fields.add(mcp_tools.FORMAT_FIELD)
    return fields


def non_null_branch(schema):
    """取字段 schema 的「基类型分支」：可选字段是 ``anyOf: [基类型, null]``（见 S1 增补说明）。

    必填字段（或将来被拍平的单分支 schema）没有 ``anyOf``，此时原样返回。断言 null 分支存在由
    调用方按需显式做（S1 增补对 ``limit``/``period`` 各断言一次）。
    """
    branches = schema.get("anyOf")
    if branches is None:
        return schema
    non_null = [branch for branch in branches if branch != NULL_BRANCH]
    assert len(non_null) == 1, f"可选取值域应恰有一个基类型分支：{branches}"
    return non_null[0]


class IdleScheduler:
    """WP7 调度器替身：start/stop 空转，healthz 恒报空闲。

    本文件起**真实** uvicorn（lifespan 必跑），不注入就会启动真调度线程——tick 会连
    SQLite/写心跳；注入替身后用例保持与 WP6 时期同一份 I/O 面。
    """

    def __init__(self):
        self.calls = []
        self.alive = False
        self.last_error = None

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


class McpHarness(unittest.TestCase):
    """真实服务进程 + 官方客户端的公共夹具（两种表面模式各起一个实例）。

    子类只声明 ``surface``（``direct`` / ``discovery``），其余共用：同一份临时 home、同一套
    就绪等待与清理。每个类各自起一个**独立进程**（真 uvicorn + 真 lifespan），因此两种模式的
    断言互不污染，测的也是运维真正会跑的那条路径。
    """

    surface = mcp_discovery.DIRECT

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.home = Path(cls._tmp.name) / "home"
        cls.home.mkdir(parents=True, exist_ok=True)
        cls._env = {key: os.environ.get(key) for key in ("DSH_HOME", "TRADING_SERVICE_PORT")}
        os.environ["DSH_HOME"] = str(cls.home)
        os.environ["TRADING_SERVICE_PORT"] = "0"
        # 缓存目录隔离：caches 默认写 $DSH_HOME/trading-workbench-cache，不隔离会把用例数据
        # 写进真实用户缓存（与 tests/test_wp6_service.py 同一手法）。
        caches.configure(home=str(cls.home))
        cls.app = app_module.create_app(home=str(cls.home),
                                        dist=str(Path(cls._tmp.name) / "dist-missing"),
                                        scheduler=IdleScheduler(),
                                        mcp_surface=cls.surface)
        # 就绪行同款装配：配置里的 port=0 由内核分配，避免与真实服务撞端口。
        cls.server = run.build_server(cls.app, load_config(str(cls.home)))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.monotonic() + START_TIMEOUT
        while not cls.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not cls.server.started:
            raise AssertionError(f"uvicorn 未能在 {START_TIMEOUT}s 内监听")
        cls.port = cls.server.servers[0].sockets[0].getsockname()[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.url = f"{cls.base}/mcp"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=15)
        stopped = not cls.thread.is_alive()
        caches.configure()  # 还原模块级缓存（home 延后解析 DSH_HOME，不钉死已删的临时目录）
        for key, value in cls._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()
        if not stopped:
            raise AssertionError("uvicorn 线程未在 15s 内退出（端口/进程未清理干净）")

    # ---- 客户端助手：每个用例一个真实会话（与 dsh-mcp-client 的会话模型一致）----

    def with_session(self, action):
        async def runner():
            async with streamable_http_client(self.url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await action(session)

        return asyncio.run(runner())

    def call(self, name, arguments):
        return self.with_session(lambda session: session.call_tool(name, arguments))

    def envelope(self, result):
        body = mcp_tools.result_payload(result)
        self.assertFalse(result.is_error, body)
        return body

    def http_snapshot(self):
        response = httpx.post(f"{self.base}/api/wb/snapshot", json={}, timeout=30)
        self.assertEqual(response.status_code, 200)
        return response.json()["value"]

    def listing(self):
        """``tools/list`` 的线格式工具表（一次真实会话）。"""
        async def runner():
            async with streamable_http_client(self.url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.list_tools()

        return asyncio.run(runner())

    def proxy_call(self, name, arguments=None):
        """经 ``call_tool`` 代理调用（discovery 模式的唯一转发入口）。"""
        return self.call("call_tool", {"name": name, "arguments": arguments or {}})


class McpProtocolSmoke(McpHarness):
    """S1–S4（direct 面）+ discovery 面的对应断言。

    一个真实服务进程 + 官方客户端（类级共享，方法间共享同一份临时 home）。
    """

    surface = mcp_discovery.DIRECT

    # ---- S1 ----

    def exposed_call(self, name, arguments):
        """在**当前表面模式**下调用一件工具：直连件直接调，其余经 ``call_tool`` 代理。

        两种模式各跑一遍同一个语义用例：direct 走 ``tools/call`` 直连；discovery 走
        ``call_tool`` 转发（同一份实现、同一信封）。这样「模式切换不改变行为」是被测出来的，
        而不是靠人肉对照。
        """
        tools = {tool.name for tool in self.listing().tools}
        if name in tools:
            return self.call(name, arguments)
        return self.proxy_call(name, arguments)

    def test_s1_initialize_and_tool_surface(self):
        """initialize → ``tools/list`` 的**表面**：两种模式各自精确、且期望值全程推导。

        * 桥接面 = ``/api/v3/*`` 路由表的一次遍历：``bridge_names == {tool_name(p)}``，
          逐名与路由表比对（路由增减时两边一起动，不写死任何条数）；
        * ``direct``    —— ``tools/list`` ≡ 基础 77 件 ++ 桥接件，**顺序也锁定**；
        * ``discovery`` —— ``tools/list`` ≡ 4 件直连保留 ++ ``list_tools``/``call_tool``；
        * 共同不变式：不含 ``confirm_decide``（人工批准通道）、名字唯一、自己那份面里的每件
          工具 schema 封闭且字段集/必填集与 ``ToolDefinition`` 逐项相等。
        """
        listing = self.listing()
        names = [tool.name for tool in listing.tools]
        init = asyncio.run(self._initialize_only())
        self.assertEqual(init.server_info.name, mcp_tools.SERVER_NAME)

        # 基础注册表口径（TOOL_COUNT == 基础面，不是 MCP 面总数）
        self.assertEqual(mcp_tools.TOOL_COUNT, BASE_TOOLS)
        base_names = [definition.name for definition in mcp_tools.TOOLS]
        self.assertEqual(len(base_names), BASE_TOOLS)

        # 桥接面 ⇄ /api/v3/* 路由表一一对应（两条模式的期望值都从这一对推导）
        bridge = self.app.state.v3_mcp_bridge
        routes = v3_route_paths(self.app)
        bridge_names = list(bridge.names)
        self.assertEqual(sorted(bridge_names), sorted(v3_mcp.tool_name(path) for path in routes))
        self.assertEqual(sorted(bridge.paths), routes)
        self.assertEqual(len(bridge.definitions), len(bridge.paths))
        self.assertTrue(all(name.startswith(V3_PREFIX) for name in bridge_names))
        direct_names = base_names + bridge_names                       # direct 的精确集合
        discovery_names = DISCOVERY_KEEP + DISCOVERY_PROXY             # discovery 的精确集合

        if self.surface == mcp_discovery.DIRECT:
            self.assertEqual(len(names), BASE_TOOLS + len(routes))
            self.assertEqual(names, direct_names)                      # 顺序也锁定
            # 只读标注：按**路由声明的只读集合**推导（不写死 36/38）
            readonly = {tool.name for tool in listing.tools
                        if tool.name.startswith(V3_PREFIX)
                        and getattr(tool.annotations, "read_only_hint", None) is True}
            self.assertEqual(readonly, declared_readonly_bridge_names(self.app))
            self.assertEqual(set(bridge_names) - readonly,
                             {v3_mcp.tool_name(path) for path in v3_mcp.NON_READONLY_PATHS})
            self.assertEqual(set(V3_NON_READONLY),
                             {v3_mcp.tool_name(path) for path in v3_mcp.NON_READONLY_PATHS})
        else:
            self.assertEqual(len(names), DISCOVERY_SURFACE)
            self.assertEqual(names, discovery_names)
            self.assertEqual(self.app.state.mcp_surface, mcp_discovery.DISCOVERY)
            # 代理可达集合 ≡ direct 面（发现代理没丢能力）——推导式，不写死条数
            self.assertEqual(set(self.app.state.mcp_discovery.catalog),
                             set(base_names) | set(bridge_names))
            self.assertNotIn("confirm_decide", names)

        self.assertEqual(len(set(names)), len(names), "工具名必须唯一")
        # 不变式 1：唯一能批准实盘操作的通道绝不进工具面（两种写法都不允许出现）
        self.assertNotIn("confirm_decide", names)
        self.assertNotIn("confirm-decide", names)

        # 逐件封闭：面里的每件工具都对**同一份定义**断言
        # additionalProperties:false + 字段集/必填集（基础层对 TOOLS、桥接层对 definitions、
        # 两个代理入口对 ``mcp_discovery`` 自己的签名——三份都是装配期同一份来源）。
        # 字段集的期望值 = ``published_fields``（定义字段 ∪ renderable 的 ``format``），
        # 与 ``mcp_discovery`` 卡片补列 ``format`` 用的是同一个 ``is_renderable`` 谓词。
        definitions = {definition.name: definition for definition in mcp_tools.TOOLS}
        definitions.update((definition.name, definition) for definition in bridge.definitions)
        for proxy_name in DISCOVERY_PROXY:
            definitions.setdefault(proxy_name, self._proxy_definition(proxy_name))
        for tool in listing.tools:
            definition = definitions[tool.name]
            schema = tool.input_schema
            self.assertEqual(set(schema["properties"]), published_fields(definition), tool.name)
            self.assertEqual({param.name for param in definition.params if param.required},
                             set(schema.get("required", [])), tool.name)
            self.assertIs(schema.get("additionalProperties"), False, tool.name)
            self.assertFalse(mcp_tools.is_blacklisted(tool.name), tool.name)

    async def _initialize_only(self):
        async with streamable_http_client(self.url) as (read, write):
            async with ClientSession(read, write) as session:
                return await session.initialize()

    def _proxy_definition(self, name):
        """两个代理入口的签名（与 ``mcp_discovery`` 定义同源，不手抄一份字段表）。"""
        params = (mcp_discovery.list_tools_signature() if name == mcp_discovery.LIST_TOOL
                  else mcp_discovery.call_tool_signature())
        return mcp_tools.ToolDefinition(name, "", None, tuple(params))

    def test_s1_confirmation_tool_is_reachable_and_read_only(self):
        """``confirmation`` 读工具：无待确认时 pending=null；两种模式下都可达。

        discovery 模式下它不在直连面里，但**经 ``call_tool`` 转发依然可达**——这正是
        「省的是 schema，不是能力」的断言。
        """
        if self.surface == mcp_discovery.DISCOVERY:
            self.assertNotIn("confirmation", [tool.name for tool in self.listing().tools])
        body = self.envelope(self.exposed_call("confirmation", {}))
        self.assertTrue(body["ok"], body)
        self.assertIsNone(body["value"]["pending"])
        self.assertEqual(body["value"]["ttl_ms"], store_access.CONFIRM_TTL_MS)
        self.assertEqual(store_access.confirmation_view(self.home), None)

    def test_s1_series_carries_its_value_domain(self):
        """S1 增补：取值域随注解发布——``limit`` 区间与 ``period`` 枚举逐值可见。

        结构说明：可选字段的注解是 ``base | None``（``Param.annotation()``），pydantic 因此
        把基类型**连同约束**放进 ``anyOf[0]``，``anyOf[1]`` 是 ``{"type": "null"}`` 分支；
        这里按实际结构取「非 null 分支」断言，不假设它被拍平成顶层 ``minimum``/``enum``。

        discovery 模式下 ``series`` 不在直连面（故无发布 schema 可查），改为断言两件事：
        ① 代理检索能回答「必填什么」（卡片 required/optional）；② 经代理转发**真的能调到**
        同一实现并拿到真实信封——「schema 便宜了，能力没便宜」。
        """
        bridge = self.app.state.v3_mcp_bridge
        if self.surface == mcp_discovery.DIRECT:
            listing = self.listing()
            series = next(tool for tool in listing.tools if tool.name == "series")
            properties = series.input_schema["properties"]

            limit = non_null_branch(properties["limit"])
            self.assertEqual(limit["minimum"], 20)
            self.assertEqual(limit["maximum"], 2000)
            self.assertEqual(limit["type"], "integer")
            self.assertEqual(properties["limit"]["anyOf"][1], NULL_BRANCH)

            period = non_null_branch(properties["period"])
            self.assertEqual(period["enum"], ["1m", "5m", "15m", "30m", "60m", "1d"])
            self.assertEqual(period["type"], "string")
            self.assertEqual(properties["period"]["anyOf"][1], NULL_BRANCH)

            # 取值域与清单同源：任何带 minimum/maximum 的字段都必须逐值出现在发布 schema 上
            # （当前只有 series.limit 带区间；加了新区间字段而没落到 schema 时这里立刻红）。
            # 基础层与桥接层分别对各自的 ToolDefinition 校验（两份都是装配期同一份清单）。
            layers = (
                ({definition.name: definition for definition in mcp_tools.TOOLS},
                 [tool for tool in listing.tools if not tool.name.startswith(V3_PREFIX)]),
                ({definition.name: definition for definition in bridge.definitions},
                 [tool for tool in listing.tools if tool.name.startswith(V3_PREFIX)]),
            )
            checked = 0
            for definitions, surface in layers:
                for tool in surface:
                    for param in definitions[tool.name].params:
                        branch = non_null_branch(tool.input_schema["properties"][param.name])
                        if param.minimum is not None:
                            self.assertEqual(branch.get("minimum"), param.minimum, tool.name)
                        if param.maximum is not None:
                            self.assertEqual(branch.get("maximum"), param.maximum, tool.name)
                        checked += 1
            self.assertGreater(checked, 0)
            return

        # discovery：检索面
        self.assertNotIn("series", [tool.name for tool in self.listing().tools])
        page = self.envelope(self.call("list_tools", {"keyword": "series"}))
        card = next(item for item in page["cards"] if item["name"] == "series")
        self.assertEqual(card["required"], ["ticker"])
        self.assertIn("limit", card["optional"])
        self.assertIn("period", card["optional"])
        # 转发面：真实调到同一实现（信封带 ok/source，正是 direct 面同一份 handler 产物）
        forwarded = self.envelope(self.exposed_call("series", {"ticker": "SH.600519"}))
        self.assertIn("ok", forwarded)

    # ---- S2 ----

    def test_s2_live_switch_is_refused_on_mcp_channel(self):
        """通道分级：MCP 的 switch_mode 无论口令一律拒，模式不变、模式文件不落地。

        discovery 模式下 ``switch_mode`` 经 ``call_tool`` 转发——封死分支在桥/工具实现内部，
        因此代理路径上同样拒（这正是「代理不绕过约束」的线级证据）。
        """
        snapshot = self.envelope(self.exposed_call("snapshot", {}))
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["value"]["mode"], "sim")
        for confirmation in ("确认实盘", None, "错误口令"):
            arguments = {"mode": "live", "expected_mode": "sim"}
            if confirmation is not None:
                arguments["confirmation"] = confirmation
            result = self.exposed_call("switch_mode", arguments)
            body = mcp_tools.result_payload(result)
            self.assertFalse(result.is_error, body)  # 业务失败不是协议错误
            self.assertFalse(body["ok"], body)
            self.assertEqual(body["error"]["code"], "trading/live-switch-web-only")
            self.assertEqual(body["error"]["message"], mcp_tools.LIVE_SWITCH_MESSAGE)
            self.assertEqual(body["error"]["details"], {})
        self.assertEqual(store_access.read_mode(self.home), "sim")
        self.assertFalse((self.home / "trading-account-mode").exists(),
                         "被拒的 live 切换不得触碰 store")

    # ---- S3 ----

    def test_s3_plan_execute_queues_and_http_matches_mcp(self):
        """排队成功（校验在 daemon）；HTTP 与 MCP 的快照稳定字段同值。"""
        body = self.envelope(self.exposed_call("plan_execute",
                                               {"plan_hash": "nope",
                                                "expected_mode": "sim",
                                                "confirmation": "确认执行"}))
        self.assertTrue(body["ok"], body)
        self.assertIs(body["value"]["queued"], True)
        self.assertTrue(body["value"]["nonce"])
        self.assertEqual(body["value"]["action"], "execute")
        pending = sorted((self.home / "trading-commands" / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        saved = json.loads(pending[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["type"], "execute_plan")
        self.assertEqual(saved["plan_hash"], "nope")
        self.assertEqual(saved["expected_mode"], "sim")
        self.assertNotIn("confirmation", saved)

        mcp_value = self.envelope(self.exposed_call("snapshot", {}))["value"]
        http_value = self.http_snapshot()
        for field in STABLE_SNAPSHOT_FIELDS:
            self.assertEqual(http_value[field], mcp_value[field], field)

    # ---- S4 ----

    def test_s4_unknown_surfaces_are_closed(self):
        """HTTP 未声明端点 → 404 信封；MCP 未知名 → 错误结果（不触达实现）。

        direct：SDK 层报「未知工具」（isError=true + 点名该工具）；
        discovery：代理层报 ``mcp/unknown-tool``（业务失败，isError=false）+ 「可用 list_tools
        检索」提示——**绝不静默返回空**。
        """
        response = httpx.post(f"{self.base}/api/wb/not-an-endpoint", json={}, timeout=30)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "trading/unknown-endpoint")

        if self.surface == mcp_discovery.DIRECT:
            # SDK 把「未知工具」作为工具级错误结果回给客户端（不是协议层 -32601）；
            # 关键断言是它确实报错，且错误信息点名工具——分发函数不可能被调用。
            result = self.call("not_a_tool", {})
            self.assertTrue(result.is_error, result)
            self.assertIn("not_a_tool", result.content[0].text)
            return
        result = self.proxy_call("not_a_tool", {})
        body = mcp_tools.result_payload(result)
        self.assertFalse(result.is_error, body)
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["error"]["code"], "mcp/unknown-tool")
        self.assertIn("not_a_tool", body["error"]["message"])
        self.assertIn("list_tools", body["error"]["message"])

    def test_failure_semantics_over_the_wire(self):
        """业务失败 isError=false + envelope；程序异常 isError=true + trading/tool-failed。

        discovery 模式下走 ``call_tool`` 转发：程序异常同样在**同一实现内部**被包成
        ``trading/tool-failed`` + isError=true（代理不吞、不改写）。
        """
        broken = self.home / "trading-workbench.json"
        try:
            broken.write_text("{oops", encoding="utf-8")
            result = self.exposed_call("snapshot", {})
            body = mcp_tools.result_payload(result)
            self.assertTrue(result.is_error, body)
            self.assertEqual(body["error"]["code"], "trading/tool-failed")
            self.assertLessEqual(len(body["error"]["message"]), 300)
        finally:
            broken.unlink()


class McpDiscoveryProtocolSmoke(McpProtocolSmoke):
    """``QUANT_MCP_SURFACE=discovery``（**默认**）下的同一套 S1–S4 + 发现代理断言。

    继承 direct 的用例保证两种模式跑的是同一套语义；本类再加 discovery 独有的断言：
    表面精确集合（4 + 2，不放宽成 >=）、代理可达集合 ≡ direct 面、检索真有用。
    """

    surface = mcp_discovery.DISCOVERY

    def test_discovery_surface_is_exactly_keep_plus_proxies(self):
        names = [tool.name for tool in self.listing().tools]
        self.assertEqual(len(names), DISCOVERY_SURFACE)
        self.assertEqual(names, DISCOVERY_KEEP + DISCOVERY_PROXY)
        self.assertEqual(self.app.state.mcp_surface, mcp_discovery.DISCOVERY)

    def test_discovery_keeps_every_capability_reachable(self):
        """发现代理没丢能力：代理目录 ≡ 「基础 77 件 + 全部桥接件」，逐名相等（推导式）。"""
        catalog = set(self.app.state.mcp_discovery.catalog)
        expected = ({definition.name for definition in mcp_tools.TOOLS}
                    | set(self.app.state.v3_mcp_bridge.names))
        self.assertEqual(catalog, expected)
        self.assertEqual(len(catalog),
                         BASE_TOOLS + len(v3_route_paths(self.app)))

    def test_list_tools_entry_answers_the_three_questions(self):
        """``list_tools`` 真有用：能回答「有没有这个能力 / 名字是什么 / 必填什么」。"""
        expected_total = BASE_TOOLS + len(v3_route_paths(self.app))
        page = self.envelope(self.call("list_tools", {}))
        self.assertTrue(page["ok"], page)
        self.assertEqual(page["catalog_total"], expected_total)
        self.assertEqual(page["count"], mcp_discovery.DEFAULT_PAGE)
        self.assertTrue(page["has_more"])
        for card in page["cards"]:
            self.assertTrue(card["name"], card)
            self.assertTrue(card["purpose"], card)
            self.assertIsInstance(card["required"], list)
            self.assertNotIn("inputSchema", card, "卡片不得带完整 schema")

        by_domain = self.envelope(self.call("list_tools", {"domain": "ml"}))
        self.assertEqual({card["name"] for card in by_domain["cards"]},
                         {"v3_ml_backtest", "v3_ml_models", "v3_ml_sweep"})
        by_prefix = self.envelope(self.call("list_tools", {"prefix": "v3_ml"}))
        self.assertEqual({card["name"] for card in by_prefix["cards"]},
                         {"v3_ml_backtest", "v3_ml_models", "v3_ml_sweep"})
        empty = self.envelope(self.call("list_tools", {"keyword": "绝不存在的能力zzz"}))
        self.assertEqual(empty["total"], 0)
        self.assertIn("hint", empty)

    def test_call_tool_rejects_unknown_name_and_bad_arguments(self):
        unknown = mcp_tools.result_payload(self.proxy_call("no_such_tool", {}))
        self.assertEqual(unknown["error"]["code"], "mcp/unknown-tool")
        bad = mcp_tools.result_payload(self.proxy_call("v3_ml_backtest", {}))
        self.assertEqual(bad["error"]["code"], "mcp/bad-arguments")
        self.assertIn("ticker", bad["error"]["message"])


class McpTokenGuardTests(unittest.TestCase):
    """``/mcp`` 沿用 HTTP 的 token 中间件：配了 token 就 401，healthz 仍豁免。

    TestClient 的 Host 必须是 ``127.0.0.1:<port>``：SDK 默认开启 DNS rebinding 保护，
    allowed_hosts 是带端口的模式，默认的 ``testserver`` 会被 421 挡掉（那会让断言看不出
    「401 来自我们的中间件」）。
    """

    def test_token_guard_covers_mcp_and_spares_healthz(self):
        with tempfile.TemporaryDirectory() as tmp:
            caches.configure(home=tmp)
            self.addCleanup(caches.configure)
            app = app_module.create_app(home=tmp, dist=os.path.join(tmp, "dist-missing"),
                                        config={"port": 0, "host": "127.0.0.1",
                                                "token": "s3cret"},
                                        scheduler=IdleScheduler())
            head = {"Accept": "application/json, text/event-stream"}
            with TestClient(app, base_url="http://127.0.0.1:8397") as client:
                denied = client.post("/mcp", json={}, headers=head)
                self.assertEqual(denied.status_code, 401)
                self.assertEqual(denied.json()["error"]["code"], "trading/unauthorized")
                allowed = client.post("/mcp", json={},
                                      headers={**head, "Authorization": "Bearer s3cret"})
                self.assertNotEqual(allowed.status_code, 401)
                self.assertEqual(client.get("/healthz").status_code, 200)

    def test_token_guard_covers_mcp_subpaths(self):
        """``/mcp`` 的判定是「等值或前缀」：将来 SDK 挂到 ``/mcp/<sub>`` 也不会有未鉴权旁路。

        实现说明：鉴权在中间件里、**先于路由**发生，所以无 token 的子路径请求必然是 401
        （不是 404）——正是这条保证让它成为「防旁路」而不是「防未知路由」。带 token 时鉴权放行，
        由路由决定去向：当前 SDK 只在裸 ``/mcp`` 注册路由，``/mcp/anything`` 因此落到静态面的
        非 GET 兜底 → 405「仅 GET」（这里只断言「不是 401」，不把兜底状态码钉死）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            caches.configure(home=tmp)
            self.addCleanup(caches.configure)
            app = app_module.create_app(home=tmp, dist=os.path.join(tmp, "dist-missing"),
                                        config={"port": 0, "host": "127.0.0.1",
                                                "token": "s3cret"},
                                        scheduler=IdleScheduler())
            head = {"Accept": "application/json, text/event-stream"}
            with TestClient(app, base_url="http://127.0.0.1:8397") as client:
                for path in ("/mcp/anything", "/mcp/", "/mcp/session/1"):
                    with self.subTest(path=path):
                        denied = client.post(path, json={}, headers=head)
                        self.assertEqual(denied.status_code, 401)
                        self.assertEqual(denied.json()["error"]["code"], "trading/unauthorized")
                        allowed = client.post(path, json={},
                                              headers={**head, "Authorization": "Bearer s3cret"})
                        self.assertNotEqual(allowed.status_code, 401)
                self.assertEqual(client.get("/healthz").status_code, 200)


if __name__ == "__main__":
    unittest.main()
