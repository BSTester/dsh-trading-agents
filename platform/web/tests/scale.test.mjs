// 图表标度纯函数（第一批共用原语，2026-09-18）。宿主无关，node --test 直测。
//
// 为什么这些函数必须被测：canvas 在 node 里没有实现（画不出、断言不了像素），
// 「刻度没算出来」「扇区总数不守恒」「缺时刻被当成 0」这类错误在浏览器里表现为
// **一张看着挺正常的假图**——没有报错、没有白屏，只有数字是错的。标度与几何是本仓库
// 唯一能在 CI 里锁住的地方（与 geometry.test.mjs 同一口径）。
//
// 本文件按「边界兜底」组织：每个函数先测正常路径，再逐个测退化/畸形输入，
// 断言的都是 docstring 里写明的口径（而不是实现细节）。
import test from "node:test";
import assert from "node:assert/strict";
import {
  barLayout, donutArcs, finiteNumber, linearScale, niceTicks, timelineLayout,
} from "../src/charts/scale.js";

// ---------------------------------------------------------------------------
// finiteNumber：缺失值不是 0
// ---------------------------------------------------------------------------

test("finiteNumber：缺失/非数字一律 null（不把 null、空串当 0）", () => {
  // 这是全模块的地基：Number(null) === 0、Number("") === 0，直接用 Number() 会把
  // 「没有成交量」变成「成交量 0」——图上就是一根高度为 0 的真柱子。
  assert.equal(finiteNumber(null), null);
  assert.equal(finiteNumber(undefined), null);
  assert.equal(finiteNumber(""), null);
  assert.equal(finiteNumber("   "), null);
  assert.equal(finiteNumber(NaN), null);
  assert.equal(finiteNumber(Infinity), null);
  assert.equal(finiteNumber(-Infinity), null);
  assert.equal(finiteNumber(true), null);
  assert.equal(finiteNumber({}), null);
  assert.equal(finiteNumber([]), null);
  assert.equal(finiteNumber("12.5"), 12.5);
  assert.equal(finiteNumber(" 12.5 "), 12.5);
  assert.equal(finiteNumber(-3), -3);
  assert.equal(finiteNumber(0), 0, "0 是有效数值，不是缺失");
});

// ---------------------------------------------------------------------------
// niceTicks：坐标轴刻度
// ---------------------------------------------------------------------------

test("niceTicks：常规区间给出整齐步长，首尾覆盖原始区间", () => {
  const ticks = niceTicks(0, 9, 4);
  assert.equal(ticks.step, 2.5);
  assert.equal(ticks.min, 0);
  assert.equal(ticks.max, 10);
  assert.deepEqual(ticks.ticks, [0, 2.5, 5, 7.5, 10]);
  // 覆盖面：必须包住原区间（否则最高/最低点会落在绘图区外被裁掉——K 线标签裁切的教训）
  assert.ok(ticks.min <= 0 && ticks.max >= 9);
});

test("niceTicks：大数与小数都不产生浮点噪声尾巴", () => {
  const big = niceTicks(0, 1e9, 5);
  assert.deepEqual(big.ticks, [0, 2e8, 4e8, 6e8, 8e8, 1e9]);
  const small = niceTicks(0, 0.3, 5);
  assert.deepEqual(small.ticks, [0, 0.1, 0.2, 0.3], "0.1*3 的浮点尾巴必须被抹掉");
});

test("niceTicks：min==max 撑开一个区间（不返回零宽/NaN）", () => {
  const positive = niceTicks(5, 5, 4);
  assert.ok(positive.min < 5 && positive.max > 5, "围绕该值撑开");
  assert.deepEqual(positive.ticks, [4.5, 4.75, 5, 5.25, 5.5]);
  assert.equal(positive.ticks.includes(5), true);
  const zero = niceTicks(0, 0, 4);
  assert.deepEqual(zero.ticks, [0, 0.25, 0.5, 0.75, 1], "0 上撑成 [0,1]，不是 [-1,1]");
});

test("niceTicks：反向区间自动交换，顺序永远递增", () => {
  const swapped = niceTicks(10, 2, 4);
  assert.ok(swapped.min <= 2 && swapped.max >= 10);
  assert.deepEqual([...swapped.ticks].sort((a, b) => a - b), swapped.ticks);
  assert.equal(niceTicks(10, 2, 4).ticks.length > 0, true);
});

test("niceTicks：非有限/缺失输入有兜底，count 非法回落默认值", () => {
  // 单边缺失 → 当成退化区间（围绕已知值撑开），而不是当成 0
  const oneSide = niceTicks(NaN, 5, 4);
  assert.ok(oneSide.min < 5 && oneSide.max > 5);
  // 双边都缺 → [0,1]
  assert.deepEqual(niceTicks("abc", undefined, 4).ticks, [0, 0.25, 0.5, 0.75, 1]);
  assert.deepEqual(niceTicks(null, null, 4).ticks, [0, 0.25, 0.5, 0.75, 1]);
  // count 非法 → 默认 5（不抛、不返回空数组）
  for (const count of [0, -3, NaN, Infinity, "x", undefined]) {
    const ticks = niceTicks(0, 100, count);
    assert.ok(ticks.ticks.length >= 2, String(count));
    assert.equal(ticks.ticks.every(Number.isFinite), true, String(count));
  }
  // 刻度数量有上限（防止 count 巨大时把画布画成一片黑）
  assert.ok(niceTicks(0, 1, 1e6).ticks.length <= 60);
});

// ---------------------------------------------------------------------------
// linearScale：值域 → 像素
// ---------------------------------------------------------------------------

test("linearScale：线性映射与反向映射都对", () => {
  const up = linearScale([0, 10], [0, 100]);
  assert.equal(up(0), 0);
  assert.equal(up(5), 50);
  assert.equal(up(10), 100);
  // y 轴惯用反向 range（大值在上方 = 小像素）
  const flipped = linearScale([0, 10], [100, 0]);
  assert.equal(flipped(0), 100);
  assert.equal(flipped(10), 0);
  assert.equal(flipped(5), 50);
  // 反向 domain（不常用但要单调）
  assert.equal(linearScale([10, 0], [0, 100])(10), 0);
  assert.equal(linearScale([10, 0], [0, 100])(0), 100);
});

test("linearScale：域退化时不产生 NaN/Infinity（所有值落 range 中点）", () => {
  const flat = linearScale([3, 3], [0, 100]);
  assert.equal(flat(3), 50);
  assert.equal(flat(7), 50, "域外值也不得变成 Infinity");
  assert.equal(flat(-1e9), 50);
  assert.equal(Number.isFinite(flat(0)), true);
  assert.equal(linearScale([0, 0], [0, 360])(0), 180);
  // 域/值域本身非法 → 兜底 [0,1]→[0,1]，仍然有限
  const fallback = linearScale(null, null);
  assert.equal(fallback(0.5), 0.5);
  assert.equal(Number.isFinite(fallback(123)), true);
});

test("linearScale：缺失的 value 返回 NaN（不伪装成中点），由调用方跳过", () => {
  const scale = linearScale([0, 10], [0, 100]);
  for (const bad of [null, undefined, "", NaN, "abc", {}]) {
    assert.equal(Number.isNaN(scale(bad)), true, JSON.stringify(bad));
  }
  assert.equal(scale("5"), 50, "数字串是有效值（上游成交量就是字符串）");
});

// ---------------------------------------------------------------------------
// barLayout：条形长度 / 槽位
// ---------------------------------------------------------------------------

test("barLayout：等距槽位 + 归一条长 + 槽内空隙", () => {
  const rows = barLayout([10, 20]);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].ratio, 0.5);
  assert.equal(rows[1].ratio, 1);
  // 槽轴归一化：2 条 → 每槽 0.5，默认 gap=0.2 → 条厚 0.4、居中留白 0.05
  assert.deepEqual(rows.map((row) => row.offset), [0.05, 0.55]);
  assert.deepEqual(rows.map((row) => row.extent), [0.4, 0.4]);
  assert.equal(rows[0].zeroRatio, 0, "非负数据零基准在槽轴底部");
  assert.deepEqual(rows.map((row) => row.negative), [false, false]);
});

test("barLayout：全 0 与全相等都不产生 NaN（条不可见但不崩）", () => {
  // 全 0：长度轴退化成 [0,1]，每条 ratio=0 —— 画出来是空轨道，不是「无限高」
  for (const row of barLayout([0, 0, 0])) {
    assert.equal(Number.isFinite(row.ratio), true);
    assert.equal(Number.isFinite(row.offset), true);
    assert.equal(Number.isFinite(row.extent), true);
    assert.equal(row.ratio, 0, "全 0 → 条不可见（绝不产生 Infinity/NaN）");
  }
  // 全相等（同为 7）：零基准仍是 0，所以两条都是满高——这是柱状图的正常读法
  assert.deepEqual(barLayout([7, 7]).map((row) => row.ratio), [1, 1]);
  assert.deepEqual(barLayout([]), []);
});

test("barLayout：null/NaN/空串跳过（valid=false），但槽位位置不塌陷", () => {
  const rows = barLayout([5, null, "", NaN, "x", 10]);
  assert.deepEqual(rows.map((row) => row.valid), [true, false, false, false, false, true]);
  assert.deepEqual(rows.map((row) => row.value), [5, null, null, null, null, 10]);
  assert.deepEqual(rows.map((row) => row.ratio), [0.5, 0, 0, 0, 0, 1]);
  // 槽位仍按 6 个 item 均匀分布（否则「跳过」会让剩下的条重叠）
  assert.ok(Math.abs(rows[5].offset - (5 / 6 + (1 / 6) * 0.2 / 2)) < 1e-12);
});

test("barLayout：负数从零基准反向生长（VBar 正负分色用）", () => {
  const rows = barLayout([-5, 10]);
  assert.equal(rows[0].negative, true);
  assert.equal(rows[1].negative, false);
  // 域 [-5,10] → 零基准在 1/3 处；两条各占 1/3、2/3 的长度轴
  assert.ok(Math.abs(rows[0].zeroRatio - 1 / 3) < 1e-12);
  assert.ok(Math.abs(rows[0].ratio - 1 / 3) < 1e-12);
  assert.ok(Math.abs(rows[1].ratio - 2 / 3) < 1e-12);
});

test("barLayout：显式 max 决定归一分母；非法 max 回落数据上界", () => {
  assert.equal(barLayout([50], { max: 100 })[0].ratio, 0.5);
  assert.equal(barLayout([50], { max: 100 })[0].zeroRatio, 0);
  // 超出上界的值收敛到 1（条不越出绘图区）
  assert.equal(barLayout([150], { max: 100 })[0].ratio, 1);
  // max 非法（0/负/非数）→ 回落数据上界，而不是除零
  for (const max of [0, -5, NaN, "x"]) {
    const row = barLayout([50], { max })[0];
    assert.equal(Number.isFinite(row.ratio), true, String(max));
    assert.equal(row.ratio, 1, String(max));
  }
  // gap 越界收敛到 [0, 0.9]
  assert.ok(Math.abs(barLayout([1, 1], { gap: 5 })[0].extent - 0.5 * 0.1) < 1e-12);
  assert.equal(barLayout([1, 1], { gap: -3 })[0].extent, 0.5);
  // 非数组 → []
  assert.deepEqual(barLayout(null), []);
  assert.deepEqual(barLayout(undefined), []);
});

// ---------------------------------------------------------------------------
// donutArcs：环图扇区
// ---------------------------------------------------------------------------

test("donutArcs：比例守恒、首尾闭合到 start/end（不留缝）", () => {
  const arcs = donutArcs([1, 1, 2]);
  assert.deepEqual(arcs.map((arc) => arc.index), [0, 1, 2]);
  assert.deepEqual(arcs.map((arc) => arc.ratio), [0.25, 0.25, 0.5]);
  assert.equal(arcs[0].from, -Math.PI / 2, "默认从 12 点方向起画");
  assert.equal(arcs[0].to, arcs[1].from, "相邻扇区必须首尾相接");
  assert.equal(arcs[1].to, arcs[2].from);
  assert.equal(arcs[2].to, -Math.PI / 2 + Math.PI * 2, "最后一段精确收在 end（浮点也不留缝）");
  assert.deepEqual(arcs.map((arc) => arc.value), [1, 1, 2]);
});

test("donutArcs：全 0 / 全负 / 全缺失 → 空数组（不画假的整圆）", () => {
  assert.deepEqual(donutArcs([0, 0]), []);
  assert.deepEqual(donutArcs([0]), []);
  assert.deepEqual(donutArcs([-1, -2]), [], "全是负数时不许回一个 100% 的假扇区");
  assert.deepEqual(donutArcs([null, "", NaN]), []);
  assert.deepEqual(donutArcs([]), []);
  assert.deepEqual(donutArcs(null), []);
});

test("donutArcs：负数/缺失项被跳过（保留原下标，调用方据此计数与找回标签）", () => {
  const arcs = donutArcs([2, -1, 2, null]);
  assert.deepEqual(arcs.map((arc) => arc.index), [0, 2], "被跳过的项不出现在扇区里");
  assert.deepEqual(arcs.map((arc) => arc.ratio), [0.5, 0.5], "分母只由正数构成");
  assert.equal(arcs[1].to, -Math.PI / 2 + Math.PI * 2);
  // 调用方数法：values.length - arcs.length 就是跳过条数
  assert.equal(4 - arcs.length, 2);
});

test("donutArcs：自定义起止角生效；角度非法回落整圈", () => {
  const half = donutArcs([1, 1], { start: 0, end: Math.PI });
  assert.equal(half[0].from, 0);
  assert.ok(Math.abs(half[0].to - Math.PI / 2) < 1e-12);
  assert.ok(Math.abs(half[1].to - Math.PI) < 1e-12);
  for (const angles of [{ start: 1, end: 0 }, { start: 0, end: 0 }, { start: 0, end: NaN },
                        { start: 0, end: -1 }]) {
    const arcs = donutArcs([1], angles);
    assert.equal(arcs.length, 1, JSON.stringify(angles));
    assert.ok(Math.abs((arcs[0].to - arcs[0].from) - Math.PI * 2) < 1e-9,
              `角度非法必须回落整圈：${JSON.stringify(angles)}`);
  }
  // 单边非法是「该边回落缺省值」，不是整圈：start 缺失/非法 → -π/2，end 仍然生效
  const halfStart = donutArcs([1], { start: Infinity, end: 1 });
  assert.ok(Math.abs(halfStart[0].from - -Math.PI / 2) < 1e-12);
  assert.ok(Math.abs(halfStart[0].to - 1) < 1e-12);
});

// ---------------------------------------------------------------------------
// timelineLayout：有起止时刻的项 → [0,1]
// ---------------------------------------------------------------------------

test("timelineLayout：缺省域由全部时刻推出，位置按比例落在 [0,1]", () => {
  const layout = timelineLayout([
    { start: 0, end: 10 },
    { start: 10, end: 20 },
  ]);
  assert.deepEqual(layout.map((row) => [row.from, row.to]), [[0, 0.5], [0.5, 1]]);
  assert.deepEqual(layout.map((row) => row.valid), [true, true]);
  assert.deepEqual(layout.map((row) => row.index), [0, 1]);
});

test("timelineLayout：缺时刻返回 null 偏移（不是 0）——「没有时刻」≠「时刻是起点」", () => {
  const layout = timelineLayout([
    { start: 0, end: 10 },   // 正常
    { label: "没跑过" },      // 两个时刻都缺
    { start: 5 },             // 只有开始 → 瞬时点
    { end: 5 },               // 只有结束 → 瞬时点
  ], { start: 0, end: 10 });
  assert.deepEqual([layout[1].from, layout[1].to], [null, null]);
  assert.equal(layout[1].valid, false);
  assert.equal(layout[1].from === 0, false, "缺时刻绝不能画成 0（会被读成『从起点就开始了』）");
  assert.deepEqual([layout[2].from, layout[2].to], [0.5, 0.5]);
  assert.deepEqual([layout[3].from, layout[3].to], [0.5, 0.5]);
  assert.equal(layout.length, 4, "输出与输入等长且同序（页面按下标取标签）");
});

test("timelineLayout：起止颠倒自动交换；域外时刻收敛到 [0,1] 并标记 clamped", () => {
  const layout = timelineLayout([
    { start: 8, end: 2 },
    { start: -5, end: 12 },
  ], { start: 0, end: 10 });
  assert.deepEqual([layout[0].from, layout[0].to], [0.2, 0.8], "end < start 时按区间画");
  assert.equal(layout[0].clamped, false);
  assert.deepEqual([layout[1].from, layout[1].to], [0, 1]);
  assert.equal(layout[1].clamped, true, "被截断必须可识别，页面才能如实标注");
});

test("timelineLayout：域退化（只有一个时刻）时放轨道中点，而不是贴左边缘", () => {
  const single = timelineLayout([{ start: 5, end: 5 }]);
  assert.deepEqual([single[0].from, single[0].to], [0.5, 0.5]);
  const explicit = timelineLayout([{ start: 7 }], { start: 3, end: 3 });
  assert.deepEqual([explicit[0].from, explicit[0].to], [0.5, 0.5]);
  const none = timelineLayout([{ label: "x" }]);
  assert.deepEqual([none[0].from, none[0].to], [null, null], "全无时刻 → 依然 null，不是中点");
});

test("timelineLayout：接受毫秒数、Date 与可解析字符串；不可解析按缺时刻处理", () => {
  const layout = timelineLayout([
    { start: 0, end: 3_600_000 },
    { start: new Date(1_800_000), end: "1970-01-01T01:00:00Z" },
    { start: "不是时间", end: 0 },
    { start: 3_600_000, end: 3_600_000 },
  ]);
  assert.deepEqual([layout[1].from, layout[1].to], [0.5, 1], "Date 与 ISO 串混用也要排进同一根轨道");
  assert.deepEqual([layout[2].from, layout[2].to], [0, 0], "一半可解析 → 按可解析的那个当瞬时点");
  assert.deepEqual([layout[3].from, layout[3].to], [1, 1]);
  assert.deepEqual(timelineLayout(null), []);
  assert.deepEqual(timelineLayout([null, undefined]).map((row) => row.valid), [false, false]);
});

// 实测（本机 UTC+8）：`Date.parse("1970-01-01 00:30:00")` 返回 -25200000（本地时区）而不是
// 1800000（UTC）。含义是**没有偏移量的写法按运行环境本地时区解释**——服务端阶段时刻正是
// 这种无偏移写法，所以必须认它；但同一批数据里混进 `Z` 串会让整根轨道平移一个时区差。
// 这里用 `new Date(y,m,d,...)`（同样的本地时区口径）锁定语义，从而与运行机器无关。
test("timelineLayout：无时区偏移的『YYYY-MM-DD HH:mm:ss』按本地时区解释（与 new Date 同口径）", () => {
  const localMs = new Date(1970, 0, 1, 0, 30, 0).getTime();
  const spaced = timelineLayout([{ start: "1970-01-01 00:30:00", end: localMs + 60_000 }],
                                { start: localMs, end: localMs + 120_000 });
  assert.deepEqual([spaced[0].from, spaced[0].to], [0, 0.5]);
  const isoLocal = timelineLayout([{ start: "1970-01-01T00:30:00", end: localMs + 60_000 }],
                                  { start: localMs, end: localMs + 120_000 });
  assert.deepEqual([isoLocal[0].from, isoLocal[0].to], [0, 0.5], "带 T 但不带 Z 也是本地时区");
});
