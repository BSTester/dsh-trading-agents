// 指标与审计（FR 附录 §8.3 监控告警 / §4.2 审计）。
// 指标：进程内计数 + Prometheus 文本暴露（/metrics），覆盖规格列出的
// Headless 成功率与耗时、SDK 会话、MCP 工具调用延迟与失败率、数据源状态、订单与风控阻断。
// 审计：所有变更类调用（POST）与 MCP tools/call 追加写入 data/audit.jsonl，源地址与结果留痕。
import fs from 'node:fs'
import path from 'node:path'

export function createMetrics() {
  const counters = new Map() // key -> number
  const gauges = new Map()

  const bump = (key, delta = 1) => counters.set(key, (counters.get(key) ?? 0) + delta)
  const label = (parts) => parts.filter(Boolean).join(',')

  return {
    http(route, method, status, durationMs) {
      bump(`quant_v3_http_requests_total{route="${route}",method="${method}",status="${status}"}`)
      bump('quant_v3_http_request_duration_ms_sum', durationMs ?? 0)
      bump('quant_v3_http_request_duration_ms_count')
    },
    wbCall(tool, ok, ms) {
      bump(`quant_v3_wb_calls_total{tool="${tool}",result="${ok ? 'ok' : 'error'}"}`)
      bump('quant_v3_wb_call_duration_ms_sum', ms ?? 0)
      bump('quant_v3_wb_call_duration_ms_count')
    },
    tool(name, ok, ms) {
      bump(`quant_v3_mcp_tool_calls_total{tool="${name}",result="${ok ? 'ok' : 'error'}"}`)
      bump('quant_v3_mcp_tool_duration_ms_sum', ms ?? 0)
      bump('quant_v3_mcp_tool_duration_ms_count')
    },
    headless(ok, ms, tokens) {
      bump(`quant_v3_headless_calls_total{result="${ok ? 'success' : 'failure'}"}`)
      bump('quant_v3_headless_duration_ms_sum', ms ?? 0)
      bump('quant_v3_headless_duration_ms_count')
      bump('quant_v3_headless_tokens_estimate_sum', tokens ?? 0)
    },
    // 结构化快照：给 UI 逐点位绑定用（与 Prometheus 文本同源）
    snapshot() {
      const out = { mcp: { calls: 0, errors: 0, durationMsSum: 0, tools: {} }, wb: { calls: 0, errors: 0, durationMsSum: 0, byTool: {} }, http: { requests: 0, errors: 0, durationMsSum: 0 }, headless: { calls: 0, errors: 0, durationMsSum: 0, tokens: 0 } }
      const parse = (key) => {
        const match = key.match(/^([a-z0-9_]+)(?:\{(.*)\})?$/)
        const labels = {}
        if (match?.[2]) {
          for (const pair of match[2].split(',')) {
            const [k, v] = pair.split('=')
            labels[k] = String(v ?? '').replace(/"/g, '')
          }
        }
        return { name: match?.[1] ?? key, labels }
      }
      for (const [key, value] of counters) {
        const { name, labels } = parse(key)
        if (name === 'quant_v3_wb_calls_total') {
          out.wb.calls += value
          if (labels.result === 'error') out.wb.errors += value
          out.wb.byTool[labels.tool] = (out.wb.byTool[labels.tool] ?? 0) + value
        } else if (name === 'quant_v3_wb_call_duration_ms_sum') out.wb.durationMsSum += value
        else if (name === 'quant_v3_mcp_tool_calls_total') {
          out.mcp.calls += value
          if (labels.result === 'error') out.mcp.errors += value
          out.mcp.tools[labels.tool] = (out.mcp.tools[labels.tool] ?? 0) + value
        } else if (name === 'quant_v3_mcp_tool_duration_ms_sum') out.mcp.durationMsSum += value
        else if (name === 'quant_v3_http_requests_total') {
          out.http.requests += value
          if (!String(labels.status).startsWith('2')) out.http.errors += value
        } else if (name === 'quant_v3_http_request_duration_ms_sum') out.http.durationMsSum += value
        else if (name === 'quant_v3_headless_calls_total') {
          out.headless.calls += value
          if (labels.result === 'failure') out.headless.errors += value
        } else if (name === 'quant_v3_headless_duration_ms_sum') out.headless.durationMsSum += value
        else if (name === 'quant_v3_headless_tokens_estimate_sum') out.headless.tokens += value
      }
      out.mcp.avgMs = out.mcp.calls > 0 ? Math.round(out.mcp.durationMsSum / out.mcp.calls) : 0
      out.wb.avgMs = out.wb.calls > 0 ? Math.round(out.wb.durationMsSum / out.wb.calls) : 0
      out.http.avgMs = out.http.requests > 0 ? Math.round(out.http.durationMsSum / out.http.requests) : 0
      out.headless.avgMs = out.headless.calls > 0 ? Math.round(out.headless.durationMsSum / out.headless.calls) : 0
      return out
    },
    gauge(name, value, labels = '') {
      gauges.set(label([name, labels]), value)
    },
    render({ workbenchUp, omsStages = {}, headlessStats = null, sdkStatus = null } = {}) {
      const lines = []
      for (const [key, value] of counters) lines.push(`${key} ${value}`)
      if (workbenchUp !== undefined) lines.push(`quant_v3_workbench_up ${workbenchUp ? 1 : 0}`)
      for (const [stage, count] of Object.entries(omsStages)) lines.push(`quant_v3_oms_orders{stage="${stage}"} ${count}`)
      if (headlessStats) {
        lines.push(`quant_v3_headless_active ${headlessStats.breaker?.active ?? 0}`)
        lines.push(`quant_v3_headless_queued ${headlessStats.breaker?.queued ?? 0}`)
      }
      if (sdkStatus) {
        lines.push(`quant_v3_sdk_channel_ready ${sdkStatus.status === 'ready' ? 1 : 0}`)
      }
      for (const [key, value] of gauges) lines.push(`${key} ${value}`)
      return lines.join('\n') + '\n'
    },
  }
}

export function createAudit(dir) {
  fs.mkdirSync(dir, { recursive: true })
  const file = path.join(dir, 'audit.jsonl')
  return {
    file,
    append(record) {
      const entry = { at: new Date().toISOString(), ...record }
      try {
        fs.appendFileSync(file, JSON.stringify(entry) + '\n')
      } catch {
        // 审计失败不阻断业务，但记到 stderr
        console.error('[audit] write failed')
      }
      return entry
    },
    readAll() {
      try {
        return fs
          .readFileSync(file, 'utf8')
          .split('\n')
          .filter((line) => line.trim() !== '')
          .map((line) => JSON.parse(line))
      } catch {
        return []
      }
    },
  }
}

export default { createMetrics, createAudit }
