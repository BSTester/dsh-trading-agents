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
    return parse_envelope_meta(status, body)[0]


def parse_envelope_meta(status, body):
    """``parse_envelope`` 的 (d, 分页) 版本。

    行情分页端点（capital-flow/history、option-screen、history-kline）的信封是
    ``{"ret_code":0, "data":{...}, "pagination":{...}}``——分页在**信封顶层**而不在
    data 内。OpenApiMarket 把它并入返回值以对齐 MCP 通道 ``futu_mcp._unwrap`` 的形状
    （那里 pagination 同样被并入 data）；无分页时第二个元素为 ``None``。
    """
    data = _safe_json_dict(body)
    if isinstance(data, dict) and data.get("s") == "ok":
        return data.get("d"), None
    if isinstance(data, dict) and data.get("s") == "error":
        raise OpenApiError(
            data.get("errmsg") or "未知错误",
            errcode=data.get("errcode"),
            need_order_confirm=data.get("need_order_confirm"),
            confirm_id=data.get("confirm_id"),
            jump_url=data.get("jump_url"),
        )
    if isinstance(data, dict) and "ret_code" in data:
        # 行情类网关信封：ret_code!=0 → 业务错误；==0 → data + 顶层 pagination
        if data.get("ret_code") != 0:
            raise OpenApiError(data.get("ret_msg") or data.get("errmsg") or "未知错误",
                               errcode=data.get("ret_code"))
        pagination = data.get("pagination")
        return data.get("data"), pagination if isinstance(pagination, dict) else None
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
        """发起请求，返回信封 d 部分；s==error / ret_code!=0 / 传输异常 → OpenApiError。"""
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
        return parse_envelope_meta(status, body)

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
        return parse_envelope_meta(status, resp_body)


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
class OpenApiMarket:
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
    #: rt-data 的交易时段枚举（官方文档 rt-data 页）
    RT_SECTIONS = frozenset({"NORMAL", "FULL", "PREMARKET", "AFTERHOURS",
                             "HK_DARK", "OVERNIGHT"})
    #: rt-ticker 的时段过滤枚举
    TICKER_PERIODS = frozenset({"NORMAL", "BEFORE", "AFTER", "OVERNIGHT"})
    #: trading-days 的市场枚举（官方文档 trading-days 页）
    TRADING_MARKETS = frozenset({"HK", "US", "SH", "SZ", "BJ", "SG", "JP", "CA",
                                 "AU", "JP_FUTURE", "SG_FUTURE"})
    #: option-expiration / option-chain 的 filter_standard 枚举
    FILTER_STANDARDS = frozenset({"ALL", "STANDARD", "NON_STANDARD"})
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

    _REQUIRED = object()  # 「必填枚举」哨兵：default=None 表示可省略（请求体去 None）

    def _enum_in(self, value, allowed, name, default=_REQUIRED):
        """枚举校验：None 走 default；default 为哨兵时视为必填。"""
        if value is None:
            if default is self._REQUIRED:
                raise ValueError(f"{name} 必填，取值之一：{sorted(allowed)}")
            return default
        if value not in allowed:
            raise ValueError(f"{name} 取值非法：{value!r}（允许：{sorted(allowed)}）")
        return value

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

    def _merge_pagination(self, d, pagination):
        """信封顶层 pagination 并入 d（对齐 futu_mcp._unwrap；无分页原样返回）。"""
        if pagination:
            return {**d, "pagination": pagination}
        return d

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
        """GET /api/v1.0/quote/{symbol}/rt-ticker —— 逐笔成交（num 1..750；period 列表）。"""
        query = {"num": self._int_in(num, 1, self.MAX_TICKER_NUM, "num", default=500)}
        if period is not None:
            if not isinstance(period, list) or \
                    any(item not in self.TICKER_PERIODS for item in period):
                raise ValueError(f"period 必须是 {sorted(self.TICKER_PERIODS)} 的列表")
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
        """GET /api/v1.0/quote/{symbol}/capital-flow —— 日内分钟级资金流。"""
        query = {"section": self._enum_in(section, self.RT_SECTIONS, "section",
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
        """GET /api/v1.0/quote/{symbol}/option-expiration —— 期权到期日列表。"""
        query = self._body({
            "index_option_type": index_option_type,
            "filter_standard": self._enum_in(filter_standard, self.FILTER_STANDARDS,
                                             "filter_standard", default="ALL"),
            "filter_expiration_cycles": filter_expiration_cycles,
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
