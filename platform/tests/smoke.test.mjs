// 进程级冒烟（规格 §5.2 S 系列）：spawn 真实 start.mjs（临时 DSH_HOME）→ SDK 客户端全链路。
import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

test("S1-S4 真实进程：initialize/tools=25/通道分级/HTTP 同值", async (t) => {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-smoke-"));
  const child = spawn(process.execPath, ["server/start.mjs"], {
    cwd: path.resolve(import.meta.dirname, ".."),
    env: { ...process.env, DSH_HOME: home, TRADING_SERVICE_PORT: "0" },
    stdio: ["ignore", "pipe", "inherit"],
  });
  t.after(async () => {
    child.kill("SIGTERM");
    await rm(home, { recursive: true, force: true });
  });
  const ready = new Promise((resolve, reject) => {
    child.stdout.on("data", (chunk) => {
      const line = String(chunk).split("\n").find((row) => row.startsWith("{"));
      if (line) { try { resolve(JSON.parse(line)); } catch (error) { reject(error); } }
    });
    child.on("exit", () => reject(new Error("服务进程提前退出")));
  });
  const boot = await Promise.race([ready, new Promise((_, reject) =>
    setTimeout(() => reject(new Error("服务 10s 未就绪")), 10_000))]);
  assert.equal(boot.ok, true);
  assert.equal(boot.tools, 25);

  const client = new Client({ name: "wp6-smoke", version: "0.0.0" });
  await client.connect(new StreamableHTTPClientTransport(new URL(boot.mcp)));
  t.after(() => client.close());
  const tools = await client.listTools();
  assert.equal(tools.tools.length, 25);

  const snapshot = await client.callTool({ name: "snapshot", arguments: {} });
  assert.equal(JSON.parse(snapshot.content[0].text).ok, true);

  const live = await client.callTool({ name: "switch_mode",
    arguments: { mode: "live", expected_mode: "sim", confirmation: "确认实盘" } });
  assert.equal(JSON.parse(live.content[0].text).error.code, "trading/live-switch-web-only");

  // plan_execute 只排队不校验计划存在（校验在 daemon 执行链）；断言 queued 业务成功
  const queued = await client.callTool({ name: "plan_execute",
    arguments: { plan_hash: "nope", expected_mode: "sim", confirmation: "确认执行" } });
  assert.equal(JSON.parse(queued.content[0].text).value.queued, true);

  const http = await fetch(`${boot.url}/api/wb/snapshot`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const viaMcp = await client.callTool({ name: "snapshot", arguments: {} });
  // snapshot 含 generated_at（每次调用都变），同值断言比较稳定字段
  assert.equal((await http.json()).value.mode, JSON.parse(viaMcp.content[0].text).value.mode);
});
