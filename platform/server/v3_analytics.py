"""V3 控制台**分析类**接口：风险量 / 因子矩阵 / 策略流水线 / 回测与参数扫描。

接线（由 ``server/app.py`` 负责，本模块不自启）：``app.py`` 的 V3 子模块自动接线循环会
``import server.v3_analytics`` 并调用 ``register(app, v3_run, home)``——位置固定在静态兜底
路由之前，因此这里注册的路径先于 ``/{path:path}`` 命中。

``v3_run(name, payload)`` 是主 agent 注入的**同步**回调：按名调用既有 56 工具面的同一份
``handle``，返回原始信封 ``{ok, value|error}``，契约上不抛异常。取数一律经它，本模块
**不新造第二事实源**，也不直连任何数据库/文件（唯一例外是策略流水线结果的落盘与自选池
配置读取，见下）。

纪律（与 ``docs/v3-integration.md`` §五 一致）：
  * 阻塞取数（``v3_run`` → 子进程）全部在 ``asyncio.to_thread`` 里执行，handler 不阻塞事件循环；
  * 任何工具/外部失败都转成 ``{ok: false, error: {code, message}}`` 信封，**不抛 500**；
  * 数值一律实测：取不到就 ``null``/错误码 + 说明，**绝不用估算值顶替**；
  * 只读：不下单、不改单、不切模式。策略流水线的产物只是「提案」，执行仍走工作台受约束入口。

接口清单（契约见各 handler docstring；计算口径见 ``server/v3_math.py``）：

===============================  ======  ================================================
路径                              方法    用途
===============================  ======  ================================================
``/api/v3/risk/analytics``       GET     组合 VaR/CVaR/Beta/Alpha/IR/Kupiec + 净值曲线
``/api/v3/factors/matrix``       GET     横截面因子 z 矩阵 + 因子 IC 序列
``/api/v3/strategy``             GET     最近一轮研究流水线结果
``/api/v3/strategy/run``         POST    跑一轮 PDAT→PET 流水线（只出提案）
``/api/v3/ml/sweep``             GET     动量策略参数网格扫描（真实回测）
``/api/v3/ml/backtest``          POST    单标的动量 long/flat 回测（PIT）
===============================  ======  ================================================
"""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Request
from starlette.responses import JSONResponse

from server import v3_math

__all__ = [
    "STRATEGY_RUNS_FILE",
    "factors_matrix_data",
    "ml_backtest",
    "ml_sweep",
    "read_last_strategy_run",
    "read_watchlist",
    "register",
    "risk_analytics",
    "strategy_last",
    "strategy_run",
]

# 策略流水线结果：一轮一行 JSONL（`GET /api/v3/strategy` 返回最后一轮）。
STRATEGY_RUNS_FILE = "v3-strategy-runs.jsonl"

# 工作台 plan 里可作为组合定义的权重口径：单笔目标权重上限（%，与 platform-v3 同值）。
SINGLE_NAME_LIMIT_PCT = 2.0

# 自选池等权组合的标的数上限（参考实现 portfolio.resolve 的 limit=8）。
WATCHLIST_LIMIT = 8
# 因子横截面矩阵/IC 的标的数上限（参考实现 slice(0, 8)）。
FACTOR_LIMIT = 8
# 因子矩阵缺省标的数（自选池前 6）。
FACTOR_DEFAULT_COUNT = 6


# ---------------------------------------------------------------------------
# 小工具：信封、参数解析、home 路径
# ---------------------------------------------------------------------------
def _error(code, message, **extra):
    """错误信封（与 ``server/app.py error_envelope`` 同形，但固定走 200 + ok=false）。"""
    payload = {"code": str(code), "message": str(message)[:300]}
    payload.update(extra)
    return {"ok": False, "error": payload}


def _tool_error(envelope, fallback_code="wb/error"):
    """把工具面信封里的 ``error`` 原样搬进我们的错误信封（保留上游错误码，不吞原因）。"""
    error = (envelope or {}).get("error")
    if isinstance(error, dict) and error:
        return dict(error)
    return {"code": fallback_code, "message": _error_message(envelope)}


def _error_message(envelope):
    error = (envelope or {}).get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "unknown")
    if error:
        return str(error)
    return "unknown"


def _call(v3_run, name, payload):
    """调用工具面并保证返回信封：``v3_run`` 契约上不抛，但这条防线不能没有。"""
    try:
        envelope = v3_run(name, payload)
    except Exception as error:  # noqa: BLE001 —— 与工具面口径一致：失败进 error 信封
        return _error("v3/tool-failed", f"{name}: {error}")
    if not isinstance(envelope, dict):
        return _error("v3/bad-envelope", f"{name} 返回了非信封对象：{type(envelope).__name__}")
    return envelope


def _value_of(envelope):
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return None
    value = envelope.get("value")
    return value if isinstance(value, dict) else None


def _bars_of(envelope):
    value = _value_of(envelope) or {}
    bars = value.get("bars")
    return bars if isinstance(bars, list) else []


def _clamp_int(value, default, minimum, maximum):
    """``Math.min(Math.max(Number(v), min), max)`` 的稳妥版：非数值 → 用缺省值。"""
    number = v3_math.to_float(value)
    if number is None:
        return default
    return max(minimum, min(maximum, int(number)))


def _int_list(raw, default, minimum, maximum, max_items):
    """逗号分隔（或数组）的正整数列表：保序去重，超范围/非数值的项剔除。

    解析后为空 → 返回空列表（调用方转 ``bad-request``）；``raw`` 为空 → 返回 ``default``。
    """
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        text = "" if raw is None else str(raw).strip()
        if not text:
            return list(default)
        items = text.split(",")
    out = []
    seen = set()
    for item in items:
        number = v3_math.to_float(item)
        if number is None:
            continue
        value = int(number)
        if value < minimum:
            continue
        value = min(maximum, value)
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out[:max_items]


def _home_path(home):
    """``home`` 归一成路径：缺省 ``$DSH_HOME`` → ``~/.dsh``（与 ``compute.command_home`` 同口径）。"""
    if home in (None, ""):
        home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    return Path(str(home)).expanduser()


def _now_iso():
    """``new Date().toISOString()`` 等价（毫秒精度 + ``Z``）。"""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _plain(value):
    """数值 → 文案（用于提案的 ``basis``）：整数值不拖 ``.0``，与 JS 字符串插值同观感。"""
    number = v3_math.to_float(value)
    if number is None:
        return "—"
    text = repr(number)
    return text[:-2] if text.endswith(".0") else text


async def _read_json_body(request):
    """读 POST body：空体按 ``{}``（无内容的探测请求不该被判非法），坏 JSON/非对象 → ``None``。"""
    try:
        body = await request.body()
    except Exception:  # noqa: BLE001 —— body 读取失败按「无有效载荷」处理
        return None
    text = body.decode("utf-8", errors="replace").strip() if body else ""
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# 自选池与策略结果落盘
# ---------------------------------------------------------------------------
def read_watchlist(home):
    """自选池：``<home>/trading-platform.json`` 顶层 ``watchlist``；缺失/损坏 → 空池。

    与 ``compute.watchlist_symbols``（``trading_core.watchlist``）读的是**同一份配置的同一个键**；
    这里只读一行 JSON 而不导入服务模块，好处是这个入口不依赖服务上下文（单测可直接注入
    临时 home），语义完全一致：取不到就是空池，不臆造标的。
    """
    path = _home_path(home) / "trading-platform.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    watchlist = raw.get("watchlist")
    if not isinstance(watchlist, list):
        return []
    names = []
    for item in watchlist:
        text = str(item or "").strip()
        if text and text not in names:
            names.append(text)
    return names


def strategy_runs_path(home):
    return _home_path(home) / STRATEGY_RUNS_FILE


def append_strategy_run(home, run):
    """追加一轮流水线结果（一行一条 JSONL）。失败返回错误字典（调用方如实回传）。"""
    path = strategy_runs_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(run, ensure_ascii=False, allow_nan=False) + "\n")
    except (OSError, ValueError) as error:
        return {"code": "strategy/persist-failed", "message": str(error)[:300]}
    return None


def read_last_strategy_run(home):
    """读最后一轮流水线结果；文件不存在/无有效行 → ``None``（从未运行过）。"""
    path = strategy_runs_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


# ---------------------------------------------------------------------------
# 工作台取数
# ---------------------------------------------------------------------------
def _load_series(v3_run, tickers, limit):
    """并发取多标的日 K（参考实现用 ``Promise.all``；这里并发度上限 8）。

    返回 ``(bars_by_ticker, errors, sources)``：
      * ``bars_by_ticker``：每个标的都有键（失败给空列表），调用方按名取用；
      * ``errors``：``[{"ticker": ..., "error": "<message>"}]``——失败就报，不静默补数；
      * ``sources``：``{ticker: <source 字符串>}``，工具面自报的数据源（如
        ``futu/quote_history_kline``），用于在响应里如实标注取数来源。
    """
    bars_by_ticker = {}
    errors = []
    sources = {}
    if not tickers:
        return bars_by_ticker, errors, sources

    def fetch(ticker):
        return ticker, _call(v3_run, "series", {"ticker": ticker, "period": "1d", "limit": limit})

    workers = max(1, min(8, len(tickers)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v3-series") as pool:
        results = list(pool.map(fetch, tickers))

    for ticker, envelope in results:
        if envelope.get("ok"):
            value = _value_of(envelope) or {}
            bars = value.get("bars")
            bars_by_ticker[ticker] = bars if isinstance(bars, list) else []
            source = value.get("source")
            if isinstance(source, str) and source:
                sources[ticker] = source
        else:
            bars_by_ticker[ticker] = []
            errors.append({"ticker": ticker, "error": _error_message(envelope)})
    return bars_by_ticker, errors, sources


def _kline_source(sources):
    """把逐标的 source 收成一个可展示的字符串；一个都没有 → ``None``（不写死常量）。"""
    unique = sorted({value for value in (sources or {}).values() if value})
    if not unique:
        return None
    return unique[0] if len(unique) == 1 else " + ".join(unique)


def _nav_of(envelope):
    """``nav`` 取 ``equity.current``（有限且 > 0 才算拿到；否则 ``None``，绝不估算）。"""
    value = _value_of(envelope) or {}
    current = v3_math.to_float(value.get("current"))
    if current is None or current <= 0:
        return None
    return current


def _resolve_portfolio(v3_run, home, explicit):
    """组合定义：显式权重 → 工作台 frozen 多标的计划 → 自选池等权。

    返回 ``(weights, source)``；无可用定义时 ``(None, 原因)``（调用方转 ``risk/no-portfolio``）。
    """
    if explicit:
        return dict(explicit), v3_math.SOURCE_EXPLICIT
    envelope = _call(v3_run, "plan", {})
    value = _value_of(envelope) or {}
    plans = value.get("plans")
    weights, source = v3_math.pick_frozen_plan_weights(plans if isinstance(plans, list) else [])
    if weights:
        return weights, source
    return v3_math.watchlist_equal_weights(read_watchlist(home), WATCHLIST_LIMIT)


# ---------------------------------------------------------------------------
# 1) GET /api/v3/risk/analytics
# ---------------------------------------------------------------------------
def risk_analytics(v3_run, home, limit=250, confidence=0.95, benchmark="SH.000300",
                   weights_raw=None):
    """组合风险量（可注入 ``v3_run``/``home``，路由只是它的异步外壳）。

    契约（与前端既有实现严格一致）::

        {ok, portfolioSource, benchmarkTicker, nav,
         analytics: {confidence, observations, tickers, window:{from,to},
                     varDailyPct, cvarDailyPct, varAmount, cvarAmount,
                     annVolPct, annReturnPct, maxDrawdownPct,
                     beta, alphaAnnPct, ir, benchmarkAnnReturnPct,
                     kupiec: {lr, pValue, breaches, observations, pass},
                     equityCurve: [{t, v}], method},
         sources: {kline, nav, errors}, navNote}

    口径：组合 = 显式 ``weights``（JSON）→ 首个 frozen 且标的数 ≥2 的计划 → 自选池等权；
    逐标的 ``series``（period=1d）与基准按交易日对齐（交集、升序）；历史模拟法 VaR/CVaR；
    Beta/Alpha（年化 252）、IR、最大回撤；Kupiec POF。对齐后 <40 个交易日 → ``risk/insufficient``。
    """
    try:
        limit = _clamp_int(limit, 250, 60, 2000)
        level = v3_math.to_float(confidence)
        if level is None:
            return _error("risk/bad-confidence", f"confidence 需为数值，收到 {confidence!r}")
        if not 0.0 < level < 1.0:
            return _error("risk/bad-confidence", f"confidence 必须在 (0,1) 内，收到 {level}")

        explicit = None
        if weights_raw not in (None, "", {}):
            if isinstance(weights_raw, dict):
                explicit = weights_raw
            else:
                try:
                    explicit = json.loads(str(weights_raw))
                except ValueError as error:
                    return _error("bad-request", f"weights 需为 JSON 对象：{error}")
            if not isinstance(explicit, dict) or not explicit:
                return _error("bad-request", "weights 需为非空 JSON 对象（{标的: 权重}）")
            explicit = {str(key): value for key, value in explicit.items()}

        weights, source = _resolve_portfolio(v3_run, home, explicit)
        if not weights:
            return _error("risk/no-portfolio", source)
        if len(weights) == 1:
            return _error("risk/single-name",
                          f"组合仅 1 个标的（{source}），单标的组合风险量无横截面意义")

        benchmark_ticker = str(benchmark or "").strip() or "SH.000300"
        tickers = list(weights)
        series_map, series_errors, series_sources = _load_series(v3_run, tickers, limit)
        bench_envelope = _call(v3_run, "series",
                               {"ticker": benchmark_ticker, "period": "1d", "limit": limit})
        bench_ok = bool(bench_envelope.get("ok"))
        bench_bars = _bars_of(bench_envelope) if bench_ok else None
        nav = _nav_of(_call(v3_run, "equity", {"window": 30}))

        analytics = v3_math.portfolio_risk(
            series_by_ticker=series_map,
            benchmark_bars=bench_bars,
            weights=weights,
            confidence=level,
            nav=nav,
        )
        if analytics.get("error"):
            return _error("risk/insufficient", analytics["error"])

        return {
            "ok": True,
            "portfolioSource": source,
            "benchmarkTicker": benchmark_ticker if bench_ok else None,
            "nav": nav,
            "analytics": analytics,
            "sources": {
                "kline": _kline_source(series_sources),
                "nav": "sim-ledger(equity.current)" if nav is not None else None,
                "errors": series_errors,
            },
            "navNote": (
                f"nav 取 equity.current={nav}（本地模拟台账权益，不代表券商资产）"
                if nav is not None
                else "nav 取不到：equity 工具失败或 current 非正（响应中 nav=null，未用估算值顶替）"
            ),
        }
    except Exception as error:  # noqa: BLE001 —— 任何内部异常都进信封，绝不 500
        return _error("v3/internal", error)


# ---------------------------------------------------------------------------
# 2) GET /api/v3/factors/matrix
# ---------------------------------------------------------------------------
def _factor_ic(v3_run, names, factor, forward_days):
    """因子 IC：需要 3..8 个标的（横截面相关），工具面字段名是 ``forward``。"""
    tickers = list(names)[:FACTOR_LIMIT]
    if len(tickers) < 3:
        return {"ok": False, "factor": factor,
                "error": _error("factors/too-few", "IC 需要 3..8 个标的（横截面相关）")["error"]}
    envelope = _call(v3_run, "ic",
                     {"tickers": tickers, "factor": factor, "forward": forward_days})
    if not envelope.get("ok"):
        return {"ok": False, "factor": factor, "error": _tool_error(envelope)}
    return v3_math.ic_stats(_value_of(envelope) or {}, factor, forward_days,
                            fallback_tickers=tickers)


def factors_matrix_data(v3_run, home, tickers_raw=None, factor="mom_20", forward_days=5):
    """横截面因子矩阵 + 因子 IC 序列。

    契约::

        {ok, matrix: {as_of, source, tickers[], factors[], matrix[][],
                      raw: [{ticker, factors}]},
         ic: {ok, factor, forwardDays, tickers, observations, meanIc, stdIc, ir,
              latestIc, points: [{t, ic}]}}

    标的 2..8：请求显式给（取前 8）→ 否则自选池前 6；不足 2 只 → ``factors/too-few``。
    矩阵值取 ``factors`` 的 ``z``（缺失格 ``null``）；IC 来自 ``ic`` 工具，缺省
    ``factor=mom_20``、``forward=5``。IC 失败**不拖垮矩阵**：``ic.ok=false`` + ``error``。
    """
    try:
        if factor is None or str(factor).strip() == "":
            factor = "mom_20"
        factor = str(factor).strip()
        forward_days = _clamp_int(forward_days, 5, 1, 250)

        if isinstance(tickers_raw, (list, tuple)):
            requested = [str(item).strip() for item in tickers_raw]
        else:
            requested = [item.strip() for item in str(tickers_raw or "").split(",")]
        requested = [item for item in requested if item]

        if len(requested) >= 2:
            names = requested[:FACTOR_LIMIT]
        else:
            names = read_watchlist(home)[:FACTOR_DEFAULT_COUNT]
        if len(names) < 2:
            return _error("factors/too-few",
                          "横截面矩阵需要 2..8 个标的（请求未给 tickers，自选池也不足 2 只）")

        envelope = _call(v3_run, "factors", {"tickers": names})
        if not envelope.get("ok"):
            return {"ok": False, "error": _tool_error(envelope)}
        value = _value_of(envelope) or {}
        rows = value.get("rows")
        matrix = v3_math.factor_matrix(rows if isinstance(rows, list) else [])
        # 工具面逐标的的失败原因照样带出去（矩阵里那一行就是空的，原因不能丢）
        matrix["failures"] = value.get("failures") if isinstance(value.get("failures"), dict) else {}
        return {"ok": True, "matrix": matrix,
                "ic": _factor_ic(v3_run, names, factor, forward_days)}
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


# ---------------------------------------------------------------------------
# 3) GET /api/v3/strategy + POST /api/v3/strategy/run
# ---------------------------------------------------------------------------
def strategy_last(home):
    """最后一轮流水线：从未运行过 → ``{ok:true, run:null, note:'尚未运行研究流水线'}``。"""
    try:
        run = read_last_strategy_run(home)
        if run:
            return {"ok": True, "run": run}
        return {"ok": True, "run": None, "note": "尚未运行研究流水线"}
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


def _strategy_analysis(v3_run, universe_list, klines, window):
    """PAAT：优先 workbench ``factors`` 的 z（综合分 = mom_20/mom_60/trend 的 z 均值），
    不可用时**如实标注**并退化为本地 K 线动量的横截面 z（保证候选池不为空）。"""
    factor_rows = []
    factors_error = None
    if len(universe_list) >= 2:
        envelope = _call(v3_run, "factors", {"tickers": universe_list[:FACTOR_LIMIT]})
        if envelope.get("ok"):
            rows = (_value_of(envelope) or {}).get("rows")
            factor_rows = rows if isinstance(rows, list) else []
        else:
            factors_error = _tool_error(envelope)

    analysis = []
    for ticker in universe_list:
        row = next((item for item in factor_rows
                    if isinstance(item, dict) and item.get("ticker") == ticker), None)
        composite = v3_math.composite_z(row) if row is not None else None
        bars = klines.get(ticker) or []
        closes = []
        for bar in bars:
            if not isinstance(bar, dict):
                continue
            close = v3_math.to_float(bar.get("c"))
            if close is not None:
                closes.append(close)
        local_momentum = (closes[-1] / closes[-1 - window] - 1) if len(closes) > window else None
        analysis.append({
            "ticker": ticker,
            "compositeZ": None if composite is None else v3_math.round_half_up(composite, 4),
            "factors": {key: (row.get("factors") or {}).get(key)
                        for key in ("mom_20", "mom_60", "rsi_14", "mdd_60")} if row else None,
            "localMomentum": (None if local_momentum is None
                              else f"{v3_math.round_half_up(local_momentum * 100, 2)}%"),
            "localMomentumRaw": local_momentum,
            "dataOk": len(bars) > 0,
        })

    score_source = "workbench/factors(z)"
    if all(item["compositeZ"] is None for item in analysis):
        raw_values = [item["localMomentumRaw"] for item in analysis]
        usable = sum(1 for value in raw_values if value is not None)
        if usable >= 2:
            for item, value in zip(analysis, v3_math.cross_sectional_z(raw_values)):
                if value is None:
                    continue
                item["compositeZ"] = v3_math.round_half_up(value, 4)
                item["fallback"] = True
            score_source = "local/series 动量横截面 z（workbench factors 不可用时的兜底）"

    stage = {
        "analyzed": len(analysis),
        "withFactors": sum(1 for item in analysis if item["compositeZ"] is not None),
        "scoreSource": score_source,
        "factorsError": factors_error,
    }
    return analysis, stage


def strategy_run(v3_run, home, payload=None):
    """跑一轮研究流水线 PDAT → PAAT → PCPT → PRT → PET，结果落盘并返回。

    契约::

        {ok, run: {asOf, universe, stages: {PDAT: {universe, bars, errors},
                                            PAAT: {analyzed, withFactors, scoreSource, factorsError},
                                            PCPT: {longs, reduces},
                                            PRT: {weightPctPerName, capped},
                                            PET: {proposals}},
                   proposals: [{ticker, action, targetWeightPct, basis, riskLevel}]}}

    **不下单**：PET 产物是「调仓建议提案」（``action_hint`` 明确执行走工作台受约束入口），
    并且只写 ``<home>/v3-strategy-runs.jsonl``。

    与参考实现 ``pipeline.mjs`` 的有意差异（各一处，均为修掉会自相矛盾的边界）：
      1. ``reduces`` 在 ``len(ranked) <= topN`` 时取**空**（参考实现 ``slice(-0)`` 会退化成
         ``slice(0)``，把刚判为「增持」的标的又列进「减持」）；
      2. ``localMomentum`` 输出真正的百分数字符串（参考实现 ``Number('12.34%')`` 恒为 NaN）。
    """
    try:
        payload = payload if isinstance(payload, dict) else {}
        top_n = _clamp_int(payload.get("topN"), 2, 1, 50)
        window = _clamp_int(payload.get("window"), 20, 1, 500)

        raw_universe = payload.get("universe")
        if isinstance(raw_universe, (list, tuple)):
            universe = [str(item).strip() for item in raw_universe if str(item or "").strip()]
        elif isinstance(raw_universe, str):
            universe = [item.strip() for item in raw_universe.split(",") if item.strip()]
        else:
            universe = []
        universe_list = (universe or read_watchlist(home))[:FACTOR_LIMIT]
        if not universe_list:
            return _error("strategy/no-universe",
                          "无可用标的：请求未给 universe，自选池（trading-platform.json watchlist）也为空")

        as_of = _now_iso()
        stages = {}

        # PDAT：数据准备（逐标的真实日 K，limit 至少 120 且覆盖 window 的 4 倍）
        series_limit = max(120, window * 4)
        bars_map, series_errors, _sources = _load_series(v3_run, universe_list, series_limit)
        klines = {}
        bar_count = 0
        for ticker in universe_list:
            bars = bars_map.get(ticker) or []
            if bars:
                klines[ticker] = bars
                bar_count += len(bars)
        stages["PDAT"] = {"universe": universe_list, "bars": bar_count,
                          "errors": series_errors}

        # PAAT：因子分析（workbench factors → 本地动量兜底）
        analysis, paat = _strategy_analysis(v3_run, universe_list, klines, window)
        stages["PAAT"] = paat

        # PCPT：候选池（按综合 z 降序；排序稳定，同分保持 universe 顺序）
        ranked = sorted([item for item in analysis if item["compositeZ"] is not None],
                        key=lambda item: item["compositeZ"], reverse=True)
        longs = ranked[:top_n]
        reduce_count = min(top_n, max(0, len(ranked) - top_n))
        reduces = list(reversed(ranked[len(ranked) - reduce_count:])) if reduce_count else []
        stages["PCPT"] = {"longs": [item["ticker"] for item in longs],
                          "reduces": [item["ticker"] for item in reduces]}

        # PRT：等权目标权重（受单笔上限约束）
        weight_pct = min(SINGLE_NAME_LIMIT_PCT, 100.0 / max(1, len(longs)))
        capped = (100.0 / len(longs) > SINGLE_NAME_LIMIT_PCT) if longs else True
        stages["PRT"] = {"weightPctPerName": v3_math.round_half_up(weight_pct, 4),
                         "capped": capped}

        # PET：调仓建议提案（不下单）
        proposals = []
        for item in longs:
            composite = item["compositeZ"]
            mom_20 = (item.get("factors") or {}).get("mom_20")
            proposals.append({
                "ticker": item["ticker"],
                "action": "增持",
                "targetWeightPct": v3_math.round_half_up(weight_pct, 4),
                "basis": f"综合动量 z={_plain(composite)}（mom_20={_plain(mom_20)}）",
                "riskLevel": "低" if (composite or 0) > 0.5 else "中",
                "action_hint": "经审批后由工作台受约束入口执行",
            })
        for item in reduces:
            proposals.append({
                "ticker": item["ticker"],
                "action": "减持",
                "targetWeightPct": 0,
                "basis": f"综合动量 z={_plain(item['compositeZ'])}（排名末位）",
                "riskLevel": "中",
                "action_hint": "经审批后由工作台受约束入口执行",
            })
        stages["PET"] = {"proposals": len(proposals)}

        summary = {"asOf": as_of, "universe": universe_list, "stages": stages,
                   "proposals": proposals}
        persist_error = append_strategy_run(home, summary)
        result = {"ok": True, "run": summary}
        if persist_error:
            result["persistError"] = persist_error
            result["note"] = "流水线已完成，但结果落盘失败（GET /api/v3/strategy 可能读不到这一轮）"
        return result
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


# ---------------------------------------------------------------------------
# 4) GET /api/v3/ml/sweep
# ---------------------------------------------------------------------------
def ml_sweep(v3_run, ticker="SH.600519", windows_raw="10,20,30,60",
             rebalance_raw="5,10,20", limit=500):
    """参数扫描网格。

    契约::

        {ok, ticker, grid: [{window, rebalanceDays, sharpe, annReturnPct,
                             maxDrawdownPct, error?}], best: {...} | null}

    ``best`` = Sharpe 最大的**有效**格；全失败 → ``null``。取不到 K 线 → 上游错误信封。
    """
    try:
        ticker = str(ticker or "").strip() or "SH.600519"
        limit = _clamp_int(limit, 500, 60, 2000)
        windows = _int_list(windows_raw, (10, 20, 30, 60), 1, 500, 8)
        rebalance = _int_list(rebalance_raw, (5, 10, 20), 1, 500, 8)
        if not windows or not rebalance:
            return _error("bad-request", "windows/rebalance 需为逗号分隔的正整数（如 10,20,30,60）")

        envelope = _call(v3_run, "series",
                         {"ticker": ticker, "period": "1d", "limit": limit})
        if not envelope.get("ok"):
            return {"ok": False, "error": _tool_error(envelope)}
        result = v3_math.param_sweep(_bars_of(envelope), windows, rebalance)
        return {"ok": True, "ticker": ticker, **result}
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


# ---------------------------------------------------------------------------
# 5) POST /api/v3/ml/backtest
# ---------------------------------------------------------------------------
def ml_backtest(v3_run, payload=None):
    """单标的动量 long/flat 回测（PIT：``t`` 日持仓只由 ``≤ t-1`` 收盘价决定）。

    契约::

        {ok, ticker, metrics: {sharpe, annReturnPct, maxDrawdownPct, signalFlips,
                               heldDays, flatDays, days, winRatePct},
         equity: [{t, value}]}

    指标按**持仓日**基准（空仓日不计入胜率）；数据不足 → ``backtest/insufficient``。
    """
    try:
        payload = payload if isinstance(payload, dict) else {}
        ticker = str(payload.get("ticker") or "").strip()
        if not ticker:
            return _error("bad-request", "ticker 必填")
        limit = _clamp_int(payload.get("limit"), 500, 60, 2000)
        window = _clamp_int(payload.get("window"), 20, 1, 500)
        rebalance_days = _clamp_int(payload.get("rebalanceDays"), 5, 1, 500)

        envelope = _call(v3_run, "series",
                         {"ticker": ticker, "period": "1d", "limit": limit})
        if not envelope.get("ok"):
            return {"ok": False, "error": _tool_error(envelope)}
        result = v3_math.backtest_momentum(_bars_of(envelope), window, rebalance_days)
        if result.get("error"):
            return _error("backtest/insufficient", result["error"])
        return {"ok": True, "ticker": ticker, "metrics": result["metrics"],
                "equity": result["equity"]}
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------
def _ok(content):
    """所有 V3 分析接口都以 200 + 信封应答（``ok=false`` 由前端按错误提示展示）。"""
    return JSONResponse(status_code=200, content=content)


def register(app, v3_run, home):
    """把 5 个分析类契约（6 条路由）挂到 FastAPI ``app`` 上。

    ``app.py`` 在静态兜底路由之前调用本函数；``v3_run`` 与 ``home`` 都是注入进来的，
    因此这里没有任何模块级全局状态，同一进程挂两次也不会互相污染（各自闭包独立）。
    """

    @app.get("/api/v3/risk/analytics")
    async def v3_risk_analytics(limit: int = 250, confidence: float = 0.95,
                                benchmark: str = "SH.000300",
                                weights: Optional[str] = None):
        """组合风险量：history-simulation VaR/CVaR + Beta/Alpha/IR + Kupiec POF + 净值曲线。"""
        def work():
            return risk_analytics(v3_run, home, limit=limit, confidence=confidence,
                                  benchmark=benchmark, weights_raw=weights)

        return _ok(await asyncio.to_thread(work))

    @app.get("/api/v3/factors/matrix")
    async def v3_factors_matrix(tickers: Optional[str] = None, factor: str = "mom_20",
                                forward_days: Optional[int] = None,
                                forward: Optional[int] = None):
        """横截面因子 z 矩阵 + 因子 IC 序列（``forward`` 与 ``forward_days`` 都接受，缺省 5）。"""
        chosen_forward = forward if forward is not None else forward_days

        def work():
            return factors_matrix_data(v3_run, home, tickers_raw=tickers, factor=factor,
                                       forward_days=chosen_forward if chosen_forward is not None else 5)

        return _ok(await asyncio.to_thread(work))

    @app.get("/api/v3/strategy")
    async def v3_strategy_show():
        """最近一轮研究流水线（从未运行过 → ``run=null`` + 说明）。"""
        return _ok(await asyncio.to_thread(strategy_last, home))

    @app.post("/api/v3/strategy/run")
    async def v3_strategy_run(request: Request):
        """跑一轮 PDAT→PET 流水线并落盘；**不下单**，产物只是调仓建议提案。"""
        payload = await _read_json_body(request)
        if payload is None:
            return _ok(_error("bad-request", "请求体需为合法 JSON 对象（topN/universe/window）"))
        return _ok(await asyncio.to_thread(strategy_run, v3_run, home, payload))

    @app.get("/api/v3/ml/sweep")
    async def v3_ml_sweep(ticker: str = "SH.600519", windows: str = "10,20,30,60",
                          rebalance: str = "5,10,20", limit: int = 500):
        """动量策略参数网格（真实日 K 回测），``best`` 取 Sharpe 最大的有效格。"""
        def work():
            return ml_sweep(v3_run, ticker=ticker, windows_raw=windows,
                            rebalance_raw=rebalance, limit=limit)

        return _ok(await asyncio.to_thread(work))

    @app.post("/api/v3/ml/backtest")
    async def v3_ml_backtest(request: Request):
        """单标的动量 long/flat 回测（PIT）；数据不足 → ``backtest/insufficient``。"""
        payload = await _read_json_body(request)
        if payload is None:
            return _ok(_error("bad-request", "请求体需为合法 JSON 对象（ticker/window/rebalanceDays/limit）"))
        return _ok(await asyncio.to_thread(ml_backtest, v3_run, payload))
