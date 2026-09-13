import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

async function client() {
  let definition;
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  vm.runInNewContext(source, { window: { __ModuleLoader__: { load: value => { definition = value; } } } });
  const React = { createElement: (tag, props, ...children) => ({ tag, props, children }) };
  return definition.factory(name => {
    assert.equal(name, "react");
    return React;
  });
}

test("native client factory contributes overlay and report cards without a second chat", async () => {
  const plugin = await client();
  const slots = [];
  plugin.apply({ connection: { rpc: { call() {} } },
    slots: { inject: (_name, fn) => fn(), register: (options, component) => slots.push({ options, component }) } });
  assert.equal(slots.filter(row => row.options.name === "shell.overlay").length, 1);
  assert.equal(slots.some(row => row.options.key === "research_publish"), true);
  assert.equal(slots.some(row => row.options.name.includes("input")), false);
  const card = slots.find(row => row.options.key === "research_publish").component;
  const tree = card({ block: { kind: "tool-result", content: [{ type: "text", text: "研究结果" }] } });
  assert.match(JSON.stringify(tree), /研究结果/);
});

test("RPC client sends only the narrow workbench channel and surfaces host errors", async () => {
  const plugin = await client();
  const calls = [];
  const rpc = { call: async (...args) => {
    calls.push(args);
    return { ok: false, error: { message: "refresh first" } };
  } };
  await assert.rejects(plugin.request(rpc, "snapshot", {}), /refresh first/);
  assert.equal(calls[0][0], "/api");
  assert.equal(calls[0][1], "trading-workbench/snapshot");
  await assert.rejects(plugin.request(rpc, "execute-order", {}), /Unsupported/);
  assert.equal(calls.length, 1);
});

test("workbench client ships pagination, report detail, and drawer UI", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  assert.match(source, /function Paged\(/, "缺少通用分页组件");
  assert.match(source, /function ReportDetail\(/, "缺少研报详情视图");
  assert.match(source, /onOpenReport/, "研报标题未接线到详情");
  assert.match(source, /tw-pager/, "分页样式缺失");
  assert.match(source, /tw-drawer/, "抽屉容器缺失");
  assert.match(source, /在新标签打开/, "缺少新标签打开入口");
  // 所有列表类视图必须走 Paged，避免出现无分页的超长列表
  // 注意 EventsView 只是"未选标的时给提示"的外壳，真正渲染列表的是 EventsBody。
  // 这里要覆盖**所有会渲染记录列表**的组件：漏掉一个就会出现无分页的长列表。
  for (const view of ["ResearchView", "SignalView", "ExecutionView", "AuditView", "EventsBody",
    "SourcesCard", "ReportDetail", "FactorsView", "PortfolioView", "TradeSummaryCard"]) {
    const body = source.slice(source.indexOf(`function ${view}(`));
    const end = body.indexOf("\n    function ", 10);
    const text = end === -1 ? body : body.slice(0, end);
    assert.match(text, /h\(Paged,/, `${view} 未使用分页`);
  }
});

test("workbench ships a Futu instrument card and a cached K-line chart, with no sample data", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  // K 线图已于 2026-09-13 按用户要求加回（此前一度被移除）；标的卡片保留
  assert.match(source, /function KLineChart\(/, "缺少 K 线图组件");
  assert.match(source, /function KLineCard\(/, "缺少 K 线卡片");
  assert.match(source, /在富途查看K线/, "缺少跳转富途看 K 线的入口");
  assert.match(source, /instrument/, "未使用标的卡片接口");
  // 不得内置示例标的（默认标的池/默认行情标的）
  for (const code of ["600519", "000001", "601318", "600036", "300750"]) {
    assert.ok(!source.includes(`"${code}"`), `仍存在示例标的 ${code}`);
  }
  assert.match(source, /useState\(""\)/, "行情标的默认值应为空");
  assert.match(source, /useState\(\[\]\)/, "标的池默认值应为空");
});

test("客户端会声明式跳过 Host 未提供的接口，而不是撞 404", async () => {
  const plugin = await client();
  const calls = [];
  const rpc = { call: async (...args) => {
    calls.push(args);
    // Host 声明：只有 snapshot 与 switch-mode（模拟进程陈旧的旧版 Host）
    return { ok: true, value: { mode: "sim", endpoints: ["snapshot", "switch-mode"] } };
  } };
  await plugin.request(rpc, "snapshot", {});
  assert.equal(plugin.internals.servedEndpoints().has("positions"), false);
  const before = calls.length;
  await assert.rejects(plugin.request(rpc, "positions", { mode: "sim" }), /Host 未提供 positions 接口/);
  assert.match(await plugin.request(rpc, "positions", { mode: "sim" }).catch((e) => e.message), /重启 dsh web/);
  assert.equal(calls.length, before, "已知缺失的接口不应再发起请求");
});

test("Host 未声明接口清单时不拦截（向后兼容旧 Host）", async () => {
  const plugin = await client();
  const calls = [];
  const rpc = { call: async (...args) => { calls.push(args); return { ok: true, value: {} }; } };
  await plugin.request(rpc, "positions", { mode: "sim" });
  assert.equal(calls.length, 1);
});

test("客户端与 Host 的接口清单必须一致", async () => {
  const { ENDPOINTS } = await import("../plugins/workbench/src/endpoints.js");
  const plugin = await client();
  assert.deepEqual([...plugin.internals.KNOWN_ENDPOINTS].sort(), [...ENDPOINTS].sort(),
    "两侧接口清单漂移会让客户端调用不存在的路由");
});

test("面板结果会缓存，避免切页签时重复取数", async () => {
  const plugin = await client();
  const { readCache, writeCache, invalidateCaches, cacheSize } = plugin.internals;
  const payload = { ticker: "600519" };
  assert.equal(readCache("instrument", payload), null);
  writeCache("instrument", payload, { price: 1 });
  assert.deepEqual(readCache("instrument", payload).value, { price: 1 });
  // 不同参数是不同缓存条目
  assert.equal(readCache("instrument", { ticker: "00700.HK" }), null);
  invalidateCaches();
  assert.equal(readCache("instrument", payload), null);
  assert.equal(cacheSize(), 0);
});

test("不缓存的接口不会被写入缓存", async () => {
  const plugin = await client();
  const { readCache, writeCache } = plugin.internals;
  writeCache("snapshot", {}, { mode: "sim" });
  assert.equal(readCache("snapshot", {}), null, "snapshot 走轮询，不应进缓存");
});

test("旧版 Host 不声明清单时，从 404 反推并停止重试", async () => {
  const plugin = await client();
  const calls = [];
  const rpc = { call: async (...args) => {
    calls.push(args);
    if (String(args[1]).endsWith("/snapshot")) return { ok: true, value: { mode: "sim" } };  // 旧 Host：无 endpoints
    throw new Error("transport failure for /api/trading-workbench/positions: HTTP 404");
  } };
  await plugin.request(rpc, "snapshot", {});
  await assert.rejects(plugin.request(rpc, "positions", { mode: "sim" }), /Host 未提供 positions 接口/);
  assert.deepEqual([...plugin.internals.missingEndpoints()], ["positions"]);
  const before = calls.length;
  // 第二次不再发请求，错误信息保持一致
  await assert.rejects(plugin.request(rpc, "positions", { mode: "sim" }), /重启 dsh web/);
  assert.equal(calls.length, before, "已知缺失的接口不应重复请求");
});

test("业务失败不会被误判为路由缺失", async () => {
  const plugin = await client();
  const rpc = { call: async () => { throw new Error("trading/analytics-unavailable: 台账不可读"); } };
  await assert.rejects(plugin.request(rpc, "positions", { mode: "sim" }), /台账不可读/);
  assert.deepEqual([...plugin.internals.missingEndpoints()], []);
});

test("刷新会清掉路由缺失记忆，允许重启后的 Host 恢复", async () => {
  const plugin = await client();
  let phase = "old";
  const rpc = { call: async (...args) => {
    if (String(args[1]).endsWith("/snapshot")) return { ok: true, value: { mode: "sim" } };
    if (phase === "old") throw new Error("transport failure for /x: HTTP 404");
    return { ok: true, value: { positions: [] } };
  } };
  await plugin.request(rpc, "snapshot", {});
  await assert.rejects(plugin.request(rpc, "positions", { mode: "sim" }), /Host 未提供/);
  phase = "new";
  plugin.internals.invalidateCaches();
  assert.deepEqual((await plugin.request(rpc, "positions", { mode: "sim" })).positions, []);
});

test("K 线周期预设：默认日线，根数不超过富途单次上限", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const periods = [...source.matchAll(/\{ id: "([^"]+)", label: "([^"]+)", limit: (\d+) \}/g)]
    .map(([, id, label, limit]) => ({ id, label, limit: Number(limit) }));
  assert.ok(periods.length >= 3, "周期预设过少");
  assert.equal(periods[0].id, "1d", "默认应为日线");
  const defaultPeriod = source.match(/const KLINE_DEFAULT_PERIOD = "([^"]+)"/)?.[1];
  assert.equal(defaultPeriod, "1d", "默认周期必须是日线");
  for (const row of periods) {
    assert.ok(row.limit >= 20 && row.limit <= 370,
      `${row.id} 根数 ${row.limit} 超出富途单次上限（20..370）`);
  }
  assert.ok(periods.some((p) => p.id === "1d"), "缺少日线");
  assert.ok(periods.some((p) => ["1m", "5m"].includes(p.id)), "缺少分钟级");
});

test("K 线按需挂载：未解析标的时不请求 series", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  // KLineCard 只在 submitted 非空时挂载，避免空 ticker 触发无意义请求
  assert.match(source, /submitted && h\(KLineCard/,
    "K 线卡片应在标的解析后才挂载");
});

test("K 线也会走缓存，不会因切页签重复取数", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const ttl = source.match(/series: (\d+) \* 60_000/);
  assert.ok(ttl, "series 未配置客户端 TTL，切页签会重复取数");
  assert.ok(Number(ttl[1]) >= 1, "series TTL 过短");
});

test("需要标的的页签在未选标的时给提示，而不是发请求报 Invalid ticker", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  // 事件页此前在 ticker 为空时仍发请求，provider 校验不过 → 界面显示 "Invalid ticker"
  const body = source.slice(source.indexOf("function EventsView("));
  const end = body.indexOf("\n    function ", 10);
  const text = end === -1 ? body : body.slice(0, end);
  assert.match(text, /if \(!ticker\)/, "EventsView 缺少空标的守卫");
  assert.match(text, /先在「行情」页查询/, "缺少可操作的提示");
  assert.doesNotMatch(text, /useEndpoint\(/, "守卫分支不应再发请求");
  assert.match(source, /function EventsBody\(/, "实际渲染应移到 EventsBody");
});

test("质量因子卡片同样受空标的守卫保护", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  assert.match(source, /ticker && h\(QualityCard/, "QualityCard 应在有标的时才挂载");
});

test("从券商持仓取标的时字段名必须对齐（symbol 而非 ticker）", async () => {
  // positions.py 输出的是 symbol；曾误读 p.ticker，导致 held 恒为空、相关性从未算出
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const line = source.slice(source.indexOf("const held ="), source.indexOf("const held =") + 220);
  assert.match(line, /p\.symbol/, "应从 symbol 取标的");
  assert.match(line, /filter\(Boolean\)/, "应过滤掉取不到标的的行");
  assert.match(source, /const canCorrelate = held\.length >= 2/, "相关性应有前置条件判断");
});

test("条件不满足时不把 provider 的原始报错抖到界面上", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  assert.match(source, /有效标的不足：请在下方输入至少 2 个标的/, "因子页缺友好提示");
  assert.match(source, /IC 需要 3\.\.8 个标的/, "IC 卡片缺友好提示");
  assert.match(source, /需要至少 2 个/, "相关性卡片缺友好提示");
});

test("Card 的 empty 只能在「确实没有内容」时才给值", async () => {
  const plugin = await client();
  const { Card, cardEmpty } = plugin.internals;

  // 加载中 / 出错 / 无数据：三种情况各给各的文案
  assert.equal(cardEmpty({ loading: true, error: "", count: 0, fallback: "无数据" }), "读取中…");
  assert.equal(cardEmpty({ loading: false, error: "boom", count: 5, fallback: "无数据" }), "boom");
  assert.equal(cardEmpty({ loading: false, error: "", count: 0, fallback: "无数据" }), "无数据");
  // 关键：成功且有数据时必须返回 undefined，否则 Card 会用兜底文案替换掉内容
  assert.equal(cardEmpty({ loading: false, error: "", count: 6, fallback: "不可用" }), undefined);

  // 行为验证：empty 为 undefined 时渲染 children，为文案时替换掉 children
  const withData = Card({ title: "x", count: 6, empty: undefined, children: "内容" });
  const body = withData.children[1];   // 假 React 把 children 收成数组
  assert.deepEqual(body.children, ["内容"], "有数据时不应被兜底文案替换");
  const emptyCard = Card({ title: "x", count: 0, empty: "不可用", children: "内容" });
  assert.match(JSON.stringify(emptyCard.children[1]), /不可用/);
});

test("全库不得再出现「error || 兜底文案」式的 empty", async () => {
  // 反例：empty: loading ? "加载中…" : (x.error || "兜底")
  // 成功时 error 是空串，"" || "兜底" 为真 → Card 用兜底替换已取到的内容，
  // 表现为「徽标显示 N 条，正文写着不可用/暂无数据」，用户会以为没取到数据。
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const offenders = source.split("\n")
    .map((line, index) => ({ line: line.trim(), number: index + 1 }))
    .filter(({ line }) => !line.startsWith("*") && !line.startsWith("//"))
    .filter(({ line }) => /loading \?.*:\s*\(.*error \|\|/.test(line));
  assert.deepEqual(offenders, [], "仍有 empty 使用了会吞掉数据的写法");
});

test("error 只交给 cardEmpty 决定优先级，不得自己用 || 兜底", async () => {
  // 这条规则不依赖写法格式：只要没有 `error ||`，就不可能把错误信息
  // 与兜底文案的优先级写错（成功时 error 是空串，`||` 会选到兜底文案）。
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const offenders = source.split("\n")
    .map((line, index) => ({ text: line, number: index + 1 }))
    .filter(({ text }) => text.includes("error ||"))
    .filter(({ text }) => !text.trim().startsWith("*") && !text.trim().startsWith("//"));
  assert.deepEqual(offenders.map((row) => `${row.number}: ${row.text.trim()}`), []);
});

test("skill 与限制文档必须写明：券商能力限制以实测为准，账户不可混算", async () => {
  const skill = await readFile(new URL("../skills/trading-agents/SKILL.md", import.meta.url), "utf8");
  const limits = await readFile(new URL("../docs/TOOL-LIMITS.md", import.meta.url), "utf8");

  for (const [name, text] of [["SKILL.md", skill], ["TOOL-LIMITS.md", limits]]) {
    // account_orders_history 不传时间范围会静默返回 no data，属"看起来没数据"的坑
    assert.match(text, /account_orders_history/, `${name} 未记录 account_orders_history 的陷阱`);
    assert.match(text, /start.*end|start`\/`end/, `${name} 未写明必须传时间范围`);
    // 券商能力限制不得写成长期规则
    assert.match(text, /实际下单|实际下单结果/, `${name} 未写明"能力限制以实际下单为准"`);
    // 账户模式必须区分
    assert.match(text, /sim\s*[|\/]\s*live|sim\/live|sim.*live.*账户/, `${name} 未写明记忆需区分账户`);
  }
  // 记忆条目格式必须带账户字段
  assert.match(skill, /\[<日期> \| <sim\|live>账户/, "记忆条目格式未包含账户字段");
});

test("K 线悬停：鼠标 x 坐标 → 正确的 K 线下标", async () => {
  const plugin = await client();
  const { barIndexAt } = plugin.internals;
  // 绘图区 x∈[54, 254)，10 根 K 线 → 每根 20px
  const shape = { padL: 54, plotW: 200, count: 10 };
  assert.equal(barIndexAt(54, shape), 0, "绘图区左边界是第 0 根");
  assert.equal(barIndexAt(73.9, shape), 0);
  assert.equal(barIndexAt(74, shape), 1, "跨过一根宽就应换下标");
  assert.equal(barIndexAt(253.9, shape), 9, "最后一根");
  // 绘图区之外一律 null，不能误命中首尾
  assert.equal(barIndexAt(53.9, shape), null, "左侧留白区不应命中");
  assert.equal(barIndexAt(254, shape), null, "右侧留白区不应命中");
  assert.equal(barIndexAt(-100, shape), null);
  assert.equal(barIndexAt(9999, shape), null);
  // 边界输入不应抛
  assert.equal(barIndexAt(10, { padL: 54, plotW: 0, count: 10 }), null);
  assert.equal(barIndexAt(10, { padL: 54, plotW: 200, count: 0 }), null);
});

test("K 线悬停：信息框在靠近右边缘时翻到左侧，不会超出画布", async () => {
  const plugin = await client();
  const { tooltipLeft } = plugin.internals;
  const width = 600, boxW = 160;
  // 光标在左侧：信息框正常放右侧
  assert.equal(tooltipLeft(100, boxW, width), 114);
  // 光标靠近右边缘：右侧放不下，翻到左侧
  assert.equal(tooltipLeft(560, boxW, width), 560 - 14 - boxW);
  // 连左侧也放不下（画布很窄）：贴右边缘，但不越界
  const narrow = 200;
  const left = tooltipLeft(150, boxW, narrow);
  assert.ok(left >= 4, "不能越出左边界");
  assert.ok(left + boxW <= narrow - 4 + 0.001, "不能越出右边界");
});

test("K 线悬停：成交量用紧凑写法", async () => {
  const plugin = await client();
  const { compactNumber } = plugin.internals;
  assert.equal(compactNumber(12345), "1.23万");
  assert.equal(compactNumber(123456789), "1.23亿");
  assert.equal(compactNumber(999), "999");
  assert.equal(compactNumber(null), "—");
  assert.equal(compactNumber("abc"), "—");
});

test("K 线图接上了鼠标事件，且只在跨根时才重绘", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const body = source.slice(source.indexOf("function KLineChart("));
  const end = body.indexOf("\n    function ", 10);
  const text = end === -1 ? body : body.slice(0, end);
  assert.match(text, /onMouseMove: handleMove/, "缺少鼠标移动处理");
  assert.match(text, /onMouseLeave: \(\) => setHover\(null\)/, "缺少移出清理");
  assert.match(text, /if \(next !== hover\) setHover\(next\)/, "应在跨到另一根 K 线时才 setState，避免每次移动都重绘");
  assert.match(text, /geometry\.current = \{/, "绘制时须记录几何量，命中判定才能与绘制对齐");
  assert.match(source, /function drawKLineHover\(/, "缺少悬停绘制");
});

test("数值展示：字段缺失时显示「—」，不得出现 NaN/undefined", async () => {
  const plugin = await client();
  const { internals } = plugin;
  assert.equal(typeof internals.numeric, "function", "缺少统一数值展示函数");
  const { numeric } = internals;
  // 注意：numeric 不做 ×100，调用方传入已换算好的百分数
  assert.equal(numeric(12.34, 2, "%"), "12.34%");
  assert.equal(numeric(0.1234, 2), "0.12");
  assert.equal(numeric(1.5, 2, "%"), "1.50%");
  assert.equal(numeric(0, 2, "%"), "0.00%", "0 是有效数值");
  assert.equal(numeric(undefined), "—");
  assert.equal(numeric(null), "—");
  assert.equal(numeric(NaN), "—");
  assert.equal(numeric(Infinity), "—");
  assert.equal(numeric(undefined, 2, "%"), "—");
});

test("台账为空时风险卡片不得显示 NaN% / undefined", async () => {
  // 实测 analytics.py 在台账为空时返回 {mode, points:[], count:0, note}，
  // **没有 max_drawdown / sharpe**；客户端曾用"对象是否存在"判断，于是显示 NaN%/undefined。
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const risky = source.split("\n")
    .map((line, index) => ({ text: line, number: index + 1 }))
    .filter(({ text }) => !text.trim().startsWith("//") && !text.trim().startsWith("*"))
    .filter(({ text }) => /\.(max_drawdown|sharpe|annualized)\s*\*/.test(text)
      || /String\([a-z]+\.data\.(sharpe|max_drawdown)\)/.test(text))
    .filter(({ text }) => !text.includes("numeric("));
  assert.deepEqual(risky.map((row) => `${row.number}: ${row.text.trim()}`), [],
    "存在未经 numeric() 包裹的数值展示");
});

test("风险卡片的空态按字段判断，而不是按对象是否存在", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  const body = source.slice(source.indexOf("function RiskView("));
  const end = body.indexOf("\n    function ", 10);
  const text = end === -1 ? body : body.slice(0, end);
  assert.match(text, /Number\.isFinite\(equity\.data\?\.max_drawdown\)/,
    "空态应按字段是否为有限数判断——equity.data 在无数据时仍是对象");
  assert.doesNotMatch(text, /empty: \(s \|\| equity\.data\)/,
    "不得再用「对象存在」当作「有数据」");
});

test("风险页包含按账户分组的持仓风险，且不跨账户合计", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  assert.match(source, /function PortfolioRiskCard\(/, "缺少持仓风险卡");
  assert.match(source, /function AccountRiskRow\(/, "缺少账户风险行组件");
  const view = source.slice(source.indexOf("function RiskView("));
  assert.match(view.slice(0, view.indexOf("\n    function ", 10)), /h\(PortfolioRiskCard/,
    "风险页未挂载持仓风险卡");
  // 必须用 Paged（所有列表都要分页）
  const card = source.slice(source.indexOf("function PortfolioRiskCard("));
  assert.match(card.slice(0, card.indexOf("\n    function ", 10)), /h\(Paged,/);
  // 两个占比口径都要出现，避免不同分母被混读
  assert.match(source, /share_of_positions/);
  assert.match(source, /share_of_assets/);
  assert.match(source, /不跨账户合计/);
});

test("风险页说明了策略层与持仓层口径不同", async () => {
  const source = await readFile(new URL("../plugins/workbench/src/client.js", import.meta.url), "utf8");
  assert.match(source, /那是\*\*策略层\*\*的指标/,
    "必须写明回撤/夏普是策略层指标，不是这个账户的实际表现");
});
