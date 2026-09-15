function accountTool(name) {
  if (!name.startsWith("mcp__futu__")) return null;
  const operation = name.slice("mcp__futu__".length);
  if (operation.startsWith("sim_trade_")) return { mode: "sim", write: false };
  if (operation.startsWith("account_")) return { mode: "live", write: false };
  if (operation.startsWith("trading_")) return { mode: "live", write: true };
  return null;
}

export function installTradingPolicy(ctx) {
  const store = ctx.tradingWorkbench;
  ctx.tools.guard((exec) => {
    const account = accountTool(exec.name);
    if (!account) return;
    const mode = store.readMode();
    if (mode !== account.mode) return `账户模式为 ${mode}，拒绝 ${account.mode} 账户调用；请先显式切换模式`;
  });
  ctx.on("tools/pre-execute", async (exec, next) => {
    const decision = await next();
    // 先尊重下游守卫：别人 deny/ask 的结论不归我们改
    if (decision.kind !== "allow") return decision;
    const account = accountTool(exec.name);
    if (!account?.write) return decision;

    // 实盘写操作 → **本插件自己的业务确认**，不经过 DSH 的 approval 系统。
    //
    // 为什么不返回 {kind:"ask"}：权限确认回答的是"这个动作准不准做"，由会话的
    // approval policy 裁决，而 full-access（policy="never"）下
    // `approval.decide()` 会直接 rejected —— 连问都不问，表现为
    // `the user rejected tool ...`，看起来像用户拒绝，实际没人被问过。
    // "这笔业务参数对不对"是交易动作的固有环节（像转账要输密码），
    // 不该因为系统设成免打扰就静默失败。
    //
    // 确认由工作台界面作答（requestConfirmation 等人点按钮），此处据结果
    // 返回 allow/deny，权限系统全程无从介入。
    const outcome = await store.requestConfirmation({
      tool: exec.name,
      mode: account.mode,
      args: exec.arguments,
      session_id: String(exec.agent?.session?.id ?? "unknown"),
      signal: exec.signal,
    });
    if (outcome.decision === "approved") return decision;
    return {
      kind: "deny",
      reason: `实盘操作未获确认（${outcome.reason}）。`
        + `这不是权限问题，而是必须由用户在工作台逐笔确认订单参数；`
        + `请把订单摘要交给用户，等其在「交易工作台」确认后重试：${exec.name}`,
    };
  });
  ctx.on("tools/execute", async (exec, next) => {
    const account = accountTool(exec.name);
    if (!account) return next();
    const release = store.enterBrokerCall(account.mode);
    try {
      return await next();
    } finally {
      release();
    }
  });
  ctx.on("tools/result", (exec, result) => {
    const account = accountTool(exec.name);
    if (!account) return;
    store.recordObservation({
      tool: exec.name, mode: account.mode, session_id: String(exec.agent?.session.id ?? "unknown"),
      is_error: result.isError, value: result.isError ? { error: result.error, content: result.content } : result.value,
    });
  });
}
