// V3 工作台「策略与因子」页（Ant Design Pro）。
//
// 模块与设计稿原样版 platform/web/public/v3/strategy.js（= 功能规格书）**一一对应**：
//   ① 研究流水线五阶段 PDAT→PAAT→PCPT→PRT→PET（Steps，真实 run.stages 逐阶段细节）+「新建研究轮」
//   ② 调仓提案表（run.proposals：标的/动作/目标权重/风险等级/依据）
//   ③ 因子库表（factors/matrix 的 z 矩阵 + 逐因子真实 IC/IR/样本，可对若干因子追加请求 ?factor=X）
//   ④ 参数扫描热力图（Heatmap ← ml/sweep 真实网格 + 标注最优格）
//   ⑤ 回测曲线（LineChart ← POST /api/v3/ml/backtest 的真实 equity）+ 年化/夏普/回撤/胜率
//   ⑥ 策略候选（真实网格按夏普前 3 + 流水线提案，点击即真跑该参数回测）
//   ⑦ 无数据源项：分层年化多空、换手率、相关性、基准指数、置信度（逐项写明原因）
//
// 取数：GET /api/v3/*（useV3 读）；写动作只有两个，且都只由人工点击发起：
//   POST /api/v3/strategy/run {topN}（跑研究流水线，只出提案，不下单）
//   POST /api/v3/ml/backtest  {ticker,window,rebalanceDays,limit}（PIT 回测取净值序列）
// 页面绝不自动写：没有任何 useEffect/轮询会调用这两个 POST。
import React from "react";
import {
  Alert, Button, Col, Descriptions, Empty, Row, Space, Statistic, Steps, Table, Tag, Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { useV3, fmt, noSourceText } from "../services/api.js";
import { MarketNote, envelopeError, marketLabel, marketTicker, tickerMarket, useMarket } from "../services/marketContext.jsx";
import { LineChart, Heatmap } from "../components/charts.jsx";

const { Text, Paragraph, Link } = Typography;

/* ── 通用小工具（每块失败只影响该块；绝不白屏） ───────────────────────────── */

/** 单块错误边界：某一块渲染/取数炸了，只把该块换成 Alert，页面其余部分照常。 */
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

/** 统一「无数据源 · 原因」渲染（措辞来自 services/api.js 的 noSourceText）。 */
function NoSource({ what, why }) {
  return (
    <Empty imageStyle={{ display: "none" }} style={{ margin: 0 }} description={<Text type="secondary">{noSourceText(what, why)}</Text>} />
  );
}

const OK = (env) => Boolean(env && env.ok);
const rowKey = (...parts) => parts.join("|");

/** 信封错误文案：`error.code：error.message` + 有 error.detail 时追加「真实原因」
 *  （统一口径见 services/marketContext.jsx 的 envelopeError：message 是冻结文案，
 *  真实原因常在 detail，只显示 message 会把「富途限频」误读成「这个市场没有池子」）。 */
function envError(env, fallback) {
  return envelopeError(env, fallback || "接口未返回 error.code/message");
}

/**
 * POST /api/v3/* —— 与 services/api.js 同一鉴权约定（localStorage.trading_token → Bearer）。
 * 本页的写动作只经人工点击调用；服务端错误以 200 + {ok:false,error} 信封返回，这里不做重试。
 */
async function postV3(path, payload) {
  const headers = { "content-type": "application/json" };
  try {
    const token = window.localStorage.getItem("trading_token");
    if (token) headers.Authorization = `Bearer ${token}`;
  } catch {
    /* localStorage 不可用时按未鉴权处理（与服务端读通道同口径） */
  }
  try {
    const response = await fetch(`/api/v3/${path}`, { method: "POST", headers, body: JSON.stringify(payload ?? {}) });
    const body = await response.json().catch(() => null);
    if (response.status === 401) return { ok: false, error: { code: "http/401", message: "需要访问令牌：右上角「令牌」填入服务配置的 token" } };
    if (!response.ok && !body) return { ok: false, error: { code: `http/${response.status}`, message: `HTTP ${response.status}` } };
    return body ?? { ok: false, error: { code: `http/${response.status}`, message: `HTTP ${response.status}` } };
  } catch (error) {
    return { ok: false, error: { code: "net", message: String((error && error.message) || error) } };
  }
}

const asArray = (value) => (Array.isArray(value) ? value : []);
const fin = (value) => Number.isFinite(Number(value));

/* ── 流水线阶段（与 binder 的 STAGE_TEXT 同一口径） ─────────────────────── */
const STAGE_TEXT = {
  PDAT: "数据准备",
  PAAT: "因子分析",
  PCPT: "规则候选",
  PRT: "回测验证",
  PET: "评估产出",
};
const STAGE_ORDER = ["PDAT", "PAAT", "PCPT", "PRT", "PET"];

/* ── 因子类别：按真实因子名前缀归类（与 binder 的 CATEGORY 同规则） ─────── */
const CATEGORY = [
  { match: /^mom_/, label: "动量", color: "blue" },
  { match: /^(pe_|pb|ps|peg)/, label: "价值", color: "purple" },
  { match: /^(rsi_|trend)/, label: "情绪", color: "gold" },
  { match: /^(liq_|vol_|mdd_)/, label: "另类", color: "green" },
];
function categoryOf(name) {
  for (const item of CATEGORY) if (item.match.test(String(name))) return item;
  return { label: "其他", color: "default" };
}

/** 因子状态判定：与 binder factorStatus 同一规则（真实 IC/IR 推导，不用占位值）。 */
function factorStatus(ic) {
  const mean = Number(ic && ic.meanIc);
  const ir = Number(ic && ic.ir);
  if (!Number.isFinite(mean) || !Number.isFinite(ir)) return { text: "无数据源", color: "default" };
  if (Math.abs(mean) < 0.03) return { text: "待观察", color: "default" };
  if (Math.abs(ir) >= 0.3) return mean > 0 ? { text: "有效", color: "green" } : { text: "反向", color: "gold" };
  return { text: "衰减", color: "gold" };
}

/** 初始就对若干因子并行请求 IC（factors/matrix?factor=X）：服务端 IC 有 30 分钟磁盘缓存。
 *  这里只取 4 个（含基线 mom_20），避免首屏堆叠过多次 5–10s 的因子计算。 */
const IC_FACTORS = ["mom_60", "rsi_14", "trend", "liq_ratio"];
const IC_MAX = 8;

/* ── ① 研究流水线五阶段 ─────────────────────────────────────────────────── */
function pipelineDetails(run) {
  const stages = (run && run.stages) || {};
  const pdat = stages.PDAT || {};
  const paat = stages.PAAT || {};
  const pcpt = stages.PCPT || {};
  const prt = stages.PRT || {};
  const pet = stages.PET || {};
  const universe = asArray(pdat.universe).length ? asArray(pdat.universe) : asArray(run && run.universe);
  return {
    PDAT: `标的池 ${universe.length} 只 · 日 K ${pdat.bars === undefined ? "—" : pdat.bars} 根`,
    PAAT: `已分析 ${paat.analyzed === undefined ? "—" : paat.analyzed} / 有因子 ${paat.withFactors === undefined ? "—" : paat.withFactors} · ${paat.scoreSource || "—"}`,
    PCPT: `增持 ${asArray(pcpt.longs).length} · 减持 ${asArray(pcpt.reduces).length}`,
    PRT: `每标的 ${prt.weightPctPerName === undefined ? "—" : fmt.num(prt.weightPctPerName, 1)}% · 上限约束 ${prt.capped ? "已启用" : "未启用"}`,
    PET: `${pet.proposals === undefined ? asArray(run && run.proposals).length : pet.proposals} 条调仓建议`,
  };
}

function PipelineCard({ env, refresh, ticker, market }) {
  const run = OK(env) ? env.run : null;
  const [busy, setBusy] = React.useState(false);
  const [note, setNote] = React.useState(null);
  const details = pipelineDetails(run);
  // run 记录自带 market 时原样显示（服务端按该市场落盘）；未返回则标「未返回」，不臆测
  const runMarket = run && run.market ? String(run.market) : null;

  const runRound = async () => {
    setBusy(true);
    setNote({ tone: "info", text: `正在跑真实流水线 PDAT→PET（market=${market}，只出调仓建议提案，不下单）…` });
    // 研究动作只由人工点击触发；载荷显式带 market，universe 随市场
    const body = await postV3("strategy/run", { topN: 2, window: 20, market });
    setBusy(false);
    if (OK(body)) {
      setNote({ tone: "ok", text: `已触发真实流水线（market=${(body.run || {}).market || market}）：as_of ${fmt.stamp((body.run || {}).asOf)}${body.persistError ? ` · 落盘失败：${body.persistError}` : ""}` });
      refresh();
    } else {
      setNote({ tone: "err", text: `流水线失败：${envError(body, `POST /api/v3/strategy/run（market=${market}）失败`)}` });
    }
  };

  return (
    <ProCard
      title="研究流水线（PDAT → PAAT → PCPT → PRT → PET）"
      bordered
      extra={
        <Space>
          <Text type="secondary" style={{ fontSize: 12 }}>研究轮记录 as_of {run ? fmt.stamp(run.asOf) : "—"}</Text>
          <Button size="small" type="primary" loading={busy} onClick={runRound}
            title={`POST /api/v3/strategy/run {topN:2, window:20, market:${market}}：只由人工点击触发，产物是调仓建议提案（不下单）`}>
            新建研究轮
          </Button>
        </Space>
      }
    >
      {!OK(env) ? (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what="研究流水线 PDAT→PET" why={envError(env, `GET /api/v3/strategy?market=${market} 取不到：该市场尚无落盘记录（无自选池/无持仓时服务端返回 market/no-universe），可点「新建研究轮」跑一轮真实流水线`)} />
          <MarketNote source={`GET /api/v3/strategy?market=${market}`} />
        </Space>
      ) : !run ? (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what="研究流水线 PDAT→PET" why={`GET /api/v3/strategy?market=${market} 返回 run=null：该市场尚无落盘记录，可点「新建研究轮」跑一轮真实流水线`} />
          <MarketNote source={`GET /api/v3/strategy?market=${market}`} />
        </Space>
      ) : (
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          <Steps
            size="small"
            current={STAGE_ORDER.length}
            items={STAGE_ORDER.map((code) => ({
              title: code,
              description: `${STAGE_TEXT[code]} · ${details[code]}`,
              status: "finish",
            }))}
          />
          <Descriptions size="small" column={{ xs: 1, sm: 3 }} bordered
            items={[
              { key: "asOf", label: "落盘时间", children: fmt.stamp(run.asOf) },
              { key: "market", label: "研究轮 market", children: runMarket ? <Tag color="blue">{`${marketLabel(runMarket)}`}</Tag> : "接口未返回（无数据源）" },
              { key: "universe", label: "标的池", children: `${asArray(run.universe).length} 只` },
              { key: "proposals", label: "调仓建议", children: `${asArray(run.proposals).length} 条` },
              { key: "scoreSource", label: "评分来源", children: ((run.stages || {}).PAAT || {}).scoreSource || "—" },
              { key: "factorsError", label: "因子回退", children: ((run.stages || {}).PAAT || {}).factorsError || "无（因子接口正常）" },
              { key: "bars", label: "日 K 合计", children: ((run.stages || {}).PDAT || {}).bars === undefined ? "—" : String(((run.stages || {}).PDAT || {}).bars) },
            ]}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            口径：/api/v3/strategy 只落盘最后一轮研究记录（本轮 market={runMarket || market}）；本轮回测标的 {ticker || "—"}。该接口**不下单**，PET 产物只是调仓建议提案。
          </Text>
          <MarketNote source={`GET /api/v3/strategy?market=${market}`} asOf={run.asOf} extra={runMarket ? `记录内 market=${runMarket}` : "记录未返回 market 字段"} />
        </Space>
      )}
      {note ? <Alert style={{ marginTop: 10 }} type={note.tone === "ok" ? "success" : note.tone === "err" ? "error" : "info"} showIcon message={note.text} /> : null}
    </ProCard>
  );
}

/* ── ② 调仓提案表 ───────────────────────────────────────────────────────── */
function ProposalsCard({ env }) {
  const { market } = useMarket();
  const run = OK(env) ? env.run : null;
  const proposals = asArray(run && run.proposals);
  const columns = [
    { title: "标的", dataIndex: "ticker", width: 120, render: (value) => <Text code>{String(value || "—")}</Text> },
    {
      title: "动作", dataIndex: "action", width: 90,
      render: (value) => <Tag color={String(value) === "增持" ? "green" : String(value) === "减持" ? "red" : "default"}>{String(value || "—")}</Tag>,
    },
    {
      title: "目标权重", dataIndex: "targetWeightPct", width: 110, align: "right",
      render: (value) => (fin(value) ? `${Number(value).toFixed(2)}%` : "—"),
    },
    {
      title: "风险等级", dataIndex: "riskLevel", width: 100,
      render: (value) => <Tag color={String(value) === "低" ? "green" : String(value) === "中" ? "gold" : "red"}>{String(value || "—")}</Tag>,
    },
    { title: "依据", dataIndex: "basis", render: (value) => <Text style={{ fontSize: 12 }}>{String(value || "—")}</Text> },
    {
      title: "执行口径", dataIndex: "action_hint", render: (value) => <Text type="secondary" style={{ fontSize: 12 }}>{String(value || "—")}</Text>,
    },
  ];
  return (
    <ProCard title="调仓提案" bordered extra={<Text type="secondary" style={{ fontSize: 12 }}>来源 /api/v3/strategy · run.proposals（真实流水线产物）</Text>}>
      {!OK(env) ? (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what="调仓提案" why={envError(env, `GET /api/v3/strategy?market=${market} 取不到`)} />
          <MarketNote source={`GET /api/v3/strategy?market=${market}`} />
        </Space>
      )
        : proposals.length === 0 ? (
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            <NoSource what="调仓提案" why={`本轮（market=${market}）流水线 PET 阶段未产出提案（run.proposals 为空）`} />
            <MarketNote source={`GET /api/v3/strategy?market=${market}`} asOf={run && run.asOf} />
          </Space>
        )
          : (
            <Space direction="vertical" size={6} style={{ width: "100%" }}>
              <Table size="small" rowKey={(record) => rowKey(record.ticker, record.action)} pagination={false}
                columns={columns} dataSource={proposals} scroll={{ x: 900 }} />
              <MarketNote source={`GET /api/v3/strategy?market=${market} · run.proposals`} asOf={run && run.asOf} extra={`提案 ${proposals.length} 条（均为该市场标的池产物）`} />
            </Space>
          )}
    </ProCard>
  );
}

/* ── ③ 因子库表 ─────────────────────────────────────────────────────────── */
function FactorLibraryCard({ baseEnv, onLoadMore, loadingMore, loadedFactors }) {
  const { market } = useMarket();
  const matrix = OK(baseEnv) ? baseEnv.matrix : null;
  const tickers = asArray(matrix && matrix.tickers);
  const factors = asArray(matrix && matrix.factors);
  const rows = asArray(matrix && matrix.matrix);
  const zOf = (rowIndex, factorName) => {
    const col = factors.indexOf(factorName);
    if (col < 0 || !rows[rowIndex]) return null;
    const value = rows[rowIndex][col];
    return fin(value) ? Number(value) : null;
  };
  const columns = [
    { title: "因子名", dataIndex: "name", width: 130, render: (value, record) => <Space size={4}><Text code>{String(value)}</Text>{record.hasIc ? null : <Tag color="default" style={{ marginInlineStart: 0 }}>无 IC</Tag>}</Space> },
    {
      title: "类别", dataIndex: "category", width: 90,
      render: (value) => <Tag color={value.color}>{value.label}</Tag>,
    },
    {
      title: `z 值 · ${tickers[0] || "—"}`, dataIndex: "z", width: 130, align: "right",
      render: (value) => (fin(value) ? fmt.num(value, 4) : "—"),
    },
    {
      title: "多标的 z", dataIndex: "zs", width: 240,
      render: (value) => (Array.isArray(value) && value.length
        ? <Text type="secondary" style={{ fontSize: 11 }}>{value.map((item) => `${item.ticker}=${item.z === null ? "—" : Number(item.z).toFixed(3)}`).join(" · ")}</Text>
        : <Text type="secondary" style={{ fontSize: 11 }}>—</Text>),
    },
    {
      title: "IC 均值（近 60 日 RankIC）", dataIndex: "meanIc", width: 170, align: "right",
      render: (value) => (fin(value) ? fmt.num(value, 4) : "无数据源"),
    },
    { title: "IR", dataIndex: "ir", width: 90, align: "right", render: (value) => (fin(value) ? fmt.num(value, 3) : "无数据源") },
    { title: "样本（期）", dataIndex: "observations", width: 110, align: "right", render: (value) => (fin(value) ? fmt.num(value, 0) : "无数据源") },
    {
      title: "分层年化多空", dataIndex: "longShort", width: 130, align: "right",
      render: () => <Text type="secondary" title={noSourceText("分层年化多空", "接口未返回该字段")}>无数据源</Text>,
    },
    {
      title: "换手率", dataIndex: "turnover", width: 100, align: "right",
      render: () => <Text type="secondary" title={noSourceText("换手率", "接口未返回该字段")}>无数据源</Text>,
    },
    {
      title: "相关性", dataIndex: "correlation", width: 100, align: "right",
      render: () => <Text type="secondary" title={noSourceText("相关性", "接口未返回该字段")}>无数据源</Text>,
    },
    {
      title: "状态", dataIndex: "status", width: 100,
      render: (value) => <Tag color={value.color} title="判定规则：|IC| < 0.03 → 待观察；|IR| ≥ 0.30 且 IC > 0 → 有效；|IC| < 0 且 |IR| ≥ 0.30 → 反向；否则 衰减（基于真实 IC/IR）">{value.text}</Tag>,
    },
  ];
  const data = factors.map((name) => {
    const ic = loadedFactors[name];
    // 「取到 IC」= ok:true 且有观测样本；observations=0（该因子在数据源里没有可用 IC）按无数据源处理，
    // 绝不把 0 当作真实 IC 展示。
    const hasIc = Boolean(ic && ic.ok && fin(ic.meanIc) && Number(ic.observations) > 0);
    return {
      key: name,
      name,
      category: categoryOf(name),
      z: rows.length ? zOf(0, name) : null,
      zs: tickers.map((ticker, index) => ({ ticker, z: rows.length > index ? zOf(index, name) : null })),
      hasIc,
      meanIc: hasIc ? ic.meanIc : null,
      ir: hasIc ? ic.ir : null,
      observations: hasIc ? ic.observations : null,
      status: factorStatus(hasIc ? ic : null),
    };
  });
  const baseIc = OK(baseEnv) && baseEnv.ic && baseEnv.ic.ok && Number(baseEnv.ic.observations) > 0 ? baseEnv.ic : null;
  const canLoadMore = factors.filter((name) => !(loadedFactors[name] && loadedFactors[name].ok && Number(loadedFactors[name].observations) > 0)).length > 0
    && Object.values(loadedFactors).filter((item) => item && item.ok && Number(item.observations) > 0).length < IC_MAX;

  if (!OK(baseEnv)) {
    return (
      <ProCard title="因子库" bordered>
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what="因子库" why={envError(baseEnv, `GET /api/v3/factors/matrix?market=${market} 取不到`)} />
          <MarketNote source={`GET /api/v3/factors/matrix?market=${market}`} />
        </Space>
      </ProCard>
    );
  }
  return (
    <ProCard
      title="因子库"
      bordered
      extra={
        <Space size={8}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            横截面 RankIC · forward {baseIc ? fmt.num(baseIc.forwardDays, 0) : "—"} 日 · 矩阵 {tickers.length} 只 × {factors.length} 因子 · as_of {matrix.as_of || "—"} · 来源 {(matrix && matrix.source) || "/api/v3/factors/matrix"}
          </Text>
          <Button size="small" loading={loadingMore} disabled={!canLoadMore} onClick={onLoadMore}
            title="对尚未取 IC 的因子逐个请求 GET /api/v3/factors/matrix?factor=X（每次 5–10s，服务端 IC 有 30 分钟缓存；本页串行且封顶 8 个）">
            补齐因子 IC
          </Button>
        </Space>
      }
    >
      {factors.length === 0 ? (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what="因子库" why="factors/matrix 返回的 factors 为空" />
          <MarketNote source={`GET /api/v3/factors/matrix?market=${market}`} />
        </Space>
      ) : (
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Table size="small" rowKey="key" pagination={false} columns={columns} dataSource={data} scroll={{ x: 1200 }} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            注：IC 为横截面 RankIC（forward {baseIc ? fmt.num(baseIc.forwardDays, 0) : "—"} 日，来源 {(baseIc && baseIc.source) || "workbench/ic"}）；
            默认基线因子为 mom_20（{(baseIc && baseIc.tickers ? baseIc.tickers.length : 0)} 只标的、{baseIc ? fmt.num(baseIc.observations, 0) : "—"} 期样本）。
            分层年化多空、换手率、相关性本服务未返回，标注「无数据源」而不填占位数字。
          </Text>
          <MarketNote
            source={`GET /api/v3/factors/matrix?market=${market}（逐因子 IC 为 ?factor=X&market=${market}）`}
            asOf={matrix && matrix.as_of}
            extra={`${tickers.length} 只 × ${factors.length} 因子`}
          />
        </Space>
      )}
    </ProCard>
  );
}

/* ── ④ 参数扫描热力图 ───────────────────────────────────────────────────── */
function HeatmapCard({ env, onRescan, scanning, ticker }) {
  const { market } = useMarket();
  const sweep = OK(env) ? env : null;
  const grid = asArray(sweep && sweep.grid);
  const best = (sweep && sweep.best) || null;
  const valid = grid.filter((cell) => fin(cell.sharpe));
  return (
    <ProCard
      title="参数扫描 · 夏普热力图"
      bordered
      extra={
        <Space size={8}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            momentum_window × rebalance_days · 标的 {ticker || "—"}
            {best ? ` · 最优 window=${best.window} / rebalance=${best.rebalanceDays}（夏普 ${fmt.num(best.sharpe, 3)}）` : ""}
          </Text>
          <Button size="small" loading={scanning} onClick={onRescan}
            title="GET /api/v3/ml/sweep?ticker=&windows=10,20,30,60&rebalance=5,10,20：真实日 K 网格回测（只读，不发写请求）">
            重新扫描
          </Button>
        </Space>
      }
    >
      {!OK(env) ? (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what="参数扫描热力图" why={envError(env, `GET /api/v3/ml/sweep?ticker=${ticker || marketTicker(market)}&market=${market} 未返回参数网格`)} />
          <MarketNote source={`GET /api/v3/ml/sweep?market=${market}`} extra={`扫描标的 ${ticker || marketTicker(market)}`} />
        </Space>
      )
        : valid.length === 0 ? (
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            <NoSource what="参数扫描热力图" why={`ml/sweep（market=${market}）返回的网格里没有有效夏普（全部无效格）`} />
            <MarketNote source={`GET /api/v3/ml/sweep?market=${market}`} extra={`扫描标的 ${ticker || marketTicker(market)}`} />
          </Space>
        )
          : (
            <Space direction="vertical" size={8} style={{ width: "100%" }}>
              <div style={{ overflowX: "auto" }}>
                <Heatmap grid={valid} rowKey="rebalanceDays" colKey="window" valueKey="sharpe" />
              </div>
              <Text type="secondary" style={{ fontSize: 12 }}>
                共 {grid.length} 组参数（{valid.length} 组有效）· 每格为真实 PIT 日 K 回测夏普；色越暖＝夏普越高，最优格在标题处标注。
                无效格（回测失败）显示为虚线「—」，不填占位数字。
              </Text>
              <MarketNote
                source={`GET /api/v3/ml/sweep?ticker=${ticker || marketTicker(market)}&windows=10,20,30,60&rebalance=5,10,20&market=${market}`}
                extra={`${grid.length} 组参数（${valid.length} 组有效）`}
              />
            </Space>
          )}
    </ProCard>
  );
}

/* ── ⑤ 回测曲线 + ⑥ 策略候选（同一份回测/候选状态） ────────────────────── */
function BacktestCard({ result, busy, onRun, candidate, ticker }) {
  const { market } = useMarket();
  const env = result && result.env ? result.env : null;
  const metrics = OK(env) ? env.metrics : null;
  const equity = asArray(OK(env) && env.equity);
  const points = equity.map((point) => ({ t: String(point.t || ""), v: Number(point.value) })).filter((point) => fin(point.v));
  const values = points.map((point) => point.v);
  const startEquity = values.length ? values[0] : null;
  const rebased = startEquity && startEquity !== 0 ? values.map((value) => (value / startEquity) * 100) : values;
  return (
    <ProCard
      title="回测曲线"
      bordered
      extra={
        <Space size={8}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {candidate
              ? `${candidate.name} · ${env && env.ticker ? env.ticker : ticker || "—"} · window=${candidate.window} / rebalance=${candidate.rebalanceDays}`
              : "点击「策略候选」里的行或下方按钮，对选定参数真跑一次 PIT 回测"}
          </Text>
          <Button size="small" type="primary" loading={busy} disabled={!candidate} onClick={() => onRun(candidate)}
            title="POST /api/v3/ml/backtest {ticker,window,rebalanceDays,limit:500}：只由人工点击触发（只读回测，不下单）">
            提交回测
          </Button>
        </Space>
      }
    >
      {!result ? (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what="回测曲线" why="尚未提交回测：本页不会自动发起回测；请在「策略候选」选择参数后点「提交回测」" />
          <MarketNote source={`POST /api/v3/ml/backtest?market=${market}（人工点击才发起）`} extra={`默认标的 ${ticker || marketTicker(market)}`} />
        </Space>
      ) : !OK(env) ? (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what="回测曲线" why={envError(env, `POST /api/v3/ml/backtest（market=${market}）失败`)} />
          <MarketNote source={`POST /api/v3/ml/backtest?market=${market}`} extra={`标的 ${ticker || marketTicker(market)}`} />
        </Space>
      ) : points.length < 2 ? (
        <Space direction="vertical" size={6} style={{ width: "100%" }}>
          <NoSource what="回测曲线" why={`回测返回的净值点位不足 2 个（${points.length} 个）`} />
          <MarketNote source={`POST /api/v3/ml/backtest?market=${market}`} extra={`标的 ${(env && env.ticker) || ticker || marketTicker(market)}`} />
        </Space>
      ) : (
        <Space direction="vertical" size={10} style={{ width: "100%" }}>
          <Row gutter={[12, 12]}>
            {[
              { title: "年化收益", value: metrics.annReturnPct, precision: 2, suffix: "%", tone: true },
              { title: "夏普", value: metrics.sharpe, precision: 3, suffix: null, tone: false },
              { title: "最大回撤", value: metrics.maxDrawdownPct, precision: 2, suffix: "%", tone: "down" },
              { title: "胜率", value: metrics.winRatePct, precision: 1, suffix: "%", tone: false },
            ].map((card) => {
              // antd Statistic 的 value 默认是 0（`undefined`→0）、`null` 会被 String() 渲染成字面量
              // `null`（带 suffix 就是 `null%`）。缺失一律显式传「—」并去掉 suffix/颜色。
              const has = fin(card.value);
              const tone = card.tone === true
                ? (Number(card.value) >= 0 ? "#3fb950" : "#f8514d")
                : card.tone === "down" ? "#f8514d" : undefined;
              return (
                <Col key={card.title} xs={12} md={6}>
                  <Statistic
                    title={card.title}
                    value={has ? Number(card.value) : "—"}
                    precision={has ? card.precision : undefined}
                    suffix={has && card.suffix ? card.suffix : undefined}
                    valueStyle={{ color: has ? tone : undefined, fontSize: 18 }}
                  />
                </Col>
              );
            })}
          </Row>
          <LineChart values={rebased} height={200} labels={[points[0].t, points[points.length - 1].t]}
            name={`${(env && env.ticker) || ticker || "标的"} 策略净值（起点归一 100）`} />
          <Descriptions size="small" column={{ xs: 1, sm: 4 }} bordered
            items={[
              { key: "days", label: "回测交易日", children: metrics.days === undefined ? "—" : String(metrics.days) },
              { key: "held", label: "持仓日", children: metrics.heldDays === undefined ? "—" : String(metrics.heldDays) },
              { key: "flat", label: "空仓日", children: metrics.flatDays === undefined ? "—" : String(metrics.flatDays) },
              { key: "flips", label: "信号翻转", children: metrics.signalFlips === undefined ? "—" : String(metrics.signalFlips) },
              { key: "range", label: "区间", children: `${points[0].t} → ${points[points.length - 1].t}` },
              { key: "source", label: "来源", children: "POST /api/v3/ml/backtest（PIT，t 日持仓只由 ≤ t−1 收盘价决定）" },
              { key: "currency", label: "曲线口径", children: `净值起点 ${startEquity === null ? "—" : fmt.num(startEquity, 4)} → 末值 ${values.length ? fmt.num(values[values.length - 1], 4) : "—"}（图上按起点归一 100 绘制）` },
              { key: "turnover", label: "换手率", children: <Text type="secondary" title={noSourceText("换手率", "POST /api/v3/ml/backtest 未返回该字段")}>无数据源</Text> },
            ]}
          />
          <Alert type="info" showIcon message="基准指数：无数据源"
            description={<Text type="secondary" style={{ fontSize: 12 }}>{noSourceText("基准指数对比", "回测接口只返回单标的策略净值，未返回基准净值序列（/api/v3/risk/analytics 的基准是组合口径，不能当作本回测基准）")}</Text>} />
          <MarketNote
            source={`POST /api/v3/ml/backtest（market=${market}，PIT 净值）`}
            extra={`标的 ${(env && env.ticker) || ticker || marketTicker(market)} · window=${candidate ? candidate.window : "—"} / rebalance=${candidate ? candidate.rebalanceDays : "—"}`}
          />
        </Space>
      )}
    </ProCard>
  );
}

function CandidatesCard({ sweepEnv, strategyEnv, selectedKey, onRun }) {
  const { market } = useMarket();
  const sweep = OK(sweepEnv) ? sweepEnv : null;
  const run = OK(strategyEnv) ? strategyEnv.run : null;
  const grid = asArray(sweep && sweep.grid).filter((cell) => fin(cell.sharpe)).slice().sort((a, b) => Number(b.sharpe) - Number(a.sharpe));
  const best = (sweep && sweep.best) || grid[0] || null;
  const rows = grid.slice(0, 3).map((cell, index) => ({
    key: `sweep-${cell.window}-${cell.rebalanceDays}`,
    kind: "sweep",
    name: `动量 long/flat · window=${cell.window} / rebalance=${cell.rebalanceDays}`,
    badge: best && Number(cell.window) === Number(best.window) && Number(cell.rebalanceDays) === Number(best.rebalanceDays) ? { text: "网格最优", color: "green" } : { text: "参数网格", color: "blue" },
    sharpe: cell.sharpe,
    annReturnPct: cell.annReturnPct,
    maxDrawdownPct: cell.maxDrawdownPct,
    window: cell.window,
    rebalanceDays: cell.rebalanceDays,
    source: "GET /api/v3/ml/sweep",
    rank: index,
  }));
  if (run) {
    rows.push({
      key: "pipeline",
      kind: "pipeline",
      name: "研究流水线 · 综合动量选股（PDAT→PET）",
      badge: { text: "已产出", color: "green" },
      sharpe: null,
      annReturnPct: null,
      maxDrawdownPct: null,
      window: null,
      rebalanceDays: null,
      source: "GET /api/v3/strategy",
      meta: `最近一轮 ${fmt.stamp(run.asOf)} · 提案 ${asArray(run.proposals).length} 条 · 评分来源 ${((run.stages || {}).PAAT || {}).scoreSource || "—"} · 回测曲线：${noSourceText("流水线净值序列", "流水线只产出调仓建议提案，不产出净值序列")}`,
    });
  }
  const columns = [
    {
      title: "候选", dataIndex: "name", width: 300,
      render: (value, record) => (
        <Space size={6}>
          <Text strong={record.kind === "sweep"}>{String(value)}</Text>
          <Tag color={record.badge.color}>{record.badge.text}</Tag>
        </Space>
      ),
    },
    { title: "夏普", dataIndex: "sharpe", width: 90, align: "right", render: (value) => (fin(value) ? fmt.num(value, 3) : "—") },
    { title: "年化", dataIndex: "annReturnPct", width: 100, align: "right", render: (value) => (fin(value) ? `${fmt.signed(value, 2)}%` : "—") },
    { title: "最大回撤", dataIndex: "maxDrawdownPct", width: 110, align: "right", render: (value) => (fin(value) ? `${fmt.num(value, 2)}%` : "—") },
    { title: "参数", key: "param", width: 180, render: (value, record) => (record.kind === "sweep" ? <Text code style={{ fontSize: 11 }}>window={record.window} · rebalance={record.rebalanceDays}</Text> : "—") },
    { title: "来源", dataIndex: "source", width: 170, render: (value) => <Text type="secondary" style={{ fontSize: 11 }}>{String(value)}</Text> },
    {
      title: "操作", key: "action", width: 120,
      render: (value, record) => (record.kind === "sweep"
        ? <Button size="small" type={selectedKey === record.key ? "primary" : "default"} onClick={() => onRun(record)}>跑该参数回测</Button>
        : <Text type="secondary" style={{ fontSize: 12 }}>无净值序列</Text>),
    },
  ];
  return (
    <ProCard title="策略候选"
      bordered
      extra={<Text type="secondary" style={{ fontSize: 12 }}>真实网格按夏普前 3 + 研究流水线 · 点击「跑该参数回测」即真跑该参数（POST /api/v3/ml/backtest）</Text>}>
      {rows.length === 0
        ? (
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            <NoSource what="策略候选" why={OK(sweepEnv) ? `ml/sweep（market=${market}）网格里没有有效夏普，且 /api/v3/strategy?market=${market} 无落盘研究轮` : envError(sweepEnv, `GET /api/v3/ml/sweep?market=${market} 未返回有效参数网格`)} />
            <MarketNote source={`GET /api/v3/ml/sweep + GET /api/v3/strategy（均带 market=${market}）`} />
          </Space>
        )
        : (
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            <Table size="small" rowKey="key" pagination={false} columns={columns} dataSource={rows} scroll={{ x: 1000 }}
              rowClassName={(record) => (record.key === selectedKey ? "ant-table-row-selected" : "")} />
            <MarketNote source={`GET /api/v3/ml/sweep + GET /api/v3/strategy（market=${market}）`} extra={`候选 ${rows.length} 条`} />
          </Space>
        )}
    </ProCard>
  );
}

/* ── ⑦ 无数据源清单 ─────────────────────────────────────────────────────── */
function NoSourceCard() {
  const items = [
    { what: "分层年化多空（分层组合收益）", why: "factors/matrix 只返回横截面 z 值与 RankIC，未返回分层组合净值序列" },
    { what: "换手率", why: "factors/matrix 与 ml/backtest 都未返回换手/成交额字段" },
    { what: "因子相关性（相关矩阵）", why: "接口未返回因子两两相关矩阵；逐因子请求也不含该字段" },
    { what: "基准指数（相对基准的超额/信息比率曲线）", why: "ml/backtest 为单标的策略净值，未返回基准净值序列；/api/v3/risk/analytics 的 benchmark 是组合口径，不可混用" },
    { what: "置信度（IC 显著性/t 值/置信区间）", why: "factors/matrix 只给 meanIc/ir/observations/latestIc，未返回显著性检验量" },
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
export default function 策略与因子Page() {
  const { market } = useMarket();
  const strategy = useV3("strategy", { market });
  const baseFactors = useV3("factors/matrix", { market });
  const [ticker, setTicker] = React.useState(null);
  const [extraIc, setExtraIc] = React.useState({});
  const [loadingIc, setLoadingIc] = React.useState(false);
  const [sweepKey, setSweepKey] = React.useState(0);
  const [sweepEnv, setSweepEnv] = React.useState(null);
  const [sweepLoading, setSweepLoading] = React.useState(false);
  const [backtest, setBacktest] = React.useState(null);
  const [backtestBusy, setBacktestBusy] = React.useState(false);
  const [candidate, setCandidate] = React.useState(null);

  /* 回测 / 扫描标的 = 研究轮标的池首只（与 binder 同一口径），但**只认属于当前市场的研究轮**：
     研究轮自带 market 时以它为准，否则按标的池首只的市场前缀判断。该市场没有可用研究轮时退回
     该市场默认标的（真实代码），不回退到别的市场；研究轮尚未返回时保持未选（不发起扫描请求）。 */
  React.useEffect(() => {
    const run = OK(strategy.value) ? (strategy.value.run || null) : null;
    const universe = asArray(run && run.universe);
    const runMarket = run && run.market
      ? String(run.market).toUpperCase()
      : (universe.length ? tickerMarket(universe[0]) : null);
    const usable = universe.length > 0 && (!runMarket || runMarket === market);
    if (usable) {
      setTicker(String(universe[0]));
      return;
    }
    setTicker(strategy.value ? marketTicker(market) : null);
  }, [strategy.value, market]);

  /* 参数网格：真实 GET /api/v3/ml/sweep（只读，显式带 market）。 */
  const loadSweep = React.useCallback(async (target) => {
    if (!target) return;
    setSweepLoading(true);
    try {
      const url = `/api/v3/ml/sweep?ticker=${encodeURIComponent(target)}&windows=10,20,30,60&rebalance=5,10,20&limit=500&market=${encodeURIComponent(market)}`;
      const headers = {};
      try {
        const token = window.localStorage.getItem("trading_token");
        if (token) headers.Authorization = `Bearer ${token}`;
      } catch { /* 未鉴权 */ }
      const response = await fetch(url, { headers });
      const body = await response.json().catch(() => null);
      setSweepEnv(body);
    } catch (error) {
      setSweepEnv({ ok: false, error: { code: "net", message: String((error && error.message) || error) } });
    } finally {
      setSweepLoading(false);
    }
  }, [market]);

  React.useEffect(() => {
    if (ticker) loadSweep(ticker);
  }, [ticker, sweepKey, loadSweep]);

  /* 基线 IC（factors/matrix 默认因子 mom_20）+ 首屏追加 4 个因子（串行，避免堆叠 5–10s 计算）。 */
  React.useEffect(() => {
    if (!OK(baseFactors.value) || !baseFactors.value.ic || !baseFactors.value.ic.ok) return;
    let cancelled = false;
    (async () => {
      setLoadingIc(true);
      const collected = {};
      for (const name of IC_FACTORS) {
        if (cancelled) break;
        // eslint-disable-next-line no-await-in-loop —— 串行是刻意的：每次因子计算 5–10s，并发会打爆服务端
        const env = await (async () => {
          try {
            const headers = {};
            try {
              const token = window.localStorage.getItem("trading_token");
              if (token) headers.Authorization = `Bearer ${token}`;
            } catch { /* 未鉴权 */ }
            const response = await fetch(`/api/v3/factors/matrix?factor=${encodeURIComponent(name)}&market=${encodeURIComponent(market)}`, { headers });
            return await response.json();
          } catch (error) {
            return { ok: false, error: { code: "net", message: String((error && error.message) || error) } };
          }
        })();
        if (env && env.ok && env.ic) collected[name] = env.ic;
      }
      if (!cancelled) {
        setExtraIc((prev) => ({ ...prev, ...collected }));
        setLoadingIc(false);
      }
    })();
    return () => { cancelled = true; };
  }, [baseFactors.value, market]);

  const loadedFactors = React.useMemo(() => {
    const baseIc = OK(baseFactors.value) && baseFactors.value.ic && baseFactors.value.ic.ok
      && Number(baseFactors.value.ic.observations) > 0 ? baseFactors.value.ic : null;
    const merged = { ...extraIc };
    if (baseIc) merged[baseIc.factor || "mom_20"] = baseIc;
    return merged;
  }, [baseFactors.value, extraIc]);

  /* 「补齐因子 IC」：对尚未取到的因子再逐个人工触发（仍串行、封顶 IC_MAX）。 */
  const loadMoreIc = async () => {
    if (!OK(baseFactors.value)) return;
    const factors = asArray(baseFactors.value.matrix && baseFactors.value.matrix.factors);
    const pending = factors
      .filter((name) => !(loadedFactors[name] && loadedFactors[name].ok && Number(loadedFactors[name].observations) > 0))
      .slice(0, 4);
    if (pending.length === 0) return;
    setLoadingIc(true);
    const collected = {};
    for (const name of pending) {
      // eslint-disable-next-line no-await-in-loop —— 串行是刻意的（每次 5–10s）
      const env = await (async () => {
        try {
          const headers = {};
          try {
            const token = window.localStorage.getItem("trading_token");
            if (token) headers.Authorization = `Bearer ${token}`;
          } catch { /* 未鉴权 */ }
          const response = await fetch(`/api/v3/factors/matrix?factor=${encodeURIComponent(name)}&market=${encodeURIComponent(market)}`, { headers });
          return await response.json();
        } catch (error) {
          return { ok: false, error: { code: "net", message: String((error && error.message) || error) } };
        }
      })();
      if (env && env.ok && env.ic) collected[name] = env.ic;
    }
    setExtraIc((prev) => ({ ...prev, ...collected }));
    setLoadingIc(false);
  };

  /* 回测：只由人工点击（「提交回测」/「跑该参数回测」）触发。 */
  const runBacktest = async (row) => {
    if (!row || row.kind !== "sweep") return;
    const target = ticker || marketTicker(market);
    setCandidate(row);
    setBacktestBusy(true);
    // 人工点击才发起；载荷显式带 market（回测标的与市场口径一致）
    const env = await postV3("ml/backtest", { ticker: target, window: row.window, rebalanceDays: row.rebalanceDays, limit: 500, market });
    setBacktest({ env, at: new Date().toISOString(), row });
    setBacktestBusy(false);
  };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <Alert
        type="info"
        showIcon
        message={`策略与因子 · 当前市场：${marketLabel(market)} · 数据全部来自本服务 /api/v3/* 实时接口`}
        description={
          <Text type="secondary" style={{ fontSize: 12 }}>
            本页所有受市场影响的请求都显式带 <Text code>market={market}</Text>（研究流水线 / 因子矩阵 / 参数扫描 / 回测），
            研究流水线标的池随市场，绝不把「全部市场」当作一档。写动作只有两个且都只由人工点击发起：
            <Text code>POST /api/v3/strategy/run</Text>（跑研究流水线，只出调仓建议提案，<Text strong>不下单</Text>）与{" "}
            <Text code>POST /api/v3/ml/backtest</Text>（PIT 回测取净值序列）。加载与刷新阶段绝不自动写。
            该市场无自选池/无持仓时服务端返回 <Text code>market/no-universe</Text>，本页原样展示原因，不假装跑过。
          </Text>
        }
      />
      <Block title="研究流水线">
        <PipelineCard env={strategy.value} refresh={strategy.refresh} ticker={ticker} market={market} />
      </Block>
      <Block title="调仓提案">
        <ProposalsCard env={strategy.value} />
      </Block>
      <Block title="因子库">
        <FactorLibraryCard baseEnv={baseFactors.value} onLoadMore={loadMoreIc} loadingMore={loadingIc} loadedFactors={loadedFactors} />
      </Block>
      <Row gutter={[12, 12]}>
        <Col xs={24} xl={12}>
          <Block title="参数扫描热力图">
            <HeatmapCard env={sweepEnv} onRescan={() => setSweepKey((value) => value + 1)} scanning={sweepLoading} ticker={ticker} />
          </Block>
        </Col>
        <Col xs={24} xl={12}>
          <Block title="策略候选">
            <CandidatesCard sweepEnv={sweepEnv} strategyEnv={strategy.value} selectedKey={candidate ? candidate.key : null} onRun={runBacktest} />
          </Block>
        </Col>
      </Row>
      <Block title="回测曲线">
        <BacktestCard result={backtest} busy={backtestBusy} onRun={runBacktest} candidate={candidate} ticker={ticker} />
      </Block>
      <Block title="无数据源项">
        <NoSourceCard />
      </Block>
      <Text type="secondary" style={{ fontSize: 12 }}>
        数据来源：/api/v3/strategy（研究流水线 PDAT→PET）· /api/v3/factors/matrix（因子矩阵 + 逐因子 IC）· /api/v3/ml/sweep（参数网格真实回测）·
        /api/v3/ml/backtest（PIT 净值回测）· /api/v3/risk（风控阈值口径）。页面不含占位数字。
        {" "}<Link href="/v3/strategy.html">对照设计稿原样版</Link>
      </Text>
    </Space>
  );
}
