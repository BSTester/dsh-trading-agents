"""结论性字段的中文标签——**唯一事实来源**。

为什么需要单独一份：回测、信号、台账、审计里到处都有"结论"，此前一律用英文枚举
（`BUY` / `SELL` / `HOLD` / `buy` / `sell` / `open` / `Buy` / `Hold`）。这些值既当
机器码（比较、落盘、跨模块契约）又当展示值，于是界面上直接出现 `BUY 100 @ 12.3`、
`Buy`、`open` 这类英文，研报里也容易被模型照抄。

这里的原则：
  * **机器码不动** —— 台账已落盘、逻辑在比较、旧记录要能读，改枚举会破坏兼容；
  * **另给一个中文标签字段**（如 `signal_label`）—— 所有面向人的地方一律用它；
  * 旧记录没有标签字段时，调用方用本表按码翻译，界面不会出现英文。

JS 侧（`plugins/workbench/src/labels.js` 与 `src/client.js` 内的回退表）是同一份表的
镜像，由 `tests/test_labels.py` 解析比对，防止两边漂移。
"""

# 信号：compute_signal 产出
SIGNAL = {"BUY": "买入", "SELL": "卖出", "HOLD": "观望"}

# 下单方向：台账 order/fill 的 action
ACTION = {"BUY": "买入", "SELL": "卖出"}

# 富途 order_side：1=Buy 2=Sell（工具 schema 明文）
SIDE = {1: "买入", 2: "卖出", "1": "买入", "2": "卖出"}

# 回测成交记录
TRADE_TYPE = {"buy": "买入", "sell": "卖出"}
TRADE_STATUS = {"open": "持仓中", "closed": "已平仓"}

# 研报评级：research_publish 的 rating
RATING = {"Buy": "买入", "Overweight": "增持", "Hold": "持有",
          "Underweight": "减持", "Sell": "卖出"}

# 数据源自检状态
SOURCE_STATUS = {"ok": "正常", "warn": "待配置", "fail": "异常", "empty": "无数据"}

# 成交口径说明（此前以英文枚举直接展示）
EXECUTION_TIMING = {"previous_bar_next_open": "信号次一交易日开盘成交"}
END_POSITION_POLICY = {"mark_to_market_no_liquidation": "按最后收盘价盯市，不强制平仓"}
EXECUTION_SOURCE = {"local_simulation": "本地模拟（非券商成交）"}

# 回测指标：敏感性网格与预览里的 metric 字段
METRIC = {"total_return": "累计收益", "annualized": "年化收益", "sharpe": "夏普比率",
          "max_drawdown": "最大回撤", "win_rate": "胜率"}

# 网格轴名：sensitivity.py 的 row_label / col_label
GRID_AXIS = {"fast": "快线", "slow": "慢线", "rsi_buy": "买入阈值", "rsi_sell": "卖出阈值"}

# 策略名
STRATEGY = {"ma_cross": "双均线", "rsi": "RSI"}

# 风控参数：界面上的中文名
RISK_CONFIG = {
    "risk_per_trade": "单笔风险占权益比",
    "stop_atr_mult": "ATR 止损倍数",
    "max_positions": "最大持仓数",
    "max_position_pct": "单标的上限占权益比",
}


def zh(table, value, fallback=None):
    """按表翻译；查不到就原样返回（不吞掉未知值，便于发现新枚举）。"""
    if value is None:
        return fallback
    if value in table:
        return table[value]
    for key, label in table.items():
        if isinstance(key, str) and isinstance(value, str) and key.lower() == value.lower():
            return label
    return value if fallback is None else fallback


def signal_label(code):
    return zh(SIGNAL, code)


def action_label(code):
    return zh(ACTION, code)


def rating_label(code):
    return zh(RATING, code)


def side_label(code):
    return zh(SIDE, code)


def trade_type_label(code):
    return zh(TRADE_TYPE, code)


def trade_status_label(code):
    return zh(TRADE_STATUS, code)


def source_status_label(code):
    return zh(SOURCE_STATUS, code)


def risk_config_label(key):
    return zh(RISK_CONFIG, key)


def metric_label(code):
    return zh(METRIC, code)
