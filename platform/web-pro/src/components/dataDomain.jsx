// 数据域模块（工作台共用）：把后端「外部数据源」端点全部对到界面上——
//   * news      AKShare 个股资讯（免密钥）
//   * financials SEC EDGAR 美股三表（公开 XBRL，含 latestEnd/ageDays/stale 新鲜度）
//   * tushare   A 股财务/行情（需要 TUSHARE_TOKEN：未配置时后端返回 tushare/no-token，如实展示）
//   * openbb    美股基本面（可选依赖；未安装时 openbb/unavailable；冷启动较慢）
//   * spot      A 股全市场快照（上游可能不可达 → 如实展示错误原文）
// 全部**按需加载**（点击按钮才请求）：这些是外部慢接口，不应在页面加载时阻塞首屏。
// 取不到一律显示「无数据源 + 真实错误原文」，绝不编造。
import React from "react";
import { Alert, Button, Descriptions, Input, Segmented, Space, Table, Tag, Typography } from "antd";
import { ProCard } from "@ant-design/pro-components";
import { getV3 } from "../services/api.js";

const { Text } = Typography;

function useOnDemand() {
  const [state, setState] = React.useState({ loading: false, value: null, error: null });
  const load = React.useCallback(async (path, params) => {
    setState({ loading: true, value: null, error: null });
    try {
      const body = await getV3(path, params ?? {});
      if (body && body.ok === false) {
        setState({ loading: false, value: null, error: body.error ?? { code: "unknown", message: "接口返回 ok=false" } });
        return;
      }
      setState({ loading: false, value: body, error: null });
    } catch (error) {
      setState({ loading: false, value: null, error: { code: "net", message: String(error.message || error) } });
    }
  }, []);
  return { ...state, load };
}

function Result({ state, children, empty }) {
  if (state.loading) return <Text type="secondary">加载中…（外部接口，可能较慢）</Text>;
  if (state.error) {
    return (
      <Alert type="warning" showIcon
        message={`无数据源 · ${state.error.code ?? "错误"}`}
        description={String(state.error.message ?? "").slice(0, 300)} />
    );
  }
  if (!state.value) return <Text type="secondary">{empty ?? "未加载（点击右侧按钮按需拉取）"}</Text>;
  return children(state.value);
}

/** 个股资讯（AKShare） */
function NewsBlock() {
  const state = useOnDemand();
  const [symbol, setSymbol] = React.useState("600519");
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Space size={8} wrap>
        <Input size="small" style={{ width: 140 }} value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="A 股代码，如 600519" />
        <Button size="small" loading={state.loading} onClick={() => state.load("news", { symbol, limit: 8 })}>拉取资讯</Button>
        {state.value?.source ? <Tag>{state.value.source}</Tag> : null}
        {state.value?.as_of ? <Text type="secondary" style={{ fontSize: 11 }}>as_of {String(state.value.as_of).slice(0, 19)}</Text> : null}
      </Space>
      <Result state={state} empty="未加载（AKShare 个股资讯，免密钥）">
        {(value) => (
          <Table size="small" rowKey={(row) => row.url || row.title} pagination={false}
            dataSource={value.rows ?? []} locale={{ emptyText: "该标的暂无资讯" }}
            columns={[
              { title: "标题", dataIndex: "title", ellipsis: true },
              { title: "来源", dataIndex: "source", width: 110 },
              { title: "发布时间", dataIndex: "published_at", width: 160 },
            ]} />
        )}
      </Result>
    </Space>
  );
}

/** SEC EDGAR 美股三表 */
function FinancialsBlock() {
  const state = useOnDemand();
  const [ticker, setTicker] = React.useState("AAPL");
  const [statement, setStatement] = React.useState("income");
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Space size={8} wrap>
        <Input size="small" style={{ width: 110 }} value={ticker} onChange={(event) => setTicker(event.target.value.toUpperCase())} />
        <Segmented size="small" value={statement} onChange={setStatement}
          options={[{ label: "利润表", value: "income" }, { label: "资产负债表", value: "balance" }, { label: "现金流量表", value: "cashflow" }]} />
        <Button size="small" loading={state.loading} onClick={() => state.load("financials", { ticker, statement, periods: 4 })}>拉取财报</Button>
        {state.value?.company ? <Tag>{state.value.company} · CIK {state.value.cik}</Tag> : null}
      </Space>
      <Result state={state} empty="未加载（SEC EDGAR 公开 XBRL；按 us-gaap 概念给最近若干期）">
        {(value) => (
          <Space direction="vertical" size={6} style={{ width: "100%" }}>
            <Table size="small" rowKey="tag" pagination={false}
              dataSource={value.lines ?? []} locale={{ emptyText: "该公司未申报任何目标概念" }}
              columns={[
                { title: "概念（us-gaap tag）", dataIndex: "tag", ellipsis: true },
                { title: "最新期末", dataIndex: "latestEnd", width: 110 },
                { title: "距今天数", dataIndex: "ageDays", width: 90, render: (v) => (v === null || v === undefined ? "—" : `${v} 天`) },
                { title: "新鲜度", dataIndex: "stale", width: 90,
                  render: (v) => (v ? <Tag color="orange">已停用口径</Tag> : <Tag color="green">当期</Tag>) },
                { title: "最近一期数值", width: 160,
                  render: (_, row) => {
                    const last = (row.points ?? []).at(-1);
                    return last ? `${Number(last.val).toLocaleString("en-US")}（${last.end} · ${last.form}）` : "—";
                  } },
              ]} />
            {Array.isArray(value.missing) && value.missing.length > 0 ? (
              <Text type="secondary" style={{ fontSize: 11 }}>
                未申报概念（接口 given missing，不当 0 处理）：{value.missing.map((item) => item.tag).join("、")}
              </Text>
            ) : null}
          </Space>
        )}
      </Result>
    </Space>
  );
}

/** Tushare A 股财务（token 门控） */
function TushareBlock() {
  const state = useOnDemand();
  const [code, setCode] = React.useState("600519.SH");
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Space size={8} wrap>
        <Input size="small" style={{ width: 150 }} value={code} onChange={(event) => setCode(event.target.value)} placeholder="如 600519.SH" />
        <Button size="small" loading={state.loading} onClick={() => state.load("tushare", { api: "income", ts_code: code })}>拉取 A 股利润表</Button>
        <Text type="secondary" style={{ fontSize: 11 }}>
          需要 TUSHARE_TOKEN：可在「接入与授权」页填写；未配置时后端不发请求，直接返回 no-token
        </Text>
      </Space>
      <Result state={state} empty="未加载（Tushare Pro，token 门控）">
        {(value) => (
          <Table size="small" pagination={false} dataSource={value.rows ?? []}
            rowKey={(row) => `${row.end_date ?? row.trade_date ?? ""}-${row.revenue ?? row.close ?? ""}`}
            locale={{ emptyText: "无返回行" }}
            columns={Object.keys((value.rows ?? [])[0] ?? {}).map((key) => ({ title: key, dataIndex: key }))} />
        )}
      </Result>
    </Space>
  );
}

/** OpenBB 美股基本面（可选依赖） */
function OpenBbBlock() {
  const state = useOnDemand();
  const [symbol, setSymbol] = React.useState("AAPL");
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Space size={8} wrap>
        <Input size="small" style={{ width: 110 }} value={symbol} onChange={(event) => setSymbol(event.target.value.toUpperCase())} />
        <Button size="small" loading={state.loading} onClick={() => state.load("openbb", { symbol })}>拉取基本面</Button>
        <Text type="secondary" style={{ fontSize: 11 }}>未安装 openbb 时返回 openbb/unavailable；冷启动可能约 1 分钟</Text>
      </Space>
      <Result state={state} empty="未加载（OpenBB 基本面指标）">
        {(value) => {
          const row = (value.rows ?? [])[0] ?? {};
          const entries = Object.entries(row).filter(([, v]) => v !== null && v !== undefined && v !== "");
          return (
            <Descriptions size="small" column={3} bordered
              items={entries.slice(0, 18).map(([key, v]) => ({ key, label: key, children: String(v) }))} />
          );
        }}
      </Result>
    </Space>
  );
}

/** A 股全市场快照（上游可能不可达） */
function SpotBlock() {
  const state = useOnDemand();
  return (
    <Space direction="vertical" size={8} style={{ width: "100%" }}>
      <Space size={8} wrap>
        <Button size="small" loading={state.loading} onClick={() => state.load("spot", { limit: 20 })}>拉取全市场快照</Button>
        <Text type="secondary" style={{ fontSize: 11 }}>上游为公开端点，实测常被上游断连（RemoteDisconnected）——失败即如实展示</Text>
      </Space>
      <Result state={state} empty="未加载（AKShare 全市场快照）">
        {(value) => (
          <Table size="small" pagination={false} dataSource={value.rows ?? []} rowKey={(row) => row.code}
            locale={{ emptyText: "无返回行" }}
            columns={[
              { title: "代码", dataIndex: "code", width: 100 },
              { title: "名称", dataIndex: "name", width: 120 },
              { title: "最新价", dataIndex: "price", width: 100 },
              { title: "涨跌幅", dataIndex: "change_pct", width: 100 },
              { title: "换手率", dataIndex: "turnover_rate", width: 100 },
              { title: "量比", dataIndex: "volume_ratio", width: 90 },
            ]} />
        )}
      </Result>
    </Space>
  );
}

export default function DataDomainCard() {
  return (
    <ProCard title="数据域（外部数据源 · 按需加载）" bordered
      extra={<Text type="secondary" style={{ fontSize: 11 }}>全部为真实接口：取不到即展示后端错误原文，不用占位数据</Text>}>
      <Space direction="vertical" size={14} style={{ width: "100%" }}>
        <div>
          <Text strong style={{ fontSize: 12 }}>① 个股资讯</Text>
          <NewsBlock />
        </div>
        <div>
          <Text strong style={{ fontSize: 12 }}>② SEC EDGAR 美股三表</Text>
          <FinancialsBlock />
        </div>
        <div>
          <Text strong style={{ fontSize: 12 }}>③ Tushare A 股财务</Text>
          <TushareBlock />
        </div>
        <div>
          <Text strong style={{ fontSize: 12 }}>④ OpenBB 美股基本面</Text>
          <OpenBbBlock />
        </div>
        <div>
          <Text strong style={{ fontSize: 12 }}>⑤ A 股全市场快照</Text>
          <SpotBlock />
        </div>
      </Space>
    </ProCard>
  );
}
