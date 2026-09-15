// MCP 面（规格 §3.6/§5.2 S1-S3 in-process 版）：SDK 客户端 initialize → tools/list=25 → 调用与通道分级。
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { WorkbenchStore } from "../../plugins/workbench/src/store.js";
import { createRpcHandler } from "../../plugins/workbench/src/rpc.js";
import { createService } from "../server/service.mjs";
import { createMcpEndpoint } from "../server/mcp.mjs";
import { buildManifest, TOOL_COUNT } from "../server/manifest.mjs";

async function withMcpServer(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-mcp-"));
  const previous = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  const store = new WorkbenchStore();
  const handle = createRpcHandler(store, {});
  const manifest = buildManifest({ handle, store });
  const mcp = createMcpEndpoint({ manifest });
  const server = createService({ store, handle, mcp, config: {}, dist: path.join(home, "no-dist"),
    endpoints: ["snapshot", "series", "switch-mode", "plan-execute"] });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}/mcp`;
  t.after(async () => {
    await new Promise((resolve) => server.close(resolve));
    if (previous === undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME = previous;
    await rm(home, { recursive: true, force: true });
  });
  return { url, store };
}

async function connect(url) {
  const client = new Client({ name: "wp6-test", version: "0.0.0" });
  await client.connect(new StreamableHTTPClientTransport(new URL(url)));
  return client;
}

test("S1 initialize + tools/list 恰 25 且与 manifest 一致", async (t) => {
  const { url } = await withMcpServer(t);
  const client = await connect(url);
  t.after(() => client.close());
  const tools = await client.listTools();
  assert.equal(tools.tools.length, TOOL_COUNT);
  assert.ok(tools.tools.some((tool) => tool.name === "plan_execute"));
  assert.ok(tools.tools.every((tool) => tool.name !== "exec"));
});

test("S2 snapshot 可调；switch_mode 的 live 通道被封死（带口令也拒）", async (t) => {
  const { url, store } = await withMcpServer(t);
  const client = await connect(url);
  t.after(() => client.close());
  const snapshot = await client.callTool({ name: "snapshot", arguments: {} });
  assert.equal(snapshot.isError, false);
  const value = JSON.parse(snapshot.content[0].text);
  assert.equal(value.ok, true);
  const live = await client.callTool({ name: "switch_mode",
    arguments: { mode: "live", expected_mode: "sim", confirmation: "确认实盘" } });
  assert.equal(JSON.parse(live.content[0].text).error.code, "trading/live-switch-web-only");
  assert.equal(store.readMode(), "sim");
});

test("S3 业务失败走 ok:false envelope（isError=false）；HTTP 与 MCP snapshot 同值", async (t) => {
  const { url } = await withMcpServer(t);
  const client = await connect(url);
  t.after(() => client.close());
  const bad = await client.callTool({ name: "series", arguments: { ticker: "BAD TICKER!" } });
  assert.equal(bad.isError, false);
  const envelope = JSON.parse(bad.content[0].text);
  assert.equal(envelope.ok, false);
  const viaMcp = await client.callTool({ name: "snapshot", arguments: {} });
  const http = await fetch(url.replace(/\/mcp$/, "/api/wb/snapshot"),
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  // snapshot 含 generated_at（每次调用都变），同值断言比较稳定字段
  const viaMcpValue = JSON.parse(viaMcp.content[0].text).value;
  const httpValue = (await http.json()).value;
  assert.equal(viaMcpValue.mode, httpValue.mode);
  assert.deepEqual(viaMcpValue.endpoints, httpValue.endpoints);
});
