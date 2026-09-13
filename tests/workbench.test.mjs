import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile, unlink } from "node:fs/promises";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { WorkbenchStore, withRunStatus, ABANDONED_AFTER_MS } from "../plugins/workbench/src/store.js";
import { createRpcHandler, createRpcFetchHandler, CACHE_TTL_MS } from "../plugins/workbench/src/rpc.js";
import { ENDPOINTS } from "../plugins/workbench/src/endpoints.js";

async function fixture(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "trading-workbench-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  return { home, store: new WorkbenchStore(home) };
}

/**
 * 磁盘缓存是跨进程共享的，测试若不注入独立目录，会读到上一次运行留下的条目。
 * 这里统一给每个用例一个一次性目录。
 */
function isolatedCache(t) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "trading-cache-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return { dir };
}

const published = {
  ticker: "00700.HK", rating: "Hold", report: "有来源的研究结论，不构成投资建议。",
  sources: [{ name: "Futu quote", as_of: "2026-09-12T06:00:00Z", reference: "mcp__futu__quote_stock_quote" }],
};

test("store persists reports with run identity and mode, refusing invalid ratings", async (t) => {
  const { home, store } = await fixture(t);
  const run = store.beginResearch({ ticker: "00700.HK", session_id: "s1" });
  assert.equal(run.status, "running");
  assert.throws(() => store.publishResearch({ ...published, run_id: run.id, rating: "BUY" }, "s1"), /rating/);
  assert.throws(() => store.publishResearch({ ...published, run_id: run.id, sources: [] }, "s1"), /sources/);
  assert.throws(() => store.publishResearch({ ...published, run_id: run.id }, "s2"), /session/);
  store.publishResearch({ ...published, run_id: run.id }, "s1");
  const snapshot = new WorkbenchStore(home).snapshot();
  assert.equal(snapshot.reports[0].rating, "Hold");
  assert.equal(snapshot.runs[0].status, "completed");
  assert.equal(snapshot.reports[0].mode, "sim");
  assert.equal(snapshot.broker, null);
});

test("mode switch requires exact confirmation, expected mode, and no in-flight broker calls", async (t) => {
  const { home, store } = await fixture(t);
  assert.throws(() => store.switchMode({ mode: "live", expected_mode: "sim" }), /确认实盘/);
  const release = store.enterBrokerCall("sim");
  assert.throws(() => store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" }), /进行/);
  assert.throws(() => new WorkbenchStore(home).switchMode({
    mode: "live", expected_mode: "sim", confirmation: "确认实盘",
  }), /进行/);
  release();
  const result = store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.equal(result.mode, "live");
  assert.throws(() => store.switchMode({ mode: "sim", expected_mode: "sim" }), /changed/);
  assert.equal(store.switchMode({ mode: "sim", expected_mode: "live" }).mode, "sim");
});

test("invalid mode and corrupted state fail closed; live persists across store instances", async (t) => {
  const { home, store } = await fixture(t);
  store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.equal(new WorkbenchStore(home).readMode(), "live");
  await writeFile(path.join(home, "trading-account-mode"), "broken");
  assert.throws(() => store.snapshot(), /mode/);
  await writeFile(path.join(home, "trading-account-mode"), "sim");
  await writeFile(path.join(home, "trading-workbench.json"), "{bad");
  assert.throws(() => new WorkbenchStore(home).snapshot(), /JSON/);
});

test("observed responses are not fills and snapshots never mix sim/live", async (t) => {
  const { store } = await fixture(t);
  store.recordObservation({ tool: "mcp__futu__sim_trade_input_order", mode: "sim", session_id: "s1",
    is_error: false, value: { order_id: "123", status: "SUBMITTED", access_token: "hidden" } });
  store.recordPreview("signal", { ticker: "600519", signal: "BUY" }, "sim");
  const sim = store.snapshot();
  assert.equal(sim.activity[0].kind, "broker_response");
  assert.equal(sim.broker.value.order_id, "123");
  assert.equal(JSON.stringify(sim).includes("hidden"), false);
  store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  const live = store.snapshot();
  assert.equal(live.broker, null);
  assert.equal(live.previews.length, 0);
  assert.equal(live.activity.some(row => row.kind === "broker_response"), false);
});

test("RPC exposes only snapshot and explicit mode switch, never tools or orders", async (t) => {
  const { store } = await fixture(t);
  const rpc = createRpcHandler(store);
  assert.equal((await rpc("snapshot", {})).ok, true);
  assert.equal((await rpc("execute", { tool: "trading_order_place" })).ok, false);
  assert.equal((await rpc("switch-mode", { mode: "live", expected_mode: "sim" })).ok, false);
  assert.equal((await rpc("switch-mode", { mode: "sim", expected_mode: "sim", extra: true })).ok, false);
  assert.equal((await rpc("switch-mode", { mode: "live", expected_mode: "sim", confirmation: "确认实盘" })).value.mode, "live");
});

test("completed broker observation survives another writer's lock and process restart", async (t) => {
  const { home, store } = await fixture(t);
  const lock = path.join(home, "trading-workbench.lock");
  await writeFile(lock, "another writer");
  store.recordObservation({ tool: "mcp__futu__sim_trade_input_order", mode: "sim",
    session_id: "s1", is_error: false, value: { order_id: "123", status: "SUBMITTED" } });
  assert.equal(store.snapshot().pending_observations, 1);
  await unlink(lock);
  const snapshot = new WorkbenchStore(home).snapshot();
  assert.equal(snapshot.broker.value.order_id, "123");
  assert.equal(snapshot.pending_observations, 0);
  assert.equal(new WorkbenchStore(home).snapshot().activity.filter(row => row.kind === "broker_response").length, 1);
});

test("native RPC envelope rejects endpoint mismatch before any mutation", async (t) => {
  const { store } = await fixture(t);
  const fetch = createRpcFetchHandler(store, "switch-mode");
  const response = await fetch(new Request("http://localhost/api/trading-workbench/switch-mode", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ type: "client-request", rpcId: "x", method: "execute",
      payload: { mode: "live", expected_mode: "sim", confirmation: "确认实盘" } }),
  }));
  assert.equal(response.status, 400);
  assert.equal(store.readMode(), "sim");
});

test("snapshot 声明 Host 实际提供的接口清单", async (t) => {
  const { store } = await fixture(t);
  const handle = createRpcHandler(store);
  const result = await handle("snapshot", {});
  assert.equal(result.ok, true);
  assert.deepEqual(result.value.endpoints, ENDPOINTS);
  // 客户端据此识别"进程陈旧"，因此清单必须与注册的路由同源
  assert.ok(result.value.endpoints.includes("positions"));
});

test("重复请求命中 Host 缓存，不再重跑取数", async (t) => {
  const { store } = await fixture(t);
  let calls = 0;
  const handle = createRpcHandler(store, {
    ...isolatedCache(t),
    analytics: { async positions() { calls += 1; return { positions: [] }; } },
  });
  const first = await handle("positions", { mode: "sim" });
  const second = await handle("positions", { mode: "sim" });
  assert.equal(calls, 1, "同一请求不应重复调用 provider");
  assert.equal(first.cached, false);
  assert.equal(second.cached, true);
  assert.equal(typeof second.cached_at, "string");
  assert.deepEqual(second.value, first.value);
});

test("_refresh 绕过缓存但仍是合法请求", async (t) => {
  const { store } = await fixture(t);
  let calls = 0;
  const handle = createRpcHandler(store, {
    ...isolatedCache(t),
    analytics: { async positions() { calls += 1; return { positions: [], n: calls }; } },
  });
  await handle("positions", { mode: "sim" });
  const forced = await handle("positions", { mode: "sim", _refresh: true });
  assert.equal(calls, 2);
  assert.equal(forced.cached, false);
  assert.equal(forced.value.n, 2);
});

test("不同参数各自缓存，互不串味", async (t) => {
  const { store } = await fixture(t);
  const seen = [];
  const handle = createRpcHandler(store, {
    ...isolatedCache(t),
    analytics: { async instrument(payload) { seen.push(payload.ticker); return { ticker: payload.ticker }; } },
  });
  await handle("instrument", { ticker: "600519" });
  await handle("instrument", { ticker: "00700.HK" });
  await handle("instrument", { ticker: "600519" });
  assert.deepEqual(seen, ["600519", "00700.HK"], "第三个请求应命中缓存");
});

test("缓存过期后重新取数", async (t) => {
  const { store } = await fixture(t);
  let calls = 0;
  let clock = 1_000_000;
  const handle = createRpcHandler(store, {
    ...isolatedCache(t),
    now: () => clock,
    analytics: { async positions() { calls += 1; return { n: calls }; } },
  });
  await handle("positions", { mode: "sim" });
  clock += CACHE_TTL_MS.positions - 1;
  assert.equal((await handle("positions", { mode: "sim" })).cached, true);
  clock += 2;
  const after = await handle("positions", { mode: "sim" });
  assert.equal(after.cached, false);
  assert.equal(calls, 2);
});

test("_refresh 不参与各接口的字段校验", async (t) => {
  const { store } = await fixture(t);
  const handle = createRpcHandler(store, { analytics: { async positions() { return {}; } } });
  const ok = await handle("positions", { mode: "sim", _refresh: true });
  assert.equal(ok.ok, true);
  const bad = await handle("positions", { mode: "sim", unexpected: 1 });
  assert.equal(bad.ok, false, "其他多余字段仍必须被拒绝");
});

test("被中断的会话不会留下永远「进行中」的 run", async (t) => {
  const { store } = await fixture(t);
  const run = store.beginResearch({ ticker: "AAPL", session_id: "s1" });
  // 刚发起：仍算进行中
  assert.equal(store.snapshot().runs[0].status, "running");
  // 超过阈值（模拟会话被中断）：派生为 abandoned，但原始数据不动
  const later = Date.parse(run.started_at) + ABANDONED_AFTER_MS + 60_000;
  assert.equal(withRunStatus(run, later).status, "abandoned");
  assert.equal(store.read().runs[0].status, "running", "不得改写历史数据");
});

test("派生状态不会误伤已完成或无时间戳的 run", () => {
  const old = { status: "running", started_at: new Date(Date.now() - 10 * 3600_000).toISOString() };
  assert.equal(withRunStatus(old).status, "abandoned");
  assert.equal(withRunStatus({ status: "completed", started_at: old.started_at }).status, "completed");
  assert.equal(withRunStatus({ status: "running" }).status, "running", "无时间戳时无法判定，保持原状");
});

test("清理孤儿 run：有研报的保留，新鲜的不动", async (t) => {
  const { store } = await fixture(t);
  // 旧的孤儿（无研报）
  const orphan = store.beginResearch({ ticker: "AAPL", session_id: "s1" });
  // 旧的但有研报——是真实产物，不能当垃圾清掉
  const kept = store.beginResearch({ ticker: "00700.HK", session_id: "s1" });
  store.publishResearch({ ...published, run_id: kept.id }, "s1");
  // 新鲜的 running
  const fresh = store.beginResearch({ ticker: "TSLA", session_id: "s1" });

  // 直接把这一个 run 调老。注意不能改用推后 now 的办法：那样"新鲜"的那条
  // 相对同一个未来时刻也会超时，测试就失去了区分能力。
  const aged = new Date(Date.now() - ABANDONED_AFTER_MS - 3600_000).toISOString();
  store.update((state) => { state.runs.find((row) => row.id === orphan.id).started_at = aged; });
  const removed = store.pruneAbandonedRuns();
  assert.deepEqual(removed, [orphan.id]);
  const ids = store.read().runs.map((row) => row.id).sort();
  assert.deepEqual(ids, [kept.id, fresh.id].sort());
});

test("清理在没有任何孤儿时是空操作", async (t) => {
  const { store } = await fixture(t);
  store.beginResearch({ ticker: "AAPL", session_id: "s1" });
  assert.deepEqual(store.pruneAbandonedRuns(), []);
  assert.equal(store.read().runs.length, 1);
});
