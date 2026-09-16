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


def _normalize_symbol(value):
    """券商持仓行的标的 → 仓库统一写法（SH.600519）。

    模拟盘行可能给裸代码（600519）或已带前缀；裸代码的 SH/SZ/BJ 判定交给
    ``trading_datasource.market.to_futu_symbol``（唯一实现，不在这里重写规则）。
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if "." in text and text.split(".", 1)[0] in MARKET_IDS:
        return text
    from trading_datasource.market import to_futu_symbol  # 共享实现，惰性导入
    return to_futu_symbol(text)


def positions_and_equity(call, mode, market, timeout=30):
    """按市场取 (持仓 {symbol: {"qty": n}}, 权益)。**任一不可得即抛错**。

    调用方（planner.plan_auto）拿到异常就跳过当日计划——不用本地台账、不编造权益：
    权益缺失若被当成 0，目标数量会全变 0，等于凭空生成清仓单。

    sim：``sim_trade_account_list`` → 该 market_id 的账户 →
    ``sim_trade_position_list(acc_id, market)``（缺 market 会报 ret=-5）+
    ``sim_trade_cash_info``（total_asset，缺失回退 balance）。

    live：**未实现**——实盘权益（账户资金）口径在本仓库尚未验证，凭猜测生成真实
    订单参数是拿真钱试错，因此这里如实抛错，由 plan_auto 在 live 模式先行跳过。
    """
    if mode != "sim":
        raise RuntimeError(f"未接入的账户模式：{mode}（live 权益口径待验证）")
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
            symbol = _normalize_symbol(row.get("symbol") or row.get("code"))
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
