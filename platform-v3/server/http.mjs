// 极简路由器（零依赖）：路径参数 :name、JSON body、JSON 响应。
export function createApp() {
  const routes = []

  function add(method, pattern, handler) {
    routes.push({ method, parts: pattern.split('/').filter(Boolean), handler })
  }

  function match(method, pathname) {
    const parts = pathname.split('/').filter(Boolean)
    for (const route of routes) {
      if (route.method !== method || route.parts.length !== parts.length) continue
      const params = {}
      let ok = true
      for (let i = 0; i < parts.length; i++) {
        const part = route.parts[i]
        if (part.startsWith(':')) params[part.slice(1)] = decodeURIComponent(parts[i])
        else if (part !== parts[i]) {
          ok = false
          break
        }
      }
      if (ok) return { handler: route.handler, params }
    }
    return null
  }

  function readBody(req, limit = 2 * 1024 * 1024) {
    return new Promise((resolve, reject) => {
      let size = 0
      const chunks = []
      req.on('data', (chunk) => {
        size += chunk.length
        if (size > limit) {
          reject(new Error('body too large'))
          req.destroy()
          return
        }
        chunks.push(chunk)
      })
      req.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8')
        if (text === '') return resolve({})
        try {
          resolve(JSON.parse(text))
        } catch {
          reject(new Error('invalid json body'))
        }
      })
      req.on('error', reject)
    })
  }

  async function handle(req, res) {
    const url = new URL(req.url, 'http://localhost')
    const found = match(req.method, url.pathname)
    if (!found) {
      res.writeHead(404, { 'content-type': 'application/json; charset=utf-8' })
      res.end(JSON.stringify({ ok: false, error: { code: 'not-found', message: url.pathname } }))
      return
    }
    try {
      const body = req.method === 'POST' || req.method === 'PUT' ? await readBody(req) : {}
      const query = Object.fromEntries(url.searchParams.entries())
      const result = await found.handler({ params: found.params, query, body, req, res })
      if (result === undefined) return // handler 已自行写响应
      res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
      res.end(JSON.stringify(result))
    } catch (error) {
      res.writeHead(400, { 'content-type': 'application/json; charset=utf-8' })
      res.end(JSON.stringify({ ok: false, error: { code: 'bad-request', message: error?.message ?? String(error) } }))
    }
  }

  return { get: (p, h) => add('GET', p, h), post: (p, h) => add('POST', p, h), handle }
}

export default createApp
