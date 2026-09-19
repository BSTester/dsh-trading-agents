// Headless Runner + 调度器（FR-GATEWAY-003/004）。
// 通道契约：stdout=最后一次非空 assistant 文本；exit 0=completed；1=未完成/失败；130=SIGINT 优雅关闭。
// 外部熔断：并发上限 / 单次超时 / token 预算全部在平台侧强制执行，超限 kill 并记录。
import { spawn } from 'node:child_process'

const PROMPT_TEMPLATES = {
  pre_market_scan:
    '扫描 {universe} 板块，评估隔夜新闻情绪影响。\n当前持仓：{positions}\n风控阈值：{risk_limits}\n输出调仓建议，包含因子依据。',
  breaking_news: '评估以下新闻对当前持仓的影响：\n{news_text}\n当前持仓：{positions}\n输出风险等级和应对建议。',
  risk_review:
    '复盘当前组合的风险暴露。\n持仓详情：{positions}\n近期市场数据：{market_summary}\n输出风险归因和改进建议。',
}

export function buildPrompt(taskType, context = {}, templates = PROMPT_TEMPLATES) {
  const template = templates[taskType]
  if (!template) throw new Error(`unknown task type: ${taskType}`)
  return template.replace(/\{(\w+)\}/g, (match, key) => (context[key] === undefined ? match : String(context[key])))
}

export function createSemaphore(limit) {
  let active = 0
  const waiting = []
  return {
    get active() {
      return active
    },
    get waiting() {
      return waiting.length
    },
    acquire() {
      return new Promise((resolve) => {
        const go = () => {
          active += 1
          resolve()
        }
        if (active < limit) go()
        else waiting.push(go)
      })
    },
    release() {
      active -= 1
      const next = waiting.shift()
      if (next) next()
    },
  }
}

export function createHeadlessRunner({ config, store, spawnImpl = spawn, clock = () => new Date(), onCall } = {}) {
  const headless = config.channels.headless
  const semaphore = createSemaphore(headless.concurrency)
  let killed = 0

  function estimateTokens(text) {
    // 粗估上界：CJK 约 1 字/token，ASCII 约 4 字符/token。
    const cjk = (text.match(/[\u4e00-\u9fff]/g) || []).length
    const rest = text.length - cjk
    return Math.ceil(cjk + rest / 4)
  }

  function runOnce(prompt) {
    return new Promise((resolve) => {
      const startedAt = clock().toISOString()
      const t0 = Date.now()
      const child = spawnImpl(headless.dshBin, ['--profile', headless.profile, prompt], {
        env: { ...process.env, DSH_HOME: config.home },
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      let stdout = ''
      let stderr = ''
      let timedOut = false
      const timer = setTimeout(() => {
        timedOut = true
        killed += 1
        child.kill('SIGTERM')
      }, headless.timeoutMs)
      child.stdout.on('data', (chunk) => {
        stdout += chunk.toString('utf8')
      })
      child.stderr.on('data', (chunk) => {
        stderr += chunk.toString('utf8')
      })
      child.on('close', (code, signal) => {
        clearTimeout(timer)
        const answer = stdout.split('\n').filter((line) => line.trim() !== '').pop() ?? ''
        const tokensEstimate = estimateTokens(prompt) + estimateTokens(answer)
        resolve({
          started_at: startedAt,
          prompt,
          answer,
          diagnostics: timedOut ? `${stderr}\n[platform] 超时 ${headless.timeoutMs}ms，已强制 kill` : stderr,
          exit_code: code,
          signal,
          success: code === 0 && !timedOut,
          timed_out: timedOut,
          duration_ms: Date.now() - t0,
          tokens_estimate: tokensEstimate,
          over_budget: tokensEstimate > headless.tokenBudgetPerCall,
        })
      })
    })
  }

  function execute(prompt) {
    return semaphore.acquire().then(async () => {
      try {
        const record = await runOnce(prompt)
        store.append('headless_calls.jsonl', record)
        try {
          onCall?.(record)
        } catch {
          // 观测失败不影响业务
        }
        return record
      } finally {
        semaphore.release()
      }
    })
  }

  function stats() {
    const calls = store.readAll('headless_calls.jsonl')
    const today = clock().toISOString().slice(0, 10)
    const todays = calls.filter((c) => String(c.started_at || '').slice(0, 10) === today)
    const ok = todays.filter((c) => c.success)
    const avgMs = todays.length === 0 ? 0 : Math.round(todays.reduce((sum, c) => sum + (c.duration_ms || 0), 0) / todays.length)
    return {
      today: { total: todays.length, success: ok.length, failed: todays.length - ok.length, avgMs, killed },
      breaker: {
        concurrencyLimit: headless.concurrency,
        active: semaphore.active,
        queued: semaphore.waiting,
        timeoutMs: headless.timeoutMs,
        tokenBudgetPerCall: headless.tokenBudgetPerCall,
      },
      last: calls.slice(-5).reverse(),
    }
  }

  return { execute, buildPrompt, stats, PROMPT_TEMPLATES }
}

export function createScheduler({ rules, runner, wbCall, clock = () => new Date() }) {
  const firedAt = new Map() // ruleId -> 'YYYY-MM-DD'
  const recent = []

  async function fire(rule) {
    const context = { universe: 'A股新能源', positions: '（见工作台 snapshot）', risk_limits: '单笔2% / 行业20% / 回撤15%' }
    try {
      const snap = await wbCall('snapshot', {})
      if (snap?.ok) context.positions = JSON.stringify(snap.value?.positions ?? '空').slice(0, 800)
    } catch {
      // 上下文取不到就用占位，任务照发（headless 侧会再取数据）
    }
    const prompt = runner.buildPrompt(rule.task, context)
    const record = await runner.execute(prompt)
    recent.unshift({ rule: rule.id, at: clock().toISOString(), success: record.success, exit_code: record.exit_code })
    return record
  }

  function tick(now = clock()) {
    const hhmm = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`
    const day = now.toISOString().slice(0, 10)
    for (const rule of rules) {
      if (rule.at !== hhmm || firedAt.get(rule.id) === day) continue
      firedAt.set(rule.id, day)
      fire(rule).catch(() => {})
    }
    return { fired: rules.filter((rule) => firedAt.get(rule.id) === day).map((rule) => rule.id) }
  }

  function view() {
    return {
      rules,
      firedToday: [...firedAt.entries()].map(([id, day]) => ({ id, day })),
      recent: recent.slice(0, 10),
    }
  }

  return { tick, view, fire }
}

export default { createHeadlessRunner, createScheduler, buildPrompt }
