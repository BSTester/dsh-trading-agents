// 审计链路：把「信号 → 下单 → 成交」串成可追溯的时间线。
//
// 数据来源（全部只读）：
//   snapshot.previews  —— Harness 中由 quant_signal 产生的信号预览
//   snapshot.activity  —— Harness 观察到的券商工具响应（下单/撤单/查询）
//   trades             —— 本地模拟台账成交记录
//
// 这是纯函数：不读文件、不发请求，便于单测。

const ORDER_TOOL = /(order|trade|fill)/i;
const SIGNAL_WINDOW_MS = 7 * 24 * 3600 * 1000;

function toMs(value) {
  if (!value) return null;
  const text = String(value);
  const stamp = Date.parse(text.length === 10 ? `${text}T00:00:00Z` : text);
  return Number.isFinite(stamp) ? stamp : null;
}

function upper(value) {
  return typeof value === "string" ? value.trim().toUpperCase() : null;
}

/** 从券商响应里尽力取标的与状态（只做浅层扫描，不做深递归）。 */
export function extractBrokerFields(value) {
  const found = { ticker: null, status: null, orderId: null };
  const queue = [value];
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
      detail: `${p.value.signal ?? "—"} @ ${p.value.price ?? "—"}${p.value.strategy ? ` · ${p.value.strategy}` : ""}`,
      source: "quant_signal",
      origin: true,
    }))
    .filter((entry) => entry.ticker !== null);

  const fills = ((trades && trades.trades) ?? []).map((t, index) => ({
    id: `fill-${index}-${t.date ?? ""}`,
    kind: "fill",
    at: t.date ?? null,
    atMs: toMs(t.date),
    ticker: upper(t.ticker),
    detail: `${t.action} ${t.shares} @ ${t.price}${t.fee !== undefined && t.fee !== null ? ` · 费用 ${t.fee}` : ""}${t.return !== undefined && t.return !== null ? ` · 收益 ${(t.return * 100).toFixed(2)}%` : ""}`,
    reason: t.reason ?? null,
    source: "local-ledger",
  }));

  const orders = (snapshot.activity ?? [])
    .filter((a) => a && ORDER_TOOL.test(String(a.tool ?? "")))
    .map((a) => {
      const fields = extractBrokerFields(a.value ?? a);
      return {
        id: a.id,
        kind: a.is_error ? "order-error" : "order",
        at: a.at ?? null,
        atMs: toMs(a.at),
        ticker: fields.ticker,
        detail: `${a.tool}${fields.orderId ? ` · #${fields.orderId}` : ""}${fields.status ? ` · ${fields.status}` : ""}`,
        source: "broker-observed",
      };
    });

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
  return {
    entries,
    stats: {
      signals: signals.length,
      orders: orders.length,
      fills: fills.length,
      linked,
      unlinked: decorated.length - linked,
      link_rule: "同标的、信号时间在前且间隔 ≤ 7 天",
    },
  };
}
