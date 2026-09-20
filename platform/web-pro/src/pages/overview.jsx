// V3 工作台「系统概览」页（Ant Design Pro）。
// 功能模块与设计稿版 /v3/index.html **一一对应**（binder: platform/web/public/v3/index.js）：
//   ① KPI 5 张（总资产 / 当日盈亏 / 年化收益 / 夏普比率 / 最大回撤）
//   ② 三通道状态 3 卡（MCP Bridge / SDK JSON-RPC / Headless CLI）
//   ③ 决策链路时间线（研究流水线产出 + 审计链 + Headless last）
//   ④ 风控红线 3 条（单笔 / 单一行业暴露 / 最大回撤）——**阈值来源逐条标注**：
//      行业上限读 `GET /api/v3/risk/industry.limitPct`（真实字段）；
//      单笔与回撤后端无阈值字段，如实标注「默认阈值（内置常量 v3_ops.LIMITS）」，不谎称取自台账
//   ⑤ Agent Loop 实时状态（turn/token/模型/推理强度 + 工具调用 Top5）
//   ⑥ 待人工审批（oms/orders stage=manual 真实订单）
//   ⑦ 数据源健康（workbench / 富途 / AKShare / SEC EDGAR / Tushare 真实探测）
// 数据：GET /api/v3/*；取不到显式「无数据源 + 原因」，页面不含任何占位数字。
import React from "react";
import {
  Alert,
  Badge,
  Card,
  Descriptions,
  Divider,
  Empty,
  Progress,
  Space,
  Statistic,
  Tag,
  Timeline,
  Tooltip,
  Typography,
} from "antd";
import { ProCard } from "@ant-design/pro-components";
import { fmt, noSourceText, useV3 } from "../services/api.js";
import { industryGateView, probeAgeText } from "../lib/risk-labels.js";
import { useMarket } from "../services/marketContext.jsx";
import { BarList, LineChart } from "../components/charts.jsx";

const { Text, Title } = Typography;

// 与设计稿 /v3/ 同一套 token（绿升红跌）
const C = {
  up: "#3fb950",
  down: "#f8514d",
  amber: "#d9a112",
  blue: "#4c8dff",
  muted: "#8b97a5",
  faint: "#626d7c",
};
const MONO = { fontVariantNumeric: "tabular-nums" };

// fmt.num 走 Number()，null/'' 会被算成 0；缺失值必须显式写「—」，不能变成 0.00
const numOr = (value, digits = 2) =>
  value === null || value === undefined || value === "" || !Number.isFinite(Number(value))
    ? "—"
    : Number(value).toFixed(digits);

/**
 * 口径标注（每个受市场影响的卡片都挂一句，避免把全局台账误读成该市场数据）：
 *   global=true  → 「全局口径，不按市场拆分」（进程级 / 单一台账 / 账号级）
 *   global=false → 「当前市场 {label} {market}」
 * text 可覆盖默认文案（例如台账口径的专门说明）。
 */
function ScopeTag({ market, label, global = false, text }) {
  const scope = text || (global ? "全局口径，不按市场拆分" : `当前市场 ${label} ${market}`);
  return (
    <Tag color={global ? "default" : "blue"} style={{ marginInlineEnd: 0 }}>
      {scope}
    </Tag>
  );
}

// KPI 五项全部取自 overview.equity —— 单一台账，后端不按市场拆分权益
const LEDGER_SCOPE = "台账口径（不按市场拆分）";

/* ── 风控红线阈值口径（数字诚实性）─────────────────────────────────────────
 * 平台真实红线在**后端常量**里：`platform/server/v3_ops.py`
 * `LIMITS = {"singlePct": 2.0, "industryPct": 20.0, "drawdownPct": 15.0}`。
 * 页面按「接口字段 → 台账原文 → 内置常量」三级取值，并**逐行标注实际用了哪一级**：
 *   * 行业上限：接口字段（`/api/v3/metrics.industryGate.industryLimitPct`，
 *     退化 `/api/v3/risk/industry.limitPct`）——两个都是后端真值；
 *   * 单笔 / 回撤：后端**没有**独立阈值字段（实测 `/api/v3/oms/orders` 只有
 *     nav / drawdown_pct / drawdown_source / industry_* 与订单 `risk.reasons`），
 *     所以从台账判定理由原文回读闸门**真正应用过**的值
 *     （「单笔占比 6.90% > 2%，需人工确认」「回撤 16.0% 触及 15% 红线」）；
 *   * 两者都拿不到时才用内置常量，并标注「默认阈值（内置常量 v3_ops.LIMITS）」。
 * 注意 `/api/v3/risk` 的 `data.config` 是**工作台 risk 工具**的仓位配置
 * （实测 `risk_per_trade=1% / max_position_pct=25% / daily_loss_limit_pct=3%`），
 * 与这套红线**不是一回事**，因此**不能**拿它顶替 2% / 20% / 15%。
 * 之前这里把 `limit: 2 / 20 / 15` 直接当数据渲染，并在 `:708` 声称「回撤阈值取自
 * 台账」——后者与代码不符（不实声明），本轮一并改正。
 */
const BUILTIN_LIMITS = { singlePct: 2, drawdownPct: 15 };
const BUILTIN_LIMIT_LABEL = "默认阈值（内置常量 v3_ops.LIMITS，后端未暴露字段）";

function errText(payload, fallback = "接口未返回原因") {
  const error = (payload && payload.error) || {};
  return String(error.message || error.code || fallback);
}

/** 无数据源区块：统一走 noSourceText（是什么 + 为什么没有），绝不留占位数字 */
function NoSource({ what, why }) {
  return (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={<Text type="secondary">{noSourceText(what, why)}</Text>}
    />
  );
}

/** 区块级取数失败：只影响本块，不白屏 */
function BlockError({ name, error }) {
  return <Alert type="error" showIcon message={`${name}：取数失败`} description={String(error)} />;
}

function Chip({ ok, okText = "运行中", badText = "无数据源" }) {
  return (
    <Tag color={ok ? "success" : "warning"} style={{ marginInlineEnd: 0 }}>
      {ok ? okText : badText}
    </Tag>
  );
}

/** 单条通道卡（设计稿 .chans .card 形态：名称 + 副标题 + 状态 chip + stat 栅格 + foot） */
function ChannelCard({ name, subtitle, chip, stats, foot, loading }) {
  return (
    <Card
      size="small"
      loading={loading}
      title={
        <Space size={8}>
          <span style={{ fontWeight: 600 }}>{name}</span>
          <Text type="secondary" style={{ fontSize: 11, fontWeight: 400 }}>
            {subtitle}
          </Text>
        </Space>
      }
      extra={chip}
    >
      {stats}
      {foot ? (
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 10 }}>
          {foot}
        </Text>
      ) : null}
    </Card>
  );
}

function KpiCard({ label, value, tone, sub, spark, scope }) {
  return (
    <Card size="small" styles={{ body: { padding: 14 } }}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Text>
      <div style={{ marginTop: 2 }}>
        <Statistic
          value={value}
          valueStyle={{
            ...MONO,
            fontSize: 20,
            fontWeight: 600,
            color: tone === "up" ? C.up : tone === "down" ? C.down : tone === "amber" ? C.amber : undefined,
          }}
        />
      </div>
      {spark ? <div style={{ marginTop: 4 }}>{spark}</div> : null}
      <Text type="secondary" style={{ fontSize: 11 }}>
        {sub}
      </Text>
      {scope ? (
        <Text type="secondary" style={{ fontSize: 10.5, display: "block", marginTop: 2 }}>
          口径：{scope}
        </Text>
      ) : null}
    </Card>
  );
}

export default function OverviewPage() {
  const { market, label } = useMarket();
  // 按市场过滤（后端支持 market=SH|HK|US）：overview（标的池与市场回显）、
  // oms/orders（持仓/订单台账，按标的市场）、strategy（研究流水线提案，按标的市场）
  const overview = useV3("overview", { market }, [market]);
  const orders = useV3("oms/orders", { market }, [market]);
  const strategy = useV3("strategy", { market }, [market]);
  // 全局口径（后端不按市场拆分，页面也不按市场重取）：进程指标、通道探测、settings 环境、审计链
  const metrics = useV3("metrics", {});
  const brain = useV3("brain", {});
  const settings = useV3("settings", {});
  const audit = useV3("audit", { window: 120 });
  // 行业红线上限的真实来源（`limitPct` 是接口字段）；其它两条红线后端无字段，见 BUILTIN_LIMITS
  const industry = useV3("risk/industry", { market }, [market]);

  const ov = overview.value && overview.value.ok ? overview.value : null;
  const mt = metrics.value && metrics.value.ok ? metrics.value : null;
  const br = brain.value && brain.value.ok ? brain.value : null;
  const st = settings.value && settings.value.ok ? settings.value : null;
  const od = orders.value && orders.value.ok ? orders.value : null;
  const auditRows =
    (audit.value && audit.value.ok && audit.value.data && audit.value.data.entries) || [];

  /* ── 市场回显：接口若给了 market / universe_source / sections.market_scoped 就如实展示 ── */
  const marketEcho = (ov && ov.market) || null;
  const universeSource = (ov && ov.universe_source) || null;
  const marketScopedText = (() => {
    const value = ov && ov.sections ? ov.sections.market_scoped : null;
    if (typeof value === "string") return value;
    if (value && typeof value === "object") {
      return Object.entries(value)
        .filter(([, item]) => item === null || item === undefined || typeof item !== "object")
        .map(([key, item]) => `${key}=${item === null || item === undefined ? "—" : String(item)}`)
        .join(" · ");
    }
    return null;
  })();
  // 后端按市场取数失败（含 market/no-universe）时如实带出原因，绝不用旧数据顶替
  const overviewError = overview.value && overview.value.ok === false ? errText(overview.value) : null;
  const ordersError = orders.value && orders.value.ok === false ? errText(orders.value) : null;
  const strategyError = strategy.value && strategy.value.ok === false ? errText(strategy.value) : null;

  const refreshAll = () => {
    overview.refresh();
    metrics.refresh();
    brain.refresh();
    settings.refresh();
    orders.refresh();
    strategy.refresh();
    audit.refresh();
    industry.refresh();
  };

  /* ── ① KPI：全部来自 overview.equity（台账权益 / 回撤 / 夏普 / 成交笔数） ── */
  const equity = (ov && ov.equity) || {};
  const points = Array.isArray(equity.points) ? equity.points : [];
  const last = points.length ? points[points.length - 1] : null;
  const prev = points.length > 1 ? points[points.length - 2] : null;
  const hasPrev = Boolean(last && prev && Number(prev.equity));
  const dailyPct = hasPrev ? (Number(last.equity) / Number(prev.equity) - 1) * 100 : null;
  const dailyAbs = hasPrev ? Number(last.equity) - Number(prev.equity) : null;
  const days = points.length > 1 ? points.length : 0;
  const annualized =
    days > 0 && Number.isFinite(Number(equity.total_return))
      ? (Math.pow(1 + Number(equity.total_return), 252 / days) - 1) * 100
      : null;
  const drawdown = Number.isFinite(Number(equity.max_drawdown))
    ? Math.abs(Number(equity.max_drawdown)) * 100
    : null;
  const spark =
    points.length >= 2 ? <LineChart values={points.map((p) => p.equity)} height={34} /> : null;

  const kpis = [
    {
      label: "总资产",
      value: Number.isFinite(Number(equity.current)) ? fmt.money(equity.current) : "—",
      sub: Number.isFinite(Number(equity.initial))
        ? `台账初始 ${fmt.money(equity.initial)}${dailyAbs === null ? "" : ` · 较前值 ${fmt.signed(dailyAbs, 2)}`}`
        : "台账未返回初始权益",
      spark,
      scope: LEDGER_SCOPE,
    },
    {
      label: "当日盈亏",
      value: dailyPct === null ? "无基准" : fmt.signed(dailyPct, 2, "%"),
      tone: dailyPct === null ? null : dailyPct >= 0 ? "up" : "down",
      sub:
        dailyPct === null
          ? `台账点位 ${points.length} 个（<2）· 无前一交易日基准，日内涨跌无数据源`
          : `基准 ${prev.t} → ${last.t}（台账回放）`,
      spark,
      scope: LEDGER_SCOPE,
    },
    {
      label: "年化收益",
      value: annualized === null ? "—" : fmt.signed(annualized, 2, "%"),
      tone: annualized === null ? null : annualized >= 0 ? "up" : "down",
      sub:
        annualized === null
          ? `台账点位不足（${points.length} 个）· 年化无数据源`
          : `${days} 个交易日年化`,
      spark,
      scope: LEDGER_SCOPE,
    },
    {
      label: "夏普比率",
      value: numOr(equity.sharpe, 2),
      sub: `成交 ${Number.isFinite(Number(equity.trades)) ? equity.trades : "—"} 笔 · 台账回放口径`,
      scope: LEDGER_SCOPE,
    },
    {
      label: "最大回撤",
      value: drawdown === null ? "—" : `−${fmt.num(drawdown, 2)}%`,
      tone: "amber",
      sub: `阈值 ${BUILTIN_LIMITS.drawdownPct}%（默认阈值·内置常量，后端未暴露字段）${equity.note ? ` · ${String(equity.note).slice(0, 18)}…` : ""}`,
      spark,
      scope: LEDGER_SCOPE,
    },
  ];

  /* ── ② 三通道状态 ── */
  const mcpCalls = Number(mt && mt.mcp ? mt.mcp.calls : 0) || 0;
  const mcpErrors = Number(mt && mt.mcp ? mt.mcp.errors : 0) || 0;
  const mcpOk = Boolean(mt);
  const sdkReason = (br && br.sdk && br.sdk.reason) || "本服务未挂载 SDK JSON-RPC 客户端";
  const headlessReason =
    (br && br.headless && br.headless.reason) || "本服务未挂载 Headless CLI 运行器";

  const mcpStats = (
    <Descriptions
      size="small"
      column={2}
      colon={false}
      items={[
        {
          key: "tools",
          label: "已注册工具",
          children: <span style={MONO}>{mt ? `${fmt.dash(mt.toolTotal)} 个` : "—"}</span>,
        },
        { key: "calls", label: "今日调用", children: <span style={MONO}>{mt ? `${mcpCalls} 次` : "—"}</span> },
        {
          key: "rate",
          label: "成功率",
          children: (
            <span style={{ ...MONO, color: mcpErrors === 0 ? C.up : C.amber }}>
              {mcpCalls > 0 ? `${fmt.num(((mcpCalls - mcpErrors) / mcpCalls) * 100, 1)}%` : "—"}
            </span>
          ),
        },
        {
          key: "latency",
          label: "平均延迟",
          children: (
            <span style={MONO}>
              {mt && Number.isFinite(Number(mt.mcp && mt.mcp.avgMs)) ? `${mt.mcp.avgMs} ms` : "—"}
            </span>
          ),
        },
      ]}
    />
  );

  /* ── ③ 决策链路时间线：研究流水线产出 + 审计链 + Headless last（全部真实来源） ── */
  const srun = strategy.value && strategy.value.ok ? strategy.value.run : null;
  const brunRaw = br ? br.decision : null;
  const brun = brunRaw && brunRaw.run ? brunRaw.run : brunRaw;
  const run = srun || (brun && brun.asOf ? brun : null);
  // 流水线节点来自按市场过滤的 /api/v3/strategy；否则回退到全局口径的 brain.decision
  const runFromStrategy = Boolean(srun);
  const headlessLast =
    (br && Array.isArray(br.headless && br.headless.last) && br.headless.last) || [];

  const timelineItems = [];
  if (run && run.asOf) {
    const count = Array.isArray(run.proposals) ? run.proposals.length : 0;
    const runMarket = run.market ? String(run.market) : null;
    timelineItems.push({
      color: "blue",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color="blue">流水线节点</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {fmt.stamp(run.asOf).slice(11, 19)}
            </Text>
          </Space>
          <Text>研究流水线产出 {count} 条调仓建议（PDAT → PET，as_of {fmt.stamp(run.asOf)}）</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            {runFromStrategy
              ? `/api/v3/strategy?market=${market}（当前市场 ${label} ${market}${runMarket ? ` · 回显 market=${runMarket}` : " · 该轮未记录 market，按后端返回原样展示"}）· 待人工审批，研究侧不下单`
              : "/api/v3/brain.decision（全局口径，不按市场拆分）· 待人工审批，研究侧不下单"}
          </Text>
        </Space>
      ),
    });
  }
  for (const entry of auditRows.slice(0, 5)) {
    const kindText = entry.kind === "signal" ? "信号" : entry.source_label || entry.kind || "审计";
    timelineItems.push({
      color: entry.kind === "signal" ? "green" : "gray",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color={entry.kind === "signal" ? "green" : "default"}>{kindText}</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {fmt.stamp(entry.at).slice(11, 19) || fmt.stamp(entry.at).slice(0, 10)}
            </Text>
          </Space>
          <Text>{`${entry.ticker ? `${entry.ticker} · ` : ""}${entry.detail || "—"}`}</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            工作台审计链 /api/v3/audit（全局口径，不按市场拆分）
          </Text>
        </Space>
      ),
    });
  }
  for (const item of headlessLast.slice(0, 3)) {
    timelineItems.push({
      color: "orange",
      children: (
        <Space direction="vertical" size={2}>
          <Space size={6} wrap>
            <Tag color="orange">Headless</Tag>
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              {fmt.stamp(item.at || item.finished_at || item.started_at).slice(11, 19)}
            </Text>
          </Space>
          <Text>{String(item.task || item.name || item.tool || "Headless 任务")}</Text>
          <Text type="secondary" style={{ fontSize: 11 }}>
            状态 {String(item.status || item.result || "—")} · /api/v3/brain（全局口径，不按市场拆分）
          </Text>
        </Space>
      ),
    });
  }

  /* ── ④ 风控红线三条 ──
   * 阈值来源优先级（逐条如实标注，**不把内置常量说成接口读数**）：
   *   ① 接口字段：行业上限取 `/api/v3/metrics.industryGate.industryLimitPct`，
   *      退化到 `/api/v3/risk/industry.limitPct`（两个都是后端真值）；
   *   ② 台账 `risk.reasons` 原文回读：闸门真正应用过的单笔 / 回撤阈值（形如
   *      「单笔占比 6.90% > 2%，需人工确认」「回撤 16.0% 触及 15% 红线」）；
   *   ③ 都取不到时才用内置常量，并**逐行标注**「默认阈值（内置常量）」。
   * 来源：`platform/server/v3_ops.py` 的 `LIMITS` / `check_order` / `OmsLedger.gate_view()`。
   */
  const nav = Number(equity.current != null ? equity.current : od && od.nav ? od.nav : 0);
  const orderRows = (od && Array.isArray(od.orders) && od.orders) || [];
  const maxSingle =
    nav > 0
      ? orderRows.reduce((max, order) => Math.max(max, (Number(order.value || 0) / nav) * 100), 0)
      : null;
  const singleUsed = maxSingle !== null && maxSingle > 0 ? maxSingle : null;

  const gate = metrics.value?.industryGate ?? null;
  const gateLimit = gate && Number.isFinite(Number(gate.industryLimitPct)) ? Number(gate.industryLimitPct) : null;
  const industryLimit = gateLimit
    ?? (Number.isFinite(Number(industry.value?.limitPct)) && industry.value?.limitPct !== null
      ? Number(industry.value.limitPct)
      : null);
  const industryLimitSource = gateLimit !== null
    ? "GET /api/v3/metrics.industryGate.industryLimitPct（后端 LIMITS.industryPct）"
    : industryLimit !== null
      ? "GET /api/v3/risk/industry.limitPct（后端 LIMITS.industryPct）"
      : industry.error
        ? `取不到（取数失败：${industry.error}）`
        : industry.loading
          ? "加载中…"
          : "两个接口都未返回行业上限字段";

  // 台账 reasons 原文回读（与风控页同一口径）：只有闸门真跑过才会有这两个数
  const ledgerReasonText = orderRows
    .map((order) => (order.risk && Array.isArray(order.risk.reasons) ? order.risk.reasons.join("；") : ""))
    .join("；");
  const singleMatch = ledgerReasonText.match(/单笔占比[^>＞]*[>＞]\s*([0-9.]+)\s*%/);
  const drawdownMatch = ledgerReasonText.match(/触及\s*([0-9.]+)\s*%\s*红线/);
  const singleLimit = singleMatch ? Number(singleMatch[1]) : null;
  const drawdownLimit = drawdownMatch ? Number(drawdownMatch[1]) : null;
  const redlines = [
    {
      label: "单笔交易上限",
      limit: singleLimit ?? BUILTIN_LIMITS.singlePct,
      limitKind: singleLimit === null ? "builtin" : "ledger",
      limitSource: singleLimit === null
        ? BUILTIN_LIMIT_LABEL
        : `台账 risk.reasons 原文回读「> ${singleLimit}%」（下单前闸门实际应用值）`,
      limitBuiltin: singleLimit === null,
      used: singleUsed,
      why:
        ordersError ||
        `当前市场 ${label} ${market} 的订单台账（/api/v3/oms/orders?market=${market}）未返回可比较的单笔金额`,
      source: od ? `${od.nav_source || "台账"} · 分母 ${fmt.money(od.nav)}（台账口径，不按市场拆分）` : null,
    },
    {
      label: "单一行业暴露上限",
      limit: industryLimit,
      limitKind: "field",
      limitSource: industryLimitSource,
      limitBuiltin: false,
      // 当前值：闸门读数优先（industryGate.industryPct，含来源与探测年龄），否则 /api/v3/risk/industry.top.weightPct
      used: gate && gate.industryPct !== null && gate.industryPct !== undefined && Number.isFinite(Number(gate.industryPct))
        ? Number(gate.industryPct)
        : (Number.isFinite(Number(industry.value?.top?.weightPct)) ? Number(industry.value.top.weightPct) : null),
      why: industry.error
        ? `GET /api/v3/risk/industry?market=${market} 取数失败：${industry.error}`
        : industry.loading
          ? "加载中…（行业读数未返回前不给判定）"
          : "接口未返回行业读数（该市场无自选池 / 无持仓时服务端返回 industry/no-universe）",
      source: gate && gate.industrySource
        ? `闸门读数来源 ${gate.industrySource}${gate.industryTop ? ` · Top 行业 ${gate.industryTop}` : ""} · as_of ${fmt.stamp(gate.industryAsOf)} · 探测年龄 ${probeAgeText(gate.industryProbeAgeMs)}`
        : industry.value?.top?.industry
          ? `当前 Top 行业 ${industry.value.top.industry} · as_of ${fmt.stamp(industry.value.as_of)} · breach=${industry.value.breach === true}`
          : null,
    },
    {
      label: "最大回撤阈值",
      limit: drawdownLimit ?? BUILTIN_LIMITS.drawdownPct,
      limitKind: drawdownLimit === null ? "builtin" : "ledger",
      limitSource: drawdownLimit === null
        ? BUILTIN_LIMIT_LABEL
        : `台账 risk.reasons 原文回读「触及 ${drawdownLimit}% 红线」（下单前闸门实际应用值）`,
      limitBuiltin: drawdownLimit === null,
      used: drawdown,
      // 台账只给**回撤点位**（overview.equity.max_drawdown），**不给阈值**；
      // 此前这里写「台账无回撤点位」也不准确，如实改成阈值来源逐条标注在行上。
      why: "台账只返回回撤点位，不返回回撤阈值（阈值需上述两个来源之一，否则为内置常量）",
      source: equity.note ? `回撤口径：${equity.note}（台账口径，不按市场拆分）` : null,
    },
  ];
  // 行业闸门 fail-open：真实风险窗口，页面必须显式提示（不是装饰）
  const gateView = industryGateView(gate);

  /* ── ⑤ Agent Loop：SDK 会话未挂载 → 会话指标无数据源；工具调用 Top5 为真实计数 ── */
  const topTools = [
    ...Object.entries((mt && mt.mcp && mt.mcp.tools) || {}),
    ...Object.entries((mt && mt.wb && mt.wb.byTool) || {}).map(([name, count]) => [`wb:${name}`, count]),
  ]
    .map(([name, count]) => [name, Number(count) || 0])
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5);

  /* ── ⑥ 待人工审批：oms/orders 中 stage=manual 的真实订单 ── */
  const manualOrders = orderRows.filter((order) => order.stage === "manual");

  /* ── ⑦ 数据源健康 ── */
  const dsFromSettings = (st && Array.isArray(st.data_sources) && st.data_sources) || [];
  const probeData = (br && br.sources && br.sources.workbench && br.sources.workbench.data) || null;
  const dsFromProbe = (probeData && Array.isArray(probeData.sources) && probeData.sources) || [];
  const healthRows = dsFromSettings.length
    ? dsFromSettings.map((item) => ({
        name: item.name,
        ok: Boolean(item.available),
        detail: String(item.detail || "—"),
      }))
    : dsFromProbe.map((item) => ({
        name: item.label || item.key || "—",
        ok: String(item.status).toLowerCase() === "ok",
        detail: String(item.detail || item.fix || "—"),
      }));
  if (mt) {
    healthRows.push({
      name: "平台工作台工具面（本平台自建；未接入 dsh-quant-data-mcp）",
      ok: Boolean(mt.workbenchUp),
      detail: `MCP 工具域 · ${fmt.dash(mt.toolDomains)} 域 · ${fmt.dash(mt.toolTotal)} 个工具 · 今日调用 ${mcpCalls} 次 · 失败 ${mcpErrors} 次 · 平均 ${fmt.dash(
        mt.mcp && mt.mcp.avgMs,
      )}ms`,
    });
  }

  const mode = String((ov && ov.mode) || "").toUpperCase();

  return (
    <Space direction="vertical" size={12} style={{ width: "100%" }}>
      {/* 行业闸门风险窗口：fail-open 时下单前不阻断，这是必须让人看见的真实状态 */}
      {gateView.available && gateView.failOpen ? (
        <Alert
          type="warning"
          showIcon
          message="行业集中度红线当前未参与下单前阻断（fail-open）"
          description={
            <Text type="secondary" style={{ fontSize: 12 }}>
              原因：{gateView.reason}。恢复新鲜读数后，单一行业暴露 &gt;{" "}
              {gateView.limitPct === null ? "上限" : `${gateView.limitPct}%`} 的订单将由闸门判{" "}
              <Text code>stage=blocked_industry</Text> 强制阻断。本提示来自 /api/v3/metrics.industryGate.failOpen。
            </Text>
          }
        />
      ) : null}
      {gateView.available && !gateView.failOpen ? (
        <Alert
          type="info"
          showIcon
          message={`行业红线已接入下单前闸门：当前读数 ${fmt.pct(gateView.pct, 2)}（${gateView.top || "—"}）· 上限 ${fmt.pct(gateView.limitPct, 0)} · ${gateView.breach ? "已超限，新订单将被强制阻断" : "未超限"}`}
          description={
            <Text type="secondary" style={{ fontSize: 12 }}>
              来源 {gateView.source || "—"} · as_of {fmt.stamp(gateView.asOf)} · 探测年龄 {probeAgeText(gateView.probeAgeMs)}
              {gateView.missing ? ` · ${gateView.missing} 只标的未取到行业分类（读数只是下界，真实暴露可能更高）` : ""}
            </Text>
          }
        />
      ) : null}
      {!gateView.available ? (
        <Alert
          type="info"
          showIcon
          message="行业闸门读数不可用（未取得 industryGate）"
          description={
            <Text type="secondary" style={{ fontSize: 12 }}>
              {gateView.reason}。本页因此不展示闸门聚合读数；行业暴露明细仍取 /api/v3/risk/industry。
            </Text>
          }
        />
      ) : null}
      {/* 页头：模式 / 数据截至 / 刷新（对应设计稿 .page-tools） */}
      <ProCard
        bordered
        bodyStyle={{ padding: "10px 16px" }}
        loading={overview.loading}
        title={
          <Title level={5} style={{ margin: 0 }}>
            系统概览
          </Title>
        }
        extra={
          <Space size={12} wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>
              平台（身体）× Harness（决策大脑）· 全局运行状态（不按市场拆分）· 环境 {mode || "—"}
            </Text>
            <Badge
              status={mode === "live" ? "error" : "processing"}
              text={mode === "live" ? "LIVE 实盘" : mode ? "SIM 模拟盘" : "模式未知"}
            />
            <Text type="secondary" style={{ ...MONO, fontSize: 12 }}>
              数据截至 {fmt.stamp(ov && ov.generated_at)}
            </Text>
            <Typography.Link onClick={refreshAll}>刷新</Typography.Link>
          </Space>
        }
      >
        {overview.error ? (
          <BlockError name="系统概览（/api/v3/overview）" error={overview.error} />
        ) : (
          <Space direction="vertical" size={4} style={{ width: "100%" }}>
            {overviewError ? (
              <Alert
                type="warning"
                showIcon
                message={noSourceText(`当前市场 ${label} ${market} 的系统概览（/api/v3/overview?market=${market}）`, overviewError)}
              />
            ) : null}
            <Space size={8} wrap>
              <ScopeTag market={market} label={label} />
              <Text type="secondary" style={{ fontSize: 11 }}>
                {marketEcho ? `接口回显 market=${String(marketEcho)}` : "接口未回显 market（按请求参数展示）"}
              </Text>
              {universeSource ? (
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {`标的池来源 universe_source=${String(universeSource)}`}
                </Text>
              ) : null}
              {marketScopedText ? (
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {`sections.market_scoped：${marketScopedText}`}
                </Text>
              ) : null}
            </Space>
            <Text type="secondary" style={{ fontSize: 11 }}>
              取数口径：overview / oms/orders / strategy 带 market={market}；metrics / brain / settings / audit
              为全局口径（不按市场拆分）。数据来源：本服务 /api/v3/*（工作台工具面 / 富途行情 / 台账）·
              取不到的项显式标注「无数据源」，页面不含占位数字
            </Text>
          </Space>
        )}
      </ProCard>

      {/* ① KPI 5 张（全部为台账口径，后端不按市场拆分权益） */}
      <Text type="secondary" style={{ fontSize: 11 }}>
        {`口径：KPI 五项（总资产 / 当日盈亏 / 年化收益 / 夏普 / 最大回撤）来自单一台账，为台账口径（不按市场拆分），
        与上方市场选择无关；按市场过滤的是下面「待人工审批」的订单明细与「风控红线」的订单分子（market=${market}）。`}
      </Text>
      {overview.error ? null : ov ? (
        <div style={{ display: "grid", gridTemplateColumns: "repeat(5, minmax(0, 1fr))", gap: 12 }}>
          {kpis.map((kpi) => (
            <KpiCard key={kpi.label} {...kpi} />
          ))}
        </div>
      ) : (
        <ProCard bordered loading={overview.loading}>
          <NoSource
            what="KPI（总资产 / 当日盈亏 / 年化收益 / 夏普比率 / 最大回撤）"
            why={errText(overview.value, "GET /api/v3/overview 未返回台账权益")}
          />
        </ProCard>
      )}

      {/* ② 三通道状态 3 卡（进程级全局口径） */}
      <Text type="secondary" style={{ fontSize: 11 }}>
        {`口径：三通道（MCP Bridge / SDK JSON-RPC / Headless CLI）为进程级全局口径（不按市场拆分），
        与当前市场 ${label} ${market} 无关；切换市场不会重取本区数据。`}
      </Text>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 12 }}>
        <ChannelCard
          name="MCP Bridge"
          subtitle="工具暴露通道"
          loading={metrics.loading}
          chip={<Chip ok={mcpOk} />}
          stats={metrics.error ? <BlockError name="MCP Bridge（/api/v3/metrics）" error={metrics.error} /> : mcpStats}
          foot={
            mt
              ? `状态机状态 running（工具面 ${fmt.dash(mt.toolTotal)} 个 · workbench ${mt.workbenchUp ? "在线" : "不可达"}）`
              : undefined
          }
        />
        <ChannelCard
          name="SDK JSON-RPC"
          subtitle="人工会话通道"
          loading={brain.loading}
          chip={<Chip ok={false} />}
          stats={
            brain.error ? (
              <BlockError name="SDK JSON-RPC（/api/v3/brain）" error={brain.error} />
            ) : (
              <Space direction="vertical" size={8} style={{ width: "100%" }}>
                <NoSource what="SDK 通道会话指标（活跃会话 / 初始化握手耗时 / 最近事件）" why={sdkReason} />
                <Descriptions
                  size="small"
                  column={1}
                  colon={false}
                  items={[
                    {
                      key: "status",
                      label: "通道状态",
                      children: (
                        <Text type="secondary">
                          {(br && br.sdk && br.sdk.status) || "unavailable"}（无数据源）
                        </Text>
                      ),
                    },
                    {
                      key: "turns",
                      label: "落盘回合",
                      children: (
                        <Text type="secondary">
                          {br && br.sdk && Array.isArray(br.sdk.turns)
                            ? `${br.sdk.turns.length} 条（未挂载会话即 0）`
                            : "无数据源"}
                        </Text>
                      ),
                    },
                  ]}
                />
              </Space>
            )
          }
          foot="SDK 通道未挂载：会话与握手耗时无数据源"
        />
        <ChannelCard
          name="Headless CLI"
          subtitle="自动唤醒通道"
          loading={brain.loading}
          chip={<Chip ok={false} />}
          stats={
            brain.error ? (
              <BlockError name="Headless CLI（/api/v3/brain）" error={brain.error} />
            ) : (
              <Descriptions
                size="small"
                column={2}
                colon={false}
                items={[
                  {
                    key: "total",
                    label: "今日唤醒",
                    children: (
                      <span style={MONO}>
                        {br && br.headless && br.headless.today ? br.headless.today.total : "无数据源"}
                      </span>
                    ),
                  },
                  {
                    key: "ok",
                    label: "成功 / 失败",
                    children: (
                      <span style={MONO}>
                        {br && br.headless && br.headless.today
                          ? `${br.headless.today.success} / ${br.headless.today.failed}`
                          : "无数据源"}
                      </span>
                    ),
                  },
                  {
                    key: "avg",
                    label: "平均耗时",
                    children: (
                      <span style={MONO}>
                        {br && br.headless && br.headless.today && Number(br.headless.today.total) > 0
                          ? `${br.headless.today.avgMs} ms`
                          : "无数据源"}
                      </span>
                    ),
                  },
                  {
                    key: "last",
                    label: "最近任务",
                    children: (
                      <Text type="secondary">{headlessLast.length ? `${headlessLast.length} 条` : "无数据源"}</Text>
                    ),
                  },
                ]}
              />
            )
          }
          foot={`Headless 通道未挂载：${headlessReason} · 定时器 + 调度心跳由服务侧提供（见网关与调度页）`}
        />
      </div>

      {/* ③ 决策链路时间线 + ④ 风控红线 */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1.35fr) minmax(0, 1fr)", gap: 12 }}>
        <ProCard
          title="决策链路时间线"
          bordered
          loading={strategy.loading || audit.loading || brain.loading}
          extra={
            <Text type="secondary" style={{ fontSize: 11 }}>
              最近 {timelineItems.length} 条
            </Text>
          }
        >
          <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
            {`口径：研究流水线节点按当前市场 ${label} ${market} 过滤（/api/v3/strategy?market=${market}）；
            审计链与 Headless 为全局口径（不按市场拆分）。`}
          </Text>
          {strategyError ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 10 }}
              message={noSourceText(`当前市场 ${label} ${market} 的研究流水线`, strategyError)}
            />
          ) : null}
          {strategy.error ? (
            <BlockError name="决策链路（/api/v3/strategy）" error={strategy.error} />
          ) : timelineItems.length === 0 ? (
            <NoSource what="决策链路" why="审计链与流水线均无记录" />
          ) : (
            <>
              <Timeline items={timelineItems} />
              <Divider style={{ margin: "8px 0" }} />
              <Text type="secondary" style={{ fontSize: 11 }}>
                本链路 {timelineItems.length} 节点 · 来源：研究流水线 /api/v3/strategy（当前市场 {label} {market}）· 工作台审计链
                /api/v3/audit（全局口径，不按市场拆分）
                {headlessLast.length ? " · Headless /api/v3/brain（全局口径，不按市场拆分）" : ""}
              </Text>
            </>
          )}
        </ProCard>

        <ProCard title="风控红线" bordered loading={orders.loading || overview.loading}>
          <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
            {`口径：订单明细按当前市场 ${label} ${market} 过滤（/api/v3/oms/orders?market=${market}）；
            单笔占比的分母 NAV 以接口 nav_source 为准（本页不假定它按市场拆分）。
            每行「上限」旁标注阈值来源：接口字段（industryGate.industryLimitPct / risk/industry.limitPct）、
            台账原文（从订单 risk.reasons 回读闸门实际应用值）、或内置常量（后端未暴露且台账未回读到，按 v3_ops.LIMITS 标注）。`}
          </Text>
          {ordersError ? (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 10 }}
              message={noSourceText(`当前市场 ${label} ${market} 的订单台账`, ordersError)}
            />
          ) : null}
          {orders.error ? <BlockError name="风控红线（/api/v3/oms/orders）" error={orders.error} /> : null}
          {redlines.map((row) => {
            const hasLimit = row.limit !== null && Number.isFinite(Number(row.limit));
            const hasValue = hasLimit && row.used !== null && Number.isFinite(Number(row.used));
            const ratio = hasValue ? Math.min(100, (Number(row.used) / Number(row.limit)) * 100) : 0;
            const stroke = !hasValue ? C.faint : ratio >= 100 ? C.down : ratio >= 70 ? C.amber : C.up;
            return (
              <div key={row.label} style={{ marginBottom: 14 }}>
                <Space style={{ width: "100%", justifyContent: "space-between" }}>
                  <Text>{row.label}</Text>
                  <Space size={6}>
                    <Text style={{ ...MONO, color: C.muted }}>
                      上限 {hasLimit ? `${row.limit}%` : "—"}
                    </Text>
                    <Tooltip title={row.limitSource}>
                      <Text type="secondary" style={{ fontSize: 10.5 }}>
                        {row.limitKind === "field" ? "接口字段" : row.limitKind === "ledger" ? "台账原文" : "内置常量"}
                      </Text>
                    </Tooltip>
                  </Space>
                </Space>
                <Progress
                  percent={Number(ratio.toFixed(1))}
                  strokeColor={stroke}
                  size="small"
                  format={() =>
                    hasValue ? (
                      <span style={{ ...MONO, color: C.muted, fontSize: 11 }}>
                        当前 {fmt.num(row.used, 2)}%
                      </span>
                    ) : (
                      <span style={{ color: C.faint, fontSize: 11 }}>无数据源</span>
                    )
                  }
                />
                {hasValue ? (
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    {ratio >= 100 ? "已越过阈值，需人工核对" : ratio >= 70 ? "接近阈值" : "阈值内"}
                    {` · 阈值来源：${row.limitSource}`}
                    {row.source ? ` · ${row.source}` : ""}
                  </Text>
                ) : (
                  <Tooltip title={hasLimit ? row.why : row.limitSource}>
                    <Text type="secondary" style={{ fontSize: 11 }}>
                      {hasLimit
                        ? noSourceText(row.label, row.why)
                        : noSourceText(`${row.label}的上限`, `${row.limitSource}（阈值都取不到，故不给当前判定）`)}
                    </Text>
                  </Tooltip>
                )}
              </div>
            );
          })}
          {od ? (
            <Text type="secondary" style={{ fontSize: 11 }}>
              台账订单 {orderRows.length} 条（当前市场 {label} {market} · manual {manualOrders.length} 条）· NAV{" "}
              {fmt.money(od.nav)}（{od.nav_source || "—"} · 台账口径，不按市场拆分）
            </Text>
          ) : null}
        </ProCard>
      </div>

      {/* ⑤ Agent Loop 实时状态（进程级全局口径） */}
      <ProCard
        title="Agent Loop 实时状态"
        bordered
        loading={metrics.loading || brain.loading}
        extra={
          <Space size={8}>
            <ScopeTag market={market} label={label} global />
            <Chip ok={false} />
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          口径：Agent Loop 会话指标与工具调用分布为进程级全局口径（不按市场拆分），不随市场切换而改变。
        </Text>
        <Descriptions
          size="small"
          column={4}
          colon={false}
          items={[
            { key: "turn", label: "当前 turn", children: <Text type="secondary">无数据源</Text> },
            { key: "token", label: "已用 token", children: <Text type="secondary">无数据源</Text> },
            { key: "model", label: "模型", children: <Text type="secondary">无数据源</Text> },
            { key: "effort", label: "推理强度", children: <Text type="secondary">无数据源</Text> },
          ]}
        />
        <Alert
          style={{ marginTop: 10 }}
          type="warning"
          showIcon
          message={noSourceText(
            "Agent Loop 会话指标（turn / token / 模型 / 推理强度）",
            `${sdkReason}；Headless：${headlessReason}`,
          )}
          description={
            <Text type="secondary" style={{ fontSize: 11 }}>
              本页只提供研究流水线记录（见决策链路时间线）与进程内工具调用计数，不用设计稿里的占位轮次 / token 顶替。
            </Text>
          }
        />
        <Divider style={{ margin: "12px 0 8px" }} orientation="left" orientationMargin={0}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            工具调用 Top 5
          </Text>
        </Divider>
        {metrics.error ? (
          <BlockError name="工具调用分布（/api/v3/metrics）" error={metrics.error} />
        ) : topTools.length === 0 ? (
          <NoSource what="工具调用 Top 5" why="进程内调用计数为空（/api/v3/metrics 未返回任何工具计数）" />
        ) : (
          <>
            <BarList items={topTools} />
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 8 }}>
              当前进程累计 · 共 {mcpCalls} 次调用 · 失败 {mcpErrors} 次 · 平均{" "}
              {fmt.dash(mt && mt.mcp ? mt.mcp.avgMs : null)}ms · 来源 /api/v3/metrics（mcp.tools + wb.byTool）
            </Text>
          </>
        )}
      </ProCard>

      {/* ⑥ 待人工审批（按目标标的市场过滤） */}
      <ProCard
        title="待人工审批"
        bordered
        loading={orders.loading}
        extra={
          <Space size={8} wrap>
            <ScopeTag market={market} label={label} />
            <Tag color={manualOrders.length ? "warning" : "default"}>{manualOrders.length} 条待处理</Tag>
            <Text type="secondary" style={{ fontSize: 11 }}>
              执行入口唯一 · 所有执行动作须经人工审批
            </Text>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          {`口径：本卡订单明细按当前市场 ${label} ${market} 过滤（/api/v3/oms/orders?market=${market}）；单笔占比的分母为台账 NAV（台账口径，不按市场拆分）。`}
        </Text>
        {orders.error ? (
          <BlockError name="待人工审批（/api/v3/oms/orders）" error={orders.error} />
        ) : manualOrders.length === 0 ? (
          <NoSource
            what="待人工审批"
            why={od ? `当前市场 ${label} ${market} 的台账没有 manual 阶段订单` : ordersError || errText(orders.value, "订单台账未返回")}
          />
        ) : (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12 }}>
            {manualOrders.map((order) => {
              const orderNav = Number(order.nav_used != null ? order.nav_used : od && od.nav ? od.nav : 0);
              const share =
                orderNav > 0 && Number.isFinite(Number(order.value)) ? (Number(order.value) / orderNav) * 100 : null;
              return (
                <Card
                  key={order.id}
                  size="small"
                  title={
                    <Space size={6} wrap>
                      <Text strong style={MONO}>
                        {order.ticker}
                      </Text>
                      <Tag color={order.side === "BUY" ? "success" : "error"}>{order.side}</Tag>
                      <Text style={MONO}>{fmt.dash(order.qty)} 股</Text>
                      <Text type="secondary" style={MONO}>
                        金额 {fmt.money(order.value)}
                      </Text>
                    </Space>
                  }
                  extra={<Tag color="warning">{order.stage}</Tag>}
                >
                  <Space direction="vertical" size={2} style={{ width: "100%" }}>
                    <Text style={{ fontSize: 12 }}>
                      来源：工作台计划 {fmt.dash(order.plan_id)}（{fmt.dash(order.plan_status)}）· 模式{" "}
                      {fmt.dash(order.mode)}
                    </Text>
                    <Text style={{ fontSize: 12 }}>
                      风控：{Array.isArray(order.risk && order.risk.reasons) && order.risk.reasons.length > 0
                        ? order.risk.reasons.join("；")
                        : "未返回判定依据（订单 risk.reasons 缺失，本页不代平台下结论）"}
                    </Text>
                    <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
                      单笔占比 {numOr(share, 2)}% {order.nav_source ? `（${order.nav_source}）` : ""}· 首次进入{" "}
                      {fmt.stamp(order.first_seen_at)} · 更新 {fmt.stamp(order.updated_at)}
                    </Text>
                  </Space>
                </Card>
              );
            })}
          </div>
        )}
      </ProCard>

      {/* ⑦ 数据源健康（全局链路） */}
      <ProCard
        title="数据源健康"
        bordered
        loading={settings.loading || metrics.loading || brain.loading}
        extra={
          <Space size={8} wrap>
            <ScopeTag market={market} label={label} global />
            <Text type="secondary" style={{ ...MONO, fontSize: 11 }}>
              workbench {mt ? (mt.workbenchUp ? "在线" : "不可达") : "—"} · 工具 {fmt.dash(mt && mt.toolTotal)} 个 ·
              数据时点 {fmt.stamp(ov && ov.generated_at)}
            </Text>
          </Space>
        }
      >
        <Text type="secondary" style={{ fontSize: 11, display: "block", marginBottom: 8 }}>
          口径：数据源探测与环境注入状态为全局口径（不按市场拆分）；本卡不按市场重取。
        </Text>
        {settings.error ? (
          <BlockError name="数据源健康（/api/v3/settings）" error={settings.error} />
        ) : healthRows.length === 0 ? (
          <NoSource what="数据源健康" why={errText(settings.value, "settings 未返回数据源清单")} />
        ) : (
          <>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))",
                gap: 12,
              }}
            >
              {healthRows.map((row) => (
                <Card key={row.name} size="small">
                  <Space size={8} style={{ width: "100%", justifyContent: "space-between" }}>
                    <Text strong style={{ fontSize: 12 }}>
                      {row.name}
                    </Text>
                    <Badge
                      status={row.ok ? "success" : "warning"}
                      text={<span style={{ fontSize: 12 }}>{row.ok ? "正常" : "未配置"}</span>}
                    />
                  </Space>
                  <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 6 }}>
                    {row.detail}
                  </Text>
                </Card>
              ))}
            </div>
            {probeData && probeData.checked_at ? (
              <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 10 }}>
                真实数据源探测 {Array.isArray(probeData.sources) ? probeData.sources.length : 0} 项 · 检查于{" "}
                {fmt.stamp(probeData.checked_at)} · ok {probeData.summary ? fmt.dash(probeData.summary.ok) : "—"} /
                warn {probeData.summary ? fmt.dash(probeData.summary.warn) : "—"} / fail{" "}
                {probeData.summary ? fmt.dash(probeData.summary.fail) : "—"} · 来源 workbench sources（/api/v3/brain）
              </Text>
            ) : null}
            <Text type="secondary" style={{ fontSize: 11, display: "block", marginTop: 6 }}>
              来源 /api/v3/settings（富途 / AKShare / SEC EDGAR / Tushare 探测）+ /api/v3/metrics（workbench
              工具面）· 全局口径（不按市场拆分）· 未配置的数据源按「未配置」如实展示，不估算可用性
            </Text>
          </>
        )}
      </ProCard>
    </Space>
  );
}
