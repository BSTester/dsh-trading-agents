import { WorkbenchError } from "./store.js";
import { buildAuditChain } from "./audit.js";
import { ENDPOINTS } from "./endpoints.js";

// 结果缓存：面板是查看用途，不需要实时。这些接口背后是 python 子进程与富途调用，
// 每次切页签都重跑既慢又浪费额度，因此在本层做 TTL 缓存（默认值，按接口粒度）。
export const CACHE_TTL_MS = {
  instrument: 5 * 60_000,
  series: 5 * 60_000,
  equity: 2 * 60_000,
  positions: 2 * 60_000,
  correlation: 10 * 60_000,
  sensitivity: 30 * 60_000,
  risk: 5 * 60_000,
  trades: 60_000,
  events: 30 * 60_000,
  factors: 10 * 60_000,
  ic: 10 * 60_000,
  audit: 60_000,
  sources: 2 * 60_000,
  quality: 30 * 60_000,  // 财报变动不频繁
};

/** 稳定序列化：键顺序不影响缓存命中。 */
function stableKey(payload) {
  const keys = Object.keys(payload).sort();
  return JSON.stringify(keys.map((key) => [key, payload[key]]));
}

export function createTtlCache({ now = Date.now } = {}) {
  const store = new Map();
  return {
    read(key, ttl) {
      const hit = store.get(key);
      if (!hit || ttl <= 0 || now() - hit.at >= ttl) return null;
      return hit;
    },
    write(key, value) {
      const entry = { value, at: now() };
      store.set(key, entry);
      return entry;
    },
    clear() { store.clear(); },
  };
}

// Exact Connection Fetch routes share Harness authentication and its RPC envelope.
export function createRpcFetchHandler(store, endpoint, deps = {}) {
  const handle = createRpcHandler(store, deps);
  return async (request) => {
    if (request.headers.get("content-type")?.split(";")[0].trim() !== "application/json") {
      return new Response("Expected application/json", { status: 415 });
    }
    let body;
    try {
      body = await request.json();
    } catch (error) {
      if (error instanceof SyntaxError) return new Response("Invalid JSON", { status: 400 });
      throw error;
    }
    if (!body || body.type !== "client-request" || typeof body.rpcId !== "string"
        || body.method !== `trading-workbench/${endpoint}`
        || Object.keys(body).some(key => !["type", "rpcId", "method", "payload"].includes(key))) {
      return new Response("Invalid RPC envelope", { status: 400 });
    }
    return Response.json({ type: "server-response", rpcId: body.rpcId,
      result: await handle(endpoint, body.payload) });
  };
}

export function createRpcHandler(store, deps = {}) {
  const cache = deps.cache ?? createTtlCache(deps);

  /** 缓存包装：命中则直接返回，并把「数据算于何时」一并告知客户端。 */
  const cached = async (endpoint, payload, force, produce, errorCode) => {
    const ttl = CACHE_TTL_MS[endpoint] ?? 0;
    const key = `${endpoint}|${stableKey(payload)}`;
    if (!force && ttl > 0) {
      const hit = cache.read(key, ttl);
      if (hit) {
        return { ok: true, value: hit.value, cached: true, cached_at: new Date(hit.at).toISOString() };
      }
    }
    try {
      const value = await produce();
      const entry = ttl > 0 ? cache.write(key, value) : { at: Date.now() };
      return { ok: true, value, cached: false, cached_at: new Date(entry.at).toISOString() };
    } catch (error) {
      return { ok: false, error: { code: errorCode,
        message: String(error?.message ?? error).slice(0, 300), details: {} } };
    }
  };

  return async (endpoint, rawPayload) => {
    try {
      if (!rawPayload || typeof rawPayload !== "object" || Array.isArray(rawPayload)) {
        throw new WorkbenchError("Expected an object payload");
      }
      // `_refresh` 是客户端显式要求绕过缓存的旁路标记，不参与各接口的字段校验
      const { _refresh: forceRefresh, ...payload } = rawPayload;
      if (endpoint === "snapshot" && Object.keys(payload).length === 0) {
        // 向客户端声明本 Host 实际提供哪些接口，供其识别进程陈旧
        return { ok: true, value: { ...store.snapshot(), endpoints: ENDPOINTS } };
      }
      if (endpoint === "switch-mode") {
        if (Object.keys(payload).some(key => !["mode", "expected_mode", "confirmation"].includes(key))) {
          throw new WorkbenchError("Unexpected switch-mode field");
        }
        return { ok: true, value: store.switchMode(payload) };
      }
      if (endpoint === "equity" || endpoint === "positions" || endpoint === "correlation") {
        const allowed = endpoint === "correlation" ? ["tickers", "window"] : ["mode", "window"];
        if (Object.keys(payload).some(key => !allowed.includes(key))) {
          throw new WorkbenchError(`Unexpected ${endpoint} field`);
        }
        const provider = deps.analytics;
        if (!provider || typeof provider[endpoint] !== "function") {
          throw new WorkbenchError("Analytics provider unavailable");
        }
        try {
          return await cached(endpoint, payload, forceRefresh,
            () => provider[endpoint](payload, { refresh: forceRefresh === true }),
            "trading/analytics-unavailable");
        } catch (error) {
          return { ok: false, error: { code: "trading/analytics-unavailable",
            message: String(error?.message ?? error).slice(0, 300), details: {} } };
        }
      }
      if (endpoint === "audit") {
        if (Object.keys(payload).length !== 0) throw new WorkbenchError("audit takes no payload");
        const mode = store.snapshot().mode;
        let trades = { trades: [] };
        const provider = deps.analytics;
        if (provider && typeof provider.trades === "function") {
          try {
            trades = await provider.trades({ mode, limit: 100 });
          } catch {
            trades = { trades: [] };  // 台账不可读时仍给出信号/响应链路
          }
        }
        return { ok: true, value: buildAuditChain({ snapshot: store.snapshot(), trades }) };
      }
      if (endpoint === "sensitivity" || endpoint === "risk" || endpoint === "trades" || endpoint === "events" || endpoint === "factors" || endpoint === "ic" || endpoint === "sources" || endpoint === "instrument" || endpoint === "quality") {
        const allowed = endpoint === "sensitivity"
          ? ["ticker", "strategy", "metric", "fast_grid", "slow_grid", "buy_grid", "sell_grid", "start"]
          : endpoint === "trades" ? ["mode", "limit"]
          : endpoint === "events" ? ["ticker", "days"]
          : endpoint === "factors" ? ["tickers", "window"]
          : endpoint === "ic" ? ["tickers", "factor", "forward", "window"]
          : endpoint === "sources" ? ["no_probe"]
          : endpoint === "instrument" ? ["ticker"]
          : endpoint === "quality" ? ["ticker"] : [];
        if (Object.keys(payload).some(key => !allowed.includes(key))) {
          throw new WorkbenchError(`Unexpected ${endpoint} field`);
        }
        const provider = deps.analytics;
        if (!provider || typeof provider[endpoint] !== "function") {
          throw new WorkbenchError("Analytics provider unavailable");
        }
        try {
          return await cached(endpoint, payload, forceRefresh,
            () => provider[endpoint](payload, { refresh: forceRefresh === true }),
            "trading/analytics-unavailable");
        } catch (error) {
          return { ok: false, error: { code: "trading/analytics-unavailable",
            message: String(error?.message ?? error).slice(0, 300), details: {} } };
        }
      }
      if (endpoint === "series") {
        if (Object.keys(payload).some(key => !["ticker", "period", "limit"].includes(key))) {
          throw new WorkbenchError("Unexpected series field");
        }
        if (typeof deps.fetchSeries !== "function") throw new WorkbenchError("Series provider unavailable");
        try {
          return await cached(endpoint, payload, forceRefresh,
            () => deps.fetchSeries(payload), "trading/series-unavailable");
        } catch (error) {
          // 取数失败以可读原因返回，不抛出：面板需显示降级状态而不是空白。
          return { ok: false, error: { code: "trading/series-unavailable",
            message: String(error?.message ?? error).slice(0, 300), details: {} } };
        }
      }
      throw new WorkbenchError("Unknown workbench operation");
    } catch (error) {
      if (!(error instanceof WorkbenchError)) throw error;
      return { ok: false, error: { code: "trading/invalid-operation", message: error.message, details: {} } };
    }
  };
}
