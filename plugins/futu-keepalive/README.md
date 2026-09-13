# @bstester/dsh-futu-keepalive —— 富途 token 保活

## 为什么需要

富途 OAuth 的 access_token 官方有效期只有 **`expires_in = 7200`（2 小时）**，而过期时
服务端对所有工具返回 JSON-RPC `internal error`，**不是 401**。

雪上加霜的是，`futu-mcp` 那一行的 `Authorization` 头是在**该行挂载时求值一次**的固定字符串：

```
行挂载 → !!js 求值读 ~/.dsh/futu-token → Zod 校验（getter 会被压平）
       → createTransport(config) 固定给 transport
```

重连也复用同一份 `config`，所以 token 一过期，行里的头永远是旧的。MCP 传输层看不到异常
（HTTP 仍是 200），不会触发重连 —— 结果是**工具继续列在工具列表里，但每次调用都失败**。

曾验证过的无效方案：用 getter 让每次请求重读 token（MCP SDK 内部确实每次请求都
`{...headers}` 展开）。**实测 `z.record(z.string())` 会把 getter 压平**，除非改
`dsh-mcp-client` 源码，否则此路不通。

## 这个插件做什么

按节奏调用**共享实现** `trading_datasource.futu_mcp.ensure_fresh()` 续期：

- 默认每 **10 分钟**检查一次；
- 剩余有效期不足 **30 分钟**时才续期——**不无条件续期**，因为换发新 token 可能让
  仍在被使用的旧 token 失效；
- 不重写 OAuth：续期逻辑只有一份，在 `plugins/datasource`；
- 缺少 venv 或 token 时**安静跳过**，失败只记一条警告，绝不影响会话里的其他能力。

配置项（都可不填）：`checkIntervalMs`、`renewWithinSeconds`、`python`。

## 热重载：让**已在运行**的会话也换上新 token

关键在于读 `dsh-agent-presets` 的源码：

```js
async function compositionStamp(path) {
  const { mtimeMs, size } = await stat(path);
  return { mtimeMs, size };            // ← 印章只由 mtime 和大小决定
}
async ensureStanding(preset) {
  const current = await compositionStamp(preset.path);
  if (sameStamp(mounted.stamp, current)) return mounted;
  this.standing.delete(preset.id);     // ← 印章变了就销毁共享挂载
  return this.ensureStanding(preset);  // ← 重建：重新读组合、重新求值 !!js 头
}
```

触发 `ensureStanding` 的入口有两个：新会话的 `mount()`，以及现有会话的
`agentPresets.recompose(agentCtx, id)`。所以续期后只要：

1. `utimesSync(presetFile)` 改变印章；
2. 调 `agentPresets.recompose(ctx, presetId)`；

就能让**当前会话**重建 futu-mcp 那一行，拿到新的 Authorization 头。

`hotReload: false` 可关闭，退回「新建会话」的人工路径。无论成功与否，
日志都会说明结果，失败时给出可执行的退路，绝不静默。

> ⚠️ **验证状态**：这条路径是**基于源码证据实现的，尚未在运行时验证**——
> 验证它需要一个交易模式会话，而我在非交易会话里创建不了。
> 因此判断逻辑、改印章、调用 recompose、失败回退都有离线测试，
> 但「recompose 之后工具真的用上了新 token」需要在交易会话里实测。
> 在此之前，"新建会话"仍是确定可行的路径。

> 另注：热重载只会在**续期确实发生**后触发（默认会话开始约 90 分钟后）。
> 短会话不会碰到它。

## 测试

`tests/futu-keepalive.test.mjs`（离线，10 例）锁住：返回 disposer、无 `ctx.effect` 时回退到
dispose 事件、续期失败只警告不抛、不叠加执行、不硬依赖任何服务、续期后触碰组合文件并
请求一次 recompose、`hotReload:false` 时不触碰文件、`agentPresets` 缺失或 recompose 抛错
时只提示新建会话。
