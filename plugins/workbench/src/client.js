// Native Harness module-factory entry: use the host's React, not a second copy.
window.__ModuleLoader__.load({
  id: "@bstester/dsh-trading-workbench",
  factory: (require) => {
    const React = require("react");
    const h = React.createElement;
    const panelStyle = {
      position: "fixed", right: 16, bottom: 60, width: "min(680px, calc(100vw - 32px))",
      maxHeight: "75vh", overflow: "auto", padding: 18, borderRadius: 12,
      border: "1px solid GrayText", background: "Canvas", color: "CanvasText",
      boxShadow: "0 8px 32px #0004", pointerEvents: "auto", fontSize: 14,
    };
    const preStyle = { whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 12 };

    async function request(rpc, endpoint, payload, signal) {
      if (!["snapshot", "switch-mode"].includes(endpoint)) throw new Error("Unsupported workbench operation");
      const result = await rpc.call("/api", `trading-workbench/${endpoint}`, payload, signal);
      if (!result.ok) throw new Error(result.error.message);
      return result.value;
    }

    function ToolCard({ block }) {
      const settled = Array.isArray(block?.content);
      const content = settled ? block.content.filter(item => item.type === "text").map(item => item.text).join("\n")
        : "Harness 正在处理…";
      return h("details", { style: { padding: 12, border: "1px solid GrayText", borderRadius: 8 } },
        h("summary", null, block?.isError ? "交易工作台 · 操作失败" : "交易工作台 · 结果"),
        h("pre", { style: preStyle }, content));
    }

    function Source({ source }) {
      const link = /^https?:\/\//i.test(source.reference);
      return h("li", null, `${source.name} · ${source.as_of} · `,
        link ? h("a", { href: source.reference, target: "_blank", rel: "noreferrer" }, source.reference) : source.reference);
    }

    function Reports({ snapshot }) {
      return h("section", null, h("h3", null, "研报结果"),
        snapshot.runs.filter(run => run.status === "running").map(run =>
          h("p", { key: run.id }, `${run.ticker} · 待 Harness 完成并发布 · ${run.started_at}`)),
        !snapshot.reports.length && h("p", null, "暂无研报。在 Harness 中提出分析请求，完成后会在这里展示。"),
        snapshot.reports.map(report => h("details", { key: report.id },
          h("summary", null, `${report.ticker} · ${report.rating} · ${report.published_at}`),
          h("pre", { style: preStyle }, report.report),
          h("ul", null, report.sources.map((source, index) => h(Source, { key: index, source }))))));
    }

    function Activity({ snapshot }) {
      return h("section", null, h("h3", null, "交易动态"),
        h("p", null, snapshot.broker
          ? `最近响应：${snapshot.broker.at}。响应不等于成交，状态以券商查询为准。`
          : "暂无该模式的券商响应；未连接不表示资产为零。"),
        snapshot.activity.slice(0, 30).map(row => h("details", { key: row.id },
          h("summary", null, `${row.at} · ${row.tool ?? row.kind}${row.is_error ? " · 失败" : ""}`),
          h("pre", { style: preStyle }, JSON.stringify(row.value ?? row, null, 2)))));
    }

    function Previews({ snapshot }) {
      return h("section", null, h("h3", null, "量化预览"),
        h("p", null, "本地计算／模拟结果，不代表券商资产或成交。请在 Harness 请求信号、回测或台账。"),
        !snapshot.previews.length && h("p", null, "暂无量化预览"),
        snapshot.previews.slice(0, 20).map(row => h("details", { key: row.id },
          h("summary", null, `${row.kind} · ${row.value?.ticker ?? ""} · ${row.at}`),
          h("pre", { style: preStyle }, JSON.stringify(row.value, null, 2)))));
    }

    function Dashboard({ rpc }) {
      const [open, setOpen] = React.useState(false);
      const [snapshot, setSnapshot] = React.useState(null);
      const [error, setError] = React.useState("");
      const [switchError, setSwitchError] = React.useState("");
      const [confirmation, setConfirmation] = React.useState("");
      const [switching, setSwitching] = React.useState(false);
      const [revision, setRevision] = React.useState(0);
      const generation = React.useRef(0);
      React.useEffect(() => {
        if (!open || switching) return;
        const controller = new AbortController();
        const current = ++generation.current;
        let timer;
        const refresh = async () => {
          try {
            const next = await request(rpc, "snapshot", {}, controller.signal);
            if (!controller.signal.aborted && generation.current === current) {
              setSnapshot(next);
              setError("");
            }
          } catch (failure) {
            if (!controller.signal.aborted && generation.current === current) setError(failure.message);
          } finally {
            if (!controller.signal.aborted) timer = setTimeout(refresh, 3000);
          }
        };
        refresh();
        return () => { controller.abort(); clearTimeout(timer); };
      }, [open, switching, revision, rpc]);

      const switchMode = async (mode) => {
        const expected = snapshot.mode;
        generation.current += 1;
        setSwitching(true);
        setSnapshot(null);
        setError("");
        setSwitchError("");
        try {
          await request(rpc, "switch-mode", { mode, expected_mode: expected,
            ...(mode === "live" ? { confirmation } : {}) });
          setConfirmation("");
        } catch (failure) {
          setSwitchError(failure.message);
        } finally {
          setSwitching(false);
          setRevision(value => value + 1);
        }
      };
      return h(React.Fragment, null,
        h("button", { type: "button", onClick: () => setOpen(value => !value), "aria-expanded": open,
          style: { position: "fixed", right: 16, bottom: 16, pointerEvents: "auto", padding: "8px 14px" } },
        open ? "收起交易工作台" : "交易工作台"),
        open && h("section", { role: "dialog", "aria-label": "交易工作台", style: panelStyle },
          h("h2", null, "交易工作台"),
          h("p", null, "这里只展示结果与切换账户模式；所有对话、分析请求及交易指令仍在 Harness 中下达。"),
          switchError && h("p", { role: "alert", style: { color: "#c33" } }, `模式切换失败：${switchError}`),
          error && h("p", { role: "alert", style: { color: "#c33" } }, `更新失败：${error}。下方如有数据，为上次快照。`),
          !snapshot && h("p", null, switching ? "正在切换模式…" : "正在读取工作台…"),
          snapshot && h(React.Fragment, null,
            snapshot.recording_error && h("p", { role: "alert" }, snapshot.recording_error),
            snapshot.pending_observations > 0 && h("p", { role: "status" },
              `${snapshot.pending_observations} 条响应已暂存，等待写锁释放后更新；不要因此重试下单。`),
            h("strong", { style: { color: snapshot.mode === "live" ? "#c33" : "inherit" } },
              snapshot.mode === "live" ? "实盘 LIVE · 真实账户" : "模拟盘 SIM"),
            h("p", null, `快照时间：${snapshot.generated_at} · 打开面板时每 3 秒刷新 · 进行中的账户调用：${snapshot.in_flight}`),
            h("p", null, "切换模式不授权下单；重启会保留当前模式。"),
            snapshot.mode === "sim"
              ? h("div", null,
                h("label", null, "切换实盘前输入「确认实盘」 ",
                  h("input", { value: confirmation, onChange: event => setConfirmation(event.target.value),
                    autoComplete: "off", "aria-label": "确认实盘" })),
                h("button", { type: "button", disabled: confirmation !== "确认实盘" || snapshot.in_flight > 0,
                  onClick: () => switchMode("live") }, "切换到实盘"))
              : h("button", { type: "button", disabled: snapshot.in_flight > 0, onClick: () => switchMode("sim") }, "切回模拟盘"),
            h("button", { type: "button", onClick: () => setRevision(value => value + 1), style: { marginLeft: 8 } }, "刷新"),
            h(Reports, { snapshot }), h(Activity, { snapshot }), h(Previews, { snapshot }),
            h("p", null, snapshot.notice))));
    }

    function apply(ctx) {
      const rpc = ctx.connection.rpc;
      ctx.slots.inject("shell.overlay", () => ctx.slots.register(
        { name: "shell.overlay", id: "trading-workbench" },
        () => h(Dashboard, { rpc })));
      for (const key of ["research_publish", "run_trading_analysis", "quant_signal", "quant_backtest", "quant_report"]) {
        ctx.slots.inject("tool.call.toolview", () => ctx.slots.register(
          { name: "tool.call.toolview", key }, ToolCard));
      }
    }

    return { inject: ["slots", "connection"], apply, request };
  },
});
