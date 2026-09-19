// 外部数据源测试：SEC EDGAR（假 fetch）、Tushare（假 fetch，含 token 缺失路径）、因子矩阵（假 wbCall）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import createSecSource from '../server/data/sec.mjs'
import createTushareSource from '../server/data/tushare.mjs'
import createFactorSource from '../server/data/factors.mjs'

const CIK_MAP = { 0: { cik_str: 320193, ticker: 'AAPL', title: 'Apple Inc.' } }

function fakeSecFetch(urls) {
  return async (url) => {
    const key = Object.keys(urls).find((k) => url.includes(k))
    if (!key) return { ok: false, status: 404, json: async () => ({}) }
    return { ok: true, status: 200, json: async () => urls[key] }
  }
}

test('SEC：ticker→CIK 映射 + 按期末去重取最近 4 期（10-K/10-Q）', async () => {
  const sec = createSecSource({
    fetchImpl: fakeSecFetch({
      'company_tickers.json': CIK_MAP,
      'us-gaap/Revenues.json': {
        label: 'Revenues',
        units: {
          USD: [
            { end: '2025-09-30', val: 100, form: '10-K', fy: 2025, fp: 'FY', filed: '2025-11-01' },
            { end: '2025-09-30', val: 100, form: '10-K', fy: 2025, fp: 'FY', filed: '2025-11-02' },
            { end: '2024-09-30', val: 90, form: '10-K', fy: 2024, fp: 'FY', filed: '2024-11-01' },
            { end: '2026-06-30', val: 80, form: '10-Q', fy: 2026, fp: 'Q3', filed: '2026-08-01' },
            { end: '2026-03-31', val: 70, form: '10-Q', fy: 2026, fp: 'Q2', filed: '2026-05-01' },
          ],
        },
      },
    }),
  })
  const result = await sec.financials('AAPL', { statement: 'income', periods: 4 })
  assert.equal(result.ok, true)
  assert.equal(result.cik, '0000320193')
  assert.equal(result.company, 'Apple Inc.')
  const revenues = result.lines.find((l) => l.tag === 'Revenues')
  assert.ok(revenues, '应含 Revenues 行')
  assert.deepEqual(revenues.points.map((p) => p.end), ['2024-09-30', '2025-09-30', '2026-03-31', '2026-06-30'])
  assert.equal(revenues.points.at(-1).val, 80)
  assert.ok(result.missing.some((m) => m.tag === 'NetIncomeLoss'), '未申报的标签应进入 missing 而不是编造')
})

test('SEC：未知 ticker 如实报错', async () => {
  const sec = createSecSource({ fetchImpl: fakeSecFetch({ 'company_tickers.json': CIK_MAP }) })
  const result = await sec.financials('NOPE')
  assert.equal(result.ok, false)
  assert.equal(result.error.code, 'sec/unknown-ticker')
})

test('Tushare：未注入 token 时不发请求并如实报错', async () => {
  let called = 0
  const tushare = createTushareSource({ token: null, fetchImpl: async () => { called += 1; return { ok: true, json: async () => ({}) } } })
  assert.equal(tushare.status().configured, false)
  const result = await tushare.income('600519.SH', '20260630')
  assert.equal(result.ok, false)
  assert.equal(result.error.code, 'tushare/no-token')
  assert.equal(called, 0, '未配置时不应发出网络请求')
})

test('Tushare：配置后解析 fields/items 为行对象；API 错误如实透传', async () => {
  const okFetch = async () => ({ ok: true, json: async () => ({ code: 0, data: { fields: ['end_date', 'revenue', 'n_income'], items: [['20260630', 922.78, 445.17]] } }) })
  const tushare = createTushareSource({ token: 'fake-token', fetchImpl: okFetch })
  const result = await tushare.income('600519.SH', '20260630')
  assert.equal(result.ok, true)
  assert.equal(result.source, 'tushare/income')
  assert.deepEqual(result.rows[0], { end_date: '20260630', revenue: 922.78, n_income: 445.17 })

  const errFetch = async () => ({ ok: true, json: async () => ({ code: 2002, msg: '权限不足' }) })
  const bad = createTushareSource({ token: 'fake-token', fetchImpl: errFetch })
  const badResult = await bad.income('600519.SH', '20260630')
  assert.equal(badResult.ok, false)
  assert.match(badResult.error.message, /权限不足/)
})

test('因子矩阵：z 值矩阵 + IC 统计（真实工作台返回结构）', async () => {
  const wbCall = async (tool) => {
    if (tool === 'factors') {
      return {
        ok: true,
        value: {
          rows: [
            { ticker: 'SH.600000', as_of: '2026-09-18', z: { mom_20: 0.7, vol_20: -0.3 }, factors: { mom_20: 0.02 } },
            { ticker: 'SH.600009', as_of: '2026-09-18', z: { mom_20: -0.7, vol_20: 0.3 }, factors: { mom_20: -0.01 } },
          ],
        },
      }
    }
    if (tool === 'ic') {
      return { ok: true, value: { factor: 'mom_20', tickers: ['A', 'B', 'C'], points: [{ t: '2026-01-02', ic: 0.2 }, { t: '2026-01-09', ic: -0.1 }, { t: '2026-01-16', ic: 0.3 }] } }
    }
    return { ok: false, error: { code: 'unexpected', message: tool } }
  }
  const factors = createFactorSource({ wbCall })
  const matrix = await factors.matrix(['SH.600000', 'SH.600009'])
  assert.equal(matrix.ok, true)
  assert.deepEqual(matrix.factors, ['mom_20', 'vol_20'])
  assert.deepEqual(matrix.matrix, [[0.7, -0.3], [-0.7, 0.3]])
  assert.equal(matrix.as_of, '2026-09-18')

  const ic = await factors.ic(['A', 'B', 'C'])
  assert.equal(ic.ok, true)
  assert.equal(ic.observations, 3)
  assert.equal(ic.meanIc, 0.1333)
  assert.equal(ic.latestIc, 0.3)
  assert.ok(ic.ir !== null)

  const tooFew = await factors.ic(['A'])
  assert.equal(tooFew.ok, false)
  assert.equal(tooFew.error.code, 'factors/too-few')
})
