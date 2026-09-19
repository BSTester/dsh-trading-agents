// V3 工作台「风险监控」页（Ant Design Pro）。
//
// 模块与设计稿原样版 platform/web/public/v3/risk.js（= 功能规格书）**一一对应**：
//   ① 指标卡 5 张：VaR / CVaR / Beta / Alpha / IR（真实值 + 金额/占比/样本/方法说明）
//   ② 三栏 Row/Col：事前（risk.config 5 项阈值）/ 事中（台账回撤 + 组合年化波动 + 事件流 events+audit）/
//      事后（Kupiec 通过与否 + 破位/期望 + 组合年化 + 最大回撤 + 区间）
//   ③ 暴露与集中度（analytics.tickers 权重 BarList）+ 单票上限说明
//   ④ 净值/回撤曲线（LineChart ← analytics.equityCurve）
//   ⑤ 风控规则表（阈值 + 来源 + 状态）
//   ⑥ 阻断记录（OMS 未自动放行订单，无则空态）
//   ⑦ 无数据源项：行业暴露、行业/因子/个股归因、逐标的 ATR、杠杆率、流动性评分、逐日 VaR 序列
//
// 本页**没有任何写动作**：全部是 GET /api/v3/*（useV3 读），不提供规则编辑入口。
import React from "react";
import {
  Alert, Col, Descriptions, Empty, Row, Space, Statistic, Table, Tag, Timeline, Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { useV3, fmt, noSourceText } from "../services/api.js";
import { LineChart, BarList } from "../components/charts.jsx";

const { Text, Link } = Typography;

/* ── 通用小工具（单块失败只影响该块） ───────────────────────────────────── */
class Block extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error) {
    return { error };
  }
  render() {
    const { title } = this.props;
    if (this.state.error) {
      return (
        <ProCard title={title} bordered>
          <Alert type="error" showIcon message={`${title} 渲染失败`}
            description={String((this.state.error && this.state.error.message) || this.state.error)} />
        </ProCard>
      );
    }
    return this.props.children;
  }
}

function NoSource({ what, why }) {
  return <Empty imageStyle={{ display: "none" }} style={{ margin: 0 }} description={<Text type="secondary">{noSourceText(what, why)}</Text>} />;
}

const OK = (env) => Boolean(env && env.ok);
const fin = (value) => Number.isFinite(Number(value));
const asArray = (value) => (Array.isArray(value) ? value : []);

function envError(env, fallback) {
  const error = (env && env.error) || {};
  const code = error.code ? String(error.code) : "";
  const message = error.message ? String(error.message) : "";
  if (code || message) return code ? `${code}：${message || fallback || "请求失败"}` : message;
  return fallback || "接口未返回 error.code/message";
}

/** 负号统一为 U+2212（与设计稿版同一字形口径）。 */
const minus = (text) => String(text).replace(/^-/, "−");
const fnum = (value, digits) => (fin(value) ? minus(fmt.num(value, digits)) : "—");
const pct = (value, digits = 2) => (fin(value) ? `${Number(value) < 0 ? "−" : ""}${Math.abs(Number(value)).toFixed(digits)}%` : "—");
const spct = (value, digits = 2) => (fin(value) ? `${Number(value) >= 0 ? "+" : "−"}${Math.abs(Number(value)).toFixed(digits)}%` : "—");
const day = (value) => String(value || "").slice(0, 10);
const toneOf = (value) => (!fin(value) || Number(value) === 0 ? undefined : Number(value) > 0 ? "#3fb950" : "#f8514d");

/** 净值曲线统计：峰值/谷底/最大回撤/修复天数（全部由 analytics.equityCurve 实数推导）。 */
function curveStats(curve) {
  const points = asArray(curve)
    .map((point) => ({ t: String((point && point.t) || ""), v: Number(point && point.v) }))
    .filter((point) => point.t && Number.isFinite(point.v));
  if (points.length < 2) return null;
  let running = points[0];
  let dd = 0;
  let peakAt = points[0];
  let trough = points[0];
  for (const point of points) {
    if (point.v > running.v) running = point;
    const value = point.v / running.v - 1;
    if (value < dd) {
      dd = value;
      peakAt = running;
      trough = point;
    }
  }
  let recovery = null;
  for (let index = points.indexOf(trough) + 1; index < points.length; index += 1) {
    if (points[index].v >= peakAt.v) { recovery = points[index]; break; }
  }
  const days = recovery ? Math.round((Date.parse(recovery.t) - Date.parse(trough.t)) / 86400000) : null;
  return { points, ddPct: dd * 100, peakAt, trough, recovery, days, first: points[0], last: points[points.length - 1] };
}

/** 台账持仓汇总：只做只数统计，绝不做跨账户/跨币种金额合并。 */
function positionSummary(positions) {
  const groups = positions && Array.isArray(positions.groups) ? positions.groups : [];
  let total = 0;
  let maxPerAccount = 0;
  const rows = [];
  for (const group of groups) {
    const list = asArray(group.positions);
    if (list.length === 0) continue;
    total += list.length;
    maxPerAccount = Math.max(maxPerAccount, list.length);
    rows.push({ account: group.account || group.acc_id || "—", n: list.length, totalAsset: group.total_asset, cash: group.cash });
  }
  return { total, maxPerAccount, rows };
}

/* ── ① 指标卡 5 张 ──────────────────────────────────────────────────────── */
function MetricCards({ env }) {
  const analytics = OK(env) ? env.analytics : null;
  if (!analytics) {
    return (
      <ProCard title="组合风险量（VaR / CVaR / Beta / Alpha / IR）" bordered>
        <NoSource what="组合风险量" why={envError(env, "GET /api/v3/risk/analytics 取不到（口径：自选池等权组合，需 ≥40 个对齐交易日）")} />
      </ProCard>
    );
  }
  const obs = fin(analytics.observations) ? Number(analytics.observations) : null;
  const level = fin(analytics.confidence) ? Number(analytics.confidence) * 100 : null;
  const cards = [
    {
      key: "var",
      title: `VaR（${level === null ? "—" : level.toFixed(0)}%，1d）`,
      value: fin(analytics.varAmount) ? Number(analytics.varAmount) : null,
      prefix: "¥",
      sub: `占权益 ${pct(analytics.varDailyPct, 3)} · 历史模拟法 ${obs === null ? "—" : obs} 日`,
      tip: "来源 /api/v3/risk/analytics.varAmount（= varDailyPct × nav）",
      color: "#4c8dff",
    },
    {
      key: "cvar",
      title: `CVaR（${level === null ? "—" : level.toFixed(0)}%，1d）`,
      value: fin(analytics.cvarAmount) ? Number(analytics.cvarAmount) : null,
      prefix: "¥",
      sub: `占权益 ${pct(analytics.cvarDailyPct, 3)} · 超出 VaR 的尾部均值`,
      tip: "来源 /api/v3/risk/analytics.cvarAmount",
      color: "#39a0ed",
    },
    {
      key: "beta",
      title: "Beta",
      value: fin(analytics.beta) ? Number(analytics.beta) : null,
      precision: 3,
      sub: `基准 ${env.benchmarkTicker || "—"} · ${obs === null ? "—" : obs} 个交易日`,
      tip: "来源 /api/v3/risk/analytics.beta（对基准日收益回归）",
      color: undefined,
    },
    {
      key: "alpha",
      title: "Alpha（年化）",
      value: fin(analytics.alphaAnnPct) ? Number(analytics.alphaAnnPct) : null,
      precision: 2,
      suffix: "%",
      sub: `年化 · 基准年化 ${spct(analytics.benchmarkAnnReturnPct, 2)}`,
      tip: "来源 /api/v3/risk/analytics.alphaAnnPct（年化 252）",
      color: toneOf(analytics.alphaAnnPct),
    },
    {
      key: "ir",
      title: "IR（信息比率）",
      value: fin(analytics.ir) ? Number(analytics.ir) : null,
      precision: 3,
      sub: `组合年化 ${spct(analytics.annReturnPct, 2)} · 年化波动 ${pct(analytics.annVolPct)}`,
      tip: "来源 /api/v3/risk/analytics.ir（年化 α / 跟踪误差）",
      color: "#8b5cf6",
    },
  ];
  return (
    <ProCard
      title="组合风险量（VaR / CVaR / Beta / Alpha / IR）"
      bordered
      extra={
        <Space size={8} wrap>
          <Tag color="blue">阈值源 /api/v3/risk</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>
            组合口径「{env.portfolioSource || "—"}」· 区间 {day(analytics.window && analytics.window.from)} → {day(analytics.window && analytics.window.to)} · {obs === null ? "—" : obs} 个交易日
          </Text>
        </Space>
      }
    >
      <Row gutter={[12, 12]}>
        {cards.map((card) => (
          <Col key={card.key} xs={24} sm={12} lg={8} xl={{ flex: "1 1 19%" }} style={{ minWidth: 190 }}>
            <ProCard bordered size="small" title={card.title} tooltip={card.tip} style={{ height: "100%" }}>
              <Statistic
                value={card.value === null ? "—" : card.value}
                precision={card.value === null ? undefined : card.precision}
                prefix={card.prefix}
                suffix={card.suffix}
                valueStyle={{ fontSize: 20, color: card.color }}
              />
              <Text type="secondary" style={{ fontSize: 11 }}>{card.sub}</Text>
            </ProCard>
          </Col>
        ))}
      </Row>
      <Alert
        style={{ marginTop: 12 }}
        type="info"
        showIcon
        message={`方法：${analytics.method || "未返回方法说明"}`}
        description={
          <Text type="secondary" style={{ fontSize: 12 }}>
            逐日 VaR 序列无数据源：接口只返回区间 VaR/CVaR 点值，未返回逐日序列，故指标卡不绘制迷你走势线（{noSourceText("逐日 VaR 序列", "risk/analytics 未返回逐日序列")}）。
          </Text>
        }
      />
    </ProCard>
  );
}

/* ── ② 三栏：事前 / 事中 / 事后 ─────────────────────────────────────────── */
function PreTradeCard({ riskEnv, orders, nav, analytics, positions }) {
  const config = OK(riskEnv) && riskEnv.data ? (riskEnv.data.config || {}) : null;
  if (!config) {
    return (
      <ProCard title="事前风控" bordered>
        <NoSource what="事前风控" why={envError(riskEnv, "GET /api/v3/risk 未返回风控配置")} />
      </ProCard>
    );
  }
  const source = riskEnv.data.source || "未返回来源";
  const reasonText = orders.map((order) => asArray(order.risk && order.risk.reasons).join("；")).join("；");
  const limitMatch = reasonText.match(/[>＞]\s*([0-9.]+)\s*%/);
  const singleLimit = limitMatch ? Number(limitMatch[1]) : null;
  const maxOrder = orders.reduce((acc, order) => (fin(order.value) && (!acc || Number(order.value) > Number(acc.value)) ? order : acc), null);
  const singleRatio = maxOrder && fin(nav) && Number(nav) ? (Number(maxOrder.value) / Number(nav)) * 100 : null;
  const weights = analytics && analytics.tickers ? Object.entries(analytics.tickers).map(([ticker, weight]) => [ticker, Number(weight) * 100]) : [];
  const heaviest = weights.slice().sort((a, b) => b[1] - a[1])[0] || null;
  const cap = fin(config.max_position_pct) ? Number(config.max_position_pct) * 100 : null;
  const over = orders.filter((order) => order.stage === "blocked" || (order.risk && order.risk.action === "blocked")).length;
  const manual = orders.filter((order) => order.stage === "manual").length;
  const summary = positionSummary(positions);

  const items = [
    {
      key: "cap",
      label: "单标的上限",
      threshold: cap === null ? "无数据源（config.max_position_pct 未返回）" : `≤ 组合权益 ${pct(cap, 0)}`,
      note: heaviest ? `组合最大单票权重 ${pct(heaviest[1])}（${heaviest[0]}，等权），距上限 ${pct(cap - heaviest[1])}` : "无数据源：组合权重取不到",
      tone: !heaviest || cap === null ? ["default", "无数据"] : (heaviest[1] <= cap ? ["green", "正常"] : ["red", "超限"]),
    },
    {
      key: "order",
      label: "单笔下单上限",
      threshold: singleLimit === null ? "≤ 权益 2%（OMS check_order 口径，未从台账回读到阈值）" : `≤ 权益 ${pct(singleLimit, 0)}（OMS check_order 口径）`,
      note: maxOrder ? `台账最大单笔 ${pct(singleRatio)}（${maxOrder.ticker} ${maxOrder.side} ${fmt.num(maxOrder.qty, 0)} 股 / ${fmt.money(maxOrder.value)}）` : "台账无订单",
      tone: singleLimit !== null && singleRatio !== null && singleRatio > singleLimit ? ["red", "超限"] : ["green", "正常"],
    },
    {
      key: "budget",
      label: "单笔风险预算",
      threshold: fin(config.risk_per_trade) ? `≤ 权益 ${pct(Number(config.risk_per_trade) * 100, 1)}（config.risk_per_trade）` : "无数据源（config.risk_per_trade 未返回）",
      note: `无数据源：工具面未提供按止损距离折算的单笔风险（止损 ATR 倍数 ${fmt.num(config.stop_atr_mult, 1)}x，逐标的 ATR 未提供）`,
      tone: ["default", "无数据"],
    },
    {
      key: "count",
      label: "最大持仓数",
      threshold: fin(config.max_positions) ? `≤ ${fmt.num(config.max_positions, 0)} 只（config.max_positions，策略口径）` : "无数据源（config.max_positions 未返回）",
      note: summary.total ? `台账持仓 ${summary.total} 只 / ${summary.rows.length} 个模拟账户（最大单账户 ${summary.maxPerAccount} 只，口径不同）` : "台账无持仓",
      tone: ["gold", "口径不同"],
    },
    {
      key: "loss",
      label: "单日亏损上限",
      threshold: fin(config.daily_loss_limit_pct) ? `单日 ≤ ${pct(Number(config.daily_loss_limit_pct) * 100, 0)}（config.daily_loss_limit_pct）` : "无数据源（config.daily_loss_limit_pct 未返回）",
      note: analytics && fin(analytics.maxDrawdownPct) ? `组合区间累计最大回撤 ${pct(analytics.maxDrawdownPct)}（analytics，非单日口径）` : "无数据源：组合净值曲线不足",
      tone: ["gold", "关注"],
    },
  ];
  return (
    <ProCard
      title="事前风控"
      bordered
      extra={<Text type="secondary" style={{ fontSize: 12 }}>阈值配置与台账校验口径 · MCP 工具 risk（配置）/ calc_var（组合风险量）</Text>}
      style={{ height: "100%" }}
    >
      <Timeline
        items={items.map((item) => ({
          key: item.key,
          color: item.tone[0] === "red" ? "red" : item.tone[0] === "green" ? "green" : item.tone[0] === "gold" ? "orange" : "gray",
          children: (
            <Space direction="vertical" size={2} style={{ width: "100%" }}>
              <Space size={6} wrap>
                <Text strong>{item.label}</Text>
                <Text>{item.threshold}</Text>
                <Tag color={item.tone[0] === "default" ? undefined : item.tone[0]}>{item.tone[1]}</Tag>
              </Space>
              <Text type="secondary" style={{ fontSize: 12 }}>{item.note}</Text>
            </Space>
          ),
        }))}
      />
      <Text type="secondary" style={{ fontSize: 12 }}>
        校验 <Text strong>{orders.length}</Text> 单 · 超单笔上限退回人工 <Text strong>{manual}</Text> 单
        {over ? ` · 硬阻断 ${over} 单` : " · 硬阻断 0 单"} · 阈值 5 项 · source {source}
      </Text>
    </ProCard>
  );
}

function LiveCard({ analytics, audit, events }) {
  const stats = analytics ? curveStats(analytics.equityCurve) : null;
  const rows = [];
  for (const entry of asArray(audit).slice(0, 6)) {
    const kind = String(entry.kind || "");
    rows.push({
      key: `audit-${entry.id || rows.length}`,
      time: fmt.stamp(entry.at),
      type: `${entry.source_label || (kind === "signal" ? "量化信号" : kind === "fill" ? "台账成交" : "审计链")} · ${entry.ticker || "—"}`,
      tone: kind === "fill" ? { color: "green", text: "成交" } : kind === "order-facts" ? { color: undefined, text: "查询" } : { color: "blue", text: "提示" },
      detail: entry.detail || "—",
    });
  }
  for (const event of asArray(events).slice(0, 3)) {
    rows.push({
      key: `event-${event.date}-${event.type}`,
      time: day(event.date) || day(event.announced) || "—",
      type: `公开披露 · ${event.type || "事件"}`,
      tone: { color: "gold", text: "公告" },
      detail: `${event.detail || "—"}${event.announced ? `（公告日 ${day(event.announced)}）` : ""}`,
    });
  }
  const columns = [
    { title: "时间", dataIndex: "time", width: 150, render: (value) => <Text style={{ fontSize: 12 }}>{String(value || "—")}</Text> },
    { title: "类型", dataIndex: "type", width: 220, render: (value) => <Text style={{ fontSize: 12 }}>{String(value)}</Text> },
    { title: "级别", dataIndex: "tone", width: 80, render: (value) => <Tag color={value.color}>{value.text}</Tag> },
    { title: "处置动作", dataIndex: "detail", render: (value) => <Text type="secondary" style={{ fontSize: 12 }}>{String(value)}</Text> },
  ];
  return (
    <ProCard
      title="事中风控"
      bordered
      extra={<Text type="secondary" style={{ fontSize: 12 }}>来源 /api/v3/audit + /api/v3/events（窗口 180 日）</Text>}
      style={{ height: "100%" }}
    >
      <Row gutter={[8, 8]}>
        <Col span={8}>
          <Text style={{ fontSize: 12 }} type="secondary">台账最大回撤</Text>
          <div style={{ fontSize: 16, color: "#8b97a5" }}>—</div>
          <Text type="secondary" style={{ fontSize: 11 }}>{noSourceText("台账逐日权益", "equity 台账只有 1 个点位，无法算回撤")}</Text>
        </Col>
        <Col span={8}>
          <Text style={{ fontSize: 12 }} type="secondary">组合区间最大回撤</Text>
          <div style={{ fontSize: 16, color: "#f8514d" }}>{analytics && fin(analytics.maxDrawdownPct) ? pct(analytics.maxDrawdownPct) : "—"}</div>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {stats ? `峰值 ${day(stats.peakAt.t)} · 谷底 ${day(stats.trough.t)}（自选池等权 ${stats.points.length} 日）` : "无数据源：净值曲线不足"}
          </Text>
        </Col>
        <Col span={8}>
          <Text style={{ fontSize: 12 }} type="secondary">年化波动率</Text>
          <div style={{ fontSize: 16 }}>{analytics && fin(analytics.annVolPct) ? pct(analytics.annVolPct) : "—"}</div>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {analytics && fin(analytics.observations)
              ? `${analytics.observations} 个交易日 · 组合年化 ${spct(analytics.annReturnPct, 2)} · 基准年化 ${spct(analytics.benchmarkAnnReturnPct, 2)}`
              : "无数据源"}
          </Text>
        </Col>
      </Row>
      <div style={{ marginTop: 10 }}>
        {rows.length === 0
          ? <NoSource what="事中事件流" why="审计链与公开披露事件窗口内均无记录" />
          : <Table size="small" rowKey="key" pagination={false} columns={columns} dataSource={rows} scroll={{ x: 600 }} />}
      </div>
    </ProCard>
  );
}

function PostTradeCard({ env }) {
  const analytics = OK(env) ? env.analytics : null;
  const stats = analytics ? curveStats(analytics.equityCurve) : null;
  const kupiec = analytics && analytics.kupiec ? analytics.kupiec : null;
  const expected = kupiec && fin(kupiec.observations) && analytics && fin(analytics.confidence)
    ? Number(kupiec.observations) * (1 - Number(analytics.confidence)) : null;
  return (
    <ProCard
      title="事后风控"
      bordered
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          {analytics
            ? `区间 ${day(analytics.window && analytics.window.from)} → ${day(analytics.window && analytics.window.to)}`
            : "无数据源"}
        </Text>
      }
      style={{ height: "100%" }}
    >
      {!analytics ? (
        <NoSource what="事后风控" why={envError(env, "GET /api/v3/risk/analytics 取不到（需 ≥40 个对齐交易日）")} />
      ) : (
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          {!kupiec ? (
            <NoSource what="Kupiec POF 检验" why="样本不足（需 ≥40 个对齐交易日）" />
          ) : (
            <Alert
              type={kupiec.pass ? "success" : "error"}
              showIcon
              message={kupiec.pass ? "Kupiec 检验通过" : "Kupiec 检验未通过"}
              description={
                <Text style={{ fontSize: 12 }}>
                  p = {fnum(kupiec.pValue, 4)} · 样本 {fmt.num(kupiec.observations, 0)} 日 · 破位 {fmt.num(kupiec.breaches, 0)} 次 / 期望{" "}
                  {expected === null ? "—" : expected.toFixed(2)} 次（{(Number(analytics.confidence) * 100).toFixed(0)}% VaR · LR {fnum(kupiec.lr, 4)}）
                </Text>
              }
            />
          )}
          <Row gutter={[8, 8]}>
            <Col span={12}>
              <Text style={{ fontSize: 12 }} type="secondary">组合最大回撤</Text>
              <div style={{ fontSize: 16, color: "#f8514d" }}>{analytics && fin(analytics.maxDrawdownPct) ? pct(analytics.maxDrawdownPct) : "—"}</div>
              <Text type="secondary" style={{ fontSize: 11 }}>
                {stats ? `区间 ${day(stats.first.t)} → ${day(stats.last.t)} · 峰值 ${day(stats.peakAt.t)}` : "无数据源：净值曲线不足"}
              </Text>
            </Col>
            <Col span={12}>
              <Text style={{ fontSize: 12 }} type="secondary">组合年化收益</Text>
              <div style={{ fontSize: 16, color: toneOf(analytics.annReturnPct) }}>{analytics && fin(analytics.annReturnPct) ? spct(analytics.annReturnPct, 2) : "—"}</div>
              <Text type="secondary" style={{ fontSize: 11 }}>基准年化 {spct(analytics.benchmarkAnnReturnPct, 2)} · 年化波动 {pct(analytics.annVolPct)}</Text>
            </Col>
            <Col span={12}>
              <Text style={{ fontSize: 12 }} type="secondary">修复天数</Text>
              <div style={{ fontSize: 16 }}>{stats ? (stats.recovery ? `${stats.days} 天` : "尚未收复") : "—"}</div>
              <Text type="secondary" style={{ fontSize: 11 }}>
                {stats ? (stats.recovery ? `${day(stats.recovery.t)} 收复前高` : `谷底 ${day(stats.trough.t)}，截至 ${day(stats.last.t)} 未收复前高`) : "无数据源：净值曲线不足"}
              </Text>
            </Col>
            <Col span={12}>
              <Text style={{ fontSize: 12 }} type="secondary">区间累计</Text>
              <div style={{ fontSize: 16, color: toneOf(stats ? stats.last.v - 1 : null) }}>{stats ? spct((stats.last.v - 1) * 100, 2) : "—"}</div>
              <Text type="secondary" style={{ fontSize: 11 }}>
                {stats ? `${day(stats.first.t)} 起点归一 1.0 → 末值 ${fmt.num(stats.last.v, 4)}` : "无数据源"}
              </Text>
            </Col>
          </Row>
          <Descriptions size="small" column={1} bordered
            items={[
              { key: "method", label: "计算方法", children: analytics.method || "—" },
              { key: "conf", label: "置信度 / 样本", children: `${fin(analytics.confidence) ? (Number(analytics.confidence) * 100).toFixed(0) : "—"}% · ${fmt.num(analytics.observations, 0)} 个观测` },
              { key: "bench", label: "基准", children: `${analytics.benchmarkTicker || env.benchmarkTicker || "—"} · 基准年化 ${spct(analytics.benchmarkAnnReturnPct, 2)}` },
            ]}
          />
        </Space>
      )}
    </ProCard>
  );
}

/* ── ③ 暴露与集中度 ─────────────────────────────────────────────────────── */
function ExposureCard({ env, riskEnv, ordersEnv }) {
  const analytics = OK(env) ? env.analytics : null;
  const config = OK(riskEnv) && riskEnv.data ? (riskEnv.data.config || {}) : null;
  const cap = config && fin(config.max_position_pct) ? Number(config.max_position_pct) * 100 : null;
  const entries = analytics && analytics.tickers
    ? Object.entries(analytics.tickers).map(([ticker, weight]) => [ticker, Number(weight) * 100]).sort((a, b) => b[1] - a[1])
    : [];
  const top5 = entries.slice(0, 5).reduce((sum, row) => sum + row[1], 0);
  const industrySource = OK(ordersEnv) ? ordersEnv.industry_source : null;
  return (
    <ProCard
      title="暴露与集中度"
      bordered
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          ┊ {cap === null ? "—" : cap.toFixed(0)}% 单票上限 · 满刻度 30% · 来源 /api/v3/risk/analytics
        </Text>
      }
    >
      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Text strong style={{ fontSize: 12 }}>行业暴露（占组合净值）</Text>
          <div style={{ marginTop: 8 }}>
            <NoSource what="行业暴露" why={`工作台工具面无行业分类数据源（OMS industry_source=${String(industrySource || "no-data").slice(0, 8)}…）：行业上限按 0% 不参与自动阻断，红线需人工核对`} />
          </div>
        </Col>
        <Col xs={24} lg={12}>
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Space size={8} wrap>
              <Text strong style={{ fontSize: 12 }}>单票集中度 Top{entries.length || 0}</Text>
              <Text type="secondary" style={{ fontSize: 12 }}>前五大合计 {entries.length ? pct(top5) : "—"}</Text>
            </Space>
            {entries.length === 0 ? (
              <NoSource what="单票集中度" why="组合权重取不到（/api/v3/risk/analytics.tickers 为空）" />
            ) : (
              <BarList items={entries.map(([ticker, weight]) => [`${ticker} 等权`, fmt.num(weight, 2)])} />
            )}
          </Space>
        </Col>
      </Row>
      <Alert
        style={{ marginTop: 12 }}
        type="warning"
        showIcon
        message={`单票上限：${cap === null ? "无数据源（config.max_position_pct 未返回）" : `≤ 组合权益 ${pct(cap, 0)}（config.max_position_pct）`}`}
        description={
          <Text type="secondary" style={{ fontSize: 12 }}>
            权重口径＝组合内权重（{(analytics && analytics.portfolioSource) || "组合口径未知"}，共 {entries.length} 个标的）；
            行业分类与因子暴露工作台未提供，行业红线不参与自动阻断（OMS industry_source=no-data）。
            杠杆率 / 流动性评分：{noSourceText("杠杆率与流动性评分", "positions 不返回融资余额与盘口深度")}。
          </Text>
        }
      />
    </ProCard>
  );
}

/* ── ④ 净值/回撤曲线 ───────────────────────────────────────────────────── */
function CurveCard({ env }) {
  const analytics = OK(env) ? env.analytics : null;
  const stats = analytics ? curveStats(analytics.equityCurve) : null;
  const values = stats ? stats.points.map((point) => point.v * 100) : [];
  return (
    <ProCard
      title="净值/回撤曲线"
      bordered
      extra={
        <Space size={8} wrap>
          {stats ? (
            <>
              <Tag color="blue">区间 {day(stats.first.t)} → {day(stats.last.t)} · {stats.points.length} 个交易日</Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>末值 {fmt.num(stats.last.v, 4)} · 区间累计 {spct((stats.last.v - 1) * 100, 2)}</Text>
            </>
          ) : <Tag>无数据源</Tag>}
        </Space>
      }
    >
      {!analytics ? (
        <NoSource what="组合净值曲线" why={envError(env, "GET /api/v3/risk/analytics 取不到（对齐后 <40 个交易日）")} />
      ) : !stats ? (
        <NoSource what="组合净值曲线" why="/api/v3/risk/analytics.equityCurve 取不到（对齐后 <40 个交易日）" />
      ) : (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <LineChart values={values} height={220} labels={[day(stats.first.t), day(stats.last.t)]}
            name={`组合净值（起点归一 1.0，${stats.points.length} 个交易日）`} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            曲线＝{analytics.portfolioSource || "组合"}按 {stats.points.length} 个交易日回放的归一日权益（起点 {day(stats.first.t)} = 1.0，图上 ×100 绘制）；
            区间最大回撤 {pct(analytics.maxDrawdownPct)}（峰值 {day(stats.peakAt.t)}，谷底 {day(stats.trough.t)}）。
            工作台未提供逐日回撤序列与阈值触发事件，故不绘制阈值线与触发点。
          </Text>
        </Space>
      )}
    </ProCard>
  );
}

/* ── ⑤ 风控规则表 ───────────────────────────────────────────────────────── */
function RulesCard({ riskEnv, orders, nav, env }) {
  const config = OK(riskEnv) && riskEnv.data ? (riskEnv.data.config || {}) : null;
  const analytics = OK(env) ? env.analytics : null;
  if (!config) {
    return (
      <ProCard title="风控规则" bordered>
        <NoSource what="风控规则" why={envError(riskEnv, "GET /api/v3/risk 未返回风控配置")} />
      </ProCard>
    );
  }
  const source = riskEnv.data.source || "未返回来源";
  const weights = analytics && analytics.tickers ? Object.entries(analytics.tickers).map(([ticker, weight]) => [ticker, Number(weight) * 100]) : [];
  const heaviest = weights.slice().sort((a, b) => b[1] - a[1])[0] || null;
  const maxOrder = orders.reduce((acc, order) => (fin(order.value) && (!acc || Number(order.value) > Number(acc.value)) ? order : acc), null);
  const singleRatio = maxOrder && fin(nav) && Number(nav) ? (Number(maxOrder.value) / Number(nav)) * 100 : null;
  const rows = [
    {
      key: "cap",
      name: "单标的上限",
      threshold: `≤ 组合权益 ${pct(config.max_position_pct == null ? null : Number(config.max_position_pct) * 100, 0)}（config.max_position_pct）`,
      current: heaviest ? pct(heaviest[1]) : "无数据源",
      tone: !heaviest || config.max_position_pct == null ? { color: undefined, text: "无数据" } : (heaviest[1] <= Number(config.max_position_pct) * 100 ? { color: "green", text: "正常" } : { color: "red", text: "超限" }),
      source: `组合权重 /api/v3/risk/analytics${heaviest ? `（${heaviest[0]}）` : ""}`,
      updated: "无数据源",
    },
    {
      key: "order",
      name: "单笔下单上限",
      threshold: "≤ 权益 2%（OMS check_order 口径）",
      current: singleRatio === null ? "—" : pct(singleRatio),
      tone: singleRatio !== null && singleRatio > 2 ? { color: "red", text: "超限" } : { color: "green", text: "正常" },
      source: maxOrder ? `台账最大单笔 ${maxOrder.ticker} ${fmt.money(maxOrder.value)}` : "台账无订单",
      updated: "无数据源",
    },
    {
      key: "budget",
      name: "单笔风险预算",
      threshold: `≤ 权益 ${pct(config.risk_per_trade == null ? null : Number(config.risk_per_trade) * 100, 1)}（config.risk_per_trade）`,
      current: "无数据源",
      tone: { color: undefined, text: "无数据" },
      source: `止损 ATR 倍数 ${fmt.num(config.stop_atr_mult, 1)}x：逐标的 ATR 未提供`,
      updated: "无数据源",
    },
    {
      key: "atr",
      name: "止损 ATR 倍数",
      threshold: `stop_atr_mult = ${fmt.num(config.stop_atr_mult, 1)}x（config）`,
      current: "无数据源",
      tone: { color: undefined, text: "无数据" },
      source: "工具面无逐标的 ATR 与止损价",
      updated: "无数据源",
    },
    {
      key: "count",
      name: "最大持仓数",
      threshold: `≤ ${fmt.num(config.max_positions, 0)} 只（config.max_positions，策略口径）`,
      current: "见 /api/v3/execution 持仓",
      tone: { color: "gold", text: "口径不同" },
      source: "台账按账户统计，不跨账户合并",
      updated: "无数据源",
    },
    {
      key: "loss",
      name: "单日亏损上限",
      threshold: `单日 ≤ ${pct(config.daily_loss_limit_pct == null ? null : Number(config.daily_loss_limit_pct) * 100, 0)}（config.daily_loss_limit_pct）`,
      current: analytics && fin(analytics.maxDrawdownPct) ? pct(analytics.maxDrawdownPct) : "—",
      tone: { color: "gold", text: "关注" },
      source: "区间累计回撤（非单日口径）· analytics.maxDrawdownPct",
      updated: "无数据源",
    },
  ];
  const columns = [
    { title: "规则名", dataIndex: "name", width: 130, render: (value) => <Text strong style={{ fontSize: 12 }}>{String(value)}</Text> },
    { title: "阈值", dataIndex: "threshold", render: (value) => <Text style={{ fontSize: 12 }}>{String(value)}</Text> },
    { title: "当前值", dataIndex: "current", width: 150, render: (value) => <Text style={{ fontSize: 12 }}>{String(value)}</Text> },
    { title: "状态", dataIndex: "tone", width: 110, render: (value) => <Tag color={value.color}>{value.text}</Tag> },
    { title: "来源", dataIndex: "source", render: (value) => <Text type="secondary" style={{ fontSize: 12 }}>{String(value)}</Text> },
    { title: "最近更新", dataIndex: "updated", width: 110, render: (value) => <Text type="secondary" style={{ fontSize: 12 }}>{String(value)}</Text> },
    { title: "操作", key: "action", width: 130, render: () => <Text type="secondary" style={{ fontSize: 12 }}>本控制台无编辑入口</Text> },
  ];
  const hit = orders.filter((order) => order.stage === "manual" || order.stage === "blocked").length;
  return (
    <ProCard
      title="风控规则"
      bordered
      extra={<Text type="secondary" style={{ fontSize: 12 }}>展示 {rows.length} 行 · 配置项 {Object.keys(config).length} 个 · source {source}</Text>}
    >
      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        <Table size="small" rowKey="key" pagination={false} columns={columns} dataSource={rows} scroll={{ x: 1000 }} />
        <Text type="secondary" style={{ fontSize: 12 }}>
          阈值来自交易平台风控配置（{source}），带「无数据源」的当前值表示工具面确实没有对应读数，不用估算值顶替；
          本控制台不提供规则编辑入口——阈值修改只经工作台受约束入口并留审计痕迹。本台账 {orders.length} 单中 {hit} 单未自动放行。
        </Text>
      </Space>
    </ProCard>
  );
}

/* ── ⑥ 阻断记录 ─────────────────────────────────────────────────────────── */
function BlocksCard({ orders }) {
  const rows = orders
    .filter((order) => (order.risk && order.risk.action !== "auto") || ["blocked", "manual", "rejected"].includes(String(order.stage)))
    .slice()
    .sort((a, b) => Number(b.value || 0) - Number(a.value || 0));
  const columns = [
    { title: "时间", dataIndex: "at", width: 150, render: (value) => <Text style={{ fontSize: 12 }}>{fmt.stamp(value)}</Text> },
    { title: "标的", dataIndex: "ticker", width: 110, render: (value) => <Text code style={{ fontSize: 12 }}>{String(value || "—")}</Text> },
    { title: "触发规则", dataIndex: "rule", width: 160, render: (value) => <Text style={{ fontSize: 12 }}>{String(value)}</Text> },
    { title: "拒绝原因", dataIndex: "reasons", render: (value) => <Text type="secondary" style={{ fontSize: 12 }}>{String(value)}</Text> },
    { title: "是否已上报", dataIndex: "reported", width: 110, render: (value) => <Tag color={value.color}>{value.text}</Tag> },
  ];
  const data = rows.map((order) => ({
    key: order.id || `${order.ticker}-${order.first_seen_at}`,
    at: order.updated_at || order.first_seen_at,
    ticker: order.ticker,
    rule: order.stage === "blocked" ? "红线强制阻断" : order.stage === "rejected" ? "券商/通道拒绝" : "单笔下单上限",
    reasons: asArray(order.risk && order.risk.reasons).join("；") || "未给出原因",
    reported: order.stage === "blocked" ? { color: "red", text: "已阻断" } : { color: "blue", text: "已登记" },
  }));
  const blocked = rows.filter((order) => order.stage === "blocked").length;
  const rejected = rows.filter((order) => order.stage === "rejected").length;
  return (
    <ProCard
      title="阻断记录"
      bordered
      extra={<Text type="secondary" style={{ fontSize: 12 }}>台账 {rows.length} 条 · 硬阻断 {blocked} 条 · 已拒绝 {rejected} 条 · 全量留痕</Text>}
    >
      {rows.length === 0 ? (
        <NoSource what="阻断/退回记录" why="OMS 台账无 blocked/rejected 阶段订单（本服务未挂载 SDK JSON-RPC / Headless 通道，记录以 /api/v3/oms/orders 台账为准）" />
      ) : (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Table size="small" rowKey="key" pagination={false} columns={columns} dataSource={data} scroll={{ x: 900 }} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            口径：stage 为 manual/blocked/rejected 的订单（当前台账 {rows.length} 单；manual＝超单笔上限退回人工确认）。
            本服务未挂载 SDK JSON-RPC / Headless 通道，记录以 /api/v3/oms/orders 台账为准。
          </Text>
        </Space>
      )}
    </ProCard>
  );
}

/* ── ⑦ 无数据源清单 ─────────────────────────────────────────────────────── */
function NoSourceCard() {
  const items = [
    { what: "行业暴露 / 行业分类", why: "工具面无行业分类数据源（OMS industry_source=no-data），行业上限按 0% 不参与自动阻断" },
    { what: "行业归因 / 因子归因 / 个股归因", why: "工作台工具面无归因数据源，本页不估算" },
    { what: "逐标的 ATR 与单笔风险折算", why: "工具面未提供逐标的 ATR 与止损价，风险预算无法按止损距离折算" },
    { what: "杠杆率", why: "positions 不返回融资余额/保证金占用" },
    { what: "流动性评分", why: "工具面无盘口深度与流动性评分数据源" },
    { what: "逐日 VaR 序列", why: "risk/analytics 只返回区间 VaR/CVaR 点值，未返回逐日序列（故指标卡不绘制迷你走势线）" },
  ];
  return (
    <ProCard title="无数据源项（逐项写明原因，不填占位数字）" bordered>
      <Space direction="vertical" size={4} style={{ width: "100%" }}>
        {items.map((item) => (
          <Text key={item.what} style={{ fontSize: 12 }}>
            <Text strong>{item.what}</Text>
            <Text type="secondary">：{noSourceText(item.what, item.why)}</Text>
          </Text>
        ))}
      </Space>
    </ProCard>
  );
}

/* ── 页面 ───────────────────────────────────────────────────────────────── */
export default function 风险监控Page() {
  const analytics = useV3("risk/analytics", { limit: 250 });
  const risk = useV3("risk", {});
  const ordersEnv = useV3("oms/orders", {});
  const execution = useV3("execution", {});
  const audit = useV3("audit", { window: 120 });
  const strategy = useV3("strategy", {});

  const orders = OK(ordersEnv.value) && Array.isArray(ordersEnv.value.orders) ? ordersEnv.value.orders : [];
  const nav = OK(analytics.value) && fin(analytics.value.nav)
    ? Number(analytics.value.nav)
    : (OK(ordersEnv.value) && fin(ordersEnv.value.nav) ? Number(ordersEnv.value.nav) : null);
  const positions = OK(execution.value) ? execution.value.positions : null;
  const universe = OK(strategy.value) ? asArray((strategy.value.run || {}).universe) : [];
  const eventsTicker = universe.length ? String(universe[0]) : "SH.600000";
  const events = useV3("events", { ticker: eventsTicker, window: 180 });
  const auditEntries = OK(audit.value) && audit.value.data ? asArray(audit.value.data.entries) : [];
  const eventRows = OK(events.value) && events.value.data ? asArray(events.value.data.events) : [];
  const mode = (positions && positions.mode) || "sim";

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message={`风险监控 · ${String(mode).toUpperCase()} 环境 · 数据全部来自本服务 /api/v3/* 实时接口`}
        description={
          <Text type="secondary" style={{ fontSize: 12 }}>
            组合口径「{OK(analytics.value) ? analytics.value.portfolioSource || "—" : "—"}」· 基准 {OK(analytics.value) ? analytics.value.benchmarkTicker || "—" : "—"} ·
            台账权益 {nav === null ? "—" : fmt.money(nav)}（本地模拟台账，不代表券商资产）。本页没有任何写动作：阈值修改只经工作台受约束入口并留审计痕迹。
          </Text>
        }
      />
      <Block title="指标卡">
        <MetricCards env={analytics.value} />
      </Block>
      <Row gutter={[12, 12]}>
        <Col xs={24} xl={8}>
          <Block title="事前风控">
            <PreTradeCard riskEnv={risk.value} orders={orders} nav={nav} analytics={OK(analytics.value) ? analytics.value.analytics : null} positions={positions} />
          </Block>
        </Col>
        <Col xs={24} xl={8}>
          <Block title="事中风控">
            <LiveCard analytics={OK(analytics.value) ? analytics.value.analytics : null} audit={auditEntries} events={eventRows} />
          </Block>
        </Col>
        <Col xs={24} xl={8}>
          <Block title="事后风控">
            <PostTradeCard env={analytics.value} />
          </Block>
        </Col>
      </Row>
      <Block title="暴露与集中度">
        <ExposureCard env={analytics.value} riskEnv={risk.value} ordersEnv={ordersEnv.value} />
      </Block>
      <Block title="净值曲线">
        <CurveCard env={analytics.value} />
      </Block>
      <Block title="风控规则">
        <RulesCard riskEnv={risk.value} orders={orders} nav={nav} env={analytics.value} />
      </Block>
      <Block title="阻断记录">
        <BlocksCard orders={orders} />
      </Block>
      <Block title="无数据源项">
        <NoSourceCard />
      </Block>
      <Text type="secondary" style={{ fontSize: 12 }}>
        数据来源：/api/v3/risk/analytics（VaR/CVaR/Beta/Alpha/IR + Kupiec + 净值曲线）· /api/v3/risk（事前阈值配置）· /api/v3/oms/orders（台账订单与风控分级）·
        /api/v3/execution（持仓口径）· /api/v3/audit（事中事件流）· /api/v3/events?ticker={eventsTicker}（公开披露事件）。页面不含占位数字。
        {" "}<Link href="/v3/risk.html">对照设计稿原样版</Link>
      </Text>
    </Space>
  );
}
