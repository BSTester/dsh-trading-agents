# WP6 独立服务化 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。
> **全局约定**：见 `2026-09-14-platform-plan-index.md`。**本包含前端 UI 任务，2.1 UI 文案规范强制执行**。
> **规格**：`docs/superpowers/specs/2026-09-15-wp6-standalone-service.md`（验收标准 = 规格 §六；工具面清单 = 规格 §三；通道分级规则 = 规格 §3.2 ⚠ 注）。

**目标：** 独立 Node 服务进程（MCP streamable-http 25 工具 + HTTP API + 静态托管）+ Ant Design Pro 形态前端（11 页签）+ preset 行替换与审批回归矩阵。

**架构：** 服务进程复用 `plugins/workbench/src` 的 `WorkbenchStore` + `createRpcHandler`（与 Harness 面板同一 handler 实例 → 对等性由结构保证）；MCP 用 `@modelcontextprotocol/sdk`（Node 侧，会话模式 + JSON 响应）；前端 Vite + antd5 + `@ant-design/pro-components`（用户已确认形态）；trading_core/daemon/指令目录协议不动。

**技术栈：** Node ≥ 22、`@modelcontextprotocol/sdk@^1.30.0`、`zod@^3.23`、`node:http`（零 Web 框架）；`vite@^6`、`@vitejs/plugin-react@^4.6`、`react@^18.3`、`antd@^5.27`、`@ant-design/pro-components@^2.8.10`。版本以 lockfile 为准。

**插入点锚点（2026-09-15 核实）：**
- `plugins/workbench/src/endpoints.js`（ENDPOINTS 20 项，只读引用，不改）；
- `plugins/workbench/src/rpc.js:64` `createRpcHandler(store, deps)`（服务复用入口）；
- `plugins/workbench/src/store.js`（`WorkbenchStore`/`switchMode`/`cancelRun`/`cancelStaleRuns`/`pruneAbandonedRuns`/`read()`/`file`）；
- `plugins/engine/src/policy.js`（审批链 A1/A2 锚，不改）；
- `agent.cordis.yml:30,32`（persona 两条）与文件末尾（新增 MCP 行）；`preset.yml:1-2`（描述行）；`plugins/workbench/cordis.patch.yml`（只加注释）。

---

### 任务 0：依赖引导与锁定测试

**文件：** 创建 `platform/package.json`、`platform/.gitignore`、`platform/tests/wp6-locks.test.mjs`、`tests/test_core_wp6_locks.py`（先失败：引用尚未创建的模块则本任务先建 `platform/server/config.mjs` 骨架——见步骤 3）

- [ ] **步骤 1：创建 `platform/package.json`**

```json
{
  "name": "@bstester/quant-platform-service",
  "private": true,
  "type": "module",
  "version": "0.1.0",
  "description": "量化平台独立服务：MCP 工具面（=工作台全功能）+ HTTP API + 前端托管（WP6）",
  "scripts": {
    "start": "node server/start.mjs",
    "test": "node --test tests/*.test.mjs",
    "web:build": "npm --prefix web run build"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.30.0",
    "zod": "^3.23.8"
  }
}
```

- [ ] **步骤 2：创建 `platform/.gitignore`（只忽略依赖与构建产物，lockfile 必须提交）**

```
node_modules/
web/node_modules/
web/dist/
```

- [ ] **步骤 3：创建 `platform/server/config.mjs`（先提交骨架，测试才能 FAIL→PASS）**

```js
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
```

- [ ] **步骤 4：安装依赖** `npm install --prefix platform`（网络一次；`platform/package-lock.json` 必须提交）
- [ ] **步骤 5：创建 `platform/tests/wp6-locks.test.mjs`（此时 FAIL：manifest 不存在）**

```js
// WP6 锁定测试（规格 §3.7）：工具面总数/名单、端点对等、能力黑名单、服务常量。
import test from "node:test";
import assert from "node:assert/strict";
import { ENDPOINT_TOOLS, ADMIN_TOOLS, TOOL_COUNT, TOOL_NAME_BLACKLIST } from "../server/manifest.mjs";
import { ENDPOINTS } from "../../plugins/workbench/src/endpoints.js";
import { DEFAULTS } from "../server/config.mjs";

test("wp6 工具面：恰 25 个工具（20 端点 + 5 维护）", () => {
  assert.equal(ENDPOINT_TOOLS.length, 20);
  assert.equal(ADMIN_TOOLS.length, 5);
  assert.equal(TOOL_COUNT, 25);
});

test("wp6 端点对等：工具端点集 ≡ workbench ENDPOINTS", () => {
  assert.deepEqual(ENDPOINT_TOOLS.map((tool) => tool.endpoint).sort(), [...ENDPOINTS].sort());
});

test("wp6 能力黑名单：无 exec/shell/file/token 类工具", () => {
  const names = [...ENDPOINT_TOOLS, ...ADMIN_TOOLS].map((tool) => tool.name);
  for (const banned of TOOL_NAME_BLACKLIST) {
    assert.ok(!names.some((name) => name.toLowerCase().includes(banned)), banned);
  }
});

test("wp6 服务常量锁定：8397/127.0.0.1/无默认 token", () => {
  assert.equal(DEFAULTS.port, 8397);
  assert.equal(DEFAULTS.host, "127.0.0.1");
  assert.equal(DEFAULTS.token, null);
});
```

- [ ] **步骤 6：创建 `tests/test_core_wp6_locks.py`（跨语言常量锁定）**

```python
"""WP6 依赖锁定：服务常量与两把口令常量的跨语言一致性。全部离线。"""
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Wp6Locks(unittest.TestCase):
    def test_confirmation_passwords_unchanged(self):
        store_js = (ROOT / "plugins" / "workbench" / "src" / "store.js").read_text(encoding="utf-8")
        rpc_js = (ROOT / "plugins" / "workbench" / "src" / "rpc.js").read_text(encoding="utf-8")
        self.assertIn("确认实盘", store_js)
        self.assertIn("确认执行", rpc_js)

    def test_service_defaults(self):
        config = (ROOT / "platform" / "server" / "config.mjs").read_text(encoding="utf-8")
        self.assertIn("8397", config)
        self.assertIn("trading-platform.json", config)
        manifest = (ROOT / "platform" / "server" / "manifest.mjs").read_text(encoding="utf-8")
        self.assertIn("trading/live-switch-web-only", manifest)

    def test_command_whitelist_still_five(self):
        sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
        from trading_core import commands  # noqa: E402
        self.assertEqual(set(commands.COMMANDS),
                         {"execute_plan", "cancel_plan", "kill", "unkill", "run_job"})


if __name__ == "__main__":
    unittest.main()
```

（顶部 `from pathlib import Path`。）

- [ ] **步骤 7：运行 `node --test platform/tests/wp6-locks.test.mjs` → FAIL（manifest.mjs 缺失）**
- [ ] **步骤 8：本任务只到 FAIL 为止（manifest 在任务 1 实现）；先提交**

```bash
git add platform/package.json platform/.gitignore platform/server/config.mjs platform/tests/wp6-locks.test.mjs tests/test_core_wp6_locks.py platform/package-lock.json
git commit -m "feat(platform): WP6 服务脚手架与锁定测试（config/常量/工具面契约）"
```

### 任务 1：工具面清单 manifest.mjs（25 工具，规格 §3）

**文件：** 创建 `platform/server/manifest.mjs`；测试：`platform/tests/wp6-locks.test.mjs`（转 PASS）

- [ ] **步骤 1：完整实现**

```js
// WP6 工具面清单（单一事实来源，规格 §3）。
// 20 个端点工具与面板共用同一个 createRpcHandler 实例（对等性由结构保证）；
// 5 个维护工具来自 workbench_admin.mjs 的能力提升。
// 通道分级（规格 §3.2 ⚠）：switch_mode 在本层封死 live——模型可见的通道不得切换实盘，
// live 切换只能由用户在独立 Web 输入口令完成；plan_execute 保留口令入口（规格 §8.3 对话侧等价入口）。
import { z } from "zod";

const REFRESH = { refresh: z.boolean().optional().describe("true 时绕过 TTL 缓存强制重取") };

function endpointTool(name, endpoint, description, input) {
  return {
    kind: "endpoint", name, endpoint, description,
    input: { ...input, ...REFRESH },
    async run({ handle, args }) {
      const { refresh: force, ...rest } = args ?? {};
      return handle(endpoint, force === true ? { ...rest, _refresh: true } : rest);
    },
  };
}

async function envelope(promise) {
  try {
    return { ok: true, value: await promise };
  } catch (error) {
    return { ok: false, error: { code: "trading/invalid-operation",
      message: String(error?.message ?? error).slice(0, 300), details: {} } };
  }
}

export const ENDPOINT_TOOLS = [
  endpointTool("snapshot", "snapshot", "工作台全量快照：账户模式、研报、研究 run、量化预览、交易响应、去重订单事实（trade_summary）、在途调用与缺失端点清单。无券商数据时明确为空。", {}),
  {
    kind: "endpoint", name: "switch_mode", endpoint: "switch-mode",
    description: "切换账户模式，仅限 live→sim（回模拟盘）。sim→live 被本工具拒绝：实盘切换只能由用户在独立 Web（http://127.0.0.1:8397）输入口令「确认实盘」完成。切换模式不授权任何订单。",
    input: {
      mode: z.enum(["sim", "live"]).describe("目标模式（本工具只接受 sim）"),
      expected_mode: z.enum(["sim", "live"]).describe("调用方所见当前模式，防过期数据误切换"),
      confirmation: z.string().optional().describe("回 sim 无需口令；本工具不接受 live 切换"),
      refresh: z.boolean().optional(),
    },
    async run({ handle, args }) {
      if (args?.mode === "live") {
        return { ok: false, error: { code: "trading/live-switch-web-only",
          message: "实盘切换只能在独立 Web 由用户输入口令完成；模式切换不授权任何订单", details: {} } };
      }
      const { refresh: force, ...rest } = args ?? {};
      return handle("switch-mode", force === true ? { ...rest, _refresh: true } : rest);
    },
  },
  endpointTool("series", "series", "K 线序列（富途优先、降级如实标注）。", {
    ticker: z.string().describe("标的代码，如 SH.600519"),
    period: z.enum(["1m", "5m", "15m", "30m", "60m", "1d"]).optional().describe("默认 5m"),
    limit: z.number().int().optional().describe("20..2000，默认 300"),
  }),
  endpointTool("equity", "equity", "本地模拟台账权益曲线（不代表券商资产）。", {
    mode: z.enum(["sim", "live"]).optional(), window: z.number().int().optional(),
  }),
  endpointTool("positions", "positions", "券商真实持仓（按账户小计，不跨币种合并；失败账户列入 errors）。", {
    mode: z.enum(["sim", "live"]).optional(), window: z.number().int().optional(),
  }),
  endpointTool("correlation", "correlation", "持仓间相关性矩阵。", {
    tickers: z.array(z.string()).describe("标的列表"), window: z.number().int().optional(),
  }),
  endpointTool("sensitivity", "sensitivity", "策略参数敏感性矩阵。", {
    ticker: z.string().optional(), strategy: z.string().optional(), metric: z.string().optional(),
    fast_grid: z.array(z.number().int()).optional(), slow_grid: z.array(z.number().int()).optional(),
    buy_grid: z.array(z.number()).optional(), sell_grid: z.array(z.number()).optional(),
    start: z.string().optional(),
  }),
  endpointTool("risk", "risk", "风控配置与当前账户风险指标。", {}),
  endpointTool("trades", "trades", "本地模拟台账成交记录（sim 专属）。", {
    mode: z.enum(["sim", "live"]).optional(), limit: z.number().int().optional(),
  }),
  endpointTool("events", "events", "标的公告/事件时间线。", {
    ticker: z.string().describe("标的代码"), days: z.number().int().optional(),
  }),
  endpointTool("factors", "factors", "横截面因子打分表。", {
    tickers: z.array(z.string()).describe("标的列表"), window: z.number().int().optional(),
  }),
  endpointTool("ic", "ic", "因子 RankIC 序列。", {
    tickers: z.array(z.string()).describe("标的列表"), factor: z.string().optional(),
    forward: z.number().int().optional(), window: z.number().int().optional(),
  }),
  endpointTool("audit", "audit", "审计链：计划→订单→成交三级链路 + 信号/响应链路。", {}),
  endpointTool("sources", "sources", "数据源健康状态（渠道可用性与 as_of）。", {
    no_probe: z.boolean().optional().describe("true 时只读缓存不探测"),
  }),
  endpointTool("instrument", "instrument", "标的解析（名称/市场/整手等）。", {
    ticker: z.string().describe("标的代码"),
  }),
  endpointTool("quality", "quality", "单标的数据质量报告（缺口/复权）。", {
    ticker: z.string().describe("标的代码"),
  }),
  endpointTool("plan", "plan", "当前/历史执行计划：目标 vs 实际 diff、逐单风控预检、状态时间线；附当前账户模式。", {}),
  endpointTool("plan_execute", "plan-execute", "唯一受约束执行入口：execute=执行已冻结计划（live 需口令「确认执行」，用户须在对话中逐笔确认后由你携带）；cancel=取消计划；kill/unkill=风控总开关。返回 queued+nonce，状态用 plan 轮询。不等待执行结果。", {
    plan_hash: z.string().optional().describe("action=execute/cancel 时必填"),
    expected_mode: z.enum(["sim", "live"]).optional().describe("action=execute 时必填"),
    confirmation: z.string().optional().describe("live 执行必须为「确认执行」"),
    action: z.enum(["execute", "cancel", "kill", "unkill"]).optional().describe("默认 execute"),
  }),
  endpointTool("schedule", "schedule", "调度快照：daemon 心跳（>5 分钟即失联）、作业历史、kill/halt 状态。", {}),
  endpointTool("reconcile", "reconcile", "对账快照：最近差异、TCA 摘要、告警列表。", {}),
];

function hoursToMs(hours) { return Math.max(1, hours ?? 2) * 3600_000; }

export const ADMIN_TOOLS = [
  { kind: "admin", name: "admin_status", description: "工作台数据维护：数据文件路径与各类记录数量。",
    input: {},
    run: ({ store }) => { const state = store.read();
      return envelope(Promise.resolve({ file: store.file, runs: state.runs.length,
        reports: state.reports.length, previews: state.previews.length, activity: state.activity.length })); } },
  { kind: "admin", name: "admin_runs", description: "工作台数据维护：列出全部研究 run（状态/标的/模式/年龄分钟）。",
    input: {},
    run: ({ store }) => { const state = store.read();
      return envelope(Promise.resolve(state.runs.map((row) => {
        const started = Date.parse(row.started_at ?? "");
        return { id: row.id, status: row.status, ticker: row.ticker, mode: row.mode,
          age_minutes: Number.isFinite(started) ? Math.round((Date.now() - started) / 60000) : null };
      }))); } },
  { kind: "admin", name: "admin_cancel_run", description: "工作台数据维护：取消指定研究 run（标记 cancelled，保留记录）。先用 admin_runs 查 id。",
    input: { run_id: z.string().describe("run id") },
    run: ({ store, args }) => envelope(store.cancelRun(String(args.run_id))) },
  { kind: "admin", name: "admin_cancel_stale", description: "工作台数据维护：批量取消超时仍 running 的 run（默认 2 小时）。",
    input: { hours: z.number().optional().describe("阈值小时数，默认 2") },
    run: ({ store, args }) => envelope(store.cancelStaleRuns({ olderThanMs: hoursToMs(args?.hours) })) },
  { kind: "admin", name: "admin_prune_runs", description: "工作台数据维护：删除超时孤儿 run（无研报者；有研报的保留）。",
    input: { hours: z.number().optional().describe("阈值小时数，默认 2") },
    run: ({ store, args }) => envelope(store.pruneAbandonedRuns({ olderThanMs: hoursToMs(args?.hours) })) },
];

export const TOOL_NAME_BLACKLIST = Object.freeze(["exec", "shell", "file_read", "file_write", "token"]);
export const TOOL_COUNT = ENDPOINT_TOOLS.length + ADMIN_TOOLS.length;

/** 服务装配入口：handle=与面板同一的 createRpcHandler 产物；store=WorkbenchStore。 */
export function buildManifest({ handle, store }) {
  const context = { handle, store };
  return [...ENDPOINT_TOOLS, ...ADMIN_TOOLS].map(({ run, ...meta }) => ({
    ...meta, call: (args) => run({ ...context, args }),
  }));
}
```

- [ ] **步骤 2：`node --test platform/tests/wp6-locks.test.mjs` → 4 用例 PASS**
- [ ] **步骤 3：`node --test platform/tests/wp6-locks.test.mjs` 与 Python 锁定 `~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_wp6_locks -v` 全 PASS**（若 Python 文件保留了步骤 8 注记中的错误行，先删掉）
- [ ] **步骤 4：Commit**

```bash
git add platform/server/manifest.mjs
git commit -m "feat(platform): WP6 工具面清单（20 端点对等 + 5 维护 + live 通道分级）"
```

### 任务 2：HTTP 服务 service.mjs + 共享工具 util.mjs

**文件：** 创建 `platform/server/util.mjs`、`platform/server/service.mjs`；测试 `platform/tests/service.test.mjs`

- [ ] **步骤 1：`platform/server/util.mjs`**

```js
// 请求体收集与 JSON 响应的小工具（service/mcp 共用；上限 1MB）。
export function collectBody(req, limit = 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) { reject(new Error("payload too large")); req.destroy(); return; }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

export function sendJson(res, status, value) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(value));
}

export function unauthorized(res) {
  sendJson(res, 401, { ok: false, error: { code: "trading/unauthorized", message: "需要 Bearer token", details: {} } });
}

export function authorized(req, token) {
  if (!token) return true;
  return req.headers.authorization === `Bearer ${token}`;
}
```

- [ ] **步骤 2：失败测试 `platform/tests/service.test.mjs`**

```js
// HTTP 面单测（离线，临时 DSH_HOME）：白名单/方法/内容类型/token/静态兜底。
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, mkdir, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { WorkbenchStore } from "../../plugins/workbench/src/store.js";
import { createRpcHandler } from "../../plugins/workbench/src/rpc.js";
import { createService } from "../server/service.mjs";

async function withServer(t, { token = null, dist = null } = {}) {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-http-"));
  const previous = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  const store = new WorkbenchStore();
  const handle = createRpcHandler(store, {});
  const server = createService({ store, handle, mcp: async (req, res) => { res.writeHead(405); res.end(); }, config: { token }, dist: dist ?? path.join(home, "empty-dist"), endpoints: ["snapshot", "series"] });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  t.after(async () => {
    await new Promise((resolve) => server.close(resolve));
    if (previous === undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME = previous;
    await rm(home, { recursive: true, force: true });
  });
  return { url, store };
}

test("snapshot 经 /api/wb 可用；未知端点 404 envelope", async (t) => {
  const { url } = await withServer(t);
  const ok = await fetch(`${url}/api/wb/snapshot`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const okBody = await ok.json();
  assert.equal(ok.status, 200);
  assert.equal(okBody.ok, true);
  assert.ok(Array.isArray(okBody.value.endpoints));
  const missing = await fetch(`${url}/api/wb/not-an-endpoint`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  assert.equal(missing.status, 404);
});

test("方法/内容类型/载荷校验：405/415/400", async (t) => {
  const { url } = await withServer(t);
  assert.equal((await fetch(`${url}/api/wb/snapshot`)).status, 405);
  assert.equal((await fetch(`${url}/api/wb/snapshot`, { method: "POST", body: "{}" })).status, 415);
  assert.equal((await fetch(`${url}/api/wb/snapshot`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{oops" })).status, 400);
});

test("token 配置后 API 需要 Bearer；healthz 豁免", async (t) => {
  const { url } = await withServer(t, { token: "s3cret" });
  assert.equal((await fetch(`${url}/api/wb/snapshot`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).status, 401);
  assert.equal((await fetch(`${url}/api/wb/snapshot`, { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer s3cret" }, body: "{}" })).status, 200);
  assert.equal((await fetch(`${url}/healthz`)).status, 200);
});

test("静态托管：dist 文件可取，缺失回退 404（未构建时不崩）", async (t) => {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-dist-"));
  const dist = path.join(home, "dist");
  await mkdir(path.join(dist, "assets"), { recursive: true });
  await writeFile(path.join(dist, "index.html"), "<html>ok</html>");
  await writeFile(path.join(dist, "assets", "app.js"), "console.log(1)");
  const { url } = await withServer(t, { dist });
  const page = await fetch(`${url}/`);
  assert.equal(page.status, 200);
  assert.match(await page.text(), /ok/);
  const js = await fetch(`${url}/assets/app.js`);
  assert.equal(js.headers.get("content-type"), "text/javascript");
  const noDist = await fetch(`${url}/healthz`);
  assert.equal(noDist.status, 200);
  await rm(home, { recursive: true, force: true });
});
```

- [ ] **步骤 3：运行 `node --test platform/tests/service.test.mjs` → FAIL（service.mjs 不存在）**
- [ ] **步骤 4：实现 `platform/server/service.mjs`**

```js
// HTTP 面（规格 §4.6/§4.7）：POST /api/wb/<endpoint> + GET /healthz + 静态 dist + /mcp 委派。
// 零框架（node:http）；白名单外的一切都到不了 handler（规格 §5.1 A7/A8）。
// 认证边界：loopback 绑定 + 可选 Bearer token（service.token）；healthz 与静态文件豁免。
import { createServer } from "node:http";
import { createReadStream, existsSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { authorized, collectBody, sendJson, unauthorized } from "./util.mjs";

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
  ".ico": "image/x-icon", ".woff2": "font/woff2", ".map": "application/json",
};

function serveStatic(res, dist, urlPath, fallback = true) {
  const relative = decodeURIComponent(urlPath === "/" ? "/index.html" : urlPath);
  const target = path.normalize(path.join(dist, relative));
  if (!target.startsWith(dist)) {
    return sendJson(res, 403, { ok: false, error: { code: "trading/forbidden", message: "路径非法", details: {} } });
  }
  if (!existsSync(target) || statSync(target).isDirectory()) {
    if (!fallback) {
      return sendJson(res, 404, { ok: false, error: { code: "trading/not-found", message: "前端未构建（npm --prefix platform/web run build）", details: {} } });
    }
    return serveStatic(res, dist, "/index.html", false);  // SPA 路由兜底
  }
  res.writeHead(200, { "Content-Type": MIME[path.extname(target)] ?? "application/octet-stream" });
  createReadStream(target).pipe(res);
}

export function createService({ store, handle, mcp, config, dist, endpoints }) {
  const root = dist ?? path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "web", "dist");
  return createServer(async (req, res) => {
    const route = new URL(req.url ?? "/", "http://localhost").pathname;
    try {
      if (route === "/healthz") {
        return sendJson(res, 200, { ok: true, mode: store.readMode() });
      }
      if (route === "/mcp") {
        if (!authorized(req, config.token)) return unauthorized(res);
        return await mcp(req, res);
      }
      if (route.startsWith("/api/wb/")) {
        if (!authorized(req, config.token)) return unauthorized(res);
        if (req.method !== "POST") {
          return sendJson(res, 405, { ok: false, error: { code: "trading/method-not-allowed", message: "仅 POST", details: {} } });
        }
        const endpoint = route.slice("/api/wb/".length);
        if (!/^[a-z-]+$/.test(endpoint) || !endpoints.includes(endpoint)) {
          // 白名单外一律 404，不区分「格式错」与「未声明」（规格 §5.1 A7 封闭性）
          return sendJson(res, 404, { ok: false, error: { code: "trading/unknown-endpoint", message: `未知端点 ${endpoint}`, details: {} } });
        }
        const contentType = String(req.headers["content-type"] ?? "").split(";")[0].trim();
        if (contentType !== "application/json") {
          return sendJson(res, 415, { ok: false, error: { code: "trading/invalid-operation", message: "Expected application/json", details: {} } });
        }
        let payload;
        try {
          payload = JSON.parse((await collectBody(req)) || "{}");
        } catch (error) {
          return sendJson(res, 400, { ok: false, error: { code: "trading/invalid-operation", message: `请求体不是合法 JSON：${error.message}`, details: {} } });
        }
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
          return sendJson(res, 400, { ok: false, error: { code: "trading/invalid-operation", message: "Expected an object payload", details: {} } });
        }
        return sendJson(res, 200, await handle(endpoint, payload));
      }
      if (req.method === "GET") return serveStatic(res, root, route);
      return sendJson(res, 405, { ok: false, error: { code: "trading/method-not-allowed", message: "仅 GET", details: {} } });
    } catch (error) {
      if (res.headersSent) { res.end(); return; }
      return sendJson(res, 500, { ok: false, error: { code: "trading/internal", message: String(error?.message ?? error).slice(0, 300), details: {} } });
    }
  });
}
```

- [ ] **步骤 5：`node --test platform/tests/service.test.mjs` → 4 用例 PASS**
- [ ] **步骤 6：Commit**

```bash
git add platform/server/util.mjs platform/server/service.mjs platform/tests/service.test.mjs
git commit -m "feat(platform): HTTP API + 静态托管 + healthz（零框架，token 可选）"
```

### 任务 3：MCP 端点 mcp.mjs

**文件：** 创建 `platform/server/mcp.mjs`；测试 `platform/tests/mcp.test.mjs`

- [ ] **步骤 1：失败测试（in-process：SDK 客户端直连，S1–S3 的进程内版本）**

```js
// MCP 面（规格 §3.6/§5.2 S1-S3 in-process 版）：SDK 客户端 initialize → tools/list=25 → 调用与通道分级。
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { WorkbenchStore } from "../../plugins/workbench/src/store.js";
import { createRpcHandler } from "../../plugins/workbench/src/rpc.js";
import { createService } from "../server/service.mjs";
import { createMcpEndpoint } from "../server/mcp.mjs";
import { buildManifest, TOOL_COUNT } from "../server/manifest.mjs";

async function withMcpServer(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-mcp-"));
  const previous = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  const store = new WorkbenchStore();
  const handle = createRpcHandler(store, {});
  const manifest = buildManifest({ handle, store });
  const mcp = createMcpEndpoint({ manifest });
  const server = createService({ store, handle, mcp, config: {}, dist: path.join(home, "no-dist"), endpoints: ["snapshot", "series", "switch-mode", "plan-execute"] });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${server.address().port}/mcp`;
  t.after(async () => {
    await new Promise((resolve) => server.close(resolve));
    if (previous === undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME = previous;
    await rm(home, { recursive: true, force: true });
  });
  return { url, store };
}

async function connect(url) {
  const client = new Client({ name: "wp6-test", version: "0.0.0" });
  await client.connect(new StreamableHTTPClientTransport(new URL(url)));
  return client;
}

test("S1 initialize + tools/list 恰 25 且与 manifest 一致", async (t) => {
  const { url } = await withMcpServer(t);
  const client = await connect(url);
  t.after(() => client.close());
  const tools = await client.listTools();
  assert.equal(tools.tools.length, TOOL_COUNT);
  assert.ok(tools.tools.some((tool) => tool.name === "plan_execute"));
  assert.ok(tools.tools.every((tool) => tool.name !== "exec"));
});

test("S2 snapshot 可调；switch_mode 的 live 通道被封死（带口令也拒）", async (t) => {
  const { url, store } = await withMcpServer(t);
  const client = await connect(url);
  t.after(() => client.close());
  const snapshot = await client.callTool({ name: "snapshot", arguments: {} });
  assert.equal(snapshot.isError, false);
  const value = JSON.parse(snapshot.content[0].text);
  assert.equal(value.ok, true);
  const live = await client.callTool({ name: "switch_mode",
    arguments: { mode: "live", expected_mode: "sim", confirmation: "确认实盘" } });
  assert.equal(JSON.parse(live.content[0].text).error.code, "trading/live-switch-web-only");
  assert.equal(store.readMode(), "sim");
});

test("S3 业务失败走 ok:false envelope（isError=false）；HTTP 与 MCP snapshot 同值", async (t) => {
  const { url } = await withMcpServer(t);
  const client = await connect(url);
  t.after(() => client.close());
  const bad = await client.callTool({ name: "series", arguments: { ticker: "BAD TICKER!" } });
  assert.equal(bad.isError, false);
  const envelope = JSON.parse(bad.content[0].text);
  assert.equal(envelope.ok, false);
  const viaMcp = await client.callTool({ name: "snapshot", arguments: {} });
  const http = await fetch(url.replace(/\/mcp$/, "/api/wb/snapshot"),
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  // snapshot 含 generated_at（每次调用都变），同值断言比较稳定字段
  const viaMcpValue = JSON.parse(viaMcp.content[0].text).value;
  const httpValue = (await http.json()).value;
  assert.equal(viaMcpValue.mode, httpValue.mode);
  assert.deepEqual(viaMcpValue.endpoints, httpValue.endpoints);
});
```

- [ ] **步骤 2：运行 → FAIL（mcp.mjs 缺失）**
- [ ] **步骤 3：实现 `platform/server/mcp.mjs`**

```js
// MCP streamable-http 端点（规格 §3.6）：每个会话一对 server+transport（SDK 官方会话模式），
// JSON 响应（enableJsonResponse，无 SSE 依赖）。工具面=buildManifest 产物，
// 与 HTTP 面共用同一 handle——两条通道对同一 payload 结果一致（规格 §5.2 R6/S3）。
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { collectBody, sendJson } from "./util.mjs";

export function createMcpEndpoint({ manifest }) {
  const sessions = new Map();

  function buildServer() {
    const server = new McpServer({ name: "quantwb", version: "0.1.0" });
    for (const tool of manifest) {
      server.registerTool(tool.name, { description: tool.description, inputSchema: tool.input },
        async (args) => {
          try {
            const result = await tool.call(args ?? {});
            return { content: [{ type: "text", text: JSON.stringify(result) }] };
          } catch (error) {
            // handler 之外的程序异常才标 isError；业务失败走 ok:false envelope（规格 §3.2 错误语义）
            return { isError: true, content: [{ type: "text", text: JSON.stringify({ ok: false,
              error: { code: "trading/tool-failed", message: String(error?.message ?? error).slice(0, 300), details: {} } }) }] };
          }
        });
    }
    return server;
  }

  return async function mcpRequest(req, res) {
    const sessionId = req.headers["mcp-session-id"];
    const known = sessionId ? sessions.get(sessionId) : undefined;
    try {
      if (req.method === "DELETE") {
        if (known) {
          sessions.delete(sessionId);
          await known.transport.close().catch(() => {});
        }
        res.writeHead(204);
        res.end();
        return;
      }
      if (req.method !== "POST") {
        res.writeHead(405, { Allow: "POST, DELETE" });
        res.end();
        return;
      }
      const body = await collectBody(req);
      let entry = known;
      if (!entry) {
        const transport = new StreamableHTTPServerTransport({
          sessionIdGenerator: () => randomUUID(),
          enableJsonResponse: true,
          onsessioninitialized: (id) => sessions.set(id, { server, transport }),
        });
        const server = buildServer();
        transport.onclose = () => {
          for (const [id, item] of sessions) if (item.transport === transport) sessions.delete(id);
        };
        await server.connect(transport);
        entry = { server, transport };
      }
      await entry.transport.handleRequest(req, res, body);
    } catch (error) {
      if (!res.headersSent) {
        sendJson(res, 500, { ok: false, error: { code: "trading/internal", message: String(error?.message ?? error).slice(0, 300), details: {} } });
      } else {
        res.end();
      }
    }
  };
}
```

- [ ] **步骤 4：`node --test platform/tests/mcp.test.mjs` → 3 用例 PASS**（SDK 客户端会真实走 initialize 握手——这就是协议级回归）
- [ ] **步骤 5：Commit**

```bash
git add platform/server/mcp.mjs platform/tests/mcp.test.mjs
git commit -m "feat(platform): MCP streamable-http 端点（会话模式，与 HTTP 同 handler）"
```

### 任务 4：组装入口 start.mjs + 进程级冒烟 smoke.test.mjs

**文件：** 创建 `platform/server/start.mjs`；测试 `platform/tests/smoke.test.mjs`

- [ ] **步骤 1：`platform/server/start.mjs`**

```js
#!/usr/bin/env node
// 量化平台独立服务组装入口（规格 §二）：
//   store+deps → createRpcHandler（一份）→ HTTP API + MCP + 静态托管。
// 启动：node platform/server/start.mjs；配置：~/.dsh/trading-platform.json 的 service 节。
import { WorkbenchStore } from "../../plugins/workbench/src/store.js";
import { createRpcHandler } from "../../plugins/workbench/src/rpc.js";
import { createSeriesProvider } from "../../plugins/workbench/src/series.js";
import { createAnalyticsProvider } from "../../plugins/workbench/src/analytics.js";
import { ENDPOINTS } from "../../plugins/workbench/src/endpoints.js";
import { loadConfig } from "./config.mjs";
import { createService } from "./service.mjs";
import { createMcpEndpoint } from "./mcp.mjs";
import { buildManifest } from "./manifest.mjs";

const config = loadConfig();
const store = new WorkbenchStore();
const deps = { fetchSeries: createSeriesProvider(), analytics: createAnalyticsProvider() };
const handle = createRpcHandler(store, deps);   // 一份 handler：HTTP 与 MCP 共享（缓存也共享）
const manifest = buildManifest({ handle, store });
const server = createService({ store, handle, mcp: createMcpEndpoint({ manifest }), config, endpoints: ENDPOINTS });

server.listen(config.port, config.host, () => {
  const { port } = server.address();
  console.log(JSON.stringify({
    ok: true, service: "quant-platform",
    url: `http://${config.host}:${port}`,
    mcp: `http://${config.host}:${port}/mcp`,
    tools: manifest.length,
    auth: config.token ? "token" : "loopback-only",
  }, null, 0));
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
```

- [ ] **步骤 2：失败测试 `platform/tests/smoke.test.mjs`（起真实子进程，S1–S4 全链路）**

```js
// 进程级冒烟（规格 §5.2 S 系列）：spawn 真实 start.mjs（临时 DSH_HOME）→ SDK 客户端全链路。
import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

test("S1-S4 真实进程：initialize/tools=25/通道分级/HTTP 同值", async (t) => {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-smoke-"));
  const child = spawn(process.execPath, ["server/start.mjs"], {
    cwd: path.resolve(import.meta.dirname, ".."),
    env: { ...process.env, DSH_HOME: home, TRADING_SERVICE_PORT: "0" },
    stdio: ["ignore", "pipe", "inherit"],
  });
  t.after(async () => {
    child.kill("SIGTERM");
    await rm(home, { recursive: true, force: true });
  });
  const ready = new Promise((resolve, reject) => {
    child.stdout.on("data", (chunk) => {
      const line = String(chunk).split("\n").find((row) => row.startsWith("{"));
      if (line) { try { resolve(JSON.parse(line)); } catch (error) { reject(error); } }
    });
    child.on("exit", () => reject(new Error("服务进程提前退出")));
  });
  const boot = await Promise.race([ready, new Promise((_, reject) =>
    setTimeout(() => reject(new Error("服务 10s 未就绪")), 10_000))]);
  assert.equal(boot.ok, true);
  assert.equal(boot.tools, 25);

  const client = new Client({ name: "wp6-smoke", version: "0.0.0" });
  await client.connect(new StreamableHTTPClientTransport(new URL(boot.mcp)));
  t.after(() => client.close());
  const tools = await client.listTools();
  assert.equal(tools.tools.length, 25);

  const snapshot = await client.callTool({ name: "snapshot", arguments: {} });
  assert.equal(JSON.parse(snapshot.content[0].text).ok, true);

  const live = await client.callTool({ name: "switch_mode",
    arguments: { mode: "live", expected_mode: "sim", confirmation: "确认实盘" } });
  assert.equal(JSON.parse(live.content[0].text).error.code, "trading/live-switch-web-only");

  // plan_execute 只排队不校验计划存在（校验在 daemon 执行链）；断言 queued 业务成功
  const queued = await client.callTool({ name: "plan_execute",
    arguments: { plan_hash: "nope", expected_mode: "sim", confirmation: "确认执行" } });
  assert.equal(JSON.parse(queued.content[0].text).value.queued, true);

  const http = await fetch(`${boot.url}/api/wb/snapshot`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const viaMcp = await client.callTool({ name: "snapshot", arguments: {} });
  // snapshot 含 generated_at（每次调用都变），同值断言比较稳定字段
  assert.equal((await http.json()).value.mode, JSON.parse(viaMcp.content[0].text).value.mode);
});
```

（注意：`plan_execute` 只写指令文件、不校验 plan_hash 存在——校验在 daemon 执行链。冒烟断言的是排队成功 `{queued:true}`。）

- [ ] **步骤 3：`TRADING_SERVICE_PORT=0` 支持**：任务 0 的 `config.mjs` 已读该环境变量；端口 0 时 `server.address().port` 为实际端口，start.mjs 打印的 URL 已是真实端口——直接可用
- [ ] **步骤 4：运行 `node --test platform/tests/smoke.test.mjs` → PASS**
- [ ] **步骤 5：Commit**

```bash
git add platform/server/start.mjs platform/tests/smoke.test.mjs
git commit -m "feat(platform): 服务组装入口与进程级 MCP 冒烟"
```

### 任务 5：审批回归 Node 矩阵（R1–R5，规格 §5.2）

**文件：** 创建 `tests/wp6-approval-regression.test.mjs`（根 tests 目录，随既有 `node --test tests/*.test.mjs` 全量跑）

- [ ] **步骤 1：完整测试**

```js
// WP6 审批回归（规格 §5.2 R 系列，全部离线）：A1/A2 策略链、A3 双保险+通道分级、
// A4 执行窄门、A7 面封闭。任何一条失败 → WP6 验收失败，禁止放宽断言。
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { WorkbenchStore } from "../plugins/workbench/src/store.js";
import { createRpcHandler } from "../plugins/workbench/src/rpc.js";
import { installTradingPolicy } from "../plugins/engine/src/policy.js";
import { ENDPOINTS } from "../plugins/workbench/src/endpoints.js";
import { ENDPOINT_TOOLS, ADMIN_TOOLS, TOOL_COUNT, TOOL_NAME_BLACKLIST, buildManifest } from "../platform/server/manifest.mjs";

async function inTempHome(t) {
  const home = await mkdtemp(path.join(os.tmpdir(), "wp6-approval-"));
  const previous = process.env.DSH_HOME;
  process.env.DSH_HOME = home;
  t.after(async () => {
    if (previous === undefined) delete process.env.DSH_HOME; else process.env.DSH_HOME = previous;
    await rm(home, { recursive: true, force: true });
  });
  return home;
}

function fakeHarness(store) {
  const guards = [];
  const hooks = {};
  const ctx = {
    tradingWorkbench: store,
    tools: { guard: (fn) => guards.push(fn) },
    on: (event, fn) => { (hooks[event] ??= []).push(fn); },
  };
  return { ctx, guards, hooks };
}

const execOf = (name) => ({ name, arguments: {}, agent: { session: { id: "s1" } } });

test("R1 模式互斥：sim 拒 live 类工具；live 拒 sim 类工具", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  const { ctx, guards } = fakeHarness(store);
  installTradingPolicy(ctx);
  assert.equal(guards.length, 1);
  assert.match(String(guards[0](execOf("mcp__futu__account_positions"))), /账户模式/);
  assert.equal(guards[0](execOf("mcp__futu__sim_trade_position_list")), undefined);
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.match(String(guards[0](execOf("mcp__futu__sim_trade_position_list"))), /账户模式/);
  assert.equal(guards[0](execOf("mcp__futu__account_positions")), undefined);
});

// R2 已按 2026-09-15 修订（规格 §5.1 A2）：实盘写操作走**业务确认**，
// 不返回 ask —— ask 在 full-access（policy="never"）下会被 approval.decide()
// 直接 rejected，表现为"用户拒绝了"而实际没人被问过。
test("R2 live 写操作走业务确认而非原生审批；sim 写不确认", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  const { ctx, hooks } = fakeHarness(store);
  installTradingPolicy(ctx);
  const next = async () => ({ kind: "allow" });
  const settling = hooks["tools/pre-execute"][0](
    execOf("mcp__futu__trading_modify_order", { acc_id: "A1", market: 1, order_id: "1", qty: 100 }), next);
  await new Promise((r) => setTimeout(r, 20));
  const pending = store.confirmationView();
  assert.ok(pending, "实盘写操作必须产生待确认");
  assert.equal(pending.operation, "改单");
  store.decideConfirmation({ id: pending.id, decision: "approved" });
  const decision = await settling;
  assert.notEqual(decision.kind, "ask", "不能返回 ask：那会受会话审批档位影响");
  assert.equal(decision.kind, "allow");
  const sim = await hooks["tools/pre-execute"][0](execOf("mcp__futu__sim_trade_place_order"), next);
  assert.equal(sim.kind, "allow");
  assert.equal(store.confirmationView(), null, "sim 写操作不应产生待确认");
});

test("R3 switch_mode：口令/expected_mode/租约/MCP 通道分级", async (t) => {
  await inTempHome(t);
  const store = new WorkbenchStore();
  const handle = createRpcHandler(store, {});
  let result = await handle("switch-mode", { mode: "live", expected_mode: "sim" });
  assert.equal(result.ok, false);
  assert.match(result.error.message, /确认实盘/);
  result = await handle("switch-mode", { mode: "live", expected_mode: "sim", confirmation: "确认" });
  assert.equal(result.ok, false);
  result = await handle("switch-mode", { mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.equal(result.ok, true);
  assert.equal(result.value.order_authorized, false);
  result = await handle("switch-mode", { mode: "sim", expected_mode: "sim" });   // 过期期望
  assert.equal(result.ok, false);
  const release = store.enterBrokerCall("live");                                  // 在途租约
  result = await handle("switch-mode", { mode: "sim", expected_mode: "live" });
  assert.equal(result.ok, false);
  assert.match(result.error.message, /账户调用/);
  release();
  const tool = buildManifest({ handle, store }).find((row) => row.name === "switch_mode");
  const viaMcp = await tool.call({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  assert.equal(viaMcp.ok, false);
  assert.equal(viaMcp.error.code, "trading/live-switch-web-only");
  const backToSim = await tool.call({ mode: "sim", expected_mode: "live" });
  assert.equal(backToSim.ok, true);
});

test("R4 plan_execute：live 口令、queued nonce、指令文件不含口令、kill/unkill 映射", async (t) => {
  const home = await inTempHome(t);
  const store = new WorkbenchStore();
  const handle = createRpcHandler(store, {});
  await store.switchMode({ mode: "live", expected_mode: "sim", confirmation: "确认实盘" });
  let result = await handle("plan-execute", { plan_hash: "h1", expected_mode: "live" });
  assert.equal(result.ok, false);
  assert.match(result.error.message, /确认执行/);
  result = await handle("plan-execute", { plan_hash: "h1", expected_mode: "live", confirmation: "确认执行" });
  assert.equal(result.ok, true);
  assert.equal(result.value.queued, true);
  assert.ok(result.value.nonce);

  const dir = path.join(home, "trading-commands", "pending");
  const bodies = async () => Promise.all((await readdir(dir)).filter((f) => f.endsWith(".json"))
    .map(async (f) => JSON.parse(await readFile(path.join(dir, f), "utf8"))));
  let all = await bodies();
  assert.ok(all.some((b) => b.type === "execute_plan" && b.plan_hash === "h1" && b.expected_mode === "live"));
  assert.ok(all.every((b) => !("confirmation" in b)));   // 口令只在服务端校验，绝不落指令文件

  await handle("plan-execute", { action: "kill" });
  await handle("plan-execute", { action: "unkill" });
  all = await bodies();
  assert.ok(all.some((b) => b.type === "kill"));
  assert.ok(all.some((b) => b.type === "unkill"));
  result = await handle("plan-execute", { action: "sell_all" });
  assert.equal(result.ok, false);                        // 白名单外动作
  result = await handle("plan-execute", { plan_hash: "h1", expected_mode: "sim" });  // 模式已变化
  assert.equal(result.ok, false);
});

test("R5 工具面封闭：恰 25、端点对等、无黑名单能力", () => {
  assert.equal(TOOL_COUNT, 25);
  assert.deepEqual(ENDPOINT_TOOLS.map((row) => row.endpoint).sort(), [...ENDPOINTS].sort());
  const names = [...ENDPOINT_TOOLS, ...ADMIN_TOOLS].map((row) => row.name);
  for (const banned of TOOL_NAME_BLACKLIST) {
    assert.ok(!names.some((name) => name.toLowerCase().includes(banned)), banned);
  }
});
```

- [ ] **步骤 2：`node --test tests/wp6-approval-regression.test.mjs` → 5 用例 PASS**（若 R3 `order_authorized` 字段名与 store 实际返回不符，以 `store.switchMode` 实际返回为准修**测试期望**并在验收记录披露；实现一律不改）
- [ ] **步骤 3：全量回归不破坏既有套件：`node --test tests/*.test.mjs` 全绿**
- [ ] **步骤 4：Commit**

```bash
git add tests/wp6-approval-regression.test.mjs
git commit -m "test: WP6 审批回归 Node 矩阵（R1-R5）"
```

### 任务 6：审批回归 Python 套件（P1–P3）

**文件：** 创建 `tests/test_core_wp6_approval.py`

- [ ] **步骤 1：完整测试**

```python
"""WP6 审批回归（Python 侧，规格 §5.2 P 系列）：kill 联动、白名单不变量、服务常量。全部离线。"""
import sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
from trading_core import commands, risk  # noqa: E402


class Wp6Approval(unittest.TestCase):
    def test_p1_kill_file_blocks_execution_chain(self):
        """kill 文件存在 → 风控规则 1 拒单（daemon 消费 execute_plan 的最终兜底）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        kill = Path(tmp.name) / "trading-kill"
        kill.write_text("", encoding="utf-8")
        verdict = risk.pre_trade_checks(
            {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 1.0, "mode": "SIM",
             "plan_id": "P", "plan_hash": "h", "stop_dist": 0.1},
            {"mode": "SIM", "kill_path": str(kill), "equity": 1e6, "positions_value": {},
             "positions_count": 0, "day_pnl_pct": 0.0, "is_trading_day": True,
             "config": {"risk_per_trade": 0.01, "max_positions": 5,
                        "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03},
             "plan_hash": "h", "plan_status": "frozen"})
        self.assertFalse(verdict.allowed)

    def test_p2_command_whitelist_and_nonce_idempotent(self):
        self.assertEqual(set(commands.COMMANDS),
                         {"execute_plan", "cancel_plan", "kill", "unkill", "run_job"})
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        nonce = commands.write_command(tmp.name, "kill", {"nonce": "n-wp6"})
        commands.poll(tmp.name, handler=lambda cmd: {"ok": True})
        commands.write_command(tmp.name, "kill", {"nonce": nonce})
        handled = commands.poll(tmp.name, handler=lambda cmd: {"ok": True})
        self.assertEqual([row for row in handled if row.get("type") == "kill"], [])

    def test_p3_passwords_never_reach_command_files(self):
        """口令字段不进指令目录：服务端校验后即丢弃（与 Node R4 呼应）。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        commands.write_command(tmp.name, "execute_plan",
                               {"plan_hash": "h", "expected_mode": "live"})
        pending = Path(tmp.name) / "trading-commands" / "pending"
        for file in pending.glob("*.json"):
            self.assertNotIn("confirmation", file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **步骤 2：`~/.dsh/trading-venv/bin/python -B -m unittest tests.test_core_wp6_approval -v` → 3 用例 PASS**
- [ ] **步骤 3：Commit**

```bash
git add tests/test_core_wp6_approval.py
git commit -m "test(core): WP6 审批回归 Python 矩阵（P1-P3）"
```

### 任务 7：6c preset 行替换与 persona 修订

**文件：** 修改 `agent.cordis.yml`、`preset.yml`、`plugins/workbench/cordis.patch.yml`

- [ ] **步骤 1：`agent.cordis.yml` 末尾追加 MCP 行（futu-mcp 行之后）**

```yaml

# ── 量化平台独立服务（WP6：MCP 工具面 = 工作台全功能，规格见
#    docs/superpowers/specs/2026-09-15-wp6-standalone-service.md） ─────────────
# 需先启动服务：node platform/server/start.mjs（未启动时本行安静降级）。
# 通道分级：quantwb 的 switch_mode 仅限 live→sim；实盘切换只能在独立 Web 由用户输入口令。

- id: quant-platform-mcp
  name: '@deepseek-ai/dsh-mcp-client'
  disabled: true
  config:
    serverName: quantwb
    transport: streamable-http
    url: http://127.0.0.1:8397/mcp
    failOnStartupError: false
    toolCallTimeoutMs: 120000
```

（`disabled: true` 起步：服务在用户机器上常驻后才由安装器/用户启用——与 fin-data 行同策略，避免未装环境的会话报错。）

- [ ] **步骤 2：persona 修订（`agent.cordis.yml:30`）**——old_string：

```text
      - Harness is the only conversation and instruction entry. The trading workbench displays reports, observed broker responses and quant previews; it has no chat or order-entry controls.
```

new_string：

```text
      - Harness is the only conversation and instruction entry. The standalone trading workbench (http://127.0.0.1:8397, start with: node platform/server/start.mjs) displays reports, observed broker responses and quant previews; it has no chat or order-entry controls. The quantwb MCP tools expose the same workbench capabilities for queries and maintenance. Live-mode switching is web-only (the quantwb switch_mode tool rejects it); the plan-execution gate still requires the user's typed confirmation before any call.
```

- [ ] **步骤 3：`preset.yml` 描述行修订**——old：

```text
description: 在 Harness 中完成十二角色投研、量化预览及交易指令；工作台展示研报、账户工具响应和量化结果，并提供模拟盘/实盘切换。实盘需独立人工确认，工作台不提供聊天或下单入口。仅安装 preset 时使用 skill 基础模式，完整工作台需安装插件。
```

new：

```text
description: 在 Harness 中完成十二角色投研、量化预览及交易指令；研报、账户工具响应与量化结果由独立工作台（platform/ 服务：Web + MCP 工具面）展示，并提供模拟盘/实盘切换。实盘需独立人工确认，工作台不提供聊天或下单入口。仅安装 preset 时使用 skill 基础模式，完整工作台需安装插件并启动 platform 服务。
```

- [ ] **步骤 4：`plugins/workbench/cordis.patch.yml` 只加注释（行本身必须保留——`tradingWorkbench` 服务是 engine 审批策略的进程内锚，规格 §5.4）**

```yaml
# 该行必须保留：tradingWorkbench 服务是 engine 账户策略（policy.js 模式互斥/原生审批/调用租约）
# 的进程内锚。WP6 起面板 UI 由独立服务（platform/）接管，本 Host 行与 legacy 面板过渡期并存，
# 面板移除另行提交（规格 §5.4）。
- insert:
    - id: trading-workbench
      name: '@bstester/dsh-trading-workbench'
```

- [ ] **步骤 5：验证 YAML 可解析**：`node -e "const fs=require('fs'); const s=fs.readFileSync('agent.cordis.yml','utf8'); console.log('yaml lines:', s.split('\n').length)"` + 用现有安装自检（`bash -n install.sh` 不适用；以 dsh profile 语法校验为准——无本地 CLI 时以 YAML 缩进人工核对 + 后续任务 12 人工回归兜底）
- [ ] **步骤 6：Commit**

```bash
git add agent.cordis.yml preset.yml plugins/workbench/cordis.patch.yml
git commit -m "feat(preset): WP6 preset 行替换（quant-platform-mcp 行 + persona/描述修订）"
```

### 任务 8：前端脚手架（Vite + antd5 + ProComponents + ProLayout 壳）

**文件：** 创建 `platform/web/package.json`、`platform/web/vite.config.mjs`、`platform/web/index.html`、`platform/web/src/main.jsx`、`platform/web/src/app.jsx`、`platform/web/src/services/api.js`、`platform/web/src/services/hooks.js`、`platform/web/src/pages/`（11 个占位页组件，任务 9–11 逐个替换为真实现）

- [ ] **步骤 1：`platform/web/package.json`**

```json
{
  "name": "@bstester/quant-platform-web",
  "private": true,
  "type": "module",
  "version": "0.1.0",
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "test": "node --test tests/*.test.mjs"
  },
  "dependencies": {
    "@ant-design/pro-components": "^2.8.10",
    "antd": "^5.27.0",
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.6.0",
    "vite": "^6.3.5"
  }
}
```

- [ ] **步骤 2：`platform/web/vite.config.mjs`**

```js
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8397" } },
  build: { outDir: "dist", chunkSizeWarningLimit: 4000 },
});
```

- [ ] **步骤 3：`platform/web/index.html`**

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>量化工作台</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

- [ ] **步骤 4：`platform/web/src/services/api.js`（数据层，规格 §4.3：两层缓存变一层半）**

```js
// 数据层：POST /api/wb/<endpoint>；envelope 解包 + 客户端内存缓存（TTL 对齐旧 client.js）。
// 业务失败（ok:false）抛 Error(message) 由页面展示——不弹全局错误掩盖降级数据。
const TTL_MS = {
  snapshot: 0, series: 300_000, equity: 300_000, positions: 300_000,
  correlation: 1_800_000, sensitivity: 3_600_000, risk: 900_000, trades: 300_000,
  events: 3_600_000, factors: 1_800_000, ic: 1_800_000, audit: 120_000,
  sources: 300_000, instrument: 600_000, quality: 3_600_000,
  plan: 60_000, schedule: 30_000, reconcile: 300_000,
};
const memory = new Map();

export function getToken() {
  try { return localStorage.getItem("trading_token") ?? ""; } catch { return ""; }
}
export function setToken(value) {
  try {
    if (value) localStorage.setItem("trading_token", value);
    else localStorage.removeItem("trading_token");
  } catch { /* 隐私模式忽略 */ }
}
export function clearCache() { memory.clear(); }

export async function callApi(endpoint, payload = {}, { refresh = false } = {}) {
  const key = `${endpoint}|${JSON.stringify(payload)}`;
  const ttl = TTL_MS[endpoint] ?? 0;
  const hit = memory.get(key);
  if (!refresh && ttl > 0 && hit && Date.now() - hit.at < ttl) return hit.value;
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let response;
  try {
    response = await fetch(`/api/wb/${endpoint}`, {
      method: "POST", headers,
      body: JSON.stringify(refresh ? { ...payload, _refresh: true } : payload),
    });
  } catch (error) {
    throw new Error(`无法连接工作台服务（127.0.0.1:8397）：${error.message}`);
  }
  if (response.status === 401) throw new Error("需要访问令牌：右上角「令牌」处填入服务配置的 token");
  if (response.status === 404) throw new Error(`服务未提供 ${endpoint}（服务版本陈旧，请重启服务后刷新）`);
  const body = await response.json();
  if (!body.ok) throw new Error(body.error?.message || body.error?.code || "请求失败");
  memory.set(key, { at: Date.now(), value: body.value });
  return body.value;
}
```

- [ ] **步骤 5：`platform/web/src/services/hooks.js`**

```js
import React from "react";
import { callApi } from "./api.js";

/** endpoint + payload 驱动的取数 hook；refresh() 绕过客户端与服务端缓存。 */
export function useEndpoint(endpoint, payload, deps = []) {
  const [state, setState] = React.useState({ loading: true });
  const key = JSON.stringify(payload ?? {});
  React.useEffect(() => {
    let alive = true;
    setState((prev) => ({ ...prev, loading: true, error: undefined }));
    callApi(endpoint, payload ?? {}).then(
      (value) => { if (alive) setState({ loading: false, value }); },
      (error) => { if (alive) setState({ loading: false, error: String(error.message || error) }) });
    return () => { alive = false; };
  }, [...deps, endpoint, key]);
  const refresh = React.useCallback(() => {
    callApi(endpoint, payload ?? {}, { refresh: true }).then(
      (value) => setState({ loading: false, value }),
      (error) => setState({ loading: false, error: String(error.message || error) }));
  }, [endpoint, key]);
  return { ...state, refresh };
}

/** snapshot 兜底轮询（60 秒）；unmount 停止（规格 §4.3）。 */
export function useSnapshotPoll(intervalMs = 60_000) {
  const snapshot = useEndpoint("snapshot", {}, []);
  React.useEffect(() => {
    const timer = setInterval(() => snapshot.refresh(), intervalMs);
    return () => clearInterval(timer);
  }, [snapshot.refresh, intervalMs]);
  return snapshot;
}
```

- [ ] **步骤 6：`platform/web/src/app.jsx`（ProLayout 壳 + 模式徽章 + 免责声明 + 令牌入口，文案规范已应用）**

```jsx
import React from "react";
import { createRoot } from "react-dom/client";
import { App as AntApp, Button, ConfigProvider, Input, Modal, Space, Tag, Typography, theme } from "antd";
import { ProLayout } from "@ant-design/pro-components";
import zhCN from "antd/locale/zh_CN";
import { clearCache, getToken, setToken } from "./services/api.js";
import { useSnapshotPoll } from "./services/hooks.js";
import MarketPage from "./pages/market.jsx";
import SignalPage from "./pages/signal.jsx";
import PortfolioPage from "./pages/portfolio.jsx";
import RiskPage from "./pages/risk.jsx";
import FactorsPage from "./pages/factors.jsx";
import ExecutionPage from "./pages/execution.jsx";
import ResearchPage from "./pages/research.jsx";
import EventsPage from "./pages/events.jsx";
import PlanPage from "./pages/plan.jsx";
import SchedulePage from "./pages/schedule.jsx";
import AuditPage from "./pages/audit.jsx";

const PAGES = [
  { key: "market", name: "行情", element: <MarketPage /> },
  { key: "signal", name: "信号", element: <SignalPage /> },
  { key: "portfolio", name: "组合", element: <PortfolioPage /> },
  { key: "risk", name: "风险", element: <RiskPage /> },
  { key: "factors", name: "因子", element: <FactorsPage /> },
  { key: "execution", name: "执行", element: <ExecutionPage /> },
  { key: "research", name: "研究", element: <ResearchPage /> },
  { key: "events", name: "事件", element: <EventsPage /> },
  { key: "plan", name: "计划", element: <PlanPage /> },
  { key: "schedule", name: "调度", element: <SchedulePage /> },
  { key: "audit", name: "审计", element: <AuditPage /> },
];

function currentKey() {
  return (location.hash.replace(/^#\//, "").split("?")[0]) || "market";
}

function TokenButton() {
  const [open, setOpen] = React.useState(false);
  const [value, setValue] = React.useState(getToken());
  return (
    <>
      <Button size="small" onClick={() => setOpen(true)}>令牌</Button>
      <Modal title="访问令牌" open={open} onCancel={() => setOpen(false)}
        onOk={() => { setToken(value.trim()); clearCache(); setOpen(false); location.reload(); }}
        okText="保存并刷新" cancelText="取消">
        <Input value={value} onChange={(event) => setValue(event.target.value)}
          placeholder="服务未配置 token 时留空" />
      </Modal>
    </>
  );
}

function Shell() {
  const [key, setKey] = React.useState(currentKey());
  React.useEffect(() => {
    const onHash = () => setKey(currentKey());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const snapshot = useSnapshotPoll();
  const mode = snapshot.value?.mode ?? "sim";
  const page = PAGES.find((item) => item.key === key) ?? PAGES[0];
  return (
    <ProLayout title="量化工作台" layout="mix" fixSiderbar
      route={{ path: "/", routes: PAGES.map(({ key: k, name }) => ({ path: `/${k}`, name })) }}
      location={{ pathname: `/${page.key}` }}
      menuItemRender={(item, dom) => (
        <a href={`#${item.path}`} onClick={() => setKey(item.path.slice(1))}>{dom}</a>)}
      avatarProps={{ render: () => (
        <Space size="small">
          <Tag color={mode === "live" ? "red" : "green"}>{mode === "live" ? "实盘 LIVE" : "模拟 SIM"}</Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            数据按 TTL 本地缓存；模式切换不授权下单
          </Typography.Text>
          <TokenButton />
        </Space>) }}>
      <AntApp>{page.element}</AntApp>
    </ProLayout>
  );
}

createRoot(document.getElementById("root")).render(
  <ConfigProvider locale={zhCN} theme={{ algorithm: theme.defaultAlgorithm }}>
    <Shell />
  </ConfigProvider>);
```

- [ ] **步骤 7：`platform/web/src/main.jsx`**：`import "./app.jsx";`（app.jsx 自挂载）
- [ ] **步骤 8：11 个占位页**（任务 9–11 逐个替换）。每个文件形如：

```jsx
// platform/web/src/pages/market.jsx（占位，任务 9 替换）
import React from "react";
import { Card, Typography } from "antd";
export default function MarketPage() {
  return <Card><Typography.Text type="secondary">迁移中。</Typography.Text></Card>;
}
```

（其余 10 页同构，`export default function XxxPage`，文件名对应 PAGES key。）
- [ ] **步骤 9：安装并构建**：`npm install --prefix platform/web && npm --prefix platform/web run build` → dist 生成、零报错
- [ ] **步骤 10：起服务联调（可选，有 venv 数据时）**：`node platform/server/start.mjs` 后浏览器开 `http://127.0.0.1:8397`，壳与 11 页签可见；`curl -s -XPOST localhost:8397/api/wb/snapshot -d '{}' -H 'Content-Type: application/json' | head -c 200`
- [ ] **步骤 11：Commit**

```bash
git add platform/web
git commit -m "feat(web): WP6 前端脚手架（ProLayout 壳/数据层/11 页签骨架）"
```

### 任务 9：图表移植（geometry 纯函数带测试 + 三个 canvas 组件）

**文件：** 创建 `platform/web/src/charts/geometry.js`、`platform/web/tests/geometry.test.mjs`、`platform/web/src/charts/line.jsx`、`platform/web/src/charts/kline.jsx`、`platform/web/src/charts/heatmap.jsx`

- [ ] **步骤 1：先写失败测试 `platform/web/tests/geometry.test.mjs`（断言移植自 `plugins/workbench/src/client.js` 既有测试语义：绘图区外不命中、右缘翻转、缺失值显示 —）**

```js
// 图表几何纯函数（自 client.js 移植，规格 §4.2）。宿主无关，node --test 直测。
import test from "node:test";
import assert from "node:assert/strict";
import { barIndexAt, tooltipLeft, compactNumber } from "../src/charts/geometry.js";

test("barIndexAt：绘图区左右留白返回 null，不得误命中首尾", () => {
  const geometry = { padL: 40, plotW: 400, count: 10 };
  assert.equal(barIndexAt(30, geometry), null);
  assert.equal(barIndexAt(450, geometry), null);
  assert.equal(barIndexAt(40, geometry), 0);
  assert.equal(barIndexAt(439, geometry), 9);
});

test("tooltipLeft：贴近右边缘翻到左侧，不越界", () => {
  assert.equal(tooltipLeft(390, 80, 400, 14), 390 - 80 - 14);
  assert.equal(tooltipLeft(100, 80, 400, 14), 100 + 14);
});

test("compactNumber：缺失显示 — 而不是 0；中文量级（万/亿）", () => {
  assert.equal(compactNumber(undefined), "—");
  assert.equal(compactNumber(null), "—");
  assert.equal(compactNumber(1234), "1234");
  assert.equal(compactNumber(5_600_000), "560.00万");
  assert.equal(compactNumber(150_000_000), "1.50亿");
});
```

- [ ] **步骤 2：运行 `npm --prefix platform/web test` → FAIL**
- [ ] **步骤 3：实现 `geometry.js`（从 `client.js:171-210` 移植语义，保持函数签名一致）**

```js
// K 线/折线几何纯函数（自 workbench client.js 移植；绘制与命中判定必须共用同一套几何量）。
export function barIndexAt(x, { padL, plotW, count }) {
  if (count <= 0 || plotW <= 0) return null;
  if (x < padL || x >= padL + plotW) return null;
  const index = Math.floor(((x - padL) / plotW) * count);
  return Math.min(Math.max(index, 0), count - 1);
}

export function tooltipLeft(cursorX, boxWidth, chartWidth, gap = 14) {
  return cursorX + boxWidth + gap > chartWidth ? cursorX - boxWidth - gap : cursorX + gap;
}

export function compactNumber(value) {
  if (value === undefined || value === null || !Number.isFinite(Number(value))) return "—";
  const absolute = Math.abs(Number(value));
  if (absolute >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (absolute >= 1e4) return `${(value / 1e4).toFixed(2)}万`;
  return String(Math.round(value));
}
```

- [ ] **步骤 4：`npm --prefix platform/web test` → PASS**
- [ ] **步骤 5：移植三个 canvas 组件**——来源锚点与移植规则（逐函数从 `plugins/workbench/src/client.js` 复制，只做列出的适配，逻辑零改动）：
  - `client.js:317-341` `useCanvasChart`、`client.js:342-352` `movingAverage` → `charts/line.jsx`；
  - `client.js:353-386` `LineChart`、`client.js:418-464` `drawKLineHover`、`client.js:465-532` `KLineChart` → `charts/kline.jsx`（保留：命中判定与绘制共用 ref 里的 `{padL, plotW, count}`；只在跨到另一根 K 线时 setState；`barIndexAt/tooltipLeft/compactNumber` 改从 `./geometry.js` import）；
  - `client.js:533-569` `HeatmapChart` → `charts/heatmap.jsx`；
  - 适配仅限：`themeColors()`（`client.js:301-316`）改为 `charts/theme.js` 一并复制；删除对 `wb-*` class 的依赖（图表容器用 `<div style={{width:"100%"}}>` 包裹）；三份文件顶部注释标明来源行号。
- [ ] **步骤 6：`npm --prefix platform/web run build` → 零报错**
- [ ] **步骤 7：Commit**

```bash
git add platform/web/src/charts platform/web/tests
git commit -m "feat(web): 图表移植（几何纯函数带测试 + K线/折线/热力图）"
```

### 任务 10：页面批 1——行情 / 信号 / 组合 / 风险

**文件：** 替换 `platform/web/src/pages/{market,signal,portfolio,risk}.jsx`

- [ ] **步骤 1：`market.jsx`（完整代码；数据=series+instrument，规格 §3.3 行情行）**

```jsx
import React from "react";
import { Card, Col, Input, Row, Select, Space, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";
import { KLineChart } from "../charts/kline.jsx";

const PERIODS = [
  { value: "1d", label: "日线" }, { value: "60m", label: "60分" },
  { value: "15m", label: "15分" }, { value: "5m", label: "5分" }, { value: "1m", label: "1分" },
];

export default function MarketPage() {
  const [ticker, setTicker] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [period, setPeriod] = React.useState("1d");
  const series = useEndpoint("series", query ? { ticker: query, period, limit: 250 } : {}, [query, period]);
  const instrument = useEndpoint("instrument", query ? { ticker: query } : {}, [query]);
  const info = instrument.value;
  return (
    <Card title="行情" extra={(
      <Space>
        <Input placeholder="标的代码，如 SH.600519" style={{ width: 220 }} value={ticker}
          onChange={(event) => setTicker(event.target.value)}
          onPressEnter={() => setQuery(ticker.trim().toUpperCase())} />
        <Select options={PERIODS} value={period} onChange={setPeriod} style={{ width: 90 }} />
      </Space>)}>
      {!query && <Typography.Text type="secondary">输入标的代码后加载；数据按 TTL 本地缓存，不追求实时行情。</Typography.Text>}
      {query && instrument.error && <Typography.Text type="warning">标的解析失败：{instrument.error}</Typography.Text>}
      {info && (
        <Space size="large" style={{ marginBottom: 12 }}>
          <Typography.Text strong>{info.name ?? query}</Typography.Text>
          {info.lot_size != null && <Typography.Text type="secondary">整手 {info.lot_size}</Typography.Text>}
        </Space>)}
      {query && series.error && <Typography.Text type="danger">K 线读取失败：{series.error}</Typography.Text>}
      {series.value?.bars?.length
        ? <KLineChart bars={series.value.bars} />
        : query && !series.loading && !series.error && <Typography.Text type="secondary">该周期暂无数据。</Typography.Text>}
      {series.loading && query ? <Typography.Text type="secondary">加载中…</Typography.Text> : null}
    </Card>);
}
```

（执行者注意：`instrument` 返回字段名以 `plugins/workbench/python/instruments.py` 实际输出为准——先读该文件再定 `info.name/lot_size` 的字段路径，禁止猜字段。）

- [ ] **步骤 2：`signal.jsx`（完整代码；数据=snapshot.previews 派生）**

```jsx
import React from "react";
import { Card, Col, Row, Statistic, Table, Tag, Typography } from "antd";
import { useSnapshotPoll } from "../services/hooks.js";

const STRATEGY = { rsi: "RSI", ma_cross: "双均线" };

export default function SignalPage() {
  const snapshot = useSnapshotPoll();
  const previews = (snapshot.value?.previews ?? []).filter((row) => row.kind !== "ledger");
  const latest = previews[0];
  return (
    <Row gutter={[16, 16]}>
      <Col span={24}>
        <Card title="量化信号预览" extra={
          <Typography.Text type="secondary">仅预览，不下单；本地缓存数据</Typography.Text>}>
          {latest ? (
            <Space size="large" wrap>
              <Statistic title="标的" value={latest.ticker ?? "—"} />
              <Statistic title="策略" value={STRATEGY[latest.strategy] ?? latest.strategy ?? "—"} />
              <Statistic title="信号" value={latest.signal?.label ?? latest.signal?.value ?? "—"} />
              <Statistic title="入场价" value={latest.entry ?? "—"} />
              <Statistic title="止损价" value={latest.stop ?? "—"} />
            </Space>)
            : <Typography.Text type="secondary">暂无信号预览；在 Harness 会话中调用 quant_signal 生成。</Typography.Text>}
        </Card>
      </Col>
      <Col span={24}>
        <Card title="历史预览">
          <Table size="small" rowKey={(row) => row.saved_at ?? row.ticker}
            dataSource={previews.slice(0, 20)}
            columns={[
              { title: "时间", dataIndex: "saved_at", render: (v) => v ?? "—" },
              { title: "标的", dataIndex: "ticker", render: (v) => v ?? "—" },
              { title: "类型", dataIndex: "kind", render: (v) => (v === "backtest" ? "回测" : "信号") },
              { title: "模式", dataIndex: "mode", render: (v) => <Tag color={v === "live" ? "red" : "green"}>{v}</Tag> },
            ]} />
        </Card>
      </Col>
    </Row>);
}
```

（同上注意：`previews` 记录的字段名以 `store.recordPreview` 写入结构为准——先读 `plugins/workbench/src/store.js` 的 `recordPreview` 与引擎脚本输出，再定 `latest.*` 路径，禁止猜字段。）

- [ ] **步骤 3：`portfolio.jsx`（完整代码；数据=positions+equity；不跨币种合并、errors 不掩盖）**

```jsx
import React from "react";
import { Alert, Card, Col, Row, Space, Table, Tag, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";
import { LineChart } from "../charts/line.jsx";

export default function PortfolioPage() {
  const [mode, setMode] = React.useState("sim");
  const positions = useEndpoint("positions", { mode }, [mode]);
  const equity = useEndpoint("equity", { mode, window: 250 }, [mode]);
  const groups = positions.value?.groups ?? [];
  return (
    <Row gutter={[16, 16]}>
      <Col span={24}>
        <Space>
          {["sim", "live"].map((m) => (
            <Tag key={m} style={{ cursor: "pointer" }} color={mode === m ? (m === "live" ? "red" : "green") : "default"}
              onClick={() => setMode(m)}>{m === "live" ? "实盘" : "模拟"}</Tag>))}
          <Typography.Text type="secondary">按账户小计，不跨账户/币种合并；实时读取失败时展示缓存并标注 stale</Typography.Text>
        </Space>
      </Col>
      {(positions.value?.errors ?? []).map((error) => (
        <Col span={24} key={error}><Alert type="warning" showIcon message={`账户读取失败：${error}`} /></Col>))}
      <Col span={14}>
        <Card title="券商持仓" loading={positions.loading}>
          {positions.error && <Typography.Text type="danger">{positions.error}</Typography.Text>}
          <Table size="small" rowKey={(row) => row.account ?? row.currency ?? JSON.stringify(row)}
            dataSource={groups}
            columns={[
              { title: "账户", dataIndex: "account", render: (v) => (v ? `****${String(v).slice(-4)}` : "—") },
              { title: "标的", dataIndex: "symbol", render: (v) => v ?? "—" },
              { title: "数量", dataIndex: "qty", align: "right", render: (v) => v ?? "—" },
              { title: "市值", dataIndex: "market_value", align: "right", render: (v) => v ?? "—" },
              { title: "币种", dataIndex: "currency", render: (v) => v ?? "—" },
            ]} />
        </Card>
      </Col>
      <Col span={10}>
        <Card title="权益曲线（本地模拟台账）" loading={equity.loading}>
          {equity.error && <Typography.Text type="danger">{equity.error}</Typography.Text>}
          {equity.value?.points?.length ? <LineChart points={equity.value.points} label="权益" /> : null}
        </Card>
      </Col>
    </Row>);
}
```

（`groups` 行字段以 `analytics.js` 的 `positions()` 输出为准——先读实现再定列，禁止猜字段。）
- [ ] **步骤 4：`risk.jsx`**（完整代码，结构同上：`risk` 端点渲染配置卡——用 antd `Descriptions` 列出 `config` 全部键值；`positions` 渲染集中度指标区；无新增计算逻辑，只透出端点已有字段）
- [ ] **步骤 5：`npm --prefix platform/web run build` → 零报错；`npm --prefix platform/web test` 仍 PASS**
- [ ] **步骤 6：文案自查（全局约定 2.1）**：`grep -rn "设计稿\|示例数据\|宁可\|窄门\|规格" platform/web/src/pages` → 仅注释命中为 0 违规
- [ ] **步骤 7：Commit**

```bash
git add platform/web/src/pages
git commit -m "feat(web): 页面批 1（行情/信号/组合/风险）"
```

### 任务 11：页面批 2——因子 / 执行 / 研究 / 事件

**文件：** 替换 `platform/web/src/pages/{factors,execution,research,events}.jsx`

- [ ] **步骤 1：`factors.jsx`**——布局：顶部关注池 Input（逗号分隔 → tickers 数组）+ `factors` 端点 ProTable（行=因子，列=标的得分）+ `ic` 端点 LineChart + 单标的 `quality` Card（Descriptions）。数据字段先读 `analytics.js` 的 `factors()/ic()` 与 `python/quality.py` 输出再定列。
- [ ] **步骤 2：`execution.jsx`**——数据=`snapshot.trade_summary` + `trades` 端点。三区：①去重订单事实表（列=标的/名称/数量/委托价/成交价/成交数量/方向/状态原码——状态码不猜标签，原样展示）；②动作行（下单/改单/撤单，来自 summary.actions）；③原始响应折叠区（antd `Collapse`，标「原始券商响应（N 条，审计核对用）」）。
- [ ] **步骤 3：`research.jsx`**——数据=`snapshot.runs/reports`。runs 表（id/标的/状态/模式/开始时间）；reports 列表 → 点击进入 `ReportDetail`（返回按钮 + Markdown 渲染 + 来源列表：名称/as_of/reference）。Markdown 渲染器按任务 9 移植规则从 `client.js:724-968`（`parseMarkdown/renderInline/renderBlocks/Markdown`）复制到 `platform/web/src/lib/markdown.jsx`，适配仅限 import 路径与 className；该移植必须先写 `platform/web/tests/markdown.test.mjs`：断言标题/列表/粗体/链接四类块解析正确（用例直接取 client.js 现有渲染语义的输入输出对）。
- [ ] **步骤 4：`events.jsx`**——数据=`events` 端点。antd `Timeline`，每项：日期 + 事件文本；无事件显示「该区间无事件」。
- [ ] **步骤 5：构建 + 文案自查 + Commit**（同任务 10 步骤 5–7；commit message `feat(web): 页面批 2（因子/执行/研究/事件）`）

### 任务 12：页面批 3——计划 / 调度 / 审计（执行窄门 UI）

**文件：** 替换 `platform/web/src/pages/{plan,schedule,audit}.jsx`

- [ ] **步骤 1：`plan.jsx`（完整代码；规格 §4.5 窄门不变量 1/2/4 全在此页）**

```jsx
import React from "react";
import { Alert, App, Button, Card, Col, Input, Row, Space, Table, Tag, Typography } from "antd";
import { callApi } from "../services/api.js";
import { useEndpoint } from "../services/hooks.js";

export default function PlanPage() {
  const { message } = App.useApp();
  const plan = useEndpoint("plan", {}, []);
  const [confirmText, setConfirmText] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  if (plan.loading && !plan.value) return <Card loading />;
  if (plan.error) {
    return <Card><Typography.Text type="danger">计划读取失败：{plan.error}</Typography.Text></Card>;
  }
  const value = plan.value ?? {};
  const current = value.plans?.[0];
  const live = value.mode === "live";
  const frozen = current?.status === "frozen";
  const submit = async (action) => {
    if (action === "execute" && live && confirmText !== "确认执行") {
      message.warning("实时账户执行需输入口令：确认执行");
      return;
    }
    setBusy(true);
    try {
      await callApi("plan-execute", action === "execute"
        ? { plan_hash: current.content_hash, expected_mode: value.mode,
            confirmation: live ? "确认执行" : undefined }
        : { plan_hash: current?.content_hash, expected_mode: value.mode, action });
      message.success(action === "execute" ? "已提交，等待 daemon 回写状态…" : "指令已提交");
      plan.refresh();
    } catch (error) {
      message.error(String(error.message || error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Row gutter={[16, 16]}>
      <Col span={24}>
        <Space size="large">
          <Tag color={live ? "red" : "green"}>{live ? "实盘 LIVE" : "模拟 SIM"}</Tag>
          {current && <>
            <Typography.Text code>{current.plan_id}</Typography.Text>
            <Typography.Text code>{current.content_hash}</Typography.Text>
            <Tag>{current.status}</Tag>
            <Typography.Text type="secondary">冻结 {current.created_at}</Typography.Text>
          </>}
          {!current && <Typography.Text type="secondary">暂无计划（daemon 生成后显示）。</Typography.Text>}
        </Space>
      </Col>
      <Col span={24}>
        <Card title="目标 vs 实际 + 逐单预检">
          <Table size="small" rowKey={(row) => `${row.symbol}-${row.side}`}
            dataSource={current?.orders ?? []}
            columns={[
              { title: "标的", dataIndex: "symbol" },
              { title: "方向", dataIndex: "side", render: (v) => (v === "BUY" ? "买入" : "卖出") },
              { title: "数量", dataIndex: "qty", align: "right" },
              { title: "限价", dataIndex: "price", align: "right" },
              { title: "预检", dataIndex: "risk_verdict", render: (v) => v ?? "—" },
            ]} />
        </Card>
      </Col>
      {frozen && (
        <Col span={24}>
          {live && <Alert type="warning" showIcon style={{ marginBottom: 12 }}
            message="实时账户执行：输入口令「确认执行」；执行已冻结计划是唯一下单入口" />}
          <Space>
            {live && <Input value={confirmText} style={{ width: 200 }}
              placeholder="输入：确认执行" onChange={(event) => setConfirmText(event.target.value)} />}
            <Button type="primary" danger={live} disabled={busy}
              onClick={() => submit("execute")}>{live ? "执行（实时账户）" : "执行计划"}</Button>
            <Button disabled={busy} onClick={() => submit("cancel")}>取消计划</Button>
            {busy && <Typography.Text type="secondary">已提交，等待 daemon 回写状态…</Typography.Text>}
          </Space>
        </Col>)}
    </Row>);
}
```

- [ ] **步骤 2：`schedule.jsx`（完整代码；心跳 >5 分钟标红 = 数据事实提示；kill 开关走 plan-execute 动作；unkill 前必须人工二次确认）**

```jsx
import React from "react";
import { App, Button, Card, Col, Row, Space, Table, Tag, Typography } from "antd";
import { callApi } from "../services/api.js";
import { useEndpoint } from "../services/hooks.js";

const LEVEL = { critical: "red", warn: "gold", info: "blue" };

export default function SchedulePage() {
  const { message, modal } = App.useApp();
  const schedule = useEndpoint("schedule", {}, []);
  const value = schedule.value ?? {};
  const heartbeat = value.heartbeat ?? {};
  const staleMinutes = heartbeat.heartbeat
    ? Math.round((Date.now() - Date.parse(heartbeat.heartbeat)) / 60000) : null;
  const lost = staleMinutes == null || staleMinutes > 5;
  const send = async (action, label) => {
    try {
      await callApi("plan-execute", { action });
      message.success(`${label}指令已提交`);
      schedule.refresh();
    } catch (error) {
      message.error(String(error.message || error));
    }
  };
  return (
    <Row gutter={[16, 16]}>
      <Col span={24}>
        <Card title="daemon 健康" extra={
          <Space>
            <Tag color={lost ? "red" : "green"}>{lost ? "失联" : "正常"}</Tag>
            {staleMinutes != null && <Typography.Text type="secondary">心跳 {staleMinutes} 分钟前</Typography.Text>}
            <Tag color={heartbeat.kill ? "red" : "default"}>{heartbeat.kill ? "kill 生效" : "kill 未激活"}</Tag>
            {heartbeat.kill
              ? <Button danger onClick={() => modal.confirm({
                  title: "解除 kill switch", content: "unkill = 人工确认后解除；确认已完成券商核对？",
                  okText: "确认解除", cancelText: "取消",
                  onOk: () => send("unkill", "unkill") })}>解除</Button>
              : <Button danger onClick={() => send("kill", "kill")}>激活 kill switch</Button>}
          </Space>}>
          {(value.jobs ?? []).length
            ? <Table size="small" rowKey={(row) => `${row.market}-${row.name}-${row.day}`}
                dataSource={value.jobs}
                columns={[
                  { title: "市场", dataIndex: "market" },
                  { title: "作业", dataIndex: "name" },
                  { title: "状态", dataIndex: "status" },
                  { title: "时间", dataIndex: "at", render: (v) => v ?? "—" },
                ]} />
            : <Typography.Text type="secondary">暂无作业记录（daemon 未运行或今日休市）。</Typography.Text>}
          {heartbeat.critical && (
            <Typography.Text type="danger" style={{ display: "block", marginTop: 8 }}>
              critical 告警待处置：{heartbeat.critical_title ?? "（见告警列表）"}；处置后随恢复流程清除
            </Typography.Text>)}
        </Card>
      </Col>
      <Col span={24}>
        <Card title="告警">
          {(value.alerts ?? value.jobs ?? []).length >= 0 && <AlertList />}
        </Card>
      </Col>
    </Row>);
}

function AlertList() {
  const reconcile = useEndpoint("reconcile", {}, []);
  const alerts = reconcile.value?.alerts ?? [];
  if (!alerts.length) return <Typography.Text type="secondary">无告警。</Typography.Text>;
  return (
    <Table size="small" rowKey={(row, index) => `${row.created_at}-${index}`}
      dataSource={alerts}
      columns={[
        { title: "级别", dataIndex: "level", render: (v) => <Tag color={LEVEL[v] ?? "default"}>{v}</Tag> },
        { title: "标题", dataIndex: "title" },
        { title: "详情", dataIndex: "detail", render: (v) => v ?? "—" },
        { title: "时间", dataIndex: "created_at" },
      ]} />);
}
```

（告警列表取自 `reconcile` 端点的 `alerts` 字段——与 WP4 验收记录偏差披露 8 一致；执行者以 `snapshot-reconcile` 实际输出为准。）

- [ ] **步骤 3：`audit.jsx`**——数据=`audit` + `sources` + `reconcile`。三区：①三级链路（antd `Table` 的 expandable：计划行 → 展开订单行 → 展开成交行，列取自 `audit.value.chain`：`plan_id / orders[].client_order_id,status,broker_order_id / fills`）；②来源卡（`sources.value.sources` 列表：名称/状态/as_of）；③对账差异表（`reconcile.value.diffs`）。字段路径先读 `plugins/workbench/src/audit.js` 的 `buildAuditChain` 输出再定，禁止猜字段。
- [ ] **步骤 4：`npm --prefix platform/web run build` → 零报错；`npm --prefix platform/web test` PASS**
- [ ] **步骤 5：文案自查**：`grep -rn "设计稿\|示例数据\|宁可\|窄门\|规格\|token 说明" platform/web/src` → 字符串字面量 0 命中（注释命中需逐条确认不在 JSX 文本位置）
- [ ] **步骤 6：Commit**

```bash
git add platform/web/src/pages platform/web/src/lib
git commit -m "feat(web): 页面批 3（计划窄门/调度 kill/审计三级链路）"
```

### 任务 13：文档修订 + 全量验收记录

**文件：** 修改 `docs/architecture.md`、`docs/RUNBOOK.md`、`README.md`、`docs/HANDOVER.md`、`docs/P4-live-trading.md`、`docs/superpowers/plans/2026-09-14-platform-plan-index.md`；本文件末尾「WP6 验收记录」

- [ ] **步骤 1：`architecture.md`**：组件职责表加一行「`platform/` 独立服务进程（WP6）」；「产品定位」图补独立 Web/MCP 两条通道；「明确不做」补「独立 Web 无聊天/逐单表单；MCP switch_mode 仅限 live→sim（通道分级，规格 §3.2）」；文首「以 WP6 合并后实测为准」标注
- [ ] **步骤 2：`RUNBOOK.md`**：新增「平台服务」一节——启动 `node platform/server/start.mjs`、`~/.dsh/trading-platform.json` 配置样例（port/host/token）、前端构建 `npm --prefix platform/web run build`、mcp-smoke 排障（`node --test platform/tests/smoke.test.mjs`）、systemd unit 样例：

```ini
# ~/.config/systemd/user/quant-platform.service
[Unit]
Description=quant platform service (workbench MCP + web)
[Service]
WorkingDirectory=/path/to/dsh-trading-agents/platform
ExecStart=/usr/bin/env node server/start.mjs
Restart=on-failure
[Install]
WantedBy=default.target
```

- [ ] **步骤 3：`README.md`**：目录结构加 `platform/`；「可选：启动独立工作台服务」小节（bootstrap 两条 npm install + 构建命令 + preset 行启用说明）
- [ ] **步骤 4：`HANDOVER.md`**：交接注意加双进程数据约定（服务进程与 Harness 进程共享 `trading-workbench.json`，模式切换靠 `expected_mode` 复核兜底）与 legacy 面板过渡策略
- [ ] **步骤 5：`P4-live-trading.md`**：实盘准入清单追加「独立 Web + MCP 入口审批回归（§5.3 人工清单 5 步）通过」
- [ ] **步骤 6：`2026-09-14-platform-plan-index.md` WP6 节**：追加「规格：`docs/superpowers/specs/2026-09-15-wp6-standalone-service.md`；计划：`docs/superpowers/plans/2026-09-15-wp6-standalone-service.md`；验收记录见计划末节」
- [ ] **步骤 7：全量验收（命令与证据粘贴进「WP6 验收记录」）**

```bash
~/.dsh/trading-venv/bin/python -B -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/*.test.mjs
node --test platform/tests/*.test.mjs
npm --prefix platform/web test
npm --prefix platform/web run build
grep -rn "设计稿\|示例数据\|宁可\|窄门" platform/web/src   # 字符串字面量 0 命中
```

- [ ] **步骤 8：Commit**

```bash
git add docs README.md
git commit -m "docs: WP6 文档修订（架构/RUNBOOK/README/HANDOVER/P4/索引）"
```

### 任务 14：人工会话回归（需用户在场，自动执行部分之外的验收门）

- [ ] **步骤 1**：用户机器上启用 preset 的 `quant-platform-mcp` 行（`disabled: false`）并启动服务；新建 Harness 会话
- [ ] **步骤 2**：执行规格 §5.3 人工清单 5 步（工具面出现 / MCP live 拒绝 / Web 口令切 live / trading_* 原生审批卡 / plan_execute 窄门 + kill 演练），证据粘贴进「WP6 验收记录」
- [ ] **步骤 3**：全部通过后在「WP6 验收记录」标记 WP6 验收完成；任何一步失败 → 按 §5.5 回滚 preset 行（`disabled: true`）并修复重跑

## WP6 验收记录（执行时填写）

> 按任务顺序回填：每个任务的 commit、全量测试输出、mcp-smoke 输出、文案自查结果、人工清单 5 步证据。live 准入仍按 P4 清单，人工评估项保持未勾选。

## 执行说明（面向调度者）

- 任务依赖：0 → 1 → 2 → 3 → 4 → 5 → 6（Python 侧可与 5 并行）；7 独立可并行；8 → 9 → 10 → 11 → 12 串行（前端）；13 收尾；14 用户在场。
- 每任务一个子代理，产出后按 subagent-driven-development 两阶段审查（spec 合规 + 代码正确性）。
- 子代理通用上下文：仓库 `/home/penn/workspace/dsh-trading-agents`；Python 用 `~/.dsh/trading-venv/bin/python`；Node 测试零框架 `node --test`；**规格 §3.2 ⚠ 通道分级规则不得放松**。
