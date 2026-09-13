// 富途 access_token 保活。
//
// 为什么需要：富途 OAuth 的 access_token 官方有效期只有 expires_in = 7200（2 小时），
// 而过期时服务端对所有工具返回 `internal error` 而不是 401。mcp__futu__* 那一行的
// Authorization 头是在**该行挂载时求值一次**的固定字符串（Zod 会把 getter 压平，
// 重连也复用同一份 config），因此 token 一过期，行里的头就永远是旧的。
//
// 本插件不重写 OAuth：续期逻辑只有一份，在 trading_datasource.futu_mcp.ensure_fresh()。
// 这里只负责「按节奏调用它」，从而让后续会话拿到新鲜 token，并与脚本通道行为一致。
import { execFile } from "node:child_process";
import { utimesSync } from "node:fs";
import { promisify } from "node:util";
import os from "node:os";
import path from "node:path";

const run = promisify(execFile);

export const name = "futu-keepalive";
// 不硬依赖任何服务：缺 venv / 缺 token 时安静跳过，不影响其他能力。
export const inject = [];

const DEFAULT_CHECK_INTERVAL_MS = 10 * 60_000;
const DEFAULT_RENEW_WITHIN_SECONDS = 1800;
const DEFAULT_PRESET_ID = "dsh-trading-agents";

function resolveHome() {
  return process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
}

/**
 * 调用共享实现续期。返回 { refreshed, detail }；任何异常都收敛为"未续期"，
 * 因为保活失败不应该影响会话里的其他功能。
 */
async function ensureFresh(python, withinSeconds, timeoutMs) {
  const script = [
    "import sys",
    `sys.path.insert(0, ${JSON.stringify(path.join(resolveHome(), "trading-python", "datasource"))})`,
    "from trading_datasource.futu_mcp import ensure_fresh",
    `refreshed, detail = ensure_fresh(${Number(withinSeconds)})`,
    "print(('REFRESHED' if refreshed else 'SKIPPED') + '|' + detail)",
  ].join("; ");
  const { stdout } = await run(python, ["-c", script], { timeout: timeoutMs });
  const line = String(stdout).trim().split("\n").pop() ?? "";
  const [flag, ...rest] = line.split("|");
  return { refreshed: flag === "REFRESHED", detail: rest.join("|") || line };
}

/**
 * 触碰 preset 组合文件，并按需热重载本会话的组合。
 *
 * 为什么这样能生效（读 dsh-agent-presets 源码得到）：
 *   `ensureStanding()` 用 `compositionStamp(path)` 比较挂载状态，而该印章是
 *   **{ mtimeMs, size }** —— 只做 utime 就能改变印章。印章不同 → 销毁并重建
 *   共享挂载 → 重新读取组合文件、重新求值那一行的 `!!js` Authorization 头。
 *   触发 `ensureStanding` 的入口有两个：新会话的 `mount()`，以及现有会话的
 *   `recompose(agentCtx, id)`。所以 recompose 能让**已在运行的会话**换上新 token。
 */
export function apply(ctx, config = {}) {
  const home = resolveHome();
  const python = config.python
    ?? path.join(home, "trading-venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const intervalMs = Number(config.checkIntervalMs) || DEFAULT_CHECK_INTERVAL_MS;
  const withinSeconds = Number(config.renewWithinSeconds) || DEFAULT_RENEW_WITHIN_SECONDS;
  const presetId = config.presetId || DEFAULT_PRESET_ID;
  const presetFile = config.presetFile
    || path.join(home, ".agent-presets", presetId, "agent.cordis.yml");
  // 热重载默认开启；万一在某环境下不稳定，配置 hotReload: false 即可退回
  // "新建会话"的人工路径。
  const hotReload = config.hotReload !== false;

  async function recomposeCurrentSession() {
    if (!hotReload) return "热重载已关闭，需新建会话";
    try {
      utimesSync(presetFile, new Date(), new Date());   // 改变印章
    } catch (error) {
      return `无法触碰组合文件（${String(error?.message ?? error).slice(0, 80)}），需新建会话`;
    }
    const presets = typeof ctx.get === "function" ? ctx.get("agentPresets") : undefined;
    if (!presets || typeof presets.recompose !== "function") {
      return "agentPresets 服务不可用，需新建会话";
    }
    try {
      await presets.recompose(ctx, presetId);
      return "已热重载组合，本会话的富途工具将使用新 token";
    } catch (error) {
      return `热重载失败（${String(error?.message ?? error).slice(0, 100)}），需新建会话`;
    }
  }

  let running = false;
  const tick = async () => {
    if (running) return; // 上一轮还没结束就跳过，避免叠加
    running = true;
    try {
      const result = await ensureFresh(python, withinSeconds, 60_000);
      if (result.refreshed) {
        const outcome = await recomposeCurrentSession();
        ctx.logger.info(`futu-keepalive: ${result.detail}；${outcome}`);
      } else {
        ctx.logger.debug?.(`futu-keepalive: ${result.detail}`);
      }
    } catch (error) {
      ctx.logger.warn(`futu-keepalive: 续期检查失败（不影响其他功能）：${String(error?.message ?? error).slice(0, 160)}`);
    } finally {
      running = false;
    }
  };

  const start = () => {
    const timer = setInterval(tick, intervalMs);
    timer.unref?.(); // 不阻止进程退出
    return timer;
  };

  if (typeof ctx.effect === "function") {
    ctx.effect(() => {
      const timer = start();
      void tick(); // 启动时先看一次：进程重启后可能已经临近过期
      return () => clearInterval(timer);
    });
  } else {
    const timer = start();
    void tick();
    ctx.on?.("dispose", () => clearInterval(timer));
  }
}
