// MCP stdio 入口：`npm run start:mcp`。
// 由 Harness 侧（dsh-cordis-universal-adapter / 任意 MCP 客户端）以 stdio 拉起：
//   command: node platform-v3/server/mcp/run.mjs
//   env:    WORKBENCH_BASE（缺省 http://127.0.0.1:8397）
import loadConfig from '../config.mjs'
import createWorkbenchClient from '../wb-client.mjs'
import createStore from '../store.mjs'
import createStrategyService from '../strategy/pipeline.mjs'
import createAkshareSource from '../data/akshare.mjs'
import createPortfolioResolver from '../data/portfolio.mjs'
import { createLocalTools } from './local-tools.mjs'
import { createMcpServer } from './stdio.mjs'

const config = loadConfig()
const wb = createWorkbenchClient(config.workbench)
const store = createStore(config.dataDir)
const strategy = createStrategyService({ wbCall: (tool, args, options) => wb.call(tool, args, options), watchlist: config.sources.watchlist, store })
const portfolio = createPortfolioResolver({ wbCall: (tool, args, options) => wb.call(tool, args, options), watchlist: config.sources.watchlist, defaultBenchmark: config.data.benchmark })
const akshare = createAkshareSource({ pythonBin: config.data.pythonBin, timeoutMs: config.data.akshareTimeoutMs })

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
  localTools: createLocalTools({ wbCall: (tool, args, options) => wb.call(tool, args, options), portfolio, akshare, strategy, config }),
})
mcp.serveStdio()
