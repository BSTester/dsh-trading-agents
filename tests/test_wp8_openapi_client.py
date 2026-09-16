"""WP8 任务 1：富途 OpenAPI 客户端与认证——全部离线（HTTP 传输 mock 注入）。

* PKCE S256：RFC 7636 附录 B 官方向量；
* AppKey 签名：签名原文五段逐字断言 + cryptography 生成密钥并公钥验签（Ed25519/RSA-SHA256）；
* OAuth 全流程（mock）：Bearer 头、expires_at 前 60s 主动刷新一次并持久化、
  401 触发刷新重试一次、刷新失败抛 OpenApiError、无 refresh_token 如实报错；
* 信封：s==ok 透传 d；s==error 抛 OpenApiError（保留 need_order_confirm/confirm_id/jump_url）；
  限频/5xx 不自动重试、如实抛出（调用次数锁定为 1）；
* 凭据：0600 原子读写、坏 JSON 抛错、路径可覆盖（测试隔离）；
* scripts/futu_auth.py --openapi 纯函数：PKCE/state 生成、授权 URL、token 交换、
  凭据合并、state 校验。

真实网络流程（/oauth2/register、浏览器授权页、60355 回调、token 端点实连）
**不自动化测试**，属人工流程：python scripts/futu_auth.py --openapi。
"""
import base64
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "plugins" / "datasource" / "python"))

from trading_datasource import futu_openapi as fo  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "futu_auth_under_test", _REPO / "scripts" / "futu_auth.py")
futu_auth = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(futu_auth)

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _pem(key):
    """cryptography 私钥 → PKCS8 PEM（Ed25519/RSA 通用）。"""
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _oauth_cred(now_ms, **over):
    cred = {"mode": "oauth", "client_id": "cid", "access_token": "tok",
            "refresh_token": "rtok", "expires_at": now_ms + 7_200_000,
            "scope": "quote:read"}
    cred.update(over)
    return cred


class RecordingHttp:
    """注入用假传输：记录每次调用，按序回放 (status, body) 响应。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers=None, body=None):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers or {}), "body": body})
        if not self.responses:
            raise AssertionError("意外的多余请求: " + url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, payload = item
        if isinstance(payload, bytes):
            return status, payload
        return status, json.dumps(payload).encode("utf-8")


class TempHomeTestBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)


class PkceTest(unittest.TestCase):
    RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"

    def test_rfc7636_appendix_b_vector(self):
        """RFC 7636 附录 B 官方向量逐字对齐。"""
        self.assertEqual(fo.Pkce.challenge(self.RFC_VERIFIER), self.RFC_CHALLENGE)

    def test_challenge_is_unpadded_base64url_of_sha256(self):
        verifier = "another-verifier-value-42"
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        self.assertEqual(fo.Pkce.challenge(verifier), expected)
        self.assertNotIn("=", expected, "S256 challenge 不得带 padding")

    def test_code_verifier_shape(self):
        for _ in range(5):
            v = fo.Pkce.code_verifier()
            self.assertGreaterEqual(len(v), 43)
            self.assertLessEqual(len(v), 128)
            self.assertRegex(v, r"\A[A-Za-z0-9\-._~]+\Z")


class AppKeySignerTest(TempHomeTestBase):
    def test_signing_message_is_five_newline_segments(self):
        """签名原文五段逐字：timestamp_ms\\nMETHOD(大写)\\npath\\nquery\\nsha256(body)hex。"""
        body = b'{"qty":100}'
        msg = fo.AppKeySigner.signing_message(
            1690000000123, "post", "/v4/trade/place", "a=1", body)
        self.assertEqual(msg.decode("utf-8").split("\n"),
                         ["1690000000123", "POST", "/v4/trade/place", "a=1",
                          hashlib.sha256(body).hexdigest()])

    def test_empty_body_uses_sha256_of_empty_bytes(self):
        msg = fo.AppKeySigner.signing_message(1, "GET", "/v4/x", "", None)
        self.assertEqual(msg.decode("utf-8").split("\n")[4], EMPTY_SHA256)
        self.assertEqual(fo.EMPTY_BODY_SHA256, EMPTY_SHA256)

    def test_empty_fields_keep_position(self):
        """空 query/空 body 段保留位置（连续 \\n 不收缩）。"""
        msg = fo.AppKeySigner.signing_message(7, "GET", "/v4/x", None, b"").decode("utf-8")
        parts = msg.split("\n")
        self.assertEqual(len(parts), 5)
        self.assertEqual(parts[3], "")
        self.assertEqual(parts[4], EMPTY_SHA256)

    def test_ed25519_signature_verifies_with_public_key(self):
        key = Ed25519PrivateKey.generate()
        signer = fo.AppKeySigner(key, "Ed25519")
        body = b"payload-bytes"
        sig_b64 = signer.sign(1700000000000, "POST", "/v4/trade/place", "k=v", body)
        message = fo.AppKeySigner.signing_message(
            1700000000000, "POST", "/v4/trade/place", "k=v", body)
        key.public_key().verify(base64.b64decode(sig_b64), message)  # 不抛即通过
        with self.assertRaises(InvalidSignature):
            key.public_key().verify(base64.b64decode(sig_b64), message + b"x")

    def test_rsa_sha256_signature_verifies_pkcs1v15(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signer = fo.AppKeySigner(key, "RSA-SHA256")
        body = b"payload-bytes"
        sig_b64 = signer.sign(1700000000000, "POST", "/v4/trade/place", "k=v", body)
        message = fo.AppKeySigner.signing_message(
            1700000000000, "POST", "/v4/trade/place", "k=v", body)
        key.public_key().verify(base64.b64decode(sig_b64), message,
                                padding.PKCS1v15(), hashes.SHA256())

    def test_nonce_matches_official_charset_and_length(self):
        for length in (1, 32, 64):
            self.assertRegex(fo.AppKeySigner.nonce(length), r"\A[A-Za-z0-9_-]{1,64}\Z")
            self.assertEqual(len(fo.AppKeySigner.nonce(length)), length)
        for bad in (0, 65):
            with self.assertRaises(ValueError):
                fo.AppKeySigner.nonce(bad)

    def test_from_path_loads_pem_and_signs(self):
        key = Ed25519PrivateKey.generate()
        path = self.tmp / "ed.pem"
        path.write_bytes(_pem(key))
        signer = fo.AppKeySigner.from_path(path, "Ed25519")
        message = fo.AppKeySigner.signing_message(1, "GET", "/v4/x", "", b"")
        key.public_key().verify(base64.b64decode(signer.sign(1, "GET", "/v4/x", "", b"")),
                                message)

    def test_algorithm_key_mismatch_raises(self):
        ed = Ed25519PrivateKey.generate()
        rk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self.assertRaises(ValueError):
            fo.AppKeySigner(rk, "Ed25519")
        with self.assertRaises(ValueError):
            fo.AppKeySigner(ed, "RSA-SHA256")
        with self.assertRaises(ValueError):
            fo.AppKeySigner(ed, "HMAC-XYZ")


class CredentialStoreTest(TempHomeTestBase):
    def test_save_load_roundtrip_0600_and_no_temp_leftovers(self):
        path = self.tmp / "futu-openapi.json"
        store = fo.CredentialStore(path)
        store.save({"mode": "oauth", "access_token": "t1", "expires_at": 1})
        self.assertEqual(store.load(), {"mode": "oauth", "access_token": "t1",
                                        "expires_at": 1})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        store.save({"mode": "oauth", "access_token": "t2"})
        self.assertEqual(store.load()["access_token"], "t2")
        self.assertEqual(os.listdir(self.tmp), ["futu-openapi.json"],
                         "原子写不得残留临时文件")

    def test_load_missing_returns_empty_dict(self):
        self.assertEqual(fo.CredentialStore(self.tmp / "nope.json").load(), {})

    def test_load_bad_json_raises_value_error(self):
        path = self.tmp / "futu-openapi.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            fo.CredentialStore(path).load()

    def test_default_path_follows_dsh_home(self):
        expected = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser() \
            / "futu-openapi.json"
        self.assertEqual(fo.CredentialStore().path, expected)


class OAuthClientTest(TempHomeTestBase):
    NOW_S = 1_700_000_000.0
    NOW_MS = 1_700_000_000_000

    def _client(self, cred_over, responses=()):
        store = fo.CredentialStore(self.tmp / "cred.json")
        store.save(_oauth_cred(self.NOW_MS, **cred_over))
        http = RecordingHttp(*responses)
        client = fo.OpenApiClient(store, http=http, now=lambda: self.NOW_S)
        return client, http, store

    def test_ok_envelope_passes_d_with_bearer(self):
        client, http, _ = self._client({}, [(200, {"s": "ok", "d": {"k": 1}})])
        self.assertEqual(client.request("GET", "/v4/market-snapshot"), {"k": 1})
        call = http.calls[0]
        self.assertEqual(call["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(call["url"], "https://webapi.futunn.com/v4/market-snapshot")
        self.assertEqual(len(http.calls), 1, "token 新鲜时不得触发刷新")

    def test_error_envelope_raises_openapi_error_with_confirm_fields(self):
        resp = {"s": "error", "errcode": 123, "errmsg": "需要二次确认",
                "need_order_confirm": True, "confirm_id": "cf-1",
                "jump_url": "https://www.futunn.com/confirm"}
        client, _, _ = self._client({}, [(200, resp)])
        with self.assertRaises(fo.OpenApiError) as cm:
            client.request("POST", "/v4/trade/place", json_body={"qty": 1})
        err = cm.exception
        self.assertEqual(err.errcode, 123)
        self.assertEqual(err.errmsg, "需要二次确认")
        self.assertIs(err.need_order_confirm, True)
        self.assertEqual(err.confirm_id, "cf-1")
        self.assertEqual(err.jump_url, "https://www.futunn.com/confirm")

    def test_pre_expiry_refreshes_once_persists_then_calls(self):
        """expires_at 前 60s：先刷新一次（持久化），再带新 token 调业务接口。"""
        client, http, store = self._client(
            {"expires_at": self.NOW_MS + 30_000},
            [(200, {"access_token": "tok2", "expires_in": 7200,
                    "refresh_token": "rtok2", "scope": "s2"}),
             (200, {"s": "ok", "d": {"snap": 1}})])
        self.assertEqual(client.request("GET", "/v4/market-snapshot"), {"snap": 1})
        token_call, business_call = http.calls
        self.assertTrue(token_call["url"].endswith("/oauth2/token"))
        form = urllib.parse.parse_qs(token_call["body"].decode("ascii"))
        self.assertEqual(form["grant_type"], ["refresh_token"])
        self.assertEqual(form["refresh_token"], ["rtok"])
        self.assertEqual(form["client_id"], ["cid"])
        self.assertEqual(business_call["headers"]["Authorization"], "Bearer tok2")
        cred = store.load()
        self.assertEqual(cred["access_token"], "tok2", "刷新后必须持久化新 token")
        self.assertEqual(cred["expires_at"], self.NOW_MS + 7_200_000)
        self.assertEqual(cred["refresh_token"], "rtok2")

    def test_401_triggers_refresh_and_single_retry(self):
        """401 → 刷新一次 → 用新 token 重试一次成功；refresh_token 不轮换则保留。"""
        client, http, store = self._client(
            {},
            [(401, {"s": "error", "errcode": 401, "errmsg": "token expired"}),
             (200, {"access_token": "tok2", "expires_in": 7200}),
             (200, {"s": "ok", "d": {"ok": 1}})])
        self.assertEqual(client.request("GET", "/v4/market-snapshot"), {"ok": 1})
        self.assertEqual(len(http.calls), 3)
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer tok")
        self.assertTrue(http.calls[1]["url"].endswith("/oauth2/token"))
        self.assertEqual(http.calls[2]["headers"]["Authorization"], "Bearer tok2")
        cred = store.load()
        self.assertEqual(cred["access_token"], "tok2")
        self.assertEqual(cred["refresh_token"], "rtok", "官方不轮换：响应不带则保留旧值")

    def test_refresh_failure_raises_and_skips_business(self):
        client, http, store = self._client(
            {"expires_at": self.NOW_MS + 30_000},
            [(200, {"error": "invalid_grant"})])
        with self.assertRaises(fo.OpenApiError):
            client.request("GET", "/v4/market-snapshot")
        self.assertEqual(len(http.calls), 1, "刷新失败时业务请求不得发出")
        self.assertEqual(store.load()["access_token"], "tok", "失败不得污染凭据")

    def test_missing_refresh_token_raises_without_requests(self):
        client, http, _ = self._client({"refresh_token": "",
                                        "expires_at": self.NOW_MS + 30_000})
        with self.assertRaises(fo.OpenApiError):
            client.request("GET", "/v4/market-snapshot")
        self.assertEqual(http.calls, [])

    def test_5xx_raises_without_retry(self):
        """限频/5xx：本层不做自动重试，如实抛出（调用次数锁定为 1，留给上层）。"""
        client, http, _ = self._client({}, [(500, b"<html>gateway oops</html>")])
        with self.assertRaises(fo.OpenApiError) as cm:
            client.request("GET", "/v4/market-snapshot")
        self.assertEqual(cm.exception.errcode, 500)
        self.assertEqual(len(http.calls), 1)

    def test_missing_mode_raises_openapi_error(self):
        store = fo.CredentialStore(self.tmp / "empty.json")
        client = fo.OpenApiClient(store, http=RecordingHttp(),
                                  now=lambda: self.NOW_S)
        with self.assertRaises(fo.OpenApiError):
            client.request("GET", "/v4/market-snapshot")


class AppKeyClientTest(TempHomeTestBase):
    NOW_S = 1_700_000_000.0

    def _client(self, algorithm, key, responses):
        pem_path = self.tmp / ("key-" + algorithm.replace("-", "") + ".pem")
        pem_path.write_bytes(_pem(key))
        store = fo.CredentialStore(self.tmp / "cred.json")
        store.save({"mode": "appkey", "app_key": "ak123",
                    "private_key_path": str(pem_path), "algorithm": algorithm})
        http = RecordingHttp(*responses)
        client = fo.OpenApiClient(store, http=http, now=lambda: self.NOW_S)
        return client, http, key

    def _verify(self, pub, sig_b64, message, algorithm):
        sig = base64.b64decode(sig_b64)
        if algorithm == "Ed25519":
            pub.verify(sig, message)
        else:
            pub.verify(sig, message, padding.PKCS1v15(), hashes.SHA256())

    def test_four_headers_present_no_bearer_and_signature_verifiable(self):
        key = Ed25519PrivateKey.generate()
        client, http, key = self._client(
            "Ed25519", key, [(200, {"s": "ok", "d": {"order_id": "o1"}})])
        out = client.request("POST", "/v4/trade/place",
                             query={"b": "2", "a": "1"}, json_body={"qty": 100})
        self.assertEqual(out, {"order_id": "o1"})
        call = http.calls[0]
        h = call["headers"]
        self.assertEqual(h["X-Api-Key"], "ak123")
        self.assertEqual(h["X-Timestamp"], "1700000000000")
        self.assertRegex(h["X-Nonce"], r"\A[A-Za-z0-9_-]{1,64}\Z")
        self.assertNotIn("Bearer", h["Authorization"], "AppKey 头是裸 base64 签名")
        self.assertEqual(call["url"],
                         "https://webapi.futunn.com/v4/trade/place?b=2&a=1")
        self.assertEqual(call["body"], b'{"qty":100}')
        message = fo.AppKeySigner.signing_message(
            int(h["X-Timestamp"]), call["method"], "/v4/trade/place",
            "b=2&a=1", call["body"])
        self._verify(key.public_key(), h["Authorization"], message, "Ed25519")

    def test_query_order_is_preserved_and_matches_signature(self):
        key = Ed25519PrivateKey.generate()
        client, http, key = self._client(
            "Ed25519", key, [(200, {"s": "ok", "d": 1})])
        client.request("GET", "/v4/x", query={"z": "3", "a": "1"})
        call = http.calls[0]
        self.assertTrue(call["url"].endswith("?z=3&a=1"),
                        "query 必须按传入顺序拼 URL 与签名，不得排序")
        message = fo.AppKeySigner.signing_message(
            int(call["headers"]["X-Timestamp"]), "GET", "/v4/x", "z=3&a=1", None)
        self._verify(key.public_key(), call["headers"]["Authorization"],
                     message, "Ed25519")

    def test_get_without_query_and_empty_body_keeps_segment_positions(self):
        key = Ed25519PrivateKey.generate()
        client, http, key = self._client(
            "Ed25519", key, [(200, {"s": "ok", "d": 1})])
        client.request("get", "/v4/market-snapshot")
        call = http.calls[0]
        self.assertEqual(call["url"], "https://webapi.futunn.com/v4/market-snapshot")
        self.assertIsNone(call["body"])
        message = fo.AppKeySigner.signing_message(
            int(call["headers"]["X-Timestamp"]), "GET", "/v4/market-snapshot",
            "", None)
        self.assertEqual(message.decode("utf-8").split("\n"),
                         ["1700000000000", "GET", "/v4/market-snapshot", "",
                          EMPTY_SHA256])
        self._verify(key.public_key(), call["headers"]["Authorization"],
                     message, "Ed25519")

    def test_rsa_sha256_client_request_verifies(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        client, http, key = self._client(
            "RSA-SHA256", key, [(200, {"s": "ok", "d": 1})])
        client.request("POST", "/v4/trade/place", json_body={"side": "BUY"})
        call = http.calls[0]
        message = fo.AppKeySigner.signing_message(
            int(call["headers"]["X-Timestamp"]), "POST", "/v4/trade/place",
            "", call["body"])
        self._verify(key.public_key(), call["headers"]["Authorization"],
                     message, "RSA-SHA256")


class FutuAuthOpenApiPureTest(unittest.TestCase):
    """scripts/futu_auth.py --openapi 的纯函数部分。

    真实网络流程（/oauth2/register、浏览器授权页、60355 回调、token 端点实连）
    不自动化测试——人工执行：python scripts/futu_auth.py --openapi。
    """

    def test_redirect_uri_is_localhost_60355(self):
        self.assertEqual(futu_auth.OPENAPI_REDIRECT_URI,
                         "http://localhost:60355/callback")

    def test_register_payload_is_public_client_with_pkce(self):
        payload = futu_auth.openapi_register_payload()
        self.assertEqual(payload["redirect_uris"], ["http://localhost:60355/callback"])
        self.assertEqual(payload["grant_types"], ["authorization_code", "refresh_token"])
        self.assertEqual(payload["response_types"], ["code"])
        self.assertEqual(payload["token_endpoint_auth_method"], "none")

    def test_generate_pkce_matches_s256(self):
        verifier, challenge = futu_auth.openapi_generate_pkce()
        self.assertEqual(challenge, fo.Pkce.challenge(verifier))
        self.assertRegex(verifier, r"\A[A-Za-z0-9\-._~]+\Z")

    def test_generate_state_shape(self):
        state = futu_auth.openapi_generate_state()
        self.assertRegex(state, r"\A[A-Za-z0-9_-]{16,}\Z")

    def test_build_authorize_url_carries_pkce_params(self):
        url = futu_auth.openapi_build_authorize_url(
            "cid", "http://localhost:60355/callback", "st-1", "chal-1",
            "quote:read trade:write")
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "webapi.futunn.com")
        self.assertEqual(parsed.path, "/oauth2/authorize/confirm")
        qs = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(qs["response_type"], ["code"])
        self.assertEqual(qs["client_id"], ["cid"])
        self.assertEqual(qs["redirect_uri"], ["http://localhost:60355/callback"])
        self.assertEqual(qs["state"], ["st-1"])
        self.assertEqual(qs["code_challenge"], ["chal-1"])
        self.assertEqual(qs["code_challenge_method"], ["S256"])
        self.assertEqual(qs["scope"], ["quote:read trade:write"])

    def test_exchange_token_sends_oauth_form_and_returns_response(self):
        recorded = {}

        def fake_post(url, form):
            recorded["url"], recorded["form"] = url, dict(form)
            return {"access_token": "a1", "expires_in": 7200, "refresh_token": "r1"}

        resp = futu_auth.openapi_exchange_token(
            fake_post, code="c1", code_verifier="v1", client_id="cid",
            redirect_uri="http://localhost:60355/callback")
        self.assertEqual(resp["access_token"], "a1")
        self.assertEqual(recorded["url"], futu_auth.TOKEN_URL)
        self.assertEqual(recorded["form"], {
            "grant_type": "authorization_code", "code": "c1",
            "redirect_uri": "http://localhost:60355/callback",
            "client_id": "cid", "code_verifier": "v1"})

    def test_credential_update_sets_expiry_and_keeps_refresh_without_rotation(self):
        base = {"mode": "oauth", "client_id": "cid", "refresh_token": "old-r"}
        out = futu_auth.openapi_credential_update(
            base, {"access_token": "a1", "expires_in": 7200}, 5_000)
        self.assertEqual(out["access_token"], "a1")
        self.assertEqual(out["expires_at"], 5_000 + 7_200_000)
        self.assertEqual(out["refresh_token"], "old-r", "官方不轮换：响应不带则保留旧值")
        out2 = futu_auth.openapi_credential_update(
            base, {"access_token": "a2", "expires_in": 7200,
                   "refresh_token": "new-r", "scope": "s"}, 5_000)
        self.assertEqual(out2["refresh_token"], "new-r")
        self.assertEqual(out2["scope"], "s")
        self.assertEqual(out2["mode"], "oauth")

    def test_validate_state_rejects_mismatch(self):
        self.assertTrue(futu_auth.openapi_validate_state("s1", "s1"))
        with self.assertRaises(ValueError):
            futu_auth.openapi_validate_state("s1", "evil")


if __name__ == "__main__":
    unittest.main()
