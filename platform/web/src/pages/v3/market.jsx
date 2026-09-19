// V3「行情与信号」页（Ant Design Pro 版）：数据入口 —— 行情看板 / 多因子信号 / 板块。
// 数据接口：GET /api/v3/market、/api/v3/market/watchlist、/api/v3/orderbook、/api/v3/factors/matrix、/api/v3/plates。
// 设计稿对应页：od-quant-harness-platform/market.html ——
//   工具条（标的/周期/数据源/as_of）→ K 线主图 → 盘口 → 自选行情表 → 多因子信号表 → 板块。
// 纪律：任一区块取不到只影响该区块，显式写「无数据源」+ 原因（权限缺口 / 上游不支持 / 字段未提供）。
import React from "react";
import {
  Alert, AutoComplete, Button, Col, Descriptions, Radio, Row, Space, Statistic, Table, Tag, Tooltip, Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { useV3 } from "../../services/v3api.js";
import { num, stampOf } from "../../services/format.jsx";
import { KLineChart } from "../../charts/kline.jsx";
import { HeatmapChart } from "../../charts/heatmap.jsx";
import { LineChart, movingAverage } from "../../charts/line.jsx";

const { Text } = Typography;

/** 周期选项：仅列出上游已支持的口径（周线 1w 上游返回 Invalid period，故不放入选项）。 */
const PERIODS = [
  { label: "1分", value: "1m" }, { label: "5分", value: "5m" }, { label: "15分", value: "15m" },
  { label: "30分", value: "30m" }, { label: "60分", value: "60m" }, { label: "日线", value: "1d" },
];

function numOr(value) {
  if (value === null || value === undefined || value === "") return null;
  return Number.isFinite(Number(value)) ? Number(value) : null;
}

function fixedValue(value, digits = 2) {
  const n = numOr(value);
  return n === null ? null : n.toFixed(digits);
}

function signedValue(value, digits = 2, suffix = "%") {
  const n = numOr(value);
  if (n === null) return null;
  return `${n >= 0 ? "+" : "−"}${Math.abs(n).toFixed(digits)}${suffix}`;
}

function pctValue(value, digits = 2) {
  const n = numOr(value);
  return n === null ? null : `${n.toFixed(digits)}%`;
}

function stampValue(value) {
  return value === null || value === undefined || value === "" ? "—" : stampOf(value);
}

function CellText({ value, fallback = "无数据源" }) {
  if (value === null || value === undefined || value === "") {
    return <Text type="secondary" style={{ fontSize: 12 }}>{fallback}</Text>;
  }
  return <span>{String(value)}</span>;
}

function NoSource({ what, why }) {
  return (
    <Alert type="warning" showIcon
      message={`${what}：无数据源`}
      description={why ? <Text type="secondary" style={{ fontSize: 12 }}>{why}</Text> : undefined} />
  );
}

function MetaChip({ label, value, title }) {
  const shown = value === null || value === undefined || value === "" ? "无数据源" : String(value);
  return (
    <Tooltip title={title}>
      <div style={{ minWidth: 0 }}>
        <Text type="secondary" style={{ fontSize: 11, display: "block" }}>{label}</Text>
        <Text style={{ fontSize: 12 }}>{shown}</Text>
      </div>
    </Tooltip>
  );
}

/** IC 点位：接口给数值或对象（v / ic / value），非有限值一律丢弃，不补 0。 */
function icPoints(ic) {
  const list = Array.isArray(ic?.points) ? ic.points : [];
  const points = [];
  for (const point of list) {
    const raw = typeof point === "number" ? point : (point?.v ?? point?.ic ?? point?.value ?? point?.meanIc);
    const value = numOr(raw);
    if (value !== null) points.push({ v: value });
  }
  return points;
}

/** 未知形状的记录数组 → 只按标量叶子生成列（键名原样保留，不猜语义）。 */
function scalarColumns(rows, limit = 10) {
  const keys = [];
  for (const row of rows) {
    for (const [key, value] of Object.entries(row ?? {})) {
      if (keys.length >= limit) break;
      if (!keys.includes(key) && (value === null || ["string", "number", "boolean"].includes(typeof value))) keys.push(key);
    }
  }
  return keys.map((key) => ({
    title: key, dataIndex: key, ellipsis: true,
    render: (value) => <CellText value={value} />,
  }));
}

/** 盘口档位：接口若返回 ok=true 的挂单数组，按标量叶子列表展示（当前环境实际返回 ok=false）。 */
function bookLevels(book) {
  const source = book?.data ?? book;
  for (const key of ["asks", "bids", "levels", "book"]) {
    if (Array.isArray(source?.[key]) && source[key].length) return { name: key, rows: source[key] };
  }
  return null;
}

export default function V3行情信号Page() {
  const [ticker, setTicker] = React.useState("SH.600519");
  const [input, setInput] = React.useState("SH.600519");
  const [period, setPeriod] = React.useState("1d");

  const market = useV3("market", { ticker, period, limit: 120 }, [ticker, period]);
  const watch = useV3("market/watchlist", { n: 6 }, []);
  const book = useV3("orderbook", { ticker }, [ticker]);
  const plates = useV3("plates", { market: "SH", plate_class: "ALL" }, []);

  const watchRows = Array.isArray(watch.value?.rows) ? watch.value.rows : [];
  const tickerParam = watchRows.map((row) => String(row?.ticker ?? "")).filter(Boolean).join(",");
  const factors = useV3("factors/matrix", { tickers: tickerParam }, [tickerParam]);

  const marketData = market.value?.data ?? null;
  const bars = React.useMemo(() => {
    const list = Array.isArray(marketData?.bars) ? marketData.bars : [];
    return list
      .filter((bar) => ["o", "h", "l", "c", "v"].every((key) => numOr(bar?.[key]) !== null))
      .map((bar) => ({
        t: bar.t, o: Number(bar.o), h: Number(bar.h), l: Number(bar.l), c: Number(bar.c), v: Number(bar.v),
      }));
  }, [marketData]);

  const ma5 = React.useMemo(() => (bars.length ? movingAverage(bars, 5) : []), [bars]);
  const ma20 = React.useMemo(() => (bars.length ? movingAverage(bars, 20) : []), [bars]);
  const lastBar = bars.length ? bars[bars.length - 1] : null;
  const prevBar = bars.length > 1 ? bars[bars.length - 2] : null;
  const changePct = lastBar && prevBar && prevBar.c ? ((lastBar.c - prevBar.c) / prevBar.c) * 100 : null;

  const matrix = factors.value?.matrix ?? null;
  const factorNames = Array.isArray(matrix?.factors) ? matrix.factors.map(String) : [];
  const matrixTickers = Array.isArray(matrix?.tickers) ? matrix.tickers.map(String) : [];
  const matrixRows = Array.isArray(matrix?.matrix) ? matrix.matrix : [];
  const factorTable = React.useMemo(() => {
    const built = matrixTickers.map((name, index) => {
      const line = Array.isArray(matrixRows[index]) ? matrixRows[index] : [];
      const values = factorNames.map((_, column) => numOr(line[column]));
      const finite = values.filter((value) => value !== null);
      const score = finite.length ? finite.reduce((sum, value) => sum + value, 0) / finite.length : null;
      return { key: name, ticker: name, values, score, rank: null };
    });
    const ranked = [...built].sort((left, right) => (right.score ?? -Infinity) - (left.score ?? -Infinity));
    let rank = 0;
    for (const row of ranked) {
      if (row.score !== null) { rank += 1; row.rank = rank; }
    }
    return ranked;
  }, [matrixTickers, matrixRows, factorNames]);

  const ic = factors.value?.ic ?? null;
  const points = React.useMemo(() => icPoints(ic), [ic]);
  const rawRows = Array.isArray(factors.value?.matrix?.raw) ? factors.value.matrix.raw : [];

  const plateList = Array.isArray(plates.value?.data?.plate_list) ? plates.value.data.plate_list : [];
  const levels = bookLevels(book.value);
  const watchSources = watch.value?.sources ?? null;

  const applyTicker = (next) => {
    const clean = String(next ?? "").trim().toUpperCase();
    if (!clean) return;
    setTicker(clean);
    setInput(clean);
  };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <div>
        <Text strong style={{ fontSize: 16 }}>行情与信号</Text>
        <Text type="secondary" style={{ fontSize: 12, marginLeft: 10 }}>
          单一标的 K 线 + 自选快照 + 多因子矩阵 + 板块清单，全部取自 /api/v3/*；取不到就写「无数据源」
        </Text>
      </div>

      {watch.error ? <Alert type="error" showIcon message="自选行情取数失败（/api/v3/market/watchlist）" description={watch.error} /> : null}
      {factors.error ? <Alert type="error" showIcon message="因子矩阵取数失败（/api/v3/factors/matrix）" description={factors.error} /> : null}
      {plates.error ? <Alert type="error" showIcon message="板块取数失败（/api/v3/plates）" description={plates.error} /> : null}

      <ProCard bordered>
        <Space wrap size={16} align="center">
          <Space size={6} align="center">
            <Text type="secondary" style={{ fontSize: 12 }}>标的</Text>
            <AutoComplete
              size="small" style={{ width: 180 }} value={input}
              onChange={(next) => setInput(next ?? "")}
              onSelect={(next) => applyTicker(next)}
              options={watchRows.map((row) => ({ value: String(row?.ticker ?? ""), label: `${row?.ticker ?? "—"}（自选）` }))}
              placeholder="SH.600519"
            />
            <Button size="small" onClick={() => applyTicker(input)}>切换</Button>
          </Space>
          <Space size={6} align="center">
            <Text type="secondary" style={{ fontSize: 12 }}>周期</Text>
            <Radio.Group size="small" value={period} onChange={(event) => setPeriod(event.target.value)} options={PERIODS} />
          </Space>
          <MetaChip label="数据源" value={marketData?.source} title="/api/v3/market data.source" />
          <MetaChip label="数据时间 as_of" value={marketData?.as_of ? stampValue(marketData.as_of) : null} title="/api/v3/market data.as_of" />
          <MetaChip label="K 线根数" value={numOr(marketData?.count) === null ? null : num(marketData.count, 0)} title="/api/v3/market data.count" />
          <Button size="small" onClick={() => { market.refresh(); watch.refresh(); book.refresh(); plates.refresh(); factors.refresh(); }}>刷新全部</Button>
          <Text type="secondary" style={{ fontSize: 12 }}>
            标的与周期切换会重新请求 /api/v3/market；周线（1w）上游返回 Invalid period，本页不提供该选项。
          </Text>
        </Space>
      </ProCard>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={17}>
          <ProCard bordered title={`K 线主图 · ${ticker}`} loading={market.loading} style={{ height: "100%" }}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>{marketData ? `${marketData.source ?? "—"} · ${stampValue(marketData.as_of)}` : "无数据源"}</Text>}>
            {market.error ? (
              <Alert type="error" showIcon style={{ marginBottom: 8 }}
                message="K 线取数失败（/api/v3/market）" description={market.error} />
            ) : null}
            {bars.length >= 2 ? (
              <>
                <Space size={16} wrap style={{ marginBottom: 8 }}>
                  <Text>昨收 <Text strong>{prevBar.c.toFixed(2)}</Text></Text>
                  <Text>最新 <Text strong>{lastBar.c.toFixed(2)}</Text></Text>
                  <Text type={changePct === null ? undefined : changePct >= 0 ? "danger" : "success"}>
                    {signedValue(changePct) ?? "无数据源"}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    开 {lastBar.o.toFixed(2)} · 高 {lastBar.h.toFixed(2)} · 低 {lastBar.l.toFixed(2)} · 量 {num(lastBar.v, 0)}
                  </Text>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    MA5 {fixedValue(ma5[ma5.length - 1]) ?? "—（根数不足 5）"} · MA20 {fixedValue(ma20[ma20.length - 1]) ?? "—（根数不足 20）"}
                  </Text>
                </Space>
                <KLineChart bars={bars} />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  共 {bars.length} 根（接口返回 {numOr(marketData?.count) === null ? "无数据源" : num(marketData.count, 0)} 根）·
                  数据源 {marketData?.source ?? "无数据源"} · as_of {stampValue(marketData?.as_of)} · 悬停查看逐根读数。
                  MA5/MA20 由本页按真实收盘价计算；买卖信号标记无数据源，故不绘制。
                </Text>
              </>
            ) : market.error ? null : market.loading ? (
              <Text type="secondary" style={{ fontSize: 12 }}>加载中 /api/v3/market…</Text>
            ) : (
              <NoSource what="K 线主图" why="接口未返回有效 OHLC（bars 不足 2 根）：该标的/周期在上游无数据或数据源不可用。" />
            )}
          </ProCard>
        </Col>

        <Col xs={24} lg={7}>
          <ProCard bordered title={`盘口深度 · ${ticker}`} loading={book.loading} style={{ height: "100%" }}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>/api/v3/orderbook</Text>}>
            {book.error ? (
              <Alert type="error" showIcon
                message="盘口五档：无数据源"
                description={<Text type="secondary" style={{ fontSize: 12 }}>
                  服务端返回 ok=false · {book.error}。原因：富途账号未开通实时行情权限（realtime quote permission required），
                  本页不伪造买卖档位与委比。
                </Text>} />
            ) : levels ? (
              <Table size="small" rowKey={(row, index) => `${levels.name}-${index}`} dataSource={levels.rows}
                pagination={false} columns={scalarColumns(levels.rows, 6)} />
            ) : (
              <NoSource what="盘口五档" why="接口未返回 asks/bids 数组：实时行情权限缺口（服务端 ok=false）。" />
            )}
            {lastBar ? (
              <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
                参考价（最近一根真实 K 线收盘，非盘口成价）：{lastBar.c.toFixed(2)} · {signedValue(changePct) ?? "—"}
              </Text>
            ) : null}
          </ProCard>
        </Col>
      </Row>

      <ProCard bordered title="自选行情" loading={watch.loading}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {watchSources ? `K 线源 ${watchSources.kline ?? "无数据源"} · 因子源 ${watchSources.factors ?? "无数据源"}` : "无数据源"}
        </Text>}>
        {watch.error ? (
          <Alert type="error" showIcon style={{ marginBottom: 8 }}
            message="自选行情取数失败（/api/v3/market/watchlist）" description={watch.error} />
        ) : null}
        {watchRows.length ? (
          <Table
            size="small" rowKey={(row, index) => `${row?.ticker ?? "row"}-${index}`} dataSource={watchRows}
            pagination={false} scroll={{ x: "max-content" }}
            rowClassName={(row) => (String(row?.ticker) === ticker ? "ant-table-row-selected" : "")}
            onRow={(row) => ({
              onClick: () => applyTicker(row?.ticker),
              style: { cursor: "pointer" },
            })}
            columns={[
              { title: "代码", dataIndex: "ticker", render: (value) => <CellText value={value} /> },
              { title: "现价", dataIndex: "close", render: (value) => <CellText value={fixedValue(value)} /> },
              {
                title: "涨跌幅", dataIndex: "changePct",
                render: (value) => {
                  const n = numOr(value);
                  if (n === null) return <CellText value={null} />;
                  return <Text type={n >= 0 ? "danger" : "success"}>{signedValue(n)}</Text>;
                },
              },
              { title: "20日动量", dataIndex: "mom20Pct", render: (value) => <CellText value={signedValue(value)} /> },
              { title: "20日波动", dataIndex: "vol20Pct", render: (value) => <CellText value={pctValue(value)} /> },
              { title: "PE(TTM)", dataIndex: "peTtm", render: (value) => <CellText value={fixedValue(value)} /> },
              { title: "PB", dataIndex: "pb", render: (value) => <CellText value={fixedValue(value)} /> },
              { title: "快照时间", dataIndex: "asOf", render: (value) => <CellText value={stampValue(value)} fallback="无数据源" /> },
            ]}
          />
        ) : (
          <NoSource what="自选行情" why="watchlist 未返回 rows：上游取数不可用或自选池为空。" />
        )}
        <Text type="secondary" style={{ fontSize: 12 }}>
          点击行可切换主图与盘口标的。接口不返回标的名称、量比、换手率、主力净流入等字段 —— 这些列无数据源，本页不展示以免编造。
        </Text>
      </ProCard>

      <ProCard bordered title="多因子信号" loading={factors.loading}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {ic?.ok ? `IC 因子 ${ic.factor ?? "—"} · 观测 ${numOr(ic.observations) === null ? "无数据源" : num(ic.observations, 0)}` : "IC：无数据源"}
        </Text>}>
        {factors.error ? (
          <Alert type="error" showIcon style={{ marginBottom: 8 }}
            message="因子矩阵取数失败（/api/v3/factors/matrix）" description={factors.error} />
        ) : null}
        {matrix ? (
          <Row gutter={[12, 12]}>
            <Col xs={24} lg={12}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                因子矩阵（行 = 标的，列 = 因子 z 值；色深代表绝对值，红正绿负）· as_of {stampValue(matrix.as_of)}
              </Text>
              <HeatmapChart rowLabels={matrixTickers} colLabels={factorNames}
                matrix={matrixTickers.map((_, index) => (Array.isArray(matrixRows[index]) ? matrixRows[index].map(numOr) : []))}
                unit="" />
            </Col>
            <Col xs={24} lg={12}>
              {ic?.ok && points.length >= 2 ? (
                <>
                  <Space size={16} wrap>
                    <Statistic title="IC 均值" value={fixedValue(ic.meanIc, 4) ?? "无数据源"} />
                    <Statistic title="IC 标准差" value={fixedValue(ic.stdIc, 4) ?? "无数据源"} />
                    <Statistic title="IR" value={fixedValue(ic.ir, 3) ?? "无数据源"} />
                    <Statistic title="最新 IC" value={fixedValue(ic.latestIc, 4) ?? "无数据源"} />
                  </Space>
                  <LineChart points={points} label={`IC 序列 · ${ic.factor ?? "—"}`} />
                </>
              ) : (
                <NoSource what="因子 IC"
                  why={ic?.ok
                    ? `ic.ok=true 但 points 不足 2 个（实际 ${points.length} 个）：无法画 IC 序列。`
                    : "ic.ok=false：上游未计算 IC（样本或因子覆盖率不足）。"} />
              )}
            </Col>
            <Col span={24}>
              <Table
                size="small" rowKey="key" dataSource={factorTable} pagination={false} scroll={{ x: "max-content" }}
                columns={[
                  { title: "标的", dataIndex: "ticker", fixed: "left", render: (value) => <CellText value={value} /> },
                  ...factorNames.map((name, column) => ({
                    title: name, width: 96,
                    render: (_, row) => (row.values[column] === null
                      ? <CellText value={null} fallback="无数据源" />
                      : <Text type={row.values[column] >= 0 ? "danger" : "success"}>{fixedValue(row.values[column])}</Text>),
                  })),
                  { title: "等权综合分", width: 120, render: (_, row) => <CellText value={fixedValue(row.score, 3)} /> },
                  {
                    title: "方向（综合分符号）", width: 150,
                    render: (_, row) => (row.score === null
                      ? <Tag>无数据源</Tag>
                      : <Tag color={row.score >= 0 ? "red" : "green"}>{row.score >= 0 ? "偏多" : "偏空"}</Tag>),
                  },
                  { title: "矩阵内排名", width: 110, render: (_, row) => (row.rank === null ? <CellText value={null} /> : `#${row.rank} / ${matrixTickers.length}`) },
                ]}
              />
              <Text type="secondary" style={{ fontSize: 12 }}>
                综合分 = 本页对真实因子 z 值取的等权均值（页面计算值，服务端未提供综合分字段）；方向仅按综合分符号划分，不引入额外阈值；
                排名为矩阵覆盖的 {matrixTickers.length} 只标的内的名次，非全市场排名。
              </Text>
            </Col>
            {rawRows.length ? (
              <Col span={24}>
                <Text type="secondary" style={{ fontSize: 12 }}>上游原始记录（matrix.raw）· 字段随数据源变化，按实际键展示</Text>
                <Table size="small" rowKey={(row, index) => `raw-${index}`} dataSource={rawRows}
                  pagination={{ pageSize: 8, size: "small" }} scroll={{ x: "max-content" }}
                  columns={scalarColumns(rawRows)} />
              </Col>
            ) : null}
          </Row>
        ) : (
          <NoSource what="多因子信号" why="接口未返回 matrix（tickers/factors/matrix 三者之一缺失）：上游因子库不可用。" />
        )}
      </ProCard>

      <ProCard bordered title="板块（market=SH · plate_class=ALL）" loading={plates.loading}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {plateList.length ? `共 ${plateList.length} 个板块` : "无数据源"}
        </Text>}>
        {plates.error ? (
          <Alert type="error" showIcon style={{ marginBottom: 8 }}
            message="板块取数失败（/api/v3/plates）" description={plates.error} />
        ) : null}
        {plateList.length ? (
          <>
            <Table
              size="small" rowKey={(row, index) => `${row?.code ?? row?.plate_id ?? "plate"}-${index}`}
              dataSource={plateList} pagination={{ pageSize: 10, size: "small" }} scroll={{ x: "max-content" }}
              columns={[
                { title: "代码", dataIndex: "code", render: (value) => <CellText value={value} /> },
                { title: "板块 ID", dataIndex: "plate_id", render: (value) => <CellText value={value} /> },
                { title: "板块名称", dataIndex: "plate_name", render: (value) => <CellText value={value} /> },
                { title: "简称", dataIndex: "sc_name", render: (value) => <CellText value={value} /> },
              ]}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              /api/v3/plates 只返回 code / plate_id / plate_name / sc_name，**不返回涨跌幅** ——
              因此本页不做板块热力着色，也不展示涨跌列，避免用编造的数字填色阶。
            </Text>
          </>
        ) : (
          <NoSource what="板块清单" why="/api/v3/plates 未返回 plate_list：板块数据源不可用。" />
        )}
      </ProCard>

      <ProCard bordered title="说明" loading={market.loading}>
        <Descriptions column={1} size="small">
          <Descriptions.Item label="行情来源">
            <CellText value={marketData?.source ? `${marketData.source}（/api/v3/market?ticker=${ticker}&period=${period}&limit=120）` : null} />
          </Descriptions.Item>
          <Descriptions.Item label="自选来源">
            <CellText value={watchSources ? `kline=${watchSources.kline ?? "—"} · factors=${watchSources.factors ?? "—"}` : null} />
          </Descriptions.Item>
          <Descriptions.Item label="盘口来源">
            <CellText value={levels
              ? `/api/v3/orderbook 返回挂单数组（${levels.name}）`
              : `无数据源（/api/v3/orderbook${book.error ? `：${book.error}` : " 未返回挂单数组"}）`} />
          </Descriptions.Item>
        </Descriptions>
        <Text type="secondary" style={{ fontSize: 12 }}>
          所有数值为服务端实测；任何取不到的区块均显式标注「无数据源」并写明原因（权限缺口 / 上游不支持该周期 / 字段未提供）。
        </Text>
      </ProCard>
    </Space>
  );
}
