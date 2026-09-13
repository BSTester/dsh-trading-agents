// 交易概要：把「Harness 观察到的券商响应」还原成交易事实。
//
// 为什么需要它：原始 activity 记录的是**工具调用**（`mcp__futu__sim_trade_history_order_list`
// 这类），同一个工具被调 7 次就会有 7 条重复行，读起来像发生了很多笔交易，
// 而真正的事实（下单/改单/撤单/成交）反而被埋在 JSON 里。
//
// 本模块只做**归纳**，不做推测：
//   - 订单字段直接取券商返回的原文，不重算价格、不补默认值；
//   - 「成交情况」用 cum_qty 与 qty 的关系推导（可自证），而不是猜状态码枚举
//     （服务端未在响应或 schema 中给出 status 的含义，因此原文附上供核对）；
//   - 方向 1=买入 / 2=卖出 来自工具 schema 的明文说明（order_side: 1=Buy 2=Sell）；
//   - 只读查询不逐条铺开，只计数，且计数如实展示，不隐藏。
//
// 纯函数，无 IO，便于离线测试。

/** 下单/改单/撤单：这些才会改变券商侧状态。 */
const ORDER_ACTIONS = [
  { match: /input_order$/, label: "下单" },
  { match: /modify_order$/, label: "改单" },
  { match: /cancel_order$/, label: "撤单" },
];

/** 唯一能提供订单事实的工具：其 data.orders 是券商侧的订单列表。 */
const ORDER_SOURCE = /history_order_list$/;

const SIDE_LABELS = { 1: "买入", 2: "卖出" };

/** 从 MCP 工具响应条目里取出业务数据（兼容 ret_code/data 与 s/d 两种信封）。 */
function businessData(entry) {
  const content = entry?.value?.content;
  if (!Array.isArray(content)) return { ok: false, reason: "无内容" };
  const text = content.find((part) => part?.type === "text")?.text;
  if (typeof text !== "string") return { ok: false, reason: "无文本内容" };
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    // 工具层错误（如 MCP error -32603）是纯文本，如实报出而不是丢掉
    return { ok: false, reason: text.slice(0, 160) };
  }
  if (!parsed || typeof parsed !== "object") return { ok: false, reason: "返回不是对象" };
  if (typeof parsed.ret_code === "number" && parsed.ret_code !== 0) {
    return { ok: false, reason: `ret=${parsed.ret_code} ${parsed.ret_msg ?? ""}`.trim() };
  }
  if (parsed.s !== undefined && String(parsed.s).toLowerCase() !== "ok") {
    return { ok: false, reason: `s=${parsed.s}` };
  }
  const data = parsed.data ?? parsed.d ?? {};
  return { ok: true, data };
}

function toolName(entry) {
  return String(entry?.tool ?? "").replace(/^mcp__futu__/, "");
}

function actionLabel(name) {
  const found = ORDER_ACTIONS.find((row) => row.match.test(name));
  return found ? found.label : null;
}

function numeric(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** 由委托数量与已成交数量推导成交情况——可自证，不依赖未知的状态码枚举。 */
function fillState(qty, cumQty) {
  if (qty === null || qty <= 0 || cumQty === null) return "未知";
  if (cumQty >= qty) return "全部成交";
  if (cumQty > 0) return "部分成交";
  return "未成交";
}

/** 券商时间戳是微秒；无法解析时返回 null，不编造时间。 */
function microTime(value) {
  const micros = numeric(value);
  if (micros === null || micros <= 0) return null;
  const date = new Date(Math.floor(micros / 1000));
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function orderRow(raw, seenAt) {
  const qty = numeric(raw.qty);
  const cumQty = numeric(raw.cum_qty);
  const price = numeric(raw.price);
  const avgFill = numeric(raw.avg_fill_price);
  const sideCode = numeric(raw.side);
  return {
    order_id: String(raw.order_id ?? ""),
    symbol: String(raw.symbol ?? ""),
    name: String(raw.stock_name ?? ""),
    side: SIDE_LABELS[sideCode] ?? null,
    side_code: sideCode,
    qty,
    filled_qty: cumQty,
    price,
    avg_fill_price: avgFill,
    // 金额用「已成交数量 × 成交均价」——两个数都来自券商原文
    amount: (cumQty !== null && avgFill !== null) ? Math.round(cumQty * avgFill * 100) / 100 : null,
    fill: fillState(qty, cumQty),
    status_code: numeric(raw.status),
    ordered_at: microTime(raw.create_time),
    updated_at: microTime(raw.update_time),
    seen_at: seenAt ?? null,
  };
}

/**
 * 把 activity 归纳为交易概要。
 *
 * @param {Array} activity store.snapshot().activity（已按模式过滤）
 * @returns {{orders: Array, actions: Array, queries: object, counts: object, notice: string}}
 */
export function summarizeBrokerActivity(activity) {
  const rows = Array.isArray(activity) ? activity : [];
  const ordersById = new Map();
  const actions = [];
  const queryTools = {};
  let errors = 0;
  let orderResponses = 0;

  for (const entry of rows) {
    const name = toolName(entry);
    const parsed = businessData(entry);

    if (ORDER_SOURCE.test(name)) {
      if (!parsed.ok) {
        errors += 1;
        continue;
      }
      orderResponses += 1;
      const list = Array.isArray(parsed.data?.orders) ? parsed.data.orders : [];
      for (const raw of list) {
        const row = orderRow(raw, entry.at ?? null);
        if (!row.order_id) continue;
        // 同一订单会在多次查询里重复出现，且状态会演进：保留**最后一次观测**
        ordersById.set(row.order_id, { ...(ordersById.get(row.order_id) ?? {}), ...row });
      }
      continue;
    }

    const label = actionLabel(name);
    if (label) {
      const ok = parsed.ok && !entry.is_error;
      actions.push({
        at: entry.at ?? null,
        action: label,
        order_id: parsed.ok ? (parsed.data?.order_id ?? null) : null,
        ok,
        detail: ok ? "" : (parsed.reason ?? "失败"),
        entry_id: entry.id ?? null,
      });
      if (!ok) errors += 1;
      continue;
    }

    // 其余一律视为查询：只计数，不逐条铺开
    queryTools[name || "unknown"] = (queryTools[name || "unknown"] ?? 0) + 1;
    if (entry.is_error) errors += 1;
  }

  // 用**我们自己记录到的**动作补全生命周期：撤单/改单成功过就标注出来。
  // 这仍然是事实（券商对该 order_id 返回了 success），不是对状态码枚举的猜测。
  const cancelled = new Set(actions.filter((row) => row.ok && row.action === "撤单" && row.order_id)
    .map((row) => row.order_id));
  const modified = new Map();
  for (const row of actions) {
    if (row.ok && row.action === "改单" && row.order_id) {
      modified.set(row.order_id, (modified.get(row.order_id) ?? 0) + 1);
    }
  }
  for (const order of ordersById.values()) {
    order.cancelled = cancelled.has(order.order_id);
    order.modified_count = modified.get(order.order_id) ?? 0;
  }

  const orders = [...ordersById.values()].sort(
    (a, b) => String(b.ordered_at ?? b.seen_at ?? "").localeCompare(String(a.ordered_at ?? a.seen_at ?? "")));

  return {
    orders,
    actions: actions.sort((a, b) => String(b.at ?? "").localeCompare(String(a.at ?? ""))),
    queries: {
      count: Object.values(queryTools).reduce((sum, value) => sum + value, 0),
      tools: Object.entries(queryTools).map(([tool, count]) => ({ tool, count }))
        .sort((a, b) => b.count - a.count),
    },
    counts: {
      responses: rows.length,
      order_responses: orderResponses,
      orders: orders.length,
      actions: actions.length,
      errors,
    },
    notice: "交易概要由 Harness 观察到的富途工具响应归纳而来，不是券商成交推送；"
      + "只读查询仅计数不列出。下单与撤单请在 Harness 会话中完成并确认。",
  };
}
