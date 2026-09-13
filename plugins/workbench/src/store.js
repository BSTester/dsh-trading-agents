import { summarizeBrokerActivity } from "./broker_trades.js";
import { randomUUID } from "node:crypto";
import { closeSync, existsSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const RATINGS = new Set(["Buy", "Overweight", "Hold", "Underweight", "Sell"]);
const LIMIT = 100;

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

  snapshot() {
    this.flushObservations();
    const mode = this.readMode();
    const state = this.read();
    const activity = state.activity.filter(row => row.mode === mode).reverse();
    return {
      version: 1, mode, generated_at: new Date().toISOString(),
      runs: state.runs.filter(row => row.mode === mode).reverse(),
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
      const result = { id: run.id, ticker, mode: run.mode, session_id: sessionId, rating: args.rating,
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
