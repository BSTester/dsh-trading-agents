// 平台 MCP 服务器（stdio，NDJSON 帧，零依赖）——FR-GATEWAY-001 / FR-TOOLS-003。
// 暴露：一级工具（六域精选）+ list_tools / call_tool 发现代理（覆盖既有 workbench 77 工具）。
// 交易写类不设一级工具；call_tool 直通时其确认闸门仍在 workbench 侧（Web 确认卡 / TTL）。
import { buildCatalog, FIRST_CLASS, WRITE_TOOLS } from './catalog.mjs'

const PROTOCOL_VERSION = '2025-06-18'
const SERVER_INFO = { name: 'quant-platform-v3', version: '3.0.0-alpha.1' }

function schema(properties, required = []) {
  return { type: 'object', properties, required, additionalProperties: false }
}

const FIRST_CLASS_SCHEMAS = {
  query_quote: schema({ ticker: { type: 'string', description: '标的代码，如 SH.600519' }, period: { type: 'string', description: 'K 线周期，默认 5m' }, limit: { type: 'number', description: '根数 20..2000' } }, ['ticker']),
  market_snapshot: schema({ stocks: { type: 'array', items: { type: 'string' }, description: '标的列表' } }, ['stocks']),
  query_order_book: schema({ ticker: { type: 'string' } }, ['ticker']),
  query_capital_flow: schema({ ticker: { type: 'string' } }, ['ticker']),
  query_financial: schema({ ticker: { type: 'string' } }, ['ticker']),
  stock_screen: schema({ con: { type: 'string', description: '筛选条件，见 workbench 契约' } }),
  eval_factor_ic: schema({ factor: { type: 'string' }, universe: { type: 'string' } }, ['factor']),
  list_factors: schema({}),
  factor_sensitivity: schema({ factor: { type: 'string' } }, ['factor']),
  sentiment_history: schema({ ticker: { type: 'string' }, window: { type: 'number' } }),
  calc_indicator: schema({ factor: { type: 'string' } }, ['factor']),
  check_risk: schema({ mode: { type: 'string' } }),
  query_position: schema({ mode: { type: 'string' } }),
  account_funds: schema({ mode: { type: 'string' } }),
  orders_open: schema({}),
  deals_today: schema({}),
  request_approval: schema({ id: { type: 'string' } }),
  audit_trail: schema({ window: { type: 'number' } }),
  research_tasks_claim: schema({}),
}

const META_TOOLS = [
  {
    name: 'list_tools',
    description: '工具发现代理：按域列出平台全部可用工具（六域：data/alpha/ml/risk/execution/ecosystem，含 workbench 直通工具）。避免上百个 schema 撑爆上下文。',
    inputSchema: schema({ domain: { type: 'string', enum: ['data', 'alpha', 'ml', 'risk', 'execution', 'ecosystem'], description: '只看某个域；缺省返回全部' } }),
  },
  {
    name: 'call_tool',
    description: '工具发现代理：按名称调用任一平台工具（含 workbench 直通）。交易写类（trade_place/trade_modify/trade_cancel）的确认闸门在 workbench 侧（Web 确认卡），本层不做二次拦截也不做绕过。',
    inputSchema: schema({ name: { type: 'string', description: '工具名（list_tools 里的 name 或 workbench 原名）' }, args: { type: 'object', description: '工具参数，透传' } }, ['name']),
  },
]

export function createMcpServer({ wbCall, wbToolNames, log }) {
  const catalog = buildCatalog(wbToolNames)
  const firstClassByName = new Map(FIRST_CLASS.map((t) => [t.name, t]))

  function toolList() {
    const tools = META_TOOLS.map((t) => ({ name: t.name, description: t.description, inputSchema: t.inputSchema }))
    for (const tool of FIRST_CLASS) {
      tools.push({ name: tool.name, description: `[${tool.domain}] ${tool.desc}（→ workbench ${tool.wb}）`, inputSchema: FIRST_CLASS_SCHEMAS[tool.name] ?? schema({}) })
    }
    return tools
  }

  async function callFirstClass(tool, args) {
    return wbAsOutcome(wbCall(tool.wb, args))
  }

  // workbench envelope { ok, value | error } → MCP outcome { text, isError }。
  async function wbAsOutcome(promise) {
    const envelope = await promise
    if (envelope?.ok) return { text: JSON.stringify(envelope.value, null, 1) }
    return { text: JSON.stringify(envelope?.error ?? { code: 'wb/error', message: 'unknown' }, null, 1), isError: true }
  }

  async function handleToolCall(name, args) {
    if (name === 'list_tools') {
      const domain = args?.domain
      const entries = domain ? { [domain]: catalog[domain] ?? [] } : catalog
      const total = Object.values(entries).reduce((sum, list) => sum + list.length, 0)
      return { text: JSON.stringify({ total, domains: entries }, null, 1) }
    }
    if (name === 'call_tool') {
      const target = String(args?.name || '')
      const inner = args?.args ?? {}
      if (firstClassByName.has(target)) return callFirstClass(firstClassByName.get(target), inner)
      if (!wbToolNames.has(target)) {
        return { text: `unknown tool: ${target}（见 list_tools）`, isError: true }
      }
      if (WRITE_TOOLS.has(target)) {
        log?.(`call_tool → ${target}（交易写类：确认闸门在 workbench Web 侧）`)
      }
      return wbAsOutcome(wbCall(target, inner))
    }
    const first = firstClassByName.get(name)
    if (first) return callFirstClass(first, args)
    return { text: `unknown tool: ${name}`, isError: true }
  }

  // 返回 JSON-RPC 响应对象；notification 返回 null。
  async function handleMessage(message) {
    const { id, method, params } = message ?? {}
    if (method === 'initialize') {
      return { jsonrpc: '2.0', id, result: { protocolVersion: PROTOCOL_VERSION, capabilities: { tools: { listChanged: false } }, serverInfo: SERVER_INFO } }
    }
    if (method === 'ping') return { jsonrpc: '2.0', id, result: {} }
    if (method === 'tools/list') return { jsonrpc: '2.0', id, result: { tools: toolList() } }
    if (method === 'tools/call') {
      const name = String(params?.name || '')
      try {
        const outcome = await handleToolCall(name, params?.arguments ?? {})
        return { jsonrpc: '2.0', id, result: { content: [{ type: 'text', text: outcome.text }], isError: outcome.isError === true } }
      } catch (error) {
        return { jsonrpc: '2.0', id, result: { content: [{ type: 'text', text: `tool error: ${error?.message ?? String(error)}` }], isError: true } }
      }
    }
    if (id === undefined) return null // notification（initialized 等）
    return { jsonrpc: '2.0', id, error: { code: -32601, message: `method not found: ${method}` } }
  }

  // 逐行驱动一个 stdio 会话。返回 stop 函数。
  function serveStdio(input = process.stdin, output = process.stdout) {
    let buffer = ''
    let closed = false
    const onData = async (chunk) => {
      buffer += chunk.toString('utf8')
      let index
      while ((index = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, index)
        buffer = buffer.slice(index + 1)
        if (line.trim() === '') continue
        let message
        try {
          message = JSON.parse(line)
        } catch {
          continue
        }
        const response = await handleMessage(message)
        if (response !== null) output.write(JSON.stringify(response) + '\n')
      }
    }
    const onClose = () => {
      closed = true // 只停止读取；在途调用的响应仍要写回（管道 EOF 常早于异步 wb 调用完成）
    }
    input.setEncoding('utf8')
    input.on('data', onData)
    input.on('close', onClose)
    return function stop() {
      closed = true
      input.removeListener('data', onData)
      input.removeListener('close', onClose)
    }
  }

  return { handleMessage, serveStdio, toolList, catalog }
}

export default createMcpServer
