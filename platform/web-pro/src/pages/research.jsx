// V3 工作台「研究报告」页（Ant Design Pro）——研报的**查看**入口。
//
// 能力边界（与仓库纪律一致，页面内也写明）：
//   * **生成**研报走 Harness 对话：skills/trading-agents（12 角色 / 6 阶段）→
//     research_publish(run_id, ticker, rating, report, sources) 落工作台 store；
//     值勤队列（daily_brief / factor_patrol / mining_round）由 install/research-duty.timer
//     驱动 `dsh --profile headless` 消费。本页**不提供生成入口**（避免出现第二套指令入口）。
//   * 本页只读展示：研报（markdown 正文 + 来源）、研究 run、量化预览、活动流、值勤队列。
// 数据：GET /api/v3/research（工作台快照：runs/reports/previews/activity）
//      GET /api/v3/research/tasks（值勤队列，HTTP-only 读端点）
import React from "react";
import { Alert, Badge, Button, Col, Descriptions, Empty, Row, Space, Table, Tag, Tooltip, Typography } from "antd";
import { ProCard } from "@ant-design/pro-components";
import { fmt, noSourceText, useV3 } from "../services/api.js";
import { Markdown } from "../lib/markdown.jsx";

const { Text } = Typography;

/** run 状态展示：服务端只给 running/completed/cancelled；running 且超 2 小时按「已中断」派生展示
 *  （与工作台既有口径一致：只改读视图，不改写磁盘数据）。 */
function runStatus(run) {
  const started = Date.parse(run?.started_at ?? "");
  const ageMinutes = Number.isFinite(started) ? (Date.now() - started) / 60000 : null;
  const raw = String(run?.status ?? "unknown");
  if (raw === "running" && ageMinutes !== null && ageMinutes > 120) {
    return { text: "已中断（超 2 小时仍 running）", color: "orange", derived: true, raw };
  }
  const map = {
    completed: { text: "已完成", color: "green" },
    running: { text: "进行中", color: "processing" },
    cancelled: { text: "已取消", color: "default" },
    abandoned: { text: "已中断", color: "orange" },
  };
  return { ...(map[raw] ?? { text: raw, color: "default" }), raw };
}

function ratingTag(report) {
  const label = report?.rating_label ?? report?.rating ?? "—";
  const code = String(report?.rating ?? "").toLowerCase();
  const color = code.includes("buy") || code.includes("overweight") ? "green"
    : code.includes("sell") || code.includes("underweight") ? "red" : "default";
  return <Tag color={color}>{String(label)}</Tag>;
}

export default function ResearchPage() {
  const research = useV3("research", {});
  const tasks = useV3("research/tasks", {});
  const [selectedId, setSelectedId] = React.useState(null);

  const reports = Array.isArray(research.value?.reports) ? research.value.reports : [];
  const runs = Array.isArray(research.value?.runs) ? research.value.runs : [];
  const previews = Array.isArray(research.value?.previews) ? research.value.previews : [];
  const activity = Array.isArray(research.value?.activity) ? research.value.activity : [];
  const queue = Array.isArray(tasks.value?.tasks) ? tasks.value.tasks : [];
  const selected = reports.find((item) => item.id === selectedId) ?? reports[0] ?? null;
  const running = runs.filter((run) => runStatus(run).raw === "running").length;

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {research.error ? <Alert type="error" showIcon message="研报数据读取失败" description={String(research.error)} /> : null}
      {research.value?.recording_error ? (
        <Alert type="warning" showIcon message="工作台记录写入异常" description={String(research.value.recording_error)} />
      ) : null}

      <Alert type="info" showIcon
        message="研报由 Harness 对话生成，本页只读查看"
        description={
          <Text type="secondary" style={{ fontSize: 12 }}>
            「出一份研报 / 深度分析某标的」在对话里由 trading-agents 技能执行（12 角色 / 6 阶段：4 分析师 →
            多空辩论 → 研究经理裁决 → 交易员提案 → 风控辩论 → 组合经理终审），终审后经
            <Text code>research_publish</Text>落库；本页只展示已发布结果与量化预览，不提供生成按钮、不下单、不启用策略。
          </Text>
        } />

      <ProCard bordered>
        <Row gutter={[12, 12]}>
          {[
            { title: "已发布研报", value: reports.length, hint: "research_publish 落库的记录" },
            { title: "研究 run", value: runs.length, hint: running > 0 ? `其中进行中 ${running}` : "无进行中" },
            { title: "量化预览", value: previews.length, hint: "signal / backtest / report" },
            { title: "活动流", value: activity.length, hint: "工作台记录的事件" },
            { title: "值勤队列", value: queue.length, hint: "docs：日报/因子巡检/周度挖掘" },
          ].map((item) => (
            <Col key={item.title} xs={12} md={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>{item.title}</Text>
              <div style={{ fontSize: 20, fontVariantNumeric: "tabular-nums" }}>{item.value}</div>
              <Text type="secondary" style={{ fontSize: 11 }}>{item.hint}</Text>
            </Col>
          ))}
        </Row>
        <Text type="secondary" style={{ fontSize: 11 }}>
          模式 {fmt.dash(research.value?.mode)} · 快照时间 {fmt.stamp(research.value?.generated_at)} · 来源 {fmt.dash(research.value?.source)}
          {research.value?.notice ? ` · notice：${research.value.notice}` : ""}
        </Text>
      </ProCard>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={9}>
          <ProCard title="研报列表" bordered extra={<Text type="secondary" style={{ fontSize: 11 }}>按发布时间倒序</Text>}>
            <Table
              size="small" rowKey={(row) => row.id} loading={research.loading}
              dataSource={reports} pagination={false}
              locale={{ emptyText: <Empty imageStyle={{ display: "none" }} description="尚无已发布研报（在对话里让 trading-agents 出一份）" /> }}
              onRow={(row) => ({ onClick: () => setSelectedId(row.id), style: { cursor: "pointer" } })}
              columns={[
                { title: "标的", dataIndex: "ticker", width: 110 },
                { title: "评级", width: 90, render: (_, row) => ratingTag(row) },
                { title: "发布时间", dataIndex: "published_at", width: 150, render: (v) => fmt.stamp(v) },
                { title: "来源数", width: 80, render: (_, row) => (row.sources ?? []).length },
              ]} />
          </ProCard>
        </Col>
        <Col xs={24} lg={15}>
          <ProCard title={selected ? `研报正文 · ${selected.ticker}` : "研报正文"} bordered
            extra={selected ? <Button size="small" onClick={() => setSelectedId(selected.id)}>当前选中</Button> : null}>
            {selected ? (
              <Space direction="vertical" size={10} style={{ width: "100%" }}>
                <Descriptions size="small" column={2} bordered
                  items={[
                    { key: "id", label: "run id", children: <Text code>{selected.id}</Text> },
                    { key: "rating", label: "评级", children: ratingTag(selected) },
                    { key: "mode", label: "模式", children: fmt.dash(selected.mode) },
                    { key: "session", label: "会话", children: fmt.dash(selected.session_id) },
                    { key: "published", label: "发布时间", children: fmt.stamp(selected.published_at) },
                    { key: "sources", label: "来源数", children: (selected.sources ?? []).length },
                  ]} />
                <div style={{ background: "#12161d", border: "1px solid #232b37", borderRadius: 8, padding: "10px 14px" }}>
                  <Markdown text={String(selected.report ?? "")} />
                </div>
                <Table size="small" rowKey={(row) => `${row.name}-${row.reference}`} pagination={false}
                  dataSource={selected.sources ?? []} locale={{ emptyText: "该研报未附来源（发布时要求至少一项）" }}
                  columns={[
                    { title: "来源", dataIndex: "name", width: 160 },
                    { title: "数据时间", dataIndex: "as_of", width: 130 },
                    { title: "引用", dataIndex: "reference", ellipsis: true },
                  ]} />
              </Space>
            ) : (
              <Empty imageStyle={{ display: "none" }} description={noSourceText("研报正文", "尚无已发布研报")} />
            )}
          </ProCard>
        </Col>
      </Row>

      <ProCard title="研究 run（记录进度与中断）" bordered>
        <Table size="small" rowKey={(row) => row.id} loading={research.loading} dataSource={runs} pagination={false}
          locale={{ emptyText: "尚无研究 run 记录" }}
          columns={[
            { title: "run id", dataIndex: "id", width: 300, render: (v) => <Text code>{v}</Text> },
            { title: "标的", dataIndex: "ticker", width: 110 },
            { title: "状态", width: 200, render: (_, row) => {
              const status = runStatus(row);
              return <Tooltip title={status.derived ? "读视图派生：超 2 小时仍 running（不改写磁盘数据）" : null}>
                <Badge status={status.color} text={status.text} />
              </Tooltip>;
            } },
            { title: "开始", dataIndex: "started_at", width: 160, render: (v) => fmt.stamp(v) },
            { title: "结束", dataIndex: "settled_at", width: 160, render: (v) => fmt.stamp(v) },
            { title: "会话", dataIndex: "session_id", ellipsis: true },
          ]} />
        <Text type="secondary" style={{ fontSize: 11 }}>
          被中断的会话会永久停在 running：在对话里用 research_cancel（list / cancel / cancel_stale）处理；本页只展示。
        </Text>
      </ProCard>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={12}>
          <ProCard title="量化预览（不下单）" bordered>
            <Table size="small" rowKey={(row) => row.id} loading={research.loading} dataSource={previews} pagination={false}
              locale={{ emptyText: "尚无量化预览" }}
              columns={[
                { title: "时间", dataIndex: "at", width: 150, render: (v) => fmt.stamp(v) },
                { title: "类型", dataIndex: "kind", width: 90, render: (v) => <Tag>{fmt.dash(v)}</Tag> },
                { title: "标的", width: 110, render: (_, row) => fmt.dash(row.value?.ticker ?? row.value?.symbol) },
                { title: "策略", width: 130, render: (_, row) => fmt.dash(row.value?.strategy_label ?? row.value?.strategy) },
                { title: "来源", dataIndex: "execution_source", ellipsis: true, render: (v) => fmt.dash(v) },
              ]} />
            <Text type="secondary" style={{ fontSize: 11 }}>
              预览来自 quant_signal / quant_backtest / quant_report 等只读调用，仅作研究记录，不代表已下单或已启用策略。
            </Text>
          </ProCard>
        </Col>
        <Col xs={24} lg={12}>
          <ProCard title="活动流" bordered>
            <Table size="small" rowKey={(row) => row.id} loading={research.loading} dataSource={activity.slice(0, 15)} pagination={false}
              locale={{ emptyText: "尚无活动记录" }}
              columns={[
                { title: "时间", dataIndex: "at", width: 150, render: (v) => fmt.stamp(v) },
                { title: "事件", dataIndex: "kind", width: 150 },
                { title: "标的", dataIndex: "ticker", width: 110, render: (v) => fmt.dash(v) },
              ]} />
          </ProCard>
        </Col>
      </Row>

      <ProCard title="值勤研究队列（daily_brief / factor_patrol / mining_round）" bordered
        extra={<Text type="secondary" style={{ fontSize: 11 }}>消费入口：install/research-duty.timer → dsh --profile headless</Text>}>
        {tasks.error ? <Alert type="warning" showIcon message={noSourceText("值勤队列", String(tasks.error))} /> : (
          <Table size="small" rowKey={(row) => row.task_id} loading={tasks.loading} dataSource={queue} pagination={false}
            locale={{ emptyText: "队列为空" }}
            columns={[
              { title: "任务", dataIndex: "task_id", width: 260, render: (v) => <Text code>{v}</Text> },
              { title: "类型", dataIndex: "kind", width: 130, render: (v) => <Tag color="blue">{fmt.dash(v)}</Tag> },
              { title: "as_of", dataIndex: "as_of", width: 110 },
              { title: "市场", dataIndex: "market", width: 90 },
              { title: "状态", dataIndex: "status", width: 110, render: (v) => fmt.dash(v) },
              { title: "标的数", width: 90, render: (_, row) => (row.payload?.symbols ?? []).length },
            ]} />
        )}
        <Text type="secondary" style={{ fontSize: 11 }}>
          队列由基础链作业按交易日入队；领取与回报（claim / report）只进 Harness 工具面——本页只读清单，不领取、不回报。
        </Text>
      </ProCard>
    </Space>
  );
}
