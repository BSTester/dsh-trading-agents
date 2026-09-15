// 行情页：K 线序列（series）+ 标的卡片（instrument）。
// 字段依据（页面每个取值路径均可指到源码行）：
//   series → plugins/workbench/src/series.js（透传 bars.py）→ {ticker, period, source, as_of, count, bars}；
//            bar = {t, o, h, l, c, v}（plugins/workbench/python/bars.py 输出声明）。
//   instrument → plugins/workbench/python/instruments.py（card 构造）→ {ticker, symbol, market, name,
//            name_en, price, prev_close, change_pct, open, high, low, volume, turnover, lot_size,
//            as_of, source, note}；脚本失败时 analytics.js instrument() 抛错进入 error。
import React from "react";
import { Card, Descriptions, Input, Select, Space, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";
import { num } from "../services/format.jsx";
import { KLineChart } from "../charts/kline.jsx";

const PERIODS = [
  { value: "1d", label: "日线" }, { value: "60m", label: "60分" },
  { value: "15m", label: "15分" }, { value: "5m", label: "5分" }, { value: "1m", label: "1分" },
];

function pct(value) {
  const parsed = Number(value);
  if (value === null || value === undefined || !Number.isFinite(parsed)) return "—";
  return `${parsed > 0 ? "+" : ""}${parsed.toFixed(2)}%`;
}

/** 标的卡片：instruments.py 输出的核心字段，不 JSON 直出。 */
function InstrumentCard({ instrument }) {
  if (instrument.loading) {
    return <Typography.Text type="secondary">标的解析中…</Typography.Text>;
  }
  if (instrument.error) {
    return <Typography.Text type="warning">标的解析失败：{instrument.error}</Typography.Text>;
  }
  const card = instrument.value;
  if (!card) return null;
  return (
    <Descriptions size="small" bordered column={{ xs: 1, sm: 2, md: 3 }}>
      <Descriptions.Item label="名称">{card.name ?? "—"}</Descriptions.Item>
      <Descriptions.Item label="代码">{card.symbol ?? "—"}（{card.market ?? "—"}）</Descriptions.Item>
      <Descriptions.Item label="最新价">{num(card.price)}</Descriptions.Item>
      <Descriptions.Item label="涨跌幅">{pct(card.change_pct)}</Descriptions.Item>
      <Descriptions.Item label="今开 / 最高 / 最低">
        {num(card.open)} / {num(card.high)} / {num(card.low)}
      </Descriptions.Item>
      <Descriptions.Item label="成交量">{num(card.volume, 0)}</Descriptions.Item>
      <Descriptions.Item label="成交额">{num(card.turnover, 0)}</Descriptions.Item>
      <Descriptions.Item label="每手股数">{num(card.lot_size, 0)}</Descriptions.Item>
      <Descriptions.Item label="数据时间">{card.as_of || "—"}</Descriptions.Item>
      <Descriptions.Item label="数据来源" span={3}>{card.source ?? "—"}</Descriptions.Item>
    </Descriptions>
  );
}

export default function MarketPage() {
  const [ticker, setTicker] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [period, setPeriod] = React.useState("1d");
  // 未输入标的时 payload 为 null：hook 跳过请求（服务端对空 ticker 会直接报错）。
  const series = useEndpoint("series", query ? { ticker: query, period, limit: 250 } : null, [query, period]);
  const instrument = useEndpoint("instrument", query ? { ticker: query } : null, [query]);
  return (
    <Card title="行情" extra={(
      <Space>
        <Input placeholder="标的代码，如 SH.600519" style={{ width: 220 }} value={ticker}
          onChange={(event) => setTicker(event.target.value)}
          onPressEnter={() => setQuery(ticker.trim().toUpperCase())} />
        <Select options={PERIODS} value={period} onChange={setPeriod} style={{ width: 90 }} />
      </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {!query && (
          <Typography.Text type="secondary">
            输入标的代码后加载；数据按 TTL 本地缓存，不追求实时行情。
          </Typography.Text>)}
        {query && <InstrumentCard instrument={instrument} />}
        {query && instrument.value?.note && (
          <Typography.Text type="warning">{instrument.value.note}</Typography.Text>)}
        {query && series.error && (
          <Typography.Text type="danger">K 线读取失败：{series.error}</Typography.Text>)}
        {query && series.value?.bars?.length
          ? <KLineChart bars={series.value.bars} />
          : query && !series.loading && !series.error && (
            <Typography.Text type="secondary">该周期暂无数据。</Typography.Text>)}
        {query && series.value?.as_of && (
          <Typography.Text type="secondary">
            K 线数据截至 {series.value.as_of}（来源：{series.value.source ?? "—"}）
          </Typography.Text>)}
      </Space>
    </Card>);
}
