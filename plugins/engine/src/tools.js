import { execFile } from "node:child_process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import os from "node:os";
import path from "node:path";

const run = promisify(execFile);
const pythonRoot = fileURLToPath(new URL("../python/", import.meta.url));
const jsonOutput = {
  schema: { type: "object", additionalProperties: true },
  render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }],
};

async function runQuant(script, args, signal) {
  const home = process.env.DSH_HOME || path.join(os.homedir(), ".dsh");
  const python = path.join(home, "trading-venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const { stdout } = await run(python, [path.join(pythonRoot, script), ...args], {
    timeout: 240_000, maxBuffer: 8 * 1024 * 1024, signal,
  });
  const start = stdout.indexOf("{");
  if (start < 0) throw new Error(`quant ${script} returned no JSON`);
  const result = JSON.parse(stdout.slice(start));
  if (result.error) throw new Error(`quant ${script}: ${result.error}`);
  return result;
}

function sessionId(exec) {
  const id = exec?.agent?.session?.id;
  if (!id) throw new Error("Trading research requires a Harness Session");
  return String(id);
}

function ticker(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9.^-]{1,40}$/.test(value)) throw new Error("Invalid ticker");
  return value.toUpperCase();
}

function strategy(value, fallback) {
  const selected = value ?? fallback;
  if (!["rsi", "ma_cross"].includes(selected)) throw new Error("strategy must be rsi or ma_cross");
  return selected;
}

export function registerEngineTools(ctx, defineTool, quant = runQuant) {
  const store = ctx.tradingWorkbench;
  const register = spec => ctx.tools.register(defineTool({ ...spec, output: spec.output ?? jsonOutput }));
  register({
    name: "run_trading_analysis",
    description: "在 Harness 中启动投研记录，返回 run id 和后续工作流；不私自调用 LLM、不生成无数据报告。随后加载 trading-agents skill，在当前会话/子代理完成取数与12角色研究，再用 research_publish 发布。",
    parameters: { ticker: { type: "string", required: true, description: "标的代码" } },
    async execute(args, exec) {
      const run = store.beginResearch({ ticker: ticker(args.ticker), session_id: sessionId(exec) });
      return { ...run,
        previous_reports: store.snapshot().reports.filter(row => row.ticker === run.ticker).slice(0, 3),
        next_step: "由 Harness 加载 trading-agents skill，使用原生数据工具和子代理完成六阶段研究。读取当前账户持仓、交易频率，标注来源及时间；数据不足不得编造。完成后调用 research_publish(run_id, ticker, rating, report, sources)。本工具只启动记录，尚未产出研报。",
      };
    },
  });
  register({
    name: "research_publish",
    description: "把当前 Harness 会话已经完成、注明数据来源的研报发布到工作台。不是启动新一轮分析，也不下单。",
    parameters: {
      run_id: { type: "string", required: true },
      ticker: { type: "string", required: true },
      rating: { type: "string", required: true, enum: ["Buy", "Overweight", "Hold", "Underweight", "Sell"] },
      report: { type: "string", required: true, description: "完整 Markdown 研报，含现有持仓、频率、风险、数据不足及免责声明" },
      sources: { type: "array", required: true, items: {
        type: "object", additionalProperties: false, properties: {
          name: { type: "string", required: true },
          as_of: { type: "string", required: true, description: "数据日期或 ISO 时间" },
          reference: { type: "string", required: true, description: "来源 URL 或 Harness 工具记录引用" },
        },
      } },
    },
    output: { ...jsonOutput, render: (_args, value) => [{ type: "text", text: value.report }] },
    async execute(args, exec) { return store.publishResearch(args, sessionId(exec)); },
  });
  register({
    name: "trading_status",
    description: "读取工作台最新研报、量化预览、账户模式和 Harness 已观察到的交易响应。没有券商数据时明确为空，不把本地模拟台账当真实资产。",
    parameters: {},
    async execute() { return store.snapshot(); },
  });
  register({
    name: "quant_signal",
    description: "计算 A 股日线 RSI/双均线信号并保存到量化预览；仅预览，不下单。"
      + " include_sentiment 时附带情绪/舆情参考输入（与研报共用 fin-data 渠道，"
      + " 只并列展示、不参与信号计算）。",
    parameters: {
      ticker: { type: "string", required: true },
      strategy: { type: "string", enum: ["rsi", "ma_cross"] },
      include_sentiment: { type: "boolean" },
    },
    async execute(args, exec) {
      const mode = store.readMode();
      const argv = ["signal", "--ticker", ticker(args.ticker),
        "--strategy", strategy(args.strategy, "rsi")];
      if (args.include_sentiment === true) argv.push("--sentiment");
      const result = await quant("engine.py", argv, exec.signal);
      store.recordPreview("signal", result, mode);
      return result;
    },
  });
  register({
    name: "quant_backtest",
    description: "A 股日线量化回测及工作台预览，下一交易日开盘成交、含费用；不是收益保证，不触发任何订单。",
    parameters: {
      ticker: { type: "string", required: true },
      strategy: { type: "string", enum: ["rsi", "ma_cross"] },
      fast: { type: "integer" }, slow: { type: "integer" }, start: { type: "string" },
    },
    async execute(args, exec) {
      const mode = store.readMode();
      const start = args.start ?? "2023-01-01";
      if (!/^\d{4}-\d{2}-\d{2}$/.test(start) || !Number.isFinite(Date.parse(start))) throw new Error("Invalid start date");
      const argv = ["--ticker", ticker(args.ticker), "--source", "sina", "--strategy",
        strategy(args.strategy, "ma_cross"), "--start", start];
      for (const key of ["fast", "slow"]) {
        if (args[key] !== undefined) {
          if (!Number.isInteger(args[key]) || args[key] < 1) throw new Error(`Invalid ${key} period`);
          argv.push(`--${key}`, String(args[key]));
        }
      }
      const result = await quant("backtest.py", argv, exec.signal);
      store.recordPreview("backtest", result, mode);
      return result;
    },
  });
  register({
    name: "quant_report",
    description: "读取本地模拟量化台账并保存预览；不代表富途模拟盘或实盘资金，live 模式不生成虚拟余额。",
    parameters: {},
    async execute(_args, exec) {
      const mode = store.readMode();
      if (mode !== "sim") throw new Error("本地台账仅支持 sim；实盘资产请在 Harness 使用富途账户查询工具");
      const result = await quant("engine.py", ["report"], exec.signal);
      store.recordPreview("ledger", result, mode);
      return result;
    },
  });
  register({
    name: "quant_switch",
    description: "切回模拟盘。实盘切换必须由用户在工作台输入确认文字；模式切换永不授权下单。",
    parameters: { mode: { type: "string", required: true, enum: ["sim", "live"] } },
    async execute(args) {
      if (args.mode !== "sim") throw new Error("请由用户在工作台切换并确认实盘；模型不能代替用户确认");
      return store.switchMode({ mode: "sim", expected_mode: store.readMode() });
    },
  });
}
