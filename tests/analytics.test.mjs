import test from "node:test";
import assert from "node:assert/strict";
import { createRpcHandler } from "../plugins/workbench/src/rpc.js";
import { createAnalyticsProvider } from "../plugins/workbench/src/analytics.js";

const equity = { mode: "sim", count: 3, current: 1010000, total_return: 0.01, max_drawdown: -0.02,
  points: [{ t: "2026-09-09", equity: 1000000, dd: 0 }, { t: "2026-09-10", equity: 1010000, dd: 0 }] };
const positions = { mode: "sim", cash: 100, market_value: 200, equity: 300, positions: [] };
const correlation = { tickers: ["600519", "000001"], matrix: [[1, 0.4], [0.4, 1]], window: 120 };

function handlerWith(analytics) {
  return createRpcHandler({}, { analytics });
}

test("equity / positions / correlation return provider values", async () => {
  const handle = handlerWith({
    equity: async () => equity, positions: async () => positions, correlation: async () => correlation,
  });
  assert.equal((await handle("equity", { mode: "sim", window: 250 })).value.current, 1010000);
  assert.equal((await handle("positions", { mode: "sim" })).value.equity, 300);
  assert.deepEqual((await handle("correlation", { tickers: ["600519", "000001"], window: 120 })).value.matrix[0], [1, 0.4]);
});

test("analytics endpoints reject unexpected fields without calling the provider", async () => {
  let called = false;
  const handle = handlerWith({ equity: async () => { called = true; return equity; } });
  const result = await handle("equity", { mode: "sim", order: "buy" });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "trading/invalid-operation");
  assert.equal(called, false);
});

test("missing provider and provider failures degrade without throwing", async () => {
  const noProvider = await createRpcHandler({}, {})("equity", {});
  assert.equal(noProvider.ok, false);
  assert.match(noProvider.error.message, /provider unavailable/i);

  const failing = await handlerWith({ equity: async () => { throw new Error("无成交记录"); } })("equity", {});
  assert.equal(failing.ok, false);
  assert.equal(failing.error.code, "trading/analytics-unavailable");
  assert.match(failing.error.message, /无成交记录/);
});

test("analytics provider validates mode, window, and ticker list before spawning python", async () => {
  let executions = 0;
  const analytics = createAnalyticsProvider({
    exec: async () => { executions += 1; return { stdout: JSON.stringify(equity) }; },
    python: () => "/tmp/python",
    now: () => 0,
  });
  await assert.rejects(() => analytics.equity({ mode: "paper" }), /Invalid mode/);
  await assert.rejects(() => analytics.equity({ window: 5 }), /Invalid window/);
  await assert.rejects(() => analytics.correlation({ tickers: ["600519"] }), /2\.\.8/);
  await assert.rejects(() => analytics.correlation({ tickers: ["600519", "a; rm -rf /"] }), /Invalid ticker/);
  assert.equal(executions, 0, "非法参数绝不到达子进程");
  await analytics.equity({ mode: "sim", window: 250 });
  assert.equal(executions, 1);
});

test("analytics provider caches per (mode, window) and refetches after TTL", async () => {
  let executions = 0;
  let clock = 0;
  const analytics = createAnalyticsProvider({
    exec: async () => { executions += 1; return { stdout: JSON.stringify(equity) }; },
    python: () => "/tmp/python",
    now: () => clock,
  });
  await analytics.equity({ mode: "sim", window: 250 });
  await analytics.equity({ mode: "sim", window: 250 });
  assert.equal(executions, 1);
  await analytics.equity({ mode: "sim", window: 120 });
  assert.equal(executions, 2, "不同 window 应各自取数");
  clock = 60_000;
  await analytics.equity({ mode: "sim", window: 250 });
  assert.equal(executions, 3, "TTL 过期重新取数");
});

test("analytics provider surfaces python-reported errors", async () => {
  const analytics = createAnalyticsProvider({
    exec: async () => ({ stdout: JSON.stringify({ error: "共同交易日不足（5 天）" }) }),
    python: () => "/tmp/python",
    now: () => 0,
  });
  await assert.rejects(() => analytics.correlation({ tickers: ["600519", "000001"] }), /共同交易日不足/);
});
