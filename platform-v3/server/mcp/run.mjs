// MCP stdio 入口：`npm run start:mcp`。
// 由 Harness 侧（dsh-cordis-universal-adapter / 任意 MCP 客户端）以 stdio 拉起：
//   command: node platform-v3/server/mcp/run.mjs
//   env:    WORKBENCH_BASE（缺省 http://127.0.0.1:8397）
import loadConfig from '../config.mjs'
import createWorkbenchClient from '../wb-client.mjs'
import createStore from '../store.mjs'
import { backtestMomentum, paramSweep } from '../strategy/backtest.mjs'
import createStrategyService from '../strategy/pipeline.mjs'
import { createMcpServer } from './stdio.mjs'

const config = loadConfig()
const wb = createWorkbenchClient(config.workbench)
const store = createStore(config.dataDir)
const strategy = createStrategyService({ wbCall: (tool, args, options) => wb.call(tool, args, options), watchlist: config.sources.watchlist, store })

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
  localTools: [
    {
      name: 'run_backtest',
      description: '[ml] 单标的动量 long/flat 回测（真实富途日 K，PIT 对齐）。',
      inputSchema: { type: 'object', properties: { ticker: { type: 'string' }, window: { type: 'number' }, rebalanceDays: { type: 'number' }, limit: { type: 'number' } }, required: ['ticker'], additionalProperties: false },
      async execute(args) {
        const envelope = await wb.call('series', { ticker: String(args.ticker), period: '1d', limit: Number(args.limit || 500) })
        if (!envelope?.ok) return { text: JSON.stringify(envelope?.error ?? {}, null, 1), isError: true }
        const result = backtestMomentum(envelope.value.bars, { window: Number(args.window || 20), rebalanceDays: Number(args.rebalanceDays || 5) })
        if (result.error) return { text: result.error, isError: true }
        return { text: JSON.stringify(result.metrics, null, 1) }
      },
    },
    {
      name: 'param_sweep',
      description: '[ml] 参数扫描：动量窗口 × 调仓周期网格（sharpe/年化/最大回撤 + 最优格）。',
      inputSchema: { type: 'object', properties: { ticker: { type: 'string' }, windows: { type: 'array', items: { type: 'number' } }, rebalanceDays: { type: 'array', items: { type: 'number' } }, limit: { type: 'number' } }, required: ['ticker'], additionalProperties: false },
      async execute(args) {
        const envelope = await wb.call('series', { ticker: String(args.ticker), period: '1d', limit: Number(args.limit || 500) })
        if (!envelope?.ok) return { text: JSON.stringify(envelope?.error ?? {}, null, 1), isError: true }
        return { text: JSON.stringify(paramSweep(envelope.value.bars, { windows: args.windows ?? [10, 20, 30, 60], rebalanceDays: args.rebalanceDays ?? [5, 10, 20] }), null, 1) }
      },
    },
    {
      name: 'strategy_run',
      description: '[ecosystem] 运行 PDAT→PET 研究流水线，产出调仓建议提案（不下单）。',
      inputSchema: { type: 'object', properties: { universe: { type: 'array', items: { type: 'string' } }, topN: { type: 'number' }, window: { type: 'number' } }, additionalProperties: false },
      async execute(args) {
        const run = await strategy.run({ universe: args.universe, topN: Number(args.topN || 2), window: Number(args.window || 20) })
        return { text: JSON.stringify(run, null, 1) }
      },
    },
  ],
})
mcp.serveStdio()
