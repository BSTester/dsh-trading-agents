// 量化交易决策平台 V3.0 —— 控制台渲染层
// 设计原则：页面不含任何静态数据。所有数值在运行时从 /api/v3/* 取真实值；
// 取不到的显式显示「—」并说明原因，绝不用占位数字顶替。
/* global location, document, fetch, setInterval */
;(function () {
  const PAGES = [
    { id: 'index', file: 'index.html', title: '系统概览', group: '监控', sub: '平台（身体）× Harness（决策大脑）· 全局运行状态' },
    { id: 'brain', file: 'brain.html', title: '决策大脑', group: '监控', sub: 'Harness Agent 会话 · 推理与工具调用 · 决策结论' },
    { id: 'market', file: 'market.html', title: '行情与信号', group: '研究', sub: '真实行情快照 · 多因子 · 数据源健康' },
    { id: 'strategy', file: 'strategy.html', title: '策略与因子', group: '研究', sub: 'PDAT→PET 研究流水线 · 回测与参数扫描' },
    { id: 'risk', file: 'risk.html', title: '风险监控', group: '交易', sub: '工作台风控配置 · 台账风险量 · 订单分级' },
    { id: 'execution', file: 'execution.html', title: '执行与审批', group: '交易', sub: '订单台账 · 风控分级 · 工作台确认边界' },
    { id: 'gateway', file: 'gateway.html', title: '网关与调度', group: '系统', sub: 'MCP / SDK / Headless 三通道 · 调度与熔断' },
    { id: 'tools', file: 'tools.html', title: '工具域治理', group: '系统', sub: '六大工具域 · 发现代理 · 调用统计' },
    { id: 'settings', file: 'settings.html', title: '接入与授权', group: '系统', sub: '交易模式 · 数据源与凭据状态 · 环境变量' },
  ]
  const GROUPS = ['监控', '研究', '交易', '系统']

  const esc = (v) => String(v ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
  const dash = (v) => (v === null || v === undefined || v === '' ? '—' : v)
  const num = (v, digits = 2) => (Number.isFinite(Number(v)) ? Number(v).toFixed(digits) : '—')
  const money = (v) => (Number.isFinite(Number(v)) ? '¥' + Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—')
  const pct = (v, digits = 2) => (Number.isFinite(Number(v)) ? `${Number(v).toFixed(digits)}%` : '—')
  const signed = (v, digits = 2, suffix = '') => (Number.isFinite(Number(v)) ? `${Number(v) >= 0 ? '+' : '−'}${Math.abs(Number(v)).toFixed(digits)}${suffix}` : '—')
  const stamp = (v) => (v ? String(v).replace('T', ' ').slice(0, 19) : '—')
  const hhmmss = (v) => (v ? String(v).slice(11, 19) : '—')

  async function api(path, options) {
    try {
      const res = await fetch(path, options)
      return await res.json()
    } catch (error) {
      return { ok: false, error: { code: 'net', message: String((error && error.message) || error) } }
    }
  }
  const post = (path, body) => api(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body ?? {}) })

  const card = (title, body, right = '') => `<section class="card"><h3>${esc(title)}${right ? `<span class="r">${right}</span>` : ''}</h3>${body}</section>`

  function table(headers, rows, empty = '暂无数据') {
    if (!Array.isArray(rows) || rows.length === 0) return `<div class="note">${esc(empty)}</div>`
    const head = headers.map((h) => `<th>${esc(h)}</th>`).join('')
    const body = rows.map((r) => `<tr>${r.map((c) => `<td>${c === null || c === undefined ? '—' : c}</td>`).join('')}</tr>`).join('')
    return `<div class="scroll-x"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`
  }

  function kpis(items) {
    return `<section class="card"><div class="grid auto">${items
      .map((i) => {
        const value = i[1] === null || i[1] === undefined || i[1] === '' ? '—' : i[1]
        return `<div class="kpi"><div class="k">${esc(i[0])}</div><div class="v ${i[3] ?? ''}">${value}</div>${i[2] ? `<div class="s">${i[2]}</div>` : ''}</div>`
      })
      .join('')}</div></section>`
  }

  const errBox = (label, error) => `<div class="err">${esc(label)}取不到：${esc(error?.message ?? error?.code ?? '未知错误')}</div>`
  const tag = (text, kind = '') => `<span class="tag ${kind}">${esc(text)}</span>`

  function lineChart(values, { height = 170, color = 'var(--blue)' } = {}) {
    const nums = (values || []).map(Number).filter(Number.isFinite)
    if (nums.length < 2) return ''
    const width = 760
    const min = Math.min(...nums)
    const max = Math.max(...nums)
    const span = max - min || 1
    const x = (i) => (i / (nums.length - 1)) * (width - 8) + 4
    const y = (v) => height - 16 - ((v - min) / span) * (height - 34)
    const d = nums.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
    const area = `${d} L${x(nums.length - 1).toFixed(1)},${height - 16} L4,${height - 16} Z`
    return `<svg viewBox="0 0 ${width} ${height}" style="width:100%;height:${height}px" role="img">
      <path d="${area}" fill="rgba(76,141,255,.10)"></path>
      <path d="${d}" fill="none" stroke="${color}" stroke-width="1.5"></path>
      <text x="6" y="14" fill="var(--faint)" font-size="10">${max.toFixed(2)}</text>
      <text x="6" y="${height - 4}" fill="var(--faint)" font-size="10">${min.toFixed(2)}</text></svg>`
  }

  // 真实 K 线：蜡烛 + 成交量（对齐设计稿行情页的图表形态）
  function candleChart(bars, { height = 210, volumeRatio = 0.24 } = {}) {
    const rows = (bars || []).filter((b) => [b.o, b.h, b.l, b.c].every((v) => Number.isFinite(Number(v))))
    if (rows.length < 2) return ''
    const width = 760
    const priceH = height * (1 - volumeRatio) - 12
    const highs = rows.map((b) => Number(b.h))
    const lows = rows.map((b) => Number(b.l))
    const max = Math.max(...highs)
    const min = Math.min(...lows)
    const span = max - min || 1
    const step = (width - 8) / rows.length
    const bodyW = Math.max(2, Math.min(9, step * 0.6))
    const y = (v) => 8 + (1 - (v - min) / span) * priceH
    const vols = rows.map((b) => Number(b.v ?? b.vol ?? 0))
    const vMax = Math.max(...vols, 1)
    let candles = ''
    let volume = ''
    rows.forEach((b, i) => {
      const o = Number(b.o)
      const c = Number(b.c)
      const up = c >= o
      const color = up ? 'var(--green)' : 'var(--red)'
      const cx = 4 + i * step + step / 2
      candles += `<line x1="${cx.toFixed(1)}" y1="${y(Number(b.h)).toFixed(1)}" x2="${cx.toFixed(1)}" y2="${y(Number(b.l)).toFixed(1)}" stroke="${color}" stroke-width="1"></line>`
      const top = y(Math.max(o, c))
      const bottom = y(Math.min(o, c))
      candles += `<rect x="${(cx - bodyW / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bodyW.toFixed(1)}" height="${Math.max(1, bottom - top).toFixed(1)}" fill="${up ? 'rgba(63,185,80,.85)' : 'rgba(248,81,77,.85)'}"></rect>`
      const vh = Math.max(1, (vols[i] / vMax) * (height * volumeRatio - 8))
      volume += `<rect x="${(cx - bodyW / 2).toFixed(1)}" y="${(height - vh).toFixed(1)}" width="${bodyW.toFixed(1)}" height="${vh.toFixed(1)}" fill="${up ? 'rgba(63,185,80,.35)' : 'rgba(248,81,77,.35)'}"></rect>`
    })
    return `<svg viewBox="0 0 ${width} ${height}" style="width:100%;height:${height}px" role="img">${candles}${volume}
      <text x="6" y="12" fill="var(--faint)" font-size="10">${max.toFixed(2)}</text>
      <text x="6" y="${(8 + priceH).toFixed(1)}" fill="var(--faint)" font-size="10">${min.toFixed(2)}</text></svg>`
  }

  // 参数热力图（真实回测网格：夏普色阶）
  function heatmap(grid, windows, rebalances) {
    if (!Array.isArray(grid) || grid.length === 0) return ''
    const values = grid.map((g) => Number(g.sharpe)).filter(Number.isFinite)
    if (values.length === 0) return ''
    const max = Math.max(...values)
    const min = Math.min(...values)
    const span = max - min || 1
    const cell = (w, r) => {
      const hit = grid.find((g) => g.window === w && g.rebalanceDays === r)
      if (!hit || !Number.isFinite(Number(hit.sharpe))) return `<div class="cell" style="background:var(--panel2);color:var(--faint)">—</div>`
      const t = (Number(hit.sharpe) - min) / span
      const color = `rgba(${Math.round(248 - 185 * t)},${Math.round(81 + 104 * t)},${Math.round(77 + 6 * t)},${(0.25 + 0.6 * t).toFixed(2)})`
      return `<div class="cell" style="background:${color};color:var(--text)" title="window=${w} rebalance=${r} sharpe=${hit.sharpe}">${Number(hit.sharpe).toFixed(2)}</div>`
    }
    return `<div class="heat" style="grid-template-columns: 84px repeat(${windows.length}, minmax(0,1fr))">
      <div class="hd"></div>${windows.map((w) => `<div class="hd">w=${w}</div>`).join('')}
      ${rebalances.map((r) => `<div class="hd">${r}日</div>${windows.map((w) => cell(w, r)).join('')}`).join('')}
    </div>`
  }

  const steps = (items) => `<div class="steps">${items.map((i) => `<div class="step ${i.state ?? ''}"><span class="n">${i.n}</span>${esc(i.label)}</div>`).join('')}</div>`

  const kanban = (cols) => `<div class="kanban">${cols.map((c) => `<div class="kcol"><div class="kh"><span>${esc(c.name)}</span><b class="num">${c.count ?? 0}</b></div>${(c.items ?? []).slice(0, 4).map((t) => `<div class="kc">${esc(t)}</div>`).join('')}</div>`).join('')}</div>`

  function bars(entries, color = 'var(--purple)') {
    const rows = (entries || []).filter(([, v]) => Number.isFinite(Number(v)))
    if (rows.length === 0) return '<div class="note">暂无计数</div>'
    const max = Math.max(...rows.map(([, v]) => Number(v))) || 1
    return rows
      .map(
        ([label, value]) =>
          `<div class="bar"><span class="lbl">${esc(label)}</span><span class="track"><span class="fill" style="width:${((Number(value) / max) * 100).toFixed(1)}%;background:${color}"></span></span><span class="val">${value}</span></div>`,
      )
      .join('')
  }

  const nodata = (what, why) => `<div class="nodata"><b>${esc(what)}：无数据源</b>${why ? ' · ' + esc(why) : ''}</div>`

  function timeline(items) {
    if (!items || items.length === 0) return nodata('事件时间线', '暂无已记录的平台事件')
    return `<div class="tl">${items
      .map(
        (i) =>
          `<div class="tl-item ${i.kind ?? ''}"><div class="tl-time">${esc(i.time ?? '—')}</div><div class="tl-title">${esc(i.title ?? '')}</div><div class="tl-body">${esc(i.body ?? '')}</div></div>`,
      )
      .join('')}</div>${items.some((i) => i.foot) ? `<div class="tl-foot">${esc(items.find((i) => i.foot)?.foot ?? '')}</div>` : ''}`
  }

  function health(items) {
    return `<div class="health">${items
      .map(
        (i) =>
          `<div class="hcard"><div class="ht"><b>${esc(i.name)}</b>${tag(i.status, i.kind ?? '')}</div><div class="hd">${esc(i.detail ?? '')}</div></div>`,
      )
      .join('')}</div>`
  }

  function gauge(label, usedPct, limitPct, color = 'var(--blue)') {
    const used = Number(usedPct)
    const limit = Number(limitPct)
    const ratio = Number.isFinite(used) && Number.isFinite(limit) && limit > 0 ? Math.min(100, (used / limit) * 100) : 0
    const shown = Number.isFinite(used) ? `${used}%` : '无数据源'
    return `<div class="gauge"><div class="gk"><span>${esc(label)}</span><span class="num">${shown} / 阈值 ${limit}%</span></div><div class="gt"><span class="gf" style="width:${ratio.toFixed(1)}%;background:${color}"></span></div></div>`
  }

  const channelTag = (status) => tag(status ?? '—', status === 'running' || status === 'ready' ? 'ok' : status === 'disabled' ? '' : 'warn')

  function shell(page, overview) {
    const mode = overview?.snapshot?.mode ?? null
    const badge = mode
      ? `<span class="badge ${mode === 'live' ? 'b-live' : 'b-sim'}"><span class="dot ok"></span>${mode === 'live' ? 'LIVE 实盘' : 'SIM 模拟盘'}</span>`
      : '<span class="badge b-sim"><span class="dot"></span>模式未知</span>'
    const ch = overview?.channels ?? {}
    const pill = (label, status) => `<span class="pill"><span class="dot ${status === 'running' || status === 'ready' ? 'ok' : status === 'disabled' ? '' : 'warn'}"></span>${label} ${esc(status ?? '—')}</span>`
    const nav = GROUPS.map(
      (group) =>
        `<div class="grp">${group}</div>` +
        PAGES.filter((p) => p.group === group)
          .map((p) => `<a href="${p.file}" class="${p.id === page.id ? 'active' : ''}">${p.title}</a>`)
          .join(''),
    ).join('')
    return `
<header class="topbar">
  <span class="brand">量化决策平台 V3.0<small>Harness-Centric</small></span>
  ${badge}
  <span class="pill" title="sim 台账权益">权益 <b class="num">${money(overview?.equity?.current)}</b></span>
  ${pill('MCP', ch.mcp?.status)}${pill('SDK', ch.sdk?.status)}${pill('Headless', ch.headless?.status)}
  <span class="spacer"></span>
  <span class="live">● 真实数据</span>
  <span class="asof num">${stamp(new Date().toISOString())}</span>
</header>
<div class="shell">
  <nav class="nav">${nav}</nav>
  <main class="main">
    <h1>${esc(page.title)}</h1>
    <p class="sub">${esc(page.sub)}</p>
    <div id="v3-body"><div class="note">加载中…</div></div>
    <div class="foot">数据来源：本平台 /api/v3/* 实时接口（既有工作台 8397 的 77 工具面、富途行情、sim 台账）；执行入口仅在既有工作台 Web。</div>
  </main>
</div>`
  }

  const RENDER = {
    async index() {
      const [overview, metrics, strategy, execution, settings] = await Promise.all([
        api('/api/v3/overview'),
        api('/api/v3/metrics'),
        api('/api/v3/strategy'),
        api('/api/v3/execution'),
        api('/api/v3/settings'),
      ])
      if (!overview.ok) return errBox('总览', overview.error)
      const equity = overview.equity ?? {}
      const points = Array.isArray(equity.points) ? equity.points : []
      const last = points.at(-1)
      const prev = points.at(-2)
      const dailyPct = last && prev ? ((Number(last.equity) - Number(prev.equity)) / Number(prev.equity)) * 100 : null
      const days = points.length > 1 ? points.length : 0
      const annualized = days > 0 && Number.isFinite(Number(equity.total_return)) ? (Math.pow(1 + Number(equity.total_return), 252 / days) - 1) * 100 : null
      const dd = Number(equity.max_drawdown)
      const headless = overview.headless ?? {}
      const oms = execution.oms ?? {}
      const manualOrders = (oms.orders ?? []).filter((o) => o.stage === 'manual').slice(0, 4)
      const maxSinglePct = (oms.orders ?? []).reduce((max, o) => Math.max(max, oms.nav ? (Number(o.value) / Number(oms.nav)) * 100 : 0), 0)
      const run = strategy?.run ?? null
      const sdk = overview.channels?.sdk ?? {}
      const sdkTurns = (await api('/api/v3/brain')).sdk?.turns ?? []
      const events = []
      for (const t of sdkTurns.slice(0, 3)) {
        events.push({ time: stamp(t.at), title: `SDK 回合 ${t.kind}${t.code ? ' · ' + t.code : ''}`, body: `会话 ${t.sessionId ?? '—'} · 工具调用 ${(t.toolCalls ?? []).length} 次 · 回答 ${(t.answer ?? '').length} 字`, kind: t.kind === 'completed' ? 'ok' : 'bad' })
      }
      for (const c of (headless.last ?? []).slice(0, 3)) {
        events.push({ time: hhmmss(c.started_at), title: `Headless ${c.success ? '完成' : '失败'}（exit ${dash(c.exit_code)}）`, body: `耗时 ${Math.round((c.duration_ms ?? 0) / 1000)}s · token 估算 ${dash(c.tokens_estimate)}`, kind: c.success ? 'ok' : 'bad' })
      }
      if (run) events.push({ time: stamp(run.asOf), title: `研究流水线产出 ${run.proposals?.length ?? 0} 条提案`, body: (run.proposals ?? []).slice(0, 3).map((p) => `${p.ticker} ${p.action}`).join('、'), kind: 'ok' })
      const entries = [...Object.entries(metrics.mcp?.tools ?? {}), ...Object.entries(metrics.wb?.byTool ?? {}).map(([n, c]) => [`wb:${n}`, c])].sort((a, b) => b[1] - a[1]).slice(0, 5)
      const futu = settings.futu ?? {}
      return (
        kpis([
          ['总资产（sim 台账）', money(equity.current), equity.mode ? `模式 ${equity.mode}` : ''],
          ['当日盈亏', dailyPct === null ? '—' : signed(dailyPct, 2, '%'), dailyPct === null ? '台账仅 1 个点位，无前一日基准' : `基准 ${prev?.t ?? '—'}`, Number(dailyPct) >= 0 ? 'pos' : 'neg'],
          ['年化收益', annualized === null ? '—' : signed(annualized, 2, '%'), annualized === null ? '台账点位不足，未年化' : `${days} 个交易日年化`, Number(annualized) >= 0 ? 'pos' : 'neg'],
          ['夏普比率', num(equity.sharpe), `成交 ${dash(equity.trades)} 笔`],
          ['最大回撤', Number.isFinite(dd) ? signed(-Math.abs(dd) * 100, 2, '%') : '—', '阈值 15%', 'neg'],
        ]) +
        card(
          '三通道状态',
          `<div class="grid g3">${['mcp', 'sdk', 'headless']
            .map((key) => {
              const c = overview.channels?.[key] ?? {}
              const status = c.status ?? '—'
              const stats =
                key === 'mcp'
                  ? [['已注册工具', dash(metrics.toolTotal)], ['今日调用', dash(metrics.mcp?.calls)], ['失败', dash(metrics.mcp?.errors)], ['平均延迟', `${metrics.mcp?.avgMs ?? 0}ms`]]
                  : key === 'sdk'
                    ? [['运行时', dash(c.serverInfo?.name ?? '—')], ['版本', dash(c.serverInfo?.version ?? '—')], ['路由', `${dash(c.route?.provider)}/${dash(c.route?.model)}`], ['已落盘回合', dash(sdkTurns.length)]]
                    : [['并发上限', dash(c.breaker?.concurrencyLimit)], ['进行/排队', `${c.breaker?.active ?? 0}/${c.breaker?.queued ?? 0}`], ['单次超时', `${Math.round((c.breaker?.timeoutMs ?? 0) / 1000)}s`], ['今日唤醒', `${headless.today?.total ?? 0}`]]
              return `<div class="card" style="margin:0"><h3>${key.toUpperCase()} ${channelTag(status)}</h3><div class="grid g2">${stats
                .map(([k, v]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v" style="font-size:14px">${v}</div></div>`)
                .join('')}</div></div>`
            })
            .join('')}</div>`,
        ) +
        `<div class="col3">` +
        card('决策链路时间线', timeline(events), `${events.length} 条真实事件`) +
        card(
          '风控红线',
          gauge('单笔交易上限', maxSinglePct > 0 ? Number(maxSinglePct.toFixed(2)) : null, 2, 'var(--amber)') +
            gauge('最大回撤', Number.isFinite(dd) ? Number(Math.abs(dd * 100).toFixed(2)) : null, 15, 'var(--red)') +
            gauge('单一行业暴露', null, 20, 'var(--blue)') +
            nodata('行业暴露', '工作台未提供行业维度敞口'),
        ) +
        card(
          'Agent Loop 实时状态',
          `<div class="grid g2">${[
            ['SDK 通道', sdk.status ?? '—'],
            ['最近一轮', sdk.lastTurn?.kind ?? '—'],
            ['已落盘回合', sdkTurns.length],
            ['Headless 今日', `${headless.today?.total ?? 0} 次`],
          ]
            .map(([k, v]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v" style="font-size:14px">${v}</div></div>`)
            .join('')}</div>` +
            `<div class="note" style="margin-top:8px">工具调用 Top5（本进程计数）</div>${bars(entries)}`,
        ) +
        `</div>` +
        card(
          '待审批（OMS 分级 manual）',
          manualOrders.length > 0
            ? manualOrders
                .map(
                  (o) =>
                    `<div class="approve" style="margin-bottom:8px"><div class="at">${tag(o.stage, 'warn')}<b>${esc(o.ticker)}</b> ${o.side} ${dash(o.qty)} 股</div><div class="akv"><span>金额 ${money(o.value)}</span><span>风控：${esc((o.risk?.reasons ?? [])[0] ?? '—')}</span><span>计划 ${esc(o.plan_id ?? '—')}</span></div></div>`,
                )
                .join('')
            : nodata('待审批订单', '当前 OMS 台账没有 manual 阶段订单'),
          `${oms.orders?.length ?? 0} 单在台账`,
        ) +
        card(
          '数据源健康',
          health([
            { name: 'workbench 8397', status: metrics.workbenchUp ? '在线' : '不可达', kind: metrics.workbenchUp ? 'ok' : 'bad', detail: `工具面 ${metrics.toolTotal ?? '—'} 个 · 今日调用 ${metrics.wb?.calls ?? 0}` },
            { name: '富途 OpenAPI', status: futu.channel ?? '—', kind: futu.channel ? 'ok' : 'warn', detail: `模式 ${futu.openapi?.mode ?? '—'} · MCP Bearer ${futu.mcp_bearer?.present ? '有效至 ' + futu.mcp_bearer.expiry : '缺失'}` },
            { name: 'AKShare', status: '已接入', kind: 'ok', detail: 'A 股新闻（免密钥）；全市场快照上游不可达' },
            { name: 'SEC EDGAR', status: '已接入', kind: 'ok', detail: '美股三表 XBRL（companyconcept）' },
            { name: 'Tushare Pro', status: settings.env?.find((e) => e.key === 'TUSHARE_TOKEN')?.injected ? '已注入' : '未注入', kind: settings.env?.find((e) => e.key === 'TUSHARE_TOKEN')?.injected ? 'ok' : 'warn', detail: '未注入 token 时不发请求' },
          ]),
        )
      )
    },

    async risk() {
      const [risk, overview, metrics, analytics] = await Promise.all([
        api('/api/v3/risk'),
        api('/api/v3/overview'),
        api('/api/v3/metrics'),
        api('/api/v3/risk/analytics?limit=250'),
      ])
      const config = risk?.data?.config ?? null
      const source = risk?.data?.source ?? null
      const equity = overview.equity ?? {}
      const dd = Number(equity.max_drawdown)
      const stages = metrics.oms ?? {}
      const a = analytics?.ok ? analytics.analytics : null
      const metricCard = (label, value, sub) => `<div class="kpi"><div class="k">${esc(label)}</div><div class="v">${value}</div><div class="s">${esc(sub ?? '')}</div></div>`
      return (
        `<section class="card"><div class="grid g5">${[
          metricCard('VaR（95%，1d）', a ? `${num(a.varDailyPct, 3)}%` : '—', a ? `金额 ${money(a.varAmount)}` : '组合风险量不可用'),
          metricCard('CVaR（95%，1d）', a ? `${num(a.cvarDailyPct, 3)}%` : '—', a ? `金额 ${money(a.cvarAmount)}` : ''),
          metricCard('Beta', a ? num(a.beta, 3) : '—', analytics?.benchmarkTicker ? `基准 ${esc(analytics.benchmarkTicker)}` : '基准不可用'),
          metricCard('Alpha（年化）', a ? `${num(a.alphaAnnPct, 2)}%` : '—', a && a.benchmarkAnnReturnPct !== null ? `基准年化 ${num(a.benchmarkAnnReturnPct, 2)}%` : '基准不可用'),
          metricCard('IR（信息比率）', a ? num(a.ir, 3) : '—', a ? `样本 ${a.observations} 天` : ''),
        ].join('')}</div><div class="note">组合来源 ${esc(analytics?.portfolioSource ?? '—')} · 方法：${esc(a?.method ?? '—')} · 窗口 ${esc(a?.window?.from ?? '—')} ~ ${esc(a?.window?.to ?? '—')}</div></section>` +
        `<div class="col3">` +
        card(
          '事前风控',
          config
            ? table(
                ['规则', '配置值', '字段'],
                [
                  ['单笔风险占比', pct(Number(config.risk_per_trade) * 100, 1), 'risk_per_trade'],
                  ['ATR 止损倍数', num(config.stop_atr_mult, 1), 'stop_atr_mult'],
                  ['最大持仓数', dash(config.max_positions), 'max_positions'],
                  ['单日亏损上限', pct(Number(config.daily_loss_limit_pct) * 100, 1), 'daily_loss_limit_pct'],
                  ['单票上限', pct(Number(config.max_position_pct) * 100, 1), 'max_position_pct'],
                ],
              ) + `<div class="note">来源：${esc(source ?? '—')}（工作台真实配置）</div>`
            : errBox('风控配置', risk?.error),
        ) +
        card(
          '事中风控',
          `<div class="grid g2">${[
            ['台账回撤', Number.isFinite(dd) ? signed(-Math.abs(dd) * 100, 2, '%') : '—'],
            ['组合年化波动', a ? `${num(a.annVolPct, 2)}%` : '—'],
            ['待人工确认', stages.manual ?? 0],
            ['红线阻断', stages.blocked ?? 0],
          ]
            .map(([k, v]) => metricCard(k, v, ''))
            .join('')}</div>` + `<div class="note">实时告警流由工作台 events 工具提供，本页未接入（无数据源）。</div>`,
        ) +
        card(
          '事后风控',
          `<div class="grid g2">${[
            ['Kupiec 检验', a?.kupiec?.pass === undefined ? '—' : a.kupiec.pass ? '通过' : '拒绝'],
            ['破位/期望', a?.kupiec ? `${a.kupiec.breaches} / ${(a.observations * 0.05).toFixed(1)}` : '—'],
            ['组合年化收益', a ? `${num(a.annReturnPct, 2)}%` : '—'],
            ['组合最大回撤', a ? `${num(a.maxDrawdownPct, 2)}%` : '—'],
          ]
            .map(([k, v]) => metricCard(k, v, ''))
            .join('')}</div>` + `<div class="note">绩效归因（行业/因子/个股）工作台未提供 → 无数据源，不做占位。</div>`,
        ) +
        `</div>` +
        card(
          '暴露与集中度',
          a && Object.keys(a.tickers ?? {}).length > 0
            ? bars(Object.entries(a.tickers).map(([t, w]) => [t, Number((w * 100).toFixed(2))]), 'var(--cyan)') +
              `<div class="note">按组合权重（${esc(analytics.portfolioSource)}）；行业维度敞口工作台未提供 → 无数据源。</div>`
            : nodata('组合暴露', '无可用组合权重'),
        ) +
        card('组合净值曲线（真实日 K 计算）', a?.equityCurve?.length >= 2 ? lineChart(a.equityCurve.map((p) => p.v)) : nodata('回撤/净值曲线', '样本不足或风险量不可用'), a ? `${a.window.from} ~ ${a.window.to} · ${a.observations} 个交易日` : '') +
        card('风控规则表（工作台真实配置）', config ? table(['规则', '阈值', '当前值', '状态', '来源'], [['单笔风险占比', pct(Number(config.risk_per_trade) * 100, 1), '—', tag('生效', 'ok'), esc(source ?? '—')], ['ATR 止损倍数', num(config.stop_atr_mult, 1), '—', tag('生效', 'ok'), esc(source ?? '—')], ['最大持仓数', dash(config.max_positions), '—', tag('生效', 'ok'), esc(source ?? '—')], ['单日亏损上限', pct(Number(config.daily_loss_limit_pct) * 100, 1), '—', tag('生效', 'ok'), esc(source ?? '—')], ['单票上限', pct(Number(config.max_position_pct) * 100, 1), '—', tag('生效', 'ok'), esc(source ?? '—')]]) : nodata('风控规则表', '工作台配置不可用')) +
        card(
          '阻断记录（OMS 台账 blocked）',
          (await api('/api/v3/oms/orders')).orders?.filter((o) => o.stage === 'blocked').length > 0
            ? table(['订单', '标的', '规则', '原因'], (await api('/api/v3/oms/orders')).orders.filter((o) => o.stage === 'blocked').map((o) => [esc(String(o.id).slice(0, 10)), esc(o.ticker), esc((o.risk?.reasons ?? [])[0] ?? '—'), esc((o.risk?.reasons ?? [])[1] ?? '—')]))
            : nodata('阻断记录', '当前台账无 blocked 阶段订单'),
          `${stages.blocked ?? 0} 条`,
        )
      )
    },


    async brain() {
      const [brain, metrics] = await Promise.all([api('/api/v3/brain'), api('/api/v3/metrics')])
      if (!brain.ok) return errBox('决策大脑', brain.error)
      const sdk = brain.sdk ?? {}
      const headless = brain.headless ?? {}
      const run = brain.decision ?? null
      const top = run?.proposals?.[0] ?? null
      const turns = sdk.turns ?? []
      const phaseOf = { 'step/start': ['行动', ''], 'assistant/message': ['推理', ''], 'tool/call': ['行动', 'ok'], 'tool/result': ['反馈', 'ok'], 'step/end': ['反馈', ''], 'turn/end': ['反馈', ''] }
      const flow = []
      let answer = ''
      const tools = []
      for (const e of sdk.events ?? []) {
        const ev = e.params?.event
        if (!ev) continue
        const mapped = phaseOf[ev.type]
        if (mapped) {
          let body = ''
          if (ev.type === 'assistant/message') {
            for (const c of ev.data?.message?.content ?? []) {
              if (c.type === 'text' && String(c.text ?? '').trim()) answer = c.text
              if (c.type === 'reasoning') body = String(c.text ?? '').slice(0, 200)
              if (c.type === 'tool-call') tools.push(String(c.name ?? ''))
            }
          } else if (ev.type === 'tool/call' || ev.type === 'tool/result') {
            body = String(ev.data?.message?.content?.[0]?.name ?? ev.data?.message?.source?.kind ?? '').slice(0, 120)
          }
          flow.push({ time: hhmmss(e.at), title: `${mapped[0]} · ${ev.type}`, body, kind: mapped[1] })
        }
      }
      if (!answer && turns[0]?.answer) answer = turns[0].answer
      const callRows = (headless.last ?? []).map((c) => [hhmmss(c.started_at), c.success ? tag('成功', 'ok') : tag('失败', 'bad'), dash(c.exit_code), `${Math.round((c.duration_ms ?? 0) / 1000)}s`, dash(c.tokens_estimate)])
      const entries = [...Object.entries(metrics.mcp?.tools ?? {}), ...Object.entries(metrics.wb?.byTool ?? {}).map(([n, c]) => [`wb:${n}`, c])].sort((a, b) => b[1] - a[1]).slice(0, 8)
      return (
        card(
          '会话控制',
          `<div class="grid g4">${[['会话', turns[0]?.sessionId ?? '—'], ['模型路由', `${dash(sdk.route?.provider)}/${dash(sdk.route?.model)}`], ['推理强度', dash(sdk.route?.reasoningEffort)], ['通道状态', dash(sdk.status)]]
            .map(([k, v]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v" style="font-size:13px">${esc(String(v))}</div></div>`)
            .join('')}</div>`,
          '<button class="btn" data-act="sdk-start">握手</button>',
        ) +
        `<div class="col3">` +
        card('推理流（真实 SDK 事件）', timeline(flow.slice(-12)), `${flow.length} 步`) +
        card('决策结论（研究流水线）',
          top
            ? `<div class="grid g2">${[['决策', `${top.ticker} ${top.action}`], ['风险等级', dash(top.riskLevel)], ['建议操作', `目标权重 ${dash(top.targetWeightPct)}%`], ['置信度', '无数据源']]
                .map(([k, v]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v" style="font-size:14px">${esc(String(v))}</div></div>`)
                .join('')}</div><div class="note">依据：${esc(String(top.basis ?? '—'))}</div><div class="note">置信度工作台未提供 → 无数据源，不填占位值。</div>`
            : nodata('决策结论', '尚未运行研究流水线'),
          '<button class="btn" data-act="strategy-run">运行流水线</button>') +
        card('运行元信息',
          `<div class="grid g2">${[['运行时', dash(sdk.serverInfo?.name ?? '—')], ['版本', dash(sdk.serverInfo?.version ?? '—')], ['落盘回合', turns.length], ['最大输出 token', dash(sdk.route?.maxTokens)]]
            .map(([k, v]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v" style="font-size:14px">${esc(String(v))}</div></div>`)
            .join('')}</div>` +
          `<div class="note">已用 token：Headless 估算合计 ${dash(metrics.headless?.tokens)}；SDK 通道不回报 token 用量 → 不展示。</div>`)
        + `</div>` +
        card('Harness 会话输出（真实回合）', answer ? `<pre class="answer">${esc(answer.slice(0, 4000))}</pre><div class="note">本轮工具调用 ${tools.length} 次${tools.length ? '：' + tools.map((t) => esc(t)).join('、') : ''}</div>` : nodata('会话输出', '本进程尚无已完成回合')) +
        card('发起真实任务（SDK 通道）', '<div class="note">提交后由平台通过 JSON-RPC 驱动 Harness 运行时，回合结束此处显示真实回答。</div>', '<input id="sdk-prompt" placeholder="例如：给出当前模拟盘的盘前检查清单" style="width:340px;background:var(--panel2);border:1px solid var(--border);border-radius:6px;padding:5px 8px;color:var(--text)"> <button class="btn" data-act="sdk-prompt">提交</button>') +
        card('工具调用分布（本进程计数）', bars(entries)) +
        card('Headless 调用明细', table(['时间', '结果', 'exit', '耗时', 'token 估算'], callRows))
      )
    },

    async market() {
      const [headline, watch, book, plates, matrix] = await Promise.all([
        api(`/api/v3/market?ticker=${encodeURIComponent(UI.market.ticker)}&period=${encodeURIComponent(UI.market.period)}&limit=120`),
        api('/api/v3/market/watchlist?n=6'),
        api(`/api/v3/orderbook?ticker=${encodeURIComponent(UI.market.ticker)}`),
        api('/api/v3/plates?market=SH&plate_class=ALL'),
        api('/api/v3/factors/matrix'),
      ])
      const bars = headline?.data?.bars ?? []
      const last = bars.at(-1) ?? {}
      const prev = bars.at(-2) ?? {}
      const chg = prev.c && last.c ? ((last.c - prev.c) / prev.c) * 100 : null
      const watchRows = (watch.rows ?? []).map((r) => [esc(r.ticker), num(r.close), signed(r.changePct, 2, '%'), signed(r.mom20Pct, 2, '%'), num(r.peTtm), num(r.pb), dash(r.asOf)])
      const m = matrix?.matrix ?? {}
      const factorRows = (m.tickers ?? []).map((t, i) => [esc(t), ...(m.factors ?? []).slice(0, 6).map((_, j) => { const v = (m.matrix ?? [])[i]?.[j]; return Number.isFinite(Number(v)) ? Number(v).toFixed(2) : '—' })])
      const plateRows = (plates?.data?.plate_list ?? []).slice(0, 12).map((p) => [esc(p.sc_name ?? p.plate_name ?? '—'), esc(p.plate_id ?? '')])
      return (
        card('工具条',
          `<div class="grid g4">${[['标的', esc(UI.market.ticker)], ['周期', esc(UI.market.period)], ['数据源', esc(headline?.data?.source ?? '—')], ['as_of', dash(headline?.data?.as_of)]]
            .map(([k, v]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v" style="font-size:13px">${v}</div></div>`)
            .join('')}</div>`,
          ['1d', '5m', '60m'].map((per) => `<button class="btn" data-act="market-period" data-period="${per}">${per}</button>`).join(' ') + ' <button class="btn" data-act="market-refresh">刷新</button>') +
        card('K 线主图（真实 OHLC + 成交量）', candleChart(bars) || nodata('K 线', 'K 线不足，无法绘制'), `${bars.length} 根 · 最新 ${num(last.c)}（${signed(chg, 2, '%')}）`) +
        card('盘口深度', nodata('盘口五档', '富途账号未开通实时行情权限（errcode=-9 realtime quote permission required）')) +
        card('自选行情表', table(['代码', '现价', '日涨跌%', '20日动量%', 'PE(TTM)', 'PB', 'as_of'], watchRows), watch.ok ? `${watch.rows.length} 只` : '取数失败') +
        card('多因子信号表（横截面 z 值）', factorRows.length > 0 ? table(['标的', ...(m.factors ?? []).slice(0, 6)], factorRows) : nodata('因子矩阵', '工作台 factors 不可用'), `as_of ${dash(m.as_of)}`) +
        card('板块（真实板块清单）', table(['板块', '代码'], plateRows), '涨跌幅需实时行情权限 → 无数据源，不填占位')
      )
    },

    async strategy() {
      const [strategy, factors, sweep] = await Promise.all([api('/api/v3/strategy'), api('/api/v3/factors/matrix'), api('/api/v3/ml/sweep?ticker=SH.600519&windows=10,20,30,60&rebalance=5,10,20')])
      const run = strategy?.run ?? null
      const st = run?.stages ?? {}
      const done = (ok) => (ok ? 'done' : '')
      const m = factors?.matrix ?? {}
      const factorRows = (m.tickers ?? []).map((t, i) => [esc(t), ...(m.factors ?? []).slice(0, 8).map((_, j) => { const v = (m.matrix ?? [])[i]?.[j]; return Number.isFinite(Number(v)) ? Number(v).toFixed(2) : '—' })])
      const ic = factors?.ic ?? {}
      const windows = [...new Set((sweep?.grid ?? []).map((g) => g.window))].sort((a, b) => a - b)
      const rebalances = [...new Set((sweep?.grid ?? []).map((g) => g.rebalanceDays))].sort((a, b) => a - b)
      return (
        card('研究流水线',
          steps([
            { n: '1', label: 'PDAT 数据准备', state: st.PDAT ? 'done' : '' },
            { n: '2', label: 'PAAT 因子分析', state: st.PAAT ? (st.PCPT ? 'done' : 'on') : '' },
            { n: '3', label: 'PCPT 候选池', state: st.PCPT ? (st.PRT ? 'done' : 'on') : '' },
            { n: '4', label: 'PRT 组合', state: st.PRT ? 'done' : '' },
            { n: '5', label: 'PET 决策', state: st.PET ? 'done' : '' },
          ]) + `<div class="note">${run ? `最近一轮 ${stamp(run.asOf)} · K 线 ${st.PDAT?.bars ?? 0} 根 · 因子覆盖 ${st.PAAT?.withFactors ?? 0}/${st.PAAT?.analyzed ?? 0} · 提案 ${st.PET?.proposals ?? 0} 条` : '尚未运行'}</div>`,
          '<button class="btn" data-act="strategy-run">运行流水线</button>') +
        card('调仓建议提案', run ? table(['标的', '动作', '目标权重', '风险', '依据'], (run.proposals ?? []).map((p) => [esc(p.ticker), p.action, `${dash(p.targetWeightPct)}%`, p.riskLevel, esc(String(p.basis ?? '').slice(0, 70))])) : nodata('提案', '尚未运行流水线')) +
        card('因子库（横截面 z 值）', factorRows.length > 0 ? table(['标的', ...(m.factors ?? []).slice(0, 8)], factorRows) : nodata('因子矩阵', '工作台 factors 不可用'),
          ic.ok ? `IC ${esc(ic.factor)} · 均值 ${num(ic.meanIc, 4)} · IR ${num(ic.ir, 3)} · 样本 ${ic.observations}` : '') +
        card('参数扫描热力图（真实回测网格）', sweep?.ok ? heatmap(sweep.grid, windows, rebalances) + `<div class="note">最优：window=${dash(sweep.best?.window)} / rebalance=${dash(sweep.best?.rebalanceDays)} 日 → sharpe ${num(sweep.best?.sharpe, 3)}（${sweep.grid.length} 格，标的 ${esc(sweep.ticker)}）</div>` : nodata('参数扫描', sweep?.error?.message ?? '回测不可用')) +
        card('回测曲线', '<div id="bt-out"><div class="note">点右侧按钮运行真实回测（富途日 K，PIT 对齐）。</div></div>', '<button class="btn" data-act="backtest">运行回测</button> <button class="btn" data-act="sweep">参数扫描</button>') +
        card('策略列表 / 策略生成器', nodata('策略注册表与生成器', '平台当前没有策略注册与生成组件；策略以研究流水线提案形式产出'))
      )
    },

    async execution() {
      const [d, ordersView] = await Promise.all([api('/api/v3/execution'), api('/api/v3/oms/orders')])
      if (!d.ok) return errBox('执行视图', d.error)
      const rowsOf = (p) => (Array.isArray(p) ? p : Array.isArray(p?.groups) ? p.groups.flatMap((g) => g.rows ?? []) : Array.isArray(p?.rows) ? p.rows : [])
      const positions = rowsOf(d.positions)
      const openOrders = rowsOf(d.orders_open)
      const deals = rowsOf(d.deals_today)
      const oms = d.oms ?? {}
      const orders = ordersView.orders ?? oms.orders ?? []
      const stages = oms.stages ?? {}
      const byStage = (list) => orders.filter((o) => list.includes(o.stage))
      const orderRow = (o) => [`<span class="num">${esc(String(o.id).slice(0, 10))}</span>`, esc(o.ticker), o.side, dash(o.qty), money(o.value), tag(o.stage, o.stage === 'blocked' ? 'bad' : o.stage === 'manual' ? 'warn' : 'ok'), esc((o.risk?.reasons ?? ['阈值内'])[0] ?? '')]
      const chain = orders[0]
      return (
        card('执行入口与计数',
          `<div class="grid g4">${[['交易模式', esc(d.positions?.mode ?? 'sim（工作台为准）')], ['在途订单', openOrders.length], ['今日成交', deals.length], ['待审批', stages.manual ?? 0]]
            .map(([k, v]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v" style="font-size:14px">${v}</div></div>`)
            .join('')}</div><div class="note">本平台不含下单入口；执行只发生在既有工作台 Web 的「执行已冻结计划」+ 人工确认（live 需口令）。</div>`) +
        card('订单生命周期看板',
          kanban([
            { name: '信号生成', count: orders.length, items: orders.slice(0, 3).map((o) => `${o.ticker} ${o.side}`) },
            { name: '风控校验', count: orders.filter((o) => (o.risk?.reasons ?? []).length >= 0).length, items: orders.slice(0, 2).map((o) => (o.risk?.action ?? '—')) },
            { name: '审批', count: stages.manual ?? 0, items: byStage(['manual']).slice(0, 3).map((o) => o.ticker) },
            { name: '已提交', count: stages.submitted ?? 0, items: byStage(['submitted']).slice(0, 3).map((o) => o.ticker) },
            { name: '部分成交', count: stages.partial ?? 0, items: [] },
            { name: '全部成交', count: stages.filled ?? 0, items: byStage(['filled']).slice(0, 3).map((o) => o.ticker) },
          ])) +
        `<div class="col3">` +
        card('自动执行（阈值内）', byStage(['risk_passed']).length > 0 ? table(['订单', '标的', '金额'], byStage(['risk_passed']).slice(0, 5).map((o) => [esc(String(o.id).slice(0, 8)), esc(o.ticker), money(o.value)])) : nodata('自动执行队列', '当前无 risk_passed 订单')) +
        card('人工确认（超阈值）', byStage(['manual']).length > 0 ? byStage(['manual']).slice(0, 3).map((o) => `<div class="approve" style="margin-bottom:8px"><div class="at">${tag('manual', 'warn')}<b>${esc(o.ticker)}</b> ${o.side} ${dash(o.qty)}</div><div class="akv"><span>金额 ${money(o.value)}</span><span>${esc((o.risk?.reasons ?? [])[0] ?? '')}</span></div></div>`).join('') + `<div class="note">确认动作在工作台 Web 完成（口令/确认卡），平台不提供提交入口。</div>` : nodata('待人工确认', '当前无 manual 订单')) +
        card('强制阻断（红线）', byStage(['blocked']).length > 0 ? table(['订单', '标的', '规则'], byStage(['blocked']).map((o) => [esc(String(o.id).slice(0, 8)), esc(o.ticker), esc((o.risk?.reasons ?? [])[0] ?? '—')])) : nodata('阻断记录', '当前无 blocked 订单')) +
        `</div>` +
        card('订单明细表', table(['订单', '标的', '方向', '数量', '金额', '阶段', '风控结论'], orders.slice(0, 12).map(orderRow)), `${orders.length} 单`) +
        card('持仓与成交质量', table(['代码', '数量', '成本', '现价'], positions.slice(0, 8).map((p) => [esc(p.ticker ?? p.symbol ?? '—'), dash(p.qty), dash(p.avg_cost ?? p.cost_price), dash(p.current ?? p.price)])) + `<div class="note">成交质量（成交率/滑点/撤单率）工作台未提供 → 无数据源。</div>`) +
        card('决策链路追溯', chain ? timeline((chain.history ?? []).slice(-6).map((h) => ({ time: stamp(h.at), title: h.stage, body: (h.reasons ?? []).join('；') || '阈值内' }))) + `<div class="note">信号溯源与决策快照由工作台 audit 工具提供，本页未接入 → 该两段无数据源。</div>` : nodata('决策链路', '台账暂无订单')) +
        card('对账', '<div class="note">把工作台 frozen 计划订单登记/更新到台账，并回读确认通道状态。</div>', '<button class="btn" data-act="oms-sync">与工作台对账</button>')
      )
    },

    async gateway() {
      const [gateway, metrics, settings] = await Promise.all([api('/api/v3/gateway'), api('/api/v3/metrics'), api('/api/v3/settings')])
      if (!gateway.ok) return errBox('网关', gateway.error)
      const ch = gateway.channels ?? {}
      const headless = gateway.headless ?? {}
      const sdk = ch.sdk ?? {}
      const calls = (headless.last ?? []).map((c) => [hhmmss(c.started_at), c.success ? tag('成功', 'ok') : tag('失败', 'bad'), dash(c.exit_code), `${Math.round((c.duration_ms ?? 0) / 1000)}s`, dash(c.tokens_estimate)])
      const toolEntries = Object.entries(metrics.mcp?.tools ?? {})
      const wbEntries = Object.entries(metrics.wb?.byTool ?? {})
      return (
        `<div class="col3">` +
        card('MCP Bridge', table(['项', '值'], [['方向', '平台 → Harness（工具暴露）'], ['协议', esc(ch.mcp?.protocol ?? '—')], ['状态', channelTag(ch.mcp?.status)], ['已注册工具', dash(metrics.toolTotal)], ['今日调用', dash(metrics.mcp?.calls)], ['失败', dash(metrics.mcp?.errors)], ['平均延迟', `${metrics.mcp?.avgMs ?? 0}ms`]]), '状态机：start（运行中）') +
        card('SDK JSON-RPC', table(['项', '值'], [['方向', '平台 ↔ Harness（会话驱动）'], ['协议', esc(ch.sdk?.protocol ?? '—')], ['状态', channelTag(sdk.status)], ['运行时', sdk.serverInfo ? `${esc(sdk.serverInfo.name)} ${esc(sdk.serverInfo.version)}` : '—'], ['路由', `${esc(sdk.route?.provider ?? '—')}/${esc(sdk.route?.model ?? '—')}`], ['最近一轮', sdk.lastTurn ? esc(sdk.lastTurn.kind) : '—']]), '<button class="btn" data-act="sdk-start">握手</button>') +
        card('Headless CLI', table(['项', '值'], [['方向', '平台 → Harness（自动唤醒）'], ['协议', esc(ch.headless?.command ?? 'CLI 子进程')], ['状态', channelTag(ch.headless?.status)], ['并发上限', dash(headless.breaker?.concurrencyLimit)], ['单次超时', `${Math.round((headless.breaker?.timeoutMs ?? 0) / 1000)}s`], ['今日唤醒', `${headless.today?.total ?? 0} 次`]]), '<button class="btn" data-act="headless-run">立即唤醒</button>') +
        `</div>` +
        card('Headless 调用日志', table(['时间', '结果', 'exit', '耗时', 'token 估算'], calls), `${headless.today?.success ?? 0} 成功 / ${headless.today?.failed ?? 0} 失败`) +
        card('调度器', table(['规则', '触发点', '任务'], (gateway.scheduler?.rules ?? []).map((r) => [esc(r.id), r.at, esc(r.task)])) + `<div class="note">事件触发（新闻/持仓异动/阈值突破）与流水线节点触发未实现 → 无数据源。</div>`) +
        card('熔断保护（平台侧强制）', table(['项', '值'], [['并发上限', dash(headless.breaker?.concurrencyLimit)], ['进行/排队', `${headless.breaker?.active ?? 0}/${headless.breaker?.queued ?? 0}`], ['单次超时', `${Math.round((headless.breaker?.timeoutMs ?? 0) / 1000)}s`], ['token 预算/次', `${Math.round((headless.breaker?.tokenBudgetPerCall ?? 0) / 1000)}K`], ['今日 kill', dash(headless.today?.killed)], ['超时退出码', '124（platform-timeout）']])) +
        card('Profile 与 Bundle', nodata('dsh bundle 列表', '本平台不是 dsh profile：它以角色（all/gateway/web/scheduler）运行，bundle 概念不适用')) +
        card('调用分布', bars([...toolEntries, ...wbEntries.map(([n, c]) => [`wb:${n}`, c])].sort((a, b) => b[1] - a[1]).slice(0, 8)), `workbench 在线 ${metrics.workbenchUp ? '是' : '否'}`)
      )
    },

    async tools() {
      const [tools, metrics] = await Promise.all([api('/api/v3/tools'), api('/api/v3/metrics')])
      if (!tools.ok) return errBox('工具目录', tools.error)
      const domains = tools.domains ?? {}
      const flat = Object.values(domains).flat()
      const calls = Object.entries(metrics.mcp?.tools ?? {})
      const wbTools = Object.entries(metrics.wb?.byTool ?? {})
      const failing = wbTools.filter(([, c]) => Number(metrics.wb?.errors ?? 0) > 0).slice(0, 5)
      return (
        card('总览',
          `<div class="grid g5">${[['工具总数', tools.total], ['工具域', Object.keys(domains).length], ['一级工具', flat.filter((t) => t.kind === 'first-class').length], ['直通工具', flat.filter((t) => t.kind === 'proxy').length], ['MCP 调用', metrics.mcp?.calls ?? 0]]
            .map(([k, v]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v" style="font-size:16px">${v}</div></div>`)
            .join('')}</div>`) +
        card('工具发现代理', '<div class="note">对外仅暴露 <b>list_tools</b> / <b>call_tool</b>：按域列出目录、按名调用；避免上百 schema 撑爆上下文。交易写类不设一级工具。</div>' + bars(calls)) +
        Object.entries(domains).map(([domain, list]) => card(`${domain}（${list.length}）`, table(['工具', '类型', '工作台工具', '说明'], list.slice(0, 12).map((t) => [esc(t.name), t.kind === 'first-class' ? tag('一级', 'ok') : tag('直通'), esc(String(t.wb ?? '')), esc(String(t.desc ?? '').slice(0, 60))])))).join('') +
        card('平台 MCP 服务器注册状态', table(['项', '值'], [['stdio 入口', 'node server/mcp/run.mjs'], ['HTTP 端点', 'POST /api/v3/mcp'], ['连接方式', 'dsh-mcp-client（stdio）或任意 MCP 客户端'], ['adapter 挂载', '未挂载（@helibeiqi/dsh-cordis-universal-adapter 未安装）→ 该行无数据源']])) +
        card('健康与降级', (failing.length > 0 ? table(['工具', '调用'], failing) : `<div class="note">workbench 调用失败 ${metrics.wb?.errors ?? 0} 次（合计 ${metrics.wb?.calls ?? 0} 次）；平均延迟 ${metrics.wb?.avgMs ?? 0}ms。</div>`))
      )
    },

    async settings() {
      const [settings, metrics] = await Promise.all([api('/api/v3/settings'), api('/api/v3/metrics')])
      if (!settings.ok) return errBox('配置', settings.error)
      const futu = settings.futu ?? {}
      const bearer = futu.mcp_bearer ?? {}
      const envRows = (settings.env ?? []).map((e) => [esc(e.key), e.injected ? tag('已注入', 'ok') : tag('未注入', 'warn'), esc(e.source)])
      const credRows = [
        ['workbench 8397', 'HTTP 工具面', '读+受约束写', metrics.workbenchUp ? tag('已连接', 'ok') : tag('不可达', 'bad'), '—'],
        ['富途 OpenAPI/OpenD', 'appkey 模式', '行情+交易', futu.channel ? tag('已配置', 'ok') : tag('缺失', 'warn'), `token ${bearer.present ? '至 ' + bearer.expiry : '缺失'}`],
        ['AKShare', '免密钥公开端点', 'A 股新闻', tag('已接入', 'ok'), 'as_of 随返回'],
        ['SEC EDGAR', '公开 XBRL', '美股三表', tag('已接入', 'ok'), 'User-Agent 可识别'],
        ['Tushare Pro', 'Token API', 'A 股财务/行情', settings.env?.find((e) => e.key === 'TUSHARE_TOKEN')?.injected ? tag('已注入', 'ok') : tag('未注入', 'warn'), '未注入时不发请求'],
      ]
      return (
        card('提示', `<div class="note">${esc(settings.mode_note ?? '')}</div>`) +
        `<div class="col3">` +
        card('交易模式', `<div class="grid g2">${[['当前模式', esc(settings.trading_mode ?? '—')], ['切换入口', '工作台 Web'], ['前置校验', '3 项（富途交易授权/风控阈值/口令）'], ['LIVE 口令', '确认执行']]
          .map(([k, v]) => `<div class="kpi"><div class="k">${esc(k)}</div><div class="v" style="font-size:13px">${v}</div></div>`)
          .join('')}</div>`) +
        card('模式切换审计', nodata('切换审计流水', '审计由工作台 audit 工具提供，本页未接入')) +
        card('富途授权', table(['项', '值'], [['渠道', esc(futu.channel ?? '—')], ['OpenAPI 模式', esc(futu.openapi?.mode ?? '—')], ['配置键', esc((futu.openapi?.config_keys ?? []).join(', ') || '—')], ['MCP Bearer', bearer.present ? tag('有效', 'ok') : tag('缺失', 'bad')], ['有效期至', esc(String(bearer.expiry ?? '—'))], ['凭据来源', bearer.present ? '~/.dsh/futu-token' : '—']]))
        + `</div>` +
        card('统一授权中心', table(['来源', '类型', '作用域', '状态', '凭据/时效'], credRows)) +
        card('环境变量与密钥来源', table(['变量', '状态', '来源'], envRows)) +
        card('授权与审计策略', table(['项', '值'], [['单笔交易上限', '2%（超过转人工确认）'], ['单一行业暴露', '20%（超过强制阻断）'], ['最大回撤', '15%（触及强制阻断）'], ['只读降级', '凭据过期/上游不可用时禁止下单'], ['审计留痕', 'data/audit.jsonl（变更 API 与每次 MCP 工具调用）']]))
      )
    },
  }

  const UI = { market: { ticker: 'SH.600519', period: '1d' } }

  async function runAction(name) {
    if (name === 'strategy-run') return post('/api/v3/strategy/run', { topN: 2 })
    if (name === 'sdk-start') return post('/api/v3/sdk/start')
    if (name === 'oms-sync') return post('/api/v3/oms/sync')
    if (name === 'backtest') return post('/api/v3/ml/backtest', { ticker: 'SH.600519', window: 20, rebalanceDays: 5, limit: 500 })
    if (name === 'sweep') return post('/api/v3/ml/param_sweep', { ticker: 'SH.600519', windows: [10, 20, 30], rebalanceDays: [5, 10], limit: 500 })
    if (name === 'headless-run') return post('/api/v3/headless/run', { task_type: 'risk_review', context: {} })
    if (name === 'sdk-prompt') {
      const text = document.getElementById('sdk-prompt')?.value?.trim()
      if (!text) return { ok: false, error: { message: '请输入任务文本' } }
      return post('/api/v3/sdk/prompt', { sessionId: 'quant-ui', text })
    }
    return { ok: false, error: { message: `未知动作 ${name}` } }
  }

  function bindActions(root, page) {
    for (const button of root.querySelectorAll('button[data-act]')) {
      button.addEventListener('click', async () => {
        const name = button.dataset.act
        if (name === 'market-period') {
          UI.market.period = button.dataset.period
          render(page)
          return
        }
        if (name === 'market-refresh') {
          render(page)
          return
        }
        button.disabled = true
        const original = button.textContent
        button.textContent = '执行中…'
        const result = await runAction(name)
        button.disabled = false
        button.textContent = original
        const out = document.getElementById(name === 'backtest' || name === 'sweep' ? 'bt-out' : name === 'headless-run' ? 'hl-out' : 'v3-none')
        if (out) {
          out.innerHTML = `<pre class="answer" style="margin-top:8px">${esc(JSON.stringify(result, null, 1).slice(0, 1800))}</pre>`
          return
        }
        render(page)
      })
    }
  }

  const pageId = (location.pathname.split('/').pop() || 'index.html').replace(/\.html$/, '') || 'index'
  const page = PAGES.find((p) => p.id === pageId) ?? PAGES[0]

  async function render(target = page) {
    document.title = `${target.title} · 量化决策平台 V3.0`
    const body = document.getElementById('v3-body')
    if (!body) return
    body.innerHTML = '<div class="note">加载中…</div>'
    try {
      body.innerHTML = await RENDER[target.id]()
    } catch (error) {
      body.innerHTML = errBox(target.title, error)
    }
    bindActions(body, target)
  }

  async function boot() {
    const overview = await api('/api/v3/overview')
    document.body.innerHTML = shell(page, overview.ok ? overview : null)
    await render(page)
    setInterval(() => render(page), 30000)
  }

  boot()
})()
