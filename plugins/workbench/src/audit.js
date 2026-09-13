// 审计链路：把「信号 → 下单 → 成交」串成可追溯的时间线。
//
// 数据来源（全部只读）：
//   snapshot.previews  —— Harness 中由 quant_signal 产生的信号预览
//   snapshot.activity  —— Harness 观察到的券商工具响应（下单/撤单/查询）
//   trades             —— 本地模拟台账成交记录
//
// 这是纯函数：不读文件、不发请求，便于单测。

import { businessData, actionLabel, toolName, ORDER_SOURCE } from "./broker_trades.js";
import { labeled, zh } from "./labels.js";

const SIGNAL_WINDOW_MS = 7 * 24 * 3600 * 1000;

/** 影响券商状态的动作 → 时间线上的标签。 */
const ACTION_KIND = { "下单": "order", "改单": "order-modify", "撤单": "order-cancel" };

function toMs(value) {
  if (!value) return null;
  const text = String(value);
  const stamp = Date.parse(text.length === 10 ? `${text}T00:00:00Z` : text);
  return Number.isFinite(stamp) ? stamp : null;
}

function upper(value) {
  return typeof value === "string" ? value.trim().toUpperCase() : null;
}

/**
 * 从券商响应里尽力取标的与状态（只做浅层扫描，不做深递归）。
 *
 * 关键点：业务 JSON 是字符串，包在 value.content[].text 里。此前直接扫 value，
 * 于是永远扫不到 symbol，时间线上全是「未知标的」，信号关联也恒为 0。
 */
export function extractBrokerFields(value) {
  const found = { ticker: null, status: null, orderId: null };
  // 先尝试按 MCP 信封解开；解不开就退回按原样扫描（例如已是普通对象）
  const unwrapped = businessData({ value });
  const queue = [unwrapped.ok ? unwrapped.data : value];
  let visited = 0;
  while (queue.length > 0 && visited < 200) {
    const node = queue.shift();
    visited += 1;
    if (!node || typeof node !== "object") continue;
    for (const [key, item] of Object.entries(node)) {
      const lower = key.toLowerCase();
      if (found.ticker === null && ["code", "symbol", "ticker", "stock_code"].includes(lower) && typeof item === "string") {
        found.ticker = upper(item.replace(/^(SH|SZ|HK|US)\./i, "").replace(/\.(SH|SZ|HK|US)$/i, ""));
      }
      if (found.status === null && ["status", "order_status", "state"].includes(lower) && typeof item === "string") {
        found.status = item;
      }
      if (found.orderId === null && ["order_id", "orderid", "id"].includes(lower) && (typeof item === "string" || typeof item === "number")) {
        found.orderId = String(item);
      }
      if (item && typeof item === "object") queue.push(item);
    }
  }
  return found;
}

/**
 * 构建审计链路。
 * @param {{snapshot?: object, trades?: object, maxEntries?: number}} input
 * @returns {{entries: object[], stats: object}}
 */
export function buildAuditChain({ snapshot = {}, trades = {}, maxEntries = 120 } = {}) {
  const signals = (snapshot.previews ?? [])
    .filter((p) => p && p.kind === "signal" && p.value && typeof p.value === "object")
    .map((p, index) => ({
      id: p.id ?? `signal-${index}`,
      kind: "signal",
      at: p.at ?? null,
      atMs: toMs(p.at),
      ticker: upper(p.value.ticker),
      detail: `${labeled(p.value.signal, p.value.signal_label, "SIGNAL")} @ ${p.value.price ?? "—"}`
        + (p.value.strategy_label || p.value.strategy ? ` · ${p.value.strategy_label ?? p.value.strategy}` : ""),
      source: "quant_signal",
      source_label: "量化信号",
      origin: true,
    }))
    .filter((entry) => entry.ticker !== null);

  const fills = ((trades && trades.trades) ?? []).map((t, index) => ({
    id: `fill-${index}-${t.date ?? ""}`,
    kind: "fill",
    at: t.date ?? null,
    atMs: toMs(t.date),
    ticker: upper(t.ticker),
    detail: `${labeled(t.action, t.action_label, "ACTION")} ${t.shares} @ ${t.price}`
      + (t.fee !== undefined && t.fee !== null ? ` · 费用 ${t.fee}` : "")
      + (t.return !== undefined && t.return !== null ? ` · 收益 ${(t.return * 100).toFixed(2)}%` : ""),
    reason: t.reason ?? null,
    source: "local-ledger",
  }));

  // 只收录与订单**有关**的响应：改单/撤单/下单、订单列表查询，以及命名未知但显然
  // 与订单有关的工具。此前用 /(order|trade|fill)/ 一把抓，把 sim_trade_cash_info
  // 这类只读查询也标成「下单」——做了几笔交易完全看不出来。
  // 这里只排除**明确**的只读查询后缀；命名未知的订单工具宁可保留，也不静默丢弃。
  const READ_ONLY_QUERY = /(_info|_query|_summary|_accounts?|_accts?)$/;
  const orders = (snapshot.activity ?? [])
    .map((a) => {
      const name = toolName(a);
      const action = actionLabel(name);
      const isFacts = ORDER_SOURCE.test(name);
      const orderNamed = /order/i.test(name);
      if (action === null && !isFacts && !(orderNamed && !READ_ONLY_QUERY.test(name))) return null;
      const fields = extractBrokerFields(a.value ?? a);
      const kind = a.is_error ? "order-error"
        : isFacts && action === null ? "order-facts"
          : (ACTION_KIND[action] ?? (action === null ? "order-other" : "order"));
      return {
        id: a.id,
        kind,
        action: action ?? (isFacts ? "订单查询" : "订单工具"),
        at: a.at ?? null,
        atMs: toMs(a.at),
        order_id: fields.orderId,
        ticker: fields.ticker,
        detail: `${name}${fields.orderId ? ` · #${fields.orderId}` : ""}${fields.status ? ` · status=${fields.status}` : ""}`,
        source: "broker-observed",
        source_label: "券商响应（Harness 观察）",
      };
    })
    .filter(Boolean);

  // 下单/改单/撤单的响应只回 order_id，不带 symbol；同一 order_id 的订单记录里
  // 有 symbol。按订单号补全 —— 这是同一条订单的事实连接，不是对标的的猜测。
  const tickerByOrder = new Map();
  for (const entry of orders) {
    if (entry.order_id && entry.ticker) tickerByOrder.set(entry.order_id, entry.ticker);
  }
  for (const entry of orders) {
    if (!entry.ticker && entry.order_id && tickerByOrder.has(entry.order_id)) {
      entry.ticker = tickerByOrder.get(entry.order_id);
      entry.ticker_from = "order_id";
    }
  }

  // 为每个下单/成交找最近的、时间不晚于它的同标信号（7 天窗口内）
  const linkTo = (entry) => {
    if (entry.ticker === null || entry.atMs === null) return null;
    let best = null;
    for (const signal of signals) {
      if (signal.ticker !== entry.ticker || signal.atMs === null) continue;
      const delta = entry.atMs - signal.atMs;
      if (delta < 0 || delta > SIGNAL_WINDOW_MS) continue;
      if (best === null || signal.atMs > best.atMs) best = signal;
    }
    return best;
  };

  const decorated = [...fills, ...orders].map((entry) => {
    const signal = linkTo(entry);
    return {
      ...entry,
      signal_id: signal?.id ?? null,
      signal_at: signal?.at ?? null,
      linked: signal !== null,
      lag_hours: signal && entry.atMs !== null ? Math.round((entry.atMs - signal.atMs) / 3600000) : null,
    };
  });

  const entries = [...signals, ...decorated]
    .sort((a, b) => (b.atMs ?? 0) - (a.atMs ?? 0))
    .slice(0, maxEntries);

  const linked = decorated.filter((e) => e.linked).length;
  // 动作明细：不拆开的话，「订单响应 34」到底是下了 34 单还是查了 34 次，读不出来
  const order_kinds = {};
  for (const entry of orders) {
    const label = entry.action ?? "其他";
    order_kinds[label] = (order_kinds[label] ?? 0) + 1;
  }
  return {
    entries,
    stats: {
      signals: signals.length,
      orders: orders.length,
      order_kinds,
      fills: fills.length,
      linked,
      unlinked: decorated.length - linked,
      link_rule: "同标的、信号时间在前且间隔 ≤ 7 天",
    },
  };
}
