/**
 * `quant-tool-whitelist` — profile 级工具白名单闸门（V3 `docs/v3-spec.md` FR-GATEWAY-002 硬边界 ①）
 *
 * 为什么需要它（实测结论，不是猜想）
 * ---------------------------------
 * profile 层「按 shipped 行的 `disabled: true` 关停」只能关**本地** `tool-*` 行。平台的量化
 * 工具面（77 个工作台工具 + `/api/v3/*` 路由桥接出的 `v3_*` 工具）是经
 * `@deepseek-ai/dsh-mcp-client` **一行**（`quant-platform-mcp`）进来的，而那个包**没有**
 * allow/deny 之类配置项（README 配置表只有 serverName/transport/url/env/failOnStartupError/
 * toolCallTimeoutMs/reconnect），`@deepseek-ai/dsh-tools` 的 `Config` 也只有
 * `mode` / `maxParallelSubCalls`。所以 `trade_place` / `trade_modify` / `trade_cancel` /
 * `plan_execute` / `switch_mode` / `v3_credentials` / `v3_oms_sync` 在那一步关不掉。
 *
 * 本插件用 `dsh-tools` 自己公开的两个机制补齐这道边界：
 *   * `ctx.tools.guard()` —— **单调**执行闸门：返回理由即拒绝，且「no later listener can turn
 *     that denial back into permission」（`dsh-tools` README）。命中就永远执行不了。
 *   * `ctx.tools.restrict({ deny })` —— 在 `agent/created` 里按 **agent 作用域**把命中的工具
 *     从模型可见的工具面上摘掉（不只是拒绝调用）。从 `schemas()` 的真实名字里算命中集，
 *     所以不会因为「名字不存在」而被 `restrict` 拒绝。
 *
 * 拒绝集与 `platform/server/v3_sdk.py` 的 `WRITE_TOOL_NAMES` / `WRITE_TOOL_PREFIXES` /
 * `WRITE_TOOL_SUBSTRINGS` **逐字同源**：`platform/tests/test_v3_sdk.py` 会读本文件并断言两侧
 * 一致——任何一边漂移，测试立刻变红。改这里就必须同步改那里（反之亦然）。
 *
 * 边界诚实性：这是**纵深的一层**，不是交易边界本身。真正的闸门在服务侧——写端点要人工在
 * Web 确认（TTL 120s 超时=拒绝，fail-closed）、`confirm-decide` / `rules-decide` 有意不进
 * MCP 工具面、`/api/v3/credentials` 的 save/clear 在桥内封死、sim→live 要人在 Web 输口令。
 */

/** Stable Cordis plugin name. */
const name = 'quant-tool-whitelist'

/** Needs the tool registry only. */
const inject = ['tools']

// ── 白名单策略（与 platform/server/v3_sdk.py 逐字同源：PYTHON_POLICY_BEGIN/END 之间）──
const DENY_NAMES = [
  'auto_pipeline',
  'confirm_decide',
  'modify_user_security',
  'openapi_config',
  'openapi_oauth',
  'openapi_test',
  'plan_execute',
  'push_subscribe',
  'push_unsubscribe',
  'record_observation',
  'research_tasks_claim',
  'research_tasks_report',
  'rules_decide',
  'sentiment_snapshot',
  'switch_mode',
]
const DENY_PREFIXES = [
  'trade_',
  'sim_trade_',
  'openapi_',
  'v3_credentials',
  'v3_oms_sync',
  'v3_strategy_run',
  'v3_sdk',
  'admin_cancel',
  'admin_prune',
]
const DENY_SUBSTRINGS = ['credentials', 'confirm_decide']
// ── 白名单策略 结束 ──────────────────────────────────────────────────────────

/** MCP 工具命名空间前缀形状：`mcp__<serverName>__<rawName>`。 */
const MCP_NAMESPACE = /^mcp__[A-Za-z0-9_-]{1,32}__/

/**
 * `mcp__quantwb__plan-execute` → `plan_execute`（判定用的规范形）。
 * @param raw - the model-facing tool name.
 * @returns the normalized name.
 */
function normalizeToolName(raw) {
  const text = String(raw === undefined || raw === null ? '' : raw).trim()
  return text.replace(MCP_NAMESPACE, '').replace(/[\s.\-]+/g, '_').toLowerCase()
}

/**
 * Split a config list ("a, b" or `["a","b"]`) into normalized names.
 * @param value - raw config value.
 * @returns normalized, non-empty names.
 */
function toNameSet(value) {
  const items = Array.isArray(value) ? value : String(value || '').split(',')
  return new Set(items.map(normalizeToolName).filter(Boolean))
}

/**
 * Decide whether one tool name is outside the whitelist.
 * @param raw - the model-facing tool name.
 * @param policy - resolved allow/deny extras.
 * @returns the denial reason, or `null` when the tool is allowed.
 */
function denialReason(raw, policy) {
  const normalized = normalizeToolName(raw)
  if (!normalized) return '空工具名（fail-closed）'
  if (policy.allowExtra.has(normalized)) return null
  if (policy.denyExtra.has(normalized)) return `运行期加严 denyExtra：${normalized}`
  if (DENY_NAMES.indexOf(normalized) >= 0) return `精确名 ${normalized}`
  for (const prefix of DENY_PREFIXES) {
    if (normalized.startsWith(prefix)) return `前缀 ${prefix}*`
  }
  for (const part of DENY_SUBSTRINGS) {
    if (normalized.indexOf(part) >= 0) return `子串 ${part}`
  }
  return null
}

/**
 * Install the guard and the per-agent visibility restriction.
 * @param ctx - the profile-level plugin context.
 * @param config - `{ allowExtra?, denyExtra? }`.
 */
function apply(ctx, config = {}) {
  const policy = { allowExtra: toNameSet(config.allowExtra), denyExtra: toNameSet(config.denyExtra) }

  // ① 单调执行闸门：命中即拒，后续任何监听器都无法改回允许。
  ctx.effect(() => ctx.tools.guard((execution) => {
    const reason = denialReason(execution && execution.name, policy)
    if (reason === null) return undefined
    return `quant-tool-whitelist 拒绝：${reason}；本会话是只读研究会话，写/交易工具不可用`
  }))

  // ② 按 agent 作用域把命中的工具从模型可见面上摘掉。
  //    stderr 只用于诊断（stdout 是 JSON-RPC 专线，绝不能碰）。
  ctx.on('agent/created', (payload) => {
    const agent = payload && payload.agent
    try {
      const schemas = (agent && agent.ctx && agent.ctx.tools && agent.ctx.tools.schemas())
        ? agent.ctx.tools.schemas() : []
      const names = (schemas || []).map((item) => item && item.name).filter(Boolean)
      const hits = names.filter((toolName) => denialReason(toolName, policy) !== null)
      if (!hits.length) {
        console.error(`[${name}] agent ${agent && agent.id}: 工具面无需收窄（0 命中）`)
        return
      }
      ctx.effect(() => agent.ctx.tools.restrict({ deny: hits }))
      console.error(`[${name}] agent ${agent && agent.id}: 已从可见工具面摘除 ${hits.length} 件 `
        + `写/交易工具：${hits.join(', ')}`)
    } catch (error) {
      // 摘除失败也不能让会话不可用：单调闸门仍在，调用那件工具依然会被拒绝。
      console.error(`[${name}] 摘除可见工具失败（执行闸门仍然生效）：${error && error.message}`)
    }
  })
}

export { apply, denialReason, inject, name, normalizeToolName }
export { DENY_NAMES, DENY_PREFIXES, DENY_SUBSTRINGS }
