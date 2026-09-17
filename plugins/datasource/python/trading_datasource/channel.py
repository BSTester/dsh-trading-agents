"""通道分派：富途取数的 ``openapi`` 优先 / ``mcp`` 回退（WP13 任务 1，规格 §8.1）。

为什么在本包：``trading_core``（core）与 ``workbench`` 脚本都要做同一件事——读
``futu_channel``、判 OpenAPI 凭据就绪、选通道取数。只有放在双方共同依赖的
``trading_datasource`` 里才可能**只有一份实现**；``platform/server/futu_data.py`` 与
``trading_core/research_sync.py`` 的 ``openapi_ready``/``load_channel`` 已改为委托本模块，
「凭据是否可用」「当前通道是什么」从此只有一个答案（历史上 core 侧是刻意镜像 + 等价测试）。

三条语义（规格 §8.1）：

  * ``openapi`` 且凭据就绪 → REST 调用；**异常原样上抛**（不静默换通道——静默换通道会让
    限频/权限/参数错误伪装成 MCP 行为，排障时看不到真实原因）；
  * ``openapi`` 但凭据未就绪 → **回退 mcp**，返回的第二元素标 ``"mcp(fallback)"``
    （可观测；不回退才是错的——没配凭据就彻底不可用会白断掉数据链）；
  * ``mcp``（默认）→ mcp，标 ``"mcp"``。

两通道都失败 → 异常上抛（宁缺毋假：调用点负责如实上报，不写占位行）。

边界（刻意不做）：本模块只做「通道选择 + 一次调用」，**不做业务聚合、不改落库口径**。
清洗/落库留在各调用点；两边**参数形状差异**（如经济日历 mcp 用 ``YYYYMMDD``、REST 用
``YYYY-MM-DD``）由调用点显式给出（``params`` 给 mcp、``openapi_params`` 给 REST），
不藏进助手——藏起来调用点就看不清两边差异了。
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

#: ``trading-platform.json`` 的 ``futu_channel`` 合法值（与 server/futu_data 既有常量同值）
CHANNEL_MCP = "mcp"
CHANNEL_OPENAPI = "openapi"

#: 回退标记（返回值第二元素）：调用方可据此告警/展示，语义是「本次实际走了 mcp」
CHANNEL_MCP_FALLBACK = "mcp(fallback)"

#: ``trading-platform.json`` 文件名（与 server/config.py 同一落点）
CONFIG_FILENAME = "trading-platform.json"


def config_home(home=None):
    """数据根目录：显式 ``home`` > ``$DSH_HOME`` > ``~/.dsh``（与 server/config 同口径）。"""
    return str(home or os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def config_path(home=None):
    """``<home>/trading-platform.json``。"""
    return Path(config_home(home)) / CONFIG_FILENAME


def channel_of(home=None):
    """读 ``futu_channel``（openapi|mcp，默认 mcp）。

    容错语义与 ``server/futu_data.load_channel`` 逐字一致（历史实现，本函数即其下沉）：
    缺文件 → 默认 mcp；坏 JSON → ``ValueError``（不静默吞配置错误）；非法值/缺键 → mcp。
    """
    path = config_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return CHANNEL_MCP
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"trading-platform.json 解析失败：{error}") from error
    channel = raw.get("futu_channel") if isinstance(raw, dict) else None
    return channel if channel in (CHANNEL_MCP, CHANNEL_OPENAPI) else CHANNEL_MCP


def openapi_ready(credential_path=None):
    """OpenAPI 凭据是否可用（**全仓库唯一判定**，实现自 server/futu_data 下沉）。

    oauth → access_token/refresh_token 至少有一个（可刷新）；
    appkey → app_key 与 private_key_path 齐备**且私钥文件可加载**
    （``AppKeySigner.from_path``：文件缺失/坏 PEM/算法不支持/私钥类型不符 → 不可用）；
    其余（文件缺失/坏 JSON/mode 未配置）→ False。

    「私钥可加载」是凭据就绪的一部分：只看路径存在会让实盘写穿过闸门（**人工确认被
    消耗**）后才在签名时失败；判在这里则确认零消耗、零 HTTP 调用。
    """
    from .futu_openapi import CredentialStore  # noqa: PLC0415 —— 避免导入期拉起 cryptography
    try:
        cred = CredentialStore(credential_path).load()
    except (OSError, ValueError):
        return False
    mode = cred.get("mode")
    if mode == "oauth":
        return bool(cred.get("access_token") or cred.get("refresh_token"))
    if mode == "appkey":
        if not (cred.get("app_key") and cred.get("private_key_path")):
            return False
        from .futu_openapi import AppKeySigner  # noqa: PLC0415
        try:
            AppKeySigner.from_path(cred["private_key_path"],
                                   cred.get("algorithm", "Ed25519"))
        except Exception:  # noqa: BLE001 —— 私钥缺失/坏 PEM/算法不支持 → 凭据不可用
            return False
        return True
    return False


#: OpenApiClient 单例表：key = 凭据路径。**为什么缓存**：token 续期状态在 client 上，
#: 每次取数新建一个 client 会让同一进程内反复刷新 token（每次取数都多一跳）。
_CLIENTS = {}


def openapi_client(credential_path=None):
    """共用 ``OpenApiClient``（按凭据路径缓存；避免同进程反复刷新 token）。"""
    key = str(credential_path or "")
    client = _CLIENTS.get(key)
    if client is None:
        from .futu_openapi import CredentialStore, OpenApiClient  # noqa: PLC0415
        client = OpenApiClient(CredentialStore(credential_path))
        _CLIENTS[key] = client
    return client


def reset_clients():
    """清空 client 缓存（测试隔离用；生产无需调用）。"""
    _CLIENTS.clear()


#: 方法组名 → 类（惰性导入：避免导入期拉起 cryptography）。名字与 ``method`` 的点号前缀一致。
_GROUP_CLASSES = None


def _group_classes():
    global _GROUP_CLASSES
    if _GROUP_CLASSES is None:
        from .futu_openapi import (  # noqa: PLC0415
            OpenApiBasicData, OpenApiDerivatives, OpenApiF10, OpenApiIpo, OpenApiPlate,
            OpenApiScreen, OpenApiShort, OpenApiSimTrade, OpenApiWatchlist)
        _GROUP_CLASSES = {"basic": OpenApiBasicData, "derivatives": OpenApiDerivatives,
                          "f10": OpenApiF10, "ipo": OpenApiIpo, "plate": OpenApiPlate,
                          "screen": OpenApiScreen, "short": OpenApiShort,
                          "simtrade": OpenApiSimTrade, "watchlist": OpenApiWatchlist}
    return _GROUP_CLASSES


def call_openapi(client, method, params):
    """按 ``"组.方法"`` 点号路径调用（如 ``"f10.statements"``、``"basic.rehab"``）。

    未知组/未知方法 → ``ValueError``（本地拒绝，零网络往返）——与传输层
    「签名即白名单」同一口径：调用点写错方法名要立刻炸，不能悄悄走到别的方法上。
    """
    if not isinstance(method, str) or "." not in method:
        raise ValueError(f"method 形如 'f10.statements'，收到 {method!r}")
    group_name, name = method.split(".", 1)
    group_cls = _group_classes().get(group_name)
    if group_cls is None:
        raise ValueError(f"未知方法组：{group_name!r}（允许：{sorted(_group_classes())}）")
    target = getattr(group_cls(client), name, None)
    if target is None:
        raise ValueError(f"方法组 {group_name} 无方法 {name!r}")
    return target(**params)


def fetch(tool, params, *, method, openapi_params=None, adapter=None, home=None,
          client=None, credential_path=None, mcp_call=None):
    """按通道取一次数，返回 ``(data, channel_used)``。

    ``tool``/``params``       —— MCP 工具名与 MCP 侧参数（原样透传 ``call_tool``）
    ``method``                —— REST 侧 ``"组.方法"``（如 ``"f10.valuation_detail"``）
    ``openapi_params``        —— REST 侧参数；缺省复用 ``params``（两边同形时省略）
    ``adapter``               —— 可选 ``data -> data`` 归一（两边响应形状确有差异时用；
                                 本函数不猜形状，归一由调用点显式声明）
    ``client``                —— 注入即可钉住 openapi 通道（测试确定性；与 server
                                 ``FutuData`` 的「注入替身视为可用」同口径）
    ``mcp_call``              —— 注入 MCP 替身（缺省 ``futu_mcp.call_tool``）

    回退语义：``channel=openapi`` 且 ``client`` 未注入且凭据未就绪 → 走 mcp 并标
    ``"mcp(fallback)"``；此时**不尝试** REST（避免明知无凭据还发一次注定失败的请求）。
    """
    channel = channel_of(home)
    if channel == CHANNEL_OPENAPI and (client is not None or openapi_ready(credential_path)):
        used_client = client if client is not None else openapi_client(credential_path)
        kwargs = dict(openapi_params if openapi_params is not None else params)
        data = call_openapi(used_client, method, kwargs)
        return (_apply(adapter, data), CHANNEL_OPENAPI)
    call = mcp_call if mcp_call is not None else _mcp_call()
    data = call(tool, params)
    used = CHANNEL_MCP_FALLBACK if channel == CHANNEL_OPENAPI else CHANNEL_MCP
    return (_apply(adapter, data), used)


def _apply(adapter, data):
    return data if adapter is None else adapter(data)


def _mcp_call():
    from .futu_mcp import call_tool  # noqa: PLC0415 —— 惰性导入（避免导入期建会话）
    return call_tool


# ---------------------------------------------------------------------------
# WP13 任务 2：模拟交易通道适配器
# ---------------------------------------------------------------------------
# 为什么需要适配器：``trading_core.broker`` 的函数签名与 ``TOOLS`` 映射被 execute.run /
# planner / reconcile / 平台闸门共同依赖，**换通道不应改动它们**（调用方零改动是硬约束）。
# 因此把「MCP 工具名 + MCP 形状参数」翻译成「REST 方法 + REST 形状参数」的选择放在
# **call 可调用对象的构造处**：``sim_call()`` 返回与 ``futu_mcp.call_tool`` 同签名的
# callable，openapi 就绪则走 REST，否则原样交给 MCP。
#
# 翻译表（形状差异全部显式列出，不藏进黑箱——本模块的既有纪律）：
#   tool（MCP）                    → REST 方法                 形状差异
#   sim_trade_account_list         → simtrade.account_list     无
#   sim_trade_position_list        → simtrade.position_list    market 为 int market_id（两边一致）
#   sim_trade_cash_info            → simtrade.cash_info        无
#   sim_trade_input_order          → simtrade.input_order      market 可为链名→需转 market_id；
#                                                              qty/price MCP 收数值、REST 收字符串
#   sim_trade_cancel_order         → simtrade.cancel_order     market 实测必须放 body（官方页面
#                                                              未列、示例无请求体）；参数原样翻译
#   sim_trade_history_order_list   → simtrade.history_order_list MCP 传 start/end 日期串；
#                                                              REST 要 market + 微秒 int
#   sim_trade_max_buy_sell         → simtrade.max_buy_sell     REST 实测必填 market → 由账户缓存解析

#: 模拟交易 MCP 工具名 → ``"组.方法"``（REST 侧调用路径）。
SIM_TOOL_ROUTES = {
    "sim_trade_account_list": "simtrade.account_list",
    "sim_trade_position_list": "simtrade.position_list",
    "sim_trade_cash_info": "simtrade.cash_info",
    "sim_trade_input_order": "simtrade.input_order",
    "sim_trade_cancel_order": "simtrade.cancel_order",
    "sim_trade_history_order_list": "simtrade.history_order_list",
    "sim_trade_max_buy_sell": "simtrade.max_buy_sell",
}

#: 账本里 start/end 是 ``YYYY-MM-DD``；REST 要微秒 int。东八区为仓库统一展示口径
#: （core 各模块 ``_TZ8`` 同值）——日界按东八区切，与交易日/对账窗口一致。
_TZ8 = timezone(timedelta(hours=8))


def to_micros(value):
    """``YYYY-MM-DD``（或 ``datetime``/int）→ 微秒 int（**当日 00:00:00.000000**，东八区）。

    int 原样返回（调用方已给微秒）；``None`` → ``None``；解析失败抛 ``ValueError``
    （不静默丢弃时间窗——丢了就查成另一个区间）。用于区间**起点**（``time_begin``）。
    """
    return _micros_of(value, end_of_day=False)


def to_micros_end(value):
    """同 ``to_micros``，但 ``YYYY-MM-DD`` 取**当日 23:59:59.999999**（东八区）。

    用于区间**终点**（``time_end``）。实测教训（2026-09-16）：
    ``/sim-trade/{acc_id}/history-orders`` 的窗口是闭区间微秒——若终点取 00:00:00，
    区间宽度为零，**当天订单一条都查不到**（对账会静默看不见当日成交）。真机对照：
    ``time_end`` 取 00:00 时返回 0 条，取 23:59:59.999999 时返回当日已撤订单。
    """
    return _micros_of(value, end_of_day=True)


def _micros_of(value, *, end_of_day):
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = datetime.fromisoformat(text)
        except ValueError as error:
            raise ValueError(f"时间参数无法解析为 YYYY-MM-DD：{value!r}") from error
    if not isinstance(value, datetime):
        raise ValueError(f"时间参数类型不支持（需 YYYY-MM-DD 或微秒 int）：{value!r}")
    moment = value if value.tzinfo else value.replace(tzinfo=_TZ8)
    if end_of_day and (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
        moment = moment.replace(hour=23, minute=59, second=59, microsecond=999999)
    return int(moment.timestamp() * 1_000_000)


def sim_call(home=None, *, client=None, mcp_call=None, credential_path=None):
    """构造模拟交易的通道分派 callable（与 ``futu_mcp.call_tool`` 同签名）。

    用法（调用方零改动，只换 call 的来源）::

        call = channel.sim_call(home)          # 生产
        call("sim_trade_account_list", {})     # 与原来完全一样

    * openapi 通道且凭据就绪（或注入了 ``client``）→ 走 REST（异常**原样上抛**：
      与 ``fetch`` 同语义，不静默换通道——静默换通道会让限价/参数错误伪装成 MCP 行为）；
    * 其余（默认 mcp / openapi 但无凭据）→ 原样交给 MCP，行为与改造前逐字一致；
    * **未知工具名**（含 live 的 ``account_*`` / ``trading_*``）→ 原样交给 MCP：
      本适配器只接管翻译表内的 7 个模拟交易工具，绝不吞掉其他调用。

    ``acc_id → market_id`` 解析：``history_order_list``/``max_buy_sell`` 的 REST 形态
    实测必填 market，而 MCP 调用点不带它——适配器按 acc_id 从账户列表解析一次并缓存
    在本次构造的实例里（调用点都先列账户，缓存必然命中）。解析不到时**不猜**：
    返回 None 让 REST 层如实缺参失败。
    """
    channel = channel_of(home)
    rest_ready = client is not None or openapi_ready(credential_path)
    use_rest = channel == CHANNEL_OPENAPI and rest_ready
    rest_client = client if client is not None else (openapi_client(credential_path)
                                                     if use_rest else None)
    mcp = mcp_call
    market_cache = {}

    def _mcp_or_raise():
        nonlocal mcp
        if mcp is None:
            mcp = _mcp_call()
        return mcp

    def _market_of(acc_id, explicit=None):
        """market 归一：显式值优先（链名→market_id），否则按 acc_id 查账户列表（缓存）。"""
        from .market_ids import sim_market_id  # noqa: PLC0415 —— 轻量模块
        resolved = sim_market_id(explicit)
        if resolved is not None:
            return resolved
        key = str(acc_id)
        if key not in market_cache:
            accounts = call_openapi(rest_client, "simtrade.account_list", {}) or {}
            for row in accounts.get("accounts") or []:
                if isinstance(row, dict) and row.get("account_id") is not None:
                    market_cache[str(row["account_id"])] = row.get("market_id")
        return market_cache.get(key)

    def _rest(tool, params):
        from .market_ids import sim_market_id  # noqa: PLC0415
        p = dict(params or {})
        acc_id = p.get("acc_id")
        if tool == "sim_trade_account_list":
            return call_openapi(rest_client, "simtrade.account_list", {})
        if tool == "sim_trade_cash_info":
            return call_openapi(rest_client, "simtrade.cash_info", {"acc_id": acc_id})
        if tool == "sim_trade_position_list":
            return call_openapi(rest_client, "simtrade.position_list",
                                {"acc_id": acc_id,
                                 "market": sim_market_id(p.get("market"))})
        if tool == "sim_trade_input_order":
            market = sim_market_id(p.get("market"))
            if market is None:
                # 不猜市场：链名未知/缺参时本地拒绝（零网络往返），比后端报缺参更早更清楚
                raise ValueError(f"input_order 的 market 无法归一为 market_id：{p.get('market')!r}")
            return call_openapi(rest_client, "simtrade.input_order", {
                "acc_id": acc_id, "market": market, "symbol": p.get("symbol"),
                "order_type": p.get("order_type"), "order_side": p.get("order_side"),
                "qty": p.get("qty"), "price": p.get("price")})
        if tool == "sim_trade_cancel_order":
            # market 实测必须在 body 携带（官方页面未列）——调用方本就传，原样翻译
            return call_openapi(rest_client, "simtrade.cancel_order",
                                {"acc_id": acc_id, "order_id": p.get("order_id"),
                                 "market": _market_of(acc_id, p.get("market"))})
        if tool == "sim_trade_history_order_list":
            return call_openapi(rest_client, "simtrade.history_order_list", {
                "acc_id": acc_id,
                "market": _market_of(acc_id, p.get("market")),
                "time_begin": to_micros(p.get("start")),
                "time_end": to_micros_end(p.get("end")),
                "page_size": p.get("page_size"), "next_key": p.get("next_key")})
        if tool == "sim_trade_max_buy_sell":
            return call_openapi(rest_client, "simtrade.max_buy_sell", {
                "acc_id": acc_id, "symbol": p.get("symbol"),
                "order_type": p.get("order_type"),
                "market": _market_of(acc_id, p.get("market")),
                "price": p.get("price"), "order_id": p.get("order_id")})
        raise ValueError(f"未接入 REST 的模拟交易工具：{tool!r}")  # pragma: no cover

    def call(tool, params, timeout=None):
        """与 ``futu_mcp.call_tool(name, arguments, timeout=30)`` 同签名。"""
        if use_rest and tool in SIM_TOOL_ROUTES:
            return _rest(tool, params)
        mcp_fn = _mcp_or_raise()
        if timeout is None:
            return mcp_fn(tool, params)
        return mcp_fn(tool, params, timeout=timeout)

    return call
