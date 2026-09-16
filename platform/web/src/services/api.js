// 数据层：POST /api/wb/<endpoint>；envelope 解包 + 客户端内存缓存（TTL 对齐旧 client.js）。
// 业务失败（ok:false）抛 Error(message) 由页面展示——不弹全局错误掩盖降级数据。
// 缺失端点自检：另维护 snapshot 声明的端点集合，未声明的端点直接拦下（不发请求）；
// 纯逻辑在 services/endpoints.js，本文件只做接线（fetch/localStorage 不进 node 测试）。
import { declaredEndpoints, endpointMissing } from "./endpoints.js";

const TTL_MS = {
  snapshot: 0, series: 300_000, equity: 300_000, positions: 300_000,
  correlation: 1_800_000, sensitivity: 3_600_000, risk: 900_000, trades: 300_000,
  events: 3_600_000, factors: 1_800_000, ic: 1_800_000, audit: 120_000,
  sources: 300_000, instrument: 600_000, quality: 3_600_000,
  plan: 60_000, schedule: 30_000, reconcile: 300_000,
  // WP10 任务 3：流程页对齐服务端 30 秒缓存（caches.py CACHE_TTL_MS["pipeline"]）；
  // auto_pipeline 是设置页读/写端点，与服务端同口径实时直通（不缓存）。
  pipeline: 30_000, auto_pipeline: 0,
  // WP7：因子快照历史对齐服务端 5 分钟缓存（caches.CACHE_TTL_MS["factors-history"]）
  "factors-history": 300_000,
  // WP11 任务 3：情绪快照历史/摘要同为按日采集，对齐服务端 5 分钟缓存
  // （caches.CACHE_TTL_MS["sentiment-history"]）。
  "sentiment-history": 300_000,
  // WP8 富途实时直通 + OpenAPI 交易只读 + 推送状态：服务端一律 TTL 0（实时直通，
  // futu_data.py 文件头与 store_access.WP8_*_ENDPOINTS 同口径），客户端同样不缓存。
  rt_quote: 0, rt_order_book: 0,
  capital_flow: 0, capital_flow_history: 0, capital_distribution: 0,
  option_expiration: 0, option_chain: 0, option_screen: 0,
  trade_max_qty: 0, orders_open: 0, orders_history: 0,
  deals_today: 0, deals_history: 0, push_status: 0,
  // WP8 任务 7 设置页：凭据读/写与连通性测试一律实时直通（服务端不走 cached()，
  // 这里 TTL 0 不写缓存；openapi_test_post 是同名防御别名，与 POST 语义对齐）。
  // WP8 OAuth：授权流程 start/status/cancel 同为实时直通（status 由设置页 2s 轮询）。
  openapi_config: 0, openapi_test: 0, openapi_test_post: 0, openapi_oauth: 0,
  // WP12 任务 6：富途数据面端点——TTL **逐项对齐服务端 caches.CACHE_TTL_MS**
  // （镜像而非自估；服务端是唯一事实源，漂移由 tests/test_wp12_locks.py 的锁定测试比对）。
  //   6h：板块列表、所属板块、复权因子（低频静态，服务端注释：按日/结构性数据）
  //   30m：经济日历两项、板块成分股、IPO 列表、F10 聚合面、衍生品聚合面
  //   5m：全市场筛选、自选列表/分组（随行情或用户操作变动）
  //   1h：做空数据（按日更新）
  //   modify_user_security 是写端点（服务端不进 TTL 表）→ 恒 0。
  economic_calendar_hot: 1_800_000, economic_calendar_search: 1_800_000,
  info_owner_plate: 21_600_000, info_rehab: 21_600_000, plate_list: 21_600_000,
  plate_stock: 1_800_000, stock_screen: 300_000, warrant_screen: 300_000,
  ipo_list: 1_800_000, short_daily_volume: 3_600_000, short_interest: 3_600_000,
  watchlist_list: 300_000, watchlist_groups: 300_000,
  f10_detail: 1_800_000, derivative_detail: 1_800_000,
  modify_user_security: 0,
};
const memory = new Map();
// snapshot 响应声明的端点集合；null = 尚未取到声明（不拦，交给 404 兜底）。
let declared = null;

export function getToken() {
  try { return localStorage.getItem("trading_token") ?? ""; } catch { return ""; }
}
export function setToken(value) {
  try {
    if (value) localStorage.setItem("trading_token", value);
    else localStorage.removeItem("trading_token");
  } catch { /* 隐私模式忽略 */ }
}
export function clearCache() { memory.clear(); }

export async function callApi(endpoint, payload = {}, { refresh = false } = {}) {
  const key = `${endpoint}|${JSON.stringify(payload)}`;
  const ttl = TTL_MS[endpoint] ?? 0;
  // 声明预检（snapshot 自身除外）：未声明即服务版本陈旧，不发请求
  if (endpoint !== "snapshot") {
    const missing = endpointMissing(endpoint, declared);
    if (missing) throw new Error(missing);
  }
  const hit = memory.get(key);
  if (!refresh && ttl > 0 && hit && Date.now() - hit.at < ttl) return hit.value;
  const headers = { "Content-Type": "application/json" };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let response;
  try {
    response = await fetch(`/api/wb/${endpoint}`, {
      method: "POST", headers,
      body: JSON.stringify(refresh ? { ...payload, _refresh: true } : payload),
    });
  } catch (error) {
    throw new Error(`无法连接工作台服务（127.0.0.1:8397）：${error.message}`);
  }
  if (response.status === 401) throw new Error("需要访问令牌：右上角「令牌」处填入服务配置的 token");
  if (response.status === 404) throw new Error(`服务未提供 ${endpoint}（服务版本陈旧，请重启服务后刷新）`);
  // 审查修复：响应体解析失败（非 JSON）给出可读错误，而不是抛解析器原始错误
  let body;
  try {
    body = await response.json();
  } catch (error) {
    throw new Error(`服务响应异常（HTTP ${response.status}）：${error.message}`);
  }
  if (!body.ok) throw new Error(body.error?.message || body.error?.code || "请求失败");
  // snapshot 是端点声明来源：写入模块级集合，后续调用据此预检
  if (endpoint === "snapshot") declared = declaredEndpoints(body.value);
  // 审查修复：TTL=0 的端点（snapshot）不写缓存——缓存写入必须带有效期
  if (ttl > 0) memory.set(key, { at: Date.now(), value: body.value });
  return body.value;
}
