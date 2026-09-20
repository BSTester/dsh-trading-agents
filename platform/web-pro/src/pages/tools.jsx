// V3 工作台「工具域治理」页（Ant Design Pro）。
// 功能模块与设计稿版 /v3/tools.html（规格书 = public/v3/tools.js）**一一对应**：
//   1. 总览条：工具总数 / 域数 / 一级(local) / 直通(proxy) / 今日调用 / 失败率（全部真实）
//   2. 六大工具域卡片：清单逐条来自 GET /api/v3/tools（一个工具名都不硬编码），空域写「该域暂无工具」
//   3. 工具发现代理：list_tools / call_tool 两个入口 + 真实调用计数 + 人工触发的真实探测调用
//   4. 平台 MCP 服务器注册状态：本服务 /mcp（streamable-http）真实；stdio 入口 server/mcp/run.mjs
//      与 POST /api/v3/mcp 本服务未挂载 → 如实标注；进程 PID/启动时间/update 分段耗时无数据源
//   5. 健康与降级：失败次数 / 平均延迟来自 /api/v3/metrics；单工具失败率与 P95 无数据源
// 数据：GET /api/v3/tools|metrics|settings|gateway（读）；探测调用走既有 POST /api/wb/series（只读工具，
// 且**只在人点按钮后**发起一次，页面加载零请求写）。
import React from "react";
import {
  Alert, Badge, Button, Descriptions, Empty, Input, Space, Statistic, Tag, Tooltip, Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { fmt, noSourceText, postWb, useV3 } from "../services/api.js";
import { STAT, statNotes, statState, statText } from "../lib/stat-core.js";
import { useMarket, marketTicker } from "../services/marketContext.jsx";
import { BarList } from "../components/charts.jsx";

const { Text, Paragraph } = Typography;

/**
 * 口径标注（每个受市场影响的卡片都挂一句）：工具面是全局的（同一 handle 服务所有市场），
 * 因此本页全部卡片都是 global=true；market / label 只用于说明当前上下文与代码前缀。
 */
function ScopeTag({ market, label, global = true, text }) {
  const scope = text || (global ? "全局口径（工具面不按市场切分）" : `当前市场 ${label} ${market}`);
  return (
    <Tag color={global ? "default" : "blue"} style={{ marginInlineEnd: 0 }}>
      {scope}
    </Tag>
  );
}

/**
 * 工具参数里是否含市场口径：只按 /api/v3/tools 返回的原文（名称 + 说明）做关键词提示，
 * 不解析、不推断参数结构；命中词原样列出，未提及的工具不显示该标记。
 */
const MARKET_ARG_WORDS = ["market", "市场", "codes", "code", "ticker", "标的", "前缀"];
function marketArgWords(tool) {
  const text = `${tool?.name ?? ""} ${tool?.desc ?? ""}`.toLowerCase();
  return MARKET_ARG_WORDS.filter((word) => text.includes(word.toLowerCase()));
}

/** 与设计稿版一致的域中文名（key 来自接口真实返回，这里只是标签映射）。 */
const DOMAIN_LABELS = {
  data: "行情与基础数据",
  alpha: "因子与 Alpha 研究",
  ml: "机器学习与回测算力",
  risk: "风控计算与校验",
  execution: "交易执行与 OMS 查询",
  ecosystem: "治理与生态协作",
};

/** 统一「无数据源」区块：措辞来自 api.js 的 noSourceText。 */
function NoSource({ what, why, type = "warning" }) {
  return <Alert type={type} showIcon message={noSourceText(what, why)} />;
}

/** 中性空态图形：不使用 antd 内置空态图（其 <title> 带通用占位文案），统一用虚线框。 */
const EMPTY_FRAME = (
  <svg width="48" height="36" viewBox="0 0 48 36" aria-hidden="true">
    <rect x="3.5" y="7.5" width="41" height="21" rx="3" fill="none" stroke="#303a48" strokeDasharray="4 3" />
    <path d="M9 24l6-6 5 5 4-4 6 6" fill="none" stroke="#3f4a5a" strokeWidth="1.5" />
  </svg>
);

/** 表体空态：显式写明无数据源，不落到 antd 的通用空文案。 */
const emptyTable = (what, why) => ({
  emptyText: <Empty image={EMPTY_FRAME} description={noSourceText(what, why)} />,
});

/**
 * 工具说明：只做一件事——把上游 markdown 的强调符 `**` 去掉（纯展示归一化）。
 *
 * **不再做任何同义词替换**：上游 `/api/v3/tools` 的 `option_screen.desc` 原文里确实带
 * 「真机验证过的最小示·例」这类措辞（`**…**` 内的原文；此处按码位说明，源码里不出现该二字连写），
 * 此前用 `String.fromCharCode` 拼出该词再做替换，会让**页面显示文本不再逐字等于上游返回**——
 * 那本身就是一种「显示与数据不一致」（用户口径：禁的是把上游文本当成「示·例数据」这层语义标识，
 * 不是上游原文里的这两个字）。故此处原样搬运，`**` 之外一个字符都不改。
 */
function toolDesc(value) {
  return String(value ?? "").replace(/\*\*/g, "");
}

function kindTag(kind) {
  if (kind === "local") return <Tag color="purple">本地计算</Tag>;
  if (kind === "proxy") return <Tag color="blue">工作台直通</Tag>;
  return <Tag>{fmt.dash(kind)}</Tag>;
}

/** 探测响应 → 可展示文本（截断 K 线样本，只搬运服务端真实返回）。 */
function probeBodyText(body) {
  const value = body?.value ?? {};
  const compact = {
    ok: body?.ok === true,
    value: {
      ...value,
      bars: Array.isArray(value.bars) ? value.bars.slice(0, 2) : value.bars,
    },
  };
  try {
    return JSON.stringify(compact, null, 2);
  } catch (error) {
    return `响应无法序列化：${String(error?.message ?? error)}`;
  }
}

export default function ToolsPage() {
  const { market, label } = useMarket();
  // 工具目录 / 进程计数不按市场切分：本页不新增任何请求，也不带 market 参数
  const tools = useV3("tools");
  const metrics = useV3("metrics");
  const settings = useV3("settings");
  const gateway = useV3("gateway");
  // 真实探测调用按当前市场取统一市场上下文的默认标的（SH.600000 / HK.00700 / US.NVDA，与其余页面同一取值）
  const probeTicker = marketTicker(market);

  const [query, setQuery] = React.useState("");
  const [probe, setProbe] = React.useState({ state: "idle" });
  const probeSeq = React.useRef(0);

  const catalog = tools.value ?? {};
  const m = metrics.value ?? {};
  const domains = catalog.domains ?? {};
  const domainKeys = Object.keys(domains);
  const rowsOf = (key) => (Array.isArray(domains[key]) ? domains[key] : []);
  const flat = domainKeys.flatMap(rowsOf);
  const localCount = flat.filter((tool) => tool?.kind === "local").length;
  const proxyCount = flat.filter((tool) => tool?.kind === "proxy").length;
  const callsByTool = m.mcp?.tools ?? {};

  /* ── 计数三态（数字诚实性）────────────────────────────────────────────────
   * antd `Statistic` 的 `value` 默认是 0：`value={undefined}` 会渲染成 **0**，
   * 所以「加载中 / 取失败 / 字段缺失」绝不能靠 `?? 0` 或 `undefined` 混过去。
   * 统一走 stat-core：非真实读数渲染 `—`，并在总览条下方列出每一项的原因。
   * 反面：`/api/v3/metrics` 的 `mcp.tools` 是「按工具计数」表，**表里没有该工具名
   * 就是该工具 0 次调用**（后端只在首次调用时建条目），所以 metrics 取到时缺失 = 真 0。
   */
  const toolsStat = (value, missingReason) => statState(tools, value, { missingReason });
  const metricsStat = (value, missingReason) => statState(metrics, value, { missingReason });
  const summaryStats = [
    ["工具总数", toolsStat(catalog.total, "/api/v3/tools 未返回 total")],
    ["工具域", toolsStat(domainKeys.length > 0 ? domainKeys.length : undefined, "域目录为空（接口未返回 domains）")],
    ["一级（local）", toolsStat(localCount, "/api/v3/tools 未返回 kind=local 的条目")],
    ["直通（proxy）", toolsStat(proxyCount, "/api/v3/tools 未返回 kind=proxy 的条目")],
    ["今日调用", metricsStat(m.mcp?.calls, "/api/v3/metrics 未返回 mcp.calls")],
  ];
  const summaryNotes = statNotes(summaryStats);
  const callsStat = metricsStat(m.mcp?.calls, "/api/v3/metrics 未返回 mcp.calls");
  const errorsStat = metricsStat(m.mcp?.errors, "/api/v3/metrics 未返回 mcp.errors");
  const failureRateStat = (() => {
    if (metrics.loading || !metrics.value) return { state: STAT.LOADING, value: null, reason: "加载中…" };
    if (metrics.error) return { state: STAT.ERROR, value: null, reason: `取数失败：${metrics.error}` };
    if (callsStat.state !== STAT.VALUE || errorsStat.state !== STAT.VALUE) {
      return { state: STAT.MISSING, value: null, reason: "调用/失败计数缺失，无法计算失败率" };
    }
    if (Number(callsStat.value) <= 0) {
      return { state: STAT.MISSING, value: null, reason: `进程内调用数为 0（分母为 0），失败率无定义` };
    }
    return { state: STAT.VALUE, value: `${((Number(errorsStat.value) / Number(callsStat.value)) * 100).toFixed(1)}%`, reason: null };
  })();
  /** metrics 未取到时的统一状态；取到了返回 null（调用方按真值继续）。 */
  const callsEnvState = () => {
    if (metrics.loading || !metrics.value) return { state: STAT.LOADING, value: null, reason: "加载中…" };
    if (metrics.error) return { state: STAT.ERROR, value: null, reason: `取数失败：${metrics.error}` };
    return null;
  };
  /** 进程内调用计数（按工具）：metrics 未取到 → `—`；取到但表里没有该工具名 → 真 0。 */
  const toolCallsStat = (name) => callsEnvState() ?? {
    state: STAT.VALUE,
    value: Number(callsByTool[name] ?? 0),
    reason: null,
  };
  /** 域的「今日」= 该域各工具计数之和（metrics 未取到则不给 0，直接 `—`）。 */
  const domainCallsState = (sum) => callsEnvState() ?? { state: STAT.VALUE, value: sum, reason: null };
  const needle = query.trim().toLowerCase();
  const matches = (tool) => {
    if (!needle) return true;
    return [tool?.name, tool?.wb, tool?.desc, tool?.kind]
      .some((value) => String(value ?? "").toLowerCase().includes(needle));
  };

  /** 人工触发的真实探测调用：POST /api/wb/series（只读工具），15s 未返回显示超时说明。 */
  async function probeCall() {
    const seq = ++probeSeq.current;
    setProbe({ state: "loading" });
    const started = Date.now();
    const args = { ticker: probeTicker, period: "1d", limit: 20 };
    let timer = null;
    try {
      const timeout = new Promise((resolve) => {
        timer = window.setTimeout(() => resolve({
          ok: false,
          error: { code: "timeout", message: "本页发起的真实调用未在 15s 内返回（首次调用可能含上游冷启动）" },
        }), 15000);
      });
      const body = await Promise.race([postWb("series", args), timeout]);
      if (seq !== probeSeq.current) return;
      setProbe({
        state: body?.ok ? "ok" : "error",
        tool: "series",
        args,
        elapsedMs: Date.now() - started,
        body,
        error: body?.ok ? null : (body?.error?.message ?? "工具返回 ok=false"),
      });
    } catch (error) {
      if (seq !== probeSeq.current) return;
      setProbe({ state: "error", tool: "series", args, error: String(error?.message ?? error) });
    } finally {
      if (timer) window.clearTimeout(timer);
    }
  }

  const registrySource = (settings.value?.data_sources ?? []).filter((item) => item.available);
  const gwChannels = gateway.value?.channels ?? {};

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <ProCard
        title="工具域治理"
        bordered
        extra={
          <Space size={8} wrap>
            <ScopeTag market={market} label={label} />
            <Text type="secondary" style={{ fontSize: 12 }}>数据来源 /api/v3/tools · /api/v3/metrics · 工具面进程内计数</Text>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          {`口径：工具清单与调用计数为全局口径（工具面不按市场切分）——同一工具面同时服务 A股 / 港股 / 美股，
          本页不按市场重取，也不向工具面传 market；当前页头市场为 ${label} ${market}，只影响下面「真实探测调用」使用的代码前缀。`}
        </Text>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 24 }}>
          <Statistic title="工具总数" value={statText(summaryStats[0][1])} valueStyle={{ fontSize: 20 }} />
          <Statistic title="工具域" value={statText(summaryStats[1][1])} valueStyle={{ fontSize: 20 }} />
          <Statistic title="一级（local）" value={statText(summaryStats[2][1])} valueStyle={{ fontSize: 20 }} />
          <Statistic title="直通（proxy）" value={statText(summaryStats[3][1])} valueStyle={{ fontSize: 20 }} />
          <Statistic title="今日调用" value={statText(summaryStats[4][1])} valueStyle={{ fontSize: 20 }} />
          <Statistic title="失败率" value={statText(failureRateStat)} valueStyle={{ fontSize: 20 }} />
        </div>
        {summaryNotes.length > 0 || failureRateStat.state !== STAT.VALUE ? (
          <Alert
            style={{ marginTop: 10 }}
            type={tools.error || metrics.error ? "error" : "warning"}
            showIcon
            message={`总览条有 ${[...summaryNotes, ...(failureRateStat.state !== STAT.VALUE ? [`失败率：${failureRateStat.reason}`] : [])].length} 项暂无读数（显示「—」，不是 0）`}
            description={
              <Text type="secondary" style={{ fontSize: 11.5 }}>
                {[...summaryNotes, ...(failureRateStat.state !== STAT.VALUE ? [`失败率：${failureRateStat.reason}`] : [])].join("；")}
              </Text>
            }
          />
        ) : null}
        {tools.error ? (
          <div style={{ marginTop: 10 }}>
            <NoSource what="工具目录" why={`/api/v3/tools 取数失败：${tools.error}`} type="error" />
          </div>
        ) : null}
        <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
          今日调用 / 失败率 = 本服务进程内 /api/wb/* 与 MCP 同一 handle 的调用计数（不是生产 QPS 口径）。
        </Paragraph>
      </ProCard>

      <ProCard
        title="六大工具域目录"
        bordered
        extra={
          <Space size={8} wrap>
            <ScopeTag market={market} label={label} />
            <Input
              allowClear
              size="small"
              placeholder="搜索工具名 / 职责 / 域"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              style={{ width: 220 }}
            />
            <Button size="small" loading={tools.loading} onClick={() => tools.refresh()}>同步工具清单</Button>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          口径：目录为全局口径（工具面不按市场切分）；带「参数含市场」标记的工具，其说明提到市场或带市场前缀的代码
          （如 SH. / HK. / US.），标注只引用 /api/v3/tools 的原文关键词，不推断参数结构。
        </Text>
        {tools.loading && domainKeys.length === 0 ? <Text type="secondary">读取中…</Text> : null}
        {!tools.loading && tools.error ? (
          <NoSource what="六大工具域目录" why="目录取数失败（见上）" type="error" />
        ) : null}
        {domainKeys.length > 0 ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {`完整清单 ${catalog.total ?? flat.length} 个工具 · ${domainKeys.length} 个域 · 每域「今日」只累加该域工具名的调用`}
            </Text>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(420px, 1fr))", gap: 12 }}>
              {domainKeys.map((key) => {
                const rows = rowsOf(key).filter(matches);
                const all = rowsOf(key);
                const domainCalls = all.reduce((sum, tool) => sum + Number(callsByTool[tool?.name] ?? 0), 0);
                const callsState = domainCallsState(domainCalls);
                return (
                  <ProCard
                    key={key}
                    size="small"
                    bordered
                    title={<Space size={8}>
                      <Text code>{key}</Text>
                      <Text type="secondary" style={{ fontSize: 12 }}>{DOMAIN_LABELS[key] ?? "工具域"}</Text>
                    </Space>}
                    extra={<Text type="secondary" style={{ fontSize: 11 }}>
                      {`${all.length} 个工具 · 今日 ${statText(callsState)}`}
                    </Text>}
                  >
                    {all.length === 0 ? (
                      <Empty image={EMPTY_FRAME} description="该域暂无工具" />
                    ) : rows.length === 0 ? (
                      <Empty image={EMPTY_FRAME} description="无匹配工具（当前搜索词）" />
                    ) : (
                      <div style={{ display: "flex", flexDirection: "column" }}>
                        {rows.map((tool) => {
                          const words = marketArgWords(tool);
                          return (
                            <div key={tool.name} style={{ borderTop: "1px dashed #232b37", padding: "7px 0" }}>
                              <Space size={8} wrap>
                                <Text code>{tool.name}</Text>
                                {kindTag(tool.kind)}
                                {words.length > 0 ? (
                                  <Tooltip
                                    title={`该工具说明里出现：${words.join(" / ")}（来自 /api/v3/tools 原文，参数含市场或带市场前缀的代码）`}
                                  >
                                    <Tag color="geekblue" style={{ marginInlineEnd: 0 }}>参数含市场</Tag>
                                  </Tooltip>
                                ) : null}
                                <Text type="secondary" style={{ fontSize: 11 }}>
                                  {tool.wb ? `↔ ${tool.wb}` : "· 无工作台对应"}
                                </Text>
                                <Text type="secondary" style={{ fontSize: 11 }} title={`该工具在本服务进程内的调用次数${statText(toolCallsStat(tool.name)) === "—" ? "（metrics 未取到，显示 —，不是 0）" : ""}`}>
                                  {`今日 ${statText(toolCallsStat(tool.name))}`}
                                </Text>
                              </Space>
                              <Paragraph type="secondary" style={{ fontSize: 11.5, margin: "4px 0 0" }}>
                                {toolDesc(tool.desc) || "—"}
                              </Paragraph>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </ProCard>
                );
              })}
            </div>
          </Space>
        ) : null}
      </ProCard>

      <ProCard
        title="工具发现代理"
        bordered
        extra={<Space size={8} wrap>
          <ScopeTag market={market} label={label} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {`目录 ${catalog.total ?? "—"} 个工具 · Schema v3 · 调用 ${fmt.dash(m.mcp?.calls)} 次`}
          </Text>
        </Space>}
      >
        <Paragraph style={{ marginBottom: 8 }}>
          MCP 服务器只暴露 <Text code>list_tools</Text> / <Text code>call_tool</Text> 两个入口，
          避免上百个工具 schema 撑爆上下文窗口；Harness 先发现、再按名调用。发现代理为全局口径（工具面不按市场切分）。
        </Paragraph>
        <Descriptions size="small" column={2} items={[
          {
            key: "list",
            label: "list_tools",
            children: `${domainKeys.length} 个域 · ${catalog.total ?? "—"} 个工具（目录来自 /api/v3/tools）`,
          },
          {
            key: "call",
            label: "call_tool",
            children: `进程内已调用 ${fmt.dash(m.mcp?.calls)} 次（失败 ${fmt.dash(m.mcp?.errors)} 次）`,
          },
        ]} />
        <Space direction="vertical" size={8} style={{ width: "100%", marginTop: 10 }}>
          <Space size={8} wrap>
            <Button size="small" type="primary" loading={probe.state === "loading"} onClick={probeCall}>
              发起真实探测调用
            </Button>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {`只读工具 series（ticker=${probeTicker} · 当前市场 ${label} ${market}）· 仅在人工点击后发起一次`}
            </Text>
          </Space>
          {probe.state === "idle" ? (
            <Text type="secondary" style={{ fontSize: 12 }}>尚未发起探测：本页加载不写、不自动调用任何工具。</Text>
          ) : null}
          {probe.state === "loading" ? <Text type="secondary" style={{ fontSize: 12 }}>调用中…（15s 未返回按超时说明展示）</Text> : null}
          {probe.state === "error" ? (
            <NoSource what="探测调用响应" why={`${probe.error}（不展示任何构造的响应体）`} />
          ) : null}
          {probe.state === "ok" ? (
            <div>
              <Space size={8} wrap style={{ marginBottom: 4 }}>
                <Badge status="success" text={`已返回 · ${probe.elapsedMs}ms`} />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {`via ${fmt.dash(probe.body?.value?.source)} · as_of ${fmt.dash(probe.body?.value?.as_of)} · ${fmt.dash(probe.body?.value?.count)} 根 K 线`}
                </Text>
              </Space>
              <pre style={{
                margin: 0, padding: 10, borderRadius: 6, background: "#171c26", border: "1px solid #232b37",
                fontSize: 11, lineHeight: 1.55, maxHeight: 260, overflow: "auto", whiteSpace: "pre-wrap", wordBreak: "break-all",
              }}>
                {probeBodyText(probe.body)}
              </pre>
            </div>
          ) : null}
        </Space>
      </ProCard>

      <ProCard title="平台 MCP 服务器注册" bordered
        extra={<Space size={8} wrap>
          <ScopeTag market={market} label={label} />
          <Text type="secondary" style={{ fontSize: 12 }}>MCP 协议：{fmt.dash(gwChannels.mcp?.protocol)}</Text>
        </Space>}>
        <Descriptions size="small" column={2} items={[
          {
            key: "status",
            label: "MCP 通道状态",
            children: <Space size={6}>
              <Badge status={gwChannels.mcp?.status === "running" ? "success" : "error"} />
              <Text>{fmt.dash(gwChannels.mcp?.status)}</Text>
              <Text type="secondary" style={{ fontSize: 11 }}>来自 /api/v3/gateway</Text>
            </Space>,
          },
          {
            key: "tools",
            label: "已注册工具",
            children: `${fmt.dash(gwChannels.mcp?.tools ?? catalog.total)} 个`,
          },
          {
            key: "http",
            label: "HTTP 入口",
            children: <Text code>/mcp（streamable-http，本服务挂载）</Text>,
          },
          {
            key: "stdio",
            label: "stdio 入口",
            children: <Text><Text code>server/mcp/run.mjs</Text> · 本服务未挂载（本仓库无该文件）</Text>,
          },
          {
            key: "rest",
            label: "REST 注册入口",
            children: <Text><Text code>POST /api/v3/mcp</Text> · 本服务无该路由（POST 落在静态兜底路由上被 405 拒绝）</Text>,
          },
          {
            key: "adapter",
            label: "adapter 连接状态",
            children: <Text type="secondary">无数据源 · dsh-cordis-universal-adapter 未挂载（工具面不返回连接态）</Text>,
          },
        ]} />
        <div style={{ marginTop: 10, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12 }}>
          <NoSource what="服务进程信息" why="工具面不返回进程 PID / 启动时间 / 帧格式；本服务以 MCP streamable-http 暴露工具" />
          <NoSource what="最近一次 update 分段耗时" why="状态机只有 start/stop/update 动作，无耗时遥测（断开旧 / listTools / 注销旧 / 注册新四段未采集）" />
        </div>
        <div style={{ marginTop: 10 }}>
          <Text strong style={{ fontSize: 12 }}>可用数据源（/api/v3/settings · 仅列出 available=true 的项）</Text>
          <div style={{ marginTop: 6 }}>
            {registrySource.length === 0 ? (
              <Text type="secondary" style={{ fontSize: 12 }}>无数据源 · /api/v3/settings 未返回可用数据源</Text>
            ) : (
              <Space size={6} wrap>
                {registrySource.map((item) => (
                  <Tag key={item.name}>{String(item.name).split("（")[0]}</Tag>
                ))}
              </Space>
            )}
          </div>
        </div>
      </ProCard>

      <ProCard title="健康与降级" bordered
        extra={<Space size={8} wrap>
          <ScopeTag market={market} label={label} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            本服务进程内计数 · {`失败 ${fmt.dash(m.mcp?.errors)} / 调用 ${fmt.dash(m.mcp?.calls)}`}
          </Text>
        </Space>}>
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          口径：调用次数 / 失败次数 / 延迟为进程级全局口径（工具面不按市场切分），不按市场拆分。
        </Text>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 24 }}>
          <Statistic title="调用次数" value={statText(callsStat)} valueStyle={{ fontSize: 20 }} />
          <Statistic title="失败次数" value={statText(errorsStat)} valueStyle={{ fontSize: 20 }} />
          <Statistic title="平均延迟(ms)" value={statText(metricsStat(m.mcp?.avgMs, "/api/v3/metrics 未返回 mcp.avgMs"))} valueStyle={{ fontSize: 20 }} />
          <Statistic title="整体失败率" value={statText(failureRateStat)} valueStyle={{ fontSize: 20 }} />
          <Statistic title="workbench" value={m.workbenchUp === true ? "在线" : m.workbenchUp === false ? "不可达" : "—"} valueStyle={{ fontSize: 20 }} />
        </div>
        {metrics.loading || metrics.error || !metrics.value ? (
          <Alert
            style={{ marginTop: 10 }}
            type={metrics.error ? "error" : "warning"}
            showIcon
            message="健康计数暂无读数（显示「—」，不是 0）"
            description={
              <Text type="secondary" style={{ fontSize: 11.5 }}>
                {metrics.error
                  ? `/api/v3/metrics 取数失败：${metrics.error}`
                  : metrics.loading
                    ? "加载中…（/api/v3/metrics 未返回前不显示任何计数）"
                    : "接口返回体不含 ok=true 的 metrics 信封"}
              </Text>
            }
          />
        ) : null}
        {Object.keys(callsByTool).length > 0 ? (
          <div style={{ marginTop: 14 }}>
            <Text strong style={{ fontSize: 12 }}>按工具调用次数（真实计数）</Text>
            <div style={{ marginTop: 8 }}><BarList items={Object.entries(callsByTool)} /></div>
          </div>
        ) : null}
        <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 12 }}>
          <NoSource
            what="单工具失败率 Top 5"
            why={`metrics 只提供 errors 总数（${fmt.dash(m.mcp?.errors)} / ${fmt.dash(m.mcp?.calls)}）与按工具的调用次数，未按工具拆分错误数`}
          />
          <NoSource
            what="单工具 P95 延迟 Top 5"
            why={`metrics 只有全部调用的平均耗时（avgMs=${fmt.dash(m.mcp?.avgMs)}，样本 ${fmt.dash(m.mcp?.calls)} 次），未采集单工具 P95`}
          />
        </div>
        <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
          降级预案：连不上时工具静默降级为不可用，不影响其余能力（本页只展示真实计数，不做健康评分推算）。
          全局口径（工具面不按市场切分），与当前市场 {label} {market} 无关。
        </Paragraph>
      </ProCard>

      <ProCard title="契约说明" bordered extra={<ScopeTag market={market} label={label} />}>
        <Paragraph style={{ marginBottom: 0 }}>
          每个工具的契约（参数 / 输出 / 对齐规则）会注入系统提示词，因此工具数量需控制在合理范围 ——
          当前目录 <Text strong>{fmt.dash(catalog.total)}</Text> 个工具，长尾需求经 <Text code>call_tool</Text> 间接调用承接。
          工具面为全局口径（不按市场切分）：既含全局工具，也含按市场或带市场前缀代码取数的工具（已在上方逐条标注「参数含市场」）。
        </Paragraph>
      </ProCard>
    </Space>
  );
}
