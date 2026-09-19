// V3 工作台（Ant Design Pro 版）外壳：菜单分组与设计稿一致（监控/研究/交易/系统），
// 9 个页面与 /v3/ 版**功能模块一一对应**；数据同样来自 /api/v3/* 与 /api/wb/*。
// 路由用 hash（#/<key>），因此不需要服务端 SPA 回退。
import React from "react";
import { ProLayout } from "@ant-design/pro-components";
import { Badge, Button, Space, Tooltip, Typography } from "antd";
import { useV3 } from "./services/api.js";
import OverviewPage from "./pages/overview.jsx";
import BrainPage from "./pages/brain.jsx";
import MarketPage from "./pages/market.jsx";
import StrategyPage from "./pages/strategy.jsx";
import RiskPage from "./pages/risk.jsx";
import ExecutionPage from "./pages/execution.jsx";
import GatewayPage from "./pages/gateway.jsx";
import ToolsPage from "./pages/tools.jsx";
import SettingsPage from "./pages/settings.jsx";

// 分组与顺序 = 设计稿左侧导航（/v3/*.html 的 .nav-group/.nav-cat）
const GROUPS = [
  { cat: "监控", items: [
    { key: "overview", name: "系统概览", element: <OverviewPage />, file: "index.html" },
    { key: "brain", name: "决策大脑", element: <BrainPage />, file: "brain.html" },
  ] },
  { cat: "研究", items: [
    { key: "market", name: "行情与信号", element: <MarketPage />, file: "market.html" },
    { key: "strategy", name: "策略与因子", element: <StrategyPage />, file: "strategy.html" },
  ] },
  { cat: "交易", items: [
    { key: "risk", name: "风险监控", element: <RiskPage />, file: "risk.html" },
    { key: "execution", name: "执行与审批", element: <ExecutionPage />, file: "execution.html" },
  ] },
  { cat: "系统", items: [
    { key: "gateway", name: "网关与调度", element: <GatewayPage />, file: "gateway.html" },
    { key: "tools", name: "工具域治理", element: <ToolsPage />, file: "tools.html" },
    { key: "settings", name: "接入与授权", element: <SettingsPage />, file: "settings.html" },
  ] },
];
const PAGES = GROUPS.flatMap((group) => group.items);

function currentKey() {
  return (window.location.hash.replace(/^#\/?/, "").split("?")[0]) || "overview";
}

/** 页头：模式徽章 + 数据时点 + 切到设计稿原样版的入口（两版并存，互相可跳） */
function HeaderExtra({ pageKey }) {
  const overview = useV3("overview", {});
  const mode = overview.value?.mode ?? null;
  const page = PAGES.find((item) => item.key === pageKey);
  return (
    <Space size="middle">
      <Tooltip title="数据来自本服务 /api/v3/*；模式以工作台为准">
        <Badge
          status={mode === "live" ? "error" : "processing"}
          text={mode ? (mode === "live" ? "LIVE 实盘" : "SIM 模拟盘") : "模式未知"}
        />
      </Tooltip>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {overview.value?.generated_at ? `数据时点 ${String(overview.value.generated_at).slice(0, 19)}` : "数据时点 —"}
      </Typography.Text>
      <Tooltip title="同一套数据与功能，设计稿原样版（HTML/CSS 逐字节一致）">
        <Button size="small" onClick={() => { if (page) window.location.href = `/v3/${page.file}`; }}>
          切到设计稿原样版
        </Button>
      </Tooltip>
    </Space>
  );
}

export default function Shell() {
  const [key, setKey] = React.useState(currentKey());
  React.useEffect(() => {
    const onHash = () => setKey(currentKey());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const page = PAGES.find((item) => item.key === key) ?? PAGES[0];
  return (
    <ProLayout
      title="量化决策平台 V3 · 工作台"
      layout="mix"
      fixSiderbar
      route={{
        path: "/",
        routes: GROUPS.map((group) => ({
          path: `/${group.cat}`,
          name: group.cat,
          routes: group.items.map((item) => ({ path: `/${item.key}`, name: item.name })),
        })),
      }}
      location={{ pathname: `/${page.key}` }}
      menuItemRender={(item, dom) => (
        <a
          href={`#/${String(item.path).replace(/^\//, "")}`}
          onClick={(event) => {
            event.preventDefault();
            const next = String(item.path).replace(/^\//, "");
            if (PAGES.some((p) => p.key === next)) {
              window.location.hash = `#/${next}`;
              setKey(next);
            }
          }}
        >
          {dom}
        </a>
      )}
      avatarProps={{ render: () => <HeaderExtra pageKey={page.key} /> }}
      footerRender={() => (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          数据来源：本服务 /api/v3/*（工作台工具面 / 富途行情 / 台账）· 取不到的项显式标注「无数据源」·
          执行与授权写动作仅经既有受约束入口
        </Typography.Text>
      )}
    >
      {page.element}
    </ProLayout>
  );
}

export { PAGES, GROUPS, currentKey };
