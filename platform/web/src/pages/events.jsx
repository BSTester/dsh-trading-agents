// 事件页：分红/除权除息、财报披露预约、经济数据（events 端点）。
// 字段依据（页面每个取值路径均可指到源码行）：
//   events → plugins/workbench/src/analytics.js events()（ticker 必填；days 默认 180，
//     范围 30..2000，本页取 400）→ plugins/workbench/python/events.py main 输出 →
//     {ticker, as_of, window_days, events:[{date, type, detail, announced?, source,
//     days_until}], sources_status{通道: ok/empty/fail:原因}, note}
//     events 已按日期升序；days_until 为相对今天的自然日差（负数=已过去）。
// 未输入标的时 payload 为 null：不发请求（服务端对空 ticker 直接报 Invalid ticker）。
import React from "react";
import { Card, Input, Space, Tag, Timeline, Typography } from "antd";
import { useEndpoint } from "../services/hooks.js";

function timelineColor(daysUntil) {
  const days = Number(daysUntil);
  if (!Number.isFinite(days)) return "gray";
  if (days < 0) return "gray";          // 已发生
  if (days <= 14) return "red";         // 两周内到期
  return "blue";
}

export default function EventsPage() {
  const [input, setInput] = React.useState("");
  const [ticker, setTicker] = React.useState("");
  const events = useEndpoint("events", ticker ? { ticker, days: 400 } : null, [ticker]);
  const rows = events.value?.events ?? [];
  return (
    <Card title="事件" extra={(
      <Input placeholder="标的代码，如 SH.600519" style={{ width: 220 }} value={input}
        onChange={(event) => setInput(event.target.value)}
        onPressEnter={() => setTicker(input.trim().toUpperCase())} />)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {!ticker && (
          <Typography.Text type="secondary">
            输入标的代码后加载前后 400 天内的分红除权、财报披露预约与经济数据事件。
          </Typography.Text>)}
        {ticker && events.error && (
          <Typography.Text type="danger">事件读取失败：{events.error}</Typography.Text>)}
        {ticker && !events.error && rows.length === 0 && (
          <Typography.Text type="secondary">
            {events.loading
              ? "事件加载中…（分红走富途，A 股另有披露预约补充源）"
              : `该区间无事件（${events.value?.as_of ?? "今天"} 前后 ${events.value?.window_days ?? 400} 天）。`}
          </Typography.Text>)}
        {rows.length > 0 && (
          <Timeline
            items={rows.map((event, index) => ({
              key: `${event.date}-${index}`,
              color: timelineColor(event.days_until),
              children: (
                <Space size={8} wrap>
                  <Tag>{event.type ?? "—"}</Tag>
                  <span>{event.date ?? "—"}</span>
                  <span>{event.detail ?? "—"}</span>
                  {event.days_until != null && (
                    <Typography.Text type="secondary">
                      {Number(event.days_until) >= 0
                        ? `${event.days_until} 天后`
                        : `${-Number(event.days_until)} 天前`}
                    </Typography.Text>)}
                </Space>),
            }))} />)}
        {events.value && (
          <Typography.Text type="secondary">
            来源状态：{Object.entries(events.value.sources_status ?? {})
              .map(([channel, status]) => `${channel}=${status}`).join(" · ") || "—"} ·
            {" "}数据截至 {events.value.as_of ?? "—"}
          </Typography.Text>)}
        {events.value?.note && (
          <Typography.Text type="secondary">{events.value.note}</Typography.Text>)}
      </Space>
    </Card>);
}
