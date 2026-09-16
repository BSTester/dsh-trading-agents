"""FastAPI 应用（WP6 补遗 C）：HTTP 面 + 唯一分发函数 handle。

移植源（逐行为准）：
  * ``plugins/workbench/src/rpc.js:97-243`` —— handle(endpoint, payload)：载荷对象性、
    ``_refresh`` 剥离、逐端点字段白名单、snapshot 合并点（rpc.js:104-107）、switch-mode、
    ``confirmation``（空载荷直读，不进缓存；rpc.js:114-119）、``confirm-decide``（唯一人工
    批准通道，字段仅 id/decision；rpc.js:120-126）、audit 的 trades 兜底（rpc.js:145-158）、
    plan/schedule/reconcile（rpc.js:185-195）、plan-execute 的动作映射与校验顺序
    （rpc.js:196-223）、series（rpc.js:224-237）、未知端点与 WorkbenchError →
    ``trading/invalid-operation`` 信封（rpc.js:238-242）；
  * Node 原实现（已退役，见 git 历史 ``aaa5f42^``）—— 路由顺序、白名单 404 先于 handle、
    content-type 415、1MB 413、坏 JSON 400、静态托管与 SPA 兜底、500 兜底信封；
  * Node 服务层原实现（已退役，见 git 历史 ``aaa5f42^``）—— sendJson / authorized / unauthorized 的等价物；
  * ``plugins/workbench/src/analytics.js`` —— 逐端点参数构造（在 ``server.compute``）。

与 Node 侧的有意差异（均为「规格更严」而非语义变更）：
  1. ``auth`` 在中间件里统一判定（Node 服务层原实现逐分支散判），但判定条件与豁免面
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
  5. 405 的 ``detail`` 用 Node 原实现的错误码 ``trading/method-not-allowed``。
  6. 静态路径拼接用 ``lstrip('/')``（见 ``_serve_static_sync`` 的说明）。
  7. 非 POST/非 GET 的兜底路由显式把**所有**方法收进信封（Node 原实现已退役）：
     ``/api/wb/*`` 下非 POST → 405「仅 POST」，多段/含斜杠路径 → 404 unknown-endpoint，
     其余路径非 GET → 405「仅 GET」。否则会落到 Starlette 的 ``{"detail": "Method Not
     Allowed"}``，前端 ``response.json()`` 就拿不到统一信封。
  8. 体上限（413）只在 ``/api/wb/*`` 分支内判、且在白名单与 content-type 之后
     （沿用 Node 原实现已退役的顺序），不再对静态与白名单外请求生效；读取时按块计数
     （``request.stream()``），超限立刻 413，不先整读。
  9. 静态响应用显式 ``Content-Type`` 头而不是 ``media_type=``：Starlette 会给 ``text/*``
     追加 ``; charset=utf-8``，而已退役 Node 原实现的表里只有 ``.html`` 带 charset。
  10. WP7 任务 1：服务内调度器（``server/scheduler.py``，吸收 daemon 常驻循环）。装配走
      ``create_app(scheduler=...)``——None 才建真调度器（测试注入替身即可禁用）；启停并入
      lifespan，``/healthz`` 附带 ``scheduler: {alive, last_error}``。
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

from server import (audit_chain, caches, compute, futu_data, futu_push, mcp_tools,
                    store_access, trading)
from server.config import load_config
from server.store_access import WorkbenchError

Body = dict  # 文档用途：handle 的载荷一律是普通 dict

# 已退役 Node 原实现的 MIME 表：**只认这张手写表**，未收录扩展名回落 octet-stream。
# 刻意不用 ``mimetypes.guess_type``：它会读 /etc/mime.types 等主机文件，同一份代码在不同
# 机器上给出不同 Content-Type（且会给 text/* 追加 charset），与已退役 Node 原实现不可比。
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

MAX_PAYLOAD = 1024 * 1024  # 与 Node 原实现（已退役）collectBody 默认上限一致
DEFAULT_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

# 兜底路由的方法面：Node 原实现（已退役）对每个路径段都只按「是不是 POST/GET」分派，
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

# 空载荷端点（rpc.js:104/115/146/188）
EMPTY_PAYLOAD_ENDPOINTS = ("snapshot", "audit", "confirmation", "plan", "schedule", "reconcile")

# rpc.js:121-123 的 confirm-decide 字段白名单：载荷只有编号与结论，**没有下单参数**——
# 确认通道不能变成下单通道。这也是唯一能批准实盘操作的入口（模型侧不进 MCP 工具面）。
CONFIRM_DECIDE_FIELDS = ("id", "decision")

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
# WP7：因子快照历史的载荷只有 limit（1..120 校验在 compute.factors_history）
FACTORS_HISTORY_FIELDS = ("limit",)
# WP7 任务 3：受约束交易工具的载荷白名单（逐工具定义；与 mcp_tools 的 params 同形）。
# 交易工具的载荷刻意**不含 mode**（模式只认模式文件，杜绝声明模式旁路）也不含口令
# （live 授权=Web 业务确认卡片，不是对话口令）；client_order_id 是幂等编号（可省）。
TRADE_PLACE_FIELDS = ("symbol", "side", "qty", "price", "client_order_id")
TRADE_MODIFY_FIELDS = ("order_id", "symbol", "side", "qty", "price", "client_order_id")
TRADE_CANCEL_FIELDS = ("order_id", "symbol", "client_order_id")
# 账户查询只受模式约束直通 broker；mode 缺省读模式文件（实时查询，不进缓存）
ACCOUNT_QUERY_FIELDS = ("mode",)
# WP8 任务 3：OpenAPI 交易只读端点的载荷白名单（逐端点定义；与 mcp_tools 的工具字段
# 逐键同形，锁定测试比对）。这些端点是**读类**：只受模式约束（mode 缺省读模式文件），
# 业务参数（code/market/exchange/page_flag/...）整体下传 trading.TradeGate._read；
# 时间戳是微秒整数、枚举（order_type/trd_market/exchange）在 mcp_tools 的 schema 层
# 与 OpenApiTrade 的方法层各拒一次（坏参数零网络往返）。实时直通，不进任何缓存。
OPENAPI_TRADE_FIELDS = {
    "trade_max_qty": ("code", "order_type", "price", "order_id", "mode"),
    "orders_open": ("market", "page_flag", "page_size", "mode"),
    "orders_history": ("market", "code", "start", "end", "page_flag", "page_size",
                       "mode"),
    "orders_detail": ("exchange", "order_ids", "mode"),
    "deals_today": ("market", "page_flag", "page_size", "mode"),
    "deals_history": ("market", "code", "start", "end", "page_flag", "page_size",
                      "mode"),
}
# WP8 富途实时直通端点的载荷白名单（深校验在 server/futu_data.py：code 归一/必填/内键）。
# 实时类 TTL 0，不进 CACHE_TTL_MS/ENDPOINT_SHAPE；WP8 任务 2 的基本五类
# （info_basicinfo/info_trading_days/info_search/info_market_state/quote_history_kline_v2）
# 进 TTL 表并在 FutuData 内做 value 层缓存。响应由 futu_data.handle 直接给出。
FUTU_FIELDS = {
    "rt_quote": ("codes",),
    "rt_order_book": ("code",),
    "capital_flow": ("code",),
    "capital_flow_history": ("code", "days"),
    "capital_distribution": ("code",),
    "option_expiration": ("code",),
    "option_chain": ("code", "field_filter"),
    "option_screen": ("filter",),
    # WP8 任务 2：OpenAPI 行情接入的 9 个增量端点（与 mcp_tools 的工具字段同形）
    "market_snapshot": ("codes",),
    "cur_kline": ("code", "num", "ktype", "autype", "extended_time"),
    "rt_data": ("code", "request_section"),
    "rt_ticker": ("code", "num", "period"),
    "info_basicinfo": ("codes",),
    "info_trading_days": ("market", "start", "end"),
    "info_search": ("keyword", "size", "news_type", "sort_type", "lang"),
    "info_market_state": ("codes", "is_contain_ba", "is_contain_overnight"),
    "quote_history_kline_v2": ("code", "start", "end", "ktype", "autype",
                               "num", "extended_time"),
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
    """rpc.js 各空载荷分支（snapshot/audit/confirmation/plan/schedule/reconcile）的等价约束。

    snapshot 例外：payload 非空走未知端点分支（rpc.js:104 只在空载荷时命中快照合并点）。
    """
    if endpoint == "snapshot":
        return
    if payload:
        raise WorkbenchError(f"{endpoint} takes no payload")


def _check_fields(endpoint, payload, allowed):
    """rpc.js:109-111/116-118/156-158/186-188 的白名单校验。"""
    if any(key not in allowed for key in payload):
        raise WorkbenchError(f"Unexpected {endpoint} field")


def create_handler(home, analytics=None, series=None, core=None, command_home=None,
                   trade=None, futu=None):
    """``rpc.js:64-231 createRpcHandler`` 的 Python 等价物；返回 ``handle(endpoint, payload)``。

    - ``analytics``：``{endpoint: callable(payload, force) -> value}``（缺省走 compute 子进程，
      即 ``compute.DEFAULT_ANALYTICS``——分析层默认表只有这一份，app 不再另立一份；Node 侧
      对应 ``createRpcHandler(store, deps).deps.analytics``）；
    - ``series``：``callable(ticker, period, limit) -> value``（缺省 compute.series）；
    - ``core``：``{name: callable() -> value}``，name ∈ snapshot-plan/schedule/reconcile；
    - ``command_home``：指令落盘根，缺省 ``home``（服务侧 DSH_HOME）；
    - ``trade``（WP7 任务 3）：``TradeGate``（缺省按 home 构造默认闸门，broker 经
      ``trading_core.broker`` + ``trading_datasource``；测试注入替身即可离线）；
    - ``futu``（WP8）：富途实时直通提供方（缺省 ``futu_data.FutuData(home=home)``——
      MCP 通道经 ``trading_datasource.futu_mcp.call_tool``；WP8 任务 2 起
      ``futu_channel=openapi`` 时切 OpenAPI REST 后端，通道路由在 futu_data；
      测试注入替身即可离线）。
    """
    if home is None:
        home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    analytics = dict(compute.DEFAULT_ANALYTICS) if analytics is None else analytics
    if series is None:
        series = compute.series
    if core is None:
        core = {name: (lambda name=name: compute.snapshot_cli(name))
                for name in compute.SNAPSHOT_COMMANDS}
        # WP7：因子快照历史走同一 core 桥（limit 由路由透传，compute 侧校验）
        core["factors-history"] = lambda limit=30: compute.factors_history(limit)
    if trade is None:
        trade = trading.TradeGate(home)
    if futu is None:
        futu = futu_data.FutuData(home=home)
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
            if endpoint == "confirmation":
                # rpc.js:114-119：空载荷直读内存态，**不进缓存**——缓存住「待确认」会让
                # 界面拿到一个已经处理掉的请求。跨进程边界见 store_access 文件头：
                # 这里只反映服务进程自身发起的确认，看不到 Harness（Node）进程的待确认。
                _takes_no_payload(endpoint, payload)
                return {"ok": True, "value": {"pending": store_access.confirmation_view(home),
                                              "ttl_ms": store_access.CONFIRM_TTL_MS}}
            if endpoint == "confirm-decide":
                # rpc.js:120-126：唯一能批准实盘操作的通道；载荷只有编号与结论。
                _check_fields(endpoint, payload, CONFIRM_DECIDE_FIELDS)
                return {"ok": True, "value": store_access.decide_confirmation(
                    home, confirmation_id=payload.get("id"), decision=payload.get("decision"))}
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
            if endpoint == "factors-history":
                # WP7：因子快照历史（服务定时收集的只读面板数据），与 plan/schedule/reconcile
                # 同走 core 桥 + TTL 缓存；limit 区间校验在 compute.factors_history
                # （ComputeError 交 cached() 落成 trading/core-unavailable）。
                _check_fields(endpoint, payload, FACTORS_HISTORY_FIELDS)
                provider = core.get("factors-history")
                if provider is None:
                    raise WorkbenchError("Core bridge unavailable")
                return caches.cached(endpoint, payload, force,
                                     lambda: provider(payload.get("limit")),
                                     "trading/core-unavailable")
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
            if endpoint == "trade_place":
                # WP7 任务 3：受约束交易写路径——闸门（模式→风控 8 规则→kill→业务确认）
                # 在 server/trading.py，唯一触达 broker 的放行方式是 Web 确认卡片批准。
                # **响应可能是长阻塞**：live 下确认 TTL=120s（store_access.CONFIRM_TTL_MS，
                # 到期自动拒绝 fail-closed），HTTP 路由经 asyncio.to_thread 阻塞等判决。
                # 与 preset 的关系：agent.cordis.yml 的 quant-platform-mcp 行
                # toolCallTimeoutMs=120000 与确认 TTL 同值——模型侧会在 120s 先看到
                # 工具超时，而闸门继续等到 TTL 到期按拒绝收尾，订单绝不会在无批准下
                # 提交；任务 5（一键安装）建议把该行调到 180000 留出作答余量。
                # 与 plan-execute 同类：动作端点不进 ENDPOINT_SHAPE/CACHE_TTL_MS 表
                # （写操作与实时查询一律 TTL 0，不落任何缓存）。
                _check_fields(endpoint, payload, TRADE_PLACE_FIELDS)
                return trade.place(payload)
            if endpoint == "trade_modify":
                # 同上：完整闸门链 + Web 确认；改单在 broker 层=撤旧+重下（TOOL-LIMITS）
                _check_fields(endpoint, payload, TRADE_MODIFY_FIELDS)
                return trade.modify(payload)
            if endpoint == "trade_cancel":
                _check_fields(endpoint, payload, TRADE_CANCEL_FIELDS)
                return trade.cancel(payload)
            if endpoint in ("account_positions", "account_orders", "account_funds"):
                # 账户查询：只受模式约束直通 broker（mode 缺省读模式文件）；
                # broker 异常在闸门内信封化为 trading/broker-unavailable，不 500。
                _check_fields(endpoint, payload, ACCOUNT_QUERY_FIELDS)
                return getattr(trade, endpoint.replace("account_", ""))(payload.get("mode"))
            if endpoint in OPENAPI_TRADE_FIELDS:
                # WP8 任务 3：OpenAPI 交易只读端点（6 个）——载荷白名单在这里拒，模式
                # 约束与通道路由在 trade._read（broker 缺 OpenAPI 通道时信封为
                # trading/openapi-unavailable，指引 scripts/futu_auth.py --openapi）。
                # 实时直通：不进 caches.cached（TTL 0，与 account_* 同类）。
                _check_fields(endpoint, payload, OPENAPI_TRADE_FIELDS[endpoint])
                return getattr(trade, endpoint)(payload)
            if endpoint in futu_data.FUTU_TOOLS:
                # WP8 富途实时直通：skills 需要而本地无缓存的数据由服务端实时经富途获取。
                # 浅白名单在这里拒（与其他端点同形），深校验（code 归一/必填/内键/上游
                # 陷阱如 option_screen 的 field_filter）在 futu_data 数据方法里。
                # 通道（mcp|openapi）与 TTL 缓存（基本五类的 value 层缓存）都在
                # futu_data.handle 内解决——本层只透传，不经 caches.cached。
                _check_fields(endpoint, payload, FUTU_FIELDS[endpoint])
                if force:
                    # ``refresh`` 旁路透传：TTL 端点在 FutuData 内识别 _refresh 重取
                    payload = {**payload, "_refresh": True}
                return futu.handle(endpoint, payload)
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


def create_app(home=None, dist=None, config=None, analytics=None, series=None, core=None,
               scheduler=None, futu=None, push=None):
    """组装 FastAPI 应用（沿用 Node 原实现已退役的组装顺序：一份 handle 共享）。

    ``scheduler``（WP7 任务 1）：传入即用（测试注入替身/禁用）；None 才建真调度器
    （``Scheduler(build_tick(home), interval=60.0)``），lifespan 启停、/healthz 上报。

    ``push``（WP8 任务 4）：富途 WS 推送运行时（``futu_push.PushRuntime``，测试注入替身
    即可离线）。缺省按 home 建一个——**只有** ``futu_channel=openapi`` 且 OpenAPI 凭据
    可用时才真正建连接，否则 lifespan 零副作用、``/healthz`` 如实报 ``enabled:false``。
    ``futu`` 注入口用于把同一个行情快照缓存接进直通层（``rt_quote`` 可命中推送数据）；
    缺省按 home 建 ``FutuData`` 并挂上 ``push.quote_cache``。
    """
    if home is None:
        home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    home = str(home)
    root = Path(dist if dist is not None else DEFAULT_DIST)
    config = load_config(home) if config is None else config
    push = push if push is not None else futu_push.PushRuntime(home=home)
    if futu is None:
        futu = futu_data.FutuData(home=home, push=push.quote_cache)
    handle = create_handler(home, analytics=analytics, series=series, core=core, futu=futu)
    endpoints = store_access.endpoints()
    if scheduler is None:
        # 延迟导入：注入替身的调用（绝大多数测试）不必承担 trading_core 的导入
        # （与 compute._load_write_command 的惰性口径一致）。
        from server import scheduler as scheduler_module
        scheduler = scheduler_module.Scheduler(scheduler_module.build_tick(home), interval=60.0)

    # MCP 工具面（WP6 补遗 D + WP7 + WP8）：56 工具注册进 MCPServer，端点工具与 HTTP 面
    # 共用同一个 handle 实例（规格 §5.2 R6 的结构保证），维护工具走 store_access 的
    # home 绑定门面。
    # 52 个 HTTP 端点里 ``confirm-decide`` **有意不进工具面**（人工决定通道，见 mcp_tools）。
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

        WP7 任务 1：同处启停服务内调度器——start 在 MCP lifespan 之前，stop 放
        finally（MCP 启动失败也要停线程）；stop 自带 join，优雅退出不悬挂。

        WP8 任务 4：同处启停富途 WS 推送（行情 + 交易）——``push.start()`` 只在
        ``futu_channel=openapi`` 且凭据可用时建连接，否则立即返回 False（零副作用）；
        stop 放 finally（MCP 启动失败也要断开推送连接）。
        """
        _app.state.scheduler.start()
        try:
            await _app.state.push.start()
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            await _app.state.push.stop()
            _app.state.scheduler.stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.home = home
    app.state.dist = str(root)
    app.state.config = config
    app.state.handle = handle
    app.state.scheduler = scheduler
    app.state.push = push
    app.state.futu = futu
    app.state.mcp = mcp_server
    app.state.mcp_app = mcp_app
    app.state.mcp_tools = bound_tools

    @app.exception_handler(Exception)
    async def unhandled(_request, error):
        """500 兜底（Node 原实现已退役）：任何未预期异常都回 ``trading/internal`` 信封。

        Starlette 默认的回退是 ``PlainTextResponse("Internal Server Error")``，那会让
        前端 ``response.json()`` 解析失败并显示解析器原始错误；这里显式接管成同形信封。
        """
        return error_envelope("trading/internal", str(error)[:300], 500)

    def check_auth(authorization):
        token = config.get("token")
        if not token:
            return True
        return authorization == f"Bearer {token}"  # Node 原实现（已退役）的 authorized

    @app.middleware("http")
    async def guard(request, call_next):
        """认证（Node 原实现的认证顺序：healthz 豁免 → /mcp 与 /api/* 需 token）。

        体上限刻意不在这里判：Node 原实现的 413 只在 ``/api/wb/*`` 分支内、且在白名单与
        content-type 之后生效（否则白名单外请求会先撞 413，静态请求也会被请求头误伤）。
        """
        route = request.url.path
        # ``/mcp`` 的判定必须是「等值或前缀」：SDK 目前只注册裸 ``/mcp``，但将来若挂到
        # ``/mcp/<sub>`` 子路径，纯等值判定会让那些请求绕过鉴权直接落到路由层。
        protected = route.startswith("/api/") or route == "/mcp" or route.startswith("/mcp/")
        if protected and not check_auth(request.headers.get("authorization")):
            # Node 原实现（已退役）：token 缺失/不匹配 → 401 trading/unauthorized
            return error_envelope("trading/unauthorized", "需要 Bearer token", 401)
        return await call_next(request)

    @app.api_route("/healthz", methods=list(ALL_METHODS))
    async def healthz():
        """Node 原实现（已退役）：豁免认证的存活探针（不判方法，任何方法同响应）。

        WP7 任务 1：附带调度器存活态——``alive`` 线程是否在跑，``last_error`` 最近一次
        tick 异常记录（成功不清除，None 即从未出错）。截断 ≤300 字符对齐既有
        ``str(error)[:300]`` 惯例；全量 traceback 属于日志语义，这里只留故障存在性的证据。

        WP8 任务 4：附带 ``push``（行情/交易两条 WS 的 connected/已鉴权/最后消息时间/
        重连次数/最后错误）。推送是旁路加速，``status()`` 的任何异常都被
        ``futu_push.safe_status`` 收敛成同形状的失败态——**绝不影响 healthz 主字段**。
        """
        last_error = scheduler.last_error
        return {"ok": True, "mode": read_mode(home),
                "scheduler": {"alive": bool(scheduler.alive),
                              "last_error": None if last_error is None else str(last_error)[:300]},
                "push": futu_push.safe_status(push)}

    @app.post("/api/wb/{endpoint}")
    async def workbench(endpoint: str, request: Request):
        """Node 原实现（已退役）：白名单 → content-type → 体上限 → JSON → handle。"""
        if endpoint not in endpoints:
            # 白名单 404 先于 handle（规格 §5.1 A7 封闭性）
            return error_envelope("trading/unknown-endpoint", f"未知端点 {endpoint}", 404)
        content_type = request.headers.get("content-type", "").split(";")[0].strip()
        if content_type != "application/json":
            return error_envelope("trading/invalid-operation", "Expected application/json", 415)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_PAYLOAD:
            # 快路径：声明就超限时不必读体（Node 原实现 collectBody 同样先看 content-length）
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
        """Node 原实现（已退役）的兜底：方法错误 → 405；多段/含斜杠路径 → 404。

        单段的合法 POST 由上一条路由吃掉，这里只接单段以外的形态：
          * 非 POST → 405「仅 POST」（Node 原实现先判方法再判白名单）；
          * POST 但 ``rest`` 含 ``/``（``a/b``、``snapshot/``、空串）→ 404 unknown-endpoint
            （Node 原实现的 ``^[a-z-]+$`` 判定）。
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
        """Node 原实现（已退役）：GET 走静态托管 + SPA 兜底。"""
        return await _serve_static(root, path)

    @app.api_route("/{path:path}", methods=list(NON_GET_METHODS))
    async def non_get_fallback():
        """Node 原实现（已退役）：静态路径的非 GET 一律 405「仅 GET」信封。"""
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
    """Node 原实现（已退役）的 serveStatic：解码 → 边界防护 → 文件/SPA 兜底 → MIME。

    有意差异 6：Node 原实现用 ``path.join(dist, relative)``，它对以 ``/``
    开头的第二段**不重置**（``join('/a','/b') === '/a/b'``），而 Python 的 ``os.path.join``
    会重置（``'/b'``）——因此这里显式 ``lstrip('/')`` 后再拼接，保持与移植源同一落点。

    有意差异 9：``Content-Type`` 走 ``headers=`` 而不是 ``media_type=``。Starlette 会对
    ``text/*`` 追加 ``; charset=utf-8``，而已退役 Node 原实现的表里只有 ``.html``
    带 charset（``.js`` 是裸 ``text/javascript``、``.css`` 是裸 ``text/css``）；未收录扩展名
    一律 ``application/octet-stream``，不做任何猜测。
    """
    try:
        relative = "index.html" if url_path in ("", "/") else _decode(f"/{url_path}")
    except ValueError:
        return error_envelope("trading/invalid-operation", "路径编码非法", 400)
    target = os.path.normpath(os.path.join(str(root), relative.lstrip("/")))
    if target != str(root) and not target.startswith(str(root) + os.sep):
        # Node 原实现（已退役）：归一化后越出 root 一律 403（不泄漏目标路径）
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
        # Node 原实现（已退役）：读取失败 → 500 trading/internal
        return error_envelope("trading/internal", f"静态文件读取失败：{error}"[:300], 500)
    media_type = MIME.get(os.path.splitext(target)[1]) or "application/octet-stream"
    return Response(content=content, headers={"Content-Type": media_type})


_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def _decode(value):
    """``decodeURIComponent`` 等价：%XX 必须成对；解码失败/含 NUL 抛 ValueError。

    ``urllib.parse.unquote`` 默认会吞掉非法转义（``errors="replace"`` 语义），而
    ``decodeURIComponent`` 抛 URIError——这里先自己判非法转义，再解码（Node 原实现已退役的
    try/catch 落到 400）。
    """
    if _BAD_ESCAPE.search(value):
        raise ValueError(f"invalid escape in {value!r}")
    decoded = unquote(value, errors="strict")
    if "\x00" in decoded:
        raise ValueError("NUL in path")
    return decoded
