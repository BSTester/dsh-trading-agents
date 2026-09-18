// 风险页：生效风控参数（risk）+ 持仓派生的组合风险（positions 的 counts 与 groups[].risk）。
// 字段依据（页面每个取值路径均可指到源码行）：
//   risk → plugins/workbench/python/analytics.py risk_view → {config, source, defaults?}；
//     配置无法解析时 {config: null, error, source}。config 键集合见
//     plugins/engine/python/risk_config.py DEFAULTS：risk_per_trade / stop_atr_mult /
//     max_positions / daily_loss_limit_pct / max_position_pct。
//   positions → plugins/workbench/python/positions.py：
//     counts（collect 输出）{accounts_checked, accounts_with_positions, positions}；
//     group.risk = account_risk → {positions, valued_positions, market_value,
//       top[{symbol, name, market_value, share_of_positions, share_of_assets, pl_ratio}],
//       max_share_of_positions, max_share_symbol, winners{count, pl_val}, losers{count, pl_val}, note}
// 页面只透出端点已有字段，不自算指标。
import React from "react";
import { Alert, Card, Descriptions, Space, Table, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";
import { num, pctOf, maskedAccount } from "../services/format.jsx";
import { useMarketFilter } from "../services/marketContext.jsx";
import { marketDisplay, marketLabelOf, viewGroups } from "../services/marketView.js";
import { isAllMarkets } from "../services/marketFilter.js";

const RISK_FIELDS = [
  { key: "risk_per_trade", label: "单笔风险占权益比例", format: "pct" },
  { key: "stop_atr_mult", label: "止损距离", format: "mult" },
  { key: "max_positions", label: "最大同时持仓数", format: "int" },
  { key: "daily_loss_limit_pct", label: "单日亏损熔断阈值", format: "pct" },
  { key: "max_position_pct", label: "单一标的最大仓位占权益比例", format: "pct" },
];

function formatRiskValue(value, format) {
  if (value === null || value === undefined) return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  // pct：配置值为比例（risk_config.py DEFAULTS，如 0.01），×100 换算收拢到 pctOf
  if (format === "pct") return pctOf(parsed);
  if (format === "mult") return `${parsed.toFixed(1)} × ATR`;
  return String(value);
}

/** 已知键按固定顺序与中文标签展示；未知新键按原始键名追加，不丢字段。 */
function riskItems(config) {
  const known = RISK_FIELDS.filter((field) => field.key in config);
  const extra = Object.keys(config)
    .filter((key) => !RISK_FIELDS.some((field) => field.key === key))
    .map((key) => ({ key, label: key, format: "raw" }));
  return [...known, ...extra];
}

function PortfolioRisk({ positions, market }) {
  const value = positions.value;
  const filtered = !isAllMarkets(market);
  // 全局市场筛选（客户端展示层，**不改请求参数**）：风险数据全部由 positions 的账户组派生，
  // 组上带 market（实测 sim 为数字 market_id）→ 按组筛；每组的「行」就是该账户（一行），
  // 故空态就是「筛后一个账户都没有」，与持仓页同一口径。
  const view = viewGroups(value?.groups, market, (group) => [group]);
  const accountRows = view.groups.map((group) => ({ group, risk: group.risk ?? {} }));
  const concentrationRows = view.groups.flatMap((group) =>
    (group.risk?.top ?? []).map((item) => ({
      ...item, account: group.account, accId: group.acc_id, groupMarket: group.market,
    })));
  const riskNote = (value?.groups ?? []).find((group) => group.risk?.note)?.risk?.note;
  const shownPositions = filtered
    ? accountRows.reduce((sum, row) => sum + (Number(row.risk.positions) || 0), 0)
    : value?.counts?.positions;
  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      {positions.error && (
        <Alert type="error" showIcon message={`持仓读取失败：${positions.error}`} />)}
      {filtered && (
        <Typography.Text type="secondary">
          市场筛选：{marketLabelOf(market)} —— 下列两张表只含该市场账户；
          风控配置是全局配置，没有市场维度，不随筛选变化。
        </Typography.Text>)}
      {view.emptyReason && (
        <Typography.Text type="warning">{view.emptyReason}</Typography.Text>)}
      {value && (
        <Descriptions size="small" bordered column={{ xs: 1, sm: 3 }}>
          <Descriptions.Item label={filtered ? `检查账户数（${marketLabelOf(market)}）` : "检查账户数"}>
            {filtered ? view.groups.length : (value.counts?.accounts_checked ?? "—")}
          </Descriptions.Item>
          <Descriptions.Item label="有持仓账户数">{value.counts?.accounts_with_positions ?? "—"}</Descriptions.Item>
          <Descriptions.Item label={filtered ? `持仓笔数（${marketLabelOf(market)}）` : "持仓笔数"}>
            {shownPositions ?? "—"}
          </Descriptions.Item>
        </Descriptions>)}
      <Table size="small" rowKey={(row) => row.group.acc_id ?? row.group.account ?? "account"}
        dataSource={accountRows} pagination={false}
        locale={{ emptyText: positions.loading ? "持仓加载中…" : "暂无账户持仓风险数据。" }}
        columns={[
          { title: "账户", key: "account",
            render: (_field, row) => maskedAccount(row.group.acc_id, row.group.account) },
          // 「全部市场」下跨市场混排：补市场列，显示值与筛选口径同源
          ...(filtered ? [] : [{ title: "市场", key: "market",
            render: (_field, row) => marketDisplay(row.group.market) }]),
          { title: "持仓数", key: "positions", align: "right",
            render: (_field, row) => row.risk.positions ?? "—" },
          { title: "最大集中度", key: "max_share",
            render: (_field, row) => (row.risk.max_share_of_positions === null
              || row.risk.max_share_of_positions === undefined ? "—"
              : `${Number(row.risk.max_share_of_positions).toFixed(2)}%（${row.risk.max_share_symbol ?? "—"}）`) },
          { title: "盈利持仓数", key: "winners", align: "right",
            render: (_field, row) => row.risk.winners?.count ?? "—" },
          { title: "亏损持仓数", key: "losers", align: "right",
            render: (_field, row) => row.risk.losers?.count ?? "—" },
        ]} />
      <Table size="small" rowKey={(row) => `${row.accId}-${row.symbol}`}
        dataSource={concentrationRows} pagination={false}
        locale={{ emptyText: "暂无集中度明细。" }}
        columns={[
          { title: "账户", key: "account", render: (_field, row) => maskedAccount(row.accId, row.account) },
          ...(filtered ? [] : [{ title: "市场", key: "market",
            render: (_field, row) => marketDisplay(row.groupMarket) }]),
          { title: "标的", key: "symbol",
            render: (_field, row) => `${row.symbol ?? "—"}（${row.name ?? "—"}）` },
          { title: "持仓市值", key: "market_value", align: "right",
            render: (_field, row) => num(row.market_value) },
          { title: "占本账户持仓市值", key: "share_of_positions", align: "right",
            render: (_field, row) => (row.share_of_positions === null
              || row.share_of_positions === undefined ? "—"
              : `${Number(row.share_of_positions).toFixed(2)}%`) },
          { title: "占本账户总资产", key: "share_of_assets", align: "right",
            render: (_field, row) => (row.share_of_assets === null
              || row.share_of_assets === undefined ? "—"
              : `${Number(row.share_of_assets).toFixed(2)}%`) },
        ]} />
      {riskNote && <Typography.Text type="secondary">{riskNote}</Typography.Text>}
    </Space>);
}

export default function RiskPage() {
  const { market } = useMarketFilter();
  const risk = useEndpoint("risk", {}, []);
  // 组合风险按模拟盘持仓读取；实盘视角请到组合页切换查看。
  const positions = useEndpoint("positions", { mode: "sim" }, []);
  const config = typeof risk.value?.config === "object" && risk.value?.config !== null
    ? risk.value.config : null;
  return (
    <Card title="风险">
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Card type="inner" title="风控配置">
          {risk.error && (
            <Alert type="error" showIcon message={`风控配置读取失败：${risk.error}`} />)}
          {risk.loading && !risk.error && (
            <Typography.Text type="secondary">风控配置加载中…</Typography.Text>)}
          {config ? (
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              <Descriptions size="small" bordered column={{ xs: 1, sm: 2 }}>
                {riskItems(config).map(({ key, label, format }) => (
                  <Descriptions.Item key={key} label={`${label}（${key}）`}>
                    {formatRiskValue(config[key], format)}
                  </Descriptions.Item>))}
              </Descriptions>
              {risk.value?.source && (
                <Typography.Text type="secondary">配置来源：{risk.value.source}</Typography.Text>)}
            </Space>
          ) : (
            !risk.error && !risk.loading && risk.value && (
              <Alert type="warning" showIcon
                message={`风控配置不可用：${risk.value.error ?? "config 为空"}`} />)
          )}
        </Card>
        <Card type="inner" title="组合风险">
          <PortfolioRisk positions={positions} market={market} />
          {positions.value?.as_of && (
            <Typography.Text type="secondary">持仓数据时间：{positions.value.as_of}</Typography.Text>)}
          <Typography.Text type="secondary">组合风险按模拟盘（sim）持仓读取。</Typography.Text>
        </Card>
        <Typography.Text type="secondary">
          本页展示读取到的风控参数与持仓派生数据；不构成投资建议，模式切换不授权下单。
        </Typography.Text>
      </Space>
    </Card>);
}
