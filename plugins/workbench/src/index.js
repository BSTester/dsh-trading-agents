import { WorkbenchStore } from "./store.js";
import { createRpcFetchHandler } from "./rpc.js";
import { createSeriesProvider } from "./series.js";
import { createAnalyticsProvider } from "./analytics.js";

export const name = "trading-workbench";
export const inject = [];

export function apply(ctx) {
  const store = new WorkbenchStore();
  ctx.provide("tradingWorkbench", store);

  // 图表数据通道：K 线序列按需拉取（Host 侧校验 + 短时缓存）。
  const deps = { fetchSeries: createSeriesProvider(), analytics: createAnalyticsProvider() };

  ctx.inject(["connection"], (apiCtx) => {
    for (const endpoint of ["snapshot", "switch-mode", "series", "equity", "positions", "correlation", "sensitivity", "risk", "trades", "events"]) {
      apiCtx.connection.fetch.register({
        path: `/api/trading-workbench/${endpoint}`,
        methods: ["POST"], requestBody: "buffered",
        fetch: createRpcFetchHandler(store, endpoint, deps),
      });
    }
  });
}
