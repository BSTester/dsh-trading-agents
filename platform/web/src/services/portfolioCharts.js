// 组合/风险/概览三页的图表派生（第二批图表，2026-09-18 用户需求：「在适当的地方增加一些
// 图表的展示」）。本模块只做**纯函数**：`过滤后的 groups → 图表项`，页面 JSX 只负责渲染。
//
// 为什么派生单独一层（与第一批 optionScreen.js 的分工一致）：canvas 在 node 里画不出来，
// 「哪条该画、哪条被跳过、占比的分母是谁」是唯一能在 CI 里锁住的地方。第一批的教训是
// 空柱状图看不出原因、缺值被当成 0；所以这里每个函数都**返回跳过计数**，由页面原样写出。
//
// 四条口径（每条都有 tests/portfolioCharts.test.mjs 的用例钉住）：
//   1. **缺失不是 0**：所有数值先过 `finiteNumber`（null/""/NaN/非数字都是缺失），
//      market_value 缺失或 ≤0 的持仓不产出条形，并计入 `skipped`（宁缺毋假）；
//   2. **图与表同源**：入参就是页面表格用的那份 `viewGroups(...)` 结果（同一份
//      `filterGroups` 过滤后的 groups），本模块**不再自己过滤市场**——否则图与表会
//      出现两个不一致的数字。市场链只用于「按市场聚合」，不用于筛掉任何组；
//   3. **不猜市场**：市场链归一复用 `marketFilter.marketChainOf`（数字 market_id
//      1/3/100 与字符串链名走同一条路），归不到的组归「其它」**而不是丢掉**；
//   4. **口径透明**：占比的分母随结果一起返回（`total`），页面不自己另算一份。
//
// 数据形状依据（2026-09-19 对 127.0.0.1:8397 /api/wb/positions mode=sim 实测）：
//   groups[] = {acc_id, account, market, positions[], market_value, pl_val, currency, risk}
//   * `market`：sim 是富途数字 market_id（1=港股 / 3=A股 / 100=美股），实盘也可能是
//     'HK'/'SH'/'US' 这类字符串链名 → 一律走 marketChainOf；
//   * `positions[].{symbol, name, market_value, pl_val}`（market_value 未取到时为 null）；
//   * **实盘账户**的 `group.market_value` 恒为 null（positions.py 对 live 账户显式给 None，
//     只给 subtotals）→ marketShareSlices 退回该组持仓市值之和，并如实计数（derivedAccounts）。
//   * `group.risk.top` 是上游 `account_risk` 的 `ranked[:3]`（**每账户最多 3 条**），
//     `share_of_positions` 是**百分数**（40.85，不是 0.4085）→ concentrationItems 原样使用，
//     不自己另算一份（页面既有集中度表格用的也是它）。
import { finiteNumber } from "../charts/scale.js";
import { marketChainOf } from "./marketFilter.js";
import { marketLabelOf } from "./marketView.js";

/** 市场链的固定展示顺序（与页头下拉 MARKET_CHOICES 同序）；`null` = 归不到链的「其它」。 */
const CHAIN_ORDER = ["SH", "HK", "US", null];
/** 归不到市场链的组的展示名（不猜、不硬塞进某个市场）。 */
export const OTHER_CHAIN_LABEL = "其它";

/** 抹掉浮点求和噪声（0.1+0.2 这类），避免同一份数据在不同调用点算出不同显示值。 */
function roundish(value) {
  return Number(value.toPrecision(12));
}

/** 严格正数：缺失 / ≤0 一律返回 null（调用方据此跳过 + 计数）。 */
function positiveNumber(raw) {
  const value = finiteNumber(raw);
  return value !== null && value > 0 ? value : null;
}

/** 持仓/风险行的标签：「代码 名称」；缺名称只用代码，都没有给「—」。 */
function holdingLabel(row) {
  const symbol = String(row?.symbol ?? "").trim() || "—";
  const name = String(row?.name ?? "").trim();
  return name ? `${symbol} ${name}` : symbol;
}

/** 代码本身（盈亏图的横轴标签只用代码，位置窄，名称在表格里）。 */
function symbolLabel(row) {
  return String(row?.symbol ?? "").trim() || "—";
}

/** 摊平：groups → [{group, position}]（保序；组或 positions 缺失当空）。 */
function holdingEntries(groups) {
  const list = Array.isArray(groups) ? groups : [];
  return list.flatMap((group) => (Array.isArray(group?.positions) ? group.positions : [])
    .map((position) => ({ group, position })));
}

/**
 * `limit` 归一：缺失/非有限 → `null`（不限）；否则向下取整并收敛到 ≥0。
 * 被 limit 截掉的条目**不是缺失**，单独计入 `omitted`（两种计数不能混）。
 */
function limitCount(limit) {
  const value = finiteNumber(limit);
  if (value === null) return null;
  return Math.max(0, Math.trunc(value));
}

/** 截断 + 计数（skipped 由调用方在产出阶段算好，这里只处理 omitted）。 */
function applyLimit(items, limit) {
  const keep = limitCount(limit);
  const kept = keep === null ? items : items.slice(0, keep);
  return { items: kept, omitted: items.length - kept.length };
}

/**
 * 图表空态文案（纯函数，三页共用；优先级一旦写错就会把「还没读到」显示成「没有数据」）。
 *
 * 优先级：**筛选原因 > 加载中 > 无数据 > 字段整列缺失**。
 *   * `reason` —— 来自 `marketView.emptyStateReason`（「当前筛选：港股 HK —— 本页无港股数据…」），
 *     它已经是写给用户的原因句，**原样返回、不改写口径**；存在时优先级最高（哪怕还在加载：
 *     用户刚切的筛选必然是筛完的结果）；
 *   * `loading` —— 请求未结束；此时说「暂无」就是把「还没读到」谎报成「没有」；
 *   * `count <= 0`（含 NaN）—— 数据本身为空，用页面既有空态文案；
 *   * 其余（有行但一条都画不出来）—— 用 `missingText` 说明**具体缺哪个字段**，
 *     空柱状图不写原因，用户只能猜是没数据还是没画出来（第一批期权页的教训）。
 *
 * @param {{reason?: string|null, loading?: boolean, loadingText?: string, count?: number,
 *   emptyText?: string, missingText?: string}} state
 * @returns {string} 画布内的空态文案（可为空串）
 */
export function chartEmptyText({
  reason = null, loading = false, loadingText = "加载中…",
  count = 0, emptyText = "暂无数据。", missingText = "",
} = {}) {
  if (reason) return reason;
  if (loading) return loadingText;
  if (!(count > 0)) return emptyText;
  return missingText;
}

/**
 * 图表派生的「跳过 / 未画」标注文案（纯函数，页面直接渲染）。
 *
 * 为什么文案也在这里而不是各页面各写一份：跳过口径（缺什么、为什么跳）必须与派生函数
 * 的计数**同一处定义**，否则改了口径忘了改文案，用户看到的理由就与图不符。
 * 两类计数分开写、不混：
 *   * `skipped` —— 数值缺失/无效而**画不出来**的条数（宁缺毋假的核心事实）；
 *   * `omitted` —— 因为显示条数上限而**没画**的条数（数据是好的，只是没展示）。
 * 都为 0 时返回 `null`（页面据此不渲染任何文字，不写「跳过 0 条」）。
 *
 * @param {{skipped?: number, omitted?: number, reason?: string,
 *   omittedReason?: string}} counts
 * @returns {string|null}
 */
export function skipNoteText({
  skipped = 0, omitted = 0,
  reason = "数值缺失或非数字", omittedReason = "超出显示条数上限",
} = {}) {
  const parts = [];
  if (skipped > 0) parts.push(`跳过 ${skipped} 条（${reason}）`);
  if (omitted > 0) parts.push(`未画 ${omitted} 条（${omittedReason}）`);
  return parts.length > 0 ? parts.join("；") : null;
}

/**
 * 持仓市值横向条形项（组合页「持仓市值」、概览页「Top5 持仓」共用）。
 *
 * 口径：
 *   * `label` = 「代码 名称」（如 `00981 中芯国际`），`value` = `market_value`；
 *   * **按市值降序**（并列保持输入顺序；跨账户同代码各成一条，与表格逐行对应，不合并）；
 *   * `market_value` 为 null/空串/非数字/≤0 → **跳过并计入 `skipped`**（缺失不是 0；
 *     0 与负数在横向条形图里没有长度可言，画出来只会误导）；
 *   * `total` = **画入项**市值之和——它就是页面显示「占比」时的分母，页面不必另算。
 *
 * @param {Array} groups `viewGroups(...)` 过滤后的账户组
 * @param {number} [limit] 只保留前 N 条；被截掉的计入 `omitted`（不是 `skipped`）
 * @returns {{items: {label: string, value: number}[], skipped: number,
 *   omitted: number, total: number}}
 */
export function holdingValueItems(groups, limit) {
  let skipped = 0;
  const items = [];
  for (const { position } of holdingEntries(groups)) {
    const value = positiveNumber(position?.market_value);
    if (value === null) { skipped += 1; continue; }
    items.push({ label: holdingLabel(position), value });
  }
  items.sort((left, right) => right.value - left.value);
  const { items: kept, omitted } = applyLimit(items, limit);
  return {
    items: kept,
    skipped,
    omitted,
    total: roundish(kept.reduce((sum, row) => sum + row.value, 0)),
  };
}

/**
 * 持仓盈亏竖向柱状项（组合页「持仓盈亏」、风险页「盈亏分布」共用）。
 *
 * 口径：
 *   * `label` = 代码（`symbol`），`value` = `pl_val`；**按盈亏降序**（盈利在左、亏损在右，
 *     读起来是一条从红到绿的分布），正负由 `VBarChart` 按 themeColors().up/down 分色
 *     （暗色主题红涨绿跌），本模块不碰颜色；
 *   * `pl_val` 为 null/空串/非数字 → 跳过并计入 `skipped`；**0 是有效值**（「不赚不亏」
 *     是一条真实事实，不是缺失），照常产出；
 *   * 不返回合计：盈亏是各账户本币数值，跨币种相加就是编数据。
 *
 * @returns {{items: {label: string, value: number}[], skipped: number, omitted: number}}
 */
export function holdingPnlItems(groups, limit) {
  let skipped = 0;
  const items = [];
  for (const { position } of holdingEntries(groups)) {
    const value = finiteNumber(position?.pl_val);
    if (value === null) { skipped += 1; continue; }
    items.push({ label: symbolLabel(position), value });
  }
  items.sort((left, right) => right.value - left.value);
  const { items: kept, omitted } = applyLimit(items, limit);
  return { items: kept, skipped, omitted };
}

/**
 * 按**市场链**聚合的持仓市值占比（概览页环图）。
 *
 * 口径：
 *   * 链的顺序固定为 SH → HK → US → 其它（与页头下拉同序，不随数据顺序漂移）；
 *     标签用 `marketView.marketLabelOf`（「港股 HK」），**不另写一份映射**；
 *   * 归不到链的组（数字类型码 `'9'`、缺 market 字段）归「其它」，**不丢**；
 *   * 每组的市值取 `group.market_value`；缺失时（**实盘账户恒为 null**）退回该组
 *     `positions[].market_value` 之和，并计入 `derivedAccounts` 如实标注；
 *     两处都取不到 → 计入 `skipped`（该账户的市值无法计入）；
 *   * 聚合后总值 ≤0 的链**不产出扇区**（与 `donutArcs` 同口径：把全 0 画成整圆、
 *     把负数按绝对值画都是编数据），计入 `emptyChains`；
 *   * `total` = 画入扇区值之和（页面中心文案用它，保证与扇区自洽）。
 *
 * **币种口径（如实声明）**：各市场账户的本币数值**直接相加，不做汇率换算**（sim 数据
 * 里 currency 恒为 null，也换不了）。故 `total` 与占比只反映各市场账户的**数值规模**，
 * 页面必须在图下写明这一点（与页面既有「币种混排不做换算」同一纪律）。
 *
 * @returns {{slices: {chain: string|null, label: string, value: number}[],
 *   skipped: number, emptyChains: number, derivedAccounts: number, total: number}}
 */
export function marketShareSlices(groups) {
  const list = Array.isArray(groups) ? groups : [];
  const buckets = new Map();      // 链 → 市值合计；键 null = 「其它」
  let skipped = 0;
  let derivedAccounts = 0;
  for (const group of list) {
    const chain = marketChainOf(group?.market);
    if (!buckets.has(chain)) buckets.set(chain, 0);
    let value = finiteNumber(group?.market_value);
    if (value === null) {
      const values = (Array.isArray(group?.positions) ? group.positions : [])
        .map((position) => finiteNumber(position?.market_value))
        .filter((item) => item !== null);
      if (values.length === 0) { skipped += 1; continue; }
      value = roundish(values.reduce((sum, item) => sum + item, 0));
      derivedAccounts += 1;
    }
    buckets.set(chain, roundish(buckets.get(chain) + value));
  }
  const slices = [];
  let emptyChains = 0;
  for (const chain of CHAIN_ORDER) {
    if (!buckets.has(chain)) continue;
    const value = buckets.get(chain);
    if (!(value > 0)) { emptyChains += 1; continue; }
    slices.push({ chain, label: chain === null ? OTHER_CHAIN_LABEL : marketLabelOf(chain), value });
  }
  return {
    slices,
    skipped,
    emptyChains,
    derivedAccounts,
    total: roundish(slices.reduce((sum, row) => sum + row.value, 0)),
  };
}

/**
 * 持仓市值 Top N（概览页「Top5 持仓」图）。
 *
 * 与 `holdingValueItems` 同源同序（同样是市值降序、同样跳过缺失并计数），只是取前 N 条；
 * 页面既有 Top5 表格与它吃**同一份** `filterGroups` 结果，所以图与表的数字逐行一致。
 * `n` 缺省 5；`n` 非正 → 不产出条目（`omitted` 如实计数）；`n` 非法（非数字）→ 回落 5。
 *
 * @returns {{items: {label: string, value: number}[], skipped: number,
 *   omitted: number, total: number}} `total` = Top N 市值之和
 */
export function topHoldings(groups, n = 5) {
  const parsed = finiteNumber(n);
  const keep = parsed === null ? 5 : Math.max(0, Math.trunc(parsed));
  return holdingValueItems(groups, keep);
}

/**
 * 各持仓占**本账户**持仓市值的比例（风险页「集中度」）。
 *
 * 口径（**优先用端点字段，不自己另算一份**）：
 *   * 账户有 `risk.top[]` 且每条 `share_of_positions` 都是有限数 → 原样使用（单位是
 *     **百分数**，40.85 表示 40.85%），顺序就是上游的市值降序；
 *   * 该字段缺失（`risk.top` 不存在、为空，或有条目 `share_of_positions` 为 null）→
 *     该账户**退回自身聚合**：`market_value / 本账户持仓市值合计 × 100`，并在
 *     `fallbackAccounts` 里计数，页面据此在图下写明「本图有多少个账户是自行聚合的」；
 *   * 账户持仓市值合计 ≤0（全缺失或全 0）→ 不产出条目，按持仓条数计入 `skipped`
 *     （画 0% 等于宣称「这只票没有仓位」，与事实不符）。
 *
 * 已知边界（如实写进页面说明）：上游 `account_risk` 的 `top` 是 `ranked[:3]`，
 * **每账户最多 3 条**——所以本图与页面既有的集中度表格一样只覆盖前 3 名，
 * 完整持仓在「持仓」表格里。
 *
 * @returns {{items: {label: string, value: number}[], skipped: number, fallbackAccounts: number}}
 */
export function concentrationItems(groups) {
  const list = Array.isArray(groups) ? groups : [];
  const items = [];
  let skipped = 0;
  let fallbackAccounts = 0;
  for (const group of list) {
    const top = Array.isArray(group?.risk?.top) ? group.risk.top : null;
    const usable = top !== null && top.length > 0
      && top.every((row) => finiteNumber(row?.share_of_positions) !== null);
    if (usable) {
      for (const row of top) {
        items.push({ label: holdingLabel(row), value: finiteNumber(row.share_of_positions) });
      }
      continue;
    }
    fallbackAccounts += 1;
    const rows = Array.isArray(group?.positions) ? group.positions : [];
    const valued = rows
      .map((row) => ({ row, value: positiveNumber(row?.market_value) }))
      .filter((entry) => entry.value !== null);
    const total = valued.reduce((sum, entry) => sum + entry.value, 0);
    if (!(total > 0)) { skipped += rows.length; continue; }
    for (const entry of valued) {
      items.push({ label: holdingLabel(entry.row), value: roundish((entry.value / total) * 100) });
    }
    skipped += rows.length - valued.length;
  }
  return { items, skipped, fallbackAccounts };
}
