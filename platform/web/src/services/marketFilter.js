// 全局市场维度（2026-09-18 用户需求：「各个页面中的信息需要按市场分类来查看，现在混为一谈」）。
//
// 为什么需要一层归一口径：本平台的数据在两个地方带市场信息，写法完全不同——
//   1. **券商账户/订单端点**按**账户**分组，``groups[].market`` 是字符串市场链（实测
//      ``orders_open`` = 'HK'/'SH'/'US'）或数字账户类型码（实测 ``deals_today`` 会出现
//      '9'/'10'/'16' 这类非市场码）——数字码必须归一，否则「按市场看」会漏掉或误判；
//   2. **研究/因子端点**按**标的**给行，市场只能从标的写法（``SH.600519``）的前缀推。
// 两处口径各写一遍必然漂移，所以这里收敛成 ``marketChainOf``（值 → 市场链）与
// ``symbolChain``（标的 → 市场链）两个纯函数，页面只做「过滤/分组」。
//
// 本模块只做**展示层归类**：不改任何请求参数、不推断服务端没给的市场。归不到三个市场链的
// 值一律返回 ``null``（非 A股/港股/美股账户，例如期货或上游未登记的类型码），它们在
// 「全部市场」下照常显示，选中具体市场时被排除——**不猜测、不硬塞进某个市场**。

/** 「全部市场」哨兵值：默认态。 */
export const MARKET_ALL = "ALL";

/** 页头下拉的取值集合（顺序即展示顺序）。 */
export const MARKET_CHOICES = [
  { value: MARKET_ALL, label: "全部市场" },
  { value: "SH", label: "A股 SH" },
  { value: "HK", label: "港股 HK" },
  { value: "US", label: "美股 US" },
];

/** 选中的市场链中文标签；未登记回落键本身（看到英文键即「这里还没登记标签」）。 */
export const MARKET_CHAIN_LABELS = { SH: "A股 SH", HK: "港股 HK", US: "美股 US" };

/**
 * 市场值 → 市场链（``SH``/``HK``/``US``）；归不到返回 ``null``。
 *
 * 别名表覆盖两类真实写法：
 *   * 标的/账户的**交易所前缀**：``SZ``/``BJ`` 与 ``SH`` 同属 A 股链（与
 *     ``planner.CALENDAR_MARKET`` 的口径一致：SZ/BJ 用 SH 日历）；
 *   * 富途 **market_id**：1=港股、3=A股、100=美股（``trading_datasource.market_ids``
 *     实测值）——券商分组在部分端点上给的是这个数字。
 * **不**收录 ``9``/``10``/``16`` 这类类型码：它们不是市场，收了就会把非股票账户
 * 谎报成某个市场。
 */
const MARKET_ALIASES = {
  SH: "SH", SZ: "SH", BJ: "SH", SS: "SH", "1": "HK", "3": "SH", "100": "US", HK: "HK", US: "US",
};

export function marketChainOf(value) {
  if (value === null || value === undefined || value === "") return null;
  const text = String(value).trim().toUpperCase();
  return Object.prototype.hasOwnProperty.call(MARKET_ALIASES, text)
    ? MARKET_ALIASES[text] : null;
}

/** 标的（``SH.600519``）→ 市场链；无前缀/未知前缀返回 ``null``。 */
export function symbolChain(symbol) {
  const text = String(symbol ?? "").trim();
  if (!text.includes(".")) return null;
  return marketChainOf(text.split(".", 1)[0]);
}

/** 是否处于「全部市场」（默认态；非法/缺失值一律当全部）。 */
export function isAllMarkets(selected) {
  return !selected || selected === MARKET_ALL;
}

/** 按市场链过滤券商分组的 ``groups``（组无 market 字段 → 只在「全部」下显示）。 */
export function filterGroups(groups, selected, marketField = "market") {
  const rows = Array.isArray(groups) ? groups : [];
  if (isAllMarkets(selected)) return rows;
  return rows.filter((group) => marketChainOf(group?.[marketField]) === selected);
}

/** 按市场链过滤「带某市场字段」的行（用于 plan/schedule 这类自带 market 的端点）。 */
export function filterByMarketField(rows, selected, marketField = "market") {
  const list = Array.isArray(rows) ? rows : [];
  if (isAllMarkets(selected)) return list;
  return list.filter((row) => marketChainOf(row?.[marketField]) === selected);
}

/** 按**标的前缀**过滤行（研究/因子/情绪这类按标的给数据的端点）。 */
export function filterBySymbol(rows, selected, symbolField = "symbol") {
  const list = Array.isArray(rows) ? rows : [];
  if (isAllMarkets(selected)) return list;
  return list.filter((row) => symbolChain(row?.[symbolField]) === selected);
}

/**
 * 把行按市场链分组（保序、原索引顺序）：``{SH: [...], HK: [...], US: [...], OTHER: [...]}``。
 *
 * 用于「全部市场」下按市场分段展示（用户要的「按市场分类来查看」）；``OTHER`` 收纳归不到
 * 三个市场的行（如非股票账户），**不隐藏**——它们本就在数据里，藏起来等于报假账。
 */
export function groupByMarketChain(rows, symbolField = "symbol") {
  const out = { SH: [], HK: [], US: [], OTHER: [] };
  for (const row of Array.isArray(rows) ? rows : []) {
    const chain = symbolChain(row?.[symbolField]);
    (out[chain] ?? out.OTHER).push(row);
  }
  return out;
}

/** 「全部市场」下是否值得分段：只有一个市场有数据时分段反而是噪音。 */
export function hasMultipleMarkets(rows, symbolField = "symbol") {
  const grouped = groupByMarketChain(rows, symbolField);
  return ["SH", "HK", "US", "OTHER"].filter((key) => grouped[key].length > 0).length > 1;
}

/** 归一到合法取值：非法/缺失 → 全部（持久化脏值不得让页面空白）。 */
export function normalizeMarketChoice(value) {
  return MARKET_CHOICES.some((choice) => choice.value === value) ? value : MARKET_ALL;
}
