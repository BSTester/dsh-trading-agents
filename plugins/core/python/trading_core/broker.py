"""券商适配层（规格 §6.2）：本模块是唯一接触券商的代码。

任务 0 只放依赖锁定常量（tools/list schema 实测口径，2026-09-14，见 WP3 计划
依赖锁定表）；下单/撤单/持仓行为在任务 4 补齐。铁律（P4 实测教训）：
提交超时 → unknown → 只查询不重放；isError=false 仍需查业务码；
sim_trade_modify_order 间歇性 -5 → 改单一律撤单 + 重下（TOOL-LIMITS）。
"""

TOOLS = {"place": "sim_trade_input_order", "cancel": "sim_trade_cancel_order",
         "positions": "sim_trade_position_list", "accounts": "sim_trade_account_list",
         "history": "sim_trade_history_order_list", "cash": "sim_trade_cash_info",
         "max_buy_sell": "sim_trade_max_buy_sell"}
PLACE_REQUIRED = ("acc_id", "market", "symbol", "order_type", "order_side", "qty")

# 市场链 → 模拟账户 market_id（数字口径来自 platform/server/store_access.MARKET_HINT
# 的实测：港股 1 / A股 3 / 美股 100）。core 不能 import server 包，这里是镜像，
# 漂移由 tests/test_wp9_plan_auto.py 的锁定用例炸掉。
MARKET_IDS = {"SH": 3, "SZ": 3, "BJ": 3, "HK": 1, "US": 100}

# live 账户查询工具名（MCP 通道实名，与 platform/server/trading.py 的实测调用一致）。
LIVE_TOOLS = {"accounts": "account_authorized_trd_accs", "positions": "account_positions",
              "funds": "account_funds"}
#: 市场前缀 → OpenAPI 授权账户 enable_market（naming-dictionary#enable-market：
#: 1=HK 2=US 4=ChinaStock）。**与上面的 MARKET_IDS 数字口径不同**（那是券商模拟账户的
#: market_id：HK 1/A 股 3/US 100），两套数字各自来自各自通道，不互相换算。core 不能
#: import server 包，这里是 platform/server/trading.py 的镜像，漂移由
#: tests/test_wp9_plan_auto.py 的锁定用例炸掉。
OPENAPI_ENABLE_MARKET = {"HK": 1, "US": 2, "SH": 4, "SZ": 4, "BJ": 4}
#: 市场链 → 标的/持仓前缀集合（SH 链含 SZ/BJ），与 planner.CALENDAR_MARKET 同源口径。
CHAIN_PREFIXES = {"SH": ("SH", "SZ", "BJ"), "HK": ("HK",), "US": ("US",)}
#: 官方 get-funds.md 的权益字段：``total_assets``=总净资产（同页另有 securities_assets/
#: cash/market_val 等分类口径，**不作权益用**）。live 权益只认这一个字段。
LIVE_EQUITY_FIELD = "total_assets"


def place(call, acc_id, market, symbol, side, qty, price, order_type=1, timeout=30):
    """下单。铁律：超时/异常 → {"status": "unknown"}，绝不重发（P4 教训代码化）。
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
    except TimeoutError as error:
        return {"status": "unknown", "broker_order_id": None, "err": str(error)[:120],
                "args": args}
    except Exception as error:  # noqa: BLE001 —— 拒单/业务失败如实上抛信息
        return {"status": "rejected", "broker_order_id": None, "err": str(error)[:160],
                "args": args}


def cancel(call, acc_id, market, order_id, timeout=30):
    return call(TOOLS["cancel"], {"acc_id": acc_id, "order_id": str(order_id),
                                  "market": market}, timeout=timeout)


def positions(call, acc_id, market_id, timeout=30):
    data = call(TOOLS["positions"], {"acc_id": acc_id, "market": market_id},
                timeout=timeout) or {}
    return data.get("position_list") or []


def accounts(call, timeout=30):
    return call(TOOLS["accounts"], {}, timeout=timeout) or {}


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
