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
//
// 深度数据（WP12 任务 6）：F10 三个 section 的按需查询卡。载荷契约来自服务端
// （app.py 的 f10_detail 白名单与 futu_data.FutuData.f10_detail）：**{code, section, params?}**
// ——字段名是 code（服务端内部再转 symbol，勿传 symbol 会被白名单拒绝）。
// section 白名单以传输层 OpenApiF10.SECTIONS（26 项）为单一事实源，本页只取研究关注的三项；
// 字段名与枚举依据、空结果语义、通道不可用的下一步指引全部在 services/f10.js 文件头登记，
// 映射逻辑是可直测纯函数（tests/f10.test.mjs），本文件只做接线与渲染。
import React from "react";
import { Alert, Button, Card, Popconfirm, Space, Table, Tag, Typography,
         message } from "antd";
import { callApi } from "../services/api.js";
import { useEndpoint, useSnapshotPoll } from "../services/hooks.js";
import { Markdown } from "../lib/markdown.jsx";
import { RawCollapse } from "../lib/raw-collapse.jsx";
import { SymbolInput } from "../components/SymbolInput.jsx";
import {
  analystConsensusSummary, dataplaneHint, institutionalSummary, ratingSummarySummary,
} from "../services/f10.js";
import {
  decideDisabledReason, ruleRows, ruleRowsForTable, statusColor, statusLabel,
  validationSummary,
} from "../services/rules.js";

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

/** 报告详情：返回按钮 + Markdown 正文 + 来源列表（name/as_of/reference）。 */function ReportDetail({ report, onBack }) {
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

// ---------------------------------------------------------------------------
// 深度数据（F10 聚合面）——三项研究关注的 section，映射纯函数在 services/f10.js
// ---------------------------------------------------------------------------

/** 键值行（缺值由纯函数给 —，这里不补默认值）。 */
function KeyValueRows({ rows }) {
  return (
    <Space direction="vertical" size={2} style={{ width: "100%" }}>
      {rows.map((row) => (
        <Space key={row.label} size={8} align="start">
          <Typography.Text type="secondary" style={{ minWidth: 180, display: "inline-block" }}>
            {row.label}
          </Typography.Text>
          <Typography.Text>{row.value}</Typography.Text>
        </Space>))}
    </Space>);
}

/** 评级汇总明细表（逐机构/逐分析师一行；字段来自官方 rating-summary 文档）。 */
function RatingRowsTable({ rows }) {
  return (
    <Table size="small"
      rowKey={(_row, index) => index}
      dataSource={rows}
      pagination={false}
      locale={{ emptyText: "本期无评级明细。" }}
      columns={[
        { title: "来源", key: "source", render: (_f, row) => row.source },
        { title: "名称", key: "name", render: (_f, row) => row.name },
        { title: "评级", key: "rating", render: (_f, row) => row.rating },
        { title: "目标价", key: "target", align: "right", render: (_f, row) => row.target },
        { title: "推荐日", key: "date", render: (_f, row) => row.date },
      ]} />);
}

/** 单 section 查询卡：错误给「服务端原因 + 下一步」，空结果给如实说明，不显示为失败。 */
function DeepDataCard({ title, section, code, summarize, asTable = false }) {
  const query = useEndpoint("f10_detail", code ? { code, section } : null, [code, section]);
  const summary = query.value ? summarize(query.value) : null;
  const hint = query.error ? dataplaneHint(query.error) : "";
  return (
    <Card type="inner" title={title}>
      {query.error && (
        <Alert type="error" showIcon message={`读取失败：${query.error}`}
          description={hint || undefined} />)}
      {summary?.note && <Alert type="info" showIcon message={summary.note} />}
      {summary && !summary.note && (
        asTable
          ? (
            <>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                机构 {summary.institutionCount} 条 · 分析师 {summary.analystCount} 条 ·
                上游 total={summary.total}
              </Typography.Text>
              <RatingRowsTable rows={summary.rows} />
            </>)
          : <KeyValueRows rows={summary.rows} />)}
      <RawCollapse value={query.value} loading={query.loading} />
    </Card>);
}

/** 深度数据区：输入标的后并发取三个 section（每个卡独立失败，互不牵连）。 */
function DeepData() {
  const [ticker, setTicker] = React.useState("");
  const [code, setCode] = React.useState("");
  const submit = (text) => setCode((typeof text === "string" ? text : ticker).trim().toUpperCase());
  return (
    <Card type="inner" title="深度数据（F10）"
      extra={(
        <Space>
          <SymbolInput placeholder="标的代码，如 HK.00700 / US.AAPL" style={{ width: 240 }} value={ticker}
            onChange={setTicker} onPressEnter={submit} />
          <Button onClick={() => submit()}>查询</Button>
        </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          数据经富途 OpenAPI 数据面（futu_channel=openapi 时可用）；接口不可用时如实显示原因。
        </Typography.Text>
        {!code && <Typography.Text type="secondary">输入标的代码后查询。</Typography.Text>}
        {code && (
          <DeepDataCard title="分析师一致预期" section="analyst_consensus" code={code}
            summarize={analystConsensusSummary} />)}
        {code && (
          <DeepDataCard title="评级汇总（逐机构/逐分析师）" section="rating_summary" code={code}
            summarize={ratingSummarySummary} asTable />)}
        {code && (
          <DeepDataCard title="机构持股" section="institutional" code={code}
            summarize={institutionalSummary} />)}
      </Space>
    </Card>);
}

/**
 * 规则候选池（WP14 任务 4）：研究院产出的规则提案 + 验证结论 + **人工批准**。
 *
 * 数据来自只读端点 ``rules``；批准/停用调 ``rules-decide``（服务端唯一放行条件是
 * ``passed → enabled``，页面侧按钮禁用只是不把用户引向必然失败的操作，服务端仍独立复核）。
 * **本按钮就是批准的唯一入口**：``rules-decide`` 是服务进程内动作端点，没有 CLI 子命令、
 * 也不进模型工具面——批准来源由服务端固定，前端不传、也传不了 ``by``。
 * 这里只做接线与渲染：状态标签/摘要/可用性判定在 services/rules.js（可直测纯函数）。
 */
function RulePool() {
  const query = useEndpoint("rules", {}, []);
  const [busy, setBusy] = React.useState(false);
  const decide = async (rule, decision) => {
    setBusy(true);
    try {
      await callApi("rules-decide", { rule_id: rule.rule_id, decision });
      message.success(decision === "enable" ? "已启用该规则" : "已停用该规则");
      query.refresh();
    } catch (error) {
      message.error(`操作失败：${error.message || error}`);
    } finally {
      setBusy(false);
    }
  };
  const rules = ruleRowsForTable(query.value?.rules);
  return (
    <Card type="inner" title="规则候选池"
      extra={(
        <Space>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            验证通过后仍需在此批准才会进入自动计划
          </Typography.Text>
          <Button size="small" onClick={query.refresh}>刷新</Button>
        </Space>)}>
      {query.error && (
        <Alert type="error" showIcon message={`候选池读取失败：${query.error}`} />)}
      <Table size="small"
        rowKey="key"
        dataSource={rules}
        pagination={{ pageSize: 5, hideOnSinglePage: true, showSizeChanger: false }}
        locale={{ emptyText: query.loading
          ? "候选池加载中…"
          : "暂无规则提案；研究院产出提案并跑 rules-validate 后会显示在这里。" }}
        expandable={{
          expandedRowRender: (rule) => (
            <Space direction="vertical" size={2}>
              {ruleRows(rule).map((row) => (
                <Space key={row.label} size={8} align="start">
                  <Typography.Text type="secondary"
                    style={{ minWidth: 120, display: "inline-block" }}>
                    {row.label}
                  </Typography.Text>
                  <Typography.Text>{row.value}</Typography.Text>
                </Space>))}
            </Space>),
        }}
        columns={[
          { title: "规则", key: "rule_id", render: (_f, rule) => rule.rule_id ?? "—" },
          { title: "状态", key: "status",
            render: (_f, rule) => (
              <Tag color={statusColor(rule.status)}>{statusLabel(rule.status)}</Tag>) },
          { title: "验证结论", key: "validation",
            render: (_f, rule) => (
              <Typography.Text style={{ fontSize: 12 }}>
                {validationSummary(rule)}
              </Typography.Text>) },
          { title: "操作", key: "decide", width: 200,
            render: (_f, rule) => {
              const enableBlocked = decideDisabledReason(rule, "enable");
              const disableBlocked = decideDisabledReason(rule, "disable");
              return (
                <Space>
                  <Popconfirm title={`批准启用「${rule.rule_id}」？启用后自动计划将采用该规则。`}
                    okText="批准启用" cancelText="取消" disabled={busy || !!enableBlocked}
                    onConfirm={() => decide(rule, "enable")}>
                    <Button size="small" type="primary" disabled={busy || !!enableBlocked}
                      title={enableBlocked ?? undefined}>批准启用</Button>
                  </Popconfirm>
                  <Popconfirm title={`停用「${rule.rule_id}」？停用后不再被自动计划采用；停用不可逆，需换新 rule_id 才能重新启用。`}
                    okText="停用" cancelText="取消" disabled={busy || !!disableBlocked}
                    onConfirm={() => decide(rule, "disable")}>
                    <Button size="small" disabled={busy || !!disableBlocked}
                      title={disableBlocked ?? undefined}>停用</Button>
                  </Popconfirm>
                </Space>);
            } },
        ]} />
    </Card>);
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
            <RulePool />
            <DeepData />
          </>
        )}
      </Space>
    </Card>);
}
