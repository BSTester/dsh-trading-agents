"""策略注册表（规格 §5.2）：universe(as_of) → target_weights(as_of)。

信号可复现铁律：输入只有 PIT store + as_of。
单标的策略的信号函数复用 trading_datasource.backtest 唯一实现（rsi 25/75，
ma_cross 5/20），导入放在调用时惰性解析，避免库模块导入即强依赖 sys.path 配置。
"""
from . import factors, store

REGISTRY = {}


def strategy(sid):
    def deco(cls):
        REGISTRY[sid] = cls()  # 注册实例：调用方直接 strat.target_weights(conn, as_of)
        return cls
    return deco


def _signals():
    from trading_datasource.backtest import ma_cross_signal, rsi_signal
    return ma_cross_signal, rsi_signal


class SingleTicker:
    id = "base"
    # 子类提供 _fn：df → 1（买）/ -1（卖）/ 0（持有）。
    # 注：既有信号函数（rsi_signal/ma_cross_signal）返回的是逐 bar 信号序列
    # （pandas Series），这里统一取最后一根 bar 的信号状态作为 as_of 时点信号。
    _fn = staticmethod(lambda df: 0)

    def universe(self, conn, as_of):
        return []

    def signal(self, conn, symbol, as_of):
        bars = store.read_bars(conn, symbol, "1d", as_of=as_of, limit=60)
        if len(bars) < 25:
            return "HOLD"
        import pandas as pd
        df = pd.DataFrame(bars).rename(columns={"t": "date", "c": "close"})
        v = self._fn(df)
        if hasattr(v, "iloc"):  # 序列信号 → 取 as_of（最后一根 bar）状态
            v = v.iloc[-1]
        return "BUY" if v == 1 else ("SELL" if v == -1 else "HOLD")


@strategy("rsi")
class RsiStrategy(SingleTicker):
    id = "rsi"

    def _fn(self, df):
        ma_cross_signal, rsi_signal = _signals()
        return rsi_signal(df, 25, 75)


@strategy("ma_cross")
class MaCrossStrategy(SingleTicker):
    id = "ma_cross"

    def _fn(self, df):
        ma_cross_signal, rsi_signal = _signals()
        return ma_cross_signal(df, 5, 20)


@strategy("momentum_value_top5")
class MomentumValueTop5:
    id = "momentum_value_top5"
    top_n = 5
    weights = {"momentum_60": 0.5, "momentum_20": 0.2, "ep": 0.3}

    def universe(self, conn, as_of):
        snap = store.read_universe(conn, as_of=as_of, index_name="SH.000300")
        return [f"{'SH.' if c.startswith(('6', '9')) else 'SZ.' if c.startswith(('0', '3')) else 'BJ.'}{c}"
                for c in (snap["symbols"] if snap else [])]

    def target_weights(self, conn, as_of):
        universe = self.universe(conn, as_of)
        per = {}
        for sym in universe:
            fv = {}
            for name in self.weights:
                try:
                    fv[name] = factors.REGISTRY[name](conn, sym, as_of)
                except Exception:
                    fv[name] = None
            if any(v is not None for v in fv.values()):
                per[sym] = fv
        if not per:
            return {}
        # 逐因子横截面 z 后按权重合成
        zs = {}
        for name, w in self.weights.items():
            raw = {s: per[s][name] for s in per if per[s][name] is not None}
            for s, z in factors.cross_sectional_zscore(raw).items():
                zs.setdefault(s, {})[name] = z
        scored = {}
        for s, z in zs.items():
            den = sum(w for name, w in self.weights.items() if z.get(name) is not None)
            num = sum(w * z[name] for name, w in self.weights.items() if z.get(name) is not None)
            scored[s] = num / den if den else None  # 全 None 的截面（如样本<3）→ 不打分
        ranked = sorted((s for s in scored if scored[s] is not None),
                        key=scored.get, reverse=True)[:self.top_n]
        if not ranked:
            return {}
        w = round(1.0 / len(ranked), 4)
        return {s: w for s in ranked}
