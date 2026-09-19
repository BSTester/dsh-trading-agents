// SDK JSON-RPC 通道客户端（FR-GATEWAY-002 / §7.1 人工研究数据流）。
// 线协议：换行分帧 JSON-RPC 2.0 over stdio（@deepseek-ai/dsh-sdk-protocol）。
//   client→server: initialize{cwd,provider,model,reasoningEffort?,maxTokens?} / session/prompt{sessionId,contentBlocks} / shutdown
//   server→client: session.event / session.status / subagent.started / subagent.finished
//   握手 serverInfo.name === 'deepseek-harness-sdk-runtime'；握手未成功前 session/prompt 会被服务端拒绝。
// 运行时由 `dsh --profile <sdkProfile>` 启动（dsh-sdk-app bundle）；stdout 只允许 JSON-RPC 帧。
import { spawn } from 'node:child_process'

export function createSdkChannel({ config, spawnImpl = spawn, clock = () => new Date() }) {
  const sdk = config.channels.sdk
  const state = {
    status: sdk.enabled ? 'pending' : 'disabled',
    reason: sdk.enabled ? '尚未启动握手' : 'QUANT_SDK_ENABLED=0',
    serverInfo: null,
    child: null,
    initialized: false,
    lastError: null,
    pending: new Map(),
  }
  const events = []
  const pendingBySession = new Map()
  let buffer = ''

  function keyPresence() {
    return Boolean(process.env.DEEPSEEK_API_KEY || process.env.ZAI_CODING_CN_API_KEY || process.env.ANTHROPIC_API_KEY)
  }

  // 从事件流里提取「最近一轮」的结论：turn/end 的 reason（成功=completed，失败=错误码+原文）
  function lastTurn() {
    for (let i = events.length - 1; i >= 0; i--) {
      const event = events[i]?.params?.event
      if (!event || event.type !== 'turn/end') continue
      const reason = event.data?.reason ?? {}
      if (reason.kind === 'error') {
        const failure = reason.error ?? reason.failure ?? {}
        return { kind: 'error', code: failure.code ?? null, message: failure.message ?? null, at: events[i].at }
      }
      return { kind: reason.kind ?? 'completed', at: events[i].at }
    }
    return null
  }

  function status() {
    return {
      status: state.status,
      reason: state.reason,
      initialized: state.initialized,
      serverInfo: state.serverInfo,
      profile: sdk.profile,
      route: { provider: sdk.provider, model: sdk.model, reasoningEffort: sdk.reasoningEffort, maxTokens: sdk.maxTokens },
      credentials: { modelKeyPresent: keyPresence() },
      sessionWarning: keyPresence() ? null : '未检测到模型密钥（DEEPSEEK_API_KEY / ZAI_CODING_CN_API_KEY）：握手可用，但 session/prompt 会在模型调用处返回 MISSING_CREDENTIAL',
      lastTurn: lastTurn(),
      events: events.slice(-20),
    }
  }

  function send(child, message) {
    child.stdin.write(JSON.stringify(message) + '\n')
  }

  function request(child, id, method, params, timeoutMs = 60000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        state.pending.delete(id)
        reject(new Error(`sdk timeout after ${timeoutMs}ms: ${method}`))
      }, timeoutMs)
      state.pending.set(id, { resolve, reject, timer })
      send(child, { jsonrpc: '2.0', id, method, params })
    })
  }

  function handleFrame(frame) {
    if (frame.id !== undefined && frame.method === undefined) {
      const pending = state.pending.get(frame.id)
      if (!pending) return
      state.pending.delete(frame.id)
      clearTimeout(pending.timer)
      if (frame.error) pending.reject(new Error(`sdk error ${frame.error.code ?? ''}: ${frame.error.message ?? ''}`))
      else pending.resolve(frame.result)
      return
    }
    if (frame.method) {
      events.push({ at: clock().toISOString(), method: frame.method, params: frame.params })
      if (events.length > 200) events.splice(0, events.length - 200)
    }
  }

  async function start() {
    if (!sdk.enabled) return { ok: false, error: { code: 'sdk/disabled', message: state.reason } }
    if (state.initialized) return { ok: true }
    if (state.child) return { ok: false, error: { code: 'sdk/starting', message: '握手进行中' } }
    if (sdk.requireKey === true && !keyPresence()) {
      state.reason = '缺少模型密钥环境变量（DEEPSEEK_API_KEY / ZAI_CODING_CN_API_KEY）'
      return { ok: false, error: { code: 'sdk/no-credentials', message: state.reason } }
    }
    state.child = spawnImpl(sdk.dshBin || config.channels.headless.dshBin, Array.isArray(sdk.argv) ? sdk.argv : ['--profile', sdk.profile], {
      env: { ...process.env, DSH_HOME: sdk.home || config.home },
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    const child = state.child
    child.stderr.on('data', (chunk) => {
      if (events.length < 500) events.push({ at: clock().toISOString(), method: 'runtime.stderr', text: chunk.toString('utf8').slice(0, 400) })
    })
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => {
      buffer += chunk
      let index
      while ((index = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, index)
        buffer = buffer.slice(index + 1)
        if (line.trim() === '') continue
        try {
          handleFrame(JSON.parse(line))
        } catch {
          // 非法帧按协议忽略
        }
      }
    })
    child.on('close', (code) => {
      state.status = 'exited'
      state.reason = `运行时退出（code=${code}）`
      state.initialized = false
      state.child = null
    })
    try {
      const result = await request(child, 'init', 'initialize', {
        cwd: sdk.cwd,
        provider: sdk.provider,
        model: sdk.model,
        reasoningEffort: sdk.reasoningEffort,
        maxTokens: sdk.maxTokens,
      })
      state.serverInfo = result?.serverInfo ?? null
      const runtimeName = state.serverInfo?.name
      if (runtimeName !== 'deepseek-harness-sdk-runtime') {
        throw new Error(`unexpected serverInfo.name: ${String(runtimeName)}`)
      }
      state.initialized = true
      state.status = 'ready'
      state.reason = null
      return { ok: true, serverInfo: state.serverInfo }
    } catch (error) {
      state.status = 'pending'
      state.reason = String(error?.message ?? error)
      state.lastError = state.reason
      try {
        child.kill('SIGTERM')
      } catch {
        // 已退出
      }
      state.child = null
      return { ok: false, error: { code: 'sdk/handshake-failed', message: state.reason } }
    }
  }

  async function prompt(sessionId, text) {
    if (!state.initialized || !state.child) {
      const started = await start()
      if (!started.ok) return started
    }
    const receipt = await request(state.child, `prompt-${clock().getTime()}`, 'session/prompt', {
      sessionId,
      contentBlocks: [{ type: 'text', text }],
    })
    return { ok: true, messageId: receipt?.messageId, sessionId }
  }

  async function stop() {
    if (state.child && state.initialized) {
      try {
        await request(state.child, 'shutdown', 'shutdown', {})
      } catch {
        // 运行时可能已直接退出
      }
    }
    try {
      state.child?.kill('SIGTERM')
    } catch {
      // 已退出
    }
  }

  return { start, prompt, stop, status, events }
}

export default createSdkChannel
