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

## 它不能解决什么

**已挂载的会话仍持有旧 token。** 实测 preset 目录**没有任何 watcher**，组合是在会话挂载时
读取的，因此 `touch` preset 文件只对**之后新建的会话**生效。要拿到新 token，必须
**新建会话**（仍选「交易智囊模式」）。插件在续期成功时会明确把这一点写进日志。

## 测试

`tests/futu-keepalive.test.mjs`（离线，7 例）锁住：返回 disposer、无 `ctx.effect` 时回退到
dispose 事件、续期失败只警告不抛、不叠加执行、不硬依赖任何服务。
