// V3.0 控制台实时数据层：按页把 /api/v3/* 的真实数据渲染成「实时数据条」。
// 设计稿中的示例区块保留不动；本层只插入一个带来源与 as_of 的实时面板（数据诚实：取不到就报取不到）。
/* global location, document, fetch */
;(async function () {
  const page = (location.pathname.split('/').pop() || 'index.html').replace(/\.html$/, '')

  const style = document.createElement('style')
  style.textContent = `
.v3-live{margin:0 0 14px;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px 16px}
.v3-live .v3-head{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.v3-live .v3-title{font-size:12.5px;font-weight:700;letter-spacing:.4px}
.v3-live .v3-badge{display:inline-flex;align-items:center;gap:5px;padding:1px 8px;border-radius:10px;font-size:10.5px;border:1px solid rgba(63,185,80,.45);color:var(--green)}
.v3-live .v3-badge.err{border-color:rgba(248,81,77,.45);color:var(--red)}
.v3-live .v3-asof{margin-left:auto;font-size:10.5px;color:var(--faint)}
.v3-live .v3-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}
.v3-live .v3-cell{background:var(--panel2);border:1px solid var(--border);border-radius:6px;padding:8px 10px}
.v3-live .v3-k{font-size:10.5px;color:var(--faint);margin-bottom:3px}
.v3-live .v3-v{font-size:13px;font-weight:600;color:var(--text)}
.v3-live .v3-v.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.v3-live .v3-table{width:100%;border-collapse:collapse;font-size:11.5px;margin-top:8px}
.v3-live .v3-table th{color:var(--faint);font-weight:500;text-align:left;padding:4px 8px;border-bottom:1px solid var(--border2);font-size:10.5px}
.v3-live .v3-table td{padding:4px 8px;border-bottom:1px solid var(--border);color:var(--muted)}
.v3-live .v3-refresh{margin-left:8px;padding:2px 10px;border-radius:5px;border:1px solid var(--border2);background:transparent;color:var(--muted);font-size:11px;cursor:pointer}
.v3-live .v3-refresh:hover{color:var(--text);border-color:var(--blue)}
`
  document.head.appendChild(style)

  function fmt(value) {
    if (value === null || value === undefined || value === '') return '—'
    if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2)
    if (typeof value === 'boolean') return value ? '是' : '否'
    return String(value)
  }

  function cells(pairs) {
    return `<div class="v3-grid">${pairs
      .map(([k, v, mono]) => `<div class="v3-cell"><div class="v3-k">${k}</div><div class="v3-v${mono ? ' mono' : ''}">${fmt(v)}</div></div>`)
      .join('')}</div>`
  }

  function table(headers, rows) {
    if (!rows || rows.length === 0) return ''
    const head = headers.map((h) => `<th>${h}</th>`).join('')
    const body = rows.map((r) => `<tr>${r.map((c) => `<td>${fmt(c)}</td>`).join('')}</tr>`).join('')
    return `<table class="v3-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
  }

  async function getJSON(url) {
    const res = await fetch(url)
    return res.json()
  }

  // 每页的数据映射：load() → { pairs, tables }（全部来自 /api/v3/* 真实接口）
  const RENDER = {
    async index() {
      const d = await getJSON('/api/v3/overview')
      const s = d.snapshot || {}
      const head = d.headless || {}
      return {
        pairs: [
          ['账户模式（工作台回读）', s.mode ?? '—'],
          ['研究 run', (s.runs ?? []).length],
          ['研报', (s.reports ?? []).length],
          ['Headless 今日 成功/失败', `${head.today?.success ?? 0} / ${head.today?.failed ?? 0}`, true],
          ['MCP 通道', d.channels?.mcp?.status],
          ['SDK 通道', d.channels?.sdk?.status],
          ['风控红线 单笔/行业/回撤', `${d.limits?.singlePct}% / ${d.limits?.industryPct}% / ${d.limits?.drawdownPct}%`, true],
        ],
      }
    },
    async brain() {
      const d = await getJSON('/api/v3/brain')
      const rows = (d.last || []).map((c) => [String(c.started_at || '').slice(11, 19), c.success ? '成功' : '失败', c.exit_code, `${Math.round((c.duration_ms || 0) / 1000)}s`, c.tokens_estimate])
      return { pairs: [['今日调用', d.today?.total], ['成功', d.today?.success], ['失败', d.today?.failed], ['平均耗时', `${Math.round((d.today?.avgMs || 0) / 1000)}s`, true]], tables: [['时间', '结果', 'exit', '耗时', 'token 估算'], rows] }
    },
    async market() {
      const d = await getJSON('/api/v3/market?ticker=SH.600519&period=1d&limit=120')
      if (!d.ok) return { pairs: [['行情', '取不到：' + (d.error?.message || '未知')]] }
      const bars = d.data?.bars || []
      const last = bars.at(-1) || {}
      const prev = bars.at(-2) || {}
      const chg = prev.c ? (((last.c - prev.c) / prev.c) * 100).toFixed(2) + '%' : '—'
      return {
        pairs: [
          ['标的', d.data?.ticker, true],
          ['数据源', d.data?.source, true],
          ['最新收盘', last.c, true],
          ['日涨跌', chg, true],
          ['K 线根数', d.data?.count, true],
          ['as_of', d.data?.as_of, true],
        ],
      }
    },
    async strategy() {
      const d = await getJSON('/api/v3/strategy')
      if (!d.ok) return { pairs: [['因子库', '取不到：' + (d.error?.message || '未知')]] }
      const factors = Array.isArray(d.data) ? d.data : d.data?.factors ?? d.data?.items ?? []
      const rows = (Array.isArray(factors) ? factors : []).slice(0, 8).map((f) => [f.name ?? f.factor ?? '?', f.value ?? f.score ?? '—'])
      return { pairs: [['因子条目', Array.isArray(factors) ? factors.length : Object.keys(factors).length]], tables: [['因子', '数值'], rows] }
    },
    async risk() {
      const d = await getJSON('/api/v3/risk')
      if (!d.ok) return { pairs: [['风控视图', '取不到：' + (d.error?.message || '未知')]] }
      const flat = []
      const walk = (obj, prefix) => {
        for (const [k, v] of Object.entries(obj || {})) {
          if (v && typeof v === 'object' && !Array.isArray(v)) walk(v, prefix ? `${prefix}.${k}` : k)
          else flat.push([prefix ? `${prefix}.${k}` : k, v, typeof v === 'number'])
        }
      }
      walk(d.data, '')
      return { pairs: flat.slice(0, 8) }
    },
    async execution() {
      const d = await getJSON('/api/v3/execution')
      const positions = d.positions
      const posRows = Array.isArray(positions) ? positions.map((p) => [p.ticker ?? p.code ?? '?', p.qty ?? p.quantity ?? '—', p.avg_cost ?? '—', p.current ?? p.last ?? '—']) : []
      return {
        pairs: [
          ['持仓条目', Array.isArray(positions) ? positions.length : '—'],
          ['在途订单', Array.isArray(d.orders_open) ? d.orders_open.length : '—'],
          ['今日成交', Array.isArray(d.deals_today) ? d.deals_today.length : '—'],
          ['OMS 台账', (d.oms || []).length],
        ],
        tables: [['标的', '数量', '成本', '现价'], posRows.slice(0, 8)],
      }
    },
    async gateway() {
      const d = await getJSON('/api/v3/gateway')
      const rules = (d.scheduler?.rules || []).map((r) => [r.id, r.at, r.task])
      const last = (d.headless?.last || []).map((c) => [String(c.started_at || '').slice(11, 19), c.success ? '成功' : '失败', c.exit_code, `${Math.round((c.duration_ms || 0) / 1000)}s`])
      return {
        pairs: [
          ['MCP', d.channels?.mcp?.status],
          ['SDK', d.channels?.sdk?.status],
          ['Headless', d.channels?.headless?.status],
          ['熔断并发 占用/上限', `${d.headless?.breaker?.active ?? 0} / ${d.headless?.breaker?.concurrencyLimit ?? 3}`, true],
          ['单次超时', `${Math.round((d.headless?.breaker?.timeoutMs || 0) / 1000)}s`, true],
        ],
        tables: [['定时规则', '触发点', '任务'], rules, ['时间', '结果', 'exit', '耗时'], last],
      }
    },
    async tools() {
      const d = await getJSON('/api/v3/tools')
      const rows = Object.entries(d.domains || {}).map(([domain, list]) => [domain, list.length, list.filter((e) => e.kind === 'first-class').length])
      return { pairs: [['工具总数', d.total], ['工具域', Object.keys(d.domains || {}).length]], tables: [['域', '工具数', '其中一级'], rows] }
    },
    async settings() {
      const d = await getJSON('/api/v3/settings')
      const rows = (d.env || []).map((e) => [e.key, e.injected ? '已注入' : '未注入', e.source])
      return {
        pairs: [
          ['交易模式', d.trading_mode],
          ['富途通道', d.futu?.channel, true],
          ['MCP Bearer', d.futu?.mcp_bearer?.present ? `有效 · 至 ${d.futu.mcp_bearer.expiry}` : '缺失', true],
          ['OpenAPI 模式', d.futu?.openapi?.mode ?? '—', true],
        ],
        tables: [['环境变量', '状态', '来源'], rows],
      }
    },
  }

  const loader = RENDER[page]
  if (!loader) return

  const host =
    document.querySelector('.main-inner') ||
    document.querySelector('.main') ||
    document.querySelector('.page') ||
    document.querySelector('.wrap') ||
    document.querySelector('.inner')
  if (!host) return

  const panel = document.createElement('div')
  panel.className = 'v3-live'
  host.insertBefore(panel, host.firstChild)

  async function refresh() {
    const asOf = new Date().toISOString().replace('T', ' ').slice(0, 19)
    panel.innerHTML = `<div class="v3-head"><span class="v3-title">实时数据 · ${page}</span><span class="v3-badge" id="v3-badge">● LIVE</span><button class="v3-refresh" id="v3-refresh">刷新</button><span class="v3-asof">来源 /api/v3/* · ${asOf} · 真实数据</span></div><div id="v3-body"><div class="v3-k">加载中…</div></div>`
    document.getElementById('v3-refresh').addEventListener('click', refresh)
    try {
      const { pairs, tables } = await loader()
      const body = document.getElementById('v3-body')
      body.innerHTML = cells(pairs || []) + (tables || []).map(([headers, rows]) => table(headers, rows)).join('')
    } catch (error) {
      const badge = document.getElementById('v3-badge')
      badge.textContent = '● 接口异常'
      badge.className = 'v3-badge err'
      document.getElementById('v3-body').innerHTML = `<div class="v3-k">取不到实时数据：${String((error && error.message) || error)}</div>`
    }
  }

  refresh()
  setInterval(refresh, 30000)
})()
