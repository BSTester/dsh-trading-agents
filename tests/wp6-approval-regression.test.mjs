// WP6 审批回归（规格 §5.2 R 系列，全部离线）：A1/A2 策略链、A3 双保险+通道分级、
// A4 执行窄门、A7 面封闭。任何一条失败 → WP6 验收失败，禁止放宽断言。
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { WorkbenchStore } from "../plugins/workbench/src/store.js";
import { createRpcHandler } from "../plugins/workbench/src/rpc.js";
import { installTradingPolicy } from "../plugins/engine/src/policy.js";
import { ENDPOINTS } from "../plugins/workbench/src/endpoints.js";
import { ENDPOINT_TOOLS, ADMIN_TOOLS, TOOL_COUNT, TOOL_NAME_BLACKLIST, buildManifest } from "../platform/server/manifest.mjs";

async function inTempHome(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-approval-"));
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
  return { ctx, guards, hooks };
}

const execOf = (name) => ({ name, arguments: {}, agent: { session: { id: "s1" } } });

test("R1 模式互斥：sim 拒 live 类工具；live 拒 sim 类工具", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  const { ctx, guards } = fakeHarness(store);
  installTradingPolicy(ctx);
  assert.equal(guards.length, 1);
  assert.match(String(guards[0](execOf("mcp__futu__account_positions"))), /账户模式/);
  assert.equal(guards[0](execOf("mcp__futu__sim_trade_position_list")), undefined);
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.match(String(guards[0](execOf("mcp__futu__sim_trade_position_list"))), /账户模式/);
  assert.equal(guards[0](execOf("mcp__futu__account_positions")), undefined);
});

test("R2 live 写操作强制原生审批（ask）；sim 写不 ask", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  const { ctx, hooks } = fakeHarness(store);
  installTradingPolicy(ctx);
  const next = async () => ({ kind: "allow" });
  const ask = await hooks["tools/pre-execute"][0](execOf("mcp__futu__trading_modify_order"), next);
  assert.equal(ask.kind, "ask");
  assert.match(ask.reason, /真实账户操作/);
  const sim = await hooks["tools/pre-execute"][0](execOf("mcp__futu__sim_trade_place_order"), next);
  assert.equal(sim.kind, "allow");
});

test("R3 switch_mode：口令/expected_mode/租约/MCP 通道分级", async (t) => {
  const home = await inTempHome(t);
  const store = new WorkbenchStore();
  // 全局护栏（tests/cache.test.mjs）：createRpcHandler 必须显式传一次性缓存目录
  const handle = createRpcHandler(store, { dir: path.join(home, "trading-workbench-cache") });
  let result = await handle("switch-mode", { mode: "live", expected_mode: "sim" });
  assert.equal(result.ok, false);
  assert.match(result.error.message, /确认实盘/);
  result = await handle("switch-mode", { mode: "live", expected_mode: "sim", confirmation: "确认" });
  assert.equal(result.ok, false);
  result = await handle("switch-mode", { mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.equal(result.ok, true);
  assert.equal(result.value.order_authorized, false);
  result = await handle("switch-mode", { mode: "sim", expected_mode: "sim" });   // 过期期望
  assert.equal(result.ok, false);
  const release = store.enterBrokerCall("live");                                  // 在途租约
  result = await handle("switch-mode", { mode: "sim", expected_mode: "live" });
  assert.equal(result.ok, false);
  assert.match(result.error.message, /账户调用/);
  release();
  const tool = buildManifest({ handle, store }).find((row) => row.name === "switch_mode");
  const viaMcp = await tool.call({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.equal(viaMcp.ok, false);
  assert.equal(viaMcp.error.code, "trading/live-switch-web-only");
  const backToSim = await tool.call({ mode: "sim", expected_mode: "live" });
  assert.equal(backToSim.ok, true);
});

test("R4 plan_execute：live 口令、queued nonce、指令文件不含口令、kill/unkill 映射", async (t) => {
  const home = await inTempHome(t);
  const store = new WorkbenchStore();
  const handle = createRpcHandler(store, { dir: path.join(home, "trading-workbench-cache") });
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  let result = await handle("plan-execute", { plan_hash: "h1", expected_mode: "live" });
  assert.equal(result.ok, false);
  assert.match(result.error.message, /确认执行/);
  result = await handle("plan-execute", { plan_hash: "h1", expected_mode: "live", confirmation: "确认执行" });
  assert.equal(result.ok, true);
  assert.equal(result.value.queued, true);
  assert.ok(result.value.nonce);

  const dir = path.join(home, "trading-commands", "pending");
  const bodies = async () => Promise.all((await readdir(dir)).filter((f) => f.endsWith(".json"))
    .map(async (f) => JSON.parse(await readFile(path.join(dir, f), "utf8"))));
  let all = await bodies();
  assert.ok(all.some((b) => b.type === "execute_plan" && b.plan_hash === "h1" && b.expected_mode === "live"));
  assert.ok(all.every((b) => !("confirmation" in b)));   // 口令只在服务端校验，绝不落指令文件

  await handle("plan-execute", { action: "kill" });
  await handle("plan-execute", { action: "unkill" });
  all = await bodies();
  assert.ok(all.some((b) => b.type === "kill"));
  assert.ok(all.some((b) => b.type === "unkill"));
  result = await handle("plan-execute", { action: "sell_all" });
  assert.equal(result.ok, false);                        // 白名单外动作
  result = await handle("plan-execute", { plan_hash: "h1", expected_mode: "sim" });  // 模式已变化
  assert.equal(result.ok, false);
});

test("R5 工具面封闭：恰 25、端点对等、无黑名单能力", () => {
  assert.equal(TOOL_COUNT, 25);
  assert.deepEqual(ENDPOINT_TOOLS.map((row) => row.endpoint).sort(), [...ENDPOINTS].sort());
  const names = [...ENDPOINT_TOOLS, ...ADMIN_TOOLS].map((row) => row.name);
  for (const banned of TOOL_NAME_BLACKLIST) {
    // 分段精确匹配（与 platform/tests/wp6-locks.test.mjs 一致）：不用裸子串 includes，
    // 否则 plan_execute 中的 "exec"（execute 的前缀）会构成子串误伤。
    assert.ok(!names.some((raw) => {
      const lower = String(raw).toLowerCase();
      return lower === banned || lower.split(/[^a-z0-9]+/).includes(banned);
    }), banned);
  }
});
