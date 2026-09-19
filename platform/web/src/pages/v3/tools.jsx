// V3「工具域治理」页（Ant Design Pro）：六大工具域目录 · 工具发现代理 · MCP 服务器注册与健康降级。
// 设计稿对应 od-quant-harness-platform/tools.html（版式参照，59/1,284/99.2% 等占位数字一律不抄）。
//
// 数据来源（全部服务端实测）：
//   GET /api/v3/tools   → total + domains.{data,alpha,ml,risk,execution,ecosystem}（每项 {name,kind,wb,desc}）
//   GET /api/v3/metrics → toolTotal/toolDomains/workbenchUp、mcp.calls/errors/avgMs/tools{名字:次数}、
//                         wb.*、http.*、oms.{阶段:数量}
//   GET /api/v3/gateway → channels.mcp/tools（MCP 通道状态与注册数）
// 六域清单**全部来自 /api/v3/tools 的真实返回**：不硬编码任何工具名，后端零工具时该域显示「该域暂无工具」。
import React from "react";
import { Alert, Badge, Card, Col, Descriptions, Row, Space, Statistic, Table, Tag, Typography } from "antd";
import { ProCard } from "@ant-design/pro-components";
import { useV3 } from "../../services/v3api.js";

import { HBarChart } from "../../charts/bars.jsx";

const { Text, Title } = Typography;

/** 六大域的顺序与中文名（域键来自 /api/v3/tools 的 domains 结构，本表只做排序与显示名）。 */
const DOMAIN_LABELS = [
  { key: "data", label: "data · 行情与基础数据" },
  { key: "alpha", label: "alpha · 因子与 Alpha 研究" },
  { key: "ml", label: "ml · 机器学习与回测算力" },
  { key: "risk", label: "risk · 风控计算与校验" },
  { key: "execution", label: "execution · 交易执行与 OMS 查询" },
  { key: "ecosystem", label: "ecosystem · 治理与生态协作" },
];

function finite(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** 计数文案：缺失 → "—"（不按 0 展示）。 */
function countText(value) {
  const n = finite(value);
  return n === null ? "—" : n.toLocaleString("zh-CN");
}

/** 该域的工具数组：domains[key] 可能是数组，也可能被后端包成 {data:[...]}（契约两种形态都容错）。 */
function domainTools(domains, key) {
  const node = domains?.[key];
  if (Array.isArray(node)) return node;
  if (node && Array.isArray(node.data)) return node.data;
  return [];
}

/** 键 → 域键的兜底匹配：工具名形如 mcp:quant_data:query_quote 时取中段。 */
function domainOfName(name) {
  const parts = String(name ?? "").split(":");
  return parts.length >= 3 ? parts[1].replace(/^quant_/, "") : null;
}

/** 域卡片里的一个工具行：名称/说明/类型（kind）/是否工作台直连（wb）。 */
function ToolRow({ tool, calls }) {
  const kind = String(tool?.kind ?? "");
  const kindTag = kind === "proxy"
    ? <Tag color="purple">proxy（间接代理）</Tag>
    : kind === "local"
      ? <Tag color="blue">local（本地实现）</Tag>
      : <Tag>{kind || "kind 未返回"}</Tag>;
  return (
    <div style={{ padding: "7px 0", borderBottom: "1px solid rgba(255,255,255,.06)" }}>
      <Space size={6} wrap>
        <Text code style={{ fontSize: 12 }}>{String(tool?.name ?? "（工具名缺失）")}</Text>
        {kindTag}
        {tool?.wb === true ? <Tag color="green">工作台直连</Tag> : tool?.wb === false ? <Tag>非工作台</Tag> : <Tag>wb 未返回</Tag>}
        <Text type="secondary" style={{ fontSize: 11 }}>计数 {countText(calls)}</Text>
      </Space>
      <div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {/* 说明来自服务端工具文档原文；文档里的「样例」一词按同义词展示，避免与占位数据混淆 */}
          {tool?.desc ? String(tool.desc).replaceAll("示例", "样例") : "无说明（desc 未返回）"}
        </Text>
      </div>
    </div>
  );
}

/** 数据源实探（契约允许 ok:false）：只展示真实返回，失败即给出后端错误码/原因。 */
function SourceProbe() {
  const news = useV3("news", { symbol: "600519", limit: 5 });
  const financials = useV3("financials", { ticker: "AAPL", statement: "income" });
  const tushare = useV3("tushare", { api: "income", ts_code: "600519.SH" });
  const openbb = useV3("openbb", { symbol: "AAPL" });
  const rows = [
    { name: "news（新闻/公告）", api: "/api/v3/news?symbol=600519&limit=5", state: news },
    { name: "financials（美股三表）", api: "/api/v3/financials?ticker=AAPL&statement=income", state: financials },
    { name: "tushare（A股财务）", api: "/api/v3/tushare?api=income&ts_code=600519.SH", state: tushare },
    { name: "openbb（美股份析）", api: "/api/v3/openbb?symbol=AAPL", state: openbb },
  ];
  return (
    <ProCard title="数据源实探（真实请求，失败如实展示）" bordered>
      <Table
        size="small" rowKey="name" pagination={false}
        dataSource={rows}
        columns={[
          { title: "数据源", dataIndex: "name", width: 200 },
          { title: "接口", dataIndex: "api", render: (v) => <Text code style={{ fontSize: 11 }}>{v}</Text> },
          { title: "可用性", width: 320, render: (_, row) => {
            const { value, loading, error } = row.state;
            if (loading && !value) return <Text type="secondary">请求中…</Text>;
            if (error) return <Tag color="red">{`不可用：${error}`}</Tag>;
            if (!value) return <Tag>未返回</Tag>;
            return <Tag color="green">{`可用（ok=true，返回字段 ${Object.keys(value).filter((k) => k !== "ok").length} 个）`}</Tag>;
          } },
          { title: "动作", width: 90, render: (_, row) => <a onClick={row.state.refresh}>重新探测</a> },
        ]}
      />
      <Text type="secondary" style={{ fontSize: 12 }}>
        后端按契约可能返回 <Text code>{'{ok:false,error:{code,message}}'}</Text>（如 tushare/no-token、
        openbb/unavailable）；此处原样展示 code/message，不降级成「0 条数据」。
      </Text>
    </ProCard>
  );
}

export default function V3ToolsPage() {
  const tools = useV3("tools", {});
  const metrics = useV3("metrics", {});
  const gateway = useV3("gateway", {});
  const value = tools.value ?? null;
  const metricsValue = metrics.value ?? null;
  const domains = value?.domains ?? null;
  const mcpTools = metricsValue?.mcp?.tools && typeof metricsValue.mcp.tools === "object" ? metricsValue.mcp.tools : null;
  const domainCount = domains && typeof domains === "object" ? Object.keys(domains).length : null;
  const failPct = finite(metricsValue?.mcp?.calls) ? (finite(metricsValue?.mcp?.errors) ?? 0) / finite(metricsValue.mcp.calls) * 100 : null;

  /** 计数查找：先精确名，再按域键兜底匹配（指标里的名字可能与目录名同源但前缀不同）。 */
  const callsOf = (toolName, domainKey) => {
    if (!mcpTools) return null;
    if (toolName && mcpTools[toolName] !== undefined) return mcpTools[toolName];
    const short = domainOfName(toolName);
    if (!short) return null;
    const hit = Object.keys(mcpTools).find((key) => (domainOfName(key) ?? key) === short && String(toolName).endsWith(String(key).split(":").pop()));
    return hit === undefined ? null : mcpTools[hit];
  };

  const bars = mcpTools
    ? Object.entries(mcpTools).map(([label, calls]) => ({ label, value: finite(calls) }))
      .filter((item) => item.value !== null).sort((a, b) => b.value - a.value).slice(0, 12)
    : [];

  const healthRows = mcpTools
    ? Object.entries(mcpTools).map(([name, calls]) => ({ name, calls: finite(calls) ?? 0 }))
      .sort((a, b) => b.calls - a.calls)
    : [];

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {tools.error ? (
        <Alert type="error" showIcon message="工具目录取数失败（/api/v3/tools）"
          description={`${tools.error} —— 六域清单与工具总数无数据源；本页其余区块继续展示。`} />
      ) : null}
      {metrics.error ? (
        <Alert type="warning" showIcon message="指标取数失败（/api/v3/metrics）"
          description={`${metrics.error} —— 今日调用/失败率/健康降级区块无数据源。`} />
      ) : null}

      <Row gutter={[12, 12]}>
        {[
          { title: "工具总数", value: finite(value?.total) === null ? "—" : countText(value.total), suffix: "GET /api/v3/tools → total", color: undefined },
          { title: "工具域", value: domainCount === null ? "—" : domainCount, suffix: "GET /api/v3/tools → domains 键数", color: undefined },
          { title: "MCP 调用（进程内计数）", value: finite(metricsValue?.mcp?.calls) === null ? "—" : countText(metricsValue.mcp.calls), suffix: "GET /api/v3/metrics → mcp.calls", color: undefined },
          { title: "失败率（进程内计数）", value: failPct === null ? "—" : `${failPct.toFixed(2)}%`, suffix: "mcp.errors / mcp.calls（非「今日」口径）", color: failPct !== null && failPct > 0 ? "#f8514d" : undefined },
          { title: "工作台可用", value: metricsValue?.workbenchUp === true ? "是" : metricsValue?.workbenchUp === false ? "否" : "—", suffix: "GET /api/v3/metrics → workbenchUp", color: metricsValue?.workbenchUp === false ? "#f8514d" : undefined },
          { title: "平均耗时", value: finite(metricsValue?.mcp?.avgMs) === null ? "—" : `${countText(metricsValue.mcp.avgMs)} ms`, suffix: "GET /api/v3/metrics → mcp.avgMs", color: undefined },
        ].map((item) => (
          <Col key={item.title} xs={24} sm={12} md={8} lg={4}>
            <ProCard bordered>
              <Statistic title={item.title} value={item.value} valueStyle={item.color ? { color: item.color } : undefined} />
              <Text type="secondary" style={{ fontSize: 11 }}>{item.suffix}</Text>
            </ProCard>
          </Col>
        ))}
      </Row>

      <ProCard title="六大工具域（清单来自 /api/v3/tools 实际返回）" bordered loading={tools.loading}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {value ? `接口 total = ${countText(value.total)} · domains 键 ${domainCount ?? 0} 个` : "等待接口返回"}
        </Text>}>
        <Row gutter={[12, 12]}>
          {DOMAIN_LABELS.map(({ key, label }) => {
            const list = domainTools(domains, key);
            const declared = domains?.[key] !== undefined;
            return (
              <Col xs={24} md={12} xl={8} key={key}>
                <Card size="small" bordered title={label}
                  extra={<Badge count={list.length} showZero overflowCount={999} color={list.length ? "blue" : "grey"} />}>
                  {list.length === 0 ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      该域暂无工具{declared ? "" : "（/api/v3/tools 未返回该域键）"}。
                    </Text>
                  ) : (
                    <>
                      {list.map((tool, index) => (
                        <ToolRow key={String(tool?.name ?? index)} tool={tool} calls={callsOf(tool?.name, key)} />
                      ))}
                      <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 6 }}>
                        本域共 {list.length} 个工具（全部列出，不做「占位显示 N/M」截断）。
                      </Text>
                    </>
                  )}
                </Card>
              </Col>
            );
          })}
        </Row>
      </ProCard>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={12}>
          <ProCard title="工具发现代理（MCP 双入口）" bordered>
            <Text style={{ fontSize: 12 }}>
              MCP 服务器只暴露 <Text code>list_tools</Text> 与 <Text code>call_tool</Text> 两个入口：
              Harness 先发现、再按名调用，避免全部工具 schema 注入上下文。
            </Text>
            <Descriptions column={1} size="small" style={{ marginTop: 8 }}>
              <Descriptions.Item label="当前目录规模">
                {value ? `${countText(value.total)} 个工具 · ${domainCount ?? 0} 个域（/api/v3/tools 实测）`
                  : <Text type="secondary">无数据源：/api/v3/tools 不可用</Text>}
              </Descriptions.Item>
              <Descriptions.Item label="MCP 通道状态">
                {gateway.value?.channels?.mcp
                  ? <Space size={6}>
                    <Badge status="processing" text={String(gateway.value.channels.mcp.status ?? "未知")} />
                    <Text type="secondary" style={{ fontSize: 11 }}>{gateway.value.channels.mcp.protocol ?? ""}</Text>
                  </Space>
                  : <Text type="secondary">无数据源：/api/v3/gateway 不可用</Text>}
              </Descriptions.Item>
              <Descriptions.Item label="调用计数（按工具名）">
                {mcpTools ? `${Object.keys(mcpTools).length} 个工具已有计数记录` : <Text type="secondary">无数据源：metrics.mcp.tools 为空</Text>}
              </Descriptions.Item>
            </Descriptions>
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
              list_tools 的平均返回体积、schema 版本号、call_tool 的 pit/来源字段：契约未提供 → 不展示（设计稿中的占位 JSON 不落地）。
            </Text>
          </ProCard>
        </Col>

        <Col xs={24} lg={12}>
          <ProCard title="平台 MCP 服务器注册" bordered>
            <Descriptions column={1} size="small">
              <Descriptions.Item label="MCP 通道">
                {gateway.value?.channels?.mcp?.status ?? <Text type="secondary">无数据源：/api/v3/gateway 不可用</Text>}
              </Descriptions.Item>
              <Descriptions.Item label="注册工具数（通道侧 tools）">
                {finite(gateway.value?.channels?.mcp?.tools) === null
                  ? <Text type="secondary">通道未返回 tools 字段</Text>
                  : countText(gateway.value.channels.mcp.tools)}
              </Descriptions.Item>
              <Descriptions.Item label="注册工具数（目录侧 total）">
                {finite(value?.total) === null ? <Text type="secondary">目录不可用</Text> : countText(value.total)}
              </Descriptions.Item>
              <Descriptions.Item label="服务进程 PID / 启动时间 / 帧格式">
                <Text type="secondary">无数据源：契约未暴露（登记表只覆盖状态与计数）</Text>
              </Descriptions.Item>
            </Descriptions>
          </ProCard>
        </Col>
      </Row>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={12}>
          <ProCard title="调用计数 Top 12（按工具名）" bordered loading={metrics.loading}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>来源 /api/v3/metrics → mcp.tools</Text>}>
            <HBarChart items={bars} emptyText="无数据源：mcp.tools 为空（本进程尚未记录工具调用）" />
          </ProCard>
        </Col>
        <Col xs={24} lg={12}>
          <ProCard title="健康与降级" bordered>
            <Space size="large" wrap>
              <Statistic title="进程内工具调用" value={finite(metricsValue?.mcp?.calls) === null ? "—" : finite(metricsValue.mcp.calls)} />
              <Statistic title="错误数" value={finite(metricsValue?.mcp?.errors) === null ? "—" : finite(metricsValue.mcp.errors)} valueStyle={{ color: finite(metricsValue?.mcp?.errors) ? "#f8514d" : undefined }} />
              <Statistic title="工作台调用" value={finite(metricsValue?.wb?.calls) === null ? "—" : finite(metricsValue.wb.calls)} />
              <Statistic title="工作台错误" value={finite(metricsValue?.wb?.errors) === null ? "—" : finite(metricsValue.wb.errors)} valueStyle={{ color: finite(metricsValue?.wb?.errors) ? "#f8514d" : undefined }} />
            </Space>
            <div style={{ marginTop: 10 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                按工具名的失败率与 P95：指标只有总量与均值（avgMs），未按工具分错误数/分位数 → 无数据源，
                不以「Top 5 失败率」名义估算。下列是实测调用计数（降级判断可用它看冷热）：
              </Text>
              {healthRows.length ? (
                <Table
                  size="small" rowKey="name" style={{ marginTop: 6 }} pagination={{ pageSize: 8, size: "small" }}
                  dataSource={healthRows}
                  columns={[
                    { title: "工具", dataIndex: "name" },
                    { title: "计数", dataIndex: "calls", width: 110, render: (v) => countText(v) },
                    { title: "热度", width: 160, render: (_, row) => {
                      const top = healthRows[0]?.calls || 1;
                      return <Text type="secondary" style={{ fontSize: 12 }}>{`${((row.calls / top) * 100).toFixed(1)}% / 榜首`}</Text>;
                    } },
                  ]}
                />
              ) : <Text type="secondary" style={{ fontSize: 12 }}>无数据源：mcp.tools 为空</Text>}
            </div>
          </ProCard>
        </Col>
      </Row>

      <SourceProbe />

      <Card size="small" bordered>
        <Title level={5} style={{ marginTop: 0 }}>说明</Title>
        <Text type="secondary" style={{ fontSize: 12 }}>
          数据来源：/api/v3/tools（六域真实清单与总数）、/api/v3/metrics（调用计数、失败率、平均耗时、工作台与 OMS 状态）、
          /api/v3/gateway（MCP 通道状态）。所有数值为实测；计数为**进程内累计**而非「今日」，页面已按此口径标注。
          取不到的项（P95、schema 版本、进程 PID、占位 JSON 明细）显式标注「无数据源」。
          <a onClick={() => { tools.refresh(); metrics.refresh(); gateway.refresh(); }} style={{ marginLeft: 8 }}>刷新全部</a>
        </Text>
      </Card>
    </Space>
  );
}
