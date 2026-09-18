// 图表几何纯函数（自 client.js 移植，规格 §4.2）。宿主无关，node --test 直测。
import test from "node:test";
import assert from "node:assert/strict";
import {
  axisNumberText, barIndexAt, compactNumber, priceTagRect, tooltipLeft, truncateText,
} from "../src/charts/geometry.js";

test("barIndexAt：绘图区左右留白返回 null，不得误命中首尾", () => {
  const geometry = { padL: 40, plotW: 400, count: 10 };
  assert.equal(barIndexAt(30, geometry), null);
  assert.equal(barIndexAt(450, geometry), null);
  assert.equal(barIndexAt(40, geometry), 0);
  assert.equal(barIndexAt(439, geometry), 9);
});

test("tooltipLeft：贴近右边缘翻到左侧，不越界", () => {
  assert.equal(tooltipLeft(390, 80, 400, 14), 296);
  assert.equal(tooltipLeft(100, 80, 400, 14), 114);
});

test("compactNumber：缺失显示 — 而不是 0；中文量级（万/亿）", () => {
  assert.equal(compactNumber(undefined), "—");
  assert.equal(compactNumber(null), "—");
  // 期望调整：client.js 原实现走 toLocaleString("en-US")，1234 → "1,234"（千分位）
  assert.equal(compactNumber(1234), "1,234");
  assert.equal(compactNumber(5_600_000), "560.00万");
  assert.equal(compactNumber(150_000_000), "1.50亿");
});

// 2026-09-18 实机缺陷：悬停 K 线时右侧收盘价标签只显示一位（8.96 → 只画出「8」）。
// 根因是标签从 `width - padR` **向右**画，而画布到 `width` 结束；这类"贴边元素向右生长"
// 的写法在 canvas 自绘里没有任何 DOM 报错，只能靠几何断言守。
test("priceTagRect：右边缘价格标签向内生长，价格数字绝不被画布裁掉", () => {
  // 实机口径：canvas 1079×360 CSS px、padR=12；「8.96」量得约 20px + 8 内边距
  const rect = priceTagRect(1079, 360, 12, 200, 28);
  assert.equal(rect.x + rect.w, 1079 - 12, "右边缘贴住绘图区右边（不是贴画布右边）");
  assert.ok(rect.x >= 0 && rect.x + rect.w <= 1079, "不得越出画布右边界");
  // 旧实现：x = 1079-12 = 1067，宽 28 → 右边界 1095，越界 16px（正是被裁掉的那截）
  assert.ok(1079 - 12 + 28 > 1079, "对照：旧写法确实会越界（防止有人改回向右画）");
  // 五位数价（约 46px 标签）同样完整可见
  const wide = priceTagRect(1079, 360, 12, 200, 46);
  assert.ok(wide.x + wide.w <= 1079 && wide.x > 0);
});

test("priceTagRect：贴顶/贴底与极窄画布都收回画布内", () => {
  const top = priceTagRect(1079, 360, 12, 10, 28);      // 最高价那根：centerY = padT
  assert.ok(top.y >= 0, "贴顶时整体下压，不越出上边界");
  const bottom = priceTagRect(1079, 360, 12, 342, 28);  // 最低价那根：centerY = height - padB
  assert.ok(bottom.y + bottom.h <= 360, "贴底时整体上抬，不越出下边界");
  const narrow = priceTagRect(20, 40, 12, 20, 46);      // 窄画布比标签还窄
  assert.equal(narrow.x, 0, "宁可压左边界也不出现负 x（负数坐标同样会裁掉价格）");
  assert.equal(priceTagRect(1079, 360, 12, 200, 0).w, 0);
});

// 2026-09-18 第一批图表原语：`fillText` 没有裁剪也不报错，超宽文字直接被画布吃掉
// （K 线右侧价格标签已经栽过一次）。行名/轴标签宽度必须先在纯函数里收敛。
test("truncateText：放得下原样返回，放不下补省略号且结果不超宽", () => {
  const measure = (text) => text.length * 7;   // 等宽桩：每字符 7px
  assert.equal(truncateText("SPY", 21, measure), "SPY");
  assert.equal(truncateText("US.SPY260918C760000", 70, measure), "US.SPY260…",
               "尽量保留可见前缀（10 字符 × 7px = 70px 刚好放下）");
  assert.equal(truncateText("US.SPY260918C760000", 1000, measure), "US.SPY260918C760000");
  for (const width of [14, 21, 49, 70, 140]) {
    const text = truncateText("US.SPY260918C760000", width, measure);
    assert.ok(measure(text) <= width, `宽度 ${width} 下 "${text}" 仍超宽`);
  }
});

test("truncateText：退化输入的兜底（不返回超宽文本，也不抛）", () => {
  assert.equal(truncateText("abc", 0, (t) => t.length * 7), "");
  assert.equal(truncateText("abc", -5, (t) => t.length * 7), "");
  assert.equal(truncateText("abc", 3, (t) => t.length * 7), "", "连省略号都放不下 → 空");
  assert.equal(truncateText(null, 100, (t) => t.length * 7), "");
  assert.equal(truncateText(undefined, 100), "");
  assert.equal(truncateText(12345, 100, (t) => t.length * 7), "12345", "数字也要能画");
  assert.equal(truncateText("abc", 10), "…", "缺 measure 时按 7px/字符 估算");
});

test("axisNumberText：整数原样、小数两位、大数走万/亿、缺失 —", () => {
  assert.equal(axisNumberText(760), "760");
  assert.equal(axisNumberText(13.111), "13.11");
  assert.equal(axisNumberText(350107), "35.01万");
  assert.equal(axisNumberText(-0.5), "-0.50");
  assert.equal(axisNumberText(null), "—");
  assert.equal(axisNumberText(""), "—");
  assert.equal(axisNumberText("758.5"), "758.50", "上游数字字符串也要认");
});
