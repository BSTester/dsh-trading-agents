// 服务配置（规格 §3.7）：~/.dsh/trading-platform.json 的 service 节可覆盖默认值。
// 缺文件用默认；坏 JSON 必须抛出——静默回退默认会让「配置没生效」变成不可诊断。
import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

export const DEFAULTS = Object.freeze({ port: 8397, host: "127.0.0.1", token: null });

export function configPath(home = process.env.DSH_HOME || path.join(os.homedir(), ".dsh")) {
  return path.join(home, "trading-platform.json");
}

export function loadConfig(home) {
  const merged = { ...DEFAULTS };
  let raw;
  try {
    raw = JSON.parse(readFileSync(configPath(home), "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT") return merged;
    throw new Error(`trading-platform.json 解析失败：${error.message}`);
  }
  const service = raw?.service ?? {};
  if (Number.isInteger(service.port) && service.port > 0 && service.port < 65536) merged.port = service.port;
  if (typeof service.host === "string" && service.host) merged.host = service.host;
  if (typeof service.token === "string" && service.token) merged.token = service.token;
  if (process.env.TRADING_SERVICE_PORT && Number.isInteger(Number(process.env.TRADING_SERVICE_PORT))) {
    merged.port = Number(process.env.TRADING_SERVICE_PORT);
  }
  return merged;
}
