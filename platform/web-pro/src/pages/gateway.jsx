// V3 工作台「网关与调度」页（Ant Design Pro）。
// 功能模块与设计稿版 /v3/gateway.html（规格书 = public/v3/gateway.js）**一一对应**：
//   1. 三通道卡（MCP Bridge 真实计数；SDK JSON-RPC / Headless CLI 本服务未挂载 → 无数据源 + reason）
//   2. Headless 调用日志表（headless.last：时间/结果/exit/耗时/token；无记录 → 空态）
//   3. 调度器（daemon 心跳 + schedule 作业历史；定时规则表恒空 → 说明规则在 install/*.timer）
//   4. 熔断保护（并发/超时/token 预算/今日 token 本服务不自管 → 如实标注；kill/halt/critical 真实）
//   5. Profile 与 Bundle（工具面不返回 → 无数据源）
//   6. 调用分布（BarList ← /api/v3/metrics 的真实进程内计数）
// 数据：只走既有读通道 GET /api/v3/*，本页没有任何写动作。
import React from "react";
import { Alert, Badge, Descriptions, Empty, Space, Statistic, Table, Tag, Typography } from "antd";
import { ProCard } from "@ant-design/pro-components";
import { fmt, noSourceText, useV3 } from "../services/api.js";
import { BarList } from "../components/charts.jsx";

const { Text, Paragraph } = Typography;

/** 统一「无数据源」区块：措辞来自 api.js 的 noSourceText，页面不自造说法。 */
function NoSource({ what, why, type = "warning" }) {
  return <Alert type={type} showIcon message={noSourceText(what, why)} />;
}

/** 中性空态图形：不使用 antd 内置空态图（其 <title> 带通用占位文案），统一用虚线框。 */
const EMPTY_FRAME = (
  <svg width="48" height="36" viewBox="0 0 48 36" aria-hidden="true">
    <rect x="3.5" y="7.5" width="41" height="21" rx="3" fill="none" stroke="#303a48" strokeDasharray="4 3" />
    <path d="M9 24l6-6 5 5 4-4 6 6" fill="none" stroke="#3f4a5a" strokeWidth="1.5" />
  </svg>
);

/** 表体空态：显式写明无数据源，不落到 antd 的通用空文案。 */
const emptyTable = (what, why) => ({
  emptyText: <Empty image={EMPTY_FRAME} description={noSourceText(what, why)} />,
});

/** 取记录里第一个有值的字段（字段缺失显示「—」，绝不猜数值）。 */
function pick(record, keys) {
  for (const key of keys) {
    const value = record?.[key];
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return undefined;
}

function channelTag(status) {
  if (status === "running") return <Tag color="success">运行中</Tag>;
  if (status === "unavailable") return <Tag color="warning">无数据源</Tag>;
  return <Tag>{fmt.dash(status)}</Tag>;
}

const REASON = {
  sdk: "本服务未挂载 SDK JSON-RPC 通道（无 dsh SDK 会话）",
  headless: "本服务未挂载 Headless CLI 子通道（无 dsh --profile headless 子进程调度）",
};

/** 通道卡里的「指标 + 值」小块（对应设计稿 .ch-metrics .m / .v / .l）。 */
function Stat({ label, value, suffix, hint }) {
  return (
    <div style={{ minWidth: 108 }}>
      <Statistic
        title={label}
        value={value}
        suffix={suffix ? <span style={{ fontSize: 12 }}>{suffix}</span> : undefined}
        valueStyle={{ fontSize: 18, fontVariantNumeric: "tabular-nums" }}
      />
      {hint ? <Text type="secondary" style={{ fontSize: 11 }}>{hint}</Text> : null}
    </div>
  );
}

/** 三通道之一：MCP Bridge（真实运行态与计数）。 */
function McpChannelCard({ gateway, metrics }) {
  const mcp = gateway.channels?.mcp ?? {};
  const calls = Number(metrics.mcp?.calls ?? 0);
  const errors = Number(metrics.mcp?.errors ?? 0);
  const successRate = calls > 0 ? `${(((calls - errors) / calls) * 100).toFixed(1)}%` : "—";
  return (
    <ProCard
      title={<Space size={8}>MCP Bridge {channelTag(mcp.status)}</Space>}
      bordered
      extra={<Text type="secondary" style={{ fontSize: 11 }}>工具 {fmt.dash(mcp.tools ?? metrics.toolTotal)} 个</Text>}
    >
      <Descriptions size="small" column={1} items={[
        { key: "protocol", label: "协议标识", children: fmt.dash(mcp.protocol) },
      ]} />
      <div style={{ display: "flex", flexWrap: "wrap", gap: 18, marginTop: 10 }}>
        <Stat label="已注册工具" value={fmt.dash(mcp.tools ?? metrics.toolTotal)} suffix="个" />
        <Stat label="今日调用" value={calls} suffix="次" hint="进程内计数" />
        <Stat label="成功率" value={successRate} hint={`失败 ${errors} 次`} />
        <Stat label="平均延迟" value={Number(metrics.mcp?.avgMs ?? 0)} suffix="ms" hint="metrics 无 P95 口径" />
      </div>
      <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
        MCP 服务端状态机只有 start / stop / update 三个动作，没有状态查询端点；
        此处按 <Text code>{`channels.mcp.status=${fmt.dash(mcp.status)}`}</Text> 标注。
      </Paragraph>
    </ProCard>
  );
}

/** 三通道之一：本服务未挂载的通道（SDK / Headless），整卡标无数据源。 */
function UnavailableChannelCard({ title, channel, reason, extraRows = [], footer }) {
  const kvs = [
    {
      key: "protocol",
      label: "协议标识",
      children: channel?.protocol
        ? fmt.dash(channel.protocol)
        : <Text type="secondary">无数据源（工具面未返回该通道协议标识）</Text>,
    },
    ...extraRows,
  ];
  return (
    <ProCard title={<Space size={8}>{title} {channelTag(channel?.status)}</Space>} bordered>
      <Descriptions size="small" column={1} items={kvs} />
      <div style={{ marginTop: 10 }}>
        <NoSource what={`${title} 运行指标`} why={reason ?? channel?.reason} />
      </div>
      {footer ? (
        <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>{footer}</Paragraph>
      ) : null}
      <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 6, marginBottom: 0 }}>
        状态 <Text code>{fmt.dash(channel?.status)}</Text> · 本页不保留任何占位数字
      </Paragraph>
    </ProCard>
  );
}

export default function GatewayPage() {
  const gateway = useV3("gateway");
  const metrics = useV3("metrics");
  const g = gateway.value ?? {};
  const m = metrics.value ?? {};

  const channels = g.channels ?? {};
  const statuses = ["mcp", "sdk", "headless"].map((key) => String(channels?.[key]?.status ?? ""));
  const upCount = statuses.filter((value) => value === "running").length;

  const scheduler = g.scheduler ?? {};
  const heartbeat = scheduler.heartbeat ?? {};
  const jobs = Array.isArray(scheduler.recent) ? scheduler.recent : [];
  const rules = Array.isArray(scheduler.rules) ? scheduler.rules : [];
  const headlessLast = Array.isArray(g.headless?.last) ? g.headless.last : [];
  const today = g.headless?.today ?? {};
  const tripped = Boolean(scheduler.kill || scheduler.halt || scheduler.critical);
  const toolCalls = Object.entries(m.mcp?.tools ?? {}).filter(([, value]) => Number.isFinite(Number(value)));

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      <ProCard
        title="网关与调度"
        bordered
        extra={
          <Space size={8}>
            <Badge
              status={upCount === 3 ? "success" : upCount === 0 ? "error" : "warning"}
              text={`${upCount} / 3 通道可用（MCP ${fmt.dash(statuses[0])} · SDK ${fmt.dash(statuses[1])} · Headless ${fmt.dash(statuses[2])}）`}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>数据时点 {fmt.stamp(g.generated_at)}</Text>
          </Space>
        }
      >
        {gateway.loading ? <Text type="secondary">读取中…</Text> : null}
        {gateway.error ? (
          <NoSource what="网关与调度" why={`/api/v3/gateway 取数失败：${gateway.error}`} type="error" />
        ) : null}
        {!gateway.loading && !gateway.error ? (
          <Descriptions size="small" column={2} items={[
            { key: "src", label: "数据来源", children: <Text code>/api/v3/gateway · /api/v3/metrics</Text> },
            { key: "hint", label: "通道语义", children: "MCP Bridge 由本服务挂载；SDK JSON-RPC 与 Headless CLI 未挂载（如实标注，不占位）" },
          ]} />
        ) : null}
      </ProCard>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))", gap: 12 }}>
        <McpChannelCard gateway={g} metrics={m} />
        <UnavailableChannelCard
          title="SDK JSON-RPC"
          channel={channels.sdk}
          reason={channels.sdk?.reason ?? REASON.sdk}
          extraRows={[{
            key: "session",
            label: "活跃会话 / 初始化握手",
            children: <Text type="secondary">无数据源（本服务未挂载 SDK 通道）</Text>,
          }]}
          footer="本服务没有 dsh SDK 会话，因此没有 session / prompt 事件可展示。"
        />
        <UnavailableChannelCard
          title="Headless CLI"
          channel={channels.headless}
          reason={channels.headless?.reason ?? REASON.headless}
          extraRows={[{
            key: "command",
            label: "命令形态",
            children: <Text code>{fmt.dash(channels.headless?.command)}</Text>,
          }, {
            key: "wake",
            label: "今日唤醒 / 成功 / 失败 / 平均耗时",
            children: (
              <Text type="secondary">
                {`唤醒 ${fmt.dash(today.total)} · 成功 ${fmt.dash(today.success)} · 失败 ${fmt.dash(today.failed)} · 平均 ${fmt.dash(today.avgMs)}ms`}
                （本服务不拉起 headless 子进程，计数恒为 0）
              </Text>
            ),
          }]}
        />
      </div>

      <ProCard
        title="Headless 调用日志"
        bordered
        extra={headlessLast.length === 0
          ? <Tag color="warning">无数据源 · 本服务未挂载 Headless 子进程</Tag>
          : <Tag color="success">{`${headlessLast.length} 条`}</Tag>}
      >
        {headlessLast.length === 0 ? (
          <Empty
            image={EMPTY_FRAME}
            description={noSourceText(
              "Headless 调用日志",
              `${g.headless?.reason ?? REASON.headless}；channels.headless.status=${fmt.dash(channels.headless?.status)}，无 exit_code / stdout / token 记录`,
            )}
          />
        ) : (
          <Table
            size="small"
            rowKey={(record, index) => String(pick(record, ["id", "at", "time"]) ?? index)}
            dataSource={headlessLast}
            pagination={false}
            locale={emptyTable("Headless 调用日志", "headless.last 为空数组")}
            columns={[
              { title: "时间", render: (_, record) => fmt.stamp(pick(record, ["at", "time", "started_at"])) },
              { title: "结果", render: (_, record) => fmt.dash(pick(record, ["result", "status", "ok"])) },
              { title: "exit", render: (_, record) => fmt.dash(pick(record, ["exit_code", "exitCode", "code"])) },
              { title: "耗时(ms)", render: (_, record) => fmt.dash(pick(record, ["durationMs", "ms", "elapsed_ms"])) },
              { title: "token", render: (_, record) => fmt.dash(pick(record, ["tokens", "token_usage", "tokens_total"])) },
            ]}
          />
        )}
      </ProCard>

      <ProCard
        title="调度器"
        bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {`定时规则 ${rules.length} 条（工具面不提供）· 作业历史 ${jobs.length} 条 · 心跳 ${fmt.dash(heartbeat.heartbeat)}`}
        </Text>}
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(300px, 1fr))", gap: 12 }}>
          <div>
            <Text strong>daemon 心跳 · 实时</Text>
            <Descriptions size="small" column={1} style={{ marginTop: 8 }} items={[
              { key: "hb", label: "最近心跳", children: fmt.dash(heartbeat.heartbeat) },
              { key: "last", label: "最近一次作业", children: fmt.dash(heartbeat.last_job) },
              { key: "next", label: "下次触发", children: fmt.dash(heartbeat.next) },
            ]} />
            <Space size={6} wrap style={{ marginTop: 8 }}>
              <Tag color={scheduler.kill ? "error" : "default"}>{`kill ${scheduler.kill ? "true" : "false"}`}</Tag>
              <Tag color={scheduler.halt ? "error" : "default"}>{`halt ${scheduler.halt ? "true" : "false"}`}</Tag>
              <Tag color={scheduler.critical ? "error" : "default"}>{`critical ${scheduler.critical ? "true" : "false"}`}</Tag>
            </Space>
          </div>
          <div>
            <Text strong>{`作业历史 · 最近 ${Math.min(jobs.length, 8)} 次`}</Text>
            <div style={{ marginTop: 8 }}>
              {jobs.length === 0 ? (
                <Empty image={EMPTY_FRAME} description={noSourceText("作业历史", "schedule 工具未返回 jobs")} />
              ) : (
                <Table
                  size="small"
                  rowKey={(record, index) => `${record.job ?? "job"}-${index}`}
                  dataSource={jobs.slice(0, 8)}
                  pagination={false}
                  locale={emptyTable("作业历史", "schedule 工具未返回 jobs")}
                  columns={[
                    { title: "作业", render: (_, record) => String(record.job ?? "—").split(":").slice(1).join(":") || String(record.job ?? "—") },
                    { title: "市场", width: 80, render: (_, record) => String(record.job ?? "").split(":")[0] || "—" },
                    { title: "运行于", width: 170, render: (_, record) => fmt.dash(record.ran) },
                  ]}
                />
              )}
            </div>
          </div>
          <div>
            <Text strong>定时规则</Text>
            <div style={{ marginTop: 8 }}>
              <NoSource
                what="定时规则表"
                why="规则定义在 install/*.timer 与配置里，不在工具面（scheduler.rules 恒为空数组）"
              />
            </div>
            {scheduler.note ? (
              <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 8, marginBottom: 0 }}>{scheduler.note}</Paragraph>
            ) : null}
            <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 8, marginBottom: 0 }}>
              本页调度开关不持久化：工具面没有可写规则表，规则改动只在 install/*.timer 与配置文件里完成。
            </Paragraph>
          </div>
        </div>
      </ProCard>

      <ProCard
        title="熔断保护"
        bordered
        extra={
          <Space size={8}>
            <Tag color={tripped ? "error" : "success"}>{tripped ? "熔断已触发" : "未触发 · 保护待命"}</Tag>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {`服务端 kill=${scheduler.kill ? "true" : "false"} · halt=${scheduler.halt ? "true" : "false"} · critical=${scheduler.critical ? "true" : "false"}`}
            </Text>
          </Space>
        }
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
          <NoSource what="并发上限" why="Headless 子进程不自管并发位，工具面无该阈值" />
          <NoSource what="单次超时" why="无 Headless 子进程调度，无超时配置来源" />
          <NoSource what="单次 token 预算" why="本服务不消耗模型 token，无预算来源" />
          <NoSource what="今日累计 token" why="本服务不消耗模型 token，无用量来源" />
        </div>
        <Alert
          style={{ marginTop: 12 }}
          type="warning"
          showIcon
          message={`强制 kill 记录：无数据源 · ${g.headless?.reason ?? REASON.headless}（headless.today.killed=${fmt.dash(today.killed)}）`}
        />
      </ProCard>

      <ProCard title="Profile 与 Bundle" bordered>
        <NoSource
          what="Profile 与 Bundle"
          why="工具面不返回 bundle 的名称 / 版本 / 条目数 / 加载状态；平台按角色运行，不使用 dsh profile"
        />
        <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0 }}>
          本页不保留任何占位条目；可用 bundle 清单见 Harness 侧 cordis.yml。
        </Paragraph>
      </ProCard>

      <ProCard
        title="调用分布"
        bordered
        extra={<Text type="secondary" style={{ fontSize: 12 }}>来源 /api/v3/metrics · 本服务进程内计数</Text>}
      >
        {metrics.error ? (
          <NoSource what="调用分布" why={`/api/v3/metrics 取数失败：${metrics.error}`} type="error" />
        ) : toolCalls.length === 0 ? (
          <Empty
            image={EMPTY_FRAME}
            description={noSourceText("工具调用分布", "本服务进程内暂无调用计数（metrics.mcp.tools 为空对象）")}
          />
        ) : (
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            <BarList items={toolCalls} />
            <Descriptions size="small" column={3} items={[
              { key: "calls", label: "调用总数", children: `${fmt.dash(m.mcp?.calls)} 次` },
              { key: "errors", label: "失败次数", children: `${fmt.dash(m.mcp?.errors)} 次` },
              { key: "avg", label: "平均耗时", children: `${fmt.dash(m.mcp?.avgMs)} ms` },
              { key: "http", label: "HTTP 请求", children: `${fmt.dash(m.http?.requests)} 次（错误 ${fmt.dash(m.http?.errors)}）` },
              { key: "wb", label: "工作台调用", children: `${fmt.dash(m.wb?.calls)} 次（错误 ${fmt.dash(m.wb?.errors)}）` },
              { key: "oms", label: "OMS 台账分级", children: Object.entries(m.oms ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "—" },
            ]} />
          </Space>
        )}
      </ProCard>
    </Space>
  );
}
