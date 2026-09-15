// 热力图。移植自 plugins/workbench/src/client.js L533-567（HeatmapChart）。
// 适配：React 改 import；h() 改 JSX；tw-* className 改内联样式（见 theme.js）。逻辑零改动。
import React from "react";
import { useCanvasChart, CHART_STYLE } from "./line.jsx";

export function HeatmapChart({ rowLabels, colLabels, tickers, matrix, unit = "" }) {
  const rows = rowLabels ?? tickers ?? [];
  const cols = colLabels ?? tickers ?? [];
  const ref = useCanvasChart((ctx, width, height, color) => {
    if (!rows.length || !matrix || !matrix.length) {
      ctx.fillStyle = color.text; ctx.font = "12px sans-serif";
      ctx.fillText("暂无矩阵数据", 12, 22); return;
    }
    const padL = 54, padT = 24, padR = 10, padB = 10;
    const cell = Math.min((width - padL - padR) / cols.length, (height - padT - padB) / rows.length, 64);
    ctx.font = "10px sans-serif";
    let min = Infinity, max = -Infinity;
    matrix.forEach((line) => line.forEach((v) => { if (v !== null && v !== undefined) { min = Math.min(min, v); max = Math.max(max, v); } }));
    if (!Number.isFinite(min)) { min = 0; max = 1; }
    const span = max - min || 1;
    rows.forEach((label, i) => { ctx.fillStyle = color.text; ctx.fillText(String(label), 4, padT + cell * i + cell / 2 + 3); });
    cols.forEach((label, j) => { ctx.fillStyle = color.text; ctx.fillText(String(label), padL + cell * j + 2, padT - 8); });
    matrix.forEach((line, i) => line.forEach((value, j) => {
      const x = padL + cell * j, y = padT + cell * i;
      if (value === null || value === undefined) {
        ctx.fillStyle = color.grid; ctx.fillRect(x, y, cell - 2, cell - 2);
        ctx.fillStyle = color.text; ctx.fillText("--", x + 4, y + cell / 2 + 3);
        return;
      }
      const strength = (value - min) / span;
      ctx.fillStyle = value >= 0 ? color.up : color.down;
      ctx.globalAlpha = 0.12 + strength * 0.78;
      ctx.fillRect(x, y, cell - 2, cell - 2);
      ctx.globalAlpha = 1;
      ctx.fillStyle = strength > 0.6 ? "#fff" : color.text;
      ctx.fillText(value.toFixed(2) + unit, x + 3, y + cell / 2 + 3);
    }));
  }, [rows.join(","), cols.join(","), JSON.stringify(matrix)]);
  return <canvas ref={ref} style={CHART_STYLE} />;
}
