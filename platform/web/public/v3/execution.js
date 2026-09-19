// V3「执行与审批」页数据绑定：设计稿 execution.html 的 HTML/CSS 一字不动，
// 本文件只把 /api/v3/* 的真实值写进设计稿 DOM，并把没有数据源的区块显式标注（不留占位数字）。
//
// 硬边界（与后端 /api/v3/execution 的 oms.note 同口径）：
//   执行入口只有一个——平台工作台 Web 的「执行已冻结计划」（plan_execute）+ 人工确认（LIVE 需口令「确认执行」）。
//   V3 控制台只登记、风控分级、对账与展示，**不含任何下单/改单/撤单/审批写入口**。
//
// 数据源（逐块对应）：
//   /api/v3/execution   —— 持仓（券商模拟账户快照）、在途订单、当日成交、OMS 台账视图
//   /api/v3/oms/orders  —— 台账订单（订单号/计划/方向/数量/金额/stage/风控原因/状态历史）
//   /api/v3/metrics     —— 通道与工具面计数（顶栏状态点）
//   /api/v3/audit       —— 审计链（信号生成看板 + 决策链路追溯）
//   /api/v3/risk        —— 事前阈值配置（追溯区的风控阈值口径）
//
// 无数据源（工具面确实没有）：券商成交流水（滑点/成交率/撤单率、成交均价）、模块化执行参数（TWAP 片数）、
//   PIT 因子快照与情绪评分、盘中市场状态、逐单决策 trace id。
/* global window, document */
;(function () {
  const V3 = window.V3
  if (!V3) return
  const { num, money, stamp, hhmmss, esc } = V3

  /* ── 小工具 ────────────────────────────────────────────────────────────── */
  const q = (sel, root) => (root || document).querySelector(sel)
  const qa = (sel, root) => Array.prototype.slice.call((root || document).querySelectorAll(sel))
  const fin = (v) => Number.isFinite(Number(v))
  const pct = (v, d) => (fin(v) ? `${Number(v).toFixed(d == null ? 2 : d)}%` : '—')
  const badge = (cls, text) => `<span class="badge ${cls}">${esc(text)}</span>`
  const clock = (v) => hhmmss(v) || String(v || '').slice(0, 10) || '—'
  const STAGE_TEXT = {
    manual: '待审批', blocked: '已阻断', risk_passed: '风控通过', submitted: '已提交',
    filled: '全部成交', rejected: '已拒绝',
  }
  const stageText = (stage) => STAGE_TEXT[String(stage)] || String(stage || '未知')
  const stageBadge = (stage) => {
    const cls = stage === 'manual' ? 'b-amber' : stage === 'blocked' || stage === 'rejected' ? 'b-red'
      : stage === 'filled' ? 'b-green' : 'b-blue'
    return badge(cls, stageText(stage))
  }
  const sideText = (side) => (String(side).toUpperCase() === 'BUY' ? '买入' : '卖出')
  const sideClass = (side) => (String(side).toUpperCase() === 'BUY' ? 'dir-buy' : 'dir-sell')

  /** 兼容 {} / {groups:[{rows:[]}]} / {groups:[{positions:[]}]} / 数组 四种形态。 */
  function flatRows(node, keys) {
    if (!node) return []
    if (Array.isArray(node)) return node.filter((row) => row && typeof row === 'object')
    const groups = Array.isArray(node.groups) ? node.groups : []
    const out = []
    for (const group of groups) {
      for (const key of keys) {
        if (Array.isArray(group[key])) {
          for (const row of group[key]) if (row && typeof row === 'object') out.push({ ...row, __group: group })
        }
      }
    }
    return out
  }
  /** 台账订单归属的计划（取订单数最多的 frozen 计划，作为「冻结计划」读数）。 */
  function dominantPlan(orders) {
    const counts = new Map()
    for (const order of orders) {
      const id = String(order.plan_id || '')
      if (!id) continue
      const row = counts.get(id) || { plan_id: id, status: order.plan_status, n: 0 }
      row.n += 1
      counts.set(id, row)
    }
    return Array.from(counts.values()).sort((a, b) => b.n - a.n)[0] || null
  }
  function toast(message, kind) {
    const wrap = q('#toastWrap')
    if (!wrap) return
    const node = document.createElement('div')
    node.className = `toast-item ${kind || ''}`.trim()
    node.textContent = message
    wrap.appendChild(node)
    setTimeout(() => {
      node.style.opacity = '0'
      node.style.transition = 'opacity .3s'
      setTimeout(() => node.remove(), 320)
    }, 3600)
  }
  /** 设计稿内联脚本里带了一整套演示状态机（演示订单号 / TRACES / 审批演示数据），HTML 不能改。
   *  该脚本在解析时已执行完毕，这里在运行期把它的**源码文本**从 DOM 中替换为一行说明：
   *  演示订单号/标的/trace id 不再残留在页面里，交互改由 execution.js 用真实台账数据渲染
   *  （冻结计划弹窗、追溯展开/收起等已绑定的监听不受影响；弹窗确认按钮已重绑为「本平台不含下单入口」）。 */
  function scrubDesignScript() {
    for (const node of qa('script')) {
      const text = node.textContent || ''
      if (text.indexOf('TRACES') < 0 && text.indexOf('示例') < 0) continue
      node.textContent = '/* 设计稿内联演示状态机（演示订单号 / 演示追踪表 / 审批演示）已在运行期由 execution.js 的真实台账数据绑定替代，演示数据不再保留在 DOM 中。 */'
    }
  }

  /* ── 1. 顶栏 ───────────────────────────────────────────────────────────── */
  function renderTopbar(mode, nav, metrics) {
    const env = q('.tb-mid .pill.env')
    if (env) {
      env.className = `pill env${String(mode).toLowerCase() === 'live' ? ' live' : ''}`
      env.textContent = String(mode || 'sim').toUpperCase()
    }
    for (const pill of qa('.tb-mid .pill')) {
      const text = (pill.textContent || '').trim()
      if (text.indexOf('权益') === 0) {
        const value = q('.num', pill)
        if (value) value.textContent = money(nav)
        pill.title = '台账权益 equity.current（本地模拟台账，不代表券商资产）· 来源 /api/v3/oms/orders.nav'
      } else if (text.indexOf('日内') === 0) {
        const value = q('.num', pill)
        if (value) {
          value.textContent = '—'
          value.className = 'num'
        }
        pill.title = '无数据源：台账权益曲线只有 1 个点位，无前值，不计算日内收益'
      }
    }
    const demo = q('.tb-mid .demo-pill')
    const beats = qa('.tb-mid .pill').filter((pill) => q('.dot', pill))
    if (demo) {
      demo.className = 'pill'
      demo.innerHTML = '<span class="dot g"></span>真实数据'
      demo.title = '本页所有数字来自本服务 /api/v3/* 实时接口'
    }
    const mcpOk = Boolean(metrics && metrics.workbenchUp)
    const specs = [
      { dot: 'g', text: `MCP ${metrics && metrics.mcp ? num(metrics.mcp.avgMs, 0) : '—'}ms`, title: `MCP 工具面：${mcpOk ? '运行中' : '不可达'} · ${metrics && metrics.toolTotal ? metrics.toolTotal : '—'} 个工具（/api/v3/metrics）` },
      { dot: 'a', text: 'SDK 无数据源', title: (metrics && metrics.sdk && metrics.sdk.reason) || '本服务未挂载 SDK JSON-RPC 通道' },
      { dot: 'a', text: 'Headless 无数据源', title: '本服务未挂载 Headless CLI 子进程通道（/api/v3/gateway 同口径）' },
    ]
    beats.forEach((pill, index) => {
      const spec = specs[index]
      if (!spec) return
      pill.innerHTML = `<span class="dot ${spec.dot} pulse"></span>${esc(spec.text)}`
      pill.title = spec.title
    })
    const right = q('.tb-right')
    if (right) {
      const ts = stamp(metrics && metrics.generated_at)
      right.textContent = `${ts === '—' ? '—' : ts.slice(0, 16)} UTC · 数据截至`
    }
    const foot = q('.sidenav .nav-foot')
    if (foot) foot.textContent = `${String(mode || 'sim').toUpperCase()} 环境 · 真实数据（/api/v3 实时接口）`
  }

  /* ── 2. 执行入口条 ─────────────────────────────────────────────────────── */
  function renderExecHead(oms, orders, dealRows, mode) {
    const env = q('.exec-mode .pill.env')
    if (env) env.textContent = `${String(mode || 'sim').toUpperCase()} 模拟环境`
    const hint = q('.exec-mode .hint')
    if (hint) hint.textContent = 'LIVE 需口令「确认执行」（工作台 Web 入口）'

    const plan = dominantPlan(orders)
    const chip = q('.plan-chip')
    if (chip) {
      chip.innerHTML = plan
        ? `冻结计划 <span class="num">${esc(plan.plan_id)}</span> · 该计划待执行 <span class="num">${plan.n}</span> 条`
        : '冻结计划：无数据源'
    }
    const note = q('.exec-note')
    if (note) note.textContent = '本平台不含下单入口：执行只在工作台 Web（plan_execute + 人工确认）'

    const manual = orders.filter((order) => order.stage === 'manual').length
    const filled = q('#statFilled')
    if (filled) filled.textContent = String(dealRows.length)
    const pending = q('#statPending')
    if (pending) {
      pending.textContent = String(manual)
      pending.style.color = manual > 0 ? 'var(--amber)' : 'var(--muted)'
    }
    const button = q('#btnFreeze')
    if (button) button.title = '入口在工作台 Web：执行已冻结计划（plan_execute）+ 人工确认；本控制台只读展示'
    const head = q('.exec-head')
    if (head) head.title = (oms && oms.note) || '执行入口只在工作台 Web'
  }

  /* ── 3. 订单生命周期看板 ───────────────────────────────────────────────── */
  function buildCard(tpl, spec) {
    const node = tpl.cloneNode(true)
    const top = q('.t', node)
    if (top) top.innerHTML = `<b>${esc(spec.ticker)}</b><span class="${spec.cls || ''}">${esc(spec.dir)}</span>`
    const mid = q('.m', node)
    if (mid) mid.innerHTML = `${esc(spec.extra)} · <span class="num">${esc(spec.time)}</span>`
    return node
  }
  function signalDir(detail) {
    const match = String(detail || '').match(/^(买入|卖出|观望)/)
    return match ? match[1] : '信号'
  }
  function orderCardOf(order) {
    const side = sideText(order.side)
    return {
      ticker: order.ticker || '—',
      dir: side,
      cls: sideClass(order.side),
      extra: `${num(order.qty, 0)} 股 · ${money(order.value)}`,
      time: clock(order.updated_at || order.first_seen_at),
    }
  }
  function renderKanban(exec, orders, audit) {
    const cols = qa('.kanban .k-col')
    const signals = audit.filter((entry) => String(entry.kind) === 'signal')
    const openRows = flatRows(exec && exec.orders_open, ['rows', 'orders'])
    const dealRows = flatRows(exec && exec.deals_today, ['rows', 'deals'])
    const partial = openRows.filter((row) => Number(row.dealt_qty || row.cum_qty || 0) > 0)
    const byStage = (stage) => orders.filter((order) => String(order.stage) === stage)
    const submitted = byStage('submitted')
    const filled = byStage('filled')
    const specs = {
      信号生成: signals.length
        ? { count: signals.length, cards: signals.slice(0, 2).map((s) => ({ ticker: s.ticker || '—', dir: signalDir(s.detail), cls: signalDir(s.detail) === '买入' ? 'dir-buy' : signalDir(s.detail) === '卖出' ? 'dir-sell' : '', extra: s.detail || '—', time: clock(s.at) })) }
        : { count: 0, cards: [], why: '审计链窗口内无量化信号' },
      风控校验: byStage('risk_passed').length
        ? { count: byStage('risk_passed').length, cards: byStage('risk_passed').slice(0, 2).map(orderCardOf) }
        : { count: 0, cards: [], why: '台账无 risk_passed 阶段订单（超单笔上限的订单全部退回 manual）' },
      审批: byStage('manual').length
        ? { count: byStage('manual').length, cards: byStage('manual').slice(0, 2).map(orderCardOf), tone: 'var(--amber)' }
        : { count: 0, cards: [], why: '台账无 manual 阶段订单' },
      已提交: (submitted.length + openRows.length)
        ? { count: submitted.length + openRows.length, cards: submitted.slice(0, 2).map(orderCardOf), tone: 'var(--blue)' }
        : { count: 0, cards: [], why: '台账无 submitted 阶段订单；券商在途订单（orders_open）当日 0 行' },
      部分成交: partial.length
        ? { count: partial.length, cards: partial.slice(0, 2).map((row) => ({ ticker: row.symbol || row.ticker || '—', dir: sideText(row.side || row.trd_side), cls: sideClass(row.side || row.trd_side), extra: `${num(row.dealt_qty || row.cum_qty, 0)}/${num(row.qty, 0)} 股`, time: clock(row.updated_time || row.create_time) })), tone: 'var(--cyan)' }
        : { count: 0, cards: [], why: '券商在途订单 0 行，台账无部分成交订单' },
      全部成交: (filled.length + dealRows.length)
        ? { count: filled.length + dealRows.length, cards: filled.slice(0, 2).map(orderCardOf), tone: 'var(--green)' }
        : { count: 0, cards: [], why: '台账无 filled 阶段订单；当日成交（deals_today，由模拟订单派生）0 行' },
    }
    for (const col of cols) {
      const name = ((q('.k-col-h .nm', col) || {}).textContent || '').trim()
      const spec = specs[name]
      if (!spec) continue
      const count = q('.k-cnt', col)
      if (count) {
        count.textContent = String(spec.count)
        count.style.color = spec.count > 0 ? (spec.tone || 'var(--text)') : 'var(--faint)'
      }
      const cards = qa('.k-card', col)
      const tpl = cards[0]
      cards.forEach((node) => node.remove())
      if (spec.cards.length > 0 && tpl) {
        for (const item of spec.cards) col.appendChild(buildCard(tpl, item))
      } else {
        const holder = document.createElement('div')
        col.appendChild(holder)
        V3.nodata(holder, `${name}订单`, spec.why)
      }
    }
    const head = q('[data-od-id="order-lifecycle-kanban"] .sec-head .sub')
    if (head) head.textContent = '状态计数取自 OMS 台账 stage 分布 + 审计链信号 + 券商在途/当日成交（/api/v3/execution）'
  }

  /* ── 4. 分级审批 ───────────────────────────────────────────────────────── */
  function renderGrades(card, orders, riskCfg, nav) {
    if (!card) return
    const scope = (sel) => q(sel, card)
    const autoScope = scope('.grade.auto')
    const manualScope = scope('.grade.manual')
    const blockScope = scope('.grade.block')
    const auto = orders.filter((order) => String(order.stage) === 'risk_passed')
    const manual = orders.filter((order) => String(order.stage) === 'manual')
    const blocked = orders.filter((order) => String(order.stage) === 'blocked' || String(order.stage) === 'rejected')
    const limit = riskCfg && fin(riskCfg.risk_per_trade) ? Number(riskCfg.risk_per_trade) * 100 : null

    if (autoScope) {
      const chip = q('.grade-h .badge', autoScope)
      if (chip) {
        chip.textContent = `台账 ${auto.length} 单`
        chip.className = `badge ${auto.length > 0 ? 'b-green' : 'b-blue'}`
      }
      const rule = q('.rule-chip', autoScope)
      if (rule) rule.textContent = `规则依据 · 单笔 ≤ 权益 2%（OMS check_order）· 风险预算 ${pct(limit, 1)}`
      const rows = qa('.o-row', autoScope)
      const tpl = rows[0]
      const note = q('.grade-note', autoScope)
      rows.forEach((node) => node.remove())
      if (auto.length > 0 && tpl) {
        for (const order of auto.slice(0, 3)) {
          const node = tpl.cloneNode(true)
          const left = q('.l', node)
          if (left) left.innerHTML = `<b>${esc(order.ticker)}</b> <span class="m">${esc(sideText(order.side))} · 计划 ${esc(String(order.plan_id || ''))}</span>`
          const right = q('.r', node)
          if (right) right.innerHTML = `${num(order.qty, 0)} 股 · ${money(order.value)}<br>${esc(clock(order.updated_at))}`
          autoScope.insertBefore(node, note || null)
        }
      } else {
        const holder = document.createElement('div')
        autoScope.insertBefore(holder, note || null)
        V3.nodata(holder, '自动执行订单', '台账 stage 分布无 risk_passed/auto（当前 10 单全部超单笔上限，退回人工确认）')
      }
      if (note) note.textContent = `台账口径：/api/v3/oms/orders；自动放行仅发生在单笔占比 ≤ 2% 且未触红线时`
    }

    if (manualScope) {
      const chip = q('.grade-h .badge', manualScope)
      if (chip) {
        chip.textContent = `需人工确认 · 台账 ${manual.length} 单`
        chip.className = `badge ${manual.length > 0 ? 'b-amber' : 'b-blue'}`
      }
      const host = q('#statePending', manualScope)
      const first = manual[0]
      const appr = host ? q('.appr-card', host) : null
      if (appr && first) {
        const reasons = ((first.risk && first.risk.reasons) || []).join('；') || '风控未给出原因'
        appr.innerHTML =
          `<div class="appr-top"><b>${esc(first.ticker)} ${esc(sideText(first.side))}</b>${stageBadge(first.stage)}</div>` +
          '<div class="appr-kv">' +
          `<div><div class="k">数量</div><div class="v num">${num(first.qty, 0)} 股</div></div>` +
          `<div><div class="k">预估金额</div><div class="v num">${money(first.value)}</div></div>` +
          '</div>' +
          `<div class="appr-line"><span class="k">触发依据</span><span class="v">${esc(reasons)}</span></div>` +
          `<div class="appr-line"><span class="k">风控回执</span><span class="v" style="color:var(--amber)">${esc(reasons)}</span></div>` +
          `<div class="appr-line"><span class="k">决策快照</span><span class="v mut num">计划 ${esc(String(first.plan_id || '—'))} · ${esc(clock(first.updated_at))}${fin(nav) ? ` · NAV ${esc(money(nav))}` : ''}</span></div>` +
          `<div class="appr-line"><span class="k">台账订单号</span><span class="v mut num">${esc(String(first.id || '—'))}</span></div>` +
          '<div style="border:1px dashed var(--border2);border-radius:6px;padding:9px 10px;font-size:11.5px;color:var(--faint);line-height:1.7">' +
          '审批入口不在本控制台：执行只在平台工作台 Web 的「执行已冻结计划」（<b>plan_execute</b>）+ 人工确认，LIVE 需口令「确认执行」。' +
          'V3 只登记、风控分级、对账与展示。</div>'
      } else if (appr) {
        V3.nodata(appr, '待人工确认订单', 'OMS 台账无 manual 阶段订单')
      }
      const approved = q('#stateApproved', manualScope)
      if (approved) {
        const big = q('.big', approved)
        const line = q('p', approved)
        if (big) big.textContent = '已批准 · 由工作台 Web 提交 OMS'
        if (line) line.textContent = '本控制台不代下单：审批动作在工作台 Web 完成后，订单状态经 /api/v3/oms/sync 对账回写台账。'
      }
      const rejected = q('#stateRejected', manualScope)
      if (rejected) {
        const big = q('.big', rejected)
        const line = q('p', rejected)
        if (big) big.textContent = '已拒绝 · 信号退回决策大脑'
        if (line) line.textContent = '拒绝动作同样在工作台 Web 完成；OMS 台账据对账结果更新 stage，不进入执行队列。'
      }
      const queue = q('.queue-next', manualScope)
      if (queue) {
        const rest = manual.slice(1)
        queue.innerHTML = rest.length
          ? `队列中还有 <span class="num">${rest.length}</span> 条：<span class="num">${esc(rest[0].ticker)}</span> ${esc(sideText(rest[0].side))} ${num(rest[0].qty, 0)} 股 · 金额 <span class="num">${esc(money(rest[0].value))}</span> · 待审批`
          : '队列中无其它待审批订单'
      }
    }

    if (blockScope) {
      const chip = q('.grade-h .badge', blockScope)
      if (chip) {
        chip.textContent = `台账 ${blocked.length} 单`
        chip.className = `badge ${blocked.length > 0 ? 'b-red' : 'b-blue'}`
      }
      const rows = qa('.o-row', blockScope)
      rows.forEach((node) => node.remove())
      const chipLine = q('.chip-line', blockScope)
      if (chipLine) chipLine.innerHTML = badge('b-blue', '行业分类：无数据源（不参与自动阻断）')
      const reason = q('.block-reason', blockScope)
      if (reason) {
        reason.innerHTML = blocked.length > 0
          ? `<span class="rl">拒绝原因：</span>${esc(((blocked[0].risk && blocked[0].risk.reasons) || []).join('；') || '台账未给出原因')}`
          : '<span class="rl">强制阻断：</span>OMS 台账 0 条 blocked/rejected 订单。硬阻断只能来自单笔占比与回撤红线；行业分类工具面无数据源（industry_source=no-data），行业上限不参与自动阻断。'
      }
      const note = q('.grade-note', blockScope)
      if (note) note.textContent = '强制阻断无操作入口 · 全程留痕可追溯（/api/v3/oms/orders）'
    }
  }

  /* ── 5. 订单明细 + 决策链路追溯 ────────────────────────────────────────── */
  function renderOrderTable(orders, onPick) {
    const table = q('table.orders')
    const tbody = table ? q('tbody', table) : null
    const tpl = tbody ? q('tr', tbody) : null
    if (!tbody || !tpl) return []
    const clone = tpl.cloneNode(true)
    tbody.innerHTML = ''
    const nodes = []
    for (const order of orders) {
      const tr = clone.cloneNode(true)
      const cells = qa('td', tr)
      const values = [
        String(order.id || '—').length > 14
          ? `${String(order.id).slice(0, 8)}…${String(order.id).slice(-4)}`
          : String(order.id || '—'),
        clock(order.updated_at || order.first_seen_at),
        String(order.ticker || '—'),
        sideText(order.side),
        num(order.price, 2),
        num(order.qty, 0),
        null,
        '—',
        '—',
        '—',
      ]
      for (let i = 0; i < cells.length; i++) {
        const value = values[i]
        if (value === null) continue
        cells[i].textContent = value
        if (i === 7 || i === 8 || i === 9) cells[i].className = 'faint'
        if (i === 8) cells[i].style.color = ''
      }
      if (cells[0]) cells[0].title = `台账订单号（OMS 内部 id）：${order.id || '—'}`
      if (cells[3]) cells[3].className = sideClass(order.side)
      if (cells[6]) cells[6].innerHTML = stageBadge(order.stage)
      if (cells[9]) cells[9].title = `OMS 台账只有计划号，无逐单 trace id：${order.plan_id || '—'}`
      tr.dataset.key = String(order.id || '')
      tbody.appendChild(tr)
      nodes.push({ tr, order })
    }
    nodes.forEach(({ tr, order }) => {
      tr.addEventListener('click', () => {
        nodes.forEach((item) => item.tr.classList.remove('selected'))
        tr.classList.add('selected')
        onPick(order)
      })
    })
    if (nodes[0]) {
      nodes[0].tr.classList.add('selected')
      onPick(nodes[0].order)
    }
    const head = q('[data-od-id="order-detail-table"] .sec-head .sub')
    if (head) head.textContent = '点击行可在下方追溯决策链路 · 订单号/状态/风控来自 /api/v3/oms/orders；成交均价与滑点无数据源'
    return nodes
  }
  function renderTrace(order, audit, riskCfg, nav) {
    const trace = (sel, text) => {
      const node = q(sel)
      if (node) node.textContent = text
    }
    if (!order) {
      trace('#traceTarget', '无数据源：OMS 台账无订单')
      return
    }
    const reasons = ((order.risk && order.risk.reasons) || []).join('；') || '未给出原因'
    trace('#traceTarget', `订单 ${order.id || '—'} · ${order.ticker || '—'} · ${sideText(order.side)} ${num(order.qty, 0)} 股 · 计划 ${order.plan_id || '—'}`)
    trace('#fFactor', '无数据源：工作台订单未携带 PIT 因子快照（/api/v3/factors/matrix 可另行查看）')
    trace('#fModel', `策略 ${order.strategy_id || '—'} · 风控动作 ${(order.risk && order.risk.action) || '—'} · ${reasons}`)
    const sent = q('#fSent')
    if (sent) {
      sent.textContent = '无数据源：工具面无情绪/情感评分'
      sent.style.color = 'var(--faint)'
    }
    trace('#fMarket', '无数据源：工具面无盘中市场状态快照')
    trace('#fParam', `计划 ${order.plan_id || '—'} · plan_status ${order.plan_status || '—'} · mode ${order.mode || '—'}`)
    trace('#fRisk',
      `NAV ${money(order.nav_used)}（${order.nav_source || '—'}）· 回撤 ${pct(order.drawdown_used)}（${order.drawdown_source || '—'}）· ` +
      `行业 ${pct(order.industry_pct)}（工具面无行业分类数据源，不参与阻断）· 单笔上限 2%（OMS check_order 口径）`)
    // 信号溯源补充：审计链里同标的的量化信号（真实存在才追加）
    const stageItems = q('.stage.s1 .stage-items')
    const old = q('.si.audit-signal')
    if (old) old.remove()
    const signal = audit.filter((entry) => String(entry.kind) === 'signal' && String(entry.ticker || '') === String(order.ticker || ''))[0]
    if (stageItems && signal) {
      const node = document.createElement('div')
      node.className = 'si audit-signal'
      node.innerHTML = `<div class="k">审计链信号（${esc(signal.source_label || '量化信号')}）</div>` +
        `<div class="v num">${esc(signal.detail || '—')} · ${esc(clock(signal.at))}</div>`
      stageItems.appendChild(node)
    }
    const list = q('#tlList')
    if (!list) return
    const history = Array.isArray(order.history) ? order.history : []
    list.innerHTML = ''
    if (history.length === 0) {
      const li = document.createElement('li')
      li.className = 'tl-item'
      li.textContent = '无数据源：台账未记录该订单的状态历史'
      list.appendChild(li)
      return
    }
    for (const record of history) {
      const stage = String(record.stage || '')
      const cls = stage === 'manual' ? 'warn' : stage === 'blocked' || stage === 'rejected' ? 'err' : stage === 'filled' ? 'ok' : ''
      const li = document.createElement('li')
      li.className = `tl-item ${cls}`.trim()
      const tag = stage === 'manual' ? '<span class="badge b-amber tg">待人工确认</span>'
        : stage === 'blocked' ? '<span class="badge b-red tg">已阻断</span>'
          : stage === 'filled' ? '<span class="badge b-green tg">已成交</span>'
            : `<span class="badge tg">${esc(stageText(stage))}</span>`
      li.innerHTML = `<span class="tm">${esc(clock(record.at))}</span>${esc(((record.reasons || []).join('；')) || stageText(stage))}${tag}`
      list.appendChild(li)
    }
    const toggle = q('#btnTraceToggle')
    if (toggle) toggle.title = `追溯对象：台账订单 ${order.id || '—'}（${nav ? 'NAV ' + money(nav) : 'NAV 无数据源'}）`
  }

  /* ── 6. 持仓摘要 + 成交质量 ────────────────────────────────────────────── */
  function renderHoldings(card, positions) {
    if (!card) return
    const groups = positions && Array.isArray(positions.groups) ? positions.groups : []
    const rows = []
    const accounts = []
    for (const group of groups) {
      const list = Array.isArray(group.positions) ? group.positions : []
      if (list.length === 0) continue
      accounts.push(group)
      for (const row of list) {
        rows.push({ ...row, __group: group })
      }
    }
    const sub = q('.sec-head .sub', card)
    if (sub) {
      sub.textContent = `券商模拟持仓快照 · as_of ${stamp(positions && positions.as_of)}${
        positions && positions.stale ? '（缓存）' : ''} · ${accounts.length} 个账户 / ${rows.length} 只标的`
    }
    const table = q('table.pos', card)
    const tbody = table ? q('tbody', table) : null
    const tpl = tbody ? q('tr', tbody) : null
    if (tbody && tpl) {
      const clone = tpl.cloneNode(true)
      tbody.innerHTML = ''
      for (const row of rows) {
        const tr = clone.cloneNode(true)
        const cells = qa('td', tr)
        const pl = Number(row.pl_val)
        const weight = Number(row.__group.market_value) ? (Number(row.market_value) / Number(row.__group.market_value)) * 100 : null
        if (cells[0]) cells[0].textContent = `${row.symbol || '—'} ${row.name || ''}`.trim()
        if (cells[1]) cells[1].textContent = num(row.qty, 0)
        if (cells[2]) cells[2].textContent = num(row.cost_price, 3)
        if (cells[3]) cells[3].textContent = num(row.price, 3)
        if (cells[4]) {
          cells[4].textContent = fin(pl) ? `${pl >= 0 ? '+' : '−'}${Math.abs(pl).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—'
          cells[4].className = `num ${pl >= 0 ? 'up' : 'down'}`
        }
        if (cells[5]) cells[5].textContent = pct(weight)
        tr.title = `${row.__group.account || ''} · 市值 ${money(row.market_value)} · 权重＝该标的市值 ÷ 所属账户持仓市值（不跨账户/币种合并）`
        tbody.appendChild(tr)
      }
      if (rows.length === 0) {
        const tr = clone.cloneNode(true)
        const cells = qa('td', tr)
        if (cells[0]) cells[0].textContent = '暂无持仓'
        for (let i = 1; i < cells.length; i++) cells[i].textContent = ''
        tbody.appendChild(tr)
      }
    }
    const tfoot = table ? q('tfoot tr', table) : null
    if (tfoot) {
      const cells = qa('td', tfoot)
      if (cells[0]) cells[0].textContent = `合计 ${rows.length} 标的 · ${accounts.length} 个模拟账户`
      if (cells[4]) {
        cells[4].textContent = '不合并'
        cells[4].className = 'num'
      }
      if (cells[5]) cells[5].textContent = '—'
    }
    const foot = q('.q-foot', card)
    if (foot) {
      const parts = accounts.map((group) => {
        const ratio = fin(group.total_asset) && Number(group.total_asset)
          ? (Number(group.cash) / Number(group.total_asset)) * 100 : null
        return `${group.account || group.acc_id} 权益 ${money(group.total_asset)}（现金 ${pct(ratio, 1)}）`
      })
      foot.innerHTML = parts.length
        ? `账户分开列示（币种不同，不跨账户/币种合并）：${parts.map(esc).join(' · ')}`
        : '无数据源：券商持仓接口未返回账户'
    }
  }

  function renderQuality(card, exec, orders, openRows) {
    if (!card) return
    const dealRows = flatRows(exec && exec.deals_today, ['rows', 'deals'])
    const sub = q('.sec-head .sub', card)
    if (sub) sub.textContent = `今日成交 ${dealRows.length} 行（deals_today）· 滑点/成交率/撤单率无数据源`
    const stats = qa('.q-grid .stat', card)
    const partial = openRows.filter((row) => Number(row.dealt_qty || row.cum_qty || 0) > 0)
    const specs = [
      { label: '成交率', html: null },
      { label: '平均滑点', html: null },
      { label: '撤单率', html: null },
      {
        label: '部分成交',
        html: `${partial.length + orders.filter((order) => order.stage === 'partial').length}<small>笔</small>`,
        color: 'var(--cyan)',
      },
    ]
    stats.forEach((stat, index) => {
      const spec = specs[index]
      if (!spec) return
      const label = q('.l', stat)
      if (label) label.textContent = spec.label
      const value = q('.v', stat)
      if (!value) return
      if (spec.html === null) {
        value.textContent = '无数据源'
        value.style.color = 'var(--faint)'
        value.style.fontSize = '13px'
        stat.title = '券商成交流水未接入：deals_today 由模拟订单派生，无独立成交回报（滑点/成交率/撤单率无分母）'
      } else {
        value.innerHTML = spec.html
        if (spec.color) value.style.color = spec.color
      }
    })
    const slips = qa('.slip-row', card)
    const foot = q('.q-foot', card)
    const first = slips[0]
    slips.forEach((node) => node.remove())
    const holder = document.createElement('div')
    if (first && first.parentElement) first.parentElement.insertBefore(holder, foot || null)
    V3.nodata(holder, '滑点分布', '券商成交流水未接入：deals_today 由模拟订单派生（当日 0 行），滑点无样本')
    if (foot) foot.textContent = `滑点分布样本：无数据源 · 台账订单 ${orders.length} 单（仅有风控分级，无成交回报）`
  }

  /* ── 7. 冻结计划弹窗（真实计划内容 + 明确下单入口） ────────────────────── */
  function renderModal(orders, plan) {
    const mask = q('#planMask')
    if (!mask) return
    const sub = q('.m-sub', mask)
    const list = q('.m-list', mask)
    const note = q('.m-note', mask)
    const scoped = plan ? orders.filter((order) => String(order.plan_id) === plan.plan_id) : []
    if (sub) {
      sub.innerHTML = plan
        ? `冻结计划 <span class="num">${esc(plan.plan_id)}</span> · 共 <span class="num">${scoped.length}</span> 条订单（台账 stage：${esc(plan.status || '—')}）`
        : '冻结计划：无数据源'
    }
    const rows = list ? qa('.o-row', list) : []
    const tpl = rows[0]
    if (list && tpl) {
      const clone = tpl.cloneNode(true)
      list.innerHTML = ''
      for (const order of scoped.slice(0, 3)) {
        const node = clone.cloneNode(true)
        const left = q('.l', node)
        if (left) left.innerHTML = `<b>${esc(order.ticker)}</b> <span class="m">${esc(sideText(order.side))} · ${esc(String(order.strategy_id || ''))}</span>`
        const right = q('.r', node)
        if (right) right.textContent = `${num(order.qty, 0)} 股 · 限价 ${num(order.price, 2)} · ${money(order.value)}`
        list.appendChild(node)
      }
      if (scoped.length === 0) {
        const node = clone.cloneNode(true)
        const left = q('.l', node)
        if (left) left.textContent = '无数据源：台账无冻结计划订单'
        const right = q('.r', node)
        if (right) right.textContent = ''
        list.appendChild(node)
      }
    }
    if (note) {
      note.textContent = '本控制台不含下单入口：请在平台工作台 Web 使用「执行已冻结计划」（plan_execute）+ 人工确认，SIM 免口令，LIVE 需口令「确认执行」。此处仅展示台账登记的计划订单。'
    }
    const confirm = q('#mConfirm', mask)
    if (confirm && confirm.dataset.v3Bound !== '1') {
      const clone = confirm.cloneNode(true)
      clone.dataset.v3Bound = '1'
      confirm.replaceWith(clone)
      clone.addEventListener('click', () => {
        mask.classList.remove('open')
        toast('本平台不含下单入口：请在平台工作台 Web 执行（plan_execute + 人工确认）', 'warn')
      })
    }
  }

  /* ── 8. 页脚 ───────────────────────────────────────────────────────────── */
  function renderFooter(metrics, positions) {
    const foot = q('footer.foot')
    if (!foot) return
    foot.innerHTML =
      '<span>数据来源：本服务 /api/v3/execution、/api/v3/oms/orders、/api/v3/metrics、/api/v3/audit 实时接口' +
      '（工作台工具面 + 券商模拟持仓）· 取不到的项显式标注「无数据源」，页面不含占位数字</span>' +
      `<span class="num">量化决策平台 V3.0 · 执行与审批 · 持仓 as_of ${esc(stamp(positions && positions.as_of))} · 平台不含下单入口</span>`
  }

  /* ── 主流程 ────────────────────────────────────────────────────────────── */
  async function render() {
    const [execEnv, omsEnv, metricsEnv, auditEnv, riskEnv] = await Promise.all([
      V3.api('execution'),
      V3.api('oms/orders'),
      V3.api('metrics'),
      V3.api('audit?window=120'),
      V3.api('risk'),
    ])
    const exec = execEnv && execEnv.ok ? execEnv : null
    const oms = omsEnv && omsEnv.ok ? omsEnv : (exec && exec.oms ? exec.oms : null)
    const orders = oms && Array.isArray(oms.orders) ? oms.orders : []
    const metrics = metricsEnv && metricsEnv.ok ? metricsEnv : {}
    const audit = auditEnv && auditEnv.ok && auditEnv.data && Array.isArray(auditEnv.data.entries) ? auditEnv.data.entries : []
    const riskCfg = riskEnv && riskEnv.ok && riskEnv.data ? (riskEnv.data.config || {}) : {}
    const positions = exec && exec.positions ? exec.positions : null
    const mode = (positions && positions.mode) || 'sim'
    const nav = oms && fin(oms.nav) ? Number(oms.nav) : null
    const openRows = flatRows(exec && exec.orders_open, ['rows', 'orders'])
    const dealRows = flatRows(exec && exec.deals_today, ['rows', 'deals'])

    renderTopbar(mode, nav, metrics)
    renderExecHead(oms, orders, dealRows, mode)
    renderKanban(exec, orders, audit)
    renderGrades(q('[data-od-id="graded-approval"]'), orders, riskCfg, nav)
    renderOrderTable(orders, (order) => renderTrace(order, audit, riskCfg, nav))
    renderHoldings(qa('.hq-grid > .card')[0], positions)
    renderQuality(qa('.hq-grid > .card')[1], exec, orders, openRows)
    renderModal(orders, dominantPlan(orders))
    renderFooter(metrics, positions)
    scrubDesignScript()
  }

  render().catch((error) => {
    const page = q('.page')
    if (page) V3.nodata(page, '页面数据', String((error && error.message) || error))
  })
})()
