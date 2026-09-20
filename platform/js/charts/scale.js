// 图表标度纯函数（第一批共用原语，2026-09-18）：坐标轴刻度、线性映射、条形槽位、
// 环图扇区、时间轴位置。
//
// 为什么单独一个模块：canvas 在 node 里画不出来，也断言不了像素——**标度与几何是本仓库
// 唯一能在 CI 里锁住的地方**（与 geometry.js 同一口径）。组件只做「把这里的归一化结果
// 乘上像素尺寸」，不在组件里算坐标，否则「刻度没包住数据」「缺值被当成 0」这类错误
// 在浏览器里只表现为一张看着正常的假图（没有报错、没有白屏，只有数字是错的）。
//
// 全模块共享的一条口径：**缺失值不是 0**。`Number(null)` 与 `Number("")` 都是 0，
// 直接用会把「没有成交量」画成「成交量 0」；所有入口一律先过 `finiteNumber`，
// 缺失返回 `null` 并由调用方跳过 + 计数（宁缺毋假）。
//
// node --test 直测，见 platform/web/tests/scale.test.mjs。

/** 归一化角度/比例等浮点结果，抹掉 `0.1*3 = 0.30000000000000004` 这类噪声。 */
function roundish(value) {
  return Number(value.toPrecision(12));
}

function clamp01(value) {
  if (!Number.isFinite(value)) return 0;
  return Math.min(1, Math.max(0, value));
}

/**
 * 严格数值归一：数字与「非空数字串」→ number；其余（null/undefined/空串/NaN/Infinity/
 * 布尔/对象/数组）→ null。
 *
 * 边界口径：`""`、`"   "`、`null` 都是**缺失**而不是 0（上游把缺失字段给成 null、
 * 成交量给成数字字符串 `"350107"`，两种都要正确处理）；`0` 是有效数值。
 */
export function finiteNumber(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") {
    const text = value.trim();
    if (text === "") return null;
    const parsed = Number(text);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

/**
 * 时刻归一（时间轴专用，导出给组件推导时间域用）：毫秒时间戳（数字或纯数字串）、`Date`、以及 `Date.parse`
 * 可解析的字符串（`2026-09-18T09:30:00Z`、`2026-09-18 09:30:00`）→ 毫秒数；其余 → null。
 *
 * 边界口径：
 *   * 纯数字串按**毫秒时间戳**解释（不是 `20260918` 这种日期压缩串）；
 *   * **没有时区偏移的字符串按运行环境本地时区解释**（与 `new Date("...")` 同一口径，
 *     实测 `Date.parse("1970-01-01 00:30:00")` 在 UTC+8 环境是 -25200000 而非 1800000）。
 *     写 `Z` 才是 UTC——同一批数据**不要混用两种写法**，否则整根轨道会平移一个时区差。
 */
export function toTimestamp(value) {
  if (value instanceof Date) {
    const time = value.getTime();
    return Number.isFinite(time) ? time : null;
  }
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string" || value.trim() === "") return null;
  const text = value.trim();
  const asNumber = Number(text);
  if (Number.isFinite(asNumber)) return asNumber;
  const parsed = Date.parse(text);
  if (Number.isFinite(parsed)) return parsed;
  const spaced = Date.parse(text.replace(" ", "T"));
  return Number.isFinite(spaced) ? spaced : null;
}

/** 「漂亮」步长：1/2/2.5/5/10 × 10^n（坐标轴刻度只用这几个因数才读得顺）。 */
function niceStep(span, count) {
  const rough = span / Math.max(1, count);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  const factor = normalized <= 1 ? 1
    : normalized <= 2 ? 2
      : normalized <= 2.5 ? 2.5
        : normalized <= 5 ? 5 : 10;
  return factor * magnitude;
}

const DEFAULT_TICK_COUNT = 5;
const MAX_TICK_COUNT = 50;

/**
 * 坐标轴刻度：给一个「整齐」的区间与步长，返回 `{min, max, step, ticks}`。
 *
 * 调用方用 `min/max` 建立 linearScale、用 `ticks` 画网格线与标签；刻度区间**保证包住
 * 原始区间**（否则最高/最低的点会落在绘图区外被裁掉——2026-09-18 K 线右边缘标签裁切
 * 的同类问题）。
 *
 * 边界兜底：
 *   * `min > max` → 自动交换（图不会上下颠倒）；
 *   * `min == max`（含全 0）→ 围绕该值撑开：值为 0 时给 `[0,1]`，非 0 时给 ±10% 的对称
 *     区间（绝不返回零宽区间，否则后续除零变 NaN/Infinity）；
 *   * 只有一边有限 → 当成退化区间围绕那一边撑开（**不把缺失当成 0**）；
 *   * 两边都不是有限数 → `[0,1]`；
 *   * `count` 非正/非有限/字符串 → 回落默认 5，并收敛到 `[2, 50]`，刻度数因此有上限。
 */
export function niceTicks(min, max, count = DEFAULT_TICK_COUNT) {
  const requested = finiteNumber(count);
  const wanted = Math.max(2, Math.min(MAX_TICK_COUNT,
    Math.trunc(requested === null ? DEFAULT_TICK_COUNT : requested)));
  let lo = finiteNumber(min);
  let hi = finiteNumber(max);
  if (lo === null && hi === null) { lo = 0; hi = 1; } else if (lo === null) lo = hi;
  else if (hi === null) hi = lo;
  if (lo > hi) { const swap = lo; lo = hi; hi = swap; }
  if (hi - lo < 1e-12) {
    if (Math.abs(hi) < 1e-12) { lo = 0; hi = 1; } else {
      const pad = Math.abs(hi) * 0.1;
      lo = hi - pad;
      hi += pad;
    }
  }
  const step = niceStep(hi - lo, wanted);
  const start = Math.floor(lo / step) * step;
  const end = Math.ceil(hi / step) * step;
  const span = Math.round((end - start) / step);
  const total = Number.isFinite(span) ? Math.max(0, Math.min(span, MAX_TICK_COUNT * 4)) : 0;
  const ticks = [];
  for (let i = 0; i <= total; i += 1) ticks.push(roundish(start + i * step));
  return { min: roundish(start), max: roundish(end), step, ticks };
}

/**
 * 线性映射：`domain`（数据域，两个数）→ `range`（像素域，两个数），返回 `(value) => number`。
 *
 * 边界兜底：
 *   * 域退化（`domain[0] === domain[1]`）→ **任何有限值都落在 range 中点**，不产生
 *     NaN/Infinity（退化域在实机里很常见：某组只有一个行权价、全 0 的量）；
 *   * `domain`/`range` 不是数组、元素缺失或非有限 → 按 `[0,1]` / `[0,1]` 兜底；
 *   * `domain` 反向（`d0 > d1`）→ 正常映射成单调递减（y 轴惯用倒序 range 同理由此覆盖）；
 *   * `value` 缺失或非有限 → 返回 **NaN**，由调用方跳过（不伪装成中点，那是在编数据）。
 */
export function linearScale(domain, range) {
  const dom = Array.isArray(domain) ? domain : [];
  const rng = Array.isArray(range) ? range : [];
  let d0 = finiteNumber(dom[0]);
  let d1 = finiteNumber(dom[1]);
  const r0 = finiteNumber(rng[0]);
  const r1 = finiteNumber(rng[1]);
  const out0 = r0 === null ? 0 : r0;
  const out1 = r1 === null ? 1 : r1;
  if (d0 === null && d1 === null) { d0 = 0; d1 = 1; } else if (d0 === null) d0 = d1;
  else if (d1 === null) d1 = d0;
  const span = d1 - d0;
  const degenerate = Math.abs(span) < 1e-12;
  const middle = (out0 + out1) / 2;
  return (value) => {
    const input = finiteNumber(value);
    if (input === null) return NaN;
    if (degenerate) return middle;
    return out0 + ((input - d0) / span) * (out1 - out0);
  };
}

/**
 * 条形布局（横向/竖向共用）：`values` → `[{index, value, valid, ratio, offset, extent,
 * zeroRatio, negative}]`，**全部归一化到 [0,1]**，组件乘上像素尺寸即可。
 *
 * 坐标口径（横向与竖向只是把这两个轴换个方向用）：
 *   * **长度轴**：`ratio = |value| / (max - min)`，收敛到 [0,1]；像素长度 = `ratio × 可用长度`。
 *   * **零基准**：`zeroRatio = (0 - min) / (max - min)`；正值从 `zeroRatio` 向 1 生长、
 *     负值从 `zeroRatio` 向 0 生长（VBar 正负分色就靠这个，而不是各自从底部长出来）。
 *   * **槽轴**：`offset = i/n + slot×gap/2`、`extent = slot×(1-gap)`（slot = 1/n）；
 *     槽位**按输入下标分配**，被跳过的条目仍占位，否则剩下的条会挤在一起与标签错位。
 *
 * `opts`：`max`/`min` 为长度轴上下界（缺省 `max = max(0, 数据最大值)`、
 * `min = min(0, 数据最小值)`）；`gap` 是槽内空隙比例。
 *
 * 边界兜底：
 *   * 缺失/非有限值 → 该条 `valid=false`、`value=null`、`ratio=0`（调用方据此跳过 + 计数）；
 *   * 全 0 / 全相等 / `max <= min` / `max` 非法 → 长度轴退化成 `[min, min+1]`，
 *     所有 `ratio` 有限（全 0 时条不可见，而不是除零变 Infinity）；
 *   * `ratio` 收敛到 [0,1]（超界值不会画出绘图区）；
 *   * `gap` 收敛到 `[0, 0.9]`；`values` 非数组 → `[]`。
 */
export function barLayout(values, opts = {}) {
  const list = Array.isArray(values) ? values : [];
  const count = list.length;
  if (count === 0) return [];
  const requestedGap = finiteNumber(opts.gap);
  const gap = requestedGap === null ? 0.2 : Math.min(0.9, Math.max(0, requestedGap));
  const numbers = list.map(finiteNumber);
  const finites = numbers.filter((value) => value !== null);
  const dataMin = finites.length ? Math.min(...finites) : 0;
  const dataMax = finites.length ? Math.max(...finites) : 0;
  let lo = finiteNumber(opts.min);
  let hi = finiteNumber(opts.max);
  if (lo === null) lo = Math.min(0, dataMin);
  if (hi === null) hi = Math.max(0, dataMax);
  if (!(hi > lo)) hi = lo + 1;
  const span = hi - lo;
  const slot = 1 / count;
  const zeroRatio = clamp01((0 - lo) / span);
  return numbers.map((value, index) => {
    const valid = value !== null;
    return {
      index,
      value: valid ? value : null,
      valid,
      // 归一化结果一律 roundish：`5 × (1/6) + (1/6 × 0.2)/2` 会给出 0.8500000000000001，
      // 直接透出去会让「同一套槽位」在不同调用点算出不同像素（也让断言只能写成近似）。
      ratio: valid ? roundish(Math.min(1, Math.abs(value) / span)) : 0,
      offset: roundish(index * slot + (slot * gap) / 2),
      extent: roundish(slot * (1 - gap)),
      zeroRatio: roundish(zeroRatio),
      negative: valid ? value < 0 : false,
    };
  });
}

/**
 * 环图扇区：`values` → `[{from, to, value, ratio, index}]`（弧度，`from/to` 顺时针）。
 *
 * 只有**有限且 > 0** 的值才产出扇区；0、负数、null/NaN/空串一律**不产出扇区**，
 * 输出里没有这一项（`index` 保留原下标，调用方用 `values.length - arcs.length` 得到
 * 跳过条数，并按 `index` 找回标签）。理由：把负数按绝对值画、或把全 0 画成一个 100%
 * 的整圆，都是**在图上编数据**——宁可空着并如实标注「跳过 N 项」。
 *
 * `opts.start`/`opts.end` 为弧度（缺省 `-π/2` 起、顺时针一整圈）；角度非法
 * （非有限、`end <= start`）时回落整圈。
 *
 * 边界兜底：总和为 0（全 0 / 全负 / 全缺失）或 `values` 非数组 → `[]`；
 * 最后一段的 `to` **精确等于 `end`**（逐段累加会有浮点误差，留下一条细缝）。
 */
export function donutArcs(values, opts = {}) {
  const list = Array.isArray(values) ? values : [];
  let start = finiteNumber(opts.start);
  let end = finiteNumber(opts.end);
  if (start === null) start = -Math.PI / 2;
  if (end === null) end = start + Math.PI * 2;
  if (!(end - start > 1e-9)) {
    start = -Math.PI / 2;
    end = start + Math.PI * 2;
  }
  const positives = list.map((value) => {
    const number = finiteNumber(value);
    return number !== null && number > 0 ? number : null;
  });
  const total = positives.reduce((sum, value) => sum + (value ?? 0), 0);
  if (!(total > 0)) return [];
  const sweep = end - start;
  const lastIndex = positives.reduce((last, value, index) => (value === null ? last : index), -1);
  const arcs = [];
  let cumulative = 0;
  positives.forEach((value, index) => {
    if (value === null) return;
    const ratio = value / total;
    const from = arcs.length === 0 ? start : arcs[arcs.length - 1].to;
    cumulative += ratio;
    const to = index === lastIndex ? end : start + cumulative * sweep;
    arcs.push({ from, to, value, ratio: roundish(ratio), index });
  });
  return arcs;
}

/**
 * 时间轴布局：把「有起止时刻的项」映射到 `[0,1]`，返回与 `items` **等长同序**的
 * `[{index, from, to, valid, clamped}]`。页面按下标取标签，所以绝不能过滤或重排。
 *
 * `items[i].start` / `items[i].end` 可以是毫秒数、`Date` 或可解析字符串（见 `toTimestamp`）。
 * `opts.start`/`opts.end` 为显式时间域，缺省由全部时刻推出（最小值 → 最大值）。
 *
 * 边界兜底（每一条都对应一种会被读错的情形）：
 *   * **两个时刻都缺 → `from = to = null`、`valid = false`**。这是本函数存在的核心理由：
 *     「没有时刻」与「时刻就是起点」在图上完全不是一回事，返回 0 会让一个从没跑过的
 *     阶段被画成「从开始就在跑」；
 *   * 只有一边有值 → 当成**瞬时点**（`from = to` 于该时刻），而不是半开区间；
 *   * `end < start`（上游给反了）→ 按 `[min, max]` 画，不画出负长度；
 *   * 时刻落在域外 → 收敛到 `[0,1]` 并置 `clamped = true`（页面据此如实标注被截断）；
 *   * 域退化（只有一个时刻 / `opts.start === opts.end`）→ 所有有效项放在轨道**中点 0.5**
 *     （贴左边缘会被读成「从起点开始」）；此时无时刻的项仍是 `null`；
 *   * `items` 非数组 → `[]`；元素为 null 时按「无时刻」处理。
 */
export function timelineLayout(items, opts = {}) {
  const list = Array.isArray(items) ? items : [];
  if (list.length === 0) return [];
  const times = list.map((item) => ({
    start: toTimestamp(item ? item.start : null),
    end: toTimestamp(item ? item.end : null),
  }));
  let lo = toTimestamp(opts.start);
  let hi = toTimestamp(opts.end);
  const known = times.flatMap(({ start, end }) => [start, end])
    .filter((value) => value !== null);
  if (lo === null) lo = known.length ? Math.min(...known) : null;
  if (hi === null) hi = known.length ? Math.max(...known) : null;
  const degenerate = lo === null || hi === null || !(hi - lo > 0);
  const span = degenerate ? 0 : hi - lo;
  return times.map(({ start, end }, index) => {
    const hasRange = start !== null && end !== null;
    const single = hasRange ? null : (start ?? end);
    if (!hasRange && single === null) {
      return { index, from: null, to: null, valid: false, clamped: false };
    }
    let rawFrom;
    let rawTo;
    if (hasRange) {
      rawFrom = (Math.min(start, end) - lo) / span;
      rawTo = (Math.max(start, end) - lo) / span;
    } else {
      rawFrom = (single - lo) / span;
      rawTo = rawFrom;
    }
    if (degenerate) { rawFrom = 0.5; rawTo = 0.5; }
    const clamped = rawFrom < 0 || rawFrom > 1 || rawTo < 0 || rawTo > 1;
    return { index, from: clamp01(rawFrom), to: clamp01(rawTo), valid: true, clamped };
  });
}
