// 服务配置（规格 §3.7）：~/.dsh/trading-platform.json 的 service 节可覆盖默认值。
// 优先级：环境变量 TRADING_SERVICE_PORT > 配置文件 > 默认（env 覆盖在缺文件时同样生效——
// 集成测试正是用「临时 DSH_HOME + TRADING_SERVICE_PORT=0」起服务的）。
import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

export const DEFAULTS = Object.freeze({ port: 8397, host: "127.0.0.1", token: null });

export function configPath(home = process.env.DSH_HOME || path.join(os.homedir(), ".dsh")) {
  return path.join(home, "trading-platform.json");
}

function validPort(value) {
  return Number.isInteger(value) && (value === 0 || (value > 0 && value < 65536));
}

export function loadConfig(home) {
  const merged = { ...DEFAULTS };
  let text = null;
  try {
    text = readFileSync(configPath(home), "utf8");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;   // 权限等读取失败原样带 code 抛出，不冒充解析错误
  }
  if (text !== null) {
    let raw;
    try {
      raw = JSON.parse(text);
    } catch (error) {
      throw new Error(`trading-platform.json 解析失败：${error.message}`);
    }
    const service = raw?.service ?? {};
    if (validPort(service.port) && service.port > 0) merged.port = service.port;
    if (typeof service.host === "string" && service.host) merged.host = service.host;
    if (typeof service.token === "string" && service.token) merged.token = service.token;
  }
  const envPort = Number(process.env.TRADING_SERVICE_PORT);
  if (process.env.TRADING_SERVICE_PORT !== undefined && process.env.TRADING_SERVICE_PORT !== ""
      && validPort(envPort)) {
    merged.port = envPort;   // env=0 是合法特例（临时端口，测试用）；文件端口仍要求 >0
  }
  return merged;
}
