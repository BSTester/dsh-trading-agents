// tradingWorkbench 服务锚（engine 对话工具与账户策略的进程内依赖）。
//
// WP7 面板退役（用户决策 2026-09-16）：legacy 面板（client.js）与 Host Connection RPC
// 已随独立服务（platform/）承接全部工作台能力而移除；实盘业务确认三方法
// （requestConfirmation/confirmationView/decideConfirmation）一并退役——
// 确认现在只存在于服务侧（platform/server/store_access.py，Web 确认卡片作答）。
// 本文件只保留：runs/reports/previews/activity/observations、模式互斥、调用租约与
// trade_summary 派生（经 broker_trades.js）。
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
      pending_observations: this.pendingObservations().length,
      recording_error: this.recordingError,
      notice: "交易动态来自 Harness 最近的富途工具响应，不是券商成交推送；下单、撤单及对话请在 Harness 中完成。",
    };
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
