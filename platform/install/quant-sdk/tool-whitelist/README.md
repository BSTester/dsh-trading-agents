# `quant-tool-whitelist`（profile 级工具白名单闸门）

`quant-sdk` profile 的**第三层**白名单：用 `@deepseek-ai/dsh-tools` 自己公开的两个机制，
把写/交易工具从会话里真正拿掉。

## 为什么需要它

profile 层「按 shipped 行的 `disabled: true` 关停」只能关**本地** `tool-*` 行。平台的量化工具面
（工作台工具 + `/api/v3/*` 路由桥接出的 `v3_*` 工具）是经 `@deepseek-ai/dsh-mcp-client`
**一行**（`quant-platform-mcp`）进来的，而那个包**没有** allow/deny 配置项，
`@deepseek-ai/dsh-tools` 的 `Config` 也只有 `mode` / `maxParallelSubCalls`。
所以 `trade_place` / `trade_modify` / `trade_cancel` / `plan_execute` / `switch_mode` /
`v3_credentials` / `v3_oms_sync` 在那一步**关不掉**。

## 它做了什么

| 机制 | 位置 | 效果 |
|---|---|---|
| `ctx.tools.guard(fn)` | 插件 `apply` | **单调**执行闸门：命中即返回理由拒绝，之后任何监听器都无法把拒绝改回允许（`dsh-tools` README 的原文承诺） |
| `ctx.tools.restrict({ deny })` | `agent/created` 监听器（按 agent 作用域） | 把命中的工具从该会话**模型可见的工具面**上摘掉（不只是拒绝调用）。命中集从 `agent.ctx.tools.schemas()` 的真实名字算出来，所以不会因「未知名字」被 `restrict` 拒 |

诊断只写 **stderr**（`console.error`）——stdout 是 JSON-RPC 专线，绝不能污染。摘除成功会打印
一行 `[quant-tool-whitelist] agent <id>: 已从可见工具面摘除 N 件 写/交易工具：…`，
这一行会出现在 `GET /api/v3/sdk/status` 的 `stderr_tail` 里，是白名单生效的**可核对证据**。

## 拒绝集从哪来（三方同源）

`DENY_NAMES` / `DENY_PREFIXES` / `DENY_SUBSTRINGS` 三个常量与
`platform/server/v3_sdk.py` 的同名常量、以及 `../tool-whitelist.json` **逐字一致**，
真值是平台自己的三处既有常量（`v3_ops.WRITE_TOOLS`、
`mcp_tools.MCP_EXCLUDED_ENDPOINTS − readOnlyExcluded`、`tools/e2e_probe.WRITE_ENDPOINTS`）。
`platform/tests/test_v3_sdk.py` 断言三方一致并断言**没有覆盖缺口**（`policy_gaps`）——
任何一边漂移，测试立刻变红。**改这里就必须同步改那两处。**

名字判定前会归一化：剥掉 `mcp__<serverName>__` 前缀，`-`/`.`/空白 折成 `_`，转小写。
因此 `mcp__quantwb__plan-execute`、`plan_execute`、`plan-execute` 三种写法等价。

## 配置

```yaml
- id: quant-sdk-tool-whitelist
  name: quant-tool-whitelist
  config:
    allowExtra: []                                       # 只读例外（从拒绝集里减去）
    denyExtra: !!js "process.env.QUANT_SDK_TOOL_DENY || ''"   # 运行期加严，逗号分隔
```

`allowExtra` 只对**精确名档**生效（前缀/子串档不会被它绕过）——见 `denialReason()` 的判定顺序。

## 安装

```bash
dsh plugin --profile quant-sdk add file:<仓库>/platform/install/quant-sdk/tool-whitelist
```

pnpm 会报 `missing peer @deepseek-ai/cordis@^4.0.2` 的警告——可忽略：cordis 从
`$DSH_HOME/profiles/node_modules` 向上解析（`pnpm-workspace.yaml` 里
`nodeLinker: hoisted` + `autoInstallPeers: false` 就是为这条路径设的）。

## 边界诚实性

这是**纵深的一层，不是交易边界本身**。真正的闸门在服务侧：写端点要人工在 Web 确认
（TTL 120s 超时=拒绝，fail-closed）、`confirm-decide` / `rules-decide` / 设置页三端点有意
不进 MCP 工具面、`/api/v3/credentials` 的 save/clear 在桥内封死、sim→live 要人在 Web 输口令。
任何一条都不该被当成「所以可以放心」。
