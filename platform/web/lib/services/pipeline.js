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
//
// 设置页的自动流水线接线只做渲染：策略的取值域在 ``services/strategies.js``，池键与
// 时刻（字符串 ↔ dayjs）转换在本文件（WP22，均为纯函数、node --test 直测）。
import dayjs from "dayjs";

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

/** 下钻目标（哈希路由，key 与 app.jsx 的 PAGES 同集合）：阶段 → 该事实的来源页。
 *
 * **必须覆盖服务端可能给出的全部阶段 key**（2026-09-18 实机缺陷）：原表只登记了 4 个
 * （plan/execute/digest/reconcile），其余 11 个一律回落 `#/schedule`——于是点「行情同步」
 * 「因子快照」「研究快照」「日历同步」等节点全部跳到调度页，用户看到的是「点击节点没有
 * 正确跳转」。回落分支还在，但它现在只服务「服务端新增了未登记阶段」这一种情况，
 * 而 `tests/test_wp21_drill_lock.py` 会拿服务端 `pipeline._JOB_LABELS` 与 `app.jsx` 的
 * PAGES 键做双向核对：**新增阶段却没登记下钻目标 → 测试直接失败**，不会再静默跳错页。
 *
 * 归属理由（每一条都指到页面上真实存在的卡片）：
 *   sync_calendar        → 调度（作业表/日历覆盖告警）
 *   sync_bars            → 行情（K 线序列的取数来源）
 *   sync_fundamentals    → 研究（深度数据 F10 / 财报）
 *   merge_announcements  → 研究（F10 公告日）
 *   quality              → 因子（因子页即消费 quality 端点）
 *   factors_snapshot     → 因子（因子打分与排序）
 *   sentiment_snapshot   → 因子（情绪快照采集摘要）
 *   research_snapshot    → 研究（研报正文/候选池）
 *   enqueue_research     → 研究（研究任务入队）
 *   build_plan / plan    → 计划（当前计划/状态时间线）
 *   auto_execute/execute → 执行（订单与指令结局）
 *   reconcile / digest   → 审计（对账差异与链路统计；digest 是当日链路摘要）
 */
const DRILL = {
  sync_calendar: "schedule",
  sync_bars: "market",
  sync_fundamentals: "research",
  merge_announcements: "research",
  quality: "factors",
  factors_snapshot: "factors",
  sentiment_snapshot: "factors",
  research_snapshot: "research",
  enqueue_research: "research",
  build_plan: "plan",
  plan: "plan",
  auto_execute: "execution",
  execute: "execution",
  reconcile: "audit",
  digest: "audit",
};
export function stageDrill(key) {
  return `#/${DRILL[key] ?? "schedule"}`;
}

/** 已登记下钻目标的阶段 key（供跨语言漂移锁测试读取；不要在前端逻辑里当契约用）。 */
export const DRILL_KEYS = Object.keys(DRILL);

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

/** auto_pipeline 摘要 → 徽章文案与颜色；非法配置如实报「配置非法」，不显示为关闭。
 *
 * ``error`` = 语义非法（``apply_overlay`` 拒绝）；``config_error`` = 文件级不可解析
 * （JSON 坏，core 按空配置容错但摘要如实标注，见 pipeline.py ``_config_unparsable``）。
 * 两者都必须是红色「配置非法」——否则坏文件在页面上与「功能没开」长得一样。 */
export function autoPipelineBadge(config) {
  if (config?.error || config?.config_error) return { text: "配置非法", color: "red" };
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

/** 执行窗口上界，镜像 trading_core.autopipeline.EXEC_WINDOW_MAX_MINUTES。
 *
 * 这里是**镜像**而非独立常量：设置页 InputNumber 的 max 用它，core 用同一语义值拒绝
 * 越界配置。两侧漂移会让「页面允许填的值被服务端拒绝」——由 tests/test_wp10_locks.py
 * 解析比对（N1/N2），不靠「记得同步」。 */
export const EXEC_WINDOW_MAX_MINUTES = 240;

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

// ---------------------------------------------------------------------------
// 关注池键与时刻字段（WP22：从自由文本改成选择器）
// ---------------------------------------------------------------------------

/** 缺省池键，镜像 ``trading_core.watchlist.DEFAULT_POOL_KEY``（由
 *  tests/test_wp22_settings_options.py 解析比对，不靠「记得同步」）。
 *
 *  池键 = ``trading-platform.json`` 顶层的**列表型键**（既有扁平口径）；键不存在时
 *  ``watchlist_symbols`` 对非缺省池 fail-closed（当日跳过 + 告警），缺省池不存在视为空池。 */
export const POOL_KEY_DEFAULT = "watchlist";

/** 池键下拉选项：缺省池恒在，其余取当前策略行用到的键（去重保序）。
 *
 * 池键没有专门端点（它是配置文件里的顶层列表键），所以「可选项」只能来自：
 * 缺省池 + 本次草稿里已经用到的键。下拉允许自由输入新键（antd Select 的 tags 模式），
 * 因此这里给的是**建议集合**而不是白名单。
 * @param {{strategies?: Array<{watchlist?: unknown}>}|null|undefined} draft 草稿或有效配置
 * @returns {Array<{value: string, label: string}>}
 */
export function poolKeyOptions(draft) {
  const keys = [POOL_KEY_DEFAULT];
  for (const row of draft?.strategies ?? []) {
    const key = typeof row?.watchlist === "string" ? row.watchlist.trim() : "";
    if (key && !keys.includes(key)) keys.push(key);
  }
  return keys.map((key) => ({
    value: key,
    label: key === POOL_KEY_DEFAULT ? `${key}（默认池）` : key,
  }));
}

/** ``"HH:MM"`` → dayjs（TimePicker 的 value）；不可解析 → null（**不猜时刻**）。
 *
 * 只认服务端校验口径（``autopipeline._hhmm``：``^\d{2}:\d{2}$`` 且 00-23/00-59；
 * 这里额外容忍 ``"9:05"`` 这种历史写法，格式化时会补零）。返回 null 时 picker 显示
 * 占位符，页面同时给出「当前值不是 HH:MM」提示——不把坏值伪装成合法值。
 *
 * 不用 ``dayjs(str, "HH:mm")``：那需要 customParseFormat 插件；这里自行拆解小时/分钟，
 * 依赖面更小（日期部分取当天，TimePicker 只显示时刻）。 */
export function hhmmToTime(value) {
  if (typeof value !== "string") return null;
  const match = /^(\d{1,2}):(\d{2})$/.exec(value.trim());
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (hour > 23 || minute > 59) return null;
  return dayjs().hour(hour).minute(minute).second(0).millisecond(0);
}

/** dayjs（TimePicker 的 onChange 值）→ ``"HH:MM"``；非 dayjs（null/undefined/字符串）→ ``""``。
 *
 * **保存写回字符串**：dayjs 对象进不了 JSON 载荷（会变成 ISO 串，被服务端
 * ``_hhmm`` 拒绝）。``""`` 一律不可保存（服务端会如实拒绝），因此页面不得把它当合法值。 */
export function timeToHhmm(value) {
  if (!value || typeof value.format !== "function") return "";
  return value.format("HH:mm");
}
