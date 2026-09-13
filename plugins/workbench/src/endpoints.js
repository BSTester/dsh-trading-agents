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
];
