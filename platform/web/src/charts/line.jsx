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

// 多线图系列固定调色板：超大/大/中/小等类别线没有涨跌语义，取中亮度色在亮暗两种
// 底色上均可读；不取 themeColors().up/down（那是涨跌语义，暗色下红涨绿跌）。
const SERIES_PALETTE = ["#f5222d", "#fa8c16", "#1677ff", "#52c41a", "#722ed1"];

/** 多线图（自绘，无依赖）：series = [{label, values}]，labels 为共用 x 轴刻度文本
 *  （与每条 values 等长；非有限数值断线不画）。网格/文字用主题色，线用固定调色板。 */
export function MultiLineChart({ labels = [], series = [] }) {
  const ref = useCanvasChart((ctx, width, height, color) => {
    const usable = series.filter((line) => Array.isArray(line.values) && line.values.some(Number.isFinite));
    if (labels.length < 2 || !usable.length) {
      ctx.fillStyle = color.text; ctx.font = "12px sans-serif";
      ctx.fillText("暂无序列数据", 12, 22); return;
    }
    const padL = 56, padR = 12, padT = usable.length > 1 ? 22 : 10, padB = 18;
    const flat = usable.flatMap((line) => line.values).filter(Number.isFinite);
    let min = Math.min(...flat), max = Math.max(...flat);
    if (max - min < 1e-9) { max += 1; min -= 1; }
    const span = max - min; min -= span * 0.06; max += span * 0.06;
    const y = (v) => padT + (height - padT - padB) * (1 - (v - min) / (max - min));
    const x = (i) => padL + (width - padL - padR) * (i / (labels.length - 1));
    ctx.strokeStyle = color.grid; ctx.fillStyle = color.text; ctx.font = "10px sans-serif";
    for (let i = 0; i <= 4; i += 1) {
      const gy = Math.round(y(min + (max - min) * (i / 4))) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(width - padR, gy); ctx.stroke();
      ctx.fillText((min + (max - min) * (i / 4)).toFixed(2), 4, gy + 3);
    }
    usable.forEach((line, li) => {
      ctx.strokeStyle = SERIES_PALETTE[li % SERIES_PALETTE.length];
      ctx.lineWidth = 1.5; ctx.beginPath();
      let started = false;
      line.values.forEach((v, i) => {
        if (!Number.isFinite(v)) { started = false; return; }
        if (!started) { ctx.moveTo(x(i), y(v)); started = true; }
        else ctx.lineTo(x(i), y(v));
      });
      ctx.stroke();
    });
    // 图例：右上角一行；放不下时按序省略（文本仍以表格呈现，不丢事实）
    ctx.font = "10px sans-serif";
    let legendX = width - padR;
    for (let li = usable.length - 1; li >= 0; li -= 1) {
      const text = usable[li].label ?? `系列${li + 1}`;
      const textW = ctx.measureText(text).width;
      if (legendX - textW - 14 < padL) break;
      ctx.fillStyle = color.text;
      ctx.fillText(text, legendX - textW, 12);
      ctx.fillStyle = SERIES_PALETTE[li % SERIES_PALETTE.length];
      ctx.fillRect(legendX - textW - 11, 5, 8, 3);
      legendX -= textW + 20;
    }
    // x 轴刻度：首/中/尾三个标签（中点取整，可能与首尾重合，重合时跳过中点）
    ctx.fillStyle = color.text;
    ctx.fillText(String(labels[0]), padL, height - 4);
    const mid = Math.floor((labels.length - 1) / 2);
    if (mid > 0 && mid < labels.length - 1) {
      const midText = String(labels[mid]);
      ctx.fillText(midText, Math.min(x(mid), width - padR - ctx.measureText(midText).width), height - 4);
    }
    const tail = String(labels[labels.length - 1]);
    ctx.fillText(tail, width - padR - ctx.measureText(tail).width, height - 4);
  }, [labels, series]);
  return <canvas ref={ref} style={CHART_STYLE_SMALL} />;
}
