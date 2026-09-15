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
