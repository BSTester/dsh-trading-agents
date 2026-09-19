// 集成测试：拉起 V3.0 服务（临时端口），验证 healthz / API / MCP HTTP 端点 / 静态 UI。
import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'

const PORT = 18407
const BASE = `http://127.0.0.1:${PORT}`
let child

before(async () => {
  const root = path.resolve(import.meta.dirname, '..')
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'quant-v3-'))
  child = spawn(process.execPath, ['server/index.mjs'], {
    cwd: root,
    env: { ...process.env, QUANT_V3_PORT: String(PORT), QUANT_V3_DATA: dataDir, QUANT_V3_SCHEDULER: '0', DSH_HOME: '/tmp/quant-v3-it-home' },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  child.stderr.on('data', () => {})
  const deadline = Date.now() + 15000
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${BASE}/healthz`)
      if (res.ok) return
    } catch {}
    await new Promise((r) => setTimeout(r, 200))
  }
  throw new Error('server did not start')
})

after(() => {
  child?.kill('SIGTERM')
})

test('healthz：服务与服务间依赖状态', async () => {
  const res = await fetch(`${BASE}/healthz`)
  const body = await res.json()
  assert.equal(body.service, 'quant-platform-v3')
  assert.equal(body.channels.mcp, 'running')
  assert.ok('workbench' in body)
})

test('settings：复用既有配置且不泄露密钥', async () => {
  const res = await fetch(`${BASE}/api/v3/settings`)
  const body = await res.json()
  assert.equal(body.futu.channel, 'openapi')
  assert.equal(typeof body.futu.mcp_bearer.present, 'boolean')
  const text = JSON.stringify(body)
  assert.ok(!text.includes('app_key'))
})

test('tools：六域目录含发现代理与一级工具', async () => {
  const res = await fetch(`${BASE}/api/v3/tools`)
  const body = await res.json()
  assert.ok(body.total > 5)
  assert.ok(body.domains.data.length > 0)
})

test('gateway：三通道与熔断参数', async () => {
  const res = await fetch(`${BASE}/api/v3/gateway`)
  const body = await res.json()
  assert.equal(body.channels.headless.status, 'ready')
  assert.equal(body.headless.breaker.concurrencyLimit, 3)
})

test('MCP over HTTP：initialize + tools/list', async () => {
  const init = await fetch(`${BASE}/api/v3/mcp`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} }) })
  const initBody = await init.json()
  assert.equal(initBody.result.serverInfo.name, 'quant-platform-v3')

  const list = await fetch(`${BASE}/api/v3/mcp`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'tools/list' }) })
  const listBody = await list.json()
  assert.ok(listBody.result.tools.length >= 4)
})

test('静态 UI：首页可访问且为设计稿', async () => {
  const res = await fetch(`${BASE}/index.html`)
  assert.equal(res.headers.get('content-type')?.includes('text/html'), true)
  const html = await res.text()
  assert.match(html, /<!DOCTYPE html>/i)
})

test('实时数据层：包含逐点位深绑定与防御性渲染护栏', async () => {
  const js = await fetch(`${BASE}/app.js`)
  const source = await js.text()
  for (const marker of ['setValueWithUnit', 'bindByLabel', 'bindChannelCard', 'deepBindIndex', 'deepBindTools', 'isSeparator']) {
    assert.ok(source.includes(marker), `app.js 缺少 ${marker}`)
  }
  // 行级防御：渲染表格时过滤非数组行（避免单页结构异常把整块实时条打空）
  assert.match(source, /filter\(\(r\) => Array\.isArray\(r\)\)/)
})

test('JSON 指标端点：与 Prometheus 文本同源', async () => {
  const res = await fetch(`${BASE}/api/v3/metrics`)
  const body = await res.json()
  assert.equal(body.ok, true)
  for (const key of ['mcp', 'wb', 'http', 'headless', 'toolTotal', 'toolDomains', 'workbenchUp']) {
    assert.ok(key in body, `缺少字段 ${key}`)
  }
  assert.ok(Number.isFinite(body.toolTotal) && body.toolTotal > 0)
})

test('静态 UI：9 页都挂了实时数据层 app.js', async () => {
  const js = await fetch(`${BASE}/app.js`)
  assert.equal(js.status, 200)
  for (const page of ['index', 'brain', 'market', 'strategy', 'risk', 'execution', 'gateway', 'tools', 'settings']) {
    const res = await fetch(`${BASE}/${page}.html`)
    const html = await res.text()
    assert.ok(html.includes('src="/app.js"'), `${page}.html missing app.js`)
    assert.match(html, /<!DOCTYPE html>/i)
  }
})
