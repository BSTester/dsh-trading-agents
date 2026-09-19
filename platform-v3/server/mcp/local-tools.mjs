// MCP 本地计算工具（不进 workbench 直通）：回测、参数扫描、研究流水线、组合风险量、A 股新闻。
// index.mjs（HTTP 服务）与 run.mjs（stdio 入口）共用这一份定义，避免两处漂移。
import { backtestMomentum, paramSweep } from '../strategy/backtest.mjs'
import { portfolioRisk } from '../data/risk-analytics.mjs'

export function createLocalTools({ wbCall, portfolio, akshare, strategy, config }) {
  const seriesFor = async (ticker, limit = 500) => {
    const envelope = await wbCall('series', { ticker, period: '1d', limit })
    return envelope?.ok ? (envelope.value?.bars ?? []) : null
  }

  return [
    {
      name: 'run_backtest',
      description: '[ml] 单标的动量 long/flat 回测。真实富途日 K；PIT 对齐：t 日持仓仅由 ≤t-1 收盘价决定，无前视。轻量引擎（等权、无滑点建模）。',
      inputSchema: { type: 'object', properties: { ticker: { type: 'string' }, window: { type: 'number', description: '动量窗口，默认 20' }, rebalanceDays: { type: 'number', description: '调仓周期，默认 5' }, limit: { type: 'number', description: 'K 线根数，默认 500' } }, required: ['ticker'], additionalProperties: false },
      async execute(args) {
        const bars = await seriesFor(String(args.ticker), Number(args.limit || 500))
        if (!bars) return { text: `series 取数失败：${args.ticker}`, isError: true }
        const result = backtestMomentum(bars, { window: Number(args.window || 20), rebalanceDays: Number(args.rebalanceDays || 5) })
        if (result.error) return { text: result.error, isError: true }
        return { text: JSON.stringify(result.metrics, null, 1) }
      },
    },
    {
      name: 'param_sweep',
      description: '[ml] 参数扫描：动量窗口 × 调仓周期网格，返回各格 sharpe/年化/最大回撤与最优格（热力图数据）。',
      inputSchema: { type: 'object', properties: { ticker: { type: 'string' }, windows: { type: 'array', items: { type: 'number' } }, rebalanceDays: { type: 'array', items: { type: 'number' } }, limit: { type: 'number' } }, required: ['ticker'], additionalProperties: false },
      async execute(args) {
        const bars = await seriesFor(String(args.ticker), Number(args.limit || 500))
        if (!bars) return { text: `series 取数失败：${args.ticker}`, isError: true }
        const result = paramSweep(bars, { windows: args.windows ?? [10, 20, 30, 60], rebalanceDays: args.rebalanceDays ?? [5, 10, 20] })
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
    {
      name: 'calc_var',
      description: '[risk] 组合风险量：历史模拟法 VaR/CVaR、Beta/Alpha、IR、最大回撤与 Kupiec POF 检验。权重缺省由工作台 frozen 计划目标或自选池等权解析。',
      inputSchema: { type: 'object', properties: { confidence: { type: 'number', description: '置信度，默认 0.95' }, benchmark: { type: 'string', description: '基准指数，默认 SH.000300' }, weights: { type: 'object', description: '组合权重（缺省自动解析）', additionalProperties: true }, limit: { type: 'number', description: '日 K 根数，默认 250' } }, additionalProperties: false },
      async execute(args) {
        const limit = Math.min(Math.max(Number(args.limit || 250), 60), 2000)
        const resolved = await portfolio.resolve({ weights: args.weights })
        if (!resolved.weights) return { text: resolved.source, isError: true }
        const tickers = Object.keys(resolved.weights)
        if (tickers.length < 2) return { text: `组合仅 ${tickers.length} 个标的（${resolved.source}），无法计算横截面风险量`, isError: true }
        const [seriesMap, bench, navInfo] = await Promise.all([
          portfolio.loadSeriesMap(tickers, limit),
          portfolio.benchmark(args.benchmark || config.data.benchmark, limit),
          portfolio.nav(),
        ])
        const analytics = portfolioRisk({
          seriesByTicker: seriesMap,
          benchmarkBars: bench.ok ? bench.bars : null,
          weights: resolved.weights,
          confidence: Number(args.confidence || 0.95),
          nav: navInfo.nav,
        })
        if (analytics.error) return { text: analytics.error, isError: true }
        const { equityCurve, ...rest } = analytics
        return { text: JSON.stringify({ portfolioSource: resolved.source, benchmark: bench.ok ? bench.ticker : null, navSource: navInfo.source, ...rest }, null, 1) }
      },
    },
    {
      name: 'search_news',
      description: '[data] A 股个股新闻（AKShare 公开端点，免密钥）：标题/摘要/来源/发布时间/链接，附 as_of。',
      inputSchema: { type: 'object', properties: { symbol: { type: 'string', description: 'A 股代码，如 600519 或 SH.600519' }, limit: { type: 'number', description: '默认 10，最大 50' } }, required: ['symbol'], additionalProperties: false },
      async execute(args) {
        const symbol = String(args.symbol).replace(/^(SH|SZ|HK|US)\./i, '')
        const result = await akshare.news(symbol, Math.min(Math.max(Number(args.limit || 10), 1), 50))
        return result.ok ? { text: JSON.stringify(result, null, 1) } : { text: JSON.stringify(result.error, null, 1), isError: true }
      },
    },
  ]
}

export default createLocalTools
