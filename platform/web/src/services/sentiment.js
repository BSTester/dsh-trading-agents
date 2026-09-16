// 情绪快照摘要的展示派生（WP11 任务 3）：纯函数，宿主无关，node --test 直测。
//
// 数据来源：服务端 ``sentiment-history`` 端点不带 symbol 的**摘要形态**——
//   trading_core/store.py::sentiment_summary() →
//   {date, symbols, records, sources:{源名:条数}}；库空时 date=null、全零（空是事实，不是错误）。
// 三源固定顺序取采集侧的实际源名（trading_core/sentiment.py 的 SOURCES 同源事实）：
//   fin_sentiment（千股千评/X 情绪）、fin_news（富途资讯面）、last30days（可选社媒引擎）。
// 缺席（未装引擎/未配密钥/当日失败）如实标 false，**不用 0 冒充「跑了但没数据」以外的东西**：
// 条数 0 与源缺席在采集侧就是同一个观测结果（未配置的源当场缺席），此处不额外推断原因。

export const SENTIMENT_SOURCES = ["fin_sentiment", "fin_news", "last30days"];

/**
 * 摘要 → 展示行：[{source, count, present}]，已知三源固定顺序在前，未知源按名追加在后。
 * 未知源**如实展示**（采集侧源名不由本表白名单决定，出现即事实），不静默丢弃。
 * @param {{sources?: Record<string, number>}|null|undefined} summary
 */
export function sentimentRows(summary) {
  const counts = summary?.sources ?? {};
  const rows = SENTIMENT_SOURCES.map((name) => ({
    source: name,
    count: Number(counts[name] ?? 0),
    present: Number(counts[name] ?? 0) > 0,
  }));
  for (const name of Object.keys(counts).sort()) {
    if (!SENTIMENT_SOURCES.includes(name)) {
      rows.push({ source: name, count: Number(counts[name] ?? 0), present: true });
    }
  }
  return rows;
}

/**
 * 采集事实一行话：无记录时不给日期（「暂无记录」），有记录才报日期与条数。
 * @param {{date?: string|null, symbols?: number, records?: number}|null|undefined} summary
 */
export function sentimentHeadline(summary) {
  if (!summary || !summary.date) return "暂无情绪快照记录";
  return `${summary.date} 采集 ${Number(summary.records ?? 0)} 条，覆盖 ${Number(summary.symbols ?? 0)} 个标的`;
}
