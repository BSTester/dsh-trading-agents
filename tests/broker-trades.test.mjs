// 交易概要的离线回归测试。
//
// 目标：确认 activity（工具调用记录）能被如实归纳成交易事实，且**不会无中生有**。
// 用真实记录的载荷形状构造样本，不访问网络、不读真实 store。
import assert from "node:assert/strict";
import test from "node:test";

import { summarizeBrokerActivity } from "../plugins/workbench/src/broker_trades.js";

/** 构造一条 activity 记录（形状与 store 记录的实际结构一致）。 */
function entry(tool, payload, extra = {}) {
  const text = typeof payload === "string" ? payload : JSON.stringify(payload);
  return {
    id: extra.id ?? `${tool}-${Math.random()}`,
    at: extra.at ?? "2026-09-12T10:10:19.517Z",
    kind: "broker_response",
    tool: `mcp__futu__${tool}`,
    mode: "sim",
    is_error: extra.is_error ?? false,
    value: { content: [{ type: "text", text }] },
  };
}

const ORDER = {
  ret_code: 0, ret_msg: "success",
  data: { orders: [{
    order_id: "6526051", symbol: "09961", stock_name: "携程集团-S",
    side: 2, qty: "200", price: "465.2", avg_fill_price: "465.2", cum_qty: "200",
    status: 4, create_time: "1768550082000000", update_time: "1768550371000000",
  }] },
};

test("从历史订单提取交易事实", () => {
  const summary = summarizeBrokerActivity([entry("sim_trade_history_order_list", ORDER)]);
  assert.equal(summary.orders.length, 1);
  const row = summary.orders[0];
  assert.equal(row.order_id, "6526051");
  assert.equal(row.symbol, "09961");
  assert.equal(row.name, "携程集团-S");
  assert.equal(row.side, "卖出");           // schema 明文：order_side 1=Buy 2=Sell
  assert.equal(row.qty, 200);
  assert.equal(row.filled_qty, 200);
  assert.equal(row.fill, "全部成交");
  assert.equal(row.amount, 93040);          // 200 × 465.2，全部来自券商原文
});

test("side 1 是买入", () => {
  const payload = { ret_code: 0, data: { orders: [{ ...ORDER.data.orders[0], side: 1 }] } };
  const row = summarizeBrokerActivity([entry("sim_trade_history_order_list", payload)]).orders[0];
  assert.equal(row.side, "买入");
});

test("成交情况由数量推导，不猜状态码", () => {
  const cases = [
    [{ qty: "100", cum_qty: "100" }, "全部成交"],
    [{ qty: "100", cum_qty: "40" }, "部分成交"],
    [{ qty: "100", cum_qty: "0" }, "未成交"],
    [{ qty: "0", cum_qty: "0" }, "未知"],
  ];
  for (const [qty, expected] of cases) {
    const payload = { ret_code: 0, data: { orders: [{ ...ORDER.data.orders[0], ...qty }] } };
    const row = summarizeBrokerActivity([entry("sim_trade_history_order_list", payload)]).orders[0];
    assert.equal(row.fill, expected, JSON.stringify(qty));
    // 原始状态码始终保留，供人工核对
    assert.equal(typeof row.status_code, "number");
  }
});

test("同一订单多次出现时按 order_id 去重并保留最新状态", () => {
  // 实测：该工具被调用 7 次，同一订单反复出现且状态会演进（2 → 4）
  const early = { ret_code: 0, data: { orders: [{ ...ORDER.data.orders[0], status: 2, cum_qty: "0" }] } };
  const late = { ret_code: 0, data: { orders: [{ ...ORDER.data.orders[0], status: 4, cum_qty: "200" }] } };
  const summary = summarizeBrokerActivity([
    entry("sim_trade_history_order_list", early, { at: "2026-09-12T10:09:00.000Z" }),
    entry("sim_trade_history_order_list", late, { at: "2026-09-12T10:12:00.000Z" }),
  ]);
  assert.equal(summary.orders.length, 1, "重复订单不得重复计数");
  assert.equal(summary.orders[0].status_code, 4, "必须保留最后一次观测");
  assert.equal(summary.orders[0].fill, "全部成交");
});

test("用自己记录到的撤单/改单动作补全订单生命周期", () => {
  // 状态码含义未知，但"券商对某 order_id 返回了撤单成功"是确凿事实
  const summary = summarizeBrokerActivity([
    entry("sim_trade_input_order", { ret_code: 0, data: { order_id: "7137731" } }),
    entry("sim_trade_modify_order", { ret_code: 0, data: { order_id: "7137731" } }),
    entry("sim_trade_cancel_order", { ret_code: 0, data: { order_id: "7137731" } }),
    entry("sim_trade_history_order_list", { ret_code: 0, data: { orders: [
      { ...ORDER.data.orders[0], order_id: "7137731", cum_qty: "0", status: 5 }] } }),
  ]);
  const row = summary.orders.find((o) => o.order_id === "7137731");
  assert.equal(row.cancelled, true);
  assert.equal(row.modified_count, 1);
});

test("失败的撤单不算已撤单", () => {
  const summary = summarizeBrokerActivity([
    entry("sim_trade_cancel_order", { ret_code: -5, ret_msg: "backend business error" }, { is_error: true }),
    entry("sim_trade_history_order_list", { ret_code: 0, data: { orders: [
      { ...ORDER.data.orders[0], order_id: "1", cum_qty: "0" }] } }),
  ]);
  assert.equal(summary.orders[0].cancelled, false);
});

test("只读查询只计数，不逐条列出", () => {
  const summary = summarizeBrokerActivity([
    entry("sim_trade_account_list", { ret_code: 0, data: {} }),
    entry("sim_trade_cash_info", { ret_code: 0, data: { mv: "1" } }),
    entry("sim_trade_max_buy_sell", { ret_code: 0, data: {} }),
  ]);
  assert.equal(summary.orders.length, 0);
  assert.equal(summary.actions.length, 0);
  assert.equal(summary.queries.count, 3, "查询次数必须如实统计");
  assert.deepEqual(summary.queries.tools.map((row) => row.tool).sort(),
    ["sim_trade_account_list", "sim_trade_cash_info", "sim_trade_max_buy_sell"]);
  assert.equal(summary.counts.responses, 3);
});

test("下单动作记录订单号", () => {
  const summary = summarizeBrokerActivity([
    entry("sim_trade_input_order", { ret_code: 0, ret_msg: "success", data: { order_id: "7137730" } }),
  ]);
  assert.equal(summary.actions.length, 1);
  assert.equal(summary.actions[0].action, "下单");
  assert.equal(summary.actions[0].ok, true);
  assert.equal(summary.actions[0].order_id, "7137730");
  assert.equal(summary.orders.length, 0, "下单响应本身不是订单事实");
});

test("撤单与改单分别标注", () => {
  const summary = summarizeBrokerActivity([
    entry("sim_trade_cancel_order", { ret_code: 0, data: { order_id: "1" } }),
    entry("sim_trade_modify_order", { ret_code: 0, data: { order_id: "2" } }),
  ]);
  // 用集合比较：中文排序依赖码点，断言顺序会引入无关的脆弱性
  assert.deepEqual(new Set(summary.actions.map((row) => row.action)), new Set(["改单", "撤单"]));
});

test("失败动作如实记录原因，不静默丢弃", () => {
  const summary = summarizeBrokerActivity([
    entry("sim_trade_modify_order", { ret_code: 0, ret_msg: "error", error: {} , data: {}} , { is_error: true }),
    entry("sim_trade_account_list", "Error: MCP error -32603: internal error", { is_error: true }),
  ]);
  assert.equal(summary.actions.length, 1);
  assert.equal(summary.actions[0].ok, false);
  assert.ok(summary.actions[0].detail.length > 0, "失败必须带原因");
  assert.equal(summary.counts.errors, 2);
  assert.equal(summary.queries.count, 1);
});

test("两种返回信封都能解析", () => {
  const accountEnvelope = entry("sim_trade_input_order", { s: "ok", d: { order_id: "999" } });
  const summary = summarizeBrokerActivity([accountEnvelope]);
  assert.equal(summary.actions[0].ok, true);
  assert.equal(summary.actions[0].order_id, "999");
});

test("业务错误码不会被当成成功", () => {
  const summary = summarizeBrokerActivity([
    entry("sim_trade_input_order", { ret_code: -3, ret_msg: "invalid parameter" }, { is_error: true }),
  ]);
  assert.equal(summary.actions[0].ok, false);
  assert.match(summary.actions[0].detail, /invalid parameter/);
});

test("空输入不产生任何事实", () => {
  const summary = summarizeBrokerActivity([]);
  assert.deepEqual(summary.orders, []);
  assert.deepEqual(summary.actions, []);
  assert.equal(summary.queries.count, 0);
  assert.deepEqual(summary.counts, {
    responses: 0, order_responses: 0, orders: 0, actions: 0, errors: 0,
  });
});

test("非数组输入不抛异常", () => {
  for (const value of [undefined, null, {}, "x"]) {
    const summary = summarizeBrokerActivity(value);
    assert.deepEqual(summary.orders, []);
    assert.equal(summary.queries.count, 0);
  }
});

test("无法解析的时间不编造", () => {
  const payload = { ret_code: 0, data: { orders: [{ ...ORDER.data.orders[0], create_time: "abc" }] } };
  const row = summarizeBrokerActivity([entry("sim_trade_history_order_list", payload)]).orders[0];
  assert.equal(row.ordered_at, null);
});

test("缺少 order_id 的行被丢弃而不是生成占位 id", () => {
  const payload = { ret_code: 0, data: { orders: [{ symbol: "09988", qty: "1", cum_qty: "1" }] } };
  const summary = summarizeBrokerActivity([entry("sim_trade_history_order_list", payload)]);
  assert.equal(summary.orders.length, 0);
});

test("订单按时间倒序", () => {
  const older = { ...ORDER.data.orders[0], order_id: "1", create_time: "1700000000000000" };
  const newer = { ...ORDER.data.orders[0], order_id: "2", create_time: "1768550082000000" };
  const payload = { ret_code: 0, data: { orders: [older, newer] } };
  const orders = summarizeBrokerActivity([entry("sim_trade_history_order_list", payload)]).orders;
  assert.deepEqual(orders.map((row) => row.order_id), ["2", "1"]);
});

test("notice 明确说明不是券商成交推送", () => {
  const summary = summarizeBrokerActivity([]);
  assert.match(summary.notice, /不是券商成交推送/);
});
