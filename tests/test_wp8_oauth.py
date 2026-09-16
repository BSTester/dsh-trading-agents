"""WP8：OAuth 2.1 + PKCE 授权流程集成（服务端流程管理 + 设置页端点）。

被测对象 ``platform/server/oauth_flow.py``（流程生命周期）与 app.py 的
``openapi_oauth`` 路由分支。全部离线：register/token 走注入 http 替身，
回调 listener 是 127.0.0.1 真实 socket（localhost 回环，无外网）。

覆盖（对应任务规格 §7）：

  * PKCE：verifier→challenge S256（RFC 7636 附录 B 官方向量）；
  * start：mock register → 返回 auth_url 含正确参数（response_type/client_id/
    redirect_uri/state/code_challenge/code_challenge_method=S256），client_id
    无则自动注册并复用；
  * callback：mock 授权回调 → 校验 state → code+code_verifier 换 token →
    落盘 CredentialStore（mode=oauth，0600，app_key 既有字段保留）→ status=done；
  * state 不匹配 → 拒绝换 token（token 端点零调用）；
  * 授权被拒（error 回调）→ 如实报拒绝原因；
  * 超时 → 标记超时并停 listener；
  * 真实 listener 集成：urllib 打 http://127.0.0.1:<port>/callback → 全流程 done；
    仅绑 127.0.0.1；无关探针 404；cancel 后端口释放；
  * 路由面：openapi_oauth 进端点清单/snapshot 声明但**有意排除在 MCP 工具面外**；
    token 认证 401；action 白名单与载荷白名单；
  * 安全不变式：code_verifier 不落盘、不进 status()/auth_url/错误文案；
  * AppKey 模式不受影响：oauth 落盘保留 app_key 既有字段，settings_api 口径不变。
"""
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402
from server import mcp_tools, oauth_flow, store_access  # noqa: E402
from server import settings_api  # noqa: E402
from server.store_access import WorkbenchError  # noqa: E402
from trading_datasource import futu_openapi as fo  # noqa: E402


def _free_port():
    """抓一个空闲回环端口（bind→读端口号→close；测试期占用窗口极小）。"""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class Clock:
    """可手拨的毫秒时钟（now 注入：expires_at 与超时判定全部离线可测）。"""

    def __init__(self, now_ms=1_700_000_000_000):
        self.now_ms = now_ms

    def __call__(self):
        return self.now_ms


class FakeHttp:
    """注入传输：按 URL 脚本化响应并记录调用（mock register/token 端点）。"""

    def __init__(self, register=None, token=None, fail=False):
        self.calls = []
        self.register = register if register is not None else {"client_id": "cid-1234"}
        self.token = token if token is not None else {
            "access_token": "at-1", "refresh_token": "rt-1",
            "expires_in": 7200, "scope": "quote:read trade:read"}
        self.fail = fail

    def __call__(self, url, payload=None, form=None):
        if self.fail:
            raise OSError("network unreachable")
        self.calls.append({"url": url, "payload": payload, "form": form})
        return self.token if form is not None else self.register

    def calls_to(self, url):
        return [call for call in self.calls if call["url"] == url]


class IdleScheduler:
    """create_app(scheduler=...) 的替身（与 test_wp8_settings 同款，零线程）。"""

    def __init__(self):
        self.alive = False
        self.last_error = None

    def start(self):
        self.alive = True

    def stop(self):
        self.alive = False


def _poll(condition, timeout=5.0, interval=0.05):
    """轮询直至 condition() 为真；超时 fail（真实 socket 异步收尾用）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


# ================================================================ PKCE 纯函数

class PkceTest(unittest.TestCase):

    RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    def test_challenge_matches_rfc7636_appendix_b(self):
        """S256 = BASE64URL(SHA256(verifier)) 无 padding，RFC 7636 附录 B 向量逐字。"""
        self.assertEqual(oauth_flow.challenge(self.RFC_VERIFIER), self.RFC_CHALLENGE)

    def test_pkce_pair_is_consistent_and_in_shape(self):
        verifier, challenge = oauth_flow.pkce_pair()
        self.assertEqual(challenge, oauth_flow.challenge(verifier))
        self.assertEqual(challenge, fo.Pkce.challenge(verifier))
        self.assertRegex(verifier, r"\A[A-Za-z0-9\-._~]+\Z")
        self.assertTrue(43 <= len(verifier) <= 128)

    def test_challenge_differs_for_different_verifiers(self):
        self.assertNotEqual(oauth_flow.challenge("verifier-one"),
                            oauth_flow.challenge("verifier-two"))


# ================================================================ start：注册+授权 URL

class StartFlowTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cred_path = self.home / "futu-openapi.json"
        self.store = fo.CredentialStore(self.cred_path)
        self.fake = FakeHttp()
        self.port = _free_port()
        self.manager = oauth_flow.OAuthFlowManager(
            self.store, str(self.home), callback_port=self.port,
            now=Clock(), http=self.fake)

    def tearDown(self):
        self.manager.stop()

    def test_start_registers_public_client_and_returns_auth_url(self):
        result = self.manager.start()
        self.assertEqual(set(result), {"auth_url", "state", "client_id",
                                       "callback_port"})
        self.assertEqual(result["client_id"], "cid-1234")
        self.assertEqual(result["callback_port"], self.port)
        # register：public client + PKCE required（规格 §三载荷逐键）
        register_calls = self.fake.calls_to(oauth_flow.REGISTER_URL)
        self.assertEqual(len(register_calls), 1)
        self.assertEqual(register_calls[0]["payload"], {
            "redirect_uris": [f"http://localhost:{self.port}/callback"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": "Quant Platform Workbench",
        })

    def test_auth_url_carries_pkce_and_state_params(self):
        result = self.manager.start()
        parsed = urllib.parse.urlparse(result["auth_url"])
        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
                         oauth_flow.AUTHORIZE_URL)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["cid-1234"])
        self.assertEqual(query["redirect_uri"],
                         [f"http://localhost:{self.port}/callback"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["state"], [result["state"]])
        self.assertRegex(query["code_challenge"][0], r"\A[A-Za-z0-9_\-]{43,128}\Z")
        # code_verifier 绝不进 URL
        self.assertNotIn("code_verifier", query)
        self.assertNotIn("verifier", result["auth_url"])

    def test_explicit_client_id_skips_register(self):
        result = self.manager.start(client_id="my-own-client")
        self.assertEqual(result["client_id"], "my-own-client")
        self.assertEqual(self.fake.calls_to(oauth_flow.REGISTER_URL), [])

    def test_existing_client_id_in_credential_store_is_reused(self):
        self.store.save({"client_id": "cid-registered-before"})
        result = self.manager.start()
        self.assertEqual(result["client_id"], "cid-registered-before")
        self.assertEqual(self.fake.calls_to(oauth_flow.REGISTER_URL), [])

    def test_register_failure_is_reported_without_listener(self):
        self.fake.register = {"error": "server_down"}
        with self.assertRaises(ValueError) as caught:
            self.manager.start()
        self.assertIn("注册", str(caught.exception))
        # 异常上抛由调用方呈现：start 失败不改状态位、不启 listener
        self.assertEqual(self.manager.status(),
                         {"pending": False, "done": False, "error": None,
                          "tokens_saved": False})
        self.assertEqual(self.fake.calls_to(oauth_flow.AUTHORIZE_URL), [])

    def test_status_is_pending_after_start(self):
        self.manager.start()
        self.assertEqual(self.manager.status(),
                         {"pending": True, "done": False, "error": None,
                          "tokens_saved": False})

    def test_idle_status_shape_before_any_start(self):
        self.assertEqual(self.manager.status(),
                         {"pending": False, "done": False, "error": None,
                          "tokens_saved": False})

    def test_second_start_on_same_manager_is_refused(self):
        self.manager.start()
        with self.assertRaises(ValueError):
            self.manager.start()

    def test_port_occupied_fails_fast_with_readable_error(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", self.port))
        blocker.listen(1)
        try:
            with self.assertRaises(ValueError) as caught:
                self.manager.start()
            self.assertIn("占用", str(caught.exception))
        finally:
            blocker.close()

    def test_code_verifier_never_persisted_or_exposed(self):
        result = self.manager.start()
        self.manager._on_callback("svc-code", result["state"])
        on_disk = json.loads(self.cred_path.read_text(encoding="utf-8"))
        self.assertNotIn("code_verifier", on_disk)
        self.assertNotIn("verifier", json.dumps(on_disk))
        self.assertNotIn("verifier", json.dumps(self.manager.status()))

    def test_listener_binds_loopback_only(self):
        self.manager.start()
        self.assertEqual(self.manager._server.server_address[0], "127.0.0.1")


# ================================================================ callback：换 token 落盘

class CallbackTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cred_path = self.home / "futu-openapi.json"
        self.store = fo.CredentialStore(self.cred_path)
        self.clock = Clock()
        self.fake = FakeHttp()
        self.manager = oauth_flow.OAuthFlowManager(
            self.store, str(self.home), callback_port=_free_port(),
            now=self.clock, http=self.fake)
        self.state = self.manager.start()["state"]
        self.addCleanup(self.manager.stop)

    def test_callback_exchanges_token_and_saves_credential(self):
        self.manager._on_callback("svc-code", self.state)
        token_calls = self.fake.calls_to(oauth_flow.TOKEN_URL)
        self.assertEqual(len(token_calls), 1)
        form = token_calls[0]["form"]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code"], "svc-code")
        self.assertEqual(form["client_id"], "cid-1234")
        self.assertEqual(form["redirect_uri"],
                         f"http://localhost:{self.manager.callback_port}/callback")
        self.assertRegex(form["code_verifier"], r"\A[A-Za-z0-9\-._~]{43,128}\Z")
        # 落盘：mode=oauth + token 四件套（expires_at = now + expires_in*1000）
        cred = self.store.load()
        self.assertEqual(cred["mode"], "oauth")
        self.assertEqual(cred["client_id"], "cid-1234")
        self.assertEqual(cred["access_token"], "at-1")
        self.assertEqual(cred["refresh_token"], "rt-1")
        self.assertEqual(cred["scope"], "quote:read trade:read")
        self.assertEqual(cred["expires_at"], self.clock.now_ms + 7200 * 1000)
        self.assertEqual(self.manager.status(),
                         {"pending": False, "done": True, "error": None,
                          "tokens_saved": True})

    def test_credential_file_is_0600(self):
        self.manager._on_callback("svc-code", self.state)
        self.assertEqual(os.stat(self.cred_path).st_mode & 0o777, 0o600)

    def test_expires_in_string_and_absent_refresh_token(self):
        self.fake.token = {"access_token": "at-2", "expires_in": "3600"}
        self.manager._on_callback("svc-code", self.state)
        cred = self.store.load()
        self.assertEqual(cred["expires_at"], self.clock.now_ms + 3600 * 1000)
        self.assertNotIn("refresh_token", cred)

    def test_state_mismatch_refuses_token_exchange(self):
        self.manager._on_callback("svc-code", "tampered-state")
        self.assertEqual(self.fake.calls_to(oauth_flow.TOKEN_URL), [])
        status = self.manager.status()
        self.assertFalse(status["done"])
        self.assertFalse(status["tokens_saved"])
        self.assertIn("state", status["error"])
        self.assertFalse(self.cred_path.exists(), "state 不符绝不写凭据")

    def test_error_callback_reports_denial_honestly(self):
        self.manager._on_error_callback("access_denied", "user denied")
        self.assertEqual(self.fake.calls_to(oauth_flow.TOKEN_URL), [])
        status = self.manager.status()
        self.assertFalse(status["pending"])
        self.assertFalse(status["done"])
        self.assertIn("access_denied", status["error"])
        self.assertFalse(status["tokens_saved"])

    def test_token_exchange_failure_marks_error_without_save(self):
        self.fake.token = {"error": "bad_verification_code"}
        self.manager._on_callback("svc-code", self.state)
        status = self.manager.status()
        self.assertFalse(status["tokens_saved"])
        self.assertIn("换取 token", status["error"])
        self.assertFalse(self.cred_path.exists())

    def test_transport_failure_marks_error_without_save(self):
        self.fake.fail = True
        self.manager._on_callback("svc-code", self.state)
        status = self.manager.status()
        self.assertIn("换取 token", status["error"])
        self.assertFalse(self.cred_path.exists())

    def test_oauth_save_preserves_existing_appkey_fields(self):
        self.store.save({"mode": "appkey", "app_key": "ak-live-0001",
                         "algorithm": "Ed25519",
                         "private_key_path": "/home/x/.dsh/futu-openapi-key.pem"})
        self.manager._on_callback("svc-code", self.state)
        cred = self.store.load()
        self.assertEqual(cred["mode"], "oauth")
        self.assertEqual(cred["app_key"], "ak-live-0001")
        self.assertEqual(cred["algorithm"], "Ed25519")
        self.assertEqual(cred["access_token"], "at-1")

    def test_callback_without_flow_is_ignored(self):
        fresh = oauth_flow.OAuthFlowManager(
            self.store, str(self.home), callback_port=_free_port(),
            now=self.clock, http=self.fake)
        try:
            fresh._on_callback("svc-code", "whatever")
            self.assertEqual(fresh.status(),
                             {"pending": False, "done": False, "error": None,
                              "tokens_saved": False})
        finally:
            fresh.stop()

    def test_listener_stops_after_completion(self):
        self.manager._on_callback("svc-code", self.state)
        # listener 已关闭（server_close 完成；不再断言内核 rebind——本机代理流量
        # 会在毫秒级窗口把临时端口当源端口抢走，裸 bind 探测天然竞态）
        self.assertTrue(_poll(lambda: self.manager._server is None))


# ================================================================ 超时与取消

class TimeoutAndCancelTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = fo.CredentialStore(self.home / "futu-openapi.json")
        self.clock = Clock()
        self.fake = FakeHttp()

    def _manager(self, port):
        return oauth_flow.OAuthFlowManager(
            self.store, str(self.home), callback_port=port,
            now=self.clock, http=self.fake)

    def test_timeout_marks_error_and_releases_port(self):
        manager = self._manager(_free_port())
        self.addCleanup(manager.stop)
        manager.start()
        self.clock.now_ms += 601 * 1000  # 拨过 600s 死线
        self.assertTrue(_poll(lambda: manager.status()["error"] is not None),
                        "超时后 status.error 应被 watcher 标记")
        status = manager.status()
        self.assertIn("超时", status["error"])
        self.assertFalse(status["pending"])
        self.assertFalse(status["done"])
        self.assertFalse(status["tokens_saved"])
        self.assertTrue(_poll(lambda: manager._server is None),
                        "超时后 listener 必须关闭")

    def test_within_timeout_flow_stays_pending(self):
        manager = self._manager(_free_port())
        self.addCleanup(manager.stop)
        manager.start()
        self.clock.now_ms += 599 * 1000
        time.sleep(0.5)  # 足够 watcher 跑若干圈
        self.assertIsNone(manager.status()["error"])
        self.assertTrue(manager.status()["pending"])

    def test_cancel_releases_port_and_resets_status(self):
        manager = self._manager(_free_port())
        self.addCleanup(manager.stop)
        manager.start()
        manager.cancel()
        self.assertEqual(manager.status(),
                         {"pending": False, "done": False, "error": None,
                          "tokens_saved": False})
        self.assertIsNone(manager._server, "cancel 必须停掉回调 listener")


# ================================================================ 真实 listener 集成

class ListenerIntegrationTest(unittest.TestCase):
    """127.0.0.1 真实回调：urllib 打 callback URL → 换 token → done（全离线回环）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.cred_path = self.home / "futu-openapi.json"
        self.store = fo.CredentialStore(self.cred_path)
        self.fake = FakeHttp()
        self.manager = oauth_flow.OAuthFlowManager(
            self.store, str(self.home), callback_port=_free_port(),
            now=Clock(), http=self.fake)
        self.addCleanup(self.manager.stop)

    def test_full_flow_through_real_callback_socket(self):
        result = self.manager.start()
        port = result["callback_port"]
        callback_url = (f"http://127.0.0.1:{port}/callback"
                        f"?code=svc-code&state={urllib.parse.quote(result['state'])}")
        with urllib.request.urlopen(callback_url, timeout=5) as response:
            self.assertEqual(response.status, 200)
            body = response.read().decode("utf-8")
        self.assertIn("授权", body)
        self.assertTrue(_poll(lambda: self.manager.status()["done"]),
                        "回调后 5s 内应完成换 token 并落盘")
        self.assertTrue(self.manager.status()["tokens_saved"])
        cred = self.store.load()
        self.assertEqual(cred["access_token"], "at-1")
        self.assertEqual(cred["mode"], "oauth")

    def test_denied_callback_marks_error(self):
        result = self.manager.start()
        port = result["callback_port"]
        callback_url = (f"http://127.0.0.1:{port}/callback"
                        "?error=access_denied&error_description=user+denied")
        with urllib.request.urlopen(callback_url, timeout=5):
            pass
        self.assertTrue(_poll(lambda: self.manager.status()["error"] is not None))
        self.assertIn("access_denied", self.manager.status()["error"])
        self.assertEqual(self.fake.calls_to(oauth_flow.TOKEN_URL), [])

    def test_irrelevant_probe_is_404_and_flow_untouched(self):
        result = self.manager.start()
        port = result["callback_port"]
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
        self.assertEqual(caught.exception.code, 404)
        time.sleep(0.5)
        self.assertTrue(self.manager.status()["pending"])

    def test_state_mismatch_through_real_socket_refuses_token(self):
        result = self.manager.start()
        port = result["callback_port"]
        callback_url = f"http://127.0.0.1:{port}/callback?code=evil&state=evil"
        with urllib.request.urlopen(callback_url, timeout=5):
            pass
        self.assertTrue(_poll(lambda: self.manager.status()["error"] is not None))
        self.assertIn("state", self.manager.status()["error"])
        self.assertEqual(self.fake.calls_to(oauth_flow.TOKEN_URL), [])


# ================================================================ 路由面（app.py）

class OAuthRouteTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        oauth_flow.shutdown_all()
        self.addCleanup(oauth_flow.shutdown_all)
        app = app_module.create_app(home=str(self.home),
                                    dist=str(self.home / "dist-missing"),
                                    scheduler=IdleScheduler())
        self.client = TestClient(app)

    def post(self, payload):
        return self.client.post("/api/wb/openapi_oauth", json=payload)

    def test_status_without_flow_reports_idle(self):
        body = self.post({"action": "status"}).json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["value"], {"pending": False, "done": False,
                                         "error": None, "tokens_saved": False})

    def test_start_status_cancel_round_trip_via_route(self):
        fake = FakeHttp()
        port = _free_port()
        with unittest.mock.patch.object(oauth_flow, "default_http", fake), \
                unittest.mock.patch.object(oauth_flow, "DEFAULT_CALLBACK_PORT", port):
            started = self.post({"action": "start"}).json()
            self.assertTrue(started["ok"], started)
            self.assertEqual(started["value"]["callback_port"], port)
            self.assertIn("code_challenge_method=S256", started["value"]["auth_url"])
            pending = self.post({"action": "status"}).json()["value"]
            self.assertTrue(pending["pending"])
            cancelled = self.post({"action": "cancel"}).json()
            self.assertTrue(cancelled["ok"])
        idle = self.post({"action": "status"}).json()["value"]
        self.assertEqual(idle, {"pending": False, "done": False, "error": None,
                                "tokens_saved": False})
        # cancel 后注册表已清（无残留 listener/manager；token 认证面看 OAuthRouteTest）
        self.assertIsNone(oauth_flow._manager_for(str(self.home)))

    def test_invalid_action_is_business_failure_envelope(self):
        body = self.post({"action": "boom"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")

    def test_missing_action_is_business_failure_envelope(self):
        body = self.post({}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")

    def test_unknown_payload_field_is_rejected(self):
        body = self.post({"action": "status", "scope": "evil"}).json()
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")

    def test_route_is_token_protected(self):
        (self.home / "trading-platform.json").write_text(json.dumps(
            {"service": {"token": "s3cret"}}, ensure_ascii=False), encoding="utf-8")
        app = app_module.create_app(home=str(self.home),
                                    dist=str(self.home / "dist-missing"),
                                    scheduler=IdleScheduler())
        with TestClient(app) as client:
            self.assertEqual(
                client.post("/api/wb/openapi_oauth", json={"action": "status"}
                            ).status_code, 401)
            authorized = client.post(
                "/api/wb/openapi_oauth", json={"action": "status"},
                headers={"Authorization": "Bearer s3cret"})
            self.assertEqual(authorized.status_code, 200)

    def test_endpoint_declared_but_excluded_from_mcp(self):
        self.assertIn("openapi_oauth", store_access.endpoints())
        declared = self.client.post("/api/wb/snapshot", json={}).json(
            )["value"]["endpoints"]
        self.assertIn("openapi_oauth", declared)
        self.assertIn("openapi_oauth", mcp_tools.MCP_EXCLUDED_ENDPOINTS)
        names = {definition.name for definition in mcp_tools.TOOLS}
        self.assertNotIn("openapi_oauth", names)


# ================================================================ AppKey 并存不受影响

class CoexistenceTest(unittest.TestCase):
    """两种模式并存：OAuth 落盘不动 AppKey 字段；AppKey 保存路径零改动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        oauth_flow.shutdown_all()
        self.addCleanup(oauth_flow.shutdown_all)

    def test_oauth_flow_writes_oauth_mode_and_status_reflects_it(self):
        store = fo.CredentialStore(self.home / "futu-openapi.json")
        manager = oauth_flow.OAuthFlowManager(
            store, str(self.home), callback_port=_free_port(),
            now=Clock(), http=FakeHttp())
        self.addCleanup(manager.stop)
        state = manager.start()["state"]
        manager._on_callback("svc-code", state)
        status = settings_api.get_config_status(str(self.home))
        self.assertTrue(status["configured"])
        self.assertEqual(status["mode"], "oauth")
        self.assertTrue(status["ready"])

    def test_appkey_save_still_works_after_oauth_mode_saved(self):
        # 先落一把真实 Ed25519 私钥供 save_config 的 from_path 校验
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
            Ed25519PrivateKey)
        pem = Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode("ascii")
        (self.home / "k.pem").write_text(pem, encoding="ascii")
        settings_api.save_config(str(self.home), {
            "mode": "appkey", "app_key": "ak-live-0001", "algorithm": "Ed25519",
            "private_key_path": str(self.home / "k.pem")})
        # OAuth 授权完成（mock 回调）→ mode 切 oauth，AppKey 字段保留
        store = fo.CredentialStore(self.home / "futu-openapi.json")
        manager = oauth_flow.OAuthFlowManager(
            store, str(self.home), callback_port=_free_port(),
            now=Clock(), http=FakeHttp())
        self.addCleanup(manager.stop)
        state = manager.start()["state"]
        manager._on_callback("svc-code", state)
        cred = store.load()
        self.assertEqual(cred["mode"], "oauth")
        self.assertEqual(cred["app_key"], "ak-live-0001")
        # oauth 模式携带 app_key 字段仍被 save_config 拒绝（设置页只保存 AppKey，不变式）
        with self.assertRaises(WorkbenchError):
            settings_api.save_config(str(self.home), {"mode": "oauth"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
