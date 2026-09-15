// MCP streamable-http 端点（规格 §3.6）：每个会话一对 server+transport（SDK 官方会话模式），
// JSON 响应（enableJsonResponse，无 SSE 依赖）。工具面=buildManifest 产物，
// 与 HTTP 面共用同一 handle——两条通道对同一 payload 结果一致（规格 §5.2 R6/S3）。
// SDK 1.30 适配：handleRequest 的 parsedBody 需为已解析的 JSON-RPC 消息对象（源码直接
// rawMessage=options.parsedBody 进 JSONRPCMessageSchema.parse，不接受字符串），
// 故先 collectBody（保留 1MB 上限）再 JSON.parse；非法 JSON 按 SDK 同款线格式回 400 -32700。
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { collectBody, sendJson } from "./util.mjs";

export function createMcpEndpoint({ manifest }) {
  const sessions = new Map();

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
    const known = sessionId ? sessions.get(sessionId) : undefined;
    try {
      if (req.method === "DELETE") {
        if (known) {
          sessions.delete(sessionId);
          await known.transport.close().catch(() => {});
        }
        res.writeHead(204);
        res.end();
        return;
      }
      if (req.method !== "POST") {
        res.writeHead(405, { Allow: "POST, DELETE" });
        res.end();
        return;
      }
      let body;
      try {
        body = JSON.parse((await collectBody(req)) || "null");
      } catch (error) {
        if (error?.code === "payload-too-large") {
          // 与 /api/wb 同一超限分类（规格 §5.1）：MCP 通道不因换协议放宽体积上限
          return sendJson(res, 413, { ok: false, error: { code: "trading/payload-too-large",
            message: "请求体超过 1MB 上限", details: {} } });
        }
        // 非法 JSON：按 SDK 同款线格式（createJsonErrorResponse）回 400 -32700
        return sendJson(res, 400, { jsonrpc: "2.0", error: { code: -32700, message: "Parse error: Invalid JSON" }, id: null });
      }
      let entry = known;
      if (!entry) {
        const server = buildServer();
        const transport = new StreamableHTTPServerTransport({
          sessionIdGenerator: () => randomUUID(),
          enableJsonResponse: true,
          onsessioninitialized: (id) => sessions.set(id, { server, transport }),
        });
        transport.onclose = () => {
          for (const [id, item] of sessions) if (item.transport === transport) sessions.delete(id);
        };
        await server.connect(transport);
        entry = { server, transport };
      }
      await entry.transport.handleRequest(req, res, body);
    } catch (error) {
      if (!res.headersSent) {
        sendJson(res, 500, { ok: false, error: { code: "trading/internal", message: String(error?.message ?? error).slice(0, 300), details: {} } });
      } else {
        res.end();
      }
    }
  };
}
