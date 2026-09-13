import test from "node:test";
import assert from "node:assert/strict";
import { createRpcHandler } from "../plugins/workbench/src/rpc.js";
import { createAnalyticsProvider } from "../plugins/workbench/src/analytics.js";
import { mkdtempSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const equity = { mode: "sim", count: 3, current: 1010000, total_return: 0.01, max_drawdown: -0.02,
  points: [{ t: "2026-09-09", equity: 1000000, dd: 0 }, { t: "2026-09-10", equity: 1010000, dd: 0 }] };
const positions = { mode: "sim", cash: 100, market_value: 200, equity: 300, positions: [] };
const correlation = { tickers: ["600519", "000001"], matrix: [[1, 0.4], [0.4, 1]], window: 120 };

/**
 * 每个 handler 用一次性磁盘缓存目录。
 * 磁盘缓存跨进程共享：若沿用默认目录，本次运行会读到上一次运行留下的条目，
 * 于是 provider 桩根本不会被调用，测试变得又脆弱又假通过。
 */
function handlerWith(analytics) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "analytics-cache-"));
  return createRpcHandler({}, { analytics, dir });
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
  const noProvider = await createRpcHandler({}, { dir: mkdtempSync(path.join(os.tmpdir(), "analytics-cache-")) })("equity", {});
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

test("sensitivity / risk / trades endpoints accept validated payloads only", async () => {
  const calls = [];
  const handle = handlerWith({
    sensitivity: async (payload) => { calls.push(["sensitivity", payload]); return { rows: [3, 5], cols: [10, 20], matrix: [[1, 2], [3, null]] }; },
    risk: async () => ({ config: { risk_per_trade: 0.01 }, source: "(默认值)" }),
    trades: async (payload) => { calls.push(["trades", payload]); return { count: 0, trades: [] }; },
  });
  const sens = await handle("sensitivity", { ticker: "600519", strategy: "ma_cross", metric: "sharpe", fast_grid: "3,5", slow_grid: "10,20" });
  assert.equal(sens.ok, true);
  assert.equal(sens.value.matrix[1][1], null);
  const risk = await handle("risk", {});
  assert.equal(risk.value.config.risk_per_trade, 0.01);
  const trades = await handle("trades", { mode: "sim", limit: 20 });
  assert.equal(trades.ok, true);
  // 越界字段被拒
  const bad = await handle("sensitivity", { ticker: "600519", order: "buy" });
  assert.equal(bad.ok, false);
  const badRisk = await handle("risk", { mode: "sim" });
  assert.equal(badRisk.ok, false);
});

test("sensitivity provider validates grids, metric, and start date", async () => {
  let executions = 0;
  const analytics = createAnalyticsProvider({
    exec: async () => { executions += 1; return { stdout: JSON.stringify({ rows: [], cols: [], matrix: [] }) }; },
    python: () => "/tmp/python", now: () => 0,
  });
  await assert.rejects(() => analytics.sensitivity({ ticker: "600519", metric: "profit" }), /Invalid metric/);
  await assert.rejects(() => analytics.sensitivity({ ticker: "600519", fast_grid: "3" }), /Invalid fast_grid/);
  await assert.rejects(() => analytics.sensitivity({ ticker: "600519", fast_grid: "3,999" }), /Invalid fast_grid value/);
  await assert.rejects(() => analytics.sensitivity({ ticker: "600519", start: "2023/01/01" }), /Invalid start date/);
  assert.equal(executions, 0);
  await analytics.sensitivity({ ticker: "600519" });
  assert.equal(executions, 1);
  await assert.rejects(() => analytics.trades({ mode: "paper" }), /Invalid mode/);
  await assert.rejects(() => analytics.trades({ limit: 9999 }), /Invalid limit/);
});

test("events endpoint validates ticker and window, degrading on failure", async () => {
  const handle = handlerWith({
    events: async (payload) => ({ ticker: payload.ticker, events: [], sources_status: { dividend: "ok" } }),
  });
  const ok = await handle("events", { ticker: "600519", days: 180 });
  assert.equal(ok.ok, true);
  const badField = await handle("events", { ticker: "600519", mode: "sim" });
  assert.equal(badField.ok, false);
  const bad = await createRpcHandler({}, { dir: mkdtempSync(path.join(os.tmpdir(), "analytics-cache-")), analytics: { events: async () => { throw new Error("仅支持 A 股"); } } });
  const failed = await bad("events", { ticker: "AAPL" });
  assert.equal(failed.error.code, "trading/analytics-unavailable");
  assert.match(failed.error.message, /仅支持 A 股/);
});

test("events provider validates ticker pattern and window bounds", async () => {
  let executions = 0;
  const analytics = createAnalyticsProvider({
    exec: async () => { executions += 1; return { stdout: JSON.stringify({ ticker: "600519", events: [] }) }; },
    python: () => "/tmp/python", now: () => 0,
  });
  await assert.rejects(() => analytics.events({ ticker: "600519; rm -rf /" }), /Invalid ticker/);
  await assert.rejects(() => analytics.events({ ticker: "600519", days: 10 }), /Invalid days/);
  assert.equal(executions, 0);
  await analytics.events({ ticker: "600519" });
  assert.equal(executions, 1);
});

test("factors / ic endpoints validate payloads and delegate", async () => {
  const seen = [];
  const handle = handlerWith({
    factors: async (payload) => { seen.push(payload); return { rows: [{ ticker: "600519", rank: 1, score: 0.5 }] }; },
    ic: async (payload) => { seen.push(payload); return { mean_ic: 0.09, icir: 0.16, points: [] }; },
  });
  const snap = await handle("factors", { tickers: ["600519", "000001"], window: 250 });
  assert.equal(snap.ok, true);
  assert.equal(snap.value.rows[0].rank, 1);
  const ic = await handle("ic", { tickers: ["600519", "000001", "601318"], factor: "mom_20", forward: 5, window: 250 });
  assert.equal(ic.value.mean_ic, 0.09);
  const bad = await handle("factors", { tickers: ["600519", "000001"], metric: "x" });
  assert.equal(bad.ok, false);
});

test("factors / ic providers enforce universe size, factor enum, and bounds", async () => {
  let executions = 0;
  const analytics = createAnalyticsProvider({
    exec: async () => { executions += 1; return { stdout: JSON.stringify({ rows: [] }) }; },
    python: () => "/tmp/python", now: () => 0,
  });
  await assert.rejects(() => analytics.factors({ tickers: ["600519"] }), /2\.\.8/);
  await assert.rejects(() => analytics.factors({ tickers: ["600519", "bad; rm"], window: 250 }), /Invalid ticker/);
  await assert.rejects(() => analytics.ic({ tickers: ["600519", "000001"] }), /3\.\.8/);
  await assert.rejects(() => analytics.ic({ tickers: ["600519", "000001", "601318"], factor: "hack" }), /Invalid factor/);
  await assert.rejects(() => analytics.ic({ tickers: ["600519", "000001", "601318"], forward: 900 }), /Invalid forward/);
  assert.equal(executions, 0, "非法参数不得进入子进程");
  await analytics.factors({ tickers: ["600519", "000001"] });
  await analytics.ic({ tickers: ["600519", "000001", "601318"] });
  assert.equal(executions, 2);
});

test("sources endpoint returns the self-check report and rejects unknown fields", async () => {
  const report = { checked_at: "2026-09-13T13:07:08+08:00",
    sources: [{ key: "futu", label: "富途", status: "ok", detail: "token 有效", fix: "-" }],
    summary: { ok: 1, warn: 0, fail: 0 } };
  const handle = handlerWith({ sources: async () => report });
  const ok = await handle("sources", {});
  assert.equal(ok.ok, true);
  assert.equal(ok.value.summary.ok, 1);
  assert.equal(ok.value.sources[0].key, "futu");
  const withNoProbe = await handle("sources", { no_probe: true });
  assert.equal(withNoProbe.ok, true);
  const bad = await handle("sources", { probe: true });
  assert.equal(bad.ok, false);
  assert.equal(bad.error.code, "trading/invalid-operation");
});

test("sources provider passes --no-probe through and degrades on failure", async () => {
  const seen = [];
  const analytics = createAnalyticsProvider({
    exec: async (_python, args) => { seen.push(args.join(" ")); return { stdout: JSON.stringify({ sources: [], summary: {} }) }; },
    python: () => "/tmp/python", now: () => 0,
  });
  await analytics.sources({ no_probe: true });
  assert.match(seen[0], /--no-probe/);
  await assert.rejects(() => analytics.sources({ probe: true }), /Unexpected sources field/);

  const failing = createAnalyticsProvider({
    exec: async () => ({ stdout: JSON.stringify({ error: "探测失败" }) }),
    python: () => "/tmp/python", now: () => 0,
  });
  await assert.rejects(() => failing.sources({}), /探测失败/);
});
