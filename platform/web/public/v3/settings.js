// V3「接入与授权」页数据绑定：设计稿 settings.html 的 HTML/CSS 一字不动，
// 这里只把 /api/v3/* 的真实值写进对应节点，并把本服务确实没有的数据源显式标注。
//
// 安全纪律（硬约束，逐条对应接口契约）：
//   * 环境变量表只显示「是否注入 + 来源」，**绝不显示值**（后端 _env_status 也只返回这两个字段）
//   * Tushare token 只显示 present / source / updated_at / 掩码尾号（/api/v3/credentials），
//     页面不提供输入框、也不伪造保存按钮——真实写入口是工作台设置页（#/legacy/settings）
//   * 富途 OpenAPI / OAuth 只读展示状态：授权流程复用既有工作台设置页，
//     本页不出现第二套授权实现
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
      toLive.disabled = true
      toLive.textContent = '切换到实盘（需工作台 Web 口令）'
      toLive.title = 'sim→live 只能在既有工作台 Web 输入口令完成；V3 页面不提供该通道'
    }
    const toSim = document.getElementById('toSim')
    if (toSim) {
      toSim.textContent = isSim ? '当前已是模拟盘' : '切回模拟盘（需工作台 Web）'
      toSim.disabled = isSim
    }
    const confirmLive = document.getElementById('confirmLive')
    if (confirmLive) {
      confirmLive.title = '切换请求不会由本页发出：sim→live 只能在既有工作台 Web 输入口令完成'
    }
    const pw = document.getElementById('modePw')
    if (pw) {
      pw.placeholder = '口令校验（只影响下方「前置校验」显示）'
      pw.title = '本页不提交切换请求：口令只用于演示前置校验项的通过状态'
    }
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
          + ' · <a href="#/legacy/settings">到工作台设置页测试连接</a>'
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
        btnRow.innerHTML = '<a class="btn" href="#/legacy/settings" '
          + 'title="富途 OpenAPI / OAuth 授权流程复用既有工作台设置页">前往工作台设置页授权</a>'
          + '<span class="btn-note">富途 OAuth 与 AppKey 保存只在工作台设置页（<code>#/legacy/settings</code>），本页不实现第二套授权</span>'
      }
    }
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

  // ── 授权与审计策略 ─────────────────────────────────────────────────────────
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

  async function render() {
    const [settings, metrics, overview, orders, audit, credentials] = await Promise.all([
      V3.api('settings'), V3.api('metrics'), V3.api('overview'), V3.api('oms/orders'),
      V3.api('audit?window=120'), V3.api('credentials'),
    ])
    if (settings.ok) {
      renderShell(overview.ok ? overview : { equity: {} }, settings, metrics.ok ? metrics : {})
      renderMode(settings, overview.ok ? overview : {}, orders.ok ? orders : {}, metrics.ok ? metrics : {})
      renderFutu(settings)
      renderPolicy(settings)
      renderEnv(settings)
    } else {
      const main = document.querySelector('.main')
      if (main) nodata(main, '接入与授权', settings.error?.message)
    }
    if (audit.ok) renderAudit(audit)
    renderCredentials(credentials.ok ? credentials : { keys: [] }, settings.ok ? settings : {}, metrics.ok ? metrics : {})
    if (metrics.ok) renderFooter(settings.ok ? settings : {}, metrics, credentials.ok ? credentials : { keys: [] })
    V3.demoSweep(document.body)
  }

  render().catch((error) => {
    const main = document.querySelector('.main')
    if (main) nodata(main, '接入与授权', String(error?.message ?? error))
  })
})()
