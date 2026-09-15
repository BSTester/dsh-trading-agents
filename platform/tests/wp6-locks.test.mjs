// WP6 锁定测试（规格 §3.7）：工具面总数/名单、端点对等、能力黑名单、服务常量。
import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { ENDPOINT_TOOLS, ADMIN_TOOLS, TOOL_COUNT, TOOL_NAME_BLACKLIST, buildManifest } from "../server/manifest.mjs";
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

test("wp6 服务配置：env 覆盖在缺文件时同样生效；env=0 合法；env 非法端口忽略", async () => {
  const { mkdtemp, rm } = await import("node:fs/promises");
  const os = await import("node:os");
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-config-"));
  const { loadConfig } = await import("../server/config.mjs");
  const previous = process.env.TRADING_SERVICE_PORT;
  try {
    process.env.TRADING_SERVICE_PORT = "0";
    assert.equal(loadConfig(home).port, 0);            // 缺文件 + env 生效（冒烟测试的关键路径）
    process.env.TRADING_SERVICE_PORT = "99999";
    assert.equal(loadConfig(home).port, 8397);         // 非法 env 忽略
  } finally {
    if (previous === undefined) delete process.env.TRADING_SERVICE_PORT;
    else process.env.TRADING_SERVICE_PORT = previous;
    await rm(home, { recursive: true, force: true });
  }
});

test("wp6 黑名单守卫：规格点名词条在列；分段匹配能拦合成名", () => {
  for (const required of ["exec", "shell", "token", "write_file"]) {
    assert.ok(TOOL_NAME_BLACKLIST.includes(required), required);
  }
  const matches = (name) => {
    const lower = name.toLowerCase();
    return TOOL_NAME_BLACKLIST.some((banned) =>
      lower === banned || lower.split(/[^a-z0-9]+/).includes(banned));
  };
  assert.ok(matches("run_shell"));
  assert.ok(matches("exec_cmd"));
  assert.ok(!matches("plan_execute"));
});

test("wp6 admin 工具：store 同步抛错包装为 ok:false envelope", async () => {
  const manifest = buildManifest({ handle: async () => ({ ok: true, value: {} }),
    store: { read: () => { throw new Error("数据文件损坏"); } } });
  const result = await manifest.find((tool) => tool.name === "admin_status").call({});
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "trading/invalid-operation");
  assert.match(result.error.message, /数据文件损坏/);
});
