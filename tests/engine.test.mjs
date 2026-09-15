import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { WorkbenchStore } from "../plugins/workbench/src/store.js";
import { registerEngineTools } from "../plugins/engine/src/tools.js";
import { installTradingPolicy } from "../plugins/engine/src/policy.js";

async function setup(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "trading-engine-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  const store = new WorkbenchStore(home);
  const tools = new Map(), hooks = new Map();
  const ctx = { tradingWorkbench: store, tools: {
    register: tool => tools.set(tool.name, tool), guard: fn => hooks.set("guard", fn),
  }, on: (event, fn) => hooks.set(event, fn) };
  const exec = { agent: { session: { id: "session-1" } }, signal: new AbortController().signal };
  return { ctx, tools, hooks, store, exec };
}

test("research stays in Harness: start returns workflow and publish stores evidence", async (t) => {
  const { ctx, tools, store, exec } = await setup(t);
  registerEngineTools(ctx, value => value);
  const started = await tools.get("run_trading_analysis").execute({ ticker: "AAPL" }, exec);
  assert.equal(started.status, "running");
  assert.match(started.next_step, /Harness/);
  assert.equal(started.report, undefined);
  const result = await tools.get("research_publish").execute({
    run_id: started.id, ticker: "AAPL", rating: "Hold", report: "研究输出。",
    sources: [{ name: "quote", as_of: "2026-09-12", reference: "tool result" }],
  }, exec);
  assert.equal(result.rating, "Hold");
  assert.equal(store.snapshot().reports.length, 1);
  await assert.rejects(tools.get("run_trading_analysis").execute({ ticker: "AAPL" }, {}), /Session/);
});

test("model cannot enable live with an argument; quant preview is cached without executing an order", async (t) => {
  const { ctx, tools, store, exec } = await setup(t);
  const calls = [];
  registerEngineTools(ctx, value => value, async (script, args) => {
    calls.push({ script, args });
    return { ticker: "600519", signal: "BUY" };
  });
  await assert.rejects(tools.get("quant_switch").execute({ mode: "live", confirmed: true }, exec), /工作台/);
  await tools.get("quant_signal").execute({ ticker: "600519" }, exec);
  assert.deepEqual(calls[0].args, ["signal", "--ticker", "600519", "--strategy", "rsi"]);
  assert.equal(store.snapshot().previews[0].kind, "signal");
});

test("policy：futu 写类一律拒绝并指引工作台；读类照模式互斥；execute 租约与 result 观察保留", async (t) => {
  const { ctx, hooks, store, exec } = await setup(t);
  installTradingPolicy(ctx);
  const liveOrder = { ...exec, name: "mcp__futu__trading_input_order",
    arguments: { acc_id: "A1", market: 100, symbol: "AAPL", order_type: 1, order_side: 1, qty: 1 } };
  // WP7 收窄：写类不分模式一律拒绝（sim 模式下也拒，并指引工作台通道）
  assert.match(hooks.get("guard")(liveOrder), /请通过工作台交易/);
  assert.match(hooks.get("guard")({ ...exec, name: "mcp__futu__sim_trade_place_order" }), /请通过工作台交易/);
  assert.equal(hooks.get("guard")({ ...exec, name: "fin_news" }), undefined, "非 futu 工具不受影响");
  // 读类保留模式互斥
  assert.match(hooks.get("guard")({ ...exec, name: "mcp__futu__account_positions" }), /账户模式/);
  store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.match(hooks.get("guard")({ ...exec, name: "mcp__futu__sim_trade_position_list" }), /账户模式/);
  assert.match(hooks.get("guard")(liveOrder), /请通过工作台交易/, "live 下写类依旧拒绝");

  // 业务确认移至工作台服务侧（WP7）：pre-execute 不再发起确认，只透传下游结论
  const decision = await hooks.get("tools/pre-execute")(liveOrder, async () => ({ kind: "allow" }));
  assert.equal(decision.kind, "allow");
  assert.equal(store.confirmationView(), null, "不得产生任何待确认");
  const denied = await hooks.get("tools/pre-execute")(liveOrder, async () => ({ kind: "deny", reason: "other policy" }));
  assert.equal(denied.kind, "deny");

  // execute 租约与 result 观察保留（用读类账户工具驱动：写类已被 guard 拒，到不了 execute）
  const readExec = { ...exec, name: "mcp__futu__account_positions", arguments: {} };
  await hooks.get("tools/execute")(readExec, async () => {
    assert.equal(store.snapshot().in_flight, 1);
    return { isError: false, value: { positions: [] } };
  });
  assert.equal(store.snapshot().in_flight, 0);
  hooks.get("tools/result")(readExec, { isError: false, value: { positions: [] } });
  assert.equal(store.snapshot().broker.value.positions.length, 0);
});

test("real pinned defineTool accepts every schema and renders a published report", async (t) => {
  const { ctx, tools, exec } = await setup(t);
  const { apply } = await import("../plugins/engine/src/index.js");
  apply(ctx);
  // 工具清单：run_trading_analysis / research_publish / research_cancel /
  // trading_status / quant_signal / quant_backtest / quant_report / quant_switch
  assert.deepEqual([...tools.keys()].sort(), [
    "quant_backtest", "quant_report", "quant_signal", "quant_switch",
    "research_cancel", "research_publish", "run_trading_analysis", "trading_status",
  ], "工具清单变了：请确认是有意新增，并同步这里的断言");
  const run = await tools.get("run_trading_analysis").execute({ ticker: "AAPL" }, exec);
  const args = { run_id: run.id, ticker: "AAPL", rating: "Hold", report: "Report",
    sources: [{ name: "quote", as_of: "2026-09-12", reference: "Harness tool" }] };
  const report = await tools.get("research_publish").execute(args, exec);
  assert.equal(report.report, "Report");
});

test("通过对话取消进行中的投研记录：list / cancel / cancel_stale", async (t) => {
  const { ctx, tools, store, exec } = await setup(t);
  registerEngineTools(ctx, value => value);
  const cancel = tools.get("research_cancel");
  assert.ok(cancel, "缺少 research_cancel 工具（面板只读，必须由对话操作）");

  const started = await tools.get("run_trading_analysis").execute({ ticker: "AAPL" }, exec);

  // ① list
  const listed = await cancel.execute({ action: "list" }, exec);
  assert.equal(listed.running_count, 1);
  assert.equal(listed.running[0].id, started.id);

  // ② cancel 需要 run_id
  await assert.rejects(cancel.execute({ action: "cancel" }, exec), /run_id/);

  // ③ cancel 指定的一条：改状态但保留记录
  const done = await cancel.execute({ action: "cancel", run_id: started.id }, exec);
  assert.deepEqual(done.cancelled, [started.id]);
  assert.equal(done.status, "cancelled");
  assert.equal(store.read().runs.length, 1, "取消不是删除");
  assert.equal((await cancel.execute({ action: "list" }, exec)).running_count, 0);
  // 重复取消应明确报错，而不是静默成功
  await assert.rejects(cancel.execute({ action: "cancel", run_id: started.id }, exec), /already settled/);
});

test("cancel_stale 只动超时的，且阈值有校验", async (t) => {
  const { ctx, tools, store, exec } = await setup(t);
  registerEngineTools(ctx, value => value);
  const cancel = tools.get("research_cancel");
  const stale = await tools.get("run_trading_analysis").execute({ ticker: "AAPL" }, exec);
  const fresh = await tools.get("run_trading_analysis").execute({ ticker: "TSLA" }, exec);
  store.update((state) => {
    state.runs.find((row) => row.id === stale.id).started_at =
      new Date(Date.now() - 5 * 3600_000).toISOString();
  });

  const result = await cancel.execute({ action: "cancel_stale", older_than_minutes: 120 }, exec);
  assert.equal(result.cancelled_count, 1);
  assert.deepEqual(result.cancelled, [stale.id]);
  const byId = Object.fromEntries(store.read().runs.map((r) => [r.id, r.status]));
  assert.equal(byId[fresh.id], "running", "新鲜的不应被动");

  for (const bad of [0, -5, 999999, 1.5, "120"]) {
    await assert.rejects(cancel.execute({ action: "cancel_stale", older_than_minutes: bad }, exec),
      /1\.\.10080/, `阈值 ${bad} 应被拒绝`);
  }
});

test("research_cancel 不影响已发布的研报", async (t) => {
  const { ctx, tools, store, exec } = await setup(t);
  registerEngineTools(ctx, value => value);
  const started = await tools.get("run_trading_analysis").execute({ ticker: "AAPL" }, exec);
  await tools.get("research_publish").execute({
    run_id: started.id, ticker: "AAPL", rating: "Hold", report: "已发布。",
    sources: [{ name: "s", as_of: "2026-09-13", reference: "r" }],
  }, exec);
  // 已结算的 run 不能被取消
  await assert.rejects(tools.get("research_cancel").execute({ action: "cancel", run_id: started.id }, ctx),
    /already settled/);
  assert.equal(store.read().reports.length, 1);
});

test("signal output carries a strategy_label with its parameters", async (t) => {
  const { ctx, tools, exec } = await setup(t);
  registerEngineTools(ctx, value => value);
  const result = await tools.get("quant_signal").execute({ ticker: "600519", strategy: "rsi" }, exec);
  if (result.error) return; // 离线环境无数据
  // 裸名 "rsi" 无法区分参数，界面上必须能看出用了哪套参数
  assert.match(result.strategy_label, /^rsi\(\d+,\d+\)$/);
  assert.equal(result.ticker, "600519");
});
