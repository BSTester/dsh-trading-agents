# WP6 补遗任务 B2：中文标签表 Python 移植。逐行对应 plugins/workbench/src/labels.js（52 行）。
#
#   labels.js:10      SIGNAL              -> SIGNAL
#   labels.js:11      ACTION              -> ACTION
#   labels.js:12      SIDE                -> SIDE
#   labels.js:13      TRADE_TYPE          -> TRADE_TYPE
#   labels.js:14      TRADE_STATUS        -> TRADE_STATUS
#   labels.js:15      RATING              -> RATING
#   labels.js:16      SOURCE_STATUS       -> SOURCE_STATUS
#   labels.js:17      EXECUTION_TIMING    -> EXECUTION_TIMING
#   labels.js:18      END_POSITION_POLICY -> END_POSITION_POLICY
#   labels.js:19      EXECUTION_SOURCE    -> EXECUTION_SOURCE
#   labels.js:20-21   METRIC              -> METRIC
#   labels.js:22      GRID_AXIS           -> GRID_AXIS
#   labels.js:23      STRATEGY            -> STRATEGY
#   labels.js:24-29   RISK_CONFIG         -> RISK_CONFIG
#   labels.js:31-33   TABLES              -> TABLES
#   labels.js:36-46   zh                  -> zh
#   labels.js:49-52   labeled             -> labeled
#
# 镜像关系（同一份表的第四份拷贝，四者必须一致）：
#   1. plugins/datasource/python/trading_datasource/labels.py —— 唯一事实来源（产出侧打 *_label）；
#   2. plugins/workbench/src/labels.js —— Host 侧镜像，本文件的逐行来源；
#   3. plugins/workbench/src/client.js 里的内置 ZH —— 浏览器侧回退表；
#   4. 本文件 —— 平台服务（FastAPI 进程）侧的第四份镜像。
# 关系 1↔2↔3 由 tests/test_labels.py 解析比对守护；本文件不在那份守护内（该测试只认
# JS 侧三份），由 tests/test_wp6_summary_audit.py 的 test_tables_match_labels_js 逐表比对
# labels.js 的键值，防止这第四份漂移。
#
# 有意差异（诚实边界）：
#   1. zh() 对「不可作为字典键」的值（dict/list/set）不回退到 Python 的 canonical 实现
#      （那里 `value in table` 会抛 TypeError），而是按 labels.js 的 `hasOwnProperty` 语义
#      返回 fallback/原值。JSON 载荷里这类值不会出现在结论字段上。
#   2. 大小写不敏感查找同 labels.js:42 `Object.keys(dict).find(...)`：命中**第一个**键，
#      而不是 canonical zh 的「最后一个」。SIDE 表只有整数键，字符串值不参与小写回退。
#   3. value is None 时返回 fallback（labels.js:37 的 `value == null` 覆盖 null 与
#      undefined），调用方未传 fallback 时返回 None，与 JS 的 undefined 对应。


SIGNAL = {"BUY": "买入", "SELL": "卖出", "HOLD": "观望"}
ACTION = {"BUY": "买入", "SELL": "卖出"}
SIDE = {1: "买入", 2: "卖出"}
TRADE_TYPE = {"buy": "买入", "sell": "卖出"}
TRADE_STATUS = {"open": "持仓中", "closed": "已平仓"}
RATING = {"Buy": "买入", "Overweight": "增持", "Hold": "持有", "Underweight": "减持", "Sell": "卖出"}
SOURCE_STATUS = {"ok": "正常", "warn": "待配置", "fail": "异常", "empty": "无数据"}
EXECUTION_TIMING = {"previous_bar_next_open": "信号次一交易日开盘成交"}
END_POSITION_POLICY = {"mark_to_market_no_liquidation": "按最后收盘价盯市，不强制平仓"}
EXECUTION_SOURCE = {"local_simulation": "本地模拟（非券商成交）"}
METRIC = {"total_return": "累计收益", "annualized": "年化收益", "sharpe": "夏普比率",
          "max_drawdown": "最大回撤", "win_rate": "胜率"}
GRID_AXIS = {"fast": "快线", "slow": "慢线", "rsi_buy": "买入阈值", "rsi_sell": "卖出阈值"}
STRATEGY = {"ma_cross": "双均线", "rsi": "RSI"}
RISK_CONFIG = {
    "risk_per_trade": "单笔风险占权益比",
    "stop_atr_mult": "ATR 止损倍数",
    "max_positions": "最大持仓数",
    "max_position_pct": "单标的上限占权益比",
}

TABLES = {"SIGNAL": SIGNAL, "ACTION": ACTION, "SIDE": SIDE, "TRADE_TYPE": TRADE_TYPE,
          "TRADE_STATUS": TRADE_STATUS, "RATING": RATING, "SOURCE_STATUS": SOURCE_STATUS,
          "EXECUTION_TIMING": EXECUTION_TIMING, "END_POSITION_POLICY": END_POSITION_POLICY,
          "EXECUTION_SOURCE": EXECUTION_SOURCE, "RISK_CONFIG": RISK_CONFIG,
          "METRIC": METRIC, "GRID_AXIS": GRID_AXIS, "STRATEGY": STRATEGY}


def _hashable(value):
    """labels.js:39 `hasOwnProperty(dict, value)` 的前提：值能当键。"""
    return isinstance(value, (str, int, float, bool, tuple)) or value is None


def zh(table, value, fallback=None):
    """labels.js:36-46：按表翻译；查不到原样返回（不吞掉未知值，便于发现新枚举）。"""
    if value is None:
        return fallback
    dictionary = TABLES.get(table) or {}
    if _hashable(value) and value in dictionary:
        return dictionary[value]
    if isinstance(value, str):
        # 大小写不敏感：历史记录里 BUY / buy / Buy 都出现过
        for key, label in dictionary.items():
            if isinstance(key, str) and key.lower() == value.lower():
                return label
    return value if fallback is None else fallback


def labeled(value, label, table, fallback=None):
    """labels.js:49-52：优先用产出侧给的中文标签，没有才按码翻译（旧记录走这里）。"""
    if isinstance(label, str) and label.strip():
        return label
    return zh(table, value, fallback)


__all__ = [
    "ACTION", "END_POSITION_POLICY", "EXECUTION_SOURCE", "EXECUTION_TIMING", "GRID_AXIS",
    "METRIC", "RATING", "RISK_CONFIG", "SIDE", "SIGNAL", "SOURCE_STATUS", "STRATEGY",
    "TABLES", "TRADE_STATUS", "TRADE_TYPE", "labeled", "zh",
]
