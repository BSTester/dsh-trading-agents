// OMS 台账测试：登记工作台 frozen 计划订单、逐单风控分级、与工作台状态对账。
// 关键断言：任何阶段都不会调用下单类工具（trade_place/trade_modify/trade_cancel）。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import createOmsLedger from '../server/oms.mjs'
import createStore from '../server/store.mjs'

function makeWb({ nav = 1_000_000, drawdownPct = -6, openRows = [], pending = null, orders = [], multiAccount = false } = {}) {
  const calls = []
  const wbCall = async (tool) => {
    calls.push(tool)
    if (tool === 'plan') return { ok: true, value: { plans: [{ plan_id: 'PLN-1', status: 'frozen', mode: 'SIM', strategy_id: 'momentum_value_top5', orders }] } }
    if (tool === 'equity') return { ok: true, value: { mode: 'sim', current: nav, max_drawdown: drawdownPct / 100 } }
    if (tool === 'account_funds') {
      const groups = multiAccount
        ? [{ acc_id: '9393', cash: { total_asset: String(nav / 2) } }, { acc_id: '3182575', cash: { total_asset: String(nav / 2) } }]
        : [{ acc_id: '3182575', cash: { total_asset: String(nav) } }]
      return { ok: true, value: { mode: 'sim', groups } }
    }
    if (tool === 'orders_open') return { ok: true, value: { groups: [{ rows: openRows }] } }
    if (tool === 'confirmation') return { ok: true, value: { pending, ttl_ms: 120000 } }
    return { ok: false, error: { code: 'unexpected', message: tool } }
  }
  return { wbCall, calls }
}

test('阈值内订单 → risk_passed；超单笔 → manual；红线 → blocked', async () => {
  const orders = [
    { client_order_id: 'o-small', symbol: 'SH.600000', side: 'BUY', qty: 1000, price: 9.07, status: 'draft' }, // 9070 ≈ 0.9%
    { client_order_id: 'o-big', symbol: 'SH.600009', side: 'BUY', qty: 5000, price: 100, status: 'draft' }, // 500k = 50%
  ]
  const { wbCall } = makeWb({ orders })
  const store = createStore('/tmp/quant-v3-oms-test')
  const ledger = createOmsLedger({ store, wbCall })
  const result = await ledger.sync()
  assert.equal(result.ok, true)
  assert.equal(result.orders, 2)
  assert.equal(ledger.get('o-small').stage, 'risk_passed')
  assert.equal(ledger.get('o-big').stage, 'manual')
  assert.match(ledger.get('o-big').risk.reasons[0], /单笔占比/)

  // 红线：回撤 15.1% 触发阻断
  const blockedWb = makeWb({ drawdownPct: -15.1, orders })
  const ledger2 = createOmsLedger({ store: createStore('/tmp/quant-v3-oms-test2'), wbCall: blockedWb.wbCall })
  await ledger2.sync()
  assert.equal(ledger2.get('o-small').stage, 'blocked')
  assert.equal(ledger2.get('o-big').stage, 'blocked')
})

test('对账：在途列表命中 → submitted；确认通道待处理状态可见', async () => {
  const orders = [{ client_order_id: 'o-small', symbol: 'SH.600000', side: 'BUY', qty: 1000, price: 9.07, status: 'draft' }]
  const { wbCall } = makeWb({ orders, openRows: [{ client_order_id: 'o-small', symbol: 'SH.600000', side: 'BUY', qty: 1000 }], pending: { client_order_id: 'o-small' } })
  const ledger = createOmsLedger({ store: createStore('/tmp/quant-v3-oms-test3'), wbCall })
  await ledger.sync()
  assert.equal(ledger.get('o-small').stage, 'submitted')
  const view = await ledger.view()
  assert.ok(view.confirmation.pending)
  assert.equal(view.confirmation.ttl_ms, 120000)
  assert.match(view.note, /执行入口只有一个/)
})

test('边界：台账从不调用下单类工具', async () => {
  const orders = [{ client_order_id: 'o1', symbol: 'SH.600000', side: 'BUY', qty: 100, price: 9.07, status: 'draft' }]
  const { wbCall, calls } = makeWb({ orders })
  const ledger = createOmsLedger({ store: createStore('/tmp/quant-v3-oms-test4'), wbCall })
  await ledger.sync()
  await ledger.view()
  for (const tool of calls) {
    assert.ok(!['trade_place', 'trade_modify', 'trade_cancel', 'plan_execute'].includes(tool), `不应调用 ${tool}`)
  }
  assert.equal(typeof ledger.placeOrder, 'undefined')
})

test('缺少账户资金时不误判：风控退回人工确认', async () => {
  const orders = [{ client_order_id: 'o1', symbol: 'SH.600000', side: 'BUY', qty: 100, price: 9.07, status: 'draft' }]
  const { wbCall } = makeWb({ nav: 0, orders })
  const ledger = createOmsLedger({ store: createStore('/tmp/quant-v3-oms-test5'), wbCall })
  await ledger.sync()
  assert.equal(ledger.get('o1').stage, 'manual')
  assert.match(ledger.get('o1').risk.reasons[0], /缺少市值或订单金额/)
})
