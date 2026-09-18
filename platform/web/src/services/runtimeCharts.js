// 流程页 / 调度页 / 执行页三张新图表的派生（第三批图表，2026-09-18 用户需求：「在适当的地方
// 增加一些图表的展示」）。本模块只做**纯函数**：`端点载荷 → 图表项`，页面 JSX 只负责渲染。
//
// 为什么派生单独一层（与第一批 optionScreen.js、第二批 portfolioCharts.js 同一分工）：
// canvas 在 node 里画不出来，「哪条该画、哪条被跳过、时间轴的域是谁」是唯一能在 CI 里锁住
// 的地方。前两批的教训是空柱状图看不出原因、缺值被当成 0，所以这里每个函数都**返回跳过
// 计数与名字**，由页面原样写出。
//
// 数据形状依据（2026-09-19 对 127.0.0.1:8397 实测，命令与响应摘录见本批 commit message）：
//
//   POST /api/wb/pipeline → {date, markets:{SH|HK|US:{stages}}, global:{stages}, ...}
//     每阶段 {label, status, at, scheduled, summary}（pipeline.py:255-290）：
//       * `scheduled` 实测是**当日时刻** `"16:00"`（来自 autopipeline.build_jobs 的 job["at"]），
//         必须配 `payload.date` 才能定位到具体毫秒——**不能用「现在」的日期**，否则页面重看
//         历史日子时整根轨道会平移到错误的一天；
//       * `at` 实测是**完整日期时间** `"2026-09-18 20:50:24"`（kv daemon:state 的 ran 值 /
//         plans.created_at），不是 "HH:MM:SS"：它自带日期，**原样使用**，套当日基准日反而错
//         （实测 SH 的 plan 阶段 at 落在 2026-09-18，而 payload.date 是 2026-09-19）；
//       * 两者都可能为 null（实测 SH.execute 与 GLOBAL.digest）：**都不画在轨道上**，
//         如实计数 + 点名（把「没有时刻」画成「从 0 点开始」就是编数据）。
//
//   POST /api/wb/schedule → {heartbeat, critical, kill, halt, jobs:[{job, ran}]}
//     `job` 键形如 `"SH:build_plan:2026-09-18"` / `"GLOBAL:reconcile:2026-09-18"`（daemon.py:99），
//     `ran` 是完整日期时间；**没有计划时刻字段**（心跳里的 next 是 "见 trading-platform.json"
//     这样的说明串，不是时刻）。故调度页的时间轴只有「实际运行时刻」这一个维度，每项画成
//     瞬时点；作业名一律取流程端点给的中文阶段名（服务端 _JOB_LABELS），**前端不自建映射表**。
//
//   POST /api/wb/snapshot → value.trade_summary.orders[]（execution.jsx 的「券商订单」表）
//     实测行字段：`side` 是服务端按工具 schema 给出的**中文**（"买入"/"卖出"）、`side_code`
//     是数字码（1/2）、`fill` 是**中文成交情况**（"全部成交"/"部分成交"/"未成交"/"未知"，
//     由 qty 与 cum_qty 自证推导）、`status_code` 是**枚举未公开**的券商原码（实测 4/5）。
//   POST /api/wb/orders_open|orders_history → {groups:[{acc_id, market, rows:[...]}]}，
//     行是券商原始 REST 字段：`side` 数字码 1/2、`status` 数字原码（实测 4/5），无中文。
//     两种信封都支持（见 `orderRows`），谁喂进来都算得对；页面喂哪一份由页面决定。
import { finiteNumber, toTimestamp } from "../charts/scale.js";
import { isAllMarkets } from "./marketFilter.js";
import { MARKETS, marketLabel } from "./pipeline.js";

/** 全局链键（与服务端 pipeline.GLOBAL_CHAIN 同一事实）。 */
const GLOBAL_CHAIN = "GLOBAL";
/** 全局链在行名里的前缀（页面卡片叫「全局（晚间链）」，这里取短名以免挤占轨道宽度）。 */
const GLOBAL_LABEL = "全局";

/** `"HH:MM"` / `"H:MM"` / `"HH:MM:SS"`（当日时刻，需要基准日才能定位）。 */
const TIME_OF_DAY = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/;
/** `"YYYY-MM-DD"`（服务端 payload.date 的形状）。 */
const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;

function pad2(value) {
  return String(value).padStart(2, "0");
}

/** 本地日期串（跨日判定的唯一口径，与 `toTimestamp` 的本地时区解释同源）。 */
function localDayOf(stamp) {
  const date = new Date(stamp);
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

/**
 * `payload.date` → `{text, stamp}`；缺失/非法/不存在的日期（`2026-02-31` 会被 Date 滚成
 * 3 月 3 日）一律返回 null——**宁可说「定位不了」，不拿一个错日期顶替**。
 */
function parseDateOnly(value) {
  if (typeof value !== "string") return null;
  const text = value.trim();
  const match = DATE_ONLY.exec(text);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const probe = new Date(year, month - 1, day);
  if (!Number.isFinite(probe.getTime())) return null;
  if (probe.getFullYear() !== year || probe.getMonth() !== month - 1 || probe.getDate() !== day) {
    return null;
  }
  return { text, stamp: probe.getTime() };
}

/**
 * 阶段时刻归一：`raw` 可能是当日时刻（`"16:00"`）、完整日期时间（`"2026-09-18 20:50:24"`）、
 * 毫秒数或 `Date`（见 `toTimestamp`）。
 *
 * @returns {{ts: number|null, timeOfDay: boolean, unresolved: boolean}}
 *   `unresolved` = 有值但定位不到（当日时刻缺基准日、或字符串根本解析不了）——它与
 *   「字段就是 null」是两回事，页面要为它给出**不同的原因**。
 */
function resolveMoment(raw, dateText) {
  if (raw === null || raw === undefined) return { ts: null, timeOfDay: false, unresolved: false };
  const text = String(raw).trim();
  if (text === "") return { ts: null, timeOfDay: false, unresolved: false };
  const match = TIME_OF_DAY.exec(text);
  if (match) {
    if (!dateText) return { ts: null, timeOfDay: true, unresolved: true };
    const stamp = toTimestamp(`${dateText}T${pad2(match[1])}:${match[2]}:${match[3] ?? "00"}`);
    return { ts: stamp, timeOfDay: true, unresolved: stamp === null };
  }
  const stamp = toTimestamp(text);
  return { ts: stamp, timeOfDay: false, unresolved: stamp === null };
}

/** 时间轴项的悬停说明：**同时**写出计划时刻与实际时刻，再接阶段摘要（brief 明确要求）。 */
function pipelineNote(stage, actual, dateText) {
  const planned = stage.scheduled === null || stage.scheduled === undefined
    ? "" : String(stage.scheduled).trim();
  const real = stage.at === null || stage.at === undefined ? "" : String(stage.at).trim();
  const parts = [];
  if (planned && real) parts.push(`计划 ${planned} → 实际 ${real}`);
  else if (planned) parts.push(`计划 ${planned}（当日尚无实际执行时刻）`);
  else if (real) parts.push(`实际 ${real}（无计划时刻）`);
  else parts.push("无计划时刻、也无实际时刻（未画上轨道）");
  // 实测 SH.plan 的 at 落在 payload.date 的前一天：不点明的话，读者会以为当日晚间跑过
  if (real && actual.ts !== null && dateText && localDayOf(actual.ts) !== dateText) {
    parts.push(`该时刻不在 ${dateText} 当日`);
  }
  const summary = stage.summary === null || stage.summary === undefined
    ? "" : String(stage.summary).trim();
  if (summary) parts.push(summary);
  return parts.join("；");
}

/**
 * 流程页「当日作业链时间轴」：把 pipeline 端点的阶段行变成时间轴项。
 *
 * 项形状 `{label, start, end, status, note}`：
 *   * `start` = **计划时刻**（`scheduled` 按 `payload.date` 定位），`end` = **实际时刻**
 *     （`at` 原样解析）——两者都有时，时间条的长度就是「计划 → 实际」的**漂移**；
 *   * 只有一边有值时两端相同（瞬时点，由 TimelineChart 画成最小 2.5px 的条）；
 *   * 两边都没有 → `start = end = null`（`timelineLayout` 判 `valid=false`，组件不画）；
 *   * `status` 原样透传（ok 绿 / failed 红 / skipped 灰 / pending 蓝，组件按状态色画）；
 *   * `note` = 「计划 x → 实际 y」+ 摘要，悬停可见。
 *
 * `opts.market`：当前市场筛选（`marketFilter.MARKET_ALL` / 具体市场链）。选中具体市场时只
 * 产出该市场链 + GLOBAL（GLOBAL 作业不属于任何单个市场，藏掉它等于让全局对账/资讯作业
 * 消失，与流程页既有卡片同一口径）；「全部市场」产出 GLOBAL + SH/HK/US，顺序与页面卡片一致。
 *
 * @param {object|null} payload pipeline 端点的 `value`
 * @returns {{items: Array, skipped: number, skippedLabels: string[], crossDay: number,
 *   note: string|null}} `crossDay` = 实际时刻不落在 `payload.date` 当日的项数（实测存在）
 */
export function pipelineTimelineItems(payload, { market } = {}) {
  const value = payload && typeof payload === "object" ? payload : null;
  if (!value) {
    return { items: [], skipped: 0, skippedLabels: [], crossDay: 0, note: "流程快照尚未读取或为空。" };
  }
  const date = parseDateOnly(value.date);
  const dateText = date?.text ?? null;

  const chains = [{ key: GLOBAL_CHAIN, label: GLOBAL_LABEL, stages: value?.global?.stages }];
  for (const key of MARKETS) {
    if (!isAllMarkets(market) && key !== market) continue;
    chains.push({ key, label: marketLabel(key), stages: value?.markets?.[key]?.stages });
  }

  const items = [];
  const skippedLabels = [];
  let crossDay = 0;
  let unresolved = 0;
  let stageCount = 0;
  for (const chain of chains) {
    const stages = chain.stages;
    if (!stages || typeof stages !== "object") continue;
    for (const [key, raw] of Object.entries(stages)) {
      stageCount += 1;
      const stage = raw && typeof raw === "object" ? raw : {};
      const label = `${chain.label} · ${stage.label ?? key}`;
      const scheduled = resolveMoment(stage.scheduled, dateText);
      const actual = resolveMoment(stage.at, dateText);
      if (scheduled.unresolved || actual.unresolved) unresolved += 1;
      let start = null;
      let end = null;
      if (scheduled.ts !== null && actual.ts !== null) { start = scheduled.ts; end = actual.ts; }
      else if (scheduled.ts !== null) { start = scheduled.ts; end = scheduled.ts; }
      else if (actual.ts !== null) { start = actual.ts; end = actual.ts; }
      if (start === null) skippedLabels.push(label);
      if (actual.ts !== null && dateText && localDayOf(actual.ts) !== dateText) crossDay += 1;
      items.push({
        label,
        start,
        end,
        status: stage.status,
        note: pipelineNote(stage, actual, dateText),
      });
    }
  }

  let note = null;
  if (stageCount === 0) {
    note = "流程快照里没有任何阶段（markets/global 为空）。";
  } else if (unresolved > 0) {
    note = `流程快照的日期不可用（date=${value.date ?? "—"}）：${unresolved} 个阶段只有 HH:MM 计划时刻，`
      + "无法定位到当日，未画上轨道（不拿「现在」的日期顶替）。";
  }
  return { items, skipped: skippedLabels.length, skippedLabels, crossDay, note };
}

/** 调度作业键 `"SH:build_plan:2026-09-18"` → `{market, jobName, date}`；不成三段的原样当作业名。 */
function parseJobKey(jobKey) {
  const text = String(jobKey ?? "").trim();
  if (!text) return { market: null, jobName: "", date: null };
  const parts = text.split(":");
  if (parts.length === 3) return { market: parts[0], jobName: parts[1], date: parts[2] };
  return { market: null, jobName: text, date: null };
}

/** 流程快照里的阶段中文名索引：`"市场|作业名"` → label（GLOBAL 用 `"GLOBAL|作业名"`）。 */
function stageLabelIndex(pipeline) {
  const index = new Map();
  const value = pipeline && typeof pipeline === "object" ? pipeline : null;
  if (!value) return index;
  const collect = (chain, stages) => {
    if (!stages || typeof stages !== "object") return;
    for (const [key, stage] of Object.entries(stages)) {
      const label = stage && typeof stage === "object" ? stage.label : null;
      if (label) index.set(`${chain}|${key}`, String(label));
    }
  };
  collect(GLOBAL_CHAIN, value?.global?.stages);
  for (const key of MARKETS) collect(key, value?.markets?.[key]?.stages);
  return index;
}

/**
 * 调度页「当日作业时刻表」：把 schedule 端点的 `jobs[]` 变成时间轴项。
 *
 * 与流程页的差别（**实测**）：调度端点只有 `ran`（实际运行时刻，完整日期时间），**没有**
 * 计划时刻字段——所以每项都是瞬时点，本函数不编造计划时刻，也不去比对流程端点的
 * `scheduled`（作业键自带日期，实测是 2026-09-18 而快照日期是 2026-09-19；拿昨天的实际
 * 比今天的计划就是编数据）。
 *
 * `jobs` 必须是**页面上那张作业表用的同一份行**（`viewScheduleJobs(...)` 过滤后）：
 * 图与表同源，本函数不再自己过滤市场。
 *
 * @param {Array} jobs `[{job, ran}]`
 * @param {{pipeline?: object}} [opts] 流程端点 `value`，只用来取**中文阶段名**（服务端
 *   `_JOB_LABELS`）；缺省时行名回落为作业键里的原始作业名，并返回一条说明，不静默。
 * @returns {{items: Array, skipped: number, skippedLabels: string[], note: string|null}}
 *   `items` 按实际时刻升序（时间轴自上而下读就是当天执行顺序）；缺时刻的项
 *   `start/end = null`，排在末尾并由组件如实计数。
 */
export function scheduleTimelineItems(jobs, { pipeline } = {}) {
  const list = Array.isArray(jobs) ? jobs : [];
  const labels = stageLabelIndex(pipeline);
  const items = [];
  const skippedLabels = [];
  for (const row of list) {
    const jobKey = row && typeof row === "object" && row.job !== null && row.job !== undefined
      ? String(row.job) : "";
    const { market, jobName } = parseJobKey(jobKey);
    const prefix = market === GLOBAL_CHAIN ? GLOBAL_LABEL : (market ? marketLabel(market) : "");
    const stageName = labels.get(`${market ?? ""}|${jobName}`) ?? jobName ?? "—";
    const label = prefix ? `${prefix} · ${stageName}` : stageName;
    const stamp = row && typeof row === "object" ? toTimestamp(row.ran) : null;
    if (stamp === null) skippedLabels.push(label);
    items.push({
      label,
      start: stamp,
      end: stamp,
      // 调度端点没有状态字段：**不编**（组件的悬停会如实显示「未运行（无状态字段）」）
      status: undefined,
      note: `作业键 ${jobKey || "—"}`,
    });
  }
  // 排序：有时刻的按时刻升序（并列保持输入顺序），缺时刻的保持在末尾的原始相对顺序
  const ordered = items
    .map((item, index) => ({ item, index }))
    .sort((left, right) => {
      const a = left.item.start;
      const b = right.item.start;
      if (a === null && b === null) return left.index - right.index;
      if (a === null) return 1;
      if (b === null) return -1;
      return (a - b) || (left.index - right.index);
    })
    .map((entry) => entry.item);
  const note = items.length > 0 && labels.size === 0
    ? "流程快照未读到，行名回落为作业键里的原始作业名（不自建中文映射表）。"
    : null;
  return { items: ordered, skipped: skippedLabels.length, skippedLabels, note };
}

// ---------------------------------------------------------------------------
// 执行页「订单分布」
// ---------------------------------------------------------------------------
// 三条口径：
//   1. **实测字段优先、候选键防御式读取**：券商原始 REST 与快照归纳面的字段名不同
//      （`status`/`order_status`/`status_code`、`side`/`side_code`/`trd_side`），命中第一个
//      非 null 的候选键——与 execution.jsx 表格的 `pickField` 同一策略；
//   2. **不猜枚举**：`status` / `status_code` 是券商**未公开枚举**的原码，一律**原样**
//      计数并原样当标签（服务端既没有给出映射表，前端也就不编中文）；
//   3. **方向码归一**：`side` 的 1/2 与 "BUY"/"SELL" 归一到中文，依据是
//      `platform/server/labels.py` 的 `SIDE = {1: "买入", 2: "卖出"}`（前端对应物
//      `plugins/workbench/src/labels.js`）与 broker_trades.js 的「按工具 schema 给中文方向」
//      ——**不是自造映射**；未知值（`3`、"MARGIN"）原样展示，便于发现新枚举。
/** 方向码 → 中文（镜像 `platform/server/labels.py` 的 SIDE 表）。 */
const SIDE_CODES = { 1: "买入", 2: "卖出" };
/** 方向字面量 → 中文（BUY/SELL 与服务端已给的中文都归一到同一个桶）。 */
const SIDE_WORDS = { BUY: "买入", SELL: "卖出", "买入": "买入", "卖出": "卖出" };

const STATUS_KEYS = ["order_status", "status", "status_code"];
const FILL_KEYS = ["fill", "fill_status"];
const SIDE_KEYS = ["trd_side", "side", "side_code", "order_side"];

/** 取第一个非 null/undefined 的候选键值；一个都没有 → null（缺失不是空串也不是 0）。 */
function pickField(row, keys) {
  for (const key of keys) {
    if (row[key] !== null && row[key] !== undefined) return row[key];
  }
  return null;
}

/** 状态/成交情况标签：原样文本（数字原码也按原码文本）；缺失/空串/对象 → null。 */
function plainLabel(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "object") return null;
  const text = String(value).trim();
  return text === "" ? null : text;
}

/** 方向标签：数字码按 labels.py SIDE 归一，BUY/SELL（任意大小写）与中文归一到中文，未知原样。 */
function sideLabel(value) {
  if (value === null || value === undefined) return null;
  if (typeof value !== "object") {
    const code = finiteNumber(value);
    if (code !== null && SIDE_CODES[code] !== undefined) return SIDE_CODES[code];
  }
  const text = plainLabel(value);
  if (text === null) return null;
  return SIDE_WORDS[text.toUpperCase()] ?? text;
}

/** 计数 → 降序分布项（并列按标签升序，保证同一份数据每次画出来的顺序一致）。 */
function tally(labels) {
  const counts = new Map();
  for (const label of labels) counts.set(label, (counts.get(label) ?? 0) + 1);
  return [...counts.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => (b.value - a.value)
      || (a.label < b.label ? -1 : (a.label > b.label ? 1 : 0)));
}

/**
 * 支持的载荷形状（三种实测信封都能直接喂）：
 *   * `{orders: [...]}` —— snapshot 的 `trade_summary`（券商订单表的来源）；
 *   * `{groups: [{acc_id, market, rows: [...]}]}` —— OpenAPI 订单端点（orders_open /
 *     orders_history），`rows` 非数组的组忽略；
 *   * 行数组本身；非对象元素（字符串等）忽略。
 */
function orderRows(payload) {
  if (Array.isArray(payload)) return payload;
  if (!payload || typeof payload !== "object") return [];
  if (Array.isArray(payload.orders)) return payload.orders;
  if (Array.isArray(payload.groups)) {
    return payload.groups.flatMap((group) => (Array.isArray(group?.rows) ? group.rows : []));
  }
  if (Array.isArray(payload.rows)) return payload.rows;
  return [];
}

/**
 * 订单行 → 分布项（执行页「订单分布」图）。
 *
 * 返回四个分组（**各自独立计数**，缺字段的行只计入它缺的那一维，三种跳过数不混算）：
 *   * `byStatus` —— 券商状态：`order_status` / `status` / `status_code` 第一个非空，**原码原样**
 *     （实测 4/5；枚举未公开，不猜标签——与表格「状态」列同一口径）；
 *   * `byFill` —— 成交情况：`fill`（服务端由 qty/cum_qty 自证推导的中文），只有 snapshot 面
 *     有这个字段；OpenAPI 原始行没有 → 空数组（页面据此不渲染这张图）；
 *   * `bySide` —— 买卖方向（数字码与 BUY/SELL 归一到中文）；
 *   * `total` —— 参与统计的**订单行数**（页面用它写「当前 N 条订单」）；
 *   * `skipped` —— 三个维度**都没有**的行数（一条都没画进任何图）。
 *
 * @param {Array} payloads 订单类端点的 `value` 数组（可混不同端点；行不合并去重，
 *   调用方不要把**同一批订单**的两个来源同时喂进来，否则会被计两次）
 * @returns {{byStatus: Array, byFill: Array, bySide: Array, total: number, skipped: number,
 *   skippedStatus: number, skippedFill: number, skippedSide: number}}
 */
export function orderDistribution(payloads) {
  const list = Array.isArray(payloads) ? payloads : [];
  const rows = list.flatMap(orderRows)
    .filter((row) => row && typeof row === "object" && !Array.isArray(row));
  const statuses = [];
  const fills = [];
  const sides = [];
  let skipped = 0;
  let skippedStatus = 0;
  let skippedFill = 0;
  let skippedSide = 0;
  for (const row of rows) {
    const status = plainLabel(pickField(row, STATUS_KEYS));
    const fill = plainLabel(pickField(row, FILL_KEYS));
    const side = sideLabel(pickField(row, SIDE_KEYS));
    if (status === null) skippedStatus += 1; else statuses.push(status);
    if (fill === null) skippedFill += 1; else fills.push(fill);
    if (side === null) skippedSide += 1; else sides.push(side);
    if (status === null && fill === null && side === null) skipped += 1;
  }
  return {
    byStatus: tally(statuses),
    byFill: tally(fills),
    bySide: tally(sides),
    total: rows.length,
    skipped,
    skippedStatus,
    skippedFill,
    skippedSide,
  };
}
