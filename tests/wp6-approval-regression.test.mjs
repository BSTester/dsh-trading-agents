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

// R2 已按 2026-09-16 WP7 收窄再修订（规格 §5.1 A2 再修订）：富途写类在 Harness 内
// **不可达**——guard 不分模式一律拒绝并指引工作台通道（quantwb 的 trade_* 工具，
// 或计划执行）；业务确认移至工作台服务侧（Web 确认卡片作答），Node 侧不再发起。
// 历史：R2 曾断言「live 写走业务确认而非 ask」（full-access 下 ask 被静默 rejected 的
// 教训）——该 pre-execute 确认分支已退役，store 三方法与端点保留（legacy）。
test("R2 live 下 futu 写类 guard 拒绝含指引；不产生待确认；mcp__futu__* 只读放行", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  const { ctx, guards, hooks } = fakeHarness(store);
  installTradingPolicy(ctx);
  // 写类 → guard 拒绝，文案含工作台通道指引
  const refusal = String(guards[0](execOf("mcp__futu__trading_modify_order",
    { acc_id: "A1", market: 1, order_id: "1", qty: 100 })));
  assert.match(refusal, /请通过工作台交易/);
  assert.match(refusal, /trade_\*/);
  assert.match(refusal, /计划执行/);
  assert.match(String(guards[0](execOf("mcp__futu__sim_trade_place_order", {}))), /请通过工作台交易/);
  // 不产生任何待确认
  assert.equal(store.confirmationView(), null, "写类被拒不得产生待确认");
  // pre-execute 只透传（不再发起业务确认；写类在 guard 已拒，正常到不了这里）
  const decision = await hooks["tools/pre-execute"][0](
    execOf("mcp__futu__trading_modify_order", {}), async () => ({ kind: "allow" }));
  assert.equal(decision.kind, "allow");
  assert.equal(store.confirmationView(), null);
  // mcp__futu__* 只读放行
  assert.equal(guards[0](execOf("mcp__futu__account_positions")), undefined);
  assert.equal(guards[0](execOf("mcp__futu__quote_news_search")), undefined);
});
