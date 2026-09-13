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
