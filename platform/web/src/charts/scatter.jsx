// 散点图 + 同系列按 x 连线（第一批共用原语，2026-09-18）：IV 微笑就是「点 + 线」。
//
// 数据形状：`series = [{key, label, color?, points: [{x, y, meta?}]}]`
//   * 期权页「IV 微笑」的来源：`services/optionScreen.js` 的 `smileSeries(group)`
//     （x = 行权价，y = 隐含波动率 %，`meta` 是已经排好版的「成交量 / 持仓量」文字行）。
//
// 与既有 line.jsx 的分工：line.jsx 的横轴是**等距下标**（K 线/序列），本组件横轴是
// **连续的 x 数值**（行权价），所以需要自己的 x 标度与「按 x 排序后连线」。
//
// 三条纪律：
//   1. 点按 x 升序连线：不排序的话上游返回顺序一变，折线就会来回穿插（画出一团毛线）；
//   2. x 或 y 缺失（null/空串/非数字）→ 该点不画 + 图下如实标注跳过条数，且**连线断开**
//      （不跨越缺失点直连，那会凭空造出一段不存在的曲线）；
//   3. 点数少于 `minPoints` → 不画（只有 1-3 个点连不成形状），显示 `emptyText` 说明原因。
import React from "react";
import { Typography } from "antd";
import { useCanvasChart } from "./line.jsx";
import { CHART_STYLE_SMALL } from "./theme.js";
import {
  DEFAULT_FONT, drawEmptyText, drawLegend, drawTooltipBox, drawXGrid, drawYGrid, paletteColor,
} from "./canvas.js";
import { axisNumberText } from "./geometry.js";
import { finiteNumber, linearScale, niceTicks } from "./scale.js";

/** 悬停命中的最大像素距离：比这远就当作「没指到任何点」（避免远处点也弹提示）。 */
const HOVER_RADIUS = 30;

/** 点数/坐标都有效的点（x 与 y 都必须是有限数）。 */
function validPoints(points) {
  return (Array.isArray(points) ? points : [])
    .map((point, order) => ({ point, order, x: finiteNumber(point?.x), y: finiteNumber(point?.y) }))
    .filter((entry) => entry.x !== null && entry.y !== null);
}

/** meta → 提示框行：数组原样当行；对象按 `键 值` 展开（键请直接写展示用中文标签）。 */
function metaLines(meta) {
  if (meta === null || meta === undefined || meta === "") return [];
  if (Array.isArray(meta)) return meta.map((line) => String(line)).filter((line) => line !== "");
  if (typeof meta === "object") {
    return Object.entries(meta)
      .filter(([, value]) => value !== null && value !== undefined && value !== "")
      .map(([key, value]) => `${key} ${value}`);
  }
  return [String(meta)];
}

/**
 * 散点 + 连线图。
 *
 * `series`：`[{key, label, color?, points:[{x,y,meta?}]}]`（`color` 缺省按调色板循环）；
 * `xLabel` / `yLabel`：轴含义与单位，画在坐标轴上、也用在悬停提示里（如「行权价」「IV %」）；
 * `height`：画布高度（缺省 150，IV 微笑建议 260 才看得出弧度）；
 * `minPoints`：有效点总数低于它就不画，显示 `emptyText`（缺省 2 —— 一个点连不成线）；
 * `xFormat` / `yFormat`：刻度与提示里的数值文案（缺省 `axisNumberText`）。
 *
 * 悬停：十字虚线 + 最近点高亮 + 信息框（第一行系列名，然后 x/y，再逐行 meta）。
 * 提示框文字会按框宽截断，框体自动避让画布边界。
 */
export function ScatterChart({
  series, xLabel = "", yLabel = "", height, emptyText = "", minPoints = 2,
  xFormat = axisNumberText, yFormat = axisNumberText,
}) {
  const list = Array.isArray(series) ? series : [];
  const prepared = list.map((one, index) => ({
    key: one?.key ?? `series-${index}`,
    label: one?.label ?? `系列${index + 1}`,
    color: one?.color || paletteColor(index),
    points: validPoints(one?.points),
    total: Array.isArray(one?.points) ? one.points.length : 0,
  }));
  const drawn = prepared.filter((one) => one.points.length > 0);
  const drawnCount = drawn.reduce((sum, one) => sum + one.points.length, 0);
  const skipped = prepared.reduce((sum, one) => sum + (one.total - one.points.length), 0);
  const floor = Number.isFinite(minPoints) ? minPoints : 2;
  const tooFew = drawnCount < floor;
  const canvasHeight = Number.isFinite(height) ? height : CHART_STYLE_SMALL.height;

  const [hover, setHover] = React.useState(null);   // {seriesIndex, pointIndex} | null
  const geometry = React.useRef(null);

  // 回调的 color 就是 useCanvasChart 内部取的 themeColors()（暗色主题色表）
  const ref = useCanvasChart((ctx, width, chartHeight, color) => {
    geometry.current = null;
    if (tooFew) {
      // 空态必须说清原因（页面传进来的 emptyText 会写明「仅 N 行，把条数上限加到 100 以上」）
      drawEmptyText(ctx, color, {
        text: emptyText || `有效点仅 ${drawnCount} 个（少于 ${floor} 个），画不出形状`,
        width,
      });
      return;
    }
    const xs = drawn.flatMap((one) => one.points.map((entry) => entry.x));
    const ys = drawn.flatMap((one) => one.points.map((entry) => entry.y));
    const xTicks = niceTicks(Math.min(...xs), Math.max(...xs), 5);
    const yTicks = niceTicks(Math.min(...ys), Math.max(...ys), 4);
    ctx.font = DEFAULT_FONT;
    const measure = (text) => ctx.measureText(text).width;
    const padL = Math.max(...yTicks.ticks.map((value) => measure(String(yFormat(value))))) + 12;
    const padR = 12;
    const padT = 22;                       // 顶部留给图例与 y 轴含义
    const padB = 30;                       // 两行：刻度 + x 轴含义
    const plotW = Math.max(10, width - padL - padR);
    const plotH = Math.max(10, chartHeight - padT - padB);
    const x = linearScale([xTicks.min, xTicks.max], [padL, padL + plotW]);
    const y = linearScale([yTicks.min, yTicks.max], [padT + plotH, padT]);
    drawYGrid(ctx, color, {
      left: padL, right: padL + plotW, top: padT, bottom: padT + plotH,
      y, ticks: yTicks.ticks, format: yFormat,
    });
    drawXGrid(ctx, color, {
      left: padL, right: padL + plotW, top: padT, bottom: padT + plotH,
      x, ticks: xTicks.ticks, format: xFormat, width, labelY: chartHeight - 16,
    });
    // 轴含义与单位：横轴贴右下、纵轴贴左上（两处都在画布内）
    ctx.textAlign = "right";
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = color.text;
    if (xLabel) ctx.fillText(xLabel, width - 6, chartHeight - 4);
    ctx.textAlign = "left";
    if (yLabel) ctx.fillText(yLabel, 4, 12);
    drawLegend(ctx, color, drawn.map((one) => ({ label: one.label, color: one.color })),
      { right: width - padR, minLeft: padL });

    // 命中判定用的像素坐标：绘制与命中必须共用同一份几何（见 kline.jsx 同一口径）
    geometry.current = { padL, padT, plotW, plotH, hits: [] };
    drawn.forEach((one, seriesIndex) => {
      // 折线：按 x 升序；同系列内 x 相同的点按原顺序稳定排列
      const ordered = [...one.points].sort((a, b) => a.x - b.x);
      ctx.strokeStyle = one.color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ordered.forEach((entry, index) => {
        const px = x(entry.x);
        const py = y(entry.y);
        if (index === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
      one.points.forEach((entry, pointIndex) => {
        const px = x(entry.x);
        const py = y(entry.y);
        geometry.current.hits.push({ px, py, seriesIndex, pointIndex });
        const active = hover !== null && hover.seriesIndex === seriesIndex
          && hover.pointIndex === pointIndex;
        ctx.beginPath();
        ctx.arc(px, py, active ? 4.5 : 2.5, 0, Math.PI * 2);
        ctx.fillStyle = one.color;
        ctx.fill();
        if (active) {
          ctx.strokeStyle = color.text;
          ctx.lineWidth = 1.5;
          ctx.stroke();
        }
      });
    });

    // 悬停：十字虚线 + 信息框（画在最上层，避免被点线盖住）
    if (hover) {
      const one = drawn[hover.seriesIndex];
      const entry = one?.points[hover.pointIndex];
      const px = x(entry.x);
      const py = y(entry.y);
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = color.line;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px, padT);
      ctx.lineTo(px, padT + plotH);
      ctx.moveTo(padL, py);
      ctx.lineTo(padL + plotW, py);
      ctx.stroke();
      ctx.setLineDash([]);
      drawTooltipBox(ctx, color, {
        lines: [
          one.label,
          `${xLabel || "x"} ${xFormat(entry.x)}`,
          `${yLabel || "y"} ${yFormat(entry.y)}`,
          ...metaLines(entry.point?.meta),
        ],
        anchorX: px, anchorY: py, width, height: chartHeight,
      });
    }
  }, [prepared, hover, tooFew, emptyText, floor, canvasHeight, xLabel, yLabel, xFormat, yFormat]);

  const handleMove = (event) => {
    const shape = geometry.current;
    const canvas = event?.currentTarget;
    if (!shape || !canvas || typeof canvas.getBoundingClientRect !== "function") return;
    const rect = canvas.getBoundingClientRect();
    const mx = event.clientX - rect.left;
    const my = event.clientY - rect.top;
    let best = null;
    let bestDistance = HOVER_RADIUS;
    for (const hit of shape.hits) {
      const distance = Math.hypot(hit.px - mx, hit.py - my);
      if (distance <= bestDistance) { best = hit; bestDistance = distance; }
    }
    const next = best ? { seriesIndex: best.seriesIndex, pointIndex: best.pointIndex } : null;
    const changed = (next === null) !== (hover === null)
      || (next !== null && hover !== null
        && (next.seriesIndex !== hover.seriesIndex || next.pointIndex !== hover.pointIndex));
    if (changed) setHover(next);
  };

  return (
    <>
      <canvas ref={ref} style={{ ...CHART_STYLE_SMALL, height: canvasHeight }}
        onMouseMove={handleMove} onMouseLeave={() => setHover(null)} />
      {skipped > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {`画入 ${drawnCount} 点 · 跳过 ${skipped} 点（x 或 y 缺失/非数字，折线在此断开）`}
        </Typography.Text>)}
    </>);
}
