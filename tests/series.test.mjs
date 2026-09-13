import test from "node:test";
import assert from "node:assert/strict";
import { createRpcHandler } from "../plugins/workbench/src/rpc.js";
import { createSeriesProvider } from "../plugins/workbench/src/series.js";

const bars = { ticker: "600519", period: "5m", source: "akshare/sina", as_of: "2026-09-11 15:00:00",
  count: 2, bars: [{ t: "2026-09-11 14:55:00", o: 1, h: 2, l: 0.5, c: 1.5, v: 10 },
                   { t: "2026-09-11 15:00:00", o: 1.5, h: 2.5, l: 1, c: 2, v: 20 }] };

function handlerWith(fetchSeries) {
  return createRpcHandler({}, { fetchSeries });
}

test("series endpoint returns bars from the injected provider", async () => {
  const handle = handlerWith(async () => bars);
  const result = await handle("series", { ticker: "600519", period: "5m", limit: 100 });
  assert.equal(result.ok, true);
  assert.equal(result.value.count, 2);
  assert.equal(result.value.bars[1].c, 2);
});

test("series endpoint rejects unexpected fields and never reaches the provider", async () => {
  let called = false;
  const handle = handlerWith(async () => { called = true; return bars; });
  const result = await handle("series", { ticker: "600519", order: "buy" });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "trading/invalid-operation");
  assert.equal(called, false);
});

test("series provider failure degrades to a readable error instead of throwing", async () => {
  const handle = handlerWith(async () => { throw new Error("分钟数据源不可用"); });
  const result = await handle("series", { ticker: "600519", period: "1m", limit: 50 });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "trading/series-unavailable");
  assert.match(result.error.message, /分钟数据源不可用/);
});

test("unknown endpoint still rejects, and provider absence is explicit", async () => {
  const handle = createRpcHandler({}, {});
  const unknown = await handle("orders", {});
  assert.equal(unknown.ok, false);
  assert.equal(unknown.error.code, "trading/invalid-operation");
  const noProvider = await handle("series", { ticker: "600519" });
  assert.equal(noProvider.ok, false);
  assert.match(noProvider.error.message, /provider unavailable/i);
});

test("series provider validates ticker, period, and limit before spawning python", async () => {
  let executions = 0;
  const fetchSeries = createSeriesProvider({
    exec: async () => { executions += 1; return { stdout: JSON.stringify(bars) }; },
    python: () => "/tmp/python",
    now: () => 0,
  });
  await assert.rejects(() => fetchSeries({ ticker: "600519; rm -rf /", period: "5m", limit: 100 }), /Invalid ticker/);
  await assert.rejects(() => fetchSeries({ ticker: "600519", period: "3s", limit: 100 }), /Invalid period/);
  await assert.rejects(() => fetchSeries({ ticker: "600519", period: "5m", limit: 5 }), /Invalid limit/);
  assert.equal(executions, 0, "非法参数绝不能到达子进程");
  const ok = await fetchSeries({ ticker: "600519", period: "5m", limit: 100 });
  assert.equal(ok.count, 2);
  assert.equal(executions, 1);
});

test("series provider caches within the TTL and refetches after it", async () => {
  let executions = 0;
  let clock = 0;
  const fetchSeries = createSeriesProvider({
    exec: async () => { executions += 1; return { stdout: JSON.stringify(bars) }; },
    python: () => "/tmp/python",
    now: () => clock,
  });
  await fetchSeries({ ticker: "600519", period: "5m", limit: 100 });
  await fetchSeries({ ticker: "600519", period: "5m", limit: 100 });
  assert.equal(executions, 1, "TTL 内应命中缓存");
  clock = 60_000;
  await fetchSeries({ ticker: "600519", period: "5m", limit: 100 });
  assert.equal(executions, 2, "TTL 过期后应重新取数");
});

test("series provider surfaces python-reported errors", async () => {
  const fetchSeries = createSeriesProvider({
    exec: async () => ({ stdout: JSON.stringify({ error: "取数失败：网络不可用" }) }),
    python: () => "/tmp/python",
    now: () => 0,
  });
  await assert.rejects(() => fetchSeries({ ticker: "600519", period: "5m", limit: 100 }), /网络不可用/);
});
