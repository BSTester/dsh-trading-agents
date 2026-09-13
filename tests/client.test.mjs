import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

async function client() {
  let definition;
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  vm.runInNewContext(source, { window: { __ModuleLoader__: { load: value => { definition = value; } } } });
  const React = { createElement: (tag, props, ...children) => ({ tag, props, children }) };
  return definition.factory(name => {
    assert.equal(name, "react");
    return React;
  });
}

test("native client factory contributes overlay and report cards without a second chat", async () => {
  const plugin = await client();
  const slots = [];
  plugin.apply({ connection: { rpc: { call() {} } },
    slots: { inject: (_name, fn) => fn(), register: (options, component) => slots.push({ options, component }) } });
  assert.equal(slots.filter(row => row.options.name === "shell.overlay").length, 1);
  assert.equal(slots.some(row => row.options.key === "research_publish"), true);
  assert.equal(slots.some(row => row.options.name.includes("input")), false);
  const card = slots.find(row => row.options.key === "research_publish").component;
  const tree = card({ block: { kind: "tool-result", content: [{ type: "text", text: "研究结果" }] } });
  assert.match(JSON.stringify(tree), /研究结果/);
});

test("RPC client sends only the narrow workbench channel and surfaces host errors", async () => {
  const plugin = await client();
  const calls = [];
  const rpc = { call: async (...args) => {
    calls.push(args);
    return { ok: false, error: { message: "refresh first" } };
  } };
  await assert.rejects(plugin.request(rpc, "snapshot", {}), /refresh first/);
  assert.equal(calls[0][0], "/api");
  assert.equal(calls[0][1], "trading-workbench/snapshot");
  await assert.rejects(plugin.request(rpc, "execute-order", {}), /Unsupported/);
  assert.equal(calls.length, 1);
});

test("workbench client ships pagination, report detail, and drawer UI", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  assert.match(source, /function Paged\(/, "缺少通用分页组件");
  assert.match(source, /function ReportDetail\(/, "缺少研报详情视图");
  assert.match(source, /onOpenReport/, "研报标题未接线到详情");
  assert.match(source, /tw-pager/, "分页样式缺失");
  assert.match(source, /tw-drawer/, "抽屉容器缺失");
  assert.match(source, /在新标签打开/, "缺少新标签打开入口");
  // 所有列表类视图必须走 Paged，避免出现无分页的超长列表
  for (const view of ["ResearchView", "SignalView", "ExecutionView", "AuditView", "EventsView"]) {
    const body = source.slice(source.indexOf(`function ${view}(`));
    const end = body.indexOf("\n    function ", 10);
    const text = end === -1 ? body : body.slice(0, end);
    assert.match(text, /h\(Paged,/, `${view} 未使用分页`);
  }
});

test("workbench shows a Futu instrument card, not charts, and ships no sample data", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /KLineChart/, "不应再内置 K 线图组件");
  assert.match(source, /在富途查看K线/, "缺少跳转富途看 K 线的入口");
  assert.match(source, /instrument/, "未使用标的卡片接口");
  // 不得内置示例标的（默认标的池/默认行情标的）
  for (const code of ["600519", "000001", "601318", "600036", "300750"]) {
    assert.ok(!source.includes(`"${code}"`), `仍存在示例标的 ${code}`);
  }
  assert.match(source, /useState\(""\)/, "行情标的默认值应为空");
  assert.match(source, /useState\(\[\]\)/, "标的池默认值应为空");
});

test("客户端会声明式跳过 Host 未提供的接口，而不是撞 404", async () => {
  const plugin = await client();
  const calls = [];
  const rpc = { call: async (...args) => {
    calls.push(args);
    // Host 声明：只有 snapshot 与 switch-mode（模拟进程陈旧的旧版 Host）
    return { ok: true, value: { mode: "sim", endpoints: ["snapshot", "switch-mode"] } };
  } };
  await plugin.request(rpc, "snapshot", {});
  assert.equal(plugin.internals.servedEndpoints().has("positions"), false);
  const before = calls.length;
  await assert.rejects(plugin.request(rpc, "positions", { mode: "sim" }), /Host 未提供 positions 接口/);
  assert.match(await plugin.request(rpc, "positions", { mode: "sim" }).catch((e) => e.message), /重启 dsh web/);
  assert.equal(calls.length, before, "已知缺失的接口不应再发起请求");
});

test("Host 未声明接口清单时不拦截（向后兼容旧 Host）", async () => {
  const plugin = await client();
  const calls = [];
  const rpc = { call: async (...args) => { calls.push(args); return { ok: true, value: {} }; } };
  await plugin.request(rpc, "positions", { mode: "sim" });
  assert.equal(calls.length, 1);
});

test("客户端与 Host 的接口清单必须一致", async () => {
  const { ENDPOINTS } = await import("../plugins/workbench/src/endpoints.js");
  const plugin = await client();
  assert.deepEqual([...plugin.internals.KNOWN_ENDPOINTS].sort(), [...ENDPOINTS].sort(),
    "两侧接口清单漂移会让客户端调用不存在的路由");
});

test("面板结果会缓存，避免切页签时重复取数", async () => {
  const plugin = await client();
  const { readCache, writeCache, invalidateCaches, cacheSize } = plugin.internals;
  const payload = { ticker: "600519" };
  assert.equal(readCache("instrument", payload), null);
  writeCache("instrument", payload, { price: 1 });
  assert.deepEqual(readCache("instrument", payload).value, { price: 1 });
  // 不同参数是不同缓存条目
  assert.equal(readCache("instrument", { ticker: "00700.HK" }), null);
  invalidateCaches();
  assert.equal(readCache("instrument", payload), null);
  assert.equal(cacheSize(), 0);
});

test("不缓存的接口不会被写入缓存", async () => {
  const plugin = await client();
  const { readCache, writeCache } = plugin.internals;
  writeCache("snapshot", {}, { mode: "sim" });
  assert.equal(readCache("snapshot", {}), null, "snapshot 走轮询，不应进缓存");
});

test("旧版 Host 不声明清单时，从 404 反推并停止重试", async () => {
  const plugin = await client();
  const calls = [];
  const rpc = { call: async (...args) => {
    calls.push(args);
    if (String(args[1]).endsWith("/snapshot")) return { ok: true, value: { mode: "sim" } };  // 旧 Host：无 endpoints
    throw new Error("transport failure for /api/trading-workbench/positions: HTTP 404");
  } };
  await plugin.request(rpc, "snapshot", {});
  await assert.rejects(plugin.request(rpc, "positions", { mode: "sim" }), /Host 未提供 positions 接口/);
  assert.deepEqual([...plugin.internals.missingEndpoints()], ["positions"]);
  const before = calls.length;
  // 第二次不再发请求，错误信息保持一致
  await assert.rejects(plugin.request(rpc, "positions", { mode: "sim" }), /重启 dsh web/);
  assert.equal(calls.length, before, "已知缺失的接口不应重复请求");
});

test("业务失败不会被误判为路由缺失", async () => {
  const plugin = await client();
  const rpc = { call: async () => { throw new Error("trading/analytics-unavailable: 台账不可读"); } };
  await assert.rejects(plugin.request(rpc, "positions", { mode: "sim" }), /台账不可读/);
  assert.deepEqual([...plugin.internals.missingEndpoints()], []);
});

test("刷新会清掉路由缺失记忆，允许重启后的 Host 恢复", async () => {
  const plugin = await client();
  let phase = "old";
  const rpc = { call: async (...args) => {
    if (String(args[1]).endsWith("/snapshot")) return { ok: true, value: { mode: "sim" } };
    if (phase === "old") throw new Error("transport failure for /x: HTTP 404");
    return { ok: true, value: { positions: [] } };
  } };
  await plugin.request(rpc, "snapshot", {});
  await assert.rejects(plugin.request(rpc, "positions", { mode: "sim" }), /Host 未提供/);
  phase = "new";
  plugin.internals.invalidateCaches();
  assert.deepEqual((await plugin.request(rpc, "positions", { mode: "sim" })).positions, []);
});
