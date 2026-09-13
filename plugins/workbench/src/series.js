import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import os from "node:os";
import path from "node:path";

const run = promisify(execFile);
const pythonDir = fileURLToPath(new URL("../python/", import.meta.url));

const PERIODS = new Set(["1m", "5m", "15m", "30m", "60m", "1d"]);
const TICKER = /^[A-Za-z0-9.^-]{1,40}$/;
/** 行情序列的短时缓存：K线在分钟级无需每次轮询都重新取数。 */
const CACHE_TTL_MS = 30_000;

function pythonPath() {
  const home = process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
  return path.join(home, "trading-venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
}

/**
 * 为一个 (ticker, period) 取 K 线序列。
 * 校验在 Host 侧完成：越界或非法参数直接抛错，绝不透传到子进程。
 */
export function createSeriesProvider({ exec = run, python = pythonPath, now = Date.now } = {}) {
  const cache = new Map();
  return async function fetchSeries({ ticker, period = "5m", limit = 300 } = {}) {
    if (typeof ticker !== "string" || !TICKER.test(ticker)) throw new Error("Invalid ticker");
    if (!PERIODS.has(period)) throw new Error("Invalid period");
    if (!Number.isInteger(limit) || limit < 20 || limit > 2000) throw new Error("Invalid limit (20..2000)");

    const key = `${ticker}|${period}|${limit}`;
    const hit = cache.get(key);
    if (hit && now() - hit.at < CACHE_TTL_MS) return hit.value;

    const { stdout } = await exec(python(), [path.join(pythonDir, "bars.py"),
      "--ticker", ticker, "--period", period, "--limit", String(limit)],
      { timeout: 120_000, maxBuffer: 16 * 1024 * 1024 });
    const start = stdout.indexOf("{");
    if (start < 0) throw new Error("bars.py returned no JSON");
    const value = JSON.parse(stdout.slice(start));
    if (value.error) throw new Error(value.error);
    cache.set(key, { at: now(), value });
    return value;
  };
}
