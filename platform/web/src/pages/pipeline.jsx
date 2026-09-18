// 流程页：每市场一条当日作业链 + 全局晚间链（对账/摘要）+ 自动执行开关状态。
// 取值路径（页面每个字段都可指到源码行）：
//   pipeline 端点 → platform/server/app.py 的 pipeline 分支（空载荷只读，TTL 30 秒，
//     形状要求见 caches.py:89 ["date","markets","global","auto_pipeline"]）
//     → trading_core.pipeline.pipeline_snapshot（plugins/core/python/trading_core/pipeline.py:264）：
//     阶段集合来自 autopipeline.build_jobs 的**真实作业链**（作业缺席即阶段缺席，
//     服务端不凭空列阶段）+ 表驱动阶段 plan/execute/digest；
//     每阶段 {label, status, at, scheduled, summary}（_stage / _market_stages）；
//     auto_pipeline = 有效配置摘要（非法配置时含 error，端点本身仍 ok——_auto_pipeline_summary）；
//     kill = ~/.dsh/trading-kill 是否存在；halt = store.halt_summary（熔断，
//     store.py:487 返回 {"halted": bool, "halt_reason": str|None}）。
//   snapshot → 账户模式（页内只读展示；切换入口是页头模式徽章，本页不复制切换流程，
//     只复用 services/mode.js 的同一文案口径）。
// 展示规则：只渲染服务端返回的字段，缺的一律 —；状态/下钻/时间映射在
// services/pipeline.js（node --test 直测），本文件只做接线与布局。
import React from "react";
import { Alert, Button, Card, Space, Steps, Tag, Typography } from "antd";
import { TimelineChart } from "../charts/timeline.jsx";
import { useEndpoint } from "../services/hooks.js";
import { modeBadge } from "../services/mode.js";
import { configWarningRows, stageChainEmptyText } from "../services/fieldState.js";
import { useMarketFilter } from "../services/marketContext.jsx";
import { marketLabelOf } from "../services/marketView.js";
import { isAllMarkets } from "../services/marketFilter.js";
import { chartEmptyText } from "../services/portfolioCharts.js";
import { pipelineTimelineItems } from "../services/runtimeCharts.js";
import {
  MARKETS, autoPipelineBadge, marketLabel, stageDrill, stageEntries,
  stageStatus, stageStatusText, stageTagColor, stageTimeText,
} from "../services/pipeline.js";

/** 一条阶段链：节点标题用服务端 label，副标题用 at/scheduled，点击下钻来源页。 */
function StageChain({ stages, loading = false }) {
  const items = stageEntries(stages).map((stage) => ({
    title: <a href={stageDrill(stage.key)}>{stage.label ?? stage.key}</a>,
    subTitle: stageTimeText(stage),
    description: (
      <Space size={4} wrap>
        <Tag color={stageTagColor(stage.status)}>{stageStatusText(stage.status)}</Tag>
        {stage.summary ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {stage.summary}
          </Typography.Text>) : null}
      </Space>),
    status: stageStatus(stage.status),
  }));
  if (!items.length) {
    // 加载中不得断言「今日无该链阶段」（E2E 取证：加载窗口把未知说成事实）
    return <Typography.Text type="secondary">{stageChainEmptyText(loading)}</Typography.Text>;
  }
  // 阶段数随作业链增长：横向可滚动，避免挤压节点文案。
  return (
    <div style={{ overflowX: "auto" }}>
      <Steps size="small" labelPlacement="vertical" items={items} />
    </div>);
}

export default function PipelinePage() {
  const pipeline = useEndpoint("pipeline", {}, []);
  const snapshot = useEndpoint("snapshot", {}, []);
  const { market } = useMarketFilter();
  const value = pipeline.value ?? {};
  const stagesOf = (chain) => chain?.stages ?? {};
  const autoBadge = autoPipelineBadge(value.auto_pipeline);
  const mode = snapshot.value?.mode;
  const badge = modeBadge(mode);
  const killActive = value.kill === true;
  const halted = value.halt?.halted === true;
  // 全局市场筛选：本页本来就是「每市场一条链」的分段结构，故只决定渲染哪几张卡片
  // （不隐藏数据以外的东西）：选中具体市场只渲染那一张；「全部市场」渲染全部三张。
  // 全局（晚间链）与自动执行/kill/halt 状态没有市场维度，保持显示。
  const filtered = !isAllMarkets(market);
  const shownMarkets = filtered ? MARKETS.filter((item) => item === market) : MARKETS;
  // 时间轴跟随同一个市场筛选；未读到快照时传 null（函数据此说「尚未读取」，
  // 而不是把「还没读到」说成「没有阶段」）。
  const chain = pipelineTimelineItems(pipeline.value ?? null, { market });

  return (
    <Card title="流程" extra={(
      <Space size="small">
        <Button size="small" disabled={pipeline.loading}
          onClick={() => pipeline.refresh()}>刷新</Button>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          数据按 TTL 本地缓存 30 秒；展示当日作业事实
        </Typography.Text>
      </Space>)}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        {pipeline.error && (
          <Alert type="error" showIcon message={`流程读取失败：${pipeline.error}`} />)}
        {/* 首启配置缺口（如关注池未配置 → 数据作业不会采集）：服务端只读给出，原样展示 */}
        {configWarningRows(value).map((warning) => (
          <Alert key={warning.code} type="warning" showIcon
            message={warning.message}
            description={warning.hint || undefined} />))}

        <Space size="small" wrap>
          <Typography.Text type="secondary">账户模式</Typography.Text>
          <Tag color={badge.color}>{badge.text}</Tag>
          <Typography.Text type="secondary">自动执行</Typography.Text>
          <Tag color={autoBadge.color}>{autoBadge.text}</Tag>
          {value.auto_pipeline?.error && (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {value.auto_pipeline.error}
            </Typography.Text>)}
          {killActive && <Tag color="red">kill switch 生效中</Tag>}
          {halted && <Tag color="red">日内熔断已触发</Tag>}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {pipeline.loading && !value.date ? "日期 读取中…" : `日期 ${value.date ?? "—"}`}
          </Typography.Text>
        </Space>

        {filtered && (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            市场筛选：{marketLabelOf(market)} —— 只显示该市场的作业链；
            「全局（晚间链）」、自动执行开关与 kill/halt 状态没有市场维度，仍显示。
          </Typography.Text>)}

        {shownMarkets.map((item) => (
          <Card type="inner" key={item} title={marketLabel(item)}>
            <StageChain stages={stagesOf(value.markets?.[item])} loading={pipeline.loading} />
          </Card>))}

        <Card type="inner" title="全局（晚间链）">
          <StageChain stages={stagesOf(value.global)} loading={pipeline.loading} />
        </Card>

        {/* 当日作业链时间轴（第三批图表）：横轴是时刻，每行一个阶段——条左端是计划时刻
            （scheduled，按 payload.date 定位）、右端是实际时刻（at），两者都有时条长就是
            当天的「计划 → 实际」漂移；只有一个时刻时画成瞬时点。派生与跳过计数在
            services/runtimeCharts.js（node --test 直测），本处只接线。 */}
        <Card type="inner" title="当日作业链时间轴（计划 → 实际）">
          <Space direction="vertical" size="small" style={{ width: "100%" }}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              每行一个阶段：时间条左端是计划时刻、右端是实际时刻，两者都有时条长即当天漂移；
              只有一个时刻的阶段按瞬时点画。悬停可见阶段名、状态、计划与实际时刻、摘要。
              {filtered
                ? `当前筛选：${marketLabelOf(market)} + 全局（与上面的卡片同一份数据）。`
                : "当前为全部市场：全局链与 SH/HK/US 三条链一起画，行名前缀即所属链。"}
            </Typography.Text>
            <TimelineChart items={chain.items}
              emptyText={chartEmptyText({
                loading: pipeline.loading && !pipeline.value,
                loadingText: "流程快照加载中…",
                count: chain.items.length,
                emptyText: "流程快照里没有任何阶段（markets/global 为空）。",
                missingText: "所有阶段都没有可用的计划/实际时刻，没有可画上轨道的项。",
              })} />
            {chain.skippedLabels.length > 0 && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {`未画上轨道（无计划时刻、也无实际时刻）：${chain.skippedLabels.join("、")}`}
              </Typography.Text>)}
            {chain.crossDay > 0 && (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {`其中 ${chain.crossDay} 项的实际时刻不在 ${value.date ?? "—"} 当日（跨日运行记录），`
                  + "已按实际时刻原样画出，悬停里也有标注。"}
              </Typography.Text>)}
            {chain.note && !(pipeline.loading && !pipeline.value) && (
              <Typography.Text type="warning" style={{ fontSize: 12 }}>{chain.note}</Typography.Text>)}
          </Space>
        </Card>

        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          阶段状态只有四种：已完成（当日有运行记录）/ 待运行（无运行记录也无告警）/
          已跳过 / 失败；「待运行」不等于失败，也不代表一定会运行。
        </Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          以上内容基于公开信息整理，不构成投资建议
        </Typography.Text>
      </Space>
    </Card>);
}
