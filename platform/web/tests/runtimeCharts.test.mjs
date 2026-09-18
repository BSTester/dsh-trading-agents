// 流程页 / 调度页 / 执行页图表派生的纯函数（2026-09-18 用户需求：「在适当的地方增加一些
// 图表的展示」，第三批：流程页当日作业链时间轴、调度页作业时刻表、执行页订单分布）。
//
// 为什么派生只在 services 里、JSX 只做渲染：canvas 在 node 里画不出来，也断言不了像素——
// 「哪条该画、哪条被跳过、时间轴横轴的域是谁」是唯一能在 CI 里锁住的地方。前两批的教训
// 是空图看不出原因、缺值被当成 0，所以这里的每条口径都配一条用例：
//   1. **缺时刻不是 0 点**：两个时刻都缺的阶段不画在轨道上（画上去等于把「没跑过」画成
//      「从当天 0 点就在跑」），跳过并**如实计数**（`skipped`/`skippedLabels`）；
//   2. **当日基准日来自 payload.date，不是「现在」**：页面重看历史日子时，用今天的日期
//      去解释 "16:00" 会把整根轨道平移到错误的一天；
//   3. **图与表同源**：调度页的时间轴吃页面上那份 `viewScheduleJobs(...)` 过滤后的行，
//      本模块不再自己过滤市场；
//   4. **实测字段，不猜**：at 实测是**完整日期时间**（`2026-09-18 20:50:24`），scheduled
//      实测是**当日时刻**（`16:00`）——两者归一方式不同，用例分别钉住。
//
// `items` 一律把**每个阶段/作业**都交给图表（含两个时刻都缺的那些，start/end 为 null）：
// 跳过条数因此由 `timelineLayout` 与组件自己数一遍（组件会写出「跳过 N 项」），本模块另给
// `skippedLabels` 让页面能**点名**是哪些阶段没上轨道——只数不点名，用户仍要自己逐张卡片找。
import test from "node:test";
import assert from "node:assert/strict";
import {
  orderDistribution, pipelineTimelineItems, scheduleTimelineItems,
} from "../src/services/runtimeCharts.js";

/** 本地时区时间戳（`toTimestamp` 对无偏移字符串按运行环境本地时区解释，两端同口径）。 */
function localStamp(year, month, day, hour, minute, second = 0) {
  return new Date(year, month - 1, day, hour, minute, second).getTime();
}

/** 真机样本（2026-09-19 对 127.0.0.1:8397 /api/wb/pipeline 实测的摘录，字段原样）。
 *
 * 关键实测事实（用例据此写）：
 *   * `at` 是**完整日期时间**（"2026-09-18 20:50:24"），不是 "HH:MM:SS"；
 *   * `scheduled` 是**当日时刻**（"16:00"），必须配 `payload.date` 才能定位；
 *   * 两者可同时为 null（execute / digest），也可能一有一无。 */
function realPipeline() {
  return {
    date: "2026-09-19",
    markets: {
      SH: {
        stages: {
          sync_bars: { label: "行情同步", status: "pending", at: null, scheduled: "16:00", summary: "" },
          build_plan: { label: "计划生成", status: "pending", at: null, scheduled: "16:20", summary: "" },
          plan: { label: "计划", status: "ok", at: "2026-09-18 20:50:24", scheduled: null,
            summary: "PLN-20260918-sim-6815 frozen 9 单" },
          execute: { label: "执行", status: "failed", at: null, scheduled: null, summary: "cancelled 9" },
        },
      },
      HK: {
        stages: {
          sync_bars: { label: "行情同步", status: "ok", at: "2026-09-19 16:36:24", scheduled: "16:30",
            summary: "" },
          plan: { label: "计划", status: "pending", at: null, scheduled: null, summary: "无自动计划" },
        },
      },
      US: {
        stages: {
          sync_bars: { label: "行情同步", status: "pending", at: null, scheduled: "05:30", summary: "" },
          auto_execute: { label: "自动执行", status: "ok", at: "2026-09-19 05:31:02",
            scheduled: "05:31", summary: "" },
        },
      },
    },
    global: {
      stages: {
        sync_calendar: { label: "日历同步", status: "pending", at: null, scheduled: "18:50", summary: "" },
        reconcile: { label: "对账", status: "pending", at: null, scheduled: "19:00", summary: "" },
        digest: { label: "摘要", status: "pending", at: null, scheduled: null, summary: "" },
      },
    },
  };
}

/** 按行名取项（断言用，避免依赖下标）。 */
function byLabel(items, label) {
  return items.find((item) => item.label === label);
}

// ---------------------------------------------------------------- pipelineTimelineItems

test("pipelineTimelineItems：选中的市场 + GLOBAL 链，行名保序、带市场前缀、GLOBAL 标「全局」", () => {
  const { items } = pipelineTimelineItems(realPipeline(), { market: "SH" });
  assert.deepEqual(items.map((item) => item.label), [
    "全局 · 日历同步", "全局 · 对账", "全局 · 摘要",
    "A股 SH · 行情同步", "A股 SH · 计划生成", "A股 SH · 计划", "A股 SH · 执行",
  ]);
});

test("pipelineTimelineItems：scheduled 按 payload.date 定位（不是「现在」），只有计划时刻时为瞬时点", () => {
  const payload = realPipeline();
  payload.date = "2026-03-05";                      // 历史日子：不得用今天来解释 16:00
  const { items } = pipelineTimelineItems(payload, { market: "SH" });
  const row = byLabel(items, "A股 SH · 行情同步");
  assert.equal(row.start, localStamp(2026, 3, 5, 16, 0));
  assert.equal(row.end, row.start);                 // 只有计划时刻 → 单边时刻按瞬时点
  assert.equal(row.status, "pending");
});

test("pipelineTimelineItems：at 是完整日期时间时原样使用（不套当日基准日），start 是计划、end 是实际", () => {
  const { items } = pipelineTimelineItems(realPipeline(), { market: "HK" });
  const row = byLabel(items, "港股 HK · 行情同步");
  assert.equal(row.start, localStamp(2026, 9, 19, 16, 30));       // scheduled 16:30 → 当日 16:30
  assert.equal(row.end, localStamp(2026, 9, 19, 16, 36, 24));     // at 完整日期时间原样
  assert.ok(row.end > row.start);                                  // 漂移：计划 → 实际
  assert.equal(row.status, "ok");
});

test("pipelineTimelineItems：两个时刻都缺 → start/end 为 null（不画在 0 点）并如实计数 + 点名", () => {
  const { items, skipped, skippedLabels } = pipelineTimelineItems(realPipeline(), { market: "SH" });
  assert.equal(skipped, 2);
  assert.deepEqual(skippedLabels, ["全局 · 摘要", "A股 SH · 执行"]);
  // 行仍在 items 里（等长同序），只是 start/end 为 null —— 由 timelineLayout 判 valid=false
  assert.equal(items.length, 7);
  assert.equal(byLabel(items, "A股 SH · 执行").start, null);
  assert.equal(byLabel(items, "A股 SH · 执行").end, null);
  assert.equal(byLabel(items, "A股 SH · 执行").status, "failed");   // 状态仍如实带出
});

test("pipelineTimelineItems：悬停 note 同时写明计划时刻与实际时刻，并接上阶段摘要", () => {
  const hk = pipelineTimelineItems(realPipeline(), { market: "HK" }).items;
  assert.match(byLabel(hk, "港股 HK · 行情同步").note, /计划 16:30 → 实际 2026-09-19 16:36:24/);
  const sh = pipelineTimelineItems(realPipeline(), { market: "SH" }).items;
  assert.match(byLabel(sh, "A股 SH · 行情同步").note, /计划 16:00（当日尚无实际执行时刻）/);
  const onlyAt = byLabel(sh, "A股 SH · 计划");
  assert.match(onlyAt.note, /实际 2026-09-18 20:50:24（无计划时刻/);
  assert.match(onlyAt.note, /PLN-20260918-sim-6815 frozen 9 单/);   // 摘要接在后面
  assert.match(byLabel(sh, "A股 SH · 执行").note, /无计划时刻、也无实际时刻/);
});

test("pipelineTimelineItems：实际时刻不在 payload.date 当日时如实标注并计数（跨日）", () => {
  const { items, crossDay } = pipelineTimelineItems(realPipeline(), { market: "SH" });
  assert.match(byLabel(items, "A股 SH · 计划").note, /不在 2026-09-19 当日/);
  assert.equal(crossDay, 1);        // 只有 SH·plan 的 at 落在 2026-09-18
});

test("pipelineTimelineItems：「全部市场」= GLOBAL + 三个市场（顺序与页面卡片一致）", () => {
  const { items, skipped } = pipelineTimelineItems(realPipeline(), { market: "ALL" });
  assert.deepEqual(items.map((row) => row.label), [
    "全局 · 日历同步", "全局 · 对账", "全局 · 摘要",
    "A股 SH · 行情同步", "A股 SH · 计划生成", "A股 SH · 计划", "A股 SH · 执行",
    "港股 HK · 行情同步", "港股 HK · 计划",
    "美股 US · 行情同步", "美股 US · 自动执行",
  ]);
  assert.equal(skipped, 3);         // 全局·摘要、SH·执行、HK·计划
});

test("pipelineTimelineItems：日期缺失/非法时 HH:MM 无法定位 → 数出并给出原因（不猜当日）", () => {
  const payload = realPipeline();
  payload.date = null;
  const { items, skipped, note } = pipelineTimelineItems(payload, { market: "SH" });
  // 只剩带完整日期时间的 SH·plan 能定位
  assert.equal(byLabel(items, "A股 SH · 计划").start, localStamp(2026, 9, 18, 20, 50, 24));
  assert.equal(skipped, 6);         // 其余 6 个阶段只有 HH:MM（或什么都没有）
  assert.match(note, /日期/);
  assert.match(note, /HH:MM/);
});

test("pipelineTimelineItems：空载荷不产出项，也不把「还没读到」说成「没有阶段」", () => {
  const empty = pipelineTimelineItems(null);
  assert.deepEqual(empty.items, []);
  assert.equal(empty.skipped, 0);
  assert.match(empty.note, /尚未读取|为空/);
  const missing = pipelineTimelineItems({ date: "2026-09-19" }, { market: "SH" });
  assert.deepEqual(missing.items, []);
  assert.equal(missing.skipped, 0);
});

test("pipelineTimelineItems：status 原样透传，未知值不编中文", () => {
  const payload = realPipeline();
  payload.markets.SH.stages.sync_bars.status = "weird";
  const { items } = pipelineTimelineItems(payload, { market: "SH" });
  assert.equal(byLabel(items, "A股 SH · 行情同步").status, "weird");
});

// ---------------------------------------------------------------- scheduleTimelineItems

/** 真机样本（2026-09-19 对 /api/wb/schedule 实测的摘录）：键形如 `市场:作业名:日期`，
 *  值 `ran` 是**完整日期时间**；**没有**计划时刻字段。 */
function realJobs() {
  return [
    { job: "US:auto_execute:2026-09-18", ran: "2026-09-18 22:35:53" },
    { job: "GLOBAL:reconcile:2026-09-18", ran: "2026-09-18 20:50:00" },
    { job: "SH:build_plan:2026-09-18", ran: "2026-09-18 20:50:00" },
    { job: "SH:sync_bars:2026-09-18", ran: "2026-09-18 16:11:55" },
    { job: "XX:weird_job:2026-09-18", ran: "2026-09-18 16:00:00" },
    { job: "SH:quality:2026-09-18", ran: "" },
    { job: "SH:merge_announcements:2026-09-18", ran: null },
  ];
}

test("scheduleTimelineItems：按实际时刻升序，标签用流程端点给的中文阶段名（不自建映射表）", () => {
  const { items } = scheduleTimelineItems(realJobs(), { pipeline: realPipeline() });
  assert.deepEqual(items.slice(0, 5).map((row) => row.label), [
    "XX · weird_job",              // 未登记市场/作业 → 原样回显，不编中文
    "A股 SH · 行情同步",
    "全局 · 对账",
    "A股 SH · 计划生成",
    "美股 US · 自动执行",
  ]);
  assert.deepEqual(items.slice(0, 5).map((row) => row.start), [
    localStamp(2026, 9, 18, 16, 0), localStamp(2026, 9, 18, 16, 11, 55),
    localStamp(2026, 9, 18, 20, 50), localStamp(2026, 9, 18, 20, 50),
    localStamp(2026, 9, 18, 22, 35, 53),
  ]);
});

test("scheduleTimelineItems：调度端点没有计划时刻 → 每项都是瞬时点（start === end）", () => {
  const { items } = scheduleTimelineItems(realJobs(), { pipeline: realPipeline() });
  for (const row of items.slice(0, 5)) {
    assert.equal(row.end, row.start);
    assert.equal(row.status, undefined);     // 不编状态：调度行只有 ran
  }
});

test("scheduleTimelineItems：缺时刻的作业不画（start/end 为 null），如实计数并点名；note 带原始作业键", () => {
  const { items, skipped, skippedLabels } = scheduleTimelineItems(realJobs(), { pipeline: realPipeline() });
  assert.equal(skipped, 2);                  // quality 空串 + merge_announcements null
  assert.deepEqual(skippedLabels, ["A股 SH · quality", "A股 SH · merge_announcements"]);
  assert.equal(items.length, 7);             // 等长：行都在，缺时刻的排在末尾
  assert.equal(items[6].start, null);
  assert.match(items[0].note, /作业键 XX:weird_job:2026-09-18/);
});

test("scheduleTimelineItems：没有流程快照时标签回落原始作业名，并给出说明（不静默）", () => {
  const { items, note } = scheduleTimelineItems(
    [{ job: "SH:build_plan:2026-09-18", ran: "2026-09-18 16:20:00" }]);
  assert.equal(items[0].label, "A股 SH · build_plan");
  assert.match(note, /流程快照/);
});

test("scheduleTimelineItems：空作业表不产出项（空态由页面按加载/休市原因给出）", () => {
  const empty = scheduleTimelineItems([], { pipeline: realPipeline() });
  assert.deepEqual(empty.items, []);
  assert.equal(empty.skipped, 0);
  const junk = scheduleTimelineItems(null, { pipeline: realPipeline() });
  assert.deepEqual(junk.items, []);
});

// ---------------------------------------------------------------- orderDistribution

/** 真机样本 A（2026-09-19 /api/wb/snapshot 的 trade_summary.orders 摘录）：券商订单表，
 *  `side` 是服务端按工具 schema 给出的**中文**，`status_code` 是**未公开枚举**的原码。 */
function realSnapshot() {
  return { orders: [
    { order_id: "7138921", symbol: "00981", side: "卖出", side_code: 2, status_code: 4, fill: "全部成交" },
    { order_id: "7138920", symbol: "03986", side: "卖出", side_code: 2, status_code: 5, fill: "未成交" },
    { order_id: "7138918", symbol: "00100", side: "卖出", side_code: 2, status_code: 4, fill: "全部成交" },
    { order_id: "7138917", symbol: "00001", side: "买入", side_code: 1, status_code: 4, fill: "全部成交" },
    { order_id: "7138916", symbol: "02513", side: "买入", side_code: 1, status_code: 4, fill: "部分成交" },
    { order_id: "7138915", symbol: "02514", side: "买入", side_code: 1, status_code: 3, fill: "未知" },
  ] };
}

test("orderDistribution：bySide 用服务端给的中文方向（数字码 1/2 按 labels.py SIDE 归一）", () => {
  const { bySide, total, skipped } = orderDistribution([realSnapshot()]);
  assert.deepEqual(bySide, [{ label: "买入", value: 3 }, { label: "卖出", value: 3 }]);
  assert.equal(total, 6);
  assert.equal(skipped, 0);
});

test("orderDistribution：byFill 与 byStatus（状态原码原样，不猜标签）", () => {
  const { byFill, byStatus } = orderDistribution([realSnapshot()]);
  assert.deepEqual(byFill, [
    { label: "全部成交", value: 3 }, { label: "未成交", value: 1 },
    { label: "未知", value: 1 }, { label: "部分成交", value: 1 },
  ]);
  assert.deepEqual(byStatus, [
    { label: "4", value: 4 }, { label: "3", value: 1 }, { label: "5", value: 1 },
  ]);
});

test("orderDistribution：OpenAPI 信封（groups[].rows，数字方向码）也能聚合", () => {
  const openapi = { groups: [
    { acc_id: "9393", market: "HK", rows: [
      { order_id: "1", side: 2, status: 5 },
      { order_id: "2", side: 2, status: 4 },
    ] },
    { acc_id: "3182575", market: "SH", rows: [{ order_id: "3", side: 1, status: 2 }] },
    { acc_id: "11587526", market: "US", rows: [] },
  ] };
  const { bySide, byStatus, total } = orderDistribution([openapi]);
  assert.deepEqual(bySide, [{ label: "卖出", value: 2 }, { label: "买入", value: 1 }]);
  assert.deepEqual(byStatus, [{ label: "2", value: 1 }, { label: "4", value: 1 }, { label: "5", value: 1 }]);
  assert.equal(total, 3);
});

test("orderDistribution：多端点合并；缺字段的行计入对应维度的跳过数（三种计数不混算）", () => {
  const open = { groups: [{ rows: [{ order_id: "a", side: 1, status: 2 }] }] };
  const history = { orders: [
    { order_id: "b", side: null, status: 4, fill: null },        // 缺方向
    { order_id: "c", side: "BUY" },                              // 缺状态与成交情况
    { order_id: "d" },                                           // 三个维度都缺
    "not-a-row",                                                 // 非对象行，忽略（不计入 total）
  ] };
  const result = orderDistribution([open, history]);
  assert.equal(result.total, 4);
  assert.deepEqual(result.bySide, [{ label: "买入", value: 2 }]);   // 1 与 "BUY" 归一到同一桶
  assert.deepEqual(result.byStatus, [{ label: "2", value: 1 }, { label: "4", value: 1 }]);
  assert.deepEqual(result.byFill, []);
  assert.equal(result.skippedSide, 2);       // b、d
  assert.equal(result.skippedStatus, 2);     // c、d
  assert.equal(result.skippedFill, 4);       // b、c、d、以及 open 的那条
  assert.equal(result.skipped, 1);           // d：三个维度都没有 → 一条都没画进去
});

test("orderDistribution：未知方向码/未知状态原样展示，不硬塞进已知枚举", () => {
  const { bySide, byStatus } = orderDistribution([{ orders: [
    { side: 3, status: "weird_code" },
    { side: "MARGIN", status: 7 },
  ] }]);
  assert.deepEqual(bySide, [{ label: "3", value: 1 }, { label: "MARGIN", value: 1 }]);
  assert.deepEqual(byStatus, [{ label: "7", value: 1 }, { label: "weird_code", value: 1 }]);
});

test("orderDistribution：空载荷/空行不产出分布，total 为 0（页面据 total 给空态）", () => {
  for (const payloads of [[], null, [{ groups: [] }], [{ orders: [] }], ["x"]]) {
    const result = orderDistribution(payloads);
    assert.deepEqual(result.bySide, []);
    assert.deepEqual(result.byStatus, []);
    assert.deepEqual(result.byFill, []);
    assert.equal(result.total, 0);
    assert.equal(result.skipped, 0);
  }
});
