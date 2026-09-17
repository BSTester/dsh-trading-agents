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
#: 由 ``register_rule`` 动态注册的规则名（内置策略不在此集合）。
#: 解析器据此区分两类名字：内置策略命中即用；规则必须每次回查 DB 状态。
RULE_NAMES = set()


def _home(home=None):
    """关注池配置根：显式 home 优先，否则 $DSH_HOME，再否则 ~/.dsh（daemon 同口径）。"""
    return Path(home) if home is not None else Path(
        os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def strategy(sid):
    def deco(cls):
        REGISTRY[sid] = cls()  # 注册实例：调用方直接 strat.target_weights(conn, as_of)
        return cls
    return deco


def register_rule(spec, registry=None):
    """把规则 spec 校验后注册进 REGISTRY（``plan_auto`` 可按 rule_id 直接消费）。

    协议校验与实例构造都在 ``rule_engine``（规则协议的**唯一实现**）；本函数只负责
    「注册进策略注册表」这一 strategies 侧职责。反向 import 放在函数内：rule_engine
    的实例要回调本模块的 ``risk_capped_weights``，模块级互相 import 会成环。

    规则名会记进 ``RULE_NAMES``：内置策略与规则在解析器里**待遇不同**——规则每次都要
    回查 DB 状态（见 ``planner._resolve_strategy``），进程内实例不得覆盖「停用」这一
    人工决定。``unregister_rule`` 是配套的摘除入口。
    """
    from . import rule_engine
    instance = rule_engine.load_rule(spec, registry=registry)
    REGISTRY[spec["rule_id"]] = instance
    RULE_NAMES.add(spec["rule_id"])
    return instance


def is_rule(name):
    """该名字是否由 ``register_rule`` 动态注册（= 规则，而非内置策略）。"""
    return name in RULE_NAMES


def unregister_rule(name):
    """摘掉进程内的规则实例（状态不再是 ``enabled`` 或加载失败时调用，fail-closed）。

    背景（WP14 任务 6 e2e 暴露的缺陷）：``_resolve_strategy`` 原先把 REGISTRY 当作
    一级事实来源，规则一旦被解析过一次就会常驻进程内——此后用户在 Web 上停用该规则，
    同一进程内的下一次计划生成**仍会命中陈旧实例并继续下单**。``disabled`` 是终态
    （``rule_engine.RULE_TRANSITIONS``），这等于「停用」在长驻服务进程里失效。
    规则名一律以 DB 状态为准，本函数负责把失效实例清出注册表。
    """
    RULE_NAMES.discard(name)
    REGISTRY.pop(name, None)


def risk_capped_weights(selected, home=None):
    """按风控上限折算等权目标权重（组合策略共用的**唯一实现**，规格 §4.2 第 5 点）。

    策略不得生成风控规则 5/6 注定拒绝的目标：单票权重上限取 ``risk_config`` 的
    ``max_position_pct``（默认 0.25）、标的数上限取 ``max_positions``（默认 5），
    超出部分留现金。定量口径（全部确定性、不依赖 dict 顺序）::

        基数 = 1 / len(selected)                        # 截断前计数：先算等权基准
        单票权重 = floor4(min(基数, max_position_pct))   # **向下**取整到 4 位小数
        入选 = list(selected)[:max_positions]            # 截断优先级 = 传入顺序
        其余标的与未用满的权重 → 现金

    向下取整的理由：向上取整会让目标名义略超 ``max_position_pct``，在规则 5 的
    严格大于判定下沦为「必被拒的目标」（差额虽小，但结构性拒绝不该由策略制造）。

    ``selected`` 的**顺序即截断优先级**，确定性由调用方负责：``watchlist_rsi`` 传
    标的代码升序（无打分可依），规则解释器传打分降序（截断时保留最优标的）。

    ``cap``/``limit`` 退化（≤0）时返回 ``{}``（fail-safe）：cap≤0 会产出**负权重**
    （经 planner diff 变成非预期卖出）；limit≤0 时列表切片会静默变成「除末位全选」。
    两者都不该被静默解释成某种目标持仓，故不产生任何目标（等价全现金、不下单）。
    配置本身的合法性由 ``risk_config`` 的未知键校验与部署审查把关。
    """
    names = list(selected)
    if not names:
        return {}
    from .daemon import risk_config
    cfg = risk_config(_home(home))
    cap = float(cfg["max_position_pct"])
    limit = int(cfg["max_positions"])
    if cap <= 0 or limit <= 0:
        return {}
    base = 1.0 / len(names)
    per_name = math.floor(min(base, cap) * 10000) / 10000  # 向下取整，不超上限
    return {name: per_name for name in names[:limit]}


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
    target_weights 把 BUY 标的等权、**非 BUY 不入表**——入表集合 = 「目标持仓」，
    缺席 = 目标权重 0。**缺席不等于「已持仓的清仓」**：清仓要由 planner 的
    **受管集合**（``managed``，规格 §4.2 第 6 点）把不在 target 的已持仓标的
    纳入 diff 才产生。没有受管集合时 planner 只遍历 target 的键，缺席标的永远
    不生成 SELL（自动流水线只买不退）——该缺口已于 2026-09-16 由 managed 修复，
    plan_auto 传 managed = 本市场关注池 ∩ universe。

    **风控上限（规格 §4.2 第 5 点）**：策略不得生成风控规则 5/6 注定拒绝的目标——
    单票权重上限取 ``risk_config`` 的 ``max_position_pct``（默认 0.25）、标的数上限取
    ``max_positions``（默认 5），超出部分留现金。定量口径与向下取整的理由见
    ``risk_capped_weights``（组合策略共用的唯一实现，本类不再自持一份）。

    **市场维度（2026-09-16 修订 K1）**：``market`` 给定时 universe 只取该市场链
    （``SH`` 链含 SZ/BJ，口径见 ``planner.CALENDAR_MARKET``），且**分母与
    ``max_positions`` 截断都发生在过滤之后**——跨市场合并计数会让先排序的市场
    吃掉全部名额（实测：三市场各 2 只全 BUY、``max_positions=5`` 时美股恒为 0），
    同时用全市场 BUY 数作分母会让各市场资金长期闲置。生产路径
    （``planner.plan_auto``）必传 market；``market=None`` 保留「全池」语义供直接
    调用与测试使用。

    **命名池（2026-09-16 修订 I1）**：``watchlist`` 指定**配置里的池键名**
    （缺省 ``watchlist``，即 ``trading-platform.json`` 既有扁平列表）。指定的键
    不存在 → ``ValueError``（fail-closed：作业入口软跳过并告警，不静默换池）；
    缺省池不存在则视为合法空池（历史配置里可能根本没有该键）。

    配置读取需要 home，而注册表里的策略实例是单例、无可变状态，因此 home 作为
    显式参数传入（缺省按 $DSH_HOME → ~/.dsh 推导）；这样同一实例可在测试里
    指向任意临时 home，不需要为每个 home 重新注册实例。
    ``risk_config`` 是既有唯一实现（``~/.dsh/trading-risk.json`` 覆盖默认值，
    未知键报错）——本类不另写配置读取；配置非法时异常如实上抛，由作业入口
    按 fail-closed 处理（静默回退默认值会掩盖配置错误）。
    关注池元素按富途 symbol（如 SH.600519）原样使用：不猜市场前缀，写错的标的
    读不到 bar → HOLD → 不入权重（宁缺毋假）。读取/分片经 ``watchlist`` 模块的
    唯一实现（I4）——本类不重写池子解析。
    """

    id = "watchlist_rsi"

    def _fn(self, df):
        ma_cross_signal, rsi_signal = _signals()
        return rsi_signal(df, 25, 75)

    def universe(self, conn, as_of, home=None, market=None, watchlist=None):
        from . import watchlist as watchlist_mod
        key = watchlist or watchlist_mod.DEFAULT_POOL_KEY
        return watchlist_mod.watchlist_symbols(
            _home(home), key=key, market=market,
            strict=key != watchlist_mod.DEFAULT_POOL_KEY)

    def target_weights(self, conn, as_of, home=None, market=None, watchlist=None):
        # 市场过滤先行（K1）：分母与截断都只看本市场链的 BUY，跨市场不互相挤占名额
        buys = sorted(s for s in self.universe(conn, as_of, home=home, market=market,
                                               watchlist=watchlist)
                      if self.signal(conn, s, as_of) == "BUY")
        # 风控上限折算的唯一实现在 risk_capped_weights（规则解释器共用同一份）；
        # 代码升序即本策略的截断优先级（无打分可依）——见该函数 docstring。
        return risk_capped_weights(buys, home=home)


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
