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
const { Context } = await load("@deepseek-ai/cordis");
const { ToolRuntime, defineTool } = await load("@deepseek-ai/dsh-tools");
const { SystemPrompt } = await load("@deepseek-ai/dsh-system-prompt");

// WP7 面板退役：本测试原经 Connection RPC（/api/trading-workbench/snapshot）读 Host 数据，
// 面板与 Host RPC 面删除后改为直调 `ctx.tradingWorkbench`（服务锚不变式）：
//   1. 真实 Cordis 组合出 workbench 服务锚 + engine 工具 + policy 三层；
//   2. 账户工具的 execute 租约与 result 观察记录进 store；
//   3. 模式互斥拒绝跨模式账户调用；futu 写类 guard 一律拒绝并指引工作台。
test("real Cordis composes workbench service anchor, engine tools, policy, and observed broker responses", async (t) => {
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
  // 服务锚就位：面板退役后 workbench 插件只 provide 服务，不再注册任何路由
  assert.ok(ctx.tradingWorkbench, "tradingWorkbench 服务必须可用");
  assert.equal(typeof ctx.tradingWorkbench.snapshot, "function");
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
  // 观察链路：最终响应进 store（原经 RPC 快照断言，现直读服务锚）
  const snapshot = ctx.tradingWorkbench.snapshot();
  assert.equal(snapshot.in_flight, 0);
  assert.equal(snapshot.broker.value.orders[0].status, "SUBMITTED");
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
});
