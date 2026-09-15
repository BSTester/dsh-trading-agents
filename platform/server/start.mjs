#!/usr/bin/env node
// 量化平台独立服务组装入口（规格 §二）：
//   store+deps → createRpcHandler（一份）→ HTTP API + MCP + 静态托管。
// 启动：node platform/server/start.mjs；配置：~/.dsh/trading-platform.json 的 service 节。
import { WorkbenchStore } from "../../plugins/workbench/src/store.js";
import { createRpcHandler } from "../../plugins/workbench/src/rpc.js";
import { createSeriesProvider } from "../../plugins/workbench/src/series.js";
import { createAnalyticsProvider } from "../../plugins/workbench/src/analytics.js";
import { ENDPOINTS } from "../../plugins/workbench/src/endpoints.js";
import { loadConfig } from "./config.mjs";
import { createService } from "./service.mjs";
import { createMcpEndpoint } from "./mcp.mjs";
import { buildManifest } from "./manifest.mjs";

const config = loadConfig();
const store = new WorkbenchStore();
const deps = { fetchSeries: createSeriesProvider(), analytics: createAnalyticsProvider() };
const handle = createRpcHandler(store, deps);   // 一份 handler：HTTP 与 MCP 共享（缓存也共享）
const manifest = buildManifest({ handle, store });
const server = createService({ store, handle, mcp: createMcpEndpoint({ manifest }), config, endpoints: ENDPOINTS });

server.listen(config.port, config.host, () => {
  const { port } = server.address();
  console.log(JSON.stringify({
    ok: true, service: "quant-platform",
    url: `http://${config.host}:${port}`,
    mcp: `http://${config.host}:${port}/mcp`,
    tools: manifest.length,
    auth: config.token ? "token" : "loopback-only",
  }, null, 0));
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
