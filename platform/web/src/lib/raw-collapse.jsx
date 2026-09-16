// 原始返回折叠块（核对出口）：数据面页面共用——通道字段与官方文档不符时，
// 界面上必须有一个能看到「上游到底返回了什么」的地方，同时界面本身不编造数据。
// 从 options.jsx 的本地实现提取（WP12 任务 6 起研究页也用同一出口）。
import React from "react";
import { Collapse } from "antd";

/** value 为空或加载中不渲染（无内容可核对时不占位）。 */
export function RawCollapse({ value, loading, label = "原始返回（核对用）" }) {
  if (loading || value === null || value === undefined) return null;
  return (
    <Collapse size="small" items={[{
      key: "raw",
      label,
      children: (
        <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12, maxHeight: 320, overflow: "auto" }}>
          {JSON.stringify(value, null, 2)}
        </pre>),
    }]} />);
}
