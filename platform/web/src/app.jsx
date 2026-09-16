import React from "react";
import { createRoot } from "react-dom/client";
import { App as AntApp, Button, ConfigProvider, Input, Modal, Radio, Space, Tag, Typography, theme } from "antd";
import { ProLayout } from "@ant-design/pro-components";
import zhCN from "antd/locale/zh_CN";
import { callApi, clearCache, getToken, setToken } from "./services/api.js";
import { LIVE_CONFIRMATION, modeBadge, switchModeRequest } from "./services/mode.js";
import { useEndpoint, useSnapshotPoll } from "./services/hooks.js";
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
import SchedulePage from "./pages/schedule.jsx";
import AuditPage from "./pages/audit.jsx";
import SettingsPage from "./pages/settings.jsx";

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
  { key: "schedule", name: "调度", element: <SchedulePage /> },
  { key: "audit", name: "审计", element: <AuditPage /> },
  // WP8 任务 7：设置页收尾（富途 OpenAPI 凭据配置：保存/测试/状态）
  { key: "settings", name: "设置", element: <SettingsPage /> },
];

function currentKey() {
  return (location.hash.replace(/^#\//, "").split("?")[0]) || "overview";
}

function TokenButton() {
  const [open, setOpen] = React.useState(false);
  const [value, setValue] = React.useState(getToken());
  return (
    <>
      <Button size="small" onClick={() => setOpen(true)}>令牌</Button>
      <Modal title="访问令牌" open={open} onCancel={() => setOpen(false)}
        onOk={() => { setToken(value.trim()); clearCache(); setOpen(false); location.reload(); }}
        okText="保存并刷新" cancelText="取消">
        <Input value={value} onChange={(event) => setValue(event.target.value)}
          placeholder="服务未配置 token 时留空" />
      </Modal>
    </>
  );
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
  const page = PAGES.find((item) => item.key === key) ?? PAGES[0];
  return (
    <ProLayout title="量化工作台" layout="mix" fixSiderbar
      route={{ path: "/", routes: PAGES.map(({ key: k, name }) => ({ path: `/${k}`, name })) }}
      location={{ pathname: `/${page.key}` }}
      menuItemRender={(item, dom) => (
        <a href={`#${item.path}`} onClick={() => setKey(item.path.slice(1))}>{dom}</a>)}
      avatarProps={{ render: () => (
        <Space size="small">
          <ModeButton mode={mode} onSwitched={() => snapshot.refresh()} />
          <PushBadge />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            数据按 TTL 本地缓存；模式切换不授权下单
          </Typography.Text>
          <TokenButton />
        </Space>) }}>
      {page.element}
    </ProLayout>
  );
}

createRoot(document.getElementById("root")).render(
  // 暗色主题：算法切 dark；canvas 图表的暗色配色由 charts/theme.js 读
  // <html data-theme="dark">（index.html 声明）分支给出，两处同一事实源。
  <ConfigProvider locale={zhCN} theme={{ algorithm: theme.darkAlgorithm }}>
    <AntApp><Shell /></AntApp>
  </ConfigProvider>);
