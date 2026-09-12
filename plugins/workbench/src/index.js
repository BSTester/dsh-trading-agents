import { WorkbenchStore } from "./store.js";
import { createRpcFetchHandler } from "./rpc.js";

export const name = "trading-workbench";
export const inject = [];

export function apply(ctx) {
  const store = new WorkbenchStore();
  ctx.provide("tradingWorkbench", store);
  ctx.inject(["connection"], (apiCtx) => {
    for (const endpoint of ["snapshot", "switch-mode"]) {
      apiCtx.connection.fetch.register({
        path: `/api/trading-workbench/${endpoint}`,
        methods: ["POST"], requestBody: "buffered",
        fetch: createRpcFetchHandler(store, endpoint),
      });
    }
  });
}
