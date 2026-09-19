// V3「系统概览」页数据绑定：设计稿 index.html 的 HTML/CSS 一字不动，
// 这里只把 /api/v3/* 的真实值写进对应节点，并把没有数据源的区块显式标注。
/* global window, document, location */
;(function () {
  const V3 = window.V3
  const { num, money, signed, stamp, esc } = V3

  const TITLES = { mcp: 'MCP Bridge', sdk: 'SDK JSON-RPC', headless: 'Headless CLI' }

  function cardByTitle(title) {
    for (const card of document.querySelectorAll('.chans .card')) {
      if ((card.querySelector('.ch-name')?.textContent ?? '').trim() === title) return card
    }
    return null
  }
  function statIn(card, label) {
    for (const stat of card?.querySelectorAll('.stat') ?? []) {
      if ((stat.querySelector('.k')?.textContent ?? '').trim().includes(label)) return stat
    }
    return null
  }
  function setStat(card, label, value, { unit = null, tone = null, relabel = null } = {}) {
    const stat = statIn(card, label)
    if (!stat) return false
    if (relabel) stat.querySelector('.k').textContent = relabel
    const v = stat.querySelector('.v')
    if (!v) return false
    v.className = `v num${tone ? ' ' + tone : ''}`
    if (unit === null) v.textContent = value
    else v.innerHTML = `${esc(value)} <small>${esc(unit)}</small>`
    return true
  }
  function setChip(card, text, kind) {
    const chip = card?.querySelector('.chip')
    if (!chip) return false
    chip.className = `chip ${kind ?? ''}`.trim()
    const dot = chip.querySelector('.dot')
    chip.textContent = text
    if (dot) chip.prepend(dot)
    return true
  }
  function setDot(el, ok) {
    if (!el) return
    el.className = `dot ${ok ? 'dg' : 'da'}`
  }

  function renderTopbar(overview) {
    const mode = (overview.mode ?? 'sim').toUpperCase()
    const env = document.querySelector('.tb-center .env')
    if (env) env.textContent = mode
    const equity = overview.equity ?? {}
    const points = Array.isArray(equity.points) ? equity.points : []
    const last = points.at(-1) ?? null
    const prev = points.at(-2) ?? null
    const daily = last && prev && Number(prev.equity) ? (Number(last.equity) / Number(prev.equity) - 1) * 100 : null
    const kvs = document.querySelectorAll('.tb-center .tb-kv')
    if (kvs[0]) kvs[0].querySelector('b').textContent = money(equity.current)
    if (kvs[1]) {
      const b = kvs[1].querySelector('b')
      b.textContent = daily === null ? '—' : signed(daily, 2, '%')
      b.className = `num ${daily === null ? '' : daily >= 0 ? 'pos' : 'neg'}`.trim()
    }
    // 设计稿这里是「示例数据」提示；真实运行下换成「真实数据」（不保留占位字样）
    const demo = document.querySelector('.pill-demo')
    if (demo) {
      demo.className = 'chip g'
      demo.innerHTML = '<i class="dot dg"></i>真实数据'
    }
    const beats = document.querySelectorAll('.beats .beat')
    const status = { mcp: overview.channels?.mcp?.status, sdk: overview.channels?.sdk?.status, headless: overview.channels?.headless?.status }
    beats.forEach((beat, index) => {
      const key = ['mcp', 'sdk', 'headless'][index]
      const ok = ['running', 'ready', 'ok'].includes(String(status[key]))
      setDot(beat.querySelector('i.dot'), ok)
      beat.title = `${TITLES[key]}：${status[key] ?? '未知'}`
    })
    const ts = document.getElementById('ts')
    if (ts) ts.textContent = stamp(overview.generated_at).slice(11, 19) || '—'
    const head = document.querySelector('.page-tools span')
    if (head) head.innerHTML = `数据截至 <b class="num">${esc(stamp(overview.generated_at) || '—')}</b>`
  }

  function renderKpis(overview) {
    const equity = overview.equity ?? {}
    const points = Array.isArray(equity.points) ? equity.points : []
    const last = points.at(-1) ?? null
    const prev = points.at(-2) ?? null
    const dailyPct = last && prev && Number(prev.equity) ? (Number(last.equity) / Number(prev.equity) - 1) * 100 : null
    const dailyAbs = last && prev ? Number(last.equity) - Number(prev.equity) : null
    const days = points.length > 1 ? points.length : 0
    const annualized = days > 0 && Number.isFinite(Number(equity.total_return))
      ? (Math.pow(1 + Number(equity.total_return), 252 / days) - 1) * 100 : null
    const dd = Number.isFinite(Number(equity.max_drawdown)) ? Math.abs(Number(equity.max_drawdown)) * 100 : null

    const map = {
      总资产: { value: money(equity.current), sub: dailyAbs === null ? '台账点位不足' : `较前值 ${signed(dailyAbs, 2)}` },
      当日盈亏: { value: dailyPct === null ? '—' : signed(dailyPct, 2, '%'), tone: dailyPct === null ? '' : dailyPct >= 0 ? 'pos' : 'neg', sub: prev ? `基准 ${prev.t}` : '无前值' },
      年化收益: { value: annualized === null ? '—' : signed(annualized, 2, '%'), tone: annualized === null ? '' : annualized >= 0 ? 'pos' : 'neg', sub: days ? `${days} 个交易日年化` : '台账点位不足' },
      夏普比率: { value: num(equity.sharpe), sub: `成交 ${equity.trades ?? '—'} 笔` },
      最大回撤: { value: dd === null ? '—' : `−${num(dd, 2)}%`, tone: 'warn', sub: '阈值 15%' },
    }
    for (const card of document.querySelectorAll('.kpis .kpi')) {
      const label = (card.querySelector('.kpi-label')?.textContent ?? '').trim()
      const spec = map[label]
      if (!spec) continue
      const value = card.querySelector('.kpi-value')
      value.textContent = spec.value
      value.className = `kpi-value num${spec.tone ? ' ' + spec.tone : ''}`
      const sub = card.querySelector('.kpi-sub')
      if (sub) sub.textContent = spec.sub
      // 迷你走势图：只有 ≥2 个真实点位才画；否则移除（设计稿里的 spark 是占位图形）
      const spark = card.querySelector('svg.spark')
      if (spark && points.length >= 2 && label !== '夏普比率') {
        V3.svgLine(card.querySelector('.kpi-value')?.parentElement?.querySelector('svg.spark') ?? null, points.map((p) => p.equity), { height: 34 })
      } else if (spark && points.length < 2) {
        spark.remove()
      }
    }
  }

  function renderChannels(overview, metrics) {
    const mcp = cardByTitle('MCP Bridge')
    if (mcp) {
      setStat(mcp, '已注册工具', metrics.toolTotal ?? '—', { unit: '个' })
      setStat(mcp, '今日调用', metrics.mcp?.calls ?? 0, { unit: '次' })
      const calls = Number(metrics.mcp?.calls ?? 0)
      const errors = Number(metrics.mcp?.errors ?? 0)
      setStat(mcp, '成功率', calls > 0 ? `${num(((calls - errors) / calls) * 100, 1)}%` : '—', { tone: errors === 0 ? 'pos' : 'warn' })
      setStat(mcp, 'P95 延迟', metrics.mcp?.avgMs ?? 0, { unit: 'ms', relabel: '平均延迟' })
      const foot = mcp.querySelector('.ch-foot')
      if (foot) foot.innerHTML = `状态机状态 <b class="mono">running</b>（工具面 ${metrics.toolTotal ?? '—'} 个 · workbench ${metrics.workbenchUp ? '在线' : '不可达'}）`
      setChip(mcp, '运行中', 'g')
    }
    const sdk = cardByTitle('SDK JSON-RPC')
    if (sdk) {
      // 平台服务未挂载 SDK 通道：如实标注，不留占位数字
      setChip(sdk, '无数据源', 'a')
      const stats = sdk.querySelector('.stats')
      if (stats) V3.nodata(stats, 'SDK 通道', overview.channels?.sdk?.reason ?? '本服务未挂载 SDK JSON-RPC 客户端')
      const foot = sdk.querySelector('.ch-foot')
      if (foot) foot.textContent = 'SDK 通道未挂载：会话与握手耗时无数据源'
    }
    const headless = cardByTitle('Headless CLI')
    if (headless) {
      setChip(headless, '无数据源', 'a')
      const stats = headless.querySelector('.stats')
      if (stats) V3.nodata(stats, 'Headless 通道', overview.channels?.headless?.reason ?? '本服务未挂载 Headless CLI 运行器')
      const foot = headless.querySelector('.ch-foot')
      if (foot) foot.textContent = '定时器 + 调度心跳由服务侧提供（见网关与调度页）'
    }
  }

  function renderTimeline(brain) {
    const list = document.querySelector('.tl')
    if (!list) return
    const items = []
    const run = brain.decision?.run ?? brain.decision ?? null
    if (run?.asOf) {
      items.push({ time: stamp(run.asOf).slice(11, 19), tag: ['流水线节点', 'b'], title: `研究流水线产出 ${(run.proposals ?? []).length} 条调仓建议`, chip: '中', act: '人工确认', dot: 'a' })
    }
    for (const entry of (brain.audit ?? []).slice(0, 4)) {
      items.push({ time: stamp(entry.at).slice(11, 19), tag: [entry.kind === 'signal' ? '信号' : entry.kind ?? '审计', 'c'], title: `${entry.ticker ?? ''} ${entry.detail ?? ''}`.trim(), chip: '低', act: '自动执行', dot: 'b' })
    }
    const template = list.querySelector('.tl-item')
    if (!template) return
    const clone = template.cloneNode(true)
    list.innerHTML = ''
    if (items.length === 0) {
      V3.nodata(list, '决策链路', '审计链与流水线均无记录')
      return
    }
    for (const item of items) {
      const node = clone.cloneNode(true)
      node.querySelector('.tl-time').textContent = item.time || '—'
      const dot = node.querySelector('.tl-dot')
      if (dot) dot.className = `tl-dot ${item.dot}`
      const tag = node.querySelector('.tag')
      if (tag) { tag.textContent = item.tag[0]; tag.className = `tag ${item.tag[1]}` }
      node.querySelector('.tl-title').textContent = item.title
      const chips = node.querySelectorAll('.chip')
      if (chips[0]) { chips[0].textContent = item.chip; chips[0].className = `chip ${item.chip === '低' ? 'g' : 'a'}` }
      if (chips[1]) { chips[1].textContent = item.act; chips[1].className = `chip ${item.act === '自动执行' ? 'b' : 'a'}` }
      list.appendChild(node)
    }
    const foot = document.querySelector('.tl-foot')
    if (foot) foot.innerHTML = `本链路 <b class="num">${items.length}</b> 节点 · 来源：工作台审计链 / 研究流水线`
    const chip = document.querySelector('.tl-card .card-h .chip')
    if (chip) chip.textContent = `最近 ${items.length} 条`
  }

  function renderRedlines(overview, orders) {
    const rows = document.querySelectorAll('.rb')
    const equity = overview.equity ?? {}
    const dd = Number.isFinite(Number(equity.max_drawdown)) ? Math.abs(Number(equity.max_drawdown)) * 100 : null
    const nav = Number(overview.equity?.current ?? 0)
    const maxSingle = (orders?.orders ?? []).reduce((max, order) => {
      const value = Number(order.value ?? 0)
      return nav > 0 ? Math.max(max, (value / nav) * 100) : max
    }, 0)
    const specs = [
      { label: '单笔交易上限', limit: 2, used: maxSingle > 0 ? maxSingle : null, why: '工作台订单台账未返回可比较的单笔金额' },
      { label: '单一行业暴露上限', limit: 20, used: null, why: '行业维度敞口工作台未提供' },
      { label: '最大回撤阈值', limit: 15, used: dd, why: '台账无回撤点位' },
    ]
    rows.forEach((row, index) => {
      const spec = specs[index]
      if (!spec) return
      const top = row.querySelector('.rb-top b')
      if (top) top.textContent = `${spec.limit}%`
      const shown = row.querySelector('.rb-top span.num')
      const fill = row.querySelector('.rb-fill')
      if (spec.used === null) {
        if (shown) shown.textContent = '无数据源'
        if (fill) { fill.className = 'rb-fill'; fill.style.width = '0%' }
        row.title = spec.why
        return
      }
      const ratio = Math.min(100, (spec.used / spec.limit) * 100)
      if (shown) shown.textContent = `当前 ${num(spec.used, 2)}%`
      if (fill) {
        fill.style.width = `${ratio.toFixed(1)}%`
        fill.className = `rb-fill ${ratio >= 100 ? 'bad' : ratio >= 70 ? 'mid' : 'ok'}`
      }
    })
  }

  function renderAgentLoop(brain, metrics) {
    const grid = document.querySelector('.al-grid')
    if (!grid) return
    const stats = grid.querySelectorAll('.stat')
    // 当前 turn / 已用 token / 模型 / 推理强度：平台服务没有 SDK 会话 → 无数据源
    const sdkMissing = '本服务未挂载 SDK 会话'
    if (stats[0]) V3.replaceWith(stats[0], `<div class="k">当前 turn</div><div class="v num">无数据源</div>`)
    if (stats[1]) V3.replaceWith(stats[1], `<div class="k">已用 token</div><div class="v num">无数据源</div>`)
    if (stats[2]) V3.replaceWith(stats[2], `<div class="k">模型</div><div class="v mono">无数据源</div>`)
    if (stats[3]) V3.replaceWith(stats[3], `<div class="k">推理强度</div><div class="v">无数据源</div>`)
    const hint = document.querySelector('.al-tools-h span:last-child')
    if (hint) hint.textContent = `当前进程累计 · ${sdkMissing}`
    const tools = [...Object.entries(metrics.mcp?.tools ?? {}), ...Object.entries(metrics.wb?.byTool ?? {}).map(([name, count]) => [`wb:${name}`, count])]
      .sort((a, b) => b[1] - a[1]).slice(0, 5)
    // 设计稿里 .tool 与 .al-tools-h 都是 .al-grid 的兄弟节点（同在 Agent Loop 卡片内）
    const toolNodes = document.querySelectorAll('.tool')
    const max = tools.length ? tools[0][1] : 1
    toolNodes.forEach((node, index) => {
      const item = tools[index]
      if (!item) { node.remove(); return }
      node.querySelector('.tname').textContent = item[0]
      node.querySelector('.tcount').textContent = item[1]
      const bar = node.querySelector('.tool-bar i')
      if (bar) bar.style.width = `${Math.max(4, (item[1] / max) * 100).toFixed(1)}%`
    })
  }

  function renderApprovals(orders) {
    const manual = (orders?.orders ?? []).filter((order) => order.stage === 'manual')
    const chip = document.querySelector('.sec-row .chip')
    if (chip) chip.textContent = `${manual.length} 条待处理`
    const cards = document.querySelectorAll('.appr .appr-card')
    cards.forEach((card, index) => {
      const order = manual[index]
      if (!order) { card.remove(); return }
      card.querySelector('.appr-title').innerHTML = `${esc(order.ticker)} ${esc(order.side)} <span class="num">${esc(order.qty)}</span> 股 · 金额 <span class="num">${esc(money(order.value))}</span>`
      card.querySelector('.appr-desc').textContent = `来源：工作台计划 ${order.plan_id ?? '—'} · 风控：${(order.risk?.reasons ?? ['阈值内']).join('；')}`
    })
    if (manual.length === 0) {
      const wrap = document.querySelector('.appr')
      if (wrap) V3.nodata(wrap, '待人工审批', '当前台账没有 manual 阶段订单')
    }
  }

  function renderSources(settings, metrics, overview) {
    const stats = document.querySelectorAll('.ds .stat')
    const futu = settings.futu ?? {}
    const envs = Object.fromEntries((settings.env ?? []).map((item) => [item.key, item]))
    const rows = [
      { name: '富途 OpenD/OpenAPI', ok: Boolean(futu.channel), detail: `渠道 ${futu.channel ?? '—'}${futu.mcp_bearer?.present ? ` · Bearer 至 ${futu.mcp_bearer.expiry ?? '—'}` : ''}` },
      { name: 'Tushare Pro', ok: Boolean(envs.TUSHARE_TOKEN?.injected), detail: envs.TUSHARE_TOKEN?.injected ? '已注入' : '未配置（可在接入与授权页填写）' },
      { name: 'AKShare', ok: true, detail: '公开端点（新闻可用；全市场快照上游不可达）' },
      { name: 'SEC EDGAR', ok: true, detail: '公开 XBRL 财报' },
    ]
    stats.forEach((stat, index) => {
      const spec = rows[index]
      if (!spec) { stat.remove(); return }
      const k = stat.querySelector('.k')
      if (k) k.innerHTML = `<i class="dot ${spec.ok ? 'dg' : 'da'}"></i>${esc(spec.name)}`
      const v = stat.querySelector('.v')
      if (v) v.innerHTML = `<span class="ds-st ${spec.ok ? 'ok' : 'fb'}">${spec.ok ? '正常' : '未配置'}</span>`
      stat.title = spec.detail
    })
    const aux = document.querySelector('.ds')?.parentElement?.querySelector('.card-h .aux')
    if (aux) aux.textContent = `workbench ${metrics.workbenchUp ? '在线' : '不可达'} · 工具 ${metrics.toolTotal ?? '—'} 个 · 数据时点 ${stamp(overview.generated_at)}`
  }

  function renderFooter() {
    const foot = document.querySelector('footer.foot')
    if (!foot) return
    foot.innerHTML = '<span>数据来源：本服务 /api/v3/* 实时接口（工作台工具面 / 富途行情 / 台账）· 取不到的项显式标注「无数据源」，页面不含占位数字</span><span>量化决策平台 V3.0 · Harness-Centric</span>'
  }

  function wireRefresh(reload) {
    const btn = document.getElementById('btnRefresh')
    if (!btn || btn.dataset.bound === 'v3') return
    btn.dataset.bound = 'v3'
    const clone = btn.cloneNode(true)
    btn.replaceWith(clone)
    clone.addEventListener('click', () => { clone.textContent = '刷新中…'; reload().finally(() => { clone.textContent = '刷新'; }) })
  }

  async function render() {
    const [overview, metrics, brain, settings, orders] = await Promise.all([
      V3.api('overview'), V3.api('metrics'), V3.api('brain'), V3.api('settings'), V3.api('oms/orders'),
    ])
    if (overview.ok) { renderTopbar(overview); renderKpis(overview) } else { V3.nodata(document.querySelector('.kpis'), 'KPI', overview.error?.message) }
    if (metrics.ok) renderChannels(overview, metrics)
    if (brain.ok) renderTimeline(brain)
    if (overview.ok) renderRedlines(overview, orders.ok ? orders : null)
    if (metrics.ok) renderAgentLoop(brain.ok ? brain : {}, metrics)
    renderApprovals(orders.ok ? orders : null)
    if (settings.ok && metrics.ok) renderSources(settings, metrics, overview.ok ? overview : {})
    renderFooter()
    wireRefresh(render)
  }

  render().catch((error) => {
    const main = document.querySelector('.inner')
    if (main) V3.nodata(main, '页面数据', String(error?.message ?? error))
  })
})()
