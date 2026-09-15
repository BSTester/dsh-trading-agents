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
/* 关键：.tw-content 是「可滚动 + flex 纵向」，flex 子项默认 flex-shrink:1。
   卡片总高超过抽屉高度时会被压缩，再配合下面的 overflow:hidden 就把内容裁掉了
   ——表现为"卡片被挡住/看不全"。用 flex:0 0 auto 禁止收缩，让容器去滚动而不是压缩卡片。 */
.tw-content>*{flex:0 0 auto}
.tw-card{border:1px solid var(--dsw-alias-border-l1,GrayText);border-radius:10px;background:var(--dsw-alias-bg-layer-2,Canvas);overflow:hidden}
.tw-card-head{display:flex;align-items:center;gap:8px;padding:10px 12px;font-weight:600;font-size:13px;border-bottom:1px solid var(--dsw-alias-border-l1,GrayText)}
.tw-count{margin-left:auto;font-size:11px;font-weight:600;padding:1px 8px;border-radius:999px;color:var(--dsw-alias-label-secondary,GrayText);background:var(--dsw-alias-interactive-bg-hover,rgba(128,128,128,.14))}
.tw-card-body{padding:12px;display:flex;flex-direction:column;gap:10px}
.tw-card-body>*{flex:0 0 auto}
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

/* ---- 研报正文（Markdown）----
   排版目标是"能读"：研报是长文，标题层级、列表缩进、表格边框、
   代码块与引用的区分都要一眼可辨，而不是一堵等宽字体的墙。 */
.tw-md{font-size:13px;line-height:1.75;color:var(--dsw-alias-label-primary,CanvasText);overflow-wrap:anywhere}
.tw-md>*:first-child{margin-top:0}
.tw-md>*:last-child{margin-bottom:0}
.tw-md-h{margin:18px 0 8px;font-weight:650;line-height:1.35;letter-spacing:-0.01em}
.tw-md-h1{font-size:19px;padding-bottom:6px;border-bottom:1px solid var(--dsw-alias-border-l1,GrayText)}
.tw-md-h2{font-size:16px;padding-bottom:5px;border-bottom:1px solid var(--dsw-alias-border-l1,GrayText)}
.tw-md-h3{font-size:14.5px}
.tw-md-h4,.tw-md-h5,.tw-md-h6{font-size:13.5px;color:var(--dsw-alias-label-secondary,GrayText)}
.tw-md-p{margin:9px 0}
.tw-md-list{margin:9px 0;padding-left:22px}
.tw-md-list li{margin:4px 0}
.tw-md-list li::marker{color:var(--dsw-alias-label-tertiary,GrayText)}
.tw-md-code{padding:1px 5px;border-radius:5px;font-size:12px;font-family:var(--ds-font-family-code,ui-monospace,Menlo,monospace);background:var(--dsw-alias-bg-layer-2,rgba(127,127,127,.14));border:1px solid var(--dsw-alias-border-l1,rgba(127,127,127,.22))}
.tw-md-pre{position:relative;margin:11px 0;padding:12px 13px;border-radius:9px;overflow:auto;max-height:420px;font-size:12px;line-height:1.6;font-family:var(--ds-font-family-code,ui-monospace,Menlo,monospace);background:var(--dsw-alias-bg-base,Canvas);border:1px solid var(--dsw-alias-border-l1,GrayText)}
.tw-md-pre code{white-space:pre;background:none;border:none;padding:0;font-size:inherit}
.tw-md-lang{position:absolute;top:6px;right:10px;font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--dsw-alias-label-tertiary,GrayText)}
.tw-md-quote{margin:11px 0;padding:2px 0 2px 13px;border-left:3px solid var(--dsw-alias-label-primary-bluish,LinkText);color:var(--dsw-alias-label-secondary,GrayText)}
.tw-md-quote .tw-md-p{margin:6px 0}
.tw-md-hr{margin:16px 0;border:none;border-top:1px solid var(--dsw-alias-border-l1,GrayText)}
.tw-md-table-wrap{margin:11px 0;overflow-x:auto;border:1px solid var(--dsw-alias-border-l1,GrayText);border-radius:9px}
.tw-md-table{width:100%;border-collapse:collapse;font-size:12.5px}
.tw-md-table th{text-align:left;font-weight:650;padding:7px 10px;white-space:nowrap;background:var(--dsw-alias-bg-layer-2,rgba(127,127,127,.1))}
.tw-md-table td{padding:7px 10px;border-top:1px solid var(--dsw-alias-border-l1,rgba(127,127,127,.22));vertical-align:top}
.tw-md-table tbody tr:nth-child(even){background:var(--dsw-alias-bg-layer-1,rgba(127,127,127,.04))}
.tw-md-link{color:var(--dsw-alias-label-primary-bluish,LinkText);text-decoration:none;border-bottom:1px solid color-mix(in srgb,currentColor 35%,transparent)}
.tw-md-link:hover{border-bottom-color:currentColor}
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
      "sources", "instrument", "quality", "plan", "plan-execute", "schedule", "reconcile"];

    // 面板是查看用途，不需要实时。结果缓存在内存里，切页签/重开面板不再重复请求；
    // Host 侧另有 TTL 缓存，两层都命中时连 python 子进程都不会启动。
    const CLIENT_TTL_MS = {
      instrument: 5 * 60_000, series: 5 * 60_000, equity: 2 * 60_000, positions: 2 * 60_000,
      correlation: 10 * 60_000, sensitivity: 30 * 60_000, risk: 5 * 60_000, trades: 60_000,
      events: 30 * 60_000, factors: 10 * 60_000, ic: 10 * 60_000, audit: 60_000,
      sources: 2 * 60_000, quality: 30 * 60_000,
      plan: 30_000, schedule: 15_000, reconcile: 2 * 60_000,
    };
    const CACHE_MAX_ENTRIES = 60;
    /** 兜底轮询间隔：面板是查看用途，切页签有缓存，不需要秒级刷新。 */
    const SNAPSHOT_POLL_MS = 60_000;

    // K 线周期预设。实测：一次富途往返无论周期都是约 2.8-5.1 秒、约 29 KB，
    // 因此分钟级并不比日线贵——差别只在"一次调用能看多长"。
    // 富途单次上限 370 根，故根数不得超过它。
    const KLINE_PERIODS = [
      { id: "1d", label: "日线", limit: 250 },   // 约 1 年
      { id: "60m", label: "60分", limit: 200 },
      { id: "15m", label: "15分", limit: 200 },
      { id: "5m", label: "5分", limit: 240 },    // 约 20 小时
      { id: "1m", label: "1分", limit: 240 },    // 约一个交易日
    ];
    /** K 线图的绘制留白：命中判定与绘制共用同一套，避免两处各写一份而错位。 */
    const KLINE_PAD = { padL: 54, padR: 12, padT: 10, padB: 18 };

    /** 鼠标 x 坐标 → K 线下标；落在绘图区外返回 null。 */
    function barIndexAt(x, { padL, plotW, count }) {
      if (!count || plotW <= 0) return null;
      const step = plotW / count;
      const index = Math.floor((x - padL) / step);
      return index >= 0 && index < count ? index : null;
    }

    /** 信息框左上角 x：右侧放不下就翻到光标左侧，再不行贴右边缘。 */
    function tooltipLeft(cursorX, boxWidth, chartWidth, gap = 14) {
      const right = cursorX + gap;
      if (right + boxWidth <= chartWidth - 4) return right;
      const left = cursorX - gap - boxWidth;
      return left >= 4 ? left : Math.max(4, chartWidth - 4 - boxWidth);
    }

    /** 成交量等大数的紧凑写法（图内空间有限）。 */
    function compactNumber(value) {
      // 注意 Number(null) === 0、Number("") === 0 —— 缺失必须显示为"—"，
      // 否则"没有成交量"会被显示成"成交量 0"，是两回事。
      if (value === null || value === undefined || value === "") return "—";
      const number = Number(value);
      if (!Number.isFinite(number)) return "—";
      const abs = Math.abs(number);
      if (abs >= 1e8) return `${(number / 1e8).toFixed(2)}亿`;
      if (abs >= 1e4) return `${(number / 1e4).toFixed(2)}万`;
      return number.toLocaleString("en-US", { maximumFractionDigits: 0 });
    }

    const KLINE_DEFAULT_PERIOD = "1d";
    const FUTU_MAX_BARS = 370;
    const endpointCache = new Map();
    // Host 在 snapshot 里声明它实际提供哪些接口；null 表示尚未获知（旧版 Host 不声明）
    let servedEndpoints = null;
    // 撞过 404 的接口：即使 Host 没声明，也能据此停止重试并说明原因
    const missingEndpoints = new Set();
    // endpoint → 数据算于何时（Host 返回的 cached_at）
    const lastServedAt = new Map();
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

    /**
     * 数值展示：非有限数一律显示 "—"。
     *
     * 不要写成 `x ? \`${(x.y * 100).toFixed(2)}%\` : "—"`——对象存在不等于字段存在。
     * 实测 analytics.py 在台账为空时返回 {points:[],count:0,note:...}，
     * 没有 max_drawdown/sharpe，于是界面会显示 "NaN%" 和 "undefined"。
     */
    function numeric(value, digits = 2, suffix = "") {
      return Number.isFinite(value) ? `${value.toFixed(digits)}${suffix}` : "—";
    }

    /** 比例转百分比（0.18 → 18.00%）：不可得时只显示「—」，不能拼成「—%」那种半截单位。 */
    function percent(ratio, digits = 2) {
      return Number.isFinite(ratio) ? `${(ratio * 100).toFixed(digits)}%` : "—";
    }

    /** 已经是百分数的数值（45.67 → 45.67%）。注意别先做除法：null/100 会变成 0。 */
    function percentValue(value, digits = 2) {
      return Number.isFinite(value) ? `${value.toFixed(digits)}%` : "—";
    }

    /** 当前客户端缓存条目数（仅用于界面显示，让"是否在缓存"可见）。 */
    const cacheSize = () => endpointCache.size;

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
      // 记录本次数据算于何时（Host 缓存命中时是更早的时间），供界面显示
      if (typeof result.cached_at === "string") lastServedAt.set(endpoint, result.cached_at);
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

    /** K 线蜡烛图：canvas 自绘，不引入依赖。 */
    /** 悬停时的准星、价格标签与信息框。 */
    function drawKLineHover(ctx, width, height, color, bar, index, geometry, bars) {
      const { padL, padR, padT, padB, plotW, plotH, min, max } = geometry;
      const y = (value) => padT + plotH * (1 - (value - min) / (max - min));
      const step = plotW / bars.length;
      const centerX = padL + step * (index + 0.5);

      // 竖向准星
      ctx.setLineDash([3, 3]); ctx.strokeStyle = color.line; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(centerX, padT); ctx.lineTo(centerX, height - padB); ctx.stroke();
      ctx.setLineDash([]);

      // 右侧价格标签（对齐收盘价）
      const priceY = y(bar.c);
      const priceText = bar.c.toFixed(2);
      ctx.font = "10px sans-serif";
      const tagW = ctx.measureText(priceText).width + 8;
      ctx.fillStyle = bar.c >= bar.o ? color.up : color.down;
      ctx.fillRect(width - padR, priceY - 7, tagW, 14);
      ctx.fillStyle = "#fff";
      ctx.fillText(priceText, width - padR + 4, priceY + 3);

      // 信息框：日期 + 开高低收 + 涨跌 + 量
      const previous = index > 0 ? bars[index - 1] : null;
      const change = previous && previous.c ? ((bar.c - previous.c) / previous.c) * 100 : null;
      const lines = [
        String(bar.t),
        `开 ${bar.o}   高 ${bar.h}`,
        `低 ${bar.l}   收 ${bar.c}`,
        change === null ? `量 ${compactNumber(bar.v)}`
          : `涨跌 ${change >= 0 ? "+" : ""}${change.toFixed(2)}%   量 ${compactNumber(bar.v)}`,
      ];
      ctx.font = "11px sans-serif";
      const boxW = Math.max(...lines.map((line) => ctx.measureText(line).width)) + 16;
      const boxH = lines.length * 15 + 10;
      const boxX = tooltipLeft(centerX, boxW, width);
      let boxY = padT + 4;
      if (boxY + boxH > height - padB) boxY = Math.max(4, height - padB - boxH);
      ctx.fillStyle = "rgba(20,20,24,.92)";
      ctx.fillRect(boxX, boxY, boxW, boxH);
      ctx.strokeStyle = color.grid; ctx.lineWidth = 1;
      ctx.strokeRect(boxX + 0.5, boxY + 0.5, boxW - 1, boxH - 1);
      ctx.fillStyle = "#f0f0f0";
      lines.forEach((line, row) => {
        ctx.fillText(line, boxX + 8, boxY + 16 + row * 15);
      });
    }

    function KLineChart({ bars }) {
      const [hover, setHover] = React.useState(null);
      const geometry = React.useRef(null);

      const ref = useCanvasChart((ctx, width, height, color) => {
        geometry.current = null;
        if (!bars || bars.length < 2) {
          ctx.fillStyle = color.text; ctx.font = "12px sans-serif";
          ctx.fillText("暂无 K 线数据", 12, 22); return;
        }
        const { padL, padR, padT, padB } = KLINE_PAD;
        const plotW = width - padL - padR;
        const plotH = height - padT - padB;
        let min = Math.min(...bars.map((b) => b.l));
        let max = Math.max(...bars.map((b) => b.h));
        if (max - min < 1e-9) { max += 1; min -= 1; }
        const span = max - min; min -= span * 0.05; max += span * 0.05;
        const y = (value) => padT + plotH * (1 - (value - min) / (max - min));
        // 命中判定要用与绘制完全相同的几何量，因此在这里记下来
        geometry.current = { padL, padR, padT, padB, plotW, plotH, min, max, count: bars.length };

        ctx.strokeStyle = color.grid; ctx.fillStyle = color.text; ctx.font = "10px sans-serif";
        for (let i = 0; i <= 4; i += 1) {
          const gridY = Math.round(y(min + (max - min) * (i / 4))) + 0.5;
          ctx.beginPath(); ctx.moveTo(padL, gridY); ctx.lineTo(width - padR, gridY); ctx.stroke();
          ctx.fillText((min + (max - min) * (i / 4)).toFixed(2), 4, gridY + 3);
        }

        const step = plotW / bars.length;
        const bodyW = Math.max(1, Math.min(step * 0.68, 9));
        bars.forEach((bar, index) => {
          const centerX = padL + step * (index + 0.5);
          const rising = bar.c >= bar.o;
          const tone = rising ? color.up : color.down;
          ctx.strokeStyle = tone; ctx.fillStyle = tone; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(centerX, y(bar.h)); ctx.lineTo(centerX, y(bar.l)); ctx.stroke();
          const top = y(Math.max(bar.o, bar.c));
          const bottom = y(Math.min(bar.o, bar.c));
          ctx.fillRect(centerX - bodyW / 2, top, bodyW, Math.max(1, bottom - top));
        });

        const last = bars[bars.length - 1];
        ctx.setLineDash([4, 3]); ctx.strokeStyle = color.line; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(padL, y(last.c)); ctx.lineTo(width - padR, y(last.c)); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = color.text;
        ctx.fillText(String(bars[0].t).slice(0, 16), padL, height - 4);
        const tail = String(last.t).slice(0, 16);
        ctx.fillText(tail, width - padR - ctx.measureText(tail).width, height - 4);

        // 悬停：画在最上层，避免被 K 线盖住
        const bar = hover === null ? null : bars[hover];
        if (bar) drawKLineHover(ctx, width, height, color, bar, hover, geometry.current, bars);
      }, [bars, hover]);

      const handleMove = (event) => {
        const shape = geometry.current;
        const canvas = event?.currentTarget;
        if (!shape || !canvas || typeof canvas.getBoundingClientRect !== "function") return;
        const rect = canvas.getBoundingClientRect();
        const next = barIndexAt(event.clientX - rect.left, shape);
        if (next !== hover) setHover(next);   // 只在跨到另一根 K 线时才重绘
      };

      return h("canvas", { ref, className: "tw-chart",
        onMouseMove: handleMove, onMouseLeave: () => setHover(null) });
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
          const esc = (value) => String(value).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
          const html = `<!DOCTYPE html><html><head><meta charset="utf-8"><title>${esc(report.ticker)} 研报</title>`
            + `<style>${STANDALONE_CSS}</style></head><body>`
            + `<header><h1>${esc(report.ticker)} · ${esc(report.rating)}</h1>`
            + `<p class="meta">发布 ${esc(report.published_at)}${report.session_id ? ` · 会话 ${esc(report.session_id)}` : ""}</p></header>`
            + `<article class="md">${blocksToHtml(parseMarkdown(report.report))}</article>`
            + `<h2>数据来源</h2><ol class="sources">${(report.sources || []).map((src) =>
              `<li><strong>${esc(src.name)}</strong><span>${esc(src.as_of)}</span><code>${esc(src.reference)}</code></li>`).join("")}</ol>`
            + `<p class="foot">由 DeepSeek Harness 交易工作台导出 · 不构成投资建议</p>`
            + `</body></html>`;
          const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
          if (typeof window !== "undefined") window.open(url, "_blank", "noopener");
        } catch (error) { /* 浏览器限制时忽略，抽屉内仍可查看 */ }
      };
      return h(React.Fragment, null,
        h("div", { className: "tw-toolbar" },
          h("button", { type: "button", className: "tw-btn", onClick: onBack }, "‹ 返回列表"),
          h("button", { type: "button", className: "tw-btn primary", onClick: openInTab }, "在新标签打开"),
          h("span", { className: `tw-tag ${RATING_CLASS[report.rating] ?? ""}` },
            labeled(report.rating, report.rating_label, "RATING")),
          h("span", { className: "tw-meta" }, `${report.ticker} · ${report.published_at}`)),
        h(Card, { title: "研报正文" }, h(Markdown, { text: report.report })),
        h(Card, { title: "数据来源", count: (report.sources || []).length,
          empty: (report.sources || []).length ? undefined : "未记录来源" },
          h(Paged, { items: report.sources || [], pageSize: 8, empty: "未记录来源",
            render: (source, index) => h(Source, { key: index, source }) })));
    }


    /**
     * 「在新标签打开」是脱离 Harness 的独立页面，拿不到主题令牌，
     * 因此自带一套克制的配色，并跟随系统深色模式。
     */
    const STANDALONE_CSS = `
:root{--fg:#1f2328;--muted:#656d76;--bg:#fff;--line:#d8dee4;--soft:#f6f8fa;--accent:#0969da}
@media (prefers-color-scheme:dark){:root{--fg:#e6edf3;--muted:#9198a1;--bg:#0d1117;--line:#30363d;--soft:#161b22;--accent:#4493f8}}
*{box-sizing:border-box}
body{margin:0;padding:48px 24px 72px;background:var(--bg);color:var(--fg);
  font:15px/1.75 system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header,article,h2,ol.sources,.foot{max-width:820px;margin-left:auto;margin-right:auto}
header h1{margin:0 0 4px;font-size:26px;letter-spacing:-.02em}
header .meta{margin:0 0 28px;color:var(--muted);font-size:13px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
  margin:44px auto 12px;padding-bottom:8px;border-bottom:1px solid var(--line)}
h1,h2,h3,h4{line-height:1.35;font-weight:650}
h1{font-size:22px;margin:30px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h2{border-bottom:none}
h3{font-size:17px;margin:26px 0 8px}
p{margin:11px 0}
ul,ol{padding-left:24px;margin:11px 0}
li{margin:5px 0}
code{font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);
  border:1px solid var(--line);border-radius:5px;padding:1px 5px}
pre{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:14px 16px;overflow:auto}
pre code{background:none;border:none;padding:0}
blockquote{margin:14px 0;padding:2px 0 2px 16px;border-left:3px solid var(--accent);color:var(--muted)}
hr{border:none;border-top:1px solid var(--line);margin:26px 0}
table{width:100%;border-collapse:collapse;margin:14px 0;font-size:14px}
th,td{border:1px solid var(--line);padding:8px 12px;text-align:left;vertical-align:top}
th{background:var(--soft);font-weight:650}
tbody tr:nth-child(even){background:color-mix(in srgb,var(--soft) 55%,transparent)}
a{color:var(--accent);text-decoration:none;border-bottom:1px solid color-mix(in srgb,currentColor 35%,transparent)}
a:hover{border-bottom-color:currentColor}
ol.sources{padding-left:20px;color:var(--muted);font-size:13.5px}
ol.sources li{margin:8px 0}
ol.sources strong{color:var(--fg);font-weight:600}
ol.sources span{margin:0 8px}
ol.sources code{font-size:11.5px}
.foot{max-width:820px;margin:48px auto 0;padding-top:16px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12px}
@media print{body{padding:0;color:#000;background:#fff}.foot{display:none}}
`;

    // ================= 中文标签 =================
    //
    // 结论性字段（信号/买卖/评级/状态）在产出侧已附 `*_label`，这里只做两件事：
    //   1) 渲染时优先用产出侧的中文标签；
    //   2) 旧记录（旧台账、旧预览、旧研报）没有标签字段，按码翻译，别让界面翻出英文。
    //
    // 表与 `plugins/datasource/python/trading_datasource/labels.py` 一致，由
    // tests/test_labels.py 比对，防止两边漂移。client 是单文件、无法 import，只能内置。
    const ZH = {
      SIGNAL: { BUY: "买入", SELL: "卖出", HOLD: "观望" },
      ACTION: { BUY: "买入", SELL: "卖出" },
      SIDE: { 1: "买入", 2: "卖出" },
      TRADE_TYPE: { buy: "买入", sell: "卖出" },
      TRADE_STATUS: { open: "持仓中", closed: "已平仓" },
      RATING: { Buy: "买入", Overweight: "增持", Hold: "持有", Underweight: "减持", Sell: "卖出" },
      SOURCE_STATUS: { ok: "正常", warn: "待配置", fail: "异常", empty: "无数据" },
      EXECUTION_TIMING: { previous_bar_next_open: "信号次一交易日开盘成交" },
      END_POSITION_POLICY: { mark_to_market_no_liquidation: "按最后收盘价盯市，不强制平仓" },
      EXECUTION_SOURCE: { local_simulation: "本地模拟（非券商成交）" },
      RISK_CONFIG: { risk_per_trade: "单笔风险占权益比", stop_atr_mult: "ATR 止损倍数",
        max_positions: "最大持仓数", max_position_pct: "单标的上限占权益比" },
      METRIC: { total_return: "累计收益", annualized: "年化收益", sharpe: "夏普比率",
        max_drawdown: "最大回撤", win_rate: "胜率" },
      GRID_AXIS: { fast: "快线", slow: "慢线", rsi_buy: "买入阈值", rsi_sell: "卖出阈值" },
      STRATEGY: { ma_cross: "双均线", rsi: "RSI" },
    };

    /** 按码翻译；大小写不敏感（历史记录里 BUY / buy / Buy 都出现过）。 */
    function zh(table, value, fallback) {
      const dict = ZH[table] ?? {};
      if (value === null || value === undefined) return fallback;
      if (Object.prototype.hasOwnProperty.call(dict, value)) return dict[value];
      if (typeof value === "string") {
        const hit = Object.keys(dict).find((key) => String(key).toLowerCase() === value.toLowerCase());
        if (hit !== undefined) return dict[hit];
      }
      return fallback === undefined ? value : fallback;
    }

    /** 优先用产出侧给的中文标签，没有才按码翻译。 */
    function labeled(value, label, table, fallback) {
      if (typeof label === "string" && label.trim()) return label;
      return zh(table, value, fallback);
    }

    // ================= Markdown 渲染 =================
    //
    // 研报正文是 Markdown。此前直接塞进 <pre>：标题、列表、表格全成了源码，
    // 一份结构化研报读起来是一堵墙。
    //
    // 不引第三方库（客户端没有打包器、也没有依赖），因此自己解析；
    // 但**渲染成 React 元素而不是拼 HTML 字符串**：研报正文含用户/模型产出，
    // 拼字符串再 innerHTML 等于开一个注入口子。
    //
    // 支持：标题、段落、有序/无序列表（含嵌套）、围栏代码块、引用、分隔线、
    //       表格、`粗体` *斜体* `行内代码` [链接](url)。
    // 不支持：原始 HTML（按字面转义）、图片（研报不放图）。

    /** 行内解析：文本 → 行内节点数组。 */
    function parseInline(text) {
      const nodes = [];
      const push = (node) => nodes.push(node);
      let buffer = "";
      const flush = () => { if (buffer) { push({ type: "text", value: buffer }); buffer = ""; } };
      // 依次匹配：代码、链接、粗体、斜体。代码优先，避免 `**a**` 被当成粗体。
      const pattern = /(`[^`]+`)|(\[[^\]]*\]\([^)\s]+\))|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\n]+\*)|(_[^_\n]+_)/;
      let rest = String(text ?? "");
      while (rest.length > 0) {
        const match = pattern.exec(rest);
        if (!match) { buffer += rest; break; }
        buffer += rest.slice(0, match.index);
        flush();
        const token = match[0];
        if (token.startsWith("`")) {
          push({ type: "code", value: token.slice(1, -1) });
        } else if (token.startsWith("[")) {
          const split = token.indexOf("](");
          const href = token.slice(split + 2, -1);
          // 只允许安全协议，挡掉 javascript: 之类
          const safe = /^(https?:|mailto:|\/|#)/i.test(href) ? href : null;
          if (safe) push({ type: "link", href: safe, children: parseInline(token.slice(1, split)) });
          else push({ type: "text", value: token });
        } else if (token.startsWith("**") || token.startsWith("__")) {
          push({ type: "strong", children: parseInline(token.slice(2, -2)) });
        } else {
          push({ type: "em", children: parseInline(token.slice(1, -1)) });
        }
        rest = rest.slice(match.index + token.length);
      }
      flush();
      return nodes;
    }

    /** 表格分隔行：| --- | :--: | */
    const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
    const splitRow = (line) => line.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((cell) => cell.trim());

    const LIST_ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

    /**
     * 解析一个列表（含嵌套）。缩进更深的项作为上一项的子列表，
     * 而不是拍平成同级——拍平会让"关键变量是 eCPM 而非库存"这种
     * 补充说明看起来和主结论平级，改变语义。
     */
    function parseList(lines, start, baseIndent) {
      const items = [];
      let index = start;
      const ordered = /\d/.test(LIST_ITEM.exec(lines[start])[2]);
      while (index < lines.length) {
        const item = LIST_ITEM.exec(lines[index]);
        if (!item || item[1].length !== baseIndent) break;
        const parts = [item[3]];
        index += 1;
        // 续行：比本项缩进更深、且不是新的列表项
        while (index < lines.length && lines[index].trim()
               && !LIST_ITEM.test(lines[index])
               && lines[index].length - lines[index].trimStart().length > baseIndent) {
          parts.push(lines[index].trim()); index += 1;
        }
        const node = { children: parseInline(parts.join(" ")) };
        const nested = index < lines.length ? LIST_ITEM.exec(lines[index]) : null;
        if (nested && nested[1].length > baseIndent) {
          const sub = parseList(lines, index, nested[1].length);
          node.sub = sub.list;
          index = sub.next;
        }
        items.push(node);
      }
      return { list: { type: "list", ordered, items }, next: index };
    }

    /**
     * 块级解析：Markdown → 块节点数组。
     * 纯函数、不碰 React，便于离线断言结构。
     */
    function parseMarkdown(text) {
      const lines = String(text ?? "").replace(/\r\n?/g, "\n").split("\n");
      const blocks = [];
      let index = 0;
      while (index < lines.length) {
        const line = lines[index];
        if (!line.trim()) { index += 1; continue; }
        // 围栏代码块
        const fence = /^\s*(```|~~~)\s*(\S*)\s*$/.exec(line);
        if (fence) {
          const marker = fence[1][0].repeat(3);
          const body = [];
          index += 1;
          while (index < lines.length && !new RegExp(`^\\s*${marker}`).test(lines[index])) {
            body.push(lines[index]); index += 1;
          }
          index += 1;   // 跳过收尾围栏
          blocks.push({ type: "code", lang: fence[2] || "", text: body.join("\n") });
          continue;
        }
        // 标题
        const heading = /^(#{1,6})\s+(.*)$/.exec(line);
        if (heading) {
          blocks.push({ type: "heading", level: heading[1].length, children: parseInline(heading[2].trim()) });
          index += 1; continue;
        }
        // 分隔线
        if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { blocks.push({ type: "hr" }); index += 1; continue; }
        // 表格：表头 + 分隔行
        if (line.includes("|") && index + 1 < lines.length && TABLE_DIVIDER.test(lines[index + 1])) {
          const header = splitRow(line).map(parseInline);
          const rows = [];
          index += 2;
          while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
            rows.push(splitRow(lines[index]).map(parseInline)); index += 1;
          }
          blocks.push({ type: "table", header, rows });
          continue;
        }
        // 引用（可多行）
        if (/^\s*>\s?/.test(line)) {
          const body = [];
          while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
            body.push(lines[index].replace(/^\s*>\s?/, "")); index += 1;
          }
          blocks.push({ type: "quote", children: parseMarkdown(body.join("\n")) });
          continue;
        }
        // 列表：按缩进分层（子项挂到上一项的 sub 上，拍平会丢层次）
        const bullet = LIST_ITEM.exec(line);
        if (bullet) {
          const parsed = parseList(lines, index, bullet[1].length);
          blocks.push(parsed.list);
          index = parsed.next;
          continue;
        }
        // 段落：连续非空行合并（Markdown 的软换行按空格处理）
        const paragraph = [line.trim()];
        index += 1;
        while (index < lines.length && lines[index].trim()
               && !/^(#{1,6})\s|^\s*(```|~~~)|^\s*>|^\s*([-*+]|\d+[.)])\s/.test(lines[index])
               && !(lines[index].includes("|") && index + 1 < lines.length && TABLE_DIVIDER.test(lines[index + 1]))) {
          paragraph.push(lines[index].trim()); index += 1;
        }
        blocks.push({ type: "paragraph", children: parseInline(paragraph.join(" ")) });
      }
      return blocks;
    }

    /** 行内节点 → React 元素。 */
    function renderInline(nodes, keyPrefix) {
      return (nodes ?? []).map((node, i) => {
        const key = `${keyPrefix}-${i}`;
        switch (node.type) {
          case "strong": return h("strong", { key }, renderInline(node.children, key));
          case "em": return h("em", { key }, renderInline(node.children, key));
          case "code": return h("code", { key, className: "tw-md-code" }, node.value);
          case "link": return h("a", { key, className: "tw-md-link", href: node.href,
            target: "_blank", rel: "noopener noreferrer" }, renderInline(node.children, key));
          default: return h(React.Fragment, { key }, node.value);
        }
      });
    }

    /** 块节点 → React 元素。 */
    function renderBlocks(blocks, keyPrefix = "md") {
      return (blocks ?? []).map((block, i) => {
        const key = `${keyPrefix}-${i}`;
        switch (block.type) {
          case "heading": {
            const level = Math.min(Math.max(block.level, 1), 6);
            return h(`h${level}`, { key, className: `tw-md-h tw-md-h${level}` },
              renderInline(block.children, key));
          }
          case "code":
            return h("pre", { key, className: "tw-md-pre" },
              block.lang ? h("span", { className: "tw-md-lang" }, block.lang) : null,
              h("code", null, block.text));
          case "quote":
            return h("blockquote", { key, className: "tw-md-quote" },
              renderBlocks(block.children, key));
          case "hr":
            return h("hr", { key, className: "tw-md-hr" });
          case "list": {
            const tag = block.ordered ? "ol" : "ul";
            return h(tag, { key, className: "tw-md-list" },
              block.items.map((item, j) => h("li", { key: `${key}-${j}` },
                renderInline(item.children, `${key}-${j}`),
                item.sub ? renderBlocks([item.sub], `${key}-${j}s`) : null)));
          }
          case "table": {
            return h("div", { key, className: "tw-md-table-wrap" },
              h("table", { className: "tw-md-table" },
                h("thead", null, h("tr", null, block.header.map((cell, j) =>
                  h("th", { key: `${key}-h${j}` }, renderInline(cell, `${key}-h${j}`))))),
                h("tbody", null, block.rows.map((row, r) =>
                  h("tr", { key: `${key}-r${r}` }, row.map((cell, c) =>
                    h("td", { key: `${key}-r${r}c${c}` }, renderInline(cell, `${key}-r${r}c${c}`))))))));
          }
          default:
            return h("p", { key, className: "tw-md-p" }, renderInline(block.children, key));
        }
      });
    }

    /** 块节点 → HTML 字符串（仅用于「在新标签打开」的独立文档）。 */
    function blocksToHtml(blocks) {
      const esc = (value) => String(value).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
      const inline = (nodes) => (nodes ?? []).map((node) => {
        switch (node.type) {
          case "strong": return `<strong>${inline(node.children)}</strong>`;
          case "em": return `<em>${inline(node.children)}</em>`;
          case "code": return `<code>${esc(node.value)}</code>`;
          case "link": return `<a href="${esc(node.href)}" target="_blank" rel="noopener noreferrer">${inline(node.children)}</a>`;
          default: return esc(node.value);
        }
      }).join("");
      return (blocks ?? []).map((block) => {
        switch (block.type) {
          case "heading": return `<h${block.level}>${inline(block.children)}</h${block.level}>`;
          case "code": return `<pre><code>${esc(block.text)}</code></pre>`;
          case "quote": return `<blockquote>${blocksToHtml(block.children)}</blockquote>`;
          case "hr": return "<hr>";
          case "list": {
            const tag = block.ordered ? "ol" : "ul";
            return `<${tag}>${block.items.map((item) => `<li>${inline(item.children)}`
              + (item.sub ? blocksToHtml([item.sub]) : "") + "</li>").join("")}</${tag}>`;
          }
          case "table":
            return `<table><thead><tr>${block.header.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>`
              + `<tbody>${block.rows.map((row) => `<tr>${row.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
          default: return `<p>${inline(block.children)}</p>`;
        }
      }).join("\n");
    }

    /** 研报正文：解析一次，抽屉里渲染 React，新标签页渲染 HTML。 */
    function Markdown({ text }) {
      const blocks = React.useMemo(() => parseMarkdown(text), [text]);
      if (!String(text ?? "").trim()) return h("p", { className: "tw-empty" }, "本篇研报没有正文");
      return h("div", { className: "tw-md" }, renderBlocks(blocks));
    }

    /**
     * 计算 Card 的 empty：**只有"确实没有内容"时才返回文案**。
     *
     * 不要写成 `loading ? "加载中…" : (error || "兜底文案")`——成功时 error 是空串，
     * `"" || "兜底文案"` 得到真值，于是 Card 会用兜底文案**替换掉已经取到的内容**：
     * 徽标显示有 N 条，正文却是"不可用/暂无数据"。这类 bug 会让用户以为没取到数据。
     */
    function cardEmpty({ loading, error, count, fallback }) {
      if (loading) return "读取中…";
      if (error) return error;
      return count > 0 ? undefined : fallback;
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

    /** K 线卡片：周期切换 + 蜡烛图。按需取数，两层缓存（客户端 TTL + Host TTL）。 */
    function KLineCard({ rpc, ticker }) {
      const [period, setPeriod] = React.useState(KLINE_DEFAULT_PERIOD);
      const preset = KLINE_PERIODS.find((row) => row.id === period) ?? KLINE_PERIODS[0];
      const series = useEndpoint(rpc, "series",
        { ticker, period: preset.id, limit: preset.limit }, [rpc, ticker, preset.id]);
      const bars = series.data?.bars ?? [];
      const span = period === "1d" ? "约 1 年" : period === "5m" ? "约 20 小时" : "一个交易日上下";
      return h(Card, { title: `K 线（${preset.label}）`, count: bars.length },
        h("div", { className: "tw-toolbar" }, KLINE_PERIODS.map((row) =>
          h("button", { key: row.id, type: "button",
            className: `tw-btn${row.id === period ? " primary" : " seg"}`,
            onClick: () => setPeriod(row.id) }, row.label)),
          h("span", { className: "tw-meta" }, `${preset.limit} 根 · ${span}`)),
        h(KLineChart, { bars }),
        h("p", { className: "tw-meta" },
          series.loading ? "取数中…"
            : series.error ? `取数失败：${series.error}`
              : `来源 ${series.data?.source ?? "—"} · 截至 ${series.data?.as_of ?? "—"}`
                + ` · ${bars.length} 根`
                + (series.data?.stale ? " ⚠ 数据源不可用，展示本地缓存" : "")),
        h("p", { className: "tw-hint" },
          "K 线按周期缓存（客户端与 Host 各 5 分钟），切换页签不会重复取数。"
          + "更细的逐笔/分时请在富途查看。"));
    }

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
          h("p", { className: "tw-hint" }, "点击上方链接可在富途查看更细的分时与逐笔。")),
        submitted && h(KLineCard, { rpc, ticker: submitted }));;
    }

    /**
     * 一条预览的中文结论块。
     *
     * 此前这里只有一段原始 JSON：结论（信号、收益率、回撤）都得自己去 JSON 里找，
     * 而且看到的是 `HOLD`、`local_simulation` 这类机器码。结论用中文列在上方，
     * JSON 仍然保留在下方作为可核对的原始证据。
     */
    function PreviewSummary({ preview }) {
      const v = preview.value ?? {};
      const kv = (pairs) => h("div", { className: "tw-kv" }, pairs
        .filter(([, value]) => value !== undefined && value !== null && value !== "")
        .map(([label, value]) => h(RiskMetricItem, { key: label, label, value: String(value) })));
      const pct = (x, digits = 2) => (Number.isFinite(x) ? `${(x * 100).toFixed(digits)}%` : "—");
      if (preview.kind === "signal") {
        return kv([
          ["结论", labeled(v.signal, v.signal_label, "SIGNAL")],
          ["标的", v.ticker], ["策略", v.strategy_label ?? v.strategy],
          ["信号日期", v.date], ["价格", v.price], ["ATR（14 日）", Number.isFinite(v.atr) ? v.atr.toFixed(3) : "—"],
          ["口径", v.execution_source_label ?? zh("EXECUTION_SOURCE", v.execution_source)],
        ]);
      }
      if (preview.kind === "backtest") {
        const m = v.summary ?? {};
        return kv([
          ["标的", v.ticker], ["策略", v.strategy],
          ["行情区间", v.data_start && v.data_end ? `${v.data_start} → ${v.data_end}` : "—"],
          ["数据源", v.source],
          ["累计收益", pct(m.total_return)], ["年化", pct(m.annualized)],
          ["夏普", Number.isFinite(m.sharpe) ? m.sharpe : "—"],
          ["最大回撤", pct(m.max_drawdown)],
          ["交易笔数", m.trades], ["胜率", pct(m.win_rate, 1)],
          ["成交口径", v.execution_timing_label ?? zh("EXECUTION_TIMING", v.execution_timing)],
          ["末仓处理", v.end_position_policy_label ?? zh("END_POSITION_POLICY", v.end_position_policy)],
          ["持仓中", (v.open_positions ?? []).length ? `${(v.open_positions ?? []).length} 笔` : "无"],
        ]);
      }
      if (preview.kind === "ledger") {
        return kv([
          ["账户模式", v.mode === "live" ? "实盘" : "模拟盘"],
          ["状态", v.status_label ?? (v.status === "available" ? "可用" : v.status)],
          ["可用现金", v.cash], ["账户权益", v.equity], ["累计收益", pct(v.total_return)],
          ["持仓数", v.positions ? Object.keys(v.positions).length : "—"],
          ["已平仓笔数", v.trades], ["胜率", pct(v.win_rate, 1)],
          ["口径", v.execution_source_label ?? zh("EXECUTION_SOURCE", v.execution_source)],
        ]);
      }
      return null;
    }

    function SignalView({ snapshot }) {
      const previews = snapshot.previews.filter((p) => p.kind === "signal" || p.kind === "backtest");
      return h(React.Fragment, null,
        h(Card, { title: "量化信号与回测（由 Harness 计算）", count: previews.length,
          // 必须是条件式：常量字符串恒为真，会让下面的 Paged 列表永远不渲染
          empty: previews.length ? undefined
            : "暂无信号。请在 Harness 会话中请求（如“看下 600519 的信号”），结果显示在这里。" },
          h(Paged, { items: previews, pageSize: 8, empty: "暂无信号预览",
            render: (p) => h("details", { key: p.id, className: "tw-item" },
              h("summary", null,
                h("span", { className: `tw-tag ${String(p.value?.signal ?? "").toUpperCase() === "BUY" ? "buy" : String(p.value?.signal ?? "").toUpperCase() === "SELL" ? "sell" : "hold"}` },
                  labeled(p.value?.signal, p.value?.signal_label, "SIGNAL", p.kind)),
                // 策略层就必须看得见「哪个标的、哪套策略」，否则条目无法归因
                [p.value?.ticker, p.value?.strategy_label ?? p.value?.strategy]
                  .filter(Boolean).join(" · "),
                h("span", { className: "tw-meta" }, p.at)),
              h("div", { className: "tw-item-body" },
                h(PreviewSummary, { preview: p }),
                h("p", { className: "tw-meta" }, "以下为原始返回（可核对，未做翻译）："),
                h("pre", { className: "tw-pre" }, JSON.stringify(p.value, null, 2)))) })),
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
    const EQUITY_MARK_NOTE = "从首次取数当日起累积，不回溯伪造历史——"
      + "用当前持仓反推过去会得到一个从未真实存在过的数字。不跨账户合计（币种不同）。";

    /** 一行每日盯市：列出各账户总资产，不做跨账户合计。 */
    function EquityMarkRow({ mark }) {
      const accounts = mark.accounts ?? [];
      return h("div", { className: "tw-kv-item" },
        h("div", { className: "tw-kv-k" }, `${mark.date} · ${mark.positions ?? "—"} 笔持仓`),
        h("div", { className: "tw-kv-v" }, accounts.length
          ? accounts.map((row) => `${row.account} 总资产 ${row.total_asset ?? "—"}`).join(" · ")
          : "该日无持仓记录"));
    }

    function PortfolioView({ rpc, snapshot }) {
      const ledgerPreview = snapshot.previews.find((p) => p.kind === "ledger");
      // 台账预览此前只显示原始 JSON，结论先给中文
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
          empty: cardEmpty({ loading: broker.loading, error: broker.error,
            count: counts.positions ?? 0, fallback: "当前账户无持仓" }) },
          h("p", { className: "tw-meta" },
            `${snapshot.mode === "live" ? "实盘" : "模拟盘"} · 数据源 ${broker.data?.source ?? "—"}`
            + ` · 取数于 ${broker.data?.as_of ?? "—"}`
            + (broker.data?.cached ? "（本地缓存）" : "")
            + (broker.data?.stale ? " ⚠ 实时读取失败，展示上次缓存" : "")
            + ` · 检查 ${counts.accounts_checked ?? "—"} 个账户，`
            + `${counts.accounts_with_positions ?? 0} 个有持仓`),
          (broker.data?.errors ?? []).length > 0 && h("p", { className: "tw-alert" },
            `部分账户读取失败：${broker.data.errors.map((e) => `${e.account}(${e.reason})`).join("；")}`),
          h("p", { className: "tw-meta" }, broker.data?.note ?? ""),
        ),
        ...groups.map(accountCard),

        h(Card, { title: "账户权益（每日盯市）", count: (broker.data?.equity_marks ?? []).length,
          empty: cardEmpty({ loading: broker.loading, error: broker.error,
            count: (broker.data?.equity_marks ?? []).length,
            fallback: "尚无盯市记录（首次取数当天开始累积）" }) },
          h("p", { className: "tw-meta" }, EQUITY_MARK_NOTE),
          h(Paged, { items: [...(broker.data?.equity_marks ?? [])].reverse(), pageSize: 6, empty: "",
            render: (mark) => h(EquityMarkRow, { key: mark.date, mark }) })),

        h(Card, { title: "本地策略台账（不是券商资产）", count: points.length,
          empty: points.length > 1 ? undefined : (equity.loading ? "加载中…" : "暂无本地策略成交记录") },
          h("p", { className: "tw-meta" },
            "来源：本地模拟台账 ~/.dsh/quant-ledger.json，用于回放 quant_signal/quant_backtest 的策略表现；"
            + "与上面的券商持仓是两套账，不要相加。"
            + (ledgerPreview ? ` · 最近台账预览 ${ledgerPreview.at}` : "")),
          // 台账预览的结论先用中文列出，原始 JSON 仍可在「信号」页展开核对
          ledgerPreview && h(PreviewSummary, { preview: ledgerPreview }),
          h(LineChart, { points: points.map((p) => ({ v: p.equity })), label: equity.data?.note || "" }),
          equity.data && h("p", { className: "tw-meta" },
            `区间 ${points[0]?.t} → ${points[points.length - 1]?.t}`
            + ` · 最大回撤 ${percent(equity.data.max_drawdown)}`
            + ` · 夏普 ${numeric(equity.data.sharpe)}`)));
    }

    function RiskConfigCard({ rpc, mode }) {
      const risk = useEndpoint(rpc, "risk", {}, [rpc]);
      const config = risk.data?.config;
      return h(Card, { title: "风控参数（引擎实际生效值）",
        empty: config ? undefined : cardEmpty({ loading: risk.loading, error: risk.error,
          count: 0, fallback: "不可用" }) },
        config && h("div", { className: "tw-kv" }, Object.entries(config).map(([key, value]) =>
          h("div", { key, className: "tw-kv-item" },
            // 中文名在前，原键名附在后面，便于对照 ~/.dsh/trading-risk.json
            h("div", { className: "tw-kv-k" }, `${zh("RISK_CONFIG", key)}（${key}）`),
            h("div", { className: "tw-kv-v" }, String(value))))),
        h("p", { className: "tw-meta" }, `来源：${risk.data?.source ?? "—"}`),
        h("p", { className: "tw-hint" }, "参数由 scripts/risk_config 管理；引擎每次决策前读取，非法配置直接拒绝交易。"));
    }

    /**
     * 持仓风险：按账户分组展示集中度与浮盈亏。
     *
     * **不跨账户合计**——不同账户可能不同币种，相加没有意义。
     * 占比分母有两个口径，界面上一并写清，避免误读：
     *   share_of_positions = 本账户持仓市值；share_of_assets = 本账户总资产（含现金）。
     */
    function AccountRiskRow({ group }) {
      const risk = group.risk ?? {};
      const top = risk.top ?? [];
      return h("div", { className: "tw-kv-item" },
        h("div", { className: "tw-kv-k" },
          `${group.account} · ${risk.positions ?? 0} 笔 · 持仓市值 ${tradeMoney(risk.market_value)}`),
        h("div", { className: "tw-kv-v" },
          `最大集中度 ${risk.max_share_symbol ?? "—"} ${percentValue(risk.max_share_of_positions)}`),
        h("div", { className: "tw-meta" },
          `浮盈 ${risk.winners?.count ?? 0} 笔 ${tradeMoney(risk.winners?.pl_val)}`
          + ` · 浮亏 ${risk.losers?.count ?? 0} 笔 ${tradeMoney(risk.losers?.pl_val)}`),
        h("div", { className: "tw-meta" },
          top.map((row) => `${row.symbol} ${percentValue(row.share_of_positions, 1)}`
            + `（占总资产 ${numeric(row.share_of_assets, 1)}%）`).join(" · ")
          || "无可用市值数据"));
    }

    function PortfolioRiskCard({ positions }) {
      const groups = (positions.data?.groups ?? []).filter((group) => (group.risk?.positions ?? 0) > 0);
      return h(Card, { title: "持仓风险（按账户，不跨账户合计）", count: groups.length,
        empty: cardEmpty({ loading: positions.loading, error: positions.error,
          count: groups.length, fallback: "当前账户无持仓" }) },
        h("p", { className: "tw-meta" },
          "占比分母：括号外为**本账户持仓市值**，括号内为**本账户总资产**（含现金）。"
          + "不同账户的币种可能不同，因此不做跨账户合计。"),
        h(Paged, { items: groups, pageSize: 3, empty: "当前账户无持仓",
          render: (group) => h(AccountRiskRow, { key: group.acc_id, group }) }));
    }

    /** 风险指标的一格：标签 + 数值。抽成组件是因为内联写法的括号极易数错。 */
    function RiskMetricItem({ label, value }) {
      return h("div", { className: "tw-kv-item" },
        h("div", { className: "tw-kv-k" }, label),
        h("div", { className: "tw-kv-v" }, String(value)));
    }

    function RiskView({ rpc, snapshot }) {
      const backtests = snapshot.previews.filter((p) => p.kind === "backtest");
      const latest = backtests[0]; // 预览按新→旧存储，第一条就是最近一次
      const bt = latest?.value;
      const s = bt?.summary;
      const equity = useEndpoint(rpc, "equity", { mode: snapshot.mode, window: 250 }, [rpc, snapshot.mode]);
      const positions = useEndpoint(rpc, "positions", { mode: snapshot.mode }, [rpc, snapshot.mode]);
      // positions.py 的根字段是 groups（按账户分组），**没有**根级 positions；
      // 曾误读 positions.data.positions，导致 held 恒为空、相关性矩阵永远算不出来。
      // 明细里的字段是 symbol（早期本地台账是 ticker），两种都认。
      const held = [...new Set((positions.data?.groups ?? [])
        .flatMap((group) => group.positions ?? [])
        .map((p) => p.symbol ?? p.ticker).filter(Boolean))].slice(0, 6);
      const canCorrelate = held.length >= 2;
      const correlation = useEndpoint(rpc, "correlation", { tickers: held, window: 120 }, [rpc, held.join(",")]);
      const ddPoints = (equity.data?.points ?? []).map((p) => ({ v: (p.dd ?? 0) * 100 }));

      // 指标必须点名主语：同一个「最大回撤」究竟是台账回放还是某次回测、
      // 覆盖哪些标的与策略，不写清楚就只是一串没有归属的数字。
      const joinParts = (parts) => parts.filter(Boolean).join(" · ");
      const replaySubject = joinParts([(equity.data?.tickers ?? []).join("、"),
        (equity.data?.strategies ?? []).join("、")]);
      const btSubject = joinParts([bt?.ticker, bt?.strategy]);
      const label = (base, subject) => (subject ? `${base}（${subject}）` : base);
      const btDetail = joinParts([btSubject,
        Number.isFinite(bt?.bars) ? `${bt.bars} 根日线` : "",
        // 实际来源而不是请求值：auto 等于没写，用户无从判断复权口径
        bt?.source ? `数据源 ${bt.source}` : "",
        bt?.data_start && bt?.data_end ? `区间 ${bt.data_start} → ${bt.data_end}` : "",
        latest?.at ? String(latest.at).slice(0, 16).replace("T", " ") : ""]);

      return h(React.Fragment, null,
        h(Card, { title: "风险指标",
          // 注意：equity.data 在台账为空时**仍然是个对象**（只有 points/note），
          // 用对象是否存在判断"有没有风险数据"会误判成有。
          empty: cardEmpty({
            loading: equity.loading && !s,
            error: s ? "" : equity.error,
            count: (Number.isFinite(equity.data?.max_drawdown) ? 1 : 0)
              + (Number.isFinite(s?.max_drawdown) ? 1 : 0),
            fallback: "暂无策略层风险数据：本地台账还没有成交记录，也没有回测预览",
          }) },
          h("p", { className: "tw-meta" },
            `台账回放：${replaySubject || "无标的"}`
            + `　｜　最近回测：${btDetail || "尚无回测预览"}`
            + (backtests.length > 1 ? `（共 ${backtests.length} 次回测记录，此处为最新一次）` : "")),
          bt?.coverage_note && bt.requested_start && bt.data_start > bt.requested_start
            && h("p", { className: "tw-alert" }, bt.coverage_note),
          h("div", { className: "tw-kv" },
            h(RiskMetricItem, { label: label("最大回撤 · 台账回放", replaySubject),
              value: percent(equity.data?.max_drawdown) }),
            h(RiskMetricItem, { label: label("夏普 · 台账回放", replaySubject),
              value: numeric(equity.data?.sharpe) }),
            h(RiskMetricItem, { label: label("最大回撤 · 回测", btSubject),
              value: percent(s?.max_drawdown) }),
            h(RiskMetricItem, { label: label("夏普 / 年化 · 回测", btSubject),
              value: `${numeric(s?.sharpe)} / ${percent(s?.annualized, 1)}` }))),
        h(Card, { title: "回撤曲线（水下图，%）",
          empty: ddPoints.length > 1 ? undefined : "需要成交记录才能回放回撤" },
          h(LineChart, { points: ddPoints, label: "drawdown %" })),
        h(PortfolioRiskCard, { positions }),
        h(Card, { title: "相关性矩阵（日收益，120 日）",
          empty: !canCorrelate
            ? `当前持仓只有 ${held.length} 个可识别标的，需要至少 2 个；`
              + "相关性矩阵由持仓自动带出，无需手工输入。"
            : cardEmpty({ loading: correlation.loading, error: correlation.error,
              count: correlation.data?.tickers?.length ?? 0, fallback: "标的不足" }) },
          correlation.data && h(HeatmapChart, { tickers: correlation.data.tickers, matrix: correlation.data.matrix }),
          correlation.data && h("p", { className: "tw-meta" }, `窗口 ${correlation.data.window} 个共同交易日 · 截至 ${correlation.data.as_of}`)),
        h(RiskConfigCard, { rpc, mode: snapshot.mode }),
        h("p", { className: "tw-hint" },
          "**口径说明**：持仓风险与相关性矩阵基于券商真实持仓；"
          + "「风险指标」里的回撤/夏普来自本地策略台账与回测预览——那是**策略层**的指标，"
          + "不是这个账户的实际表现。两者不可混看。"));
    }

    /** IC 检验当前只覆盖价量因子（估值因子需历史估值序列，后续接入）。 */
    const IC_FACTORS = { mom_20: 1, mom_60: 1, vol_20: 1, trend: 1, rsi_14: 1, liq_ratio: 1, mdd_60: 1 };

    const FACTOR_LABELS = {
      mom_20: "动量20", mom_60: "动量60", vol_20: "波动率", trend: "趋势偏离",
      rsi_14: "RSI14", liq_ratio: "量能比", mdd_60: "最大回撤",
      pe_ttm: "PE(TTM)", pb: "PB", peg: "PEG", ps: "PS",
    };

    /** 质量因子（财报原文计算）。ROE/ROA 不可得时如实说明，不估算。 */
    function QualityCard({ rpc, ticker }) {
      const quality = useEndpoint(rpc, "quality", { ticker }, [rpc, ticker]);
      const latest = quality.data?.latest;
      const periods = quality.data?.periods ?? [];
      const pct = (value) => (value === null || value === undefined ? "—" : `${value}%`);
      const returns = quality.data?.returns ?? {};
      const metrics = latest ? [
        ["净资产收益率 ROE", pct(returns.roe)],
        ["总资产收益率 ROA", pct(returns.roa)],
        ["毛利率", pct(latest.gross_margin)],
        ["营业利润率", pct(latest.operating_margin)],
        ["净利率", pct(latest.net_margin)],
        ["研发占比", pct(latest.rd_ratio)],
        ["实际税率", pct(latest.effective_tax_rate)],
        ["稀释 EPS", latest.diluted_eps ?? "—"],
        ["每股股息", latest.dividend_per_share ?? "—"],
        ["营收同比", latest.revenue_yoy === null || latest.revenue_yoy === undefined
          ? "—" : `${latest.revenue_yoy.toFixed(2)}%`],
        ["净利同比", latest.net_profit_yoy === null || latest.net_profit_yoy === undefined
          ? "—" : `${latest.net_profit_yoy.toFixed(2)}%`],
      ] : [];
      return h(Card, { title: `质量因子（${ticker}）`, count: periods.length,
        empty: cardEmpty({ loading: quality.loading, error: quality.error,
          count: periods.length, fallback: "富途未返回财报数据" }) },
        latest && h("p", { className: "tw-meta" },
          `最新期间 ${latest.period_end ?? "—"} · ${latest.fiscal_year ?? "—"} · `
          + `type=${latest.financial_type ?? "—"} · 币种 ${quality.data?.currency ?? "—"} · `
          + `${periods.length} 个期间`),
        latest && h("div", { className: "tw-kv" }, metrics.map(([label, value]) =>
          h("div", { key: label, className: "tw-kv-item" },
            h("div", { className: "tw-kv-k" }, label),
            h("div", { className: "tw-kv-v" }, String(value))))),
        returns.available && h("p", { className: "tw-meta" },
          `ROE/ROA 来源 ${returns.source} · 权益期 ${returns.balance_period ?? "—"}`
          + ` · 利润期 ${returns.income_period ?? "—"}（富途无资产负债表接口，按渠道优先级落到备用源）`),
        returns.available && h("p", { className: "tw-hint" }, returns.note ?? ""),
        (quality.data?.unavailable ?? []).length > 0 && h("p", { className: "tw-hint" },
          "无法提供：" + quality.data.unavailable.map((row) => `${row.label}（${row.reason}）`).join("；")),
        h("p", { className: "tw-hint" }, quality.data?.note ?? ""));
    }

    /** 因子表格的一行：一个标的的排名、综合分与各因子取值。 */
    function FactorRow({ row }) {
      const tone = (row.score ?? 0) >= 0
        ? "var(--dsw-alias-state-success-primary,#2ea043)"
        : "var(--dsw-alias-state-error-primary,#d1242f)";
      const detail = Object.entries(FACTOR_LABELS).map(([key, label]) => {
        const value = row.factors?.[key];
        return `${label} ${value === undefined || value === null ? "—" : Number(value).toFixed(3)}`;
      }).join(" · ");
      return h("div", { className: "tw-kv-item" },
        h("div", { className: "tw-kv-k" }, `#${row.rank} ${row.ticker}`),
        h("div", { className: "tw-kv-v", style: { color: tone } },
          `${row.score >= 0 ? "+" : ""}${row.score}`),
        h("div", { className: "tw-meta" }, detail));
    }

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
          empty: !enough ? "有效标的不足：请在下方输入至少 2 个标的（如 00700.HK,AAPL）"
            : cardEmpty({ loading: snap.loading, error: snap.error,
              count: rows.length, fallback: "暂无数据" }) },
          h(Paged, { items: rows, pageSize: 4, empty: "暂无因子数据",
            render: (row) => h(FactorRow, { key: row.ticker, row }) }),          snap.data?.failures && Object.keys(snap.data.failures).length > 0
            && h("p", { className: "tw-meta" }, `跳过：${Object.entries(snap.data.failures).map(([k, v]) => `${k}(${v})`).join("；")}`)),
        h(Card, { title: "因子 IC / ICIR（横截面，forward 5 日）",
          empty: tickers.length < 3 ? "IC 需要 3..8 个标的（横截面相关）"
            : cardEmpty({ loading: ic.loading, error: ic.error,
              count: icPoints.length, fallback: "样本不足" }) },
          h("div", { className: "tw-toolbar" }, Object.keys(IC_FACTORS).map((k) =>
            h("button", { key: k, type: "button", className: `tw-btn seg${factor === k ? " active" : ""}`,
              onClick: () => setFactor(k) }, FACTOR_LABELS[k] ?? k))),
          ic.data && h("div", { className: "tw-kv" },
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "均值 IC"), h("div", { className: "tw-kv-v" }, String(ic.data.mean_ic))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "IC 标准差"), h("div", { className: "tw-kv-v" }, String(ic.data.ic_std))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "ICIR"), h("div", { className: "tw-kv-v" }, String(ic.data.icir))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "正 IC 占比"), h("div", { className: "tw-kv-v" }, String(ic.data.positive_ratio))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "样本期数"), h("div", { className: "tw-kv-v" }, numeric(ic.data.count, 0)))),
          icPoints.length > 1 && h(LineChart, { points: icPoints, label: "IC 序列" }),
          ic.data && h("p", { className: "tw-hint" }, ic.data.note)),
        ticker && h(QualityCard, { rpc, ticker }));
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
          `成交金额 ${tradeMoney(row.amount)} · 订单号 ${row.order_id}`
          + ` · 券商原始状态码 ${row.status_code ?? "—"}（未公开枚举含义，故原样保留）`));
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
          empty: cardEmpty({ loading: trades.loading, error: trades.error,
            count: rows.length, fallback: "暂无成交记录" }) },
          h(Paged, { items: rows, pageSize: 10,
            empty: cardEmpty({ loading: trades.loading, error: trades.error,
              count: rows.length, fallback: "暂无成交记录" }),
            render: (row, i) => h("div", { key: i, className: "tw-kv-item" },
              h("div", { className: "tw-kv-k" }, `${row.date} · ${row.ticker}`),
              h("div", { className: "tw-kv-v", style: { color: String(row.action).toUpperCase() === "BUY" ? "var(--dsw-alias-state-success-primary,#2ea043)" : "var(--dsw-alias-state-error-primary,#d1242f)" } },
                `${labeled(row.action, row.action_label, "ACTION")} ${row.shares} @ ${row.price}`),
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
                  `📄 ${report.ticker} · ${labeled(report.rating, report.rating_label, "RATING")} · ${report.published_at}`),
                h("div", { className: "tw-meta" }, (report.report || "").replace(/\s+/g, " ").slice(0, 120) + "…"))) })),
        h(Card, { title: "参数敏感性（样本内网格）" },
          h("div", { className: "tw-toolbar" },
            h("span", { className: "tw-meta" }, `标的 ${ticker}`),
            h("button", { type: "button", className: `tw-btn seg${strategy === "ma_cross" ? " active" : ""}`,
              onClick: () => setStrategy("ma_cross") }, zh("STRATEGY", "ma_cross")),
            h("button", { type: "button", className: `tw-btn seg${strategy === "rsi" ? " active" : ""}`,
              onClick: () => setStrategy("rsi") }, zh("STRATEGY", "rsi")),
            ["total_return", "sharpe", "max_drawdown", "win_rate"].map((m) =>
              h("button", { key: m, type: "button", className: `tw-btn seg${metric === m ? " active" : ""}`,
                onClick: () => setMetric(m) }, zh("METRIC", m))),
            h("button", { type: "button", className: "tw-btn primary", disabled: busy, onClick: runSensitivity },
              busy ? "计算中…" : "计算网格")),
          failure && h("p", { className: "tw-alert" }, `计算失败：${failure}`),
          !result && !busy && h("p", { className: "tw-empty" }, "点击「计算网格」运行参数扫描（只读回测，不下单）。"),
          result && h(React.Fragment, null,
            h(HeatmapChart, { rowLabels: result.rows, colLabels: result.cols, matrix: result.matrix }),
            h("p", { className: "tw-meta" },
              `行=${zh("GRID_AXIS", result.row_label)} 列=${zh("GRID_AXIS", result.col_label)}`
              + ` · 指标=${zh("METRIC", result.metric)} · 样本 ${result.bars} 根（截至 ${result.as_of}）`),
            h("p", { className: "tw-meta" }, `最优：${zh("GRID_AXIS", result.row_label)}=${result.best.row}, ${zh("GRID_AXIS", result.col_label)}=${result.best.col}`
              + ` → ${zh("METRIC", result.metric)} ${result.best.value}（${result.best.trades} 笔 / 胜率 ${result.best.win_rate}）`),
            h("p", { className: "tw-hint" }, result.note))));
    }

    /** 未选标的时的事件页：给可操作提示，**不发请求**（否则 provider 会抛 Invalid ticker）。 */
    function EventsView({ rpc, ticker }) {
      if (!ticker) {
        return h(Card, { title: "事件", empty: "请先在「行情」页查询一个标的"
          + "（如 00700.HK / AAPL / 600519）；事件、因子与质量因子等页签共用它。" });
      }
      return h(EventsBody, { rpc, ticker });
    }

    function EventsBody({ rpc, ticker }) {
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
            empty: cardEmpty({ loading: events.loading, error: events.error,
              count: upcoming.length, fallback: "窗口内无即将发生的事件" }),
            render: (event, index) => line(event, index) }),
          events.data && h("p", { className: "tw-meta" },
            `来源状态：${Object.entries(events.data.sources_status || {})
              .map(([k, v]) => `${k}=${labeled(v, undefined, "SOURCE_STATUS")}`).join(" · ")}`)),
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
        empty: cardEmpty({ loading: sources.loading, error: sources.error,
          count: rows.length, fallback: "未返回渠道状态" }) },
        h(Paged, { items: rows, pageSize: 6, empty: "未返回渠道状态",
          render: (row) => h("div", { key: row.key, className: "tw-item" },
            h("div", { className: "tw-item-body", style: { paddingTop: "8px" } },
              h("span", { className: `tw-tag ${row.status === "ok" ? "buy" : row.status === "fail" ? "sell" : "hold"}` },
                `${icon[row.status] ?? ""} ${row.label}（${labeled(row.status, row.status_label, "SOURCE_STATUS")}）`),
              h("div", { className: "tw-meta" }, row.detail),
              row.status !== "ok" && row.fix && h("div", { className: "tw-meta" }, `修复：${row.fix}`))) }),
        sources.data && h("p", { className: "tw-meta" },
          `自检时间 ${sources.data.checked_at} · 正常 ${sources.data.summary.ok} / 待配置 ${sources.data.summary.warn} / 异常 ${sources.data.summary.fail}`));
    }

    const CHAIN_KIND = {
      signal: { label: "信号", cls: "" },
      order: { label: "下单", cls: "buy" },
      "order-modify": { label: "改单", cls: "buy" },
      "order-cancel": { label: "撤单", cls: "sell" },
      "order-facts": { label: "订单查询", cls: "hold" },
      "order-other": { label: "订单工具", cls: "buy" },
      "order-error": { label: "下单失败", cls: "sell" },
      fill: { label: "成交", cls: "hold" },
    };

    function AuditView({ rpc, snapshot }) {
      const audit = useEndpoint(rpc, "audit", {}, [rpc, snapshot.mode, snapshot.generated_at]);
      const entries = audit.data?.entries ?? [];
      const stats = audit.data?.stats;
      return h(React.Fragment, null,
        h(Card, { title: "链路统计",
          empty: stats ? undefined : cardEmpty({ loading: audit.loading, error: audit.error,
            count: 0, fallback: "暂无链路数据" }) },
          stats && h("div", { className: "tw-kv" },
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "信号"), h("div", { className: "tw-kv-v" }, String(stats.signals))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "订单相关响应"),
              h("div", { className: "tw-kv-v" }, String(stats.orders))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "成交（台账）"), h("div", { className: "tw-kv-v" }, String(stats.fills))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "已关联信号"),
              h("div", { className: "tw-kv-v", style: { color: "var(--dsw-alias-state-success-primary,#2ea043)" } }, String(stats.linked))),
            h("div", { className: "tw-kv-item" }, h("div", { className: "tw-kv-k" }, "未关联"),
              h("div", { className: "tw-kv-v", style: { color: stats.unlinked > 0 ? "var(--dsw-alias-state-warn-label,#9a6700)" : "inherit" } }, String(stats.unlinked)))),
          // 拆出动作明细：否则「订单相关响应 34」会被读成「下了 34 单」
          stats && stats.order_kinds && Object.keys(stats.order_kinds).length > 0
            && h("p", { className: "tw-meta" }, "构成："
              + Object.entries(stats.order_kinds).map(([label, n]) => `${label} ${n} 次`).join(" · ")),
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
                h("div", { className: "tw-meta" },
                  `${entry.detail} · 来源 ${entry.source_label ?? entry.source}`)));
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
        // 不在这里清缓存：缓存键里带 mode，模拟盘与实盘本就各存一份。
        // 之前清空全部缓存，导致每次切模式后所有页签都要重新取数（factors 冷启动 25 秒）。
        finally { setSwitching(false); setRevision((v) => v + 1); }
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
            // 徽标也用中文：结论性字样一律中文，缩写留给代码
            snapshot && h("span", { className: `tw-badge${live ? " live" : ""}` }, live ? "实盘" : "模拟盘"),
            snapshot && h("span", { className: "tw-meta" }, `更新 ${String(snapshot.generated_at).slice(11, 19)}`),
            h("span", { className: "tw-meta", title: "面板数据按 TTL 本地缓存，命中时不重新取数" },
              `本地缓存 ${cacheSize()} 项`),
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
        Card, cardEmpty, numeric, percent, percentValue,
        parseMarkdown, renderBlocks, blocksToHtml, Markdown, ReportDetail, Paged,
        zh, labeled, ZH,
        barIndexAt, tooltipLeft, compactNumber,
        servedEndpoints: () => servedEndpoints, cacheSize: () => endpointCache.size,
        missingEndpoints: () => [...missingEndpoints] } };
  },
});
