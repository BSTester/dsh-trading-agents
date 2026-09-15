// 计划页：当前冻结计划的目标→订单 diff、逐单预检结论、受约束执行入口（唯一写指令路径）。
// 字段依据（页面每个取值路径均可指到源码行）：
//   plan 端点 → platform/server/app.py:199-212：只读端点经 trading_core 子命令取数，
//     plan 额外并入「当前账户模式」store_access.snapshot(home)["mode"]（app.py:208-209），
//     因此载荷顶层是 {plans, alerts, mode}（caches.py:69 的最小字段要求为 plans/alerts）。
//     plans 来自 plugins/core/python/trading_core/snapshots.py:37-46 plan_snapshot：
//       {plan_id, as_of, mode, strategy_id, target, content_hash, status, created_at,
//        orders:[{client_order_id, symbol, side, qty, price, status, broker_order_id,
//                 risk_verdict}]}
//     计划行来自 store.list_plans（store.py:315-322，ORDER BY created_at, plan_id），
//     订单来自 store.get_orders_by_plan（store.py:344-346，ORDER BY rowid）与
//     snapshots._orders_of（snapshots.py:23-34）。
//   risk_verdict → snapshots._verdict_of（snapshots.py:10-20）：取该计划内该标的
//     最新一条 risk_checks。放行为「预检通过」，拦截为「规则N拦截：理由」，从未预检为 None。
//   side 取值 BUY/SELL、price 为限价 → planner.py:29-33 生成订单时写入。
//   created_at 为计划落库时间（planner.py:35-40 INSERT plans.created_at）。
//   订单状态枚举 → trading_core/oms.py:13-14 STATES（我方自有状态机，非券商状态码）。
//   plan-execute → platform/server/app.py:213-241：动作白名单 5 种（app.py:99-105）；
//     execute 必须带 plan_hash（app.py:226-228）、expected_mode 必须等于服务侧当前模式
//     （app.py:229-232）、live 必须逐字带口令「确认执行」（app.py:233-234，口令不落盘）；
//     校验通过只原子写指令文件即返回（app.py:240-241），口令字段不进 command_payload。
//   取消语义 → daemon.py:219-234 _cancel_plan：只本地撤销 draft/frozen，在途订单交对账兜底。
// 「不猜」：缺字段一律 —；状态只按我方状态机翻译，未知枚举原样展示。
import React from "react";
import { Alert, App, Button, Card, Input, Space, Table, Tag, Typography } from "antd";
import { callApi } from "../services/api.js";
import { useEndpoint } from "../services/hooks.js";
import { num } from "../services/format.jsx";

const SIDE = { BUY: "买入", SELL: "卖出" };

const ORDER_STATUS = { draft: "草稿", frozen: "已冻结", submitting: "提交中", submitted: "已提交",
                       partial: "部分成交", filled: "已成交", cancelled: "已撤销",
                       rejected: "已拒绝", unknown: "未知" };

const ORDER_STATUS_COLOR = { draft: "default", frozen: "blue", submitting: "processing",
                             submitted: "processing", partial: "gold", filled: "green",
                             cancelled: "default", rejected: "red", unknown: "orange" };

const PLAN_STATUS = { draft: "草稿", frozen: "已冻结", approved: "已批准", executing: "执行中" };

const ORDER_COLUMNS = [
  { title: "标的", key: "symbol", render: (_field, row) => row.symbol ?? "—" },
  // 方向：计划生成时写入 BUY/SELL（planner.py:31），未知值原样展示
  { title: "方向", key: "side", render: (_field, row) => SIDE[row.side] ?? row.side ?? "—" },
  { title: "数量", key: "qty", align: "right", render: (_field, row) => num(row.qty, 0) },
  { title: "限价", key: "price", align: "right", render: (_field, row) => num(row.price) },
  { title: "状态", key: "status",
    render: (_field, row) => (
      <Tag color={ORDER_STATUS_COLOR[row.status] ?? "default"}>
        {ORDER_STATUS[row.status] ?? row.status ?? "—"}
      </Tag>) },
  // 预检结论来自 risk_checks 最新一条；从未预检显示 —
  { title: "预检", key: "risk_verdict", render: (_field, row) => row.risk_verdict ?? "—" },
];

export default function PlanPage() {
  const { message } = App.useApp();
  const plan = useEndpoint("plan", {}, []);
  const [confirmText, setConfirmText] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  if (plan.loading && !plan.value) return <Card loading />;
  if (plan.error) {
    return (
      <Card title="计划">
        <Typography.Text type="danger">计划读取失败：{plan.error}</Typography.Text>
      </Card>);
  }

  const value = plan.value ?? {};
  const plans = value.plans ?? [];
  const current = plans[0];
  const orders = current?.orders ?? [];
  const live = value.mode === "live";
  const frozen = current?.status === "frozen";

  const submit = async (action) => {
    if (!current || busy) return;
    // 实时账户逐字口令门槛：页面与服务侧（app.py:233-234）各校验一次，不满足则不发请求
    if (action === "execute" && live && confirmText !== "确认执行") {
      message.warning("实时账户执行需输入口令：确认执行");
      return;
    }
    setBusy(true);
    try {
      await callApi("plan-execute", action === "execute"
        ? { plan_hash: current.content_hash, expected_mode: value.mode,
            confirmation: live ? "确认执行" : undefined }
        : { plan_hash: current.content_hash, expected_mode: value.mode, action: "cancel" });
      message.success("已提交，等待 daemon 回写状态…");
      plan.refresh();
    } catch (error) {
      message.error(`提交失败：${error.message || error}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="计划" extra={(
      <Space size="small">
        <Typography.Text type="secondary">账户模式</Typography.Text>
        <Tag color={live ? "red" : "green"}>{live ? "实盘 LIVE" : "模拟 SIM"}</Tag>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          模式只影响执行校验，切换模式不等于授权下单
        </Typography.Text>
      </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {!current && (
          <Typography.Text type="secondary">暂无计划（daemon 生成后显示）。</Typography.Text>)}

        {current && (
          <Card type="inner" title="当前计划" extra={(
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              历史计划共 {plans.length} 个
            </Typography.Text>)}>
            <Space direction="vertical" size={4} style={{ width: "100%" }}>
              <Space size="small" wrap>
                <Typography.Text type="secondary">计划</Typography.Text>
                <Typography.Text code>{current.plan_id ?? "—"}</Typography.Text>
                <Tag color={current.status === "frozen" ? "blue" : "default"}>
                  {PLAN_STATUS[current.status] ?? current.status ?? "—"}
                </Tag>
              </Space>
              <Space size="small" wrap>
                <Typography.Text type="secondary">内容哈希</Typography.Text>
                <Typography.Text code>{current.content_hash ?? "—"}</Typography.Text>
              </Space>
              <Typography.Text type="secondary">
                冻结时间 {current.created_at ?? "—"} · 计划记录模式 {current.mode ?? "—"} ·
                策略 {current.strategy_id ?? "—"} · 基准日 {current.as_of ?? "—"}
              </Typography.Text>
            </Space>
            <Table size="small" style={{ marginTop: 12 }}
              rowKey={(row) => row.client_order_id}
              dataSource={orders}
              pagination={false}
              scroll={{ x: "max-content" }}
              locale={{ emptyText: "该计划没有订单（无差异目标与取不到价格的目标在生成时跳过）。" }}
              columns={ORDER_COLUMNS} />
          </Card>)}

        {current && frozen && (
          <Card type="inner" title="执行">
            <Space direction="vertical" size="small" style={{ width: "100%" }}>
              {live && (
                <Alert type="warning" showIcon
                  message="实时账户执行：输入口令「确认执行」；执行已冻结计划是唯一下单入口" />)}
              {live && (
                <Input value={confirmText} placeholder="输入：确认执行" autoComplete="off"
                  style={{ maxWidth: 260 }} disabled={busy}
                  aria-label="确认执行"
                  onChange={(event) => setConfirmText(event.target.value)} />)}
              <Space size="small" wrap>
                <Button type={live ? "primary" : "default"} danger={live} disabled={busy}
                  onClick={() => submit("execute")}>
                  {live ? "执行（实时账户）" : "执行计划"}
                </Button>
                <Button disabled={busy} onClick={() => submit("cancel")}>取消计划</Button>
                {busy && (
                  <Typography.Text type="secondary">已提交，等待 daemon 回写状态…</Typography.Text>)}
              </Space>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                提交只落指令文件，状态由 daemon 回写；取消只撤销未提交订单，在途订单以对账为准。
              </Typography.Text>
            </Space>
          </Card>)}

        {current && !frozen && (
          <Typography.Text type="secondary">
            当前计划状态为 {PLAN_STATUS[current.status] ?? current.status ?? "—"}，非冻结态不显示执行区。
          </Typography.Text>)}

        {/* 计划列表：端点按 created_at 升序返回全部计划（store.py:316），
            上方「当前计划」取第一条——列出来便于核对是否存在更晚的计划。 */}
        {plans.length > 1 && (
          <Card type="inner" title="计划列表" extra={(
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              端点按创建时间升序返回 {plans.length} 条，上方「当前计划」为第一条
            </Typography.Text>)}>
            <Table size="small"
              rowKey={(row) => row.plan_id}
              dataSource={plans}
              pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
              scroll={{ x: "max-content" }}
              columns={[
                { title: "计划", key: "plan_id",
                  render: (_field, row) => <Typography.Text code>{row.plan_id ?? "—"}</Typography.Text> },
                { title: "状态", key: "status",
                  render: (_field, row) => PLAN_STATUS[row.status] ?? row.status ?? "—" },
                { title: "内容哈希", key: "content_hash",
                  render: (_field, row) => <Typography.Text code>{row.content_hash ?? "—"}</Typography.Text> },
                { title: "创建时间", key: "created_at", render: (_field, row) => row.created_at ?? "—" },
                { title: "订单数", key: "orders", align: "right",
                  render: (_field, row) => (row.orders ?? []).length },
              ]} />
          </Card>)}
      </Space>
    </Card>);
}
