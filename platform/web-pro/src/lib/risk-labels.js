// 风控口径的展示标签（纯函数，无 React 依赖 —— 便于 `node --test` 直测）。
//
// 为什么单列一层：`stage` / `risk.rule` / `metrics.industryGate` 都是**后端契约**，
// 前端的正确行为必须跟着契约走，而不是各自写一套字符串比较。已发生的真实缺陷：
//   * 后端新增第三个风控态 `stage="blocked_industry"`（行业红线硬阻断）后，
//     页面按 `stage === "blocked"` 字面量判色，`blocked_industry` 落成**灰色**，
//     与「行业超限被强制阻断」的事实相反；
//   * `risk.rule` 的枚举值（`industry-red-line` / `drawdown-red-line` / `single-order` /
//     `within-limits`）直接上屏是英文机器码，人读不懂；
//   * 行业读数 `industry_source="no-data"` 时页面显示 0%，等于把「没有读数」说成
//     「暴露为 0」（后端明确要求 fail-open 且写明原因）。
//
// 契约来源（只读核对，2026-09-20）：`platform/server/v3_ops.py` 的
// `STAGES` / `_RULE_OF_ACTION` / `OmsLedger.gate_view()` 与 `platform/server/v3_risk_gate.py`。

/** 台账阶段 → 中文标签 + 颜色（antd Tag color）。 */
export const STAGE_LABELS = {
  risk_passed: { text: "风控通过", color: "green" },
  manual: { text: "待审批", color: "gold" },
  blocked: { text: "回撤红线阻断", color: "red" },
  blocked_industry: { text: "行业红线阻断", color: "red" },
  submitted: { text: "已提交", color: "blue" },
  partial: { text: "部分成交", color: "cyan" },
  filled: { text: "全部成交", color: "green" },
  rejected: { text: "已拒绝", color: "red" },
};

/** 阶段标签：未知阶段原样显示，不猜语义。 */
export function stageLabel(stage) {
  const key = String(stage ?? "");
  if (!key) return "未知";
  return STAGE_LABELS[key]?.text ?? key;
}

/** 阶段颜色：**阻断类阶段一律红色**（`blocked` 与 `blocked_industry` 同等对待）。 */
export function stageTone(stage) {
  const key = String(stage ?? "");
  return STAGE_LABELS[key]?.color ?? (key.includes("blocked") ? "red" : "blue");
}

/** 阶段是否属于「硬阻断」（回撤红线 / 行业红线）。 */
export function isBlockedStage(stage) {
  const key = String(stage ?? "");
  return key === "blocked" || key === "blocked_industry";
}

/** `risk.rule`（后端机器码）→ 人话。 */
export const RULE_LABELS = {
  "industry-red-line": "行业红线",
  "drawdown-red-line": "回撤红线",
  "single-order": "单笔超限",
  "within-limits": "阈值内",
};

/** 规则标签：未知规则原样显示（不把机器码翻译成没根据的中文）。 */
export function ruleLabel(rule) {
  const key = String(rule ?? "");
  if (!key) return null;
  return RULE_LABELS[key] ?? key;
}

/** 探测年龄（毫秒）→ 人话；非数值返回 `—`（不猜时刻）。 */
export function probeAgeText(ms) {
  const value = Number(ms);
  if (ms === null || ms === undefined || ms === "" || !Number.isFinite(value) || value < 0) return "—";
  if (value < 1000) return `${Math.round(value)}ms`;
  if (value < 60000) return `${Math.round(value / 1000)}s`;
  return `${Math.round(value / 60000)}min`;
}

/**
 * 把 `/api/v3/metrics` 的 `industryGate` 归一成页面可渲染的读数。
 *
 * 返回：
 *   `{ available:false, reason }` —— 本服务进程未返回该块（旧进程/取数失败）
 *   `{ available:true, hasReading:false, failOpen:true, source, reason, limitPct }`
 *        —— `industryPct=null`：**没有读数**，闸门 fail-open 不阻断，绝不能显示 0%
 *   `{ available:true, hasReading:true, failOpen:false, pct, top, source, asOf,
 *      probeAgeMs, missing, limitPct, breach }`
 */
export function industryGateView(gate) {
  if (!gate || typeof gate !== "object") {
    return {
      available: false,
      hasReading: false,
      failOpen: null,
      reason: "/api/v3/metrics 未返回 industryGate（本服务进程可能早于行业闸门改动，或该端点取数失败）",
    };
  }
  const limitPct = Number.isFinite(Number(gate.industryLimitPct)) ? Number(gate.industryLimitPct) : null;
  const raw = gate.industryPct;
  const hasReading = raw !== null && raw !== undefined && raw !== "" && Number.isFinite(Number(raw));
  const source = gate.industrySource ? String(gate.industrySource) : null;
  if (!hasReading) {
    return {
      available: true,
      hasReading: false,
      failOpen: true,
      limitPct,
      source: source ?? "no-data",
      top: gate.industryTop ?? null,
      asOf: gate.industryAsOf ?? null,
      probeAgeMs: gate.industryProbeAgeMs ?? null,
      missing: gate.industryMissing ?? null,
      breach: false,
      reason: `无新鲜行业读数（industry_source=${source ?? "no-data"}）→ 行业红线**未参与阻断**（fail-open），不是「暴露 0%」`,
    };
  }
  const pct = Number(raw);
  return {
    available: true,
    hasReading: true,
    failOpen: false,
    pct,
    top: gate.industryTop ?? null,
    source,
    asOf: gate.industryAsOf ?? null,
    probeAgeMs: gate.industryProbeAgeMs ?? null,
    missing: gate.industryMissing ?? null,
    limitPct,
    breach: limitPct !== null ? pct > limitPct : null,
    reason: null,
  };
}

/* ── 台账订单的行业读数（逐单，不是组合读数） ────────────────────────────────
 *
 * 与 `industryGateView`（组合级 `/api/v3/metrics.industryGate`）分开：这里是
 * `/api/v3/oms/orders.orders[]` 的**逐单**字段。已发生的真实缺陷是——闸门上线前落盘的
 * 10 笔存量单带着 `industry_pct: 0.0` +
 * `industry_source: "no-data（工具面无行业分类数据源，按 0% 不阻断；行业红线需人工核对）"`，
 * 页面直接 `fmt.pct(industry_pct)` 就把「当时没有行业读数」显示成「行业暴露 0.00%」，
 * 并把一句**已失效的断言**（当时行业分类数据源确实缺，现在 `futu/info_owner_plate`
 * 已接通，实测 SH 37.5% / HK 40% / US 50%）当成现状展示。
 *
 * 后端（`platform/server/v3_ops.py` 的 `order_view` / `legacy_pre_gate`）已在**返回视图**
 * 里把它归一化成 `industry_pct=null` + `legacy_pre_gate=true` + 如实文案。前端这里：
 *   1. 认后端标记（首选）；
 *   2. 后端尚未归一化（旧进程 / 旧构建）时按**那句已失效的断言**兜底识别——只有闸门前的
 *      实现会产生它，当前实现不可能；判据与后端同源，不猜时间戳。
 * 无论走哪条路径，**都不许**把闸门前的记录渲染成「0.00%」。
 */

/** 闸门前 `industry_source` 的原文（后端 `v3_ops.LEGACY_INDUSTRY_SOURCE_TEXT`）。 */
export const RETIRED_INDUSTRY_SOURCE_TEXT = "工具面无行业分类数据源";
/** 历史判定的如实文案（后端 `v3_ops.LEGACY_INDUSTRY_SOURCE`）。 */
export const LEGACY_INDUSTRY_SOURCE = "历史判定（该单登记于行业闸门上线前，当时无行业读数）";
/** 历史判定的说明（后端 `v3_ops.LEGACY_INDUSTRY_NOTE` 的前端等价文案）。 */
export const LEGACY_INDUSTRY_NOTE =
  "该判定未包含行业红线：登记时行业闸门尚未接入，台账没有行业读数——这里的「—」是「当时没读到」，不是「行业暴露 0%」。原始 risk.reasons / history 原样保留（未回写、未重判）。";

const stampText = (value) => (value ? String(value).replace("T", " ").slice(0, 19) : "—");

/**
 * 台账订单 → 页面可渲染的行业读数（纯函数）。
 *
 * 返回 `{ present, legacyPreGate, graded, hasReading, pct, source, asOf, probeAgeMs,
 *        text, sourceLine, note }`：
 *   * `legacyPreGate`（闸门前的历史判定）→ `text="行业暴露 — · 历史判定（未含行业红线）"`，
 *     **绝不**显示读数（当时没有读数，`0.0` 不是「暴露 0%」）；
 *   * `hasReading`（真实读数）→ `text="行业暴露 37.50%"` + 来源 + as_of + 探测年龄；
 *   * `industry_pct === null`（闸门后但无新鲜读数）→ `text="行业暴露 — · 未参与阻断（fail-open）"`；
 *   * 台账压根没有 `industry_*` 字段 → 如实说「台账未记录行业读数」，
 *     **不**替它下 fail-open / 历史判定的结论。
 */
export function orderIndustryView(order) {
  if (!order || typeof order !== "object") {
    return {
      present: false, legacyPreGate: null, graded: null, hasReading: false, pct: null,
      source: null, asOf: null, probeAgeMs: null,
      text: "行业暴露 —", sourceLine: "台账未返回该订单", note: null,
    };
  }
  const raw = order.industry_pct;
  const hasReading = raw !== null && raw !== undefined && raw !== ""
    && Number.isFinite(Number(raw));
  const legacyPreGate = order.legacy_pre_gate === true
    || String(order.industry_source || "").includes(RETIRED_INDUSTRY_SOURCE_TEXT);
  const source = order.industry_source ? String(order.industry_source) : null;
  const asOf = order.industry_as_of ?? null;
  const probeAgeMs = order.industry_probe_age_ms ?? null;

  if (legacyPreGate) {
    return {
      present: true, legacyPreGate: true, graded: false, hasReading: false,
      pct: null, source: LEGACY_INDUSTRY_SOURCE, asOf: null, probeAgeMs: null,
      text: "行业暴露 — · 历史判定（未含行业红线）",
      sourceLine: `来源 ${LEGACY_INDUSTRY_SOURCE}`,
      note: order.industry_note ? String(order.industry_note) : LEGACY_INDUSTRY_NOTE,
    };
  }
  const graded = order.industry_graded !== false;
  if (hasReading) {
    const pct = Number(raw);
    return {
      present: true, legacyPreGate: false, graded, hasReading: true,
      pct, source, asOf, probeAgeMs,
      text: `行业暴露 ${pct.toFixed(2)}%`,
      sourceLine: `来源 ${source || "—"}${asOf ? ` · as_of ${stampText(asOf)}` : ""}` +
        ` · 探测年龄 ${probeAgeText(probeAgeMs)}`,
      note: null,
    };
  }
  if (raw === null) {
    return {
      present: true, legacyPreGate: false, graded, hasReading: false,
      pct: null, source, asOf, probeAgeMs,
      text: "行业暴露 — · 未参与阻断（fail-open）",
      sourceLine: `来源 ${source || "no-data"}${asOf ? ` · as_of ${stampText(asOf)}` : ""}`,
      note: "无新鲜行业读数：行业红线**未参与阻断**（fail-open），不是「行业暴露 0%」。",
    };
  }
  return {
    present: true, legacyPreGate: false, graded, hasReading: false,
    pct: null, source, asOf, probeAgeMs,
    text: "行业暴露 — · 台账未记录行业读数",
    sourceLine: source ? `来源 ${source}` : "台账该单没有 industry_* 字段",
    note: "该记录没有行业读数字段，无从判断是否经过行业闸门——不回填、不猜。",
  };
}
