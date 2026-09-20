"""FR-DATA-003：PIT（Point-in-Time）历史数据读取的**唯一入口**。

规格原文（``docs/v3-spec.md:288``）::

    系统应确保所有历史数据查询遵循 Point-in-Time 原则。平台侧的 ``data/cache.py``
    作为唯一数据读取接口，确保所有历史数据查询遵循 PIT 原则，防止前视偏差。

本模块就是那个入口。它把此前**散落在 5 处**的 PIT 约束收敛成一份实现：

===============================  ====================================================
原位置                            约束（迁移后全部改走本模块）
===============================  ====================================================
``v3_analytics._read_pit_fundamentals``  ``announced_at`` 非空且 ≤ ``as_of``（没有公告日的行不可见）
``v3_analytics._latest_period_rows``     ``period_end`` ≤ ``as_of`` 才可参与「最新报告期」选择
``v3_analytics.sentiment_factor_values`` ``sentiment_snapshots.date`` ≤ ``as_of``
``v3_ml._feature_matrix``                ``t`` 日特征只用 ``closes[0..t]``（``LAG_SAME_DAY``）
``v3_math.backtest_momentum``            ``t`` 日持仓只用 ``closes[0..t-1]``（``LAG_PREV_DAY``）
``trading._local_close``                 本地日线最近收盘 ``ts`` ≤ 当日
===============================  ====================================================

一、``as_of`` 语义（**显式选择，不许含糊**）
-------------------------------------------
``as_of`` 是「截至时点」，必须是 ``YYYY-MM-DD``（**必填**，缺失直接抛 :class:`PitError`
——与 ``trading_core.store._require_as_of`` 同一条「读取必须显式给 as_of」纪律）。
当日算不算「已知」，由调用方从两个模式里**显式**挑一个：

``AS_OF_INCLUSIVE``（含 ``as_of`` 当日）
    ``ts <= as_of``。语义 = 「``as_of`` 当日**收盘后**视角」：当日已收盘的日 K、
    当日已披露的公告都视为已知。用于「今天已收盘，我要看截至今天的截面」。

``AS_OF_EXCLUSIVE``（不含 ``as_of`` 当日）
    ``ts < as_of``。语义 = 「``as_of`` 当日**开盘前/盘中**视角」：当日的数据还没产生，
    只能用到 ``as_of`` 前一日。用于「站在 ``t`` 日开盘前做决策，只许用 ≤ ``t-1``」。

比较一律是 **字符串比较**（``ts`` 与 ``as_of`` 都是 ``YYYY-MM-DD`` 文本）：这与既有 SQL
（``trading_core.store`` 的 ``ts<=?`` / ``announced_at<=?``）**逐字同口径**，因此内存闸门
与 SQL 闸门不可能给出不同答案。注意由此产生的一个**既有事实**：``announced_at`` 若带时间
成分（``2026-09-20T05:00:00Z``），``INCLUSIVE`` + ``as_of=2026-09-20`` 也会把它排除
（字符串 ``"2026-09-20T…" > "2026-09-20"``）——这是迁移前就有的行为，本模块如实保留、
不做「顺手修正」（改它属于口径变更，必须单独评审）。

二、缓存键（最容易出错的地方）
------------------------------
TTL 层**复用** ``server.caches``（内存 + 磁盘两级、``CACHE_TTL_MS`` 唯一 TTL 表），
本模块不自造第二套过期机制。缓存键是 ``f"{endpoint}|{stable_key(payload)}"``，payload 至少含::

    {"store": "sqlite:/…/trading.sqlite",   # 数据源身份：不同 home/临时库不得串味
     "symbol": "SH.600519",                 # 标的（横截面按标的分别成键）
     "period": "1d", "limit": 1,
     "as_of": "2026-09-20",                 # ★ 必须进键
     "mode": "inclusive"}                   # ★ 必须进键

三条**不得删**的理由（删任一条都会重新引入前视/串味）：

1. **``as_of`` 进键**：``as_of=2026-09-20`` 与 ``as_of=2026-09-21`` 是两个不同的历史视图。
   若共用键，先算的那次会把它的窗口（可能更长、含更晚的行）喂给后一次请求——前视偏差
   的典型来源。``tests/test_data_cache.py::CacheKeyIsolationTests`` 用「同一标的、只改
   as_of」钉住这一点。
2. **``mode`` 进键**：``ts<=as_of`` 与 ``ts<as_of`` 差一整天（当日那一根 K 线）。
3. **``store`` 进键**：同一个标的 + 同一个 ``as_of`` 在不同交易库（不同 ``home``、
   测试临时库）里是完全不同的数据；少了它，跨库/跨测试就会读到别人的行。
   ``conn`` 是内存库（``PRAGMA database_list`` 无文件）时身份不可知 → **不缓存**，
   除非调用方显式给 ``store=``。

三、缓存的三条边界（为什么有的路径**有意不缓存**）
--------------------------------------------------
* 本地交易库读取（fundamentals / sentiment / 本地日 K）：走 TTL 缓存。这些表由作业
  按日写入，TTL 取与同类面板端点同量级（见 ``caches.CACHE_TTL_MS`` 的 ``pit-*`` 三条）。
* **上游工具取的 K 线（``fetch=``）不走本模块的 TTL 层**：``series`` 端点在上层
  ``app.py`` 已经通过 ``caches.cached("series", …)`` 有 10 分钟 TTL（键
  ``series|{ticker,period,limit}``）。在这里再包一层就是「第二套 TTL」，而且会让
  「上游刚更新、本层还没过期」变成一个新的陈旧来源。本模块只对它做 **PIT 过滤**（这一步
  从不缓存、每次现算）——PIT 闸门与新鲜度因此互不干扰。``test_data_cache`` 的
  ``NoCachePathTests`` 钉住「fetch 路径不写缓存」。
* **写路径上的风险基准价（``trading._local_close``）以 ``cache=False`` 调用**：它是下单前
  风控的价格基准，必须拿最新读数，不允许 TTL 陈旧值；但**仍然**过本模块的 PIT 闸门
  （``ts <= today``），因此「唯一入口」不被绕过。理由同时写在 ``trading._local_close``
  的注释与 ``docs/e2e-and-data-gaps.md``。

四、返回结构（可核验）
----------------------
所有读取函数返回同一个信封（字段齐全、缺失即 ``null`` + 原因，**绝不填 0/估算**）::

    {"ok": True, "kind": "bars"|"fundamentals"|"sentiment"|"bars-frame",
     "as_of": "2026-09-20", "mode": "inclusive",
     "semantics": "含 as_of 当日：…",
     "source": "trading-data/trading.sqlite:bars" | "futu/quote_history_kline",
     "window": {"start": …, "end": …} | None,     # 实际用到的记录区间
     "rows_used": 299,                            # 实际用到的记录数（可核验）
     "bars": [...] | None, "closes": [...] | None, "derived": {...} | None,
     "rows": [...] | None,
     "rejected": {"future": 0, "undated": 0},      # 被 PIT 闸门挡掉的行数（未来的行）
     "missing": None | {"code": …, "reason": …},   # 没数据时给原因，不给 0
     "cached": True|False,
     "cache_policy": "ttl:pit-bars" | "none:…"}

``rejected.future`` 是**鉴别力证据**：``as_of=t`` 的读数里，凡是 ``> t`` 的记录都必须被
计数挡掉（``selfcheck`` 与测试据此断言「窗口内不含未来记录」）。

五、派生量
----------
``closes`` / ``derived.simpleReturns`` **只由可见窗口内的 bar 算出**，因此派生量在数学上
不可能是未来的函数。第一根的收益没有前值 → ``None``（不是 0）。

本模块只依赖标准库 + ``server.caches``；``trading_core.store`` 在**函数内**延迟导入
（``server.v3_math`` 等纯计算模块也 import 本模块，模块级导入会把整条交易栈拉进
纯算术单测）。所有函数只读：不写库、不建表、不 migrate、不碰网络（``fetch`` 由调用方
注入，本模块不认识任何具体数据源）。
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from server import caches

__all__ = [
    "AS_OF_EXCLUSIVE",
    "AS_OF_INCLUSIVE",
    "AS_OF_MODES",
    "ENDPOINT_BARS",
    "ENDPOINT_FUNDAMENTALS",
    "ENDPOINT_SENTIMENT",
    "LAG_PREV_DAY",
    "LAG_SAME_DAY",
    "PitError",
    "PitSourceError",
    "close_series",
    "latest_period_rows",
    "normalize_as_of",
    "open_store",
    "pit_prefix",
    "pit_rows",
    "read_bars",
    "read_bars_frame",
    "read_fundamentals",
    "read_sentiment_snapshots",
    "returns_of",
    "selfcheck",
    "semantics",
    "semantics_text",
    "store_path",
    "visible_at",
    "window_of",
]

# ---------------------------------------------------------------------------
# 常量：as_of 模式与滞后档
# ---------------------------------------------------------------------------
#: 含 ``as_of`` 当日：``ts <= as_of``（当日收盘后视角）。
AS_OF_INCLUSIVE = "inclusive"
#: 不含 ``as_of`` 当日：``ts < as_of``（当日开盘前/盘中视角）。
AS_OF_EXCLUSIVE = "exclusive"
AS_OF_MODES = (AS_OF_INCLUSIVE, AS_OF_EXCLUSIVE)

#: 滞后档：特征/读数在 ``t`` 日可见 ``≤ t``（当日已收盘的收盘价是已知信息）。
LAG_SAME_DAY = 0
#: 滞后档：站在 ``t`` 日做决策，只可用 ``≤ t-1``（当日还没收盘）。
LAG_PREV_DAY = 1

# PIT 读数端点（TTL 值在 ``caches.CACHE_TTL_MS`` 里，本模块不另存一张 TTL 表）。
ENDPOINT_BARS = "pit-bars"
ENDPOINT_FUNDAMENTALS = "pit-fundamentals"
ENDPOINT_SENTIMENT = "pit-sentiment"

#: 交给 ``trading_core.store.read_bars`` 的时间上界：**故意设成最大值**。
#: 时间上界**只**由本模块的 PIT 闸门决定；若这里再传一个真实 as_of，闸门就有了两份实现
#: （SQL 一份、内存一份），``EXCLUSIVE`` 模式更是无法用 ``<=`` 的 SQL 表达。
_NO_UPPER_BOUND = "9999-12-31"


class PitError(ValueError):
    """调用方参数错误（as_of 缺失/非法、mode 未知、缺数据源）——显式抛，不静默兜底。"""


class PitSourceError(RuntimeError):
    """上游取数失败（``fetch`` 回调抛出、本地库读失败）。调用方按自己的错误信封口径处理。

    ``code``/``details`` 保留上游语义，避免「取数失败」被泛化成一件事。
    """

    def __init__(self, message, *, code="pit/source-failed", details=None):
        super().__init__(str(message))
        self.code = str(code)
        self.details = dict(details or {})


# ---------------------------------------------------------------------------
# as_of 语义（唯一实现）
# ---------------------------------------------------------------------------
def normalize_as_of(as_of):
    """``as_of`` → ``YYYY-MM-DD``；缺失/非法 → :class:`PitError`（**不**回退到今天）。

    「缺省今天」正是前视偏差最喜欢的默认值（回测忘了传 as_of 就悄悄用了未来数据），
    因此这里与 ``trading_core.store._require_as_of`` 同一取向：**必须显式给**。
    """
    if as_of in (None, ""):
        raise PitError("PIT 纪律：读取必须显式提供 as_of（规格 §4.2 规则 1）")
    text = str(as_of).strip()[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise PitError(f"as_of 需为 YYYY-MM-DD，收到 {as_of!r}") from None
    return text


def check_mode(mode):
    """模式必须是两个显式取值之一（缺省/拼错 → 抛错，不猜）。"""
    if mode not in AS_OF_MODES:
        raise PitError(f"as_of 模式需显式选择 {'/'.join(AS_OF_MODES)}，收到 {mode!r}")
    return mode


def semantics_text(mode, as_of):
    """人类可读的口径说明（写进响应，页面原样展示）。"""
    check_mode(mode)
    if mode == AS_OF_INCLUSIVE:
        return (f"含 as_of 当日（ts <= {as_of}）：as_of 当日已收盘的日 K / 已披露的公告"
                f"视为已知（收盘后视角）")
    return (f"不含 as_of 当日（ts < {as_of}）：as_of 当日的记录一律不可见"
            f"（开盘前/盘中视角）")


def semantics(mode, as_of):
    """口径的结构化描述（``mode`` / ``asOf`` / ``bound`` / ``text``）。"""
    check_mode(mode)
    bound = f"ts <= {as_of}" if mode == AS_OF_INCLUSIVE else f"ts < {as_of}"
    return {"mode": mode, "asOf": as_of, "bound": bound, "text": semantics_text(mode, as_of)}


def visible_at(ts, as_of, mode):
    """单条记录在 ``as_of``/``mode`` 下是否可见（**唯一的可见性判定**）。

    缺失时间戳 → ``False``（无法证明「当时已知」的行不得进入读数，宁缺毋假）。
    比较按字符串（与既有 SQL 的 TEXT 比较逐字一致）。
    """
    check_mode(mode)
    if ts in (None, ""):
        return False
    text = str(ts)
    return text <= as_of if mode == AS_OF_INCLUSIVE else text < as_of


def pit_rows(rows, as_of, mode, *, key="t"):
    """按 PIT 过滤行序列 → ``(visible, {"future": n, "undated": m, ...})``。

    ``future`` = 时间戳**晚于** ``as_of`` 而被挡掉的行数（前视证据）；
    ``undated`` = 没有时间戳、无法证明当时已知的行数。两者都**不静默丢弃**。
    """
    as_of = normalize_as_of(as_of)
    check_mode(mode)
    visible = []
    future = 0
    undated = 0
    for row in rows or []:
        stamp = row.get(key) if isinstance(row, dict) else None
        if stamp in (None, ""):
            undated += 1
            continue
        if visible_at(stamp, as_of, mode):
            visible.append(row)
        else:
            future += 1
    return visible, {"future": future, "undated": undated, "kept": len(visible)}


def pit_prefix(values, index, *, lag=LAG_SAME_DAY):
    """PIT 可见前缀：``values[: index + 1 - lag]``（**≤ t / ≤ t-1 边界的唯一实现**）。

    ``lag=LAG_SAME_DAY``（0）→ ``t`` 日可见 ``values[0..t]``（特征工程：当日收盘已知）；
    ``lag=LAG_PREV_DAY``（1）→ ``t`` 日可见 ``values[0..t-1]``（决策：当日尚未收盘）。
    越界（``index - lag < 0``）返回空序列——调用方必须自己处理「历史不足」，本函数
    绝不回填 0 或缩短滞后档。
    """
    if lag is None:
        raise PitError("lag 必须显式给出（LAG_SAME_DAY / LAG_PREV_DAY）")
    end = int(index) + 1 - int(lag)
    if end <= 0:
        return values[:0]
    return values[:end]


def window_of(rows, *, key="t"):
    """行序列的实际时间区间 ``{"start", "end"}``；空 → ``None``（不是编造的区间）。"""
    stamps = [str(row.get(key)) for row in rows or []
              if isinstance(row, dict) and row.get(key) not in (None, "")]
    if not stamps:
        return None
    return {"start": min(stamps), "end": max(stamps)}


# ---------------------------------------------------------------------------
# 派生量（只由可见窗口算出）
# ---------------------------------------------------------------------------
def _number(value):
    """尽力转成有限 ``float``；``bool``/NaN/Inf/不可转 → ``None``。

    与 ``v3_math.to_float`` 同一条规则。**为什么不 import 它**：``v3_math`` 要 import 本模块
    取 ``pit_prefix``（见其 ``backtest_momentum``），模块级反向 import 会成环；这条规则
    只有 4 行，重复一次比引入环依赖便宜。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def close_series(bars):
    """``bars`` → ``(closes, stamps)``：丢掉收盘价缺失/非数值的 bar（不插值、不清零）。"""
    closes = []
    stamps = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        close = _number(bar.get("c"))
        if close is None:
            continue
        closes.append(close)
        stamps.append(bar.get("t"))
    return closes, stamps


def returns_of(closes):
    """简单收益序列（``closes[i]/closes[i-1] - 1``）：第一格 ``None``（没有前值 ≠ 0）。"""
    out = [None]
    for index in range(1, len(closes or [])):
        prior = closes[index - 1]
        out.append(None if not prior else closes[index] / prior - 1.0)
    return out


# ---------------------------------------------------------------------------
# home / 只读连接
# ---------------------------------------------------------------------------
def _home_path(home):
    """``home`` 归一成路径：缺省 ``$DSH_HOME`` → ``~/.dsh``（与 ``v3_analytics._home_path`` 同口径）。"""
    import os
    if home in (None, ""):
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    return Path(str(home)).expanduser()


def store_path(home):
    """交易库路径（``<home>/trading-data/trading.sqlite``）。"""
    return _home_path(home) / "trading-data" / "trading.sqlite"


def open_store(home):
    """**只读**打开交易库（``mode=ro``）；库不存在/打不开 → ``None``。

    与 ``v3_analytics._open_trading_store`` 同一取向：只发 ``SELECT``，不 import
    ``trading_core.store`` 的建表/migrate 路径（读数口不该在取数路径上写库）。
    """
    path = store_path(home)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    return conn


# ---------------------------------------------------------------------------
# 缓存（复用 caches.py；键含 as_of / mode / 标的 / 数据源身份）
# ---------------------------------------------------------------------------
def _store_identity(conn, store):
    """缓存键里的数据源身份：显式 ``store`` 优先，否则取 ``PRAGMA database_list`` 的文件路径。"""
    if store not in (None, ""):
        return str(store)
    if conn is None:
        return None
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        try:
            name, path = row[1], row[2]
        except (IndexError, TypeError):
            continue
        if name == "main" and path:
            return f"sqlite:{path}"
    return None  # 内存库等无稳定身份 → 调用方须显式 store= 才缓存


def _ttl_ms(endpoint):
    """TTL 只有 ``caches.CACHE_TTL_MS`` 一份（本模块不另存表）。"""
    return int(caches.CACHE_TTL_MS.get(endpoint, 0) or 0)


def _cache_get(endpoint, payload, force):
    if force:
        return None
    ttl = _ttl_ms(endpoint)
    if ttl <= 0:
        return None
    hit = caches.read(endpoint, payload, ttl)
    return None if hit is None else hit[1]


def _cache_put(endpoint, payload, value):
    if _ttl_ms(endpoint) <= 0:
        return
    caches.write(endpoint, payload, value)


def _payload(endpoint, *, store, symbol, period=None, limit=None, as_of, mode, extra=None):
    """缓存 payload（键的原料）。

    ★ ``as_of`` 与 ``mode`` 在键里是**硬要求**：同一标的、只改 ``as_of`` 必须是两个不同的
    缓存条目，否则「先算的那次」会把它的窗口喂给后来者 —— 前视偏差就是这样进来的。
    ★ ``store`` 是数据源身份：不同 home/临时库的同名标的不得串味。
    """
    body = {"endpoint": endpoint, "store": store, "symbol": symbol, "as_of": as_of, "mode": mode}
    if period is not None:
        body["period"] = period
    if limit is not None:
        body["limit"] = int(limit)
    if extra:
        body.update(extra)
    return body


# ---------------------------------------------------------------------------
# 本地交易库读取（本地路径的原始行；时间闸门**不在这里**）
# ---------------------------------------------------------------------------
def _ensure_row_factory(conn):
    """``store.read_bars`` 按列名取值（``r["ts"]``）——调用方给的裸连接补上 row_factory。"""
    if conn is not None and getattr(conn, "row_factory", None) is None:
        conn.row_factory = sqlite3.Row
    return conn


def _local_bars(conn, symbol, period, limit):
    """本地 ``bars`` 全量行（升序，**不限时间**；时间上界由本模块闸门定，见 ``_NO_UPPER_BOUND``）。"""
    if conn is None:
        raise PitError("本地读取需要 conn=（sqlite3 连接）或 home=")
    _ensure_row_factory(conn)
    try:  # 延迟导入：纯计算模块（v3_math/v3_ml）也 import 本模块，不该被交易栈拖住
        from trading_core import store as core_store
    except ImportError as error:
        raise PitSourceError(f"trading_core 不可导入，本地 bars 读取不可用：{error}",
                             code="pit/core-unavailable") from error
    try:
        bars = core_store.read_bars(conn, str(symbol), str(period), _NO_UPPER_BOUND, limit=None)
    except Exception as error:  # noqa: BLE001 —— 表缺失/坏连接都算「取不到」
        raise PitSourceError(f"本地 bars 读取失败：{error}",
                             code="pit/local-read-failed") from error
    if limit is not None and int(limit) > 0:
        bars = bars[-int(limit):]
    return bars


def _local_fundamentals(conn, symbol):
    """本地 ``fundamentals`` 全量行（同一标的、按 ``period_end`` 升序）。

    与 ``trading_core.store.read_fundamentals`` 同列同序；时间闸门（``announced_at`` /
    ``period_end``）由本模块施加。
    """
    if conn is None:
        raise PitError("本地读取需要 conn=（sqlite3 连接）或 home=")
    _ensure_row_factory(conn)
    try:
        rows = conn.execute(
            "SELECT field,period_end,announced_at,value,source FROM fundamentals"
            " WHERE symbol=? ORDER BY period_end", (str(symbol),)).fetchall()
    except sqlite3.Error as error:
        raise PitSourceError(f"本地 fundamentals 读取失败：{error}",
                             code="pit/local-read-failed") from error
    return [dict(row) for row in rows]


def _local_sentiment(conn, symbol):
    """本地 ``sentiment_snapshots`` 全量行（同一标的；按 ``date`` 升序）。"""
    if conn is None:
        raise PitError("本地读取需要 conn=（sqlite3 连接）或 home=")
    _ensure_row_factory(conn)
    try:
        rows = conn.execute(
            "SELECT date,symbol,source,payload FROM sentiment_snapshots"
            " WHERE symbol=? ORDER BY date", (str(symbol),)).fetchall()
    except sqlite3.Error as error:
        raise PitSourceError(f"本地 sentiment_snapshots 读取失败：{error}",
                             code="pit/local-read-failed") from error
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# 读取：日 K（单标的）
# ---------------------------------------------------------------------------
def read_bars(as_of, mode, *, symbol, conn=None, fetch=None, period="1d", limit=None,
              source=None, home=None, store=None, cache=True, force=False):
    """按 ``as_of`` 读取某标的的日 K 与派生量（**唯一的 K 线 PIT 入口**）。

    ``conn`` 给本地交易库连接（或 ``home=`` 由本模块只读打开）；``fetch`` 给上游回调
    ``fetch(symbol) -> (bars, source)``（如包一层 ``v3_run("series", …)``），二者取一，
    同时给则 ``fetch`` 优先。上游失败请让 ``fetch`` 抛异常 —— 本函数会把它包成
    :class:`PitSourceError` 抛出（**不**缓存失败，也不把它伪装成「没有数据」）。

    ``limit`` 是「可见窗口里最近的 N 根」（先过 PIT 闸门再截断，顺序不能反）。
    返回信封见模块 docstring；``bars``/``closes``/``derived`` 在无数据时为 ``None`` +
    ``missing`` 里的原因。
    """
    as_of = normalize_as_of(as_of)
    check_mode(mode)
    symbol = str(symbol or "").strip()
    if not symbol:
        raise PitError("symbol 必填（PIT 读取永远针对确定标的）")
    period = str(period or "1d")
    if limit is not None:
        limit = int(limit)
        if limit <= 0:
            raise PitError(f"limit 需为正整数，收到 {limit}")

    owns_conn = False
    if fetch is None and conn is None:
        if home is None:
            raise PitError("read_bars 需要 conn=（sqlite 连接）、home=（交易库所在 home）"
                           "或 fetch=（上游回调）之一")
        conn = open_store(home)
        owns_conn = True
        if conn is None:
            core = _missing_core("bars", as_of, mode, symbol, period,
                                 code="pit/no-store",
                                 reason=f"交易库 {store_path(home)} 不存在或不可只读打开")
            return _envelope(core, source=None, cached=False,
                             cache_policy="none:no-store")

    identity = None
    if fetch is None:
        identity = _store_identity(conn, store)
    use_ttl = bool(cache) and fetch is None and identity is not None
    payload = _payload(ENDPOINT_BARS, store=identity or store, symbol=symbol, period=period,
                       limit=limit, as_of=as_of, mode=mode,
                       extra={"origin": "fetch" if fetch is not None else "local"})
    try:
        if use_ttl:
            hit = _cache_get(ENDPOINT_BARS, payload, force)
            if hit is not None:
                return _envelope(hit, source=hit.get("source"), cached=True,
                                 cache_policy=f"ttl:{ENDPOINT_BARS}")
        if fetch is not None:
            try:
                raw, src = _call_fetch(fetch, symbol)
            except PitSourceError:
                raise
            except Exception as error:  # noqa: BLE001 —— 上游异常原样带原因
                raise PitSourceError(str(error) or error.__class__.__name__) from error
            src = src or source
            cache_policy = ("none:上游 series 端点已由 caches.cached('series') 提供 10 分钟 TTL，"
                            "本层只做 PIT 过滤，不叠第二套 TTL")
        else:
            raw = _local_bars(conn, symbol, period, None)
            src = source or f"{store_path(home) if home else (identity or 'local')}:bars"
            cache_policy = (f"ttl:{ENDPOINT_BARS}" if use_ttl
                            else "none:数据源身份不可知（内存库）→ 不缓存")
        core = _bars_core(raw, as_of, mode, symbol, period, limit, src)
        if use_ttl:
            _cache_put(ENDPOINT_BARS, payload, core)
        return _envelope(core, source=src, cached=False, cache_policy=cache_policy)
    finally:
        if owns_conn and conn is not None:
            conn.close()


def _bars_core(raw, as_of, mode, symbol, period, limit, source):
    """原始行 → PIT 闸门 → 截断 → 信封核心（缓存里存的就是这个）。"""
    visible, stats = pit_rows(raw, as_of, mode, key="t")
    if limit is not None and len(visible) > limit:
        visible = visible[-limit:]
    closes, _stamps = close_series(visible)
    core = {
        "kind": "bars",
        "as_of": as_of,
        "mode": mode,
        "semantics": semantics_text(mode, as_of),
        "symbol": symbol,
        "period": period,
        "source": source,
        "window": window_of(visible),
        "rows_used": len(visible),
        "bars": visible or None,
        "closes": closes or None,
        "derived": ({"latestClose": closes[-1], "simpleReturns": returns_of(closes),
                     "n": len(closes)} if closes else None),
        "rejected": {"future": stats["future"], "undated": stats["undated"]},
        "missing": None,
    }
    if not visible:
        reasons = [f"{symbol} 在 {as_of}（{mode}）没有任何可见的 {period} 记录"]
        if stats["future"]:
            reasons.append(f"被 PIT 闸门挡掉 {stats['future']} 行（晚于 {as_of}）")
        if stats["undated"]:
            reasons.append(f"{stats['undated']} 行没有时间戳（无法证明当时已知）")
        core["missing"] = {"code": "pit/no-data", "reason": "；".join(reasons),
                           "symbol": symbol, "as_of": as_of}
    return core


def _missing_core(kind, as_of, mode, symbol, period, *, code, reason):
    return {
        "kind": kind,
        "as_of": as_of,
        "mode": mode,
        "semantics": semantics_text(mode, as_of),
        "symbol": symbol,
        "period": period,
        "source": None,
        "window": None,
        "rows_used": 0,
        "bars": None,
        "closes": None,
        "derived": None,
        "rejected": {"future": 0, "undated": 0},
        "missing": {"code": code, "reason": reason, "symbol": symbol, "as_of": as_of},
    }


def _call_fetch(fetch, symbol):
    """``fetch(symbol) -> (bars, source)``；也接受裸列表（来源记 ``None``）。"""
    result = fetch(symbol)
    if isinstance(result, tuple) and len(result) == 2:
        bars, source = result
    else:
        bars, source = result, None
    if bars is None:
        bars = []
    if not isinstance(bars, list):
        raise PitSourceError(f"fetch 必须返回 bars 列表，收到 {type(bars).__name__}",
                             code="pit/bad-fetch")
    return bars, source


def _envelope(core, *, source, cached, cache_policy):
    """核心 → 对外信封（加 ``cached`` / ``cache_policy``，不改核心字段）。"""
    return {
        "ok": True,
        **core,
        "source": core.get("source") if core.get("source") is not None else source,
        "cached": bool(cached),
        "cache_policy": cache_policy,
    }


# ---------------------------------------------------------------------------
# 读取：日 K（横截面）
# ---------------------------------------------------------------------------
def read_bars_frame(as_of, mode, *, symbols, conn=None, fetch=None, period="1d", limit=None,
                    source=None, home=None, store=None, cache=True, force=False):
    """横截面读取：逐标的走 :func:`read_bars`，聚合成一个可核验的截面信封。

    ``series`` 里每个标的都是 :func:`read_bars` 的完整信封（缺的标的是 ``missing``，
    **不占位、不填 0**）；顶层另给联合 ``window`` / 总 ``rows_used`` / 挡掉的未来行数。
    """
    as_of = normalize_as_of(as_of)
    check_mode(mode)
    names = [str(item).strip() for item in (symbols or []) if str(item).strip()]
    if not names:
        raise PitError("symbols 必填（横截面读取至少一个标的）")
    conn_provided = conn is not None
    shared = conn
    if not conn_provided and fetch is None and home is not None:
        shared = open_store(home)
    series = {}
    present = []
    missing = []
    rows_used = 0
    rejected_future = 0
    rejected_undated = 0
    stamps = []
    try:
        for name in names:
            entry = read_bars(as_of, mode, symbol=name, conn=shared, fetch=fetch, period=period,
                              limit=limit, source=source, home=home, store=store,
                              cache=cache, force=force)
            series[name] = entry
            rows_used += int(entry.get("rows_used") or 0)
            rejected_future += int((entry.get("rejected") or {}).get("future") or 0)
            rejected_undated += int((entry.get("rejected") or {}).get("undated") or 0)
            window = entry.get("window")
            if window:
                stamps.extend([window.get("start"), window.get("end")])
                present.append(name)
            else:
                missing.append({"symbol": name,
                                "code": (entry.get("missing") or {}).get("code"),
                                "reason": (entry.get("missing") or {}).get("reason")})
    finally:
        if shared is not None and not conn_provided and fetch is None:
            shared.close()
    frame_window = ({"start": min(stamps), "end": max(stamps)} if stamps else None)
    return {
        "ok": True,
        "kind": "bars-frame",
        "as_of": as_of,
        "mode": mode,
        "semantics": semantics_text(mode, as_of),
        "symbols": names,
        "source": source or (f"横截面 {len(present)}/{len(names)} 只有可见记录"),
        "window": frame_window,
        "rows_used": rows_used,
        "rejected": {"future": rejected_future, "undated": rejected_undated},
        "present": present,
        "missing": missing or None,
        "series": series,
        "cached": bool(all(item.get("cached") for item in series.values())) if series else False,
        "cache_policy": f"ttl:{ENDPOINT_BARS}（逐标的成键）",
    }


# ---------------------------------------------------------------------------
# 读取：基本面（PIT = announced_at）
# ---------------------------------------------------------------------------
def read_fundamentals(as_of, mode=AS_OF_INCLUSIVE, *, symbol, conn=None, home=None,
                      fields=None, period_ceiling=True, source=None, store=None,
                      cache=True, force=False):
    """PIT 基本面行（``announced_at`` 非空且按 ``mode`` 可见）→ 信封。

    可见性时间戳是 ``announced_at``（公告日）：**没有公告日的行一律不可见** —— 它们无法
    证明「当时已知」，拿报告期当可得日就是前视偏差（宁缺毋假，与迁移前逐字一致）。

    ``period_ceiling=True``（缺省）再叠一道独立闸门：``period_end <= as_of``。
    两道闸门是两件事：``announced_at`` 管「什么时候能看到」，``period_end`` 管「看到的是
    哪一期」。迁移前该闸门在 ``v3_analytics._latest_period_rows`` 里，对现有调用方
    逐字段等价（那里本来也会丢掉 ``period_end > as_of`` 的行）；收敛到本模块后，
    「未来报告期」不会再从别的调用路径漏进来。
    """
    as_of = normalize_as_of(as_of)
    check_mode(mode)
    symbol = str(symbol or "").strip()
    if not symbol:
        raise PitError("symbol 必填")
    own_conn = False
    if conn is None and home is not None:
        conn = open_store(home)
        own_conn = True
    identity = _store_identity(conn, store)
    field_list = None
    if fields:
        field_list = sorted({str(item) for item in fields})
    use_ttl = bool(cache) and identity is not None
    payload = _payload(ENDPOINT_FUNDAMENTALS, store=identity or store, symbol=symbol,
                       as_of=as_of, mode=mode,
                       extra={"fields": field_list, "period_ceiling": bool(period_ceiling)})
    try:
        if use_ttl:
            hit = _cache_get(ENDPOINT_FUNDAMENTALS, payload, force)
            if hit is not None:
                return {"ok": True, **hit, "cached": True,
                        "cache_policy": f"ttl:{ENDPOINT_FUNDAMENTALS}"}
        if conn is None:
            return {"ok": True, "kind": "fundamentals", "as_of": as_of, "mode": mode,
                    "semantics": semantics_text(mode, as_of), "symbol": symbol,
                    "source": None, "window": None, "rows_used": 0, "rows": None,
                    "rejected": {"future": 0, "undated": 0},
                    "missing": {"code": "pit/no-store",
                                "reason": f"交易库 {store_path(home)} 不存在或不可只读打开",
                                "symbol": symbol, "as_of": as_of},
                    "cached": False, "cache_policy": "none:no-store"}
        raw = _local_fundamentals(conn, symbol)
        visible, stats = pit_rows(raw, as_of, mode, key="announced_at")
        period_dropped = 0
        if period_ceiling:
            kept = []
            for row in visible:
                period = row.get("period_end")
                if period in (None, "") or str(period) <= as_of:
                    kept.append(row)
                else:
                    period_dropped += 1
            visible = kept
        if field_list:
            visible = [row for row in visible if str(row.get("field")) in set(field_list)]
        src = source or f"{store_path(home) if home else (identity or 'local')}:fundamentals"
        core = {
            "kind": "fundamentals",
            "as_of": as_of,
            "mode": mode,
            "semantics": semantics_text(mode, as_of),
            "symbol": symbol,
            "source": src,
            "window": window_of(visible, key="announced_at"),
            "rows_used": len(visible),
            "rows": visible or None,
            "rejected": {"future": stats["future"], "undated": stats["undated"],
                         "period_ceiling": period_dropped},
            "missing": None,
        }
        if not visible:
            reasons = []
            if stats["future"]:
                reasons.append(f"被 PIT 闸门挡掉 {stats['future']} 行（公告日晚于 {as_of}）")
            if stats["undated"]:
                reasons.append(f"{stats['undated']} 行没有公告日（无公告日 = 无法证明当时已知）")
            if period_dropped:
                reasons.append(f"另有 {period_dropped} 行的报告期晚于 {as_of}")
            if field_list:
                reasons.append(f"字段过滤 fields={field_list} 之后没有可用的 PIT 行")
            if not reasons:
                reasons.append(f"{symbol} 在 trading-data/fundamentals 里没有任何公告日 ≤ {as_of}"
                               " 的 PIT 记录")
            core["missing"] = {"code": "pit/no-data", "reason": "；".join(reasons),
                               "symbol": symbol, "as_of": as_of}
        if use_ttl:
            _cache_put(ENDPOINT_FUNDAMENTALS, payload, core)
        return {"ok": True, **core, "cached": False,
                "cache_policy": (f"ttl:{ENDPOINT_FUNDAMENTALS}" if use_ttl
                                 else "none:数据源身份不可知（内存库）→ 不缓存")}
    finally:
        if own_conn and conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# 读取：情绪快照（PIT = 采集日 date）
# ---------------------------------------------------------------------------
def read_sentiment_snapshots(as_of, mode=AS_OF_INCLUSIVE, *, symbol, conn=None, home=None,
                             limit=60, source=None, store=None, cache=True, force=False):
    """PIT 情绪快照行（``date`` 按 ``mode`` 可见）→ 信封；``limit`` = 最近的 N 条。

    这里只管**采集日**这一道闸门（快照按采集日落库）。文档自身的发布时间窗口由
    ``v3_analytics.sentiment_factor_values`` 在其上再叠一层（**两道闸门都要过**，
    与迁移前的口径逐条一致）——本模块不替它做窗口判断，因为它那一步要做的是打分，
    属于业务口径而不是数据读取。
    """
    as_of = normalize_as_of(as_of)
    check_mode(mode)
    symbol = str(symbol or "").strip()
    if not symbol:
        raise PitError("symbol 必填")
    limit = int(limit or 60)
    if limit <= 0:
        raise PitError(f"limit 需为正整数，收到 {limit}")
    own_conn = False
    if conn is None and home is not None:
        conn = open_store(home)
        own_conn = True
    identity = _store_identity(conn, store)
    use_ttl = bool(cache) and identity is not None
    payload = _payload(ENDPOINT_SENTIMENT, store=identity or store, symbol=symbol,
                       limit=limit, as_of=as_of, mode=mode)
    try:
        if use_ttl:
            hit = _cache_get(ENDPOINT_SENTIMENT, payload, force)
            if hit is not None:
                return {"ok": True, **hit, "cached": True,
                        "cache_policy": f"ttl:{ENDPOINT_SENTIMENT}"}
        if conn is None:
            return {"ok": True, "kind": "sentiment", "as_of": as_of, "mode": mode,
                    "semantics": semantics_text(mode, as_of), "symbol": symbol,
                    "source": None, "window": None, "rows_used": 0, "rows": None,
                    "rejected": {"future": 0, "undated": 0},
                    "missing": {"code": "pit/no-store",
                                "reason": f"交易库 {store_path(home)} 不存在或不可只读打开",
                                "symbol": symbol, "as_of": as_of},
                    "cached": False, "cache_policy": "none:no-store"}
        raw = _local_sentiment(conn, symbol)
        visible, stats = pit_rows(raw, as_of, mode, key="date")
        visible.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
        if len(visible) > limit:
            visible = visible[:limit]
        src = source or f"{store_path(home) if home else (identity or 'local')}:sentiment_snapshots"
        core = {
            "kind": "sentiment",
            "as_of": as_of,
            "mode": mode,
            "semantics": semantics_text(mode, as_of),
            "symbol": symbol,
            "source": src,
            "window": window_of(visible, key="date"),
            "rows_used": len(visible),
            "rows": visible or None,
            "rejected": {"future": stats["future"], "undated": stats["undated"]},
            "missing": None,
        }
        if not visible:
            reasons = [f"{symbol} 在 {as_of}（{mode}）没有可见的情绪快照"]
            if stats["future"]:
                reasons.append(f"被 PIT 闸门挡掉 {stats['future']} 行（采集日晚于 {as_of}）")
            if stats["undated"]:
                reasons.append(f"{stats['undated']} 行没有采集日（不可见）")
            core["missing"] = {"code": "pit/no-data", "reason": "；".join(reasons),
                               "symbol": symbol, "as_of": as_of}
        if use_ttl:
            _cache_put(ENDPOINT_SENTIMENT, payload, core)
        return {"ok": True, **core, "cached": False,
                "cache_policy": (f"ttl:{ENDPOINT_SENTIMENT}" if use_ttl
                                 else "none:数据源身份不可知（内存库）→ 不缓存")}
    finally:
        if own_conn and conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# 报告期选择（PIT = period_end）
# ---------------------------------------------------------------------------
def latest_period_rows(rows, as_of, mode=AS_OF_INCLUSIVE):
    """**最新可见报告期**的字段行 → ``(fields, period_end, announced_at, source, stats)``。

    ``fields`` = ``{field: 原始行}``（数值转换留给调用方，避免本模块依赖 ``v3_math``）；
    ``period_end > as_of`` 的报告期**不可见**（未来的财务期不能当「最新一期」）。
    没有任何可见期 → ``({}, None, None, None, stats)``。
    """
    visible, stats = pit_rows(rows, as_of, mode, key="period_end")
    by_period = {}
    for row in visible:
        period = str(row.get("period_end") or "")
        if not period:
            continue
        by_period.setdefault(period, {})[str(row.get("field"))] = row
    if not by_period:
        return {}, None, None, None, stats
    latest = max(by_period)
    fields = by_period[latest]
    sample = next(iter(fields.values()))
    return fields, latest, sample.get("announced_at"), sample.get("source"), stats


# ---------------------------------------------------------------------------
# 自检（只读；可 `python -m server.data.cache` 直接跑）
# ---------------------------------------------------------------------------
def _selfcheck_fixture(workdir):
    """临时交易库：一根「过去」K 线序列 + 两根**未来**K 线 + 一期未来财报 + 一条未来快照。"""
    from trading_core import store as core_store
    path = Path(workdir) / "trading.sqlite"
    conn = core_store.connect(path)  # 建表（临时目录，不碰真实库）
    bars = [{"t": f"2026-09-{day:02d}", "o": 1.0, "h": 1.0, "l": 1.0, "c": 10.0 + day, "v": 1.0}
            for day in (10, 11, 12)]
    bars += [{"t": "2026-09-13", "o": 1.0, "h": 1.0, "l": 1.0, "c": 999.0, "v": 1.0},
             {"t": "2026-09-14", "o": 1.0, "h": 1.0, "l": 1.0, "c": 999.0, "v": 1.0}]
    core_store.upsert_bars(conn, "X.TEST", "1d", bars, "selfcheck")
    core_store.upsert_fundamentals(conn, "X.TEST", [
        {"field": "revenue", "period_end": "2026-06-30", "announced_at": "2026-08-28",
         "value": 1000.0, "source": "selfcheck"},
        # 两道闸门的**独立性**证据：这一行公告日可见（2026-09-11 ≤ as_of），但报告期在
        # as_of 之后（2026-09-30 > 2026-09-12）→ 必须被 period_ceiling 单独挡掉。
        {"field": "revenue", "period_end": "2026-09-30", "announced_at": "2026-09-11",
         "value": 8888.0, "source": "selfcheck"},
        {"field": "revenue", "period_end": "2026-12-31", "announced_at": "2026-09-13",
         "value": 9999.0, "source": "selfcheck"},
    ], "selfcheck")
    conn.close()
    return path


def selfcheck(workdir=None, *, keep=False):
    """只读自检：证明「幂等 / 无未来 / 键隔离 / 缺失即 null」。

    返回 ``{"ok": bool, "checks": [{"name", "ok", "detail"}], …}``；``ok=False`` 表示
    PIT 纪律被破坏（测试与 CI 可以据此变红）。不触达网络、不碰真实交易库（缺省在临时
    目录建库，``keep=False`` 时用完即删）。
    """
    import shutil
    import tempfile

    from server import caches as _caches
    holder = None if workdir else tempfile.mkdtemp(prefix="pit-selfcheck-")
    workdir = workdir or holder
    checks = []

    def record(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    try:
        path = _selfcheck_fixture(workdir)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        old_cache = _caches._CACHE  # noqa: SLF001 —— 自检要隔离磁盘目录，用完还原
        try:
            _caches.configure(home=workdir)
            first = read_bars("2026-09-12", AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
            second = read_bars("2026-09-12", AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
            third = read_bars("2026-09-12", AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
            core_fields = ("as_of", "mode", "source", "window", "rows_used", "bars",
                           "closes", "derived", "rejected", "missing")
            same = all(first.get(key) == second.get(key) for key in core_fields)
            record("幂等：同一 as_of 连读两次逐字段相同", same,
                   {"as_of": first.get("as_of"), "rows_used": first.get("rows_used"),
                    "window": first.get("window"), "cached_flags":
                        [first.get("cached"), second.get("cached"), third.get("cached")]})
            record("缓存命中：同一 as_of 第二次读数走 TTL 缓存", second.get("cached") is True,
                   second.get("cache_policy"))
            future = [bar for bar in (first.get("bars") or []) if str(bar.get("t")) > "2026-09-12"]
            record("PIT 严格：as_of=t 的窗口内没有 > t 的记录", not future,
                   {"window": first.get("window"), "rejected": first.get("rejected"),
                    "future_rows": future})
            record("PIT 证据：两根未来 K 线被闸门计数挡掉",
                   (first.get("rejected") or {}).get("future") == 2, first.get("rejected"))
            inclusive = read_bars("2026-09-12", AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
            exclusive = read_bars("2026-09-12", AS_OF_EXCLUSIVE, symbol="X.TEST", conn=conn)
            record("模式显式：inclusive 含当日、exclusive 不含当日（差一根）",
                   inclusive["rows_used"] == 3 and exclusive["rows_used"] == 2,
                   {"inclusive": inclusive["rows_used"], "exclusive": exclusive["rows_used"],
                    "inclusive_window": inclusive["window"], "exclusive_window": exclusive["window"]})
            other_day = read_bars("2026-09-11", AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
            record("键隔离：不同 as_of 不串味",
                   other_day["rows_used"] != inclusive["rows_used"]
                   and other_day["as_of"] == "2026-09-11",
                   {"t_12": inclusive["rows_used"], "t_11": other_day["rows_used"]})
            record("键隔离：不同标的不是同一条缓存", read_bars(
                "2026-09-12", AS_OF_INCLUSIVE, symbol="Y.TEST", conn=conn
            )["rows_used"] == 0, "Y.TEST 无记录 → rows_used=0（与 X.TEST 不同键）")
            empty = read_bars("2026-09-12", AS_OF_INCLUSIVE, symbol="Y.TEST", conn=conn)
            record("缺失即 null + 原因（不填 0）",
                   empty["bars"] is None and empty["closes"] is None
                   and isinstance(empty.get("missing"), dict) and empty["missing"].get("reason"),
                   {"bars": empty["bars"], "missing": empty["missing"]})
            funds = read_fundamentals("2026-09-12", AS_OF_INCLUSIVE, symbol="X.TEST", conn=conn)
            funds_rejected = funds.get("rejected") or {}
            record("基本面 PIT：未来公告不可见、未来报告期被独立闸门挡掉",
                   funds["rows_used"] == 1
                   and funds_rejected.get("future") == 1
                   and funds_rejected.get("period_ceiling") == 1
                   and all(str(row["announced_at"]) <= "2026-09-12" for row in funds["rows"] or []),
                   {"rows_used": funds["rows_used"],
                    "rejected": funds_rejected,
                    "periods": [row["period_end"] for row in funds["rows"] or []]})
            prefix_same = pit_prefix(list(range(10)), 5, lag=LAG_SAME_DAY)
            prefix_prev = pit_prefix(list(range(10)), 5, lag=LAG_PREV_DAY)
            record("边界原语：≤t / ≤t-1 的可见前缀",
                   prefix_same == [0, 1, 2, 3, 4, 5] and prefix_prev == [0, 1, 2, 3, 4],
                   {"same_day": prefix_same, "prev_day": prefix_prev})
        finally:
            _caches._CACHE = old_cache  # noqa: SLF001
            conn.close()
        return {"ok": all(item["ok"] for item in checks), "workdir": str(workdir),
                "checks": checks}
    finally:
        if holder and not keep:
            shutil.rmtree(holder, ignore_errors=True)


def _main():  # pragma: no cover —— 脚本入口（CI/人工核验用）
    import json
    report = selfcheck()
    for item in report["checks"]:
        print(f"[{'PASS' if item['ok'] else 'FAIL'}] {item['name']}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    sys.exit(_main())
