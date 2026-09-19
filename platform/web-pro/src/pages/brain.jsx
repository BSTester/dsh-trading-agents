// V3 工作台「决策大脑」页（Ant Design Pro）。
// 功能模块与设计稿版 /v3/brain.html **一一对应**（binder: platform/web/public/v3/brain.js）：
//   sec-1 会话控制条（会话 / 模型路由 / 推理强度 / 通道状态 / Trace）
//   sec-2 推理流（SDK events → Timeline；无事件时展示研究流水线 PDAT→PET 真实阶段）
//   sec-3 工具调用分布（metrics 真实计数）+ Headless 调用明细表（brain.headless.last）
//   sec-4 决策结论（decision.run.proposals：决策 / 依据 / 风险等级 / 建议操作 + 全部提案表；置信度无数据源）
//   sec-5 引用证据（流水线 / 因子矩阵与 IC / 资讯 / 审计链）
//   sec-6 运行元信息（serverInfo / route / 落盘回合 / token / 检查点）
//   sec-7 去向（导出决策快照 / 写入决策日志 / 复制 trace）
// 数据：GET /api/v3/*；取不到显式「无数据源 + 原因」，页面不含任何占位数字。
import React from "react";
import {
  Alert,
  App,
  Badge,
  Descriptions,
  Divider,
  Empty,
  Segmented,
  Space,
  Table,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { fmt, noSourceText, useV3 } from "../services/api.js";
import { BarList } from "../components/charts.jsx";

const { Text, Title } = Typography;

// 与设计稿 /v3/ 同一套 token（绿升红跌）
const C = {
  up: "#3fb950",
  down: "#f8514d",
  amber: "#d9a112",
  blue: "#4c8dff",
  purple: "#a371f7",
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

const asArray = (value) => (Array.isArray(value) ? value : []);

/** brain.sdk.events → Timeline 项（只读取任务需要的叶子字段，不整体序列化事件对象） */
function eventItem(event, index) {
  const kind = String(event.kind || event.type || "");
  const at = fmt.stamp(event.at || event.time || event.ts);
  const label = textOfEvent(event, kind);
  const tone =
    kind === "tool/result"
      ? "green"
      : kind === "tool/call"
        ? "orange"
        : kind === "turn/end"
          ? "gray"
          : "blue";
  return {
    color: tone,
    children: (
      <Space direction="vertical" size={2} key={`${kind}-${index}`}>
        <Space size={6} wrap>
          <Tag color={tone === "gray" ? "default" : tone}>{kind || "event"}</Tag>
          <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
            {at}
          </Text>
        </Space>
        <Text>{label}</Text>
      </Space>
    ),
  };
}

function textOfEvent(event, kind) {
  if (kind === "step/start") return String(event.title || event.label || event.step || "步骤开始");
  if (kind === "assistant/message") return String(event.text || event.message || event.content || "推理消息");
  if (kind === "tool/call") return `调用 ${String(event.tool || event.name || "工具")}`;
  if (kind === "tool/result")
    return `${String(event.tool || event.name || "工具")} → ${String(event.status || event.result || "已返回")}`;
  if (kind === "turn/end") return `回合结束${event.reason ? ` · ${String(event.reason)}` : ""}`;
  const fallback = event.text || event.message || event.tool || event.name;
  return fallback ? String(fallback) : "（该事件无可展示文本字段）";
}

export default function BrainPage() {
  const overview = useV3("overview", {});
  const metrics = useV3("metrics", {});
  const brain = useV3("brain", {});
  const strategy = useV3("strategy", {});
  const factors = useV3("factors/matrix", {});
  const audit = useV3("audit", { window: 120 });

  const ov = overview.value && overview.value.ok ? overview.value : null;
  const mt = metrics.value && metrics.value.ok ? metrics.value : null;
  const br = brain.value && brain.value.ok ? brain.value : null;
  const srun = strategy.value && strategy.value.ok ? strategy.value.run : null;
  const brunRaw = br ? br.decision : null;
  const brun = brunRaw && brunRaw.run ? brunRaw.run : brunRaw;
  const run = srun || (brun && brun.asOf ? brun : null);
  const auditRows = (audit.value && audit.value.ok && audit.value.data && audit.value.data.entries) || [];

  const proposals = asArray(run && run.proposals);
  const top = proposals[0] || null;
  const newsSymbol = String((top && top.ticker) || "SH.600519").replace(/^[A-Za-z]+\./, "");
  const news = useV3("news", { symbol: newsSymbol, limit: 5 });

  const sdkReason = (br && br.sdk && br.sdk.reason) || "本服务未挂载 SDK JSON-RPC 通道";
  const headlessReason =
    (br && br.headless && br.headless.reason) || "本服务未挂载 Headless CLI 子通道";
  const sdkTurns = asArray(br && br.sdk && br.sdk.turns);
  const sdkEvents = asArray(br && br.sdk && br.sdk.events);
  const headlessLast = asArray(br && br.headless && br.headless.last);
  const headlessToday = (br && br.headless && br.headless.today) || null;

  const { message } = App.useApp();

  const refreshAll = () => {
    overview.refresh();
    metrics.refresh();
    brain.refresh();
    strategy.refresh();
    factors.refresh();
    audit.refresh();
    news.refresh();
  };

  /* ── sec-1 会话控制条 ── */
  const sessionOptions = [
    {
      value: "strategy-run",
      label: `研究流水线 · ${run ? fmt.stamp(run.asOf) : "无数据源"}（/api/v3/strategy 最近一轮）`,
    },
    { value: "sdk", label: `SDK 会话 · ${sdkTurns[0] && sdkTurns[0].sessionId ? sdkTurns[0].sessionId : "无数据源（通道未挂载）"}` },
    { value: "headless", label: "Headless 任务 · 无数据源（通道未挂载）" },
  ];
  const channelPills = [
    {
      key: "mcp",
      ok: Boolean(mt && Number.isFinite(Number(mt.mcp && mt.mcp.avgMs))),
      text: `MCP ${mt && Number.isFinite(Number(mt.mcp && mt.mcp.avgMs)) ? `${fmt.num(mt.mcp.avgMs, 0)}ms` : "无数据源"}`,
      title: "workbench 工具面平均耗时（/api/v3/metrics）",
    },
    { key: "sdk", ok: false, text: "SDK 无数据源", title: sdkReason },
    { key: "headless", ok: false, text: "Headless 无数据源", title: headlessReason },
  ];

  /* ── sec-2 推理流：研究流水线 PDAT → PET（真实落盘阶段） ── */
  const stages = (run && run.stages) || {};
  const universe = asArray(stages.PDAT && stages.PDAT.universe).length
    ? asArray(stages.PDAT.universe)
    : asArray(run && run.universe);
  const pipelineItems = [];
  if (run) {
    const pdat = stages.PDAT || {};
    const paat = stages.PAAT || {};
    const pcpt = stages.PCPT || {};
    const prt = stages.PRT || {};
    const time = fmt.stamp(run.asOf).slice(11, 19);
    pipelineItems.push({
      color: "blue",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color="blue">观察</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {time} · 耗时无数据源
            </Text>
          </Space>
          <Text>
            数据准备（PDAT）：标的池 <b style={MONO}>{universe.length}</b> 只（
            {universe.slice(0, 4).join("、") || "—"}
            {universe.length > 4 ? " 等" : ""}）· 日 K <b style={MONO}>{fmt.dash(pdat.bars)}</b> 根 · 取数错误{" "}
            <b style={MONO}>{asArray(pdat.errors).length}</b> 条
          </Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            数据源：workbench series（/api/v3/strategy）
          </Text>
        </Space>
      ),
    });
    pipelineItems.push({
      color: "purple",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color="purple">推理</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {time} · 耗时无数据源
            </Text>
          </Space>
          <Text>
            因子分析（PAAT）：已分析 <b style={MONO}>{fmt.dash(paat.analyzed)}</b> 只 · 有因子{" "}
            <b style={MONO}>{fmt.dash(paat.withFactors)}</b> 只 · 评分来源{" "}
            <b style={MONO}>{String(paat.scoreSource || "—")}</b>
          </Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            因子错误：{paat.factorsError ? String(paat.factorsError) : "0 条"}
          </Text>
        </Space>
      ),
    });
    pipelineItems.push({
      color: "orange",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color="orange">行动</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {time} · 耗时无数据源
            </Text>
          </Space>
          <Text>
            规则候选（PCPT）：增持 <b style={MONO}>{asArray(pcpt.longs).length}</b> 只（
            {asArray(pcpt.longs).join("、") || "—"}）· 减持 <b style={MONO}>{asArray(pcpt.reduces).length}</b> 只（
            {asArray(pcpt.reduces).join("、") || "—"}）
          </Text>
        </Space>
      ),
    });
    pipelineItems.push({
      color: "orange",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color="orange">行动</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {time} · 耗时无数据源
            </Text>
          </Space>
          <Text>
            目标权重（PRT）：每标的 <b style={MONO}>{fmt.dash(prt.weightPctPerName)}%</b> · 组合上限约束{" "}
            <b>{prt.capped ? "已启用" : "未启用"}</b>
          </Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            仓位由 V3 研究流水线本地计算，不构成下单指令
          </Text>
        </Space>
      ),
    });
    pipelineItems.push({
      color: "green",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color="green">反馈</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {time} · 耗时无数据源
            </Text>
          </Space>
          <Text>
            评估产出（PET）：<b style={MONO}>{proposals.length}</b> 条调仓建议已成文，待人工审批（研究侧不下单、不启用策略）
          </Text>
        </Space>
      ),
    });
  }

  /* ── sec-3 工具调用分布（真实计数） ── */
  const toolRows = [
    ...Object.entries((mt && mt.mcp && mt.mcp.tools) || {}),
    ...Object.entries((mt && mt.wb && mt.wb.byTool) || {}).map(([name, count]) => [`wb:${name}`, count]),
  ]
    .map(([name, count]) => [name, Number(count) || 0])
    .sort((a, b) => b[1] - a[1])
    .slice(0, 6);
  const mcpCalls = Number(mt && mt.mcp ? mt.mcp.calls : 0) || 0;
  const mcpErrors = Number(mt && mt.mcp ? mt.mcp.errors : 0) || 0;

  /* ── sec-4 决策结论 ── */
  const riskWorst = proposals.some((item) => String(item.riskLevel || "").indexOf("高") >= 0)
    ? "高"
    : proposals.some((item) => String(item.riskLevel || "").indexOf("中") >= 0)
      ? "中"
      : proposals.length
        ? "低"
        : null;
  const increases = proposals.filter((item) => String(item.action || "").indexOf("增") >= 0).length;
  const decreases = proposals.filter((item) => String(item.action || "").indexOf("减") >= 0).length;

  const proposalColumns = [
    {
      title: "标的",
      dataIndex: "ticker",
      width: 120,
      render: (value) => (
        <Text strong style={MONO}>
          {fmt.dash(value)}
        </Text>
      ),
    },
    {
      title: "决策",
      dataIndex: "action",
      width: 90,
      render: (value) => (
        <Tag color={String(value || "").indexOf("增") >= 0 ? "success" : String(value || "").indexOf("减") >= 0 ? "error" : "default"}>
          {fmt.dash(value)}
        </Tag>
      ),
    },
    {
      title: "目标权重",
      dataIndex: "targetWeightPct",
      width: 110,
      align: "right",
      render: (value) => <span style={MONO}>{numOr(value, 1)}%</span>,
    },
    {
      title: "风险等级",
      dataIndex: "riskLevel",
      width: 100,
      render: (value) => (
        <Tag color={value === "高" ? "error" : value === "中" ? "warning" : "success"}>{fmt.dash(value)}</Tag>
      ),
    },
    { title: "依据", dataIndex: "basis", render: (value) => <span style={{ fontSize: 12 }}>{String(value || "—")}</span> },
    {
      title: "建议操作",
      dataIndex: "action_hint",
      width: 220,
      render: (value) => <Text type="secondary" style={{ fontSize: 11 }}>{String(value || "经人工审批后执行")}</Text>,
    },
  ];

  /* ── sec-5 引用证据 ── */
  const evidence = [];
  if (run) {
    const pdat = stages.PDAT || {};
    const paat = stages.PAAT || {};
    evidence.push({
      tag: "因子",
      color: "blue",
      title: `研究流水线 PDAT/PAAT：标的池 ${asArray(run.universe).length} 只 · 日 K ${fmt.dash(pdat.bars)} 根 · 评分来源 ${String(paat.scoreSource || "—")}`,
      meta: `/api/v3/strategy · ${fmt.stamp(run.asOf)}`,
    });
  }
  const fv = factors.value && factors.value.ok ? factors.value : null;
  if (fv && fv.matrix) {
    const ic = fv.ic || {};
    evidence.push({
      tag: "因子",
      color: "blue",
      title: `横截面因子 z 矩阵：${asArray(fv.matrix.tickers).length} 只 × ${asArray(fv.matrix.factors).length} 因子 · IC（${String(ic.factor || "—")}，forward ${fmt.dash(ic.forwardDays)} 日）均值 ${numOr(ic.meanIc, 4)} · IR ${numOr(ic.ir, 2)} · 样本 ${fmt.dash(ic.observations)} 期`,
      meta: `${String(fv.matrix.source || "/api/v3/factors/matrix")} · as_of ${String(fv.matrix.as_of || "—")}`,
    });
  }
  const newsRows = (news.value && news.value.ok && asArray(news.value.rows)) || [];
  for (const row of newsRows.slice(0, 3)) {
    evidence.push({
      tag: "新闻",
      color: "orange",
      title: String(row.title || "—"),
      meta: `${String(row.source || "资讯源")} · ${String(row.published_at || "—")}`,
    });
  }
  for (const entry of auditRows.slice(0, 3)) {
    evidence.push({
      tag: "审计",
      color: "default",
      title: `${entry.source_label || entry.kind || "审计"} · ${entry.ticker ? `${entry.ticker} · ` : ""}${String(entry.detail || "")}`,
      meta: `工作台 audit · ${fmt.stamp(entry.at)}`,
    });
  }
  if (br && br.sources && br.sources.decision) {
    evidence.push({ tag: "记录", color: "green", title: `决策记录：${String(br.sources.decision)}`, meta: "本服务落盘路径" });
  }

  /* ── sec-7 去向：导出真实快照 ── */
  const exportSnapshot = () => {
    const payload = {
      exported_at: new Date().toISOString(),
      source: "/api/v3/strategy（研究流水线最近一轮）· /api/v3/brain（通道与来源）",
      as_of: run ? run.asOf : null,
      equity_current: (ov && ov.equity && ov.equity.current) || null,
      run: run || null,
    };
    const name = `v3-decision-snapshot-${run && run.asOf ? String(run.asOf).replace(/[:.]/g, "-") : "nodata"}.json`;
    try {
      const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      message.success(`决策快照已导出：${name}`);
    } catch (error) {
      message.error(`导出失败：${String((error && error.message) || error)}`);
    }
  };
  const copyTrace = () => {
    const text = run ? `strategy-run@${run.asOf}` : "无数据源";
    try {
      if (window.navigator && navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).catch(() => {});
      }
    } catch (error) {
      /* 剪贴板不可用不影响提示 */
    }
    message.info(run ? `已复制研究轮标识：${text}` : "trace：无数据源（SDK / Headless 通道未挂载）");
  };
  const writeLog = () => {
    message.info(
      run
        ? `决策日志：本服务无写入接口；研究产物落盘于 ${String(run.asOf)} 的流水线记录`
        : "决策日志：本服务无写入接口，且当前无流水线记录",
    );
  };

  const mode = String((ov && ov.mode) || "").toUpperCase();

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {/* 页头：模式 / 权益 / 三通道（对应设计稿 .topbar） */}
      <ProCard
        bordered
        bodyStyle={{ padding: "10px 16px" }}
        loading={overview.loading || metrics.loading}
        title={
          <Title level={5} style={{ margin: 0 }}>
            决策大脑
          </Title>
        }
        extra={
          <Space size={12} wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>
              平台是身体，DeepSeek Harness 是决策大脑 · 一次决策的完整推理：观察 → 推理 → 行动 → 反馈
            </Text>
            <Badge status={mode === "live" ? "error" : "processing"} text={mode === "live" ? "LIVE 实盘" : mode ? "SIM 模拟盘" : "模式未知"} />
            <Text style={{ ...MONO, fontSize: 12 }}>权益 {fmt.money(ov && ov.equity && ov.equity.current)}</Text>
            <Typography.Link onClick={refreshAll}>刷新</Typography.Link>
          </Space>
        }
      >
        {overview.error ? <BlockError name="决策大脑页头（/api/v3/overview）" error={overview.error} /> : null}
        <Space size={8} wrap>
          {channelPills.map((pill) => (
            <Tooltip key={pill.key} title={pill.title}>
              <Tag color={pill.ok ? "success" : "warning"} style={{ marginInlineEnd: 0 }}>
                {pill.text}
              </Tag>
            </Tooltip>
          ))}
          <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
            数据时点 {fmt.stamp(ov && ov.generated_at)}
          </Text>
        </Space>
      </ProCard>

      {/* sec-1 会话控制条 */}
      <ProCard title="决策会话" bordered loading={brain.loading || strategy.loading}>
        <Space size={16} wrap align="start" style={{ width: "100%" }}>
          <Space direction="vertical" size={4}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              会话
            </Text>
            <Segmented
              options={sessionOptions.map((item) => ({ label: item.label, value: item.value }))}
              defaultValue="strategy-run"
            />
          </Space>
          <Descriptions
            size="small"
            column={2}
            colon={false}
            style={{ flex: 1, minWidth: 420 }}
            items={[
              {
                key: "model",
                label: "模型路由",
                children: (
                  <Tooltip title="本服务未挂载 SDK / Headless 会话通道：本页不提供模型型号与路由">
                    <Text type="secondary">无数据源</Text>
                  </Tooltip>
                ),
              },
              {
                key: "effort",
                label: "推理强度",
                children: (
                  <Tooltip title="本服务未挂载 SDK / Headless 会话通道：本页不提供推理强度">
                    <Text type="secondary">无数据源</Text>
                  </Tooltip>
                ),
              },
              {
                key: "session",
                label: "Session id",
                children: (
                  <Text type="secondary" style={MONO}>
                    {sdkTurns[0] && sdkTurns[0].sessionId ? String(sdkTurns[0].sessionId) : "无数据源"}
                  </Text>
                ),
              },
              {
                key: "trace",
                label: "Trace",
                children: (
                  <Tooltip title="没有 SDK / Headless 会话，trace id 无数据源">
                    <Text type="secondary" style={MONO}>
                      无数据源
                    </Text>
                  </Tooltip>
                ),
              },
              {
                key: "channels",
                label: "通道状态",
                span: 2,
                children: (
                  <Space size={6} wrap>
                    <Tag color={mt && Number.isFinite(Number(mt.mcp && mt.mcp.avgMs)) ? "success" : "warning"}>
                      MCP {mt && Number.isFinite(Number(mt.mcp && mt.mcp.avgMs)) ? `${fmt.num(mt.mcp.avgMs, 0)}ms` : "无数据源"}
                    </Tag>
                    <Tag color="warning">SDK 无数据源</Tag>
                    <Tag color="warning">Headless 无数据源</Tag>
                  </Space>
                ),
              },
            ]}
          />
        </Space>
        <Divider style={{ margin: "12px 0" }} />
        <Space size={8} wrap>
          <Tag color={run ? "success" : "default"}>
            {run ? `研究流水线已完成 · 提案 ${proposals.length} 条` : "无数据源 · 研究流水线尚无落盘记录"}
          </Tag>
          <Text type="secondary" style={{ fontSize: 11 }}>
            来源 /api/v3/strategy（as_of {run ? fmt.stamp(run.asOf) : "—"}）· SDK：{sdkReason} · Headless：{headlessReason}
          </Text>
        </Space>
      </ProCard>

      {/* sec-2 推理流 */}
      <ProCard
        title="推理流 · 研究流水线 PDAT → PET"
        bordered
        loading={strategy.loading || brain.loading}
        extra={<Text type="secondary" style={{ fontSize: 11 }}>本轮 {pipelineItems.length} 个阶段</Text>}
      >
        {strategy.error ? <BlockError name="推理流（/api/v3/strategy）" error={strategy.error} /> : null}
        {pipelineItems.length === 0 ? (
          <NoSource what="推理流" why="GET /api/v3/strategy 返回 run=null：研究流水线尚无落盘记录" />
        ) : (
          <Timeline items={pipelineItems} />
        )}
        <Divider style={{ margin: "12px 0 8px" }} orientation="left" orientationMargin={0}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            Agent Loop 事件流（/api/v3/brain · sdk.events）
          </Text>
        </Divider>
        {brain.error ? (
          <BlockError name="Agent Loop 事件流（/api/v3/brain）" error={brain.error} />
        ) : sdkEvents.length === 0 ? (
          <NoSource
            what="Agent Loop 事件流（step/start · assistant/message · tool/call · tool/result · turn/end）"
            why={sdkReason}
          />
        ) : (
          <Timeline items={sdkEvents.map(eventItem)} />
        )}
      </ProCard>

      {/* sec-3 工具调用分布 + Headless 调用明细表 */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 12 }}>
        <ProCard
          title="工具调用分布（当日进程累计）"
          bordered
          loading={metrics.loading}
          extra={
            <Text type="secondary" style={{ fontSize: 11 }}>
              共 {mcpCalls} 次调用 · 失败 {mcpErrors} 次 · 平均 {fmt.dash(mt && mt.mcp && mt.mcp.avgMs)}ms
            </Text>
          }
        >
          {metrics.error ? (
            <BlockError name="工具调用分布（/api/v3/metrics）" error={metrics.error} />
          ) : toolRows.length === 0 ? (
            <NoSource what="工具调用分布" why="GET /api/v3/metrics 未返回任何工具调用计数（进程内计数为空）" />
          ) : (
            <>
              <BarList items={toolRows} />
              <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
                服务端只暴露调用计数与平均耗时（mcp.tools + wb.byTool），不提供逐次耗时瀑布
              </Text>
            </>
          )}
        </ProCard>

        <ProCard
          title="Headless 调用明细"
          bordered
          loading={brain.loading}
          extra={
            <Text type="secondary" style={{ fontSize: 11 }}>
              今日 {headlessToday ? headlessToday.total : "无数据源"} 次 · 成功 {headlessToday ? headlessToday.success : "—"} / 失败{" "}
              {headlessToday ? headlessToday.failed : "—"} · 平均 {headlessToday && Number(headlessToday.total) > 0 ? `${headlessToday.avgMs}ms` : "无数据源"}
            </Text>
          }
        >
          {brain.error ? (
            <BlockError name="Headless 调用明细（/api/v3/brain）" error={brain.error} />
          ) : headlessLast.length === 0 ? (
            <NoSource what="Headless 调用明细（headless.last）" why={`${headlessReason}；今日唤醒 0 次`} />
          ) : (
            <Table
              size="small"
              rowKey={(row, index) => String(row.id || row.at || index)}
              dataSource={headlessLast}
              pagination={false}
              columns={[
                { title: "时间", dataIndex: "at", width: 170, render: (value) => <span style={MONO}>{fmt.stamp(value)}</span> },
                { title: "任务", dataIndex: "task", render: (value, row) => String(value || row.name || row.tool || "—") },
                { title: "状态", dataIndex: "status", width: 100, render: (value) => <Tag>{String(value || "—")}</Tag> },
                { title: "耗时", dataIndex: "ms", width: 90, align: "right", render: (value) => <span style={MONO}>{fmt.dash(value)}</span> },
              ]}
            />
          )}
        </ProCard>
      </div>

      {/* sec-4 决策结论 */}
      <ProCard
        title="决策结论"
        bordered
        loading={strategy.loading}
        extra={
          <Space size={8} wrap>
            <Text type="secondary" style={{ fontSize: 11 }}>
              {run ? `as_of ${fmt.stamp(run.asOf)} 产出` : "无数据源"}
            </Text>
            <Tag color={proposals.length ? "blue" : "default"}>
              {proposals.length ? `增持 ${increases} / 减持 ${decreases}` : "无数据源"}
            </Tag>
          </Space>
        }
      >
        {strategy.error ? <BlockError name="决策结论（/api/v3/strategy）" error={strategy.error} /> : null}
        {!run || proposals.length === 0 ? (
          <NoSource what="决策结论" why="研究流水线未返回任何调仓建议提案（可经工作台受约束入口生成一轮）" />
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.4fr) minmax(0, 1fr)", gap: 16 }}>
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  依据（{proposals.length} 条提案）
                </Text>
                <ul style={{ margin: "8px 0 0", paddingInlineStart: 18, fontSize: 12, lineHeight: 1.9 }}>
                  {proposals.map((item) => (
                    <li key={`${item.ticker}-${item.action}`}>
                      <Text strong style={MONO}>
                        {fmt.dash(item.ticker)}
                      </Text>{" "}
                      <Text strong>{fmt.dash(item.action)}</Text> 目标权重 <span style={MONO}>{numOr(item.targetWeightPct, 1)}%</span> · 风险{" "}
                      <span style={MONO}>{fmt.dash(item.riskLevel)}</span>：{String(item.basis || "—")}
                      <Text type="secondary" style={{ display: "block", fontSize: 11 }}>
                        {String(item.action_hint || "经人工审批后执行")}
                      </Text>
                    </li>
                  ))}
                </ul>
              </div>
              <Descriptions
                size="small"
                column={1}
                colon={false}
                items={[
                  {
                    key: "risk",
                    label: "风险等级",
                    children: (
                      <Tag color={riskWorst === "低" ? "success" : riskWorst === "中" ? "warning" : "error"}>{fmt.dash(riskWorst)}</Tag>
                    ),
                  },
                  {
                    key: "action",
                    label: "建议操作",
                    children: (
                      <Text strong style={MONO}>
                        {`${fmt.dash(top.ticker)} ${fmt.dash(top.action)} → 目标权重 ${numOr(top.targetWeightPct, 1)}%`}
                      </Text>
                    ),
                  },
                  {
                    key: "detail",
                    label: "本轮",
                    children: (
                      <Text style={{ fontSize: 12 }}>
                        {proposals.length} 条提案（增持 {increases} / 减持 {decreases}）· 每标的权重上限{" "}
                        <span style={MONO}>{fmt.dash(stages.PRT && stages.PRT.weightPctPerName)}%</span> · 待人工审批，研究侧不下单
                      </Text>
                    ),
                  },
                  {
                    key: "confidence",
                    label: "置信度",
                    children: (
                      <Tooltip title="研究流水线不输出置信度，/api/v3/strategy 与 /api/v3/brain 均无该字段">
                        <Text type="secondary">无数据源</Text>
                      </Tooltip>
                    ),
                  },
                ]}
              />
            </div>
            <Divider style={{ margin: "14px 0 10px" }} orientation="left" orientationMargin={0}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                全部提案
              </Text>
            </Divider>
            <Table
              size="small"
              rowKey={(row) => `${row.ticker}-${row.action}`}
              dataSource={proposals}
              columns={proposalColumns}
              pagination={false}
              scroll={{ x: 900 }}
            />
          </>
        )}
      </ProCard>

      {/* sec-5 引用证据 */}
      <ProCard
        title="引用证据"
        bordered
        loading={factors.loading || news.loading || audit.loading}
        extra={<Text type="secondary" style={{ fontSize: 11 }}>共 {evidence.length} 条</Text>}
      >
        {evidence.length === 0 ? (
          <NoSource what="引用证据" why="研究流水线、因子矩阵、资讯与审计链均未返回可用条目" />
        ) : (
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            {evidence.map((item, index) => (
              <Space key={`${item.tag}-${index}`} size={10} align="start">
                <Tag color={item.color}>{item.tag}</Tag>
                <div>
                  <Text style={{ fontSize: 12 }}>{item.title}</Text>
                  <Text type="secondary" style={{ ...MONO, fontSize: 11, display: "block" }}>
                    {item.meta}
                  </Text>
                </div>
              </Space>
            ))}
          </Space>
        )}
      </ProCard>

      {/* sec-6 运行元信息 */}
      <ProCard title="运行元信息" bordered loading={brain.loading}>
        {brain.error ? <BlockError name="运行元信息（/api/v3/brain）" error={brain.error} /> : null}
        <Descriptions
          size="small"
          column={2}
          colon={false}
          items={[
            {
              key: "server",
              label: "serverInfo",
              children: <Text type="secondary">{br && br.sdk && br.sdk.serverInfo ? String(br.sdk.serverInfo) : "无数据源"}</Text>,
            },
            {
              key: "route",
              label: "模型路由 route",
              children: <Text type="secondary">{br && br.sdk && br.sdk.route ? String(br.sdk.route) : "无数据源"}</Text>,
            },
            {
              key: "turns",
              label: "落盘回合数（turns）",
              children: (
                <Text type="secondary" style={MONO}>
                  {`${sdkTurns.length} 条`}
                  {sdkTurns.length === 0 ? "（通道未挂载，无会话回合）" : ""}
                </Text>
              ),
            },
            {
              key: "lastTurn",
              label: "最近回合 lastTurn",
              children: <Text type="secondary">{br && br.sdk && br.sdk.lastTurn ? fmt.stamp(br.sdk.lastTurn) : "无数据源"}</Text>,
            },
            {
              key: "token",
              label: "已用 token",
              children: <Text type="secondary">无数据源（会话通道未挂载）</Text>,
            },
            {
              key: "checkpoint",
              label: "检查点 / Compaction",
              children: <Text type="secondary">无数据源（会话通道未挂载）</Text>,
            },
            {
              key: "decision",
              label: "决策记录落盘",
              span: 2,
              children: (
                <Text type="secondary" style={MONO}>
                  {br && br.sources && br.sources.decision ? String(br.sources.decision) : "无数据源"}
                </Text>
              ),
            },
          ]}
        />
        <Alert
          style={{ marginTop: 10 }}
          type="warning"
          showIcon
          message={noSourceText("SDK / Headless 会话元信息（session id · Agent Loop 轮次 · Compaction · token · 检查点）", `SDK：${sdkReason}；Headless：${headlessReason}`)}
          description={
            <Text type="secondary" style={{ fontSize: 11 }}>
              本页只提供研究流水线的真实落盘记录（见推理流与决策结论），不用设计稿里的占位 session / 轮次 / token 顶替。
            </Text>
          }
        />
      </ProCard>

      {/* sec-7 去向 */}
      <ProCard
        title="本结论去向"
        bordered
        extra={
          <Space size={8} wrap>
            <Typography.Link onClick={exportSnapshot}>导出决策快照</Typography.Link>
            <Typography.Link onClick={writeLog}>写入决策日志</Typography.Link>
            <Typography.Link onClick={copyTrace}>复制 trace</Typography.Link>
          </Space>
        }
      >
        <Space direction="vertical" size={4}>
          <Text style={{ fontSize: 12 }}>
            {run
              ? `依据本结论生成的调仓建议 · ${proposals.length} 条提案（as_of ${fmt.stamp(run.asOf)}）→ 工作台受约束入口人工审批后才可能执行`
              : "本结论去向 · 无数据源（研究流水线尚无落盘记录）"}
          </Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            平台不在此页下单：执行动作只能经工作台既有受约束入口（人工审批）完成
          </Text>
        </Space>
      </ProCard>
    </Space>
  );
}
