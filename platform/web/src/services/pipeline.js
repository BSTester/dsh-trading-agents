// 流程页展示纯函数（WP10 任务 3）：阶段状态映射、下钻目标、时间与市场文案。
// 本模块只映射服务端给的值——**不新增、不推断、不重排阶段**。事实来源（可逐行核对）：
//   platform/server/app.py 的 pipeline 端点 → trading_core.pipeline.pipeline_snapshot
//   （plugins/core/python/trading_core/pipeline.py:264）：
//     {date, markets: {SH|HK|US: {stages}}, global: {stages}, auto_pipeline, kill, halt, alerts}
//   每阶段 {label, status, at, scheduled, summary}：
//     label —— 中文名由服务端 _JOB_LABELS 给出（未登记作业回退英文作业名），前端不自建映射表；
//     status —— {ok, pending, skipped, failed}（pipeline.py 文件头「阶段状态口径」，
//               无证据一律 pending，页面不得把它显示成成功）；
//     at / scheduled —— 实际执行时刻 / 计划时刻（二者互斥，见 pipeline.py _stage 与
//               _market_stages 的 scheduled 赋值）。
//   阶段 key = 真实作业名或表驱动阶段（plan/execute/digest）；**顺序即作业链顺序**
//   （Python dict 保序 → JSON → Object.entries 对字符串键保序），本模块不排序。
// 状态到 antd Steps 的映射与下钻目标在此单点定义，供 node --test 直测。

/** 与 pipeline.MARKETS 同一集合（服务端只返回这三个市场键）。 */
export const MARKETS = ["SH", "HK", "US"];

/** 市场中文标签；未登记的回退为键本身（看到英文键即「这里还没登记标签」）。 */
const MARKET_LABELS = { SH: "A股 SH", HK: "港股 HK", US: "美股 US" };
export function marketLabel(market) {
  return MARKET_LABELS[market] ?? String(market ?? "—");
}

/** 阶段状态 → antd Steps 的 status；未知/缺失回落 wait（不猜成功）。 */
const STEPS_STATUS = { ok: "finish", pending: "wait", skipped: "wait", failed: "error" };
export function stageStatus(status) {
  return STEPS_STATUS[status] ?? "wait";
}

/** 阶段状态 → Tag 颜色（与调度页共用同一套三档语义色）。 */
const TAG_COLOR = { ok: "green", failed: "red", skipped: "default", pending: "default" };
export function stageTagColor(status) {
  return TAG_COLOR[status] ?? "default";
}

/** 阶段状态中文文案；未知状态原样回显状态码（不编词），空值 → —。 */
const STATUS_TEXT = { ok: "已完成", pending: "待运行", skipped: "已跳过", failed: "失败" };
export function stageStatusText(status) {
  if (status === undefined || status === null || status === "") return "—";
  return STATUS_TEXT[status] ?? String(status);
}

/** 下钻目标（哈希路由，key 与 app.jsx 的 PAGES 同集合）：阶段 → 该事实的来源页。 */
const DRILL = { plan: "plan", execute: "execution", digest: "audit", reconcile: "audit" };
export function stageDrill(key) {
  return `#/${DRILL[key] ?? "schedule"}`;
}

/** 阶段时间文案：实际执行时刻优先，其次计划时刻；都没有 → —（不拿当前时间冒充）。 */
export function stageTimeText(stage) {
  if (stage?.at) return String(stage.at);
  if (stage?.scheduled) return `计划 ${stage.scheduled}`;
  return "—";
}

/** 阶段字典 → 有序条目数组（保序；null/非对象 → 空数组，页面显示「无阶段」）。 */
export function stageEntries(stages) {
  if (!stages || typeof stages !== "object") return [];
  return Object.entries(stages).map(([key, stage]) => ({ key, ...(stage ?? {}) }));
}

/** auto_pipeline 摘要 → 徽章文案与颜色；非法配置如实报「配置非法」，不显示为关闭。 */
export function autoPipelineBadge(config) {
  if (config?.error) return { text: "配置非法", color: "red" };
  if (config?.enabled === true) return { text: "自动执行已开启", color: "green" };
  if (config?.enabled === false) return { text: "自动执行关闭", color: "default" };
  return { text: "—", color: "default" };
}

// ---------------------------------------------------------------------------
// 设置页「自动流水线」草稿（WP10 任务 3）
// ---------------------------------------------------------------------------
// 键集唯一事实源 = 服务端载荷白名单 settings_api.AUTO_PIPELINE_FIELDS（app.py:160 同名
// 常量）；服务端对未知键**直接拒绝**，所以草稿与载荷都必须恒等于这五个键——多带一个
// 只读字段（如 error/date）就会让保存失败，这正是下面两个纯函数存在的理由。
//   GET auto_pipeline → 有效配置（缺省补全后的完整结构；非法配置含 error 字段）
//   POST auto_pipeline ← 五键载荷（校验复用 trading_core.autopipeline.apply_overlay，
//                        与调度侧同一实现；失败文件零改动）

/** auto_pipeline 载荷白名单键（顺序即草稿字段顺序）。 */
export const AUTO_PIPELINE_KEYS = [
  "enabled", "strategies", "exec_at", "exec_window_minutes", "reconcile_at",
];

/** 缺省值镜像 trading_core.autopipeline.AUTO_PIPELINE_DEFAULTS（首屏未读回前的占位）。 */
export const AUTO_PIPELINE_DEFAULTS = {
  enabled: false,
  strategies: [],
  exec_at: { SH: "09:35", HK: "09:45", US: "22:35" },
  exec_window_minutes: 30,
  reconcile_at: "19:00",
};

/** 服务端有效配置 → 可编辑草稿：只保留白名单键，缺失补默认（error 等只读字段不带）。 */
export function autoPipelineDraft(config) {
  const src = config && typeof config === "object" ? config : {};
  const execAt = src.exec_at && typeof src.exec_at === "object" ? src.exec_at : {};
  const strategies = Array.isArray(src.strategies) ? src.strategies : [];
  return {
    enabled: src.enabled === true,
    strategies: strategies.map((row) => ({
      market: row?.market ?? "",
      strategy: row?.strategy ?? "",
      watchlist: row?.watchlist ?? "watchlist",
    })),
    exec_at: Object.fromEntries(MARKETS.map(
      (market) => [market, execAt[market] ?? AUTO_PIPELINE_DEFAULTS.exec_at[market]])),
    exec_window_minutes: Number.isInteger(src.exec_window_minutes)
      ? src.exec_window_minutes : AUTO_PIPELINE_DEFAULTS.exec_window_minutes,
    reconcile_at: typeof src.reconcile_at === "string" && src.reconcile_at
      ? src.reconcile_at : AUTO_PIPELINE_DEFAULTS.reconcile_at,
  };
}

/** 草稿 → POST 载荷：键集恒等于白名单（草稿里的任何多余字段都不会漏给服务端）。 */
export function autoPipelinePayload(draft) {
  const out = {};
  for (const key of AUTO_PIPELINE_KEYS) out[key] = draft?.[key];
  return out;
}

/** 新增策略行的初值（与 watchlist_rsi 订阅口径一致：市场 + 策略 + 池键）。 */
export function newStrategyRow() {
  return { market: "SH", strategy: "watchlist_rsi", watchlist: "watchlist" };
}
