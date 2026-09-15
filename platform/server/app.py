"""FastAPI 应用（WP6 补遗 C）：HTTP 面 + 唯一分发函数 handle。

移植源（逐行为准）：
  * ``plugins/workbench/src/rpc.js:97-230`` —— handle(endpoint, payload)：载荷对象性、
    ``_refresh`` 剥离、逐端点字段白名单、snapshot 合并点（rpc.js:106）、switch-mode、
    audit 的 trades 兜底（rpc.js:132-145）、plan/schedule/reconcile（rpc.js:172-182）、
    plan-execute 的动作映射与校验顺序（rpc.js:183-210）、series（rpc.js:211-224）、
    未知端点与 WorkbenchError → ``trading/invalid-operation`` 信封（rpc.js:225-228）；
  * ``platform/server/service.mjs:42-88`` —— 路由顺序、白名单 404 先于 handle、
    content-type 415、1MB 413、坏 JSON 400、静态托管与 SPA 兜底、500 兜底信封；
  * ``platform/server/util.mjs`` —— sendJson / authorized / unauthorized 的等价物。

与 Node 侧的有意差异（均为「规格更严」而非语义变更）：
  1. ``auth`` 在中间件里统一判定（service.mjs 在每个分支里散着判），但判定条件与豁免面
     完全一致：token 非空时 ``/api/*`` 与 ``/mcp`` 需 ``Authorization: Bearer <token>``。
  2. ``/mcp`` 本轮是 405 占位（任务 D 换成真实 MCP 挂载）。
  3. 端点白名单与静态路径在**路由层**判（先于 handle），使「白名单 404 不触达 handle」这一
     不变式由框架保证而不是约定；handle 内部对未知端点仍抛同样的 WorkbenchError。
  4. 阻塞取数（子进程/文件）经 ``asyncio.to_thread`` 让出事件循环——响应内容不变，
     只是不再阻塞其他请求。
  5. 405 的 ``detail`` 用 service.mjs 的错误码 ``trading/method-not-allowed``（本任务规格
     未指定该码，取移植源）。
"""
import asyncio
import json
import mimetypes
import os
import re
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from server import audit_chain, caches, compute, store_access
from server.config import load_config
from server.store_access import WorkbenchError

Body = dict  # 文档用途：handle 的载荷一律是普通 dict

# service.mjs:10-14 的 MIME 表（未收录的扩展名回落 octet-stream）
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

# 分析类端点的默认取数入口（可注入，便于测试替换；Node 侧对应 deps.analytics）
ANALYTICS_DEFAULT = {
    "equity": lambda payload, force: compute.analytics("equity", payload, force),
    "positions": lambda payload, force: compute.analytics("positions", payload, force),
    "correlation": lambda payload, force: compute.analytics("correlation", payload, force),
    "sensitivity": lambda payload, force: compute.analytics("sensitivity", payload, force),
    "risk": lambda payload, force: compute.analytics("risk", payload, force),
    "trades": lambda payload, force: compute.analytics("trades", payload, force),
    "events": lambda payload, force: compute.analytics("events", payload, force),
    "factors": lambda payload, force: compute.analytics("factors", payload, force),
    "ic": lambda payload, force: compute.analytics("ic", payload, force),
    "sources": lambda payload, force: compute.analytics("sources", payload, force),
    "instrument": lambda payload, force: compute.analytics("instrument", payload, force),
    "quality": lambda payload, force: compute.analytics("quality", payload, force),
}


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

    - ``analytics``：``{endpoint: callable(payload, force) -> value}``（缺省走 compute 子进程）；
    - ``series``：``callable(ticker, period, limit) -> value``（缺省 compute.series）；
    - ``core``：``{name: callable() -> value}``，name ∈ snapshot-plan/schedule/reconcile；
    - ``command_home``：指令落盘根，缺省 ``home``（服务侧 DSH_HOME）。
    """
    if home is None:
        home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    analytics = dict(ANALYTICS_DEFAULT) if analytics is None else analytics
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
                action = payload.get("action") or "execute"
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
                _check_fields(endpoint, payload, ("ticker", "period", "limit"))
                return caches.cached(
                    endpoint, payload, force,
                    lambda: series(payload.get("ticker"), payload.get("period") or "5m",
                                   payload.get("limit", 300)),
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

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.home = home
    app.state.dist = str(root)
    app.state.config = config
    app.state.handle = handle

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
        """认证 + 1MB 体上限（service.mjs:55-79 的顺序：认证 → 上限 → 路由）。"""
        route = request.url.path
        if (route.startswith("/api/") or route == "/mcp") and not check_auth(
                request.headers.get("authorization")):
            # util.mjs:27-29：token 缺失/不匹配 → 401 trading/unauthorized
            return error_envelope("trading/unauthorized", "需要 Bearer token", 401)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_PAYLOAD:
            return error_envelope("trading/payload-too-large", "请求体超过 1MB 上限", 413)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        """service.mjs:47-49：豁免认证的存活探针。"""
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
        body = await request.body()
        if len(body) > MAX_PAYLOAD:
            return error_envelope("trading/payload-too-large", "请求体超过 1MB 上限", 413)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            return error_envelope("trading/invalid-operation",
                                  f"请求体不是合法 JSON：{error}", 400)
        if not isinstance(payload, dict):
            return error_envelope("trading/invalid-operation", "Expected an object payload", 400)
        # 阻塞取数让出事件循环：响应与 Node 一致，只是不阻塞其他请求（有意差异 4）
        return JSONResponse(status_code=200, content=await asyncio.to_thread(handle, endpoint,
                                                                            payload))

    @app.get("/api/wb/{endpoint}")
    async def workbench_get(endpoint: str):
        """service.mjs:56-58：非 POST 一律 405（错误码取移植源 method-not-allowed）。"""
        return error_envelope("trading/method-not-allowed", "仅 POST", 405)

    @app.api_route("/mcp", methods=["GET", "POST"])
    async def mcp_placeholder():
        """任务 D 在此挂真实 MCP；本轮按任务约定给 405 占位。"""
        return error_envelope("trading/method-not-allowed", "MCP 未启用（任务 D 接线）", 405)

    @app.get("/{path:path}")
    async def static_files(path: str):
        """``service.mjs:82``：GET 走静态托管 + SPA 兜底。"""
        return _serve_static(root, path)

    return app


def _read_mode(home):
    """store_access.read_mode 的容错包装：healthz 不应因数据文件损坏而 500。"""
    try:
        return store_access.read_mode(home)
    except Exception:  # noqa: BLE001
        return "unknown"


read_mode = _read_mode


def _serve_static(root, url_path):
    """``service.mjs:16-40 serveStatic``：解码 → 边界防护 → 文件/SPA 兜底 → MIME。

    有意差异 6：``service.mjs:23`` 用 Node 的 ``path.join(dist, relative)``，它对以 ``/``
    开头的第二段**不重置**（``join('/a','/b') === '/a/b'``），而 Python 的 ``os.path.join``
    会重置（``'/b'``）——因此这里显式 ``lstrip('/')`` 后再拼接，保持与移植源同一落点。
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
    media_type = MIME.get(os.path.splitext(target)[1]) or (mimetypes.guess_type(target)[0]
                                                           or "application/octet-stream")
    return Response(content=content, media_type=media_type)


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
