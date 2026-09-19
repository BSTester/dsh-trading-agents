// V3「策略与因子」页数据绑定。
// 设计稿 strategy.html 的 HTML/CSS 一字不动：研究流水线阶段、因子库 IC/IR、策略候选、
// 回测曲线、参数扫描热力图、策略生成器全部绑定 /api/v3/* 的真实值；接口没有的字段
// （分层年化多空、换手率、相关性、股票池、基准指数、置信度）显式标注「无数据源」。
/* global window, document */
;(function () {
  const V3 = window.V3
  const { num, signed, stamp, esc } = V3
  const $ = (selector, root) => (root || document).querySelector(selector)
  const $$ = (selector, root) => Array.prototype.slice.call((root || document).querySelectorAll(selector))
  const sectionOf = (id) => $('[data-od-id="' + id + '"]')
  const errText = (payload, fallback) => {
    const error = (payload && payload.error) || {}
    return String(error.message || error.code || fallback || '接口未返回原因')
  }
  const asArray = (value) => (Array.isArray(value) ? value : [])
  const uniqSorted = (list) => Array.from(new Set(list)).sort((a, b) => Number(a) - Number(b))

  // 设计稿有 5 个因子权重滑块 → 用 5 个真实因子（IC 由 /api/v3/factors/matrix 逐个返回）
  const IC_FACTORS = ['mom_20', 'mom_60', 'rsi_14', 'trend', 'liq_ratio']
  // 因子名 → 设计稿的类别列（真实因子名，类别按名字前缀归类，标题里写明）
  const CATEGORY = [
    { match: /^mom_/, label: '动量', color: 'var(--blue)' },
    { match: /^(pe_|pb|ps)/, label: '价值', color: 'var(--purple)' },
    { match: /^(rsi_|trend)/, label: '情绪', color: 'var(--amber)' },
    { match: /^(liq_|vol_|mdd_)/, label: '另类', color: 'var(--green)' },
  ]
  const STAGE_TEXT = {
    PDAT: '数据准备',
    PAAT: '因子分析',
    PCPT: '规则候选',
    PRT: '回测验证',
    PET: '评估产出',
  }

  const state = {
    ticker: 'SH.600519', run: null, ic: {}, matrix: null, sweep: null,
    candidates: [], selected: -1, backtest: null, backtestAt: null,
  }

  function purgeDemoScripts() {
    for (const node of $$('script:not([src])')) {
      if ((node.textContent || '').indexOf('示例') === -1) continue
      node.textContent = '/* 设计稿内联演示数据（STRATS / 研究轮计时 / 已提交回测占位）已由 strategy.js 的真实数据绑定取代 */'
    }
  }

  function categoryOf(name) {
    for (const item of CATEGORY) if (item.match.test(String(name))) return item
    return { label: '其他', color: 'var(--faint)' }
  }

  function cloneBind(selector, handler) {
    const node = $(selector)
    if (!node) return null
    const clone = node.cloneNode(true)
    node.replaceWith(clone)
    clone.addEventListener('click', handler)
    return clone
  }

  function status(node, kind, text) {
    if (!node) return
    node.className = 'submit-status ' + (kind || '')
    node.textContent = text
  }

  function dayLabel(value) {
    const text = stamp(value)
    return text && text !== '—' ? text.slice(0, 16) : '—'
  }

  // ---------------------------------------------------------------- 顶栏
  function renderTopbar(overview, metrics, brain) {
    const mode = String((overview && overview.mode) || '').toUpperCase()
    const env = $('.topbar .env')
    if (env && mode) env.textContent = mode
    const items = $$('.topbar .tb-mid .tb-item')
    const equity = (overview && overview.equity) || {}
    if (items[0]) {
      const node = $('.num', items[0])
      if (node) node.textContent = V3.money(equity.current)
    }
    const points = asArray(equity.points)
    const last = points.length ? points[points.length - 1] : null
    const prev = points.length > 1 ? points[points.length - 2] : null
    const daily = last && prev && Number(prev.equity) ? (Number(last.equity) / Number(prev.equity) - 1) * 100 : null
    if (items[1]) {
      const node = $('.num', items[1])
      if (node) {
        if (daily === null) {
          node.textContent = '无数据源'
          node.className = 'num'
          node.style.color = 'var(--faint)'
          node.title = '权益台账只有 ' + points.length + ' 个点位，日内涨跌无数据源'
        } else {
          node.textContent = signed(daily, 2, '%')
          node.className = 'num ' + (daily >= 0 ? 'up' : 'down')
        }
      }
    }
    const demo = $('.topbar .pill-demo')
    if (demo) {
      demo.className = 'pill'
      demo.style.color = 'var(--green)'
      demo.style.borderColor = 'rgba(63,185,80,.45)'
      demo.textContent = '真实数据'
      demo.title = '页面数字全部来自本服务 /api/v3/* 实时接口'
    }
    const mcpMs = metrics && metrics.ok && metrics.mcp ? metrics.mcp.avgMs : null
    const mcpOk = Number.isFinite(Number(mcpMs))
    const chans = $$('.topbar .chan')
    const specs = [
      { text: mcpOk ? 'MCP ' + num(mcpMs, 0) + 'ms' : 'MCP 无数据源', ok: mcpOk, title: 'workbench 工具面平均耗时（/api/v3/metrics）' },
      { text: 'SDK 无数据源', ok: false, title: ((brain && brain.sdk) || {}).reason || '本服务未挂载 SDK JSON-RPC 通道' },
      { text: 'Headless 无数据源', ok: false, title: ((brain && brain.headless) || {}).reason || '本服务未挂载 Headless CLI 子通道' },
    ]
    chans.forEach((node, index) => {
      const spec = specs[index]
      if (!spec) return
      node.innerHTML = '<i class="dot ' + (spec.ok ? 'dot-g' : 'dot-a') + '"></i>' + esc(spec.text)
      node.title = spec.title
    })
    const right = $('.topbar .tb-right')
    if (right) {
      const time = dayLabel(overview && overview.generated_at)
      right.innerHTML = '<span class="num">' + esc(time.slice(0, 10)) + '</span><span class="lbl">' + esc(time.slice(11) || '—') + '</span>'
      right.title = '平台服务生成时间'
    }
  }

  // --------------------------------------------------------- 研究流水线
  function renderPipeline(run) {
    const section = sectionOf('research-pipeline')
    if (!section) return
    const steps = $('.pipe-steps', section)
    const time = $('#runTime', section)
    if (time) {
      const clone = time.cloneNode(true)
      time.replaceWith(clone)
    }
    const stateNode = $('#runTime', section)
    if (!run) {
      if (steps) V3.nodata(steps, '研究流水线 PDAT→PET', 'GET /api/v3/strategy 返回 run=null：尚无落盘记录（可点「新建研究轮」跑一轮真实流水线）')
      if (stateNode) {
        stateNode.textContent = '无数据源'
        stateNode.className = 'val num'
      }
      return
    }
    const stages = run.stages || {}
    const pdat = stages.PDAT || {}
    const paat = stages.PAAT || {}
    const pcpt = stages.PCPT || {}
    const prt = stages.PRT || {}
    const pet = stages.PET || {}
    const universe = asArray(pdat.universe).length ? asArray(pdat.universe) : asArray(run.universe)
    const details = {
      PDAT: '标的池 ' + universe.length + ' 只 · 日 K ' + (pdat.bars === undefined ? '—' : pdat.bars) + ' 根',
      PAAT: '已分析 ' + (paat.analyzed === undefined ? '—' : paat.analyzed) + ' / 有因子 ' + (paat.withFactors === undefined ? '—' : paat.withFactors)
        + ' · ' + (paat.scoreSource || '—'),
      PCPT: '增持 ' + asArray(pcpt.longs).length + ' · 减持 ' + asArray(pcpt.reduces).length,
      PRT: '每标的 ' + (prt.weightPctPerName === undefined ? '—' : num(prt.weightPctPerName, 1)) + '% · 上限约束 '
        + (prt.capped ? '已启用' : '未启用'),
      PET: (pet.proposals === undefined ? asArray(run.proposals).length : pet.proposals) + ' 条调仓建议',
    }
    if (steps) {
      steps.innerHTML = ['PDAT', 'PAAT', 'PCPT', 'PRT', 'PET'].map((code, index) => (index ? '<div class="s-line ok"></div>' : '')
        + '<div class="step done"><div class="s-c">✓</div><div class="s-l"><b>' + code + '</b><span>'
        + esc(STAGE_TEXT[code] + ' · ' + details[code]) + '</span></div></div>').join('')
    }
    const infos = $$('.pipe-side .pi .val', section)
    if (infos[0]) {
      infos[0].textContent = 'as_of ' + stamp(run.asOf)
      infos[0].title = '研究轮标识：本服务只落盘一轮记录（/api/v3/strategy）'
    }
    if (stateNode) {
      stateNode.textContent = '已完成 · 提案 ' + asArray(run.proposals).length + ' 条'
      stateNode.className = 'val num'
      stateNode.title = '落盘时间 ' + stamp(run.asOf)
    }
    cloneBind('#btnNewRound', async () => {
      const button = $('#btnNewRound')
      if (!button || button.disabled) return
      const original = button.textContent
      button.disabled = true
      button.textContent = '运行中…'
      const payload = await V3.post('strategy/run', { topN: 2 })
      button.disabled = false
      button.textContent = original
      const note = $('.card-side', section)
      if (payload && payload.ok) {
        if (note) note.textContent = '已触发真实流水线：' + stamp((payload.run || {}).asOf)
        await render()
      } else if (note) {
        note.textContent = '流水线失败：' + errText(payload, 'POST /api/v3/strategy/run 失败')
      }
    })
    const side = $('.card-side', section)
    if (side) side.textContent = '研究轮记录 as_of ' + stamp(run.asOf)
  }

  // ------------------------------------------------------------- 因子库
  function factorStatus(ic) {
    const mean = Number(ic.meanIc)
    const ir = Number(ic.ir)
    if (!Number.isFinite(mean) || !Number.isFinite(ir)) return { text: '无数据源', cls: 'b-gray' }
    if (Math.abs(mean) < 0.03) return { text: '待观察', cls: 'b-gray' }
    if (Math.abs(ir) >= 0.3) return mean > 0 ? { text: '有效', cls: 'b-green' } : { text: '反向', cls: 'b-amber' }
    return { text: '衰减', cls: 'b-amber' }
  }

  function renderFactorLibrary(basePayload) {
    const section = sectionOf('factor-library')
    if (!section) return
    const table = $('table', section)
    const tbody = table ? $('tbody', table) : null
    const sub = $('.card-sub', section)
    const side = $('.card-side', section)
    const note = $('.tnote', section)
    if (!tbody) return
    const matrix = basePayload && basePayload.ok ? basePayload.matrix : null
    const factors = Object.keys(state.ic).filter((name) => state.ic[name] && state.ic[name].ok)
    if (factors.length === 0) {
      V3.nodata(tbody.closest('.tscroll') || tbody, '因子库', errText(basePayload, 'GET /api/v3/factors/matrix 未返回 IC'))
      if (sub) sub.textContent = '因子库：无数据源'
      return
    }
    const template = $('tr', tbody)
    const clone = template ? template.cloneNode(true) : null
    tbody.innerHTML = factors.map((name) => {
      const ic = state.ic[name]
      const status = factorStatus(ic)
      const category = categoryOf(name)
      const row = clone ? clone.cloneNode(true) : document.createElement('tr')
      const cells = $$('td', row)
      if (cells[0]) { cells[0].textContent = name; cells[0].className = 'f-name' }
      if (cells[1]) cells[1].innerHTML = '<span class="cat"><i class="cat-dot" style="background:' + category.color + '"></i>' + esc(category.label) + '</span>'
      if (cells[2]) cells[2].textContent = num(ic.meanIc, 4)
      if (cells[3]) cells[3].textContent = num(ic.ir, 2)
      if (cells[4]) { cells[4].textContent = '无数据源'; cells[4].className = 'num'; cells[4].title = '分层年化多空：接口未返回（无数据源）' }
      if (cells[5]) { cells[5].textContent = '—'; cells[5].title = '换手率：接口未返回（无数据源）' }
      if (cells[6]) { cells[6].textContent = '—'; cells[6].title = '相关性：接口未返回（无数据源）' }
      if (cells[7]) cells[7].innerHTML = '<span class="badge ' + status.cls + '" title="判定规则：|IC| < 0.03 → 待观察；|IR| ≥ 0.30 且 IC > 0 → 有效；|IR| ≥ 0.30 且 IC < 0 → 反向；否则 衰减（基于真实 IC/IR）">' + status.text + '</span>'
      return row.outerHTML
    }).join('')
    if (sub) {
      sub.textContent = '横截面 RankIC · forward ' + (state.ic[factors[0]].forwardDays === undefined ? '—' : state.ic[factors[0]].forwardDays)
        + ' 日 · 因子矩阵 ' + (matrix ? asArray(matrix.tickers).length + ' 只 × ' + asArray(matrix.factors).length + ' 因子' : '—')
        + ' · as_of ' + ((matrix && matrix.as_of) || '—') + ' · 来源 ' + ((matrix && matrix.source) || '/api/v3/factors/matrix')
    }
    if (side) {
      side.textContent = factors.length + ' 个因子已取 IC（样本 ' + (state.ic[factors[0]].observations === undefined ? '—' : state.ic[factors[0]].observations)
        + ' 期）· 分层年化多空 / 换手率 / 相关性：无数据源'
    }
    if (note) {
      note.textContent = '注：IC 为横截面 RankIC（forward ' + (state.ic[factors[0]].forwardDays === undefined ? '—' : state.ic[factors[0]].forwardDays)
        + ' 日）；数据源 workbench/ic（经 /api/v3/factors/matrix）。分层年化多空、换手率、相关性本服务未返回，标注「无数据源」而不填占位数字；状态标签由真实 IC/IR 按上文规则判定。'
    }
  }

  // --------------------------------------------------- 策略候选 + 回测曲线
  // 回测曲线的 svg 是 section 的直接子节点：先包一层容器，避免无数据源标注吃掉卡片头部
  function curveBox() {
    const section = sectionOf('backtest-curve')
    if (!section) return null
    const svg = $('svg.chart', section)
    if (svg && svg.parentElement !== section && svg.parentElement.classList.contains('curvebox')) return svg.parentElement
    if (svg) {
      const box = document.createElement('div')
      box.className = 'curvebox'
      section.insertBefore(box, svg)
      box.appendChild(svg)
      return box
    }
    return $('.curvebox', section)
  }

  function curveSvg() {
    const box = curveBox()
    if (!box) return null
    let svg = $('svg.chart', box)
    if (!svg) {
      svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
      svg.setAttribute('class', 'chart')
      svg.setAttribute('viewBox', '0 0 730 250')
      svg.setAttribute('role', 'img')
      box.appendChild(svg)
    }
    return svg
  }

  function drawCurve(metrics, equity, label) {
    const box = curveBox()
    const svg = curveSvg()
    if (!box || !svg) return
    const points = asArray(equity).map((point) => ({ t: point.t, v: Number(point.value) })).filter((point) => Number.isFinite(point.v))
    if (points.length < 2) {
      V3.nodata(box, '回测曲线', '回测返回的净值点位不足 2 个 · ' + label)
      return
    }
    const left = 44
    const right = 700
    const top = 26
    const bottom = 155
    const ddTop = 165
    const ddBottom = 212
    const min = Math.min.apply(null, points.map((point) => point.v))
    const max = Math.max.apply(null, points.map((point) => point.v))
    const span = max - min || 1
    const x = (index) => left + (index / (points.length - 1)) * (right - left)
    const y = (value) => bottom - ((value - min) / span) * (bottom - top)
    let peak = -Infinity
    const drawdowns = points.map((point) => {
      peak = Math.max(peak, point.v)
      return peak > 0 ? point.v / peak - 1 : 0
    })
    const worst = Math.min.apply(null, drawdowns.concat([0]))
    const ddScale = Math.abs(worst) || 1
    const ddY = (value) => ddTop + (Math.abs(value) / ddScale) * (ddBottom - ddTop)
    const parts = []

    for (let i = 0; i <= 4; i++) {
      const price = min + (span * i) / 4
      const py = y(price)
      parts.push('<line x1="' + left + '" y1="' + py.toFixed(1) + '" x2="' + right + '" y2="' + py.toFixed(1) + '" stroke="var(--border)" stroke-width="1"/>')
      parts.push('<text x="40" y="' + (py + 3.5).toFixed(1) + '" text-anchor="end" font-size="9.5" fill="var(--faint)"'
        + ' font-family="Menlo,Consolas,monospace">' + price.toFixed(3) + '</text>')
    }
    parts.push('<line x1="' + left + '" y1="' + ddTop + '" x2="' + right + '" y2="' + ddTop + '" stroke="var(--border2)" stroke-width="1"/>')
    parts.push('<text x="40" y="' + (ddTop + 3.5) + '" text-anchor="end" font-size="9.5" fill="var(--faint)" font-family="Menlo,Consolas,monospace">0%</text>')
    parts.push('<text x="40" y="' + (ddBottom + 3.5) + '" text-anchor="end" font-size="9.5" fill="var(--faint)" font-family="Menlo,Consolas,monospace">'
      + (worst * 100).toFixed(1) + '%</text>')

    const equityLine = points.map((point, index) => (index ? 'L' : 'M') + x(index).toFixed(1) + ',' + y(point.v).toFixed(1)).join(' ')
    const ddLine = drawdowns.map((value, index) => (index ? 'L' : 'M') + x(index).toFixed(1) + ',' + ddY(value).toFixed(1)).join(' ')
    parts.push('<path d="' + ddLine + ' L' + x(points.length - 1).toFixed(1) + ',' + ddTop + ' L' + x(0).toFixed(1) + ',' + ddTop + ' Z" fill="rgba(248,81,77,.16)"/>')
    parts.push('<path d="' + ddLine + '" fill="none" stroke="rgba(248,81,77,.55)" stroke-width="1"/>')
    parts.push('<path d="' + equityLine + '" fill="none" stroke="var(--blue)" stroke-width="2"/>')
    parts.push('<circle cx="' + x(points.length - 1).toFixed(1) + '" cy="' + y(points[points.length - 1].v).toFixed(1) + '" r="3" fill="var(--blue)"/>')
    parts.push('<text x="' + (right + 7) + '" y="' + (y(points[points.length - 1].v) + 3).toFixed(1) + '" font-size="10" fill="var(--blue)"'
      + ' font-family="Menlo,Consolas,monospace">' + points[points.length - 1].v.toFixed(3) + '</text>')

    const ticks = [0, Math.floor((points.length - 1) / 4), Math.floor((points.length - 1) / 2), Math.floor((points.length - 1) * 3 / 4), points.length - 1]
    for (const index of ticks) {
      parts.push('<line x1="' + x(index).toFixed(1) + '" y1="16" x2="' + x(index).toFixed(1) + '" y2="210" stroke="var(--border)" stroke-width="1" opacity=".55"/>')
      parts.push('<text x="' + x(index).toFixed(1) + '" y="232" text-anchor="middle" font-size="10" fill="var(--faint)"'
        + ' font-family="Menlo,Consolas,monospace">' + esc(String(points[index].t).slice(0, 7)) + '</text>')
    }
    const aria = label + ' 净值与回撤（真实 PIT 回测，' + points.length + ' 个交易日）'
    svg.setAttribute('aria-label', aria)
    svg.innerHTML = parts.join('')
  }

  function renderMetrics(metrics) {
    const ann = document.getElementById('mAnn')
    if (ann) {
      const value = metrics ? Number(metrics.annReturnPct) : null
      ann.textContent = Number.isFinite(value) ? signed(value, 2, '%') : '—'
      ann.className = 'm-v num ' + (Number.isFinite(value) ? (value >= 0 ? 'up' : 'down') : '')
    }
    const sharpe = document.getElementById('mSharpe')
    if (sharpe) sharpe.textContent = metrics ? num(metrics.sharpe, 2) : '—'
    const dd = document.getElementById('mDd')
    if (dd) {
      const value = metrics ? Number(metrics.maxDrawdownPct) : null
      dd.textContent = Number.isFinite(value) ? '−' + Math.abs(value).toFixed(2) + '%' : '—'
      dd.className = 'm-v num ' + (Number.isFinite(value) ? 'down' : '')
    }
    const win = document.getElementById('mWin')
    if (win) win.textContent = metrics && Number.isFinite(Number(metrics.winRatePct)) ? num(metrics.winRatePct, 1) + '%' : '—'
    const turn = document.getElementById('mTurn')
    if (turn) {
      turn.textContent = '无数据源'
      turn.title = '换手率：/api/v3/ml/backtest 未返回（无数据源）'
    }
    const note = sectionOf('backtest-curve') && $('.legend', sectionOf('backtest-curve'))
    if (note) {
      note.innerHTML = '<span><i style="background:var(--blue)"></i>策略净值（真实 PIT 回测）</span>'
        + '<span><i style="background:rgba(248,81,77,.6)"></i>回撤（由真实净值计算）</span>'
        + '<span style="color:var(--faint)">基准指数：无数据源</span>'
    }
  }

  function candidateRows(section) {
    let box = $('.strat-rows', section)
    if (box) return box
    box = document.createElement('div')
    box.className = 'strat-rows'
    const first = $('.strat-row', section)
    if (first) section.insertBefore(box, first)
    else section.appendChild(box)
    for (const row of $$('.strat-row', section)) box.appendChild(row)
    return box
  }

  function renderCandidates(run, sweep) {
    const section = sectionOf('strategy-list')
    if (!section) return
    const rows = []
    const grid = asArray(sweep && sweep.ok && sweep.grid)
      .filter((cell) => Number.isFinite(Number(cell.sharpe)))
      .sort((a, b) => Number(b.sharpe) - Number(a.sharpe))
    const best = (sweep && sweep.ok && sweep.best) || (grid[0] || null)
    for (const cell of grid.slice(0, 3)) {
      const isBest = Boolean(best && Number(cell.window) === Number(best.window) && Number(cell.rebalanceDays) === Number(best.rebalanceDays))
      rows.push({
        kind: 'sweep',
        name: '动量 long/flat · window=' + cell.window + ' / rebalance=' + cell.rebalanceDays,
        badge: isBest ? '网格最优' : '参数网格',
        badgeCls: isBest ? 'b-green' : 'b-blue',
        meta: 'window=' + cell.window + ' · rebalance=' + cell.rebalanceDays + ' · 夏普 ' + num(cell.sharpe, 3)
          + ' · 年化 ' + signed(cell.annReturnPct, 2, '%') + ' · 回撤 ' + num(cell.maxDrawdownPct, 2) + '% · 标的 ' + state.ticker,
        window: cell.window,
        rebalanceDays: cell.rebalanceDays,
      })
    }
    if (run) {
      rows.push({
        kind: 'pipeline',
        name: '研究流水线 · 综合动量选股（PDAT→PET）',
        badge: '已产出',
        badgeCls: 'b-green',
        meta: '最近一轮 ' + stamp(run.asOf) + ' · 提案 ' + asArray(run.proposals).length + ' 条 · 评分来源 '
          + (((run.stages || {}).PAAT || {}).scoreSource || '—') + ' · 回测曲线：无数据源（流水线不产出净值序列）',
      })
    }
    const box = candidateRows(section)
    if (rows.length === 0) {
      V3.nodata(box, '策略候选', errText(sweep, 'GET /api/v3/ml/sweep 未返回有效参数网格'))
      const side = $('.card-side', section)
      if (side) side.textContent = '策略候选：无数据源'
      return
    }
    state.candidates = rows
    const subtitle = $('.card-side', section)
    if (subtitle) {
      subtitle.textContent = '真实候选 ' + rows.length + ' 个（参数网格按夏普取前 3 + 研究流水线）· 点击行切换回测视图'
    }
    box.innerHTML = rows.map((row, index) => '<div class="strat-row' + (index === state.selected ? ' active' : '')
      + '" data-index="' + index + '"><div class="s-top"><span class="s-name">' + esc(row.name)
      + '</span><span class="badge ' + row.badgeCls + '">' + esc(row.badge) + '</span></div>'
      + '<div class="s-meta num">' + esc(row.meta) + '</div></div>').join('')
    $$('.strat-row', box).forEach((node, index) => node.addEventListener('click', () => selectCandidate(index)))
  }

  async function selectCandidate(index) {
    const row = state.candidates[index]
    if (!row) return
    state.selected = index
    $$('.strat-row').forEach((node) => node.classList.toggle('active', node.getAttribute('data-index') === String(index)))
    const sub = $('#curveSub')
    if (row.kind === 'pipeline') {
      if (sub) sub.textContent = row.name + ' · ' + row.meta
      renderMetrics(null)
      const box = curveBox()
      if (box) V3.nodata(box, '回测曲线', '研究流水线（PDAT→PET）只产出调仓建议提案，不产出净值序列；净值回测见动量 long/flat 参数网格')
      return
    }
    const payload = await V3.post('ml/backtest', {
      ticker: state.ticker, window: row.window, rebalanceDays: row.rebalanceDays, limit: 500,
    })
    if (!payload || !payload.ok) {
      if (sub) sub.textContent = row.name + ' · 回测失败：' + errText(payload, 'POST /api/v3/ml/backtest 失败')
      renderMetrics(null)
      const box = curveBox()
      if (box) V3.nodata(box, '回测曲线', errText(payload, '回测取数失败'))
      return
    }
    state.backtest = payload.metrics
    state.backtestAt = new Date().toISOString()
    if (sub) {
      sub.textContent = row.name + ' · ' + state.ticker + ' · window=' + row.window + ' / rebalance=' + row.rebalanceDays
        + ' · PIT 回测 ' + ((payload.metrics || {}).days === undefined ? '—' : payload.metrics.days) + ' 个交易日 · 来源 /api/v3/ml/backtest'
    }
    renderMetrics(payload.metrics)
    drawCurve(payload.metrics, payload.equity, row.name + ' ' + state.ticker)
  }

  // ------------------------------------------------------ 参数扫描热力图
  function renderHeatmap(sweep) {
    const section = sectionOf('param-scan-heatmap')
    if (!section) return
    const grid = $('.hm', section)
    const legend = $('.hm-legend', section)
    const note = $('.hm-note', section)
    if (!grid) return
    const cells = asArray(sweep && sweep.ok && sweep.grid)
    if (cells.length === 0) {
      V3.nodata(grid, '参数扫描热力图', errText(sweep, 'GET /api/v3/ml/sweep 未返回参数网格'))
      if (note) note.textContent = '参数网格：无数据源'
      return
    }
    const windows = uniqSorted(cells.map((cell) => cell.window))
    const rebalances = uniqSorted(cells.map((cell) => cell.rebalanceDays))
    const values = cells.map((cell) => Number(cell.sharpe)).filter(Number.isFinite)
    const min = values.length ? Math.min.apply(null, values) : 0
    const max = values.length ? Math.max.apply(null, values) : 1
    const best = sweep.best || null
    let html = '<div class="hm-colhead"></div>' + windows.map((window) => '<div class="hm-colhead">' + esc(window) + '</div>').join('')
    for (const rebalance of rebalances) {
      html += '<div class="hm-rowhead">' + esc(rebalance) + '</div>'
      for (const window of windows) {
        const cell = cells.find((item) => Number(item.window) === Number(window) && Number(item.rebalanceDays) === Number(rebalance))
        const sharpe = cell ? Number(cell.sharpe) : NaN
        if (!cell || !Number.isFinite(sharpe)) {
          html += '<div class="hm-cell" style="background:rgba(255,255,255,.03)" title="'
            + esc('window=' + window + ' rebalance=' + rebalance + '：回测无有效结果（无数据源）')
            + '"><span class="num" style="color:var(--faint)">—</span></div>'
          continue
        }
        const norm = max === min ? 0.5 : (sharpe - min) / (max - min)
        const alpha = (0.09 + norm * 0.54).toFixed(2)
        const background = sharpe >= 0 ? 'rgba(76,141,255,' + alpha + ')' : 'rgba(248,81,77,' + alpha + ')'
        const isBest = Boolean(best && Number(best.window) === Number(window) && Number(best.rebalanceDays) === Number(rebalance))
        html += '<div class="hm-cell' + (isBest ? ' best' : '') + '" style="background:' + background + '" title="'
          + esc('window=' + window + ' rebalance=' + rebalance + ' · 夏普 ' + sharpe + ' · 年化 ' + cell.annReturnPct
            + '% · 最大回撤 ' + cell.maxDrawdownPct + '%') + '"><span class="num">' + sharpe.toFixed(3)
          + '</span>' + (isBest ? '<em>最优</em>' : '') + '</div>'
      }
    }
    grid.innerHTML = html
    if (legend) legend.innerHTML = '<i></i>夏普 ' + num(min, 3) + ' → ' + num(max, 3) + '（蓝＝夏普≥0 / 红＝夏普<0）'
    if (note) {
      note.textContent = '共 ' + cells.length + ' 组参数（momentum_window × rebalance_days）· 标的 ' + state.ticker
        + ' · 真实日 K 回测（/api/v3/ml/sweep）' + (best ? ' · 最优 window=' + best.window + ' / rebalance=' + best.rebalanceDays + '（夏普 ' + num(best.sharpe, 3) + '）' : ' · 无最优格')
    }
  }

  // -------------------------------------------------------- 策略生成器
  function weightFromIc() {
    const names = IC_FACTORS.filter((name) => state.ic[name] && state.ic[name].ok)
    if (names.length === 0) return null
    const raw = names.map((name) => Math.abs(Number(state.ic[name].meanIc)) || 0)
    const total = raw.reduce((sum, value) => sum + value, 0)
    if (!(total > 0)) return null
    const exact = raw.map((value) => (value / total) * 100)
    const floors = exact.map((value) => Math.floor(value))
    let rest = 100 - floors.reduce((sum, value) => sum + value, 0)
    const order = exact.map((value, index) => ({ index, frac: value - floors[index] })).sort((a, b) => b.frac - a.frac)
    for (const item of order) {
      if (rest <= 0) break
      floors[item.index] += 1
      rest -= 1
    }
    return names.map((name, index) => ({ name, weight: floors[index], ic: state.ic[name] }))
  }

  function renderGenerator(risk) {
    const section = sectionOf('strategy-generator')
    if (!section) return
    const rows = $$('.w-row', section)
    const weights = weightFromIc()
    const side = $('.card-side', section)
    if (side) {
      side.textContent = '回测标的 ' + state.ticker + ' · 接口 /api/v3/ml/backtest（单标的动量 long/flat）· 因子列取值 /api/v3/factors/matrix'
    }
    rows.forEach((row, index) => {
      const label = $('.w-name', row)
      const slider = $('.w-slider', row)
      const shown = $('.w-val', row)
      const item = weights ? weights[index] : null
      if (label) {
        label.textContent = item ? item.name : '无数据源'
        label.title = item ? '真实因子（IC 均值 ' + num(item.ic.meanIc, 4) + '）' : '无数据源：因子 IC 未取到'
      }
      if (slider && item) slider.value = String(item.weight)
      if (slider && !item) slider.disabled = true
      if (shown) shown.textContent = item ? item.weight + '%' : '无数据源'
      if (!item) row.title = '无数据源：GET /api/v3/factors/matrix 未返回可用 IC'
    })
    const total = document.getElementById('wTotal')
    if (total) total.textContent = weights ? weights.reduce((sum, item) => sum + item.weight, 0) + '%' : '无数据源'
    const hint = document.getElementById('wHint')
    if (hint) hint.textContent = weights ? '（按真实 |IC| 归一化，可手工调整）' : ''

    const rebalance = document.getElementById('rebal')
    if (rebalance && state.sweep && state.sweep.best) {
      const want = String(state.sweep.best.rebalanceDays)
      let matched = false
      for (const option of $$('option', rebalance)) {
        const hit = String(option.textContent).replace(/[^0-9]/g, '') === want
        option.selected = hit
        matched = matched || hit
      }
      if (!matched) rebalance.title = '参数网格最优 rebalance=' + want + ' 不在设计稿下拉项内'
    }
    const pool = document.getElementById('pool')
    if (pool) {
      pool.innerHTML = '<option selected>无数据源 · 回测接口为单标的（' + esc(state.ticker) + '）</option>'
      pool.disabled = true
      pool.title = '股票池：/api/v3/ml/backtest 不接受股票池参数（无数据源）'
    }

    const label = $$('.f-label', section).filter((node) => String(node.textContent).indexOf('风控约束') >= 0)[0]
    if (label) {
      const readonly = $('.ro', label)
      if (readonly) readonly.textContent = '只读 · 来源 /api/v3/risk（workbench risk 配置）'
    }
    const chips = $('.chips', section)
    const config = (risk && risk.ok && risk.data && risk.data.config) || null
    if (chips) {
      if (!config) {
        V3.nodata(chips, '风控约束', errText(risk, 'GET /api/v3/risk 未返回风控配置'))
      } else {
        const items = [
          { label: '单笔风险', value: config.risk_per_trade, kind: 'pct' },
          { label: '单标的持仓上限', value: config.max_position_pct, kind: 'pct' },
          { label: '日亏损上限', value: config.daily_loss_limit_pct, kind: 'pct' },
          { label: '最大持仓数', value: config.max_positions, kind: 'count' },
        ].filter((item) => item.value !== undefined && item.value !== null)
        chips.innerHTML = items.map((item) => '<span class="chip">' + esc(item.label) + ' ≤ <b class="num">'
          + (item.kind === 'pct' ? esc(num(Number(item.value) * 100, 2)) + '%' : esc(item.value)) + '</b></span>').join('')
        chips.title = '真实风控配置来源：' + ((risk.data && risk.data.source) || '/api/v3/risk')
      }
    }

    cloneBind('#btnSubmit', async () => {
      const node = document.getElementById('submitStatus')
      const candidate = state.candidates[state.selected] || null
      const window = candidate && candidate.kind === 'sweep' ? candidate.window : ((state.sweep && state.sweep.best) || {}).window || 20
      const chosen = document.getElementById('rebal')
      const option = chosen && chosen.selectedOptions ? chosen.selectedOptions[0] : null
      const rebalanceDays = Number(String(option ? option.textContent : '').replace(/[^0-9]/g, '')) || 10
      status(node, '', '正在回测 ' + state.ticker + '（window=' + window + ' / rebalance=' + rebalanceDays + '）…')
      const payload = await V3.post('ml/backtest', { ticker: state.ticker, window, rebalanceDays, limit: 500 })
      if (!payload || !payload.ok) {
        status(node, 'err', '回测失败：' + errText(payload, 'POST /api/v3/ml/backtest 失败'))
        return
      }
      const metrics = payload.metrics || {}
      status(node, 'ok', '已完成真实回测：夏普 ' + num(metrics.sharpe, 3) + ' · 年化 ' + signed(metrics.annReturnPct, 2, '%')
        + ' · 最大回撤 ' + num(metrics.maxDrawdownPct, 2) + '% · 胜率 ' + num(metrics.winRatePct, 1) + '%（' + state.ticker
        + ' window=' + window + ' rebalance=' + rebalanceDays + '，' + (metrics.days === undefined ? '—' : metrics.days) + ' 个交易日）')
      state.backtest = metrics
      renderMetrics(metrics)
      drawCurve(metrics, payload.equity, '动量 long/flat ' + state.ticker)
      const sub = $('#curveSub')
      if (sub) sub.textContent = '动量 long/flat · ' + state.ticker + ' · window=' + window + ' / rebalance=' + rebalanceDays
        + ' · PIT 回测 ' + (metrics.days === undefined ? '—' : metrics.days) + ' 个交易日 · 来源 /api/v3/ml/backtest'
      await renderRounds(state.run, state.audit, { metrics, window, rebalanceDays })
    })
  }

  // ------------------------------------------------------ 研究轮记录
  async function renderRounds(run, entries, lastBacktest) {
    const section = sectionOf('research-rounds')
    if (!section) return
    const list = $('.rounds', section)
    const title = $('.card-title', section)
    const sub = $('.card-sub', section)
    if (!list) return
    if (title) title.textContent = '最近研究轮与审计链记录'
    if (sub) sub.textContent = '研究轮取自 /api/v3/strategy（只落盘最后一轮）；历史条目取自工作台审计链 /api/v3/audit'
    const rows = []
    if (run) {
      rows.push({
        time: stamp(run.asOf).slice(5, 16),
        text: 'PET · 评估产出 · ' + asArray(run.proposals).length + ' 条调仓建议',
        badge: '已完成', badgeCls: 'b-green', link: 'brain.html', linkText: '决策大脑',
      })
    }
    if (lastBacktest) {
      rows.push({
        time: stamp(lastBacktest.at || new Date().toISOString()).slice(5, 16),
        text: 'PRT · 动量回测 · ' + state.ticker + ' window=' + lastBacktest.window + ' / rebalance=' + lastBacktest.rebalanceDays
          + ' · 夏普 ' + num(lastBacktest.metrics.sharpe, 3),
        badge: '已完成', badgeCls: 'b-green', link: '#', linkText: '回测曲线',
      })
    }
    for (const entry of asArray(entries).slice(0, 2)) {
      const kind = String(entry.kind || 'audit')
      rows.push({
        time: stamp(entry.at).slice(5, 16),
        text: (entry.source_label || kind) + ' · ' + (entry.ticker ? entry.ticker + ' · ' : '') + String(entry.detail || ''),
        badge: kind === 'signal' ? '信号' : kind === 'fill' ? '成交' : kind === 'order-facts' ? '订单' : kind,
        badgeCls: kind === 'signal' ? 'b-blue' : kind === 'fill' ? 'b-green' : 'b-gray',
        link: 'execution.html', linkText: '执行与审批',
      })
    }
    if (rows.length === 0) {
      V3.nodata(list, '研究轮与审计链记录', 'GET /api/v3/strategy 无落盘记录，且 /api/v3/audit 未返回条目')
      return
    }
    list.innerHTML = rows.slice(0, 4).map((row) => '<li><span class="r-t num">' + esc(row.time)
      + '</span><span class="r-s">' + esc(row.text) + '</span><span class="badge ' + row.badgeCls + '">' + esc(row.badge)
      + '</span><a class="r-link" href="' + esc(row.link) + '">' + esc(row.linkText) + '</a></li>').join('')
  }

  // --------------------------------------------------------------- 主流程
  async function render() {
    const [overview, metrics, strategy, risk, audit, baseFactors] = await Promise.all([
      V3.api('overview'), V3.api('metrics'), V3.api('strategy'), V3.api('risk'), V3.api('audit'), V3.api('factors/matrix'),
    ])
    const run = strategy && strategy.ok ? strategy.run : null
    state.run = run
    state.audit = audit && audit.ok && audit.data ? audit.data.entries : []
    state.matrix = baseFactors && baseFactors.ok ? baseFactors.matrix : null
    state.ic = {}
    if (baseFactors && baseFactors.ok && baseFactors.ic) state.ic[baseFactors.ic.factor || IC_FACTORS[0]] = baseFactors.ic
    const universe = asArray(run && run.universe)
    state.ticker = universe.length ? String(universe[0]) : 'SH.600519'

    renderTopbar(overview, metrics, strategy)
    renderPipeline(run)

    const [sweep, ...icPayloads] = await Promise.all([
      V3.api('ml/sweep?ticker=' + encodeURIComponent(state.ticker) + '&windows=10,20,30,60&rebalance=5,10,20'),
      ...IC_FACTORS.slice(1).map((name) => V3.api('factors/matrix?factor=' + encodeURIComponent(name))),
    ])
    IC_FACTORS.slice(1).forEach((name, index) => {
      const payload = icPayloads[index]
      if (payload && payload.ok && payload.ic) state.ic[name] = payload.ic
    })
    state.sweep = sweep && sweep.ok ? sweep : null

    renderFactorLibrary(baseFactors)
    renderCandidates(run, sweep)
    renderHeatmap(state.sweep)
    renderGenerator(risk)
    await renderRounds(run, state.audit, null)
    if (state.candidates.length) await selectCandidate(0)

    const foot = $('footer.foot')
    if (foot) {
      foot.innerHTML = '<span>数据来源：/api/v3/strategy（研究流水线 PDAT→PET）· /api/v3/factors/matrix（因子矩阵 + 逐个因子 IC）· '
        + '/api/v3/ml/sweep（参数网格真实回测）· /api/v3/ml/backtest（PIT 净值回测）· /api/v3/risk（风控配置）· '
        + '/api/v3/audit（审计链）· /api/v3/metrics（通道计数）。取不到的项标注「无数据源」，页面不含占位数字。</span>'
        + '<span>量化决策平台 V3.0 · Harness-Centric 控制台</span>'
    }
  }

  purgeDemoScripts()
  render().catch((error) => {
    const main = $('main.main')
    if (main) V3.nodata(main, '策略与因子页面数据', String((error && error.message) || error))
  })
})()
