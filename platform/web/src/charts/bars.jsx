// 横向/竖向条形图（第一批共用原语，2026-09-18）。
//
// 数据形状：`items = [{label, value, color?}]` —— **由调用方聚合与排序**，本组件不排序
// （「按值降序」还是「按行权价升序」是业务决定，不是绘图决定）。
//   * 期权页「行权价分布」的来源：`services/optionScreen.js` 的 `strikeBars(group)`
//     （按行权价聚合的成交量/持仓量，值可能是 `null` = 上游未给）。
//
// 两条贯穿本文件的纪律：
//   1. **缺失值不是 0**：`value` 为 null/undefined/空串/非数字 → 该条不画，并在图下方
//      用 `Typography.Text type="secondary"` 如实标注跳过了多少条（宁缺毋假）；
//   2. **不越界**：行名/数值/轴标签全部先按可用宽度收敛（`truncateText`），数值另设右侧
//      专用列——canvas 画出边界等于没画，而且没有任何报错。
import React from "react";
import { Typography } from "antd";
import { useCanvasChart } from "./line.jsx";
import { CHART_STYLE_SMALL } from "./theme.js";
import { DEFAULT_FONT, drawEmptyText, drawXGrid, drawYGrid } from "./canvas.js";
import { axisNumberText, compactNumber, truncateText } from "./geometry.js";
import { barLayout, finiteNumber, linearScale, niceTicks } from "./scale.js";

/** 「画入 N 条 · 跳过 M 条」的如实标注（M=0 时不渲染）。 */
function SkipNote({ drawn, skipped, reason }) {
  if (skipped <= 0) return null;
  return (
    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
      {`画入 ${drawn} 条 · 跳过 ${skipped} 条（${reason}）`}
    </Typography.Text>);
}

/** 只有有限数值的条目才进绘制（缺失/非数字一律跳过 + 计数）。 */
function drawableItems(items) {
  const all = Array.isArray(items) ? items : [];
  return { all, valid: all.filter((item) => finiteNumber(item?.value) !== null) };
}

/**
 * 横向条形图：每行一个条目，条长 ∝ 数值，行名在左、数值在右侧专用列。
 *
 * `items`：`[{label, value, color?}]`（**顺序即行的顺序**，组件不排序）；
 * `max`：长度轴显式上界（不给则按数据最大值；用于跨图对齐量纲）；
 * `height`：画布 CSS 高度，缺省按行数给 `max(150, 24×条数 + 30)`；
 * `valueFormat`：数值文案（默认 `compactNumber`，如 350107 → "35.01万"）；
 * `emptyText`：没有可画条目时显示的原因。
 *
 * 返回 `<>canvas + 跳过标注</>`；条数不足时画布内显示 `emptyText`。
 */
export function HBarChart({
  items, max, height, valueFormat = compactNumber, emptyText = "",
}) {
  const { all, valid } = drawableItems(items);
  const skipped = all.length - valid.length;
  const canvasHeight = Number.isFinite(height)
    ? height
    : Math.max(CHART_STYLE_SMALL.height, 24 * valid.length + 30);
  // 回调的 color 就是 useCanvasChart 内部取的 themeColors()（暗色主题色表）
  const ref = useCanvasChart((ctx, width, chartHeight, color) => {
    if (valid.length === 0) {
      drawEmptyText(ctx, color, { text: emptyText, width });
      return;
    }
    ctx.font = DEFAULT_FONT;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const measure = (text) => ctx.measureText(text).width;
    // 行名列：按最长行名给宽，上限画布 35%（行名可能很长，如 US.SPY260918C760000）
    const labels = valid.map((item) => String(item.label ?? ""));
    const widest = Math.max(...labels.map(measure));
    const labelW = Math.min(widest + 8, Math.max(40, width * 0.35));
    const values = valid.map((item) => finiteNumber(item.value));
    const valueTexts = values.map((value) => String(valueFormat(value)));
    // 数值列也设上限（画布 35%）：窄画布下不设限会把条区挤成 0，条全等于没画
    const valueW = Math.min(Math.max(...valueTexts.map(measure)), Math.max(24, width * 0.35));
    const padL = 8 + labelW;
    const padR = 12 + valueW;
    const padT = 8;
    const padB = 20;
    const plotW = Math.max(10, width - padL - padR);
    const plotH = Math.max(10, chartHeight - padT - padB);
    const dataMax = Math.max(...values, 0);
    const hint = finiteNumber(max);
    const ticks = niceTicks(0, hint !== null && hint > 0 ? hint : (dataMax > 0 ? dataMax : 1), 4);
    const x = linearScale([ticks.min, ticks.max], [padL, padL + plotW]);
    // 条长按 ticks.max 归一（不是按数据最大值）：否则条长与刻度对不上
    const rows = barLayout(values, { max: ticks.max, gap: 0.25 });
    drawXGrid(ctx, color, {
      left: padL, right: padL + plotW, top: padT, bottom: padT + plotH,
      x, ticks: ticks.ticks, format: axisNumberText, width, labelY: chartHeight - 6,
    });
    rows.forEach((row, index) => {
      const centerY = padT + plotH * row.offset + (plotH * row.extent) / 2;
      const barH = Math.max(1, plotH * row.extent);
      const barW = Math.max(0, plotW * row.ratio);
      ctx.fillStyle = color.text;
      ctx.textAlign = "left";
      ctx.fillText(truncateText(labels[index], labelW, measure), 6, centerY);
      if (barW > 0) {
        ctx.fillStyle = valid[index].color || color.line;
        ctx.fillRect(padL, centerY - barH / 2, barW, barH);
      }
      // 数值右对齐在右侧专用列里（不会压住条、也不会被画布裁掉）
      ctx.fillStyle = color.text;
      ctx.textAlign = "right";
      ctx.fillText(valueTexts[index], width - 6, centerY);
    });
  }, [valid, canvasHeight, max, valueFormat, emptyText]);
  return (
    <>
      <canvas ref={ref} style={{ ...CHART_STYLE_SMALL, height: canvasHeight }} />
      <SkipNote drawn={valid.length} skipped={skipped} reason="数值缺失或非数字" />
    </>);
}

/**
 * 竖向柱状图：每根柱一个条目，支持正负分色。
 *
 * `items`：`[{label, value, color?}]`；未给 `color` 时按正负用 `themeColors().up/down`
 * （暗色主题下红涨绿跌，正值红、负值绿），给了 `color` 一律以调用方为准（用于多指标分色）；
 * `height` / `emptyText` 同 HBarChart；`valueFormat` 用于 **y 轴刻度文案**（默认
 * `axisNumberText`：整数原样、千级以上走万/亿）。
 *
 * 零基准由 `barLayout` 的 `zeroRatio` 给出：正值向上、负值向下，两向共用同一条零线。
 * 类别标签放不下时按固定步长隔条显示（不叠字、不越界）；不逐柱标数值——柱多时会糊成一片，
 * 精确值在页面的表格里。
 */
export function VBarChart({ items, height, valueFormat = axisNumberText, emptyText = "" }) {
  const { all, valid } = drawableItems(items);
  const skipped = all.length - valid.length;
  const canvasHeight = Number.isFinite(height) ? height : CHART_STYLE_SMALL.height;
  // 回调的 color 就是 useCanvasChart 内部取的 themeColors()（暗色主题色表）
  const ref = useCanvasChart((ctx, width, chartHeight, color) => {
    if (valid.length === 0) {
      drawEmptyText(ctx, color, { text: emptyText, width });
      return;
    }
    const values = valid.map((item) => finiteNumber(item.value));
    const ticks = niceTicks(Math.min(0, ...values), Math.max(0, ...values), 4);
    ctx.font = DEFAULT_FONT;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    const measure = (text) => ctx.measureText(text).width;
    const tickTexts = ticks.ticks.map((value) => String(valueFormat(value)));
    // padL 上限画布 35%：刻度文案很长时也不能把柱区挤没
    const padL = Math.min(Math.max(...tickTexts.map(measure)) + 12, Math.max(30, width * 0.35));
    const padR = 10;
    const padT = 10;
    const padB = 20;
    const plotW = Math.max(10, width - padL - padR);
    const plotH = Math.max(10, chartHeight - padT - padB);
    const top = padT;
    const bottom = padT + plotH;
    const y = linearScale([ticks.min, ticks.max], [bottom, top]);
    const rows = barLayout(values, { min: ticks.min, max: ticks.max, gap: 0.25 });
    drawYGrid(ctx, color, {
      left: padL, right: padL + plotW, top, bottom, y, ticks: ticks.ticks, format: valueFormat,
    });
    const zeroY = y(0);
    // 类别标签：按「最宽标签 + 8px」估算每屏能放几个，放不下的按固定步长隔条显示
    const labels = valid.map((item) => String(item.label ?? ""));
    const widest = Math.max(...labels.map(measure));
    const slotW = plotW / rows.length;
    const perLabel = widest + 8;
    const step = Math.max(1, Math.ceil(rows.length / Math.max(1, Math.floor(plotW / perLabel))));
    ctx.textAlign = "center";
    rows.forEach((row, index) => {
      const left = padL + plotW * row.offset;
      const barW = Math.max(1, plotW * row.extent);
      const valueY = y(row.value ?? 0);
      const high = Math.min(zeroY, valueY);
      const height2 = Math.max(1, Math.abs(valueY - zeroY));
      ctx.fillStyle = valid[index].color
        || (row.negative ? color.down : color.up);
      ctx.fillRect(left, high, barW, height2);
      if (index % step === 0) {
        const text = truncateText(labels[index], slotW * step, measure);
        const textW = measure(text);
        // 标签向内收敛进 [2, width-2]：贴右边缘的最后一条若按中心对齐，右半个字会被
        // 画布吃掉（与 kline 的价格标签同一教训，见 geometry.priceTagRect）
        const centerX = Math.min(Math.max(left + barW / 2, padL), width - padR);
        const textLeft = Math.min(Math.max(2, centerX - textW / 2), Math.max(2, width - 2 - textW));
        ctx.fillStyle = color.text;
        ctx.textAlign = "left";
        ctx.fillText(text, textLeft, chartHeight - 6);
        ctx.textAlign = "center";
      }
    });
    // 零线画在最上层，正负柱一眼能分辨基准
    ctx.strokeStyle = color.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, Math.round(zeroY) + 0.5);
    ctx.lineTo(width - padR, Math.round(zeroY) + 0.5);
    ctx.stroke();
  }, [valid, canvasHeight, valueFormat, emptyText]);
  return (
    <>
      <canvas ref={ref} style={{ ...CHART_STYLE_SMALL, height: canvasHeight }} />
      <SkipNote drawn={valid.length} skipped={skipped} reason="数值缺失或非数字" />
    </>);
}
