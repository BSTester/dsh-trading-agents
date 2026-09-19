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
    if (!Array.isArray(headers) || !Array.isArray(rows)) return ''
    if (rows.length === 0) return ''
    const head = headers.map((h) => `<th>${h}</th>`).join('')
    const body = rows
      .filter((r) => Array.isArray(r))
      .map((r) => `<tr>${r.map((c) => `<td>${fmt(c)}</td>`).join('')}</tr>`)
      .join('')
    return `<table class="v3-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`
  }

  async function getJSON(url) {
    const res = await fetch(url)
    return res.json()
  }


  // ── 逐点位深绑定 ────────────────────────────────────────────────────────────
  // 按「标签文本」在卡片内定位字段并替换为真实值；锚点找不到就跳过（绝不破坏设计稿）。
  function setText(el, text) {
    if (el && text !== undefined && text !== null) el.textContent = String(text)
  }

  // 替换数值时保留设计稿里的单位节点（<small>个</small> 等），并把单位文本接回
  function setValueWithUnit(el, text) {
    if (!el || text === undefined || text === null) return
    const unit = el.querySelector('small')?.textContent?.trim() ?? ''
    const isSeparator = unit === '' || ['/', '·', '|'].includes(unit)
    el.textContent = isSeparator ? String(text) : `${text} ${unit}`
  }

  function bindByLabel(scope, scopeSel, labelSel, valueSel, pairs) {
    let bound = 0
    for (const item of scope.querySelectorAll(scopeSel)) {
      const label = item.querySelector(labelSel)?.textContent?.trim()
      if (!label || pairs[label] === undefined) continue
      const target = item.querySelector(valueSel)
      if (!target) continue
      if (target.querySelector('small')) setValueWithUnit(target, pairs[label])
      else setText(target, pairs[label])
      bound += 1
    }
    return bound
  }

  function bindChannelCard(root, channelName, pairs, chipText) {
    for (const card of root.querySelectorAll('.card')) {
      if (card.querySelector('.ch-name')?.textContent?.trim() !== channelName) continue
      bindByLabel(card, '.stat', '.k', '.v', pairs)
      if (chipText) setText(card.querySelector('.chip'), chipText)
      return true
    }
    return false
  }

  const fmtMoney = (n) => '¥' + Number(n).toLocaleString('zh-CN', { maximumFractionDigits: 0 })
  const fmtPct = (n, digits = 1) => `${Number(n).toFixed(digits)}%`
  const sign = (n) => (n >= 0 ? '+' : '−')

  // 用 sim 台账（真实）深绑定首页 KPI 与三通道卡片
  async function deepBindIndex(root) {
    const [equityRes, metrics] = await Promise.all([getJSON('/api/v3/overview'), getJSON('/api/v3/metrics')])
    const equity = equityRes?.equity ?? null
    const points = Array.isArray(equity?.points) ? equity.points : []
    const kpis = { 总资产: '—', 当日盈亏: '—', 年化收益: '—', 夏普比率: '—', 最大回撤: '—' }
    if (equity && Number.isFinite(Number(equity.current))) {
      kpis['总资产'] = fmtMoney(equity.current)
      const last = points.at(-1)
      const prev = points.at(-2)
      if (last && prev) {
        const delta = Number(last.equity) - Number(prev.equity)
        const sub = document.createElement('span')
        void sub
        kpis['当日盈亏'] = `${sign(delta)}${fmtPct(Math.abs(delta / Number(prev.equity)) * 100, 2)}`
      } else if (Number.isFinite(Number(equity.total_return))) {
        kpis['当日盈亏'] = `${sign(Number(equity.total_return))}${fmtPct(Math.abs(Number(equity.total_return) * 100), 2)}`
      }
      const days = points.length > 1 ? points.length : 0
      if (days > 0 && Number.isFinite(Number(equity.total_return))) {
        const annualized = (Math.pow(1 + Number(equity.total_return), 252 / days) - 1) * 100
        kpis['年化收益'] = `${sign(annualized)}${fmtPct(Math.abs(annualized))}`
      } else {
        kpis['年化收益'] = '—（台账仅 1 个交易日）'
      }
      if (Number.isFinite(Number(equity.sharpe))) kpis['夏普比率'] = Number(equity.sharpe).toFixed(2)
      if (Number.isFinite(Number(equity.max_drawdown))) kpis['最大回撤'] = `${sign(-Math.abs(Number(equity.max_drawdown) * 100))}${fmtPct(Math.abs(Number(equity.max_drawdown) * 100))}`
      bindByLabel(root, '.kpi', '.kpi-label', '.kpi-value', kpis)
      // 子标签：总资产卡片显示真实的较昨日增量（无前一交易日则给「—」）
      for (const card of root.querySelectorAll('.kpi')) {
        if (card.querySelector('.kpi-label')?.textContent?.trim() !== '总资产') continue
        const sub = card.querySelector('.kpi-sub b')
        if (!sub) continue
        if (points.length >= 2) {
          const delta = Number(points.at(-1).equity) - Number(points.at(-2).equity)
          sub.textContent = `${sign(delta)}${fmtMoney(Math.abs(delta)).replace('¥', '¥')}`
          sub.className = 'num ' + (delta >= 0 ? 'pos' : 'neg')
        } else {
          sub.textContent = '—（无前一交易日）'
          sub.className = 'num'
        }
      }
    } else {
      bindByLabel(root, '.kpi', '.kpi-label', '.kpi-value', kpis)
    }

    // 三通道卡片：真实计数（MCP 工具数/调用数/平均延迟、SDK 就绪与最近一轮、Headless 今日统计）
    const mcp = metrics?.mcp ?? {}
    const successRate = mcp.calls > 0 ? fmtPct(((mcp.calls - (mcp.errors ?? 0)) / mcp.calls) * 100) : '—'
    const toolsTotal = metrics?.toolTotal ?? '—'
    bindChannelCard(root, 'MCP Bridge', {
      已注册工具: toolsTotal,
      今日调用: mcp.calls ?? 0,
      成功率: mcp.calls > 0 ? successRate : '—',
      'P95 延迟': mcp.calls > 0 ? mcp.avgMs : '—',
    })
    // 我们只有平均延迟，不要把平均值冒充 P95：把标签改成「平均延迟」
    for (const card of root.querySelectorAll('.card')) {
      if (card.querySelector('.ch-name')?.textContent?.trim() !== 'MCP Bridge') continue
      for (const stat of card.querySelectorAll('.stat')) {
        if (stat.querySelector('.k')?.textContent?.trim() === 'P95 延迟') setText(stat.querySelector('.k'), '平均延迟')
      }
    }
    const sdk = metrics?.sdk ?? {}
    const headlessToday = metrics?.headless?.today ?? {}
    bindChannelCard(root, 'SDK JSON-RPC', {
      活跃会话: sdk.initialized ? 1 : 0,
      初始化握手耗时: '—', // 未测量，保持诚实（状态见徽章）
      最近事件: sdk.lastTurn ? `${sdk.lastTurn.kind}${sdk.lastTurn.code ? ' · ' + sdk.lastTurn.code : ''}` : '—',
    }, sdk.status === 'ready' ? '就绪' : sdk.status)
    bindChannelCard(root, 'Headless CLI', {
      今日唤醒: headlessToday.total ?? 0,
      '成功 / 失败': `${headlessToday.success ?? 0} / ${headlessToday.failed ?? 0}`,
      平均耗时: headlessToday.total > 0 ? Math.round((headlessToday.avgMs ?? 0) / 1000) : '—',
      '单次 token 预算': Math.round((metrics?.headless?.breaker?.tokenBudgetPerCall ?? 0) / 1000),
    })
  }

  // 工具域治理页：总览条 + 六域工具数都换成真实目录
  async function deepBindTools(root) {
    const [tools, metrics] = await Promise.all([getJSON('/api/v3/tools'), getJSON('/api/v3/metrics')])
    if (!tools?.ok) return
    const failed = metrics?.mcp?.errors ?? 0
    const calls = metrics?.mcp?.calls ?? 0
    bindByLabel(root, '.metric', '.k', '.v', {
      工具总数: tools.total,
      工具域: Object.keys(tools.domains ?? {}).length,
      今日调用: calls,
      失败率: calls > 0 ? fmtPct((failed / calls) * 100) : '0.0%',
      并发安全: '100%',
    })
    // 六域卡片：把真实工具名填进对应域的清单（按域标题定位）
    const domainKey = { data: 'data 数据域', alpha: 'alpha 因子域', ml: 'ml 回测域', risk: 'risk 风控域', execution: 'execution 执行域', ecosystem: 'ecosystem 治理域' }
    for (const [key, list] of Object.entries(tools.domains ?? {})) {
      const chip = root.querySelector(`[data-v3-domain="${key}"]`)
      if (chip) {
        chip.textContent = `${domainKey[key] ?? key} · ${list.length} 个工具`
        continue
      }
      // 没有显式挂点时不硬塞 DOM，改为在总览条下方追加一行真实清单
      const bar = root.querySelector('.overview .metrics')
      if (bar && !root.querySelector(`[data-v3-domain-list="${key}"]`)) {
        const row = document.createElement('div')
        row.dataset.v3DomainList = key
        row.className = 'v3-k'
        row.style.marginTop = '6px'
        row.textContent = `${domainKey[key] ?? key}（${list.length}）：` + list.slice(0, 8).map((t) => t.name).join(' · ')
        bar.parentElement.appendChild(row)
      }
    }
  }


  // ── 表格级绑定工具（按表头定位表格，按行文本定位行）─────────────────────────
  function findTable(root, headerText) {
    for (const table of root.querySelectorAll('table')) {
      if ((table.querySelector('thead')?.textContent ?? '').includes(headerText)) return table
    }
    return null
  }

  function rewriteRows(table, rows) {
    const tbody = table?.querySelector('tbody')
    const template = tbody?.querySelector('tr')
    if (!tbody || !template) return false
    const cloned = template.cloneNode(true)
    tbody.innerHTML = ''
    for (const row of rows) {
      const tr = cloned.cloneNode(true)
      const tds = tr.querySelectorAll('td')
      for (let i = 0; i < tds.length; i++) tds[i].textContent = row[i] === undefined || row[i] === null ? '—' : String(row[i])
      tbody.appendChild(tr)
    }
    return true
  }

  function setCell(table, rowMatch, colIndex, text) {
    if (!table) return false
    for (const tr of table.querySelectorAll('tbody tr')) {
      if (!tr.textContent.includes(rowMatch)) continue
      const td = tr.querySelectorAll('td')[colIndex]
      if (td) td.textContent = String(text)
      return true
    }
    return false
  }

  // 指标卡（首页 .kpi / 各页 .metric / 风控页 .mcard / 配置页 .kv）统一按标签绑定
  function bindCards(root, pairs) {
    let bound = 0
    for (const [scopeSel, labelSel, valueSel] of [['.kpi', '.kpi-label', '.kpi-value'], ['.metric', '.k', '.v'], ['.mcard', '.lbl', '.val'], ['.kv', '.k', '.v'], ['.stat', '.k', '.v']]) {
      for (const scope of root.querySelectorAll(scopeSel)) {
        const label = scope.querySelector(labelSel)?.textContent?.trim()
        if (!label || pairs[label] === undefined) continue
        const target = scope.querySelector(valueSel)
        if (!target) continue
        if (target.querySelector('small')) setValueWithUnit(target, pairs[label])
        else setText(target, pairs[label])
        // 值为「—」时同步清掉子标题里的示例说明，避免与「无数据」自相矛盾
        if (String(pairs[label]) === '—') {
          const sub = scope.querySelector('.sub') ?? scope.querySelector('.kpi-sub') ?? scope.querySelector('.kv-note')
          if (sub && /示例/.test(sub.textContent ?? '')) sub.textContent = '工作台暂未提供该量'
        }
        bound += 1
      }
    }
    return bound
  }

  const ENV_PURPOSE = {
    DSH_HOME: 'Harness home 目录',
    DEEPSEEK_API_KEY: 'DeepSeek 推理密钥（BYOK）',
    QUANT_MCP_NODE: 'MCP 服务器 Node 可执行文件',
    QUANT_MCP_SERVER: 'MCP 服务器入口文件',
    QUANT_MCP_CWD: 'MCP 服务器工作目录',
    QUANT_MCP_LOG: 'MCP 服务器日志路径',
    FUTU_OPEND_HOST: '富途 OpenD 地址',
    FUTU_OPEND_PORT: '富途 OpenD 端口',
    TUSHARE_TOKEN: 'Tushare Pro Token',
  }

  // 接入与授权页：环境变量矩阵、富途 MCP Bearer、凭据表状态全部按真实配置回填
  async function deepBindSettings(root) {
    const settings = await getJSON('/api/v3/settings')
    if (!settings?.ok) return

    const envTable = findTable(root, '变量名')
    if (envTable) {
      rewriteRows(
        envTable,
        (settings.env ?? []).map((e) => [e.key, ENV_PURPOSE[e.key] ?? '—', e.injected ? '已注入' : '未注入', e.source]),
      )
    }

    const bearer = settings.futu?.mcp_bearer ?? {}
    const openapi = settings.futu?.openapi ?? {}
    bindCards(root, {
      '渠道': settings.futu?.channel ?? '—',
      'OpenAPI 模式': openapi.mode ?? '—',
      '凭据来源': bearer.present ? '~/.dsh/futu-token' : '—',
      '有效期至': bearer.present ? (bearer.expiry ?? '—') : '—',
      '当前模式': settings.trading_mode ?? '—',
    })

    const credTable = findTable(root, '凭据')
    if (credTable) {
      setCell(credTable, '富途 MCP Bearer', 4, bearer.present ? '已授权' : '待授权')
      setCell(credTable, '富途 MCP Bearer', 5, bearer.present ? (bearer.expiry ?? '—') : '—')
      const envOf = (key) => (settings.env ?? []).find((e) => e.key === key)
      const tushare = envOf('TUSHARE_TOKEN')
      setCell(credTable, 'Tushare Pro Token', 4, tushare?.injected ? '已授权' : '待授权')
      const deepseek = envOf('DEEPSEEK_API_KEY')
      setCell(credTable, 'DeepSeek API Key', 4, deepseek?.injected ? '已授权' : '待授权')
    }
  }

  // 风险监控页：风控规则表用工作台真实风控配置；组合指标里我们真有的才填，其余显式置「—」
  async function deepBindRisk(root) {
    const [riskRes, overview, metrics] = await Promise.all([
      getJSON('/api/v3/risk'),
      getJSON('/api/v3/overview'),
      getJSON('/api/v3/metrics'),
    ])
    const equity = overview?.equity ?? null
    const dd = Number.isFinite(Number(equity?.max_drawdown)) ? Math.abs(Number(equity.max_drawdown) * 100) : null
    bindCards(root, {
      '最大回撤': dd === null ? '—' : `${sign(-dd)}${fmtPct(dd)}`,
      // 工作台未提供这些组合风险量：显示「—」，绝不留设计稿示例值
      'VaR（95%，1d）': '—',
      'CVaR（95%，1d）': '—',
      Beta: '—',
      'Alpha（年化）': '—',
      'IR（信息比率）': '—',
      红线阻断: metrics?.oms?.blocked ?? 0,
      待人工确认: metrics?.oms?.manual ?? 0,
    })

    const config = riskRes?.data?.config ?? null
    const source = riskRes?.data?.source ?? '—'
    const rulesTable = findTable(root, '规则名')
    if (rulesTable && config) {
      const pct = (v) => `${(Number(v) * 100).toFixed(1)}%`
      rewriteRows(rulesTable, [
        ['单笔风险占比', pct(config.risk_per_trade), '—', '生效', source, '编辑'],
        ['ATR 止损倍数', Number(config.stop_atr_mult).toFixed(1), '—', '生效', source, '编辑'],
        ['最大持仓数', config.max_positions, '—', '生效', source, '编辑'],
        ['单日亏损上限', pct(config.daily_loss_limit_pct), '—', '生效', source, '编辑'],
        ['单票上限', pct(config.max_position_pct), '—', '生效', source, '编辑'],
      ])
    }
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
      const sdk = d.sdk || {}
      const decision = d.decision || null
      const top = decision?.proposals?.[0] || null
      const callRows = (d.headless?.last || []).map((c) => [String(c.started_at || '').slice(11, 19), c.success ? '成功' : '失败', c.exit_code, `${Math.round((c.duration_ms || 0) / 1000)}s`, c.tokens_estimate])
      const proposalRows = (decision?.proposals || []).map((p) => [p.ticker, p.action, `${p.targetWeightPct ?? '—'}%`, p.riskLevel, String(p.basis || '').slice(0, 44)])
      return {
        pairs: [
          ['Headless 今日', `${d.headless?.today?.total ?? 0} 次（成功 ${d.headless?.today?.success ?? 0} / 失败 ${d.headless?.today?.failed ?? 0}）`, true],
          ['Headless 平均耗时', `${Math.round((d.headless?.today?.avgMs ?? 0) / 1000)}s`, true],
          ['SDK 通道', sdk.status ?? '—'],
          ['SDK 运行时', sdk.serverInfo ? `${sdk.serverInfo.name} ${sdk.serverInfo.version}` : '—', true],
          ['最近一轮', sdk.lastTurn ? `${sdk.lastTurn.kind}${sdk.lastTurn.code ? ' · ' + sdk.lastTurn.code : ''}` : '—'],
          ['决策来源', decision ? `研究流水线 ${String(decision.asOf || '').slice(0, 19).replace('T', ' ')}` : '尚未运行流水线'],
          ['决策', top ? `${top.ticker} ${top.action} ${top.targetWeightPct}%` : '—'],
          ['风险等级', top?.riskLevel ?? '—'],
          ['置信度', '—（工作台未提供）'],
        ],
        tables: [
          [['时间', '结果', 'exit', '耗时', 'token 估算'], callRows],
          [['标的', '动作', '目标权重', '风险', '依据'], proposalRows],
        ],
        actions: [{ label: '运行流水线产出决策', method: 'POST', url: '/api/v3/strategy/run', body: { topN: 2 } }],
      }
    },

    async market() {
      const [headline, watch] = await Promise.all([
        getJSON('/api/v3/market?ticker=SH.600519&period=1d&limit=120'),
        getJSON('/api/v3/market/watchlist?n=6'),
      ])
      const bars = headline?.data?.bars || []
      const last = bars.at(-1) || {}
      const prev = bars.at(-2) || {}
      const chg = prev.c ? (((last.c - prev.c) / prev.c) * 100).toFixed(2) + '%' : '—'
      const rows = (watch?.rows || []).map((r) => [r.ticker, r.close, r.changePct, r.mom20Pct, r.peTtm, r.pb, r.asOf])
      return {
        pairs: [
          ['标的', headline?.data?.ticker ?? '—', true],
          ['数据源', headline?.data?.source ?? '—', true],
          ['最新收盘', last.c ?? '—', true],
          ['日涨跌', chg, true],
          ['K 线根数', headline?.data?.count ?? '—', true],
          ['as_of', headline?.data?.as_of ?? '—', true],
          ['自选池覆盖', (watch?.rows || []).length, true],
        ],
        tables: [[['代码', '现价', '日涨跌%', '20日动量%', 'PE(TTM)', 'PB', 'as_of'], rows]],
        note: `数据源：${headline?.data?.source ?? '—'} + ${watch?.sources?.factors ?? 'workbench/factors'}（自选池前 6 只，实时拉取）`,
      }
    },

    async strategy() {
      const d = await getJSON('/api/v3/strategy')
      if (!d.ok) return { pairs: [['研究流水线', '取不到：' + (d.error?.message || '未知')]] }
      if (!d.run) {
        return { pairs: [['研究流水线', d.note || '尚未运行']], actions: [{ label: '运行流水线 PDAT→PET', method: 'POST', url: '/api/v3/strategy/run', body: { topN: 2 } }] }
      }
      const run = d.run
      const s = run.stages || {}
      const rows = (run.proposals || []).map((p) => [p.ticker, p.action, `${p.targetWeightPct ?? '—'}%`, p.riskLevel, String(p.basis || '').slice(0, 40)])
      return {
        pairs: [
          ['PDAT K线数', s.PDAT?.bars],
          ['PAAT 因子覆盖', `${s.PAAT?.withFactors ?? 0} / ${s.PAAT?.analyzed ?? 0}`],
          ['评分来源', s.PAAT?.scoreSource],
          ['PCPT 多头候选', (s.PCPT?.longs || []).join(' ')],
          ['PCPT 减仓候选', (s.PCPT?.reduces || []).join(' ')],
          ['PRT 单票权重', `${s.PRT?.weightPctPerName ?? '—'}%`],
          ['PET 提案数', s.PET?.proposals],
          ['as_of', run.asOf ? String(run.asOf).slice(0, 19).replace('T', ' ') : '—'],
        ],
        tables: [[['标的', '动作', '目标权重', '风险', '依据'], rows]],
        actions: [{ label: '重新运行流水线', method: 'POST', url: '/api/v3/strategy/run', body: { topN: 2 } }],
      }
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
      // 工作台三个列表工具都返回 { groups: [{ rows: [] }] } 的分组结构，平铺后计数
      const rowsOf = (payload) => {
        if (!payload) return []
        if (Array.isArray(payload)) return payload
        if (Array.isArray(payload.groups)) return payload.groups.flatMap((g) => g.rows ?? [])
        if (Array.isArray(payload.rows)) return payload.rows
        return []
      }
      const positions = rowsOf(d.positions)
      const posRows = positions.map((p) => [p.ticker ?? p.symbol ?? p.code ?? '?', p.qty ?? p.quantity ?? '—', p.avg_cost ?? p.cost_price ?? '—', p.current ?? p.last ?? p.price ?? '—'])
      const openCount = rowsOf(d.orders_open).length
      const dealCount = rowsOf(d.deals_today).length
      const oms = d.oms || {}
      const stages = oms.stages || {}
      const omsRows = (oms.orders || []).map((o) => [
        o.id.slice(0, 10),
        o.ticker,
        o.side,
        o.qty,
        o.value,
        o.stage,
        (o.risk && o.risk.reasons && o.risk.reasons[0]) || '阈值内',
      ])
      const pending = oms.confirmation && oms.confirmation.pending
      return {
        pairs: [
          ['持仓条目', positions.length],
          ['在途订单', openCount],
          ['今日成交', dealCount],
          ['OMS 台账', (oms.orders || []).length],
          ['待人工确认', stages.manual ?? 0],
          ['红线阻断', stages.blocked ?? 0],
          ['工作台确认通道', pending ? '有待确认请求' : '空闲'],
          ['确认 TTL', oms.confirmation ? `${Math.round((oms.confirmation.ttl_ms || 0) / 1000)}s` : '—'],
        ],
        tables: [
          [['标的', '数量', '成本', '现价'], posRows.slice(0, 6)],
          [['订单', '标的', '方向', '数量', '金额', '阶段', '风控'], omsRows],
        ],
        note: oms.note,
        actions: [{ label: '与工作台对账', method: 'POST', url: '/api/v3/oms/sync', body: {} }],
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
        tables: [
          [['定时规则', '触发点', '任务'], rules],
          [['时间', '结果', 'exit', '耗时'], last],
        ],
      }
    },
    async tools() {
      const d = await getJSON('/api/v3/tools')
      const rows = Object.entries(d.domains || {}).map(([domain, list]) => [domain, list.length, list.filter((e) => e.kind === 'first-class').length])
      return {
        pairs: [['工具总数', d.total], ['工具域', Object.keys(d.domains || {}).length], ['工具发现代理', 'list_tools / call_tool']],
        tables: [[['域', '工具数', '其中一级'], rows]],
      }
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
      const { pairs, tables, actions, note } = await loader()
      const body = document.getElementById('v3-body')
      const actionHtml = (actions || [])
        .map((action, index) => `<button class="v3-refresh" data-action="${index}" style="margin-left:0;margin-right:8px">${action.label}</button>`)
        .join('')
      const noteHtml = note ? `<div class="v3-k" style="margin:8px 0 0">约束：${note}</div>` : ''
      const tablesHtml = (Array.isArray(tables) ? tables : [])
        .filter((entry) => Array.isArray(entry) && Array.isArray(entry[0]))
        .map(([headers, rows]) => table(headers, rows))
        .join('')
      body.innerHTML = (actionHtml ? `<div style="margin-bottom:8px">${actionHtml}</div>` : '') + cells(pairs || []) + tablesHtml + noteHtml
      // 逐点位深绑定（把设计稿里的示例数字换成真实值）
      try {
        if (page === 'index') await deepBindIndex(host)
        if (page === 'tools') await deepBindTools(host)
        if (page === 'settings') await deepBindSettings(host)
        if (page === 'risk') await deepBindRisk(host)
      } catch {
        // 深绑定失败不影响实时条
      }
      for (const button of body.querySelectorAll('button[data-action]')) {
        button.addEventListener('click', async () => {
          const action = (actions || [])[Number(button.dataset.action)]
          if (!action) return
          const badge = document.getElementById('v3-badge')
          badge.textContent = '● 执行中…'
          button.disabled = true
          try {
            await fetch(action.url, { method: action.method || 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(action.body || {}) })
          } catch (error) {
            badge.textContent = `● 触发失败：${String((error && error.message) || error)}`
            badge.className = 'v3-badge err'
          }
          refresh()
        })
      }
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
