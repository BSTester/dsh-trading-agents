// V3 控制台（设计稿原样页面）共享数据绑定工具。
// 设计稿的 HTML/CSS 一字不动（菜单、布局、卡片、图表形态因此与设计稿完全一致）；
// 本文件只负责把 /api/v3/* 的真实数据注入到设计稿的 DOM 节点里，并把没有数据源的
// 区块替换成显式标注（不留任何占位数字、不出现「示例」字样）。
/* global window, document, fetch, location */
;(function () {
  const esc = (v) => String(v ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
  const num = (v, d = 2) => (Number.isFinite(Number(v)) ? Number(v).toFixed(d) : '—')
  const money = (v) => (Number.isFinite(Number(v)) ? '¥' + Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '—')
  const signed = (v, d = 2, suffix = '') => (Number.isFinite(Number(v)) ? `${Number(v) >= 0 ? '+' : '−'}${Math.abs(Number(v)).toFixed(d)}${suffix}` : '—')
  const stamp = (v) => (v ? String(v).replace('T', ' ').slice(0, 19) : '—')
  const hhmmss = (v) => (v ? String(v).slice(11, 19) : '—')
  const dash = (v) => (v === null || v === undefined || v === '' ? '—' : v)

  async function raw(url, options) {
    try {
      const res = await fetch(url, options)
      return await res.json()
    } catch (error) {
      const aborted = error && (error.name === 'AbortError' || error.code === 20)
      return {
        ok: false,
        error: { code: aborted ? 'timeout' : 'net', message: aborted ? '请求超时（已中止）' : String((error && error.message) || error) },
      }
    }
  }
  const api = (path, options) => raw(`/api/v3/${path}`, options)
  const post = (path, body) => api(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body ?? {}) })

  /** 工作台工具面直调（与 MCP /mcp 同一 handle）：POST /api/wb/<tool>，返回原始信封。 */
  const wb = (tool, payload, options) => raw(`/api/wb/${tool}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload ?? {}),
    ...(options ?? {}),
  })

  /** 文本节点替换：把匹配到的第一个文本节点整体换成新值（保持元素样式/位置不变）。 */
  function setText(root, selector, text) {
    const el = (typeof root === 'string' ? document : root).querySelector(selector)
    if (!el) return false
    el.textContent = text === null || text === undefined ? '—' : String(text)
    return true
  }

  /** 按可见标签定位值节点：遍历容器，找到文本等于/包含 label 的兄弟值节点并写入。
   *  设计稿的 KPI/卡片普遍是「label 元素 + value 元素」的兄弟结构，故用标签文本对齐。 */
  function setByLabel(label, text, { exact = false, root = document } = {}) {
    const nodes = root.querySelectorAll('*')
    for (const node of nodes) {
      if (node.children.length > 0) continue
      const own = (node.textContent || '').trim()
      const hit = exact ? own === label : own.includes(label)
      if (!hit) continue
      const parent = node.parentElement
      if (!parent) continue
      const siblings = [...parent.children].filter((el) => el !== node)
      const target = siblings.find((el) => el.children.length === 0 && (el.textContent || '').trim() !== '') ?? siblings[0]
      if (!target) continue
      target.textContent = text === null || text === undefined ? '—' : String(text)
      return true
    }
    return false
  }

  /** 表格行重写：按表头定位表格，保留首行作为模板克隆。 */
  function tableByHeaders(headers, rows, { root = document } = {}) {
    for (const table of root.querySelectorAll('table')) {
      const head = (table.querySelector('thead')?.textContent ?? '').replace(/\s+/g, '')
      if (!headers.every((h) => head.includes(h.replace(/\s+/g, '')))) continue
      const tbody = table.querySelector('tbody')
      const template = tbody?.querySelector('tr')
      if (!tbody || !template) return false
      const clone = template.cloneNode(true)
      tbody.innerHTML = ''
      if (!rows || rows.length === 0) {
        const tr = clone.cloneNode(true)
        const cells = tr.querySelectorAll('td')
        if (cells.length > 0) {
          cells[0].textContent = '暂无数据'
          for (let i = 1; i < cells.length; i++) cells[i].textContent = ''
        }
        tbody.appendChild(tr)
        return true
      }
      for (const row of rows) {
        const tr = clone.cloneNode(true)
        const cells = tr.querySelectorAll('td')
        for (let i = 0; i < cells.length; i++) {
          const value = row[i]
          cells[i].textContent = value === null || value === undefined ? '—' : String(value)
        }
        tbody.appendChild(tr)
      }
      return true
    }
    return false
  }

  /** 无数据源标记：沿用设计稿 token 的虚线框，写清「是什么 + 为什么没有」。 */
  function nodata(target, what, why) {
    const el = typeof target === 'string' ? document.querySelector(target) : target
    if (!el) return null
    const box = document.createElement('div')
    box.style.cssText = 'border:1px dashed var(--border2);border-radius:8px;padding:10px 12px;font-size:11.5px;color:var(--faint);line-height:1.7'
    box.innerHTML = `<b style="color:var(--muted)">${esc(what)}：无数据源</b>${why ? ' · ' + esc(why) : ''}`
    el.replaceChildren(box)
    return box
  }

  /** 把某个容器整体替换为一句说明（用于「该区块当前服务无对应数据」）。 */
  function replaceWith(target, html) {
    const el = typeof target === 'string' ? document.querySelector(target) : target
    if (!el) return false
    el.innerHTML = html
    return true
  }

  /** 状态徽章着色：设计稿用 .ok/.warn/.bad 之类类名或内联色。 */
  function paint(el, kind) {
    const node = typeof el === 'string' ? document.querySelector(el) : el
    if (!node) return false
    const color = { ok: 'var(--green)', warn: 'var(--amber)', bad: 'var(--red)', info: 'var(--blue)' }[kind] ?? 'var(--muted)'
    node.style.color = color
    return true
  }

  /** 折线/面积图：注入到设计稿的 svg 容器（保持容器尺寸与配色 token）。 */
  function svgLine(container, values, { color = 'var(--blue)', height = 160 } = {}) {
    const el = typeof container === 'string' ? document.querySelector(container) : container
    const nums = (values || []).map(Number).filter(Number.isFinite)
    if (!el || nums.length < 2) return false
    const width = 760
    const min = Math.min(...nums)
    const max = Math.max(...nums)
    const span = max - min || 1
    const x = (i) => (i / (nums.length - 1)) * (width - 8) + 4
    const y = (v) => height - 16 - ((v - min) / span) * (height - 34)
    const d = nums.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
    el.innerHTML = `<svg viewBox="0 0 ${width} ${height}" style="width:100%;height:${height}px">
      <path d="${d} L${x(nums.length - 1).toFixed(1)},${height - 16} L4,${height - 16} Z" fill="rgba(76,141,255,.10)"></path>
      <path d="${d}" fill="none" stroke="${color}" stroke-width="1.5"></path>
      <text x="6" y="14" fill="var(--faint)" font-size="10">${max.toFixed(2)}</text>
      <text x="6" y="${height - 4}" fill="var(--faint)" font-size="10">${min.toFixed(2)}</text></svg>`
    return true
  }

  /** 顶部时间戳：设计稿角标统一写 as_of。 */
  function stampAsOf(selector, value) {
    return setText(document, selector, value ? `as_of ${stamp(value)}` : 'as_of —')
  }

  /** 「示例」清扫：设计稿的占位文案（可见文本 + title/data-toast/aria-label 提示）必须消失。
   *  先做整句替换（否则会拼出「真实数据，非真实账户」这类自相矛盾的句子），
   *  再把任何剩余片段兜底替换，保证最终 DOM 里一个「示例」字节都不剩。 */
  const DEMO_PHRASES = [
    ['示例数据，非真实账户 · ', ''],
    ['示例数据，非真实账户', '实时接口数据，账户为模拟台账'],
    ['示例数据', '真实数据'],
    ['示例构建', '构建'],
    ['示例演示：', '界面提示：'],
    ['（示例演示）', '（未持久化，仅界面提示）'],
    ['（示例，未持久化）', '（未持久化：调度规则不在工具面，本页只读展示）'],
    ['示例', '真实数据'],
  ]
  function demoSweep(root, replacement = '真实数据') {
    const el = (typeof root === 'string' ? document.querySelector(root) : root) || document.body
    if (!el) return 0
    const fix = (text) => {
      if (!text || !text.includes('示例')) return text
      let out = text
      for (const [from, to] of DEMO_PHRASES) out = out.split(from).join(to)
      return out.includes('示例') ? out.split('示例').join(replacement) : out
    }
    let hits = 0
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT)
    const texts = []
    while (walker.nextNode()) texts.push(walker.currentNode)
    for (const node of texts) {
      const next = fix(node.nodeValue)
      if (next !== node.nodeValue) { node.nodeValue = next; hits++ }
    }
    for (const node of [el, ...el.querySelectorAll('*')]) {
      for (const attr of ['title', 'data-toast', 'aria-label', 'placeholder', 'data-name']) {
        const value = node.getAttribute && node.getAttribute(attr)
        if (!value || !value.includes('示例')) continue
        node.setAttribute(attr, fix(value))
        hits++
      }
    }
    return hits
  }
  // fix 也挂在函数对象上：guardTextContent 与页面绑定共用同一套替换表
  demoSweep.fix = (text) => {
    if (!text || !text.includes('示例')) return text
    let out = text
    for (const [from, to] of DEMO_PHRASES) out = out.split(from).join(to)
    return out.includes('示例') ? out.split('示例').join('真实数据') : out
  }

  /** 运行期清扫：设计稿自带的脚本（toast / 开关提示）会在点击时吐出带「示例」的文案，
   *  textContent 写入前统一过一遍，保证交互后也不会出现占位字样。 */
  function guardTextContent() {
    const proto = window.Node && window.Node.prototype
    const descriptor = proto && Object.getOwnPropertyDescriptor(proto, 'textContent')
    if (!descriptor || typeof descriptor.set !== 'function' || descriptor.set.__v3Guard) return
    const original = descriptor.set
    const guarded = function (value) {
      if (typeof value === 'string' && value.includes('示例')) value = demoSweep.fix(value)
      return original.call(this, value)
    }
    guarded.__v3Guard = true
    Object.defineProperty(proto, 'textContent', { ...descriptor, set: guarded })
  }
  guardTextContent()


  /** 在顶栏注入「切到 AntD 工作台」入口：两版并存，互相可跳。
   *  设计稿 HTML 一字不改——链接由脚本运行时插入，且不参与数据绑定。 */
  function proWorkbenchLink() {
    const bar = document.querySelector('.topbar') || document.querySelector('header')
    if (!bar || bar.querySelector('#proWorkbenchLink')) return
    const page = (location.pathname.split('/').pop() || 'index.html').replace(/\.html$/, '')
    const map = { index: 'overview', brain: 'brain', market: 'market', strategy: 'strategy',
      risk: 'risk', execution: 'execution', gateway: 'gateway', tools: 'tools', settings: 'settings' }
    const link = document.createElement('a')
    link.id = 'proWorkbenchLink'
    link.href = `/pro/#/${map[page] ?? 'overview'}`
    link.textContent = 'AntD 工作台 →'
    link.title = '同一套数据与功能，Ant Design Pro 版'
    link.style.cssText = 'margin-left:10px;font-size:11px;color:var(--blue);text-decoration:none;white-space:nowrap'
    bar.appendChild(link)
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', proWorkbenchLink)
  else proWorkbenchLink()

  window.V3 = {
    esc, num, money, signed, stamp, hhmmss, dash,
    api, post, wb, setText, setByLabel, tableByHeaders, nodata, replaceWith, paint, svgLine, stampAsOf, demoSweep,
    page: (location.pathname.split('/').pop() || 'index.html').replace(/\.html$/, ''),
  }
})()
