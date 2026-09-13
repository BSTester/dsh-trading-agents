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

test("workbench ships a Futu instrument card and a cached K-line chart, with no sample data", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  // K 线图已于 2026-09-13 按用户要求加回（此前一度被移除）；标的卡片保留
  assert.match(source, /function KLineChart\(/, "缺少 K 线图组件");
  assert.match(source, /function KLineCard\(/, "缺少 K 线卡片");
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

test("K 线周期预设：默认日线，根数不超过富途单次上限", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const periods = [...source.matchAll(/\{ id: "([^"]+)", label: "([^"]+)", limit: (\d+) \}/g)]
    .map(([, id, label, limit]) => ({ id, label, limit: Number(limit) }));
  assert.ok(periods.length >= 3, "周期预设过少");
  assert.equal(periods[0].id, "1d", "默认应为日线");
  const defaultPeriod = source.match(/const KLINE_DEFAULT_PERIOD = "([^"]+)"/)?.[1];
  assert.equal(defaultPeriod, "1d", "默认周期必须是日线");
  for (const row of periods) {
    assert.ok(row.limit >= 20 && row.limit <= 370,
      `${row.id} 根数 ${row.limit} 超出富途单次上限（20..370）`);
  }
  assert.ok(periods.some((p) => p.id === "1d"), "缺少日线");
  assert.ok(periods.some((p) => ["1m", "5m"].includes(p.id)), "缺少分钟级");
});

test("K 线按需挂载：未解析标的时不请求 series", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  // KLineCard 只在 submitted 非空时挂载，避免空 ticker 触发无意义请求
  assert.match(source, /submitted && h\(KLineCard/,
    "K 线卡片应在标的解析后才挂载");
});

test("K 线也会走缓存，不会因切页签重复取数", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const ttl = source.match(/series: (\d+) \* 60_000/);
  assert.ok(ttl, "series 未配置客户端 TTL，切页签会重复取数");
  assert.ok(Number(ttl[1]) >= 1, "series TTL 过短");
});
