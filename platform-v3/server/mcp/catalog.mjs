// 六大工具域目录（FR-TOOLS-001/002/003）。
// 设计：既有 workbench 已有 77 个工具（数据/因子/风控/执行/治理全覆盖），V3.0 不复制其
// schema，而是按规格做「工具发现代理」：对外只暴露 list_tools / call_tool 两个入口 +
// 少量一级高频工具；call_tool 将调用转发到 workbench 对应工具（交易写类仍走其 Web 确认闸门）。
// 每个条目：{ name, domain, wb, desc, args }，args 为参数摘要（供 list_tools 展示）。

export const DOMAINS = ['data', 'alpha', 'ml', 'risk', 'execution', 'ecosystem']

export const FIRST_CLASS = [
  { name: 'query_quote', domain: 'data', wb: 'series', desc: 'K 线序列（富途优先、降级如实标注）', args: { ticker: 'str 必填，如 SH.600519', period: 'period 默认 5m', limit: 'int 20..2000' } },
  { name: 'market_snapshot', domain: 'data', wb: 'market_snapshot', desc: '批量市场快照', args: { stocks: 'str[] 必填' } },
  { name: 'query_order_book', domain: 'data', wb: 'rt_order_book', desc: '盘口深度', args: { ticker: 'str 必填' } },
  { name: 'query_capital_flow', domain: 'data', wb: 'capital_flow', desc: '资金流向', args: { ticker: 'str 必填' } },
  { name: 'query_financial', domain: 'data', wb: 'f10_detail', desc: '个股 F10（财务/公司资料）', args: { ticker: 'str 必填' } },
  { name: 'stock_screen', domain: 'data', wb: 'stock_screen', desc: '条件选股', args: { fields: '见 workbench 契约' } },
  { name: 'eval_factor_ic', domain: 'alpha', wb: 'ic', desc: 'IC/IR 分析、分层回测', args: { factor: 'str', universe: 'str?' } },
  { name: 'list_factors', domain: 'alpha', wb: 'factors', desc: '因子库当前值', args: {} },
  { name: 'factor_sensitivity', domain: 'alpha', wb: 'sensitivity', desc: '因子敏感性', args: { factor: 'str' } },
  { name: 'sentiment_history', domain: 'alpha', wb: 'sentiment_history', desc: '情绪因子历史', args: { ticker: 'str?', window: 'int?' } },
  { name: 'calc_indicator', domain: 'ml', wb: 'sensitivity', desc: '技术/统计指标敏感性（ ML 域先以统计量落位，回测引擎随 strategy-svc 迭代）', args: { factor: 'str' } },
  { name: 'check_risk', domain: 'risk', wb: 'risk', desc: '组合风险视图（回撤/集中度）', args: { mode: 'mode?' } },
  { name: 'query_position', domain: 'execution', wb: 'positions', desc: '券商真实持仓（按账户小计）', args: { mode: 'mode?' } },
  { name: 'account_funds', domain: 'execution', wb: 'account_funds', desc: '账户资金', args: { mode: 'mode?' } },
  { name: 'orders_open', domain: 'execution', wb: 'orders_open', desc: '在途订单', args: {} },
  { name: 'deals_today', domain: 'execution', wb: 'deals_today', desc: '今日成交', args: {} },
  { name: 'request_approval', domain: 'ecosystem', wb: 'confirmation', desc: '审批确认状态查询', args: { id: 'str?' } },
  { name: 'audit_trail', domain: 'ecosystem', wb: 'audit', desc: '审计链查询', args: { window: 'int?' } },
  { name: 'research_tasks_claim', domain: 'ecosystem', wb: 'research_tasks_claim', desc: '研究任务队列领取', args: {} },
]

// 交易写类：不进一级工具；经 call_tool 转发时命中 WRITE_TOOLS 会被标记，且其真正闸门
// 在 workbench 侧（Web 确认卡 / TTL）。V3.0 不绕过、不复制这道边界。
export const WRITE_TOOLS = new Set(['trade_place', 'trade_modify', 'trade_cancel', 'plan_execute', 'switch_mode'])

export function domainOf(wbName) {
  if (WRITE_TOOLS.has(wbName)) return 'execution'
  if (/^(series|rt_|market_snapshot|cur_kline|quote_history|capital_|plate_|stock_screen|info_|watchlist|f10|derivative|ipo_|economic_|short_|option_)/.test(wbName)) return 'data'
  if (/^(factors|ic|sensitivity|correlation|sentiment_|factors_history|quality)/.test(wbName)) return 'alpha'
  if (/^(risk|snapshot|equity)/.test(wbName)) return 'risk'
  if (/^(positions|account_|orders_|deals_|trade_max_qty|push_)/.test(wbName)) return 'execution'
  return 'ecosystem'
}

// list_tools 的目录：一级工具 + workbench 77 工具的域归类（名称即契约入口，参数经 call_tool 直传）。
// 已被一级工具覆盖的 wb 工具不再重复列出。
export function buildCatalog(wbToolNames) {
  const byDomain = Object.fromEntries(DOMAINS.map((d) => [d, []]))
  const covered = new Set()
  for (const tool of FIRST_CLASS) {
    byDomain[tool.domain].push({ name: tool.name, kind: 'first-class', wb: tool.wb, desc: tool.desc, args: tool.args })
    covered.add(tool.wb)
  }
  for (const name of wbToolNames) {
    if (covered.has(name)) continue
    byDomain[domainOf(name)].push({ name, kind: 'proxy', wb: name, desc: 'workbench 工具直通（参数见 workbench 契约）', args: { '*': '透传' } })
  }
  return byDomain
}
