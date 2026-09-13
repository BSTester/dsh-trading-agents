import { WorkbenchError } from "./store.js";

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
  return async (endpoint, payload) => {
    try {
      if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        throw new WorkbenchError("Expected an object payload");
      }
      if (endpoint === "snapshot" && Object.keys(payload).length === 0) {
        return { ok: true, value: store.snapshot() };
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
          return { ok: true, value: await provider[endpoint](payload) };
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
          return { ok: true, value: await deps.fetchSeries(payload) };
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
