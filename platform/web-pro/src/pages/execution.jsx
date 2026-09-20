// V3 工作台「执行与审批」页（Ant Design Pro）。
//
// 模块与设计稿原样版 platform/web/public/v3/execution.js（= 功能规格书）**一一对应**：
//   ① 执行入口条：模式 / 冻结计划（真实 plan_id · 待执行数）/ 今日成交 / 待审批数 +
//      「平台不含下单入口，执行只经此受约束入口 + 人工确认，LIVE 需口令」
//   ② 订单生命周期看板（信号生成/风控校验/审批/已提交/部分成交/全部成交，OMS stages 真实计数）
//   ③ 分级审批三档：自动执行（risk_passed）/ 人工确认（manual，真实订单卡）/ 强制阻断（blocked）
//   ④ 订单明细表（OMS orders：订单号/标的/方向/数量/金额/阶段/风控结论）
//   ⑤ 持仓摘要（execution.positions，账户口径不跨账户/币种合并）+ 成交质量（无数据源）
//   ⑥ 决策链路追溯（订单 history + audit）
//   ⑦ 冻结计划执行（Modal）：plan → 首次点击只进入待确认态（展示将提交的载荷 + 重取 plan 校验 hash/mode）
//      第二次点击才提交；expected_mode 取当前模式；mode=live 必须逐字口令「确认执行」，口令不符在任何请求之前返回；
//      失败显示 error.code：message
//   ⑧ 待确认请求（confirmation）：pending（id/operation/tool/mode/到期/summary）→ 批准/拒绝，载荷严格
//      {id, decision}，同样二次点击；到期或未知 → 批准禁用（fail-closed）；决定后立即重读
//
// 硬边界：本页是唯一受约束执行入口。加载与刷新只读，绝不自动写；写动作只在人工点击后发起。
// 平台不含逐单下单/改单/撤单入口：台账里 stage=manual 只是平台侧台账的审批阶段，不是券商待确认。
import React from "react";
import {
  Alert, Badge, Button, Col, Descriptions, Empty, Form, Input, Modal, Progress, Row, Space,
  Table, Tag, Timeline, Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { useV3, useWbAction, postWb, getV3, fmt, noSourceText } from "../services/api.js";
import { MarketNote, envelopeError, marketLabel, useMarket } from "../services/marketContext.jsx";
import { BarList } from "../components/charts.jsx";

const { Text, Link } = Typography;

/* ── 常量（与 binder / 服务端契约一字不差） ─────────────────────────────── */
const PLAN_CONFIRM_WORD = "确认执行";   // LIVE 逐字口令（app.py:534-535）
const ARM_TTL_MS = 20000;               // 待确认态存活窗口：过期即撤销，避免「很久以前那一次点击」被兑现
const FALLBACK_TTL_SECONDS = 120;       // confirmation.ttl_ms 缺失时的展示兜底

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

/** 取数失败原因：HTTP 非 2xx（error）与 HTTP 200 的 ok:false 信封都取服务端 code/message 原文，
 *  并在有 error.detail 时追加「真实原因」（统一口径见 services/marketContext.jsx 的 envelopeError）。 */
function envError(env, fallback) {
  return envelopeError(env, fallback || "接口未返回 error.code/message");
}

const stageText = (stage) => ({
  manual: "待审批", blocked: "已阻断", risk_passed: "风控通过", submitted: "已提交",
  filled: "全部成交", rejected: "已拒绝", partial: "部分成交",
}[String(stage)] || String(stage || "未知"));
const stageColor = (stage) => (stage === "manual" ? "gold" : stage === "blocked" || stage === "rejected" ? "red"
  : stage === "filled" ? "green" : "blue");
const sideText = (side) => (String(side).toUpperCase() === "BUY" ? "买入" : "卖出");
const shortId = (id) => (String(id || "—").length > 14 ? `${String(id).slice(0, 8)}…${String(id).slice(-4)}` : String(id || "—"));

/** 兼容 {} / {groups:[{rows:[]}]} / {groups:[{positions:[]}]} / 数组 四种形态。 */
function flatRows(node, keys) {
  if (!node) return [];
  if (Array.isArray(node)) return node.filter((row) => row && typeof row === "object");
  const groups = Array.isArray(node.groups) ? node.groups : [];
  const out = [];
  for (const group of groups) {
    for (const key of keys) {
      if (Array.isArray(group[key])) {
        for (const row of group[key]) if (row && typeof row === "object") out.push({ ...row, __group: group });
      }
    }
  }
  return out;
}

/** 台账订单归属的计划（取订单数最多的冻结计划）。 */
function dominantPlan(orders) {
  const counts = new Map();
  for (const order of orders) {
    const id = String(order.plan_id || "");
    if (!id) continue;
    const row = counts.get(id) || { plan_id: id, status: order.plan_status, n: 0 };
    row.n += 1;
    counts.set(id, row);
  }
  return Array.from(counts.values()).sort((a, b) => b.n - a.n)[0] || null;
}

/** confirmation 端点口径 {pending:{…}|null, ttl_ms}；兼容直接给 {id,…} 的形态。 */
function pendingOf(value) {
  if (!value || typeof value !== "object") return null;
  if (value.pending !== undefined) return value.pending || null;
  return value.id ? value : null;
}
/** 剩余存活秒数：<=0 已到期；null 表示到期时间未知（同样按不可批准处理，fail-closed）。 */
function remainingSeconds(expiresAt) {
  const at = Date.parse(String(expiresAt == null ? "" : expiresAt));
  if (Number.isNaN(at)) return null;
  return Math.max(0, Math.ceil((at - Date.now()) / 1000));
}

/**
 * 两条写通道的**读**端点（POST /api/wb/plan、POST /api/wb/confirmation，空载荷读状态）
 * 只能 POST，因此不能走 useV3（GET）。语义与 useV3 一致：{ value, loading, error, refresh }，
 * 且 refresh 返回 Promise 便于 await；本 hook 绝不写（空载荷 = 读状态）。
 */
function useWbRead(tool) {
  const [state, setState] = React.useState({ loading: true });
  const seqRef = React.useRef(0);
  const run = React.useCallback(async () => {
    seqRef.current += 1;
    const seq = seqRef.current;
    setState((prev) => ({ ...prev, loading: true, error: undefined }));
    try {
      const value = await postWb(tool, {});
      if (seqRef.current === seq) setState({ loading: false, value });
      return value;
    } catch (error) {
      if (seqRef.current === seq) setState({ loading: false, error: String((error && error.message) || error) });
      return { ok: false, error: { code: "net", message: String((error && error.message) || error) } };
    }
  }, [tool]);
  React.useEffect(() => {
    run();
    return () => { seqRef.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tool]);
  return { ...state, refresh: run };
}


/* ── 与工作台对账：POST /api/v3/oms/sync（把工作台 frozen 计划订单登记/更新到台账并回读状态） ──
 *  这是本页第二个动作按钮，同样**只在人工点击后发起**；失败按 error.code：message 展示。 */
function useReconcile() {
  const [state, setState] = React.useState({ busy: false, result: null, error: null });
  const run = React.useCallback(async () => {
    setState({ busy: true, result: null, error: null });
    try {
      const response = await fetch("/api/v3/oms/sync", {
        method: "POST",
        headers: (() => {
          const headers = { "content-type": "application/json" };
          try {
            const token = localStorage.getItem("trading_token");
            if (token) headers.Authorization = `Bearer ${token}`;
          } catch { /* localStorage 不可用按未鉴权 */ }
          return headers;
        })(),
        body: "{}",
      });
      const body = await response.json().catch(() => null);
      if (!response.ok || (body && body.ok === false)) {
        setState({ busy: false, result: null, error: body?.error ?? { code: `http-${response.status}`, message: "对账失败" } });
        return;
      }
      setState({ busy: false, result: body, error: null });
    } catch (error) {
      setState({ busy: false, result: null, error: { code: "net", message: String(error.message || error) } });
    }
  }, []);
  return { ...state, run };
}

function ReconcileButton() {
  const { busy, result, error, run } = useReconcile();
  return (
    <Space direction="vertical" size={4} style={{ width: "100%" }}>
      <Space size={8} wrap>
        <Button size="small" loading={busy} onClick={run}
          title="POST /api/v3/oms/sync：把工作台 frozen 计划订单登记/更新到 OMS 台账并回读确认状态">
          与工作台对账
        </Button>
        <Text type="secondary" style={{ fontSize: 11 }}>
          对账只读工作台的 frozen 计划并更新**平台台账**；不下单、不改变券商侧状态
        </Text>
      </Space>
      {error ? <Text type="danger" style={{ fontSize: 11 }}>{`${error.code ?? "错误"}：${error.message ?? ""}`}</Text> : null}
      {result && result.nav !== undefined ? (
        <Text type="secondary" style={{ fontSize: 11 }}>
          {`对账完成 · NAV ${result.nav ?? "—"} · 台账 ${(result.orders ?? []).length} 单 · 阶段 ${Object.entries(result.stages ?? {}).map(([k, v]) => `${k}=${v}`).join(" ")}`}
        </Text>
      ) : null}
    </Space>
  );
}

/* ── ① 执行入口条 ───────────────────────────────────────────────────────── */
function ExecHeader({ mode, planEnv, planErr, orders, deals, omsNote, nav, onOpenPlan, confirmPending }) {
  const { market } = useMarket();
  const planValue = OK(planEnv) ? (planEnv.value || {}) : null;
  const plan = planValue ? asArray(planValue.plans)[0] || null : null;
  const scoped = plan ? asArray(plan.orders) : [];
  const frozen = orders.filter((order) => order.plan_status === "frozen");
  const manual = orders.filter((order) => order.stage === "manual");
  const planReadout = plan
    ? { plan_id: plan.plan_id, n: scoped.filter((order) => String(order.status) !== "filled").length, status: plan.status }
    : dominantPlan(orders);
  return (
    <ProCard
      title={`执行入口条 · ${String(mode || "sim").toUpperCase()} 模拟环境`}
      bordered
      extra={
        <Space size={8} wrap>
          <Tag color="blue">唯一受约束执行入口</Tag>
          <Tag color={String(mode).toLowerCase() === "live" ? "red" : "gold"}>
            LIVE 需逐字口令「{PLAN_CONFIRM_WORD}」
          </Tag>
          <Button size="small" type="primary" onClick={onOpenPlan}
            title="打开冻结计划执行弹窗：plan-execute 只在人工二次确认后写入指令文件；LIVE 需逐字口令「确认执行」">
            打开冻结计划执行
          </Button>
          <ReconcileButton />
        </Space>
      }
    >
      <Alert
        type="warning"
        showIcon
        message="平台不含下单入口：执行只经「执行已冻结计划」（plan-execute）+ 人工确认；LIVE 需逐字口令「确认执行」"
        description={
          <Text type="secondary" style={{ fontSize: 12 }}>
            本页只有两条写通道，且都只在人工点击后发生：① 冻结计划执行（<Text code>POST /api/wb/plan-execute</Text>，人工二次确认）与
            ② 人工审批决定（<Text code>POST /api/wb/confirm-decide</Text>，载荷严格 {"{id, decision}"}）。
            台账里 stage=manual 是**平台侧台账**的审批阶段（超单笔上限退回人工），**不是**券商待确认；平台不提供逐单下单/改单/撤单入口。
            {omsNote ? ` OMS 说明：${omsNote}` : ""}
          </Text>
        }
      />
      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col xs={12} md={6}>
          <Text type="secondary" style={{ fontSize: 12 }}>冻结计划</Text>
          <div style={{ fontSize: 15, fontFamily: "ui-monospace, Menlo, monospace" }}>
            {planReadout ? String(planReadout.plan_id || "—") : "无数据源"}
          </div>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {planReadout
              ? `该计划待执行 ${planReadout.n} 条 · 状态 ${planReadout.status || "—"}${plan ? ` · content_hash ${plan.content_hash || "—"}` : "（来自 OMS 台账）"}`
              : (planErr || "POST /api/wb/plan 未返回冻结计划")}
          </Text>
        </Col>
        <Col xs={12} md={6}>
          <Text type="secondary" style={{ fontSize: 12 }}>今日成交</Text>
          <div style={{ fontSize: 15 }}>{fmt.num(deals.length, 0)} 行</div>
          <Text type="secondary" style={{ fontSize: 11 }}>来源 /api/v3/execution.deals_today（由模拟订单派生）</Text>
        </Col>
        <Col xs={12} md={6}>
          <Text type="secondary" style={{ fontSize: 12 }}>台账待审批（stage=manual）</Text>
          <div style={{ fontSize: 15, color: manual.length ? "#d9a112" : undefined }}>{manual.length} 单</div>
          <Text type="secondary" style={{ fontSize: 11 }}>平台侧台账审批阶段，非券商待确认；券商待确认见「待确认请求」</Text>
        </Col>
        <Col xs={12} md={6}>
          <Text type="secondary" style={{ fontSize: 12 }}>券商待确认请求</Text>
          <div style={{ fontSize: 15, color: confirmPending ? "#f8514d" : undefined }}>
            {confirmPending ? "有 1 笔待答复" : "无"}
          </div>
          <Text type="secondary" style={{ fontSize: 11 }}>来源 /api/wb/confirmation（唯一能批准实盘操作的通道）</Text>
        </Col>
      </Row>
      <Text type="secondary" style={{ fontSize: 12 }}>
        台账 {orders.length} 单 · 冻结计划归属 {frozen.length} 单 · 台账 NAV {fmt.money(nav)}
      </Text>
      <MarketNote
        source={`GET /api/v3/execution?market=${market} + GET /api/v3/oms/orders?market=${market}`}
        extra="台账 / 持仓 / 在途 / 成交均按当前市场过滤，跨市场不合并"
        style={{ display: "block", marginTop: 6 }}
      />
    </ProCard>
  );
}

/* ── ② 订单生命周期看板 ─────────────────────────────────────────────────── */
const LIFECYCLE = ["信号生成", "风控校验", "审批", "已提交", "部分成交", "全部成交"];
function LifecycleBoard({ execEnv, orders, audit, stages }) {
  const { market } = useMarket();
  const exec = OK(execEnv) ? execEnv : null;
  const signals = audit.filter((entry) => String(entry.kind) === "signal");
  const openRows = flatRows(exec && exec.orders_open, ["rows", "orders"]);
  const dealRows = flatRows(exec && exec.deals_today, ["rows", "deals"]);
  const partial = openRows.filter((row) => Number(row.dealt_qty || row.cum_qty || 0) > 0);
  const byStage = (stage) => orders.filter((order) => String(order.stage) === stage);
  const specs = {
    信号生成: signals.length
      ? { count: signals.length, cards: signals.slice(0, 2).map((signal) => ({ title: signal.ticker || "—", extra: signal.detail || "—", time: fmt.stamp(signal.at) })) }
      : { count: 0, cards: [], why: "审计链窗口内无量化信号" },
    风控校验: byStage("risk_passed").length
      ? { count: byStage("risk_passed").length, cards: byStage("risk_passed").slice(0, 2).map((order) => ({ title: order.ticker, extra: `${sideText(order.side)} · ${fmt.num(order.qty, 0)} 股`, time: fmt.stamp(order.updated_at) })) }
      : { count: 0, cards: [], why: "台账无 risk_passed 阶段订单（超单笔上限的订单全部退回 manual）" },
    审批: byStage("manual").length
      ? { count: byStage("manual").length, cards: byStage("manual").slice(0, 2).map((order) => ({ title: order.ticker, extra: `${sideText(order.side)} · ${fmt.money(order.value)}`, time: fmt.stamp(order.updated_at) })), color: "gold" }
      : { count: 0, cards: [], why: "台账无 manual 阶段订单" },
    已提交: (byStage("submitted").length + openRows.length)
      ? { count: byStage("submitted").length + openRows.length, cards: byStage("submitted").slice(0, 2).map((order) => ({ title: order.ticker, extra: `${sideText(order.side)} · ${fmt.num(order.qty, 0)} 股`, time: fmt.stamp(order.updated_at) })), color: "blue" }
      : { count: 0, cards: [], why: "台账无 submitted 阶段订单；券商在途订单（orders_open）当日 0 行" },
    部分成交: partial.length
      ? { count: partial.length, cards: partial.slice(0, 2).map((row) => ({ title: row.symbol || row.ticker || "—", extra: `${fmt.num(row.dealt_qty || row.cum_qty, 0)}/${fmt.num(row.qty, 0)} 股`, time: fmt.stamp(row.updated_time || row.create_time) })), color: "cyan" }
      : { count: 0, cards: [], why: "券商在途订单 0 行，台账无部分成交订单" },
    全部成交: (byStage("filled").length + dealRows.length)
      ? { count: byStage("filled").length + dealRows.length, cards: byStage("filled").slice(0, 2).map((order) => ({ title: order.ticker, extra: `${sideText(order.side)} · ${fmt.money(order.value)}`, time: fmt.stamp(order.updated_at) })), color: "green" }
      : { count: 0, cards: [], why: "台账无 filled 阶段订单；当日成交（deals_today，由模拟订单派生）0 行" },
  };
  const declared = stages && typeof stages === "object" ? Object.entries(stages) : [];
  return (
    <ProCard
      title="订单生命周期"
      bordered
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          状态计数取自 OMS 台账 stage 分布 + 审计链信号 + 券商在途/当日成交（/api/v3/execution）
          {declared.length ? ` · 台账 stages：${declared.map(([key, value]) => `${key}=${value}`).join(" / ")}` : ""}
        </Text>
      }
    >
      <Row gutter={[10, 10]}>
        {LIFECYCLE.map((name) => {
          const spec = specs[name];
          return (
            <Col key={name} xs={24} sm={12} xl={4}>
              <ProCard size="small" bordered title={<Space size={6}><Text style={{ fontSize: 12 }}>{name}</Text><Badge count={spec.count} showZero color={spec.count > 0 ? (spec.color === "gold" ? "#d9a112" : spec.color === "green" ? "#3fb950" : "#4c8dff") : "#626d7c"} /></Space>}
                style={{ height: "100%" }}>
                <Progress percent={Math.min(100, spec.count * 20)} showInfo={false} size="small"
                  strokeColor={spec.color === "gold" ? "#d9a112" : spec.color === "green" ? "#3fb950" : spec.color === "cyan" ? "#39a0ed" : "#4c8dff"} />
                {spec.cards.length === 0 ? (
                  <Text type="secondary" style={{ fontSize: 11 }}>{noSourceText(name, spec.why)}</Text>
                ) : (
                  <Space direction="vertical" size={4} style={{ width: "100%" }}>
                    {spec.cards.map((card, cardIndex) => (
                      <div key={`${name}-${cardIndex}`} style={{ border: "1px solid #232b37", borderRadius: 4, padding: "4px 6px" }}>
                        <Text style={{ fontSize: 12 }} strong>{String(card.title)}</Text>
                        <div><Text type="secondary" style={{ fontSize: 11 }}>{String(card.extra)}</Text></div>
                        <div><Text type="secondary" style={{ fontSize: 10 }}>{card.time}</Text></div>
                      </div>
                    ))}
                  </Space>
                )}
              </ProCard>
            </Col>
          );
        })}
      </Row>
      <MarketNote
        source={`GET /api/v3/oms/orders?market=${market} + GET /api/v3/execution?market=${market} + /api/v3/audit（全局窗口）`}
        extra={`该市场台账 ${orders.length} 单`}
        style={{ display: "block", marginTop: 8 }}
      />
    </ProCard>
  );
}

/* ── ③ 分级审批三档 ─────────────────────────────────────────────────────── */
function ApprovalGrades({ orders, riskCfg, nav, onOpenPlan, industryEnv }) {
  const { market } = useMarket();
  const auto = orders.filter((order) => String(order.stage) === "risk_passed");
  const manual = orders.filter((order) => String(order.stage) === "manual");
  const blocked = orders.filter((order) => ["blocked", "rejected"].includes(String(order.stage)));
  const limit = riskCfg && fin(riskCfg.risk_per_trade) ? Number(riskCfg.risk_per_trade) * 100 : null;
  const first = manual[0] || null;
  const orderCard = (order) => (
    <div key={order.id || order.ticker} style={{ border: "1px solid #232b37", borderRadius: 6, padding: "8px 10px", marginTop: 6 }}>
      <Space size={6} wrap>
        <Text strong>{order.ticker}</Text>
        <Text style={{ fontSize: 12 }}>{sideText(order.side)} · {fmt.num(order.qty, 0)} 股 · {fmt.money(order.value)}</Text>
        <Tag color={stageColor(order.stage)}>{stageText(order.stage)}</Tag>
      </Space>
      <div><Text type="secondary" style={{ fontSize: 11 }}>订单号 {order.id || "—"}</Text></div>
      <div><Text type="secondary" style={{ fontSize: 11 }}>风控结论 {asArray(order.risk && order.risk.reasons).join("；") || "未给出原因"}</Text></div>
      <div><Text type="secondary" style={{ fontSize: 11 }}>计划 {order.plan_id || "—"} · NAV {fmt.money(order.nav_used)}（{order.nav_source || "—"}）· {fmt.stamp(order.updated_at || order.first_seen_at)}</Text></div>
    </div>
  );
  return (
    <ProCard
      title="分级审批"
      bordered
      extra={<Text type="secondary" style={{ fontSize: 12 }}>台账口径 /api/v3/oms/orders · 自动放行仅发生在单笔占比 ≤ 2% 且未触红线时</Text>}
    >
      <Row gutter={[12, 12]}>
        <Col xs={24} lg={8}>
          <ProCard size="small" bordered title={<Space size={6}><Text style={{ fontSize: 12 }}>自动执行</Text><Tag color={auto.length ? "green" : "blue"}>台账 {auto.length} 单</Tag></Space>}
            subTitle={<Text type="secondary" style={{ fontSize: 11 }}>规则依据 · 单笔 ≤ 权益 2%（OMS check_order）· 风险预算 {fmt.pct(limit, 1)}</Text>}>
            {auto.length === 0
              ? <Text type="secondary" style={{ fontSize: 11 }}>{noSourceText("自动执行订单", "台账 stage 分布无 risk_passed/auto（当前订单全部超单笔上限，退回人工确认）")}</Text>
              : <Space direction="vertical" size={4} style={{ width: "100%" }}>{auto.slice(0, 3).map(orderCard)}</Space>}
          </ProCard>
        </Col>
        <Col xs={24} lg={8}>
          <ProCard size="small" bordered title={<Space size={6}><Text style={{ fontSize: 12 }}>人工确认</Text><Tag color={manual.length ? "gold" : "blue"}>需人工确认 · 台账 {manual.length} 单</Tag></Space>}>
            {!first ? (
              <Text type="secondary" style={{ fontSize: 11 }}>{noSourceText("待人工确认订单", "OMS 台账无 manual 阶段订单")}</Text>
            ) : (
              <Space direction="vertical" size={6} style={{ width: "100%" }}>
                <Text strong style={{ fontSize: 12 }}>{first.ticker} {sideText(first.side)} · {fmt.money(first.value)}</Text>
                <Descriptions size="small" column={2} bordered
                  items={[
                    { key: "qty", label: "数量", children: `${fmt.num(first.qty, 0)} 股` },
                    { key: "value", label: "预估金额", children: fmt.money(first.value) },
                    { key: "reason", label: "触发依据", children: asArray(first.risk && first.risk.reasons).join("；") || "未给出原因" },
                    { key: "plan", label: "决策快照", children: `计划 ${first.plan_id || "—"} · ${fmt.stamp(first.updated_at)}${fin(nav) ? ` · NAV ${fmt.money(nav)}` : ""}` },
                    { key: "id", label: "台账订单号", children: first.id || "—" },
                    { key: "stage", label: "台账阶段", children: <Tag color="gold">{stageText(first.stage)}</Tag> },
                  ]}
                />
                <Alert type="info" showIcon message="审批与执行入口就在本页"
                  description={
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      ①「打开冻结计划执行」弹窗（<Text code>plan-execute</Text>，人工二次确认；LIVE 需逐字口令「{PLAN_CONFIRM_WORD}」）；
                      ②下方「待确认请求」区块（<Text code>confirm-decide</Text>，唯一能批准实盘操作的通道）。
                      台账 stage=manual 只是平台侧台账的审批阶段，不是券商待确认。
                    </Text>
                  } />
                <Button size="small" onClick={onOpenPlan}>打开冻结计划执行</Button>
                {manual.length > 1 ? (
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    队列中还有 {manual.length - 1} 条：{manual[1].ticker} {sideText(manual[1].side)} {fmt.num(manual[1].qty, 0)} 股 · 金额 {fmt.money(manual[1].value)} · 待审批
                  </Text>
                ) : <Text type="secondary" style={{ fontSize: 11 }}>队列中无其它待审批订单</Text>}
              </Space>
            )}
          </ProCard>
        </Col>
        <Col xs={24} lg={8}>
          <ProCard size="small" bordered title={<Space size={6}><Text style={{ fontSize: 12 }}>强制阻断</Text><Tag color={blocked.length ? "red" : "blue"}>台账 {blocked.length} 单</Tag></Space>}
            subTitle={<Text type="secondary" style={{ fontSize: 11 }}>强制阻断无操作入口 · 全程留痕可追溯（/api/v3/oms/orders）</Text>}>
            <Tag color={industryEnv && OK(industryEnv) ? (industryEnv.breach ? "red" : "blue") : undefined}>
              {industryEnv && OK(industryEnv)
                ? `行业暴露 ${fmt.pct(industryEnv.top && industryEnv.top.weightPct, 2)} · 上限 ${fmt.pct(industryEnv.limitPct, 0)} · ${industryEnv.breach ? "超限（人工核对）" : "未超限"}（/api/v3/risk/industry）`
                : noSourceText("行业分类", envError(industryEnv, "GET /api/v3/risk/industry 取不到"))}
            </Tag>
            <div style={{ marginTop: 6 }}>
              {blocked.length > 0
                ? <Space direction="vertical" size={4} style={{ width: "100%" }}>{blocked.slice(0, 3).map(orderCard)}</Space>
                : (
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {noSourceText("强制阻断订单", "OMS 台账 0 条 blocked/rejected 订单；硬阻断只能来自单笔占比与回撤红线，行业上限只做展示与人工核对，未接入自动阻断")}
                  </Text>
                )}
            </div>
          </ProCard>
        </Col>
      </Row>
      <MarketNote
        source={`GET /api/v3/oms/orders?market=${market} + GET /api/v3/risk/industry?market=${market}`}
        extra={`自动执行 ${auto.length} 单 / 人工确认 ${manual.length} 单 / 强制阻断 ${blocked.length} 单（仅当前市场）`}
        style={{ display: "block", marginTop: 8 }}
      />
    </ProCard>
  );
}

/* ── ⑤ 持仓摘要 + 成交质量 ─────────────────────────────────────────────── */
function HoldingsCard({ execEnv }) {
  const { market } = useMarket();
  const exec = OK(execEnv) ? execEnv : null;
  const positions = exec ? exec.positions : null;
  const groups = positions && Array.isArray(positions.groups) ? positions.groups : [];
  const rows = [];
  const accounts = [];
  for (const group of groups) {
    const list = asArray(group.positions);
    if (list.length === 0) continue;
    accounts.push(group);
    for (const row of list) rows.push({ ...row, __group: group });
  }
  const columns = [
    { title: "标的", dataIndex: "symbol", width: 180, render: (value, record) => <Text style={{ fontSize: 12 }}>{`${value || "—"} ${record.name || ""}`.trim()}</Text> },
    { title: "数量", dataIndex: "qty", width: 90, align: "right", render: (value) => fmt.num(value, 0) },
    { title: "成本", dataIndex: "cost_price", width: 90, align: "right", render: (value) => fmt.num(value, 3) },
    { title: "现价", dataIndex: "price", width: 90, align: "right", render: (value) => fmt.num(value, 3) },
    {
      title: "浮动盈亏", dataIndex: "pl_val", width: 130, align: "right",
      render: (value) => (fin(value) ? <Text style={{ color: Number(value) >= 0 ? "#3fb950" : "#f8514d" }}>{fmt.signed(value, 2)}</Text> : "—"),
    },
    {
      title: "权重", dataIndex: "weight", width: 90, align: "right",
      render: (value) => (fin(value) ? `${Number(value).toFixed(2)}%` : "—"),
    },
  ];
  const data = rows.map((row) => {
    const groupValue = Number(row.__group.market_value);
    return {
      key: `${row.__group.acc_id || row.__group.account}-${row.symbol}`,
      symbol: row.symbol,
      name: row.name,
      qty: row.qty,
      cost_price: row.cost_price,
      price: row.price,
      pl_val: row.pl_val,
      weight: groupValue ? (Number(row.market_value) / groupValue) * 100 : null,
      account: row.__group.account || row.__group.acc_id,
    };
  });
  return (
    <ProCard
      title="持仓摘要"
      bordered
      extra={
        <Text type="secondary" style={{ fontSize: 12 }}>
          券商模拟持仓快照 · as_of {fmt.stamp(positions && positions.as_of)}{positions && positions.stale ? "（缓存）" : ""} · {accounts.length} 个账户 / {rows.length} 只标的
        </Text>
      }
      style={{ height: "100%" }}
    >
      {!positions ? (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what={`${marketLabel(market)} 持仓摘要`} why={envError(execEnv, `GET /api/v3/execution?market=${market} 未返回 positions`)} />
          <MarketNote source={`GET /api/v3/execution?market=${market}`} extra="持仓按市场过滤；账户之间不跨币种合并" />
        </Space>
      ) : (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Table size="small" rowKey="key" pagination={false} columns={columns} dataSource={data} scroll={{ x: 800 }} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            账户分开列示（币种不同，不跨账户/币种合并）：{accounts.length
              ? accounts.map((group) => `${group.account || group.acc_id} 权益 ${fmt.money(group.total_asset)}（现金 ${fin(group.total_asset) && Number(group.total_asset) ? fmt.pct((Number(group.cash) / Number(group.total_asset)) * 100, 1) : "—"}）`).join(" · ")
              : "无数据源：券商持仓接口未返回账户"}
          </Text>
          <MarketNote source={`GET /api/v3/execution?market=${market}`} asOf={positions.as_of} extra={`${accounts.length} 个账户 / ${rows.length} 只标的（当前市场）`} />
        </Space>
      )}
    </ProCard>
  );
}

/* ── 成交质量：GET /api/v3/execution/quality（真实券商委托/成交回报） ──────────
 *  口径：委托/成交笔数与名义金额来自券商历史委托与成交流水（sources.orders / sources.deals），
 *  滑点为**近似口径**，服务端在 missing 里给出原文说明，本卡原样展示，不换算、不估算。
 *  失败或空态一律写「无数据源 · 原因」，缺项显示「—」，不填占位数字。
 *  市场**不再由本卡自己持有**：统一走 services/marketContext.jsx 的 useMarket()，
 *  与页头 MarketPicker、本页其它模块同一个事实源（避免同页两个市场口径）。
 */

/** 按市场取成交质量（只用 GET；市场切换或人工刷新才发请求）。 */
function useQuality(market) {
  const [state, setState] = React.useState({ loading: true, value: undefined, error: undefined });
  const seqRef = React.useRef(0);
  const run = React.useCallback(async (refresh = false) => {
    seqRef.current += 1;
    const seq = seqRef.current;
    setState((prev) => ({ ...prev, loading: true, error: undefined }));
    try {
      const value = await getV3("execution/quality", { market, mode: "sim" }, { refresh });
      if (seqRef.current !== seq) return;
      // 失败信封（HTTP 200 + ok:false）不是「空数据」：把服务端原因留在 error，交给卡片如实展示。
      if (value && value.ok === false) {
        setState({ loading: false, value: undefined, error: envError(value, "GET /api/v3/execution/quality 返回 ok:false") });
        return;
      }
      setState({ loading: false, value, error: undefined });
    } catch (error) {
      if (seqRef.current === seq) setState({ loading: false, value: undefined, error: String((error && error.message) || error) });
    }
  }, [market]);
  React.useEffect(() => {
    run(false);
    return () => { seqRef.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [market]);
  return { ...state, refresh: () => run(true) };
}

const NUM_FONT = { fontFamily: "ui-monospace, Menlo, monospace", fontVariantNumeric: "tabular-nums" };

function qualityMetricCards(metrics, points, asOf, definitions) {
  const define = (key, fallback) => (definitions && definitions[key]) || fallback;
  const slip = metrics && fin(metrics.avgSlippageBps) ? Number(metrics.avgSlippageBps) : null;
  const tiles = [
    { key: "filled", label: "已成交", value: metrics && fin(metrics.filled) ? `${fmt.num(metrics.filled, 0)} 笔` : "—", hint: "券商成交流水（deals）" },
    { key: "partial", label: "部分成交", value: metrics && fin(metrics.partial) ? `${fmt.num(metrics.partial, 0)} 笔` : "—", hint: "券商委托状态（orders）" },
    { key: "cancelled", label: "已撤单", value: metrics && fin(metrics.cancelled) ? `${fmt.num(metrics.cancelled, 0)} 笔` : "—", hint: "券商委托状态（orders）" },
    { key: "fillRate", label: "成交率", value: metrics && fin(metrics.fillRatePct) ? fmt.pct(metrics.fillRatePct, 2) : "—", hint: define("fillRatePct", "服务端口径：已成交（含部分）/ 总委托") },
    { key: "cancelRate", label: "撤单率", value: metrics && fin(metrics.cancelRatePct) ? fmt.pct(metrics.cancelRatePct, 2) : "—", hint: define("cancelRatePct", "服务端口径：已撤 / 总委托") },
    {
      key: "slip",
      label: "平均滑点(bps)",
      value: slip === null ? "—" : (slip === 0 ? fmt.num(0, 2) + " bps" : fmt.signed(slip, 2, " bps")),
      hint: define("avgSlippageBps", "正 = 不利（服务端口径），见下方 missing 原文"),
      color: slip === null || slip === 0 ? undefined : slip > 0 ? "#f8514d" : "#3fb950",
    },
    { key: "notional", label: "成交名义金额", value: metrics && fin(metrics.notional) ? fmt.money(metrics.notional) : "—", hint: "券商成交名义金额合计" },
  ];
  return (
    <Row gutter={[8, 8]}>
      <Col xs={24} md={12} xl={8}>
        <div style={{ border: "1px solid #232b37", borderRadius: 6, padding: "6px 8px", height: "100%" }}>
          <Text type="secondary" style={{ fontSize: 12 }}>委托 / 已成交 / 部分 / 撤单</Text>
          <div style={{ fontSize: 15, ...NUM_FONT }}>
            {metrics
              ? `${fmt.num(metrics.orders, 0)} / ${fmt.num(metrics.filled, 0)} / ${fmt.num(metrics.partial, 0)} / ${fmt.num(metrics.cancelled, 0)}`
              : "— / — / — / —"}
          </div>
          <Text type="secondary" style={{ fontSize: 11 }}>{`as_of ${fmt.stamp(asOf)} · 共 ${asArray(points).length} 个滑点取样点`}</Text>
        </div>
      </Col>
      {tiles.map((tile) => (
        <Col xs={12} md={6} xl={4} key={tile.key}>
          <div style={{ border: "1px solid #232b37", borderRadius: 6, padding: "6px 8px", height: "100%" }}>
            <Text type="secondary" style={{ fontSize: 12 }}>{tile.label}</Text>
            <div style={{ fontSize: 15, color: tile.color, ...NUM_FONT }}>{tile.value}</div>
            <Text type="secondary" style={{ fontSize: 11 }}>{tile.hint}</Text>
          </div>
        </Col>
      ))}
    </Row>
  );
}

function QualityCard({ market, qualityEnv }) {
  const env = qualityEnv.value;
  const data = OK(env) ? env : null;
  const metrics = data && data.metrics && typeof data.metrics === "object" ? data.metrics : null;
  const points = data ? asArray(data.points).filter((point) => point && typeof point === "object") : [];
  const sources = data && data.sources && typeof data.sources === "object" ? data.sources : null;
  const missing = data ? asArray(data.missing).map((line) => String(line)).filter(Boolean) : [];
  const reasonText = missing.join("；") || "服务端未返回 missing 口径说明";

  const bars = points
    .map((point) => {
      const value = Number(point.slippageBps);
      if (!Number.isFinite(value)) return null;
      const stamp = String(point.t || "");
      const label = `${stamp.length >= 10 ? stamp.slice(5, 10) : (stamp || "—")} 滑点 ${value.toFixed(2)}bp`;
      // BarList 的宽度是 `value/max*100%`，负值会写成无效 CSS 宽度（条形不可见）：
      // 因此宽度传绝对值，符号保留在标签文字与整体配色（负＝有利，绿）里。
      return { label, value: Math.abs(value), signed: value, notional: Number(point.notional) };
    })
    .filter(Boolean)
    .slice(-12);
  const maxBar = bars.reduce((max, bar) => Math.max(max, Math.abs(bar.value)), 0);

  const reason = qualityEnv.error
    ? `请求失败 · ${qualityEnv.error}`
    : (data ? reasonText : `GET /api/v3/execution/quality 未返回数据（market=${market}）`);

  return (
    <ProCard
      title="成交质量"
      bordered
      style={{ height: "100%" }}
      extra={
        <Space size={8} wrap>
          <Tag color="blue">{`当前市场：${marketLabel(market)}`}</Tag>
          <Text type="secondary" style={{ fontSize: 11 }}>市场由页头统一选择器决定</Text>
          <Button size="small" loading={qualityEnv.loading} onClick={qualityEnv.refresh}
            title="重新读取 GET /api/v3/execution/quality（只读 GET，不写任何状态）">
            刷新
          </Button>
        </Space>
      }
    >
      {qualityEnv.loading && !env ? (
        <Empty imageStyle={{ display: "none" }} style={{ margin: 0 }} description={<Text type="secondary">正在读取成交质量…</Text>} />
      ) : !data || !metrics ? (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what={`成交质量 · ${marketLabel(market)}`} why={reason} />
          {!data && !qualityEnv.error ? (
            <Text type="secondary" style={{ fontSize: 11 }}>
              成交类数据没有开源替代：当日无委托或券商未返回委托/成交流水时，本卡按原因如实展示，不用估算值顶替。
            </Text>
          ) : null}
          <MarketNote source={`GET /api/v3/execution/quality?market=${market}&mode=sim`} extra="该市场无成交/委托回报时按原因展示，不跨市场合并" />
        </Space>
      ) : (
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          {qualityMetricCards(metrics, points, data.as_of, data.definitions)}
          <div>
            <Space size={8} wrap style={{ marginBottom: 6 }}>
              <Text strong style={{ fontSize: 12 }}>{`滑点走势（成交回报，最近 ${bars.length} 个取样点）`}</Text>
              <Text type="secondary" style={{ fontSize: 11 }}>横条＝滑点 bps；正值（红）＝成交劣于基准价，负值（绿）＝成交优于基准价</Text>
            </Space>
            {bars.length === 0 ? (
              <NoSource what="滑点走势" why={`/api/v3/execution/quality.points 为空（market=${market}）`} />
            ) : (
              <BarList
                items={bars.map((bar) => [bar.label, bar.value])}
                max={maxBar || 1}
                color={Number(metrics.avgSlippageBps) < 0 ? "#3fb950" : "#f8514d"}
              />
            )}
          </div>
          <Descriptions size="small" column={{ xs: 1, sm: 2 }} bordered
            items={[
              { key: "orders", label: "委托来源", children: <Text code style={{ fontSize: 11 }}>{sources && sources.orders ? String(sources.orders) : "—"}</Text> },
              { key: "deals", label: "成交来源", children: <Text code style={{ fontSize: 11 }}>{sources && sources.deals ? String(sources.deals) : "—"}</Text> },
              { key: "market", label: "市场 / 模式", children: `${data.market || market} · ${String(data.mode || "sim").toUpperCase()}` },
              { key: "asof", label: "口径时点 as_of", children: fmt.stamp(data.as_of) },
            ]}
          />
          <Alert
            type={missing.length ? "warning" : "info"}
            showIcon
            message={`口径说明（服务端 missing 原文，${missing.length} 条）`}
            description={
              <Space direction="vertical" size={2} style={{ width: "100%" }}>
                {missing.length === 0 ? (
                  <Text type="secondary" style={{ fontSize: 12 }}>服务端未返回 missing 说明（口径以 sources 为准）</Text>
                ) : (
                  missing.map((line, index) => (
                    <Text key={`missing-${index}`} type="secondary" style={{ fontSize: 12 }}>{`· ${line}`}</Text>
                  ))
                )}
              </Space>
            }
          />
          <MarketNote
            source={`GET /api/v3/execution/quality?market=${market}&mode=${String(data.mode || "sim")}`}
            asOf={data.as_of}
            extra={`委托来源 ${sources && sources.orders ? String(sources.orders) : "—"} · 成交来源 ${sources && sources.deals ? String(sources.deals) : "—"}（跨市场不合并）`}
          />
        </Space>
      )}
    </ProCard>
  );
}

/* ── ⑥ 决策链路追溯 ─────────────────────────────────────────────────────── */
function DecisionTrace({ order, audit, riskCfg, nav, execEnv }) {
  const { market } = useMarket();
  const [selected, setSelected] = React.useState(null);
  const current = order || null;
  const columns = [
    { title: "订单号", dataIndex: "id", width: 150, render: (value) => <Text code style={{ fontSize: 11 }} title={`台账订单号（OMS 内部 id）：${value}`}>{shortId(value)}</Text> },
    { title: "标的", dataIndex: "ticker", width: 110, render: (value) => <Text style={{ fontSize: 12 }}>{String(value || "—")}</Text> },
    { title: "方向", dataIndex: "side", width: 70, render: (value) => <Text style={{ fontSize: 12, color: String(value).toUpperCase() === "BUY" ? "#3fb950" : "#f8514d" }}>{sideText(value)}</Text> },
    { title: "数量", dataIndex: "qty", width: 90, align: "right", render: (value) => fmt.num(value, 0) },
    { title: "金额", dataIndex: "value", width: 120, align: "right", render: (value) => fmt.money(value) },
    { title: "阶段", dataIndex: "stage", width: 100, render: (value) => <Tag color={stageColor(value)}>{stageText(value)}</Tag> },
    { title: "计划", dataIndex: "plan_id", width: 190, render: (value) => <Text type="secondary" style={{ fontSize: 11 }}>{String(value || "—")}</Text> },
  ];
  const history = asArray(current && current.history);
  const events = OK(execEnv) ? execEnv : null;
  const openRows = flatRows(events && events.orders_open, ["rows", "orders"]);
  const dealRows = flatRows(events && events.deals_today, ["rows", "deals"]);
  const signal = audit.filter((entry) => String(entry.kind) === "signal" && String(entry.ticker || "") === String((current && current.ticker) || ""))[0] || null;
  return (
    <ProCard
      title="决策链路追溯"
      bordered
      extra={<Text type="secondary" style={{ fontSize: 12 }}>点击订单行可切换追溯对象 · 未取到的字段显式标注「无数据源」</Text>}
    >
      <Table size="small" rowKey={(record) => record.id} pagination={false} columns={columns}
        dataSource={asArray(order ? [order] : [])} scroll={{ x: 800 }} locale={{ emptyText: <NoSource what="订单明细" why="OMS 台账无订单（/api/v3/oms/orders.orders 为空）" /> }} />
      <Descriptions style={{ marginTop: 12 }} size="small" column={{ xs: 1, sm: 2 }} bordered
        items={[
          { key: "target", label: "追溯对象", children: current ? `订单 ${current.id} · ${current.ticker} · ${sideText(current.side)} ${fmt.num(current.qty, 0)} 股 · 计划 ${current.plan_id || "—"}` : "无数据源：OMS 台账无订单" },
          { key: "factor", label: "因子快照", children: <Text type="secondary">{noSourceText("PIT 因子快照", "工作台订单未携带 PIT 因子快照（/api/v3/factors/matrix 可另行查看）")}</Text> },
          { key: "model", label: "策略与风控", children: current ? `策略 ${current.strategy_id || "—"} · 风控动作 ${(current.risk && current.risk.action) || "—"} · ${asArray(current.risk && current.risk.reasons).join("；") || "未给出原因"}` : "—" },
          { key: "sentiment", label: "情绪评分", children: <Text type="secondary">{noSourceText("情绪/情感评分", "工具面无情绪评分数据源")}</Text> },
          { key: "market", label: "盘中市场状态", children: <Text type="secondary">{noSourceText("盘中市场状态快照", "工具面无盘中市场状态数据源")}</Text> },
          { key: "param", label: "计划参数", children: current ? `计划 ${current.plan_id || "—"} · plan_status ${current.plan_status || "—"} · mode ${current.mode || "—"}` : "—" },
          { key: "risk", label: "风控阈值口径", children: current ? `NAV ${fmt.money(current.nav_used)}（${current.nav_source || "—"}）· 回撤 ${fmt.pct(current.drawdown_used)}（${current.drawdown_source || "—"}）· 行业 ${fmt.pct(current.industry_pct)}（工具面无行业分类，不参与阻断）· 单笔上限 ${riskCfg && fin(riskCfg.risk_per_trade) ? fmt.pct(Number(riskCfg.risk_per_trade) * 100, 1) : "—"}（风险预算）· NAV ${fin(nav) ? fmt.money(nav) : "—"}` : "—" },
          { key: "traceid", label: "逐单 trace id", children: <Text type="secondary">{noSourceText("逐单决策 trace id", "OMS 台账只有计划号，无逐单 trace id")}</Text> },
        ]}
      />
      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col xs={24} lg={12}>
          <Text strong style={{ fontSize: 12 }}>订单状态历史（OMS history）</Text>
          {history.length === 0 ? (
            <div style={{ marginTop: 8 }}><NoSource what="订单状态历史" why="台账未记录该订单的状态历史" /></div>
          ) : (
            <Timeline style={{ marginTop: 8 }}
              items={history.map((record, index) => ({
                key: `${record.at}-${index}`,
                color: record.stage === "manual" ? "orange" : ["blocked", "rejected"].includes(String(record.stage)) ? "red" : record.stage === "filled" ? "green" : "blue",
                children: (
                  <Space direction="vertical" size={0}>
                    <Text style={{ fontSize: 12 }}>{fmt.stamp(record.at)} · {stageText(record.stage)}</Text>
                    <Text type="secondary" style={{ fontSize: 11 }}>{asArray(record.reasons).join("；") || "无附加原因"}</Text>
                  </Space>
                ),
              }))}
            />
          )}
        </Col>
        <Col xs={24} lg={12}>
          <Text strong style={{ fontSize: 12 }}>审计链同期记录（/api/v3/audit）</Text>
          <Space direction="vertical" size={4} style={{ width: "100%", marginTop: 8 }}>
            {signal ? (
              <Text style={{ fontSize: 12 }}>
                同标的量化信号（{signal.source_label || "量化信号"}）：{signal.detail || "—"} · {fmt.stamp(signal.at)}
              </Text>
            ) : (
              <Text type="secondary" style={{ fontSize: 12 }}>{noSourceText("同标的审计链信号", "审计链窗口内无该标的的 signal 记录")}</Text>
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              券商在途订单 {openRows.length} 行 · 当日成交 {dealRows.length} 行（/api/v3/execution）
            </Text>
            <MarketNote
              source={`GET /api/v3/oms/orders?market=${market} + GET /api/v3/execution?market=${market}`}
              extra="追溯对象限定在当前市场台账内"
            />
          </Space>
        </Col>
      </Row>
    </ProCard>
  );
}

/* ── ⑦ 冻结计划执行（Modal） ────────────────────────────────────────────── */
function PlanModal({ open, onClose, planEnv, planErr, mode, wb, reloadGate }) {
  const plan = OK(planEnv) ? asArray(planEnv.value && planEnv.value.plans)[0] || null : null;
  const hash = plan && typeof plan.content_hash === "string" && plan.content_hash ? plan.content_hash : null;
  const [armed, setArmed] = React.useState(null);   // {action, plan_hash, expected_mode, live}
  const [password, setPassword] = React.useState("");
  const [msg, setMsg] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const timerRef = React.useRef(null);
  const live = String(mode || "sim").toLowerCase() === "live";

  const clearArm = React.useCallback(() => {
    setArmed(null);
    if (timerRef.current) clearTimeout(timerRef.current);
  }, []);

  React.useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);
  React.useEffect(() => { if (!open) { setMsg(null); setPassword(""); clearArm(); } }, [open, clearArm]);

  const setArm = (value) => {
    setArmed(value);
    if (timerRef.current) clearTimeout(timerRef.current);
    // 待确认态自动作废：二次点击窗口过期即撤销
    timerRef.current = setTimeout(() => setArmed(null), ARM_TTL_MS);
  };

  /** 口令校验先于一切请求：live + 口令不符 → 一个字节都不发。 */
  const passwordGateOk = () => {
    if (!live) return true;
    if (String(password || "") === PLAN_CONFIRM_WORD) return true;
    setMsg({ tone: "err", text: `实时账户执行需输入口令「${PLAN_CONFIRM_WORD}」；口令不合规，未发出任何请求` });
    return false;
  };

  const click = async (action) => {
    if (busy) { setMsg({ tone: "warn", text: "正在刷新或提交，请稍候再点击…" }); return; }
    const want = action === "cancel" ? "cancel" : "execute";
    // ① 口令门槛优先于一切：不合规 → 冷返回，读写都不发
    if (want === "execute" && !passwordGateOk()) return;
    if (!hash) { setMsg({ tone: "err", text: planErr || "无冻结计划：/api/wb/plan 未返回可执行计划（content_hash 缺失）" }); return; }
    if (want === "execute" && plan.status !== "frozen") {
      setMsg({ tone: "err", text: `当前计划状态为 ${plan.status || "未知"}（非冻结态），不可执行；请刷新后重试` });
      return;
    }
    // ② 首次点击：只进入待确认态，并立刻重取 plan / confirmation 校验 hash 与 mode 未变
    if (!armed || armed.action !== want) {
      setArm({ action: want, plan_hash: hash, expected_mode: mode, live });
      setMsg({
        tone: "warn",
        text: want === "execute"
          ? "待确认：请核对下方将提交的载荷，再次点击「执行已冻结计划」才写入指令（加载时不写、不自动执行）"
          : "待确认：请再次点击「取消计划」提交取消指令",
      });
      setBusy(true);
      try {
        await reloadGate();
      } finally {
        setBusy(false);
      }
      return;
    }
    // ③ 第二次点击 = 提交（载荷就是上一次点击展示出来的那一份）
    setBusy(true);
    try {
      const payload = armed.action === "cancel"
        ? { plan_hash: armed.plan_hash, expected_mode: armed.expected_mode, action: "cancel" }
        : { plan_hash: armed.plan_hash, expected_mode: armed.expected_mode };
      if (armed.action === "execute" && armed.live) payload.confirmation = PLAN_CONFIRM_WORD;
      const env = await wb.run("plan-execute", payload);
      if (OK(env)) {
        const value = env.value || {};
        clearArm();
        setPassword("");
        setMsg({ tone: "ok", text: `已提交（action=${value.action || armed.action}，nonce=${value.nonce || "—"}）：指令文件已写入，状态由 daemon 回写` });
      } else {
        setMsg({ tone: "err", text: envError(env, "plan-execute 失败") });
      }
    } finally {
      setBusy(false);
      await reloadGate();
    }
  };

  const confirmLines = armed
    ? [
      { k: "当前模式", v: String(mode || "sim").toUpperCase() },
      { k: "expected_mode", v: String(armed.expected_mode || "—") },
      { k: "plan_hash", v: armed.plan_hash },
      { k: "action", v: armed.action },
      { k: "confirmation", v: armed.action === "execute" ? (armed.live ? PLAN_CONFIRM_WORD : "无（非 live 模式）") : "—" },
    ]
    : [];

  return (
    <Modal
      open={open}
      onCancel={onClose}
      title="执行已冻结计划"
      width={760}
      footer={[
        <Button key="close" onClick={onClose} title="只关闭弹窗，不提交任何指令">关闭</Button>,
        <Button key="cancel" danger loading={busy} onClick={() => click("cancel")}
          title="提交 plan-execute（action=cancel）：只撤销未提交订单，需二次点击确认">
          {armed && armed.action === "cancel" ? "再次点击确认取消" : "取消计划"}
        </Button>,
        <Button key="execute" type="primary" loading={busy} onClick={() => click("execute")}
          title={`提交 plan-execute（action=execute）：唯一受约束下单入口，需二次点击确认${live ? `；LIVE 必须先输入逐字口令「${PLAN_CONFIRM_WORD}」，口令不合规不发请求` : "；SIM 免口令"}`}>
          {armed && armed.action === "execute" ? "再次点击确认执行" : `执行已冻结计划${live ? "（LIVE 需口令）" : ""}`}
        </Button>,
      ]}
    >
      <Space direction="vertical" size={8} style={{ width: "100%" }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {plan
            ? `冻结计划 ${plan.plan_id} · 共 ${asArray(plan.orders).length} 条订单（状态 ${plan.status || "—"}）· 内容哈希 ${plan.content_hash || "—"} · 模式 ${plan.mode || "—"}`
            : (planErr ? `冻结计划取数失败：${planErr}` : "冻结计划：无数据源（/api/wb/plan 未返回计划）")}
        </Text>
        {plan ? (
          <Table size="small" pagination={false} rowKey={(record) => record.client_order_id || `${record.symbol}-${record.side}`}
            dataSource={asArray(plan.orders).slice(0, 8)}
            columns={[
              { title: "标的", dataIndex: "symbol", width: 110 },
              { title: "方向", dataIndex: "side", width: 70, render: (value) => sideText(value) },
              { title: "数量", dataIndex: "qty", width: 90, align: "right", render: (value) => fmt.num(value, 0) },
              { title: "限价", dataIndex: "price", width: 90, align: "right", render: (value) => fmt.num(value, 2) },
              { title: "计划内状态", dataIndex: "status", width: 110, render: (value) => <Tag>{String(value || "—")}</Tag> },
              { title: "风控回执", dataIndex: "risk_verdict", render: (value) => <Text type="secondary" style={{ fontSize: 11 }}>{value === null || value === undefined ? "—" : String(value)}</Text> },
            ]}
          />
        ) : null}
        <Descriptions size="small" column={2} bordered
          items={[
            { key: "mode", label: "当前模式", children: live ? <Tag color="red">LIVE（实盘）</Tag> : <Tag color="gold">{String(mode || "sim").toUpperCase()}（模拟）</Tag> },
            { key: "expected", label: "expected_mode", children: String(mode || "sim") },
            { key: "hash", label: "plan_hash", children: hash || (planErr ? "取数失败" : "—") },
            { key: "status", label: "计划状态", children: plan ? String(plan.status || "—") : "—" },
          ]}
        />
        {live ? (
          <Form layout="vertical" size="small">
            <Form.Item label={`LIVE 口令（逐字输入「${PLAN_CONFIRM_WORD}」）`} style={{ marginBottom: 0 }}>
              <Input.Password value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="off"
                placeholder={`输入口令：${PLAN_CONFIRM_WORD}`} aria-label="确认执行口令" />
            </Form.Item>
            <Text type="secondary" style={{ fontSize: 11 }}>口令只存在于本输入框；不合规时不会发出任何请求。</Text>
          </Form>
        ) : null}
        <Alert type={armed ? "warning" : "info"} showIcon
          message={armed ? "已进入待确认态：再次点击才提交" : "首次点击只进入待确认态"}
          description={
            <Text type="secondary" style={{ fontSize: 12 }}>
              执行已冻结计划是<Text strong>唯一受约束下单入口</Text>：下方按钮需<Text strong>人工二次确认</Text>——首次点击只进入待确认态并展示将提交的载荷，
              第二次点击才写入指令文件；LIVE 还需逐字口令「{PLAN_CONFIRM_WORD}」。提交 plan_hash + expected_mode（模式已变化会被服务端拒绝，请刷新重试），
              取消用 action=cancel。本页不含逐单下单入口。
            </Text>
          } />
        {armed ? (
          <Descriptions size="small" column={1} bordered title="将提交的载荷（待确认）"
            items={confirmLines.map((line) => ({ key: line.k, label: line.k, children: <Text code>{String(line.v)}</Text> }))} />
        ) : null}
        {msg ? <Alert type={msg.tone === "ok" ? "success" : msg.tone === "err" ? "error" : "warning"} showIcon message={msg.text} /> : null}
      </Space>
    </Modal>
  );
}

/* ── ⑧ 待确认请求 ───────────────────────────────────────────────────────── */
function ConfirmChannel({ env, envErr, pending, ttlMs, wb, reload, onDecided }) {
  const [armed, setArmed] = React.useState(null);   // {id, decision}
  const [msg, setMsg] = React.useState(null);
  const [busy, setBusy] = React.useState(false);
  const [, forceTick] = React.useState(0);
  const timerRef = React.useRef(null);

  React.useEffect(() => {
    const tick = setInterval(() => forceTick((value) => value + 1), 1000);
    return () => clearInterval(tick);
  }, []);
  React.useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);

  const remain = pending && pending.id ? remainingSeconds(pending.expires_at) : null;
  const expired = remain !== null && remain <= 0;
  const unknown = remain === null;
  const disabled = !pending || !pending.id || expired || unknown || busy;

  const click = async (decision) => {
    if (busy) { setMsg({ tone: "warn", text: "正在提交或刷新，请稍候再点击…" }); return; }
    if (!pending || !pending.id) { setMsg({ tone: "err", text: "无待确认请求（可能已处理或已超时）" }); return; }
    const left = remainingSeconds(pending.expires_at);
    if (left !== null && left <= 0) {
      setMsg({ tone: "err", text: "该请求已超时（服务端按拒绝 fail-closed 处理），请刷新后查看最新状态" });
      return;
    }
    // 首次点击：只进入待确认态 + 重读 confirmation（不发写请求）
    if (!armed || armed.decision !== decision || armed.id !== pending.id) {
      setArmed({ id: pending.id, decision });
      setMsg({ tone: "warn", text: decision === "approved" ? "待确认：批准即授权该笔实盘操作；请再次点击「确认批准」提交 {id, decision}" : "待确认：请再次点击「确认拒绝」提交 {id, decision}" });
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => setArmed(null), ARM_TTL_MS);
      setBusy(true);
      try {
        await reload();
      } finally {
        setBusy(false);
      }
      return;
    }
    // 第二次点击 = 提交（载荷严格 {id, decision}）
    setBusy(true);
    try {
      const result = await wb.run("confirm-decide", { id: armed.id, decision: armed.decision });
      if (OK(result)) {
        const value = result.value || {};
        setMsg({ tone: "ok", text: `已${armed.decision === "approved" ? "批准" : "拒绝"}（id=${value.id || armed.id}，operation=${value.operation || "—"}）；服务端按先到者定论记账` });
      } else {
        setMsg({ tone: "err", text: envError(result, "confirm-decide 失败") });
      }
    } finally {
      setArmed(null);
      setBusy(false);
      await reload();            // 决定后立刻重读 confirmation
      if (onDecided) onDecided();
    }
  };

  const ttlSeconds = fin(ttlMs) ? Math.round(Number(ttlMs) / 1000) : FALLBACK_TTL_SECONDS;
  const fields = pending && pending.summary && Array.isArray(pending.summary.fields) ? pending.summary.fields : [];
  return (
    <ProCard
      title="待确认请求"
      bordered
      extra={<Text type="secondary" style={{ fontSize: 12 }}>实盘业务确认 · /api/wb/confirmation → confirm-decide（唯一能批准实盘操作的通道）</Text>}
    >
      {envErr ? (
        <Alert type="error" showIcon message="待确认请求读取失败" description={<Text code>{envErr}</Text>} />
      ) : !pending || !pending.id ? (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Alert type="info" showIcon message="当前无待确认请求（空闲）"
            description={
              <Text type="secondary" style={{ fontSize: 12 }}>
                只有实盘写操作（下单/改单/撤单）才会产生待确认；SIM 模式与只读动作不产生。请求出现后本区块每 10 秒自动重读一次。
                没有待确认请求时按钮不可用（决策载荷必须带服务端给出的 id，本页不能凭空构造）。
              </Text>
            } />
          <Space>
            <Button type="primary" disabled title="无 pending.id 时不可批准（fail-closed）">批准</Button>
            <Button disabled>拒绝</Button>
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            TTL {ttlSeconds} 秒：到期或未作答一律按拒绝处理（fail-closed）。口径区分：台账 stage=manual 是平台侧台账审批阶段，不是券商待确认。
          </Text>
          {msg ? <Alert type={msg.tone === "ok" ? "success" : msg.tone === "err" ? "error" : "warning"} showIcon message={msg.text} /> : null}
        </Space>
      ) : (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space size={8} wrap>
            <Text strong>{pending.operation || "实盘写操作"}</Text>
            <Tag color={expired || unknown ? "red" : "gold"} id="v3ConfirmRemain">
              {unknown ? "剩余时间未知（批准已禁用）" : (expired ? "已超时（服务端按拒绝）" : `等待确认中 · 剩余 ${remain} 秒`)}
            </Tag>
          </Space>
          <Descriptions size="small" column={{ xs: 1, sm: 2 }} bordered
            items={[
              { key: "id", label: "请求编号 id", children: <Text code>{String(pending.id)}</Text> },
              { key: "tool", label: "工具", children: String(pending.tool || "—") },
              { key: "mode", label: "模式", children: String(pending.mode || "—") },
              { key: "session", label: "来源 session", children: String(pending.session_id || "—") },
              { key: "at", label: "发起 / 到期", children: `${fmt.stamp(pending.at)} → ${fmt.stamp(pending.expires_at)}` },
              { key: "status", label: "状态", children: String(pending.status || "pending") },
            ]}
          />
          {fields.length > 0 ? (
            <Descriptions size="small" column={1} bordered title="summary"
              items={fields.map((field, index) => ({ key: `f-${index}`, label: String(field.label || "—"), children: <Text code>{field.value === null || field.value === undefined ? "—" : String(field.value)}</Text> }))} />
          ) : (
            <Text type="secondary" style={{ fontSize: 12 }}>{noSourceText("summary.fields", "服务端未给出 summary.fields")}</Text>
          )}
          <Space>
            <Button type="primary" disabled={disabled} loading={busy} onClick={() => click("approved")}
              title={unknown ? "到期时间未知，批准已禁用（fail-closed）" : "批准即授权该笔实盘操作；需二次点击确认；载荷只有 {id, decision}"}>
              {armed && armed.decision === "approved" ? "再次点击确认批准" : "批准"}
            </Button>
            <Button disabled={!pending || !pending.id || busy} loading={busy} onClick={() => click("rejected")}
              title="拒绝该笔实盘操作；需二次点击确认；载荷只有 {id, decision}">
              {armed && armed.decision === "rejected" ? "再次点击确认拒绝" : "拒绝"}
            </Button>
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            批准是唯一能授权实盘操作的通道：决策载荷只有 {"{id, decision}"}，本页不能填写任何下单参数。首次点击只进入待确认态，
            <Text strong>第二次点击才提交</Text>；决定后立刻重读 confirmation。TTL {ttlSeconds} 秒，到期或未知一律按拒绝（fail-closed）。
          </Text>
          {msg ? <Alert type={msg.tone === "ok" ? "success" : msg.tone === "err" ? "error" : "warning"} showIcon message={msg.text} /> : null}
        </Space>
      )}
    </ProCard>
  );
}

/* ── 页面 ───────────────────────────────────────────────────────────────── */
export default function 执行审批Page() {
  const { market } = useMarket();
  // 台账 / 持仓 / 在途 / 成交 / 成交质量全部按当前市场过滤：接口缺省值各不相同（有的默认全部），
  // 所以这里**始终显式带 market**，绝不靠后端默认，也不把「全部市场」当成第四档。
  const exec = useV3("execution", { market });
  const oms = useV3("oms/orders", { market });
  const metrics = useV3("metrics", {});
  const audit = useV3("audit", { window: 120 });
  const risk = useV3("risk", {});
  // 两条写通道的读端点只能 POST（空载荷 = 读状态）：加载与刷新只读，绝不写。
  const plan = useWbRead("plan");
  const confirmation = useWbRead("confirmation");
  const wb = useWbAction();

  const [modalOpen, setModalOpen] = React.useState(false);
  const [selectedId, setSelectedId] = React.useState(null);
  // 成交质量：市场来自统一上下文（页头 MarketPicker）；本卡不再自带市场开关，避免同页两个市场事实源。
  const quality = useQuality(market);
  // 行业暴露与集中度：与风控页同一只读端点，用于取消「行业分类无数据源」的表述（只读 GET，带市场）。
  const industry = useV3("risk/industry", { limit_pct: 20, market });

  const ordersEnvelope = OK(oms.value) ? oms.value : (OK(exec.value) && exec.value.oms ? exec.value.oms : null);
  const orders = ordersEnvelope && Array.isArray(ordersEnvelope.orders) ? ordersEnvelope.orders : [];
  const positions = OK(exec.value) ? exec.value.positions : null;
  const mode = (OK(plan.value) && plan.value.value && plan.value.value.mode) || (positions && positions.mode) || "sim";
  const nav = ordersEnvelope && fin(ordersEnvelope.nav) ? Number(ordersEnvelope.nav) : null;
  const riskCfg = OK(risk.value) && risk.value.data ? (risk.value.data.config || {}) : {};
  const dealRows = flatRows(OK(exec.value) ? exec.value.deals_today : null, ["rows", "deals"]);
  const auditEntries = OK(audit.value) && audit.value.data ? asArray(audit.value.data.entries) : [];
  const planValue = OK(plan.value) ? (plan.value.value || {}) : null;
  const confirmValue = OK(confirmation.value) ? (confirmation.value.value || {}) : null;
  const pending = pendingOf(confirmValue);
  const ttlMs = confirmValue && fin(confirmValue.ttl_ms) ? Number(confirmValue.ttl_ms) : null;
  const stages = ordersEnvelope && ordersEnvelope.stages ? ordersEnvelope.stages : null;
  const planErr = OK(plan.value) ? null : envError(plan.value, "POST /api/wb/plan 取数失败");
  const confirmErr = OK(confirmation.value) ? null : envError(confirmation.value, "POST /api/wb/confirmation 取数失败");

  const selected = orders.find((order) => String(order.id) === String(selectedId)) || orders[0] || null;

  /** 只读重取两条写通道的读端点（plan / confirmation）：加载、打开弹窗、待确认态刷新都只走它。 */
  const reloadGate = React.useCallback(async () => {
    await Promise.all([plan.refresh(), confirmation.refresh()]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan.refresh, confirmation.refresh]);

  const metricsEnv = OK(metrics.value) ? metrics.value : {};

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Block title="执行入口条">
        <ExecHeader mode={mode} planEnv={plan.value} planErr={planErr} orders={orders} deals={dealRows} nav={nav}
          omsNote={ordersEnvelope && ordersEnvelope.note ? ordersEnvelope.note : null}
          onOpenPlan={() => setModalOpen(true)} confirmPending={Boolean(pending && pending.id)} />
      </Block>
      <Block title="订单生命周期">
        <LifecycleBoard execEnv={exec.value} orders={orders} audit={auditEntries} stages={stages} />
      </Block>
      <Block title="分级审批">
        <ApprovalGrades orders={orders} riskCfg={riskCfg} nav={nav} onOpenPlan={() => setModalOpen(true)} industryEnv={industry.value} />
      </Block>
      <Block title="订单明细">
        <ProCard title="订单明细" bordered
          extra={<Text type="secondary" style={{ fontSize: 12 }}>订单号/状态/风控来自 /api/v3/oms/orders；成交均价与滑点无数据源</Text>}>
          {orders.length === 0 ? (
            <Space direction="vertical" size={6} style={{ width: "100%" }}>
              <NoSource what="订单明细" why={ordersEnvelope ? `当前市场（market=${market}）OMS 台账无订单（orders 为空）` : envError(oms.value, `GET /api/v3/oms/orders?market=${market} 取不到`)} />
              <MarketNote source={`GET /api/v3/oms/orders?market=${market}`} extra="其它市场的订单不在此列出" />
            </Space>
          ) : (
            <Table size="small" rowKey={(record) => record.id} pagination={{ pageSize: 10 }} scroll={{ x: 1100 }}
              onRow={(record) => ({ onClick: () => setSelectedId(record.id), style: { cursor: "pointer" } })}
              rowClassName={(record) => (selected && record.id === selected.id ? "ant-table-row-selected" : "")}
              dataSource={orders}
              columns={[
                { title: "订单号", dataIndex: "id", width: 150, render: (value) => <Text code style={{ fontSize: 11 }} title={`台账订单号（OMS 内部 id）：${value}`}>{shortId(value)}</Text> },
                { title: "时间", dataIndex: "updated_at", width: 150, render: (value, record) => <Text style={{ fontSize: 12 }}>{fmt.stamp(value || record.first_seen_at)}</Text> },
                { title: "标的", dataIndex: "ticker", width: 110, render: (value) => <Text style={{ fontSize: 12 }}>{String(value || "—")}</Text> },
                { title: "方向", dataIndex: "side", width: 70, render: (value) => <Text style={{ fontSize: 12, color: String(value).toUpperCase() === "BUY" ? "#3fb950" : "#f8514d" }}>{sideText(value)}</Text> },
                { title: "委托价", dataIndex: "price", width: 90, align: "right", render: (value) => fmt.num(value, 2) },
                { title: "数量", dataIndex: "qty", width: 90, align: "right", render: (value) => fmt.num(value, 0) },
                { title: "金额", dataIndex: "value", width: 120, align: "right", render: (value) => fmt.money(value) },
                { title: "阶段", dataIndex: "stage", width: 100, render: (value) => <Tag color={stageColor(value)}>{stageText(value)}</Tag> },
                { title: "风控结论", dataIndex: "risk", render: (value) => <Text type="secondary" style={{ fontSize: 11 }}>{asArray(value && value.reasons).join("；") || "未给出原因"}</Text> },
                { title: "成交均价", key: "fillAvg", width: 100, render: () => <Text type="secondary" style={{ fontSize: 11 }} title={noSourceText("成交均价", "券商成交流水未接入")}>无数据源</Text> },
                { title: "滑点(bp)", key: "slip", width: 100, render: () => <Text type="secondary" style={{ fontSize: 11 }} title={noSourceText("滑点", "券商成交流水未接入")}>无数据源</Text> },
                { title: "trace id", key: "trace", width: 100, render: () => <Text type="secondary" style={{ fontSize: 11 }} title={noSourceText("逐单 trace id", "OMS 台账只有计划号")}>无数据源</Text> },
              ]}
            />
          )}
          <MarketNote
            source={`GET /api/v3/oms/orders?market=${market}`}
            extra={`当前市场台账 ${orders.length} 单（跨市场不合并）`}
            style={{ display: "block", marginTop: 8 }}
          />
        </ProCard>
      </Block>
      <Row gutter={[12, 12]}>
        <Col xs={24} xl={12}>
          <Block title="持仓摘要">
            <HoldingsCard execEnv={exec.value} />
          </Block>
        </Col>
        <Col xs={24} xl={12}>
          <Block title="成交质量">
            <QualityCard market={market} qualityEnv={quality} />
          </Block>
        </Col>
      </Row>
      <Block title="决策链路追溯">
        <DecisionTrace order={selected} audit={auditEntries} riskCfg={riskCfg} nav={nav} execEnv={exec.value} />
      </Block>
      <Block title="待确认请求">
        <ConfirmChannel env={confirmation.value} envErr={confirmErr} pending={pending} ttlMs={ttlMs} wb={wb}
          reload={reloadGate} onDecided={oms.refresh} />
      </Block>
      <Block title="无数据源项">
        <ProCard title="无数据源项（逐项写明原因，不填占位数字）" bordered>
          <Space direction="vertical" size={4} style={{ width: "100%" }}>
            {[
              { what: "模块化执行参数（TWAP 片数 / 执行算法）", why: "工具面无执行算法参数与切片计划数据源" },
              { what: "PIT 因子快照与情绪评分", why: "工作台订单未携带 PIT 因子快照，工具面无情绪评分" },
              { what: "盘中市场状态", why: "工具面无盘中市场状态快照" },
              { what: "逐单决策 trace id", why: "OMS 台账只有计划号，无逐单 trace id" },
            ].map((item) => (
              <Text key={item.what} style={{ fontSize: 12 }}>
                <Text strong>{item.what}</Text>
                <Text type="secondary">：{noSourceText(item.what, item.why)}</Text>
              </Text>
            ))}
          </Space>
        </ProCard>
      </Block>
      <Text type="secondary" style={{ fontSize: 12 }}>
        数据来源：/api/v3/execution?market={market}、/api/v3/oms/orders?market={market}、/api/v3/metrics（{metricsEnv.toolTotal === undefined ? "—" : metricsEnv.toolTotal} 个工具面工具，工作台{metricsEnv.workbenchUp ? "在线" : "不可达"}）、
        /api/v3/audit 与 /api/wb/plan、/api/wb/confirmation 实时接口 · 当前市场：{marketLabel(market)} · 持仓 as_of {fmt.stamp(positions && positions.as_of)} · 唯一受约束执行入口（plan-execute + 人工二次确认）。页面不含占位数字。
        {" "}<Link href="/v3/execution.html">对照设计稿原样版</Link>
      </Text>
      <PlanModal open={modalOpen} onClose={() => setModalOpen(false)} planEnv={plan.value} planErr={planErr}
        mode={mode} wb={wb} reloadGate={reloadGate} />
    </Space>
  );
}
