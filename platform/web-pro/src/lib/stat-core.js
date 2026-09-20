// 「数字诚实性」三态口径（纯函数，无 React 依赖 —— 便于 `node --test` 直测）。
//
// 为什么必须有这一层（实测证据，不是风格偏好）：
//   * antd `Statistic` 的 `value` 默认值是 **0**
//     （`node_modules/antd/es/statistic/Statistic.js`：`value = 0` 解构默认），
//     所以 `value={undefined}` 会被渲染成 **0**；
//   * `value={null}` 走 `String(value)`（`node_modules/antd/es/statistic/Number.js`），
//     不匹配数字正则 → 原样渲染字面量 **null**（带 `suffix="%"` 就是 `null%`）。
//
// 于是「还没取到（loading）」「取失败（error）」「接口没这个字段（missing）」三种情况
// 绝不能用 0 / null 顶替：一律落到 `—`，并把**原因**交给页面显示；
// 只有接口**确实返回了 0** 时才显示 0（真 0 与「没有数据」必须可区分）。
//
// 判定规则（页面统一口径）：
//   loading  → 显示 `—`，原因「加载中…」
//   error    → 显示 `—`，原因「取数失败：<真实错误原文>」
//   missing  → 显示 `—`，原因由调用方给出（字段名 + 为什么没有）
//   value    → 显示接口返回值（含 0）
export const STAT = {
  LOADING: "loading",
  ERROR: "error",
  MISSING: "missing",
  VALUE: "value",
};

/** 空值判定：null / undefined / 空串 才算「没有数据」（0 与 false 是有数据的真实值）。 */
export function isMissingValue(value) {
  return value === null || value === undefined || value === "";
}

/**
 * 把「取数状态 + 字段值」归一成四态之一。
 *
 * @param env   useV3 的返回对象（读 `.loading` / `.error`），可为 null
 * @param value 待展示字段值
 * @param opts.missingReason 字段缺失时的原因（默认「接口未返回该字段」）
 * @returns {{state: string, value: *, reason: string|null}}
 */
export function statState(env, value, { missingReason = "接口未返回该字段" } = {}) {
  if (!env || env.loading) {
    return { state: STAT.LOADING, value: null, reason: "加载中…" };
  }
  if (env.error) {
    return { state: STAT.ERROR, value: null, reason: `取数失败：${env.error}` };
  }
  if (isMissingValue(value)) {
    return { state: STAT.MISSING, value: null, reason: missingReason };
  }
  return { state: STAT.VALUE, value, reason: null };
}

/**
 * antd `Statistic` 的 `value`：只有拿到真实读数才给数字/数值字符串，
 * 其余三态一律 `—`（**绝不能**给 undefined/null，否则会被渲染成 0 / null）。
 */
export function statText(state) {
  if (!state || state.state !== STAT.VALUE) return "—";
  const numeric = Number(state.value);
  if (Number.isFinite(numeric)) return numeric;
  return String(state.value);
}

/** 「—」时的原因（加载中 / 取数失败 / 字段缺失）；有真实读数时为 null。 */
export function statNote(state) {
  return !state || state.state === STAT.VALUE ? null : state.reason;
}

/**
 * 汇总若干 `[标签, 状态]` 里所有非真实读数的原因，页面挂一行「为什么是 —」。
 * 全部是真实读数时返回空数组（页面不渲染该行）。
 */
export function statNotes(entries) {
  return (entries ?? [])
    .filter(([, state]) => state && state.state !== STAT.VALUE)
    .map(([label, state]) => `${label}：${state.reason}`);
}
