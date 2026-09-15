// WP7 任务 4：Harness 富途写通道收窄（独立成文件的理由：这是 WP7 的**新不变量**——
// futu 写类在 Harness 进程内不可达；wp6-approval-regression.test.mjs 守的是 WP6 验收
// 回归链（R1 原样保留、R2 就地改写），混在一起会模糊两个工作包的边界，也让 WP7 的
// 红灯证据不干净。分类依据 = docs/TOOL-LIMITS.md §七 + trading_core/broker.py 锁定
// 工具名，分类表见 plugins/engine/src/policy.js 头注。
//
// 新不变量一句话：**futu 写类（下单/改单/撤单动词）guard 一律拒绝并指引工作台，
// 不分模式，且不产生任何待确认**。业务确认移至工作台服务侧（quantwb trade_* 工具，
// Web 确认卡片作答）。
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { WorkbenchStore } from "../plugins/workbench/src/store.js";
import { installTradingPolicy } from "../plugins/engine/src/policy.js";

async function inTempHome(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp7-policy-"));
  const previous = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  t.after(async () => {
    if (previous === undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME = previous;
    await rm(home, { recursive: true, force: true });
  });
  return home;
}

function fakeHarness(store) {
  const guards = [];
  const hooks = {};
  const ctx = {
    tradingWorkbench: store,
    tools: { guard: (fn) => guards.push(fn) },
    on: (event, fn) => { (hooks[event] ??= []).push(fn); },
  };
  installTradingPolicy(ctx);
  return { guards, hooks, guard: guards[0], preExecute: hooks["tools/pre-execute"][0] };
}

const execOf = (name, args = {}) => ({ name, arguments: args,
  agent: { session: { id: "s1" } }, signal: new AbortController().signal });
const ALLOW = async () => ({ kind: "allow" });

// 写类全集：下单/改单/撤单动词 × sim_trade_*/trading_* 两族（含 order_place 命名变体）
const WRITE_TOOLS = [
  "mcp__futu__trading_input_order", "mcp__futu__trading_order_place",
  "mcp__futu__trading_modify_order", "mcp__futu__trading_cancel_order",
  "mcp__futu__sim_trade_input_order", "mcp__futu__sim_trade_place_order",
  "mcp__futu__sim_trade_modify_order", "mcp__futu__sim_trade_cancel_order",
];

// ===================== 一、写类：guard 一律拒绝（不分模式） =====================

test("live 下 futu 写类 guard 拒绝，文案含「请通过工作台交易」并指引 quantwb trade_*/计划执行", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  const { guard } = fakeHarness(store);
  const refusal = String(guard(execOf("mcp__futu__trading_modify_order", { order_id: "7137795" })));
  assert.match(refusal, /请通过工作台交易/);
  assert.match(refusal, /trade_\*/, "必须指引 quantwb 的 trade_* 工具");
  assert.match(refusal, /计划执行/, "必须指引计划执行这条通道");
});

test("sim 下 futu 写类同样拒绝：收窄不分模式（sim_trade_place_order）", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore(); // 默认 sim
  const { guard } = fakeHarness(store);
  const refusal = String(guard(execOf("mcp__futu__sim_trade_place_order",
    { acc_id: "A1", market: 1, symbol: "00700", order_type: 1, order_side: 1, qty: 100, price: 428.4 })));
  assert.match(refusal, /请通过工作台交易/);
  assert.match(refusal, /trade_\*/);
  assert.match(refusal, /计划执行/);
});

test("收窄覆盖写动词全集：下单/改单/撤单 × 两族，sim 与 live 皆拒", async (t) => {
  await inTempHome(t);
  for (const mode of ["sim", "live"]) {
    const store = new WorkbenchStore();
    if (mode === "live") await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
    const { guard } = fakeHarness(store);
    for (const name of WRITE_TOOLS) {
      const refusal = guard(execOf(name, {}));
      assert.ok(typeof refusal === "string" && refusal.length > 0, `${mode} 下 ${name} 应被拒绝`);
      assert.match(String(refusal), /请通过工作台交易/, name);
    }
  }
});

// ===================== 二、只读不受影响 =====================

test("行情/资讯类放行：guard 返回 undefined，两种模式皆然", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore(); // sim
  const { guard } = fakeHarness(store);
  for (const name of ["mcp__futu__quote_news_search", "mcp__futu__quote_trading_days"]) {
    assert.equal(guard(execOf(name, {})), undefined, name);
  }
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  for (const name of ["mcp__futu__quote_news_search", "mcp__futu__quote_trading_days"]) {
    assert.equal(guard(execOf(name, {})), undefined, name);
  }
});

test("本模式的账户查询放行（收窄不碰研究需要的只读查询）", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore(); // sim
  const { guard } = fakeHarness(store);
  for (const name of ["mcp__futu__sim_trade_position_list", "mcp__futu__sim_trade_cash_info",
    "mcp__futu__sim_trade_history_order_list", "mcp__futu__sim_trade_max_buy_sell"]) {
    assert.equal(guard(execOf(name, {})), undefined, name);
  }
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  for (const name of ["mcp__futu__account_positions", "mcp__futu__account_cash_info"]) {
    assert.equal(guard(execOf(name, {})), undefined, name);
  }
});

test("读类保留模式互斥（R1 口径）：模式外的账户查询仍拒并提示切模式", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore(); // sim
  const { guard } = fakeHarness(store);
  assert.match(String(guard(execOf("mcp__futu__account_positions", {}))), /账户模式/);
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.match(String(guard(execOf("mcp__futu__sim_trade_position_list", {}))), /账户模式/);
});

// ===================== 三、pre-execute：业务确认退役 =====================

test("pre-execute 不再发起业务确认：requestConfirmation 不被调用、无待确认、next 正常放行", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  let confirmCalls = 0;
  const original = store.requestConfirmation.bind(store);
  store.requestConfirmation = (...args) => { confirmCalls += 1; return original(...args); };
  const { guard, preExecute } = fakeHarness(store);

  // 写类在 guard 已拒（Harness 内不可达）
  assert.match(String(guard(execOf("mcp__futu__trading_input_order", { qty: 1 }))), /请通过工作台交易/);
  // 即便（异常情况下）到达 pre-execute，也不发起确认，只透传 next 的结论
  const decision = await preExecute(execOf("mcp__futu__trading_input_order",
    { acc_id: "A1", market: 100, symbol: "AAPL", order_type: 1, order_side: 1, qty: 1 }), ALLOW);
  assert.equal(decision.kind, "allow", "pre-execute 对写类只放行透传（决策=allow）");
  assert.equal(confirmCalls, 0, "store.requestConfirmation 不得被调用");
  assert.equal(store.confirmationView(), null, "不得产生任何待确认");
});

// ===================== 四、非 futu 工具不受影响；execute/result 保留 =====================

test("非 futu 工具完全不受影响：guard/execute/result 都不介入", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  const { guard, preExecute, hooks } = fakeHarness(store);
  assert.equal(guard(execOf("fin_news", {})), undefined);
  let nextRan = 0;
  const decision = await preExecute(execOf("fin_news", {}), async () => { nextRan += 1; return { kind: "allow" }; });
  assert.equal(decision.kind, "allow");
  assert.equal(nextRan, 1);
  let executed = 0;
  await hooks["tools/execute"][0](execOf("fin_news", {}), async () => { executed += 1; return { isError: false }; });
  assert.equal(executed, 1, "非账户工具直接放行，不进租约");
  const before = store.pendingObservations().length;
  hooks["tools/result"][0](execOf("fin_news", {}), { isError: false, value: {} });
  assert.equal(store.pendingObservations().length, before, "非账户工具不留观察");
});

test("tools/execute 租约与 tools/result 观察保留（账户读类照常生效）", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore(); // sim
  const { hooks } = fakeHarness(store);
  const readExec = execOf("mcp__futu__sim_trade_position_list", { acc_id: "A1", market: 1 });
  await hooks["tools/execute"][0](readExec, async () => {
    assert.equal(store.snapshot().in_flight, 1, "租约期内 in_flight=1");
    return { isError: false, value: { position_list: [] } };
  });
  assert.equal(store.snapshot().in_flight, 0, "结束后租约释放");
  hooks["tools/result"][0](readExec, { isError: false, value: { position_list: [] } });
  assert.equal(store.snapshot().broker.value.position_list.length, 0, "最终响应照记");
});

// ===================== 五、store 确认方法保留（legacy，不删） =====================

test("store.requestConfirmation/decideConfirmation 仍可用（legacy 面板过渡期 + 服务侧同语义）", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  const settling = store.requestConfirmation({
    tool: "mcp__futu__trading_input_order", mode: "live", args: { qty: 1 }, session_id: "s1" });
  const view = store.confirmationView();
  assert.ok(view, "store 层确认流保留：仍能产生并裁决待确认");
  store.decideConfirmation({ id: view.id, decision: "approved" });
  assert.equal((await settling).decision, "approved");
});

// ===================== 六、fail-closed：未知动词（WP7 任务 5 序言修复） =====================
// 拒绝名单只列已知写动词的话，上游新增写动词（如 trading_order_place_v2）会被当
// 读放行——live 下危险。两族策略有意不同，理由见 policy.js 头注：
//   trading_*  未知动词一律按写拒绝（fail-closed，实盘不能赌）；
//   sim_trade_* 未知动词按读处理（模式桶 sim 互斥照旧，sim 写伤害有界）。

test("trading 族未知动词 fail-closed：trading_order_place_v2 按写拒绝，sim/live 皆然", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore(); // sim
  const { guard } = fakeHarness(store);
  assert.match(String(guard(execOf("mcp__futu__trading_order_place_v2", {}))),
    /请通过工作台交易/, "sim 下未知实盘动词也必须按写拒绝");
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.match(String(guard(execOf("mcp__futu__trading_order_place_v2", {}))),
    /请通过工作台交易/, "live 下未知实盘动词必须按写拒绝（fail-closed 核心场景）");
});

test("sim_trade 族未知动词按读处理：sim_trade_unknown_query 走 sim 桶模式互斥", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore(); // sim → 本模式读查询放行
  const { guard } = fakeHarness(store);
  assert.equal(guard(execOf("mcp__futu__sim_trade_unknown_query", {})),
    undefined, "sim 族未知动词按读：本模式（sim）放行");
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.match(String(guard(execOf("mcp__futu__sim_trade_unknown_query", {}))),
    /账户模式/, "sim 族未知动词按读：模式外（live）仍被互斥拒绝");
});
