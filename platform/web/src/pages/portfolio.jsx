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
//
// 图表（WP26 第二批）：持仓市值 / 持仓盈亏两张分布图，派生全在
// services/portfolioCharts.js（纯函数、node --test 直测），页面只接线与渲染。
// **图与表同源**：三处都吃同一份 `viewGroups(...)`（同一个 filterGroups 结果），
// 图不会与表格出现两个不一致的数字；跳过条数由 skipNoteText 如实标注。
import React from "react";
import { Alert, Card, Col, Divider, Row, Space, Statistic, Table, Tag, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";
import { num, pctOf, maskedAccount, stampOf } from "../services/format.jsx";
import { useMarketFilter } from "../services/marketContext.jsx";
import { marketDisplay, marketLabelOf, viewGroups } from "../services/marketView.js";
import { isAllMarkets } from "../services/marketFilter.js";
import { LineChart } from "../charts/line.jsx";
import { HBarChart, VBarChart } from "../charts/bars.jsx";
import { compactNumber } from "../charts/geometry.js";
import {
  chartEmptyText, holdingPnlItems, holdingValueItems, skipNoteText,
} from "../services/portfolioCharts.js";

const MODES = [
  { value: "sim", label: "模拟 sim" },
  { value: "live", label: "实盘 live" },
];

export default function PortfolioPage() {
  const [mode, setMode] = React.useState("sim");
  const { market } = useMarketFilter();
  const positions = useEndpoint("positions", { mode }, [mode]);
  const equity = useEndpoint("equity", { mode, window: 250 }, [mode]);
  // 全局市场筛选（客户端展示层，**不改请求参数**）：positions 按账户分组、组上带
  // market（实测 sim 为数字 market_id 1/3/100）→ 按组筛、按行判空态。
  const filtered = !isAllMarkets(market);
  const view = viewGroups(positions.value?.groups, market, (group) => group?.positions ?? []);
  const rows = view.groups.flatMap((group) =>
    (group.positions ?? []).map((position) => ({
      ...position, account: group.account, accId: group.acc_id, groupMarket: group.market,
    })));
  const equityPoints = (equity.value?.points ?? []).map((point) => ({ t: point.t, v: point.equity }));
  const counts = positions.value?.counts;
  // 图表派生（纯函数）：入参就是上表那份 view.groups——图与表必然同源；跨账户不合并
  // （与表格逐行对应），跨币种按数值直接相加、不做汇率换算（页面既有口径）。
  const valueChart = holdingValueItems(view.groups);
  const pnlChart = holdingPnlItems(view.groups);
  // 条形右侧的「市值 · 占比」：占比分母就是图内画入项合计（valueChart.total），
  // 分子分母同一份数据，扇区/条形自洽。
  const valueText = (value) => (valueChart.total > 0
    ? `${compactNumber(value)} · ${((value / valueChart.total) * 100).toFixed(1)}%`
    : compactNumber(value));
  const valueNote = skipNoteText({
    skipped: valueChart.skipped, reason: "market_value 缺失、为 0 或为负",
  });
  const pnlNote = skipNoteText({
    skipped: pnlChart.skipped, reason: "pl_val 缺失或非数字",
  });
  // 图的空态（口径与既有表格同一优先级，纯函数在 services/portfolioCharts.js）：
  // 筛选原因 > 加载中 > 无持仓 > 字段整列缺失。
  const emptyState = {
    reason: view.emptyReason, loading: positions.loading,
    loadingText: "持仓加载中…", count: rows.length, emptyText: "暂无持仓。",
  };
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
            message={`数据已非实时（stale），以下为 ${stampOf(positions.value.as_of) === "—" ? "上次" : stampOf(positions.value.as_of)} 取得的缓存：${positions.value.error ?? ""}`} />)}
        {(positions.value?.errors ?? []).map((row, index) => (
          <Alert key={`${row.acc_id ?? row.account ?? "error"}-${index}`} type="warning" showIcon
            message={`账户读取失败：${row.account ?? "—"}：${row.reason ?? "—"}`} />))}
        {filtered && (
          <Typography.Text type="secondary">
            市场筛选：{marketLabelOf(market)} —— 上表与下方两张图只列该市场账户的持仓
            （账户列悬停可见完整账户名）；权益曲线是本地模拟台账，没有市场维度，仍为全局口径。
          </Typography.Text>)}
        {view.emptyReason && (
          <Typography.Text type="warning">{view.emptyReason}</Typography.Text>)}
        {counts && (
          <Typography.Text type="secondary">
            读取账户 {counts.accounts_checked ?? "—"} 个，其中有持仓 {counts.accounts_with_positions ?? "—"} 个、
            持仓 {filtered ? rows.length : (counts.positions ?? "—")} 笔
            {filtered ? `（已按「${marketLabelOf(market)}」筛选）` : ""}
            （数据时间：{stampOf(positions.value?.as_of)}）
          </Typography.Text>)}
        <Table size="small"
          rowKey={(row) => `${row.accId}-${row.symbol}`}
          dataSource={rows}
          pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
          locale={{ emptyText: positions.loading ? "持仓加载中…" : "暂无持仓。" }}
          columns={[
            { title: "账户", key: "account", render: (_field, row) => maskedAccount(row.accId, row.account) },
            // 「全部市场」下跨市场混排：补市场列（账户名只在悬停提示里，看不出来就还是混为一谈）
            ...(filtered ? [] : [{ title: "市场", key: "market",
              render: (_field, row) => marketDisplay(row.groupMarket) }]),
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
        <Card type="inner" title="持仓分布（图）">
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            <Typography.Text strong style={{ fontSize: 12 }}>
              持仓市值（按市值降序，条形右侧为市值 · 占比）
            </Typography.Text>
            <HBarChart items={valueChart.items} valueFormat={valueText}
              emptyText={chartEmptyText({ ...emptyState,
                missingText: "这些持仓的 market_value 全部缺失或为 0/负，无法绘制市值条。" })} />
            {valueNote && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>{valueNote}</Typography.Text>)}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              占比分母是本图画入项的市值合计 {compactNumber(valueChart.total)}
               （缺失/为 0 的持仓不进分母）；跨账户、跨币种按数值直接相加，不做汇率换算，
               故占比只反映数值规模。
            </Typography.Text>
            <Divider style={{ margin: "4px 0" }} />
            <Typography.Text strong style={{ fontSize: 12 }}>
              持仓盈亏（按 pl_val 降序，红涨绿跌，标签为代码）
            </Typography.Text>
            <VBarChart items={pnlChart.items}
              emptyText={chartEmptyText({ ...emptyState,
                missingText: "这些持仓的 pl_val 全部缺失，无法绘制盈亏柱。" })} />
            {pnlNote && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>{pnlNote}</Typography.Text>)}
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              盈亏是各账户本币数值，不跨币种相加；精确数值看上方表格（两图与表格同一份数据）。
            </Typography.Text>
          </Space>
        </Card>
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
