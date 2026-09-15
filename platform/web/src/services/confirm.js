// 待确认实盘操作的纯逻辑（倒计时/按钮禁用/摘要行）。无浏览器依赖，node --test 直测。
// 锚点：服务端 store_access.confirmation_view 字段（id/at/expires_at/operation/tool/
// summary.fields）与 CONFIRM_TTL_MS=120s 的 fail-closed 语义（超时按拒绝）。
// 页面侧只是同一规则的提前判定：服务端 confirm-decide 仍会独立复核，编号不匹配、
// 已超时、重复决定一律拒绝——这里的禁用只是不把用户往必然失败的操作上引。

/** 服务端确认 TTL（毫秒），与 store_access.CONFIRM_TTL_MS 同值，改动必须两处同步。 */
export const CONFIRM_TTL_MS = 120_000;

/**
 * 剩余存活秒数：正数=未过期；0=已到期（夹到 0，绝不返回负数）；
 * null=到期时间缺失/不可解析（未知）。半秒向上取整：显示「还剩 1 秒」时请求仍可能被受理。
 * @param {string|undefined|null} expiresAt ISO 时间（confirmation_view.expires_at）
 * @param {number} nowMs 当前毫秒时戳
 * @returns {number|null}
 */
export function remainingSeconds(expiresAt, nowMs) {
  const at = Date.parse(String(expiresAt ?? ""));
  if (Number.isNaN(at)) return null;
  return Math.max(0, Math.ceil((at - nowMs) / 1000));
}

/**
 * 批准/拒绝按钮的禁用条件：无待确认、缺编号、已过期或到期时间未知一律禁用。
 * @param {{id?: string, expires_at?: string}|null} pending confirmation_view 的 pending
 * @param {number} nowMs
 * @returns {boolean}
 */
export function decideDisabled(pending, nowMs) {
  if (!pending || typeof pending.id !== "string" || !pending.id) return true;
  const seconds = remainingSeconds(pending.expires_at, nowMs);
  return seconds === null || seconds <= 0;
}

/**
 * 订单摘要 → 逐行「标签：值」；缺失字段如实显示 —（与执行页「只归纳、不推测」同口径）。
 * @param {{fields?: {label?: string, value?: string}[]}|null|undefined} summary
 * @returns {string[]}
 */
export function summaryLines(summary) {
  const fields = Array.isArray(summary?.fields) ? summary.fields : [];
  return fields.map((field) => `${field?.label ?? "—"}：${field?.value ?? "—"}`);
}
