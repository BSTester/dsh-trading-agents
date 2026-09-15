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
