// V3.0 平台服务入口：HTTP(API + 静态 UI) + MCP(stdio 由 npm run start:mcp 单独拉起；
// HTTP JSON-RPC 单消息端点 /api/v3/mcp 同样可用) + Headless 通道 + 调度器。
// 数据来源：既有 workbench(8397) 77 工具面；本服务不直连券商、不绕过交易确认边界。
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { createApp } from './http.mjs'
import loadConfig from './config.mjs'
import createWorkbenchClient from './wb-client.mjs'
import createMcpServer from './mcp/stdio.mjs'
import createStore from './store.mjs'
import { createHeadlessRunner, createScheduler } from './gateway/headless.mjs'
import createSdkChannel from './gateway/sdk.mjs'
import { checkOrder } from './risk.mjs'
import createOmsLedger from './oms.mjs'
import { backtestMomentum, paramSweep } from './strategy/backtest.mjs'
import createStrategyService from './strategy/pipeline.mjs'
import { createMetrics, createAudit } from './observability.mjs'

const config = loadConfig()
const metrics = createMetrics()
const audit = createAudit(config.dataDir)
const wb = createWorkbenchClient({ ...config.workbench, onCall: ({ tool, ok, ms }) => metrics.wbCall(tool, ok, ms) })
const store = createStore(config.dataDir)
const runner = createHeadlessRunner({ config, store, onCall: (record) => metrics.headless(record.success, record.duration_ms, record.tokens_estimate) })
const oms = createOmsLedger({ store, wbCall: (tool, args, options) => wb.call(tool, args, options) })
const sdkChannel = createSdkChannel({ config, clock: () => new Date() })
const strategy = createStrategyService({ wbCall: (tool, args, options) => wb.call(tool, args, options), watchlist: config.sources.watchlist, store })

const strategyLocalTools = [
  {
    name: 'run_backtest',
    description: '[ml] 单标的动量 long/flat 回测。真实富途日 K；PIT 对齐：t 日持仓仅由 ≤t-1 收盘价决定，无前视。轻量引擎（等权、无滑点建模），与 workbench 因子回测互补。',
    inputSchema: { type: 'object', properties: { ticker: { type: 'string', description: '标的，如 SH.600519' }, window: { type: 'number', description: '动量窗口，默认 20' }, rebalanceDays: { type: 'number', description: '调仓周期（天），默认 5' }, limit: { type: 'number', description: 'K 线根数，默认 500' } }, required: ['ticker'], additionalProperties: false },
    async execute(args) {
      const envelope = await wb.call('series', { ticker: String(args.ticker), period: '1d', limit: Number(args.limit || 500) })
      if (!envelope?.ok) return { text: JSON.stringify(envelope?.error ?? {}, null, 1), isError: true }
      const result = backtestMomentum(envelope.value.bars, { window: Number(args.window || 20), rebalanceDays: Number(args.rebalanceDays || 5) })
      if (result.error) return { text: result.error, isError: true }
      const equity = result.equity
      result.equity = [equity[0], equity[Math.floor(equity.length / 2)], equity.at(-1)]
      return { text: JSON.stringify(result.metrics, null, 1) }
    },
  },
  {
    name: 'param_sweep',
    description: '[ml] 参数扫描：动量窗口 × 调仓周期网格，返回各格 sharpe/年化/最大回撤与最优格（热力图数据）。',
    inputSchema: { type: 'object', properties: { ticker: { type: 'string' }, windows: { type: 'array', items: { type: 'number' }, description: '默认 [10,20,30,60]' }, rebalanceDays: { type: 'array', items: { type: 'number' }, description: '默认 [5,10,20]' }, limit: { type: 'number' } }, required: ['ticker'], additionalProperties: false },
    async execute(args) {
      const envelope = await wb.call('series', { ticker: String(args.ticker), period: '1d', limit: Number(args.limit || 500) })
      if (!envelope?.ok) return { text: JSON.stringify(envelope?.error ?? {}, null, 1), isError: true }
      const result = paramSweep(envelope.value.bars, { windows: args.windows ?? [10, 20, 30, 60], rebalanceDays: args.rebalanceDays ?? [5, 10, 20] })
      return { text: JSON.stringify(result, null, 1) }
    },
  },
  {
    name: 'strategy_run',
    description: '[ecosystem] 运行 PDAT→PAAT→PCPT→PRT→PET 研究流水线，产出调仓建议提案（不下单；执行走工作台受约束入口）。',
    inputSchema: { type: 'object', properties: { universe: { type: 'array', items: { type: 'string' }, description: '缺省用自选池前 8 只' }, topN: { type: 'number', description: '多头候选数，默认 2' }, window: { type: 'number', description: '动量窗口，默认 20' } }, additionalProperties: false },
    async execute(args) {
      const run = await strategy.run({ universe: args.universe, topN: Number(args.topN || 2), window: Number(args.window || 20) })
      return { text: JSON.stringify(run, null, 1) }
    },
  },
]

const WB_TOOL_NAMES = new Set([
  'snapshot', 'switch_mode', 'series', 'equity', 'positions', 'correlation', 'sensitivity', 'risk', 'trades', 'events',
  'factors', 'ic', 'audit', 'sources', 'instrument', 'quality', 'plan', 'confirmation', 'rules', 'plan_execute',
  'schedule', 'reconcile', 'pipeline', 'factors_history', 'sentiment_history', 'trade_place', 'trade_modify',
  'trade_cancel', 'account_positions', 'account_orders', 'account_funds', 'trade_max_qty', 'orders_open',
  'orders_history', 'orders_detail', 'deals_today', 'deals_history', 'rt_quote', 'rt_order_book', 'capital_flow',
  'capital_flow_history', 'capital_distribution', 'option_expiration', 'option_chain', 'option_screen',
  'market_snapshot', 'cur_kline', 'rt_data', 'rt_ticker', 'info_basicinfo', 'info_trading_days', 'info_search',
  'info_market_state', 'quote_history_kline_v2', 'push_status', 'push_subscribe', 'push_unsubscribe', 'stock_screen',
  'plate_list', 'plate_stock', 'short_daily_volume', 'short_interest', 'ipo_list', 'economic_calendar_hot',
  'economic_calendar_search', 'info_owner_plate', 'watchlist_list', 'watchlist_groups', 'f10_detail',
  'derivative_detail', 'research_tasks_claim', 'research_tasks_report', 'admin_status', 'admin_runs',
  'admin_cancel_run', 'admin_cancel_stale', 'admin_prune_runs',
])

const mcp = createMcpServer({
  wbCall: (tool, args, options) => wb.call(tool, args, options),
  wbToolNames: WB_TOOL_NAMES,
  localTools: strategyLocalTools,
  log: (message) => console.log(`[mcp] ${message}`),
  onToolCall: ({ name, ok, ms }) => {
    metrics.tool(name, ok, ms)
    // 工具面审计（含 trade_* 直通调用）：谁在何时调了什么、成没成
    audit.append({ kind: 'mcp-tool', action: name, status: ok ? 200 : 500, ms })
  },
})

const scheduler = createScheduler({
  rules: [
    { id: 'pre_market_scan', at: '08:30', task: 'pre_market_scan' },
    { id: 'midday_review', at: '12:00', task: 'risk_review' },
    { id: 'close_analysis', at: '16:00', task: 'pre_market_scan' },
  ],
  runner,
  wbCall: (tool, args, options) => wb.call(tool, args, options),
})

const startedAt = Date.now()
const app = createApp({
  onRequest: ({ method, pathname, status, durationMs, remote }) => {
    // 路径中的 id 归一，避免指标基数爆炸
    metrics.http(pathname.replace(/\/[0-9a-f]{8,}/gi, '/<id>'), method, status, durationMs)
    if (method === 'POST' && pathname !== '/api/v3/mcp') {
      audit.append({ kind: 'api', action: pathname, status, ms: durationMs, remote })
    }
  },
})

function wbValue(promise) {
  return promise.then((envelope) => {
    if (envelope?.ok) return { ok: true, data: envelope.value }
    return { ok: false, error: envelope?.error ?? { code: 'wb/error', message: 'unknown' } }
  })
}

app.get('/healthz', async () => {
  const workbench = await wb.health()
  return {
    ok: true,
    service: 'quant-platform-v3',
    version: '3.0.0-alpha.1',
    uptime_s: Math.round((Date.now() - startedAt) / 1000),
    workbench,
    channels: { mcp: 'running', headless: 'ready', sdk: sdkChannel.status().status },
  }
})

app.get('/api/v3/overview', async () => {
  const [snapshot, equity, headless] = await Promise.all([
    wbValue(wb.call('snapshot', {})),
    wbValue(wb.call('equity', { window: 60 })),
    Promise.resolve(runner.stats()),
  ])
  return {
    ok: true,
    snapshot: snapshot.ok ? snapshot.data : null,
    equity: equity.ok ? equity.data : null,
    headless,
    channels: {
      mcp: { status: 'running', transport: config.channels.mcp.transport },
      sdk: { status: sdkChannel.status().status, note: sdkChannel.status().reason ?? config.channels.sdk.note },
      headless: { status: 'ready', breaker: headless.breaker },
    },
    limits: { singlePct: 2, industryPct: 20, drawdownPct: 15 },
  }
})

app.get('/api/v3/brain', async () => ({ headless: runner.stats(), sdk: sdkChannel.status() }))

app.get('/api/v3/sdk', async () => ({ ok: true, ...sdkChannel.status() }))

app.post('/api/v3/sdk/start', async () => sdkChannel.start())

app.post('/api/v3/sdk/prompt', async ({ body }) => {
  const sessionId = String(body.sessionId || `quant-${Date.now()}`)
  const text = String(body.text || '').trim()
  if (text === '') return { ok: false, error: { code: 'sdk/empty-prompt', message: 'text 不能为空' } }
  return sdkChannel.prompt(sessionId, text)
})

app.get('/api/v3/market', async ({ query }) => {
  const ticker = query.ticker || (config.sources.watchlist[0] ?? 'SH.600519')
  return wbValue(wb.call('series', { ticker, period: query.period || '1d', limit: Number(query.limit || 120) }))
})

app.get('/api/v3/strategy', async () => {
  const last = strategy.last()
  if (last) return { ok: true, run: last }
  return { ok: true, run: null, note: '尚未运行研究流水线：点「运行流水线 PDAT→PET」或 POST /api/v3/strategy/run' }
})

app.post('/api/v3/strategy/run', async ({ body }) => {
  const run = await strategy.run({ universe: body.universe, topN: Number(body.topN || 2), window: Number(body.window || 20) })
  return { ok: true, run }
})

app.get('/api/v3/strategy/last', async () => {
  const last = strategy.last()
  return last ? { ok: true, run: last } : { ok: false, error: { code: 'strategy/never-run', message: '尚未运行流水线（POST /api/v3/strategy/run）' } }
})

app.post('/api/v3/ml/backtest', async ({ body }) => {
  if (!body.ticker) return { ok: false, error: { code: 'bad-request', message: 'ticker 必填' } }
  const envelope = await wb.call('series', { ticker: String(body.ticker), period: '1d', limit: Number(body.limit || 500) })
  if (!envelope?.ok) return { ok: false, error: envelope?.error ?? { code: 'wb/error' } }
  const result = backtestMomentum(envelope.value.bars, { window: Number(body.window || 20), rebalanceDays: Number(body.rebalanceDays || 5) })
  if (result.error) return { ok: false, error: { code: 'backtest/insufficient', message: result.error } }
  return { ok: true, ticker: body.ticker, metrics: result.metrics, equity: result.equity }
})

app.post('/api/v3/ml/param_sweep', async ({ body }) => {
  if (!body.ticker) return { ok: false, error: { code: 'bad-request', message: 'ticker 必填' } }
  const envelope = await wb.call('series', { ticker: String(body.ticker), period: '1d', limit: Number(body.limit || 500) })
  if (!envelope?.ok) return { ok: false, error: envelope?.error ?? { code: 'wb/error' } }
  const result = paramSweep(envelope.value.bars, { windows: body.windows ?? [10, 20, 30, 60], rebalanceDays: body.rebalanceDays ?? [5, 10, 20] })
  return { ok: true, ticker: body.ticker, ...result }
})

app.get('/api/v3/risk', async () => wbValue(wb.call('risk', {})))

app.get('/api/v3/execution', async () => {
  const [positions, orders, deals] = await Promise.all([
    wbValue(wb.call('positions', {})),
    wbValue(wb.call('orders_open', {})),
    wbValue(wb.call('deals_today', {})),
  ])
  const omsView = await oms.view()
  return { ok: true, positions: positions.data ?? null, orders_open: orders.data ?? null, deals_today: deals.data ?? null, oms: omsView }
})

app.get('/api/v3/metrics', async () => {
  const health = await wb.health()
  const tools = mcp.catalog
  return {
    ok: true,
    ...metrics.snapshot(),
    toolTotal: Object.values(tools).reduce((sum, list) => sum + list.length, 0),
    toolDomains: Object.keys(tools).length,
    workbenchUp: health.ok,
    headless: { ...metrics.snapshot().headless, today: runner.stats().today, breaker: runner.stats().breaker },
    oms: oms.statusCounts(),
    sdk: sdkChannel.status(),
  }
})

app.get('/api/v3/oms/orders', async () => ({ ok: true, ...(await oms.view()) }))

app.post('/api/v3/oms/sync', async () => oms.sync())

app.get('/api/v3/gateway', async () => ({
  ok: true,
  channels: {
    mcp: { direction: '平台→Harness（工具暴露）', protocol: 'MCP stdio/NDJSON + HTTP /api/v3/mcp', status: 'running' },
    sdk: (() => {
      const s = sdkChannel.status()
      return { direction: '平台↔Harness（会话驱动）', protocol: '换行分帧 JSON-RPC / stdio', status: s.status, reason: s.reason, route: s.route, serverInfo: s.serverInfo, profile: s.profile, sessionWarning: s.sessionWarning, lastTurn: s.lastTurn }
    })(),
    headless: { direction: '平台→Harness（自动唤醒）', protocol: 'CLI 子进程', command: `${config.channels.headless.dshBin} --profile ${config.channels.headless.profile} "<task>"`, status: 'ready' },
  },
  scheduler: scheduler.view(),
  headless: runner.stats(),
}))

app.get('/api/v3/tools', async ({ query }) => {
  const catalog = mcp.catalog
  if (query.domain) return { ok: true, total: (catalog[query.domain] ?? []).length, domains: { [query.domain]: catalog[query.domain] ?? [] } }
  const total = Object.values(catalog).reduce((sum, list) => sum + list.length, 0)
  return { ok: true, total, domains: catalog }
})

app.get('/api/v3/settings', async () => {
  const envKeys = ['DSH_HOME', 'DEEPSEEK_API_KEY', 'QUANT_MCP_NODE', 'QUANT_MCP_SERVER', 'QUANT_MCP_CWD', 'QUANT_MCP_LOG', 'FUTU_OPEND_HOST', 'FUTU_OPEND_PORT', 'TUSHARE_TOKEN']
  const futu = config.sources.futu
  return {
    ok: true,
    mode_note: '模式切换（sim/live）沿用既有工作台 Web 闸门：switch_mode 只接受 live→sim，sim→live 由用户在 8397 Web 输入口令完成；V3.0 不另开口子。',
    trading_mode: 'sim（以工作台为准，经 wb snapshot.mode 回读）',
    futu: {
      channel: futu.channel,
      mcp_bearer: { present: futu.mcpTokenPresent, expiry: futu.mcpTokenExpiry },
      openapi: { mode: futu.openapiMode, config_keys: futu.openapiConfigKeys },
    },
    env: envKeys.map((key) => {
      const injected = Boolean(process.env[key] || (key === 'DSH_HOME' && config.home))
      const source = process.env[key] ? '环境变量' : key === 'DSH_HOME' ? '默认（DSH_HOME）' : '环境变量（未注入）'
      return { key, injected, source }
    }),
    data_sources: ['workbench(8397) 77 工具', '富途 OpenAPI/OpenD', 'futu-token(MCP Bearer)', 'trading-venv(akshare/pandas)', '~/.dsh/trading-platform.json(自选/流水线配置)'],
  }
})

app.post('/api/v3/headless/run', async ({ body }) => {
  const taskType = String(body.task_type || 'pre_market_scan')
  const record = await runner.execute(runner.buildPrompt(taskType, body.context ?? {}))
  return { ok: record.success, record }
})

app.post('/api/v3/risk/check', async ({ body }) => {
  const check = checkOrder(body.order ?? {}, body.context ?? {})
  return { ok: true, check }
})

app.post('/api/v3/mcp', async ({ body }) => {
  const response = await mcp.handleMessage(body)
  return JSON.parse(JSON.stringify(response ?? { jsonrpc: '2.0', result: {} }))
})

// ── 静态 UI（9 页设计稿 + 资产） ────────────────────────────────────────────
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json' }

function serveStatic(pathname, res) {
  const rel = pathname === '/' ? 'index.html' : pathname.replace(/^\/+/, '')
  const target = path.join(config.webDir, rel)
  if (!target.startsWith(config.webDir) || !fs.existsSync(target) || !fs.statSync(target).isFile()) {
    res.writeHead(404, { 'content-type': 'text/plain; charset=utf-8' })
    res.end('not found')
    return
  }
  res.writeHead(200, { 'content-type': MIME[path.extname(target)] ?? 'application/octet-stream' })
  res.end(fs.readFileSync(target))
}

const server = http.createServer(async (req, res) => {
  const pathname = new URL(req.url, 'http://localhost').pathname
  if (pathname === '/metrics') {
    const health = await wb.health()
    res.writeHead(200, { 'content-type': 'text/plain; version=0.0.4; charset=utf-8' })
    res.end(metrics.render({ workbenchUp: health.ok, omsStages: oms.statusCounts(), headlessStats: runner.stats(), sdkStatus: sdkChannel.status() }))
    return
  }
  if (pathname === '/healthz' || pathname.startsWith('/api/')) return void app.handle(req, res)
  if (req.method !== 'GET') {
    res.writeHead(405, { 'content-type': 'text/plain; charset=utf-8' })
    return void res.end('method not allowed')
  }
  serveStatic(pathname, res)
})

if (process.env.QUANT_V3_SCHEDULER !== '0') {
  setInterval(() => scheduler.tick(), 30_000).unref()
}

server.listen(config.service.port, config.service.host, () => {
  console.log(`[quant-v3] listening on http://${config.service.host}:${config.service.port} (workbench=${config.workbench.base})`)
})

export { server, config, app, mcp, runner, scheduler, oms }
