import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile, unlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { WorkbenchStore } from "../plugins/workbench/src/store.js";
import { createRpcHandler, createRpcFetchHandler } from "../plugins/workbench/src/rpc.js";

async function fixture(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "trading-workbench-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  return { home, store: new WorkbenchStore(home) };
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
