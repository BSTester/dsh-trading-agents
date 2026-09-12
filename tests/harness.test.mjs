import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import * as engine from "../plugins/engine/src/index.js";
import * as workbench from "../plugins/workbench/src/index.js";

const require = createRequire(new URL("../plugins/engine/src/index.js", import.meta.url));
const load = name => import(pathToFileURL(require.resolve(name)));
const { Context, Service } = await load("@deepseek-ai/cordis");
const { ToolRuntime, defineTool } = await load("@deepseek-ai/dsh-tools");
const { SystemPrompt } = await load("@deepseek-ai/dsh-system-prompt");

test("real Cordis composes root workbench, tool service, policy, and observed broker responses", async (t) => {
  const home = await mkdtemp(path.join(os.tmpdir(), "trading-harness-"));
  const previous = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  const ctx = new Context();
  const fibers = [];
  t.after(async () => {
    try {
      for (const fiber of fibers.reverse()) await fiber.dispose();
    } finally {
      if (previous === undefined) delete process.env.DSH_HOME;
      else process.env.DSH_HOME = previous;
      await rm(home, { recursive: true, force: true });
    }
  });
  const routes = new Map();
  class Connection extends Service {
    constructor(owner) { super(owner, "connection"); }
    get fetch() {
      const owner = this.ctx;
      return { register: route => this.register(owner, route) };
    }
    register(owner, route) {
      return owner.effect(() => {
        routes.set(route.path, route);
        return () => routes.delete(route.path);
      });
    }
  }
  for (const plugin of [Connection]) {
    const fiber = ctx.plugin(plugin);
    fibers.push(fiber);
    await fiber;
  }
  const prompt = ctx.plugin(SystemPrompt);
  fibers.push(prompt);
  await prompt;
  const runtime = ctx.plugin(ToolRuntime);
  fibers.push(runtime);
  await runtime;
  const host = ctx.plugin(workbench);
  fibers.push(host);
  await host;
  await new Promise(resolve => setImmediate(resolve));
  const tools = ctx.plugin(engine);
  fibers.push(tools);
  await tools;
  ctx.tools.register(defineTool({
    name: "mcp__futu__sim_trade_history_order_list", description: "Fixture broker observation",
    parameters: {}, output: { schema: { type: "object", additionalProperties: true },
      render: (_a, v) => [{ type: "text", text: JSON.stringify(v) }] },
    async execute() {
      assert.equal(ctx.tradingWorkbench.inFlight, 1);
      return { orders: [{ order_id: "sim-1", status: "SUBMITTED" }] };
    },
  }));
  const result = await ctx.tools.execute({
    name: "mcp__futu__sim_trade_history_order_list", arguments: {}, callId: "fixture-1",
    signal: new AbortController().signal,
  });
  assert.equal(result.isError, false, JSON.stringify(result));
  const response = await routes.get("/api/trading-workbench/snapshot").fetch(new Request(
    "http://localhost/api/trading-workbench/snapshot", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type: "client-request", rpcId: "snapshot-1", method: "trading-workbench/snapshot", payload: {} }),
    }));
  const { result: snapshot } = await response.json();
  assert.equal(snapshot.value.in_flight, 0);
  assert.equal(snapshot.value.broker.value.orders[0].status, "SUBMITTED");
  ctx.tradingWorkbench.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  const denied = await ctx.tools.execute({
    name: "mcp__futu__sim_trade_history_order_list", arguments: {}, callId: "fixture-2",
    signal: new AbortController().signal,
  });
  assert.equal(denied.isError, true);
  let placed = false;
  ctx.tools.register(defineTool({
    name: "mcp__futu__trading_order_place", description: "Fixture only; never a real broker",
    parameters: {}, output: { schema: { type: "object", additionalProperties: true },
      render: () => [{ type: "text", text: "fixture" }] },
    async execute() { placed = true; return {}; },
  }));
  const unapproved = await ctx.tools.execute({
    name: "mcp__futu__trading_order_place", arguments: {}, callId: "fixture-3",
    signal: new AbortController().signal,
  });
  assert.equal(unapproved.isError, true);
  assert.equal(placed, false);
  assert.equal(ctx.tradingWorkbench.inFlight, 0);
  await host.dispose();
  assert.equal(routes.size, 0);
});
