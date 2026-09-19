// V3「网关与调度」页数据绑定：设计稿 gateway.html 的 HTML/CSS 一字不动，
// 这里只把 /api/v3/* 的真实值写进对应节点，并把本服务确实没有的数据源显式标注。
//
// 数据诚实性（与后端 v3_ops.py 的契约一致，逐块说明）：
//   * MCP Bridge → GET /api/v3/gateway（channels.mcp）+ GET /api/v3/metrics（mcp 计数、toolTotal）
//   * SDK JSON-RPC / Headless CLI → channels.sdk / channels.headless 恒为 unavailable + reason，
//     本服务没有这两个通道，卡片整块标「无数据源」，一个占位数字都不留
//   * 调度器 → scheduler.recent 取 schedule 工具的真实作业历史（jobs），heartbeat 取 daemon 心跳；
//     scheduler.rules 恒为空数组（规则定义在 install/*.timer，不在工具面）→ 定时触发列如实标注
//   * 熔断保护 → channels.headless.today 恒为零计数、breaker 为 null：并发/超时/token 预算
//     三个阈值不在工具面 → 标「无数据源」，不猜数值
//   * Profile 与 Bundle → 工具面不返回 bundle 版本/条目 → 整块标「无数据源」
/* global window, document */
;(function () {
  const V3 = window.V3
  const { esc, num, stamp, dash, hhmmss, nodata, replaceWith } = V3

  const REASON = {
    sdk: '本服务未挂载 SDK JSON-RPC 通道（无 dsh SDK 会话）',
    headless: '本服务未挂载 Headless CLI 子通道（无 dsh --profile headless 子进程调度）',
  }

  function cardByTitle(title) {
    for (const card of document.querySelectorAll('.grid-3 .card')) {
      if ((card.querySelector('.ch-name')?.textContent ?? '').trim() === title) return card
    }
    return null
  }
  function statIn(card, label) {
    for (const item of card?.querySelectorAll('.ch-metrics .m') ?? []) {
      const own = (item.querySelector('.l')?.textContent ?? '').trim()
      if (own === label) return item
    }
    return null
  }
  /** 写通道卡片指标：保留设计稿的 .m/.v/.l 结构与配色类，只换数字与标签。 */
  function setStat(card, label, value, { unit = null, tone = null, relabel = null } = {}) {
    const item = statIn(card, label)
    if (!item) return false
    const v = item.querySelector('.v')
    const l = item.querySelector('.l')
    if (relabel && l) l.textContent = relabel
    if (!v) return false
    v.className = `v num${tone ? ' ' + tone : ''}`
    if (unit === null) v.textContent = value
    else v.innerHTML = `${esc(value)} <span class="unit">${esc(unit)}</span>`
    return true
  }
  function setChip(card, text, cls) {
    const chip = card?.querySelector('.st')
    if (!chip) return false
    chip.className = `st ${cls}`.trim()
    const dot = chip.querySelector('.dot')
    chip.textContent = text
    if (dot) chip.prepend(dot)
    return true
  }
  function setDot(card, cls) {
    const dot = card?.querySelector('.ch-top .dot')
    if (dot) dot.className = `dot ${cls}`
  }
  const pct = (part, whole) => (Number(whole) > 0 ? `${num((Number(part) / Number(whole)) * 100, 1)}%` : '—')
  const n = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0)

  // ── 顶栏 ───────────────────────────────────────────────────────────────────
  function renderTop(overview, gateway) {
    const env = document.querySelector('.topbar .env')
    if (env) env.textContent = String(overview.mode ?? '—').toUpperCase()
    const kvs = document.querySelectorAll('.topmid .t-item')
    const equity = overview.equity ?? {}
    const points = Array.isArray(equity.points) ? equity.points : []
    const last = points.at(-1) ?? null
    const prev = points.at(-2) ?? null
    const daily = last && prev && n(prev.equity) ? (n(last.equity) / n(prev.equity) - 1) * 100 : null
    if (kvs[0]) kvs[0].innerHTML = `权益<b class="num">${esc(V3.money(equity.current))}</b>`
    if (kvs[1]) {
      const text = daily === null ? '—' : V3.signed(daily, 2, '%')
      const cls = daily === null ? '' : daily >= 0 ? ' up' : ' down'
      kvs[1].innerHTML = `日内<b class="num${cls}">${esc(text)}</b>`
    }
    // head-status：刷新时间取接口 generated_at；三通道徽章按真实 status 着色
    const st = document.querySelector('.head-status .st')
    const statuses = ['mcp', 'sdk', 'headless'].map((key) => String(gateway.channels?.[key]?.status ?? ''))
    if (st) {
      const up = statuses.filter((value) => value === 'running').length
      st.className = `st ${up === 3 ? 'st-green' : up === 0 ? 'st-red' : 'st-amber'}`
      st.innerHTML = `<i class="dot ${up === 3 ? 'dot-green' : up === 0 ? 'dot-red' : 'dot-amber'}"></i>`
        + `${up} / 3 通道可用（MCP ${dash(statuses[0])} · SDK ${dash(statuses[1])} · Headless ${dash(statuses[2])}）`
    }
    const note = document.querySelector('.head-status .note')
    if (note) note.textContent = `刷新于 ${hhmmss(gateway.generated_at)} · 服务 /api/v3/gateway`
    const right = document.querySelector('.topright')
    if (right) right.textContent = stamp(gateway.generated_at)
  }

  // ── 三个通道卡片 ───────────────────────────────────────────────────────────
  function renderMcp(gateway, metrics) {
    const card = cardByTitle('MCP Bridge')
    if (!card) return
    setDot(card, 'dot-green')
    setChip(card, '运行中', 'st-green')
    for (const tag of card.querySelectorAll('.demo-tag')) tag.remove()
    const mcp = gateway.channels?.mcp ?? {}
    const calls = n(metrics.mcp?.calls)
    const errors = n(metrics.mcp?.errors)
    const kvs = card.querySelectorAll('.kv .v2')
    if (kvs[1]) kvs[1].textContent = mcp.protocol ?? '—'
    setStat(card, '已注册工具', metrics.toolTotal ?? '—', { unit: '个' })
    setStat(card, '今日调用', calls, { unit: '次' })
    setStat(card, '成功率', pct(calls - errors, calls), { tone: errors === 0 ? 'green' : 'red' })
    setStat(card, 'P95 延迟', n(metrics.mcp?.avgMs), { unit: 'ms', relabel: '平均延迟' })
    const cur = card.querySelector('.lc-cur')
    if (cur) cur.textContent = `当前 · ${dash(mcp.status)}`
    const foot = card.querySelector('.ch-foot')
    if (foot) foot.textContent = `MCP 服务端状态机只有 start/stop/update 三个动作，无状态查询端点；此处按 channels.mcp.status=${dash(mcp.status)} 标注`
    const buttons = card.querySelectorAll('.ch-actions .btn')
    if (buttons[0]) buttons[0].dataset.toast = `重载配置需在启动侧操作，本页不提供：MCP Bridge 当前注册 ${metrics.toolTotal ?? '—'} 个工具`
    if (buttons[1]) buttons[1].dataset.toast = `工具清单见「工具域治理」页（/v3/tools.html）：共 ${metrics.toolTotal ?? '—'} 个工具 · ${metrics.toolDomains ?? '—'} 个域`
  }

  function renderUnavailableChannel(title, channel, opts) {
    const card = cardByTitle(title)
    if (!card) return
    setDot(card, 'dot-red')
    setChip(card, '无数据源', 'st-amber')
    for (const tag of card.querySelectorAll('.demo-tag')) tag.remove()
    const kvs = card.querySelectorAll('.kv .v2')
    if (opts.protocol && kvs[1]) kvs[1].textContent = opts.protocol
    const block = card.querySelector('.ch-metrics') ?? card.querySelector('.ev-list')
    if (block) nodata(block, title, opts.reason ?? channel?.reason)
    for (const button of card.querySelectorAll('.ch-actions .btn')) {
      button.dataset.toast = `${title}：无数据源 · ${opts.reason ?? channel?.reason ?? '本服务未挂载该通道'}`
    }
    return card
  }

  function renderSdk(gateway) {
    const card = renderUnavailableChannel('SDK JSON-RPC', gateway.channels?.sdk, {
      protocol: gateway.channels?.sdk?.protocol,
      reason: gateway.channels?.sdk?.reason ?? REASON.sdk,
    })
    if (!card) return
    for (const kv of card.querySelectorAll('.kv')) {
      const label = (kv.querySelector('.k')?.textContent ?? '').trim()
      if (label !== '协议标识') continue
      kv.querySelector('.v2').innerHTML = '<span style="color:var(--faint)">无数据源（本服务未挂载 SDK 通道）</span>'
    }
    // 设计稿这里给了两条「session/prompt 完成 14:32」之类的事件：本服务没有 SDK 会话 → 一并标注
    const events = card.querySelector('.ev-list')
    if (events) nodata(events, 'SDK 会话事件', gateway.channels?.sdk?.reason ?? REASON.sdk)
  }

  function renderHeadless(gateway) {
    const today = gateway.headless?.today ?? {}
    const card = renderUnavailableChannel('Headless CLI', gateway.channels?.headless, {
      reason: gateway.channels?.headless?.reason ?? REASON.headless,
    })
    if (!card) return
    const chip = card.querySelector('.mono-chip')
    if (chip) {
      const command = gateway.channels?.headless?.command ?? 'dsh --profile headless "<task>"'
      chip.textContent = command
      chip.title = `未挂载的命令形态（本服务不拉起 headless 子进程）：${command}`
    }
    // 退出码分布：今日唤醒 0 次 → 三段都无计数，标「无常量数据」
    for (const track of card.querySelectorAll('.exit-track')) track.innerHTML = ''
    for (const count of card.querySelectorAll('.exit-n')) count.textContent = '0'
    const exits = card.querySelector('.exits')
    if (exits) {
      const title = exits.querySelector('.exits-title')
      if (title) title.textContent = `退出码分布 · 今日（唤醒 ${n(today.total)} 次）`
    }
  }

  // ── Headless 调用日志表 ────────────────────────────────────────────────────
  function renderCallLog(gateway) {
    const section = document.getElementById('headless-call-log')
    if (!section) return
    const head = section.querySelector('.head-right')
    if (head) head.innerHTML = `<span class="pill" style="color:var(--amber);border-style:dashed">无数据源 · 本服务未挂载 Headless 子进程</span>`
    const wrap = section.querySelector('.table-wrap')
    if (wrap) {
      nodata(wrap, 'Headless 调用日志',
        `${gateway.headless?.reason ?? REASON.headless}；channels.headless.status=${dash(gateway.channels?.headless?.status)}，无 exit_code / stdout / token 记录`)
    }
  }

  // ── 调度器 ─────────────────────────────────────────────────────────────────
  function schedulerRow(name, sub, { on = false } = {}) {
    const row = document.createElement('div')
    row.className = `sched-row${on ? '' : ' off'}`
    row.innerHTML = `<label class="switch"><input type="checkbox"${on ? ' checked' : ''} aria-label="${esc(name)}"><span></span></label>`
      + `<div class="sched-info"><div class="sched-name">${esc(name)}</div>`
      + `<div class="sched-sub">${sub}</div></div>`
    return row
  }

  function renderScheduler(gateway) {
    const section = document.querySelector('[data-od-id="scheduler"]')
    if (!section) return
    const scheduler = gateway.scheduler ?? {}
    const recent = Array.isArray(scheduler.recent) ? scheduler.recent : []
    const heartbeat = scheduler.heartbeat ?? {}
    const rules = Array.isArray(scheduler.rules) ? scheduler.rules : []
    const cols = section.querySelectorAll('.sched-col')
    const head = section.querySelector('.head-right')
    if (head) head.innerHTML = `<span class="pill">定时规则 ${rules.length} 条（工具面不提供）· 作业历史 ${recent.length} 条 · 心跳 ${dash(heartbeat.heartbeat)}</span>`

    // 第 1 列：daemon 心跳（真实）
    if (cols[0]) {
      const col = cols[0]
      col.querySelector('.sched-title').textContent = 'daemon 心跳 · 实时'
      col.querySelectorAll('.sched-row').forEach((row) => row.remove())
      col.appendChild(schedulerRow('heartbeat', `最近心跳 <b class="num">${esc(dash(heartbeat.heartbeat))}</b>`, { on: Boolean(heartbeat.heartbeat) }))
      col.appendChild(schedulerRow('最近作业', `最近一次 <b class="num">${esc(dash(heartbeat.last_job))}</b>`, { on: Boolean(heartbeat.last_job) }))
      const next = String(heartbeat.next ?? '').trim()
      col.appendChild(schedulerRow('下次触发',
        next ? (next.startsWith('见') ? esc(next) : `见 ${esc(next)}`) : '<span style="color:var(--faint)">无数据源（schedule 未返回触发计划）</span>',
        { on: Boolean(next) }))
    }
    // 第 2 列：真实作业历史（schedule.jobs）
    if (cols[1]) {
      const col = cols[1]
      col.querySelector('.sched-title').textContent = `作业历史 · 最近 ${Math.min(recent.length, 6)} 次`
      col.querySelectorAll('.sched-row').forEach((row) => row.remove())
      if (recent.length === 0) {
        col.appendChild(schedulerRow('作业历史', '<span style="color:var(--faint)">schedule 工具未返回 jobs</span>', { on: false }))
      }
      for (const job of recent.slice(0, 6)) {
        const name = String(job.job ?? '—')
        const market = name.split(':')[0]
        col.appendChild(schedulerRow(name.split(':').slice(1).join(':') || name,
          `市场 <b class="num">${esc(market)}</b> · 运行于 <b class="num">${esc(dash(job.ran))}</b>`, { on: true }))
      }
    }
    // 第 3 列：定时规则（工具面无规则表，如实标注）
    if (cols[2]) {
      const col = cols[2]
      col.querySelector('.sched-title').textContent = '定时规则'
      col.querySelectorAll('.sched-row').forEach((row) => row.remove())
      col.appendChild(schedulerRow('定时规则表', '<span style="color:var(--faint)">无数据源 · 规则定义在 install/*.timer 与配置里，不在工具面（scheduler.rules 恒为空数组）</span>', { on: false }))
      col.appendChild(schedulerRow('kill 开关', `kill <b class="num">${scheduler.kill ? 'true' : 'false'}</b> · halt <b class="num">${scheduler.halt ? 'true' : 'false'}</b> · critical <b class="num">${scheduler.critical ? 'true' : 'false'}</b>`, { on: !scheduler.kill && !scheduler.halt }))
    }
    section.title = scheduler.note ?? ''
  }

  // ── 熔断保护 ───────────────────────────────────────────────────────────────
  function renderBreaker(gateway) {
    const section = document.querySelector('[data-od-id="circuit-breaker"]')
    if (!section) return
    const head = section.querySelector('.head-right')
    const sched = gateway.scheduler ?? {}
    if (head) {
      const tripped = Boolean(sched.kill || sched.halt || sched.critical)
      head.innerHTML = `<span class="warn-pill"><i class="dot dot-amber" style="width:6px;height:6px"></i>`
        + `服务端 kill=${sched.kill ? 'true' : 'false'} · halt=${sched.halt ? 'true' : 'false'} · critical=${sched.critical ? 'true' : 'false'}</span>`
        + `<span class="pill" style="color:${tripped ? 'var(--red)' : 'var(--green)'};border-style:${tripped ? 'solid' : 'dashed'}">`
        + `${tripped ? '熔断已触发' : '未触发 · 保护待命'}</span>`
    }
    const items = section.querySelectorAll('.brk-item')
    const reasons = [
      '并发上限：Headless 子进程不自管并发位，工具面无该阈值',
      '单次超时：无 Headless 子进程调度，无超时配置来源',
      '单次 token 预算：本服务不消耗模型 token，无预算来源',
      '今日累计 token：本服务不消耗模型 token，无用量来源',
    ]
    items.forEach((item, index) => {
      const label = (item.querySelector('.l')?.textContent ?? '熔断参数').trim()
      replaceWith(item, `<div class="l" style="color:var(--amber)">无数据源</div>`
        + `<div class="v" style="font-size:13px;font-weight:500;line-height:1.6">${esc(label)}</div>`
        + `<div class="sub">${esc(reasons[index] ?? '工具面无该阈值')}</div>`)
    })
    const kill = section.querySelector('.kill')
    if (kill) {
      kill.innerHTML = '<span class="dot dot-amber"></span><span>强制 kill 记录：'
        + `<b class="num">无数据源</b> · ${esc(gateway.headless?.reason ?? REASON.headless)}（headless.killed=${n(gateway.headless?.today?.killed)}）</span>`
    }
  }

  // ── Profile 与 Bundle ──────────────────────────────────────────────────────
  function renderBundles() {
    const section = document.querySelector('[data-od-id="profile-bundle"]')
    if (!section) return
    const path = section.querySelector('.path')
    if (path) {
      path.textContent = '无数据源'
      path.title = '工具面不返回 $DSH_HOME/profiles 路径'
    }
    const rows = section.querySelectorAll('.bundle-row')
    if (rows[0]) {
      replaceWith(rows[0], '<div style="grid-column:1/-1;color:var(--faint);line-height:1.7">'
        + '无数据源 · 工具面不返回 bundle 的名称 / 版本 / 条目数 / 加载状态；'
        + '本页不再保留占位条目（可用 bundle 清单见 Harness 侧 cordis.yml）</div>')
    }
    rows.forEach((row, index) => { if (index > 0) row.remove() })
  }

  // ── 页脚 ───────────────────────────────────────────────────────────────────
  function renderFooter(gateway) {
    const foot = document.querySelector('footer.foot')
    if (!foot) return
    const spans = foot.querySelectorAll('span')
    if (spans[0]) spans[0].textContent = '数据来源：/api/v3/gateway（通道与调度）· /api/v3/metrics（调用计数）· 工具面 schedule 作业历史'
    if (spans[1]) spans[1].textContent = `数据截至 ${stamp(gateway.generated_at)} · MCP ${dash(gateway.channels?.mcp?.status)} · SDK/Headless 未挂载`
  }

  function wireSwitchHint() {
    // 调度开关不持久化：设计稿的开关是展示件，工具面没有可写规则表 → 提示改为如实说明
    for (const input of document.querySelectorAll('.switch input')) {
      input.addEventListener('change', () => {
        input.checked = !input.checked
        input.title = '调度规则由 install/*.timer 与配置定义，不在工具面：本页开关只读，不持久化'
      })
    }
  }

  async function render() {
    const [gateway, metrics, overview] = await Promise.all([
      V3.api('gateway'), V3.api('metrics'), V3.api('overview'),
    ])
    if (gateway.ok) {
      renderCallLog(gateway)
      renderScheduler(gateway)
      renderBreaker(gateway)
      renderFooter(gateway)
      renderSdk(gateway)
      renderHeadless(gateway)
      if (metrics.ok) renderMcp(gateway, metrics)
      if (overview.ok) renderTop(overview, gateway)
    } else {
      const main = document.querySelector('.page')
      if (main) nodata(main, '网关与调度', gateway.error?.message)
    }
    if (!metrics.ok) {
      const card = cardByTitle('MCP Bridge')
      const block = card?.querySelector('.ch-metrics')
      if (block) nodata(block, 'MCP 调用计数', metrics.error?.message)
    }
    renderBundles()
    wireSwitchHint()
    V3.demoSweep(document.body)
  }

  render().catch((error) => {
    const main = document.querySelector('.page')
    if (main) nodata(main, '网关与调度', String(error?.message ?? error))
  })
})()
