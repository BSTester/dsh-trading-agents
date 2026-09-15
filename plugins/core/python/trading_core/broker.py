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
