// 页面级市场视图纯函数（2026-09-18 用户需求：「各个页面中的信息需要按市场分类来查看，
// 现在混为一谈不方便」）。
//
// 与 marketFilter.js 的分工（那一份是口径，本份是页面判断）：
//   marketFilter.js —— 「市场值/标的 → 市场链」的归一，以及三个基础过滤器；
//   marketView.js   —— 「payload → 过滤后的行 + 空态原因」：页面把端点返回整块丢进来，
//                       拿回该显示的行与一句写清原因的空态文案。页面 JSX 里不再写 if/else，
//                       也就能被 node --test 直测（本仓库页面组件没有单测）。
//
// 三条纪律（与 marketFilter.js 同源，不另立口径）：
//   1. **不改请求参数**：过滤全在客户端展示层，端点照原样调；
//   2. **不猜市场**：归不到市场链的行（裸代码 ``00981``、类型码 ``'9'``、缺字段）
//      在「全部市场」下照常显示，选中具体市场时被排除，并**如实计数**（unclassified），
//      空态文案里说明是「无法判定」而不是「属于别的市场」；
//   3. **空态要说原因**：页面原本的「暂无数据」在筛掉了全部行时会变成看不出原因的空白，
//      故统一由 ``emptyStateReason`` 给出「当前筛选：港股 HK —— 本页无港股数据（共 N 行…）」。
//
// 实测形状依据（2026-09-18 对 127.0.0.1:8397 的响应，用例注释里逐条对应）：
//   positions.groups[].market = 1/3/100（富途 market_id，数字）
//   deals_today/orders_open.groups[].market = 'HK'/'SH'/'US' + '9'/'10'/'16'（账户类型码）
//   plan.plans[] **没有** market 字段（只有 target 键与 orders[].symbol）→ 由标的派生
//   schedule.jobs[].job = "SH:build_plan:2026-09-18"（市场在字符串前缀里）
//   snapshot.previews[].value.ticker（标的是嵌套字段）
//   audit.entries[].ticker / reconcile 的裸代码 '00981'（**无交易所前缀**）→ 无法判定
import {
  MARKET_ALL, MARKET_CHOICES, isAllMarkets, marketChainOf, symbolChain,
} from "./marketFilter.js";

/** 市场链简称：只用于空态文案（「本页无港股数据」比「本页无港股 HK 数据」好读）。 */
export const MARKET_CHAIN_SHORT = { SH: "A股", HK: "港股", US: "美股" };

/** 选中市场的展示名；与页头下拉**同源**（MARKET_CHOICES），未登记取值回落键本身。 */
export function marketLabelOf(selected) {
  const choice = MARKET_CHOICES.find((item) => item.value === selected);
  return choice ? choice.label : String(selected ?? "");
}

/**
 * 行内**市场值** → 展示文案：能归一就显示与筛选同源的中文标签（数字 market_id
 * ``1`` 也显示「港股 HK」），归不到就**原样**显示原值（``'9'`` 显示 ``9``，不猜），
 * 空值显示 —。
 */
export function marketDisplay(value) {
  const chain = marketChainOf(value);
  if (chain) return marketLabelOf(chain);
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

/** 标的（``HK.00700``）→ 展示文案；无前缀/未知前缀显示 —（表示无法判定，不猜市场）。 */
export function symbolMarketDisplay(symbol) {
  const chain = symbolChain(symbol);
  return chain ? marketLabelOf(chain) : "—";
}

/**
 * 空态原因：**只在「数据存在、但当前筛选下一条都不剩」时**给出，其余一律 null
 * 交给页面既有空态。返回文案一定含市场名，便于用户判断是自己选错了还是真没数据。
 *
 * @param {string} selected 页头选中的市场链（或 MARKET_ALL）
 * @param {{total?: number, kept?: number, unclassified?: number, scope?: string}} state
 *   total 过滤前的行数；kept 过滤后的行数；unclassified 其中无法判定市场的行数；
 *   scope 页面/区块自称（默认「本页」）。
 * @returns {string|null}
 */
export function emptyStateReason(selected, { total = 0, kept = 0, unclassified = 0, scope = "本页" } = {}) {
  if (isAllMarkets(selected)) return null;
  if (kept > 0) return null;
  if (total <= 0) return null;                       // 数据本身为空 → 页面既有空态
  const label = marketLabelOf(selected);
  const short = MARKET_CHAIN_SHORT[selected] ?? label;
  if (unclassified >= total) {
    return `当前筛选：${label} —— ${scope}共 ${total} 行的市场标识无法判定`
      + `（无交易所前缀或未登记的市场码），无法归入该市场，故未列出。`;
  }
  return `当前筛选：${label} —— ${scope}无${short}数据（共 ${total} 行，均属其他市场）。`;
}

/** 过滤结果的标准形状（所有 view* 都返回它，页面只认这几个字段）。 */
function result(rows, total, { unclassified = 0, scope = "本页", selected } = {}) {
  return {
    rows,
    total,
    kept: rows.length,
    dropped: total - rows.length,
    unclassified,
    emptyReason: emptyStateReason(selected, { total, kept: rows.length, unclassified, scope }),
  };
}

/**
 * 核心：按 ``chainOf(row)`` 给出的市场链过滤。
 * 「全部市场」**原样返回**（一行不丢，含无法判定的行）；否则只保留等于选中链的行。
 */
export function viewRows(rows, selected, chainOf, { scope = "本页" } = {}) {
  const list = Array.isArray(rows) ? rows : [];
  if (isAllMarkets(selected)) return result(list, list.length, { selected, scope });
  const kept = [];
  let unclassified = 0;
  for (const row of list) {
    const chain = chainOf(row);
    if (chain === selected) kept.push(row);
    else if (chain === null || chain === undefined) unclassified += 1;
  }
  return result(kept, list.length, { unclassified, scope, selected });
}

/** 按**标的前缀**过滤（symbolField 逐页确认：factors 是 ``ticker``，其余多为 ``symbol``）。 */
export function viewBySymbol(rows, selected, symbolField = "symbol", options = {}) {
  return viewRows(rows, selected, (row) => symbolChain(row?.[symbolField]), options);
}

/** 按行自带的**市场字段**过滤（如 ``market``）。 */
export function viewByMarket(rows, selected, marketField = "market", options = {}) {
  return viewRows(rows, selected, (row) => marketChainOf(row?.[marketField]), options);
}

/**
 * 券商分组信封（``groups:[{market, rows|positions}]``）→ 过滤后的组 + 摊平后的行。
 *
 * 为什么不能只按组数判断空态：实测 ``deals_today`` 里 HK 账户**存在但没有成交行**
 * （``{acc_id:'9393', market:'HK', rows:[]}``）——只看组数会把「无港股数据」显示成
 * 「有数据」。故空态按**行数**判定，而过滤按**组**进行（组内的行同属该账户市场）。
 */
export function viewGroups(groups, selected, rowsOf, { scope = "本页", marketField = "market" } = {}) {
  const list = Array.isArray(groups) ? groups : [];
  const pick = typeof rowsOf === "function"
    ? rowsOf : (group) => group?.rows ?? group?.positions ?? [];
  const total = list.reduce((sum, group) => sum + pick(group).length, 0);
  const kept = isAllMarkets(selected)
    ? list : list.filter((group) => marketChainOf(group?.[marketField]) === selected);
  const rows = kept.flatMap((group) => pick(group));
  const unclassified = list
    .filter((group) => marketChainOf(group?.[marketField]) === null)
    .reduce((sum, group) => sum + pick(group).length, 0);
  return {
    groups: kept,
    ...result(rows, total, { unclassified, scope, selected }),
  };
}

/**
 * 计划的市场链集合（保序、去重）。
 *
 * **实测口径**：``plan`` 端点的 ``plans[]`` 没有 ``market`` 字段（页头注释里也没有），
 * 市场只能从 ``target`` 的键与 ``orders[].symbol`` 的前缀推——两处都是带前缀的标的，
 * 与因子/持仓的标的写法同源。没有任何可判定的标的时返回空数组（不猜）。
 */
export function planChainsOf(plan) {
  const symbols = [
    ...Object.keys(plan?.target ?? {}),
    ...(plan?.orders ?? []).map((order) => order?.symbol),
  ];
  const chains = [];
  for (const symbol of symbols) {
    const chain = symbolChain(symbol);
    if (chain && !chains.includes(chain)) chains.push(chain);
  }
  return chains;
}

/** 计划的市场展示：单一市场给标签，多市场如实标「混合」（不挑一个冒充），无标的给 —。 */
export function planMarketDisplay(plan) {
  const chains = planChainsOf(plan);
  if (chains.length === 0) return "—";
  if (chains.length === 1) return marketLabelOf(chains[0]);
  return `混合（${chains.map((chain) => marketLabelOf(chain)).join("、")}）`;
}

/**
 * 计划列表过滤：**任一**标的落在选中市场即保留（混合市场计划在两个市场下都该看得见，
 * 藏起来等于丢事实）；一条标的都判不出市场的计划计入 unclassified。
 */
export function viewPlans(plans, selected, { scope = "本页" } = {}) {
  const list = Array.isArray(plans) ? plans : [];
  if (isAllMarkets(selected)) return result(list, list.length, { selected, scope });
  const chains = list.map((plan) => planChainsOf(plan));
  const kept = list.filter((_plan, index) => chains[index].includes(selected));
  const unclassified = chains.filter((item) => item.length === 0).length;
  return result(kept, list.length, { unclassified, scope, selected });
}

/**
 * 调度作业键 ``"市场:作业名:日期"`` → 市场链（实测 daemon.py 的键形如
 * ``SH:build_plan:2026-09-18``）；``GLOBAL`` 这类非市场前缀返回 null。
 */
export function jobMarketChain(jobKey) {
  const text = String(jobKey ?? "");
  if (!text.includes(":")) return null;
  return marketChainOf(text.split(":", 1)[0]);
}

/**
 * 作业行过滤：选中市场保留该市场的作业；**无市场维度的行（GLOBAL 等）保持显示**——
 * 它们不属于任何单个市场，按市场筛掉等于让对账/资讯类全局作业从页面上消失。
 */
export function viewScheduleJobs(jobs, selected, { scope = "本页", marketField = "job" } = {}) {
  const list = Array.isArray(jobs) ? jobs : [];
  if (isAllMarkets(selected)) {
    return { ...result(list, list.length, { selected, scope }),
      marketless: list.filter((row) => jobMarketChain(row?.[marketField]) === null).length };
  }
  const kept = list.filter((row) => {
    const chain = jobMarketChain(row?.[marketField]);
    return chain === selected || chain === null;
  });
  const unclassified = list.filter((row) => jobMarketChain(row?.[marketField]) === null).length;
  return {
    ...result(kept, list.length, { unclassified, scope, selected }),
    marketless: unclassified,
  };
}

/**
 * 信号预览（``snapshot.previews``）过滤：标的在**嵌套的** ``value.ticker`` 里
 * （``{id, kind, mode, value:{ticker, strategy, ...}}``），``filterBySymbol`` 取不到，
 * 故单独给一个取链函数；``value`` 为 null 的行（实测 ledger 行）计入无法判定。
 */
export function viewSignalPreviews(previews, selected, { scope = "本页" } = {}) {
  return viewRows(previews, selected,
    (row) => symbolChain(row?.value?.ticker ?? row?.ticker), { scope });
}

/**
 * 单标的页面的市场提示（events / capital / market 页：页面数据只属于用户输入的那一个标的）。
 *
 * 为什么**只提示不隐藏**：这三页没有「跨市场混排」——一次只查一个标的，不存在用户抱怨的
 * 「混为一谈」；把它们按全局市场藏掉会与用户刚输入的标的直接冲突（本任务对行情页也明确
 * 要求不过滤）。故这里给一句写清两边的提示，数据照常显示；一致/无筛选/无法判定时不打扰。
 *
 * @returns {string|null}
 */
export function symbolMarketNote(symbol, selected) {
  if (isAllMarkets(selected)) return null;
  const text = String(symbol ?? "").trim();
  if (!text) return null;
  const chain = symbolChain(text);
  const label = marketLabelOf(selected);
  if (chain === selected) return null;
  if (chain === null) {
    return `标的 ${text} 没有交易所前缀，无法判定是否属于「${label}」；本页不做市场过滤。`;
  }
  return `标的 ${text} 属于「${marketLabelOf(chain)}」，与当前筛选「${label}」不一致；`
    + "本页不做市场过滤（数据仍按输入标的显示）。";
}
