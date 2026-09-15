// 策略链审批回归（A1/A2，Harness 进程内 policy.js）；服务面回归（switch-mode/plan-execute/
// 工具面封闭/同源）已迁移 tests/test_wp6_service_approval.py + tests/test_wp6_mcp.py。
// 任何一条失败 → WP6 验收失败，禁止放宽断言。
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { WorkbenchStore } from "../plugins/workbench/src/store.js";
import { installTradingPolicy } from "../plugins/engine/src/policy.js";

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
