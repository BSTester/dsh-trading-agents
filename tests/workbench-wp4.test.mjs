import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { ENDPOINTS, ENDPOINT_SHAPE, matchesShape } from "../plugins/workbench/src/endpoints.js";
import { createRpcHandler } from "../plugins/workbench/src/rpc.js";
import { writeCommand, COMMANDS } from "../plugins/workbench/src/commandbus.js";
import { createTtlCache } from "../plugins/workbench/src/cache.js";
import { mkdtempSync, rmSync } from "node:fs";

function handler(t, store, deps) {
  // 缓存端点（plan/schedule/reconcile）写磁盘：一律注入一次性目录（全局护栏）
  return createRpcHandler(store, { cache: isolatedCache(t), ...deps });
}

// 磁盘缓存跨进程共享：凡触到缓存端点（plan/schedule/reconcile）的用例必须
// 注入一次性目录，否则会读到其他进程留下的条目（workbench.test.mjs 同款守卫）。
function isolatedCache(t) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "wp4-cache-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return createTtlCache({ dir });
}

function fakeStore(mode = "sim") {
  return { snapshot: () => ({ mode, generated_at: "2026-09-14T10:00:00Z", in_flight: 0 }) };
}

async function tmpHome(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp4-commands-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  return home;
}

test("wp4 endpoints registered with shape", () => {
  for (const e of ["plan", "plan-execute", "schedule", "reconcile"]) {
    assert(ENDPOINTS.includes(e), e);
  }
  assert(matchesShape("plan", { plans: [], alerts: [] }));
  assert(matchesShape("schedule", { heartbeat: {}, jobs: [] }));
  assert(matchesShape("reconcile", { diffs: [], tca: {} }));
  // plan-execute 是动作端点，不进缓存形状表
  assert(!(  "plan-execute" in ENDPOINT_SHAPE));
  assert.equal(matchesShape("plan", { plans: [] }), false);
});

test("plan-execute queues a whitelisted command file (sim, no confirmation needed)", async (t) => {
  const home = await tmpHome(t);
  const calls = [];
  const deps = {
    commandBus: { writeCommand: (...args) => calls.push(args) },
  };
  const handle = handler(t, fakeStore("sim"), deps);
  const result = await handle("plan-execute", { plan_hash: "h1", expected_mode: "sim" });
  assert.equal(result.ok, true);
  assert.equal(result.value.queued, true);
  assert.ok(result.value.nonce);
  assert.equal(calls[0][1], "execute_plan");
  assert.deepEqual(calls[0][2], { plan_hash: "h1", expected_mode: "sim" });
});

test("plan-execute in live requires exact confirmation phrase and matching mode", async (t) => {
  const home = await tmpHome(t);
  const calls = [];
  const deps = { commandBus: { writeCommand: (...args) => calls.push(args) } };
  const handle = handler(t, fakeStore("live"), deps);
  const refused = await handle("plan-execute", { plan_hash: "h1", expected_mode: "live", confirmation: "确认" });
  assert.equal(refused.ok, false);
  assert.equal(calls.length, 0);
  const ok = await handle("plan-execute", { plan_hash: "h1", expected_mode: "live", confirmation: "确认执行" });
  assert.equal(ok.value.queued, true);
  const mismatch = await handler(t, fakeStore("sim"), deps)("plan-execute",
    { plan_hash: "h1", expected_mode: "live", confirmation: "确认执行" });
  assert.equal(mismatch.ok, false);
  assert.match(mismatch.error.message, /模式/);
});

test("plan-execute rejects unknown fields and unknown actions", async (t) => {
  const deps = { commandBus: { writeCommand: () => {} } };
  const handle = handler(t, fakeStore("sim"), deps);
  const extra = await handle("plan-execute", { plan_hash: "h1", expected_mode: "sim", extra: 1 });
  assert.equal(extra.ok, false);
  assert.match(extra.error.message, /field/);
  const badAction = await handle("plan-execute", { plan_hash: "h1", expected_mode: "sim", action: "格式化" });
  assert.equal(badAction.ok, false);
  assert.match(badAction.error.message, /action/);
  const noHash = await handle("plan-execute", { expected_mode: "sim" });
  assert.equal(noHash.ok, false);
  assert.match(noHash.error.message, /plan_hash/);
});

test("plan-execute kill/unkill/cancel map to whitelist types", async (t) => {
  const calls = [];
  const deps = { commandBus: { writeCommand: (...args) => calls.push(args) } };
  const handle = handler(t, fakeStore("sim"), deps);
  for (const [action, type] of [["kill", "kill"], ["unkill", "unkill"], ["cancel", "cancel_plan"]]) {
    const result = await handle("plan-execute", { expected_mode: "sim", action });
    assert.equal(result.value.queued, true);
    assert.equal(calls.at(-1)[1], type);
  }
});

test("plan/schedule/reconcile read via corebridge; plan merges account mode", async (t) => {
  const deps = {
    cache: isolatedCache(t),
    corebridge: {
      plan: async () => ({ plans: [{ plan_id: "P1", content_hash: "h1" }], alerts: [] }),
      schedule: async () => ({ heartbeat: { heartbeat: "t" }, jobs: [] }),
      reconcile: async () => ({ diffs: [], tca: {}, chain: [] }),
    },
  };
  const handle = handler(t, fakeStore("live"), deps);
  const plan = await handle("plan", {});
  assert.equal(plan.value.mode, "live");
  assert.equal(plan.value.plans[0].plan_id, "P1");
  const schedule = await handle("schedule", {});
  assert.equal(schedule.value.heartbeat.heartbeat, "t");
  const reconcile = await handle("reconcile", {});
  assert.deepEqual(reconcile.value.diffs, []);
});

test("plan read failure surfaces as structured error, not a throw", async (t) => {
  const deps = { cache: isolatedCache(t),
    corebridge: { plan: async () => { throw new Error("core 不可用"); } } };
  const handle = handler(t, fakeStore("sim"), deps);
  const result = await handle("plan", {});
  assert.equal(result.ok, false);
  assert.match(result.error.message, /core 不可用/);
});

test("commandbus mirrors the python whitelist and writes pending atomically", async (t) => {
  const home = await tmpHome(t);
  await assert.rejects(writeCommand(home, "rm_rf", {}), /白名单/);
  const nonce = await writeCommand(home, "execute_plan", { plan_hash: "h1" });
  assert.ok(/^[0-9a-f]{32}$/.test(nonce));
  const pending = await readdir(path.join(home, "trading-commands", "pending"));
  assert.deepEqual(pending, [`${nonce}.json`]);
  const body = JSON.parse(await readFile(path.join(home, "trading-commands", "pending", `${nonce}.json`), "utf8"));
  assert.equal(body.type, "execute_plan");
  assert.equal(body.plan_hash, "h1");
  assert.equal(Object.keys(COMMANDS).length, 5);
});

// ── 任务 6/7/8：计划与调度页签、审计链路 ─────────────────────────────────────

function tabSource(source, name, nextName) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf(`function ${nextName}(`);
  const slice = source.slice(start, end > start ? end : undefined);
  // 文案规范只约束页面渲染字符串；设计意图写在注释里是规范要求的行为，
  // 因此检查前先剥掉行注释。
  return slice.replace(/^\s*\/\/.*$/gm, "");
}

test("client ships plan tab wired into nav and dispatch", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  assert.match(source, /\{ id: "plan", label: "计划" \},/);
  assert.match(source, /function PlanTab\(/);
  assert.match(source, /tab === "plan" && h\(PlanTab/);
  assert.match(source, /request\(rpc, "plan"/);
});

test("client ships schedule tab wired into nav and dispatch", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  assert.match(source, /\{ id: "schedule", label: "调度" \},/);
  assert.match(source, /function ScheduleTab\(/);
  assert.match(source, /tab === "schedule" && h\(ScheduleTab/);
  assert.match(source, /useEndpoint\(rpc, "schedule"/);
});

test("schedule tab shows heartbeat freshness, alerts, kill switch without design copy", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const tab = tabSource(source, "ScheduleTab", "Dashboard");
  assert.match(tab, /5 \* 60_000/, "心跳 5 分钟阈值标红（数据事实提示）");
  assert.match(tab, /act\("kill"/, "激活 kill switch 走受约束动作");
  assert.match(tab, /act\("unkill"/, "解除走受约束动作");
  assert.match(tab, /request\(rpc, "plan-execute", \{ action \}/, "动作统一经 plan-execute");
  assert.match(tab, /alerts/, "告警列表来自 reconcile 端点");
  assert.match(tab, /halt/, "熔断状态行");
  // 页签文案规范（全局约定 2.1）
  assert.doesNotMatch(tab, /规格|设计稿|示例|宁可|窄门|token/);
});

test("audit view renders three-level plan→order→fill chain from reconcile data", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const chain = tabSource(source, "AuditChainCard", "AuditView");
  assert.match(source, /function AuditChainCard\(/);
  assert.match(source, /h\(AuditChainCard, \{ rpc, revision/);
  assert.match(chain, /reconcile/, "链路数据来自 reconcile 端点");
  assert.match(chain, /chain/, "计划→订单→成交 三级");
  assert.match(chain, /fills/, "成交层");
  // 计划要求的保留项：功能性说明文案不被清理
  assert.match(source, /原始券商响应/);
  assert.doesNotMatch(chain, /规格|设计稿|示例|宁可|窄门|token/);
});

test("plan tab keeps the live gate: exact phrase, frozen-only execute, cancel action", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const tab = tabSource(source, "PlanTab", "ScheduleTab");
  assert.match(tab, /=== "frozen"/, "只有冻结计划可执行");
  assert.match(tab, /=== "live"/, "实盘门槛判断");
  assert.match(tab, /"确认执行"/, "实盘口令必须逐字匹配");
  assert.match(tab, /action: "cancel"/, "取消计划走受约束动作");
  assert.match(tab, /content_hash/, "执行必须带计划哈希");
  assert.match(tab, /expected_mode/, "执行必须带期望模式");
  // 页签文案规范（全局约定 2.1）：正文只保留数据事实/操作反馈/安全口令
  assert.doesNotMatch(tab, /规格|设计稿|示例|宁可|窄门|token/);
});
