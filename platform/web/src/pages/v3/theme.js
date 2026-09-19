// V3 控制台设计 token（与 OpenDesign 设计稿 od-quant-harness-platform 的 :root 一致）。
// 作用域：只在 V3 页面外层套 ConfigProvider，既有 16 页保持 antd 默认蓝，互不影响。
// 设计稿原值：--bg:#0b0e13 --panel:#12161d --panel2:#171c26 --border:#232b37 --border2:#2f3a49
//            --text:#e6edf3 --muted:#8b97a5 --faint:#626d7c --blue:#4c8dff --green:#3fb950
//            --red:#f8514d --amber:#d9a112 --purple:#a371f7 --cyan:#39a0ed
import { theme as antdTheme } from "antd";

export const DESIGN = {
  bg: "#0b0e13",
  panel: "#12161d",
  panel2: "#171c26",
  border: "#232b37",
  border2: "#2f3a49",
  text: "#e6edf3",
  muted: "#8b97a5",
  faint: "#626d7c",
  blue: "#4c8dff",
  green: "#3fb950",
  red: "#f8514d",
  amber: "#d9a112",
  purple: "#a371f7",
  cyan: "#39a0ed",
  mono: "ui-monospace, Menlo, Consolas, monospace",
};

/** 供 V3 页面外层 ConfigProvider 使用：暗色算法 + 设计稿 token。 */
export const V3_THEME = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    colorPrimary: DESIGN.blue,
    colorInfo: DESIGN.blue,
    colorSuccess: DESIGN.green,
    colorError: DESIGN.red,
    colorWarning: DESIGN.amber,
    colorBgBase: DESIGN.bg,
    colorBgLayout: DESIGN.bg,
    colorBgContainer: DESIGN.panel,
    colorBgElevated: DESIGN.panel2,
    colorBorder: DESIGN.border,
    colorBorderSecondary: DESIGN.border,
    colorText: DESIGN.text,
    colorTextSecondary: DESIGN.muted,
    colorTextTertiary: DESIGN.faint,
    colorTextQuaternary: DESIGN.faint,
    borderRadius: 8,
    fontSize: 13,
  },
  components: {
    Card: { colorBgContainer: DESIGN.panel, headerBg: DESIGN.panel },
    Table: { headerBg: DESIGN.panel2, rowHoverBg: DESIGN.panel2, borderColor: DESIGN.border },
    Statistic: { contentFontSize: 22 },
  },
};

/** 语义色：涨跌/风险等级的颜色映射（页面共用，避免各页自造色值）。 */
export const SEMANTIC = {
  up: DESIGN.green,
  down: DESIGN.red,
  warn: DESIGN.amber,
  info: DESIGN.blue,
  muted: DESIGN.muted,
};
