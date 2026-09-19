// V3「决策大脑」页（Ant Design Pro 版）：平台（身体）× DeepSeek Harness（决策大脑）的一次决策全流程。
// 数据接口：GET /api/v3/brain（后端复用既有工具面/会话账本，无第二数据路径）。
// 设计稿对应页：od-quant-harness-platform/brain.html ——
//   页头控制条 → 三栏（推理流 Timeline / 决策结论 / 运行元信息）→ 会话输出 → 工具调用分布 → 调用明细。
// 纪律：取不到的区块显式写「无数据源」并说明原因（通道未挂载 / 字段未提供），不编造任何数字。
import React from "react";
import {
  Alert, Badge, Col, Descriptions, Progress, Row, Select, Space, Statistic, Table, Tag, Timeline, Tooltip, Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { useV3 } from "../../services/v3api.js";
import { num, stampOf } from "../../services/format.jsx";
import { HBarChart } from "../../charts/bars.jsx";
import { TimelineChart } from "../../charts/timeline.jsx";

const { Text, Paragraph } = Typography;

/** 推理流步骤分类：行动（工具调用）/ 推理（reasoning）/ 反馈（回合结束、错误）/ 观察（其余）。 */
const STEP_META = {
  observe: { text: "观察", color: "#39a0ed" },
  think: { text: "推理", color: "#a371f7" },
  act: { text: "行动", color: "#4c8dff" },
  fb: { text: "反馈", color: "#d9a112" },
};

/** 已知字段的中文标签；未知字段原样展示字段名（不猜语义）。 */
const LABEL = {
  total: "总数", success: "成功", failed: "失败", avgMs: "平均耗时(ms)", killed: "被终止",
  at: "时间", sessionId: "会话", kind: "类型", code: "代码", answer: "回答", toolCalls: "工具调用",
  status: "状态", reason: "原因", model: "模型", provider: "提供方", reasoningEffort: "推理强度",
  maxTokens: "最大 token", serverInfo: "服务信息", route: "路由", lastTurn: "最近回合",
  duration_ms: "耗时(ms)", tokens_estimate: "估算 token", breaker: "熔断器", mode: "模式",
  method: "方法", version: "版本", name: "名称", count: "计数", ok: "正常", error: "错误",
  from: "开始", to: "结束", window: "窗口", limit: "上限", source: "来源", asOf: "时点",
};

function numOr(value) {
  if (value === null || value === undefined || value === "") return null;
  return Number.isFinite(Number(value)) ? Number(value) : null;
}

function fixedText(value, digits = 2) {
  const n = numOr(value);
  return n === null ? null : n.toFixed(digits);
}

function stampValue(value) {
  return value === null || value === undefined || value === "" ? "—" : stampOf(value);
}

function hhmmss(value) {
  const match = /T(\d{2}:\d{2}:\d{2})/.exec(String(value ?? ""));
  return match ? match[1] : "—";
}

function clipText(value, limit) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

function plainText(value) {
  return String(value ?? "")
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\*\*([^*]*)\*\*/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

/** 未知形状的对象 → 只取标量叶子（不展开嵌套结构，避免把接口内部结构当数据展示）。 */
function leafPairs(source, limit = 8) {
  const pairs = [];
  for (const [key, value] of Object.entries(source ?? {})) {
    if (pairs.length >= limit) break;
    if (value === null || ["string", "number", "boolean"].includes(typeof value)) {
      pairs.push({ key, label: LABEL[key] ?? key, value });
    }
  }
  return pairs;
}

/** 会话回合：真实字段 at / sessionId / kind / code / answer / toolCalls。 */
function turnRows(turns) {
  return (Array.isArray(turns) ? turns : []).map((turn, index) => {
    const calls = Array.isArray(turn?.toolCalls) ? turn.toolCalls : [];
    return {
      key: `${turn?.sessionId ?? "session"}-${turn?.at ?? index}-${index}`,
      at: turn?.at ?? null,
      sessionId: turn?.sessionId ?? null,
      kind: turn?.kind ?? null,
      code: turn?.code ?? null,
      answer: turn?.answer ?? null,
      calls,
      callNames: calls.map((call) => (typeof call === "string" ? call : String(call?.name ?? call?.tool ?? "工具调用"))),
    };
  });
}

/** 事件流：params.event.{type,data.message.content[]}；turn/end 等带 data.reason。 */
function eventRows(events) {
  return (Array.isArray(events) ? events : []).map((entry, index) => {
    const payload = entry?.params?.event ?? null;
    const data = payload?.data ?? {};
    const content = Array.isArray(data?.message?.content) ? data.message.content : [];
    return {
      key: `${entry?.at ?? index}-${index}`,
      at: entry?.at ?? null,
      method: entry?.method ?? null,
      type: String(payload?.type ?? ""),
      reason: data?.reason ?? null,
      texts: content.filter((item) => item?.type === "text" && item.text).map((item) => String(item.text)),
      thoughts: content.filter((item) => item?.type === "reasoning" && item.text).map((item) => String(item.text)),
      tools: content.filter((item) => item?.type === "tool-call")
        .map((item) => ({ name: item?.name ?? null })),
    };
  });
}

function stepKindOf(row) {
  if (row.tools.length) return "act";
  if (row.thoughts.length) return "think";
  if (/turn\/end|turn\/error|error|fail/i.test(row.type)) return "fb";
  return "observe";
}

function MetaChip({ label, value, title }) {
  const shown = value === null || value === undefined || value === "" ? "无数据源" : String(value);
  return (
    <Tooltip title={title}>
      <div style={{ minWidth: 0 }}>
        <Text type="secondary" style={{ fontSize: 11, display: "block" }}>{label}</Text>
        <Text style={{ fontSize: 12 }}>{shown}</Text>
      </div>
    </Tooltip>
  );
}

/** 缺失单元格：显式写「无数据源」，不留空、不填 0。 */
function CellText({ value, fallback = "无数据源" }) {
  if (value === null || value === undefined || value === "") {
    return <Text type="secondary" style={{ fontSize: 12 }}>{fallback}</Text>;
  }
  return <span>{String(value)}</span>;
}

function NoSource({ what, why }) {
  return (
    <Alert type="warning" showIcon style={{ marginBottom: 8 }}
      message={`${what}：无数据源`}
      description={why ? <Text type="secondary" style={{ fontSize: 12 }}>{why}</Text> : undefined} />
  );
}

export default function V3决策大脑Page() {
  const { value, loading, error, refresh } = useV3("brain", {});
  const [session, setSession] = React.useState("");

  const sdk = value?.sdk ?? null;
  const route = sdk?.route ?? null;
  const headless = value?.headless ?? null;
  const today = headless?.today ?? null;
  const lastRun = Array.isArray(headless?.last) ? (headless.last[0] ?? null) : null;
  const decision = value?.decision ?? null;
  const proposal = Array.isArray(decision?.proposals) ? (decision.proposals[0] ?? null) : null;
  const stages = decision?.stages ?? null;
  const paat = stages?.PAAT ?? null;
  const pdat = stages?.PDAT ?? null;
  const sources = value?.sources ?? null;

  const turns = React.useMemo(() => turnRows(sdk?.turns), [sdk?.turns]);
  const events = React.useMemo(() => eventRows(sdk?.events), [sdk?.events]);
  const unavailable = sdk ? String(sdk.status).toLowerCase() === "unavailable" : false;
  const reason = sdk?.reason ?? null;

  const sessionOptions = React.useMemo(() => {
    const seen = new Set();
    const options = [];
    for (const turn of turns) {
      if (!turn.sessionId || seen.has(turn.sessionId)) continue;
      seen.add(turn.sessionId);
      options.push({
        value: turn.sessionId,
        label: `${turn.sessionId} · ${turn.kind ?? "—"} · ${stampValue(turn.at)}`,
      });
    }
    return options;
  }, [turns]);

  const visibleTurns = React.useMemo(
    () => (session ? turns.filter((turn) => turn.sessionId === session) : turns),
    [turns, session],
  );

  // 事件流优先（粒度更细）；没有事件时退回真实回合内容。两路**不叠加计数**，避免同一批调用被算两次。
  const steps = React.useMemo(() => {
    if (events.length) {
      return events.map((row) => ({ key: row.key, kind: stepKindOf(row), row }));
    }
    const built = [];
    for (const turn of visibleTurns) {
      const calls = turn.calls;
      const seq = [];
      for (const name of turn.callNames) {
        if (seq.length && seq[seq.length - 1].name === name) seq[seq.length - 1].calls += 1;
        else seq.push({ name, calls: 1 });
      }
      built.push({
        key: `${turn.key}-head`, kind: "observe",
        text: `真实回合开始 · 会话 ${turn.sessionId ?? "—"} · 类型 ${turn.kind ?? "—"} · 工具调用 ${calls.length} 次`,
        at: turn.at,
      });
      const answer = plainText(turn.answer);
      if (answer) {
        built.push({ key: `${turn.key}-answer`, kind: "think", text: clipText(answer, 900), at: turn.at });
      }
      if (seq.length) {
        built.push({
          key: `${turn.key}-tools`, kind: "act", at: turn.at,
          tools: seq.map((item) => ({ name: `${item.name} ×${item.calls}` })),
        });
      }
    }
    return built;
  }, [events, visibleTurns]);

  const toolSource = events.some((row) => row.tools.length)
    ? { name: "sdk.events", unit: "事件流 tool-call 条目" }
    : (turns.some((turn) => turn.callNames.length) ? { name: "sdk.turns[].toolCalls", unit: "回合工具调用记录" } : null);

  const toolCounts = React.useMemo(() => {
    const map = new Map();
    const add = (name) => {
      const key = String(name ?? "工具调用");
      map.set(key, (map.get(key) ?? 0) + 1);
    };
    if (events.some((row) => row.tools.length)) {
      for (const row of events) for (const tool of row.tools) add(tool.name);
    } else {
      for (const turn of turns) for (const name of turn.callNames) add(name);
    }
    return [...map.entries()]
      .map(([name, calls]) => ({ label: name, value: calls }))
      .sort((left, right) => right.value - left.value);
  }, [events, turns]);

  const toolTimeline = React.useMemo(() => {
    const items = [];
    for (const row of events) {
      for (const tool of row.tools) {
        items.push({ label: String(tool.name ?? "工具调用"), start: row.at ?? null, note: row.type || row.method || "" });
      }
    }
    return items;
  }, [events]);

  const detailRows = React.useMemo(() => {
    if (events.some((row) => row.tools.length)) {
      const rows = [];
      for (const row of events) {
        for (const tool of row.tools) {
          rows.push({
            key: `${row.key}-${tool.name ?? "tool"}-${rows.length}`,
            at: row.at, from: "事件流", session: null, tool: tool.name ?? null, status: null, note: row.type || row.method || null,
          });
        }
      }
      return rows;
    }
    const rows = [];
    for (const turn of visibleTurns) {
      turn.callNames.forEach((name, index) => {
        rows.push({
          key: `${turn.key}-${index}-${name}`, at: turn.at, from: "回合",
          session: turn.sessionId, tool: name, status: null, note: turn.code ?? null,
        });
      });
    }
    return rows;
  }, [events, visibleTurns]);

  const tokenUsed = numOr(lastRun?.tokens_estimate);
  const tokenMax = numOr(route?.maxTokens);
  const tokenPercent = tokenUsed !== null && tokenMax ? Math.min(100, (tokenUsed / tokenMax) * 100) : null;
  const lastColumns = Array.isArray(headless?.last) && headless.last.length ? headless.last : [];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <div>
        <Text strong style={{ fontSize: 16 }}>决策大脑</Text>
        <Text type="secondary" style={{ fontSize: 12, marginLeft: 10 }}>
          平台是身体，DeepSeek Harness 是决策大脑 · 推理流 / 决策结论 / 运行元信息全部取自 /api/v3/brain
        </Text>
      </div>

      {error ? <Alert type="error" showIcon message="决策大脑取数失败（/api/v3/brain）" description={error} /> : null}
      {unavailable ? (
        <Alert type="warning" showIcon
          message="SDK JSON-RPC 通道无数据源"
          description={<Text type="secondary" style={{ fontSize: 12 }}>{reason ?? "服务端返回 sdk.status=unavailable，未给出 reason"}</Text>} />
      ) : null}

      <ProCard bordered title="决策会话" extra={<a onClick={refresh}>刷新</a>}>
        <Space wrap size={16} align="center">
          <Space size={6}>
            <Text type="secondary" style={{ fontSize: 12 }}>会话</Text>
            <Select
              size="small" style={{ width: 360 }} allowClear
              value={session || undefined}
              onChange={(next) => setSession(next ?? "")}
              placeholder={sessionOptions.length ? "全部真实回合" : "尚无回合会话"}
              options={sessionOptions}
              notFoundContent={<Text type="secondary" style={{ fontSize: 12 }}>无数据源：sdk.turns 为空</Text>}
            />
          </Space>
          <MetaChip label="模型" value={route?.model} title={`路由 provider：${route?.provider ?? "无数据源"}`} />
          <MetaChip label="推理强度" value={route?.reasoningEffort} title={`maxTokens：${route?.maxTokens ?? "无数据源"}`} />
          <MetaChip label="决策时点" value={decision?.asOf ? stampValue(decision.asOf) : null} title="decision.asOf" />
          <MetaChip label="会话通道" value={sdk?.status} title={`/api/v3/brain sdk.status${reason ? ` · ${reason}` : ""}`} />
          <Badge status={unavailable ? "default" : turns.length ? "processing" : "warning"}
            text={`已记录回合 ${turns.length}`} />
        </Space>
      </ProCard>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={12}>
          <ProCard bordered title="推理流 · Agent Loop" loading={loading} style={{ height: "100%" }}
            extra={<Tooltip title="观察 / 推理 / 行动 / 反馈"><Text type="secondary" style={{ fontSize: 12 }}>按事件类型着色</Text></Tooltip>}>
            {steps.length ? (
              <Timeline
                items={steps.map((step) => {
                  const meta = STEP_META[step.kind] ?? STEP_META.observe;
                  const row = step.row ?? null;
                  return {
                    color: meta.color,
                    children: (
                      <div>
                        <Space size={8} align="center" wrap>
                          <Tag color={meta.color} style={{ marginInlineEnd: 0 }}>{meta.text}</Tag>
                          {row ? <Text type="secondary" style={{ fontSize: 12 }}>{hhmmss(row.at)}</Text> : null}
                          {row ? <Text type="secondary" style={{ fontSize: 12 }}>{row.type || row.method || "—"}</Text> : null}
                        </Space>
                        {row?.thoughts?.length ? (
                          <Paragraph style={{ marginBottom: 4, marginTop: 6, whiteSpace: "pre-wrap", fontSize: 12.5 }}>
                            {clipText(row.thoughts.join("\n"), 900)}
                          </Paragraph>
                        ) : null}
                        {row?.texts?.length ? (
                          <Paragraph style={{ marginBottom: 4, marginTop: 6, whiteSpace: "pre-wrap", fontSize: 12.5 }}>
                            {clipText(row.texts.join("\n"), 900)}
                          </Paragraph>
                        ) : null}
                        {row?.tools?.length ? (
                          <Space size={6} wrap>
                            {row.tools.map((tool, index) => (
                              <Tag key={`${step.key}-tool-${index}`} color="blue">{tool.name ?? "工具调用（事件未给 name）"}</Tag>
                            ))}
                          </Space>
                        ) : null}
                        {row?.reason ? (
                          <Paragraph style={{ marginBottom: 0, marginTop: 6, fontSize: 12.5 }}>
                            <Text type="secondary">结束原因：</Text>{clipText(row.reason, 400)}
                          </Paragraph>
                        ) : null}
                        {!row ? (
                          <div>
                            <Paragraph style={{ marginBottom: step.tools ? 6 : 0, marginTop: 6, whiteSpace: "pre-wrap", fontSize: 12.5 }}>
                              {step.text ?? ""}
                            </Paragraph>
                            {step.tools?.length ? (
                              <Space size={6} wrap>
                                {step.tools.map((tool, index) => (
                                  <Tag key={`${step.key}-tool-${index}`} color="blue">{tool.name}</Tag>
                                ))}
                              </Space>
                            ) : null}
                          </div>
                        ) : null}
                      </div>
                    ),
                  };
                })}
              />
            ) : (
              <NoSource what="推理流" why={reason
                ?? "sdk.events 与 sdk.turns 均为空：本服务未挂载 SDK/Headless 会话通道，或本轮尚无已记录的回合。"} />
            )}
          </ProCard>
        </Col>

        <Col xs={24} lg={6}>
          <ProCard bordered title="决策结论" loading={loading} style={{ height: "100%" }}
            extra={<Tag color="gold">{proposal ? String(proposal.action ?? "—") : "无建议"}</Tag>}>
            {!decision ? (
              <NoSource what="决策结论" why="decision 为 null：本服务未返回决策阶段产物（PDAT/PAAT 流水线未运行或未落盘）。" />
            ) : !proposal ? (
              <NoSource what="决策结论" why={`decision 存在（asOf ${stampValue(decision.asOf)}），但 proposals 为空：本轮无真实调仓建议。`} />
            ) : (
              <Space direction="vertical" size={10} style={{ width: "100%" }}>
                <Descriptions column={1} size="small">
                  <Descriptions.Item label="决策时点">{stampValue(decision.asOf)}</Descriptions.Item>
                  <Descriptions.Item label="标的"><CellText value={proposal.ticker} /></Descriptions.Item>
                  <Descriptions.Item label="目标权重">
                    <CellText value={fixedText(proposal.targetWeightPct, 1) === null ? null : `${fixedText(proposal.targetWeightPct, 1)}%`} />
                  </Descriptions.Item>
                  <Descriptions.Item label="风险等级"><CellText value={proposal.riskLevel} /></Descriptions.Item>
                  <Descriptions.Item label="执行前置">
                    <CellText value={proposal.action_hint} fallback="无数据源：proposal 未返回 action_hint" />
                  </Descriptions.Item>
                </Descriptions>
                <div>
                  <Text type="secondary" style={{ fontSize: 11 }}>依据</Text>
                  <Paragraph style={{ marginBottom: 0, marginTop: 4, fontSize: 12.5, whiteSpace: "pre-wrap" }}>
                    {proposal.basis ? plainText(proposal.basis) : "无数据源：proposal 未返回 basis"}
                  </Paragraph>
                </div>
                <div>
                  <Text type="secondary" style={{ fontSize: 11 }}>置信度</Text>
                  <div><Text type="secondary" style={{ fontSize: 12 }}>无数据源：/api/v3/brain decision 未提供置信度字段</Text></div>
                </div>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  本页只读；执行只发生在工作台受约束入口（人工确认）。
                </Text>
              </Space>
            )}
          </ProCard>
        </Col>

        <Col xs={24} lg={6}>
          <ProCard bordered title="运行元信息" loading={loading} style={{ height: "100%" }}>
            <Space direction="vertical" size={10} style={{ width: "100%" }}>
              {today ? (
                <Space size="large" wrap>
                  <Statistic title="今日 Headless 总数" value={numOr(today.total) ?? "无数据源"} />
                  <Statistic title="成功" value={numOr(today.success) ?? "无数据源"} valueStyle={{ color: "#3fb950" }} />
                  <Statistic title="失败" value={numOr(today.failed) ?? "无数据源"} valueStyle={{ color: "#f8514d" }} />
                  <Statistic title="被终止" value={numOr(today.killed) ?? "无数据源"} />
                  <Statistic title="平均耗时(ms)" value={numOr(today.avgMs) ?? "无数据源"} />
                </Space>
              ) : (
                <NoSource what="Headless 今日计数" why="/api/v3/brain headless.today 为空：本服务未挂载 Headless 账本。" />
              )}

              <Descriptions column={1} size="small" title="通道与路由">
                <Descriptions.Item label="SDK 状态"><CellText value={sdk?.status} /></Descriptions.Item>
                <Descriptions.Item label="SDK 原因"><CellText value={reason} fallback="—（通道可用或未返回 reason）" /></Descriptions.Item>
                <Descriptions.Item label="路由模型"><CellText value={route?.model} /></Descriptions.Item>
                <Descriptions.Item label="提供方"><CellText value={route?.provider} /></Descriptions.Item>
                <Descriptions.Item label="最近回合">
                  <CellText value={sdk?.lastTurn ? `${sdk.lastTurn.kind ?? "—"} · ${stampValue(sdk.lastTurn.at ?? sdk.lastTurn)}` : null} />
                </Descriptions.Item>
              </Descriptions>

              <div>
                <Text type="secondary" style={{ fontSize: 11 }}>已用 token（估算 / 路由上限）</Text>
                {tokenUsed === null && tokenMax === null ? (
                  <div><Text type="secondary" style={{ fontSize: 12 }}>无数据源：headless.last 未返回 tokens_estimate，sdk.route 未返回 maxTokens</Text></div>
                ) : (
                  <>
                    <div>
                      <Text style={{ fontSize: 12 }}>
                        {tokenUsed === null ? "无数据源" : num(tokenUsed, 0)} / {tokenMax === null ? "无数据源" : num(tokenMax, 0)}
                        {tokenPercent === null ? "" : ` · ${tokenPercent.toFixed(2)}%`}
                      </Text>
                    </div>
                    <Progress percent={tokenPercent === null ? 0 : Number(tokenPercent.toFixed(2))} showInfo={false} size="small" />
                  </>
                )}
              </div>

              {leafPairs(headless?.breaker).length ? (
                <Descriptions column={1} size="small" title="熔断器（headless.breaker）">
                  {leafPairs(headless.breaker).map((pair) => (
                    <Descriptions.Item key={pair.key} label={pair.label}><CellText value={pair.value} /></Descriptions.Item>
                  ))}
                </Descriptions>
              ) : (
                <NoSource what="熔断器状态" why="/api/v3/brain headless.breaker 为空或非标量结构。" />
              )}

              {Array.isArray(sdk?.serverInfo) && sdk.serverInfo.length ? (
                <div>
                  <Text type="secondary" style={{ fontSize: 11 }}>serverInfo</Text>
                  <div><Text style={{ fontSize: 12 }}>{clipText(sdk.serverInfo.map((item) => (typeof item === "string" ? item : JSON.stringify(item))).join(" · "), 400)}</Text></div>
                </div>
              ) : null}
            </Space>
          </ProCard>
        </Col>
      </Row>

      <ProCard bordered title="Headless 最近运行（headless.last）" loading={loading}>
        {lastColumns.length ? (
          <Table
            size="small" rowKey={(row, index) => `${row?.at ?? row?.id ?? index}`}
            dataSource={lastColumns} pagination={false} scroll={{ x: "max-content" }}
            columns={(() => {
              const keys = [];
              for (const row of lastColumns) {
                for (const [key, item] of Object.entries(row ?? {})) {
                  if (keys.length >= 8) break;
                  if (!keys.includes(key) && (item === null || ["string", "number", "boolean"].includes(typeof item))) keys.push(key);
                }
              }
              return keys.map((key) => ({
                title: LABEL[key] ?? key,
                dataIndex: key,
                render: (item) => (key === "at" && item ? stampValue(item) : <CellText value={item} />),
              }));
            })()}
          />
        ) : (
          <NoSource what="Headless 运行台账" why="/api/v3/brain headless.last 为空数组：尚无已落盘的 Headless 运行记录。" />
        )}
      </ProCard>

      <ProCard bordered title="会话输出（sdk.turns）" loading={loading}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>{`共 ${visibleTurns.length} 个回合${session ? ` · 已筛选 ${session}` : ""}`}</Text>}>
        {visibleTurns.length ? (
          <Table
            size="small" rowKey="key" dataSource={visibleTurns} pagination={false} scroll={{ x: "max-content" }}
            expandable={{
              expandedRowRender: (row) => (
                <Paragraph style={{ marginBottom: 0, whiteSpace: "pre-wrap", fontSize: 12.5 }}>
                  {row.answer ? plainText(row.answer) : "无数据源：该回合未返回 answer 字段"}
                </Paragraph>
              ),
            }}
            columns={[
              { title: "时间", dataIndex: "at", width: 170, render: (item) => stampValue(item) },
              { title: "会话", dataIndex: "sessionId", render: (item) => <CellText value={item} /> },
              { title: "类型", dataIndex: "kind", width: 110, render: (item) => <CellText value={item} /> },
              { title: "代码", dataIndex: "code", width: 110, render: (item) => <CellText value={item} fallback="—" /> },
              { title: "工具调用", width: 100, render: (_, row) => num(row.calls.length, 0) },
              { title: "回答", render: (_, row) => (row.answer ? `${plainText(row.answer).length} 字（展开查看）` : <Text type="secondary" style={{ fontSize: 12 }}>无数据源</Text>) },
            ]}
          />
        ) : (
          <NoSource what="会话输出" why={reason ?? "sdk.turns 为空：本服务未挂载 SDK 会话账本，或尚无已记录的回合。"} />
        )}
      </ProCard>

      <ProCard bordered title="工具调用分布" loading={loading}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {toolSource ? `计数来源：${toolSource.name}（${toolSource.unit}）` : "无数据源"}
        </Text>}>
        <Row gutter={[12, 12]}>
          <Col xs={24} lg={12}>
            <HBarChart items={toolCounts} emptyText="无数据源：sdk.events 与 sdk.turns 均未返回工具调用"
              valueFormat={(item) => `${num(item, 0)} 次`} />
          </Col>
          <Col xs={24} lg={12}>
            {toolTimeline.length ? (
              <TimelineChart items={toolTimeline} emptyText="无数据源：事件流无 tool-call 时刻" />
            ) : (
              <NoSource what="工具调用时序" why="sdk.events 无 tool-call 事件（或事件未带 at 时刻）：回合记录只有计数，没有逐次调用时刻，故不画时序轨道。" />
            )}
          </Col>
        </Row>
        <Text type="secondary" style={{ fontSize: 12 }}>
          分工具平均耗时：无数据源（/api/v3/brain 未提供逐工具耗时字段）。
        </Text>
      </ProCard>

      <ProCard bordered title="调用明细" loading={loading}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>{`共 ${detailRows.length} 条`}</Text>}>
        {detailRows.length ? (
          <Table
            size="small" rowKey="key" dataSource={detailRows} pagination={{ pageSize: 10, size: "small" }}
            scroll={{ x: "max-content" }}
            columns={[
              { title: "时间", dataIndex: "at", width: 170, render: (item) => stampValue(item) },
              { title: "来源", dataIndex: "from", width: 90, render: (item) => <Tag>{item}</Tag> },
              { title: "会话", dataIndex: "session", render: (item) => <CellText value={item} /> },
              { title: "工具", dataIndex: "tool", render: (item) => <CellText value={item} /> },
              { title: "状态", dataIndex: "status", width: 120, render: (item) => <CellText value={item} fallback="无数据源" /> },
              { title: "备注", dataIndex: "note", render: (item) => <CellText value={item} fallback="—" /> },
            ]}
          />
        ) : (
          <NoSource what="调用明细" why={reason ?? "sdk.events 与 sdk.turns 均未返回工具调用记录。"} />
        )}
      </ProCard>

      <ProCard bordered title="数据源" loading={loading}>
        {sources ? (
          <Descriptions column={1} size="small">
            {leafPairs(sources, 12).map((pair) => (
              <Descriptions.Item key={pair.key} label={pair.label}><CellText value={pair.value} /></Descriptions.Item>
            ))}
          </Descriptions>
        ) : (
          <NoSource what="数据源健康" why="/api/v3/brain sources 为空：服务端未返回数据源清单。" />
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>
          数据来源：GET /api/v3/brain（复用既有工具面/会话账本的同一 handle）。所有数值为实测；
          取不到的项显式标注「无数据源」并说明原因，不使用估算值。
        </Text>
      </ProCard>
    </Space>
  );
}
