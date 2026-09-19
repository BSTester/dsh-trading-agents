// 数据面展示纯函数（WP12 任务 6）：F10 三 section 与衍生品两 section 的摘要映射，
// 以及通道不可用时的「原因 + 下一步」指引。宿主无关（React/antd 不进本模块），
// 供 platform/web/tests/*.test.mjs 用 node --test 直测。
//
// 字段依据（每个取值路径都能指到官方文档或锁定表，**不猜字段名**）：
//   analyst_consensus → 官方 /zh-cn/api/quote/research/analyst-consensus：
//       rating(1=Sell/2=Underperform/3=Hold/4=Buy/5=Strong Buy), total,
//       strong_buy/buy/hold/underperform/sell(%), average/highest/lowest,
//       num_of_target_analysts, update_time_str。
//       **合法但无覆盖时 data 为空对象 {}**（官方「限制范围」原文）→ 必须显示「无覆盖」
//       而不是空白。
//   rating_summary → 官方 /zh-cn/api/quote/research/rating-summary：
//       pagination{has_more,next_key,total}, inst_rating_summary_list[].institution_info
//       {institution_name,...} + rating_item_list[]{rating(1=Sell/2=Hold/3=Buy),
//       target_price, recommendation_date_str}；analyst_rating_summary_list[].analyst_info
//       {analyst_name,num_of_stars,success_rate,excess_return}。
//       **仅 US/CA 有数据**（其它市场返回空列表）。
//       注意：本 section 的 rating 是 **3 档**（1/2/3），与 analyst_consensus 的 5 档
//       不是同一枚举——两套标签在下面分开定义，不合并。
//   institutional → 锁定表 §C.5：period_text, institution_quantity(_change),
//       holder_quantity(_change), holder_pct(_change), close_price, open_price,
//       last_close_price, update_time(_str)。
//   option_volatility → 锁定表 §C.7：timestamp, implied_volatility, history_volatility,
//       volatility_premium, average_impvol, impvol_status, analysis。
//   option_exercise_probability → 锁定表 §C.7：timestamp, security_price,
//       strike_probability（-9 = 用户无期权数据查询权限）。
//
// 展示纪律（架构 §「结论性字段」与索引 §2.4 文案白名单）：缺字段一律 —，绝不补默认值、
// 绝不用 0 冒充缺失；空结果与失败是两回事（空对象/空列表 = 上游合法无数据，要如实说明）。

/** 缺失值占位（与全站页面一致）。 */
export const MISSING = "—";

/** 数值格式化：非法/缺失 → —，不补 0。
 *
 * 千分位与小数位口径对齐 `services/format.jsx` 的 `num()`（同为 zh-CN 分组），
 * 但本模块**不 import 那个文件**：format.jsx 带 React/antd，node --test 无法直接加载，
 * 而这里的映射必须是可直测的纯函数（与 pipeline.js / sentiment.js 同一取舍）。
 */
export function fmtNum(value, digits = 2) {
  const number = typeof value === "string" ? Number(value) : value;
  if (typeof number !== "number" || !Number.isFinite(number)) return MISSING;
  return number.toLocaleString("zh-CN",
    { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** 百分比格式化：上游已是百分数（官方「比率：1.23 表示 1.23%」），只补 %，不再乘 100。 */
export function fmtPct(value, digits = 2) {
  const text = fmtNum(value, digits);
  return text === MISSING ? MISSING : `${text}%`;
}

/** 文本兜底：空串/null/undefined → —（0 是合法值，不吞）。 */
export function fmtText(value) {
  if (value === null || value === undefined || value === "") return MISSING;
  return String(value);
}

// ---------------------------------------------------------------------------
// 分析师一致预期（analyst_consensus，5 档评级）
// ---------------------------------------------------------------------------

/** analyst_consensus.rating 的五档语义（官方文档原文）。 */
const ANALYST_RATING_LABELS = {
  1: "Sell 卖出",
  2: "Underperform 跑输大盘",
  3: "Hold 持有",
  4: "Buy 买入",
  5: "Strong Buy 强烈买入",
};

/** 评级码 → 标签；未知码原样回显（不猜档位），缺失 → —。 */
export function analystRatingLabel(code) {
  if (code === null || code === undefined || code === "") return MISSING;
  return ANALYST_RATING_LABELS[code] ?? `rating=${code}`;
}

/** 空对象/空值判定：官方「无分析师覆盖」返回 data={}。 */
export function isEmptyObject(value) {
  return !!value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === 0;
}

/**
 * 分析师共识摘要 → `{ rows, note }`。
 * rows 为 `[{label, value}]`（页面直接渲染成键值行，不含样式）；
 * note 承载「合法但无数据」的如实说明（无覆盖），有数据时为 ""。
 */
export function analystConsensusSummary(value) {
  if (value === null || value === undefined) return { rows: [], note: "" };
  if (isEmptyObject(value)) {
    return { rows: [], note: "上游返回空对象：该标的当前无分析师覆盖。" };
  }
  const distribution = [
    ["强烈买入 strong_buy", value.strong_buy], ["买入 buy", value.buy],
    ["持有 hold", value.hold], ["跑输大盘 underperform", value.underperform],
    ["卖出 sell", value.sell],
  ].map(([label, raw]) => ([label, fmtPct(raw)]))
    // 官方：buy/underperform 仅部分市场返回 → 缺失档位不显示（不写 0）
    .filter(([, text]) => text !== MISSING);
  const rows = [
    { label: "综合评级", value: analystRatingLabel(value.rating) },
    { label: "覆盖分析师", value: fmtText(value.total) },
    ...distribution.map(([label, text]) => ({ label, value: text })),
    { label: "目标价区间", value: targetRange(value) },
    { label: "目标价分析师数", value: fmtText(value.num_of_target_analysts) },
    { label: "数据日期", value: fmtText(value.update_time_str) },
  ];
  return { rows, note: "" };
}

/** 目标价区间：最低–最高（平均另起一项）；三者都缺 → —。 */
function targetRange(value) {
  const low = fmtNum(value.lowest);
  const high = fmtNum(value.highest);
  const average = fmtNum(value.average);
  if (low === MISSING && high === MISSING && average === MISSING) return MISSING;
  const range = (low === MISSING || high === MISSING) ? MISSING : `${low} – ${high}`;
  if (range === MISSING) return average === MISSING ? MISSING : `平均 ${average}`;
  return average === MISSING ? range : `${range}（平均 ${average}）`;
}

// ---------------------------------------------------------------------------
// 评级汇总（rating_summary，3 档评级；仅 US/CA 有数据）
// ---------------------------------------------------------------------------

/** rating_summary.rating_item_list[].rating 的三档语义（官方文档原文）。 */
const RATING_ITEM_LABELS = { 1: "Sell 卖出", 2: "Hold 持有", 3: "Buy 买入" };

/** 评级条目码 → 标签；未知码原样回显，缺失 → —。 */
export function ratingItemLabel(code) {
  if (code === null || code === undefined || code === "") return MISSING;
  return RATING_ITEM_LABELS[code] ?? `rating=${code}`;
}

/** 取列表里第一条评级条目（官方每机构/分析师带 rating_item_list[]）。 */
function firstRatingItem(entry) {
  const items = Array.isArray(entry?.rating_item_list) ? entry.rating_item_list : [];
  return items[0] ?? null;
}

/**
 * 评级汇总摘要 → `{ total, institutionCount, analystCount, rows, note }`。
 * rows = 前若干条明细 `{name, rating, target, date}`（页面表格用；名称来源见下）。
 */
export function ratingSummarySummary(value, limit = 8) {
  const empty = { total: MISSING, institutionCount: 0, analystCount: 0, rows: [], note: "" };
  if (value === null || value === undefined) return empty;
  const institutions = Array.isArray(value.inst_rating_summary_list)
    ? value.inst_rating_summary_list : [];
  const analysts = Array.isArray(value.analyst_rating_summary_list)
    ? value.analyst_rating_summary_list : [];
  const rows = [
    ...institutions.map((entry) => ({
      name: fmtText(entry?.institution_info?.institution_name),
      source: "机构",
      ...ratingItemCells(firstRatingItem(entry)),
    })),
    ...analysts.map((entry) => ({
      name: fmtText(entry?.analyst_info?.analyst_name),
      source: "分析师",
      ...ratingItemCells(firstRatingItem(entry)),
    })),
  ].slice(0, limit);
  const note = (institutions.length === 0 && analysts.length === 0)
    ? "上游返回空列表：该接口仅美股/加股有数据（其它市场无覆盖），也可能是该标的本期无评级。"
    : "";
  return {
    total: fmtText(value?.pagination?.total),
    institutionCount: institutions.length,
    analystCount: analysts.length,
    rows,
    note,
  };
}

/** 单条评级条目 → 三个展示单元（缺失一律 —）。 */
function ratingItemCells(item) {
  return {
    rating: ratingItemLabel(item?.rating),
    target: fmtNum(item?.target_price),
    date: fmtText(item?.recommendation_date_str),
  };
}

// ---------------------------------------------------------------------------
// 机构持股（institutional）
// ---------------------------------------------------------------------------

/**
 * 机构持股摘要 → `{ rows, note }`。持仓数量/占比与环比变化分列展示，
 * 环比缺失显示 —（不写 0）。
 */
export function institutionalSummary(value) {
  if (value === null || value === undefined) return { rows: [], note: "" };
  if (isEmptyObject(value)) {
    return { rows: [], note: "上游返回空对象：该标的当期无机构持股数据。" };
  }
  const rows = [
    { label: "报告期", value: fmtText(value.period_text) },
    { label: "机构数量", value: fmtText(value.institution_quantity) },
    { label: "机构数量变化", value: fmtText(value.institution_quantity_change) },
    { label: "持股数量", value: fmtText(value.holder_quantity) },
    { label: "持股数量变化", value: fmtText(value.holder_quantity_change) },
    { label: "持股占比", value: fmtPct(value.holder_pct) },
    { label: "持股占比变化", value: fmtPct(value.holder_pct_change) },
    { label: "收盘价", value: fmtNum(value.close_price) },
    { label: "数据更新", value: fmtText(value.update_time_str ?? value.update_time) },
  ];
  return { rows, note: "" };
}

// ---------------------------------------------------------------------------
// 衍生品（option_volatility / option_exercise_probability）
// ---------------------------------------------------------------------------

/**
 * 期权波动率摘要 → `{ rows, note }`。
 * **合约要求**（官方 -3）：symbol 必须是期权合约，传正股会被拒——页面指引写明。
 */
export function optionVolatilitySummary(value) {
  if (value === null || value === undefined) return { rows: [], note: "" };
  if (isEmptyObject(value)) {
    return { rows: [], note: "上游返回空对象：该合约暂无可用波动率。" };
  }
  const rows = [
    { label: "隐含波动率 IV", value: fmtNum(value.implied_volatility) },
    { label: "历史波动率 HV", value: fmtNum(value.history_volatility) },
    { label: "波动率溢价", value: fmtNum(value.volatility_premium) },
    { label: "平均隐含波动率", value: fmtNum(value.average_impvol) },
    { label: "IV 状态", value: fmtText(value.impvol_status) },
    { label: "分析", value: fmtText(value.analysis) },
    { label: "数据时间", value: fmtText(value.timestamp) },
  ];
  return { rows, note: "" };
}

/**
 * 行权概率摘要 → `{ rows, note }`。
 * `strike_probability` 官方为结构化字段：数组 → 页面表格行，对象 → 键值行，
 * 其它/缺失 → —（形状不猜）。
 */
export function exerciseProbabilitySummary(value) {
  if (value === null || value === undefined) return { rows: [], note: "", strikes: [] };
  if (isEmptyObject(value)) {
    return { rows: [], note: "上游返回空对象：该合约暂无行权概率数据。", strikes: [] };
  }
  const raw = value.strike_probability;
  const strikes = Array.isArray(raw) ? raw : [];
  const rows = [
    { label: "标的价格", value: fmtNum(value.security_price) },
    { label: "数据时间", value: fmtText(value.timestamp) },
    ...(raw && typeof raw === "object" && !Array.isArray(raw)
      ? Object.entries(raw).map(([key, item]) => ({ label: key, value: fmtText(item) }))
      : []),
    ...(strikes.length === 0 && !(raw && typeof raw === "object")
      ? [{ label: "行权概率", value: MISSING }] : []),
  ];
  return { rows, note: "", strikes };
}

/**
 * 行权概率数组 → 展示行。**键名不猜**：锁定表只固定了 `strike_probability` 这个外层字段，
 * 数组元素内部的键名未核对 → 用**上游自带的键**原样成行（界面显示上游键，界面不编字段名）。
 * 只取前 `limit` 个元素（页面另有「原始返回」折叠块兜底）。
 */
export function strikeRows(strikes, limit = 10) {
  if (!Array.isArray(strikes)) return [];
  return strikes.slice(0, limit).map((item, index) => ({
    index: index + 1,
    cells: (item && typeof item === "object")
      ? Object.entries(item).map(([key, value]) => ({ label: key, value: fmtText(value) }))
      : [{ label: "值", value: fmtText(item) }],
  }));
}

// ---------------------------------------------------------------------------
// 通道不可用的「原因 + 下一步」指引
// ---------------------------------------------------------------------------

/** 设置页哈希路由（app.jsx PAGES: settings）。 */
const SETTINGS_ROUTE = "#/settings";

/**
 * 服务端错误文案 → 页面补充的下一步指引（**不改写服务端原因**，只加可执行动作）。
 * 匹配依据是服务端既有文案（platform/server/futu_data.py 的 OPENAPI_AUTH_HINT 与
 * MCP 通道拒绝语），措辞变更时这里要同步——只做子串匹配，不做语义推断。
 */
export function dataplaneHint(error) {
  const text = String(error ?? "");
  if (!text) return "";
  if (text.includes("仅支持 openapi 通道") || text.includes("mcp 通道未登记")) {
    return `下一步：在设置页配置 OpenAPI 凭据并把通道切换为 openapi（${SETTINGS_ROUTE}）。`;
  }
  if (text.includes("未配置 openapi 凭据") || text.includes("OpenAPI 通道不可用")) {
    return `下一步：在设置页完成凭据配置/授权后重试（${SETTINGS_ROUTE}）。`;
  }
  if (text.includes("errcode=-9") || text.includes("无期权数据查询权限")) {
    return "说明：当前账户无期权数据查询权限（官方 -9），与标的无关。";
  }
  return "";
}
