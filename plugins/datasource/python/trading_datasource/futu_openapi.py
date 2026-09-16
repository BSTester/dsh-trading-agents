"""富途 OpenAPI（REST）客户端 —— OAuth 2.1+PKCE 与 AppKey 双认证（全仓库唯一实现）。

对齐官方文档（2026-09-16 实抓，规格 §三 认证与凭据）：
- REST Host ``https://webapi.futunn.com``；响应信封
  ``{"s":"ok","d":...}`` / ``{"s":"error","errcode":int,"errmsg":str,
  "jump_url"?,"need_order_confirm"?,"confirm_id"?}``；
- OAuth 2.1+PKCE（推荐）：``POST /oauth2/register``（public client，PKCE required）
  → ``GET /oauth2/authorize/confirm``（S256 challenge）→ 本地 callback 收 code/state
  → ``POST /oauth2/token``（authorization_code + code_verifier）→
  ``{access_token, expires_in:7200, refresh_token, scope}``；刷新用
  ``grant_type=refresh_token``（官方不轮换 refresh_token）；调用带
  ``Authorization: Bearer {access_token}``；
- AppKey：头 ``X-Api-Key`` / ``X-Timestamp``(ms) / ``X-Nonce``（1-64 位
  [A-Za-z0-9_-]）/ ``Authorization: base64(签名)``（无 Bearer 前缀）；
  签名原文五段以 ``\\n`` 连接（空字段保留位置）：
  ``timestamp_ms\\nMETHOD(大写)\\nrequest_path(不含域名与query)\\nquery_string(原始串)
  \\nsha256(body)小写hex``；Ed25519 直接签原文，RSA-SHA256 先 sha256 再 PKCS#1 v1.5；
- Token 安全：不进环境变量，只落 ``~/.dsh/futu-openapi.json``（0600 原子写）。

限频/5xx：本层**不做自动重试**，如实抛 OpenApiError——重试/退避策略留给上层调用方
（交易链路重试需与 OMS 状态机协同，客户端层盲重试会重复下单）。

HTTP 传输可注入（``OpenApiClient(..., http=request_fn)``）：
``request_fn(method, url, headers, body_bytes|None) -> (status:int, body:bytes)``；
默认实现 ``_default_http`` 用标准库 urllib（与 futu_mcp 同为标准库通道）。

仅授权流程（scripts/futu_auth.py --openapi）负责注册/浏览器授权/落盘；本模块负责
凭据读写与带认证的请求。测试见 tests/test_wp8_openapi_client.py（离线注入）。
"""
import base64
import hashlib
import json
import os
import re
import secrets
import string
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_HOST = "https://webapi.futunn.com"
TOKEN_PATH = "/oauth2/token"
# 空 body 的 sha256（官方签名原文第 5 段在无请求体时的取值）
EMPTY_BODY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
NONCE_ALPHABET = string.ascii_letters + string.digits + "_-"
NONCE_PATTERN = r"[A-Za-z0-9_-]{1,64}"


class OpenApiError(Exception):
    """富途 OpenAPI 错误（信封 s==error / token 刷新失败 / 非预期传输响应）。

    need_order_confirm/confirm_id：下单二次确认信封字段——上层（交易闸门/Web 卡片）
    批准后自动调 order-confirm；jump_url：部分错误附带的跳转地址。
    限频/5xx 也在本错误如实抛出（本层不自动重试）。
    """

    def __init__(self, errmsg, errcode=None, need_order_confirm=None,
                 confirm_id=None, jump_url=None):
        super().__init__(errmsg)
        self.errcode = errcode
        self.errmsg = errmsg
        self.need_order_confirm = need_order_confirm
        self.confirm_id = confirm_id
        self.jump_url = jump_url


class Pkce:
    """OAuth 2.1 PKCE（RFC 7636）：S256 = BASE64URL(SHA256(verifier))，无 padding。"""

    @staticmethod
    def code_verifier():
        # 43-128 个非保留字符；token_urlsafe(48) 产 64 字符 [A-Za-z0-9_-]
        return secrets.token_urlsafe(48)

    @staticmethod
    def challenge(verifier):
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class AppKeySigner:
    """AppKey 请求签名。

    官方规则：Ed25519 直接签签名原文；RSA-SHA256 先 sha256 再 PKCS#1 v1.5 签名
    （即 ``key.sign(msg, PKCS1v15(), SHA256())`` 的 hash-then-sign）。
    """

    ALGORITHMS = ("Ed25519", "RSA-SHA256")

    def __init__(self, private_key, algorithm):
        algo = self._normalize_algorithm(algorithm)
        if algo == "Ed25519" and not isinstance(private_key, Ed25519PrivateKey):
            raise ValueError("算法 Ed25519 需要 Ed25519 私钥")
        if algo == "RSA-SHA256" and not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("算法 RSA-SHA256 需要 RSA 私钥")
        self._key = private_key
        self.algorithm = algo

    @staticmethod
    def _normalize_algorithm(algorithm):
        algo = str(algorithm).strip().upper().replace("_", "-")
        if algo == "ED25519":
            return "Ed25519"
        if algo == "RSA-SHA256":
            return "RSA-SHA256"
        raise ValueError(f"不支持的签名算法：{algorithm}（可选 Ed25519 / RSA-SHA256）")

    @classmethod
    def from_path(cls, private_key_path, algorithm):
        """从 PEM 私钥文件（PKCS8）构造签名器。"""
        pem = Path(private_key_path).expanduser().read_bytes()
        key = serialization.load_pem_private_key(pem, password=None)
        return cls(key, algorithm)

    @staticmethod
    def signing_message(timestamp_ms, method, path, query, body_bytes):
        """签名原文五段，以 \\n 连接，空字段保留位置：

        timestamp_ms \\n METHOD(大写) \\n request_path \\n query_string \\n sha256(body)hex
        """
        body_part = hashlib.sha256(body_bytes or b"").hexdigest()
        return "\n".join([str(timestamp_ms), str(method).upper(), path,
                          query or "", body_part]).encode("utf-8")

    def sign(self, timestamp_ms, method, path, query, body_bytes):
        """返回 base64(signature)（即 Authorization 头的取值，无 Bearer 前缀）。"""
        message = self.signing_message(timestamp_ms, method, path, query, body_bytes)
        if self.algorithm == "Ed25519":
            signature = self._key.sign(message)
        else:
            signature = self._key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode("ascii")

    @staticmethod
    def nonce(length=32):
        """随机 X-Nonce：1-64 位 [A-Za-z0-9_-]（官方约束）。"""
        if not 1 <= length <= 64:
            raise ValueError("nonce 长度须在 1-64")
        return "".join(secrets.choice(NONCE_ALPHABET) for _ in range(length))


def default_credential_path():
    """~/.dsh/futu-openapi.json（DSH_HOME 可改根，与 futu_mcp 口径一致）。"""
    dsh = Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh")).expanduser()
    return dsh / "futu-openapi.json"


class CredentialStore:
    """~/.dsh/futu-openapi.json 读写（0600 原子写；路径可覆盖供测试隔离）。

    文件字段：mode(oauth|appkey) / client_id / access_token / refresh_token /
    expires_at(ms) / scope / app_key / private_key_path / algorithm。
    Token 安全约定：不进环境变量，只落本文件（0600）。
    """

    def __init__(self, path=None):
        self.path = Path(path).expanduser() if path is not None \
            else default_credential_path()

    def load(self):
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ValueError(f"凭据文件不是合法 JSON：{self.path}（{e}）") from e
        if not isinstance(data, dict):
            raise ValueError(f"凭据文件顶层必须是 JSON 对象：{self.path}")
        return data

    def save(self, data):
        """原子写：同目录临时文件 + fsync + chmod 0600 + os.replace。"""
        if not isinstance(data, dict):
            raise ValueError("凭据必须是 dict")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=".futu-openapi.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def json_body_bytes(json_body):
    """请求体字节（发送与签名哈希共用同一份，保证逐字节一致）。"""
    return json.dumps(json_body, ensure_ascii=False, separators=(",", ":")) \
        .encode("utf-8")


def query_string(query):
    """原始 query 串：dict 按插入序完全转义（safe=""，避免 + / 空格歧义）；str 原样。"""
    if query is None:
        return ""
    if isinstance(query, str):
        return query
    return urllib.parse.urlencode(query, quote_via=urllib.parse.quote, safe="")


def _safe_json_dict(body):
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_envelope(status, body):
    """官方响应信封：{"s":"ok","d":...} → d；{"s":"error",...} → OpenApiError。

    非 JSON / 非信封（含 5xx、限频页）：如实抛 OpenApiError——本层不自动重试。
    """
    data = _safe_json_dict(body)
    if isinstance(data, dict) and data.get("s") == "ok":
        return data.get("d")
    if isinstance(data, dict) and data.get("s") == "error":
        raise OpenApiError(
            data.get("errmsg") or "未知错误",
            errcode=data.get("errcode"),
            need_order_confirm=data.get("need_order_confirm"),
            confirm_id=data.get("confirm_id"),
            jump_url=data.get("jump_url"),
        )
    raise OpenApiError(
        errcode=status if isinstance(status, int) and status else -1,
        errmsg=f"非预期响应（HTTP {status}）：{(body or b'')[:200]!r}")


def _default_http(method, url, headers, body):
    """urllib 传输：非 2xx 不抛（HTTPError 转 (status, body)），由上层解释信封。"""
    req = urllib.request.Request(url, data=body, method=str(method).upper())
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        finally:
            e.close()
        return e.code, payload


class OpenApiClient:
    """富途 OpenAPI REST 客户端（OAuth 2.1+PKCE / AppKey 双认证）。

    OAuth：Bearer 调用；expires_at 前 60s 或遇 401 时用 refresh_token 刷新**一次**
    并重试**一次**（刷新失败 → OpenApiError）；刷新成功立即持久化新 token。
    AppKey：每次请求现算签名头（nonce 自动生成）。
    限频/5xx：不自动重试，如实抛出——留给上层。
    """

    REFRESH_LEEWAY_MS = 60_000  # expires_at 前 60s 视为临期，主动刷新

    def __init__(self, credential_store, http=None, host=DEFAULT_HOST, now=None):
        self.store = credential_store
        self._http = http or _default_http
        self.host = str(host).rstrip("/")
        self._now = now or time.time

    # ------------------------------------------------------------ 公共入口

    def request(self, method, path, query=None, json_body=None):
        """发起请求，返回信封 d 部分；s==error / 传输异常 → OpenApiError。"""
        mode = self.store.load().get("mode")
        if mode == "oauth":
            return self._request_oauth(method, path, query, json_body)
        if mode == "appkey":
            return self._request_appkey(method, path, query, json_body)
        raise OpenApiError(
            f"凭据 mode 未配置（{mode!r}）：请先运行 scripts/futu_auth.py --openapi"
            "或在 ~/.dsh/futu-openapi.json 配置 AppKey")

    # ------------------------------------------------------------ OAuth

    def _now_ms(self):
        return int(self._now() * 1000)

    def _url(self, path, query):
        qs = query_string(query)
        return self.host + path + (("?" + qs) if qs else "")

    def _request_oauth(self, method, path, query, json_body):
        cred = self.store.load()
        if not cred.get("access_token") and not cred.get("refresh_token"):
            raise OpenApiError(
                "OAuth 凭据缺失（access_token/refresh_token 均为空）；"
                "请先运行 scripts/futu_auth.py --openapi")
        refreshed = False
        expires_at = cred.get("expires_at")
        # expires_at 缺失时无法判断临期，跳过主动刷新、只依赖 401 兜底
        if expires_at is not None and \
                self._now_ms() >= int(expires_at) - self.REFRESH_LEEWAY_MS:
            cred = self._refresh(cred)  # 临期主动刷新一次；失败抛 OpenApiError
            refreshed = True
        status, body = self._send_oauth(cred, method, path, query, json_body)
        if status == 401 and not refreshed:
            cred = self._refresh(cred)  # 401 触发刷新一次
            status, body = self._send_oauth(cred, method, path, query, json_body)
        return parse_envelope(status, body)

    def _send_oauth(self, cred, method, path, query, json_body):
        headers = {}
        body = None
        if json_body is not None:
            body = json_body_bytes(json_body)
            headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer " + str(cred.get("access_token", ""))
        return self._http(str(method).upper(), self._url(path, query), headers, body)

    def _refresh(self, cred):
        """refresh_token 换新 access_token（官方不轮换 refresh_token）。

        注意 token 端点响应不是 {"s":...} 信封，而是裸 OAuth JSON。
        """
        refresh_token = cred.get("refresh_token")
        if not refresh_token:
            raise OpenApiError("无 refresh_token 可刷新；请重新运行"
                               " scripts/futu_auth.py --openapi")
        form = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": cred.get("client_id", ""),
        }).encode("ascii")
        status, body = self._http(
            "POST", self.host + TOKEN_PATH,
            {"Content-Type": "application/x-www-form-urlencoded"}, form)
        data = _safe_json_dict(body)
        if status >= 400 or not isinstance(data, dict) or not data.get("access_token"):
            detail = None
            if isinstance(data, dict):
                detail = data.get("errmsg") or data.get("error_description") \
                    or data.get("error")
            errcode = data.get("errcode") if isinstance(data, dict) else None
            raise OpenApiError(
                errcode=errcode if errcode is not None else status,
                errmsg="refresh_token 刷新失败："
                       f"{detail or (body[:200] if body else status)}")
        updated = dict(cred)
        updated["mode"] = "oauth"
        updated["access_token"] = data["access_token"]
        updated["expires_at"] = self._now_ms() + \
            int(float(data.get("expires_in", 7200)) * 1000)
        if data.get("refresh_token"):  # 官方不轮换：响应带了才更新
            updated["refresh_token"] = data["refresh_token"]
        if data.get("scope"):
            updated["scope"] = data["scope"]
        self.store.save(updated)  # 刷新后持久化新 token
        return updated

    # ------------------------------------------------------------ AppKey

    def _request_appkey(self, method, path, query, json_body):
        cred = self.store.load()
        app_key = cred.get("app_key")
        key_path = cred.get("private_key_path")
        if not app_key or not key_path:
            raise OpenApiError("AppKey 凭据缺失（app_key/private_key_path）；"
                               "请在 ~/.dsh/futu-openapi.json 补全")
        signer = AppKeySigner.from_path(key_path, cred.get("algorithm", "Ed25519"))
        ts_ms = self._now_ms()
        body = json_body_bytes(json_body) if json_body is not None else None
        qs = query_string(query)
        headers = {
            "X-Api-Key": str(app_key),
            "X-Timestamp": str(ts_ms),
            "X-Nonce": signer.nonce(),
            # AppKey：base64(签名)，无 Bearer 前缀
            "Authorization": signer.sign(ts_ms, method, path, qs, body),
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, resp_body = self._http(str(method).upper(), self._url(path, query),
                                       headers, body)
        return parse_envelope(status, resp_body)
