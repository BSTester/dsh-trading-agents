"""演练内核：kill 开关生命周期（WP5 演练的纯函数部分）。

kill 文件是风控规则 1 的物理载体（risk._kill_exists）：文件存在即拒绝一切订单。
run_kill_drill 是「建 kill → 断言拒单 → 清除」的演练闭环，scripts/drills.sh
只做薄封装并打印 JSON；真正的 daemon/心跳演练在 WP4 合并后按 RUNBOOK 执行。
"""
from pathlib import Path

from . import risk

# 演练用最小订单与账户上下文：数字口径保证除 kill 外其余规则全部放行，
# 拒单只可能来自规则 1（与 tests/test_core_wp5_drills.py 同一口径）。
_ORDER = {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 1.0, "mode": "SIM",
          "plan_id": "P", "plan_hash": "h", "stop_dist": 0.1}
_CTX = {"mode": "SIM", "equity": 1e6, "positions_value": {},
        "positions_count": 0, "day_pnl_pct": 0.0, "is_trading_day": True,
        "config": {"risk_per_trade": 0.01, "max_positions": 5,
                   "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03},
        "plan_hash": "h", "plan_status": "frozen"}


def kill_on(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("")


def kill_off(path):
    Path(path).unlink(missing_ok=True)


def run_kill_drill(kill_path):
    """演练闭环：kill_on → 断言风控拒单（规则 1）→ kill_off。全离线、无副作用残留。"""
    kill_on(kill_path)
    verdict = risk.pre_trade_checks(_ORDER, dict(_CTX, kill_path=str(kill_path)))
    kill_off(kill_path)
    result = {"drill": "kill_switch", "kill_path": str(kill_path),
              "rejected": not verdict.allowed, "rule": verdict.rule,
              "reason": verdict.reason, "cleared": not Path(kill_path).exists()}
    result["ok"] = result["rejected"] and verdict.rule == 1 and result["cleared"]
    return result
