// 作业链时间轴（第一批共用原语，2026-09-18）：横轴是时间，每行一个「有起止时刻的项」。
//
// 数据形状：`items = [{label, start, end, status?, note?}]`
//   * 流程页「流程时间轴」的来源：pipeline 端点的阶段行
//     `{label, status, at, scheduled, summary}` —— 页面把 `at`（实际执行）或
//     `scheduled`（计划时刻）映射成 `start`/`end`；只有一个时刻时只给 `start`，
//     本组件按**瞬时点**画（最小条宽 2.5px，否则「跑过一次」会被画成什么都没有）。
//   * `status`：ok 绿 / failed 红 / skipped 灰 / pending 蓝（语义与调度页的 antd Tag 一致，
//     见 canvas.js 的 STATUS_COLORS）；未知状态回落主题色。
//   * `note`：悬停时显示（阶段摘要/失败原因），不画在轨道上，避免与时间条重叠。
//
// 与 bars.jsx 相同的两条纪律：
//   1. **缺时刻的项不画在 0 位**：`timelineLayout` 对缺时刻返回 `null` 偏移，本组件跳过
//      这些项并在图下如实标注条数——把「没有时刻」画成「从起点开始」，等于把没跑过的
//      阶段画成跑过了；
//   2. 行名/时间文字按可用宽度收敛（`truncateText`），绝不越出画布。
import React from "react";
import { Typography } from "antd";
import { useCanvasChart } from "./line.jsx";
import { CHART_STYLE_SMALL } from "./theme.js";
import { DEFAULT_FONT, chartStatusColor, drawEmptyText, drawTooltipBox } from "./canvas.js";
import { truncateText } from "./geometry.js";
import { linearScale, timelineLayout, toTimestamp } from "./scale.js";

/** 状态中文文案（与 services/pipeline.js 的 STATUS_TEXT 同一套词；只用于悬停提示）。 */
const STATUS_TEXT = { ok: "已完成", pending: "待运行", skipped: "已跳过", failed: "失败" };

/** 时间戳 → 「MM-DD HH:mm」（本地时区，与页面其他地方的时间显示口径一致）。 */
function defaultTimeFormat(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (number) => String(number).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * 时间轴：每行左侧是项名、右侧是时间条（横轴为时间）。
 *
 * `items`：`[{label, start, end, status?, note?}]`；`start`/`end` 可为毫秒数、`Date` 或
 *   可解析字符串（无时区偏移的写法按**运行环境本地时区**解释，见 scale.js 的 `toTimestamp`）；
 * `height`：画布高度（缺省按行数给 `max(150, 26×条数 + 26)`）；
 * `emptyText`：没有任何可画项时的原因文案；
 * `timeFormat`：轴与提示里的时间文案（默认「MM-DD HH:mm」）；
 * `labelWidth`：行名列宽（缺省按最长行名，上限画布 40%）。
 *
 * 时间域取全部时刻的最小/最大值（与 `timelineLayout` 的缺省域同源），轴上标首/中/尾三个
 * 时刻；域内只有一个时刻时三项重合，只画得下一个。悬停显示项名、起止时刻、状态与 `note`。
 */
export function TimelineChart({
  items, height, emptyText = "", timeFormat = defaultTimeFormat, labelWidth,
}) {
  const all = Array.isArray(items) ? items : [];
  const layouts = timelineLayout(all);
  const stamps = all.map((item) => ({
    start: toTimestamp(item ? item.start : null),
    end: toTimestamp(item ? item.end : null),
  }));
  const entries = layouts
    .map((row, index) => ({ row, item: all[index], stamp: stamps[index] }))
    .filter((entry) => entry.row.valid);
  const skipped = layouts.length - entries.length;
  const clamped = entries.filter((entry) => entry.row.clamped).length;
  const known = stamps.flatMap((stamp) => [stamp.start, stamp.end])
    .filter((value) => value !== null);
  const domain = known.length ? [Math.min(...known), Math.max(...known)] : null;
  const canvasHeight = Number.isFinite(height)
    ? height
    : Math.max(CHART_STYLE_SMALL.height, 26 * entries.length + 26);

  const [hover, setHover] = React.useState(null);
  const geometry = React.useRef(null);

  // 回调的 color 就是 useCanvasChart 内部取的 themeColors()（暗色主题色表）
  const ref = useCanvasChart((ctx, width, chartHeight, color) => {
    geometry.current = null;
    if (entries.length === 0) {
      drawEmptyText(ctx, color, { text: emptyText, width });
      return;
    }
    ctx.font = DEFAULT_FONT;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    const measure = (text) => ctx.measureText(text).width;
    const labels = entries.map((entry) => String(entry.item?.label ?? ""));
    const widest = Math.max(...labels.map(measure));
    const nameWidth = Number.isFinite(labelWidth)
      ? labelWidth
      : Math.min(widest + 8, Math.max(40, width * 0.4));
    const padL = 8 + nameWidth;
    const padR = 14;
    const padT = 12;
    const padB = 24;
    const plotW = Math.max(10, width - padL - padR);
    const plotH = Math.max(10, chartHeight - padT - padB);
    // 横轴：比例 [0,1] → 像素；轴标签再由时间域反推成时刻（与行条同一套像素）
    const x = linearScale([0, 1], [padL, padL + plotW]);
    if (domain) {
      let lastRight = -Infinity;
      ctx.save();
      [0, 0.5, 1].forEach((ratio) => {
        const text = timeFormat(domain[0] + (domain[1] - domain[0]) * ratio);
        const textW = measure(text);
        const left = Math.min(Math.max(2, x(ratio) - textW / 2), Math.max(2, width - 2 - textW));
        if (left < lastRight + 8) return;      // 放不下就省略（域退化时三项重合）
        ctx.strokeStyle = color.grid;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(Math.round(x(ratio)) + 0.5, padT);
        ctx.lineTo(Math.round(x(ratio)) + 0.5, padT + plotH);
        ctx.stroke();
        ctx.fillStyle = color.text;
        ctx.fillText(text, left, chartHeight - 8);
        lastRight = left + textW;
      });
      ctx.restore();
    }
    const slot = plotH / entries.length;
    const barH = Math.max(4, Math.min(12, slot * 0.45));
    geometry.current = { rows: [] };
    entries.forEach((entry, index) => {
      const centerY = padT + slot * index + slot / 2;
      const fromX = x(entry.row.from);
      const toX = Math.max(x(entry.row.to), fromX + 2.5);
      ctx.textAlign = "left";
      ctx.fillStyle = color.text;
      ctx.fillText(truncateText(labels[index], nameWidth, measure), 6, centerY);
      // 轨道底线：让「时间条很短」的项也能看出整行的时间范围
      ctx.strokeStyle = color.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, Math.round(centerY) + 0.5);
      ctx.lineTo(padL + plotW, Math.round(centerY) + 0.5);
      ctx.stroke();
      ctx.fillStyle = chartStatusColor(entry.item?.status, color.line);
      ctx.fillRect(fromX, centerY - barH / 2, Math.max(2.5, toX - fromX), barH);
      geometry.current.rows.push({ centerY, index });
    });
    if (hover !== null && geometry.current.rows[hover]) {
      const entry = entries[hover];
      const barFrom = x(entry.row.from);
      const barTo = Math.max(x(entry.row.to), barFrom + 2.5);
      const startMs = entry.stamp.start ?? entry.stamp.end;
      const endMs = entry.stamp.end ?? entry.stamp.start;
      const lines = [
        String(entry.item?.label ?? ""),
        `${timeFormat(startMs)} → ${timeFormat(endMs)}`,
        STATUS_TEXT[entry.item?.status]
          ?? (entry.item?.status ? String(entry.item.status) : "未运行（无状态字段）"),
      ];
      if (entry.item?.note) lines.push(String(entry.item.note));
      drawTooltipBox(ctx, color, {
        lines,
        anchorX: (barFrom + barTo) / 2 + 20,
        anchorY: geometry.current.rows[hover].centerY,
        width, height: chartHeight, top: 2,
      });
    }
  }, [entries, canvasHeight, emptyText, timeFormat, labelWidth, hover, domain?.[0], domain?.[1]]);

  const handleMove = (event) => {
    const shape = geometry.current;
    const canvas = event?.currentTarget;
    if (!shape || !canvas || typeof canvas.getBoundingClientRect !== "function") return;
    const rect = canvas.getBoundingClientRect();
    const my = event.clientY - rect.top;
    let best = null;
    let bestDistance = Infinity;
    shape.rows.forEach((row) => {
      const distance = Math.abs(row.centerY - my);
      if (distance < bestDistance) { bestDistance = distance; best = row.index; }
    });
    if (best !== hover) setHover(best);
  };

  return (
    <>
      <canvas ref={ref} style={{ ...CHART_STYLE_SMALL, height: canvasHeight }}
        onMouseMove={handleMove} onMouseLeave={() => setHover(null)} />
      {(skipped > 0 || clamped > 0) && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {skipped > 0 ? `画入 ${entries.length} 项 · 跳过 ${skipped} 项（缺 start/end 时刻，未画上轨道）` : ""}
          {skipped > 0 && clamped > 0 ? " · " : ""}
          {clamped > 0 ? `${clamped} 项落在时间范围外，已收敛到两端` : ""}
        </Typography.Text>)}
    </>);
}
