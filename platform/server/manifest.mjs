// WP6 工具面清单（单一事实来源，规格 §3）。
// 20 个端点工具与面板共用同一个 createRpcHandler 实例（对等性由结构保证）；
// 5 个维护工具来自 workbench_admin.mjs 的能力提升。
// 通道分级（规格 §3.2 ⚠）：switch_mode 在本层封死 live——模型可见的通道不得切换实盘，
// live 切换只能由用户在独立 Web 输入口令完成；plan_execute 保留口令入口（规格 §8.3 对话侧等价入口）。
import { z } from "zod";

const REFRESH = { refresh: z.boolean().optional().describe("true 时绕过 TTL 缓存强制重取") };

function endpointTool(name, endpoint, description, input) {
  return {
    kind: "endpoint", name, endpoint, description,
    input: { ...input, ...REFRESH },
    async run({ handle, args }) {
      const { refresh: force, ...rest } = args ?? {};
      return handle(endpoint, force === true ? { ...rest, _refresh: true } : rest);
    },
  };
}

// envelope 接收 thunk 而非 promise：store 方法同步抛错（数据文件损坏/不可写等）
// 也必须被捕获并包装为 {ok:false, error:{code:"trading/invalid-operation"}} envelope。
async function envelope(fn) {
  try {
    return { ok: true, value: await fn() };
  } catch (error) {
    return { ok: false, error: { code: "trading/invalid-operation",
      message: String(error?.message ?? error).slice(0, 300), details: {} } };
  }
}

export const ENDPOINT_TOOLS = [
  endpointTool("snapshot", "snapshot", "工作台全量快照：账户模式、研报、研究 run、量化预览、交易响应、去重订单事实（trade_summary）、在途调用与缺失端点清单。无券商数据时明确为空。", {}),
  {
    kind: "endpoint", name: "switch_mode", endpoint: "switch-mode",
    description: "切换账户模式，仅限 live→sim（回模拟盘）。sim→live 被本工具拒绝：实盘切换只能由用户在独立 Web（http://127.0.0.1:8397）输入口令「确认实盘」完成。切换模式不授权任何订单。",
    input: {
      mode: z.enum(["sim", "live"]).describe("目标模式（本工具只接受 sim）"),
      expected_mode: z.enum(["sim", "live"]).describe("调用方所见当前模式，防过期数据误切换"),
      confirmation: z.string().optional().describe("回 sim 无需口令；本工具不接受 live 切换"),
      refresh: z.boolean().optional(),
    },
    async run({ handle, args }) {
      if (args?.mode === "live") {
        return { ok: false, error: { code: "trading/live-switch-web-only",
          message: "实盘切换只能在独立 Web 由用户输入口令完成；模式切换不授权任何订单", details: {} } };
      }
      const { refresh: force, ...rest } = args ?? {};
      return handle("switch-mode", force === true ? { ...rest, _refresh: true } : rest);
    },
  },
  endpointTool("series", "series", "K 线序列（富途优先、降级如实标注）。", {
    ticker: z.string().describe("标的代码，如 SH.600519"),
    period: z.enum(["1m", "5m", "15m", "30m", "60m", "1d"]).optional().describe("默认 5m"),
    limit: z.number().int().min(20).max(2000).optional().describe("20..2000，默认 300"),
  }),
  endpointTool("equity", "equity", "本地模拟台账权益曲线（不代表券商资产）。", {
    mode: z.enum(["sim", "live"]).optional(), window: z.number().int().optional(),
  }),
  endpointTool("positions", "positions", "券商真实持仓（按账户小计，不跨币种合并；失败账户列入 errors）。", {
    mode: z.enum(["sim", "live"]).optional(), window: z.number().int().optional(),
  }),
  endpointTool("correlation", "correlation", "持仓间相关性矩阵。", {
    tickers: z.array(z.string()).describe("标的列表"), window: z.number().int().optional(),
  }),
  endpointTool("sensitivity", "sensitivity", "策略参数敏感性矩阵。", {
    ticker: z.string().optional(), strategy: z.string().optional(), metric: z.string().optional(),
    fast_grid: z.array(z.number().int()).optional(), slow_grid: z.array(z.number().int()).optional(),
    buy_grid: z.array(z.number()).optional(), sell_grid: z.array(z.number()).optional(),
    start: z.string().optional(),
  }),
  endpointTool("risk", "risk", "风控配置与当前账户风险指标。", {}),
  endpointTool("trades", "trades", "本地模拟台账成交记录（sim 专属）。", {
    mode: z.enum(["sim", "live"]).optional(), limit: z.number().int().optional(),
  }),
  endpointTool("events", "events", "标的公告/事件时间线。", {
    ticker: z.string().describe("标的代码"), days: z.number().int().optional(),
  }),
  endpointTool("factors", "factors", "横截面因子打分表。", {
    tickers: z.array(z.string()).describe("标的列表"), window: z.number().int().optional(),
  }),
  endpointTool("ic", "ic", "因子 RankIC 序列。", {
    tickers: z.array(z.string()).describe("标的列表"), factor: z.string().optional(),
    forward: z.number().int().optional(), window: z.number().int().optional(),
  }),
  endpointTool("audit", "audit", "审计链：计划→订单→成交三级链路 + 信号/响应链路。", {}),
  endpointTool("sources", "sources", "数据源健康状态（渠道可用性与 as_of）。", {
    no_probe: z.boolean().optional().describe("true 时只读缓存不探测"),
  }),
  endpointTool("instrument", "instrument", "标的解析（名称/市场/整手等）。", {
    ticker: z.string().describe("标的代码"),
  }),
  endpointTool("quality", "quality", "单标的数据质量报告（缺口/复权）。", {
    ticker: z.string().describe("标的代码"),
  }),
  endpointTool("plan", "plan", "当前/历史执行计划：目标 vs 实际 diff、逐单风控预检、状态时间线；附当前账户模式。", {}),
  endpointTool("plan_execute", "plan-execute", "唯一受约束执行入口：execute=执行已冻结计划（live 需口令「确认执行」，用户须在对话中逐笔确认后由你携带）；cancel=取消计划；kill/unkill=风控总开关。返回 queued+nonce，状态用 plan 轮询。不等待执行结果。", {
    plan_hash: z.string().optional().describe("action=execute/cancel 时必填"),
    expected_mode: z.enum(["sim", "live"]).optional().describe("action=execute 时必填"),
    confirmation: z.string().optional().describe("live 执行必须为「确认执行」"),
    action: z.enum(["execute", "cancel", "kill", "unkill"]).optional().describe("默认 execute"),
  }),
  endpointTool("schedule", "schedule", "调度快照：daemon 心跳（>5 分钟即失联）、作业历史、kill/halt 状态。", {}),
  endpointTool("reconcile", "reconcile", "对账快照：最近差异、TCA 摘要、告警列表。", {}),
];

function hoursToMs(hours) { return Math.max(1, hours ?? 2) * 3600_000; }

export const ADMIN_TOOLS = [
  { kind: "admin", name: "admin_status", description: "工作台数据维护：数据文件路径与各类记录数量。",
    input: {},
    run: ({ store }) => envelope(() => { const state = store.read();
      return { file: store.file, runs: state.runs.length,
        reports: state.reports.length, previews: state.previews.length, activity: state.activity.length }; }) },
  { kind: "admin", name: "admin_runs", description: "工作台数据维护：列出全部研究 run（状态/标的/模式/年龄分钟）。",
    input: {},
    run: ({ store }) => envelope(() => store.read().runs.map((row) => {
      const started = Date.parse(row.started_at ?? "");
      return { id: row.id, status: row.status, ticker: row.ticker, mode: row.mode,
        age_minutes: Number.isFinite(started) ? Math.round((Date.now() - started) / 60000) : null };
    })) },
  { kind: "admin", name: "admin_cancel_run", description: "工作台数据维护：取消指定研究 run（标记 cancelled，保留记录）。先用 admin_runs 查 id。",
    input: { run_id: z.string().describe("run id") },
    run: ({ store, args }) => envelope(() => store.cancelRun(String(args.run_id))) },
  { kind: "admin", name: "admin_cancel_stale", description: "工作台数据维护：批量取消超时仍 running 的 run（默认 2 小时）。",
    input: { hours: z.number().optional().describe("阈值小时数，默认 2") },
    run: ({ store, args }) => envelope(() => store.cancelStaleRuns({ olderThanMs: hoursToMs(args?.hours) })) },
  { kind: "admin", name: "admin_prune_runs", description: "工作台数据维护：删除超时孤儿 run（无研报者；有研报的保留）。",
    input: { hours: z.number().optional().describe("阈值小时数，默认 2") },
    run: ({ store, args }) => envelope(() => store.pruneAbandonedRuns({ olderThanMs: hoursToMs(args?.hours) })) },
];

export const TOOL_NAME_BLACKLIST = Object.freeze(["exec", "shell", "file_read", "file_write", "read_file", "write_file", "token"]);
export const TOOL_COUNT = ENDPOINT_TOOLS.length + ADMIN_TOOLS.length;

/** 服务装配入口：handle=与面板同一的 createRpcHandler 产物；store=WorkbenchStore。 */
export function buildManifest({ handle, store }) {
  const context = { handle, store };
  return [...ENDPOINT_TOOLS, ...ADMIN_TOOLS].map(({ run, ...meta }) => ({
    ...meta, call: (args) => run({ ...context, args }),
  }));
}
