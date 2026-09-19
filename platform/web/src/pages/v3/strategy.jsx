// V3「策略与因子」页（Ant Design Pro）。设计稿对应 od-quant-harness-platform/strategy.html。
//
// 数据来源（全部实测，缺失即显式「无数据源」，不造占位数字）：
//   * 研究流水线 + 调仓建议提案：GET /api/v3/strategy（POST /api/v3/strategy/run 触发新一轮）
//   * 因子库横截面 z 矩阵 + IC/IR：GET /api/v3/factors/matrix?tickers=
//   * 参数扫描热力图：GET /api/v3/ml/sweep?ticker=&windows=&rebalance=
//   * 回测曲线：POST /api/v3/ml/backtest（按钮触发，本页唯一写动作；只跑回测，不下单）
//   * 策略注册表：当前服务无数据源（无策略注册表工具）→ 显式标注，不列假策略
//
// 图表复用仓库自研 canvas 组件：HeatmapChart（扫描热力图）、LineChart（回测净值）、
// VBarChart（IC 序列）——见 src/charts/。
import React from "react";
import { Alert, Button, Card, Col, Input, InputNumber, Row, Space, Statistic, Steps, Tag, Typography } from "antd";
import { ProCard, ProTable } from "@ant-design/pro-components";
import { callV3, useV3 } from "../../services/v3api.js";
import { num } from "../../services/format.jsx";
import { HeatmapChart } from "../../charts/heatmap.jsx";
import { LineChart } from "../../charts/line.jsx";
import { VBarChart } from "../../charts/bars.jsx";

const { Text, Title } = Typography;

/** 有限数值或 null（不把缺失当成 0）。 */
function finiteOf(value, digits = 2) {
  const n = Number(value);
  if (!Number.isFinite(n)) return null;
  return Number(n.toFixed(digits));
}

function textOf(value, digits = 2) {
  const n = finiteOf(value, digits);
  return n === null ? "—" : String(n);
}

function pctText(value, digits = 2) {
  const n = finiteOf(value, digits);
  return n === null ? "—" : `${n}%`;
}

function moneyText(value) {
  const n = finiteOf(value, 2);
  return n === null ? "—" : `¥${n.toLocaleString("zh-CN")}`;
}

/** 研究流水线五阶段（PDAT→PAAT→PCPT→PRT→PET）的固定定义与阶段摘要口径。 */
const STAGE_DEFS = [
  { key: "PDAT", title: "数据准备", desc: "行情/基础数据落库" },
  { key: "PAAT", title: "因子分析", desc: "横截面因子与评分" },
  { key: "PCPT", title: "规则候选", desc: "多头候选与减仓名单" },
  { key: "PRT", title: "回测验证", desc: "权重上限与组合约束" },
  { key: "PET", title: "评估产出", desc: "调仓建议提案" },
];

/** 阶段摘要：只陈述服务端实际给出的字段，缺字段显示「—」而不是编造进度。 */
function stageSummary(key, stage) {
  if (!stage || typeof stage !== "object") return "服务端未返回该阶段";
  if (key === "PDAT") {
    return `universe ${textOf(stage.universe, 0)} · bars ${textOf(stage.bars, 0)} · 错误 ${Array.isArray(stage.errors) ? stage.errors.length : "—"}`;
  }
  if (key === "PAAT") {
    return `已分析 ${textOf(stage.analyzed, 0)} · 带因子 ${textOf(stage.withFactors, 0)} · 评分源 ${stage.scoreSource ?? "—"}`;
  }
  if (key === "PCPT") {
    return `多头 ${Array.isArray(stage.longs) ? stage.longs.length : "—"} · 减仓 ${Array.isArray(stage.reduces) ? stage.reduces.length : "—"}`;
  }
  if (key === "PRT") {
    return `单名权重 ${pctText(stage.weightPctPerName)} · 是否封顶 ${stage.capped === undefined ? "—" : stage.capped ? "是" : "否"}`;
  }
  if (key === "PET") return `提案 ${Array.isArray(stage.proposals) ? stage.proposals.length : "—"} 条`;
  return "—";
}

/** 提案动作 → 展示色（买入/加仓红、卖出/减仓绿——与全站红涨绿跌一致）。 */
function actionTag(action) {
  const text = String(action ?? "—");
  const lower = text.toLowerCase();
  const color = /buy|买入|加仓|increase/.test(lower) ? "red"
    : /sell|卖出|减仓|reduce/.test(lower) ? "green" : "default";
  return <Tag color={color}>{text}</Tag>;
}

function riskTag(level) {
  const text = String(level ?? "—");
  const lower = text.toLowerCase();
  const color = /high|高/.test(lower) ? "red" : /mid|中/.test(lower) ? "gold" : /low|低/.test(lower) ? "green" : "default";
  return <Tag color={color}>{text}</Tag>;
}

/** 扫描网格 → 热力图矩阵：行=rebalanceDays、列=window（缺失组合置 null，画成灰格不画 0）。 */
function buildSweepMatrix(grid) {
  const rows = Array.isArray(grid) ? grid : [];
  const windows = [...new Set(rows.map((cell) => cell?.window).filter((v) => v !== undefined && v !== null))]
    .sort((a, b) => Number(a) - Number(b));
  const rebalances = [...new Set(rows.map((cell) => cell?.rebalanceDays).filter((v) => v !== undefined && v !== null))]
    .sort((a, b) => Number(a) - Number(b));
  const matrix = rebalances.map((rebalance) => windows.map((window) => {
    const hit = rows.find((cell) => Number(cell?.window) === Number(window) && Number(cell?.rebalanceDays) === Number(rebalance));
    return hit ? finiteOf(hit.sharpe, 2) : null;
  }));
  return { windows, rebalances, matrix };
}

export default function V3策略因子Page() {
  // ── 流水线 + 提案 ────────────────────────────────────────────────────────────
  const strategy = useV3("strategy", {});
  const run = strategy.value?.run ?? null;
  const stages = run?.stages ?? null;
  const proposals = Array.isArray(strategy.value?.proposals) ? strategy.value.proposals
    : Array.isArray(run?.stages?.PET?.proposals) ? run.stages.PET.proposals : [];
  const [actionState, setActionState] = React.useState({ busy: false, ok: null, message: "" });

  const triggerRun = React.useCallback(async () => {
    setActionState({ busy: true, ok: null, message: "" });
    try {
      const body = await callV3("strategy/run", {}, { refresh: true });
      const envelope = body?.value ?? body ?? {};
      if (envelope.ok === false) throw new Error(envelope.error?.message || envelope.error?.code || "研究轮返回 ok=false");
      setActionState({ busy: false, ok: true, message: `已触发研究轮（topN=2）· ${run?.asOf ?? "asOf 见刷新后"}` });
      strategy.refresh();
    } catch (error) {
      setActionState({ busy: false, ok: false, message: String(error.message || error) });
    }
  }, [strategy.refresh, run?.asOf]);

  // ── 因子矩阵 + IC/IR ────────────────────────────────────────────────────────
  const [tickers, setTickers] = React.useState("SH.600519,SZ.300750,SH.601318");
  const [tickerInput, setTickerInput] = React.useState(tickers);
  const factors = useV3("factors/matrix", { tickers }, [tickers]);
  const matrixValue = factors.value?.matrix ?? null;
  const matrixRows = Array.isArray(matrixValue?.tickers) ? matrixValue.tickers : [];
  const matrixFactors = Array.isArray(matrixValue?.factors) ? matrixValue.factors : [];
  const matrixGrid = Array.isArray(matrixValue?.matrix) ? matrixValue.matrix : [];
  const ic = factors.value?.ic ?? null;
  const icPoints = Array.isArray(ic?.points) ? ic.points : [];
  const icBars = icPoints.map((point, index) => ({
    label: String(point?.t ?? point?.at ?? index + 1),
    value: finiteOf(point?.ic ?? point?.value, 3),
  }));

  // ── 参数扫描热力图 ──────────────────────────────────────────────────────────
  const [sweepTicker, setSweepTicker] = React.useState("SH.600519");
  const [sweepWindows, setSweepWindows] = React.useState("10,20,30,60");
  const [sweepRebalance, setSweepRebalance] = React.useState("5,10,20");
  const sweep = useV3("ml/sweep", { ticker: sweepTicker, windows: sweepWindows, rebalance: sweepRebalance },
    [sweepTicker, sweepWindows, sweepRebalance]);
  const sweepGrid = Array.isArray(sweep.value?.grid) ? sweep.value.grid : [];
  const sweepShape = buildSweepMatrix(sweepGrid);
  const best = sweep.value?.best ?? null;

  // ── 回测曲线（按钮触发） ─────────────────────────────────────────────────────
  const [btTicker, setBtTicker] = React.useState("SH.600519");
  const [btWindow, setBtWindow] = React.useState(20);
  const [btRebalance, setBtRebalance] = React.useState(5);
  const [btState, setBtState] = React.useState({ loading: false, error: "", value: null });
  const runBacktest = React.useCallback(async () => {
    setBtState({ loading: true, error: "", value: null });
    try {
      const body = await callV3("ml/backtest", { ticker: btTicker, window: btWindow, rebalanceDays: btRebalance, limit: 500 }, { refresh: true });
      const envelope = body?.value ?? body ?? {};
      if (envelope.ok === false) throw new Error(envelope.error?.message || envelope.error?.code || "回测返回 ok=false");
      setBtState({ loading: false, error: "", value: envelope });
    } catch (error) {
      setBtState({ loading: false, error: String(error.message || error), value: null });
    }
  }, [btTicker, btWindow, btRebalance]);
  const btMetrics = btState.value?.metrics ?? null;
  const equity = Array.isArray(btState.value?.equity) ? btState.value.equity : [];
  // LineChart 只吃 [{t,v}]：净值序列取 value 字段；点不足 2 个时组件自己画「暂无序列数据」
  const equityPoints = equity.map((point) => ({ t: point?.t, v: finiteOf(point?.value, 4) }));

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {strategy.error ? <Alert type="error" showIcon message="研究流水线取数失败" description={strategy.error} /> : null}
      {strategy.value?.note ? <Alert type="warning" showIcon message="服务端提示" description={String(strategy.value.note)} /> : null}

      <ProCard title="研究流水线 PDAT → PAAT → PCPT → PRT → PET" bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>{`asOf ${run?.asOf ?? "—"} · universe ${textOf(run?.universe, 0)}`}</Text>}>
        {run ? (
          <Steps size="small" responsive
            items={STAGE_DEFS.map((stage) => ({
              title: `${stage.key} ${stage.title}`,
              description: <Text type="secondary" style={{ fontSize: 12 }}>{stageSummary(stage.key, stages?.[stage.key])}</Text>,
              status: stages?.[stage.key] ? "finish" : "wait",
            }))} />
        ) : (
          <Space direction="vertical" size={8} style={{ width: "100%" }}>
            <Text type="secondary">{strategy.loading ? "加载中…" : "服务未返回本轮 run（GET /api/v3/strategy 的 run 为空）"}</Text>
            <Steps size="small" responsive
              items={STAGE_DEFS.map((stage) => ({ title: `${stage.key} ${stage.title}`, description: <Text type="secondary" style={{ fontSize: 12 }}>{stage.desc}</Text>, status: "wait" }))} />
          </Space>
        )}
        <Space style={{ marginTop: 12 }} wrap>
          <Button type="primary" loading={actionState.busy} onClick={triggerRun}>触发新一轮（topN=2）</Button>
          <Button onClick={strategy.refresh} disabled={strategy.loading}>刷新</Button>
          <Text type="secondary" style={{ fontSize: 12 }}>触发等于运行研究轮（POST /api/v3/strategy/run）；本页与平台均不下单。</Text>
        </Space>
        {actionState.message ? (
          <Alert style={{ marginTop: 8 }} showIcon type={actionState.ok ? "success" : "error"} message={actionState.message} />
        ) : null}
      </ProCard>

      <ProCard title="调仓建议提案" bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>{`来源：/api/v3/strategy 的 proposals · 共 ${proposals.length} 条`}</Text>}>
        <ProTable
          rowKey={(row, index) => `${row?.ticker ?? "?"}-${row?.action ?? "?"}-${index}`}
          search={false} options={false} pagination={false} size="small"
          loading={strategy.loading} dataSource={proposals}
          locale={{ emptyText: strategy.loading ? "加载中…" : "服务未返回提案（PCPT/PRT/PET 阶段可能无候选）" }}
          columns={[
            { title: "标的", dataIndex: "ticker", render: (v) => <Text strong>{v ?? "—"}</Text> },
            { title: "动作", dataIndex: "action", render: (v) => actionTag(v) },
            { title: "目标权重", dataIndex: "targetWeightPct", align: "right", render: (v) => pctText(v) },
            { title: "风险等级", dataIndex: "riskLevel", render: (v) => riskTag(v) },
            { title: "依据", dataIndex: "basis", render: (v) => <Text type="secondary">{v ?? "—"}</Text> },
          ]} />
        <Text type="secondary" style={{ fontSize: 12 }}>
          提案是研究产出，不是订单；执行入口在工作台 Web（plan_execute + 人工确认，live 需口令）。
        </Text>
      </ProCard>

      <Row gutter={[12, 12]}>
        <Col xs={24} xl={14}>
          <ProCard title="因子库 · 横截面 z 矩阵" bordered
            extra={<Text type="secondary" style={{ fontSize: 12 }}>{`as_of ${matrixValue?.as_of ?? "—"}`}</Text>}>
            {factors.error ? <Alert type="error" showIcon message="因子矩阵取数失败" description={factors.error} /> : null}
            <Space wrap style={{ marginBottom: 8 }}>
              <Input style={{ width: 320 }} value={tickerInput} onChange={(event) => setTickerInput(event.target.value)}
                placeholder="标的，逗号分隔（如 SH.600519,SZ.300750）" />
              <Button onClick={() => setTickers(tickerInput.trim())} loading={factors.loading}>查询</Button>
            </Space>
            {matrixGrid.length && matrixFactors.length ? (
              <HeatmapChart rowLabels={matrixRows} colLabels={matrixFactors} matrix={matrixGrid} />
            ) : (
              <Alert type="info" showIcon message="无数据源 / 无矩阵"
                description={factors.loading ? "加载中…" : "GET /api/v3/factors/matrix 未返回 matrix.tickers/factors/matrix（后端该端点尚未实现或所选标的无因子数据）。"} />
            )}
          </ProCard>
        </Col>
        <Col xs={24} xl={10}>
          <ProCard title="因子 IC / IR" bordered>
            {ic ? (
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <Row gutter={[12, 12]}>
                  <Col span={8}><Statistic title="因子" value={ic.factor ?? "—"} valueStyle={{ fontSize: 14 }} /></Col>
                  <Col span={8}><Statistic title="观测数" value={textOf(ic.observations, 0)} /></Col>
                  <Col span={8}><Statistic title="最新 IC" value={textOf(ic.latestIc, 3)} /></Col>
                  <Col span={12}><Statistic title="均值 IC" value={textOf(ic.meanIc, 4)} /></Col>
                  <Col span={12}><Statistic title="IR" value={textOf(ic.ir, 2)} /></Col>
                </Row>
                {icBars.some((bar) => bar.value !== null) ? (
                  <VBarChart items={icBars} height={180} emptyText="IC 序列点无数值" />
                ) : (
                  <Text type="secondary">{`ic.points 共 ${icPoints.length} 个点，但均无数值 → 无数据源`}</Text>
                )}
              </Space>
            ) : (
              <Alert type="info" showIcon message="无 IC/IR 数据源"
                description={factors.loading ? "加载中…" : "GET /api/v3/factors/matrix 未返回 ic 字段（后端该端点尚未实现），本页不估算 IC。"} />
            )}
          </ProCard>
        </Col>
      </Row>

      <ProCard title="参数扫描 · 夏普热力图" bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>{sweepGrid.length ? `共 ${sweepGrid.length} 组参数` : "无数据源"}</Text>}>
        {sweep.error ? <Alert type="error" showIcon message="参数扫描取数失败" description={sweep.error} /> : null}
        <Space wrap style={{ marginBottom: 8 }}>
          <Input style={{ width: 180 }} addonBefore="标的" value={sweepTicker} onChange={(event) => setSweepTicker(event.target.value)} />
          <Input style={{ width: 220 }} addonBefore="窗口" value={sweepWindows} onChange={(event) => setSweepWindows(event.target.value)} />
          <Input style={{ width: 220 }} addonBefore="调仓" value={sweepRebalance} onChange={(event) => setSweepRebalance(event.target.value)} />
          <Button onClick={sweep.refresh} loading={sweep.loading}>重新扫描</Button>
        </Space>
        {sweepGrid.length ? (
          <>
            <HeatmapChart rowLabels={sweepShape.rebalances.map((v) => String(v))}
              colLabels={sweepShape.windows.map((v) => String(v))} matrix={sweepShape.matrix} />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {`行 = rebalanceDays ↓ · 列 = window → · 单元为夏普（缺失组合画灰格，不当 0）`}
              {best ? ` · best：window ${textOf(best.window, 0)} / rebalance ${textOf(best.rebalanceDays, 0)} / 夏普 ${textOf(best.sharpe, 2)}` : ""}
            </Text>
          </>
        ) : (
          <Alert type="info" showIcon message="无数据源 / 无扫描结果"
            description={sweep.loading ? "加载中…" : "GET /api/v3/ml/sweep 未返回 grid（后端该端点尚未实现），本页不列假参数组合。"} />
        )}
      </ProCard>

      <ProCard title="回测曲线" bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>{btState.value ? `${btState.value.ticker ?? btTicker} · equity ${equity.length} 点` : "按钮触发 POST /api/v3/ml/backtest"}</Text>}>
        <Space wrap style={{ marginBottom: 8 }}>
          <Input style={{ width: 180 }} addonBefore="标的" value={btTicker} onChange={(event) => setBtTicker(event.target.value)} />
          <InputNumber addonBefore="窗口" min={2} value={btWindow} onChange={setBtWindow} />
          <InputNumber addonBefore="调仓" min={1} value={btRebalance} onChange={setBtRebalance} />
          <Button type="primary" loading={btState.loading} onClick={runBacktest}>运行回测</Button>
          <Text type="secondary" style={{ fontSize: 12 }}>limit 固定 500 日；只跑回测，不产生任何订单。</Text>
        </Space>
        {btState.error ? <Alert type="error" showIcon message="回测失败" description={btState.error} /> : null}
        {btMetrics ? (
          <Row gutter={[12, 12]} style={{ marginBottom: 8 }}>
            {[
              { title: "夏普", value: textOf(btMetrics.sharpe, 2) },
              { title: "年化收益", value: pctText(btMetrics.annReturnPct) },
              { title: "最大回撤", value: pctText(btMetrics.maxDrawdownPct) },
              { title: "胜率", value: pctText(btMetrics.winRatePct) },
              { title: "信号翻转", value: textOf(btMetrics.signalFlips, 0) },
              { title: "持仓/空仓日", value: `${textOf(btMetrics.heldDays, 0)} / ${textOf(btMetrics.flatDays, 0)}` },
              { title: "天数", value: textOf(btMetrics.days, 0) },
            ].map((item) => (
              <Col key={item.title} xs={12} sm={8} md={6} lg={4}>
                <Statistic title={item.title} value={item.value} valueStyle={{ fontSize: 16 }} />
              </Col>
            ))}
          </Row>
        ) : null}
        {equityPoints.length ? (
          <LineChart points={equityPoints} label={`${btState.value?.ticker ?? btTicker} 净值`} />
        ) : (
          <Alert type="info" showIcon message={btState.value ? "回测未返回净值序列" : "尚未运行回测"}
            description={btState.value ? "POST /api/v3/ml/backtest 返回的 equity 为空。" : "点击「运行回测」后在此绘制净值曲线（数据来自服务端实测）。"} />
        )}
      </ProCard>

      <Card size="small" bordered title="策略列表">
        <Alert type="info" showIcon message="无数据源：策略注册表"
          description={<Text type="secondary">
            当前服务没有策略注册表工具（无列出策略名/版本/回测摘要的接口），因此本页不列出任何策略行，
            也不使用设计稿中的占位策略数字。策略产出走研究流水线（PDAT→PET）的提案表，见上方。
          </Text>} />
      </Card>

      <Card size="small" bordered>
        <Title level={5} style={{ marginTop: 0 }}>说明</Title>
        <Text type="secondary" style={{ fontSize: 12 }}>
          数据来源：本服务 /api/v3/strategy、/api/v3/factors/matrix、/api/v3/ml/sweep、POST /api/v3/ml/backtest
          （与工作台共用同一 handle，无第二数据路径）。所有数值为服务端实测；取不到的区块显式标注「无数据源」并说明原因，
          不使用估算值或设计稿占位数字。
        </Text>
      </Card>
    </Space>
  );
}
