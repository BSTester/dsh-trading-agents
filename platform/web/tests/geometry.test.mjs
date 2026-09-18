// 图表几何纯函数（自 client.js 移植，规格 §4.2）。宿主无关，node --test 直测。
import test from "node:test";
import assert from "node:assert/strict";
import { barIndexAt, tooltipLeft, compactNumber, priceTagRect } from "../src/charts/geometry.js";

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
