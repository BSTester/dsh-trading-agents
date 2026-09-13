#!/usr/bin/env node
// 工作台数据维护（Node 侧，因为 store 是 JS）。
//
// 用途：清理被中断的会话留下的孤儿 run —— 它们永远停在 running，
// 展示层虽已派生为 abandoned，但数据仍占着 LIMIT 名额。
//
// 用法：
//   node scripts/workbench_admin.mjs status
//   node scripts/workbench_admin.mjs prune-runs [--hours 2]
import { WorkbenchStore } from "../plugins/workbench/src/store.js";

const [, , action = "status", ...rest] = process.argv;

function hoursArg() {
  const index = rest.indexOf("--hours");
  if (index === -1) return undefined;
  const value = Number(rest[index + 1]);
  return Number.isFinite(value) && value > 0 ? value * 3600_000 : undefined;
}

const store = new WorkbenchStore();
const state = store.read();
console.log(`数据文件：${store.file ?? "(默认)"}`);
console.log(`runs=${state.runs.length} reports=${state.reports.length} `
  + `previews=${state.previews.length} activity=${state.activity.length}`);

if (action === "prune-runs") {
  const olderThanMs = hoursArg();
  const removed = store.pruneAbandonedRuns(olderThanMs ? { olderThanMs } : {});
  console.log(removed.length ? `已清理 ${removed.length} 个孤儿 run：${removed.join(", ")}`
    : "没有需要清理的孤儿 run。");
} else if (action !== "status") {
  console.error(`未知动作：${action}（可用：status | prune-runs）`);
  process.exit(2);
}
