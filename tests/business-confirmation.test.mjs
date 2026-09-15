// 实盘业务确认（方案 A）的回归测试。
//
// 核心不变量一句话：**实盘写操作永不返回 `{kind:"ask"}`**。
//
// 为什么这条最重要：DSH 的审批在 full-access（approval policy = "never"）下会
// 直接返回 rejected —— `dsh-user-approval` 的 decide() 第一句判断就是
// `if (effectivePolicy(session) === "never") return "rejected"`，连问都不问。
// 于是 `{kind:"ask"}` 表现为 `the user rejected tool ...`：看起来像用户拒绝了，
// 实际没有任何人被问过。
//
// 业务确认回答的是另一个问题（"这笔单子对不对"），所以它由交易插件自己发起、
// 由工作台界面作答，全程不经过 approval 系统 —— 只要返回的不是 `ask`，
// 会话的审批档位就影响不到它。
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const { WorkbenchStore, WorkbenchError, CONFIRM_TTL_MS, describeOrderArgs, orderOperation } =
  await import("../plugins/workbench/src/store.js");
const { createRpcHandler } = await import("../plugins/workbench/src/rpc.js");
const { installTradingPolicy } = await import("../plugins/engine/src/policy.js");

/** 一次性 DSH_HOME：绝不动用户真实的 ~/.dsh。 */
function home(t) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "confirm-test-"));
  const previous = process.env.DSH_HOME;
  process.env.DSH_HOME = dir;
  t.after(() => {
    if (previous === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = previous;
  });
  return dir;
}

function storeIn(dir, mode = "live") {
  writeFileSync(path.join(dir, "trading-account-mode"), `${mode}\n`);
  return new WorkbenchStore();
}

function harness(store) {
  const hooks = {};
  const guards = [];
  const ctx = {
    tradingWorkbench: store,
    tools: { guard: (fn) => guards.push(fn) },
    on: (event, fn) => { (hooks[event] ??= []).push(fn); },
  };
  installTradingPolicy(ctx);
  return { hooks, guards, preExecute: hooks["tools/pre-execute"][0] };
}

const ALLOW = async () => ({ kind: "allow" });
const LIVE_ORDER = {
  name: "mcp__futu__trading_input_order",
  arguments: { acc_id: "281756480774050900", market: 100, symbol: "TSLL",
    order_type: 1, order_side: 1, qty: 4, price: 9.30 },
  agent: { session: { id: "s1" } },
};
const execOf = (name, args = {}) => ({ name, arguments: args,
  agent: { session: { id: "s1" } }, signal: new AbortController().signal });

// ===================== 一、接口层：永不 ask =====================

test("实盘写操作返回的不是 ask —— 这是 full-access 下仍能下单的机制保证", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const { preExecute } = harness(store);

  const settling = preExecute(execOf(LIVE_ORDER.name, LIVE_ORDER.arguments), ALLOW);
  await new Promise((r) => setTimeout(r, 30));
  const view = store.confirmationView();
  assert.ok(view, "应当产生一笔待确认");
  assert.equal(view.operation, "下单");
  // 摘要必须是中文可核对字段
  const labels = view.summary.fields.map((f) => f.label);
  for (const field of ["账户", "市场", "标的", "方向", "数量", "价格"]) {
    assert.ok(labels.includes(field), `摘要缺少「${field}」`);
  }
  assert.equal(view.summary.fields.find((f) => f.label === "方向").value, "买入（order_side=1）");
  assert.equal(view.summary.fields.find((f) => f.label === "市场").value, "100（美股）");
  // 原始参数一并保留，便于核对
  assert.equal(view.summary.raw.qty, 4);

  store.decideConfirmation({ id: view.id, decision: "approved" });
  const decision = await settling;
  assert.notEqual(decision.kind, "ask", "绝不能返回 ask：那会落到会话的审批档位上被静默拒绝");
  assert.equal(decision.kind, "allow");
});

test("用户拒绝 → deny，且提示这是业务确认不是权限问题", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const { preExecute } = harness(store);
  const settling = preExecute(execOf(LIVE_ORDER.name, LIVE_ORDER.arguments), ALLOW);
  await new Promise((r) => setTimeout(r, 30));
  store.decideConfirmation({ id: store.confirmationView().id, decision: "rejected" });
  const decision = await settling;
  assert.equal(decision.kind, "deny");
  assert.match(decision.reason, /工作台/);
  assert.match(decision.reason, /不是权限问题/);
});

test("超时按拒绝处理（fail-closed），绝不自动放行", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const outcome = await store.requestConfirmation({
    tool: "mcp__futu__trading_input_order", mode: "live", args: {}, session_id: "s1", ttlMs: 40,
  });
  assert.equal(outcome.decision, "rejected");
  assert.match(outcome.reason, /未确认/);
  assert.equal(store.confirmationView(), null, "超时后不应再挂着待确认项");
});

test("会话中断（abort）→ 拒绝，不悬挂", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const controller = new AbortController();
  const settling = store.requestConfirmation({
    tool: "mcp__futu__trading_input_order", mode: "live", args: {}, session_id: "s1",
    ttlMs: CONFIRM_TTL_MS, signal: controller.signal,
  });
  controller.abort();
  const outcome = await settling;
  assert.equal(outcome.decision, "rejected");
  assert.equal(store.confirmationView(), null);
});

test("同时只允许一笔待确认：第二笔立即按拒绝返回，不排队", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const first = store.requestConfirmation({
    tool: "mcp__futu__trading_input_order", mode: "live", args: { qty: 1 }, session_id: "s1" });
  await new Promise((r) => setTimeout(r, 10));
  const second = await store.requestConfirmation({
    tool: "mcp__futu__trading_cancel_order", mode: "live", args: { order_id: "1" }, session_id: "s1" });
  assert.equal(second.decision, "rejected");
  assert.match(second.reason, /已有一笔待确认/);
  // 第一笔仍然有效
  store.decideConfirmation({ id: store.confirmationView().id, decision: "approved" });
  assert.equal((await first).decision, "approved");
});

// ===================== 二、作答通道 =====================

test("只有正确的编号与结论能被接受；重复提交无效", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const settling = store.requestConfirmation({
    tool: "mcp__futu__trading_cancel_order", mode: "live", args: { order_id: "7137795" }, session_id: "s1" });
  const id = store.confirmationView().id;

  assert.throws(() => store.decideConfirmation({ id: "wrong", decision: "approved" }), WorkbenchError);
  assert.throws(() => store.decideConfirmation({ id, decision: "maybe" }), WorkbenchError);
  store.decideConfirmation({ id, decision: "approved" });
  assert.throws(() => store.decideConfirmation({ id, decision: "approved" }), WorkbenchError,
    "重复提交必须失败，避免双击把同一笔批两次");
  assert.equal((await settling).decision, "approved");
});

test("workbench RPC 是唯一作答通道，且只吃 id/decision", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const handle = createRpcHandler(store, { dir: path.join(dir, "cache") });
  const settling = store.requestConfirmation({
    tool: "mcp__futu__trading_input_order", mode: "live", args: { qty: 4 }, session_id: "s1" });
  const view = (await handle("confirmation", {})).value.pending;
  assert.ok(view, "confirmation 端点应能读到待确认项");

  // 载荷里不得出现任何下单参数——确认通道不能变成下单通道
  const bad = await handle("confirm-decide", { id: view.id, decision: "approved", price: 1 });
  assert.equal(bad.ok, false, "多余字段必须被拒");
  const wrong = await handle("confirm-decide", { id: "nope", decision: "approved" });
  assert.equal(wrong.ok, false);
  const ok = await handle("confirm-decide", { id: view.id, decision: "approved" });
  assert.equal(ok.ok, true);
  assert.equal((await settling).decision, "approved");
});

test("确认端点不进缓存：处理完的请求不能又被读出来", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const handle = createRpcHandler(store, { dir: path.join(dir, "cache") });
  const settling = store.requestConfirmation({
    tool: "mcp__futu__trading_input_order", mode: "live", args: {}, session_id: "s1" });
  await handle("confirmation", {});
  store.decideConfirmation({ id: store.confirmationView().id, decision: "approved" });
  await settling;
  const after = await handle("confirmation", {});
  assert.equal(after.cached, undefined, "confirmation 不应被缓存");
  assert.equal(after.value.pending, null, "已处理完的请求不应再出现");
});

// ===================== 三、策略链（A1/A2 的新形态）=====================

test("模拟盘写操作从不确认（模拟盘是沙箱）", async (t) => {
  const dir = home(t);
  const store = storeIn(dir, "sim");
  const { preExecute } = harness(store);
  const decision = await preExecute(execOf("mcp__futu__sim_trade_input_order", { qty: 1 }), ALLOW);
  assert.equal(decision.kind, "allow");
  assert.equal(store.confirmationView(), null, "sim 不应产生待确认");
});

test("只读账户查询从不确认", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const { preExecute } = harness(store);
  for (const name of ["mcp__futu__account_positions", "mcp__futu__account_cash_info"]) {
    const decision = await preExecute(execOf(name, {}), ALLOW);
    assert.equal(decision.kind, "allow", name);
  }
  assert.equal(store.confirmationView(), null);
});

test("下游守卫的 deny/ask 不被本插件改写", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const { preExecute } = harness(store);
  const denied = await preExecute(execOf(LIVE_ORDER.name, {}), async () => ({ kind: "deny", reason: "上游拒绝" }));
  assert.equal(denied.kind, "deny");
  assert.equal(denied.reason, "上游拒绝");
  const asked = await preExecute(execOf(LIVE_ORDER.name, {}), async () => ({ kind: "ask", reason: "别的插件要问" }));
  assert.equal(asked.kind, "ask", "别人的 ask 归别人，不该被我们吞掉");
  assert.equal(store.confirmationView(), null, "被上游拦下时不该产生待确认");
});

test("模式互斥仍然生效：sim 模式下拒绝实盘工具", async (t) => {
  const dir = home(t);
  const store = storeIn(dir, "sim");
  const { guards } = harness(store);
  const refusal = guards[0]({ name: LIVE_ORDER.name, arguments: {}, agent: { session: { id: "s1" } } });
  assert.match(refusal, /账户模式/);
});

// ===================== 四、摘要渲染 =====================

test("摘要：已知枚举给中文，未知值原样显示并提示核对", () => {
  const known = describeOrderArgs("mcp__futu__trading_input_order",
    { acc_id: "A1", market: 1, symbol: "00700", order_side: 2, qty: 100, price: 428.4 });
  const field = (label) => known.fields.find((f) => f.label === label).value;
  assert.equal(field("市场"), "1（港股）");
  assert.equal(field("方向"), "卖出（order_side=2）");
  assert.equal(field("数量"), "100");

  const unknown = describeOrderArgs("mcp__futu__trading_input_order", { market: 77, order_side: 9 });
  assert.match(unknown.fields.find((f) => f.label === "市场").value, /未识别.*请核对/);
  assert.match(unknown.fields.find((f) => f.label === "方向").value, /未识别.*请核对/);
});

test("操作类型按 <op>_order 后缀识别", () => {
  assert.equal(orderOperation("mcp__futu__trading_input_order"), "input");
  assert.equal(orderOperation("mcp__futu__trading_modify_order"), "modify");
  assert.equal(orderOperation("mcp__futu__trading_cancel_order"), "cancel");
  assert.equal(orderOperation("mcp__futu__trading_something_else"), null);
});

test("确认链路留痕：请求与裁决都进 activity", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  const settling = store.requestConfirmation({
    tool: "mcp__futu__trading_input_order", mode: "live",
    args: { symbol: "TSLL", qty: 4 }, session_id: "s1" });
  store.decideConfirmation({ id: store.confirmationView().id, decision: "approved" });
  await settling;
  const kinds = store.snapshot().activity.map((row) => row.kind);
  assert.ok(kinds.includes("confirmation_requested"));
  assert.ok(kinds.includes("confirmation_approved"));
  const approved = store.snapshot().activity.find((row) => row.kind === "confirmation_approved");
  assert.equal(approved.tool, "mcp__futu__trading_input_order");
  assert.equal(approved.session_id, "s1");
});

test("非实盘模式不得发起业务确认", async (t) => {
  const dir = home(t);
  const store = storeIn(dir);
  assert.throws(() => store.requestConfirmation({ tool: "x", mode: "sim", args: {} }), WorkbenchError);
});
