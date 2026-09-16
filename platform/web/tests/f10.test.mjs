// 数据面展示纯函数（WP12 任务 6）。宿主无关，node --test 直测。
// 实现在 services/f10.js（research.jsx / options.jsx 只做接线：React/antd 不进测试）。
// 断言锚点：字段名与枚举全部来自官方文档或锁定表（f10.js 文件头逐条登记）——
//   analyst_consensus 5 档评级 / rating_summary 3 档评级（**两套枚举不合并**）/
//   「合法但无数据」是空对象或空列表（要如实说明，不是失败）。
import test from "node:test";
import assert from "node:assert/strict";
import {
  MISSING, analystConsensusSummary, analystRatingLabel, dataplaneHint,
  exerciseProbabilitySummary, fmtNum, fmtPct, fmtText, institutionalSummary,
  optionVolatilitySummary, ratingItemLabel, ratingSummarySummary, strikeRows,
} from "../src/services/f10.js";

test("fmtNum/fmtPct/fmtText：缺失一律 —，0 是合法值不被吞", () => {
  assert.equal(fmtNum(1.234), "1.23");
  assert.equal(fmtNum("1.5"), "1.50");
  assert.equal(fmtNum(0), "0.00");
  assert.equal(fmtNum(null), MISSING);
  assert.equal(fmtNum(undefined), MISSING);
  assert.equal(fmtNum("abc"), MISSING);
  assert.equal(fmtNum(NaN), MISSING);
  // 官方口径：比率 1.23 表示 1.23% → 只补 % 不乘 100
  assert.equal(fmtPct(22.727), "22.73%");
  assert.equal(fmtPct(0), "0.00%");
  assert.equal(fmtPct(null), MISSING);
  assert.equal(fmtText(0), "0");
  assert.equal(fmtText(""), MISSING);
  assert.equal(fmtText(null), MISSING);
  assert.equal(fmtText("2026-06-06"), "2026-06-06");
});

test("analystRatingLabel：analyst_consensus 是 5 档；未知码原样回显不猜档位", () => {
  assert.equal(analystRatingLabel(1), "Sell 卖出");
  assert.equal(analystRatingLabel(2), "Underperform 跑输大盘");
  assert.equal(analystRatingLabel(3), "Hold 持有");
  assert.equal(analystRatingLabel(4), "Buy 买入");
  assert.equal(analystRatingLabel(5), "Strong Buy 强烈买入");
  assert.equal(analystRatingLabel(9), "rating=9");
  assert.equal(analystRatingLabel(null), MISSING);
});

test("ratingItemLabel：rating_summary 是 3 档，与 5 档枚举不混用", () => {
  assert.equal(ratingItemLabel(1), "Sell 卖出");
  assert.equal(ratingItemLabel(2), "Hold 持有");
  assert.equal(ratingItemLabel(3), "Buy 买入");
  // 关键：3 档枚举里没有 4/5 —— 若两套标签被错误合并，这里会显示「Buy/Strong Buy」
  assert.equal(ratingItemLabel(4), "rating=4");
  assert.equal(ratingItemLabel(5), "rating=5");
  assert.equal(ratingItemLabel(undefined), MISSING);
});

test("analystConsensusSummary：官方响应样例 → 键值行齐全", () => {
  const { rows, note } = analystConsensusSummary({
    average: 686.71, buy: 22.727, highest: 794.73, hold: 0, lowest: 560,
    num_of_target_analysts: 44, rating: 5, sell: 0, strong_buy: 77.273,
    total: 44, underperform: 0, update_time: 1780729098,
    update_time_str: "2026-06-06",
  });
  assert.equal(note, "");
  const map = Object.fromEntries(rows.map((row) => [row.label, row.value]));
  assert.equal(map["综合评级"], "Strong Buy 强烈买入");
  assert.equal(map["覆盖分析师"], "44");
  assert.equal(map["强烈买入 strong_buy"], "77.27%");
  assert.equal(map["买入 buy"], "22.73%");
  assert.equal(map["持有 hold"], "0.00%");
  assert.equal(map["目标价区间"], "560.00 – 794.73（平均 686.71）");
  assert.equal(map["数据日期"], "2026-06-06");
});

test("analystConsensusSummary：市场差异字段缺失 → 该档不显示（不写 0）", () => {
  // 官方：buy/underperform 仅 HK/CN/SG/MY/AU/JP 返回；US/CA 只有 strong_buy/hold/sell
  const { rows } = analystConsensusSummary({
    rating: 3, total: 10, strong_buy: 10, hold: 80, sell: 10,
    average: 100, highest: 120, lowest: 90, num_of_target_analysts: 8,
    update_time_str: "2026-01-02",
  });
  const labels = rows.map((row) => row.label);
  assert.ok(!labels.includes("买入 buy"));
  assert.ok(!labels.includes("跑输大盘 underperform"));
  assert.equal(labels.includes("持有 hold"), true);
});

test("analystConsensusSummary：空对象 = 上游合法无覆盖（如实说明，不是失败）", () => {
  const { rows, note } = analystConsensusSummary({});
  assert.deepEqual(rows, []);
  assert.match(note, /无分析师覆盖/);
});

test("analystConsensusSummary：目标价三缺时给 —，只缺区间给平均", () => {
  const onlyAverage = analystConsensusSummary({ rating: 4, average: 50 });
  assert.equal(Object.fromEntries(onlyAverage.rows.map((r) => [r.label, r.value]))["目标价区间"],
    "平均 50.00");
  const none = analystConsensusSummary({ rating: 4 });
  assert.equal(Object.fromEntries(none.rows.map((r) => [r.label, r.value]))["目标价区间"], MISSING);
});

test("ratingSummarySummary：机构+分析师两个维度都映射（名称/评级/目标价/推荐日）", () => {
  const { total, institutionCount, analystCount, rows, note } = ratingSummarySummary({
    pagination: { has_more: true, next_key: "2", total: 33 },
    inst_rating_summary_list: [{
      institution_info: { institution_name: "Wedbush" },
      rating_item_list: [{ rating: 3, target_price: 400,
        recommendation_date_str: "2026-06-05T05:00:00Z" }],
    }],
    analyst_rating_summary_list: [{
      analyst_info: { analyst_name: "Jane Doe", num_of_stars: 4 },
      rating_item_list: [{ rating: 2, target_price: 350,
        recommendation_date_str: "2026-06-04T05:00:00Z" }],
    }],
  });
  assert.equal(total, "33");
  assert.equal(institutionCount, 1);
  assert.equal(analystCount, 1);
  assert.equal(note, "");
  assert.deepEqual(rows[0], { name: "Wedbush", source: "机构", rating: "Buy 买入",
    target: "400.00", date: "2026-06-05T05:00:00Z" });
  assert.deepEqual(rows[1], { name: "Jane Doe", source: "分析师", rating: "Hold 持有",
    target: "350.00", date: "2026-06-04T05:00:00Z" });
});

test("ratingSummarySummary：两个维度都空 → 如实说明仅美股/加股有数据", () => {
  const result = ratingSummarySummary({ pagination: { total: 0 },
    inst_rating_summary_list: [], analyst_rating_summary_list: [] });
  assert.deepEqual(result.rows, []);
  assert.match(result.note, /仅美股\/加股有数据/);
});

test("ratingSummarySummary：rating_item_list 缺失时字段给 —（不崩、不猜）", () => {
  const { rows } = ratingSummarySummary({
    inst_rating_summary_list: [{ institution_info: { institution_name: "X" } }],
    analyst_rating_summary_list: [],
  });
  assert.deepEqual(rows[0], { name: "X", source: "机构", rating: MISSING,
    target: MISSING, date: MISSING });
});

test("institutionalSummary：锁定表 §C.5 字段逐项映射，环比缺失给 —", () => {
  const { rows, note } = institutionalSummary({
    period_text: "2026Q2", institution_quantity: 120,
    institution_quantity_change: 5, holder_quantity: 1_000_000,
    holder_pct: 12.5, close_price: 88.8, update_time_str: "2026-07-01",
  });
  const map = Object.fromEntries(rows.map((row) => [row.label, row.value]));
  assert.equal(note, "");
  assert.equal(map["报告期"], "2026Q2");
  assert.equal(map["机构数量"], "120");
  assert.equal(map["机构数量变化"], "5");
  assert.equal(map["持股占比"], "12.50%");
  assert.equal(map["持股占比变化"], MISSING);
  assert.equal(map["收盘价"], "88.80");
  assert.equal(map["数据更新"], "2026-07-01");
});

test("institutionalSummary：空对象 → 如实说明无数据", () => {
  const { note } = institutionalSummary({});
  assert.match(note, /无机构持股数据/);
});

test("optionVolatilitySummary：锁定表 §C.7 字段映射", () => {
  const { rows, note } = optionVolatilitySummary({
    timestamp: 1780000000, implied_volatility: 0.35, history_volatility: 0.28,
    volatility_premium: 0.07, average_impvol: 0.31, impvol_status: "HIGH",
  });
  const map = Object.fromEntries(rows.map((row) => [row.label, row.value]));
  assert.equal(note, "");
  assert.equal(map["隐含波动率 IV"], "0.35");
  assert.equal(map["历史波动率 HV"], "0.28");
  assert.equal(map["IV 状态"], "HIGH");
  assert.equal(map["数据时间"], "1780000000");
  assert.equal(map["分析"], MISSING);
});

test("exerciseProbabilitySummary：数组行权概率 → strikes 表格行；对象 → 键值行", () => {
  const array = exerciseProbabilitySummary({
    security_price: 100, timestamp: 1780000000,
    strike_probability: [{ strike_price: 100, probability: 0.42 }],
  });
  assert.equal(array.strikes.length, 1);
  assert.equal(Object.fromEntries(array.rows.map((r) => [r.label, r.value]))["标的价格"], "100.00");

  const object = exerciseProbabilitySummary({
    security_price: 100, strike_probability: { itm_probability: 0.42 },
  });
  assert.deepEqual(object.strikes, []);
  const map = Object.fromEntries(object.rows.map((r) => [r.label, r.value]));
  assert.equal(map.itm_probability, "0.42");
});

test("exerciseProbabilitySummary：形状不可识别时如实给 —（不猜形状）", () => {
  const { rows } = exerciseProbabilitySummary({ security_price: 100 });
  const map = Object.fromEntries(rows.map((r) => [r.label, r.value]));
  assert.equal(map["行权概率"], MISSING);
});

test("strikeRows：数组元素键名按上游原样成行（界面不编字段名），非对象元素给「值」", () => {
  const rows = strikeRows([
    { strike_price: 100, probability: 0.42, note: null },
    "raw-token",
  ]);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].index, 1);
  assert.deepEqual(rows[0].cells, [
    { label: "strike_price", value: "100" },
    { label: "probability", value: "0.42" },
    { label: "note", value: MISSING },
  ]);
  assert.deepEqual(rows[1].cells, [{ label: "值", value: "raw-token" }]);
  assert.deepEqual(strikeRows(undefined), []);
  assert.deepEqual(strikeRows("x"), []);
});

test("strikeRows：limit 截断（页面另有原始返回兜底，行表不求全）", () => {
  const rows = strikeRows(Array.from({ length: 15 }, (_v, index) => ({ strike: index })), 3);
  assert.equal(rows.length, 3);
});

test("dataplaneHint：按服务端文案给下一步（原因由服务端给，本函数只加动作）", () => {  assert.match(dataplaneHint("f10_detail 仅支持 openapi 通道：mcp 通道未登记该端点的上游工具"
    + "（上游工具名与参数形状未核对，禁止猜名）。请配置 openapi 凭据"), /设置页/);
  assert.match(dataplaneHint("OpenAPI 通道不可用：未配置 openapi 凭据"
    + "（~/.dsh/futu-openapi.json）"), /设置页/);
  assert.match(dataplaneHint("富途业务错误（errcode=-9）：无期权数据查询权限"), /-9/);
  assert.equal(dataplaneHint("富途业务错误（errcode=-3）：symbol 格式非法"), "");
  assert.equal(dataplaneHint(""), "");
  assert.equal(dataplaneHint(undefined), "");
});
