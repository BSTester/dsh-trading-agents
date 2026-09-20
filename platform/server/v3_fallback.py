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
import time
from datetime import datetime, timezone

__all__ = [
    "CHAIN_SPECS",
    "attempts_chain",
    "describe_attempts",
    "failure_of",
    "probe_chains",
    "register",
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
        return {
            "ok": True,
            "as_of": now_iso(),
            "chains": chains,
            "note": NOTE,
            "probe_policy": "每条链按主源→降级源各做一次轻量只读探测；空结果视同不可用",
        }

    app.state.v3_fallback = {"routes": ("/api/v3/sources/status",)}
    return probe_chains
