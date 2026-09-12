import { WorkbenchError } from "./store.js";

// Exact Connection Fetch routes share Harness authentication and its RPC envelope.
export function createRpcFetchHandler(store, endpoint) {
  const handle = createRpcHandler(store);
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

export function createRpcHandler(store) {
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
      throw new WorkbenchError("Unknown workbench operation");
    } catch (error) {
      if (!(error instanceof WorkbenchError)) throw error;
      return { ok: false, error: { code: "trading/invalid-operation", message: error.message, details: {} } };
    }
  };
}
