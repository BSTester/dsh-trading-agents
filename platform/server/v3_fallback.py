"""通用「主源 → 降级源」链（``run_chain``）+ ``GET /api/v3/sources/status``。

存在的理由只有一条：**每个响应都要能回答「这个数从哪来、什么时候的、降级到哪一级」**。
把「按顺序试、把每一级的真实结果记下来、全失败就如实报错」收敛成一份实现，避免每个端点
各写一遍 try/except 之后各自漂移（有的吞异常当成功、有的把空列表当有数据）。

纪律（与仓库「数据诚实」一致）:
  * ``run_chain`` **不抛异常**也**不编造成功**：任一级抛出的异常原文进 ``attempts``，全部
    失败返回 ``(None, None, attempts)``，由调用方决定错误码与文案；
  * 空结果视同失败（``None`` / 空列表 / 空字符串 / 无 ``ok`` 键但为空的容器）——「取到 0 条」
    不能冒充「该源可用」；
  * 每次尝试都记 ``ms``（真实耗时），因此降级链不是猜测，而是可核对的时间线。

``GET /api/v3/sources/status``：对每条链做**一次轻量只读探测**（K 线、快照、1 期财务、1 条
资讯、1 个标的的板块归属、10 条委托），逐级尝试主源与降级源，把真实可用性与最近命中来源
写回响应。探测失败也如实记 ``last_ok:false`` + ``error``；**绝不探测任何写端点**。

放在本模块而不是 ``v3_sources.py``：``v3_sources`` 的路由清单已被既有单测逐条锁定
（``test_registers_all_five_routes_and_exposes_state``），而「链」本身是本模块的职责。
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, timezone

__all__ = [
    "AKSHARE_RETRY_ATTEMPTS",
    "AKSHARE_RETRY_BASE_MS",
    "AKSHARE_RETRY_MAX_MS",
    "CHAIN_SPECS",
    "RETRYABLE_ERROR_NAMES",
    "attempts_chain",
    "describe_attempts",
    "failure_of",
    "is_retryable_akshare",
    "probe_chains",
    "register",
    "retry_akshare",
    "run_chain",
]

#: 探测预算：单条链的总时长上限（秒）。主源失败才轮到降级源，超预算的剩余源记 skipped。
PROBE_CHAIN_TIMEOUT = 45.0
#: 探测用标的（A 股权限缺口最小、三市场都存在的常用标的）
PROBE_TICKER = "SH.600000"
PROBE_US_TICKER = "AAPL"
PROBE_HK_TICKER = "HK.00700"
#: 板块反查上限（``plate_stock`` 逐板块扫描，仅当 info_owner_plate 整体不可用时才走）
PROBE_PLATE_CODE = "SH.LIST0949"

#: 8 条链的静态描述（``primary``/``fallback`` 是**契约字段**，前端按它渲染「主源/降级源」）。
#: 探测函数在下面按 key 注册；静态描述与探测实现分开，是为了让描述可被单测逐条核对。
CHAIN_SPECS = (
    {
        "key": "kline",
        "label": "K 线/历史行情",
        "primary": "futu/quote_history_kline",
        "fallback": "akshare/stock_zh_a_hist",
    },
    {
        "key": "snapshot",
        "label": "实时行情快照",
        "primary": "futu/market_snapshot",
        "fallback": "akshare/stock_zh_a_spot_em",
    },
    {
        "key": "financials_cn",
        "label": "A 股/港股财务报表",
        "primary": "futu/f10_detail/statements",
        "fallback": "akshare/stock_financial_abstract",
    },
    {
        "key": "financials_us",
        "label": "美股财务报表",
        "primary": "sec/companyconcept(us-gaap XBRL)",
        "fallback": "openbb/equity.fundamental",
    },
    {
        "key": "news",
        "label": "个股资讯",
        "primary": "futu/info_search",
        "fallback": "akshare/stock_news_em",
    },
    {
        "key": "industry",
        "label": "行业/板块映射",
        "primary": "futu/info_owner_plate",
        "fallback": "futu/plate_stock",
    },
    {
        "key": "quality",
        "label": "成交质量（委托/成交回报）",
        "primary": "futu/orders_history",
        # 成交类数据**没有**开源替代：降级源留空，前端显示「无降级源」。
        "fallback": "",
    },
    {
        "key": "spot",
        "label": "A 股全市场快照",
        "primary": "futu/market_snapshot",
        "fallback": "akshare/stock_zh_a_spot_em",
    },
)

SPEC_BY_KEY = {spec["key"]: spec for spec in CHAIN_SPECS}

NOTE = "所有降级都带 source 标注；两源都失败时如实报错（不返回占位数据）"


# ── 通用工具 ────────────────────────────────────────────────────────────────────


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _error_text(error):
    """异常 → 一句带类型名的真实文本（不吞、不改写）。"""
    message = str(error).strip() or repr(error)
    return f"{type(error).__name__}: {message}"


def _failure(code, message):
    return {"code": str(code), "message": str(message)[:400]}


def failure_of(result):
    """判定一次尝试的结果是否算失败；失败返回 ``error`` 字典，成功返回 ``None``。

    规则（**保守**：宁可把可疑结果当失败去试下一级，也不把空壳当成功）:
      * ``None`` → ``chain/empty``；
      * 有 ``ok`` 键的映射 → ``ok`` 为假即失败（带上它自己的 ``error``，保留上游错误码）；
      * 空列表/空元组/空集合/空字符串 → ``chain/empty``；
      * 其余（非空容器、标量、无 ``ok`` 键的非空映射）→ 成功。
    """
    if result is None:
        return _failure("chain/empty", "该源返回 None（视为失败，不当成功）")
    if isinstance(result, dict):
        if "ok" in result:
            if result.get("ok"):
                return None
            error = result.get("error")
            if isinstance(error, dict):
                return {
                    "code": str(error.get("code") or "chain/upstream-error"),
                    "message": str(error.get("message") or "上游返回 ok=false（无 message）")[:400],
                }
            if error:
                return _failure("chain/upstream-error", str(error))
            return _failure("chain/upstream-error", "上游返回 ok=false（无 error 字段）")
        return None
    if isinstance(result, (list, tuple, set, frozenset, str, bytes)):
        if len(result) == 0:
            return _failure("chain/empty", "该源返回空结果（视为失败）")
        return None
    return None


def _normalize_entry(entry):
    """``(name, callable)`` / ``(name, callable, kwargs)`` → ``(name, callable, kwargs)``。

    形态非法时抛 ``TypeError``（这是**调用方的编程错误**，不是上游失败，不能进 attempts 被
    当成一次失败的取数——那会让「链里从未有过这个源」看起来像「这个源挂了」）。
    """
    if not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3):
        raise TypeError(f"链元素需为 (name, callable[, kwargs])，收到 {entry!r}")
    name, func = entry[0], entry[1]
    kwargs = entry[2] if len(entry) == 3 else {}
    if not callable(func):
        raise TypeError(f"{name!r} 的第二个元素必须是可调用对象，收到 {type(func).__name__}")
    if kwargs is None:
        kwargs = {}
    if not isinstance(kwargs, dict):
        raise TypeError(f"{name!r} 的第三个元素必须是 dict，收到 {type(kwargs).__name__}")
    return str(name), func, dict(kwargs)


def run_chain(chain, *, timeout=None, clock=time.monotonic):
    """按顺序尝试 ``(name, callable[, kwargs])``，返回 ``(value, used_source, attempts)``。

    * 命中即返回 ``(该次返回值, 源名, attempts)``——不再尝试后面的源；
    * 全部失败返回 ``(None, None, attempts)``；
    * ``attempts`` 每项 ``{source, ok, ms, error?[, skipped]}``，可直接作为响应的 ``chain``
      字段（既回答了「降级到哪一级」，也带真实耗时）；
    * ``timeout`` 是**整条链的墙钟预算**：预算耗尽后剩余源记 ``skipped=True`` 且
      ``error.code='chain/timeout'``——不为赶时间把没试过的源写成失败，也不假装成功。
    """
    attempts = []
    start = clock()
    for entry in chain or ():
        name, func, kwargs = _normalize_entry(entry)
        if timeout is not None and (clock() - start) > float(timeout):
            attempts.append(
                {
                    "source": name,
                    "ok": False,
                    "ms": 0,
                    "skipped": True,
                    "error": _failure("chain/timeout", f"链预算 {timeout}s 已耗尽，未尝试 {name}"),
                }
            )
            continue
        begin = clock()
        try:
            value = func(**kwargs)
        except Exception as error:  # noqa: BLE001 —— 上游/解析异常都要留痕，不能吞
            attempts.append(
                {
                    "source": name,
                    "ok": False,
                    "ms": int((clock() - begin) * 1000),
                    "error": _failure("chain/exception", _error_text(error)),
                }
            )
            continue
        elapsed = int((clock() - begin) * 1000)
        failure = failure_of(value)
        if failure is not None:
            attempts.append({"source": name, "ok": False, "ms": elapsed, "error": failure})
            continue
        attempts.append({"source": name, "ok": True, "ms": elapsed})
        return value, name, attempts
    return None, None, attempts


def attempts_chain(attempts):
    """``attempts`` → 响应里的 ``chain`` 字段（保持每项 ``{source, ok, ms, error?}`` 原样）。"""
    return [dict(item) for item in (attempts or [])]


def describe_attempts(attempts):
    """``attempts`` → 一行人类可读的降级时间线（用于错误 message，不隐藏任何一级）。"""
    parts = []
    for item in attempts or []:
        source = item.get("source")
        if item.get("ok"):
            parts.append(f"{source}=ok({item.get('ms')}ms)")
            continue
        error = item.get("error") or {}
        code = error.get("code") or "error"
        message = str(error.get("message") or "")[:160]
        skipped = "(跳过)" if item.get("skipped") else ""
        parts.append(f"{source}{skipped}={code}: {message}")
    return " → ".join(parts) if parts else "（空链，未尝试任何源）"


def last_error(attempts):
    """全链失败时的**真实**错误：取最后一个尝试失败的错误（保留上游错误码与原文）。"""
    for item in reversed(list(attempts or [])):
        error = item.get("error")
        if isinstance(error, dict) and error:
            return dict(error)
    return {"code": "chain/all-failed", "message": "降级链全部失败（无 error 明细）"}


# ── AKShare 专用重试策略 ────────────────────────────────────────────────────────
# 为什么单独一份：富途那条腿已经由 ``v3_ratelimit`` 统一治理（全局限速 + 单飞 + 退避 +
# 冷却，见 app.py 的 v3_run 接线）；AKShare 是**免密钥的开源腿**，没有 limiter，
# 而它的上游（东财/新浪）实测会直接 ``RemoteDisconnected`` 断连。这里只做一件小事：
# 「连接/超时/5xx 才重试、指数退避 + 抖动、业务错误一次都不重试、每次尝试都留痕」。
#
# ``run_chain`` 的签名与语义**一字未改**：重试是链里某一级的内部行为，重试耗尽的真实错误
# 仍以失败信封交给 ``run_chain`` 去降级。


#: 默认重试次数（含首次尝试）：3 次 = 首次 + 2 次重试。
AKSHARE_RETRY_ATTEMPTS = 3
#: 首次退避基数与单次退避上限（毫秒，指数增长：1200 → 2400 → 4800…）。
AKSHARE_RETRY_BASE_MS = 1200
AKSHARE_RETRY_MAX_MS = 8000
#: 退避抖动比例（在上一步退避量之上叠加 ``[0, 25%)`` 的随机量；注入 ``rand`` 可控）。
AKSHARE_JITTER_RATIO = 0.25
#: 值得重试的异常类名（按 ``type(error).__mro__`` 匹配——requests/urllib3/http.client
#: 各家实现不同，但类名是一致的；``RemoteDisconnected`` 同时是 ``ConnectionResetError``）。
RETRYABLE_ERROR_NAMES = frozenset({
    "ConnectionError", "ConnectionResetError", "ConnectionAbortedError",
    "ConnectionRefusedError", "BrokenPipeError", "RemoteDisconnected",
    "TimeoutError", "Timeout", "ConnectTimeout", "ReadTimeout",
    "URLError", "ProtocolError", "IncompleteRead", "ChunkedEncodingError",
    "NewConnectionError", "MaxRetryError", "ResponseError", "ClosedPoolError",
})


def _http_status(error):
    """异常里的 HTTP 状态码（``urllib.error.HTTPError.code`` / httpx 的 ``status_code``）。"""
    for attr in ("status_code", "code", "status"):
        value = getattr(error, attr, None)
        if isinstance(value, bool):  # bool 是 int 的子类，别把 True 当 1
            continue
        if isinstance(value, int):
            return value
    response = getattr(error, "response", None)
    if response is not None:
        for attr in ("status_code", "status"):
            value = getattr(response, attr, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def is_retryable_akshare(error):
    """异常是否值得重试（**保守**：只认连接类/超时类/HTTP 5xx）。

    业务性错误（参数错、KeyError、ValueError、HTTP 4xx、解析失败…）→ ``False``：
    重试它们只会让「参数写错」多打三次上游，且把真实错误码淹没在重试里。
    """
    if not isinstance(error, BaseException):
        return False
    status = _http_status(error)
    if isinstance(status, int):
        return 500 <= status < 600
    names = {cls.__name__ for cls in type(error).__mro__}
    return bool(names & RETRYABLE_ERROR_NAMES)


def is_empty_akshare_result(value):
    """空结果判定：``None`` / 空容器 / pandas 的 ``.empty``。

    空结果是**业务结果**（源回应了，只是没有数据）→ 不重试，原样透传给调用方，
    由调用方按「空结果视同失败」的链纪律去降级或如实报错。
    """
    if value is None:
        return True
    flag = getattr(value, "empty", None)  # pandas.DataFrame / Series
    if isinstance(flag, bool):
        return flag
    if isinstance(value, (str, bytes, list, tuple, set, frozenset, dict)):
        return len(value) == 0
    return False


def _akshare_backoff_ms(index, base_ms, max_ms, rand):
    """第 ``index``（1 起）次失败后的退避毫秒数：指数增长 + ``[0,25%)`` 抖动，封顶 ``max_ms``。"""
    delay = min(float(max_ms), float(base_ms) * (2 ** (index - 1)))
    return int(min(float(max_ms), delay + rand() * delay * AKSHARE_JITTER_RATIO))


def retry_akshare(call, *, attempts=AKSHARE_RETRY_ATTEMPTS, base_ms=AKSHARE_RETRY_BASE_MS,
                  max_ms=AKSHARE_RETRY_MAX_MS, sleep=time.sleep, clock=time.monotonic,
                  rand=None, is_empty=None, budget_ms=None):
    """带超时/重试地调一次 AKShare 取数 → ``(value, attempts_meta)``。

    规则（与任务书逐条对应）:

      * 只对 ``ConnectionError`` / ``RemoteDisconnected`` / ``TimeoutError`` / ``HTTPError 5xx``
        重试（``is_retryable_akshare``）；**业务性错误不重试**，一次就返回；
      * 指数退避 + 抖动（``base_ms`` 起、``max_ms`` 封顶），等待走注入的 ``sleep``
        （参数是**秒**，与 ``time.sleep`` 一致），耗时走注入的 ``clock``（默认 ``time.monotonic``）；
      * 成功但**空结果**（``None``/空容器/pandas ``.empty``）→ 不重试，原样返回该值
        （调用方按链纪律处理，见 ``is_empty_akshare_result``）；
      * 每次尝试都进 ``attempts_meta``：``{attempt, ok, ms, error?, retryable?, wait_ms?}``；
      * 全失败 → ``(None, attempts_meta)``；``attempts`` 全部用尽或遇到业务错误都走这条返回，
        因此调用方永远能拿到「重试了几次、每次多久、真实错误原文」。

    ``rand`` 注入替代 ``random.random``（单测据此断言确定性的退避时间）。
    ``budget_ms`` 是这一级的**时间预算**（默认 ``None`` = 不设限）：已花时间达到预算就
    **停止重试**，最后一个 attempt 记 ``stopped='budget'``——上游一次调用可能要几十秒
    （实测新浪现货接口单次 50s），没有这一层，降级链的墙钟预算就形同虚设。
    """
    total = max(1, int(attempts))
    base = max(0.0, float(base_ms))
    cap = max(base, float(max_ms))
    rng = random.random if rand is None else rand
    empty_of = is_empty_akshare_result if is_empty is None else is_empty
    budget = None if budget_ms is None else max(0.0, float(budget_ms))
    meta = []
    started = clock()
    for index in range(1, total + 1):
        begin = clock()
        try:
            value = call()
        except Exception as error:  # noqa: BLE001 —— 上游异常按可重试性分类后留痕
            elapsed = int((clock() - begin) * 1000)
            retryable = is_retryable_akshare(error)
            entry = {
                "attempt": index,
                "ok": False,
                "ms": elapsed,
                "retryable": retryable,
                "error": {
                    "code": "akshare/retryable-error" if retryable else "akshare/business-error",
                    "message": _error_text(error),
                },
            }
            spent = (clock() - started) * 1000
            if not retryable or index >= total:
                meta.append(entry)
                return None, meta
            if budget is not None and spent >= budget:
                # 时间预算用尽：**不再重试**，但也如实说明「是预算停的，不是上游好了/业务错」
                entry["stopped"] = "budget"
                entry["budget_ms"] = budget
                meta.append(entry)
                return None, meta
            entry["wait_ms"] = _akshare_backoff_ms(index, base, cap, rng)
            meta.append(entry)
            sleep(entry["wait_ms"] / 1000.0)
            continue
        elapsed = int((clock() - begin) * 1000)
        entry = {"attempt": index, "ok": True, "ms": elapsed}
        if empty_of(value):
            entry["empty"] = True
        meta.append(entry)
        return value, meta
    return None, meta


# ── 探测实现 ────────────────────────────────────────────────────────────────────
# 每条链的探测都是**只读**：K 线取 3 根、快照取 1 个标的、财务取 1 期、资讯取 1 条、
# 板块取 1 个标的、委托取 10 条。探测返回统一信封 {ok, rows/as_of/source, error?}。


def _envelope_error(code, message):
    return {"ok": False, "error": {"code": code, "message": str(message)[:400]}}


def _tool_probe(v3_run, name, payload, expect, describe):
    """把一次工具面调用包装成探测：``expect(value)`` 返回行数/说明，抛错即失败。"""
    envelope = v3_run(name, payload)
    if not isinstance(envelope, dict):
        return _envelope_error("probe/bad-envelope", f"{name} 返回非信封对象：{type(envelope).__name__}")
    if not envelope.get("ok"):
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        return _envelope_error(error.get("code") or f"probe/{name}-failed",
                               error.get("message") or f"{name} 返回 ok=false")
    value = envelope.get("value")
    try:
        detail = expect(value)
    except Exception as error:  # noqa: BLE001 —— 形状不符也是真实失败（不改写成成功）
        return _envelope_error("probe/unexpected-shape", f"{name} 返回形状不符：{_error_text(error)}")
    if not detail:
        return _envelope_error("probe/no-data", f"{name} 未返回可用数据（{describe}）")
    return {
        "ok": True,
        "as_of": (value or {}).get("as_of") if isinstance(value, dict) else None,
        "source": (value or {}).get("source") if isinstance(value, dict) else None,
        "rows": detail,
        "probe": describe,
    }


def _rows_of(value, *keys):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


def _deps_for_sources(home):
    """惰性取 ``v3_sources``（避免模块级循环导入：v3_sources 会 import 本模块）。"""
    from server import v3_sources  # noqa: PLC0415

    return v3_sources, v3_sources.Deps(home=home)


def build_probes(v3_run, home, *, sources_deps=None):
    """返回 ``{key: [(source_name, callable), ...]}``——每条链按主源→降级源排列。

    可用性探测只认「真取到数据」：K 线至少 1 根、财务至少 1 期、资讯至少 1 条、
    委托至少 1 条。取到 0 条 = 该源不可用（不把空壳算成可用）。

    ``sources_deps`` 是可注入的 ``v3_sources.Deps``（测试用假 fetch/假 akshare），缺省按
    ``home`` 建一份真实依赖。
    """
    deps = sources_deps if sources_deps is not None else _deps_for_sources(home)[1]

    def series(value):
        bars = _rows_of(value, "bars")
        return len(bars) if bars else 0

    def f10_statements(value):
        reports = _rows_of(value, "report_list")
        if not reports:
            return 0
        return sum(len(report.get("item_list") or []) for report in reports if isinstance(report, dict))

    def any_rows(value):
        """形状未知时取「任一个非空列表字段」的行数（快照类返回键名随上游而变）。"""
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            for item in value.values():
                if isinstance(item, list) and item:
                    return len(item)
        return 0

    def sectors(value):
        rows = _rows_of(value, "sectors")
        return len(rows) if rows else 0

    def stock_list(value):
        rows = _rows_of(value, "stock_list")
        return len(rows) if rows else 0

    def order_groups(value):
        groups = _rows_of(value, "groups")
        return sum(len(group.get("rows") or []) for group in groups if isinstance(group, dict))

    def akshare_kline(deps):
        from server import v3_sources  # noqa: PLC0415

        return v3_sources.fetch_kline_akshare(deps, PROBE_TICKER, 3)

    def akshare_cn_financials(deps):
        from server import v3_sources  # noqa: PLC0415

        return v3_sources.fetch_financials_akshare(deps, "SH.600000", "income", 1)

    def akshare_news(deps):
        from server import v3_sources  # noqa: PLC0415

        return v3_sources.fetch_news(deps, "600000", 1)

    def akshare_spot(deps):
        from server import v3_sources  # noqa: PLC0415

        return v3_sources.fetch_spot(deps, 3)

    def sec_financials(home_dir):
        from server import v3_sources  # noqa: PLC0415

        sec = v3_sources.SecSource(deps, home=home_dir)
        cik, _company, error = sec.resolve(PROBE_US_TICKER)
        if error is not None:
            return error
        return sec.concept(cik, "Revenues", 1)

    def openbb_financials(deps):
        """保留给需要「真的探一次 openbb」的调用方（重依赖，默认探测路径不调用它）。"""
        from server import v3_sources  # noqa: PLC0415

        return v3_sources.fetch_openbb(deps, PROBE_US_TICKER)

    return {
        "kline": [
            ("futu/quote_history_kline", lambda: _tool_probe(
                v3_run, "series", {"ticker": PROBE_TICKER, "period": "1d", "limit": 20},
                series, "series SH.600000 limit=20（工具面下限）取最近 3 根")),
            ("akshare/stock_zh_a_hist", lambda: akshare_kline(deps)),
        ],
        "snapshot": [
            ("futu/market_snapshot", lambda: _tool_probe(
                v3_run, "market_snapshot", {"codes": [PROBE_TICKER]}, any_rows,
                "market_snapshot SH.600000（需实时行情权限）")),
            ("akshare/stock_zh_a_spot_em", lambda: akshare_spot(deps)),
        ],
        "financials_cn": [
            ("futu/f10_detail/statements", lambda: _tool_probe(
                v3_run, "f10_detail",
                {"code": PROBE_TICKER, "section": "statements", "params": {"statement_type": 1, "limit": 1}},
                f10_statements, "f10_detail SH.600000 statements(利润表) limit=1 期")),
            ("akshare/stock_financial_abstract", lambda: akshare_cn_financials(deps)),
        ],
        "financials_us": [
            ("sec/companyconcept(us-gaap XBRL)", lambda: sec_financials(home)),
            # openbb 的 import 实测 44~57s（见 v3_sources.OPENBB_IMPORT_TIMEOUT），**不是**轻量
            # 探测：这里如实记一条「未探测」的尝试（含原因），实际可用性由 /api/v3/openbb 按需核对。
            ("openbb/equity.fundamental", lambda: _envelope_error(
                "probe/skipped-heavy",
                "openbb 冷启动实测 44~57s，属重依赖 → 状态探测不触发它；"
                "实际可用性请用 GET /api/v3/openbb 按需核对（两个源都失败时该链如实标不可用）")),
        ],
        "news": [
            ("futu/info_search", lambda: _tool_probe(
                v3_run, "info_search", {"keyword": "600000", "size": 3},
                lambda value: len(_rows_of(value, "rows", "items", "news_list", "data")),
                "info_search keyword=600000 取 1 条")),
            ("akshare/stock_news_em", lambda: akshare_news(deps)),
        ],
        "industry": [
            ("futu/info_owner_plate", lambda: _tool_probe(
                v3_run, "info_owner_plate", {"code": PROBE_TICKER}, sectors,
                "info_owner_plate SH.600000 取所属板块")),
            ("futu/plate_stock", lambda: _tool_probe(
                v3_run, "plate_stock", {"plate_code": PROBE_PLATE_CODE, "limit": 3}, stock_list,
                f"plate_stock {PROBE_PLATE_CODE} 取成分股 3 条")),
        ],
        "quality": [
            ("futu/orders_history", lambda: _tool_probe(
                v3_run, "orders_history", {"market": "SH", "page_size": 10}, order_groups,
                "orders_history market=SH page_size=10 取委托")),
        ],
        "spot": [
            ("futu/market_snapshot", lambda: _tool_probe(
                v3_run, "market_snapshot", {"codes": [PROBE_TICKER]}, series,
                "market_snapshot SH.600000（需实时行情权限）")),
            ("akshare/stock_zh_a_spot_em", lambda: akshare_spot(deps)),
        ],
    }


def probe_chains(v3_run, home, *, keys=None, timeout=PROBE_CHAIN_TIMEOUT, sources_deps=None):
    """逐链探测并返回 ``chains`` 列表（契约字段齐全，另附 ``attempts``/``probe``）。

    ``available`` = 链里**至少一级**真的取到了数据；``last_source``/``last_ok`` 是实际命中
    （或最后尝试）的那一级；全失败时 ``error`` 保留最后一个上游错误原文。
    """
    probes = build_probes(v3_run, home, sources_deps=sources_deps)
    wanted = [key for key in (keys or [spec["key"] for spec in CHAIN_SPECS]) if key in SPEC_BY_KEY]
    rows = []
    for key in wanted:
        spec = dict(SPEC_BY_KEY[key])
        chain = probes.get(key) or []
        value, used_source, attempts = run_chain(chain, timeout=timeout)
        checked_at = now_iso()
        last = attempts[-1] if attempts else {}
        row = {
            "key": spec["key"],
            "label": spec["label"],
            "primary": spec["primary"],
            "fallback": spec["fallback"],
            "available": used_source is not None,
            "last_source": used_source or last.get("source") or spec["primary"],
            "last_ok": used_source is not None,
            "checked_at": checked_at,
            "attempts": attempts_chain(attempts),
            "probe": (value or {}).get("probe") if isinstance(value, dict) else None,
            "rows": (value or {}).get("rows") if isinstance(value, dict) else None,
            "as_of": (value or {}).get("as_of") if isinstance(value, dict) else None,
            "error": None if used_source is not None else last_error(attempts),
        }
        row["chain_size"] = len(chain)
        rows.append(row)
    return rows


# ── 路由注册 ────────────────────────────────────────────────────────────────────


def register(app, v3_run, home, deps=None):
    """挂 ``GET /api/v3/sources/status``。

    ``deps`` 仅供测试注入：``probe``（整体替换探测器）、``sources_deps``
    （注入 ``v3_sources.Deps``，用假 akshare/假 fetch 替掉真实网络）。
    """
    deps = deps or {}
    probe = deps.get("probe") or (
        lambda keys: probe_chains(v3_run, home, keys=keys, sources_deps=deps.get("sources_deps")))

    @app.get("/api/v3/sources/status")
    async def v3_sources_status(keys: str = ""):
        """各数据源降级链的**真实**可用性（一次轻量只读探测；不探测任何写端点）。"""
        wanted = [item.strip() for item in str(keys or "").split(",") if item.strip()] or None
        try:
            chains = await asyncio.to_thread(probe, wanted)
        except Exception as error:  # noqa: BLE001 —— 统一信封，不把栈透给前端
            return {"ok": False, "error": {"code": "sources/internal", "message": _error_text(error)}}
        if not isinstance(chains, list):
            return {"ok": False, "error": {"code": "sources/bad-probe",
                                           "message": f"探测器返回了 {type(chains).__name__}，不是链列表"}}
        # 规格 §8.3：把这次**真实**探测的结果落盘，供 Prometheus 出口 ``/metrics`` 读取。
        # 理由：``/metrics`` 抓取路径上**不能**再发起外部探测（富途探测要消耗限流额度，
        # 用监控触发「被限流」告警是自伤），所以降级链指标只能来自「别人已经探测过的结果」。
        # best-effort：落盘失败不阻断本响应（与仓库其余留痕同义）。
        #
        # 惰性 import 是**必须**的：``server.observability`` → ``server.v3_ops`` →
        # ``server.v3_universe`` → ``server.v3_quality`` → 本模块，顶层 import 会形成
        # 循环（本模块此时只初始化到一半，``PROBE_CHAIN_TIMEOUT`` 还不存在）。
        from server.observability import record_datasource_probe
        record_datasource_probe(home, chains)
        return {
            "ok": True,
            "as_of": now_iso(),
            "chains": chains,
            "note": NOTE,
            "probe_policy": "每条链按主源→降级源各做一次轻量只读探测；空结果视同不可用",
        }

    app.state.v3_fallback = {"routes": ("/api/v3/sources/status",)}
    return probe_chains
