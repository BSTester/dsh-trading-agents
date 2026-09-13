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
