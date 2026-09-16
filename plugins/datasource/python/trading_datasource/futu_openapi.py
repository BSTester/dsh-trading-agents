"""富途 OpenAPI（REST）客户端 —— OAuth 2.1+PKCE 与 AppKey 双认证（全仓库唯一实现）。

对齐官方文档（2026-09-16 实抓，规格 §三 认证与凭据）：
- REST Host ``https://webapi.futunn.com``；**两种信封并存，不是「同一信封」**：
  * 交易侧：``{"s":"ok","d":...}`` / ``{"s":"error","errcode":int,"errmsg":str,
    "jump_url"?,"need_order_confirm"?,"confirm_id"?}``（``s==error`` → OpenApiError，
    ``need_order_confirm=true`` → OrderConfirmRequired）；
  * 行情侧：``{"ret_code":0,"data":{...},"pagination":{...}}``（``ret_code!=0`` →
    OpenApiError，errcode=ret_code）；分页在**信封顶层**而非 data 内；
  * 两者都不是（5xx/429/网关页/非 JSON/非信封）→ ``UnexpectedResponse``：这是
    **非业务错误**（响应体不是券商结论，请求可能已到达服务端），写路径必须落
    ``unknown``「先查询、勿重放」，只有真业务错误信封才算拒绝；
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
  \\nsha256(body)小写hex``；**没有请求体时第 5 段传空字符串**（官方文档逐字规定，
  官方 GET 示例第 5 段为空——不是 ``sha256(b"")`` 的 e3b0c44…，空 body 的原文以
  ``\\n`` 结尾）；Ed25519 直接签原文，RSA-SHA256 先 sha256 再 PKCS#1 v1.5；
- **WebSocket 签名原文与 REST 不同**（WP8 任务 4，附录 A 实抓）：行情 WS 与交易 WS
  同构，原文 ``{timestamp_ms}\\n{nonce}\\nWEBSOCKET\\nws/auth``（``sign_ws`` /
  ``ws_signing_message``）；鉴权/刷新的 JSON 帧构造在 ``server/futu_push.py``；
- Token 安全：不进环境变量，只落 ``~/.dsh/futu-openapi.json``（0600 原子写）。

限频/5xx：本层**不做自动重试**，如实抛 ``UnexpectedResponse``（非信封响应，见上）——
重试/退避策略留给上层调用方（交易链路重试需与 OMS 状态机协同，客户端层盲重试会重复
下单）；429 时响应头 ``Retry-After`` 并入 ``OpenApiError.retry_after`` 供上层退避。

HTTP 传输可注入（``OpenApiClient(..., http=request_fn)``）：
``request_fn(method, url, headers, body_bytes|None)
-> (status:int, body:bytes, headers:dict)``；
默认实现 ``_default_http`` 用标准库 urllib（与 futu_mcp 同为标准库通道），并把
URLError/超时/连接重置等传输异常包装为 ``TransportError``（OpenApiError 子类）。

仅授权流程（scripts/futu_auth.py --openapi）负责注册/浏览器授权/落盘；本模块负责
凭据读写与带认证的请求。本模块另含两个 REST 方法组：``OpenApiMarket``（WP8 任务 2，
行情）与 ``OpenApiTrade``（WP8 任务 3，交易/订单/成交/账户）。测试见
tests/test_wp8_openapi_client.py、tests/test_wp8_market.py、tests/test_wp8_trading.py
（离线注入）。
"""
import base64
import hashlib
import http.client
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
NONCE_ALPHABET = string.ascii_letters + string.digits + "_-"
NONCE_PATTERN = r"[A-Za-z0-9_-]{1,64}"


class OpenApiError(Exception):
    """富途 OpenAPI 错误（信封 s==error / token 刷新失败 / 非预期传输响应）。

    need_order_confirm=true 的信封抛专用子类 ``OrderConfirmRequired``；
    jump_url：部分错误附带的跳转地址；retry_after：429 时从响应头并入
    （原值不解析，供上层退避）；限频/5xx 也在本错误如实抛出（本层不自动重试）。
    """

    def __init__(self, errmsg, errcode=None, need_order_confirm=None,
                 confirm_id=None, jump_url=None, retry_after=None):
        super().__init__(errmsg)
        self.errcode = errcode
        self.errmsg = errmsg
        self.need_order_confirm = need_order_confirm
        self.confirm_id = confirm_id
        self.jump_url = jump_url
        self.retry_after = retry_after


class TransportError(OpenApiError):
    """传输层异常（DNS 解析失败 / 连接拒绝 / 超时 / 连接重置 / 响应中断）。

    由默认传输 ``_default_http`` 抛出：urllib 的 URLError/OSError 等不再裸穿，
    使 ``request()`` 的「传输异常 → OpenApiError」契约成立（errcode 为 None，
    与业务 errcode 语义区分）。注入式传输抛出的任意异常同样由 ``_transport_call``
    收敛为本类，契约对注入传输一样成立。
    """


class UnexpectedResponse(OpenApiError):
    """非信封响应（5xx / 429 / 非 JSON / 网关页）：**不是业务结论**。

    ``parse_envelope_meta`` 对既非 ``{"s":...}`` 也非 ``{"ret_code":...}`` 的响应抛本类
    （``errcode`` = HTTP status，``retry_after`` = 429 的响应头原值）。

    与业务错误信封（有 errcode/errmsg）严格区分：本类代表「服务端**没有**给出可判定的
    业务结论」，请求**可能已到达券商**——交易写路径必须落 ``unknown``（铁律：先查询订单，
    勿重放），绝不能当 ``rejected``（终态、无出边，会把可能已成交的单丢掉）。
    读路径同样按「通道不可用」而不是「业务错误」处置（见 server/futu_data.py）。
    """


class OrderConfirmRequired(OpenApiError):
    """下单二次确认信封（``need_order_confirm=true``）：订单**已挂起待确认**。

    该信封代表订单已挂起而不是失败——上层（交易闸门/Web 卡片）必须显式
    ``except OrderConfirmRequired`` 分流：向用户展示 confirm_id/jump_url，批准后
    调 order-confirm；**禁止对原请求盲重试**（会重复下单）。
    """


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


def json_body_bytes(json_body):
    """请求体字节（发送与签名哈希共用同一份，保证逐字节一致）。"""
    return json.dumps(json_body, ensure_ascii=False, separators=(",", ":")) \
        .encode("utf-8")


def query_string(query):
    """原始 query 串：dict 按插入序完全转义（safe=""，避免 + / 空格歧义）；str 原样。

    * **同名多值**必须展开：``period=["BEFORE","AFTER"]`` → ``period=BEFORE&period=AFTER``
      （官方 rt-ticker 多值语义）→ ``doseq=True``。缺了它，list 会被序列化成 Python repr
      （``period=%5B%27BEFORE%27...%5D``），网关按非法枚举拒绝（实测 -3 invalid_parameter）；
    * ``None`` = 调用方未提供 → **整条省略**（与请求体 ``_body`` 去 None 同规）。不省略会
      出站 ``start=None``，网关按日期 pattern 拒绝（capital-flow/history 实测 -3）；
    * ``str`` 值不受 doseq 影响（仍是单值）；URL 与 AppKey 签名原文共用本函数，
      保证逐字节一致。
    """
    if query is None:
        return ""
    if isinstance(query, str):
        return query
    pairs = [(key, value) for key, value in query.items() if value is not None]
    return urllib.parse.urlencode(pairs, doseq=True,
                                  quote_via=urllib.parse.quote, safe="")


def _safe_json_dict(body):
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_envelope(status, body):
    """官方响应信封 → d；业务错误信封 → OpenApiError（见 ``parse_envelope_meta``）。

    非 JSON / 非信封（含 5xx、限频页）：抛 ``UnexpectedResponse``（OpenApiError 子类）
    ——「没有业务结论」与「业务拒绝」是两回事，本层不自动重试。
    """
    return parse_envelope_meta(status, body)[0]


def parse_envelope_meta(status, body):
    """``parse_envelope`` 的 (d, 分页) 版本。

    三种形态（**不是同一信封**）：
      * 交易侧 ``{"s":"ok","d":...}`` → (d, None)；``{"s":"error",...}`` →
        OpenApiError / OrderConfirmRequired；
      * 行情侧网关 ``{"ret_code":0,"data":{...},"pagination":{...}}`` —— 分页在**信封
        顶层**而不在 data 内，OpenApiMarket 把它并入返回值以对齐 MCP 通道
        ``futu_mcp._unwrap`` 的形状（那里 pagination 同样被并入 data）；``ret_code!=0``
        → OpenApiError（errcode=ret_code）；无分页时第二个元素为 ``None``；
      * 其余（非 JSON/非信封/5xx/429 网关页）→ ``UnexpectedResponse``：**非业务错误**，
        写路径据此落 unknown「先查询、勿重放」，不得当业务拒绝。
    """
    data = _safe_json_dict(body)
    if isinstance(data, dict) and data.get("s") == "ok":
        return data.get("d"), None
    if isinstance(data, dict) and data.get("s") == "error":
        fields = dict(
            errmsg=data.get("errmsg") or "未知错误",
            errcode=data.get("errcode"),
            need_order_confirm=data.get("need_order_confirm"),
            confirm_id=data.get("confirm_id"),
            jump_url=data.get("jump_url"))
        # need_order_confirm=true：订单已挂起待确认——抛专用子类，供交易闸门
        # 显式 except 分流（该信封禁止盲重试，会重复下单）
        cls = OrderConfirmRequired if fields["need_order_confirm"] else OpenApiError
        raise cls(**fields)
    if isinstance(data, dict) and "ret_code" in data:
        # 行情类网关信封：ret_code!=0 → 业务错误；==0 → data + 顶层 pagination
        if data.get("ret_code") != 0:
            raise OpenApiError(data.get("ret_msg") or data.get("errmsg") or "未知错误",
                               errcode=data.get("ret_code"))
        pagination = data.get("pagination")
        return data.get("data"), pagination if isinstance(pagination, dict) else None
    raise UnexpectedResponse(
        errcode=status if isinstance(status, int) and status else -1,
        errmsg=f"非预期响应（HTTP {status}）：{(body or b'')[:200]!r}")


def _default_http(method, url, headers, body):
    """urllib 传输：返回 ``(status, body, headers)`` 三元组（headers 供上层取
    ``Retry-After`` 等响应头）。

    - 非 2xx 不抛（HTTPError 转 (status, body, headers)），由上层解释信封；
    - 其余传输异常（URLError/连接拒绝/超时/连接重置/响应中断）一律包装为
      ``TransportError``（OpenApiError 子类）——``request()`` 契约
      「传输异常 → OpenApiError」成立，不再裸穿给调用方。
    """
    req = urllib.request.Request(url, data=body, method=str(method).upper())
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
            resp_headers = dict(e.headers or {})
        except (OSError, http.client.HTTPException) as inner:
            # 读错误响应体时连接中断，同样收敛为传输异常
            raise TransportError(
                f"网络传输失败（读取错误响应体：{type(inner).__name__}）：{inner}"
            ) from inner
        finally:
            e.close()
        return e.code, payload, resp_headers
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        # URLError（含 DNS/拒绝连接）是 OSError 子类；socket.timeout/TimeoutError、
        # ConnectionResetError 也是 OSError 子类；IncompleteRead 走 HTTPException
        raise TransportError(f"网络传输失败（{type(e).__name__}）：{e}") from e


class OpenApiClient:
    """富途 OpenAPI REST 客户端（OAuth 2.1+PKCE / AppKey 双认证）。

    OAuth：Bearer 调用；expires_at 前 60s 或遇 401 时用 refresh_token 刷新**一次**
    并重试**一次**（刷新失败 → OpenApiError）；刷新成功立即持久化新 token。
    AppKey：每次请求现算签名头（nonce 自动生成）。
    限频/5xx：不自动重试，如实抛出——留给上层；非信封响应统一为
    ``UnexpectedResponse``，429 时响应头 ``Retry-After`` 并入 ``OpenApiError.retry_after``。
    传输异常（含注入式传输抛出的任意异常）收敛为 ``TransportError``。
    """

    REFRESH_LEEWAY_MS = 60_000  # expires_at 前 60s 视为临期，主动刷新

    def __init__(self, credential_store, http=None, host=DEFAULT_HOST, now=None):
        self.store = credential_store
        self._http = http or _default_http
        self.host = str(host).rstrip("/")
        self._now = now or time.time

    # ------------------------------------------------------------ 公共入口

    def request(self, method, path, query=None, json_body=None):
        """发起请求，返回信封 d 部分。

        s==error / ret_code!=0 → OpenApiError（业务结论）；非信封/5xx/429 →
        UnexpectedResponse（**非业务结论**，调用方不得当业务拒绝）；传输异常为
        TransportError 子类；429 附 retry_after。
        """
        d, _pagination = self.request_meta(method, path, query, json_body)
        return d

    def request_meta(self, method, path, query=None, json_body=None):
        """``request`` 的 (d, 信封顶层 pagination) 版本。

        分页由 OpenApiMarket 的分页端点（capital_flow_history/option_screen/
        history_kline）消费：并入返回值后与 MCP 通道 data 形状一致（见
        parse_envelope_meta）。无分页时 pagination 为 None。
        """
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

    @staticmethod
    def _transport_call(http, method, url, headers, body):
        """调传输并兑现 ``request()`` 的契约：传输层异常一律收敛为 ``TransportError``。

        默认传输 ``_default_http`` 自己已包装；**注入式传输**（测试/自研通道）可能抛任意
        异常（connection reset / 自定义 HTTP 库异常）——在三个调用点统一收敛，避免裸穿
        给调用方被误当业务错误（交易写路径会把裸异常上抛成 broker-unavailable 而不是
        unknown）。已经是 OpenApiError 系（含 TransportError）的原样上抛。
        """
        try:
            return http(str(method).upper(), url, headers, body)
        except OpenApiError:
            raise
        except Exception as error:  # noqa: BLE001 —— 注入传输的任意异常都是传输失败
            raise TransportError(
                f"网络传输失败（{type(error).__name__}）：{error}") from error

    @staticmethod
    def _with_retry_after(err, status, headers):
        """429 时把响应头 ``Retry-After`` 原值并入错误（供上层退避；不解析格式）。

        headers 键名大小写不敏感扫描（dict 化后的键保留服务端原始大小写）。
        """
        if status == 429 and err.retry_after is None:
            for key, value in (headers or {}).items():
                if str(key).lower() == "retry-after" and value:
                    err.retry_after = str(value).strip()
                    break
        return err

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
        status, body, headers = self._send_oauth(cred, method, path, query, json_body)
        if status == 401 and not refreshed:
            cred = self._refresh(cred)  # 401 触发刷新一次
            status, body, headers = self._send_oauth(cred, method, path, query,
                                                     json_body)
        try:
            return parse_envelope_meta(status, body)
        except OpenApiError as e:
            raise self._with_retry_after(e, status, headers) from None

    def _send_oauth(self, cred, method, path, query, json_body):
        headers = {}
        body = None
        if json_body is not None:
            body = json_body_bytes(json_body)
            headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer " + str(cred.get("access_token", ""))
        return self._transport_call(self._http, method, self._url(path, query),
                                    headers, body)

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
        status, body, headers = self._transport_call(
            self._http, "POST", self.host + TOKEN_PATH,
            {"Content-Type": "application/x-www-form-urlencoded"}, form)
        data = _safe_json_dict(body)
        if status >= 400 or not isinstance(data, dict) or not data.get("access_token"):
            detail = None
            if isinstance(data, dict):
                detail = data.get("errmsg") or data.get("error_description") \
                    or data.get("error")
            errcode = data.get("errcode") if isinstance(data, dict) else None
            raise self._with_retry_after(
                OpenApiError(
                    errcode=errcode if errcode is not None else status,
                    errmsg="refresh_token 刷新失败："
                           f"{detail or (body[:200] if body else status)}"),
                status, headers)
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
        status, resp_body, headers = self._transport_call(
            self._http, method, self._url(path, query), headers, body)
        try:
            return parse_envelope_meta(status, resp_body)
        except OpenApiError as e:
            raise self._with_retry_after(e, status, headers) from None


# ---------------------------------------------------------------------------
# REST 方法组共用参数校验（OpenApiMarket / OpenApiTrade 唯一实现）
# ---------------------------------------------------------------------------
class _RestValidators:
    """REST 方法组共用参数校验器（``OpenApiMarket`` / ``OpenApiTrade`` 唯一实现）。

    约定：``None`` = 调用方未提供；必填字段缺省即拒（本地 ``ValueError``，消息面向
    调用方）；枚举/区间在此拒绝，坏参数**不触达网络**；请求体经 ``_body`` 去 None
    （官方接口按缺省处理省略字段）。严格度对齐官方文档：未知枚举值一律拒绝。
    """

    _REQUIRED = object()  # 「必填枚举」哨兵：default=None 表示可省略（请求体去 None）

    def _codes(self, codes, count_max):
        """批量 code_list 校验：1..count_max 个非空字符串（官方 invalid_parameter 面）。"""
        if not isinstance(codes, list) or not 1 <= len(codes) <= count_max:
            raise ValueError(
                f"code_list 必须是 1..{count_max} 个标的代码的列表（如 HK.00700）")
        for code in codes:
            if not isinstance(code, str) or not code.strip():
                raise ValueError(f"code_list 元素必须是非空字符串，得到：{code!r}")
        return list(codes)

    def _int_in(self, value, low, high, name, default=None):
        """整数区间校验：None 走 default；越界/类型错拒绝（bool 是 int 的子类，排除）。"""
        if value is None:
            if default is None:
                raise ValueError(f"{name} 必填（{low}..{high} 的整数）")
            return default
        if isinstance(value, bool) or not isinstance(value, int) \
                or not low <= value <= high:
            raise ValueError(f"{name} 必须是 {low}..{high} 的整数")
        return value

    def _enum_in(self, value, allowed, name, default=_REQUIRED):
        """枚举校验：None 走 default；default 为哨兵时视为必填。"""
        if value is None:
            if default is self._REQUIRED:
                raise ValueError(f"{name} 必填，取值之一：{sorted(allowed)}")
            return default
        if value not in allowed:
            raise ValueError(f"{name} 取值非法：{value!r}（允许：{sorted(allowed)}）")
        return value

    def _csv_enum(self, value, allowed, name, default=None):
        """逗号分隔字符串枚举（官方 ``filter_expiration_cycles`` 的形态）。

        官方类型是**字符串**（网关 pattern 逐字：``^(A|B)(,(A|B))*$``，2026-09-16 实测），
        **不是数组**——传 list 会被出站序列化成 Python repr 并被网关 -3 拒绝，因此这里只
        接受字符串（list 等非字符串本地拒绝，坏参数零网络往返）；``""`` 视为未提供。
        元素两端空白容忍并归一（去掉后按原文逗号拼接，保证与网关 pattern 一致）。
        """
        if value is None or value == "":
            return default
        if not isinstance(value, str):
            raise ValueError(
                f"{name} 必须是逗号分隔的字符串（官方类型 string，如 'WEEK,MONTH'），"
                f"不接受 {type(value).__name__}：{value!r}；允许：{sorted(allowed)}")
        parts = [part.strip() for part in value.split(",")]
        if any(part not in allowed for part in parts):
            raise ValueError(f"{name} 取值非法：{value!r}"
                             f"（允许：{sorted(allowed)}，逗号分隔）")
        return ",".join(parts)

    def _date(self, value, name, required=False):
        """yyyy-MM-dd 日期校验（含日历有效性）。"""
        if value is None or value == "":
            if required:
                raise ValueError(f"{name} 必填（yyyy-MM-dd）")
            return None
        if not isinstance(value, str):
            raise ValueError(f"{name} 必须是 yyyy-MM-dd 字符串")
        try:
            import datetime as _dt
            _dt.date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} 不是合法日期：{value!r}（yyyy-MM-dd）") from None
        return value

    def _body(self, mapping):
        """去掉 None 值的请求体（官方接口按缺省处理省略字段）。"""
        return {key: value for key, value in mapping.items() if value is not None}

    # ------------------------------------------------------------ 文本/标量/路径
    # WP12 任务 2 起，以下助手由 OpenApiMarket/OpenApiTrade/数据面七组**共用一份实现**
    # （原先只存在于 OpenApiTrade；提升到基类避免第二份实现漂移）。

    def _path_token(self, value, name):
        """路径片段：非空字符串且不含路径分隔符/空白（避免拼出意外路径）。"""
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 必须是非空字符串")
        text = value.strip()
        if any(char in text for char in "/?#") or any(char.isspace() for char in text):
            raise ValueError(f"{name} 含非法字符（不得含 / ? # 或空白）：{text[:40]!r}")
        return text

    def _symbol(self, code):
        """标的代码校验（官方 exchange.symbol 形状；大小写归一为大写）。"""
        if not isinstance(code, str) or not self._SYMBOL_RE.fullmatch(code.strip().upper()):
            raise ValueError(f"code 形如 US.AAPL/HK.00700/SH.600519，得到：{code!r}")
        return code.strip().upper()

    def _symbol_in_path(self, symbol):
        """进路径的标的代码：``_symbol`` 形状 + 官方长度上限 32（锁定表 §C.1/§C.7）。"""
        code = self._symbol(symbol)
        if len(code) > self.SYMBOL_PATH_MAX:
            raise ValueError(f"symbol 长度不得超过 {self.SYMBOL_PATH_MAX}：{code[:40]!r}")
        return code

    def _text(self, value, name, required=True, default=None):
        """数量/价格/编号：官方声明为 string（本层接受数值并转字符串）。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise ValueError(f"{name} 必填")
            return default
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"{name} 必须是字符串或数值，得到：{value!r}")
        return str(value).strip()

    def _text_max(self, value, name, max_len, required=True):
        """有官方长度上限的文本字段（超长本地拒绝，零网络往返）。"""
        text = self._text(value, name, required=required)
        if text is not None and len(text) > max_len:
            raise ValueError(f"{name} 长度不得超过 {max_len}：{text[:40]!r}")
        return text

    def _scalar(self, value, name, required=False, default=None):
        """标量直通（str/int/float，排除 bool/对象/数组）。

        用于锁定表**只给出参数名、未给出枚举或区间**的字段：本层只挡明显的类型错误
        （会拼出 Python repr 的 dict/list），枚举语义由服务端判定并如实抛出——
        不自行发明枚举（「禁止猜测」纪律，见锁定表 §D）。
        """
        if value is None or value == "":
            if required:
                raise ValueError(f"{name} 必填")
            return default
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"{name} 必须是标量（字符串或数值），得到：{value!r}")
        return value

    def _opt_int(self, value, low, high, name):
        """可选整数区间：``None`` 原样通过（不触发 ``_int_in`` 的必填判定）。"""
        if value is None:
            return None
        return self._int_in(value, low, high, name)

    def _merge_pagination(self, d, pagination):
        """信封顶层 pagination 并入 d（对齐 futu_mcp._unwrap；无分页原样返回）。"""
        if pagination:
            return {**d, "pagination": pagination}
        return d

    # ------------------------------------------------------------ 错误码语义
    # 锁定表 §A「无数据语义」与 §C.7/§C.8/§D.3 的权限项在此机械落地——两个语义是
    # 官方明示的，不属于「业务解释」：`-10` 是「合法但无数据」，`-9` 是「无权限/身份无效」。

    def _tolerate_no_data(self, call):
        """``-10 no_data`` → 空而非错：返回 ``{"no_data": True}``。

        **不伪造 items/字段**：官方只说「无数据」，本层就不替它编造空数组或零值——
        调用方据 ``no_data`` 如实展示「该标的当前无此数据」。
        """
        try:
            return call()
        except OpenApiError as error:
            if error.errcode == self.NO_DATA_ERRCODE:
                return {"no_data": True}
            raise

    def _permission_note(self, call, note):
        """``-9`` → 如实抛出且可读：消息 = 本层说明 + 官方原文；**不重试、不当空数据**。"""
        try:
            return call()
        except OpenApiError as error:
            if error.errcode == self.PERMISSION_ERRCODE:
                raise OpenApiError(f"{note}：{error.errmsg}",
                                   errcode=error.errcode,
                                   need_order_confirm=error.need_order_confirm,
                                   confirm_id=error.confirm_id,
                                   jump_url=error.jump_url,
                                   retry_after=error.retry_after) from error
            raise

    #: 标的代码形状（官方 exchange.symbol，如 HK.00700/US.AAPL/SH.600519）
    _SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,6}\.[A-Za-z0-9._]+$")
    #: 标的代码进路径时的官方长度上限（锁定表 §C.1/§C.7：symbol ≤32 字符）
    SYMBOL_PATH_MAX = 32
    #: 官方「无数据」错误码（锁定表 §A 无数据语义）
    NO_DATA_ERRCODE = -10
    #: 官方「权限/用户身份」错误码（锁定表 §C.7/C.8/§D.3）
    PERMISSION_ERRCODE = -9


# ---------------------------------------------------------------------------
# OpenApiMarket：行情 REST 方法组（WP8 任务 2）
# ---------------------------------------------------------------------------
# 路径与参数逐项对照官方文档（2026-09-16 web_fetch 实抓，前缀 /api/v1.0/quote）：
#   realtime/market-snapshot … basic-data/search、capital-flow/*、derivatives/*、
#   screening/option-screen（各方法 docstring 里带精确路径与参数）。
# 约定：每个方法做参数白名单/类型/区间校验（本地 ValueError，消息面向调用方）→
# client.request(request_meta) → 返回信封 d。符号归一**不在这里**做（futu_data 层统一
# 走 market.to_futu_symbol，全仓库一份归一），这里只做形状校验。
# 三个分页端点（capital_flow_history/option_screen/history_kline）用 request_meta 把
# 信封顶层 pagination 并入返回值——与 MCP 通道 futu_mcp._unwrap 的形状一致，使
# futu_data 的双通道路由可以产出同形状响应（归一化 fixture 见 tests/test_wp8_market.py）。
class OpenApiMarket(_RestValidators):
    """富途行情 OpenAPI（REST）方法组：WP8 任务 2 的 OpenAPI 后端唯一入口。

    每个方法对应一个官方 REST 端点（方法名 = futu_data.OPENAPI_METHODS 的登记值）；
    参数名与官方文档一致（code_list/code/symbol/num/ktype/autype/...）。调用方必须
    先配好凭据（~/.dsh/futu-openapi.json，OAuth 或 AppKey），否则 client.request 抛
    OpenApiError。
    """

    #: 行情快照/报价/基本信息/市场状态批量上限（官方：单次最多 400 个 code）
    MAX_CODE_LIST = 400
    #: K 线单次条数上限（官方：num 默认 370、最大 370）
    MAX_KLINE_NUM = 370
    #: 买卖盘档数上限（官方：num 1..60）
    MAX_ORDER_BOOK_NUM = 60
    #: 逐笔成交条数上限（官方：num 默认 500、最大 750）
    MAX_TICKER_NUM = 750

    #: ktype 枚举（官方文档 cur-kline 页）：1=1分 2=日 3=周 4=月 5=年 6=5分 7=15分
    #: 8=30分 9=60分 10=3分 11=季 14=120分 15=240分 26=10分 29=180分
    KTYPE_VALUES = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15, 26, 29})
    #: autype 枚举：0=不复权 1=前复权 2=后复权 3=前复权含股息 4=后复权含股息
    AUTYPE_VALUES = frozenset({0, 1, 2, 3, 4})
    #: extended_time 枚举：0=默认 1=含盘前盘后（美股 1 分 K） 2=含夜盘
    EXTENDED_TIME_VALUES = frozenset({0, 1, 2})
    #: rt-data 的交易时段枚举（官方文档 rt-data 页，6 值；**仅 rt_data 用**）
    RT_SECTIONS = frozenset({"NORMAL", "FULL", "PREMARKET", "AFTERHOURS",
                             "HK_DARK", "OVERNIGHT"})
    #: capital-flow 的 section 枚举（官方 naming-dictionary#capital-flow-section，4 值）。
    #: **不是 RT_SECTIONS**：网关逐字返回 allowed:[NORMAL, FULL, PREMARKET, AFTERHOURS]
    #: （2026-09-16 实测 OVERNIGHT/HK_DARK → -3 invalid_parameter）；两者复用同一常量
    #: 会把 rt-data 的夜盘枚举漏进 capital-flow，是已复现的 live 缺陷。
    CAPITAL_FLOW_SECTIONS = frozenset({"NORMAL", "FULL", "PREMARKET", "AFTERHOURS"})
    #: rt-ticker 的时段过滤枚举
    TICKER_PERIODS = frozenset({"NORMAL", "BEFORE", "AFTER", "OVERNIGHT"})
    #: trading-days 的市场枚举（官方文档 trading-days 页）
    TRADING_MARKETS = frozenset({"HK", "US", "SH", "SZ", "BJ", "SG", "JP", "CA",
                                 "AU", "JP_FUTURE", "SG_FUTURE"})
    #: option-expiration / option-chain 的 filter_standard 枚举
    FILTER_STANDARDS = frozenset({"ALL", "STANDARD", "NON_STANDARD"})
    #: option-expiration 的 filter_expiration_cycles 取值（官方网关 pattern 逐字，
    #: 2026-09-16 实测；请求形态是**逗号分隔字符串**而非数组，见 _csv_enum）
    EXPIRATION_CYCLES = frozenset({"MONTH", "WEEK", "END_OF_MONTH", "QUARTERLY",
                                   "WEEKMON", "WEEKTUE", "WEEKWED", "WEEKTHU",
                                   "WEEKFRI"})
    #: capital-flow-history 的聚合周期
    FLOW_PERIOD_TYPES = frozenset({"DAY", "WEEK", "MONTH"})
    #: find-news 的资讯类型：1=资讯 2=公告 3=研报
    NEWS_TYPES = frozenset({1, 2, 3})
    #: find-news / find-community 排序：1=热度/阅读量 2=时间
    SEARCH_SORT_TYPES = frozenset({1, 2})
    #: find-news / find-community 语言过滤
    SEARCH_LANGS = frozenset({"zh-CN", "zh-HK", "en", "ja"})
    #: find-community 社区类型：1=讨论 2=话题 3=直播
    COMMUNITY_TYPES = frozenset({1, 2, 3})

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------ 校验助手
    # _codes/_int_in/_enum_in/_date/_body 五个通用校验器来自 _RestValidators
    # （与 OpenApiTrade 共用一份实现）；_merge_pagination 亦已提升至基类
    # （WP12 任务 2 起数据面方法组共用）。

    # ------------------------------------------------------------ 实时行情（realtime）

    def market_snapshot(self, code_list):
        """POST /api/v1.0/quote/snapshot —— 行情快照（批量 1..400，按品类分组字段）。"""
        return self.client.request("POST", "/api/v1.0/quote/snapshot",
                                   json_body={"code_list": self._codes(code_list,
                                                                       self.MAX_CODE_LIST)})

    def stock_quote(self, code_list):
        """POST /api/v1.0/quote/stock-quote —— 实时报价（轻量版快照）。"""
        return self.client.request("POST", "/api/v1.0/quote/stock-quote",
                                   json_body={"code_list": self._codes(code_list,
                                                                       self.MAX_CODE_LIST)})

    def order_book(self, code, num=None):
        """POST /api/v1.0/quote/order-book —— 买卖盘（num 1..60，缺省=权限档上限）。"""
        body = {"code": self._codes([code], 1)[0]}
        if num is not None:
            body["num"] = self._int_in(num, 1, self.MAX_ORDER_BOOK_NUM, "num")
        return self.client.request("POST", "/api/v1.0/quote/order-book", json_body=body)

    def cur_kline(self, symbol, num, ktype=2, autype=1, extended_time=0):
        """GET /api/v1.0/quote/{symbol}/cur-kline —— 当前 K 线（num 必填 1..370）。"""
        query = {
            "num": self._int_in(num, 1, self.MAX_KLINE_NUM, "num"),
            "ktype": self._enum_in(ktype, self.KTYPE_VALUES, "ktype", default=2),
            "autype": self._enum_in(autype, self.AUTYPE_VALUES, "autype", default=1),
            "extended_time": self._enum_in(extended_time, self.EXTENDED_TIME_VALUES,
                                           "extended_time", default=0),
        }
        return self.client.request("GET", f"/api/v1.0/quote/{symbol}/cur-kline",
                                   query=query)

    def rt_data(self, symbol, request_section="NORMAL"):
        """GET /api/v1.0/quote/{symbol}/rt-data —— 分时数据（时段枚举见 RT_SECTIONS）。"""
        query = {"request_section": self._enum_in(request_section, self.RT_SECTIONS,
                                                  "request_section", default="NORMAL")}
        return self.client.request("GET", f"/api/v1.0/quote/{symbol}/rt-data",
                                   query=query)

    def rt_ticker(self, symbol, num=500, period=None):
        """GET /api/v1.0/quote/{symbol}/rt-ticker —— 逐笔成交（num 1..750；period 列表）。

        ``period`` 是**同名多值**参数：``["BEFORE","AFTER"]`` 出站为
        ``?num=…&period=BEFORE&period=AFTER``（由 ``query_string`` 的 doseq 展开），
        绝不是 ``period=['BEFORE', 'AFTER']`` 的 Python repr（后者实测 -3）。
        """
        query = {"num": self._int_in(num, 1, self.MAX_TICKER_NUM, "num", default=500)}
        if period is not None:
            if not isinstance(period, list) or not period or \
                    any(item not in self.TICKER_PERIODS for item in period):
                raise ValueError(f"period 必须是 {sorted(self.TICKER_PERIODS)} 的非空列表")
            query["period"] = list(period)
        return self.client.request("GET", f"/api/v1.0/quote/{symbol}/rt-ticker",
                                   query=query)

    # ------------------------------------------------------------ 基本数据（basic-data）

    def stock_basicinfo(self, code_list):
        """POST /api/v1.0/quote/stock-basicinfo —— 标的基本静态信息（批量 1..400）。"""
        return self.client.request("POST", "/api/v1.0/quote/stock-basicinfo",
                                   json_body={"code_list": self._codes(code_list,
                                                                       self.MAX_CODE_LIST)})

    def trading_days(self, market, start, end):
        """GET /api/v1.0/quote/trading-days —— 交易日历（market/start/end 全必填）。"""
        query = {
            "market": self._enum_in(market, self.TRADING_MARKETS, "market"),
            "start": self._date(start, "start", required=True),
            "end": self._date(end, "end", required=True),
        }
        if query["start"] > query["end"]:
            raise ValueError("start 不能晚于 end")
        return self.client.request("GET", "/api/v1.0/quote/trading-days", query=query)

    def history_kline(self, symbol, end, start=None, ktype=2, autype=1,
                      num=370, extended_time=0):
        """GET /api/v1.0/quote/{symbol}/history-kline —— 历史 K 线（end 必填；num≤370）。

        信封顶层 pagination 并入返回值（向更早翻页游标，与 MCP 通道同形状）。
        """
        query = {
            "start": self._date(start, "start"),
            "end": self._date(end, "end", required=True),
            "ktype": self._enum_in(ktype, self.KTYPE_VALUES, "ktype", default=2),
            "autype": self._enum_in(autype, self.AUTYPE_VALUES, "autype", default=1),
            "num": self._int_in(num, 1, self.MAX_KLINE_NUM, "num", default=370),
            "extended_time": self._enum_in(extended_time, self.EXTENDED_TIME_VALUES,
                                           "extended_time", default=0),
        }
        d, pagination = self.client.request_meta(
            "GET", f"/api/v1.0/quote/{symbol}/history-kline", query=query)
        return self._merge_pagination(d, pagination)

    def market_state(self, code_list, is_contain_ba=None, is_contain_overnight=None):
        """POST /api/v1.0/quote/market-state —— 市场状态（批量 1..400，带市场前缀）。"""
        body = self._body({
            "code_list": self._codes(code_list, self.MAX_CODE_LIST),
            "is_contain_ba": is_contain_ba,
            "is_contain_overnight": is_contain_overnight,
        })
        for flag in ("is_contain_ba", "is_contain_overnight"):
            if flag in body and not isinstance(body[flag], bool):
                raise ValueError(f"{flag} 必须是布尔值")
        return self.client.request("POST", "/api/v1.0/quote/market-state",
                                   json_body=body)

    def search_news(self, symbol, size=10, news_type=None, sort_type=None, lang=None):
        """GET /api/v1.0/quote/find-news —— 资讯搜索（官方 search 页的资讯子接口）。"""
        query = {
            "symbol": self._keyword(symbol),
            "size": self._int_in(size, 1, 50, "size", default=10),
            "news_type": self._enum_in(news_type, self.NEWS_TYPES, "news_type",
                                       default=None),
            "sort_type": self._enum_in(sort_type, self.SEARCH_SORT_TYPES, "sort_type",
                                       default=None),
            "lang": self._enum_in(lang, self.SEARCH_LANGS, "lang", default=None),
        }
        return self.client.request("GET", "/api/v1.0/quote/find-news",
                                   query=self._body(query))

    def search_community(self, symbol, size=10, community_type=None, sort_type=None,
                         lang=None):
        """GET /api/v1.0/quote/find-community —— 社区搜索（search 页的社区子接口）。"""
        query = {
            "symbol": self._keyword(symbol),
            "size": self._int_in(size, 1, 50, "size", default=10),
            "community_type": self._enum_in(community_type, self.COMMUNITY_TYPES,
                                            "community_type", default=None),
            "sort_type": self._enum_in(sort_type, self.SEARCH_SORT_TYPES, "sort_type",
                                       default=None),
            "lang": self._enum_in(lang, self.SEARCH_LANGS, "lang", default=None),
        }
        return self.client.request("GET", "/api/v1.0/quote/find-community",
                                   query=self._body(query))

    def _keyword(self, symbol):
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol（搜索关键词）必须是非空字符串")
        return symbol

    # ------------------------------------------------------------ 资金（capital-flow）

    def capital_flow(self, symbol, section="NORMAL"):
        """GET /api/v1.0/quote/{symbol}/capital-flow —— 日内分钟级资金流。

        ``section`` 用 ``CAPITAL_FLOW_SECTIONS``（4 值），**不是** ``RT_SECTIONS``（6 值，
        含 rt-data 专有的 HK_DARK/OVERNIGHT，网关对 capital-flow 拒绝这两个值）。
        """
        query = {"section": self._enum_in(section, self.CAPITAL_FLOW_SECTIONS, "section",
                                          default="NORMAL")}
        return self.client.request("GET", f"/api/v1.0/quote/{symbol}/capital-flow",
                                   query=query)

    def capital_flow_history(self, symbol, period_type="DAY", start=None, end=None,
                             count=365):
        """GET /api/v1.0/quote/{symbol}/capital-flow/history —— 历史资金流（count 1..1000）。

        信封顶层 pagination 并入返回值（has_more，与 MCP 通道同形状）。
        """
        query = {
            "period_type": self._enum_in(period_type, self.FLOW_PERIOD_TYPES,
                                         "period_type", default="DAY"),
            "start": self._date(start, "start"),
            "end": self._date(end, "end"),
            "count": self._int_in(count, 1, 1000, "count", default=365),
        }
        d, pagination = self.client.request_meta(
            "GET", f"/api/v1.0/quote/{symbol}/capital-flow/history", query=query)
        return self._merge_pagination(d, pagination)

    def capital_distribution(self, symbol):
        """GET /api/v1.0/quote/{symbol}/capital-distribution —— 日内资金分布快照。"""
        return self.client.request("GET",
                                   f"/api/v1.0/quote/{symbol}/capital-distribution")

    # ------------------------------------------------------------ 衍生品 / 筛选

    def option_expiration(self, symbol, index_option_type=None,
                          filter_standard="ALL", filter_expiration_cycles=None):
        """GET /api/v1.0/quote/{symbol}/option-expiration —— 期权到期日列表。

        ``filter_expiration_cycles`` 官方类型是**逗号分隔字符串**（如 ``"WEEK,MONTH"``），
        传 list 本地拒绝（见 ``_csv_enum``）；若不拒，list 会出站成 Python repr 并被
        网关 -3 拒绝（已实测）。
        """
        query = self._body({
            "index_option_type": index_option_type,
            "filter_standard": self._enum_in(filter_standard, self.FILTER_STANDARDS,
                                             "filter_standard", default="ALL"),
            "filter_expiration_cycles": self._csv_enum(
                filter_expiration_cycles, self.EXPIRATION_CYCLES,
                "filter_expiration_cycles"),
        })
        return self.client.request("GET", f"/api/v1.0/quote/{symbol}/option-expiration",
                                   query=query)

    def option_chain(self, symbol, start=None, end=None, index_option_type=None,
                     filter_standard="ALL"):
        """GET /api/v1.0/quote/{symbol}/option-chain —— 期权链（单次最多 20 个到期日）。"""
        query = self._body({
            "start": self._date(start, "start"),
            "end": self._date(end, "end"),
            "index_option_type": index_option_type,
            "filter_standard": self._enum_in(filter_standard, self.FILTER_STANDARDS,
                                             "filter_standard", default="ALL"),
        })
        return self.client.request("GET", f"/api/v1.0/quote/{symbol}/option-chain",
                                   query=query)

    def option_screen(self, strategy, field_filter=None, sort_obj=None, next_key=None,
                      limit=None, request_exact_data=None, strategy_param=None):
        """POST /api/v1.0/quote/option-screen —— 期权筛选器（strategy 必填对象）。

        信封顶层 pagination 并入返回值（has_more/next_key/total，与 MCP 通道同形状）。
        """
        if not isinstance(strategy, dict) or not strategy:
            raise ValueError("strategy 必须是非空对象（如 {market_category_list: [1]}）")
        body = self._body({
            "strategy": strategy,
            "field_filter": field_filter,
            "sort_obj": sort_obj,
            "next_key": next_key,
            "limit": limit,
            "request_exact_data": request_exact_data,
            "strategy_param": strategy_param,
        })
        if "limit" in body:
            body["limit"] = self._int_in(body["limit"], 0, 1000, "limit")
        for name in ("field_filter", "sort_obj", "strategy_param"):
            if name in body and not isinstance(body[name], dict):
                raise ValueError(f"{name} 必须是对象")
        if "next_key" in body and not isinstance(body["next_key"], str):
            raise ValueError("next_key 必须是字符串（分页游标）")
        d, pagination = self.client.request_meta(
            "POST", "/api/v1.0/quote/option-screen", json_body=body)
        return self._merge_pagination(d, pagination)


# ---------------------------------------------------------------------------
# OpenApiTrade：交易 REST 方法组（WP8 任务 3）
# ---------------------------------------------------------------------------
# 13 个方法与官方路径/参数逐项对照（2026-09-16 web_fetch 实抓 .md 原文，前缀 /api/v1.0）：
#   下单        POST   /accounts/{acc_id}/orders            body {code! qty! side! order_type!
#                                                            time_in_force! price? session?
#                                                            aux_price? lot_type? remark?
#                                                            order_class? multi_leg_info?}
#   改单        PUT    /accounts/{acc_id}/orders/{order_id}  body {exchange! qty! price! aux_price?}
#   撤单        DELETE /accounts/{acc_id}/orders/{order_id}  query {exchange!}
#   二次确认    POST   /accounts/{acc_id}/order_confirm      body {confirm_id!}
#   最大可交易量 GET    /accounts/{acc_id}/acctradinginfo    query {code! order_type! price? order_id?}
#   未完成订单  GET    /accounts/{acc_id}/orders             query {trd_market! page_flag! page_size? 10..100}
#   历史订单    GET    /accounts/{acc_id}/orders_history     query {trd_market! page_flag! code? start?
#                                                            end? page_size? 10..100}
#   订单详情    POST   /accounts/{acc_id}/orders/detail      body {exchange! order_ids!（<50 个）}
#   当日成交    GET    /accounts/{acc_id}/order_fills        query {trd_market! page_flag! page_size? 10..100}
#   历史成交    GET    /accounts/{acc_id}/fills_history      query {trd_market! page_flag! code? start?
#                                                            end? page_size? 10..50}
#   授权账户    GET    /accounts/authorized_trd_accs         （无路径/查询参数）
#   账户资金    GET    /accounts/{acc_id}/funds              query {currency?}
#   持仓        GET    /accounts/{acc_id}/positions          query {code? pl_ratio_min? pl_ratio_max?}
#
# 信封：交易侧是 ``{"s":"ok","d":...}`` / ``{"s":"error","errcode","errmsg","jump_url"?,
# "need_order_confirm"?,"confirm_id"?}``（**行情侧是另一套**：``{"ret_code":0,"data":...,
# "pagination":...}``，见模块头与 parse_envelope_meta——两者不是同一信封），由 client.request
# 统一解析——need_order_confirm=true 抛 ``OrderConfirmRequired``（订单在券商侧**已挂起**，
# 调 order_confirm 放行；**禁止对原请求重发**）。既非 s 信封也非 ret_code 信封（5xx/429/
# 网关页/非 JSON）→ ``UnexpectedResponse``：**没有业务结论**，写路径落 unknown 先查询，
# 不得当业务拒绝。
#
# 文档与实现的差异登记（不猜，逐条给出源码依据）：
#   * funds 的 ``currency`` 在官方参数表标 Required=Yes，但同页 curl 示例未传该参数
#     → 本层按**可选**处理（按 Required 会与官方示例直接冲突），并在 docstring 登记。
#   * 改单官方明示「Does not support modifying A-share orders」→ **本层只做事实透传**
#     不在传输层预判市场（传输层只忠实实现文档契约；「A 股改单怎么办」是交易闸门的策略，
#     在 platform/server/trading.py 里决定并写明给调用方的替代路径）。
#   * exchange 官方枚举没有北交所（BJ）→ 本层不自行扩枚举（strict：未知值拒绝）；
#     北交所标的的撤单/改单因此会在此被拒（如实暴露，不伪造 exchange 值）。
#   * ``multi_leg_info`` 在 place-order 参数表标类型 ``MultiLegInfo``（对象），而
#     naming-dictionary 的 ``Order.multi_leg_info`` 是 ``list[MultiLegInfo]``（响应侧）
#     → 本层请求侧**对象与对象列表都接受、原样透传**，内键按 naming-dictionary 白名单
#     校验（多腿订单两种形态在真实通道的取舍以实测为准，见 tests/test_wp8_trading.py）。
#   * 时间戳参数（orders_history/fills_history 的 start/end）官方单位是**微秒**
#     → 本层只校验非负整数，不做单位换算（调用方给什么传什么）。
#   * ``positions`` 与 ``order_details`` 的响应 ``d`` 是**数组**（其余是对象）→
#     本层原样返回，不做形状包装。
class OpenApiTrade(_RestValidators):
    """富途交易 OpenAPI（REST）方法组：WP8 任务 3 的 OpenAPI 交易后端唯一入口。

    每个方法对应一个官方 REST 端点；参数名与官方文档一致（code/qty/price/side/
    order_type/time_in_force/session/aux_price/lot_type/remark/order_class/
    multi_leg_info/trd_market/page_flag/page_size/exchange/order_ids/...）。调用方必须
    先配好凭据（``~/.futu-openapi.json``，OAuth 或 AppKey），否则 client.request 抛
    ``OpenApiError``。参数白名单 = 方法签名（未知关键字 Python 直接 TypeError），
    枚举/区间/必填在下单前本地拒绝（ValueError），坏参数零网络往返。
    """

    #: side 枚举（naming-dictionary#trd-side；NONE=未知，不作为下单值）
    SIDES = frozenset({"BUY", "SELL", "SELL_SHORT", "BUY_BACK"})
    #: order_type 枚举（naming-dictionary#order-type；NONE=未知，不作为下单值）
    ORDER_TYPES = frozenset({"LIMIT", "MARKET", "AUCTION", "AUCTION_LIMIT", "STOP",
                             "STOP_LIMIT", "MARKET_IF_TOUCHED", "LIMIT_IF_TOUCHED"})
    #: 必须带触发价的订单类型（place-order 页：aux_price required when order type is ...）
    AUX_PRICE_ORDER_TYPES = frozenset({"STOP", "STOP_LIMIT", "MARKET_IF_TOUCHED",
                                       "LIMIT_IF_TOUCHED"})
    #: time_in_force 枚举（naming-dictionary#time-in-force；NONE=未知）
    TIME_IN_FORCE = frozenset({"DAY", "GTC"})
    #: 美股交易时段枚举（naming-dictionary#trading-session；NONE=未知）——仅美股适用
    SESSIONS = frozenset({"RTH", "RTH+Pre/Post-Mkt", "OVERNIGHT", "ALL_DAY"})
    #: lot_type 枚举（naming-dictionary#lot-type；仅港股适用）
    LOT_TYPES = frozenset({"ODD", "ROUND"})
    #: order_class 枚举（naming-dictionary#order-class）
    ORDER_CLASSES = frozenset({"NORMAL", "MLEG"})
    #: exchange 枚举（naming-dictionary#exchange）
    EXCHANGES = frozenset({"US", "SEHK", "SGX", "SSE", "SZSE", "JP", "CA", "CME",
                           "CBOT", "NYMEX", "COMEX", "CBOE", "HKFE", "KR"})
    #: trd_market 枚举（naming-dictionary#trd-market；NONE=未知）
    TRD_MARKETS = frozenset({"HK", "US", "SG", "HKCC", "CA", "FUTURES", "JP", "KR"})
    #: currency 枚举（naming-dictionary#currency；NONE=未知）
    CURRENCIES = frozenset({"HKD", "USD", "CNH", "JPY", "SGD", "KRW"})
    #: security_type 枚举（naming-dictionary#security-type）
    SECURITY_TYPES = frozenset({"STOCK", "OPTION", "FUTURES", "MULTILEG_OPTION"})
    #: option_strategy 枚举（naming-dictionary#option-strategy；拼写 CalenderSpread 为官方原文）
    OPTION_STRATEGIES = frozenset({
        "Covered", "VerticalSpread", "Straddle", "Strangle", "Collar", "Butterfly",
        "Condor", "IronButterfly", "IronCondor", "CalenderSpread", "DiagonalSpread",
        "Customize"})
    #: MultiLegInfo / OrderLegInfo 的字段白名单（naming-dictionary）
    MULTI_LEG_KEYS = ("option_strategy", "underlying_symbol", "underlying_stock_name",
                      "leg_infos")
    MULTI_LEG_REQUIRED = ("option_strategy", "underlying_symbol", "leg_infos")
    LEG_KEYS = ("leg_symbol", "leg_exchange", "leg_ratio_qty", "leg_side",
                "leg_security_type", "leg_stock_name", "leg_hp_multiplier",
                "leg_avg_fill_price")
    LEG_REQUIRED = ("leg_symbol", "leg_exchange", "leg_ratio_qty", "leg_side",
                    "leg_security_type")
    #: remark 的 UTF-8 字节上限（place-order 页：maximum length 64 bytes）
    REMARK_MAX_BYTES = 64
    #: order_ids 数量上限（order detail 页：length should be less than 50）
    MAX_ORDER_IDS = 49
    #: 分页 page_size 区间（orders/orders_history/order_fills: 10-100；
    #: fills_history: 10-50 —— 官方两页给的上界不同，各自钉死）
    PAGE_SIZE_RANGE = (10, 100)
    PAGE_SIZE_HISTORY_DEALS = (10, 50)

    # _SYMBOL_RE 由 _RestValidators 提供（WP12 任务 2 起共用一份实现）

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------ 校验助手（交易专有）

    def _acc_id(self, value):
        """acc_id 进路径：非空字符串且不含路径分隔符/空白（避免拼出意外路径）。"""
        return self._path_token(value, "acc_id")

    # _path_token / _symbol / _text 由 _RestValidators 提供（WP12 任务 2 起共用一份实现）

    def _micros(self, value, name):
        """微秒时间戳：非负整数（官方单位微秒，本层不换算）。"""
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} 必须是非负整数（微秒时间戳）")
        return value

    def _page_flag(self, value):
        """page_flag 必填但可为空串（空串=从头开始，官方原文）。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("page_flag 必须是字符串（空串=从头开始）")
        return value

    def _page_size(self, value, bounds):
        if value is None:
            return None
        return self._int_in(value, bounds[0], bounds[1], "page_size")

    def _remark(self, value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("remark 必须是字符串")
        if len(value.encode("utf-8")) > self.REMARK_MAX_BYTES:
            raise ValueError(f"remark 的 UTF-8 长度不得超过 {self.REMARK_MAX_BYTES} 字节")
        return value

    def _multi_leg(self, value):
        """多腿信息内键白名单 + 必填校验（对象或对象列表都接受，原样透传）。"""
        if value is None:
            return None
        items = value if isinstance(value, list) else [value]
        if not items:
            raise ValueError("multi_leg_info 不能为空")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("multi_leg_info 必须是对象或对象列表")
            unknown = sorted(set(item) - set(self.MULTI_LEG_KEYS))
            if unknown:
                raise ValueError(f"multi_leg_info 含未支持字段：{unknown}")
            for field in self.MULTI_LEG_REQUIRED:
                if item.get(field) in (None, ""):
                    raise ValueError(f"multi_leg_info.{field} 必填")
            self._enum_in(item["option_strategy"], self.OPTION_STRATEGIES,
                          "multi_leg_info.option_strategy")
            legs = item["leg_infos"]
            if not isinstance(legs, list) or not legs:
                raise ValueError("multi_leg_info.leg_infos 必须是至少一条腿的列表")
            for leg in legs:
                if not isinstance(leg, dict):
                    raise ValueError("multi_leg_info.leg_infos 元素必须是对象")
                unknown = sorted(set(leg) - set(self.LEG_KEYS))
                if unknown:
                    raise ValueError(f"multi_leg_info.leg_infos 含未支持字段：{unknown}")
                for field in self.LEG_REQUIRED:
                    if leg.get(field) in (None, ""):
                        raise ValueError(f"multi_leg_info.leg_infos.{field} 必填")
                self._enum_in(leg["leg_exchange"], self.EXCHANGES,
                              "multi_leg_info.leg_infos.leg_exchange")
                self._enum_in(leg["leg_side"], self.SIDES,
                              "multi_leg_info.leg_infos.leg_side")
                self._enum_in(leg["leg_security_type"], self.SECURITY_TYPES,
                              "multi_leg_info.leg_infos.leg_security_type")
        return value

    # ------------------------------------------------------------ 交易（Trade）

    def place_order(self, acc_id, code, qty, side, order_type, time_in_force,
                    price=None, session=None, aux_price=None, lot_type=None,
                    remark=None, order_class=None, multi_leg_info=None):
        """POST /api/v1.0/accounts/{acc_id}/orders —— 下单（官方 place-order 页）。

        需要二次确认时信封抛 ``OrderConfirmRequired``（含 confirm_id/jump_url）：订单在
        券商侧**已挂起**，调用方须调 order_confirm 完成；**禁止对原请求重发**（会重复下单）。
        """
        body = self._body({
            "code": self._symbol(code),
            "qty": self._text(qty, "qty"),
            "side": self._enum_in(side, self.SIDES, "side"),
            "order_type": self._enum_in(order_type, self.ORDER_TYPES, "order_type"),
            "time_in_force": self._enum_in(time_in_force, self.TIME_IN_FORCE,
                                           "time_in_force"),
            "price": self._text(price, "price", required=False),
            "session": self._enum_in(session, self.SESSIONS, "session", default=None),
            "aux_price": self._text(aux_price, "aux_price", required=False),
            "lot_type": self._enum_in(lot_type, self.LOT_TYPES, "lot_type", default=None),
            "remark": self._remark(remark),
            "order_class": self._enum_in(order_class, self.ORDER_CLASSES,
                                         "order_class", default=None),
            "multi_leg_info": self._multi_leg(multi_leg_info),
        })
        if body["order_type"] in self.AUX_PRICE_ORDER_TYPES and "aux_price" not in body:
            raise ValueError(
                f"order_type={body['order_type']} 时 aux_price 必填（官方触发价规则）")
        if body.get("order_class") == "MLEG" and "multi_leg_info" not in body:
            raise ValueError("order_class=MLEG 时 multi_leg_info 必填（官方多腿订单规则）")
        return self.client.request("POST",
                                   f"/api/v1.0/accounts/{self._acc_id(acc_id)}/orders",
                                   json_body=body)

    def modify_order(self, acc_id, order_id, exchange, qty, price, aux_price=None):
        """PUT /api/v1.0/accounts/{acc_id}/orders/{order_id} —— 改单（官方 modify-order 页）。

        官方明示**不支持改 A 股订单**（Does not support modifying A-share orders）：本层
        忠实透传不做市场预判，拒绝/回退策略由交易闸门决定（platform/server/trading.py）。
        """
        body = self._body({
            "exchange": self._enum_in(exchange, self.EXCHANGES, "exchange"),
            "qty": self._text(qty, "qty"),
            "price": self._text(price, "price"),
            "aux_price": self._text(aux_price, "aux_price", required=False),
        })
        return self.client.request(
            "PUT", f"/api/v1.0/accounts/{self._acc_id(acc_id)}"
                   f"/orders/{self._path_token(order_id, 'order_id')}",
            json_body=body)

    def cancel_order(self, acc_id, order_id, exchange):
        """DELETE /api/v1.0/accounts/{acc_id}/orders/{order_id}?exchange=... —— 撤单。"""
        query = {"exchange": self._enum_in(exchange, self.EXCHANGES, "exchange")}
        return self.client.request(
            "DELETE", f"/api/v1.0/accounts/{self._acc_id(acc_id)}"
                      f"/orders/{self._path_token(order_id, 'order_id')}",
            query=query)

    def order_confirm(self, acc_id, confirm_id):
        """POST /api/v1.0/accounts/{acc_id}/order_confirm —— 券商侧二次确认。

        下单/改单返回 ``need_order_confirm=true`` 后**唯一**的放行方式；成功后返回
        ``{"order_id": ...}``。对同一 confirm_id 重复调用由券商侧语义兜底（本层不重试）。
        """
        return self.client.request(
            "POST", f"/api/v1.0/accounts/{self._acc_id(acc_id)}/order_confirm",
            json_body={"confirm_id": self._text(confirm_id, "confirm_id")})

    def max_trade_qty(self, acc_id, code, order_type, price=None, order_id=None):
        """GET /api/v1.0/accounts/{acc_id}/acctradinginfo —— 最大可交易量。

        带 order_id 时查该订单的最大可改数量（官方要求两次查询间隔 > 0.5s——本层不睡眠、
        不重试，节奏由调用方控制）。
        """
        query = self._body({
            "code": self._symbol(code),
            "order_type": self._enum_in(order_type, self.ORDER_TYPES, "order_type"),
            "price": self._text(price, "price", required=False),
            "order_id": self._text(order_id, "order_id", required=False),
        })
        return self.client.request(
            "GET", f"/api/v1.0/accounts/{self._acc_id(acc_id)}/acctradinginfo",
            query=query)

    # ------------------------------------------------------------ 订单（Order）

    def open_orders(self, acc_id, trd_market, page_flag="", page_size=None):
        """GET /api/v1.0/accounts/{acc_id}/orders —— 未完成订单（含最近 24h 已成交/已撤）。

        响应 ``d``：``{orders: [Order], page_flag: str, completed: bool}``。
        """
        query = self._body({
            "trd_market": self._enum_in(trd_market, self.TRD_MARKETS, "trd_market"),
            "page_flag": self._page_flag(page_flag),
            "page_size": self._page_size(page_size, self.PAGE_SIZE_RANGE),
        })
        return self.client.request(
            "GET", f"/api/v1.0/accounts/{self._acc_id(acc_id)}/orders", query=query)

    def history_orders(self, acc_id, trd_market, page_flag="", code=None, start=None,
                       end=None, page_size=None):
        """GET /api/v1.0/accounts/{acc_id}/orders_history —— 历史订单。

        start/end 是**创建时间**的微秒时间戳；官方组合语义（0/0 → 近 90 天）由服务端处理，
        本层不补默认窗口（传什么是什么）。
        """
        query = self._body({
            "trd_market": self._enum_in(trd_market, self.TRD_MARKETS, "trd_market"),
            "page_flag": self._page_flag(page_flag),
            "code": self._symbol(code) if code is not None else None,
            "start": self._micros(start, "start"),
            "end": self._micros(end, "end"),
            "page_size": self._page_size(page_size, self.PAGE_SIZE_RANGE),
        })
        return self.client.request(
            "GET", f"/api/v1.0/accounts/{self._acc_id(acc_id)}/orders_history",
            query=query)

    def order_details(self, acc_id, exchange, order_ids):
        """POST /api/v1.0/accounts/{acc_id}/orders/detail —— 订单详情（同 exchange 批量）。

        响应 ``d`` 是数组（Order 列表）。order_ids 少于 50 个（官方原文）。
        """
        if not isinstance(order_ids, list) or not 1 <= len(order_ids) <= self.MAX_ORDER_IDS:
            raise ValueError(f"order_ids 必须是 1..{self.MAX_ORDER_IDS} 个订单号的列表")
        ids = [self._path_token(order_id, "order_ids") for order_id in order_ids]
        body = {
            "exchange": self._enum_in(exchange, self.EXCHANGES, "exchange"),
            "order_ids": ids,
        }
        return self.client.request(
            "POST", f"/api/v1.0/accounts/{self._acc_id(acc_id)}/orders/detail",
            json_body=body)

    # ------------------------------------------------------------ 成交（Deal）

    def today_deals(self, acc_id, trd_market, page_flag="", page_size=None):
        """GET /api/v1.0/accounts/{acc_id}/order_fills —— 当日成交。

        响应 ``d``：``{order_fills: [OrderFill], page_flag: str, completed: bool}``。
        """
        query = self._body({
            "trd_market": self._enum_in(trd_market, self.TRD_MARKETS, "trd_market"),
            "page_flag": self._page_flag(page_flag),
            "page_size": self._page_size(page_size, self.PAGE_SIZE_RANGE),
        })
        return self.client.request(
            "GET", f"/api/v1.0/accounts/{self._acc_id(acc_id)}/order_fills", query=query)

    def history_deals(self, acc_id, trd_market, page_flag="", code=None, start=None,
                      end=None, page_size=None):
        """GET /api/v1.0/accounts/{acc_id}/fills_history —— 历史成交。

        start/end 是**更新时间**的微秒时间戳（与 history_orders 的创建时间不同）；
        page_size 上界 50（官方 fills_history 页，与 orders 页的 100 不同）。
        """
        query = self._body({
            "trd_market": self._enum_in(trd_market, self.TRD_MARKETS, "trd_market"),
            "page_flag": self._page_flag(page_flag),
            "code": self._symbol(code) if code is not None else None,
            "start": self._micros(start, "start"),
            "end": self._micros(end, "end"),
            "page_size": self._page_size(page_size, self.PAGE_SIZE_HISTORY_DEALS),
        })
        return self.client.request(
            "GET", f"/api/v1.0/accounts/{self._acc_id(acc_id)}/fills_history",
            query=query)

    # ------------------------------------------------------------ 账户（Account）

    def authorized_accounts(self):
        """GET /api/v1.0/accounts/authorized_trd_accs —— 授权交易账户（无参数）。

        响应 ``d``：``{accounts: [Account]}``；Account 含 ``account_id`` / ``security_firm``
        / ``enable_market``（list[int]）/ ``acc_type`` 等。
        """
        return self.client.request("GET", "/api/v1.0/accounts/authorized_trd_accs")

    def account_funds(self, acc_id, currency=None):
        """GET /api/v1.0/accounts/{acc_id}/funds —— 账户资金（净资产/购买力等）。

        官方参数表把 ``currency`` 标成必填，但同页 curl 示例未传 → 本层按可选处理
        （登记见文件头差异清单）；该参数只对期货/综合证券账户生效。
        """
        query = self._body({
            "currency": self._enum_in(currency, self.CURRENCIES, "currency",
                                      default=None),
        })
        return self.client.request(
            "GET", f"/api/v1.0/accounts/{self._acc_id(acc_id)}/funds", query=query)

    def positions(self, acc_id, code=None, pl_ratio_min=None, pl_ratio_max=None):
        """GET /api/v1.0/accounts/{acc_id}/positions —— 持仓（响应 ``d`` 是数组）。

        盈亏比例过滤（pl_ratio_min/pl_ratio_max）官方是字符串百分数（如 "10" 表示 ≥+10%）。
        """
        low = self._text(pl_ratio_min, "pl_ratio_min", required=False)
        high = self._text(pl_ratio_max, "pl_ratio_max", required=False)
        if low is not None and high is not None:
            try:
                low_value, high_value = float(low), float(high)
            except ValueError:
                raise ValueError("pl_ratio_min/pl_ratio_max 必须是数值字符串") from None
            if low_value > high_value:
                raise ValueError("pl_ratio_min 不能大于 pl_ratio_max")
        query = self._body({
            "code": self._symbol(code) if code is not None else None,
            "pl_ratio_min": low,
            "pl_ratio_max": high,
        })
        return self.client.request(
            "GET", f"/api/v1.0/accounts/{self._acc_id(acc_id)}/positions", query=query)


# ---------------------------------------------------------------------------
# WP12 任务 2：富途数据面传输方法组（筛选/板块/做空/基础数据/IPO/自选/衍生品）
# ---------------------------------------------------------------------------
# 路径、参数名、枚举、区间与错误码语义**逐项对照锁定表**
# `docs/superpowers/plans/wp12-endpoint-lock.md`（2026-09-16 官方文档逐页核对，53 条目标；
# llms.txt 的 F10 链接已实测 404，严禁按 llms.txt 猜路径）。纪律与既有方法组同构：
#   * 方法签名即参数白名单（未知关键字 Python 直接 TypeError）；
#   * 枚举/区间/结构本地校验（坏参数**零网络往返**）；
#   * 每个方法只做「校验 + 一次 REST 调用 + envelope 解析」，**不做业务聚合**；
#   * 锁定表未给出枚举/区间的字段**不自行发明**（`_scalar` 直通，服务端 -3/-5 如实抛出）；
#   * `-10 no_data` → 空而非错；`-9` → 如实抛出且可读（基类 `_tolerate_no_data`
#     / `_permission_note`）。
#
# 路径常量集中声明，并由文件末尾 WP12_TRANSPORT_ENDPOINTS 与锁定表绑定
# （tests/test_wp12_transport.py 逐条比对——防路径漂移与「猜路径」回流）。
STOCK_SCREEN_PATH = "/api/v1.0/quote/stock-screen"
WARRANT_SCREEN_PATH = "/api/v1.0/quote/warrant-screen"
PLATE_LIST_PATH = "/api/v1.0/quote/plate-list"
PLATE_STOCK_PATH = "/api/v1.0/quote/plate-stock"
SHORT_DAILY_VOLUME_PATH = "/api/v1.0/quote/{symbol}/short/daily-volume"
SHORT_INTEREST_PATH = "/api/v1.0/quote/{symbol}/short/interest"
ECONOMIC_CALENDAR_HOT_PATH = "/api/v1.0/quote/economic-calendar/hot"
ECONOMIC_CALENDAR_SEARCH_PATH = "/api/v1.0/quote/economic-calendar/search"
OWNER_PLATE_PATH = "/api/v1.0/quote/{symbol}/owner-plate"
REHAB_PATH = "/api/v1.0/quote/{symbol}/corporate-actions/rehab"
IPO_LIST_PATH = "/api/v1.0/quote/ipo-list/{market}"
WATCHLIST_LIST_PATH = "/api/v1.0/quote/user-security"
WATCHLIST_GROUPS_PATH = "/api/v1.0/quote/user-security-group"
MODIFY_USER_SECURITY_PATH = "/api/v1.0/quote/modify-user-security"
FUTURE_INFO_PATH = "/api/v1.0/quote/future-info"
REFERENCE_FUTURE_PATH = "/api/v1.0/quote/{symbol}/reference-future"
OPTION_VOLATILITY_PATH = "/api/v1.0/quote/{symbol}/option-volatility"
OPTION_EXERCISE_PROBABILITY_PATH = "/api/v1.0/quote/{symbol}/option-exercise-probability"


class OpenApiScreen(_RestValidators):
    """全市场筛选方法组（锁定表 §C.3）：条件选股与窝轮筛选器。

    官方筛选是「条件数组 + 取值数组」模型：``screen_queries`` 过滤、``retrieve_queries``
    决定每个命中标的返回哪些列（顺序一一对应）。本层只校验**结构**（每元素恰好一个
    白名单键）；property name / 具体枚举值语义属网关判定（-5），锁定表未给全量字段字典，
    故不在此自行枚举。
    """

    #: screen_queries 的「11 选 1」查询类型（锁定表 §C.3）
    SCREEN_QUERY_KEYS = frozenset({
        "simple_field_query", "plate_query", "simple_property_query",
        "cumulative_property_query", "financial_property_query",
        "indicator_positional_query", "indicator_pattern_query",
        "featured_property_query", "broker_holdings_query", "kline_shape_query",
        "option_query"})
    #: retrieve_queries 的「9 选 1」取值类型（锁定表 §C.3）
    RETRIEVE_QUERY_KEYS = frozenset({
        "basic_property", "simple_property", "cumulative_property",
        "financial_property", "featured_property", "indicator_property",
        "broker_property", "kline_shape_property", "option_property"})
    #: sort/sorts 的 direction：1=升序 2=降序 3=绝对值升序 4=绝对值降序
    SORT_DIRECTIONS = frozenset({1, 2, 3, 4})
    #: user_stock_list_mode：0=不限制 1=自选范围 2=持仓范围
    USER_STOCK_LIST_MODES = frozenset({0, 1, 2})
    #: stock-screen 单页上限（官方 limit 默认 200、最大 300）
    SCREEN_LIMIT_MAX = 300
    #: warrant-screen 的 market_type 枚举（锁定表 §C.3）
    WARRANT_MARKET_TYPES = frozenset({1, 4, 15})
    #: warrant-screen 单页上限
    WARRANT_LIMIT_MAX = 1000

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------ 校验助手（筛选专有）

    def _query_list(self, value, allowed, name, required=True):
        """「N 选 1」查询数组：每元素是恰好一个白名单键的对象。"""
        if value is None:
            if required:
                raise ValueError(f"{name} 必填（查询对象数组，每元素恰好一个查询类型）")
            return None
        if not isinstance(value, list) or not value:
            raise ValueError(f"{name} 必须是非空数组，得到：{value!r}")
        for item in value:
            if not isinstance(item, dict) or len(item) != 1:
                raise ValueError(
                    f"{name} 每个元素必须是恰好一个查询类型的对象"
                    f"（允许：{sorted(allowed)}）")
            key = next(iter(item))
            if key not in allowed:
                raise ValueError(f"{name} 含未支持查询类型：{key!r}"
                                 f"（允许：{sorted(allowed)}）")
        return list(value)

    def _sort_obj(self, value, name):
        """单字段排序对象：``{direction, <property_type>: {...}}``。"""
        if value is None:
            return None
        if not isinstance(value, dict) or not value:
            raise ValueError(f"{name} 必须是非空对象（如 {{direction: 2, "
                             f"simple_property: {{name: 2301}}}}）")
        if "direction" in value:
            self._enum_in(value["direction"], self.SORT_DIRECTIONS,
                          f"{name}.direction")
        return value

    def _sort_list(self, value, name):
        """多字段排序数组（元素同 ``_sort_obj``）。"""
        if value is None:
            return None
        if not isinstance(value, list) or not value:
            raise ValueError(f"{name} 必须是非空数组（元素为排序对象）")
        for item in value:
            self._sort_obj(item, f"{name} 元素")
        return list(value)

    def _id_list(self, value, name):
        """stock_id 整数数组（watchlist_stock_ids / holding_stock_ids）。"""
        if value is None:
            return None
        if not isinstance(value, list) or not value:
            raise ValueError(f"{name} 必须是非空整数数组")
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                raise ValueError(f"{name} 元素必须是非负整数，得到：{item!r}")
        return list(value)

    # ------------------------------------------------------------ 筛选

    def stock_screen(self, screen_queries, retrieve_queries=None, sort=None, sorts=None,
                     next_key=None, limit=None, watchlist_stock_ids=None,
                     holding_stock_ids=None, user_stock_list_mode=None):
        """POST /api/v1.0/quote/stock-screen —— 条件选股（锁定表 §C.3）。

        信封顶层 pagination（total/has_more/next_key）并入返回值。限制：``broker_holdings_query``
        / ``kline_shape_query`` 仅 HK；``option_query`` 需标的有期权（服务端判定）。
        """
        body = self._body({
            "screen_queries": self._query_list(screen_queries, self.SCREEN_QUERY_KEYS,
                                               "screen_queries"),
            "retrieve_queries": self._query_list(retrieve_queries,
                                                 self.RETRIEVE_QUERY_KEYS,
                                                 "retrieve_queries", required=False),
            "sort": self._sort_obj(sort, "sort"),
            "sorts": self._sort_list(sorts, "sorts"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, 1, self.SCREEN_LIMIT_MAX, "limit"),
            "watchlist_stock_ids": self._id_list(watchlist_stock_ids,
                                                 "watchlist_stock_ids"),
            "holding_stock_ids": self._id_list(holding_stock_ids, "holding_stock_ids"),
            "user_stock_list_mode": self._enum_in(user_stock_list_mode,
                                                  self.USER_STOCK_LIST_MODES,
                                                  "user_stock_list_mode", default=None),
        })
        d, pagination = self.client.request_meta("POST", STOCK_SCREEN_PATH,
                                                 json_body=body)
        return self._merge_pagination(d, pagination)

    def warrant_screen(self, market_type=None, is_delay=None, only_count=None,
                       stock_owner=None, screen_groups=None, sorts=None,
                       next_key=None, limit=None):
        """POST /api/v1.0/quote/warrant-screen —— 窝轮筛选器（锁定表 §C.3）。

        **数据端点**：平台策略/风控/执行不引入窝轮品类（规格 §1.1）；本方法只保证 API
        面完整。``market_type`` 1/4/15（锁定表枚举），``limit`` ≤1000。
        ``is_delay/only_count/screen_groups`` 锁定表**只给参数名、未给类型与枚举** →
        原样透传不发明（POST body 走 JSON 序列化，无查询串的多值序列化风险）。
        """
        body = self._body({
            "market_type": self._enum_in(market_type, self.WARRANT_MARKET_TYPES,
                                         "market_type", default=None),
            "is_delay": is_delay,
            "only_count": only_count,
            "stock_owner": self._text(stock_owner, "stock_owner", required=False),
            "screen_groups": screen_groups,
            "sorts": self._sort_list(sorts, "sorts"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, 1, self.WARRANT_LIMIT_MAX, "limit"),
        })
        d, pagination = self.client.request_meta("POST", WARRANT_SCREEN_PATH,
                                                 json_body=body)
        return self._merge_pagination(d, pagination)


class OpenApiPlate(_RestValidators):
    """板块方法组（锁定表 §C.2）：板块列表与板块成分股。

    ``market`` 官方未在本轮核对中给出枚举全量（只给「必填 + 命名市场」），故本层只校验
    **大写市场码形状**（``^[A-Z][A-Z0-9_]*$``），不发明枚举。``plate_class=REGION`` 仅
    SH/SZ 支持：该条件本地可判定，**前置拒绝**（官方 ``-8 unsupported``，零网络往返）。
    """

    #: plate_class 枚举（官方大小写敏感）
    PLATE_CLASSES = frozenset({"ALL", "INDUSTRY", "REGION", "CONCEPT", "OTHER"})
    #: REGION 板块仅支持的市场
    REGION_MARKETS = frozenset({"SH", "SZ"})

    def __init__(self, client):
        self.client = client

    def _market(self, value):
        """市场码：大写字母/下划线形状（不发明枚举，见类 docstring）。"""
        if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", value.strip()):
            raise ValueError(f"market 必须是大写市场码（如 HK/US/SH/SZ），得到：{value!r}")
        return value.strip()

    def plate_list(self, market, plate_class):
        """GET /api/v1.0/quote/plate-list —— 指定市场指定分类的板块列表（锁定表 §C.2）。"""
        market = self._market(market)
        plate_class = self._enum_in(plate_class, self.PLATE_CLASSES, "plate_class")
        if plate_class == "REGION" and market not in self.REGION_MARKETS:
            raise ValueError(f"plate_class=REGION 仅支持 {'/'.join(sorted(self.REGION_MARKETS))}"
                             f"（官方 -8 unsupported），得到 market={market}")
        return self.client.request("GET", PLATE_LIST_PATH,
                                   query={"market": market, "plate_class": plate_class})

    def plate_stock(self, plate_code, sort_field=None, ascend=None, price_type=None,
                    leverage_direction=None, leverage_multiple=None, next_key=None,
                    limit=None):
        """GET /api/v1.0/quote/plate-stock —— 板块成分股（锁定表 §C.2，pagination 并入）。

        ``sort_field/price_type/leverage_*`` 官方未给枚举/区间 → 标量直通（不发明）。
        """
        query = self._body({
            "plate_code": self._text(plate_code, "plate_code"),
            "sort_field": self._scalar(sort_field, "sort_field"),
            "ascend": self._scalar(ascend, "ascend"),
            "price_type": self._scalar(price_type, "price_type"),
            "leverage_direction": self._scalar(leverage_direction, "leverage_direction"),
            "leverage_multiple": self._scalar(leverage_multiple, "leverage_multiple"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._scalar(limit, "limit"),
        })
        d, pagination = self.client.request_meta("GET", PLATE_STOCK_PATH, query=query)
        return self._merge_pagination(d, pagination)


class OpenApiShort(_RestValidators):
    """卖空数据方法组（锁定表 §C.6）：每日卖空成交与空头持仓。

    仅港股/美股可卖空证券（官方 ``-8``）；该条件**前缀本地可判定** → 前置拒绝。
    ``-10 no_data`` 是官方明示「无卖空数据」→ 空而非错（``{"no_data": True}``）。
    HK 是成交维度、US 是持仓维度（官方口径，字段不同——本层原样透传不归一）。
    """

    #: 支持卖空数据的市场（官方：仅 HK/US 可卖空证券）
    SHORT_MARKETS = frozenset({"HK", "US"})
    #: count 上限（官方默认 30、最大 90）
    COUNT_MAX = 90

    def __init__(self, client):
        self.client = client

    def _short_symbol(self, symbol):
        code = self._symbol(symbol)
        market = code.split(".", 1)[0]
        if market not in self.SHORT_MARKETS:
            raise ValueError(f"卖空数据仅支持 {'/'.join(sorted(self.SHORT_MARKETS))} "
                             f"可卖空证券（官方 -8），得到：{code}")
        return code

    def short_daily_volume(self, symbol, count=None):
        """GET /api/v1.0/quote/{symbol}/short/daily-volume —— 每日卖空成交（锁定表 §C.6）。"""
        code = self._short_symbol(symbol)
        query = self._body({"count": self._opt_int(count, 1, self.COUNT_MAX, "count")})
        return self._tolerate_no_data(lambda: self.client.request(
            "GET", SHORT_DAILY_VOLUME_PATH.format(symbol=code), query=query))

    def short_interest(self, symbol, count=None):
        """GET /api/v1.0/quote/{symbol}/short/interest —— 空头持仓（锁定表 §C.6）。"""
        code = self._short_symbol(symbol)
        query = self._body({"count": self._opt_int(count, 1, self.COUNT_MAX, "count")})
        return self._tolerate_no_data(lambda: self.client.request(
            "GET", SHORT_INTEREST_PATH.format(symbol=code), query=query))


class OpenApiBasicData(_RestValidators):
    """基础数据方法组（锁定表 §C.1）：经济日历、所属板块、复权因子。"""

    #: economic-calendar/hot 的 limit 区间（官方 1..20）
    CALENDAR_LIMIT_MAX = 20
    #: economic-calendar/search 的 search_type 枚举（官方 1..4）
    CALENDAR_SEARCH_TYPES = frozenset({1, 2, 3, 4})

    def __init__(self, client):
        self.client = client

    def economic_calendar_hot(self, limit=None, next_key=None, date=None, timezone=None):
        """GET /api/v1.0/quote/economic-calendar/hot —— 热门经济事件（锁定表 §C.1）。"""
        query = self._body({
            "limit": self._opt_int(limit, 1, self.CALENDAR_LIMIT_MAX, "limit"),
            "next_key": self._text(next_key, "next_key", required=False),
            "date": self._date(date, "date"),
            "timezone": self._scalar(timezone, "timezone"),
        })
        d, pagination = self.client.request_meta("GET", ECONOMIC_CALENDAR_HOT_PATH,
                                                 query=query)
        return self._merge_pagination(d, pagination)

    def economic_calendar_search(self, keyword, search_type, limit=None,
                                 next_key=None, time_order_type=None):
        """GET /api/v1.0/quote/economic-calendar/search —— 经济事件搜索（锁定表 §C.1）。

        ``keyword`` 与 ``search_type`` 官方必填。``limit/time_order_type`` 官方未给区间
        → 标量直通（不发明）。
        """
        query = self._body({
            "keyword": self._text(keyword, "keyword"),
            "search_type": self._enum_in(search_type, self.CALENDAR_SEARCH_TYPES,
                                         "search_type"),
            "limit": self._scalar(limit, "limit"),
            "next_key": self._text(next_key, "next_key", required=False),
            "time_order_type": self._scalar(time_order_type, "time_order_type"),
        })
        d, pagination = self.client.request_meta("GET", ECONOMIC_CALENDAR_SEARCH_PATH,
                                                 query=query)
        return self._merge_pagination(d, pagination)

    def owner_plate(self, symbol):
        """GET /api/v1.0/quote/{symbol}/owner-plate —— 标的所属板块（锁定表 §C.1）。

        行业中性化的数据来源；需按 ``plate_type`` 过滤行业类、剔除概念板块（调用方口径）。
        """
        return self.client.request("GET", OWNER_PLATE_PATH.format(
            symbol=self._symbol_in_path(symbol)))

    def rehab(self, symbol, divi_mode=None):
        """GET /api/v1.0/quote/{symbol}/corporate-actions/rehab —— 复权因子（锁定表 §C.1）。

        **路径注意**：文档归属「基本数据」，REST 路径在 ``/corporate-actions/rehab``
        （锁定表 §C.1 的显式提醒）。``divi_mode`` 官方未列枚举 → 标量直通。
        """
        query = self._body({"divi_mode": self._scalar(divi_mode, "divi_mode")})
        return self.client.request("GET", REHAB_PATH.format(
            symbol=self._symbol_in_path(symbol)), query=query)


class OpenApiIpo(_RestValidators):
    """IPO 方法组（锁定表 §C.4）：每市场独立路径，market 为**小写**路径片段。"""

    #: 官方支持的市场（路径片段小写；jp 不在列）
    IPO_MARKETS = frozenset({"hk", "us", "cn", "sg", "my"})

    def __init__(self, client):
        self.client = client

    def ipo_list(self, market, request_type=None):
        """GET /api/v1.0/quote/ipo-list/{market} —— 新股列表（锁定表 §C.4）。

        ``market`` 官方取值**小写**（hk/us/cn/sg/my），是**每市场独立路径**的一部分：
        本层按文档取值严格匹配（与 plate_class/group_type 同口径的大小写敏感纪律），
        不做大小写归一——路径片段与官方枚举逐字一致。

        ``request_type`` 省略时**不代填**：官方各市场默认值不同（HK/US/SG/MY=11、A 股=4），
        省略即由服务端按市场套用默认，本层代填反而会固化错误默认。
        """
        if not isinstance(market, str) or market not in self.IPO_MARKETS:
            raise ValueError(f"market 取值之一：{sorted(self.IPO_MARKETS)}"
                             f"（官方每市场独立路径，小写），得到：{market!r}")
        query = self._body({
            "request_type": self._opt_int(request_type, 1, 11, "request_type"),
        })
        return self.client.request("GET", IPO_LIST_PATH.format(market=market), query=query)


class OpenApiWatchlist(_RestValidators):
    """自选方法组（锁定表 §C.8）：列表/分组只读 + 修改自选。

    ``-9``（用户身份缺失或无效）在三个方法上如实抛出且可读（本层带语义说明）。
    ``modify_user_security`` 是**写类**端点：实现在此，但**不进 MCP 工具面**
    （注册面由 WP12 任务 4 决定；规格 §7.2 的 HTTP-only 档）。
    """

    #: 分组类型枚举（官方大小写敏感）
    GROUP_TYPES = frozenset({"ALL", "CUSTOM", "SYSTEM"})
    #: group_name 官方长度上限
    GROUP_NAME_MAX = 100
    #: code_list 官方上限（修改自选）
    MODIFY_CODES_MAX = 200
    #: -9 的可读说明（锁定表 §C.8：用户身份缺失或无效）
    IDENTITY_NOTE = "用户身份缺失或无效（官方 errcode=-9）"

    def __init__(self, client):
        self.client = client

    def watchlist_list(self, group_name):
        """GET /api/v1.0/quote/user-security —— 自选股列表（锁定表 §C.8，group_name 必填）。"""
        query = {"group_name": self._text_max(group_name, "group_name",
                                              self.GROUP_NAME_MAX)}
        return self._permission_note(
            lambda: self.client.request("GET", WATCHLIST_LIST_PATH, query=query),
            self.IDENTITY_NOTE)

    def watchlist_groups(self, group_type=None):
        """GET /api/v1.0/quote/user-security-group —— 自选分组（锁定表 §C.8）。"""
        query = self._body({
            "group_type": self._enum_in(group_type, self.GROUP_TYPES, "group_type",
                                        default=None),
        })
        return self._permission_note(
            lambda: self.client.request("GET", WATCHLIST_GROUPS_PATH, query=query),
            self.IDENTITY_NOTE)

    def modify_user_security(self, op, code_list, group_name=None):
        """POST /api/v1.0/quote/modify-user-security —— 修改自选（锁定表 §C.8）。

        ``op`` 官方未列枚举（只说明非法值 -3）→ 非空字符串直通，不发明取值集合。
        """
        body = self._body({
            "op": self._text(op, "op"),
            "code_list": self._codes(code_list, self.MODIFY_CODES_MAX),
            "group_name": self._text_max(group_name, "group_name", self.GROUP_NAME_MAX,
                                         required=False),
        })
        return self._permission_note(
            lambda: self.client.request("POST", MODIFY_USER_SECURITY_PATH, json_body=body),
            self.IDENTITY_NOTE)


class OpenApiDerivatives(_RestValidators):
    """衍生品方法组（锁定表 §C.7）：期货信息、相关期货、期权波动率与行权概率。"""

    #: 期货批量上限（官方 code_list ≤400）
    FUTURE_CODES_MAX = 400
    #: option-volatility 的 query_time_period 区间与 hv_time_period 区间（官方）
    QUERY_TIME_PERIOD_RANGE = (1, 5)
    HV_TIME_PERIOD_RANGE = (5, 250)
    #: option-exercise-probability 的 limit 区间（官方 1..1000）
    EXERCISE_LIMIT_RANGE = (1, 1000)
    #: -9 的可读说明（锁定表 §C.7/§D.3：无期权数据查询权限）
    PERMISSION_NOTE = "无期权数据查询权限（官方 errcode=-9）"

    def __init__(self, client):
        self.client = client

    def future_info(self, code_list):
        """POST /api/v1.0/quote/future-info —— 期货合约信息（锁定表 §C.7，批量 ≤400）。"""
        return self.client.request("POST", FUTURE_INFO_PATH,
                                   json_body={"code_list": self._codes(
                                       code_list, self.FUTURE_CODES_MAX)})

    def reference_future(self, symbol):
        """GET /api/v1.0/quote/{symbol}/reference-future —— 相关期货（锁定表 §C.7）。"""
        return self.client.request("GET", REFERENCE_FUTURE_PATH.format(
            symbol=self._symbol_in_path(symbol)))

    def option_volatility(self, symbol, query_time_period=None, hv_time_period=None):
        """GET /api/v1.0/quote/{symbol}/option-volatility —— 隐含/历史波动率（锁定表 §C.7）。

        ``symbol`` 必须是**期权合约**（传正股由服务端 -3 拒绝）。``-10`` 无可用波动率
        → 空而非错。
        """
        query = self._body({
            "query_time_period": self._opt_int(query_time_period,
                                               *self.QUERY_TIME_PERIOD_RANGE,
                                               name="query_time_period"),
            "hv_time_period": self._opt_int(hv_time_period, *self.HV_TIME_PERIOD_RANGE,
                                            name="hv_time_period"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(lambda: self.client.request(
            "GET", OPTION_VOLATILITY_PATH.format(symbol=code), query=query))

    def option_exercise_probability(self, symbol, limit=None):
        """GET /api/v1.0/quote/{symbol}/option-exercise-probability —— 行权概率（§C.7）。

        该端点可能返回官方 ``-9``（用户无期权数据查询权限）：**如实抛出且可读**，
        绝不退化成空数据（锁定表 §D.3 的显式要求）。
        """
        query = self._body({
            "limit": self._opt_int(limit, *self.EXERCISE_LIMIT_RANGE, name="limit"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(lambda: self._permission_note(
            lambda: self.client.request(
                "GET", OPTION_EXERCISE_PROBABILITY_PATH.format(symbol=code),
                query=query),
            self.PERMISSION_NOTE))


# ---------------------------------------------------------------------------
# WP12 任务 3：OpenApiF10 —— 个股深度数据方法组（锁定表 §C.5 的 26 项）
# ---------------------------------------------------------------------------
# 官方已把 llms.txt 的扁平 ``f10/*`` 重组为 7 个命名空间（financials / research /
# valuation / corporate-actions / shareholders / company / top-brokers）；路径与参数名
# **逐项对照锁定表 §C.5**（2026-09-16 逐页核对；llms.txt 的 ``/f10/*.md`` 已实测全部 404，
# 严禁按 llms.txt 猜路径）。纪律与任务 2 七组同构：
#   * 方法签名即参数白名单（未知关键字 Python 直接 TypeError）；枚举/区间/必填本地校验，
#     坏参数**零网络往返**；每个方法只做「校验 + 一次 REST + envelope 解析」，不做业务聚合；
#   * 锁定表**未给出区间/枚举**的字段一律 ``_scalar`` 直通，**不发明上限**——按行处理：
#     例 ``insider_holders.limit`` 表内给了 ≤30，而 ``institutional.limit`` 未给 → 后者直通；
#   * 响应**原样透传**：``pub_trading_day`` / ``period_text`` / ``announced_at`` 等字段不解释、
#     不归一、不丢弃（PIT 钥匙的语义由落库层决定，传输层只搬运）；
#   * ``-10 no_data`` **仅在锁定表显式列出该语义的 section 上**转空（``_tolerate_no_data``）；
#     未列出的 section 与 ``-9``/``-3`` 等一律如实抛出，不重试、不吞错；
#   * 列出 ``next_key`` 的 section 走 ``request_meta`` 并并入信封 ``pagination``
#     （锁定表 §A 全局约定：游标不透明、来自 ``pagination.next_key``），未列出者不伪造分页。
# 本地可判定的 ``-8``（锁定表明示市场集合）前置拒绝：``earnings_price_history`` 仅
# HK/US/SH/SZ；``top_brokers``/``top_brokers_history`` 仅港股。其余 ``-8``（非正股/
# 品类不支持）取决于证券类型，代码字符串本地不可判定 → 交服务端判定并如实抛出。
F10_EARNINGS_PRICE_MOVE_PATH = "/api/v1.0/quote/{symbol}/financials/earnings-price-move"
F10_EARNINGS_PRICE_HISTORY_PATH = "/api/v1.0/quote/{symbol}/financials/earnings-price-history"
F10_STATEMENTS_PATH = "/api/v1.0/quote/{symbol}/financials/statements"
F10_REVENUE_BREAKDOWN_PATH = "/api/v1.0/quote/{symbol}/financials/revenue-breakdown"
F10_ANALYST_CONSENSUS_PATH = "/api/v1.0/quote/{symbol}/research/analyst-consensus"
F10_RATING_SUMMARY_PATH = "/api/v1.0/quote/{symbol}/research/rating-summary"
F10_MORNINGSTAR_PATH = "/api/v1.0/quote/{symbol}/research/morningstar"
F10_VALUATION_DETAIL_PATH = "/api/v1.0/quote/{symbol}/valuation/detail"
F10_VALUATION_PLATE_STOCKS_PATH = "/api/v1.0/quote/valuation/plate-stocks"
F10_VALUATION_INDEX_STOCKS_PATH = "/api/v1.0/quote/valuation/index-stocks"
F10_VALUATION_INDEX_STOCK_PLATES_PATH = "/api/v1.0/quote/valuation/index-stock-plates"
F10_DIVIDENDS_PATH = "/api/v1.0/quote/{symbol}/corporate-actions/dividends"
F10_BUYBACKS_PATH = "/api/v1.0/quote/{symbol}/corporate-actions/buybacks"
F10_SPLITS_PATH = "/api/v1.0/quote/{symbol}/corporate-actions/splits"
F10_SHAREHOLDERS_OVERVIEW_PATH = "/api/v1.0/quote/{symbol}/shareholders/overview"
F10_HOLDING_CHANGES_PATH = "/api/v1.0/quote/{symbol}/shareholders/holding-changes"
F10_HOLDER_DETAIL_PATH = "/api/v1.0/quote/{symbol}/shareholders/holder-detail"
F10_INSTITUTIONAL_PATH = "/api/v1.0/quote/{symbol}/shareholders/institutional"
F10_INSIDER_HOLDERS_PATH = "/api/v1.0/quote/{symbol}/shareholders/insider-holders"
F10_INSIDER_TRADES_PATH = "/api/v1.0/quote/{symbol}/shareholders/insider-trades"
F10_COMPANY_PROFILE_PATH = "/api/v1.0/quote/{symbol}/company/profile"
F10_COMPANY_EXECUTIVES_PATH = "/api/v1.0/quote/{symbol}/company/executives"
F10_COMPANY_EXECUTIVE_BACKGROUND_PATH = "/api/v1.0/quote/{symbol}/company/executive-background"
F10_COMPANY_OPERATIONAL_EFFICIENCY_PATH = ("/api/v1.0/quote/{symbol}/company/"
                                           "operational-efficiency")
F10_TOP_BROKERS_PATH = "/api/v1.0/quote/{symbol}/top-brokers"
F10_TOP_BROKERS_HISTORY_PATH = "/api/v1.0/quote/{symbol}/top-brokers-history"


class OpenApiF10(_RestValidators):
    """个股深度数据方法组（锁定表 §C.5，26 个 section）。

    统一分发入口 ``f10(symbol, section, **section_params)``：``section`` 是**枚举白名单**
    （未知 section 本地拒绝、零网络往返），分发到同名专用方法。

    ``symbol`` 的位置随 section 而变（锁定表逐行声明）：多数 section 是**路径片段**
    （``/quote/{symbol}/…``）；``valuation_plate_stocks`` / ``valuation_index_stocks`` /
    ``valuation_index_stock_plates`` 的 symbol 是**查询参数**（板块代码/指数代码，路径里
    没有 symbol）——本层按表放置，不做归一化猜测。
    """

    #: 26 个 section（锁定表 §C.5 逐行；未知取值本地拒绝）
    SECTIONS = frozenset({
        # 财务数据（4）
        "earnings_price_move", "earnings_price_history", "statements", "revenue_breakdown",
        # 研究（3）
        "analyst_consensus", "rating_summary", "morningstar",
        # 估值（4）
        "valuation_detail", "valuation_plate_stocks", "valuation_index_stocks",
        "valuation_index_stock_plates",
        # 公司行为（3）
        "dividends", "buybacks", "splits",
        # 股东持股（6）
        "shareholders_overview", "holding_changes", "holder_detail", "institutional",
        "insider_holders", "insider_trades",
        # 公司信息（4）
        "company_profile", "company_executives", "company_executive_background",
        "company_operational_efficiency",
        # 十大经纪商（2）
        "top_brokers", "top_brokers_history",
    })

    # ---- 逐行区间/枚举（锁定表 §C.5；表内未给出的字段一律不在此声明）----
    #: earnings-price-move 的 count：官方默认 10、最大 50
    COUNT_RANGE = (1, 50)
    #: overview_count：官方默认 8、最大 50（生效值 = min(overview_count, count)）
    OVERVIEW_COUNT_RANGE = (1, 50)
    #: statements 的 statement_type（官方 1~4）
    STATEMENT_TYPES = frozenset({1, 2, 3, 4})
    #: rating-summary 的 rating_dimension_type（官方 1 或 2）
    RATING_DIMENSION_TYPES = frozenset({1, 2})
    #: rating-summary 的 limit（官方 ≤20）
    RATING_LIMIT_RANGE = (1, 20)
    #: valuation-detail 的 valuation_type（官方 ∈[1,2,3]）
    VALUATION_TYPES = frozenset({1, 2, 3})
    #: valuation-detail 的 interval_type（官方 ∈[1..10]）
    INTERVAL_TYPE_RANGE = (1, 10)
    #: valuation/plate-stocks 的 limit（官方 1~50）
    PLATE_STOCKS_LIMIT_RANGE = (1, 50)
    #: buybacks 的 limit（官方 ≤50）
    BUYBACKS_LIMIT_RANGE = (1, 50)
    #: insider-holders 的 limit（官方 ≤30）
    INSIDER_HOLDERS_LIMIT_RANGE = (1, 30)
    #: insider-trades 的 limit（官方 ≤50）
    INSIDER_TRADES_LIMIT_RANGE = (1, 50)
    #: operational-efficiency 的 limit（官方 ≤100）与 financial_type（官方 ∈{7,102}）
    EFFICIENCY_LIMIT_RANGE = (1, 100)
    EFFICIENCY_FINANCIAL_TYPES = frozenset({7, 102})
    #: top-brokers-history 的 days_before（官方必填 1~365）
    DAYS_BEFORE_RANGE = (1, 365)
    #: earnings-price-history 支持的市场（官方 -8：市场不在 HK/US/SH/SZ）
    EARNINGS_HISTORY_MARKETS = frozenset({"HK", "US", "SH", "SZ"})
    #: 十大经纪商支持的市场（官方 -8：非港股）
    HK_ONLY_MARKETS = frozenset({"HK"})

    def __init__(self, client):
        self.client = client

    # ------------------------------------------------------------ 共用助手

    def _in_markets(self, symbol, markets, name):
        """市场白名单守卫（**本地可判定的 -8**：官方明示市场集合时前置拒绝）。"""
        code = self._symbol_in_path(symbol)
        market = code.split(".", 1)[0]
        if market not in markets:
            raise ValueError(f"{name} 仅支持 {'/'.join(sorted(markets))}"
                             f"（官方 -8），得到：{code}")
        return code

    def _page(self, path, query):
        """带游标的 GET：信封 pagination 并入返回值（锁定表 §A 分页约定）。"""
        d, pagination = self.client.request_meta("GET", path, query=query)
        return self._merge_pagination(d, pagination)

    def _get(self, path):
        """无查询参数的 GET（不传 query，避免伪造空查询）。"""
        return self.client.request("GET", path)

    # ------------------------------------------------------------ 分发

    def f10(self, symbol, section, **section_params):
        """按 section 分发到专用方法（未知 section 本地拒绝，零网络往返）。"""
        if section not in self.SECTIONS:
            raise ValueError(f"section 取值非法：{section!r}"
                             f"（允许：{sorted(self.SECTIONS)}）")
        return getattr(self, section)(symbol, **section_params)

    # ------------------------------------------------------------ 财务数据（4）

    def earnings_price_move(self, symbol, count=None, overview_count=None):
        """GET …/financials/earnings-price-move —— 财报日股价变动（锁定表 §C.5）。"""
        query = self._body({
            "count": self._opt_int(count, *self.COUNT_RANGE, name="count"),
            "overview_count": self._opt_int(overview_count, *self.OVERVIEW_COUNT_RANGE,
                                            name="overview_count"),
        })
        return self.client.request("GET", F10_EARNINGS_PRICE_MOVE_PATH.format(
            symbol=self._symbol_in_path(symbol)), query=query)

    def earnings_price_history(self, symbol):
        """GET …/financials/earnings-price-history —— 财报日历史（锁定表 §C.5）。

        ``-10``（无财报日股价）→ 空而非错；``-8`` 市场不在 HK/US/SH/SZ **本地可判定**
        → 前置拒绝。
        """
        code = self._in_markets(symbol, self.EARNINGS_HISTORY_MARKETS, "财报日股价")
        return self._tolerate_no_data(
            lambda: self._get(F10_EARNINGS_PRICE_HISTORY_PATH.format(symbol=code)))

    def statements(self, symbol, statement_type=None, financial_type=None,
                   currency_code=None, next_key=None, limit=None):
        """GET …/financials/statements —— 财务报表（锁定表 §C.5）。

        ``statement_type`` 官方 1~4；``financial_type``/``currency_code``/``limit``
        表内**未给枚举或区间** → 标量直通（不发明）。``-10`` 该报表期无数据 → 空而非错。
        """
        query = self._body({
            "statement_type": self._enum_in(statement_type, self.STATEMENT_TYPES,
                                            "statement_type", default=None),
            "financial_type": self._scalar(financial_type, "financial_type"),
            "currency_code": self._scalar(currency_code, "currency_code"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._scalar(limit, "limit"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(F10_STATEMENTS_PATH.format(symbol=code), query))

    def revenue_breakdown(self, symbol, date=None, financial_type=None,
                          currency_code=None):
        """GET …/financials/revenue-breakdown —— 营收构成（锁定表 §C.5）。

        ``date`` 官方 yyyy-MM-dd（本层含日历有效性校验）；``-10`` 无营收构成 → 空而非错。
        """
        query = self._body({
            "date": self._date(date, "date"),
            "financial_type": self._scalar(financial_type, "financial_type"),
            "currency_code": self._scalar(currency_code, "currency_code"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(lambda: self.client.request(
            "GET", F10_REVENUE_BREAKDOWN_PATH.format(symbol=code), query=query))

    # ------------------------------------------------------------ 研究（3）

    def analyst_consensus(self, symbol):
        """GET …/research/analyst-consensus —— 分析师共识（锁定表 §C.5）。"""
        return self._get(F10_ANALYST_CONSENSUS_PATH.format(
            symbol=self._symbol_in_path(symbol)))

    def rating_summary(self, symbol, rating_dimension_type=None, next_key=None,
                       limit=None):
        """GET …/research/rating-summary —— 评级汇总（锁定表 §C.5）。

        ``rating_dimension_type`` 官方 1 或 2；``limit`` 官方 ≤20。
        """
        query = self._body({
            "rating_dimension_type": self._enum_in(rating_dimension_type,
                                                   self.RATING_DIMENSION_TYPES,
                                                   "rating_dimension_type", default=None),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, *self.RATING_LIMIT_RANGE, name="limit"),
        })
        code = self._symbol_in_path(symbol)
        return self._page(F10_RATING_SUMMARY_PATH.format(symbol=code), query)

    def morningstar(self, symbol):
        """GET …/research/morningstar —— 晨星评级（锁定表 §C.5）。

        ``-10``（晨星无覆盖）→ 空而非错。
        """
        return self._tolerate_no_data(
            lambda: self._get(F10_MORNINGSTAR_PATH.format(
                symbol=self._symbol_in_path(symbol))))

    # ------------------------------------------------------------ 估值（4）

    def valuation_detail(self, symbol, valuation_type=None, interval_type=None):
        """GET …/valuation/detail —— 估值明细（锁定表 §C.5，两参数均官方枚举）。"""
        query = self._body({
            "valuation_type": self._enum_in(valuation_type, self.VALUATION_TYPES,
                                            "valuation_type", default=None),
            "interval_type": self._opt_int(interval_type, *self.INTERVAL_TYPE_RANGE,
                                           name="interval_type"),
        })
        return self.client.request("GET", F10_VALUATION_DETAIL_PATH.format(
            symbol=self._symbol_in_path(symbol)), query=query)

    def valuation_plate_stocks(self, symbol, valuation_type=None, next_key=None,
                               limit=None, sort_type=None, sort_id=None):
        """GET /quote/valuation/plate-stocks —— 板块估值成分股（锁定表 §C.5）。

        **``symbol`` 是查询参数（板块代码）**，路径无 symbol。``valuation_type``/
        ``sort_type``/``sort_id`` 表内本行未给区间 → 标量直通；``limit`` 官方 1~50；
        ``-10`` 无数据 → 空而非错。
        """
        query = self._body({
            "symbol": self._symbol(symbol),
            "valuation_type": self._scalar(valuation_type, "valuation_type"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, *self.PLATE_STOCKS_LIMIT_RANGE, name="limit"),
            "sort_type": self._scalar(sort_type, "sort_type"),
            "sort_id": self._scalar(sort_id, "sort_id"),
        })
        return self._tolerate_no_data(
            lambda: self._page(F10_VALUATION_PLATE_STOCKS_PATH, query))

    def valuation_index_stocks(self, symbol, valuation_type=None, next_key=None,
                               limit=None, sort_type=None, sort_id=None,
                               filter_security=None):
        """GET /quote/valuation/index-stocks —— 指数成分股估值（锁定表 §C.5，⭐llms.txt 漏列）。

        **``symbol`` 是查询参数（指数代码）**；``limit`` 表内本行未给区间 → 标量直通；
        ``-10`` 无数据 → 空而非错。
        """
        query = self._body({
            "symbol": self._symbol(symbol),
            "valuation_type": self._scalar(valuation_type, "valuation_type"),
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._scalar(limit, "limit"),
            "sort_type": self._scalar(sort_type, "sort_type"),
            "sort_id": self._scalar(sort_id, "sort_id"),
            "filter_security": self._scalar(filter_security, "filter_security"),
        })
        return self._tolerate_no_data(
            lambda: self._page(F10_VALUATION_INDEX_STOCKS_PATH, query))

    def valuation_index_stock_plates(self, symbol):
        """GET /quote/valuation/index-stock-plates —— 指数所属板块（锁定表 §C.5，⭐漏列）。

        **``symbol`` 是查询参数（指数代码）**；官方本行未给错误码表 → 按通用码处理
        （不把 ``-10`` 吞成空——表内未列该语义）。
        """
        query = {"symbol": self._symbol(symbol)}
        return self.client.request("GET", F10_VALUATION_INDEX_STOCK_PLATES_PATH,
                                   query=query)

    # ------------------------------------------------------------ 公司行为（3）

    def dividends(self, symbol):
        """GET …/corporate-actions/dividends —— 分红派息（锁定表 §C.5，0 成功含空列表）。"""
        return self._get(F10_DIVIDENDS_PATH.format(
            symbol=self._symbol_in_path(symbol)))

    def buybacks(self, symbol, next_key=None, limit=None):
        """GET …/corporate-actions/buybacks —— 回购（锁定表 §C.5，``limit`` ≤50）。"""
        query = self._body({
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, *self.BUYBACKS_LIMIT_RANGE, name="limit"),
        })
        code = self._symbol_in_path(symbol)
        return self._page(F10_BUYBACKS_PATH.format(symbol=code), query)

    def splits(self, symbol):
        """GET …/corporate-actions/splits —— 拆合股（锁定表 §C.5，0 成功含空列表）。"""
        return self._get(F10_SPLITS_PATH.format(symbol=self._symbol_in_path(symbol)))

    # ------------------------------------------------------------ 股东持股（6）

    def shareholders_overview(self, symbol, period_id=None):
        """GET …/shareholders/overview —— 股东概况（锁定表 §C.5）。"""
        query = self._body({"period_id": self._scalar(period_id, "period_id")})
        return self.client.request("GET", F10_SHAREHOLDERS_OVERVIEW_PATH.format(
            symbol=self._symbol_in_path(symbol)), query=query)

    def holding_changes(self, symbol, limit=None, next_key=None, sort_column=None,
                        sort_type=None, filter_type=None, holder_category=None):
        """GET …/shareholders/holding-changes —— 持股变动（锁定表 §C.5）。

        本行参数表内**均未给区间/枚举** → 全部标量直通（不发明）；``-10`` 无持股变动
        → 空而非错。
        """
        query = self._body({
            "limit": self._scalar(limit, "limit"),
            "next_key": self._text(next_key, "next_key", required=False),
            "sort_column": self._scalar(sort_column, "sort_column"),
            "sort_type": self._scalar(sort_type, "sort_type"),
            "filter_type": self._scalar(filter_type, "filter_type"),
            "holder_category": self._scalar(holder_category, "holder_category"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(F10_HOLDING_CHANGES_PATH.format(symbol=code), query))

    def holder_detail(self, symbol, request_type=None, period_id=None, holder_id=None,
                      sort_column=None, sort_type=None, limit=None, next_key=None):
        """GET …/shareholders/holder-detail —— 股东明细（锁定表 §C.5；``-10`` → 空）。"""
        query = self._body({
            "request_type": self._scalar(request_type, "request_type"),
            "period_id": self._scalar(period_id, "period_id"),
            "holder_id": self._scalar(holder_id, "holder_id"),
            "sort_column": self._scalar(sort_column, "sort_column"),
            "sort_type": self._scalar(sort_type, "sort_type"),
            "limit": self._scalar(limit, "limit"),
            "next_key": self._text(next_key, "next_key", required=False),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(F10_HOLDER_DETAIL_PATH.format(symbol=code), query))

    def institutional(self, symbol, limit=None, next_key=None):
        """GET …/shareholders/institutional —— 机构持股（锁定表 §C.5）。

        本行 ``limit`` 表内**未给上限** → 标量直通（与 insider-holders 的 ≤30 逐行区分）。
        """
        query = self._body({
            "limit": self._scalar(limit, "limit"),
            "next_key": self._text(next_key, "next_key", required=False),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(F10_INSTITUTIONAL_PATH.format(symbol=code), query))

    def insider_holders(self, symbol, next_key=None, limit=None):
        """GET …/shareholders/insider-holders —— 内部持股人（锁定表 §C.5，``limit`` ≤30）。"""
        query = self._body({
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, *self.INSIDER_HOLDERS_LIMIT_RANGE, name="limit"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(F10_INSIDER_HOLDERS_PATH.format(symbol=code), query))

    def insider_trades(self, symbol, next_key=None, limit=None, holder_id=None):
        """GET …/shareholders/insider-trades —— 内部交易（锁定表 §C.5，``limit`` ≤50）。"""
        query = self._body({
            "next_key": self._text(next_key, "next_key", required=False),
            "limit": self._opt_int(limit, *self.INSIDER_TRADES_LIMIT_RANGE, name="limit"),
            "holder_id": self._scalar(holder_id, "holder_id"),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(
            lambda: self._page(F10_INSIDER_TRADES_PATH.format(symbol=code), query))

    # ------------------------------------------------------------ 公司信息（4）

    def company_profile(self, symbol):
        """GET …/company/profile —— 公司简介（锁定表 §C.5；``-10`` 无资料 → 空）。"""
        return self._tolerate_no_data(lambda: self._get(
            F10_COMPANY_PROFILE_PATH.format(symbol=self._symbol_in_path(symbol))))

    def company_executives(self, symbol):
        """GET …/company/executives —— 公司高管（锁定表 §C.5；``-10`` 无数据 → 空）。"""
        return self._tolerate_no_data(lambda: self._get(
            F10_COMPANY_EXECUTIVES_PATH.format(symbol=self._symbol_in_path(symbol))))

    def company_executive_background(self, symbol, leader_name):
        """GET …/company/executive-background —— 高管背景（锁定表 §C.5，``leader_name`` 必填）。"""
        query = {"leader_name": self._text(leader_name, "leader_name")}
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(lambda: self.client.request(
            "GET", F10_COMPANY_EXECUTIVE_BACKGROUND_PATH.format(symbol=code),
            query=query))

    def company_operational_efficiency(self, symbol, limit=None, financial_type=None,
                                       currency_code=None, next_key=None):
        """GET …/company/operational-efficiency —— 运营效率（锁定表 §C.5）。

        ``limit`` 官方 ≤100；``financial_type`` 官方 ∈{7,102}；``-10`` 无数据 → 空。
        """
        query = self._body({
            "limit": self._opt_int(limit, *self.EFFICIENCY_LIMIT_RANGE, name="limit"),
            "financial_type": self._enum_in(financial_type,
                                            self.EFFICIENCY_FINANCIAL_TYPES,
                                            "financial_type", default=None),
            "currency_code": self._scalar(currency_code, "currency_code"),
            "next_key": self._text(next_key, "next_key", required=False),
        })
        code = self._symbol_in_path(symbol)
        return self._tolerate_no_data(lambda: self._page(
            F10_COMPANY_OPERATIONAL_EFFICIENCY_PATH.format(symbol=code), query))

    # ------------------------------------------------------------ 十大经纪商（2）

    def top_brokers(self, symbol, date=None):
        """GET …/quote/{symbol}/top-brokers —— 十大经纪商（锁定表 §C.5，**仅港股**）。

        ``date`` 官方 yyyy-MM-dd、缺省取最新；非港股官方 ``-8`` **本地可判定** → 前置拒绝。
        路径以锁定表为准（``/quote/{symbol}/top-brokers``；文档目录 ``top-brokers/`` 是
        文档站路径，**不是** REST 路径——两者不一致，禁止按文档目录推断）。
        """
        code = self._in_markets(symbol, self.HK_ONLY_MARKETS, "十大经纪商")
        query = self._body({"date": self._date(date, "date")})
        return self.client.request("GET", F10_TOP_BROKERS_PATH.format(symbol=code),
                                   query=query)

    def top_brokers_history(self, symbol, days_before):
        """GET …/top-brokers-history —— 十大经纪商历史（锁定表 §C.5，**仅港股**）。

        ``days_before`` 官方**必填 1~365**；非港股官方 ``-8`` 本地可判定 → 前置拒绝。
        """
        code = self._in_markets(symbol, self.HK_ONLY_MARKETS, "十大经纪商历史")
        query = {"days_before": self._int_in(days_before, *self.DAYS_BEFORE_RANGE,
                                             name="days_before")}
        return self.client.request("GET", F10_TOP_BROKERS_HISTORY_PATH.format(symbol=code),
                                   query=query)


#: WP12 任务 3 F10 端点绑定面：(类名, 方法名) → (HTTP 方法, 路径模板)。
#: 锁定测试 ``tests/test_wp12_f10_transport.py`` 逐条比对锁定表 §C.5——**新增端点必须先入
#: 锁定表再入本表**（禁止未核对路径落地）。
WP12_F10_ENDPOINTS = {
    ("OpenApiF10", "earnings_price_move"): ("GET", F10_EARNINGS_PRICE_MOVE_PATH),
    ("OpenApiF10", "earnings_price_history"): ("GET", F10_EARNINGS_PRICE_HISTORY_PATH),
    ("OpenApiF10", "statements"): ("GET", F10_STATEMENTS_PATH),
    ("OpenApiF10", "revenue_breakdown"): ("GET", F10_REVENUE_BREAKDOWN_PATH),
    ("OpenApiF10", "analyst_consensus"): ("GET", F10_ANALYST_CONSENSUS_PATH),
    ("OpenApiF10", "rating_summary"): ("GET", F10_RATING_SUMMARY_PATH),
    ("OpenApiF10", "morningstar"): ("GET", F10_MORNINGSTAR_PATH),
    ("OpenApiF10", "valuation_detail"): ("GET", F10_VALUATION_DETAIL_PATH),
    ("OpenApiF10", "valuation_plate_stocks"): ("GET", F10_VALUATION_PLATE_STOCKS_PATH),
    ("OpenApiF10", "valuation_index_stocks"): ("GET", F10_VALUATION_INDEX_STOCKS_PATH),
    ("OpenApiF10", "valuation_index_stock_plates"): (
        "GET", F10_VALUATION_INDEX_STOCK_PLATES_PATH),
    ("OpenApiF10", "dividends"): ("GET", F10_DIVIDENDS_PATH),
    ("OpenApiF10", "buybacks"): ("GET", F10_BUYBACKS_PATH),
    ("OpenApiF10", "splits"): ("GET", F10_SPLITS_PATH),
    ("OpenApiF10", "shareholders_overview"): ("GET", F10_SHAREHOLDERS_OVERVIEW_PATH),
    ("OpenApiF10", "holding_changes"): ("GET", F10_HOLDING_CHANGES_PATH),
    ("OpenApiF10", "holder_detail"): ("GET", F10_HOLDER_DETAIL_PATH),
    ("OpenApiF10", "institutional"): ("GET", F10_INSTITUTIONAL_PATH),
    ("OpenApiF10", "insider_holders"): ("GET", F10_INSIDER_HOLDERS_PATH),
    ("OpenApiF10", "insider_trades"): ("GET", F10_INSIDER_TRADES_PATH),
    ("OpenApiF10", "company_profile"): ("GET", F10_COMPANY_PROFILE_PATH),
    ("OpenApiF10", "company_executives"): ("GET", F10_COMPANY_EXECUTIVES_PATH),
    ("OpenApiF10", "company_executive_background"): (
        "GET", F10_COMPANY_EXECUTIVE_BACKGROUND_PATH),
    ("OpenApiF10", "company_operational_efficiency"): (
        "GET", F10_COMPANY_OPERATIONAL_EFFICIENCY_PATH),
    ("OpenApiF10", "top_brokers"): ("GET", F10_TOP_BROKERS_PATH),
    ("OpenApiF10", "top_brokers_history"): ("GET", F10_TOP_BROKERS_HISTORY_PATH),
}


#: WP12 任务 2 数据面端点绑定面：(类名, 方法名) → (HTTP 方法, 路径模板)。
#: 锁定测试 ``tests/test_wp12_transport.py`` 逐条比对锁定表——**新增端点必须先入锁定表
#: 再入本表**（禁止未核对路径落地）。F10 26 项见 ``WP12_F10_ENDPOINTS``（任务 3）、
#: 模拟交易 9 项属任务 4/5。
WP12_TRANSPORT_ENDPOINTS = {
    ("OpenApiScreen", "stock_screen"): ("POST", STOCK_SCREEN_PATH),
    ("OpenApiScreen", "warrant_screen"): ("POST", WARRANT_SCREEN_PATH),
    ("OpenApiPlate", "plate_list"): ("GET", PLATE_LIST_PATH),
    ("OpenApiPlate", "plate_stock"): ("GET", PLATE_STOCK_PATH),
    ("OpenApiShort", "short_daily_volume"): ("GET", SHORT_DAILY_VOLUME_PATH),
    ("OpenApiShort", "short_interest"): ("GET", SHORT_INTEREST_PATH),
    ("OpenApiBasicData", "economic_calendar_hot"): ("GET", ECONOMIC_CALENDAR_HOT_PATH),
    ("OpenApiBasicData", "economic_calendar_search"): ("GET",
                                                       ECONOMIC_CALENDAR_SEARCH_PATH),
    ("OpenApiBasicData", "owner_plate"): ("GET", OWNER_PLATE_PATH),
    ("OpenApiBasicData", "rehab"): ("GET", REHAB_PATH),
    ("OpenApiIpo", "ipo_list"): ("GET", IPO_LIST_PATH),
    ("OpenApiWatchlist", "watchlist_list"): ("GET", WATCHLIST_LIST_PATH),
    ("OpenApiWatchlist", "watchlist_groups"): ("GET", WATCHLIST_GROUPS_PATH),
    ("OpenApiWatchlist", "modify_user_security"): ("POST", MODIFY_USER_SECURITY_PATH),
    ("OpenApiDerivatives", "future_info"): ("POST", FUTURE_INFO_PATH),
    ("OpenApiDerivatives", "reference_future"): ("GET", REFERENCE_FUTURE_PATH),
    ("OpenApiDerivatives", "option_volatility"): ("GET", OPTION_VOLATILITY_PATH),
    ("OpenApiDerivatives", "option_exercise_probability"): (
        "GET", OPTION_EXERCISE_PROBABILITY_PATH),
}
