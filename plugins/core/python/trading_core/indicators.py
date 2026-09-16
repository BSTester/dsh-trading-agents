"""技术指标：ATR(14) 与止损距离的**唯一实现**（计划定量与执行风控共用）。

为什么独立成模块：WP9 结构性缺口修复要求「计划按风险预算定量」与「执行侧规则 4 按
同一止损距离校验」两侧口径**必须同源**——两处各写一份 ATR 迟早漂移（一处改周期、
一处改倍数），漂移的后果是计划数量与风控判定互相矛盾：要么计划必被拦（徒劳计划），
要么实际单笔风险超出预算（硬规则形同虚设）。因此本模块是唯一实现，planner 与 daemon
都只调用它。

口径（对照 engine 快路径「1% 风险 ÷ 2×ATR」的同一哲学，规格 §4.2 第 5 点）::

    TR_t   = max(high - low, |high - prev_close|, |low - prev_close|)
    ATR    = 最近 period 根 TR 的算术均值（Wilder 平滑的更简口径，日频足够）
    止损距离 = stop_atr_mult × ATR

**宁缺毋假**：bar 不足 period+1 根（算不出 period 个 TR）或 ATR ≤ 0 时返回 ``None``
——调用方**不得**把 None 当 0 处理，也不得退回「无止损」口径静默放行：
  * planner：该标的跳过并在结果里列出（不生成必被规则 4 拦的徒劳订单）；
  * daemon（执行侧 ctx）：保持 ``stop_dist=None``，规则 4 按全额名义判定（保守拒绝）。

纯标准库实现：不引入 numpy/pandas（core 的依赖面刻意保持最小）。
"""
from . import store

#: ATR 周期（规格 §4.2 第 5 点固定 14）
ATR_PERIOD = 14


def true_ranges(bars):
    """逐根 True Range（``bars`` 升序；首根无前收，不产 TR）。"""
    out = []
    for prev, cur in zip(bars, bars[1:]):
        high, low, prev_close = cur["h"], cur["l"], prev["c"]
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr(bars, period=ATR_PERIOD):
    """最近 ``period`` 根 True Range 的均值；**不足 period+1 根 bar 或均值 ≤ 0 → None**。

    需要 period+1 根 bar 才能算出 period 个 TR（首个 TR 依赖前一根收盘）。
    均值为 0（例如整段横盘且 h=l=c）时按「不可用」返回 None：止损距离为 0 会让
    规则 4 的风险额恒为 0，等于**静默废除**该条硬规则。
    """
    if period <= 0:
        raise ValueError(f"ATR 周期必须为正：{period!r}")
    if len(bars) < period + 1:
        return None
    trs = true_ranges(bars[-(period + 1):])
    if not trs:
        return None
    value = sum(trs) / len(trs)
    return value if value > 0 else None


def stop_distance(conn, symbol, as_of, stop_atr_mult, period=ATR_PERIOD):
    """该标的在 ``as_of`` 时点的止损距离（PIT：只读 as_of 及之前的 bars）。

    返回 ``stop_atr_mult × ATR(period)``；ATR 不可用（bar 不足/恒为 0）或倍数非正时
    返回 ``None``——调用方按各自契约处理（见模块 docstring）。
    """
    if stop_atr_mult is None or stop_atr_mult <= 0:
        return None
    bars = store.read_bars(conn, symbol, "1d", as_of=as_of, limit=period + 1)
    value = atr(bars, period)
    if value is None:
        return None
    return stop_atr_mult * value
