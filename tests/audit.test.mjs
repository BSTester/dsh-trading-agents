import test from "node:test";
import assert from "node:assert/strict";
import { buildAuditChain, extractBrokerFields } from "../plugins/workbench/src/audit.js";
import { createRpcHandler } from "../plugins/workbench/src/rpc.js";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";

/** 一次性缓存目录：测试绝不写进用户真实的 ~/.dsh/trading-workbench-cache。 */
function isolatedCache(t) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "audit-cache-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return { dir };
}

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

test("audit endpoint composes snapshot and trades, rejecting payloads", async (t) => {
  const store = { snapshot: () => snapshot };
  const handle = createRpcHandler(store, { ...isolatedCache(t),
    analytics: { trades: async () => trades } });
  const ok = await handle("audit", {});
  assert.equal(ok.ok, true);
  assert.equal(ok.value.stats.fills, 1);
  const withPayload = await handle("audit", { mode: "sim" });
  assert.equal(withPayload.ok, false);
  assert.equal(withPayload.error.code, "trading/invalid-operation");
});

test("audit endpoint still returns a chain when the ledger is unreadable", async (t) => {
  const store = { snapshot: () => snapshot };
  const handle = createRpcHandler(store, { ...isolatedCache(t),
    analytics: { trades: async () => { throw new Error("台账不可读"); } } });
  const result = await handle("audit", {});
  assert.equal(result.ok, true, "台账故障不应让审计页空白");
  assert.equal(result.value.stats.fills, 0);
  assert.equal(result.value.stats.orders, 2);
});

// MCP 工具响应把业务 JSON 当作**字符串**放在 value.content[].text 里。
// 此前 extractBrokerFields 直接扫 value，永远扫不到 symbol，
// 于是审计时间线上所有条目都是「未知标的」，信号关联恒为 0。
const envelope = (payload) => ({ value: { content: [{ type: "text", text: JSON.stringify(payload) }] } });

test("extractBrokerFields 解开 MCP 信封后能取到标的", () => {
  const found = extractBrokerFields(envelope({
    ret_code: 0, ret_msg: "success",
    data: { orders: [{ symbol: "00700", order_id: "7137795", status: "5" }] },
  }).value);
  assert.equal(found.ticker, "00700");
  assert.equal(found.orderId, "7137795");
  assert.equal(found.status, "5");
});

test("信封是 s/d 形式时同样能取到标的", () => {
  const found = extractBrokerFields(envelope({ s: "ok", d: { symbol: "HK.09988", order_id: 42 } }).value);
  assert.equal(found.ticker, "09988", "市场前缀应被去掉");
  assert.equal(found.orderId, "42");
});

test("信封解不开时退回原样扫描，不静默丢字段", () => {
  const found = extractBrokerFields({ code: "SH.600519", order_id: "1" });
  assert.equal(found.ticker, "600519");
});

const activity = [
  { id: "facts", at: "2026-09-10T09:00:00Z", tool: "mcp__futu__sim_trade_history_order_list", is_error: false,
    ...envelope({ ret_code: 0, data: { orders: [{ symbol: "00700", order_id: "7137795" }] } }) },
  { id: "cancel", at: "2026-09-10T09:05:00Z", tool: "mcp__futu__sim_trade_cancel_order", is_error: false,
    ...envelope({ ret_code: 0, data: { order_id: "7137795" } }) },
  { id: "modify", at: "2026-09-10T09:06:00Z", tool: "mcp__futu__sim_trade_modify_order", is_error: true,
    ...envelope({ ret_code: -5, ret_msg: "backend business error" }) },
  { id: "place", at: "2026-09-10T09:07:00Z", tool: "mcp__futu__sim_trade_input_order", is_error: false,
    ...envelope({ ret_code: 0, data: { order_id: "7137796" } }) },
  { id: "cash", at: "2026-09-10T09:08:00Z", tool: "mcp__futu__sim_trade_cash_info", is_error: false,
    ...envelope({ ret_code: 0, data: { cash: 100 } }) },
  { id: "positions", at: "2026-09-10T09:09:00Z", tool: "mcp__futu__sim_trade_position_list", is_error: false,
    ...envelope({ ret_code: 0, data: { positions: [] } }) },
];

test("只读查询不得计入订单响应（此前一律标成「下单」）", () => {
  const chain = buildAuditChain({ snapshot: { ...snapshot, activity }, trades: { trades: [] } });
  assert.equal(chain.stats.orders, 4, "现金/持仓查询不是交易事实");
  assert.equal(chain.stats.order_kinds["订单查询"], 1);
  assert.equal(chain.stats.order_kinds["撤单"], 1);
  assert.equal(chain.stats.order_kinds["改单"], 1);
  assert.equal(chain.stats.order_kinds["下单"], 1);
  const kinds = chain.entries.filter((e) => e.kind !== "signal").map((e) => e.kind).sort();
  assert.deepEqual(kinds, ["order", "order-cancel", "order-error", "order-facts"]);
});

test("下单/改单/撤单的响应只回 order_id，按订单号回填标的", () => {
  const chain = buildAuditChain({ snapshot: { ...snapshot, activity }, trades: { trades: [] } });
  const cancel = chain.entries.find((e) => e.kind === "order-cancel");
  assert.equal(cancel.ticker, "00700", "撤单本身不带 symbol，应由同一 order_id 的订单记录补全");
  assert.equal(cancel.ticker_from, "order_id");
  // 失败改单没有 order_id，无从连接，应如实留空而不是猜
  const failed = chain.entries.find((e) => e.kind === "order-error");
  assert.equal(failed.ticker, null);
});
