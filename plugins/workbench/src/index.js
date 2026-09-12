// @bstester/dsh-trading-workbench — Client 半：投研流水线卡片。
//
// 在 tool.call.toolview 里以 key='run_trading_analysis' 注册卡片，
// 渲染该工具结果的投研报告（props.block 是已落定的 tool-result 节点）。
// 纯展示、不写任何宿主状态；报告文本由 Host 的 trading-engine 生成。

export function apply(ctx) {
  const slots = ctx.get("slots");
  if (slots === undefined) return;

  slots.inject("tool.call.toolview", () => slots.register(
    { name: "tool.call.toolview", key: "run_trading_analysis" },
    (props) => {
      const block = props.block;
      let text = "";
      if (block && block.type === "tool-result" && Array.isArray(block.content)) {
        text = block.content
          .filter((c) => c && c.type === "text")
          .map((c) => c.text)
          .join("\n");
      }
      if (block && block.type === "tool-call") {
        text = "分析进行中…";
      }
      return React.createElement(
        "div",
        {
          style: {
            padding: "12px 14px", border: "1px solid rgba(128,128,128,.25)",
            borderRadius: "8px", background: "rgba(255,255,255,.03)",
            fontSize: "13px", lineHeight: "1.5", whiteSpace: "pre-wrap",
          },
        },
        React.createElement(
          "div",
          { style: { fontWeight: 600, marginBottom: "6px", color: "#4f9" } },
          "📊 TradingAgents 投研"
        ),
        text,
      );
    },
  ));
}
