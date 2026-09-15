import { summarizeBrokerActivity } from "./broker_trades.js";
import { zh } from "./labels.js";
import { randomUUID } from "node:crypto";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const RATINGS = new Set(["Buy", "Overweight", "Hold", "Underweight", "Sell"]);
const LIMIT = 100;
// 研究 run 超过这个时长仍是 running，就认为发起它的会话已中断。
// 不这么做的话，被中断的会话会留下一个**永远显示"进行中"**的 run，
// 在面板上看起来像还在跑。派生状态而不改写历史：原始数据保持不动。
const ABANDONED_AFTER_MS = 2 * 60 * 60 * 1000;

export class WorkbenchError extends Error {}

/**
 * 实盘业务确认的存活时长。超时按**拒绝**处理（fail-closed）：
 * 一笔没人看的委托不该因为"等太久"就自动生效。
 */
export const CONFIRM_TTL_MS = 120_000;

/**
 * 需要业务确认的实盘操作类型（按工具名后缀判定）。
 *
 * 三类都确认，包括撤单：确认回答的是"这笔业务参数对不对"，撤错单同样是业务错误
 * （撤掉保护性止损、撤错 order_id）。放宽只需要改这一张表。
 */
export const CONFIRM_OPERATIONS = Object.freeze({ input: "下单", modify: "改单", cancel: "撤单" });

/** 从工具名取出操作类型：`mcp__futu__trading_input_order` → `input`。 */
export function orderOperation(tool) {
  const match = /(?:^|_)(input|modify|cancel)_order$/.exec(String(tool ?? ""));
  return match ? match[1] : null;
}

/** 方向：1=Buy 2=Sell（工具 schema 明文）。 */
const ORDER_SIDE = { 1: "买入", 2: "卖出", "1": "买入", "2": "卖出" };
/**
 * 市场代码 → 中文提示。取值来自 `sim_trade_account_list` 的**实测返回**
 * （港股账户 market=1、A股账户 market=3、美股账户 market=100），
 * 不是从文档猜的：P4 文档明确要求"使用实际返回的 market_id"下单。
 * 未列出的代码不猜，原样显示并提示核对。
 */
const MARKET_HINT = { 1: "港股", 3: "A股", 100: "美股" };

/**
 * 把券商写操作的工具入参渲染成中文订单摘要。
 *
 * 未知字段一律原样列出、未知枚举附上原始代码——摘要的作用是让人核对，
 * 不是替人解释。宁可显示得笨一点，也不能把没认出来的字段藏起来。
 */
export function describeOrderArgs(tool, args = {}) {
  const normalized = {};
  for (const [key, value] of Object.entries(args ?? {})) normalized[key.toLowerCase()] = value;
  const raw = (key) => normalized[key];
  const fields = [];
  const push = (label, value) => {
    if (value === undefined || value === null || value === "") return;
    fields.push({ label, value: String(value) });
  };
  const market = raw("market");
  push("账户", raw("acc_id"));
  push("市场", market === undefined ? undefined
    : MARKET_HINT[market] ? `${market}（${MARKET_HINT[market]}）` : `${market}（未识别的市场代码，请核对）`);
  push("标的", raw("symbol"));
  const side = raw("order_side") ?? raw("trd_side");
  push("方向", side === undefined ? undefined
    : ORDER_SIDE[side] ? `${ORDER_SIDE[side]}（order_side=${side}）` : `未识别（order_side=${side}，请核对）`);
  push("数量", raw("qty") ?? raw("quantity"));
  push("价格", raw("price"));
  push("订单类型", raw("order_type"));
  push("订单号", raw("order_id"));
  push("有效期", raw("time_in_force") ?? raw("order_trade_time_type"));
  push("备注", raw("text") ?? raw("remark"));
  return { tool, fields, raw: args ?? {} };
}
class WorkbenchBusyError extends WorkbenchError {}

function text(value, label, max = 200) {
  if (typeof value !== "string" || !value.trim() || value.length > max) {
    throw new WorkbenchError(`Invalid ${label}`);
  }
  return value.trim();
}

function modeValue(mode) {
  if (mode !== "sim" && mode !== "live") throw new WorkbenchError("Invalid account mode; expected sim/live");
  return mode;
}

function atomicWrite(file, content) {
  const temporary = `${file}.${randomUUID()}.tmp`;
  try {
    writeFileSync(temporary, content, { mode: 0o600, flag: "wx" });
    renameSync(temporary, file);
  } finally {
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

function sanitize(value) {
  const serialized = JSON.stringify(value, (key, item) =>
    /token|secret|password|authorization|cookie/i.test(key) ? "[redacted]" : item);
  if (serialized === undefined) return null;
  if (serialized.length > 64_000) return { truncated: true, note: "完整响应请查看 Harness 工具记录" };
  return JSON.parse(serialized);
}

function emptyState() {
  return { version: 1, runs: [], reports: [], previews: [], activity: [], broker: {} };
}

/**
 * 给 run 补一个派生状态：running 且开始时间已超过阈值 → abandoned（会话已中断）。
 * 只影响读出的视图，不写回磁盘——历史记录保持原样。
 */
export function withRunStatus(run, now = Date.now()) {
  if (!run || run.status !== "running") return run;
  const started = Date.parse(run.started_at ?? "");
  if (!Number.isFinite(started)) return run;
  if (now - started < ABANDONED_AFTER_MS) return run;
  return { ...run, status: "abandoned" };
}

export { ABANDONED_AFTER_MS };

export class WorkbenchStore {
  /**
   * 待确认的实盘业务动作（内存态，不落盘）。
   *
   * 为什么只在内存：确认是"此刻等人回答"的瞬时状态，进程重启后本就无人回答，
   * 落盘反而会让一个陈旧请求在重启后复活。持久化的是**事件留痕**（activity），
   * 用于事后追溯谁在何时批了哪一笔。
   */
  pendingConfirmation = null;

  constructor(home = process.env.DSH_HOME || path.join(os.homedir(), ".dsh")) {
    this.home = home;
    this.file = path.join(home, "trading-workbench.json");
    this.modeFile = path.join(home, "trading-account-mode");
    this.observations = path.join(home, "trading-observations");
    this.recordingError = null;
  }

  get inFlight() {
    try {
      return readdirSync(this.home).filter(name => name.startsWith("trading-call-") && name.endsWith(".active")).length;
    } catch (error) {
      if (error.code === "ENOENT") return 0;
      throw error;
    }
  }

  readMode() {
    try {
      return modeValue(readFileSync(this.modeFile, "utf8").trim());
    } catch (error) {
      if (error.code === "ENOENT") return "sim";
      throw error;
    }
  }

  read() {
    let state;
    try {
      state = JSON.parse(readFileSync(this.file, "utf8"));
    } catch (error) {
      if (error.code === "ENOENT") return emptyState();
      throw error;
    }
    if (state.version !== 1 || !["runs", "reports", "previews", "activity"].every(key => Array.isArray(state[key]))
        || !state.broker || typeof state.broker !== "object") {
      throw new WorkbenchError("Invalid workbench state; restore a valid backup");
    }
    return state;
  }

  update(fn, afterCommit) {
    mkdirSync(this.home, { recursive: true });
    const lock = path.join(this.home, "trading-workbench.lock");
    let fd;
    try {
      fd = openSync(lock, "wx", 0o600);
    } catch (error) {
      if (error.code === "EEXIST") throw new WorkbenchBusyError("Workbench is busy; retry after the other writer finishes");
      throw error;
    }
    try {
      const state = this.read();
      const result = fn(state);
      for (const key of ["runs", "reports", "previews", "activity"]) state[key] = state[key].slice(-LIMIT);
      atomicWrite(this.file, JSON.stringify(state));
      if (afterCommit) afterCommit(result);
      return result;
    } finally {
      closeSync(fd);
      unlinkSync(lock);
    }
  }

  event(state, value) {
    const event = { id: randomUUID(), at: new Date().toISOString(), ...value };
    state.activity.push(event);
    return event;
  }

  /**
   * 显式取消一个仍在 running 的 run：标记为 cancelled 并保留记录。
   *
   * 与 pruneAbandonedRuns 的区别：取消是**用户主动操作**，保留记录可供追溯；
   * 删除是清理。与派生的 abandoned 也不同——abandoned 是"会话死了"的推断，
   * cancelled 是"人决定不做了"。
   */
  cancelRun(id) {
    let settled = null;
    this.update((state) => {
      const run = (state.runs ?? []).find((row) => row.id === id);
      if (!run) throw new WorkbenchError(`Unknown run: ${id}`);
      if (run.status !== "running") {
        throw new WorkbenchError(`Run is already settled: ${run.status}`);
      }
      run.status = "cancelled";
      run.settled_at = new Date().toISOString();
      this.event(state, { kind: "research_cancelled", mode: run.mode, ticker: run.ticker,
        run_id: run.id, session_id: run.session_id });
      settled = { ...run };
    });
    return settled;
  }

  /** 取消所有超过给定时长仍停留在 running 的 run，返回被取消的 id 列表。 */
  cancelStaleRuns({ now = Date.now(), olderThanMs = ABANDONED_AFTER_MS } = {}) {
    const cancelled = [];
    this.update((state) => {
      for (const run of state.runs ?? []) {
        if (run.status !== "running") continue;
        const started = Date.parse(run.started_at ?? "");
        if (Number.isFinite(started) && now - started >= olderThanMs) {
          run.status = "cancelled";
          run.settled_at = new Date().toISOString();
          this.event(state, { kind: "research_cancelled", mode: run.mode, ticker: run.ticker,
            run_id: run.id, session_id: run.session_id });
          cancelled.push(run.id);
        }
      }
    });
    return cancelled;
  }

  /**
   * 删除「被中断且从未发布过研报」的 run，返回被删除的 id 列表。
   *
   * 为什么需要：会话中断会在库里留下永远 running 的 run。展示层已用派生状态
   * 把它标成 abandoned，但数据本身也该能清掉，否则会一直占着 LIMIT 名额。
   * **有研报的 run 一律保留**——那是真实产物，不能当垃圾清掉。
   */
  pruneAbandonedRuns({ now = Date.now(), olderThanMs = ABANDONED_AFTER_MS } = {}) {
    const removed = [];
    this.update((state) => {
      // 报告的主键 id 就是 run id（publishResearch 用 run.id 作为报告 id），没有 run_id 字段
      const published = new Set((state.reports ?? [])
        .flatMap((row) => [row.id, row.run_id]).filter(Boolean));
      state.runs = (state.runs ?? []).filter((run) => {
        if (run.status !== "running") return true;
        const started = Date.parse(run.started_at ?? "");
        if (!Number.isFinite(started) || now - started < olderThanMs) return true;
        if (published.has(run.id)) return true;
        removed.push(run.id);
        return false;
      });
    });
    return removed;
  }

  snapshot() {
    this.flushObservations();
    const mode = this.readMode();
    const state = this.read();
    const activity = state.activity.filter(row => row.mode === mode).reverse();
    return {
      version: 1, mode, generated_at: new Date().toISOString(),
      runs: state.runs.filter(row => row.mode === mode).reverse().map(row => withRunStatus(row)),
      reports: state.reports.filter(row => row.mode === mode).reverse(),
      previews: state.previews.filter(row => row.mode === mode).reverse(),
      activity,
      // 派生视图：把工具调用归纳成交易事实（不写回磁盘，读时计算）
      trade_summary: summarizeBrokerActivity(activity),
      broker: state.broker[mode] ?? null,
      in_flight: this.inFlight,
      // 待确认的业务动作：UI 轮询 confirmation 端点读它（snapshot 是 60 秒一次，太慢）
      confirmation: this.confirmationView(),
      pending_observations: this.pendingObservations().length,
      recording_error: this.recordingError,
      notice: "交易动态来自 Harness 最近的富途工具响应，不是券商成交推送；下单、撤单及对话请在 Harness 中完成。",
    };
  }

  /**
   * 发起一次**业务确认**并等待用户在工作台作答。
   *
   * 与 DSH 的 approval 系统完全无关：权限确认回答的是"这个动作准不准做"，
   * 由会话的 approval policy 裁决；而 full-access（policy="never"）下
   * `approval.decide()` 会直接返回 rejected，连问都不问
   * （dsh-user-approval 的 decide()）。"这笔业务参数对不对"是交易动作的固有
   * 环节，不该因为系统被设成免打扰就静默拒绝。
   *
   * 因此写操作在这里无条件等人确认，调用方据结果返回 allow/deny，
   * **永不返回 {kind:"ask"}** —— 权限系统无从介入。
   *
   * 回答只能来自工作台 RPC（`decideConfirmation`）；工具参数无法自证已确认，
   * 模型不能自己批自己。
   *
   * @returns {Promise<{decision:"approved"|"rejected", reason:string, id:string}>}
   *          超时/取消一律 rejected（fail-closed）。
   */
  requestConfirmation({ tool, mode, args, session_id, ttlMs = CONFIRM_TTL_MS, signal } = {}) {
    modeValue(mode);
    if (mode !== "live") throw new WorkbenchError("只有实盘写操作需要业务确认");
    if (typeof tool !== "string" || !tool.trim()) throw new WorkbenchError("Invalid tool name");
    // 一次只允许一笔：两笔并发时"我看到的是哪一笔"会变模糊，宁可让后来的重试
    if (this.pendingConfirmation) {
      return Promise.resolve({ decision: "rejected", id: null,
        reason: "已有一笔待确认的实盘操作，请先在工作台处理它再重试" });
    }

    const id = randomUUID();
    const at = new Date();
    const request = {
      id, at: at.toISOString(), expires_at: new Date(at.getTime() + ttlMs).toISOString(),
      mode, tool, session_id: session_id ?? "unknown",
      operation: CONFIRM_OPERATIONS[orderOperation(tool)] ?? "实盘写操作",
      summary: describeOrderArgs(tool, args),
      status: "pending", decided_at: null, decided_by: null,
    };
    this.pendingConfirmation = request;
    this.recordConfirmationEvent("confirmation_requested", request);

    return new Promise((resolve) => {
      let settled = false;
      const settle = (decision, reason) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener?.("abort", onAbort);
        resolve({ decision, reason, id });
      };
      const onAbort = () => {
        request.status = "cancelled";
        this.pendingConfirmation = null;
        this.recordConfirmationEvent("confirmation_cancelled", request, "会话已中断");
        settle("rejected", "会话已中断");
      };
      // 注意**不能** unref：这个定时器保证"没人回答也会到期拒绝"。
      // unref 掉之后，进程在没有其他活时可以直接退出，Promise 永不 settle ——
      // 一个正在等人的工具调用会变成悬空。TTL 只有两分钟，钉住这段时间是应该的。
      const timer = setTimeout(() => {
        request.status = "expired";
        this.pendingConfirmation = null;
        this.recordConfirmationEvent("confirmation_expired", request, `超过 ${Math.round(ttlMs / 1000)} 秒未确认`);
        settle("rejected", `超过 ${Math.round(ttlMs / 1000)} 秒未确认，按拒绝处理`);
      }, ttlMs);
      // settle 是内部句柄，只在内存里挂着；confirmationView() 不会把它送出去
      request.settle = settle;
      if (signal?.aborted) { onAbort(); return; }
      signal?.addEventListener?.("abort", onAbort, { once: true });
    });
  }

  /** 当前待确认项的只读视图（不含 settle，避免把内部句柄送到界面）。 */
  confirmationView() {
    const request = this.pendingConfirmation;
    if (!request) return null;
    return { id: request.id, at: request.at, expires_at: request.expires_at,
      mode: request.mode, tool: request.tool, operation: request.operation,
      session_id: request.session_id, status: request.status, summary: request.summary };
  }

  /**
   * 用户在工作台作出决定。**这是唯一能批准实盘操作的入口**。
   * @param {{id:string, decision:"approved"|"rejected"}} input
   */
  decideConfirmation({ id, decision } = {}) {
    if (decision !== "approved" && decision !== "rejected") {
      throw new WorkbenchError("Invalid decision; expected approved/rejected");
    }
    const request = this.pendingConfirmation;
    if (!request) throw new WorkbenchError("没有待确认的实盘操作（可能已超时或被处理）");
    if (request.id !== id) throw new WorkbenchError("确认编号不匹配；可能已被处理或已超时");
    this.pendingConfirmation = null;
    request.status = decision;
    request.decided_at = new Date().toISOString();
    // 唯一能批准实盘操作的入口就是工作台 RPC，所以主体恒为工作台界面
    request.decided_by = "workbench-ui";
    this.recordConfirmationEvent(
      decision === "approved" ? "confirmation_approved" : "confirmation_rejected", request);
    request.settle?.(decision, decision === "approved" ? "用户在工作台确认" : "用户在工作台拒绝");
    return { id, decision, tool: request.tool, operation: request.operation };
  }

  /** 确认链路的活动留痕；写盘失败不影响确认本身（锁被占用时不能卡住等人回答）。 */
  recordConfirmationEvent(kind, request, note) {
    try {
      this.update((state) => this.event(state, {
        kind, mode: request.mode, tool: request.tool, operation: request.operation,
        confirmation_id: request.id, session_id: request.session_id, ...(note ? { note } : {}),
        summary: request.summary?.fields ?? [],
      }));
    } catch { /* 留痕尽力而为，绝不因此改变确认结论 */ }
  }

  switchMode({ mode, expected_mode, confirmation }) {
    modeValue(mode);
    modeValue(expected_mode);
    return this.update((state) => {
      const current = this.readMode();
      if (current !== expected_mode) throw new WorkbenchError("Account mode changed; refresh before switching");
      if (this.inFlight) throw new WorkbenchError("有账户调用正在进行，请结束后切换");
      if (mode === "live" && current !== "live" && confirmation !== "确认实盘") {
        throw new WorkbenchError("请输入「确认实盘」；切换模式不等于授权下单");
      }
      atomicWrite(this.modeFile, `${mode}\n`);
      if (mode !== current) this.event(state, { kind: "mode_changed", mode, previous_mode: current });
      return { mode, previous_mode: current, order_authorized: false };
    });
  }

  enterBrokerCall(expectedMode) {
    const lease = path.join(this.home, `trading-call-${randomUUID()}.active`);
    try {
      this.update(() => {
        if (this.readMode() !== modeValue(expectedMode)) throw new WorkbenchError("Account mode changed before dispatch");
        writeFileSync(lease, JSON.stringify({ pid: process.pid, mode: expectedMode, at: new Date().toISOString() }),
          { mode: 0o600, flag: "wx" });
      });
    } catch (error) {
      if (existsSync(lease)) unlinkSync(lease);
      throw error;
    }
    let released = false;
    return () => {
      if (!released) unlinkSync(lease);
      released = true;
    };
  }

  beginResearch({ ticker, session_id }) {
    ticker = text(ticker, "ticker", 40).toUpperCase();
    session_id = text(session_id, "session_id");
    return this.update((state) => {
      const run = { id: randomUUID(), ticker, session_id, mode: this.readMode(),
        status: "running", started_at: new Date().toISOString() };
      state.runs.push(run);
      this.event(state, { kind: "research_started", mode: run.mode, ticker, run_id: run.id, session_id });
      return run;
    });
  }

  publishResearch(args, sessionId) {
    if (!RATINGS.has(args.rating)) throw new WorkbenchError("Invalid rating; choose one of the five ratings");
    const report = text(args.report, "report", 64_000);
    const ticker = text(args.ticker, "ticker", 40).toUpperCase();
    if (!Array.isArray(args.sources) || !args.sources.length || args.sources.length > 30) {
      throw new WorkbenchError("Research sources are required (1-30)");
    }
    const sources = args.sources.map(source => {
      const as_of = text(source.as_of, "source timestamp");
      if (!Number.isFinite(Date.parse(as_of))) throw new WorkbenchError("Invalid source timestamp");
      return { name: text(source.name, "source name"),
        as_of, reference: text(source.reference, "source reference", 2000) };
    });
    return this.update((state) => {
      const run = state.runs.find(row => row.id === args.run_id);
      if (!run || run.session_id !== sessionId) throw new WorkbenchError("Unknown run or session mismatch");
      if (run.mode !== this.readMode()) throw new WorkbenchError("Account mode changed; start a new research run");
      if (run.status !== "running") throw new WorkbenchError("Research run is already settled");
      if (run.ticker !== ticker) throw new WorkbenchError("Research ticker mismatch");
      const result = { id: run.id, ticker, mode: run.mode, session_id: sessionId,
        rating: args.rating,
        // 评级码保留（枚举校验与历史记录都靠它），另存中文标签供界面与记忆使用
        rating_label: zh("RATING", args.rating),
        report, sources, published_at: new Date().toISOString() };
      state.reports.push(result);
      run.status = "completed";
      this.event(state, { kind: "research_published", mode: run.mode, ticker, run_id: run.id, session_id: sessionId });
      return result;
    });
  }

  recordPreview(kind, value, mode) {
    modeValue(mode);
    return this.update((state) => {
      const preview = { id: randomUUID(), at: new Date().toISOString(), kind, mode,
        execution_source: "local_simulation", value: sanitize(value) };
      state.previews.push(preview);
      return preview;
    });
  }

  recordObservation({ tool, mode, session_id, is_error, value }) {
    modeValue(mode);
    const event = { id: randomUUID(), at: new Date().toISOString(),
      kind: "broker_response", tool, mode, session_id, is_error: Boolean(is_error), value: sanitize(value) };
    try {
      mkdirSync(this.observations, { recursive: true });
      atomicWrite(path.join(this.observations, `${event.id}.json`), JSON.stringify(event));
      this.flushObservations();
      return event;
    } catch (error) {
      this.recordingError = `Broker response recording failed: ${error.message}`;
      throw error;
    }
  }

  pendingObservations() {
    try {
      return readdirSync(this.observations).filter(name => name.endsWith(".json"));
    } catch (error) {
      if (error.code === "ENOENT") return [];
      throw error;
    }
  }

  flushObservations() {
    if (!this.pendingObservations().length) return;
    try {
      this.update((state) => {
        const files = this.pendingObservations();
        const rows = files.map(file => JSON.parse(readFileSync(path.join(this.observations, file), "utf8")))
          .sort((a, b) => a.at.localeCompare(b.at));
        for (const row of rows) {
          modeValue(row.mode);
          if (!state.activity.some(item => item.id === row.id)) state.activity.push(row);
          if (!state.broker[row.mode] || state.broker[row.mode].at <= row.at) state.broker[row.mode] = row;
        }
        state.activity.sort((a, b) => a.at.localeCompare(b.at));
        return files;
      }, (files) => {
        // Remove the durable inbox only after commit, while still holding the writer lock.
        for (const file of files) unlinkSync(path.join(this.observations, file));
      });
    } catch (error) {
      if (error instanceof WorkbenchBusyError) return;
      this.recordingError = `Broker response recording failed: ${error.message}`;
      throw error;
    }
    this.recordingError = null;
  }
}
