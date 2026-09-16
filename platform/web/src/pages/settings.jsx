// 设置页：富途 OpenAPI 凭据配置（保存 / 连通性测试 / 当前状态 / OAuth 2.1+PKCE 授权）。
// 端点（取值路径均可指到源码行）：
//   openapi_config → platform/server/settings_api.py：POST 空载荷=读状态（与
//     GET /api/wb/openapi_config 专用路由同一实现，app.py）；POST 带载荷=保存
//     （校验 → 写私钥 0600 → 写凭据 JSON → 联动 trading-platform.json 的 futu_channel，
//     先校验后落盘；业务失败 = trading/invalid-operation 信封）。
//   openapi_test → settings_api.test_connectivity：用**已保存**凭据经真实调用路径
//     （OpenApiClient + OpenApiMarket.trading-days）发一次 GET 并计时；失败 =
//     trading/openapi-unavailable 信封（callApi 抛 Error(message)）。
//   openapi_oauth → platform/server/oauth_flow.py：OAuth 2.1+PKCE 授权流程的
//     start（注册 client 如需 → 服务端起 127.0.0.1 回调监听 → 返回授权 URL）/
//     status（本页 2s 轮询 {pending,done,error,tokens_saved}）/ cancel（停监听清状态）。
//     授权完成后凭据由服务端落盘（mode=oauth，0600），本页**不**经 openapi_config
//     保存 OAuth 凭据（服务端也拒绝）——与 AppKey 模式并存，存哪种写哪种 mode。
// 安全约定（tests/test_wp8_settings.py 与 tests/test_wp8_oauth.py 全文 grep 钉死）：
// 私钥 PEM 原文只进不出——服务端任何响应都不含 PEM 与完整 app_key，只有「已配置 +
// 公钥指纹（SHA256 前 16 hex）」；code_verifier 只存服务端内存，不落盘不进日志；
// 本页也绝不把粘贴框内容写进 localStorage，刷新即丢、以服务端落盘为准。
import React from "react";
import {
  Alert, App, Button, Card, Descriptions, Form, Input, Radio, Select, Space, Tabs,
  Tag, Typography,
} from "antd";
import { callApi, clearCache, getToken, setToken } from "../services/api.js";
import { useEndpoint } from "../services/hooks.js";

const { TextArea } = Input;

// 与服务端 settings_api._normalize_algorithm 同一口径的合法算法集合
const ALGORITHMS = ["Ed25519", "RSA-SHA256"];

function ValueOrDash({ children }) {
  return <span>{children ?? "—"}</span>;
}

/** 当前状态卡：只渲染服务端返回的字段，缺的一律 —（不猜、不补）。 */
function StatusCard({ status }) {
  const value = status.value;
  const loading = status.loading && !value;
  if (status.error) {
    return <Alert type="warning" showIcon message={`状态读取失败：${status.error}`} />;
  }
  return (
    <Card size="small" title="当前状态" extra={value?.ready
      ? <Tag color="green">就绪（凭据可加载）</Tag>
      : <Tag color={value?.configured ? "orange" : "default"}>{value?.configured ? "未就绪" : "未配置"}</Tag>}>
      <Descriptions size="small" column={{ xs: 1, sm: 2 }} loading={loading}>
        <Descriptions.Item label="凭据">
          {value?.configured ? <Tag color="green">已配置</Tag> : <Tag>未配置</Tag>}
        </Descriptions.Item>
        <Descriptions.Item label="认证方式">
          <ValueOrDash>{value?.mode}</ValueOrDash>
        </Descriptions.Item>
        <Descriptions.Item label="通道">
          <ValueOrDash>{value?.channel === "openapi" ? "openapi（本页凭据）" : value?.channel === "mcp" ? "mcp（OAuth 令牌）" : value?.channel}</ValueOrDash>
        </Descriptions.Item>
        <Descriptions.Item label="签名算法">
          <ValueOrDash>{value?.algorithm}</ValueOrDash>
        </Descriptions.Item>
        <Descriptions.Item label="AppKey（掩码）">
          <Typography.Text code><ValueOrDash>{value?.app_key_masked}</ValueOrDash></Typography.Text>
        </Descriptions.Item>
        <Descriptions.Item label="私钥">
          {value?.private_key_exists
            ? <Space size={4}><Tag color="green">文件在位</Tag><Typography.Text code>{value.private_key_fingerprint}</Typography.Text></Space>
            : <Tag>未检出</Tag>}
        </Descriptions.Item>
      </Descriptions>
      {value?.last_error && (
        <Alert type="error" showIcon style={{ marginTop: 8 }}
          message={`凭据文件读取失败：${value.last_error}`} />
      )}
      <Typography.Text type="secondary" style={{ fontSize: 12, display: "block", marginTop: 8 }}>
        私钥原文不回显：这里只有公钥指纹（SHA256 前 16 位），用于与富途控制台人工比对是否同一把钥匙。
      </Typography.Text>
    </Card>
  );
}

/** OAuth 2.1+PKCE 授权面板：Client ID（可空=自动注册）→ 开始授权 → 轮询 → 落盘。 */
function OAuthPanel({ form, mode, status }) {
  const { message } = App.useApp();
  // phase ∈ idle | starting | waiting | done | error（error 携带服务端文案）
  const [oauth, setOauth] = React.useState({ phase: "idle" });
  const waiting = oauth.phase === "waiting";

  // 等待授权期间 2s 轮询 status 端点；done/error 即停（切走 OAuth 模式也停）。
  React.useEffect(() => {
    if (!waiting || mode !== "oauth") return undefined;
    const timer = setInterval(async () => {
      try {
        const flow = await callApi("openapi_oauth", { action: "status" });
        if (flow.done && flow.tokens_saved) {
          setOauth({ phase: "done" });
          message.success("OAuth 授权完成，凭据已保存");
          status.refresh();
        } else if (flow.error) {
          setOauth({ phase: "error", error: flow.error });
        }
      } catch { /* 单次轮询失败忽略：下个周期重试 */ }
    }, 2000);
    return () => clearInterval(timer);
  }, [waiting, mode]);

  const startOAuth = async () => {
    const clientId = String(form.getFieldValue("oauth_client_id") ?? "").trim();
    setOauth({ phase: "starting" });
    try {
      const flow = await callApi("openapi_oauth",
        clientId ? { action: "start", client_id: clientId } : { action: "start" });
      setOauth({ phase: "waiting", authUrl: flow.auth_url });
      status.refresh();
    } catch (error) {
      setOauth({ phase: "error", error: String(error.message || error) });
    }
  };

  const cancelOAuth = async () => {
    try {
      await callApi("openapi_oauth", { action: "cancel" });
    } catch { /* 取消失败不打断 UI：服务端 600s 超时兜底 */ }
    setOauth({ phase: "idle" });
  };

  return (
    <>
      <Form.Item name="oauth_client_id" label="Client ID（富途 OpenAPI OAuth 客户端）"
        extra="留空则自动注册 public client（PKCE required）；已注册过可填既有 client_id 复用，注册结果由服务端写入凭据文件。">
        <Input placeholder="留空自动注册，或粘贴已注册的 client_id" autoComplete="off" allowClear />
      </Form.Item>
      <Space wrap>
        <Button type="primary" loading={oauth.phase === "starting"}
          disabled={waiting} onClick={startOAuth}>开始授权</Button>
        {waiting && <Button onClick={cancelOAuth}>取消</Button>}
      </Space>
      <Typography.Text type="secondary" style={{ display: "block", fontSize: 12, marginTop: 8 }}>
        流程：注册 client（如需）→ 服务端在本机启动回调监听（仅 127.0.0.1:60355）→
        你在浏览器完成富途账号授权 → 服务端校验 state 后自动换取 token 并落盘
        ~/.dsh/futu-openapi.json（0600，本页不再回显）。授权链接 10 分钟内有效，超时可重新发起。
      </Typography.Text>
      {waiting && oauth.authUrl && (
        <Alert type="info" showIcon style={{ marginTop: 8 }} message="等待授权中…（完成或被拒后本页自动更新）"
          description={<a href={oauth.authUrl} target="_blank" rel="noreferrer">点击打开富途授权页（若浏览器未自动打开）</a>} />
      )}
      {oauth.phase === "done" && (
        <Alert type="success" showIcon style={{ marginTop: 8 }}
          message="✓ OAuth 授权完成，凭据已保存（mode=oauth）" />
      )}
      {oauth.phase === "error" && (
        <Alert type="error" showIcon style={{ marginTop: 8 }} message="授权未完成"
          description={oauth.error} />
      )}
    </>
  );
}

/** 服务访问令牌：平台 Bearer token（写入 localStorage，服务端可选项）。 */
function ServiceTokenSection() {
  const { message } = App.useApp();
  const [value, setValue] = React.useState(getToken());
  return (
    <Card size="small" title="服务访问令牌">
      <Space direction="vertical" style={{ width: "100%" }} size="small">
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          平台服务的 Bearer token（服务端 trading-platform.json 的 service.token 配置后生效）；未配置则留空。
        </Typography.Text>
        <Input.Password value={value} autoComplete="off" aria-label="服务访问令牌"
          placeholder="留空 = 服务未启用 token 鉴权"
          onChange={(event) => setValue(event.target.value)} />
        <Button size="small" type="primary" disabled={!value.trim()}
          onClick={() => { setToken(value.trim()); clearCache(); message.success("令牌已保存并清除缓存"); }}>
          保存令牌
        </Button>
      </Space>
    </Card>
  );
}

export default function SettingsPage() {
  const status = useEndpoint("openapi_config", {}, []);
  const [form] = Form.useForm();
  const [keyTab, setKeyTab] = React.useState("pem");
  const [mode, setMode] = React.useState("appkey");
  const [channel, setChannel] = React.useState("openapi");
  const [busy, setBusy] = React.useState(false);
  const [testResult, setTestResult] = React.useState(null); // {ok, text}

  const saveAndTest = async () => {
    setTestResult(null);
    let values;
    try {
      values = await form.validateFields();
    } catch {
      return; // 表单校验未过：antd 已在字段旁标红
    }
    const payload = { mode: "appkey", channel };
    payload.app_key = (values.app_key ?? "").trim();
    payload.algorithm = values.algorithm ?? "Ed25519";
    if (keyTab === "pem") {
      if ((values.private_key_pem ?? "").trim()) payload.private_key_pem = values.private_key_pem;
    } else if ((values.private_key_path ?? "").trim()) {
      payload.private_key_path = values.private_key_path.trim();
    }
    setBusy(true);
    try {
      await callApi("openapi_config", payload); // 1) 保存（先校验后落盘）
      let ok = true;
      let text = "保存成功";
      try {
        const test = await callApi("openapi_test", {}); // 2) 连通性（真实 trading-days GET）
        text = `HTTP ${test.http_status} · ret_code ${test.ret_code} · ${test.ret_msg} · ${test.latency_ms}ms`;
      } catch (error) {
        ok = false;
        text = String(error.message || error);
      }
      setTestResult({ ok, text });
      if (ok) message.success("凭据已保存，连通性测试通过");
      else message.warning("凭据已保存，但连通性测试失败");
      status.refresh();
    } catch (error) {
      message.error(`保存失败：${error.message || error}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: "100%" }}>
      <Card size="small" title="富途 OpenAPI 凭据">
        <Form form={form} layout="vertical" requiredMark={false}
          initialValues={{ mode: "appkey", algorithm: "Ed25519" }}>
          <Form.Item label="通道（写入 trading-platform.json 的 futu_channel）">
            <Radio.Group value={channel} onChange={(event) => setChannel(event.target.value)}
              options={[
                { label: "OpenAPI（AppKey/ OAuth，本页凭据）", value: "openapi" },
                { label: "MCP（OAuth 令牌，右上角「令牌」维护）", value: "mcp" },
              ]} />
          </Form.Item>
          <Form.Item label="认证方式">
            <Radio.Group value={mode} onChange={(event) => setMode(event.target.value)}
              options={[
                { label: "AppKey 签名", value: "appkey" },
                { label: "OAuth 2.1 + PKCE（推荐，本页发起授权）", value: "oauth" },
              ]} />
          </Form.Item>
          {mode === "oauth" ? (
            <OAuthPanel form={form} mode={mode} status={status} />
          ) : (
            <>
              <Form.Item name="app_key" label="AppKey ID" rules={[
                { required: true, message: "AppKey ID 必填（富途开放平台控制台创建）" }]}>
                <Input placeholder="如 0be2eb1122334455" autoComplete="off" allowClear />
              </Form.Item>
              <Form.Item name="algorithm" label="签名算法" rules={[
                { required: true, message: "请选择签名算法" }]}>
                <Select options={ALGORITHMS.map((value) => ({ value, label: value }))} />
              </Form.Item>
              <Form.Item label="私钥（与控制台创建 AppKey 时登记的公钥成对；二选一）">
                <Tabs activeKey={keyTab} onChange={setKeyTab} items={[
                  {
                    key: "pem", label: "粘贴 PEM", forceRender: true,
                    children: (
                      <Form.Item name="private_key_pem" noStyle>
                        <TextArea rows={7} spellCheck={false}
                          placeholder={"-----BEGIN PRIVATE KEY-----\n…（PEM 原文，保存后服务端落盘 0600，本页不再回显）\n-----END PRIVATE KEY-----"}
                          aria-label="私钥 PEM 原文" />
                      </Form.Item>
                    ),
                  },
                  {
                    key: "path", label: "文件路径", forceRender: true,
                    children: (
                      <Form.Item name="private_key_path" noStyle>
                        <Input placeholder="已放好的私钥文件路径，如 ~/.dsh/futu-openapi-key.pem"
                          autoComplete="off" aria-label="私钥文件路径" />
                      </Form.Item>
                    ),
                  },
                ]} />
              </Form.Item>
              <Button type="primary" loading={busy} onClick={saveAndTest}>保存并测试</Button>
              <Typography.Text type="secondary" style={{ display: "block", fontSize: 12, marginTop: 8 }}>
                保存流程：校验（AppKey/PEM/算法匹配）→ 私钥写 ~/.dsh/futu-openapi-key.pem（0600）→
                写 ~/.dsh/futu-openapi.json → 更新通道 → 立即用已保存凭据发一次真实 trading-days 请求。
              </Typography.Text>
            </>
          )}
        </Form>
      </Card>
      {testResult && (
        <Alert showIcon type={testResult.ok ? "success" : "error"}
          message={testResult.ok ? "连通性测试通过（ret_code 0）" : "连通性测试失败"}
          description={<Typography.Text copyable={testResult.ok} style={{ fontSize: 12 }}>{testResult.text}</Typography.Text>} />
      )}
      <ServiceTokenSection />
      <StatusCard status={status} />
    </Space>
  );
}
