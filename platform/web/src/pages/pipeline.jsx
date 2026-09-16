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
import { useEndpoint } from "../services/hooks.js";
import { modeBadge } from "../services/mode.js";
import {
  MARKETS, autoPipelineBadge, marketLabel, stageDrill, stageEntries,
  stageStatus, stageStatusText, stageTagColor, stageTimeText,
} from "../services/pipeline.js";

/** 一条阶段链：节点标题用服务端 label，副标题用 at/scheduled，点击下钻来源页。 */
function StageChain({ stages }) {
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
    return <Typography.Text type="secondary">今日无该链阶段。</Typography.Text>;
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
  const value = pipeline.value ?? {};
  const stagesOf = (chain) => chain?.stages ?? {};
  const autoBadge = autoPipelineBadge(value.auto_pipeline);
  const mode = snapshot.value?.mode;
  const badge = modeBadge(mode);
  const killActive = value.kill === true;
  const halted = value.halt?.halted === true;

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
            日期 {value.date ?? "—"}
          </Typography.Text>
        </Space>

        {MARKETS.map((market) => (
          <Card type="inner" key={market} title={marketLabel(market)}>
            <StageChain stages={stagesOf(value.markets?.[market])} />
          </Card>))}

        <Card type="inner" title="全局（晚间链）">
          <StageChain stages={stagesOf(value.global)} />
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
