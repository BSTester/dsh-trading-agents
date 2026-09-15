"""因子注册表：@factor 注册，统一签名 fn(conn, futu_symbol, as_of) -> float|None。

可复现性（规格 §5.1）：输入只有 PIT store 与 as_of；情绪永不入内。
"""
import math

from . import store

REGISTRY = {}


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


def valuation_values(ticker, fetcher=None, akshare_module=None):
    """估值因子唯一实现（实现体自 plugins/workbench/python/factors.py 收敛，WP2 任务 3；
    字段路径 2026-09-14 三市场 48 通道实测锁定，不另猜）。

    优先富途（PE/PB/PS + 历史分位，全市场），失败回退同花顺（**仅 A 股**，含 PEG）。
    返回 (values, source)。fetcher 替换富途 call_tool、akshare_module 替换 akshare
    （离线测试注入口，仓库既有模式）；online 缺省行为与收敛前逐字一致。
    """
    from trading_datasource.market import is_a_share, to_futu_symbol
    values, sources = {}, []

    # ① 富途 MCP（优先通道）
    try:
        from trading_datasource.futu_mcp import call_tool  # 共享客户端
        get = fetcher or call_tool
        symbol = to_futu_symbol(ticker)
        for vt, key in ((1, "pe_ttm"), (2, "pb"), (3, "ps")):
            try:
                data = get("quote_valuation_detail",
                           {"symbol": symbol, "valuation_type": vt})
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
