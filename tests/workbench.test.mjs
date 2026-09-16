import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile, unlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { WorkbenchStore, withRunStatus, ABANDONED_AFTER_MS } from "../plugins/workbench/src/store.js";

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

test("可以显式取消一条进行中的研究记录，且保留记录", async (t) => {
  const { store } = await fixture(t);
  const run = store.beginResearch({ ticker: "AAPL", session_id: "s1" });
  assert.equal(store.snapshot().runs[0].status, "running");

  const settled = store.cancelRun(run.id);
  assert.equal(settled.status, "cancelled");
  assert.ok(settled.settled_at, "应记录settle时间");
  // 记录保留（不是删除），且处于可追溯的终态
  const after = store.read().runs;
  assert.equal(after.length, 1);
  assert.equal(after[0].status, "cancelled");
  // 快照只把 running 当"进行中"，取消后不再出现在那条提示里
  assert.deepEqual(store.snapshot().runs.filter((row) => row.status === "running"), []);
  // 留痕
  assert.ok(store.read().activity.some((row) => row.kind === "research_cancelled"));
});

test("取消一个不存在的或已结算的 run 会明确报错", async (t) => {
  const { store } = await fixture(t);
  assert.throws(() => store.cancelRun("nope"), /Unknown run/);
  const run = store.beginResearch({ ticker: "AAPL", session_id: "s1" });
  store.cancelRun(run.id);
  assert.throws(() => store.cancelRun(run.id), /already settled/);
});

test("cancel-stale 只取消超时的，不动新鲜的和已结算的", async (t) => {
  const { store } = await fixture(t);
  const stale = store.beginResearch({ ticker: "AAPL", session_id: "s1" });
  const fresh = store.beginResearch({ ticker: "TSLA", session_id: "s1" });
  const aged = new Date(Date.now() - ABANDONED_AFTER_MS - 3600_000).toISOString();
  store.update((state) => { state.runs.find((row) => row.id === stale.id).started_at = aged; });

  const cancelled = store.cancelStaleRuns();
  assert.deepEqual(cancelled, [stale.id]);
  const byId = Object.fromEntries(store.read().runs.map((row) => [row.id, row.status]));
  assert.equal(byId[stale.id], "cancelled");
  assert.equal(byId[fresh.id], "running");
});

test("WP7 面板退役后 store 不再有确认面：确认类端点/方法不存在，快照无 confirmation 键", async (t) => {
  const { store } = await fixture(t);
  // 实盘确认只存在于服务侧（platform/server/store_access.py + Web 确认卡片）
  for (const gone of ["requestConfirmation", "confirmationView", "decideConfirmation"]) {
    assert.equal(store[gone], undefined, `${gone} 必须已删除`);
  }
  assert.equal("confirmation" in store.snapshot(), false, "快照不得再携带 confirmation 键");
  store.recordObservation({ tool: "mcp__futu__sim_trade_position_list", mode: "sim",
    session_id: "s1", is_error: false, value: { positions: [] } });
  assert.equal(store.snapshot().confirmation, undefined);
});
