// 规则候选池的展示派生（WP14 任务 4）：纯函数，宿主无关，node --test 直测。
//
// 数据来源：服务端 ``rules`` 端点（只读）＝ ``trading_core`` CLI 的 rules-list 输出：
//   {rules: [{rule_id, hypothesis, factors, combine, universe, top_n, rebalance,
//             provenance, status, validation, approved_by, approved_at, created_at}]}
// status 枚举的单一事实源是 ``rule_engine.RULE_STATUSES``（candidate/validating/passed/
// failed/enabled/disabled）——标签是展示，**未知状态原样回显不猜档位**（与仓库标签口径一致：
// 机器码保留、标签只做翻译）。
//
// 批准按钮的可用性：服务端 ``rule_engine.decide_rule`` 只在 ``passed`` 放行 enable，
// 可停用集为 candidate/failed/passed/enabled（与 RULE_TRANSITIONS 一致）。页面侧提前
// 判定只是**不把用户引向必然失败的操作**——服务端仍会独立复核（与 services/confirm.js
// 的禁用口径同一理由）。

export const RULE_STATUS = {
  candidate: { label: "候选", color: "default" },
  validating: { label: "验证中", color: "processing" },
  passed: { label: "已通过", color: "green" },
  failed: { label: "未通过", color: "red" },
  enabled: { label: "已启用", color: "blue" },
  disabled: { label: "已停用", color: "default" },
};

/** 状态中文标签；未知状态原样回显（不猜档位、不吞值）。 */
export function statusLabel(status) {
  if (typeof status !== "string" || !status) return "—";
  return RULE_STATUS[status]?.label ?? status;
}

/** 状态徽章色（antd Tag color）；未知状态用中性色。 */
export function statusColor(status) {
  return RULE_STATUS[status]?.color ?? "default";
}

/** enable 可用性：仅 ``passed``（服务端唯一放行条件）。 */
export function canEnable(rule) {
  return rule?.status === "passed";
}

/** disable 可用性：服务端可停用集（candidate/failed/passed/enabled）。 */
export function canDisable(rule) {
  return ["candidate", "failed", "passed", "enabled"].includes(rule?.status);
}

/**
 * 批准/停用按钮的禁用原因（null=可用）。不可用的**原因**要能显示给用户，
 * 否则按钮灰着却不知为何（研究页的操作反馈白名单允许「下一步动作」）。
 */
export function decideDisabledReason(rule, decision) {
  if (!rule || typeof rule.rule_id !== "string" || !rule.rule_id) return "规则缺少 rule_id";
  if (decision === "enable") {
    if (rule.status === "enabled") return "已启用";
    if (!canEnable(rule)) return `仅「已通过」的规则可启用（当前：${statusLabel(rule.status)}）`;
    return null;
  }
  if (decision === "disable") {
    if (!canDisable(rule)) return `当前状态不可停用（${statusLabel(rule.status)}）`;
    return null;
  }
  return `未知决定 ${decision}`;
}

/**
 * 样本外（walk-forward OOS）状态一句话摘要。
 *
 * 为什么必须显示：``passed`` 只代表机械门槛（IC t 检验 + 分层单调）通过，**样本外是否
 * 跑过是另一件事**——``validation.walkforward.status="not_run"`` 时批准人若只看到
 * 「通过」，会误以为已经过 OOS 验证。状态语义来自 ``cli._walkforward_summary``：
 * not_run（未请求，如未给 --start/--end）/ failed（跑不通）/ no_folds（数据窗口不足未
 * 产出折）/ ok（已跑 N 折）。未知状态原样回显，不猜档位。
 * @param {object|null|undefined} walkforward ``validation.walkforward`` 记录本身
 * @returns {string}
 */
export function walkforwardSummary(walkforward) {
  if (!walkforward || typeof walkforward !== "object" || !walkforward.status) {
    return "样本外：未请求";
  }
  const reason = typeof walkforward.reason === "string" && walkforward.reason
    ? `——${walkforward.reason}` : "";
  if (walkforward.status === "ok") {
    const folds = typeof walkforward.folds === "number" && !Number.isNaN(walkforward.folds)
      ? `已跑 ${walkforward.folds} 折` : "已跑（折数未记录）";
    return `样本外：${folds}`;
  }
  if (walkforward.status === "not_run") return `样本外：未请求${reason}`;
  if (walkforward.status === "failed") return `样本外：未跑通${reason}`;
  if (walkforward.status === "no_folds") return `样本外：未产出 OOS 折${reason}`;
  return `样本外：${walkforward.status}`;
}

/**
 * 验证报告一句话摘要：未验证 → 如实说明；通过 → 关键统计；未通过 → 逐条原因；
 * 末段固定附样本外状态（``walkforwardSummary``）——批准人必须同时看到机械门槛结论
 * 与样本外是否跑过。字段来自 ``factors.ic_report`` + ``passes_gate``。
 * @param {{validation?: object|null, status?: string}} rule
 * @returns {string}
 */
export function validationSummary(rule) {
  const validation = rule?.validation;
  if (!validation) return "尚未验证（先跑 rules-validate）";
  const reasons = Array.isArray(validation.gate_reasons) ? validation.gate_reasons : [];
  let gate;
  if (validation.passed) {
    const factors = validation.factors ?? {};
    const stats = Object.entries(factors)
      .map(([name, report]) => `${name}(t=${fmt(report?.t_stat)}, n=${fmt(report?.n, 0)})`)
      .join("、");
    gate = `通过${stats ? `：${stats}` : ""}`;
  } else if (reasons.length === 0) {
    gate = "未通过（未记录原因）";
  } else {
    gate = `未通过：${reasons.join("；")}`;
  }
  return `${gate}；${walkforwardSummary(validation.walkforward)}`;
}

function fmt(value, digits = 2) {
  if (typeof value !== "number" || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

/** 规则参数行（因子/合成/池/组合参数），供表格展开区展示。 */
export function ruleRows(rule) {
  const spec = rule ?? {};
  const provenance = spec.provenance ?? {};
  return [
    { label: "假设", value: spec.hypothesis ?? "—" },
    { label: "因子", value: (spec.factors ?? []).join("、") || "—" },
    { label: "合成", value: spec.combine ?? "—" },
    { label: "池 / 市场", value: spec.universe ?? "—" },
    { label: "Top-N / 再平衡", value: `${spec.top_n ?? "—"} / ${spec.rebalance ?? "—"}` },
    { label: "研究来源", value: provenance.research_run_id ?? "—" },
    { label: "样本外", value: walkforwardSummary(spec.validation?.walkforward) },
    { label: "批准", value: spec.approved_by ? `${spec.approved_by} @ ${spec.approved_at ?? "—"}` : "—" },
    { label: "创建时间", value: spec.created_at ?? "—" },
  ];
}

/** 列表行（表格 dataSource）：原样透传 + 稳定 rowKey（rule_id 缺省时按序补位）。 */
export function ruleRowsForTable(rules) {
  return (Array.isArray(rules) ? rules : []).map((rule, index) => ({
    ...rule, key: rule?.rule_id ?? `row-${index}`,
  }));
}
