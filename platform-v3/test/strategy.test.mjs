// 策略回测引擎 + PDAT→PET 流水线测试。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { backtestMomentum, paramSweep } from '../server/strategy/backtest.mjs'
import createStrategyService from '../server/strategy/pipeline.mjs'
import createStore from '../server/store.mjs'

function makeBars(closes) {
  return closes.map((c, i) => ({ t: `d${i}`, o: c, h: c, l: c, c }))
}

test('回测：单边上涨序列持仓为多、净值单调增、无前视', () => {
  // 严格单调上涨：warmup 后动量恒为正 → 持仓为 1，日收益恒正 → 净值严格递增
  const closes = Array.from({ length: 120 }, (_, i) => 100 + i * 0.5)
  const bars = makeBars(closes)
  const result = backtestMomentum(bars, { window: 20, rebalanceDays: 5 })
  assert.equal(result.error, undefined)
  assert.ok(result.metrics.sharpe > 1, `sharpe=${result.metrics.sharpe}`)
  assert.ok(result.metrics.annReturnPct > 0)
  assert.ok(result.metrics.heldDays > 80, `heldDays=${result.metrics.heldDays}`)
  const values = result.equity.map((p) => p.value)
  assert.deepEqual(values, [...values].sort((a, b) => a - b))
})

test('回测：单边下跌序列保持空仓、收益为 0', () => {
  const closes = Array.from({ length: 100 }, (_, i) => 200 - i)
  const result = backtestMomentum(makeBars(closes), { window: 20, rebalanceDays: 5 })
  assert.equal(result.error, undefined)
  assert.ok(Math.abs(result.metrics.annReturnPct) < 1e-9)
  assert.ok(result.metrics.heldDays <= 1)
})

test('回测：数据不足报错', () => {
  const result = backtestMomentum(makeBars([1, 2, 3]), { window: 20 })
  assert.match(result.error, /insufficient/)
})

test('参数扫描：网格尺寸与最优格', () => {
  const closes = []
  for (let i = 0; i < 120; i++) closes.push(100 + i * 0.8 + Math.sin(i / 3) * 2)
  const result = paramSweep(makeBars(closes), { windows: [10, 20], rebalanceDays: [5, 10] })
  assert.equal(result.grid.length, 4)
  assert.ok(result.best && result.grid.every((r) => r.sharpe === null || result.best.sharpe >= r.sharpe))
})

test('流水线 PDAT→PET：mock workbench，产出提案且不下单', async () => {
  const up = makeBars(Array.from({ length: 120 }, (_, i) => 100 + i))
  const down = makeBars(Array.from({ length: 120 }, (_, i) => 300 - i))
  const wbCall = async (tool, args) => {
    if (tool === 'series') {
      const ticker = args.ticker
      return { ok: true, value: { ticker, bars: ticker.endsWith('A') ? up : down } }
    }
    if (tool === 'factors') {
      return {
        ok: true,
        value: {
          rows: args.tickers.map((ticker) => ({
            ticker,
            z: { mom_20: ticker.endsWith('A') ? 1.5 : -1.2, mom_60: ticker.endsWith('A') ? 1.1 : -0.8, trend: ticker.endsWith('A') ? 0.9 : -0.5 },
          })),
        },
      }
    }
    return { ok: false, error: { code: 'unexpected', message: tool } }
  }
  const store = createStore('/tmp/quant-v3-strategy-test')
  const service = createStrategyService({ wbCall, watchlist: ['AAA', 'BBB'], store })
  const run = await service.run({ topN: 1, window: 20 })
  assert.equal(run.stages.PDAT.universe.length, 2)
  assert.deepEqual(run.stages.PCPT.longs, ['AAA'])
  assert.equal(run.stages.PET.proposals, 2)
  const long = run.proposals.find((p) => p.action === '增持')
  assert.equal(long.ticker, 'AAA')
  assert.match(long.action_hint, /工作台受约束入口/)
  assert.ok(!JSON.stringify(run).includes('place_order'))
  const last = service.last()
  assert.ok(last && last.asOf)
})
