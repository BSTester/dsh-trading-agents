"""OAuth 2.1 + PKCE 授权流程管理（WP8：设置页「开始授权」的服务端生命周期）。

与 ``scripts/futu_auth.py --openapi`` 的 CLI 流程同源同规（富途 OpenAPI 规格 §三），
形态不同：CLI 在终端里同步等回调，这里由 Web 设置页发起、服务端**异步**完成——

    start   → 注册 client（如需）→ 生成 PKCE verifier/challenge + state →
              启动 127.0.0.1 callback listener → 返回授权 URL（前端开新窗口）
    回调    → 浏览器授权后重定向 ``http://localhost:<port>/callback?code&state`` →
              校验 state（逐字，防 CSRF）→ code + code_verifier 换 token →
              落盘 CredentialStore（mode=oauth，0600 原子写）
    status  → 前端 2s 轮询 {pending, done, error, tokens_saved}
    cancel  → 停 listener、清状态

安全不变式（测试 tests/test_wp8_oauth.py 钉死）：
  * ``code_verifier`` 只存内存：不落盘、不进日志、不进 status()/auth_url/错误文案；
  * ``state`` 逐字校验，不符即拒绝换 token；
  * callback listener 只绑 127.0.0.1；超时（默认 600s）自动停并标记超时；
  * 凭据写入走 ``trading_datasource.futu_openapi.CredentialStore``（全仓库唯一
    凭据读写实现，0600 原子写），app_key 等既有字段合并保留（两种模式并存）。

register/token 的网络调用经 ``http`` 注入（生产 ``default_http`` = urllib，
测试注入替身离线）；时钟 ``now`` 注入（毫秒），超时与 ``expires_at`` 全部离线可测。
纯函数（register_payload/build_authorize_url/challenge/...）与 CLI 脚本各自独立成
份——平台服务不 import scripts/，与 settings_api 的分层口径一致。
"""
import http.server
import json
import secrets
import threading
import time
import urllib.parse
import urllib.request

from server.settings_api import credential_path
from server.store_access import WorkbenchError

REGISTER_URL = "https://webapi.futunn.com/oauth2/register"
AUTHORIZE_URL = "https://webapi.futunn.com/oauth2/authorize/confirm"
TOKEN_URL = "https://webapi.futunn.com/oauth2/token"
#: 默认回调端口（redirect_uri = http://localhost:<port>/callback；与 CLI 同口）
DEFAULT_CALLBACK_PORT = 60355
#: 默认 scope（与 scripts/futu_auth.py --openapi 一致：含交易写，供交易链路使用）
DEFAULT_SCOPE = "quote:read accid:* trade:read trade:write"
#: 授权流程超时（秒）：超时自动停 listener 并标记，绝不无限挂着端口
FLOW_TIMEOUT_SECONDS = 600
#: client_id 保存进凭据文件的键（CredentialStore 已支持）
CLIENT_NAME = "Quant Platform Workbench"

IDLE_STATUS = {"pending": False, "done": False, "error": None, "tokens_saved": False}


def default_http(url, payload=None, form=None):
    """scripts/futu_auth.py ``http_json`` 的等价物（POST JSON / 表单 → dict）。"""
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
    else:
        data = json.dumps(payload or {}).encode()
        request = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def register_payload(callback_port=DEFAULT_CALLBACK_PORT):
    """POST /oauth2/register 注册体：public client，PKCE required（规格 §三）。"""
    return {
        "redirect_uris": [f"http://localhost:{callback_port}/callback"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "client_name": CLIENT_NAME,
    }


def challenge(verifier):
    """PKCE S256：BASE64URL(SHA256(verifier))，无 padding（RFC 7636 附录 B 向量）。"""
    import base64  # noqa: PLC0415
    import hashlib  # noqa: PLC0415
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def pkce_pair():
    """(verifier, challenge)：verifier 43-128 个非保留字符，challenge 即 S256。"""
    from trading_datasource.futu_openapi import Pkce  # noqa: PLC0415
    verifier = Pkce.code_verifier()
    return verifier, challenge(verifier)


def build_authorize_url(client_id, redirect_uri, state, code_challenge, scope):
    """授权 URL（S256；scope 的 %20/%2A 精确编码，口径同 scripts/futu_auth.py）。"""
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }, safe=":", quote_via=urllib.parse.quote)


def validate_state(expected, received):
    """回调 state 逐字校验，不符即拒绝换 token（防回调劫持/CSRF）。"""
    if not expected or received != expected:
        raise ValueError(f"state 不匹配（疑似回调劫持），拒绝换 token："
                         f"期望 {expected!r}，收到 {received!r}")
    return True


def classify_callback(query):
    """回调 query（parse_qs 产物）分类，口径同 scripts/futu_auth.py：

    ``("code", code, state)`` 继续换 token；``("error", error, description)``
    如实报「授权被拒绝/失败」，绝不误报超时。
    """
    error = query.get("error", [""])[0]
    if error:
        return ("error", error, query.get("error_description", [""])[0])
    code = query.get("code", [""])[0]
    if code:
        return ("code", code, query.get("state", [""])[0])
    return ("error", "invalid_callback", "回调缺少 code 参数")


def token_request_form(code, client_id, redirect_uri, code_verifier):
    """POST /oauth2/token 的表单（authorization_code + code_verifier）。"""
    return {"grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": client_id,
            "code_verifier": code_verifier}


def oauth_credential_update(cred, token_resp, client_id, now_ms):
    """token 响应 → 新凭据 dict（expires_at 毫秒），口径同 scripts/futu_auth.py：

    官方不轮换 refresh_token：响应带了才更新；既有字段（app_key 等）原样保留；
    mode 固定 oauth；client_id 一并写入（首次注册后随 token 一起落盘）。
    """
    out = dict(cred)
    out["mode"] = "oauth"
    out["client_id"] = client_id
    out["access_token"] = token_resp["access_token"]
    out["expires_at"] = now_ms + int(float(token_resp.get("expires_in", 7200)) * 1000)
    if token_resp.get("refresh_token"):
        out["refresh_token"] = token_resp["refresh_token"]
    if token_resp.get("scope"):
        out["scope"] = token_resp["scope"]
    return out


class OAuthFlowManager:
    """管理一次 OAuth 2.1 + PKCE 授权流程的生命周期（单实例单流程）。

    ``credential_store``：绑定凭据路径的 ``CredentialStore``（读写唯一入口）；
    ``home``：工作台 DSH_HOME（诊断信息用）；``callback_port``：回调端口；
    ``now``：毫秒时钟注入（缺省系统时间）；``http``：register/token 传输注入
    （缺省 urllib）；``timeout_seconds``：授权超时（超时停 listener 并标记）。
    """

    def __init__(self, credential_store, home, callback_port=DEFAULT_CALLBACK_PORT,
                 now=None, http=None, timeout_seconds=FLOW_TIMEOUT_SECONDS):
        self._store = credential_store
        self.home = str(home)
        self.callback_port = int(callback_port)
        self._now = now if now is not None else (lambda: int(time.time() * 1000))
        self._http = http if http is not None else default_http
        self._timeout_seconds = float(timeout_seconds)
        self._lock = threading.RLock()
        self._server = None
        self._watcher = None
        self._stop_event = threading.Event()
        self._verifier = None      # 只存内存：不落盘、不进日志（不变式 2）
        self._expected_state = None
        self._client_id = None
        self._redirect_uri = None
        self._status = dict(IDLE_STATUS)

    # ------------------------------------------------------------ 对外状态

    def status(self):
        """{"pending": bool, "done": bool, "error": str|None, "tokens_saved": bool}"""
        with self._lock:
            return dict(self._status)

    # ------------------------------------------------------------ 启动

    def start(self, client_id=None, scope=DEFAULT_SCOPE):
        """启动授权流程，返回 {"auth_url", "state", "client_id", "callback_port"}。

        无 client_id 时先 POST /oauth2/register 注册 public client（复用凭据文件
        已存的）；生成 PKCE + state（内存）→ 绑 127.0.0.1 回调 listener → 起
        watcher 线程等回调/超时。失败（注册被拒/端口占用）抛 ``ValueError``，
        状态保持 idle、不产生半截监听。
        """
        with self._lock:
            if self._server is not None:
                raise ValueError("已有进行中的授权流程（请先取消或等待完成）")
        cred = self._load_cred()
        client_id = str(client_id or "").strip() or cred.get("client_id")
        redirect_uri = f"http://localhost:{self.callback_port}/callback"
        if not client_id:
            resp = self._http(REGISTER_URL, register_payload(self.callback_port))
            client_id = resp.get("client_id") if isinstance(resp, dict) else None
            if not client_id:
                raise ValueError(
                    "注册 OAuth client 失败："
                    + json.dumps(resp, ensure_ascii=False)[:300])
        verifier, code_challenge = pkce_pair()
        state = secrets.token_urlsafe(16)
        with self._lock:
            self._verifier = verifier
            self._expected_state = state
            self._client_id = client_id
            self._redirect_uri = redirect_uri
            self._status = {"pending": True, "done": False, "error": None,
                            "tokens_saved": False}
            self._bind_listener()
        self._watcher = threading.Thread(target=self._watch, daemon=True,
                                         name="oauth-flow-callback")
        self._watcher.start()
        return {"auth_url": build_authorize_url(client_id, redirect_uri, state,
                                                code_challenge, scope),
                "state": state,
                "client_id": client_id,
                "callback_port": self.callback_port}

    # ------------------------------------------------------------ 回调（watcher 线程内）

    def _on_callback(self, code, state):
        """callback 收到授权码 → 校验 state → 换 token → 落盘 CredentialStore。"""
        with self._lock:
            if self._expected_state is None:
                return  # 无进行中的流程：游离回调一律忽略
            try:
                validate_state(self._expected_state, state)
            except ValueError as error:
                self._fail(str(error))
                return
            form = token_request_form(code, self._client_id, self._redirect_uri,
                                      self._verifier)
        try:
            resp = self._http(TOKEN_URL, form=form)
        except Exception as error:  # noqa: BLE001 —— 传输层异常如实收敛为流程失败
            with self._lock:
                self._fail(f"换取 token 失败：{error}")
            return
        if not isinstance(resp, dict) or not resp.get("access_token"):
            with self._lock:
                self._fail("换取 token 失败："
                           + json.dumps(resp, ensure_ascii=False)[:300])
            return
        with self._lock:
            cred = {**self._load_cred(), "client_id": self._client_id}
            self._store.save(oauth_credential_update(cred, resp, self._client_id,
                                                     self._now()))
            self._status = {"pending": False, "done": True, "error": None,
                            "tokens_saved": True}
            self._teardown_listener()

    def _on_error_callback(self, error, description=""):
        """授权被拒/畸形回调（``?error=...``）：如实报拒绝原因，不换 token。"""
        with self._lock:
            message = f"授权被拒绝/失败：{error}"
            if description:
                message += f"（{description}）"
            self._fail(message + "——未换取 token，请重新发起授权")

    # ------------------------------------------------------------ 停止

    def stop(self):
        """清理 callback listener 与 watcher 线程（状态保留供 status() 读）。"""
        self._stop_event.set()
        with self._lock:
            self._teardown_listener()
        if self._watcher is not None and self._watcher.is_alive():
            self._watcher.join(timeout=2)

    def cancel(self):
        """用户主动取消：停 listener 并把状态复位为 idle。"""
        self.stop()
        with self._lock:
            self._status = dict(IDLE_STATUS)
            self._verifier = None
            self._expected_state = None

    # ------------------------------------------------------------ 内部

    def _load_cred(self):
        from trading_datasource.futu_openapi import CredentialStore  # noqa: PLC0415
        try:
            cred = self._store.load()
        except FileNotFoundError:
            cred = {}
        return cred if isinstance(cred, dict) else {}

    def _bind_listener(self):
        manager = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            """60355 回调：捕获 code/state（或 error），先回页面再异步换 token。"""

            def do_GET(self):
                manager._handle_request(self)

            def log_message(self, *args):
                pass  # 授权码/	state 不进任何日志

        try:
            server = http.server.HTTPServer(("127.0.0.1", self.callback_port),
                                            _Handler)
        except OSError as error:
            self._status = dict(IDLE_STATUS)
            raise ValueError(f"端口 {self.callback_port} 已被占用（可能是残留的"
                             f"旧授权进程），请先结束再重试：{error}") from error
        server.timeout = 0.2  # handle_request 的无请求等待上限，stop/超时及时生效
        self._server = server

    def _handle_request(self, handler):
        if not handler.path.startswith("/callback"):
            handler.send_response(404)
            handler.end_headers()
            return  # 无关探针：忽略并继续监听
        query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
        kind, value, extra = classify_callback(query)
        handler.send_response(200)
        handler.end_headers()
        if kind == "code":
            body = ("<h3>✅ 授权已收到</h3><p>正在完成凭据交换，"
                    "可关闭本页回到工作台查看结果。</p>")
        else:
            body = (f"<h3>❌ 授权被拒绝/失败</h3><p>{value}，"
                    "可关闭本页回到工作台重试。</p>")
        html = ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<script>window.close();</script></head>"
                f"<body style='font-family:sans-serif'>{body}</body></html>")
        try:
            handler.wfile.write(html.encode("utf-8"))
        except OSError:
            pass  # 浏览器提前断开不影响流程
        if kind == "code":
            self._on_callback(value, extra)
        else:
            self._on_error_callback(value, extra)

    def _watch(self):
        """watcher 线程：循环 handle_request，直到完成/失败/取消/超时。"""
        deadline = self._now() + self._timeout_seconds * 1000
        while not self._stop_event.is_set():
            with self._lock:
                if self._status["done"] or self._status["error"]:
                    return
                server = self._server
            if self._now() >= deadline:
                with self._lock:
                    if not (self._status["done"] or self._status["error"]):
                        self._fail(f"授权超时（{int(self._timeout_seconds)} 秒内"
                                   "未收到回调），请重新发起授权")
                return
            if server is None:
                return
            try:
                server.handle_request()
            except OSError:
                return  # listener 已被 stop() 关闭：正常退出

    def _fail(self, message):
        """标记流程失败（截断 ≤300 字符，与其他端点文案同口径）并停 listener。"""
        self._status = {"pending": False, "done": False,
                        "error": str(message)[:300], "tokens_saved": False}
        self._teardown_listener()

    def _teardown_listener(self):
        if self._server is not None:
            try:
                self._server.server_close()
            except OSError:
                pass
            self._server = None


# ---------------------------------------------------------------------------
# 进程内流程注册表：app.py 的 openapi_oauth 分支按 home 存取（工作台单 home，
# dict 键隔离让多 home 测试互不串扰）；流程一次性——重新 start 自动停旧建新。
# ---------------------------------------------------------------------------

_MANAGERS = {}
_REGISTRY_LOCK = threading.Lock()


def _manager_for(home):
    return _MANAGERS.get(str(home))


def start_flow(home, client_id=None, callback_port=None, http=None):
    """启动（或重启）``home`` 的授权流程，返回 start() 的结果 dict。"""
    from trading_datasource.futu_openapi import CredentialStore  # noqa: PLC0415
    key = str(home)
    port = DEFAULT_CALLBACK_PORT if callback_port is None else callback_port
    transport = default_http if http is None else http
    with _REGISTRY_LOCK:
        old = _MANAGERS.pop(key, None)
        if old is not None:
            old.stop()
        manager = OAuthFlowManager(CredentialStore(credential_path(home)), home,
                                   callback_port=port, http=transport)
        try:
            result = manager.start(client_id=client_id)
        except BaseException:
            manager.stop()
            raise
        _MANAGERS[key] = manager
        return result


def flow_status(home):
    """当前流程状态；从未启动过 → idle 形状（前端轮询永远拿得到同形结构）。"""
    manager = _manager_for(home)
    return manager.status() if manager is not None else dict(IDLE_STATUS)


def cancel_flow(home):
    """取消当前流程：停 listener、清状态，返回 {"cancelled": True}。"""
    with _REGISTRY_LOCK:
        manager = _MANAGERS.pop(str(home), None)
    if manager is not None:
        manager.cancel()
    return {"cancelled": True}


def handle_action(home, payload):
    """openapi_oauth 端点的分发：action ∈ {start, status, cancel}。

    业务失败（非法 action/注册被拒/端口占用）抛 ``WorkbenchError``，由 app.py
    统一落 ``trading/invalid-operation`` 信封（与其他端点同形）。
    """
    action = payload.get("action")
    if action == "start":
        try:
            return start_flow(home, client_id=payload.get("client_id"))
        except ValueError as error:
            raise WorkbenchError(str(error)) from error
    if action == "status":
        return flow_status(home)
    if action == "cancel":
        return cancel_flow(home)
    raise WorkbenchError(
        f"action 取值非法：{action!r}（允许 start / status / cancel）")


def shutdown_all():
    """停掉所有在途流程（app lifespan 收尾用；无流程时零副作用）。"""
    with _REGISTRY_LOCK:
        managers = list(_MANAGERS.values())
        _MANAGERS.clear()
    for manager in managers:
        manager.stop()
