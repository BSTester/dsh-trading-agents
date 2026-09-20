// V3 工作台「行情与信号」页（Ant Design Pro）。
// 功能模块与设计稿版 /v3/market.html **一一对应**（binder: platform/web/public/v3/market.js）：
//   sec-1 工具条 + K 线主图（真实 o/h/l/c/v 蜡烛 + 成交量、标的头现价/涨跌幅/昨收、MA5/MA20 读数）
//   sec-2 盘口深度（/api/v3/orderbook，富途实时行情权限缺口 → 无数据源 + 原因）
//   sec-3 自选行情表（/api/v3/market/watchlist：真实收盘 / 涨跌幅 / 20 日动量）
//   sec-4 多因子信号表（/api/v3/factors/matrix 的 z 矩阵：价值←pb / 成长←peg / 动量←mom_20 /
//        质量←mdd_60 / 情绪←rsi_14 / 另类←liq_ratio + 综合分 + 信号）
//   sec-5 板块热力图（/api/v3/plates 真实清单；涨跌幅无数据源）
//   sec-6 数据源健康（workbench sources 真实探测 + /api/v3/metrics 调用计数）
// 数据：GET /api/v3/*；取不到显式「无数据源 + 原因」，页面不含任何占位数字。
import React from "react";
import {
  Alert,
  Badge,
  Button,
  Card,
  Descriptions,
  Divider,
  Empty,
  Input,
  Segmented,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { fmt, noSourceText, useV3 } from "../services/api.js";
import { CandleChart } from "../components/charts.jsx";
import DataDomainCard from "../components/dataDomain.jsx";

const { Text, Title } = Typography;

// 与设计稿 /v3/ 同一套 token（绿升红跌）
const C = {
  up: "#3fb950",
  down: "#f8514d",
  amber: "#d9a112",
  blue: "#4c8dff",
  muted: "#8b97a5",
  faint: "#626d7c",
};
const MONO = { fontVariantNumeric: "tabular-nums" };

// fmt.num 走 Number()，null/'' 会被算成 0；缺失值必须显式写「—」，不能变成 0.00
const numOr = (value, digits = 2) =>
  value === null || value === undefined || value === "" || !Number.isFinite(Number(value))
    ? "—"
    : Number(value).toFixed(digits);

// 设计稿因子列名 → 真实因子（矩阵里存在同名因子才取值，标题里写明对应关系）
const FACTOR_COLUMNS = [
  { header: "价值", factor: "pb" },
  { header: "成长", factor: "peg" },
  { header: "动量", factor: "mom_20" },
  { header: "质量", factor: "mdd_60" },
  { header: "情绪", factor: "rsi_14" },
  { header: "另类", factor: "liq_ratio" },
];

const PERIODS = [
  { label: "1d", value: "1d" },
  { label: "5m", value: "5m" },
  { label: "60m", value: "60m" },
];

const asArray = (value) => (Array.isArray(value) ? value : []);

function errText(payload, fallback = "接口未返回原因") {
  const error = (payload && payload.error) || {};
  return String(error.message || error.code || fallback);
}

/** 无数据源区块：统一走 noSourceText（是什么 + 为什么没有），绝不留占位数字 */
function NoSource({ what, why }) {
  return (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={<Text type="secondary">{noSourceText(what, why)}</Text>}
    />
  );
}

/** 区块级取数失败：只影响本块，不白屏 */
function BlockError({ name, error }) {
  return <Alert type="error" showIcon message={`${name}：取数失败`} description={String(error)} />;
}

const upDownColor = (value) => (Number(value) >= 0 ? C.up : C.down);

function sma(values, window) {
  const out = new Array(values.length).fill(null);
  let sum = 0;
  for (let index = 0; index < values.length; index += 1) {
    sum += values[index];
    if (index >= window) sum -= values[index - window];
    if (index >= window - 1) out[index] = sum / window;
  }
  return out;
}

function signalBadge(score) {
  if (score === null || score === undefined || !Number.isFinite(Number(score))) {
    return { text: "无数据源", color: "default" };
  }
  if (Number(score) >= 0.25) return { text: "看多", color: "success" };
  if (Number(score) <= -0.25) return { text: "看空", color: "error" };
  return { text: "中性", color: "default" };
}

export default function MarketPage() {
  const overview = useV3("overview", {});
  const metrics = useV3("metrics", {});
  const brain = useV3("brain", {});
  const watchlist = useV3("market/watchlist", { n: 6 });
  const plates = useV3("plates", { market: "SH", plate_class: "ALL" });

  const ov = overview.value && overview.value.ok ? overview.value : null;
  const mt = metrics.value && metrics.value.ok ? metrics.value : null;
  const br = brain.value && brain.value.ok ? brain.value : null;
  const watchRows = asArray(watchlist.value && watchlist.value.ok && watchlist.value.rows);

  const [ticker, setTicker] = React.useState("SH.600519");
  const [input, setInput] = React.useState("SH.600519");
  const [period, setPeriod] = React.useState("1d");
  const [starred, setStarred] = React.useState({});
  const [keyword, setKeyword] = React.useState("");
  const autoRef = React.useRef(true);

  // 自选快照到达后，未人工切换过标的时默认跟随自选首只（与设计稿一致）
  React.useEffect(() => {
    if (!autoRef.current || watchRows.length === 0) return;
    const first = watchRows[0].ticker;
    if (first) {
      setTicker(first);
      setInput(first);
    }
  }, [watchRows.length]);

  const barsHook = useV3("market", { ticker, period, limit: 160 });
  const barsData = barsHook.value && barsHook.value.ok ? barsHook.value.data : null;
  const bars = asArray(barsData && barsData.bars);

  const tickersKey = watchRows.map((row) => row.ticker).join(",");
  const factors = useV3("factors/matrix", tickersKey ? { tickers: tickersKey } : {});
  const orderbook = useV3("orderbook", { ticker });

  const refreshAll = () => {
    overview.refresh();
    metrics.refresh();
    brain.refresh();
    watchlist.refresh();
    plates.refresh();
    barsHook.refresh();
    factors.refresh();
    orderbook.refresh();
  };

  const pickTicker = (next) => {
    const value = String(next || "").trim();
    if (!value) return;
    autoRef.current = false;
    setTicker(value);
    setInput(value);
  };

  /* ── sec-1 K 线主图与标的头 ── */
  const closes = bars.map((bar) => Number(bar.c));
  const ma5 = sma(closes, 5);
  const ma20 = sma(closes, 20);
  const lastBar = bars.length ? bars[bars.length - 1] : null;
  const prevBar = bars.length > 1 ? bars[bars.length - 2] : null;
  const changePct =
    lastBar && prevBar && Number(prevBar.c) ? (Number(lastBar.c) / Number(prevBar.c) - 1) * 100 : null;

  const equity = (ov && ov.equity) || {};
  const ovPoints = asArray(equity.points);
  const ovLast = ovPoints.length ? ovPoints[ovPoints.length - 1] : null;
  const ovPrev = ovPoints.length > 1 ? ovPoints[ovPoints.length - 2] : null;
  const dailyPct = ovLast && ovPrev && Number(ovPrev.equity)
    ? (Number(ovLast.equity) / Number(ovPrev.equity) - 1) * 100
    : null;

  const mcpMs = mt && mt.mcp ? mt.mcp.avgMs : null;
  const sdkReason = (br && br.sdk && br.sdk.reason) || "本服务未挂载 SDK JSON-RPC 通道";
  const headlessReason = (br && br.headless && br.headless.reason) || "本服务未挂载 Headless CLI 子通道";

  /* ── sec-3 自选行情表 ── */
  const factorIndex = (() => {
    const matrix = (factors.value && factors.value.ok && factors.value.matrix) || null;
    if (!matrix || !Array.isArray(matrix.matrix)) return null;
    const tickers = asArray(matrix.tickers);
    const names = asArray(matrix.factors);
    const map = {};
    tickers.forEach((code, rowIndex) => {
      map[code] = {};
      names.forEach((factor, colIndex) => {
        const raw = asArray(matrix.matrix[rowIndex])[colIndex];
        map[code][factor] =
          raw === null || raw === undefined || !Number.isFinite(Number(raw)) ? null : Number(raw);
      });
    });
    return { map, tickers, names, matrix };
  })();

  const factorOf = (code, factor) => (factorIndex && factorIndex.map[code] ? factorIndex.map[code][factor] ?? null : null);
  const compositeOf = (code) => {
    if (!factorIndex || !factorIndex.map[code]) return null;
    const values = Object.values(factorIndex.map[code]).filter((value) => value !== null && Number.isFinite(value));
    if (values.length === 0) return null;
    return values.reduce((sum, value) => sum + value, 0) / values.length;
  };

  const factorCell = (value) => {
    if (value === null || value === undefined) {
      return (
        <Tooltip title="该因子在 /api/v3/factors/matrix 中未返回（无数据源）">
          <Text type="secondary">—</Text>
        </Tooltip>
      );
    }
    return <span style={{ ...MONO, color: upDownColor(value) }}>{fmt.signed(value, 2)}</span>;
  };

  const filteredWatch = watchRows.filter(
    (row) => !keyword || String(row.ticker).toLowerCase().indexOf(keyword.toLowerCase()) >= 0,
  );

  const watchColumns = [
    {
      title: "代码",
      dataIndex: "ticker",
      width: 120,
      render: (value) => (
        <Text strong style={MONO}>
          {fmt.dash(value)}
        </Text>
      ),
    },
    {
      title: "名称",
      dataIndex: "name",
      width: 90,
      render: () => (
        <Tooltip title="证券简称：自选快照接口未返回（无数据源）">
          <Text type="secondary">—</Text>
        </Tooltip>
      ),
    },
    {
      title: "现价",
      dataIndex: "close",
      width: 100,
      align: "right",
      render: (value) => <span style={MONO}>{numOr(value)}</span>,
    },
    {
      title: "涨跌幅",
      dataIndex: "changePct",
      width: 100,
      align: "right",
      render: (value) => (
        <span style={{ ...MONO, color: upDownColor(value) }}>{fmt.signed(value, 2, "%")}</span>
      ),
    },
    {
      title: "20 日动量",
      dataIndex: "mom20Pct",
      width: 110,
      align: "right",
      render: (value) => (
        <span style={{ ...MONO, color: upDownColor(value) }}>{fmt.signed(value, 2, "%")}</span>
      ),
    },
    {
      title: "量比",
      width: 80,
      align: "right",
      render: () => (
        <Tooltip title="量比：自选快照接口未返回（无数据源）">
          <Text type="secondary" style={MONO}>
            —
          </Text>
        </Tooltip>
      ),
    },
    {
      title: "换手率",
      width: 90,
      align: "right",
      render: () => (
        <Tooltip title="换手率：自选快照接口未返回（无数据源）">
          <Text type="secondary" style={MONO}>
            —
          </Text>
        </Tooltip>
      ),
    },
    {
      title: "主力净流入",
      width: 110,
      align: "right",
      render: () => (
        <Tooltip title="主力净流入：自选快照接口未返回（无数据源）">
          <Text type="secondary" style={MONO}>
            —
          </Text>
        </Tooltip>
      ),
    },
    {
      title: "因子信号",
      width: 100,
      render: (_, row) => {
        const score = compositeOf(row.ticker);
        const badge = signalBadge(score);
        return (
          <Tooltip
            title={
              score === null
                ? "综合分无数据源（因子矩阵未覆盖该标的）"
                : `综合分 = 因子 z 均值 ${numOr(score, 3)}（阈值 ±0.25）`
            }
          >
            <Tag color={badge.color}>{badge.text}</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: "自选",
      width: 70,
      align: "center",
      render: (_, row) => (
        <Tooltip title="本地标记（未持久化到服务端）">
          <Typography.Link
            onClick={(event) => {
              event.stopPropagation();
              setStarred((prev) => ({ ...prev, [row.ticker]: !prev[row.ticker] }));
            }}
          >
            {starred[row.ticker] ? "★" : "☆"}
          </Typography.Link>
        </Tooltip>
      ),
    },
  ];

  /* ── sec-4 多因子信号表 ── */
  const availableFactors = (factorIndex && factorIndex.names) || [];
  const activeColumns = FACTOR_COLUMNS.filter((column) => availableFactors.indexOf(column.factor) >= 0);
  const factorRows = (factorIndex ? factorIndex.tickers : []).map((code) => ({ ticker: code }));
  const factorTableColumns = [
    {
      title: "代码",
      dataIndex: "ticker",
      width: 120,
      fixed: "left",
      render: (value) => (
        <Text strong style={MONO}>
          {fmt.dash(value)}
        </Text>
      ),
    },
    {
      title: "名称",
      width: 90,
      render: () => (
        <Tooltip title="证券简称：因子矩阵接口未返回（无数据源）">
          <Text type="secondary">—</Text>
        </Tooltip>
      ),
    },
    ...FACTOR_COLUMNS.map((column) => ({
      title: (
        <Tooltip title={`${column.header} ← ${column.factor}${availableFactors.indexOf(column.factor) >= 0 ? "（workbench/factors z）" : "：该因子无数据源"}`}>
          <span>{column.header}</span>
        </Tooltip>
      ),
      key: column.factor,
      width: 90,
      align: "right",
      render: (_, row) => factorCell(factorOf(row.ticker, column.factor)),
    })),
    {
      title: "综合分",
      width: 100,
      align: "right",
      render: (_, row) => {
        const score = compositeOf(row.ticker);
        return (
          <Text strong style={{ ...MONO, color: score === null ? undefined : upDownColor(score) }}>
            {score === null ? "—" : fmt.signed(score, 2)}
          </Text>
        );
      },
    },
    {
      title: "信号",
      width: 90,
      render: (_, row) => {
        const badge = signalBadge(compositeOf(row.ticker));
        return <Tag color={badge.color}>{badge.text}</Tag>;
      },
    },
  ];

  /* ── sec-5 板块清单 ── */
  const plateList = asArray(plates.value && plates.value.ok && plates.value.data && plates.value.data.plate_list);

  /* ── sec-6 数据源健康 ── */
  const probeData = (br && br.sources && br.sources.workbench && br.sources.workbench.data) || null;
  const probeList = asArray(probeData && probeData.sources);
  const healthCards = probeList.map((source) => {
    const status = String(source.status || "").toLowerCase();
    const ok = status === "ok";
    const warn = status === "warn";
    return {
      key: source.key || source.label,
      name: String(source.label || source.key || "—"),
      statusText: ok ? "正常" : warn ? "警告" : status === "fail" ? "失败" : String(source.status || "未知"),
      status: ok ? "success" : warn ? "warning" : "error",
      detail: String(source.detail || "—"),
      fix: ok ? null : String(source.fix || "—"),
    };
  });
  if (mt) {
    healthCards.push({
      key: "dsh-quant-data-mcp",
      name: "dsh-quant-data-mcp（workbench 工具面）",
      statusText: mt.workbenchUp ? "在线" : "不可达",
      status: mt.workbenchUp ? "success" : "error",
      detail: `MCP 工具域 · ${fmt.dash(mt.toolDomains)} 域 · ${fmt.dash(mt.toolTotal)} 个工具 · 今日调用 ${
        mt.mcp ? mt.mcp.calls : "—"
      } 次 · 失败 ${mt.mcp ? mt.mcp.errors : "—"} 次 · 平均 ${fmt.dash(mt.mcp && mt.mcp.avgMs)}ms`,
      fix: null,
    });
  }

  const mode = String((ov && ov.mode) || "").toUpperCase();

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {/* 工具条（对应设计稿 .pagehead + .seg + 顶栏权益/三通道） */}
      <ProCard
        bordered
        bodyStyle={{ padding: "10px 16px" }}
        loading={overview.loading}
        title={
          <Title level={5} style={{ margin: 0 }}>
            行情与信号
          </Title>
        }
        extra={
          <Space size={10} wrap>
            <Badge status={mode === "live" ? "error" : "processing"} text={mode === "live" ? "LIVE 实盘" : mode ? "SIM 模拟盘" : "模式未知"} />
            <Text style={{ ...MONO, fontSize: 12 }}>权益 {fmt.money(equity.current)}</Text>
            <Text style={{ ...MONO, fontSize: 12, color: dailyPct === null ? C.faint : upDownColor(dailyPct) }}>
              日内 {dailyPct === null ? "无数据源（台账 <2 个点位）" : fmt.signed(dailyPct, 2, "%")}
            </Text>
            <Tag color={mt && Number.isFinite(Number(mcpMs)) ? "success" : "warning"}>
              MCP {mt && Number.isFinite(Number(mcpMs)) ? `${fmt.num(mcpMs, 0)}ms` : "无数据源"}
            </Tag>
            <Tooltip title={sdkReason}>
              <Tag color="warning">SDK 无数据源</Tag>
            </Tooltip>
            <Tooltip title={headlessReason}>
              <Tag color="warning">Headless 无数据源</Tag>
            </Tooltip>
            <Typography.Link onClick={refreshAll}>刷新</Typography.Link>
          </Space>
        }
      >
        <Space size={12} wrap>
          <Input.Search
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onSearch={pickTicker}
            placeholder="输入代码，如 SH.600519"
            style={{ width: 240 }}
            enterButton="切换"
          />
          <Segmented
            options={PERIODS}
            value={period}
            onChange={(value) => setPeriod(String(value))}
          />
          <Text type="secondary" style={{ ...MONO, fontSize: 12 }}>
            数据源 {barsData ? String(barsData.source || "—") : "—"} · as_of{" "}
            {barsData ? fmt.stamp(barsData.as_of) : "—"} · K 线 {bars.length} 根
          </Text>
        </Space>
      </ProCard>

      {/* sec-1 K 线主图 */}
      <ProCard
        title={
          <Space size={12} wrap>
            <span style={{ fontWeight: 600 }}>{ticker}</span>
            <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
              {period}
            </Text>
            <Text style={{ ...MONO, fontSize: 22, fontWeight: 700, color: changePct === null ? C.faint : upDownColor(changePct) }}>
              {lastBar ? numOr(lastBar.c) : "—"}
            </Text>
            <Text style={{ ...MONO, fontSize: 13, color: changePct === null ? C.faint : upDownColor(changePct) }}>
              {changePct === null ? "无数据源" : fmt.signed(changePct, 2, "%")}
            </Text>
            <Text type="secondary" style={{ ...MONO, fontSize: 12, fontWeight: 400 }}>
              昨收 {prevBar ? numOr(prevBar.c) : "—"}
            </Text>
          </Space>
        }
        bordered
        loading={barsHook.loading}
        extra={
          <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
            {barsData ? `${String(barsData.source || "—")} · as_of ${fmt.stamp(barsData.as_of)} · ${bars.length} 根` : "—"}
          </Text>
        }
      >
        {barsHook.error ? (
          <BlockError name={`K 线（${ticker} ${period} · /api/v3/market）`} error={barsHook.error} />
        ) : barsHook.value && barsHook.value.ok === false ? (
          <NoSource what={`K 线（${ticker} ${period}）`} why={errText(barsHook.value)} />
        ) : bars.length < 2 ? (
          <NoSource what={`K 线（${ticker} ${period}）`} why="接口返回的 K 线不足 2 根，无法绘图" />
        ) : (
          <>
            <CandleChart bars={bars} height={320} />
            <Descriptions
              size="small"
              column={7}
              colon={false}
              style={{ marginTop: 10 }}
              items={[
                { key: "o", label: "开", children: <span style={MONO}>{numOr(lastBar.o)}</span> },
                { key: "h", label: "高", children: <span style={MONO}>{numOr(lastBar.h)}</span> },
                { key: "l", label: "低", children: <span style={MONO}>{numOr(lastBar.l)}</span> },
                {
                  key: "c",
                  label: "收",
                  children: (
                    <span style={{ ...MONO, color: upDownColor(Number(lastBar.c) - Number(lastBar.o)) }}>
                      {numOr(lastBar.c)}
                    </span>
                  ),
                },
                { key: "v", label: "量", children: <span style={MONO}>{numOr(lastBar.v, 0)}</span> },
                { key: "ma5", label: "MA5", children: <span style={MONO}>{numOr(ma5[ma5.length - 1])}</span> },
                { key: "ma20", label: "MA20", children: <span style={MONO}>{numOr(ma20[ma20.length - 1])}</span> },
              ]}
            />
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
              共 {bars.length} 根真实 {period} K（含成交量，上游未标注单位，原样展示）· 最新 bar {fmt.stamp(lastBar.t)} ·
              MA5 / MA20 为真实收盘均值 · 买卖点信号：无数据源（接口未返回逐 bar 信号）
            </Text>
          </>
        )}
      </ProCard>

      {/* sec-2 盘口深度 */}
      <ProCard
        title="盘口深度"
        bordered
        loading={orderbook.loading}
        extra={
          <Space size={10} wrap>
            <Text type="secondary" style={{ ...MONO, fontSize: 12 }}>
              {ticker}
            </Text>
            <Text style={{ ...MONO, fontSize: 13, color: changePct === null ? C.faint : upDownColor(changePct) }}>
              最新 {lastBar ? numOr(lastBar.c) : "—"} · {changePct === null ? "无数据源" : fmt.signed(changePct, 2, "%")}
            </Text>
          </Space>
        }
      >
        {orderbook.error ? (
          <BlockError name="盘口深度（/api/v3/orderbook）" error={orderbook.error} />
        ) : orderbook.value && orderbook.value.ok === false ? (
          <>
            <Alert
              type="warning"
              showIcon
              message={noSourceText("五档盘口（委买 / 委卖 / 委比 / 内外盘）", errText(orderbook.value))}
              description={
                <Text type="secondary" style={{ fontSize: 11 }}>
                  富途实时行情权限未开通（errcode=-9 realtime quote permission required）：五档快照与委比 / 内外盘均无数据源。
                  上方最新价为真实最近收盘（/api/v3/market），不是盘口价。
                </Text>
              }
            />
            <Descriptions
              size="small"
              column={3}
              colon={false}
              style={{ marginTop: 12 }}
              items={[
                { key: "last", label: "最新价（收盘）", children: <span style={MONO}>{lastBar ? numOr(lastBar.c) : "—"}</span> },
                {
                  key: "chg",
                  label: "涨跌幅",
                  children: (
                    <span style={{ ...MONO, color: changePct === null ? C.faint : upDownColor(changePct) }}>
                      {changePct === null ? "无数据源" : fmt.signed(changePct, 2, "%")}
                    </span>
                  ),
                },
                { key: "prev", label: "昨收", children: <span style={MONO}>{prevBar ? numOr(prevBar.c) : "—"}</span> },
                { key: "bid", label: "委比", children: <Text type="secondary">无数据源</Text> },
                { key: "inner", label: "内盘（主动卖）", children: <Text type="secondary">无数据源</Text> },
                { key: "outer", label: "外盘（主动买）", children: <Text type="secondary">无数据源</Text> },
              ]}
            />
          </>
        ) : (
          <NoSource what="五档盘口" why="接口返回的盘口字段未包含可用档位" />
        )}
      </ProCard>

      {/* sec-3 自选行情表 */}
      <ProCard
        title="自选行情"
        bordered
        loading={watchlist.loading}
        extra={
          <Space size={10} wrap>
            <Input
              size="small"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              placeholder="筛选代码"
              style={{ width: 140 }}
            />
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {watchRows.length} 只 · 快照 as_of {watchRows.length ? fmt.stamp(watchRows[0].asOf) : "—"}
            </Text>
          </Space>
        }
      >
        {watchlist.error ? (
          <BlockError name="自选行情（/api/v3/market/watchlist）" error={watchlist.error} />
        ) : watchRows.length === 0 ? (
          <NoSource what="自选池" why={errText(watchlist.value, "GET /api/v3/market/watchlist 未返回任何标的")} />
        ) : (
          <>
            <Table
              size="small"
              rowKey="ticker"
              dataSource={filteredWatch}
              columns={watchColumns}
              pagination={false}
              scroll={{ x: 960 }}
              onRow={(row) => ({ onClick: () => pickTicker(row.ticker), style: { cursor: "pointer" } })}
              locale={{ emptyText: <Text type="secondary">无匹配标的</Text> }}
            />
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
              点击行切换主图与盘口标的 · 收盘 / 涨跌幅 / 20 日动量来自 /api/v3/market/watchlist 真实快照 ·
              证券简称 / 量比 / 换手率 / 主力净流入：无数据源（自选快照接口未返回）
              {asArray(watchlist.value && watchlist.value.errors).length
                ? ` · 取数失败标的：${asArray(watchlist.value.errors).map((item) => item.ticker).join("、")}`
                : ""}
            </Text>
          </>
        )}
      </ProCard>

      {/* sec-4 多因子信号表 */}
      <ProCard
        title="多因子信号"
        bordered
        loading={factors.loading}
        extra={
          <Text type="secondary" style={{ fontSize: 11 }}>
            {factorIndex
              ? `横截面因子 z 矩阵 · ${factorIndex.tickers.length} 只 × ${factorIndex.names.length} 因子 · as_of ${String(
                  factorIndex.matrix.as_of || "—",
                )}`
              : "无数据源"}
          </Text>
        }
      >
        {factors.error ? (
          <BlockError name="多因子信号（/api/v3/factors/matrix）" error={factors.error} />
        ) : !factorIndex ? (
          <NoSource what="多因子信号" why={errText(factors.value, "GET /api/v3/factors/matrix 失败")} />
        ) : (
          <>
            <Table
              size="small"
              rowKey="ticker"
              dataSource={factorRows}
              columns={factorTableColumns}
              pagination={false}
              scroll={{ x: 1100 }}
            />
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
              列取值（真实因子）：{activeColumns.map((column) => `${column.header}←${column.factor}`).join(" / ") || "无数据源"}
              {activeColumns.length < FACTOR_COLUMNS.length ? " · 其余列该因子无数据源" : ""} · 综合分＝已返回因子 z
              均值 · 信号阈值 ±0.25 · 来源 {String(factorIndex.matrix.source || "/api/v3/factors/matrix")} · as_of{" "}
              {String(factorIndex.matrix.as_of || "—")}
              {factors.value && factors.value.ok && factors.value.ic && factors.value.ic.ok
                ? ` · IC（${String(factors.value.ic.factor || "—")}，forward ${fmt.dash(
                    factors.value.ic.forwardDays,
                  )} 日）均值 ${numOr(factors.value.ic.meanIc, 4)} · IR ${numOr(
                    factors.value.ic.ir,
                    2,
                  )} · 样本 ${fmt.dash(factors.value.ic.observations)} 期`
                : ""}
            </Text>
          </>
        )}
      </ProCard>

      {/* sec-5 板块清单 */}
      <ProCard
        title="板块热力图"
        bordered
        loading={plates.loading}
        extra={
          <Text type="secondary" style={{ fontSize: 11 }}>
            {plateList.length ? `板块清单 ${plateList.length} 个（显示前 ${Math.min(24, plateList.length)} 个）` : "无数据源"}
          </Text>
        }
      >
        {plates.error ? (
          <BlockError name="板块清单（/api/v3/plates）" error={plates.error} />
        ) : plateList.length === 0 ? (
          <NoSource what="板块清单" why={errText(plates.value, "GET /api/v3/plates 未返回板块清单")} />
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 8 }}>
              {plateList.slice(0, 24).map((plate) => (
                <Tooltip
                  key={plate.code || plate.plate_id}
                  title={`板块 ${plate.sc_name || plate.plate_name || plate.code} · 涨跌幅无数据源（需富途实时行情权限）· 代码 ${plate.code}`}
                >
                  <Card size="small" bodyStyle={{ padding: "8px 10px" }}>
                    <Text style={{ fontSize: 12, display: "block" }} ellipsis>
                      {plate.sc_name || plate.plate_name || plate.code}
                    </Text>
                    <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
                      涨跌幅 无数据源
                    </Text>
                  </Card>
                </Tooltip>
              ))}
            </div>
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
              板块涨跌幅 / 色阶：无数据源（/api/v3/plates 只返回板块清单，涨跌幅需富途实时行情权限）· 来源
              /api/v3/plates?market=SH&plate_class=ALL
            </Text>
          </>
        )}
      </ProCard>

      {/* sec-6 数据源健康 */}
      <ProCard
        title="数据源健康"
        bordered
        loading={brain.loading || metrics.loading}
        extra={
          <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
            {probeData ? `检查于 ${fmt.stamp(probeData.checked_at)}` : "无数据源"}
          </Text>
        }
      >
        {brain.error ? (
          <BlockError name="数据源健康（/api/v3/brain）" error={brain.error} />
        ) : healthCards.length === 0 ? (
          <NoSource what="数据源健康" why={errText(brain.value, "workbench sources 未返回可用数据源")} />
        ) : (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
              {healthCards.map((card) => (
                <Card key={card.key} size="small">
                  <Space size={8} style={{ width: "100%", justifyContent: "space-between" }}>
                    <Text strong style={{ fontSize: 12 }}>
                      {card.name}
                    </Text>
                    <Badge status={card.status} text={<span style={{ fontSize: 12 }}>{card.statusText}</span>} />
                  </Space>
                  <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 6 }}>
                    {card.detail}
                  </Text>
                  {card.fix ? (
                    <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 4 }}>
                      修复：{card.fix}
                    </Text>
                  ) : null}
                </Card>
              ))}
            </div>
            <Divider style={{ margin: "10px 0" }} />
            <Text type="secondary" style={{ fontSize: 11 }}>
              真实数据源探测 {probeList.length} 项 · 来源 workbench sources（/api/v3/brain）
              {probeData && probeData.summary
                ? ` · ok ${fmt.dash(probeData.summary.ok)} / warn ${fmt.dash(probeData.summary.warn)} / fail ${fmt.dash(probeData.summary.fail)}`
                : ""}{" "}
              · 调用计数 {mt ? `${mt.mcp ? mt.mcp.calls : 0} 次 / 失败 ${mt.mcp ? mt.mcp.errors : 0} 次 / 平均 ${fmt.dash(mt.mcp && mt.mcp.avgMs)}ms` : "无数据源"}
              （/api/v3/metrics）
            </Text>
          </>
        )}
      </ProCard>
      {/* 数据域：把后端外部数据源端点（news/financials/tushare/openbb/spot）全部对到界面 */}
      <DataDomainCard />
    </Space>
  );
}
