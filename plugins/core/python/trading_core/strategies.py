"""策略注册表（规格 §5.2）：universe(as_of) → target_weights(as_of)。

信号可复现铁律：输入只有 PIT store + as_of。
单标的策略的信号函数复用 trading_datasource.backtest 唯一实现（rsi 25/75，
ma_cross 5/20），导入放在调用时惰性解析，避免库模块导入即强依赖 sys.path 配置。
"""
import math
import os
from pathlib import Path

from . import factors, store

REGISTRY = {}


def _home(home=None):
    """关注池配置根：显式 home 优先，否则 $DSH_HOME，再否则 ~/.dsh（daemon 同口径）。"""
    return Path(home) if home is not None else Path(
        os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


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


@strategy("watchlist_rsi")
class WatchlistRsiStrategy(SingleTicker):
    """关注池 RSI 组合策略（规格 §4.6）：BUY 等权、其余现金，**权重尊重风控上限**。

    与单标的 RsiStrategy 的区别只在聚合口径：universe 来自配置关注池，
    target_weights 把 BUY 标的等权、非 BUY 不入表（0 权重=目标清仓由 planner
    比较券商实际持仓后的 diff 自然产生，策略层只决定目标持仓）。

    **风控上限（规格 §4.2 第 5 点，2026-09-16 修订）**：策略不得生成风控规则 5/6
    注定拒绝的目标——单票权重上限取 ``risk_config`` 的 ``max_position_pct``
    （默认 0.25）、标的数上限取 ``max_positions``（默认 5），超出部分留现金。
    定量口径（全部确定性、不依赖 dict 顺序）::

        基数 = 1 / len(BUY 标的)                    # 截断前计数：先算等权基准
        单票权重 = floor4(min(基数, max_position_pct))   # **向下**取整到 4 位小数
        入选 = sorted(BUY 标的)[:max_positions]      # 按标的代码升序，确定性截断
        其余标的与未用满的权重 → 现金

    向下取整的理由：向上取整会让目标名义略超 ``max_position_pct``，在规则 5 的
    严格大于判定下沦为「必被拒的目标」（差额虽小，但结构性拒绝不该由策略制造）。

    配置读取需要 home，而注册表里的策略实例是单例、无可变状态，因此 home 作为
    显式参数传入（缺省按 $DSH_HOME → ~/.dsh 推导）；这样同一实例可在测试里
    指向任意临时 home，不需要为每个 home 重新注册实例。
    ``risk_config`` 是既有唯一实现（``~/.dsh/trading-risk.json`` 覆盖默认值，
    未知键报错）——本类不另写配置读取；配置非法时异常如实上抛，由作业入口
    按 fail-closed 处理（静默回退默认值会掩盖配置错误）。
    关注池元素按富途 symbol（如 SH.600519）原样使用：不猜市场前缀，写错的标的
    读不到 bar → HOLD → 不入权重（宁缺毋假）。
    """

    id = "watchlist_rsi"

    def _fn(self, df):
        ma_cross_signal, rsi_signal = _signals()
        return rsi_signal(df, 25, 75)

    def universe(self, conn, as_of, home=None):
        from .daemon import platform_config
        watchlist = platform_config(_home(home)).get("watchlist") or []
        if isinstance(watchlist, str):
            # 兼容逗号分隔写法：字符串按字符迭代会静默产出垃圾标的，这里显式切开
            watchlist = watchlist.split(",")
        return [str(s).strip() for s in watchlist if str(s).strip()]

    def target_weights(self, conn, as_of, home=None):
        from .daemon import risk_config
        cfg = risk_config(_home(home))
        cap = float(cfg["max_position_pct"])
        limit = int(cfg["max_positions"])
        buys = sorted(s for s in self.universe(conn, as_of, home=home)
                      if self.signal(conn, s, as_of) == "BUY")
        if not buys:
            return {}
        if cap <= 0 or limit <= 0:
            # 退化配置 fail-safe：cap≤0 会产出**负权重**（经 planner diff 变成非预期
            # 卖出）；limit≤0 时 buys[:limit] 在 Python 切片下会静默变成「除末位全选」。
            # 两者都不该被静默解释成某种目标持仓，故不产生任何目标（等价全现金、不下单）。
            # 配置本身的合法性由 risk_config 的未知键校验与部署审查把关。
            return {}
        base = 1.0 / len(buys)
        per_name = min(base, cap)
        per_name = math.floor(per_name * 10000) / 10000  # 向下取整，不超上限
        return {s: per_name for s in buys[:limit]}


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
