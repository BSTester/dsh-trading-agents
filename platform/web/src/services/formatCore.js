// 展示格式化的**纯函数内核**（无 React / 无 antd / 无 JSX）。
//
// 为什么与 format.jsx 分开：format.jsx 里 `maskedAccount` 需要 antd Tooltip，于是整份文件
// 带 JSX，node --test 与 `scripts/audit_page_fields.mjs` 都无法直接加载它——可移植的纯格式化
// 只能靠「在测试里抄一份」来验，抄一份就必然漂移。这里把三个纯字符串函数抽出来，format.jsx
// 原样再导出（**对外 API 与行为零变化**），于是：
//   * `platform/web/tests/formatCore.test.mjs` 直接钉住口径；
//   * 字段审计工具 `scripts/audit_page_fields.mjs` 用**同一份实现**算出「后端值应显示成什么」，
//     再与浏览器里真实渲染的文本比对——不是工具另抄一份契约。
//
// 口径（2026-09-19 与任务书 §可判定口径逐条对齐）：
//   num(value, digits)      —— null/undefined/"" → "—"；有限数 → zh-CN 千分位、**最多** digits
//                              位小数；非有限（NaN/Infinity/非数字字符串）→ 原样字符串。
//   pctOf(ratio, digits)    —— **比例 ×100** 显示百分数（0.0123 → "1.23%"）；非有限 → "—"。
//   stampOf(value)          —— ISO 的 "T" → 空格、截到 19 位；缺失 → "—"。

/** 缺失值占位（全站一致；与 f10.js 的 MISSING、各页面字面量 "—" 同源）。 */
export const MISSING = "—";

/** 数值格式化：null/undefined/空串显示 —；非有限数值原样字符串；其余按中文环境千分位。 */
export function numText(value, digits = 2) {
  if (value === null || value === undefined || value === "") return MISSING;
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? parsed.toLocaleString("zh-CN", { maximumFractionDigits: digits })
    : String(value);
}

/** 比例 → 百分数显示（纯单位换算，非指标计算）：0.0123 → 1.23%。 */
export function pctOfText(ratio, digits = 2) {
  const parsed = Number(ratio);
  if (ratio === null || ratio === undefined || !Number.isFinite(parsed)) return MISSING;
  return `${(parsed * 100).toFixed(digits)}%`;
}

/** 秒级时间戳统一展示：ISO 的 T 分隔 → 与库内时间一致的空格分隔；缺失显示 —。 */
export function stampText(value) {
  return value ? String(value).replace("T", " ").slice(0, 19) : MISSING;
}

/** 毫秒时间戳的下界（≈1973-03-03）：低于它的数字不当作时间戳，避免把「12345」当时间。 */
const MS_EPOCH_MIN = 1e11;
/** 微秒时间戳的下界（≈1973 年的微秒数）：券商 OpenAPI 的 create_time/update_time 实测是微秒整数。 */
const US_EPOCH_MIN = 1e14;

/**
 * 毫秒时间戳（富途资金流 `capital_flow_item_time` 等实测是 int64 毫秒）→ Date；
 * 非时间戳（缺失/字符串时间/秒级小数字）返回 null，由调用方按「原样字符串」兜底。
 */
function epochDate(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  if (!Number.isFinite(number)) return null;
  // 微秒（券商 OpenAPI 的 create_time 实测形如 "1789363901000000"）先降到毫秒
  const ms = Math.abs(number) >= US_EPOCH_MIN ? number / 1000
    : Math.abs(number) >= MS_EPOCH_MIN ? number : null;
  if (ms === null) return null;
  const date = new Date(ms);
  return Number.isNaN(date.getTime()) ? null : date;
}

function pad2(number) {
  return String(number).padStart(2, "0");
}

/**
 * **时刻**展示：毫秒时间戳 → `HH:mm`（本地时区）；不是时间戳时退回原字符串的前 16 位
 * （`2026-09-18 09:30:00` → `2026-09-18 09:30`），缺失 → `—`。
 *
 * 为什么必须转：上游部分字段是 int64 毫秒（实测 `capital_flow_item_time` =
 * 1789695000000），直接插值会渲染出 13 位整数——「时间」列看起来像一串编号。
 */
export function clockText(value) {
  if (value === null || value === undefined || value === "") return MISSING;
  const date = epochDate(value);
  if (!date) return String(value).slice(0, 16);
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

/**
 * **日期**展示：毫秒时间戳 → `YYYY-MM-DD`（本地时区）；不是时间戳时退回原字符串的前 10 位
 * （`2026-09-18 00:00:00` → `2026-09-18`），缺失 → `—`。
 *
 * 与 `clockText` 同一判据：页面原来对同一字段做 `.slice(0, 10)`，在毫秒时间戳上得到的是
 * `1786291200` 这种 10 位假日期。
 */
export function dayText(value) {
  if (value === null || value === undefined || value === "") return MISSING;
  const date = epochDate(value);
  if (!date) return String(value).slice(0, 10);
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

/**
 * **分钟精度**时刻展示（执行页 OpenAPI 订单/成交表的「更新时间」列）：毫秒或微秒时间戳 →
 * `YYYY-MM-DD HH:mm`；ISO 串 → T 换空格、截 16 位；缺失 → `—`。
 *
 * 为什么必须转：模拟盘（sim）与部分券商通道给的 `create_time`/`update_time` 是**微秒整数**
 * （实测 `"1789363901000000"`），页面原来做 `.slice(0, 16)` 直接把它当文本截断 → 「更新时间」
 * 列显示 16 位数字（1789363901000000），看时间的人无法读出任何时刻。
 */
export function minuteText(value) {
  if (value === null || value === undefined || value === "") return MISSING;
  const date = epochDate(value);
  if (!date) return String(value).replace("T", " ").slice(0, 16);
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} `
    + `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}
