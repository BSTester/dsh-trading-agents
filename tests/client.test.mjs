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
