// 概览页：专业量化工作台着陆页——一屏看全状态。数据全部来自既有端点的组合调用：
// 字段依据（页面每个取值路径均可指到源码行）：
//   snapshot → store_access.snapshot：{mode, notice, ...}（模式徽章同页头 modeBadge）。
//   positions → plugins/workbench/python/positions.py collect → counts{positions,
//     accounts_checked, accounts_with_positions}；group.positions[]（symbol/name/qty/
//     market_value/pl_val，币种混排——Top5 按市值数值排序仅作展示，不做跨币种合并）。
//   deals_today → trading.py OpenAPI 只读端点 → {groups:[{acc_id, market, rows}], errors}；
//     sim / mcp 通道下服务端如实拒绝（trading/openapi-unavailable），页面照实展示不可用。
//   reconcile → trading_core/snapshots.py reconcile_snapshot → alerts[{level,title,detail,
//     created_at}]（告警级别色表复用调度页 ALERT_LEVEL_COLOR）。
//   equity → analytics.py equity_curve → points[{t, equity, dd}]（迷你图取近 60 点）。
//   factors-history → trading_core store.list_factor_snapshots → snapshots[{date, payload,
//     created_at}]（倒序，[0] 最新；payload 是因子快照对象，factors/tickers 键给出数量）。
//   schedule → snapshots.py schedule_snapshot → {heartbeat{heartbeat,...}, kill, halt, jobs}
//     （心跳新鲜度与调度页 parseHeartbeat 同口径）。
//   sources → plugins/workbench/python/sources.py → summary{ok, warn, fail}。
//   push_status → futu_push 运行时门面：enabled/quote.connected/quote.authenticated，
//     文案只有三类白名单（已连接/连接中/未启用），读取失败不猜状态。
// 所有取数走 useEndpoint/useSnapshotPoll（callApi），无直连；缺失端点被声明预检拦截。
import React from "react";
import { Card, Col, Row, Space, Statistic, Table, Tag, Tooltip, Typography } from "antd";
import { useEndpoint, useSnapshotPoll } from "../services/hooks.js";
import { num, stampOf } from "../services/format.jsx";
import { modeBadge } from "../services/mode.js";
import { LineChart } from "../charts/line.jsx";
import { ALERT_LEVEL_COLOR, parseHeartbeat } from "./schedule.jsx";

/** 心跳距今分钟数（向下取整）；无法解析返回 null。 */
function minutesSince(ms) {
  if (!Number.isFinite(ms)) return null;
  return Math.max(0, Math.floor((Date.now() - ms) / 60_000));
}

/** push_status → 三类白名单文案；未取到/读取失败返回 null（不猜状态，同页头 PushBadge）。 */
function pushStatusText(push) {
  if (!push || !push.enabled) return null;
  const quote = push.quote ?? {};
  if (quote.connected && quote.authenticated) return { text: "实时推送已连接", color: "green" };
  return { text: "推送连接中", color: "blue" };
}

/** 今日成交笔数：OpenAPI 分组结果跨账户求和（纯计数，不合并金额）。 */
function dealsCount(deals) {
  return (deals?.groups ?? []).reduce((sum, group) => sum + (group.rows?.length ?? 0), 0);
}

/** 持仓 Top5（按市值数值排序，仅展示；币种混排不做换算）。 */
function topPositions(positions, limit = 5) {
  return (positions?.groups ?? [])
    .flatMap((group) => (group.positions ?? []).map((position) => ({
      ...position,
      account: group.account,
      accId: group.acc_id,
    })))
    .sort((a, b) => (Number(b.market_value) || 0) - (Number(a.market_value) || 0))
    .slice(0, limit);
}

const ALERT_COLUMNS = [
  { title: "级别", key: "level",
    render: (_field, row) => (
      <Tag color={ALERT_LEVEL_COLOR[row.level] ?? "default"}>{row.level ?? "—"}</Tag>) },
  { title: "标题", key: "title", render: (_field, row) => row.title ?? "—" },
  { title: "详情", key: "detail", ellipsis: true, render: (_field, row) => row.detail ?? "—" },
  { title: "时间", key: "created_at", render: (_field, row) => stampOf(row.created_at) },
];

export default function OverviewPage() {
  const snapshot = useSnapshotPoll();
  const mode = snapshot.value?.mode ?? null;
  const positions = useEndpoint("positions", mode ? { mode } : null, [mode]);
  const deals = useEndpoint("deals_today", mode ? { mode } : null, [mode]);
  const reconcile = useEndpoint("reconcile", {}, []);
  const equity = useEndpoint("equity", mode ? { mode, window: 60 } : null, [mode]);
  const factorsHistory = useEndpoint("factors-history", { limit: 30 });
  const schedule = useEndpoint("schedule", {}, []);
  const sources = useEndpoint("sources", {}, []);
  const push = useEndpoint("push_status", {}, []);

  const badge = modeBadge(mode ?? "sim");
  const alerts = reconcile.value?.alerts ?? [];
  const openAlerts = alerts.length;
  const holdings = positions.value?.counts?.positions
    ?? (positions.value?.groups ?? []).reduce((sum, group) => sum + (group.positions?.length ?? 0), 0);
  const todayDeals = deals.error ? null : dealsCount(deals.value);
  const equityPoints = (equity.value?.points ?? []).map((point) => ({ t: point.t, v: point.equity }));
  const latestSnapshot = factorsHistory.value?.snapshots?.[0] ?? null;
  const pushStatus = pushStatusText(push.value);

  // 调度器健康摘要：心跳新鲜度 + kill/halt 原样事实（字段缺失一律 —，不推断）
  const heartbeat = schedule.value?.heartbeat ?? null;
  const heartbeatMs = parseHeartbeat(heartbeat?.heartbeat);
  const heartbeatMinutes = Number.isFinite(heartbeatMs) ? minutesSince(heartbeatMs) : null;
  const statusItems = [
    { label: "推送", node: pushStatus
      ? <Tag color={pushStatus.color}>{pushStatus.text}</Tag>
      : <Typography.Text type="secondary">{push.error ? "推送状态读取失败" : (push.value && !push.value.enabled ? "推送未启用" : "—")}</Typography.Text> },
    { label: "调度器", node: (
      <Space size={4} wrap>
        {heartbeatMinutes === null
          ? <Typography.Text type="secondary">{schedule.error ? "读取失败" : "无心跳记录"}</Typography.Text>
          : <Typography.Text type={heartbeatMinutes > 5 ? "warning" : undefined}>心跳 {heartbeatMinutes} 分钟前</Typography.Text>}
        {schedule.value?.kill ? <Tag color="red">kill 已落下</Tag> : null}
        {schedule.value?.halt ? <Tag color="orange">已暂停</Tag> : null}
      </Space>) },
    { label: "数据源", node: sources.error
      ? <Typography.Text type="secondary">读取失败</Typography.Text>
      : sources.value?.summary
        ? <Typography.Text>正常 {sources.value.summary.ok ?? "—"} · 待配置 {sources.value.summary.warn ?? "—"} · 异常 {sources.value.summary.fail ?? "—"}</Typography.Text>
        : <Typography.Text type="secondary">—</Typography.Text> },
  ];

  return (
    <Card title="概览" extra={snapshot.value?.generated_at && (
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        快照 {stampOf(snapshot.value.generated_at)}
      </Typography.Text>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {snapshot.error && (
          <Typography.Text type="danger">快照读取失败：{snapshot.error}</Typography.Text>)}
        {snapshot.value?.notice && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>{snapshot.value.notice}</Typography.Text>)}

        <Row gutter={[16, 16]}>
          <Col xs={12} md={6}>
            <Card size="small"><Statistic title="账户模式" value={badge.text} /></Card>
          </Col>
          <Col xs={12} md={6}>
            <Card size="small">
              <Statistic title="持仓数" value={positions.loading ? "…" : (holdings ?? "—")} />
              {positions.error && <Typography.Text type="warning" style={{ fontSize: 12 }}>持仓读取失败</Typography.Text>}
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card size="small">
              <Statistic title="今日成交（OpenAPI）" value={deals.loading ? "…" : (todayDeals ?? "—")} />
              {deals.error && (
                <Tooltip title={deals.error}>
                  <Typography.Text type="warning" style={{ fontSize: 12 }}>实盘 OpenAPI 通道不可用</Typography.Text>
                </Tooltip>)}
            </Card>
          </Col>
          <Col xs={12} md={6}>
            <Card size="small">
              <Statistic title="活跃告警" value={reconcile.loading ? "…" : openAlerts}
                valueStyle={openAlerts > 0 ? { color: "#f5222d" } : undefined} />
              {reconcile.error && <Typography.Text type="warning" style={{ fontSize: 12 }}>告警读取失败</Typography.Text>}
            </Card>
          </Col>
        </Row>

        <Row gutter={[16, 16]}>
          <Col xs={24} lg={14}>
            <Card size="small" title="权益曲线（本地模拟台账，近 60 点）">
              {equity.error && <Typography.Text type="danger">权益读取失败：{equity.error}</Typography.Text>}
              {equityPoints.length >= 2 && <LineChart points={equityPoints} label="权益" />}
              {!equity.error && !equity.loading && equity.value && equityPoints.length < 2 && (
                <Typography.Text type="secondary">暂无足够的权益序列（不足 2 点不绘制）。</Typography.Text>)}
            </Card>
          </Col>
          <Col xs={24} lg={10}>
            <Card size="small" title="运行状态">
              <Space direction="vertical" size="small" style={{ width: "100%" }}>
                {statusItems.map((item) => (
                  <Space key={item.label} size={8}>
                    <Typography.Text type="secondary" style={{ width: 56 }}>{item.label}</Typography.Text>
                    {item.node}
                  </Space>))}
              </Space>
            </Card>
          </Col>
        </Row>

        <Row gutter={[16, 16]}>
          <Col xs={24} lg={14}>
            <Card size="small" title="持仓 Top5（按市值）">
              <Table size="small"
                rowKey={(row) => `${row.accId}-${row.symbol}`}
                dataSource={topPositions(positions.value)}
                pagination={false}
                loading={positions.loading}
                locale={{ emptyText: positions.loading ? "持仓加载中…" : "暂无持仓。" }}
                columns={[
                  { title: "标的", key: "symbol", render: (_f, row) => (
                    <Space size={4}>
                      <span>{row.symbol || "—"}</span>
                      {row.name ? <Typography.Text type="secondary">{row.name}</Typography.Text> : null}
                    </Space>) },
                  { title: "数量", key: "qty", align: "right", render: (_f, row) => num(row.qty, 0) },
                  { title: "市值", key: "market_value", align: "right", render: (_f, row) => num(row.market_value) },
                  { title: "盈亏", key: "pl_val", align: "right", render: (_f, row) => num(row.pl_val) },
                  { title: "币种", key: "currency", render: (_f, row) => row.currency ?? "—" },
                ]} />
            </Card>
          </Col>
          <Col xs={24} lg={10}>
            <Card size="small" title="因子快照">
              <Space size="large" wrap>
                <Statistic title="最新快照日期" value={latestSnapshot?.date ?? "—"} />
                <Statistic title="因子数"
                  value={latestSnapshot?.payload?.factors?.length ?? "—"} />
                <Statistic title="覆盖标的"
                  value={latestSnapshot?.payload?.tickers?.length ?? "—"} />
              </Space>
              {factorsHistory.error && (
                <Typography.Text type="warning" style={{ fontSize: 12 }}>
                  因子历史读取失败：{factorsHistory.error}
                </Typography.Text>)}
              {latestSnapshot && (
                <Typography.Text type="secondary" style={{ fontSize: 12, display: "block" }}>
                  本地保留 {factorsHistory.value?.snapshots?.length ?? 0} 份快照（按日收集，倒序取最新）。
                </Typography.Text>)}
            </Card>
          </Col>
        </Row>

        <Card size="small" title="最新告警（3 条）">
          <Table size="small"
            rowKey={(_row, index) => index}
            dataSource={alerts.slice(0, 3)}
            pagination={false}
            loading={reconcile.loading}
            locale={{ emptyText: reconcile.loading ? "告警加载中…" : "暂无告警。" }}
            columns={ALERT_COLUMNS} />
        </Card>
      </Space>
    </Card>);
}
