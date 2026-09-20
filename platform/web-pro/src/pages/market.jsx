// V3 工作台「行情与信号」页（Ant Design Pro）。
// 功能模块与设计稿版 /v3/market.html **一一对应**（binder: platform/web/public/v3/market.js）：
//   sec-0 工具条：三市场切换（A股 SH / 港股 HK / 美股 US）+ 交易时段徽章（/api/v3/markets/calendar）
//   sec-1 K 线主图（真实 o/h/l/c/v 蜡烛 + 成交量、标的头现价/涨跌幅/昨收、MA5/MA20 读数）
//   sec-2 盘口深度（/api/v3/orderbook 真实五档；A 股实时行情权限缺口 → 无数据源 + errcode 原文）
//   sec-3 自选行情表（/api/v3/market/watchlist：真实收盘 / 涨跌幅 / 20 日动量）
//   sec-4 多因子信号表（/api/v3/factors/matrix 的 z 矩阵：价值←pb / 成长←peg / 动量←mom_20 /
//        质量←mdd_60 / 情绪←rsi_14 / 另类←liq_ratio + 综合分 + 信号）
//   sec-5 板块热力图（/api/v3/plates 真实清单；**只对 A 股请求**，其它市场不发无效请求）
//   sec-6 数据源健康（workbench sources 真实探测 + /api/v3/metrics 调用计数）
// 三市场参数：默认标的 SH.600000 / HK.00700 / US.NVDA；切换市场后 K 线、盘口、自选、因子请求
// 全部带上对应市场与标的。历史 K 线可用；实时快照 / 盘口在富途实时行情权限缺失时原样报错。
// 数据：GET /api/v3/*（**只读，绝不在加载/轮询里发写请求**）；取不到显式「无数据源 + 原因」。
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

/* ── 三市场（A股 / 港股 / 美股）──────────────────────────────────────────────
 * 默认标的取合约里给定的真实代码；切换市场 = 主图 / 盘口 / 自选 / 因子请求参数一起切。 */
const MARKETS = [
  { value: "SH", label: "A股 SH", ticker: "SH.600000", name: "A股" },
  { value: "HK", label: "港股 HK", ticker: "HK.00700", name: "港股" },
  { value: "US", label: "美股 US", ticker: "US.NVDA", name: "美股" },
];
const MARKET_OPTIONS = MARKETS.map((market) => ({ value: market.value, label: market.label }));
const marketOf = (value) => MARKETS.find((market) => market.value === value) || MARKETS[0];
const marketName = (value) => marketOf(value).name;

/** session → 徽章（open 绿 / lunch 琥珀 / pre 蓝 / post 灰 / closed 灰），与接口契约一一对应。 */
const SESSION_TAG = {
  open: { color: "success", text: "交易中" },
  lunch: { color: "warning", text: "午间休市" },
  pre: { color: "processing", text: "盘前" },
  post: { color: "default", text: "盘后" },
  closed: { color: "default", text: "休市" },
};
const SESSION_TEXT = {
  open: "交易中",
  lunch: "午间休市",
  pre: "盘前",
  post: "盘后",
  closed: "休市",
};

const asArray = (value) => (Array.isArray(value) ? value : []);

function errText(payload, fallback = "接口未返回原因") {
  const error = (payload && payload.error) || {};
  return String(error.message || error.code || fallback);
}

/* ── 本页新增端点的只读取数（GET /api/v3/*）─────────────────────────────────
 * 为什么不直接用共享层 useV3：本页需要两件它没有的能力——
 *   (1) **可跳过**的取数（板块只对 A 股请求，其它市场一个请求都不发）；
 *   (2) 区分「端点未上线 / 返回非 JSON」与「接口正常但没数据」——共享层把非 JSON 响应折成
 *       value=null，页面就没法如实写出原因（本服务未注册的 /api/v3/* 会落到 SPA 兜底返回 HTML）。
 * 鉴权与查询串拼法沿用共享层同一约定（localStorage.trading_token → Authorization: Bearer）。
 * 只读：本 hook 只发 GET，绝不在加载或轮询里发写请求。 */
function useReadV3(path, params = {}, enabled = true) {
  const [state, setState] = React.useState({ loading: Boolean(enabled), value: null, error: null });
  const seqRef = React.useRef(0);
  const key = JSON.stringify(params ?? {});
  const read = React.useCallback(
    async (refresh = false) => {
      seqRef.current += 1;
      const seq = seqRef.current;
      if (!enabled) {
        setState({ loading: false, value: null, error: null });
        return;
      }
      setState((prev) => ({ ...prev, loading: true, error: null }));
      const query = new URLSearchParams();
      for (const [name, value] of Object.entries(params ?? {})) {
        if (value === undefined || value === null || value === "") continue;
        query.set(name, String(value));
      }
      if (refresh) query.set("_", String(Date.now()));
      const suffix = query.toString();
      // 一律同源相对路径：控制台只与本服务通信，不接受任何外部基地址覆盖
      const url = `/api/v3/${path}${suffix ? `?${suffix}` : ""}`;
      const headers = {};
      try {
        const token = window.localStorage.getItem("trading_token");
        if (token) headers.Authorization = `Bearer ${token}`;
      } catch {
        /* localStorage 不可用时按未鉴权处理 */
      }
      let response = null;
      try {
        response = await window.fetch(url, { headers });
      } catch (error) {
        if (seqRef.current === seq) {
          setState({ loading: false, value: null, error: `网络不可达（${url}）：${String(error?.message || error)}` });
        }
        return;
      }
      const contentType = String(response.headers.get("content-type") || "");
      let body = null;
      try {
        body = await response.json();
      } catch {
        body = null;
      }
      if (seqRef.current !== seq) return;
      if (response.status === 401) {
        setState({ loading: false, value: null, error: "需要访问令牌：右上角「令牌」填入服务配置的 token" });
        return;
      }
      if (body === null || typeof body !== "object") {
        setState({
          loading: false,
          value: null,
          error: `HTTP ${response.status} 返回的不是 JSON（content-type=${contentType || "未标注"}）：该端点可能尚未上线`,
        });
        return;
      }
      if (!response.ok) {
        setState({ loading: false, value: body, error: body?.error?.message ? String(body.error.message) : `HTTP ${response.status}` });
        return;
      }
      setState({ loading: false, value: body, error: null });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [path, key, enabled],
  );
  React.useEffect(() => {
    read(false);
    return () => {
      seqRef.current += 1;
    };
  }, [read]);
  return { ...state, refresh: () => read(true) };
}

/** 休市原因：周末 / 节假日 / 非交易时段（字段优先，不臆造）。 */
function closedReason(state) {
  if (!state) return "接口未返回该市场";
  const holiday = state.holiday;
  const holidayText =
    holiday === null || holiday === undefined || holiday === ""
      ? ""
      : typeof holiday === "string"
        ? holiday
        : String(holiday.name || holiday.label || holiday.date || "");
  if (holidayText) return `节假日休市：${holidayText}`;
  if (state.isTradingDay === false) return state.label ? String(state.label) : "休市（非交易日：周末或节假日）";
  if (state.isTradingDay === true) return `非交易时段（${state.label ? String(state.label) : "今日为交易日，当前不在交易时段"}）`;
  return state.label ? String(state.label) : "休市（接口未返回 isTradingDay 与 label，无法判定具体原因）";
}

/** 各 session 的口径说明（同一份文案既用于徽章提示，也用于页面明示，避免两处漂移）。 */
function sessionReason(state) {
  if (!state) return "接口未返回该市场";
  const session = String(state.session || "").toLowerCase();
  if (session === "closed") return closedReason(state);
  if (session === "pre") return "盘前：尚未进入连续竞价时段";
  if (session === "lunch") return `午间休市：连续竞价暂停（${asArray(state.lunch).join("–") || "接口未返回午休时段"}）`;
  if (session === "post") return "盘后：当日交易时段已结束";
  if (session === "open") return `连续竞价进行中（${state.open || "—"}–${state.close || "—"}）`;
  return `接口返回未知 session=${String(state.session)}`;
}

// fmt.num 走 Number()，null/'' 会被算成 0；缺失值必须显式写「—」，不能变成 0.00
const numOr = (value, digits = 2) =>
  value === null || value === undefined || value === "" || !Number.isFinite(Number(value))
    ? "—"
    : Number(value).toFixed(digits);

/** 价格原样展示（不四舍五入到固定位数，避免把真实报价修成假的精度）。 */
const priceText = (value) =>
  value === null || value === undefined || value === "" || !Number.isFinite(Number(value))
    ? "—"
    : String(Number(value));

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
  const [market, setMarket] = React.useState("SH");
  const [ticker, setTicker] = React.useState(marketOf("SH").ticker);
  const [input, setInput] = React.useState(marketOf("SH").ticker);
  const [period, setPeriod] = React.useState("1d");
  const [starred, setStarred] = React.useState({});
  const [keyword, setKeyword] = React.useState("");
  const autoRef = React.useRef(true);

  const overview = useV3("overview", {});
  const metrics = useV3("metrics", {});
  const brain = useV3("brain", {});
  // 自选 / 因子请求参数随市场切换（market 一并传给后端；后端未按市场过滤时下面会如实标注）
  const watchlist = useV3("market/watchlist", { n: 6, market });
  // 板块只对 A 股请求：其它市场**不发请求**，直接显示无数据源与原因
  const plates = useReadV3("plates", { market: "SH", plate_class: "ALL" }, market === "SH");
  // 交易时段（只读 GET；每 60 秒刷新一次，绝不发写请求）
  const calendar = useReadV3("markets/calendar", { markets: "SH,HK,US" });

  const ov = overview.value && overview.value.ok ? overview.value : null;
  const mt = metrics.value && metrics.value.ok ? metrics.value : null;
  const br = brain.value && brain.value.ok ? brain.value : null;
  const watchRows = asArray(watchlist.value && watchlist.value.ok && watchlist.value.rows);
  const marketRows = watchRows.filter((row) => String(row.ticker || "").toUpperCase().startsWith(`${market}.`));

  // 自选快照到达后，未人工切换过标的时默认跟随**当前市场**的自选首只
  const marketRowsKey = marketRows.map((row) => row.ticker).join(",");
  React.useEffect(() => {
    if (!autoRef.current || marketRows.length === 0) return;
    const first = marketRows[0].ticker;
    if (first) {
      setTicker(first);
      setInput(first);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [marketRowsKey, market]);

  const barsHook = useV3("market", { ticker, period, limit: 160 });
  const barsData = barsHook.value && barsHook.value.ok ? barsHook.value.data : null;
  const bars = asArray(barsData && barsData.bars);

  // 因子矩阵按当前市场的自选标的请求；该市场无自选标的时退化为该市场默认标的
  const factorTickers = marketRows.length
    ? marketRows.map((row) => row.ticker).join(",")
    : marketOf(market).ticker;
  const factors = useV3("factors/matrix", { tickers: factorTickers, market });
  const orderbook = useV3("orderbook", { ticker });

  // 交易时段徽章只在只读 GET 上轮询（60s），组件卸载即清理
  React.useEffect(() => {
    const timer = window.setInterval(() => calendar.refresh(), 60000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refreshAll = () => {
    overview.refresh();
    metrics.refresh();
    brain.refresh();
    watchlist.refresh();
    plates.refresh();
    barsHook.refresh();
    factors.refresh();
    orderbook.refresh();
    calendar.refresh();
  };

  const pickTicker = (next) => {
    const value = String(next || "").trim();
    if (!value) return;
    autoRef.current = false;
    setTicker(value);
    setInput(value);
  };

  /** 切市场：主图标的回到该市场默认代码，自选/因子请求参数随之切换。 */
  const pickMarket = (next) => {
    const target = marketOf(String(next || "SH"));
    autoRef.current = false;
    setMarket(target.value);
    setTicker(target.ticker);
    setInput(target.ticker);
  };

  /* ── sec-0 交易时段 ── */
  const calMarkets = (calendar.value && calendar.value.ok && calendar.value.markets) || null;
  const calState = calMarkets ? calMarkets[market] || null : null;
  const session = String((calState && calState.session) || "").toLowerCase();
  const sessionTag = SESSION_TAG[session] || { color: "default", text: "无数据源" };
  const badgeText =
    session === "open"
      ? `交易中（${(calState && calState.open) || "—"}–${(calState && calState.close) || "—"}）`
      : sessionTag.text;
  const tzText = (calendar.value && calendar.value.ok && calendar.value.timezone) || null;
  const marketTz = (calState && calState.timezone) || null;
  const nextOpenText = calState && calState.nextOpen ? fmt.stamp(calState.nextOpen) : "—";
  const holidaysLoaded = calendar.value && calendar.value.ok ? calendar.value.holidays_loaded : undefined;
  const lunchText = calState ? asArray(calState.lunch).join("–") : "";
  const calendarTip = [
    `当前市场：${marketName(market)}（${market}）`,
    `session=${session || "未返回"} → ${SESSION_TEXT[session] || "无数据源"}`,
    calState
      ? `今日是否交易日：${calState.isTradingDay === true ? "是" : calState.isTradingDay === false ? "否" : "未返回"} · 开市 ${calState.open || "—"} · 收市 ${calState.close || "—"} · 午休 ${lunchText || "—"}`
      : "该市场未在接口返回中",
    `下次开市 ${nextOpenText}`,
    `时间均按接口返回时区 ${tzText || "（未返回）"}${marketTz ? ` · 该市场本地时区 ${marketTz}` : ""}`,
    holidaysLoaded === true
      ? "节假日表：已加载"
      : holidaysLoaded === false
        ? "节假日表未加载，仅按周末判断（节假日可能被误判为交易日）"
        : "节假日表加载状态：接口未返回",
    `最近判定 as_of ${calendar.value && calendar.value.ok ? fmt.stamp(calendar.value.as_of) : "—"}`,
  ].join("\n");

  /* ── sec-1 K 线主图与标的头 ── */
  const closes = bars.map((bar) => Number(bar.c));
  const ma5 = sma(closes, 5);
  const ma20 = sma(closes, 20);
  const lastBar = bars.length ? bars[bars.length - 1] : null;
  const prevBar = bars.length > 1 ? bars[bars.length - 2] : null;
  const changePct =
    lastBar && prevBar && Number(prevBar.c) ? (Number(lastBar.c) / Number(prevBar.c) - 1) * 100 : null;
  const barsSource = barsData ? String(barsData.source || "—") : null;

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

  /* ── sec-2 盘口深度（真实五档；A 股权限缺口时走 error 分支） ── */
  const obLevels = (() => {
    const payload = orderbook.value && orderbook.value.ok ? orderbook.value.data : null;
    if (!payload) return null;
    const groups = Array.isArray(payload) ? payload : [payload];
    for (const group of groups) {
      const books = asArray(group && group.books);
      for (const book of books) {
        const asks = asArray(book.ask_list);
        const bids = asArray(book.bid_list);
        if (asks.length || bids.length) {
          return {
            exchange: book.exchange || null,
            asks: asks.slice(0, 5),
            bids: bids.slice(0, 5),
          };
        }
      }
    }
    return { exchange: null, asks: [], bids: [] };
  })();
  const obHasLevels = Boolean(obLevels && (obLevels.asks.length || obLevels.bids.length));

  const levelColumns = (side) => [
    {
      title: "档位",
      width: 60,
      render: (_, __, index) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {side === "ask" ? `卖${(obLevels ? obLevels.asks.length : 0) - index}` : `买${index + 1}`}
        </Text>
      ),
    },
    {
      title: "价格",
      align: "right",
      render: (_, row) => (
        <span style={{ ...MONO, color: side === "ask" ? C.down : C.up }}>{priceText(row.price)}</span>
      ),
    },
    {
      title: "数量",
      align: "right",
      render: (_, row) => <span style={MONO}>{numOr(row.volume, 0)}</span>,
    },
    {
      title: "委托笔数",
      align: "right",
      render: (_, row) => (
        <span style={MONO}>{row.order_count === 0 ? "0（未披露）" : numOr(row.order_count, 0)}</span>
      ),
    },
  ];

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
  const watchMarkets = Array.from(
    new Set(watchRows.map((row) => String(row.ticker || "").split(".")[0]).filter(Boolean)),
  ).sort();
  // 接口未按市场过滤时（自选池当前按平台配置，A 股为主）如实标注，不假装是该市场的数据
  const watchOffMarket = watchRows.length > 0 && marketRows.length === 0;

  const marketTag = (code) => {
    const prefix = String(code || "").split(".")[0];
    const own = prefix === market;
    return (
      <Tag color={own ? "blue" : "default"} style={{ marginInlineEnd: 6 }}>
        {prefix}
      </Tag>
    );
  };

  const watchColumns = [
    {
      title: "代码",
      dataIndex: "ticker",
      width: 170,
      render: (value) => (
        <Text strong style={MONO}>
          {marketTag(value)}
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
  const factorOffMarket =
    factorRows.length > 0 &&
    !factorRows.some((row) => String(row.ticker).toUpperCase().startsWith(`${market}.`));
  const factorTableColumns = [
    {
      title: "代码",
      dataIndex: "ticker",
      width: 170,
      fixed: "left",
      render: (value) => (
        <Text strong style={MONO}>
          {marketTag(value)}
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

  /* ── sec-5 板块清单（只有 A 股会到这里） ── */
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
      {/* sec-0 工具条（三市场切换 + 交易时段徽章 + 数据源标注）
          有意不加 loading：工具条承载市场切换与交易时段徽章（来自轻量端点），
          不能被较慢的 overview 请求盖成骨架屏；权益 / MCP 等读数在到达前按「—／无数据源」如实显示。 */}
      <ProCard
        bordered
        bodyStyle={{ padding: "10px 16px" }}
        title={
          <Space size={10}>
            <Title level={5} style={{ margin: 0 }}>
              行情与信号
            </Title>
            <Tag color="blue">{`当前市场 ${marketName(market)} ${market}`}</Tag>
          </Space>
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
        <Space direction="vertical" size={8} style={{ width: "100%" }}>
          <Space size={12} wrap align="center">
            <Segmented options={MARKET_OPTIONS} value={market} onChange={pickMarket} />
            <Input.Search
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onSearch={pickTicker}
              placeholder={`输入代码，如 ${marketOf(market).ticker}`}
              style={{ width: 250 }}
              enterButton="切换"
            />
            <Segmented options={PERIODS} value={period} onChange={(value) => setPeriod(String(value))} />
            <Text type="secondary" style={{ ...MONO, fontSize: 12 }}>
              {`${marketOf(market).label} · 默认标的 ${marketOf(market).ticker}`}
            </Text>
          </Space>

          <Space size={10} wrap align="center">
            <Tooltip title={<div style={{ whiteSpace: "pre-line" }}>{calendarTip}</div>}>
              <Tag color={sessionTag.color} style={{ marginInlineEnd: 0 }}>
                {badgeText}
              </Tag>
            </Tooltip>
            {session === "closed" || session === "lunch" || session === "post" || session === "pre" ? (
              <Text type="secondary" style={{ fontSize: 12 }}>
                {`原因：${sessionReason(calState)}`}
              </Text>
            ) : null}
            <Text type="secondary" style={{ ...MONO, fontSize: 12 }}>
              {`下次开市 ${nextOpenText}`}
            </Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {`时间均按接口时区 ${tzText || "（未返回）"}${
                marketTz && tzText && marketTz !== tzText ? `（${marketName(market)}本地 ${marketTz}：开收市与下次开市按市场本地时区给出）` : ""
              }`}
            </Text>
            {calendar.error ? (
              <Text style={{ color: C.down, fontSize: 12 }}>{`交易时段：无数据源 · ${calendar.error}`}</Text>
            ) : null}
          </Space>

          <Text type="secondary" style={{ fontSize: 11 }}>
            状态口径（session ← /api/v3/markets/calendar?markets=SH,HK,US）：交易中 open（绿，含开收市时刻）/ 午间休市 lunch（琥珀）
            / 盘前 pre（蓝）/ 盘后 post（灰）/ 休市 closed（灰，附 label 原因）；休市原因区分周末 / 节假日 / 非交易时段。
            {holidaysLoaded === false ? " 本次接口返回 holidays_loaded=false：节假日表未加载，仅按周末判断（节假日可能被误判为交易日）。" : ""}
            {calendar.value && calendar.value.ok
              ? ` 最近判定 as_of ${fmt.stamp(calendar.value.as_of)} · 覆盖市场 ${Object.keys(calMarkets || {}).join(" / ") || "未返回"}。`
              : " 接口未返回可用判定（无数据源），本页不臆测交易状态。"}
          </Text>

          <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
            {`K 线来源 ${barsSource || "—"} · as_of ${barsData ? fmt.stamp(barsData.as_of) : "—"} · ${bars.length} 根 · 实时快照 / 盘口来源 futu/rt_order_book（A 股实时行情权限缺失时原样返回 errcode 错误，不填占位）`}
          </Text>
        </Space>
      </ProCard>

      {/* sec-1 K 线主图 */}
      <ProCard
        title={
          <Space size={12} wrap>
            <span style={{ fontWeight: 600 }}>{ticker}</span>
            <Tag color="blue">{marketName(market)}</Tag>
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
            <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
              {lastBar ? `${session === "open" ? "最近 bar" : "最近收盘"} as_of ${barsData ? fmt.stamp(barsData.as_of) : "—"}` : ""}
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
              来源 {barsSource || "—"}（历史 K 线，日线可用）· as_of {barsData ? fmt.stamp(barsData.as_of) : "—"} ·
              {session === "open"
                ? " 该市场当前交易中，本序列仍是已落库的 K 线，非逐笔实时"
                : ` 该市场当前${SESSION_TEXT[session] || "状态未知"}：上列价格为最近收盘，最新 bar 时点为 ${fmt.stamp(lastBar.t)}`}
              {" · MA5 / MA20 为真实收盘均值 · 买卖点信号：无数据源（接口未返回逐 bar 信号）"}
            </Text>
          </>
        )}
      </ProCard>

      {/* sec-2 盘口深度（真实五档；A 股走权限缺口分支） */}
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
            <Text type="secondary" style={{ fontSize: 11 }}>
              /api/v3/orderbook · 富途实时快照
            </Text>
          </Space>
        }
      >
        {orderbook.error ? (
          <BlockError name={`盘口深度（/api/v3/orderbook · ${ticker}）`} error={orderbook.error} />
        ) : orderbook.value && orderbook.value.ok === false ? (
          <>
            <Alert
              type="warning"
              showIcon
              message={noSourceText("五档盘口（委买 / 委卖 / 委比 / 内外盘）", errText(orderbook.value))}
              description={
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {`${marketName(market)}实时行情权限未开通（富途原样返回 errcode=-9 realtime quote permission required）：五档快照与委比 / 内外盘均无数据源。`}
                  上方最新价为真实最近收盘（{barsSource || "/api/v3/market"}），不是盘口价；A 股实时快照需在富途侧开通行情权限。
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
        ) : obHasLevels ? (
          <>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 12 }}>
              <div>
                <Text strong style={{ fontSize: 12 }}>
                  {`卖盘（ask，前 ${obLevels.asks.length} 档）`}
                </Text>
                <Table
                  size="small"
                  style={{ marginTop: 6 }}
                  rowKey={(_, index) => `ask-${index}`}
                  dataSource={obLevels.asks}
                  columns={levelColumns("ask")}
                  pagination={false}
                  locale={{ emptyText: <Text type="secondary">无数据源（ask_list 为空）</Text> }}
                />
              </div>
              <div>
                <Text strong style={{ fontSize: 12 }}>
                  {`买盘（bid，前 ${obLevels.bids.length} 档）`}
                </Text>
                <Table
                  size="small"
                  style={{ marginTop: 6 }}
                  rowKey={(_, index) => `bid-${index}`}
                  dataSource={obLevels.bids}
                  columns={levelColumns("bid")}
                  pagination={false}
                  locale={{ emptyText: <Text type="secondary">无数据源（bid_list 为空）</Text> }}
                />
              </div>
            </div>
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
              来源 /api/v3/orderbook（富途实时快照 rt_order_book）· 交易所 {obLevels.exchange || "—"} ·
              委托笔数为 0 表示该档上游未披露笔数（原样展示，不填占位）· 委比 / 内外盘：无数据源（快照接口未返回）
              {session !== "open" ? ` · 当前${marketName(market)}${SESSION_TEXT[session] || "状态未知"}，上表为最近一次快照` : ""}
            </Text>
          </>
        ) : (
          <NoSource what={`五档盘口（${ticker}）`} why="接口返回的 books 中 ask_list / bid_list 均为空（无可展示档位）" />
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
              {`${marketName(market)}标的 ${marketRows.length} 只 / 接口返回 ${watchRows.length} 只 · 快照 as_of ${watchRows.length ? fmt.stamp(watchRows[0].asOf) : "—"}`}
            </Text>
          </Space>
        }
      >
        {watchlist.error ? (
          <BlockError name={`自选行情（/api/v3/market/watchlist · market=${market}）`} error={watchlist.error} />
        ) : watchRows.length === 0 ? (
          <NoSource
            what={`${marketName(market)}自选池`}
            why={errText(watchlist.value, `GET /api/v3/market/watchlist?n=6&market=${market} 未返回任何标的`)}
          />
        ) : (
          <>
            {watchOffMarket ? (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 10 }}
                message={noSourceText(`${marketName(market)}自选标的`, `/api/v3/market/watchlist 按平台配置的自选池返回（${watchMarkets.join(" / ")}），当前未按 market 过滤`)}
                description={
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {`本页已按所选市场传参（market=${market}）；下表为接口实际返回的标的（原样展示，未做替换），点击行仍可切换主图与盘口标的。`}
                  </Text>
                }
              />
            ) : null}
            <Table
              size="small"
              rowKey="ticker"
              dataSource={filteredWatch}
              columns={watchColumns}
              pagination={false}
              scroll={{ x: 1010 }}
              onRow={(row) => ({ onClick: () => pickTicker(row.ticker), style: { cursor: "pointer" } })}
              locale={{ emptyText: <Text type="secondary">无匹配标的</Text> }}
            />
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
              点击行切换主图与盘口标的 · 收盘 / 涨跌幅 / 20 日动量来自 /api/v3/market/watchlist 真实快照 ·
              代码前的市场标记为该标的所属市场（蓝色 = 当前选中市场）· 证券简称 / 量比 / 换手率 / 主力净流入：无数据源（自选快照接口未返回）
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
          <BlockError name={`多因子信号（/api/v3/factors/matrix · market=${market}）`} error={factors.error} />
        ) : !factorIndex ? (
          <NoSource
            what={`${marketName(market)}多因子信号`}
            why={errText(factors.value, `GET /api/v3/factors/matrix?tickers=${factorTickers}&market=${market} 失败`)}
          />
        ) : (
          <>
            {factorOffMarket ? (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 10 }}
                message={noSourceText(`${marketName(market)}因子矩阵`, `/api/v3/factors/matrix 当前返回 workbench/factors 覆盖的标的（${Array.from(
                  new Set(factorRows.map((row) => String(row.ticker).split(".")[0])),
                ).join(" / ")}），未覆盖该市场`)}
                description={
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {`本页已按所选市场传参（tickers=${factorTickers} · market=${market}）；下表为接口实际返回的矩阵，原样展示，不做替换或估算。`}
                  </Text>
                }
              />
            ) : null}
            <Table
              size="small"
              rowKey="ticker"
              dataSource={factorRows}
              columns={factorTableColumns}
              pagination={false}
              scroll={{ x: 1110 }}
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

      {/* sec-5 板块清单（仅 A 股请求） */}
      <ProCard
        title="板块热力图"
        bordered
        loading={market === "SH" && plates.loading}
        extra={
          <Text type="secondary" style={{ fontSize: 11 }}>
            {market !== "SH"
              ? "无数据源 · 未发起请求"
              : plateList.length
                ? `板块清单 ${plateList.length} 个（显示前 ${Math.min(24, plateList.length)} 个）`
                : "无数据源"}
          </Text>
        }
      >
        {market !== "SH" ? (
          <Alert
            type="info"
            showIcon
            message={noSourceText(`${marketName(market)}板块数据`, "/api/v3/plates 只对 A 股（market=SH）请求：该市场的板块口径与清单未接入，本页未对该市场发起请求")}
            description={
              <Text type="secondary" style={{ fontSize: 11 }}>
                {`当前选中市场为 ${market}（${marketName(market)}）。按市场开关，本页不会向 /api/v3/plates 发送 market=${market} 的无效请求；切回 A股 SH 即恢复真实板块清单。`}
              </Text>
            }
          />
        ) : plates.error ? (
          <BlockError name="板块清单（/api/v3/plates?market=SH）" error={plates.error} />
        ) : plateList.length === 0 ? (
          <NoSource what="板块清单" why={errText(plates.value, "GET /api/v3/plates?market=SH&plate_class=ALL 未返回板块清单")} />
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
              /api/v3/plates?market=SH&plate_class=ALL · 仅 A 股请求，切换市场时不发该请求
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
              （/api/v3/metrics）· 逐条降级链（主源 / 降级源 / 最近来源）见「接入与授权」页的「数据源与降级链」卡
            </Text>
          </>
        )}
      </ProCard>
      {/* 数据域：把后端外部数据源端点（news/financials/tushare/openbb/spot）全部对到界面 */}
      <DataDomainCard />
    </Space>
  );
}
