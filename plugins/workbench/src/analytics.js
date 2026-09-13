import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import os from "node:os";
import path from "node:path";

const run = promisify(execFile);
const pythonDir = fileURLToPath(new URL("../python/", import.meta.url));

const MODES = new Set(["sim", "live"]);
const STRATEGIES = new Set(["ma_cross", "rsi"]);
const METRICS = new Set(["total_return", "annualized", "sharpe", "max_drawdown", "win_rate"]);
const GRID = /^\d{1,3}(,\d{1,3}){1,7}$/;
const FACTORS = new Set(["mom_20", "mom_60", "vol_20", "trend", "rsi_14", "liq_ratio", "mdd_60"]);
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

  async function call(args, key, options = {}) {
    const hit = cache.get(key);
    if (hit && !options.skipCache && now() - hit.at < CACHE_TTL_MS) return hit.value;
    const script = options.script ?? "analytics.py";
    const { stdout } = await exec(python(), [path.join(pythonDir, script), ...args],
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
    // 券商真实持仓（模拟盘读模拟账户，实盘读真实账户）；python 侧另有磁盘缓存，
    // 因此用户点「刷新」时需要把 refresh 透传下去，否则只会拿到同一份缓存。
    async positions(payload = {}, options = {}) {
      const mode = modeOf(payload.mode);
      const args = ["--mode", mode];
      if (options.refresh) args.push("--refresh");
      return call(args, `positions|${mode}`, { skipCache: options.refresh, script: "positions.py" });
    },
    async sensitivity(payload = {}) {
      const tickerList = payload.ticker;
      if (typeof tickerList !== "string" || !TICKER.test(tickerList)) throw new Error("Invalid ticker");
      const strategy = payload.strategy ?? "ma_cross";
      if (!STRATEGIES.has(strategy)) throw new Error("Invalid strategy");
      const metric = payload.metric ?? "total_return";
      if (!METRICS.has(metric)) throw new Error("Invalid metric");
      for (const field of ["fast_grid", "slow_grid", "buy_grid", "sell_grid"]) {
        const value = payload[field];
        if (value === undefined) continue;
        if (typeof value !== "string" || !GRID.test(value)) throw new Error(`Invalid ${field}`);
        for (const part of value.split(",")) {
          const number = Number(part);
          if (!Number.isInteger(number) || number < 1 || number > 500) throw new Error(`Invalid ${field} value`);
        }
      }
      const start = payload.start ?? "2023-01-01";
      if (!/^\d{4}-\d{2}-\d{2}$/.test(start)) throw new Error("Invalid start date");
      const args = ["sensitivity", "--ticker", tickerList, "--strategy", strategy, "--metric", metric, "--start", start];
      if (payload.fast_grid) args.push("--fast-grid", payload.fast_grid);
      if (payload.slow_grid) args.push("--slow-grid", payload.slow_grid);
      if (payload.buy_grid) args.push("--buy-grid", payload.buy_grid);
      if (payload.sell_grid) args.push("--sell-grid", payload.sell_grid);
      return call(args, `sensitivity|${args.join(" ")}`);
    },
    async events(payload = {}) {
      const ticker = payload.ticker;
      if (typeof ticker !== "string" || !TICKER.test(ticker)) throw new Error("Invalid ticker");
      const days = intInRange(payload.days, 180, 30, 2000, "days");
      return call(["events", "--ticker", ticker, "--days", String(days)], `events|${ticker}|${days}`);
    },
    async factors(payload = {}) {
      const tickers = payload.tickers;
      if (!Array.isArray(tickers) || tickers.length < 2 || tickers.length > 8) {
        throw new Error("tickers must list 2..8 symbols");
      }
      if (tickers.some((t) => typeof t !== "string" || !TICKER.test(t))) throw new Error("Invalid ticker in list");
      const window = intInRange(payload.window, 250, 80, 1000, "window");
      return call(["snapshot", "--tickers", tickers.join(","), "--window", String(window)],
        `factors|${tickers.join(",")}|${window}`);
    },
    async ic(payload = {}) {
      const tickers = payload.tickers;
      if (!Array.isArray(tickers) || tickers.length < 3 || tickers.length > 8) {
        throw new Error("IC 需要 3..8 个标的（横截面相关）");
      }
      if (tickers.some((t) => typeof t !== "string" || !TICKER.test(t))) throw new Error("Invalid ticker in list");
      const factor = payload.factor ?? "mom_20";
      if (!FACTORS.has(factor)) throw new Error("Invalid factor");
      const forward = intInRange(payload.forward, 5, 1, 60, "forward");
      const window = intInRange(payload.window, 250, 80, 1000, "window");
      return call(["ic", "--tickers", tickers.join(","), "--factor", factor,
                   "--forward", String(forward), "--window", String(window)],
        `ic|${tickers.join(",")}|${factor}|${forward}|${window}`);
    },
    async sources(payload = {}) {
      if (Object.keys(payload).some((key) => key !== "no_probe")) throw new Error("Unexpected sources field");
      const args = ["sources"];
      if (payload.no_probe === true) args.push("--no-probe");
      return call(args, `sources|${payload.no_probe === true ? "noprobe" : "probe"}`);
    },
    async instrument(payload = {}) {
      const ticker = payload.ticker;
      if (typeof ticker !== "string" || !TICKER.test(ticker)) throw new Error("Invalid ticker");
      const hit = cache.get(`instrument|${ticker}`);
      if (hit && now() - hit.at < CACHE_TTL_MS) return hit.value;
      const { stdout } = await exec(python(), [path.join(pythonDir, "instruments.py"), "--ticker", ticker],
        { timeout: 120_000, maxBuffer: 4 * 1024 * 1024 });
      const start = stdout.indexOf("{");
      if (start < 0) throw new Error("instruments.py returned no JSON");
      const value = JSON.parse(stdout.slice(start));
      if (value.error) throw new Error(value.error);
      cache.set(`instrument|${ticker}`, { at: now(), value });
      return value;
    },
    async risk() {
      return call(["risk"], "risk");
    },
    async trades(payload = {}) {
      const mode = modeOf(payload.mode);
      const limit = intInRange(payload.limit, 50, 1, 200, "limit");
      return call(["trades", "--mode", mode, "--limit", String(limit)], `trades|${mode}|${limit}`);
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
