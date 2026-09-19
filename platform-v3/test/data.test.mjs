// 数据面测试：组合风险量数学 + AKShare 桥（用桩进程，不打网络）+ 对齐逻辑。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { portfolioRisk, alignSeries, returnsOf, kupiecPof, maxDrawdown, stdev } from '../server/data/risk-analytics.mjs'
import createAkshareSource from '../server/data/akshare.mjs'

function barsFrom(closes, startDay = 0) {
  // 生成严格递增的唯一交易日（跨月也唯一），避免被按交易日对齐时去重
  const base = Date.UTC(2025, 0, 1) + startDay * 86400000
  return closes.map((c, i) => ({ t: new Date(base + i * 86400000).toISOString().slice(0, 10), c: String(c) }))
}

test('对齐：只保留所有序列共有的交易日', () => {
  const a = barsFrom([10, 11, 12, 13])
  const b = [{ t: a[1].t, c: '20' }, { t: a[2].t, c: '21' }, { t: a[3].t, c: '22' }]
  const { dates, closes } = alignSeries({ A: a, B: b })
  assert.equal(dates.length, 3)
  assert.equal(closes.A.length, 3)
  assert.deepEqual(closes.B, [20, 21, 22])
})

test('组合风险量：VaR/CVaR 次序、Beta=1（对自身）、Kupiec 无破位时通过', () => {
  // 确定性构造：两条序列，组合与基准都取同一条 → beta 应为 1，alpha≈0
  const closes = Array.from({ length: 200 }, (_, i) => 100 * Math.exp(0.0005 * i + 0.01 * Math.sin(i / 5)))
  const series = { A: barsFrom(closes), B: barsFrom(closes.map((c) => c * 1.0001)) }
  const result = portfolioRisk({ seriesByTicker: series, benchmarkBars: barsFrom(closes), weights: { A: 0.5, B: 0.5 }, confidence: 0.95, nav: 1_000_000 })
  assert.equal(result.error, undefined)
  assert.ok(result.cvarDailyPct >= result.varDailyPct, 'CVaR 应不小于 VaR')
  assert.ok(result.varAmount > 0, '有 NAV 时应给出金额')
  assert.ok(Math.abs(result.beta - 1) < 0.05, `beta≈1，实际 ${result.beta}`)
  assert.ok(Math.abs(result.alphaAnnPct) < 5, `alpha≈0，实际 ${result.alphaAnnPct}`)
  assert.equal(result.observations > 150, true)
  assert.ok(result.kupiec.pValue >= 0 && result.kupiec.pValue <= 1)
})

test('组合风险量：数据不足时返回错误而不是估算', () => {
  const short = barsFrom([1, 2, 3, 4, 5])
  const result = portfolioRisk({ seriesByTicker: { A: short, B: short }, weights: { A: 1, B: 1 } })
  assert.match(result.error, /交易日不足/)
})

test('Kupiec POF：破位数偏离期望即拒绝（统计学上正确）', () => {
  // 期望破位 12.5 次（250 × 5%）：接近期望应通过，0 次与 60 次都应被拒绝
  const ok = kupiecPof(13, 250, 0.05)
  assert.equal(ok.pass, true, `13 次应通过，LR=${ok.lr}`)
  const tooFew = kupiecPof(0, 250, 0.05)
  assert.equal(tooFew.pass, false, '0 破位显著偏低，应拒绝')
  assert.ok(tooFew.lr > 10)
  const tooMany = kupiecPof(60, 250, 0.05)
  assert.equal(tooMany.pass, false)
  assert.ok(tooMany.lr > 10)
})

test('辅助函数：returnsOf / maxDrawdown / stdev', () => {
  const r = returnsOf([100, 110, 99])
  assert.equal(r.length, 2)
  assert.ok(Math.abs(r[0] - 0.1) < 1e-9)
  assert.ok(Math.abs(maxDrawdown([1, 1.2, 0.9]) + 0.25) < 1e-9)
  assert.ok(Math.abs(stdev([1, 1, 1])) < 1e-12)
})

// ── AKShare 桥：用假进程验证参数拼装、JSON 解析、超时与错误归一 ──────────────
function fakeSpawn(behavior) {
  return (bin, args) => {
    const child = new EventEmitter()
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    child.kill = () => {}
    child.spawnArgs = { bin, args }
    setImmediate(() => behavior(child))
    return child
  }
}

test('AKShare 桥：解析 JSON 输出并透传 as_of/source', async () => {
  let seenArgs = null
  const source = createAkshareSource({
    pythonBin: '/fake/python',
    scriptPath: '/fake/bridge.py',
    spawnImpl: fakeSpawn((child) => {
      seenArgs = child.spawnArgs
      child.stdout.emit('data', JSON.stringify({ ok: true, as_of: '2026-09-19T10:00:00Z', source: 'akshare/stock_news_em', rows: [{ title: '真实新闻' }] }))
      child.emit('close', 0)
    }),
  })
  const result = await source.news('600519', 5)
  assert.equal(result.ok, true)
  assert.equal(result.source, 'akshare/stock_news_em')
  assert.equal(result.rows[0].title, '真实新闻')
  assert.equal(seenArgs.bin, '/fake/python')
  assert.deepEqual(seenArgs.args, ['/fake/bridge.py', 'news', '--symbol', '600519', '--limit', '5'])
})

test('AKShare 桥：无输出 / 非法 JSON / 超时都归一为错误对象', async () => {
  const noOutput = createAkshareSource({ pythonBin: 'p', scriptPath: 's', spawnImpl: fakeSpawn((child) => child.emit('close', 1)) })
  assert.equal((await noOutput.news('600519')).error.code, 'akshare/no-output')

  const badJson = createAkshareSource({
    pythonBin: 'p',
    scriptPath: 's',
    spawnImpl: fakeSpawn((child) => {
      child.stdout.emit('data', 'not json\n')
      child.emit('close', 0)
    }),
  })
  assert.equal((await badJson.news('600519')).error.code, 'akshare/bad-json')

  const slow = createAkshareSource({ pythonBin: 'p', scriptPath: 's', timeoutMs: 30, spawnImpl: fakeSpawn(() => {}) })
  assert.equal((await slow.news('600519')).error.code, 'akshare/timeout')
})
