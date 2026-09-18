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

/**
 * 右边缘价格标签的矩形：**右边缘贴住绘图区右边、向左展开**。
 *
 * 为什么必须向左（2026-09-18 实机缺陷）：原实现在 `x = width − padR` 处**向右**画，
 * 而画布到 `width` 就结束——标签宽度约 28–46px，远大于 `padR`（12），于是右半截被裁掉，
 * 实机表现为「悬停某日后右侧收盘价只显示一位」（实测 8.96 只画出「8」）。
 * 右侧贴边元素一律「向内生长」，与 `line.jsx` 的图例（`legendX - textW`）和 x 轴尾标签
 * （`width - padR - measureText`）同一口径。抽成纯函数是为了让「不越界」可被单测锁住
 * ——canvas 自绘没有 DOM 可供断言，几何是这里唯一可测的地方。
 *
 * 取值口径：``tagW`` 由调用方用 ``measureText`` 量出（含左右内边距）；``centerY`` 是标签
 * 垂直中心（对齐收盘价的 y）。返回的矩形保证落在 ``[0,width] × [0,height]`` 内：
 * 极窄画布下 ``x`` 收到 0，贴顶/贴底时 ``y`` 收回画布内——宁可压边，也不能让价格数字被裁。
 */
export function priceTagRect(width, height, padR, centerY, tagW, tagH = 14) {
  const w = Math.max(0, tagW);
  const h = Math.max(0, tagH);
  const x = Math.max(0, width - padR - w);
  const y = Math.min(Math.max(0, centerY - h / 2), Math.max(0, height - h));
  return { x, y, w, h };
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
