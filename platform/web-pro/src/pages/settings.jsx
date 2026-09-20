// V3 工作台「接入与授权」页（Ant Design Pro）。
// 功能模块与设计稿版 /v3/settings.html（规格书 = public/v3/settings.js）**一一对应**：
//   1. 交易模式卡：当前模式 + 切换控件（目标模式 / 口令 / 二次确认）；sim→live 必须逐字「确认实盘」，
//      口令不符**不发请求**；文案写明「切换模式 ≠ 授权下单」；成功后刷新状态
//   2. 模式切换审计：GET /api/v3/audit?window=120 的真实条目（操作人 / 来源 IP 如实标注无数据源）
//   3. 富途授权卡：渠道 / OpenAPI 模式 / Bearer 有效期（真实）+ 凭据表单 + OAuth 2.1+PKCE 面板
//      （私钥与密钥绝不回显、保存后清空输入）
//   4. 统一授权中心：GET /api/v3/credentials 的真实状态表 + Tushare token 可配置（保存 / 测试 / 清除）
//   5. 环境变量与密钥来源：settings.env 只显示「是否注入 + 来源」，绝不显示值
//   6. 授权与审计策略：阈值 2% / 20% / 15% + 只读降级说明（只读展示，不下发）
//   7. 自动流水线：开关 + 策略行（可增删）+ 三市场 exec_at + exec_window_minutes + reconcile_at + 保存
// 写动作纪律：全部只在人工点击后发起；口令与格式校验在发请求之前；加载时只发空载荷「读状态」。
import React from "react";
import {
  Alert, Button, Descriptions, Empty, Input, InputNumber, Modal, Select, Space, Statistic,
  Switch, Table, Tag, Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { fmt, noSourceText, postWb, useV3 } from "../services/api.js";
import { useMarket } from "../services/marketContext.jsx";

const { Text, Paragraph, Link } = Typography;

/* ── 契约常量（与设计稿版 / 服务端逐字一致） ───────────────────────────────── */
/** sim→live 的逐字口令（服务端 store_access.switch_mode 独立复核同一字符串）。 */
const LIVE_CONFIRMATION = "确认实盘";
/** 三市场执行时刻 / 晚间对账时刻：HH:MM。 */
const HHMM_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const EXEC_WINDOW_MAX = 240;
const MARKETS = [["SH", "A股 SH"], ["HK", "港股 HK"], ["US", "美股 US"]];
const ALGORITHMS = ["Ed25519", "RSA-SHA256"];
const CHANNELS = [["openapi", "OpenAPI（AppKey / OAuth 凭据）"], ["mcp", "MCP（OAuth 令牌通道）"]];
const POOL_KEY_DEFAULT = "watchlist";
const BUILTIN_STRATEGIES = ["watchlist_rsi", "momentum_value_top5", "ma_cross", "rsi"];
const PURPOSES = {
  DSH_HOME: "Harness 工作目录（Headless 自动唤醒入口）",
  DEEPSEEK_API_KEY: "DeepSeek 推理调用（BYOK）",
  QUANT_MCP_NODE: "MCP Bridge 节点入口",
  QUANT_MCP_SERVER: "平台 MCP 服务器地址",
  QUANT_MCP_CWD: "MCP 工具默认工作目录",
  QUANT_MCP_LOG: "MCP Bridge 日志文件",
  FUTU_OPEND_HOST: "OpenD 连接地址",
  FUTU_OPEND_PORT: "OpenD 连接端口",
  TUSHARE_TOKEN: "Tushare Pro 行情 / 财务数据",
};
const MODE_LABEL = { sim: "模拟盘 SIM", live: "实盘 LIVE" };

/** 服务端信封 → 「code：message」（绝不含服务端未返回的字段；密钥不进这条路径）。 */
const errText = (body) => `${body?.error?.code ?? "error"}：${body?.error?.message ?? "服务端未给出原因"}`;
const modeLabel = (mode) => MODE_LABEL[mode] ?? fmt.dash(mode) ?? "—";

/**
 * POST /api/v3/credentials（save / test / clear）。
 * 共享取数层 api.js 只提供 GET /api/v3/*（getV3/useV3）与 POST /api/wb/*（postWb），
 * 本页不得修改共享文件，故在此就地实现同一鉴权约定（localStorage.trading_token → Bearer）。
 * 只在人工点击后调用；响应只用于展示状态与掩码，绝不回显凭据值。
 */
async function postCredentials(payload) {
  const headers = { "content-type": "application/json" };
  try {
    const token = window.localStorage.getItem("trading_token");
    if (token) headers.Authorization = `Bearer ${token}`;
  } catch {
    /* localStorage 不可用时按未鉴权处理 */
  }
  let response = null;
  try {
    response = await window.fetch("/api/v3/credentials", {
      method: "POST",
      headers,
      body: JSON.stringify(payload ?? {}),
    });
  } catch (error) {
    return { ok: false, error: { code: "net", message: String(error?.message ?? error) } };
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (response.status === 401) {
    return { ok: false, error: { code: "unauthorized", message: "需要访问令牌：右上角「令牌」填入服务配置的 token" } };
  }
  if (!response.ok) {
    return { ok: false, error: { code: `http-${response.status}`, message: body?.error?.message || `HTTP ${response.status}` } };
  }
  return body;
}

/**
 * 只读 GET /api/v3/*（本页新增的「数据源与降级链」用）。
 * 为什么不直接用共享层 useV3：需要区分「端点未上线 / 返回非 JSON」与「接口正常但没数据」——
 * 未注册的 /api/v3/* 会落到 SPA 兜底返回 HTML（HTTP 200），共享层把这种响应折成 value=null，
 * 页面就无法如实写出原因。鉴权与查询串沿用共享层同一约定（localStorage.trading_token → Bearer），
 * 一律同源相对路径：控制台只与本服务通信（不接受外部基地址覆盖，避免同页混用事实源）。
 * 只读：只发 GET，加载与刷新都不发任何写请求。
 */
function useReadV3(path, params = {}) {
  const [state, setState] = React.useState({ loading: true, value: null, error: null });
  const seqRef = React.useRef(0);
  const key = JSON.stringify(params ?? {});
  const read = React.useCallback(
    async (refresh = false) => {
      seqRef.current += 1;
      const seq = seqRef.current;
      setState((prev) => ({ ...prev, loading: true, error: null }));
      const query = new URLSearchParams();
      for (const [name, value] of Object.entries(params ?? {})) {
        if (value === undefined || value === null || value === "") continue;
        query.set(name, String(value));
      }
      if (refresh) query.set("_", String(Date.now()));
      const suffix = query.toString();
      // 一律同源相对路径：控制台只与本服务通信，不接受任何外部基地址覆盖
      const url = `/api/v3/${path}${suffix ? `?${suffix}` : ""}`;
      const headers = {};
      try {
        const token = window.localStorage.getItem("trading_token");
        if (token) headers.Authorization = `Bearer ${token}`;
      } catch {
        /* localStorage 不可用时按未鉴权处理 */
      }
      let response = null;
      try {
        response = await window.fetch(url, { headers });
      } catch (error) {
        if (seqRef.current === seq) {
          setState({ loading: false, value: null, error: `网络不可达（${url}）：${String(error?.message || error)}` });
        }
        return;
      }
      const contentType = String(response.headers.get("content-type") || "");
      let body = null;
      try {
        body = await response.json();
      } catch {
        body = null;
      }
      if (seqRef.current !== seq) return;
      if (response.status === 401) {
        setState({ loading: false, value: null, error: "需要访问令牌：右上角「令牌」填入服务配置的 token" });
        return;
      }
      if (body === null || typeof body !== "object") {
        setState({
          loading: false,
          value: null,
          error: `HTTP ${response.status} 返回的不是 JSON（content-type=${contentType || "未标注"}）：该端点可能尚未上线`,
        });
        return;
      }
      if (!response.ok) {
        setState({ loading: false, value: body, error: body?.error?.message ? String(body.error.message) : `HTTP ${response.status}` });
        return;
      }
      setState({ loading: false, value: body, error: null });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [path, key],
  );
  React.useEffect(() => {
    read(false);
    return () => {
      seqRef.current += 1;
    };
  }, [read]);
  return { ...state, refresh: () => read(true) };
}

/** 错误原文：字符串原样展示；结构化错误保留 code 与 message（两段都不丢），其余回退到 JSON 原文。 */
function rawErrorText(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    if (value.code && value.message) return `${String(value.code)}：${String(value.message)}`;
    if (value.message) return String(value.message);
    if (value.code) return String(value.code);
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

/** 自动流水线有效配置 → 本地草稿（缺省补全，非法配置保留 error 供如实展示）。 */
function autoDraft(config) {
  const src = config && typeof config === "object" ? config : {};
  const execAt = src.exec_at && typeof src.exec_at === "object" ? src.exec_at : {};
  return {
    enabled: src.enabled === true,
    strategies: (Array.isArray(src.strategies) ? src.strategies : []).map((row) => ({
      market: row?.market ?? "SH",
      strategy: row?.strategy ?? "",
      watchlist: row?.watchlist ?? POOL_KEY_DEFAULT,
    })),
    exec_at: Object.fromEntries(MARKETS.map(([market]) => [market, execAt[market] ?? ""])),
    exec_window_minutes: Number.isInteger(src.exec_window_minutes) ? src.exec_window_minutes : 30,
    reconcile_at: typeof src.reconcile_at === "string" ? src.reconcile_at : "",
    error: src.error ?? null,
  };
}

/** 统一「无数据源」区块：措辞来自 api.js 的 noSourceText。 */
function NoSource({ what, why, type = "warning" }) {
  return <Alert type={type} showIcon message={noSourceText(what, why)} />;
}

/**
 * 口径标注（每个受市场影响的卡片都挂一句）：
 *   global=true  → 「全局口径，不按市场拆分」（模式 / 台账 / 凭据 / 环境 / 降级链 / 审计）
 *   global=false → 「当前市场 {label} {market}」（自动流水线策略行按市场分别配置）
 */
function ScopeTag({ market, label, global = false, text }) {
  const scope = text || (global ? "全局口径，不按市场拆分" : `当前市场 ${label} ${market}`);
  return (
    <Tag color={global ? "default" : "blue"} style={{ marginInlineEnd: 0 }}>
      {scope}
    </Tag>
  );
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

/** 行内结果行（成功 / 失败 / 进行中）。 */
function Msg({ message }) {
  if (!message) return null;
  const color = message.tone === "bad" ? "#f8514d" : message.tone === "ok" ? "#3fb950" : "#8b97a5";
  return <Text style={{ color, fontSize: 12 }}>{message.text}</Text>;
}

export default function SettingsPage() {
  const { market, label } = useMarket();
  // 全部为全局口径（后端不按市场拆分，页面也不按市场重取）：模式 / 台账 / 环境 / 凭据 / 降级链 / 审计
  const settings = useV3("settings");
  const metrics = useV3("metrics");
  const overview = useV3("overview");
  const orders = useV3("oms/orders");
  const audit = useV3("audit", { window: 120 });
  const credentials = useV3("credentials");
  // 数据源降级链状态（只读 GET；链路为全局口径，不按市场重取）
  const sourcesStatus = useReadV3("sources/status");

  const s = settings.value ?? {};
  const mode = String(s.trading_mode ?? overview.value?.mode ?? "");
  const envRows = Array.isArray(s.env) ? s.env : [];
  const credKeys = Array.isArray(credentials.value?.keys) ? credentials.value.keys : [];
  const auditEntries = Array.isArray(audit.value?.data?.entries) ? audit.value.data.entries : [];
  const auditStats = audit.value?.data?.stats ?? {};
  const stages = orders.value?.stages ?? {};
  const nav = Number(orders.value?.nav);
  const futu = s.futu ?? {};
  const bearer = futu.mcp_bearer ?? {};
  const expiryRaw = bearer.expiry ? String(bearer.expiry) : "";
  const bearerLeftMinutes = expiryRaw && !Number.isNaN(Date.parse(expiryRaw))
    ? Math.max(0, Math.round((Date.parse(expiryRaw) - Date.now()) / 60000))
    : null;
  const envByKey = Object.fromEntries(envRows.map((item) => [item.key, item]));
  const futuReady = Boolean(futu.channel);
  const riskReady = Number.isFinite(nav);

  /* ── 交易模式切换（人工动作 · POST /api/wb/switch-mode） ───────────────────── */
  const [target, setTarget] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [modeMsg, setModeMsg] = React.useState(null);
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [ack, setAck] = React.useState(false);
  const [switching, setSwitching] = React.useState(false);
  const effectiveTarget = target || (mode === "live" ? "sim" : "live");
  const pwOk = password === LIVE_CONFIRMATION;

  /** 客户端前置校验：sim→live 口令不符**不发请求**（服务端复核是第二道，不是第一道）。 */
  function requestSwitch() {
    if (!mode) {
      setModeMsg({ tone: "info", text: "模式未知：等待 /api/v3/settings 返回后再发起切换（本页未发送请求）" });
      return;
    }
    if (effectiveTarget === mode) {
      setModeMsg({ tone: "info", text: `当前已处于 ${modeLabel(mode)}，无需切换（本页未发送请求）` });
      return;
    }
    if (effectiveTarget === "live" && !pwOk) {
      setModeMsg({
        tone: "bad",
        text: `口令校验未通过：请输入逐字「${LIVE_CONFIRMATION}」。本页未发送任何请求。`,
      });
      return;
    }
    setAck(false);
    setConfirmOpen(true);
    setModeMsg({
      tone: "info",
      text: effectiveTarget === "live"
        ? "口令已通过客户端校验 → 请在弹窗二次确认（服务端仍会独立复核）"
        : "LIVE→SIM 无需口令 → 请在弹窗二次确认",
    });
  }

  /** 真正的人工写动作：只在人点「确认切换」后发一次请求。 */
  async function confirmSwitch() {
    const next = effectiveTarget;
    const expected = mode;
    const payload = { mode: next, expected_mode: expected };
    if (next === "live") payload.confirmation = LIVE_CONFIRMATION;
    setConfirmOpen(false);
    setSwitching(true);
    setModeMsg({ tone: "info", text: `正在切换 ${expected} → ${next} …` });
    let body = null;
    try {
      body = await postWb("switch-mode", payload);
    } catch (error) {
      body = { ok: false, error: { code: "net", message: String(error?.message ?? error) } };
    }
    setSwitching(false);
    if (!body?.ok) {
      setModeMsg({ tone: "bad", text: `切换失败：${errText(body)}` });
      return;
    }
    const value = body.value ?? {};
    setPassword("");
    setModeMsg({
      tone: "ok",
      text: `已切换：${value.previous_mode ?? expected} → ${value.mode ?? next} · 下单授权=${value.order_authorized === true ? "是" : "否（模式 ≠ 下单授权）"}`,
    });
    await Promise.all([settings.refresh(), overview.refresh(), audit.refresh()]);
  }

  /* ── 富途 OpenAPI 凭据 + OAuth 2.1 + PKCE（人工动作） ─────────────────────── */
  const [openapi, setOpenapi] = React.useState({ loading: true });
  const [appKey, setAppKey] = React.useState("");
  const [algorithm, setAlgorithm] = React.useState("Ed25519");
  const [channel, setChannel] = React.useState("openapi");
  const [pem, setPem] = React.useState("");
  const [keyPath, setKeyPath] = React.useState("");
  const [futuMsg, setFutuMsg] = React.useState(null);
  const [futuBusy, setFutuBusy] = React.useState(null);
  const [clientId, setClientId] = React.useState("");
  const [oauth, setOauth] = React.useState({ phase: "idle", value: null, error: null });
  const oauthTimer = React.useRef(null);
  const oauthSeq = React.useRef(0);
  const channelTouched = React.useRef(false);

  /** 读状态（空载荷）：GET 语义的只读调用，不在加载期写任何字节。 */
  const loadOpenapi = React.useCallback(async () => {
    setOpenapi((prev) => ({ ...prev, loading: true }));
    const body = await postWb("openapi_config", {});
    setOpenapi(body?.ok ? { loading: false, value: body.value ?? {} } : { loading: false, error: errText(body) });
  }, []);

  React.useEffect(() => {
    loadOpenapi();
  }, [loadOpenapi]);

  React.useEffect(() => {
    const status = openapi.value;
    if (!channelTouched.current && status?.channel) setChannel(status.channel);
  }, [openapi.value]);

  React.useEffect(() => () => {
    if (oauthTimer.current) window.clearInterval(oauthTimer.current);
  }, []);

  function statusText() {
    if (openapi.loading) return "状态读取中…";
    if (openapi.error) return `状态读取失败：${openapi.error}`;
    const v = openapi.value ?? {};
    return `configured=${v.configured === true} · mode=${fmt.dash(v.mode)} · 通道=${fmt.dash(v.channel)}`
      + ` · app_key=${fmt.dash(v.app_key_masked)} · 私钥${v.private_key_exists ? `在位（指纹 ${fmt.dash(v.private_key_fingerprint)}）` : "未检出"}`
      + ` · ready=${v.ready === true}`;
  }

  /** 保存凭据：先本地校验（AppKey 必填、私钥二选一），再发一次请求；成功后清空输入。 */
  async function saveFutu() {
    const key = appKey.trim();
    if (!key) {
      setFutuMsg({ tone: "bad", text: "保存失败：AppKey ID 必填（本页未发送请求）" });
      return;
    }
    const pemText = pem.trim();
    const pathText = keyPath.trim();
    if (!pemText && !pathText) {
      setFutuMsg({ tone: "bad", text: "保存失败：私钥二选一 —— 粘贴 PEM 原文或给出已在位的私钥文件路径（本页未发送请求）" });
      return;
    }
    const payload = { mode: "appkey", app_key: key, algorithm, channel };
    if (pemText) payload.private_key_pem = pemText;
    if (pathText) payload.private_key_path = pathText;
    setFutuBusy("save");
    setFutuMsg({ tone: "info", text: "正在保存凭据…" });
    const body = await postWb("openapi_config", payload);
    setFutuBusy(null);
    if (!body?.ok) {
      setFutuMsg({ tone: "bad", text: `保存失败：${errText(body)}` });
      return;
    }
    const v = body.value ?? {};
    setAppKey("");
    setPem("");
    setOpenapi({ loading: false, value: v });
    setFutuMsg({
      tone: "ok",
      text: `已保存（mode=${fmt.dash(v.mode)} · 算法 ${fmt.dash(v.algorithm)} · 私钥${v.private_key_exists ? "在位" : "未检出"} · 指纹 ${fmt.dash(v.private_key_fingerprint)}）· 密钥输入已清空、私钥不回显`,
    });
  }

  /** 测试连接：用**已保存**凭据发一次真实 trading-days 请求（不做任何写入）。 */
  async function testFutu() {
    setFutuBusy("test");
    setFutuMsg({ tone: "info", text: "正在用已保存凭据测试（真实请求）…" });
    const body = await postWb("openapi_test", {});
    setFutuBusy(null);
    if (!body?.ok) {
      setFutuMsg({ tone: "bad", text: `测试未通过：${errText(body)}` });
      return;
    }
    const v = body.value ?? {};
    setFutuMsg({
      tone: "ok",
      text: `测试通过：HTTP ${fmt.dash(v.http_status)} · ret_code ${fmt.dash(v.ret_code)} · ${fmt.dash(v.ret_msg)} · ${fmt.dash(v.latency_ms)}ms（行情 ${fmt.dash(v.data?.market)}，${(v.data?.trade_days ?? []).length} 个交易日）`,
    });
  }

  function stopOAuthPolling() {
    if (oauthTimer.current) {
      window.clearInterval(oauthTimer.current);
      oauthTimer.current = null;
    }
  }

  /** 开始 OAuth：start → 展示授权 URL → 每 2 秒轮询 status 直到 done / error。 */
  async function startOAuth() {
    stopOAuthPolling();
    oauthSeq.current += 1;
    const seq = oauthSeq.current;
    const payload = { action: "start" };
    const trimmed = clientId.trim();
    if (trimmed) payload.client_id = trimmed;
    setOauth({ phase: "starting", value: null, error: null });
    setFutuMsg({ tone: "info", text: "正在发起 OAuth 授权（PKCE + S256）…" });
    const body = await postWb("openapi_oauth", payload);
    if (seq !== oauthSeq.current) return;
    if (!body?.ok) {
      setOauth({ phase: "error", value: null, error: errText(body) });
      setFutuMsg({ tone: "bad", text: `发起授权失败：${errText(body)}` });
      return;
    }
    setOauth({ phase: "waiting", value: body.value ?? {}, error: null });
    setFutuMsg({ tone: "info", text: "等待你在浏览器完成授权…（每 2 秒轮询 status，取消或完成即停）" });
    oauthTimer.current = window.setInterval(async () => {
      const status = await postWb("openapi_oauth", { action: "status" });
      if (seq !== oauthSeq.current) return;
      if (!status?.ok) {
        stopOAuthPolling();
        setOauth({ phase: "error", value: null, error: errText(status) });
        setFutuMsg({ tone: "bad", text: `状态轮询失败：${errText(status)}` });
        return;
      }
      const value = status.value ?? {};
      const phase = value.done ? "done" : value.error ? "error" : value.pending ? "waiting" : "idle";
      setOauth({ phase, value, error: value.error ?? null });
      if (value.done || value.error || !value.pending) {
        stopOAuthPolling();
        if (value.done) {
          setFutuMsg({
            tone: "ok",
            text: `OAuth 授权完成：凭据已由服务端落盘（0600）${value.tokens_saved ? "" : " · 服务端未报告 tokens_saved"}`,
          });
          await loadOpenapi();
          await settings.refresh();
        } else if (value.error) {
          setFutuMsg({ tone: "bad", text: `OAuth 授权未完成：${value.error}` });
        }
      }
    }, 2000);
  }

  /** 取消授权：停止轮询 + 服务端清状态（人工点击才发）。 */
  async function cancelOAuth() {
    stopOAuthPolling();
    oauthSeq.current += 1;
    const body = await postWb("openapi_oauth", { action: "cancel" });
    if (!body?.ok) {
      setFutuMsg({ tone: "bad", text: `取消失败：${errText(body)}` });
      return;
    }
    setOauth({ phase: "idle", value: null, error: null });
    setFutuMsg({ tone: "ok", text: "已取消授权（回调监听已停止，服务端状态已清）" });
  }

  /* ── 统一授权中心：Tushare token 的页面化配置（人工动作） ─────────────────── */
  const [tokenValue, setTokenValue] = React.useState("");
  const [credMsg, setCredMsg] = React.useState(null);
  const [credBusy, setCredBusy] = React.useState(null);
  const [clearOpen, setClearOpen] = React.useState(false);
  const firstKey = credKeys[0] ?? null;

  async function credentialAction(action) {
    if (!firstKey) return;
    const value = tokenValue.trim();
    if (action === "save" && !value) {
      setCredMsg({ tone: "bad", text: "请先粘贴 token（本页未发送请求）" });
      return;
    }
    setCredBusy(action);
    setCredMsg({ tone: "info", text: "执行中…" });
    const payload = { action, key: firstKey.key };
    if (action !== "test") payload.value = value;
    const body = await postCredentials(payload);
    setCredBusy(null);
    if (!body?.ok) {
      setCredMsg({ tone: "bad", text: `${body?.error?.code ?? "失败"}：${body?.error?.message ?? "服务端未给出原因"}` });
      return;
    }
    if (action === "test") {
      setCredMsg({ tone: "ok", text: `连通正常（${fmt.dash(body.latency_ms)}ms · 来源 ${fmt.dash(body.source)}）` });
    } else {
      if (action === "save") setTokenValue("");
      setCredMsg({
        tone: "ok",
        text: action === "save" ? "已保存（0600 落盘，立即生效；环境变量优先）" : "已清除页面配置（环境变量不受影响）",
      });
    }
    await credentials.refresh();
  }

  /* ── 自动流水线（人工动作 · POST /api/wb/auto_pipeline） ──────────────────── */
  const [auto, setAuto] = React.useState(null);
  const [autoMsg, setAutoMsg] = React.useState(null);
  const [autoSaving, setAutoSaving] = React.useState(false);

  const loadAuto = React.useCallback(async () => {
    const body = await postWb("auto_pipeline", {});
    if (body?.ok) {
      setAuto(autoDraft(body.value));
    } else {
      setAutoMsg({ tone: "bad", text: `读取自动流水线失败：${errText(body)}` });
    }
  }, []);

  React.useEffect(() => {
    loadAuto();
  }, [loadAuto]);

  const patchAuto = (patch) => setAuto((prev) => (prev ? { ...prev, ...patch } : prev));
  const patchStrategy = (index, patch) => setAuto((prev) => {
    if (!prev) return prev;
    const strategies = prev.strategies.map((row, i) => (i === index ? { ...row, ...patch } : row));
    return { ...prev, strategies };
  });

  /** 控件 → 载荷（含本地格式校验；非法返回 null，**不发请求**）。 */
  function readAutoPayload() {
    if (!auto) return null;
    const execAt = {};
    for (const [market] of MARKETS) {
      const value = String(auto.exec_at?.[market] ?? "").trim();
      if (!HHMM_RE.test(value)) return null;
      execAt[market] = value;
    }
    const windowValue = Number(auto.exec_window_minutes);
    if (!Number.isInteger(windowValue) || windowValue < 1 || windowValue > EXEC_WINDOW_MAX) return null;
    const reconcile = String(auto.reconcile_at ?? "").trim();
    if (!HHMM_RE.test(reconcile)) return null;
    return {
      enabled: auto.enabled === true,
      strategies: auto.strategies.map((row) => ({
        market: row.market,
        strategy: String(row.strategy ?? "").trim(),
        watchlist: String(row.watchlist ?? "").trim() || POOL_KEY_DEFAULT,
      })),
      exec_at: execAt,
      exec_window_minutes: windowValue,
      reconcile_at: reconcile,
    };
  }

  async function saveAuto() {
    const payload = readAutoPayload();
    if (!payload) {
      setAutoMsg({
        tone: "bad",
        text: `保存失败：执行时刻需为 HH:MM（如 09:35），执行窗口需为 1–${EXEC_WINDOW_MAX} 的整数。本页未发送请求。`,
      });
      return;
    }
    setAutoSaving(true);
    setAutoMsg({ tone: "info", text: "正在保存…" });
    const body = await postWb("auto_pipeline", payload);
    setAutoSaving(false);
    if (!body?.ok) {
      setAutoMsg({ tone: "bad", text: `保存失败：${errText(body)}` });
      return;
    }
    setAuto(autoDraft(body.value));
    const effective = await postWb("auto_pipeline", {});
    if (effective?.ok) {
      const value = autoDraft(effective.value);
      setAuto(value);
      setAutoMsg({
        tone: "ok",
        text: `已保存并重新读取生效值：enabled=${value.enabled === true} · 策略 ${value.strategies.length} 项 · 执行窗口 ${value.exec_window_minutes} 分钟 · 对账 ${fmt.dash(value.reconcile_at)}`,
      });
    } else {
      setAutoMsg({ tone: "bad", text: `已保存，但重新读取失败：${errText(effective)}` });
    }
    await settings.refresh();
  }

  /* ── 审计导出（浏览器端 CSV，用当前真实条目；不写服务端） ─────────────────── */
  function exportAudit() {
    const escape = (value) => `"${String(value ?? "").replace(/"/g, '""')}"`;
    const lines = ["时间,来源,类型,标的,详情"].concat(
      auditEntries.map((entry) => [entry.at, entry.source_label ?? entry.source, entry.kind, entry.ticker, entry.detail].map(escape).join(",")),
    );
    const blob = new Blob([`\ufeff${lines.join("\n")}`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `v3-audit-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  /* ── 「数据源与降级链」派生值（GET /api/v3/sources/status，只读） ──────────── */
  const chainPayload = sourcesStatus.value && sourcesStatus.value.ok === true ? sourcesStatus.value : null;
  const chainRows = Array.isArray(chainPayload?.chains) ? chainPayload.chains : [];
  const chainAvailable = chainRows.filter((row) => row?.available === true).length;
  const chainWithFallback = chainRows.filter(
    (row) => typeof row?.fallback === "string" && row.fallback.trim() !== "",
  ).length;
  const chainReason = sourcesStatus.error
    ? sourcesStatus.error
    : sourcesStatus.value && sourcesStatus.value.ok === false
      ? `${sourcesStatus.value.error?.code ?? "error"}：${sourcesStatus.value.error?.message ?? "服务端未给出原因"}`
      : "GET /api/v3/sources/status 未返回 chains 清单";

  const unconfigured = credKeys.filter((item) => !item.present).length;
  const injectedMissing = envRows.filter((item) => !item.injected).length;
  const oauthValue = oauth.value ?? {};
  const oauthBadge = oauth.phase === "done"
    ? { color: "success", text: "已完成 · 凭据已落盘" }
    : oauth.phase === "error"
      ? { color: "error", text: "失败" }
      : oauth.phase === "waiting" || oauth.phase === "starting"
        ? { color: "warning", text: "等待授权中…" }
        : { color: "default", text: "无进行中的流程" };

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {/* 页头市场接入点：本页哪些跟随市场、哪些是全局口径 */}
      <ProCard bordered bodyStyle={{ padding: "10px 16px" }}>
        <Space size={10} wrap>
          <ScopeTag market={market} label={label} />
          <Text type="secondary" style={{ fontSize: 11 }}>
            {`本页市场接入点：仅「自动流水线」的策略行按市场分别配置（默认新增行 market=SH，可逐行改为 HK / US）；
            交易模式与台账统计、富途 / 统一授权中心 / 环境变量 / 数据源与降级链 / 审计均为全局口径（不按市场拆分），切换市场不重取这些数据。`}
          </Text>
        </Space>
      </ProCard>

      {s.mode_note ? (
        <Alert type="info" showIcon message={String(s.mode_note)} />
      ) : null}

      {/* 1. 交易模式卡（全局口径：单一模式 + 单一台账） */}
      <ProCard
        title="交易模式"
        bordered
        extra={
          <Space size={8} wrap>
            <Tag color={mode === "live" ? "error" : mode === "sim" ? "success" : "default"}>
              {`当前生效：${modeLabel(mode)}`}
            </Tag>
            <ScopeTag market={market} label={label} global />
            <Text type="secondary" style={{ fontSize: 12 }}>数据来源 /api/v3/settings · /api/v3/overview · /api/v3/oms/orders</Text>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          {`口径：交易模式、台账权益与 OMS 台账统计为全局口径（不按市场拆分）——台账是单一账本，模式是全平台一个开关；
          按市场查看持仓 / 订单明细见「执行与审批」页（/api/v3/oms/orders?market=${market}），授权凭据本身不分市场。`}
        </Text>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: 12 }}>
          <div>
            <Space size={8}><Tag color={mode === "sim" ? "success" : "default"}>{mode === "sim" ? "当前生效" : "未生效"}</Tag><Text strong>模拟盘 SIM</Text></Space>
            <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
              订单只进入本地模拟账本，不触达券商；行情、信号与 Agent Loop 全链路真实演练。
            </Paragraph>
          </div>
          <div>
            <Space size={8}><Tag color={mode === "live" ? "success" : "default"}>{mode === "live" ? "当前生效" : "未启用"}</Tag><Text strong>实盘 LIVE</Text></Space>
            <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
              订单将经富途 OpenAPI 真实报出；启用前需通过前置校验，且只能由人在工作台 Web 输入口令完成。
            </Paragraph>
          </div>
        </div>

        <Space size={32} wrap style={{ marginTop: 14 }}>
          <Statistic title="当前权益（本地模拟台账）" value={fmt.dash(overview.value?.equity?.current)} valueStyle={{ fontSize: 18 }} />
          <Statistic title="OMS 台账待人工" value={fmt.dash(stages.manual)} suffix="笔" valueStyle={{ fontSize: 18 }} />
          <Statistic title="台账 NAV" value={fmt.dash(Number.isFinite(nav) ? nav : undefined)} valueStyle={{ fontSize: 18 }} />
        </Space>

        <Descriptions
          size="small"
          column={1}
          style={{ marginTop: 12 }}
          items={[
            {
              key: "futu",
              label: "前置校验 · 富途交易授权",
              children: (
                <Space size={6}>
                  <Tag color={futuReady ? "success" : "warning"}>{futuReady ? "通过" : "未通过"}</Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>{`futu.channel=${fmt.dash(futu.channel)}（/api/v3/settings）`}</Text>
                </Space>
              ),
            },
            {
              key: "risk",
              label: "前置校验 · 单笔 / 行业风控",
              children: (
                <Space size={6}>
                  <Tag color={riskReady ? "success" : "warning"}>{riskReady ? "通过" : "未知"}</Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>{`风控分级：nav=${fmt.dash(Number.isFinite(nav) ? nav : undefined)} · 台账订单 ${fmt.dash(stages.manual)} 笔待人工`}</Text>
                </Space>
              ),
            },
            {
              key: "pw",
              label: "前置校验 · 口令",
              children: (
                <Space size={6}>
                  <Tag color={pwOk ? "success" : "warning"}>{pwOk ? "已通过客户端校验" : "未通过"}</Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {`逐字口令「${LIVE_CONFIRMATION}」由本页先校验、服务端 switch_mode 独立复核；口令不符时本页不发请求`}
                  </Text>
                </Space>
              ),
            },
          ]}
        />

        <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px dashed #232b37" }}>
          <Space size={10} wrap align="center">
            <Text strong style={{ fontSize: 12 }}>目标模式</Text>
            <Select
              size="small"
              style={{ width: 150 }}
              value={effectiveTarget}
              onChange={(value) => setTarget(value)}
              options={[["live", "实盘 LIVE"], ["sim", "模拟盘 SIM"]].map(([value, label]) => ({ value, label }))}
            />
            <Input.Password
              size="small"
              style={{ width: 240 }}
              placeholder={`口令：${LIVE_CONFIRMATION}（仅 sim→live 需要）`}
              value={password}
              autoComplete="new-password"
              onChange={(event) => setPassword(event.target.value)}
            />
            <Button
              size="small"
              danger={effectiveTarget === "live"}
              type="primary"
              loading={switching}
              onClick={requestSwitch}
            >
              {effectiveTarget === "live" ? "确认切换到实盘" : "确认切回模拟盘"}
            </Button>
            <Msg message={modeMsg} />
          </Space>
          <Paragraph type="secondary" style={{ fontSize: 12, marginTop: 8, marginBottom: 0 }}>
            切换模式 ≠ 授权下单：下单 / 执行只经「执行与审批」页的受约束入口；sim→live 需逐字口令「{LIVE_CONFIRMATION}」+
            二次确认，LIVE→SIM 无需口令；任何切换都写入审计日志（口令只用于本页前置校验，不落 localStorage）。
          </Paragraph>
        </div>
      </ProCard>

      {/* 2. 模式切换审计（全局审计链） */}
      <ProCard
        title="模式切换审计"
        bordered
        extra={
          <Space size={8} wrap>
            <ScopeTag market={market} label={label} global />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {auditEntries.length > 0 ? `真实审计链 · 最近 ${auditEntries.length} 条` : "真实审计链 · 当前窗口无记录"}
            </Text>
            <Button size="small" disabled={auditEntries.length === 0} onClick={exportAudit}>导出审计日志</Button>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          口径：审计链记录全局动作（模式切换等），为全局口径（不按市场拆分）；本卡不按市场重取。
        </Text>
        {audit.error ? (
          <NoSource what="审计链" why={`/api/v3/audit 取数失败：${audit.error}`} type="error" />
        ) : auditEntries.length === 0 ? (
          <Empty image={EMPTY_FRAME} description={noSourceText("审计链", "audit 工具返回 0 条记录")} />
        ) : (
          <>
            <Table
              size="small"
              rowKey={(record, index) => String(record.id ?? index)}
              dataSource={auditEntries}
              pagination={false}
              locale={emptyTable("审计链", "audit 工具返回 0 条记录")}
              columns={[
                { title: "时间", width: 170, render: (_, entry) => fmt.stamp(entry.at) },
                { title: "来源", width: 170, render: (_, entry) => fmt.dash(entry.source_label ?? entry.source) },
                { title: "类型", width: 100, render: (_, entry) => <Tag>{fmt.dash(entry.kind)}</Tag> },
                { title: "标的", width: 110, render: (_, entry) => fmt.dash(entry.ticker) },
                { title: "操作人 / 来源 IP", render: () => <Text type="secondary" style={{ fontSize: 12 }}>无数据源（审计链不记录操作人与来源 IP）</Text> },
                { title: "详情摘要", render: (_, entry) => <Text style={{ fontSize: 12 }} title={String(entry.detail ?? "")}>{String(entry.detail ?? "—").slice(0, 60)}</Text> },
              ]}
            />
            <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 8, marginBottom: 0 }}>
              {`来源：/api/v3/audit · window=120 由前端传入 · 信号 ${fmt.dash(auditStats.signals)} · 订单事实 ${fmt.dash(auditStats.orders)} · 成交 ${fmt.dash(auditStats.fills)}（链接 ${fmt.dash(auditStats.linked)} / 未链接 ${fmt.dash(auditStats.unlinked)}）`}
            </Paragraph>
          </>
        )}
      </ProCard>

      {/* 3. 富途 OpenAPI / OpenD 授权（凭据与通道全局共用；本页无按市场拆分的账户/持仓字段） */}
      <ProCard
        title="富途 OpenAPI / OpenD 授权"
        bordered
        extra={
          <Space size={8} wrap>
            <Tag color={futuReady ? "success" : "warning"}>
              {`OpenAPI 凭据${futuReady ? "就绪" : "不可用"} · 连接态由 OpenD 会话决定`}
            </Tag>
            <ScopeTag market={market} label={label} global />
            <Text type="secondary" style={{ fontSize: 12 }}>行情与交易通道的唯一实盘入口</Text>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          {`口径：凭据（AppKey / 私钥 / OAuth token）与 OpenD 通道全局共用，不按市场拆分；本页不展示按市场拆分的富途账户与持仓
          （/api/v3/settings 未返回该字段），按市场查看持仓 / 订单请见「执行与审批」页（market=${market}）。`}
        </Text>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 12 }}>
          <Descriptions
            size="small"
            column={1}
            title="OpenD 连接配置"
            items={[
              {
                key: "endpoint",
                label: "Host / Port",
                children: envByKey.FUTU_OPEND_HOST?.injected || envByKey.FUTU_OPEND_PORT?.injected
                  ? `${envByKey.FUTU_OPEND_HOST?.injected ? "FUTU_OPEND_HOST 已注入" : "FUTU_OPEND_HOST 默认"} : ${envByKey.FUTU_OPEND_PORT?.injected ? "FUTU_OPEND_PORT 已注入" : "FUTU_OPEND_PORT 默认"}`
                  : "默认 127.0.0.1 : 11111",
              },
              {
                key: "conn",
                label: "连接状态 / 心跳",
                children: <Text type="secondary">无数据源（本服务不维护 OpenD 会话：连接与心跳由 OpenD 客户端自身暴露）</Text>,
              },
              {
                key: "account",
                label: "登录账号 / 更新",
                children: <Text type="secondary">无数据源（工具面不返回账号，凭据文件只回键名不回值）</Text>,
              },
              {
                key: "store",
                label: "存储方式",
                children: (
                  <Text style={{ fontSize: 12 }}>
                    {`凭据文件 ~/futu-openapi.json（只回键名）· mode=${fmt.dash(futu.openapi?.mode)} · 键名 ${fmt.dash((futu.openapi?.config_keys ?? []).join(" / "))} · 最近轮换时间无数据源`}
                  </Text>
                ),
              },
            ]}
          />
          <Descriptions
            size="small"
            column={1}
            title="富途 MCP 授权"
            items={[
              {
                key: "bearer",
                label: "Bearer Token",
                children: (
                  <Space size={8}>
                    <Tag color={bearer.present ? "success" : "warning"}>{bearer.present ? "有效" : "无数据源"}</Tag>
                    {bearerLeftMinutes === null ? null : (
                      <Text className="num">{`剩余 ${Math.floor(bearerLeftMinutes / 60)}h${String(bearerLeftMinutes % 60).padStart(2, "0")}m`}</Text>
                    )}
                  </Space>
                ),
              },
              {
                key: "expiry",
                label: "有效期至",
                children: `${fmt.stamp(bearer.expiry)}（来自 mcp_bearer.expiry）`,
              },
              {
                key: "renew",
                label: "自动续期",
                children: <Text type="secondary">无数据源（工具面不返回自动续期开关与最近续期时间）</Text>,
              },
              {
                key: "scope",
                label: "授权作用域",
                children: <Text type="secondary">无数据源（作用域来自富途侧授权，工具面不返回，只读展示）</Text>,
              },
              {
                key: "channel",
                label: "通道来源",
                children: `${fmt.dash(futu.channel)} · ${fmt.dash(futu.channel_source)}`,
              },
            ]}
          />
        </div>

        <div style={{ marginTop: 12, paddingTop: 12, borderTop: "1px dashed #232b37" }}>
          <Space size={10} wrap align="center">
            <Text strong style={{ fontSize: 12 }}>OpenAPI 凭据与 OAuth 授权</Text>
            <Text type="secondary" style={{ fontSize: 11.5 }}>{statusText()}</Text>
          </Space>

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12, marginTop: 10 }}>
            <div>
              <Text style={{ fontSize: 12 }}>AppKey ID（必填）</Text>
              <Input.Password
                value={appKey}
                placeholder="AppKey ID（保存后不回显）"
                autoComplete="new-password"
                onChange={(event) => setAppKey(event.target.value)}
              />
            </div>
            <div>
              <Text style={{ fontSize: 12 }}>签名算法</Text>
              <Select
                style={{ width: "100%" }}
                value={algorithm}
                onChange={(value) => setAlgorithm(value)}
                options={ALGORITHMS.map((value) => ({ value, label: value }))}
              />
            </div>
            <div>
              <Text style={{ fontSize: 12 }}>通道（写 trading-platform.json 的 futu_channel）</Text>
              <Select
                style={{ width: "100%" }}
                value={channel}
                onChange={(value) => { channelTouched.current = true; setChannel(value); }}
                options={CHANNELS.map(([value, label]) => ({ value, label }))}
              />
            </div>
            <div>
              <Text style={{ fontSize: 12 }}>私钥文件路径（二选一：已在位）</Text>
              <Input
                value={keyPath}
                placeholder="如 ~/.dsh/futu-openapi-key.pem"
                autoComplete="off"
                onChange={(event) => setKeyPath(event.target.value)}
              />
            </div>
            <div style={{ gridColumn: "1 / -1" }}>
              <Text style={{ fontSize: 12 }}>私钥 PEM（二选一：粘贴原文）</Text>
              <Input.TextArea
                rows={4}
                value={pem}
                spellCheck={false}
                placeholder="私钥 PEM 原文（服务端落盘 0600；保存成功后本框立即清空、不回显）"
                onChange={(event) => setPem(event.target.value)}
              />
              <Text type="secondary" style={{ fontSize: 11 }}>私钥原文只进不出：页面与接口都不回显，也不写 localStorage。</Text>
            </div>
          </div>

          <Space size={8} wrap style={{ marginTop: 10 }}>
            <Button type="primary" size="small" loading={futuBusy === "save"} onClick={saveFutu}>保存凭据</Button>
            <Button size="small" loading={futuBusy === "test"} onClick={testFutu}>测试连接</Button>
            <Text type="secondary" style={{ fontSize: 11.5 }}>
              保存：先本地校验再发请求，服务端校验通过才落盘；测试：用已保存凭据发一次真实 trading-days 请求。
            </Text>
          </Space>

          <div style={{ marginTop: 12 }}>
            <Space size={10} wrap align="center">
              <Text style={{ fontSize: 12 }}>Client ID（可选）</Text>
              <Input
                style={{ width: 320 }}
                value={clientId}
                placeholder="留空 = 自动注册 public client（PKCE required）"
                autoComplete="off"
                onChange={(event) => setClientId(event.target.value)}
              />
              <Button size="small" loading={oauth.phase === "starting"} onClick={startOAuth}>开始 OAuth 授权</Button>
              <Button size="small" danger onClick={cancelOAuth}>取消授权</Button>
            </Space>
            <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 6, marginBottom: 0 }}>
              流程：注册 client（如需）→ 服务端在本机起 127.0.0.1 回调监听 → 在浏览器完成富途授权 → 服务端校验 state 后换取
              token 并落盘（0600）。本页不接触任何 token。
            </Paragraph>
            <Space direction="vertical" size={6} style={{ width: "100%", marginTop: 8 }}>
              <Space size={8} wrap>
                <Tag color={oauthBadge.color}>{oauthBadge.text}</Tag>
                <Text type="secondary" style={{ fontSize: 11.5 }}>
                  {`轮询：每 2 秒 POST /api/wb/openapi_oauth {action:'status'}（done/error 即停；无 pending 也停）· 状态 ${fmt.dash(oauthValue.pending === undefined ? undefined : String(oauthValue.pending))} · done=${fmt.dash(oauthValue.done === undefined ? undefined : String(oauthValue.done))}`}
                </Text>
              </Space>
              {oauthValue.auth_url ? (
                <Space size={8} wrap>
                  <Text style={{ fontSize: 12 }}>授权 URL（PKCE + S256）</Text>
                  <Link href={oauthValue.auth_url} target="_blank" rel="noreferrer">打开富途授权页</Link>
                  <Text copyable={{ text: oauthValue.auth_url }} style={{ fontSize: 11 }} />
                </Space>
              ) : null}
              {oauthValue.auth_url ? (
                <Text type="secondary" style={{ fontSize: 11.5, wordBreak: "break-all" }}>
                  {`scope：${fmt.dash(oauthValue.scope)} · 回调地址：http://localhost:${fmt.dash(oauthValue.callback_port)}/callback · 授权链接约 10 分钟内有效`}
                </Text>
              ) : null}
              {oauth.error ? (
                <Text style={{ color: "#f8514d", fontSize: 12 }}>
                  {`错误：${oauth.error}（令牌类字段一律不显示，凭据只由服务端落盘）`}
                </Text>
              ) : null}
            </Space>
          </div>

          <div style={{ marginTop: 10 }}>
            <Msg message={futuMsg} />
            {openapi.error ? (
              <div style={{ marginTop: 8 }}>
                <NoSource what="OpenAPI 配置状态" why={openapi.error} />
              </div>
            ) : null}
            {openapi.value?.last_error ? (
              <div style={{ marginTop: 8 }}>
                <Alert type="error" showIcon message={`凭据文件读取失败：${String(openapi.value.last_error)}`} />
              </div>
            ) : null}
          </div>
        </div>
      </ProCard>

      {/* 4. 统一授权中心（全局口径：凭据按账号 / 环境注入，与市场无关） */}
      <ProCard
        title="统一授权中心"
        bordered
        extra={
          <Space size={8} wrap>
            <ScopeTag market={market} label={label} global />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {`真实凭据状态 · ${credKeys.length} 项（/api/v3/credentials）· 只显示是否注入与掩码尾号`}
            </Text>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          口径：统一授权中心（Tushare token 等）为全局口径（不按市场拆分）——凭据按账号 / 环境注入，同一份凭据服务全部市场。
        </Text>
        {credentials.error ? (
          <NoSource what="凭据状态" why={`/api/v3/credentials 取数失败：${credentials.error}`} type="error" />
        ) : credKeys.length === 0 ? (
          <Empty image={EMPTY_FRAME} description={noSourceText("凭据状态", "接口返回空清单")} />
        ) : (
          <Table
            size="small"
            rowKey="key"
            dataSource={credKeys}
            pagination={false}
            locale={emptyTable("凭据状态", "接口返回空清单")}
            columns={[
              { title: "凭据", render: (_, item) => <Space size={6}><Text>{item.label}</Text><Text type="secondary" style={{ fontSize: 11 }}>{fmt.dash(item.usage)}</Text></Space> },
              { title: "环境变量", width: 170, render: (_, item) => <Text code>{fmt.dash(item.env)}</Text> },
              { title: "是否已配置", width: 110, render: (_, item) => <Tag color={item.present ? "success" : "warning"}>{item.present ? "已配置" : "未配置"}</Tag> },
              { title: "来源", width: 190, render: (_, item) => fmt.dash(item.source) },
              { title: "更新时间", width: 170, render: (_, item) => (item.updated_at ? fmt.stamp(item.updated_at) : "—") },
              { title: "掩码尾号", width: 110, render: (_, item) => fmt.dash(item.hint) },
            ]}
          />
        )}
        <div style={{ marginTop: 10, display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          <Text type="secondary" style={{ fontSize: 12 }}>{`${firstKey?.label ?? "凭据"}：`}</Text>
          <Input.Password
            style={{ minWidth: 300 }}
            value={tokenValue}
            autoComplete="new-password"
            placeholder="粘贴 token（保存后不再回显）"
            onChange={(event) => setTokenValue(event.target.value)}
          />
          <Button size="small" loading={credBusy === "save"} onClick={() => credentialAction("save")}>保存</Button>
          <Button size="small" loading={credBusy === "test"} onClick={() => credentialAction("test")}>测试连通性</Button>
          {firstKey?.present ? (
            <Button size="small" danger loading={credBusy === "clear"} onClick={() => setClearOpen(true)}>清除页面配置</Button>
          ) : null}
          <Msg message={credMsg} />
        </div>
        <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 8, marginBottom: 0 }}>
          本页只显示「是否配置 / 来源 / 更新时间 / 掩码尾号」，任何位置都不显示密钥值；
          写入经 POST /api/v3/credentials（save / test / clear），保存后输入框立即清空。
          其余凭据（富途 OpenAPI、DeepSeek BYOK、MCP 凭据）不由该接口托管：富途授权见上方区块，环境变量注入状态见下表。
        </Paragraph>
      </ProCard>

      {/* 4.5 数据源与降级链（只读 GET /api/v3/sources/status）
          有意不加 loading：该端点要对 8 条链各做一次真实只读探测（实测 20–60s），
          卡片结构（统计位 / 表头 / 口径说明）必须立刻可见，探测中显式写明「探测中」。 */}
      <ProCard
        title="数据源与降级链"
        bordered
        extra={
          <Space size={8} wrap>
            <ScopeTag market={market} label={label} global />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {chainPayload ? `真实链状态 · ${chainRows.length} 条 · as_of ${fmt.stamp(chainPayload.as_of)}` : "GET /api/v3/sources/status · 只读"}
            </Text>
            <Button size="small" loading={sourcesStatus.loading} onClick={() => sourcesStatus.refresh()}>刷新</Button>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          口径：降级链为全局链路（不按市场拆分）——主源 / 降级源对各市场共用，本卡不按市场重取。
        </Text>
        {chainRows.length > 0 ? (
          <Space size={32} wrap style={{ marginBottom: 12 }}>
            <Statistic title="可用链 / 总链数" value={`${chainAvailable} / ${chainRows.length}`} valueStyle={{ fontSize: 18 }} />
            <Statistic title="其中有降级源的链" value={chainWithFallback} suffix={`/ ${chainRows.length}`} valueStyle={{ fontSize: 18 }} />
            <Statistic title="最近检查时间" value={fmt.stamp(chainPayload.checked_at ?? chainPayload.as_of)} valueStyle={{ fontSize: 18 }} />
          </Space>
        ) : sourcesStatus.loading ? (
          <div style={{ marginBottom: 12 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              正在探测各链（每链按主源 → 降级源各做一次真实只读 GET，实测约 20–60 秒；本卡不发任何写请求）…
            </Text>
          </div>
        ) : (
          <div style={{ marginBottom: 12 }}>
            <NoSource what="数据源降级链" why={chainReason} />
          </div>
        )}
        <Table
          size="small"
          rowKey={(row, index) => String(row?.key ?? index)}
          dataSource={chainRows}
          pagination={false}
          locale={emptyTable(
            "数据源降级链",
            sourcesStatus.loading ? "探测中（每链一次真实只读 GET，实测约 20–60 秒）" : chainReason,
          )}
          scroll={{ x: 1400 }}
          columns={[
            {
              title: "数据源",
              width: 220,
              render: (_, row) => (
                <Space size={6}>
                  <Text style={{ fontSize: 12 }}>{fmt.dash(row.label || row.key)}</Text>
                  <Text code style={{ fontSize: 11 }}>{fmt.dash(row.key)}</Text>
                </Space>
              ),
            },
            {
              title: "主源",
              width: 250,
              render: (_, row) => <Text code style={{ fontSize: 11 }}>{fmt.dash(row.primary)}</Text>,
            },
            {
              title: "降级源",
              width: 250,
              render: (_, row) =>
                typeof row.fallback === "string" && row.fallback.trim() !== "" ? (
                  <Text code style={{ fontSize: 11 }}>{row.fallback}</Text>
                ) : (
                  <Text type="secondary" style={{ fontSize: 11 }}>无降级源（该链无开源替代）</Text>
                ),
            },
            {
              title: "当前可用",
              width: 110,
              render: (_, row) => (
                <Tag color={row.available === true ? "success" : row.available === false ? "error" : "default"}>
                  {row.available === true ? "可用" : row.available === false ? "不可用" : "未返回"}
                </Tag>
              ),
            },
            {
              title: "最近来源（last_source · 最近实际命中）",
              width: 280,
              render: (_, row) => (
                <Space size={6}>
                  {row.last_source ? (
                    <Text code style={{ fontSize: 11 }}>{String(row.last_source)}</Text>
                  ) : (
                    <Text type="secondary">—</Text>
                  )}
                  <Tag color={row.last_ok === true ? "success" : row.last_ok === false ? "error" : "default"}>
                    {row.last_ok === true ? "成功" : row.last_ok === false ? "失败" : "未返回"}
                  </Tag>
                </Space>
              ),
            },
            {
              title: "检查时间",
              width: 170,
              render: (_, row) => <span className="num">{row.checked_at ? fmt.stamp(row.checked_at) : "—"}</span>,
            },
            {
              title: "错误",
              render: (_, row) => {
                const text = rawErrorText(row.error);
                return text ? (
                  <Text style={{ color: "#f8514d", fontSize: 11.5, wordBreak: "break-all" }}>{text}</Text>
                ) : (
                  <Text type="secondary">—</Text>
                );
              },
            },
          ]}
        />
        <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 8, marginBottom: 0 }}>
          {`降级口径：优先富途（主源）；富途不可用时按该链的 fallback 降级到开源源（AKShare 等），每条数据都带 source 标注，可在行情 / 研究页核对。
            两个源都失败时如实返回错误原文，不返回占位数据。成交质量类没有开源替代，降级源列为空时即为「无降级源」。
            本卡只读展示，页面不提供任何降级开关；来源 GET /api/v3/sources/status。`}
          {chainPayload && chainPayload.note ? ` 服务端说明：${String(chainPayload.note)}` : ""}
        </Paragraph>
      </ProCard>

      {/* 5. 环境变量与密钥来源（全局口径） */}
      <ProCard
        title="环境变量与密钥来源"
        bordered
        extra={
          <Space size={8} wrap>
            <ScopeTag market={market} label={label} global />
            <Tag color={injectedMissing > 0 ? "warning" : "success"}>{`${injectedMissing} 项未注入 / 共 ${envRows.length} 项`}</Tag>
          </Space>
        }
      >
        {envRows.length === 0 ? (
          <Empty image={EMPTY_FRAME} description={noSourceText("环境变量清单", "/api/v3/settings 未返回 env")} />
        ) : (
          <Table
            size="small"
            rowKey="key"
            dataSource={envRows}
            pagination={false}
            locale={emptyTable("环境变量清单", "/api/v3/settings 未返回 env")}
            columns={[
              { title: "变量名", width: 220, render: (_, item) => <Text code>{item.key}</Text> },
              { title: "用途", render: (_, item) => PURPOSES[item.key] ?? "（工具面未提供用途说明）" },
              { title: "是否已注入", width: 120, render: (_, item) => <Tag color={item.injected ? "default" : "warning"}>{item.injected ? "已注入" : "未注入"}</Tag> },
              { title: "来源", width: 190, render: (_, item) => fmt.dash(item.source) },
            ]}
          />
        )}
        <Paragraph type="secondary" style={{ fontSize: 11.5, marginTop: 8, marginBottom: 0 }}>
          「未注入」指该变量尚未进入 Harness 运行时环境；本表只显示注入状态与来源，不显示任何变量值。
          其中 TUSHARE_TOKEN 的已配置状态见上方「统一授权中心」。本表为全局口径（不按市场拆分）。
        </Paragraph>
      </ProCard>

      {/* 6. 授权与审计策略（全局：风控阈值对所有市场统一） */}
      <ProCard
        title="授权与审计策略"
        bordered
        extra={
          <Space size={8} wrap>
            <ScopeTag market={market} label={label} global />
            <Text type="secondary" style={{ fontSize: 12 }}>只读展示 · 由风控引擎统一下发（本页不提供写入）</Text>
          </Space>
        }
      >
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(260px, 1fr))", gap: 12 }}>
          <Alert
            type="info"
            showIcon
            message="自动执行 · 阈值内"
            description={`单笔 ≤ 2% · 行业 ≤ 20% · 回撤 ≤ 15%（v3_ops.LIMITS），当前模式 ${String(mode || "—").toUpperCase()}`}
          />
          <Alert
            type="warning"
            showIcon
            message="人工确认 · 超阈值"
            description="进入审批队列（待审批笔数见「执行与审批」页）；LIVE 下所有执行均需人工审批与口令"
          />
          <Alert
            type="error"
            showIcon
            message="强制阻断 · 红线"
            description="触发红线规则立即拒绝并推送告警，记录完整审计链（权威来源 /api/v3/audit）"
          />
        </div>
        <Descriptions
          size="small"
          column={2}
          style={{ marginTop: 12 }}
          items={[
            { key: "downgrade", label: "只读降级策略", children: "凭据过期或上游不可用时自动降级为只读（禁止下单），不影响其余能力" },
            { key: "level", label: "审计日志级别", children: <Tag color="blue">审计链完整（/api/v3/audit）</Tag> },
            { key: "retention", label: "审计保留期", children: <Text type="secondary">无数据源（工具面不返回保留期策略）</Text> },
            { key: "rotate", label: "审批口令轮换时间", children: <Text type="secondary">无数据源（轮换由工作台 Web 的口令闸门负责，时间不在工具面）</Text> },
            { key: "dual", label: "双人复核", children: "LIVE 大额订单需双人复核后执行（阈值由风控引擎统一下发，本页只读展示）" },
            { key: "ops", label: "策略开关", children: <Text type="secondary">无数据源（开关由风控引擎下发，工具面无写入通道）</Text> },
          ]}
        />
      </ProCard>

      {/* 7. 自动流水线（策略行带 market，按市场分别配置） */}
      <ProCard
        title="自动流水线"
        bordered
        extra={
          <Space size={8} wrap>
            <Tag color={auto?.error ? "error" : auto?.enabled ? "success" : "default"}>
              {auto?.error ? "配置非法" : auto?.enabled ? "自动执行已开启" : "自动执行关闭"}
            </Tag>
            <ScopeTag market={market} label={label} text={`本页当前市场：${label} ${market}（策略行仍可分别配置各市场）`} />
            <Text type="secondary" style={{ fontSize: 12 }}>POST /api/wb/auto_pipeline · 人工点击保存 · 校验复用调度侧同一实现</Text>
          </Space>
        }
      >
        {auto === null ? (
          <Text type="secondary">读取中…</Text>
        ) : (
          <Space direction="vertical" size={10} style={{ width: "100%" }}>
            <Text type="secondary" style={{ fontSize: 11, display: "block" }}>
              {`口径：自动流水线是全局配置，但每行策略自带 market 字段（SH / HK / US）——本页当前市场为 ${label} ${market}，
              策略行仍可分别配置各市场，本卡不按当前市场过滤或重写任何策略行。`}
            </Text>
            <Space size={10} wrap align="center">
              <Switch
                checked={auto.enabled}
                onChange={(checked) => patchAuto({ enabled: checked })}
              />
              <Text type="secondary" style={{ fontSize: 12.5 }}>
                启用自动流水线（仅模拟盘自动执行；实盘计划照常生成，仍需在「执行与审批」页人工确认）
              </Text>
            </Space>

            <Text type="secondary" style={{ fontSize: 12 }}>
              策略行：市场 + 策略 + 关注池键名。策略取值域由服务端校验（内置策略 id，或研究页「已批准（status=enabled）」的规则 id）——
              写成别的名字保存会被拒绝并列出合法取值。
            </Text>

            <datalist id="v3AutoStrategyList">
              {BUILTIN_STRATEGIES.map((value) => <option key={value} value={value} />)}
            </datalist>

            {auto.strategies.length === 0 ? (
              <Text type="secondary" style={{ fontSize: 12 }}>未配置策略：开启后流水线只跑数据与对账，不会生成计划。</Text>
            ) : auto.strategies.map((row, index) => (
              <Space key={`row-${index}`} size={8} wrap>
                <Select
                  size="small"
                  style={{ width: 150 }}
                  value={row.market}
                  onChange={(value) => patchStrategy(index, { market: value })}
                  options={MARKETS.map(([value, labelText]) => ({ value, label: `${value} · ${labelText}` }))}
                />
                {row.market === market ? <Tag color="blue" style={{ marginInlineEnd: 0 }}>本页当前市场</Tag> : null}
                <Input
                  size="small"
                  style={{ width: 280 }}
                  list="v3AutoStrategyList"
                  value={row.strategy}
                  placeholder="策略 id（内置策略或已批准的规则 id）"
                  onChange={(event) => patchStrategy(index, { strategy: event.target.value })}
                />
                <Input
                  size="small"
                  style={{ width: 180 }}
                  value={row.watchlist}
                  placeholder={POOL_KEY_DEFAULT}
                  onChange={(event) => patchStrategy(index, { watchlist: event.target.value })}
                />
                <Button
                  size="small"
                  danger
                  type="text"
                  onClick={() => patchAuto({ strategies: auto.strategies.filter((_, i) => i !== index) })}
                >
                  删除
                </Button>
              </Space>
            ))}

            <Button
              size="small"
              onClick={() => patchAuto({
                strategies: [...auto.strategies, { market: "SH", strategy: BUILTIN_STRATEGIES[0], watchlist: POOL_KEY_DEFAULT }],
              })}
            >
              添加策略
            </Button>

            <Space size={12} wrap align="flex-end">
              {MARKETS.map(([marketKey, marketText]) => (
                <div key={marketKey}>
                  <Text style={{ fontSize: 12 }}>{`执行时刻 · ${marketText}`}</Text>
                  {marketKey === market ? <Tag color="blue" style={{ marginInlineStart: 6 }}>本页当前市场</Tag> : null}
                  <br />
                  <Input
                    size="small"
                    style={{ width: 110 }}
                    maxLength={5}
                    value={auto.exec_at?.[marketKey] ?? ""}
                    placeholder="HH:MM"
                    onChange={(event) => patchAuto({ exec_at: { ...auto.exec_at, [marketKey]: event.target.value } })}
                  />
                </div>
              ))}
              <div>
                <Text style={{ fontSize: 12 }}>{`执行窗口（分钟，1–${EXEC_WINDOW_MAX}）`}</Text>
                <br />
                <InputNumber
                  size="small"
                  style={{ width: 140 }}
                  min={1}
                  max={EXEC_WINDOW_MAX}
                  value={auto.exec_window_minutes}
                  onChange={(value) => patchAuto({ exec_window_minutes: value })}
                />
              </div>
              <div>
                <Text style={{ fontSize: 12 }}>晚间对账时刻</Text>
                <br />
                <Input
                  size="small"
                  style={{ width: 110 }}
                  maxLength={5}
                  value={auto.reconcile_at ?? ""}
                  placeholder="HH:MM"
                  onChange={(event) => patchAuto({ reconcile_at: event.target.value })}
                />
              </div>
            </Space>

            <Space size={8} wrap>
              <Button type="primary" size="small" loading={autoSaving} onClick={saveAuto}>保存自动流水线</Button>
              <Text type="secondary" style={{ fontSize: 11.5 }}>
                保存 = 先本地校验格式（HH:MM 与 1–{EXEC_WINDOW_MAX} 分钟），再由服务端校验取值并原子写；失败则文件零改动。
              </Text>
            </Space>
            <Msg message={autoMsg} />
          </Space>
        )}
      </ProCard>

      <ProCard bordered>
        <Text type="secondary" style={{ fontSize: 11.5 }}>
          {`数据来源：/api/v3/settings · /api/v3/credentials · /api/v3/metrics · /api/v3/audit · /api/v3/sources/status；工具 ${fmt.dash(metrics.value?.toolTotal)} 个 · 数据截至 ${fmt.stamp(metrics.value?.generated_at)}；凭据未配置 ${unconfigured} 项（密钥值不在任何位置展示）；本页当前市场 ${label} ${market}（仅自动流水线策略行按市场分别配置，其余为全局口径）。`}
        </Text>
      </ProCard>

      <Modal
        open={confirmOpen}
        title={`二次确认 · 切换到${effectiveTarget === "live" ? "实盘 LIVE" : "模拟盘 SIM"}`}
        okText={effectiveTarget === "live" ? "确认切换到实盘" : "确认切回模拟盘"}
        cancelText="取消"
        okButtonProps={{ danger: effectiveTarget === "live", disabled: effectiveTarget === "live" && !ack }}
        onCancel={() => { setConfirmOpen(false); setAck(false); }}
        onOk={confirmSwitch}
        destroyOnClose
      >
        <Paragraph style={{ fontSize: 12.5 }}>
          {effectiveTarget === "live"
            ? `即将把交易模式由 ${modeLabel(mode)} 切换为 LIVE。切换后订单将经富途 OpenAPI 真实报出，本操作写入审计日志。切换模式不等于下单授权：LIVE 下每笔执行仍需在「执行与审批」页人工审批。`
            : `即将把交易模式由 ${modeLabel(mode)} 切换为 SIM。切换后订单只进入本地模拟账本，本操作写入审计日志；LIVE 下已报出的订单不受影响，需在执行页人工处理。`}
        </Paragraph>
        {effectiveTarget === "live" ? (
          <Space size={8} align="center">
            <Switch checked={ack} onChange={setAck} size="small" />
            <Text style={{ fontSize: 12 }}>我已确认风险，并理解切换不等于下单授权</Text>
          </Space>
        ) : null}
      </Modal>

      <Modal
        open={clearOpen}
        title="清除页面配置的 Tushare token"
        okText="确认清除"
        cancelText="取消"
        okButtonProps={{ danger: true }}
        onCancel={() => setClearOpen(false)}
        onOk={async () => { setClearOpen(false); await credentialAction("clear"); }}
        destroyOnClose
      >
        <Paragraph style={{ fontSize: 12.5 }}>
          将删除凭据文件里的该项配置（环境变量不受影响）。该动作只在你确认后发起一次请求。
        </Paragraph>
      </Modal>
    </Space>
  );
}
