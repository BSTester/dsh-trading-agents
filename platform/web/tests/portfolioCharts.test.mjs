// 组合/风险/概览三页图表派生的纯函数（2026-09-18 用户需求：「在适当的地方增加一些图表的
// 展示」，第二批：组合页持仓分布、风险页集中度与盈亏分布、概览页市场占比与 Top5）。
//
// 为什么派生只在 services 里、JSX 只做渲染：canvas 在 node 里画不出来，也断言不了像素——
// 「哪条该画、哪条被跳过、占比的分母是谁」是唯一能在 CI 里锁住的地方。第一批的教训是
// 空柱状图看不出原因、缺值被当成 0，所以这里的每条口径都配一条用例：
//   1. **缺失不是 0**：market_value 为 null/空串/0 → 不产出条，并**如实计数**（宁缺毋假）；
//   2. **图与表同源**：全部函数只吃页面上那份 `viewGroups(...)` 过滤后的 groups，
//      不自己再过滤一遍市场（否则图与表会出现两个不一致的数字）；
//   3. **不猜市场**：市场链归一复用 marketFilter.marketChainOf（数字 market_id 1/3/100
//      与字符串链名走同一条路），归不到的归「其它」而不是硬塞进某个市场；
//   4. **口径透明**：占比的分母随返回值一起给出（`total`），页面不自己另算一份。
import test from "node:test";
import assert from "node:assert/strict";
import {
  chartEmptyText, concentrationItems, holdingPnlItems, holdingValueItems, marketShareSlices,
  skipNoteText,
  topHoldings,
} from "../src/services/portfolioCharts.js";

/** 真机样本（2026-09-19 对 127.0.0.1:8397 /api/wb/positions mode=sim 的响应摘录）：
 *  market 是富途数字 market_id（1=港股 / 3=A股 / 100=美股），账户持仓 5/8/2 条。 */
function realGroups() {
  // 组上的 market_value 与「组内持仓市值之和」在真机上是一致的（sim 模式实测
  // 376020 = 60600+65150+63880+32790+153600），样本里保持同一口径。
  return [
    { account: "港股账户", acc_id: "9393", market: 1, market_value: 279350, positions: [
      { symbol: "00100", name: "MINIMAX-W", qty: 200, market_value: 60600, pl_val: -55620 },
      { symbol: "00981", name: "中芯国际", qty: 1000, market_value: 65150, pl_val: -31125 },
      { symbol: "03986", name: "兆易创新", qty: 400, market_value: 153600, pl_val: 76300 },
    ] },
    { account: "A股账户", acc_id: "3182575", market: 3, market_value: 182841, positions: [
      { symbol: "603993", name: "洛阳钼业", qty: 2100, market_value: 36771, pl_val: -13500.9 },
      { symbol: "002475", name: "立讯精密", qty: 2700, market_value: 146070, pl_val: -11475 },
    ] },
    { account: "美股账户", acc_id: "11587526", market: 100, market_value: 326800, positions: [
      { symbol: "NVDA", name: "英伟达", qty: 800, market_value: 175900, pl_val: 33032 },
      { symbol: "MSTR", name: "Strategy", qty: 1000, market_value: 150900, pl_val: -21086 },
    ] },
  ];
}

// --------------------------------------------------------------- holdingValueItems

test("holdingValueItems：按市值降序，label 带代码与名称，total 是画入项之和", () => {
  const { items, skipped, omitted, total } = holdingValueItems(realGroups());
  assert.deepEqual(items.map((row) => row.label), [
    "NVDA 英伟达", "03986 兆易创新", "MSTR Strategy", "002475 立讯精密",
    "00981 中芯国际", "00100 MINIMAX-W", "603993 洛阳钼业",
  ]);
  assert.deepEqual(items.map((row) => row.value),
    [175900, 153600, 150900, 146070, 65150, 60600, 36771]);
  assert.equal(skipped, 0);
  assert.equal(omitted, 0);
  assert.equal(total,  // 画入项之和，即占比分母
    175900 + 153600 + 150900 + 146070 + 65150 + 60600 + 36771);
});

test("holdingValueItems：market_value 为 null / 空串 / 0 / 非数字 → 跳过并如实计数（缺失不是 0）", () => {
  const groups = [{ market: 1, positions: [
    { symbol: "A", name: "甲", market_value: 100 },
    { symbol: "B", name: "乙", market_value: null },
    { symbol: "C", name: "丙", market_value: "" },
    { symbol: "D", name: "丁", market_value: 0 },
    { symbol: "E", name: "戊", market_value: "不是数字" },
    { symbol: "F", name: "己", market_value: -5 },
  ] }];
  const { items, skipped, total } = holdingValueItems(groups);
  assert.deepEqual(items, [{ label: "A 甲", value: 100 }]);
  assert.equal(skipped, 5);            // 缺失 2 + 0 1 + 非数字 1 + 负数 1
  assert.equal(total, 100);            // 跳过的不进分母
});

test("holdingValueItems：上游数字字符串照样计入（不是只认 number）", () => {
  const groups = [{ market: 100, positions: [
    { symbol: "NVDA", name: "英伟达", market_value: "175900.0" },
  ] }];
  const { items, skipped } = holdingValueItems(groups);
  assert.deepEqual(items, [{ label: "NVDA 英伟达", value: 175900 }]);
  assert.equal(skipped, 0);
});

test("holdingValueItems：limit 只截断（计入 omitted），不把被截的算成缺失", () => {
  const { items, skipped, omitted, total } = holdingValueItems(realGroups(), 3);
  assert.deepEqual(items.map((row) => row.label), ["NVDA 英伟达", "03986 兆易创新", "MSTR Strategy"]);
  assert.equal(skipped, 0);
  assert.equal(omitted, 4);
  assert.equal(total, 175900 + 153600 + 150900);
});

test("holdingValueItems：缺 name 只用代码；跨账户同代码各成一条（不合并，与表格逐行对应）", () => {
  const groups = [
    { market: 1, positions: [{ symbol: "00700", market_value: 10 }] },
    { market: 3, positions: [{ symbol: "00700", market_value: 20 }] },
  ];
  const { items } = holdingValueItems(groups);
  assert.deepEqual(items, [{ label: "00700", value: 20 }, { label: "00700", value: 10 }]);
});

test("holdingValueItems：groups 非数组 / 空 → 空结果，不抛错", () => {
  assert.deepEqual(holdingValueItems(null), { items: [], skipped: 0, omitted: 0, total: 0 });
  assert.deepEqual(holdingValueItems([]), { items: [], skipped: 0, omitted: 0, total: 0 });
  assert.deepEqual(holdingValueItems([{ market: 1, positions: null }]),
    { items: [], skipped: 0, omitted: 0, total: 0 });
});

// --------------------------------------------------------------- holdingPnlItems

test("holdingPnlItems：按盈亏降序（盈利在左、亏损在右），label 只用代码", () => {
  const { items, skipped } = holdingPnlItems(realGroups());
  assert.deepEqual(items.map((row) => row.label),
    ["03986", "NVDA", "002475", "603993", "MSTR", "00981", "00100"]);
  assert.deepEqual(items.map((row) => row.value),
    [76300, 33032, -11475, -13500.9, -21086, -31125, -55620]);
  assert.equal(skipped, 0);
});

test("holdingPnlItems：pl_val 缺失跳过并计数，但 0 是有效值（不跳）", () => {
  const groups = [{ market: 1, positions: [
    { symbol: "A", pl_val: 0 },
    { symbol: "B", pl_val: null },
    { symbol: "C" },
    { symbol: "D", pl_val: "abc" },
    { symbol: "E", pl_val: "-12.5" },
  ] }];
  const { items, skipped } = holdingPnlItems(groups);
  assert.deepEqual(items, [{ label: "A", value: 0 }, { label: "E", value: -12.5 }]);
  assert.equal(skipped, 3);
});

test("holdingPnlItems：limit 截断计入 omitted（正负都保留，由组件分色）", () => {
  const { items, omitted } = holdingPnlItems(realGroups(), 2);
  assert.deepEqual(items, [{ label: "03986", value: 76300 }, { label: "NVDA", value: 33032 }]);
  assert.equal(omitted, 5);
});

// --------------------------------------------------------------- marketShareSlices

test("marketShareSlices：数字 market_id 归一为市场链，标签与页头筛选同源", () => {
  const { slices, skipped, total } = marketShareSlices(realGroups());
  assert.deepEqual(slices, [
    { chain: "SH", label: "A股 SH", value: 182841 },
    { chain: "HK", label: "港股 HK", value: 279350 },
    { chain: "US", label: "美股 US", value: 326800 },
  ]);
  assert.equal(skipped, 0);
  assert.equal(total, 182841 + 279350 + 326800);
});

test("marketShareSlices：链顺序固定为 SH/HK/US/其它（与页头下拉一致，不随数据顺序变）", () => {
  const groups = [
    { market: 100, market_value: 1 },
    { market: "3", market_value: 2 },
    { market: "HK", market_value: 3 },
    { market: "9", market_value: 4 },      // 账户类型码：不是市场 → 其它
    { market_value: 5 },                    // 缺字段 → 其它
  ];
  const { slices } = marketShareSlices(groups);
  assert.deepEqual(slices.map((row) => row.chain), ["SH", "HK", "US", null]);
  assert.deepEqual(slices.map((row) => row.label), ["A股 SH", "港股 HK", "美股 US", "其它"]);
  assert.deepEqual(slices.map((row) => row.value), [2, 3, 1, 9]);
});

test("marketShareSlices：归不到市场链的组归「其它」而不是被丢掉（不猜、不藏）", () => {
  const { slices, skipped } = marketShareSlices([
    { market: "SZ", market_value: 10 },     // SZ 与 SH 同属 A 股链
    { market: "9", market_value: 20 },      // 期货/未登记类型码
  ]);
  assert.deepEqual(slices, [
    { chain: "SH", label: "A股 SH", value: 10 },
    { chain: null, label: "其它", value: 20 },
  ]);
  assert.equal(skipped, 0);
});

test("marketShareSlices：同链多账户聚合成一个扇区（占比之和 = 100%）", () => {
  const { slices, total } = marketShareSlices([
    { market: 3, market_value: 30 },
    { market: "SH", market_value: 10 },
    { market: 1, market_value: 60 },
  ]);
  assert.deepEqual(slices.map((row) => row.value), [40, 60]);
  assert.equal(total, 100);
  const ratioSum = slices.reduce((sum, row) => sum + row.value / total, 0);
  assert.equal(Math.round(ratioSum * 100), 100);
});

test("marketShareSlices：账户 group.market_value 缺失（实盘账户恒为 null）→ 退回该组持仓市值之和", () => {
  const { slices, skipped } = marketShareSlices([
    { market: 100, market_value: null, positions: [
      { symbol: "NVDA", market_value: 100 }, { symbol: "MSTR", market_value: 40 },
    ] },
  ]);
  assert.deepEqual(slices, [{ chain: "US", label: "美股 US", value: 140 }]);
  assert.equal(skipped, 0);
});

test("marketShareSlices：账户市值缺失且持仓也无市值 → 计数，不产 0 值扇区", () => {
  const { slices, skipped, emptyChains, total } = marketShareSlices([
    { market: 1, market_value: null, positions: [{ symbol: "A", market_value: null }] },
    { market: 3, market_value: 50, positions: [] },
  ]);
  assert.deepEqual(slices, [{ chain: "SH", label: "A股 SH", value: 50 }]);
  assert.equal(skipped, 1);        // 1 个账户的市值无法计入
  assert.equal(emptyChains, 1);    // 港股链聚合后仍无正值 → 不产出扇区
  assert.equal(total, 50);
});

test("marketShareSlices：全 0 / 全负 / 全缺失 → 空扇区但如实计数（不画整圆冒充 100%）", () => {
  const zero = marketShareSlices([{ market: 1, market_value: 0 }, { market: 3, market_value: 0 }]);
  assert.deepEqual(zero.slices, []);
  assert.equal(zero.emptyChains, 2);
  assert.equal(zero.total, 0);
  const negative = marketShareSlices([{ market: 1, market_value: -3 }]);
  assert.deepEqual(negative.slices, []);
  assert.equal(negative.emptyChains, 1);
  assert.deepEqual(marketShareSlices(null).slices, []);
});

// --------------------------------------------------------------- topHoldings

test("topHoldings：按市值取前 N（降序，label 带名称），舍掉的计入 omitted", () => {
  const { items, skipped, omitted, total } = topHoldings(realGroups(), 5);
  assert.deepEqual(items.map((row) => row.label), [
    "NVDA 英伟达", "03986 兆易创新", "MSTR Strategy", "002475 立讯精密", "00981 中芯国际",
  ]);
  assert.equal(items.length, 5);
  assert.equal(skipped, 0);
  assert.equal(omitted, 2);
  assert.equal(total, 175900 + 153600 + 150900 + 146070 + 65150);
});

test("topHoldings：n 缺省为 5；n 非正/非有限 → 不产出条目（omitted 如实计数）", () => {
  assert.equal(topHoldings(realGroups()).items.length, 5);
  assert.deepEqual(topHoldings(realGroups(), 0).items, []);
  assert.equal(topHoldings(realGroups(), 0).omitted, 7);
  assert.equal(topHoldings(realGroups(), "abc").items.length, 5);   // 非法 n 回落缺省
});

test("topHoldings：市值缺失的持仓不占 Top5 名额（不把 null 当 0 混进来）", () => {
  const groups = [{ market: 1, positions: [
    { symbol: "A", name: "甲", market_value: null },
    { symbol: "B", name: "乙", market_value: 1 },
  ] }];
  const { items, skipped } = topHoldings(groups, 5);
  assert.deepEqual(items, [{ label: "B 乙", value: 1 }]);
  assert.equal(skipped, 1);
});

// --------------------------------------------------------------- concentrationItems

test("concentrationItems：优先用端点 risk.top[].share_of_positions（不自己另算一份）", () => {
  const groups = [{
    account: "港股账户", acc_id: "9393", market: 1,
    // 故意让自算结果与端点不同：端点给 40.85，自算 153600/279350 = 54.98
    risk: { top: [
      { symbol: "03986", name: "兆易创新", market_value: 153600, share_of_positions: 40.85 },
      { symbol: "00981", name: "中芯国际", market_value: 65150, share_of_positions: 17.33 },
    ] },
    positions: [
      { symbol: "03986", name: "兆易创新", market_value: 153600 },
      { symbol: "00981", name: "中芯国际", market_value: 65150 },
    ],
  }];
  const { items, skipped, fallbackAccounts } = concentrationItems(groups);
  assert.deepEqual(items, [
    { label: "03986 兆易创新", value: 40.85 },
    { label: "00981 中芯国际", value: 17.33 },
  ]);
  assert.equal(skipped, 0);
  assert.equal(fallbackAccounts, 0);
});

test("concentrationItems：risk.top 缺失 → 退回自身聚合（占比 = 市值 / 本账户持仓市值）并计数", () => {
  const groups = [{
    market: 1,
    positions: [
      { symbol: "A", name: "甲", market_value: 30 },
      { symbol: "B", name: "乙", market_value: 10 },
      { symbol: "C", name: "丙", market_value: null },
    ],
  }];
  const { items, skipped, fallbackAccounts } = concentrationItems(groups);
  assert.deepEqual(items, [
    { label: "A 甲", value: 75 },
    { label: "B 乙", value: 25 },
  ]);
  assert.equal(fallbackAccounts, 1);
  assert.equal(skipped, 1);          // 市值缺失的那条无法给占比
});

test("concentrationItems：risk.top 里 share_of_positions 为 null 也退回自身聚合（字段在但值缺）", () => {
  const groups = [{
    market: 100,
    risk: { top: [{ symbol: "NVDA", name: "英伟达", market_value: 100, share_of_positions: null }] },
    positions: [{ symbol: "NVDA", name: "英伟达", market_value: 100 }],
  }];
  const { items, fallbackAccounts } = concentrationItems(groups);
  assert.deepEqual(items, [{ label: "NVDA 英伟达", value: 100 }]);
  assert.equal(fallbackAccounts, 1);
});

test("concentrationItems：账户持仓市值合计为 0/全缺失 → 不产出条目并如实计数（不画 0%）", () => {
  const { items, skipped, fallbackAccounts } = concentrationItems([{
    market: 1, positions: [{ symbol: "A", market_value: null }, { symbol: "B", market_value: 0 }],
  }]);
  assert.deepEqual(items, []);
  assert.equal(skipped, 2);
  assert.equal(fallbackAccounts, 1);
});

test("concentrationItems：多账户按输入顺序拼接（与集中度表格同序），空数组不炸", () => {
  const groups = [
    { market: 1, risk: { top: [{ symbol: "HK1", name: "港一", share_of_positions: 60 }] } },
    { market: 3, risk: { top: [{ symbol: "SH1", name: "沪一", share_of_positions: 70 }] } },
  ];
  const { items } = concentrationItems(groups);
  assert.deepEqual(items, [
    { label: "HK1 港一", value: 60 },
    { label: "SH1 沪一", value: 70 },
  ]);
  assert.deepEqual(concentrationItems(null),
    { items: [], skipped: 0, fallbackAccounts: 0 });
});

// --------------------------------------------------------------- skipNoteText

test("skipNoteText：跳过量与未画量分开写；都为 0 时返回 null（不写「跳过 0 条」）", () => {
  assert.equal(skipNoteText({ skipped: 0, omitted: 0 }), null);
  assert.equal(skipNoteText(), null);
  assert.equal(skipNoteText({ skipped: 2, reason: "market_value 缺失或为 0" }),
    "跳过 2 条（market_value 缺失或为 0）");
  assert.equal(skipNoteText({ skipped: 2, omitted: 3, reason: "市值缺失", omittedReason: "只画前 5 条" }),
    "跳过 2 条（市值缺失）；未画 3 条（只画前 5 条）");
  assert.equal(skipNoteText({ omitted: 1 }), "未画 1 条（超出显示条数上限）");
});

// --------------------------------------------------------------- chartEmptyText

test("chartEmptyText：筛选空态原因优先于一切（含加载中），且原样返回而不改写口径", () => {
  const reason = "当前筛选：港股 HK —— 本页无港股数据（共 15 行，均属其他市场）。";
  assert.equal(chartEmptyText({
    reason, loading: true, count: 15, emptyText: "暂无持仓。", missingText: "字段全缺。",
  }), reason);
});

test("chartEmptyText：优先级 筛选原因 > 加载中 > 无数据 > 字段整列缺失", () => {
  const base = { reason: null, loading: false, loadingText: "持仓加载中…",
    count: 3, emptyText: "暂无持仓。", missingText: "market_value 全部缺失。" };
  assert.equal(chartEmptyText({ ...base, loading: true }), "持仓加载中…");
  assert.equal(chartEmptyText({ ...base, count: 0 }), "暂无持仓。");
  assert.equal(chartEmptyText(base), "market_value 全部缺失。");
  // 无数据优先于「字段缺失」：一行都没有时说「字段缺失」是误导
  assert.equal(chartEmptyText({ ...base, count: 0, missingText: "字段全缺" }), "暂无持仓。");
});

test("chartEmptyText：缺省参数不产生 undefined 文案（NaN 计数也算无数据）", () => {
  assert.equal(chartEmptyText(), "暂无数据。");
  assert.equal(chartEmptyText({ count: Number.NaN }), "暂无数据。");
  assert.equal(chartEmptyText({ count: 2 }), "");     // 未给 missingText → 空串（画布不写字）
});
