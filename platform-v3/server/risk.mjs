// 风控（FR-EXEC-003）与 OMS 生命周期（FR-EXEC-001/002）。
// 分级审批：阈值内=自动执行；超单笔阈值=人工确认；触发红线（行业超限/回撤超限）=强制阻断。
// 重要边界：V3.0 的执行落点永远是既有 workbench 的 trade_place（Web 确认卡闸门），
// 本模块不做任何直接券商对接，也不提供绕过路径。

export const DEFAULT_LIMITS = { singlePct: 2, industryPct: 20, drawdownPct: 15 }

export function checkOrder(order, context, limits = DEFAULT_LIMITS) {
  const reasons = []
  let action = 'auto'

  const nav = Number(context?.nav ?? 0)
  const value = Number(order?.value ?? 0)
  if (nav > 0 && value > 0) {
    const singlePct = (value / nav) * 100
    if (singlePct > limits.singlePct) {
      action = 'manual'
      reasons.push(`单笔占比 ${singlePct.toFixed(2)}% > ${limits.singlePct}%，需人工确认`)
    }
  } else {
    action = 'manual'
    reasons.push('缺少市值或订单金额，无法自动判定')
  }

  const industryPct = Number(context?.industryPct ?? 0)
  if (industryPct > limits.industryPct) {
    action = 'blocked'
    reasons.push(`行业集中度 ${industryPct.toFixed(1)}% > ${limits.industryPct}% 红线，强制阻断`)
  }

  const drawdownPct = Math.abs(Number(context?.drawdownPct ?? 0))
  if (drawdownPct >= limits.drawdownPct) {
    action = 'blocked'
    reasons.push(`回撤 ${drawdownPct.toFixed(1)}% 触及 ${limits.drawdownPct}% 红线，强制阻断`)
  }

  return { action, reasons }
}

// OMS 状态机：created → risk_checked → (auto|manual|blocked) → submitted → (filled|partial|rejected)
// manual 需要 workbench 侧确认卡通过后才可 submit；blocked 是终态。
export function createOms({ store }) {
  const orders = store.readJson('oms_orders.json') || {}

  function create(order, check) {
    const id = `OMS-${Date.now()}-${Math.floor(Math.random() * 1e4)}`
    orders[id] = {
      id,
      created_at: new Date().toISOString(),
      order,
      trace_id: `trace-${id.toLowerCase()}`,
      stage: 'risk_checked',
      action: check.action,
      reasons: check.reasons,
      events: [
        { at: new Date().toISOString(), event: 'created' },
        { at: new Date().toISOString(), event: 'risk_checked', detail: check.reasons.join('；') || '阈值内' },
      ],
    }
    store.writeJson('oms_orders.json', orders)
    return orders[id]
  }

  function transition(id, stage, detail) {
    const record = orders[id]
    if (!record) return null
    record.stage = stage
    record.events.push({ at: new Date().toISOString(), event: stage, detail: detail ?? '' })
    store.writeJson('oms_orders.json', orders)
    return record
  }

  function list() {
    return Object.values(orders).sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
  }

  function get(id) {
    return orders[id] ?? null
  }

  return { create, transition, list, get }
}

export default { checkOrder, createOms }
