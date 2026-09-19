// formatCore 纯格式化契约（2026-09-19 字段审计任务的回归钉）。
//
// 为什么单独测这一层：`num/pctOf/stampOf` 此前**没有任何单测**，而它们是全站展示口径的
// 唯一入口；format.jsx 带 React/antd 无法被 node --test 直接加载，故实现抽到本模块。
// 用例逐条对应任务书 §可判定口径：有值不得显示 —、缺失不得显示 0、比例必须 ×100、
// 千分位必须存在、时间戳不得带 T，以及「毫秒时间戳不得原样显示成 13 位整数」。
import test from "node:test";
import assert from "node:assert/strict";

import {
  MISSING, clockText, dayText, numText, pctOfText, roundTo, stampText,
} from "../src/services/formatCore.js";

test("numText：缺失 → —，绝不显示 0", () => {
  for (const empty of [null, undefined, ""]) {
    assert.equal(numText(empty), MISSING);
    assert.equal(numText(empty, 0), MISSING);
  }
  assert.equal(numText(0), "0");            // 0 是真实值，不是缺失
  assert.equal(numText(0, 0), "0");
});

test("numText：千分位与最多 N 位小数（zh-CN）", () => {
  assert.equal(numText(1234567.891), "1,234,567.89");
  assert.equal(numText(1234567.891, 0), "1,234,568");
  assert.equal(numText(9.1), "9.1");        // 只限最大值，不补零
  assert.equal(numText(9.07, 3), "9.07");
  assert.equal(numText(-12935913), "-12,935,913");
  assert.equal(numText(1000), "1,000");
  assert.equal(numText(999), "999");
});

test("numText：非有限值原样返回字符串（不变成 NaN）", () => {
  assert.equal(numText("abc"), "abc");
  assert.equal(numText(Number.NaN), "NaN");
  assert.equal(numText(Number.POSITIVE_INFINITY), "Infinity");
});

test("pctOfText：比例 ×100，缺失 / 非有限 → —", () => {
  assert.equal(pctOfText(0.0123), "1.23%");
  assert.equal(pctOfText(0.472), "47.20%");
  assert.equal(pctOfText(-0.076, 3), "-7.600%");
  assert.equal(pctOfText(0), "0.00%");
  assert.equal(pctOfText(null), MISSING);
  assert.equal(pctOfText(undefined), MISSING);
  assert.equal(pctOfText("abc"), MISSING);
  // 已经是百分数的值**不再**乘 100（由调用方选对函数，避免重复 ×100）。
  // 注：百分数**不做千分位**（`toFixed` 直出），这是既有口径（portfolio 页累计收益率同源）。
  assert.equal(pctOfText(31.19), "3119.00%");
});

test("stampText：ISO 的 T → 空格、截 19 位；缺失 → —", () => {
  assert.equal(stampText("2026-09-19T02:31:16"), "2026-09-19 02:31:16");
  assert.equal(stampText("2026-09-19T02:31:16.281Z"), "2026-09-19 02:31:16");
  assert.equal(stampText("2026-09-19T02:31:20+08:00"), "2026-09-19 02:31:20");
  assert.equal(stampText("2026-09-19 02:31:16"), "2026-09-19 02:31:16");   // 已是空格分隔
  assert.equal(stampText(null), MISSING);
  assert.equal(stampText(""), MISSING);
});

test("clockText：毫秒时间戳 → HH:mm（不再显示 13 位整数）", () => {
  // 1789695000000 = 2026-09-18 09:30:00（本地时区）；断言用同一台机器的本地时刻，
  // 故这里先算出期望的小时:分钟，避免测试对运行时区敏感。
  const date = new Date(1789695000000);
  const expected = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  assert.equal(clockText(1789695000000), expected);
  assert.doesNotMatch(clockText(1789695000000), /^\d{10,}$/);
  // 字符串时间退回前 16 位（不二次加工）；缺失 → —
  assert.equal(clockText("2026-09-18 09:30:00"), "2026-09-18 09:30");
  assert.equal(clockText("09:30"), "09:30");
  assert.equal(clockText(null), MISSING);
  assert.equal(clockText(""), MISSING);
});

test("dayText：毫秒时间戳 → YYYY-MM-DD（不再显示 10 位假日期）", () => {
  const date = new Date(1786291200000);
  const expected = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`
    + `-${String(date.getDate()).padStart(2, "0")}`;
  assert.equal(dayText(1786291200000), expected);
  assert.doesNotMatch(dayText(1786291200000), /^\d{8,}$/);
  assert.equal(dayText("2026-09-18 00:00:00"), "2026-09-18");
  assert.equal(dayText("2026-09-18"), "2026-09-18");
  assert.equal(dayText(null), MISSING);
});

test("roundTo：antd Statistic 的 precision 是截断，必须先四舍五入（2026-09-19 实测缺陷）", () => {
  // 真机取证：ATR 5.915678571428567 在 signal 页被 antd 渲染成 "5.91"（正确 5.92），
  // 因为 antd es/statistic/Number.js 对小数串是 padEnd + slice（截断），不做四舍五入。
  assert.equal(roundTo(5.915678571428567, 2), 5.92);
  assert.equal(roundTo(5.915678571428567, 2).toFixed(2), "5.92");
  // 固定位数语义与 numText 的「最多 N 位」不同：交给 antd 的仍是数值，由 precision 补零
  assert.equal(String(roundTo(61.4, 2)), "61.4");
  assert.equal(roundTo(999809.29, 2), 999809.29);
  // 缺失/非数值原样返回（页面靠 `?? "—"` 与 Statistic 原样字符串渲染兜底）
  for (const empty of [null, undefined, "", "—"]) assert.equal(roundTo(empty, 2), empty);
  assert.equal(roundTo("abc", 2), "abc");
});
