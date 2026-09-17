#!/usr/bin/env node
/**
 * 前端真浏览器 E2E（系统 Chromium + CDP，零新依赖）
 *
 * 用法：`node scripts/e2e_web.mjs [--routes a,b] [--no-screenshot] [--fail-on-soft]`
 *   E2E_BASE        覆盖工作台地址（默认 http://127.0.0.1:8397）
 *   E2E_CHROME      覆盖 Chromium 可执行路径（默认 Playwright 缓存 chromium-1234）
 *   E2E_IDLE_MS     网络空闲判定窗口（默认 400ms）
 *   E2E_MAX_WAIT_MS 每路由等待网络空闲的上限（默认 25000ms）
 *   `--routes`      只跑指定路由（单页复验）；`--no-screenshot` CI 友好；
 *   `--fail-on-soft` 让「已知在修」的软断言也计入退出码（修复落地后判闭环）。
 *
 * 锚点是**状态感知**的：路由表的 required 条目可以是字符串（必须出现）或数组
 * （数据态/空态任一出现即满足，例如 `plan` 的「当前计划 | 暂无计划」），报告里会
 * 标出命中的是哪一支（data/empty）。
 *
 * 每个路由做一次**完整加载**（路由间先 about:blank，避免同文档导航不触发
 * loadEventFired），并采两次 DOM：
 *   - domEarly：loadEventFired 之后立刻采（**加载窗口**，用于观察加载态渲染）
 *   - domFinal：真实网络空闲后采（**稳态**，锚点断言以此为准）
 *
 * 「网络空闲」用 Network.requestWillBeSent / loadingFinished / loadingFailed 追踪
 * **在途请求计数**，而不是 performance 资源条目数——后者只在请求**完成**时才产生
 * 条目，会把「仍在飞行」误判为安静（本脚本首版即踩此坑）。
 *
 * 稳态判定 = 网络空闲 **且** DOM 稳定（连续 3 次采样 ≥1s 不变）。只看网络空闲不够：
 * SPA 有依赖式二次请求波（positions/deals/equity 依赖 snapshot 返回的 mode），两波
 * 之间会出现瞬时「空闲」——本脚本首版据此提前快照，把加载窗口误当稳态。
 *
 * 判定（退出码非零当且仅当出现缺陷）：console error / 未捕获异常 / 5xx / 请求加载
 * 失败 / 白屏 / React 崩溃文案 / **内容区**缺必需锚点 / 未空闲 / 导航未完成。
 * 采集但**不判缺陷**（按严重度如实列出）：4xx、console.warning、加载窗口内出现的
 * 事实断言（如「无心跳记录」「暂无持仓」）及其是否在稳态消失。
 *
 * 只读：不点击任何提交类控件、不改服务状态、不安装依赖。
 * 产物：`~/.dsh/logs/e2e-web-<ts>/`（截图 + report.json）。
 */
import { spawn } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import path from "node:path";

const BASE = process.env.E2E_BASE ?? "http://127.0.0.1:8397";
const CHROME =
  process.env.E2E_CHROME ??
  path.join(homedir(), ".cache/ms-playwright/chromium-1234/chrome-linux/chrome");
const IDLE_MS = Number(process.env.E2E_IDLE_MS ?? 400);
const MAX_WAIT_MS = Number(process.env.E2E_MAX_WAIT_MS ?? 25000);
const TS = new Date().toISOString().replace(/[:.]/g, "-");
const OUT_DIR = path.join(homedir(), ".dsh", "logs", `e2e-web-${TS}`);
const SHOT_ROUTES = new Set(["overview", "pipeline", "research", "settings"]);

// --------------------------------------------------------------------------- CLI（零依赖手写解析）
// `--routes a,b`   只跑指定路由（单页复验；未知键会提示并列出可用键）
// `--no-screenshot` 不落截图（CI 友好，产物只剩 report.json）
// `--fail-on-soft`  把「已知在修」的软断言也计入退出码（修复落地后用它判闭环）
function parseArgs(argv) {
  const opts = { routes: null, screenshot: true, failOnSoft: false, help: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") opts.help = true;
    else if (arg === "--no-screenshot") opts.screenshot = false;
    else if (arg === "--fail-on-soft") opts.failOnSoft = true;
    else if (arg === "--routes") opts.routes = String(argv[++i] ?? "");
    else if (arg.startsWith("--routes=")) opts.routes = arg.slice("--routes=".length);
    else {
      process.stderr.write(`未知参数：${arg}\n`);
      opts.help = true;
    }
  }
  return opts;
}
const OPTS = parseArgs(process.argv.slice(2));

/**
 * 路由表。required = 页面职责特有的锚点；**每个条目可以是字符串，也可以是字符串数组**
 * （数组 = 数据态/空态的可接受替代，命中任一即算满足——2026-09-17 加固：`plan` 在空库时
 * 正确显示「暂无计划」，把它写成必需「当前计划」会造成假阳性；`settings` 页面无页面级
 * 标题，只能以卡片标题为锚点）。optional = 依赖当前数据的锚点，缺失只作信息记录。
 */
const ROUTES = [
  { key: "overview", name: "概览", required: ["运行状态"], optional: ["今日成交（OpenAPI）", "因子快照"] },
  { key: "market", name: "行情", required: ["行情"], optional: ["实时报价（rt_quote"] },
  { key: "capital", name: "资金", required: ["资金流向"], optional: ["资金分布（超大"] },
  { key: "options", name: "期权", required: ["期权分析"], optional: ["期权波动率", "到期日列表"] },
  { key: "signal", name: "信号", required: ["信号"], optional: ["最新信号", "历史预览"] },
  { key: "portfolio", name: "组合", required: ["组合"], optional: ["权益曲线", "累计收益率"] },
  { key: "risk", name: "风险", required: ["风险"], optional: ["组合风险", "风控配置"] },
  { key: "factors", name: "因子", required: ["因子"], optional: ["情绪快照采集", "因子打分与排序"] },
  { key: "execution", name: "执行", required: ["执行"], optional: ["券商订单", "台账最近"] },
  { key: "research", name: "研究", required: ["研究"], optional: ["规则候选池", "深度数据（F10）", "已发布研报"] },
  { key: "events", name: "事件", required: ["事件"], optional: [] },
  // 计划页：有计划渲染「当前计划」，空库渲染「暂无计划」——两种都是正确状态
  { key: "plan", name: "计划", required: [["当前计划", "暂无计划"]], optional: ["目标 vs 实际", "状态时间线"] },
  { key: "pipeline", name: "流程", required: ["流程"], optional: ["全局（晚间链）"] },
  { key: "schedule", name: "调度", required: ["调度"], optional: ["daemon 状态", "作业历史", "告警"] },
  { key: "audit", name: "审计", required: ["审计"], optional: ["对账差异", "数据源与授权状态"] },
  // 设置页：无页面级标题 → 以卡片标题为锚点（凭据卡 / 自动流水线卡，任一在位即算通过）
  { key: "settings", name: "设置", required: [["富途 OpenAPI 凭据", "自动流水线"]], optional: ["当前状态", "服务访问令牌"] },
];

const CRASH_PATTERNS = [
  "something went wrong",
  "application error",
  "minified react error",
  "cannot read propert",
  "is not a function",
  "渲染失败",
  "页面出错",
];

/**
 * 加载窗口内**以事实断言口吻**渲染的**空态**文案（absence 类）。
 * 通用「加载中/读取中/解析中」是正常加载提示，**不记录**——只记「把未知说成已知」的那些。
 *
 * 2026-09-17 收窄（假阳性修正）：首版把裸串 `"读取失败"` 也列在这里，结果命中的是
 * portfolio 页的**说明文案**「实时读取失败时展示缓存并标注 stale」（那是策略描述，
 * 不是事实断言），而该页 4 个 API 全部 200 —— 纯属探针过松。现拆成两类：
 *   * 空态断言（本节列表）：加载窗口出现即「未知被说成没有」；
 *   * 失败断言（FAILURE_CLAIM_RE）：只在**带错误详情**（`…读取失败：<error>`）时才算，
 *     且**必须**有失败请求支撑（见路由求值处）——有失败请求时如实展示失败是**诚实**行为。
 */
const EMPTY_STATE_CLAIMS = ["无心跳记录", "暂无持仓", "暂无告警", "暂无计划", "暂无权益序列",
                            "数据源 —", "推送 —", "今日无该链阶段"];
/** 带错误详情的失败断言（全角冒号是页面实际写法，`读取失败时…` 的说明文案不会命中）。 */
const FAILURE_CLAIM_RE = /读取失败：/;

/**
 * 软断言（soft assertions）：报告但不令退出码非零——因为对应缺陷**已知在修**，
 * 在修复落地前让 CI 常年变红会让这个门失去意义。`--fail-on-soft` 可把它们也计入退出码，
 * 用于修复落地后的闭环验证（判据：同一条软断言不再出现）。
 */
const SOFT_ASSERTIONS = {
  LOADING_FACT_CLAIMS: {
    name: "loading-window-factual-claims",
    title: "加载窗口内不得出现事实口吻的空态文案",
    knownBeingFixed: true,
    note: "把「尚未取到」渲染成「确实没有」（如「暂无持仓」「无心跳记录」）会让用户把未知当已知；"
      + "对应修复落地后用 --fail-on-soft 复跑，判据＝本条不再出现。"
      + "注：带错误详情且确有失败请求的「读取失败：…」不计入（那是诚实展示失败）。",
  },
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (m) => process.stdout.write(`${m}\n`);

async function getJson(url, timeoutMs = 3000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { signal: ctrl.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

/** 极简 CDP 客户端：id 关联请求/响应，事件按 method 分发。 */
class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Map();
    ws.addEventListener("message", (ev) => {
      let msg;
      try {
        msg = JSON.parse(typeof ev.data === "string" ? ev.data : String(ev.data));
      } catch {
        return;
      }
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
        return;
      }
      if (msg.method) for (const fn of this.handlers.get(msg.method) ?? []) fn(msg.params ?? {});
    });
  }
  on(method, fn) {
    if (!this.handlers.has(method)) this.handlers.set(method, []);
    this.handlers.get(method).push(fn);
  }
  send(method, params = {}, timeoutMs = 30000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP 超时：${method}`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => (clearTimeout(timer), resolve(v)),
        reject: (e) => (clearTimeout(timer), reject(e)),
      });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", () => reject(new Error(`WebSocket 连接失败：${wsUrl}`)), { once: true });
    });
    return new Cdp(ws);
  }
}

async function launchChromium(port, profileDir) {
  const child = spawn(
    CHROME,
    [
      "--headless=new",
      `--remote-debugging-port=${port}`,
      "--no-sandbox",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--window-size=1440,900",
      `--user-data-dir=${profileDir}`,
      "about:blank",
    ],
    { stdio: ["ignore", "pipe", "pipe"] },
  );
  let stderr = "";
  child.stderr.on("data", (d) => (stderr += String(d)));
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    try {
      const version = await getJson(`http://127.0.0.1:${port}/json/version`);
      return { child, version };
    } catch {
      if (child.exitCode !== null) throw new Error(`Chromium 提前退出：${stderr.slice(-400)}`);
      await sleep(250);
    }
  }
  child.kill("SIGKILL");
  throw new Error(`Chromium 20s 内未就绪：${stderr.slice(-400)}`);
}

async function findPageTarget(port) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    try {
      const list = await getJson(`http://127.0.0.1:${port}/json/list`);
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch {
      /* 继续等 */
    }
    await sleep(200);
  }
  throw new Error("未找到可用的 page target");
}

async function evaluate(cdp, expression) {
  const res = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
  if (res.exceptionDetails) throw new Error(`页面内求值失败：${res.exceptionDetails.text ?? "未知"}`);
  return res.result?.value;
}

/** 内容区文本与结构（排除侧边导航——否则任何页面都能匹配到导航里的页面名）。 */
const DOM_PROBE = `(() => {
  const root = document.getElementById('root');
  const pick = () => document.querySelector('.ant-pro-layout-content')
    || document.querySelector('.ant-layout-content')
    || document.querySelector('#root main')
    || root;
  const content = pick();
  const text = (content?.innerText ?? '').replace(/\\s+/g, ' ').trim();
  return {
    rootHtmlLength: root ? root.innerHTML.length : -1,
    contentHtmlLength: content ? content.innerHTML.length : -1,
    contentCards: content ? content.querySelectorAll('.ant-card').length : -1,
    contentTables: content ? content.querySelectorAll('.ant-table').length : -1,
    contentSteps: content ? content.querySelectorAll('.ant-steps').length : -1,
    readyState: document.readyState,
    textLength: text.length,
    textSample: text.slice(0, 200),
    text: text.slice(0, 20000),
  };
})()`;

async function main() {
  if (OPTS.help) {
    log("前端真浏览器 E2E（系统 Chromium + CDP，零新依赖）");
    log("  用法：node scripts/e2e_web.mjs [--routes a,b] [--no-screenshot] [--fail-on-soft]");
    log("    --routes a,b      只跑指定路由（单页复验；未知键报错并列出可用键）");
    log("    --no-screenshot   不落截图（CI 友好）");
    log("    --fail-on-soft    把「已知在修」的软断言也计入退出码（修复落地后判闭环用）");
    log("  环境变量：E2E_BASE / E2E_CHROME / E2E_IDLE_MS / E2E_MAX_WAIT_MS");
    log(`  可用路由：${ROUTES.map((r) => r.key).join(",")}`);
    return 0;
  }
  await mkdir(OUT_DIR, { recursive: true });
  const profileDir = await mkdtemp(path.join(tmpdir(), "e2e-web-profile-"));
  const port = 20000 + Math.floor(Math.random() * 20000);
  log("前端真浏览器 E2E");
  log(`  目标：${BASE}`);
  log(`  Chromium：${CHROME}`);
  log(`  CDP 端口：${port}｜空闲窗口 ${IDLE_MS}ms｜等待上限 ${MAX_WAIT_MS}ms`);
  log(`  产物：${OUT_DIR}`);

  let child = null;
  const report = {
    startedAt: new Date().toISOString(),
    base: BASE,
    chrome: CHROME,
    cdpPort: port,
    outDir: OUT_DIR,
    options: { routes: OPTS.routes, screenshot: OPTS.screenshot, failOnSoft: OPTS.failOnSoft },
    routes: [],
    defects: [],
    observations: [],
    softAssertions: [],
    coverageGaps: [],
  };

  try {
    const launched = await launchChromium(port, profileDir);
    child = launched.child;
    log(`  Chromium 就绪（${launched.version.Browser ?? "?"}）`);
    const target = await findPageTarget(port);
    const cdp = await Cdp.connect(target.webSocketDebuggerUrl);
    for (const m of ["Page.enable", "Runtime.enable", "Log.enable", "Network.enable"]) await cdp.send(m);
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1440,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });

    // ---- 在途请求追踪（真实网络空闲判据）----
    const net = { inflight: new Map(), lastEventAt: Date.now() };
    const apiReqs = [];
    cdp.on("Network.requestWillBeSent", (p) => {
      const u = p.request?.url ?? "";
      net.inflight.set(p.requestId, u.slice(0, 160));
      net.lastEventAt = Date.now();
      if (u.includes("/api/wb/")) {
        const rec = { requestId: p.requestId, endpoint: u.split("/api/wb/")[1], status: null, failed: null };
        apiReqs.push(rec);
      }
    });
    const done = (p) => {
      net.inflight.delete(p.requestId);
      net.lastEventAt = Date.now();
    };
    cdp.on("Network.loadingFinished", done);
    cdp.on("Network.loadingFailed", done);

    let loadFired = false;
    cdp.on("Page.loadEventFired", () => (loadFired = true));

    // `--routes a,b`：只跑指定路由（单页复验）。未知键直接失败并列出可用键，
    // 避免「以为跑了、其实拼错键被静默忽略」。
    let selected = ROUTES;
    if (OPTS.routes !== null) {
      const wanted = OPTS.routes.split(",").map((s) => s.trim()).filter(Boolean);
      const known = new Set(ROUTES.map((r) => r.key));
      const unknown = wanted.filter((k) => !known.has(k));
      if (unknown.length) {
        report.fatal = `--routes 含未知路由：${unknown.join(",")}（可用：${[...known].join(",")}）`;
        throw new Error(report.fatal);
      }
      selected = ROUTES.filter((r) => wanted.includes(r.key));
      log(`  路由筛选：${selected.map((r) => r.key).join(",")}（共 ${selected.length}/${ROUTES.length}）`);
    }

    for (const route of selected) {
      const state = { console: [], exceptions: [], logs: [], network: [] };
      cdp.on("Runtime.consoleAPICalled", (p) => {
        if (["error", "warning", "assert"].includes(p.type)) {
          const text = (p.args ?? [])
            .map((a) => a.value ?? a.description ?? a.unserializableValue ?? a.type)
            .join(" ");
          state.console.push({ level: p.type, text: text.slice(0, 400) });
        }
      });
      cdp.on("Runtime.exceptionThrown", (p) => {
        const d = p.exceptionDetails ?? {};
        state.exceptions.push({
          text: (d.exception?.description ?? d.text ?? "未知异常").slice(0, 500),
          url: d.url ?? null,
          line: d.lineNumber ?? null,
        });
      });
      cdp.on("Log.entryAdded", (p) => {
        const e = p.entry ?? {};
        if (["error", "warning"].includes(e.level)) {
          state.logs.push({ level: e.level, source: e.source ?? null, text: (e.text ?? "").slice(0, 400) });
        }
      });
      cdp.on("Network.responseReceived", (p) => {
        const r = p.response ?? {};
        const rec = apiReqs.find((x) => x.requestId === p.requestId);
        if (rec) rec.status = r.status;
        if (typeof r.status === "number" && r.status >= 400) {
          state.network.push({ kind: "http", status: r.status, url: (r.url ?? "").slice(0, 300), type: p.type ?? null });
        }
      });
      cdp.on("Network.loadingFailed", (p) => {
        const rec = apiReqs.find((x) => x.requestId === p.requestId);
        if (rec) rec.failed = p.errorText ?? "失败";
        state.network.push({
          kind: "failed",
          status: null,
          url: net.inflight.get(p.requestId) ?? null,
          type: p.type ?? null,
          error: `${p.errorText ?? "加载失败"}${p.blockedReason ? ` (${p.blockedReason})` : ""}`,
        });
      });

      const routeReqs = [];
      const url = `${BASE}/#/${route.key}`;
      loadFired = false;
      await cdp.send("Page.navigate", { url: "about:blank" });
      await sleep(120);
      net.inflight.clear();
      apiReqs.length = 0;
      net.lastEventAt = Date.now();
      await cdp.send("Page.navigate", { url });

      const dl = Date.now() + 15000;
      while (!loadFired && Date.now() < dl) await sleep(80);
      const domEarly = await evaluate(cdp, DOM_PROBE);

      // 真实网络空闲：在途为 0 且 IDLE_MS 内无网络事件
      const waitStart = Date.now();
      let idle = false;
      while (Date.now() - waitStart < MAX_WAIT_MS) {
        if (net.inflight.size === 0 && Date.now() - net.lastEventAt >= IDLE_MS) {
          idle = true;
          break;
        }
        await sleep(80);
      }
      const waitMs = Date.now() - waitStart;
      const pendingAtSnapshot = [...net.inflight.values()];

      // DOM 稳定：连续 3 次采样（≥1s）文本长度与卡片/表格数不变。SPA 存在依赖式二次请求波
      // （positions/deals/equity 依赖 snapshot 返回的 mode），只看网络空闲会在两波之间误判。
      const sig = (d) => `${d.textLength}|${d.contentCards}|${d.contentTables}`;
      let domFinal = await evaluate(cdp, DOM_PROBE);
      let stableRuns = 1;
      let lastSig = sig(domFinal);
      const stableStart = Date.now();
      while (Date.now() - stableStart < 12000) {
        await sleep(500);
        const cur = await evaluate(cdp, DOM_PROBE);
        const curSig = sig(cur);
        stableRuns = curSig === lastSig ? stableRuns + 1 : 1;
        lastSig = curSig;
        domFinal = cur;
        if (stableRuns >= 3 && net.inflight.size === 0) break;
      }
      const domStableMs = Date.now() - stableStart;

      const contentText = domFinal.text ?? "";
      const earlyText = domEarly.text ?? "";

      // 锚点求值（状态感知）：条目为字符串时按「必须出现」；为数组时按「任一出现即满足」
      // （数据态/空态的可接受替代），并记录命中的是哪一支，便于报告区分「数据态/空态」。
      const evalAnchor = (spec) => {
        const alts = Array.isArray(spec) ? spec : [spec];
        const hit = alts.find((alt) => contentText.includes(alt)) ?? null;
        return {
          anchor: alts.join(" | "),
          alternatives: alts,
          found: hit !== null,
          matched: hit,
          state: hit === null ? "missing" : (alts.length > 1 ? (hit === alts[0] ? "data" : "empty") : "single"),
        };
      };
      const required = route.required.map(evalAnchor);
      const optional = route.optional.map((a) => ({ anchor: a, found: contentText.includes(a) }));
      const missingRequired = required.filter((r) => !r.found).map((r) => r.anchor);
      const haystack = contentText.toLowerCase();
      const crashText = CRASH_PATTERNS.find((p) => haystack.includes(p)) ?? null;
      const blank =
        domFinal.rootHtmlLength < 50 || (domFinal.contentHtmlLength >= 0 && domFinal.contentHtmlLength < 50);
      // 空态断言（加载窗口 = 把未知说成没有）
      const earlyLoadingFacts = EMPTY_STATE_CLAIMS.filter((t) => earlyText.includes(t));
      const finalLoadingFacts = EMPTY_STATE_CLAIMS.filter((t) => contentText.includes(t));
      const loadingResolved = earlyLoadingFacts.filter((t) => !finalLoadingFacts.includes(t));
      // 失败断言：只有「带错误详情」且**没有**失败请求支撑时才算加载态说谎
      const earlyFailureClaim = FAILURE_CLAIM_RE.test(earlyText);

      const consoleErrors = state.console.filter((c) => c.level === "error");
      const consoleWarnings = state.console.filter((c) => c.level === "warning");
      const http5xx = state.network.filter((n) => n.kind === "http" && n.status >= 500);
      const http4xx = state.network.filter((n) => n.kind === "http" && n.status >= 400 && n.status < 500);
      const failed = state.network.filter((n) => n.kind === "failed");

      const entry = {
        route: route.key,
        name: route.name,
        url,
        loadFired,
        idle,
        idleWaitMs: waitMs,
        domStableMs,
        apiRequests: apiReqs.map((r) => ({ endpoint: r.endpoint, status: r.status, failed: r.failed })),
        pendingAtSnapshot,
        early: { textLength: domEarly.textLength, textSample: domEarly.textSample },
        dom: domFinal,
        required,
        optional,
        missingRequired,
        consoleErrors,
        consoleWarnings,
        exceptions: state.exceptions,
        logs: state.logs,
        http5xx,
        http4xx,
        failedRequests: failed,
        blank,
        crashText,
        earlyLoadingFacts,
        finalLoadingFacts,
        loadingResolved,
        earlyFailureClaim,
      };

      if (OPTS.screenshot && SHOT_ROUTES.has(route.key)) {
        try {
          const shot = await cdp.send("Page.captureScreenshot", { format: "png" });
          entry.screenshot = path.join(OUT_DIR, `${route.key}.png`);
          await writeFile(entry.screenshot, Buffer.from(shot.data, "base64"));
        } catch (error) {
          entry.screenshotError = String(error?.message ?? error);
        }
      }
      report.routes.push(entry);

      const flags = [];
      if (!loadFired) flags.push("无loadEvent");
      if (!idle) flags.push(`未空闲(在途${pendingAtSnapshot.length})`);
      if (blank) flags.push("白屏");
      if (crashText) flags.push(`崩溃:${crashText}`);
      if (consoleErrors.length) flags.push(`console错误x${consoleErrors.length}`);
      if (state.exceptions.length) flags.push(`异常x${state.exceptions.length}`);
      if (http5xx.length) flags.push(`5xx x${http5xx.length}`);
      if (failed.length) flags.push(`请求失败x${failed.length}`);
      if (missingRequired.length) flags.push(`必需锚点缺失:${missingRequired.join(",")}`);
      log(
        `  ${route.key.padEnd(10)} 内容${String(domFinal.textLength).padStart(5)}字 ` +
          `卡${String(domFinal.contentCards).padStart(2)} 表${String(domFinal.contentTables).padStart(2)} ` +
          `空闲${String(waitMs).padStart(5)}ms 稳定${String(domStableMs).padStart(5)}ms ` +
          `${flags.length ? "⚠ " + flags.join(" ") : "✓"}` +
          `${entry.screenshot ? " 📷" : ""}`,
      );
    }

    // ---- 结论 ----
    for (const r of report.routes) {
      const push = (severity, category, detail) =>
        report.defects.push({ severity, category, route: r.route, url: r.url, detail });
      if (r.blank) push("阻断", "白屏", `#root=${r.dom.rootHtmlLength} 内容区=${r.dom.contentHtmlLength}`);
      if (r.crashText) push("阻断", "React 崩溃文案", `命中「${r.crashText}」`);
      for (const e of r.consoleErrors) push("严重", "console.error", e.text);
      for (const e of r.exceptions) push("严重", "未捕获异常", `${e.text}${e.url ? ` @ ${e.url}:${e.line}` : ""}`);
      for (const n of r.http5xx) push("严重", "HTTP 5xx", `${n.status} ${n.url}`);
      for (const n of r.failedRequests) push("严重", "请求加载失败", `${n.error} ${n.url ?? ""}`);
      for (const a of r.required.filter((x) => !x.found)) push("重要", "内容区必需锚点缺失", `未找到「${a.anchor}」`);
      if (!r.loadFired) push("重要", "导航未完成", "15s 内无 loadEventFired");
      if (!r.idle) push("重要", "网络未空闲", `等待 ${r.idleWaitMs}ms 后仍有 ${r.pendingAtSnapshot.length} 个在途请求`);

      const anyRequestFailed = (r.failedRequests?.length ?? 0) > 0
        || (r.http5xx?.length ?? 0) > 0
        || (r.apiRequests ?? []).some((q) => q.failed || (q.status ?? 0) >= 400);
      if (r.earlyFailureClaim && anyRequestFailed) {
        // 加载窗口就显示了失败，且确有请求失败 → **诚实展示**，只作观察（带原因）
        report.observations.push({
          severity: "次要",
          category: "加载窗口内展示读取失败（有失败请求支撑）",
          route: r.route,
          detail: "页面在读取失败时如实显示错误详情；底层失败请求：" +
            JSON.stringify((r.apiRequests ?? []).filter((q) => q.failed || (q.status ?? 0) >= 400).slice(0, 3)),
        });
      } else if (r.earlyFailureClaim) {
        const spec = SOFT_ASSERTIONS.LOADING_FACT_CLAIMS;
        report.softAssertions.push({
          name: spec.name,
          title: spec.title,
          knownBeingFixed: spec.knownBeingFixed,
          route: r.route,
          failed: true,
          detail: "加载窗口出现「读取失败：…」但**没有任何失败请求**（加载态说谎）",
          note: spec.note,
        });
      }
      if (r.earlyLoadingFacts.length) {
        report.observations.push({
          severity: "重要",
          category: "加载窗口内出现事实断言",
          route: r.route,
          detail:
            `加载窗口出现 ${r.earlyLoadingFacts.join("、")}` +
            (r.loadingResolved.length ? `（稳态已消失：${r.loadingResolved.join("、")}，属瞬时）` : "（稳态仍在）"),
        });
        // **软断言**（报告但不令退出码非零，除非 --fail-on-soft）：
        // 加载窗口把「尚未取到」渲染成「确实没有」——对应修复已知在修，故默认不阻断。
        const spec = SOFT_ASSERTIONS.LOADING_FACT_CLAIMS;
        report.softAssertions.push({
          name: spec.name,
          title: spec.title,
          knownBeingFixed: spec.knownBeingFixed,
          route: r.route,
          failed: true,
          detail: `加载窗口出现事实断言：${r.earlyLoadingFacts.join("、")}`
            + (r.loadingResolved.length ? "" : "（**稳态仍在**，比瞬时更严重）"),
          note: spec.note,
        });
      }
      for (const a of r.optional.filter((x) => !x.found)) {
        report.observations.push({
          severity: "次要",
          category: "数据相关锚点未出现",
          route: r.route,
          detail: `「${a.anchor}」未渲染（库内数据少时属预期，需人工判断）`,
        });
      }
      for (const n of r.http4xx) {
        report.observations.push({ severity: "重要", category: "HTTP 4xx", route: r.route, detail: `${n.status} ${n.url}` });
      }
      for (const w of r.consoleWarnings) {
        report.observations.push({ severity: "次要", category: "console.warning", route: r.route, detail: w.text });
      }
      for (const l of r.logs) {
        report.observations.push({
          severity: "次要",
          category: `浏览器日志(${l.source ?? "?"})`,
          route: r.route,
          detail: l.text,
        });
      }
    }

    report.summary = {
      routes: report.routes.length,
      withScreenshot: report.routes.filter((r) => r.screenshot).length,
      anchorStates: report.routes.map((r) => ({
        route: r.route,
        required: r.required.map((a) => `${a.anchor}=${a.found ? a.state : "missing"}`),
      })),
      passed: report.routes.filter(
        (r) =>
          r.loadFired &&
          r.idle &&
          !r.blank &&
          !r.crashText &&
          r.consoleErrors.length === 0 &&
          r.exceptions.length === 0 &&
          r.http5xx.length === 0 &&
          r.failedRequests.length === 0 &&
          r.required.every((a) => a.found),
      ).length,
      blocked: report.defects.filter((d) => d.severity === "阻断").length,
      severe: report.defects.filter((d) => d.severity === "严重").length,
      important: report.defects.filter((d) => d.severity === "重要").length,
      observations: report.observations.length,
      softFailed: report.softAssertions.filter((s) => s.failed).length,
      softKnownBeingFixed: report.softAssertions.filter((s) => s.failed && s.knownBeingFixed).length,
    };
    report.finishedAt = new Date().toISOString();
  } catch (error) {
    report.fatal = String(error?.message ?? error);
    report.coverageGaps.push(`E2E 运行中断：${report.fatal}`);
  } finally {
    if (child && child.exitCode === null) {
      child.kill("SIGTERM");
      const dl = Date.now() + 5000;
      while (child.exitCode === null && Date.now() < dl) await sleep(100);
      if (child.exitCode === null) child.kill("SIGKILL");
      log(`  Chromium 已退出（code=${child.exitCode}）`);
    }
    await rm(profileDir, { recursive: true, force: true }).catch(() => {});
  }

  const reportFile = path.join(OUT_DIR, "report.json");
  await writeFile(reportFile, JSON.stringify(report, null, 2));
  const s = report.summary ?? {};
  log("");
  log("=== 结果 ===");
  log(
    `  路由 ${s.routes ?? 0} / 通过 ${s.passed ?? 0}｜阻断 ${s.blocked ?? 0} 严重 ${s.severe ?? 0} ` +
      `重要 ${s.important ?? 0}｜观察项 ${s.observations ?? 0}${report.fatal ? `｜致命：${report.fatal}` : ""}`,
  );
  log(
    `  软断言失败 ${s.softFailed ?? 0}（其中「已知在修」${s.softKnownBeingFixed ?? 0}）` +
      `｜--fail-on-soft＝${OPTS.failOnSoft ? "开" : "关"}`,
  );
  log(`  报告：${reportFile}`);
  if (report.softAssertions?.length) {
    log("  软断言（默认不计入退出码）：");
    for (const a of report.softAssertions.slice(0, 8)) {
      log(
        `    [${a.knownBeingFixed ? "已知在修" : "软"}] ${a.name} @ ${a.route}：` +
          `${String(a.detail).slice(0, 130)}`,
      );
    }
  }
  if (report.defects?.length) {
    log("  缺陷：");
    for (const d of report.defects.slice(0, 20)) {
      log(`    [${d.severity}] ${d.route} ${d.category}：${String(d.detail).slice(0, 150)}`);
    }
  }
  if (report.observations?.length) {
    log("  观察项（不判缺陷）：");
    for (const o of report.observations.slice(0, 12)) {
      log(`    [${o.severity}] ${o.route} ${o.category}：${String(o.detail).slice(0, 150)}`);
    }
  }
  const softFatal = OPTS.failOnSoft && (report.softAssertions?.length ?? 0) > 0;
  process.exit(
    Boolean(report.fatal) || (report.defects?.length ?? 0) > 0 || softFatal ? 1 : 0,
  );
}

main()
  .then((code) => process.exit(typeof code === "number" ? code : 0))
  .catch((error) => {
    log(`E2E 致命错误：${String(error?.stack ?? error)}`);
    process.exit(1);
  });
