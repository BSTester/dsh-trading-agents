import React from "react";
import { createRoot } from "react-dom/client";
import { App as AntApp, Button, ConfigProvider, Input, Modal, Space, Tag, Typography, theme } from "antd";
import { ProLayout } from "@ant-design/pro-components";
import zhCN from "antd/locale/zh_CN";
import { clearCache, getToken, setToken } from "./services/api.js";
import { useSnapshotPoll } from "./services/hooks.js";
import MarketPage from "./pages/market.jsx";
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

const PAGES = [
  { key: "market", name: "行情", element: <MarketPage /> },
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
];

function currentKey() {
  return (location.hash.replace(/^#\//, "").split("?")[0]) || "market";
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
          <Tag color={mode === "live" ? "red" : "green"}>{mode === "live" ? "实盘 LIVE" : "模拟 SIM"}</Tag>
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            数据按 TTL 本地缓存；模式切换不授权下单
          </Typography.Text>
          <TokenButton />
        </Space>) }}>
      <AntApp>{page.element}</AntApp>
    </ProLayout>
  );
}

createRoot(document.getElementById("root")).render(
  <ConfigProvider locale={zhCN} theme={{ algorithm: theme.defaultAlgorithm }}>
    <Shell />
  </ConfigProvider>);
