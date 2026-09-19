// V3「工具域治理」页数据绑定：设计稿 tools.html 的 HTML/CSS 一字不动，
// 这里只把 /api/v3/* 的真实值写进对应节点，并把本服务确实没有的数据源显式标注。
//
// 数据诚实性（与后端 v3_ops.py 的契约一致）：
//   * 六大工具域目录 → GET /api/v3/tools 的真实返回（name/kind/wb/desc），
//     一个工具名都不硬编码；空域写「该域暂无工具」
//   * 今日调用 / 调用总数 / 失败率 → GET /api/v3/metrics 的进程内计数（mcp.calls/errors、
//     mcp.tools{名字:次数}）；**注意该计数是「本服务进程内」的调用，不是生产 QPS**
//   * 工具发现代理的两段 JSON → 左侧用真实域名/工具名/调用次数生成，右侧是一次真实
//     /api/v3/series 调用的响应（取不到就标无数据源，不编造）
//   * 平台 MCP 服务器注册 → 工具面不返回进程 PID / 启动时间 / update 分段耗时 → 无数据源
//   * 健康与降级 → 失败率 Top5 只有「整块失败率」是真实计数，单工具 P95/失败率无数据源
/* global window, document, AbortController, setTimeout, clearTimeout */
;(function () {
  const V3 = window.V3
  const { esc, num, stamp, dash, nodata, replaceWith } = V3

  const DOMAIN_LABELS = {
    data: '行情与基础数据',
    alpha: '因子与 Alpha 研究',
    ml: '机器学习与回测算力',
    risk: '风控计算与校验',
    execution: '交易执行与 OMS 查询',
    ecosystem: '治理与生态协作',
  }
  const SCHEMA = 'v3'
  //: 三个统计口径来自 /api/v3/metrics（进程内计数），逐字标注，避免被读成生产指标
  const COUNT_NOTE = '本服务进程内 /api/wb/* 与 MCP 同一 handle 的调用计数'
  const n = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0)

  const state = { catalog: null, total: 0, query: '' }

  // ── 顶栏（设计稿是写死的演示值：改绑 /api/v3/overview + /api/v3/metrics）────────
  function renderTop(overview, metrics) {
    const chips = document.querySelectorAll('.top-mid .chip')
    const mode = String(overview.mode ?? '—').toUpperCase()
    if (chips[0]) {
      chips[0].textContent = mode
      chips[0].title = `当前交易模式：${mode}（/api/v3/overview）· 切换 LIVE 需工作台 Web 口令`
    }
    const equity = overview.equity ?? {}
    const points = Array.isArray(equity.points) ? equity.points : []
    const last = points.at(-1) ?? null
    const prev = points.at(-2) ?? null
    const daily = last && prev && n(prev.equity) ? (n(last.equity) / n(prev.equity) - 1) * 100 : null
    if (chips[1]) chips[1].innerHTML = `权益 <b class="num">${esc(V3.money(equity.current))}</b>`
    if (chips[2]) {
      chips[2].innerHTML = `日内 <b class="num${daily === null ? '' : daily >= 0 ? ' up' : ' down'}">`
        + `${esc(daily === null ? '—' : V3.signed(daily, 2, '%'))}</b>`
      chips[2].title = daily === null ? `台账只有 ${points.length} 个点位，无法算日内涨跌` : '按台账最新两个点位计算'
    }
    const names = ['MCP', 'SDK', 'Headless']
    for (let index = 3; index < 6; index++) {
      const chip = chips[index]
      const name = names[index - 3]
      if (!chip || !name) continue
      const mounted = name === 'MCP'
      const value = mounted ? `${n(metrics.mcp?.avgMs)}ms` : '未挂载'
      chip.innerHTML = `<i class="dot ${mounted ? 'g' : 'r'}"></i>${esc(name)} <b class="num">${esc(value)}</b>`
      chip.title = mounted ? `${name} 平均延迟（/api/v3/metrics，无 P95 口径）` : `${name}：本服务未挂载该通道`
    }
    const right = document.querySelector('.top-right')
    if (right) right.textContent = V3.stamp(metrics.generated_at)
  }

  // ── 顶部总览条 ─────────────────────────────────────────────────────────────
  function renderOverview(tools, metrics) {
    const metrics0 = document.querySelectorAll('.overview .metric .v')
    const calls = n(metrics.mcp?.calls)
    const errors = n(metrics.mcp?.errors)
    const values = [
      tools.total ?? state.total ?? '—',
      Object.keys(tools.domains ?? {}).length || '—',
      tools.total ? `${tools.total} / ${tools.total}` : '—',
      calls,
      calls > 0 ? `${num((errors / calls) * 100, 1)}%` : '—',
      SCHEMA,
    ]
    metrics0.forEach((node, index) => {
      if (index >= values.length) return
      node.textContent = String(values[index])
      node.className = `v num${index === 2 ? ' g' : index === 4 ? ' a' : ''}`
    })
    const labels = document.querySelectorAll('.overview .metric .k')
    if (labels[2]) { labels[2].textContent = '可发现工具'; labels[2].title = '可发现工具 = 目录内工具总数（工具面未标注并发安全性）' }
    if (labels[4]) labels[4].title = COUNT_NOTE
    const input = document.getElementById('tool-search')
    if (input) input.placeholder = '搜索工具名 / 职责 / 域'
  }

  // ── 六大工具域目录 ─────────────────────────────────────────────────────────
  function toolRow(tool, callsBy) {
    const row = document.createElement('div')
    row.className = 'tool-row'
    row.dataset.name = String(tool.name ?? '').toLowerCase()
    // 说明文本不落进 data-* 属性：工具描述里本身可能带「示例」字样（如 option_screen 的参数说明），
    // 属性值虽不可见但会被 DOM 文本审计命中；过滤时直接读 .t-desc 的 textContent。

    const kind = String(tool.kind ?? '')
    const badge = kind === 'local'
      ? '<span class="badge-safe" style="color:var(--purple);border-color:rgba(163,113,247,.35);background:rgba(163,113,247,.09)">本地计算</span>'
      : '<span class="badge-safe">工作台直通</span>'
    const target = tool.wb ? ` ↔ ${tool.wb}` : ' · 无工作台对应'
    row.innerHTML = `<div class="t-line"><code class="t-name">${esc(tool.name)}</code>`
      + `<span class="t-desc">${esc(tool.desc ?? '—')}</span></div>`
      + `<div class="t-meta">${badge}<span>kind <b>${esc(kind || '—')}</b></span>`
      + `<span title="该工具在本服务进程内的调用次数">今日 <b class="num">${esc(callsBy[tool.name] ?? 0)}</b></span>`
      + `<span class="t-schema num">${esc(target)}</span></div>`
    return row
  }

  function renderDomains(tools, metrics) {
    const grid = document.querySelector('.domain-grid')
    if (!grid) return
    const domains = tools.domains ?? {}
    const callsBy = metrics.mcp?.tools ?? {}
    const keys = Object.keys(domains)
    state.catalog = domains
    state.total = tools.total ?? 0
    grid.innerHTML = ''
    for (const key of keys) {
      const rows = Array.isArray(domains[key]) ? domains[key] : []
      const domainCalls = rows.reduce((sum, tool) => sum + n(callsBy[tool.name]), 0)
      const card = document.createElement('div')
      card.className = 'domain-card'
      card.dataset.domain = key
      const head = `<div class="d-head"><div><div class="d-key">${esc(key)}</div>`
        + `<div class="d-cn">${esc(DOMAIN_LABELS[key] ?? '工具域')}</div></div>`
        + `<div class="d-stat"><span class="num">${rows.length}</span> 个工具 · 今日 <span class="num">${domainCalls}</span>`
        + `<br><span class="d-show">全量展示 ${rows.length} / ${rows.length}</span></div></div>`
      card.innerHTML = head
      if (rows.length === 0) {
        const empty = document.createElement('div')
        empty.className = 'tool-row'
        empty.style.color = 'var(--faint)'
        empty.textContent = '该域暂无工具'
        card.appendChild(empty)
      } else {
        for (const tool of rows) card.appendChild(toolRow(tool, callsBy))
      }
      grid.appendChild(card)
    }
    const counter = document.getElementById('search-count')
    if (counter) {
      counter.textContent = `完整清单 ${tools.total ?? 0} 个工具 · ${keys.length} 个域 · Schema ${SCHEMA} · 今日调用口径：${COUNT_NOTE}`
    }
    const sub = document.querySelector('[data-od-id="domain-catalog"] .sec-head .sub')
    if (sub) {
      sub.title = '每域「今日」只累加该域工具名的调用；schedule/sources/positions 等会话级工具的调用计入总览「今日调用」，故各域之和通常小于总数'
    }
  }

  // ── 搜索 / 同步（重绑：设计稿脚本里的「示例子集」计数不再使用）────────────────
  function rebindControls(reload) {
    const input = document.getElementById('tool-search')
    if (input && input.dataset.bound !== 'v3') {
      const clone = input.cloneNode(true)
      input.replaceWith(clone)
      clone.dataset.bound = 'v3'
      clone.addEventListener('input', () => { state.query = clone.value.trim().toLowerCase(); applyFilter() })
    }
    const button = document.getElementById('sync-btn')
    if (button && button.dataset.bound !== 'v3') {
      const clone = button.cloneNode(true)
      button.replaceWith(clone)
      clone.dataset.bound = 'v3'
      clone.addEventListener('click', async () => {
        clone.disabled = true
        clone.textContent = '同步中…'
        await reload()
        clone.textContent = `已同步 ${V3.hhmmss(new Date().toISOString())}`
        clone.classList.add('done')
        setTimeout(() => { clone.textContent = '同步工具清单'; clone.classList.remove('done'); clone.disabled = false }, 2400)
      })
    }
  }

  function applyFilter() {
    const query = state.query
    let shown = 0
    for (const card of document.querySelectorAll('.domain-card')) {
      let hits = 0
      for (const row of card.querySelectorAll('.tool-row')) {
        const desc = (row.querySelector('.t-desc')?.textContent ?? '').toLowerCase()
        const hit = !query || (row.dataset.name ?? '').includes(query) || desc.includes(query)
        row.style.display = hit ? '' : 'none'
        if (hit) hits++
      }
      card.style.display = hits ? '' : 'none'
      shown += hits
    }
    const counter = document.getElementById('search-count')
    if (counter) {
      counter.textContent = query
        ? `匹配 ${shown} 个工具（来自 /api/v3/tools 的真实清单）`
        : `完整清单 ${state.total} 个工具 · Schema ${SCHEMA} · 今日调用口径：${COUNT_NOTE}`
    }
  }

  // ── 工具发现代理：两段 codeblock 用真实数据 ─────────────────────────────────
  function jsonCap(value, limit = 1100) {
    const text = JSON.stringify(value, null, 2)
    return text.length > limit ? `${text.slice(0, limit)}\n… （响应已截断）` : text
  }

  function renderDiscovery(tools, metrics, live, liveError) {
    const blocks = document.querySelectorAll('[data-od-id="tool-discovery-proxy"] .codeblock')
    if (blocks.length === 0) return
    const domains = tools.domains ?? {}
    // 选调用最多的域，保证示例是「真跑过」的域
    const callsBy = metrics.mcp?.tools ?? {}
    const keys = Object.keys(domains)
    let pick = keys[0]
    let best = -1
    for (const key of keys) {
      const rows = Array.isArray(domains[key]) ? domains[key] : []
      const total = rows.reduce((sum, tool) => sum + n(callsBy[tool.name]), 0)
      if (total > best) { best = total; pick = key }
    }
    const rows = (Array.isArray(domains[pick]) ? domains[pick] : []).slice(0, 8)
    const payload = {
      domain: pick,
      tool_count: (domains[pick] ?? []).length,
      calls_today: best,
      schema: SCHEMA,
      tools: rows.map((tool) => ({ name: tool.name, kind: tool.kind, calls: n(callsBy[tool.name]) })),
    }
    const title0 = blocks[0].querySelector('.code-title')
    if (title0) {
      title0.innerHTML = `<span>list_tools(domain="${esc(pick)}")</span>`
        + `<span class="meta">清单来自 /api/v3/tools · 次数来自 /api/v3/metrics（进程内计数）</span>`
    }
    const pre0 = blocks[0].querySelector('pre')
    if (pre0) pre0.textContent = jsonCap(payload)

    const title1 = blocks[1].querySelector('.code-title')
    const pre1 = blocks[1].querySelector('pre')
    if (live && live.ok) {
      const tool = live.tool
      const elapsed = live.elapsedMs
      if (title1) {
        title1.innerHTML = `<span>call_tool(name="${esc(tool)}", args={ticker:"SH.600519", period:"1d"})</span>`
          + `<span class="meta">真实调用 · ${esc(elapsed)}ms · via ${esc(dash(live.source))}</span>`
      }
      if (pre1) {
        const body = { ...live.body }
        if (body.bars) body.bars = body.bars.slice(0, 2)
        pre1.textContent = `→ 200 OK（/api/v3/${tool}，本页发起）\n${jsonCap(body, 1200)}`
      }
    } else {
      if (title1) title1.innerHTML = '<span>call_tool(...)</span><span class="meta">无数据源</span>'
      if (pre1) {
        pre1.textContent = `无数据源：本页发起真实调用未在超时内返回\n`
          + `原因：${String(liveError?.message ?? '请求超时（首次调用含上游模块冷启动）')}\n`
          + '（不展示编造的响应体；重试可点右上「同步工具清单」）'
      }
    }
    const note = document.querySelector('[data-od-id="tool-discovery-proxy"] .note')
    if (note) {
      note.innerHTML = 'MCP 服务器只暴露 <code>list_tools</code> / <code>call_tool</code> 两个入口，'
        + '避免上百个工具 schema 撑爆上下文窗口；Harness 先发现、再按名调用。'
        + `目录当前 <b class="num">${tools.total ?? 0}</b> 个工具 · Schema <code>${SCHEMA}</code>。`
    }
  }

  // ── 平台 MCP 服务器注册 ────────────────────────────────────────────────────
  function renderRegistry(settings, gateway) {
    const section = document.querySelector('[data-od-id="mcp-server-registry"]')
    if (!section) return
    const sub = section.querySelector('.card-head .sub')
    if (sub) {
      sub.textContent = `MCP 协议：${gateway.channels?.mcp?.protocol ?? '—'}`
      sub.title = '来自 /api/v3/gateway 的 channels.mcp.protocol'
    }
    const kv = section.querySelector('.kv')
    // 设计稿这一块是「服务进程」详情（PID/启动时间/帧格式…）：工具面没有这些字段 → 整列标无数据源
    const columns = section.querySelectorAll('.duo > div')
    if (columns[0]) {
      replaceWith(columns[0], '<h4 class="h-title">服务进程</h4>'
        + '<div style="border:1px dashed var(--border2);border-radius:8px;padding:10px 12px;font-size:11.5px;color:var(--faint);line-height:1.7">'
        + '<b style="color:var(--muted)">服务进程信息：无数据源</b><br>'
        + '工具面不返回进程 PID / 启动时间 / 帧格式；本服务以 <code>'
        + `${esc(gateway.channels?.mcp?.protocol ?? 'MCP streamable-http')}</code> 暴露工具，`
        + `已注册 <b class="num">${esc(gateway.channels?.mcp?.tools ?? '—')}</b> 个工具（channels.mcp.tools）。</div>`)
    }
    if (columns[1]) {
      const stateLine = columns[1].querySelector('.state-line')
      const status = String(gateway.channels?.mcp?.status ?? '')
      if (stateLine) {
        stateLine.innerHTML = `<i class="dot ${status === 'running' ? 'g' : 'r'}"></i>`
          + `MCP 通道状态：<b style="color:${status === 'running' ? 'var(--green)' : 'var(--red)'}">${esc(dash(status))}</b>`
          + '<span class="sub-inline">· 来自 /api/v3/gateway</span>'
      }
      const label = columns[1].querySelector('.update-label')
      if (label) label.textContent = '最近一次 update 分段耗时：无数据源（状态机只有 start/stop/update 动作，无耗时遥测）'
      const timeline = columns[1].querySelector('.timeline')
      if (timeline) timeline.remove()
      const legend = columns[1].querySelector('.legend')
      if (legend) legend.innerHTML = '<span style="color:var(--faint)">无数据源 · 断开旧 / listTools / 注销旧 / 注册新 四段耗时未采集</span>'
    }
    const chips = section.querySelector('.src-chips')
    if (chips) {
      const available = (settings.data_sources ?? []).filter((item) => item.available).map((item) => item.name)
      const names = available.length ? available : ['（/api/v3/settings 未返回可用数据源）']
      chips.innerHTML = names.map((name) => `<span class="mono" title="${esc(name)}">${esc(String(name).split('（')[0])}</span>`).join('')
    }
  }

  // ── 健康与降级 ─────────────────────────────────────────────────────────────
  function renderHealth(metrics) {
    const section = document.querySelector('[data-od-id="health-degradation"]')
    if (!section) return
    const calls = n(metrics.mcp?.calls)
    const errors = n(metrics.mcp?.errors)
    const overall = calls > 0 ? (errors / calls) * 100 : null
    const sub = section.querySelector('.card-head .sub')
    if (sub) sub.textContent = `本服务进程内计数 · ${COUNT_NOTE}`
    const cols = section.querySelectorAll('.h-col')
    // 左列：单工具失败率无数据源（计数只有 errors 总数，未按工具拆分）
    if (cols[0]) {
      const title = cols[0].querySelector('h4')
      if (title) {
        title.innerHTML = `调用失败率 Top 5 <span class="sub-inline">（整体 ${overall === null ? '—' : num(overall, 1) + '%'}）</span>`
        title.title = overall === null ? '无调用记录' : `进程内 ${calls} 次调用 / ${errors} 次错误`
      }
      const rows = cols[0].querySelectorAll('.bar-row')
      rows.forEach((row, index) => {
        if (index > 0) { row.remove(); return }
        replaceWith(row, '<div style="border:1px dashed var(--border2);border-radius:8px;padding:10px 12px;font-size:11.5px;color:var(--faint);line-height:1.7">'
          + `<b style="color:var(--muted)">单工具失败率：无数据源</b><br>metrics 只提供 errors 总数`
          + `（${errors} / ${calls}）与按工具的调用次数，未按工具拆分错误数，故无法排序 Top 5。</div>`)
      })
    }
    // 右列：单工具 P95 延迟无数据源（只有整块 avgMs）
    if (cols[1]) {
      const title = cols[1].querySelector('h4')
      if (title) title.innerHTML = `平均延迟 Top 5 <span class="sub-inline">（整体 ${n(metrics.mcp?.avgMs)}ms · 无 P95 口径）</span>`
      const rows = cols[1].querySelectorAll('.bar-row')
      rows.forEach((row, index) => {
        if (index > 0) { row.remove(); return }
        replaceWith(row, '<div style="border:1px dashed var(--border2);border-radius:8px;padding:10px 12px;font-size:11.5px;color:var(--faint);line-height:1.7">'
          + `<b style="color:var(--muted)">单工具延迟：无数据源</b><br>metrics 只有全部调用的平均耗时`
          + `（avgMs=${n(metrics.mcp?.avgMs)}，样本 ${calls} 次），未采集单工具 P95。</div>`)
      })
    }
    const compat = section.querySelector('.compat')
    if (compat) {
      compat.innerHTML = '<b>降级与兼容性</b>'
        + `<span>调用失败率 ${overall === null ? '—' : num(overall, 1) + '%'}（${errors}/${calls}）</span>`
        + `<span>平均耗时 <b class="num">${n(metrics.mcp?.avgMs)}ms</b></span>`
        + `<span>workbench ${metrics.workbenchUp ? '<b class="ok">在线</b>' : '<b style="color:var(--red)">不可达</b>'}</span>`
        + '<span>降级预案：连不上时工具静默降级为不可用，不影响其余能力</span>'
    }
  }

  // ── 页脚 ───────────────────────────────────────────────────────────────────
  function renderFooter(tools, metrics) {
    const foot = document.querySelector('.page-foot')
    if (!foot) return
    const spans = foot.querySelectorAll('span')
    if (spans[0]) spans[0].innerHTML = `工具域治理 · 数据来源 <code style="font-family:var(--mono)">/api/v3/tools</code> + <code style="font-family:var(--mono)">/api/v3/metrics</code>`
    if (spans[1]) spans[1].textContent = `${tools.total ?? 0} 个工具 · ${Object.keys(tools.domains ?? {}).length} 个域 · 数据截至 ${stamp(metrics.generated_at)} · 工具面未提供的数据源一律显式标注`
    const contract = document.querySelector('[data-od-id="tool-contract-note"]')
    if (contract) {
      const chip = contract.querySelector('.chip.demo')
      if (chip) { chip.className = 'chip'; chip.textContent = '契约说明' }
      const note = contract.querySelector('p')
      if (note) {
        note.innerHTML = '每个工具的契约（参数 / 输出 / 对齐规则）会注入系统提示词，因此工具数量需控制在合理范围'
          + ` —— 当前目录 <b class="num">${tools.total ?? 0}</b> 个工具，`
          + '长尾需求经 <code>call_tool</code> 间接调用承接。'
      }
    }
  }

  // ── 一次真实工具调用（失败只影响该 codeblock）──────────────────────────────
  async function probeSeries() {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 15000)
    const started = Date.now()
    try {
      const envelope = await V3.wb('series', { ticker: 'SH.600519', period: '1d', limit: 20 }, { signal: controller.signal })
      if (!envelope.ok) return { error: envelope.error ?? { message: '工具返回 ok=false' } }
      const value = envelope.value ?? {}
      return {
        ok: true,
        tool: 'series',
        elapsedMs: Date.now() - started,
        source: value.source,
        body: { ok: true, tool: 'series', args: { ticker: 'SH.600519', period: '1d', limit: 20 }, elapsed_ms: Date.now() - started, data: value },
      }
    } catch (error) {
      return { error: { message: String((error && error.message) || error) } }
    } finally {
      clearTimeout(timer)
    }
  }

  async function load() {
    const [tools, metrics, settings, gateway, overview] = await Promise.all([
      V3.api('tools'), V3.api('metrics'), V3.api('settings'), V3.api('gateway'), V3.api('overview'),
    ])
    if (tools.ok) {
      renderOverview(tools, metrics.ok ? metrics : {})
      renderDomains(tools, metrics.ok ? metrics : {})
      applyFilter()
    } else {
      const grid = document.querySelector('.domain-grid')
      if (grid) nodata(grid, '六大工具域目录', tools.error?.message)
      const counter = document.getElementById('search-count')
      if (counter) counter.textContent = `目录取数失败：${tools.error?.message ?? '未知错误'}（/api/v3/tools）`
    }
    if (metrics.ok && overview.ok) renderTop(overview, metrics)
    if (metrics.ok) renderHealth(metrics)
    if (settings.ok) {
      renderRegistry(settings, gateway.ok ? gateway : {})
      renderFooter(tools.ok ? tools : { domains: {} }, metrics.ok ? metrics : {})
    }
    const live = await probeSeries()
    renderDiscovery(tools.ok ? tools : { domains: {} }, metrics.ok ? metrics : {}, live, live.error)
    rebindControls(load)
    V3.demoSweep(document.body)
  }

  load().catch((error) => {
    const main = document.querySelector('.content')
    if (main) nodata(main, '工具域治理', String(error?.message ?? error))
  })
})()
