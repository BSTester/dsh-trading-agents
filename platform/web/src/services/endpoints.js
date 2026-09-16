// 声明端点预检（对齐既有「缺失端点自检」口径：HTTP 404 + snapshot.endpoints 声明比对）。
// 纯函数、无浏览器依赖：snapshot 响应里声明的端点集合与本次要调的端点比对；
// 未声明即说明服务版本陈旧，页面直接给出可读提示且不发起请求（404 分支只作兜底）。
// 集合来源：服务端 app.py snapshot 分支写入 store_access.endpoints()（WP7 起服务自有清单）。

/**
 * 取出 snapshot 响应声明的端点数组；非数组（含缺失、旧服务未带该字段）返回 null，
 * 表示「无法判断」——此时 endpointMissing 一律放行，避免旧服务被前端拦死。
 * @param {unknown} snapshot
 * @returns {string[]|null}
 */
export function declaredEndpoints(snapshot) {
  const list = snapshot?.endpoints;
  return Array.isArray(list) ? list : null;
}

/**
 * 声明集合里没有该端点时返回提示文案；declared 为 null（未声明集合）返回 null 不拦。
 * @param {string} endpoint
 * @param {string[]|null|undefined} declared
 * @returns {string|null}
 */
export function endpointMissing(endpoint, declared) {
  if (!Array.isArray(declared)) return null;
  if (declared.includes(endpoint)) return null;
  return `服务未提供 ${endpoint}（服务版本陈旧，请重启服务后刷新）`;
}
