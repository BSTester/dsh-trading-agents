"""模拟交易通道适配器（WP13 任务 2；WP13 审查次要项从 ``channel.py`` 拆出）。

通道分派（``channel.fetch``）与模拟交易适配器（本模块）职责不同：前者是通用的
「openapi 优先 / mcp 回退」取数入口，后者是**给既有 ``trading_core.broker`` 调用点
换一根 call 的来源**。拆开是为了让 ``channel.py`` 聚焦通道选择（文件不再一个顶两个）。

为什么需要适配器：``trading_core.broker`` 的函数签名与 ``TOOLS`` 映射被 execute.run /
planner / reconcile / 平台闸门共同依赖，**换通道不应改动它们**（调用方零改动是硬约束）。
因此把「MCP 工具名 + MCP 形状参数」翻译成「REST 方法 + REST 形状参数」的选择放在
**call 可调用对象的构造处**：``sim_call()`` 返回与 ``futu_mcp.call_tool`` 同签名的
callable，openapi 就绪则走 REST，否则原样交给 MCP。

翻译表（形状差异全部显式列出，不藏进黑箱——与 ``channel.py`` 同一纪律）：

  tool（MCP）                    → REST 方法                  形状差异
  sim_trade_account_list         → simtrade.account_list      无
  sim_trade_position_list        → simtrade.position_list     market 为 int market_id（两边一致）
  sim_trade_cash_info            → simtrade.cash_info         无
  sim_trade_input_order          → simtrade.input_order       market 可为链名→需转 market_id；
                                                              qty/price MCP 收数值、REST 收字符串
  sim_trade_cancel_order         → simtrade.cancel_order      market 实测必须放 body（官方页面
                                                              未列、示例无请求体）；参数原样翻译
  sim_trade_order_list           → simtrade.order_list        当日订单；market 实测必填
                                                              （缺参报 missing required parameter）
  sim_trade_history_order_list   → simtrade.history_order_list MCP 传 start/end 日期串；
                                                              REST 要 market + 微秒 int
  sim_trade_max_buy_sell         → simtrade.max_buy_sell      REST 实测必填 market → 由账户缓存解析

公开名由 ``channel.py`` 末尾再导出（``channel.sim_call`` / ``channel.to_micros`` 等），
既有调用方与测试零改动。
"""
from datetime import datetime, timedelta, timezone

#: 模拟交易 MCP 工具名 → ``"组.方法"``（REST 侧调用路径）。
SIM_TOOL_ROUTES = {
    "sim_trade_account_list": "simtrade.account_list",
    "sim_trade_position_list": "simtrade.position_list",
    "sim_trade_cash_info": "simtrade.cash_info",
    "sim_trade_input_order": "simtrade.input_order",
    "sim_trade_cancel_order": "simtrade.cancel_order",
    "sim_trade_order_list": "simtrade.order_list",
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
    # 函数内导入 ``channel``：其一避免模块级循环导入（channel 末尾再导出本模块的公开名），
    # 其二让本模块可以独立测试（导入期不触碰配置文件与凭据）。
    from .channel import (CHANNEL_OPENAPI, _mcp_call, call_openapi,  # noqa: PLC0415
                          channel_of, openapi_client, openapi_ready)
    used_channel = channel_of(home)
    rest_ready = client is not None or openapi_ready(credential_path)
    use_rest = used_channel == CHANNEL_OPENAPI and rest_ready
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

    def _rest(tool, params, timeout=None):
        from .market_ids import sim_market_id  # noqa: PLC0415
        p = dict(params or {})
        acc_id = p.get("acc_id")
        if tool == "sim_trade_account_list":
            return call_openapi(rest_client, "simtrade.account_list", {}, timeout=timeout)
        if tool == "sim_trade_cash_info":
            return call_openapi(rest_client, "simtrade.cash_info", {"acc_id": acc_id},
                                timeout=timeout)
        if tool == "sim_trade_position_list":
            return call_openapi(rest_client, "simtrade.position_list",
                                {"acc_id": acc_id,
                                 "market": sim_market_id(p.get("market"))},
                                timeout=timeout)
        if tool == "sim_trade_input_order":
            market = sim_market_id(p.get("market"))
            if market is None:
                # 不猜市场：链名未知/缺参时本地拒绝（零网络往返），比后端报缺参更早更清楚
                raise ValueError(f"input_order 的 market 无法归一为 market_id：{p.get('market')!r}")
            return call_openapi(rest_client, "simtrade.input_order", {
                "acc_id": acc_id, "market": market, "symbol": p.get("symbol"),
                "order_type": p.get("order_type"), "order_side": p.get("order_side"),
                "qty": p.get("qty"), "price": p.get("price")}, timeout=timeout)
        if tool == "sim_trade_cancel_order":
            # market 实测必须在 body 携带（官方页面未列）——调用方本就传，原样翻译
            return call_openapi(rest_client, "simtrade.cancel_order",
                                {"acc_id": acc_id, "order_id": p.get("order_id"),
                                 "market": _market_of(acc_id, p.get("market"))},
                                timeout=timeout)
        if tool == "sim_trade_order_list":
            # 当日订单：market 实测必填（缺参后端报 missing required parameter）——
            # 显式解析（链名 → market_id，或按 acc_id 查账户缓存），不猜。
            return call_openapi(rest_client, "simtrade.order_list", {
                "acc_id": acc_id,
                "market": _market_of(acc_id, p.get("market"))}, timeout=timeout)
        if tool == "sim_trade_history_order_list":
            return call_openapi(rest_client, "simtrade.history_order_list", {
                "acc_id": acc_id,
                "market": _market_of(acc_id, p.get("market")),
                "time_begin": to_micros(p.get("start")),
                "time_end": to_micros_end(p.get("end")),
                "page_size": p.get("page_size"), "next_key": p.get("next_key")},
                timeout=timeout)
        if tool == "sim_trade_max_buy_sell":
            return call_openapi(rest_client, "simtrade.max_buy_sell", {
                "acc_id": acc_id, "symbol": p.get("symbol"),
                "order_type": p.get("order_type"),
                "market": _market_of(acc_id, p.get("market")),
                "price": p.get("price"), "order_id": p.get("order_id")},
                timeout=timeout)
        raise ValueError(f"未接入 REST 的模拟交易工具：{tool!r}")  # pragma: no cover

    def call(tool, params, timeout=None):
        """与 ``futu_mcp.call_tool(name, arguments, timeout=30)`` 同签名。

        ``timeout`` 在两条通道下都兑现：REST 经 ``call_openapi`` 的 scope（WP13 审查
        M2），MCP 原样传给 ``call_tool``。
        """
        if use_rest and tool in SIM_TOOL_ROUTES:
            return _rest(tool, params, timeout)
        mcp_fn = _mcp_or_raise()
        if timeout is None:
            return mcp_fn(tool, params)
        return mcp_fn(tool, params, timeout=timeout)

    return call
