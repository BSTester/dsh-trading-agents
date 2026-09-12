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

test("policy rejects opposite account, requires Harness approval for live writes, records results", async (t) => {
  const { ctx, hooks, store, exec } = await setup(t);
  installTradingPolicy(ctx);
  const live = { ...exec, name: "mcp__futu__trading_order_place", arguments: { ticker: "AAPL", quantity: 1 } };
  assert.match(hooks.get("guard")(live), /sim/);
  assert.equal(hooks.get("guard")({ ...exec, name: "fin_news" }), undefined);
  store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.match(hooks.get("guard")({ ...exec, name: "mcp__futu__sim_trade_input_order" }), /live/);
  const decision = await hooks.get("tools/pre-execute")(live, async () => ({ kind: "allow" }));
  assert.equal(decision.kind, "ask");
  assert.match(decision.reason, /AAPL/);
  const denied = await hooks.get("tools/pre-execute")(live, async () => ({ kind: "deny", reason: "other policy" }));
  assert.equal(denied.kind, "deny");
  await hooks.get("tools/execute")(live, async () => {
    assert.equal(store.snapshot().in_flight, 1);
    return { isError: false, value: { status: "SUBMITTED" } };
  });
  assert.equal(store.snapshot().in_flight, 0);
  hooks.get("tools/result")(live, { isError: false, value: { status: "SUBMITTED" } });
  assert.equal(store.snapshot().broker.value.status, "SUBMITTED");
});

test("real pinned defineTool accepts every schema and renders a published report", async (t) => {
  const { ctx, tools, exec } = await setup(t);
  const { apply } = await import("../plugins/engine/src/index.js");
  apply(ctx);
  assert.equal(tools.size, 7);
  const run = await tools.get("run_trading_analysis").execute({ ticker: "AAPL" }, exec);
  const args = { run_id: run.id, ticker: "AAPL", rating: "Hold", report: "Report",
    sources: [{ name: "quote", as_of: "2026-09-12", reference: "Harness tool" }] };
  const report = await tools.get("research_publish").execute(args, exec);
  assert.equal(report.report, "Report");
});
