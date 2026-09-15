// 图表几何纯函数。移植自 plugins/workbench/src/client.js L171-197（逻辑零改动，
// 仅抽出为独立模块；缺失值显示"—"的语义保持不变）。

/** 鼠标 x 坐标 → K 线下标；落在绘图区外返回 null。 */
export function barIndexAt(x, { padL, plotW, count }) {
  if (!count || plotW <= 0) return null;
  const step = plotW / count;
  const index = Math.floor((x - padL) / step);
  return index >= 0 && index < count ? index : null;
}

/** 信息框左上角 x：右侧放不下就翻到光标左侧，再不行贴右边缘。 */
export function tooltipLeft(cursorX, boxWidth, chartWidth, gap = 14) {
  const right = cursorX + gap;
  if (right + boxWidth <= chartWidth - 4) return right;
  const left = cursorX - gap - boxWidth;
  return left >= 4 ? left : Math.max(4, chartWidth - 4 - boxWidth);
}

/** 成交量等大数的紧凑写法（图内空间有限）。 */
export function compactNumber(value) {
  // 注意 Number(null) === 0、Number("") === 0 —— 缺失必须显示为"—"，
  // 否则"没有成交量"会被显示成"成交量 0"，是两回事。
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  const abs = Math.abs(number);
  if (abs >= 1e8) return `${(number / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(number / 1e4).toFixed(2)}万`;
  return number.toLocaleString("en-US", { maximumFractionDigits: 0 });
}
