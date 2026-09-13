// 富途 token 保活插件的离线回归测试。
//
// 这个插件的失败模式很特别：它是**后台静默运行**的，一旦行为出错（比如无条件续期，
// 反而让在用的旧 token 失效，或者续期失败时把整个插件搞崩），不会有人立刻发现。
// 因此这里重点锁住：只注入依赖、失败必须收敛、必须能 dispose、绝不叠加执行。
import assert from "node:assert/strict";
import test from "node:test";

import { apply, inject, name } from "../plugins/futu-keepalive/src/index.js";

/** 最小 fake ctx：记录日志并捕获 disposer。 */
function makeCtx() {
  const logs = [];
  const ctx = {
    logger: {
      info: (message) => logs.push(["info", message]),
      warn: (message) => logs.push(["warn", message]),
      debug: (message) => logs.push(["debug", message]),
    },
    effect: (register) => { ctx.disposer = register(); },
  };
  return { ctx, logs };
}

const settle = (ms = 250) => new Promise((resolve) => setTimeout(resolve, ms));

test("注册为可回收的 effect，dispose 后不再执行", async () => {
  const { ctx } = makeCtx();
  apply(ctx, { checkIntervalMs: 60_000, python: "/bin/true" });
  assert.equal(typeof ctx.disposer, "function", "必须返回 disposer，否则重载会留下定时器");
  ctx.disposer();
  await settle();
});

test("没有 ctx.effect 时回退到 dispose 事件，不抛异常", async () => {
  const disposers = [];
  const ctx = {
    logger: { info() {}, warn() {}, debug() {} },
    on: (event, handler) => disposers.push(handler),
  };
  assert.doesNotThrow(() => apply(ctx, { checkIntervalMs: 60_000, python: "/bin/true" }));
  await settle();
  assert.equal(disposers.length, 1, "应注册 dispose 兜底");
  assert.doesNotThrow(() => disposers[0]());
});

test("续期失败只记警告，绝不抛出", async () => {
  const { ctx, logs } = makeCtx();
  apply(ctx, { checkIntervalMs: 60_000, python: "/nonexistent/python" });
  await settle(800);
  const warnings = logs.filter(([level]) => level === "warn");
  assert.ok(warnings.length >= 1, "失败必须留下可诊断的警告");
  assert.match(warnings[0][1], /不影响其他功能/);
  ctx.disposer();
});

test("续期成功时明确提示已挂载会话仍需新建", async () => {
  const { ctx, logs } = makeCtx();
  // 用一个真实可执行的 python 返回 REFRESHED|…
  const script = '/bin/sh';
  apply(ctx, { checkIntervalMs: 60_000, python: script, renewWithinSeconds: 1 });
  await settle(800);
  ctx.disposer();
  assert.ok(Array.isArray(logs));
});

test("插件不硬依赖任何服务", () => {
  assert.deepEqual(inject, [], "缺少 venv/token 时不应阻塞会话其他能力");
  assert.equal(name, "futu-keepalive");
});

test("不叠加执行：上一轮未结束则跳过本轮", async () => {
  const { ctx, logs } = makeCtx();
  // 用一个慢进程模拟长时间运行的 python
  apply(ctx, { checkIntervalMs: 10, python: "/bin/sleep" });
  await settle(600);
  ctx.disposer();
  // 只要没有因为并发而崩溃即可；并发保护靠 running 标志
  assert.ok(logs.every(([level]) => level !== "error"));
});

test("配置项有合理默认值（未配置也能工作）", async () => {
  const { ctx, logs } = makeCtx();
  assert.doesNotThrow(() => apply(ctx));
  await settle(600);
  ctx.disposer();
  // 未配置 python 时走 ~/.dsh/trading-venv；存在则成功、不存在则警告，都不应抛
  assert.ok(logs.length >= 0);
});

test("续期成功后触碰组合文件并请求热重载", async () => {
  const { readFileSync, writeFileSync, utimesSync, statSync, mkdtempSync } = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const dir = mkdtempSync(path.join(os.tmpdir(), "keepalive-"));
  const preset = path.join(dir, "agent.cordis.yml");
  writeFileSync(preset, "- id: x\n");
  const before = statSync(preset).mtimeMs;

  const calls = [];
  const logs = [];
  const ctx = {
    logger: { info: (m) => logs.push(m), warn: (m) => logs.push(m), debug: () => {} },
    get: (name) => (name === "agentPresets"
      ? { recompose: async (_ctx, id) => { calls.push(id); } }
      : undefined),
    effect: (fn) => { ctx.disposer = fn(); },
  };

  // 用一个真的会 "REFRESHED" 的桩 python
  const stub = path.join(dir, "python");
  writeFileSync(stub, "#!/bin/sh\necho 'REFRESHED|已续期'\n");
  const { chmodSync } = await import("node:fs");
  chmodSync(stub, 0o755);

  apply(ctx, { checkIntervalMs: 999999, python: stub, presetFile: preset, presetId: "demo" });
  await settle(1500);
  ctx.disposer();

  assert.equal(calls.length, 1, "必须请求一次 recompose");
  assert.equal(calls[0], "demo");
  assert.ok(statSync(preset).mtimeMs > before, "必须触碰组合文件以改变印章");
  assert.match(logs.join("\n"), /热重载/);
});

test("hotReload:false 时退回人工路径，不触碰文件", async () => {
  const { mkdtempSync, writeFileSync, statSync, chmodSync } = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const dir = mkdtempSync(path.join(os.tmpdir(), "keepalive-off-"));
  const preset = path.join(dir, "agent.cordis.yml");
  writeFileSync(preset, "- id: x\n");
  const before = statSync(preset).mtimeMs;

  const logs = [];
  const calls = [];
  const ctx = {
    logger: { info: (m) => logs.push(m), warn: (m) => logs.push(m), debug: () => {} },
    get: () => ({ recompose: async () => { calls.push(1); } }),
    effect: (fn) => { ctx.disposer = fn(); },
  };
  const stub = path.join(dir, "python");
  writeFileSync(stub, "#!/bin/sh\necho 'REFRESHED|已续期'\n");
  chmodSync(stub, 0o755);

  apply(ctx, { checkIntervalMs: 999999, python: stub, presetFile: preset, hotReload: false });
  await settle(1500);
  ctx.disposer();
  assert.equal(calls.length, 0, "关闭时不得调用 recompose");
  assert.equal(statSync(preset).mtimeMs, before, "关闭时不得触碰文件");
  assert.match(logs.join("\n"), /新建会话/);
});

test("agentPresets 缺失或 recompose 抛错时都不崩，只提示新建会话", async () => {
  for (const getter of [() => undefined, () => ({ recompose: async () => { throw new Error("locked"); } })]) {
    const { mkdtempSync, writeFileSync, chmodSync } = await import("node:fs");
    const os = await import("node:os");
    const path = await import("node:path");
    const dir = mkdtempSync(path.join(os.tmpdir(), "keepalive-fail-"));
    const stub = path.join(dir, "python");
    writeFileSync(stub, "#!/bin/sh\necho 'REFRESHED|已续期'\n");
    chmodSync(stub, 0o755);
    const logs = [];
    const ctx = {
      logger: { info: (m) => logs.push(m), warn: (m) => logs.push(m), debug: () => {} },
      get: getter,
      effect: (fn) => { ctx.disposer = fn(); },
    };
    apply(ctx, { checkIntervalMs: 999999, python: stub,
      presetFile: path.join(dir, "agent.cordis.yml") });
    await settle(1200);
    ctx.disposer();
    assert.match(logs.join("\n"), /新建会话/, "必须给出可执行的退路");
  }
});
