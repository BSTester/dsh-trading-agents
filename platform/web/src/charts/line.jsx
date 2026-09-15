// 折线图 + canvas 绘制原语。移植自 plugins/workbench/src/client.js：
// useCanvasChart L317-340、movingAverage L342-351、LineChart L353-383。
// 适配：React 改 import；h() 改 JSX；tw-* className 改内联样式（见 theme.js）。逻辑零改动。
import React from "react";
import { themeColors, CHART_STYLE_SMALL } from "./theme.js";

export function useCanvasChart(draw, deps) {
  const ref = React.useRef(null);
  React.useEffect(() => {
    const canvas = ref.current;
    if (!canvas || typeof canvas.getContext !== "function") return;
    const dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(rect.width, 200);
    const height = Math.max(rect.height, 80);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    try {
      draw(ctx, width, height, themeColors());
    } catch (error) {
      ctx.fillStyle = themeColors().down;
      ctx.font = "12px sans-serif";
      ctx.fillText(`绘图失败：${String(error.message).slice(0, 80)}`, 10, 20);
    }
  }, deps);
  return ref;
}

export function movingAverage(bars, n) {
  const out = new Array(bars.length).fill(null);
  let sum = 0;
  for (let i = 0; i < bars.length; i += 1) {
    sum += bars[i].c;
    if (i >= n) sum -= bars[i - n].c;
    if (i >= n - 1) out[i] = sum / n;
  }
  return out;
}

export function LineChart({ points, label = "" }) {
  const ref = useCanvasChart((ctx, width, height, color) => {
    if (!points || points.length < 2) {
      ctx.fillStyle = color.text; ctx.font = "12px sans-serif";
      ctx.fillText("暂无序列数据", 12, 22); return;
    }
    const padL = 46, padR = 12, padT = 10, padB = 18;
    const values = points.map((p) => p.v);
    let min = Math.min(...values), max = Math.max(...values);
    if (max - min < 1e-9) { max += 1; min -= 1; }
    const span = max - min; min -= span * 0.06; max += span * 0.06;
    const y = (v) => padT + (height - padT - padB) * (1 - (v - min) / (max - min));
    const x = (i) => padL + (width - padL - padR) * (i / (points.length - 1));
    ctx.strokeStyle = color.grid; ctx.fillStyle = color.text; ctx.font = "10px sans-serif";
    for (let i = 0; i <= 4; i += 1) {
      const v = min + (max - min) * (i / 4);
      const gy = Math.round(y(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(width - padR, gy); ctx.stroke();
      ctx.fillText(v.toFixed(2), 4, gy + 3);
    }
    const rising = values[values.length - 1] >= values[0];
    ctx.strokeStyle = rising ? color.up : color.down; ctx.lineWidth = 1.8; ctx.beginPath();
    points.forEach((p, i) => { if (i === 0) ctx.moveTo(x(i), y(p.v)); else ctx.lineTo(x(i), y(p.v)); });
    ctx.stroke();
    ctx.globalAlpha = 0.12; ctx.fillStyle = ctx.strokeStyle;
    ctx.lineTo(x(points.length - 1), height - padB); ctx.lineTo(x(0), height - padB); ctx.closePath(); ctx.fill();
    ctx.globalAlpha = 1;
    if (label) { ctx.fillStyle = color.text; ctx.fillText(label, padL, height - 4); }
  }, [points, label]);
  return <canvas ref={ref} style={CHART_STYLE_SMALL} />;
}
