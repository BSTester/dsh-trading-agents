// 策略回测引擎（FR-STRAT-002 的参数优化部分）：单标的「动量动量、long/flat」策略的
// 向量化回测与参数扫描。K 线来自 workbench `series`（富途真实数据）。
// PIT 纪律：t 日持仓只由 ≤ t-1 的收盘价决定（signal 用 c[t-1] 与 c[t-1-w]），无前视。
// 说明：这是自研的轻量回测（等权、无滑点佣金建模），与 workbench 的因子回测互补。

export function backtestMomentum(bars, { window = 20, rebalanceDays = 5 } = {}) {
  const closes = bars.map((b) => Number(b.c)).filter((c) => Number.isFinite(c))
  if (closes.length < window + 2) {
    return { error: `bars insufficient: need >= ${window + 2}, got ${closes.length}` }
  }
  const returns = [] // r[t] = c[t]/c[t-1]-1，t 从 1 开始
  for (let t = 1; t < closes.length; t++) returns.push(closes[t] / closes[t - 1] - 1)

  // position[t]（对齐 returns 下标：returns[t-1] 是第 t 天收益）由 ≤ t-1 的数据决定
  let position = 0
  let signalFlips = 0
  const strat = []
  const positions = []
  const equity = []
  let equityValue = 1
  let lastSignalPos = null
  let daysSinceRebalance = rebalanceDays // 第一个可调仓日为第 1 天
  for (let t = 1; t < closes.length; t++) {
    const idx = t - 1 // closes 下标：今日为 t
    if (daysSinceRebalance >= rebalanceDays && idx - window >= 0) {
      const momentum = closes[idx] / closes[idx - window] - 1
      const newPos = momentum > 0 ? 1 : 0
      if (lastSignalPos !== null && newPos !== lastSignalPos) signalFlips += 1
      lastSignalPos = newPos
      position = newPos
      daysSinceRebalance = 0
    } else {
      daysSinceRebalance += 1
    }
    const r = returns[t - 1]
    const s = position * r
    strat.push(s)
    positions.push(position)
    equityValue *= 1 + s
    equity.push({ t: bars[t]?.t ?? String(t), value: Number(equityValue.toFixed(6)) })
  }

  const n = strat.length
  if (n === 0) return { error: 'no strategy returns' }
  const mean = strat.reduce((a, b) => a + b, 0) / n
  const std = Math.sqrt(strat.reduce((sum, x) => sum + (x - mean) ** 2, 0) / Math.max(1, n - 1))
  const sharpe = std > 0 ? (mean / std) * Math.sqrt(252) : 0
  const annReturn = mean * 252
  let peak = 1
  let mdd = 0
  for (const point of equity) {
    peak = Math.max(peak, point.value)
    mdd = Math.min(mdd, point.value / peak - 1)
  }
  // 口径：持仓天数/胜率只统计真正持仓（position=1）的日子，空仓日不计入
  const heldDays = positions.filter((p) => p === 1).length
  const winDays = positions.filter((p, i) => p === 1 && strat[i] > 0).length
  const winRate = heldDays === 0 ? 0 : winDays / heldDays
  return {
    metrics: {
      sharpe: Number(sharpe.toFixed(3)),
      annReturnPct: Number((annReturn * 100).toFixed(2)),
      maxDrawdownPct: Number((mdd * 100).toFixed(2)),
      signalFlips,
      heldDays,
      flatDays: n - heldDays,
      days: n,
      winRatePct: Number((winRate * 100).toFixed(1)),
    },
    equity,
  }
}

export function paramSweep(bars, { windows = [10, 20, 30, 60], rebalanceDays = [5, 10, 20] } = {}) {
  const rows = []
  for (const window of windows) {
    for (const rebalance of rebalanceDays) {
      const result = backtestMomentum(bars, { window, rebalanceDays: rebalance })
      rows.push({
        window,
        rebalanceDays: rebalance,
        sharpe: result.error ? null : result.metrics.sharpe,
        annReturnPct: result.error ? null : result.metrics.annReturnPct,
        maxDrawdownPct: result.error ? null : result.metrics.maxDrawdownPct,
        error: result.error ?? undefined,
      })
    }
  }
  const valid = rows.filter((r) => r.sharpe !== null)
  valid.sort((a, b) => b.sharpe - a.sharpe)
  return { grid: rows, best: valid[0] ?? null }
}

export default { backtestMomentum, paramSweep }
