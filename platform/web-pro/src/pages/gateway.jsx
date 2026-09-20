// V3 工作台「网关与调度」页（Ant Design Pro）。
// 功能模块与设计稿版 /v3/gateway.html（规格书 = public/v3/gateway.js）**一一对应**：
//   1. 三通道卡（MCP Bridge 真实计数；SDK JSON-RPC / Headless CLI 本服务未挂载 → 无数据源 + reason）
//   2. Headless 调用日志表（headless.last：时间/结果/exit/耗时/token；无记录 → 空态）
//   3. 调度器（daemon 心跳 + schedule 作业历史；定时规则表恒空 → 说明规则在 install/*.timer）
//   4. 熔断保护（并发/超时/token 预算/今日 token 本服务不自管 → 如实标注；kill/halt/critical 真实）
//   5. Profile 与 Bundle（工具面不返回 → 无数据源）
//   6. 调用分布（BarList ← /api/v3/metrics 的真实进程内计数）
// 数据：只走既有读通道 GET /api/v3/*，本页没有任何写动作。
import React from "react";
import { Alert, Badge, Descriptions, Empty, Select, Space, Statistic, Table, Tag, Typography } from "antd";
import { ProCard } from "@ant-design/pro-components";
import { fmt, noSourceText, useV3 } from "../services/api.js";
import { STAT, statNote, statNotes, statState, statText } from "../lib/stat-core.js";
import { industryGateView, probeAgeText } from "../lib/risk-labels.js";
import { useMarket } from "../services/marketContext.jsx";
import { BarList } from "../components/charts.jsx";

const { Text, Paragraph } = Typography;

/**
 * 口径标注（每个受市场影响的卡片都挂一句）：
 *   global=true  → 「全局口径，不按市场拆分」（三通道 / 熔断 / 调用分布 / 心跳）
 *   global=false → 「当前市场 {label} {market}」
 */
function ScopeTag({ market, label, global = false, text }) {
  const scope = text || (global ? "全局口径，不按市场拆分" : `当前市场 ${label} ${market}`);
  return (
    <Tag color={global ? "default" : "blue"} style={{ marginInlineEnd: 0 }}>
      {scope}
    </Tag>
  );
}

/** 作业市场的真实取值来自 job 前缀（GLOBAL:enqueue_research:2026-09-19 → GLOBAL）；无前缀显示「—」，不猜。 */
const MARKET_LABELS = { SH: "A股 SH", HK: "港股 HK", US: "美股 US", GLOBAL: "全局作业" };
const jobMarketOf = (record) => String(record?.job ?? "").split(":")[0] || "—";

/** 统一「无数据源」区块：措辞来自 api.js 的 noSourceText，页面不自造说法。 */
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

/** 取记录里第一个有值的字段（字段缺失显示「—」，绝不猜数值）。 */
function pick(record, keys) {
  for (const key of keys) {
    const value = record?.[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return undefined;
}

function channelTag(status) {
  if (status === "running") return <Tag color="success">运行中</Tag>;
  if (status === "unavailable") return <Tag color="warning">无数据源</Tag>;
  return <Tag>{fmt.dash(status)}</Tag>;
}

const REASON = {
  sdk: "SDK JSON-RPC 会话通道已实现（platform/server/v3_sdk.py）；后端未上报 channels.sdk.reason，故此处为兜底说明，真实状态见 GET /api/v3/sdk/status",
  headless: "Headless 通道已实现（platform/server/v3_headless.py：真实 spawn + 工具白名单 + 熔断 + headless_log 落库）；后端未上报 channels.headless.reason，故此处为兜底说明，真实状态见 GET /api/v3/headless/schedule",
};

/** 通道卡里的「指标 + 值」小块（对应设计稿 .ch-metrics .m / .v / .l）。 */
function Stat({ label, value, suffix, hint }) {
  return (
    <div style={{ minWidth: 108 }}>
      <Statistic
        title={label}
        value={value}
        suffix={suffix ? <span style={{ fontSize: 12 }}>{suffix}</span> : undefined}
        valueStyle={{ fontSize: 18, fontVariantNumeric: "tabular-nums" }}
      />
      {hint ? <Text type="secondary" style={{ fontSize: 11 }}>{hint}</Text> : null}
    </div>
  );
}

/**
 * 三通道之一：MCP Bridge（真实运行态与计数）。
 *
 * 计数三态（数字诚实性）：`/api/v3/metrics` 加载中/取失败时**不得**显示 0——
 * `0` 只能表示「接口确实返回 0」。因此走 stat-core 的四态判定，
 * 非真实读数一律渲染 `—` 并在卡内列出原因（`metricsEnv` 传 useV3 的返回对象）。
 */
function McpChannelCard({ gateway, metrics, metricsEnv }) {
  const mcp = gateway.channels?.mcp ?? {};
  const metricsState = metricsEnv ?? {};
  const callsStat = statState(metricsState, metrics.mcp?.calls, {
    missingReason: "/api/v3/metrics 未返回 mcp.calls",
  });
  const errorsStat = statState(metricsState, metrics.mcp?.errors, {
    missingReason: "/api/v3/metrics 未返回 mcp.errors",
  });
  const avgStat = statState(metricsState, metrics.mcp?.avgMs, {
    missingReason: "/api/v3/metrics 未返回 mcp.avgMs",
  });
  const calls = callsStat.state === STAT.VALUE ? Number(callsStat.value) : null;
  const errors = errorsStat.state === STAT.VALUE ? Number(errorsStat.value) : null;
  const successRate = calls !== null && calls > 0 ? `${(((calls - errors) / calls) * 100).toFixed(1)}%` : "—";
  const notes = statNotes([["今日调用", callsStat], ["失败次数", errorsStat], ["平均延迟", avgStat]]);
  return (
    <ProCard
      title={<Space size={8}>MCP Bridge {channelTag(mcp.status)}</Space>}
      bordered
      extra={<Text type="secondary" style={{ fontSize: 11 }}>MCP 工具面 {fmt.dash(mcp.tools_total ?? mcp.tools ?? metrics.mcpToolTotal)} 个</Text>}
    >
      <Descriptions size="small" column={1} items={[
        { key: "protocol", label: "协议标识", children: fmt.dash(mcp.protocol) },
      ]} />
      <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginTop: 10 }}>
        <Stat
          label="MCP 工具面"
          value={fmt.dash(mcp.tools_total ?? mcp.tools ?? metrics.mcpToolTotal)}
          suffix="个"
          hint={mcp.tools_source ? `来源：${mcp.tools_source}` : "与 MCP tools/list 同源"}
        />
        <Stat
          label="平台工具目录"
          value={fmt.dash(mcp.tools_domain_catalog ?? metrics.toolTotal)}
          suffix="个"
          hint="六域目录口径，与 MCP 工具面不是同一个数"
        />
        <Stat
          label="今日调用"
          value={statText(callsStat)}
          suffix={callsStat.state === STAT.VALUE ? "次" : undefined}
          hint={statNote(callsStat) ?? "进程内计数"}
        />
        <Stat
          label="成功率"
          value={successRate}
          hint={errorsStat.state === STAT.VALUE ? `失败 ${errors} 次` : statNote(errorsStat)}
        />
        <Stat
          label="平均延迟"
          value={statText(avgStat)}
          suffix={avgStat.state === STAT.VALUE ? "ms" : undefined}
          hint={statNote(avgStat) ?? "metrics 无 P95 口径"}
        />
      </div>
      {notes.length > 0 ? (
        <Alert
          style={{ marginTop: 10 }}
          type={metricsState.error ? "error" : "warning"}
          showIcon
          message={`运行计数暂无读数（不是 0） · ${notes.join("；")}`}
        />
      ) : null}
      <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
        MCP 服务端状态机只有 start / stop / update 三个动作，没有状态查询端点；
        此处按 <Text code>{`channels.mcp.status=${fmt.dash(mcp.status)}`}</Text> 标注。
      </Paragraph>
    </ProCard>
  );
}

/** 三通道之一：本服务未挂载的通道（SDK / Headless），整卡标无数据源。 */
function UnavailableChannelCard({ title, channel, reason, extraRows = [], footer }) {
  const kvs = [
    {
      key: "protocol",
      label: "协议标识",
      children: channel?.protocol
        ? fmt.dash(channel.protocol)
        : <Text type="secondary">无数据源（工具面未返回该通道协议标识）</Text>,
    },
    ...extraRows,
  ];
  return (
    <ProCard title={<Space size={8}>{title} {channelTag(channel?.status)}</Space>} bordered>
      <Descriptions size="small" column={1} items={kvs} />
      <div style={{ marginTop: 10 }}>
        <NoSource what={`${title} 运行指标`} why={reason ?? channel?.reason} />
      </div>
      {footer ? (
        <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>{footer}</Paragraph>
      ) : null}
      <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 6, marginBottom: 0 }}>
        状态 <Text code>{fmt.dash(channel?.status)}</Text> · 本页不保留任何占位数字
      </Paragraph>
    </ProCard>
  );
}

/**
 * 告警三态（+ 两个必须分清的中间态）的配色与文案。
 *
 * 取自 ``GET /api/v3/ops/alerts`` 的 ``state`` 字段，**不在这里重新判定**：
 *   * firing      表达式为真且已满 ``for`` → 红（error）
 *   * pending     条件为真但 ``for`` 未满 → 橙（warning）
 *   * no-data     指标不存在 / 窗口历史不足 → **中性灰**（default）：看不到 ≠ 正常
 *   * unsupported 表达式超出平台内求值器的 PromQL 子集 → 紫（purple）
 *   * ok          判定过且不满足 → 绿（success）
 */
const ALERT_STATE_META = {
  firing: { color: "error", label: "触发" },
  pending: { color: "warning", label: "待满 for" },
  "no-data": { color: "default", label: "无法判定" },
  unsupported: { color: "purple", label: "表达式不支持" },
  ok: { color: "success", label: "正常" },
};

const alertStateMeta = (state) => ALERT_STATE_META[state] ?? { color: "default", label: fmt.dash(state) };

/** 告警的「当前值 / 阈值」：没有读数就是「—」，绝不显示 0（0 只表示接口确实返回 0）。 */
function AlertValue({ item, field }) {
  const value = item?.[field];
  if (value === null || value === undefined || value === "") {
    return <Text type="secondary">—</Text>;
  }
  if (typeof value === "object") {
    return <Text type="secondary">{JSON.stringify(value)}</Text>;
  }
  return <Text>{typeof value === "number" ? Number(value.toPrecision(6)) : String(value)}</Text>;
}

/**
 * 运维告警卡：**平台内求值，不是 Grafana**。
 *
 * 数据来自 ``GET /api/v3/ops/alerts``（``server/v3_alerts.py``）：它解析
 * ``deploy/monitoring/alerts.yml``（与 Prometheus 同一份规则文件）后在进程内对 ``/metrics``
 * 求值。本机未部署 Grafana/Prometheus，所以这里没有 Grafana 面板可嵌——本卡片就是替代面。
 */
function AlertsCard({ env }) {
  const data = env?.value ?? {};
  const items = Array.isArray(data.alerts) ? data.alerts : [];
  const [stateFilter, setStateFilter] = React.useState("ALL");
  const summary = data.summary ?? {};
  const states = Array.from(new Set(items.map((item) => String(item.state ?? "")))).filter(Boolean);
  const visible = stateFilter === "ALL" ? items : items.filter((item) => item.state === stateFilter);
  // 默认只看需要人处理的三态（firing / pending / no-data / unsupported），ok 收在筛选里
  const attention = items.filter((item) => item.state !== "ok").length;
  const options = [
    { value: "ALL", label: `全部（${items.length} 条）` },
    ...states.map((state) => ({
      value: state,
      label: `${alertStateMeta(state).label}（${items.filter((row) => row.state === state).length} 条）`,
    })),
  ];
  return (
    <ProCard
      title="运维告警"
      bordered
      extra={
        <Space size={8} wrap>
          <Tag color={summary.firing ? "error" : "default"}>{`firing ${fmt.dash(summary.firing)}`}</Tag>
          <Tag color={summary.pending ? "warning" : "default"}>{`pending ${fmt.dash(summary.pending)}`}</Tag>
          <Tag>{`no-data ${fmt.dash(summary["no-data"])}`}</Tag>
          {summary.unsupported ? <Tag color="purple">{`unsupported ${fmt.dash(summary.unsupported)}`}</Tag> : null}
          <Tag color="success">{`ok ${fmt.dash(summary.ok)}`}</Tag>
          <ScopeTag market={undefined} label={undefined} global />
        </Space>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 10 }}
        message="数据来自平台内规则求值器（GET /api/v3/ops/alerts），不是 Grafana"
        description={
          <Text type="secondary" style={{ fontSize: 11.5 }}>
            求值器（<Text code>server/v3_alerts.py</Text>）解析 <Text code>platform/deploy/monitoring/alerts.yml</Text>
            （与 Prometheus 同一份规则文件）后，在本服务进程内对 <Text code>/metrics</Text> 求值；
            本机未部署 Grafana/Prometheus，因此没有 Grafana 面板可嵌，本卡片就是轻量替代面。
            三态语义：<Text code>firing</Text> 红 / <Text code>pending</Text> 橙（条件为真但 for 未满）/
            <Text code>no-data</Text> 中性灰（指标缺失或窗口历史不足——<Text strong>看不到不等于正常</Text>）/
            <Text code>unsupported</Text> 紫（表达式超出求值器支持的 PromQL 子集，显式报出，不当 ok）。
            规则文件：<Text code>{fmt.dash(data.rules_file)}</Text> · 求值时刻 {fmt.stamp(data.as_of)}
          </Text>
        }
      />
      {env?.loading && !env?.value ? <Text type="secondary">读取中…（未取到前不显示任何计数）</Text> : null}
      {env?.error ? (
        <NoSource what="运维告警" why={`/api/v3/ops/alerts 取数失败：${env.error}`} type="error" />
      ) : null}
      {!env?.loading && !env?.error && env?.value && data.ok !== true ? (
        <NoSource
          what="运维告警"
          why={data.ok === false
            ? `求值器返回失败信封：${data.error?.code ?? "unknown"} · ${data.error?.message ?? "无原因"}`
            : "接口没有返回有效信封（端点可能尚未注册——运行中的服务进程需要重启一次才会有 /api/v3/ops/alerts）"}
          type={data.ok === false ? "error" : "warning"}
        />
      ) : null}
      {!env?.loading && !env?.error && env?.value && data.ok === true && items.length === 0 ? (
        <Empty image={EMPTY_FRAME} description={noSourceText("运维告警", "alerts 为空数组（规则文件里没有规则？）")} />
      ) : null}
      {items.length > 0 ? (
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          <Space size={8} wrap style={{ justifyContent: "space-between", width: "100%" }}>
            <Text type="secondary" style={{ fontSize: 11.5 }}>
              {`需要人看的有 ${attention} 条（firing / pending / no-data / unsupported）；表内按状态与严重度排序，来源为平台内求值`}
            </Text>
            <Select size="small" style={{ minWidth: 190 }} value={stateFilter} onChange={setStateFilter} options={options} />
          </Space>
          <Table
            size="small"
            rowKey={(record) => String(record.rule ?? record.name)}
            dataSource={visible.slice(0, 20)}
            pagination={false}
            locale={emptyTable("运维告警", `没有状态为 ${stateFilter} 的规则`)}
            expandable={{
              expandedRowRender: (record) => (
                <Space direction="vertical" size={4} style={{ width: "100%" }}>
                  <Text code style={{ fontSize: 11.5 }}>{fmt.dash(record.expr)}</Text>
                  <Text type="secondary" style={{ fontSize: 11.5 }}>
                    {`证据：${fmt.dash(record.evidence?.reason)}`}
                  </Text>
                  {record.thresholds?.length > 1 ? (
                    <Text type="secondary" style={{ fontSize: 11.5 }}>
                      {`表达式里全部阈值：${record.thresholds
                        .map((row) => `${row.metric ?? "（无指标前缀）"} ${row.operator} ${row.threshold}`)
                        .join(" · ")}`}
                    </Text>
                  ) : null}
                </Space>
              ),
            }}
            columns={[
              {
                title: "规则",
                render: (_, record) => (
                  <Space direction="vertical" size={0}>
                    <Text strong style={{ fontSize: 12 }}>{fmt.dash(record.rule)}</Text>
                    <Text type="secondary" style={{ fontSize: 11 }}>{fmt.dash(record.summary)}</Text>
                  </Space>
                ),
              },
              {
                title: "严重度",
                width: 90,
                render: (_, record) => (
                  <Tag color={record.severity === "critical" ? "error" : record.severity === "warning" ? "warning" : "default"}>
                    {fmt.dash(record.severity)}
                  </Tag>
                ),
              },
              {
                title: "状态",
                width: 108,
                render: (_, record) => <Tag color={alertStateMeta(record.state).color}>{alertStateMeta(record.state).label}</Tag>,
              },
              { title: "当前值", width: 110, render: (_, record) => <AlertValue item={record} field="value" /> },
              { title: "阈值", width: 100, render: (_, record) => <AlertValue item={record} field="threshold" /> },
              {
                // 只显示比较运算符；完整表达式在展开行（列宽放不下，且表达式本身是证据不是结论）
                title: "运算符",
                width: 82,
                render: (_, record) => <Text type="secondary" style={{ fontSize: 11 }}>{fmt.dash(record.operator)}</Text>,
              },
              { title: "since", width: 170, render: (_, record) => fmt.stamp(record.since) },
              {
                title: "影响范围",
                width: 96,
                render: (_, record) => (
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {record.state === "no-data" ? "无法判定" : record.series?.length ? `${record.series.length} 条序列` : "—"}
                  </Text>
                ),
              },
            ]}
          />
        </Space>
      ) : null}
    </ProCard>
  );
}

export default function GatewayPage() {
  const { market, label } = useMarket();
  const gateway = useV3("gateway");
  const metrics = useV3("metrics");
  // 运维告警（平台内求值：GET /api/v3/ops/alerts；不是 Grafana 面板）
  const opsAlerts = useV3("ops/alerts");
  // 调度作业历史的市场筛选（job 前缀；默认「全部市场」，纯前端过滤，不新增请求）
  const [jobMarket, setJobMarket] = React.useState("ALL");
  const g = gateway.value ?? {};
  const m = metrics.value ?? {};

  const channels = g.channels ?? {};
  const statuses = ["mcp", "sdk", "headless"].map((key) => String(channels?.[key]?.status ?? ""));
  const upCount = statuses.filter((value) => value === "running").length;

  const scheduler = g.scheduler ?? {};
  const heartbeat = scheduler.heartbeat ?? {};
  const jobs = Array.isArray(scheduler.recent) ? scheduler.recent : [];
  const rules = Array.isArray(scheduler.rules) ? scheduler.rules : [];
  const headlessLast = Array.isArray(g.headless?.last) ? g.headless.last : [];
  const today = g.headless?.today ?? {};
  const tripped = Boolean(scheduler.kill || scheduler.halt || scheduler.critical);
  const toolCalls = Object.entries(m.mcp?.tools ?? {}).filter(([, value]) => Number.isFinite(Number(value)));
  // 行业红线下单前闸门（/api/v3/metrics.industryGate）：failOpen=true 表示当前不阻断
  const gateView = industryGateView(metrics.value?.industryGate);

  /* ── 作业市场分布 / 筛选（真实字段：job 前缀，无则不造） ── */
  const countByMarket = (key) => jobs.filter((record) => jobMarketOf(record) === key).length;
  const jobMarkets = Array.from(new Set(jobs.map(jobMarketOf))).filter((key) => key && key !== "—").sort();
  const visibleJobs = jobMarket === "ALL" ? jobs : jobs.filter((record) => jobMarketOf(record) === jobMarket);
  const jobMarketOptions = [
    { value: "ALL", label: `全部市场（${jobs.length} 条）` },
    ...jobMarkets.map((key) => ({
      value: key,
      label: `${MARKET_LABELS[key] ?? key}${key === market ? "（本页当前市场）" : ""} · ${countByMarket(key)} 条`,
    })),
  ];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <ProCard
        title="网关与调度"
        bordered
        extra={
          <Space size={8} wrap>
            <Badge
              status={upCount === 3 ? "success" : upCount === 0 ? "error" : "warning"}
              text={`${upCount} / 3 通道可用（MCP ${fmt.dash(statuses[0])} · SDK ${fmt.dash(statuses[1])} · Headless ${fmt.dash(statuses[2])}）`}
            />
            <ScopeTag market={market} label={label} global />
            <Text type="secondary" style={{ fontSize: 12 }}>数据时点 {fmt.stamp(g.generated_at)}</Text>
          </Space>
        }
      >
        {gateway.loading ? <Text type="secondary">读取中…</Text> : null}
        {gateway.error ? (
          <NoSource what="网关与调度" why={`/api/v3/gateway 取数失败：${gateway.error}`} type="error" />
        ) : null}
        {!gateway.loading && !gateway.error ? (
          <Descriptions size="small" column={2} items={[
            { key: "src", label: "数据来源", children: <Text code>/api/v3/gateway · /api/v3/metrics</Text> },
            { key: "scope", label: "市场口径", children: `三通道 / 熔断 / 调用分布为全局口径（不按市场拆分）；调度作业带真实市场前缀，可按市场筛选（本页当前市场 ${label} ${market}）` },
            { key: "hint", label: "通道语义", children: "MCP Bridge 由本服务挂载；SDK JSON-RPC 与 Headless CLI 未挂载（如实标注，不占位）" },
          ]} />
        ) : null}
      </ProCard>

      <Text type="secondary" style={{ fontSize: 11 }}>
        口径：三通道卡为进程级全局口径（不按市场拆分），与当前市场 {label} {market} 无关；市场只影响下方调度作业历史的筛选视图。
      </Text>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 12 }}>
        <McpChannelCard gateway={g} metrics={m} metricsEnv={metrics} />
        <UnavailableChannelCard
          title="SDK JSON-RPC"
          channel={channels.sdk}
          reason={channels.sdk?.reason ?? REASON.sdk}
          extraRows={[{
            key: "session",
            label: "活跃会话 / 初始化握手",
            children: <Text type="secondary">无数据源（本服务未挂载 SDK 通道）</Text>,
          }]}
          footer="本服务没有 dsh SDK 会话，因此没有 session / prompt 事件可展示。"
        />
        <UnavailableChannelCard
          title="Headless CLI"
          channel={channels.headless}
          reason={channels.headless?.reason ?? REASON.headless}
          extraRows={[{
            key: "command",
            label: "命令形态",
            children: <Text code>{fmt.dash(channels.headless?.command)}</Text>,
          }, {
            key: "wake",
            label: "今日唤醒 / 成功 / 失败 / 平均耗时",
            children: (
              <Text type="secondary">
                {`唤醒 ${fmt.dash(today.total)} · 成功 ${fmt.dash(today.success)} · 失败 ${fmt.dash(today.failed)} · 平均 ${fmt.dash(today.avgMs)}ms`}
                {`（来源：headless_log 当日真实记录，GET /api/v3/headless/log；全 0 表示当日无唤醒，不是"不拉起子进程"）`}
              </Text>
            ),
          }]}
        />
      </div>

      <AlertsCard env={opsAlerts} />

      <ProCard
        title="Headless 调用日志"
        bordered
        extra={<Space size={8} wrap>
          <ScopeTag market={market} label={label} global />
          {headlessLast.length === 0
            ? <Tag color="warning">无数据源 · 本服务未挂载 Headless 子进程</Tag>
            : <Tag color="success">{`${headlessLast.length} 条`}</Tag>}
        </Space>}
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          口径：Headless 通道为进程级全局口径（不按市场拆分）；「市场」列取记录里真实返回的 market 字段，没有就显示「—」，不推断。
        </Text>
        {headlessLast.length === 0 ? (
          <Empty
            image={EMPTY_FRAME}
            description={noSourceText(
              "Headless 调用日志",
              `${g.headless?.reason ?? REASON.headless}；channels.headless.status=${fmt.dash(channels.headless?.status)}，无 exit_code / stdout / token 记录`,
            )}
          />
        ) : (
          <Table
            size="small"
            rowKey={(record, index) => String(pick(record, ["id", "at", "time"]) ?? index)}
            dataSource={headlessLast}
            pagination={false}
            locale={emptyTable("Headless 调用日志", "headless.last 为空数组")}
            columns={[
              { title: "时间", render: (_, record) => fmt.stamp(pick(record, ["at", "time", "started_at"])) },
              { title: "市场", width: 100, render: (_, record) => fmt.dash(pick(record, ["market", "market_group"])) },
              { title: "结果", render: (_, record) => fmt.dash(pick(record, ["result", "status", "ok"])) },
              { title: "exit", render: (_, record) => fmt.dash(pick(record, ["exit_code", "exitCode", "code"])) },
              { title: "耗时(ms)", render: (_, record) => fmt.dash(pick(record, ["durationMs", "ms", "elapsed_ms"])) },
              { title: "token", render: (_, record) => fmt.dash(pick(record, ["tokens", "token_usage", "tokens_total"])) },
            ]}
          />
        )}
      </ProCard>

      <ProCard
        title="调度器"
        bordered
        extra={<Space size={8} wrap>
          <ScopeTag market={market} label={label} global />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {`定时规则 ${rules.length} 条（工具面不提供）· 作业历史 ${jobs.length} 条 · 心跳 ${fmt.dash(heartbeat.heartbeat)}`}
          </Text>
        </Space>}
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          {`口径：调度器与 daemon 心跳为全局调度台账（不按市场拆分）；每条作业带真实市场前缀（SH / HK / US / GLOBAL），
          下面按该前缀筛选（纯前端过滤，不新增请求）。本页当前市场 ${label} ${market}。`}
        </Text>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 12 }}>
          <div>
            <Text strong>daemon 心跳 · 实时</Text>
            <Descriptions size="small" column={1} style={{ marginTop: 8 }} items={[
              { key: "hb", label: "最近心跳", children: fmt.dash(heartbeat.heartbeat) },
              { key: "last", label: "最近一次作业", children: fmt.dash(heartbeat.last_job) },
              { key: "next", label: "下次触发", children: fmt.dash(heartbeat.next) },
            ]} />
            <Space size={6} wrap style={{ marginTop: 8 }}>
              <Tag color={scheduler.kill ? "error" : "default"}>{`kill ${scheduler.kill ? "true" : "false"}`}</Tag>
              <Tag color={scheduler.halt ? "error" : "default"}>{`halt ${scheduler.halt ? "true" : "false"}`}</Tag>
              <Tag color={scheduler.critical ? "error" : "default"}>{`critical ${scheduler.critical ? "true" : "false"}`}</Tag>
            </Space>
          </div>
          <div>
            <Space size={8} wrap style={{ justifyContent: "space-between", width: "100%" }}>
              <Text strong>{`作业历史 · 最近 ${Math.min(visibleJobs.length, 8)} 次`}</Text>
              <Select size="small" style={{ minWidth: 220 }} value={jobMarket} onChange={setJobMarket} options={jobMarketOptions} />
            </Space>
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 6, fontVariantNumeric: "tabular-nums" }}>
              {jobMarkets.length === 0
                ? "作业历史未返回可识别的市场前缀（job 字段）"
                : `市场分布：${jobMarkets.map((key) => `${key} ${countByMarket(key)}`).join(" · ")} · 来源 schedule.jobs 的 job 前缀`}
            </Text>
            <div style={{ marginTop: 8 }}>
              {jobs.length === 0 ? (
                <Empty image={EMPTY_FRAME} description={noSourceText("作业历史", "schedule 工具未返回 jobs")} />
              ) : visibleJobs.length === 0 ? (
                <Empty
                  image={EMPTY_FRAME}
                  description={noSourceText(`作业历史（市场 ${jobMarket}）`, `schedule.jobs 中没有 job 前缀为 ${jobMarket} 的作业`)}
                />
              ) : (
                <Table
                  size="small"
                  rowKey={(record, index) => `${record.job ?? "job"}-${index}`}
                  dataSource={visibleJobs.slice(0, 8)}
                  pagination={false}
                  locale={emptyTable("作业历史", "schedule 工具未返回 jobs")}
                  columns={[
                    { title: "作业", render: (_, record) => String(record.job ?? "—").split(":").slice(1).join(":") || String(record.job ?? "—") },
                    {
                      title: "市场",
                      width: 110,
                      render: (_, record) => (
                        <Space size={4}>
                          <Text style={{ fontSize: 12 }}>{jobMarketOf(record)}</Text>
                          {jobMarketOf(record) === market ? <Tag color="blue" style={{ marginInlineEnd: 0 }}>当前市场</Tag> : null}
                        </Space>
                      ),
                    },
                    { title: "运行于", width: 170, render: (_, record) => fmt.dash(record.ran) },
                  ]}
                />
              )}
            </div>
          </div>
          <div>
            <Text strong>定时规则</Text>
            <div style={{ marginTop: 8 }}>
              <NoSource
                what="定时规则表"
                why="规则定义在 install/*.timer 与配置里，不在工具面（scheduler.rules 恒为空数组）"
              />
            </div>
            {scheduler.note ? (
              <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 8, marginBottom: 0 }}>{scheduler.note}</Paragraph>
            ) : null}
            <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 8, marginBottom: 0 }}>
              本页调度开关不持久化：工具面没有可写规则表，规则改动只在 install/*.timer 与配置文件里完成。
              定时规则为全局口径（不按市场拆分），各市场执行时刻由「接入与授权」页的自动流水线配置。
            </Paragraph>
          </div>
        </div>
      </ProCard>

      <ProCard
        title="熔断保护"
        bordered
        extra={
          <Space size={8} wrap>
            <Tag color={tripped ? "error" : "success"}>{tripped ? "熔断已触发" : "未触发 · 保护待命"}</Tag>
            <ScopeTag market={market} label={label} global />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {`服务端 kill=${scheduler.kill ? "true" : "false"} · halt=${scheduler.halt ? "true" : "false"} · critical=${scheduler.critical ? "true" : "false"}`}
            </Text>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          口径：熔断保护为进程级全局口径（不按市场拆分），一个开关对所有市场生效。
        </Text>
        {/* 行业红线下单前闸门的 fail-open 窗口：这是熔断/保护面的真实缺口，必须可见 */}
        <Alert
          type={gateView.available && gateView.failOpen ? "warning" : "info"}
          showIcon
          style={{ marginBottom: 10 }}
          message={gateView.available
            ? (gateView.failOpen
              ? "行业红线下单前闸门：当前 fail-open（未参与阻断）"
              : `行业红线下单前闸门：已生效 · 当前读数 ${fmt.pct(gateView.pct, 2)}（${gateView.top || "—"}）· 上限 ${fmt.pct(gateView.limitPct, 0)}`)
            : "行业红线下单前闸门：读数不可用（未取得 industryGate）"}
          description={
            <Text type="secondary" style={{ fontSize: 11.5 }}>
              {gateView.available && !gateView.failOpen
                ? `来源 ${gateView.source || "—"} · as_of ${fmt.stamp(gateView.asOf)} · 探测年龄 ${probeAgeText(gateView.probeAgeMs)} · 单一行业暴露 > 上限 → stage=blocked_industry（risk.rule=industry-red-line）强制阻断`
                : gateView.reason}
            </Text>
          }
        />
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
          <NoSource what="并发上限" why="Headless 子进程不自管并发位，工具面无该阈值" />
          <NoSource what="单次超时" why="无 Headless 子进程调度，无超时配置来源" />
          <NoSource what="单次 token 预算" why="本服务不消耗模型 token，无预算来源" />
          <NoSource what="今日累计 token" why="本服务不消耗模型 token，无用量来源" />
        </div>
        <Alert
          style={{ marginTop: 12 }}
          type="warning"
          showIcon
          message={`强制 kill 记录：无数据源 · ${g.headless?.reason ?? REASON.headless}（headless.today.killed=${fmt.dash(today.killed)}）`}
        />
      </ProCard>

      <ProCard
        title="Profile 与 Bundle"
        bordered
        extra={<ScopeTag market={market} label={label} global />}
      >
        <NoSource
          what="Profile 与 Bundle"
          why="工具面不返回 bundle 的名称 / 版本 / 条目数 / 加载状态；平台按角色运行，不使用 dsh profile"
        />
        <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
          本页不保留任何占位条目；可用 bundle 清单见 Harness 侧 cordis.yml。Profile / Bundle 为全局口径（不按市场拆分）。
        </Paragraph>
      </ProCard>

      <ProCard
        title="调用分布"
        bordered
        extra={<Space size={8} wrap>
          <ScopeTag market={market} label={label} global />
          <Text type="secondary" style={{ fontSize: 12 }}>来源 /api/v3/metrics · 本服务进程内计数</Text>
        </Space>}
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          口径：调用分布为进程内计数（全局口径，不按市场拆分）——同一 handle 同时服务各市场工具。
        </Text>
        {metrics.error ? (
          <NoSource what="调用分布" why={`/api/v3/metrics 取数失败：${metrics.error}`} type="error" />
        ) : metrics.loading && !metrics.value ? (
          <Text type="secondary">读取中…（/api/v3/metrics 未返回前不显示任何计数，避免把「未取到」读成 0）</Text>
        ) : toolCalls.length === 0 ? (
          <Empty
            image={EMPTY_FRAME}
            description={noSourceText("工具调用分布", "本服务进程内暂无调用计数（metrics.mcp.tools 为空对象）")}
          />
        ) : (
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            <BarList items={toolCalls} />
            <Descriptions size="small" column={3} items={[
              { key: "calls", label: "调用总数", children: `${fmt.dash(m.mcp?.calls)} 次` },
              { key: "errors", label: "失败次数", children: `${fmt.dash(m.mcp?.errors)} 次` },
              { key: "avg", label: "平均耗时", children: `${fmt.dash(m.mcp?.avgMs)} ms` },
              { key: "http", label: "HTTP 请求", children: `${fmt.dash(m.http?.requests)} 次（错误 ${fmt.dash(m.http?.errors)}）` },
              { key: "wb", label: "工作台调用", children: `${fmt.dash(m.wb?.calls)} 次（错误 ${fmt.dash(m.wb?.errors)}）` },
              { key: "oms", label: "OMS 台账分级", children: Object.entries(m.oms ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—" },
            ]} />
          </Space>
        )}
      </ProCard>
    </Space>
  );
}
