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
    // 先等子进程真正退出再 rm(home)：上一版 kill 后立刻删目录，进程可能还挂着
    // （server.close 等活动连接），rm 与退出赛跑留下临时目录/僵尸进程。
    if (child.exitCode === null && child.signalCode === null) {
      child.kill("SIGTERM");
      await Promise.race([
        new Promise((resolve) => child.once("exit", resolve)),
        new Promise((resolve) => setTimeout(resolve, 3000).unref()),
      ]);
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    }
    await rm(home, { recursive: true, force: true });
  });
  // stdout 按行缓冲：data 事件分片可能把一行 boot JSON 截成多段，按 \n 切完整行再解析
  const ready = new Promise((resolve, reject) => {
    let buffer = "";
    child.stdout.on("data", (chunk) => {
      buffer += String(chunk);
      const lines = buffer.split("\n");
      buffer = lines.pop();   // 保留最后不完整段，等下一个分片
      for (const line of lines) {
        if (line.startsWith("{")) {
          try { resolve(JSON.parse(line)); } catch (error) { reject(error); }
          return;   // boot JSON 只需解析一次
        }
      }
    });
    child.on("error", reject);
    child.on("exit", () => reject(new Error("服务进程提前退出")));
  });
  let bootTimer;
  const boot = await Promise.race([ready, new Promise((_, reject) => {
    bootTimer = setTimeout(() => reject(new Error("服务 10s 未就绪")), 10_000);
    bootTimer.unref();   // 超时兜底不得拖住测试进程退出
  })]).finally(() => clearTimeout(bootTimer));   // boot 即到：清掉 10s 定时器
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
