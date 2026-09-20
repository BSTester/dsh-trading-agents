// V3 工作台「统一市场上下文」（A股 SH / 港股 HK / 美股 US）。
//
// 为什么要有它：市场是**跨页的同一件事实**。行情页的三市场切换、策略页的标的池、风险页的
// 组合口径、执行页的台账/成交质量、研究页的研报归属，必须是同一个市场；否则同屏会出现
// 两个互相矛盾的市场事实源（本页 SH、那张卡 HK）。
//
// 契约：
//   MARKETS          [{ key, label }]（顺序即页头顺序：A股 / 港股 / 美股）
//   MarketProvider   状态持久化在 localStorage["trading_market"]，默认 "SH"
//   useMarket()      → { market, setMarket, label }
//   MarketPicker     页头用的 Segmented（size=small，居中不抢焦）
//   MarketNote       受市场影响的卡片上的一行口径标注（「当前市场：A股 SH · 来源 …」）
//
// 只读：本模块不发起任何请求，只保存「当前看哪个市场」这一个用户选择。
import React from "react";
import { Segmented, Typography } from "antd";
import { fmt } from "./api.js";

const { Text } = Typography;

/** 三市场（顺序即页头 / 页内 Segmented 顺序）。 */
export const MARKETS = [
  { key: "SH", label: "A股" },
  { key: "HK", label: "港股" },
  { key: "US", label: "美股" },
];

/** 持久化键：与既有 localStorage 习惯一致（trading_token 同前缀）。 */
export const MARKET_STORAGE_KEY = "trading_market";
const DEFAULT_MARKET = "SH";

/** 各市场默认标的（K 线 / 盘口 / 回测的默认取值，真实代码；无池子时用它兜底而不是编造）。 */
const DEFAULT_TICKERS = { SH: "SH.600000", HK: "HK.00700", US: "US.NVDA" };

/** 市场标记（含交易所后缀的展示形态，如「A股 SH」）。 */
const MARKET_LABELS = { SH: "A股 SH", HK: "港股 HK", US: "美股 US" };

/** 归一化：任何输入都落到三市场之一（未知值按默认市场处理，绝不产生第四档「全部」）。 */
export function normalizeMarket(value) {
  const key = String(value ?? "").toUpperCase();
  return MARKETS.some((item) => item.key === key) ? key : DEFAULT_MARKET;
}

/** key → { key, label }（label 为「A股」这种短名）。 */
export function marketInfo(value) {
  const key = normalizeMarket(value);
  return MARKETS.find((item) => item.key === key) || MARKETS[0];
}

/** key → 带市场后缀的展示名（「A股 SH」）。 */
export function marketLabel(value) {
  return MARKET_LABELS[normalizeMarket(value)] || MARKET_LABELS[DEFAULT_MARKET];
}

/** key → 该市场默认标的（真实代码，不含占位）。 */
export function marketTicker(value) {
  return DEFAULT_TICKERS[normalizeMarket(value)] || DEFAULT_TICKERS[DEFAULT_MARKET];
}

/** 标的 → 其所属市场口径（SH.600000 → SH；无法判定返回 null，不臆测）。
 *
 *  与后端口径逐字一致（platform/server/v3_universe.py）：**A 股 = SH**，
 *  ``SH.`` / ``SZ.`` / ``BJ.`` 三个前缀都归入 ``SH``（深市、北交所与沪市同属 A 股口径），
 *  ``HK.`` → HK，``US.`` → US。裸代码（如 ``00700``）无法可靠判定，返回 null。 */
const PREFIX_TO_MARKET = { SH: "SH", SZ: "SH", BJ: "SH", HK: "HK", US: "US" };

export function tickerMarket(ticker) {
  const prefix = String(ticker ?? "").trim().toUpperCase().split(".")[0];
  return PREFIX_TO_MARKET[prefix] || null;
}

/** 读 localStorage：只有合法值才认，读不到/被禁用一律默认 SH。 */
function readStoredMarket() {
  try {
    const stored = window.localStorage.getItem(MARKET_STORAGE_KEY);
    if (stored && MARKETS.some((item) => item.key === String(stored).toUpperCase())) {
      return String(stored).toUpperCase();
    }
  } catch {
    /* localStorage 不可用（隐私模式等）：按默认市场处理 */
  }
  return DEFAULT_MARKET;
}

const MarketContext = React.createContext(null);

/** 统一市场上下文：切市场 → 所有订阅页面把 market 放进 useV3 的 deps 自动重取。 */
export function MarketProvider({ children }) {
  const [market, setMarketState] = React.useState(readStoredMarket);

  const setMarket = React.useCallback((next) => {
    const key = normalizeMarket(next);
    setMarketState(key);
    try {
      window.localStorage.setItem(MARKET_STORAGE_KEY, key);
    } catch {
      /* 写不进去不影响本次会话内的切换 */
    }
  }, []);

  const value = React.useMemo(() => {
    const info = marketInfo(market);
    return { market: info.key, setMarket, label: info.label };
  }, [market, setMarket]);

  return <MarketContext.Provider value={value}>{children}</MarketContext.Provider>;
}

// 兜底：万一有组件被挂在 Provider 之外（例如区块级错误边界重挂子树），
// 仍给出可用的默认上下文而不是抛错白屏。Provider 之外无法跨组件共享选择，
// 因此这条路径只读默认市场、切换为空操作（页面源码中没有任何页面依赖它）。
const FALLBACK_CONTEXT = {
  market: DEFAULT_MARKET,
  setMarket: () => {},
  label: marketInfo(DEFAULT_MARKET).label,
};

/** 订阅当前市场：{ market, setMarket, label }。页面把 market 放进 useV3 的 deps 即可自动重取。 */
export function useMarket() {
  const context = React.useContext(MarketContext);
  return context || FALLBACK_CONTEXT;
}

/** 页头市场选择器：size=small，与 SIM 徽章 / 数据时点同一行，居中不抢焦。 */
export function MarketPicker() {
  const { market, setMarket, label } = useMarket();
  return (
    <Segmented
      size="small"
      value={market}
      onChange={(value) => setMarket(value)}
      options={MARKETS.map((item) => ({ value: item.key, label: item.label }))}
      aria-label={`市场切换（当前 ${label} ${market}）`}
      title={`统一市场上下文：当前 ${label} ${market}；切换后各页按该市场重新取数`}
    />
  );
}

/**
 * 受市场影响的卡片上的一行口径标注：「当前市场：A股 SH · 来源 … · as_of … · 附加说明」。
 * 页面文字一律来自它，避免各页自造第三种说法。
 */
export function MarketNote({ source, asOf, extra, style }) {
  const { market, label } = useMarket();
  const parts = [`当前市场：${label} ${market}`];
  if (source) parts.push(`来源 ${source}`);
  if (asOf !== undefined && asOf !== null && asOf !== "") parts.push(`as_of ${fmt.stamp(asOf)}`);
  if (extra) parts.push(String(extra));
  return (
    <Text type="secondary" style={{ fontSize: 11, ...(style || {}) }}>
      {parts.join(" · ")}
    </Text>
  );
}

/* ── 失败展示的统一口径（error.code / error.message / **error.detail**）─────────
 * 服务端对「该市场没有池子」这类失败给的是**冻结文案** error.message（「该市场没有配置自选池、
 * 也没有真实持仓」），**真实原因放在 error.detail**（富途限频 -12006、positions 取数失败、
 * 上游客服端原文…）。只显示 message 会把「富途限频」误读成「这个市场没有池子」，属于误导性展示。
 * 因此页面失败展示统一为：`{code}：{message}` + 有 detail 时追加「 · 真实原因：{detail}」。
 */

/** detail 可能是字符串 / 数组 / 对象 / 数字：安全字符串化并截断（绝不抛错，也不铺开整对象）。 */
export const ERROR_DETAIL_MAX = 240;

function detailLeaf(value, depth = 0) {
  if (value === null || value === undefined) return "";
  const type = typeof value;
  if (type === "string") return value;
  if (type === "number" || type === "boolean") return String(value);
  if (type !== "object" || depth >= 3) return "";
  if (Array.isArray(value)) {
    return value.map((item) => detailLeaf(item, depth + 1)).filter(Boolean).join("；");
  }
  // 常见承载字段优先，避免把整对象铺开成一行 JSON
  for (const key of ["detail", "reason", "message", "error", "note", "hint"]) {
    const nested = detailLeaf(value[key], depth + 1);
    if (nested) return nested;
  }
  try {
    const text = JSON.stringify(value);
    // 空对象/空数组不是「原因」，不追加「真实原因：{}」这种噪音
    return text === "{}" || text === "[]" || text === "null" ? "" : text;
  } catch {
    return "";
  }
}

/** error.detail（或 error.details）→ 可展示的单行文本；没有则返回空串。 */
export function errorDetailText(error) {
  const raw = error && typeof error === "object" ? (error.detail ?? error.details) : null;
  const text = String(detailLeaf(raw) || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  return text.length > ERROR_DETAIL_MAX ? `${text.slice(0, ERROR_DETAIL_MAX)}…` : text;
}

/**
 * 信封失败文案：`{code}：{message}`（原样取服务端 code/message），有 detail 时追加
 * 「 · 真实原因：{detail}」——detail 才是「为什么」，不能被 message 的冻结文案掩盖。
 */
export function envelopeError(env, fallback = "接口未返回 error.code/message") {
  const error = (env && env.error) || {};
  const code = error.code ? String(error.code) : "";
  const message = error.message ? String(error.message) : "";
  const head = code && message ? `${code}：${message}` : (message || code || fallback);
  const detail = errorDetailText(error);
  return detail ? `${head} · 真实原因：${detail}` : head;
}

export default MarketProvider;
