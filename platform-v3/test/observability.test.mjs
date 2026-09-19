// 可观测性与审计测试：指标渲染、钩子计数、审计留痕、/metrics 端点。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import http from 'node:http'
import { createMetrics, createAudit } from '../server/observability.mjs'
import createWorkbenchClient from '../server/wb-client.mjs'

test('指标渲染：含规格要求的指标族与实时分量', () => {
  const metrics = createMetrics()
  metrics.wbCall('series', true, 42)
  metrics.wbCall('factors', false, 43000)
  metrics.tool('query_quote', true, 120)
  metrics.headless(true, 42000, 1500)
  const text = metrics.render({ workbenchUp: true, omsStages: { manual: 10, blocked: 0 }, headlessStats: { breaker: { active: 1, queued: 2 } }, sdkStatus: { status: 'pending' } })
  for (const expected of [
    'quant_v3_wb_calls_total{tool="series",result="ok"} 1',
    'quant_v3_wb_calls_total{tool="factors",result="error"} 1',
    'quant_v3_wb_call_duration_ms_sum',
    'quant_v3_mcp_tool_calls_total{tool="query_quote",result="ok"} 1',
    'quant_v3_headless_calls_total{result="success"} 1',
    'quant_v3_headless_tokens_estimate_sum 1500',
    'quant_v3_workbench_up 1',
    'quant_v3_oms_orders{stage="manual"} 10',
    'quant_v3_headless_active 1',
    'quant_v3_sdk_channel_ready 0',
  ]) {
    assert.ok(text.includes(expected), `缺少指标行: ${expected}`)
  }
  assert.match(text, /\n$/)
})

test('审计：追加写入并可按行读回', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'quant-v3-audit-'))
  const audit = createAudit(dir)
  audit.append({ kind: 'api', action: '/api/v3/oms/sync', status: 200, remote: '127.0.0.1' })
  audit.append({ kind: 'api', action: '/api/v3/strategy/run', status: 200, remote: '127.0.0.1' })
  const rows = audit.readAll()
  assert.equal(rows.length, 2)
  assert.equal(rows[0].action, '/api/v3/oms/sync')
  assert.ok(rows[0].at)
})

test('wb-client 观测钩子：成功与失败都上报', async () => {
  const seen = []
  const real = http.createServer((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify(req.url.includes('bad') ? { ok: false, error: { code: 'x', message: 'y' } } : { ok: true, value: 1 }))
  })
  await new Promise((resolve) => real.listen(0, '127.0.0.1', resolve))
  const client = createWorkbenchClient({ base: `http://127.0.0.1:${real.address().port}`, onCall: (info) => seen.push(info) })
  await client.call('series', {})
  await client.call('bad', {})
  assert.deepEqual(seen.map((s) => [s.tool, s.ok]), [['series', true], ['bad', false]])
  assert.ok(seen.every((s) => typeof s.ms === 'number'))
  real.close()
})
