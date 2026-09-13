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
