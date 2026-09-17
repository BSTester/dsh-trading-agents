"""声明式规则协议与状态机（规格 §9.2/§9.3）。

职责边界（与 store 的分工）：

  * ``validate_spec`` —— 协议校验（字段白名单/取值域/因子注册与成熟度/来源纪律），
    一次返回全部问题（提案方一次修完，不挤牙膏）；**纯函数，不触库**；
  * ``load_rule`` / ``RuleStrategy`` —— 解释器：spec → Strategy 协议实例
    （``universe``/``target_weights``），供 ``strategies.register_rule`` 注册后由
    ``plan_auto`` 按 rule_id 消费；风控上限口径**复用** ``strategies.risk_capped_weights``，
    本模块不重写一份；
  * ``set_rule_status`` / ``decide_rule`` —— 状态流转的**唯一入口**（非法流转报错）；
    持久化经 ``store.upsert_rule``/``update_rule``，本模块不自己写 SQL。

两条不可绕过的纪律（规格 §9.2）：

  1. **LLM 不写代码**：factors 只能引用已注册因子（``factors.REGISTRY``）；
     combine 只能是解释器支持的有限算子集（首版：zscore 等权 / IC 加权）。
  2. **演进条款的机械执行**：资讯/F10/做空域因子（未满 250 交易日）一律拒绝入规则——
     它们只能在研究文字里讨论，转正走同一套验证门（规格 §6.3）。

第三条在状态机上：``enabled`` 只能由 ``decide_rule`` 产生（记录批准人），
``set_rule_status`` 不可直通——Harness 不能自批自己挖的因子。
"""
from datetime import date, datetime, timedelta, timezone

from . import store

_TZ8 = timezone(timedelta(hours=8))

#: 规则 spec 的字段白名单（规格 §9.2；多一个字段都拒绝——协议是锁死的）
SPEC_FIELDS = ("rule_id", "hypothesis", "factors", "combine", "universe",
               "top_n", "rebalance", "provenance")

#: 合成算子白名单（首版两个；新算子 = 核心库 PR，不提供动态执行通道）
COMBINES = ("zscore_equal_weight", "ic_weighted")

#: 再平衡周期（首版三档）
REBALANCES = ("daily", "weekly", "monthly")

#: top_n 取值域（1..100：Top-N 组合的合理上界，超出多半是提案写错）
MAX_TOP_N = 100

#: 未成熟因子域前缀（资讯/F10/做空）：规格 §6.3 演进条款——攒满 250 交易日才可进规则
IMMATURE_FACTOR_PREFIXES = ("sentiment", "f10", "short")

#: 状态机（规格 §9.3）：candidate→validating→passed/failed→enabled/disabled
RULE_STATUSES = ("candidate", "validating", "passed", "failed", "enabled", "disabled")
RULE_TRANSITIONS = {
    "candidate": ("validating", "disabled"),
    "validating": ("passed", "failed"),
    "passed": ("disabled",),      # → enabled 只能经 decide_rule（记录批准人）
    "failed": ("disabled",),
    "enabled": ("disabled",),
    "disabled": (),               # 终态
}

#: 提案来源纪律：提案由 harness 产出，人只在批准环节出现（created_by 是自证字段）
PROPOSER = "harness"


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def _is_immature(name, immature):
    """域前缀判定 + 调用方显式集合（显式集合优先，供攒数期满后放行单只因子）。"""
    if name in immature:
        return True
    return any(name.startswith(prefix) for prefix in IMMATURE_FACTOR_PREFIXES)


def validate_spec(spec, registry=None, immature=frozenset()):
    """校验规则提案，返回 ``(ok, errors)``；errors 为中文可读的错误列表。

    ``registry`` 缺省用 ``factors.REGISTRY``（延迟导入，避免模块级耦合）；
    ``immature`` 是调用方补充的未成熟因子名集合（演进条款放行后由调用方指定）。
    """
    errors = []
    if not isinstance(spec, dict):
        return False, ["规则必须是对象（JSON object）"]

    for key in spec:
        if key not in SPEC_FIELDS:
            errors.append(f"未知字段 {key}（协议只接受：{'/'.join(SPEC_FIELDS)}）")
    for key in ("rule_id", "hypothesis", "combine", "universe", "rebalance", "provenance"):
        if key not in spec:
            errors.append(f"缺少必填字段 {key}")

    rule_id = spec.get("rule_id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        errors.append("rule_id 必须是非空字符串")

    hypothesis = spec.get("hypothesis")
    if not isinstance(hypothesis, str) or not hypothesis.strip():
        errors.append("hypothesis 必须是非空字符串（研究假设要写清人话）")

    if registry is None:
        from . import factors
        registry = factors.REGISTRY
    factors_list = spec.get("factors")
    if not isinstance(factors_list, list) or not factors_list:
        errors.append("factors 必须是非空数组")
    else:
        for name in factors_list:
            if not isinstance(name, str) or not name.strip():
                errors.append(f"factors 元素必须是非空字符串，收到 {name!r}")
            elif name not in registry:
                errors.append(f"因子未注册：{name}（LLM 不得引用未注册因子，算子需走核心库 PR）")
            elif _is_immature(name, immature):
                errors.append(
                    f"因子未满 250 交易日，不得进规则：{name}"
                    "（资讯/F10/做空域因子先攒 PIT 历史，转正走同一套验证门）")

    combine = spec.get("combine")
    if combine is not None and combine not in COMBINES:
        errors.append(f"combine 不在白名单：{combine!r}（允许：{'/'.join(COMBINES)}）")

    universe = spec.get("universe")
    if not isinstance(universe, str) or not universe.strip():
        errors.append("universe 必须是非空字符串（如 watchlist.SH）")

    top_n = spec.get("top_n")
    if isinstance(top_n, bool) or not isinstance(top_n, int):
        errors.append(f"top_n 必须是整数，收到 {top_n!r}")
    elif not 1 <= top_n <= MAX_TOP_N:
        errors.append(f"top_n 超出取值域 1..{MAX_TOP_N}：{top_n}")

    rebalance = spec.get("rebalance")
    if rebalance is not None and rebalance not in REBALANCES:
        errors.append(f"rebalance 不在白名单：{rebalance!r}（允许：{'/'.join(REBALANCES)}）")

    provenance = spec.get("provenance")
    if not isinstance(provenance, dict):
        errors.append("provenance 必须是对象（含 research_run_id/created_by/created_at）")
    else:
        for key in ("research_run_id", "created_by", "created_at"):
            if not isinstance(provenance.get(key), str) or not provenance.get(key, "").strip():
                errors.append(f"provenance.{key} 必须是非空字符串")
        created_by = provenance.get("created_by")
        if created_by is not None and created_by != PROPOSER:
            errors.append(
                f"provenance.created_by 必须是 {PROPOSER}，收到 {created_by!r}"
                "（提案产自 harness；人只在批准环节出现，不在提案里自证）")

    return (not errors), errors


# ---------------------------------------------------------------------------
# 任务 2：规则解释器（spec → Strategy 协议实例）
# ---------------------------------------------------------------------------
#: IC 加权的历史窗口与门槛（规格 §9.3）：不足门槛直接拒绝，不用退化权重兜底
IC_WINDOW_DAYS = 120
IC_MIN_DAYS = 30
IC_HORIZON_DAYS = 20

#: 横截面 z 与 RankIC 至少要 3 个样本才有定义（factors 既有口径）
MIN_CROSS_SECTION = 3


def _parse_universe(universe):
    """``universe`` 文本 → ``(pool_key, market)``。规则写死、不猜::

        "watchlist"      → ("watchlist", None)   缺省池全量
        "watchlist.SH"   → ("watchlist", "SH")   缺省池 + 市场链
        "pool2.HK"       → ("pool2", "HK")       命名池 + 市场链

    市场片段只认作业链市场（SH/HK/US；SZ/BJ 归 SH 链）。不认识的片段直接报错，
    **不当成池名的一部分静默吞掉**——宁可提案被拒，也不扫错池子下单。
    """
    from .planner import CHAIN_MARKETS
    if not isinstance(universe, str) or not universe.strip():
        raise ValueError("universe 必须是非空字符串")
    text = universe.strip()
    if "." not in text:
        return text, None
    pool, market = text.split(".", 1)
    if not pool:
        raise ValueError(f"universe 池名为空：{universe!r}")
    market = market.upper()
    if market not in CHAIN_MARKETS:
        raise ValueError(
            f"universe 市场片段未知：{market!r}（允许：{'/'.join(CHAIN_MARKETS)}）")
    return pool, market


def _period_key(rebalance, date_text):
    """再平衡周期键：``weekly`` → ``(ISO 年, 周)``；``monthly`` → ``"YYYY-MM"``。

    ``daily`` 返回 ``None``（每天都是新周期，调用方据此走「不沿用」分支）。
    """
    if rebalance == "daily":
        return None
    parsed = date.fromisoformat(str(date_text)[:10])
    if rebalance == "monthly":
        return parsed.strftime("%Y-%m")
    iso = parsed.isocalendar()
    return (iso[0], iso[1])


def _forward_return(conn, symbol, as_of, horizon, end):
    """``as_of`` 起 ``horizon`` 个交易日的前向收益（读库；只用到 ``end`` 为止的数据）。

    找不到 ``as_of`` 当根 bar，或其后不足 ``horizon`` 根 → ``None``
    （不猜、不外推：宁可该日不参与 IC，也不编造前向收益）。
    """
    bars = store.read_bars(conn, symbol, "1d", as_of=end, limit=400)
    for index, bar in enumerate(bars):
        if bar["t"] == as_of:
            future = index + horizon
            if future < len(bars):
                return bars[future]["c"] / bar["c"] - 1.0
            return None
    return None


def _ic_factor_weights(conn, factor_names, horizon=IC_HORIZON_DAYS, window=IC_WINDOW_DAYS,
                       min_days=IC_MIN_DAYS, end=None, snapshots=None, fwd_return=None):
    """各因子近期 ``|RankIC|`` 的归一权重（``combine=ic_weighted`` 的权重来源）。

    - 窗口：``factor_snapshots`` 最近 ``window`` 日；少于 ``min_days`` 日 → ``ValueError``
      （规格 §9.3：历史不够就不给 IC 加权，不用等权兜底冒充）；
    - ``end``：前向收益的数据上限，缺省取最新快照日；规则解释器传**评估日** ``as_of``
      （PIT：前向收益只能用到评估日为止的数据，最新快照自然算不出收益而被跳过）；
    - ``snapshots`` / ``fwd_return`` 是测试注入口（缺省读库）；
    - 任一因子在窗口内算不出 IC → ``ValueError``：**算不出预测力的因子不得靠等权
      兜底混进权重**，否则「IC 加权」名不副实。
    """
    loader = snapshots or (lambda: store.list_factor_snapshots(conn, limit=window))
    records = list(loader())
    if len(records) < min_days:
        raise ValueError(f"IC 加权需 ≥{min_days} 日历史（当前 {len(records)} 日）")
    forward = fwd_return or _forward_return
    end = end or records[0]["date"]
    from . import factors as factors_mod
    ics = {}
    for name in factor_names:
        series = []
        for record in records:
            tickers = (record.get("payload") or {}).get("tickers") or {}
            values = {symbol: row.get(name) for symbol, row in tickers.items()
                      if isinstance(row, dict) and row.get(name) is not None}
            if len(values) < MIN_CROSS_SECTION:
                continue
            returns = {symbol: forward(conn, symbol, record["date"], horizon, end)
                       for symbol in values}
            returns = {s: r for s, r in returns.items() if r is not None}
            if len(returns) < MIN_CROSS_SECTION:
                continue
            ic = factors_mod.rank_ic(values, returns)
            if ic is not None:
                series.append(ic)
        if not series:
            raise ValueError(f"IC 加权无可用历史 IC：{name}")
        ics[name] = abs(sum(series) / len(series))
    total = sum(ics.values())
    if total <= 0:
        raise ValueError("IC 加权总权重为 0（窗口内所有因子 |IC| 均为 0）")
    return {name: value / total for name, value in ics.items()}


class RuleStrategy:
    """声明式规则的 Strategy 协议适配器（规格 §9.2/§9.3）。

    与手工策略（``watchlist_rsi``/``momentum_value_top5``）共用同一注册表协议，
    因此 ``planner.plan_auto`` 按 ``rule_id`` 消费时**零特判**：

    - ``universe``：spec 的 ``universe`` 解析成池键 + 市场链，读取经 ``watchlist``
      唯一实现（缺省池不存在 = 合法空池；命名池不存在 = fail-closed）；
    - ``target_weights``：按 ``combine`` 合成打分 → 取 ``top_n`` → 权重经
      ``strategies.risk_capped_weights``（组合策略共用口径，本类不重写风控）；
    - 再平衡：``daily`` 每日重算；``weekly``/``monthly`` 在**同一周期内沿用上次目标**
      （kv ``rule:<rule_id>:last_weights``）——周中不因数据抖动换仓；
    - 缺值口径（**完整样本**）：任一因子缺值的标的整只跳过（宁缺毋假）；
      完整样本 <3（横截面 z 无定义）时返回 ``{}``（等价全现金，不硬凑权重）；
    - 市场一致性：``market`` 给定时必须与 spec 的 universe 市场一致，否则
      ``ValueError`` fail-closed——把 US 规则塞进 SH 计划的后果是拿错市场的标的
      下单，而 ``plan_auto`` 对 ``ValueError`` 的处理正是「软跳过 + 告警」。
    """

    def __init__(self, spec, registry=None):
        self.id = spec["rule_id"]
        self.spec = spec
        self._injected = registry

    def _factors(self):
        if self._injected is not None:
            return self._injected
        from . import factors
        return factors.REGISTRY

    def _weights_key(self):
        return f"rule:{self.id}:last_weights"

    def universe(self, conn, as_of, home=None, market=None):
        from . import watchlist as watchlist_mod
        pool, spec_market = _parse_universe(self.spec["universe"])
        if market is not None and spec_market is not None and str(market).upper() != spec_market:
            raise ValueError(
                f"规则 universe 市场与作业市场不一致：{spec_market} vs {str(market).upper()}"
                "（fail-closed，不拿错市场的标的下单）")
        target_market = spec_market if market is None else str(market).upper()
        return watchlist_mod.watchlist_symbols(
            home, key=pool, market=target_market,
            strict=pool != watchlist_mod.DEFAULT_POOL_KEY)

    def target_weights(self, conn, as_of, home=None, market=None):
        from . import strategies as strategies_mod
        rebalance = self.spec.get("rebalance") or "weekly"
        cached = self._cached_weights(conn, as_of, rebalance)
        if cached is not None:
            return cached
        selected = self._select(conn, as_of,
                                self.universe(conn, as_of, home=home, market=market))
        weights = strategies_mod.risk_capped_weights(selected, home=home)
        store.kv_set(conn, self._weights_key(), {"date": str(as_of)[:10], "weights": weights})
        return weights

    def _cached_weights(self, conn, as_of, rebalance):
        """同周期沿用上次目标（返回副本，调用方改动不污染 kv）。"""
        if rebalance == "daily":
            return None
        last = store.kv_get(conn, self._weights_key())
        if not isinstance(last, dict) or not last.get("date"):
            return None  # 无记录 = 当日视为再平衡日
        if _period_key(rebalance, last["date"]) == _period_key(rebalance, as_of):
            return dict(last.get("weights") or {})
        return None

    def _select(self, conn, as_of, symbols):
        """打分取优：完整样本 → 逐因子横截面 z → 按 combine 权重合成 → top_n。

        返回**按打分降序**的标的列表（顺序即 ``risk_capped_weights`` 的截断优先级：
        截断时保留最优标的，与 ``watchlist_rsi`` 的代码升序不同，因为本类有打分）。
        """
        registry = self._factors()
        names = list(self.spec["factors"])
        per = {}
        for symbol in symbols:
            values = {}
            for name in names:
                try:
                    values[name] = registry[name](conn, symbol, as_of)
                except Exception:  # noqa: BLE001 —— 单因子异常按缺值处理，不中止整轮
                    values[name] = None
            if all(values[name] is not None for name in names):
                per[symbol] = values  # 完整样本口径：缺任一因子整只跳过
        if len(per) < MIN_CROSS_SECTION:
            return []
        if (self.spec.get("combine") or "zscore_equal_weight") == "ic_weighted":
            fused = _ic_factor_weights(conn, names, end=as_of)
        else:
            fused = {name: 1.0 / len(names) for name in names}
        from . import factors as factors_mod
        zs = {}
        for name in names:
            raw = {symbol: per[symbol][name] for symbol in per}
            for symbol, z in factors_mod.cross_sectional_zscore(raw).items():
                if z is not None:
                    zs.setdefault(symbol, {})[name] = z
        denominator = sum(fused[name] for name in names)
        scored = {}
        for symbol, values in zs.items():
            if len(values) != len(names):
                continue  # 去极值后仍缺项：不打分（宁缺毋假）
            scored[symbol] = sum(fused[name] * values[name] for name in names) / denominator
        ranked = sorted(scored, key=scored.get, reverse=True)
        return ranked[:int(self.spec["top_n"])]


def load_rule(spec, registry=None):
    """校验并构造规则实例（``registry`` = 因子名→函数的注入口，缺省 ``factors.REGISTRY``）。

    **构造前二次校验**：CLI、审批端点、动态注册三条路径都可能拿到被改过的 spec，
    这里不信任上游、再校验一次（fail-closed）。非法 spec 抛 ``ValueError``，
    调用方（``plan_auto``/作业入口）按既有口径软跳过并告警。
    """
    ok, errors = validate_spec(spec, registry=registry)
    if not ok:
        raise ValueError("规则校验失败：" + "；".join(errors))
    _parse_universe(spec["universe"])  # fail-fast：非法市场片段在注册/加载时就拒绝
    return RuleStrategy(spec, registry=registry)


def set_rule_status(conn, rule_id, status, validation=None):
    """状态流转（校验后落库）。``enabled`` 不可经此直通——那只属于 decide_rule。"""
    if status not in RULE_STATUSES:
        raise ValueError(f"未知规则状态 {status!r}（允许：{'/'.join(RULE_STATUSES)}）")
    if status == "enabled":
        raise ValueError("启用必须经 decide_rule 记录批准人（set_rule_status 不可直通）")
    current = store.get_rule(conn, rule_id)["status"]
    if status not in RULE_TRANSITIONS[current]:
        raise ValueError(
            f"非法状态流转：{current} → {status}（当前状态允许："
            f"{'/'.join(RULE_TRANSITIONS[current]) or '无（终态）'}）")
    store.update_rule(conn, rule_id, status=status, validation=validation)
    return status


def decide_rule(conn, rule_id, decision, by):
    """人工批准/停用（Web 端点唯一入口；``by`` 记录批准来源）。

    enable：仅 ``passed`` 可启用 → ``enabled`` + approved_at/approved_by；
    disable：candidate/failed/passed/enabled → ``disabled`` 留档（批准痕迹保留）。
    """
    if decision not in ("enable", "disable"):
        raise ValueError(f"未知决定 {decision!r}（允许：enable/disable）")
    if not isinstance(by, str) or not by.strip():
        raise ValueError("decide_rule 必须记录操作来源 by（如 web）")
    current = store.get_rule(conn, rule_id)["status"]
    if decision == "enable":
        if current != "passed":
            raise ValueError(f"仅通过验证的规则可启用：当前 {current}（需 passed）")
        store.update_rule(conn, rule_id, status="enabled",
                          approved_at=_now(), approved_by=by)
        return "enabled"
    if current not in ("candidate", "failed", "passed", "enabled"):
        raise ValueError(f"当前状态不可停用：{current}")
    store.update_rule(conn, rule_id, status="disabled")
    return "disabled"
