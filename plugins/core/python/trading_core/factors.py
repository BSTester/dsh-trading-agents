"""因子注册表：@factor 注册，统一签名 fn(conn, futu_symbol, as_of) -> float|None。

可复现性（规格 §5.1）：输入只有 PIT store 与 as_of；情绪永不入内。

本模块另含**验证门统计**（WP14 任务 3，规格 §9.3）：IC 报告（t 检验/分层单调）、
因子半衰期、Top-N 换手率与门槛判定 ``passes_gate``——阈值集中定义，CLI 与
``rules-validate`` 共用同一实现，不在各处重写。
"""
import math
from statistics import NormalDist

from . import store

REGISTRY = {}

#: IC 均值 t 检验门槛：正态近似下约 5% 双侧
IC_T_THRESHOLD = 2.0
#: IC 序列最少期数：正态近似在极小样本下不可用（不是小样本 t 分布）
IC_MIN_SAMPLES = 20
#: 分层单调性门槛：分位序（1..Q）与各组收益的 Spearman
LAYER_MONOTONIC_MIN = 0.9


def factor(name):
    def deco(fn):
        REGISTRY[name] = fn
        return fn
    return deco


def _closes(conn, symbol, as_of, n):
    bars = store.read_bars(conn, symbol, "1d", as_of=as_of, limit=n)
    if len(bars) < n:
        return None
    return [b["c"] for b in bars]


def _momentum(n):
    def fn(conn, symbol, as_of):
        closes = _closes(conn, symbol, as_of, n + 1)
        if closes is None:
            return None
        return closes[-1] / closes[0] - 1.0
    return fn


def _volatility(n):
    def fn(conn, symbol, as_of):
        closes = _closes(conn, symbol, as_of, n + 1)
        if closes is None:
            return None
        rets = [math.log(closes[i + 1] / closes[i]) for i in range(n)]
        mean = sum(rets) / n
        return math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) * math.sqrt(250)
    return fn


factor("momentum_20")(_momentum(20))
factor("momentum_60")(_momentum(60))
factor("momentum_120")(_momentum(120))
factor("volatility_20")(_volatility(20))


def valuation_values(ticker, fetcher=None, akshare_module=None, home=None, client=None,
                     credential_path=None):
    """估值因子唯一实现（实现体自 plugins/workbench/python/factors.py 收敛，WP2 任务 3；
    字段路径 2026-09-14 三市场 48 通道实测锁定，不另猜）。

    优先富途（PE/PB/PS + 历史分位，全市场），失败回退同花顺（**仅 A 股**，含 PEG）。
    返回 (values, source)。fetcher 替换富途 call_tool、akshare_module 替换 akshare
    （离线测试注入口，仓库既有模式）；online 缺省行为与收敛前逐字一致。

    取数通道（WP13 任务 1）：``fetcher`` 未注入时经 ``trading_datasource.channel.fetch``
    ——openapi 有凭据走 REST ``f10.valuation_detail``（响应同为 ``trend`` 形状，锁定表
    §C.5），否则 mcp（无凭据时标注回退）。``home/client/credential_path`` 为通道分派参数；
    source 字面量 ``futu/quote_valuation_detail`` 不变（落库口径零变化）。
    """
    from trading_datasource import channel
    from trading_datasource.market import is_a_share, to_futu_symbol
    values, sources = {}, []

    # ① 富途（优先通道：openapi 就绪走 REST，否则 mcp）
    try:
        symbol = to_futu_symbol(ticker)
        for vt, key in ((1, "pe_ttm"), (2, "pb"), (3, "ps")):
            try:
                params = {"symbol": symbol, "valuation_type": vt}
                if fetcher is not None:
                    data = fetcher("quote_valuation_detail", params)
                else:
                    data, _channel = channel.fetch("quote_valuation_detail", params,
                                                   method="f10.valuation_detail",
                                                   home=home, client=client,
                                                   credential_path=credential_path)
                    data = data or {}  # None 视为无数据（下轮 trend 取空、跳过该指标）
                trend = data.get("trend") or {}
                value, percentile = trend.get("current_value"), trend.get("valuation_percentile")
                if isinstance(value, (int, float)) and value > 0:
                    values[key] = round(float(value), 4)
                if isinstance(percentile, (int, float)):
                    values[f"{key}_pct"] = round(float(percentile), 2)
            except Exception:
                continue  # 单项失败不影响其余估值指标
        if values:
            sources.append("futu/quote_valuation_detail")
    except Exception:
        pass

    # ② 同花顺备用（**仅 A 股**，含 PEG）
    #
    # 必须显式判市场：`ak.stock_value_em` 吃的是 A 股代码，
    # `000001.HK`（港股长和）用 split(".")[0] 得到 "000001"，
    # 正好是平安银行 —— 富途估值一旦取不到，就会把平安银行的 PE/PB
    # 悄悄填进长和的因子行。此前这里没有任何市场判断。
    try:
        if akshare_module is None:
            import akshare as akshare_module
        ak = akshare_module
        if not is_a_share(ticker):
            raise RuntimeError("同花顺估值仅支持 A 股")
        df = ak.stock_value_em(symbol=str(ticker).split(".")[0])
        if df is not None and not df.empty:
            row = df.tail(1).to_dict("records")[0]

            def pick(*names):
                for name in names:
                    value = row.get(name)
                    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                        return float(value)
                return None

            fallback = {"pe_ttm": pick("PE(TTM)"), "pb": pick("市净率"),
                        "peg": pick("PEG值"), "ps": pick("市销率")}
            added = {k: v for k, v in fallback.items() if v is not None and k not in values}
            if added:
                values.update(added)
                sources.append("akshare/同花顺估值")
    except Exception:
        pass

    return values, "+".join(sources) if sources else ""


@factor("ep")
def _ep(conn, symbol, as_of):
    """EP = 市盈率倒数（字段 pe_ttm，锁定表口径），方向统一"越大越看多"。"""
    v = store.read_valuations(conn, symbol, as_of).get("pe_ttm")
    return (1.0 / v) if v and v > 0 else None


def cross_sectional_zscore(values, mad_bound=3.0):
    """横截面 z-score，MAD 去极值（规格 §5.1）。返回 {key: z}。"""
    if len(values) < 3:
        return {k: None for k in values}
    xs = sorted(values.values())
    med = xs[len(xs) // 2]
    mad = sorted(abs(x - med) for x in xs)[len(xs) // 2] or 1e-12
    clipped = {k: med + max(-mad_bound, min(mad_bound, (v - med) / (1.4826 * mad))) * (1.4826 * mad)
               for k, v in values.items()}
    mean = sum(clipped.values()) / len(clipped)
    var = sum((v - mean) ** 2 for v in clipped.values()) / (len(clipped) - 1)
    std = math.sqrt(var) or 1e-12
    return {k: (v - mean) / std for k, v in clipped.items()}


def composite_score(per_symbol_factors, weights):
    """{symbol: {factor: raw}} × {factor: weight} → {symbol: score}。
    因子方向在注册时已统一（越大越看多），此处不再翻方向。"""
    out = {}
    for symbol, fv in per_symbol_factors.items():
        num = den = 0.0
        for name, w in weights.items():
            v = fv.get(name)
            if v is not None:
                num += w * v
                den += abs(w)
        out[symbol] = num / den if den else None
    return out


def _rank(values):
    order = sorted(values, key=values.get)
    return {k: i for i, k in enumerate(order)}


def _spearman_ordered(xs, ys):
    """已配对序列的 Spearman；样本 < 3 返回 None。"""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _rank({i: v for i, v in enumerate(xs)}), _rank({i: v for i, v in enumerate(ys)})
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    denom = n * (n * n - 1)
    return 1 - 6 * d2 / denom if denom else None


#: 前向收益定位窗口：从 end 往前最多读这么多根 bar。
#: 需覆盖验证门最大取样跨度（lookback*step）与运行时 IC 窗口（IC_WINDOW_DAYS）。
FORWARD_LOOKBACK_BARS = 400


def forward_return(conn, symbol, as_of, horizon, end=None):
    """``as_of`` 起 ``horizon`` 个交易日的前向收益（**唯一实现**，规格 §9.3）。

    验证门（``cli._rule_panels``）与 ``ic_weighted`` 运行时
    （``rule_engine._ic_factor_weights``）共用本函数——两处各写一份会让「批准时的 IC」
    与「运行时的 IC」按不同前向收益计算，统计有效性无从谈起（阶段 B 修复的缺陷）。

    口径：
      * 基准 = ``as_of`` **当根** bar 收盘；终点 = 其后第 ``horizon`` 根收盘；
      * 找不到 ``as_of`` 当根，或其后不足 ``horizon`` 根 → ``None``
        （宁缺毋假：绝不返回短窗口收益冒充 horizon 日收益）；
      * ``end`` 是数据上限——``None`` 表示到该标的**最新** bar（历史验证：
        前向收益已实现，本就应该用它）；给定值表示不得越界（运行时防前视：
        评估日之后的数据不得参与 IC 加权）；
      * 定位窗口为 ``FORWARD_LOOKBACK_BARS`` 根：``as_of`` 早于该窗口起点的样本
        定位不到当根 → ``None``（如实缺值，不外推）。
    """
    if horizon < 1:
        raise ValueError(f"horizon 必须为正整数，收到 {horizon!r}")
    limit = end if end is not None else store.last_bar_date(conn, symbol, "1d")
    if limit is None:
        return None
    bars = store.read_bars(conn, symbol, "1d", as_of=limit, limit=FORWARD_LOOKBACK_BARS)
    for index, bar in enumerate(bars):
        if bar["t"] == as_of:
            future = index + horizon
            if future < len(bars):
                return bars[future]["c"] / bar["c"] - 1.0
            return None
    return None


def rank_ic(factor_values, forward_returns):
    """Spearman 秩相关；样本 < 3 或零方差返回 None。"""
    common = [k for k in factor_values if k in forward_returns]
    if len(common) < 3:
        return None
    return _spearman_ordered([factor_values[k] for k in common],
                             [forward_returns[k] for k in common])


def quintile_returns(factor_values, forward_returns, buckets=5):
    """按因子升序分桶，返回 {Q1..Q5: 组内平均前向收益}；样本不足返回 {}。"""
    common = sorted((k for k in factor_values if k in forward_returns), key=factor_values.get)
    if len(common) < buckets:
        return {}
    size = len(common) // buckets
    out = {}
    for q in range(buckets):
        part = common[q * size:(q + 1) * size] if q < buckets - 1 else common[(buckets - 1) * size:]
        out[f"Q{q + 1}"] = sum(forward_returns[k] for k in part) / len(part)
    return out


def ic_report(factor_panel, forward_panel, quantiles=5):
    """多期 IC 报告——验证门的唯一统计入口（规格 §9.3）。

    ``factor_panel``/``forward_panel`` 形状 ``{date: {symbol: value}}``：两侧日期键取
    交集，单期共同样本 < 3 的日期跳过（沿用 ``rank_ic`` 语义）。返回::

        {"rank_ic_mean", "t_stat", "p_value", "n",
         "layers": [{"q": 1..Q, "ret": 各分位组前向收益的跨期均值}],
         "monotonic": 分位序 vs 组收益 Spearman >= LAYER_MONOTONIC_MIN}

    **口径披露**：t 统计用 IC 序列的均值/标准误 + **正态近似**（``statistics.NormalDist``），
    不是小样本 t 分布；IC 序列零方差时 ``t_stat``/``p_value`` 为 ``None``（无法检验），
    由 ``passes_gate`` 判不通过。样本量要求见 ``IC_MIN_SAMPLES``。
    """
    ics, layer_acc = [], {}
    for date in sorted(set(factor_panel) & set(forward_panel)):
        fv, fr = factor_panel[date], forward_panel[date]
        ic = rank_ic(fv, fr)
        if ic is not None:
            ics.append(ic)
        for key, value in quintile_returns(fv, fr, buckets=quantiles).items():
            layer_acc.setdefault(key, []).append(value)
    n = len(ics)
    layers = [{"q": index + 1, "ret": sum(values) / len(values)}
              for index, (_key, values) in enumerate(sorted(layer_acc.items()))]
    monotonic = False
    if len(layers) >= 3:
        spread = _spearman_ordered([row["q"] for row in layers],
                                   [row["ret"] for row in layers])
        monotonic = spread is not None and spread >= LAYER_MONOTONIC_MIN
    if n == 0:
        return {"rank_ic_mean": None, "t_stat": None, "p_value": None, "n": 0,
                "layers": layers, "monotonic": monotonic}
    mean = sum(ics) / n
    t_stat = p_value = None
    if n >= 2:
        var = sum((x - mean) ** 2 for x in ics) / (n - 1)
        std = math.sqrt(var)
        if std > 0:
            t_stat = mean / (std / math.sqrt(n))
            p_value = 2 * (1 - NormalDist().cdf(abs(t_stat)))
    return {"rank_ic_mean": mean, "t_stat": t_stat, "p_value": p_value, "n": n,
            "layers": layers, "monotonic": monotonic}


def factor_half_life(series):
    """因子半衰期（lag-1 自相关估计）。

    单位是**采样间隔（调仓期数）**，不是自然日——日频序列下才是「交易日」。
    ``None`` 从序列剔除；样本 < 3、零方差、自相关 >= 1 → ``None``（不可估计）；
    自相关 <= 0 → ``0.0``（该间隔内即无持续性）。
    """
    xs = [float(v) for v in series if v is not None]
    if len(xs) < 3:
        return None
    left, right = xs[:-1], xs[1:]
    mean_left, mean_right = sum(left) / len(left), sum(right) / len(right)
    cov = sum((x - mean_left) * (y - mean_right) for x, y in zip(left, right))
    var_left = sum((x - mean_left) ** 2 for x in left)
    var_right = sum((y - mean_right) ** 2 for y in right)
    if var_left <= 0 or var_right <= 0:
        return None
    rho = cov / math.sqrt(var_left * var_right)
    if rho <= 0:
        return 0.0
    if rho >= 1:
        return None
    return math.log(0.5) / math.log(rho)


def turnover(top_sets):
    """相邻期 Top-N 成员的**新进比例**均值：(|当期 − 上期|) / |当期|。

    期数 < 2 或空集输入 → ``None``；空期（``set()``）跳过，不计入均值。
    """
    periods = [set(item) for item in top_sets if item]
    if len(periods) < 2:
        return None
    ratios = [len(cur - prev) / len(cur) for prev, cur in zip(periods, periods[1:]) if cur]
    return sum(ratios) / len(ratios) if ratios else None


def passes_gate(report, t_threshold=IC_T_THRESHOLD, min_samples=IC_MIN_SAMPLES,
                monotonic_min=LAYER_MONOTONIC_MIN):
    """验证门判定（规格 §9.3）：``(ok, reasons)``。

    通过条件：IC 样本量达标 **且** t 统计为正且 >= 门槛（因子方向统一为「越大越看多」，
    负 t 一律拒绝）**且** 分层单调。``reasons`` 为空表示通过，非空逐条即为验证报告
    的拒绝依据（供 ``rules-validate`` 落库，不在此处写库）。
    """
    reasons = []
    n = report.get("n") or 0
    if n < min_samples:
        reasons.append(f"IC 样本不足：{n} < {min_samples}（正态近似在极小样本下不可用）")
    t_stat = report.get("t_stat")
    if t_stat is None:
        reasons.append("IC 序列无方差或样本不足，无法做 t 检验")
    elif t_stat < 0:
        reasons.append(f"因子方向为负：t={t_stat:.2f}（要求越大越看多）")
    elif t_stat < t_threshold:
        reasons.append(f"t 检验不显著：t={t_stat:.2f} < {t_threshold}")
    if not report.get("monotonic"):
        reasons.append(f"分层不单调：分位序与组收益的 Spearman < {monotonic_min}")
    return (not reasons), reasons
