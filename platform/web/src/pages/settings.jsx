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
  Alert, App, Button, Card, Descriptions, Form, Input, InputNumber, Radio, Select, Space,
  Switch, Tabs, Tag, TimePicker, Typography,
} from "antd";
import { callApi, clearCache, getToken, setToken } from "../services/api.js";
import { useEndpoint } from "../services/hooks.js";
import {
  EXEC_WINDOW_MAX_MINUTES, MARKETS, POOL_KEY_DEFAULT, autoPipelineDraft,
  autoPipelinePayload, hhmmToTime, newStrategyRow, poolKeyOptions, timeToHhmm,
} from "../services/pipeline.js";
import { strategyFallbackOption, strategyOptions } from "../services/strategies.js";

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

/** 时刻字段的脏值提示：配置里存着不是 HH:MM 的历史值时，picker 只能显示占位符——
 *  这里如实说明「当前值是什么、为什么显示不出来」，不让它看起来像「本来就没配」。 */
function HhmmHint({ value }) {
  if (!value || hhmmToTime(value)) return null;
  return (
    <Typography.Text type="danger" style={{ fontSize: 12 }}>
      当前值 {value} 不是 HH:MM，请重新选择
    </Typography.Text>);
}

/** 自动流水线：总开关 + 策略行 + 各市场执行时刻 + 执行窗口 + 晚间对账时刻。
 *  端点：auto_pipeline —— 空载荷=读有效配置（与 GET /api/wb/auto_pipeline 同一实现）；
 *  带载荷=校验（复用 trading_core.autopipeline.apply_overlay，与调度侧同一实现，写入侧
 *  另加策略名取值域校验）后原子写，失败文件零改动。
 *  rules —— 只读规则清单：策略下拉的「已批准规则」组只能来自它（**只有 status='enabled'
 *  的规则可被消费**，见 services/strategies.js）。读取失败只降级为「只列内置策略」并提示。
 *  草稿与载荷的键集恒等于服务端白名单（services/pipeline.js 的 AUTO_PIPELINE_KEYS）——
 *  只读字段（error/date）不会经本页回写。
 *  字段形状（WP22，2026-09-18 实机反馈「配置字段记不住也输不对」）：策略/关注池键是
 *  Select（策略项来自内置策略 + 已批准规则；池键默认 watchlist 且允许自定义键名），
 *  两个时刻是 TimePicker（HH:mm，字符串与 dayjs 的互转在 services/pipeline.js）。
 *  语义：开关作用于流水线的**自动执行**环节；实盘（live）的计划照常生成但永不自动执行，
 *  仍走计划页人工确认。 */
function AutoPipelineSection() {
  const { message } = App.useApp();
  const config = useEndpoint("auto_pipeline", {}, []);
  const rules = useEndpoint("rules", {}, []);
  const [draft, setDraft] = React.useState(null);
  const [busy, setBusy] = React.useState(false);

  // 读回后初始化一次草稿：用户编辑期间不被后续读回覆盖（刷新只更新卡片右上角标签）。
  React.useEffect(() => {
    if (config.value && draft === null) setDraft(autoPipelineDraft(config.value));
  }, [config.value, draft]);

  const base = () => draft ?? autoPipelineDraft(config.value);
  const patch = (fields) => setDraft({ ...base(), ...fields });
  const patchExecAt = (market, value) => setDraft({
    ...base(), exec_at: { ...base().exec_at, [market]: value } });
  const patchStrategy = (index, fields) => {
    const rows = base().strategies.map((row, i) => (i === index ? { ...row, ...fields } : row));
    setDraft({ ...base(), strategies: rows });
  };
  const addStrategy = () => setDraft({ ...base(), strategies: [...base().strategies, newStrategyRow()] });
  const removeStrategy = (index) => setDraft({
    ...base(), strategies: base().strategies.filter((_row, i) => i !== index) });

  const save = async () => {
    setBusy(true);
    try {
      const value = await callApi("auto_pipeline", autoPipelinePayload(base()));
      setDraft(autoPipelineDraft(value));
      message.success("自动流水线配置已保存");
      config.refresh();
    } catch (error) {
      message.error(`保存失败：${error.message || error}`);
    } finally {
      setBusy(false);
    }
  };

  const loading = config.loading && !config.value;
  const enabled = base().enabled === true;
  const ruleList = rules.value?.rules;
  // 配置里存着「不可消费」的策略名（已停用规则/未知名字/自动路径跑不起来的单标的策略）：
  // 下拉仍会显示它（补位项，不被静默替换），这里再给一条说明——保存会被服务端拒绝或当日
  // build_plan 会失败，用户需要知道为什么。
  const blocked = base().strategies
    .map((row) => strategyFallbackOption(row.strategy, ruleList))
    .filter((option) => option !== null);
  return (
    <Card size="small" title="自动流水线"
      extra={enabled ? <Tag color="green">自动执行已开启</Tag> : <Tag>自动执行关闭</Tag>}>
      <Space direction="vertical" size="small" style={{ width: "100%" }}>
        {config.error && (
          <Alert type="warning" showIcon message={`配置读取失败：${config.error}`} />)}
        {config.value?.error && (
          <Alert type="error" showIcon message={`当前配置非法：${config.value.error}`}
            description="配置文件里的 auto_pipeline 无法被调度侧解析；保存一次合法配置即可覆盖。" />)}

        <Space size="small" wrap>
          <Switch checked={enabled} disabled={loading || busy} aria-label="启用自动流水线"
            onChange={(checked) => patch({ enabled: checked })} />
          <Typography.Text>启用自动流水线</Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            仅模拟盘自动执行；实盘计划照常生成，但仍需在计划页人工确认
          </Typography.Text>
        </Space>

        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          策略（市场 + 策略 + 关注池键名）：每个市场每天按策略生成目标权重并冻结计划。
          策略从下拉里选「内置策略」或研究页「已批准（已启用）的规则」——每项的说明写的是它怎么选股；
          关注池键选配置文件顶层的列表键（默认 watchlist），也可直接输入新的键名。
        </Typography.Text>
        {rules.error && (
          <Alert type="warning" showIcon message={`规则清单读取失败：${rules.error}`}
            description="策略下拉暂时只列内置策略（已批准规则在服务恢复前选不到）；不影响保存既有配置。" />)}
        {blocked.length > 0 && (
          <Alert type="warning" showIcon message="当前配置里的策略无法被自动流水线消费"
            description={`${blocked.map((option) => option.label).join("；")}——保存后当日 build_plan 会软跳过或失败，建议改用组合策略或重新批准规则。`} />)}
        {base().strategies.length === 0 && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            未配置策略：开启后流水线只跑数据与对账，不会生成计划
          </Typography.Text>)}
        {base().strategies.map((row, index) => (
          <Space key={`strategy-${index}`} size="small" wrap>
            <Select value={row.market || undefined} style={{ width: 90 }}
              disabled={busy} aria-label={`策略 ${index + 1} 市场`}
              placeholder="市场"
              onChange={(value) => patchStrategy(index, { market: value })}
              options={MARKETS.map((market) => ({ value: market, label: market }))} />
            <Select value={row.strategy || undefined} style={{ width: 360 }} showSearch
              optionFilterProp="label" disabled={busy}
              aria-label={`策略 ${index + 1} 名称`} placeholder="选择策略（内置策略或已批准规则）"
              onChange={(value) => patchStrategy(index, { strategy: value })}
              options={strategyOptions(ruleList, row.strategy)} />
            <Select value={row.watchlist ? [row.watchlist] : []} mode="tags"
              style={{ width: 220 }} disabled={busy}
              aria-label={`策略 ${index + 1} 关注池键`} placeholder="关注池键（默认 watchlist）"
              options={poolKeyOptions(base())}
              onChange={(values) => patchStrategy(index, {
                // tags 模式允许自由输入新键；取**最后输入的一个**（多贴一个也不越界），
                // 清空则回落到缺省池键（core 的同一缺省；留空会被服务端拒绝）。
                watchlist: String(values[values.length - 1] ?? POOL_KEY_DEFAULT),
              })} />
            <Button size="small" disabled={busy} onClick={() => removeStrategy(index)}>删除</Button>
          </Space>))}
        <Button size="small" disabled={busy} onClick={addStrategy}>添加策略</Button>

        <Space size="small" wrap>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>执行时刻（北京时间）</Typography.Text>
          {MARKETS.map((market) => (
            <Space key={market} size={4}>
              <Typography.Text>{market}</Typography.Text>
              <TimePicker value={hhmmToTime(base().exec_at[market])} format="HH:mm"
                minuteStep={1} allowClear={false} disabled={busy} style={{ width: 100 }}
                aria-label={`${market} 执行时刻`}
                onChange={(value) => patchExecAt(market, timeToHhmm(value))} />
              <HhmmHint value={base().exec_at[market]} />
            </Space>))}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            窗口
          </Typography.Text>
          <InputNumber value={base().exec_window_minutes} min={1}
            max={EXEC_WINDOW_MAX_MINUTES} disabled={busy}
            aria-label="执行窗口分钟数"
            onChange={(value) => patch({ exec_window_minutes: value })} />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>分钟</Typography.Text>
        </Space>

        <Space size="small" wrap>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>晚间对账时刻</Typography.Text>
          <TimePicker value={hhmmToTime(base().reconcile_at)} format="HH:mm" minuteStep={1}
            allowClear={false} disabled={busy} style={{ width: 100 }}
            aria-label="晚间对账时刻"
            onChange={(value) => patch({ reconcile_at: timeToHhmm(value) })} />
          <HhmmHint value={base().reconcile_at} />
        </Space>

        <Space size="small" wrap>
          <Button type="primary" loading={busy} disabled={loading || draft === null}
            onClick={save}>保存自动流水线</Button>
          {loading && <Typography.Text type="secondary">配置读取中…</Typography.Text>}
        </Space>
        <Typography.Text type="secondary" style={{ display: "block", fontSize: 12 }}>
          关闭时流水线不自动运行（数据同步与因子快照等既有作业不受本开关影响）；
          执行时刻按各市场交易日历触发，美股为夏令时口径，冬令时需人工调整。
        </Typography.Text>
      </Space>
    </Card>);
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
      <AutoPipelineSection />
      <StatusCard status={status} />
    </Space>
  );
}
