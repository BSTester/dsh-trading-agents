// 信号页：读取 snapshot.previews（写入结构见 plugins/workbench/src/store.js recordPreview：
// {id, at, kind, mode, execution_source, value}；snapshot() 已按当前模式过滤并倒序，最新在前）。
// value 字段依据：
//   kind = "signal" → plugins/engine/python/engine.py compute_signal → {ticker, strategy,
//     strategy_label, date, price, signal, signal_label, atr, execution_source,
//     execution_source_label, sentiment?}；没有入场价/止损价字段（那是 decide 流程的输出），
//     因此卡片展示 price 与 atr。
//   kind = "backtest" → plugins/datasource/python/trading_datasource/backtest.py main 输出
//     {ticker, strategy, source, summary{total_return, annualized, sharpe, max_drawdown, bars,
//     final_equity, trades, win_rate}, ...}。
// kind = "ledger"（quant_report 本地台账预览）不在本页展示。
import React from "react";
import { Card, Col, Row, Space, Statistic, Table, Tag, Typography } from "antd";
import { useSnapshotPoll } from "../services/hooks.js";
import { useMarketFilter } from "../services/marketContext.jsx";
import { marketLabelOf, symbolMarketDisplay, viewSignalPreviews } from "../services/marketView.js";
import { isAllMarkets } from "../services/marketFilter.js";
// antd Statistic 带 precision 时是**截断**（ATR 5.9157 会显示成 5.91，正确值 5.92）：
// 先按同位数四舍五入再把数值交给它。口径见 services/formatCore.js 的 roundTo。
import { round } from "../services/format.jsx";

const KIND_LABEL = { signal: "信号", backtest: "回测" };
const MODE_LABEL = { sim: "模拟", live: "实盘" };
const MODE_COLOR = { sim: "green", live: "red" };

function timeOf(value) {
  return typeof value === "string" ? value.slice(0, 19).replace("T", " ") : "—";
}

export default function SignalPage() {
  const snapshot = useSnapshotPoll();
  const { market } = useMarketFilter();
  const filtered = !isAllMarkets(market);
  // 全局市场筛选（客户端展示层，**不改请求参数**）：previews 的标的在**嵌套的**
  // value.ticker 里（实测 store.recordPreview 的结构），由 viewSignalPreviews 取链过滤；
  // value 为 null 的行（ledger）与无 ticker 的行计入「无法判定」，空态里如实说明。
  const view = viewSignalPreviews(
    (snapshot.value?.previews ?? []).filter((row) => row.kind !== "ledger"), market);
  const previews = view.rows;
  const latest = previews.find((row) => row.kind === "signal");
  const value = latest?.value ?? null;
  return (
    <Card title="信号">
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {snapshot.error && (
          <Typography.Text type="danger">快照读取失败：{snapshot.error}</Typography.Text>)}
        {filtered && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            市场筛选：{marketLabelOf(market)} —— 只列该市场标的的信号/回测预览。
          </Typography.Text>)}
        {latest && value ? (
          <Card type="inner" title={`最新信号${filtered ? `（${marketLabelOf(market)}）` : ""}`} extra={(
            <Space size="small">
              <Typography.Text type="secondary">{value.execution_source_label ?? ""}</Typography.Text>
              <Typography.Text type="secondary">记录时间 {timeOf(latest.at)}</Typography.Text>
            </Space>)}>
            <Row gutter={16}>
              <Col span={4}><Statistic title="标的" value={value.ticker ?? "—"} /></Col>
              <Col span={4}><Statistic title="策略" value={value.strategy_label ?? value.strategy ?? "—"} /></Col>
              <Col span={4}><Statistic title="信号" value={value.signal_label ?? value.signal ?? "—"}
                valueStyle={{ color: value.signal === "BUY" ? "#cf1322"
                  : value.signal === "SELL" ? "#3f8600" : undefined }} /></Col>
              <Col span={4}><Statistic title="收盘价" value={round(value.price, 2) ?? "—"} precision={2} /></Col>
              <Col span={4}><Statistic title="ATR(14)" value={round(value.atr, 2) ?? "—"} precision={2} /></Col>
              <Col span={4}><Statistic title="数据日期" value={value.date ?? "—"} /></Col>
            </Row>
          </Card>
        ) : (
          !snapshot.error && (
            snapshot.loading
              ? <Typography.Text type="secondary">快照加载中…</Typography.Text>
              : <Typography.Text type="secondary">
                {view.emptyReason ?? "暂无信号预览；在 Harness 会话中调用 quant_signal 生成。"}
              </Typography.Text>)
        )}
        <Card type="inner" title="历史预览">
          <Table size="small" rowKey="id" pagination={false}
            dataSource={previews.slice(0, 20)}
            locale={{ emptyText: "暂无预览记录。" }}
            columns={[
              { title: "时间", dataIndex: "at", render: timeOf },
              { title: "标的", key: "ticker", render: (_field, row) => row.value?.ticker ?? "—" },
              // 「全部市场」下补市场列（信号可能跨市场混排）；「—」= 无前缀，无法判定
              ...(filtered ? [] : [{ title: "市场", key: "market",
                render: (_field, row) => symbolMarketDisplay(row.value?.ticker) }]),
              { title: "策略", key: "strategy", render: (_field, row) => row.value?.strategy ?? "—" },
              { title: "类型", dataIndex: "kind",
                render: (kind) => <Tag>{KIND_LABEL[kind] ?? kind}</Tag> },
              { title: "模式", dataIndex: "mode",
                render: (mode) => <Tag color={MODE_COLOR[mode] ?? "default"}>{MODE_LABEL[mode] ?? mode}</Tag> },
            ]} />
        </Card>
      </Space>
    </Card>);
}
