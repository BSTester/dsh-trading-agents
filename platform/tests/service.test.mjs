// HTTP 面单测（离线，临时 DSH_HOME）：白名单/方法/内容类型/token/静态兜底。
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, mkdir, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { WorkbenchStore } from "../../plugins/workbench/src/store.js";
import { createRpcHandler } from "../../plugins/workbench/src/rpc.js";
import { createService } from "../server/service.mjs";

async function withServer(t, { token = null, dist = null } = {}) {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-http-"));
  const previous = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  const store = new WorkbenchStore();
  const handle = createRpcHandler(store, {});
  const server = createService({ store, handle,
    mcp: async (req, res) => { res.writeHead(405); res.end(); },
    config: { token }, dist: dist ?? path.join(home, "empty-dist"),
    endpoints: ["snapshot", "series"] });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  t.after(async () => {
    await new Promise((resolve) => server.close(resolve));
    if (previous === undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME = previous;
    await rm(home, { recursive: true, force: true });
  });
  return { url, store };
}

test("snapshot 经 /api/wb 可用；未知端点 404 envelope", async (t) => {
  const { url } = await withServer(t);
  const ok = await fetch(`${url}/api/wb/snapshot`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const okBody = await ok.json();
  assert.equal(ok.status, 200);
  assert.equal(okBody.ok, true);
  assert.ok(Array.isArray(okBody.value.endpoints));
  const missing = await fetch(`${url}/api/wb/not-an-endpoint`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  assert.equal(missing.status, 404);
});

test("方法/内容类型/载荷校验：405/415/400", async (t) => {
  const { url } = await withServer(t);
  assert.equal((await fetch(`${url}/api/wb/snapshot`)).status, 405);
  assert.equal((await fetch(`${url}/api/wb/snapshot`, { method: "POST", body: "{}" })).status, 415);
  assert.equal((await fetch(`${url}/api/wb/snapshot`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{oops" })).status, 400);
});

test("token 配置后 API 需要 Bearer；healthz 豁免", async (t) => {
  const { url } = await withServer(t, { token: "s3cret" });
  assert.equal((await fetch(`${url}/api/wb/snapshot`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).status, 401);
  assert.equal((await fetch(`${url}/api/wb/snapshot`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer s3cret" }, body: "{}" })).status, 200);
  assert.equal((await fetch(`${url}/healthz`)).status, 200);
});

test("静态托管：dist 文件可取，SPA 兜底 index.html", async (t) => {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-dist-"));
  const dist = path.join(home, "dist");
  await mkdir(path.join(dist, "assets"), { recursive: true });
  await writeFile(path.join(dist, "index.html"), "<html>ok</html>");
  await writeFile(path.join(dist, "assets", "app.js"), "console.log(1)");
  const { url } = await withServer(t, { dist });
  const page = await fetch(`${url}/`);
  assert.equal(page.status, 200);
  assert.match(await page.text(), /ok/);
  const js = await fetch(`${url}/assets/app.js`);
  assert.equal(js.headers.get("content-type"), "text/javascript");
  const spa = await fetch(`${url}/some/client/route`);
  assert.equal(spa.status, 200);
  assert.match(await spa.text(), /ok/);
  const noDist = await fetch(`${url}/healthz`);
  assert.equal(noDist.status, 200);
  await rm(home, { recursive: true, force: true });
});

test("未构建 dist：静态请求 404 envelope（trading/not-found），服务不崩", async (t) => {
  const { url } = await withServer(t);
  const missing = await fetch(`${url}/some/route`);
  assert.equal(missing.status, 404);
  const body = await missing.json();
  assert.equal(body.error.code, "trading/not-found");
  assert.equal((await fetch(`${url}/healthz`)).status, 200);
});

test("超限请求体：413 envelope（不再是连接重置）", async (t) => {
  const { url } = await withServer(t);
  const big = "x".repeat(1024 * 1024 + 10);
  const response = await fetch(`${url}/api/wb/snapshot`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pad: big }) });
  assert.equal(response.status, 413);
  assert.equal((await response.json()).error.code, "trading/payload-too-large");
  assert.equal((await fetch(`${url}/healthz`)).status, 200);   // 服务存活
});
