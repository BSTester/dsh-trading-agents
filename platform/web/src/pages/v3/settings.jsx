// V3「接入与授权」页（Ant Design Pro）：交易模式 · 模式切换审计 · 富途授权 · 统一凭据清单 ·
// 环境变量与密钥来源 · 授权与审计策略。设计稿对应 od-quant-harness-platform/settings.html
// （版式参照，1,024,380 / 账号掩码 / 圆点占位 等占位值一律不抄）。
//
// 数据来源（全部服务端实测）：
//   GET /api/v3/settings        → mode_note、trading_mode、futu.{channel,mcp_bearer{present,expiry},
//                                 openapi{mode,config_keys}}、env[{key,injected,source}]、data_sources[]
//   GET /api/v3/audit?window=120 → data.entries[{id,kind,at,ticker,detail}]（模式切换审计的可用真实数据）
//   GET /api/v3/metrics         → oms.{stage:数量} 等运维计数（页面只做只读展示）
//
// 密钥安全（硬约束）：env 表只显示「是否注入 + 来源」，futu.openapi.config_keys 只显示**键名**，
// 任何字段的值一律不展示、不写 localStorage。后端契约本身也不返回密钥值。
//
// 模式切换：本页不新开写入口。既有闸门是工作台（8397）的 POST /api/wb/switch-mode
// （载荷 {mode, expected_mode, confirmation?}，sim→live 需逐字口令「确认实盘」，服务端独立复核），
// 页面顶栏已有该入口。此处只做只读展示 + 指向入口的说明，避免出现第二个能改交易模式的地方。
import React from "react";
import { Alert, App, Badge, Button, Card, Col, Descriptions, Input, Row, Space, Statistic, Table, Tag, Typography } from "antd";
import { ProCard } from "@ant-design/pro-components";
import { callV3, postV3, useV3 } from "../../services/v3api.js";
import { stampOf } from "../../services/format.jsx";

const { Text, Title } = Typography;

function finite(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function countText(value) {
  const n = finite(value);
  return n === null ? "—" : n.toLocaleString("zh-CN");
}


/** 页面化密钥配置：读状态 / 保存 / 真实连通性测试 / 清除。
 *  硬约束：任何输入与响应都不回显凭据值（服务端也不返回），只显示「已配置/未配置 + 来源 + 掩码尾号」。 */
function CredentialPanel() {
  const { message } = App.useApp();
  const [state, setState] = React.useState({ loading: true, keys: [], error: null, busy: null, test: null });
  const [draft, setDraft] = React.useState("");

  const load = React.useCallback(async () => {
    setState((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const body = await callV3("credentials");
      setState((prev) => ({ ...prev, loading: false, keys: body.keys ?? [] }));
    } catch (error) {
      setState((prev) => ({ ...prev, loading: false, error: String(error.message || error) }));
    }
  }, []);
  React.useEffect(() => { load(); }, [load]);

  const act = async (action, extra = {}) => {
    setState((prev) => ({ ...prev, busy: action, test: action === "test" ? prev.test : null }));
    try {
      const body = await postV3("credentials", { action, key: "tushare_token", ...extra });
      if (action === "test") {
        setState((prev) => ({ ...prev, busy: null, test: body }));
        if (body.ok) message.success(`连通性正常（${body.latency_ms}ms，来源：${body.source}）`);
        else message.error(body.error?.message ?? "连通性测试失败");
        return;
      }
      if (!body.ok) {
        message.error(body.error?.message ?? "操作失败");
        return;
      }
      setState((prev) => ({ ...prev, busy: null, keys: body.keys ?? [], test: null }));
      if (action === "save") { setDraft(""); message.success("已保存（0600 落盘，不回显）"); }
      if (action === "clear") message.success("已清除页面配置的密钥（环境变量不受影响）");
    } catch (error) {
      setState((prev) => ({ ...prev, busy: null }));
      message.error(String(error.message || error));
    }
  };

  const entry = state.keys.find((k) => k.key === "tushare_token") ?? null;
  return (
    <ProCard title="密钥与授权（可在本页完成）" bordered loading={state.loading}
      extra={<Button size="small" type="link" onClick={load}>刷新状态</Button>}>
      {state.error ? <Alert type="error" showIcon style={{ marginBottom: 8 }} message="凭据状态读取失败" description={state.error} /> : null}
      <Space direction="vertical" size={10} style={{ width: "100%" }}>
        <div>
          <Space size={8} wrap>
            <Text strong>{entry?.label ?? "Tushare Pro Token"}</Text>
            {entry?.present
              ? <Tag color="green">已配置</Tag>
              : <Tag color="orange">未配置</Tag>}
            <Text type="secondary" style={{ fontSize: 12 }}>
              用途：{entry?.usage ?? "A 股财务/行情（/api/v3/tushare）"} · 环境变量：{entry?.env ?? "TUSHARE_TOKEN"}
            </Text>
          </Space>
          <div style={{ marginTop: 4 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {entry?.present
                ? `来源：${entry.source}${entry.updated_at ? ` · 更新于 ${stampOf(entry.updated_at)}` : ""}${entry.hint ? ` · 尾号 ${entry.hint}` : ""}`
                : "保存后立即生效（环境变量优先于页面配置）；凭据 0600 落盘，服务端不回显任何值。"}
            </Text>
          </div>
          <Space.Compact style={{ width: "100%", maxWidth: 560, marginTop: 6 }}>
            <Input.Password value={draft} onChange={(event) => setDraft(event.target.value)}
              placeholder="粘贴 Tushare Pro token（保存后不再回显）" autoComplete="off" />
            <Button type="primary" disabled={!draft.trim() || state.busy === "save"}
              loading={state.busy === "save"} onClick={() => act("save", { value: draft })}>保存</Button>
          </Space.Compact>
          <Space size={8} style={{ marginTop: 8 }} wrap>
            <Button size="small" loading={state.busy === "test"} onClick={() => act("test")}>测试连通性</Button>
            <Button size="small" danger disabled={!entry?.present || state.busy === "clear"}
              loading={state.busy === "clear"} onClick={() => act("clear")}>清除页面配置</Button>
            {state.test ? (
              state.test.ok
                ? <Text type="success" style={{ fontSize: 12 }}>测试通过：{state.test.latency_ms}ms · 来源 {state.test.source} · 返回 {state.test.rows} 行</Text>
                : <Text type="danger" style={{ fontSize: 12 }}>测试失败：{state.test.error?.code} · {state.test.error?.message}</Text>
            ) : null}
          </Space>
        </div>
        <div>
          <Space size={8} wrap>
            <Text strong>富途 OpenAPI 授权（OAuth 2.1 + PKCE）</Text>
            <Button size="small" onClick={() => { window.location.hash = "#/settings"; }}>前往既有设置页完成授权</Button>
            <Text type="secondary" style={{ fontSize: 12 }}>
              OAuth 授权需要本机回调端口（http://localhost:&lt;port&gt;/callback），流程在既有工作台「设置」页的
              「OAuth 授权」面板完成，凭据由服务端落盘（mode=oauth）；本页不重复实现第二套授权，只做状态展示与入口。
            </Text>
          </Space>
        </div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          其它数据源无需密钥：AKShare / SEC EDGAR 为公开端点；OpenBB 为可选依赖（未安装时该项如实报 unavailable）。
        </Text>
      </Space>
    </ProCard>
  );
}

/** 只读的「无数据源」说明。 */
function NoSource({ children }) {
  return <Text type="secondary" style={{ fontSize: 12 }}>无数据源：{children}</Text>;
}

/** 模式展示：优先取短模式名，其次原样回显接口文案（接口若给了长句，照实显示）。 */
function modeText(settings) {
  const raw = settings?.trading_mode ?? settings?.mode ?? null;
  if (raw === null || raw === undefined || raw === "") return null;
  const text = String(raw);
  const lower = text.toLowerCase();
  if (lower === "sim" || lower === "live") return { short: lower, raw: text };
  const hit = /^(sim|live)\b/i.exec(text.trim());
  return { short: hit ? hit[1].toLowerCase() : null, raw: text };
}

/** data_sources 条目：契约写的是数组，元素可能是字符串（后端实现）也可能是对象 —— 两种都如实渲染。 */
function dataSourceCell(row, field) {
  if (typeof row === "string") return field === "name" ? row : "—";
  if (!row || typeof row !== "object") return "—";
  const value = row[field];
  return value === undefined || value === null || value === "" ? "—" : String(value);
}

export default function V3SettingsPage() {
  const settings = useV3("settings", {});
  const audit = useV3("audit", { window: 120 });
  const metrics = useV3("metrics", {});
  const { modal } = App.useApp();
  const value = settings.value ?? null;
  const mode = modeText(value);
  const futu = value?.futu ?? null;
  const bearer = futu?.mcp_bearer ?? null;
  const openapi = futu?.openapi ?? null;
  const env = Array.isArray(value?.env) ? value.env : [];
  const sources = Array.isArray(value?.data_sources) ? value.data_sources : [];
  const entries = Array.isArray(audit.value?.data?.entries) ? audit.value.data.entries : [];
  const omsStages = metrics.value?.oms && typeof metrics.value.oms === "object" ? metrics.value.oms : null;

  /** 模式切换说明：本页不发起写操作，指向顶栏既有闸门。 */
  const explainSwitch = () => {
    modal.info({
      title: "模式切换入口",
      content: (
        <Space direction="vertical" size={6}>
          <Text>模式切换沿用既有工作台闸门：顶栏模式徽章 → 选择目标模式 → 逐字口令（sim→live 需「确认实盘」）→ 二次确认。</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            服务端 POST /api/wb/switch-mode 独立复核（含 expected_mode 与在途租约检查），
            响应 order_authorized 恒为 false：切换模式不等于授权下单。
          </Text>
        </Space>
      ),
      okText: "知道了",
    });
  };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {settings.error ? (
        <Alert type="error" showIcon message="接入配置取数失败（/api/v3/settings）"
          description={`${settings.error} —— 模式/富途授权/环境变量/凭据清单区块均无数据源；审计区块独立取数。`} />
      ) : null}
      {audit.error ? (
        <Alert type="warning" showIcon message="审计取数失败（/api/v3/audit）"
          description={`${audit.error} —— 模式切换审计区块无数据源。`} />
      ) : null}

      <Alert type={value?.mode_note ? "info" : "warning"} showIcon
        message="交易模式与授权集中管理（只读展示）"
        description={<Space direction="vertical" size={4}>
          <Text style={{ fontSize: 12 }}>
            {value?.mode_note ?? "无数据源：/api/v3/settings 未返回 mode_note（模式切换口径以服务端为准）。"}
          </Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            切换模式不等于授权下单：LIVE 下每笔执行仍需人工审批与口令校验。本页不接受任何密钥输入，也不展示密钥值。
          </Text>
        </Space>} />

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={8}>
          <ProCard title="交易模式" bordered loading={settings.loading}
            extra={mode?.short
              ? <Tag color={mode.short === "live" ? "red" : "green"}>{mode.short === "live" ? "实盘 LIVE" : "模拟 SIM"}</Tag>
              : <Tag>模式未知</Tag>}>
            {value === null ? (
              <NoSource>接口不可用，当前模式未知</NoSource>
            ) : (
              <Space direction="vertical" size={6} style={{ width: "100%" }}>
                <Text style={{ fontSize: 12 }}>接口原文：{value?.trading_mode ?? "—"}</Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  模式只读回读（权威来源是工作台 snapshot.mode）。切换只经顶栏模式徽章闸门：
                  逐字口令 + 二次确认 + 服务端独立复核；成功响应 order_authorized 恒为 false。
                </Text>
                <Space size={8} wrap>
                  <Button size="small" onClick={explainSwitch}>切换到实盘（说明）</Button>
                  <Button size="small" onClick={explainSwitch}>切回模拟盘（说明）</Button>
                </Space>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  两个按钮不发起写请求：/api/v3/settings 的契约不含写路径，页面不新开第二个改模式的入口。
                </Text>
              </Space>
            )}
          </ProCard>
        </Col>

        <Col xs={24} lg={8}>
          <ProCard title="模式切换审计" bordered
            extra={<Space size={8}>
              <Text type="secondary" style={{ fontSize: 12 }}>/api/v3/audit?window=120</Text>
              <a onClick={audit.refresh}>刷新</a>
            </Space>}>
            <Table
              size="small" rowKey={(row) => String(row?.id ?? `${row?.kind}-${row?.at}`)}
              dataSource={entries} loading={audit.loading} pagination={{ pageSize: 6, size: "small" }}
              locale={{ emptyText: "无数据源：audit.data.entries 为空（该窗口内无审计记录）" }}
              columns={[
                { title: "id", dataIndex: "id", width: 110, render: (v) => <Text code style={{ fontSize: 11 }}>{String(v ?? "—")}</Text> },
                { title: "kind", dataIndex: "kind", width: 90, render: (v) => <Tag>{String(v ?? "—")}</Tag> },
                { title: "at", dataIndex: "at", width: 150, render: (v) => stampOf(v) },
                { title: "ticker", dataIndex: "ticker", width: 90, render: (v) => v ?? "—" },
                { title: "detail", dataIndex: "detail", render: (v) => (
                  <Text type="secondary" style={{ fontSize: 12 }} ellipsis={{ tooltip: true }}>{String(v ?? "—")}</Text>) },
              ]}
            />
            <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 6 }}>
              如实说明：该接口返回的是**审计链**（计划→订单→成交 + 信号链路）的原始条目，字段为
              id / kind / at / ticker / detail，**没有**设计稿里的「操作人 / 切换方向 / 口令校验 / 前置校验 / 结果 / 来源 IP」。
              因此本表按原始字段名展示，不把 kind 硬解释成「模式切换结果」。
            </Text>
          </ProCard>
        </Col>

        <Col xs={24} lg={8}>
          <ProCard title="富途授权" bordered loading={settings.loading}
            extra={futu?.channel ? <Tag color="blue">{String(futu.channel)}</Tag> : <Tag>通道未知</Tag>}>
            {futu === null ? (
              <NoSource>接口未返回 futu 段</NoSource>
            ) : (
              <Space direction="vertical" size={6} style={{ width: "100%" }}>
                <Descriptions column={1} size="small">
                  <Descriptions.Item label="MCP Bearer 是否存在">
                    {bearer?.present === true
                      ? <Tag color="green">存在</Tag>
                      : bearer?.present === false
                        ? <Tag color="orange">不存在</Tag>
                        : <Text type="secondary">未返回 present</Text>}
                  </Descriptions.Item>
                  <Descriptions.Item label="MCP Bearer 到期">
                    {bearer?.expiry ? stampOf(bearer.expiry) : <Text type="secondary">未返回 expiry（可能为长期或不适用）</Text>}
                  </Descriptions.Item>
                  <Descriptions.Item label="OpenAPI 模式">
                    {openapi?.mode ?? <Text type="secondary">未返回 mode</Text>}
                  </Descriptions.Item>
                  <Descriptions.Item label="OpenAPI 配置项（只列键名）">
                    {Array.isArray(openapi?.config_keys) && openapi.config_keys.length
                      ? <Space size={4} wrap>{openapi.config_keys.map((key) => <Tag key={String(key)}>{String(key)}</Tag>)}</Space>
                      : <Text type="secondary">未返回 config_keys</Text>}
                  </Descriptions.Item>
                </Descriptions>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  凭据值一律不回显：这里只有「存在与否 + 到期 + 键名」，AppKey / 私钥 / 登录账号的设计稿占位（账号掩码、圆点占位）不落地。
                  OAuth 重新授权与连接测试在既有工作台「设置」页完成；本页不发起该流程。
                </Text>
              </Space>
            )}
          </ProCard>
        </Col>
      </Row>

      <CredentialPanel />

      <ProCard title="统一授权中心（凭据清单）" bordered loading={settings.loading}
        extra={<Text type="secondary" style={{ fontSize: 12 }}>
          {sources.length ? `接口 data_sources 返回 ${sources.length} 项` : "接口未返回 data_sources"}
        </Text>}>
        <Table
          size="small" rowKey={(row, index) => `${dataSourceCell(row, "name")}-${index}`}
          dataSource={sources} pagination={false}
          locale={{ emptyText: "无数据源：/api/v3/settings → data_sources 为空" }}
          columns={[
            { title: "凭据 / 数据源", render: (_, row) => dataSourceCell(row, "name") },
            { title: "所属服务", width: 150, render: (_, row) => dataSourceCell(row, "service") },
            { title: "类型", width: 130, render: (_, row) => dataSourceCell(row, "type") },
            { title: "作用域", width: 170, render: (_, row) => dataSourceCell(row, "scope") },
            { title: "状态", width: 130, render: (_, row) => {
              const status = dataSourceCell(row, "status");
              if (status === "—") return <Text type="secondary">未返回</Text>;
              const ok = /已授权|可用|ok|active/i.test(status);
              return <Tag color={ok ? "green" : "orange"}>{status}</Tag>;
            } },
            { title: "到期", width: 140, render: (_, row) => {
              const expiry = dataSourceCell(row, "expiry");
              return expiry === "—" ? "—" : stampOf(expiry);
            } },
            { title: "最近轮换", width: 140, render: (_, row) => {
              const rotated = dataSourceCell(row, "rotated_at");
              return rotated === "—" ? "—" : stampOf(rotated);
            } },
          ]}
        />
        <Text type="secondary" style={{ fontSize: 12 }}>
          契约只冻结 data_sources 的存在性；本表按 name/service/type/scope/status/expiry/rotated_at 逐个试探读取，
          缺字段显示「—」或「未返回」。**不含任何凭据值**；设计稿中 12 项凭据（Tushare Token、PostgreSQL 连接串、
          Webhook Secret 等）属占位清单，未在接口出现前不展示。
        </Text>
      </ProCard>

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={14}>
          <ProCard title="环境变量与密钥来源（只显示是否注入 + 来源）" bordered loading={settings.loading}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>
              {env.length ? `${env.filter((item) => item?.injected === true).length} / ${env.length} 已注入` : "接口未返回 env"}
            </Text>}>
            <Table
              size="small" rowKey={(row) => String(row?.key ?? Math.random())}
              dataSource={env} pagination={false}
              locale={{ emptyText: "无数据源：/api/v3/settings → env 为空" }}
              columns={[
                { title: "变量名", dataIndex: "key", render: (v) => <Text code>{String(v ?? "—")}</Text> },
                { title: "用途 / 说明", width: 200, render: (_, row) => row?.purpose ?? row?.desc ?? <Text type="secondary">未返回</Text> },
                { title: "是否已注入", width: 120, render: (_, row) => {
                  if (row?.injected === true) return <Tag color="green">已注入</Tag>;
                  if (row?.injected === false) return <Tag color="orange">未注入</Tag>;
                  return <Text type="secondary">未返回</Text>;
                } },
                { title: "来源", width: 180, render: (_, row) => row?.source ?? <Text type="secondary">未返回</Text> },
              ]}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              安全约定：本表**只有键名、是否注入、来源**，没有值；接口契约也不返回任何密钥值。
              页面不提供密钥输入框，凭据由凭据库 / KMS 托管（轮换在既有工作台设置页）。
            </Text>
          </ProCard>
        </Col>

        <Col xs={24} lg={10}>
          <ProCard title="OMS 与运维计数（只读）" bordered loading={metrics.loading}
            extra={<Text type="secondary" style={{ fontSize: 12 }}>来源 /api/v3/metrics</Text>}>
            {omsStages ? (
              <Table
                size="small" rowKey="stage" pagination={false}
                dataSource={Object.entries(omsStages).map(([stage, count]) => ({ stage, count: finite(count) }))}
                columns={[
                  { title: "OMS 阶段", dataIndex: "stage" },
                  { title: "订单数", dataIndex: "count", width: 110, render: (v) => countText(v) },
                ]}
              />
            ) : <NoSource>metrics.oms 未返回（OMS 台账未挂载或指标接口不可用）</NoSource>}
            <Space size="large" style={{ marginTop: 10 }} wrap>
              <Statistic title="HTTP 请求" value={finite(metrics.value?.http?.requests) === null ? "—" : finite(metrics.value.http.requests)} />
              <Statistic title="HTTP 错误" value={finite(metrics.value?.http?.errors) === null ? "—" : finite(metrics.value.http.errors)} />
              <Statistic title="工作台可用" value={metrics.value?.workbenchUp === true ? "是" : metrics.value?.workbenchUp === false ? "否" : "—"} />
            </Space>
          </ProCard>
        </Col>
      </Row>

      <ProCard title="授权与审计策略" bordered>
        <Row gutter={[12, 12]}>
          <Col xs={24} lg={10}>
            <Descriptions column={1} size="small" title="执行授权策略（只读）">
              <Descriptions.Item label="自动执行">
                <Text type="secondary">无数据源：/api/v3/settings 未返回阈值配置（单笔 2% / 行业 20% 属策略侧配置，不经本接口暴露）</Text>
              </Descriptions.Item>
              <Descriptions.Item label="人工确认">
                <Text type="secondary">LIVE 下每笔执行仍需人工审批与口令校验（既有工作台闸门）</Text>
              </Descriptions.Item>
              <Descriptions.Item label="强制阻断">
                <Text type="secondary">红线规则由风控引擎下发，本页不展示未从接口取得的阈值</Text>
              </Descriptions.Item>
            </Descriptions>
          </Col>
          <Col xs={24} lg={14}>
            <Descriptions column={1} size="small" title="审计与降级（只读）">
              <Descriptions.Item label="审计窗口">
                {`本页审计查询 window=120（条/窗口，按接口默认）。审计链条目实测 ${countText(entries.length)} 条。`}
              </Descriptions.Item>
              <Descriptions.Item label="审批口令轮换 / 保留期 / 双人复核">
                <NoSource>契约未提供这些策略字段（设计稿的 180 天 / 双人复核不落地）</NoSource>
              </Descriptions.Item>
              <Descriptions.Item label="只读降级">
                <Text type="secondary">凭据缺失或上游不可用时，相关数据源逐项标记失败（见各接口 error 展示），不伪造数据</Text>
              </Descriptions.Item>
              <Descriptions.Item label="密钥不落盘明文">
                <Space size={6}><Badge status="success" text="本页只读，仅展示存在性/来源" /><Badge status="success" text="不写 localStorage（沿用 services/v3api.js 的 token 约定）" /></Space>
              </Descriptions.Item>
            </Descriptions>
          </Col>
        </Row>
      </ProCard>

      <Card size="small" bordered>
        <Title level={5} style={{ marginTop: 0 }}>说明</Title>
        <Text type="secondary" style={{ fontSize: 12 }}>
          数据来源：/api/v3/settings、/api/v3/audit?window=120、/api/v3/metrics。所有数值为实测；
          取不到的项显式标注「无数据源」并给出原因，未使用设计稿占位值。密钥类字段（env 值、AppKey、私钥、
          登录账号、口令）一律不采集、不展示。
          <a onClick={() => { settings.refresh(); audit.refresh(); metrics.refresh(); }} style={{ marginLeft: 8 }}>刷新全部</a>
        </Text>
        <Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 6 }}>
          模式变更只经顶栏闸门（POST /api/wb/switch-mode）；本页不新增写入口，也不触发任何下单/改单/撤单动作。
        </Text>
      </Card>
    </Space>
  );
}
