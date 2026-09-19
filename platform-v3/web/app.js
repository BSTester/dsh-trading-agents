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
      const [overview, metrics, strategy] = await Promise.all([api('/api/v3/overview'), api('/api/v3/metrics'), api('/api/v3/strategy')])
      if (!overview.ok) return errBox('总览', overview.error)
      const equity = overview.equity ?? {}
      const points = Array.isArray(equity.points) ? equity.points : []
      const headless = overview.headless ?? {}
      const today = headless.today ?? {}
      const dd = Number(equity.max_drawdown)
      const run = strategy?.run ?? null
      return (
        kpis([
          ['sim 台账权益', money(equity.current), equity.mode ? `模式 ${equity.mode}` : ''],
          ['累计收益', Number.isFinite(Number(equity.total_return)) ? signed(Number(equity.total_return) * 100, 4, '%') : '—', '台账口径', Number(equity.total_return) >= 0 ? 'pos' : 'neg'],
          ['夏普（台账）', num(equity.sharpe), `成交 ${dash(equity.trades)} 笔`],
          ['最大回撤（台账）', Number.isFinite(dd) ? signed(-Math.abs(dd) * 100, 2, '%') : '—', null, 'neg'],
          ['Headless 今日', `${today.total ?? 0} 次`, `成功 ${today.success ?? 0} / 失败 ${today.failed ?? 0}`],
          ['台账点位', points.length, points.length > 0 ? `最近 ${points.at(-1)?.t ?? '—'}` : '尚无点位'],
        ]) +
        card(
          '三通道状态',
          `<div class="grid g3">${['mcp', 'sdk', 'headless']
            .map((key) => {
              const c = overview.channels?.[key] ?? {}
              const extra =
                key === 'mcp'
                  ? `已注册工具 ${dash(metrics.toolTotal)} · 今日调用 ${dash(metrics.mcp?.calls)} · 失败 ${dash(metrics.mcp?.errors)}`
                  : key === 'sdk'
                    ? `运行时 ${dash(c.serverInfo?.name ?? '—')} ${dash(c.serverInfo?.version ?? '')} · ${dash(c.reason ?? c.note ?? '待握手')}`
                    : `并发上限 ${dash(c.breaker?.concurrencyLimit)} · 超时 ${Math.round((c.breaker?.timeoutMs ?? 0) / 1000)}s`
              return `<div class="card" style="margin:0"><h3>${key.toUpperCase()} ${channelTag(c.status)}</h3><div class="note" style="margin:0">${esc(extra)}</div></div>`
            })
            .join('')}</div>`,
        ) +
        card(
          '最近决策（PDAT→PET 流水线）',
          run
            ? table(
                ['标的', '动作', '目标权重', '风险', '依据'],
                (run.proposals ?? []).map((p) => [esc(p.ticker), p.action, `${dash(p.targetWeightPct)}%`, p.riskLevel, esc(String(p.basis ?? '').slice(0, 60))]),
              ) + `<div class="note">流水线时间 ${stamp(run.asOf)} · 评分来源 ${esc(run.stages?.PAAT?.scoreSource ?? '—')} · 因子覆盖 ${run.stages?.PAAT?.withFactors ?? 0}/${run.stages?.PAAT?.analyzed ?? 0}</div>`
            : '<div class="note">尚未运行研究流水线。</div>',
          '<button class="btn" data-act="strategy-run">运行流水线</button>',
        ) +
        card(
          '台账权益曲线',
          points.length >= 2 ? lineChart(points.map((p) => p.equity)) : `<div class="note">台账仅 ${points.length} 个交易日点位，无可绘制曲线（不做插值、不用占位）</div>`,
          points.length >= 2 ? `as_of ${dash(points.at(-1)?.t)}` : '',
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
      const assistantTexts = []
      const toolCalls = []
      for (const e of sdk.events ?? []) {
        const ev = e.params?.event
        if (!ev || ev.type !== 'assistant/message') continue
        for (const b of ev.data?.message?.content ?? []) {
          if (b.type === 'text' && String(b.text ?? '').trim()) assistantTexts.push(b.text)
          if (b.type === 'tool-call') toolCalls.push(String(b.name ?? ''))
        }
      }
      const callRows = (headless.last ?? []).map((c) => [hhmmss(c.started_at), c.success ? tag('成功', 'ok') : tag('失败', 'bad'), dash(c.exit_code), `${Math.round((c.duration_ms ?? 0) / 1000)}s`, dash(c.tokens_estimate)])
      const entries = [...Object.entries(metrics.mcp?.tools ?? {}), ...Object.entries(metrics.wb?.byTool ?? {}).map(([n, c]) => [`wb:${n}`, c])].sort((a, b) => b[1] - a[1]).slice(0, 6)
      const turns_ = sdk.turns ?? []
      const lastTurn = turns_.find((t) => t.answer) ?? null
      const turnRows = turns_.map((t) => [stamp(t.at), `${esc(t.kind)}${t.code ? ' · ' + esc(t.code) : ''}`, esc(t.sessionId ?? '—'), (t.toolCalls ?? []).length, (t.answer ?? '').length])
      return (
        kpis([
          ['SDK 通道', sdk.status ?? '—', sdk.serverInfo ? `${sdk.serverInfo.name} ${sdk.serverInfo.version}` : '未握手'],
          ['最近一轮', sdk.lastTurn ? sdk.lastTurn.kind : '—', sdk.lastTurn?.code ?? sdk.lastTurn?.at ?? ''],
          ['Headless 今日', `${headless.today?.total ?? 0} 次`, `成功 ${headless.today?.success ?? 0} / 失败 ${headless.today?.failed ?? 0}`],
          ['Headless 平均耗时', `${Math.round((headless.today?.avgMs ?? 0) / 1000)}s`, `熔断 kill ${headless.today?.killed ?? 0} 次`],
          ['决策来源', run ? '研究流水线' : '—', run ? stamp(run.asOf) : '尚未运行'],
          ['决策', top ? `${top.ticker} ${top.action}` : '—', top ? `目标 ${dash(top.targetWeightPct)}% · 风险 ${dash(top.riskLevel)}` : ''],
        ]) +
        card(
          'Harness 会话（真实回合）',
          assistantTexts.length > 0 || lastTurn
            ? `<pre class="answer">${esc((assistantTexts.at(-1) ?? lastTurn.answer).slice(0, 4000))}</pre>` +
              `<div class="note">以上为 SDK 通道真实模型输出${lastTurn ? `（回合 ${stamp(lastTurn.at)}，会话 ${esc(lastTurn.sessionId ?? '—')}，路由 ${esc(lastTurn.route?.provider ?? '—')}/${esc(lastTurn.route?.model ?? '—')}）` : ''}；工具调用 ${(assistantTexts.length > 0 ? toolCalls.length : (lastTurn.toolCalls ?? []).length)} 次</div>`
            : `<div class="note">尚无已完成的 SDK 回合。${esc(sdk.sessionWarning ?? '可点右侧按钮握手后发起任务')}</div>`,
          '<button class="btn" data-act="sdk-start">握手</button>',
        ) +
        card(
          '发起真实任务（SDK 通道）',
          '<div class="note">输入任务文本后提交，平台通过 JSON-RPC 驱动 Harness 运行时；回合结束后上方显示真实回答。</div>',
          '<input id="sdk-prompt" placeholder="例如：给出当前模拟盘的盘前检查清单" style="width:340px;background:var(--panel2);border:1px solid var(--border);border-radius:6px;padding:5px 8px;color:var(--text)"> <button class="btn" data-act="sdk-prompt">提交</button>',
        ) +
        card('SDK 回合记录（落盘）', table(['时间', '结果', '会话', '工具调用', '回答字数'], turnRows, '尚无持久化回合')) +
        card('工具调用分布（本进程计数）', bars(entries)) +
        card('Headless 调用明细', table(['时间', '结果', 'exit', '耗时', 'token 估算'], callRows)) +
        card(
          '决策提案（研究流水线）',
          run ? table(['标的', '动作', '目标权重', '风险', '依据'], (run.proposals ?? []).map((p) => [esc(p.ticker), p.action, `${dash(p.targetWeightPct)}%`, p.riskLevel, esc(String(p.basis ?? '').slice(0, 60))])) : '<div class="note">尚未运行流水线。</div>',
          '<button class="btn" data-act="strategy-run">运行流水线</button>',
        )
      )
    },

    async market() {
      const [headline, watch] = await Promise.all([api('/api/v3/market?period=1d&limit=120'), api('/api/v3/market/watchlist?n=6')])
      if (!headline.ok) return errBox('行情', headline.error)
      const bars_ = headline.data?.bars ?? []
      const last = bars_.at(-1) ?? {}
      const prev = bars_.at(-2) ?? {}
      const chg = prev.c && last.c ? ((last.c - prev.c) / prev.c) * 100 : null
      const rows = (watch.rows ?? []).map((r) => [esc(r.ticker), num(r.close), signed(r.changePct, 2, '%'), signed(r.mom20Pct, 2, '%'), num(r.peTtm), num(r.pb), dash(r.asOf)])
      return (
        kpis([
          ['标的', esc(headline.data?.ticker ?? '—'), esc(headline.data?.source ?? '')],
          ['最新收盘', num(last.c), `as_of ${dash(headline.data?.as_of)}`],
          ['日涨跌', signed(chg, 2, '%'), `前收 ${num(prev.c)}`, Number(chg) >= 0 ? 'pos' : 'neg'],
          ['K 线根数', headline.data?.count ?? bars_.length, '日线'],
          ['自选池覆盖', watch.rows?.length ?? 0, watch.sources?.factors ? `因子源 ${esc(watch.sources.factors)}` : ''],
        ]) +
        card('收盘序列（真实日 K）', bars_.length >= 2 ? lineChart(bars_.map((b) => b.c)) : '<div class="note">K 线不足，无法绘制</div>', `${esc(headline.data?.source ?? '—')} · as_of ${dash(headline.data?.as_of)}`) +
        card('自选池实时快照', table(['代码', '现价', '日涨跌%', '20日动量%', 'PE(TTM)', 'PB', 'as_of'], rows), watch.ok ? `${watch.rows.length} 只 · 逐票 K 线 + 因子` : '取数失败')
      )
    },

    async strategy() {
      const strategy = await api('/api/v3/strategy')
      const run = strategy?.run ?? null
      const stages = run?.stages ?? {}
      return (
        kpis([
          ['PDAT K 线数', stages.PDAT?.bars ?? '—', stages.PDAT?.errors?.length ? `取数失败 ${stages.PDAT.errors.length} 只` : '全部成功'],
          ['PAAT 因子覆盖', stages.PAAT ? `${stages.PAAT.withFactors}/${stages.PAAT.analyzed}` : '—', esc(stages.PAAT?.scoreSource ?? '')],
          ['PCPT 多头候选', (stages.PCPT?.longs ?? []).join(' ') || '—', ''],
          ['PCPT 减仓候选', (stages.PCPT?.reduces ?? []).join(' ') || '—', ''],
          ['PRT 单票权重', stages.PRT ? `${stages.PRT.weightPctPerName}%` : '—', stages.PRT?.capped ? '受单笔上限约束' : ''],
          ['PET 提案数', stages.PET?.proposals ?? '—', run ? stamp(run.asOf) : '尚未运行'],
        ]) +
        card(
          '流水线阶段',
          run
            ? table(['阶段', '结果'], [
                ['PDAT 数据准备', `${stages.PDAT?.bars ?? 0} 根 K 线 · 失败 ${stages.PDAT?.errors?.length ?? 0} 只`],
                ['PAAT 因子分析', `${stages.PAAT?.withFactors ?? 0}/${stages.PAAT?.analyzed ?? 0} 有因子 · 来源 ${esc(stages.PAAT?.scoreSource ?? '—')}`],
                ['PCPT 候选池', `多头 ${(stages.PCPT?.longs ?? []).length} · 减仓 ${(stages.PCPT?.reduces ?? []).length}`],
                ['PRT 组合', `单票 ${dash(stages.PRT?.weightPctPerName)}%`],
                ['PET 决策', `${stages.PET?.proposals ?? 0} 条提案`],
              ])
            : '<div class="note">尚未运行研究流水线。</div>',
          '<button class="btn" data-act="strategy-run">运行流水线</button>',
        ) +
        card('调仓建议提案', run ? table(['标的', '动作', '目标权重', '风险', '依据'], (run.proposals ?? []).map((p) => [esc(p.ticker), p.action, `${dash(p.targetWeightPct)}%`, p.riskLevel, esc(String(p.basis ?? '').slice(0, 70))])) : '<div class="note">无提案</div>') +
        card(
          '真实回测 / 参数扫描',
          '<div class="note">单标的动量 long/flat 回测，数据为富途日 K，PIT 对齐（t 日持仓只由 ≤t-1 收盘价决定）。</div><div id="bt-out"><div class="note">点右侧按钮运行。</div></div>',
          '<button class="btn" data-act="backtest">运行回测</button> <button class="btn" data-act="sweep">参数扫描</button>',
        )
      )
    },

    async risk() {
      const [risk, overview, metrics] = await Promise.all([api('/api/v3/risk'), api('/api/v3/overview'), api('/api/v3/metrics')])
      const config = risk?.data?.config ?? null
      const source = risk?.data?.source ?? null
      const equity = overview.equity ?? {}
      const dd = Number(equity.max_drawdown)
      const stages = metrics.oms ?? {}
      return (
        kpis([
          ['最大回撤（台账）', Number.isFinite(dd) ? signed(-Math.abs(dd) * 100, 2, '%') : '—', 'sim 台账', 'neg'],
          ['夏普（台账）', num(equity.sharpe), `成交 ${dash(equity.trades)} 笔`],
          ['待人工确认', stages.manual ?? 0, 'OMS 分级'],
          ['红线阻断', stages.blocked ?? 0, 'OMS 分级'],
          ['已提交', stages.submitted ?? 0, '工作台在途命中'],
          ['风控配置来源', source ? '工作台' : '—', esc(String(source ?? '').slice(0, 28))],
        ]) +
        card(
          '工作台风控配置（真实读取）',
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
              ) + `<div class="note">来源：${esc(source ?? '—')}。工作台未提供的组合风险量（VaR/CVaR/Beta/Alpha/IR）本页不展示，也不填占位值。</div>`
            : errBox('风控配置', risk?.error),
        ) +
        card('订单风控分级（OMS 台账）', bars(Object.entries(stages), 'var(--amber)')) +
        card('平台侧阈值（兜底）', table(['项', '阈值'], [['单笔交易上限', '2%（超过转人工确认）'], ['单一行业暴露', '20%（超过强制阻断）'], ['最大回撤', '15%（触及强制阻断）']]), '以工作台风控为准')
      )
    },

    async execution() {
      const d = await api('/api/v3/execution')
      if (!d.ok) return errBox('执行视图', d.error)
      const rowsOf = (p) => (Array.isArray(p) ? p : Array.isArray(p?.groups) ? p.groups.flatMap((g) => g.rows ?? []) : Array.isArray(p?.rows) ? p.rows : [])
      const positions = rowsOf(d.positions)
      const openOrders = rowsOf(d.orders_open)
      const deals = rowsOf(d.deals_today)
      const oms = d.oms ?? {}
      const orderRows = (oms.orders ?? []).map((o) => [
        `<span class="num">${esc(String(o.id).slice(0, 10))}</span>`,
        esc(o.ticker),
        o.side,
        dash(o.qty),
        money(o.value),
        tag(o.stage, o.stage === 'blocked' ? 'bad' : o.stage === 'manual' ? 'warn' : 'ok'),
        esc((o.risk?.reasons ?? ['阈值内'])[0] ?? ''),
      ])
      return (
        kpis([
          ['OMS 台账订单', (oms.orders ?? []).length, oms.nav ? `NAV ${money(oms.nav)}` : 'NAV 不可用'],
          ['待人工确认', oms.stages?.manual ?? 0, '超单笔阈值'],
          ['红线阻断', oms.stages?.blocked ?? 0, '触发红线'],
          ['券商持仓', positions.length, esc(d.positions?.source ?? '')],
          ['在途订单', openOrders.length, '工作台 orders_open'],
          ['今日成交', deals.length, '工作台 deals_today'],
          ['确认通道', oms.confirmation?.pending ? '有待确认' : '空闲', `TTL ${Math.round((oms.confirmation?.ttl_ms ?? 0) / 1000)}s`],
        ]) +
        card('订单台账与风控分级', table(['订单', '标的', '方向', '数量', '金额', '阶段', '风控结论'], orderRows), esc(oms.note ?? '')) +
        card('组合与成交', table(['代码', '数量', '成本', '现价'], positions.slice(0, 10).map((p) => [esc(p.ticker ?? p.symbol ?? '—'), dash(p.qty), dash(p.avg_cost ?? p.cost_price), dash(p.current ?? p.price)])), '来自工作台 8397') +
        card('执行边界', '<div class="note">本平台不含下单入口：orders_open / deals_today 为真实回读；执行只发生在既有工作台 Web 的「执行已冻结计划」+ 人工确认（live 需口令）。</div>') +
        card('对账', '<div class="note">把工作台 frozen 计划的订单登记/更新到台账，并回读确认通道状态。</div>', '<button class="btn" data-act="oms-sync">与工作台对账</button>')
      )
    },

    async gateway() {
      const [gateway, metrics] = await Promise.all([api('/api/v3/gateway'), api('/api/v3/metrics')])
      if (!gateway.ok) return errBox('网关', gateway.error)
      const ch = gateway.channels ?? {}
      const headless = gateway.headless ?? {}
      const sdk = ch.sdk ?? {}
      const rules = (gateway.scheduler?.rules ?? []).map((r) => [esc(r.id), r.at, esc(r.task)])
      const calls = (headless.last ?? []).map((c) => [hhmmss(c.started_at), c.success ? tag('成功', 'ok') : tag('失败', 'bad'), dash(c.exit_code), `${Math.round((c.duration_ms ?? 0) / 1000)}s`, dash(c.tokens_estimate)])
      return (
        kpis([
          ['MCP', ch.mcp?.status ?? '—', esc(ch.mcp?.protocol ?? '')],
          ['SDK', sdk.status ?? '—', sdk.serverInfo ? `${sdk.serverInfo.name} ${sdk.serverInfo.version}` : '未握手'],
          ['Headless', ch.headless?.status ?? '—', esc(ch.headless?.command ?? '')],
          ['熔断并发', `${headless.breaker?.active ?? 0}/${headless.breaker?.concurrencyLimit ?? 0}`, `排队 ${headless.breaker?.queued ?? 0}`],
          ['单次超时', `${Math.round((headless.breaker?.timeoutMs ?? 0) / 1000)}s`, `token 预算 ${Math.round((headless.breaker?.tokenBudgetPerCall ?? 0) / 1000)}K`],
          ['今日唤醒', `${headless.today?.total ?? 0} 次`, `成功 ${headless.today?.success ?? 0} / 失败 ${headless.today?.failed ?? 0}`],
          ['平台请求数', metrics.http?.requests ?? 0, `错误 ${metrics.http?.errors ?? 0}`],
          ['workbench', metrics.workbenchUp ? '在线' : '不可达', '数据源 8397'],
        ]) +
        card('定时规则', table(['规则', '触发点', '任务'], rules), '同日同规则只触发一次') +
        card('Headless 通道', '<div class="note">外部熔断：并发上限 / 单次超时 / token 预算均在平台侧强制，超限 kill 并记录（超时记 exit=124）。</div><div id="hl-out"></div>', '<button class="btn" data-act="headless-run">立即唤醒一次</button>') +
        card('Headless 调用记录', table(['时间', '结果', 'exit', '耗时', 'token 估算'], calls)) +
        card(
          'SDK 通道',
          table(['项', '值'], [
            ['协议', esc(ch.sdk?.protocol ?? '—')],
            ['运行时', sdk.serverInfo ? `${esc(sdk.serverInfo.name)} ${esc(sdk.serverInfo.version)}` : '—'],
            ['路由', `${esc(sdk.route?.provider ?? '—')} / ${esc(sdk.route?.model ?? '—')} / ${esc(sdk.route?.reasoningEffort ?? '—')}`],
            ['最近一轮', sdk.lastTurn ? `${esc(sdk.lastTurn.kind)}${sdk.lastTurn.code ? ' · ' + esc(sdk.lastTurn.code) : ''}` : '—'],
            ['密钥', metrics.sdk?.credentials?.modelKeyPresent ? '环境变量已注入' : '环境变量未注入（使用凭据库）'],
          ]),
          '<button class="btn" data-act="sdk-start">握手</button>',
        ) +
        card('调用分布（本进程）', bars([...Object.entries(metrics.mcp?.tools ?? {}), ...Object.entries(metrics.wb?.byTool ?? {}).map(([n, c]) => [`wb:${n}`, c])].sort((a, b) => b[1] - a[1]).slice(0, 8)))
      )
    },

    async tools() {
      const [tools, metrics] = await Promise.all([api('/api/v3/tools'), api('/api/v3/metrics')])
      if (!tools.ok) return errBox('工具目录', tools.error)
      const domains = tools.domains ?? {}
      const flat = Object.values(domains).flat()
      return (
        kpis([
          ['工具总数', tools.total, `覆盖 ${Object.keys(domains).length} 个域`],
          ['一级工具', flat.filter((t) => t.kind === 'first-class').length, '含本地计算工具'],
          ['直通工具', flat.filter((t) => t.kind === 'proxy').length, '工作台工具面'],
          ['MCP 调用', metrics.mcp?.calls ?? 0, `失败 ${metrics.mcp?.errors ?? 0}`],
          ['平均延迟', `${metrics.mcp?.avgMs ?? 0}ms`, 'tools/call 均值'],
          ['workbench 调用', metrics.wb?.calls ?? 0, `平均 ${metrics.wb?.avgMs ?? 0}ms`],
        ]) +
        card('工具发现代理', '<div class="note">对外只暴露 <b>list_tools</b> / <b>call_tool</b> 两个入口，避免上百个 schema 撑爆上下文；交易写类（trade_place / trade_modify / trade_cancel）不设一级工具，确认闸门在既有工作台。</div>') +
        card('调用计数（本进程）', bars(Object.entries(metrics.mcp?.tools ?? {}))) +
        Object.entries(domains)
          .map(([domain, list]) =>
            card(
              `${domain}（${list.length}）`,
              table(['工具', '类型', '工作台工具', '说明'], list.slice(0, 12).map((t) => [esc(t.name), t.kind === 'first-class' ? tag('一级', 'ok') : tag('直通'), esc(String(t.wb ?? '')), esc(String(t.desc ?? '').slice(0, 48))])),
            ),
          )
          .join('')
      )
    },

    async settings() {
      const [settings, metrics] = await Promise.all([api('/api/v3/settings'), api('/api/v3/metrics')])
      if (!settings.ok) return errBox('配置', settings.error)
      const futu = settings.futu ?? {}
      const bearer = futu.mcp_bearer ?? {}
      const envRows = (settings.env ?? []).map((e) => [esc(e.key), e.injected ? tag('已注入', 'ok') : tag('未注入', 'warn'), esc(e.source)])
      return (
        kpis([
          ['交易模式', esc(settings.trading_mode ?? '—'), '以工作台为准'],
          ['富途通道', esc(futu.channel ?? '—'), `OpenAPI 模式 ${esc(futu.openapi?.mode ?? '—')}`],
          ['MCP Bearer', bearer.present ? '有效' : '缺失', bearer.present ? `至 ${esc(String(bearer.expiry ?? '—'))}` : ''],
          ['配置目录', '已读取', 'QUANT_CONFIG_HOME'],
          ['工具总数', metrics.toolTotal ?? '—', `${metrics.toolDomains ?? '—'} 个域`],
          ['workbench', metrics.workbenchUp ? '在线' : '不可达', '数据源 8397'],
        ]) +
        card('交易模式与执行边界', `<div class="note">${esc(settings.mode_note ?? '')}</div>`) +
        card('富途授权（真实读取）', table(['项', '值'], [['渠道', esc(futu.channel ?? '—')], ['OpenAPI 模式', esc(futu.openapi?.mode ?? '—')], ['OpenAPI 配置键', esc((futu.openapi?.config_keys ?? []).join(', ') || '—')], ['MCP Bearer', bearer.present ? tag('有效', 'ok') : tag('缺失', 'bad')], ['有效期至', esc(String(bearer.expiry ?? '—'))], ['凭据来源', bearer.present ? '~/.dsh/futu-token' : '—']])) +
        card('环境变量注入状态', table(['变量', '状态', '来源'], envRows)) +
        card('数据源清单（复用既有配置）', table(['来源', '说明'], (settings.data_sources ?? []).map((s) => [esc(s), '既有项目配置，未复制未修改'])))
      )
    },
  }

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
