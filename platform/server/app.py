"""FastAPI 应用（WP6 补遗 C）：HTTP 面 + 唯一分发函数 handle。

移植源（逐行为准）：
  * ``plugins/workbench/src/rpc.js:97-230`` —— handle(endpoint, payload)：载荷对象性、
    ``_refresh`` 剥离、逐端点字段白名单、snapshot 合并点（rpc.js:106）、switch-mode、
    audit 的 trades 兜底（rpc.js:132-145）、plan/schedule/reconcile（rpc.js:172-182）、
    plan-execute 的动作映射与校验顺序（rpc.js:183-210）、series（rpc.js:211-224）、
    未知端点与 WorkbenchError → ``trading/invalid-operation`` 信封（rpc.js:225-228）；
  * ``platform/server/service.mjs:42-88`` —— 路由顺序、白名单 404 先于 handle、
    content-type 415、1MB 413、坏 JSON 400、静态托管与 SPA 兜底、500 兜底信封；
  * ``platform/server/util.mjs`` —— sendJson / authorized / unauthorized 的等价物；
  * ``plugins/workbench/src/analytics.js`` —— 逐端点参数构造（在 ``server.compute``）。

与 Node 侧的有意差异（均为「规格更严」而非语义变更）：
  1. ``auth`` 在中间件里统一判定（service.mjs 在每个分支里散着判），但判定条件与豁免面
     完全一致：token 非空时 ``/api/*`` 与 ``/mcp``（含 ``/mcp/...`` 任何子路径）需
     ``Authorization: Bearer <token>``。``/mcp`` 用「等值或前缀」判定而不是等值：SDK 将来若在
     ``/mcp`` 下加子路径，前缀判定保证新路径仍在鉴权分支内，不出现未鉴权旁路。
  2. ``/mcp`` 是真实 MCP streamable-http 端点（补遗任务 D）。SDK 的 ``streamable_http_app()``
     自带 ``lifespan=lambda app: session_manager.run()``，因此并入主 app 的 lifespan；路由用
     「插进主 router」而不是 ``app.mount()``（见 ``create_app`` 里的有意差异 10）。
  3. 端点白名单与静态路径在**路由层**判（先于 handle），使「白名单 404 不触达 handle」这一
     不变式由框架保证而不是约定；handle 内部对未知端点仍抛同样的 WorkbenchError。
  4. 阻塞取数（子进程/文件）经 ``asyncio.to_thread`` 让出事件循环——响应内容不变，
     只是不再阻塞其他请求。
  5. 405 的 ``detail`` 用 service.mjs 的错误码 ``trading/method-not-allowed``。
  6. 静态路径拼接用 ``lstrip('/')``（见 ``_serve_static_sync`` 的说明）。
  7. 非 POST/非 GET 的兜底路由显式把**所有**方法收进信封（service.mjs:57/83）：
     ``/api/wb/*`` 下非 POST → 405「仅 POST」，多段/含斜杠路径 → 404 unknown-endpoint，
     其余路径非 GET → 405「仅 GET」。否则会落到 Starlette 的 ``{"detail": "Method Not
     Allowed"}``，前端 ``response.json()`` 就拿不到统一信封。
  8. 体上限（413）只在 ``/api/wb/*`` 分支内判、且在白名单与 content-type 之后
     （service.mjs:60-75 的顺序），不再对静态与白名单外请求生效；读取时按块计数
     （``request.stream()``），超限立刻 413，不先整读。
  9. 静态响应用显式 ``Content-Type`` 头而不是 ``media_type=``：Starlette 会给 ``text/*``
     追加 ``; charset=utf-8``，而 service.mjs:10-14 的表里只有 ``.html`` 带 charset。
"""
import asyncio
import contextlib
import json
import os
import re
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from mcp.server.mcpserver import MCPServer

from server import audit_chain, caches, compute, mcp_tools, store_access
from server.config import load_config
from server.store_access import WorkbenchError

Body = dict  # 文档用途：handle 的载荷一律是普通 dict

# service.mjs:10-14 的 MIME 表：**只认这张手写表**，未收录扩展名回落 octet-stream。
# 刻意不用 ``mimetypes.guess_type``：它会读 /etc/mime.types 等主机文件，同一份代码在不同
# 机器上给出不同 Content-Type（且会给 text/* 追加 charset），与 service.mjs 不可比。
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".map": "application/json",
}

MAX_PAYLOAD = 1024 * 1024  # util.mjs:2 collectBody 默认上限
DEFAULT_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

# 兜底路由的方法面：service.mjs 对每个路径段都只按「是不是 POST/GET」分派，
# 其余方法一律落统一信封，因此这里收全 HTTP 方法，绝不再落到 Starlette 的默认 405。
ALL_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD")
NON_GET_METHODS = ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD")

# 分析类端点（rpc.js:114-171）：白名单字段 + 缓存错误码 trading/analytics-unavailable
ANALYTICS_ENDPOINTS = {
    "equity": ("mode", "window"),
    "positions": ("mode", "window"),
    "correlation": ("tickers", "window"),
    "sensitivity": ("ticker", "strategy", "metric", "fast_grid", "slow_grid",
                    "buy_grid", "sell_grid", "start"),
    "risk": (),
    "trades": ("mode", "limit"),
    "events": ("ticker", "days"),
    "factors": ("tickers", "window"),
    "ic": ("tickers", "factor", "forward", "window"),
    "sources": ("no_probe",),
    "instrument": ("ticker",),
    "quality": ("ticker",),
}

# 空载荷端点（rpc.js:104/133/175）
EMPTY_PAYLOAD_ENDPOINTS = ("snapshot", "audit", "plan", "schedule", "reconcile")

# rpc.js:69-70 的动作 → 指令类型映射（规格 §8.2 的 5 种里服务面可达的 4 种）
EXECUTE_ACTIONS = {
    "execute": "execute_plan",
    "cancel": "cancel_plan",
    "kill": "kill",
    "unkill": "unkill",
}

SWITCH_MODE_FIELDS = ("mode", "expected_mode", "confirmation")
PLAN_EXECUTE_FIELDS = ("plan_hash", "expected_mode", "confirmation", "action")
SERIES_FIELDS = ("ticker", "period", "limit")


def error_envelope(code, message, status):
    """``sendJson(res, status, {ok:false, error:{code, message, details:{}}})``。"""
    return JSONResponse(status_code=status,
                        content={"ok": False, "error": {"code": code, "message": message,
                                                        "details": {}}})


def _is_refresh(payload):
    """rpc.js:125 的 ``forceRefresh === true``：只有严格 true 才算强制刷新。

    有意差异 5：Python 里 ``True == 1``，若不显式判类型，``{"_refresh": 1}`` 会被误当成
    强制刷新；``is True`` 与 JS 的 ``===`` 对齐。
    """
    return payload.get("_refresh") is True


def _takes_no_payload(endpoint, payload):
    """rpc.js:104/133/175 的空载荷约束（snapshot 例外：payload 非空走未知端点分支）。"""
    if endpoint == "snapshot":
        return
    if payload:
        raise WorkbenchError(f"{endpoint} takes no payload")


def _check_fields(endpoint, payload, allowed):
    """rpc.js:109-111/116-118/156-158/186-188 的白名单校验。"""
    if any(key not in allowed for key in payload):
        raise WorkbenchError(f"Unexpected {endpoint} field")


def create_handler(home, analytics=None, series=None, core=None, command_home=None):
    """``rpc.js:64-231 createRpcHandler`` 的 Python 等价物；返回 ``handle(endpoint, payload)``。

    - ``analytics``：``{endpoint: callable(payload, force) -> value}``（缺省走 compute 子进程，
      即 ``compute.DEFAULT_ANALYTICS``——分析层默认表只有这一份，app 不再另立一份；Node 侧
      对应 ``createRpcHandler(store, deps).deps.analytics``）；
    - ``series``：``callable(ticker, period, limit) -> value``（缺省 compute.series）；
    - ``core``：``{name: callable() -> value}``，name ∈ snapshot-plan/schedule/reconcile；
    - ``command_home``：指令落盘根，缺省 ``home``（服务侧 DSH_HOME）。
    """
    if home is None:
        home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    analytics = dict(compute.DEFAULT_ANALYTICS) if analytics is None else analytics
    if series is None:
        series = compute.series
    if core is None:
        core = {name: (lambda name=name: compute.snapshot_cli(name))
                for name in compute.SNAPSHOT_COMMANDS}
    write_home = home if command_home is None else command_home

    def handle(endpoint, raw_payload):
        try:
            # rpc.js:99-101：载荷必须是对象（非 dict / 数组一律拒绝）
            if not isinstance(raw_payload, dict):
                raise WorkbenchError("Expected an object payload")
            # rpc.js:103：`_refresh` 是绕过缓存的旁路标记，不参与字段校验
            payload = {key: value for key, value in raw_payload.items() if key != "_refresh"}
            force = _is_refresh(raw_payload)

            if endpoint == "snapshot" and not payload:
                # rpc.js:104-107：向客户端声明本 Host 实际提供哪些接口（合并点）
                return {"ok": True, "value": {**store_access.snapshot(home),
                                              "endpoints": store_access.endpoints()}}
            if endpoint == "switch-mode":
                _check_fields(endpoint, payload, SWITCH_MODE_FIELDS)
                return {"ok": True, "value": store_access.switch_mode(
                    home, mode=payload.get("mode"), expected_mode=payload.get("expected_mode"),
                    confirmation=payload.get("confirmation"))}
            if endpoint in ANALYTICS_ENDPOINTS:
                _check_fields(endpoint, payload, ANALYTICS_ENDPOINTS[endpoint])
                provider = analytics.get(endpoint)
                if provider is None:
                    raise WorkbenchError("Analytics provider unavailable")
                return caches.cached(endpoint, payload, force,
                                     lambda: provider(payload, force),
                                     "trading/analytics-unavailable")
            if endpoint == "audit":
                # rpc.js:132-145：空载荷；trades 取不到时仍给出信号/响应链路
                _takes_no_payload(endpoint, payload)
                mode = store_access.snapshot(home)["mode"]
                trades = {"trades": []}
                provider = analytics.get("trades")
                if provider is not None:
                    try:
                        trades = provider({"mode": mode, "limit": 100}, False)
                    except Exception:  # noqa: BLE001 —— 与 rpc.js:140-141 的空 catch 同义
                        trades = {"trades": []}
                return {"ok": True, "value": audit_chain.build_audit_chain(
                    store_access.snapshot(home), trades)}
            if endpoint in ("plan", "schedule", "reconcile"):
                # rpc.js:172-181：只读端点经 trading_core 子命令取数，plan 并入当前 mode
                _takes_no_payload(endpoint, payload)
                name = f"snapshot-{endpoint}"
                if name not in core:
                    raise WorkbenchError("Core bridge unavailable")

                def produce(name=name, endpoint=endpoint):
                    value = core[name]()
                    if endpoint == "plan":
                        return {**value, "mode": store_access.snapshot(home)["mode"]}
                    return value

                return caches.cached(endpoint, payload, force, produce, "trading/core-unavailable")
            if endpoint == "plan-execute":
                # rpc.js:183-210：唯一受约束执行入口；校验通过后原子写指令文件即返回
                _check_fields(endpoint, payload, PLAN_EXECUTE_FIELDS)
                # rpc.js:189 ``payload.action ?? "execute"``：只有 null/undefined 才兜底，
                # ``""`` 必须落到 Unknown plan-execute action（不能像 ``or`` 那样被吞掉）。
                action = payload.get("action")
                if action is None:
                    action = "execute"
                type_ = EXECUTE_ACTIONS.get(action)
                if not type_:
                    raise WorkbenchError(f"Unknown plan-execute action: {action}")
                command_payload = {}
                if type_ == "execute_plan":
                    plan_hash = payload.get("plan_hash")
                    if not isinstance(plan_hash, str) or not plan_hash:
                        raise WorkbenchError("plan-execute requires plan_hash")
                    current = store_access.snapshot(home)["mode"]
                    if payload.get("expected_mode") != current:
                        raise WorkbenchError(
                            f"模式已变化：期望 {payload.get('expected_mode')}，当前 {current}，请刷新后重试")
                    if current == "live" and payload.get("confirmation") != "确认执行":
                        raise WorkbenchError("实时账户执行需输入口令「确认执行」")
                    command_payload["plan_hash"] = plan_hash
                    command_payload["expected_mode"] = payload.get("expected_mode")
                elif type_ == "cancel_plan" and isinstance(payload.get("plan_hash"), str):
                    command_payload["plan_hash"] = payload["plan_hash"]
                # 口令字段到此为止：绝不进入 command_payload（规格 §5.2 P3）
                nonce = compute.write_command(write_home, type_, command_payload)
                return {"ok": True, "value": {"queued": True, "nonce": nonce, "action": action}}
            if endpoint == "series":
                _check_fields(endpoint, payload, SERIES_FIELDS)
                # series.js:28 的解构默认只在 undefined 时生效：null/"" 交给 compute.series
                # 报 Invalid period（不能用 ``or`` 兜底，否则非法值被静默改成 "5m"）。
                period = payload["period"] if "period" in payload else "5m"
                limit = payload["limit"] if "limit" in payload else 300
                return caches.cached(
                    endpoint, payload, force,
                    lambda: series(payload.get("ticker"), period, limit),
                    "trading/series-unavailable")
            raise WorkbenchError("Unknown workbench operation")
        except WorkbenchError as error:
            # rpc.js:226-228：业务失败一律 200 + 失败信封，不抛 HTTP 5xx
            return {"ok": False, "error": {"code": "trading/invalid-operation",
                                           "message": str(error), "details": {}}}

    return handle


def create_app(home=None, dist=None, config=None, analytics=None, series=None, core=None):
    """组装 FastAPI 应用（``platform/server/start.mjs`` 的组装顺序：一份 handle 共享）。"""
    if home is None:
        home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    home = str(home)
    root = Path(dist if dist is not None else DEFAULT_DIST)
    config = load_config(home) if config is None else config
    handle = create_handler(home, analytics=analytics, series=series, core=core)
    endpoints = store_access.endpoints()

    # MCP 工具面（补遗任务 D）：25 工具注册进 MCPServer，端点工具与 HTTP 面共用同一个 handle
    # 实例（规格 §5.2 R6 的结构保证），维护工具走 store_access 的 home 绑定门面。
    mcp_server = MCPServer(name=mcp_tools.SERVER_NAME, version=mcp_tools.SERVER_VERSION)
    bound_tools = mcp_tools.register(mcp_server, handle, mcp_tools.StoreApi(home))
    # json_response=True 对齐 Node 版 enableJsonResponse：无 SSE 依赖，普通 JSON 响应。
    mcp_app = mcp_server.streamable_http_app(json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        """主 app lifespan 里并入 SDK 的 lifespan（session manager 的 task group）。

        ``streamable_http_app()`` 返回的 Starlette app 自带
        ``lifespan=lambda app: session_manager.run()``；不进入它，MCP 会话管理器从未启动，
        ``/mcp`` 的每个请求都会因 ``self._task_group is None`` 失败。SDK 的
        ``StreamableHTTPSessionManager.run()`` 每个实例只能进一次，因此进程内只建一个
        MCPServer（``app.state.mcp``），由 uvicorn 的 lifespan 驱动。
        """
        async with mcp_app.router.lifespan_context(mcp_app):
            yield

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.home = home
    app.state.dist = str(root)
    app.state.config = config
    app.state.handle = handle
    app.state.mcp = mcp_server
    app.state.mcp_app = mcp_app
    app.state.mcp_tools = bound_tools

    @app.exception_handler(Exception)
    async def unhandled(_request, error):
        """500 兜底（service.mjs:84-87）：任何未预期异常都回 ``trading/internal`` 信封。

        Starlette 默认的回退是 ``PlainTextResponse("Internal Server Error")``，那会让
        前端 ``response.json()`` 解析失败并显示解析器原始错误；这里显式接管成同形信封。
        """
        return error_envelope("trading/internal", str(error)[:300], 500)

    def check_auth(authorization):
        token = config.get("token")
        if not token:
            return True
        return authorization == f"Bearer {token}"  # util.mjs:31-33 authorized

    @app.middleware("http")
    async def guard(request, call_next):
        """认证（service.mjs:47-56 的认证顺序：healthz 豁免 → /mcp 与 /api/* 需 token）。

        体上限刻意不在这里判：service.mjs 的 413 只在 ``/api/wb/*`` 分支内、且在白名单与
        content-type 之后生效（否则白名单外请求会先撞 413，静态请求也会被请求头误伤）。
        """
        route = request.url.path
        # ``/mcp`` 的判定必须是「等值或前缀」：SDK 目前只注册裸 ``/mcp``，但将来若挂到
        # ``/mcp/<sub>`` 子路径，纯等值判定会让那些请求绕过鉴权直接落到路由层。
        protected = route.startswith("/api/") or route == "/mcp" or route.startswith("/mcp/")
        if protected and not check_auth(request.headers.get("authorization")):
            # util.mjs:27-29：token 缺失/不匹配 → 401 trading/unauthorized
            return error_envelope("trading/unauthorized", "需要 Bearer token", 401)
        return await call_next(request)

    @app.api_route("/healthz", methods=list(ALL_METHODS))
    async def healthz():
        """service.mjs:47-49：豁免认证的存活探针（Node 侧不判方法，任何方法同响应）。"""
        return {"ok": True, "mode": read_mode(home)}

    @app.post("/api/wb/{endpoint}")
    async def workbench(endpoint: str, request: Request):
        """``service.mjs:54-81``：白名单 → content-type → 体上限 → JSON → handle。"""
        if endpoint not in endpoints:
            # 白名单 404 先于 handle（规格 §5.1 A7 封闭性）
            return error_envelope("trading/unknown-endpoint", f"未知端点 {endpoint}", 404)
        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        if content_type != "application/json":
            return error_envelope("trading/invalid-operation", "Expected application/json", 415)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_PAYLOAD:
            # 快路径：声明就超限时不必读体（util.mjs collectBody 同样先看 content-length）
            return error_envelope("trading/payload-too-large", "请求体超过 1MB 上限", 413)
        body = bytearray()
        # Q-3：按块计数，超限立即返回，不先把整个体读进内存
        # （chunked 传输没有 content-length，只能靠边读边数）。
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_PAYLOAD:
                return error_envelope("trading/payload-too-large", "请求体超过 1MB 上限", 413)
        try:
            payload = json.loads(bytes(body).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            return error_envelope("trading/invalid-operation",
                                  f"请求体不是合法 JSON：{error}", 400)
        if not isinstance(payload, dict):
            return error_envelope("trading/invalid-operation", "Expected an object payload", 400)
        # 阻塞取数让出事件循环：响应与 Node 一致，只是不阻塞其他请求（有意差异 4）
        return JSONResponse(status_code=200, content=await asyncio.to_thread(handle, endpoint,
                                                                            payload))

    @app.api_route("/api/wb/{rest:path}", methods=list(ALL_METHODS))
    async def workbench_fallback(rest: str, request: Request):
        """``service.mjs:54-63`` 的兜底：方法错误 → 405；多段/含斜杠路径 → 404。

        单段的合法 POST 由上一条路由吃掉，这里只接单段以外的形态：
          * 非 POST → 405「仅 POST」（service.mjs:56-58 先判方法再判白名单）；
          * POST 但 ``rest`` 含 ``/``（``a/b``、``snapshot/``、空串）→ 404 unknown-endpoint
            （service.mjs:60 的 ``^[a-z-]+$`` 判定）。
        """
        if request.method != "POST":
            return error_envelope("trading/method-not-allowed", "仅 POST", 405)
        return error_envelope("trading/unknown-endpoint", f"未知端点 {rest}", 404)

    # /mcp：MCP streamable-http 端点（规格 §3.6，SDK 挂载）。
    # 有意差异 10：不用 ``app.mount("/mcp", mcp_app)``——Starlette 的 Mount 只匹配
    # ``/mcp/...``，裸 ``/mcp`` 会由 redirect_slashes 变成 307 跳转；MCP 客户端（含
    # dsh-mcp-client）对 307 的跟随策略不由我们掌握，且每次会话都多一跳。这里把 SDK 的
    # 路由**原样插进主 app 的 router**（路径仍是 ``/mcp``），位置固定在静态兜底之前，
    # 语义与挂载等价且没有跳转。token 中间件的判定路径 ``/mcp`` 与 ``/mcp/`` 前缀因此仍然
    # 精确命中（见 guard 的「等值或前缀」说明）。
    app.router.routes.extend(mcp_app.routes)

    @app.get("/{path:path}")
    async def static_files(path: str):
        """``service.mjs:82``：GET 走静态托管 + SPA 兜底。"""
        return await _serve_static(root, path)

    @app.api_route("/{path:path}", methods=list(NON_GET_METHODS))
    async def non_get_fallback():
        """``service.mjs:83``：静态路径的非 GET 一律 405「仅 GET」信封。"""
        return error_envelope("trading/method-not-allowed", "仅 GET", 405)

    return app


def _read_mode(home):
    """store_access.read_mode 的容错包装：healthz 不应因数据文件损坏而 500。"""
    try:
        return store_access.read_mode(home)
    except Exception:  # noqa: BLE001
        return "unknown"


read_mode = _read_mode


async def _serve_static(root, url_path):
    """静态托管的异步外壳：全部文件系统操作（stat/读取）在线程里做，不阻塞事件循环。"""
    return await asyncio.to_thread(_serve_static_sync, root, url_path)


def _serve_static_sync(root, url_path):
    """``service.mjs:16-40 serveStatic``：解码 → 边界防护 → 文件/SPA 兜底 → MIME。

    有意差异 6：``service.mjs:23`` 用 Node 的 ``path.join(dist, relative)``，它对以 ``/``
    开头的第二段**不重置**（``join('/a','/b') === '/a/b'``），而 Python 的 ``os.path.join``
    会重置（``'/b'``）——因此这里显式 ``lstrip('/')`` 后再拼接，保持与移植源同一落点。

    有意差异 9：``Content-Type`` 走 ``headers=`` 而不是 ``media_type=``。Starlette 会对
    ``text/*`` 追加 ``; charset=utf-8``，而 ``service.mjs:10-14`` 的表里只有 ``.html``
    带 charset（``.js`` 是裸 ``text/javascript``、``.css`` 是裸 ``text/css``）；未收录扩展名
    一律 ``application/octet-stream``，不做任何猜测。
    """
    try:
        relative = "index.html" if url_path in ("", "/") else _decode(f"/{url_path}")
    except ValueError:
        return error_envelope("trading/invalid-operation", "路径编码非法", 400)
    target = os.path.normpath(os.path.join(str(root), relative.lstrip("/")))
    if target != str(root) and not target.startswith(str(root) + os.sep):
        # service.mjs:24-26：归一化后越出 root 一律 403（不泄漏目标路径）
        return error_envelope("trading/forbidden", "路径非法", 403)
    if not os.path.isfile(target):
        # SPA 路由兜底；index.html 也不存在 → 前端未构建
        index = os.path.join(str(root), "index.html")
        if url_path not in ("", "/") and os.path.isfile(index):
            target = index
        else:
            return error_envelope("trading/not-found",
                                  "前端未构建（npm --prefix platform/web run build）", 404)
    try:
        content = Path(target).read_bytes()
    except OSError as error:
        # service.mjs:36-38：读取失败 → 500 trading/internal
        return error_envelope("trading/internal", f"静态文件读取失败：{error}"[:300], 500)
    media_type = MIME.get(os.path.splitext(target)[1]) or "application/octet-stream"
    return Response(content=content, headers={"Content-Type": media_type})


_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _decode(value):
    """``decodeURIComponent`` 等价：%XX 必须成对；解码失败/含 NUL 抛 ValueError。

    ``urllib.parse.unquote`` 默认会吞掉非法转义（``errors="replace"`` 语义），而
    ``decodeURIComponent`` 抛 URIError——这里先自己判非法转义，再解码（service.mjs:19-22
    的 try/catch 落到 400）。
    """
    if _BAD_ESCAPE.search(value):
        raise ValueError(f"invalid escape in {value!r}")
    decoded = unquote(value, errors="strict")
    if "\x00" in decoded:
        raise ValueError("NUL in path")
    return decoded
