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
#   1. `Object.prototype.hasOwnProperty.call(dict, value)` 会把 value 强制成**对象键**再查表
#      （ToPropertyKey -> ToString）：`zh("SIDE", "1")` 与 `zh("SIDE", 1)` 一样命中，
#      `zh("SIDE", true)` 查的是键 "true"（不命中 -> 返回 true 本身）。本实现按同一规则
#      把表键与查询值都过一遍 js_key_string()（补遗 B 审查项 3）。
#   2. 大小写不敏感查找同 labels.js:42 `Object.keys(dict).find(...)`：命中**第一个**键。
#   3. value is None 时返回 fallback（labels.js:37 的 `value == null` 覆盖 null 与
#      undefined）；fallback 缺省时返回原值。Python 无法区分「未传 fallback」与「显式传
#      null」（JS 里 `fallback === undefined` 才返回原值），服务侧没有传 null fallback 的
#      调用点，因此按前者解释。
#   4. 查不到时原样返回未知值（不吞枚举），fallback 仅在该参数非 None 时生效。
from server import _js


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


def zh(table, value, fallback=None):
    """labels.js:36-46：按表翻译；查不到原样返回（不吞掉未知值，便于发现新枚举）。

    `hasOwnProperty` 的键强制转换是关键：JS 对象的键永远是字符串，所以 `zh("SIDE", "1")`
    命中 SIDE[1]，而 `zh("SIDE", True)` 查键 "true" 不命中、返回 True 本身（不是「买入」）。
    若直接 `value in {1: ...}`，Python 会把 True 当成 1（hash 相同）而错误命中。
    """
    if value is None:
        return fallback
    dictionary = TABLES.get(table) or {}
    key = _js.js_key_string(value)
    for existing, label in dictionary.items():
        if _js.js_key_string(existing) == key:
            return label
    if isinstance(value, str):
        # 大小写不敏感：历史记录里 BUY / buy / Buy 都出现过
        lowered = value.lower()
        for existing, label in dictionary.items():
            if _js.js_key_string(existing).lower() == lowered:
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
