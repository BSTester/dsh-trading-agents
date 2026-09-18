// 环图（第一批共用原语，2026-09-18）：占位/构成类图表（「概览市场占比」那一批会复用）。
//
// 数据形状：`slices = [{label, value, color?}]` —— 调用方决定顺序与标签；
//   * 概览页「市场占比」的来源是各页面/端点已经聚合好的占比行（本组件不做业务聚合）。
//
// 与 bars.jsx 相同的两条纪律：
//   1. 数值缺失/非正数**不画扇区**（负数按绝对值画、或全 0 画成整圆，都是在编数据），
//      并在图下如实标注跳过条数；
//   2. 图例文字按可用宽度收敛，不越出画布（图例在右侧放不下就整体挪到下方）。
import React from "react";
import { Typography } from "antd";
import { useCanvasChart } from "./line.jsx";
import { CHART_STYLE_SMALL } from "./theme.js";
import { drawEmptyText, paletteColor } from "./canvas.js";
import { compactNumber, truncateText } from "./geometry.js";
import { donutArcs, finiteNumber } from "./scale.js";

/**
 * 环图。
 *
 * `slices`：`[{label, value, color?}]`（`color` 缺省按固定调色板循环）；
 * `height`：画布高度（缺省 150）；
 * `centerText`：圆心文字（如合计/占比口径，超过孔径会截断）；
 * `valueFormat`：图例里的数值文案（默认 `compactNumber`）；
 * `emptyText`：无可画扇区时的原因文案。
 *
 * 布局：图例优先放右侧（要求宽度够**且**纵向放得下），否则整体挪到下方折行——
 * 纵向放不下时环会收缩（宁可环小，也不让 canvas 把图例行静默吃掉），仍放不下则如实写
 * 「…还有 N 项」（调用方给足 height 即可全画下）。扇区从 12 点方向顺时针。
 * 返回 `<>canvas + 跳过标注</>`。
 */
export function DonutChart({
  slices, height, centerText = "", valueFormat = compactNumber, emptyText = "",
}) {
  const all = Array.isArray(slices) ? slices : [];
  const valid = all.filter((slice) => {
    const value = finiteNumber(slice?.value);
    return value !== null && value > 0;
  });
  const skipped = all.length - valid.length;
  const canvasHeight = Number.isFinite(height) ? height : CHART_STYLE_SMALL.height;
  const total = valid.reduce((sum, slice) => sum + finiteNumber(slice.value), 0);
  const ref = useCanvasChart((ctx, width, chartHeight, color) => {
    if (valid.length === 0) {
      drawEmptyText(ctx, color, { text: emptyText, width });
      return;
    }
    ctx.font = "10px sans-serif";
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const measure = (text) => ctx.measureText(text).width;
    const labels = valid.map((slice) => String(slice.label ?? ""));
    const values = valid.map((slice) => String(valueFormat(finiteNumber(slice.value))));
    const entries = valid.map((slice, index) => ({
      label: labels[index], value: values[index], color: slice.color || paletteColor(index),
    }));
    const widestLabel = Math.max(...entries.map((entry) => measure(entry.label)));
    const widestValue = Math.max(...entries.map((entry) => measure(entry.value)));
    // 图例放右侧的判据：单行图例（色块 12 + 标签 + 8 + 数值 + 12）得放得下，
    // 且给环留出至少 110px；否则改放下方折行。
    const rightWidth = 12 + widestLabel + 8 + widestValue + 12;
    // 右侧图例还要求**纵向放得下**：塞不下的行会被画布静默吃掉（canvas 没有裁剪提示），
    // 那种「图例少了几项」的图比换布局更坏，所以放不下就整体挪到下方折行。
    const sideLegend = width - rightWidth >= 110 && entries.length * 16 + 12 <= chartHeight;
    const legendH = sideLegend ? 0 : entries.length * 16 + 10;
    const ringBoxH = Math.max(40, chartHeight - legendH);
    const cx = sideLegend ? (width - rightWidth) / 2 + 6 : width / 2;
    const cy = ringBoxH / 2;
    const radius = Math.max(16, Math.min(
      (sideLegend ? width - rightWidth : width) / 2 - 12,
      ringBoxH / 2 - 12,
    ));
    const inner = radius * 0.6;
    const arcs = donutArcs(valid.map((slice) => finiteNumber(slice.value)));
    arcs.forEach((arc) => {
      ctx.beginPath();
      ctx.arc(cx, cy, radius, arc.from, arc.to);
      ctx.arc(cx, cy, inner, arc.to, arc.from, true);
      ctx.closePath();
      ctx.fillStyle = entries[arc.index].color;
      ctx.fill();
    });
    if (centerText) {
      ctx.textAlign = "center";
      ctx.fillStyle = color.text;
      const text = truncateText(centerText, inner * 2 - 6, measure);
      ctx.fillText(text, cx, cy);
      ctx.textAlign = "left";
    }
    // 图例：右侧单列（数值右对齐）或下方折行
    if (sideLegend) {
      const left = width - rightWidth + 4;
      const valueRight = width - 6;
      const startY = Math.max(10, cy - (entries.length * 16) / 2 + 8);
      entries.forEach((entry, index) => {
        const rowY = startY + index * 16;
        ctx.fillStyle = entry.color;
        ctx.fillRect(left - 10, rowY - 3, 8, 6);
        ctx.fillStyle = color.text;
        ctx.fillText(truncateText(entry.label, valueRight - measure(entry.value) - 8 - left, measure),
          left, rowY);
        ctx.textAlign = "right";
        ctx.fillText(entry.value, valueRight, rowY);
        ctx.textAlign = "left";
      });
    } else {
      // 下方折行图例：按可用宽度贪婪换行；**放不下的行如实写「还有 N 项」**，
      // 而不是让 canvas 把剩下的行静默截掉（调用方给足 height 就能全画下）。
      const maxRows = Math.max(1, Math.floor((chartHeight - ringBoxH - 10 + 6) / 16));
      let cursorX = 4;
      let rowY = ringBoxH + 10;
      let placed = 0;
      for (const entry of entries) {
        const blockW = 10 + measure(entry.label) + 6 + measure(entry.value) + 14;
        const wraps = cursorX > 4 && cursorX + blockW > width - 4;
        if (wraps) {
          const nextRow = rowY + 16;
          if (placed > 0 && Math.floor((nextRow - ringBoxH - 10) / 16) + 1 > maxRows) break;
          cursorX = 4;
          rowY = nextRow;
        }
        ctx.fillStyle = entry.color;
        ctx.fillRect(cursorX, rowY - 3, 8, 6);
        ctx.fillStyle = color.text;
        const labelText = truncateText(entry.label,
          Math.max(10, width - cursorX - 10 - measure(entry.value) - 6 - 10), measure);
        ctx.fillText(labelText, cursorX + 10, rowY);
        ctx.fillText(entry.value, cursorX + 10 + measure(labelText) + 6, rowY);
        cursorX += 10 + measure(labelText) + 6 + measure(entry.value) + 14;
        placed += 1;
      }
      if (placed < entries.length) {
        ctx.fillStyle = color.text;
        const rest = `…还有 ${entries.length - placed} 项（画布高度不够，加 height 或看表格）`;
        ctx.textAlign = "left";
        ctx.fillText(truncateText(rest, width - 8, measure), 4, Math.min(chartHeight - 5, rowY + 16));
      }
    }
  }, [valid, canvasHeight, centerText, valueFormat, emptyText]);
  return (
    <>
      <canvas ref={ref} style={{ ...CHART_STYLE_SMALL, height: canvasHeight }} />
      {skipped > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {`画入 ${valid.length} 项 · 跳过 ${skipped} 项（数值缺失、为 0 或负数，不计入占比）`}
        </Typography.Text>)}
    </>);
}
