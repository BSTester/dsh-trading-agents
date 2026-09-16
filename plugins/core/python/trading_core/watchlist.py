"""关注池读取的唯一实现（WP9 修订 I4）：配置读取 + 命名池 + 市场分片。

为什么单独一个模块：planner（symbols_for_market）、daemon（``@watchlist`` 占位替换）、
strategies（组合策略 universe）三处都要读关注池，各写一遍已漂移出「有/无市场过滤」
两种口径——正是 K1（无市场维度导致跨市场分母与截断）的根因。这里收敛成一处。

分层：本模块依赖 ``daemon.platform_config``（配置读取的唯一实现）与
``planner.CALENDAR_MARKET``（SZ/BJ 归 SH 链的口径），两者都在**函数内**惰性
import——planner/daemon 逆向依赖本模块，模块级 import 会成环。
"""
import os
from pathlib import Path

#: 缺省池键：trading-platform.json 顶层 ``watchlist``（既有扁平列表口径）。
DEFAULT_POOL_KEY = "watchlist"


def _home_path(home=None):
    """配置根：显式 home 优先，否则 $DSH_HOME，再否则 ~/.dsh（daemon 同口径）。"""
    return Path(home) if home is not None else Path(
        os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


def read_pool(home, key=None):
    """读命名池 → ``(symbols, exists)``。

    - ``symbols``：大写归一、去空、按原始顺序（确定性由调用方排序决定）；
    - ``exists``：该键是否**存在**于配置——「键不存在」与「键为空列表」语义不同：
      前者在 strict 调用下是配置错误（fail-closed），后者是合法的空池；
    - 字符串值按逗号切分（兼容 ``"SH.600519,HK.00700"`` 既有写法：字符串按字符
      迭代会静默产出垃圾标的）。
    """
    from .daemon import platform_config
    name = key or DEFAULT_POOL_KEY
    config = platform_config(_home_path(home)) or {}
    if name not in config:
        return [], False
    raw = config[name] or []
    if isinstance(raw, str):
        raw = raw.split(",")
    symbols = []
    for item in raw:
        text = str(item).strip().upper()
        if text:
            symbols.append(text)
    return symbols, True


def watchlist_symbols(home, key=None, market=None, strict=False):
    """关注池标的（唯一实现）。

    - ``key``：池键名，缺省 ``DEFAULT_POOL_KEY``；
    - ``market``：给定时只取该市场链（``SH``/``HK``/``US``；SZ/BJ 归 SH 链）；
    - ``strict=True`` 且指定的池**不存在** → ``ValueError``（fail-closed：宁可跳过该
      策略并告警，也不静默换一个池子——用错池子的后果是拿别的标的下单）。
    """
    symbols, exists = read_pool(home, key)
    if strict and not exists:
        raise ValueError(f"关注池键不存在：{key or DEFAULT_POOL_KEY}")
    if market is None:
        return symbols
    from .planner import CALENDAR_MARKET
    return [s for s in symbols if CALENDAR_MARKET.get(s.split(".", 1)[0]) == market]
