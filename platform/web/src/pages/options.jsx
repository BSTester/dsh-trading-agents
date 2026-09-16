// 期权分析页：到期日列表（option_expiration）→ 期权链（option_chain）→ 期权筛选
// （option_screen）。数据源全部是富途实时直通端点（platform/server/futu_data.py，
// TTL 0），value 原样透传不做形状归一；页面按官方字段名防御式读取，缺字段一律 —，
// 每个区块折叠「原始返回」供核对——通道字段与文档不符时界面不编造数据。
// 到期日选择是**本地行过滤**：服务端 option_chain 的载荷白名单只有 code/field_filter
// （app.py FUTU_FIELDS / futu_data.option_chain），不接受到期日参数；链行自带的到期日
// 字段（expiration_date 等候选键）用于筛选展示。期权行是否有 Greeks 取决于上游字段面
// （REST 更全），行里出现哪些希腊字母键就展示哪些，不凭空造列。
// option_screen 必填陷阱（futu_data.option_screen 与 docs/TOOL-LIMITS 的实测事实，页面
// 指引原文照抄）：filter 必须是非空对象，且必须带**非空 field_filter**（省略时上游只
// 返回 4 个默认字段、其余全 null），strategy 也是上游必填（服务端提示示例
// {"market_category_list": [1]}）——缺了直接被拒，不浪费注定失败的往返。
import React from "react";
import { Alert, Button, Card, Input, Select, Space, Table, Typography } from "antd";
import { callApi } from "../services/api.js";
import { useEndpoint } from "../services/hooks.js";
import { num } from "../services/format.jsx";
import { RawCollapse } from "../lib/raw-collapse.jsx";
import {
  dataplaneHint, exerciseProbabilitySummary, optionVolatilitySummary, strikeRows,
} from "../services/f10.js";

const { TextArea } = Input;

const STRATEGY_EXAMPLE = '{"market_category_list": [1]}';

/** 多候选键防御读取：返回第一个非 null/undefined 的值，全部缺失返回 undefined（不补默认值）。 */
function pick(row, keys) {
  for (const key of keys) {
    if (row && row[key] !== null && row[key] !== undefined) return row[key];
  }
  return undefined;
}

/** 透传 value → 行对象数组：数组直接用；对象取全部数组型成员拼接（兼容裸数组与分组对象）。 */
function rowsOf(value) {
  if (Array.isArray(value)) return value.filter((row) => row && typeof row === "object");
  if (value && typeof value === "object") {
    return Object.values(value).flatMap((part) =>
      Array.isArray(part) ? part.filter((row) => row && typeof row === "object") : []);
  }
  return [];
}

/** 到期日候选：行内字符串、或行对象的 expiration_date/date 候选键，原样去重排序。 */
function expirationCandidates(value) {
  const rows = Array.isArray(value) ? value : rowsOf(value);
  const dates = rows.map((row) => (typeof row === "string"
    ? row
    : pick(row, ["expiration_date", "expire_date", "date", "strike_time"])))
    .filter((item) => typeof item === "string" && item);
  return [...new Set(dates)].sort();
}

const CHAIN_BASE_COLUMNS = [
  { title: "代码", key: "code", render: (_f, row) => pick(row, ["code", "option_code", "ticker"]) ?? "—" },
  { title: "名称", key: "name", render: (_f, row) => pick(row, ["name", "option_name"]) ?? "—" },
  { title: "到期日", key: "expiration", render: (_f, row) => pick(row, ["expiration_date", "expire_date", "date"]) ?? "—" },
  { title: "行权价", key: "strike", align: "right", render: (_f, row) => num(pick(row, ["strike_price", "strike"])) },
  { title: "买价", key: "bid", align: "right", render: (_f, row) => num(pick(row, ["bid_price", "bid"])) },
  { title: "卖价", key: "ask", align: "right", render: (_f, row) => num(pick(row, ["ask_price", "ask"])) },
  { title: "成交量", key: "volume", align: "right", render: (_f, row) => num(pick(row, ["volume", "turnover_vol"]), 0) },
  { title: "持仓量", key: "open_interest", align: "right", render: (_f, row) => num(pick(row, ["open_interest", "position"]), 0) },
];

// Greeks 列（上游行里出现哪个键才追加哪列；rest 更全，MCP 通道可能全缺 → 无列不造列）。
const GREEK_KEYS = [
  { key: "delta", title: "Delta", keys: ["delta"] },
  { key: "gamma", title: "Gamma", keys: ["gamma"] },
  { key: "theta", title: "Theta", keys: ["theta"] },
  { key: "vega", title: "Vega", keys: ["vega"] },
  { key: "rho", title: "Rho", keys: ["rho"] },
  { key: "iv", title: "隐含波动率", keys: ["implied_volatility", "iv"] },
];

function chainColumns(rows) {
  const present = GREEK_KEYS.filter((greek) => rows.some((row) => pick(row, greek.keys) !== undefined));
  return [...CHAIN_BASE_COLUMNS,
    ...present.map((greek) => ({
      title: greek.title, key: greek.key, align: "right",
      render: (_f, row) => num(pick(row, greek.keys)),
    })),
    { title: "更新时间", key: "update_time", render: (_f, row) => pick(row, ["update_time", "update_date_time", "data_date"]) ?? "—" },
  ];
}

/** 原始返回折叠块见 src/lib/raw-collapse.jsx（WP12 任务 6 起与研究页共用）。 */

/** 期权链表：本地过滤到期日（服务端 option_chain 不收到期日参数，见文件头）。 */
function ChainTable({ chain }) {
  const allRows = rowsOf(chain.value);
  const [expiration, setExpiration] = React.useState("");
  const dates = expirationCandidates(chain.value);
  const rows = expiration ? allRows.filter((row) => {
    const rowDate = pick(row, ["expiration_date", "expire_date", "date"]);
    return rowDate === expiration || String(rowDate ?? "").startsWith(expiration);
  }) : allRows;
  if (chain.error) {
    return <Alert type="error" showIcon message={`期权链读取失败：${chain.error}`} />;
  }
  return (
    <>
      {dates.length > 0 && (
        <Space style={{ marginBottom: 8 }}>
          <Typography.Text type="secondary">到期日（本地过滤）：</Typography.Text>
          <Select style={{ width: 160 }}
            allowClear
            placeholder="全部到期日"
            value={expiration || undefined}
            onChange={(value) => setExpiration(value ?? "")}
            options={dates.map((date) => ({ value: date, label: date }))} />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            行 {expiration ? rows.length : allRows.length} / {allRows.length}
          </Typography.Text>
        </Space>)}
      <Table size="small"
        rowKey={(_row, index) => index}
        dataSource={rows}
        pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
        loading={chain.loading}
        scroll={{ x: "max-content" }}
        locale={{ emptyText: chain.loading ? "期权链加载中…" : "暂无期权链数据。" }}
        columns={chainColumns(rows)} />
      <RawCollapse value={chain.value} loading={chain.loading} />
    </>);
}

/** 期权筛选：filter JSON 手输（strategy 预填服务端提示示例；field_filter 必填陷阱在指引）。 */
function OptionScreen() {
  const [strategyText, setStrategyText] = React.useState(STRATEGY_EXAMPLE);
  const [filterText, setFilterText] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [result, setResult] = React.useState(null);   // { value } | { error }
  const run = async () => {
    setResult(null);
    let strategy;
    let fieldFilter;
    try {
      strategy = JSON.parse(strategyText);
      if (!strategy || typeof strategy !== "object" || Array.isArray(strategy)
        || Object.keys(strategy).length === 0) {
        throw new Error("strategy 必须是非空对象");
      }
    } catch (error) {
      setResult({ error: `strategy JSON 无效：${error.message}` });
      return;
    }
    try {
      fieldFilter = JSON.parse(filterText);
      if (!fieldFilter || typeof fieldFilter !== "object" || Array.isArray(fieldFilter)
        || Object.keys(fieldFilter).length === 0) {
        throw new Error("field_filter 必须是非空对象");
      }
    } catch (error) {
      setResult({ error: `field_filter JSON 无效：${error.message}（option_screen 必须带非空 field_filter：` +
        "省略时上游只返回 4 个默认字段、其余全 null，见 docs/TOOL-LIMITS.md）" });
      return;
    }
    setBusy(true);
    try {
      // 载荷形状（futu_data.option_screen 的校验面）：filter = {strategy, field_filter}
      // —— strategy 是 filter 内的非空对象键，不是 filter 的顶层展开。
      const value = await callApi("option_screen", { filter: { strategy, field_filter: fieldFilter } });
      setResult({ value });
    } catch (error) {
      setResult({ error: String(error.message || error) });
    } finally {
      setBusy(false);
    }
  };
  const rows = result?.value ? rowsOf(result.value) : [];
  return (
    <>
      <Alert type="info" showIcon style={{ marginBottom: 8 }} message={(
        <Typography.Text style={{ fontSize: 12 }}>
          filter 必须是非空对象，且必须带非空 field_filter（省略时上游只返回 4 个默认字段、
          其余全 null）；strategy 为上游必填，服务端提示示例：{STRATEGY_EXAMPLE}。
          参数校验失败时服务端会直接拒绝并给出原因，页面原样展示。
        </Typography.Text>)} />
      <Space direction="vertical" size="small" style={{ width: "100%", marginBottom: 8 }}>
        <TextArea rows={2} value={strategyText} onChange={(event) => setStrategyText(event.target.value)}
          aria-label="strategy JSON" placeholder='strategy（上游必填），如 {"market_category_list": [1]}' />
        <TextArea rows={2} value={filterText} onChange={(event) => setFilterText(event.target.value)}
          aria-label="field_filter JSON"
          placeholder='field_filter（必填，非空对象）：决定返回哪些字段；留空或 {} 会被拒绝' />
        <Button type="primary" loading={busy} onClick={run}>筛选</Button>
      </Space>
      {result?.error && <Alert type="error" showIcon style={{ marginBottom: 8 }} message={result.error} />}
      {result?.value && (
        <>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            返回行 {rows.length}
            {result.value.has_more === true ? " · 上游还有更多（has_more=true）" : ""}
            {result.value.total !== undefined && result.value.total !== null ? ` · total=${result.value.total}` : ""}
            {result.value.next_key ? ` · next_key=${result.value.next_key}` : ""}
          </Typography.Text>
          <Table size="small"
            rowKey={(_row, index) => index}
            dataSource={rows}
            pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
            scroll={{ x: "max-content" }}
            locale={{ emptyText: "上游返回了空结果。" }}
            columns={chainColumns(rows)} />
          <RawCollapse value={result.value} loading={false} />
        </>)}
    </>);
}

// ---------------------------------------------------------------------------
// 期权波动率与行权概率（derivative_detail，WP12 任务 6）
// ---------------------------------------------------------------------------
// 载荷契约（app.py derivative_detail 白名单 + futu_data.FutuData.derivative_detail）：
//   {code, section, params?}——字段名是 code；section 白名单 4 项，本区用其中两项。
// **合约要求**（锁定表 §C.7 官方 -3）：这两个 section 的 symbol 必须是**期权合约**，
// 传正股会被上游直接拒绝——所以本区用独立的「合约代码」输入，不与正股输入共用。
// 权限语义：行权概率官方 -9 = 用户无期权数据查询权限（与标的无关），由 dataplaneHint 说明。

/** 键值行（缺值由纯函数给 —）。 */
function DerivativeRows({ rows }) {
  return (
    <Space direction="vertical" size={2} style={{ width: "100%" }}>
      {rows.map((row) => (
        <Space key={row.label} size={8} align="start">
          <Typography.Text type="secondary" style={{ minWidth: 180, display: "inline-block" }}>
            {row.label}
          </Typography.Text>
          <Typography.Text>{row.value}</Typography.Text>
        </Space>))}
    </Space>);
}

/** 单 section 查询卡：错误给服务端原因 + 下一步；空对象给如实说明。 */
function DerivativeCard({ title, section, code, summarize, withStrikes = false }) {
  const query = useEndpoint("derivative_detail", code ? { code, section } : null, [code, section]);
  const summary = query.value ? summarize(query.value) : null;
  const strikes = withStrikes ? strikeRows(summary?.strikes) : [];
  const hint = query.error ? dataplaneHint(query.error) : "";
  return (
    <Card type="inner" title={title}>
      {query.error && (
        <Alert type="error" showIcon message={`读取失败：${query.error}`}
          description={hint || undefined} />)}
      {summary?.note && <Alert type="info" showIcon message={summary.note} />}
      {summary && !summary.note && (
        <>
          <DerivativeRows rows={summary.rows} />
          {strikes.length > 0 && (
            <>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                行权概率 {summary.strikes.length} 条（键名按上游原样展示，完整内容见下方原始返回）：
              </Typography.Text>
              {strikes.map((row) => (
                <Space key={row.index} wrap size={12} style={{ marginTop: 4 }}>
                  <Typography.Text type="secondary">#{row.index}</Typography.Text>
                  {row.cells.map((cell) => (
                    <Typography.Text key={cell.label}>
                      <Typography.Text type="secondary">{cell.label}=</Typography.Text>{cell.value}
                    </Typography.Text>))}
                </Space>))}
            </>)}
        </>)}
      <RawCollapse value={query.value} loading={query.loading} />
    </Card>);
}

/** 衍生品区：独立合约输入 + 两张卡（各自独立失败）。 */
function DerivativeDetail() {
  const [contract, setContract] = React.useState("");
  const [code, setCode] = React.useState("");
  return (
    <Card type="inner" title="期权波动率与行权概率（derivative_detail）"
      extra={(
        <Space>
          <Input placeholder="期权合约代码，如 US.AAPL260116C00200000" style={{ width: 300 }}
            value={contract}
            onChange={(event) => setContract(event.target.value)}
            onPressEnter={() => setCode(contract.trim().toUpperCase())} />
          <Button onClick={() => setCode(contract.trim().toUpperCase())}>查询</Button>
        </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          两个接口要求传期权合约代码（传正股会被上游拒绝）；数据经富途 OpenAPI 数据面。
        </Typography.Text>
        {!code && <Typography.Text type="secondary">输入期权合约代码后查询。</Typography.Text>}
        {code && (
          <DerivativeCard title="期权波动率" section="option_volatility" code={code}
            summarize={optionVolatilitySummary} />)}
        {code && (
          <DerivativeCard title="行权概率" section="option_exercise_probability" code={code}
            summarize={exerciseProbabilitySummary} withStrikes />)}
      </Space>
    </Card>);
}

export default function OptionsPage() {
  const [ticker, setTicker] = React.useState("");
  const [query, setQuery] = React.useState("");
  const expirations = useEndpoint("option_expiration", query ? { code: query } : null, [query]);
  const chain = useEndpoint("option_chain", query ? { code: query } : null, [query]);
  return (
    <Card title="期权分析" extra={(
      <Space>
        <Input placeholder="标的代码，如 HK.00700 / US.AAPL" style={{ width: 240 }} value={ticker}
          onChange={(event) => setTicker(event.target.value)}
          onPressEnter={() => setQuery(ticker.trim().toUpperCase())} />
      </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {!query && (
          <Typography.Text type="secondary">
            输入标的代码后查询；富途实时直通（TTL 0），期权限级决定数据面。
          </Typography.Text>)}
        {query && (
          <Card type="inner" title="到期日列表（option_expiration）">
            {expirations.error && (
              <Alert type="error" showIcon message={`到期日读取失败：${expirations.error}`} />)}
            {!expirations.error && (
              <Space size={4} wrap>
                {expirationCandidates(expirations.value).map((date) => (
                  <Typography.Text key={date} code>{date}</Typography.Text>))}
                {expirations.loading && <Typography.Text type="secondary">到期日加载中…</Typography.Text>}
                {!expirations.loading && expirationCandidates(expirations.value).length === 0 && (
                  <Typography.Text type="secondary">上游未返回到期日（见原始返回）。</Typography.Text>)}
              </Space>)}
            <RawCollapse value={expirations.value} loading={expirations.loading} />
          </Card>)}
        {query && (
          <Card type="inner" title="期权链（option_chain）">
            {/* key=query：换标的时重建组件，到期日本地筛选不残留上一个标的的选择 */}
            <ChainTable key={query} chain={chain} />
          </Card>)}
        <Card type="inner" title="期权筛选（option_screen）">
          <OptionScreen />
        </Card>
        <DerivativeDetail />
      </Space>
    </Card>);
}
