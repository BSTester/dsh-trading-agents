// MCP streamable-http 端点（规格 §3.6）：每个会话一对 server+transport（SDK 官方会话模式），
// JSON 响应（enableJsonResponse，无 SSE 依赖）。工具面=buildManifest 产物，
// 与 HTTP 面共用同一 handle——两条通道对同一 payload 结果一致（规格 §5.2 R6/S3）。
// SDK 1.30 适配：handleRequest 的 parsedBody 需为已解析的 JSON-RPC 消息对象（源码直接
// rawMessage=options.parsedBody 进 JSONRPCMessageSchema.parse，不接受字符串），
// 故先 collectBody（保留 1MB 上限）再 JSON.parse；非法 JSON 按 SDK 同款线格式回 400 -32700。
// 会话生命周期：陈旧/缺失 sid 的普通请求不构建会话（SDK 原生路径是 build+connect 完再拒，
// 白耗一套 server+transport）；回收走惰性清扫 + DELETE/onclose 双路径，无需 timer。
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { collectBody, sendJson } from "./util.mjs";

const SESSION_TTL_MS = 30 * 60_000;    // 会话闲置上限；agent 每轮对话新建会话，超时即回收
const SESSION_MAX = 64;                 // 硬上限，防异常客户端撑表

export function createMcpEndpoint({ manifest }) {
  const sessions = new Map();

  // 惰性清扫：过期（TTL）先删，再按 lastSeen 最旧淘汰到硬上限内。
  // 无需 timer：mcpRequest 每次进入先扫一遍；DELETE 与 transport.onclose 双路径兜底
  // （onclose 里的扫描删除是幂等的，重复 delete/close 无副作用）。
  function sweepSessions() {
    const now = Date.now();
    for (const [id, item] of sessions) {
      if (now - item.lastSeen > SESSION_TTL_MS) {
        sessions.delete(id);
        item.transport.close().catch(() => {});
      }
    }
    if (sessions.size > SESSION_MAX) {
      const oldest = [...sessions.entries()].sort((a, b) => a[1].lastSeen - b[1].lastSeen);
      for (const [id, item] of oldest.slice(0, sessions.size - SESSION_MAX)) {
        sessions.delete(id);
        item.transport.close().catch(() => {});
      }
    }
  }

  function buildServer() {
    const server = new McpServer({ name: "quantwb", version: "0.1.0" });
    for (const tool of manifest) {
      server.registerTool(tool.name, { description: tool.description, inputSchema: tool.input },
        async (args) => {
          try {
            const result = await tool.call(args ?? {});
            // 显式 isError:false：CallToolResult 的该字段 optional，缺席时客户端读到 undefined
            return { isError: false, content: [{ type: "text", text: JSON.stringify(result) }] };
          } catch (error) {
            // handler 之外的程序异常才标 isError；业务失败走 ok:false envelope（规格 §3.2 错误语义）
            return { isError: true, content: [{ type: "text", text: JSON.stringify({ ok: false,
              error: { code: "trading/tool-failed", message: String(error?.message ?? error).slice(0, 300), details: {} } }) }] };
          }
        });
    }
    return server;
  }

  return async function mcpRequest(req, res) {
    const sessionId = req.headers["mcp-session-id"];
    try {
      sweepSessions();
      const known = sessionId ? sessions.get(sessionId) : undefined;
      if (req.method === "DELETE") {
        if (known) {
          sessions.delete(sessionId);
          await known.transport.close().catch(() => {});
        }
        // 幂等宽容：重复 DELETE 视为已注销（SDK 原生为 404/-32001，此处选择宽容）
        res.writeHead(204);
        res.end();
        return;
      }
      if (req.method !== "POST") {
        res.writeHead(405, { Allow: "POST, DELETE" });
        res.end();
        return;
      }
      let parsed;
      try {
        parsed = JSON.parse((await collectBody(req)) || "null");
      } catch (error) {
        if (error?.code === "payload-too-large") {
          // 与 /api/wb 同一超限分类（规格 §5.1）：MCP 通道不因换协议放宽体积上限
          return sendJson(res, 413, { ok: false, error: { code: "trading/payload-too-large",
            message: "请求体超过 1MB 上限", details: {} } });
        }
        // 非法 JSON：按 SDK 同款线格式（createJsonErrorResponse）回 400 -32700，不建会话
        return sendJson(res, 400, { jsonrpc: "2.0", error: { code: -32700, message: "Parse error: Invalid JSON" }, id: null });
      }
      if (!known && parsed?.method !== "initialize") {
        // 陈旧/缺失会话的普通请求：不构建会话，按 SDK 线格式拒绝
        if (sessionId) {
          sendJson(res, 404, { jsonrpc: "2.0", id: parsed?.id ?? null,
            error: { code: -32001, message: "Session not found" } });
        } else {
          sendJson(res, 400, { jsonrpc: "2.0", id: parsed?.id ?? null,
            error: { code: -32000, message: "Server not initialized" } });
        }
        return;
      }
      let entry = known;
      if (!entry) {
        const server = buildServer();
        const transport = new StreamableHTTPServerTransport({
          sessionIdGenerator: () => randomUUID(),
          enableJsonResponse: true,
          onsessioninitialized: (id) => sessions.set(id, { server, transport, lastSeen: Date.now() }),
        });
        transport.onclose = () => {
          for (const [id, item] of sessions) if (item.transport === transport) sessions.delete(id);
        };
        await server.connect(transport);
        entry = { server, transport, lastSeen: Date.now() };
      }
      entry.lastSeen = Date.now();   // 每次请求 touch：惰性清扫的活跃度依据
      await entry.transport.handleRequest(req, res, parsed);
    } catch (error) {
      if (!res.headersSent) {
        // 意外崩溃：按 JSON-RPC 线格式回 -32603（通道协议一致，不混业务 envelope）
        sendJson(res, 500, { jsonrpc: "2.0", id: null,
          error: { code: -32603, message: String(error?.message ?? error).slice(0, 300) } });
      } else {
        res.end();
      }
    }
  };
}
