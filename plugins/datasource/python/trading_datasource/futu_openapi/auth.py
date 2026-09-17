"""认证与凭据：OAuth 2.1+PKCE、AppKey 签名、凭据落盘（0600）。"""
import base64
import hashlib
import json
import os
import secrets
import string
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

NONCE_ALPHABET = string.ascii_letters + string.digits + "_-"
NONCE_PATTERN = r"[A-Za-z0-9_-]{1,64}"
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

        官方规则（逐字）：**没有请求体时第 5 段传空字符串**（官方 GET 示例第 5 段
        为空），不是 ``sha256(b"")`` 的 e3b0c44…——因此空 body 的原文以 ``\\n`` 结尾，
        如 ``1700000000000\\nGET\\n/v4/x\\n\\n``（按官方原文验签通过；用 e3b0c44…
        验签 InvalidSignature，已实证）。
        """
        body_part = "" if not body_bytes else hashlib.sha256(body_bytes).hexdigest()
        return "\n".join([str(timestamp_ms), str(method).upper(), path,
                          query or "", body_part]).encode("utf-8")

    def sign(self, timestamp_ms, method, path, query, body_bytes):
        """返回 base64(signature)（即 Authorization 头的取值，无 Bearer 前缀）。"""
        message = self.signing_message(timestamp_ms, method, path, query, body_bytes)
        return self._sign_message(message)

    @staticmethod
    def ws_signing_message(timestamp_ms, nonce):
        """WebSocket 鉴权/刷新的签名原文（**与 REST 五段原文不同**，附录 A 实抓）：

        ``{timestamp_ms}\\n{nonce}\\nWEBSOCKET\\nws/auth``

        行情 WS 与交易 WS 完全同构（``trade_event_push/auth.md`` 已核对），因此两种
        action（auth/refresh）共用这一份原文——刷新帧换的是 ``timestamp_ms``/``nonce``，
        原文形状不变。
        """
        return "\n".join([str(timestamp_ms), str(nonce), "WEBSOCKET",
                          "ws/auth"]).encode("utf-8")

    def sign_ws(self, timestamp_ms, nonce):
        """WS 鉴权/刷新帧的 ``authorization``：base64(signature)，无 Bearer 前缀。

        签名算法与 REST 一致（Ed25519 直签原文；RSA-SHA256 先 sha256 再 PKCS#1 v1.5），
        只有原文不同——**不得复用 REST 的 ``sign``**（五段原文会验签失败）。
        """
        return self._sign_message(self.ws_signing_message(timestamp_ms, nonce))

    def _sign_message(self, message):
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

