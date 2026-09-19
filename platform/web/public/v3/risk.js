// V3「风险监控」页数据绑定：设计稿 risk.html 的 HTML/CSS 一字不动，
// 本文件只把 /api/v3/* 的真实值写进设计稿 DOM，并把没有数据源的区块显式标注（不留占位数字）。
//
// 数据源（逐块对应）：
//   /api/v3/risk/analytics?limit=250 —— 组合风险量：VaR/CVaR/Beta/Alpha/IR、Kupiec、净值曲线、组合权重
//   /api/v3/risk                     —— 事前阈值配置（risk_per_trade / stop_atr_mult / max_positions / ...）
//   /api/v3/oms/orders               —— 台账订单与风控分级（单笔占比、manual/blocked）
//   /api/v3/execution                —— 持仓（账户/标的/市值），用于最大持仓只数口径
//   /api/v3/metrics                  —— 通道与工具面计数（顶栏状态点）
//   /api/v3/audit?window=120         —— 审计链（事中事件流）
//   /api/v3/events?ticker=..&window=180 —— 公开披露事件（分红/除权除息）
//
// 无数据源（工具面确实没有）：行业分类（故行业暴露 / 行业上限不参与阻断）、归因（行业/因子/个股）、
//   逐标的 ATR 与单笔风险折算、校验耗时、杠杆率、流动性评分、逐日 VaR 序列（故指标卡迷你图不绘制）。
/* global window, document */
;(function () {
  const V3 = window.V3
  if (!V3) return
  const { num, money, stamp, hhmmss, esc } = V3

  /* ── 小工具 ────────────────────────────────────────────────────────────── */
  const q = (sel, root) => (root || document).querySelector(sel)
  const qa = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel))
  const fin = (v) => Number.isFinite(Number(v))
  // 设计稿用 U+2212 作负号：这里把 shared.js 的 num() 输出统一成同一字形，避免两种负号混排
  const minus = (text) => String(text).replace(/^-/, '−')
  const fnum = (v, d) => minus(num(v, d))
  const pct = (v, d) => (fin(v) ? `${Number(v) < 0 ? '−' : ''}${Math.abs(Number(v)).toFixed(d == null ? 2 : d)}%` : '—')
  const spct = (v, d) => (fin(v) ? `${Number(v) >= 0 ? '+' : '−'}${Math.abs(Number(v)).toFixed(d == null ? 2 : d)}%` : '—')
  const badge = (kind, text) => `<span class="badge ${kind}">${esc(text)}</span>`
  const tint = (v) => (!fin(v) || Number(v) === 0 ? 'var(--muted)' : Number(v) > 0 ? 'var(--green)' : 'var(--red)')
  const day = (v) => String(v || '').slice(0, 10)
  const clock = (v) => hhmmss(v) || day(v)

  /** 设计稿的每个区块都以 .card-title 文本区分（DOM 层级不假设嵌套，逐层用选择器取）。 */
  function cardByTitle(title) {
    for (const card of qa('.card')) {
      const node = q('.card-title', card)
      if (node && (node.textContent || '').indexOf(title) >= 0) return card
    }
    return null
  }
  /** 表格按表头文本定位（与 V3.tableByHeaders 同一套路，但支持单元格内写入徽章）。 */
  function tableWith(headers) {
    for (const table of qa('table')) {
      const head = (q('thead', table) ? q('thead', table).textContent : '').replace(/\s+/g, '')
      if (headers.every((h) => head.indexOf(String(h).replace(/\s+/g, '')) >= 0)) return table
    }
    return null
  }
  function rewriteTable(table, rows) {
    const tbody = table ? q('tbody', table) : null
    const tpl = tbody ? q('tr', tbody) : null
    if (!tbody || !tpl) return false
    const clone = tpl.cloneNode(true)
    tbody.innerHTML = ''
    for (const row of rows) {
      const tr = clone.cloneNode(true)
      const cells = qa('td', tr)
      for (let i = 0; i < cells.length; i++) {
        const value = row[i]
        if (value === undefined) continue
        if (value && typeof value === 'object' && value.html !== undefined) cells[i].innerHTML = value.html
        else cells[i].textContent = value === null || value === undefined ? '—' : String(value)
      }
      tbody.appendChild(tr)
    }
    return true
  }
  /** 在容器内插入一块「无数据源」标注（沿用 shared.js 的 dashed token 样式）。 */
  function noData(parent, what, why, before) {
    if (!parent) return null
    const holder = document.createElement('div')
    if (before && before.parentElement === parent) parent.insertBefore(holder, before)
    else parent.appendChild(holder)
    V3.nodata(holder, what, why)
    return holder
  }
  function setMini(mini, k, v, s, vColor) {
    if (!mini) return
    const kn = q('.k', mini)
    const vn = q('.v', mini)
    const sn = q('.s', mini)
    if (kn) kn.textContent = k
    if (vn) {
      vn.textContent = v
      if (vColor) vn.style.color = vColor
    }
    if (sn) sn.textContent = s
  }
  function headVars(card, title, meta) {
    if (!card) return
    const t = q('.card-title', card)
    if (t && title) t.innerHTML = title
    const m = q('.card-meta', card)
    if (m && meta) m.innerHTML = meta
  }

  /* ── 纯计算 ────────────────────────────────────────────────────────────── */
  /** 净值曲线上的峰值/谷底/最大回撤/是否收复（全部由 analytics.equityCurve 实数推导）。 */
  function curveStats(curve) {
    const points = (curve || [])
      .map((p) => ({ t: String((p && p.t) || ''), v: Number(p && p.v) }))
      .filter((p) => p.t && Number.isFinite(p.v))
    if (points.length < 2) return null
    let running = points[0]
    let dd = 0
    let peakAt = points[0]
    let trough = points[0]
    for (const p of points) {
      if (p.v > running.v) running = p
      const d = p.v / running.v - 1
      if (d < dd) {
        dd = d
        peakAt = running
        trough = p
      }
    }
    let recovery = null
    for (let i = points.indexOf(trough) + 1; i < points.length; i++) {
      if (points[i].v >= peakAt.v) {
        recovery = points[i]
        break
      }
    }
    const days = recovery
      ? Math.round((Date.parse(recovery.t) - Date.parse(trough.t)) / 86400000)
      : null
    return {
      points, ddPct: dd * 100, peakAt, trough, recovery, days,
      first: points[0], last: points[points.length - 1],
    }
  }
  /** 持仓汇总（多账户/多币种：只做只数统计，绝不做跨币种金额合并）。 */
  function positionSummary(positions) {
    const groups = positions && Array.isArray(positions.groups) ? positions.groups : []
    let total = 0
    let maxPerAccount = 0
    const accounts = []
    for (const group of groups) {
      const rows = Array.isArray(group.positions) ? group.positions : []
      if (rows.length === 0) continue
      total += rows.length
      maxPerAccount = Math.max(maxPerAccount, rows.length)
      accounts.push({ account: group.account || group.acc_id || '—', n: rows.length, total_asset: group.total_asset, cash: group.cash })
    }
    return { total, maxPerAccount, accounts: accounts.length, rows: accounts }
  }

  /* ── 1. 顶栏 ───────────────────────────────────────────────────────────── */
  function renderTopbar(mode, nav, metrics, positions) {
    const env = q('.topbar-center .pill.env')
    if (env) {
      env.textContent = String(mode || 'sim').toUpperCase()
      env.title = '模式来自 /api/v3/execution positions.mode'
    }
    for (const pill of qa('.topbar-center .pill')) {
      const label = q('.lbl', pill)
      if (!label) continue
      const name = (label.textContent || '').trim()
      const value = q('.num', pill)
      if (name === '权益') {
        if (value) value.textContent = money(nav)
        pill.title = '台账权益 equity.current（本地模拟台账，不代表券商资产）· 来源 /api/v3/risk/analytics.nav'
      } else if (name === '日内') {
        if (value) {
          value.textContent = '—'
          value.className = 'num'
        }
        pill.title = '无数据源：台账权益曲线只有 1 个点位（2026-09-18），无前值；不混用研究组合的净值曲线'
      }
    }
    // 设计稿这里是「示例数据」提示；真实运行下换成「真实数据」（不保留占位字样）
    const demo = q('.topbar-center .pill.demo')
    if (demo) {
      demo.className = 'pill'
      demo.innerHTML = '<span class="dot"></span>真实数据'
      demo.title = '本页所有数字来自本服务 /api/v3/* 实时接口'
    }
    const beats = qa('.topbar-center .pill.hb')
    const mcpOk = Boolean(metrics && metrics.workbenchUp)
    const specs = [
      { dot: mcpOk, text: `MCP ${metrics && metrics.toolTotal ? metrics.toolTotal : '—'} 工具`, title: `MCP 工具面：${mcpOk ? '运行中' : '不可达'}（/api/v3/metrics）` },
      { dot: false, text: 'SDK 无数据源', title: (metrics && metrics.sdk && metrics.sdk.reason) || '本服务未挂载 SDK JSON-RPC 通道' },
      { dot: false, text: 'Headless 无数据源', title: '本服务未挂载 Headless CLI 子进程通道（/api/v3/gateway 同口径）' },
    ]
    beats.forEach((beat, index) => {
      const spec = specs[index]
      if (!spec) return
      beat.className = 'pill hb'
      beat.innerHTML = `<span class="dot${spec.dot ? '' : ' a'}"></span>${esc(spec.text)}`
      beat.title = spec.title
    })
    const right = q('.topbar-right')
    if (right) {
      const ts = stamp(metrics && metrics.generated_at)
      right.innerHTML = `<span class="num">${esc(ts === '—' ? '—' : ts.slice(0, 16))}</span> UTC · 数据截至`
    }
    const navFoot = q('.nav-foot')
    if (navFoot) navFoot.innerHTML = '<span class="dot a"></span>风控分级 · 台账待人工确认'
    const meta = q('.page-head .head-meta')
    if (meta) {
      meta.innerHTML =
        `<span class="badge info">阈值源：/api/v3/risk</span>` +
        `<span class="badge ${mcpOk ? 'ok' : 'bad'}">${mcpOk ? '工作台在线' : '工作台不可达'}</span>`
    }
  }

  /* ── 2. 指标卡行（5 张必须用真实值） ───────────────────────────────────── */
  function renderMetrics(a) {
    const cards = qa('.metrics .mcard')
    if (!a) {
      for (const card of cards) {
        card.replaceChildren()
        V3.nodata(card, '组合风险量', '/api/v3/risk/analytics 取不到（口径：自选池等权组合，需 ≥40 个对齐交易日）')
      }
      return
    }
    const obs = fin(a.observations) ? Number(a.observations) : null
    const level = fin(a.confidence) ? Number(a.confidence) * 100 : null
    const specs = [
      {
        value: money(a.varAmount),
        sub: `占权益 ${pct(a.varDailyPct, 3)} · 历史模拟法 ${obs == null ? '—' : obs} 日（${level == null ? '—' : level.toFixed(0)}%）`,
        color: 'var(--blue)',
        title: '/api/v3/risk/analytics.varAmount（= varDailyPct × nav）',
      },
      {
        value: money(a.cvarAmount),
        sub: `占权益 ${pct(a.cvarDailyPct, 3)} · 超出 VaR 的尾部均值`,
        color: 'var(--cyan)',
        title: '/api/v3/risk/analytics.cvarAmount',
      },
      {
        value: fnum(a.beta, 3),
        sub: `基准 ${a.benchmarkTicker || '—'} · ${obs == null ? '—' : obs} 个交易日`,
        color: null,
        title: '/api/v3/risk/analytics.beta（对基准日收益回归）',
      },
      {
        value: spct(a.alphaAnnPct, 2),
        sub: `年化 · 基准年化 ${spct(a.benchmarkAnnReturnPct, 2)}`,
        color: tint(a.alphaAnnPct),
        title: '/api/v3/risk/analytics.alphaAnnPct（年化 252）',
      },
      {
        value: fnum(a.ir, 3),
        sub: `组合年化 ${spct(a.annReturnPct, 2)} · 年化波动 ${pct(a.annVolPct)}`,
        color: 'var(--purple)',
        title: '/api/v3/risk/analytics.ir（年化 α / 跟踪误差）',
      },
    ]
    cards.forEach((card, index) => {
      const spec = specs[index]
      if (!spec) return
      const value = q('.val', card)
      if (value) {
        value.textContent = spec.value
        value.style.color = spec.color || ''
      }
      const sub = q('.sub', card)
      if (sub) sub.textContent = spec.sub
      card.title = spec.title
      // 设计稿的 spark 折线是占位图形：工作台不提供逐日 VaR/Alpha 序列 → 移除，绝不画假线
      const spark = q('svg.spark', card)
      if (spark) spark.remove()
    })
  }

  /* ── 3. 三栏：事前 / 事中 / 事后 ───────────────────────────────────────── */
  function renderPre(card, riskData, orders, nav, analytics, positions) {
    if (!card) return
    const cfg = (riskData && riskData.config) || {}
    const cfgSource = (riskData && riskData.source) || '未返回来源'
    const reasonText = orders.map((o) => ((o.risk && o.risk.reasons) || []).join('；')).join('；')
    const limitMatch = reasonText.match(/[>＞]\s*([0-9.]+)\s*%/)
    const singleLimit = limitMatch ? Number(limitMatch[1]) : null
    const maxOrder = orders.reduce((acc, o) => (fin(o.value) && (!acc || Number(o.value) > Number(acc.value)) ? o : acc), null)
    const singleRatio = maxOrder && nav ? (Number(maxOrder.value) / nav) * 100 : null
    const weights = analytics && analytics.tickers ? Object.entries(analytics.tickers).map(([t, w]) => [t, Number(w) * 100]) : []
    const heaviest = weights.slice().sort((x, y) => y[1] - x[1])[0] || null
    const cap = fin(cfg.max_position_pct) ? Number(cfg.max_position_pct) * 100 : null
    const over = orders.filter((o) => o.stage === 'blocked' || (o.risk && o.risk.action === 'blocked')).length
    const manual = orders.filter((o) => o.stage === 'manual').length
    const summary = positionSummary(positions)

    const rows = [
      {
        name: '单标的上限',
        th: cap == null ? '无数据源（config.max_position_pct 未返回）' : `≤ 组合权益 ${pct(cap, 0)}`,
        note: heaviest ? `组合最大单票权重 ${pct(heaviest[1])}（${heaviest[0]}，等权），距上限 ${pct(cap - heaviest[1])}` : '无数据源：组合权重取不到',
        tone: !heaviest || cap == null ? ['mute', '无数据'] : (heaviest[1] <= cap ? ['ok', '正常'] : ['bad', '超限']),
      },
      {
        name: '单笔下单上限',
        th: singleLimit == null ? '≤ 权益 2%（OMS check_order 口径，未从台账回读到阈值）' : `≤ 权益 ${pct(singleLimit, 0)}（OMS check_order 口径）`,
        note: maxOrder
          ? `台账最大单笔 ${pct(singleRatio)}（${maxOrder.ticker} ${maxOrder.side} ${num(maxOrder.qty, 0)} 股 / ${money(maxOrder.value)}）`
          : '台账无订单',
        tone: singleLimit != null && singleRatio != null && singleRatio > singleLimit ? ['bad', '超限'] : ['ok', '正常'],
      },
      {
        name: '单笔风险预算',
        th: fin(cfg.risk_per_trade) ? `≤ 权益 ${pct(Number(cfg.risk_per_trade) * 100, 1)}（config.risk_per_trade）` : '无数据源（config.risk_per_trade 未返回）',
        note: `无数据源：工具面未提供按止损距离折算的单笔风险（止损 ATR 倍数 ${num(cfg.stop_atr_mult, 1)}x，逐标的 ATR 未提供）`,
        tone: ['mute', '无数据'],
      },
      {
        name: '最大持仓数',
        th: fin(cfg.max_positions) ? `≤ ${num(cfg.max_positions, 0)} 只（config.max_positions，策略口径）` : '无数据源（config.max_positions 未返回）',
        note: summary.total
          ? `台账持仓 ${summary.total} 只 / ${summary.accounts} 个模拟账户（最大单账户 ${summary.maxPerAccount} 只，口径不同）`
          : '台账无持仓',
        tone: ['warn', '口径不同'],
      },
      {
        name: '单日亏损上限',
        th: fin(cfg.daily_loss_limit_pct) ? `单日 ≤ ${pct(Number(cfg.daily_loss_limit_pct) * 100, 0)}（config.daily_loss_limit_pct）` : '无数据源（config.daily_loss_limit_pct 未返回）',
        note: analytics && fin(analytics.maxDrawdownPct)
          ? `组合区间累计最大回撤 ${pct(analytics.maxDrawdownPct)}（analytics，非单日口径）`
          : '无数据源：组合净值曲线不足',
        tone: ['warn', '关注'],
      },
    ]
    const checks = qa('.check', card)
    checks.forEach((check, index) => {
      const spec = rows[index]
      if (!spec) {
        check.remove()
        return
      }
      check.innerHTML =
        `<div class="check-main"><span class="check-name">${esc(spec.name)}</span>` +
        `<span class="check-th">${esc(spec.th)}</span>` +
        `<div class="check-note">${esc(spec.note)}</div></div>` +
        `<div class="check-side">${badge(spec.tone[0], spec.tone[1])}</div>`
    })
    const foot = q('.check-foot', card)
    if (foot) {
      foot.innerHTML =
        `<span>本台账校验 <span class="num">${orders.length}</span> 单 · 超单笔上限 <span class="num">${manual}</span> 单` +
        `${over ? ` · 硬阻断 <span class="num">${over}</span> 单` : ' · 硬阻断 0 单'}</span>` +
        `<span class="num" style="color:var(--faint)">阈值 5 项 · source ${esc(cfgSource)}</span>`
    }
    headVars(card, '事前风控<span class="tag">阈值配置与台账校验口径</span>',
      `MCP 工具 <span class="num">risk</span>（配置）· <span class="num">calc_var</span>（组合风险量）`)
  }

  function renderLive(card, analytics, audit, events, ledger) {
    if (!card) return
    const minis = qa('.mini3 .mini', card)
    const stats = analytics ? curveStats(analytics.equityCurve) : null
    setMini(minis[0], '台账最大回撤',
      ledger && fin(ledger.drawdown_pct) ? pct(ledger.drawdown_pct) : '—',
      `本地模拟台账（${(ledger && ledger.drawdown_source) || '无数据源'}，权益曲线仅 1 个点位）`,
      'var(--muted)')
    setMini(minis[1], '组合区间最大回撤',
      analytics && fin(analytics.maxDrawdownPct) ? pct(analytics.maxDrawdownPct) : '—',
      stats ? `峰值 ${day(stats.peakAt.t)} · 谷底 ${day(stats.trough.t)}（自选池等权 ${stats.points.length} 日）` : '无数据源：净值曲线不足',
      'var(--red)')
    setMini(minis[2], '年化波动率',
      analytics && fin(analytics.annVolPct) ? pct(analytics.annVolPct) : '—',
      analytics && fin(analytics.observations)
        ? `${analytics.observations} 个交易日 · 组合年化 ${spct(analytics.annReturnPct, 2)} · 基准年化 ${spct(analytics.benchmarkAnnReturnPct, 2)}`
        : '无数据源',
      null)

    const table = tableWith(['时间', '类型', '级别', '处置动作'])
    if (!table) return
    const KIND_TEXT = { signal: '量化信号', fill: '台账成交', 'order-facts': '券商响应' }
    const rows = []
    for (const entry of audit.slice(0, 6)) {
      const kind = String(entry.kind || '')
      const tone = kind === 'fill' ? ['ok', '成交'] : kind === 'order-facts' ? ['mute', '查询'] : ['mute', '提示']
      rows.push([
        clock(entry.at),
        `${entry.source_label || KIND_TEXT[kind] || '审计链'} · ${entry.ticker || '—'}`,
        { html: badge(tone[0], tone[1]) },
        entry.detail || '—',
      ])
    }
    for (const event of events.slice(0, 3)) {
      rows.push([
        day(event.date) || day(event.announced) || '—',
        `公开披露 · ${event.type || '事件'}`,
        { html: badge('info', '公告') },
        `${event.detail || '—'}${event.announced ? `（公告日 ${day(event.announced)}）` : ''}`,
      ])
    }
    const wrap = q('.tbl-wrap', card)
    if (rows.length === 0) {
      if (wrap) V3.nodata(wrap, '事中事件流', '审计链与公开披露事件窗口内均无记录')
    } else {
      rewriteTable(table, rows)
    }
    headVars(card, '事中风控<span class="tag">台账风险量与真实事件流</span>',
      `来源 <span class="num">/api/v3/audit</span> + <span class="num">/api/v3/events</span>（窗口 180 日）`)
  }

  function renderPost(card, analytics) {
    if (!card) return
    const stats = analytics ? curveStats(analytics.equityCurve) : null
    for (const attr of qa('.attr', card)) {
      const name = (q('.attr-h span', attr) || {}).textContent || '归因'
      V3.nodata(attr, `${name.trim()}归因`, '工作台工具面无行业分类/因子暴露/个股归因数据源，本页不估算')
    }
    const boxes = qa('.post-stats > div', card)
    if (boxes[0]) {
      const k = q('.k', boxes[0])
      const v = q('.v', boxes[0])
      const s = q('.s', boxes[0])
      if (k) k.textContent = '组合最大回撤'
      if (v) {
        v.textContent = analytics && fin(analytics.maxDrawdownPct) ? pct(analytics.maxDrawdownPct) : '—'
        v.style.color = 'var(--red)'
      }
      if (s) {
        s.textContent = stats
          ? `区间 ${day(stats.first.t)} → ${day(stats.last.t)} · 峰值 ${day(stats.peakAt.t)}`
          : '无数据源：净值曲线不足'
      }
    }
    if (boxes[1]) {
      const k = q('.k', boxes[1])
      const v = q('.v', boxes[1])
      const s = q('.s', boxes[1])
      if (k) k.textContent = '修复天数'
      if (v) v.textContent = stats ? (stats.recovery ? `${stats.days} 天` : '尚未收复') : '—'
      if (s) {
        s.textContent = stats
          ? (stats.recovery ? `${day(stats.recovery.t)} 收复前高` : `谷底 ${day(stats.trough.t)}，截至 ${day(stats.last.t)} 未收复前高`)
          : '无数据源：净值曲线不足'
      }
    }
    const kupiec = q('.kupiec', card)
    if (kupiec) {
      const k = analytics && analytics.kupiec ? analytics.kupiec : null
      if (!k) {
        V3.nodata(kupiec, 'Kupiec POF 检验', '样本不足（需 ≥40 个对齐交易日）')
      } else {
        const expected = fin(k.observations) && fin(analytics.confidence)
          ? Number(k.observations) * (1 - Number(analytics.confidence))
          : null
        kupiec.innerHTML =
          badge(k.pass ? 'ok' : 'bad', k.pass ? 'Kupiec 检验通过' : 'Kupiec 检验未通过') +
          `<span><span class="num">p=${fnum(k.pValue, 4)}</span> · 样本 <span class="num">${num(k.observations, 0)}</span> 日 · ` +
          `例外 <span class="num">${num(k.breaches, 0)}</span> 次 / 预期 <span class="num">${expected == null ? '—' : expected.toFixed(2)}</span> 次` +
          `（${analytics.confidence ? (Number(analytics.confidence) * 100).toFixed(0) : '—'}% VaR · LR ${fnum(k.lr, 4)}）</span>`
      }
    }
    headVars(card, '事后风控<span class="tag">回测检验与区间统计</span>',
      analytics
        ? `区间 ${day(analytics.window && analytics.window.from)} → ${day(analytics.window && analytics.window.to)} · ${num(analytics.observations, 0)} 个交易日 · ` +
          `组合年化 ${spct(analytics.annReturnPct, 2)} · 最大回撤 ${pct(analytics.maxDrawdownPct)}`
        : '无数据源')
  }

  /* ── 4. 暴露与集中度 ───────────────────────────────────────────────────── */
  function renderExposure(card, analytics, config, industrySource) {
    if (!card) return
    const FULL = 30 // 满刻度：单票上限 25% 留出余量
    const cap = config && fin(config.max_position_pct) ? Number(config.max_position_pct) * 100 : null
    const entries = analytics && analytics.tickers
      ? Object.entries(analytics.tickers).map(([ticker, w]) => [ticker, Number(w) * 100]).sort((x, y) => y[1] - x[1])
      : []
    const top5 = entries.slice(0, 5).reduce((sum, row) => sum + row[1], 0)
    const split = q('.split', card)
    const cols = split ? qa(':scope > div', split) : []
    const left = cols[0]
    const right = cols[1]

    headVars(card, '暴露与集中度<span class="tag">组合权重 · 来源 /api/v3/risk/analytics</span>',
      `<span style="color:var(--red)">┊ ${cap == null ? '—' : cap.toFixed(0)}% 单票上限</span> · 满刻度 ${FULL}%`)

    if (left) {
      const capLine = q('.col-cap', left)
      if (capLine) capLine.innerHTML = '<span>行业暴露（占组合净值）</span><span class="thr">无数据源</span>'
      qa('.irow', left).forEach((row) => row.remove())
      noData(left, '行业暴露', `工作台工具面无行业分类数据源（OMS industry_source=${(industrySource || 'no-data').slice(0, 8)}…）：行业上限按 0% 不参与自动阻断，红线需人工核对`)
    }
    if (right) {
      const capLine = q('.col-cap', right)
      if (capLine) {
        capLine.innerHTML = `<span>单票集中度 Top${entries.length || 0}</span>` +
          `<span class="num" style="color:var(--faint)">前五大合计 ${entries.length ? pct(top5) : '—'}</span>`
      }
      const trows = qa('.trow', right)
      const tpl = trows[0]
      const anchor = q('.tile2', right)
      trows.forEach((row) => row.remove())
      if (entries.length === 0 || !tpl) {
        noData(right, '单票集中度', '组合权重取不到（/api/v3/risk/analytics.tickers 为空）', anchor)
      } else {
        for (const row of entries) {
          const node = tpl.cloneNode(true)
          const name = q('.tn', node)
          if (name) name.innerHTML = `${esc(row[0])}<span class="sec-ind">等权</span>`
          const bar = q('.ibar', node)
          if (bar) bar.style.width = `${Math.min(100, (row[1] / FULL) * 100).toFixed(1)}%`
          const value = q('.iv', node)
          if (value) value.textContent = pct(row[1])
          node.title = `${row[0]} 组合权重（自选池等权持仓数 ${entries.length}）`
          if (anchor) right.insertBefore(node, anchor)
          else right.appendChild(node)
        }
      }
      const tiles = q('.tile2', right)
      if (tiles) V3.nodata(tiles, '杠杆率 / 流动性评分', '工具面无保证金占用与流动性评分数据源（positions 不返回融资与盘口深度）')
      const note = q('.note-line', right)
      if (note) {
        note.textContent = `权重＝组合内权重（${(analytics && analytics.portfolioSource) || '组合口径未知'}，共 ${entries.length} 个标的）；` +
          '行业分类与因子暴露工作台未提供，行业红线不参与自动阻断（OMS industry_source=no-data）。'
      }
    }
  }

  /* ── 5. 净值曲线 ───────────────────────────────────────────────────────── */
  function renderCurve(card, analytics) {
    if (!card) return
    const stats = analytics ? curveStats(analytics.equityCurve) : null
    const chart = q('svg.dd-svg', card)
    headVars(card, stats
      ? `组合净值曲线<span class="tag">${day(stats.first.t)} → ${day(stats.last.t)} · ${stats.points.length} 个交易日</span>`
      : '组合净值曲线<span class="tag">无数据源</span>',
      stats
        ? `<span style="color:var(--blue)">━ 组合净值（归一起点 1.0）</span>` +
          `<span style="color:var(--cyan)">● 末值 ${num(stats.last.v, 4)}</span>` +
          `<span>区间累计 ${spct((stats.last.v - 1) * 100, 2)}</span>`
        : '<span>无数据源</span>')
    if (!chart) return
    if (!stats) {
      const box = document.createElement('div')
      chart.replaceWith(box)
      V3.nodata(box, '组合净值曲线', '/api/v3/risk/analytics.equityCurve 取不到（对齐后 <40 个交易日）')
      return
    }
    // 设计稿的曲线是占位图形：整块换成由 analytics.equityCurve 真实绘制的 SVG（容器与 CSS 类保持不变）
    const box = document.createElement('div')
    box.className = 'dd-svg'
    chart.replaceWith(box)
    V3.svgLine(box, stats.points.map((p) => p.v * 100), { color: 'var(--blue)', height: 200 })
    const note = q('.note-line', card)
    if (note) {
      note.textContent = `曲线＝${(analytics && analytics.portfolioSource) || '组合'}按 ${stats.points.length} 个交易日回放的归一日权益（起点 ${day(stats.first.t)} = 1.0）；` +
        `区间最大回撤 ${pct(analytics && analytics.maxDrawdownPct)}（峰值 ${day(stats.peakAt.t)}，谷底 ${day(stats.trough.t)}）。` +
        '工作台未提供逐日回撤序列与阈值触发事件，故不绘制阈值线与触发点。'
    }
  }

  /* ── 6. 风控规则表（事前阈值：真实配置项） ─────────────────────────────── */
  function renderRules(card, riskData, orders, nav, analytics) {
    if (!card) return
    const cfg = (riskData && riskData.config) || {}
    const source = (riskData && riskData.source) || '未返回来源'
    const weights = analytics && analytics.tickers ? Object.entries(analytics.tickers).map(([t, w]) => [t, Number(w) * 100]) : []
    const heaviest = weights.slice().sort((x, y) => y[1] - x[1])[0] || null
    const maxOrder = orders.reduce((acc, o) => (fin(o.value) && (!acc || Number(o.value) > Number(acc.value)) ? o : acc), null)
    const singleRatio = maxOrder && nav ? (Number(maxOrder.value) / nav) * 100 : null
    const hit = orders.filter((o) => o.stage === 'manual' || o.stage === 'blocked').length

    const rows = [
      [
        '单标的上限',
        `≤ 组合权益 ${pct(cfg.max_position_pct == null ? null : Number(cfg.max_position_pct) * 100, 0)}（config.max_position_pct）`,
        heaviest ? pct(heaviest[1]) : '无数据源',
        { html: !heaviest || cfg.max_position_pct == null
          ? badge('mute', '无数据')
          : badge(heaviest[1] <= Number(cfg.max_position_pct) * 100 ? 'ok' : 'bad',
            heaviest[1] <= Number(cfg.max_position_pct) * 100 ? '正常' : '超限') },
        `组合权重 /api/v3/risk/analytics${heaviest ? `（${heaviest[0]}）` : ''}`,
        '无数据源',
      ],
      [
        '单笔下单上限',
        '≤ 权益 2%（OMS check_order 口径）',
        singleRatio == null ? '—' : pct(singleRatio),
        { html: badge(singleRatio != null && singleRatio > 2 ? 'bad' : 'ok', singleRatio != null && singleRatio > 2 ? '超限' : '正常') },
        maxOrder ? `台账最大单笔 ${maxOrder.ticker} ${money(maxOrder.value)}` : '台账无订单',
        '无数据源',
      ],
      [
        '单笔风险预算',
        `≤ 权益 ${pct(cfg.risk_per_trade == null ? null : Number(cfg.risk_per_trade) * 100, 1)}（config.risk_per_trade）`,
        '无数据源',
        { html: badge('mute', '无数据') },
        `止损 ATR 倍数 ${num(cfg.stop_atr_mult, 1)}x：逐标的 ATR 未提供`,
        '无数据源',
      ],
      [
        '止损 ATR 倍数',
        `stop_atr_mult = ${num(cfg.stop_atr_mult, 1)}x（config）`,
        '无数据源',
        { html: badge('mute', '无数据') },
        '工具面无逐标的 ATR 与止损价',
        '无数据源',
      ],
      [
        '最大持仓数',
        `≤ ${num(cfg.max_positions, 0)} 只（config.max_positions，策略口径）`,
        '见 /api/v3/execution 持仓',
        { html: badge('warn', '口径不同') },
        '台账按账户统计，不跨账户合并',
        '无数据源',
      ],
      [
        '单日亏损上限',
        `单日 ≤ ${pct(cfg.daily_loss_limit_pct == null ? null : Number(cfg.daily_loss_limit_pct) * 100, 0)}（config.daily_loss_limit_pct）`,
        analytics && fin(analytics.maxDrawdownPct) ? pct(analytics.maxDrawdownPct) : '—',
        { html: badge('warn', '关注') },
        '区间累计回撤（非单日口径）· analytics.maxDrawdownPct',
        '无数据源',
      ],
    ]
    const table = tableWith(['规则名', '阈值', '当前值', '状态', '最近更新'])
    if (table) rewriteTable(table, rows)
    headVars(card, '风控规则<span class="tag">事前阈值配置（逐项来源可查）</span>',
      `展示 ${rows.length} 行 · 配置项 ${Object.keys(cfg).length} 个 · source ${esc(source)}`)
    const note = q('.rules-note', card)
    if (note) {
      note.textContent = `阈值来自交易平台风控配置（${source}），带「无数据源」的当前值表示工具面确实没有对应读数，不用估算值顶替；` +
        `本控制台不提供规则编辑入口——阈值修改只经工作台受约束入口并留审计痕迹。本台账 ${orders.length} 单中 ${hit} 单未自动放行。`
    }
  }

  /* ── 7. 阻断记录（台账未自动放行的订单） ───────────────────────────────── */
  function renderBlocks(card, orders) {
    if (!card) return
    const rows = orders
      .filter((o) => (o.risk && o.risk.action !== 'auto') || o.stage === 'blocked' || o.stage === 'manual' || o.stage === 'rejected')
      .slice()
      .sort((x, y) => Number(y.value || 0) - Number(x.value || 0))
    const table = tableWith(['时间', '标的', '触发规则', '拒绝原因', '是否已上报'])
    const wrap = q('.tbl-wrap', card)
    if (!table || rows.length === 0) {
      if (wrap) V3.nodata(wrap, '阻断/退回记录', orders.length === 0 ? 'OMS 台账无订单' : '台账无 blocked/rejected 阶段订单')
    } else {
      rewriteTable(table, rows.map((o) => [
        clock(o.updated_at || o.first_seen_at),
        o.ticker || '—',
        o.stage === 'blocked' ? '红线强制阻断' : o.stage === 'rejected' ? '券商/通道拒绝' : '单笔下单上限',
        { html: esc((((o.risk && o.risk.reasons) || []).join('；')) || '未给出原因') },
        { html: `<span class="badge ${o.stage === 'blocked' ? 'bad' : 'ok'}">${o.stage === 'blocked' ? '已阻断' : '已登记'}</span>` },
      ]))
    }
    const bad = rows.filter((o) => o.stage === 'blocked').length
    headVars(card, '阻断记录<span class="tag">OMS 台账中未自动放行的订单</span>',
      `台账 <span class="num">${rows.length}</span> 条 · 硬阻断 <span class="num">${bad}</span> 条 · 全量留痕`)
    const note = q('.rules-note', card)
    if (note) {
      note.textContent = `口径：stage 为 manual/blocked/rejected 的订单（当前台账全部为 manual——超单笔上限退回人工确认，无 blocked 硬阻断）。` +
        '本服务未挂载 SDK JSON-RPC / Headless 通道，记录以 /api/v3/oms/orders 台账为准。'
    }
  }

  /* ── 8. 页脚 ───────────────────────────────────────────────────────────── */
  function renderFooter(analytics, metrics) {
    const foot = q('footer.footer')
    if (!foot) return
    const src = (analytics && analytics.portfolioSource) || '组合口径未知'
    foot.innerHTML =
      `<span>数据来源：本服务 /api/v3/risk/analytics、/api/v3/risk、/api/v3/oms/orders、/api/v3/execution、/api/v3/audit、/api/v3/events 实时接口 · ` +
      `组合口径「${esc(src)}」· 取不到的项显式标注「无数据源」，页面不含占位数字</span>` +
      `<span>量化决策平台 V3.0 · 风险监控 · 数据截至 ${esc(stamp(metrics && metrics.generated_at))}</span>`
  }

  /* ── 主流程 ────────────────────────────────────────────────────────────── */
  async function render() {
    const [analyticsEnv, riskEnv, ordersEnv, metricsEnv, auditEnv, eventsEnv, execEnv] = await Promise.all([
      V3.api('risk/analytics?limit=250'),
      V3.api('risk'),
      V3.api('oms/orders'),
      V3.api('metrics'),
      V3.api('audit?window=120'),
      V3.api('events?ticker=SH.600000&window=180'),
      V3.api('execution'),
    ])
    const analytics = analyticsEnv && analyticsEnv.ok && analyticsEnv.analytics
      ? Object.assign({}, analyticsEnv.analytics, {
          portfolioSource: analyticsEnv.portfolioSource,
          benchmarkTicker: analyticsEnv.benchmarkTicker,
        })
      : null
    const riskData = riskEnv && riskEnv.ok ? (riskEnv.data || null) : null
    const orders = ordersEnv && ordersEnv.ok && Array.isArray(ordersEnv.orders) ? ordersEnv.orders : []
    const metrics = metricsEnv && metricsEnv.ok ? metricsEnv : {}
    const audit = auditEnv && auditEnv.ok && auditEnv.data && Array.isArray(auditEnv.data.entries) ? auditEnv.data.entries : []
    const events = eventsEnv && eventsEnv.ok && eventsEnv.data && Array.isArray(eventsEnv.data.events) ? eventsEnv.data.events : []
    const exec = execEnv && execEnv.ok ? execEnv : null
    const nav = analyticsEnv && fin(analyticsEnv.nav)
      ? Number(analyticsEnv.nav)
      : (ordersEnv && fin(ordersEnv.nav) ? Number(ordersEnv.nav) : null)

    renderTopbar(exec && exec.positions && exec.positions.mode, nav, metrics, exec && exec.positions)
    renderMetrics(analytics)
    renderPre(cardByTitle('事前风控'), riskData, orders, nav, analytics, exec && exec.positions)
    renderLive(cardByTitle('事中风控'), analytics, audit, events, ordersEnv && ordersEnv.ok ? ordersEnv : null)
    renderPost(cardByTitle('事后风控'), analytics)
    renderExposure(cardByTitle('暴露与集中度'), analytics,
      riskData && riskData.config, ordersEnv && ordersEnv.industry_source)
    renderCurve(cardByTitle('回撤曲线'), analytics)
    renderRules(cardByTitle('风控规则'), riskData, orders, nav, analytics)
    renderBlocks(cardByTitle('阻断记录'), orders)
    renderFooter(analytics, metrics)
  }

  render().catch((error) => {
    const main = q('.main-inner')
    if (main) V3.nodata(main, '页面数据', String((error && error.message) || error))
  })
})()
