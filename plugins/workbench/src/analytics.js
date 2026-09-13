import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import os from "node:os";
import path from "node:path";

const run = promisify(execFile);
const pythonDir = fileURLToPath(new URL("../python/", import.meta.url));

const MODES = new Set(["sim", "live"]);
const TICKER = /^[A-Za-z0-9.^-]{1,40}$/;
const CACHE_TTL_MS = 30_000;

function pythonPath() {
  const home = process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
  return path.join(home, "trading-venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
}

function modeOf(value) {
  const mode = value ?? "sim";
  if (!MODES.has(mode)) throw new Error("Invalid mode");
  return mode;
}

function intInRange(value, fallback, min, max, label) {
  const number = value ?? fallback;
  if (!Number.isInteger(number) || number < min || number > max) {
    throw new Error(`Invalid ${label} (${min}..${max})`);
  }
  return number;
}

/**
 * 分析层数据提供方：权益曲线回放、持仓盯市、相关性矩阵。
 * 全部只读；参数在 Host 侧校验后才进入子进程。
 */
export function createAnalyticsProvider({ exec = run, python = pythonPath, now = Date.now } = {}) {
  const cache = new Map();

  async function call(args, key) {
    const hit = cache.get(key);
    if (hit && now() - hit.at < CACHE_TTL_MS) return hit.value;
    const { stdout } = await exec(python(), [path.join(pythonDir, "analytics.py"), ...args],
      { timeout: 180_000, maxBuffer: 16 * 1024 * 1024 });
    const start = stdout.indexOf("{");
    if (start < 0) throw new Error("analytics.py returned no JSON");
    const value = JSON.parse(stdout.slice(start));
    if (value.error) throw new Error(value.error);
    cache.set(key, { at: now(), value });
    return value;
  }

  return {
    async equity(payload = {}) {
      const mode = modeOf(payload.mode);
      const window = intInRange(payload.window, 250, 20, 1000, "window");
      return call(["equity", "--mode", mode, "--window", String(window)], `equity|${mode}|${window}`);
    },
    async positions(payload = {}) {
      const mode = modeOf(payload.mode);
      return call(["positions", "--mode", mode], `positions|${mode}`);
    },
    async correlation(payload = {}) {
      const tickers = payload.tickers;
      if (!Array.isArray(tickers) || tickers.length < 2 || tickers.length > 8) {
        throw new Error("tickers must list 2..8 symbols");
      }
      if (tickers.some((t) => typeof t !== "string" || !TICKER.test(t))) throw new Error("Invalid ticker in list");
      const window = intInRange(payload.window, 120, 40, 500, "window");
      return call(["correlation", "--tickers", tickers.join(","), "--window", String(window)],
        `correlation|${tickers.join(",")}|${window}`);
    },
  };
}
