"""关注池读取的唯一实现（WP9 修订 I4）：配置读取 + 命名池 + 市场分片。

为什么单独一个模块：planner（symbols_for_market）、daemon（``@watchlist`` 占位替换）、
strategies（组合策略 universe）三处都要读关注池，各写一遍已漂移出「有/无市场过滤」
两种口径——正是 K1（无市场维度导致跨市场分母与截断）的根因。这里收敛成一处。

分层：本模块依赖 ``daemon.platform_config``（配置读取的唯一实现）与
``planner.CALENDAR_MARKET``（SZ/BJ 归 SH 链的口径），两者都在**函数内**惰性
import——planner/daemon 逆向依赖本模块，模块级 import 会成环。
"""
import json
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

#: 首启配置入口（E2E 缺陷 7）：从 ``universe`` 表的指数成分快照生成关注池。
def init_from_index(home, index_name, limit=None, force=False, today=None, conn=None):
    """把 ``universe`` 里该指数的成分快照写成配置的关注池（首个可用入口）。

    为什么需要它：关注池为空时**全部数据作业静默跳过**（无行情、无因子、无计划），
    而界面上此前没有任何提示——首启「平台在跑但什么都没发生」。本函数给出最小可用的
    初始化路径：``trading_core watchlist-init --from-index SH.000300``。

    - **保留其他键**：只合并顶层 ``watchlist``，原子写（tmp + replace）；
    - **不覆盖已有非空关注池**（除非 ``force=True``）——用户手配的池子优先；
    - **幂等**：内容一致时标 ``unchanged`` 且不写盘；
    - 指数快照缺失 → 如实报错（不生成空池假装成功）。
    """
    from datetime import date as _date

    from . import store as _store
    day = today or _date.today().isoformat()
    own = conn is None
    conn = conn or _store.connect(_store.db_path(str(home)))
    try:
        snap = _store.read_universe(conn, as_of=day, index_name=index_name)
    finally:
        if own:
            conn.close()
    if snap is None or not snap.get("symbols"):
        return {"ok": False,
                "error": f"universe 表没有 {index_name} 在 {day} 之前的成分快照"
                         f"（先跑 ``trading_core universe --index {index_name}``）"}
    symbols = [str(s).strip().upper() for s in snap["symbols"] if str(s).strip()]
    symbols = sorted(set(
        s if "." in s else f"{'SH' if s[:1] in ('6', '9') else 'SZ' if s[:1] in ('0', '3') else 'BJ'}.{s}"
        for s in symbols))
    if limit is not None:
        symbols = symbols[:int(limit)]
    path = _home_path(home) / "trading-platform.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(config, dict):
            config = {}
    except (OSError, ValueError):
        # 坏配置不静默覆盖（宁缺毋假）：报错让人先处理
        return {"ok": False, "error": f"{path} 无法解析为 JSON，先修正后再初始化"}
    current = config.get(DEFAULT_POOL_KEY) or []
    # 幂等优先：内容本来就一致 → 成功且不写盘（重复执行初始化不该报「已配置」）
    if list(current) == symbols:
        return {"ok": True, "unchanged": True, "watchlist": symbols, "source": index_name}
    if current and not force:
        return {"ok": False,
                "error": f"关注池已配置（{len(current)} 个标的）；如需覆盖请加 --force"}
    config[DEFAULT_POOL_KEY] = symbols
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "unchanged": False, "watchlist": symbols,
            "source": index_name, "snapshot_date": snap.get("snapshot_date")}

