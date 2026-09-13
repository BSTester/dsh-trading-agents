import test from "node:test";
import assert from "node:assert/strict";
import { buildAuditChain, extractBrokerFields } from "../plugins/workbench/src/audit.js";
import { createRpcHandler } from "../plugins/workbench/src/rpc.js";

const snapshot = {
  mode: "sim", generated_at: "2026-09-11T10:00:00Z", in_flight: 0, pending_observations: 0,
  previews: [
    { id: "s1", kind: "signal", at: "2026-09-10T09:35:00Z", value: { ticker: "600519", signal: "BUY", price: 1275.16, strategy: "rsi" } },
    { id: "s2", kind: "backtest", at: "2026-09-10T09:40:00Z", value: { ticker: "600519", summary: {} } },
  ],
  activity: [
    { id: "a1", at: "2026-09-10T09:36:20Z", tool: "mcp__futu__sim_trade_input_order", is_error: false,
      value: { data: { code: "SH.600519", order_id: "998877", status: "SUBMITTED" } } },
    { id: "a2", at: "2026-08-01T09:00:00Z", tool: "mcp__futu__trading_order_place", is_error: true, value: { code: "SH.000001" } },
  ],
};

const trades = { mode: "sim", trades: [
  { date: "2026-09-11", action: "BUY", ticker: "600519", shares: 500, price: 1275.16, fee: 191.27, reason: "rsi 信号" },
] };

test("audit chain links fills and orders to their preceding signal", () => {
  const chain = buildAuditChain({ snapshot, trades });
  assert.equal(chain.stats.signals, 1, "只有 signal 预览进入链路，backtest 预览不算");
  assert.equal(chain.stats.orders, 2);
  assert.equal(chain.stats.fills, 1);
  assert.equal(chain.stats.linked, 2, "成交与下单响应各关联到信号");
  assert.equal(chain.stats.unlinked, 1, "8 月那笔错误单超出 7 天窗口，未关联");

  const fill = chain.entries.find((e) => e.kind === "fill");
  assert.equal(fill.signal_id, "s1");
  assert.equal(fill.linked, true);
  assert.equal(fill.lag_hours, 14);

  const error = chain.entries.find((e) => e.kind === "order-error");
  assert.equal(error.linked, false);
  assert.equal(error.signal_id, null);
});

test("audit chain marks signals as origin and sorts newest first", () => {
  const chain = buildAuditChain({ snapshot, trades });
  const signal = chain.entries.find((e) => e.kind === "signal");
  assert.equal(signal.origin, true);
  const stamps = chain.entries.map((e) => e.atMs ?? 0);
  for (let i = 1; i < stamps.length; i += 1) assert.ok(stamps[i - 1] >= stamps[i], "应按时间倒序");
});

test("extractBrokerFields strips market prefixes and finds ids/status", () => {
  assert.deepEqual(extractBrokerFields({ data: { code: "HK.00700", order_id: 42, status: "FILLED" } }),
    { ticker: "00700", status: "FILLED", orderId: "42" });
  assert.deepEqual(extractBrokerFields({ symbol: "600519.SH" }).ticker, "600519");
  assert.equal(extractBrokerFields("not an object").ticker, null);
});

test("audit chain tolerates empty or hostile inputs", () => {
  const empty = buildAuditChain({});
  assert.equal(empty.entries.length, 0);
  assert.equal(empty.stats.signals, 0);
  const cyclic = {};
  cyclic.self = cyclic;
  assert.doesNotThrow(() => buildAuditChain({ snapshot: { previews: [{}], activity: [{ tool: "order", value: cyclic }] } }));
});

test("audit endpoint composes snapshot and trades, rejecting payloads", async () => {
  const store = { snapshot: () => snapshot };
  const handle = createRpcHandler(store, { analytics: { trades: async () => trades } });
  const ok = await handle("audit", {});
  assert.equal(ok.ok, true);
  assert.equal(ok.value.stats.fills, 1);
  const withPayload = await handle("audit", { mode: "sim" });
  assert.equal(withPayload.ok, false);
  assert.equal(withPayload.error.code, "trading/invalid-operation");
});

test("audit endpoint still returns a chain when the ledger is unreadable", async () => {
  const store = { snapshot: () => snapshot };
  const handle = createRpcHandler(store, { analytics: { trades: async () => { throw new Error("台账不可读"); } } });
  const result = await handle("audit", {});
  assert.equal(result.ok, true, "台账故障不应让审计页空白");
  assert.equal(result.value.stats.fills, 0);
  assert.equal(result.value.stats.orders, 2);
});
