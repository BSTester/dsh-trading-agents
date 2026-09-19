import { test } from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import createWorkbenchClient from '../server/wb-client.mjs'
import { checkOrder } from '../server/risk.mjs'
import { buildPrompt, createSemaphore } from '../server/gateway/headless.mjs'
import loadConfig from '../server/config.mjs'

test('wb-client：envelope 透传与网络错误归一', async () => {
  const real = http.createServer((req, res) => {
    let body = ''
    req.on('data', (c) => {
      body += c
    })
    req.on('end', () => {
      if (req.url === '/api/wb/series') {
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ ok: true, value: { ticker: JSON.parse(body).ticker, bars: 3 } }))
      } else if (req.url === '/api/wb/boom') {
        res.writeHead(200, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ ok: false, error: { code: 'wb/boom', message: '炸了' } }))
      } else {
        res.writeHead(404)
        res.end('{}')
      }
    })
  })
  await new Promise((resolve) => real.listen(0, '127.0.0.1', resolve))
  const port = real.address().port
  const client = createWorkbenchClient({ base: `http://127.0.0.1:${port}`, timeoutMs: 2000 })

  const ok = await client.call('series', { ticker: 'SH.600519' })
  assert.equal(ok.ok, true)
  assert.equal(ok.value.ticker, 'SH.600519')

  const fail = await client.call('boom', {})
  assert.equal(fail.ok, false)
  assert.equal(fail.error.code, 'wb/boom')

  const missing = await client.call('nope', {})
  assert.equal(missing.ok, false)
  assert.match(missing.error.code, /wb\/(http-404|unreachable)/)

  const down = createWorkbenchClient({ base: 'http://127.0.0.1:1', timeoutMs: 500 })
  const unreachable = await down.call('series', {})
  assert.equal(unreachable.ok, false)
  assert.equal(unreachable.error.code, 'wb/unreachable')
  real.close()
})

test('风控分级：阈值内自动 / 超单笔人工 / 触红线阻断', () => {
  const base = { order: { value: 10000 }, context: { nav: 1_000_000, industryPct: 12, drawdownPct: 6 } }
  assert.equal(checkOrder(base.order, base.context).action, 'auto')

  const manual = checkOrder(base.order, { ...base.context, nav: 200_000 })
  assert.equal(manual.action, 'manual')
  assert.match(manual.reasons[0], /单笔占比/)

  const blockedIndustry = checkOrder(base.order, { ...base.context, industryPct: 21 })
  assert.equal(blockedIndustry.action, 'blocked')

  const blockedDrawdown = checkOrder(base.order, { ...base.context, drawdownPct: -15.1 })
  assert.equal(blockedDrawdown.action, 'blocked')

  const unknown = checkOrder({}, {})
  assert.equal(unknown.action, 'manual')
})

test('headless 提示词模板与信号量并发', async () => {
  const prompt = buildPrompt('pre_market_scan', { universe: 'A股新能源', positions: '[{}]', risk_limits: '2%' })
  assert.match(prompt, /扫描 A股新能源 板块/)
  assert.match(prompt, /风控阈值：2%/)
  assert.throws(() => buildPrompt('nope', {}))

  const sem = createSemaphore(2)
  let running = 0
  let peak = 0
  const job = async () => {
    await sem.acquire()
    running += 1
    peak = Math.max(peak, running)
    await new Promise((r) => setTimeout(r, 20))
    running -= 1
    sem.release()
  }
  await Promise.all(Array.from({ length: 5 }, job))
  assert.equal(peak, 2)
  assert.equal(sem.active, 0)
})

test('config：默认值与既有配置复用（不打印密钥）', () => {
  const config = loadConfig({ DSH_HOME: '/tmp/quant-v3-test-home' })
  assert.equal(config.workbench.base, 'http://127.0.0.1:8397')
  assert.equal(config.channels.headless.concurrency, 3)
  assert.ok(config.sources.futu)
  assert.ok(!JSON.stringify(config).includes('app_key'))
})
