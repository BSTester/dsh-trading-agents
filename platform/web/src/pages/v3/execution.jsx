// V3「执行与审批」页（Ant Design Pro）。设计稿对应 od-quant-harness-platform/execution.html。
//
// 本页只读：平台不含下单入口，执行只发生在工作台 Web（plan_execute + 人工确认，live 需口令）。
// 因此本页有意不提供「提交/确认/拒绝」按钮——审批交互在工作台 Web，本页只呈现事实。
//
// 数据来源（全部实测，缺失即显式「无数据源」）：
//   * 执行入口 + 订单生命周期 + 分级审批 + 订单明细：GET /api/v3/execution（GET /api/v3/oms/orders 同形状兜底）
//   * OMS 状态同步：POST /api/v3/oms/sync（按钮触发；只读服务的同步，不下单）
//   * 决策链路：GET /api/v3/audit?window=120
//   * 成交质量（滑点/换手/成交率）：当前服务无数据源 → 显式标注，不填占位值
//
// 图表复用仓库自研 canvas 组件：TimelineChart（订单/审计链路时段）。见 src/charts/。
import React from "react";
import { Alert, Badge, Button, Card, Col, Descriptions, Empty, Row, Space, Statistic, Table, Tag, Timeline, Tooltip, Typography } from "antd";
import { ProCard, ProTable } from "@ant-design/pro-components";
import { callV3, useV3 } from "../../services/v3api.js";
import { minuteOf, num } from "../../services/format.jsx";
import { TimelineChart } from "../../charts/timeline.jsx";

const { Text, Title, Paragraph } = Typography;

function finiteOf(value, digits = 2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return Number(n.toFixed(digits));
}

function textOf(value, digits = 2) {
  const n = finiteOf(value, digits);
  return n === null ? "—" : String(n);
}

function moneyText(value) {
  const n = finiteOf(value, 2);
  return n === null ? "—" : `¥${n.toLocaleString("zh-CN")}`;
}

/**
 * positions / orders_open / deals_today 三种可能形状归一：`{groups:[{rows:[]}]}`、`{rows:[]}`、数组。
 * 只取叶子行数组，不序列化任何 live 对象。
 */
function flattenRows(value) {
  if (Array.isArray(value)) return value;
  if (value && Array.isArray(value.rows)) return value.rows;
  if (value && Array.isArray(value.groups)) {
    return value.groups.flatMap((group) => (Array.isArray(group?.rows) ? group.rows : []));
  }
  return [];
}

/** 归一化阶段键：大小写/下划线/连字符/空格差异不影响匹配（"已提交"、"SUBMITTED"、"submitted" 同键）。 */
function stageKeyOf(value) {
  return String(value ?? "").trim().toLowerCase().replace(/[\s_-]/g, "");
}

/** 订单生命周期 6 列：服务端 stage 文案的中英文别名 → 固定列（顺序即流程顺序）。 */
const STAGES = [
  { key: "信号生成", aliases: ["信号生成", "signal", "signals", "created", "new", "待校验"] },
  { key: "风控校验", aliases: ["风控校验", "risk", "riskcheck", "风控", "校验"] },
  { key: "审批", aliases: ["审批", "approval", "approve", "pendingapproval", "人工确认", "待审批"] },
  { key: "已提交", aliases: ["已提交", "submitted", "submit", "sent", "oms", "accepted"] },
  { key: "部分成交", aliases: ["部分成交", "partial", "partiallyfilled", "partialfill"] },
  { key: "全部成交", aliases: ["全部成交", "filled", "filledall", "全部", "done", "complete"] },
];

function stageIndex(order) {
  const raw = stageKeyOf(order?.stage);
  if (!raw) return -1;
  for (let i = 0; i < STAGES.length; i += 1) {
    if (STAGES[i].aliases.some((alias) => stageKeyOf(alias) === raw)) return i;
  }
  return -1;
}

/** 风控动作归一：allow → 自动放行；reject/block → 强制阻断；其余（confirm/manual/…）→ 人工确认。 */
function riskClass(order) {
  const action = String(order?.risk?.action ?? "").toLowerCase();
  if (action === "allow" || action === "allowed" || action === "pass") return "auto";
  if (action === "reject" || action === "rejected" || action === "block" || action === "blocked" || action === "deny") return "block";
  if (!action) return "unknown";
  return "manual";
}

function riskReasons(order) {
  const reasons = order?.risk?.reasons;
  return Array.isArray(reasons) ? reasons.filter((item) => item !== undefined && item !== null && item !== "") : [];
}

function sideTag(side) {
  const text = String(side ?? "—");
  const lower = text.toLowerCase();
  if (/buy|买入|b\b/.test(lower)) return <Tag color="red">{text}</Tag>;
  if (/sell|卖出|s\b/.test(lower)) return <Tag color="green">{text}</Tag>;
  return <Tag>{text}</Tag>;
}

/** 分级审批三栏的标题与说明（阈值来自服务端 risk.action，本页不自造阈值数字）。 */
const GRADES = [
  { kind: "auto", title: "自动执行", badge: "阈值内自动放行", color: "#3fb950" },
  { kind: "manual", title: "人工确认", badge: "需工作台 Web 人工确认", color: "#d9a112" },
  { kind: "block", title: "强制阻断", badge: "红线拦截", color: "#f8514d" },
];

export default function V3执行审批Page() {
  const execution = useV3("execution", {});
  const omsFallback = useV3("oms/orders", {});
  const audit = useV3("audit", { window: 120 });

  const payload = execution.value ?? omsFallback.value ?? null;
  const oms = payload?.oms ?? null;
  const orders = Array.isArray(oms?.orders) ? oms.orders : [];
  const positions = flattenRows(payload?.positions);
  const openOrders = flattenRows(payload?.orders_open);
  const deals = flattenRows(payload?.deals_today);

  const [syncState, setSyncState] = React.useState({ busy: false, ok: null, message: "" });
  const syncOrders = React.useCallback(async () => {
    setSyncState({ busy: true, ok: null, message: "" });
    try {
      const body = await callV3("oms/sync", {}, { refresh: true });
      const envelope = body?.value ?? body ?? {};
      if (envelope.ok === false) throw new Error(envelope.error?.message || envelope.error?.code || "同步返回 ok=false");
      setSyncState({ busy: false, ok: true, message: "已触发 OMS 状态同步（POST /api/v3/oms/sync）" });
      execution.refresh();
      omsFallback.refresh();
    } catch (error) {
      setSyncState({ busy: false, ok: false, message: String(error.message || error) });
    }
  }, [execution.refresh, omsFallback.refresh]);

  // 订单生命周期看板：数量优先取 oms.stages，缺失则按本页订单的 stage 计数回退（并如实标注口径）
  const stageCounts = oms?.stages && typeof oms.stages === "object" ? oms.stages : null;
  const kanban = STAGES.map((stage) => {
    const ordersInStage = orders.filter((order) => stageIndex(order) === STAGES.indexOf(stage));
    const declared = stageCounts
      ? Object.entries(stageCounts).find(([name]) => stageKeyOf(name) === stageKeyOf(stage.key))?.[1]
      : undefined;
    return {
      ...stage,
      count: declared !== undefined ? declared : ordersInStage.length,
      countSource: declared !== undefined
        ? "oms.stages 声明的阶段计数"
        : (stageCounts ? "oms.stages 未声明该阶段" : "该接口未实现（无阶段计数数据源）→ 显示本次响应内订单行数"),
      orders: ordersInStage,
    };
  });
  const unmapped = orders.filter((order) => stageIndex(order) === -1);

  const byGrade = { auto: [], manual: [], block: [], unknown: [] };
  orders.forEach((order) => { byGrade[riskClass(order)].push(order); });

  // 决策链路：默认追溯第一条订单；可点表格行切换
  const [traceOrder, setTraceOrder] = React.useState(null);
  const selected = traceOrder ?? orders[0] ?? null;
  const history = Array.isArray(selected?.history) ? selected.history : [];
  const timelineItems = history.map((step, index) => ({
    label: `${textOf(index + 1, 0)} ${step?.stage ?? "—"}`,
    start: step?.at,
    end: step?.at,
    status: (Array.isArray(step?.reasons) && step.reasons.length) ? "failed" : "ok",
    note: Array.isArray(step?.reasons) && step.reasons.length ? step.reasons.join("；") : undefined,
  }));
  const auditEntries = Array.isArray(audit.value?.data?.entries) ? audit.value.data.entries : [];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {execution.error && omsFallback.error ? (
        <Alert type="error" showIcon message="执行取数失败" description={`/api/v3/execution：${execution.error}；/api/v3/oms/orders：${omsFallback.error}`} />
      ) : null}
      {execution.error && !omsFallback.error ? (
        <Alert type="warning" showIcon message="GET /api/v3/execution 失败，已回退 /api/v3/oms/orders" description={execution.error} />
      ) : null}

      <ProCard title="执行入口（平台不含下单入口）" bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>{oms?.note ? String(oms.note) : "oms.note 未返回"}</Text>}>
        <Row gutter={[12, 12]} align="middle">
          <Col xs={24} lg={14}>
            <Alert type="warning" showIcon message="平台不含下单入口"
              description={<Text type="secondary">
                执行只发生在工作台 Web 的「执行已冻结计划」（plan_execute）+ 人工确认；live 模式需口令「确认执行」。
                本页（V3 控制台）是只读视图：不提供提交/确认/拒绝按钮，也不做任何假交互。
                {oms?.confirmation ? ` 服务端确认口径：${String(oms.confirmation)}` : ""}
              </Text>} />
          </Col>
          <Col xs={24} lg={10}>
            <Space size="large" wrap>
              <Statistic title="OMS 审定净值（nav）" value={moneyText(oms?.nav)} />
              <Statistic title="今日成交（deals_today）" value={payload ? deals.length : "—"} />
              <Statistic title="挂单（orders_open）" value={payload ? openOrders.length : "—"} />
            </Space>
            <Space style={{ marginTop: 8 }} wrap>
              <Button onClick={syncOrders} loading={syncState.busy}>同步 OMS 状态</Button>
              <Text type="secondary" style={{ fontSize: 12 }}>POST /api/v3/oms/sync（只同步台账，不下单）</Text>
            </Space>
            {syncState.message ? (
              <Alert style={{ marginTop: 8 }} showIcon type={syncState.ok ? "success" : "error"} message={syncState.message} />
            ) : null}
          </Col>
        </Row>
      </ProCard>

      <ProCard title="订单生命周期看板" bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {stageCounts ? "计数来源 oms.stages" : "oms.stages 未返回，计数按 orders 的 stage 回退"}
        </Text>}>
        <Row gutter={[8, 8]}>
          {kanban.map((stage) => (
            <Col key={stage.key} xs={24} sm={12} md={8} xl={4}>
              <Card size="small" bordered title={<Space size={6}><Text strong>{stage.key}</Text><Badge count={stage.count ?? 0} showZero color={stage.count ? "#4c8dff" : "#3a4150"} /></Space>}
                extra={<Tooltip title={`计数口径：${stage.countSource}`}><Text type="secondary" style={{ fontSize: 11 }}>口径</Text></Tooltip>}>
                {stage.orders.length ? (
                  <Space direction="vertical" size={6} style={{ width: "100%" }}>
                    {stage.orders.slice(0, 4).map((order) => (
                      <Space key={String(order?.id ?? order?.ticker)} direction="vertical" size={0} style={{ width: "100%" }}>
                        <Space size={6}>
                          <Text code>{order?.ticker ?? "—"}</Text>
                          {sideTag(order?.side)}
                        </Space>
                        <Text type="secondary" style={{ fontSize: 11.5 }}>{`${textOf(order?.qty, 0)} 股 · ${moneyText(order?.value)}`}</Text>
                      </Space>
                    ))}
                    {stage.orders.length > 4 ? <Text type="secondary" style={{ fontSize: 11 }}>{`仅显示前 4 条，共 ${stage.orders.length} 条`}</Text> : null}
                  </Space>
                ) : <Text type="secondary" style={{ fontSize: 12 }}>{stage.count ? "该阶段有单，但明细未返回" : "此阶段无订单"}</Text>}
              </Card>
            </Col>
          ))}
        </Row>
        {unmapped.length ? (
          <Alert style={{ marginTop: 8 }} type="warning" showIcon
            message={`${unmapped.length} 条订单的 stage 未能匹配六阶段`}
            description={`原始 stage：${[...new Set(unmapped.map((order) => String(order?.stage ?? "（空）")))].join("、")}`} />
        ) : null}
      </ProCard>

      <ProCard title="分级审批（自动 / 人工 / 阻断）" bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>{`按订单 risk.action 分组 · 共 ${orders.length} 条`}</Text>}>
        <Row gutter={[12, 12]}>
          {GRADES.map((grade) => {
            const rows = byGrade[grade.kind];
            return (
              <Col key={grade.kind} xs={24} xl={8}>
                <Card size="small" bordered
                  title={<Space size={8}><Text strong style={{ color: grade.color }}>{grade.title}</Text><Tag>{grade.badge}</Tag></Space>}
                  extra={<Text type="secondary" style={{ fontSize: 12 }}>{`${rows.length} 条`}</Text>}>
                  {rows.length ? (
                    <Space direction="vertical" size={8} style={{ width: "100%" }}>
                      {rows.map((order) => (
                        <Card key={String(order?.id ?? `${order?.ticker}-${order?.qty}`)} size="small" type="inner"
                          title={<Space size={6}><Text code>{order?.ticker ?? "—"}</Text>{sideTag(order?.side)}</Space>}
                          extra={<Text type="secondary" style={{ fontSize: 11 }}>{order?.plan_id ? `计划 ${order.plan_id}` : "无 plan_id"}</Text>}>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {`${textOf(order?.qty, 0)} 股 · ${moneyText(order?.value)} · 价格 ${textOf(order?.price, 3)}`}
                          </Text>
                          <div style={{ marginTop: 4 }}>
                            <Text type="secondary" style={{ fontSize: 11 }}>{`风控动作 ${order?.risk?.action ?? "—"}：`}</Text>
                            {riskReasons(order).length
                              ? riskReasons(order).map((reason, index) => <Tag key={`${reason}-${index}`} color={grade.kind === "block" ? "red" : "default"}>{String(reason)}</Tag>)
                              : <Text type="secondary" style={{ fontSize: 11 }}>服务未返回风控原因</Text>}
                          </div>
                        </Card>
                      ))}
                    </Space>
                  ) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该分级当前无订单" />}
                </Card>
              </Col>
            );
          })}
        </Row>
        {byGrade.unknown.length ? (
          <Alert style={{ marginTop: 8 }} type="warning" showIcon
            message={`${byGrade.unknown.length} 条订单未返回 risk.action，未归入任何分级`}
            description="不计入自动放行，也不计为阻断——分级以服务端风控回执为准。" />
        ) : null}
        <Alert style={{ marginTop: 8 }} type="info" showIcon message="审批交互不在本页"
          description={<Text type="secondary">
            人工确认与口令输入只在工作台 Web（受约束入口）发生；V3 控制台不提供确认/拒绝按钮。
            强制阻断无操作入口，全程留痕可追溯。
          </Text>} />
      </ProCard>

      <ProCard title="订单明细" bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>点击行切换下方「决策链路追溯」的目标</Text>}>
        <ProTable
          rowKey={(row, index) => String(row?.id ?? `${row?.ticker ?? "?"}-${index}`)}
          search={false} options={false} size="small"
          loading={execution.loading || omsFallback.loading}
          dataSource={orders}
          pagination={false}
          locale={{ emptyText: execution.loading || omsFallback.loading ? "加载中…" : "服务未返回订单（或当前无订单）" }}
          rowClassName={(row) => (selected && String(row?.id) === String(selected?.id) ? "v3-row-active" : "")}
          onRow={(row) => ({ onClick: () => setTraceOrder(row) })}
          columns={[
            { title: "订单号", dataIndex: "id", render: (v, row) => <a onClick={() => setTraceOrder(row)}>{v ?? "—"}</a> },
            { title: "标的", dataIndex: "ticker", render: (v) => <Text code>{v ?? "—"}</Text> },
            { title: "方向", dataIndex: "side", render: (v) => sideTag(v) },
            { title: "数量", dataIndex: "qty", align: "right", render: (v) => textOf(v, 0) },
            { title: "价格", dataIndex: "price", align: "right", render: (v) => textOf(v, 3) },
            { title: "金额", dataIndex: "value", align: "right", render: (v) => moneyText(v) },
            { title: "阶段", dataIndex: "stage", render: (v, row) => <Tag color={stageIndex(row) === -1 ? "default" : "blue"}>{v ?? "—"}</Tag> },
            { title: "风控动作", render: (_, row) => row?.risk?.action ?? "—" },
            { title: "计划", dataIndex: "plan_id", render: (v) => v ?? "—" },
            { title: "链路步数", align: "right", render: (_, row) => (Array.isArray(row?.history) ? row.history.length : 0) },
          ]} />
      </ProCard>

      <Row gutter={[12, 12]}>
        <Col xs={24} xl={14}>
          <ProCard title="持仓" bordered
            extra={<Text type="secondary" style={{ fontSize: 12 }}>{`positions / orders_open / deals_today 兼容 groups 或数组 · 持仓 ${positions.length} 条`}</Text>}>
            <Table
              size="small" rowKey={(row, index) => `${row?.symbol ?? row?.ticker ?? "?"}-${row?.qty ?? index}`}
              dataSource={positions} pagination={false} loading={execution.loading}
              locale={{ emptyText: "服务未返回持仓行（positions 为空或形状未知）" }}
              columns={[
                { title: "标的", render: (_, row) => row?.symbol ?? row?.ticker ?? "—" },
                { title: "名称", render: (_, row) => row?.name ?? "—" },
                { title: "数量", align: "right", render: (_, row) => num(row?.qty) },
                { title: "成本", align: "right", render: (_, row) => textOf(row?.cost ?? row?.avg_cost, 3) },
                { title: "市值", align: "right", render: (_, row) => moneyText(row?.market_value) },
                { title: "浮动盈亏", align: "right", render: (_, row) => (row?.pl_val === undefined ? "—" : <Text type={Number(row.pl_val) >= 0 ? "success" : "danger"}>{moneyText(row.pl_val)}</Text>) },
              ]} />
            <div style={{ marginTop: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>{`挂单 ${openOrders.length} 条 · 今日成交 ${deals.length} 条（形状同上，行数据直接来自服务端）`}</Text>
            </div>
          </ProCard>
        </Col>
        <Col xs={24} xl={10}>
          <ProCard title="成交质量" bordered>
            <Alert type="info" showIcon message="无数据源：成交质量（滑点/成交率/换手/冲击成本）"
              description={<Text type="secondary">
                当前服务没有成交质量归因工具（/api/v3/execution 只给出 positions、orders_open、deals_today、oms 四块），
                因此本区块不展示滑点、成交率、换手等任何数字，也不使用设计稿占位值。
                成交明细行见左侧「今日成交」计数与订单明细表（均为原始台账字段，未做二次推导）。
              </Text>} />
          </ProCard>
        </Col>
      </Row>

      <ProCard title="决策链路追溯" bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {selected ? `当前追溯：${selected.id ?? "—"} · ${selected.ticker ?? "—"} · ${selected.side ?? "—"} ${textOf(selected.qty, 0)} 股` : "无可追溯订单"}
        </Text>}>
        {selected ? (
          <Space direction="vertical" size={12} style={{ width: "100%" }}>
            <Descriptions size="small" column={3} bordered>
              <Descriptions.Item label="订单">{selected.id ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="计划">{selected.plan_id ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="当前阶段">{selected.stage ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="风控动作">{selected.risk?.action ?? "—"}</Descriptions.Item>
              <Descriptions.Item label="风控原因" span={2}>
                {riskReasons(selected).length ? riskReasons(selected).join("；") : "服务未返回"}
              </Descriptions.Item>
            </Descriptions>
            <div>
              <Title level={5} style={{ marginTop: 0 }}>阶段流转（history）</Title>
              {history.length ? (
                <>
                  <TimelineChart items={timelineItems} emptyText="history 无可画条目"
                    timeFormat={(value) => minuteOf(value)} />
                  <Timeline style={{ marginTop: 12 }}
                    items={history.map((step, index) => ({
                      color: (Array.isArray(step?.reasons) && step.reasons.length) ? "red" : "green",
                      children: (
                        <Space direction="vertical" size={0}>
                          <Text strong>{`${index + 1}. ${step?.stage ?? "—"} · ${minuteOf(step?.at)}`}</Text>
                          <Text type="secondary" style={{ fontSize: 12 }}>
                            {Array.isArray(step?.reasons) && step.reasons.length ? step.reasons.join("；") : "无风控原因记录"}
                          </Text>
                        </Space>
                      ),
                    }))} />
                </>
              ) : <Text type="secondary">该订单未返回 history（无阶段流转记录）</Text>}
            </div>
          </Space>
        ) : (
          <Alert type="info" showIcon message="无数据源：无订单可追溯"
            description="oms.orders 为空，链路追溯需要一条订单的 history 字段。" />
        )}

        <div style={{ marginTop: 12 }}>
          <Title level={5}>审计链路（/api/v3/audit?window=120）</Title>
          {audit.error ? <Alert type="error" showIcon message="审计取数失败" description={audit.error} /> : null}
          {auditEntries.length ? (
            <Timeline items={auditEntries.map((entry) => ({
              children: (
                <Space direction="vertical" size={0}>
                  <Text strong>{`${entry?.kind ?? "—"} · ${minuteOf(entry?.at)} · ${entry?.ticker ?? "—"}`}</Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>{entry?.detail ?? entry?.id ?? "—"}</Text>
                </Space>
              ),
            }))} />
          ) : (
            <Text type="secondary">{audit.loading ? "加载中…" : "审计接口未返回条目（或窗口内无记录）→ 无数据源"}</Text>
          )}
        </div>
      </ProCard>

      <Card size="small" bordered>
        <Title level={5} style={{ marginTop: 0 }}>说明</Title>
        <Paragraph type="secondary" style={{ fontSize: 12, marginBottom: 0 }}>
          数据来源：本服务 /api/v3/execution（失败回退 /api/v3/oms/orders）、POST /api/v3/oms/sync、/api/v3/audit?window=120。
          交易边界：平台不含下单入口；下单/改单/撤单只在工作台 Web 的受约束入口（plan_execute + 人工确认，live 需口令），
          本页全部区块只读，不含任何提交类按钮。缺失数据源（成交质量、绩效归因）显式标注，不使用估算值。
        </Paragraph>
      </Card>
    </Space>
  );
}
