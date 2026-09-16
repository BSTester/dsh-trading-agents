// 因子页：横截面因子打分（factors）+ 因子 IC 序列（ic）+ 单标的财报质量（quality）。
// 字段依据（页面每个取值路径均可指到源码行）：
//   factors → plugins/workbench/src/analytics.js factors()（tickers 2..8，window 默认 250）
//     → plugins/workbench/python/factors.py snapshot() →
//     {tickers:[按综合分排序], rows:[{ticker, factors{7个价量键+close+可用估值键},
//     as_of, z{...}, score, rank}], factors:[因子键列表（含估值键）], sources:[],
//     failures{标的:原因}, window, note}；score 为 None 的行不进 rows（composite 过滤）。
//   ic → analytics.js ic()（tickers 3..8；factor/forward/window 默认 mom_20/5/250）
//     → factors.py ic_series() → {factor, tickers, forward_days, points:[{t, ic}],
//     count, mean_ic, ic_std, icir, positive_ratio, note}
//   quality → analytics.js quality() → plugins/workbench/python/quality.py collect() →
//     {ticker, symbol, as_of, source, currency, periods:[...], latest:{fiscal_year,
//     financial_type, period_end, currency, accounting_standards, revenue, gross_profit,
//     operating_profit, net_profit, research_development, diluted_eps, dividend_per_share,
//     gross_margin, operating_margin, net_margin, rd_ratio, effective_tax_rate,
//     revenue_yoy, net_profit_yoy}, returns, unavailable:[{key, label, reason}], note}
//     注意：quality.py 的比率（毛利率等）已 ×100，页原样加 %；ROE/ROA 同（fundamentals.py
//     returns_of 已 ×100）。不可得即 "—"，不估算。
// 因子键中文标签沿用旧客户端 FACTOR_LABELS（client.js L1388-1392）；没有标签的键
// （如 pe_ttm_pct）按原始键名展示，不自造含义；financial_type 原码展示（quality.py
// 明确说明服务端未给出其枚举含义）。
import React from "react";
import { Alert, Card, Col, Descriptions, Input, Row, Select, Space, Statistic, Table, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";
import { num } from "../services/format.jsx";
import { LineChart } from "../charts/line.jsx";
import { sentimentHeadline, sentimentRows } from "../services/sentiment.js";

// IC 检验当前只覆盖价量因子（同旧客户端 IC_FACTORS，client.js L1385）。
const FACTORS = ["mom_20", "mom_60", "vol_20", "trend", "rsi_14", "liq_ratio", "mdd_60"];
// 默认值取 analytics.js ic() 的 factor 默认（"mom_20"）。
const DEFAULT_FACTOR = "mom_20";
const FACTOR_LABELS = {
  mom_20: "动量20", mom_60: "动量60", vol_20: "波动率", trend: "趋势偏离",
  rsi_14: "RSI14", liq_ratio: "量能比", mdd_60: "最大回撤",
  pe_ttm: "PE(TTM)", pb: "PB", peg: "PEG", ps: "PS",
};

/** 因子原始取值展示：旧客户端 Number(value).toFixed(3)、缺失 —；缺失统一走 num。 */
function factorCell(value) {
  return num(value, 3);
}

/** quality.py 输出的百分数字段（已 ×100）：原样加 %，缺失 —。 */
function pctCell(value) {
  if (value === null || value === undefined) return "—";
  return `${Number(value).toFixed(2)}%`;
}

function parseWatchlist(text) {
  return String(text).split(/[,，\s]+/).map((part) => part.trim().toUpperCase()).filter(Boolean);
}

/** 因子打分表：行 = 标的（factors.py composite 已按综合分排序），列 = 各因子原始取值。 */
function FactorsTable({ snap }) {
  const rows = snap.value?.rows ?? [];
  // 列顺序 = 输出侧 factors 键列表（价量 + 估值），估值键无标签时用原始键名；
  // close 不在 factors 键列表里（factors.py factor_values 额外给出），单独追加。
  const keys = [...(snap.value?.factors ?? []), "close"];
  const columns = [
    { title: "排名", key: "rank", width: 64, render: (_f, row) => row.rank ?? "—" },
    { title: "标的", key: "ticker", render: (_f, row) => row.ticker ?? "—" },
    { title: "综合分", key: "score", align: "right",
      render: (_f, row) => (row.score === null || row.score === undefined
        ? "—" : `${row.score >= 0 ? "+" : ""}${row.score}`) },
    ...keys.map((key) => ({
      title: FACTOR_LABELS[key] ?? key,
      key,
      align: "right",
      render: (_f, row) => factorCell(row.factors?.[key]),
    })),
    { title: "因子日", key: "as_of", render: (_f, row) => row.as_of ?? "—" },
  ];
  return (
    <Table size="small" dataSource={rows}
      rowKey={(row) => row.ticker}
      pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
      scroll={{ x: "max-content" }}
      locale={{ emptyText: snap.loading ? "因子计算中…（每只标的取 250 日日线）" : "暂无因子数据。" }}
      columns={columns} />
  );
}

/** 单标的财报质量（quality.py collect 输出；不可得即 —，不估算）。 */
function QualityDescriptions({ quality }) {
  const data = quality.value;
  const latest = data?.latest ?? {};
  const returns = data?.returns ?? {};
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      <Descriptions size="small" bordered column={{ xs: 1, sm: 2, md: 3 }}>
        <Descriptions.Item label="报告期">{latest.period_end ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="财年">{latest.fiscal_year ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="报表类型码">{latest.financial_type ?? "—"}（原码，含义未公开）</Descriptions.Item>
        <Descriptions.Item label="币种">{data?.currency ?? latest.currency ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="会计准则">{latest.accounting_standards ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="财报期间数">{data?.periods?.length ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="营业收入">{num(latest.revenue)}</Descriptions.Item>
        <Descriptions.Item label="毛利润">{num(latest.gross_profit)}</Descriptions.Item>
        <Descriptions.Item label="营业利润">{num(latest.operating_profit)}</Descriptions.Item>
        <Descriptions.Item label="净利润">{num(latest.net_profit)}</Descriptions.Item>
        <Descriptions.Item label="研发费用">{num(latest.research_development)}</Descriptions.Item>
        <Descriptions.Item label="稀释 EPS">{latest.diluted_eps ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="每股股息">{latest.dividend_per_share ?? "—"}</Descriptions.Item>
        <Descriptions.Item label="毛利率">{pctCell(latest.gross_margin)}</Descriptions.Item>
        <Descriptions.Item label="营业利润率">{pctCell(latest.operating_margin)}</Descriptions.Item>
        <Descriptions.Item label="净利率">{pctCell(latest.net_margin)}</Descriptions.Item>
        <Descriptions.Item label="研发占比">{pctCell(latest.rd_ratio)}</Descriptions.Item>
        <Descriptions.Item label="实际税率">{pctCell(latest.effective_tax_rate)}</Descriptions.Item>
        <Descriptions.Item label="营收同比">{pctCell(latest.revenue_yoy)}</Descriptions.Item>
        <Descriptions.Item label="净利同比">{pctCell(latest.net_profit_yoy)}</Descriptions.Item>
        <Descriptions.Item label="净资产收益率 ROE">{pctCell(returns.roe)}</Descriptions.Item>
        <Descriptions.Item label="总资产收益率 ROA">{pctCell(returns.roa)}</Descriptions.Item>
      </Descriptions>
      {returns.available && (
        <Typography.Text type="secondary">
          ROE/ROA 来源 {returns.source ?? "—"} · 权益期 {returns.balance_period ?? "—"} ·
          利润期 {returns.income_period ?? "—"}（富途无资产负债表接口，按渠道优先级落到备用源）
        </Typography.Text>)}
      {(data?.unavailable ?? []).map((row) => (
        <Typography.Text key={row.key} type="warning">
          无法提供：{row.label}（{row.reason}）
        </Typography.Text>))}
      {data?.note && <Typography.Text type="secondary">{data.note}</Typography.Text>}
    </Space>
  );
}

export default function FactorsPage() {
  const [input, setInput] = React.useState("");
  const [watchlist, setWatchlist] = React.useState([]);
  const [factor, setFactor] = React.useState(DEFAULT_FACTOR);
  const tickers = watchlist;
  // 服务端校验：factors 需 2..8 个标的（analytics.js factors()）；
  // ic 需 3..8（ic()）；quality 单标的。条件不满足时 payload 为 null 跳过请求。
  const snap = useEndpoint("factors", tickers.length >= 2 ? { tickers, window: 250 } : null, [tickers.join(",")]);
  const ic = useEndpoint("ic", tickers.length >= 3 ? { tickers, factor, forward: 5, window: 250 } : null,
    [tickers.join(","), factor]);
  const quality = useEndpoint("quality", tickers.length === 1 ? { ticker: tickers[0] } : null, [tickers.join(",")]);
  // WP11 任务 3：情绪快照采集摘要（不带 symbol 的形态——采集是流水线事实，与关注池无关）
  const sentiment = useEndpoint("sentiment-history", {}, []);
  const sentimentSummary = sentiment.value?.summary ?? null;
  const icPoints = (ic.value?.points ?? []).map((point) => ({ t: point.t, v: point.ic }));
  const failures = Object.entries(snap.value?.failures ?? {});
  return (
    <Card title="因子" extra={(
      <Space>
        <Input placeholder="关注池，逗号分隔 2..8 个，如 SH.600519,SH.600036" style={{ width: 320 }}
          value={input} aria-label="关注池（逗号分隔）"
          onChange={(event) => setInput(event.target.value)}
          onPressEnter={() => { setWatchlist(parseWatchlist(input)); }} />
        <Select value={factor} onChange={setFactor} style={{ width: 130 }}
          options={FACTORS.map((key) => ({ value: key, label: FACTOR_LABELS[key] ?? key }))} />
      </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Card type="inner" title="情绪快照采集"
          extra={(<Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {sentimentHeadline(sentimentSummary)}
          </Typography.Text>)}>
          {sentiment.error && (
            <Alert type="error" showIcon message={`情绪快照读取失败：${sentiment.error}`} />)}
          <Table size="small" rowKey="source" pagination={false}
            dataSource={sentimentRows(sentimentSummary)}
            columns={[
              { title: "来源", dataIndex: "source" },
              { title: "条数", dataIndex: "count", width: 90 },
              { title: "状态", key: "present", width: 90,
                render: (_value, row) => (row.present ? "在场" : "缺席") },
            ]} />
          <Typography.Text type="secondary">
            每交易日收盘作业链自动采集落库（原始渠道文本，不打分）；情绪只作研究参考，不参与信号计算。
          </Typography.Text>
        </Card>
        {tickers.length === 0 && (
          <Typography.Text type="secondary">
            输入关注池后回车加载：≥2 个标的做横截面打分，≥3 个加做 IC 检验，恰好 1 个只看财报质量。
          </Typography.Text>)}
        {tickers.length === 1 && (
          <Typography.Text type="secondary">
            单标的（{tickers[0]}）：横截面打分与 IC 需要至少 2/3 个标的，本页仅展示财报质量。
          </Typography.Text>)}

        {tickers.length >= 2 && (
          <Card type="inner" title="因子打分与排序"
            extra={snap.value && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                窗口 {snap.value.window} 个交易日
              </Typography.Text>)}>
            {snap.error && <Alert type="error" showIcon message={`因子读取失败：${snap.error}`} />}
            <FactorsTable snap={snap} />
            {failures.length > 0 && (
              <Typography.Text type="secondary">
                跳过：{failures.map(([key, reason]) => `${key}（${reason}）`).join("；")}
              </Typography.Text>)}
            {snap.value?.sources?.length > 0 && (
              <Typography.Text type="secondary">
                数据源：{snap.value.sources.join("、")}
              </Typography.Text>)}
            {snap.value?.note && (
              <Typography.Text type="secondary">{snap.value.note}</Typography.Text>)}
          </Card>)}

        {tickers.length >= 3 && (
          <Card type="inner" title={`因子 IC / ICIR（${FACTOR_LABELS[factor] ?? factor}）`}
            extra={ic.value && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                forward {ic.value.forward_days} 日 · 样本 {ic.value.count} 期
              </Typography.Text>)}>
            {ic.error && <Alert type="error" showIcon message={`IC 读取失败：${ic.error}`} />}
            {ic.loading && !ic.error && (
              <Typography.Text type="secondary">IC 计算中…（逐期横截面相关，标的越多越慢）</Typography.Text>)}
            {icPoints.length >= 2 && <LineChart points={icPoints} label="IC 序列" />}
            {icPoints.length === 1 && (
              <Typography.Text type="secondary">IC 序列仅 1 期，不足以绘制。</Typography.Text>)}
            {ic.value && (
              <Row gutter={16}>
                <Col span={4}><Statistic title="均值 IC" value={ic.value.mean_ic ?? "—"} /></Col>
                <Col span={4}><Statistic title="IC 标准差" value={ic.value.ic_std ?? "—"} /></Col>
                <Col span={4}><Statistic title="ICIR" value={ic.value.icir ?? "—"} /></Col>
                <Col span={4}><Statistic title="正 IC 占比" value={ic.value.positive_ratio ?? "—"} /></Col>
                <Col span={4}><Statistic title="样本期数" value={ic.value.count ?? "—"} /></Col>
              </Row>)}
            {ic.value?.note && (
              <Typography.Text type="secondary">{ic.value.note}</Typography.Text>)}
          </Card>)}

        {tickers.length === 1 && (
          <Card type="inner" title={`财报质量（${tickers[0]}）`}>
            {quality.error && <Alert type="error" showIcon message={`质量读取失败：${quality.error}`} />}
            {quality.loading && !quality.error && (
              <Typography.Text type="secondary">财报质量读取中…（富途财报原文计算）</Typography.Text>)}
            {quality.value && <QualityDescriptions quality={quality} />}
            {quality.value?.as_of && (
              <Typography.Text type="secondary">
                取数时间 {quality.value.as_of}（来源：{quality.value.source ?? "—"}）
              </Typography.Text>)}
          </Card>)}
      </Space>
    </Card>);
}
