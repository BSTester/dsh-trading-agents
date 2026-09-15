// 工作台 Host 提供的接口清单（单一事实来源）。
//
// 用途有三：
//   1. index.js 据此注册路由；
//   2. snapshot 响应里向客户端**声明**当前 Host 实际提供哪些接口；
//   3. 客户端据此避免调用不存在的接口（否则每个都撞 404），并给出可读原因。
//
// 为什么需要声明：Client 半边是每次请求从磁盘读取的，而 Host 半边只在进程启动时
// 加载一次。插件升级后若没重启，客户端会调用一批 Host 根本没有的路由，表现为
// 满屏 `transport failure ... HTTP 404`，完全看不出是进程陈旧。声明之后可以在
// 面板上直接说明白。
export const ENDPOINTS = [
  "snapshot",
  "switch-mode",
  "series",
  "equity",
  "positions",
  "correlation",
  "sensitivity",
  "risk",
  "trades",
  "events",
  "factors",
  "ic",
  "audit",
  "sources",
  "instrument",
  "quality",
  "plan",
  "plan-execute",
  "schedule",
  "reconcile",
  // 实盘业务确认：读待确认项 / 提交用户的决定。两者都不进缓存——
  // 缓存住"待确认"会让界面显示一个已经处理掉的请求。
  "confirmation",
  "confirm-decide",
];

/**
 * 每个接口结果的最小字段。用途：缓存读写两侧都校验形状。
 *
 * 为什么必须校验：缓存目录由 Host 进程、CLI 与测试共用。一旦有空对象/截断结果
 * 混进缓存，TTL 内界面会把「取数失败」显示成「账户没有持仓」——用户会以为
 * 仓位真的清空了。宁可当作未命中重新取数，也不要展示一个假事实。
 */
export const ENDPOINT_SHAPE = {
  series: ["ticker", "bars"],
  equity: ["mode", "points"],
  positions: ["mode", "groups"],
  correlation: ["matrix"],
  sensitivity: ["ticker", "matrix"],
  risk: ["config"],
  trades: ["trades"],
  events: ["ticker", "events"],
  factors: ["tickers"],
  ic: ["points"],
  sources: ["sources"],
  instrument: ["ticker"],
  quality: ["ticker"],
  // WP4：plan-execute 是动作端点（排队即返回），不进缓存形状表
  plan: ["plans", "alerts"],
  confirmation: ["pending"],
  schedule: ["heartbeat", "jobs"],
  reconcile: ["diffs", "tca"],
};

/** 载荷是否具备该接口的最小字段；未知接口一律放行。 */
export function matchesShape(endpoint, value) {
  const required = ENDPOINT_SHAPE[endpoint];
  if (!required) return true;
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  return required.every((field) => field in value);
}
