// 图表主题色读取 + 容器内联样式。移植自 plugins/workbench/src/client.js：
// themeColors L301-314（canvas 取色逻辑原样）；CHART_STYLE/CHART_STYLE_SMALL 为
// 原 .tw-chart / .tw-chart.small CSS（client.js L63-64）的内联等价（移植适配②）。

export function themeColors() {
  const fallback = { up: "#2ea043", down: "#d1242f", line: "#4a9eff", text: "#888", grid: "rgba(128,128,128,.25)", bg: "transparent" };
  if (typeof document === "undefined") return fallback;
  const s = getComputedStyle(document.documentElement);
  const read = (name, fb) => (s.getPropertyValue(name) || "").trim() || fb;
  return {
    up: read("--dsw-alias-state-success-primary", fallback.up),
    down: read("--dsw-alias-state-error-primary", fallback.down),
    line: read("--dsw-alias-button-info-fill", fallback.line),
    text: read("--dsw-alias-label-secondary", fallback.text),
    grid: read("--dsw-alias-border-l1", fallback.grid),
    bg: read("--dsw-alias-bg-base", fallback.bg),
  };
}

/** 原 .tw-chart：宽 100%、高 360px、block、圆角 8、底色取主题（client.js L63）。 */
export const CHART_STYLE = { width: "100%", height: 360, display: "block", borderRadius: 8, background: "var(--dsw-alias-bg-base, Canvas)" };
/** 原 .tw-chart.small（client.js L64）：仅高度不同（150px）。 */
export const CHART_STYLE_SMALL = { ...CHART_STYLE, height: 150 };
