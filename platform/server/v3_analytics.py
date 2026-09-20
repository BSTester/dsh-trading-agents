"""V3 控制台**分析类**接口：风险量 / 因子矩阵 / 策略流水线 / 回测与参数扫描。

接线（由 ``server/app.py`` 负责，本模块不自启）：``app.py`` 的 V3 子模块自动接线循环会
``import server.v3_analytics`` 并调用 ``register(app, v3_run, home)``——位置固定在静态兜底
路由之前，因此这里注册的路径先于 ``/{path:path}`` 命中。

``v3_run(name, payload)`` 是主 agent 注入的**同步**回调：按名调用既有 56 工具面的同一份
``handle``，返回原始信封 ``{ok, value|error}``，契约上不抛异常。取数一律经它，本模块
**不新造第二事实源**，也不直连任何外部数据源（唯一的本地持久化是策略流水线结果——自
2026-09-20 起主存 ``server.v3_db`` 的 SQLite 表，同一份再追加 JSONL 冷备；另读自选池配置
``trading-platform.json``，见下）。

纪律（与 ``docs/v3-integration.md`` §五 一致）：
  * 阻塞取数（``v3_run`` → 子进程）全部在 ``asyncio.to_thread`` 里执行，handler 不阻塞事件循环；
  * 任何工具/外部失败都转成 ``{ok: false, error: {code, message}}`` 信封，**不抛 500**；
  * 数值一律实测：取不到就 ``null``/错误码 + 说明，**绝不用估算值顶替**；
  * 只读：不下单、不改单、不切模式。策略流水线的产物只是「提案」，执行仍走工作台受约束入口。
  * **市场口径（2026-09-20）**：``risk/analytics`` / ``factors/matrix`` / ``strategy`` 支持
    ``market=SH|HK|US``——池子统一来自 ``server.v3_universe.resolve_universe``（配置
    ``watchlists.<market>`` → 富途真实持仓 ``account_positions``），该市场无宇宙则
    ``market/no-universe``（不退回全部市场、不发明自选池）。``risk/analytics`` 与
    ``factors/matrix`` 的路由缺省 ``SH``（保持既有 A 股口径）；``strategy`` 不传 ``market``
    时不过滤（与历史一致），``POST strategy/run`` 把 ``market`` 记进落盘记录。

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
``/api/v3/ml/models``            GET     ML 策略族（Lasso/GBDT/MLP）与动量基线同口径评估
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

from server import v3_db, v3_math, v3_ml, v3_universe

__all__ = [
    "STRATEGY_RUNS_FILE",
    "factors_matrix_data",
    "ml_backtest",
    "ml_models",
    "ml_sweep",
    "read_last_strategy_run",
    "read_watchlist",
    "register",
    "risk_analytics",
    "strategy_last",
    "strategy_markets",
    "strategy_run",
]

# 策略流水线结果：一轮一行 JSONL（`GET /api/v3/strategy` 返回最后一轮）。
STRATEGY_RUNS_FILE = "v3-strategy-runs.jsonl"

# 工作台 plan 里可作为组合定义的权重口径：单笔目标权重上限（%，与 V3 原型同值）。
SINGLE_NAME_LIMIT_PCT = 2.0

# 自选池等权组合的标的数上限（参考实现 portfolio.resolve 的 limit=8）。
WATCHLIST_LIMIT = 8
# 因子横截面矩阵/IC 的标的数上限（参考实现 slice(0, 8)）。
FACTOR_LIMIT = 8
# 因子矩阵缺省标的数（自选池前 6）。
FACTOR_DEFAULT_COUNT = 6
# ML 策略族缺省标的数（该市场宇宙前 6）：池化样本越多越稳，但取数受全局限流器约束。
ML_UNIVERSE_LIMIT = 6
# ML 特征回看窗口缺省（与动量基线的窗口语义一致）。
ML_WINDOW = 20
# ML 标签前瞻期缺省（t 日特征 → t+horizon 收益）。
ML_HORIZON = 1


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
    """追加一轮流水线结果：**主存 SQLite**（``strategy_runs`` 表），同一份再追加 JSONL 冷备。

    返回 ``None`` 或错误字典（调用方如实回传）。只有**两个存储都失败**才算落盘失败——
    库成功就说明这一轮读得回来（读路径优先库、库空才回退文件）。
    """
    db_error = None
    try:
        v3_db.append_event(home, "strategy_runs", run)
    except Exception as error:  # noqa: BLE001 —— 数据库失败不能阻断既有文件落盘
        db_error = error
    file_error = None
    try:
        path = strategy_runs_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(run, ensure_ascii=False, allow_nan=False) + "\n")
    except (OSError, ValueError) as error:
        file_error = error
    if db_error is None or file_error is None:
        return None
    return {"code": "strategy/persist-failed",
            "message": f"sqlite: {db_error}; file: {file_error}"[:300]}


def _strategy_run_records(home):
    """策略轮记录（按写入顺序）+ 来源标注。

    主源 = SQLite（``v3_db``，含迁移进来的历史记录）；库为空/不可用 → **回退只读**
    ``<home>/v3-strategy-runs.jsonl``（冷备，迁移后仍保留）。回退保证既有行为不变：
    手写文件、旧实例、库被删掉都能照读。
    """
    try:
        records = v3_db.list_events(home, "strategy_runs", limit=None, order="asc")
    except Exception:  # noqa: BLE001 —— 数据库不可用按「库为空」处理，绝不 500
        records = []
    if records:
        return records, f"sqlite:{v3_db.db_path(home)}#strategy_runs（冷备 {strategy_runs_path(home)}）"
    path = strategy_runs_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], str(path)
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out, str(path)


def read_last_strategy_run(home, market=None):
    """读最后一轮流水线结果；库与冷备都没有有效记录 → ``None``（从未运行过）。

    ``market`` 给定时只认 ``run.market`` 等于该市场的记录（旧记录没有 ``market`` 字段，
    因此**不会被当成该市场的记录**——调用方用 :func:`strategy_markets` 如实说明有哪些市场）。
    """
    records, _source = _strategy_run_records(home)
    for value in reversed(records):
        if market is None:
            return value
        label = str(value.get("market") or "").strip().upper()
        if label == market:
            return value
    return None


def strategy_markets(home):
    """落盘记录里出现过的市场标注（保序、去重；旧记录无标注 → ``None``）。"""
    records, _source = _strategy_run_records(home)
    labels = []
    for value in records:
        label = value.get("market")
        label = str(label).strip().upper() if label not in (None, "") else None
        if label not in labels:
            labels.append(label)
    return labels


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


def _resolve_portfolio(v3_run, home, explicit, market=None):
    """组合定义：显式权重 → 工作台 frozen 多标的计划 → **该市场宇宙** → 自选池等权。

    返回 ``(weights, source, universe_source, error_code)``：

      * ``weights`` 为空时 ``source`` 是真实原因，``error_code`` 为 ``None``
        （``risk/no-portfolio``）或 ``"market/no-universe"``（market 给定但两级来源都空；
        此时 ``source`` 里带 ``v3_universe`` 的真实原因，供 ``detail`` 用）。
      * ``market`` 为 ``None``（不传）时行为与历史完全一致（自选池不过滤）。
    """
    if explicit:
        return dict(explicit), v3_math.SOURCE_EXPLICIT, None, None
    envelope = _call(v3_run, "plan", {})
    value = _value_of(envelope) or {}
    plans = value.get("plans")
    plans = plans if isinstance(plans, list) else []
    if market:
        # 先按市场筛计划：多市场计划并存时，不能因为「第一个 frozen 计划是别的市场」
        # 就漏掉本市场的计划（预筛后再取首个 frozen，语义与历史一致）
        scoped = []
        for plan in plans:
            target = plan.get("target") if isinstance(plan, dict) else None
            if not isinstance(target, dict):
                continue
            if any(v3_universe.market_of_ticker(ticker) == market for ticker in target):
                scoped.append(plan)
        plans = scoped
    weights, source = v3_math.pick_frozen_plan_weights(plans)
    if weights and market:
        filtered = {ticker: weight for ticker, weight in weights.items()
                    if v3_universe.market_of_ticker(ticker) == market}
        if len(filtered) >= 2:
            return filtered, f"{source}（已按 market={market} 过滤）", None, None
        # 该市场的 frozen 计划不足 2 只 → 继续看该市场宇宙（不把别的市场混进来）
    elif weights:
        return weights, source, None, None
    if market:
        universe = v3_universe.resolve_universe(v3_run, home, market)
        if universe is None:
            detail = v3_universe.universe_note(home, market) or ""
            return (None, f"该市场没有配置自选池、也没有真实持仓"
                          + (f"（{detail}）" if detail else ""), None, "market/no-universe")
        names = list(universe.get("tickers") or [])[:WATCHLIST_LIMIT]
        equal, equal_source = v3_math.watchlist_equal_weights(names, WATCHLIST_LIMIT)
        if equal:
            # 配置自选池时沿用既有文案（portfolioSource 口径不变）；池子来自真实持仓
            # （没有配置池）时把来源写进文案，避免把持仓宇宙说成「自选池」
            source_text = str(universe.get("source") or "")
            if source_text and not source_text.startswith("config/"):
                equal_source = f"自选池等权（{len(names)} 只，来源 {source_text}）"
            return equal, equal_source, universe.get("source"), None
        return (None, f"该市场宇宙为空（market={market}）", universe.get("source"),
                "market/no-universe")
    weights, source = v3_math.watchlist_equal_weights(read_watchlist(home), WATCHLIST_LIMIT)
    return weights, source, None, None


# ---------------------------------------------------------------------------
# 1) GET /api/v3/risk/analytics
# ---------------------------------------------------------------------------
def risk_analytics(v3_run, home, limit=250, confidence=0.95, benchmark=None,
                   weights_raw=None, market=None):
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

    ``market``（``SH``/``HK``/``US``，路由缺省 ``SH``）：组合＝该市场宇宙等权（或该市场的
    frozen 计划目标），并回显 ``market`` / ``universe_source``；该市场无宇宙 →
    ``market/no-universe``（含 ``detail`` 写明真实原因，不退回全部市场）。``market=None``
    时行为与历史一致（自选池不过滤）——函数级缺省保留给既有调用方与单测。

    ``benchmark``（缺省 ``None``）：显式给定时**原样使用**（不按市场改写）；缺省时
    ``market`` 给定 → 用 ``v3_universe.benchmark_for`` 实测该市场基准（SH.000300 /
    HK.800000 / US.SPY 首选，逐级降级），``market=None`` → 沿用历史缺省 ``SH.000300``。
    该市场基准全部不可用 → **不退回 SH.000300**：``benchmark=null`` + ``benchmarkNote``
    写明候选，beta/alpha/ir 一律 ``null``（绝不拿 A 股基准算港/美股组合）。
    """
    try:
        limit = _clamp_int(limit, 250, 60, 2000)
        level = v3_math.to_float(confidence)
        if level is None:
            return _error("risk/bad-confidence", f"confidence 需为数值，收到 {confidence!r}")
        if not 0.0 < level < 1.0:
            return _error("risk/bad-confidence", f"confidence 必须在 (0,1) 内，收到 {level}")

        code = None
        if market is not None:
            code = v3_universe.normalize_market(market)
            if code is None:
                return _error("market/bad-market", "market 需为 SH / HK / US",
                              market=str(market))

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

        weights, source, universe_source, error_code = _resolve_portfolio(
            v3_run, home, explicit, code)
        if not weights:
            if error_code == "market/no-universe":
                return _error("market/no-universe",
                              "该市场没有配置自选池、也没有真实持仓",
                              market=code, detail=source)
            return _error("risk/no-portfolio", source)
        if len(weights) == 1:
            return _error("risk/single-name",
                          f"组合仅 1 个标的（{source}），单标的组合风险量无横截面意义")

        # ── 基准：显式 > 该市场实测基准 > 历史缺省（仅 market 未指定时）────────────
        benchmark_ticker = str(benchmark or "").strip()
        benchmark_source = None
        if benchmark_ticker:
            benchmark_note = (f"基准由请求显式指定：{benchmark_ticker}"
                              + ("（显式值优先，不按 market 改写）" if code else ""))
        elif code:
            resolved = v3_universe.benchmark_for(code, v3_run)
            if resolved is None:
                benchmark_ticker = ""
                benchmark_note = (v3_universe.benchmark_note(code)
                                  or f"该市场（{code}）基准不可用")
            else:
                benchmark_ticker = resolved["ticker"]
                benchmark_source = resolved.get("source")
                benchmark_note = resolved.get("note") or f"基准取自 {benchmark_ticker}"
        else:
            benchmark_ticker = "SH.000300"
            benchmark_note = "未指定 market：沿用历史缺省基准 SH.000300"

        tickers = list(weights)
        series_map, series_errors, series_sources = _load_series(v3_run, tickers, limit)
        bench_envelope = (_call(v3_run, "series",
                                {"ticker": benchmark_ticker, "period": "1d", "limit": limit})
                          if benchmark_ticker else None)
        bench_ok = bool(bench_envelope and bench_envelope.get("ok"))
        bench_bars = _bars_of(bench_envelope) if bench_ok else None
        if bench_ok and not benchmark_source:
            value = _value_of(bench_envelope) or {}
            benchmark_source = value.get("source") if isinstance(value.get("source"), str) else None
        if not benchmark_ticker:
            benchmark_note += "；beta/alpha/ir 与 benchmarkAnnReturnPct 一律返回 null（不跨市场兜底）"
        elif not bench_ok:
            benchmark_note += f"；该基准取数失败（{_error_message(bench_envelope)}）→ beta/alpha/ir 返回 null"
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

        if bench_ok and analytics.get("beta") is None:
            # 基准取到了但比值仍为空：基准与组合的交易日没对齐（跨市场基准常见，例如拿
            # 沪深 300 给港股组合做基准时两地假期不同），或基准收益方差为 0。如实说明，
            # 不假装算过——口径与 v3_math.portfolio_risk 的实现一致，不改它的契约。
            window = analytics.get("window") or {}
            benchmark_note += (f"；基准取数成功但 beta/alpha/ir 仍为 null：基准与组合的交易日"
                               f"未对齐（组合窗口 {window.get('from')}..{window.get('to')}）"
                               f"或基准收益方差为 0 → 请按市场选基准，勿跨市场混用")

        return {
            "ok": True,
            "market": code,
            "universe_source": universe_source,
            "portfolioSource": source,
            "benchmark": benchmark_ticker or None,
            "benchmarkTicker": benchmark_ticker if bench_ok else None,
            "benchmarkSource": benchmark_source,
            "benchmarkNote": benchmark_note,
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
            "marketNote": (
                f"组合＝market={code} 宇宙（来源 {universe_source or source}）；"
                f"基准 {benchmark_ticker or 'null'}（见 benchmarkNote；beta/alpha/ir 与该基准同源）"
                if code else "未指定 market：组合口径与历史一致（自选池不过滤）"
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


def factors_matrix_data(v3_run, home, tickers_raw=None, factor="mom_20", forward_days=5,
                        market=None):
    """横截面因子矩阵 + 因子 IC 序列。

    契约::

        {ok, matrix: {as_of, source, tickers[], factors[], matrix[][],
                      raw: [{ticker, factors}]},
         ic: {ok, factor, forwardDays, tickers, observations, meanIc, stdIc, ir,
              latestIc, points: [{t, ic}]}}

    标的 2..8：请求显式给（取前 8）→ 否则**该市场宇宙**前 6（``market`` 缺省 SH，
    与 ``/market/watchlist`` 同一份 ``resolve_universe``）→ 不足 2 只 →
    ``market/no-universe``（该市场无池）或 ``factors/too-few``。
    显式 ``tickers=`` 优先，此时 ``market`` **只作标注**（不裁剪请求的标的）。
    矩阵值取 ``factors`` 的 ``z``（缺失格 ``null``）；IC 来自 ``ic`` 工具，缺省
    ``factor=mom_20``、``forward=5``。IC 失败**不拖垮矩阵**：``ic.ok=false`` + ``error``。
    """
    try:
        if factor is None or str(factor).strip() == "":
            factor = "mom_20"
        factor = str(factor).strip()
        forward_days = _clamp_int(forward_days, 5, 1, 250)

        code = None
        if market is not None:
            code = v3_universe.normalize_market(market)
            if code is None:
                return _error("market/bad-market", "market 需为 SH / HK / US",
                              market=str(market))

        if isinstance(tickers_raw, (list, tuple)):
            requested = [str(item).strip() for item in tickers_raw]
        else:
            requested = [item.strip() for item in str(tickers_raw or "").split(",")]
        requested = [item for item in requested if item]

        universe_source = None
        explicit_universe = len(requested) >= 2
        if explicit_universe:
            names = requested[:FACTOR_LIMIT]
        elif code:
            universe = v3_universe.resolve_universe(v3_run, home, code)
            if universe is None:
                detail = v3_universe.universe_note(home, code) or ""
                return _error("market/no-universe",
                              "该市场没有配置自选池、也没有真实持仓",
                              market=code, detail=detail)
            universe_source = universe.get("source")
            names = list(universe.get("tickers") or [])[:FACTOR_DEFAULT_COUNT]
        else:
            names = read_watchlist(home)[:FACTOR_DEFAULT_COUNT]
        if len(names) < 2:
            reason = (f"该市场（{code}）宇宙不足 2 只标的" if code and not explicit_universe
                      else "横截面矩阵需要 2..8 个标的（请求未给 tickers，自选池也不足 2 只）")
            return _error("factors/too-few", reason, market=code)

        envelope = _call(v3_run, "factors", {"tickers": names})
        if not envelope.get("ok"):
            return {"ok": False, "error": _tool_error(envelope), "market": code}
        value = _value_of(envelope) or {}
        rows = value.get("rows")
        matrix = v3_math.factor_matrix(rows if isinstance(rows, list) else [])
        # 工具面逐标的的失败原因照样带出去（矩阵里那一行就是空的，原因不能丢）
        matrix["failures"] = value.get("failures") if isinstance(value.get("failures"), dict) else {}
        return {"ok": True, "market": code, "universe_source": universe_source,
                "market_filter": ("显式 tickers 优先，market 仅作标注" if explicit_universe
                                  else "标的取该市场宇宙"),
                "matrix": matrix,
                "ic": _factor_ic(v3_run, names, factor, forward_days)}
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


# ---------------------------------------------------------------------------
# 3) GET /api/v3/strategy + POST /api/v3/strategy/run
# ---------------------------------------------------------------------------
def strategy_last(home, market=None):
    """最后一轮流水线：从未运行过 → ``{ok:true, run:null, note:'尚未运行研究流水线'}``。

    ``market`` 给定时只返回该市场的那一轮（``run.market``），并带 ``market`` 与 ``filter``
    标注；该市场没有记录 → ``run=null`` + 说明**已落盘的市场**（不臆造）。
    """
    try:
        code = None
        if market is not None:
            code = v3_universe.normalize_market(market)
            if code is None:
                return _error("market/bad-market", "market 需为 SH / HK / US",
                              market=str(market))
        run = read_last_strategy_run(home, market=code)
        if run:
            payload = {"ok": True, "run": run}
            if code:
                payload["market"] = code
                payload["filter"] = f"run.market == {code}"
            return payload
        if code:
            available = [label for label in strategy_markets(home) if label]
            note = (f"market={code} 没有研究流水线记录"
                    + (f"（已落盘的市场：{'/'.join(available)}）" if available
                       else "（文件里没有带 market 标注的记录）"))
            return {"ok": True, "run": None, "market": code,
                    "filter": f"run.market == {code}", "note": note}
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

    ``payload.market``（``SH``/``HK``/``US``）：未给 ``universe`` 时 universe 取该市场宇宙
    （``resolve_universe``），并把 ``market`` 与 ``universe_source`` 记进 run（响应与落盘
    都带）；该市场无宇宙 → ``market/no-universe``。未传 ``market`` 时行为与历史一致。

    与参考实现 ``pipeline.mjs`` 的有意差异（各一处，均为修掉会自相矛盾的边界）：
      1. ``reduces`` 在 ``len(ranked) <= topN`` 时取**空**（参考实现 ``slice(-0)`` 会退化成
         ``slice(0)``，把刚判为「增持」的标的又列进「减持」）；
      2. ``localMomentum`` 输出真正的百分数字符串（参考实现 ``Number('12.34%')`` 恒为 NaN）。
    """
    try:
        payload = payload if isinstance(payload, dict) else {}
        top_n = _clamp_int(payload.get("topN"), 2, 1, 50)
        window = _clamp_int(payload.get("window"), 20, 1, 500)

        code = None
        raw_market = payload.get("market")
        if raw_market not in (None, ""):
            code = v3_universe.normalize_market(raw_market)
            if code is None:
                return _error("market/bad-market", "market 需为 SH / HK / US",
                              market=str(raw_market))

        raw_universe = payload.get("universe")
        if isinstance(raw_universe, (list, tuple)):
            universe = [str(item).strip() for item in raw_universe if str(item or "").strip()]
        elif isinstance(raw_universe, str):
            universe = [item.strip() for item in raw_universe.split(",") if item.strip()]
        else:
            universe = []
        universe_source = None
        if not universe and code:
            resolved = v3_universe.resolve_universe(v3_run, home, code)
            if resolved is None:
                detail = v3_universe.universe_note(home, code) or ""
                return _error("market/no-universe",
                              "该市场没有配置自选池、也没有真实持仓",
                              market=code, detail=detail)
            universe = list(resolved.get("tickers") or [])
            universe_source = resolved.get("source")
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

        summary = {"asOf": as_of, "universe": universe_list, "market": code,
                   "universe_source": universe_source, "stages": stages,
                   "proposals": proposals}
        persist_error = append_strategy_run(home, summary)
        result = {"ok": True, "run": summary, "market": code,
                  "universe_source": universe_source}
        if persist_error:
            result["persistError"] = persist_error
            result["note"] = "流水线已完成，但结果落盘失败（GET /api/v3/strategy 可能读不到这一轮）"
        return result
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


# ---------------------------------------------------------------------------
# 4) GET /api/v3/ml/sweep
# ---------------------------------------------------------------------------
def _market_label(ticker, market=None):
    """单标的端点的 ``market`` 回显：显式请求参数优先，否则取标的自身的市场前缀。

    返回 ``(code|None, note)``——这两个端点是**单标的**口径，market 只作标注（不改变取数）。
    """
    raw = "" if market is None else str(market).strip()
    if raw:
        code = v3_universe.normalize_market(raw)
        if code is None:
            return None, "market 需为 SH / HK / US"
        return code, f"market={code} 由请求参数回显（单标的端点，market 只作标注，不改变取数）"
    code = v3_universe.market_of_ticker(ticker)
    if code:
        return code, f"market={code} 取目标的市场前缀（单标的端点，market 只作标注）"
    return None, "market 无法判定（标的没有市场前缀，且请求未给 market）——如实留空，不猜"


def ml_sweep(v3_run, ticker="SH.600519", windows_raw="10,20,30,60",
             rebalance_raw="5,10,20", limit=500, market=None):
    """参数扫描网格。

    契约::

        {ok, ticker, market, marketNote, grid: [{window, rebalanceDays, sharpe, annReturnPct,
                             maxDrawdownPct, error?}], best: {...} | null}

    ``best`` = Sharpe 最大的**有效**格；全失败 → ``null``。取不到 K 线 → 上游错误信封。
    ``market`` 只作标注（另加 ``market``/``marketNote``，不改既有字段）。
    """
    try:
        ticker = str(ticker or "").strip() or "SH.600519"
        limit = _clamp_int(limit, 500, 60, 2000)
        code, market_note = _market_label(ticker, market)
        if market is not None and str(market).strip() and code is None:
            return _error("market/bad-market", "market 需为 SH / HK / US", market=str(market))
        windows = _int_list(windows_raw, (10, 20, 30, 60), 1, 500, 8)
        rebalance = _int_list(rebalance_raw, (5, 10, 20), 1, 500, 8)
        if not windows or not rebalance:
            return _error("bad-request", "windows/rebalance 需为逗号分隔的正整数（如 10,20,30,60）")

        envelope = _call(v3_run, "series",
                         {"ticker": ticker, "period": "1d", "limit": limit})
        if not envelope.get("ok"):
            return {"ok": False, "error": _tool_error(envelope), "market": code}
        result = v3_math.param_sweep(_bars_of(envelope), windows, rebalance)
        return {"ok": True, "ticker": ticker, "market": code, "marketNote": market_note,
                **result}
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


# ---------------------------------------------------------------------------
# 5) POST /api/v3/ml/backtest
# ---------------------------------------------------------------------------
def ml_backtest(v3_run, payload=None):
    """单标的动量 long/flat 回测（PIT：``t`` 日持仓只由 ``≤ t-1`` 收盘价决定）。

    契约::

        {ok, ticker, market, marketNote,
         metrics: {sharpe, annReturnPct, maxDrawdownPct, signalFlips,
                   heldDays, flatDays, days, winRatePct},
         equity: [{t, value}]}

    指标按**持仓日**基准（空仓日不计入胜率）；数据不足 → ``backtest/insufficient``。
    ``payload.market``（或查询参数）只作标注：显式给出即回显，否则取标的的市场前缀
    （单标的端点，不改取数口径）。
    """
    try:
        payload = payload if isinstance(payload, dict) else {}
        ticker = str(payload.get("ticker") or "").strip()
        if not ticker:
            return _error("bad-request", "ticker 必填")
        raw_market = payload.get("market")
        code, market_note = _market_label(ticker, raw_market)
        if raw_market is not None and str(raw_market).strip() and code is None:
            return _error("market/bad-market", "market 需为 SH / HK / US", market=str(raw_market))
        limit = _clamp_int(payload.get("limit"), 500, 60, 2000)
        window = _clamp_int(payload.get("window"), 20, 1, 500)
        rebalance_days = _clamp_int(payload.get("rebalanceDays"), 5, 1, 500)

        envelope = _call(v3_run, "series",
                         {"ticker": ticker, "period": "1d", "limit": limit})
        if not envelope.get("ok"):
            return {"ok": False, "error": _tool_error(envelope), "market": code}
        result = v3_math.backtest_momentum(_bars_of(envelope), window, rebalance_days)
        if result.get("error"):
            return _error("backtest/insufficient", result["error"], market=code)
        return {"ok": True, "ticker": ticker, "market": code, "marketNote": market_note,
                "metrics": result["metrics"], "equity": result["equity"]}
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


# ---------------------------------------------------------------------------
# 6) GET /api/v3/ml/models
# ---------------------------------------------------------------------------
def _dominant_source(sources):
    """逐标的 ``source`` → 一个代表值；口径不一致时**如实并列**，不挑一个当全部。"""
    values = sorted({str(value) for value in (sources or {}).values() if value})
    if not values:
        return None
    return values[0] if len(values) == 1 else "+".join(values)


def ml_models(v3_run, home, *, market="SH", ticker=None, window=ML_WINDOW,
              horizon=ML_HORIZON, limit=500, cost_bps=0.0):
    """ML 策略族（FR-STRAT-002）：Lasso / GBDT / MLP + 现有动量基线，**同口径**评估。

    契约::

        {ok, market, universe_source, universe_note, source, sources, tickers, failures,
         as_of, generated_at, window, horizon, cost_bps, n_samples, n_train, n_test, split,
         feature_names, models: [{name, impl, kind, params, metrics, metrics_in_sample,
                                  per_ticker, coef_top|first_tree|weight_shapes}],
         baseline: {name: "momentum", impl, params, metrics, metrics_in_sample, per_ticker},
         backends: {sklearn, lightgbm}, notes: [...]}

    口径与纪律：
      * 数据只经既有限流器 ``v3_run("series", {ticker, period: "1d", limit})``（**只读**）；
      * ``ticker`` 缺省用该市场宇宙（``v3_universe.resolve_universe`` 前
        ``ML_UNIVERSE_LIMIT`` 只）——该市场无宇宙 → ``market/no-universe``；
      * **PIT 严格**：特征只用 ``≤ t`` 的收盘价，标签为 ``t+horizon`` 收益；
      * 样本不足（``< v3_ml.MIN_SAMPLES``）→ ``ml/insufficient-sample``
        （``error.detail = {n_samples, required, window, horizon, tickers}``），**不硬跑**；
      * 指标一律**样本外**（时序 70/30 留出），三个模型与基线逐项同口径；
      * 部分标的取数失败**不拖垮**整轮：失败的进 ``failures``（原样带上游错误码），
        其余标照常训练；**全部失败**才回错误信封；
      * ``impl`` 如实标注真实实现（本部署无 sklearn/lightgbm → 恒为 ``numpy-*``）。
    """
    try:
        code = v3_universe.normalize_market(market if market is not None else "SH")
        if code is None:
            return _error("market/bad-market", "market 需为 SH / HK / US", market=str(market))
        window = _clamp_int(window, ML_WINDOW, 2, 500)
        horizon = _clamp_int(horizon, ML_HORIZON, 1, 250)
        limit = _clamp_int(limit, 500, 60, 2000)

        raw_ticker = str(ticker or "").strip()
        universe_source = None
        universe_note = None
        if raw_ticker:
            names = [raw_ticker]
            universe_source = "请求显式 ticker（单标的，不解析市场宇宙）"
        else:
            universe = v3_universe.resolve_universe(v3_run, home, code)
            if universe is None:
                detail = v3_universe.universe_note(home, code) or ""
                return _error("market/no-universe",
                              "该市场没有配置自选池、也没有真实持仓",
                              market=code, detail=detail)
            universe_source = universe.get("source")
            universe_note = universe.get("note")
            names = list(universe.get("tickers") or [])[:ML_UNIVERSE_LIMIT]
        if not names:
            return _error("market/no-universe",
                          f"该市场（{code}）宇宙为空，无法取数", market=code)

        bars_by_ticker = {}
        failures = {}
        sources = {}
        for name in names:
            envelope = _call(v3_run, "series",
                             {"ticker": name, "period": "1d", "limit": limit})
            if not envelope.get("ok"):
                failures[name] = _tool_error(envelope)
                continue
            value = _value_of(envelope) or {}
            bars = value.get("bars")
            bars = bars if isinstance(bars, list) else []
            if not bars:
                failures[name] = {"code": "series/empty", "message": "该标的没有返回日 K"}
                continue
            bars_by_ticker[name] = bars
            if value.get("source"):
                sources[name] = str(value.get("source"))
        if not bars_by_ticker:
            first = failures.get(names[0]) or next(iter(failures.values()))
            return {"ok": False, "error": first, "market": code, "failures": failures}

        try:
            suite = v3_ml.run_model_suite(bars_by_ticker, window=window, horizon=horizon,
                                          cost_bps=cost_bps)
        except v3_ml.InsufficientSample as error:
            return _error("ml/insufficient-sample", str(error),
                          market=code, detail=dict(error.detail))
        return {"ok": True, "market": code,
                "universe_source": universe_source, "universe_note": universe_note,
                "source": _dominant_source(sources), "sources": sources,
                "tickers": sorted(bars_by_ticker.keys()),
                "failures": failures or None, "requested_tickers": list(names),
                **suite}
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
                                benchmark: Optional[str] = None,
                                weights: Optional[str] = None, market: str = "SH"):
        """组合风险量：history-simulation VaR/CVaR + Beta/Alpha/IR + Kupiec POF + 净值曲线。

        ``market`` 缺省 ``SH``（保持既有 A 股口径）；组合＝该市场宇宙等权（或该市场
        frozen 计划目标）；该市场无宇宙 → ``market/no-universe``。``benchmark`` 缺省
        按市场实测选取（SH.000300 / HK.800000 / US.SPY，逐级降级；全不可用 → null 且
        beta/alpha/ir 为 null）；显式给出时原样使用。
        """
        def work():
            return risk_analytics(v3_run, home, limit=limit, confidence=confidence,
                                  benchmark=benchmark, weights_raw=weights, market=market)

        return _ok(await asyncio.to_thread(work))

    @app.get("/api/v3/factors/matrix")
    async def v3_factors_matrix(tickers: Optional[str] = None, factor: str = "mom_20",
                                forward_days: Optional[int] = None,
                                forward: Optional[int] = None, market: str = "SH"):
        """横截面因子 z 矩阵 + 因子 IC 序列（``forward`` 与 ``forward_days`` 都接受，缺省 5）。

        ``market`` 缺省 ``SH``：未给 ``tickers`` 时标的取该市场宇宙（与 watchlist 同一份
        解析）；显式 ``tickers`` 优先（此时 ``market`` 仅作标注）。
        """
        chosen_forward = forward if forward is not None else forward_days

        def work():
            return factors_matrix_data(v3_run, home, tickers_raw=tickers, factor=factor,
                                       forward_days=chosen_forward if chosen_forward is not None else 5,
                                       market=market)

        return _ok(await asyncio.to_thread(work))

    @app.get("/api/v3/strategy")
    async def v3_strategy_show(market: str = ""):
        """最近一轮研究流水线（从未运行过 → ``run=null`` + 说明）。

        ``market`` 缺省不下过滤（与历史一致：返回最后一条记录）；给了就只返回该市场那轮。
        """
        return _ok(await asyncio.to_thread(strategy_last, home, market or None))

    @app.post("/api/v3/strategy/run")
    async def v3_strategy_run(request: Request):
        """跑一轮 PDAT→PET 流水线并落盘；**不下单**，产物只是调仓建议提案。"""
        payload = await _read_json_body(request)
        if payload is None:
            return _ok(_error("bad-request", "请求体需为合法 JSON 对象（topN/universe/window）"))
        return _ok(await asyncio.to_thread(strategy_run, v3_run, home, payload))

    @app.get("/api/v3/ml/sweep")
    async def v3_ml_sweep(ticker: str = "SH.600519", windows: str = "10,20,30,60",
                          rebalance: str = "5,10,20", limit: int = 500, market: str = ""):
        """动量策略参数网格（真实日 K 回测），``best`` 取 Sharpe 最大的有效格。

        ``market`` 只作标注/回显（单标的端点，不改取数口径）。
        """
        def work():
            return ml_sweep(v3_run, ticker=ticker, windows_raw=windows,
                            rebalance_raw=rebalance, limit=limit,
                            market=market or None)

        return _ok(await asyncio.to_thread(work))

    @app.post("/api/v3/ml/backtest")
    async def v3_ml_backtest(request: Request):
        """单标的动量 long/flat 回测（PIT）；数据不足 → ``backtest/insufficient``。

        ``market`` 可来自查询参数或请求体，仅作标注/回显。
        """
        payload = await _read_json_body(request)
        if payload is None:
            return _ok(_error("bad-request", "请求体需为合法 JSON 对象（ticker/window/rebalanceDays/limit）"))
        query_market = (request.query_params.get("market") or "").strip()
        if query_market and not str(payload.get("market") or "").strip():
            payload = {**payload, "market": query_market}
        return _ok(await asyncio.to_thread(ml_backtest, v3_run, payload))

    @app.get("/api/v3/ml/models")
    async def v3_ml_models(market: str = "SH", ticker: str = "", window: int = ML_WINDOW,
                           horizon: int = ML_HORIZON, limit: int = 500,
                           cost_bps: float = 0.0):
        """ML 策略族（Lasso/GBDT/MLP）+ 动量基线的**同口径**样本外评估。

        ``market`` 缺省 ``SH``；``ticker`` 给了就是单标的，否则取该市场宇宙（前 6 只）。
        样本不足 → ``ml/insufficient-sample``；该市场无宇宙 → ``market/no-universe``。
        """
        def work():
            return ml_models(v3_run, home, market=market or "SH", ticker=ticker,
                             window=window, horizon=horizon, limit=limit,
                             cost_bps=cost_bps)

        return _ok(await asyncio.to_thread(work))
