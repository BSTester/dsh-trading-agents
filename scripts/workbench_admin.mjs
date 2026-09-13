#!/usr/bin/env node
// 工作台数据维护（Node 侧，因为 store 是 JS）。
//
// 典型场景：被中断的会话会留下停在 running 的研究 run，面板上一直显示"进行中"。
//   status                     查看当前数据量
//   runs                       列出全部 run 及其状态与年龄
//   cancel-run <id>            取消指定 run（标记 cancelled，保留记录）
//   cancel-stale [--hours N]   取消所有超时仍 running 的 run（默认 2 小时）
//   prune-runs [--hours N]     删除超时的孤儿 run（无研报者；有研报的保留）
import { WorkbenchStore } from "../plugins/workbench/src/store.js";

const [, , action = "status", ...rest] = process.argv;

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
} else if (action !== "status") {
  console.error(`未知动作：${action}（可用：status | runs | cancel-run | cancel-stale | prune-runs）`);
  process.exit(2);
}
