// 请求体收集与 JSON 响应的小工具（service/mcp 共用；上限 1MB）。
export function collectBody(req, limit = 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        const error = new Error("payload too large");
        error.code = "payload-too-large";   // util 不拆 socket；响应权在 service
        reject(error);
        req.removeAllListeners("data");     // 停止接收，但不断连
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

export function sendJson(res, status, value) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify(value));
}

export function unauthorized(res) {
  sendJson(res, 401, { ok: false, error: { code: "trading/unauthorized", message: "需要 Bearer token", details: {} } });
}

export function authorized(req, token) {
  if (!token) return true;
  return req.headers.authorization === `Bearer ${token}`;
}
