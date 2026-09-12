// Native Harness module-factory entry: use the host's React, not a second copy.
//
// 交易工作台 Client：右下角浮动入口 + 面板（研报 / 交易动态 / 量化预览 / 模式切换）。
// 样式注入到 document.head 一次（带 id 守卫，随插件卸载移除），全部使用 Harness
// 主题令牌 --dsw-alias-*，缺失时回退到系统色，保证浅色/深色模式都正常。

window.__ModuleLoader__.load({
  id: "@bstester/dsh-trading-workbench",
  factory: (require) => {
    const React = require("react");
    const h = React.createElement;

    const STYLE_ID = "dsh-trading-workbench-style";
    const CSS = `
.tw-fab {
  position: fixed; right: 18px; bottom: 18px; z-index: 60;
  display: inline-flex; align-items: center; gap: 8px;
  padding: 10px 16px; border-radius: 999px; cursor: pointer;
  font-size: 13px; font-weight: 600; letter-spacing: .2px;
  color: var(--dsw-alias-label-primary, CanvasText);
  background: var(--dsw-alias-button-elevated-fill, ButtonFace);
  border: 1px solid var(--dsw-alias-border-l2, GrayText);
  box-shadow: 0 6px 20px rgba(0,0,0,.18);
  transition: transform .12s ease, background .12s ease;
}
.tw-fab:hover { transform: translateY(-1px); background: var(--dsw-alias-button-floating-hover, var(--dsw-alias-interactive-bg-hover, ButtonFace)); }
.tw-fab:active { transform: translateY(0); }
.tw-fab-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--dsw-alias-state-success-primary, #2ea043); }
.tw-fab-dot.is-live { background: var(--dsw-alias-state-error-primary, #d1242f); }

.tw-panel {
  position: fixed; right: 18px; bottom: 68px; z-index: 60;
  width: min(760px, calc(100vw - 36px)); max-height: 78vh;
  display: flex; flex-direction: column; overflow: hidden;
  border-radius: 14px;
  background: var(--dsw-alias-bg-layer-2, Canvas);
  color: var(--dsw-alias-label-primary, CanvasText);
  border: 1px solid var(--dsw-alias-border-l2, GrayText);
  box-shadow: 0 18px 48px rgba(0,0,0,.28);
  font-size: 13px; line-height: 1.55;
}
.tw-header {
  display: flex; align-items: center; gap: 10px;
  padding: 14px 16px; border-bottom: 1px solid var(--dsw-alias-border-l1, GrayText);
  background: var(--dsw-alias-bg-layer-3, Canvas);
}
.tw-title { font-size: 15px; font-weight: 700; margin: 0; }
.tw-badge {
  margin-left: auto; padding: 3px 10px; border-radius: 999px;
  font-size: 11px; font-weight: 700; letter-spacing: .6px; text-transform: uppercase;
  background: var(--dsw-alias-state-success-primary, #2ea043); color: #fff;
}
.tw-badge.is-live { background: var(--dsw-alias-state-error-primary, #d1242f); }
.tw-body { padding: 14px 16px 18px; overflow: auto; display: flex; flex-direction: column; gap: 14px; }
.tw-body::-webkit-scrollbar { width: 10px; }
.tw-body::-webkit-scrollbar-thumb { background: var(--dsw-alias-scrollbar-hover-l1, rgba(128,128,128,.4)); border-radius: 6px; }

.tw-hint { margin: 0; color: var(--dsw-alias-label-tertiary, GrayText); font-size: 12px; }
.tw-meta { margin: 0; color: var(--dsw-alias-label-secondary, GrayText); font-size: 12px; }
.tw-alert {
  margin: 0; padding: 8px 10px; border-radius: 8px; font-size: 12px;
  background: var(--dsw-alias-interactive-bg-hover-danger, rgba(209,36,47,.12));
  color: var(--dsw-alias-label-error, #d1242f);
  border: 1px solid var(--dsw-alias-state-error-primary, rgba(209,36,47,.4));
}
.tw-status {
  margin: 0; padding: 8px 10px; border-radius: 8px; font-size: 12px;
  background: var(--dsw-alias-interactive-bg-hover, rgba(128,128,128,.10));
  color: var(--dsw-alias-label-secondary, GrayText);
}

.tw-card {
  border: 1px solid var(--dsw-alias-border-l1, GrayText); border-radius: 10px;
  background: var(--dsw-alias-bg-layer-1, Canvas); overflow: hidden;
}
.tw-card-head {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 12px; font-weight: 600; font-size: 13px;
  border-bottom: 1px solid var(--dsw-alias-border-l1, GrayText);
  background: var(--dsw-alias-bg-layer-2, Canvas);
}
.tw-count {
  margin-left: auto; font-weight: 600; font-size: 11px; padding: 1px 8px; border-radius: 999px;
  color: var(--dsw-alias-label-secondary, GrayText);
  background: var(--dsw-alias-interactive-bg-hover, rgba(128,128,128,.14));
}
.tw-card-body { padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }

.tw-item { border: 1px solid var(--dsw-alias-border-l1, GrayText); border-radius: 8px; background: var(--dsw-alias-bg-base, Canvas); }
.tw-item > summary {
  cursor: pointer; padding: 8px 10px; font-size: 12px; font-weight: 600;
  list-style: none; display: flex; align-items: center; gap: 8px;
}
.tw-item > summary::-webkit-details-marker { display: none; }
.tw-item > summary::before { content: "▸"; color: var(--dsw-alias-label-tertiary, GrayText); transition: transform .12s ease; }
.tw-item[open] > summary::before { transform: rotate(90deg); }
.tw-item > summary:hover { background: var(--dsw-alias-interactive-bg-hover, rgba(128,128,128,.10)); }
.tw-item-body { padding: 0 10px 10px; }
.tw-tag {
  font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 4px;
  background: var(--dsw-alias-interactive-bg-hover, rgba(128,128,128,.16));
  color: var(--dsw-alias-label-secondary, GrayText);
}
.tw-tag.rating-buy { background: var(--dsw-alias-state-success-primary, #2ea043); color: #fff; }
.tw-tag.rating-sell { background: var(--dsw-alias-state-error-primary, #d1242f); color: #fff; }
.tw-tag.rating-hold { background: var(--dsw-alias-state-warn-label, #9a6700); color: #fff; }

.tw-pre {
  margin: 6px 0 0; padding: 10px; border-radius: 8px; font-size: 12px;
  white-space: pre-wrap; overflow-wrap: anywhere;
  font-family: var(--ds-font-family-code, ui-monospace, SFMono-Regular, Menlo, monospace);
  background: var(--dsw-alias-bg-base, Canvas);
  border: 1px solid var(--dsw-alias-border-l1, GrayText);
  max-height: 320px; overflow: auto;
}
.tw-sources { margin: 8px 0 0; padding-left: 18px; font-size: 12px; color: var(--dsw-alias-label-secondary, GrayText); }
.tw-sources a { color: var(--dsw-alias-label-primary-bluish, LinkText); }

.tw-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tw-input {
  flex: 1 1 200px; min-width: 180px; padding: 7px 10px; border-radius: 8px; font-size: 12px;
  color: var(--dsw-alias-label-primary, CanvasText);
  background: var(--dsw-alias-bg-base, Field);
  border: 1px solid var(--dsw-alias-border-l2, GrayText);
}
.tw-input:focus { outline: 2px solid var(--dsw-alias-button-info-fill, Highlight); outline-offset: 1px; }
.tw-btn {
  padding: 7px 14px; border-radius: 8px; cursor: pointer; font-size: 12px; font-weight: 600;
  color: var(--dsw-alias-label-primary, ButtonText);
  background: var(--dsw-alias-button-tool-bar-fill, ButtonFace);
  border: 1px solid var(--dsw-alias-border-l2, GrayText);
  transition: background .12s ease;
}
.tw-btn:hover:not(:disabled) { background: var(--dsw-alias-button-tool-bar-hover, var(--dsw-alias-interactive-bg-hover, ButtonFace)); }
.tw-btn:disabled { opacity: .45; cursor: not-allowed; }
.tw-btn.is-primary { background: var(--dsw-alias-button-info-fill, Highlight); color: var(--dsw-alias-label-primary-foreground, HighlightText); border-color: transparent; }
.tw-btn.is-danger { background: var(--dsw-alias-state-error-primary, #d1242f); color: #fff; border-color: transparent; }
.tw-btn.is-ghost { background: transparent; }
.tw-empty { margin: 0; color: var(--dsw-alias-label-tertiary, GrayText); font-size: 12px; }

.tw-toolcard { border: 1px solid var(--dsw-alias-border-l1, GrayText); border-left: 3px solid var(--dsw-alias-button-info-fill, Highlight); border-radius: 8px; background: var(--dsw-alias-bg-layer-1, Canvas); }
.tw-toolcard > summary { cursor: pointer; padding: 8px 10px; font-size: 12px; font-weight: 600; }
.tw-toolcard.is-error { border-left-color: var(--dsw-alias-state-error-primary, #d1242f); }
`;

    function injectStyles(ctx) {
      const styles = ctx.get ? ctx.get("styles") : undefined;
      if (styles && typeof styles.insert === "function") return styles.insert(CSS);
      if (typeof document === "undefined" || !document.head) return undefined;
      if (document.getElementById(STYLE_ID)) return undefined;
      const tag = document.createElement("style");
      tag.id = STYLE_ID;
      tag.textContent = CSS;
      document.head.appendChild(tag);
      return () => { tag.remove(); };
    }

    async function request(rpc, endpoint, payload, signal) {
      if (!["snapshot", "switch-mode"].includes(endpoint)) throw new Error("Unsupported workbench operation");
      const result = await rpc.call("/api", `trading-workbench/${endpoint}`, payload, signal);
      if (!result.ok) throw new Error(result.error.message);
      return result.value;
    }

    const RATING_CLASS = { Buy: "rating-buy", Overweight: "rating-buy", Sell: "rating-sell", Underweight: "rating-sell", Hold: "rating-hold" };

    function ToolCard({ block }) {
      const settled = Array.isArray(block?.content);
      const content = settled
        ? block.content.filter(item => item.type === "text").map(item => item.text).join("\n")
        : "Harness 正在处理…";
      return h("details", { className: `tw-toolcard${block?.isError ? " is-error" : ""}` },
        h("summary", null, block?.isError ? "交易工作台 · 操作失败" : "交易工作台 · 结果"),
        h("div", { className: "tw-item-body" }, h("pre", { className: "tw-pre" }, content)));
    }

    function Source({ source }) {
      const link = /^https?:\/\//i.test(source.reference);
      return h("li", null, `${source.name} · ${source.as_of} · `,
        link ? h("a", { href: source.reference, target: "_blank", rel: "noreferrer" }, source.reference) : source.reference);
    }

    function Card({ title, count, empty, children }) {
      return h("section", { className: "tw-card" },
        h("div", { className: "tw-card-head" }, title,
          count !== undefined && h("span", { className: "tw-count" }, String(count))),
        h("div", { className: "tw-card-body" }, empty ? h("p", { className: "tw-empty" }, empty) : children));
    }

    function Reports({ snapshot }) {
      const running = snapshot.runs.filter(run => run.status === "running");
      const hasContent = running.length > 0 || snapshot.reports.length > 0;
      return h(Card, { title: "研报结果", count: snapshot.reports.length,
        empty: hasContent ? undefined : "暂无研报。在 Harness 中提出分析请求，完成后会在这里展示。" },
        running.map(run => h("div", { key: run.id, className: "tw-item" },
          h("div", { className: "tw-item-body" },
            h("span", { className: "tw-tag" }, "进行中"),
            ` ${run.ticker} · 待 Harness 完成并发布 · ${run.started_at}`))),
        snapshot.reports.map(report => h("details", { key: report.id, className: "tw-item" },
          h("summary", null,
            h("span", { className: `tw-tag ${RATING_CLASS[report.rating] ?? ""}` }, report.rating),
            `${report.ticker}`,
            h("span", { className: "tw-meta" }, report.published_at)),
          h("div", { className: "tw-item-body" },
            h("pre", { className: "tw-pre" }, report.report),
            h("ul", { className: "tw-sources" }, report.sources.map((source, index) => h(Source, { key: index, source })))))));
    }

    function Activity({ snapshot }) {
      return h(Card, { title: "交易动态", count: snapshot.activity.length },
        h("p", { className: "tw-meta" }, snapshot.broker
          ? `最近响应：${snapshot.broker.at}。响应不等于成交，状态以券商查询为准。`
          : "暂无该模式的券商响应；未连接不表示资产为零。"),
        snapshot.activity.slice(0, 30).map(row => h("details", { key: row.id, className: "tw-item" },
          h("summary", null, `${row.at} · ${row.tool ?? row.kind}`,
            row.is_error ? h("span", { className: "tw-tag", style: { background: "var(--dsw-alias-state-error-primary, #d1242f)", color: "#fff" } }, "失败") : null),
          h("div", { className: "tw-item-body" },
            h("pre", { className: "tw-pre" }, JSON.stringify(row.value ?? row, null, 2))))));
    }

    function Previews({ snapshot }) {
      return h(Card, { title: "量化预览", count: snapshot.previews.length,
        empty: snapshot.previews.length ? undefined : "暂无量化预览" },
        h("p", { className: "tw-meta" }, "本地计算／模拟结果，不代表券商资产或成交。请在 Harness 请求信号、回测或台账。"),
        snapshot.previews.slice(0, 20).map(row => h("details", { key: row.id, className: "tw-item" },
          h("summary", null, `${row.kind}`, h("span", { className: "tw-tag" }, row.value?.ticker ?? "-"),
            h("span", { className: "tw-meta" }, row.at)),
          h("div", { className: "tw-item-body" },
            h("pre", { className: "tw-pre" }, JSON.stringify(row.value, null, 2))))));
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

      const live = snapshot?.mode === "live";
      return h(React.Fragment, null,
        h("button", { type: "button", className: "tw-fab", onClick: () => setOpen(value => !value),
          "aria-expanded": open, title: "交易工作台" },
          h("span", { className: `tw-fab-dot${live ? " is-live" : ""}` }),
          open ? "收起交易工作台" : "交易工作台"),
        open && h("section", { role: "dialog", "aria-label": "交易工作台", className: "tw-panel" },
          h("header", { className: "tw-header" },
            h("h2", { className: "tw-title" }, "交易工作台"),
            snapshot && h("span", { className: `tw-badge${live ? " is-live" : ""}` }, live ? "实盘 LIVE" : "模拟盘 SIM")),
          h("div", { className: "tw-body" },
            h("p", { className: "tw-hint" }, "这里只展示结果与切换账户模式；所有对话、分析请求及交易指令仍在 Harness 中下达。"),
            switchError && h("p", { role: "alert", className: "tw-alert" }, `模式切换失败：${switchError}`),
            error && h("p", { role: "alert", className: "tw-alert" }, `更新失败：${error}。下方如有数据，为上次快照。`),
            !snapshot && h("p", { className: "tw-status" }, switching ? "正在切换模式…" : "正在读取工作台…"),
            snapshot && h(React.Fragment, null,
              snapshot.recording_error && h("p", { role: "alert", className: "tw-alert" }, snapshot.recording_error),
              snapshot.pending_observations > 0 && h("p", { role: "status", className: "tw-status" },
                `${snapshot.pending_observations} 条响应已暂存，等待写锁释放后更新；不要因此重试下单。`),
              h("p", { className: "tw-meta" },
                `快照时间：${snapshot.generated_at} · 每 3 秒刷新 · 进行中的账户调用：${snapshot.in_flight}`),
              h("p", { className: "tw-meta" }, "切换模式不授权下单；重启会保留当前模式。"),
              h("div", { className: "tw-row" },
                snapshot.mode === "sim"
                  ? h(React.Fragment, null,
                    h("input", { className: "tw-input", value: confirmation, placeholder: "切换实盘前输入「确认实盘」",
                      onChange: event => setConfirmation(event.target.value), autoComplete: "off", "aria-label": "确认实盘" }),
                    h("button", { type: "button", className: "tw-btn is-danger",
                      disabled: confirmation !== "确认实盘" || snapshot.in_flight > 0,
                      onClick: () => switchMode("live") }, "切换到实盘"))
                  : h("button", { type: "button", className: "tw-btn is-primary", disabled: snapshot.in_flight > 0,
                      onClick: () => switchMode("sim") }, "切回模拟盘"),
                h("button", { type: "button", className: "tw-btn is-ghost",
                  onClick: () => setRevision(value => value + 1) }, "刷新")),
              h(Reports, { snapshot }), h(Activity, { snapshot }), h(Previews, { snapshot }),
              h("p", { className: "tw-hint" }, snapshot.notice)))));
    }

    function apply(ctx) {
      const disposeStyles = injectStyles(ctx);
      if (typeof ctx.effect === "function" && typeof disposeStyles === "function") {
        ctx.effect(() => disposeStyles);
      }
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
