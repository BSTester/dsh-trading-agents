/**
 * 富途账户工具分类（WP7 收窄，2026-09-16）。
 *
 * 分类依据 = docs/TOOL-LIMITS.md §七（2026-09-13 全量体检：9 个实盘账户查询工具 +
 * 4 个实盘下单工具）与 plugins/core/python/trading_core/broker.py 的 WP3 锁定工具名：
 *
 * | 前缀          | 操作（去掉 mcp__futu__ 后的匹配）                        | 类别 | 处置 |
 * |---------------|----------------------------------------------------------|------|------|
 * | `sim_trade_*` | `input_order`/`place_order`/`modify_order`/`cancel_order` | **写**（下单/改单/撤单） | guard 一律拒绝（不分模式） |
 * | `sim_trade_*` | 其余：`position_list`/`account_list`/`history_order_list`/`cash_info`/`max_buy_sell` | 读（查询） | 模式桶 sim，互斥照旧 |
 * | `trading_*`   | `input_order`/`order_place`/`modify_order`/`cancel_order` | **写**（4 个实盘下单工具） | guard 一律拒绝（不分模式） |
 * | `account_*`   | 全部：`positions`/`cash_info`/`orders_history`/`authorized_trd_accs` 等（实盘账户只有查询） | 读（查询） | 模式桶 live，互斥照旧 |
 * | 其余（`quote_*`/`fin_*` 等） | —— | 非账户工具（行情/资讯研究） | 不归本策略管 |
 *
 * WP7 决策：富途**写路径唯一在工作台服务**（trade_place/trade_modify/trade_cancel，
 * 业务确认由独立 Web 确认卡片作答）。因此写类在 Harness 进程内**不可达**：
 * guard 一律拒绝并指引工作台通道，不再区分 sim/live。
 * 查询类保留模式互斥（WP6 R1 口径）：sim 拒实盘查询、live 拒 sim 查询；
 * 模式外的账户查询请用工作台（quantwb account_positions 等）。
 *
 * WP7 任务 5 修订（未知动词 fail-closed）：上表只列已知动词，而写类判定若仅依赖
 * 拒绝名单，上游新增写动词（如 `trading_order_place_v2`）会被当读放行——live 下
 * 危险。因此两族策略**有意不同**：
 *   - `trading_*` 族：未知动词一律按**写**拒绝（fail-closed，实盘不能赌）；
 *   - `sim_trade_*` 族：未知动词按**读**处理（模式桶 sim 互斥照旧）——sim 写伤害
 *     有界（模拟盘账本），fail-closed 误杀研究查询的代价更大；已知 4 个写动词仍按写拒绝。
 */

/** 写动词（下单/改单/撤单）。按后缀匹配，覆盖两族的全部已知写工具与命名变体。 */
const WRITE_VERBS = [
  "input_order",   // 下单（trading_input_order / sim_trade_input_order）
  "place_order",   // 下单（sim_trade_place_order）
  "order_place",   // 下单（trading_order_place，WP3 锁定 schema 的命名变体）
  "modify_order",  // 改单（两族都有；服务侧因间歇性 -5 也不用它，改=撤+重下）
  "cancel_order",  // 撤单（两族都有）
];

function writeOperation(operation) {
  return WRITE_VERBS.find((verb) => operation.endsWith(verb)) ?? null;
}

function accountTool(name) {
  if (!name.startsWith("mcp__futu__")) return null;
  const operation = name.slice("mcp__futu__".length);
  if (operation.startsWith("sim_trade_")) {
    // sim 族：未知动词按**读**处理（模式桶 sim，互斥照旧）。与 trading_ 族的差异是
    // 有意为之：sim 写伤害有界，误杀查询代价更大；已知写动词仍按写拒绝（见头注）。
    return { mode: "sim", write: writeOperation(operation) };
  }
  if (operation.startsWith("account_")) return { mode: "live", write: false };
  if (operation.startsWith("trading_")) {
    // trading 族 fail-closed：未知动词一律按**写**拒绝。拒绝名单只覆盖已知动词，
    // 上游新增写动词若被当读放行，live 下危险——实盘不能赌。
    return { mode: "live", write: true };
  }
  return null;
}

/** 写类的统一拒绝文案（WP7 收窄）：指引工作台的交易与查询通道。 */
const WRITE_REFUSAL = "富途写通道已收窄至工作台：请通过工作台交易"
  + "（quantwb 的 trade_* 工具，或计划执行）；模式外的账户查询请用工作台工具";

export function installTradingPolicy(ctx) {
  const store = ctx.tradingWorkbench;
  ctx.tools.guard((exec) => {
    const account = accountTool(exec.name);
    if (!account) return;
    // 写类：不分模式一律拒绝（WP7 收窄）。这是 deny 不是 ask，不落会话审批档位。
    if (account.write) return WRITE_REFUSAL;
    const mode = store.readMode();
    if (mode !== account.mode) return `账户模式为 ${mode}，拒绝 ${account.mode} 账户调用；请先显式切换模式`;
  });
  ctx.on("tools/pre-execute", async (exec, next) => {
    // 业务确认移至工作台服务侧（WP7）：futu 写类在 guard 已一律拒绝，到不了
    // pre-execute；main 624ccd0 引入的 requestConfirmation 分支就此退役。
    // store 三方法与 confirmation/confirm-decide 端点**保留**（legacy 面板过渡期 +
    // 服务侧 Python 移植同语义）。本钩子只做下游结论的显式透传：别人 deny/ask
    // 的结论不归我们改。
    return next();
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
