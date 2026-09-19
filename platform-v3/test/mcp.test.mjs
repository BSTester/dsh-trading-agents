import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createMcpServer } from '../server/mcp/stdio.mjs'
import { buildCatalog, domainOf, WRITE_TOOLS } from '../server/mcp/catalog.mjs'

const WB_TOOLS = new Set(['series', 'snapshot', 'trade_place', 'f10_detail', 'ic'])

function makeServer() {
  const calls = []
  const wbCall = async (wb, args) => {
    calls.push({ wb, args })
    if (wb === 'series') return { ok: true, value: { ticker: args.ticker, bars: 120 } }
    return { ok: true, value: { wb, args } }
  }
  return { server: createMcpServer({ wbCall, wbToolNames: WB_TOOLS }), calls }
}

test('initialize 返回协议版本与 serverInfo', async () => {
  const { server } = makeServer()
  const res = await server.handleMessage({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} })
  assert.equal(res.result.protocolVersion, '2025-06-18')
  assert.equal(res.result.serverInfo.name, 'quant-platform-v3')
})

test('tools/list 含发现代理与一级工具', async () => {
  const { server } = makeServer()
  const res = await server.handleMessage({ jsonrpc: '2.0', id: 2, method: 'tools/list' })
  const names = res.result.tools.map((t) => t.name)
  for (const expected of ['list_tools', 'call_tool', 'query_quote', 'eval_factor_ic', 'check_risk', 'query_position']) {
    assert.ok(names.includes(expected), `missing ${expected}`)
  }
})

test('一级工具 query_quote 转发到 workbench series', async () => {
  const { server, calls } = makeServer()
  const res = await server.handleMessage({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'query_quote', arguments: { ticker: 'SH.600519' } } })
  assert.equal(res.result.isError, false)
  assert.deepEqual(calls, [{ wb: 'series', args: { ticker: 'SH.600519' } }])
  assert.match(res.result.content[0].text, /SH\.600519/)
})

test('call_tool 直通 workbench 工具；未知工具报 isError', async () => {
  const { server, calls } = makeServer()
  const ok = await server.handleMessage({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'call_tool', arguments: { name: 'ic', args: { factor: 'mom_20d' } } } })
  assert.equal(ok.result.isError, false)
  assert.equal(calls.at(-1).wb, 'ic')
  const bad = await server.handleMessage({ jsonrpc: '2.0', id: 5, method: 'tools/call', params: { name: 'call_tool', arguments: { name: 'nope' } } })
  assert.equal(bad.result.isError, true)
})

test('list_tools 按域汇总且不含写工具为一级入口', async () => {
  const { server } = makeServer()
  const res = await server.handleMessage({ jsonrpc: '2.0', id: 6, method: 'tools/call', params: { name: 'list_tools', arguments: {} } })
  const payload = JSON.parse(res.result.content[0].text)
  assert.ok(payload.total > 5)
  for (const domain of ['data', 'alpha', 'ml', 'risk', 'execution', 'ecosystem']) {
    assert.ok(Array.isArray(payload.domains[domain]))
  }
  const firstClassNames = new Set(['query_quote', 'call_tool', 'list_tools'])
  for (const list of Object.values(payload.domains)) {
    for (const entry of list) {
      assert.ok(!firstClassNames.has(entry.name) || entry.kind === 'first-class')
    }
  }
})

test('notification（无 id）不产生响应；未知方法返回 -32601', async () => {
  const { server } = makeServer()
  assert.equal(await server.handleMessage({ jsonrpc: '2.0', method: 'notifications/initialized' }), null)
  const err = await server.handleMessage({ jsonrpc: '2.0', id: 9, method: 'nope' })
  assert.equal(err.error.code, -32601)
})

test('目录：域归类与写工具标记', () => {
  assert.equal(domainOf('rt_quote'), 'data')
  assert.equal(domainOf('ic'), 'alpha')
  assert.equal(domainOf('trade_place'), 'execution')
  assert.ok(WRITE_TOOLS.has('plan_execute'))
  const catalog = buildCatalog([...WB_TOOLS])
  const flat = Object.values(catalog).flat()
  assert.ok(flat.some((e) => e.name === 'query_quote' && e.kind === 'first-class'))
  assert.ok(flat.some((e) => e.name === 'trade_place' && e.kind === 'proxy'))
})
