// @bstester/dsh-trading-engine — deterministic TradingAgents pipeline.
//
// Registers `run_trading_analysis`：直调 llm 服务走完 12 角色流水线，
// 决策追加写入 ~/.dsh/trading-memory.md。输出结构化投研报告。

import { defineTool } from "@deepseek-ai/dsh-tools";
import os from "node:os";
import path from "node:path";
import { readFile, mkdir, appendFile } from "node:fs/promises";

const MEMORY_PATH = path.join(os.homedir(), ".dsh", "trading-memory.md");

/** 从 llm 流收集纯文本输出。 */
async function complete(ctx, prompt, system = "", maxTokens = 2000) {
  const model = ctx.get("agentDefaultModel");
  const selection = model && typeof model.currentSelection === "function"
    ? model.currentSelection() : {};
  const provider = selection.provider ?? process.env.DSH_LLM_PROVIDER ?? "deepseek";
  const modelId = selection.model ?? process.env.DSH_LLM_MODEL ?? "deepseek-chat";
  const llm = ctx.get("llm");
  if (llm === undefined) throw new Error("llm service unavailable");

  const messages = [{
    id: `trading-msg-${Date.now()}`,
    role: "user",
    content: [{ type: "text", text: prompt }],
    source: { kind: "user" },
  }];

  let text = "";
  for await (const chunk of llm.stream({
    provider,
    model: modelId,
    messages,
    system,
    maxTokens,
  })) {
    if (chunk.type === "text-delta") text += chunk.text;
    if (chunk.type === "finish" && chunk.reason.kind === "error") {
      throw new Error(`llm error: ${chunk.reason.failure?.message ?? "unknown"}`);
    }
  }
  return text.trim();
}

/** 记忆：读该标的历史教训 + 写 pending 决策。 */
async function readMemory(ticker) {
  try {
    const all = await readFile(MEMORY_PATH, "utf8");
    const hits = all.split(/<!-- ENTRY_END -->/).filter((b) => b.includes(ticker)).slice(-3);
    return hits.length ? hits.join("\n").slice(-1500) : "";
  } catch {
    return "";
  }
}

async function writeMemory(ticker, rating, thesis) {
  await mkdir(path.dirname(MEMORY_PATH), { recursive: true });
  const date = new Date().toISOString().slice(0, 10);
  const entry = `[${date} | ${ticker} | ${rating} | pending]\nDECISION: ${thesis}\n<!-- ENTRY_END -->\n`;
  await appendFile(MEMORY_PATH, entry, "utf8");
}

const ROLES = {
  market: "你是市场分析师。选最多8个互补技术指标（均线/MACD/RSI/布林/ATR/VWMA），基于行情写趋势报告，观点具体可执行，≤300字。",
  sentiment: "你是社交舆情分析师。分析该标的近一周社交媒体讨论、新闻与公众情绪，写情绪走向及交易含义，≤300字。",
  news: "你是新闻分析师。覆盖公司新闻与宏观，写当前世界状态相关报告，≤300字。",
  fundamentals: "你是基本面分析师。覆盖公司画像/财务/财务历史，给出基本面图景，≤300字。",
  bull: "你是看多研究员。用增长潜力/竞争优势/积极指标论证买入，并逐点反驳对方最新论点，对话式，≤200字。",
  bear: "你是看空研究员。用风险/劣势/负面指标论证回避，逐点批判对方，对话式，≤200字。",
  manager: "你是研究经理兼辩论主持人。给交易员清晰投资计划。评级必须恰选 Buy/Overweight/Hold/Underweight/Sell 之一，证据均衡时才 Hold。输出：评级+理由+战略动作。",
  trader: "你是交易员。基于研究计划给提案：Action(Buy/Hold/Sell)/Reasoning(2-4句)/Entry/Stop/Position Sizing，以 FINAL TRANSACTION PROPOSAL: **X** 结尾。",
  aggressive: "你是激进风控分析师。champion 高风险高回报机会，逐点反驳保守与中立，论证为何激进最优，≤200字。",
  conservative: "你是保守风控分析师。批判高风险元素，逐点反驳激进与中立，论证低风险调整更可持续，≤200字。",
  neutral: "你是中立风控分析师。指出两派过度之处，给适度折中方案（如减半仓位/分批），≤200字。",
  pm: "你是组合经理。综合风控辩论交付最终决策。评级恰选 Buy/Overweight/Hold/Underweight/Sell，果断、锚定证据，注入历史教训。输出：评级/操作/相对持仓/交易频率/风险回报比/期限/核心论点(≤5)/主要风险(≤3)/历史教训/免责。",
};

export const name = "trading-engine";
export const inject = ["tools"];

export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: "run_trading_analysis",
    description:
      "运行 TradingAgents 12角色投研流水线（四分析师→多空辩论→研究经理→交易员→三派风控→组合经理），直调 LLM，输出结构化投资建议并写入决策记忆。标的如 00700.HK / AAPL / 600519。",
    parameters: {
      ticker: { type: "string", required: true, description: "标的代码，如 00700.HK / AAPL / 600519" },
      name: { type: "string", description: "公司名（可选）" },
      debate_rounds: { type: "number", description: "多空辩论轮数，默认 1" },
    },
    output: {
      schema: { type: "object", additionalProperties: true },
      render: (_a, v) => [{ type: "text", text: v.report }],
    },
    async execute(args) {
      const ticker = String(args.ticker);
      const name = args.name ? String(args.name) : ticker;
      const rounds = Math.min(Math.max(args.debate_rounds ?? 1, 1), 3);
      const ctxLabel = `${name}（${ticker}）`;

      const lessons = await readMemory(ticker);

      // 阶段1：四分析师
      const analystKeys = ["market", "sentiment", "news", "fundamentals"];
      const reports = {};
      for (const key of analystKeys) {
        reports[key] = await complete(ctx,
          `标的：${ctxLabel}。请基于可用数据（可先自行调用行情/新闻工具）给出你的分析。`,
          ROLES[key], 1200);
      }
      const reportsText = Object.entries(reports)
        .map(([k, v]) => `${k} 分析师：\n${v}`).join("\n\n");

      // 阶段2：多空辩论
      let bull = await complete(ctx, `四份报告：\n${reportsText}\n请发表你的看多论证。`, ROLES.bull, 800);
      let bear = "";
      for (let i = 0; i < rounds; i++) {
        bear = await complete(ctx, `四份报告：\n${reportsText}\n看多方最新发言：${bull}\n请反驳并发表看空论证。`, ROLES.bear, 800);
        if (i < rounds - 1) {
          bull = await complete(ctx, `四份报告：\n${reportsText}\n看空方最新发言：${bear}\n请反驳看空方。`, ROLES.bull, 800);
        }
      }
      const debate = `Bull: ${bull}\n\nBear: ${bear}`;

      // 阶段3：研究经理
      const plan = await complete(ctx, `辩论记录：\n${debate}\n请裁决并给出投资计划。`, ROLES.manager, 800);

      // 阶段4：交易员
      const proposal = await complete(ctx, `投资计划：\n${plan}\n请给出交易提案。`, ROLES.trader, 800);

      // 阶段5：三方风控
      const agg = await complete(ctx, `交易员决策：\n${proposal}\n${reportsText}`, ROLES.aggressive, 800);
      const con = await complete(ctx, `交易员决策：\n${proposal}\n激进派：${agg}`, ROLES.conservative, 800);
      const neu = await complete(ctx, `交易员决策：\n${proposal}\n激进派：${agg}\n保守派：${con}`, ROLES.neutral, 800);
      const riskDebate = `激进：${agg}\n保守：${con}\n中立：${neu}`;

      // 阶段6：组合经理终审
      const pm = await complete(ctx,
        `研究计划：\n${plan}\n交易提案：\n${proposal}\n风控辩论：\n${riskDebate}\n历史教训：\n${lessons || "无"}\n请给最终决策。`,
        ROLES.pm, 1200);

      // 记忆落盘（从 pm 文本粗提评级）
      const ratingMatch = pm.match(/Buy|Overweight|Hold|Underweight|Sell/);
      const rating = ratingMatch ? ratingMatch[0] : "Hold";
      await writeMemory(ticker, rating, pm.slice(0, 200).replace(/\n+/g, " "));

      const report =
`## 📊 TradingAgents 投研：${ctxLabel}

**最终决策**
${pm}

> 本分析为 AI 研究输出，不构成投资建议。`

      return { report, rating, phases: { reports, debate, plan, proposal, riskDebate } };
    },
  }));
}
