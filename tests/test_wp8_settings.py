"""WP8 任务 7：富途 OpenAPI 凭据配置 UI（Web 设置页）的服务端面。

全部离线（连通性测试用注入传输 mock http）。覆盖：

  * ``GET /api/wb/openapi_config``：未配置 → configured:false / ready:false；
    已配置 → configured/mode/app_key_masked/algorithm/private_key_exists/
    private_key_fingerprint/channel/ready 全字段；
  * POST 保存（粘贴 PEM 模式）：私钥落 ``<home>/futu-openapi-key.pem``（0600 原子写）、
    凭据 JSON（mode/app_key/algorithm/private_key_path）正确、GET 立即 configured:true
    且 ``futu_data.openapi_ready()`` 为 True（保存后立即可用，不变式 3）；
  * POST 保存（文件路径模式）：路径落盘；``~`` 展开；文件缺失拒绝；
  * 校验面：缺 app_key / 坏 PEM / PEM 与算法不匹配 / 非法 channel / mode=oauth →
    ``trading/invalid-operation`` 业务失败信封（仓库口径：业务失败一律 200 + 信封，
    与其他端点同形；HTTP 400 仅留给路由层的坏 JSON/非对象体）；
  * POST ``openapi_test``：注入传输（mock http）→ ret_code 0 + http_status +
    latency_ms + 签名头断言（真实调用路径：OpenApiClient + OpenApiMarket.trading-days）；
    未配置凭据 → ``trading/openapi-unavailable``（零网络）；
  * 安全不变式：**私钥 PEM 原文绝不出现在任何 GET/POST 响应中**（响应全文 grep 断言）；
    GET 响应同样不含完整 app_key（只有掩码）；
  * 声明面：openapi_config / openapi_test 进 ``store_access.endpoints()``（snapshot
    声明，前端 callApi 预检据此放行），但**有意排除在 MCP 工具面之外**——凭据写入是
    人工 Web 动作，绝不做成模型工具（与 confirm-decide 同类）；
  * channel 联动：保存时更新 trading-platform.json 顶层 futu_channel。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402
from server import config as server_config  # noqa: E402
from server import futu_data, store_access  # noqa: E402
from server import settings_api  # noqa: E402
from trading_datasource import futu_openapi as fo  # noqa: E402


def _ed25519_pem_text():
    key = Ed25519PrivateKey.generate()
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")


def _rsa_pem_text():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")


class IdleScheduler:
    """create_app(scheduler=...) 的替身：start/stop 只记账（零线程）。"""

    def __init__(self):
        self.alive = False
        self.last_error = None
        self.calls = []

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


class RecordingHttp:
    """注入传输：记录请求，返回脚本化 trading-days 成功信封（mock http）。"""

    def __init__(self, response=(200, b'{"ret_code":0,"ret_msg":"success",'
                                 b'"data":{"trading_days":["2026-09-16"]}}')):
        self.requests = []
        self.response = response

    def __call__(self, method, url, headers, body):
        self.requests.append({"method": method, "url": url, "headers": headers,
                              "body": body})
        status, resp_body = self.response
        return status, resp_body, {}


class SettingsRouteTest(unittest.TestCase):
    """路由层（TestClient + 临时 home）：GET/POST 信封与落盘效果。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        app = app_module.create_app(home=str(self.home),
                                    dist=str(self.home / "dist-missing"),
                                    scheduler=IdleScheduler())
        self.client = TestClient(app)

    def post(self, endpoint, payload):
        return self.client.post(f"/api/wb/{endpoint}", json=payload)

    # ---- GET 读状态 -------------------------------------------------------

    def test_get_unconfigured_reports_false_everywhere(self):
        body = self.client.get("/api/wb/openapi_config")
        self.assertEqual(body.status_code, 200)
        value = body.json()["value"]
        self.assertTrue(body.json()["ok"])
        self.assertFalse(value["configured"])
        self.assertFalse(value["ready"])
        self.assertIsNone(value["mode"])
        self.assertIsNone(value["app_key_masked"])
        self.assertIsNone(value["private_key_fingerprint"])
        self.assertFalse(value["private_key_exists"])
        self.assertEqual(value["channel"], "mcp")  # 缺省通道
        self.assertIsNone(value["last_error"])

    def test_post_empty_payload_reads_status_like_call_api(self):
        """前端 callApi 一律 POST：空载荷 = 读状态（与 GET 同实现）。"""
        body = self.post("openapi_config", {})
        self.assertTrue(body.json()["ok"], body.json())
        self.assertIn("configured", body.json()["value"])

    def test_get_route_is_token_protected(self):
        """token 认证在 /api/* 中间件：配置错 token 后 GET 必须 401。"""
        home = self.home
        (home / "trading-platform.json").write_text(json.dumps(
            {"service": {"token": "s3cret"}}, ensure_ascii=False), encoding="utf-8")
        app = app_module.create_app(home=str(home), dist=str(home / "dist-missing"),
                                    scheduler=IdleScheduler())
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/wb/openapi_config").status_code, 401)
            authorized = client.get("/api/wb/openapi_config",
                                    headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(authorized.status_code, 200)
        self.assertTrue(authorized.json()["ok"])

    # ---- POST 保存（粘贴 PEM 模式） ---------------------------------------

    def test_save_with_pem_writes_key_0600_credentials_and_channel(self):
        pem = _ed25519_pem_text()
        resp = self.post("openapi_config", {
            "mode": "appkey", "app_key": "0be2eb1122334455", "algorithm": "Ed25519",
            "private_key_pem": pem, "channel": "openapi"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"], body)
        # 私钥文件：<home>/futu-openapi-key.pem，0600
        key_file = self.home / "futu-openapi-key.pem"
        self.assertTrue(key_file.is_file())
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
        # 凭据 JSON 字段正确
        cred = json.loads((self.home / "futu-openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(cred["mode"], "appkey")
        self.assertEqual(cred["app_key"], "0be2eb1122334455")
        self.assertEqual(cred["algorithm"], "Ed25519")
        self.assertEqual(cred["private_key_path"], str(key_file))
        # channel 联动 trading-platform.json 顶层 futu_channel
        cfg = json.loads((self.home / "trading-platform.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["futu_channel"], "openapi")
        # 保存响应即状态（同 GET 形状）且绝不回显 PEM 原文
        self.assertTrue(body["value"]["configured"])
        self.assertNotIn("PRIVATE KEY", resp.text)
        # GET：configured:true + 全字段
        fetched = self.client.get("/api/wb/openapi_config")
        status = fetched.json()["value"]
        self.assertTrue(status["configured"])
        self.assertEqual(status["mode"], "appkey")
        self.assertEqual(status["algorithm"], "Ed25519")
        self.assertEqual(status["channel"], "openapi")
        self.assertEqual(status["app_key_masked"], "0be2****4455")
        self.assertTrue(status["private_key_exists"])
        self.assertRegex(status["private_key_fingerprint"], r"^[0-9a-f]{16}$")
        # 保存后立即可用（不变式 3）
        self.assertTrue(status["ready"])
        self.assertTrue(futu_data.openapi_ready(self.home / "futu-openapi.json"))
        # 安全不变式 1：私钥 PEM 原文与完整 app_key 绝不出现在 GET 响应
        self.assertNotIn("PRIVATE KEY", fetched.text)
        self.assertNotIn("0be2eb1122334455", fetched.text)

    def test_save_with_path_mode_stores_expanded_path(self):
        key_file = self.home / "my-key.pem"
        key_file.write_text(_ed25519_pem_text(), encoding="ascii")
        key_file.chmod(0o600)
        resp = self.post("openapi_config", {
            "mode": "appkey", "app_key": "appkey-12345678",
            "algorithm": "Ed25519", "private_key_path": str(key_file)})
        self.assertTrue(resp.json()["ok"], resp.text)
        cred = json.loads((self.home / "futu-openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(cred["private_key_path"], str(key_file))
        self.assertFalse((self.home / "futu-openapi-key.pem").exists(),
                         "path 模式不另写默认私钥文件")
        self.assertTrue(resp.json()["value"]["ready"])

    def test_save_with_tilde_path_expands_home(self):
        key_file = self.home / "tilde-key.pem"
        key_file.write_text(_ed25519_pem_text(), encoding="ascii")
        key_file.chmod(0o600)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            resp = self.post("openapi_config", {
                "mode": "appkey", "app_key": "appkey-12345678",
                "algorithm": "Ed25519", "private_key_path": "~/tilde-key.pem"})
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
        self.assertTrue(resp.json()["ok"], resp.text)
        cred = json.loads((self.home / "futu-openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(cred["private_key_path"], str(key_file))

    def test_save_with_missing_key_file_rejected(self):
        resp = self.post("openapi_config", {
            "mode": "appkey", "app_key": "appkey-12345678",
            "algorithm": "Ed25519", "private_key_path": str(self.home / "nope.pem")})
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertFalse((self.home / "futu-openapi.json").exists(),
                         "校验失败绝不写凭据文件")

    # ---- 校验面 -----------------------------------------------------------

    def test_save_without_app_key_rejected(self):
        resp = self.post("openapi_config", {
            "mode": "appkey", "algorithm": "Ed25519",
            "private_key_pem": _ed25519_pem_text()})
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("app_key", body["error"]["message"])
        self.assertFalse((self.home / "futu-openapi.json").exists())

    def test_save_without_any_key_material_rejected(self):
        resp = self.post("openapi_config", {"mode": "appkey", "app_key": "ak-0001"})
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")

    def test_save_with_bad_pem_rejected(self):
        resp = self.post("openapi_config", {
            "mode": "appkey", "app_key": "ak-0001", "algorithm": "Ed25519",
            "private_key_pem": "这不是 PEM"})
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("PEM", body["error"]["message"])
        self.assertFalse((self.home / "futu-openapi.json").exists())

    def test_save_with_algorithm_mismatch_rejected_then_switch_updates(self):
        # Ed25519 私钥配 RSA-SHA256：构造 AppKeySigner 即拒（类型不符）
        resp = self.post("openapi_config", {
            "mode": "appkey", "app_key": "ak-0001", "algorithm": "RSA-SHA256",
            "private_key_pem": _ed25519_pem_text()})
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        # 换算法：RSA 私钥 + RSA-SHA256 保存成功，algorithm 更新
        resp = self.post("openapi_config", {
            "mode": "appkey", "app_key": "ak-0001", "algorithm": "RSA-SHA256",
            "private_key_pem": _rsa_pem_text()})
        self.assertTrue(resp.json()["ok"], resp.text)
        cred = json.loads((self.home / "futu-openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(cred["algorithm"], "RSA-SHA256")
        self.assertEqual(resp.json()["value"]["algorithm"], "RSA-SHA256")

    def test_save_with_unknown_field_rejected(self):
        resp = self.post("openapi_config", {"app_key": "ak", "bogus": 1})
        self.assertEqual(resp.json()["error"]["code"], "trading/invalid-operation")

    def test_save_oauth_mode_rejected_with_pointer_to_auth_script(self):
        resp = self.post("openapi_config", {"mode": "oauth"})
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("futu_auth", body["error"]["message"])

    # ---- openapi_test（连通性） -------------------------------------------

    def test_test_endpoint_without_credentials_fails_honestly(self):
        resp = self.post("openapi_test", {})
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/openapi-unavailable")

    def test_test_endpoint_rejects_any_payload(self):
        resp = self.post("openapi_test", {"market": "HK"})
        self.assertEqual(resp.json()["error"]["code"], "trading/invalid-operation")

    def test_saved_credentials_drive_real_call_path_with_mock_http(self):
        """mock 凭据 → 真实调用路径（OpenApiClient+OpenApiMarket，注入 http）→ ret_code 0。"""
        settings_api.save_config(str(self.home), {
            "mode": "appkey", "app_key": "ak-live-0001", "algorithm": "Ed25519",
            "private_key_pem": _ed25519_pem_text()})
        fake = RecordingHttp()
        envelope = settings_api.test_connectivity(str(self.home), http=fake)
        self.assertTrue(envelope["ok"], envelope)
        value = envelope["value"]
        self.assertEqual(value["http_status"], 200)
        self.assertEqual(value["ret_code"], 0)
        self.assertEqual(value["ret_msg"], "success")
        self.assertIsInstance(value["latency_ms"], int)
        self.assertEqual(value["data"], {"trading_days": ["2026-09-16"]})
        # 真实调用路径：trading-days GET + AppKey 签名头（X-Api-Key=已保存凭据）
        self.assertEqual(len(fake.requests), 1)
        request = fake.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertIn("/api/v1.0/quote/trading-days", request["url"])
        self.assertEqual(request["headers"]["X-Api-Key"], "ak-live-0001")
        self.assertTrue(request["headers"]["Authorization"])

    def test_test_endpoint_failure_keeps_message_short(self):
        settings_api.save_config(str(self.home), {
            "mode": "appkey", "app_key": "ak-live-0001", "algorithm": "Ed25519",
            "private_key_pem": _ed25519_pem_text()})

        def broken(method, url, headers, body):
            raise ConnectionError("connection reset by peer")

        envelope = settings_api.test_connectivity(str(self.home), http=broken)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error"]["code"], "trading/openapi-unavailable")
        self.assertLessEqual(len(envelope["error"]["message"]), 300)

    # ---- 声明面 -----------------------------------------------------------

    def test_settings_endpoints_declared_but_excluded_from_mcp(self):
        self.assertIn("openapi_config", store_access.endpoints())
        self.assertIn("openapi_test", store_access.endpoints())
        # snapshot 向前端声明（callApi 预检据此放行）
        with TestClient(app_module.create_app(
                home=str(self.home), dist=str(self.home / "dist-missing"),
                scheduler=IdleScheduler())) as client:
            declared = client.post("/api/wb/snapshot", json={}).json()["value"]["endpoints"]
        self.assertIn("openapi_config", declared)
        self.assertIn("openapi_test", declared)
        from server import mcp_tools
        self.assertIn("openapi_config", mcp_tools.MCP_EXCLUDED_ENDPOINTS)
        self.assertIn("openapi_test", mcp_tools.MCP_EXCLUDED_ENDPOINTS)
        names = {definition.name for definition in mcp_tools.TOOLS}
        self.assertNotIn("openapi_config", names)
        self.assertNotIn("openapi_test", names)


class StatusShapeTest(unittest.TestCase):
    """get_config_status 的字段口径（不經路由直读）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def write_cred(self, data):
        (self.home / "futu-openapi.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_corrupt_credential_file_reports_last_error_not_crash(self):
        (self.home / "futu-openapi.json").write_text("{broken", encoding="utf-8")
        status = settings_api.get_config_status(str(self.home))
        self.assertFalse(status["configured"])
        self.assertFalse(status["ready"])
        self.assertIn("JSON", status["last_error"])

    def test_short_app_key_is_masked_entirely(self):
        self.write_cred({"mode": "appkey", "app_key": "abc"})
        status = settings_api.get_config_status(str(self.home))
        self.assertEqual(status["app_key_masked"], "****")

    def test_oauth_mode_counts_as_configured(self):
        self.write_cred({"mode": "oauth", "access_token": "t", "expires_at": 1})
        status = settings_api.get_config_status(str(self.home))
        self.assertTrue(status["configured"])
        self.assertEqual(status["mode"], "oauth")
        self.assertTrue(status["ready"])

    def test_missing_private_key_file_reports_exists_false(self):
        self.write_cred({"mode": "appkey", "app_key": "ak-0001",
                         "private_key_path": str(self.home / "gone.pem"),
                         "algorithm": "Ed25519"})
        status = settings_api.get_config_status(str(self.home))
        self.assertFalse(status["private_key_exists"])
        self.assertIsNone(status["private_key_fingerprint"])
        self.assertFalse(status["ready"])


class UnitLevelTest(unittest.TestCase):
    """write_private_key / key_fingerprint / save_futu_channel 单元口径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_write_private_key_is_0600_and_canonical(self):
        path = Path(settings_api.write_private_key(str(self.home), _ed25519_pem_text()))
        self.assertEqual(path, self.home / "futu-openapi-key.pem")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # 落盘的是可加载的规范 PKCS8 PEM
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        self.assertIsInstance(key, Ed25519PrivateKey)

    def test_write_private_key_rejects_bad_pem(self):
        with self.assertRaises(ValueError):
            settings_api.write_private_key(str(self.home), "-----BEGIN X-----")
        self.assertFalse((self.home / "futu-openapi-key.pem").exists())

    def test_fingerprint_is_16_hex_and_differs_across_keys(self):
        first = settings_api.write_private_key(str(self.home), _ed25519_pem_text())
        fp = settings_api.key_fingerprint(first)
        self.assertRegex(fp, r"^[0-9a-f]{16}$")
        second = self.home / "second.pem"
        second.write_text(_ed25519_pem_text(), encoding="ascii")
        self.assertNotEqual(fp, settings_api.key_fingerprint(str(second)))

    def test_fingerprint_missing_file_is_none(self):
        self.assertIsNone(settings_api.key_fingerprint(str(self.home / "gone.pem")))

    def test_save_futu_channel_round_trip_and_validation(self):
        cfg_path = self.home / "trading-platform.json"
        cfg_path.write_text(json.dumps({"service": {"token": "t", "port": 9001}}),
                            encoding="utf-8")
        channel = server_config.save_futu_channel(str(self.home), "openapi")
        self.assertEqual(channel, "openapi")
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(data["futu_channel"], "openapi")
        self.assertEqual(data["service"], {"token": "t", "port": 9001},
                         "已有键原样保留")
        with self.assertRaises(ValueError):
            server_config.save_futu_channel(str(self.home), "both")
        self.assertEqual(json.loads(cfg_path.read_text(encoding="utf-8"))["futu_channel"],
                         "openapi", "校验失败不写盘")

    def test_save_futu_channel_creates_file_with_0600(self):
        channel = server_config.save_futu_channel(str(self.home), "mcp")
        self.assertEqual(channel, "mcp")
        path = self.home / "trading-platform.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_save_config_rejects_invalid_channel_before_any_write(self):
        with self.assertRaises(Exception):
            settings_api.save_config(str(self.home), {
                "mode": "appkey", "app_key": "ak-0001", "algorithm": "Ed25519",
                "private_key_pem": _ed25519_pem_text(), "channel": "both"})
        self.assertFalse((self.home / "futu-openapi.json").exists(),
                         "channel 非法时凭据也不写（先校验后落盘）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
