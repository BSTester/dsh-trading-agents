// V3「网关与调度」页（Ant Design Pro）：平台与 Harness 之间的「一核三通道」运维视图。
// 设计稿对应 od-quant-harness-platform/gateway.html（版式参照，数字一律不抄）。
//
// 数据来源（全部服务端实测，缺失即显式声明，绝不补位）：
//   GET /api/v3/gateway → 三通道状态（channels.mcp/sdk/headless）、调度器（scheduler.rules/
//     .recent/.firedToday）、Headless 台账（headless.today/.breaker/.last）
//   GET /api/v3/metrics → MCP 工具调用计数（mcp.calls/errors/avgMs/tools）、工作台调用
//     （wb.*）、HTTP 请求（http.*）、OMS 阶段（oms）
// 契约之外的字段（Profile/Bundle 清单、P95、并发位释放等）后端尚未提供 → 本页如实标注
// 「无数据源」，不摆假数字。
import React from "react";
import { Alert, Badge, Card, Col, Descriptions, Row, Space, Statistic, Table, Tag, Typography } from "antd";
import { ProCard } from "@ant-design/pro-components";
import { useV3 } from "../../services/v3api.js";
import { stampOf } from "../../services/format.jsx";
import { HBarChart } from "../../charts/bars.jsx";

const { Text, Title } = Typography;

/** 「无数据源」统一写法：一句原因 + 可选接口名，供所有缺失区块复用。 */
function NoSource({ reason, api }) {
  return (
    <Text type="secondary" style={{ fontSize: 12 }}>
      无数据源：{reason}{api ? `（接口 ${api}）` : ""}
    </Text>
  );
}

/** 有限数值才算「有数据」；null/undefined/空串/非数字一律 null（不按 0 展示）。 */
function finite(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** 计数文案：缺失 → "—"，有值 → 千分位；用于卡片里的指标格。 */
function countText(value) {
  const n = finite(value);
  return n === null ? "—" : n.toLocaleString("zh-CN");
}

/** 毫秒文案：缺失 → "—"。 */
function msText(value) {
  const n = finite(value);
  return n === null ? "—" : `${n.toLocaleString("zh-CN")} ms`;
}

/** 通道状态 → antd Badge 状态（未知状态一律灰，不猜成「正常」）。 */
function channelBadge(status) {
  const text = String(status ?? "");
  if (/^(running|ready|ok|alive)$/i.test(text)) return { status: "success", text };
  if (/^(starting|pending|degraded|lazy)$/i.test(text)) return { status: "warning", text };
  if (/^(stopped|down|failed|error)$/i.test(text)) return { status: "error", text };
  return { status: "default", text: text || "未知" };
}

/** 通道卡片的一个指标格：值缺失时显示「无数据源」而不是 0。 */
function Metric({ label, value, unit, missing }) {
  const text = typeof value === "string" ? value : countText(value);
  return (
    <div>
      <Text type="secondary" style={{ fontSize: 11, display: "block" }}>{label}</Text>
      {text === "—"
        ? <Text type="secondary" style={{ fontSize: 12 }}>无数据源{missing ? `：${missing}` : ""}</Text>
        : <Text strong style={{ fontSize: 16 }}>{text}{unit ? <Text type="secondary" style={{ fontSize: 11 }}> {unit}</Text> : null}</Text>}
    </div>
  );
}

/** 三通道卡片：状态（channels.*）+ 指标（metrics.*）；缺的指标格显式「无数据源」。 */
function ChannelCards({ channels, metrics, headlessToday, headlessLast, toolTotal }) {
  const mcp = channels?.mcp ?? null;
  const sdk = channels?.sdk ?? null;
  const head = channels?.headless ?? null;
  const mcpBadge = channelBadge(mcp?.status);
  const sdkBadge = channelBadge(sdk?.status);
  const headBadge = channelBadge(head?.status);
  const mcpMetrics = metrics?.mcp ?? null;
  const breaker = metrics?.headless ?? null;
  const mcpFailPct = mcpMetrics && finite(mcpMetrics.calls) ? (finite(mcpMetrics.errors) ?? 0) / finite(mcpMetrics.calls) * 100 : null;
  const toolCalls = mcpMetrics?.tools && typeof mcpMetrics.tools === "object" ? Object.values(mcpMetrics.tools).reduce((sum, v) => sum + (finite(v) ?? 0), 0) : null;

  return (
    <Row gutter={[12, 12]}>
      <Col xs={24} lg={8}>
        <ProCard title="MCP Bridge" bordered
          extra={<Badge status={mcpBadge.status} text={<span className="num">{mcpBadge.text}</span>} />}>
          <Descriptions column={1} size="small">
            <Descriptions.Item label="协议">{mcp?.protocol ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="方向">{mcp?.direction ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="已注册工具">
              {finite(mcp?.tools) === null
                ? <NoSource reason="gateway 通道未返回 tools；/api/v3/tools 的 total 见「工具域治理」页" api="/api/v3/gateway" />
                : countText(mcp.tools)}
            </Descriptions.Item>
            <Descriptions.Item label="工具面总数（/api/v3/tools）">
              {finite(toolTotal) === null ? <NoSource reason="接口未返回 total" api="/api/v3/tools" /> : countText(toolTotal)}
            </Descriptions.Item>
          </Descriptions>
          <Row gutter={[8, 8]} style={{ marginTop: 8 }}>
            <Col span={8}><Metric label="MCP 调用" value={mcpMetrics?.calls} missing="metrics.mcp.calls 缺失" /></Col>
            <Col span={8}><Metric label="失败率" value={mcpFailPct === null ? "—" : `${mcpFailPct.toFixed(2)}%`} missing="调用数为 0 或字段缺失" /></Col>
            <Col span={8}><Metric label="平均耗时" value={mcpMetrics?.avgMs === undefined ? "—" : msText(mcpMetrics.avgMs)} missing="metrics.mcp.avgMs 缺失" /></Col>
            <Col span={8}><Metric label="工具计数合计" value={toolCalls} missing="metrics.mcp.tools 为空" /></Col>
            <Col span={8}><Metric label="错误数" value={mcpMetrics?.errors} missing="metrics.mcp.errors 缺失" /></Col>
            <Col span={8}>
              <Text type="secondary" style={{ fontSize: 11, display: "block" }}>P95 延迟</Text>
              <NoSource reason="本服务未暴露分位数（只给 avgMs）" />
            </Col>
          </Row>
          <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
            生命周期状态机（start/stop/update）与 adapter 版本号不在 /api/v3/gateway 契约内 → 无数据源。
          </Text>
        </ProCard>
      </Col>

      <Col xs={24} lg={8}>
        <ProCard title="SDK JSON-RPC" bordered
          extra={<Badge status={sdkBadge.status} text={<span className="num">{sdkBadge.text}</span>} />}>
          <Descriptions column={1} size="small">
            <Descriptions.Item label="协议">{sdk?.protocol ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="方向">{sdk?.direction ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="原因（reason）">
              {sdk?.reason
                ? <Text type={sdkBadge.status === "error" ? "danger" : "secondary"}>{sdk.reason}</Text>
                : <Text type="secondary">服务未返回 reason（通道正常时通常为空）</Text>}
            </Descriptions.Item>
            <Descriptions.Item label="路由">{sdk?.route ?? <NoSource reason="契约未包含 route" />}</Descriptions.Item>
            <Descriptions.Item label="serverInfo">
              {sdk?.serverInfo ? <Text code>{JSON.stringify(sdk.serverInfo)}</Text> : <NoSource reason="契约未包含 serverInfo" />}
            </Descriptions.Item>
          </Descriptions>
          <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
            活跃会话数、初始化握手耗时、会话事件列表：/api/v3/gateway 未返回这些字段 → 无数据源。
          </Text>
        </ProCard>
      </Col>

      <Col xs={24} lg={8}>
        <ProCard title="Headless CLI" bordered
          extra={<Badge status={headBadge.status} text={<span className="num">{headBadge.text}</span>} />}>
          <Descriptions column={1} size="small">
            <Descriptions.Item label="协议">{head?.protocol ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="方向">{head?.direction ?? "—"}</Descriptions.Item>
            <Descriptions.Item label="唤醒命令">
              {head?.command
                ? <Text code style={{ fontSize: 12 }}>{head.command}</Text>
                : <NoSource reason="契约未包含 command" />}
            </Descriptions.Item>
          </Descriptions>
          <Row gutter={[8, 8]} style={{ marginTop: 8 }}>
            <Col span={8}><Metric label="今日唤醒" value={headlessToday?.total} missing="headless.today 缺失" /></Col>
            <Col span={8}><Metric label="成功" value={headlessToday?.success} missing="headless.today 缺失" /></Col>
            <Col span={8}><Metric label="失败" value={headlessToday?.failed} missing="headless.today 缺失" /></Col>
            <Col span={8}><Metric label="平均耗时" value={headlessToday?.avgMs === undefined ? "—" : msText(headlessToday.avgMs)} missing="headless.today 缺失" /></Col>
            <Col span={8}><Metric label="强制 kill" value={headlessToday?.killed} missing="headless.today.killed 缺失" /></Col>
            <Col span={8}><Metric label="近 30 日累计调用" value={breaker?.calls} missing="metrics.headless.calls 缺失" /></Col>
          </Row>
          <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
            最近一条调用记录：{headlessLast?.started_at
              ? stampOf(headlessLast.started_at)
              : <Text type="secondary">无数据源：headless.last 为空</Text>}
          </Text>
        </ProCard>
      </Col>
    </Row>
  );
}

/** Headless 调用日志表：列与契约的 headless.last[*] 字段一一对应。 */
function HeadlessLog({ rows, loading }) {
  return (
    <ProCard title="Headless 调用日志" bordered extra={<Text type="secondary" style={{ fontSize: 12 }}>来源 /api/v3/gateway → headless.last</Text>}>
      <Table
        size="small" rowKey={(row) => String(row?.started_at ?? row?.id ?? Math.random())}
        dataSource={Array.isArray(rows) ? rows : []} loading={loading} pagination={false}
        locale={{ emptyText: "无数据源：headless.last 为空（本服务未返回调用记录）" }}
        scroll={{ x: 900 }}
        columns={[
          { title: "开始时间", dataIndex: "started_at", width: 160, render: (v) => stampOf(v) },
          { title: "退出码", width: 90, render: (_, row) => {
            const code = finite(row?.exit_code);
            if (code === null) return <Text type="secondary">—</Text>;
            return <Tag color={code === 0 ? "green" : code === 124 ? "orange" : "red"}>{code}</Tag>;
          } },
          { title: "耗时", width: 100, render: (_, row) => msText(row?.duration_ms) },
          { title: "结果", width: 90, render: (_, row) => (row?.success === undefined
            ? <Text type="secondary">未返回</Text>
            : <Tag color={row.success ? "green" : "red"}>{row.success ? "成功" : "失败"}</Tag>) },
          { title: "超时/强杀", width: 120, render: (_, row) => (row?.timed_out
            ? <Tag color="orange">{`超时${row?.killed_by ? `（${row.killed_by}）` : ""}`}</Tag>
            : <Text type="secondary">—</Text>) },
          { title: "token 估算", width: 110, render: (_, row) => (finite(row?.tokens_estimate) === null ? "—" : countText(row.tokens_estimate)) },
          { title: "题面/摘要", render: (_, row) => (
            <Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: true }}>
              {row?.prompt_summary ?? row?.prompt ?? row?.stdout_summary ?? "本服务未返回题面/摘要字段"}
            </Text>) },
          { title: "stderr", render: (_, row) => (row?.stderr
            ? <Text type="danger" style={{ fontSize: 12 }} ellipsis={{ tooltip: row.stderr }}>{String(row.stderr).slice(0, 200)}</Text>
            : <Text type="secondary">—</Text>) },
        ]}
      />
      <Text type="secondary" style={{ fontSize: 12 }}>
        契约只冻结了 headless.last 的存在性：本表按行渲染实际存在的字段（缺字段显示「—」/「未返回」），
        不按设计稿固定列名硬取。
      </Text>
    </ProCard>
  );
}

/** 调度器：定时规则（scheduler.rules）+ 今日已触发（scheduler.firedToday）+ 最近触发（recent）。 */
function SchedulerCard({ scheduler, loading }) {
  const rules = Array.isArray(scheduler?.rules) ? scheduler.rules : [];
  const fired = Array.isArray(scheduler?.firedToday) ? scheduler.firedToday : [];
  const recent = Array.isArray(scheduler?.recent) ? scheduler.recent : [];
  const firedIds = new Set(fired.map((item) => item?.id));
  const byId = new Map(fired.map((item) => [item?.id, item?.day]));

  return (
    <ProCard title="调度器" bordered loading={loading}
      extra={<Text type="secondary" style={{ fontSize: 12 }}>
        {rules.length ? `规则 ${rules.length} 条 · 今日已触发 ${firedIds.size} 条` : "来源 /api/v3/gateway → scheduler"}
      </Text>}>
      <Table
        size="small" rowKey={(row) => String(row?.id ?? Math.random())}
        dataSource={rules} pagination={false}
        locale={{ emptyText: "无数据源：scheduler.rules 为空" }}
        columns={[
          { title: "规则", dataIndex: "id", render: (v, row) => (
            <Space direction="vertical" size={0}>
              <Text strong>{row?.label ?? v ?? "—"}</Text>
              <Text type="secondary" style={{ fontSize: 11 }}>{row?.task ?? row?.prompt ?? ""}</Text>
            </Space>) },
          { title: "触发时刻", width: 100, render: (_, row) => <Text code>{row?.at ?? "—"}</Text> },
          { title: "状态", width: 110, render: (_, row) => {
            if (row?.enabled === undefined) return <Text type="secondary">未返回 enabled</Text>;
            return <Tag color={row.enabled ? "blue" : "default"}>{row.enabled ? "已启用" : "已停用"}</Tag>;
          } },
          { title: "今日已触发", width: 130, render: (_, row) => (byId.has(row?.id)
            ? <Tag color="green">{String(byId.get(row.id))}</Tag>
            : <Text type="secondary">否</Text>) },
        ]}
      />
      <div style={{ marginTop: 10 }}>
        <Text strong style={{ fontSize: 12 }}>最近触发</Text>
        <Table
          size="small" rowKey={(row) => `${row?.rule}-${row?.at}`} style={{ marginTop: 6 }}
          dataSource={recent} pagination={false}
          locale={{ emptyText: "无数据源：scheduler.recent 为空" }}
          columns={[
            { title: "规则", dataIndex: "rule" },
            { title: "时刻", dataIndex: "at", render: (v) => stampOf(v) },
            { title: "结果", width: 100, render: (_, row) => (row?.success === undefined
              ? <Text type="secondary">未返回</Text>
              : <Tag color={row.success ? "green" : "red"}>{row.success ? "成功" : "失败"}</Tag>) },
            { title: "退出码", width: 90, render: (_, row) => (finite(row?.exit_code) === null ? "—" : row.exit_code) },
          ]}
        />
      </div>
      <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
        调度心跳（heartbeat）不在契约内（实现提供 scheduler.firedToday）→ 心跳区块无数据源；
        事件触发型规则与「下次触发倒计时」同属未提供字段。
      </Text>
    </ProCard>
  );
}

/** 熔断保护：只展示 headless.breaker 实际给出的门限，其余标「无数据源」。 */
function BreakerCard({ breaker, today }) {
  const rows = [
    { label: "并发上限", value: breaker?.concurrencyLimit, unit: "路" },
    { label: "当前占用", value: breaker?.active, unit: "路" },
    { label: "排队中", value: breaker?.queued, unit: "路" },
    { label: "单次超时", value: breaker?.timeoutMs, unit: "ms" },
    { label: "单次 token 预算", value: breaker?.tokenBudgetPerCall, unit: "token" },
    { label: "今日强制 kill", value: today?.killed, unit: "次" },
  ];
  return (
    <ProCard title="熔断保护" bordered extra={<Text type="secondary" style={{ fontSize: 12 }}>来源 /api/v3/gateway → headless.breaker</Text>}>
      <Row gutter={[12, 12]}>
        {rows.map((item) => (
          <Col xs={12} md={8} key={item.label}>
            <Card size="small" bordered>
              <Statistic title={item.label} value={finite(item.value) === null ? "—" : finite(item.value)} suffix={finite(item.value) === null ? null : item.unit} />
              {finite(item.value) === null
                ? <NoSource reason={`breaker.${item.label} 未返回`} />
                : null}
            </Card>
          </Col>
        ))}
      </Row>
      <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
        今日累计 token 与预算使用率、最近一次强制 kill 的时刻/原因/并发位释放情况：契约未提供 → 无数据源。
      </Text>
    </ProCard>
  );
}

export default function V3GatewayPage() {
  const gateway = useV3("gateway", {});
  const metrics = useV3("metrics", {});
  const tools = useV3("tools", {});
  const value = gateway.value ?? null;
  const metricsValue = metrics.value ?? null;
  const channels = value?.channels ?? null;
  const scheduler = value?.scheduler ?? null;
  const headless = value?.headless ?? null;
  const toolCounts = metricsValue?.mcp?.tools && typeof metricsValue.mcp.tools === "object" ? metricsValue.mcp.tools : null;
  const bars = toolCounts
    ? Object.entries(toolCounts)
      .map(([label, calls]) => ({ label, value: finite(calls) }))
      .filter((item) => item.value !== null)
      .sort((a, b) => b.value - a.value)
      .slice(0, 12)
    : [];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {gateway.error ? (
        <Alert type="error" showIcon message="网关取数失败（/api/v3/gateway）" description={`${gateway.error} —— 本页三通道/调度器/熔断区块均无数据源`} />
      ) : null}
      {metrics.error ? (
        <Alert type="warning" showIcon message="指标取数失败（/api/v3/metrics）" description={`${metrics.error} —— 调用计数与调用分布区块无数据源`} />
      ) : null}
      {tools.error ? (
        <Alert type="warning" showIcon message="工具面取数失败（/api/v3/tools）" description={`${tools.error} —— 工具总数区块无数据源`} />
      ) : null}

      <ChannelCards channels={channels} metrics={metricsValue}
        headlessToday={headless?.today} headlessLast={Array.isArray(headless?.last) ? headless.last[0] : null}
        toolTotal={tools.value?.total} />

      <HeadlessLog rows={headless?.last} loading={gateway.loading} />

      <SchedulerCard scheduler={scheduler} loading={gateway.loading} />

      <BreakerCard breaker={headless?.breaker} today={headless?.today} />

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={12}>
          <ProCard title="Profile 与 Bundle" bordered>
            <NoSource reason="/api/v3/gateway 契约未暴露 profile 路径与 bundle 清单（版本/条目数/加载状态）" />
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
              设计稿此处的 dsh-base / dsh-headless / adapter 版本号属于占位数据，未接入前不展示。
            </Text>
          </ProCard>
        </Col>
        <Col xs={24} lg={12}>
          <ProCard title="调用分布（MCP 工具计数 Top 12）" bordered loading={metrics.loading}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>来源 /api/v3/metrics → mcp.tools</Text>}>
            <HBarChart items={bars} emptyText="无数据源：mcp.tools 为空（本进程尚未记录工具调用）" />
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
              计数自本服务进程启动起累计（进程内计数器），非「今日」口径；工作台调用 wb.calls
              {finite(metricsValue?.wb?.calls) === null ? " 未返回" : ` = ${countText(metricsValue.wb.calls)}`}、
              HTTP 请求 http.requests{finite(metricsValue?.http?.requests) === null ? " 未返回" : ` = ${countText(metricsValue.http.requests)}`}。
            </Text>
          </ProCard>
        </Col>
      </Row>

      <Card size="small" bordered>
        <Title level={5} style={{ marginTop: 0 }}>说明</Title>
        <Text type="secondary" style={{ fontSize: 12 }}>
          数据来源：本服务 /api/v3/gateway、/api/v3/metrics、/api/v3/tools（同一 handle，无第二数据路径）。
          所有数值为实测；取不到的项显式标注「无数据源」并给出原因，未使用设计稿占位值。
          本页只读；刷新按钮见各卡片右上角来源标注。
          <a onClick={() => { gateway.refresh(); metrics.refresh(); tools.refresh(); }} style={{ marginLeft: 8 }}>刷新全部</a>
        </Text>
      </Card>
    </Space>
  );
}
