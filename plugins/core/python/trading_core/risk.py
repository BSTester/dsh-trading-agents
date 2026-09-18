"""风控硬拦截（规格 §七）：pre_trade_checks 是提交前唯一强制入口。

本文件只做判定；执行侧（oms/execute_plan）必须先调用本函数并落 risk_checks，
再决定是否提交——拒绝即跳过该单并记录，无覆盖按钮。

规则 8 的 plan_status 取值说明：规格 §6.1 生命周期 draft→frozen→approved→
executing 中，frozen 之后内容与 content_hash 均不可变，approved/executing 是
同一冻结计划的合法后续态，逐单预检必须放行，否则执行编排自锁；expired/
cancelled/draft 一律拒绝。
"""
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Verdict:
    allowed: bool
    rule: int = 0
    reason: str = ""


def _kill_exists(kill_path):
    return Path(kill_path).exists()


def pre_trade_checks(order, ctx):
    cfg = ctx["config"]
    # 规则 1 kill switch
    if _kill_exists(ctx["kill_path"]):
        return Verdict(False, 1, "kill switch 生效：拒绝一切订单")
    # 规则 2 模式一致
    if order["mode"] != ctx["mode"]:
        return Verdict(False, 2, f"订单模式 {order['mode']} != 账户模式 {ctx['mode']}")
    # 规则 3 交易日（**日粒度**：只查 ctx["is_trading_day"]）
    # 文案只说它真正检查的事：本规则拿不到时钟/日历，**不判**连续竞价或半日市收盘；
    # 钟点层的守卫在平台侧——人工写入走 server/trading.py 的时段闸门（sessions），
    # 自动执行走 autopilot 守卫 9 的执行窗口。文案若声称「非连续竞价时段」，就是
    # 让调用方以为这里挡得住盘中/收盘后的单，而实际它只按日拦休市。
    if not ctx["is_trading_day"]:
        return Verdict(False, 3, "非交易日：不提交订单")
    # 规则 4 单笔风险
    risk_amt = order["qty"] * (order.get("stop_dist") or order["price"])
    if risk_amt > ctx["equity"] * cfg["risk_per_trade"]:
        return Verdict(False, 4, f"单笔风险 {risk_amt:.0f} > 权益×{cfg['risk_per_trade']}")
    # 规则 5 单票市值上限（成交后口径）
    after = ctx["positions_value"].get(order["symbol"], 0.0) + order["qty"] * order["price"]
    if after > ctx["equity"] * cfg["max_position_pct"]:
        return Verdict(False, 5,
                       f"成交后单票市值 {after / ctx['equity']:.1%} > {cfg['max_position_pct']:.0%}")
    # 规则 6 最大持仓数（成交后口径）
    if ctx["positions_count"] + (1 if order["symbol"] not in ctx["positions_value"] else 0) > cfg["max_positions"]:
        return Verdict(False, 6, f"成交后持仓数 > {cfg['max_positions']}")
    # 规则 7 日内熔断
    if ctx["day_pnl_pct"] <= -cfg["daily_loss_limit_pct"]:
        return Verdict(False, 7, f"日内亏损 {ctx['day_pnl_pct']:.2%} 触发熔断 {cfg['daily_loss_limit_pct']:.0%}")
    # 规则 8 计划一致性（frozen 及其合法后续态，见模块 docstring）
    if order.get("plan_hash") != ctx["plan_hash"] or ctx["plan_status"] not in ("frozen", "approved", "executing"):
        return Verdict(False, 8, "订单不属于 frozen 且 hash 匹配的计划")
    return Verdict(True)
