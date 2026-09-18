// 展示格式化收拢：批 1 各页面本地重复的格式化实现统一到这里。
// 只做纯展示换算（千分位/百分号/账户脱敏），不做任何业务计算或指标推导。
//
// 2026-09-19：三个纯字符串函数（num/pctOf/stampOf）的实现移到 `./formatCore.js`
// （无 JSX，可被 node --test 与字段审计工具直接加载），本文件**原样再导出**——
// 全站 `import { num, pctOf, stampOf } from "…/format.jsx"` 的写法与行为零变化。
import React from "react";
import { Tooltip } from "antd";
import {
  MISSING, clockText, dayText, minuteText, numText, pctOfText, stampText,
} from "./formatCore.js";

export { MISSING };

/** 数值格式化：null/undefined/空串显示 —；非有限数值原样字符串；其余按中文环境千分位。 */
export function num(value, digits = 2) {
  return numText(value, digits);
}

/** 比例 → 百分数显示（纯单位换算，非指标计算）：0.0123 → 1.23%。 */
export function pctOf(ratio, digits = 2) {
  return pctOfText(ratio, digits);
}

/** 账户末四位脱敏（…+末四位）；title 有值时悬停显示完整账户名（统一 antd Tooltip）。 */
export function maskedAccount(accId, title) {
  const id = typeof accId === "string" ? accId : "";
  const masked = id ? `…${id.slice(-4)}` : "—";
  return <Tooltip title={title}><span>{masked}</span></Tooltip>;
}

/** 秒级时间戳统一展示：ISO 的 T 分隔 → 与库内时间一致的空格分隔；缺失显示 —。
 *  用于卡片 extra 一类元数据（alerts.emit 的 created_at、reconcile 的 diffs_at 同源）。 */
export function stampOf(value) {
  return stampText(value);
}

/** 时刻列（可能给毫秒时间戳）：时间戳 → HH:mm；字符串时间退回前 16 位；缺失 → —。 */
export function clockOf(value) {
  return clockText(value);
}

/** 日期列（可能给毫秒时间戳）：时间戳 → YYYY-MM-DD；字符串时间退回前 10 位；缺失 → —。 */
export function dayOf(value) {
  return dayText(value);
}

/** 分钟精度时刻（毫秒/微秒时间戳或 ISO 串）：执行页 OpenAPI 订单/成交表的更新时间列。 */
export function minuteOf(value) {
  return minuteText(value);
}
