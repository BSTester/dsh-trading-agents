// strategy-svc：PDAT → PAAT → PCPT → PRT → PET 五阶段研究流水线（FR-STRAT-001）。
// 数据来源：workbench `factors`（真实多因子 z 值）+ `series`（真实富途日 K，本地动量复核）。
// PET 产物是「调仓建议提案」：写入决策日志并按风控分级标注，绝不直接下单——
// 执行仍走既有工作台的受约束入口（trade_place + Web 确认卡）。
export function createStrategyService({ wbCall, watchlist, store, limits = { singlePct: 2, industryPct: 20, drawdownPct: 15 } }) {
  function compositeOf(row) {
    // 透明组合：动量/趋势类 z 值取均值（估值/波动类只展示，不进综合分——口径见 README）
    const keys = ['mom_20', 'mom_60', 'trend']
    const values = keys.map((k) => row?.z?.[k]).filter((v) => Number.isFinite(v))
    if (values.length === 0) return null
    return values.reduce((a, b) => a + b, 0) / values.length
  }

  async function run({ universe, topN = 2, window = 20 } = {}) {
    const asOf = new Date().toISOString()
    const universeList = (universe && universe.length > 0 ? universe : watchlist).slice(0, 8)
    const stages = {}

    // PDAT：数据准备
    const seriesErrors = []
    const klines = {}
    stages.PDAT = { universe: universeList, bars: 0 }
    for (const ticker of universeList) {
      const envelope = await wbCall('series', { ticker, period: '1d', limit: Math.max(120, window * 4) })
      if (envelope?.ok && Array.isArray(envelope.value?.bars)) {
        klines[ticker] = envelope.value.bars
        stages.PDAT.bars += envelope.value.bars.length
      } else {
        seriesErrors.push({ ticker, error: envelope?.error?.message ?? 'unknown' })
      }
    }
    stages.PDAT.errors = seriesErrors

    // PAAT：因子分析（workbench 真实 z 值，横截面 8 只需要较长时间 → 单独放宽超时）
    let factorRows = []
    let factorsError = null
    if (universeList.length >= 2) {
      const envelope = await wbCall('factors', { tickers: universeList.slice(0, 8) }, { timeoutMs: 180000 })
      if (envelope?.ok) factorRows = envelope.value?.rows ?? []
      else factorsError = envelope?.error ?? { code: 'wb/error', message: 'unknown' }
    }
    const analysis = universeList.map((ticker) => {
      const row = factorRows.find((r) => r.ticker === ticker) ?? null
      const composite = row ? compositeOf(row) : null
      const bars = klines[ticker] ?? []
      const closes = bars.map((b) => Number(b.c)).filter(Number.isFinite)
      const localMomentum = closes.length > window ? closes.at(-1) / closes.at(-1 - window) - 1 : null
      return {
        ticker,
        compositeZ: composite === null ? null : Number(composite.toFixed(4)),
        factors: row ? { mom_20: row.factors?.mom_20, mom_60: row.factors?.mom_60, rsi_14: row.factors?.rsi_14, mdd_60: row.factors?.mdd_60 } : null,
        localMomentum: localMomentum === null ? null : Number((localMomentum * 100).toFixed(2) + '%'),
        localMomentumRaw: localMomentum,
        dataOk: bars.length > 0,
      }
    })

    // 兜底：workbench 因子不可用时，用本地 K 线动量做横截面标准化，保证候选池不为空（并如实标注来源）
    let scoreSource = 'workbench/factors(z)'
    if (analysis.every((a) => a.compositeZ === null)) {
      const values = analysis.map((a) => a.localMomentumRaw).filter((v) => Number.isFinite(v))
      if (values.length >= 2) {
        const mean = values.reduce((x, y) => x + y, 0) / values.length
        const std = Math.sqrt(values.reduce((sum, x) => sum + (x - mean) ** 2, 0) / Math.max(1, values.length - 1))
        for (const a of analysis) {
          if (!Number.isFinite(a.localMomentumRaw)) continue
          a.compositeZ = std > 0 ? Number(((a.localMomentumRaw - mean) / std).toFixed(4)) : 0
          a.fallback = true
        }
        scoreSource = 'local/series 动量横截面 z（workbench factors 不可用时的兜底）'
      }
    }
    stages.PAAT = { analyzed: analysis.length, withFactors: analysis.filter((a) => a.compositeZ !== null).length, scoreSource, factorsError }

    // PCPT：候选池（按综合 z 降序）
    const ranked = analysis.filter((a) => a.compositeZ !== null).sort((a, b) => b.compositeZ - a.compositeZ)
    const longs = ranked.slice(0, topN)
    const reduces = ranked.slice(-Math.min(topN, Math.max(0, ranked.length - topN))).reverse()
    stages.PCPT = { longs: longs.map((a) => a.ticker), reduces: reduces.map((a) => a.ticker) }

    // PRT：等权目标权重（受单笔上限约束）
    const weightPct = Math.min(limits.singlePct, 100 / Math.max(1, longs.length))
    stages.PRT = { weightPctPerName: weightPct, capped: 100 / longs.length > limits.singlePct }

    // PET：调仓建议提案（不下单；执行走工作台受约束入口）
    const proposals = []
    for (const a of longs) {
      proposals.push({ ticker: a.ticker, action: '增持', targetWeightPct: weightPct, basis: `综合动量 z=${a.compositeZ}（mom_20=${a.factors?.mom_20 ?? '—'}）`, riskLevel: (a.compositeZ ?? 0) > 0.5 ? '低' : '中', action_hint: '经审批后由工作台受约束入口执行' })
    }
    for (const a of reduces) {
      proposals.push({ ticker: a.ticker, action: '减持', targetWeightPct: 0, basis: `综合动量 z=${a.compositeZ}（排名末位）`, riskLevel: '中', action_hint: '经审批后由工作台受约束入口执行' })
    }
    stages.PET = { proposals: proposals.length }

    const summary = { asOf, universe: universeList, stages, proposals }
    // 落盘完整摘要（stages + 提案对象），供 /api/v3/strategy/last 与页面直接消费
    store.append('strategy_runs.jsonl', summary)
    return summary
  }

  function last() {
    const runs = store.readAll('strategy_runs.jsonl')
    return runs.at(-1) ?? null
  }

  return { run, last }
}

export default createStrategyService
