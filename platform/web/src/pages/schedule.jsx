// 调度页：daemon 心跳新鲜度、作业历史、kill switch、日内熔断、告警分级（含 critical 常驻红字）。
// 字段依据（页面每个取值路径均可指到源码行）：
//   schedule 端点 → platform/server/app.py:199-212 → snapshots.py:49-68 schedule_snapshot：
//     {heartbeat, critical, critical_title, kill, halt, jobs}（caches.py:70 的最小字段要求为
//     heartbeat/jobs）。
//     heartbeat：~/.dsh/trading-daemon.json 的原文（snapshots.py:53-59 读取，损坏按无心跳），
//       由 daemon.tick 写 {"heartbeat","last_job","next"}（daemon.py:105-107，时间戳形如
//       "YYYY-MM-DD HH:MM:SS"）；alerts.emit 追加 {"critical","critical_title"}
//       （alerts.py:20-23，心跳用 setdefault 兜底，缺失时写成 ISO 的 "T" 分隔格式）。
//       故页面解析同时接受空格与 T 两种分隔（本页 parseHeartbeat）。
//     critical/critical_title：bool(heartbeat.critical) 与 heartbeat.critical_title
//       （snapshots.py:64-65）——critical 告警未被 daemon 清掉前常驻。
//     kill：~/.dsh/trading-kill 是否存在（snapshots.py:66 → daemon.kill_path，daemon.py:39-40）；
//       risk.py 同一条 kill 文件事实（daemon.py:6）。
//     halt：store.is_halted(conn) → kv "halt:active".active（store.py:392-394）。
//     jobs：kv "daemon:state".ran 展开为 [{job, ran}]，按 ran 倒序（snapshots.py:60-62）；
//       键形如 "{市场}:{作业名}:{日期}"，值是该作业最近一次运行时间（daemon.py:99-103）。
//   reconcile.alerts → snapshots.py:71-94 reconcile_snapshot → alerts.list_recent：
//     [{level, title, detail, created_at}]（alerts.py:30-34；表结构 store.py:58-60）。
//   plan-execute kill/unkill → app.py:99-105 白名单映射到指令类型，daemon.py:128-133 只落/删
//     kill 文件；页面提交后刷新 schedule 端点确认回写。
// 「不猜」：作业表只列实际返回的 job/ran；缺字段一律 —。
import React from "react";
import { Alert, App, Button, Card, Space, Table, Tag, Typography } from "antd";
import { callApi } from "../services/api.js";
import { useEndpoint } from "../services/hooks.js";
import { stampOf } from "../services/format.jsx";

const HEARTBEAT_STALE_MS = 5 * 60_000;

const ALERT_LEVEL_COLOR = { critical: "red", warn: "gold", info: "blue" };

/** 心跳时间戳解析：daemon 写 "YYYY-MM-DD HH:MM:SS"，alerts.emit 可能写 ISO 的 T 分隔。 */
function parseHeartbeat(text) {
  if (typeof text !== "string" || !text) return NaN;
  return Date.parse(text.replace(" ", "T"));
}

/** 距今分钟数（向下取整）；无法解析返回 null。 */
function minutesSince(ms) {
  if (!Number.isFinite(ms)) return null;
  return Math.max(0, Math.floor((Date.now() - ms) / 60_000));
}

const JOB_COLUMNS = [
  { title: "最近运行", key: "ran", render: (_field, row) => row.ran ?? "—" },
  // 作业键形如 市场:作业名:日期（daemon.py:99），原样展示，不拆字段也不补状态
  { title: "作业", key: "job", render: (_field, row) => row.job ?? "—" },
];

// 告警时间是 alerts.emit 写的 ISO（T 分隔）：展示走共享 stampOf（T→空格、秒级）。
const ALERT_COLUMNS = [
  { title: "级别", key: "level",
    render: (_field, row) => (
      <Tag color={ALERT_LEVEL_COLOR[row.level] ?? "default"}>{row.level ?? "—"}</Tag>) },
  { title: "标题", key: "title", render: (_field, row) => row.title ?? "—" },
  { title: "详情", key: "detail", render: (_field, row) => row.detail ?? "—" },
  { title: "时间", key: "created_at", render: (_field, row) => stampOf(row.created_at) },
];

export default function SchedulePage() {
  const { message, modal } = App.useApp();
  const schedule = useEndpoint("schedule", {}, []);
  const reconcile = useEndpoint("reconcile", {}, []);
  const [busy, setBusy] = React.useState(false);

  const value = schedule.value ?? {};
  const heartbeat = value.heartbeat ?? {};
  // 首屏尚未拿到快照时不能断言失联：loading 且无 value 属「读取中」，非「心跳缺失」
  const pending = schedule.loading && !schedule.value;
  const hbMs = parseHeartbeat(heartbeat.heartbeat);
  const stale = !Number.isFinite(hbMs) || (Date.now() - hbMs) > HEARTBEAT_STALE_MS;
  const minutes = minutesSince(hbMs);
  const killActive = value.kill === true;
  const halted = value.halt === true;
  const critical = value.critical === true;
  const jobs = value.jobs ?? [];
  const alerts = reconcile.value?.alerts ?? [];
  // 告警行没有服务端 id：键在渲染前一次算好（antd 的 rowKey 不再传下标）
  const alertRows = alerts.map((row, index) => ({
    ...row, _key: `${row.created_at ?? ""}|${row.title ?? ""}|${index}` }));

  const act = async (action, doneMessage) => {
    setBusy(true);
    try {
      await callApi("plan-execute", { action });
      message.success(doneMessage);
      schedule.refresh();
      reconcile.refresh();
    } catch (error) {
      message.error(`提交失败：${error.message || error}`);
    } finally {
      setBusy(false);
    }
  };

  const activateKill = () => act("kill", "已提交激活指令，等待 daemon 写 kill 文件。");

  const confirmUnkill = () => {
    modal.confirm({
      title: "解除 kill switch",
      content: "unkill = 人工确认后解除；确认已完成券商核对？",
      okText: "确认解除",
      cancelText: "取消",
      onOk: () => act("unkill", "已提交解除指令，等待 daemon 删除 kill 文件。"),
    });
  };

  return (
    <Card title="调度" extra={(
      <Space size="small">
        <Button size="small" disabled={busy}
          onClick={() => { schedule.refresh(); reconcile.refresh(); }}>刷新</Button>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          页面只读快照；kill 与熔断状态由 daemon 落盘事实决定
        </Typography.Text>
      </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {schedule.error && (
          <Alert type="error" showIcon message={`调度读取失败：${schedule.error}`} />)}
        {reconcile.error && (
          <Alert type="error" showIcon message={`告警读取失败：${reconcile.error}`} />)}

        {/* critical 告警常驻：daemon 未清掉 heartbeat.critical 前一直显示 */}
        {critical && (
          <Alert type="error" showIcon
            message={`critical 告警：${value.critical_title ?? "—"}`} />)}

        <Card type="inner" title="daemon 状态">
          <Space direction="vertical" size={4} style={{ width: "100%" }}>
            <Space size="small" wrap>
              <Typography.Text type="secondary">心跳</Typography.Text>
              <Tag color={pending ? "default" : stale ? "red" : "green"}>
                {pending ? "读取中" : stale ? "失联" : "正常"}
              </Tag>
              <Typography.Text>
                {pending
                  ? "心跳读取中…"
                  : heartbeat.heartbeat
                    ? `心跳 ${minutes ?? "—"} 分钟前`
                    : "心跳缺失（daemon 未运行）"}
              </Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {heartbeat.heartbeat ?? "—"}
              </Typography.Text>
            </Space>
            <Space size="small" wrap>
              <Typography.Text type="secondary">kill switch</Typography.Text>
              <Tag color={killActive ? "red" : "default"}>{killActive ? "生效中" : "未激活"}</Tag>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {killActive ? "生效后拒绝一切订单" : "未阻止下单"}
              </Typography.Text>
            </Space>
            <Space size="small" wrap>
              <Typography.Text type="secondary">日内熔断</Typography.Text>
              <Tag color={halted ? "red" : "default"}>{halted ? "已触发" : "未触发"}</Tag>
            </Space>
            {heartbeat.last_job && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                最近作业 {heartbeat.last_job}
              </Typography.Text>)}
            <Space size="small" wrap>
              {killActive
                ? <Button danger disabled={busy} onClick={confirmUnkill}>解除 kill switch</Button>
                : <Button type="primary" danger disabled={busy} onClick={activateKill}>
                    激活 kill switch
                  </Button>}
              {busy && (
                <Typography.Text type="secondary">已提交，等待 daemon 回写状态…</Typography.Text>)}
            </Space>
          </Space>
        </Card>

        <Card type="inner" title="作业历史">
          <Table size="small"
            rowKey={(row) => row.job}
            dataSource={jobs}
            pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
            locale={{ emptyText: schedule.loading
              ? "作业记录加载中…"
              : "暂无作业记录（daemon 未运行或今日休市）。" }}
            columns={JOB_COLUMNS} />
        </Card>

        <Card type="inner" title="告警">
          <Table size="small"
            rowKey={(row) => row._key}
            dataSource={alertRows}
            pagination={{ pageSize: 10, hideOnSinglePage: true, showSizeChanger: false }}
            locale={{ emptyText: reconcile.loading ? "告警加载中…" : "暂无告警。" }}
            columns={ALERT_COLUMNS} />
        </Card>
      </Space>
    </Card>);
}
