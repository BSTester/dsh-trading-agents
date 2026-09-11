// @bstester/dsh-trading-agents-plugin — v2 deterministic orchestration engine (scaffold).
//
// v1 ships the workflow as a skill (see ../../skills/trading-agents/SKILL.md):
// the model drives the six-role pipeline itself. This package is the planned
// v2: a Cordis plugin that runs the same pipeline deterministically —
//
//   - registers a `run_trading_analysis` model Tool on ctx.tools;
//   - executes the six-role state machine with direct llm service calls
//     (quick model for analysts/debaters, deep model for managers);
//   - pulls data through the mcp__futu__ tools registered by dsh-mcp-client;
//   - owns the append-only decision memory (.tradingagents/memory.md).
//
// It is intentionally not implemented yet: validate the workflow quality with
// the skill-driven v1 first, then port it here. See docs/architecture.md.

export default function createTradingAgentsPlugin() {
  return {
    name: 'dsh-trading-agents-plugin',
    apply(ctx) {
      // TODO(v2): ctx.tools.register(run_trading_analysis definition)
      void ctx
    },
  }
}
