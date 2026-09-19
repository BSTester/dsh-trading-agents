// V3「接入与授权」页数据绑定：设计稿 settings.html 的 HTML/CSS 一字不动，
// 这里只把 /api/v3/* 的真实值写进对应节点，并把本服务确实没有的数据源显式标注。
//
// 安全纪律（硬约束，逐条对应接口契约）：
//   * 环境变量表只显示「是否注入 + 来源」，**绝不显示值**（后端 _env_status 也只返回这两个字段）
//   * Tushare token 只显示 present / source / updated_at / 掩码尾号（/api/v3/credentials），
//     页面提供输入框 + 保存 / 测试 / 清除（保存后立即清空，服务端与页面都不回显值）
//   * 富途 OpenAPI 凭据 + OAuth 2.1+PKCE 在「富途 OpenAPI / OpenD 授权」区块内就地完成：
//     POST /api/wb/openapi_config（空载荷=读状态、带载荷=保存）、/api/wb/openapi_test、
//     /api/wb/openapi_oauth（start/status/cancel，等待期每 2 秒轮询 status）
//   * 交易模式切换与自动流水线同样是页面上的人工动作：POST /api/wb/switch-mode
//     （sim→live 逐字口令「确认实盘」，客户端先校验、服务端独立复核）、
//     POST /api/wb/auto_pipeline（空载荷=读有效配置、带载荷=校验后原子写）
//   * 所有写动作只在人明确点击/输入后发送：加载时零写入、无自动提交
//   * 未挂载/未采集的数据源（OpenD Host/Port、账号掩码、密码掩码、有效期、轮换时间…）
//     一律标「无数据源」，不留设计稿占位串
/* global window, document, Blob, URL */
;(function () {
  const V3 = window.V3
  const { esc, money, signed, stamp, dash, num, nodata, replaceWith } = V3

  const MODE_LABEL = { sim: '模拟盘 SIM', live: '实盘 LIVE' }
  const PURPOSES = {
    DSH_HOME: 'Harness 工作目录（Headless 自动唤醒入口）',
    DEEPSEEK_API_KEY: 'DeepSeek 推理调用（BYOK）',
    QUANT_MCP_NODE: 'MCP Bridge 节点入口',
    QUANT_MCP_SERVER: '平台 MCP 服务器地址',
    QUANT_MCP_CWD: 'MCP 工具默认工作目录',
    QUANT_MCP_LOG: 'MCP Bridge 日志文件',
    FUTU_OPEND_HOST: 'OpenD 连接地址',
    FUTU_OPEND_PORT: 'OpenD 连接端口',
    TUSHARE_TOKEN: 'Tushare Pro 行情 / 财务数据',
  }
  const n = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0)
  const badge = (text, kind) => `<span class="badge badge-${kind}">${esc(text)}</span>`
  const noSource = (why) => '<span style="color:var(--faint)">无数据源'
    + (why ? `（${esc(why)}）` : '') + '</span>'

  let auditRows = []

  /** 模式切换弹窗的目标/当前模式：二次确认弹窗是设计稿共用的一个节点，状态在这里。
   *  modeCurrent 由每次 render 从 /api/v3/settings 刷新（作为 expected_mode 的乐观并发标记）。 */
  let modeTarget = 'live'
  let modeCurrent = '—'

  // ══ 人工写动作：共用契约 ═══════════════════════════════════════════════════
  // 三项能力（模式切换 / 富途 OpenAPI 凭据 + OAuth / 自动流水线）都走人工点击，
  // 页面加载**不写**、不自动提交；每个动作的失败都显示服务端信封的 code + message。
  /** sim→live 的逐字口令（与 store_access.switch_mode 的复核字符串逐字节一致）。 */
  const LIVE_CONFIRMATION = '确认实盘'
  /** 载荷白名单（与 server/settings_api.py 的 AUTO_PIPELINE_FIELDS 同形；openapi_config 的
   *  CONFIG_FIELDS 见 buildAppKeyForm——只挑白名单键，多带一个只读字段就会被服务端拒绝）。 */
  const AUTO_PIPELINE_KEYS = ['enabled', 'strategies', 'exec_at', 'exec_window_minutes', 'reconcile_at']
  const MARKETS = ['SH', 'HK', 'US']
  const MARKET_LABEL = { SH: 'A股 SH', HK: '港股 HK', US: '美股 US' }
  const ALGORITHMS = ['Ed25519', 'RSA-SHA256']
  const CHANNELS = [['openapi', 'OpenAPI（本页 AppKey / OAuth 凭据）'], ['mcp', 'MCP（OAuth 令牌通道）']]
  /** 内置策略镜像（core strategies.REGISTRY 的非规则项）；规则 id 由服务端在拒绝时列出。 */
  const BUILTIN_STRATEGIES = ['watchlist_rsi', 'momentum_value_top5', 'ma_cross', 'rsi']
  const EXEC_WINDOW_MAX_MINUTES = 240
  const POOL_KEY_DEFAULT = 'watchlist'
  /** 服务端信封 → 「code：message」；绝不含服务端未返回的字段（密钥不进这条路径）。 */
  const errText = (body) => `${body?.error?.code ?? 'error'}：${body?.error?.message ?? '服务端未给出原因'}`
  const HHMM_RE = /^([01]\d|2[0-3]):[0-5]\d$/

  function msgIn(node, text, tone) {
    if (!node) return
    node.textContent = text
    node.style.color = tone === 'bad' ? 'var(--red)' : tone === 'ok' ? 'var(--green)' : 'var(--faint)'
  }
  function el(tag, attrs, text) {
    const node = document.createElement(tag)
    for (const [key, value] of Object.entries(attrs ?? {})) {
      if (value === undefined || value === null) continue
      if (key === 'style') node.style.cssText = value
      else if (key === 'text') node.textContent = String(value)
      else node.setAttribute(key, String(value))
    }
    if (text !== undefined) node.textContent = String(text)
    return node
  }
  const input = (attrs) => el('input', { class: 'input', type: 'text', autocomplete: 'off', ...(attrs ?? {}) })
  function select(options, value, attrs) {
    const node = el('select', { class: 'input', ...(attrs ?? {}) })
    for (const [val, label] of options) {
      const option = el('option', { value: val }, label)
      if (String(val) === String(value ?? '')) option.selected = true
      node.appendChild(option)
    }
    return node
  }
  const btn = (label, attrs) => el('button', { class: 'btn', type: 'button', ...(attrs ?? {}) }, label)
  const opBtn = (label, attrs) => el('button', { class: 'op', type: 'button', ...(attrs ?? {}) }, label)
  const field = (label, control, { width = 220, hint } = {}) => {
    const wrap = el('div', { style: 'display:flex;flex-direction:column;gap:4px' })
    wrap.appendChild(el('span', { class: 'stat-label' }, label))
    if (control) {
      control.style.width = `${width}px`
      control.style.maxWidth = '100%'
      wrap.appendChild(control)
    }
    if (hint) wrap.appendChild(el('span', { class: 'stat-label', style: 'color:var(--faint)' }, hint))
    return wrap
  }
  /** 人工动作进行中禁用按钮（避免重复提交）；结果行由各动作自己写，不被这里清掉。 */
  async function withBusy(button, work) {
    if (button) button.disabled = true
    try {
      return await work()
    } finally {
      if (button) button.disabled = false
    }
  }

  // ── 顶栏 / 侧栏 ────────────────────────────────────────────────────────────
  function renderShell(overview, settings, metrics) {
    const mode = String(settings.trading_mode ?? overview.mode ?? '—')
    const envBadge = document.querySelector('.topbar .badge')
    if (envBadge) {
      const ok = mode === 'live'
      envBadge.className = `badge badge-${ok ? 'red' : 'green'}`
      envBadge.innerHTML = `<i class="dot dot-${ok ? 'red' : 'green'}"></i>${esc(String(mode).toUpperCase())}`
      envBadge.title = `当前交易模式：${MODE_LABEL[mode] ?? mode}`
    }
    const kvs = document.querySelectorAll('.tb-mid .tb-kv')
    const equity = overview.equity ?? {}
    const points = Array.isArray(equity.points) ? equity.points : []
    const last = points.at(-1) ?? null
    const prev = points.at(-2) ?? null
    const delta = last && prev ? n(last.equity) - n(prev.equity) : null
    const pct = last && prev && n(prev.equity) ? (n(last.equity) / n(prev.equity) - 1) * 100 : null
    if (kvs[0]) kvs[0].innerHTML = `权益<b class="num">${esc(money(equity.current))}</b>`
    if (kvs[1]) {
      kvs[1].innerHTML = delta === null
        ? `日内<b class="num">台账点位不足（${points.length} 个点位）</b>`
        : `日内<b class="num${delta >= 0 ? ' up' : ''}">${esc(signed(delta, 2))} · ${esc(signed(pct, 2, '%'))}</b>`
    }
    const pills = document.querySelectorAll('.tb-mid .pill.hb')
    const chans = [
      ['MCP', metrics.mcp?.avgMs, 'avg 延迟'],
      ['SDK', null, '未挂载'],
      ['Headless', null, '未挂载'],
    ]
    pills.forEach((pill, index) => {
      const [name, value, suffix] = chans[index] ?? []
      if (name === undefined) return
      const ok = value !== null
      pill.className = 'pill hb'
      pill.innerHTML = `<i class="dot dot-${ok ? 'green' : 'red'}"></i>${esc(name)} <b class="num">${ok ? `${n(value)}ms` : esc(suffix)}</b>`
      pill.title = ok ? `${name} 平均延迟 ${n(value)}ms（/api/v3/metrics）` : `${name}：本服务未挂载该通道`
    })
    const foot = document.querySelector('.side-foot')
    if (foot) foot.textContent = `v3 · /api/v3/metrics 工具 ${metrics.toolTotal ?? '—'} 个 · ${stamp(metrics.generated_at)}`
  }

  // ── 交易模式卡 ─────────────────────────────────────────────────────────────
  function renderMode(settings, overview, orders, metrics) {
    const section = document.querySelector('[data-od-id="trading-mode"]')
    if (!section) return
    const mode = String(settings.trading_mode ?? '—')
    modeCurrent = mode
    const isSim = mode === 'sim'
    const headBadge = section.querySelector('.card-head .badge')
    if (headBadge) {
      headBadge.className = `badge badge-${isSim ? 'green' : 'red'}`
      headBadge.innerHTML = `<i class="dot dot-${isSim ? 'green' : 'red'}"></i>当前生效：${esc(MODE_LABEL[mode] ?? mode)}`
    }
    const cols = section.querySelectorAll('.mode-col')
    // SIM 列
    if (cols[0]) {
      const badge0 = cols[0].querySelector('.badge')
      if (badge0) {
        badge0.className = `badge badge-${isSim ? 'green' : 'grey'}`
        badge0.innerHTML = `<i class="dot dot-${isSim ? 'green' : 'blue'}"></i>${isSim ? '当前生效' : '未生效'}`
      }
      const stats = cols[0].querySelectorAll('.stat')
      if (stats[0]) {
        stats[0].querySelector('.stat-label').textContent = '当前权益（模拟台账）'
        stats[0].querySelector('.stat-value').textContent = money(overview.equity?.current)
        stats[0].title = '来源 /api/v3/overview 的 equity.current（本地模拟台账，不代表券商资产）'
      }
      if (stats[1]) {
        stats[1].querySelector('.stat-label').textContent = 'OMS 台账订单'
        stats[1].querySelector('.stat-value').textContent = `${n(orders.stages?.manual)} 笔待人工`
        stats[1].title = '来源 /api/v3/oms/orders 的 stages.manual'
      }
    }
    // LIVE 列
    if (cols[1]) {
      const badge1 = cols[1].querySelector('.badge')
      if (badge1) {
        badge1.className = `badge badge-${isSim ? 'red' : 'green'}`
        badge1.innerHTML = `<i class="dot dot-${isSim ? 'red' : 'green'}"></i>${isSim ? '未启用' : '当前生效'}`
      }
      const desc = cols[1].querySelector('.mode-desc')
      if (desc) desc.textContent = '订单将经富途 OpenAPI 真实报出；启用前需通过右侧三项前置校验，且只能由人在工作台 Web 输入口令完成。'
    }
    // 前置校验：三项都是真实可判定的
    const checks = section.querySelectorAll('.check-list li .badge')
    const futuReady = Boolean(settings.futu?.channel)
    const riskReady = n(orders.stages?.manual) >= 0 && Number.isFinite(Number(orders.nav))
    const pwEl = document.getElementById('checkPw')
    const labels = section.querySelectorAll('.check-list li span:first-child')
    const specs = [
      { ok: futuReady, text: futuReady ? '通过' : '未通过', why: `futu.channel=${dash(settings.futu?.channel)}（/api/v3/settings）` },
      { ok: riskReady, text: riskReady ? '通过' : '未知', why: `风控分级：nav=${n(orders.nav)} · 台账订单 ${n(orders.stages?.manual)} 笔待人工` },
    ]
    specs.forEach((spec, index) => {
      const node = checks[index]
      if (!node) return
      node.className = `badge badge-${spec.ok ? 'green' : 'amber'}`
      node.textContent = spec.text
      node.title = spec.why
      if (labels[index]) labels[index].title = spec.why
    })
    if (checks[2]) checks[2].title = '口令校验由工作台 Web 的 switch_mode 闸门执行：V3.0 不另开口子'
    // 模式切换条：切换入口只有一个（工作台 Web），这里如实说明而不是伪造动作
    const barNote = section.querySelector('.bar-note')
    if (barNote) barNote.textContent = settings.mode_note ?? '模式切换只经工作台 Web 口令闸门，本页不提供切换通道。'
    const toLive = document.getElementById('toLive')
    if (toLive) {
      toLive.disabled = !isSim
      toLive.title = isSim
        ? '选择目标模式并输入逐字口令「确认实盘」→ 二次确认 → POST /api/wb/switch-mode'
        : '当前已是实盘'
    }
    const toSim = document.getElementById('toSim')
    if (toSim) {
      toSim.textContent = '切回模拟盘'
      toSim.disabled = isSim
      toSim.title = isSim
        ? '当前已是模拟盘'
        : 'LIVE→SIM 无需口令（仍需二次确认）→ POST /api/wb/switch-mode'
    }
    const confirmLive = document.getElementById('confirmLive')
    if (confirmLive) {
      confirmLive.title = isSim
        ? '确认后 POST /api/wb/switch-mode（服务端会独立复核口令）'
        : '确认后 POST /api/wb/switch-mode（LIVE→SIM 无需口令）'
    }
    const pwInput = document.getElementById('modePw')
    if (pwInput) {
      pwInput.title = '口令只用于本页客户端前置校验；sim→live 的逐字口令是「确认实盘」，口令不符时本页不发请求'
    }
  }

  // ── 交易模式切换（人工动作 · POST /api/wb/switch-mode）──────────────────────
  // 契约：载荷 {mode, expected_mode, confirmation}；sim→live 必须逐字口令「确认实盘」，
  // **客户端先校验**（口令不符连请求都不发），服务端仍会独立复核；LIVE→SIM 无需口令，
  // 但仍要人点二次确认。切换成功后重新取 /api/v3/settings 与 /api/v3/overview。
  function patchMode(settings, refresh) {
    const section = document.querySelector('[data-od-id="trading-mode"]')
    if (!section) return
    const current = String(settings.trading_mode ?? 'sim')
    const bar = section.querySelector('.mode-bar')
    const pwInput = document.getElementById('modePw')
    if (pwInput) {
      pwInput.placeholder = `输入口令：${LIVE_CONFIRMATION}（仅 sim→live 需要）`
      if (pwInput.dataset.v3Width !== '1') {
        pwInput.dataset.v3Width = '1'
        pwInput.style.width = '260px'   // 设计稿在本页 style 里给 #modePw 钉了 210px：口令提示更长，就地放宽
      }
    }
    const barNote = section.querySelector('.bar-note')
    if (barNote) {
      barNote.textContent = '切换模式 ≠ 授权下单：下单/执行只经「执行与审批」页的受约束入口。'
        + 'sim→live 需逐字口令「确认实盘」+ 二次确认；LIVE→SIM 无需口令。任何切换都写入审计日志。'
    }
    // 结果行 + 目标模式选择：设计稿 HTML 一字不动，控件用设计稿自身的类就地追加。
    let panel = section.querySelector('#v3ModePanel')
    if (!panel) {
      panel = el('div', { id: 'v3ModePanel', style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap;border-top:1px dashed var(--border);margin-top:10px;padding-top:10px;flex:1 0 100%' })
      panel.appendChild(el('span', { class: 'bar-label' }, '目标模式'))
      panel.appendChild(select([['live', '实盘 LIVE'], ['sim', '模拟盘 SIM']], current === 'sim' ? 'live' : 'sim', { id: 'v3ModeTarget' }))
      const live = btn('切换到实盘', { id: 'v3ModeGoLive', class: 'btn btn-danger' })
      const sim = btn('切回模拟盘', { id: 'v3ModeGoSim' })
      panel.appendChild(live)
      panel.appendChild(sim)
      panel.appendChild(el('span', { id: 'v3ModeMsg', class: 'btn-note' }, ''))
      if (bar) bar.appendChild(panel)
      else section.appendChild(panel)
    }
    const target = panel.querySelector('#v3ModeTarget')
    if (target && !target.dataset.v3Synced) {
      target.dataset.v3Synced = '1'
      target.value = current === 'sim' ? 'live' : 'sim'
    }
    const msg = panel.querySelector('#v3ModeMsg')
    const goLive = panel.querySelector('#v3ModeGoLive')
    const goSim = panel.querySelector('#v3ModeGoSim')
    if (goLive) goLive.disabled = current !== 'sim'
    if (goSim) goSim.disabled = current === 'sim'
    if (msg) msgIn(msg, `当前生效：${MODE_LABEL[current] ?? current}（expected_mode=${current}）· 口令逐字为「${LIVE_CONFIRMATION}」`, 'info')

    // 设计稿脚本给 #toLive / #toSim / #confirmLive 绑了纯演示 handler；这里在捕获阶段
    // 接管（第 4 个参数 true + stopImmediatePropagation），确保演示 handler 不再抢先弹 toast。
    const takeover = (button, handler) => {
      if (!button || button.dataset.v3Bound === '1') return
      button.dataset.v3Bound = '1'
      button.addEventListener('click', (event) => {
        event.preventDefault()
        event.stopImmediatePropagation()
        handler()
      }, true)
    }
    takeover(document.getElementById('toLive'), () => openModeModal('live', current, pwInput, msg))
    takeover(document.getElementById('toSim'), () => openModeModal('sim', current, pwInput, msg))
    takeover(goLive, () => openModeModal('live', current, pwInput, msg))
    takeover(goSim, () => openModeModal('sim', current, pwInput, msg))
    takeover(document.getElementById('confirmLive'), () => sendMode(refresh))
  }

  /** 客户端前置校验：sim→live 口令不符**不发请求**（服务端复核是第二道，不是第一道）。 */
  function openModeModal(target, current, pwInput, msg) {
    if (target === current) {
      msgIn(msg, `当前已处于 ${MODE_LABEL[current] ?? current}，无需切换`, 'info')
      return
    }
    if (target === 'live' && (pwInput?.value ?? '') !== LIVE_CONFIRMATION) {
      msgIn(msg, `口令校验未通过：请输入逐字「${LIVE_CONFIRMATION}」。本页未发送任何请求。`, 'bad')
      pwInput?.focus()
      return
    }
    msgIn(msg, target === 'live'
      ? '口令已通过客户端校验 → 请在弹窗二次确认（服务端仍会独立复核）'
      : 'LIVE→SIM 无需口令 → 请在弹窗二次确认', 'info')
    showModeModal(target)
  }

  function showModeModal(target) {
    const mask = document.getElementById('modalMask')
    if (!mask) return
    modeTarget = target
    const title = mask.querySelector('.modal-title')
    const staticText = mask.querySelector('.modal-text')
    if (staticText && staticText.dataset.v3Hidden !== '1') {
      staticText.dataset.v3Hidden = '1'
      staticText.style.display = 'none'
    }
    let dynamic = mask.querySelector('#v3ModalText')
    if (!dynamic) {
      dynamic = el('p', { class: 'modal-text', id: 'v3ModalText' })
      ;(staticText ?? title)?.insertAdjacentElement('afterend', dynamic)
    }
    const known = modeCurrent !== '—' ? modeCurrent : '当前模式'
    const toLive = target === 'live'
    if (title) title.lastChild.textContent = `二次确认 · 切换到${toLive ? '实盘 LIVE' : '模拟盘 SIM'}`
    if (dynamic) {
      dynamic.textContent = toLive
        ? `即将把交易模式由 ${known} 切换为 LIVE。切换后订单将经富途 OpenAPI 真实报出，本操作写入审计日志。切换模式不等于下单授权：LIVE 下每笔执行仍需在「执行与审批」页人工审批。`
        : `即将把交易模式由 ${known} 切换为 SIM。切换后订单只进入本地模拟账本，本操作写入审计日志；LIVE 下已报出的订单不受影响，需在执行页人工处理。`
    }
    const ackRow = mask.querySelector('.ack')
    const ack = document.getElementById('riskAck')
    const confirmBtn = document.getElementById('confirmLive')
    if (ackRow) ackRow.style.display = toLive ? '' : 'none'
    if (ack) ack.checked = !toLive
    if (confirmBtn) {
      confirmBtn.disabled = toLive
      confirmBtn.textContent = toLive ? '确认切换到实盘' : '确认切回模拟盘'
    }
    mask.classList.add('show')
  }

  function closeModeModal() {
    const mask = document.getElementById('modalMask')
    if (!mask) return
    mask.classList.remove('show')
    const ack = document.getElementById('riskAck')
    const confirmBtn = document.getElementById('confirmLive')
    if (ack) ack.checked = false
    if (confirmBtn) confirmBtn.disabled = true
  }

  /** 真正的人工写动作：只在人点「确认切换」后发一次请求。 */
  async function sendMode(refresh) {
    const target = modeTarget
    const expected = modeCurrent
    const mask = document.getElementById('modalMask')
    const confirmBtn = document.getElementById('confirmLive')
    closeModeModal()   // 先关弹窗：失败也会写进模式卡的结果行，不留半开弹窗
    const msg = document.querySelector('#v3ModePanel #v3ModeMsg')
    const payload = { mode: target, expected_mode: expected }
    if (target === 'live') payload.confirmation = LIVE_CONFIRMATION
    await withBusy(confirmBtn, async () => {
      msgIn(msg, `正在切换 ${expected} → ${target} …`, 'info')
      try {
        const body = await V3.wb('switch-mode', payload)
        if (!body?.ok) { msgIn(msg, `切换失败：${errText(body)}`, 'bad'); return }
        const value = body.value ?? {}
        msgIn(msg, `已切换：${value.previous_mode ?? expected} → ${value.mode ?? target} · 下单授权=${value.order_authorized === true ? '是' : '否（模式≠下单授权）'}`, 'ok')
        document.getElementById('modePw').value = ''
        if (mask) mask.classList.remove('show')
        modeCurrent = String(value.mode ?? target)
        await refresh()
      } catch (error) {
        msgIn(msg, `切换失败：${String(error?.message ?? error)}`, 'bad')
      }
    })
  }


  // ── 模式切换审计（真实审计链）─────────────────────────────────────────────
  function renderAudit(audit) {
    const section = document.querySelector('[data-od-id="mode-switch-audit"]')
    if (!section) return
    const entries = Array.isArray(audit.data?.entries) ? audit.data.entries : []
    const stats = audit.data?.stats ?? {}
    auditRows = entries
    const sub = section.querySelector('.title-sub')
    const n3 = entries.length
    if (sub) {
      sub.textContent = n3
        ? `真实审计链 · 最近 ${n3} 条（信号 / 订单事实 / 成交）`
        : '真实审计链 · 当前窗口无记录'
    }
    const wrap = section.querySelector('.table-wrap')
    if (!wrap) return
    if (n3 === 0) {
      nodata(wrap, '审计链', 'audit 工具返回 0 条记录')
      return
    }
    const rows = entries.slice(0, 4).map((entry) => {
      const at = String(entry.at ?? '')
      const time = at.includes('T') ? `${at.slice(0, 10)} ${at.slice(11, 19)}` : at
      const detail = String(entry.detail ?? '')
      return '<tr>'
        + `<td class="num">${esc(at ? time : '—')}</td>`
        + `<td>${esc(entry.source_label ?? entry.source ?? '—')}</td>`
        + `<td class="num" title="与第一列同一个时间戳">${esc(at ? time.slice(11) : '—')}</td>`
        + `<td>${noSource('审计链不记录操作人 / 来源 IP')}</td>`
        + `<td class="num">${esc(entry.kind ?? '—')}</td>`
        + `<td>${badge('已记录', 'green')}</td>`
        + `<td class="num" title="${esc(detail)}">${esc(detail.length > 26 ? detail.slice(0, 26) + '…' : detail) || '—'}</td>`
        + '</tr>'
    }).join('')
    wrap.innerHTML = '<table class="data-table"><thead><tr>'
      + '<th>时间</th><th>来源</th><th>时刻</th><th>操作人</th><th>类型</th><th>记录</th><th>详情摘要</th>'
      + '</tr></thead><tbody>' + rows + '</tbody></table>'
      + `<div class="table-foot">来源：/api/v3/audit · window=120 由前端传入但审计工具面不接受该字段（后端只在工具声明该字段时才下传）· 信号 ${n(stats.signals)} · 订单事实 ${n(stats.orders)} · 成交 ${n(stats.fills)}`
      + `（链接 ${n(stats.linked)} / 未链接 ${n(stats.unlinked)}）· 审计链不采集操作人与来源 IP，故该两列如实标注无数据源。</div>`
    const exportBtn = section.querySelector('[data-action="export"]')
    if (exportBtn && exportBtn.dataset.bound !== 'v3') {
      exportBtn.dataset.bound = 'v3'
      const clone = exportBtn.cloneNode(true)
      exportBtn.replaceWith(clone)
      clone.addEventListener('click', () => exportAudit(clone))
    }
  }

  /** 导出：用当前真实条目在浏览器端生成 CSV（不做假动作，也不伪造服务端任务）。 */
  function exportAudit(button) {
    const escape = (value) => `"${String(value ?? '').replace(/"/g, '""')}"`
    const lines = ['时间,来源,类型,标的,详情']
      .concat(auditRows.map((entry) => [entry.at, entry.source_label ?? entry.source, entry.kind, entry.ticker, entry.detail].map(escape).join(',')))
    const blob = new Blob([`\ufeff${lines.join('\n')}`], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `v3-audit-${new Date().toISOString().slice(0, 10)}.csv`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    const original = button.textContent
    button.textContent = `已导出 ${auditRows.length} 条`
    setTimeout(() => { button.textContent = original }, 2400)
  }

  // ── 富途 OpenAPI / OpenD 授权（只读状态）──────────────────────────────────
  function renderFutu(settings) {
    const section = document.querySelector('[data-od-id="futu-openapi-auth"]')
    if (!section) return
    const futu = settings.futu ?? {}
    const source = (settings.data_sources ?? []).find((item) => String(item.name).includes('富途 OpenAPI'))
    const openapiOk = Boolean(source?.available)
    const headBadge = section.querySelector('.card-head .badge')
    if (headBadge) {
      headBadge.className = `badge badge-${openapiOk ? 'green' : 'amber'}`
      headBadge.innerHTML = `<i class="dot dot-${openapiOk ? 'green' : 'amber'}"></i>OpenAPI 凭据${openapiOk ? '就绪' : '不可用'} · 连接态由 OpenD 会话决定`
    }
    const cols = section.querySelectorAll('.futu-col')
    const envs = Object.fromEntries((settings.env ?? []).map((item) => [item.key, item]))
    // 左列：OpenD 连接配置
    if (cols[0]) {
      const kvs = cols[0].querySelectorAll('.kv')
      const host = envs.FUTU_OPEND_HOST
      const port = envs.FUTU_OPEND_PORT
      const endpoint = host?.injected || port?.injected
        ? `${host?.injected ? 'FUTU_OPEND_HOST 已注入' : 'FUTU_OPEND_HOST 默认'} : ${port?.injected ? 'FUTU_OPEND_PORT 已注入' : 'FUTU_OPEND_PORT 默认'}`
        : '默认 127.0.0.1 : 11111'
      if (kvs[0]) {
        kvs[0].querySelector('.v').innerHTML = `<span class="num">${esc(endpoint)}</span>`
          + `<span class="kv-tag" title="${esc(host?.source ?? '未注入')} / ${esc(port?.source ?? '未注入')}">环境变量仅报注入状态，不显示值</span>`
      }
      if (kvs[1]) {
        kvs[1].querySelector('.v').innerHTML = noSource('本服务不维护 OpenD 会话：连接/心跳由 OpenD 客户端自身暴露')
          + ' · <a href="#v3FutuPanel">本页下方「测试连接」用已保存凭据发一次真实请求</a>'
      }
      if (kvs[2]) {
        kvs[2].querySelector('.v').innerHTML = noSource('工具面不返回登录账号（凭据文件只回键名，不回值）')
      }
      if (kvs[3]) {
        kvs[3].querySelector('.v').innerHTML = '<span class="num mask" style="color:var(--faint)">不显示</span>'
          + '<span class="kv-tag">密钥不在本页展示，也不在本页修改</span>'
      }
      if (kvs[4]) {
        kvs[4].querySelector('.v').innerHTML = '<span class="num mask" style="color:var(--faint)">不显示</span>'
          + '<span class="kv-tag">同上</span>'
      }
      const note = cols[0].querySelector('.kv-note')
      if (note) {
        note.innerHTML = `存储方式：凭据文件 <code style="font-family:var(--mono)">~/futu-openapi.json</code>（本接口只回键名）`
          + ` · mode=<b class="num">${esc(dash(futu.openapi?.mode))}</b>`
          + ` · 键名 ${esc((futu.openapi?.config_keys ?? []).join(' / ') || '—')}`
          + ' · 最近轮换时间无数据源'
      }
    }
    // 右列：富途 MCP 授权
    if (cols[1]) {
      const kvs = cols[1].querySelectorAll('.kv')
      const bearer = futu.mcp_bearer ?? {}
      if (kvs[0]) {
        const expiryRaw = bearer.expiry ? String(bearer.expiry) : ''
        const left = expiryRaw && !Number.isNaN(Date.parse(expiryRaw))
          ? Math.max(0, Math.round((Date.parse(expiryRaw) - Date.now()) / 60000))
          : null
        kvs[0].querySelector('.v').innerHTML = (bearer.present ? badge('有效', 'green') : badge('无数据源', 'amber'))
          + (left === null ? '' : `剩余 <span class="num">${Math.floor(left / 60)}h${String(left % 60).padStart(2, '0')}m</span>`)
      }
      if (kvs[1]) {
        kvs[1].querySelector('.v').innerHTML = `<span class="num">${esc(stamp(bearer.expiry))}</span>`
          + '<span class="kv-tag">来自 mcp_bearer.expiry</span>'
      }
      if (kvs[2]) {
        kvs[2].querySelector('.v').innerHTML = noSource('工具面不返回自动续期开关与最近续期时间')
      }
      if (kvs[3]) {
        kvs[3].querySelector('.v').innerHTML = `<span class="num">渠道 ${esc(dash(futu.channel))}</span>`
          + `<span class="kv-tag" title="${esc(futu.channel_source ?? '')}">${esc(futu.channel_source ?? '')}</span>`
      }
      for (const sw of cols[1].querySelectorAll('.switch')) {
        sw.classList.remove('on')
        sw.removeAttribute('role')
        sw.removeAttribute('tabindex')
        sw.title = '作用域开关：作用域来自富途侧授权，工具面不返回，只读展示'
        sw.dataset.name = `${sw.dataset.name ?? '授权作用域'}（只读）`
      }
      const btnRow = cols[1].querySelector('.btn-row')
      if (btnRow) {
        btnRow.innerHTML = '<span class="btn-note">就地凭据与 OAuth 控制在下方「OpenAPI 凭据与 OAuth 授权」面板（人工点击才发送）</span>'
      }
    }
  }

  // ── 富途 OpenAPI 凭据 + OAuth 2.1+PKCE（人工动作）──────────────────────────
  // 契约（三个端点，载荷白名单由服务端钉死）：
  //   POST /api/wb/openapi_config  空载荷=读状态；带白名单载荷=保存
  //                                {mode, app_key, algorithm, private_key_pem, private_key_path, channel}
  //   POST /api/wb/openapi_test    {} = 用已保存凭据发一次真实 trading-days 请求（ret_code 0 即通）
  //   POST /api/wb/openapi_oauth   {action:'start'|'status'|'cancel', client_id?}
  // 安全铁律：私钥 / AppKey **绝不回显**（输入框 type=password，保存成功后立即清空；
  // 同一份输入不写 localStorage / console）；授权展示只给 URL / scope 说明 / 到期口径，
  // 服务端返回的 token 类字段一个都不进 DOM。
  function renderFutuConfig(status, settings, refresh) {
    const section = document.querySelector('[data-od-id="futu-openapi-auth"]')
    if (!section) return
    const grid = section.querySelector('.futu-grid')
    if (!grid) return
    let panel = section.querySelector('#v3FutuPanel')
    if (!panel) {
      panel = el('div', { id: 'v3FutuPanel' })
      panel.style.cssText = 'border-top:1px dashed var(--border);margin-top:12px;padding-top:12px;display:flex;flex-direction:column;gap:10px'
      buildFutuPanel(panel, refresh)
      grid.insertAdjacentElement('afterend', panel)
    }
    syncFutuStatus(panel, status, settings)
  }

  function buildFutuPanel(panel, refresh) {
    // 认证方式切换（设计稿 .seg-group/.seg 语言）
    const head = el('div', { style: 'display:flex;align-items:center;gap:10px;flex-wrap:wrap' })
    head.appendChild(el('span', { class: 'sub-title', style: 'margin:0' }, 'OpenAPI 凭据与 OAuth 授权'))
    const segs = el('div', { class: 'seg-group', id: 'v3FutuSegs' })
    for (const [key, label] of [['appkey', 'AppKey 凭据'], ['oauth', 'OAuth 2.1 + PKCE']]) {
      segs.appendChild(el('button', { class: 'seg', type: 'button', 'data-f': key }, label))
    }
    head.appendChild(segs)
    head.appendChild(el('span', { class: 'btn-note', id: 'v3FutuStatus' }, '状态读取中…'))
    panel.appendChild(head)

    const body = el('div', { id: 'v3FutuBody', style: 'display:flex;flex-wrap:wrap;gap:12px;align-items:flex-start' })
    panel.appendChild(body)
    panel.appendChild(el('div', { class: 'btn-note', id: 'v3FutuMsg' }, ''))

    // 两块表单都**在加载时一次性建好**（纯读取与输入控件，零请求、零写入）：
    // 切 Tab 只切显隐，不重建——人正在输的 AppKey / PEM / Client ID 不会因为来回切 Tab 被清掉。
    const appkeyPane = el('div', { id: 'v3FutuAppKeyPane', style: 'display:flex;flex-wrap:wrap;gap:12px;flex:1 1 100%' })
    appkeyPane.appendChild(buildAppKeyForm(refresh))
    const oauthPane = el('div', { id: 'v3FutuOAuthPane', style: 'display:none;flex-wrap:wrap;gap:12px;flex:1 1 100%' })
    oauthPane.appendChild(buildOAuthPanel(refresh))
    body.appendChild(appkeyPane)
    body.appendChild(oauthPane)

    const show = (which) => {
      appkeyPane.style.display = which === 'appkey' ? 'flex' : 'none'
      oauthPane.style.display = which === 'oauth' ? 'flex' : 'none'
    }
    segs.querySelectorAll('.seg').forEach((seg) => {
      seg.addEventListener('click', () => {
        segs.querySelectorAll('.seg').forEach((item) => item.classList.remove('on'))
        seg.classList.add('on')
        panel.dataset.tab = seg.dataset.f
        show(seg.dataset.f)
      })
    })
    panel.dataset.tab = 'appkey'
    segs.querySelector('[data-f="appkey"]').classList.add('on')
    show('appkey')
  }

  /** 当前状态快照（每次 render 覆盖；OAuth 轮询完成时也更新它）。 */
  let futuStatus = null
  /** AppKey 表单里用户已选的通道/算法：切换 Tab 重建表单时保留（不被服务端状态覆盖）。 */
  const futuChoice = { channel: null, algorithm: null }

  function buildAppKeyForm(refresh) {
    const wrap = el('div', { style: 'display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start;flex:1 1 100%' })
    const appKey = input({ id: 'v3FutuAppKey', type: 'password', placeholder: 'AppKey ID（保存后不回显）', autocomplete: 'new-password' })
    const algorithm = select(ALGORITHMS.map((value) => [value, value]), futuChoice.algorithm ?? 'Ed25519', { id: 'v3FutuAlgorithm' })
    const pem = el('textarea', { class: 'input', id: 'v3FutuPem', rows: '5', spellcheck: 'false', placeholder: '私钥 PEM 原文（保存后服务端落盘 0600，本页立即清空、不回显）' })
    pem.style.cssText = 'width:420px;max-width:100%;font-size:11.5px;line-height:1.5;resize:vertical'
    const keyPath = input({ id: 'v3FutuKeyPath', placeholder: '或：已存在的私钥文件路径，如 ~/.dsh/futu-openapi-key.pem' })
    const channel = select(CHANNELS, futuChoice.channel ?? futuStatus?.channel ?? 'openapi', { id: 'v3FutuChannel' })
    futuChoice.channel = channel.value
    channel.addEventListener('change', () => { futuChoice.channel = channel.value })
    algorithm.addEventListener('change', () => { futuChoice.algorithm = algorithm.value })

    wrap.appendChild(field('AppKey ID（必填）', appKey, { width: 260 }))
    wrap.appendChild(field('签名算法', algorithm, { width: 160 }))
    wrap.appendChild(field('通道（写 trading-platform.json 的 futu_channel）', channel, { width: 260 }))
    const pemField = field('私钥 PEM（二选一：粘贴原文）', pem, { width: 420 })
    pemField.appendChild(el('span', { class: 'stat-label' }, '私钥原文只进不出：保存成功后本框立即清空，页面与接口都不回显'))
    wrap.appendChild(pemField)
    wrap.appendChild(field('私钥文件路径（二选一：已在位）', keyPath, { width: 420 }))
    const save = btn('保存凭据', { id: 'v3FutuSave' })
    const test = btn('测试连接', { id: 'v3FutuTest' })
    const row = el('div', { class: 'btn-row', style: 'flex:1 1 100%;margin-top:0' })
    row.appendChild(save)
    row.appendChild(test)
    row.appendChild(el('span', { class: 'btn-note' }, '保存：先校验后落盘（PEM 0600）；测试：用**已保存**凭据发一次真实 trading-days 请求（ret_code 0 即通）。密钥类字段绝不回显。'))
    wrap.appendChild(row)

    const msg = () => document.querySelector('#v3FutuMsg')
    save.addEventListener('click', () => withBusy(save, async () => {
      const key = (appKey.value ?? '').trim()
      if (!key) { msgIn(msg(), '保存失败：AppKey ID 必填（本页未发送请求）', 'bad'); return }
      msgIn(msg(), '正在保存凭据…', 'info')
      const payload = {
        mode: 'appkey',
        app_key: key,
        algorithm: algorithm.value,
        channel: channel.value,
      }
      const pemText = (pem.value ?? '').trim()
      const pathText = (keyPath.value ?? '').trim()
      if (pemText) payload.private_key_pem = pemText
      if (pathText) payload.private_key_path = pathText
      const body = await V3.wb('openapi_config', payload)
      if (!body?.ok) { msgIn(msg(), `保存失败：${errText(body)}`, 'bad'); return }
      appKey.value = ''
      pem.value = ''
      msgIn(msg(), `已保存（mode=${body.value?.mode ?? 'appkey'} · 算法 ${body.value?.algorithm ?? algorithm.value} · 私钥${body.value?.private_key_exists ? '在位' : '未检出'} · 指纹 ${body.value?.private_key_fingerprint ?? '—'}）· 输入框已清空、私钥不回显`, 'ok')
      await refresh()
    }))
    test.addEventListener('click', () => withBusy(test, async () => {
      msgIn(msg(), '正在用已保存凭据测试（真实请求）…', 'info')
      try {
        const body = await V3.wb('openapi_test', {})
        if (!body?.ok) { msgIn(msg(), `测试未通过：${errText(body)}`, 'bad'); return }
        const value = body.value ?? {}
        msgIn(msg(), `测试通过：HTTP ${value.http_status} · ret_code ${value.ret_code} · ${value.ret_msg} · ${value.latency_ms}ms（行情 ${value.data?.market ?? '—'}，${(value.data?.trade_days ?? []).length} 个交易日）`, 'ok')
      } catch (error) {
        msgIn(msg(), `测试未通过：${String(error?.message ?? error)}`, 'bad')
      }
    }))
    return wrap
  }

  function buildOAuthPanel(refresh) {
    const wrap = el('div', { style: 'display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start;flex:1 1 100%' })
    const clientId = input({ id: 'v3OAuthClientId', placeholder: '留空 = 自动注册 public client（PKCE required）' })
    wrap.appendChild(field('Client ID（可选）', clientId, { width: 380 }))
    const start = btn('开始 OAuth 授权', { id: 'v3OAuthStart' })
    const cancel = btn('取消授权', { id: 'v3OAuthCancel', class: 'btn btn-danger' })
    const row = el('div', { class: 'btn-row', style: 'flex:1 1 100%;margin-top:0' })
    row.appendChild(start)
    row.appendChild(cancel)
    row.appendChild(el('span', { class: 'btn-note' }, '流程：注册 client（如需）→ 服务端在本机起 127.0.0.1 回调监听 → 你在浏览器完成富途授权 → 服务端校验 state 后换取 token 并落盘（mode=oauth，0600）。'))
    wrap.appendChild(row)
    const flow = el('div', { id: 'v3OAuthFlow', style: 'flex:1 1 100%;display:flex;flex-direction:column;gap:6px' })
    wrap.appendChild(flow)

    start.addEventListener('click', () => withBusy(start, async () => {
      msgIn(document.querySelector('#v3FutuMsg'), '正在发起 OAuth 授权（PKCE + S256）…', 'info')
      const trimmed = (clientId.value ?? '').trim()
      const body = await V3.wb('openapi_oauth', trimmed ? { action: 'start', client_id: trimmed } : { action: 'start' })
      if (!body?.ok) {
        msgIn(document.querySelector('#v3FutuMsg'), `发起授权失败：${errText(body)}`, 'bad')
        oauthRender(flow, { pending: false, done: false, error: body?.error?.message ?? null, tokens_saved: false })
        return
      }
      oauthState = { phase: 'waiting' }
      renderOAuthFlow(flow, body.value ?? {}, true)
      startOAuthPolling(flow, refresh)
      msgIn(document.querySelector('#v3FutuMsg'), '等待你在浏览器完成授权…（每 2 秒轮询 status，取消或完成即停）', 'info')
    }))
    cancel.addEventListener('click', () => withBusy(cancel, async () => {
      stopOAuthPolling()
      const body = await V3.wb('openapi_oauth', { action: 'cancel' })
      if (!body?.ok) { msgIn(document.querySelector('#v3FutuMsg'), `取消失败：${errText(body)}`, 'bad'); return }
      oauthState = { phase: 'idle' }
      oauthRender(flow, { pending: false, done: false, error: null, tokens_saved: false })
      msgIn(document.querySelector('#v3FutuMsg'), '已取消授权（回调监听已停止，服务端状态已清）', 'ok')
    }))
    // 打开面板时如实反映当前流程状态（只读 status，不启动任何流程）
    oauthRender(flow, oauthState.status ?? { pending: false, done: false, error: null, tokens_saved: false })
    return wrap
  }

  let oauthTimer = null
  let oauthState = { phase: 'idle', status: null }

  function stopOAuthPolling() {
    if (oauthTimer) { clearInterval(oauthTimer); oauthTimer = null }
  }

  /** 2 秒轮询 status 直到 done / error（或人点取消）。 */
  function startOAuthPolling(flow, refresh) {
    stopOAuthPolling()
    oauthTimer = setInterval(async () => {
      const body = await V3.wb('openapi_oauth', { action: 'status' })
      if (!body?.ok) {
        oauthState = { phase: 'error', status: { pending: false, done: false, error: errText(body), tokens_saved: false } }
        stopOAuthPolling()
        oauthRender(flow, oauthState.status)
        msgIn(document.querySelector('#v3FutuMsg'), `状态轮询失败：${errText(body)}`, 'bad')
        return
      }
      const value = body.value ?? {}
      oauthState = { phase: value.done ? 'done' : value.error ? 'error' : value.pending ? 'waiting' : 'idle', status: value }
      oauthRender(flow, value)
      if (value.done || value.error || !value.pending) {
        stopOAuthPolling()
        if (value.done) {
          msgIn(document.querySelector('#v3FutuMsg'), `OAuth 授权完成：凭据已由服务端落盘（mode=oauth，0600）${value.tokens_saved ? '' : ' · 注意：服务端未报告 tokens_saved'}`, 'ok')
        } else if (value.error) {
          msgIn(document.querySelector('#v3FutuMsg'), `OAuth 授权未完成：${value.error}`, 'bad')
        }
        await refresh()
      }
    }, 2000)
  }

  function oauthRender(flow, value) {
    if (!flow) return
    flow.replaceChildren()
    const state = value ?? {}
    const badgeKind = state.done ? 'green' : state.error ? 'red' : state.pending ? 'amber' : 'grey'
    const badgeText = state.done ? '已完成 · 凭据已落盘' : state.error ? '失败' : state.pending ? '等待授权中…' : '无进行中的流程'
    const row = el('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' })
    row.appendChild(el('span', { class: `badge badge-${badgeKind}` }, badgeText))
    row.appendChild(el('span', { class: 'btn-note' }, `轮询：每 2 秒 POST /api/wb/openapi_oauth {action:'status'}（done/error 即停；无 pending 也停）`))
    flow.appendChild(row)
    if (state.authUrl) {
      const linkRow = el('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' })
      linkRow.appendChild(el('span', { class: 'stat-label' }, '授权 URL（PKCE + S256）'))
      linkRow.appendChild(el('a', { class: 'btn', href: state.authUrl, target: '_blank', rel: 'noreferrer' }, '打开富途授权页'))
      linkRow.appendChild(el('code', { class: 'num', style: 'font-size:11px;color:var(--faint);word-break:break-all;max-width:640px' }, state.authUrl))
      flow.appendChild(linkRow)
      flow.appendChild(el('div', { class: 'btn-note' }, `scope：${state.scope ?? 'openapi（服务端 DEFAULT_SCOPE）'} · 回调地址：http://localhost:${state.callback_port ?? '—'}/callback · 授权链接约 10 分钟内有效，超时可重新发起`))
    }
    if (state.error) flow.appendChild(el('div', { class: 'btn-note', style: 'color:var(--red)' }, `错误：${state.error}（令牌类字段一律不显示，凭据只由服务端落盘）`))
  }

  function renderOAuthFlow(flow, value, started) {
    oauthState = { phase: 'waiting', status: value }
    const snapshot = { ...value, pending: true, done: false, error: null }
    oauthRender(flow, snapshot)
    if (started) {
      const hint = el('div', { class: 'btn-note' }, '已开始：请在新标签页完成富途账号授权；本页不接触任何 token，完成后由服务端落盘。')
      flow.appendChild(hint)
    }
  }

  /** 状态快照同步：只写服务端返回的字段（app_key_masked / 指纹 / ready），绝无密钥值。 */
  function syncFutuStatus(panel, status, settings) {
    futuStatus = status
    const statusNode = panel.querySelector('#v3FutuStatus')
    if (statusNode && status) {
      statusNode.textContent = `configured=${status.configured === true} · mode=${dash(status.mode)} · 通道=${dash(status.channel)}`
        + ` · app_key=${status.app_key_masked ?? '—'} · 私钥${status.private_key_exists ? `在位（指纹 ${status.private_key_fingerprint ?? '—'}）` : '未检出'}`
        + ` · ready=${status.ready === true}`
    }
    const channel = panel.querySelector('#v3FutuChannel')
    if (channel && status?.channel && futuChoice.channel === null) {
      channel.value = status.channel
      futuChoice.channel = status.channel
    }
    if (status?.last_error) msgIn(panel.querySelector('#v3FutuMsg'), `凭据文件读取失败：${status.last_error}`, 'bad')
  }


  // ── 统一授权中心（真实凭据状态）─────────────────────────────────────────────
  function renderCredentials(credentials, settings, metrics) {
    const section = document.querySelector('[data-od-id="credential-registry"]')
    if (!section) return
    const keys = Array.isArray(credentials.keys) ? credentials.keys : []
    const sub = section.querySelector('.title-sub')
    if (sub) sub.textContent = `真实凭据状态 · ${keys.length} 项（/api/v3/credentials）· 只显示是否注入与掩码尾号`
    const tbody = section.querySelector('#credTable tbody')
    if (!tbody) return
    // 表头按接口真实字段对齐（只给 label/env/present/source/updated_at/hint，绝无值）
    const thead = section.querySelector('#credTable thead tr')
    if (thead) {
      thead.innerHTML = '<th>凭据</th><th>环境变量</th><th>是否已配置</th><th>来源</th><th>更新时间</th><th>掩码尾号</th><th>操作</th>'
    }
    const rows = keys.map((item) => {
      const present = Boolean(item.present)
      const status = badge(present ? '已配置' : '未配置', present ? 'green' : 'amber')
      return `<tr data-status="${present ? 'ok' : 'pending'}">`
        + `<td>${esc(item.label)}<span class="kv-tag" style="margin-left:6px">${esc(item.usage ?? '')}</span></td>`
        + `<td class="num svc" title="只显示变量名，不显示值">${esc(item.env)}</td>`
        + `<td>${status}</td>`
        + `<td>${esc(item.source ?? '—')}</td>`
        + `<td class="num">${esc(item.updated_at ? stamp(item.updated_at) : '—')}</td>`
        + `<td class="num" title="只保留尾 4 位，用于人眼核对自己填的那把">${esc(item.hint ?? '—')}</td>`
        + '<td><div class="ops"><span class="op" style="cursor:default;color:var(--faint)" '
        + 'title="本页只读展示凭据状态；写入由 Agent 调用 /api/v3/credentials（save / test / clear）">只读</span></div></td>'
        + '</tr>'
    }).join('')
    tbody.innerHTML = rows
    const wrap = section.querySelector('.table-wrap')
    if (wrap) {
      const existing = wrap.parentElement?.querySelector('.table-foot')
      const foot = existing ?? document.createElement('div')
      foot.className = 'table-foot'
      foot.innerHTML = '本页只读展示「是否配置 / 来源 / 掩码尾号」，任何位置都不显示密钥值。'
        + `其余凭据（富途 OpenAPI、DeepSeek BYOK、MCP 凭据）不由 /api/v3/credentials 托管：`
        + `富途授权见「富途 OpenAPI / OpenD 授权」区块，环境变量注入状态见下表。`
        + `Tushare token 的写入/测试/清除由 Agent 调用 <code>/api/v3/credentials</code> 完成。`
      if (!existing) wrap.parentElement.appendChild(foot)
    }
    // 分段计数：设计稿脚本在 DOMContentLoaded 时按旧占位行算过一次，这里按真实行重算并重绑
    const segs = section.querySelectorAll('#credSegs .seg')
    const counts = { all: keys.length, ok: 0, readonly: 0, pending: 0, expired: 0 }
    for (const item of keys) counts[item.present ? 'ok' : 'pending'] += 1
    segs.forEach((seg) => {
      const key = seg.dataset.f
      const b = seg.querySelector('b')
      if (b && counts[key] != null) b.textContent = counts[key]
    })
    rebindCredFilters(section)
  }

  function rebindCredFilters(section) {
    const segs = section.querySelectorAll('#credSegs .seg')
    const search = section.querySelector('#credSearch')
    const apply = () => {
      const query = (search?.value ?? '').trim().toLowerCase()
      const current = [...segs].find((seg) => seg.classList.contains('on'))?.dataset.f ?? 'all'
      for (const row of section.querySelectorAll('#credTable tbody tr')) {
        const okFilter = current === 'all' || row.dataset.status === current
        const okQuery = !query || row.textContent.toLowerCase().includes(query)
        row.style.display = okFilter && okQuery ? '' : 'none'
      }
    }
    segs.forEach((seg) => {
      const clone = seg.cloneNode(true)
      seg.replaceWith(clone)
      clone.addEventListener('click', () => {
        segs.forEach((item) => item.classList.remove('on'))
        clone.classList.add('on')
        apply()
      })
    })
    if (search && search.dataset.bound !== 'v3') {
      const clone = search.cloneNode(true)
      search.replaceWith(clone)
      clone.dataset.bound = 'v3'
      clone.addEventListener('input', apply)
    }
    apply()
  }

  // ── 环境变量与密钥来源（只报注入状态 + 来源，绝不出值）──────────────────────
  function renderEnv(settings) {
    const section = document.querySelector('[data-od-id="env-key-sources"]')
    if (!section) return
    const envs = Array.isArray(settings.env) ? settings.env : []
    const missing = envs.filter((item) => !item.injected).length
    const headBadge = section.querySelector('.card-head .badge')
    if (headBadge) {
      headBadge.className = `badge badge-${missing ? 'amber' : 'green'}`
      headBadge.textContent = `${missing} 项未注入 / 共 ${envs.length} 项`
    }
    const sub = section.querySelector('.title-sub')
    if (sub) sub.textContent = 'Harness 运行时注入清单 · 只显示是否注入与来源（值不展示）'
    const tbody = section.querySelector('tbody')
    if (!tbody) return
    tbody.innerHTML = envs.map((item) => {
      const purpose = PURPOSES[item.key] ?? '（工具面未提供用途说明）'
      const status = item.injected
        ? '<span class="badge badge-grey">已注入</span>'
        : badge('未注入', 'amber')
      return `<tr><td class="num">${esc(item.key)}</td><td>${esc(purpose)}</td>`
        + `<td>${status}</td><td>${esc(item.source)}</td></tr>`
    }).join('')
    const foot = section.querySelector('.table-foot')
    if (foot) {
      foot.innerHTML = `「未注入」指该变量尚未进入 Harness 运行时环境；本表只显示注入状态与来源，`
        + `不显示任何变量值。平台侧凭据由凭据文件 / 环境变量托管，`
        + `其中 <code>TUSHARE_TOKEN</code> 的已配置状态见上方「统一授权中心」。`
    }
    return missing
  }

  // ── 自动流水线（人工动作 · POST /api/wb/auto_pipeline）─────────────────────
  // 契约：空载荷=读有效配置；带白名单载荷=校验后原子写，失败文件零改动。
  // 载荷恒等于白名单五键 {enabled, strategies, exec_at, exec_window_minutes, reconcile_at}——
  // 多带一个只读字段（error/date）就会被服务端拒绝，所以这里显式只挑这五个键。
  // strategies 每项 {market:'SH'|'HK'|'US', strategy:<内置策略 id 或已批准规则 id>, watchlist:<池键名>}。
  // 加载时**只读**，开关/时刻/策略行都只改本地草稿；只有人点「保存自动流水线」才发请求。
  let autoState = null
  let autoDirty = false

  function renderAutoPipeline(config, refresh) {
    const main = document.querySelector('.main')
    const policy = document.querySelector('[data-od-id="auth-audit-policy"]')
    if (!main || !policy) return
    let section = document.getElementById('v3AutoPipeline')
    if (!section) {
      section = el('section', { class: 'card', id: 'v3AutoPipeline' })
      const head = el('div', { class: 'card-head' })
      const title = el('div', { class: 'card-title' }, '自动流水线')
      title.appendChild(el('span', { class: 'title-sub' }, 'POST /api/wb/auto_pipeline · 人工点击保存'))
      head.appendChild(title)
      head.appendChild(el('span', { class: 'badge badge-grey', id: 'v3AutoBadge' }, '读取中'))
      section.appendChild(head)
      section.appendChild(el('div', { id: 'v3AutoBody', style: 'display:flex;flex-direction:column;gap:10px' }))
      policy.insertAdjacentElement('beforebegin', section)
      buildAutoPipeline(section, refresh)
    }
    applyAutoConfig(section, config)
  }

  function autoDraft(config) {
    const src = config && typeof config === 'object' ? config : {}
    const execAt = src.exec_at && typeof src.exec_at === 'object' ? src.exec_at : {}
    return {
      enabled: src.enabled === true,
      strategies: (Array.isArray(src.strategies) ? src.strategies : []).map((row) => ({
        market: row?.market ?? 'SH',
        strategy: row?.strategy ?? '',
        watchlist: row?.watchlist ?? POOL_KEY_DEFAULT,
      })),
      exec_at: Object.fromEntries(MARKETS.map((market) => [market, execAt[market] ?? ''])),
      exec_window_minutes: Number.isInteger(src.exec_window_minutes) ? src.exec_window_minutes : 30,
      reconcile_at: typeof src.reconcile_at === 'string' ? src.reconcile_at : '',
    }
  }

  function buildAutoPipeline(section, refresh) {
    const body = section.querySelector('#v3AutoBody')
    // 首屏骨架（读取返回前的占位草稿）：applyAutoConfig 拿到有效配置后立即覆盖。
    // 必须在建行之前赋值——renderAutoRows 依赖 autoState。
    autoState = autoDraft(null)
    const enable = el('div', { class: 'switch', id: 'v3AutoEnabled', role: 'switch', 'aria-checked': 'false', tabindex: '0', 'data-name': '自动流水线' })
    // 设计稿共享脚本给 .switch 绑了演示 handler（弹「界面提示」toast）：这里在捕获阶段接管。
    enable.addEventListener('click', (event) => {
      event.preventDefault()
      event.stopImmediatePropagation()
      const on = !enable.classList.contains('on')
      toggleAutoEnabled(on)
    }, true)
    const enableRow = el('div', { style: 'display:flex;gap:10px;align-items:center;flex-wrap:wrap' })
    enableRow.appendChild(enable)
    enableRow.appendChild(el('span', { style: 'color:var(--muted);font-size:12.5px' }, '启用自动流水线（仅模拟盘自动执行；实盘计划照常生成，仍需在「执行与审批」页人工确认）'))
    body.appendChild(enableRow)

    body.appendChild(el('div', { class: 'btn-note' }, '策略行：市场 + 策略 + 关注池键名。策略取值域由服务端校验（内置策略 id，或研究页「已批准（status=enabled）」的规则 id）——写成别的名字保存会被拒绝并列出合法取值。'))
    const rows = el('div', { id: 'v3AutoRows', style: 'display:flex;flex-direction:column;gap:8px' })
    // 删除按委派绑定一次（行会整体重绘，逐个行走监听会随重绘倍增）
    rows.addEventListener('click', (event) => {
      const button = event.target.closest('[data-role="remove"]')
      if (!button) return
      autoState.strategies.splice(Number(button.dataset.index), 1)
      autoDirty = true
      renderAutoRows(section)
    })
    body.appendChild(rows)
    const add = btn('添加策略', { id: 'v3AutoAdd', class: 'btn-text' })
    add.style.alignSelf = 'flex-start'
    add.addEventListener('click', () => {
      autoState.strategies.push({ market: 'SH', strategy: 'watchlist_rsi', watchlist: POOL_KEY_DEFAULT })
      autoDirty = true
      renderAutoRows(section)
    })
    body.appendChild(add)

    const times = el('div', { style: 'display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap' })
    for (const market of MARKETS) {
      times.appendChild(field(`执行时刻 · ${MARKET_LABEL[market]}`, input({ id: `v3AutoExec${market}`, placeholder: 'HH:MM', 'data-market': market, maxlength: '5' }), { width: 110 }))
    }
    times.appendChild(field('执行窗口（分钟，1–240）', input({ id: 'v3AutoWindow', type: 'number', min: '1', max: String(EXEC_WINDOW_MAX_MINUTES) }), { width: 140 }))
    times.appendChild(field('晚间对账时刻', input({ id: 'v3AutoReconcile', placeholder: 'HH:MM', maxlength: '5' }), { width: 110 }))
    body.appendChild(times)

    const actions = el('div', { class: 'btn-row', style: 'margin-top:0' })
    const save = btn('保存自动流水线', { id: 'v3AutoSave' })
    actions.appendChild(save)
    actions.appendChild(el('span', { class: 'btn-note' }, '保存 = 校验（与调度侧同一实现）后原子写 trading-platform.json；失败则文件零改动，错误原样显示。'))
    body.appendChild(actions)
    body.appendChild(el('div', { class: 'btn-note', id: 'v3AutoMsg' }, ''))

    save.addEventListener('click', () => withBusy(save, async () => {
      const msg = section.querySelector('#v3AutoMsg')
      const draft = readAutoDraft(section)
      if (!draft) { msgIn(msg, '保存失败：执行时刻需为 HH:MM（如 09:35），执行窗口需为 1–240 的整数。本页未发送请求。', 'bad'); return }
      msgIn(msg, '正在保存…', 'info')
      const payload = {}
      for (const key of AUTO_PIPELINE_KEYS) payload[key] = draft[key]
      const posted = JSON.stringify(payload)
      const body = await V3.wb('auto_pipeline', payload)
      if (!body?.ok) { msgIn(msg, `保存失败：${errText(body)}`, 'bad'); return }
      autoDirty = false
      applyAutoConfig(section, body.value)
      const effective = await V3.wb('auto_pipeline', {})   // 保存后重新读取并显示生效值
      if (effective?.ok) {
        applyAutoConfig(section, effective.value)
        const value = effective.value ?? {}
        msgIn(msg, `已保存并重新读取生效值：enabled=${value.enabled === true} · 策略 ${(value.strategies ?? []).length} 项 · 执行窗口 ${value.exec_window_minutes} 分钟 · 对账 ${value.reconcile_at ?? '—'}（提交 ${posted.length} 字节，未含白名单外字段）`, 'ok')
      } else {
        msgIn(msg, `已保存，但重新读取失败：${errText(effective)}`, 'bad')
      }
      await refresh()
    }))
  }

  function toggleAutoEnabled(on) {
    const node = document.getElementById('v3AutoEnabled')
    if (node) {
      node.classList.toggle('on', on)
      node.setAttribute('aria-checked', on ? 'true' : 'false')
    }
    if (autoState) autoState.enabled = on
    autoDirty = true
    const badge = document.getElementById('v3AutoBadge')
    if (badge) {
      badge.className = `badge badge-${on ? 'amber' : 'grey'}`
      badge.textContent = on ? '草稿：将开启（未保存）' : '草稿：将关闭（未保存）'
    }
  }

  function renderAutoRows(section) {
    const rows = section.querySelector('#v3AutoRows')
    if (!rows || !autoState) return
    rows.replaceChildren()
    if (autoState.strategies.length === 0) {
      rows.appendChild(el('div', { class: 'btn-note' }, '未配置策略：开启后流水线只跑数据与对账，不会生成计划。'))
      return
    }
    autoState.strategies.forEach((row, index) => {
      const line = el('div', { style: 'display:flex;gap:8px;align-items:center;flex-wrap:wrap' })
      const market = select(MARKETS.map((m) => [m, `${m} · ${MARKET_LABEL[m]}`]), row.market, { 'data-index': index, 'data-role': 'market', 'aria-label': `策略 ${index + 1} 市场` })
      const strategy = el('input', { class: 'input', list: 'v3AutoStrategyList', 'data-index': index, 'data-role': 'strategy', 'aria-label': `策略 ${index + 1} 名称`, placeholder: '策略 id（内置策略或已批准的规则 id）' })
      strategy.value = row.strategy
      const pool = el('input', { class: 'input', list: 'v3AutoPoolList', 'data-index': index, 'data-role': 'watchlist', 'aria-label': `策略 ${index + 1} 关注池键`, placeholder: POOL_KEY_DEFAULT })
      pool.value = row.watchlist
      const del = opBtn('删除', { class: 'op danger', 'data-index': index, 'data-role': 'remove' })
      for (const node of [market, strategy, pool]) {
        node.style.width = node === strategy ? '300px' : '180px'
        node.addEventListener('change', () => { syncAutoRow(index, node) })
        node.addEventListener('input', () => { syncAutoRow(index, node) })
        line.appendChild(node)
      }
      line.appendChild(del)
      rows.appendChild(line)
    })
    ensureAutoDatalists()
    fillAutoPoolList()
  }

  /** 策略/池键的联想候选（datalist 挂在 body 上，与设计稿 DOM 无关，只做输入提示）。 */
  function ensureAutoDatalists() {
    if (!document.getElementById('v3AutoStrategyList')) {
      const list = el('datalist', { id: 'v3AutoStrategyList' })
      list.replaceChildren(...BUILTIN_STRATEGIES.map((value) => el('option', { value })))
      document.body.appendChild(list)
    }
    if (!document.getElementById('v3AutoPoolList')) document.body.appendChild(el('datalist', { id: 'v3AutoPoolList' }))
  }

  function fillAutoPoolList() {
    const pools = document.getElementById('v3AutoPoolList')
    if (!pools || !autoState) return
    const keys = [POOL_KEY_DEFAULT, ...new Set(autoState.strategies.map((row) => row.watchlist).filter(Boolean))]
    pools.replaceChildren(...keys.map((key) => el('option', { value: key })))
  }

  function syncAutoRow(index, node) {
    const row = autoState.strategies[index]
    if (!row) return
    row[node.dataset.role] = node.value
    autoDirty = true
  }

  /** 读回服务端有效配置 → 写进控件（用户正在编辑时不覆盖，只更新草稿里的非编辑字段）。 */
  function applyAutoConfig(section, config) {
    const draft = autoDraft(config)
    if (!autoState || !autoDirty) autoState = draft
    const enable = section.querySelector('#v3AutoEnabled')
    const enabled = autoState.enabled
    if (enable) {
      enable.classList.toggle('on', enabled)
      enable.setAttribute('aria-checked', enabled ? 'true' : 'false')
    }
    const badge = section.querySelector('#v3AutoBadge')
    if (badge) {
      const illegal = Boolean(config?.error)
      badge.className = `badge badge-${illegal ? 'red' : enabled ? 'green' : 'grey'}`
      badge.textContent = illegal ? '配置非法' : enabled ? '自动执行已开启' : '自动执行关闭'
      badge.title = illegal ? `配置文件里的 auto_pipeline 无法被调度侧解析：${config.error}` : ''
    }
    const sub = section.querySelector('.title-sub')
    if (sub) sub.textContent = 'POST /api/wb/auto_pipeline · 人工点击保存 · 校验复用调度侧同一实现'
    for (const market of MARKETS) {
      const node = document.getElementById(`v3AutoExec${market}`)
      if (node && !node.dataset.v3Touched) node.value = autoState.exec_at[market] ?? ''
    }
    const win = section.querySelector('#v3AutoWindow')
    if (win && !win.dataset.v3Touched) win.value = String(autoState.exec_window_minutes)
    const rec = section.querySelector('#v3AutoReconcile')
    if (rec && !rec.dataset.v3Touched) rec.value = autoState.reconcile_at ?? ''
    // 人工编辑标记：只在首次绑定（重绘不重复挂监听，也不覆盖人正在输的值）
    for (const node of section.querySelectorAll('#v3AutoExecSH, #v3AutoExecHK, #v3AutoExecUS, #v3AutoWindow, #v3AutoReconcile')) {
      if (node.dataset.v3TouchedBound === '1') continue
      node.dataset.v3TouchedBound = '1'
      node.addEventListener('input', () => { node.dataset.v3Touched = '1' })
    }
    renderAutoRows(section)
  }

  /** 控件 → 草稿（含本地格式校验；非法时返回 null，**不发请求**）。 */
  function readAutoDraft(section) {
    const execAt = {}
    for (const market of MARKETS) {
      const value = (section.querySelector(`#v3AutoExec${market}`)?.value ?? '').trim()
      if (!HHMM_RE.test(value)) return null
      execAt[market] = value
    }
    const windowValue = Number(section.querySelector('#v3AutoWindow')?.value)
    if (!Number.isInteger(windowValue) || windowValue < 1 || windowValue > EXEC_WINDOW_MAX_MINUTES) return null
    const reconcile = (section.querySelector('#v3AutoReconcile')?.value ?? '').trim()
    if (!HHMM_RE.test(reconcile)) return null
    const strategies = [...section.querySelectorAll('#v3AutoRows [data-role="strategy"]')].map((node) => {
      const index = Number(node.dataset.index)
      return {
        market: section.querySelector(`#v3AutoRows [data-role="market"][data-index="${index}"]`)?.value ?? 'SH',
        strategy: (node.value ?? '').trim(),
        watchlist: (section.querySelector(`#v3AutoRows [data-role="watchlist"][data-index="${index}"]`)?.value ?? '').trim() || POOL_KEY_DEFAULT,
      }
    })
    return {
      enabled: Boolean(document.getElementById('v3AutoEnabled')?.classList.contains('on')),
      strategies,
      exec_at: execAt,
      exec_window_minutes: windowValue,
      reconcile_at: reconcile,
    }
  }


  function renderPolicy(settings) {
    const section = document.querySelector('[data-od-id="auth-audit-policy"]')
    if (!section) return
    const tiers = section.querySelectorAll('.tier')
    if (tiers[0]) {
      const desc = tiers[0].querySelector('.tier-desc')
      if (desc) {
        desc.innerHTML = '单笔 ≤ <b class="num">2%</b> · 行业 ≤ <b class="num">20%</b> · 回撤 ≤ <b class="num">15%</b>'
          + `（v3_ops.LIMITS），当前模式 <b class="num">${esc(String(settings.trading_mode ?? '—').toUpperCase())}</b>`
      }
    }
    if (tiers[1]) {
      const desc = tiers[1].querySelector('.tier-desc')
      if (desc) desc.textContent = '进入审批队列（待审批笔数见执行与审批页）；LIVE 下所有执行均需人工审批与口令'
    }
    if (tiers[2]) {
      const desc = tiers[2].querySelector('.tier-desc')
      if (desc) desc.textContent = '触发红线规则立即拒绝并推送告警，记录完整审计链（审计链真实来源：/api/v3/audit）'
    }
    const note = section.querySelector('.btn-note')
    if (note) note.innerHTML = '口令轮换由工作台 Web 的口令闸门负责 · 轮换时间：<span style="color:var(--faint)">无数据源</span>'
    for (const row of section.querySelectorAll('.policy-row')) {
      const text = (row.querySelector('.policy-text')?.textContent ?? '').trim()
      if (text.startsWith('审计保留期')) {
        const value = row.querySelector('.num.strong')
        if (value) { value.textContent = '无数据源'; value.style.color = 'var(--faint)' }
      }
      if (text.startsWith('审计日志级别')) {
        const tag = row.querySelector('.badge')
        if (tag) { tag.className = 'badge badge-blue'; tag.textContent = '审计链完整（/api/v3/audit）' }
      }
    }
    for (const sw of section.querySelectorAll('.policy-row .switch')) {
      sw.title = '策略开关由风控引擎统一下发：本页只读展示，不提供写入'
      sw.removeAttribute('role')
      sw.removeAttribute('tabindex')
      sw.addEventListener('click', (event) => event.stopImmediatePropagation(), true)
    }
  }

  // ── 页脚 ───────────────────────────────────────────────────────────────────
  function renderFooter(settings, metrics, credentials) {
    const foot = document.querySelector('.page-foot')
    if (!foot) return
    const unconfigured = (credentials.keys ?? []).filter((item) => !item.present).length
    foot.innerHTML = `<span>数据来源：/api/v3/settings · /api/v3/credentials · /api/v3/metrics · /api/v3/audit</span>`
      + '<span class="sep">·</span>'
      + `<span>环境变量只显示是否注入与来源；Tushare token 只显示状态与掩码尾号（未配置 ${unconfigured} 项），任何位置都不显示密钥值</span>`
      + '<span class="sep">·</span>'
      + `<span>工具 ${metrics.toolTotal ?? '—'} 个 · 数据截至 ${stamp(metrics.generated_at)}</span>`
    const sync = document.querySelector('.page-sync .num')
    if (sync) sync.textContent = stamp(metrics.generated_at)
    const side = document.querySelector('.side-foot')
    if (side) side.textContent = `v3 · 工具 ${metrics.toolTotal ?? '—'} 个 · 凭据未配置时 Tushare 取数直接返回 no-token`
  }

  /** Tushare token 的**页面化配置**：在设计稿凭据表下方就地渲染输入框 + 保存/测试/清除。
   *  设计稿 HTML 不改；控件使用设计稿自己的 .input/.btn 类与 token 变量，视觉与页面一致。
   *  安全：输入框 type=password，保存后立即清空；服务端与页面都不回显凭据值（只显示掩码尾号）。 */
  function renderCredentialOps(credentials, refresh) {
    const section = document.querySelector('[data-od-id="credential-registry"]')
    if (!section) return
    const entries = Array.isArray(credentials.keys) ? credentials.keys : []
    const entry = entries[0]
    if (!entry) return
    const present = Boolean(entry.present)

    // 1) 表格行内操作列：把「只读」换成真实按钮
    const firstRowOps = section.querySelector('#credTable tbody tr td:last-child .ops')
    if (firstRowOps) {
      firstRowOps.innerHTML = '<button class="op" data-cred="test">测试</button>'
        + (present ? '<button class="op danger" data-cred="clear">清除</button>' : '')
    }

    // 2) 表下就地表单（已存在则不重复创建）
    let panel = section.querySelector('#v3CredOps')
    if (!panel) {
      panel = document.createElement('div')
      panel.id = 'v3CredOps'
      panel.className = 'table-foot'
      panel.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px'
      panel.innerHTML = '<span style="color:var(--muted)">' + esc(entry.label) + '：</span>'
        + '<input class="input" id="v3CredValue" type="password" autocomplete="off" '
        + 'placeholder="粘贴 token（保存后不再回显）" style="min-width:320px">'
        + '<button class="btn" id="v3CredSave">保存</button>'
        + '<button class="btn" id="v3CredTest">测试连通性</button>'
        + (present ? '<button class="btn" id="v3CredClear">清除页面配置</button>' : '')
        + '<span id="v3CredMsg" style="color:var(--faint)"></span>'
      const wrap = section.querySelector('.table-wrap')
      ;(wrap ?? section).parentElement?.appendChild(panel) ?? section.appendChild(panel)

      const msg = (text, tone) => {
        const box = panel.querySelector('#v3CredMsg')
        if (box) { box.textContent = text; box.style.color = tone === 'bad' ? 'var(--red)' : tone === 'ok' ? 'var(--green)' : 'var(--faint)' }
      }
      const act = async (action) => {
        const input = panel.querySelector('#v3CredValue')
        const value = input?.value?.trim() ?? ''
        if (action === 'save' && !value) { msg('请先粘贴 token', 'bad'); return }
        msg('执行中…')
        try {
          const body = await V3.post('credentials', { action, key: entry.key, value })
          if (!body?.ok) { msg(`${body?.error?.code ?? '失败'}：${body?.error?.message ?? ''}`, 'bad'); return }
          if (action === 'test') {
            msg(body.ok ? `连通正常（${body.latency_ms}ms · 来源 ${body.source}）` : '', 'ok')
            if (body.error) msg(`${body.error.code}：${body.error.message}`, 'bad')
          } else {
            if (action === 'save' && input) input.value = ''
            msg(action === 'save' ? '已保存（0600 落盘，立即生效；环境变量优先）' : '已清除页面配置（环境变量不受影响）', 'ok')
          }
          await refresh()
        } catch (error) {
          msg(String(error?.message ?? error), 'bad')
        }
      }
      panel.querySelector('#v3CredSave')?.addEventListener('click', () => act('save'))
      panel.querySelector('#v3CredTest')?.addEventListener('click', () => act('test'))
      panel.querySelector('#v3CredClear')?.addEventListener('click', () => act('clear'))
    }

    // 3) 行内按钮绑定（每次渲染后重新绑定）
    section.querySelectorAll('[data-cred]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const action = btn.dataset.cred
        const value = panel.querySelector('#v3CredValue')?.value?.trim() ?? ''
        const msg = panel.querySelector('#v3CredMsg')
        if (action === 'save' && !value) { if (msg) { msg.textContent = '请先在下方输入 token'; msg.style.color = 'var(--red)' } return }
        const body = await V3.post('credentials', { action, key: entry.key, value })
        if (msg) {
          msg.textContent = body?.ok ? (action === 'test' ? `连通正常（${body.latency_ms}ms）` : '已清除') : `${body?.error?.code ?? '失败'}`
          msg.style.color = body?.ok ? 'var(--green)' : 'var(--red)'
        }
        await refresh()
      })
    })
  }

  /** 人工动作后的状态刷新：重新取 /api/v3/settings 与 /api/v3/overview（模式切换的硬要求），
   *  并一并刷新 OpenAPI 凭据状态 / 自动流水线有效配置。epoch 保证并发刷新只有最后一次落地。 */
  let refreshEpoch = 0

  async function render() {
    const epoch = ++refreshEpoch
    const [settings, metrics, overview, orders, audit, credentials, openapi, pipeline] = await Promise.all([
      V3.api('settings'), V3.api('metrics'), V3.api('overview'), V3.api('oms/orders'),
      V3.api('audit?window=120'), V3.api('credentials'),
      V3.wb('openapi_config', {}), V3.wb('auto_pipeline', {}),
    ])
    if (epoch !== refreshEpoch) return
    if (settings.ok) {
      renderShell(overview.ok ? overview : { equity: {} }, settings, metrics.ok ? metrics : {})
      renderMode(settings, overview.ok ? overview : {}, orders.ok ? orders : {}, metrics.ok ? metrics : {})
      renderFutu(settings)
      renderFutuConfig(openapi.ok ? openapi.value : null, settings, render)
      renderPolicy(settings)
      renderEnv(settings)
      patchMode(settings, render)
    } else {
      const main = document.querySelector('.main')
      if (main) nodata(main, '接入与授权', settings.error?.message)
    }
    if (audit.ok) renderAudit(audit)
    renderCredentials(credentials.ok ? credentials : { keys: [] }, settings.ok ? settings : {}, metrics.ok ? metrics : {})
    if (credentials.ok) renderCredentialOps(credentials, render)
    renderAutoPipeline(pipeline.ok ? pipeline.value : null, render)
    if (metrics.ok) renderFooter(settings.ok ? settings : {}, metrics, credentials.ok ? credentials : { keys: [] })
    V3.demoSweep(document.body)
  }

  render().catch((error) => {
    const main = document.querySelector('.main')
    if (main) nodata(main, '接入与授权', String(error?.message ?? error))
  })
})()
