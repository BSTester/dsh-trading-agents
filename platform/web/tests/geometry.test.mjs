// 图表几何纯函数（自 client.js 移植，规格 §4.2）。宿主无关，node --test 直测。
import test from "node:test";
import assert from "node:assert/strict";
import { barIndexAt, tooltipLeft, compactNumber } from "../src/charts/geometry.js";

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
