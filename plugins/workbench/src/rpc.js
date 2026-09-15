import { CONFIRM_TTL_MS, WorkbenchError } from "./store.js";
import { buildAuditChain } from "./audit.js";
import { ENDPOINTS, matchesShape } from "./endpoints.js";
import { createTtlCache } from "./cache.js";
import { createCoreBridge } from "./corebridge.js";
import { writeCommand } from "./commandbus.js";
import { pythonHome } from "./pycore.js";

// 结果缓存：面板是查看用途，不需要实时。这些接口背后是 python 子进程与富途调用，
// 每次切页签都重跑既慢又浪费额度，因此在本层做 TTL 缓存（默认值，按接口粒度）。
export const CACHE_TTL_MS = {
  // 面板是**查看用途**，不追求实时（用户明确要求"可以是本地缓存的数据，避免频繁调用"）。
  // 这些数字直接决定"切页签会不会重新等"——冷启动实测 factors 25s、correlation 7s。
  instrument: 10 * 60_000,
  series: 10 * 60_000,
  equity: 5 * 60_000,
  positions: 5 * 60_000,
  correlation: 30 * 60_000,
  sensitivity: 60 * 60_000,
  risk: 15 * 60_000,
  trades: 5 * 60_000,
  events: 60 * 60_000,
  factors: 30 * 60_000,
  ic: 30 * 60_000,
  audit: 2 * 60_000,
  sources: 5 * 60_000,
  quality: 60 * 60_000,
  // WP4：调度态是低频变化的面板数据；心跳/作业给短 TTL，对账给中等 TTL
  plan: 60_000,
  schedule: 30_000,
  reconcile: 5 * 60_000,
};
/** 稳定序列化：键顺序不影响缓存命中。 */
function stableKey(payload) {
  const keys = Object.keys(payload).sort();
  return JSON.stringify(keys.map((key) => [key, payload[key]]));
}

export { createTtlCache } from "./cache.js";

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
  const bridge = deps.corebridge ?? createCoreBridge();
  const bus = deps.commandBus ?? { writeCommand };
  // plan-execute 的动作 → 白名单指令类型映射（规格 §8.2，5 种）
  const EXECUTE_ACTIONS = { execute: "execute_plan", cancel: "cancel_plan",
    kill: "kill", unkill: "unkill" };

  /** 缓存包装：命中则直接返回，并把「数据算于何时」一并告知客户端。 */
  const cached = async (endpoint, payload, force, produce, errorCode) => {
    const ttl = CACHE_TTL_MS[endpoint] ?? 0;
    const key = `${endpoint}|${stableKey(payload)}`;
    if (!force && ttl > 0) {
      const hit = cache.read(key, ttl);
      // 命中也要校验形状：坏条目（空对象、截断结果）当作未命中并重新取数，
      // 否则界面会把「取数失败」展示成「账户没有持仓」。
      if (hit && matchesShape(endpoint, hit.value)) {
        return { ok: true, value: hit.value, cached: true, cached_at: new Date(hit.at).toISOString() };
      }
    }
    try {
      const value = await produce();
      if (!matchesShape(endpoint, value)) {
        throw new Error(`${endpoint} 返回的载荷不完整，已按失败处理`);
      }
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
      if (endpoint === "confirmation") {
        if (Object.keys(payload).length !== 0) throw new WorkbenchError("confirmation takes no payload");
        // 内存态直读，不进缓存：缓存住"待确认"会让界面拿到已经处理掉的请求
        return { ok: true, value: { pending: store.confirmationView(),
          ttl_ms: CONFIRM_TTL_MS } };
      }
      if (endpoint === "confirm-decide") {
        if (Object.keys(payload).some(key => !["id", "decision"].includes(key))) {
          throw new WorkbenchError("Unexpected confirm-decide field");
        }
        // 这是唯一能批准实盘操作的通道；载荷只有编号与结论，没有下单参数
        return { ok: true, value: store.decideConfirmation(payload) };
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
      if (endpoint === "plan" || endpoint === "schedule" || endpoint === "reconcile") {
        // 只读端点：经 pycore 子命令取 trading-core 快照，Host 不直读 SQLite。
        // plan 额外并入账户模式（live 口令门槛由 plan-execute 分支复核）。
        if (Object.keys(payload).length !== 0) {
          throw new WorkbenchError(`${endpoint} takes no payload`);
        }
        const produce = endpoint === "plan"
          ? async () => ({ ...(await bridge.plan()), mode: store.snapshot().mode })
          : () => bridge[endpoint]();
        return await cached(endpoint, payload, forceRefresh, produce, "trading/core-unavailable");
      }
      if (endpoint === "plan-execute") {
        // 唯一受约束执行入口（规格 §8.3 边界变更）：校验 → 原子写指令文件即返回，
        // 不等待执行结果；执行状态由 plan 端点轮询。live 必须口令「确认执行」。
        if (Object.keys(payload).some(key => !["plan_hash", "expected_mode", "confirmation", "action"].includes(key))) {
          throw new WorkbenchError("Unexpected plan-execute field");
        }
        const action = payload.action ?? "execute";
        const type = EXECUTE_ACTIONS[action];
        if (!type) throw new WorkbenchError(`Unknown plan-execute action: ${action}`);
        const commandPayload = {};
        if (type === "execute_plan") {
          if (typeof payload.plan_hash !== "string" || !payload.plan_hash) {
            throw new WorkbenchError("plan-execute requires plan_hash");
          }
          if (payload.expected_mode !== store.snapshot().mode) {
            throw new WorkbenchError(`模式已变化：期望 ${payload.expected_mode}，当前 ${store.snapshot().mode}，请刷新后重试`);
          }
          if (store.snapshot().mode === "live" && payload.confirmation !== "确认执行") {
            throw new WorkbenchError("实时账户执行需输入口令「确认执行」");
          }
          commandPayload.plan_hash = payload.plan_hash;
          commandPayload.expected_mode = payload.expected_mode;
        } else if (type === "cancel_plan" && typeof payload.plan_hash === "string") {
          commandPayload.plan_hash = payload.plan_hash;
        }
        const nonce = await bus.writeCommand(pythonHome(), type, commandPayload);
        return { ok: true, value: { queued: true, nonce, action } };
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
