import React from "react";
import { createRoot } from "react-dom/client";
import { App as AntApp, ConfigProvider, Input, Modal, Radio, Select, Space, Tag, Typography, theme } from "antd";
import { ProLayout } from "@ant-design/pro-components";
import zhCN from "antd/locale/zh_CN";
import { callApi } from "./services/api.js";
import { LIVE_CONFIRMATION, modeBadge, switchModeRequest } from "./services/mode.js";
import { useEndpoint, useSnapshotPoll } from "./services/hooks.js";
import { MARKET_CHOICES } from "./services/marketFilter.js";
import { MarketFilterProvider, useMarketFilter } from "./services/marketContext.jsx";
import OverviewPage from "./pages/overview.jsx";
import MarketPage from "./pages/market.jsx";
import CapitalPage from "./pages/capital.jsx";
import OptionsPage from "./pages/options.jsx";
import SignalPage from "./pages/signal.jsx";
import PortfolioPage from "./pages/portfolio.jsx";
import RiskPage from "./pages/risk.jsx";
import FactorsPage from "./pages/factors.jsx";
import ExecutionPage from "./pages/execution.jsx";
import ResearchPage from "./pages/research.jsx";
import EventsPage from "./pages/events.jsx";
import PlanPage from "./pages/plan.jsx";
import PipelinePage from "./pages/pipeline.jsx";
import SchedulePage from "./pages/schedule.jsx";
import AuditPage from "./pages/audit.jsx";
import SettingsPage from "./pages/settings.jsx";
// V3 控制台（Ant Design Pro）：9 页对应 OpenDesign 设计稿 od-quant-harness-platform/*
import V3OverviewPage from "./pages/v3/overview.jsx";
import V3BrainPage from "./pages/v3/brain.jsx";
import V3MarketPage from "./pages/v3/market.jsx";
import V3StrategyPage from "./pages/v3/strategy.jsx";
import V3RiskPage from "./pages/v3/risk.jsx";
import V3ExecutionPage from "./pages/v3/execution.jsx";
import V3GatewayPage from "./pages/v3/gateway.jsx";
import V3ToolsPage from "./pages/v3/tools.jsx";
import V3SettingsPage from "./pages/v3/settings.jsx";
import { V3_THEME } from "./pages/v3/theme.js";

const PAGES = [
  { key: "overview", name: "概览", element: <OverviewPage /> },
  { key: "market", name: "行情", element: <MarketPage /> },
  { key: "capital", name: "资金", element: <CapitalPage /> },
  { key: "options", name: "期权", element: <OptionsPage /> },
  { key: "signal", name: "信号", element: <SignalPage /> },
  { key: "portfolio", name: "组合", element: <PortfolioPage /> },
  { key: "risk", name: "风险", element: <RiskPage /> },
  { key: "factors", name: "因子", element: <FactorsPage /> },
  { key: "execution", name: "执行", element: <ExecutionPage /> },
  { key: "research", name: "研究", element: <ResearchPage /> },
  { key: "events", name: "事件", element: <EventsPage /> },
  { key: "plan", name: "计划", element: <PlanPage /> },
  // WP10 任务 3：流程页（当日闭环阶段链）；放在「调度」之前——先看闭环跑到哪一步，
  // 再看调度作业明细。
  { key: "pipeline", name: "流程", element: <PipelinePage /> },
  { key: "schedule", name: "调度", element: <SchedulePage /> },
  { key: "audit", name: "审计", element: <AuditPage /> },
  // WP8 任务 7：设置页收尾（富途 OpenAPI 凭据配置：保存/测试/状态）
  { key: "settings", name: "设置", element: <SettingsPage /> },
];

// V3 控制台页面（顺序 = 设计稿导航：监控 → 研究 → 交易 → 系统）
const V3_PAGES = [
  { key: "v3-overview", name: "系统概览", group: "监控", element: <V3OverviewPage /> },
  { key: "v3-brain", name: "决策大脑", group: "监控", element: <V3BrainPage /> },
  { key: "v3-market", name: "行情与信号", group: "研究", element: <V3MarketPage /> },
  { key: "v3-strategy", name: "策略与因子", group: "研究", element: <V3StrategyPage /> },
  { key: "v3-risk", name: "风险监控", group: "交易", element: <V3RiskPage /> },
  { key: "v3-execution", name: "执行与审批", group: "交易", element: <V3ExecutionPage /> },
  { key: "v3-gateway", name: "网关与调度", group: "系统", element: <V3GatewayPage /> },
  { key: "v3-tools", name: "工具域治理", group: "系统", element: <V3ToolsPage /> },
  { key: "v3-settings", name: "接入与授权", group: "系统", element: <V3SettingsPage /> },
];
const V3_GROUPS = ["监控", "研究", "交易", "系统"];
const ALL_PAGES = [...V3_PAGES, ...PAGES];

function currentKey() {
  return (location.hash.replace(/^#\//, "").split("?")[0]) || "overview";
}

/** 页头模式切换入口：徽章可点开，sim→live 需逐字口令；服务端仍独立复核一遍。
 *  MCP 通道的 switch_mode 只接受切到 sim，live 只能由用户在本入口（过渡期还有
 *  legacy 面板）手工切换；请求带 expected_mode，成功后提示订单未获授权。 */
function ModeButton({ mode, onSwitched }) {
  const { message } = AntApp.useApp();
  const [open, setOpen] = React.useState(false);
  const [target, setTarget] = React.useState(mode);
  const [confirmation, setConfirmation] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const badge = modeBadge(mode);
  const needConfirmation = target === "live" && mode !== "live";

  const openModal = () => { setTarget(mode); setConfirmation(""); setOpen(true); };
  const closeModal = () => { if (!busy) setOpen(false); };

  const submit = async () => {
    let payload;
    try {
      payload = switchModeRequest({ target, current: mode, confirmation });
    } catch (error) {
      message.error(error.message || String(error));
      return;
    }
    setBusy(true);
    try {
      const value = await callApi("switch-mode", payload);
      const text = modeBadge(value?.mode ?? target).text;
      message.success(`已切换为 ${text}；order_authorized: ${value?.order_authorized ?? false}`);
      setOpen(false);
      onSwitched();
    } catch (error) {
      message.error(`切换失败：${error.message || error}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Tag color={badge.color} style={{ cursor: "pointer" }} onClick={openModal}
        role="button" tabIndex={0} aria-label={`账户模式：${badge.text}`}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openModal(); }
        }}>{badge.text}</Tag>
      <Modal title="账户模式" open={open} onCancel={closeModal} onOk={submit}
        okText="确认切换" cancelText="取消" confirmLoading={busy}>
        <Space direction="vertical" size="middle" style={{ width: "100%" }}>
          <Radio.Group value={target} disabled={busy}
            aria-label="目标模式"
            onChange={(event) => { setTarget(event.target.value); setConfirmation(""); }}
            options={[
              { label: "模拟 SIM", value: "sim" },
              { label: "实盘 LIVE", value: "live" },
            ]} />
          {needConfirmation && (
            <Input value={confirmation} autoComplete="off" disabled={busy}
              aria-label={LIVE_CONFIRMATION}
              placeholder={`输入：${LIVE_CONFIRMATION}`}
              onChange={(event) => setConfirmation(event.target.value)} />)}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            切换模式不等于授权下单；确认后显示 order_authorized=false
          </Typography.Text>
        </Space>
      </Modal>
    </>
  );
}

/** 页头推送状态指示：10 秒轮询 push_status（服务端与 /healthz 的 push 同一实现）。
 *  文案只有三类白名单：已连接 / 连接中 / 未启用；取数失败（旧服务无该端点）不显示，
 *  不猜状态、不把「未知」说成任何一类。 */
function PushBadge() {
  const status = useEndpoint("push_status", {}, []);
  React.useEffect(() => {
    const timer = setInterval(() => status.refresh(), 10_000);
    return () => clearInterval(timer);
  }, [status.refresh]);
  const push = status.value;
  if (status.error || !push) return null;
  if (!push.enabled) return <Tag>推送未启用</Tag>;
  const quote = push.quote ?? {};
  if (quote.connected && quote.authenticated) return <Tag color="green">实时推送已连接</Tag>;
  return <Tag color="blue">推送连接中</Tag>;
}

function Shell() {
  const [key, setKey] = React.useState(currentKey());
  React.useEffect(() => {
    const onHash = () => setKey(currentKey());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const snapshot = useSnapshotPoll();
  const mode = snapshot.value?.mode ?? "sim";
  const { market, setMarket } = useMarketFilter();
  const page = ALL_PAGES.find((item) => item.key === key) ?? V3_PAGES[0];
  return (
    <ProLayout title="量化决策平台 V3" layout="mix" fixSiderbar
      route={{ path: "/", routes: [
        { path: "/v3", name: "V3 控制台",
          routes: V3_GROUPS.flatMap((group) =>
            V3_PAGES.filter((p) => p.group === group).map((p) => ({ path: `/${p.key}`, name: `${group} · ${p.name}` }))) },
        { path: "/legacy", name: "既有工作台",
          routes: PAGES.map(({ key: k, name }) => ({ path: `/${k}`, name })) },
      ] }}
      location={{ pathname: `/${page.key}` }}
      menuItemRender={(item, dom) => (
        <a href={`#${item.path}`} onClick={() => setKey(item.path.slice(1))}>{dom}</a>)}
      avatarProps={{ render: () => (
        <Space size="small">
          {/* 全局市场维度（2026-09-18）：页面按它过滤/分段；选择持久化在 localStorage。
              放在页头而不是各页卡片里，是为了「切页不丢、一眼看到当前视角」。 */}
          <Select size="small" style={{ width: 118 }} value={market}
            aria-label="市场筛选" options={MARKET_CHOICES}
            onChange={(value) => setMarket(value)} />
          <ModeButton mode={mode} onSwitched={() => snapshot.refresh()} />
          <PushBadge />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            数据按 TTL 本地缓存；模式切换不授权下单
          </Typography.Text>
        </Space>) }}>
      {page.key.startsWith("v3-")
        // V3 页面套设计稿 token（作用域化：既有页面保持 antd 默认主题）
        ? <ConfigProvider locale={zhCN} theme={V3_THEME}>{page.element}</ConfigProvider>
        : page.element}
    </ProLayout>
  );
}

createRoot(document.getElementById("root")).render(
  // 暗色主题：算法切 dark；canvas 图表的暗色配色由 charts/theme.js 读
  // <html data-theme="dark">（index.html 声明）分支给出，两处同一事实源。
  <ConfigProvider locale={zhCN} theme={{ algorithm: theme.darkAlgorithm }}>
    <AntApp>
      <MarketFilterProvider>
        <Shell />
      </MarketFilterProvider>
    </AntApp>
  </ConfigProvider>);
