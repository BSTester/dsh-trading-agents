// 图表主题色读取 + 容器内联样式。移植自 plugins/workbench/src/client.js：
// themeColors L301-314（canvas 取色逻辑原样）；CHART_STYLE/CHART_STYLE_SMALL 为
// 原 .tw-chart / .tw-chart.small CSS（client.js L63-64）的内联等价（移植适配②）。

// 暗色配色（全站暗色主题，<html data-theme="dark"> 由 index.html 声明）：
//   背景 #141414（antd dark 默认）、网格线 rgba(255,255,255,0.06)、文字 #d9d9d9；
//   涨 #f5222d / 跌 #52c41a —— 中国习惯红涨绿跌，保持不变（亮色系 antd red-7/green-7
//   在暗底上对比度不足，改用 red-6/green-6）。
const DARK = {
  up: "#f5222d", down: "#52c41a", line: "#4a9eff", text: "#d9d9d9",
  grid: "rgba(255,255,255,0.06)", bg: "transparent",
};

export function themeColors() {
  const fallback = { up: "#2ea043", down: "#d1242f", line: "#4a9eff", text: "#888", grid: "rgba(128,128,128,.25)", bg: "transparent" };
  if (typeof document === "undefined") return fallback;
  if (document.documentElement.getAttribute("data-theme") === "dark") return DARK;
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

/** 原 .tw-chart：宽 100%、高 360px、block、圆角 8。底色 fallback 由 Canvas 改 transparent：
 *  暗色卡片（#141414）下 Canvas 系统色是白色，会画出白色矩形；透明即融入卡片底色
 *  （亮色卡片为白、暗色卡片为深，两种主题下都不再需要自己铺底）。 */
export const CHART_STYLE = { width: "100%", height: 360, display: "block", borderRadius: 8, background: "var(--dsw-alias-bg-base, transparent)" };
/** 原 .tw-chart.small（client.js L64）：仅高度不同（150px）。 */
export const CHART_STYLE_SMALL = { ...CHART_STYLE, height: 150 };
