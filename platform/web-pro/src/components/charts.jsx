// 工作台共享图表组件（纯 SVG，无额外依赖）：与设计稿版的图表形态一一对应——
// 折线/面积、K 线蜡烛+成交量、参数热力图。颜色取设计稿 token，保证两版观感一致。
import React from "react";

const TOKEN = {
  blue: "#4c8dff",
  green: "#3fb950",
  red: "#f8514d",
  amber: "#d9a112",
  cyan: "#39a0ed",
  faint: "#626d7c",
  muted: "#8b97a5",
  border: "#232b37",
};

/** 折线/面积图（组合净值、回测净值、暴露曲线）。values 为数值数组，labels 可选。 */
export function LineChart({ values, height = 180, color = TOKEN.blue, labels = [], area = true, name = "" }) {
  const nums = (values ?? []).map(Number).filter(Number.isFinite);
  if (nums.length < 2) return null;
  const width = 760;
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = max - min || 1;
  const x = (index) => (index / (nums.length - 1)) * (width - 8) + 4;
  const y = (value) => height - 16 - ((value - min) / span) * (height - 34);
  const path = nums.map((value, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", height }} role="img" aria-label={name}>
      {area ? (
        <path d={`${path} L${x(nums.length - 1).toFixed(1)},${height - 16} L4,${height - 16} Z`} fill="rgba(76,141,255,.10)" />
      ) : null}
      <path d={path} fill="none" stroke={color} strokeWidth="1.5" />
      <text x="6" y="14" fill={TOKEN.faint} fontSize="10">{max.toFixed(2)}</text>
      <text x="6" y={height - 4} fill={TOKEN.faint} fontSize="10">{min.toFixed(2)}</text>
      {labels.length >= 2 ? (
        <>
          <text x="6" y={height - 4} fill={TOKEN.faint} fontSize="10">{labels[0]}</text>
          <text x={width - 90} y={height - 4} fill={TOKEN.faint} fontSize="10">{labels.at(-1)}</text>
        </>
      ) : null}
    </svg>
  );
}

/** K 线：真实 OHLC 蜡烛 + 成交量柱（设计稿 market 页主图形态）。 */
export function CandleChart({ bars, height = 260, volumeRatio = 0.26 }) {
  const rows = (bars ?? []).filter((bar) => [bar.o, bar.h, bar.l, bar.c].every((v) => Number.isFinite(Number(v))));
  if (rows.length < 2) return null;
  const width = 860;
  const priceHeight = height * (1 - volumeRatio) - 12;
  const highs = rows.map((bar) => Number(bar.h));
  const lows = rows.map((bar) => Number(bar.l));
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const span = max - min || 1;
  const step = (width - 8) / rows.length;
  const bodyWidth = Math.max(2, Math.min(10, step * 0.62));
  const y = (value) => 8 + (1 - (value - min) / span) * priceHeight;
  const volumes = rows.map((bar) => Number(bar.v ?? 0));
  const volumeMax = Math.max(...volumes, 1);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} style={{ width: "100%", height }} role="img" aria-label="K 线">
      {rows.map((bar, index) => {
        const open = Number(bar.o);
        const close = Number(bar.c);
        const up = close >= open;
        const color = up ? TOKEN.green : TOKEN.red;
        const cx = 4 + index * step + step / 2;
        const top = y(Math.max(open, close));
        const bottom = y(Math.min(open, close));
        const volumeHeight = Math.max(1, (volumes[index] / volumeMax) * (height * volumeRatio - 8));
        return (
          <g key={`${bar.t ?? index}`}>
            <line x1={cx} y1={y(Number(bar.h))} x2={cx} y2={y(Number(bar.l))} stroke={color} strokeWidth="1" />
            <rect x={cx - bodyWidth / 2} y={top} width={bodyWidth} height={Math.max(1, bottom - top)}
              fill={up ? "rgba(63,185,80,.85)" : "rgba(248,81,77,.85)"} />
            <rect x={cx - bodyWidth / 2} y={height - volumeHeight} width={bodyWidth} height={volumeHeight}
              fill={up ? "rgba(63,185,80,.35)" : "rgba(248,81,77,.35)"} />
          </g>
        );
      })}
      <text x="6" y="12" fill={TOKEN.faint} fontSize="10">{max.toFixed(2)}</text>
      <text x="6" y={(8 + priceHeight).toFixed(1)} fill={TOKEN.faint} fontSize="10">{min.toFixed(2)}</text>
    </svg>
  );
}

/** 参数热力图：grid=[{window,rebalanceDays,sharpe}]（设计稿 strategy 页形态，夏普色阶）。 */
export function Heatmap({ grid, rowKey = "rebalanceDays", colKey = "window", valueKey = "sharpe" }) {
  const rows = (grid ?? []).filter((cell) => Number.isFinite(Number(cell?.[valueKey])));
  if (rows.length === 0) return null;
  const cols = [...new Set(rows.map((cell) => cell[colKey]))].sort((a, b) => a - b);
  const rowKeys = [...new Set(rows.map((cell) => cell[rowKey]))].sort((a, b) => a - b);
  const values = rows.map((cell) => Number(cell[valueKey]));
  const max = Math.max(...values);
  const min = Math.min(...values);
  const span = max - min || 1;
  const colorOf = (value) => {
    const t = (value - min) / span;
    return `rgba(${Math.round(248 - 185 * t)},${Math.round(81 + 104 * t)},${Math.round(77 + 6 * t)},${(0.25 + 0.6 * t).toFixed(2)})`;
  };
  return (
    <div style={{ display: "grid", gridTemplateColumns: `88px repeat(${cols.length}, minmax(0, 1fr))`, gap: 4 }}>
      <div />
      {cols.map((col) => (
        <div key={`h-${col}`} style={{ textAlign: "center", fontSize: 11, color: TOKEN.faint }}>{`${colKey}=${col}`}</div>
      ))}
      {rowKeys.map((row) => (
        <React.Fragment key={`r-${row}`}>
          <div style={{ fontSize: 11, color: TOKEN.faint }}>{`${rowKey}=${row}`}</div>
          {cols.map((col) => {
            const hit = rows.find((cell) => cell[rowKey] === row && cell[colKey] === col);
            if (!hit) {
              return <div key={`c-${row}-${col}`} style={{ border: `1px dashed ${TOKEN.border}`, borderRadius: 4, padding: "6px 4px", textAlign: "center", fontSize: 11, color: TOKEN.faint }}>—</div>;
            }
            const value = Number(hit[valueKey]);
            return (
              <div key={`c-${row}-${col}`} title={`${rowKey}=${row} ${colKey}=${col} ${valueKey}=${value}`}
                style={{ background: colorOf(value), borderRadius: 4, padding: "6px 4px", textAlign: "center", fontSize: 11, fontVariantNumeric: "tabular-nums" }}>
                {value.toFixed(2)}
              </div>
            );
          })}
        </React.Fragment>
      ))}
    </div>
  );
}

/** 横向条形：工具调用分布 / 暴露权重（设计稿 bars 形态）。 */
export function BarList({ items, color = TOKEN.blue, max: maxProp = null }) {
  const rows = (items ?? []).filter(([, value]) => Number.isFinite(Number(value)));
  if (rows.length === 0) return null;
  const max = maxProp ?? Math.max(...rows.map(([, value]) => Number(value))) ?? 1;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {rows.map(([label, value]) => (
        <div key={String(label)} style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ width: 220, flex: "none", fontSize: 11, color: TOKEN.muted, fontFamily: "ui-monospace, Menlo, monospace" }}>{label}</span>
          <span style={{ flex: 1, background: "#171c26", borderRadius: 3, height: 10, overflow: "hidden" }}>
            <span style={{ display: "block", height: "100%", width: `${((Number(value) / (max || 1)) * 100).toFixed(1)}%`, background: color }} />
          </span>
          <span style={{ width: 64, flex: "none", textAlign: "right", fontSize: 11, color: TOKEN.muted }}>{value}</span>
        </div>
      ))}
    </div>
  );
}

export { TOKEN as CHART_TOKEN };
