// V3「行情与信号」页数据绑定。
// 设计稿 market.html 的 HTML/CSS 一字不动：K 线用真实日/分钟 K（含成交量）重绘进设计稿
// 自己的 SVG 容器，表格/盘口/热力图/数据源健康全部写真实接口值；接口没有的字段（证券简称、
// 量比、换手率、主力净流入、板块涨跌幅、五档盘口）显式写「无数据源」+ 原因，绝不留占位数字。
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

  // 设计稿周期按钮 → 工作台 series 的 period 取值（1分/周线 工作台不支持，按服务端错误如实标注）
  const PERIOD_MAP = { '1分': '1m', '5分': '5m', '日线': '1d', '周线': '1w' }
  const GEO = { W: 880, H: 352, PADL: 14, PADR: 812, PT: 16, PB: 250, VT: 272, VB: 338 }
  const GREEN = 'var(--green)'
  const RED = 'var(--red)'
  // 设计稿因子列名 → 真实因子（矩阵里存在同名因子才取值，标题里写明对应关系）
  const FACTOR_COLUMNS = [
    { header: '价值', factor: 'pb' },
    { header: '成长', factor: 'peg' },
    { header: '动量', factor: 'mom_20' },
    { header: '质量', factor: 'mdd_60' },
    { header: '情绪', factor: 'rsi_14' },
    { header: '另类', factor: 'liq_ratio' },
  ]

  const state = {
    ticker: '', period: '日线', watch: [], watchErrors: [],
    matrix: null, composite: {}, bars: [], barsMeta: null, starred: {}, chart: null,
  }

  function purgeDemoScripts() {
    for (const node of $$('script:not([src])')) {
      if ((node.textContent || '').indexOf('示例') === -1) continue
      node.textContent = '/* 设计稿内联演示数据（WATCH 行情表 / KLINE 蜡烛 / 板块与盘口样例）已由 market.js 的真实数据绑定取代 */'
    }
  }

  function dayLabel(value) {
    const text = stamp(value)
    return text && text !== '—' ? text.slice(0, 10) : '—'
  }

  // ---------------------------------------------------------------- 顶栏
  function renderTopbar(overview, metrics, brain) {
    const mode = String((overview && overview.mode) || '').toUpperCase()
    const env = $('.topbar .env')
    if (env && mode) env.textContent = mode
    const items = $$('.topbar .tb-mid .tb-item')
    const equity = (overview && overview.equity) || {}
    if (items[0]) {
      const node = $('b', items[0])
      if (node) node.textContent = V3.money(equity.current)
    }
    const points = asArray(equity.points)
    const last = points.length ? points[points.length - 1] : null
    const prev = points.length > 1 ? points[points.length - 2] : null
    const daily = last && prev && Number(prev.equity) ? (Number(last.equity) / Number(prev.equity) - 1) * 100 : null
    if (items[1]) {
      const node = $('b', items[1])
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
    const demo = $('.topbar .pill.demo')
    if (demo) {
      demo.className = 'pill'
      demo.innerHTML = '<i class="dot g"></i>真实数据'
      demo.title = '页面数字全部来自本服务 /api/v3/* 实时接口'
    }
    const mcpMs = metrics && metrics.ok && metrics.mcp ? metrics.mcp.avgMs : null
    const mcpOk = Number.isFinite(Number(mcpMs))
    const sdkReason = ((brain && brain.sdk) || {}).reason || '本服务未挂载 SDK JSON-RPC 通道'
    const headlessReason = ((brain && brain.headless) || {}).reason || '本服务未挂载 Headless CLI 子通道'
    const hb = $('.topbar .hb')
    if (hb) {
      hb.innerHTML = '<span class="pill" title="workbench 工具面平均耗时（/api/v3/metrics）"><i class="dot ' + (mcpOk ? 'g' : 'a') + '"></i>MCP '
        + (mcpOk ? esc(num(mcpMs, 0)) + 'ms' : '无数据源') + '</span>'
        + '<span class="pill" title="' + esc(sdkReason) + '"><i class="dot a"></i>SDK 无数据源</span>'
        + '<span class="pill" title="' + esc(headlessReason) + '"><i class="dot a"></i>Headless 无数据源</span>'
    }
    const right = $('.topbar .tb-right')
    if (right) right.textContent = dayLabel(overview && overview.generated_at)
    const navfoot = $('.sidenav .navfoot')
    if (navfoot) navfoot.innerHTML = 'Agent Loop v3.0<br>MCP 通道在线 · SDK / Headless 未挂载 · ' + esc(mode || 'SIM') + ' 环境'
  }

  // --------------------------------------------------------------- K 线
  function ensureBox() {
    const svg = document.getElementById('ksvg')
    if (svg) {
      if (!svg.parentElement.classList.contains('chartbox')) {
        const wrap = document.createElement('div')
        wrap.className = 'chartbox'
        svg.parentElement.insertBefore(wrap, svg)
        wrap.appendChild(svg)
      }
      return svg.parentElement
    }
    return $('.chartcard .chartbox')
  }

  function chartNoData(what, why) {
    const box = ensureBox()
    if (box) V3.nodata(box, what, why)
  }

  function sma(values, window) {
    const out = new Array(values.length).fill(null)
    let sum = 0
    for (let i = 0; i < values.length; i++) {
      sum += values[i]
      if (i >= window) sum -= values[i - window]
      if (i >= window - 1) out[i] = sum / window
    }
    return out
  }

  function niceStep(value) {
    const power = Math.pow(10, Math.floor(Math.log(value) / Math.LN10))
    const base = value / power
    return (base < 1.5 ? 1 : base < 3 ? 2 : base < 7 ? 5 : 10) * power
  }

  function timeLabel(text, intraday) {
    const value = String(text || '')
    return intraday ? value.slice(5, 16) : value.slice(5, 10)
  }

  function drawChart() {
    const bars = state.bars
    if (bars.length < 2) {
      chartNoData('K 线', '接口返回的 K 线不足 2 根，无法绘图')
      return
    }
    const limit = Math.min(120, bars.length)
    const view = bars.slice(bars.length - limit)
    const closes = bars.map((bar) => Number(bar.c))
    const ma5 = sma(closes, 5)
    const ma20 = sma(closes, 20)
    const start = bars.length - limit
    const intraday = state.period !== '日线'
    const slot = (GEO.PADR - GEO.PADL) / view.length

    let min = Infinity
    let max = -Infinity
    for (const bar of view) {
      min = Math.min(min, Number(bar.l))
      max = Math.max(max, Number(bar.h))
    }
    for (let i = 0; i < view.length; i++) {
      for (const series of [ma5, ma20]) {
        const value = series[start + i]
        if (value === null || value === undefined) continue
        min = Math.min(min, value)
        max = Math.max(max, value)
      }
    }
    if (!Number.isFinite(min) || !Number.isFinite(max)) {
      chartNoData('K 线', 'K 线高低价字段缺失')
      return
    }
    const pad = (max - min) * 0.07 || 1
    min -= pad
    max += pad
    const Y = (price) => GEO.PB - ((price - min) / (max - min)) * (GEO.PB - GEO.PT)
    const x = (index) => GEO.PADL + (index + 0.5) * slot
    const parts = []

    // 价格网格 + 右侧刻度（真实价格）
    const step = niceStep((max - min) / 6)
    const digits = step >= 1 ? 0 : step >= 0.1 ? 1 : 2
    for (let price = Math.ceil(min / step) * step; price < max; price += step) {
      const y = Y(price)
      parts.push('<line x1="' + GEO.PADL + '" y1="' + y.toFixed(1) + '" x2="' + GEO.PADR + '" y2="' + y.toFixed(1)
        + '" style="stroke:var(--border)" stroke-dasharray="2 4"/>')
      parts.push('<text x="' + (GEO.PADR + 6) + '" y="' + (y + 4).toFixed(1) + '" font-size="10" class="num" style="fill:var(--faint)">'
        + price.toFixed(digits) + '</text>')
    }

    // 蜡烛（真实 o/h/l/c）
    for (let i = 0; i < view.length; i++) {
      const bar = view[i]
      const up = Number(bar.c) >= Number(bar.o)
      const color = up ? GREEN : RED
      const px = x(i)
      parts.push('<line x1="' + px.toFixed(1) + '" y1="' + Y(Number(bar.h)).toFixed(1) + '" x2="' + px.toFixed(1)
        + '" y2="' + Y(Number(bar.l)).toFixed(1) + '" style="stroke:' + color + '"/>')
      const yOpen = Y(Number(bar.o))
      const yClose = Y(Number(bar.c))
      const body = Math.max(1.2, Math.abs(yOpen - yClose))
      const width = Math.max(1.2, slot * 0.62)
      parts.push('<rect x="' + (px - width / 2).toFixed(1) + '" y="' + Math.min(yOpen, yClose).toFixed(1)
        + '" width="' + width.toFixed(1) + '" height="' + body.toFixed(1) + '" style="fill:' + color + '"><title>'
        + esc(bar.t + ' 开' + num(bar.o) + ' 高' + num(bar.h) + ' 低' + num(bar.l) + ' 收' + num(bar.c)) + '</title></rect>')
    }

    // MA5 / MA20（真实收盘均值）
    const line = (series, color) => {
      const points = []
      for (let i = 0; i < view.length; i++) {
        const value = series[start + i]
        if (value === null || value === undefined) continue
        points.push(x(i).toFixed(1) + ',' + Y(value).toFixed(1))
      }
      return points.length < 2 ? '' : '<polyline points="' + points.join(' ') + '" style="fill:none;stroke:' + color + '" stroke-width="1.4"/>'
    }
    parts.push(line(ma5, 'var(--cyan)') + line(ma20, 'var(--amber)'))

    // 最新价虚线 + 价签
    const lastBar = view[view.length - 1]
    const lastClose = Number(lastBar.c)
    const prevClose = Number(view[view.length - 2].c)
    const lastColor = lastClose >= prevClose ? GREEN : RED
    const lastY = Y(lastClose)
    const tag = lastClose.toFixed(2)
    const tagWidth = tag.length * 6.2 + 10
    parts.push('<line x1="' + GEO.PADL + '" y1="' + lastY.toFixed(1) + '" x2="' + GEO.PADR + '" y2="' + lastY.toFixed(1)
      + '" style="stroke:' + lastColor + '" stroke-dasharray="3 3" opacity=".5"/>')
    parts.push('<rect x="' + (GEO.PADR + 2) + '" y="' + (lastY - 8).toFixed(1) + '" width="' + tagWidth.toFixed(0)
      + '" height="15" rx="3" style="fill:rgba(248,81,77,.16);stroke:' + lastColor + '" stroke-width=".8"/>')
    parts.push('<text x="' + (GEO.PADR + 2 + tagWidth / 2).toFixed(1) + '" y="' + (lastY + 3.5).toFixed(1)
      + '" font-size="10" text-anchor="middle" class="num" style="fill:' + lastColor + '">' + tag + '</text>')

    // 成交量（真实 v，上游未标注单位）
    parts.push('<line x1="' + GEO.PADL + '" y1="' + (GEO.VT - 10) + '" x2="' + GEO.PADR + '" y2="' + (GEO.VT - 10) + '" style="stroke:var(--border)"/>')
    let volumeMax = 0
    for (const bar of view) volumeMax = Math.max(volumeMax, Number(bar.v) || 0)
    for (let i = 0; i < view.length; i++) {
      const bar = view[i]
      const height = volumeMax > 0 ? (Number(bar.v) || 0) / volumeMax * (GEO.VB - GEO.VT) : 0
      const width = Math.max(1.2, slot * 0.62)
      const up = Number(bar.c) >= Number(bar.o)
      parts.push('<rect x="' + (x(i) - width / 2).toFixed(1) + '" y="' + (GEO.VB - height).toFixed(1)
        + '" width="' + width.toFixed(1) + '" height="' + height.toFixed(1) + '" style="fill:' + (up ? GREEN : RED) + '" opacity=".55"><title>'
        + esc(bar.t + ' 量 ' + num(bar.v, 0)) + '</title></rect>')
    }
    parts.push('<text x="' + (GEO.PADR + 6) + '" y="' + (GEO.VT + 2) + '" font-size="9" style="fill:var(--faint)">成交量（原始值）</text>')

    // 时间刻度（真实 t）
    const ticks = [0, Math.floor((view.length - 1) / 4), Math.floor((view.length - 1) / 2), Math.floor((view.length - 1) * 3 / 4), view.length - 1]
    for (const index of ticks) {
      parts.push('<text x="' + x(index).toFixed(1) + '" y="350" font-size="10" text-anchor="middle" style="fill:var(--faint)">'
        + esc(timeLabel(view[index].t, intraday)) + '</text>')
    }

    // 十字光标
    parts.push('<g id="xh" opacity="0"><line id="xv" x1="0" y1="' + GEO.PT + '" x2="0" y2="' + GEO.VB
      + '" style="stroke:var(--border2)" stroke-dasharray="3 3"/><line id="xhz" x1="' + GEO.PADL + '" y1="0" x2="'
      + GEO.PADR + '" y2="0" style="stroke:var(--border2)" stroke-dasharray="3 3"/><circle id="xd" r="2.5" style="fill:var(--blue)"/></g>')
    parts.push('<rect id="hit" x="' + GEO.PADL + '" y="' + GEO.PT + '" width="' + (GEO.PADR - GEO.PADL) + '" height="'
      + (GEO.VB - GEO.PT) + '" fill="transparent" pointer-events="all"/>')

    const box = ensureBox()
    if (!box) return
    box.innerHTML = '<svg id="ksvg" viewBox="0 0 ' + GEO.W + ' ' + GEO.H + '" preserveAspectRatio="xMidYMid meet" role="img" aria-label="'
      + esc(state.ticker + ' ' + state.period + ' K 线与成交量（真实数据）') + '">' + parts.join('') + '</svg>'
    state.chart = { Y, view, slot, start, ma5, ma20, lastIndex: view.length - 1 }
    bindCrosshair()
    readout(view.length - 1)
  }

  function readout(index) {
    const chart = state.chart
    if (!chart) return
    const bar = chart.view[index]
    if (!bar) return
    const intraday = state.period !== '日线'
    V3.setText(document, '#lgDate', intraday ? String(bar.t).slice(5, 16) : String(bar.t).slice(5, 10))
    V3.setText(document, '#lgO', num(bar.o))
    V3.setText(document, '#lgH', num(bar.h))
    V3.setText(document, '#lgL', num(bar.l))
    const closeNode = document.getElementById('lgC')
    if (closeNode) {
      closeNode.textContent = num(bar.c)
      closeNode.className = Number(bar.c) >= Number(bar.o) ? 'up' : 'down'
    }
    const volume = document.getElementById('lgV')
    if (volume) {
      volume.textContent = num(bar.v, 0)
      volume.title = '上游未标注成交量单位，原样展示'
    }
    const m5 = chart.ma5[chart.start + index]
    const m20 = chart.ma20[chart.start + index]
    V3.setText(document, '#lgMA5', m5 === null || m5 === undefined ? '—' : num(m5))
    V3.setText(document, '#lgMA20', m20 === null || m20 === undefined ? '—' : num(m20))
  }

  function bindCrosshair() {
    const svg = document.getElementById('ksvg')
    const hit = document.getElementById('hit')
    if (!svg || !hit || !state.chart) return
    const chart = state.chart
    hit.addEventListener('mousemove', (event) => {
      const rect = svg.getBoundingClientRect()
      if (!rect.width || !rect.height) return
      const sx = ((event.clientX - rect.left) * GEO.W) / rect.width
      const sy = ((event.clientY - rect.top) * GEO.H) / rect.height
      let index = Math.floor((sx - GEO.PADL) / chart.slot)
      index = Math.max(0, Math.min(chart.view.length - 1, index))
      const px = GEO.PADL + (index + 0.5) * chart.slot
      const group = document.getElementById('xh')
      if (group) group.setAttribute('opacity', '1')
      const vLine = document.getElementById('xv')
      if (vLine) { vLine.setAttribute('x1', px); vLine.setAttribute('x2', px) }
      const y = Math.max(GEO.PT, Math.min(GEO.PB, sy))
      const hLine = document.getElementById('xhz')
      if (hLine) { hLine.setAttribute('y1', y); hLine.setAttribute('y2', y) }
      const dot = document.getElementById('xd')
      if (dot) {
        dot.setAttribute('cx', px)
        dot.setAttribute('cy', chart.Y(Number(chart.view[index].c)).toFixed(1))
      }
      readout(index)
    })
    hit.addEventListener('mouseleave', () => {
      const group = document.getElementById('xh')
      if (group) group.setAttribute('opacity', '0')
      readout(chart.view.length - 1)
    })
  }

  function renderQuoteHeader() {
    const bars = state.bars
    const last = bars.length ? bars[bars.length - 1] : null
    const prev = bars.length > 1 ? bars[bars.length - 2] : null
    const change = last && prev ? (Number(last.c) / Number(prev.c) - 1) * 100 : null
    const name = document.getElementById('chName')
    if (name) {
      name.textContent = state.ticker || '—'
      name.title = '证券简称：接口未返回（无数据源），此处显示真实代码'
    }
    V3.setText(document, '#chCode', state.ticker || '—')
    V3.setText(document, '#chPeriod', state.period)
    V3.setText(document, '#chPrev', prev ? '昨收 ' + num(prev.c) : '昨收 —')
    const price = document.getElementById('chPrice')
    if (price) {
      price.textContent = last ? num(last.c) : '—'
      price.className = 'big num ' + (change === null || change >= 0 ? 'up' : 'down')
    }
    const chg = document.getElementById('chChg')
    if (chg) {
      chg.textContent = change === null ? '无数据源' : signed(change, 2, '%')
      chg.className = 'num ' + (change === null || change >= 0 ? 'up' : 'down')
    }
    const foot = $('.chartcard .chartfoot')
    if (foot) {
      foot.innerHTML = '<span><i class="dot g"></i>K 线 <b class="num">' + esc(bars.length) + '</b> 根</span>'
        + '<span>数据源 <b>' + esc((state.barsMeta && state.barsMeta.source) || '—') + '</b></span>'
        + '<span class="faint">as_of ' + esc((state.barsMeta && state.barsMeta.as_of) || '—')
        + ' · 悬停图表查看十字光标读数 · 点击自选行可切换主图标的 · 买卖点信号：无数据源（接口未返回逐 bar 信号）</span>'
    }
  }

  async function loadBars() {
    const period = PERIOD_MAP[state.period] || '1d'
    const payload = await V3.api('market?ticker=' + encodeURIComponent(state.ticker)
      + '&period=' + encodeURIComponent(period) + '&limit=160')
    if (!payload || !payload.ok) {
      state.bars = []
      state.barsMeta = null
      renderQuoteHeader()
      chartNoData('K 线（' + state.ticker + ' ' + state.period + '）', errText(payload, 'GET /api/v3/market 失败'))
      const ts = document.getElementById('ts')
      if (ts) ts.textContent = '无数据源（' + errText(payload, '取数失败') + '）'
      return
    }
    const data = payload.data || {}
    state.bars = asArray(data.bars)
    state.barsMeta = data
    renderQuoteHeader()
    drawChart()
    const ts = document.getElementById('ts')
    if (ts) ts.textContent = stamp(data.as_of) + ' · 来源 ' + (data.source || '—') + ' · ' + (data.count || state.bars.length) + ' 根'
  }

  // --------------------------------------------------------------- 盘口
  function renderBook(orderbook, row) {
    const section = sectionOf('sec-2')
    if (!section) return
    V3.setText(document, '#bookSym', state.ticker || '—')
    const last = state.bars.length ? state.bars[state.bars.length - 1] : null
    const prev = state.bars.length > 1 ? state.bars[state.bars.length - 2] : null
    const change = row && Number.isFinite(Number(row.changePct))
      ? Number(row.changePct)
      : (last && prev ? (Number(last.c) / Number(prev.c) - 1) * 100 : null)
    const lastNode = document.getElementById('bookLast')
    if (lastNode) {
      lastNode.textContent = last ? num(last.c) : (row ? num(row.close) : '—')
      lastNode.className = 'num ' + (change === null || change >= 0 ? 'up' : 'down')
    }
    const chgNode = document.getElementById('bookChg')
    if (chgNode) {
      chgNode.textContent = change === null ? '无数据源' : signed(change, 2, '%')
      chgNode.className = 'num ' + (change === null || change >= 0 ? 'up' : 'down')
    }
    const asks = document.getElementById('bookAsks')
    const bids = document.getElementById('bookBids')
    if (bids) bids.replaceChildren()
    if (asks) {
      if (orderbook && orderbook.ok) {
        const book = orderbook.data || {}
        const rows = asArray(book.asks).slice(0, 5)
        asks.innerHTML = rows.map((item, index) => '<div class="brow a"><i class="bar" style="width:'
          + Math.max(4, 100 - index * 16) + '%"></i><span class="bl">卖' + (rows.length - index) + '</span>'
          + '<b class="num bp">' + esc(num(item.price ?? item[0])) + '</b><span class="num bv">' + esc(num(item.volume ?? item[1], 0)) + '</span></div>').join('')
      } else {
        // 富途未开通实时行情权限：errcode=-9 realtime quote permission required → 如实标注
        V3.nodata(asks, '五档盘口', errText(orderbook, 'GET /api/v3/orderbook 失败'))
      }
    }
    for (const node of $$('.bsum b', section)) node.textContent = '无数据源'
    for (const node of $$('.bsum label', section)) node.title = '五档快照派生指标：盘口无数据源'
    const note = $('.booknote', section)
    if (note) {
      note.textContent = '五档快照：无数据源 · ' + (orderbook && orderbook.ok ? '接口返回的盘口字段未包含委比/内外盘'
        : errText(orderbook, '取数失败')) + '（富途实时行情权限未开通）· 最新价为真实最近收盘'
    }
  }

  // ------------------------------------------------------- 自选行情表
  function factorOf(ticker, factor) {
    const matrix = state.matrix
    if (!matrix || !Array.isArray(matrix.matrix)) return null
    const rowIndex = asArray(matrix.tickers).indexOf(ticker)
    const colIndex = asArray(matrix.factors).indexOf(factor)
    if (rowIndex < 0 || colIndex < 0) return null
    const value = asArray(matrix.matrix[rowIndex])[colIndex]
    return value === null || value === undefined || !Number.isFinite(Number(value)) ? null : Number(value)
  }

  function compositeOf(ticker) {
    const matrix = state.matrix
    if (!matrix || !Array.isArray(matrix.matrix)) return null
    const rowIndex = asArray(matrix.tickers).indexOf(ticker)
    if (rowIndex < 0) return null
    const row = asArray(matrix.matrix[rowIndex]).map(Number).filter(Number.isFinite)
    if (row.length === 0) return null
    return row.reduce((sum, value) => sum + value, 0) / row.length
  }

  function signalBadge(score) {
    if (score === null || score === undefined) return { text: '无数据源', cls: 'b-flat' }
    if (score >= 0.25) return { text: '看多', cls: 'b-long' }
    if (score <= -0.25) return { text: '看空', cls: 'b-short' }
    return { text: '中性', cls: 'b-flat' }
  }

  function renderWatch(filter) {
    const body = document.getElementById('watchBody')
    if (!body) return
    const keyword = String(filter || '').trim().toLowerCase()
    const rows = state.watch.filter((row) => !keyword || String(row.ticker).toLowerCase().indexOf(keyword) >= 0)
    if (rows.length === 0) {
      body.innerHTML = '<tr><td colspan="9" style="text-align:left;color:var(--faint)">'
        + (state.watch.length === 0 ? '自选池：无数据源（GET /api/v3/market/watchlist 未返回任何标的）' : '无匹配标的') + '</td></tr>'
      return
    }
    body.innerHTML = rows.map((row) => {
      const change = Number(row.changePct)
      const score = compositeOf(row.ticker)
      const badge = signalBadge(score)
      const starred = Boolean(state.starred[row.ticker])
      return '<tr data-code="' + esc(row.ticker) + '" class="' + (row.ticker === state.ticker ? 'cur' : '') + '" title="as_of ' + esc(row.asOf || '—') + '">'
        + '<td class="num">' + esc(row.ticker) + '</td>'
        + '<td style="color:var(--faint)">—</td>'
        + '<td class="num">' + esc(num(row.close)) + '</td>'
        + '<td class="num ' + (change >= 0 ? 'up' : 'down') + '">' + esc(signed(change, 2, '%')) + '</td>'
        + '<td class="num" style="color:var(--faint)" title="量比：接口未返回（无数据源）">—</td>'
        + '<td class="num" style="color:var(--faint)" title="换手率：接口未返回（无数据源）">—</td>'
        + '<td class="num" style="color:var(--faint)" title="主力净流入：接口未返回（无数据源）">—</td>'
        + '<td><span class="badge ' + badge.cls + '" title="'
        + esc(score === null ? '综合分无数据源（因子矩阵未覆盖该标的）' : '综合分 = 因子 z 均值 ' + num(score, 3) + '（阈值 ±0.25）') + '">' + badge.text + '</span></td>'
        + '<td><button class="star ' + (starred ? 'on' : '') + '" data-star="' + esc(row.ticker) + '" title="本地标记（未持久化到服务端）">'
        + (starred ? '★' : '☆') + '</button></td></tr>'
    }).join('')
  }

  function renderWatchHead(errors) {
    const section = sectionOf('sec-3')
    if (!section) return
    const subs = $$('.chead .sub', section)
    if (subs[0]) {
      subs[0].textContent = state.watch.length + ' 只 · 点击行切换主图与盘口标的 · 证券简称 / 量比 / 换手率 / 主力净流入：无数据源（自选快照接口未返回）'
      if (asArray(errors).length) subs[0].title = '取数失败标的：' + asArray(errors).map((item) => item.ticker).join('、')
    }
    if (subs[1]) {
      const asOf = state.watch.length ? state.watch[0].asOf : null
      subs[1].textContent = '快照 as_of ' + (asOf || '—')
    }
  }

  // ------------------------------------------------------- 多因子信号表
  function factorCell(value) {
    if (value === null || value === undefined) {
      return '<span class="fcell" title="该因子无数据源"><span class="ftrack"></span><span class="num fv">—</span></span>'
    }
    const width = Math.min(22, Math.abs(value) * 22)
    const positive = value >= 0
    return '<span class="fcell"><span class="ftrack"><i class="ffill" style="' + (positive ? 'left:50%' : 'right:50%')
      + ';width:' + width.toFixed(1) + 'px;background:' + (positive ? GREEN : RED) + '"></i></span>'
      + '<span class="num fv">' + signed(value, 2) + '</span></span>'
  }

  function renderFactors() {
    const body = document.getElementById('factBody')
    const section = sectionOf('sec-4')
    if (!body || !section) return
    const matrix = state.matrix
    const subs = $$('.chead .sub', section)
    const ic = (state.ic && state.ic.ok) ? state.ic : null
    if (!matrix || !Array.isArray(matrix.matrix)) {
      V3.nodata(body.closest('.tscroll') || body, '多因子信号', errText(state.factorError, 'GET /api/v3/factors/matrix 失败'))
      if (subs[0]) subs[0].textContent = '多因子信号：无数据源'
      return
    }
    const tickers = asArray(matrix.tickers)
    const available = asArray(matrix.factors)
    const active = FACTOR_COLUMNS.filter((column) => available.indexOf(column.factor) >= 0)
    body.innerHTML = tickers.map((ticker) => {
      const cells = FACTOR_COLUMNS.map((column) => '<td title="' + esc(column.header + ' ← ' + column.factor
        + (available.indexOf(column.factor) >= 0 ? '（workbench/factors z）' : '：该因子无数据源')) + '">'
        + factorCell(factorOf(ticker, column.factor)) + '</td>').join('')
      const score = compositeOf(ticker)
      const badge = signalBadge(score)
      return '<tr><td class="num">' + esc(ticker) + '</td>'
        + '<td style="color:var(--faint)" title="证券简称：接口未返回（无数据源）">—</td>'
        + cells
        + '<td class="num" style="font-weight:700"><span class="' + (score !== null && score >= 0 ? 'up' : 'down') + '">'
        + (score === null ? '—' : signed(score, 2)) + '</span></td>'
        + '<td class="num" style="color:var(--faint)" title="全市场排名：矩阵只含请求标的，无排名数据源">—</td>'
        + '<td><span class="badge ' + badge.cls + '">' + badge.text + '</span></td></tr>'
    }).join('')
    if (subs[0]) {
      subs[0].textContent = '横截面因子 z 矩阵 · ' + tickers.length + ' 只 × ' + available.length + ' 因子 · 来源 '
        + (matrix.source || '/api/v3/factors/matrix') + ' · as_of ' + (matrix.as_of || '—')
        + (ic ? ' · IC（' + (ic.factor || '—') + '，forward ' + (ic.forwardDays === undefined ? '—' : ic.forwardDays)
          + ' 日）均值 ' + num(ic.meanIc, 4) + ' · 样本 ' + (ic.observations === undefined ? '—' : ic.observations) + ' 期' : '')
    }
    if (subs[1]) {
      subs[1].textContent = '列取值（真实因子）：' + active.map((column) => column.header + '←' + column.factor).join(' / ')
        + (active.length < FACTOR_COLUMNS.length ? ' · 其余列无数据源' : '')
        + ' · 综合分＝已返回因子 z 均值 · 信号阈值 ±0.25 · 分层年化多空 / 换手率 / 相关性：无数据源'
    }
  }

  // --------------------------------------------------------- 板块热力图
  function renderPlates(plates) {
    const section = sectionOf('sec-5')
    if (!section) return
    const heat = $('.heat', section)
    const subs = $$('.chead .sub', section)
    const legend = $('.hlegend', section)
    const list = asArray(plates && plates.ok && plates.data && plates.data.plate_list)
    if (legend) {
      legend.innerHTML = '<span>板块涨跌幅：无数据源（需富途实时行情权限，/api/v3/plates 只返回板块清单）</span>'
    }
    if (!heat) return
    if (list.length === 0) {
      V3.nodata(heat, '板块热力图', errText(plates, 'GET /api/v3/plates 未返回板块清单'))
      if (subs[0]) subs[0].textContent = '板块清单：无数据源'
      return
    }
    const shown = list.slice(0, 24)
    heat.innerHTML = shown.map((plate) => '<div class="hcell" style="background:rgba(255,255,255,.03)" title="'
      + esc('板块 ' + (plate.sc_name || plate.plate_name || plate.code) + ' · 涨跌幅无数据源（需实时行情权限）') + '">'
      + '<b>' + esc(plate.sc_name || plate.plate_name || plate.code) + '</b>'
      + '<span class="hp num" style="color:var(--faint);font-size:12px">涨跌幅 —</span></div>').join('')
    if (subs[0]) {
      subs[0].textContent = '板块清单 ' + list.length + ' 个（显示前 ' + shown.length + ' 个）· 涨跌幅：无数据源（需富途实时行情权限）· 来源 /api/v3/plates'
    }
  }

  // ------------------------------------------------------- 数据源健康
  function healthCard(source, checkedAt) {
    const status = String(source.status || '').toLowerCase()
    const kind = status === 'ok' ? 'g' : status === 'warn' ? 'a' : 'r'
    const label = status === 'ok' ? '正常' : status === 'warn' ? '警告' : status === 'fail' ? '失败' : (source.status || '未知')
    const warn = status === 'ok' ? '' : ' warn'
    return '<div class="hcard' + warn + '" title="' + esc(String(source.detail || '')) + '">'
      + '<div class="ht"><b>' + esc(source.label || source.key || '—') + '</b>'
      + '<span class="st"><i class="dot ' + kind + '"></i>' + esc(label) + '</span></div>'
      + '<div class="hd">' + esc(String(source.detail || '—')) + '</div>'
      + '<div class="last num">检查于 ' + esc(checkedAt) + '</div>'
      + (status === 'ok' ? '' : '<div class="warntxt">修复：' + esc(String(source.fix || '—')) + '</div>')
      + '</div>'
  }

  function renderHealth(brain, metrics) {
    const section = sectionOf('sec-6')
    if (!section) return
    const grid = $('.hgrid', section)
    const subs = $$('.chead .sub', section)
    if (!grid) return
    const workbench = (brain && brain.sources && brain.sources.workbench) || {}
    const data = workbench.data || {}
    const list = asArray(data.sources)
    const checkedAt = stamp(data.checked_at)
    const cards = list.map((source) => healthCard(source, checkedAt))
    if (metrics && metrics.ok) {
      cards.push('<div class="hcard"><div class="ht"><b>dsh-quant-data-mcp</b>'
        + '<span class="st"><i class="dot ' + (metrics.workbenchUp ? 'g' : 'r') + '"></i>' + (metrics.workbenchUp ? '在线' : '不可达') + '</span></div>'
        + '<div class="hd">MCP 工具域 · ' + esc(metrics.toolDomains || '—') + ' 域 · ' + esc(metrics.toolTotal || '—')
        + ' 个工具 · 今日调用 ' + esc(metrics.mcp ? metrics.mcp.calls : '—') + ' 次 · 失败 '
        + esc(metrics.mcp ? metrics.mcp.errors : '—') + ' 次 · 平均 ' + esc(metrics.mcp ? metrics.mcp.avgMs : '—') + 'ms</div>'
        + '<div class="last num">检查于 ' + esc(stamp(metrics.generated_at)) + '</div></div>')
    }
    if (cards.length === 0) {
      V3.nodata(grid, '数据源健康', errText(workbench, 'GET /api/v3/brain 的 sources 未返回可用数据源'))
      if (subs[0]) subs[0].textContent = '数据源健康：无数据源'
      return
    }
    grid.innerHTML = cards.join('')
    if (subs[0]) {
      subs[0].textContent = '真实数据源探测 ' + list.length + ' 项 · 来源 workbench sources（/api/v3/brain）· ok '
        + esc((data.summary && data.summary.ok) === undefined ? '—' : data.summary.ok) + ' / warn '
        + esc((data.summary && data.summary.warn) === undefined ? '—' : data.summary.warn) + ' / fail '
        + esc((data.summary && data.summary.fail) === undefined ? '—' : data.summary.fail)
    }
    if (subs[1]) subs[1].textContent = '检查于 ' + checkedAt
  }

  // ------------------------------------------------------------ 交互绑定
  function bindInteractions() {
    const query = document.getElementById('q')
    if (query) {
      const clone = query.cloneNode(true)
      clone.setAttribute('placeholder', '输入代码，如 SH.600519')
      query.replaceWith(clone)
      clone.addEventListener('input', () => renderWatch(clone.value))
    }
    const seg = document.getElementById('seg')
    if (seg) {
      const clone = seg.cloneNode(true)
      seg.replaceWith(clone)
      const mark = () => $$('button', clone).forEach((button) => button.classList.toggle('on', button.getAttribute('data-p') === state.period))
      clone.addEventListener('click', async (event) => {
        const button = event.target.closest('button[data-p]')
        if (!button) return
        state.period = button.getAttribute('data-p')
        mark()
        await loadBars()
      })
      mark()
    }
    const refresh = document.getElementById('refresh')
    if (refresh) {
      const clone = refresh.cloneNode(true)
      refresh.replaceWith(clone)
      clone.addEventListener('click', async () => {
        clone.classList.remove('spin')
        void clone.offsetWidth
        clone.classList.add('spin')
        await render()
      })
    }
    // 设计稿内联脚本在 #watchBody 上挂了「切换演示标的」的监听：整节点替换后再挂真实交互
    const watch = document.getElementById('watchBody')
    if (watch) {
      const clone = watch.cloneNode(true)
      watch.replaceWith(clone)
      clone.addEventListener('click', async (event) => {
        const star = event.target.closest('[data-star]')
        if (star) {
          const ticker = star.getAttribute('data-star')
          state.starred[ticker] = !state.starred[ticker]
          renderWatch(document.getElementById('q') ? document.getElementById('q').value : '')
          return
        }
        const row = event.target.closest('tr[data-code]')
        if (!row) return
        const ticker = row.getAttribute('data-code')
        if (!ticker || ticker === state.ticker) return
        state.ticker = ticker
        renderWatch(document.getElementById('q') ? document.getElementById('q').value : '')
        await loadBars()
        const orderbook = await V3.api('orderbook?ticker=' + encodeURIComponent(state.ticker))
        renderBook(orderbook, state.watch.find((item) => item.ticker === state.ticker) || null)
      })
    }
  }

  // --------------------------------------------------------------- 主流程
  async function render() {
    const [overview, metrics, brain, watchlist, plates] = await Promise.all([
      V3.api('overview'), V3.api('metrics'), V3.api('brain'), V3.api('market/watchlist?n=6'), V3.api('plates?market=SH&plate_class=ALL'),
    ])

    if (overview && overview.ok) renderTopbar(overview, metrics, brain)
    else V3.nodata($('.topbar .tb-mid'), '顶栏权益', errText(overview, 'GET /api/v3/overview 失败'))

    if (watchlist && watchlist.ok) {
      state.watch = asArray(watchlist.rows)
      state.watchErrors = asArray(watchlist.errors)
    } else {
      state.watch = []
      state.watchErrors = []
    }
    if (!state.ticker) state.ticker = state.watch.length ? state.watch[0].ticker : 'SH.600519'
    if (!state.watch.some((row) => row.ticker === state.ticker) && state.watch.length) state.ticker = state.watch[0].ticker

    const tickers = state.watch.map((row) => row.ticker)
    const factors = await V3.api('factors/matrix' + (tickers.length ? '?tickers=' + encodeURIComponent(tickers.join(',')) : ''))
    if (factors && factors.ok) {
      state.matrix = factors.matrix
      state.ic = factors.ic
      state.factorError = null
    } else {
      state.matrix = null
      state.ic = null
      state.factorError = factors
    }

    renderWatchHead(watchlist && watchlist.ok ? watchlist.errors : [])
    renderWatch(document.getElementById('q') ? document.getElementById('q').value : '')
    renderFactors()
    renderPlates(plates)
    renderHealth(brain, metrics)
    bindInteractions()

    await loadBars()
    const row = state.watch.find((item) => item.ticker === state.ticker) || null
    const orderbook = await V3.api('orderbook?ticker=' + encodeURIComponent(state.ticker))
    renderBook(orderbook, row)

    const pagehead = $('.pagehead span')
    if (pagehead) {
      pagehead.textContent = '数据入口 · 行情看板 / 多因子信号 / 数据源健康 · 真实接口数据（取不到的项标注「无数据源」）'
    }
    const foot = $('footer.foot')
    if (foot) {
      foot.innerHTML = '<span>数据来源：/api/v3/market（K 线，' + esc((state.barsMeta && state.barsMeta.source) || '—')
        + '）· /api/v3/market/watchlist（自选快照）· /api/v3/factors/matrix（因子 z 与 IC）· /api/v3/plates（板块清单）· '
        + '/api/v3/orderbook（五档，权限不足时标注原因）· /api/v3/brain（数据源探测）· /api/v3/metrics（通道计数）</span>'
        + '<span>量化决策平台 V3.0 · 页面不含占位数字</span>'
    }
  }

  purgeDemoScripts()
  render().catch((error) => {
    const main = $('main .wrap')
    if (main) V3.nodata(main, '行情与信号页面数据', String((error && error.message) || error))
  })
})()
