"""富途接口限流治理（**全站唯一实现**）：全局限速 + 单飞 + 退避重试 + 冷却 + 统一错误码。

背景（真实问题，2026-09-20 实测）
--------------------------------
连续密集真机请求会让富途返回 **``-12006``（请求过于频繁）**，表现为 HTTP 403；另有
``-12009`` / HTTP 439。治理前这些错误被原样透传给上层——例如 ``market/no-universe`` 的
``error.detail`` 里写着「positions 取数失败…[errcode=439]」，**用户会误读成「没有数据」**。

因此本模块做四件事，且**只有这一个实现**（v3_market / v3_universe / v3_quality /
v3_industry / v3_analytics / v3_sources 一律不自己造退避/单飞）：

1. **全局限速**——令牌桶（``rate_per_sec`` 默认 3，``burst`` 默认 3），超出即等待
   （等待毫秒累计进 ``throttle_wait_ms``）；并发上限 ``max_concurrency``（默认 2）。
2. **单飞（single-flight）**——同一 ``key``（工具名 + 稳定序列化后的参数）的并发调用
   **共享同一次真实请求**，其余等待复用结果（计 ``coalesced``）；成败都共享
   （同一 key 的并发调用本就该看到同一事实，避免 N 倍压力打上游）。
3. **退避重试**——识别到限流错误 → 指数退避 + 抖动（``base_backoff_ms`` 起、
   ``max_backoff_ms`` 封顶），最多重试 ``QUANT_FUTU_RETRY``（默认 2）次。
4. **冷却**——连续 ``cooldown_after``（默认 3）次限流错误后进入 ``cooldown_ms``
   （默认 5000）冷却，期间**新调用直接返回限流错误、不发请求**，
   ``retry_after_ms`` 为剩余冷却毫秒；冷却结束自动恢复（下一次真实成功清零连续计数）。

统一错误信封（**关键：解决「被误读成没有数据」**）::

    {"ok": false, "error": {
        "code": "futu/rate-limited",
        "message": "富途接口限流（上游 -12006），已退避重试 2 次仍失败，请稍后重试",
        "detail": {"upstream": "-12006", "retry_after_ms": 5200, "retries": 2,
                   "cooldown": true, "reason": "<上游原文，截断 300>"},
        "retry_after_ms": 5200}}

**非限流错误原样透传**（不重写业务错误、不改既有 ``trading/*`` 信封）。

错误码映射（``is_rate_limit_error`` / ``upstream_code_of``）
-----------------------------------------------------------
===================  ==========================================  ==============
上游写法              识别条件                                     ``upstream``
===================  ==========================================  ==============
``-12006`` / 12006    数字边界匹配（含 ``HTTP 403：{"code":-12006}``）  ``-12006``
``-12009`` / 12009    同上                                        ``-12009``
``439``               ``errcode=439`` / ``code: 439`` / ``[439]``   ``439``
``HTTP 403``          **只在同时含 12006 时**才算（裸 403 不算）        ``-12006``
``futu/rate-limited`` 内层调用已被本模块限流（外层再看到同一事实）      ``futu/rate-limited``
文字特征               请求过于频繁 / 频率限制 / 超出频率 / rate limit  ``text``
===================  ==========================================  ==============

配置项（环境变量，全部带默认；``QUANT_FUTU_RATELIMIT=0`` 可整体关闭）
-------------------------------------------------------------------
=================================  ==================  ==========================
环境变量                             默认                 含义
=================================  ==================  ==========================
``QUANT_FUTU_RATELIMIT``             ``1``（开）           ``0``/``false``/``off``/``no`` → 整体关闭（直通）
``QUANT_FUTU_RATE_PER_SEC``         ``3``                令牌桶速率（个/秒）
``QUANT_FUTU_BURST``                ``3``                令牌桶容量（突发额度）
``QUANT_FUTU_MAX_CONCURRENCY``      ``2``                同时在飞的真实请求上限
``QUANT_FUTU_BACKOFF_BASE_MS``      ``400``              首次退避基数（毫秒，指数增长）
``QUANT_FUTU_BACKOFF_MAX_MS``       ``8000``             单次退避上限（毫秒）
``QUANT_FUTU_COOLDOWN_MS``          ``5000``             冷却时长（毫秒）
``QUANT_FUTU_COOLDOWN_AFTER``       ``3``                连续 N 次限流后进入冷却
``QUANT_FUTU_RETRY``                ``2``                限流错误后的最大重试次数
``QUANT_FUTU_EXTRA_TOOLS``          （空）               额外声明为富途只读的工具名（逗号分隔）
=================================  ==================  ==========================

接入方式（app.py 接线处，只读工具才过限流器）
--------------------------------------------
``v3_run`` / ``_wb_http`` 在 app.py 里包一层：``name``（或 endpoint）命中
``is_futu_tool()`` 的只读集合 → ``limiter.run_sync(stable_key(name, payload), call)``；
未命中 → 原样直通（本地台账类工具不被无谓限速）。写/交易端点**永不在集合内**
（``switch-mode`` / ``plan-execute`` / ``trade_*`` / ``modify_user_security`` 等）。

异步/同步双入口
---------------
``run(key, call)`` 是**协程**（契约要求的异步面）；``run_sync(key, call)`` 是**同步**面，
供 app.py 的同步 ``v3_run``（跑在 ``asyncio.to_thread`` 里）使用。两者共用同一份线程安全
状态（令牌桶/并发槽/单飞表/冷却），因此同一个进程里两条路径互相限流、互相单飞。
**嵌套口径**：同一线程/任务内的二次调用（外层受管调用内部又走一次 ``v3_run``）直接透传，
避免「外层还占着并发槽、内层又在等槽」的自锁；并发线程/任务之间仍严格互相限流。
等待用注入的 ``sleep``（默认 ``asyncio.sleep``；同步面用 ``blocking_sleep``，默认
``time.sleep``；注入了同步假 sleep 时自动沿用它），时间用注入的 ``clock``
（默认 ``time.monotonic``）——单测据此注入假时钟/假 sleep 做确定性断言。

测试：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_ratelimit -v``
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextvars
import inspect
import json
import os
import random
import re
import threading
import time

__all__ = [
    "DEFAULTS", "ENV_KEYS", "ENABLE_ENV", "EXTRA_TOOLS_ENV", "RATE_LIMIT_CODE",
    "FUTU_TOOL_NAMES", "FutuLimiter", "blocking_cooldown_ms", "futu_run",
    "get_limiter", "is_futu_endpoint", "is_futu_tool", "is_rate_limit_error",
    "limiter_from_env", "metrics_view", "rate_limited_envelope", "reset_limiter",
    "stable_key", "stats", "upstream_code_of", "wrap_futu_call",
]

#: 统一限流错误码（前端已就绪：``error.detail`` 会显示为「富途接口限流… · 真实原因：{…}」）。
RATE_LIMIT_CODE = "futu/rate-limited"

#: 配置默认值（与 ``docs/e2e-and-data-gaps.md`` 的表格逐值一致）。
DEFAULTS = {
    "rate_per_sec": 3.0,
    "burst": 3,
    "max_concurrency": 2,
    "base_backoff_ms": 400,
    "max_backoff_ms": 8000,
    "cooldown_ms": 5000,
    "cooldown_after": 3,
    "retries": 2,
}

#: 配置项 → 环境变量名。
ENV_KEYS = {
    "rate_per_sec": "QUANT_FUTU_RATE_PER_SEC",
    "burst": "QUANT_FUTU_BURST",
    "max_concurrency": "QUANT_FUTU_MAX_CONCURRENCY",
    "base_backoff_ms": "QUANT_FUTU_BACKOFF_BASE_MS",
    "max_backoff_ms": "QUANT_FUTU_BACKOFF_MAX_MS",
    "cooldown_ms": "QUANT_FUTU_COOLDOWN_MS",
    "cooldown_after": "QUANT_FUTU_COOLDOWN_AFTER",
    "retries": "QUANT_FUTU_RETRY",
}
ENABLE_ENV = "QUANT_FUTU_RATELIMIT"
EXTRA_TOOLS_ENV = "QUANT_FUTU_EXTRA_TOOLS"

# ---------------------------------------------------------------------------
# 富途只读工具/端点集合（**只读**：写与交易端点永不在内）
# ---------------------------------------------------------------------------
# 依据：store_access 的 FUTU_ENDPOINTS / WP8_MARKET_ENDPOINTS / WP8_TRADE_ENDPOINTS /
# WP12_ENDPOINTS（除去写类 modify_user_security）+ WP7 账户只读 + 实测触达富途的
# 复合端点（``series`` 实测 ``source=futu/quote_history_kline``；``snapshot`` 聚合持仓与
# 行情；``sources`` 逐链探测含富途；因子/相关性/敏感性/IC 都建立在 series 之上）。
# 不在集合内的工具（本地台账 plan/equity/trades/audit/rules/schedule/… 与全部写端点）
# 直通，不被无谓限速。
FUTU_TOOL_NAMES = frozenset({
    # WP8 富途直通（8）
    "rt_quote", "rt_order_book", "capital_flow", "capital_flow_history",
    "capital_distribution", "option_expiration", "option_chain", "option_screen",
    # WP8 任务 2：OpenAPI 行情（9）
    "market_snapshot", "cur_kline", "rt_data", "rt_ticker", "info_basicinfo",
    "info_trading_days", "info_search", "info_market_state", "quote_history_kline_v2",
    # WP12 数据面（只读 15；有意的写类 modify_user_security 不在内）
    "economic_calendar_hot", "economic_calendar_search", "info_owner_plate", "info_rehab",
    "plate_list", "plate_stock", "stock_screen", "warrant_screen", "ipo_list",
    "short_daily_volume", "short_interest", "watchlist_list", "watchlist_groups",
    "f10_detail", "derivative_detail",
    # WP8 任务 3 交易只读（6）+ WP7 账户只读（3）
    "trade_max_qty", "orders_open", "orders_history", "orders_detail",
    "deals_today", "deals_history",
    "account_positions", "account_orders", "account_funds",
    # 实测触达富途的复合端点
    "series", "positions", "snapshot", "sources", "factors", "factors-history",
    "ic", "correlation", "sensitivity", "events", "instrument", "quality",
})

# ---------------------------------------------------------------------------
# 限流错误识别
# ---------------------------------------------------------------------------
_CODE_12006 = re.compile(r"(?<!\d)-?12006(?!\d)")
_CODE_12009 = re.compile(r"(?<!\d)-?12009(?!\d)")
_CODE_439 = re.compile(r"(?:errcode|ret_?code|code|status|http(?:\s*status)?)\W{0,6}439\b"
                       r"|\[\s*439\s*\]", re.IGNORECASE)
#: 本模块自己的信封（内层已限流）——外层再看到时同样是限流事实，按此码记账更诚实。
_CODE_OWN = re.compile(r"futu/rate-limited")
_PHRASES = ("请求过于频繁", "请求频繁", "过于频繁", "频率限制", "超出频率", "访问频率",
            "限流", "rate limit", "ratelimit", "rate-limit", "too many requests",
            "too frequent", "frequent requests", "request frequency")


def _text_of(error):
    """错误对象 → 用于特征扫描的文本（异常/信封/字符串/嵌套 details 都覆盖）。"""
    if error is None:
        return ""
    if isinstance(error, str):
        return error
    if isinstance(error, BaseException):
        parts = [f"{type(error).__name__}: {error}"]
        for attr in ("errcode", "code", "details", "detail", "args"):
            value = getattr(error, attr, None)
            if value is None or attr == "args":
                continue
            parts.append(_text_of(value))
        return " ".join(parts)
    if isinstance(error, dict):
        try:
            return json.dumps(error, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 —— 不可序列化时退回 repr
            return str(error)
    if isinstance(error, (list, tuple, set)):
        return " ".join(_text_of(item) for item in error)
    return str(error)


def is_rate_limit_error(error):
    """``error`` 是否为富途限流错误（``-12006`` / ``-12009`` / ``errcode=439`` / 文字特征）。

    接受异常对象、错误信封 dict、字符串；``None`` / 无关错误 → ``False``。
    裸 ``HTTP 403`` **不算**（403 只在同时含 ``12006`` 时才由 12006 分支命中），
    避免把权限类 403 误判成限流。

    **``ok=true`` 的信封一律不算**（实测教训）：工具自己的 ``value`` 里可能带着**子项**的
    限流说明（例如 ``deals_today`` 某个账户分组取数失败的原因里含「请求过于频繁」），
    但那不代表这次调用失败——把它改写成限流信封会**丢掉已经取到的数据**。
    """
    if isinstance(error, dict) and error.get("ok") is True:
        return False
    text = _text_of(error)
    if not text:
        return False
    if _CODE_12006.search(text) or _CODE_12009.search(text) or _CODE_439.search(text):
        return True
    lowered = text.lower()
    return any(phrase in lowered for phrase in _PHRASES)


def upstream_code_of(error):
    """限流错误 → 上游错误码标识。

    ``-12006`` / ``-12009`` / ``439`` / ``futu/rate-limited``（内层已被本模块限流）/
    ``text``（只有文字特征）/ ``unknown``。
    """
    text = _text_of(error)
    if _CODE_12006.search(text):
        return "-12006"
    if _CODE_12009.search(text):
        return "-12009"
    if _CODE_439.search(text):
        return "439"
    if _CODE_OWN.search(text):
        return "futu/rate-limited"
    lowered = text.lower()
    if any(phrase in lowered for phrase in _PHRASES):
        return "text"
    return "unknown"


def _error_message(error):
    """错误原文（写进信封 ``detail.reason``，让「真实原因」可见，不被误读成没有数据）。"""
    if error is None:
        return ""
    if isinstance(error, dict):
        payload = error.get("error") if isinstance(error.get("error"), dict) else error
        message = payload.get("message") or payload.get("detail") or payload.get("reason")
        return str(message) if message else ""
    if isinstance(error, BaseException):
        return str(error)
    return str(error)


def rate_limited_envelope(error, retry_after_ms, *, retries=0, cooldown=False, upstream=None):
    """统一限流错误信封（见模块 docstring 的 JSON 样例）。

    ``retry_after_ms``：建议等待毫秒（冷却中 = 剩余冷却；否则 = 建议退避）。
    ``retries``：已经发生的退避重试次数；``cooldown``：当前是否处于冷却。
    """
    code = upstream or upstream_code_of(error)
    delay = max(0, int(retry_after_ms or 0))
    if cooldown and retries <= 0:
        message = f"富途接口限流冷却中（上游 {code}），请约 {delay / 1000:.1f}s 后重试"
    elif retries > 0:
        message = (f"富途接口限流（上游 {code}），已退避重试 {retries} 次仍失败，"
                   f"请稍后重试")
    else:
        message = f"富途接口限流（上游 {code}），请稍后重试"
    detail = {"upstream": code, "retry_after_ms": delay, "retries": int(retries),
              "cooldown": bool(cooldown)}
    # 「真实原因」必须看得到（这正是修「被误读成没有数据」的关键）：优先取 error.message，
    # 拿不到（例如触发文本藏在工具 value 的嵌套字段里）就退回收敛后的原文。
    reason = _error_message(error) or _text_of(error)
    if reason:
        detail["reason"] = reason[:300]
    return {"ok": False,
            "error": {"code": RATE_LIMIT_CODE, "message": message, "detail": detail,
                      "retry_after_ms": delay}}


# ---------------------------------------------------------------------------
# 稳定键（单飞复用口径：工具名 + 稳定序列化后的参数）
# ---------------------------------------------------------------------------
def stable_key(name, payload=None):
    """``工具名|稳定序列化参数``——同键并发调用共享同一次真实请求。"""
    body = payload if isinstance(payload, dict) else (payload or {})
    try:
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str,
                             separators=(",", ":"))
    except Exception:  # noqa: BLE001 —— 不可序列化参数退化为 repr（仍保持同参同键）
        encoded = repr(body)
    return f"{name}|{encoded}"


# ---------------------------------------------------------------------------
# 配置装载
# ---------------------------------------------------------------------------
def _env_number(key, *, cast, minimum=None, maximum=None):
    raw = os.environ.get(ENV_KEYS[key])
    default = DEFAULTS[key]
    if raw is None or str(raw).strip() == "":
        return cast(default)
    try:
        value = cast(float(str(raw).strip()))
    except (TypeError, ValueError):
        return cast(default)
    if minimum is not None and value < minimum:
        return cast(default)
    if maximum is not None and value > maximum:
        return cast(default)
    return cast(value)


def _env_enabled(default=True):
    raw = os.environ.get(ENABLE_ENV)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "disable", "disabled")


def limiter_from_env(**overrides):
    """按环境变量（+ 显式覆盖）建一个 limiter（``get_limiter`` 的构造器，测试可直接用）。"""
    params = {
        "rate_per_sec": _env_number("rate_per_sec", cast=float, minimum=1e-9),
        "burst": _env_number("burst", cast=int, minimum=1),
        "max_concurrency": _env_number("max_concurrency", cast=int, minimum=1),
        "base_backoff_ms": _env_number("base_backoff_ms", cast=int, minimum=0),
        "max_backoff_ms": _env_number("max_backoff_ms", cast=int, minimum=0),
        "cooldown_ms": _env_number("cooldown_ms", cast=int, minimum=0),
        "cooldown_after": _env_number("cooldown_after", cast=int, minimum=1),
        "retries": _env_number("retries", cast=int, minimum=0),
        "enabled": _env_enabled(),
    }
    params.update(overrides)
    return FutuLimiter(**params)


_LIMITER_LOCK = threading.Lock()
_LIMITER = None


def get_limiter():
    """进程内唯一 limiter（app.py 接线与 ``/api/v3/metrics`` 读同一份计数）。"""
    global _LIMITER
    with _LIMITER_LOCK:
        if _LIMITER is None:
            _LIMITER = limiter_from_env()
        return _LIMITER


def reset_limiter(limiter=None):
    """替换/清空进程内 limiter（测试用；生产只在启动时经 ``get_limiter`` 建一次）。"""
    global _LIMITER
    with _LIMITER_LOCK:
        _LIMITER = limiter
        return _LIMITER


def stats():
    """进程内 limiter 的计数快照（``/api/v3/metrics`` 与测试都读这里）。"""
    return get_limiter().stats()


def blocking_cooldown_ms():
    """进程内 limiter 的剩余冷却毫秒（0 = 未冷却）。"""
    return get_limiter().blocking_cooldown_ms()


def metrics_view(limiter=None):
    """``/api/v3/metrics`` 的 ``futu`` 字段（真实计数；既有字段一字不改，只加这一块）。"""
    target = limiter if limiter is not None else get_limiter()
    snapshot = target.stats()
    remaining = target.blocking_cooldown_ms()
    return {
        "enabled": bool(target.enabled),
        "calls": snapshot["calls"],
        "coalesced": snapshot["coalesced"],
        "retries": snapshot["retries"],
        "rateLimited": snapshot["rate_limited"],
        "throttleWaitMs": snapshot["throttle_wait_ms"],
        # cooldownUntil 用**墙钟毫秒**（人/前端可读）；未冷却为 0。
        "cooldownUntil": int(time.time() * 1000) + remaining if remaining > 0 else 0,
        "cooldownRemainingMs": remaining,
        "inFlight": snapshot["in_flight"],
        "queued": snapshot["queued"],
    }


# ---------------------------------------------------------------------------
# 富途只读工具判定（接入层用；写端点永不在此）
# ---------------------------------------------------------------------------
def futu_tool_names():
    """富途只读工具集合 = 内置集合 + ``QUANT_FUTU_EXTRA_TOOLS``（逗号分隔，运维扩展）。"""
    names = set(FUTU_TOOL_NAMES)
    raw = os.environ.get(EXTRA_TOOLS_ENV) or ""
    for item in raw.split(","):
        token = item.strip()
        if token:
            names.add(token)
    return frozenset(names)


def is_futu_tool(name):
    """``name``（工具名或端点名）是否属富途只读集合。"""
    return str(name or "") in futu_tool_names()


#: 端点名与工具名在本仓库里同源（``ToolDefinition.endpoint``），别名保留以便阅读。
is_futu_endpoint = is_futu_tool


# ---------------------------------------------------------------------------
# 单飞航班
# ---------------------------------------------------------------------------
class _Flight:
    """一次在飞的真实请求：``future`` 承载结果，``waiters`` 是复用者计数。"""

    __slots__ = ("future", "waiters")

    def __init__(self):
        self.future = concurrent.futures.Future()
        self.waiters = 0


#: 同一线程/任务内的嵌套深度（``ContextVar`` 在协程里按任务隔离、在线程里按线程隔离）：
#: 外层调用已过闸时，内层二次调用直接透传，避免与外层争并发槽造成自锁。
_DEPTH = contextvars.ContextVar("futu_limiter_depth", default=0)


class FutuLimiter:
    """富途接口限流器（限速 / 单飞 / 退避 / 冷却）——契约见任务书与模块 docstring。"""

    def __init__(self, *, rate_per_sec=3.0, burst=3, max_concurrency=2,
                 base_backoff_ms=400, max_backoff_ms=8000, cooldown_ms=5000,
                 cooldown_after=3, sleep=asyncio.sleep, clock=time.monotonic,
                 blocking_sleep=None, rand=random.random, retries=2, enabled=True):
        self.rate_per_sec = float(rate_per_sec)
        self.burst = float(max(int(burst), 1))
        self.max_concurrency = max(int(max_concurrency), 1)
        self.base_backoff_ms = max(int(base_backoff_ms), 0)
        self.max_backoff_ms = max(int(max_backoff_ms), 0)
        self.cooldown_ms = max(int(cooldown_ms), 0)
        self.cooldown_after = max(int(cooldown_after), 1)
        self.retries = max(int(retries), 0)
        self.enabled = bool(enabled)
        self._sleep = sleep or asyncio.sleep
        self._clock = clock
        self._rand = rand
        if blocking_sleep is not None:
            self._blocking = blocking_sleep
        elif self._sleep is asyncio.sleep or inspect.iscoroutinefunction(self._sleep):
            # 注入的是协程睡眠（含默认 asyncio.sleep）→ 同步面必须另有阻塞睡眠
            self._blocking = time.sleep
        else:
            # 注入的是同步假 sleep（单测）→ 同步面沿用它，保证假时钟一致
            self._blocking = self._sleep

        self._lock = threading.Lock()
        self._tokens = self.burst
        self._last_refill = None
        self._in_flight = 0
        self._slot_waiters = collections.deque()
        self._flights = {}
        self._cooldown_until = 0.0
        self._consecutive = 0
        self._last_upstream = None
        self._counters = {"calls": 0, "coalesced": 0, "retries": 0,
                          "rate_limited": 0, "throttle_wait_ms": 0.0}

    # ---- 计数 / 状态 -----------------------------------------------------
    def _bump(self, key, amount=1):
        with self._lock:
            self._counters[key] += amount

    def stats(self):
        """计数快照（契约字段齐全）::

            {calls, coalesced, retries, rate_limited, throttle_wait_ms,
             cooldown_until, in_flight, queued}
        """
        now = self._clock()
        with self._lock:
            counters = dict(self._counters)
            in_flight = self._in_flight
            queued = len(self._slot_waiters) + sum(f.waiters for f in self._flights.values())
            cooldown_until = self._cooldown_until if self._cooldown_until > now else 0.0
        return {"calls": counters["calls"],
                "coalesced": counters["coalesced"],
                "retries": counters["retries"],
                "rate_limited": counters["rate_limited"],
                "throttle_wait_ms": round(counters["throttle_wait_ms"], 3),
                "cooldown_until": cooldown_until,
                "in_flight": in_flight,
                "queued": queued}

    def blocking_cooldown_ms(self):
        """剩余冷却毫秒（0 = 未冷却）。冷却只由时间解除，调用它不会改状态。"""
        now = self._clock()
        with self._lock:
            if self._cooldown_until <= now:
                return 0
            remaining = self._cooldown_until - now
        return max(0, int(round(remaining * 1000)))

    def _cooldown_rejection(self):
        """冷却期内的直接拒绝信封（不发请求、不占令牌、不计并发）。"""
        remaining = self.blocking_cooldown_ms()
        return rate_limited_envelope(None, remaining or self.cooldown_ms, retries=0,
                                     cooldown=True, upstream=self._last_upstream)

    # ---- 令牌桶 / 并发槽 -----------------------------------------------
    def _throttle_wait(self):
        """令牌桶：返回需要等待的秒数（同时把令牌记成「未来债务」保证顺序公平）。"""
        rate = self.rate_per_sec
        if rate <= 0:
            return 0.0
        now = self._clock()
        with self._lock:
            if self._last_refill is None:
                self._last_refill = now
            elapsed = now - self._last_refill
            if elapsed > 0:
                self._tokens = min(self.burst, self._tokens + elapsed * rate)
                self._last_refill = now
            self._tokens -= 1.0
            if self._tokens >= 0:
                return 0.0
            return -self._tokens / rate

    def _acquire_slot(self):
        """并发槽：``None`` = 立刻拿到；否则返回 ``concurrent.futures.Future``（置位=拿到）。"""
        with self._lock:
            if self._in_flight < self.max_concurrency:
                self._in_flight += 1
                return None
            waiter = concurrent.futures.Future()
            self._slot_waiters.append(waiter)
            return waiter

    def _release_slot(self):
        """释放并发槽：有排队者就**直接转交**（``in_flight`` 不变），否则递减。"""
        waiter = None
        with self._lock:
            while self._slot_waiters:
                candidate = self._slot_waiters.popleft()
                if candidate.set_running_or_notify_cancel():
                    waiter = candidate  # 槽转交：in_flight 保持不变
                    break
            if waiter is None:
                self._in_flight = max(0, self._in_flight - 1)
        if waiter is not None:
            waiter.set_result(None)  # 槽已转交，持有者开始真实请求

    # ---- 单飞 -----------------------------------------------------------
    def _join_flight(self, key):
        """``(flight, joined)``；``joined=True`` 表示已有在飞请求，复用其结果。"""
        with self._lock:
            flight = self._flights.get(key)
            if flight is not None:
                flight.waiters += 1
                self._counters["coalesced"] += 1
                return flight, True
            flight = _Flight()
            self._flights[key] = flight
            return flight, False

    def _publish(self, key, flight, value=None, error=None):
        with self._lock:
            self._flights.pop(key, None)
            waiters = flight.waiters
            flight.waiters = 0
        if error is not None:
            flight.future.set_exception(error)
        else:
            flight.future.set_result(value)
        return waiters

    def _leave_flight(self, flight):
        with self._lock:
            flight.waiters = max(0, flight.waiters - 1)

    # ---- 限流记账 / 退避 ------------------------------------------------
    def _note_rate_limited(self, error):
        """记一次限流错误；连续达到阈值即进入冷却（冷却期不发请求）。"""
        upstream = upstream_code_of(error)
        now = self._clock()
        with self._lock:
            self._counters["rate_limited"] += 1
            self._consecutive += 1
            if upstream != "unknown":
                self._last_upstream = upstream
            if self._consecutive >= self.cooldown_after and self.cooldown_ms > 0:
                self._cooldown_until = now + self.cooldown_ms / 1000.0

    def _note_success(self):
        with self._lock:
            self._consecutive = 0

    def _backoff_seconds(self, attempt):
        """指数退避 + 抖动（半量到全量），``max_backoff_ms`` 封顶。"""
        base = self.base_backoff_ms / 1000.0
        if base <= 0:
            return 0.0
        cap = self.max_backoff_ms / 1000.0 if self.max_backoff_ms > 0 else base
        delay = min(base * (2 ** max(attempt, 0)), cap)
        if delay <= 0:
            return 0.0
        jitter = self._rand()
        try:
            jitter = float(jitter)
        except (TypeError, ValueError):
            jitter = 0.0
        return delay * (0.5 + 0.5 * max(0.0, min(1.0, jitter)))

    def _on_rate_limited(self, error, attempt):
        """限流错误 → ``(退避秒数, None)`` 或 ``(None, 最终信封)``。"""
        self._note_rate_limited(error)
        if attempt >= self.retries:
            remaining = self.blocking_cooldown_ms()
            delay_ms = remaining if remaining > 0 else int(self._backoff_seconds(attempt) * 1000)
            return None, rate_limited_envelope(error, delay_ms, retries=attempt,
                                               cooldown=remaining > 0,
                                               upstream=self._last_upstream)
        self._bump("retries")
        return self._backoff_seconds(attempt), None

    # ---- 等待原语 -------------------------------------------------------
    async def _async_wait(self, seconds):
        if seconds <= 0:
            return
        result = self._sleep(seconds)
        if inspect.isawaitable(result):
            await result

    def _blocking_wait(self, seconds):
        if seconds <= 0:
            return
        result = self._blocking(seconds)
        if inspect.isawaitable(result):
            raise TypeError("run_sync 需要阻塞睡眠（blocking_sleep）；注入的 sleep 是协程")
        return result

    async def _async_slot(self, waiter):
        if waiter.done():
            return waiter.result()
        return await asyncio.wrap_future(waiter)

    # ---- 同步入口（app.py 的 v3_run 走这里）-----------------------------
    def run_sync(self, key, call):
        """同步执行：限速 → 并发槽 → 真实请求 → 限流退避重试 → 统一信封/原样透传。

        ``call`` 是零参可调用对象（返回原始信封/值，可抛异常）。非限流异常原样抛出；
        限流错误在重试耗尽后**返回**统一信封（不抛）。

        **嵌套防自锁**：同一线程内的二次调用（理论上不该出现——外层调用内部又走一次
        ``v3_run``）直接透传，避免「外层的并发槽还没还，内层又在等槽」这种自锁。
        """
        if not self.enabled:
            return call()
        depth = _DEPTH.get()
        if depth > 0:
            return call()
        self._bump("calls")
        token = _DEPTH.set(depth + 1)
        try:
            return self._run_sync_gated(key, call)
        finally:
            _DEPTH.reset(token)

    def _run_sync_gated(self, key, call):
        if self.blocking_cooldown_ms() > 0:
            return self._cooldown_rejection()
        flight, joined = self._join_flight(key)
        if joined:
            try:
                return flight.future.result()
            finally:
                self._leave_flight(flight)
        try:
            value = self._run_sync_inner(call)
        except BaseException as error:  # noqa: BLE001 —— 成败都共享给同键复用者
            self._publish(key, flight, error=error)
            raise
        self._publish(key, flight, value=value)
        return value

    def _run_sync_inner(self, call):
        attempt = 0
        while True:
            wait = self._throttle_wait()
            if wait > 0:
                self._bump("throttle_wait_ms", wait * 1000.0)
                self._blocking_wait(wait)
            waiter = self._acquire_slot()
            if waiter is not None:
                waiter.result()  # 阻塞等槽（槽由释放者直接转交）
            try:
                value = call()
            except Exception as error:  # noqa: BLE001 —— 非限流异常原样透传
                self._release_slot()  # 先还槽再退避：退避期间不占并发额度
                if not is_rate_limit_error(error):
                    raise
                delay, final = self._on_rate_limited(error, attempt)
                if final is not None:
                    return final
                self._blocking_wait(delay)
                attempt += 1
                continue
            except BaseException:
                self._release_slot()
                raise
            self._release_slot()
            if is_rate_limit_error(value):
                delay, final = self._on_rate_limited(value, attempt)
                if final is not None:
                    return final
                self._blocking_wait(delay)
                attempt += 1
                continue
            self._note_success()
            return value

    # ---- 异步入口（契约面）---------------------------------------------
    async def run(self, key, call):
        """``run_sync`` 的异步等价（``sleep`` 用协程睡眠；``call`` 可为协程函数）。

        嵌套口径同 ``run_sync``：同一任务内的二次调用直接透传（``ContextVar`` 在协程里
        按任务隔离，因此并发任务之间仍互相限流/单飞）。
        """
        if not self.enabled:
            return await self._invoke(call)
        depth = _DEPTH.get()
        if depth > 0:
            return await self._invoke(call)
        self._bump("calls")
        token = _DEPTH.set(depth + 1)
        try:
            return await self._run_async_gated(key, call)
        finally:
            _DEPTH.reset(token)

    @staticmethod
    async def _invoke(call):
        value = call()
        if inspect.isawaitable(value):
            value = await value
        return value

    async def _run_async_gated(self, key, call):
        if self.blocking_cooldown_ms() > 0:
            return self._cooldown_rejection()
        flight, joined = self._join_flight(key)
        if joined:
            try:
                return await self._async_slot(flight.future)
            finally:
                self._leave_flight(flight)
        try:
            value = await self._run_async_inner(call)
        except BaseException as error:  # noqa: BLE001
            self._publish(key, flight, error=error)
            raise
        self._publish(key, flight, value=value)
        return value

    async def _run_async_inner(self, call):
        attempt = 0
        while True:
            wait = self._throttle_wait()
            if wait > 0:
                self._bump("throttle_wait_ms", wait * 1000.0)
                await self._async_wait(wait)
            waiter = self._acquire_slot()
            if waiter is not None:
                await self._async_slot(waiter)
            try:
                value = call()
                if inspect.isawaitable(value):
                    value = await value
            except Exception as error:  # noqa: BLE001
                self._release_slot()
                if not is_rate_limit_error(error):
                    raise
                delay, final = self._on_rate_limited(error, attempt)
                if final is not None:
                    return final
                await self._async_wait(delay)
                attempt += 1
                continue
            except BaseException:
                self._release_slot()
                raise
            self._release_slot()
            if is_rate_limit_error(value):
                delay, final = self._on_rate_limited(value, attempt)
                if final is not None:
                    return final
                await self._async_wait(delay)
                attempt += 1
                continue
            self._note_success()
            return value


# ---------------------------------------------------------------------------
# 接入层包装
# ---------------------------------------------------------------------------
def futu_run(limiter, name, payload, call):
    """同步接入点：``name`` 命中富途只读集合 → 过 limiter；否则**原样直通**。

    ``call()`` 是真正发请求的零参可调用对象（返回原始信封/值）。
    """
    if limiter is None or not getattr(limiter, "enabled", False):
        return call()
    if not is_futu_tool(name):
        return call()
    return limiter.run_sync(stable_key(name, payload), call)


def wrap_futu_call(limiter, name):
    """``wrap_futu_call(limiter, name) -> callable``：返回 ``wrapped(payload, call)``。

    接入层（app.py 的 ``v3_run`` / ``_wb_http``）用它把「工具名 + 参数」稳定键与同步调用
    包进 limiter：``wrapped(payload, lambda: tool.run(payload))``。
    """
    def wrapped(payload, call):
        return futu_run(limiter, name, payload, call)
    return wrapped
