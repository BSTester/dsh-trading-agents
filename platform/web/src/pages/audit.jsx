// 审计页：计划→订单→成交三级链路、信号→下单→成交时间线、数据源授权状态、对账差异。
// 字段依据（页面每个取值路径均可指到源码行）：
//   三级链路 → reconcile 端点的 chain（platform/server/app.py:199-212 →
//     plugins/core/python/trading_core/snapshots.py:86-90 reconcile_snapshot）：
//      chain:[{plan_id, mode, status, created_at,
//              orders:[{client_order_id, symbol, side, qty, price, status,
//                       broker_order_id, risk_verdict, fills:[{price, qty, traded_at}]}]}]
//     订单与成交来自 store.get_orders_by_plan（store.py:344-346）、
//     store.fills_by_order（store.py:367-370）与 snapshots._orders_of(with_fills=True)
//     （snapshots.py:23-34）；计划行来自 store.list_plans（store.py:315-322 升序），
//     端点经 snapshots.py:_plans_newest_first 翻转为最新在前（chain[0]=最新，与 plan 端点同口径）。
//     ——计划号/订单号/成交这一层结构只在 chain 上，审计时间线（audit 端点）里没有。
//   时间线 → audit 端点（app.py:186-198）→ platform/server/audit_chain.py:170-350
//     build_audit_chain：{entries, stats}。
//     entry：{id, kind(signal|order|order-modify|order-cancel|order-facts|order-other|
//       order-error|fill), at, atMs, ticker, detail, source, source_label,
//       origin?(信号起点), signal_id/signal_at/linked/lag_hours?(订单与成交),
//       reason?(成交台账), order_id?/action?(券商观察行)}。
//     stats：{signals, orders, order_kinds, fills, linked, unlinked, link_rule}
//       （audit_chain.py:339-350）；kind 取值见 audit_chain.py:57 与 :263-268。
//   数据源 → sources 端点（app.py:178-185 → plugins/workbench/python/sources.py:6 输出
//     {checked_at, sources:[{key, label, status, detail, fix}], summary:{ok,warn,fail}}）；
//     状态中文映射对齐 platform/server/labels.py:50 SOURCE_STATUS（ok/warn/fail）。
//   对账差异 → reconcile.diffs（snapshots.py:74-75 读 kv "reconcile:latest"）与
//     reconcile.diffs_at；差异行由 trading_core/reconcile.py:5-25 compare 产出：
//     {symbol, local, broker, kind:"missing_side"} / {symbol, local, broker, qty_diff,
//      kind:"qty"} / {symbol, local_value, broker_value, kind:"value"}。
// 「不猜」：缺字段一律 —；枚举只映射源码里出现的取值，未知值原样展示。
import React from "react";
import { Card, Space, Statistic, Table, Tag, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";
import { num, stampOf } from "../services/format.jsx";
import { useMarketFilter } from "../services/marketContext.jsx";
import {
  marketLabelOf, planMarketDisplay, symbolMarketDisplay, viewBySymbol, viewPlans,
} from "../services/marketView.js";
import { isAllMarkets } from "../services/marketFilter.js";

const SIDE = { BUY: "买入", SELL: "卖出" };

/** ISO 时间 → 展示（分钟精度），与执行页 timeOf 同一口径；缺失显示「时间未知」。 */
function timeOf(iso) {
  return iso ? String(iso).replace("T", " ").slice(0, 16) : "时间未知";
}

// 链路行类型 → 中文（audit_chain.py:57 ACTION_KIND 与 :263-268 kind 赋值）
const KIND_LABEL = { signal: "信号", order: "下单", "order-modify": "改单",
                     "order-cancel": "撤单", "order-facts": "订单查询",
                     "order-other": "订单工具", "order-error": "下单失败", fill: "成交" };

const KIND_COLOR = { signal: "blue", order: "green", "order-modify": "green",
                     "order-cancel": "red", "order-facts": "gold", "order-other": "green",
                     "order-error": "red", fill: "cyan" };

const PLAN_STATUS = { draft: "草稿", frozen: "已冻结", approved: "已批准", executing: "执行中" };

const ORDER_STATUS = { draft: "草稿", frozen: "已冻结", submitting: "提交中", submitted: "已提交",
                       partial: "部分成交", filled: "已成交", cancelled: "已撤销",
                       rejected: "已拒绝", unknown: "未知" };

// labels.py:50 SOURCE_STATUS
const SOURCE_STATUS = { ok: "正常", warn: "待配置", fail: "异常", empty: "无数据" };

const SOURCE_STATUS_COLOR = { ok: "green", warn: "gold", fail: "red", empty: "default" };

// reconcile.py:12/17/23 的 kind 取值
const DIFF_KIND = { missing_side: "单边缺失", qty: "数量不一致", value: "市值不一致" };

// audit_chain.py 只给成交台账行写了 source（:244，无 source_label），故补一份中文兜底；
// signal/order 行自带 source_label，优先用服务端文案；未知取值原样展示（「不猜」）。
const SOURCE_LABEL = { "local-ledger": "本地台账", "broker-observed": "券商观察",
                       quant_signal: "量化信号" };

const DIFF_KIND_COLOR = { missing_side: "red", qty: "gold", value: "blue" };

const FILL_COLUMNS = [
  { title: "成交价", key: "price", align: "right", render: (_field, row) => num(row.price) },
  { title: "数量", key: "qty", align: "right", render: (_field, row) => num(row.qty, 0) },
  { title: "成交时间", key: "traded_at", render: (_field, row) => row.traded_at ?? "—" },
];

const CHAIN_ORDER_COLUMNS = [
  { title: "订单号", key: "client_order_id", render: (_field, row) => row.client_order_id ?? "—" },
  { title: "标的", key: "symbol", render: (_field, row) => row.symbol ?? "—" },
  { title: "方向", key: "side", render: (_field, row) => SIDE[row.side] ?? row.side ?? "—" },
  { title: "数量", key: "qty", align: "right", render: (_field, row) => num(row.qty, 0) },
  { title: "限价", key: "price", align: "right", render: (_field, row) => num(row.price) },
  { title: "状态", key: "status",
    render: (_field, row) => <Tag>{ORDER_STATUS[row.status] ?? row.status ?? "—"}</Tag> },
  { title: "券商单号", key: "broker_order_id", render: (_field, row) => row.broker_order_id ?? "—" },
  { title: "成交笔数", key: "fills", align: "right",
    render: (_field, row) => (row.fills ?? []).length },
];

/** 链路展开行里的成交明细：无成交时给出事实说明，不留空白。 */
function fillsOf(order) {
  const fills = order?.fills ?? [];
  if (fills.length === 0) return <Typography.Text type="secondary">无成交</Typography.Text>;
  // 成交行没有服务端 id：键在渲染前一次算好（antd 的 rowKey 不再传下标）
  const rows = fills.map((fill, index) => ({ ...fill, _key: `${order.client_order_id}-${index}` }));
  return (
    <Table size="small"
      rowKey={(row) => row._key}
      dataSource={rows}
      pagination={false}
      columns={FILL_COLUMNS} />);
}

const CHAIN_COLUMNS = [
  { title: "计划", key: "plan_id", render: (_field, row) => (
    <Typography.Text code>{row.plan_id ?? "—"}</Typography.Text>) },
  { title: "模式", key: "mode", render: (_field, row) => row.mode ?? "—" },
  { title: "状态", key: "status",
    render: (_field, row) => <Tag>{PLAN_STATUS[row.status] ?? row.status ?? "—"}</Tag> },
  { title: "创建时间", key: "created_at", render: (_field, row) => row.created_at ?? "—" },
  { title: "订单数", key: "orders", align: "right",
    render: (_field, row) => (row.orders ?? []).length },
];

const ENTRY_COLUMNS = [
  { title: "时间", key: "at", render: (_field, row) => timeOf(row.at) },
  { title: "类型", key: "kind",
    render: (_field, row) => (
      <Tag color={KIND_COLOR[row.kind] ?? "default"}>{KIND_LABEL[row.kind] ?? row.kind ?? "—"}</Tag>) },
  { title: "标的", key: "ticker", render: (_field, row) => row.ticker ?? "—" },
  { title: "内容", key: "detail", render: (_field, row) => row.detail ?? "—" },
  { title: "关联", key: "linked", render: (_field, row) => {
    if (row.origin) return <Typography.Text type="secondary">链路起点</Typography.Text>;
    if (row.linked) {
      return <Typography.Text type="secondary">
        {`依据信号 ${row.signal_id ?? "—"}（滞后 ${row.lag_hours ?? "—"} 小时）`}
      </Typography.Text>;
    }
    return <Typography.Text type="warning">未找到对应信号</Typography.Text>;
  } },
  { title: "来源", key: "source",
    render: (_field, row) => row.source_label ?? SOURCE_LABEL[row.source] ?? row.source ?? "—" },
];

const SOURCE_COLUMNS = [
  { title: "来源", key: "label", render: (_field, row) => row.label ?? row.key ?? "—" },
  { title: "状态", key: "status",
    render: (_field, row) => (
      <Tag color={SOURCE_STATUS_COLOR[row.status] ?? "default"}>
        {SOURCE_STATUS[row.status] ?? row.status ?? "—"}
      </Tag>) },
  { title: "说明", key: "detail", render: (_field, row) => row.detail ?? "—" },
  { title: "修复", key: "fix",
    render: (_field, row) => (row.status !== "ok" && row.fix ? row.fix : "—") },
];

/**
 * 对账差异的「本地/券商」单元格（数量列）。
 *
 * `reconcile.compare` 的两类差异**值形状不同**（reconcile.py:104-124）：
 *   * `kind="qty"` → `local`/`broker` 是**整数数量**；
 *   * `kind="missing_side"` → `local`/`broker` 是**整份持仓字典**（`{qty: N}`）或 `null`。
 * 早先直接 `num(row.local ?? row.local_value)`：`num()` 对非有限值回落 `String(value)`，
 * 把持仓字典渲染成 **`[object Object]`**（2026-09-19 真机取证：`broker={"qty":5200}` → 页面上
 * 该格就是 `[object Object]`）。这里取字典里的 `qty`（唯一的数量事实）；`null`（该侧确实
 * 没有持仓）与取不到 `qty` 的字典都显示 `—`——**绝不把未知渲染成一个看起来像数字的东西**。
 */
function diffQty(value, digits = 0) {
  if (value && typeof value === "object") return num(value.qty, digits);
  return num(value, digits);
}

const DIFF_COLUMNS = [
  { title: "标的", key: "symbol", render: (_field, row) => row.symbol ?? "—" },
  { title: "差异类型", key: "kind",
    render: (_field, row) => (
      <Tag color={DIFF_KIND_COLOR[row.kind] ?? "default"}>
        {DIFF_KIND[row.kind] ?? row.kind ?? "—"}
      </Tag>) },
  // 数量类差异取 local/broker，市值类差异取 local_value/broker_value（reconcile.py:12-24）
  { title: "本地", key: "local", align: "right",
    render: (_field, row) => diffQty(row.local ?? row.local_value) },
  { title: "券商", key: "broker", align: "right",
    render: (_field, row) => diffQty(row.broker ?? row.broker_value) },
  { title: "数量差（本地−券商）", key: "qty_diff", align: "right",
    render: (_field, row) => num(row.qty_diff, 0) },
];

export default function AuditPage() {
  const { market } = useMarketFilter();
  const audit = useEndpoint("audit", {}, []);
  const sources = useEndpoint("sources", {}, []);
  const reconcile = useEndpoint("reconcile", {}, []);

  const chain = reconcile.value?.chain ?? [];
  const entries = audit.value?.entries ?? [];
  const stats = audit.value?.stats;
  const sourceRows = sources.value?.sources ?? [];
  const sourceSummary = sources.value?.summary;
  const diffs = reconcile.value?.diffs ?? [];
  // 全局市场筛选（客户端展示层，**不改请求参数**）。三块数据的市场来源各不相同：
  //   链路 → 计划的 target 键与订单标的前缀（plan 端点/chain 都**没有** market 字段，实测）；
  //   时间线 → entries[].ticker（**实测可能是裸代码 '00981'**，无交易所前缀 → 无法判定）；
  //   对账差异 → diffs[].symbol；
  // 数据源授权/链路统计没有市场维度，恒为全局（不按市场藏掉）。
  const filtered = !isAllMarkets(market);
  const chainView = viewPlans(chain, market, { scope: "链路" });
  const entryView = viewBySymbol(entries, market, "ticker", { scope: "时间线" });
  const diffView = viewBySymbol(diffs, market, "symbol", { scope: "对账差异" });
  // 审计行/差异行没有服务端 id：键在渲染前一次算好（antd 的 rowKey 不再传下标）
  const entryRows = entryView.rows.map((row, index) => ({
    ...row, _key: `${row.kind ?? ""}-${row.id ?? index}` }));
  const diffRows = diffView.rows.map((row, index) => ({
    ...row, _key: `${row.symbol ?? ""}-${row.kind ?? ""}-${index}` }));
  // 无法判定市场的行（裸代码）被排除时要如实报数，不能让它们静默消失
  const blindEntries = filtered && entryView.unclassified > 0
    ? `另有 ${entryView.unclassified} 条时间线记录的标的没有交易所前缀（如 '00981'），`
      + "无法判定市场，未计入上表——请切到「全部市场」核对。" : null;
  const blindDiffs = filtered && diffView.unclassified > 0
    ? `另有 ${diffView.unclassified} 条差异行的标的市场无法判定，未计入上表。` : null;

  return (
    <Card title="审计">
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {reconcile.error && (
          <Typography.Text type="danger">对账读取失败：{reconcile.error}</Typography.Text>)}
        {audit.error && (
          <Typography.Text type="danger">审计读取失败：{audit.error}</Typography.Text>)}
        {filtered && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            市场筛选：{marketLabelOf(market)} —— 链路/时间线/对账差异按该市场过滤；
            数据源与授权状态、链路统计没有市场维度，仍为全局口径。
          </Typography.Text>)}

        <Card type="inner" title="计划 → 订单 → 成交" extra={(
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            最近 {chainView.rows.length} 个计划{filtered ? `（${marketLabelOf(market)}）` : ""}，逐级展开
          </Typography.Text>)}>
          <Table size="small"
            rowKey={(row) => row.plan_id}
            dataSource={chainView.rows}
            pagination={false}
            scroll={{ x: "max-content" }}
            locale={{ emptyText: reconcile.loading
              ? "链路加载中…"
              : (chainView.emptyReason ?? "暂无链路数据（冻结计划并产生订单后显示）。") }}
            columns={[
              ...CHAIN_COLUMNS,
              // 「全部市场」下跨市场混排：补市场列（由计划自带的 target/订单标的派生）
              ...(filtered ? [] : [{ title: "市场", key: "market",
                render: (_field, row) => planMarketDisplay(row) }]),
            ]}
            expandable={{
              expandedRowRender: (plan) => (
                <Table size="small"
                  rowKey={(row) => row.client_order_id}
                  dataSource={viewBySymbol(plan.orders ?? [], market, "symbol").rows}
                  pagination={false}
                  scroll={{ x: "max-content" }}
                  locale={{ emptyText: filtered
                    ? `该计划没有「${marketLabelOf(market)}」的订单。`
                    : "该计划没有订单。" }}
                  columns={CHAIN_ORDER_COLUMNS}
                  expandable={{
                    // 无成交的订单也允许展开：展开区给出「无成交」事实，不留空白
                    expandedRowRender: (order) => fillsOf(order),
                  }} />),
            }} />
        </Card>

        <Card type="inner" title="链路统计"
          extra={(
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              关联规则：{stats?.link_rule ?? "—"}
            </Typography.Text>)}>
          {stats ? (
            <Space size="large" wrap>
              <Statistic title="信号" value={stats.signals ?? "—"} />
              <Statistic title="订单相关响应" value={stats.orders ?? "—"} />
              <Statistic title="成交（本地台账）" value={stats.fills ?? "—"} />
              <Statistic title="已关联信号" value={stats.linked ?? "—"} />
              <Statistic title="未关联" value={stats.unlinked ?? "—"} />
            </Space>) : (
            <Typography.Text type="secondary">
              {audit.loading ? "链路统计加载中…" : "暂无链路统计。"}
            </Typography.Text>)}
          {stats?.order_kinds && Object.keys(stats.order_kinds).length > 0 && (
            <Typography.Text type="secondary">
              构成：{Object.entries(stats.order_kinds).map(([label, count]) =>
                `${label} ${count} 次`).join(" · ")}
            </Typography.Text>)}
        </Card>

        <Card type="inner" title="时间线（信号 → 下单 → 成交）">
          <Table size="small"
            rowKey={(row) => row._key}
            dataSource={entryRows}
            pagination={{ pageSize: 12, hideOnSinglePage: true, showSizeChanger: false }}
            scroll={{ x: "max-content" }}
            locale={{ emptyText: audit.loading
              ? "时间线加载中…"
              : (entryView.emptyReason ?? "暂无记录（产生信号并下单/成交后显示）。") }}
            columns={[
              ...ENTRY_COLUMNS,
              // 「全部市场」下补市场列；「—」= 标的无交易所前缀，市场无法判定（不猜）
              ...(filtered ? [] : [{ title: "市场", key: "market",
                render: (_field, row) => symbolMarketDisplay(row.ticker) }]),
            ]} />
          {blindEntries && (
            <Typography.Text type="warning" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
              {blindEntries}
            </Typography.Text>)}
        </Card>

        <Card type="inner" title="数据源与授权状态" extra={(
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            自检时间 {stampOf(sources.value?.checked_at)} · 正常 {sourceSummary?.ok ?? "—"} /
            待配置 {sourceSummary?.warn ?? "—"} / 异常 {sourceSummary?.fail ?? "—"}
          </Typography.Text>)}>
          {sources.error && (
            <Typography.Text type="danger">数据源读取失败：{sources.error}</Typography.Text>)}
          <Table size="small"
            rowKey={(row) => row.key}
            dataSource={sourceRows}
            pagination={false}
            scroll={{ x: "max-content" }}
            locale={{ emptyText: sources.loading ? "数据源自检中…" : "未返回渠道状态。" }}
            columns={SOURCE_COLUMNS} />
        </Card>

        <Card type="inner" title="对账差异" extra={(
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            差异时间 {stampOf(reconcile.value?.diffs_at)}{filtered ? ` · ${marketLabelOf(market)}` : ""}
          </Typography.Text>)}>
          <Table size="small"
            rowKey={(row) => row._key}
            dataSource={diffRows}
            pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
            scroll={{ x: "max-content" }}
            locale={{ emptyText: reconcile.loading
              ? "差异加载中…"
              : (diffView.emptyReason ?? "暂无对账差异。") }}
            columns={[
              ...DIFF_COLUMNS,
              ...(filtered ? [] : [{ title: "市场", key: "market",
                render: (_field, row) => symbolMarketDisplay(row.symbol) }]),
            ]} />
          {blindDiffs && (
            <Typography.Text type="warning" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
              {blindDiffs}
            </Typography.Text>)}
        </Card>

        <Typography.Text type="secondary">
          审计只记录 Harness 观察到的响应与本地台账；实盘成交以券商成交查询为准。
        </Typography.Text>
      </Space>
    </Card>);
}
