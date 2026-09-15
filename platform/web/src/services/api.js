// 数据层：POST /api/wb/<endpoint>；envelope 解包 + 客户端内存缓存（TTL 对齐旧 client.js）。
// 业务失败（ok:false）抛 Error(message) 由页面展示——不弹全局错误掩盖降级数据。
const TTL_MS = {
  snapshot: 0, series: 300_000, equity: 300_000, positions: 300_000,
  correlation: 1_800_000, sensitivity: 3_600_000, risk: 900_000, trades: 300_000,
  events: 3_600_000, factors: 1_800_000, ic: 1_800_000, audit: 120_000,
  sources: 300_000, instrument: 600_000, quality: 3_600_000,
  plan: 60_000, schedule: 30_000, reconcile: 300_000,
};
const memory = new Map();

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
  } catch {
    throw new Error(`服务响应异常（HTTP ${response.status}）`);
  }
  if (!body.ok) throw new Error(body.error?.message || body.error?.code || "请求失败");
  // 审查修复：TTL=0 的端点（snapshot）不写缓存——缓存写入必须带有效期
  if (ttl > 0) memory.set(key, { at: Date.now(), value: body.value });
  return body.value;
}
