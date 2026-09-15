// 执行页：券商交易事实（snapshot.trade_summary，读时归纳不写盘）+ 本地台账成交（trades）
// + 待确认实盘操作卡片（WP7 任务 3：服务进程内业务确认的 Web 作答入口）。
// 字段依据（页面每个取值路径均可指到源码行）：
//   confirmation → server/store_access.py confirmation_view：{id, at, expires_at, mode,
//     tool, operation(下单/改单/撤单), session_id, status, summary{fields,raw}}；
//     TTL 120 秒（CONFIRM_TTL_MS），超时服务端按拒绝收尾（fail-closed）。
//   批准/拒绝 → confirm-decide（唯一能批准实盘操作的通道；载荷只有 id 与 decision）。
//   snapshot.trade_summary → plugins/workbench/src/broker_trades.js summarizeBrokerActivity：
//     orders[{order_id, symbol, name, side(1=买入/2=卖出，由 summary 按工具 schema 给出，
//       未知为 null), side_code, qty, filled_qty(cum_qty), price(委托价), avg_fill_price,
//       amount(成交数量×成交均价), fill(未知/全部成交/部分成交/未成交，由 qty 与 cum_qty
//       推导——可自证，不依赖状态码枚举), status_code(券商原始状态码，枚举含义未公开),
//       ordered_at/updated_at(微秒转 ISO，解析失败 null), seen_at, cancelled, modified_count}]
//     actions[{at, action(下单/改单/撤单，按工具名后缀归纳), order_id, ok, detail, entry_id}]
//     queries{count, tools[{tool, count}]}   counts{responses, order_responses, orders,
//     actions, errors}   notice
//   snapshot.activity → store.js recordObservation：{id, at, kind, tool, mode, session_id,
//     is_error, value}（value 已脱敏/截断）——原始响应折叠区只列事实，不解读。
//   trades → plugins/workbench/python/analytics.py trades_view → {mode, count, total,
//     trades:[{date, action, action_label, ticker, shares, price, fee, return, reason,
//     stop, execution_source, execution_source_label}], win_rate, total_fees, note}
// 「只归纳、不推测」：缺字段一律 —，不补默认值；状态码原样展示，不猜标签；
// 台账模式取 snapshot.value.mode（当前账户模式），切换模式不授权下单。
import React from "react";
import { Alert, App, Button, Card, Collapse, Space, Statistic, Table, Tag, Typography } from "antd";
import { callApi } from "../services/api.js";
import { decideDisabled, remainingSeconds, summaryLines } from "../services/confirm.js";
import { useEndpoint, useSnapshotPoll } from "../services/hooks.js";

/** ISO 时间 → 展示（分钟精度）；缺失显示「时间未知」（与旧客户端一致，不编造）。 */
function timeOf(iso) {
  return iso ? String(iso).replace("T", " ").slice(0, 16) : "时间未知";
}

/** 券商/台账原文数值原样展示：缺字段 —，不重算、不截断位数（num 会四舍五入，事实页不用）。 */
function rawCell(value) {
  return value ?? "—";
}

const FILL_COLOR = { 全部成交: "green", 部分成交: "blue", 未成交: "default", 未知: "orange" };

const ORDER_COLUMNS = [
  { title: "委托时间", key: "ordered_at", render: (_f, row) => timeOf(row.ordered_at) },
  { title: "标的", key: "symbol",
    render: (_f, row) => (
      <Space size={4}>
        <span>{row.symbol || "—"}</span>
        {row.name ? <Typography.Text type="secondary">{row.name}</Typography.Text> : null}
      </Space>) },
  // 方向：summary 已按工具 schema（order_side 1=Buy 2=Sell）给出中文；未知时展示原码
  { title: "方向", key: "side", render: (_f, row) => row.side ?? (row.side_code != null ? `side=${row.side_code}` : "—") },
  { title: "数量", key: "qty", align: "right", render: (_f, row) => rawCell(row.qty) },
  { title: "委托价", key: "price", align: "right", render: (_f, row) => rawCell(row.price) },
  { title: "成交均价", key: "avg_fill_price", align: "right", render: (_f, row) => rawCell(row.avg_fill_price) },
  { title: "已成交", key: "filled_qty", align: "right", render: (_f, row) => rawCell(row.filled_qty) },
  // 成交情况由 summary 依据 qty/cum_qty 推导，直接使用
  { title: "成交情况", key: "fill",
    render: (_f, row) => <Tag color={FILL_COLOR[row.fill] ?? "default"}>{row.fill ?? "—"}</Tag> },
  // 状态原码：服务端未公开枚举含义，原样展示，不猜标签
  { title: "状态原码", key: "status_code", render: (_f, row) => rawCell(row.status_code) },
  { title: "成交金额", key: "amount", align: "right", render: (_f, row) => rawCell(row.amount) },
  { title: "订单号", key: "order_id", render: (_f, row) => row.order_id || "—" },
  { title: "备注", key: "lifecycle",
    render: (_f, row) => (
      <Space size={4}>
        {row.cancelled ? <Tag color="red">已撤单（我方记录）</Tag> : null}
        {row.modified_count > 0 ? <Tag>改单 {row.modified_count} 次</Tag> : null}
      </Space>) },
];

/** 原始券商响应条目：只还原「何时调了什么工具、是否报错」，正文折叠供审计核对。 */
function RawResponseItems({ activity }) {
  return (
    <Space direction="vertical" size="small" style={{ width: "100%" }}>
      {(activity ?? []).map((row) => (
        <Collapse
          key={row.id}
          size="small"
          items={[{
            key: row.id,
            label: (
              <Space size={8}>
                <span>{timeOf(row.at)}</span>
                <span>{row.tool ?? row.kind ?? "—"}</span>
                {row.is_error ? <Tag color="red">失败</Tag> : null}
              </Space>),
            children: (
              <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 12, maxHeight: 320, overflow: "auto" }}>
                {JSON.stringify(row.value ?? row, null, 2)}
              </pre>),
          }]} />))}
    </Space>);
}

/** 待确认实盘操作卡片（WP7 任务 3）：10s 轮询 + 1s 倒计时；唯一人工批准通道的页面侧。
 *  文案三类白名单：安全合规（批准=授权该笔实盘操作）/ 操作反馈（已批准、已拒绝、等待确认中）
 *  / 字段标签（operation、工具、摘要行、剩余秒数）。 */
function ConfirmationCard() {
  const { message } = App.useApp();
  const confirmation = useEndpoint("confirmation");
  const pending = confirmation.value?.pending ?? null;
  // 倒计时秒针：只在有待确认时走秒，卸载/无单即清（setInterval 必须有对应 cleanup）
  const [now, setNow] = React.useState(() => Date.now());
  React.useEffect(() => {
    if (!pending) return undefined;
    setNow(Date.now());
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [pending?.id]);
  // 10s 轮询兜底：卡片展示期间即使无人操作也能及时看到超时消失
  React.useEffect(() => {
    if (!pending) return undefined;
    const timer = setInterval(() => confirmation.refresh(), 10_000);
    return () => clearInterval(timer);
  }, [confirmation.refresh, pending?.id]);
  const [busy, setBusy] = React.useState(false);

  if (confirmation.error) {
    return <Typography.Text type="danger">待确认读取失败：{confirmation.error}</Typography.Text>;
  }
  if (!pending) return null;
  const seconds = remainingSeconds(pending.expires_at, now);
  const disabled = decideDisabled(pending, now) || busy;
  const decide = async (decision) => {
    setBusy(true);
    try {
      await callApi("confirm-decide", { id: pending.id, decision });
      message.success(decision === "approved" ? "已批准" : "已拒绝");
      confirmation.refresh();
    } catch (error) {
      message.error(`作答失败：${error.message || error}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card type="inner" title={`待确认实盘操作：${pending.operation ?? "实盘写操作"}`}
      extra={(
        <Space size={8}>
          <Tag color="orange">等待确认中</Tag>
          <Typography.Text type={seconds === null || seconds <= 0 ? "danger" : "warning"}>
            {seconds === null ? "剩余时间未知" : `剩余 ${seconds} 秒`}
          </Typography.Text>
        </Space>)}>
      <Typography.Paragraph type="warning" style={{ marginBottom: 8 }}>
        批准即授权该笔实盘操作；拒绝或 120 秒超时按拒单处理（fail-closed）。
      </Typography.Paragraph>
      <Space direction="vertical" size={2} style={{ width: "100%", marginBottom: 8 }}>
        <Typography.Text>工具：{pending.tool ?? "—"}</Typography.Text>
        <Typography.Text>模式：{pending.mode ?? "—"}</Typography.Text>
        {summaryLines(pending.summary).map((line) => (
          <Typography.Text key={line}>{line}</Typography.Text>))}
      </Space>
      <Space size="small">
        <Button type="primary" danger disabled={disabled} onClick={() => decide("approved")}>
          批准
        </Button>
        <Button disabled={busy} onClick={() => decide("rejected")}>拒绝</Button>
      </Space>
    </Card>);
}

export default function ExecutionPage() {
  const snapshot = useSnapshotPoll();
  const summary = snapshot.value?.trade_summary ?? null;
  const activity = snapshot.value?.activity ?? [];
  const orders = summary?.orders ?? [];
  const actions = summary?.actions ?? [];
  const queries = summary?.queries ?? { count: 0, tools: [] };
  const counts = summary?.counts ?? {};
  // 本地台账成交：与券商响应无关（模拟撮合），单独一区展示。
  // 模式必须取自快照里的当前账户模式（snapshot.value.mode，store.js readMode）；
  // hook 返回值上没有 mode 字段——快照未到位前 payload 为 null 跳过，
  // 避免在实盘模式下先闪现一份 sim 台账。
  const mode = snapshot.value?.mode ?? null;
  const trades = useEndpoint("trades", mode ? { mode, limit: 50 } : null, [mode]);
  const tradeRows = trades.value?.trades ?? [];
  return (
    <Card title="执行">
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {snapshot.error && (
          <Typography.Text type="danger">快照读取失败：{snapshot.error}</Typography.Text>)}
        {snapshot.value?.notice && (
          <Typography.Text type="secondary">{snapshot.value.notice}</Typography.Text>)}
        {/* 待确认实盘操作：确认是服务进程内存态——本卡片覆盖 WP7 交易闸门（服务进程
            发起）的待确认；Harness 会话内发起的确认仍只在 Harness 的工作台面板可见。 */}
        <ConfirmationCard />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          交易确认是进程内存态：上面的卡片只显示本服务进程发起的待确认（经 trade_* 工具
          或计划执行发起）；Harness 会话内发起的待确认本页看不到，请在 Harness 内的工作台
          面板作答。
        </Typography.Text>

        <Card type="inner" title="券商订单（按订单号去重，保留最后一次观测）"
          extra={summary && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              响应 {counts.responses ?? "—"} 条 · 订单响应 {counts.order_responses ?? "—"} 条 ·
              订单 {counts.orders ?? "—"} 笔
            </Typography.Text>)}>
          <Table size="small"
            rowKey={(row) => row.order_id}
            dataSource={orders}
            pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
            scroll={{ x: "max-content" }}
            locale={{ emptyText: snapshot.loading
              ? "订单加载中…"
              : counts.responses
                ? `观察到的 ${counts.responses} 条券商响应中没有订单事实（均为查询类调用）。`
                : "暂无券商响应记录。" }}
            columns={ORDER_COLUMNS} />
        </Card>

        <Card type="inner" title="下单 / 改单 / 撤单动作"
          extra={summary && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              动作 {counts.actions ?? "—"} 次{counts.errors ? ` · 失败 ${counts.errors} 次` : ""}
            </Typography.Text>)}>
          {actions.length === 0 ? (
            <Typography.Text type="secondary">
              {snapshot.loading
                ? "动作记录加载中…"
                : "暂无动作记录；下单/改单/撤单经工作台的 trade_* 工具或计划执行发起。"}
            </Typography.Text>
          ) : (
            <Table size="small"
              rowKey={(row) => row.entry_id ?? `${row.at}-${row.action}`}
              dataSource={actions}
              pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
              locale={{ emptyText: "暂无动作记录。" }}
              columns={[
                { title: "时间", key: "at", render: (_f, row) => timeOf(row.at) },
                { title: "动作", key: "action", render: (_f, row) => row.action ?? "—" },
                { title: "订单号", key: "order_id", render: (_f, row) => row.order_id ?? "—" },
                { title: "结果", key: "ok",
                  render: (_f, row) => (row.ok
                    ? <Tag color="green">成功</Tag>
                    : <Tag color="red">失败{row.detail ? `：${row.detail}` : ""}</Tag>) },
              ]} />
          )}
          {queries.tools.length > 0 && (
            <Typography.Text type="secondary">
              另有 {queries.count} 次只读查询未列出：{queries.tools.map((row) => `${row.tool || "unknown"}×${row.count}`).join("、")}
            </Typography.Text>)}
          {summary?.notice && (
            <Typography.Text type="secondary">{summary.notice}</Typography.Text>)}
        </Card>

        <Card type="inner" title="本地台账成交（模拟撮合，非券商成交）">
          {trades.error && <Alert type="error" showIcon message={`台账读取失败：${trades.error}`} />}
          <Table size="small"
            rowKey={(row) => `${row.date}|${row.ticker}|${row.action}|${row.shares}|${row.price}`}
            dataSource={tradeRows}
            pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
            locale={{ emptyText: trades.loading ? "台账加载中…" : "暂无成交记录。" }}
            columns={[
              { title: "日期", key: "date", render: (_f, row) => row.date ?? "—" },
              { title: "标的", key: "ticker", render: (_f, row) => row.ticker ?? "—" },
              { title: "方向", key: "action_label", render: (_f, row) => row.action_label ?? row.action ?? "—" },
              { title: "数量", key: "shares", align: "right", render: (_f, row) => rawCell(row.shares) },
              { title: "价格", key: "price", align: "right", render: (_f, row) => rawCell(row.price) },
              { title: "费用", key: "fee", align: "right", render: (_f, row) => rawCell(row.fee) },
              { title: "收益", key: "return", align: "right",
                render: (_f, row) => (row.return === null || row.return === undefined
                  ? "—" : `${(Number(row.return) * 100).toFixed(2)}%`) },
              { title: "备注", key: "reason", render: (_f, row) => row.reason ?? "—" },
            ]} />
          {trades.value && (
            <Space size="large" wrap>
              <Statistic title="台账最近" value={trades.value.count ?? "—"} suffix={`/ ${trades.value.total ?? "—"} 笔`} />
              <Statistic title="胜率（卖出计）"
                value={trades.value.win_rate === null || trades.value.win_rate === undefined
                  ? "—" : `${(Number(trades.value.win_rate) * 100).toFixed(0)}%`} />
              <Statistic title="累计费用" value={trades.value.total_fees ?? "—"} />
            </Space>)}
          {trades.value?.note && (
            <Typography.Text type="secondary">{trades.value.note}</Typography.Text>)}
        </Card>

        {activity.length > 0 && (
          <Collapse items={[{
            key: "raw",
            label: `原始券商响应（${activity.length} 条，审计核对用）`,
            children: <RawResponseItems activity={activity} />,
          }]} />)}
        {snapshot.value && activity.length === 0 && (
          <Typography.Text type="secondary">暂无原始券商响应记录。</Typography.Text>)}
      </Space>
    </Card>);
}
