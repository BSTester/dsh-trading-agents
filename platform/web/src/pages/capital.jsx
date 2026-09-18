// 资金流向页：个股分时资金流（capital_flow）+ 历史资金流（capital_flow_history）
// + 日内资金分布（capital_distribution）。
// 字段依据（页面每个取值路径均可指到源码行）：
//   三端点都是富途实时直通（platform/server/futu_data.py FutuData.capital_flow /
//   capital_flow_history / capital_distribution）：参数归一（code → market.to_futu_symbol）
//   后经 MCP/OpenAPI 通道取数，value **原样透传、不做形状归一**（futu_data._fetch 只透传
//   data）。页面按富途官方字段名防御式读取：flow_items[]（capital_flow_item_time、in_flow、
//   super_inflow/big_inflow/mid_inflow/sml_inflow）、hist_items[]（capital_flow_item_time、
//   in_flow）、distribution（in_flow 与四档单量）。候选键全部缺失时展示 —；每个区块折叠
//   「原始返回」供核对——通道字段与文档不符时界面不编造数据。
//   A 股分时资金流实测可用（futu_data.py 文件头：A 股实时报价的替代路径之一）；
//   业务错误（如 ret_code=-9 的替代路径提示）由服务端错误信封给出，页面原样展示。
// 占比 = 该档净流入绝对值 / 四档绝对值之和（纯展示换算，非指标计算；总和为 0 或缺失 → —）。
import React from "react";
import { Alert, Card, Collapse, Select, Space, Table, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";
import { num } from "../services/format.jsx";
import { useMarketFilter } from "../services/marketContext.jsx";
import { symbolMarketNote } from "../services/marketView.js";
import { SymbolInput } from "../components/SymbolInput.jsx";
import { MultiLineChart } from "../charts/line.jsx";

const DAYS_OPTIONS = [
  { value: 30, label: "30 天" }, { value: 60, label: "60 天" }, { value: 90, label: "90 天" },
];

// 四档单的展示名与官方候选键（超大/大/中/小）：*_ratio 为上游自带占比键（有则原样展示）。
const SIZE_BUCKETS = [
  { label: "超大单", keys: ["super_inflow", "super_net_inflow"],
    ratioKeys: ["super_inflow_ratio", "super_net_inflow_ratio"] },
  { label: "大单", keys: ["big_inflow", "large_net_inflow"],
    ratioKeys: ["big_inflow_ratio", "large_net_inflow_ratio"] },
  { label: "中单", keys: ["mid_inflow", "medium_net_inflow"],
    ratioKeys: ["mid_inflow_ratio", "medium_net_inflow_ratio"] },
  { label: "小单", keys: ["sml_inflow", "small_net_inflow"],
    ratioKeys: ["sml_inflow_ratio", "small_net_inflow_ratio"] },
];

/** 多候选键防御读取：返回第一个非 null/undefined 的值，全部缺失返回 undefined（不补默认值）。 */
function pick(row, keys) {
  for (const key of keys) {
    if (row && row[key] !== null && row[key] !== undefined) return row[key];
  }
  return undefined;
}

/** 透传 value → 行数组：数组（含裸数组）直接用；对象取「候选键下的数组」或全部数组型成员拼接。 */
function rowsOf(value, keys) {
  if (Array.isArray(value)) return value.filter((row) => row && typeof row === "object");
  const direct = pick(value, keys);
  if (Array.isArray(direct)) return direct.filter((row) => row && typeof row === "object");
  if (value && typeof value === "object") {
    return Object.values(value).flatMap((part) =>
      Array.isArray(part) ? part.filter((row) => row && typeof row === "object") : []);
  }
  return [];
}

/** 净流入值上色：中国习惯红涨绿跌，正值红（antd danger）、负值绿（antd success）。 */
function FlowText({ value }) {
  if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) {
    return <span>—</span>;
  }
  const parsed = Number(value);
  if (parsed > 0) return <Typography.Text type="danger">{num(parsed)}</Typography.Text>;
  if (parsed < 0) return <Typography.Text type="success">{num(parsed)}</Typography.Text>;
  return <span>{num(parsed)}</span>;
}

/** 「原始返回」折叠块：透传数据的核对出口（形状不符时界面不编造，原始 JSON 说话）。 */
function RawCollapse({ value, loading }) {
  if (loading || value === null || value === undefined) return null;
  return (
    <Collapse size="small" items={[{
      key: "raw",
      label: "原始返回（核对用）",
      children: (
        <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12, maxHeight: 320, overflow: "auto" }}>
          {JSON.stringify(value, null, 2)}
        </pre>),
    }]} />);
}

/** 分时资金流：表格 + 四档净流入多线图。 */
function IntradayFlow({ flow }) {
  const items = rowsOf(flow.value, ["flow_items"]);
  if (flow.error) return <Alert type="error" showIcon message={`分时资金流读取失败：${flow.error}`} />;
  return (
    <>
      <Table size="small"
        rowKey={(_row, index) => index}
        dataSource={items}
        pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
        loading={flow.loading}
        locale={{ emptyText: flow.loading ? "分时资金流加载中…" : "暂无分时资金流数据。" }}
        scroll={{ x: "max-content" }}
        columns={[
          { title: "时间", key: "t", render: (_f, row) => pick(row, ["capital_flow_item_time", "update_time", "time"]) ?? "—" },
          { title: "净流入", key: "in_flow", align: "right", render: (_f, row) => <FlowText value={pick(row, ["in_flow", "main_inflow"])} /> },
          ...SIZE_BUCKETS.map((bucket) => ({
            title: bucket.label, key: bucket.label, align: "right",
            render: (_f, row) => <FlowText value={pick(row, bucket.keys)} />,
          })),
        ]} />
      <MultiLineChart
        labels={items.map((row) => String(pick(row, ["capital_flow_item_time", "update_time", "time"]) ?? ""))}
        series={SIZE_BUCKETS.map((bucket) => ({
          label: bucket.label,
          values: items.map((row) => Number(pick(row, bucket.keys))),
        }))} />
      {items.length > 0 && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          折线为四档净流入（单位与上游一致）；缺数值的时点断线不画。
        </Typography.Text>)}
      <RawCollapse value={flow.value} loading={flow.loading} />
    </>);
}

/** 历史资金流：按日表格（上游 hist_items；字段面与上游一致，缺 —）。 */
function FlowHistory({ hist }) {
  const items = rowsOf(hist.value, ["hist_items"]);
  if (hist.error) return <Alert type="error" showIcon message={`历史资金流读取失败：${hist.error}`} />;
  return (
    <>
      <Table size="small"
        rowKey={(_row, index) => index}
        dataSource={items}
        pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
        loading={hist.loading}
        locale={{ emptyText: hist.loading ? "历史资金流加载中…" : "暂无历史资金流数据。" }}
        scroll={{ x: "max-content" }}
        columns={[
          { title: "日期", key: "day", render: (_f, row) => String(pick(row, ["capital_flow_item_time", "day", "date"]) ?? "—").slice(0, 10) },
          { title: "净流入", key: "in_flow", align: "right", render: (_f, row) => <FlowText value={pick(row, ["in_flow"])} /> },
        ]} />
      <RawCollapse value={hist.value} loading={hist.loading} />
    </>);
}

/** 资金分布：四档单量占比（纯展示换算：|档值| / Σ|档值|）+ 整体净流入。 */
function Distribution({ dist }) {
  if (dist.error) return <Alert type="error" showIcon message={`资金分布读取失败：${dist.error}`} />;
  const body = dist.value && typeof dist.value === "object" && !Array.isArray(dist.value)
    ? (dist.value.distribution && typeof dist.value.distribution === "object" ? dist.value.distribution : dist.value)
    : {};
  const buckets = SIZE_BUCKETS.map((bucket) => ({
    ...bucket,
    value: pick(body, bucket.keys),
    ratio: pick(body, bucket.ratioKeys),
  }));
  const totalAbs = buckets.reduce((sum, item) => {
    const value = Number(item.value);
    return sum + (Number.isFinite(value) ? Math.abs(value) : 0);
  }, 0);
  const inFlow = pick(body, ["in_flow", "main_inflow"]);
  return (
    <>
      {/* Statistic 的 value 只收字符串/数字（ReactNode 会变 [object Object]），
          整体净流入需要着色，直接用 FlowText 排版。 */}
      <Space direction="vertical" size={0} style={{ marginBottom: 8 }}>
        <Typography.Text type="secondary" style={{ fontSize: 14 }}>整体净流入</Typography.Text>
        <FlowText value={inFlow} />
      </Space>
      <Table size="small"
        rowKey={(row) => row.label}
        dataSource={buckets}
        pagination={false}
        loading={dist.loading}
        locale={{ emptyText: dist.loading ? "资金分布加载中…" : "暂无资金分布数据。" }}
        columns={[
          { title: "档位", key: "label", render: (_f, row) => row.label },
          { title: "净流入", key: "value", align: "right", render: (_f, row) => <FlowText value={row.value} /> },
          { title: "占比", key: "ratio", width: 280, render: (_f, row) => {
            // 上游给了占比键就用上游的（原样）；没给才做绝对值占比的展示换算
            if (row.ratio !== undefined && Number.isFinite(Number(row.ratio))) {
              const percent = Number(row.ratio) <= 1 ? Number(row.ratio) * 100 : Number(row.ratio);
              return <span>{percent.toFixed(2)}%</span>;
            }
            const share = totalAbs > 0 && row.value !== undefined && Number.isFinite(Number(row.value))
              ? (Math.abs(Number(row.value)) / totalAbs) * 100 : null;
            return share === null ? "—" : `${share.toFixed(2)}%`;
          } },
        ]} />
      {dist.value?.pagination && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          上游分页：{JSON.stringify(dist.value.pagination)}
        </Typography.Text>)}
      <RawCollapse value={dist.value} loading={dist.loading} />
    </>);
}

export default function CapitalPage() {
  const [ticker, setTicker] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [days, setDays] = React.useState(30);
  const { market } = useMarketFilter();
  // 未输入标的时 payload 为 null：hook 跳过请求（服务端对空 code 直接参数报错）。
  const flow = useEndpoint("capital_flow", query ? { code: query } : null, [query]);
  const hist = useEndpoint("capital_flow_history", query ? { code: query, days } : null, [query, days]);
  const dist = useEndpoint("capital_distribution", query ? { code: query } : null, [query]);
  // 市场维度：三个端点的返回是富途原文透传，行里**没有**标的/市场字段（实测只有
  // capital_flow_item_time/in_flow/四档单量），市场只存在于用户输入的 code 上——
  // 一次只查一个标的，不存在跨市场混排。故不做过滤，只在不一致时提示（与行情页同口径）。
  const marketNote = symbolMarketNote(query, market);
  return (
    <Card title="资金流向" extra={(
      <Space>
        <SymbolInput placeholder="标的代码，如 SH.600519 / HK.00700" style={{ width: 240 }} value={ticker}
          onChange={setTicker}
          onPressEnter={(text) => setQuery(text.trim().toUpperCase())} />
        <Select options={DAYS_OPTIONS} value={days} onChange={setDays} style={{ width: 96 }}
          aria-label="历史资金流回看天数" />
      </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {!query && (
          <Typography.Text type="secondary">
            输入标的代码后查询；三个端点都是富途实时直通（TTL 0 不缓存）。
          </Typography.Text>)}
        {query && marketNote && <Alert type="info" showIcon message={marketNote} />}
        {query && (
          <Card type="inner" title="分时资金流（大 / 中 / 小 / 超大单）">
            <IntradayFlow flow={flow} />
          </Card>)}
        {query && (
          <Card type="inner" title="历史资金流" extra={(
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              回看 {days} 天
            </Typography.Text>)}>
            <FlowHistory hist={hist} />
          </Card>)}
        {query && (
          <Card type="inner" title="资金分布（超大 / 大 / 中 / 小单占比）">
            <Distribution dist={dist} />
          </Card>)}
      </Space>
    </Card>);
}
