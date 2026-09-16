"""富途 OpenAPI 连通性自检脚本（scripts/futu_openapi_check.py）离线测试。

用临时生成的 Ed25519 密钥 + 假传输：断言脚本构造的请求签名可被公钥验签、
空 body 的第 5 段为空串、错误路径退出码正确。全程离线。
"""
import base64
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from trading_datasource import futu_openapi as fo  # noqa: E402

spec = importlib.util.spec_from_file_location("futu_openapi_check", ROOT / "scripts" / "futu_openapi_check.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def write_key(tmp: Path) -> Path:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    path = tmp / "key.pem"
    path.write_bytes(pem)
    path.chmod(0o600)
    return path


class RecordingTransport:
    """记录请求；用请求里的公钥验签 Authorization，并核对签名原文。"""

    def __init__(self, response=(200, b'{"ret_code":0,"data":{"ok":1}}')):
        self.requests = []
        self.response = response

    def __call__(self, request, timeout):
        self.requests.append(request)
        status, body = self.response
        return status, body, {}


class CheckScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.key_path = write_key(self.tmp_path)
        self.pub = serialization.load_pem_private_key(
            self.key_path.read_bytes(), password=None).public_key()

    def test_get_request_is_correctly_signed_with_empty_body_segment(self):
        transport = RecordingTransport()
        code = check.main(["--app-key", "AK-TEST", "--key-file", str(self.key_path)],
                          transport=transport)
        self.assertEqual(code, 0)
        request = transport.requests[0]
        self.assertEqual(request["method"], "GET")
        self.assertIsNone(request["body"])
        self.assertEqual(request["headers"]["X-Api-Key"], "AK-TEST")
        self.assertNotIn("Bearer", request["headers"]["Authorization"])
        # 用同一公钥重算签名原文并验签（证明脚本的签名可被服务端验签）
        message = "\n".join([
            request["headers"]["X-Timestamp"], "GET", "/api/v1.0/quote/trading-days",
            request["url"].split("?", 1)[1], ""]).encode()
        self.pub.verify(base64.b64decode(request["headers"]["Authorization"]), message)

    def test_post_body_segment_is_sha256(self):
        transport = RecordingTransport()
        body = '{"code_list":["HK.00700"]}'
        code = check.main(["--app-key", "AK", "--key-file", str(self.key_path),
                           "--method", "POST", "--path", "/api/v1.0/quote/snapshot",
                           "--query", "", "--body", body], transport=transport)
        self.assertEqual(code, 0)
        request = transport.requests[0]
        self.assertEqual(request["body"], body.encode())
        message = "\n".join([request["headers"]["X-Timestamp"], "POST",
                             "/api/v1.0/quote/snapshot", "", __import__("hashlib").sha256(
                                 body.encode()).hexdigest()]).encode()
        self.pub.verify(base64.b64decode(request["headers"]["Authorization"]), message)

    def test_private_key_missing_exit_2(self):
        out = io.StringIO()
        stdout, sys.stdout = sys.stdout, out
        try:
            code = check.main(["--app-key", "AK", "--key-file", str(self.tmp_path / "nope.pem")],
                              transport=RecordingTransport())
        finally:
            sys.stdout = stdout
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())["stage"], "private-key")

    def test_transport_error_exit_1(self):
        def boom(request, timeout):
            raise OSError("connection reset")

        out = io.StringIO()
        stdout, sys.stdout = sys.stdout, out
        try:
            code = check.main(["--app-key", "AK", "--key-file", str(self.key_path)], transport=boom)
        finally:
            sys.stdout = stdout
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["stage"], "transport")

    def test_business_error_envelope_exit_1(self):
        transport = RecordingTransport(response=(401, json.dumps(
            {"code": -12006, "trace_id": "x"}).encode()))
        out = io.StringIO()
        stdout, sys.stdout = sys.stdout, out
        try:
            code = check.main(["--app-key", "AK", "--key-file", str(self.key_path)],
                              transport=transport)
        finally:
            sys.stdout = stdout
        self.assertEqual(code, 1)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["http_status"], 401)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
