// canvas 绘制小工具：网格与坐标轴标签、悬停信息框、图例、分色调色板。
//
// 只放「必须拿到 ctx 才能做的事」：所有能抽成纯函数的部分都在 scale.js（标度）与
// geometry.js（几何/文本收敛）里，并用 node --test 锁住。本文件的效果只能在浏览器里看，
// 所以这里刻意写得笨：一处一判断，绝不画出边界外的元素（canvas 的 fillText 没有裁剪、
// 也不报错，越界部分是**静默消失**的——2026-09-18 K 线右边缘价格标签就栽在这里）。
//
// 四条共用规则（第一批四个组件都遵守，第二批复用同一套视觉）：
//   1. 颜色一律由调用方传入的 `color`（= themeColors()）决定，本文件不写死主题；
//   2. 轴标签放不下就整条省略，绝不叠字、绝不越出画布；
//   3. 悬停信息框的文字先按框宽截断再画（框内截断，不是让文字溢出框外）；
//   4. 数据条目的缺失由组件在**进入绘制前**跳过并计数，本文件不负责'—'。
import { tooltipLeft, truncateText } from "./geometry.js";

/** 轴标签与图例字号（与既有 line.jsx/kline.jsx 的 10px 一致）。 */
export const DEFAULT_FONT = "10px sans-serif";

/**
 * 多系列固定调色板：只用于「没有涨跌语义的并列系列」（分类、多指标）。
 * 不取 themeColors().up/down —— 那是涨跌语义（暗色下红涨绿跌），拿来做分类色会误导。
 * 中亮度，在暗色卡片（#141414）与亮色卡片上都能看清。
 */
export const CHART_PALETTE = ["#4a9eff", "#fa8c16", "#52c41a", "#f5222d", "#722ed1", "#13c2c2"];

/** 取调色板第 i 个颜色（超出后循环；i 非法时取第 0 个）。 */
export function paletteColor(index) {
  const position = Number.isInteger(index) && index >= 0 ? index : 0;
  return CHART_PALETTE[position % CHART_PALETTE.length];
}

/**
 * 状态语义色（与调度/流程页的 antd Tag 语义一一对应：ok 绿 / failed 红 / skipped 灰 /
 * pending 蓝）。语义色**不跟随主题的 up/down**：那是行情涨跌色，把「失败」画成红色没问题，
 * 但把「成功」画成绿色在红涨绿跌的约定里会被误读成下跌，所以这里用固定的状态色表。
 */
export const STATUS_COLORS = {
  ok: "#52c41a", failed: "#ff4d4f", skipped: "#8c8c8c", pending: "#1677ff",
};

/** 状态 → 颜色；未知/缺失状态回落到 `fallback`（页面传 themeColors().line 或 text）。 */
export function chartStatusColor(status, fallback = STATUS_COLORS.pending) {
  return STATUS_COLORS[status] ?? fallback;
}

/**
 * 空态文案：**说明为什么没画**（调用方传具体原因，如「仅 3 行，画不出微笑」），
 * 而不是留一张空网格让人以为数据是 0。长句按画布宽度折行（最多 3 行）后截断。
 */
export function drawEmptyText(ctx, color, { text, width, padding = 12 }) {
  ctx.save();
  ctx.font = "12px sans-serif";
  ctx.fillStyle = color.text;
  ctx.textAlign = "left";
  ctx.textBaseline = "top";
  const maxWidth = Math.max(20, width - padding * 2);
  const measure = (value) => ctx.measureText(value).width;
  const source = String(text === null || text === undefined || text === "" ? "暂无可绘制的数据" : text);
  const lines = [];
  let rest = source;
  while (rest !== "" && lines.length < 3) {
    if (measure(rest) <= maxWidth) { lines.push(rest); rest = ""; break; }
    let cut = rest.length;
    while (cut > 1 && measure(rest.slice(0, cut)) > maxWidth) cut -= 1;
    lines.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  if (rest !== "" && lines.length === 3) {
    lines[2] = truncateText(`${lines[2]}${rest}`, maxWidth, measure);
  }
  lines.forEach((line, index) => ctx.fillText(line, padding, padding + index * 18));
  ctx.restore();
}

/**
 * 数值轴（y 轴）网格线 + 左侧刻度标签。
 * `y(value)` 是 scale.js 的 linearScale 结果（像素），`ticks` 是 niceTicks(...).ticks。
 * 标签**右对齐在 `padL - 6`**：所以调用方必须先量出最宽标签再定 padL（见各组件）。
 * 落在绘图区外的刻度直接跳过（退化域下 ticks 可能超出绘制范围）。
 */
export function drawYGrid(ctx, color, { left, right, top, bottom, y, ticks, format }) {
  ctx.save();
  ctx.font = DEFAULT_FONT;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  ctx.lineWidth = 1;
  for (const value of ticks) {
    const py = y(value);
    if (!Number.isFinite(py)) continue;
    const line = Math.round(py) + 0.5;
    if (line < top - 1 || line > bottom + 1) continue;
    ctx.strokeStyle = color.grid;
    ctx.beginPath();
    ctx.moveTo(left, line);
    ctx.lineTo(right, line);
    ctx.stroke();
    ctx.fillStyle = color.text;
    ctx.fillText(format(value), left - 6, line);
  }
  ctx.restore();
}

/**
 * 时间/类别轴（x 轴）网格线 + 底部刻度标签。
 * 标签以刻度为中心、收敛进 `[2, width-2]`；与前一个标签重叠时**整条跳过**（不叠字）。
 * `labelY` 是标签基线（调用方保证 `labelY + 2 <= height`）。
 */
export function drawXGrid(ctx, color, { left, right, top, bottom, x, ticks, format, width, labelY }) {
  ctx.save();
  ctx.font = DEFAULT_FONT;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.lineWidth = 1;
  let lastRight = -Infinity;
  for (const value of ticks) {
    const px = x(value);
    if (!Number.isFinite(px)) continue;
    const line = Math.round(px) + 0.5;
    if (line < left - 1 || line > right + 1) continue;
    ctx.strokeStyle = color.grid;
    ctx.beginPath();
    ctx.moveTo(line, top);
    ctx.lineTo(line, bottom);
    ctx.stroke();
    const text = String(format(value));
    const textW = ctx.measureText(text).width;
    const textLeft = Math.min(Math.max(2, line - textW / 2), Math.max(2, width - 2 - textW));
    if (textLeft < lastRight + 6) continue;
    ctx.fillStyle = color.text;
    ctx.fillText(text, textLeft, labelY);
    lastRight = textLeft + textW;
  }
  ctx.restore();
}

/**
 * 图例（右对齐一行）：`entries = [{label, color}]`，从右往左摆放，视觉顺序与数组一致。
 * 放不下的条目整体省略（与 line.jsx 的 MultiLineChart 同一口径：图表不自带表格，
 * 省略的系列在页面上的表格/文案里仍有事实）。
 */
export function drawLegend(ctx, color, entries, { right, minLeft, top = 12 }) {
  ctx.save();
  ctx.font = DEFAULT_FONT;
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  let cursor = right;
  for (const entry of [...entries].reverse()) {
    const text = String(entry.label ?? "");
    if (text === "") continue;
    const textW = ctx.measureText(text).width;
    if (cursor - textW - 16 < minLeft) break;
    ctx.fillStyle = color.text;
    ctx.fillText(text, cursor - textW, top);
    ctx.fillStyle = entry.color || color.line;
    ctx.fillRect(cursor - textW - 11, top - 7, 8, 3);
    cursor -= textW + 20;
  }
  ctx.restore();
}

/**
 * 悬停信息框：`lines` 每项一行（第一行当地标题，原样画）。位置由 geometry.tooltipLeft
 * 决定（右侧放不下自动翻到左侧），纵向也收敛进画布内；每行先按框宽截断，**文字不溢出框**。
 */
export function drawTooltipBox(ctx, color, { lines, anchorX, anchorY, width, height, top = 4 }) {
  const rows = (Array.isArray(lines) ? lines : []).map((line) => String(line)).filter(Boolean);
  if (rows.length === 0) return;
  ctx.save();
  ctx.font = "11px sans-serif";
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  const maxWidth = Math.max(2, width - 8);
  const natural = Math.max(...rows.map((row) => ctx.measureText(row).width));
  const boxW = Math.min(natural + 16, maxWidth);
  const lineHeight = 15;
  const boxH = rows.length * lineHeight + 10;
  const boxX = tooltipLeft(anchorX, boxW, width);
  let boxY = anchorY + 14;
  if (boxY + boxH > height - 2) boxY = anchorY - 14 - boxH;
  boxY = Math.min(Math.max(top, boxY), Math.max(top, height - 2 - boxH));
  ctx.fillStyle = "rgba(20,20,24,.92)";
  ctx.fillRect(boxX, boxY, boxW, boxH);
  ctx.strokeStyle = color.grid;
  ctx.lineWidth = 1;
  ctx.strokeRect(boxX + 0.5, boxY + 0.5, boxW - 1, boxH - 1);
  ctx.fillStyle = "#f0f0f0";
  const inner = boxW - 16;
  rows.forEach((row, index) => {
    const text = truncateText(row, inner, (value) => ctx.measureText(value).width);
    ctx.fillText(text, boxX + 8, boxY + 16 + index * lineHeight);
  });
  ctx.restore();
}
