// HTTP 面（规格 §4.6/§4.7）：POST /api/wb/<endpoint> + GET /healthz + 静态 dist + /mcp 委派。
// 零框架（node:http）；白名单外的一切都到不了 handler（规格 §5.1 A7/A8）。
// 认证边界：loopback 绑定 + 可选 Bearer token（service.token）；healthz 与静态文件豁免。
import { createServer } from "node:http";
import { createReadStream, existsSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { authorized, collectBody, sendJson, unauthorized } from "./util.mjs";

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
  ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png",
  ".ico": "image/x-icon", ".woff2": "font/woff2", ".map": "application/json",
};

function serveStatic(res, dist, urlPath, fallback = true) {
  const relative = decodeURIComponent(urlPath === "/" ? "/index.html" : urlPath);
  const target = path.normalize(path.join(dist, relative));
  if (target !== dist && !target.startsWith(dist + path.sep)) {
    return sendJson(res, 403, { ok: false, error: { code: "trading/forbidden", message: "路径非法", details: {} } });
  }
  if (!existsSync(target) || statSync(target).isDirectory()) {
    if (!fallback) {
      return sendJson(res, 404, { ok: false, error: { code: "trading/not-found", message: "前端未构建（npm --prefix platform/web run build）", details: {} } });
    }
    return serveStatic(res, dist, "/index.html", false);  // SPA 路由兜底
  }
  res.writeHead(200, { "Content-Type": MIME[path.extname(target)] ?? "application/octet-stream" });
  createReadStream(target).pipe(res);
}

export function createService({ store, handle, mcp, config, dist, endpoints }) {
  const root = dist ?? path.join(path.dirname(fileURLToPath(import.meta.url)), "..", "web", "dist");
  return createServer(async (req, res) => {
    const route = new URL(req.url ?? "/", "http://localhost").pathname;
    try {
      if (route === "/healthz") {
        return sendJson(res, 200, { ok: true, mode: store.readMode() });
      }
      if (route === "/mcp") {
        if (!authorized(req, config.token)) return unauthorized(res);
        return await mcp(req, res);
      }
      if (route.startsWith("/api/wb/")) {
        if (!authorized(req, config.token)) return unauthorized(res);
        if (req.method !== "POST") {
          return sendJson(res, 405, { ok: false, error: { code: "trading/method-not-allowed", message: "仅 POST", details: {} } });
        }
        const endpoint = route.slice("/api/wb/".length);
        if (!/^[a-z-]+$/.test(endpoint) || !endpoints.includes(endpoint)) {
          // 白名单外一律 404，不区分「格式错」与「未声明」（规格 §5.1 A7 封闭性）
          return sendJson(res, 404, { ok: false, error: { code: "trading/unknown-endpoint", message: `未知端点 ${endpoint}`, details: {} } });
        }
        const contentType = String(req.headers["content-type"] ?? "").split(";")[0].trim();
        if (contentType !== "application/json") {
          return sendJson(res, 415, { ok: false, error: { code: "trading/invalid-operation", message: "Expected application/json", details: {} } });
        }
        let payload;
        try {
          payload = JSON.parse((await collectBody(req)) || "{}");
        } catch (error) {
          return sendJson(res, 400, { ok: false, error: { code: "trading/invalid-operation", message: `请求体不是合法 JSON：${error.message}`, details: {} } });
        }
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
          return sendJson(res, 400, { ok: false, error: { code: "trading/invalid-operation", message: "Expected an object payload", details: {} } });
        }
        return sendJson(res, 200, await handle(endpoint, payload));
      }
      if (req.method === "GET") return serveStatic(res, root, route);
      return sendJson(res, 405, { ok: false, error: { code: "trading/method-not-allowed", message: "仅 GET", details: {} } });
    } catch (error) {
      if (res.headersSent) { res.end(); return; }
      return sendJson(res, 500, { ok: false, error: { code: "trading/internal", message: String(error?.message ?? error).slice(0, 300), details: {} } });
    }
  });
}
