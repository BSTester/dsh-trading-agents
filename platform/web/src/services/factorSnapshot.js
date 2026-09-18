// 因子快照（factors-history）载荷的展示统计——概览页「因子快照」卡的唯一取值口径。
//
// 为什么单独一层：**载荷形状与首版的假设不一致**（2026-09-19 对 127.0.0.1:8397
// `POST /api/wb/factors-history {"limit":30}` 实测）：
//   snapshots[0].payload = {date, tickers:{标的:{因子键: 值}}, computed_at, note}
// 即 **没有 `factors` 键**（`payload.factors.length` 恒 undefined），
// 且 `tickers` 是**对象**而不是数组（`payload.tickers.length` 同样恒 undefined）
// ——于是「因子数」「覆盖标的」两个 Statistic 在一份**有数据**的快照上永远显示 —。
// 页面上这两个数字现在由本模块从真实形状推出（可 node --test 直测）：
//   * 覆盖标的 = `tickers` 的键个数；
//   * 因子数   = 各标的因子字典键的**并集**大小（不同标的气键集合不同时不重复计数，
//     与 payload.note 里的 `registry=` 声明同义），任一方缺失都给 0 而不是编造。
//
// 只做计数，不做任何指标推导；缺失一律 0（调用方据此决定显示 — 还是 0）。

/** payload.tickers → 标的数组（对象按值、数组原样；其它形状返回空数组）。 */
export function factorTickerRows(payload) {
  const tickers = payload?.tickers;
  if (Array.isArray(tickers)) return tickers;
  if (tickers && typeof tickers === "object") return Object.values(tickers);
  return [];
}

/**
 * 快照载荷 → 展示统计。
 * @param {{tickers?: unknown}} payload snapshots[0].payload
 * @returns {{covered: number, factors: number}} covered=覆盖标的数；factors=因子集合大小
 */
export function factorSnapshotStats(payload) {
  const rows = factorTickerRows(payload);
  const keys = new Set();
  for (const row of rows) {
    if (row && typeof row === "object" && !Array.isArray(row)) {
      for (const key of Object.keys(row)) keys.add(key);
    }
  }
  return { covered: rows.length, factors: keys.size };
}
