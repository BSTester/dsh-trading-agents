// 结论性字段的中文标签——Host 侧镜像。
//
// 唯一事实来源是 `plugins/datasource/python/trading_datasource/labels.py`（产出侧
// 用它给每个结论字段附 `*_label`）。这里镜像一份，用途只有一个：
// **渲染历史记录**。旧台账/旧预览/旧研报里没有 `*_label`，只有 `BUY`、`buy`、
// `open`、`Buy` 这类英文码，界面必须能把它们翻成中文，否则翻出旧记录又会看到英文。
//
// 两边由 `tests/test_labels.py` 解析比对，不允许漂移。

export const SIGNAL = { BUY: "买入", SELL: "卖出", HOLD: "观望" };
export const ACTION = { BUY: "买入", SELL: "卖出" };
export const SIDE = { 1: "买入", 2: "卖出" };
export const TRADE_TYPE = { buy: "买入", sell: "卖出" };
export const TRADE_STATUS = { open: "持仓中", closed: "已平仓" };
export const RATING = { Buy: "买入", Overweight: "增持", Hold: "持有", Underweight: "减持", Sell: "卖出" };
export const SOURCE_STATUS = { ok: "正常", warn: "待配置", fail: "异常", empty: "无数据" };
export const EXECUTION_TIMING = { previous_bar_next_open: "信号次一交易日开盘成交" };
export const END_POSITION_POLICY = { mark_to_market_no_liquidation: "按最后收盘价盯市，不强制平仓" };
export const EXECUTION_SOURCE = { local_simulation: "本地模拟（非券商成交）" };
export const METRIC = { total_return: "累计收益", annualized: "年化收益", sharpe: "夏普比率",
  max_drawdown: "最大回撤", win_rate: "胜率" };
export const GRID_AXIS = { fast: "快线", slow: "慢线", rsi_buy: "买入阈值", rsi_sell: "卖出阈值" };
export const STRATEGY = { ma_cross: "双均线", rsi: "RSI" };
export const RISK_CONFIG = {
  risk_per_trade: "单笔风险占权益比",
  stop_atr_mult: "ATR 止损倍数",
  max_positions: "最大持仓数",
  max_position_pct: "单标的上限占权益比",
};

const TABLES = { SIGNAL, ACTION, SIDE, TRADE_TYPE, TRADE_STATUS, RATING,
  SOURCE_STATUS, EXECUTION_TIMING, END_POSITION_POLICY, EXECUTION_SOURCE, RISK_CONFIG,
  METRIC, GRID_AXIS, STRATEGY };

/** 按表翻译；查不到原样返回（不吞掉未知值，便于发现新枚举）。 */
export function zh(table, value, fallback) {
  if (value === null || value === undefined) return fallback;
  const dict = TABLES[table] ?? {};
  if (Object.prototype.hasOwnProperty.call(dict, value)) return dict[value];
  if (typeof value === "string") {
    // 大小写不敏感：历史记录里 BUY / buy / Buy 都出现过
    const hit = Object.keys(dict).find((key) => String(key).toLowerCase() === value.toLowerCase());
    if (hit !== undefined) return dict[hit];
  }
  return fallback === undefined ? value : fallback;
}

/** 优先用产出侧给的中文标签，没有才按码翻译（旧记录走这里）。 */
export function labeled(value, label, table, fallback) {
  if (typeof label === "string" && label.trim()) return label;
  return zh(table, value, fallback);
}
