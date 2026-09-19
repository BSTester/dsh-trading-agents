// V3「系统概览」页（Ant Design Pro 版）：平台（身体）× Harness（决策大脑）全局状态。
// 数据全部来自 GET /api/v3/overview（服务端复用既有工具面，同一 handle，无第二数据路径）。
// 设计稿对应页：od-quant-harness-platform / index.html（KPI 行 + 三通道 + 时间线 + 风控红线 +
// Agent Loop + 待审批 + 数据源健康）。缺失数据源的位置显式声明「无数据源」，不留占位数字。
import React from "react";
import { Alert, Badge, Card, Col, Descriptions, Progress, Row, Space, Statistic, Table, Tag, Typography } from "antd";
import { ProCard } from "@ant-design/pro-components";
import { useV3 } from "../../services/v3api.js";
import { num, stampOf } from "../../services/format.jsx";

const { Text, Title } = Typography;

function numOf(value, digits = 2) {
  return Number.isFinite(Number(value)) ? Number(Number(value).toFixed(digits)) : null;
}

function pctText(value, digits = 2) {
  const n = numOf(value, digits);
  return n === null ? "—" : `${n}%`;
}

function moneyText(value) {
  const n = numOf(value, 2);
  return n === null ? "—" : `¥${n.toLocaleString("zh-CN")}`;
}

export default function V3OverviewPage() {
  const { value, loading, error, refresh } = useV3("overview", {});
  const equity = value?.equity ?? null;
  const points = Array.isArray(equity?.points) ? equity.points : [];
  const last = points.at(-1) ?? null;
  const prev = points.at(-2) ?? null;
  const dailyPct = last && prev && Number(prev.equity) ? ((Number(last.equity) - Number(prev.equity)) / Number(prev.equity)) * 100 : null;
  const positions = value?.positions ?? null;
  const positionRows = Array.isArray(positions?.positions) ? positions.positions : [];
  const planValue = value?.plan ?? null;
  const plans = Array.isArray(planValue?.plans) ? planValue.plans : [];
  const frozen = plans.filter((p) => p.status === "frozen");
  const sources = value?.sources ?? null;
  const schedule = value?.schedule ?? null;
  const health = sources?.summary ?? null;

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {error ? <Alert type="error" showIcon message="概览取数失败" description={error} /> : null}
      {value?.errors?.length ? (
        <Alert type="warning" showIcon
          message={`部分工具不可用（${value.errors.length}）`}
          description={value.errors.map((e) => `${e.tool}: ${e.error?.message ?? e.error?.code ?? "未知"}`).join("；")} />
      ) : null}

      <Row gutter={[12, 12]}>
        {[
          { title: "台账权益", value: moneyText(equity?.current), suffix: value?.mode ? `${value.mode.toUpperCase()} 模式` : null },
          { title: "当日盈亏", value: dailyPct === null ? "—" : `${dailyPct >= 0 ? "+" : "−"}${Math.abs(dailyPct).toFixed(2)}%`,
            suffix: dailyPct === null ? "台账点位不足，无前一日基准" : `基准 ${prev?.t ?? "—"}`,
            color: dailyPct === null ? undefined : dailyPct >= 0 ? "#3fb950" : "#f8514d" },
          { title: "夏普比率（台账）", value: equity?.sharpe === undefined || equity?.sharpe === null ? "—" : numOf(equity.sharpe, 2), suffix: `成交 ${equity?.trades ?? "—"} 笔` },
          { title: "最大回撤（台账）", value: equity?.max_drawdown === undefined ? "—" : pctText(Math.abs(Number(equity.max_drawdown)) * 100),
            suffix: "阈值 15%", color: "#f8514d" },
          { title: "持仓条目", value: positionRows.length, suffix: positions?.counts?.accounts_checked ? `账户 ${positions.counts.accounts_checked} 个` : "工作台 positions" },
        ].map((item) => (
          <Col key={item.title} xs={24} sm={12} md={8} lg={4} xl={4}>
            <ProCard bordered>
              <Statistic title={item.title} value={item.value} valueStyle={item.color ? { color: item.color } : undefined} />
              <Text type="secondary" style={{ fontSize: 12 }}>{item.suffix ?? ""}</Text>
            </ProCard>
          </Col>
        ))}
      </Row>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={8}>
          <ProCard title="三通道状态" bordered>
            <Descriptions column={1} size="small">
              <Descriptions.Item label="MCP 工具面">
                <Badge status="processing" text="运行中" /> {`（工具 ${value?.snapshot?.value?.tools ?? "—"} 个）`}
              </Descriptions.Item>
              <Descriptions.Item label="SDK JSON-RPC">
                <Badge status="default" text="无数据源" /> <Text type="secondary">FastAPI 服务未挂载 SDK 通道客户端</Text>
              </Descriptions.Item>
              <Descriptions.Item label="Headless CLI">
                <Badge status={schedule?.heartbeat?.alive === false ? "error" : "processing"} text={schedule?.heartbeat ? "调度心跳可见" : "无数据源"} />
              </Descriptions.Item>
            </Descriptions>
          </ProCard>
        </Col>
        <Col xs={24} lg={8}>
          <ProCard title="风控红线" bordered>
            <Space direction="vertical" style={{ width: "100%" }}>
              <div>
                <Text type="secondary">最大回撤（台账）/ 阈值 15%</Text>
                <Progress percent={equity?.max_drawdown ? Math.min(100, Math.abs(Number(equity.max_drawdown)) * 100 / 15 * 100) : 0}
                  showInfo={false} strokeColor="#f8514d" size="small" />
              </div>
              <div>
                <Text type="secondary">单笔交易上限 2%（OMS 台账单笔占比）</Text>
                <Progress percent={0} showInfo={false} strokeColor="#d9a112" size="small" />
                <Text type="secondary" style={{ fontSize: 12 }}>平台侧 OMS 台账未挂载到本服务 → 无数据源</Text>
              </div>
              <div>
                <Text type="secondary">单一行业暴露 20%</Text>
                <Text type="secondary" style={{ fontSize: 12, display: "block" }}>工作台未提供行业维度敞口 → 无数据源</Text>
              </div>
            </Space>
          </ProCard>
        </Col>
        <Col xs={24} lg={8}>
          <ProCard title="数据源健康" bordered>
            {health ? (
              <Space size="large">
                <Statistic title="正常" value={health.ok ?? 0} valueStyle={{ color: "#3fb950" }} />
                <Statistic title="告警" value={health.warn ?? 0} valueStyle={{ color: "#d9a112" }} />
                <Statistic title="失败" value={health.fail ?? 0} valueStyle={{ color: "#f8514d" }} />
              </Space>
            ) : <Text type="secondary">sources 工具不可用 → 无数据源</Text>}
            <div style={{ marginTop: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                推送：{value?.push?.enabled ? "已启用" : "未启用"} · 调度心跳：{schedule?.heartbeat ? "有" : "无"}
              </Text>
            </div>
          </ProCard>
        </Col>
      </Row>

      <ProCard title="冻结计划（待执行入口在工作台 Web）" bordered extra={<a onClick={refresh}>刷新</a>}>
        <Table
          size="small" rowKey="plan_id" loading={loading}
          dataSource={frozen}
          pagination={false}
          locale={{ emptyText: "当前无冻结计划" }}
          columns={[
            { title: "计划", dataIndex: "plan_id" },
            { title: "模式", dataIndex: "mode" },
            { title: "状态", dataIndex: "status", render: (v) => <Tag color={v === "frozen" ? "gold" : "default"}>{v}</Tag> },
            { title: "订单数", render: (_, row) => (row.orders ?? []).length },
            { title: "目标权重", render: (_, row) => Object.entries(row.target ?? {}).map(([k, v]) => `${k} ${(Number(v) * 100).toFixed(2)}%`).join("、") || "—" },
            { title: "创建时间", dataIndex: "created_at", render: (v) => stampOf(v) },
          ]}
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          执行只发生在工作台 Web 的「执行已冻结计划」+ 人工确认（live 需口令）；本页只读。
        </Text>
      </ProCard>

      <ProCard title="持仓（Top）" bordered>
        <Table
          size="small" rowKey={(row) => `${row.symbol ?? row.ticker}-${row.qty}`}
          dataSource={positionRows.slice(0, 8)} pagination={false} loading={loading}
          locale={{ emptyText: "当前无持仓或工作台未返回" }}
          columns={[
            { title: "标的", render: (_, row) => row.symbol ?? row.ticker ?? "—" },
            { title: "名称", render: (_, row) => row.name ?? "—" },
            { title: "数量", render: (_, row) => num(row.qty) },
            { title: "市值", render: (_, row) => moneyText(row.market_value) },
            { title: "浮动盈亏", render: (_, row) => (row.pl_val === undefined ? "—" : <Text type={Number(row.pl_val) >= 0 ? "success" : "danger"}>{moneyText(row.pl_val)}</Text>) },
          ]}
        />
      </ProCard>

      <Card size="small" bordered>
        <Title level={5} style={{ marginTop: 0 }}>说明</Title>
        <Text type="secondary" style={{ fontSize: 12 }}>
          数据来源：本服务 /api/v3/overview（复用既有 56 工具面的同一 handle）。所有数值为实测；
          取不到的项显式标注「无数据源」并说明原因，不使用估算值。
        </Text>
      </Card>
    </Space>
  );
}
