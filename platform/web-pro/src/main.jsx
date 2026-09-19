import React from "react";
import { createRoot } from "react-dom/client";
import { App as AntApp, ConfigProvider, theme as antdTheme } from "antd";
import zhCN from "antd/locale/zh_CN";
import Shell from "./app.jsx";

// 设计稿 token（与 /v3/ 版同一套；保证两版配色/语义色一致）
const DESIGN = {
  bg: "#0b0e13",
  panel: "#12161d",
  panel2: "#171c26",
  border: "#232b37",
  text: "#e6edf3",
  muted: "#8b97a5",
  faint: "#626d7c",
  blue: "#4c8dff",
  green: "#3fb950",
  red: "#f8514d",
  amber: "#d9a112",
  purple: "#a371f7",
  cyan: "#39a0ed",
};

const V3_THEME = {
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
    borderRadius: 8,
    fontSize: 13,
  },
  components: {
    Card: { colorBgContainer: DESIGN.panel, headerBg: DESIGN.panel },
    Table: { headerBg: DESIGN.panel2, rowHoverBg: DESIGN.panel2, borderColor: DESIGN.border },
  },
};

createRoot(document.getElementById("root")).render(
  <ConfigProvider locale={zhCN} theme={V3_THEME}>
    <AntApp>
      <Shell />
    </AntApp>
  </ConfigProvider>,
);

export { DESIGN, V3_THEME };
