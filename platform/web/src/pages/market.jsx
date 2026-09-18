// 行情页：K 线序列（series）+ 标的卡片（instrument）+ 实时报价/盘口（WP8 富途直通）。
// 字段依据（页面每个取值路径均可指到源码行）：
//   series → plugins/workbench/src/series.js（透传 bars.py）→ {ticker, period, source, as_of, count, bars}；
//            bar = {t, o, h, l, c, v}（plugins/workbench/python/bars.py 输出声明）。
//   instrument → plugins/workbench/python/instruments.py（card 构造）→ {ticker, symbol, market, name,
//            name_en, price, prev_close, change_pct, open, high, low, volume, turnover, lot_size,
//            as_of, source, note}；脚本失败时 analytics.js instrument() 抛错进入 error。
//   rt_quote → futu_data.rt_quote：codes 1..10 → 通道透传（推送快照命中时 {source:"push",
//            code_list:[…]}；REST/MCP 通道按官方字段面 code_list[].last_price/prev_close_price/…
//            防御式读取，缺字段一律 —）。A 股实时无权限（ret=-9）：错误信封自带替代路径
//            提示（capital_flow / history-kline），页面原样展示——数据事实，非故障。
//   rt_order_book → futu_data.rt_order_book：档数随行情权限（HK 10 / US 60 / A 股不可用），
//            不假定固定档数；通道形状有实测差异（futu_data._fetch_mcp：MCP 侧 data 是数组
//            [{books:…}]，REST 元素级字段更全），页面按候选键防御式提取买卖档，原始返回
//            折叠可查——形状不符时界面不编造。
import React from "react";
import { Alert, Card, Col, Collapse, Descriptions, Row, Select, Space, Table, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";
import { num } from "../services/format.jsx";
import { useMarketFilter } from "../services/marketContext.jsx";
import { symbolMarketNote } from "../services/marketView.js";
import { SymbolInput } from "../components/SymbolInput.jsx";
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

/** 多候选键防御读取：返回第一个非 null/undefined 的值，全部缺失返回 undefined（不补默认值）。 */
function pickKey(row, keys) {
  for (const key of keys) {
    if (row && row[key] !== null && row[key] !== undefined) return row[key];
  }
  return undefined;
}

/** rt_quote / rt_order_book 的 value → 首个条目对象（推送快照 code_list[0]、MCP 数组
 *  首元素、或对象本身）；取不到返回 null。 */
function firstEntry(value) {
  if (Array.isArray(value)) return value.find((item) => item && typeof item === "object") ?? null;
  if (value && typeof value === "object") {
    const list = pickKey(value, ["code_list", "quote_list"]);
    if (Array.isArray(list)) return list.find((item) => item && typeof item === "object") ?? null;
    return value;
  }
  return null;
}

function pctText(value) {
  const parsed = Number(value);
  if (value === null || value === undefined || !Number.isFinite(parsed)) return "—";
  return `${parsed > 0 ? "+" : ""}${parsed.toFixed(2)}%`;
}

/** 涨跌幅优先用上游字段；没有才由 最新价/昨收 换算（纯展示换算）。 */
function changePctOf(entry) {
  const direct = pickKey(entry, ["change_rate", "change_pct", "premium_rate"]);
  if (direct !== undefined) return direct;
  const last = Number(pickKey(entry, ["last_price", "cur_price", "price"]));
  const prev = Number(pickKey(entry, ["prev_close_price", "prev_close"]));
  if (Number.isFinite(last) && Number.isFinite(prev) && prev !== 0) {
    return ((last - prev) / prev) * 100;
  }
  return undefined;
}

/** 实时报价卡（rt_quote，TTL 0）：最新价/涨跌幅/买卖一档/成交量。 */
function RtQuoteCard({ ticker }) {
  const quote = useEndpoint("rt_quote", ticker ? { codes: [ticker] } : null, [ticker]);
  if (quote.error) {
    return <Alert type="warning" showIcon message={`实时报价不可用：${quote.error}`} />;
  }
  const entry = firstEntry(quote.value);
  if (!entry) {
    return (
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        <Typography.Text type="secondary">
          {quote.loading ? "实时报价加载中…" : "上游未返回报价条目；原始返回如下供核对。"}
        </Typography.Text>
        {!quote.loading && quote.value !== undefined && quote.value !== null && (
          <Collapse size="small" items={[{
            key: "raw",
            label: "原始返回（核对用）",
            children: (
              <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12, maxHeight: 320, overflow: "auto" }}>
                {JSON.stringify(quote.value, null, 2)}
              </pre>),
          }]} />)}
      </Space>);
  }
  const bid = pickKey(entry, ["bid_price", "bid"]);
  const ask = pickKey(entry, ["ask_price", "ask"]);
  const changePct = changePctOf(entry);
  return (
    <Descriptions size="small" bordered column={{ xs: 1, sm: 2, md: 3 }}>
      <Descriptions.Item label="最新价">
        {num(pickKey(entry, ["last_price", "cur_price", "price"]))}
      </Descriptions.Item>
      <Descriptions.Item label="涨跌幅">{pctText(changePct)}</Descriptions.Item>
      <Descriptions.Item label="今开 / 最高 / 最低">
        {num(pickKey(entry, ["open_price", "open"]))} / {num(pickKey(entry, ["high_price", "high"]))} / {num(pickKey(entry, ["low_price", "low"]))}
      </Descriptions.Item>
      <Descriptions.Item label="昨收">{num(pickKey(entry, ["prev_close_price", "prev_close"]))}</Descriptions.Item>
      <Descriptions.Item label="买一 价/量">
        {Array.isArray(bid) ? `${num(bid[0])} / ${num((entry.bid_volume ?? entry.bid_volumes)?.[0], 0)}` : num(bid)}
      </Descriptions.Item>
      <Descriptions.Item label="卖一 价/量">
        {Array.isArray(ask) ? `${num(ask[0])} / ${num((entry.ask_volume ?? entry.ask_volumes)?.[0], 0)}` : num(ask)}
      </Descriptions.Item>
      <Descriptions.Item label="成交量">{num(pickKey(entry, ["volume", "turnover_vol"]), 0)}</Descriptions.Item>
      <Descriptions.Item label="成交额">{num(pickKey(entry, ["turnover", "amount"]), 0)}</Descriptions.Item>
      <Descriptions.Item label="更新时间 / 来源">
        {pickKey(entry, ["update_time", "time"]) ?? "—"}
        {quote.value?.source === "push" ? "（推送快照）" : ""}
      </Descriptions.Item>
    </Descriptions>);
}

/** 盘口档位行：对象取 price/volume/num_orders 候选键；数字按并行数组配对。 */
function levelRow(item, index, fallbackVolume) {
  if (item && typeof item === "object") {
    return {
      level: index + 1,
      price: pickKey(item, ["price", "p"]),
      volume: pickKey(item, ["volume", "v", "qty"]),
      orders: pickKey(item, ["num_orders", "order_num", "orders"]),
    };
  }
  return { level: index + 1, price: item, volume: fallbackVolume, orders: undefined };
}

/** 从盘口 value 提取某一侧档位数组（键面跨通道防御；缺 → 空数组）。 */
function sideLevels(entry, side) {
  if (!entry || typeof entry !== "object") return [];
  const nested = pickKey(entry, [side, `${side}_list`, `${side}s`]);
  if (Array.isArray(nested)) {
    return nested.map((item, index) => levelRow(item, index, undefined));
  }
  const prices = pickKey(entry, [`${side}_price`, `${side}_prices`]);
  const volumes = pickKey(entry, [`${side}_volume`, `${side}_volumes`]);
  if (Array.isArray(prices)) {
    return prices.map((price, index) => ({
      level: index + 1,
      price,
      volume: Array.isArray(volumes) ? volumes[index] : undefined,
      orders: undefined,
    }));
  }
  return [];
}

const BOOK_COLUMNS = [
  { title: "档位", key: "level", width: 56, render: (_f, row) => row.level },
  { title: "价格", key: "price", align: "right", render: (_f, row) => num(row.price) },
  { title: "数量", key: "volume", align: "right", render: (_f, row) => num(row.volume, 0) },
  { title: "订单数", key: "orders", align: "right", render: (_f, row) => num(row.orders, 0) },
];

/** 实时盘口卡（rt_order_book，TTL 0）：档数随行情权限，不假定固定档数。 */
function OrderBookCard({ ticker }) {
  const book = useEndpoint("rt_order_book", ticker ? { code: ticker } : null, [ticker]);
  if (book.error) {
    return <Alert type="warning" showIcon message={`实时盘口不可用：${book.error}`} />;
  }
  // MCP 通道 data 是数组 [{books:…}]：条目对象可能藏在 books 键下，先摊平再取档位
  const entry = firstEntry(book.value);
  const bookBody = entry && typeof entry.books === "object" && entry.books !== null
    ? (Array.isArray(entry.books) ? entry.books.find((item) => item && typeof item === "object") ?? {} : entry.books)
    : entry ?? {};
  const bids = sideLevels(bookBody, "bid");
  const asks = sideLevels(bookBody, "ask");
  return (
    <>
      <Row gutter={16}>
        <Col xs={24} md={12}>
          <Typography.Text type="secondary">买盘（{bids.length} 档）</Typography.Text>
          <Table size="small"
            rowKey={(row) => `bid-${row.level}`}
            dataSource={bids}
            pagination={false}
            loading={book.loading}
            locale={{ emptyText: book.loading ? "盘口加载中…" : "无买盘数据。" }}
            columns={BOOK_COLUMNS} />
        </Col>
        <Col xs={24} md={12}>
          <Typography.Text type="secondary">卖盘（{asks.length} 档）</Typography.Text>
          <Table size="small"
            rowKey={(row) => `ask-${row.level}`}
            dataSource={asks}
            pagination={false}
            loading={book.loading}
            locale={{ emptyText: book.loading ? "盘口加载中…" : "无卖盘数据。" }}
            columns={BOOK_COLUMNS} />
        </Col>
      </Row>
      {!book.loading && bids.length === 0 && asks.length === 0 && (
        <Typography.Text type="secondary">
          通道未返回盘口档位（权限或字段面差异）；原始返回如下供核对。
        </Typography.Text>)}
      {!book.loading && book.value !== undefined && book.value !== null && (
        <Collapse size="small" style={{ marginTop: 8 }} items={[{
          key: "raw",
          label: "原始返回（核对用）",
          children: (
            <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12, maxHeight: 320, overflow: "auto" }}>
              {JSON.stringify(book.value, null, 2)}
            </pre>),
        }]} />)}
    </>);
}

export default function MarketPage() {
  const [ticker, setTicker] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [period, setPeriod] = React.useState("1d");
  const { market } = useMarketFilter();
  // 未输入标的时 payload 为 null：hook 跳过请求（服务端对空 ticker 会直接报错）。
  const series = useEndpoint("series", query ? { ticker: query, period, limit: 250 } : null, [query, period]);
  const instrument = useEndpoint("instrument", query ? { ticker: query } : null, [query]);
  // 本页**不按全局市场过滤**（一次只查一个标的，按市场藏掉会与用户输入直接冲突）：
  // 只用 symbolChain 校验输入前缀与当前筛选是否一致，不一致就说一句，数据照常显示。
  const marketNote = symbolMarketNote(query, market);
  return (
    <Card title="行情" extra={(
      <Space>
        <SymbolInput placeholder="标的代码，如 SH.600519" style={{ width: 220 }} value={ticker}
          onChange={setTicker}
          onPressEnter={(text) => setQuery(text.trim().toUpperCase())} />
        <Select options={PERIODS} value={period} onChange={setPeriod} style={{ width: 90 }} />
      </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {!query && (
          <Typography.Text type="secondary">
            输入标的代码后加载；K 线与标的按 TTL 本地缓存，实时报价/盘口为富途直通（TTL 0）。
          </Typography.Text>)}
        {query && marketNote && <Alert type="info" showIcon message={marketNote} />}
        {query && <InstrumentCard instrument={instrument} />}
        {query && instrument.value?.note && (
          <Typography.Text type="warning">{instrument.value.note}</Typography.Text>)}
        {query && (
          <Card type="inner" title="实时报价（rt_quote）">
            <RtQuoteCard ticker={query} />
          </Card>)}
        {query && (
          <Card type="inner" title="实时盘口（rt_order_book，档数随行情权限）">
            <OrderBookCard ticker={query} />
          </Card>)}
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
