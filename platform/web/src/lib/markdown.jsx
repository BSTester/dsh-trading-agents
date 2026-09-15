// Markdown 渲染（React 部分）——自 plugins/workbench/src/client.js 移植：
// renderInline L870-882、renderBlocks L885-923、Markdown L957-961。
// 适配（与 charts 移植同一约定，逻辑零改动）：
//   ①h(...) → JSX；②tw-* className → 内联样式（等价规则取自 client.js L95-117 原 CSS）；
//   ③纯函数从 markdown-core.js re-export（node --test 无 JSX 加载器，测试走纯 JS 模块）。
// 仅三处 CSS 子规则无法在不动 renderBlocks/renderInline 结构的前提下内联表达而省略
// （均为装饰，不影响结构与内容）：li::marker 颜色、表格偶数行斑马纹、
// 引用块内段落的 .tw-md-quote .tw-md-p margin 收窄（9px→6px）。
// 安全性同原实现：渲染 React 元素而非拼 HTML 字符串，原始 HTML 按字面转义。
import React from "react";
import { parseMarkdown } from "./markdown-core.js";

export { parseInline, parseList, parseMarkdown } from "./markdown-core.js";

// 原 .tw-md（client.js L95）：13px / 1.75 行高 / 主文字色 / 任意断行。
const MD_STYLE = {
  fontSize: 13, lineHeight: 1.75,
  color: "var(--dsw-alias-label-primary, CanvasText)", overflowWrap: "anywhere",
};
const P_STYLE = { margin: "9px 0" };
// 原 .tw-md-h 基础 + .tw-md-h1..h6 分级（client.js L99-104）。
const HEADING_BASE = { margin: "18px 0 8px", fontWeight: 650, lineHeight: 1.35, letterSpacing: "-0.01em" };
const HEADING_RULE = { paddingBottom: 6, borderBottom: "1px solid var(--dsw-alias-border-l1, GrayText)" };
const HEADING_STYLES = {
  1: { ...HEADING_BASE, ...HEADING_RULE, fontSize: 19 },
  2: { ...HEADING_BASE, ...HEADING_RULE, fontSize: 16, paddingBottom: 5 },
  3: { ...HEADING_BASE, fontSize: 14.5 },
  4: { ...HEADING_BASE, fontSize: 13.5, color: "var(--dsw-alias-label-secondary, GrayText)" },
  5: { ...HEADING_BASE, fontSize: 13.5, color: "var(--dsw-alias-label-secondary, GrayText)" },
  6: { ...HEADING_BASE, fontSize: 13.5, color: "var(--dsw-alias-label-secondary, GrayText)" },
};
// 原 .tw-md-code / .tw-md-link / .tw-md-pre / .tw-md-lang / .tw-md-quote / .tw-md-hr /
//    .tw-md-table-wrap / .tw-md-table / th / td（client.js L108-117）。
const CODE_STYLE = {
  padding: "1px 5px", borderRadius: 5, fontSize: 12,
  fontFamily: "var(--ds-font-family-code, ui-monospace, Menlo, monospace)",
  background: "var(--dsw-alias-bg-layer-2, rgba(127,127,127,.14))",
  border: "1px solid var(--dsw-alias-border-l1, rgba(127,127,127,.22))",
};
const LINK_STYLE = {
  color: "var(--dsw-alias-label-primary-bluish, LinkText)", textDecoration: "none",
  borderBottom: "1px solid color-mix(in srgb, currentColor 35%, transparent)",
};
const PRE_STYLE = {
  position: "relative", margin: "11px 0", padding: "12px 13px", borderRadius: 9,
  overflow: "auto", maxHeight: 420, fontSize: 12, lineHeight: 1.6,
  fontFamily: "var(--ds-font-family-code, ui-monospace, Menlo, monospace)",
  background: "var(--dsw-alias-bg-base, Canvas)",
  border: "1px solid var(--dsw-alias-border-l1, GrayText)",
};
const PRE_CODE_STYLE = { whiteSpace: "pre", background: "none", border: "none", padding: 0, fontSize: "inherit" };
const LANG_STYLE = {
  position: "absolute", top: 6, right: 10, fontSize: 10, letterSpacing: "0.06em",
  textTransform: "uppercase", color: "var(--dsw-alias-label-tertiary, GrayText)",
};
const QUOTE_STYLE = {
  margin: "11px 0", padding: "2px 0 2px 13px",
  borderLeft: "3px solid var(--dsw-alias-label-primary-bluish, LinkText)",
  color: "var(--dsw-alias-label-secondary, GrayText)",
};
const HR_STYLE = { margin: "16px 0", border: "none", borderTop: "1px solid var(--dsw-alias-border-l1, GrayText)" };
const LIST_STYLE = { margin: "9px 0", paddingLeft: 22 };
const LI_STYLE = { margin: "4px 0" };
const TABLE_WRAP_STYLE = {
  margin: "11px 0", overflowX: "auto", borderRadius: 9,
  border: "1px solid var(--dsw-alias-border-l1, GrayText)",
};
const TABLE_STYLE = { width: "100%", borderCollapse: "collapse", fontSize: 12.5 };
const TH_STYLE = {
  textAlign: "left", fontWeight: 650, padding: "7px 10px", whiteSpace: "nowrap",
  background: "var(--dsw-alias-bg-layer-2, rgba(127,127,127,.1))",
};
const TD_STYLE = {
  padding: "7px 10px", verticalAlign: "top",
  borderTop: "1px solid var(--dsw-alias-border-l1, rgba(127,127,127,.22))",
};

/** 行内节点 → React 元素。逻辑与 client.js renderInline 逐分支一致。 */
function renderInline(nodes, keyPrefix) {
  return (nodes ?? []).map((node, i) => {
    const key = `${keyPrefix}-${i}`;
    switch (node.type) {
      case "strong": return <strong key={key}>{renderInline(node.children, key)}</strong>;
      case "em": return <em key={key}>{renderInline(node.children, key)}</em>;
      case "code": return <code key={key} style={CODE_STYLE}>{node.value}</code>;
      case "link": return (
        <a key={key} style={LINK_STYLE} href={node.href} target="_blank" rel="noopener noreferrer">
          {renderInline(node.children, key)}
        </a>);
      default: return <React.Fragment key={key}>{node.value}</React.Fragment>;
    }
  });
}

/** 块节点 → React 元素。逻辑与 client.js renderBlocks 逐分支一致。 */
function renderBlocks(blocks, keyPrefix = "md") {
  return (blocks ?? []).map((block, i) => {
    const key = `${keyPrefix}-${i}`;
    switch (block.type) {
      case "heading": {
        const level = Math.min(Math.max(block.level, 1), 6);
        const Tag = `h${level}`;
        return <Tag key={key} style={HEADING_STYLES[level]}>{renderInline(block.children, key)}</Tag>;
      }
      case "code":
        return (
          <pre key={key} style={PRE_STYLE}>
            {block.lang ? <span style={LANG_STYLE}>{block.lang}</span> : null}
            <code style={PRE_CODE_STYLE}>{block.text}</code>
          </pre>);
      case "quote":
        return <blockquote key={key} style={QUOTE_STYLE}>{renderBlocks(block.children, key)}</blockquote>;
      case "hr":
        return <hr key={key} style={HR_STYLE} />;
      case "list": {
        const Tag = block.ordered ? "ol" : "ul";
        return (
          <Tag key={key} style={LIST_STYLE}>
            {block.items.map((item, j) => (
              <li key={`${key}-${j}`} style={LI_STYLE}>
                {renderInline(item.children, `${key}-${j}`)}
                {item.sub ? renderBlocks([item.sub], `${key}-${j}s`) : null}
              </li>))}
          </Tag>);
      }
      case "table": {
        return (
          <div key={key} style={TABLE_WRAP_STYLE}>
            <table style={TABLE_STYLE}>
              <thead>
                <tr>
                  {block.header.map((cell, j) => (
                    <th key={`${key}-h${j}`} style={TH_STYLE}>{renderInline(cell, `${key}-h${j}`)}</th>))}
                </tr>
              </thead>
              <tbody>
                {block.rows.map((row, r) => (
                  <tr key={`${key}-r${r}`}>
                    {row.map((cell, c) => (
                      <td key={`${key}-r${r}c${c}`} style={TD_STYLE}>{renderInline(cell, `${key}-r${r}c${c}`)}</td>))}
                  </tr>))}
              </tbody>
            </table>
          </div>);
      }
      default:
        return <p key={key} style={P_STYLE}>{renderInline(block.children, key)}</p>;
    }
  });
}

/** 研报正文：解析一次，渲染 React 元素。逻辑与 client.js Markdown 组件一致。 */
export function Markdown({ text }) {
  const blocks = React.useMemo(() => parseMarkdown(text), [text]);
  if (!String(text ?? "").trim()) return <p style={P_STYLE}>本篇研报没有正文</p>;
  return <div style={MD_STYLE}>{renderBlocks(blocks)}</div>;
}
