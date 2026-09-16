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
  // WP7：因子快照历史对齐服务端 5 分钟缓存（caches.CACHE_TTL_MS["factors-history"]）
  "factors-history": 300_000,
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
