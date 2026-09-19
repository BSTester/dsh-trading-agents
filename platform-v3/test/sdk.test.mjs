// SDK 通道客户端的协议一致性测试：对着按官方 dsh-sdk-protocol 契约实现的测试桩运行时。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import createSdkChannel from '../server/gateway/sdk.mjs'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

function makeConfig(overrides = {}) {
  return {
    home: '/tmp/quant-v3-sdk-home',
    channels: {
      headless: { dshBin: process.execPath },
      sdk: {
        enabled: true,
        dshBin: process.execPath,
        argv: [path.join(root, 'test/fixtures/fake-dsh-sdk.mjs')],
        profile: 'sdk',
        provider: 'deepseek-official',
        model: 'deepseek-flash',
        reasoningEffort: 'high',
        maxTokens: 49152,
        cwd: '/tmp',
        requireKey: false, // 测试桩不需要模型密钥
        ...overrides,
      },
    },
  }
}

test('握手成功：serverInfo 为 deepseek-harness-sdk-runtime', async () => {
  const channel = createSdkChannel({ config: makeConfig() })
  const started = await channel.start()
  assert.equal(started.ok, true)
  const status = channel.status()
  assert.equal(status.status, 'ready')
  assert.equal(status.serverInfo.name, 'deepseek-harness-sdk-runtime')
  await channel.stop()
})

test('session/prompt 入队回执 + 事件流', async () => {
  const channel = createSdkChannel({ config: makeConfig() })
  await channel.start()
  const result = await channel.prompt('quant-001', '分析当前持仓风险')
  assert.equal(result.ok, true)
  assert.equal(result.messageId, 'msg-1')
  const methods = channel.status().events.map((e) => e.method)
  assert.ok(methods.includes('session.status'))
  assert.ok(methods.includes('session.event'))
  await channel.stop()
})

test('未握手直接 prompt：先自动握手', async () => {
  const channel = createSdkChannel({ config: makeConfig() })
  const result = await channel.prompt('quant-002', '评估隔夜新闻')
  assert.equal(result.ok, true)
  assert.equal(channel.status().initialized, true)
  await channel.stop()
})

test('无密钥：握手照常成功（握手不需要密钥），但给出会话警告', async () => {
  const saved = [process.env.DEEPSEEK_API_KEY, process.env.ZAI_CODING_CN_API_KEY, process.env.ANTHROPIC_API_KEY]
  delete process.env.DEEPSEEK_API_KEY
  delete process.env.ZAI_CODING_CN_API_KEY
  delete process.env.ANTHROPIC_API_KEY
  try {
    const channel = createSdkChannel({ config: makeConfig() })
    const started = await channel.start()
    assert.equal(started.ok, true)
    const status = channel.status()
    assert.equal(status.status, 'ready')
    assert.equal(status.credentials.modelKeyPresent, false)
    assert.match(status.sessionWarning, /MISSING_CREDENTIAL|模型密钥/)
    await channel.stop()
  } finally {
    if (saved[0] !== undefined) process.env.DEEPSEEK_API_KEY = saved[0]
    if (saved[1] !== undefined) process.env.ZAI_CODING_CN_API_KEY = saved[1]
    if (saved[2] !== undefined) process.env.ANTHROPIC_API_KEY = saved[2]
  }
})

test('requireKey=true 时按显式要求预检并拒绝', async () => {
  const saved = [process.env.DEEPSEEK_API_KEY, process.env.ZAI_CODING_CN_API_KEY, process.env.ANTHROPIC_API_KEY]
  delete process.env.DEEPSEEK_API_KEY
  delete process.env.ZAI_CODING_CN_API_KEY
  delete process.env.ANTHROPIC_API_KEY
  try {
    const channel = createSdkChannel({ config: makeConfig({ requireKey: true }) })
    const result = await channel.start()
    assert.equal(result.ok, false)
    assert.equal(result.error.code, 'sdk/no-credentials')
  } finally {
    if (saved[0] !== undefined) process.env.DEEPSEEK_API_KEY = saved[0]
    if (saved[1] !== undefined) process.env.ZAI_CODING_CN_API_KEY = saved[1]
    if (saved[2] !== undefined) process.env.ANTHROPIC_API_KEY = saved[2]
  }
})

test('最近一轮结论从事件流提取（turn/end 错误码）', async () => {
  const channel = createSdkChannel({ config: makeConfig() })
  await channel.start()
  await channel.prompt('quant-003', '自检')
  // 手写一条 turn/end 错误事件，验证 status().lastTurn 的提取
  channel.events.push({ at: new Date().toISOString(), method: 'session.event', params: { sessionId: 'quant-003', event: { type: 'turn/end', data: { turn: 1, reason: { kind: 'error', error: { code: 'MISSING_CREDENTIAL', message: 'llm-deepseek: no API key' } } } } } })
  const status = channel.status()
  assert.equal(status.lastTurn.kind, 'error')
  assert.equal(status.lastTurn.code, 'MISSING_CREDENTIAL')
  assert.match(status.lastTurn.message, /no API key/)
  await channel.stop()
})
