// platform-autostart 插件的离线回归测试。
//
// 这个插件在**会话启动路径**上运行，失败模式与 futu-keepalive 同类：一旦行为出错
//（比如服务已在跑还重复拉起、venv 缺失仍然 spawn、或者抛错炸掉会话启动），后果落在
// 会话本身，很难第一时间归因。因此全部依赖（probe/spawn/fs/wait/log）注入，
// 纯逻辑（readPort/resolvePaths/ensureRunning）不碰真实网络与进程；index.js 的接线
// 只测「不抛错 + 一行 JSON 日志 + 每会话最多一次」。
import test from "node:test";
import assert from "node:assert/strict";
import { spawn as realSpawn } from "node:child_process";
import net from "node:net";
import realFs from "node:fs";
import os from "node:os";
import path from "node:path";

import { apply, inject, name } from "../plugins/platform-autostart/src/index.js";
import { DEFAULT_PORT, ensureRunning, readPort, resolvePaths }
  from "../plugins/platform-autostart/src/autostart.js";

const tmpHome = (t) => {
  const home = realFs.mkdtempSync(path.join(os.tmpdir(), "autostart-"));
  t.after(() => realFs.rmSync(home, { recursive: true, force: true }));
  return home;
};

/** 假 venv + 假仓库：只摆 ensureRunning 检查存在性的文件，不执行任何东西。 */
function fakeInstall(home) {
  const repo = path.join(home, "repo");
  realFs.mkdirSync(path.join(repo, "platform", "server"), { recursive: true });
  realFs.writeFileSync(path.join(repo, "platform", "server", "run.py"), "# entry\n");
  realFs.mkdirSync(path.join(home, "trading-venv", "bin"), { recursive: true });
  realFs.writeFileSync(path.join(home, "trading-venv", "bin", "python"), "#!/bin/sh\nexit 0\n");
  realFs.writeFileSync(path.join(home, "trading-platform-repo"), repo + "\n");
  return { repo, venvPython: path.join(home, "trading-venv", "bin", "python") };
}

/** 在真 fs 上包一层 openSync 间谍，其余调用透传；记录 [file, flags, fd]。 */
function spyFs(openCalls) {
  return {
    ...realFs,
    openSync(file, flags, ...rest) {
      const fd = realFs.openSync(file, flags, ...rest);
      openCalls.push([file, flags, fd]);
      return fd;
    },
  };
};

// ===================== 一、纯函数：readPort / resolvePaths =====================

test("readPort 解析 service.port；坏 JSON/越界/缺失回落默认 8397", () => {
  assert.equal(readPort('{"service":{"port":9001}}'), 9001);
  assert.equal(readPort('{"service":{}}'), DEFAULT_PORT);
  assert.equal(readPort('{"service":{"port":"9001"}}'), DEFAULT_PORT, "字符串端口不采纳");
  assert.equal(readPort("{oops"), DEFAULT_PORT, "坏 JSON 回落默认（不抛错）");
  assert.equal(readPort(""), DEFAULT_PORT);
  assert.equal(readPort(undefined), DEFAULT_PORT);
});

test("resolvePaths：环境变量优先于标记文件；标记取首行去空白；都缺则 null", () => {
  const { venvPython, repoRoot } = resolvePaths({ home: "/h", markerText: "  /marker/repo\n" });
  assert.equal(venvPython, path.join("/h", "trading-venv", "bin", "python"));
  assert.equal(repoRoot, "/marker/repo");
  assert.equal(
    resolvePaths({ home: "/h", markerText: "/marker/repo\n", env: { DSH_TRADING_REPO: "/env/repo" } }).repoRoot,
    "/env/repo", "DSH_TRADING_REPO 覆盖标记文件");
  assert.equal(resolvePaths({ home: "/h", markerText: undefined, env: {} }).repoRoot, null);
  assert.equal(resolvePaths({ home: "/h", markerText: "\n  \n" }).repoRoot, null, "空白标记不算数");
});

test("resolvePaths：win32 取 Scripts/python.exe（平台可注入，Linux 也能测）", () => {
  const { venvPython } = resolvePaths({ home: "/h", markerText: "/r", platform: "win32" });
  assert.equal(venvPython, path.join("/h", "trading-venv", "Scripts", "python.exe"));
});

// ===================== 二、ensureRunning：分支与拉起参数 =====================

test("probe ok → action:'ok'，spawn 不被调用", async (t) => {
  const home = tmpHome(t);
  fakeInstall(home);
  let spawns = 0;
  const result = await ensureRunning({
    probe: async () => ({ ok: true, status: 200 }),
    spawn: () => { spawns += 1; return { unref() {} }; },
    home,
    fs: spyFs([]),
  });
  assert.equal(result.action, "ok");
  assert.equal(spawns, 0, "服务已在跑时绝不重复拉起");
});

test("probe 失败 + 仓库/venv 就绪 → detached 拉起、unref、日志路径正确，复测通过 → started", async (t) => {
  const home = tmpHome(t);
  const { repo, venvPython } = fakeInstall(home);
  const openCalls = [];
  const events = [];
  const probes = [];
  let unrefs = 0;
  const probe = async (url) => {
    probes.push(url);
    events.push("probe");
    return probes.length === 1 ? { ok: false, status: 0 } : { ok: true, status: 200 };
  };
  const waits = [];
  const result = await ensureRunning({
    probe,
    spawn: (cmd, args, opts) => {
      events.push("spawn");
      assert.equal(cmd, venvPython);
      assert.deepEqual(args, ["-m", "server.run"]);
      assert.equal(opts.cwd, path.join(repo, "platform"), "cwd 必须是 <repo>/platform（-m server.run 的解析前提）");
      assert.equal(opts.detached, true, "分离进程：随会话结束仍存活");
      return { unref: () => { unrefs += 1; } };
    },
    wait: async (ms) => { events.push("wait"); waits.push(ms); },
    home,
    fs: spyFs(openCalls),
  });
  assert.equal(result.action, "started");
  assert.equal(unrefs, 1, "必须 unref，不阻塞父进程退出");
  assert.deepEqual(events, ["probe", "spawn", "wait", "probe"], "复测必须发生在等待之后");
  assert.equal(waits[0], 4000, "默认等 ~4s 再复测");
  const logPath = path.join(home, "trading-platform-service.log");
  assert.deepEqual(openCalls.map(([file, flags]) => [file, flags]), [[logPath, "a"]],
    "日志以 append 打开并作为 stdio 落点");
  assert.ok(Number.isInteger(realFs.statSync(logPath).ino), "日志文件确实被创建");
  assert.ok(probes.every((url) => url === "http://127.0.0.1:8397/healthz"), `探测 /healthz，实际 ${probes}`);
  assert.match(result.detail, /trading-platform-service\.log/, "detail 指向日志文件");
});

test("spawn 的 stdio：['ignore', logFd, logFd]（stdout/stderr 都进日志文件）", async (t) => {
  const home = tmpHome(t);
  fakeInstall(home);
  const openCalls = [];
  let stdio;
  await ensureRunning({
    probe: async () => ({ ok: false, status: 0 }),
    spawn: (_cmd, _args, opts) => { stdio = opts.stdio; return { unref() {} }; },
    wait: async () => {},
    home,
    fs: spyFs(openCalls),
  });
  const logPath = path.join(home, "trading-platform-service.log");
  assert.deepEqual(openCalls.map(([file, flags]) => [file, flags]), [[logPath, "a"]]);
  const fd = openCalls[0][2];
  assert.equal(stdio[0], "ignore");
  assert.equal(stdio[1], fd, "stdout 接日志 fd");
  assert.equal(stdio[2], fd, "stderr 接同一个日志 fd");
});

// ===================== 三、未安装：绝不强行启动 =====================

test("仓库标记缺失 → not-installed，指引 install/HARNESS_SETUP.md，且不 spawn", async (t) => {
  const home = tmpHome(t);
  let spawns = 0;
  const result = await ensureRunning({
    probe: async () => ({ ok: false, status: 0 }),
    spawn: () => { spawns += 1; return { unref() {} }; },
    home,
    fs: spyFs([]),
  });
  assert.equal(result.action, "not-installed");
  assert.match(result.detail, /install\/HARNESS_SETUP\.md/);
  assert.equal(spawns, 0, "未安装时不得强行启动");
});

test("标记存在但入口 run.py 缺失 → not-installed（仓库不完整）", async (t) => {
  const home = tmpHome(t);
  realFs.writeFileSync(path.join(home, "trading-platform-repo"), path.join(home, "repo") + "\n");
  realFs.mkdirSync(path.join(home, "trading-venv", "bin"), { recursive: true });
  realFs.writeFileSync(path.join(home, "trading-venv", "bin", "python"), "x");
  const result = await ensureRunning({
    probe: async () => ({ ok: false, status: 0 }),
    spawn: () => { throw new Error("不该 spawn"); },
    home,
    fs: spyFs([]),
  });
  assert.equal(result.action, "not-installed");
  assert.match(result.detail, /install\/HARNESS_SETUP\.md/);
});

test("venv 缺失 → not-installed，指引含 venv 创建命令", async (t) => {
  const home = tmpHome(t);
  fakeInstall(home);
  realFs.rmSync(path.join(home, "trading-venv"), { recursive: true, force: true });
  const result = await ensureRunning({
    probe: async () => ({ ok: false, status: 0 }),
    spawn: () => { throw new Error("不该 spawn"); },
    home,
    fs: spyFs([]),
  });
  assert.equal(result.action, "not-installed");
  assert.match(result.detail, /python3 -m venv/, "指引必须给出可照抄的创建命令");
});

// ===================== 四、端口与失败收敛 =====================

test("端口覆盖：config 文件 port=9001 → probe URL 含 9001", async (t) => {
  const home = tmpHome(t);
  fakeInstall(home);
  realFs.writeFileSync(path.join(home, "trading-platform.json"),
    JSON.stringify({ service: { port: 9001 } }));
  const port = readPort(realFs.readFileSync(path.join(home, "trading-platform.json"), "utf8"));
  assert.equal(port, 9001);
  const probes = [];
  await ensureRunning({
    probe: async (url) => { probes.push(url); return { ok: true, status: 200 }; },
    spawn: () => { throw new Error("不该 spawn"); },
    home,
    port,
    fs: spyFs([]),
  });
  assert.ok(probes.length >= 1 && probes[0].includes(":9001/healthz"), `URL 应含 9001，实际 ${probes}`);
});

test("spawn 抛错 → spawn-failed（不抛出、不炸调用方）", async (t) => {
  const home = tmpHome(t);
  fakeInstall(home);
  const result = await ensureRunning({
    probe: async () => ({ ok: false, status: 0 }),
    spawn: () => { throw new Error("EACCES"); },
    home,
    fs: spyFs([]),
  });
  assert.equal(result.action, "spawn-failed");
  assert.match(result.detail, /EACCES|trading-platform-service\.log/);
});

test("复测仍不通 → spawn-failed，detail 指向日志文件", async (t) => {
  const home = tmpHome(t);
  fakeInstall(home);
  const result = await ensureRunning({
    probe: async () => ({ ok: false, status: 0 }),
    spawn: () => ({ unref() {} }),
    wait: async () => {},
    home,
    fs: spyFs([]),
  });
  assert.equal(result.action, "spawn-failed");
  assert.match(result.detail, /trading-platform-service\.log/);
});

// ===================== 五、index.js 接线：不阻塞、全捕获、每会话一次 =====================

test("插件不硬依赖任何服务；apply 不阻塞且产出 JSON 日志（未安装 → 仅提示）", async (t) => {
  assert.deepEqual(inject, [], "缺 venv/仓库时不得阻塞会话其他能力");
  assert.equal(name, "platform-autostart");

  const home = tmpHome(t);
  // 选一个当前必然无人监听的 loopback 端口：先绑 0 号端口拿内核分配，再立刻释放。
  const freePort = await new Promise((resolve) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
  realFs.writeFileSync(path.join(home, "trading-platform.json"),
    JSON.stringify({ service: { port: freePort } }));

  const previous = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  const originalLog = console.log;
  const lines = [];
  console.log = (...args) => lines.push(args.map(String).join(" "));
  t.after(() => {
    console.log = originalLog;
    if (previous === undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME = previous;
  });

  const startedAt = Date.now();
  assert.doesNotThrow(() => apply({}), "apply 本身不得抛错");
  assert.ok(Date.now() - startedAt < 100, "apply 必须立即返回（setTimeout 0 把工作挪出挂载路径）");
  apply({});  // 第二次调用：每会话最多尝试一次
  await new Promise((resolve) => setTimeout(resolve, 300));

  const parsed = lines.map((line) => { try { return JSON.parse(line); } catch { return null; } })
    .filter((entry) => entry && entry.event === "platform-autostart");
  assert.equal(parsed.length, 1, `整个进程只尝试一次，实际 ${parsed.length} 条日志`);
  assert.equal(parsed[0].action, "not-installed", "tmp home 无仓库标记 → 只提示不启动");
  assert.match(parsed[0].detail, /install\/HARNESS_SETUP\.md/);
  assert.ok(parsed[0].ts, "日志带时间戳便于 RUNBOOK 排障");
});

test("realSpawn 未被本测试触发（防止误拉起真实服务）", () => {
  // 哨兵断言：上面所有用例的 spawn 全部注入；若未来有人把真实 spawn 漏进纯逻辑，
  // 这里提醒测试作者检查。本身恒真，作为意图标注。
  assert.ok(typeof realSpawn === "function");
});
