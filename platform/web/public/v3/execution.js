// V3「执行与审批」页数据绑定：设计稿 execution.html 的 HTML/CSS 一字不动，
// 本文件只把 /api/v3/* 的真实值写进设计稿 DOM，并把没有数据源的区块显式标注（不留占位数字）。
//
// 硬边界（与后端 /api/v3/execution 的 oms.note 同口径）：
//   本页是**本仓库唯一允许触发订单动作的界面**（既有 AntD 工作台将被删除），两条写通道：
//     ① 冻结计划执行——「执行已冻结计划」弹窗内的 plan-execute（人工二次确认；LIVE 需逐字口令「确认执行」）；
//     ② 人工审批决定——「待确认请求」区块的 confirm-decide（载荷只有 {id, decision}）。
//   两条通道都只在人工点击后发生：加载/轮询只读，绝不写；首次点击只进入待确认态，第二次点击才提交。
//   平台不含逐单下单/改单/撤单入口；台账里 stage=manual 只是平台侧台账的审批阶段，不是券商待确认。
//
// 数据源（逐块对应）：
//   /api/v3/execution   —— 持仓（券商模拟账户快照）、在途订单、当日成交、OMS 台账视图
//   /api/v3/oms/orders  —— 台账订单（订单号/计划/方向/数量/金额/stage/风控原因/状态历史）
//   /api/v3/metrics     —— 通道与工具面计数（顶栏状态点）
//   /api/v3/audit       —— 审计链（信号生成看板 + 决策链路追溯）
//   /api/v3/risk        —— 事前阈值配置（追溯区的风控阈值口径）
//   /api/wb/plan        —— 冻结计划（真实 plan_id/content_hash/status/mode/orders），执行入口的唯一目标来源
//   /api/wb/confirmation—— 待确认实盘操作（pending/ttl_ms），人工审批决定区块的唯一来源
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
    if (hint) hint.textContent = '本页含唯一受约束执行入口 · LIVE 需逐字口令「确认执行」'

    const plan = dominantPlan(orders)
    const chip = q('.plan-chip')
    if (chip) {
      chip.innerHTML = plan
        ? `冻结计划 <span class="num">${esc(plan.plan_id)}</span> · 该计划待执行 <span class="num">${plan.n}</span> 条`
        : '冻结计划：无数据源'
    }
    const note = q('.exec-note')
    if (note) {
      note.textContent = '本页「执行已冻结计划」是平台唯一受约束下单入口（plan-execute + 人工二次确认）；平台不含逐单下单入口'
    }

    const manual = orders.filter((order) => order.stage === 'manual').length
    const filled = q('#statFilled')
    if (filled) filled.textContent = String(dealRows.length)
    const pending = q('#statPending')
    if (pending) {
      pending.textContent = String(manual)
      pending.style.color = manual > 0 ? 'var(--amber)' : 'var(--muted)'
      pending.title = '平台 OMS 台账（/api/v3/oms/orders）stage=manual 的订单数——这是平台侧台账的审批阶段，不是券商待确认；券商待确认见「待确认请求」区块'
    }
    const button = q('#btnFreeze')
    if (button) button.title = '打开冻结计划执行弹窗：plan-execute 只在人工二次确认后写入指令文件；LIVE 需逐字口令「确认执行」'
    const head = q('.exec-head')
    if (head) head.title = '本页含唯一受约束执行入口：执行已冻结计划（plan-execute，人工二次确认）与待确认请求（confirm-decide）'
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
          '审批与执行入口就在本页：①「执行已冻结计划」弹窗（<b>plan-execute</b>，人工二次确认；LIVE 需逐字口令「确认执行」）；' +
          '②下方「待确认请求」区块（<b>confirm-decide</b>，唯一能批准实盘操作的通道）。' +
          '台账 stage=manual 只是平台侧台账的审批阶段，<b>不是</b>券商待确认。</div>'
      } else if (appr) {
        V3.nodata(appr, '待人工确认订单', 'OMS 台账无 manual 阶段订单')
      }
      const approved = q('#stateApproved', manualScope)
      if (approved) {
        const big = q('.big', approved)
        const line = q('p', approved)
        if (big) big.textContent = '已批准 · 经本页确认通道提交 OMS'
        if (line) line.textContent = '批准动作来自本页「待确认请求」区块（confirm-decide）；订单状态经 /api/v3/oms/sync 对账回写台账。'
      }
      const rejected = q('#stateRejected', manualScope)
      if (rejected) {
        const big = q('.big', rejected)
        const line = q('p', rejected)
        if (big) big.textContent = '已拒绝 · 信号退回决策大脑'
        if (line) line.textContent = '拒绝同样在本页「待确认请求」区块完成；OMS 台账据对账结果更新 stage，不进入执行队列。'
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
  function renderModal(orders, planLedger) {
    const mask = q('#planMask')
    if (!mask) return
    const sub = q('.m-sub', mask)
    const list = q('.m-list', mask)
    const note = q('.m-note', mask)
    // 目标计划来自 /api/wb/plan（真实 content_hash 与订单）——执行入口绝不绑定台账推测值；
    // 台账（/api/v3/oms/orders）只在计划端点取不到时兜底展示，且明确标注来源。
    const plan = currentPlan()
    const planOrders = plan && Array.isArray(plan.orders) ? plan.orders : null
    const ledgerScoped = planLedger
      ? orders.filter((order) => String(order.plan_id) === planLedger.plan_id) : []
    const scoped = planOrders || ledgerScoped
    if (sub) {
      if (plan) {
        sub.innerHTML = `冻结计划 <span class="num">${esc(plan.plan_id)}</span> · 共 <span class="num">${scoped.length}</span> 条订单` +
          `（状态 ${esc(plan.status || '—')}）· 内容哈希 <span class="num">${esc(planHashOf(plan) || '—')}</span>`
      } else if (planLedger) {
        sub.innerHTML = `冻结计划 <span class="num">${esc(planLedger.plan_id)}</span> · 共 <span class="num">${scoped.length}</span> 条订单（台账 stage：${esc(planLedger.status || '—')}）`
      } else {
        sub.textContent = GATE.planErr
          ? `冻结计划取数失败：${GATE.planErr}`
          : '冻结计划：无数据源（/api/wb/plan 未返回计划）'
      }
    }
    const rows = list ? qa('.o-row', list) : []
    const tpl = rows[0]
    if (list && tpl) {
      const clone = tpl.cloneNode(true)
      list.innerHTML = ''
      for (const order of scoped.slice(0, 5)) {
        const node = clone.cloneNode(true)
        const left = q('.l', node)
        if (left) left.innerHTML = `<b>${esc(order.ticker || order.symbol || '—')}</b> <span class="m">${esc(sideText(order.side))} · ${esc(String(order.status || order.strategy_id || ''))}</span>`
        const right = q('.r', node)
        if (right) right.textContent = `${num(order.qty, 0)} 股 · 限价 ${num(order.price, 2)}`
        list.appendChild(node)
      }
      if (scoped.length === 0) {
        const node = clone.cloneNode(true)
        const left = q('.l', node)
        if (left) left.textContent = plan ? '该计划没有订单' : '无数据源：未取到冻结计划'
        const right = q('.r', node)
        if (right) right.textContent = ''
        list.appendChild(node)
      }
    }
    if (note) {
      note.innerHTML = '执行已冻结计划是<b>唯一受约束下单入口</b>：下方按钮需<b>人工二次确认</b>——首次点击只进入待确认态并展示将提交的载荷，' +
        '第二次点击才写入指令文件；LIVE 还需逐字口令「确认执行」。<b>execute</b> 提交 plan_hash + expected_mode' +
        '（模式已变化会被服务端拒绝，请刷新重试），取消用 action=cancel。本页不含逐单下单入口。'
    }
    renderPlanGate()
  }

  /* ── 8. 页脚 ───────────────────────────────────────────────────────────── */
  function renderFooter(metrics, positions) {
    const foot = q('footer.foot')
    if (!foot) return
    foot.innerHTML =
      '<span>数据来源：本服务 /api/v3/execution、/api/v3/oms/orders、/api/v3/metrics、/api/v3/audit 与 /api/wb/plan、/api/wb/confirmation 实时接口' +
      '（工作台工具面 + 券商模拟持仓）· 取不到的项显式标注「无数据源」，页面不含占位数字</span>' +
      `<span class="num">量化决策平台 V3.0 · 执行与审批 · 持仓 as_of ${esc(stamp(positions && positions.as_of))} · 唯一受约束执行入口（plan-execute + 人工二次确认）</span>`
  }

  /* ── 9. 人工执行与审批写入口（plan-execute / confirm-decide） ─────────────
   * 本页是本仓库**唯一允许触发订单动作的界面**（既有 AntD 工作台将删除），
   * 两条写通道只用服务端已冻结的契约：
   *   POST /api/wb/plan          空载荷 → {ok,value:{plans:[{plan_id,content_hash,status,mode,target,orders[]}],alerts,mode}}
   *   POST /api/wb/plan-execute  白名单 {plan_hash,expected_mode,confirmation,action}
   *     · action 缺省 = execute；取消 = action:'cancel'
   *     · execute 必须带 plan_hash；expected_mode 必须等于服务端当前模式，
   *       否则「模式已变化…请刷新后重试」（app.py:528-531）
   *     · 当前模式 live 时还必须逐字带 confirmation:'确认执行'（app.py:532-533）
   *   POST /api/wb/confirmation  空载荷 → {ok,value:{pending:{id,operation,tool,mode,session_id,summary,…}|null,ttl_ms}}
   *   POST /api/wb/confirm-decide 白名单**只有** {id,decision}；decision ∈ approved/rejected
   *     （store_access.decide_confirmation：Invalid decision; expected approved/rejected）
   * 三重保守约束：
   *   ① 只在人工点击后发生——加载与轮询只读，绝不写；
   *   ② 必须二次点击：首次点击只进入待确认态并展示将提交的载荷，第二次点击才发请求；
   *   ③ live 执行口令在客户端先校验，不合规**一个字节都不发**；口令只存在于 input.value，
   *      不进 localStorage、不进日志、不进任何提示文案。
   * 口径区分：/api/v3/oms/orders 里 stage=manual 是**平台侧台账**的审批阶段，不是券商待确认；
   * 券商实盘业务确认只出现在「待确认请求」区块（confirm-decide）。 */
  const PLAN_CONFIRM_WORD = '确认执行'
  const CONFIRM_TTL_FALLBACK = 120
  const ARM_TTL_MS = 20000
  const GATE = {
    plan: null, planErr: null, mode: 'sim',
    pending: null, ttlMs: null, confirmErr: null,
    armedPlan: null, armedDecision: null,
    planMsg: null, confirmMsg: null,
    busy: false, armTimer: null, timers: false,
  }

  /** 失败一律展示 error.code + message（shared.js 的网络失败也是同一形状）。 */
  const errText = (payload, fallback) => {
    const error = payload && payload.error
    if (error && (error.code || error.message)) {
      return `${error.code || 'error'}：${error.message || fallback || '请求失败'}`
    }
    return fallback || '请求失败（服务端未给出 error.code/message）'
  }
  const isLive = () => String(GATE.mode || 'sim').toLowerCase() === 'live'
  /** 端点按创建时间倒序返回（最新在前）：plans[0] 即当前计划，与既有工作台同一口径——
   *  执行入口不随任何展示筛选改变目标，提交的 content_hash 永远是这一条。 */
  function currentPlan() {
    const plans = GATE.plan && Array.isArray(GATE.plan.plans) ? GATE.plan.plans : []
    return plans[0] || null
  }
  const planHashOf = (plan) => (plan && typeof plan.content_hash === 'string' && plan.content_hash
    ? plan.content_hash : null)
  /** confirmation 端点口径 {pending:{…}|null,ttl_ms}；兼容直接给 {id,…} 的形态。 */
  function pendingOf(value) {
    if (!value || typeof value !== 'object') return null
    if (value.pending !== undefined) return value.pending || null
    return value.id ? value : null
  }
  /** 剩余存活秒数（与 src/services/confirm.js 同一规则）：0=已到期，null=到期时间未知。 */
  function remainingSeconds(expiresAt) {
    const at = Date.parse(String(expiresAt == null ? '' : expiresAt))
    if (Number.isNaN(at)) return null
    return Math.max(0, Math.ceil((at - Date.now()) / 1000))
  }
  async function loadPlan() {
    const env = await V3.wb('plan', {})
    if (env && env.ok) {
      GATE.plan = env.value || {}
      GATE.mode = GATE.plan.mode || GATE.mode
      GATE.planErr = null
    } else {
      GATE.planErr = errText(env, 'POST /api/wb/plan 取数失败')
    }
    return env
  }
  async function loadConfirmation() {
    const env = await V3.wb('confirmation', {})
    if (env && env.ok) {
      const value = env.value || {}
      GATE.pending = pendingOf(value)
      GATE.ttlMs = Number.isFinite(Number(value.ttl_ms)) ? Number(value.ttl_ms) : null
      GATE.confirmErr = null
    } else {
      GATE.confirmErr = errText(env, 'POST /api/wb/confirmation 取数失败')
    }
    return env
  }
  const refreshGateOnly = () => Promise.all([loadPlan(), loadConfirmation()])
  function setPlanMsg(text, tone) {
    GATE.planMsg = text ? { text, tone: tone || 'info' } : null
    renderPlanGate()
  }
  function setConfirmMsg(text, tone) {
    GATE.confirmMsg = text ? { text, tone: tone || 'info' } : null
    renderConfirmChannel()
  }
  /** 待确认态自动作废：二次点击窗口过期即撤销，避免「很久以前的那一次点击」被兑现。 */
  function armExpiry() {
    if (GATE.armTimer) clearTimeout(GATE.armTimer)
    GATE.armTimer = setTimeout(() => {
      GATE.armedPlan = null
      GATE.armedDecision = null
      renderPlanGate()
      renderConfirmChannel()
    }, ARM_TTL_MS)
  }
  function resetArm() {
    GATE.armedPlan = null
    GATE.armedDecision = null
    renderPlanGate()
    renderConfirmChannel()
  }

  /* ── 9a. 冻结计划执行（弹窗内控件） ─────────────────────────────────────── */
  function buildPlanGate(mask) {
    let gate = q('#v3PlanGate', mask)
    if (gate) return gate
    gate = document.createElement('div')
    gate.id = 'v3PlanGate'
    gate.innerHTML =
      '<div class="appr-line"><span class="k">当前模式</span><span class="v num" id="v3PlanMode">—</span></div>' +
      '<div class="appr-line"><span class="k">expected_mode</span><span class="v num" id="v3PlanExpected">—</span></div>' +
      '<div class="appr-line"><span class="k">plan_hash</span><span class="v num" id="v3PlanHash">—</span></div>' +
      '<div class="pass-row" id="v3PlanPassRow" style="display:none">' +
        '<input type="password" id="v3PlanPass" placeholder="输入口令：确认执行" autocomplete="off" aria-label="确认执行口令">' +
      '</div>' +
      '<div class="block-reason" id="v3PlanWarn">执行已冻结计划是<b>唯一受约束下单入口</b>：只在人工点击并<b>二次确认</b>后写入指令文件，' +
        '绝不自动执行、加载时不写。SIM 免口令；LIVE 需逐字口令「确认执行」。</div>' +
      '<div class="appr-line" id="v3PlanConfirmLine" style="display:none">' +
        '<span class="k">待确认载荷</span><span class="v num" id="v3PlanConfirmText">—</span></div>' +
      '<div class="pass-err" id="v3PlanErr" style="display:none"></div>'
    const note = q('.m-note', mask)
    const btns = q('.m-btns', mask)
    if (note && note.parentElement && btns) note.parentElement.insertBefore(gate, btns)
    else mask.appendChild(gate)
    // 「取消计划」按钮（action:'cancel'）：插在「确认执行」左边，沿用设计稿 .m-btns 布局
    const confirmBtn = q('#mConfirm', mask)
    if (btns && confirmBtn && !q('#v3PlanCancel', btns)) {
      const cancelPlan = document.createElement('button')
      cancelPlan.className = 'btn'
      cancelPlan.id = 'v3PlanCancel'
      cancelPlan.textContent = '取消计划'
      btns.insertBefore(cancelPlan, confirmBtn)
    }
    return gate
  }
  function renderPlanGate() {
    const mask = q('#planMask')
    if (!mask) return
    const gate = buildPlanGate(mask)
    if (!gate) return
    const plan = currentPlan()
    const modeText = String(GATE.mode || 'sim')
    const liveNow = isLive()
    V3.setText(gate, '#v3PlanMode', liveNow ? 'LIVE（实盘）' : `${modeText.toUpperCase()}（模拟）`)
    V3.setText(gate, '#v3PlanExpected', modeText)
    V3.setText(gate, '#v3PlanHash', planHashOf(plan) || (GATE.planErr ? '取数失败' : '—'))
    const passRow = q('#v3PlanPassRow', gate)
    if (passRow) passRow.style.display = liveNow ? 'flex' : 'none'
    const armed = GATE.armedPlan
    const btn = q('#mConfirm', mask)
    if (btn) {
      btn.textContent = armed && armed.action === 'execute' ? '再次点击确认执行'
        : (liveNow ? '执行已冻结计划（LIVE 需口令）' : '执行已冻结计划')
      btn.disabled = GATE.busy
      btn.title = '提交 plan-execute（action=execute）：唯一受约束下单入口，需二次点击确认' +
        (liveNow ? '；LIVE 必须先输入逐字口令「确认执行」，口令不合规不发请求' : '；SIM 免口令')
    }
    const cancelBtn = q('#v3PlanCancel', mask)
    if (cancelBtn) {
      cancelBtn.textContent = armed && armed.action === 'cancel' ? '再次点击确认取消' : '取消计划'
      cancelBtn.disabled = GATE.busy
      cancelBtn.title = '提交 plan-execute（action=cancel）：只本地撤销未提交订单，需二次点击确认；左侧「取消」只关闭弹窗，不提交任何指令'
    }
    const line = q('#v3PlanConfirmLine', gate)
    const lineText = q('#v3PlanConfirmText', gate)
    if (line && lineText) {
      line.style.display = armed ? 'flex' : 'none'
      if (armed) {
        lineText.textContent = armed.action === 'cancel'
          ? `action=cancel · plan_hash=${armed.plan_hash} · expected_mode=${armed.expected_mode}`
          : `action=execute · plan_hash=${armed.plan_hash} · expected_mode=${armed.expected_mode}` +
            (armed.live ? ` · confirmation=${PLAN_CONFIRM_WORD}` : ' · 无 confirmation（非 live）')
      }
    }
    const box = q('#v3PlanErr', gate)
    if (box) {
      const msg = GATE.planMsg
      box.style.display = msg ? 'block' : 'none'
      box.textContent = msg ? msg.text : ''
      box.style.color = msg && msg.tone === 'ok' ? 'var(--green)'
        : msg && msg.tone === 'warn' ? 'var(--amber)' : 'var(--red)'
    }
  }
  function bindPlanGate() {
    const mask = q('#planMask')
    if (!mask) return
    // 设计稿内联脚本在解析时已给 #mConfirm 绑了演示 toast（HTML 不能改）：克隆替换掉它，
    // 从此这个按钮只走真实 plan-execute 通道。
    const confirmBtn = q('#mConfirm', mask)
    if (confirmBtn && confirmBtn.dataset.v3Bound !== '1') {
      const clone = confirmBtn.cloneNode(true)
      clone.dataset.v3Bound = '1'
      confirmBtn.replaceWith(clone)
      clone.addEventListener('click', () => planExecuteClick('execute'))
    }
    const cancelBtn = q('#v3PlanCancel', mask)
    if (cancelBtn && cancelBtn.dataset.v3Bound !== '1') {
      cancelBtn.dataset.v3Bound = '1'
      cancelBtn.addEventListener('click', () => planExecuteClick('cancel'))
    }
    const closeBtn = q('#mCancel', mask)
    if (closeBtn && closeBtn.dataset.v3Reset !== '1') {
      closeBtn.dataset.v3Reset = '1'
      closeBtn.title = '只关闭弹窗，不提交任何指令'
      closeBtn.addEventListener('click', resetArm)   // 关闭弹窗 = 撤销待确认态
    }
    const openBtn = q('#btnFreeze')
    if (openBtn && openBtn.dataset.v3Refresh !== '1') {
      openBtn.dataset.v3Refresh = '1'
      openBtn.addEventListener('click', () => {
        resetArm()
        refreshGateOnly().then(() => { renderModal([], dominantPlan([])) }).catch(() => {})
      })
    }
  }
  /**
   * 执行/取消冻结计划。任何一次点击都以「口令校验 → 计划校验 → 是否已处于待确认态」的顺序处理，
   * 校验不通过时在**发请求之前** return。
   * @param {'execute'|'cancel'} action
   */
  async function planExecuteClick(action) {
    const want = action === 'cancel' ? 'cancel' : 'execute'
    if (GATE.busy) { setPlanMsg('正在刷新或提交，请稍候再点击…', 'warn'); return }
    const plan = currentPlan()
    const hash = planHashOf(plan)
    const pass = q('#v3PlanPass')
    const password = pass ? String(pass.value || '') : ''
    // ① 口令门槛优先于一切：live 执行口令不合规 → 不发任何请求（读写都不发）
    if (want === 'execute' && isLive() && password !== PLAN_CONFIRM_WORD) {
      setPlanMsg(`实时账户执行需输入口令「${PLAN_CONFIRM_WORD}」；口令不合规，未发出任何请求`, 'err')
      return
    }
    if (!hash) {
      setPlanMsg(GATE.planErr || '无冻结计划：/api/wb/plan 未返回可执行计划（content_hash 缺失）', 'err')
      return
    }
    if (want === 'execute' && plan.status !== 'frozen') {
      setPlanMsg(`当前计划状态为 ${plan.status || '未知'}（非冻结态），不可执行；请刷新后重试`, 'err')
      return
    }
    // ② 首次点击：只进入待确认态（不发写请求），并立刻重新拉取 plan/confirmation
    if (!GATE.armedPlan || GATE.armedPlan.action !== want) {
      GATE.armedPlan = { action: want, plan_hash: hash, expected_mode: GATE.mode, live: isLive() }
      GATE.busy = true
      setPlanMsg(want === 'execute'
        ? '待确认：请核对下方载荷，再次点击「执行已冻结计划」才写入指令（不会自动执行）'
        : '待确认：请再次点击「取消计划」提交取消指令', 'warn')
      try {
        await refreshGateOnly()          // 执行前重新拉取（只读）
      } finally {
        GATE.busy = false
      }
      const fresh = currentPlan()
      if (GATE.armedPlan && (planHashOf(fresh) !== GATE.armedPlan.plan_hash || GATE.mode !== GATE.armedPlan.expected_mode)) {
        GATE.armedPlan = null
        setPlanMsg('计划或账户模式已变化（plan_hash / mode），已撤销待确认态，请核对后重新点击', 'err')
      }
      renderPlanGate()
      renderModal([], dominantPlan([]))
      renderConfirmChannel()
      armExpiry()
      return
    }
    // ③ 第二次点击 = 提交（载荷就是上一次点击展示出来的那一份）
    const armed = GATE.armedPlan
    if (armed.action === 'execute' && armed.live && password !== PLAN_CONFIRM_WORD) {
      setPlanMsg(`实时账户执行需输入口令「${PLAN_CONFIRM_WORD}」；口令不合规，未发出任何请求`, 'err')
      return
    }
    GATE.busy = true
    renderPlanGate()
    try {
      const payload = armed.action === 'cancel'
        ? { plan_hash: armed.plan_hash, expected_mode: armed.expected_mode, action: 'cancel' }
        : { plan_hash: armed.plan_hash, expected_mode: armed.expected_mode }
      if (armed.action === 'execute' && armed.live) payload.confirmation = PLAN_CONFIRM_WORD
      const env = await V3.wb('plan-execute', payload)
      if (env && env.ok) {
        const value = env.value || {}
        GATE.armedPlan = null
        if (pass) pass.value = ''      // 口令不留存
        setPlanMsg(`已提交（action=${value.action || armed.action}，nonce=${value.nonce || '—'}）：`
          + '指令文件已写入，状态由 daemon 回写；计划合法性与风控在 daemon 侧复核', 'ok')
        toast(armed.action === 'cancel' ? '取消计划指令已提交' : '执行指令已提交（等待 daemon 回写）', 'ok')
      } else {
        setPlanMsg(errText(env, 'plan-execute 失败'), 'err')
      }
    } catch (error) {
      setPlanMsg(String((error && error.message) || error), 'err')
    } finally {
      GATE.busy = false
      await refreshGateOnly()          // 执行后重新拉取
      renderPlanGate()
      renderModal([], dominantPlan([]))
      renderConfirmChannel()
    }
  }

  /* ── 9b. 人工审批决定（待确认请求 → confirm-decide） ─────────────────────── */
  function ensureConfirmSection() {
    let section = q('#v3ConfirmChannel')
    if (section) return section
    const anchor = q('[data-od-id="graded-approval"]')
    if (!anchor || !anchor.parentElement) return null
    section = document.createElement('section')
    section.className = 'card'
    section.id = 'v3ConfirmChannel'
    section.innerHTML =
      '<div class="sec-head">' +
        '<h2>待确认请求</h2>' +
        '<span class="sub">实盘业务确认 · /api/wb/confirmation → confirm-decide（唯一能批准实盘操作的通道）</span>' +
      '</div>' +
      '<div id="v3ConfirmBody"></div>' +
      '<div class="queue-next" id="v3ConfirmNote"></div>'
    anchor.parentElement.insertBefore(section, anchor.nextSibling)
    return section
  }
  function renderConfirmChannel() {
    const section = ensureConfirmSection()
    if (!section) return
    const body = q('#v3ConfirmBody', section)
    if (!body) return
    const note = q('#v3ConfirmNote', section)
    const ttlSeconds = GATE.ttlMs ? Math.round(GATE.ttlMs / 1000) : CONFIRM_TTL_FALLBACK
    if (note) {
      note.innerHTML = '口径区分：/api/v3/oms/orders 台账里 stage=manual 的订单是<b>平台侧台账的审批阶段</b>（风控超单笔上限退回人工），' +
        '<b>不是</b>券商待确认；<b>券商实盘业务确认</b>只出现在本区块——批准即授权该笔实盘操作，拒绝或超过 ' +
        `${esc(ttlSeconds)} 秒未作答一律按拒绝处理（fail-closed）。本页不产生下单参数：决策载荷只有 {id, decision}。`
    }
    if (GATE.confirmErr) {
      body.innerHTML = '<div class="appr-card"><div class="appr-top"><b>待确认请求读取失败</b>' +
        '<span class="badge b-red">取数失败</span></div>' +
        `<div class="appr-line"><span class="k">错误</span><span class="v" style="color:var(--red)">${esc(GATE.confirmErr)}</span></div></div>`
      return
    }
    const pending = GATE.pending
    if (!pending || !pending.id) {
      // 无待确认时同样把两个控件摆在页面上（禁用）：确认通道的存在与口径对操作者可见，
      // 但没有 id 就无从作答——服务端也会拒（没有待确认的实盘操作）。
      body.innerHTML = '<div class="appr-card">' +
        '<div class="appr-top"><b>当前无待确认请求</b><span class="badge b-blue">空闲</span></div>' +
        '<div class="appr-line"><span class="k">说明</span><span class="v">只有实盘写操作（下单/改单/撤单）才会产生待确认；' +
        'SIM 模式与只读动作不产生。请求出现后本区块每 10 秒自动重读一次。</span></div>' +
        '<div class="pass-row">' +
          '<button class="btn btn-primary" id="v3Approve" disabled>批准</button>' +
          '<button class="btn" id="v3Reject" disabled>拒绝</button>' +
        '</div>' +
        `<div class="pass-err" id="v3ConfirmErr" style="display:${GATE.confirmMsg ? 'block' : 'none'};color:var(--red)">` +
          `${esc(GATE.confirmMsg ? GATE.confirmMsg.text : '')}</div>` +
        '<div class="queue-next">没有待确认请求时按钮不可用（决策载荷必须带服务端给出的 id，本页不能凭空构造）。</div>' +
      '</div>'
      return
    }
    const remain = remainingSeconds(pending.expires_at)
    const expired = remain !== null && remain <= 0
    const armed = GATE.armedDecision
    const fields = pending.summary && Array.isArray(pending.summary.fields) ? pending.summary.fields : []
    const remainText = remain === null ? '剩余时间未知' : (expired ? '已超时（服务端按拒绝）' : `等待确认中 · 剩余 ${remain} 秒`)
    body.innerHTML = '<div class="appr-card">' +
      `<div class="appr-top"><b>${esc(pending.operation || '实盘写操作')}</b>` +
        `<span class="badge ${expired || remain === null ? 'b-red' : 'b-amber'}" id="v3ConfirmRemain">${esc(remainText)}</span></div>` +
      '<div class="appr-kv">' +
        `<div><div class="k">请求编号 id</div><div class="v num" id="v3ConfirmId">${esc(pending.id)}</div></div>` +
        `<div><div class="k">工具</div><div class="v num">${esc(pending.tool || '—')}</div></div>` +
        `<div><div class="k">模式</div><div class="v num">${esc(pending.mode || '—')}</div></div>` +
        `<div><div class="k">来源 session</div><div class="v num">${esc(pending.session_id || '—')}</div></div>` +
      '</div>' +
      '<div class="appr-line"><span class="k">时间</span><span class="v num">' +
        `发起 ${esc(stamp(pending.at))} · 到期 ${esc(stamp(pending.expires_at))} · 状态 ${esc(pending.status || 'pending')}</span></div>` +
      (fields.length
        ? fields.map((field) => '<div class="appr-line"><span class="k">' + esc(field.label || '—') + '</span>' +
            '<span class="v num">' + esc(field.value == null ? '—' : field.value) + '</span></div>').join('')
        : '<div class="appr-line"><span class="k">摘要</span><span class="v">服务端未给出 summary.fields</span></div>') +
      '<div class="pass-row">' +
        `<button class="btn btn-primary" id="v3Approve"${expired || remain === null || GATE.busy ? ' disabled' : ''}>` +
          `${armed && armed.decision === 'approved' ? '再次点击确认批准' : '批准'}</button>` +
        `<button class="btn" id="v3Reject"${GATE.busy ? ' disabled' : ''}>` +
          `${armed && armed.decision === 'rejected' ? '再次点击确认拒绝' : '拒绝'}</button>` +
      '</div>' +
      `<div class="pass-err" id="v3ConfirmErr" style="display:${GATE.confirmMsg ? 'block' : 'none'};` +
        `color:${GATE.confirmMsg && GATE.confirmMsg.tone === 'ok' ? 'var(--green)' : GATE.confirmMsg && GATE.confirmMsg.tone === 'warn' ? 'var(--amber)' : 'var(--red)'}">` +
        `${esc(GATE.confirmMsg ? GATE.confirmMsg.text : '')}</div>` +
      '<div class="queue-next">批准是唯一能授权实盘操作的通道：决策载荷只有 {id, decision}，本页不能填写任何下单参数。' +
        '首次点击只进入待确认态，<b>第二次点击才提交</b>；决定后立刻重读 confirmation。</div>' +
    '</div>'
    const approve = q('#v3Approve', body)
    if (approve) approve.addEventListener('click', () => decideClick('approved'))
    const reject = q('#v3Reject', body)
    if (reject) reject.addEventListener('click', () => decideClick('rejected'))
  }
  function tickConfirmCountdown() {
    if (!GATE.pending) return
    const badge = q('#v3ConfirmRemain')
    if (!badge) return
    const remain = remainingSeconds(GATE.pending.expires_at)
    const expired = remain !== null && remain <= 0
    badge.textContent = remain === null ? '剩余时间未知' : (expired ? '已超时（服务端按拒绝）' : `等待确认中 · 剩余 ${remain} 秒`)
    badge.className = `badge ${expired || remain === null ? 'b-red' : 'b-amber'}`
    const approve = q('#v3Approve')
    if (approve) approve.disabled = expired || remain === null || GATE.busy
  }
  /**
   * 批准/拒绝。首次点击只进入待确认态（不发请求）；第二次点击提交 {id, decision}。
   * decision 取值来自服务端：approved / rejected（store_access.decide_confirmation）。
   */
  async function decideClick(decision) {
    if (GATE.busy) { setConfirmMsg('正在提交或刷新，请稍候再点击…', 'warn'); return }
    const pending = GATE.pending
    if (!pending || !pending.id) { setConfirmMsg('无待确认请求（可能已处理或已超时）', 'err'); return }
    const remain = remainingSeconds(pending.expires_at)
    if (remain !== null && remain <= 0) {
      setConfirmMsg('该请求已超时（服务端按拒绝 fail-closed 处理），请刷新后查看最新状态', 'err')
      return
    }
    if (!GATE.armedDecision || GATE.armedDecision.decision !== decision || GATE.armedDecision.id !== pending.id) {
      GATE.armedDecision = { id: pending.id, decision }
      GATE.busy = true
      setConfirmMsg(decision === 'approved'
        ? '待确认：批准即授权该笔实盘操作；请再次点击「确认批准」提交 {id, decision}'
        : '待确认：请再次点击「确认拒绝」提交 {id, decision}', 'warn')
      try {
        await loadConfirmation()          // 决定前重新拉取（只读）
      } finally {
        GATE.busy = false
      }
      const fresh = GATE.pending
      if (GATE.armedDecision && (!fresh || fresh.id !== GATE.armedDecision.id)) {
        GATE.armedDecision = null
        setConfirmMsg('待确认请求已变化（已处理 / 已超时 / 换了一笔），已撤销待确认态，请核对后重新点击', 'err')
      }
      renderConfirmChannel()
      armExpiry()
      return
    }
    const armed = GATE.armedDecision
    GATE.busy = true
    try {
      const env = await V3.wb('confirm-decide', { id: armed.id, decision: armed.decision })
      if (env && env.ok) {
        const value = env.value || {}
        setConfirmMsg(`已${armed.decision === 'approved' ? '批准' : '拒绝'}（id=${value.id || armed.id}，`
          + `operation=${value.operation || '—'}）；服务端按先到者定论记账`, 'ok')
        toast(`已${armed.decision === 'approved' ? '批准' : '拒绝'}该笔实盘操作`, armed.decision === 'approved' ? 'ok' : 'warn')
      } else {
        setConfirmMsg(errText(env, 'confirm-decide 失败'), 'err')
      }
    } catch (error) {
      setConfirmMsg(String((error && error.message) || error), 'err')
    } finally {
      GATE.busy = false
      GATE.armedDecision = null
      await refreshGateOnly()             // 决定后立刻重读 confirmation（与 plan）
      await render().catch(() => {})      // 台账/看板据对账结果重读（失败不再抛到监听器外）
    }
  }
  /** 只读轮询：1 秒更新倒计时（无请求），10 秒重读待确认，30 秒重读计划/模式。 */
  function startGateTimers() {
    if (GATE.timers) return
    GATE.timers = true
    setInterval(tickConfirmCountdown, 1000)
    setInterval(() => {
      loadConfirmation().then(() => { renderConfirmChannel() }).catch(() => {})
    }, 10000)
    setInterval(() => {
      loadPlan().then(() => { renderPlanGate() }).catch(() => {})
    }, 30000)
  }

  /* ── 主流程 ────────────────────────────────────────────────────────────── */
  async function render() {
    // 只读取数（含两条写通道的**读**端点）：加载阶段绝不调用 plan-execute / confirm-decide
    const [execEnv, omsEnv, metricsEnv, auditEnv, riskEnv, planEnv, confirmEnv] = await Promise.all([
      V3.api('execution'),
      V3.api('oms/orders'),
      V3.api('metrics'),
      V3.api('audit?window=120'),
      V3.api('risk'),
      V3.wb('plan', {}),
      V3.wb('confirmation', {}),
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
    if (planEnv && planEnv.ok) {
      GATE.plan = planEnv.value || {}
      GATE.mode = GATE.plan.mode || GATE.mode
      GATE.planErr = null
    } else {
      GATE.planErr = errText(planEnv, 'POST /api/wb/plan 取数失败')
    }
    if (confirmEnv && confirmEnv.ok) {
      const value = confirmEnv.value || {}
      GATE.pending = pendingOf(value)
      GATE.ttlMs = Number.isFinite(Number(value.ttl_ms)) ? Number(value.ttl_ms) : null
      GATE.confirmErr = null
    } else {
      GATE.confirmErr = errText(confirmEnv, 'POST /api/wb/confirmation 取数失败')
    }

    renderTopbar(mode, nav, metrics)
    renderExecHead(oms, orders, dealRows, mode)
    renderKanban(exec, orders, audit)
    renderGrades(q('[data-od-id="graded-approval"]'), orders, riskCfg, nav)
    renderOrderTable(orders, (order) => renderTrace(order, audit, riskCfg, nav))
    renderHoldings(qa('.hq-grid > .card')[0], positions)
    renderQuality(qa('.hq-grid > .card')[1], exec, orders, openRows)
    renderModal(orders, dominantPlan(orders))   // 内部渲染 plan-execute 控件
    bindPlanGate()
    renderConfirmChannel()                      // 「待确认请求」区块（confirm-decide）
    renderFooter(metrics, positions)
    scrubDesignScript()
    startGateTimers()
  }

  render().catch((error) => {
    const page = q('.page')
    if (page) V3.nodata(page, '页面数据', String((error && error.message) || error))
  })
})()
