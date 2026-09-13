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
.tw-link{background:none;border:none;padding:0;cursor:pointer;font:inherit;font-weight:600;color:var(--dsw-alias-label-primary-bluish,LinkText);text-align:left}
.tw-link:hover{text-decoration:underline}
.tw-pager{display:flex;align-items:center;gap:10px;justify-content:center;padding-top:6px;border-top:1px solid var(--dsw-alias-border-l1,GrayText);margin-top:4px}
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

    const KNOWN_ENDPOINTS = ["snapshot", "switch-mode", "series", "equity", "positions",
      "correlation", "sensitivity", "risk", "trades", "events", "factors", "ic", "audit",
      "sources", "instrument"];

    // 面板是查看用途，不需要实时。结果缓存在内存里，切页签/重开面板不再重复请求；
    // Host 侧另有 TTL 缓存，两层都命中时连 python 子进程都不会启动。
    const CLIENT_TTL_MS = {
      instrument: 5 * 60_000, series: 5 * 60_000, equity: 2 * 60_000, positions: 2 * 60_000,
      correlation: 10 * 60_000, sensitivity: 30 * 60_000, risk: 5 * 60_000, trades: 60_000,
      events: 30 * 60_000, factors: 10 * 60_000, ic: 10 * 60_000, audit: 60_000,
      sources: 2 * 60_000,
    };
    const CACHE_MAX_ENTRIES = 60;
    /** 兜底轮询间隔：面板是查看用途，切页签有缓存，不需要秒级刷新。 */
    const SNAPSHOT_POLL_MS = 60_000;
    const endpointCache = new Map();
    // Host 在 snapshot 里声明它实际提供哪些接口；null 表示尚未获知（旧版 Host 不声明）
    let servedEndpoints = null;
    // 撞过 404 的接口：即使 Host 没声明，也能据此停止重试并说明原因
    const missingEndpoints = new Set();
    // 用户在 5 秒内点击过刷新：期间发出的请求都绕过缓存
    let forceUntil = 0;

    function cacheKey(endpoint, payload) {
      return `${endpoint}|${JSON.stringify(Object.keys(payload ?? {}).sort()
        .map((key) => [key, payload[key]]))}`;
    }

    function readCache(endpoint, payload) {
      const hit = endpointCache.get(cacheKey(endpoint, payload));
      const ttl = CLIENT_TTL_MS[endpoint] ?? 0;
      if (!hit || ttl <= 0 || Date.now() - hit.at >= ttl) return null;
      return hit;
    }

    function writeCache(endpoint, payload, value) {
      if ((CLIENT_TTL_MS[endpoint] ?? 0) <= 0) return;
      endpointCache.set(cacheKey(endpoint, payload), { value, at: Date.now() });
      while (endpointCache.size > CACHE_MAX_ENTRIES) {
        endpointCache.delete(endpointCache.keys().next().value);
      }
    }

    /** 用户主动刷新：清掉客户端缓存，并让随后 5 秒内的请求强制穿透 Host 缓存。 */
    function invalidateCaches() {
      endpointCache.clear();
      forceUntil = Date.now() + 5000;
      // 用户可能刚重启过 Host：清掉"缺失"记忆，给它一次机会
      missingEndpoints.clear();
    }

    /** Host 未提供该接口时的可读原因（而不是一句 HTTP 404）。 */
    function missingEndpointMessage(endpoint) {
      return `Host 未提供 ${endpoint} 接口：当前 dsh web 进程早于插件更新，`
        + "重启 dsh web（或重开工作台）后可用。";
    }

    /** 传输层 404 说明该路由在 Host 里根本不存在（而不是业务失败）。 */
    function isRouteMissing(message) {
      return /HTTP 404|transport failure|not found/i.test(String(message));
    }

    async function request(rpc, endpoint, payload, signal, options = {}) {
      if (!KNOWN_ENDPOINTS.includes(endpoint)) throw new Error("Unsupported workbench operation");
      if (missingEndpoints.has(endpoint)) throw new Error(missingEndpointMessage(endpoint));
      if (servedEndpoints && !servedEndpoints.has(endpoint)) throw new Error(missingEndpointMessage(endpoint));
      const force = options.force === true || Date.now() < forceUntil;
      const body = force ? { ...payload, _refresh: true } : payload;
      let result;
      try {
        result = await rpc.call("/api", `trading-workbench/${endpoint}`, body, signal);
      } catch (failure) {
        // 旧版 Host 不声明清单，只能从 404 反推：记下来，避免每次切页签都重复撞墙
        if (isRouteMissing(failure?.message)) {
          missingEndpoints.add(endpoint);
          throw new Error(missingEndpointMessage(endpoint));
        }
        throw failure;
      }
      if (!result.ok) throw new Error(result.error.message);
      if (endpoint === "snapshot" && Array.isArray(result.value?.endpoints)) {
        servedEndpoints = new Set(result.value.endpoints);
      }
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
      const [state, setState] = React.useState(
        () => { const hit = readCache(endpoint, payload); return { data: hit?.value ?? null, error: "", loading: !hit }; });
      React.useEffect(() => {
        let alive = true;
        const controller = new AbortController();
        const hit = readCache(endpoint, payload);
        const force = Date.now() < forceUntil;
        if (hit && !force) {
          // 命中缓存：直接展示，不再发请求
          setState({ data: hit.value, error: "", loading: false });
          return () => { alive = false; };
        }
        setState({ data: hit?.value ?? null, error: "", loading: !hit });
        request(rpc, endpoint, payload, controller.signal, { force })
          .then((value) => {
            if (!alive) return;
            writeCache(endpoint, payload, value);
            setState({ data: value, error: "", loading: false });
          })
          .catch((failure) => {
            // 失败时若有旧数据就继续展示旧数据，只把错误说明挂上去
            if (alive) setState({ data: hit?.value ?? null, error: failure.message, loading: false });
          });
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

    /** 通用分页：所有列表统一分页展示（页码、上下页、总数）。 */
    function Paged({ items, pageSize = 8, empty, render }) {
      const list = items ?? [];
      const [page, setPage] = React.useState(0);
      React.useEffect(() => { setPage(0); }, [list.length]);
      if (list.length === 0) return h("p", { className: "tw-empty" }, empty ?? "暂无数据");
      const pages = Math.max(1, Math.ceil(list.length / pageSize));
      const current = Math.min(page, pages - 1);
      const slice = list.slice(current * pageSize, current * pageSize + pageSize);
      return h(React.Fragment, null,
        slice.map((item, index) => render(item, current * pageSize + index)),
        pages > 1 && h("div", { className: "tw-pager" },
          h("button", { type: "button", className: "tw-btn seg", disabled: current === 0,
            onClick: () => setPage(current - 1) }, "‹ 上一页"),
          h("span", { className: "tw-meta" }, `第 ${current + 1} / ${pages} 页 · 共 ${list.length} 条`),
          h("button", { type: "button", className: "tw-btn seg", disabled: current >= pages - 1,
            onClick: () => setPage(current + 1) }, "下一页 ›")));
    }

    /** 研报详情页：抽屉内全幅展示 + 可新标签打开。 */
    function ReportDetail({ report, onBack }) {
      const openInTab = () => {
        try {
          const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><title>${report.ticker} 研报</title>`
            + `<style>body{font-family:system-ui,-apple-system,sans-serif;max-width:900px;margin:32px auto;padding:0 20px;line-height:1.6}`
            + `pre{white-space:pre-wrap;background:#f6f8fa;padding:16px;border-radius:8px}`
            + `h1{font-size:20px}ul{color:#555}</style></head><body>`
            + `<h1>${report.ticker} · ${report.rating} · ${report.published_at}</h1>`
            + `<pre>${String(report.report).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]))}</pre>`
            + `<h3>来源</h3><ul>${(report.sources || []).map((s) => `<li>${s.name} · ${s.as_of} · ${s.reference}</li>`).join("")}</ul>`
            + `</body></html>`;
          const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
          if (typeof window !== "undefined") window.open(url, "_blank", "noopener");
        } catch (error) { /* 浏览器限制时忽略，抽屉内仍可查看 */ }
      };
      return h(React.Fragment, null,
        h("div", { className: "tw-toolbar" },
          h("button", { type: "button", className: "tw-btn", onClick: onBack }, "‹ 返回列表"),
          h("button", { type: "button", className: "tw-btn primary", onClick: openInTab }, "在新标签打开"),
          h("span", { className: `tw-tag ${RATING_CLASS[report.rating] ?? ""}` }, report.rating),
          h("span", { className: "tw-meta" }, `${report.ticker} · ${report.published_at}`)),
        h(Card, { title: "研报正文" }, h("pre", { className: "tw-pre" }, report.report)),
        h(Card, { title: "数据来源", count: (report.sources || []).length,
          empty: (report.sources || []).length ? undefined : "未记录来源" },
          h("ul", { className: "tw-sources" }, (report.sources || []).map((s, i) => h(Source, { key: i, source: s })))));
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

    const NAV = [
      { id: "market", label: "行情" }, { id: "signal", label: "信号" },
      { id: "portfolio", label: "组合" }, { id: "risk", label: "风险" },
      { id: "factors", label: "因子" },
      { id: "execution", label: "执行" }, { id: "research", label: "研究" },
      { id: "events", label: "事件" }, { id: "audit", label: "审计" },
    ];

    function MarketView({ rpc, onResolved }) {
      const [ticker, setTicker] = React.useState("");
      const [submitted, setSubmitted] = React.useState("");
      const [card, setCard] = React.useState(null);
      const [error, setError] = React.useState("");
      const [loading, setLoading] = React.useState(false);

      const lookup = async () => {
        const value = ticker.trim().toUpperCase();
        if (!value) { setError("请输入标的代码（如 00700.HK / AAPL / 600519）"); return; }
        setLoading(true); setError("");
        try {
          const data = await request(rpc, "instrument", { ticker: value });
          setCard(data); setSubmitted(value); onResolved(value);
        } catch (failure) {
          setCard(null); setError(failure.message);
        } finally { setLoading(false); }
      };

      const changeColor = (card?.change_pct ?? 0) >= 0
        ? "var(--dsw-alias-state-success-primary,#2ea043)" : "var(--dsw-alias-state-error-primary,#d1242f)";
      return h(React.Fragment, null,
        h(Card, { title: "标的查询" },
          h("div", { className: "tw-toolbar" },
            h("input", { className: "tw-input", value: ticker, placeholder: "00700.HK / AAPL / 600519",
              "aria-label": "标的代码", onChange: (e) => setTicker(e.target.value),
              onKeyDown: (e) => { if (e.key === "Enter") lookup(); } }),
            h("button", { type: "button", className: "tw-btn primary", disabled: loading, onClick: lookup },
              loading ? "查询中…" : "查询")),
          error && h("p", { className: "tw-alert" }, error),
          !card && !error && h("p", { className: "tw-empty" }, "输入标的代码查询名称/价格/区间与富途跳转。")),
        card && h(Card, { title: `${card.name ?? card.symbol}${card.name_en ? ` · ${card.name_en}` : ""}` },
          h("div", { className: "tw-kv" },
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "最新价"),
              h("div", { className: "tw-kv-v" }, card.price ?? "—")),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "涨跌幅"),
              h("div", { className: "tw-kv-v", style: { color: changeColor } },
                card.change_pct === null || card.change_pct === undefined ? "—" : `${card.change_pct >= 0 ? "+" : ""}${card.change_pct}%`)),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "今开 / 昨收"),
              h("div", { className: "tw-kv-v" }, `${card.open ?? "—"} / ${card.prev_close ?? "—"}`)),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "最高 / 最低"),
              h("div", { className: "tw-kv-v" }, `${card.high ?? "—"} / ${card.low ?? "—"}`)),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "成交量"),
              h("div", { className: "tw-kv-v" }, card.volume ? Number(card.volume).toLocaleString() : "—")),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "成交额"),
              h("div", { className: "tw-kv-v" }, card.turnover ? Number(card.turnover).toLocaleString() : "—")),
            card.lot_size && h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "每手股数"),
              h("div", { className: "tw-kv-v" }, String(card.lot_size)))),
          h("div", { className: "tw-toolbar", style: { marginTop: "4px" } },
            h("a", { className: "tw-btn primary", href: card.futu_url, target: "_blank", rel: "noreferrer" },
              "在富途查看K线 ↗"),
            h("span", { className: "tw-meta" }, `${card.symbol} · 数据 ${card.as_of ?? "—"} · ${card.source ?? "—"}`)),
          card.note && h("p", { className: "tw-hint" }, card.note),
          h("p", { className: "tw-hint" }, "工作台不内置行情图表；K线请点击上方链接在富途查看。")));
    }

    function SignalView({ snapshot }) {
      const previews = snapshot.previews.filter((p) => p.kind === "signal" || p.kind === "backtest");
      return h(React.Fragment, null,
        h(Card, { title: "量化信号与回测（由 Harness 计算）", count: previews.length,
          empty: "暂无信号。请在 Harness 会话中请求（如“看下 600519 的信号”），结果显示在这里。" },
          h(Paged, { items: previews, pageSize: 8, empty: "暂无信号预览",
            render: (p) => h("details", { key: p.id, className: "tw-item" },
              h("summary", null,
                h("span", { className: `tw-tag ${p.value?.signal === "BUY" ? "buy" : p.value?.signal === "SELL" ? "sell" : "hold"}` },
                  p.value?.signal ?? p.kind),
                `${p.value?.ticker ?? ""}`,
                h("span", { className: "tw-meta" }, p.at)),
              h("div", { className: "tw-item-body" }, h("pre", { className: "tw-pre" }, JSON.stringify(p.value, null, 2)))) })),
        h("p", { className: "tw-hint" }, "工作台只展示结果；信号计算、回测与下单请在 Harness 会话中发起。"));
    }

    /** 一行券商持仓。 */
    function BrokerPositionRow({ row }) {
      const pnl = row.pl_val ?? 0;
      return h("div", { className: "tw-kv-item" },
        h("div", { className: "tw-kv-k" }, `${row.symbol || "—"} ${row.name}`),
        h("div", { className: "tw-kv-v", style: { color: pnl >= 0 ? "var(--dsw-alias-state-success-primary,#2ea043)" : "var(--dsw-alias-state-error-primary,#d1242f)" } },
          `${row.qty ?? "—"} 股 · 成本 ${row.cost_price ?? "—"} → 现价 ${row.price ?? "—"}`
          + ` · 市值 ${row.market_value ?? "—"}（${pnl >= 0 ? "+" : ""}${row.pl_val ?? "—"} / ${row.pl_ratio ?? "—"}%）`));
    }

    /** 券商真实持仓：按账户分组，从不跨账户/币种合并。 */
    function PortfolioView({ rpc, snapshot }) {
      const ledgerPreview = snapshot.previews.find((p) => p.kind === "ledger");
      const broker = useEndpoint(rpc, "positions", { mode: snapshot.mode }, [rpc, snapshot.mode]);
      const equity = useEndpoint(rpc, "equity", { mode: snapshot.mode, window: 250 }, [rpc, snapshot.mode]);
      const groups = broker.data?.groups ?? [];
      const counts = broker.data?.counts ?? {};
      const points = equity.data?.points ?? [];

      const accountCard = (group) => h(Card, {
        key: group.acc_id,
        title: group.kind === "real" ? group.account : `${group.account}（模拟）`,
        count: group.positions.length,
      },
        h(Paged, { items: group.positions, pageSize: 8,
          render: (row) => h(BrokerPositionRow, { key: `${group.acc_id}-${row.symbol}`, row }) }),
        h("p", { className: "tw-meta" },
          group.subtotals
            ? `小计（按币种分开）：${group.subtotals.map((s) => `${s.currency} 市值 ${s.market_value} / 盈亏 ${s.pl_val}`).join(" · ")}`
            : `小计：市值 ${group.market_value ?? "—"} · 盈亏 ${group.pl_val ?? "—"}`));

      return h(React.Fragment, null,
        h(Card, { title: "券商持仓（富途真实数据）", count: counts.positions ?? 0,
          empty: broker.loading ? "读取中…" : (broker.error || "当前账户无持仓") },
          h("p", { className: "tw-meta" },
            `${snapshot.mode === "live" ? "实盘" : "模拟盘"} · 数据源 ${broker.data?.source ?? "—"}`
            + ` · 取数于 ${broker.data?.as_of ?? "—"}`
            + (broker.data?.cached ? "（本地缓存）" : "")
            + (broker.data?.stale ? " ⚠ 实时读取失败，展示上次缓存" : "")
            + ` · 检查 ${counts.accounts_checked ?? "—"} 个账户，`
            + `${counts.accounts_with_positions ?? 0} 个有持仓`),
          broker.data?.error && h("p", { className: "tw-alert" }, broker.data.error),
          (broker.data?.errors ?? []).length > 0 && h("p", { className: "tw-alert" },
            `部分账户读取失败：${broker.data.errors.map((e) => `${e.account}(${e.reason})`).join("；")}`),
          h("p", { className: "tw-meta" }, broker.data?.note ?? ""),
        ),
        ...groups.map(accountCard),

        h(Card, { title: "本地策略台账（不是券商资产）", count: points.length,
          empty: points.length > 1 ? undefined : (equity.loading ? "加载中…" : "暂无本地策略成交记录") },
          h("p", { className: "tw-meta" },
            "来源：本地模拟台账 ~/.dsh/quant-ledger.json，用于回放 quant_signal/quant_backtest 的策略表现；"
            + "与上面的券商持仓是两套账，不要相加。"
            + (ledgerPreview ? ` · 最近台账预览 ${ledgerPreview.at}` : "")),
          h(LineChart, { points: points.map((p) => ({ v: p.equity })), label: equity.data?.note || "" }),
          equity.data && h("p", { className: "tw-meta" },
            `区间 ${points[0]?.t} → ${points[points.length - 1]?.t} · 最大回撤 ${(equity.data.max_drawdown * 100).toFixed(2)}% · 夏普 ${equity.data.sharpe}`)));
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
      const held = (positions.data?.positions ?? []).map((p) => p.ticker).slice(0, 6);
      const correlation = useEndpoint(rpc, "correlation", { tickers: held, window: 120 }, [rpc, held.join(",")]);
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

    /** IC 检验当前只覆盖价量因子（估值因子需历史估值序列，后续接入）。 */
    const IC_FACTORS = { mom_20: 1, mom_60: 1, vol_20: 1, trend: 1, rsi_14: 1, liq_ratio: 1, mdd_60: 1 };

    const FACTOR_LABELS = {
      mom_20: "动量20", mom_60: "动量60", vol_20: "波动率", trend: "趋势偏离",
      rsi_14: "RSI14", liq_ratio: "量能比", mdd_60: "最大回撤",
      pe_ttm: "PE(TTM)", pb: "PB", peg: "PEG", ps: "PS",
    };

    function FactorsView({ rpc, ticker, watchlist, setWatchlist }) {
      const [factor, setFactor] = React.useState("mom_20");
      const tickers = watchlist;
      const enough = tickers.length >= 2;
      const snap = useEndpoint(rpc, "factors", enough ? { tickers, window: 250 } : {}, [rpc, tickers.join(",")]);
      const ic = useEndpoint(rpc, "ic", tickers.length >= 3 ? { tickers, factor, forward: 5, window: 250 } : {},
        [rpc, tickers.join(","), factor]);
      const rows = snap.data?.rows ?? [];
      const icPoints = (ic.data?.points ?? []).map((p) => ({ v: p.ic }));
      return h(React.Fragment, null,
        h(Card, { title: "标的池（默认空，需自行输入）", count: tickers.length },
          h("div", { className: "tw-toolbar" },
            h("input", { className: "tw-input", value: tickers.join(","), "aria-label": "标的池（逗号分隔，2..8个）",
              onChange: (e) => setWatchlist(e.target.value.toUpperCase().split(",").map((t) => t.trim()).filter(Boolean).slice(0, 8)) }),
            h("button", { type: "button", className: "tw-btn", onClick: () => setWatchlist([ticker, ...tickers.filter((t) => t !== ticker)].slice(0, 8)) },
              `加入当前标的 ${ticker}`)),
          h("p", { className: "tw-hint" }, "因子：价量（7）+ 估值（PE/PB/PEG/PS，同花顺源）；横截面 z-score 合成打分，估值越低分越高。")),
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
          h("div", { className: "tw-toolbar" }, Object.keys(IC_FACTORS).map((k) =>
            h("button", { key: k, type: "button", className: `tw-btn seg${factor === k ? " active" : ""}`,
              onClick: () => setFactor(k) }, FACTOR_LABELS[k] ?? k))),
          ic.data && h("div", { className: "tw-kv" },
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "均值 IC"), h("div", { className: "tw-kv-v" }, String(ic.data.mean_ic))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "IC 标准差"), h("div", { className: "tw-kv-v" }, String(ic.data.ic_std))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "ICIR"), h("div", { className: "tw-kv-v" }, String(ic.data.icir))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "正 IC 占比"), h("div", { className: "tw-kv-v" }, String(ic.data.positive_ratio))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "样本期数"), h("div", { className: "tw-kv-v" }, String(ic.data.count)))),
          icPoints.length > 1 && h(LineChart, { points: icPoints, label: "IC 序列" }),
          ic.data && h("p", { className: "tw-hint" }, ic.data.note)));
    }

    /** 交易概要：把「调了哪些工具」归纳成「发生了什么交易」。 */
    function tradeWhen(iso) {
      return iso ? String(iso).replace("T", " ").slice(0, 16) : "时间未知";
    }

    function tradeMoney(value) {
      return (value === null || value === undefined)
        ? "—" : value.toLocaleString("en-US", { maximumFractionDigits: 2 });
    }

    /** 一行订单事实：数量/价格/成交情况全部来自券商原文，不重算。 */
    function TradeOrderRow({ row }) {
      const sideTag = row.side === "买入"
        ? h("span", { className: "tw-tag buy" }, "买入")
        : row.side === "卖出"
          ? h("span", { className: "tw-tag sell" }, "卖出")
          : h("span", { className: "tw-tag" }, `side=${row.side_code}`);
      return h("div", { className: "tw-kv-item" },
        h("div", { className: "tw-kv-k" },
          `${tradeWhen(row.ordered_at)} · ${row.symbol || "—"} ${row.name} `,
          sideTag,
          row.cancelled ? h("span", { className: "tw-tag sell" }, "已撤单") : null,
          row.modified_count > 0 ? h("span", { className: "tw-tag" }, `改单${row.modified_count}次`) : null,
          !row.cancelled && row.fill !== "全部成交" ? h("span", { className: "tw-tag hold" }, row.fill) : null),
        h("div", { className: "tw-kv-v" },
          `${row.qty ?? "—"} 股 @ 委托 ${tradeMoney(row.price)} · `
          + `成交 ${row.filled_qty ?? "—"} 股 @ 均价 ${tradeMoney(row.avg_fill_price)}`),
        h("div", { className: "tw-meta" },
          `成交金额 ${tradeMoney(row.amount)} · 订单号 ${row.order_id} · status=${row.status_code ?? "—"}`));
    }

    /** 一行下单/改单/撤单动作。 */
    function TradeActionRow({ row }) {
      const failed = h("span", { style: { color: "var(--dsw-alias-state-error-primary,#d1242f)" } },
        `失败：${row.detail}`);
      return h("div", { className: "tw-kv-item" },
        h("div", { className: "tw-kv-k" }, `${tradeWhen(row.at)} · ${row.action}`),
        h("div", { className: "tw-kv-v" },
          row.ok ? `已提交${row.order_id ? ` · 订单号 ${row.order_id}` : ""}` : failed));
    }

    /** 原始响应：保留为可展开证据，但不再是主列表。 */
    function RawResponseRow({ row }) {
      return h("details", { className: "tw-item" },
        h("summary", null, `${tradeWhen(row.at)} · ${row.tool ?? row.kind}`,
          row.is_error ? h("span", { className: "tw-tag sell" }, "失败") : null),
        h("div", { className: "tw-item-body" },
          h("pre", { className: "tw-pre" }, JSON.stringify(row.value ?? row, null, 2))));
    }

    function TradeSummaryCard({ summary, activity }) {
      const data = summary ?? {};
      const orders = data.orders ?? [];
      const actions = data.actions ?? [];
      const queries = data.queries ?? { count: 0, tools: [] };
      const counts = data.counts ?? {};
      const responses = activity ?? [];

      const stat = (label, value, tone) => h("div", { className: "tw-kv-item" },
        h("div", { className: "tw-kv-k" }, label),
        h("div", { className: "tw-kv-v", style: tone ? { color: tone } : undefined }, String(value)));

      if (orders.length === 0 && actions.length === 0) {
        return h(Card, { title: "交易概要", empty: counts.responses
          ? `观察到的 ${counts.responses} 条券商响应中没有交易事实（均为账户查询）`
          : "暂无券商响应记录" });
      }

      const stats = h("div", { className: "tw-kv" },
        stat("订单", `${orders.length} 笔`),
        stat("下单/改单/撤单", `${actions.length} 次`),
        stat("账户查询", `${queries.count} 次`),
        counts.errors
          ? stat("失败", `${counts.errors} 次`, "var(--dsw-alias-state-error-primary,#d1242f)")
          : null);

      const actionBlock = actions.length === 0 ? null : h(React.Fragment, null,
        h("p", { className: "tw-meta" }, "下单/改单/撤单动作"),
        h(Paged, { items: actions, pageSize: 6, render: (row) => h(TradeActionRow, {
          key: row.entry_id ?? `${row.at}-${row.action}`, row }) }));

      const queryBlock = queries.tools.length === 0 ? null
        : h("p", { className: "tw-meta" },
          `另有 ${queries.count} 次只读查询未列出：`
          + queries.tools.map((row) => `${row.tool || "unknown"}×${row.count}`).join("、"));

      const rawBlock = responses.length === 0 ? null : h("details", { className: "tw-item" },
        h("summary", null, `原始券商响应（${responses.length} 条，审计核对用）`),
        h("div", { className: "tw-item-body" },
          h(Paged, { items: responses, pageSize: 10,
            render: (row) => h(RawResponseRow, { key: row.id, row }) })));

      return h(Card, { title: "交易概要", count: orders.length },
        stats,
        h("p", { className: "tw-meta" }, data.notice ?? ""),
        h("p", { className: "tw-meta" }, "订单明细（券商返回，按 order_id 去重后保留最新状态）"),
        h(Paged, { items: orders, pageSize: 8, empty: "暂无订单记录",
          render: (row) => h(TradeOrderRow, { key: row.order_id, row }) }),
        actionBlock,
        queryBlock,
        rawBlock);
    }

    function ExecutionView({ rpc, snapshot }) {
      const trades = useEndpoint(rpc, "trades", { mode: snapshot.mode, limit: 50 }, [rpc, snapshot.mode]);
      const rows = trades.data?.trades ?? [];
      return h(React.Fragment, null,
        h(Card, { title: "成交记录（本地台账）", count: rows.length,
          empty: trades.loading ? "加载中…" : (trades.error || "暂无成交记录") },
          h(Paged, { items: rows, pageSize: 10, empty: trades.loading ? "加载中…" : (trades.error || "暂无成交记录"),
            render: (row, i) => h("div", { key: i, className: "tw-kv-item" },
              h("div", { className: "tw-kv-k" }, `${row.date} · ${row.ticker}`),
              h("div", { className: "tw-kv-v", style: { color: row.action === "BUY" ? "var(--dsw-alias-state-success-primary,#2ea043)" : "var(--dsw-alias-state-error-primary,#d1242f)" } },
                `${row.action} ${row.shares} @ ${row.price}`),
              h("div", { className: "tw-meta" },
                `费用 ${row.fee ?? "—"}${row.return !== undefined && row.return !== null ? ` · 收益 ${(row.return * 100).toFixed(2)}%` : ""}${row.reason ? ` · ${row.reason}` : ""}`)) }),
          trades.data && h("p", { className: "tw-meta" },
            `共 ${trades.data.total} 笔 · 胜率 ${trades.data.win_rate === null ? "—" : `${(trades.data.win_rate * 100).toFixed(0)}%`} · 累计费用 ${trades.data.total_fees}`)),
        h(TradeSummaryCard, { summary: snapshot.trade_summary, activity: snapshot.activity }));
    }

    function ResearchView({ rpc, snapshot, ticker, onOpenReport }) {
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
        h(Card, { title: "研报列表（点击标题查看详情）", count: snapshot.reports.length },
          running.length > 0 && h("p", { className: "tw-meta" },
            `进行中：${running.map((run) => `${run.ticker} (${run.started_at})`).join("、")}`),
          h(Paged, { items: snapshot.reports, pageSize: 8,
            empty: "暂无研报。在 Harness 中要求完整投研后，发布结果会显示在这里。",
            render: (report) => h("div", { key: report.id, className: "tw-item" },
              h("div", { className: "tw-item-body", style: { paddingTop: "8px" } },
                h("button", { type: "button", className: "tw-link", onClick: () => onOpenReport(report) },
                  `📄 ${report.ticker} · ${report.rating} · ${report.published_at}`),
                h("div", { className: "tw-meta" }, (report.report || "").replace(/\s+/g, " ").slice(0, 120) + "…"))) })),
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
        h(Card, { title: `即将发生的事件（${ticker}）`, count: upcoming.length },
          h(Paged, { items: upcoming, pageSize: 8,
            empty: events.loading ? "加载中…" : (events.error || "窗口内无即将发生的事件"),
            render: (event, index) => line(event, index) }),
          events.data && h("p", { className: "tw-meta" },
            `来源状态：${Object.entries(events.data.sources_status || {}).map(([k, v]) => `${k}=${v}`).join(" · ")}`)),
        h(Card, { title: "近期已发生", count: past.length },
          h(Paged, { items: past, pageSize: 8, empty: "窗口内无历史事件",
            render: (event, index) => line(event, index) })),
        h("p", { className: "tw-hint" }, events.data?.note
          || "事件来自公开披露源；港股/美股事件请用富途工具查询（quote_financials_* / quote_corporate_actions_* / quote_economic_calendar_search）。"));
    }

    function SourcesCard({ rpc, revision }) {
      const sources = useEndpoint(rpc, "sources", {}, [rpc, revision]);
      const rows = sources.data?.sources ?? [];
      const icon = { ok: "✅", warn: "⚠️", fail: "❌" };
      return h(Card, { title: "数据源与授权状态", count: rows.length,
        empty: sources.loading ? "自检中…" : (sources.error || "不可用") },
        rows.map((row) => h("div", { key: row.key, className: "tw-item" },
          h("div", { className: "tw-item-body", style: { paddingTop: "8px" } },
            h("span", { className: `tw-tag ${row.status === "ok" ? "buy" : row.status === "fail" ? "sell" : "hold"}` },
              `${icon[row.status] ?? ""} ${row.label}`),
            h("div", { className: "tw-meta" }, row.detail),
            row.status !== "ok" && row.fix && h("div", { className: "tw-meta" }, `修复：${row.fix}`)))),
        sources.data && h("p", { className: "tw-meta" },
          `自检时间 ${sources.data.checked_at} · 正常 ${sources.data.summary.ok} / 待配置 ${sources.data.summary.warn} / 异常 ${sources.data.summary.fail}`));
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
        h(Card, { title: "审计时间线（信号 → 下单 → 成交）", count: entries.length },
          h(Paged, { items: entries, pageSize: 12,
            empty: "暂无记录：先在 Harness 中产生信号，再下单/成交后这里会出现链路。",
            render: (entry) => {
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
            } })),
        h(SourcesCard, { rpc, revision: snapshot.generated_at }),
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
      const [ticker, setTicker] = React.useState("");  // 无默认标的：未查询时留空
      const [watchlist, setWatchlist] = React.useState([]);  // 标的池默认空，由用户输入
      const [detail, setDetail] = React.useState(null);
      const generation = React.useRef(0);
      const [sources, setSources] = React.useState(null);
      React.useEffect(() => {
        if (!open) return;
        let alive = true;
        const controller = new AbortController();
        request(rpc, "sources", {}, controller.signal)
          .then((value) => { if (alive) setSources(value); })
          .catch(() => { if (alive) setSources(null); });
        return () => { alive = false; controller.abort(); };
      }, [open, revision, rpc]);

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
            // 面板不需要实时：默认 60 秒一次兜底刷新；用户可点「刷新」即时更新
            if (!controller.signal.aborted) timer = setTimeout(refresh, SNAPSHOT_POLL_MS);
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
        finally { setSwitching(false); invalidateCaches(); setRevision((v) => v + 1); }
      };

      const live = snapshot?.mode === "live";
      // Host 会声明它实际提供哪些接口（stale 进程会缺一批）
      const served = Array.isArray(snapshot?.endpoints) ? snapshot.endpoints : null;
      const missing = served ? KNOWN_ENDPOINTS.filter((name) => !served.includes(name)) : [];
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
            h("button", { type: "button", className: "tw-btn seg", title: "清除本地缓存并重新取数",
              onClick: () => { invalidateCaches(); setError(""); setRevision((v) => v + 1); } }, "刷新"),
            sources && h("span", { className: `tw-badge${sources.summary.fail > 0 ? " live" : ""}`,
              title: sources.sources.map((s) => `${s.label}: ${s.detail}`).join("\n") },
              sources.summary.fail > 0 ? `数据源 ${sources.summary.fail} 项异常`
                : sources.summary.warn > 0 ? `数据源 ${sources.summary.warn} 项待配置` : "数据源正常"),
            h("button", { type: "button", className: "tw-close", onClick: () => setOpen(false), "aria-label": "关闭" }, "×")),
          h("div", { className: "tw-main" },
            h("nav", { className: "tw-nav" }, NAV.map((item) =>
              h("button", { key: item.id, type: "button", className: `tw-nav-item${tab === item.id ? " active" : ""}`,
                onClick: () => setTab(item.id) }, h("span", { className: "tw-nav-dot" }), item.label))),
            h("div", { className: "tw-content" },
              missing.length > 0 && h("p", { role: "alert", className: "tw-alert" },
                `当前 Harness 进程未加载 ${missing.length} 个工作台接口（${missing.join("、")}）：`
                + "该进程启动早于插件更新，Host 半边只在启动时加载一次。"
                + "重启 dsh web 后即可用；在此之前这些页签不会发起请求。"),
              switchError && h("p", { role: "alert", className: "tw-alert" }, `模式切换失败：${switchError}`),
              error && h("p", { role: "alert", className: "tw-alert" }, `更新失败：${error}`),
              !snapshot && h("p", { className: "tw-status" }, switching ? "正在切换模式…" : "正在读取工作台…"),
              detail && h(ReportDetail, { report: detail, onBack: () => setDetail(null) }),
              !detail && tab === "market" && h(MarketView, { rpc, onResolved: setTicker }),
              !detail && snapshot && tab === "signal" && h(SignalView, { snapshot }),
              !detail && snapshot && tab === "portfolio" && h(PortfolioView, { rpc, snapshot }),
              !detail && snapshot && tab === "risk" && h(RiskView, { rpc, snapshot }),
              !detail && tab === "factors" && h(FactorsView, { rpc, ticker, watchlist, setWatchlist }),
              !detail && snapshot && tab === "execution" && h(ExecutionView, { rpc, snapshot }),
              !detail && snapshot && tab === "research" && h(ResearchView, { rpc, snapshot, ticker, onOpenReport: setDetail }),
              !detail && tab === "events" && h(EventsView, { rpc, ticker }),
              !detail && snapshot && tab === "audit" && h(AuditView, { rpc, snapshot }),
              snapshot && h("div", { className: "tw-row" },
                h("p", { className: "tw-hint" },
                  "面板数据按 TTL 本地缓存（分钟级），不追求实时行情；"
                  + "点「刷新」可清除缓存并重新取数。实时价格请用富途行情工具查询。"),
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

    // `internals` 仅供测试断言缓存与接口自检行为，不参与运行时逻辑
    return { inject: ["slots", "connection"], apply, request,
      internals: { readCache, writeCache, invalidateCaches, KNOWN_ENDPOINTS, CLIENT_TTL_MS,
        servedEndpoints: () => servedEndpoints, cacheSize: () => endpointCache.size,
        missingEndpoints: () => [...missingEndpoints] } };
  },
});
