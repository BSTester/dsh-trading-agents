// K 线蜡烛图（canvas 自绘，不引入依赖）。移植自 plugins/workbench/src/client.js：
// KLINE_PAD L167-168、drawKLineHover L418-463、KLineChart L465-531。
// 适配：React 改 import；h() 改 JSX；tw-* className 改内联样式；
// barIndexAt/tooltipLeft/compactNumber 从 ./geometry.js import（client.js 本地定义不再复制）。逻辑零改动。
import React from "react";
import { useCanvasChart, CHART_STYLE } from "./line.jsx";
import { barIndexAt, tooltipLeft, compactNumber } from "./geometry.js";

/** K 线图的绘制留白：命中判定与绘制共用同一套，避免两处各写一份而错位。 */
const KLINE_PAD = { padL: 54, padR: 12, padT: 10, padB: 18 };

/** 悬停时的准星、价格标签与信息框。 */
function drawKLineHover(ctx, width, height, color, bar, index, geometry, bars) {
  const { padL, padR, padT, padB, plotW, plotH, min, max } = geometry;
  const y = (value) => padT + plotH * (1 - (value - min) / (max - min));
  const step = plotW / bars.length;
  const centerX = padL + step * (index + 0.5);

  // 竖向准星
  ctx.setLineDash([3, 3]); ctx.strokeStyle = color.line; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(centerX, padT); ctx.lineTo(centerX, height - padB); ctx.stroke();
  ctx.setLineDash([]);

  // 右侧价格标签（对齐收盘价）
  const priceY = y(bar.c);
  const priceText = bar.c.toFixed(2);
  ctx.font = "10px sans-serif";
  const tagW = ctx.measureText(priceText).width + 8;
  ctx.fillStyle = bar.c >= bar.o ? color.up : color.down;
  ctx.fillRect(width - padR, priceY - 7, tagW, 14);
  ctx.fillStyle = "#fff";
  ctx.fillText(priceText, width - padR + 4, priceY + 3);

  // 信息框：日期 + 开高低收 + 涨跌 + 量
  const previous = index > 0 ? bars[index - 1] : null;
  const change = previous && previous.c ? ((bar.c - previous.c) / previous.c) * 100 : null;
  const lines = [
    String(bar.t),
    `开 ${bar.o}   高 ${bar.h}`,
    `低 ${bar.l}   收 ${bar.c}`,
    change === null ? `量 ${compactNumber(bar.v)}`
      : `涨跌 ${change >= 0 ? "+" : ""}${change.toFixed(2)}%   量 ${compactNumber(bar.v)}`,
  ];
  ctx.font = "11px sans-serif";
  const boxW = Math.max(...lines.map((line) => ctx.measureText(line).width)) + 16;
  const boxH = lines.length * 15 + 10;
  const boxX = tooltipLeft(centerX, boxW, width);
  let boxY = padT + 4;
  if (boxY + boxH > height - padB) boxY = Math.max(4, height - padB - boxH);
  ctx.fillStyle = "rgba(20,20,24,.92)";
  ctx.fillRect(boxX, boxY, boxW, boxH);
  ctx.strokeStyle = color.grid; ctx.lineWidth = 1;
  ctx.strokeRect(boxX + 0.5, boxY + 0.5, boxW - 1, boxH - 1);
  ctx.fillStyle = "#f0f0f0";
  lines.forEach((line, row) => {
    ctx.fillText(line, boxX + 8, boxY + 16 + row * 15);
  });
}

/** K 线蜡烛图：canvas 自绘，不引入依赖。 */
export function KLineChart({ bars }) {
  const [hover, setHover] = React.useState(null);
  const geometry = React.useRef(null);

  const ref = useCanvasChart((ctx, width, height, color) => {
    geometry.current = null;
    if (!bars || bars.length < 2) {
      ctx.fillStyle = color.text; ctx.font = "12px sans-serif";
      ctx.fillText("暂无 K 线数据", 12, 22); return;
    }
    const { padL, padR, padT, padB } = KLINE_PAD;
    const plotW = width - padL - padR;
    const plotH = height - padT - padB;
    let min = Math.min(...bars.map((b) => b.l));
    let max = Math.max(...bars.map((b) => b.h));
    if (max - min < 1e-9) { max += 1; min -= 1; }
    const span = max - min; min -= span * 0.05; max += span * 0.05;
    const y = (value) => padT + plotH * (1 - (value - min) / (max - min));
    // 命中判定要用与绘制完全相同的几何量，因此在这里记下来
    geometry.current = { padL, padR, padT, padB, plotW, plotH, min, max, count: bars.length };

    ctx.strokeStyle = color.grid; ctx.fillStyle = color.text; ctx.font = "10px sans-serif";
    for (let i = 0; i <= 4; i += 1) {
      const gridY = Math.round(y(min + (max - min) * (i / 4))) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, gridY); ctx.lineTo(width - padR, gridY); ctx.stroke();
      ctx.fillText((min + (max - min) * (i / 4)).toFixed(2), 4, gridY + 3);
    }

    const step = plotW / bars.length;
    const bodyW = Math.max(1, Math.min(step * 0.68, 9));
    bars.forEach((bar, index) => {
      const centerX = padL + step * (index + 0.5);
      const rising = bar.c >= bar.o;
      const tone = rising ? color.up : color.down;
      ctx.strokeStyle = tone; ctx.fillStyle = tone; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(centerX, y(bar.h)); ctx.lineTo(centerX, y(bar.l)); ctx.stroke();
      const top = y(Math.max(bar.o, bar.c));
      const bottom = y(Math.min(bar.o, bar.c));
      ctx.fillRect(centerX - bodyW / 2, top, bodyW, Math.max(1, bottom - top));
    });

    const last = bars[bars.length - 1];
    ctx.setLineDash([4, 3]); ctx.strokeStyle = color.line; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, y(last.c)); ctx.lineTo(width - padR, y(last.c)); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color.text;
    ctx.fillText(String(bars[0].t).slice(0, 16), padL, height - 4);
    const tail = String(last.t).slice(0, 16);
    ctx.fillText(tail, width - padR - ctx.measureText(tail).width, height - 4);

    // 悬停：画在最上层，避免被 K 线盖住
    const bar = hover === null ? null : bars[hover];
    if (bar) drawKLineHover(ctx, width, height, color, bar, hover, geometry.current, bars);
  }, [bars, hover]);

  const handleMove = (event) => {
    const shape = geometry.current;
    const canvas = event?.currentTarget;
    if (!shape || !canvas || typeof canvas.getBoundingClientRect !== "function") return;
    const rect = canvas.getBoundingClientRect();
    const next = barIndexAt(event.clientX - rect.left, shape);
    if (next !== hover) setHover(next);   // 只在跨到另一根 K 线时才重绘
  };

  return (
    <canvas ref={ref} style={CHART_STYLE}
      onMouseMove={handleMove} onMouseLeave={() => setHover(null)} />
  );
}
