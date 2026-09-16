// 会话启动自动检测并拉起量化平台服务 —— 纯逻辑层。
//
// 设计约定（与 scripts/install_platform.py 的「纯函数与副作用分离」同一套思路）：
//   * 本模块不做任何"活的"操作：probe/spawn/fs/wait/log 全部由调用方注入，
//     因此可以离线、确定性地测试全部分支（tests/test_platform_autostart.mjs）。
//   * 职责只有三件事：
//       1. GET /healthz 探测 —— 通了就什么都不做（闸门在本探测，不在插件）；
//       2. 未安装（仓库/venv 缺失）→ 返回安装指引，绝不强行启动；
//       3. 已安装但未启动 → 以分离进程（detached + unref）拉起，等 ~4s 复测确认。
//   * 本插件只是"启动服务进程"，不触碰服务自身的认证/闸门（token、模式切换、
//     交易确认都留在服务侧）——见仓库 README 的安全边界。
import path from "node:path";

export const DEFAULT_PORT = 8397;
export const PROBE_TIMEOUT_MS = 2000;
export const START_WAIT_MS = 4000;
export const SERVICE_LOG_NAME = "trading-platform-service.log";
export const REPO_MARKER_NAME = "trading-platform-repo";
export const SETUP_GUIDE = "install/HARNESS_SETUP.md";

/** 解析 ~/.dsh/trading-platform.json 的 service.port；坏 JSON/越界一律回落默认。 */
export function readPort(configText) {
  let raw;
  try {
    raw = JSON.parse(configText);
  } catch {
    return DEFAULT_PORT; // 坏 JSON → 默认端口（与 install_platform.service_config 同一口径）
  }
  const service = raw && typeof raw === "object" && !Array.isArray(raw) ? raw.service : null;
  const port = service && typeof service === "object" ? service.port : undefined;
  if (typeof port === "number" && Number.isInteger(port) && port > 0 && port < 65536) return port;
  return DEFAULT_PORT;
}

/** venv 解释器路径；win32 分支用参数注入，Linux 上也能测（对齐 install_platform.venv_python）。 */
export function venvPythonPath(home, platform = process.platform) {
  return path.join(home, "trading-venv",
    platform === "win32" ? path.join("Scripts", "python.exe") : path.join("bin", "python"));
}

/**
 * 定位 venv 解释器与平台仓库根。
 * 仓库根优先级：环境变量 DSH_TRADING_REPO > 标记文件 <home>/trading-platform-repo
 * （首行去空白，由 install_plugins.py / install_platform.py 写入）> null。
 */
export function resolvePaths({ home, markerText, env = {}, platform = process.platform } = {}) {
  const venvPython = venvPythonPath(home, platform);
  let repoRoot = null;
  const fromEnv = typeof env.DSH_TRADING_REPO === "string" ? env.DSH_TRADING_REPO.trim() : "";
  if (fromEnv) {
    repoRoot = fromEnv;
  } else if (typeof markerText === "string" && markerText) {
    const firstLine = markerText.split(/\r?\n/, 1)[0].trim();
    if (firstLine) repoRoot = firstLine;
  }
  return { venvPython, repoRoot };
}

/**
 * 确保平台服务在跑；返回 { action, detail }：
 *   ok            服务已在运行（首次探测即通）
 *   started       未在跑 → 分离进程拉起，~4s 复测通过
 *   spawn-failed  拉起后复测仍不通 / spawn 本身抛错（detail 指向日志文件）
 *   not-installed 仓库或 venv 缺失（detail 给安装指引），绝不强行启动
 *
 * 并发会话竞态是安全的：多个会话可能同时探测失败并各自 spawn，但服务自身绑定
 * 127.0.0.1:<port> 失败即打印 {"ok":false} 并退出（run.py 的既定行为），
 * 最终只有一个监听者存活；/healthz 探测是第一道闸门，绝大多数情况只命中一次。
 */
export async function ensureRunning({
  probe, spawn, home, now = () => new Date().toISOString(),
  log = () => {}, port = DEFAULT_PORT, host = "127.0.0.1",
  wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  fs, env = {}, platform = process.platform,
} = {}) {
  const healthUrl = `http://${host}:${port}/healthz`;
  const finish = async (action, detail) => {
    try {
      log({ event: "platform-autostart", ts: now(), action, detail, url: healthUrl });
    } catch { /* 日志失败不影响结论 */ }
    return { action, detail };
  };

  try {
    const first = await probe(healthUrl, PROBE_TIMEOUT_MS);
    if (first?.ok) {
      return finish("ok", `平台服务已在运行（${healthUrl}），无需拉起`);
    }
  } catch (error) {
    // 探测异常视同"未在跑"，继续走安装检查（绝不向上抛）
    void error;
  }

  const markerPath = path.join(home, REPO_MARKER_NAME);
  let markerText = null;
  try {
    markerText = fs.readFileSync(markerPath, "utf8");
  } catch { /* 标记缺失 → 走 not-installed */ }
  const { venvPython, repoRoot } = resolvePaths({ home, markerText, env, platform });
  if (!repoRoot) {
    return finish("not-installed",
      `未找到量化平台仓库（缺标记文件 ${markerPath}，且未设 DSH_TRADING_REPO）；`
      + `请按 ${SETUP_GUIDE} 安装后再用，本插件不会强行启动`);
  }

  const entry = path.join(repoRoot, "platform", "server", "run.py");
  if (!fs.existsSync(entry)) {
    return finish("not-installed",
      `仓库不完整：入口缺失（${entry}）；请按 ${SETUP_GUIDE} 完成安装（clone + 安装器）`);
  }

  if (!fs.existsSync(venvPython)) {
    return finish("not-installed",
      `venv 解释器缺失（${venvPython}）；请按 ${SETUP_GUIDE} 创建：`
      + `python3 -m venv ${path.join(home, "trading-venv")} 并安装 platform/requirements.txt`);
  }

  const logPath = path.join(home, SERVICE_LOG_NAME);
  let logFd;
  try {
    logFd = fs.openSync(logPath, "a"); // append：历史日志保留，服务输出与本插件记录同文件
  } catch (error) {
    return finish("spawn-failed", `日志文件打开失败（${logPath}）：${String(error?.message ?? error)}`);
  }
  try {
    const child = spawn(venvPython, ["-m", "server.run"], {
      cwd: path.join(repoRoot, "platform"), // -m server.run 的解析前提（见 docs/RUNBOOK.md 启动节）
      detached: true,                        // 分离进程：随会话结束仍存活
      stdio: ["ignore", logFd, logFd],       // stdout/stderr 都进日志文件
    });
    child.unref?.(); // 不阻塞父进程退出
  } catch (error) {
    return finish("spawn-failed", `服务进程拉起失败：${String(error?.message ?? error)}；详见日志 ${logPath}`);
  } finally {
    try { fs.closeSync(logFd); } catch { /* 父侧关闭即可，子进程已持有自己的副本 */ }
  }

  await wait(START_WAIT_MS);
  try {
    const second = await probe(healthUrl, PROBE_TIMEOUT_MS);
    if (second?.ok) {
      return finish("started", `平台服务已拉起（${healthUrl}）；运行日志 ${logPath}`);
    }
  } catch (error) {
    void error;
  }
  return finish("spawn-failed",
    `服务拉起后 ${Math.round(START_WAIT_MS / 1000)}s 内未就绪；详见日志 ${logPath}`
    + `（常见原因：端口被非本服务占用、依赖未装、解释器不在 trading-venv 内）`);
}
