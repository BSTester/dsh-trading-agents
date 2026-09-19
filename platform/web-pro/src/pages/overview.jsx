// V3 工作台「系统概览」页（Ant Design Pro）。
// 功能模块与设计稿版 /v3/index.html **一一对应**（binder: platform/web/public/v3/index.js）：
//   ① KPI 5 张（总资产 / 当日盈亏 / 年化收益 / 夏普比率 / 最大回撤）
//   ② 三通道状态 3 卡（MCP Bridge / SDK JSON-RPC / Headless CLI）
//   ③ 决策链路时间线（研究流水线产出 + 审计链 + Headless last）
//   ④ 风控红线 3 条（单笔上限 2% / 单一行业暴露 20% / 最大回撤阈值 15%）
//   ⑤ Agent Loop 实时状态（turn/token/模型/推理强度 + 工具调用 Top5）
//   ⑥ 待人工审批（oms/orders stage=manual 真实订单）
//   ⑦ 数据源健康（workbench / 富途 / AKShare / SEC EDGAR / Tushare 真实探测）
// 数据：GET /api/v3/*；取不到显式「无数据源 + 原因」，页面不含任何占位数字。
import React from "react";
import {
  Alert,
  Badge,
  Card,
  Descriptions,
  Divider,
  Empty,
  Progress,
  Space,
  Statistic,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { fmt, noSourceText, useV3 } from "../services/api.js";
import { BarList, LineChart } from "../components/charts.jsx";

const { Text, Title } = Typography;

// 与设计稿 /v3/ 同一套 token（绿升红跌）
const C = {
  up: "#3fb950",
  down: "#f8514d",
  amber: "#d9a112",
  blue: "#4c8dff",
  muted: "#8b97a5",
  faint: "#626d7c",
};
const MONO = { fontVariantNumeric: "tabular-nums" };

// fmt.num 走 Number()，null/'' 会被算成 0；缺失值必须显式写「—」，不能变成 0.00
const numOr = (value, digits = 2) =>
  value === null || value === undefined || value === "" || !Number.isFinite(Number(value))
    ? "—"
    : Number(value).toFixed(digits);

function errText(payload, fallback = "接口未返回原因") {
  const error = (payload && payload.error) || {};
  return String(error.message || error.code || fallback);
}

/** 无数据源区块：统一走 noSourceText（是什么 + 为什么没有），绝不留占位数字 */
function NoSource({ what, why }) {
  return (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={<Text type="secondary">{noSourceText(what, why)}</Text>}
    />
  );
}

/** 区块级取数失败：只影响本块，不白屏 */
function BlockError({ name, error }) {
  return <Alert type="error" showIcon message={`${name}：取数失败`} description={String(error)} />;
}

function Chip({ ok, okText = "运行中", badText = "无数据源" }) {
  return (
    <Tag color={ok ? "success" : "warning"} style={{ marginInlineEnd: 0 }}>
      {ok ? okText : badText}
    </Tag>
  );
}

/** 单条通道卡（设计稿 .chans .card 形态：名称 + 副标题 + 状态 chip + stat 栅格 + foot） */
function ChannelCard({ name, subtitle, chip, stats, foot, loading }) {
  return (
    <Card
      size="small"
      loading={loading}
      title={
        <Space size={8}>
          <span style={{ fontWeight: 600 }}>{name}</span>
          <Text type="secondary" style={{ fontSize: 11, fontWeight: 400 }}>
            {subtitle}
          </Text>
        </Space>
      }
      extra={chip}
    >
      {stats}
      {foot ? (
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 10 }}>
          {foot}
        </Text>
      ) : null}
    </Card>
  );
}

function KpiCard({ label, value, tone, sub, spark }) {
  return (
    <Card size="small" styles={{ body: { padding: 14 } }}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Text>
      <div style={{ marginTop: 2 }}>
        <Statistic
          value={value}
          valueStyle={{
            ...MONO,
            fontSize: 20,
            fontWeight: 600,
            color: tone === "up" ? C.up : tone === "down" ? C.down : tone === "amber" ? C.amber : undefined,
          }}
        />
      </div>
      {spark ? <div style={{ marginTop: 4 }}>{spark}</div> : null}
      <Text type="secondary" style={{ fontSize: 11 }}>
        {sub}
      </Text>
    </Card>
  );
}

export default function OverviewPage() {
  const overview = useV3("overview", {});
  const metrics = useV3("metrics", {});
  const brain = useV3("brain", {});
  const settings = useV3("settings", {});
  const orders = useV3("oms/orders", {});
  const strategy = useV3("strategy", {});
  const audit = useV3("audit", { window: 120 });

  const ov = overview.value && overview.value.ok ? overview.value : null;
  const mt = metrics.value && metrics.value.ok ? metrics.value : null;
  const br = brain.value && brain.value.ok ? brain.value : null;
  const st = settings.value && settings.value.ok ? settings.value : null;
  const od = orders.value && orders.value.ok ? orders.value : null;
  const auditRows =
    (audit.value && audit.value.ok && audit.value.data && audit.value.data.entries) || [];

  const refreshAll = () => {
    overview.refresh();
    metrics.refresh();
    brain.refresh();
    settings.refresh();
    orders.refresh();
    strategy.refresh();
    audit.refresh();
  };

  /* ── ① KPI：全部来自 overview.equity（台账权益 / 回撤 / 夏普 / 成交笔数） ── */
  const equity = (ov && ov.equity) || {};
  const points = Array.isArray(equity.points) ? equity.points : [];
  const last = points.length ? points[points.length - 1] : null;
  const prev = points.length > 1 ? points[points.length - 2] : null;
  const hasPrev = Boolean(last && prev && Number(prev.equity));
  const dailyPct = hasPrev ? (Number(last.equity) / Number(prev.equity) - 1) * 100 : null;
  const dailyAbs = hasPrev ? Number(last.equity) - Number(prev.equity) : null;
  const days = points.length > 1 ? points.length : 0;
  const annualized =
    days > 0 && Number.isFinite(Number(equity.total_return))
      ? (Math.pow(1 + Number(equity.total_return), 252 / days) - 1) * 100
      : null;
  const drawdown = Number.isFinite(Number(equity.max_drawdown))
    ? Math.abs(Number(equity.max_drawdown)) * 100
    : null;
  const spark =
    points.length >= 2 ? <LineChart values={points.map((p) => p.equity)} height={34} /> : null;

  const kpis = [
    {
      label: "总资产",
      value: Number.isFinite(Number(equity.current)) ? fmt.money(equity.current) : "—",
      sub: Number.isFinite(Number(equity.initial))
        ? `台账初始 ${fmt.money(equity.initial)}${dailyAbs === null ? "" : ` · 较前值 ${fmt.signed(dailyAbs, 2)}`}`
        : "台账未返回初始权益",
      spark,
    },
    {
      label: "当日盈亏",
      value: dailyPct === null ? "无基准" : fmt.signed(dailyPct, 2, "%"),
      tone: dailyPct === null ? null : dailyPct >= 0 ? "up" : "down",
      sub:
        dailyPct === null
          ? `台账点位 ${points.length} 个（<2）· 无前一交易日基准，日内涨跌无数据源`
          : `基准 ${prev.t} → ${last.t}（台账回放）`,
      spark,
    },
    {
      label: "年化收益",
      value: annualized === null ? "—" : fmt.signed(annualized, 2, "%"),
      tone: annualized === null ? null : annualized >= 0 ? "up" : "down",
      sub:
        annualized === null
          ? `台账点位不足（${points.length} 个）· 年化无数据源`
          : `${days} 个交易日年化`,
      spark,
    },
    {
      label: "夏普比率",
      value: numOr(equity.sharpe, 2),
      sub: `成交 ${Number.isFinite(Number(equity.trades)) ? equity.trades : "—"} 笔 · 台账回放口径`,
    },
    {
      label: "最大回撤",
      value: drawdown === null ? "—" : `−${fmt.num(drawdown, 2)}%`,
      tone: "amber",
      sub: `阈值 15%${equity.note ? ` · ${String(equity.note).slice(0, 18)}…` : ""}`,
      spark,
    },
  ];

  /* ── ② 三通道状态 ── */
  const mcpCalls = Number(mt && mt.mcp ? mt.mcp.calls : 0) || 0;
  const mcpErrors = Number(mt && mt.mcp ? mt.mcp.errors : 0) || 0;
  const mcpOk = Boolean(mt);
  const sdkReason = (br && br.sdk && br.sdk.reason) || "本服务未挂载 SDK JSON-RPC 客户端";
  const headlessReason =
    (br && br.headless && br.headless.reason) || "本服务未挂载 Headless CLI 运行器";

  const mcpStats = (
    <Descriptions
      size="small"
      column={2}
      colon={false}
      items={[
        {
          key: "tools",
          label: "已注册工具",
          children: <span style={MONO}>{mt ? `${fmt.dash(mt.toolTotal)} 个` : "—"}</span>,
        },
        { key: "calls", label: "今日调用", children: <span style={MONO}>{mt ? `${mcpCalls} 次` : "—"}</span> },
        {
          key: "rate",
          label: "成功率",
          children: (
            <span style={{ ...MONO, color: mcpErrors === 0 ? C.up : C.amber }}>
              {mcpCalls > 0 ? `${fmt.num(((mcpCalls - mcpErrors) / mcpCalls) * 100, 1)}%` : "—"}
            </span>
          ),
        },
        {
          key: "latency",
          label: "平均延迟",
          children: (
            <span style={MONO}>
              {mt && Number.isFinite(Number(mt.mcp && mt.mcp.avgMs)) ? `${mt.mcp.avgMs} ms` : "—"}
            </span>
          ),
        },
      ]}
    />
  );

  /* ── ③ 决策链路时间线：研究流水线产出 + 审计链 + Headless last（全部真实来源） ── */
  const srun = strategy.value && strategy.value.ok ? strategy.value.run : null;
  const brunRaw = br ? br.decision : null;
  const brun = brunRaw && brunRaw.run ? brunRaw.run : brunRaw;
  const run = srun || (brun && brun.asOf ? brun : null);
  const headlessLast =
    (br && Array.isArray(br.headless && br.headless.last) && br.headless.last) || [];

  const timelineItems = [];
  if (run && run.asOf) {
    const count = Array.isArray(run.proposals) ? run.proposals.length : 0;
    timelineItems.push({
      color: "blue",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color="blue">流水线节点</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {fmt.stamp(run.asOf).slice(11, 19)}
            </Text>
          </Space>
          <Text>研究流水线产出 {count} 条调仓建议（PDAT → PET，as_of {fmt.stamp(run.asOf)}）</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            /api/v3/strategy · 待人工审批，研究侧不下单
          </Text>
        </Space>
      ),
    });
  }
  for (const entry of auditRows.slice(0, 5)) {
    const kindText = entry.kind === "signal" ? "信号" : entry.source_label || entry.kind || "审计";
    timelineItems.push({
      color: entry.kind === "signal" ? "green" : "gray",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color={entry.kind === "signal" ? "green" : "default"}>{kindText}</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {fmt.stamp(entry.at).slice(11, 19) || fmt.stamp(entry.at).slice(0, 10)}
            </Text>
          </Space>
          <Text>{`${entry.ticker ? `${entry.ticker} · ` : ""}${entry.detail || "—"}`}</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            工作台审计链 /api/v3/audit
          </Text>
        </Space>
      ),
    });
  }
  for (const item of headlessLast.slice(0, 3)) {
    timelineItems.push({
      color: "orange",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color="orange">Headless</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {fmt.stamp(item.at || item.finished_at || item.started_at).slice(11, 19)}
            </Text>
          </Space>
          <Text>{String(item.task || item.name || item.tool || "Headless 任务")}</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            状态 {String(item.status || item.result || "—")}
          </Text>
        </Space>
      ),
    });
  }

  /* ── ④ 风控红线三条（单笔 2% / 行业 20% / 回撤 15%） ── */
  const nav = Number(equity.current != null ? equity.current : od && od.nav ? od.nav : 0);
  const orderRows = (od && Array.isArray(od.orders) && od.orders) || [];
  const maxSingle =
    nav > 0
      ? orderRows.reduce((max, order) => Math.max(max, (Number(order.value || 0) / nav) * 100), 0)
      : null;
  const singleUsed = maxSingle !== null && maxSingle > 0 ? maxSingle : null;
  const redlines = [
    {
      label: "单笔交易上限",
      limit: 2,
      used: singleUsed,
      why: "工作台订单台账（/api/v3/oms/orders）未返回可比较的单笔金额",
      source: od ? `${od.nav_source || "台账"} · 分母 ${fmt.money(od.nav)}` : null,
    },
    {
      label: "单一行业暴露上限",
      limit: 20,
      used: null,
      why: (od && od.industry_source) || "行业维度敞口工作台未提供（工具面无行业分类数据源）",
      source: null,
    },
    {
      label: "最大回撤阈值",
      limit: 15,
      used: drawdown,
      why: "台账无回撤点位",
      source: equity.note ? `回撤口径：${equity.note}` : null,
    },
  ];

  /* ── ⑤ Agent Loop：SDK 会话未挂载 → 会话指标无数据源；工具调用 Top5 为真实计数 ── */
  const topTools = [
    ...Object.entries((mt && mt.mcp && mt.mcp.tools) || {}),
    ...Object.entries((mt && mt.wb && mt.wb.byTool) || {}).map(([name, count]) => [`wb:${name}`, count]),
  ]
    .map(([name, count]) => [name, Number(count) || 0])
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);

  /* ── ⑥ 待人工审批：oms/orders 中 stage=manual 的真实订单 ── */
  const manualOrders = orderRows.filter((order) => order.stage === "manual");

  /* ── ⑦ 数据源健康 ── */
  const dsFromSettings = (st && Array.isArray(st.data_sources) && st.data_sources) || [];
  const probeData = (br && br.sources && br.sources.workbench && br.sources.workbench.data) || null;
  const dsFromProbe = (probeData && Array.isArray(probeData.sources) && probeData.sources) || [];
  const healthRows = dsFromSettings.length
    ? dsFromSettings.map((item) => ({
        name: item.name,
        ok: Boolean(item.available),
        detail: String(item.detail || "—"),
      }))
    : dsFromProbe.map((item) => ({
        name: item.label || item.key || "—",
        ok: String(item.status).toLowerCase() === "ok",
        detail: String(item.detail || item.fix || "—"),
      }));
  if (mt) {
    healthRows.push({
      name: "dsh-quant-data-mcp（workbench 工具面）",
      ok: Boolean(mt.workbenchUp),
      detail: `MCP 工具域 · ${fmt.dash(mt.toolDomains)} 域 · ${fmt.dash(mt.toolTotal)} 个工具 · 今日调用 ${mcpCalls} 次 · 失败 ${mcpErrors} 次 · 平均 ${fmt.dash(
        mt.mcp && mt.mcp.avgMs,
      )}ms`,
    });
  }

  const mode = String((ov && ov.mode) || "").toUpperCase();

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {/* 页头：模式 / 数据截至 / 刷新（对应设计稿 .page-tools） */}
      <ProCard
        bordered
        bodyStyle={{ padding: "10px 16px" }}
        loading={overview.loading}
        title={
          <Title level={5} style={{ margin: 0 }}>
            系统概览
          </Title>
        }
        extra={
          <Space size={12} wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>
              平台（身体）× Harness（决策大脑）· 全局运行状态 · 环境 {mode || "—"}
            </Text>
            <Badge
              status={mode === "live" ? "error" : "processing"}
              text={mode === "live" ? "LIVE 实盘" : mode ? "SIM 模拟盘" : "模式未知"}
            />
            <Text type="secondary" style={{ ...MONO, fontSize: 12 }}>
              数据截至 {fmt.stamp(ov && ov.generated_at)}
            </Text>
            <Typography.Link onClick={refreshAll}>刷新</Typography.Link>
          </Space>
        }
      >
        {overview.error ? (
          <BlockError name="系统概览（/api/v3/overview）" error={overview.error} />
        ) : (
          <Text type="secondary" style={{ fontSize: 11 }}>
            数据来源：本服务 /api/v3/*（工作台工具面 / 富途行情 / 台账）· 取不到的项显式标注「无数据源」，页面不含占位数字
          </Text>
        )}
      </ProCard>

      {/* ① KPI 5 张 */}
      {overview.error ? null : ov ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(5, minmax(0, 1fr))", gap: 12 }}>
          {kpis.map((kpi) => (
            <KpiCard key={kpi.label} {...kpi} />
          ))}
        </div>
      ) : (
        <ProCard bordered loading={overview.loading}>
          <NoSource
            what="KPI（总资产 / 当日盈亏 / 年化收益 / 夏普比率 / 最大回撤）"
            why={errText(overview.value, "GET /api/v3/overview 未返回台账权益")}
          />
        </ProCard>
      )}

      {/* ② 三通道状态 3 卡 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 12 }}>
        <ChannelCard
          name="MCP Bridge"
          subtitle="工具暴露通道"
          loading={metrics.loading}
          chip={<Chip ok={mcpOk} />}
          stats={metrics.error ? <BlockError name="MCP Bridge（/api/v3/metrics）" error={metrics.error} /> : mcpStats}
          foot={
            mt
              ? `状态机状态 running（工具面 ${fmt.dash(mt.toolTotal)} 个 · workbench ${mt.workbenchUp ? "在线" : "不可达"}）`
              : undefined
          }
        />
        <ChannelCard
          name="SDK JSON-RPC"
          subtitle="人工会话通道"
          loading={brain.loading}
          chip={<Chip ok={false} />}
          stats={
            brain.error ? (
              <BlockError name="SDK JSON-RPC（/api/v3/brain）" error={brain.error} />
            ) : (
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <NoSource what="SDK 通道会话指标（活跃会话 / 初始化握手耗时 / 最近事件）" why={sdkReason} />
                <Descriptions
                  size="small"
                  column={1}
                  colon={false}
                  items={[
                    {
                      key: "status",
                      label: "通道状态",
                      children: (
                        <Text type="secondary">
                          {(br && br.sdk && br.sdk.status) || "unavailable"}（无数据源）
                        </Text>
                      ),
                    },
                    {
                      key: "turns",
                      label: "落盘回合",
                      children: (
                        <Text type="secondary">
                          {br && br.sdk && Array.isArray(br.sdk.turns)
                            ? `${br.sdk.turns.length} 条（未挂载会话即 0）`
                            : "无数据源"}
                        </Text>
                      ),
                    },
                  ]}
                />
              </Space>
            )
          }
          foot="SDK 通道未挂载：会话与握手耗时无数据源"
        />
        <ChannelCard
          name="Headless CLI"
          subtitle="自动唤醒通道"
          loading={brain.loading}
          chip={<Chip ok={false} />}
          stats={
            brain.error ? (
              <BlockError name="Headless CLI（/api/v3/brain）" error={brain.error} />
            ) : (
              <Descriptions
                size="small"
                column={2}
                colon={false}
                items={[
                  {
                    key: "total",
                    label: "今日唤醒",
                    children: (
                      <span style={MONO}>
                        {br && br.headless && br.headless.today ? br.headless.today.total : "无数据源"}
                      </span>
                    ),
                  },
                  {
                    key: "ok",
                    label: "成功 / 失败",
                    children: (
                      <span style={MONO}>
                        {br && br.headless && br.headless.today
                          ? `${br.headless.today.success} / ${br.headless.today.failed}`
                          : "无数据源"}
                      </span>
                    ),
                  },
                  {
                    key: "avg",
                    label: "平均耗时",
                    children: (
                      <span style={MONO}>
                        {br && br.headless && br.headless.today && Number(br.headless.today.total) > 0
                          ? `${br.headless.today.avgMs} ms`
                          : "无数据源"}
                      </span>
                    ),
                  },
                  {
                    key: "last",
                    label: "最近任务",
                    children: (
                      <Text type="secondary">{headlessLast.length ? `${headlessLast.length} 条` : "无数据源"}</Text>
                    ),
                  },
                ]}
              />
            )
          }
          foot={`Headless 通道未挂载：${headlessReason} · 定时器 + 调度心跳由服务侧提供（见网关与调度页）`}
        />
      </div>

      {/* ③ 决策链路时间线 + ④ 风控红线 */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.35fr) minmax(0, 1fr)", gap: 12 }}>
        <ProCard
          title="决策链路时间线"
          bordered
          loading={strategy.loading || audit.loading || brain.loading}
          extra={
            <Text type="secondary" style={{ fontSize: 11 }}>
              最近 {timelineItems.length} 条
            </Text>
          }
        >
          {strategy.error ? (
            <BlockError name="决策链路（/api/v3/strategy）" error={strategy.error} />
          ) : timelineItems.length === 0 ? (
            <NoSource what="决策链路" why="审计链与流水线均无记录" />
          ) : (
            <>
              <Timeline items={timelineItems} />
              <Divider style={{ margin: "8px 0" }} />
              <Text type="secondary" style={{ fontSize: 11 }}>
                本链路 {timelineItems.length} 节点 · 来源：研究流水线 /api/v3/strategy · 工作台审计链 /api/v3/audit
                {headlessLast.length ? " · Headless /api/v3/brain" : ""}
              </Text>
            </>
          )}
        </ProCard>

        <ProCard title="风控红线" bordered loading={orders.loading || overview.loading}>
          {orders.error ? <BlockError name="风控红线（/api/v3/oms/orders）" error={orders.error} /> : null}
          {redlines.map((row) => {
            const hasValue = row.used !== null && Number.isFinite(Number(row.used));
            const ratio = hasValue ? Math.min(100, (Number(row.used) / row.limit) * 100) : 0;
            const stroke = !hasValue ? C.faint : ratio >= 100 ? C.down : ratio >= 70 ? C.amber : C.up;
            return (
              <div key={row.label} style={{ marginBottom: 14 }}>
                <Space style={{ width: "100%", justifyContent: "space-between" }}>
                  <Text>{row.label}</Text>
                  <Text style={{ ...MONO, color: C.muted }}>上限 {row.limit}%</Text>
                </Space>
                <Progress
                  percent={Number(ratio.toFixed(1))}
                  strokeColor={stroke}
                  size="small"
                  format={() =>
                    hasValue ? (
                      <span style={{ ...MONO, color: C.muted, fontSize: 11 }}>
                        当前 {fmt.num(row.used, 2)}%
                      </span>
                    ) : (
                      <span style={{ color: C.faint, fontSize: 11 }}>无数据源</span>
                    )
                  }
                />
                {hasValue ? (
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {ratio >= 100 ? "已越过阈值，需人工核对" : ratio >= 70 ? "接近阈值" : "阈值内"}
                    {row.source ? ` · ${row.source}` : ""}
                  </Text>
                ) : (
                  <Tooltip title={row.why}>
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      {noSourceText(row.label, row.why)}
                    </Text>
                  </Tooltip>
                )}
              </div>
            );
          })}
          {od ? (
            <Text type="secondary" style={{ fontSize: 11 }}>
              台账订单 {orderRows.length} 条（manual {manualOrders.length} 条）· NAV {fmt.money(od.nav)}（
              {od.nav_source || "—"}）
            </Text>
          ) : null}
        </ProCard>
      </div>

      {/* ⑤ Agent Loop 实时状态 */}
      <ProCard
        title="Agent Loop 实时状态"
        bordered
        loading={metrics.loading || brain.loading}
        extra={<Chip ok={false} />}
      >
        <Descriptions
          size="small"
          column={4}
          colon={false}
          items={[
            { key: "turn", label: "当前 turn", children: <Text type="secondary">无数据源</Text> },
            { key: "token", label: "已用 token", children: <Text type="secondary">无数据源</Text> },
            { key: "model", label: "模型", children: <Text type="secondary">无数据源</Text> },
            { key: "effort", label: "推理强度", children: <Text type="secondary">无数据源</Text> },
          ]}
        />
        <Alert
          style={{ marginTop: 10 }}
          type="warning"
          showIcon
          message={noSourceText(
            "Agent Loop 会话指标（turn / token / 模型 / 推理强度）",
            `${sdkReason}；Headless：${headlessReason}`,
          )}
          description={
            <Text type="secondary" style={{ fontSize: 11 }}>
              本页只提供研究流水线记录（见决策链路时间线）与进程内工具调用计数，不用设计稿里的占位轮次 / token 顶替。
            </Text>
          }
        />
        <Divider style={{ margin: "12px 0 8px" }} orientation="left" orientationMargin={0}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            工具调用 Top 5
          </Text>
        </Divider>
        {metrics.error ? (
          <BlockError name="工具调用分布（/api/v3/metrics）" error={metrics.error} />
        ) : topTools.length === 0 ? (
          <NoSource what="工具调用 Top 5" why="进程内调用计数为空（/api/v3/metrics 未返回任何工具计数）" />
        ) : (
          <>
            <BarList items={topTools} />
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
              当前进程累计 · 共 {mcpCalls} 次调用 · 失败 {mcpErrors} 次 · 平均{" "}
              {fmt.dash(mt && mt.mcp ? mt.mcp.avgMs : null)}ms · 来源 /api/v3/metrics（mcp.tools + wb.byTool）
            </Text>
          </>
        )}
      </ProCard>

      {/* ⑥ 待人工审批 */}
      <ProCard
        title="待人工审批"
        bordered
        loading={orders.loading}
        extra={
          <Space size={8}>
            <Tag color={manualOrders.length ? "warning" : "default"}>{manualOrders.length} 条待处理</Tag>
            <Text type="secondary" style={{ fontSize: 11 }}>
              执行入口唯一 · 所有执行动作须经人工审批
            </Text>
          </Space>
        }
      >
        {orders.error ? (
          <BlockError name="待人工审批（/api/v3/oms/orders）" error={orders.error} />
        ) : manualOrders.length === 0 ? (
          <NoSource
            what="待人工审批"
            why={od ? "当前台账没有 manual 阶段订单" : errText(orders.value, "订单台账未返回")}
          />
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12 }}>
            {manualOrders.map((order) => {
              const orderNav = Number(order.nav_used != null ? order.nav_used : od && od.nav ? od.nav : 0);
              const share =
                orderNav > 0 && Number.isFinite(Number(order.value)) ? (Number(order.value) / orderNav) * 100 : null;
              return (
                <Card
                  key={order.id}
                  size="small"
                  title={
                    <Space size={6} wrap>
                      <Text strong style={MONO}>
                        {order.ticker}
                      </Text>
                      <Tag color={order.side === "BUY" ? "success" : "error"}>{order.side}</Tag>
                      <Text style={MONO}>{fmt.dash(order.qty)} 股</Text>
                      <Text type="secondary" style={MONO}>
                        金额 {fmt.money(order.value)}
                      </Text>
                    </Space>
                  }
                  extra={<Tag color="warning">{order.stage}</Tag>}
                >
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    <Text style={{ fontSize: 12 }}>
                      来源：工作台计划 {fmt.dash(order.plan_id)}（{fmt.dash(order.plan_status)}）· 模式{" "}
                      {fmt.dash(order.mode)}
                    </Text>
                    <Text style={{ fontSize: 12 }}>
                      风控：{((order.risk && order.risk.reasons) || ["阈值内"]).join("；")}
                    </Text>
                    <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
                      单笔占比 {numOr(share, 2)}% {order.nav_source ? `（${order.nav_source}）` : ""}· 首次进入{" "}
                      {fmt.stamp(order.first_seen_at)} · 更新 {fmt.stamp(order.updated_at)}
                    </Text>
                  </Space>
                </Card>
              );
            })}
          </div>
        )}
      </ProCard>

      {/* ⑦ 数据源健康 */}
      <ProCard
        title="数据源健康"
        bordered
        loading={settings.loading || metrics.loading || brain.loading}
        extra={
          <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
            workbench {mt ? (mt.workbenchUp ? "在线" : "不可达") : "—"} · 工具 {fmt.dash(mt && mt.toolTotal)} 个 ·
            数据时点 {fmt.stamp(ov && ov.generated_at)}
          </Text>
        }
      >
        {settings.error ? (
          <BlockError name="数据源健康（/api/v3/settings）" error={settings.error} />
        ) : healthRows.length === 0 ? (
          <NoSource what="数据源健康" why={errText(settings.value, "settings 未返回数据源清单")} />
        ) : (
          <>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
                gap: 12,
              }}
            >
              {healthRows.map((row) => (
                <Card key={row.name} size="small">
                  <Space size={8} style={{ width: "100%", justifyContent: "space-between" }}>
                    <Text strong style={{ fontSize: 12 }}>
                      {row.name}
                    </Text>
                    <Badge
                      status={row.ok ? "success" : "warning"}
                      text={<span style={{ fontSize: 12 }}>{row.ok ? "正常" : "未配置"}</span>}
                    />
                  </Space>
                  <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 6 }}>
                    {row.detail}
                  </Text>
                </Card>
              ))}
            </div>
            {probeData && probeData.checked_at ? (
              <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 10 }}>
                真实数据源探测 {Array.isArray(probeData.sources) ? probeData.sources.length : 0} 项 · 检查于{" "}
                {fmt.stamp(probeData.checked_at)} · ok {probeData.summary ? fmt.dash(probeData.summary.ok) : "—"} /
                warn {probeData.summary ? fmt.dash(probeData.summary.warn) : "—"} / fail{" "}
                {probeData.summary ? fmt.dash(probeData.summary.fail) : "—"} · 来源 workbench sources（/api/v3/brain）
              </Text>
            ) : null}
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 6 }}>
              来源 /api/v3/settings（富途 / AKShare / SEC EDGAR / Tushare 探测）+ /api/v3/metrics（workbench
              工具面）· 未配置的数据源按「未配置」如实展示，不估算可用性
            </Text>
          </>
        )}
      </ProCard>
    </Space>
  );
}
