// 既有工作台（8397）客户端：全部数据与受约束交易入口都从这里来。
// 契约：POST /api/wb/{tool}，JSON envelope { ok: true, value } | { ok: false, error }。
// 本客户端不做重试（上游语义是幂等读为主，写类必须显式重试），只做超时与错误归一。
export function createWorkbenchClient({ base, timeoutMs = 30000, fetchImpl = fetch, onCall } = {}) {
  async function call(tool, args = {}, options = {}) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? timeoutMs)
    const startedAt = Date.now()
    const report = (ok) => {
      try {
        onCall?.({ tool, ok, ms: Date.now() - startedAt })
      } catch {
        // 观测失败不影响业务
      }
    }
    try {
      const res = await fetchImpl(`${base}/api/wb/${encodeURIComponent(tool)}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(args ?? {}),
        signal: controller.signal,
      })
      if (!res.ok) {
        report(false)
        return { ok: false, error: { code: 'wb/http-' + res.status, message: `workbench ${tool} http ${res.status}` } }
      }
      const envelope = await res.json()
      report(envelope?.ok === true)
      return envelope
    } catch (error) {
      const reason = error?.cause?.code || error?.name || 'unknown'
      report(false)
      return { ok: false, error: { code: 'wb/unreachable', message: `workbench ${tool} failed: ${reason}` } }
    } finally {
      clearTimeout(timer)
    }
  }

  async function health() {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 5000)
    try {
      const res = await fetchImpl(`${base}/healthz`, { signal: controller.signal })
      return { ok: res.ok, status: res.status }
    } catch {
      return { ok: false, status: 0 }
    } finally {
      clearTimeout(timer)
    }
  }

  return { call, health, base }
}

export default createWorkbenchClient
