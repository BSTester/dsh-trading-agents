// V3「决策大脑」页数据绑定。
// 设计稿 brain.html 的 HTML/CSS 一字不动：这里只把 /api/v3/* 的真实值写进设计稿的
// 节点。本服务**未挂载** SDK / Headless 通道（/api/v3/brain 明确返回 unavailable + reason），
// 因此会话类区块（模型 / 推理强度 / Trace / token / 检查点）一律显式标注「无数据源」，
// 绝不用设计稿里的占位数字（turn 3/12、12,480、deepseek-v4-flash 等）顶替。
/* global window, document, Blob, URL */
;(function () {
  const V3 = window.V3
  const { num, money, signed, stamp, hhmmss, esc } = V3
  const $ = (selector, root) => (root || document).querySelector(selector)
  const $$ = (selector, root) => Array.prototype.slice.call((root || document).querySelectorAll(selector))
  const cardOf = (id) => $('[data-od-id="' + id + '"]')
  const errText = (payload, fallback) => {
    const error = (payload && payload.error) || {}
    return String(error.message || error.code || fallback || '接口未返回原因')
  }
  const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
  const asArray = (value) => (Array.isArray(value) ? value : [])
  const values = (list, render) => asArray(list).map(render).join('')

  /** 设计稿内联演示脚本（SESSIONS 里的 trace/token/turn 占位）随页面一起消失。 */
  function purgeDemoScripts() {
    for (const node of $$('script:not([src])')) {
      if ((node.textContent || '').indexOf('示例') === -1) continue
      node.textContent = '/* 设计稿内联演示数据（占位会话 / trace / token）已由 brain.js 的真实数据绑定取代 */'
    }
  }

  let toastTimer = null
  function toast(message) {
    const box = document.getElementById('toast')
    if (!box) return
    box.textContent = message
    box.classList.add('show')
    clearTimeout(toastTimer)
    toastTimer = setTimeout(() => box.classList.remove('show'), 2800)
  }

  function cloneBind(selector, handler) {
    const node = $(selector)
    if (!node) return null
    const clone = node.cloneNode(true)
    node.replaceWith(clone)
    clone.addEventListener('click', handler)
    return clone
  }

  function download(name, text) {
    try {
      const url = URL.createObjectURL(new Blob([text], { type: 'application/json;charset=utf-8' }))
      const link = document.createElement('a')
      link.href = url
      link.download = name
      document.body.appendChild(link)
      link.click()
      link.remove()
      setTimeout(() => URL.revokeObjectURL(url), 4000)
      return true
    } catch (error) {
      return false
    }
  }

  function dayLabel(value) {
    const text = stamp(value)
    if (!text || text === '—') return '—'
    const day = text.slice(0, 10)
    const parsed = Date.parse(day + 'T00:00:00Z')
    return Number.isFinite(parsed) ? day + ' ' + WEEK[new Date(parsed).getUTCDay()] : day
  }

  // ---------------------------------------------------------------- 顶栏
  function renderTopbar(overview, metrics, brain) {
    const mode = String((overview && overview.mode) || '').toUpperCase()
    const env = $('.topbar .badge.sim')
    if (env && mode) env.textContent = mode

    const equity = (overview && overview.equity) || {}
    const eqNode = $('.topbar .tb-eq b')
    if (eqNode) eqNode.textContent = money(equity.current)
    const points = asArray(equity.points)
    const last = points.length ? points[points.length - 1] : null
    const prev = points.length > 1 ? points[points.length - 2] : null
    const daily = last && prev && Number(prev.equity)
      ? (Number(last.equity) / Number(prev.equity) - 1) * 100 : null
    const chg = $('.topbar .tb-chg')
    if (chg) {
      if (daily === null) {
        chg.textContent = '日内 无数据源'
        chg.style.color = 'var(--faint)'
        chg.title = '权益台账只有 ' + points.length + ' 个点位（' + (equity.note || '—') + '），日内涨跌无法计算'
      } else {
        chg.textContent = signed(daily, 2, '%')
        chg.style.color = daily >= 0 ? 'var(--green)' : 'var(--red)'
        chg.title = '由台账 ' + prev.t + ' → ' + last.t + ' 计算'
      }
    }

    // 设计稿这里是「示例数据」提示：真实运行下换成「真实数据」
    const demo = $('.topbar .badge.demo')
    if (demo) {
      demo.className = 'badge sim'
      demo.textContent = '真实数据'
      demo.title = '页面数字全部来自本服务 /api/v3/* 实时接口'
    }

    const hb = $('.topbar .hb')
    if (hb) {
      const mcpMs = metrics && metrics.ok ? metrics.mcp && metrics.mcp.avgMs : null
      const mcpOk = Number.isFinite(Number(mcpMs))
      const sdkReason = ((brain && brain.sdk) || {}).reason || '本服务未挂载 SDK JSON-RPC 通道'
      const headlessReason = ((brain && brain.headless) || {}).reason || '本服务未挂载 Headless CLI 子通道'
      hb.innerHTML = '三通道 '
        + '<i class="dot ' + (mcpOk ? 'g' : 'a') + '"></i>MCP <b class="num">' + (mcpOk ? esc(num(mcpMs, 0)) + 'ms' : '无数据源') + '</b>'
        + '<span class="sep">·</span><i class="dot a"></i>SDK <b>无数据源</b>'
        + '<span class="sep">·</span><i class="dot a"></i>Headless <b>无数据源</b>'
      hb.title = 'MCP：workbench 工具面平均耗时（/api/v3/metrics）· SDK：' + sdkReason + ' · Headless：' + headlessReason
    }

    const right = $('.topbar .tb-right')
    if (right) {
      right.textContent = dayLabel(overview && overview.generated_at)
      right.title = '平台服务生成时间 ' + stamp(overview && overview.generated_at)
    }
  }

  // ------------------------------------------------------- 控制条（会话）
  function sessionStatus(mode, run) {
    const badge = $('#statusBadge')
    if (!badge) return
    if (mode === 'strategy-run') {
      const count = asArray((run || {}).proposals).length
      badge.className = 'status'
      badge.innerHTML = '<i class="dot g"></i>决策流水线已完成 · 提案 <span class="num">' + esc(count) + '</span> 条'
      badge.title = run ? '来源 /api/v3/strategy（as_of ' + stamp(run.asOf) + '）' : 'GET /api/v3/strategy 未返回落盘记录'
      return
    }
    badge.className = 'status queued'
    badge.innerHTML = '<i class="pulse"></i>无数据源 · ' + (mode === 'sdk' ? 'SDK JSON-RPC' : 'Headless CLI')
    badge.title = mode === 'sdk'
      ? '本服务未挂载 SDK JSON-RPC 通道：会话轮次 / trace 无数据源'
      : '本服务未挂载 Headless CLI 子通道：任务轮次 / trace 无数据源'
  }

  function renderControl(brain, run) {
    const section = cardOf('sec-1')
    if (!section) return
    const select = $('#sessionSel', section)
    if (select) {
      const clone = select.cloneNode(false)
      const add = (value, label) => {
        const option = document.createElement('option')
        option.value = value
        option.textContent = label
        clone.appendChild(option)
      }
      add('strategy-run', '研究流水线 · ' + (run ? stamp(run.asOf) : '无数据源') + '（/api/v3/strategy 最近一轮）')
      add('sdk', 'SDK 会话 · 无数据源（通道未挂载）')
      add('headless', 'Headless 任务 · 无数据源（通道未挂载）')
      clone.value = 'strategy-run'
      select.replaceWith(clone)
      clone.addEventListener('change', () => sessionStatus(clone.value, run))
    }

    // 模型 / 推理强度 / Trace：设计稿的 deepseek-v4-flash、max、trace-... 都是占位串
    const reasons = [
      '本服务未挂载 SDK / Headless 会话通道：本页不提供模型型号',
      '本服务未挂载 SDK / Headless 会话通道：本页不提供推理强度',
      '本服务未挂载 SDK / Headless 会话通道：没有会话 trace id',
    ]
    $$('.ctrl .meta-chip .v', section).forEach((node, index) => {
      node.textContent = '无数据源'
      node.title = reasons[index] || reasons[0]
    })
    const trace = $('#traceId', section)
    if (trace) {
      trace.textContent = '无数据源'
      trace.title = '没有 SDK / Headless 会话，trace id 无数据源'
    }
    sessionStatus('strategy-run', run)
  }

  // ------------------------------------------------- 推理流（研究流水线）
  function stepNode(template, kind, tag, time, duration, bodyHtml) {
    const node = template.cloneNode(true)
    node.className = 'step ' + kind
    const body = $('.scard', node)
    body.className = 'scard'
    body.innerHTML = '<div class="shead"><span class="tag ' + kind + '">' + esc(tag) + '</span>'
      + '<span class="stime num">' + esc(time) + '</span>'
      + '<span class="sdur num">' + esc(duration) + '</span></div>' + bodyHtml
    return node
  }

  function toolBody(name, params, paragraphs) {
    return '<div class="tool"><div class="trow"><span class="tname">' + esc(name)
      + '</span><span class="tstat ok">成功</span></div>'
      + '<div class="tparams">' + esc(params) + '</div>'
      + '<div class="tret">' + values(paragraphs, (line) => '<p>' + line + '</p>') + '</div></div>'
  }

  function renderTimeline(run) {
    const section = cardOf('sec-2')
    if (!section) return
    const list = $('.tl', section)
    const title = $('.tl-head .tl-title', section)
    if (!list) return
    if (title) title.textContent = '推理流 · 研究流水线 PDAT → PET'
    const template = $('.step', list)
    if (!template) return
    const keep = template.cloneNode(true)
    list.innerHTML = ''

    if (!run) {
      V3.nodata(list, '推理流', 'GET /api/v3/strategy 返回 run=null：研究流水线尚无落盘记录')
      return
    }
    const stages = run.stages || {}
    const universe = asArray((stages.PDAT || {}).universe).length ? asArray(stages.PDAT.universe) : asArray(run.universe)
    const codes = (list2, max) => asArray(list2).slice(0, max).join('、') + (asArray(list2).length > max ? ' 等' : '')
    const time = hhmmss(run.asOf)
    const duration = '无数据源'
    const nodes = []

    // ① PDAT 观察（真实：标的池 / 日 K 根数 / 取数错误）
    const pdat = stages.PDAT || {}
    nodes.push(stepNode(keep, 'observe', '观察', time, duration,
      '<div class="rtext">数据准备（PDAT）：标的池 <span class="num">' + esc(universe.length) + '</span> 只（'
      + esc(codes(universe, 4)) + '）· 日 K <span class="num">' + esc(pdat.bars === undefined ? '—' : pdat.bars)
      + '</span> 根 · 取数错误 <span class="num">' + esc(asArray(pdat.errors).length) + '</span> 条。数据源：workbench series（/api/v3/strategy）。</div>'))

    // ② PAAT 推理（真实：已分析数 / 有因子数 / 评分来源）
    const paat = stages.PAAT || {}
    nodes.push(stepNode(keep, 'think', '推理', time, duration,
      '<div class="rtext">因子分析（PAAT）：已分析 <span class="num">' + esc(paat.analyzed === undefined ? '—' : paat.analyzed)
      + '</span> 只 · 有因子 <span class="num">' + esc(paat.withFactors === undefined ? '—' : paat.withFactors)
      + '</span> 只 · 评分来源 <span class="num">' + esc(paat.scoreSource || '—')
      + '</span> · 因子错误 <span class="num">' + esc(paat.factorsError ? 1 : 0) + '</span> 条。</div>'))

    // ③ PCPT 行动（真实：增持 / 减持名单）
    const pcpt = stages.PCPT || {}
    const longs = asArray(pcpt.longs)
    const reduces = asArray(pcpt.reduces)
    nodes.push(stepNode(keep, 'act', '行动', time, duration,
      toolBody('v3:research-pipeline · PCPT',
        'stage=PCPT · universe=' + universe.length + ' 只 · 输入=因子 z（workbench/factors）',
        ['规则候选：增持 <span class="num">' + esc(longs.length) + '</span> 只（' + esc(longs.join('、') || '—') + '）· 减持 <span class="num">'
          + esc(reduces.length) + '</span> 只（' + esc(reduces.join('、') || '—') + '）。'])))

    // ④ PRT 行动（真实：每标的权重 / 是否触顶）
    const prt = stages.PRT || {}
    nodes.push(stepNode(keep, 'act', '行动', time, duration,
      toolBody('v3:research-pipeline · PRT',
        'stage=PRT · weightPctPerName=' + (prt.weightPctPerName === undefined ? '—' : prt.weightPctPerName),
        ['目标权重：每标的 <span class="num">' + esc(prt.weightPctPerName === undefined ? '—' : prt.weightPctPerName)
          + '%</span> · 组合上限约束 <span class="num">' + esc(prt.capped ? '已启用' : '未启用') + '</span>。',
          '仓位由 V3 研究流水线本地计算，不构成下单指令。'])))

    // ⑤ PET 反馈（真实：提案条数）
    const proposals = asArray(run.proposals)
    nodes.push(stepNode(keep, 'fb', '反馈', time, duration,
      '<div class="rtext">评估产出（PET）：<span class="num">' + esc(proposals.length)
      + '</span> 条调仓建议已成文，待人工审批（研究侧不下单、不启用策略）。</div>'))

    for (const node of nodes) list.appendChild(node)
    const chip = $('.tl-head .chip', section)
    if (chip) chip.textContent = '本轮 ' + nodes.length + ' 个阶段'
  }

  // ------------------------------------------------- 工具调用分布（真实计数）
  function renderToolChart(metrics) {
    const section = cardOf('sec-3')
    if (!section) return
    const wrap = $('.wf-wrap', section)
    const hint = $('.wf-head .wf-hint', section)
    const title = $('.wf-head .wf-title', section)
    if (!wrap) return
    const calls = Number(metrics && metrics.mcp && metrics.mcp.calls) || 0
    const errors = Number(metrics && metrics.mcp && metrics.mcp.errors) || 0
    const avgMs = metrics && metrics.mcp ? metrics.mcp.avgMs : null
    const rows = Object.entries((metrics && metrics.mcp && metrics.mcp.tools) || {})
      .map(([name, count]) => ({ name, count: Number(count) || 0 }))
      .sort((a, b) => b.count - a.count)
      .slice(0, 6)
    if (title) title.textContent = '工具调用分布（当日进程累计）'
    if (hint) {
      hint.textContent = '共 ' + calls + ' 次调用 · 失败 ' + errors + ' 次 · 平均 ' + num(avgMs, 0)
        + 'ms · 服务端只暴露调用计数与平均耗时，不提供逐次耗时瀑布'
    }
    if (rows.length === 0) {
      V3.nodata(wrap, '工具调用分布', 'GET /api/v3/metrics 未返回任何工具调用计数（进程内计数为空）')
      return
    }
    const width = 760
    const height = 196
    const padLeft = 118
    const padRight = 744
    const top = 14
    const bottom = 168
    const max = rows[0].count || 1
    const slot = (bottom - top) / rows.length
    const barH = Math.min(16, slot * 0.42)
    const x = (value) => padLeft + (value / max) * (padRight - padLeft)
    const parts = []
    for (let i = 0; i < rows.length; i++) {
      const row = rows[i]
      const y = top + i * slot + slot / 2
      parts.push('<text x="110" y="' + (y + 4).toFixed(1) + '" text-anchor="end" font-size="11" fill="#8b97a5"'
        + ' font-family="ui-monospace,Menlo,Consolas,monospace">' + esc(row.name) + '</text>')
      parts.push('<rect x="' + padLeft + '" y="' + (y - barH / 2).toFixed(1) + '" width="'
        + Math.max(1, x(row.count) - padLeft).toFixed(1) + '" height="' + barH.toFixed(1)
        + '" rx="3" fill="rgba(76,141,255,.22)" stroke="#4c8dff"><title>' + esc(row.name + ' · ' + row.count + ' 次') + '</title></rect>')
      parts.push('<text x="' + (x(row.count) + 6).toFixed(1) + '" y="' + (y + 4).toFixed(1)
        + '" font-size="11" fill="#8b97a5" font-family="ui-monospace,Menlo,Consolas,monospace">' + esc(row.count) + '</text>')
    }
    parts.push('<line x1="' + padLeft + '" y1="' + bottom + '" x2="' + padRight + '" y2="' + bottom + '" stroke="#2f3a49"/>')
    parts.push('<text x="' + padLeft + '" y="186" font-size="10" fill="#626d7c" font-family="ui-monospace,Menlo,Consolas,monospace">0 次</text>')
    parts.push('<text x="' + padRight + '" y="186" text-anchor="end" font-size="10" fill="#626d7c"'
      + ' font-family="ui-monospace,Menlo,Consolas,monospace">' + esc(max) + ' 次</text>')
    wrap.innerHTML = '<svg viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="工具调用次数分布（真实计数）">'
      + parts.join('') + '</svg>'
  }

  // ------------------------------------------------------------ 决策结论
  function renderDecision(run) {
    const section = cardOf('sec-4')
    if (!section) return
    const proposals = asArray(run && run.proposals)
    const turn = $('.d-head .d-turn', section)
    if (turn) turn.textContent = run ? 'as_of ' + stamp(run.asOf) + ' 产出' : '无数据源'

    const badge = $('.d-head .d-badge', section)
    const increases = proposals.filter((item) => String(item.action || '').indexOf('增') >= 0)
    const decreases = proposals.filter((item) => String(item.action || '').indexOf('减') >= 0)
    if (badge) badge.textContent = proposals.length ? ('增持 ' + increases.length + ' / 减持 ' + decreases.length) : '无数据源'

    if (!run || proposals.length === 0) {
      const sec = $$('.d-sec', section)[0]
      if (sec) V3.nodata(sec, '决策结论', '研究流水线未返回任何调仓建议提案（POST /api/v3/strategy/run 可生成一轮）')
      for (const node of $$('.d-sec', section).slice(1)) node.replaceChildren()
      return
    }

    const sections = $$('.d-sec', section)
    if (sections[0]) {
      const head = $('.k', sections[0])
      if (head) head.textContent = '依据（' + proposals.length + ' 条提案）'
      const list = $('.d-points', sections[0])
      if (list) {
        list.innerHTML = values(proposals, (item) => '<li>' + esc(item.ticker) + ' <b>' + esc(item.action || '—')
          + '</b> 目标权重 <span class="num">' + esc(item.targetWeightPct === undefined ? '—' : num(item.targetWeightPct, 1))
          + '%</span> · 风险 <span class="num">' + esc(item.riskLevel || '—') + '</span>：' + esc(item.basis || '—')
          + '。<span style="color:var(--faint)">' + esc(item.action_hint || '经人工审批后执行') + '</span></li>')
      }
    }

    if (sections[1]) {
      const kvs = $$('.kv', sections[1])
      const top = proposals[0]
      const worst = proposals.some((item) => String(item.riskLevel || '').indexOf('高') >= 0) ? '高'
        : proposals.some((item) => String(item.riskLevel || '').indexOf('中') >= 0) ? '中' : '低'
      if (kvs[0]) {
        const value = $('.v', kvs[0])
        if (value) {
          value.innerHTML = '<i class="dot" style="background:' + (worst === '低' ? 'var(--green)' : worst === '中' ? 'var(--amber)' : 'var(--red)')
            + ';display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px"></i>' + esc(worst)
        }
      }
      if (kvs[1]) {
        const value = $('.v', kvs[1])
        if (value) {
          value.className = 'v d-action num'
          value.innerHTML = esc(top.ticker + ' ' + (top.action || '')) + ' <span class="arr">→</span> 目标权重 '
            + esc(num(top.targetWeightPct, 1)) + '%'
        }
      }
      const sub = $('.d-action-sub', sections[1])
      if (sub) {
        const prt = (run.stages || {}).PRT || {}
        sub.innerHTML = '本轮 <span class="num">' + esc(proposals.length) + '</span> 条提案（增持 '
          + esc(increases.length) + ' / 减持 ' + esc(decreases.length) + '）· 每标的权重上限 <span class="num">'
          + esc(prt.weightPctPerName === undefined ? '—' : num(prt.weightPctPerName, 1)) + '%</span> · 待人工审批，研究侧不下单'
      }
    }

    if (sections[2]) {
      const value = $('.conf-row .v', sections[2])
      if (value) {
        value.textContent = '无数据源'
        value.title = '研究流水线不输出置信度，/api/v3/strategy 无该字段'
      }
      const fill = $('.fill.conf', sections[2])
      if (fill) {
        fill.style.width = '0%'
        fill.title = '无数据源：无置信度字段'
      }
    }
  }

  // ------------------------------------------------------------ 引用证据
  function renderEvidence(run, factors, news, brain) {
    const section = cardOf('sec-5')
    if (!section) return
    const list = $('.ev', section)
    const label = $('.d-head .d-turn', section)
    if (!list) return
    const items = []

    if (run) {
      const stages = run.stages || {}
      const pdat = stages.PDAT || {}
      const paat = stages.PAAT || {}
      items.push({
        tag: ['因子', ''],
        title: '研究流水线 PDAT/PAAT：标的池 ' + asArray(run.universe).length + ' 只 · 日 K '
          + (pdat.bars === undefined ? '—' : pdat.bars) + ' 根 · 评分来源 ' + (paat.scoreSource || '—'),
        meta: '/api/v3/strategy · ' + stamp(run.asOf),
      })
    }
    if (factors && factors.ok) {
      const matrix = factors.matrix || {}
      const ic = factors.ic || {}
      items.push({
        tag: ['因子', ''],
        title: '横截面因子 z 矩阵：' + asArray(matrix.tickers).length + ' 只 × ' + asArray(matrix.factors).length
          + ' 因子 · IC（' + (ic.factor || '—') + '，forward ' + (ic.forwardDays === undefined ? '—' : ic.forwardDays) + ' 日）均值 '
          + num(ic.meanIc, 4) + ' · IR ' + num(ic.ir, 2) + ' · 样本 ' + (ic.observations === undefined ? '—' : ic.observations) + ' 期',
        meta: (matrix.source || '/api/v3/factors/matrix') + ' · as_of ' + (matrix.as_of || '—'),
      })
    }
    for (const row of asArray(news && news.rows).slice(0, 2)) {
      items.push({
        tag: ['新闻', 'warn'],
        title: String(row.title || '—'),
        meta: (row.source || '资讯源') + ' · ' + (row.published_at || '—'),
      })
    }
    if (brain && brain.sources && brain.sources.decision) {
      items.push({ tag: ['记录', 'ok'], title: '决策记录：' + String(brain.sources.decision), meta: '本服务落盘路径' })
    }
    for (const entry of asArray(brain && brain.audit).slice(0, 2)) {
      items.push({
        tag: ['审计', ''],
        title: (entry.source_label || entry.kind || '审计') + ' · ' + (entry.ticker ? entry.ticker + ' · ' : '') + String(entry.detail || ''),
        meta: '工作台 audit · ' + stamp(entry.at),
      })
    }

    if (items.length === 0) {
      V3.nodata(list, '引用证据', '研究流水线、因子矩阵、资讯与审计链均未返回可用条目')
      if (label) label.textContent = '无数据源'
      return
    }
    list.innerHTML = values(items, (item) => '<li><span class="etag ' + (item.tag[1] || '') + '">' + esc(item.tag[0])
      + '</span><div><div class="et">' + esc(item.title) + '</div><div class="em">' + esc(item.meta) + '</div></div></li>')
    if (label) label.textContent = '共 ' + items.length + ' 条'
  }

  // -------------------------------------------------------- 运行元信息
  function renderRunMeta(brain) {
    const section = cardOf('sec-6')
    if (!section) return
    const body = $$(':scope > div', section).filter((node) => !node.classList.contains('d-head'))[0]
    const sdkReason = ((brain && brain.sdk) || {}).reason || '本服务未挂载 SDK JSON-RPC 通道'
    const headlessReason = ((brain && brain.headless) || {}).reason || '本服务未挂载 Headless CLI 子通道'
    if (body) {
      // 设计稿的 session id / turn 3 / 12 / 12,480 token / 检查点 全部是占位值：
      // 本服务两类会话通道都未挂载，整块标注无数据源（不保留任何占位数字）。
      V3.nodata(body, 'SDK / Headless 会话元信息（session id · Agent Loop 轮次 · Compaction · token · 检查点）',
        'SDK：' + sdkReason + '；Headless：' + headlessReason + '。本页只提供研究流水线记录（见推理流）。')
    }
  }

  // ------------------------------------------------------------- 去向区
  function renderActions(run, overview) {
    const section = cardOf('sec-7')
    if (!section) return
    const link = $('a.alink', section)
    if (link) {
      const count = asArray(run && run.proposals).length
      link.textContent = run
        ? '依据本结论生成的调仓建议 · ' + count + ' 条提案（as_of ' + stamp(run.asOf) + '）→'
        : '本结论去向 · 无数据源（研究流水线尚无落盘记录）→'
      link.title = '提案经工作台受约束入口人工审批后才可能执行'
    }
    cloneBind('#exportBtn', () => {
      const payload = {
        exported_at: new Date().toISOString(),
        source: '/api/v3/strategy（研究流水线最近一轮）· /api/v3/brain（通道与来源）',
        as_of: run ? run.asOf : null,
        equity_current: (overview && overview.equity && overview.equity.current) || null,
        run: run || null,
      }
      const name = 'v3-decision-snapshot-' + (run && run.asOf ? String(run.asOf).replace(/[:.]/g, '-') : 'nodata') + '.json'
      toast(download(name, JSON.stringify(payload, null, 2))
        ? '决策快照已导出：' + name
        : '导出失败：当前浏览器不支持本地下载')
    })
    cloneBind('#logBtn', () => toast(run
      ? '决策日志：本服务无写入接口；研究产物落盘于 ' + ((run && run.asOf) || '') + ' 的流水线记录'
      : '决策日志：本服务无写入接口，且当前无流水线记录'))
    cloneBind('#copyTrace', () => {
      const text = run ? 'strategy-run@' + run.asOf : '无数据源'
      toast(run ? '已复制研究轮标识：' + text : 'trace：无数据源（SDK/Headless 通道未挂载）')
      // 剪贴板写入不阻塞提示（无焦点/无权限时 promise 可能一直 pending）
      try {
        if (window.navigator && navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).catch(() => {})
        }
      } catch (error) {
        /* 复制失败不影响提示与页面 */
      }
    })
  }

  function renderFooter(brain, run, metrics) {
    const foot = $('footer.footer')
    if (!foot) return
    const sources = (brain && brain.sources) || {}
    foot.innerHTML = '<span>数据来源：/api/v3/strategy（研究流水线 PDAT→PET，'
      + esc(run ? 'as_of ' + stamp(run.asOf) : '无落盘记录') + '）· /api/v3/factors/matrix（因子与 IC）· /api/v3/news（个股资讯）· '
      + '/api/v3/audit（工作台审计链）· /api/v3/metrics（进程内调用计数）· /api/v3/overview（权益台账）；'
      + 'SDK / Headless 通道未挂载的区块已标注「无数据源」，页面不含占位数字</span>'
      + '<span>量化决策平台 V3.0 · Harness-Centric（' + esc(metrics && metrics.ok ? 'workbench 工具面 ' + (metrics.workbenchUp ? '在线' : '不可达') : 'workbench 状态未知') + '）</span>'
    foot.title = String(sources.sdk || '') + ' / ' + String(sources.headless || '')
  }

  // --------------------------------------------------------------- 主流程
  async function render() {
    const [overview, metrics, brain, factors, strategy, audit] = await Promise.all([
      V3.api('overview'), V3.api('metrics'), V3.api('brain'), V3.api('factors/matrix'),
      V3.api('strategy'), V3.api('audit'),
    ])
    const run = strategy && strategy.ok ? strategy.run : null
    const enriched = Object.assign({}, brain && brain.ok ? brain : {}, { audit: audit && audit.ok ? (audit.data && audit.data.entries) : [] })
    const news = await V3.api('news?symbol=' + encodeURIComponent(String((asArray(run && run.proposals)[0] || {}).ticker || 'SH.600519').replace(/^[A-Za-z]+\./, '')) + '&limit=5')

    if (overview && overview.ok) renderTopbar(overview, metrics, brain)
    else V3.nodata($('.topbar .tb-mid'), '顶栏权益', errText(overview, 'GET /api/v3/overview 失败'))

    renderControl(brain, run)
    renderTimeline(run)
    if (metrics && metrics.ok) renderToolChart(metrics)
    else V3.nodata(cardOf('sec-3') && $('.wf-wrap', cardOf('sec-3')), '工具调用分布', errText(metrics, 'GET /api/v3/metrics 失败'))
    if (run) renderDecision(run)
    else renderDecision(null)
    renderEvidence(run, factors, news, enriched)
    renderRunMeta(brain)
    renderActions(run, overview)
    renderFooter(brain, run, metrics)
  }

  purgeDemoScripts()
  render().catch((error) => {
    const main = $('.main .page')
    if (main) V3.nodata(main, '决策大脑页面数据', String((error && error.message) || error))
  })
})()
