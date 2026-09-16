"""WP6 补遗任务 D 协议冒烟（规格 §5.2 协议层 S1–S4；loopback 集成）。

起**真实** uvicorn 线程（``TRADING_SERVICE_PORT=0`` + 临时 ``DSH_HOME``），用官方 ``mcp``
Python 客户端的 streamable-http 传输连 ``/mcp``，逐条钉死：

  * S1 —— initialize → tools/list 恰 41（WP8 富途直通起）、名单与 ``mcp_tools.TOOLS`` 一致，且每个工具的
    inputSchema 字段集/必填集与规格清单逐项一致（additionalProperties:false）；工具面
    **不含** ``confirm_decide``（人工批准通道，规格 §5.1 A7），含只读的 ``confirmation``；
    另有取值域断言：``series.limit`` 的 ``20..2000`` 与 ``series.period`` 的六值枚举
    （可选字段落在 ``anyOf`` 的基类型分支上，见 ``non_null_branch``）；
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
from server import caches, mcp_tools, run, store_access  # noqa: E402
from server.config import load_config  # noqa: E402

START_TIMEOUT = 20
STABLE_SNAPSHOT_FIELDS = ("mode", "version", "endpoints", "in_flight", "notice")
NULL_BRANCH = {"type": "null"}


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


class McpProtocolSmoke(unittest.TestCase):
    """S1–S4：一个真实服务进程 + 官方客户端（类级共享，方法间共享同一份临时 home）。"""

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
                                        scheduler=IdleScheduler())
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

    # ---- S1 ----

    def test_s1_initialize_and_tool_surface(self):
        """initialize → tools/list 恰 41；名单与输入字段集逐个对齐规格清单（WP7/WP8 增量）。"""
        async def runner():
            async with streamable_http_client(self.url) as (read, write):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    listing = await session.list_tools()
                    return init, listing

        init, listing = asyncio.run(runner())
        self.assertEqual(init.server_info.name, mcp_tools.SERVER_NAME)
        names = [tool.name for tool in listing.tools]
        self.assertEqual(len(names), 41)
        self.assertEqual(len(names), mcp_tools.TOOL_COUNT)
        self.assertEqual(names, [definition.name for definition in mcp_tools.TOOLS])
        # 不变式 1：唯一能批准实盘操作的通道绝不进工具面（两种写法都不允许出现）
        self.assertNotIn("confirm_decide", names)
        self.assertNotIn("confirm-decide", names)
        self.assertIn("confirmation", names)
        definitions = {definition.name: definition for definition in mcp_tools.TOOLS}
        for tool in listing.tools:
            definition = definitions[tool.name]
            schema = tool.input_schema
            self.assertEqual(set(schema["properties"]), set(definition.fields), tool.name)
            self.assertEqual({param.name for param in definition.params if param.required},
                             set(schema.get("required", [])), tool.name)
            self.assertIs(schema.get("additionalProperties"), False, tool.name)
            self.assertFalse(mcp_tools.is_blacklisted(tool.name), tool.name)

    def test_s1_confirmation_tool_is_read_only(self):
        """``confirmation`` 读工具：无待确认时 pending=null，且它是只读的（不触达批准）。"""
        body = self.envelope(self.call("confirmation", {}))
        self.assertTrue(body["ok"], body)
        self.assertIsNone(body["value"]["pending"])
        self.assertEqual(body["value"]["ttl_ms"], store_access.CONFIRM_TTL_MS)
        self.assertEqual(store_access.confirmation_view(self.home), None)

    def test_s1_series_carries_its_value_domain(self):
        """S1 增补：取值域也随注解发布——``limit`` 区间与 ``period`` 枚举逐值可见。

        结构说明：可选字段的注解是 ``base | None``（``Param.annotation()``），pydantic 因此
        把基类型**连同约束**放进 ``anyOf[0]``，``anyOf[1]`` 是 ``{"type": "null"}`` 分支；
        这里按实际结构取「非 null 分支」断言，不假设它被拍平成顶层 ``minimum``/``enum``。
        """
        async def runner():
            async with streamable_http_client(self.url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.list_tools()

        listing = asyncio.run(runner())
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
        definitions = {definition.name: definition for definition in mcp_tools.TOOLS}
        for tool in listing.tools:
            for param in definitions[tool.name].params:
                branch = non_null_branch(tool.input_schema["properties"][param.name])
                if param.minimum is not None:
                    self.assertEqual(branch.get("minimum"), param.minimum, tool.name)
                if param.maximum is not None:
                    self.assertEqual(branch.get("maximum"), param.maximum, tool.name)

    # ---- S2 ----

    def test_s2_live_switch_is_refused_on_mcp_channel(self):
        """通道分级：MCP 的 switch_mode 无论口令一律拒，模式不变、模式文件不落地。"""
        snapshot = self.envelope(self.call("snapshot", {}))
        self.assertTrue(snapshot["ok"])
        self.assertEqual(snapshot["value"]["mode"], "sim")
        for confirmation in ("确认实盘", None, "错误口令"):
            arguments = {"mode": "live", "expected_mode": "sim"}
            if confirmation is not None:
                arguments["confirmation"] = confirmation
            result = self.call("switch_mode", arguments)
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
        body = self.envelope(self.call("plan_execute", {"plan_hash": "nope",
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

        mcp_value = self.envelope(self.call("snapshot", {}))["value"]
        http_value = self.http_snapshot()
        for field in STABLE_SNAPSHOT_FIELDS:
            self.assertEqual(http_value[field], mcp_value[field], field)

    # ---- S4 ----

    def test_s4_unknown_surfaces_are_closed(self):
        """HTTP 未声明端点 → 404 信封；MCP 未知名 → 错误结果（不触达 handle）。"""
        response = httpx.post(f"{self.base}/api/wb/not-an-endpoint", json={}, timeout=30)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "trading/unknown-endpoint")

        # SDK 把「未知工具」作为工具级错误结果回给客户端（不是协议层 -32601）；
        # 关键断言是它确实报错，且错误信息点名工具——分发函数不可能被调用。
        result = self.call("not_a_tool", {})
        self.assertTrue(result.is_error, result)
        self.assertIn("not_a_tool", result.content[0].text)

    def test_failure_semantics_over_the_wire(self):
        """业务失败 isError=false + envelope；程序异常 isError=true + trading/tool-failed。"""
        broken = self.home / "trading-workbench.json"
        try:
            broken.write_text("{oops", encoding="utf-8")
            result = self.call("snapshot", {})
            body = mcp_tools.result_payload(result)
            self.assertTrue(result.is_error, body)
            self.assertEqual(body["error"]["code"], "trading/tool-failed")
            self.assertLessEqual(len(body["error"]["message"]), 300)
        finally:
            broken.unlink()


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
