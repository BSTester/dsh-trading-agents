// Host → trading-core 子进程桥（模式同 engine/src/tools.js 的 runQuant）。
import { execFile } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);

export function pythonHome() {
  return process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
}

export function pythonBin() {
  return path.join(pythonHome(), "trading-venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
}

export async function pycore(args, { timeout = 60_000 } = {}) {
  const { stdout } = await run(pythonBin(), ["-m", "trading_core", ...args],
    { timeout, maxBuffer: 8 * 1024 * 1024 });
  const start = stdout.indexOf("{");
  if (start < 0) throw new Error(`trading_core ${args[0]} 无 JSON 输出`);
  const parsed = JSON.parse(stdout.slice(start));
  if (parsed.error) throw new Error(`trading_core: ${parsed.error}`);
  return parsed;
}
