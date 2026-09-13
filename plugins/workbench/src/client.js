// 交易工作台 Client：抽屉式大盘面 + 分类导航 + canvas 动态图表（零依赖自绘）。
//
// 分工：工作台只做信息展示（行情图表、信号、组合、风险、执行、研究、事件、审计）。
// 研报生成与下单指令一律在 Harness 会话中完成（策略强制实盘审批）。
//
// 样式注入 document.head（id 守卫，随插件卸载移除），使用 Harness 主题令牌
// --dsw-alias-*，缺失时回退系统色，浅/深色自适应。

window.__ModuleLoader__.load({
  id: "@bstester/dsh-trading-workbench",
  factory: (require) => {
    const React = require("react");
    const h = React.createElement;

    const STYLE_ID = "dsh-trading-workbench-style";
    const CSS = `
.tw-fab{position:fixed;right:18px;bottom:18px;z-index:60;display:inline-flex;align-items:center;gap:8px;padding:10px 16px;border-radius:999px;cursor:pointer;font-size:13px;font-weight:600;color:var(--dsw-alias-label-primary,CanvasText);background:var(--dsw-alias-button-elevated-fill,ButtonFace);border:1px solid var(--dsw-alias-border-l2,GrayText);box-shadow:0 6px 20px rgba(0,0,0,.18);transition:transform .12s ease,background .12s ease}
.tw-fab:hover{transform:translateY(-1px);background:var(--dsw-alias-interactive-bg-hover,ButtonFace)}
.tw-dot{width:8px;height:8px;border-radius:50%;background:var(--dsw-alias-state-success-primary,#2ea043)}
.tw-dot.live{background:var(--dsw-alias-state-error-primary,#d1242f)}
.tw-scrim{position:fixed;inset:0;z-index:70;background:rgba(0,0,0,.42);opacity:0;pointer-events:none;transition:opacity .18s ease}
.tw-scrim.open{opacity:1;pointer-events:auto}
.tw-drawer{position:fixed;top:0;right:0;bottom:0;z-index:71;width:min(1180px,94vw);display:flex;flex-direction:column;background:var(--dsw-alias-bg-layer-1,Canvas);color:var(--dsw-alias-label-primary,CanvasText);border-left:1px solid var(--dsw-alias-border-l2,GrayText);box-shadow:-18px 0 48px rgba(0,0,0,.32);transform:translateX(102%);transition:transform .22s ease;font-size:13px;line-height:1.5}
.tw-drawer.open{transform:translateX(0)}
.tw-top{display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid var(--dsw-alias-border-l1,GrayText);background:var(--dsw-alias-bg-layer-2,Canvas)}
.tw-title{font-size:16px;font-weight:700;margin:0}
.tw-badge{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.5px;background:var(--dsw-alias-state-success-primary,#2ea043);color:#fff}
.tw-badge.live{background:var(--dsw-alias-state-error-primary,#d1242f)}
.tw-close{margin-left:auto;background:transparent;border:none;color:var(--dsw-alias-label-secondary,GrayText);font-size:20px;cursor:pointer;line-height:1;padding:4px 8px;border-radius:6px}
.tw-close:hover{background:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.15))}
.tw-main{flex:1;display:flex;min-height:0}
.tw-nav{width:132px;flex:0 0 132px;border-right:1px solid var(--dsw-alias-border-l1,GrayText);background:var(--dsw-alias-bg-layer-2,Canvas);padding:10px 8px;display:flex;flex-direction:column;gap:2px;overflow:auto}
.tw-nav-item{display:flex;align-items:center;gap:8px;padding:9px 10px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600;color:var(--dsw-alias-label-secondary,GrayText);border:none;background:transparent;text-align:left;width:100%}
.tw-nav-item:hover{background:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.12));color:var(--dsw-alias-label-primary,CanvasText)}
.tw-nav-item.active{background:var(--dsw-alias-interactive-bg-active,var(--dsw-alias-button-info-fill,Highlight));color:var(--dsw-alias-label-primary-foreground,HighlightText)}
.tw-nav-dot{width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.5}
.tw-content{flex:1;min-width:0;overflow:auto;padding:16px 18px 24px;display:flex;flex-direction:column;gap:14px}
.tw-content::-webkit-scrollbar{width:10px}
.tw-content::-webkit-scrollbar-thumb{background:var(--dsw-alias-scrollbar-hover-l1,rgba(128,128,128,.4));border-radius:6px}
.tw-hint{margin:0;color:var(--dsw-alias-label-tertiary,GrayText);font-size:12px}
.tw-meta{margin:0;color:var(--dsw-alias-label-secondary,GrayText);font-size:12px}
.tw-alert{margin:0;padding:8px 10px;border-radius:8px;font-size:12px;background:var(--dsw-alias-interactive-bg-hover-danger,rgba(209,36,47,.12));color:var(--dsw-alias-label-error,#d1242f);border:1px solid var(--dsw-alias-state-error-primary,rgba(209,36,47,.4))}
.tw-status{margin:0;padding:8px 10px;border-radius:8px;font-size:12px;background:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.1));color:var(--dsw-alias-label-secondary,GrayText)}
.tw-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tw-input{padding:7px 10px;border-radius:8px;font-size:12px;color:var(--dsw-alias-label-primary,CanvasText);background:var(--dsw-alias-bg-base,Field);border:1px solid var(--dsw-alias-border-l2,GrayText);width:120px}
.tw-input:focus{outline:2px solid var(--dsw-alias-button-info-fill,Highlight);outline-offset:1px}
.tw-btn{padding:6px 12px;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600;color:var(--dsw-alias-label-primary,ButtonText);background:var(--dsw-alias-button-tool-bar-fill,ButtonFace);border:1px solid var(--dsw-alias-border-l2,GrayText)}
.tw-btn:hover:not(:disabled){background:var(--dsw-alias-button-tool-bar-hover,var(--dsw-alias-interactive-bg-hover,ButtonFace))}
.tw-btn:disabled{opacity:.45;cursor:not-allowed}
.tw-btn.primary{background:var(--dsw-alias-button-info-fill,Highlight);color:var(--dsw-alias-label-primary-foreground,HighlightText);border-color:transparent}
.tw-btn.danger{background:var(--dsw-alias-state-error-primary,#d1242f);color:#fff;border-color:transparent}
.tw-btn.seg{padding:5px 10px;font-size:11px}
.tw-btn.seg.active{background:var(--dsw-alias-interactive-bg-active,var(--dsw-alias-button-info-fill,Highlight));color:var(--dsw-alias-label-primary-foreground,HighlightText)}
.tw-card{border:1px solid var(--dsw-alias-border-l1,GrayText);border-radius:10px;background:var(--dsw-alias-bg-layer-2,Canvas);overflow:hidden}
.tw-card-head{display:flex;align-items:center;gap:8px;padding:10px 12px;font-weight:600;font-size:13px;border-bottom:1px solid var(--dsw-alias-border-l1,GrayText)}
.tw-count{margin-left:auto;font-size:11px;font-weight:600;padding:1px 8px;border-radius:999px;color:var(--dsw-alias-label-secondary,GrayText);background:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.14))}
.tw-card-body{padding:12px;display:flex;flex-direction:column;gap:10px}
.tw-chart{width:100%;height:360px;display:block;border-radius:8px;background:var(--dsw-alias-bg-base,Canvas)}
.tw-chart.small{height:150px}
.tw-kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px}
.tw-kv-item{padding:8px 10px;border-radius:8px;background:var(--dsw-alias-bg-base,Canvas);border:1px solid var(--dsw-alias-border-l1,GrayText)}
.tw-kv-k{font-size:11px;color:var(--dsw-alias-label-tertiary,GrayText)}
.tw-kv-v{font-size:14px;font-weight:700;margin-top:2px}
.tw-item{border:1px solid var(--dsw-alias-border-l1,GrayText);border-radius:8px;background:var(--dsw-alias-bg-base,Canvas)}
.tw-item>summary{cursor:pointer;padding:8px 10px;font-size:12px;font-weight:600;display:flex;align-items:center;gap:8px;list-style:none}
.tw-item>summary::-webkit-details-marker{display:none}
.tw-item>summary::before{content:"▸";color:var(--dsw-alias-label-tertiary,GrayText);transition:transform .12s ease}
.tw-item[open]>summary::before{transform:rotate(90deg)}
.tw-item>summary:hover{background:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.1))}
.tw-item-body{padding:0 10px 10px}
.tw-tag{font-size:10px;font-weight:700;padding:1px 6px;border-radius:4px;background:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.16));color:var(--dsw-alias-label-secondary,GrayText)}
.tw-tag.buy{background:var(--dsw-alias-state-success-primary,#2ea043);color:#fff}
.tw-tag.sell{background:var(--dsw-alias-state-error-primary,#d1242f);color:#fff}
.tw-tag.hold{background:var(--dsw-alias-state-warn-label,#9a6700);color:#fff}
.tw-pre{margin:6px 0 0;padding:10px;border-radius:8px;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;font-family:var(--ds-font-family-code,ui-monospace,Menlo,monospace);background:var(--dsw-alias-bg-base,Canvas);border:1px solid var(--dsw-alias-border-l1,GrayText);max-height:300px;overflow:auto}
.tw-sources{margin:8px 0 0;padding-left:18px;font-size:12px;color:var(--dsw-alias-label-secondary,GrayText)}
.tw-sources a{color:var(--dsw-alias-label-primary-bluish,LinkText)}
.tw-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.tw-empty{margin:0;color:var(--dsw-alias-label-tertiary,GrayText);font-size:12px}
.tw-toolcard{border:1px solid var(--dsw-alias-border-l1,GrayText);border-left:3px solid var(--dsw-alias-button-info-fill,Highlight);border-radius:8px;background:var(--dsw-alias-bg-layer-1,Canvas)}
.tw-toolcard>summary{cursor:pointer;padding:8px 10px;font-size:12px;font-weight:600}
.tw-toolcard.err{border-left-color:var(--dsw-alias-state-error-primary,#d1242f)}
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
      if (!["snapshot", "switch-mode", "series", "equity", "positions", "correlation", "sensitivity", "risk", "trades", "events", "factors", "ic", "audit"].includes(endpoint))
        throw new Error("Unsupported workbench operation");
      const result = await rpc.call("/api", `trading-workbench/${endpoint}`, payload, signal);
      if (!result.ok) throw new Error(result.error.message);
      return result.value;
    }

    // ── 主题色读取（图表绘制用）────────────────────────────────────────────
    function themeColors() {
      const fallback = { up: "#2ea043", down: "#d1242f", line: "#4a9eff", text: "#888", grid: "rgba(128,128,128,.25)", bg: "transparent" };
      if (typeof document === "undefined") return fallback;
      const s = getComputedStyle(document.documentElement);
      const read = (name, fb) => (s.getPropertyValue(name) || "").trim() || fb;
      return {
        up: read("--dsw-alias-state-success-primary", fallback.up),
        down: read("--dsw-alias-state-error-primary", fallback.down),
        line: read("--dsw-alias-button-info-fill", fallback.line),
        text: read("--dsw-alias-label-secondary", fallback.text),
        grid: read("--dsw-alias-border-l1", fallback.grid),
        bg: read("--dsw-alias-bg-base", fallback.bg),
      };
    }

    // ── canvas 绘制原语 ──────────────────────────────────────────────────
    function useCanvasChart(draw, deps) {
      const ref = React.useRef(null);
      React.useEffect(() => {
        const canvas = ref.current;
        if (!canvas || typeof canvas.getContext !== "function") return;
        const dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
        const rect = canvas.getBoundingClientRect();
        const width = Math.max(rect.width, 200);
        const height = Math.max(rect.height, 80);
        canvas.width = Math.round(width * dpr);
        canvas.height = Math.round(height * dpr);
        const ctx = canvas.getContext("2d");
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, width, height);
        try {
          draw(ctx, width, height, themeColors());
        } catch (error) {
          ctx.fillStyle = themeColors().down;
          ctx.font = "12px sans-serif";
          ctx.fillText(`绘图失败：${String(error.message).slice(0, 80)}`, 10, 20);
        }
      }, deps);
      return ref;
    }

    function movingAverage(bars, n) {
      const out = new Array(bars.length).fill(null);
      let sum = 0;
      for (let i = 0; i < bars.length; i += 1) {
        sum += bars[i].c;
        if (i >= n) sum -= bars[i - n].c;
        if (i >= n - 1) out[i] = sum / n;
      }
      return out;
    }

    function KLineChart({ bars }) {
      const ref = useCanvasChart((ctx, width, height, color) => {
        if (!bars || bars.length < 2) {
          ctx.fillStyle = color.text;
          ctx.font = "12px sans-serif";
          ctx.fillText("暂无K线数据", 12, 22);
          return;
        }
        const padL = 46, padR = 12, padT = 10, volH = Math.round(height * 0.22), gap = 8;
        const priceH = height - padT - volH - gap - 18;
        const ma5 = movingAverage(bars, 5), ma20 = movingAverage(bars, 20);
        const values = bars.flatMap((b) => [b.h, b.l]).concat(ma5.filter((v) => v !== null), ma20.filter((v) => v !== null));
        let min = Math.min(...values), max = Math.max(...values);
        if (max - min < 1e-9) { max += 1; min -= 1; }
        const span = max - min;
        min -= span * 0.04; max += span * 0.04;
        const maxVol = Math.max(...bars.map((b) => b.v || 0), 1);
        const innerW = width - padL - padR;
        const step = innerW / bars.length;
        const cw = Math.max(1, Math.min(step * 0.65, 12));
        const y = (price) => padT + priceH * (1 - (price - min) / (max - min));

        // 网格 + 价格刻度
        ctx.strokeStyle = color.grid; ctx.lineWidth = 1;
        ctx.fillStyle = color.text; ctx.font = "10px sans-serif";
        for (let i = 0; i <= 4; i += 1) {
          const price = min + (max - min) * (i / 4);
          const gy = Math.round(y(price)) + 0.5;
          ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(width - padR, gy); ctx.stroke();
          ctx.fillText(price.toFixed(2), 4, gy + 3);
        }
        // 蜡烛 + 成交量
        bars.forEach((b, i) => {
          const cx = padL + step * i + step / 2;
          const up = b.c >= b.o;
          ctx.strokeStyle = up ? color.up : color.down;
          ctx.fillStyle = up ? color.up : color.down;
          ctx.beginPath(); ctx.moveTo(cx, y(b.h)); ctx.lineTo(cx, y(b.l)); ctx.stroke();
          const top = y(Math.max(b.o, b.c)), bottom = y(Math.min(b.o, b.c));
          ctx.fillRect(cx - cw / 2, top, cw, Math.max(1, bottom - top));
          const vh = (b.v || 0) / maxVol * volH;
          ctx.globalAlpha = 0.55;
          ctx.fillRect(cx - cw / 2, height - 16 - vh, cw, Math.max(0.5, vh));
          ctx.globalAlpha = 1;
        });
        // 均线
        const line = (series, color2) => {
          ctx.strokeStyle = color2; ctx.lineWidth = 1.4; ctx.beginPath();
          let started = false;
          series.forEach((value, i) => {
            if (value === null) return;
            const cx = padL + step * i + step / 2;
            if (!started) { ctx.moveTo(cx, y(value)); started = true; } else ctx.lineTo(cx, y(value));
          });
          ctx.stroke();
        };
        line(ma5, color.line);
        line(ma20, "#c9a227");
        // 时间刻度
        ctx.fillStyle = color.text;
        const ticks = 4;
        for (let i = 0; i < ticks; i += 1) {
          const index = Math.floor((bars.length - 1) * (i / (ticks - 1)));
          const label = String(bars[index].t).slice(5, 16);
          const lx = padL + step * index + step / 2;
          ctx.fillText(label, Math.min(lx, width - padR - 62), height - 3);
        }
        // 图例
        ctx.fillStyle = color.line; ctx.fillRect(padL, padT - 6, 10, 2);
        ctx.fillStyle = color.text; ctx.fillText("MA5", padL + 14, padT - 2);
        ctx.fillStyle = "#c9a227"; ctx.fillRect(padL + 48, padT - 6, 10, 2);
        ctx.fillStyle = color.text; ctx.fillText("MA20", padL + 62, padT - 2);
      }, [bars]);
      return h("canvas", { ref, className: "tw-chart" });
    }

    function LineChart({ points, label = "" }) {
      const ref = useCanvasChart((ctx, width, height, color) => {
        if (!points || points.length < 2) {
          ctx.fillStyle = color.text; ctx.font = "12px sans-serif";
          ctx.fillText("暂无序列数据", 12, 22); return;
        }
        const padL = 46, padR = 12, padT = 10, padB = 18;
        const values = points.map((p) => p.v);
        let min = Math.min(...values), max = Math.max(...values);
        if (max - min < 1e-9) { max += 1; min -= 1; }
        const span = max - min; min -= span * 0.06; max += span * 0.06;
        const y = (v) => padT + (height - padT - padB) * (1 - (v - min) / (max - min));
        const x = (i) => padL + (width - padL - padR) * (i / (points.length - 1));
        ctx.strokeStyle = color.grid; ctx.fillStyle = color.text; ctx.font = "10px sans-serif";
        for (let i = 0; i <= 4; i += 1) {
          const v = min + (max - min) * (i / 4);
          const gy = Math.round(y(v)) + 0.5;
          ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(width - padR, gy); ctx.stroke();
          ctx.fillText(v.toFixed(2), 4, gy + 3);
        }
        const rising = values[values.length - 1] >= values[0];
        ctx.strokeStyle = rising ? color.up : color.down; ctx.lineWidth = 1.8; ctx.beginPath();
        points.forEach((p, i) => { if (i === 0) ctx.moveTo(x(i), y(p.v)); else ctx.lineTo(x(i), y(p.v)); });
        ctx.stroke();
        ctx.globalAlpha = 0.12; ctx.fillStyle = ctx.strokeStyle;
        ctx.lineTo(x(points.length - 1), height - padB); ctx.lineTo(x(0), height - padB); ctx.closePath(); ctx.fill();
        ctx.globalAlpha = 1;
        if (label) { ctx.fillStyle = color.text; ctx.fillText(label, padL, height - 4); }
      }, [points, label]);
      return h("canvas", { ref, className: "tw-chart small" });
    }

    // ── 展示组件 ────────────────────────────────────────────────────────
    /** 按需拉取只读端点：切换分类或刷新时取数，失败降级为可读错误。 */
    function useEndpoint(rpc, endpoint, payload, deps) {
      const [state, setState] = React.useState({ data: null, error: "", loading: true });
      React.useEffect(() => {
        let alive = true;
        const controller = new AbortController();
        setState((s) => ({ ...s, loading: true }));
        request(rpc, endpoint, payload, controller.signal)
          .then((value) => { if (alive) setState({ data: value, error: "", loading: false }); })
          .catch((failure) => { if (alive) setState({ data: null, error: failure.message, loading: false }); });
        return () => { alive = false; controller.abort(); };
      }, deps);
      return state;
    }

    function HeatmapChart({ rowLabels, colLabels, tickers, matrix, unit = "" }) {
      const rows = rowLabels ?? tickers ?? [];
      const cols = colLabels ?? tickers ?? [];
      const ref = useCanvasChart((ctx, width, height, color) => {
        if (!rows.length || !matrix || !matrix.length) {
          ctx.fillStyle = color.text; ctx.font = "12px sans-serif";
          ctx.fillText("暂无矩阵数据", 12, 22); return;
        }
        const padL = 54, padT = 24, padR = 10, padB = 10;
        const cell = Math.min((width - padL - padR) / cols.length, (height - padT - padB) / rows.length, 64);
        ctx.font = "10px sans-serif";
        let min = Infinity, max = -Infinity;
        matrix.forEach((line) => line.forEach((v) => { if (v !== null && v !== undefined) { min = Math.min(min, v); max = Math.max(max, v); } }));
        if (!Number.isFinite(min)) { min = 0; max = 1; }
        const span = max - min || 1;
        rows.forEach((label, i) => { ctx.fillStyle = color.text; ctx.fillText(String(label), 4, padT + cell * i + cell / 2 + 3); });
        cols.forEach((label, j) => { ctx.fillStyle = color.text; ctx.fillText(String(label), padL + cell * j + 2, padT - 8); });
        matrix.forEach((line, i) => line.forEach((value, j) => {
          const x = padL + cell * j, y = padT + cell * i;
          if (value === null || value === undefined) {
            ctx.fillStyle = color.grid; ctx.fillRect(x, y, cell - 2, cell - 2);
            ctx.fillStyle = color.text; ctx.fillText("--", x + 4, y + cell / 2 + 3);
            return;
          }
          const strength = (value - min) / span;
          ctx.fillStyle = value >= 0 ? color.up : color.down;
          ctx.globalAlpha = 0.12 + strength * 0.78;
          ctx.fillRect(x, y, cell - 2, cell - 2);
          ctx.globalAlpha = 1;
          ctx.fillStyle = strength > 0.6 ? "#fff" : color.text;
          ctx.fillText(value.toFixed(2) + unit, x + 3, y + cell / 2 + 3);
        }));
      }, [rows.join(","), cols.join(","), JSON.stringify(matrix)]);
      return h("canvas", { ref, className: "tw-chart" });
    }

    function Card({ title, count, empty, children }) {
      return h("section", { className: "tw-card" },
        h("div", { className: "tw-card-head" }, title,
          count !== undefined && h("span", { className: "tw-count" }, String(count))),
        h("div", { className: "tw-card-body" }, empty ? h("p", { className: "tw-empty" }, empty) : children));
    }

    function Source({ source }) {
      const link = /^https?:\/\//i.test(source.reference);
      return h("li", null, `${source.name} · ${source.as_of} · `,
        link ? h("a", { href: source.reference, target: "_blank", rel: "noreferrer" }, source.reference) : source.reference);
    }

    const RATING_CLASS = { Buy: "buy", Overweight: "buy", Sell: "sell", Underweight: "sell", Hold: "hold" };

    function ToolCard({ block }) {
      const settled = Array.isArray(block?.content);
      const content = settled ? block.content.filter((i) => i.type === "text").map((i) => i.text).join("\n")
        : "Harness 正在处理…";
      return h("details", { className: `tw-toolcard${block?.isError ? " err" : ""}` },
        h("summary", null, block?.isError ? "交易工作台 · 操作失败" : "交易工作台 · 结果"),
        h("div", { className: "tw-item-body" }, h("pre", { className: "tw-pre" }, content)));
    }

    const PERIODS = ["1m", "5m", "15m", "30m", "60m", "1d"];
    const NAV = [
      { id: "market", label: "行情" }, { id: "signal", label: "信号" },
      { id: "portfolio", label: "组合" }, { id: "risk", label: "风险" },
      { id: "factors", label: "因子" },
      { id: "execution", label: "执行" }, { id: "research", label: "研究" },
      { id: "events", label: "事件" }, { id: "audit", label: "审计" },
    ];

    function MarketView({ rpc, state, setState }) {
      const { ticker, period, bars, loading, error, meta } = state;
      React.useEffect(() => {
        let alive = true;
        const controller = new AbortController();
        setState((s) => ({ ...s, loading: true, error: "" }));
        request(rpc, "series", { ticker, period, limit: 300 }, controller.signal)
          .then((value) => { if (alive) setState((s) => ({ ...s, bars: value.bars, meta: { source: value.source, as_of: value.as_of }, loading: false })); })
          .catch((failure) => { if (alive) setState((s) => ({ ...s, error: failure.message, loading: false })); });
        return () => { alive = false; controller.abort(); };
      }, [ticker, period]);

      const last = bars && bars.length ? bars[bars.length - 1] : null;
      const prev = bars && bars.length > 1 ? bars[bars.length - 2] : null;
      const change = last && prev ? ((last.c - prev.c) / prev.c) * 100 : 0;
      return h(React.Fragment, null,
        h("div", { className: "tw-toolbar" },
          h("input", { className: "tw-input", value: ticker, "aria-label": "标的代码",
            onChange: (e) => setState((s) => ({ ...s, ticker: e.target.value.toUpperCase() })) }),
          PERIODS.map((p) => h("button", { key: p, type: "button",
            className: `tw-btn seg${p === period ? " active" : ""}`,
            onClick: () => setState((s) => ({ ...s, period: p })) }, p)),
          h("span", { className: "tw-meta" }, loading ? "加载中…" : meta.as_of ? `数据截至 ${meta.as_of} · ${meta.source}` : "")),
        error && h("p", { className: "tw-alert" }, `行情获取失败：${error}`),
        h("div", { className: "tw-kv" },
          h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "最新价"),
            h("div", { className: "tw-kv-v" }, last ? last.c.toFixed(2) : "—")),
          h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "涨跌（周期）"),
            h("div", { className: "tw-kv-v", style: { color: change >= 0 ? "var(--dsw-alias-state-success-primary,#2ea043)" : "var(--dsw-alias-state-error-primary,#d1242f)" } },
              `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`)),
          h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "最高/最低（区间）"),
            h("div", { className: "tw-kv-v" }, bars && bars.length ? `${Math.max(...bars.map((b) => b.h)).toFixed(2)} / ${Math.min(...bars.map((b) => b.l)).toFixed(2)}` : "—")),
          h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "成交量（末根）"),
            h("div", { className: "tw-kv-v" }, last ? Math.round(last.v).toLocaleString() : "—"))),
        h(Card, { title: "K线 · MA5/MA20 · 成交量" }, h(KLineChart, { bars })),
        h("p", { className: "tw-hint" }, "分钟级数据来自新浪源（A股）；港股/美股当前的分钟K线数据源受富途接口限制，暂以快照单点降级显示。"));
    }

    function SignalView({ snapshot, series }) {
      const previews = snapshot.previews.filter((p) => p.kind === "signal" || p.kind === "backtest");
      return h(React.Fragment, null,
        h(Card, { title: "量化信号与回测（由 Harness 计算）", count: previews.length,
          empty: "暂无信号。请在 Harness 会话中请求（如“看下 600519 的信号”），结果显示在这里。" },
          previews.slice(0, 10).map((p) => h("details", { key: p.id, className: "tw-item" },
            h("summary", null,
              h("span", { className: `tw-tag ${p.value?.signal === "BUY" ? "buy" : p.value?.signal === "SELL" ? "sell" : "hold"}` },
                p.value?.signal ?? p.kind),
              `${p.value?.ticker ?? ""}`,
              h("span", { className: "tw-meta" }, p.at)),
            h("div", { className: "tw-item-body" }, h("pre", { className: "tw-pre" }, JSON.stringify(p.value, null, 2)))))),
        series && series.length > 1 && h(Card, { title: "价格走势（当前标的）" },
          h(LineChart, { points: series.map((b) => ({ v: b.c })), label: "收盘价" })),
        h("p", { className: "tw-hint" }, "工作台只展示结果；信号计算、回测与下单请在 Harness 会话中发起。"));
    }

    function PortfolioView({ rpc, snapshot, series }) {
      const ledgerPreview = snapshot.previews.find((p) => p.kind === "ledger");
      const positions = useEndpoint(rpc, "positions", { mode: snapshot.mode }, [rpc, snapshot.mode]);
      const equity = useEndpoint(rpc, "equity", { mode: snapshot.mode, window: 250 }, [rpc, snapshot.mode]);
      const rows = positions.data?.positions ?? [];
      const points = equity.data?.points ?? [];
      return h(React.Fragment, null,
        h(Card, { title: "账户与权益" },
          h("div", { className: "tw-kv" },
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "账户模式"),
              h("div", { className: "tw-kv-v" }, snapshot.mode === "live" ? "实盘 LIVE" : "模拟盘 SIM")),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "现金"),
              h("div", { className: "tw-kv-v" }, positions.data?.cash?.toLocaleString?.() ?? "—")),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "持仓市值"),
              h("div", { className: "tw-kv-v" }, positions.data?.market_value?.toLocaleString?.() ?? "—")),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "总权益"),
              h("div", { className: "tw-kv-v" }, positions.data?.equity?.toLocaleString?.() ?? "—")),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "累计收益"),
              h("div", { className: "tw-kv-v" }, equity.data?.total_return !== undefined ? `${(equity.data.total_return * 100).toFixed(2)}%` : "—")),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "成交笔数"),
              h("div", { className: "tw-kv-v" }, String(equity.data?.trades ?? positions.data?.trades ?? "—")))),
          positions.error && h("p", { className: "tw-alert" }, `持仓读取失败：${positions.error}`),
          h("p", { className: "tw-meta" }, "来源：本地模拟台账（非券商资产）；实盘持仓请用富途账户查询工具。")),
        h(Card, { title: "权益曲线（按成交回放 + 日线盯市）",
          empty: points.length > 1 ? undefined : (equity.loading ? "加载中…" : "暂无成交记录，无法回放权益曲线") },
          h(LineChart, { points: points.map((p) => ({ v: p.equity })), label: equity.data?.note || "" }),
          equity.data && h("p", { className: "tw-meta" },
            `区间 ${points[0]?.t} → ${points[points.length - 1]?.t} · 最大回撤 ${(equity.data.max_drawdown * 100).toFixed(2)}% · 夏普 ${equity.data.sharpe}`)),
        h(Card, { title: "持仓明细", count: rows.length,
          empty: rows.length ? undefined : "当前无持仓" },
          rows.length > 0 && h("div", { className: "tw-kv" }, rows.map((row) =>
            h("div", { key: row.ticker, className: "tw-kv-item" },
              h("div", { className: "tw-kv-k" }, `${row.ticker} · ${row.shares} 股 · 止损 ${row.stop ?? "—"}`),
              h("div", { className: "tw-kv-v", style: { color: (row.pnl ?? 0) >= 0 ? "var(--dsw-alias-state-success-primary,#2ea043)" : "var(--dsw-alias-state-error-primary,#d1242f)" } },
                `${row.price ?? "—"}（${row.pnl >= 0 ? "+" : ""}${row.pnl} / ${row.pnl_pct ?? "—"}%）`)))),
          ledgerPreview && h("p", { className: "tw-meta" }, `最近台账预览：${ledgerPreview.at}`)),
        series && series.length > 1 && h(Card, { title: "当前标的走势" },
          h(LineChart, { points: series.map((b) => ({ v: b.c })), label: "收盘价" })));
    }

    function RiskConfigCard({ rpc, mode }) {
      const risk = useEndpoint(rpc, "risk", {}, [rpc]);
      const config = risk.data?.config;
      return h(Card, { title: "风控参数（引擎实际生效值）",
        empty: config ? undefined : (risk.loading ? "加载中…" : (risk.error || "不可用")) },
        config && h("div", { className: "tw-kv" }, Object.entries(config).map(([key, value]) =>
          h("div", { key, className: "tw-kv-item" },
            h("div", { className: "tw-kv-k" }, key),
            h("div", { className: "tw-kv-v" }, String(value))))),
        h("p", { className: "tw-meta" }, `来源：${risk.data?.source ?? "—"}`),
        h("p", { className: "tw-hint" }, "参数由 scripts/risk_config 管理；引擎每次决策前读取，非法配置直接拒绝交易。"));
    }

    function RiskView({ rpc, snapshot }) {
      const latest = snapshot.previews.find((p) => p.kind === "backtest");
      const s = latest?.value?.summary;
      const equity = useEndpoint(rpc, "equity", { mode: snapshot.mode, window: 250 }, [rpc, snapshot.mode]);
      const positions = useEndpoint(rpc, "positions", { mode: snapshot.mode }, [rpc, snapshot.mode]);
      const held = (positions.data?.positions ?? []).map((p) => p.ticker);
      const basket = (held.length >= 2 ? held : ["600519", "000001", "601318", "600036"]).slice(0, 6);
      const correlation = useEndpoint(rpc, "correlation", { tickers: basket, window: 120 },
        [rpc, basket.join(",")]);
      const ddPoints = (equity.data?.points ?? []).map((p) => ({ v: (p.dd ?? 0) * 100 }));
      return h(React.Fragment, null,
        h(Card, { title: "风险指标",
          empty: (s || equity.data) ? undefined : "暂无风险数据" },
          h("div", { className: "tw-kv" },
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "最大回撤（台账回放）"),
              h("div", { className: "tw-kv-v" }, equity.data ? `${(equity.data.max_drawdown * 100).toFixed(2)}%` : "—")),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "夏普（台账回放）"),
              h("div", { className: "tw-kv-v" }, equity.data ? String(equity.data.sharpe) : "—")),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "回测最大回撤"),
              h("div", { className: "tw-kv-v" }, s ? `${(s.max_drawdown * 100).toFixed(2)}%` : "—")),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "回测夏普 / 年化"),
              h("div", { className: "tw-kv-v" }, s ? `${s.sharpe} / ${(s.annualized * 100).toFixed(1)}%` : "—")))),
        h(Card, { title: "回撤曲线（水下图，%）",
          empty: ddPoints.length > 1 ? undefined : "需要成交记录才能回放回撤" },
          h(LineChart, { points: ddPoints, label: "drawdown %" })),
        h(Card, { title: "相关性矩阵（日收益，120 日）",
          empty: (correlation.data?.tickers?.length ?? 0) >= 2 ? undefined
            : (correlation.loading ? "加载中…" : (correlation.error || "标的不足")) },
          correlation.data && h(HeatmapChart, { tickers: correlation.data.tickers, matrix: correlation.data.matrix }),
          correlation.data && h("p", { className: "tw-meta" }, `窗口 ${correlation.data.window} 个共同交易日 · 截至 ${correlation.data.as_of}`)),
        h(RiskConfigCard, { rpc, mode: snapshot.mode }),
        h("p", { className: "tw-hint" }, "持仓敞口与相关性均基于本地台账与公开日线；实盘口径请结合富途账户查询。"));
    }

    const FACTOR_LABELS = {
      mom_20: "动量20", mom_60: "动量60", vol_20: "波动率", trend: "趋势偏离",
      rsi_14: "RSI14", liq_ratio: "量能比", mdd_60: "最大回撤",
    };

    function FactorsView({ rpc, ticker, watchlist, setWatchlist }) {
      const [factor, setFactor] = React.useState("mom_20");
      const tickers = watchlist;
      const snap = useEndpoint(rpc, "factors", { tickers, window: 250 }, [rpc, tickers.join(",")]);
      const ic = useEndpoint(rpc, "ic", { tickers, factor, forward: 5, window: 250 }, [rpc, tickers.join(","), factor]);
      const rows = snap.data?.rows ?? [];
      const icPoints = (ic.data?.points ?? []).map((p) => ({ v: p.ic }));
      return h(React.Fragment, null,
        h(Card, { title: "标的池", count: tickers.length },
          h("div", { className: "tw-toolbar" },
            h("input", { className: "tw-input", value: tickers.join(","), "aria-label": "标的池（逗号分隔，2..8个）",
              onChange: (e) => setWatchlist(e.target.value.toUpperCase().split(",").map((t) => t.trim()).filter(Boolean).slice(0, 8)) }),
            h("button", { type: "button", className: "tw-btn", onClick: () => setWatchlist([ticker, ...tickers.filter((t) => t !== ticker)].slice(0, 8)) },
              `加入当前标的 ${ticker}`)),
          h("p", { className: "tw-hint" }, "价量因子横截面打分；估值/质量因子待基本面源接入。")),
        h(Card, { title: "因子打分与排序", count: rows.length,
          empty: snap.loading ? "加载中…" : (snap.error || "有效标的不足") },
          rows.length > 0 && h("div", { className: "tw-kv" }, rows.map((row) =>
            h("div", { key: row.ticker, className: "tw-kv-item" },
              h("div", { className: "tw-kv-k" }, `#${row.rank} ${row.ticker}`),
              h("div", { className: "tw-kv-v", style: { color: (row.score ?? 0) >= 0 ? "var(--dsw-alias-state-success-primary,#2ea043)" : "var(--dsw-alias-state-error-primary,#d1242f)" } },
                `${row.score >= 0 ? "+" : ""}${row.score}`),
              h("div", { className: "tw-meta" }, Object.entries(FACTOR_LABELS).map(([k, label]) =>
                `${label} ${row.factors[k] !== undefined && row.factors[k] !== null ? Number(row.factors[k]).toFixed(3) : "—"}`).join(" · "))))),
          snap.data?.failures && Object.keys(snap.data.failures).length > 0
            && h("p", { className: "tw-meta" }, `跳过：${Object.entries(snap.data.failures).map(([k, v]) => `${k}(${v})`).join("；")}`)),
        h(Card, { title: "因子 IC / ICIR（横截面，forward 5 日）",
          empty: ic.loading ? "加载中…" : (ic.error || "样本不足") },
          h("div", { className: "tw-toolbar" }, Object.entries(FACTOR_LABELS).map(([k, label]) =>
            h("button", { key: k, type: "button", className: `tw-btn seg${factor === k ? " active" : ""}`,
              onClick: () => setFactor(k) }, label))),
          ic.data && h("div", { className: "tw-kv" },
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "均值 IC"), h("div", { className: "tw-kv-v" }, String(ic.data.mean_ic))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "IC 标准差"), h("div", { className: "tw-kv-v" }, String(ic.data.ic_std))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "ICIR"), h("div", { className: "tw-kv-v" }, String(ic.data.icir))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "正 IC 占比"), h("div", { className: "tw-kv-v" }, String(ic.data.positive_ratio))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "样本期数"), h("div", { className: "tw-kv-v" }, String(ic.data.count)))),
          icPoints.length > 1 && h(LineChart, { points: icPoints, label: "IC 序列" }),
          ic.data && h("p", { className: "tw-hint" }, ic.data.note)));
    }

    function ExecutionView({ rpc, snapshot }) {
      const trades = useEndpoint(rpc, "trades", { mode: snapshot.mode, limit: 50 }, [rpc, snapshot.mode]);
      const rows = trades.data?.trades ?? [];
      return h(React.Fragment, null,
        h(Card, { title: "成交记录（本地台账）", count: rows.length,
          empty: trades.loading ? "加载中…" : (trades.error || "暂无成交记录") },
          rows.length > 0 && h("div", { className: "tw-kv" }, rows.slice(0, 20).map((row, i) =>
            h("div", { key: i, className: "tw-kv-item" },
              h("div", { className: "tw-kv-k" }, `${row.date} · ${row.ticker}`),
              h("div", { className: "tw-kv-v", style: { color: row.action === "BUY" ? "var(--dsw-alias-state-success-primary,#2ea043)" : "var(--dsw-alias-state-error-primary,#d1242f)" } },
                `${row.action} ${row.shares} @ ${row.price}`),
              h("div", { className: "tw-meta" },
                `费用 ${row.fee ?? "—"}${row.return !== undefined && row.return !== null ? ` · 收益 ${(row.return * 100).toFixed(2)}%` : ""}${row.reason ? ` · ${row.reason}` : ""}`)))),
          trades.data && h("p", { className: "tw-meta" },
            `共 ${trades.data.total} 笔 · 胜率 ${trades.data.win_rate === null ? "—" : `${(trades.data.win_rate * 100).toFixed(0)}%`} · 累计费用 ${trades.data.total_fees}`)),
        h(Card, { title: "交易动态（Harness 观察到的券商响应）", count: snapshot.activity.length },
          h("p", { className: "tw-meta" }, "响应不等于成交；下单请在 Harness 会话中完成并确认。"),
          snapshot.activity.slice(0, 30).map((row) => h("details", { key: row.id, className: "tw-item" },
            h("summary", null, `${row.at} · ${row.tool ?? row.kind}`,
              row.is_error ? h("span", { className: "tw-tag sell" }, "失败") : null),
            h("div", { className: "tw-item-body" }, h("pre", { className: "tw-pre" }, JSON.stringify(row.value ?? row, null, 2)))))));
    }

    function ResearchView({ rpc, snapshot, ticker }) {
      const running = snapshot.runs.filter((run) => run.status === "running");
      const [strategy, setStrategy] = React.useState("ma_cross");
      const [metric, setMetric] = React.useState("total_return");
      const [result, setResult] = React.useState(null);
      const [busy, setBusy] = React.useState(false);
      const [failure, setFailure] = React.useState("");

      const runSensitivity = async () => {
        setBusy(true); setFailure("");
        try {
          const value = await request(rpc, "sensitivity", { ticker, strategy, metric, start: "2023-01-01" });
          setResult(value);
        } catch (error) { setFailure(error.message); }
        finally { setBusy(false); }
      };

      return h(React.Fragment, null,
        h(Card, { title: "研报结果", count: snapshot.reports.length,
          empty: snapshot.reports.length || running.length ? undefined : "暂无研报。在 Harness 中要求完整投研后，发布结果会显示在这里。" },
          running.map((run) => h("div", { key: run.id, className: "tw-item" },
            h("div", { className: "tw-item-body" }, h("span", { className: "tw-tag" }, "进行中"), ` ${run.ticker} · ${run.started_at}`))),
          snapshot.reports.map((report) => h("details", { key: report.id, className: "tw-item" },
            h("summary", null, h("span", { className: `tw-tag ${RATING_CLASS[report.rating] ?? ""}` }, report.rating),
              report.ticker, h("span", { className: "tw-meta" }, report.published_at)),
            h("div", { className: "tw-item-body" }, h("pre", { className: "tw-pre" }, report.report),
              h("ul", { className: "tw-sources" }, report.sources.map((s, i) => h(Source, { key: i, source: s }))))))),
        h(Card, { title: "参数敏感性（样本内网格）" },
          h("div", { className: "tw-toolbar" },
            h("span", { className: "tw-meta" }, `标的 ${ticker}`),
            h("button", { type: "button", className: `tw-btn seg${strategy === "ma_cross" ? " active" : ""}`,
              onClick: () => setStrategy("ma_cross") }, "双均线"),
            h("button", { type: "button", className: `tw-btn seg${strategy === "rsi" ? " active" : ""}`,
              onClick: () => setStrategy("rsi") }, "RSI"),
            ["total_return", "sharpe", "max_drawdown", "win_rate"].map((m) =>
              h("button", { key: m, type: "button", className: `tw-btn seg${metric === m ? " active" : ""}`,
                onClick: () => setMetric(m) }, m)),
            h("button", { type: "button", className: "tw-btn primary", disabled: busy, onClick: runSensitivity },
              busy ? "计算中…" : "计算网格")),
          failure && h("p", { className: "tw-alert" }, `计算失败：${failure}`),
          !result && !busy && h("p", { className: "tw-empty" }, "点击「计算网格」运行参数扫描（只读回测，不下单）。"),
          result && h(React.Fragment, null,
            h(HeatmapChart, { rowLabels: result.rows, colLabels: result.cols, matrix: result.matrix }),
            h("p", { className: "tw-meta" },
              `行=${result.row_label} 列=${result.col_label} · 指标=${result.metric} · 样本 ${result.bars} 根（截至 ${result.as_of}）`),
            h("p", { className: "tw-meta" }, `最优：${result.row_label}=${result.best.row}, ${result.col_label}=${result.best.col} → ${result.metric} ${result.best.value}（${result.best.trades} 笔 / 胜率 ${result.best.win_rate}）`),
            h("p", { className: "tw-hint" }, result.note))));
    }

    function EventsView({ rpc, ticker }) {
      const events = useEndpoint(rpc, "events", { ticker, days: 400 }, [rpc, ticker]);
      const rows = events.data?.events ?? [];
      const upcoming = rows.filter((e) => (e.days_until ?? 0) >= 0);
      const past = rows.filter((e) => (e.days_until ?? 0) < 0).reverse();
      const line = (event, index) => h("div", { key: `${event.date}-${index}`, className: "tw-item" },
        h("div", { className: "tw-item-body", style: { paddingTop: "8px" } },
          h("span", { className: `tw-tag ${(event.days_until ?? 0) >= 0 && (event.days_until ?? 0) <= 14 ? "hold" : ""}` },
            event.type),
          ` ${event.date}`,
          h("span", { className: "tw-meta" },
            ` · ${(event.days_until ?? 0) >= 0 ? `${event.days_until} 天后` : `${-event.days_until} 天前`} · ${event.detail}`)));
      return h(React.Fragment, null,
        h(Card, { title: `即将发生的事件（${ticker}）`, count: upcoming.length,
          empty: events.loading ? "加载中…" : (events.error || "窗口内无即将发生的事件") },
          upcoming.map(line),
          events.data && h("p", { className: "tw-meta" },
            `来源状态：${Object.entries(events.data.sources_status || {}).map(([k, v]) => `${k}=${v}`).join(" · ")}`)),
        h(Card, { title: "近期已发生", count: past.length,
          empty: past.length ? undefined : "窗口内无历史事件" },
          past.slice(0, 10).map(line)),
        h("p", { className: "tw-hint" }, events.data?.note
          || "事件来自公开披露源；港股/美股事件请用富途工具查询（quote_financials_* / quote_corporate_actions_* / quote_economic_calendar_search）。"));
    }

    const CHAIN_KIND = {
      signal: { label: "信号", cls: "" },
      order: { label: "下单", cls: "buy" },
      "order-error": { label: "下单失败", cls: "sell" },
      fill: { label: "成交", cls: "hold" },
    };

    function AuditView({ rpc, snapshot }) {
      const audit = useEndpoint(rpc, "audit", {}, [rpc, snapshot.mode, snapshot.generated_at]);
      const entries = audit.data?.entries ?? [];
      const stats = audit.data?.stats;
      return h(React.Fragment, null,
        h(Card, { title: "链路统计",
          empty: stats ? undefined : (audit.loading ? "加载中…" : (audit.error || "暂无链路数据")) },
          stats && h("div", { className: "tw-kv" },
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "信号"), h("div", { className: "tw-kv-v" }, String(stats.signals))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "下单响应"), h("div", { className: "tw-kv-v" }, String(stats.orders))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "成交（台账）"), h("div", { className: "tw-kv-v" }, String(stats.fills))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "已关联信号"),
              h("div", { className: "tw-kv-v", style: { color: "var(--dsw-alias-state-success-primary,#2ea043)" } }, String(stats.linked))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "未关联"),
              h("div", { className: "tw-kv-v", style: { color: stats.unlinked > 0 ? "var(--dsw-alias-state-warn-label,#9a6700)" : "inherit" } }, String(stats.unlinked)))),
          stats && h("p", { className: "tw-meta" }, `关联规则：${stats.link_rule}`),
          h("p", { className: "tw-meta" }, `账户模式 ${snapshot.mode} · 进行中调用 ${snapshot.in_flight} · 暂存响应 ${snapshot.pending_observations}`)),
        h(Card, { title: "审计时间线（信号 → 下单 → 成交）", count: entries.length,
          empty: entries.length ? undefined : "暂无记录：先在 Harness 中产生信号，再下单/成交后这里会出现链路。" },
          entries.slice(0, 40).map((entry) => {
            const meta = CHAIN_KIND[entry.kind] ?? { label: entry.kind, cls: "" };
            return h("div", { key: entry.id, className: "tw-item" },
              h("div", { className: "tw-item-body", style: { paddingTop: "8px" } },
                h("span", { className: `tw-tag ${meta.cls}` }, meta.label),
                ` ${entry.at ?? "—"} · ${entry.ticker ?? "未知标的"}`,
                entry.origin
                  ? h("span", { className: "tw-meta" }, " · 链路起点")
                  : h("span", { className: "tw-meta" },
                      entry.linked
                        ? ` · 依据信号 ${entry.signal_id}（滞后 ${entry.lag_hours}h）`
                        : " · ⚠ 未找到对应信号"),
                h("div", { className: "tw-meta" }, `${entry.detail} · 来源 ${entry.source}`)));
          })),
        h("p", { className: "tw-hint" }, "审计仅记录 Harness 观察到的响应与本地台账；实盘成交请以券商成交查询为准。"));
    }

    function Dashboard({ rpc }) {
      const [open, setOpen] = React.useState(false);
      const [tab, setTab] = React.useState("market");
      const [snapshot, setSnapshot] = React.useState(null);
      const [error, setError] = React.useState("");
      const [switchError, setSwitchError] = React.useState("");
      const [confirmation, setConfirmation] = React.useState("");
      const [switching, setSwitching] = React.useState(false);
      const [revision, setRevision] = React.useState(0);
      const [market, setMarket] = React.useState({ ticker: "600519", period: "5m", bars: null, loading: false, error: "", meta: {} });
      const [watchlist, setWatchlist] = React.useState(["600519", "000001", "601318", "600036", "300750"]);
      const generation = React.useRef(0);

      React.useEffect(() => {
        if (!open || switching) return;
        const controller = new AbortController();
        const current = ++generation.current;
        let timer;
        const refresh = async () => {
          try {
            const next = await request(rpc, "snapshot", {}, controller.signal);
            if (!controller.signal.aborted && generation.current === current) { setSnapshot(next); setError(""); }
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
        generation.current += 1; setSwitching(true); setSnapshot(null); setError(""); setSwitchError("");
        try {
          await request(rpc, "switch-mode", { mode, expected_mode: expected, ...(mode === "live" ? { confirmation } : {}) });
          setConfirmation("");
        } catch (failure) { setSwitchError(failure.message); }
        finally { setSwitching(false); setRevision((v) => v + 1); }
      };

      const live = snapshot?.mode === "live";
      return h(React.Fragment, null,
        h("button", { type: "button", className: "tw-fab", "aria-expanded": open, title: "交易工作台",
          onClick: () => setOpen((v) => !v) },
          h("span", { className: `tw-dot${live ? " live" : ""}` }), open ? "收起工作台" : "交易工作台"),
        h("div", { className: `tw-scrim${open ? " open" : ""}`, onClick: () => setOpen(false) }),
        h("aside", { className: `tw-drawer${open ? " open" : ""}`, role: "dialog", "aria-label": "交易工作台", "aria-hidden": !open },
          h("header", { className: "tw-top" },
            h("h2", { className: "tw-title" }, "交易工作台"),
            snapshot && h("span", { className: `tw-badge${live ? " live" : ""}` }, live ? "实盘 LIVE" : "模拟盘 SIM"),
            snapshot && h("span", { className: "tw-meta" }, `更新 ${String(snapshot.generated_at).slice(11, 19)}`),
            h("button", { type: "button", className: "tw-close", onClick: () => setOpen(false), "aria-label": "关闭" }, "×")),
          h("div", { className: "tw-main" },
            h("nav", { className: "tw-nav" }, NAV.map((item) =>
              h("button", { key: item.id, type: "button", className: `tw-nav-item${tab === item.id ? " active" : ""}`,
                onClick: () => setTab(item.id) }, h("span", { className: "tw-nav-dot" }), item.label))),
            h("div", { className: "tw-content" },
              switchError && h("p", { role: "alert", className: "tw-alert" }, `模式切换失败：${switchError}`),
              error && h("p", { role: "alert", className: "tw-alert" }, `更新失败：${error}`),
              !snapshot && h("p", { className: "tw-status" }, switching ? "正在切换模式…" : "正在读取工作台…"),
              tab === "market" && h(MarketView, { rpc, state: market, setState: setMarket }),
              snapshot && tab === "signal" && h(SignalView, { snapshot, series: market.bars }),
              snapshot && tab === "portfolio" && h(PortfolioView, { rpc, snapshot, series: market.bars }),
              snapshot && tab === "risk" && h(RiskView, { rpc, snapshot }),
              tab === "factors" && h(FactorsView, { rpc, ticker: market.ticker, watchlist, setWatchlist }),
              snapshot && tab === "execution" && h(ExecutionView, { rpc, snapshot }),
              snapshot && tab === "research" && h(ResearchView, { rpc, snapshot, ticker: market.ticker }),
              tab === "events" && h(EventsView, { rpc, ticker: market.ticker }),
              snapshot && tab === "audit" && h(AuditView, { snapshot }),
              snapshot && h("div", { className: "tw-row" },
                snapshot.mode === "sim"
                  ? h(React.Fragment, null,
                    h("input", { className: "tw-input", value: confirmation, placeholder: "输入「确认实盘」以切换",
                      onChange: (e) => setConfirmation(e.target.value), autoComplete: "off", "aria-label": "确认实盘" }),
                    h("button", { type: "button", className: "tw-btn danger",
                      disabled: confirmation !== "确认实盘" || snapshot.in_flight > 0,
                      onClick: () => switchMode("live") }, "切换到实盘"))
                  : h("button", { type: "button", className: "tw-btn primary",
                      disabled: snapshot.in_flight > 0, onClick: () => switchMode("sim") }, "切回模拟盘"),
                h("button", { type: "button", className: "tw-btn", onClick: () => setRevision((v) => v + 1) }, "刷新")),
              snapshot && h("p", { className: "tw-hint" }, snapshot.notice)))));
    }

    function apply(ctx) {
      const disposeStyles = injectStyles(ctx);
      if (typeof ctx.effect === "function" && typeof disposeStyles === "function") ctx.effect(() => disposeStyles);
      const rpc = ctx.connection.rpc;
      ctx.slots.inject("shell.overlay", () => ctx.slots.register(
        { name: "shell.overlay", id: "trading-workbench" }, () => h(Dashboard, { rpc })));
      for (const key of ["research_publish", "run_trading_analysis", "quant_signal", "quant_backtest", "quant_report"]) {
        ctx.slots.inject("tool.call.toolview", () => ctx.slots.register(
          { name: "tool.call.toolview", key }, ToolCard));
      }
    }

    return { inject: ["slots", "connection"], apply, request };
  },
});
