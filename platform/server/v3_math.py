"""V3 分析类接口的**纯计算内核**（无 IO、无 FastAPI 依赖，便于确定性单测）。

移植源（逐行为准；有意差异在各函数 docstring 里逐条标注）：
  * V3 原型（已退役）的风险量实现 —— 交易日对齐、历史模拟法 VaR/CVaR、
    Beta/Alpha/IR、最大回撤、Kupiec POF；
  * V3 原型（已退役）的回测实现 —— 单标的动量 long/flat 回测与参数扫描；
  * V3 原型（已退役）的因子口径 —— 因子 z 矩阵与 IC 统计口径。

设计约束（与 ``server/app.py`` 的 V3 诚实性约定一致）：
  * **只做算术**：不取数、不读盘、不碰 FastAPI，因此可以用确定性构造数据断言数值；
  * **取不到就是取不到**：所有转换失败返回 ``None``（由调用方转成 ``null`` 或错误信封），
    绝不回退到 0 或任何估计值；
  * 对外数值统一走 :func:`round_half_up`（近似 JS ``Number(x.toFixed(n))``）：Python 内置
    ``round`` 是银行家舍入（``round(0.5) == 0``），而参考实现是 ``toFixed``，
    同一份输入必须在两个实现里得到同一个数字。
"""

import decimal
import math

# FR-DATA-003：PIT 边界的**唯一实现**在 ``server.data.cache``。本模块只消费
# ``pit_prefix``（``t`` 日可见前缀），不再自带一份「≤ t-1」的切片逻辑。
from server.data import cache as pit_cache

__all__ = [
    "TRADING_DAYS",
    "adf_test",
    "align_series",
    "backtest_momentum",
    "composite_z",
    "cross_sectional_z",
    "factor_matrix",
    "half_life",
    "ic_stats",
    "kupiec_pof",
    "max_drawdown",
    "mean",
    "ols_slope",
    "param_sweep",
    "param_sweep_fast",
    "pearson",
    "pick_frozen_plan_weights",
    "portfolio_risk",
    "price_volume_factor_values",
    "returns_of",
    "round_half_up",
    "stdev",
    "to_float",
    "watchlist_equal_weights",
]

# 年化交易日数（risk-analytics.mjs / backtest.mjs 同值）。
TRADING_DAYS = 252

# 综合分只用「动量/趋势」三类 z（估值/波动类只展示，不进综合分——口径以本文件为准）。
COMPOSITE_KEYS = ("mom_20", "mom_60", "trend")

# 组合风险量的最小样本：对齐后交易日 < 40 → risk/insufficient（参考实现同阈值）。
MIN_OBSERVATIONS = 40

# 权重来源文案（与参考实现的文案一致，页面直接展示）。
SOURCE_EXPLICIT = "请求显式权重"
SOURCE_NO_PORTFOLIO = "无可用组合定义（既无多标的计划，自选池也为空）"


# ---------------------------------------------------------------------------
# 数值工具
# ---------------------------------------------------------------------------
def to_float(value):
    """把工具面/配置里的值尽力转成有限 ``float``；转不了返回 ``None``。

    ``bool`` 显式排除（``isinstance(True, int)`` 为真，但 ``true`` 不是一个数值）；
    NaN/Inf 一律视为「取不到」——它们不是可用的实测值，混进均值会污染整条链。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def round_half_up(value, digits):
    """``Number(x.toFixed(digits))`` 等价：对**二进制精确值**做十进制四舍五入。

    关键是 ``Decimal(number)``（float 的精确十进制展开）而不是 ``repr(number)``（最短
    往返表示）：``toFixed`` 看的是内存里那个数，``(1.005).toFixed(2) === "1.00"``、
    ``(0.125).toFixed(2) === "0.13"``——本函数给出同样的结果。

    非有限值/非数值 → ``None``（调用方按「取不到」处理，不伪造 0）。已知边界：JS 对
    ``|x| >= 1e21`` 的 ``toFixed`` 直接退化成 ``ToString``（科学计数法），本模块不模仿
    该分支——这里处理的是价格、收益率、z 值，量级差着十几个数量级。
    """
    number = to_float(value)
    if number is None:
        return None
    digits = int(digits)
    with decimal.localcontext() as context:
        context.prec = 400  # 量级极端的输入也不让 quantize 溢出精度
        quantum = decimal.Decimal(1).scaleb(-digits)
        return float(decimal.Decimal(number).quantize(quantum,
                                                      rounding=decimal.ROUND_HALF_UP))


def mean(values):
    """算术平均；空列表 → 0.0（参考实现 ``xs.length === 0 ? 0 : ...``）。"""
    items = list(values)
    return 0.0 if not items else sum(items) / len(items)


def stdev(values):
    """**样本**标准差（除以 n-1）；少于 2 个 → 0.0（参考实现同口径）。"""
    items = list(values)
    count = len(items)
    if count < 2:
        return 0.0
    average = mean(items)
    return math.sqrt(sum((item - average) ** 2 for item in items) / (count - 1))


# ---------------------------------------------------------------------------
# 序列对齐与收益
# ---------------------------------------------------------------------------
def align_series(series_by_ticker):
    """交易日对齐：取所有序列**共有**的日期（升序），返回 ``(dates, closes)``。

    ``closes[ticker]`` 与 ``dates`` 一一对应。缺 bar 或收盘价非有限的标的，
    在交集层面就被排除（而不是带一个 NaN 进整条计算链）——这是**有意差异**：
    ``risk-analytics.mjs`` 的 ``Number(b.c)`` 会把坏值变成 NaN 并静默传染整段指标，
    这里宁可少几天也不产出假数字。
    """
    entries = []
    for ticker, bars in (series_by_ticker or {}).items():
        rows = [bar for bar in (bars or []) if isinstance(bar, dict)]
        if rows:
            entries.append((str(ticker), rows))
    if not entries:
        return [], {}

    maps = []
    for ticker, rows in entries:
        closes = {}
        for bar in rows:
            day = bar.get("t")
            close = to_float(bar.get("c"))
            if day is None or close is None:
                continue
            closes[str(day)] = close
        if closes:
            maps.append((ticker, closes))
    if not maps:
        return [], {}

    first = maps[0][1]
    common = sorted(day for day in first if all(day in closes for _, closes in maps))
    closes_by_ticker = {ticker: [closes[day] for day in common] for ticker, closes in maps}
    return common, closes_by_ticker


def returns_of(closes):
    """日收益率序列 ``r[t] = c[t]/c[t-1] - 1``（长度 = len(closes) - 1）。

    ``closes[i-1] == 0`` 时收益率在数学上无定义（JS 会给出 ``Infinity``）：这里抛
    ``ValueError``，由调用方转成错误信封——不把 Inf 混进均值假装算出了风险量。
    """
    out = []
    for index in range(1, len(closes)):
        previous = closes[index - 1]
        if previous == 0:
            raise ValueError(f"第 {index} 个收盘价为 0，收益率不可计算")
        out.append(closes[index] / previous - 1)
    return out


def max_drawdown(values):
    """最大回撤（负数，比例）；峰值从首值起算（参考实现 ``values[0] ?? 1``）。"""
    items = list(values)
    if not items:
        return 0.0
    peak = items[0]
    drawdown = 0.0
    for value in items:
        peak = max(peak, value)
        if peak != 0:
            drawdown = min(drawdown, value / peak - 1)
    return drawdown


# ---------------------------------------------------------------------------
# Kupiec POF（比例失败检验）
# ---------------------------------------------------------------------------
def kupiec_pof(breaches, observations, p):
    """Kupiec POF 检验：LR = -2 ln[ (1-p)^(n-x) p^x / ((1-x/n)^(n-x) (x/n)^x) ]。

    p 值为自由度 1 的卡方**生存函数** ``erfc(sqrt(LR/2))``（规格给定写法）。
    ``pass = pValue > 0.05`` 用的是**未舍入**的 p 值（与参考实现一致：先判定再舍入展示）。
    ``observations == 0`` 时三项均为 ``None``（含 ``pass``，保持键集稳定）。
    """
    x = int(breaches)
    n = int(observations)
    if n <= 0:
        return {"lr": None, "pValue": None, "breaches": x, "observations": n, "pass": None}
    if not (0.0 < float(p) < 1.0):
        return {"lr": None, "pValue": None, "breaches": x, "observations": n, "pass": None}

    pi = x / n
    if x == 0:
        log_null = n * math.log(1 - p)
    else:
        log_null = (n - x) * math.log(1 - p) + x * math.log(p)
    if x == 0:
        log_alt = n * math.log(1 - pi)
    elif x == n:
        log_alt = n * math.log(pi)
    else:
        log_alt = (n - x) * math.log(1 - pi) + x * math.log(pi)

    lr = max(0.0, -2 * (log_null - log_alt))
    p_value = math.erfc(math.sqrt(lr / 2))
    return {
        "lr": round_half_up(lr, 4),
        "pValue": round_half_up(p_value, 4),
        "breaches": x,
        "observations": n,
        "pass": p_value > 0.05,
    }


# ---------------------------------------------------------------------------
# 组合风险量
# ---------------------------------------------------------------------------
def portfolio_risk(series_by_ticker, benchmark_bars=None, weights=None,
                   confidence=0.95, nav=None, trading_days=TRADING_DAYS):
    """给定各标的日 K、权重与基准日 K，算组合风险量（历史模拟法，非参数）。

    返回体与参考实现 ``portfolioRisk()`` 同形；样本不足/无可用序列时返回
    ``{"error": "..."}``（调用方转 ``risk/insufficient`` 等错误码），**不抛异常**。

    口径与局限（原样保留参考实现的 ``method`` 文案）：历史模拟法 VaR；
    组合按给定权重（归一到 1）；未计交易成本与分红送转以外的公司行为。
    """
    all_series = series_by_ticker or {}
    weight_map = weights or {}
    tickers = [str(t) for t in weight_map if all_series.get(t)]
    if not tickers:
        return {"error": "组合无可用的成分序列"}

    total_weight = 0.0
    for ticker in tickers:
        total_weight += to_float(weight_map.get(ticker)) or 0.0
    if not total_weight > 0:
        return {"error": "组合权重之和为 0"}
    normalized = {ticker: (to_float(weight_map.get(ticker)) or 0.0) / total_weight
                  for ticker in tickers}

    dates, closes = align_series({ticker: all_series[ticker] for ticker in tickers})
    if len(dates) < MIN_OBSERVATIONS:
        return {"error": f"对齐后交易日不足（{len(dates)} 天），无法计算风险量"}

    try:
        component_returns = {ticker: returns_of(closes[ticker]) for ticker in tickers}
    except ValueError as error:
        return {"error": str(error)}

    count = len(dates) - 1
    portfolio = []
    for index in range(count):
        value = 0.0
        for ticker in tickers:
            value += normalized[ticker] * component_returns[ticker][index]
        portfolio.append(value)

    benchmark_returns = None
    benchmark_aligned = None
    if benchmark_bars:
        bench_map = {}
        for bar in benchmark_bars:
            if not isinstance(bar, dict):
                continue
            day = bar.get("t")
            close = to_float(bar.get("c"))
            if day is None or close is None:
                continue
            bench_map[str(day)] = close
        bench_dates = [day for day in dates if day in bench_map]
        if len(bench_dates) >= MIN_OBSERVATIONS:
            try:
                values = returns_of([bench_map[day] for day in bench_dates])
            except ValueError:
                values = None
            if values is not None:
                benchmark_aligned = bench_dates
                benchmark_returns = values

    level = 1 - confidence
    ordered = sorted(portfolio)
    var_index = max(0, math.floor(level * len(ordered)) - 1)
    var_daily = -ordered[var_index]
    tail = ordered[:var_index + 1]
    cvar_daily = -mean(tail) if tail else var_daily
    breaches = sum(1 for value in portfolio if value < -var_daily)
    kupiec = kupiec_pof(breaches, len(portfolio), level)

    beta = None
    alpha_annual = None
    information_ratio = None
    benchmark_ann_return = None
    if benchmark_returns is not None and len(benchmark_returns) == len(portfolio):
        mean_portfolio = mean(portfolio)
        mean_benchmark = mean(benchmark_returns)
        covariance = mean([(portfolio[i] - mean_portfolio) * (benchmark_returns[i] - mean_benchmark)
                           for i in range(len(portfolio))])
        benchmark_variance = stdev(benchmark_returns) ** 2
        beta = covariance / benchmark_variance if benchmark_variance > 0 else None
        if beta is not None:
            alpha_annual = (mean_portfolio - beta * mean_benchmark) * trading_days
        differences = [portfolio[i] - benchmark_returns[i] for i in range(len(portfolio))]
        spread = stdev(differences)
        information_ratio = (mean(differences) / spread) * math.sqrt(trading_days) if spread > 0 else None
        benchmark_ann_return = mean_benchmark * trading_days

    equity = 1.0
    equity_curve = []
    for value in portfolio:
        equity *= 1 + value
        equity_curve.append(round_half_up(equity, 6))

    return {
        "confidence": confidence,
        "observations": len(portfolio),
        "tickers": normalized,
        "window": {"from": dates[0], "to": dates[-1]},
        "benchmark": ({"from": benchmark_aligned[0], "to": benchmark_aligned[-1]}
                      if benchmark_aligned else None),
        "varDailyPct": round_half_up(var_daily * 100, 3),
        "cvarDailyPct": round_half_up(cvar_daily * 100, 3),
        "varAmount": round_half_up(nav * var_daily, 2) if nav else None,
        "cvarAmount": round_half_up(nav * cvar_daily, 2) if nav else None,
        "annVolPct": round_half_up(stdev(portfolio) * math.sqrt(trading_days) * 100, 2),
        "annReturnPct": round_half_up(mean(portfolio) * trading_days * 100, 2),
        "maxDrawdownPct": round_half_up(max_drawdown(equity_curve) * 100, 2),
        "beta": None if beta is None else round_half_up(beta, 3),
        "alphaAnnPct": None if alpha_annual is None else round_half_up(alpha_annual * 100, 2),
        "ir": None if information_ratio is None else round_half_up(information_ratio, 3),
        "benchmarkAnnReturnPct": (None if benchmark_ann_return is None
                                  else round_half_up(benchmark_ann_return * 100, 2)),
        "kupiec": kupiec,
        "equityCurve": [{"t": dates[index + 1], "v": value}
                        for index, value in enumerate(equity_curve)],
        "method": "历史模拟法 VaR（非参数）；组合按给定权重；未计交易成本与分红送转以外的公司行为",
    }


# ---------------------------------------------------------------------------
# 单标的动量 long/flat 回测（PIT）
# ---------------------------------------------------------------------------
def backtest_momentum(bars, window=20, rebalance_days=5):
    """单标的「动量 long/flat」回测（``backtest.mjs`` 的逐行移植）。

    **PIT 纪律**：``t`` 日持仓只由 ``≤ t-1`` 的收盘价决定（信号用 ``c[t-1]`` 与
    ``c[t-1-window]``），无前视。指标按**持仓日**基准：空仓日不计入胜率。

    返回 ``{"metrics": ..., "equity": [...]}``；数据不足返回 ``{"error": "..."}``。
    ``positions``/``strategyReturns`` 是**排查与单测用**的逐日明细，两个 HTTP 路由只取
    ``metrics``/``equity``（对外契约与参考实现同形），因此多这两个键不改变接口。

    有意差异 1：先按收盘价过滤出可用的 ``(t, c)`` 对再取时间戳。参考实现过滤了收盘价
    却仍用**未过滤**的下标取 ``bars[t].t``——只有在存在坏 bar 时才会错位；正常数据下
    两者逐位相同。
    有意差异 2：``window``/``rebalanceDays`` 非正时直接报错（参考实现会退化成一个
    每天重算、动量恒为 0 的空壳，指标看似有效实则无信息）。

    FR-DATA-003 迁移（2026-09-20）：信号可见窗口改由 ``server.data.cache.pit_prefix``
    给出（``lag=LAG_PREV_DAY``）——「``t`` 日只可用 ``≤ t-1``」这条边界不再由本函数自己
    维护切片，而是走**唯一入口**。数值逐位不变（同一个切片、同一个除法）。
    """
    pairs = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        close = to_float(bar.get("c"))
        if close is None:
            continue
        pairs.append((bar.get("t"), close))
    closes = [close for _, close in pairs]
    stamps = [stamp for stamp, _ in pairs]

    window = int(window)
    rebalance_days = int(rebalance_days)
    if window < 1 or rebalance_days < 1:
        return {"error": f"window/rebalanceDays 必须为正整数（window={window}, "
                         f"rebalanceDays={rebalance_days}）"}
    if len(closes) < window + 2:
        return {"error": f"bars insufficient: need >= {window + 2}, got {len(closes)}"}

    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]

    position = 0
    signal_flips = 0
    strat = []
    positions = []
    equity = []
    equity_value = 1.0
    last_signal_pos = None
    days_since_rebalance = rebalance_days  # 第一个可调仓日为第 1 天
    for t in range(1, len(closes)):
        # PIT：t 日的决策只许看 ≤ t-1（``pit_prefix`` 是这条边界的唯一实现）。
        # ``len(visible) > window`` 与原来的 ``index - window >= 0`` 等价（整数），
        # ``visible[-1] / visible[-1 - window]`` 与 ``closes[index] / closes[index-window]``
        # 逐位相同 —— 迁移不改数值，只改这条约束的**归属**。
        visible = pit_cache.pit_prefix(closes, t, lag=pit_cache.LAG_PREV_DAY)
        if days_since_rebalance >= rebalance_days and len(visible) > window:
            momentum = visible[-1] / visible[-1 - window] - 1
            new_position = 1 if momentum > 0 else 0
            if last_signal_pos is not None and new_position != last_signal_pos:
                signal_flips += 1
            last_signal_pos = new_position
            position = new_position
            days_since_rebalance = 0
        else:
            days_since_rebalance += 1
        value = position * returns[t - 1]
        strat.append(value)
        positions.append(position)
        equity_value *= 1 + value
        equity.append({"t": stamps[t] if t < len(stamps) else str(t),
                       "value": round_half_up(equity_value, 6)})

    count = len(strat)
    if count == 0:
        return {"error": "no strategy returns"}
    average = mean(strat)
    deviation = math.sqrt(sum((value - average) ** 2 for value in strat) / max(1, count - 1))
    sharpe = (average / deviation) * math.sqrt(TRADING_DAYS) if deviation > 0 else 0.0
    annual_return = average * TRADING_DAYS

    peak = 1.0
    drawdown = 0.0
    for point in equity:
        peak = max(peak, point["value"])
        drawdown = min(drawdown, point["value"] / peak - 1)

    held_days = sum(1 for item in positions if item == 1)
    win_days = sum(1 for index, item in enumerate(positions) if item == 1 and strat[index] > 0)
    win_rate = 0.0 if held_days == 0 else win_days / held_days

    return {
        "metrics": {
            "sharpe": round_half_up(sharpe, 3),
            "annReturnPct": round_half_up(annual_return * 100, 2),
            "maxDrawdownPct": round_half_up(drawdown * 100, 2),
            "signalFlips": signal_flips,
            "heldDays": held_days,
            "flatDays": count - held_days,
            "days": count,
            "winRatePct": round_half_up(win_rate * 100, 1),
        },
        "equity": equity,
        "positions": positions,
        "strategyReturns": strat,
    }


def param_sweep(bars, windows=(10, 20, 30, 60), rebalance_days=(5, 10, 20)):
    """参数扫描网格：``windows × rebalanceDays`` 逐格真实回测，``best`` 取 Sharpe 最大。

    失败格只带 ``error``（不填 0 顶替）；``best`` 在全失败时为 ``None``。
    """
    rows = []
    for window in windows:
        for rebalance in rebalance_days:
            result = backtest_momentum(bars, window=window, rebalance_days=rebalance)
            error = result.get("error")
            metrics = result.get("metrics") or {}
            row = {
                "window": window,
                "rebalanceDays": rebalance,
                "sharpe": None if error else metrics.get("sharpe"),
                "annReturnPct": None if error else metrics.get("annReturnPct"),
                "maxDrawdownPct": None if error else metrics.get("maxDrawdownPct"),
            }
            if error:
                row["error"] = error
            rows.append(row)
    valid = [row for row in rows if row["sharpe"] is not None]
    valid.sort(key=lambda row: row["sharpe"], reverse=True)
    return {"grid": rows, "best": valid[0] if valid else None}


# ---------------------------------------------------------------------------
# 参数扫描（快速路径）：numpy 向量化 + 时间预算，数值与 param_sweep 逐格一致
# ---------------------------------------------------------------------------
def param_sweep_fast(bars, windows=(10, 20, 30, 60), rebalance_days=(5, 10, 20),
                     *, deadline=None, clock=None):
    """:func:`param_sweep` 的**快速路径**（FR-STRAT-002「数千次完整回测」）。

    与参考实现的**逐格一致**由构造保证（差异只在不重算的东西上）：

      * 信号 ``t`` 日动量 = ``c[t-1]/c[t-1-window] - 1``（``pit_prefix(lag=LAG_PREV_DAY)``
        切片的标量等价——不再逐日复制可见前缀列表，只做两次下标）；
      * 调仓日 = ``t ≡ 1 (mod rebalance)``（参考实现 ``days_since`` 计数的解析解）；
      * 指标归约（mean/std/胜率）沿用与参考实现**同一份** Python 逐元素口径（求和顺序
        一致，浮点逐位相同）；``maxDrawdown`` 仍在「6 位舍入后的净值」上算（同
        ``backtest_momentum``），舍入用 ``floor(x·10⁶ + 0.5)/10⁶``——正值上与
        :func:`round_half_up` 逐位一致。

    ``deadline``（:func:`time.perf_counter` 时刻）给定时，超预算即停：``grid`` 只含**已执行**
    的格，``planned``/``executed``/``truncated`` 如实上报——绝不把没跑的格子谎报成完成。
    至少执行一格才允许截断（一格都不许欠的预算没有意义）。``clock`` 供测试注入
    （缺省 ``time.perf_counter``）。

    非有限收盘价（≤0 / NaN）会让参考实现抛 ``ZeroDivisionError``/``ValueError``：
    这类输入本函数直接退回 :func:`param_sweep`（行为完全一致），不假装算出了指标。

    返回 ``{grid, best, planned, executed, truncated}``；``grid`` 行形状与
    :func:`param_sweep` 逐键一致（多出的预算三键在顶层，不动行——前端兼容）。
    """
    import time as _time

    now = clock or _time.perf_counter
    pairs = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        close = to_float(bar.get("c"))
        if close is None:
            continue
        pairs.append((bar.get("t"), close))
    closes = [close for _, close in pairs]
    n = len(closes)
    rows = []
    truncated = False

    usable = n >= 3 and all(value > 0 for value in closes)
    windows = [int(window) for window in windows]
    rebalances = [int(value) for value in rebalance_days]
    planned = len(windows) * len(rebalances)
    if usable:
        import numpy as np

        close_array = np.asarray(closes, dtype=float)
        # 日收益率 r[t] = c[t]/c[t-1] - 1（t = 1..n-1）——与参考实现同一份除法。
        returns = close_array[1:] / close_array[:-1] - 1.0

    executed = 0
    for window in windows:
        if truncated:
            break
        if not usable:
            continue
        if n < window + 2:
            # 这一组 window 下所有 rebalance 格都是同一个「样本不足」——与参考实现同文案。
            message = f"bars insufficient: need >= {window + 2}, got {n}"
            for rebalance in rebalances:
                rows.append({"window": window, "rebalanceDays": rebalance, "sharpe": None,
                             "annReturnPct": None, "maxDrawdownPct": None, "error": message})
                executed += 1
            continue
        # 每个窗口只算一次动量符号：mom[t] = c[t-1]/c[t-1-window] - 1（t ≥ window+1）。
        t_indices = np.arange(window + 1, n)
        momentum = close_array[t_indices - 1] / close_array[t_indices - 1 - window] - 1.0
        sign_positive = momentum > 0
        for rebalance in rebalances:
            if deadline is not None and executed > 0 and now() >= deadline:
                truncated = True
                break
            # 调仓日（参考实现 ``days_since`` 计数的解析解）：
            #   * 首个可决策日 t0 = window+1（warmup 里 days 只增不减，一到可算即决策）；
            #   * 之后每 **r+1** 天一次（决策置 0 → 次日判 0>=r 为假并 +1 → 第 r+1 天判中
            #     ——参考实现的「先判后加」顺序，r=1 时实际隔天调仓，本快速路径**照实复刻**）。
            # t0 之前仓位保持初值 0：在 t=0 处放一个「False 决策」哨兵，展开时不越界。
            # 决策符号按动量数组下标 t-(window+1) 取（不是位置切片——调仓隔 r+1 天跳档）。
            real_decisions = np.arange(window + 1, n, rebalance + 1)
            decision_times = np.concatenate(([0], real_decisions))
            decision_position = np.concatenate(
                ([False], sign_positive[real_decisions - (window + 1)]))
            # 展开到逐日：position[t] = 最后一个 ≤ t 的调仓日的决策。
            day_index = np.searchsorted(decision_times, np.arange(1, n), side="right") - 1
            position = decision_position[day_index].astype(float)
            strat_list = (position * returns).tolist()
            position_list = position.astype(int).tolist()

            count = len(strat_list)
            average = mean(strat_list)
            deviation = math.sqrt(sum((value - average) ** 2 for value in strat_list)
                                  / max(1, count - 1))
            sharpe = (average / deviation) * math.sqrt(TRADING_DAYS) if deviation > 0 else 0.0
            annual_return = average * TRADING_DAYS

            # 净值与回撤：与参考实现一样在「6 位舍入后的净值」上算回撤（逐位一致）。
            equity_value = 1.0
            rounded_equity = []
            for value in strat_list:
                equity_value *= 1 + value
                rounded_equity.append(math.floor(equity_value * 1e6 + 0.5) / 1e6)
            drawdown = max_drawdown(rounded_equity)

            rows.append({
                "window": window,
                "rebalanceDays": rebalance,
                "sharpe": round_half_up(sharpe, 3),
                "annReturnPct": round_half_up(annual_return * 100, 2),
                "maxDrawdownPct": round_half_up(drawdown * 100, 2),
            })
            executed += 1
        if truncated:
            break

    if not usable and executed == 0:
        # 非法价格序列：退回参考实现拿逐格结果（行为完全一致，包括逐格错误）。
        reference = param_sweep(bars, windows, rebalances)
        return {**reference, "planned": planned,
                "executed": len(reference.get("grid") or []), "truncated": False}
    valid = [row for row in rows if row["sharpe"] is not None]
    valid.sort(key=lambda row: row["sharpe"], reverse=True)
    return {"grid": rows, "best": valid[0] if valid else None,
            "planned": planned, "executed": executed, "truncated": truncated}


# ---------------------------------------------------------------------------
# 因子矩阵 / IC / 综合分
# ---------------------------------------------------------------------------
def _z_value(row, key):
    z = (row or {}).get("z")
    if not isinstance(z, dict):
        return None
    return to_float(z.get(key))


def factor_matrix(rows):
    """横截面因子矩阵：行=标的、列=因子（值为工作台给出的 z 分数）。

    列集合 = 各行 ``z`` 键的并集（排序）；取不到的格给 ``null``（不是 0）。
    """
    items = [row for row in (rows or []) if isinstance(row, dict)]
    factor_keys = sorted({str(key) for row in items
                          for key in ((row.get("z") or {}) if isinstance(row.get("z"), dict) else {})})
    as_of_values = sorted(str(row["as_of"]) for row in items if row.get("as_of"))
    matrix = []
    for row in items:
        line = []
        for key in factor_keys:
            value = _z_value(row, key)
            line.append(None if value is None else round_half_up(value, 4))
        matrix.append(line)
    return {
        "ok": True,
        "as_of": as_of_values[-1] if as_of_values else None,
        "source": "workbench/factors(z)",
        "tickers": [row.get("ticker") for row in items],
        "factors": factor_keys,
        "matrix": matrix,
        "raw": [{"ticker": row.get("ticker"), "factors": row.get("factors") or None}
                for row in items],
    }


def ic_stats(value, factor, forward_days, fallback_tickers=None):
    """因子 RankIC 序列的统计口径：均值/样本标准差/IR(ICIR)/最新值与最近 40 个点。

    ``ir`` **不做年化**（参考实现为 ``mean/std``）；样本 <2 个时 ``stdIc``/``ir`` 为 ``null``。
    """
    points = []
    for point in (value or {}).get("points") or []:
        if not isinstance(point, dict):
            continue
        number = to_float(point.get("ic"))
        if number is None:
            continue
        points.append((point.get("t"), number))
    values = [number for _, number in points]

    average = sum(values) / len(values) if values else None
    deviation = None
    if len(values) > 1:
        deviation = math.sqrt(sum((item - average) ** 2 for item in values) / (len(values) - 1))
    return {
        "ok": True,
        "factor": factor,
        "forwardDays": forward_days,
        "tickers": (value or {}).get("tickers") or list(fallback_tickers or []),
        "source": "workbench/ic",
        "observations": len(values),
        "meanIc": None if average is None else round_half_up(average, 4),
        "stdIc": None if deviation is None else round_half_up(deviation, 4),
        "ir": (round_half_up(average / deviation, 3)
               if deviation is not None and deviation > 0 else None),
        "latestIc": None if not values else round_half_up(values[-1], 4),
        "points": [{"t": stamp, "ic": round_half_up(number, 4)} for stamp, number in points[-40:]],
    }


def composite_z(row):
    """综合分 = ``mom_20``/``mom_60``/``trend`` 三类 z 的算术平均；无有效值 → ``None``。"""
    values = []
    for key in COMPOSITE_KEYS:
        number = _z_value(row, key)
        if number is not None:
            values.append(number)
    if not values:
        return None
    return sum(values) / len(values)


def cross_sectional_z(values):
    """横截面标准化（样本标准差 n-1）；有效值不足 2 个 → 全 ``None``；标准差为 0 → 0。

    只在 workbench ``factors`` 不可用时作为兜底打分（``PAAT.scoreSource`` 会如实标注）。
    """
    items = [to_float(value) for value in values]
    finite = [value for value in items if value is not None]
    if len(finite) < 2:
        return [None] * len(items)
    average = mean(finite)
    deviation = stdev(finite)
    return [None if value is None else ((value - average) / deviation if deviation > 0 else 0.0)
            for value in items]


# ---------------------------------------------------------------------------
# 组合定义（权重解析）
# ---------------------------------------------------------------------------
def pick_frozen_plan_weights(plans):
    """工作台 ``plan`` 列表 → 首个「已冻结且 ``target`` 标的数 ≥2」的计划权重。

    返回 ``(weights, source)``；找不到给 ``(None, None)``。权重非正/非数值的标的被丢弃
    （它们是「无效条目」而不是可用成分），丢弃后不足 2 只则继续找下一个计划。
    """
    for plan in plans or []:
        if not isinstance(plan, dict):
            continue
        if str(plan.get("status") or "").strip().lower() != "frozen":
            continue
        target = plan.get("target")
        if not isinstance(target, dict) or len(target) < 2:
            continue
        weights = {}
        for ticker, weight in target.items():
            number = to_float(weight)
            if number is not None and number > 0:
                weights[str(ticker)] = number
        if len(weights) >= 2:
            return weights, f"工作台 frozen 计划 {plan.get('plan_id')}"
    return None, None


def watchlist_equal_weights(watchlist, limit=8):
    """自选池前 ``limit`` 只等权；池空 → ``(None, 原因)``。"""
    names = []
    for item in watchlist or []:
        text = str(item or "").strip()
        if text and text not in names:
            names.append(text)
    names = names[:limit]
    if not names:
        return None, SOURCE_NO_PORTFOLIO
    return {name: 1.0 / len(names) for name in names}, f"自选池等权（{len(names)} 只）"


# ---------------------------------------------------------------------------
# 统计套利内核（FR-STRAT-002）：OLS 对冲比率 / ADF / 半衰期 / 相关
# ---------------------------------------------------------------------------
# statsmodels v0.14.5 tsa/adfvalues.py: MacKinnon(1994), c, N=1，系数已缩放。
MACKINNON_C_N1 = (2.1659, 1.4412, 0.038269)
MACKINNON_C_N1_LARGE = (1.7339, 0.93202, -0.12745, -0.010368)
MACKINNON_TAU_MIN = -18.83
MACKINNON_TAU_MAX = 2.74
MACKINNON_TAU_STAR = -1.61
#: ADF 的最小样本：对齐观测（差分后回归）少于它 → 统计量为 ``None``（不硬算）。
MIN_ADF_OBS = 40

ADF_IMPL_NOTE = ("单序列 DF 检验（带常数项、0 阶滞后）；MacKinnon(1994) c/N=1 "
                 "完整左尾响应面近似 p 值，标准库实现，非精确分布；"
                 "训练残差筛选不等同校准的 Engle-Granger 协整检验")


def _norm_cdf(value):
    """erfc 避免左尾概率被 1 + erf 的相消截成零。"""
    return 0.5 * math.erfc(-value / math.sqrt(2.0))


def mackinnon_pvalue(t_stat):
    if t_stat > MACKINNON_TAU_MAX:
        return 1.0
    if t_stat < MACKINNON_TAU_MIN:
        return 0.0
    coefficients = MACKINNON_C_N1 if t_stat <= MACKINNON_TAU_STAR else MACKINNON_C_N1_LARGE
    value = 0.0
    for coefficient in reversed(coefficients):
        value = value * t_stat + coefficient
    return _norm_cdf(value)


def adf_test(values):
    """ADF（DF，带常数项、0 阶滞后）单位根检验 → ``{tStat, pValue, n, approx, note}``。

    回归：``Δs_t = a + γ·s_{t-1} + e``；``t = γ / se(γ)``。p 值为
    :func:`mackinnon_pvalue` 的两段响应面近似；保留未舍入概率供筛选。

    差分后样本 < :data:`MIN_ADF_OBS`、或 ``s_{t-1}`` 零方差 → ``tStat``/``pValue`` 为 ``None``
    （检验就是检验：算不出就如实说算不出，绝不「恒通过」）。
    """
    items = [value for value in (to_float(item) for item in (values or []))
             if value is not None]
    n = len(items)
    base = {"tStat": None, "pValue": None, "n": n, "lags": 0, "approx": True,
            "note": ADF_IMPL_NOTE}
    if n < MIN_ADF_OBS + 1:
        return {**base, "note": f"{ADF_IMPL_NOTE}；样本 {n} < {MIN_ADF_OBS + 1}，不检验"}
    dy = [items[i] - items[i - 1] for i in range(1, n)]
    lag = items[:-1]
    count = len(dy)
    mean_x = mean(lag)
    mean_y = mean(dy)
    sxx = sum((x - mean_x) ** 2 for x in lag)
    if sxx <= 0:
        return {**base, "note": f"{ADF_IMPL_NOTE}；滞后项零方差，t 统计量不可计算"}
    sxy = sum((lag[i] - mean_x) * (dy[i] - mean_y) for i in range(count))
    gamma = sxy / sxx
    intercept = mean_y - gamma * mean_x
    rss = sum((dy[i] - intercept - gamma * lag[i]) ** 2 for i in range(count))
    if count <= 2:
        return base
    variance = rss / (count - 2)
    if variance <= 0:
        # 残差零方差 = 完美拟合（构造序列），t 统计量趋于无穷：如实给 None 而不是 inf。
        return {**base, "note": f"{ADF_IMPL_NOTE}；残差零方差，t 统计量不可计算"}
    se = math.sqrt(variance / sxx)
    t_stat = gamma / se
    p_value = mackinnon_pvalue(t_stat)
    return {"tStat": round_half_up(t_stat, 4), "pValue": p_value,
            "n": n, "lags": 0, "approx": True, "note": ADF_IMPL_NOTE}


def half_life(values):
    """价差半衰期（AR(1)）：``Δs_t = b·s_{t-1} + e``，``HL = -ln2 / b``（交易日）。

    ``b ≥ 0``（非均值回复）或样本不足（< 3）→ ``None``。未计常数项（价差已近零均值
    是常用口径），如实在 ``note`` 说明。"""
    items = [value for value in (to_float(item) for item in (values or []))
             if value is not None]
    if len(items) < 3:
        return None
    lag = items[:-1]
    dy = [items[i] - items[i - 1] for i in range(1, len(items))]
    mean_x = mean(lag)
    mean_y = mean(dy)
    sxx = sum((x - mean_x) ** 2 for x in lag)
    if sxx <= 0:
        return None
    slope = sum((lag[i] - mean_x) * (dy[i] - mean_y) for i in range(len(dy))) / sxx
    if slope >= 0:
        return None
    return round_half_up(-math.log(2.0) / slope, 1)


def ols_slope(x_values, y_values):
    """无截距 OLS 斜率 Σxy/Σx²；仅用于启发式对冲，非校准协整检验。"""
    xs = [value for value in (to_float(item) for item in (x_values or []))
          if value is not None]
    ys = [value for value in (to_float(item) for item in (y_values or []))
          if value is not None]
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    sxx = sum(value * value for value in xs)
    if sxx <= 0:
        return None
    return sum(xs[i] * ys[i] for i in range(len(xs))) / sxx


def pearson(x_values, y_values):
    """皮尔逊相关；样本不一致/不足 2/零方差 → ``None``（不是 0）。"""
    xs = [value for value in (to_float(item) for item in (x_values or []))
          if value is not None]
    ys = [value for value in (to_float(item) for item in (y_values or []))
          if value is not None]
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    mean_x = mean(xs)
    mean_y = mean(ys)
    sxx = sum((value - mean_x) ** 2 for value in xs)
    syy = sum((value - mean_y) ** 2 for value in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(len(xs)))
    return sxy / math.sqrt(sxx * syy)


# ---------------------------------------------------------------------------
# 价量因子（矩阵 as_of 路径的本地复算；公式与 workbench factors.py 逐行同源）
# ---------------------------------------------------------------------------
def price_volume_factor_values(closes, volumes=None):
    """价量因子值（与 ``plugins/workbench/python/factors.py::factor_values`` 同公式）。

    背景（FR-DATA-003 / 任务 4）：工作台 ``factors`` 工具面的 payload 白名单在
    ``app.ANALYTICS_ENDPOINTS``（本任务不可改），``as_of`` 无法透传给子进程；因此
    ``/api/v3/factors/matrix?as_of=`` 的价量列改为：K 线经 ``server.data.cache``
    （PIT 唯一入口，显式 as_of + INCLUSIVE）读取后，**用同一份公式本地复算**。本函数
    就是那份公式：``mom_20 / mom_60 / vol_20 / trend / rsi_14 / liq_ratio / mdd_60``
    （外加展示用 ``close``；z 打分不含 close）。

    与参考实现的差异只有防御性：分母为 0 / 样本不足的**单个因子**给 ``None``（参考实现
    会抛异常导致整只标的失败）；整体样本 < 65 根 → ``None``（与参考实现的 65 根下限一致）。
    ``volumes`` 缺失（bar 无 ``v``）→ ``liq_ratio`` 为 ``None``。
    """
    closes = [value for value in (to_float(item) for item in (closes or []))
              if value is not None]
    if len(closes) < 65:
        return None

    def ratio(numerator, denominator):
        if numerator is None or denominator is None or denominator == 0:
            return None
        return numerator / denominator

    latest = closes[-1]
    mom_20 = ratio(latest, closes[-21])
    mom_60 = ratio(latest, closes[-61])
    mom_20 = None if mom_20 is None else mom_20 - 1
    mom_60 = None if mom_60 is None else mom_60 - 1

    rets = []
    for index in range(len(closes) - 20, len(closes)):
        if closes[index - 1] is not None and closes[index - 1] > 0:
            rets.append(closes[index] / closes[index - 1] - 1)
    average = mean(rets)
    variance = (sum((value - average) ** 2 for value in rets) / (len(rets) - 1)
                if len(rets) > 1 else 0.0)
    vol_20 = math.sqrt(variance) * math.sqrt(TRADING_DAYS)

    ma20_window = closes[-20:]
    ma20 = sum(ma20_window) / len(ma20_window) if ma20_window else None
    trend = ratio(latest, ma20)
    trend = None if trend is None else trend - 1
    rsi_14 = _rsi(closes)
    liq_ratio = None
    if volumes:
        short = [value for value in (to_float(item) for item in volumes[-20:])
                 if value is not None]
        long_ = [value for value in (to_float(item) for item in volumes[-60:])
                 if value is not None]
        if len(short) == 20 and len(long_) == 60:
            liq_ratio = ratio(mean(short), max(mean(long_), 1e-9))
    peak = -math.inf
    mdd_60 = 0.0
    for price in closes[-60:]:
        peak = max(peak, price)
        if peak > 0:
            mdd_60 = min(mdd_60, price / peak - 1)
    return {"mom_20": mom_20, "mom_60": mom_60, "vol_20": vol_20, "trend": trend,
            "rsi_14": rsi_14, "liq_ratio": liq_ratio, "mdd_60": mdd_60, "close": latest}


def _rsi(values, n=14):
    """RSI（与 workbench ``factors.py::rsi`` 同式）；样本 < n+1 → ``None``。"""
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[-n:]) / n
    avg_loss = sum(losses[-n:]) / n
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)
