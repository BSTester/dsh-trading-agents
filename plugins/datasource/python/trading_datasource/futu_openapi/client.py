"""HTTP 客户端：认证请求、token 刷新、AppKey 签名、传输异常收敛。"""
import contextvars
import http.client
import inspect
import time
import urllib.request

from .auth import AppKeySigner
from .envelope import (json_body_bytes, _safe_json_dict,
                       parse_envelope_meta, query_string)
from .errors import OpenApiError, TransportError

DEFAULT_HOST = "https://webapi.futunn.com"
TOKEN_PATH = "/oauth2/token"
#: 传输超时缺省值（秒）——与历史硬编码 30 一致；实例级可覆盖，单次调用可再覆盖。
DEFAULT_TIMEOUT = 30.0
#: 单次调用的超时覆盖（``OpenApiClient.timeout_scope``）：**contextvars 而非实例属性**——
#: client 是跨调用缓存的单例（``channel.openapi_client``），改实例属性会串到并发调用；
#: contextvars 让覆盖只在本上下文生效，异步/多线程下互不干扰。
_CALL_TIMEOUT = contextvars.ContextVar("openapi_call_timeout", default=None)


def _default_http(method, url, headers, body, timeout=DEFAULT_TIMEOUT):
    """urllib 传输：返回 ``(status, body, headers)`` 三元组（headers 供上层取
    ``Retry-After`` 等响应头）。

    - 非 2xx 不抛（HTTPError 转 (status, body, headers)），由上层解释信封；
    - 其余传输异常（URLError/连接拒绝/超时/连接重置/响应中断）一律包装为
      ``TransportError``（OpenApiError 子类）——``request()`` 契约
      「传输异常 → OpenApiError」成立，不再裸穿给调用方；
    - ``timeout`` 是**连接+读取**超时（秒），缺省 ``DEFAULT_TIMEOUT``；调用链
      （``request(timeout=)`` / ``timeout_scope``）的覆盖值最终落到这里。
    """
    req = urllib.request.Request(url, data=body, method=str(method).upper())
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def _http_takes_timeout(http):
    """注入式传输是否接受第 5 个参数 ``timeout``。

    兼容契约：**老替身是 4 参** ``(method, url, headers, body)``（既有测试大量如此），
    支持超时覆盖的传输声明第 5 参（默认真实传输 ``_default_http`` 即如此，可位置可关键字）。
    用签名判定而非「试调 + 吞 TypeError」：后者会把传输内部的真实 TypeError 一起吞掉。
    """
    try:
        params = list(inspect.signature(http).parameters.values())
    except (TypeError, ValueError):  # 内建/不可内省可调用：按老契约处理
        return False
    if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params):
        return True
    positional = [p for p in params
                  if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    if len(positional) >= 5:
        return True
    return any(p.name == "timeout" for p in params)


class _TimeoutScope:
    """``with client.timeout_scope(sec):`` —— 块内请求的传输超时覆盖。"""

    def __init__(self, timeout):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
                or timeout <= 0:
            raise ValueError(f"timeout 必须是正数秒：{timeout!r}")
        self.timeout = float(timeout)
        self._token = None

    def __enter__(self):
        self._token = _CALL_TIMEOUT.set(self.timeout)
        return self

    def __exit__(self, *_exc):
        _CALL_TIMEOUT.reset(self._token)
        return False


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

    def __init__(self, credential_store, http=None, host=DEFAULT_HOST, now=None,
                 timeout=DEFAULT_TIMEOUT):
        self.store = credential_store
        self._http = http or _default_http
        self.host = str(host).rstrip("/")
        self._now = now or time.time
        #: 实例级缺省传输超时（秒）。单次调用可用 ``request(timeout=)`` 或
        #: ``with client.timeout_scope(sec):`` 覆盖——后者是通道适配器
        #: （``channel.sim_call``）把调用方 timeout 兑现到 REST 的方式。
        self.timeout = timeout

    # ------------------------------------------------------------ 传输超时

    def timeout_scope(self, timeout):
        """``with`` 块内覆盖本上下文后续请求的传输超时（contextvars：并发安全）。

        为什么需要：``client`` 是跨调用缓存的**单例**（``channel.openapi_client``），
        把超时写进实例属性会串到并发调用；``request(timeout=)`` 又要穿透 40+ 个方法组
        签名。scope 让「本次调用链的超时」随上下文走，组方法保持零改动。
        """
        return _TimeoutScope(timeout)

    def _effective_timeout(self, timeout=None):
        """单次参数 > scope 覆盖 > 实例缺省。"""
        if timeout is not None:
            return float(timeout)
        scoped = _CALL_TIMEOUT.get()
        return float(self.timeout if scoped is None else scoped)

    # ------------------------------------------------------------ 公共入口

    def request(self, method, path, query=None, json_body=None, timeout=None):
        """发起请求，返回信封 d 部分。

        s==error / ret_code!=0 → OpenApiError（业务结论）；非信封/5xx/429 →
        UnexpectedResponse（**非业务结论**，调用方不得当业务拒绝）；传输异常为
        TransportError 子类；429 附 retry_after。``timeout`` 缺省走
        ``timeout_scope``/实例缺省（见 ``_effective_timeout``）。
        """
        d, _pagination = self.request_meta(method, path, query, json_body,
                                           timeout=timeout)
        return d

    def request_meta(self, method, path, query=None, json_body=None, timeout=None):
        """``request`` 的 (d, 信封顶层 pagination) 版本。

        分页由 OpenApiMarket 的分页端点（capital_flow_history/option_screen/
        history_kline）消费：并入返回值后与 MCP 通道 data 形状一致（见
        parse_envelope_meta）。无分页时 pagination 为 None。
        """
        mode = self.store.load().get("mode")
        if mode == "oauth":
            return self._request_oauth(method, path, query, json_body, timeout=timeout)
        if mode == "appkey":
            return self._request_appkey(method, path, query, json_body, timeout=timeout)
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
    def _transport_call(http, method, url, headers, body, timeout=None):
        """调传输并兑现 ``request()`` 的契约：传输层异常一律收敛为 ``TransportError``。

        默认传输 ``_default_http`` 自己已包装；**注入式传输**（测试/自研通道）可能抛任意
        异常（connection reset / 自定义 HTTP 库异常）——在三个调用点统一收敛，避免裸穿
        给调用方被误当业务错误（交易写路径会把裸异常上抛成 broker-unavailable 而不是
        unknown）。已经是 OpenApiError 系（含 TransportError）的原样上抛。

        ``timeout`` 只在传输接受该参数时透传（``_http_takes_timeout``）——老 4 参替身
        继续工作；真实传输据此兑现调用方给的时限（WP13 审查 M2：sim 写路径的 timeout
        过去在 REST 分支被静默丢弃，而传输失败的判定正建立在该时限上）。
        """
        try:
            if timeout is not None and _http_takes_timeout(http):
                return http(str(method).upper(), url, headers, body, timeout)
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

    def _request_oauth(self, method, path, query, json_body, timeout=None):
        timeout = self._effective_timeout(timeout)
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
            cred = self._refresh(cred, timeout=timeout)  # 临期刷新一次；失败抛 OpenApiError
            refreshed = True
        status, body, headers = self._send_oauth(cred, method, path, query, json_body,
                                                 timeout=timeout)
        if status == 401 and not refreshed:
            cred = self._refresh(cred, timeout=timeout)  # 401 触发刷新一次
            status, body, headers = self._send_oauth(cred, method, path, query,
                                                     json_body, timeout=timeout)
        try:
            return parse_envelope_meta(status, body)
        except OpenApiError as e:
            raise self._with_retry_after(e, status, headers) from None

    def _send_oauth(self, cred, method, path, query, json_body, timeout=None):
        headers = {}
        body = None
        if json_body is not None:
            body = json_body_bytes(json_body)
            headers["Content-Type"] = "application/json"
        headers["Authorization"] = "Bearer " + str(cred.get("access_token", ""))
        return self._transport_call(self._http, method, self._url(path, query),
                                    headers, body, timeout=timeout)

    def _refresh(self, cred, timeout=None):
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
            {"Content-Type": "application/x-www-form-urlencoded"}, form,
            timeout=timeout or self._effective_timeout())
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

    def _request_appkey(self, method, path, query, json_body, timeout=None):
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
            self._http, method, self._url(path, query), headers, body,
            timeout=self._effective_timeout(timeout))
        try:
            return parse_envelope_meta(status, resp_body)
        except OpenApiError as e:
            raise self._with_retry_after(e, status, headers) from None


# ---------------------------------------------------------------------------
# REST 方法组共用参数校验（OpenApiMarket / OpenApiTrade 唯一实现）
