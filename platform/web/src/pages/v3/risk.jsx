// V3「风险监控」页（Ant Design Pro 版）：事前 / 事中 / 事后三段式风控 + 暴露集中度 + 净值回撤 + 规则与阻断留痕。
// 数据接口：GET /api/v3/risk/analytics、/api/v3/risk、/api/v3/oms/orders、/api/v3/events；
//          按钮触发 POST /api/v3/oms/sync（同步 OMS 台账，失败如实报错）。
// 设计稿对应页：od-quant-harness-platform/risk.html ——
//   指标卡行（VaR/CVaR/Beta/Alpha/IR）→ 三栏（事前/事中/事后）→ 暴露与集中度 → 净值/回撤曲线 →
//   风控规则表 → 阻断记录。
// 纪律：任何区块取不到只影响该区块，显式写「无数据源」+ 原因（接口未暴露 / 权限缺口 / 字段缺失）。
import React from "react";
import {
  Alert, Button, Col, Descriptions, Progress, Row, Space, Statistic, Table, Tag, Tooltip, Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { useV3 } from "../../services/v3api.js";
import { getToken } from "../../services/api.js";
import { num, pctOf, stampOf } from "../../services/format.jsx";
import { HBarChart } from "../../charts/bars.jsx";
import { LineChart } from "../../charts/line.jsx";

const { Text } = Typography;

/** 风控配置字段 → 中文规则名；`raw` = 非比例口径（原值展示，不做 ×100 折算）。 */
const CONFIG_ROWS = [
  { key: "risk_per_trade", name: "单笔交易风险预算" },
  { key: "max_position_pct", name: "单一标的上限" },
  { key: "daily_loss_limit_pct", name: "单日亏损上限" },
  { key: "max_positions", name: "最大持仓数", raw: true },
  { key: "stop_atr_mult", name: "ATR 止损倍数", raw: true },
];

function numOr(value) {
  if (value === null || value === undefined || value === "") return null;
  return Number.isFinite(Number(value)) ? Number(value) : null;
}

function fixedValue(value, digits = 2) {
  const n = numOr(value);
  return n === null ? null : n.toFixed(digits);
}

/** 接口已按「百分数」给出的字段（如 varDailyPct=-2.6 表示 -2.6%）：只补单位不换算。 */
function pctNumberText(value, digits = 2) {
  const n = numOr(value);
  return n === null ? null : `${n.toFixed(digits)}%`;
}

function signedValue(value, digits = 2, suffix = "%") {
  const n = numOr(value);
  if (n === null) return null;
  return `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(digits)}${suffix}`;
}

function moneyValue(value) {
  const n = numOr(value);
  return n === null ? null : `¥${n.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
}

function stampValue(value) {
  return value === null || value === undefined || value === "" ? "—" : stampOf(value);
}

/** 缺失时返回 null（交给 CellText 显示「无数据源」而不是「—」）。 */
function stampOrNone(value) {
  return value === null || value === undefined || value === "" ? null : stampOf(value);
}

/** 配置口径展示：|v| ≤ 1 视为比例（页面 ×100 折算），否则按原值展示；两条路径都在说明里写明。 */
function configDisplay(value, isRaw) {
  const n = numOr(value);
  if (n === null) return { text: null, note: "配置未返回该字段" };
  if (isRaw || Math.abs(n) > 1) return { text: n.toLocaleString("zh-CN"), note: `服务端原值 ${n}（非比例口径，原样展示）` };
  return { text: pctOf(n, 2), note: `服务端原值 ${n}（比例口径，页面按 ×100 折算为百分数展示）` };
}

function CellText({ value, fallback = "无数据源" }) {
  if (value === null || value === undefined || value === "") {
    return <Text type="secondary" style={{ fontSize: 12 }}>{fallback}</Text>;
  }
  return <span>{String(value)}</span>;
}

function NoSource({ what, why }) {
  return (
    <Alert type="warning" showIcon
      message={`${what}：无数据源`}
      description={why ? <Text type="secondary" style={{ fontSize: 12 }}>{why}</Text> : undefined} />
  );
}

/** 指标卡：值缺失时显式写「无数据源」，不留空、不写 0。 */
function MetricCard({ title, value, sub, tone }) {
  return (
    <ProCard bordered>
      <Statistic title={title} value={value === null || value === undefined || value === "" ? "无数据源" : String(value)}
        valueStyle={tone ? { color: tone } : undefined} />
      <Text type="secondary" style={{ fontSize: 12 }}>{sub ?? ""}</Text>
    </ProCard>
  );
}

function orderStage(order) {
  return String(order?.stage ?? "").toLowerCase();
}

function orderAction(order) {
  return order?.risk?.action ?? null;
}

function orderReasons(order) {
  const list = order?.risk?.reasons;
  return Array.isArray(list) ? list.map(String) : [];
}

function orderStamp(order) {
  const history = Array.isArray(order?.history) ? order.history : [];
  const first = history[0] ?? null;
  return first?.at ?? first?.time ?? first?.ts ?? null;
}

export default function V3风险监控Page() {
  const analyticsRes = useV3("risk/analytics", { limit: 250 }, []);
  const configRes = useV3("risk", {}, []);
  const omsRes = useV3("oms/orders", {}, []);
  const [sync, setSync] = React.useState({ loading: false });

  const analytics = analyticsRes.value ?? null;
  const an = analytics?.analytics ?? null;
  const curveSource = Array.isArray(an?.equityCurve) ? an.equityCurve : [];
  const configData = configRes.value?.data ?? null;
  const cfg = configData?.config ?? null;
  const oms = omsRes.value ?? null;
  const orders = Array.isArray(oms?.orders) ? oms.orders : [];
  const nav = numOr(oms?.nav) ?? numOr(analytics?.nav);
  const blocked = orders.filter((order) => orderStage(order) === "blocked");
  const manual = orders.filter((order) => orderStage(order) === "manual");
  const stageCounts = Array.isArray(oms?.stages)
    ? oms.stages.map((item, index) => ({ stage: String(item?.stage ?? item?.name ?? index), count: item?.count ?? item?.n ?? null }))
    : oms?.stages && typeof oms.stages === "object"
      ? Object.entries(oms.stages).map(([stage, count]) => ({ stage, count }))
      : [];

  // 事中事件流：优先取人工确认/被阻断订单的标的（最多 2 个），否则用契约占位标的。
  const eventTicker = manual[0]?.ticker ?? blocked[0]?.ticker ?? "SH.600000";
  const eventsRes = useV3("events", { ticker: eventTicker, window: 180 }, [eventTicker]);
  const eventRows = Array.isArray(eventsRes.value?.data?.events) ? eventsRes.value.data.events : [];

  const weights = React.useMemo(() => {
    const map = an?.tickers;
    if (!map || typeof map !== "object") return [];
    return Object.entries(map)
      .map(([name, value]) => ({ label: name, weight: numOr(value) }))
      .filter((item) => item.weight !== null)
      .sort((left, right) => right.weight - left.weight);
  }, [an]);

  const curve = React.useMemo(() => curveSource
    .map((point) => ({ t: point?.t ?? null, v: numOr(point?.v) }))
    .filter((point) => point.v !== null), [curveSource]);

  const drawdown = React.useMemo(() => {
    if (curve.length < 2) return null;
    let peak = -Infinity;
    let peakAt = null;
    const points = [];
    let minDD = Infinity;
    let minAt = null;
    for (const point of curve) {
      if (point.v > peak) { peak = point.v; peakAt = point.t; }
      const dd = (point.v / peak - 1) * 100;
      points.push({ v: dd });
      if (dd < minDD) { minDD = dd; minAt = point.t; }
    }
    return { points, minDD, minAt, lastDD: points[points.length - 1].v, from: curve[0].t, to: curve[curve.length - 1].t };
  }, [curve]);

  const windowRange = an?.window && typeof an.window === "object"
    ? `${an.window.from ?? "—"} → ${an.window.to ?? "—"}` : "无数据源";

  const actionCounts = React.useMemo(() => {
    const map = new Map();
    for (const order of orders) {
      const action = orderAction(order);
      const key = action === null || action === undefined || action === "" ? "未返回 action" : String(action);
      map.set(key, (map.get(key) ?? 0) + 1);
    }
    return [...map.entries()].map(([action, count]) => ({ action, count }));
  }, [orders]);

  const preflightReasons = React.useMemo(() => orders
    .filter((order) => orderReasons(order).length)
    .map((order, index) => ({
      key: `${order?.id ?? order?.ticker ?? "order"}-${index}`,
      ticker: order?.ticker ?? null,
      action: orderAction(order),
      stage: order?.stage ?? null,
      reasons: orderReasons(order).join("；"),
    })), [orders]);

  const top5 = weights.slice(0, 5);
  const top5Sum = top5.reduce((sum, item) => sum + item.weight, 0);
  const weightBars = weights.slice(0, 10).map((item) => ({
    label: item.label, value: Number((item.weight * 100).toFixed(4)),
  }));
  const maxOrderPercent = nav
    ? orders.reduce((max, order) => {
      const value = numOr(order?.value);
      if (value === null) return max;
      const share = (value / nav) * 100;
      return max === null || share > max ? share : max;
    }, null)
    : null;

  const kupiec = an?.kupiec ?? null;
  const kupiecExpected = kupiec && numOr(an?.confidence) !== null && numOr(kupiec.observations) !== null
    ? (1 - Number(an.confidence)) * Number(kupiec.observations) : null;

  const runSync = async () => {
    setSync({ loading: true });
    try {
      const token = getToken();
      const headers = { "content-type": "application/json" };
      if (token) headers.Authorization = `Bearer ${token}`;
      const response = await fetch("/api/v3/oms/sync", { method: "POST", headers, body: "{}" });
      let body = null;
      try { body = await response.json(); } catch { body = null; }
      if (!response.ok) throw new Error(body?.error?.message || `HTTP ${response.status}`);
      if (body && body.ok === false) throw new Error(body?.error?.message || "接口返回 ok=false");
      setSync({ loading: false, ok: body?.note ?? body?.message ?? "已触发 /api/v3/oms/sync" });
      omsRes.refresh();
    } catch (error) {
      setSync({ loading: false, error: String(error?.message ?? error) });
    }
  };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <div>
        <Text strong style={{ fontSize: 16 }}>风险监控</Text>
        <Text type="secondary" style={{ fontSize: 12, marginLeft: 10 }}>
          事前 · 事中 · 事后三段式风控 · 指标取自 /api/v3/risk/analytics，阈值取自 /api/v3/risk，单据取自 /api/v3/oms/orders
        </Text>
      </div>

      {analyticsRes.error ? <Alert type="error" showIcon message="风险分析取数失败（/api/v3/risk/analytics）" description={analyticsRes.error} /> : null}
      {configRes.error ? <Alert type="error" showIcon message="风控配置取数失败（/api/v3/risk）" description={configRes.error} /> : null}
      {omsRes.error ? <Alert type="error" showIcon message="OMS 订单取数失败（/api/v3/oms/orders）" description={omsRes.error} /> : null}

      <Row gutter={[12, 12]}>
        <Col xs={24} sm={12} md={8} lg={4} xl={4}>
          <MetricCard title="VaR（置信度，1d）" tone="#4a9eff"
            value={an ? (pctNumberText(an.varDailyPct) ?? "无数据源") : "无数据源"}
            sub={an
              ? `金额 ${moneyValue(an.varAmount) ?? "无数据源"} · 置信度 ${pctOf(an.confidence, 1)} · 样本 ${numOr(an.observations) === null ? "无数据源" : num(an.observations, 0)}`
              : "无数据源：/api/v3/risk/analytics 不可用"} />
        </Col>
        <Col xs={24} sm={12} md={8} lg={4} xl={4}>
          <MetricCard title="CVaR（置信度，1d）" tone="#39a0ed"
            value={an ? (pctNumberText(an.cvarDailyPct) ?? "无数据源") : "无数据源"}
            sub={an
              ? `金额 ${moneyValue(an.cvarAmount) ?? "无数据源"} · 方法 ${an.method ?? "无数据源"}`
              : "无数据源：/api/v3/risk/analytics 不可用"} />
        </Col>
        <Col xs={24} sm={12} md={8} lg={4} xl={4}>
          <MetricCard title="Beta" value={an ? (fixedValue(an.beta, 3) ?? "无数据源") : "无数据源"}
            sub={an ? `基准 ${analytics?.benchmarkTicker ?? "无数据源"} · 样本 ${numOr(an.observations) === null ? "无数据源" : num(an.observations, 0)}` : "无数据源：/api/v3/risk/analytics 不可用"} />
        </Col>
        <Col xs={24} sm={12} md={8} lg={4} xl={4}>
          <MetricCard title="Alpha（年化）" tone="#3fb950"
            value={an ? (signedValue(an.alphaAnnPct, 2, "%") ?? "无数据源") : "无数据源"}
            sub={an ? `组合年化 ${signedValue(an.annReturnPct, 2, "%") ?? "无数据源"} · 基准年化 ${signedValue(an.benchmarkAnnReturnPct, 2, "%") ?? "无数据源"}` : "无数据源：/api/v3/risk/analytics 不可用"} />
        </Col>
        <Col xs={24} sm={12} md={8} lg={4} xl={4}>
          <MetricCard title="IR（信息比率）" tone="#a371f7"
            value={an ? (fixedValue(an.ir, 3) ?? "无数据源") : "无数据源"}
            sub={an ? `年化波动 ${pctNumberText(an.annVolPct) ?? "无数据源"} · 观察窗口 ${windowRange}` : "无数据源：/api/v3/risk/analytics 不可用"} />
        </Col>
        <Col xs={24} sm={12} md={8} lg={4} xl={4}>
          <MetricCard title="净值 / 数据口径" value={nav === null ? "无数据源" : moneyValue(nav)}
            sub={analytics
              ? `组合口径 ${analytics.portfolioSource ?? "无数据源"} · 净值点位 ${curve.length}`
              : "无数据源：/api/v3/risk/analytics 不可用"} />
        </Col>
      </Row>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={8}>
          <ProCard bordered title="事前风控 · 阈值与订单预检" loading={configRes.loading || omsRes.loading} style={{ height: "100%" }}>
            {cfg ? (
              <Table
                size="small" rowKey="key" pagination={false}
                dataSource={CONFIG_ROWS.map((row) => {
                  const display = configDisplay(cfg[row.key], row.raw);
                  return { key: row.key, name: row.name, field: row.key, value: display.text, note: display.note };
                })}
                columns={[
                  { title: "规则", dataIndex: "name", render: (value) => <CellText value={value} fallback="—" /> },
                  { title: "字段", dataIndex: "field", render: (value) => <Text code style={{ fontSize: 12 }}>{String(value)}</Text> },
                  {
                    title: "取值", dataIndex: "value",
                    render: (value, row) => (value === null
                      ? <CellText value={null} />
                      : <Tooltip title={row.note}><span>{value}</span></Tooltip>),
                  },
                ]}
              />
            ) : (
              <NoSource what="风控阈值" why="/api/v3/risk 未返回 data.config（配置源不可用）。" />
            )}
            <div style={{ marginTop: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                配置来源：{configData?.source ?? "无数据源"} · 页面只读，规则写入口不在本服务。
              </Text>
            </div>

            <div style={{ marginTop: 12 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>订单风控动作分布（risk.action）</Text>
              {actionCounts.length ? (
                <div style={{ marginTop: 6 }}>
                  <Space size={6} wrap>
                    {actionCounts.map((item) => (
                      <Tag key={item.action} color={/pass|allow|ok|通过/i.test(item.action) ? "green" : "gold"}>
                        {item.action} × {item.count}
                      </Tag>
                    ))}
                  </Space>
                </div>
              ) : (
                <div><Text type="secondary" style={{ fontSize: 12 }}>无数据源：/api/v3/oms/orders 未返回订单</Text></div>
              )}
            </div>

            <div style={{ marginTop: 12 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>单笔占比上限校验</Text>
              <div>
                <Text style={{ fontSize: 12 }}>
                  OMS 单笔最大占比 {maxOrderPercent === null ? "无数据源" : `${maxOrderPercent.toFixed(2)}%`} ·
                  上限 {cfg ? (configDisplay(cfg.max_position_pct).text ?? "无数据源") : "无数据源"}（字段 max_position_pct）
                </Text>
              </div>
              <Progress
                percent={maxOrderPercent !== null && numOr(cfg?.max_position_pct) ? Math.min(100, Number(((maxOrderPercent / (Number(cfg.max_position_pct) * 100)) * 100).toFixed(1))) : 0}
                showInfo={false} size="small" strokeColor="#d9a112" />
              <Text type="secondary" style={{ fontSize: 12 }}>
                {maxOrderPercent === null || !cfg || numOr(cfg.max_position_pct) === null
                  ? "缺 OMS 单笔金额或配置阈值，无法计算占用比例。"
                  : `占用上限的 ${((maxOrderPercent / (numOr(cfg.max_position_pct) * 100)) * 100).toFixed(1)}%（页面按 OMS 单笔金额 / 净值 ÷ 配置上限计算）。`}
              </Text>
            </div>
          </ProCard>
        </Col>

        <Col xs={24} lg={8}>
          <ProCard bordered title="事中风控 · 实时指标与事件" loading={analyticsRes.loading || eventsRes.loading} style={{ height: "100%" }}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>事件源 /api/v3/events · as_of {stampValue(eventsRes.value?.data?.as_of)}</Text>}>
            <Space size="large" wrap>
              <Statistic title="当前最大回撤" value={an ? (pctNumberText(an.maxDrawdownPct) ?? "无数据源") : "无数据源"}
                valueStyle={{ color: "#d9a112" }} />
              <Statistic title="年化波动" value={an ? (pctNumberText(an.annVolPct) ?? "无数据源") : "无数据源"} />
              <Statistic title="观察样本" value={an && numOr(an.observations) !== null ? num(an.observations, 0) : "无数据源"} />
            </Space>
            <div style={{ marginTop: 8 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                观察窗口 {windowRange} · 回撤阈值：无数据源（/api/v3/risk 与 /api/v3/risk/analytics 均未返回回撤阈值字段，
                故不画阈值线、不做触发判定）。
              </Text>
            </div>

            <div style={{ marginTop: 12 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>标的 {eventTicker} 的事件/公告（window {eventsRes.value?.data?.window_days ?? "无数据源"} 天）</Text>
              {eventsRes.error ? (
                <Alert type="error" showIcon style={{ marginTop: 6 }} message="事件取数失败（/api/v3/events）" description={eventsRes.error} />
              ) : eventRows.length ? (
                <Table
                  size="small" style={{ marginTop: 6 }} rowKey={(row, index) => `${row?.date ?? "event"}-${index}`}
                  dataSource={eventRows} pagination={{ pageSize: 5, size: "small" }}
                  columns={[
                    { title: "日期", dataIndex: "date", width: 110, render: (value) => <CellText value={value} /> },
                    { title: "类型", dataIndex: "type", width: 120, render: (value) => <CellText value={value} /> },
                    { title: "详情", dataIndex: "detail", render: (value) => <CellText value={value} fallback="—" /> },
                    { title: "公告", dataIndex: "announced", width: 90, render: (value) => <CellText value={value} fallback="—" /> },
                  ]}
                />
              ) : (
                <div style={{ marginTop: 6 }}>
                  <NoSource what={`${eventTicker} 事件流`} why="接口未返回 events：该标的在窗口内无公司行为公告。" />
                </div>
              )}
              <Text type="secondary" style={{ fontSize: 12 }}>
                事件源为公司行为公告，不含风险告警级别；实时告警流水（回撤触发、流动性告警等）：无数据源 ——
                本服务未挂载风控告警流通道。
              </Text>
            </div>
          </ProCard>
        </Col>

        <Col xs={24} lg={8}>
          <ProCard bordered title="事后风控 · 归因与检验" loading={analyticsRes.loading} style={{ height: "100%" }}>
            {an ? (
              <Descriptions column={1} size="small">
                <Descriptions.Item label="组合年化收益"><CellText value={signedValue(an.annReturnPct, 2, "%")} /></Descriptions.Item>
                <Descriptions.Item label="基准年化收益"><CellText value={signedValue(an.benchmarkAnnReturnPct, 2, "%")} /></Descriptions.Item>
                <Descriptions.Item label="Alpha（年化）"><CellText value={signedValue(an.alphaAnnPct, 2, "%")} /></Descriptions.Item>
                <Descriptions.Item label="Beta"><CellText value={fixedValue(an.beta, 3)} /></Descriptions.Item>
                <Descriptions.Item label="IR"><CellText value={fixedValue(an.ir, 3)} /></Descriptions.Item>
                <Descriptions.Item label="最大回撤"><CellText value={pctNumberText(an.maxDrawdownPct)} /></Descriptions.Item>
                <Descriptions.Item label="VaR 方法"><CellText value={an.method} /></Descriptions.Item>
              </Descriptions>
            ) : (
              <NoSource what="事后指标" why="/api/v3/risk/analytics 不可用（无组合净值序列，无法计算收益/风险指标）。" />
            )}

            <div style={{ marginTop: 10 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>Kupiec 检验（VaR 回测）</Text>
              {kupiec ? (
                <div style={{ marginTop: 4 }}>
                  <Tag color={kupiec.pass ? "green" : "red"}>{kupiec.pass ? "检验通过" : "检验未通过"}</Tag>
                  <Text style={{ fontSize: 12 }}>
                    p = {fixedValue(kupiec.pValue, 4) ?? "无数据源"} · LR = {fixedValue(kupiec.lr, 4) ?? "无数据源"} ·
                    例外 {numOr(kupiec.breaches) === null ? "无数据源" : num(kupiec.breaches, 0)} 次 /
                    样本 {numOr(kupiec.observations) === null ? "无数据源" : num(kupiec.observations, 0)} 日 ·
                    预期例外 {kupiecExpected === null ? "无数据源" : kupiecExpected.toFixed(2)} 次
                  </Text>
                  <div>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      预期例外 = 页面按 (1 − 置信度{pctOf(an?.confidence, 2)}) × 样本数 计算；检验口径由服务端给出（{an?.method ?? "无数据源"}）。
                    </Text>
                  </div>
                </div>
              ) : (
                <div style={{ marginTop: 4 }}>
                  <NoSource what="Kupiec 检验" why="/api/v3/risk/analytics 未返回 kupiec 字段（样本不足或未做回测检验）。" />
                </div>
              )}
            </div>

            <div style={{ marginTop: 10 }}>
              <NoSource what="收益归因（行业 / 因子 / 个股贡献）"
                why="/api/v3 未暴露归因接口：/api/v3/risk/analytics 只返回组合净值序列与逐标的权重，无贡献拆解字段。" />
            </div>
          </ProCard>
        </Col>
      </Row>

      <ProCard bordered title="暴露与集中度" loading={analyticsRes.loading}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {analytics ? `口径 ${analytics.portfolioSource ?? "无数据源"} · 逐标的权重快照` : "无数据源"}
        </Text>}>
        {weights.length ? (
          <Row gutter={[12, 12]}>
            <Col xs={24} lg={14}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                逐标的权重（占组合净值 %，按权重降序；上限字段 max_position_pct = {cfg ? (configDisplay(cfg.max_position_pct).text ?? "无数据源") : "无数据源"}）
              </Text>
              <HBarChart items={weightBars} valueFormat={(value) => `${Number(value).toFixed(2)}%`}
                emptyText="无数据源：/api/v3/risk/analytics 未返回 tickers 权重" />
            </Col>
            <Col xs={24} lg={10}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                单票集中度 Top5 · 前五合计 {pctOf(top5Sum, 2)}（页面按真实权重求和）
              </Text>
              <Table
                size="small" style={{ marginTop: 6 }} rowKey="label" dataSource={top5} pagination={false}
                columns={[
                  { title: "标的", dataIndex: "label", render: (value) => <CellText value={value} /> },
                  {
                    title: "权重", dataIndex: "weight", key: "weight",
                    render: (value) => {
                      const n = numOr(value);
                      return <CellText value={n === null ? null : pctOf(n, 2)} />;
                    },
                  },
                  {
                    title: "占上限比", dataIndex: "weight", key: "weightShare",
                    render: (value) => {
                      const n = numOr(value);
                      const limit = numOr(cfg?.max_position_pct);
                      if (n === null || limit === null || limit === 0) return <CellText value={null} fallback="无数据源" />;
                      const share = ((n / limit) * 100).toFixed(1);
                      return <Progress percent={Math.min(100, Number(share))} size="small" format={() => `${share}%`} />;
                    },
                  },
                ]}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                行业暴露：无数据源 —— /api/v3/risk/analytics 只返回标的权重，不含行业分类字段，故不做行业集中度校验与着色。
              </Text>
            </Col>
          </Row>
        ) : (
          <NoSource what="暴露与集中度" why="/api/v3/risk/analytics 未返回 analytics.tickers 权重（组合快照不可用）。" />
        )}
      </ProCard>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={12}>
          <ProCard bordered title="组合净值曲线" loading={analyticsRes.loading} style={{ height: "100%" }}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>{`${curve.length} 点 · ${stampValue(curve[0]?.t)} → ${stampValue(curve[curve.length - 1]?.t)}`}</Text>}>
            {curve.length >= 2 ? (
              <>
                <LineChart points={curve.map((point) => ({ v: point.v }))} label="组合净值（/api/v3/risk/analytics equityCurve）" />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  区间 {stampValue(curve[0]?.t)} → {stampValue(curve[curve.length - 1]?.t)} · 起点 {fixedValue(curve[0]?.v, 4)} ·
                  末点 {fixedValue(curve[curve.length - 1]?.v, 4)} · 服务端最大回撤 {pctNumberText(an?.maxDrawdownPct) ?? "无数据源"}
                </Text>
              </>
            ) : (
              <NoSource what="组合净值曲线" why={`equityCurve 有效点位不足 2 个（实际 ${curve.length} 个）：无法绘图。`} />
            )}
          </ProCard>
        </Col>
        <Col xs={24} lg={12}>
          <ProCard bordered title="回撤曲线" loading={analyticsRes.loading} style={{ height: "100%" }}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>页面按净值序列逐点计算回撤</Text>}>
            {drawdown ? (
              <>
                <LineChart points={drawdown.points} label="回撤 %（相对区间内前高）" />
                <Descriptions column={2} size="small" style={{ marginTop: 8 }}>
                  <Descriptions.Item label="最深回撤">{`${drawdown.minDD.toFixed(2)}%`}</Descriptions.Item>
                  <Descriptions.Item label="最低点日期">{stampValue(drawdown.minAt)}</Descriptions.Item>
                  <Descriptions.Item label="当前回撤">{`${drawdown.lastDD.toFixed(2)}%`}</Descriptions.Item>
                  <Descriptions.Item label="区间">{`${stampValue(drawdown.from)} → ${stampValue(drawdown.to)}`}</Descriptions.Item>
                </Descriptions>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  回撤 = 当日净值 / 区间内前高 − 1（页面计算，输入为服务端真实 equityCurve）；服务端同时给出的
                  maxDrawdownPct = {pctNumberText(an?.maxDrawdownPct) ?? "无数据源"}。回撤阈值与触发次数：无数据源（无阈值字段）。
                </Text>
              </>
            ) : (
              <NoSource what="回撤曲线" why="净值序列点位不足，无法计算回撤。" />
            )}
          </ProCard>
        </Col>
      </Row>

      <ProCard bordered title="风控规则表" loading={configRes.loading}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {configData ? `来源 ${configData.source ?? "无数据源"} · 共 ${CONFIG_ROWS.length} 条（服务端返回的配置项）` : "无数据源"}
        </Text>}>
        {cfg ? (
          <Table
            size="small" rowKey="key" pagination={false} scroll={{ x: "max-content" }}
            dataSource={CONFIG_ROWS.map((row) => {
              const display = configDisplay(cfg[row.key], row.raw);
              const current = row.key === "max_position_pct"
                ? (maxOrderPercent === null ? null : `${maxOrderPercent.toFixed(2)}%（OMS 单笔最大占比）`)
                : null;
              return { key: row.key, name: row.name, field: row.key, value: display.text, note: display.note, current };
            })}
            columns={[
              { title: "规则名", dataIndex: "name", render: (value) => <CellText value={value} fallback="—" /> },
              { title: "字段", dataIndex: "field", render: (value) => <Text code style={{ fontSize: 12 }}>{String(value)}</Text> },
              { title: "阈值", dataIndex: "value", render: (value, row) => (value === null ? <CellText value={null} /> : <Tooltip title={row.note}><span>{value}</span></Tooltip>) },
              { title: "当前值", dataIndex: "current", render: (value) => <CellText value={value} fallback="无数据源" /> },
              { title: "最近更新", render: () => <CellText value={null} fallback="无数据源" /> },
            ]}
          />
        ) : (
          <NoSource what="风控规则表" why="/api/v3/risk 未返回 data.config（工作台风控配置不可用）。" />
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>
          规则热更新状态与「最近更新」时间：无数据源 —— 本服务未暴露规则变更审计字段；规则编辑只能由持风控权限的角色在受约束入口操作，本页只读。
        </Text>
      </ProCard>

      <ProCard bordered title="阻断记录" loading={omsRes.loading}
        extra={
          <Space size={8}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {stageCounts.length ? `阶段分布 ${stageCounts.map((item) => `${item.stage}:${item.count}`).join("、")}` : "阶段分布：无数据源"}
            </Text>
            <Button size="small" loading={sync.loading} onClick={runSync}>同步 OMS 台账</Button>
          </Space>
        }>
        {sync.error ? (
          <Alert type="error" showIcon style={{ marginBottom: 8 }} message="同步失败（POST /api/v3/oms/sync）" description={sync.error} />
        ) : null}
        {sync.ok ? <Alert type="success" showIcon style={{ marginBottom: 8 }} message="同步结果" description={sync.ok} /> : null}

        {blocked.length ? (
          <>
            <Table
              size="small" rowKey={(row, index) => `${row?.id ?? row?.plan_id ?? "order"}-${index}`}
              dataSource={blocked} pagination={false} scroll={{ x: "max-content" }}
              columns={[
                { title: "时间", width: 170, render: (_, row) => <CellText value={stampOrNone(orderStamp(row))} fallback="无数据源" /> },
                { title: "标的", dataIndex: "ticker", render: (value) => <CellText value={value} /> },
                { title: "方向", dataIndex: "side", width: 80, render: (value) => <CellText value={value} fallback="—" /> },
                { title: "数量", dataIndex: "qty", width: 100, render: (value) => <CellText value={fixedValue(value, 0)} /> },
                { title: "金额", dataIndex: "value", width: 130, render: (value) => <CellText value={moneyValue(value)} /> },
                { title: "阶段", dataIndex: "stage", width: 100, render: (value) => <Tag color="red">{String(value)}</Tag> },
                { title: "风控动作", width: 120, render: (_, row) => <CellText value={orderAction(row)} /> },
                { title: "拒绝原因", render: (_, row) => <CellText value={orderReasons(row).join("；")} /> },
                { title: "计划", dataIndex: "plan_id", render: (value) => <CellText value={value} fallback="—" /> },
                { title: "是否已上报", render: () => <CellText value={null} fallback="无数据源" /> },
              ]}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              阻断记录 = /api/v3/oms/orders 中 stage=blocked 的订单；原因取自 risk.reasons。
              「是否已上报」：无数据源（接口未返回上报状态字段）。
            </Text>
          </>
        ) : (
          <>
            <NoSource what="阻断记录"
              why={`当前无 stage=blocked 的订单${stageCounts.length ? `（OMS 阶段分布 ${stageCounts.map((item) => `${item.stage}:${item.count}`).join("、")}）` : "，且接口未返回阶段分布"}。`} />
            {manual.length ? (
              <div style={{ marginTop: 8 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  另有 {manual.length} 笔订单处于人工确认阶段（stage=manual）：{manual.map((order) => order.ticker).filter(Boolean).join("、") || "标的：无数据源"}
                </Text>
              </div>
            ) : null}
          </>
        )}

        {orders.length ? (
          <div style={{ marginTop: 10 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              被触发的风控原因（risk.reasons，共 {preflightReasons.length} 笔订单命中）
            </Text>
            {preflightReasons.length ? (
              <Table
                size="small" style={{ marginTop: 6 }} rowKey="key" dataSource={preflightReasons}
                pagination={{ pageSize: 5, size: "small" }} scroll={{ x: "max-content" }}
                columns={[
                  { title: "标的", dataIndex: "ticker", render: (value) => <CellText value={value} /> },
                  { title: "阶段", dataIndex: "stage", width: 100, render: (value) => <CellText value={value} /> },
                  { title: "动作", dataIndex: "action", width: 120, render: (value) => <CellText value={value} /> },
                  { title: "原因", dataIndex: "reasons", render: (value) => <CellText value={value} /> },
                ]}
              />
            ) : (
              <div><Text type="secondary" style={{ fontSize: 12 }}>无数据源：OMS 订单均未返回 risk.reasons</Text></div>
            )}
          </div>
        ) : null}

        <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
          OMS 说明：{oms?.note ?? "无数据源"} · 确认口径：{oms?.confirmation
            ? `${oms.confirmation.pending ? "有待确认订单" : "无待确认订单"}（TTL ${Math.round((oms.confirmation.ttl_ms ?? 0) / 1000)}s）`
            : "无数据源"}。
          阻断事件上报通道状态：无数据源（接口未暴露上报字段）；本页只读，不触发下单/改单。
        </Text>
      </ProCard>

      <ProCard bordered title="说明" loading={analyticsRes.loading}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          数据来源：/api/v3/risk/analytics（组合净值与风险指标）、/api/v3/risk（风控阈值配置）、
          /api/v3/oms/orders（订单与风控动作）、/api/v3/events（公司行为公告）。
          所有数值为服务端实测；取不到的区块显式标注「无数据源」并写明原因，不使用估算值替代。
        </Text>
      </ProCard>
    </Space>
  );
}
