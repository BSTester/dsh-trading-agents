"""声明式规则协议与状态机（规格 §9.2/§9.3）。

职责边界（与 store 的分工）：

  * ``validate_spec`` —— 协议校验（字段白名单/取值域/因子注册与成熟度/来源纪律），
    一次返回全部问题（提案方一次修完，不挤牙膏）；**纯函数，不触库**；
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
from datetime import datetime, timedelta, timezone

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
