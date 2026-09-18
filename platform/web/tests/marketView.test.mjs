// 页面级市场视图纯函数（2026-09-18 用户需求：各页面按市场分类查看）。
//
// 与 marketFilter.test.mjs 的分工：那一份测「市场值/标的 → 市场链」的归一口径；
// 本份测「payload → 过滤后的行 + 空态原因」这层页面判断——页面 JSX 里不再写 if/else。
// 用例里的数据形状全部取自 2026-09-18 对 127.0.0.1:8397 的实测响应（见各用例注释）：
//   positions.groups[].market = 1/3/100（富途 market_id，数字）
//   deals_today/orders_open.groups[].market = 'HK'/'SH'/'US' + '9'/'10'/'16'（类型码）
//   plan.plans[] **没有** market 字段，只有 target{"SH.600031":0.25} 与 orders[].symbol
//   schedule.jobs[].job = "SH:build_plan:2026-09-18"（市场在字符串前缀里）
//   snapshot.previews[].value.ticker（标的是嵌套字段）
import test from "node:test";
import assert from "node:assert/strict";
import { MARKET_ALL } from "../src/services/marketFilter.js";
import {
  MARKET_CHAIN_SHORT, emptyStateReason, jobMarketChain, marketDisplay, marketLabelOf,
  planChainsOf, planMarketDisplay, symbolMarketDisplay, symbolMarketNote,
  viewByMarket, viewBySymbol, viewGroups, viewPlans, viewScheduleJobs, viewSignalPreviews,
} from "../src/services/marketView.js";

test("marketLabelOf：选中市场的展示名与页头下拉同源", () => {
  assert.equal(marketLabelOf(MARKET_ALL), "全部市场");
  assert.equal(marketLabelOf("SH"), "A股 SH");
  assert.equal(marketLabelOf("HK"), "港股 HK");
  assert.equal(marketLabelOf("US"), "美股 US");
  // 未登记取值回落键本身（看到英文键即「这里还没登记标签」，不抛错）
  assert.equal(marketLabelOf("JP"), "JP");
});

test("marketDisplay/symbolMarketDisplay：展示值与筛选口径同源", () => {
  // 券商 market_id 与交易所前缀都显示成同一个中文标签
  assert.equal(marketDisplay(1), "港股 HK");
  assert.equal(marketDisplay(3), "A股 SH");
  assert.equal(marketDisplay(100), "美股 US");
  assert.equal(marketDisplay("sz"), "A股 SH");
  // 非市场码原样展示（不猜、不硬塞进某个市场）
  assert.equal(marketDisplay("9"), "9");
  assert.equal(marketDisplay(null), "—");
  assert.equal(symbolMarketDisplay("HK.00700"), "港股 HK");
  assert.equal(symbolMarketDisplay("SZ.002475"), "A股 SH");
  // 无前缀的裸代码（实测 execution/audit 的 '00981'）展示 — 表示「无法判定」
  assert.equal(symbolMarketDisplay("00981"), "—");
});

test("emptyStateReason：写明是哪个市场没有数据，并给出总行数", () => {
  const reason = emptyStateReason("HK", { total: 37, kept: 0, unclassified: 0 });
  assert.match(reason, /当前筛选：港股 HK/);
  assert.match(reason, /本页无港股数据/);
  assert.match(reason, /共 37 行/);
  // 「全部市场」下有数据就不该有空态原因
  assert.equal(emptyStateReason(MARKET_ALL, { total: 37, kept: 37 }), null);
  // 有数据也不给
  assert.equal(emptyStateReason("HK", { total: 37, kept: 3 }), null);
  // 数据本身为空：交给页面既有空态（不冒充「筛选没数据」）
  assert.equal(emptyStateReason("HK", { total: 0, kept: 0 }), null);
  // 全部行都无法判定市场：如实说明，不硬塞进某个市场
  const blind = emptyStateReason("US", { total: 2, kept: 0, unclassified: 2 });
  assert.match(blind, /当前筛选：美股 US/);
  assert.match(blind, /市场标识无法判定/);
  assert.match(blind, /无法归入该市场，故未列出/);
  assert.doesNotMatch(blind, /均属其他市场/);
  assert.deepEqual(MARKET_CHAIN_SHORT, { SH: "A股", HK: "港股", US: "美股" });
});

test("viewBySymbol：按标的前缀过滤；ALL 不丢任何行（含无前缀行）", () => {
  // 实测 factors.rows 形状（ticker 带交易所前缀，可跨市场混排）
  const rows = [
    { ticker: "SH.600036", rank: 1 }, { ticker: "SH.600519", rank: 2 },
    { ticker: "HK.00700", rank: 3 }, { ticker: "US.AAPL", rank: 4 },
  ];
  assert.deepEqual(viewBySymbol(rows, "HK", "ticker").rows.map((r) => r.ticker), ["HK.00700"]);
  assert.deepEqual(viewBySymbol(rows, "SH", "ticker").rows.map((r) => r.rank), [1, 2]);
  const all = viewBySymbol(rows, MARKET_ALL, "ticker");
  assert.equal(all.rows.length, 4, "全部市场不得丢任何行");
  assert.equal(all.emptyReason, null);
  // 无前缀行：ALL 下照常显示；选中具体市场时被排除且如实计入 unclassified
  const bare = [{ symbol: "00981" }, { symbol: "HK.00700" }];
  assert.equal(viewBySymbol(bare, MARKET_ALL).rows.length, 2);
  const hk = viewBySymbol(bare, "HK");
  assert.deepEqual(hk.rows.map((r) => r.symbol), ["HK.00700"]);
  assert.equal(hk.unclassified, 1);
});

test("viewByMarket：自带 market 字段的行按市场过滤（非市场码归 null）", () => {
  const rows = [{ market: "SH", id: "a" }, { market: 1, id: "b" },
                { market: "9", id: "c" }, { id: "d" }];
  assert.deepEqual(viewByMarket(rows, "HK").rows.map((r) => r.id), ["b"]);
  assert.deepEqual(viewByMarket(rows, "SH").rows.map((r) => r.id), ["a"]);
  assert.equal(viewByMarket(rows, MARKET_ALL).rows.length, 4, "全部市场不得丢行（含 '9' 与缺字段行）");
  assert.deepEqual(viewByMarket(rows, "US").rows, []);
});

test("viewGroups：数字 market_id 与字符串市场码都按同一口径过滤", () => {
  // 实测 positions：groups[].market = 1/3/100（数字 market_id）
  const positions = [
    { acc_id: "9393", market: 1, positions: [{ symbol: "00981" }, { symbol: "09988" }] },
    { acc_id: "3182575", market: 3, positions: [{ symbol: "603993" }] },
    { acc_id: "11587526", market: 100, positions: [{ symbol: "NVDA" }] },
  ];
  const hk = viewGroups(positions, "HK");
  assert.deepEqual(hk.groups.map((g) => g.acc_id), ["9393"]);
  assert.equal(hk.rows.length, 2, "默认按 group.positions 取行");
  const sh = viewGroups(positions, "SH");
  assert.deepEqual(sh.groups.map((g) => g.acc_id), ["3182575"]);
  assert.equal(viewGroups(positions, MARKET_ALL).groups.length, 3, "全部市场不得丢任何组");
  // 实测 deals_today：groups[].market = 'HK'/'SH'/'US' + '9'/'10'/'16'（账户类型码）
  const deals = [
    { acc_id: "9393", market: "HK", rows: [] },
    { acc_id: "3182575", market: "SH", rows: [{ deal_id: "1" }] },
    { acc_id: "6683020", market: "13", rows: [{ deal_id: "2" }] },
  ];
  const dealsHk = viewGroups(deals, "HK");
  assert.deepEqual(dealsHk.groups.map((g) => g.acc_id), ["9393"]);
  assert.equal(dealsHk.rows.length, 0);
  // HK 账户存在但一行都没有：这是「本页无港股数据」，必须给空态原因
  assert.match(dealsHk.emptyReason, /当前筛选：港股 HK/);
  assert.match(dealsHk.emptyReason, /本页无港股数据/);
  // 缺 market 字段的组只在全部市场下显示
  const withBlind = viewGroups([{ acc_id: "x", rows: [{ deal_id: "9" }] }], "US");
  assert.deepEqual(withBlind.groups, []);
  assert.match(withBlind.emptyReason, /市场标识无法判定/);
});

test("planChainsOf/planMarketDisplay：计划没有 market 字段，市场由 target 与订单标的派生", () => {
  // 实测 plan.plans[0]：{target: {"SH.600031": 0.25}, orders:[{symbol:"SH.600031"}]}
  const plan = {
    plan_id: "PLN-20260918-sim-6815", target: { "SH.600031": 0.25, "SZ.002475": 0.1 },
    orders: [{ symbol: "SH.600031" }, { symbol: "SZ.002475" }],
  };
  assert.deepEqual(planChainsOf(plan), ["SH"], "SZ 与 SH 同属 A 股链");
  assert.equal(planMarketDisplay(plan), "A股 SH");
  assert.equal(planMarketDisplay({ plan_id: "p", orders: [{ symbol: "HK.00700" }] }), "港股 HK");
  // 混合市场计划：如实标「混合」，不挑一个市场冒充
  const mixed = { target: {}, orders: [{ symbol: "SH.600031" }, { symbol: "HK.00700" }] };
  assert.deepEqual(planChainsOf(mixed), ["SH", "HK"]);
  assert.equal(planMarketDisplay(mixed), "混合（A股 SH、港股 HK）");
  // 无标的计划：无法判定
  assert.deepEqual(planChainsOf({ plan_id: "p" }), []);
  assert.equal(planMarketDisplay({ plan_id: "p" }), "—");
});

test("viewPlans：混合市场计划在两个市场下都可见；无标的计划计入无法判定", () => {
  const plans = [
    { plan_id: "sh", orders: [{ symbol: "SH.600031" }] },
    { plan_id: "hk", orders: [{ symbol: "HK.00700" }] },
    { plan_id: "mixed", orders: [{ symbol: "SH.600036" }, { symbol: "US.AAPL" }] },
    { plan_id: "blind", orders: [] },
  ];
  assert.equal(viewPlans(plans, MARKET_ALL).rows.length, 4, "全部市场不得丢任何计划");
  assert.deepEqual(viewPlans(plans, "US").rows.map((p) => p.plan_id), ["mixed"]);
  assert.deepEqual(viewPlans(plans, "SH").rows.map((p) => p.plan_id), ["sh", "mixed"]);
  const hk = viewPlans(plans, "HK");
  assert.deepEqual(hk.rows.map((p) => p.plan_id), ["hk"]);
  assert.equal(hk.unclassified, 1, "无标的计划无法判定归属");
  assert.equal(viewPlans([{ plan_id: "blind", orders: [] }], "HK").emptyReason !== null, true);
  assert.equal(viewPlans([], "HK").emptyReason, null, "计划本身为空时不算筛选空态");
});

test("jobMarketChain/viewScheduleJobs：作业键取市场前缀；无市场维度的行保持显示", () => {
  // 实测 schedule.jobs[].job = "SH:build_plan:2026-09-18" / "GLOBAL:reconcile:..."
  assert.equal(jobMarketChain("SH:build_plan:2026-09-18"), "SH");
  assert.equal(jobMarketChain("HK:sync_bars:2026-09-18"), "HK");
  assert.equal(jobMarketChain("US:factors_snapshot:2026-09-18"), "US");
  assert.equal(jobMarketChain("GLOBAL:reconcile:2026-09-18"), null, "GLOBAL 不是市场");
  assert.equal(jobMarketChain("没有冒号的作业名"), null);
  const jobs = [
    { job: "GLOBAL:reconcile:2026-09-18", ran: "1" },
    { job: "SH:build_plan:2026-09-18", ran: "1" },
    { job: "HK:build_plan:2026-09-18", ran: "1" },
    { job: "US:build_plan:2026-09-18", ran: "1" },
  ];
  assert.equal(viewScheduleJobs(jobs, MARKET_ALL).rows.length, 4, "全部市场不得丢行");
  const hk = viewScheduleJobs(jobs, "HK");
  assert.deepEqual(hk.rows.map((r) => r.job),
    ["GLOBAL:reconcile:2026-09-18", "HK:build_plan:2026-09-18"],
    "无市场维度的 GLOBAL 行保持显示（它不属于任何单个市场）");
  assert.equal(hk.marketless, 1);
  assert.equal(hk.emptyReason, null);
  // 一个市场作业都没有时仍要说清：是全空态还是被筛掉
  const onlySh = viewScheduleJobs([{ job: "SH:build_plan:2026-09-18" }], "US");
  assert.deepEqual(onlySh.rows, []);
  assert.match(onlySh.emptyReason, /当前筛选：美股 US/);
});

test("viewSignalPreviews：previews 的标的在 value.ticker 里（嵌套），按它过滤", () => {
  // 实测 snapshot.previews[].value 形状：{ticker, strategy, ...}
  const previews = [
    { id: "1", kind: "signal", value: { ticker: "SH.600519" } },
    { id: "2", kind: "signal", value: { ticker: "HK.00700" } },
    { id: "3", kind: "backtest", value: {} },
    { id: "4", kind: "ledger", value: null },
  ];
  assert.equal(viewSignalPreviews(previews, MARKET_ALL).rows.length, 4, "全部市场不得丢行");
  assert.deepEqual(viewSignalPreviews(previews, "HK").rows.map((r) => r.id), ["2"]);
  const us = viewSignalPreviews(previews, "US");
  assert.deepEqual(us.rows, []);
  // 无法判定的只有 value 为空/{}/无 ticker 的行（id 3、4）；id 1/2 是「别的市场」，不算无法判定
  assert.equal(us.unclassified, 2, "value 里没有可判定标的的行计入无法判定");
  assert.match(us.emptyReason, /当前筛选：美股 US/);
});

test("symbolMarketNote/symbolMarketNote：单标的页面只提示不隐藏（与行情页同口径）", () => {
  // 「全部市场」下无需提示
  assert.equal(symbolMarketNote("SH.600519", MARKET_ALL), null);
  // 一致：无需提示
  assert.equal(symbolMarketNote("SH.600519", "SH"), null);
  assert.equal(symbolMarketNote("SZ.002475", "SH"), null);
  // 不一致：提示里写清两边市场名
  const mismatch = symbolMarketNote("SH.600519", "HK");
  assert.match(mismatch, /A股 SH/);
  assert.match(mismatch, /港股 HK/);
  // 无前缀：如实说无法判定，不猜
  const blind = symbolMarketNote("00981", "HK");
  assert.match(blind, /无法判定/);
  assert.match(blind, /00981/);
});
