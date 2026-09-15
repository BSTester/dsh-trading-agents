// WP6 锁定测试（规格 §3.7）：工具面总数/名单、端点对等、能力黑名单、服务常量。
import test from "node:test";
import assert from "node:assert/strict";
import { ENDPOINT_TOOLS, ADMIN_TOOLS, TOOL_COUNT, TOOL_NAME_BLACKLIST } from "../server/manifest.mjs";
import { ENDPOINTS } from "../../plugins/workbench/src/endpoints.js";
import { DEFAULTS } from "../server/config.mjs";

test("wp6 工具面：恰 25 个工具（20 端点 + 5 维护）", () => {
  assert.equal(ENDPOINT_TOOLS.length, 20);
  assert.equal(ADMIN_TOOLS.length, 5);
  assert.equal(TOOL_COUNT, 25);
});

test("wp6 端点对等：工具端点集 ≡ workbench ENDPOINTS", () => {
  assert.deepEqual(ENDPOINT_TOOLS.map((tool) => tool.endpoint).sort(), [...ENDPOINTS].sort());
});

test("wp6 能力黑名单：无 exec/shell/file/token 类工具", () => {
  const names = [...ENDPOINT_TOOLS, ...ADMIN_TOOLS].map((tool) => tool.name);
  // 黑名单按整名或下划线/连字符分段精确匹配（规格 §3.6）：plan_execute 是规格点名的
  // snake_case 动作工具，不得因子串 "exec" 误伤；exec/shell/file/token 类能力名仍被拦死。
  for (const banned of TOOL_NAME_BLACKLIST) {
    assert.ok(!names.some((name) => {
      const lower = name.toLowerCase();
      return lower === banned || lower.split(/[^a-z0-9]+/).includes(banned);
    }), banned);
  }
});

test("wp6 服务常量锁定：8397/127.0.0.1/无默认 token", () => {
  assert.equal(DEFAULTS.port, 8397);
  assert.equal(DEFAULTS.host, "127.0.0.1");
  assert.equal(DEFAULTS.token, null);
});

test("wp6 admin 工具：store 同步抛错包装为 ok:false envelope", async () => {
  const { buildManifest } = await import("../server/manifest.mjs");
  const manifest = buildManifest({ handle: async () => ({ ok: true, value: {} }),
    store: { read: () => { throw new Error("数据文件损坏"); } } });
  const result = await manifest.find((tool) => tool.name === "admin_status").call({});
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "trading/invalid-operation");
  assert.match(result.error.message, /数据文件损坏/);
});
