#!/usr/bin/env node
// 工作台数据维护（Node 侧，因为 store 是 JS）。
//
// 典型场景：被中断的会话会留下停在 running 的研究 run，面板上一直显示"进行中"。
//   status                     查看当前数据量
//   runs                       列出全部 run 及其状态与年龄
//   cancel-run <id>            取消指定 run（标记 cancelled，保留记录）
//   cancel-stale [--hours N]   取消所有超时仍 running 的 run（默认 2 小时）
//   prune-runs [--hours N]     删除超时的孤儿 run（无研报者；有研报的保留）
//
// 数据种子（2026-09-19 新增，服务于页面字段级审计 `scripts/audit_page_fields.mjs`）：
// 信号页/研究页的字段只有在 store 里**有记录**时才会渲染，而记录的生产者是 Harness 对话
// 工具（quant_signal / run_trading_analysis / research_publish）——CI 与子代理会话拿不到
// 工具面时，只能按**同一实现**直接落库。这两个动作刻意只调用 store 自己的公开方法
// （recordPreview / beginResearch / publishResearch），不手写 JSON、不绕过校验：
//   seed-preview  --ticker X [--strategy rsi|ma_cross] [--sentiment]
//                 跑 plugins/engine/python/engine.py signal（与 quant_signal 工具同一条
//                 命令）再把结果记成 kind=signal 预览；只写 sim（store 只认 sim/live，
//                 模式取自会话模式文件，本动作不切模式）。
//   seed-research --ticker X [--rating Hold] [--report-file F] [--sources-file F]
//                 [--session <id>]
//                 beginResearch 建 run；给了 report-file/sources-file 就接着 publishResearch。
//                 **只登记记录，不产生任何 LLM 研究**：调用方自己保证 report 内容真实、
//                 sources 逐条可核（见 docs/E2E-ACCEPTANCE.md 的字段验收一节）。
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WorkbenchStore } from "../plugins/workbench/src/store.js";

const [, , action = "status", ...rest] = process.argv;

/** `--key value` 取值；缺失返回 undefined。 */
function arg(name) {
  const index = rest.indexOf(`--${name}`);
  return index === -1 ? undefined : rest[index + 1];
}

const REPO_ROOT = path.resolve(fileURLToPath(new URL("..", import.meta.url)));

function hoursArg() {
  const index = rest.indexOf("--hours");
  if (index === -1) return undefined;
  const value = Number(rest[index + 1]);
  return Number.isFinite(value) && value > 0 ? value * 3600_000 : undefined;
}

function ageMinutes(run) {
  const started = Date.parse(run.started_at ?? "");
  if (!Number.isFinite(started)) return null;
  return (Date.now() - started) / 60_000;
}

const store = new WorkbenchStore();
const state = store.read();
console.log(`数据文件：${store.file}`);
console.log(`runs=${state.runs.length} reports=${state.reports.length} `
  + `previews=${state.previews.length} activity=${state.activity.length}`);

if (action === "runs") {
  if (!state.runs.length) console.log("没有 run。");
  for (const run of state.runs) {
    const age = ageMinutes(run);
    console.log(`  ${run.id}  ${run.status.padEnd(10)} ${run.ticker.padEnd(10)} ${run.mode}`
      + `  ${age === null ? "年龄未知" : `${age.toFixed(0)} 分钟前`}`);
  }
} else if (action === "cancel-run") {
  const id = rest[0];
  if (!id) {
    console.error("用法：workbench_admin.mjs cancel-run <run_id>");
    process.exit(2);
  }
  try {
    const run = store.cancelRun(id);
    console.log(`已取消 run ${run.id}（${run.ticker}，原状态 running → cancelled）`);
  } catch (error) {
    console.error(`取消失败：${error.message}`);
    process.exit(1);
  }
} else if (action === "cancel-stale") {
  const olderThanMs = hoursArg();
  const cancelled = store.cancelStaleRuns(olderThanMs ? { olderThanMs } : {});
  console.log(cancelled.length ? `已取消 ${cancelled.length} 个超时 run：${cancelled.join(", ")}`
    : "没有需要取消的超时 run。");
} else if (action === "prune-runs") {
  const olderThanMs = hoursArg();
  const removed = store.pruneAbandonedRuns(olderThanMs ? { olderThanMs } : {});
  console.log(removed.length ? `已删除 ${removed.length} 个孤儿 run：${removed.join(", ")}`
    : "没有需要删除的孤儿 run。");
} else if (action === "seed-preview") {
  const ticker = String(arg("ticker") ?? "").trim().toUpperCase();
  if (!ticker) {
    console.error("用法：workbench_admin.mjs seed-preview --ticker <标的> [--strategy rsi|ma_cross] [--sentiment]");
    process.exit(2);
  }
  const strategy = String(arg("strategy") ?? "rsi");
  const python = path.join(store.home, "trading-venv", "bin", "python");
  const argv = [path.join(REPO_ROOT, "plugins/engine/python/engine.py"), "signal",
    "--ticker", ticker, "--strategy", strategy];
  if (rest.includes("--sentiment")) argv.push("--sentiment");
  // 与 plugins/engine/src/tools.js 的 runQuant 同一条命令、同一解析口径（取第一个 { 之后的 JSON）
  const stdout = execFileSync(python, argv, {
    encoding: "utf8", timeout: 240_000, maxBuffer: 8 * 1024 * 1024,
    env: { ...process.env, DSH_HOME: store.home },
  });
  const result = JSON.parse(stdout.slice(stdout.indexOf("{")));
  if (result.error) {
    console.error(`引擎报错：${result.error}`);
    process.exit(1);
  }
  const preview = store.recordPreview("signal", result, store.readMode());
  console.log(`已记录预览 ${preview.id}｜kind=${preview.kind}｜mode=${preview.mode}`
    + `｜${result.ticker} ${result.date} ${result.signal} price=${result.price} atr=${result.atr}`);
} else if (action === "seed-research") {
  const ticker = String(arg("ticker") ?? "").trim().toUpperCase();
  const sessionId = String(arg("session") ?? "workbench-seed");
  if (!ticker) {
    console.error("用法：workbench_admin.mjs seed-research --ticker <标的> [--rating Hold] "
      + "[--report-file <md>] [--sources-file <json>] [--session <id>]");
    process.exit(2);
  }
  const run = store.beginResearch({ ticker, session_id: sessionId });
  console.log(`已建 run ${run.id}｜ticker=${run.ticker}｜mode=${run.mode}｜${run.started_at}`);
  const reportFile = arg("report-file");
  const sourcesFile = arg("sources-file");
  if (!reportFile || !sourcesFile) {
    console.log("（未给 --report-file/--sources-file：只登记 run，未发布研报）");
    process.exit(0);
  }
  const published = store.publishResearch({
    run_id: run.id, ticker,
    rating: String(arg("rating") ?? "Hold"),
    report: readFileSync(reportFile, "utf8"),
    sources: JSON.parse(readFileSync(sourcesFile, "utf8")),
  }, sessionId);
  console.log(`已发布研报 ${published.id}｜rating=${published.rating}`
    + `（${published.rating_label}）｜来源 ${published.sources.length} 条｜${published.published_at}`);
} else if (action !== "status") {
  console.error(`未知动作：${action}（可用：status | runs | cancel-run | cancel-stale | prune-runs`
    + ` | seed-preview | seed-research）`);
  process.exit(2);
}
