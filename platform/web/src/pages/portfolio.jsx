// 组合页：券商真实持仓（positions）+ 本地模拟台账权益曲线（equity）。
// 字段依据（页面每个取值路径均可指到源码行）：
//   positions → plugins/workbench/python/positions.py collect → {mode, as_of, source, stale,
//     groups, counts{accounts_checked, accounts_with_positions, positions},
//     errors[{account, acc_id, reason}], equity_marks, note, cached}
//     group（sim/live 两种构造）：{account, acc_id, market, kind, positions[], market_value, pl_val,
//       cash, total_asset, currency, subtotals?(live), risk}
//     position：{symbol, name, qty, available, cost_price, price, market_value, pl_val, pl_ratio,
//       currency}（sim 模式 currency 恒为 null，响应未提供）
//   equity → plugins/workbench/python/analytics.py equity_curve → {mode, count,
//     points[{t, equity, dd}], tickers, strategies, initial, current, total_return, max_drawdown,
//     sharpe, trades, note}
// 模式切换只是查看视角（本地 state），不调 switch-mode；模式切换永不授权下单。
import React from "react";
import { Alert, Card, Col, Row, Space, Statistic, Table, Tag, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";
import { num, pctOf, maskedAccount } from "../services/format.jsx";
import { LineChart } from "../charts/line.jsx";

const MODES = [
  { value: "sim", label: "模拟 sim" },
  { value: "live", label: "实盘 live" },
];

export default function PortfolioPage() {
  const [mode, setMode] = React.useState("sim");
  const positions = useEndpoint("positions", { mode }, [mode]);
  const equity = useEndpoint("equity", { mode, window: 250 }, [mode]);
  const rows = (positions.value?.groups ?? []).flatMap((group) =>
    (group.positions ?? []).map((position) => ({
      ...position, account: group.account, accId: group.acc_id,
    })));
  const equityPoints = (equity.value?.points ?? []).map((point) => ({ t: point.t, v: point.equity }));
  const counts = positions.value?.counts;
  return (
    <Card title="组合" extra={(
      <Space size="small">
        {MODES.map(({ value, label }) => (
          <Tag.CheckableTag key={value} checked={mode === value}
            onChange={() => setMode(value)}>{label}</Tag.CheckableTag>))}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          仅切换查看视角，不会切换账户模式
        </Typography.Text>
      </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Typography.Text type="secondary">
          按账户小计，不跨账户/币种合并；实时读取失败时展示缓存并标注 stale
        </Typography.Text>
        {positions.error && (
          <Alert type="error" showIcon message={`持仓读取失败：${positions.error}`} />)}
        {positions.value?.stale && (
          <Alert type="warning" showIcon
            message={`数据已非实时（stale），以下为 ${positions.value.as_of ?? "上次"} 取得的缓存：${positions.value.error ?? ""}`} />)}
        {(positions.value?.errors ?? []).map((row, index) => (
          <Alert key={`${row.acc_id ?? row.account ?? "error"}-${index}`} type="warning" showIcon
            message={`账户读取失败：${row.account ?? "—"}：${row.reason ?? "—"}`} />))}
        {counts && (
          <Typography.Text type="secondary">
            读取账户 {counts.accounts_checked ?? "—"} 个，其中有持仓 {counts.accounts_with_positions ?? "—"} 个、
            持仓 {counts.positions ?? "—"} 笔（数据时间：{positions.value?.as_of ?? "—"}）
          </Typography.Text>)}
        <Table size="small"
          rowKey={(row) => `${row.accId}-${row.symbol}`}
          dataSource={rows}
          pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
          locale={{ emptyText: positions.loading ? "持仓加载中…" : "暂无持仓。" }}
          columns={[
            { title: "账户", key: "account", render: (_field, row) => maskedAccount(row.accId, row.account) },
            { title: "标的", key: "symbol", render: (_field, row) => (
              <Space size={4}>
                <span>{row.symbol || "—"}</span>
                {row.name ? <Typography.Text type="secondary">{row.name}</Typography.Text> : null}
              </Space>) },
            { title: "数量", key: "qty", align: "right", render: (_field, row) => num(row.qty, 0) },
            { title: "市值", key: "market_value", align: "right", render: (_field, row) => num(row.market_value) },
            { title: "盈亏", key: "pl_val", align: "right", render: (_field, row) => num(row.pl_val) },
            { title: "币种", key: "currency", render: (_field, row) => row.currency ?? "—" },
          ]} />
        <Card type="inner" title="权益曲线（本地模拟台账）">
          {equity.error && (
            <Typography.Text type="danger">权益读取失败：{equity.error}</Typography.Text>)}
          {equityPoints.length >= 2 && <LineChart points={equityPoints} label="权益" />}
          {equityPoints.length === 1 && (
            <Typography.Text type="secondary">权益序列仅 1 个点，不足以绘制。</Typography.Text>)}
          {!equity.error && !equity.loading && equity.value && equityPoints.length === 0 && (
            <Typography.Text type="secondary">暂无权益序列。</Typography.Text>)}
          <Row gutter={16} style={{ marginTop: 12 }}>
            <Col span={6}><Statistic title="最新权益" value={equity.value?.current ?? "—"} precision={2} /></Col>
            <Col span={6}><Statistic title="累计收益率" value={pctOf(equity.value?.total_return)} /></Col>
            <Col span={6}><Statistic title="最大回撤" value={pctOf(equity.value?.max_drawdown)} /></Col>
            <Col span={6}><Statistic title="台账成交笔数" value={equity.value?.trades ?? "—"} /></Col>
          </Row>
          {equity.value?.note && (
            <Typography.Text type="secondary">{equity.value.note}</Typography.Text>)}
        </Card>
      </Space>
    </Card>);
}
