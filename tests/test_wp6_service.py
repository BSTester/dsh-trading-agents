"""WP6 补遗 C 契约测试：FastAPI app（envelope/白名单/静态/认证）+ 计算桥 + TTL 缓存。

覆盖面（逐条对任务书）：
  * 契约：snapshot envelope、未知端点 404 且不触达 handle、GET 405、非 JSON 415、
    坏 JSON 400、超限 413、healthz 豁免、token 401/200 与静态豁免；
  * handle 分发：逐端点白名单拒绝、每端点错误码、形状校验失败消息、缓存语义；
  * switch-mode 全矩阵（HTTP 侧）；plan-execute 映射与「口令不落指令文件」；
  * 静态托管：200 / SPA 兜底 / 未构建 404 / 路径穿越 403；
  * 端到端：真实子进程取数在空 home 下给失败信封而绝不 500。

全部离线：分析层取数在绝大多数用例里被注入的 fake provider 替掉；只有最后一条端到端用例
调用真实 ``compute.run_script``，且只断言「有 envelope、不是 500」。
"""
import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402
from server import caches, compute, store_access  # noqa: E402

ENDPOINTS = [
    "snapshot", "switch-mode", "series", "equity", "positions", "correlation",
    "sensitivity", "risk", "trades", "events", "factors", "ic", "audit", "sources",
    "instrument", "quality", "plan", "plan-execute", "schedule", "reconcile",
    # 2026-09-15 业务确认：读待确认（不进缓存）+ 唯一的人工批准通道
    "confirmation", "confirm-decide",
    # WP7：因子快照历史（服务定时收集），与 store_access.WP7_ENDPOINTS 同步
    *store_access.WP7_ENDPOINTS,
    # WP8：富途实时直通（服务端实时取数），与 store_access.FUTU_ENDPOINTS 同步
    *store_access.FUTU_ENDPOINTS,
    # WP8 任务 2：OpenAPI 行情接入，与 store_access.WP8_MARKET_ENDPOINTS 同步
    *store_access.WP8_MARKET_ENDPOINTS,
    # WP8 任务 3：OpenAPI 交易只读端点，与 store_access.WP8_TRADE_ENDPOINTS 同步
    *store_access.WP8_TRADE_ENDPOINTS,
]


class RecordingProvider:
    """可注入的分析层替身：记录调用并按端点返回固定载荷（对齐 deps.analytics）。"""

    def __init__(self, values=None, error=None):
        self.values = values or {}
        self.error = error
        self.calls = []

    def __call__(self, endpoint):
        def provider(payload, force):
            self.calls.append((endpoint, dict(payload), force))
            if self.error is not None:
                raise self.error
            return self.values.get(endpoint, self._default(endpoint))
        return provider

    @staticmethod
    def _default(endpoint):
        return {
            "equity": {"mode": "sim", "points": [], "count": 0},
            "positions": {"mode": "sim", "groups": []},
            "correlation": {"matrix": []},
            "sensitivity": {"ticker": "600519", "matrix": []},
            "risk": {"config": {}},
            "trades": {"trades": []},
            "events": {"ticker": "600519", "events": []},
            "factors": {"tickers": []},
            "ic": {"points": []},
            "sources": {"sources": []},
            "instrument": {"ticker": "600519"},
            "quality": {"ticker": "600519"},
        }.get(endpoint, {})


def fake_analytics(recorder):
    """``{endpoint: callable(payload, force)}``：注入 create_app(analytics=...)。"""
    return {name: recorder(name) for name in app_module.ANALYTICS_ENDPOINTS}


def completed(stdout, returncode=0, stderr=""):
    """``subprocess.CompletedProcess`` 的最小替身：注入 runner 时用，不启动进程。"""
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


class IdleScheduler:
    """WP7 调度器替身：start/stop 只记账不调度，healthz 恒报空闲。

    Base.make_app 默认注入它，避免用例期起真调度线程触真实 SQLite/作业链；
    需要验证默认装配的用例自行传 ``scheduler=None`` 之外的值或显式覆盖。
    """

    def __init__(self):
        self.calls = []
        self.alive = False
        self.last_error = None

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        # 缓存目录隔离：caches 默认写 $DSH_HOME/trading-workbench-cache，测试必须换成临时
        # home，否则会把用例数据写进真实用户缓存（磁盘层是补遗 C 新增的行为）。
        caches.configure(home=str(self.home))

    def make_app(self, **kwargs):
        kwargs.setdefault("home", str(self.home))
        kwargs.setdefault("dist", str(Path(self._tmp.name) / "dist-missing"))
        # WP7：默认注入空转调度器——不进 lifespan 的用例本来就不会启动它，注入后连
        # 「构造真调度器（惰性导入 trading_core）」这一步也省掉，测试保持离线纯替身。
        kwargs.setdefault("scheduler", IdleScheduler())
        return app_module.create_app(**kwargs)

    def client(self, app):
        return TestClient(app, raise_server_exceptions=False)

    def post(self, client, endpoint, payload=None, headers=None, **kwargs):
        body = {} if payload is None else payload
        head = {"Content-Type": "application/json"}
        head.update(headers or {})
        return client.post(f"/api/wb/{endpoint}", content=json.dumps(body), headers=head, **kwargs)

    def make_dist(self):
        dist = Path(self._tmp.name) / "dist"
        (dist / "assets").mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<html>panel</html>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        return dist

    def write_config(self, **service):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"service": service}), encoding="utf-8")


class ContractTests(Base):
    """HTTP 面契约：状态码、信封、认证、静态。"""

    def test_snapshot_envelope_lists_all_endpoints(self):
        response = self.post(self.client(self.make_app()), "snapshot")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["value"]["endpoints"], ENDPOINTS)
        self.assertEqual(len(body["value"]["endpoints"]),
                        22 + len(store_access.WP7_ENDPOINTS) + len(store_access.FUTU_ENDPOINTS)
                        + len(store_access.WP8_MARKET_ENDPOINTS)
                        + len(store_access.WP8_TRADE_ENDPOINTS))
        self.assertEqual(body["value"]["mode"], "sim")
        self.assertIn("generated_at", body["value"])

    def test_unknown_endpoint_is_404_before_handle(self):
        """白名单 404 必须在 handle 之前返回：patch create_handler 数「handle 被调了几次」。

        路由层（``app.py`` 的 ``endpoint not in endpoints``）先返回，因此请求根本到不了
        handle。这里用 ``create_handler`` 的替身包一层计数（0 = 未知端点全被路由层拦下，
        1 = 白名单内请求确实触达 handle），比「注入取数替身没被调用」更直接：
        取数替身只在部分端点上可观测，而 handle 是唯一分发入口。
        """
        calls = []
        real = app_module.create_handler

        def spy(*args, **kwargs):
            handle = real(*args, **kwargs)

            def counted(endpoint, payload):
                calls.append((endpoint, payload))
                return handle(endpoint, payload)

            return counted

        with unittest.mock.patch.object(app_module, "create_handler", side_effect=spy):
            app = self.make_app(analytics=fake_analytics(RecordingProvider()),
                                series=lambda *a: {})
        client = self.client(app)
        for name in ("nope", "Foo", "snapshot2"):
            response = self.post(client, name)
            self.assertEqual(response.status_code, 404, name)
            self.assertEqual(response.json()["error"]["code"], "trading/unknown-endpoint")
        self.assertEqual(calls, [], "白名单 404 必须在 handle 之前返回（计数 0）")
        # 正对照：白名单内的一次请求必须真的进 handle（计数 1）
        self.assertEqual(self.post(client, "snapshot").status_code, 200)
        self.assertEqual([name for name, _ in calls], ["snapshot"])

    def test_method_and_media_type_guards(self):
        client = self.client(self.make_app())
        self.assertEqual(client.get("/api/wb/snapshot").status_code, 405)
        self.assertEqual(client.post("/api/wb/snapshot", content="{}",
                                     headers={"Content-Type": "text/plain"}).status_code, 415)
        self.assertEqual(client.post("/api/wb/snapshot", content="{oops",
                                     headers={"Content-Type": "application/json"}).status_code, 400)
        big = json.dumps({"pad": "x" * (1024 * 1024 + 8)})
        response = client.post("/api/wb/snapshot", content=big,
                               headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "trading/payload-too-large")

    def test_non_post_and_multi_segment_paths_use_envelope(self):
        """P1-1 实测矩阵：绝不能再落到 Starlette 的 ``{"detail": "Method Not Allowed"}``。

        Node 原实现（已退役）的判定顺序是「先方法、后格式」：``/api/wb/*`` 下非 POST 一律 405；
        POST 但路径不是单个 ``[a-z-]+`` 段（含 ``/``、尾斜杠、空段）→ 404 unknown-endpoint；
        其余路径非 GET → 405「仅 GET」。
        """
        client = self.client(self.make_app())
        for method in ("put", "delete", "options", "get"):
            response = getattr(client, method)("/api/wb/equity")
            self.assertEqual(response.status_code, 405, method)
            self.assertEqual(response.json()["error"],
                             {"code": "trading/method-not-allowed", "message": "仅 POST",
                              "details": {}}, method)
        for path in ("/api/wb/a/b", "/api/wb/snapshot/", "/api/wb/"):
            response = client.post(path, content="{}",
                                   headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 404, path)
            self.assertEqual(response.json()["error"]["code"], "trading/unknown-endpoint", path)
        response = client.post("/", content="{}", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["error"]["message"], "仅 GET")
        self.assertEqual(response.json()["error"]["code"], "trading/method-not-allowed")
        # 非 POST 的多段路径在原实现里同样是 405（方法先于路径格式判定）
        self.assertEqual(client.put("/api/wb/a/b").status_code, 405)

    def test_body_limit_only_applies_inside_wb_whitelist(self):
        """P1-2：413 判定在白名单与 content-type 之后，且只作用于 ``/api/wb/*``。"""
        dist = self.make_dist()
        client = self.client(self.make_app(dist=str(dist)))
        big = b"x" * (2 * 1024 * 1024)
        # 白名单外 + text/plain + 2MB → 先撞 404（不是 413）
        unknown = client.post("/api/wb/nope", content=big,
                              headers={"Content-Type": "text/plain"})
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"]["code"], "trading/unknown-endpoint")
        # 白名单内 + text/plain + 2MB → 415（不是 413）
        wrong_type = client.post("/api/wb/snapshot", content=big,
                                 headers={"Content-Type": "text/plain"})
        self.assertEqual(wrong_type.status_code, 415)
        # 静态请求声明 2MB 体也必须正常 200（上限不再作用于静态）
        asset = client.get("/assets/app.js", headers={"Content-Length": str(2 * 1024 * 1024)})
        self.assertEqual(asset.status_code, 200)
        self.assertIn("console.log", asset.text)

    def test_chunked_body_over_limit_is_413(self):
        """Q-3：没有 content-length 的 chunked 请求靠边读边数判定，超限立即 413。"""
        client = self.client(self.make_app())

        def chunks():
            for _ in range(3):  # 1.5MB > 1MB，且完全不声明 content-length
                yield b"x" * (512 * 1024)

        response = client.post("/api/wb/snapshot", content=chunks(),
                               headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "trading/payload-too-large")

        def small_chunks():
            yield b'{"pad": "ok"}'

        under = client.post("/api/wb/snapshot", content=small_chunks(),
                            headers={"Content-Type": "application/json"})
        self.assertEqual(under.status_code, 200)
        # snapshot 带载荷 → handle 层的未知操作（说明体确实被读全并解析了）
        self.assertFalse(under.json()["ok"])
        self.assertEqual(under.json()["error"]["message"], "Unknown workbench operation")

    def test_non_object_payload_is_400(self):
        client = self.client(self.make_app())
        response = client.post("/api/wb/snapshot", content="[]",
                               headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["message"], "Expected an object payload")

    def test_healthz_exempt_from_token(self):
        self.write_config(token="s3cret")
        client = self.client(self.make_app())
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        # WP7：healthz 附带调度器存活态（替身未启动 → alive=False、last_error=None）。
        body = response.json()
        # WP8 任务 4：healthz 增 push 字段（此处只锁定主字段与 push 的存在性/形状；
        # push 自身形状由 tests/test_wp8_push.py 的 PushServiceWiringTest 逐键锁定）。
        push = body.pop("push")
        self.assertEqual(body, {"ok": True, "mode": "sim",
                                "scheduler": {"alive": False, "last_error": None}})
        self.assertIsInstance(push.get("quote"), dict)
        self.assertIsInstance(push.get("trade"), dict)

    def test_token_required_for_api_and_static_exempt(self):
        dist = self.make_dist()
        self.write_config(token="s3cret")
        client = self.client(self.make_app(dist=str(dist)))
        self.assertEqual(self.post(client, "snapshot").status_code, 401)
        self.assertEqual(self.post(client, "snapshot").json()["error"]["code"],
                         "trading/unauthorized")
        ok = self.post(client, "snapshot", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["ok"])
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/assets/app.js").status_code, 200)

    def test_mcp_endpoint_is_mounted(self):
        """任务 D 后 /mcp 是真实 MCP streamable-http 端点（不再是 405 占位）。

        协议面（initialize / tools.list / 工具调用）由 tests/test_wp6_mcp.py 用真实 uvicorn
        + 官方 mcp 客户端覆盖（S1–S4）；这里只锁服务面事实：路由已挂载，且响应是 JSON-RPC
        线格式而不是 HTTP 框架的信封。Host 必须写成 ``127.0.0.1:<port>``——SDK 默认开启 DNS
        rebinding 保护，allowed_hosts 是 ``127.0.0.1:*`` 这种带端口的模式，TestClient 默认的
        ``testserver`` 或裸 ``127.0.0.1``（无端口）都会被 421 挡掉。
        """
        head = {"Accept": "application/json, text/event-stream"}
        with TestClient(self.make_app(), base_url="http://127.0.0.1:8397") as client:
            for response in (client.get("/mcp", headers=head),
                             client.post("/mcp", json={}, headers=head)):
                self.assertNotEqual(response.status_code, 405)
                self.assertNotEqual(response.status_code, 404)
                self.assertEqual(response.json()["jsonrpc"], "2.0")

    def test_unexpected_error_is_500_envelope(self):
        """500 兜底（Node 原实现已退役）：未预期异常也必须回 trading/internal 信封。

        触发方式：数据文件损坏时 ``read_store`` 抛 JSONDecodeError（不是 WorkbenchError，
        与 Node 侧 ``JSON.parse`` 抛 SyntaxError 同位置），因此它穿透 handle 到框架兜底，
        而不是被伪装成 ``trading/invalid-operation`` 的 200。
        """
        (self.home / "trading-workbench.json").write_text("{oops", encoding="utf-8")
        client = self.client(self.make_app())
        response = self.post(client, "snapshot")
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/internal")
        self.assertTrue(body["error"]["message"])


class HandleDispatchTests(Base):
    """handle 逐端点分发：白名单、错误码、缓存语义。"""

    def setUp(self):
        super().setUp()
        self.recorder = RecordingProvider()
        self.app = self.make_app(analytics=fake_analytics(self.recorder),
                                 series=lambda ticker, period, limit: {"ticker": ticker,
                                                                       "bars": []},
                                 core={name: (lambda data=name: {"plans": [], "alerts": [],
                                                                 "heartbeat": {}, "jobs": [],
                                                                 "diffs": [], "tca": {}})
                                       for name in compute.SNAPSHOT_COMMANDS})
        self.client = self.client(self.app)

    def test_payload_must_be_object(self):
        body = app_module.create_handler(str(self.home))("snapshot", ["x"])
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "Expected an object payload")

    def test_unknown_operation_message(self):
        handle = app_module.create_handler(str(self.home))
        body = handle("mystery", {})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertEqual(body["error"]["message"], "Unknown workbench operation")

    def test_whitelist_rejections_per_endpoint(self):
        cases = [
            ("equity", {"ticker": "600519"}, "Unexpected equity field"),
            ("positions", {"limit": 3}, "Unexpected positions field"),
            ("correlation", {"mode": "sim"}, "Unexpected correlation field"),
            ("sensitivity", {"window": 250}, "Unexpected sensitivity field"),
            ("risk", {"mode": "sim"}, "Unexpected risk field"),
            ("trades", {"window": 5}, "Unexpected trades field"),
            ("events", {"mode": "sim"}, "Unexpected events field"),
            ("factors", {"ticker": "600519"}, "Unexpected factors field"),
            ("ic", {"days": 30}, "Unexpected ic field"),
            ("sources", {"probe": True}, "Unexpected sources field"),
            ("instrument", {"days": 1}, "Unexpected instrument field"),
            ("quality", {"mode": "sim"}, "Unexpected quality field"),
            ("switch-mode", {"order_authorized": True}, "Unexpected switch-mode field"),
            ("plan-execute", {"nonce": "x"}, "Unexpected plan-execute field"),
            ("series", {"days": 5}, "Unexpected series field"),
        ]
        for endpoint, payload, message in cases:
            body = self.app.state.handle(endpoint, payload)
            self.assertFalse(body["ok"], endpoint)
            self.assertEqual(body["error"]["message"], message, endpoint)
            self.assertEqual(body["error"]["code"], "trading/invalid-operation", endpoint)

    def test_empty_payload_endpoints_reject_fields(self):
        for endpoint in ("audit", "plan", "schedule", "reconcile"):
            body = self.app.state.handle(endpoint, {"mode": "sim"})
            self.assertFalse(body["ok"], endpoint)
            self.assertEqual(body["error"]["message"], f"{endpoint} takes no payload", endpoint)

    def test_snapshot_with_payload_is_unknown(self):
        body = self.app.state.handle("snapshot", {"mode": "sim"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "Unknown workbench operation")

    def test_refresh_is_stripped_from_field_checks(self):
        """``_refresh`` 不进字段校验；只有严格 true 才绕过缓存（rpc.js:103/125）。"""
        first = self.app.state.handle("equity", {})
        self.assertTrue(first["ok"])
        self.assertFalse(first["cached"])
        # 非严格 true 的 _refresh 仍算「不强制」：命中上一次的缓存
        second = self.app.state.handle("equity", {"_refresh": 1})
        self.assertTrue(second["ok"])
        self.assertTrue(second["cached"])
        # 严格 true：绕过缓存重新取数
        third = self.app.state.handle("equity", {"_refresh": True})
        self.assertTrue(third["ok"])
        self.assertFalse(third["cached"])
        self.assertEqual(self.recorder.calls, [("equity", {}, False), ("equity", {}, True)])

    def test_analytics_success_then_cache_hit(self):
        first = self.app.state.handle("equity", {"mode": "sim"})
        self.assertTrue(first["ok"])
        self.assertFalse(first["cached"])
        self.assertEqual(self.recorder.calls, [("equity", {"mode": "sim"}, False)])
        second = self.app.state.handle("equity", {"mode": "sim"})
        self.assertTrue(second["ok"])
        self.assertTrue(second["cached"])
        self.assertEqual(first["cached_at"], second["cached_at"])
        self.assertEqual(len(self.recorder.calls), 1, "缓存命中不应再次取数")

    def test_cache_key_ignores_key_order(self):
        self.app.state.handle("equity", {"mode": "sim", "window": 250})
        self.app.state.handle("equity", {"window": 250, "mode": "sim"})
        self.assertEqual(len(self.recorder.calls), 1)

    def test_refresh_bypasses_cache(self):
        self.app.state.handle("equity", {})
        self.app.state.handle("equity", {"_refresh": True})
        self.assertEqual(len(self.recorder.calls), 2)

    def test_shape_failure_is_failure_envelope(self):
        bad = RecordingProvider(values={"equity": {"points": []}})
        app = self.make_app(analytics=fake_analytics(bad), series=lambda *a: {})
        body = app.state.handle("equity", {})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/analytics-unavailable")
        self.assertEqual(body["error"]["message"], "equity 返回的载荷不完整，已按失败处理")

    def test_provider_error_is_analytics_unavailable(self):
        failing = RecordingProvider(error=RuntimeError("富途不可用"))
        app = self.make_app(analytics=fake_analytics(failing), series=lambda *a: {})
        body = app.state.handle("risk", {})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/analytics-unavailable")
        self.assertEqual(body["error"]["message"], "富途不可用")

    def test_series_error_code(self):
        def broken(*_args):
            raise RuntimeError("bars.py 无 JSON 输出：boom")

        app = self.make_app(analytics=fake_analytics(RecordingProvider()), series=broken)
        body = app.state.handle("series", {"ticker": "600519"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/series-unavailable")
        self.assertIn("boom", body["error"]["message"])

    def test_series_success_and_limits(self):
        app = self.make_app(analytics=fake_analytics(RecordingProvider()),
                            series=lambda ticker, period, limit: {"ticker": ticker, "bars": []})
        body = app.state.handle("series", {"ticker": "600519"})
        self.assertTrue(body["ok"])
        self.assertEqual(body["value"], {"ticker": "600519", "bars": []})

    def test_core_endpoints_error_code(self):
        def broken():
            raise compute.ComputeError("trading_core: 库不可读")

        app = self.make_app(analytics=fake_analytics(RecordingProvider()),
                            series=lambda *a: {},
                            core={"snapshot-plan": broken, "snapshot-schedule": broken,
                                  "snapshot-reconcile": broken})
        for endpoint in ("plan", "schedule", "reconcile"):
            body = app.state.handle(endpoint, {})
            self.assertFalse(body["ok"], endpoint)
            self.assertEqual(body["error"]["code"], "trading/core-unavailable", endpoint)

    def test_plan_merges_current_mode(self):
        app = self.make_app(analytics=fake_analytics(RecordingProvider()),
                            series=lambda *a: {},
                            core={"snapshot-plan": lambda: {"plans": [], "alerts": []},
                                  "snapshot-schedule": lambda: {"heartbeat": {}, "jobs": []},
                                  "snapshot-reconcile": lambda: {"diffs": [], "tca": {}}})
        body = app.state.handle("plan", {})
        self.assertTrue(body["ok"])
        self.assertEqual(body["value"], {"plans": [], "alerts": [], "mode": "sim"})
        body = app.state.handle("schedule", {})
        self.assertEqual(body["value"], {"heartbeat": {}, "jobs": []})

    def test_audit_uses_trades_and_survives_failure(self):
        recorder = RecordingProvider(values={"trades": {"trades": [{"id": "t1", "ticker": "600519",
                                                                    "action": "BUY",
                                                                    "date": "2026-01-05"}]}})
        app = self.make_app(analytics=fake_analytics(recorder), series=lambda *a: {})
        body = app.state.handle("audit", {})
        self.assertTrue(body["ok"])
        self.assertEqual(recorder.calls[-1], ("trades", {"mode": "sim", "limit": 100}, False))
        self.assertEqual(set(body["value"]), {"entries", "stats"})

        failing = RecordingProvider(error=RuntimeError("台账不可读"))
        app = self.make_app(analytics=fake_analytics(failing), series=lambda *a: {})
        body = app.state.handle("audit", {})
        self.assertTrue(body["ok"], "台账不可读时仍要给出信号/响应链路")

    def test_switch_mode_requires_allowed_fields_only(self):
        body = self.app.state.handle("switch-mode", {"mode": "sim", "expected_mode": "sim"})
        self.assertTrue(body["ok"])
        self.assertFalse(body["value"]["order_authorized"])


class SwitchModeMatrixTests(Base):
    """switch-mode 全矩阵（HTTP 侧）。"""

    def setUp(self):
        super().setUp()
        self.client = self.client(self.make_app(analytics=fake_analytics(RecordingProvider()),
                                                series=lambda *a: {}))

    def set_mode(self, mode):
        (self.home / "trading-account-mode").write_text(f"{mode}\n", encoding="utf-8")

    def test_no_confirmation_for_live_is_rejected(self):
        self.set_mode("sim")
        body = self.post(self.client, "switch-mode",
                         {"mode": "live", "expected_mode": "sim"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertEqual(body["error"]["message"], "请输入「确认实盘」；切换模式不等于授权下单")

    def test_wrong_confirmation_is_rejected(self):
        self.set_mode("sim")
        body = self.post(self.client, "switch-mode",
                         {"mode": "live", "expected_mode": "sim", "confirmation": "确认"}).json()
        self.assertFalse(body["ok"])

    def test_confirmation_switches_and_never_authorizes_orders(self):
        self.set_mode("sim")
        response = self.post(self.client, "switch-mode",
                            {"mode": "live", "expected_mode": "sim", "confirmation": "确认实盘"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["value"], {"mode": "live", "previous_mode": "sim",
                                        "order_authorized": False})
        self.assertEqual(store_access.read_mode(self.home), "live")

    def test_stale_expected_mode_is_rejected(self):
        self.set_mode("live")
        body = self.post(self.client, "switch-mode",
                         {"mode": "sim", "expected_mode": "sim"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "Account mode changed; refresh before switching")

    def test_active_lease_is_rejected(self):
        self.set_mode("sim")
        (self.home / "trading-call-abc.active").write_text("", encoding="utf-8")
        body = self.post(self.client, "switch-mode",
                         {"mode": "sim", "expected_mode": "sim"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "有账户调用正在进行，请结束后切换")

    def test_invalid_mode_value_is_rejected(self):
        body = self.post(self.client, "switch-mode",
                         {"mode": "paper", "expected_mode": "sim"}).json()
        self.assertFalse(body["ok"])


class PlanExecuteTests(Base):
    """plan-execute：动作映射、live 口令门槛、指令文件内容。"""

    def setUp(self):
        super().setUp()
        self.client = self.client(self.make_app(analytics=fake_analytics(RecordingProvider()),
                                                series=lambda *a: {}))

    def set_mode(self, mode):
        (self.home / "trading-account-mode").write_text(f"{mode}\n", encoding="utf-8")

    def pending(self):
        pending = self.home / "trading-commands" / "pending"
        return sorted(pending.glob("*.json")) if pending.is_dir() else []

    def test_execute_requires_plan_hash(self):
        body = self.post(self.client, "plan-execute", {"expected_mode": "sim",
                                                       "action": "execute"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "plan-execute requires plan_hash")

    def test_execute_rejects_stale_expected_mode(self):
        body = self.post(self.client, "plan-execute",
                        {"plan_hash": "h1", "expected_mode": "live"}).json()
        self.assertFalse(body["ok"])
        self.assertIn("模式已变化", body["error"]["message"])

    def test_live_requires_passphrase(self):
        self.set_mode("live")
        body = self.post(self.client, "plan-execute",
                         {"plan_hash": "h1", "expected_mode": "live"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "实时账户执行需输入口令「确认执行」")
        self.assertEqual(self.pending(), [], "口令不通过时不得写指令文件")

    def test_live_wrong_passphrase_is_rejected(self):
        self.set_mode("live")
        body = self.post(self.client, "plan-execute",
                         {"plan_hash": "h1", "expected_mode": "live",
                          "confirmation": "确认"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(self.pending(), [])

    def test_live_with_passphrase_queues_without_persisting_it(self):
        self.set_mode("live")
        response = self.post(self.client, "plan-execute",
                             {"plan_hash": "h1", "expected_mode": "live",
                              "confirmation": "确认执行"})
        body = response.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"]["action"], "execute")
        self.assertTrue(body["value"]["nonce"])
        files = self.pending()
        self.assertEqual(len(files), 1)
        raw = files[0].read_text(encoding="utf-8")
        saved = json.loads(raw)
        self.assertEqual(saved["type"], "execute_plan")
        self.assertEqual(saved["plan_hash"], "h1")
        self.assertEqual(saved["expected_mode"], "live")
        self.assertNotIn("confirmation", saved)
        self.assertNotIn("确认执行", raw)

    def test_sim_execute_queues(self):
        body = self.post(self.client, "plan-execute",
                         {"plan_hash": "h2", "expected_mode": "sim"}).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"]["queued"], True)
        self.assertEqual(json.loads(self.pending()[0].read_text(encoding="utf-8"))["type"],
                         "execute_plan")

    def test_cancel_plan_carries_plan_hash(self):
        body = self.post(self.client, "plan-execute",
                         {"plan_hash": "h3", "action": "cancel"}).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["value"]["action"], "cancel")
        saved = json.loads(self.pending()[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["type"], "cancel_plan")
        self.assertEqual(saved["plan_hash"], "h3")

    def test_kill_and_unkill_map_to_commands(self):
        for action, expected in (("kill", "kill"), ("unkill", "unkill")):
            before = {path.name for path in self.pending()}
            body = self.post(self.client, "plan-execute", {"action": action}).json()
            self.assertTrue(body["ok"], action)
            self.assertEqual(body["value"]["action"], action)
            fresh = [path for path in self.pending() if path.name not in before]
            self.assertEqual(len(fresh), 1, action)
            saved = json.loads(fresh[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["type"], expected)
            self.assertEqual(saved["nonce"], body["value"]["nonce"])
            self.assertNotIn("plan_hash", saved)

    def test_unknown_action_is_rejected(self):
        body = self.post(self.client, "plan-execute", {"action": "wipe"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "Unknown plan-execute action: wipe")
        self.assertEqual(self.pending(), [])


class ConfirmationRoutesTests(Base):
    """业务确认路由契约（2026-09-15 修订）：22 端点白名单、confirmation 不缓存、批准走 HTTP。

    业务确认的语义细节（TTL/取消/重复决定/工具面排除）在
    ``tests/test_wp6_service_approval.py`` 的 R2′ 与 ``tests/test_wp6_store_access.py`` 的
    ``ConfirmationTest``；这里只钉服务面契约：路由可达、信封同形、**不进缓存**、批准通道
    经 HTTP 路由真的能让阻塞中的 ``request_confirmation`` 放行。
    """

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.client = self.client(self.app)

    def pending(self):
        return self.post(self.client, "confirmation").json()["value"]["pending"]

    def start(self, **overrides):
        kwargs = {"tool": "mcp__futu__trading_input_order", "mode": "live",
                  "args": {"symbol": "TSLL", "qty": 4}, "session_id": "s1"}
        kwargs.update(overrides)
        outcome = {}
        thread = threading.Thread(
            target=lambda: outcome.update(
                store_access.request_confirmation(str(self.home), **kwargs)), daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            view = self.pending()
            if view is not None:
                return view, outcome, thread
            time.sleep(0.005)
        self.fail("3 秒内未出现待确认项")

    def test_both_endpoints_are_whitelisted_and_snapshot_declares_them(self):
        self.assertEqual(len(store_access.endpoints()), 22 + len(store_access.WP7_ENDPOINTS)
                        + len(store_access.FUTU_ENDPOINTS)
                        + len(store_access.WP8_MARKET_ENDPOINTS)
                        + len(store_access.WP8_TRADE_ENDPOINTS))
        self.assertIn("confirmation", store_access.endpoints())
        self.assertIn("confirm-decide", store_access.endpoints())
        declared = self.post(self.client, "snapshot").json()["value"]["endpoints"]
        self.assertEqual(declared, ENDPOINTS)

    def test_confirmation_takes_no_payload_and_is_never_cached(self):
        first = self.post(self.client, "confirmation", {})
        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"], {"pending": None, "ttl_ms": store_access.CONFIRM_TTL_MS})
        self.assertNotIn("cached", body)
        second = self.post(self.client, "confirmation", {}).json()
        self.assertNotIn("cached", second, "confirmation 直读内存态，绝不能进 TTL 缓存")
        self.assertNotIn("cached_at", second)
        # handle 层与 HTTP 同源：同一载荷同结果
        self.assertEqual(self.app.state.handle("confirmation", {}), body)

        bad = self.post(self.client, "confirmation", {"mode": "sim"}).json()
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["message"], "confirmation takes no payload")

    def test_confirm_decide_envelope(self):
        cases = [
            ({}, "Invalid decision; expected approved/rejected"),
            ({"id": "x", "decision": "no"}, "Invalid decision; expected approved/rejected"),
            ({"id": "x", "decision": "approved"}, "没有待确认的实盘操作（可能已超时或被处理）"),
            ({"id": "x", "decision": "approved", "price": 9.3}, "Unexpected confirm-decide field"),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                body = self.post(self.client, "confirm-decide", payload).json()
                self.assertFalse(body["ok"], body)
                self.assertEqual(body["error"]["code"], "trading/invalid-operation")
                self.assertEqual(body["error"]["message"], message)

    def test_http_approval_releases_the_blocked_call(self):
        view, outcome, thread = self.start()
        decided = self.post(self.client, "confirm-decide",
                            {"id": view["id"], "decision": "approved"}).json()
        self.assertTrue(decided["ok"], decided)
        thread.join(5)
        self.assertEqual(outcome["decision"], "approved")
        self.assertEqual(outcome["reason"], "用户在工作台确认")
        self.assertIsNone(self.pending())


class StaticTests(Base):
    """静态托管：200 / SPA 兜底 / 未构建 404 / 路径穿越 403。"""

    def test_index_and_assets(self):
        dist = self.make_dist()
        client = self.client(self.make_app(dist=str(dist)))
        index = client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("panel", index.text)
        self.assertTrue(index.headers["content-type"].startswith("text/html"))
        asset = client.get("/assets/app.js")
        self.assertEqual(asset.status_code, 200)
        self.assertIn("text/javascript", asset.headers["content-type"])

    def test_mime_table_matches_service_mjs(self):
        """P1-4：只用手写 MIME 表，未收录扩展名 octet-stream，text/* 不加 charset。"""
        dist = self.make_dist()
        (dist / "notes.txt").write_text("plain", encoding="utf-8")
        (dist / "style.css").write_text("body{}", encoding="utf-8")
        client = self.client(self.make_app(dist=str(dist)))
        self.assertEqual(client.get("/notes.txt").headers["content-type"],
                         "application/octet-stream")
        self.assertEqual(client.get("/style.css").headers["content-type"], "text/css")
        self.assertEqual(client.get("/assets/app.js").headers["content-type"],
                         "text/javascript")
        self.assertEqual(client.get("/").headers["content-type"], "text/html; charset=utf-8")

    def test_spa_fallback(self):
        dist = self.make_dist()
        client = self.client(self.make_app(dist=str(dist)))
        response = client.get("/some/deep/route")
        self.assertEqual(response.status_code, 200)
        self.assertIn("panel", response.text)

    def test_unbuilt_dist_is_404(self):
        client = self.client(self.make_app())
        response = client.get("/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "trading/not-found")

    def test_path_traversal_is_forbidden(self):
        """穿越向量：既含客户端会规范化的形式，也含原样到达服务端的百分号编码形式。

        httpx 会把 ``/a/../b`` 规范成 ``/b``，所以这里不断言状态码，只断言**不泄漏**
        （服务端自身的边界由 ``test_raw_dot_segment_request_cannot_escape_root`` 用原始
        socket 断言）。
        """
        dist = self.make_dist()
        secret = Path(self._tmp.name) / "secret.txt"
        secret.write_text("top-secret", encoding="utf-8")
        client = self.client(self.make_app(dist=str(dist)))
        for path in ("/..%2fsecret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt",
                     "/%2e%2e%2f%2e%2e%2fetc%2fpasswd", "/..%5csecret.txt"):
            response = client.get(path)
            self.assertNotIn("top-secret", response.text, path)
            self.assertIn(response.status_code, (200, 400, 403, 404), path)

    def _raw_request(self, path):
        """真起一次 uvicorn，返回 ``(status, raw)``；顺带取出就绪行 stdout 供断言。

        用原始 socket 发请求是为了绕开 httpx 的 URL 规范化——``GET /../secret.txt``
        必须以字面形式到达服务端，才能验证服务端自己的路径边界防护。
        """
        from server import run as run_module

        app = self.make_app(dist=str(self.make_dist()))
        config = {"port": 0, "host": "127.0.0.1", "token": None}
        server = run_module.build_server(app, config, port=0)
        thread = threading.Thread(target=server.run, daemon=True)
        stdout = io.StringIO()
        with unittest.mock.patch("sys.stdout", stdout):
            thread.start()
            deadline = time.time() + 15
            while not server.started and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(server.started, "uvicorn 未在 15s 内启动")
            self.addCleanup(lambda: (setattr(server, "should_exit", True), thread.join(10)))
            port = server.servers[0].sockets[0].getsockname()[1]
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                sock.sendall(f"GET {path} HTTP/1.1\r\nHost: testserver\r\n"
                             "Connection: close\r\n\r\n".encode("latin-1"))
                chunks = []
                while True:
                    data = sock.recv(65536)
                    if not data:
                        break
                    chunks.append(data)
        raw = b"".join(chunks).decode("latin-1")
        ready = [line for line in stdout.getvalue().strip().splitlines() if line.startswith("{")]
        return port, int(raw.split(" ")[1]), raw, ready

    def test_raw_dot_segment_request_cannot_escape_root(self):
        """绕开 httpx 的 URL 规范化：手工发 ``GET /../secret.txt``，服务端必须挡住。"""
        secret = Path(self._tmp.name) / "secret.txt"
        secret.write_text("top-secret", encoding="utf-8")
        port, status, raw, ready = self._raw_request("/../secret.txt")
        self.assertEqual(status, 403, raw[:200])
        self.assertNotIn("top-secret", raw)
        self.assertNotIn("secret.txt", raw)
        # 顺带验证就绪行（Node 原实现已退役的单行 JSON）与真实端口
        self.assertTrue(ready)
        line = json.loads(ready[-1])
        self.assertEqual(line, {"ok": True, "service": "quant-platform",
                                "url": f"http://127.0.0.1:{port}",
                                "mcp": f"http://127.0.0.1:{port}/mcp",
                                "tools": 56, "auth": "loopback-only"})

    def test_bad_encoding_is_400(self):
        dist = self.make_dist()
        client = self.client(self.make_app(dist=str(dist)))
        response = client.get("/%zz")
        self.assertEqual(response.status_code, 400)


class EndToEndTests(Base):
    """端到端：完整链路（真子进程）只允许「信封」，绝不允许 500。

    真子进程只在三个用例里出现（其余全部走注入的 runner/provider），并且都不依赖网络成功：
    脚本自己把任何异常打成 ``{"error": ...}``。
    """

    def test_real_compute_failure_is_envelope_not_500(self):
        """取数失败（脚本抛错）时必须是 200 + 失败信封，绝不能是 500。"""
        def failing(command, timeout):
            del timeout
            script = Path(command[1]).name
            return completed(json.dumps({"error": f"{script} 富途 token 过期"}), returncode=1)

        app = self.make_app(analytics=compute.analytics_providers(failing), series=lambda *a: {})
        client = self.client(app)
        payloads = {"equity": {"mode": "sim"}, "risk": {}, "trades": {"mode": "sim"},
                    "sources": {}}
        for endpoint, payload in payloads.items():
            response = self.post(client, endpoint, payload)
            self.assertEqual(response.status_code, 200, endpoint)
            body = response.json()
            self.assertFalse(body["ok"], endpoint)
            self.assertEqual(body["error"]["code"], "trading/analytics-unavailable", endpoint)
            self.assertIn("富途 token 过期", body["error"]["message"], endpoint)

    def test_real_subprocess_bridge_runs(self):
        """真取数：equity 在空 home 下返回形状合法的载荷（离线时 points 可为空）。"""
        value = compute.analytics("equity", {"mode": "sim"})
        self.assertIn("mode", value)
        self.assertIn("points", value)
        self.assertIsInstance(value["points"], list)

    def test_runner_receives_exact_command(self):
        """注入 runner 钉住「脚本路径 + 参数 + 超时」这一层，不启动进程。"""
        seen = []

        def runner(command, timeout):
            seen.append((command, timeout))
            return completed('{"mode": "sim", "points": [], "count": 0}')

        value = compute.run_script("analytics.py", ["equity", "--mode", "sim"], runner=runner)
        self.assertEqual(value["count"], 0)
        command, timeout = seen[0]
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(command[1].endswith("plugins/workbench/python/analytics.py"))
        self.assertEqual(command[2:], ["equity", "--mode", "sim"])
        self.assertEqual(timeout, compute.TIMEOUT)

    def test_parse_stdout_contract(self):
        """``analytics.js:68-73``：首个 ``{`` 起 parse；error 键抛出；无 JSON / 坏 JSON 抛错。"""
        self.assertEqual(compute.parse_stdout("x.py", 'noise\n{"a": 1}'), {"a": 1})
        with self.assertRaises(compute.ComputeError) as caught:
            compute.parse_stdout("x.py", 'noise\n{"a": 1}\ntail')
        self.assertIn("输出不是合法 JSON", str(caught.exception))
        with self.assertRaises(compute.ComputeError) as caught:
            compute.parse_stdout("analytics.py", "", returncode=2, stderr="line1\nboom")
        self.assertIn("analytics.py 无 JSON 输出：boom", str(caught.exception))
        with self.assertRaises(compute.ComputeError):
            compute.parse_stdout("analytics.py", "{not json")
        with self.assertRaises(compute.ComputeError) as caught:
            compute.parse_stdout("analytics.py", '{"error": "富途不可用"}')
        self.assertEqual(str(caught.exception), "富途不可用")

    def test_snapshot_cli_parses_and_rejects(self):
        seen = []

        def runner(command, timeout):
            seen.append(command)
            return completed('{"plans": [], "alerts": []}')

        value = compute.snapshot_cli("snapshot-plan", runner=runner)
        self.assertEqual(value, {"plans": [], "alerts": []})
        self.assertEqual(seen[0][:4], [sys.executable, "-m", "trading_core", "snapshot-plan"])

    def test_series_uses_injected_runner(self):
        seen = []

        def runner(command, timeout):
            seen.append((command, timeout))
            return completed('{"ticker": "600519", "bars": []}')

        value = compute.series("600519", "1m", 20, runner=runner)
        self.assertEqual(value["bars"], [])
        command, timeout = seen[0]
        self.assertTrue(command[1].endswith("plugins/workbench/python/bars.py"))
        self.assertEqual(command[2:], ["--ticker", "600519", "--period", "1m", "--limit", "20"])
        self.assertEqual(timeout, 120_000)


class ComputeBridgeTests(Base):
    """计算桥单测：参数构造与 series 校验（不依赖真实取数）。"""

    def test_endpoint_table_covers_analytics_surface(self):
        self.assertEqual(set(compute.ENDPOINTS), set(app_module.ANALYTICS_ENDPOINTS))

    def test_arg_construction_matches_analytics_js(self):
        cases = [
            ("equity", {}, ["equity", "--mode", "sim", "--window", "250"]),
            ("equity", {"mode": "live", "window": 20}, ["equity", "--mode", "live",
                                                        "--window", "20"]),
            ("positions", {"mode": "live"}, ["--mode", "live"]),
            ("correlation", {"tickers": ["600519", "000001"]},
             ["correlation", "--tickers", "600519,000001", "--window", "120"]),
            ("events", {"ticker": "600519"}, ["--ticker", "600519", "--days", "180"]),
            ("factors", {"tickers": ["600519", "000001"]},
             ["snapshot", "--tickers", "600519,000001", "--window", "250"]),
            ("ic", {"tickers": ["600519", "000001", "601318"]},
             ["ic", "--tickers", "600519,000001,601318", "--factor", "mom_20",
              "--forward", "5", "--window", "250"]),
            ("risk", {}, ["risk"]),
            ("trades", {"mode": "live", "limit": 1}, ["trades", "--mode", "live", "--limit", "1"]),
            ("sources", {"no_probe": True}, ["--no-probe"]),
            ("sources", {}, []),
            ("instrument", {"ticker": "600519"}, ["--ticker", "600519"]),
            ("quality", {"ticker": "600519"}, ["--ticker", "600519"]),
            ("sensitivity", {"ticker": "600519"},
             ["--ticker", "600519", "--strategy", "ma_cross", "--metric", "total_return",
              "--start", "2023-01-01"]),
            ("sensitivity", {"ticker": "600519", "fast_grid": "3,5", "slow_grid": "10,20"},
             ["--ticker", "600519", "--strategy", "ma_cross", "--metric", "total_return",
              "--start", "2023-01-01", "--fast-grid", "3,5", "--slow-grid", "10,20"]),
        ]
        for endpoint, payload, expected in cases:
            script, build, _source = compute.ENDPOINTS[endpoint]
            force = False
            self.assertEqual(build(payload, force), expected, endpoint)
            self.assertTrue(script.endswith(".py"))

    def test_positions_refresh_flag(self):
        _script, build, _source = compute.ENDPOINTS["positions"]
        self.assertEqual(build({}, True), ["--mode", "sim", "--refresh"])
        self.assertEqual(build({}, False), ["--mode", "sim"])

    def test_arg_validation_messages(self):
        _script, equity, _s = compute.ENDPOINTS["equity"]
        for payload in ({"mode": "paper"}, {"window": 19}, {"window": 1001}, {"window": 1.5},
                        {"window": True}):
            with self.assertRaises(compute.ComputeError):
                equity(payload, False)
        _script, tickers, _s = compute.ENDPOINTS["factors"]
        with self.assertRaises(compute.ComputeError) as caught:
            tickers({"tickers": ["600519"]}, False)
        self.assertEqual(str(caught.exception), "tickers must list 2..8 symbols")
        _script, ic, _s = compute.ENDPOINTS["ic"]
        with self.assertRaises(compute.ComputeError) as caught:
            ic({"tickers": ["600519", "000001"]}, False)
        self.assertEqual(str(caught.exception), "IC 需要 3..8 个标的（横截面相关）")

    def test_run_script_missing_script_reports_no_json(self):
        """``run_script`` 没有脚本白名单：它拿到的是不存在的脚本路径，报「无 JSON 输出」。

        真正的白名单在 ``compute.ENDPOINTS``（端点 → 脚本）与 ``SNAPSHOT_COMMANDS``
        （见 ``test_snapshot_cli_whitelist``），``run_script`` 本身只负责「进程 + JSON + error」。
        """
        with self.assertRaises(compute.ComputeError) as caught:
            compute.run_script("nosuchscript.py", [])
        self.assertIn("nosuchscript.py 无 JSON 输出", str(caught.exception))

    def test_endpoint_timeouts_match_analytics_js(self):
        """P1-3：逐端点 timeout 对齐 analytics.js（默认 180s，instrument 120s）。"""
        self.assertEqual(compute.TIMEOUT, 180_000)
        self.assertEqual(compute.timeout_for("equity"), 180_000)
        self.assertEqual(compute.timeout_for("trades"), 180_000)
        self.assertEqual(compute.timeout_for("instrument"), 120_000)
        seen = {}

        def runner(command, timeout):
            seen[Path(command[1]).name] = timeout
            return completed('{"ticker": "600519"}')

        providers = compute.analytics_providers(runner)
        providers["instrument"]({"ticker": "600519"}, False)
        providers["equity"]({"mode": "sim"}, False)
        self.assertEqual(seen, {"instruments.py": 120_000, "analytics.py": 180_000})

    def test_null_only_params_use_js_default_semantics(self):
        """``or`` → ``??``：只有 null/undefined 才兜底，空串是非法值。

        这里覆盖合法的一侧（None = JS 的 null 走默认），非法空串一侧见
        ``ParamSemanticsTests``（HTTP 全链路）。
        """
        _script, sensitivity, _s = compute.ENDPOINTS["sensitivity"]
        self.assertEqual(sensitivity({"ticker": "600519", "strategy": None, "metric": None,
                                      "start": None}, False),
                         ["--ticker", "600519", "--strategy", "ma_cross",
                          "--metric", "total_return", "--start", "2023-01-01"])
        # grid 字段显式 null 与 JS 的 ``value === undefined`` 不同：必须报错
        with self.assertRaises(compute.ComputeError) as caught:
            sensitivity({"ticker": "600519", "fast_grid": None}, False)
        self.assertEqual(str(caught.exception), "Invalid fast_grid")
        _script, ic, _s = compute.ENDPOINTS["ic"]
        tickers = {"tickers": ["600519", "000001", "601318"]}
        self.assertEqual(ic({**tickers, "factor": None}, False),
                         ["ic", "--tickers", "600519,000001,601318", "--factor", "mom_20",
                          "--forward", "5", "--window", "250"])

    def test_series_validation(self):
        with self.assertRaises(compute.ComputeError) as caught:
            compute.series("bad ticker!")
        self.assertEqual(str(caught.exception), "Invalid ticker")
        with self.assertRaises(compute.ComputeError) as caught:
            compute.series("600519", period="2h")
        self.assertEqual(str(caught.exception), "Invalid period")
        for limit in (19, 2001, 1.5, True):
            with self.assertRaises(compute.ComputeError) as caught:
                compute.series("600519", limit=limit)
            self.assertEqual(str(caught.exception), "Invalid limit (20..2000)")

    def test_snapshot_cli_whitelist(self):
        with self.assertRaises(compute.ComputeError):
            compute.snapshot_cli("snapshot-something")
        self.assertEqual(compute.SNAPSHOT_COMMANDS,
                         ("snapshot-plan", "snapshot-schedule", "snapshot-reconcile"))
        self.assertEqual(compute.PYTHON, sys.executable)

    def test_stable_key_is_order_independent(self):
        self.assertEqual(caches.stable_key({"a": 1, "b": 2}), caches.stable_key({"b": 2, "a": 1}))
        self.assertNotEqual(caches.stable_key({"a": 1}), caches.stable_key({"a": 2}))

    def test_cache_ttl_table_matches_rpc_js(self):
        self.assertEqual(caches.CACHE_TTL_MS["plan"], 60_000)
        self.assertEqual(caches.CACHE_TTL_MS["schedule"], 30_000)
        self.assertEqual(caches.CACHE_TTL_MS["reconcile"], 5 * 60_000)
        # WP7 任务 2：+factors-history（5m，服务自有端点，不在 legacy rpc.js 表里）
        self.assertEqual(caches.CACHE_TTL_MS["factors-history"], 5 * 60_000)
        # WP8 任务 2：+基本类 5m ×4 + quote_history_kline_v2 10m（实时类不进表）
        self.assertEqual(len(caches.CACHE_TTL_MS), 23)
        self.assertEqual(caches.CACHE_TTL_MS["quality"], 60 * 60_000)

    def test_shape_table_and_matcher(self):
        self.assertEqual(caches.ENDPOINT_SHAPE["series"], ["ticker", "bars"])
        self.assertEqual(caches.ENDPOINT_SHAPE["plan"], ["plans", "alerts"])
        self.assertTrue(caches.matches_shape("plan-execute", {"queued": True}))
        self.assertFalse(caches.matches_shape("series", None))
        self.assertFalse(caches.matches_shape("series", []))
        self.assertFalse(caches.matches_shape("series", {"ticker": "x"}))

    def test_cached_reports_cached_at(self):
        calls = []
        result = caches.cached("risk", {}, False, lambda: (calls.append(1), {"config": {}})[1],
                               "trading/analytics-unavailable")
        self.assertTrue(result["ok"])
        self.assertFalse(result["cached"])
        self.assertTrue(result["cached_at"].endswith("Z"))
        again = caches.cached("risk", {}, False, lambda: (calls.append(1), {"config": {}})[1],
                              "trading/analytics-unavailable")
        self.assertTrue(again["cached"])
        self.assertEqual(result["cached_at"], again["cached_at"])
        self.assertEqual(len(calls), 1)

    def test_cached_message_is_clipped_to_300(self):
        def boom():
            raise RuntimeError("x" * 500)

        result = caches.cached("risk", {}, False, boom, "trading/analytics-unavailable")
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["error"]["message"]), 300)
        self.assertEqual(result["error"]["details"], {})

    def test_cache_read_respects_ttl_boundary(self):
        """``rpc.js:64`` 的 ``now() - hit.at < TTL``：未过 TTL 命中，过 TTL 未命中。"""
        caches.write("risk", {}, {"config": {}}, now=1000)
        self.assertEqual(caches.read("risk", {}, 60_000, now=60_999)[0], 1000)
        self.assertIsNone(caches.read("risk", {}, 60_000, now=61_000))
        self.assertIsNone(caches.read("risk", {}, 0, now=1000))
        self.assertIsNone(caches.read("risk", {}, 60_000, now=1000_000))

    def test_cached_treats_bad_entry_as_miss(self):
        """缓存里是坏条目（形状不符）时重新取数，而不是把它当数据返回（rpc.js:78-79）。"""
        caches.write("risk", {}, {"not_config": True})
        calls = []
        result = caches.cached("risk", {}, False,
                               lambda: (calls.append(1), {"config": {}})[1],
                               "trading/analytics-unavailable")
        self.assertTrue(result["ok"])
        self.assertFalse(result["cached"])
        self.assertEqual(len(calls), 1)

    def test_cached_force_reads_through_and_overwrites(self):
        """``force=True``（_refresh）跳过读缓存，但结果**照旧写回**（rpc.js:76/89）。"""
        caches.clear()
        first = caches.cached("risk", {}, False, lambda: {"config": {"n": 1}},
                              "trading/analytics-unavailable")
        self.assertFalse(first["cached"])
        calls = []
        second = caches.cached("risk", {}, True,
                               lambda: (calls.append(1), {"config": {"n": 2}})[1],
                               "trading/analytics-unavailable")
        self.assertEqual(len(calls), 1, "force 必须真的重新取数")
        self.assertFalse(second["cached"])
        self.assertEqual(second["value"]["config"]["n"], 2)
        third = caches.cached("risk", {}, False, lambda: {"config": {"n": 3}},
                              "trading/analytics-unavailable")
        self.assertTrue(third["cached"], "force 取到的新值应写回缓存")
        self.assertEqual(third["value"]["config"]["n"], 2)


class ParamSemanticsTests(Base):
    """P1-3：``or`` → ``??``（仅 None 兜底）后的参数校验，走**真实** provider 全链路。

    这些用例刻意不注入分析层：默认 provider 就是 ``compute.DEFAULT_ANALYTICS``，参数校验在
    起子进程之前完成，因此错误的空值会以失败信封返回、且不会真的 spawn 任何进程。
    """

    def setUp(self):
        super().setUp()
        self.client = self.client(self.make_app())

    def test_invalid_empty_values_are_rejected_end_to_end(self):
        cases = [
            ("sensitivity", {"ticker": "600519", "strategy": ""}, "Invalid strategy"),
            ("sensitivity", {"ticker": "600519", "metric": ""}, "Invalid metric"),
            ("sensitivity", {"ticker": "600519", "fast_grid": None}, "Invalid fast_grid"),
            ("sensitivity", {"ticker": "600519", "start": ""}, "Invalid start date"),
            ("ic", {"tickers": ["600519", "000001", "601318"], "factor": ""}, "Invalid factor"),
            ("series", {"ticker": "600519", "period": None}, "Invalid period"),
            ("series", {"ticker": "600519", "period": ""}, "Invalid period"),
            ("plan-execute", {"action": ""}, "Unknown plan-execute action: "),
        ]
        for endpoint, payload, message in cases:
            body = self.post(self.client, endpoint, payload).json()
            self.assertFalse(body["ok"], (endpoint, payload))
            self.assertEqual(body["error"]["message"], message, (endpoint, payload))

    def test_null_defaults_still_work_end_to_end(self):
        """反向对照：显式 null（JS 的 ``null ??``）仍走默认值，不能被误判成非法。"""
        body = self.post(self.client, "series",
                         {"ticker": "bad ticker!", "period": None}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "Invalid ticker")


class InnerAnalyticsCacheTests(Base):
    """Q-2：analytics provider 的 30s 内层缓存（analytics.js:16/62-77）。"""

    def providers(self, runner):
        return compute.analytics_providers(runner)

    def test_audit_reuses_trades_subprocess(self):
        calls = []

        def runner(command, timeout):
            calls.append((list(command), timeout))
            return completed('{"trades": []}')

        app = self.make_app(analytics=compute.analytics_providers(runner),
                            series=lambda *a: {},
                            core={name: (lambda data=name: {"plans": [], "alerts": [],
                                                            "heartbeat": {}, "jobs": [],
                                                            "diffs": [], "tca": {}})
                                  for name in compute.SNAPSHOT_COMMANDS})
        for _ in range(3):
            body = app.state.handle("audit", {})
            self.assertTrue(body["ok"], body)
        self.assertEqual(len(calls), 1, "30s 内同一 (脚本, 参数) 只应起一次子进程")
        command, timeout = calls[0]
        self.assertEqual(Path(command[1]).name, "analytics.py")
        self.assertEqual(command[2:], ["trades", "--mode", "sim", "--limit", "100"])
        self.assertEqual(timeout, compute.TIMEOUT)

    def test_inner_cache_key_separates_arguments(self):
        calls = []

        def runner(command, timeout):
            calls.append(list(command[2:]))
            return completed('{"trades": []}')

        providers = self.providers(runner)
        providers["trades"]({"mode": "sim", "limit": 1}, False)
        providers["trades"]({"mode": "sim", "limit": 2}, False)
        providers["trades"]({"mode": "sim", "limit": 1}, False)  # 回到旧参数 → 命中
        self.assertEqual(len(calls), 2, "参数不同必须各自起进程，参数相同必须复用")

    def test_force_refetch_is_visible_for_positions(self):
        """positions 的 refresh 会 skipCache（analytics.js:93），并带上 ``--refresh``。"""
        calls = []

        def runner(command, timeout):
            calls.append(list(command[2:]))
            return completed('{"mode": "sim", "groups": []}')

        providers = self.providers(runner)
        providers["positions"]({"mode": "sim"}, False)
        providers["positions"]({"mode": "sim"}, False)
        self.assertEqual(len(calls), 1)
        providers["positions"]({"mode": "sim"}, True)
        self.assertEqual(len(calls), 2)
        self.assertIn("--refresh", calls[1])

    def test_force_does_not_bypass_inner_cache_for_other_endpoints(self):
        """对齐 analytics.js：``_refresh`` 只绕过外层 TTL；30s 内层缓存照旧命中。

        这是**有意**的移植语义（equity() 不接收 refresh 参数），不是漏传 force。
        """
        calls = []

        def runner(command, timeout):
            calls.append(list(command[2:]))
            return completed('{"mode": "sim", "points": []}')

        providers = self.providers(runner)
        providers["equity"]({"mode": "sim"}, False)
        providers["equity"]({"mode": "sim"}, True)
        self.assertEqual(len(calls), 1)


class DiskCacheTests(unittest.TestCase):
    """Q-1：caches 的内存 + 磁盘两级（``cache.js`` 等价物）。全部离线，只碰临时目录。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def cache(self, now=None, **kwargs):
        return caches.TtlCache(home=str(self.home), now=now, **kwargs)

    def files(self):
        directory = Path(caches.default_dir(str(self.home)))
        return sorted(directory.glob("*.json")) if directory.is_dir() else []

    def test_default_dir_follows_home_and_dsh_home(self):
        self.assertEqual(caches.default_dir("/tmp/x"), "/tmp/x/trading-workbench-cache")
        with unittest.mock.patch.dict(os.environ, {"DSH_HOME": "/tmp/y"}):
            self.assertEqual(caches.default_dir(), "/tmp/y/trading-workbench-cache")

    def test_safe_name_matches_cache_js_shape(self):
        name = caches.safe_name("switch-mode|[]")
        self.assertRegex(name, r"^switch-mode-[0-9a-f]{16}\.json$")
        self.assertEqual(caches.safe_name("a/b|[]").split("-")[0], "a_b")

    def test_write_then_new_instance_reads_hit(self):
        self.cache().write("risk|[]", {"config": {"n": 1}})
        self.assertTrue(self.files(), "写盘必须真的落文件")
        fresh = caches.TtlCache(home=str(self.home))
        hit = fresh.read("risk|[]", 60_000)
        self.assertEqual(hit["value"], {"config": {"n": 1}})
        self.assertIn("at", hit)

    def test_expired_entry_is_miss_and_file_is_removed(self):
        self.cache(now=lambda: 1_000).write("risk|[]", {"config": {}})
        self.assertTrue(self.files())
        fresh = caches.TtlCache(home=str(self.home))
        self.assertIsNone(fresh.read("risk|[]", 60_000, now=61_000))
        self.assertEqual(self.files(), [], "过期条目必须被删文件（cache.js:52）")

    def test_bad_json_and_non_object_are_miss(self):
        directory = Path(caches.default_dir(str(self.home)))
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / caches.safe_name("risk|[]")
        for junk in ("{not json", "[1, 2]", '{"value": 1}', '{"at": "yesterday"}'):
            target.write_text(junk, encoding="utf-8")
            self.assertIsNone(caches.TtlCache(home=str(self.home)).read("risk|[]", 60_000), junk)

    def test_oversized_entry_stays_in_memory_only(self):
        cache = self.cache(max_bytes=1024)
        value = {"config": {"pad": "x" * 4096}}
        entry = cache.write("risk|[]", value)
        self.assertEqual(entry["value"], value)
        self.assertEqual(self.files(), [], "超 maxBytes 只留内存不落盘")
        self.assertEqual(cache.read("risk|[]", 60_000)["value"], value)
        self.assertIsNone(caches.TtlCache(home=str(self.home)).read("risk|[]", 60_000))

    def test_ttl_zero_means_no_cache(self):
        cache = self.cache()
        cache.write("risk|[]", {"config": {}})
        self.assertIsNone(cache.read("risk|[]", 0))

    def test_prune_evicts_oldest_beyond_max_files(self):
        cache = self.cache(max_files=3)
        for index in range(6):
            cache.write(f"risk|{index}", {"config": {"n": index}})
            time.sleep(0.01)  # mtime 淘汰需要可分辨的时间戳
        self.assertEqual(len(self.files()), 3)
        names = [path.name for path in self.files()]
        newest = caches.safe_name("risk|5")
        self.assertIn(newest, names, "最新写入必须留下")
        self.assertNotIn(caches.safe_name("risk|0"), names)

    def test_prune_removes_stray_tmp_files(self):
        directory = Path(caches.default_dir(str(self.home)))
        directory.mkdir(parents=True, exist_ok=True)
        stray = directory / "risk-deadbeefdeadbeef.json.1234.abcd.tmp"
        stray.write_text("partial", encoding="utf-8")
        self.cache().write("risk|[]", {"config": {}})
        self.assertFalse(stray.exists(), "崩溃残留的临时文件必须被清掉（cache.js:70-72）")

    def test_module_level_cache_is_isolated_by_configure(self):
        configured = caches.configure(home=str(self.home))
        self.assertEqual(configured.directory, caches.default_dir(str(self.home)))
        caches.write("risk", {}, {"config": {}}, now=5_000)
        self.assertEqual(caches.read("risk", {}, 60_000, now=5_001), (5_000, {"config": {}}))
        caches.configure(home=str(self.home / "other"))
        self.assertIsNone(caches.read("risk", {}, 60_000, now=5_002))


class DefaultWiringSmokeTests(Base):
    """Q-6：不注入任何依赖的冒烟（真 ``python -m trading_core`` 子进程，无网络）。"""

    def test_plan_and_schedule_with_default_wiring(self):
        client = self.client(self.make_app())  # analytics/series/core 全走默认实现
        cases = {"plan": {"plans", "alerts", "mode"}, "schedule": {"heartbeat", "jobs"}}
        for endpoint, fields in cases.items():
            response = self.post(client, endpoint)
            self.assertEqual(response.status_code, 200, endpoint)
            body = response.json()
            self.assertTrue(body["ok"], (endpoint, body))
            self.assertTrue(fields <= set(body["value"]), (endpoint, body["value"]))
            self.assertFalse(body["cached"], endpoint)

    @unittest.skipUnless(os.environ.get("DSH_WP6_SLOW") == "1",
                         "慢用例（bars.py 真实取数可能 ~10s）：DSH_WP6_SLOW=1 时开启")
    def test_series_with_default_wiring_optional(self):
        client = self.client(self.make_app())
        response = self.post(client, "series", {"ticker": "600519", "period": "1d", "limit": 20})
        self.assertEqual(response.status_code, 200)
        self.assertIn("value" if response.json()["ok"] else "error", response.json())


class RunEntryTests(Base):
    """入口：就绪行形状、uvicorn 配置、启动失败契约与解释器告警。"""

    def test_repo_data_layer_takes_precedence_over_installed_copy(self):
        """WP8 修正：仓库存在时，入口把仓库数据层插到 sys.path 最前。

        背景：venv 的 dsh-trading-python.pth 指向 $DSH_HOME 的**副本**，
        新增模块不会自动同步（实测缺 futu_openapi / sign_ws）。从仓库运行服务时
        必须以仓库代码为准，否则「测试全绿、服务报 AttributeError」。
        """
        import sys as _sys

        from server import run as run_module  # noqa: F401

        repo = Path(__file__).resolve().parents[1]
        dirs = (repo / "plugins" / "datasource" / "python",
                repo / "plugins" / "core" / "python")
        for directory in dirs:
            self.assertIn(str(directory), _sys.path, str(directory))
        first_site_packages = min(
            (i for i, entry in enumerate(_sys.path) if "site-packages" in entry),
            default=len(_sys.path))
        for directory in dirs:
            self.assertLess(_sys.path.index(str(directory)), first_site_packages)

    def test_ready_line_shape(self):
        from server import run as run_module
        config = {"port": 0, "host": "127.0.0.1", "token": None}
        line = run_module.ready_line(("127.0.0.1", 41234), config)
        self.assertEqual(line, {"ok": True, "service": "quant-platform",
                                "url": "http://127.0.0.1:41234",
                                "mcp": "http://127.0.0.1:41234/mcp",
                                "tools": 56, "auth": "loopback-only"})
        line = run_module.ready_line(("127.0.0.1", 8397),
                                     {"port": 8397, "host": "127.0.0.1", "token": "t"})
        self.assertEqual(line["auth"], "token")
        self.assertEqual(run_module.ready_line(None, {"port": 8397, "host": "127.0.0.1",
                                                      "token": None})["url"],
                         "http://127.0.0.1:8397")

    def test_build_server_does_not_listen(self):
        from server import run as run_module
        app = self.make_app()
        config = {"port": 0, "host": "127.0.0.1", "token": None}
        server = run_module.build_server(app, config)
        self.assertIsNone(server.ready_line)
        self.assertEqual(server.config.log_level, "warning")
        self.assertEqual(server.config.timeout_graceful_shutdown, 5)

    def test_main_returns_zero(self):
        from server import run as run_module
        app = self.make_app()
        config = {"port": 0, "host": "127.0.0.1", "token": None}
        server = run_module.build_server(app, config)
        ran = []
        server.run = lambda: ran.append(True)  # 不真起监听
        with unittest.mock.patch.object(run_module, "build_server", return_value=server):
            self.assertEqual(run_module.main([]), 0)
        self.assertEqual(ran, [True])

    def test_main_reports_eaddrinuse_as_json_and_returns_one(self):
        """P1-5：占住端口后真跑一次 uvicorn。

        uvicorn 0.53 的 bind 失败走 ``sys.exit(3)``（不是 OSError），因此 ``main()`` 必须捕
        ``SystemExit`` 才能打印失败单行 JSON 并以 1 退出——旧的 ``except OSError`` 是死代码。
        """
        from server import run as run_module

        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        port = holder.getsockname()[1]
        (self.home / "trading-platform.json").write_text(
            json.dumps({"service": {"port": port, "host": "127.0.0.1"}}), encoding="utf-8")
        stderr = io.StringIO()
        env = {"DSH_HOME": str(self.home), "TRADING_SERVICE_PORT": str(port)}
        with unittest.mock.patch.dict(os.environ, env):
            with contextlib.redirect_stderr(stderr):
                code = run_module.main([])
        self.assertEqual(code, 1)
        raw = stderr.getvalue()
        self.assertIn('"ok": false', raw)
        failure = [json.loads(line) for line in raw.splitlines() if line.startswith("{")]
        self.assertTrue(failure, raw)
        self.assertFalse(failure[-1]["ok"])
        self.assertEqual(failure[-1]["service"], "quant-platform")
        self.assertIn(str(port), failure[-1]["error"])

    def test_interpreter_warning_detects_foreign_venv(self):
        """Q-8：venv 解释器存在且与 sys.executable 不同 → 返回告警行；一致/缺失 → 静默。"""
        from server import run as run_module

        self.assertIsNone(run_module.interpreter_warning(str(self.home)))
        binary = self.home / "trading-venv" / "bin"
        binary.mkdir(parents=True)
        (binary / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        warning = run_module.interpreter_warning(str(self.home))
        self.assertIsNotNone(warning)
        self.assertEqual(warning["level"], "warning")
        self.assertIn("trading-venv", warning["message"])
        # 同一解释器（venv 内启动）不告警
        (binary / "python").unlink()
        os.symlink(sys.executable, binary / "python")
        self.assertIsNone(run_module.interpreter_warning(str(self.home)))

    def test_main_prints_interpreter_warning_to_stderr(self):
        from server import run as run_module

        binary = self.home / "trading-venv" / "bin"
        binary.mkdir(parents=True)
        (binary / "python").write_text("#!/bin/sh\n", encoding="utf-8")
        app = self.make_app()
        server = run_module.build_server(app, {"port": 0, "host": "127.0.0.1", "token": None})
        server.run = lambda: None
        stderr = io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)}):
            with unittest.mock.patch.object(run_module, "build_server", return_value=server):
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(run_module.main([]), 0)
        self.assertIn("trading-venv", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
