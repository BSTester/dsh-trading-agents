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

const execOf = (name, args = {}) => ({ name, arguments: args,
  agent: { session: { id: "s1" } } });

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

// R2 已按 2026-09-15 修订（规格 §5.1 A2）：实盘写操作走**业务确认**，
// 不返回 ask —— ask 在 full-access（policy="never"）下会被 approval.decide()
// 直接 rejected，表现为"用户拒绝了"而实际没人被问过。
test("R2 live 写操作走业务确认而非原生审批；sim 写不确认", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  const { ctx, hooks } = fakeHarness(store);
  installTradingPolicy(ctx);
  const next = async () => ({ kind: "allow" });
  const settling = hooks["tools/pre-execute"][0](
    execOf("mcp__futu__trading_modify_order", { acc_id: "A1", market: 1, order_id: "1", qty: 100 }), next);
  await new Promise((r) => setTimeout(r, 20));
  const pending = store.confirmationView();
  assert.ok(pending, "实盘写操作必须产生待确认");
  assert.equal(pending.operation, "改单");
  store.decideConfirmation({ id: pending.id, decision: "approved" });
  const decision = await settling;
  assert.notEqual(decision.kind, "ask", "不能返回 ask：那会受会话审批档位影响");
  assert.equal(decision.kind, "allow");
  const sim = await hooks["tools/pre-execute"][0](execOf("mcp__futu__sim_trade_place_order"), next);
  assert.equal(sim.kind, "allow");
  assert.equal(store.confirmationView(), null, "sim 写操作不应产生待确认");
});
