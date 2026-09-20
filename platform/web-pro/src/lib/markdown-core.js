// Markdown 解析纯函数——自 plugins/workbench/src/client.js L723-867 移植。
// **逻辑零改动**（含注释）：解析规则、正则、边界处理与原实现逐行一致；
// 适配仅有两处：①模块化 export（原为闭包内函数）；②不含任何 React 依赖，
// 便于 node --test 直测（tests/markdown.test.mjs 的断言形状先用原实现探查确认）。
// React 渲染部分（renderInline/renderBlocks/Markdown）在同目录 markdown.jsx。

/** 行内解析：文本 → 行内节点数组。 */
export function parseInline(text) {
  const nodes = [];
  const push = (node) => nodes.push(node);
  let buffer = "";
  const flush = () => { if (buffer) { push({ type: "text", value: buffer }); buffer = ""; } };
  // 依次匹配：代码、链接、粗体、斜体。代码优先，避免 `**a**` 被当成粗体。
  const pattern = /(`[^`]+`)|(\[[^\]]*\]\([^)\s]+\))|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\n]+\*)|(_[^_\n]+_)/;
  let rest = String(text ?? "");
  while (rest.length > 0) {
    const match = pattern.exec(rest);
    if (!match) { buffer += rest; break; }
    buffer += rest.slice(0, match.index);
    flush();
    const token = match[0];
    if (token.startsWith("`")) {
      push({ type: "code", value: token.slice(1, -1) });
    } else if (token.startsWith("[")) {
      const split = token.indexOf("](");
      const href = token.slice(split + 2, -1);
      // 只允许安全协议，挡掉 javascript: 之类
      const safe = /^(https?:|mailto:|\/|#)/i.test(href) ? href : null;
      if (safe) push({ type: "link", href: safe, children: parseInline(token.slice(1, split)) });
      else push({ type: "text", value: token });
    } else if (token.startsWith("**") || token.startsWith("__")) {
      push({ type: "strong", children: parseInline(token.slice(2, -2)) });
    } else {
      push({ type: "em", children: parseInline(token.slice(1, -1)) });
    }
    rest = rest.slice(match.index + token.length);
  }
  flush();
  return nodes;
}

/** 表格分隔行：| --- | :--: | */
const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
const splitRow = (line) => line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((cell) => cell.trim());

const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

/**
 * 解析一个列表（含嵌套）。缩进更深的项作为上一项的子列表，
 * 而不是拍平成同级——拍平会让"关键变量是 eCPM 而非库存"这种
 * 补充说明看起来和主结论平级，改变语义。
 */
export function parseList(lines, start, baseIndent) {
  const items = [];
  let index = start;
  const ordered = /\d/.test(LIST_ITEM.exec(lines[start])[2]);
  while (index < lines.length) {
    const item = LIST_ITEM.exec(lines[index]);
    if (!item || item[1].length !== baseIndent) break;
    const parts = [item[3]];
    index += 1;
    // 续行：比本项缩进更深、且不是新的列表项
    while (index < lines.length && lines[index].trim()
           && !LIST_ITEM.test(lines[index])
           && lines[index].length - lines[index].trimStart().length > baseIndent) {
      parts.push(lines[index].trim()); index += 1;
    }
    const node = { children: parseInline(parts.join(" ")) };
    const nested = index < lines.length ? LIST_ITEM.exec(lines[index]) : null;
    if (nested && nested[1].length > baseIndent) {
      const sub = parseList(lines, index, nested[1].length);
      node.sub = sub.list;
      index = sub.next;
    }
    items.push(node);
  }
  return { list: { type: "list", ordered, items }, next: index };
}

/**
 * 块级解析：Markdown → 块节点数组。
 * 纯函数、不碰 React，便于离线断言结构。
 */
export function parseMarkdown(text) {
  const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) { index += 1; continue; }
    // 围栏代码块
    const fence = /^\s*(```|~~~)\s*(\S*)\s*$/.exec(line);
    if (fence) {
      const marker = fence[1][0].repeat(3);
      const body = [];
      index += 1;
      while (index < lines.length && !new RegExp(`^\\s*${marker}`).test(lines[index])) {
        body.push(lines[index]); index += 1;
      }
      index += 1;   // 跳过收尾围栏
      blocks.push({ type: "code", lang: fence[2] || "", text: body.join("\n") });
      continue;
    }
    // 标题
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({ type: "heading", level: heading[1].length, children: parseInline(heading[2].trim()) });
      index += 1; continue;
    }
    // 分隔线
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { blocks.push({ type: "hr" }); index += 1; continue; }
    // 表格：表头 + 分隔行
    if (line.includes("|") && index + 1 < lines.length && TABLE_DIVIDER.test(lines[index + 1])) {
      const header = splitRow(line).map(parseInline);
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(splitRow(lines[index]).map(parseInline)); index += 1;
      }
      blocks.push({ type: "table", header, rows });
      continue;
    }
    // 引用（可多行）
    if (/^\s*>\s?/.test(line)) {
      const body = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        body.push(lines[index].replace(/^\s*>\s?/, "")); index += 1;
      }
      blocks.push({ type: "quote", children: parseMarkdown(body.join("\n")) });
      continue;
    }
    // 列表：按缩进分层（子项挂到上一项的 sub 上，拍平会丢层次）
    const bullet = LIST_ITEM.exec(line);
    if (bullet) {
      const parsed = parseList(lines, index, bullet[1].length);
      blocks.push(parsed.list);
      index = parsed.next;
      continue;
    }
    // 段落：连续非空行合并（Markdown 的软换行按空格处理）
    const paragraph = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim()
           && !/^(#{1,6})\s|^\s*(```|~~~)|^\s*>|^\s*([-*+]|\d+[.)])\s/.test(lines[index])
           && !(lines[index].includes("|") && index + 1 < lines.length && TABLE_DIVIDER.test(lines[index + 1]))) {
      paragraph.push(lines[index].trim()); index += 1;
    }
    blocks.push({ type: "paragraph", children: parseInline(paragraph.join(" ")) });
  }
  return blocks;
}
