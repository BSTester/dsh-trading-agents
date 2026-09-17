"""券商适配层（规格 §6.2）：本模块是唯一接触券商的代码。

铁律（P4 实测教训）：**传输失败** → unknown → 只查询不重放（业务拒绝才落 rejected，
分类见 ``is_transport_failure``）；isError=false 仍需查业务码；
sim_trade_modify_order 间歇性 -5 → 改单一律撤单 + 重下（TOOL-LIMITS）。
"""
# 市场口径常量**直接引用规范模块**（WP13 审查 M1：原先这里是镜像副本，靠锁定测试
# 守漂移——那是「用测试维护重复」）。``market_ids`` 是零依赖纯常量模块，且
# ``trading_datasource/__init__.py`` 刻意不做 eager 导入，因此模块级导入不会拉起
# futu_mcp 会话（与 ``quality.py`` 顶层导入 ``trading_datasource.market`` 同一惯例）。
from trading_datasource.market_ids import (  # noqa: E402
    OPENAPI_ENABLE_MARKET, SIM_MARKET_IDS, sim_market_id)

TOOLS = {"place": "sim_trade_input_order", "cancel": "sim_trade_cancel_order",
         "positions": "sim_trade_position_list", "accounts": "sim_trade_account_list",
         "orders": "sim_trade_order_list", "history": "sim_trade_history_order_list",
         "cash": "sim_trade_cash_info", "max_buy_sell": "sim_trade_max_buy_sell"}
PLACE_REQUIRED = ("acc_id", "market", "symbol", "order_type", "order_side", "qty")

#: 市场链 → 模拟账户 market_id。名字保留给既有调用方；对象即规范常量（同对象别名）。
MARKET_IDS = SIM_MARKET_IDS


def sim_call(home=None, **kwargs):
    """模拟交易通道 callable（与 ``futu_mcp.call_tool`` 同签名）——sim 链路的唯一入口。

    WP13 任务 2：``futu_channel: openapi`` 且凭据就绪 → REST（``OpenApiSimTrade``）；
    否则原样交给托管 MCP。翻译表与解析逻辑只有一份，在
    ``trading_datasource.channel.sim_call``；本函数是 **core 侧的稳定入口**，让
    daemon 执行链/平台闸门不必知道 datasource 的分派细节（也避免它们各自 import）。
    """
    from trading_datasource.channel import sim_call as _sim_call  # noqa: PLC0415
    return _sim_call(home, **kwargs)

# live 账户查询工具名（MCP 通道实名，与 platform/server/trading.py 的实测调用一致）。
LIVE_TOOLS = {"accounts": "account_authorized_trd_accs", "positions": "account_positions",
              "funds": "account_funds"}
# OPENAPI_ENABLE_MARKET（市场前缀 → OpenAPI 授权账户 enable_market）由上面的 import 引入：
# 常量本体在 ``trading_datasource.market_ids``（WP13 审查 M1：原先 core 与
# platform/server/trading.py 各存一份镜像，靠锁定测试守漂移）。**与 MARKET_IDS 数字口径
# 不同**（那是券商模拟账户的 market_id：HK 1/A 股 3/US 100），两套数字各自来自各自通道，
# 不互相换算。
#: 市场链 → 标的/持仓前缀集合（SH 链含 SZ/BJ），与 planner.CALENDAR_MARKET 同源口径。
CHAIN_PREFIXES = {"SH": ("SH", "SZ", "BJ"), "HK": ("HK",), "US": ("US",)}
#: 官方 get-funds.md 的权益字段：``total_assets``=总净资产（同页另有 securities_assets/
#: cash/market_val 等分类口径，**不作权益用**）。live 权益只认这一个字段。
LIVE_EQUITY_FIELD = "total_assets"


def is_transport_failure(error):
    """传输失败（请求**可能已到达券商**）→ 真；业务拒绝 → 假。

    为什么需要这个判定（WP13 审查 K1）：真实传输**不抛** ``TimeoutError``——
    OpenAPI REST 把 URLError/连接重置/超时统一包装为 ``TransportError``
    （``futu_openapi/errors.py``），非信封/5xx/429 为 ``UnexpectedResponse``（同样
    「服务端没给出业务结论」，``errors.py`` 明示请求可能已到达券商），托管 MCP 重试后抛
    ``FutuUnavailable``。三者都不是 ``TimeoutError`` 子类，故按异常类型二分支的旧写法会把
    **可能已到券商的单落成 ``rejected``（OMS 终态、无出边）**，从此不再被查询真值——
    与仓库铁律「先查询，不重放」冲突。

    分类口径：传输类异常 → ``unknown``（等对账/查询收敛）；业务信封错误
    （``OpenApiError`` 且 errcode 明确、券商已给出拒绝结论）→ 保持 ``rejected``。

    惰性导入（沿用本模块既有惯例：避免导入期拉起网络栈/密码学依赖）；datasource 不可用时
    退化为「仅超时判定」——宁可少判 unknown，也不能因导入失败连 TimeoutError 都误判。
    """
    kinds = [TimeoutError]
    try:
        from trading_datasource.futu_openapi.errors import (  # noqa: PLC0415
            TransportError, UnexpectedResponse)
        kinds += [TransportError, UnexpectedResponse]
    except Exception:  # noqa: BLE001 —— datasource 不可用则退化
        pass
    try:
        from trading_datasource.futu_mcp import FutuUnavailable  # noqa: PLC0415
        kinds.append(FutuUnavailable)
    except Exception:  # noqa: BLE001
        pass
    return isinstance(error, tuple(kinds))


def place(call, acc_id, market, symbol, side, qty, price, order_type=1, timeout=30):
    """下单。铁律：**传输失败** → {"status": "unknown"}（先查询，绝不重发）；业务拒绝
    → {"status": "rejected"}。分类由 ``is_transport_failure`` 统一承担（不要在此按异常
    类型二分支——真实传输异常与超时不同型，见该函数说明）。
    改单需求一律「撤单 + 重新下单」：sim_trade_modify_order 间歇性 -5（TOOL-LIMITS）。"""
    side_code = 1 if side == "BUY" else 2
    args = {"acc_id": acc_id, "market": market, "symbol": symbol.lstrip("SH.ZBJ."),
            "order_type": order_type, "order_side": side_code, "qty": int(qty)}
    if price:
        args["price"] = float(price)
    try:
        data = call(TOOLS["place"], args, timeout=timeout) or {}
        return {"status": "submitted", "broker_order_id": str(data.get("order_id") or ""),
                "args": args}
    except Exception as error:  # noqa: BLE001 —— 传输失败与业务拒绝在此分流
        if is_transport_failure(error):
            return {"status": "unknown", "broker_order_id": None,
                    "err": str(error)[:120], "args": args}
        return {"status": "rejected", "broker_order_id": None, "err": str(error)[:160],
                "args": args}


def cancel(call, acc_id, market, order_id, timeout=30):
    return call(TOOLS["cancel"], {"acc_id": acc_id, "order_id": str(order_id),
                                  "market": market}, timeout=timeout)


def positions(call, acc_id, market_id, timeout=30):
    """模拟持仓行（键名二义，两键取到即用）。

    **实测（2026-09-16）**：REST ``/sim-trade/{acc_id}/positions`` 返回 ``{"positions": [...]}``，
    与 MCP 通道实测键一致；``position_list`` 是 core 早期口径。原实现只读
    ``position_list``，在真实响应下会静默返回空列表（平台组合页 sim 持仓因此恒为空）——
    WP13 任务 2 的端到端测试暴露该缺陷，此处按 ``_sim_positions_and_equity`` 的既有
    双键口径修正（不新增第三份判定）。
    """
    data = call(TOOLS["positions"], {"acc_id": acc_id, "market": market_id},
                timeout=timeout) or {}
    return data.get("positions") or data.get("position_list") or []


def accounts(call, timeout=30):
    return call(TOOLS["accounts"], {}, timeout=timeout) or {}


#: 计划执行链历史上的占位账户号（``execute.run`` 的 ``ctx`` 缺省值）。REST 通道会把它
#: 拼进 URL（``/api/v1.0/sim-trade/SIM/orders``）→ 券商 ``-3 invalid parameter``；
#: 见 ``sim_account_for_market``。
PLACEHOLDER_ACCOUNT = "SIM"


def sim_account_for_market(call, market, timeout=30):
    """市场（链名或 market_id）→ 该市场的模拟账户 id；解析不到返回 ``None``。

    存在理由（2026-09-17 实机验证「模拟盘全自动闭环」时暴露）：``execute.run`` 用
    ``ctx.get("acc_id", "SIM")`` 的**占位符**下单——MCP 通道下上游忽略它（所以历史上
    没暴露），切到 ``futu_channel: openapi`` 后 REST 把 acc_id 拼进 URL，券商如实回
    ``errcode=-3 invalid parameter``：计划/守卫/指令/风控全部正常，**订单却从未到达
    模拟账户**。市场→market_id 归一交给 ``market_ids.sim_market_id``（唯一实现，含
    SH/SZ/BJ 同属 A 股 3）；账户事实与 ``_sim_positions_and_equity`` 同一张
    ``sim_trade_account_list``，不新增第二份市场判定。**不猜**：解析不到就返回 None，
    由调用方决定回落（不拿别的市场的账户去下单）。
    """
    market_id = sim_market_id(market)
    if market_id is None:
        return None
    rows = (call(TOOLS["accounts"], {}, timeout=timeout) or {}).get("accounts") or []
    for account in rows:
        if account.get("market_id") == market_id and account.get("account_id"):
            return str(account["account_id"])
    return None


def normalize_symbol(value):
    """券商持仓/订单行的标的 → 仓库统一写法（SH.600519）。

    模拟盘行可能给裸代码（600519）或已带前缀；裸代码的 SH/SZ/BJ 判定交给
    ``trading_datasource.market.to_futu_symbol``（唯一实现，不在这里重写规则）。
    对账（``reconcile``）复用同一实现——标的口径只有一份。
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text and text.split(".", 1)[0] in MARKET_IDS:
        return text
    from trading_datasource.market import to_futu_symbol  # 共享实现，惰性导入
    return to_futu_symbol(text)


def _enable_codes(account):
    """账户 ``enable_market`` 归一成 int 集合（通道可能给 int 或数字字符串）。"""
    codes = set()
    for value in account.get("enable_market") or []:
        try:
            codes.add(int(value))
        except (TypeError, ValueError):
            continue
    return codes


class EquityUnavailable(RuntimeError):
    """权益不可得（官方字段缺失或非正）。

    与「通道故障」区分开：前者是数据口径问题（该账户资金响应不含 total_assets），
    后者是查询失败。调用方的告警文案据此分流，避免把数据问题误报成通道断线。
    """


def positions_and_equity(call, mode, market, timeout=30):
    """按市场取 (持仓 {symbol: {"qty": n}}, 权益)。**任一不可得即抛错**。

    调用方（planner.plan_auto）拿到异常就跳过当日计划——不用本地台账、不编造权益：
    权益缺失若被当成 0，目标数量会全变 0，等于凭空生成清仓单。

    sim：``sim_trade_account_list`` → 该 market_id 的账户 →
    ``sim_trade_position_list(acc_id, market)``（缺 market 会报 ret=-5）+
    ``sim_trade_cash_info``（total_asset，缺失回退 balance）。

    live：``account_authorized_trd_accs``（enable_market 命中）→ ``account_positions``
    （按 code 前缀过滤到目标市场）+ ``account_funds`` 的 ``total_assets``（官方
    get-funds.md 口径）。**只读取，不产生任何写操作**——live 计划的执行仍由工作台
    口令 + Web 确认卡片把守（规格 §4.2 双模式都生成、执行分模式）。
    """
    if mode == "sim":
        return _sim_positions_and_equity(call, market, timeout)
    if mode == "live":
        return _live_positions_and_equity(call, market, timeout)
    raise RuntimeError(f"未接入的账户模式：{mode}")


def _sim_positions_and_equity(call, market, timeout):
    market_id = MARKET_IDS.get(market)
    if market_id is None:
        raise RuntimeError(f"未知市场：{market}")
    accounts_ = (call(TOOLS["accounts"], {}, timeout=timeout) or {}).get("accounts") or []
    chosen = [a for a in accounts_ if a.get("market_id") == market_id and a.get("account_id")]
    if not chosen:
        raise RuntimeError(f"无 {market} 模拟账户（market_id={market_id}）")
    positions, equity, missing = {}, 0.0, False
    for account in chosen:
        acc_id = str(account["account_id"])
        data = call(TOOLS["positions"], {"acc_id": acc_id, "market": market_id},
                    timeout=timeout) or {}
        # sim 持仓列表实测键为 positions（workbench/positions.py 与 platform 交易测试
        # 一致）；position_list 是 core 早期口径，保留兼容读取，两者取到即用。
        for row in (data.get("positions") or data.get("position_list") or []):
            symbol = normalize_symbol(row.get("symbol") or row.get("code"))
            qty = row.get("qty")
            if not symbol or qty is None:
                continue
            positions[symbol] = {"qty": int(qty)}
        cash = call(TOOLS["cash"], {"acc_id": acc_id}, timeout=timeout) or {}
        total = cash.get("total_asset", cash.get("balance"))
        if total is None:
            missing = True
        else:
            equity += float(total)
    return positions, (None if missing else equity)


def _live_positions_and_equity(call, market, timeout):
    """live 分支（官方口径 2026-09-16 实抓 get-accounts.md / get-funds.md）：

    * ``account_authorized_trd_accs`` → ``{accounts:[{account_id, enable_market:[int]}]}``
      （enable_market：1=HK 2=US 4=ChinaStock）；无命中账户即抛错，**不退化**到别的市场；
    * ``account_positions {acc_id}`` → 该账户**全部**持仓（接口不接受 market 参数），
      行字段 ``code``/``qty``（实测见 plugins/workbench/python/positions.py）；
      因此按 ``code`` 前缀过滤到目标市场链（跨市场持仓不进本市场计划）；
    * ``account_funds {acc_id}`` → ``total_assets``（总净资产）。任一命中账户取不到该
      字段、或汇总权益非正 → 抛错（**绝不编造权益**：当成 0 会凭空生成清仓单）。

    **权益分母口径（如实披露，不是保守保证）**：官方资金接口只给账户整体的
    ``total_assets``，没有「按市场切分」的权益。多市场同时生成计划时（SH/HK/US 各一份），
    每份计划的分母都是同一笔账户整体权益——若各市场权重之和都接近 1，**跨市场总敞口
    可能超过账户权益**。本版据此实现并如实登记该近似：执行前仍有风控 8 规则逐单拦截与
    人工确认把守，但不得据此认为跨市场敞口已被自动约束；需要精确口径时应引入按市场的
    权益拆分或组合级联合约束（后续工作，不在本任务范围）。
    """
    want = OPENAPI_ENABLE_MARKET.get(market)
    if want is None:
        raise RuntimeError(f"未知市场：{market}")
    accounts = (call(LIVE_TOOLS["accounts"], {}, timeout=timeout) or {}).get("accounts") or []
    chosen = [a for a in accounts
              if isinstance(a, dict) and a.get("account_id") and want in _enable_codes(a)]
    if not chosen:
        raise RuntimeError(f"无 {market} 授权交易账户（enable_market 未含 {want}）")
    prefixes = tuple(CHAIN_PREFIXES.get(market) or (market,))
    positions, equity = {}, 0.0
    for account in chosen:
        acc_id = str(account["account_id"])
        rows = call(LIVE_TOOLS["positions"], {"acc_id": acc_id}, timeout=timeout)
        rows = rows if isinstance(rows, list) else ((rows or {}).get("positions") or [])
        for row in rows:
            symbol = normalize_symbol(row.get("code") or row.get("symbol"))
            qty = row.get("qty")
            if not symbol or qty is None:
                continue
            if symbol.split(".", 1)[0] not in prefixes:
                continue  # 跨市场持仓不进本市场计划（目标权重本就已按市场切片）
            positions[symbol] = {"qty": int(qty)}
        funds = call(LIVE_TOOLS["funds"], {"acc_id": acc_id}, timeout=timeout) or {}
        total = funds.get(LIVE_EQUITY_FIELD)
        if total is None:
            raise EquityUnavailable(
                f"账户资金缺 {LIVE_EQUITY_FIELD}（官方 get-funds 字段）：acc_id={acc_id}")
        equity += float(total)
    if equity <= 0:
        raise EquityUnavailable(f"账户权益非正：{equity}（拒绝用非正权益算订单数量）")
    return positions, equity
