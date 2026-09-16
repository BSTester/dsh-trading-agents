// tradingWorkbench 服务锚：engine 的 8 个对话工具与 policy 的模式互斥/租约/观察记录
// 都依赖这个进程内服务。WP7 面板退役（用户决策 2026-09-16）后，本插件不再注册任何
// Connection RPC——工作台 UI 由独立服务（platform/，FastAPI 单进程）承接。
import { WorkbenchStore } from "./store.js";

export const name = "trading-workbench";
export const inject = [];

export function apply(ctx) {
  ctx.provide("tradingWorkbench", new WorkbenchStore());
}
