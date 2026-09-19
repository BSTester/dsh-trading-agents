// 测试桩：按 @deepseek-ai/dsh-sdk-protocol 文档契约模拟 `dsh --profile sdk` 运行时。
// initialize → serverInfo deepseek-harness-sdk-runtime；session/prompt → {messageId}
// 随后下发 session.status(running) / session.event(assistant 文本) / session.status(idle)。
let buffer = ''
process.stdin.setEncoding('utf8')
process.stdin.on('data', (chunk) => {
  buffer += chunk
  let index
  while ((index = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, index)
    buffer = buffer.slice(index + 1)
    if (line.trim() === '') continue
    let message
    try {
      message = JSON.parse(line)
    } catch {
      continue
    }
    const { id, method, params } = message
    if (method === 'initialize') {
      process.stdout.write(
        JSON.stringify({ jsonrpc: '2.0', id, result: { serverInfo: { name: 'deepseek-harness-sdk-runtime', version: '0.1.5' }, protocolVersion: 'sdk-1' } }) + '\n',
      )
    } else if (method === 'session/prompt') {
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result: { messageId: 'msg-1' } }) + '\n')
      const sessionId = params?.sessionId ?? 'unknown'
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', method: 'session.status', params: { sessionId, status: 'running' } }) + '\n')
      process.stdout.write(
        JSON.stringify({ jsonrpc: '2.0', method: 'session.event', params: { sessionId, event: { type: 'assistant', text: '示例结论：持有。' } } }) + '\n',
      )
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', method: 'session.status', params: { sessionId, status: 'idle' } }) + '\n')
    } else if (method === 'shutdown') {
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result: {} }) + '\n')
      process.exit(0)
    }
  }
})
