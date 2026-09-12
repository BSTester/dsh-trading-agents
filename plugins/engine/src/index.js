import { defineTool } from "@deepseek-ai/dsh-tools";
import { registerEngineTools } from "./tools.js";
import { installTradingPolicy } from "./policy.js";

export const name = "trading-engine";
export const inject = ["tools", "tradingWorkbench"];

export function apply(ctx) {
  registerEngineTools(ctx, defineTool);
  installTradingPolicy(ctx);
}
