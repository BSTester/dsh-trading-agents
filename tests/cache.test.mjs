// 两级缓存（内存 + 磁盘）的离线测试。
//
// 磁盘这一级的存在理由很具体：这些接口冷启动要 5-25 秒，而 Host 缓存原先只在内存里，
// **进程一重启就全丢**——用户每次重启后第一次点每个页签都要重新等一遍。
// 因此下面最关键的一条是"新实例（模拟重启）仍能命中"。
import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, rmSync, writeFileSync, mkdirSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createTtlCache } from "../plugins/workbench/src/cache.js";

function scratch(t) {
  const dir = mkdtempSync(path.join(os.tmpdir(), "cache-test-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test("同一实例内命中内存缓存", (t) => {
  const cache = createTtlCache({ dir: scratch(t) });
  cache.write("factors|abc", { rows: 1 });
  assert.deepEqual(cache.read("factors|abc", 1000).value, { rows: 1 });
});

test("新实例仍能命中磁盘缓存（模拟进程重启）", (t) => {
  const dir = scratch(t);
  const first = createTtlCache({ dir });
  first.write("factors|abc", { rows: 42 });

  const afterRestart = createTtlCache({ dir });   // 内存是空的
  const hit = afterRestart.read("factors|abc", 60_000);
  assert.ok(hit, "重启后应命中磁盘缓存——这正是加磁盘一级的目的");
  assert.deepEqual(hit.value, { rows: 42 });
});

test("过期条目视为未命中，并从磁盘删除", (t) => {
  const dir = scratch(t);
  let clock = 1_000_000;
  const cache = createTtlCache({ dir, now: () => clock });
  cache.write("events|abc", { events: [] });
  clock += 999;
  assert.ok(cache.read("events|abc", 1000), "未过期应命中");
  clock += 2;
  assert.equal(cache.read("events|abc", 1000), null, "过期后应未命中");
  assert.equal(readdirSync(dir).length, 0, "过期条目应被清理");
});

test("ttl 为 0 表示不缓存", (t) => {
  const dir = scratch(t);
  const cache = createTtlCache({ dir });
  cache.write("audit|abc", { entries: [] });
  assert.equal(cache.read("audit|abc", 0), null);
});

test("坏文件当作未命中，不影响正常取数", (t) => {
  const dir = scratch(t);
  createTtlCache({ dir }).write("risk|abc", { config: 1 });
  const file = readdirSync(dir)[0];
  writeFileSync(path.join(dir, file), "{ 这不是 JSON");
  // 必须用新实例：内存会先命中，那样就测不到磁盘解析这条路
  assert.equal(createTtlCache({ dir }).read("risk|abc", 60_000), null);
});

test("超大结果只进内存不落盘", (t) => {
  const dir = scratch(t);
  const cache = createTtlCache({ dir, maxBytes: 64 });
  const value = { blob: "x".repeat(500) };
  cache.write("series|abc", value);
  assert.deepEqual(cache.read("series|abc", 60_000).value, value, "内存仍应命中");
  assert.deepEqual(readdirSync(dir), [], "不应写盘");
});

test("不可序列化的值只留内存，不抛异常", (t) => {
  const dir = scratch(t);
  const cache = createTtlCache({ dir });
  const cyclic = {};
  cyclic.self = cyclic;
  assert.doesNotThrow(() => cache.write("x|y", cyclic));
  assert.deepEqual(readdirSync(dir), []);
});

test("条目数超限时按时间淘汰最旧的", (t) => {
  const dir = scratch(t);
  let clock = 1_000_000;
  const cache = createTtlCache({ dir, maxFiles: 3, now: () => clock });
  for (let i = 0; i < 6; i += 1) {
    clock += 1000;
    cache.write(`e${i}|k`, { i });
  }
  const left = readdirSync(dir).length;
  assert.ok(left <= 3, `应淘汰到不超过 3 个，实际 ${left}`);
});

test("不同参数各自成键，互不串味", (t) => {
  const dir = scratch(t);
  const cache = createTtlCache({ dir });
  cache.write("instrument|600519", { t: "600519" });
  cache.write("instrument|00700", { t: "00700" });
  assert.deepEqual(cache.read("instrument|600519", 60_000).value, { t: "600519" });
  assert.deepEqual(cache.read("instrument|00700", 60_000).value, { t: "00700" });
});

test("目录不可写时退化为纯内存，不抛异常", (t) => {
  const dir = path.join(scratch(t), "不存在", "层级");
  const cache = createTtlCache({ dir });
  assert.doesNotThrow(() => cache.write("k|v", { a: 1 }));
  assert.deepEqual(cache.read("k|v", 60_000).value, { a: 1 }, "内存仍可用");
});

test("暴露目录路径，便于排查与告知用户", (t) => {
  const dir = scratch(t);
  assert.equal(createTtlCache({ dir }).directory, dir);
});

test("默认目录落在 DSH_HOME 下的 trading-workbench-cache", (t) => {
  const home = scratch(t);
  const previous = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  t.after(() => {
    if (previous === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = previous;
  });
  assert.equal(createTtlCache().directory, path.join(home, "trading-workbench-cache"));
});

test("坏缓存条目（空对象/截断结果）必须当未命中，不能展示成「账户没有持仓」", async () => {
  const { createRpcHandler } = await import("../plugins/workbench/src/rpc.js");
  const { mkdtempSync } = await import("node:fs");
  const os = await import("node:os");
  const path = await import("node:path");
  const dir = mkdtempSync(path.join(os.tmpdir(), "cache-shape-"));

  let calls = 0;
  const handle = createRpcHandler({}, { dir, analytics: {
    positions: async () => { calls += 1; return { mode: "sim", groups: [{ acc_id: "1", positions: [] }] }; },
  } });
  const good = await handle("positions", { mode: "sim" });
  assert.equal(good.ok, true);
  assert.equal(good.cached, false);

  // 直接往磁盘缓存里塞一个空对象，模拟并发写入/崩溃留下的坏条目。
  // 必须换一个 handler：原 handler 的内存缓存里还是刚才那份好数据。
  const { createTtlCache } = await import("../plugins/workbench/src/cache.js");
  createTtlCache({ dir }).write('positions|[["mode","sim"]]', {});
  const after = await createRpcHandler({}, { dir, analytics: {
    positions: async () => { calls += 1; return { mode: "sim", groups: [{ acc_id: "1", positions: [] }] }; },
  } })("positions", { mode: "sim" });
  assert.equal(after.ok, true, "坏条目应被忽略并重新取数，而不是报错");
  assert.equal(after.cached, false, "坏条目不得被当作缓存命中");
  assert.equal(after.value.groups.length, 1);
  assert.equal(calls, 2, "应当重新取数一次");
});

test("并发写缓存不得共用同一个临时文件", async () => {
  const source = await (await import("node:fs/promises")).readFile(
    new URL("../plugins/workbench/src/cache.js", import.meta.url), "utf8");
  // 共用 "${target}.tmp" 时，Host 进程与 CLI 会互相截断写入，留下半截 JSON
  assert.doesNotMatch(source, /const temp = `\$\{target\}\.tmp`/,
    "临时文件名必须唯一（含 pid 与随机串）");
  assert.match(source, /process\.pid/, "临时名应包含 pid");
});

test("测试绝不允许写进用户真实的缓存目录——全局护栏", async () => {
  const { readdir, readFile } = await import("node:fs/promises");
  const url = new URL(".", import.meta.url);
  const files = (await readdir(url)).filter((name) => name.endsWith(".test.mjs"));
  const offenders = [];
  for (const name of files) {
    const source = await readFile(new URL(name, url), "utf8");
    for (const match of source.matchAll(/createRpcHandler\(/g)) {
      // 括号配对取出整个调用的实参
      let depth = 0, end = -1;
      const start = match.index + match[0].length - 1;
      for (let i = start; i < source.length; i += 1) {
        if (source[i] === "(") depth += 1;
        else if (source[i] === ")") { depth -= 1; if (depth === 0) { end = i; break; } }
      }
      if (end === -1) continue;
      const args = source.slice(start, end + 1);
      // 允许：显式 dir、isolatedCache(t)/isolated(t) 这类一次性目录
      if (/\bdir\b|isolatedCache\(|isolated\(/.test(args)) continue;
      const line = source.slice(0, match.index).split("\n").length;
      offenders.push(`${name}:L${line} → ${args.slice(0, 60).replace(/\s+/g, " ")}`);
    }
  }
  // 真实事故：测试用桩返回空载荷并写进了 ~/.dsh/trading-workbench-cache，
  // 于是面板在 TTL 内把「没有持仓」当成事实展示出来。
  assert.deepEqual(offenders, [],
    "测试必须给 createRpcHandler 传一次性 dir，否则会污染用户真实缓存");
});
