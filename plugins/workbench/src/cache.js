// 结果缓存：内存 + 磁盘两级。
//
// 为什么需要磁盘这一级：这些接口背后是 python 子进程与富途调用，冷启动很贵
// （实测 factors 25.4s、correlation 7.2s、instrument 6.7s、events 5.9s、series 4.9s）。
// 只放内存意味着**每次重启进程缓存全丢**，用户重启后第一次点每个页签都要重新等一遍。
//
// 设计取舍：
//   * 磁盘条目带写入时刻，TTL 与内存一致；过期即视为未命中并删除。
//   * 条目上限 + 总量上限，超了按 mtime 淘汰最旧的，避免无限增长。
//   * 解析失败、超限、写盘失败一律当作未命中，绝不让缓存问题影响正常取数。
//   * 大结果（超过 maxBytes）只进内存，不落盘——面板数据不值得为此占用磁盘。
import { mkdirSync, readFileSync, readdirSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import os from "node:os";
import path from "node:path";

const DEFAULT_MAX_BYTES = 2 * 1024 * 1024;
const DEFAULT_MAX_FILES = 120;

function safeName(key) {
  const [endpoint, ...rest] = String(key).split("|");
  const digest = createHash("sha1").update(rest.join("|")).digest("hex").slice(0, 16);
  return `${String(endpoint).replace(/[^a-zA-Z0-9_-]/g, "_")}-${digest}.json`;
}

export function createTtlCache(options = {}) {
  const now = options.now ?? Date.now;
  const dir = options.dir
    ?? path.join(process.env.DSH_HOME || path.join(os.homedir(), ".dsh"), "trading-workbench-cache");
  const maxFiles = Number(options.maxFiles) || DEFAULT_MAX_FILES;
  const maxBytes = Number(options.maxBytes) || DEFAULT_MAX_BYTES;
  const memory = new Map();
  let diskReady = false;

  function ensureDir() {
    if (diskReady) return true;
    try {
      mkdirSync(dir, { recursive: true });
      diskReady = true;
    } catch {
      diskReady = false;
    }
    return diskReady;
  }

  function readDisk(file, ttl) {
    try {
      const raw = readFileSync(path.join(dir, file), "utf8");
      const entry = JSON.parse(raw);
      if (!entry || typeof entry.at !== "number") return null;
      if (ttl > 0 && now() - entry.at >= ttl) {
        try { unlinkSync(path.join(dir, file)); } catch { /* 过期文件删不掉也无所谓 */ }
        return null;
      }
      return entry;
    } catch {
      return null;   // 缺文件、坏 JSON、权限问题都当作未命中
    }
  }

  function prune() {
    try {
      const files = readdirSync(dir)
        .map((name) => {
          try { return { name, at: statSync(path.join(dir, name)).mtimeMs }; } catch { return null; }
        })
        .filter(Boolean)
        .sort((a, b) => b.at - a.at);
      for (const file of files.slice(maxFiles)) {
        try { unlinkSync(path.join(dir, file.name)); } catch { /* 忽略 */ }
      }
    } catch { /* 目录不可读时跳过淘汰 */ }
  }

  return {
    /** 命中返回 { value, at }，否则 null。ttl<=0 表示不缓存。 */
    read(key, ttl) {
      if (ttl <= 0) return null;
      const hit = memory.get(key);
      if (hit && now() - hit.at < ttl) return hit;
      if (hit) memory.delete(key);
      if (!ensureDir()) return null;
      const fromDisk = readDisk(safeName(key), ttl);
      if (fromDisk) memory.set(key, fromDisk);   // 回填内存，下次零开销
      return fromDisk;
    },

    /** 写入两级缓存，返回条目 { at }。 */
    write(key, value) {
      const entry = { value, at: now() };
      memory.set(key, entry);
      if (!ensureDir()) return entry;
      let payload;
      try {
        payload = JSON.stringify(entry);
      } catch {
        return entry;                            // 不可序列化的值只留内存
      }
      if (Buffer.byteLength(payload) > maxBytes) return entry;
      const target = path.join(dir, safeName(key));
      try {
        const temp = `${target}.tmp`;
        writeFileSync(temp, payload);
        renameSync(temp, target);
        prune();
      } catch { /* 写盘失败不影响返回 */ }
      return entry;
    },

    clearMemory() { memory.clear(); },
    get directory() { return dir; },
  };
}
