// 展示格式化收拢：批 1 各页面本地重复的格式化实现统一到这里。
// 只做纯展示换算（千分位/百分号/账户脱敏），不做任何业务计算或指标推导。
import React from "react";
import { Tooltip } from "antd";

/** 数值格式化：null/undefined/空串显示 —；非有限数值原样字符串；其余按中文环境千分位。 */
export function num(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed)
    ? parsed.toLocaleString("zh-CN", { maximumFractionDigits: digits })
    : String(value);
}

/** 比例 → 百分数显示（纯单位换算，非指标计算）：0.0123 → 1.23%。 */
export function pctOf(ratio, digits = 2) {
  const parsed = Number(ratio);
  if (ratio === null || ratio === undefined || !Number.isFinite(parsed)) return "—";
  return `${(parsed * 100).toFixed(digits)}%`;
}

/** 账户末四位脱敏（…+末四位）；title 有值时悬停显示完整账户名（统一 antd Tooltip）。 */
export function maskedAccount(accId, title) {
  const id = typeof accId === "string" ? accId : "";
  const masked = id ? `…${id.slice(-4)}` : "—";
  return <Tooltip title={title}><span>{masked}</span></Tooltip>;
}
