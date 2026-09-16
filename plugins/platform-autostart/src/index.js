// 会话启动自动检测并拉起量化平台服务 —— Harness 接线层。
//
// 行为约束（按不变式逐条落地）：
//   1. 绝不阻塞/破坏会话：apply 里只挂一个 setTimeout(0)，把全部工作挪出挂载路径；
//      run() 全程 try/catch，任何异常都收敛为一行 JSON 日志，绝不向上抛。
//   2. 不绕过任何闸门：本插件只负责"把服务进程拉起来"，服务的 token 认证、sim/live
//      模式闸门、交易确认全部留在服务侧，与本插件无关。
//   3. 拉起的是分离进程（detached + unref，见 autostart.js），随会话结束仍存活。
//   4. 每个会话（进程）最多尝试一次启动：模块级 attempted 标志，apply 被多次调用也只跑一回。
//
// 日志约定（便于 docs/RUNBOOK.md 排障）：探测/启动结果打一行
//   {"event":"platform-autostart","ts":...,"action":"ok|started|not-installed|spawn-failed","detail":...}
// 到 Harness 控制台，并追加到 ~/.dsh/trading-platform-service.log（服务自身输出也在
// 该文件，拉起失败先看这里）。
import { spawn } from "node:child_process";
import fs from "node:fs";
import { request as httpRequest } from "node:http";
import os from "node:os";
import path from "node:path";

import { ensureRunning, readPort, SERVICE_LOG_NAME } from "./autostart.js";

export const name = "platform-autostart";
// 不硬依赖任何服务：venv/仓库未安装时只提示，不影响会话其他能力。
export const inject = [];

function resolveHome() {
  return process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
}

/** GET /healthz 的最小探测：任何异常/超时都收敛为 {ok:false}，绝不抛错。 */
function probeHttp(url, timeoutMs = 2000) {
  return new Promise((resolve) => {
    try {
      const request = httpRequest(url, { method: "GET", timeout: timeoutMs }, (response) => {
        response.resume(); // 排干响应体，socket 才能正常回收
        resolve({ ok: response.statusCode >= 200 && response.statusCode < 300, status: response.statusCode });
      });
      request.on("timeout", () => { request.destroy(); resolve({ ok: false, status: 0 }); });
      request.on("error", () => resolve({ ok: false, status: 0 }));
      request.end();
    } catch {
      resolve({ ok: false, status: 0 });
    }
  });
}

/** best-effort 追加一行到服务日志文件；失败安静忽略（控制台日志已足够定位）。 */
function appendServiceLog(logPath, line) {
  try {
    fs.appendFileSync(logPath, JSON.stringify(line) + "\n");
  } catch { /* 文件系统问题不该影响会话 */ }
}

async function run() {
  const home = resolveHome();
  const logPath = path.join(home, SERVICE_LOG_NAME);
  const result = await ensureRunning({
    probe: probeHttp,
    spawn,
    fs,
    home,
    port: readPort(readConfigText(home)),
    log: (line) => {
      try {
        console.log(JSON.stringify(line));
      } catch { /* 序列化失败不该影响会话 */ }
      appendServiceLog(logPath, line);
    },
  });
  return result;
}

function readConfigText(home) {
  try {
    return fs.readFileSync(path.join(home, "trading-platform.json"), "utf8");
  } catch {
    return ""; // 配置缺失/不可读 → 默认端口 8397
  }
}

// 每个会话（进程）最多尝试一次；apply 可能被重复调用（组合重载等），据此兜底。
let attempted = false;

export function apply(ctx) {
  void ctx; // 本插件不消费任何 Cordis 服务
  if (attempted) return;
  attempted = true;
  // setTimeout(0)：挂载路径立即返回，检测/拉起在下一个事件循环轮次进行。
  // 一次性定时器、自带全捕获，无需 disposer（futu-keepalive 的 interval 才需要）。
  setTimeout(() => {
    run().catch((error) => {
      try {
        const line = {
          event: "platform-autostart",
          ts: new Date().toISOString(),
          action: "spawn-failed",
          detail: `自动拉起流程异常（不影响会话）：${String(error?.message ?? error)}`,
        };
        console.log(JSON.stringify(line));
        appendServiceLog(path.join(resolveHome(), SERVICE_LOG_NAME), line);
      } catch { /* 最后的兜底：什么都不做 */ }
    });
  }, 0);
}
