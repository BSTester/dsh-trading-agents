// 研究页：研究 run 记录（snapshot.runs）+ 已发布研报（snapshot.reports）+ 报告详情。
// 字段依据（页面每个取值路径均可指到源码行）：
//   runs → plugins/workbench/src/store.js beginResearch 写入 {id, ticker, session_id,
//     mode, status: "running", started_at}；publishResearch 置 status="completed"；
//     cancelRun/cancelStaleRuns 置 status="cancelled" 且附 settled_at；
//     withRunStatus 把超时仍 running 的读视图派生为 "abandoned"（不改写历史）。
//     snapshot() 已按当前模式过滤并倒序（最新在前）。
//   reports → store.js publishResearch 写入 {id: run.id, ticker, mode, session_id,
//     rating, rating_label(产出侧中文标签), report(markdown 正文), sources:[{name, as_of,
//     reference}], published_at}；snapshot() 同样按模式过滤并倒序。
// 正文渲染用 src/lib/markdown.jsx（自 plugins/workbench/src/client.js 移植，逻辑零改动）。
// 评级标签：优先 rating_label（产出侧给出）；缺失时回退原始 rating 码展示，不猜枚举。
import React from "react";
import { Button, Card, Space, Table, Tag, Typography } from "antd";
import { useSnapshotPoll } from "../services/hooks.js";
import { Markdown } from "../lib/markdown.jsx";

// status 枚举值全部来自 store.js 字面量；中文为展示标签（依据 store.js 各自注释语义）。
const RUN_STATUS = {
  running: { label: "进行中", color: "blue" },
  completed: { label: "已发布", color: "green" },
  cancelled: { label: "已取消", color: "default" },
  abandoned: { label: "已中断", color: "orange" },
};
const MODE_LABEL = { sim: "模拟", live: "实盘" };
const MODE_COLOR = { sim: "green", live: "red" };

function timeOf(value) {
  return typeof value === "string" ? value.slice(0, 19).replace("T", " ") : "—";
}

function RunsTable({ snapshot }) {
  const runs = snapshot.value?.runs ?? [];
  return (
    <Table size="small"
      rowKey={(row) => row.id}
      dataSource={runs}
      pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
      locale={{ emptyText: snapshot.loading ? "研究记录加载中…" : "暂无研究记录。" }}
      columns={[
        { title: "开始时间", key: "started_at", render: (_f, row) => timeOf(row.started_at) },
        { title: "标的", key: "ticker", render: (_f, row) => row.ticker ?? "—" },
        { title: "状态", key: "status",
          render: (_f, row) => {
            const known = RUN_STATUS[row.status];
            return known
              ? <Tag color={known.color}>{known.label}</Tag>
              : <Tag>{row.status ?? "—"}</Tag>;
          } },
        { title: "模式", key: "mode",
          render: (_f, row) => (
            <Tag color={MODE_COLOR[row.mode] ?? "default"}>{MODE_LABEL[row.mode] ?? row.mode ?? "—"}</Tag>) },
        { title: "结束时间", key: "settled_at",
          render: (_f, row) => (row.settled_at ? timeOf(row.settled_at) : "—") },
        { title: "Run ID", key: "id",
          render: (_f, row) => (
            <Typography.Text copyable={{ text: row.id }} type="secondary" style={{ fontSize: 12 }}>
              {String(row.id ?? "—").slice(0, 8)}
            </Typography.Text>) },
      ]} />
  );
}

/** 报告详情：返回按钮 + Markdown 正文 + 来源列表（name/as_of/reference）。 */
function ReportDetail({ report, onBack }) {
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Space wrap>
        <Button onClick={onBack}>返回列表</Button>
        <Tag color="blue">{report.rating_label ?? report.rating ?? "—"}</Tag>
        <Typography.Text strong>{report.ticker ?? "—"}</Typography.Text>
        <Typography.Text type="secondary">
          发布 {timeOf(report.published_at)} · 会话 {String(report.session_id ?? "—").slice(0, 8)}
        </Typography.Text>
      </Space>
      <Card type="inner" title="研报正文">
        <Markdown text={report.report} />
      </Card>
      <Card type="inner" title={`数据来源（${(report.sources ?? []).length} 条）`}>
        <ol style={{ margin: 0, paddingLeft: 20 }}>
          {(report.sources ?? []).map((source, index) => (
            <li key={`${source.name}-${index}`}>
              <Typography.Text strong>{source.name ?? "—"}</Typography.Text>
              <Typography.Text type="secondary"> · {source.as_of ?? "—"} · </Typography.Text>
              {/^https?:\/\//i.test(source.reference ?? "")
                ? <a href={source.reference} target="_blank" rel="noopener noreferrer">{source.reference}</a>
                : <Typography.Text code>{source.reference ?? "—"}</Typography.Text>}
            </li>))}
        </ol>
        {(report.sources ?? []).length === 0 && (
          <Typography.Text type="secondary">未记录来源。</Typography.Text>)}
      </Card>
    </Space>);
}

export default function ResearchPage() {
  const snapshot = useSnapshotPoll();
  const reports = snapshot.value?.reports ?? [];
  const [selectedId, setSelectedId] = React.useState(null);
  const detail = selectedId ? reports.find((row) => row.id === selectedId) ?? null : null;
  const running = (snapshot.value?.runs ?? []).filter((run) => run.status === "running");
  return (
    <Card title="研究">
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {snapshot.error && (
          <Typography.Text type="danger">快照读取失败：{snapshot.error}</Typography.Text>)}
        {detail ? (
          <ReportDetail report={detail} onBack={() => setSelectedId(null)} />
        ) : (
          <>
            <Card type="inner" title="研究运行记录"
              extra={running.length > 0 && (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  进行中：{running.map((run) => `${run.ticker}（${timeOf(run.started_at)}）`).join("、")}
                </Typography.Text>)}>
              <RunsTable snapshot={snapshot} />
            </Card>
            <Card type="inner" title="已发布研报（点击查看详情）">
              <Table size="small"
                rowKey={(row) => row.id}
                dataSource={reports}
                pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
                locale={{ emptyText: snapshot.loading
                  ? "研报加载中…"
                  : "暂无研报；在 Harness 会话中要求完整投研并发布后，会显示在这里。" }}
                columns={[
                  { title: "发布时间", key: "published_at", render: (_f, row) => timeOf(row.published_at) },
                  { title: "标的", key: "ticker", render: (_f, row) => row.ticker ?? "—" },
                  { title: "评级", key: "rating",
                    render: (_f, row) => <Tag color="blue">{row.rating_label ?? row.rating ?? "—"}</Tag> },
                  { title: "来源数", key: "sources",
                    align: "right", render: (_f, row) => (row.sources ?? []).length },
                  { title: "操作", key: "open",
                    render: (_f, row) => (
                      <Button size="small" onClick={() => setSelectedId(row.id)}>查看</Button>) },
                ]} />
            </Card>
          </>
        )}
      </Space>
    </Card>);
}
