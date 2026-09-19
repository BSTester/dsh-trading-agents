// OMS 台账（FR-EXEC-001/002/003）：把工作台的 frozen 计划订单登记为平台侧订单实体，
// 逐单做风控分级，并与工作台的真实状态对账（plan / orders_open / confirmation）。
//
// 边界（刻意设计）：本模块**没有任何下单方法**。执行入口只有一个——既有工作台的
// plan_execute + Web 确认（live 需口令）；V3 只登记、分级、对账、展示。
import { checkOrder, DEFAULT_LIMITS } from './risk.mjs'

const STAGE = {
  BLOCKED: 'blocked', // 触发红线：不可执行
  MANUAL: 'manual', // 超单笔阈值：需人工确认后由工作台执行
  RISK_PASSED: 'risk_passed', // 阈值内：等人工在工作台执行（唯一执行入口）
  SUBMITTED: 'submitted', // 工作台已报出（有 broker_order_id / 在途列表命中）
  FILLED: 'filled',
  REJECTED: 'rejected',
}

export function createOmsLedger({ store, wbCall, limits = DEFAULT_LIMITS }) {
  const orders = store.readJson('oms_orders.json') || {}

  function totalOf(group) {
    const candidates = [group?.cash?.total_asset, group?.cash?.mv, group?.cash?.balance]
    for (const candidate of candidates) {
      const value = Number(candidate)
      if (Number.isFinite(value) && value > 0) return value
    }
    return 0
  }

  async function workbenchContext() {
    const [equityEnvelope, fundsEnvelope, openEnvelope, confirmEnvelope] = await Promise.all([
      wbCall('equity', { window: 30 }),
      wbCall('account_funds', {}),
      wbCall('orders_open', {}),
      wbCall('confirmation', {}),
    ])

    // NAV 口径（安全优先）：① 本地 sim 台账 current（单币种，最安全）
    //   ② 仅当账户资金只有一个分组时用该账户 total_asset
    //   ③ 多账户多币种 → 不折算，NAV=0（风控保守退回人工确认，严禁偏宽松）
    let nav = 0
    let navSource = 'none'
    let drawdownPct = 0
    let drawdownSource = 'none'
    if (equityEnvelope?.ok) {
      const current = Number(equityEnvelope.value?.current)
      if (Number.isFinite(current) && current > 0) {
        nav = current
        navSource = 'sim-ledger(equity.current)'
      }
      const rawDd = Number(equityEnvelope.value?.max_drawdown)
      if (Number.isFinite(rawDd)) {
        drawdownPct = Math.abs(rawDd) <= 1 ? rawDd * 100 : rawDd
        drawdownSource = 'sim-ledger(max_drawdown)'
      }
    }
    if (nav === 0 && fundsEnvelope?.ok) {
      const groups = fundsEnvelope.value?.groups ?? []
      if (groups.length === 1) {
        nav = totalOf(groups[0])
        navSource = `account_funds(单账户 ${groups[0]?.acc_id ?? ''})`
      } else if (groups.length > 1) {
        navSource = `multi-account(${groups.length} 个账户/多币种) 不折算`
      }
    }

    const open = []
    if (openEnvelope?.ok) {
      for (const group of openEnvelope.value?.groups ?? []) {
        for (const row of group.rows ?? []) open.push(row)
      }
    }
    return {
      nav,
      navSource,
      drawdownPct,
      drawdownSource,
      openRows: open,
      confirmation: confirmEnvelope?.ok ? confirmEnvelope.value : null,
    }
  }

  function upsert(order, context, planMeta) {
    const id = String(order.client_order_id ?? `${planMeta.plan_id}-${order.symbol}-${order.side}`)
    const value = Number(order.qty ?? 0) * Number(order.price ?? 0)
    const verdict = checkOrder({ value }, { nav: context.nav, industryPct: 0, drawdownPct: context.drawdownPct }, limits)
    const openHit = context.openRows.some(
      (row) => String(row.client_order_id ?? '') === id || (row.symbol === order.symbol && row.side === order.side && Number(row.qty) === Number(order.qty)),
    )
    let stage = STAGE.RISK_PASSED
    if (verdict.action === 'blocked') stage = STAGE.BLOCKED
    else if (verdict.action === 'manual') stage = STAGE.MANUAL
    if (order.broker_order_id || openHit) stage = STAGE.SUBMITTED
    if (String(order.status ?? '') === 'filled') stage = STAGE.FILLED
    if (String(order.status ?? '') === 'rejected') stage = STAGE.REJECTED

    const existing = orders[id]
    const reasons = [...verdict.reasons]
    if (verdict.action === 'manual' && context.nav === 0) {
      reasons.push(`NAV 不可用（${context.navSource}）：不折算，保守退回人工确认`)
    }
    const record = {
      id,
      plan_id: planMeta.plan_id,
      plan_status: planMeta.status,
      mode: planMeta.mode,
      strategy_id: planMeta.strategy_id,
      ticker: order.symbol,
      side: order.side,
      qty: order.qty,
      price: order.price,
      value,
      broker_order_id: order.broker_order_id ?? null,
      stage,
      risk: { action: verdict.action, reasons },
      nav_used: context.nav,
      nav_source: context.navSource,
      drawdown_used: context.drawdownPct,
      drawdown_source: context.drawdownSource,
      first_seen_at: existing?.first_seen_at ?? new Date().toISOString(),
      updated_at: new Date().toISOString(),
      history: [...(existing?.history ?? []), { at: new Date().toISOString(), stage, reasons }].slice(-10),
    }
    orders[id] = record
    return record
  }

  async function sync() {
    const planEnvelope = await wbCall('plan', {})
    if (!planEnvelope?.ok) return { ok: false, error: planEnvelope?.error ?? { code: 'wb/error' } }
    const context = await workbenchContext()
    const plans = planEnvelope.value?.plans ?? []
    const seen = []
    for (const plan of plans) {
      for (const order of plan.orders ?? []) {
        seen.push(upsert(order, context, plan).id)
      }
    }
    store.writeJson('oms_orders.json', orders)
    const record = { at: new Date().toISOString(), plans: plans.length, orders: seen.length, nav: context.nav, drawdownPct: context.drawdownPct }
    store.append('oms_sync.jsonl', record)
    return { ok: true, ...record, stages: statusCounts() }
  }

  function statusCounts() {
    const counts = {}
    for (const record of Object.values(orders)) counts[record.stage] = (counts[record.stage] ?? 0) + 1
    return counts
  }

  function list() {
    return Object.values(orders).sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1))
  }

  function get(id) {
    return orders[id] ?? null
  }

  async function view() {
    const context = await workbenchContext()
    return {
      note: '执行入口只有一个：既有工作台 Web 的「执行已冻结计划」+ 人工确认（live 需口令）。V3 只登记、分级、对账与展示。',
      confirmation: context.confirmation,
      nav: context.nav,
      drawdownPct: context.drawdownPct,
      stages: statusCounts(),
      orders: list().slice(0, 20),
    }
  }

  return { sync, list, get, view, statusCounts, STAGE }
}

export default createOmsLedger
