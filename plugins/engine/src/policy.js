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
    if (decision.kind === "deny") return decision;
    const account = accountTool(exec.name);
    if (account?.write) {
      return { kind: "ask", reason: `真实账户操作，必须在 Harness 确认完整参数（模式切换不是下单授权）：\n${exec.name}\n${JSON.stringify(exec.arguments)}` };
    }
    return decision;
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
