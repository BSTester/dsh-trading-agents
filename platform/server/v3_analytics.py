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
                                        + ``risk_detail``（FR-EXEC-003：杠杆率 / 流动性 /
                                        绩效归因 / 资金检查；``?details=false`` 可关）
``/api/v3/factors/matrix``       GET     横截面因子 z 矩阵（价量 + 估值 + 质量/成长/情绪/
                                        另类）+ 因子 IC + 逐因子覆盖率
``/api/v3/factors/registry``     GET     因子注册表（六类 + 真实数据源 + PIT 口径）+ 覆盖率
``/api/v3/risk/funding-check``   GET     事前风控·资金检查（只读）：订单金额 vs 真实购买力
``/api/v3/strategy``             GET     最近一轮研究流水线结果
``/api/v3/strategy/run``         POST    跑一轮 PDAT→PET 流水线（只出提案）
``/api/v3/ml/sweep``             GET     动量策略参数网格扫描（真实回测；组合硬上限 5000
                                         + 时间预算，``plannedCombos``/``truncated`` 如实）
``/api/v3/ml/backtest``          POST    单标的动量 long/flat 回测（PIT）
``/api/v3/ml/models``            GET     ML 策略族（Lasso/GBDT/MLP）与动量基线同口径评估
``/api/v3/strategies/event-study`` GET   事件驱动策略（FR-STRAT-002；由本模块代挂
                                         ``server.v3_strategies``，真实公告事件 + 持有收益）
``/api/v3/strategies/stat-arb``  GET     统计套利策略（FR-STRAT-002；OLS 对冲比率 +
                                         ADF 近似 p + 价差 z 双腿回测）
===============================  ======  ================================================

新增因子类别（FR-STRAT-001 补全，2026-09-20）与**真实数据源 / PIT 口径**见
:data:`FACTOR_REGISTRY`；本模块**不另造第二事实源**：质量/成长读本地
``trading-data/trading.sqlite`` 的 ``fundamentals``（只认 ``announced_at ≤ as_of`` 的行，
与 ``trading_core.store.read_fundamentals`` 同一 WHERE 条件，只读打开、不跑 migrate），
情绪读 ``sentiment_snapshots`` 原文经 ``server.v3_nlp`` 离线复算，另类走既有富途工具
（``capital_flow_history`` / ``short_interest``）。取不到的标的在矩阵里就是 ``null`` +
``factorsMissing`` 里的原因，**绝不用均值/0 填充**。

**FR-DATA-003（2026-09-20，PIT 收敛）**：上面这些「历史数据读取 + PIT 过滤」的实现细节
（``announced_at <= as_of``、``period_end <= as_of``、``date <= as_of``、日 K 的
``ts <= as_of``）**已全部搬进** ``server.data.cache``（PIT 唯一入口）。本模块只按业务口径
消费它的信封（``as_of`` / ``source`` / ``rows_used`` / ``window`` / 缺失原因），不再自己写
SQL 过滤，也不再自己开只读连接；「哪些路径有意不走 TTL」在 ``server/data/cache.py`` 的
模块 docstring 第三节逐条写明。
"""

import asyncio
import copy
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Request
from starlette.responses import JSONResponse

from server import v3_db, v3_math, v3_ml, v3_universe
# FR-DATA-003：历史数据读取一律经 ``server.data.cache``（PIT 唯一入口）。本模块不再自己
# 写 ``announced_at <= as_of`` / ``date <= as_of`` / ``ts <= today`` 的过滤，也不自己开
# 只读连接——口径与连接方式都收敛到那一个模块。
from server.data import cache as pit_cache

__all__ = [
    "ALIGNMENT_INPUT_LENGTH",
    "ALIGNMENT_NULL_PADDED",
    "SENTIMENT_ORIGIN_LIVE",
    "SENTIMENT_ORIGIN_SNAPSHOT",
    "STRATEGY_RUNS_FILE",
    "FACTOR_REGISTRY",
    "default_news_fetch",
    "factor_registry_data",
    "factors_matrix_data",
    "funding_check_data",
    "liquidity_risk",
    "live_sentiment_factor_value",
    "live_sentiment_values",
    "load_pit_bars",
    "ml_backtest",
    "ml_models",
    "ml_sweep",
    "portfolio_attribution",
    "portfolio_leverage",
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


# ---------------------------------------------------------------------------
# 等长 null 对齐（FR-TOOLS-002 子规范③）
# ---------------------------------------------------------------------------
# 「输出与输入等长，头部窗口位置为 ``null``，模型按索引对齐」——本模块里**时间序列**型
# 输出统一按这条口径收口，并把口径写进响应的 ``alignment`` 字段（模型据此自证对齐方式，
# 不必从文案里猜）。两个取值：
#
# * ``input-length-null-padded``：输出长度 == 输入窗口长度，头部预热/不可知位是 ``null``
#   （**不是 0、也不丢行**）。第一根 K 线没有前值 → 那天没有收益/净值，那一位只能是 null。
# * ``input-length``：本来就是等长的横截面（一行一标的），无需补位，只作标注。
ALIGNMENT_NULL_PADDED = "input-length-null-padded"
ALIGNMENT_INPUT_LENGTH = "input-length"


def _pad_series_head(points, head_stamps, value_key):
    """序列**头部预热位**补 ``null``（等长对齐）：返回 ``[预热位…, 原点…]``。

    两件事一起做到：输出长度 == 输入窗口长度，且预热位取 **``null``**（取值不可知）
    而不是 0——0 会被前端/模型读成「当天收益恰为 0」，是另一种意义上的假数据。
    ``head_stamps`` 是预热位的时间戳（通常只有第一个输入时点一个）；时间戳缺失时照样
    占位（``t=null``），**不丢行**。
    """
    prefix = [{"t": stamp, value_key: None} for stamp in (head_stamps or [])]
    return prefix + list(points or [])


def _first_series_stamp(bars):
    """输入 K 线里**第一根可用 bar** 的时间戳（与 ``v3_math`` 的可用性判定同口径）。

    ``v3_math.backtest_momentum`` 会跳过收盘价非数值的 bar；预热位要对齐的是它实际用到的
    第一根 bar，所以这里用同一条过滤（``to_float(c) is not None``），不另立第二套口径。
    """
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        if v3_math.to_float(bar.get("c")) is not None:
            return bar.get("t")
    return None


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
def load_pit_bars(v3_run, tickers, limit, *, as_of=None, mode=None):
    """并发取多标的日 K 的 **PIT 信封**（每个标的都过 ``pit_cache.read_bars`` 闸门）。

    返回 ``(envelopes, errors)``：``envelopes`` 只含取数成功的标的（完整信封——
    ``window``/``rows_used``/``rejected.future`` 可核验），``errors`` 逐标的带上游错误。
    ``_load_series`` 在其上收窄成 ``(bars, errors, sources)`` 三元组；需要 rejected
    证据的调用方（事件研究 / 统计套利 / 因子矩阵 as_of 路径）直接用本函数。
    """
    envelopes = {}
    errors = []
    names = [str(item or "").strip() for item in (tickers or []) if str(item or "").strip()]
    if not names:
        return envelopes, errors
    as_of = as_of or _series_as_of()
    mode = mode or pit_cache.AS_OF_INCLUSIVE

    def fetch(symbol):
        envelope = _call(v3_run, "series",
                         {"ticker": symbol, "period": "1d", "limit": limit})
        if not envelope.get("ok"):
            error = _tool_error(envelope)
            raise pit_cache.PitSourceError(_error_message(envelope),
                                           code=error.get("code") or "wb/error")
        value = _value_of(envelope) or {}
        bars = value.get("bars")
        return (bars if isinstance(bars, list) else []), (value.get("source") or None)

    def worker(ticker):
        try:
            envelope = pit_cache.read_bars(as_of, mode, symbol=ticker, fetch=fetch,
                                           period="1d", source="futu/series(1d)")
        except pit_cache.PitSourceError as error:
            return ticker, None, str(error)
        return ticker, envelope, None

    workers = max(1, min(8, len(names)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v3-series") as pool:
        results = list(pool.map(worker, names))

    for ticker, envelope, error in results:
        if envelope is None:
            errors.append({"ticker": ticker, "error": error})
            continue
        envelopes[ticker] = envelope
    return envelopes, errors


def _load_series(v3_run, tickers, limit, *, as_of=None, mode=None):
    """并发取多标的日 K（参考实现用 ``Promise.all``；这里并发度上限 8）。

    返回 ``(bars_by_ticker, errors, sources)``：
      * ``bars_by_ticker``：每个标的都有键（失败给空列表），调用方按名取用；
      * ``errors``：``[{"ticker": ..., "error": "<message>"}]``——失败就报，不静默补数；
      * ``sources``：``{ticker: <source 字符串>}``，工具面自报的数据源（如
        ``futu/quote_history_kline``），用于在响应里如实标注取数来源。

    FR-DATA-003 迁移：取到的 K 线**必须**过 ``server.data.cache`` 的 PIT 闸门
    （``as_of`` 缺省 = 今天 UTC，``AS_OF_INCLUSIVE``）。上游工具面的错误码/消息仍旧原样
    带进 ``errors``（经 ``PitSourceError`` 透传），不因为多了一层就吞原因。
    取数本体在 :func:`load_pit_bars`（信封级原语），这里只做形状收窄。
    """
    envelopes, errors = load_pit_bars(v3_run, tickers, limit, as_of=as_of, mode=mode)
    bars_by_ticker = {str(ticker): [] for ticker in (tickers or [])}
    bars_by_ticker.update({ticker: list(envelope.get("bars") or [])
                           for ticker, envelope in envelopes.items()})
    sources = {ticker: envelope.get("source") for ticker, envelope in envelopes.items()
               if isinstance(envelope.get("source"), str) and envelope.get("source")}
    return bars_by_ticker, errors, sources


def _series_as_of():
    """工具面 K 线读数的 ``as_of``（``YYYY-MM-DD``，UTC 当天）。

    ``series`` 工具**不接受** ``as_of``（它返回「到现在为止最近的 N 根」），所以这里取
    请求发起时的 UTC 日期作 PIT 上界：正常的日 K 都在它之内，而任何**晚于今天**的时间戳
    （上游脏数据/时区错位）会被闸门挡掉并计数——这比不过闸门要严格，且对干净数据零影响。
    """
    return datetime.now(timezone.utc).date().isoformat()


def _pit_series_envelope(v3_run, ticker, limit):
    """``series`` 工具信封的 **PIT 版本**：错误分支原样返回上游信封，成功分支的 bars 过闸门。

    迁移前，单标的取数直接 ``_call(v3_run, "series", …)``；现在同一处**必须**经
    ``server.data.cache`` 的 PIT 闸门（``as_of`` = 今天 UTC，``AS_OF_INCLUSIVE``）。
    保持**工具面信封形状**（``{"ok", "value": {"bars", "source"}}`` / ``{"ok": False,
    "error"}``）是刻意的：调用方的 ``_tool_error`` / ``_bars_of`` / ``_error_message``
    三个分支一个字都不用改，上游错误码也不会在迁移中被抹平。
    """
    envelope = _call(v3_run, "series", {"ticker": ticker, "period": "1d", "limit": limit})
    if not envelope.get("ok"):
        return envelope
    value = _value_of(envelope) or {}
    bars = value.get("bars")
    bars = bars if isinstance(bars, list) else []
    visible, _stats = pit_cache.pit_rows(bars, _series_as_of(),
                                         pit_cache.AS_OF_INCLUSIVE, key="t")
    return {"ok": True, "value": {**value, "bars": visible}}


def _kline_source(sources):
    """把逐标的 source 收成一个可展示的字符串；一个都没有 → ``None``（不写死常量）。

    ``sources`` 可能是 ``{ticker: source}``（本模块 ``_load_series`` 的形状）或工具面
    ``factors`` 直接回给我们的 **source 列表**（``bars.py`` 的 ``sources`` 字段）——
    两种形状都认，不因为形状差异把整个响应打成 ``v3/internal``。
    """
    if isinstance(sources, dict):
        values = list(sources.values())
    elif isinstance(sources, (list, tuple, set)):
        values = list(sources)
    else:
        values = []
    unique = sorted({str(value) for value in values if value})
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
                   weights_raw=None, market=None, details=True):
    """组合风险量（可注入 ``v3_run``/``home``，路由只是它的异步外壳）。

    契约（与前端既有实现严格一致）::

        {ok, portfolioSource, benchmarkTicker, nav,
         analytics: {confidence, observations, tickers, window:{from,to},
                     varDailyPct, cvarDailyPct, varAmount, cvarAmount,
                     annVolPct, annReturnPct, maxDrawdownPct,
                     beta, alphaAnnPct, ir, benchmarkAnnReturnPct,
                     kupiec: {lr, pValue, breaches, observations, pass},
                     equityCurve: [{t, v}], method},
         sources: {kline, nav, errors}, navNote,
         risk_detail: {leverage, liquidity, attribution, fundsCheck, errors}}

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

    ``details``（缺省 ``True``）：是否附带 **FR-EXEC-003 补全块** ``risk_detail``——
    杠杆率（真实资金字段推导，缺融资字段即 no-data）/ 流动性风险（订单金额 / 近 20 日 ADV）/
    绩效归因（逐标的 + 行业；因子维度如实缺失）/ 资金检查（``account_funds`` 购买力读数）。
    该块**只读、不改任何既有阈值判定**；``details=False`` 时响应与历史逐字段一致（单测用）。
    任何一条子项失败都只进 ``risk_detail.errors``，绝不影响既有的 VaR/CVaR 主区块。
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
        # FR-DATA-003：基准 K 线同样经 PIT 唯一入口（错误信封原样保留）。
        bench_envelope = (_pit_series_envelope(v3_run, benchmark_ticker, limit)
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
        _align_equity_curve(analytics)

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
            **({"risk_detail": _safe_risk_detail(v3_run, home, code, weights, series_map,
                                                 series_sources, analytics, nav)}
               if details else {}),
        }
    except Exception as error:  # noqa: BLE001 —— 任何内部异常都进信封，绝不 500
        return _error("v3/internal", error)


def _align_equity_curve(analytics):
    """``analytics.equityCurve`` 的**等长 null 对齐**（就地改写，返回同一个 dict）。

    输入 = 对齐后的交易日 D 天（``window.from..to``）；``v3_math.portfolio_risk`` 原来只给
    ``observations = D-1`` 个点（``dates[1..]``）——**第一天的位置被丢掉了**，按索引对齐时
    整体错位一天。这里把第一天作为**预热位**补回来：那天还没有收益（净值从它之后的第一个
    收益日才累乘出来），因此 ``v=null``，不是 0。补位后 ``len(equityCurve) == observations+1``。
    """
    curve = analytics.get("equityCurve")
    window = analytics.get("window") or {}
    if isinstance(curve, list) and curve:
        analytics["equityCurve"] = _pad_series_head(curve, [window.get("from")], "v")
    analytics["alignment"] = ALIGNMENT_NULL_PADDED
    analytics["alignmentNote"] = (
        "equityCurve 等长对齐：一行 = 一个对齐交易日（长度 = observations + 1），"
        "首日没有前值 → v=null（不是 0）；第 2 行起的 v 与对齐交易日 dates[1..] 逐位对应。"
        "**消费方契约**：预热位是「无读数」，按 stat-core 三态口径应显示占位符，"
        "不得用 Number(null)=0 强转成 0（那会把曲线起点画成 0）。"
        "maxDrawdownPct/observations 等指标口径不变（仍按收益序列算）")
    return analytics


def _safe_risk_detail(v3_run, home, code, weights, series_map, series_sources, analytics, nav):
    """``_risk_detail_block`` 的**外层兜底**：补全块再坏也不能把主区块变成错误信封。"""
    try:
        return _risk_detail_block(v3_run, home, code, weights, series_map, series_sources,
                                  analytics, nav)
    except Exception as error:  # noqa: BLE001
        return {"leverage": None, "liquidity": None, "attribution": None,
                "fundsCheck": None,
                "errors": [{"item": "risk_detail", "error": f"{type(error).__name__}: {error}"}],
                "note": "FR-EXEC-003 补全块整体失败 → 四个子项一律 null（主区块不受影响）"}


def _risk_detail_block(v3_run, home, code, weights, series_map, series_sources, analytics, nav):
    """FR-EXEC-003 补全块的装配（**只读**）：杠杆率 / 流动性 / 归因 / 资金检查。

    每一项都**独立失败**：异常只进 ``errors``，不拖垮其他项，也不改既有 VaR 主区块。
    订单金额口径：单笔金额用一个可解释的**名义单** —— 组合等权目标权重下、按
    单笔上限（``v3_ops.LIMITS['singlePct']``，缺省 2%）计的名义订单额；资金检查与流动性
    都按它算（页面/响应里写明是「名义单」而不是真实待执行订单，避免读成真实委托）。
    """
    errors = []
    detail = {"nominalOrderNote": None, "leverage": None, "liquidity": None,
              "attribution": None, "fundsCheck": None}

    equity_for_nominal = nav if isinstance(nav, (int, float)) and nav > 0 else None
    nominal = None
    if equity_for_nominal:
        nominal = equity_for_nominal * 0.02
        detail["nominalOrderNote"] = (
            f"名义单金额 = NAV {equity_for_nominal} × 单笔上限 2%（v3_ops.LIMITS.singlePct）"
            f"= {v3_math.round_half_up(nominal, 2)}；本块是**读数 + 分级建议**，不代表任何真实委托")
    else:
        detail["nominalOrderNote"] = ("NAV 不可用 → 名义单金额为 null，流动性/资金检查两项"
                                      "各按其缺失原因返回 null（不估算）")

    funds_envelope = _call(v3_run, "account_funds", {})
    positions_envelope = _call(v3_run, "positions", {})

    # ① 杠杆率
    try:
        detail["leverage"] = portfolio_leverage(v3_run, market=code,
                                                funds_envelope=funds_envelope,
                                                positions_envelope=positions_envelope)
    except Exception as error:  # noqa: BLE001
        errors.append({"item": "leverage", "error": f"{type(error).__name__}: {error}"})
        detail["leverage"] = {"error": {"code": "leverage/internal", "message": str(error)},
                             "leverage_ratio_pct": None,
                             "note": "杠杆率计算内部异常 → null（不估算）"}

    # ② 流动性风险：取组合里权重最大的标的（名义单打在该标的上）
    try:
        target = None
        if series_map:
            target = max(weights, key=lambda ticker: v3_math.to_float(weights[ticker]) or 0.0)
        bars = list(series_map.get(target) or []) if target else []
        detail["liquidity"] = liquidity_risk(nominal, bars, market=code, ticker=target,
                                             order_note=detail["nominalOrderNote"])
        detail["liquidity"]["portfolioOrderValue"] = None if nominal is None else \
            v3_math.round_half_up(nominal, 2)
        if not target:
            detail["liquidity"]["reason"] = "组合里没有可用的 K 线 → 无法算 ADV"
    except Exception as error:  # noqa: BLE001
        errors.append({"item": "liquidity", "error": f"{type(error).__name__}: {error}"})
        detail["liquidity"] = {"grade": "no-data", "participation_pct": None,
                               "reason": f"流动性计算内部异常：{error}"}

    # ③ 绩效归因（逐标的 + 行业；行业映射由调用方按需注入 —— 这里只做逐标的 + 缺失说明）
    try:
        detail["attribution"] = portfolio_attribution(positions_envelope, market=code)
    except Exception as error:  # noqa: BLE001
        errors.append({"item": "attribution", "error": f"{type(error).__name__}: {error}"})
        detail["attribution"] = {"byTicker": [], "byIndustry": None, "byFactor": None,
                                 "error": {"code": "attribution/internal", "message": str(error)}}

    # ④ 资金检查
    try:
        detail["fundsCheck"] = funding_check_data(
            v3_run, order_value=nominal, market=code, funds_envelope=funds_envelope)
        detail["fundsCheck"]["orderValueSource"] = detail["nominalOrderNote"]
    except Exception as error:  # noqa: BLE001
        errors.append({"item": "fundsCheck", "error": f"{type(error).__name__}: {error}"})
        detail["fundsCheck"] = {"ok": False, "action": "unknown", "orderValue": nominal,
                                "reason": f"资金检查内部异常：{error}"}

    detail["errors"] = errors
    detail["sources"] = {
        "leverage": (detail["leverage"] or {}).get("source"),
        "liquidity": ((detail["liquidity"] or {}).get("adv_source")),
        "attribution": (detail["attribution"] or {}).get("source"),
        "fundsCheck": (detail["fundsCheck"] or {}).get("source"),
        "kline": _kline_source(series_sources),
    }
    detail["note"] = ("FR-EXEC-003 补全块：杠杆率=持仓市值/总资产（上游无融资负债字段 → 该项 null）；"
                      "流动性=名义单金额/近 20 日 ADV；归因=券商持仓未实现盈亏（因子维度缺 PIT 敞口）；"
                      "资金检查=订单金额 vs account_funds 购买力。全部只读，不参与也不改既有下单前闸门。")
    return detail


# ---------------------------------------------------------------------------
# 2) GET /api/v3/factors/matrix
# ---------------------------------------------------------------------------
def _aligned_ic_points(raw_points):
    """工具面 ``ic`` 的 ``points`` → **等长** points（一行输入一行输出，取不到的位 ``null``）。

    ``v3_math.ic_stats`` 原来会「丢掉非数值行 + 只留最近 40 个点」，两者都让输出比输入短：
    模型按索引对齐时会静默错位（第 0 个点不再是第一期）。这里按 FR-TOOLS-002 的等长 null
    对齐重建：**一行一期、原序不动**，``ic`` 解析不出来（``null``/非数值）的位就是 ``null``
    ——不是 0，也不丢行；非 dict 的畸形元素照样占位（``t=null``），长度因此恒等于输入长度。
    统计量（``observations`` / ``meanIc`` / ``stdIc`` / ``ir`` / ``latestIc``）仍只按**有值**
    的期数算，口径不变。
    """
    aligned = []
    for point in raw_points if isinstance(raw_points, list) else []:
        if not isinstance(point, dict):
            aligned.append({"t": None, "ic": None})
            continue
        number = v3_math.to_float(point.get("ic"))
        aligned.append({"t": point.get("t"),
                        "ic": None if number is None else v3_math.round_half_up(number, 4)})
    return aligned


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
    value = _value_of(envelope) or {}
    stats = v3_math.ic_stats(value, factor, forward_days, fallback_tickers=tickers)
    stats["points"] = _aligned_ic_points(value.get("points"))
    stats["alignment"] = ALIGNMENT_NULL_PADDED
    stats["alignmentNote"] = (
        f"points 等长对齐：长度 = 工具面 ic 的期数（{len(stats['points'])} 期，原序），"
        "ic 取不到的期是 null（不是 0）；不再截尾 40 期、不再丢弃非数值行。"
        "observations/meanIc/stdIc/ir/latestIc 仍只按有值的期数算")
    return stats


def factors_matrix_data(v3_run, home, tickers_raw=None, factor="mom_20", forward_days=5,
                        market=None, classes=None, as_of=None, include_sentiment=True,
                        live_sentiment=False, sentiment_fetch=None):
    """横截面因子矩阵 + 因子 IC 序列 + **新四类因子**（FR-STRAT-001 补全）。

    契约::

        {ok, market, universe_source, market_filter,
         matrix: {as_of, source, tickers[], factors[], matrix[][],
                  raw: [{ticker, factors}], failures, alignment, alignmentNote},
         factors: [{key, class, classLabel, source, pit, direction, covered, total,
                    coveragePct, tickers, missingTickers}],
         factorsMissing: [{key, reason}],
         sentimentSources: {ticker: {origin, score, source, reason}},
         sources: {kline, quality, growth, sentiment, alternative},
         ic: {ok, factor, forwardDays, tickers, observations, meanIc, stdIc, ir,
              latestIc, points: [{t, ic}], alignment, alignmentNote}}

    标的 2..8：请求显式给（取前 8）→ 否则**该市场宇宙**前 6（``market`` 缺省 SH，
    与 ``/market/watchlist`` 同一份 ``resolve_universe``）→ 不足 2 只 →
    ``market/no-universe``（该市场无池）或 ``factors/too-few``。
    显式 ``tickers=`` 优先，此时 ``market`` **只作标注**（不裁剪请求的标的）。
    矩阵值取 ``factors`` 的 ``z``（缺失格 ``null``）；IC 来自 ``ic`` 工具，缺省
    ``factor=mom_20``、``forward=5``。IC 失败**不拖垮矩阵**：``ic.ok=false`` + ``error``。

    ``classes``：要并入矩阵的新因子类别（``quality`` / ``growth`` / ``sentiment`` /
    ``alternative``，逗号分隔或列表；``all`` = 四类全要，缺省）。价量 + 估值列（工作台
    ``factors`` 的 z）**恒定保留**——历史调用零改动。

      * 质量（``gross_margin`` / ``net_margin``）与成长（``revenue_yoy`` / ``net_profit_yoy``）：
        读本地交易库 ``fundamentals``，**只认 ``announced_at ≤ as_of`` 的行**（PIT）；
        缺公告日、缺同期基期的因子在矩阵里就是 ``null``，原因进 ``factorsMissing``。
      * 情绪（``sentiment``）：**快照 ∨ 实时**——``store.sentiment_snapshots`` 的落库原文经
        ``v3_nlp`` 离线复算（PIT ≤ ``as_of``）；该标的快照取不到分时，``live_sentiment=True``
        下再走 ``v3_nlp`` **实时**打分（既有 ``akshare/stock_news_em`` 通道取资讯 → 经
        ``server.data.cache`` 的 PIT 闸门，显式 ``as_of`` + INCLUSIVE → 同一份
        ``score_documents``；见 :func:`live_sentiment_factor_value`）。两条通道的读数**逐标的
        标注来源**（``snapshot`` / ``live:nlp-v1`` / 取不到 = ``null`` + 原因，
        见响应 ``sentimentSources``），绝不混为一谈、也绝不填 0。
      * 另类（``capital_flow`` / ``short_interest``）：既有富途工具**实时**取数（受全局限流
        约束，故**缺省不并入**——需要时显式 ``classes=alternative`` 或 ``classes=all``）。
        A 股无卖空数据是上游事实，进 ``factorsMissing`` 而不是填 0。

    ``live_sentiment``（缺省 ``False``）：情绪因子的**实时兜底**开关。缺省关是为了让内部
    流水线与单测保持零网络；``/api/v3/factors/matrix`` 与 ``/api/v3/factors/registry``
    两条只读路由显式打开（并可用 ``sentiment_fetch`` 注入假资讯源做单测）。

    新因子一律作为**横截面 z**（截断 ±3）并入 ``matrix.matrix``，并按
    :data:`FACTOR_REGISTRY` 的 ``direction`` 参与 ``strategy_run`` 的扩展综合分；
    ``as_of`` 显式给出时用它做 PIT 上界（缺省 = 今天，UTC）。

    **as_of 模式**：``as_of`` 显式给出时，价量列保留独立 PIT 复算路径：
    K 线经 ``server.data.cache``（显式 ``as_of`` + INCLUSIVE）读取 →
    ``v3_math.price_volume_factor_values``（与 workbench factors.py 同公式）本地复算 →
    横截面 z 同口径打分。响应里 ``matrix.as_of`` 是**价量列实际日期**，
    ``asOf`` 是本地因子 PIT 上界，两者并列；不一致时 ``priceVolumePit.asOfNote``
    解释。实时估值列与另类列在该模式**不并入**（不可 PIT，不冒充）；IC 同理跳过
    （``ic.skipped`` + ``ic/as-of-unsupported``）。不传 ``as_of`` 时行为与历史逐字段一致。
    """
    try:
        if factor is None or str(factor).strip() == "":
            factor = "mom_20"
        factor = str(factor).strip()
        forward_days = _clamp_int(forward_days, 5, 1, 250)
        try:
            pit_date = _pit_date(as_of)
        except pit_cache.PitError as error:
            return _error("factors/bad-as-of", str(error))

        wanted = _factor_classes(classes)
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

        explicit_as_of = as_of not in (None, "")
        if explicit_as_of:
            price_volume = _price_volume_rows_pit(v3_run, names, pit_date)
            base_rows = price_volume["rows"]
            price_volume_pit = price_volume["pit"]
            failures = price_volume["failures"]
            kline_source = price_volume["source"]
        else:
            envelope = _call(v3_run, "factors", {"tickers": names})
            if not envelope.get("ok"):
                return {"ok": False, "error": _tool_error(envelope), "market": code}
            value = _value_of(envelope) or {}
            rows = value.get("rows")
            base_rows = [copy.deepcopy(row) for row in (rows if isinstance(rows, list) else [])
                         if isinstance(row, dict)]
            price_volume_pit = None
            failures = value.get("failures") if isinstance(value.get("failures"), dict) else {}
            kline_source = _kline_source(value.get("sources"))
        matrix = v3_math.factor_matrix(base_rows)
        # 工具面逐标的的失败原因照样带出去（矩阵里那一行就是空的，原因不能丢）
        matrix["failures"] = failures if isinstance(failures, dict) else {}
        if explicit_as_of:
            matrix["source"] = (f"data.cache/pit-bars（as_of={pit_date}，INCLUSIVE）+ "
                                "v3_math 价量公式本地复算 z")
        wanted = _factor_classes(classes)
        drop_alternative_note = None
        if explicit_as_of and "alternative" in wanted:
            # 实时另类因子在该模式下不可 PIT → 不并入（缺原因写清，不拿今天的实时数冒充）。
            wanted.discard("alternative")
            drop_alternative_note = ("as_of 模式下实时另类因子（capital_flow/short_interest）"
                                     "没有 PIT 口径，未并入矩阵")
        extras, missing, sources, sentiment_report = _factor_extras(
            v3_run, home, names, base_rows, wanted, pit_date, code,
            include_sentiment=include_sentiment, live_sentiment=live_sentiment,
            sentiment_fetch=sentiment_fetch)
        if drop_alternative_note:
            missing.append({"key": "capital_flow/short_interest",
                            "reason": drop_alternative_note})
        # 只并入**真有读数**的因子的列：一个标的都没取到的因子不进矩阵（它照样出现在
        # 顶层 ``factors`` 覆盖率与 ``factorsMissing`` 里 —— 缺席，不是 0，也不是均值）。
        extras = {key: values for key, values in extras.items() if values}
        if extras:
            _merge_factor_extras(matrix, base_rows, extras)
            matrix["factors"] = sorted({str(key) for key in matrix.get("factors", [])}
                                       | {str(key) for key in extras})
            matrix["matrix"] = _project_matrix(base_rows, matrix["factors"])
        matrix["raw"] = [{"ticker": row.get("ticker"),
                          "factors": dict(row.get("factors") or {})}
                         for row in base_rows]
        # 等长对齐（FR-TOOLS-002）：矩阵是**横截面**——``matrix.raw`` / ``matrix.matrix``
        # 一行一标的，与 ``matrix.tickers`` 等长；缺数据的格是 ``null``（不是 0）。这一路
        # 本来就等长，只把口径写进响应（时间序列型的 ``ic.points`` 另有 null 补位口径）。
        matrix["alignment"] = ALIGNMENT_INPUT_LENGTH
        matrix["alignmentNote"] = (
            f"横截面等长对齐：{len(matrix.get('raw') or [])} 行 = {len(names)} 只标的"
            "（请求显式 tickers 或该市场宇宙前 8），一行一标的、顺序同 matrix.tickers；"
            "取不到的格是 null（不是 0）")
        registry = factor_registry_data(matrix)
        if explicit_as_of:
            # 响应口径并列且诚实：``matrix.as_of`` = 价量列实际日期（逐标的最后一根可见
            # bar 的最大值）；``asOf`` = 本地因子（质量/成长/情绪）PIT 上界。两者一致时
            # 说明价量列就在该时点；不一致（停牌/缺数）时 asOfNote 解释差在哪。
            price_volume_date = matrix.get("as_of")
            as_of_note = None
            if price_volume_date != pit_date:
                as_of_note = (f"价量列实际日期 {price_volume_date} 早于 PIT 上界 {pit_date}"
                              "（部分标的在该时点没有可见 bar：停牌/缺数——缺的就是缺，"
                              "不向后填补）")
        else:
            price_volume_date = matrix.get("as_of")
            as_of_note = None
        if explicit_as_of:
            # IC 工具面同样不接受 as_of（同一白名单约束）：该模式下不返回用未来数据算的
            # IC，如实给 skipped + 原因，而不是冒充。
            ic_block = {"ok": False, "factor": factor, "forwardDays": forward_days,
                        "skipped": True,
                        "error": {"code": "ic/as-of-unsupported",
                                  "message": "显式 as_of 下不返回 IC：工作台 ic 工具面"
                                             "不支持 as_of（payload 白名单在 app.py），"
                                             "返回最新数据口径的 IC 会构成前视"}}
        else:
            ic_block = _factor_ic(v3_run, names, factor, forward_days)
        response = {"ok": True, "market": code, "universe_source": universe_source,
                    "market_filter": ("显式 tickers 优先，market 仅作标注" if explicit_universe
                                      else "标的取该市场宇宙"),
                    "asOf": pit_date, "classes": sorted(wanted),
                    "matrix": matrix,
                    "factors": registry["coverage"]["factors"],
                    "factorsMissing": missing,
                    # 情绪因子的**逐标的来源**：snapshot（落库快照）/ live:nlp-v1（实时打分）/
                    # null（取不到 + 原因）。快照与实时两条通道因此可逐标的核对，不混为一谈。
                    "sentimentSources": sentiment_report,
                    "sources": {**{"kline": kline_source}, **sources},
                    "ic": ic_block}
        if explicit_as_of:
            response["priceVolumePit"] = {**price_volume_pit,
                                          "actualDate": price_volume_date,
                                          "asOfNote": as_of_note}
        return response
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


#: 价量列 as_of 复算的取数根数（65 根下限 + 富余，够 mom_60/mdd_60）。
PRICE_VOLUME_BARS = 260


def _price_volume_rows_pit(v3_run, names, pit_date):
    """as_of 模式的价量因子行：K 线过 ``data.cache`` PIT 闸门后按工作台同公式复算。

    返回 ``{rows, failures, pit, source}``：``rows`` 与工作台 ``factors`` 工具的行同形
    （``{ticker, factors, as_of}``，z 由调用方按横截面补），``pit`` 带被闸门挡掉的
    未来行计数（可核验的「没有未来数据」证据）。
    """
    envelopes, errors = load_pit_bars(v3_run, names, PRICE_VOLUME_BARS,
                                      as_of=pit_date, mode=pit_cache.AS_OF_INCLUSIVE)
    rows = []
    failures = {}
    rejected_future = 0
    rejected_undated = 0
    windows = {}
    for ticker in names:
        envelope = envelopes.get(ticker)
        if envelope is None:
            detail = next((item.get("error") for item in errors
                           if item.get("ticker") == ticker), None)
            failures[ticker] = f"K 线取数失败：{detail}"
            continue
        rejected_future += int((envelope.get("rejected") or {}).get("future") or 0)
        rejected_undated += int((envelope.get("rejected") or {}).get("undated") or 0)
        bars = list(envelope.get("bars") or [])
        if not bars:
            failures[ticker] = (envelope.get("missing") or {}).get("reason") or "无可见 bar"
            continue
        windows[ticker] = envelope.get("window")
        closes = [v3_math.to_float(bar.get("c")) for bar in bars]
        volumes = [v3_math.to_float(bar.get("v")) for bar in bars]
        values = v3_math.price_volume_factor_values(closes, volumes)
        if values is None:
            failures[ticker] = f"可见 bar 不足 65 根（{len([c for c in closes if c])}）"
            continue
        rows.append({"ticker": ticker,
                     "factors": {key: (v3_math.round_half_up(value, 5)
                                       if isinstance(value, float) else value)
                                 for key, value in values.items()},
                     "as_of": bars[-1].get("t")})
    # 横截面 z（n-1 样本 std、截断 ±3）——与工作台 zscores / _cross_sectional_z_map 同口径。
    for key in ("mom_20", "mom_60", "vol_20", "trend", "rsi_14", "liq_ratio", "mdd_60"):
        raw = {row["ticker"]: v3_math.to_float(row["factors"].get(key)) for row in rows}
        raw = {ticker: value for ticker, value in raw.items() if value is not None}
        if len(raw) < 2:
            continue
        by_ticker = _cross_sectional_z_map(raw)
        for row in rows:
            z = by_ticker.get(row["ticker"])
            if z is not None:
                row["z"] = {**row.get("z", {}), key: z}
    return {
        "rows": rows,
        "failures": failures,
        "pit": {"mode": "pit-recompute", "asOfGate": pit_date,
                "semantics": pit_cache.semantics_text(pit_cache.AS_OF_INCLUSIVE, pit_date),
                "rejectedFuture": rejected_future, "rejectedUndated": rejected_undated,
                "windows": windows},
        "source": (f"data.cache/pit-bars（as_of={pit_date}，INCLUSIVE）→ "
                   "v3_math.price_volume_factor_values 本地复算"),
    }


#: 允许的因子类别（``classes=`` 参数取值）。``price`` 不是可选项——价量列恒定存在。
FACTOR_CLASS_KEYS = ("quality", "growth", "sentiment", "alternative")


def _factor_classes(raw):
    """``classes`` 参数 → 类别集合。``all``/空 → 除 ``alternative`` 外的全部（见 docstring）。"""
    if raw in (None, "", []):
        return {"quality", "growth", "sentiment"}
    items = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
    text = [str(item).strip().lower() for item in items if str(item).strip()]
    if not text or "all" in text:
        return set(FACTOR_CLASS_KEYS)
    if "none" in text or "price" in text and len(text) == 1:
        return set()
    return {item for item in text if item in FACTOR_CLASS_KEYS}


def _pit_date(as_of):
    if as_of in (None, ""):
        return datetime.now(timezone.utc).date().isoformat()
    if not isinstance(as_of, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of) is None:
        raise pit_cache.PitError("as_of 需为完整有效的 YYYY-MM-DD")
    return pit_cache.normalize_as_of(as_of)


def _factor_extras(v3_run, home, names, base_rows, wanted, pit_date, market, *,
                   include_sentiment=True, live_sentiment=False, sentiment_fetch=None):
    """新四类因子的原始值 → ``(extras, missing, sources, sentiment_report)``。

    ``extras`` 形状 ``{factor_key: {ticker: raw_value}}``；取不到的标的**不出现在**该字典里
    （不是 0，也不是均值）；每个因子的缺失原因汇总进 ``missing``；``sentiment_report`` 是
    情绪因子的**逐标的来源**（``snapshot`` / ``live:nlp-v1`` / 取不到；见
    :func:`local_factor_extras`），进响应的 ``sentimentSources``。

    ``live_sentiment``（缺省 ``False``）：快照取不到时是否再走 ``v3_nlp`` **实时**打分。
    缺省关是为了让内部流水线（``strategy_run``）与单测保持**零网络**；两个矩阵路由显式打开。
    ``sentiment_fetch`` 是实时取数的注入点（``(home, ticker, limit) -> 零参取数函数|None``）。
    """
    extras = {key: {} for key in ("gross_margin", "net_margin", "roe", "roa", "revenue_yoy",
                                  "net_profit_yoy", "sentiment", "capital_flow",
                                  "short_interest")}
    missing = []
    sources = {"quality": None, "growth": None, "sentiment": None, "alternative": None}
    sentiment_report = {}

    if wanted & {"quality", "growth"} or (include_sentiment and "sentiment" in wanted):
        local, local_missing, local_sources, sentiment_report = local_factor_extras(
            home, names, pit_date,
            want_quality=bool(wanted & {"quality", "growth"}),
            want_sentiment=bool(include_sentiment and "sentiment" in wanted),
            live_sentiment=bool(live_sentiment),
            sentiment_fetch=sentiment_fetch)
        classes_by_key = {entry["key"]: entry["class"] for entry in FACTOR_REGISTRY}
        for key, values in local.items():
            if classes_by_key[key] in wanted:
                extras[key].update(values)
        missing.extend(item for item in local_missing if classes_by_key[item["key"]] in wanted)
        for key, value in local_sources.items():
            if value and key in wanted:
                sources[key] = value

    if "alternative" in wanted:
        reasons = {"capital_flow": [], "short_interest": []}
        for ticker in names:
            values, meta = alternative_factor_values(v3_run, ticker)
            for key, value in values.items():
                extras[key][ticker] = value
            for key, reason in (meta.get("reasons") or {}).items():
                reasons.setdefault(key, []).append(f"{ticker}：{reason}")
        sources["alternative"] = ("futu/capital_flow_history + futu/short_interest（实时，"
                                  "受全局限流器约束）")
        for key in ("capital_flow", "short_interest"):
            if not extras[key]:
                missing.append({"key": key,
                                "reason": "；".join((reasons.get(key) or [])[:3])
                                          or "上游未返回可解析字段（no-data）"})
    return extras, missing, sources, sentiment_report


def local_factor_extras(home, names, pit_date, *, want_quality=True, want_sentiment=True,
                        live_sentiment=False, sentiment_fetch=None):
    """新因子原始值：质量 / 成长（**本地交易库**）+ 情绪（**快照 ∨ 实时**）。

    ``strategy_run`` 的 PAAT 阶段与 ``factors_matrix_data`` 共用这一份实现（不做第二事实源），
    一次打开交易库、逐标的读数；交易库缺失时全部返回空 + 原因（绝不用均值/0 顶替）。
    返回 ``(extras, missing, sources, sentiment_report)``，前三个与 :func:`_factor_extras`
    同形；``sentiment_report`` 是``{ticker: {"origin", "score", "source", "reason"}}``——
    逐标的标注这一格情绪分的**来源**（见下），取不到时 ``origin=None`` + 原因。

    情绪（``want_sentiment``）有两条通道，**快照优先、实时兜底**：

      1. ``snapshot``：``<home>/trading-data/trading.sqlite`` 的 ``sentiment_snapshots``
         （按日采集；PIT 两道闸门见 :func:`sentiment_factor_values`）；
      2. ``live:nlp-v1``（仅 ``live_sentiment=True``）：该标的的**实时资讯**经
         ``server.data.cache`` 的 PIT 闸门（显式 ``as_of``，见
         :func:`live_sentiment_factor_value`）后交同一份 ``server.v3_nlp`` 打分器。

    实时候选是**并发**取的（akshare 每标的 1~3s，逐标的独立失败、互不牵连），并且只在
    快照确实取不到该标的时才并入——两条通道的读数与来源在响应里逐标的可查，绝不混为一谈。
    """
    extras = {key: {} for key in ("gross_margin", "net_margin", "roe", "roa", "revenue_yoy",
                                  "net_profit_yoy", "sentiment")}
    missing = []
    sources = {"quality": None, "growth": None, "sentiment": None}
    sentiment_report = {}
    conn = _open_trading_store(home) if want_quality else None
    if want_quality:
        if conn is None:
            reason = f"交易库 {_trading_store_path(home)} 不存在或不可只读打开"
            for key in ("gross_margin", "net_margin", "roe", "roa", "revenue_yoy", "net_profit_yoy"):
                missing.append({"key": key, "reason": reason})
            sources["quality"] = f"trading-data/fundamentals 不可读（{reason}）"
            sources["growth"] = sources["quality"]
        else:
            reasons = {}
            records = 0
            for ticker in names:
                rows = _read_pit_fundamentals(conn, ticker, pit_date)
                if rows:
                    records += 1
                values, meta = quality_growth_factors(rows, ticker, pit_date)
                for key, value in values.items():
                    extras[key][ticker] = value
                for key, reason in (meta.get("reasons") or {}).items():
                    reasons.setdefault(key, []).append(f"{ticker}：{reason}")
            conn.close()
            sources["quality"] = (f"trading-data/fundamentals（PIT announced_at ≤ {pit_date}；"
                                  f"{records}/{len(names)} 只有可用财报；毛利/净利率来自财报科目，"
                                  "ROE/ROA 只读离线同步的百分数，不年化、不跨期补值）")
            sources["growth"] = (f"trading-data/fundamentals（PIT announced_at ≤ {pit_date}；"
                                 "同报告期同比）")
            for key in ("gross_margin", "net_margin", "roe", "roa", "revenue_yoy", "net_profit_yoy"):
                if not extras[key]:
                    entry = next(item for item in FACTOR_REGISTRY if item["key"] == key)
                    detail = reasons.get(key) or [str(entry["pit"])]
                    missing.append({"key": key, "reason": "；".join(detail[:3])})
    if want_sentiment:
        # 快照优先：逐标的先读落库快照；**只有快照确实取不到的标的**才进实时候选，
        # 且实时候选是并发取好再打分（快照有分的标的既不多一次 akshare 往返，来源也不混）。
        snapshot = {ticker: sentiment_factor_values(home, ticker, pit_date)
                    for ticker in names}
        pending = [ticker for ticker in names if snapshot[ticker][0] is None]
        live_values = (live_sentiment_values(home, pending, pit_date, fetch_factory=sentiment_fetch)
                       if live_sentiment and pending else {})
        reasons = []
        covered = 0
        snapshot_covered = 0
        live_covered = 0
        for ticker in names:
            score, meta = snapshot[ticker]
            origin = SENTIMENT_ORIGIN_SNAPSHOT if score is not None else None
            snapshot_reason = None if score is not None else (meta.get("reason") or "score=null")
            live_meta = None
            if score is None and live_values:
                live_score, live_meta = live_values.get(ticker) or (None, None)
                if live_score is not None:
                    score, meta, origin = live_score, live_meta, SENTIMENT_ORIGIN_LIVE
            reason = None
            source = (meta or {}).get("source")
            if score is None:
                reason = f"快照：{snapshot_reason}"
                if live_sentiment:
                    live_reason = (live_meta or {}).get("reason") or "未取到（见 sentimentSources）"
                    reason += f"；实时：{live_reason}"
                    if live_meta is not None:
                        # 两条通道**都**取不到时来源如实并列（不拿快照那半掩盖实时那半）
                        source = f"{source} ∨ {live_meta.get('source') or '实时资讯（未取到）'}"
                reasons.append(f"{ticker}：{reason}")
            else:
                extras["sentiment"][ticker] = score
                covered += 1
                if origin == SENTIMENT_ORIGIN_SNAPSHOT:
                    snapshot_covered += 1
                else:
                    live_covered += 1
            sentiment_report[ticker] = {
                "origin": origin,
                "score": (None if score is None else v3_math.round_half_up(score, 6)),
                "source": source,
                "reason": reason or (meta or {}).get("reason"),
            }
        if live_sentiment:
            sources["sentiment"] = (
                f"store.sentiment_snapshots（PIT ≤ {pit_date}）∨ akshare/stock_news_em 实时"
                f"（经 server.data.cache PIT 闸门，as_of={pit_date} INCLUSIVE）→ "
                f"server.v3_nlp.score_documents；快照 {snapshot_covered}/{len(names)} · "
                f"实时补 {live_covered}/{len(names)} · 合计 {covered}/{len(names)}")
        else:
            sources["sentiment"] = (f"store.sentiment_snapshots（PIT ≤ {pit_date}）+ "
                                    f"v3_nlp 离线复算；{covered}/{len(names)} 只有可用情绪分")
        if covered == 0:
            missing.append({"key": "sentiment",
                            "reason": "；".join(reasons[:3]) or "窗口内没有可用情绪文档"})
    return extras, missing, sources, sentiment_report


def _cross_sectional_z_map(values):
    """``{ticker: value}`` → ``{ticker: z}``（样本标准差 n−1，截断 ±3；有效值 <2 → 全 null）。"""
    tickers = sorted(values)
    raw = [values[ticker] for ticker in tickers]
    zs = v3_math.cross_sectional_z(raw)
    return {ticker: z for ticker, z in zip(tickers, zs)}


def _merge_factor_extras(matrix, base_rows, extras):
    """把新因子的横截面 z 写进每行 ``z``（**只增不改**：价量/估值 z 一位不动）。"""
    by_ticker = {row.get("ticker"): row for row in base_rows}
    for key, values in extras.items():
        zs = _cross_sectional_z_map(values)
        for ticker, z in zs.items():
            row = by_ticker.get(ticker)
            if row is None:
                continue
            zmap = row.get("z")
            if not isinstance(zmap, dict):
                zmap = {}
                row["z"] = zmap
            zmap[key] = z
        for ticker, value in values.items():
            row = by_ticker.get(ticker)
            if row is None:
                continue
            factors = row.get("factors")
            if not isinstance(factors, dict):
                factors = {}
                row["factors"] = factors
            factors[key] = value


def _project_matrix(base_rows, keys):
    """按 ``keys`` 的列顺序重建二维矩阵（缺格 ``null``，不填 0）。"""
    out = []
    for row in base_rows:
        zmap = row.get("z") if isinstance(row.get("z"), dict) else {}
        line = []
        for key in keys:
            value = v3_math.to_float(zmap.get(key))
            line.append(None if value is None else v3_math.round_half_up(value, 4))
        out.append(line)
    return out


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


def _strategy_analysis(v3_run, universe_list, klines, window, home=None):
    """PAAT：优先 workbench ``factors`` 的 z（综合分 = mom_20/mom_60/trend 的 z 均值），
    不可用时**如实标注**并退化为本地 K 线动量的横截面 z（保证候选池不为空）。

    **FR-STRAT-001 补全（2026-09-20）**：``home`` 给定时，PAAT 再把 **质量/成长/情绪**
    三类新因子（``local_factor_extras``：本地交易库 + 情绪快照，不联网）按横截面 z **同权**
    并进 ``extendedZ``，并如实回报每类因子的覆盖数与缺失原因：

      * ``compositeZ`` 保持历史口径（纯价量动量 z）——既有排序不因本改动而变化；
      * ``extendedZ`` = 有数据的各项 z（价量动量 + 质量 + 成长 + 情绪）同权平均，
        取不到的维度**不参与**（不填 0）；选股/评分改用 ``extendedZ``（见 PET 的 ``basis``），
        缺失维度全部记进 ``stage.factorCoverage``。
    """
    factor_rows = []
    factors_error = None
    if len(universe_list) >= 2:
        envelope = _call(v3_run, "factors", {"tickers": universe_list[:FACTOR_LIMIT]})
        if envelope.get("ok"):
            rows = (_value_of(envelope) or {}).get("rows")
            factor_rows = rows if isinstance(rows, list) else []
        else:
            factors_error = _tool_error(envelope)

    extras, extras_missing, extras_sources = ({}, [], {})
    if home:
        try:
            # 研究流水线（PAAT）只走**本地快照**通道（``live_sentiment`` 缺省关）：一轮流水线
            # 不该因为逐标的实时资讯取数而变慢/变不确定；实时兜底只在两条矩阵只读路由上开。
            extras, extras_missing, extras_sources, _sentiment_report = local_factor_extras(
                home, list(universe_list)[:FACTOR_LIMIT], _pit_date(None))
        except Exception as error:  # noqa: BLE001 —— 新因子失败不拖垮 PAAT
            extras_missing = [{"key": "quality/growth/sentiment",
                               "reason": f"{type(error).__name__}: {error}"}]

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
        extras_of_ticker = {key: values.get(ticker) for key, values in extras.items()
                            if values.get(ticker) is not None}
        analysis.append({
            "ticker": ticker,
            "compositeZ": None if composite is None else v3_math.round_half_up(composite, 4),
            "extras": {key: v3_math.round_half_up(value, 4)
                       for key, value in extras_of_ticker.items()},
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

    extended = _extended_scores(analysis, extras)
    for item in analysis:
        item["extendedZ"] = extended.get(item["ticker"])

    coverage = {
        "classes": {
            "price": {"covered": sum(1 for item in analysis if item["compositeZ"] is not None),
                      "total": len(analysis), "source": score_source},
            "quality": {"covered": sum(1 for item in analysis if any(
                item["extras"].get(key) is not None
                for key in ("gross_margin", "net_margin", "roe", "roa"))),
                        "total": len(analysis), "source": extras_sources.get("quality")},
            "growth": {"covered": sum(1 for item in analysis if item["extras"].get("revenue_yoy") is not None
                                      or item["extras"].get("net_profit_yoy") is not None),
                       "total": len(analysis), "source": extras_sources.get("growth")},
            "sentiment": {"covered": sum(1 for item in analysis
                                         if item["extras"].get("sentiment") is not None),
                          "total": len(analysis), "source": extras_sources.get("sentiment")},
        },
        "missing": extras_missing,
        "note": ("extendedZ = 有数据的横截面 z 同权平均（价量动量 + 质量 + 成长 + 情绪）；"
                 "取不到的因子维度不参与、也不填 0；compositeZ 保持历史纯价量口径"),
    }
    stage = {
        "analyzed": len(analysis),
        "withFactors": sum(1 for item in analysis if item["compositeZ"] is not None),
        "scoreSource": score_source,
        "factorsError": factors_error,
        "factorCoverage": coverage,
    }
    return analysis, stage


def _extended_scores(analysis, extras):
    """扩展综合分：``{ticker: z}`` —— 价量动量 z 与质量/成长/情绪 z 同权平均。

    没有 ``home``（``extras`` 为空）时逐字段退化为历史行为（= ``compositeZ``），
    因此既有调用方与单测零改动。
    """
    if not extras:
        return {item["ticker"]: item["compositeZ"] for item in analysis}
    z_by_key = {}
    for key, values in extras.items():
        if not values:
            continue
        z_by_key[key] = _cross_sectional_z_map({ticker: value for ticker, value in values.items()})
    out = {}
    for item in analysis:
        parts = []
        if item["compositeZ"] is not None:
            parts.append(item["compositeZ"])
        for key, zmap in z_by_key.items():
            z = zmap.get(item["ticker"])
            if z is not None:
                parts.append(z)
        out[item["ticker"]] = (v3_math.round_half_up(sum(parts) / len(parts), 4)
                               if parts else None)
    return out


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

        # PAAT：因子分析（workbench factors → 本地动量兜底）+ 质量/成长/情绪扩维
        analysis, paat = _strategy_analysis(v3_run, universe_list, klines, window, home=home)
        stages["PAAT"] = paat

        # PCPT：候选池。排序优先用 **extendedZ**（价量动量 + 质量 + 成长 + 情绪同权），
        # 没有扩维因子时 extendedZ 恒等于 compositeZ（既有行为逐字段不变）。
        def _rank_key(item):
            score = item.get("extendedZ")
            return score if score is not None else item["compositeZ"]

        ranked = sorted([item for item in analysis if _rank_key(item) is not None],
                        key=_rank_key, reverse=True)
        longs = ranked[:top_n]
        reduce_count = min(top_n, max(0, len(ranked) - top_n))
        reduces = list(reversed(ranked[len(ranked) - reduce_count:])) if reduce_count else []
        stages["PCPT"] = {"longs": [item["ticker"] for item in longs],
                          "reduces": [item["ticker"] for item in reduces],
                          "rankBy": ("extendedZ（价量动量+质量+成长+情绪同权）"
                                     if any(item.get("extendedZ") is not None
                                            and item.get("extras") for item in analysis)
                                     else "compositeZ（纯价量动量 z；扩维因子无数据）")}

        # PRT：等权目标权重（受单笔上限约束）
        weight_pct = min(SINGLE_NAME_LIMIT_PCT, 100.0 / max(1, len(longs)))
        capped = (100.0 / len(longs) > SINGLE_NAME_LIMIT_PCT) if longs else True
        stages["PRT"] = {"weightPctPerName": v3_math.round_half_up(weight_pct, 4),
                         "capped": capped}

        # PET：调仓建议提案（不下单）
        proposals = []
        for item in longs:
            score = _rank_key(item)
            composite = item["compositeZ"]
            mom_20 = (item.get("factors") or {}).get("mom_20")
            extras_text = "、".join(f"{key}={_plain(value)}"
                                    for key, value in sorted((item.get("extras") or {}).items()))
            proposals.append({
                "ticker": item["ticker"],
                "action": "增持",
                "targetWeightPct": v3_math.round_half_up(weight_pct, 4),
                "basis": (f"扩展综合 z={_plain(score)}（动量 z={_plain(composite)}，"
                          f"mom_20={_plain(mom_20)}"
                          + (f"；扩维因子 {extras_text}" if extras_text else
                             "；扩维因子无数据，仅用价量动量")
                          + "）"),
                "riskLevel": "低" if (score or 0) > 0.5 else "中",
                "action_hint": "经审批后由工作台受约束入口执行",
            })
        for item in reduces:
            proposals.append({
                "ticker": item["ticker"],
                "action": "减持",
                "targetWeightPct": 0,
                "basis": f"扩展综合 z={_plain(_rank_key(item))}（排名末位）",
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

        envelope = _pit_series_envelope(v3_run, ticker, limit)
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
         equity: [{t, value}], alignment, alignmentNote}

    指标按**持仓日**基准（空仓日不计入胜率）；数据不足 → ``backtest/insufficient``。
    ``equity`` 与输入**等长**（``len(equity) == metrics.days + 1``）：首行是预热位
    （``value=null``——那天没有前值 ⇒ 没有收益，净值从它之后的第一个收益日才算起），
    第 2 行起逐位对应后一根可用 bar（FR-TOOLS-002 等长 null 对齐，
    ``alignment = "input-length-null-padded"``）。
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

        envelope = _pit_series_envelope(v3_run, ticker, limit)
        if not envelope.get("ok"):
            return {"ok": False, "error": _tool_error(envelope), "market": code}
        bars = _bars_of(envelope)
        result = v3_math.backtest_momentum(bars, window, rebalance_days)
        if result.get("error"):
            return _error("backtest/insufficient", result["error"], market=code)
        # FR-TOOLS-002 等长 null 对齐：``equity`` 一行 = 一根输入 K 线（可用 bar），首根是
        # **预热位**——它没有前值 ⇒ 当天没有收益，净值从它之后的第一个收益日才算起，故
        # ``value=null``（不是 0，也不丢行）。``metrics`` 口径不变（仍按收益序列算）。
        equity = _pad_series_head(result["equity"], [_first_series_stamp(bars)], "value")
        return {"ok": True, "ticker": ticker, "market": code, "marketNote": market_note,
                "metrics": result["metrics"], "equity": equity,
                "alignment": ALIGNMENT_NULL_PADDED,
                "alignmentNote": (
                    f"equity 等长对齐：长度 = 输入可用 K 线数 = metrics.days + 1"
                    f"（{len(equity)} 行），首行是预热位 → value=null（不是 0）；"
                    "第 2 行起的 value 与「第二根可用 bar」逐位对应。"
                    "**消费方契约**：预热位是「无读数」，按 stat-core 三态口径显示占位符，"
                    "不得用 Number(null)=0 强转（那会把曲线起点画成 0）")}
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
            envelope = _pit_series_envelope(v3_run, name, limit)
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
                # 等长对齐（FR-TOOLS-002）：per_ticker 一行一标的，与 tickers 等长；
                # 取数失败的标的进 failures（不是全 0 的一行）。
                "alignment": ALIGNMENT_INPUT_LENGTH,
                "alignmentNote": (f"横截面等长对齐：per_ticker 一行一标的"
                                  f"（{len(bars_by_ticker)} 只有效 / 请求 {len(names)} 只），"
                                  "顺序同 tickers；失败的标的在 failures，不填 0"),
                **suite}
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


# ===========================================================================
# FR-EXEC-003 补全：杠杆率 / 流动性风险 / 绩效归因 / 资金检查
# ===========================================================================
#
# 四条纪律（与模块头部的数据诚实性一致，逐条可查）:
#
#   1. **只读**：只调只读工具（``positions`` / ``equity`` / ``plan`` / ``account_funds``）
#      与只读的本地库/HTTP；本段**没有任何**下单/改单/撤单/切模式入口。
#   2. **不改既有闸门语义**：行业红线 / 单笔上限 / 回撤红线在 ``v3_stdlib``（``v3_ops``）
#      里一分不动；这里只做「读数 + 分级建议」，并把资金检查作为**新增的、可缺省**
#      一维交给调用方（缺省不参与 → 既有判定逐字段不变）。
#   3. **缺数据一律 ``null`` + 原因**：绝不用 0 / 行业均值 / 估算值顶替（``no-data``）。
#   4. **口径写在响应里**：每个子项都带 ``source`` / ``as_of`` / ``note``，页面原样展示。

#: 富途模拟账户 ``market_id`` → 市场链（与 ``trading_core.market_ids.SIM_MARKET_IDS``
#: 同源实测口径：港股 1 / A 股 3 / 美股 100）。表外的 market_id（期权 9、期货 10-13、
#: 日股 16…）**不猜**，一律排除并计数。
SIM_MARKET_TO_CHAIN = {1: "HK", 3: "SH", 100: "US"}
#: 用于把持仓行拼成 ``MARKET.CODE`` 的账户市场 → 前缀（A 股模拟账户同时承载 SH/SZ/BJ，
#: 只有 6 位代码无法区分交易所，故统一用 ``SH`` 前缀——与 ``v3_universe.resolve_universe``
#: 对真实持仓的归一同一口径；行业解析用的是同一份前缀语义）。
SIM_MARKET_TO_PREFIX = {1: "HK", 3: "SH", 100: "US"}

#: ADV 口径：近 20 个交易日的**日均成交额**（既有 K 线链路 ``series``，period=1d）。
ADV_WINDOW = 20
#: 参与率分级阈值（%）：订单金额 / ADV。≥ block → 建议阻断（人工改单）；≥ warn → 建议人工确认。
LIQUIDITY_WARN_PCT = 5.0
LIQUIDITY_BLOCK_PCT = 10.0

#: 组合归因口径标识（写在响应里，不用前端二次换算）。
ATTRIBUTION_TICKER_METHOD = "逐标的贡献 = 该标的未实现盈亏 / 账户总资产（券商持仓口径，非时间加权）"
ATTRIBUTION_INDUSTRY_METHOD = "行业贡献 = 该行业全部持仓未实现盈亏合计 / 账户总资产"
ATTRIBUTION_FACTOR_REASON = (
    "因子归因缺 PIT 建仓因子敞口：券商持仓不返回建仓时点的因子值，"
    "工作台台账也没有逐笔因子快照（factor_snapshots 只有日度横截面快照）——"
    "本实现不臆造因子贡献，返回 null")
#: 归因可用的行业映射来源（与 ``/api/v3/risk/industry`` 同一份富途板块链路）。
ATTRIBUTION_INDUSTRY_SOURCE = "server.v3_industry.IndustryResolver（futu/info_owner_plate）"


def _trading_store_path(home):
    """交易库路径（``<home>/trading-data/trading.sqlite``）——实现已收敛到 PIT 入口。

    FR-DATA-003：路径口径只有一份（``server.data.cache.store_path``）；本模块保留这个薄名
    是为了不打断既有调用点与错误文案。
    """
    return pit_cache.store_path(home)


def _open_trading_store(home):
    """**只读**打开交易库（``mode=ro``）；库不存在/打不开 → ``None``（调用方如实报 no-data）。

    FR-DATA-003：连接方式与 PIT 口径都收敛到 ``server.data.cache``（``open_store``）——
    只读打开后只发 ``SELECT``，不 import ``trading_core.store`` 的建表/``migrate()`` 路径
    （读数口不该在取数路径上写库）。PIT 条件由 ``pit_cache.read_fundamentals`` 施加，
    与 ``trading_core.store.read_fundamentals`` 的 ``announced_at IS NOT NULL AND
    announced_at<=as_of`` 逐字同口径。
    """
    return pit_cache.open_store(home)


def _read_pit_fundamentals(conn, ticker, as_of):
    """PIT 基本面行（``announced_at`` 非空且 ≤ ``as_of``）→ ``[row, ...]``。

    FR-DATA-003 迁移：过滤逻辑本身搬进 ``server.data.cache.read_fundamentals``（PIT 唯一
    入口），本函数退化成薄适配（把信封里的行取出来、把缺连接的情形保持为 ``[]``）。
    ``conn`` 为 ``None``（库缺失）→ 空列表。**没有公告日的行一律不可见**——它们无法证明
    「当时已知」，拿报告期当可得日就是前视偏差（宁缺毋假）。
    """
    if conn is None:
        return []
    try:
        envelope = pit_cache.read_fundamentals(str(as_of), pit_cache.AS_OF_INCLUSIVE,
                                               symbol=str(ticker), conn=conn,
                                               source="trading-data/fundamentals")
    except pit_cache.PitSourceError:
        return []
    return list(envelope.get("rows") or [])


def _latest_period_rows(rows, ticker, as_of):
    """最新报告期的字段行 → ``(field → value, period_end, announced_at, source)``。

    FR-DATA-003 迁移：``period_end <= as_of`` 这道闸门搬进
    ``server.data.cache.latest_period_rows``（PIT 唯一入口）；数值转换仍留在本模块
    （``v3_math.to_float``），返回形状与原实现逐字段一致。
    """
    fields, latest, announced, source, _stats = pit_cache.latest_period_rows(
        rows, as_of, pit_cache.AS_OF_INCLUSIVE)
    if latest is None:
        return {}, None, None, None
    return ({key: v3_math.to_float(value.get("value")) for key, value in fields.items()},
            latest, announced, source)


def _ratio_pct(numerator, denominator):
    """比率（%）——分子/分母任一缺失或分母非正 → ``None``（不用 0 假冒）。"""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return v3_math.round_half_up(numerator / denominator * 100.0, 4)


def quality_growth_factors(rows, ticker, as_of):
    """质量（毛利率/净利率/ROE/ROA）+ 成长（``revenue_yoy``/``net_profit_yoy``）。

    ``rows`` 是 :func:`_read_pit_fundamentals` 的 PIT 行；**只用 ≤ ``as_of`` 的公告**。
    返回 ``(values, meta)``：``values`` 里缺的键**不出现**（调用方按 no-data 记原因），
    ``meta`` 写明用了哪个报告期/公告日/来源，以及每个缺失因子**为什么缺**。
    """
    values = {}
    fields, period, announced, source = _latest_period_rows(rows, ticker, as_of)
    reasons = {}
    if not period:
        reason = (f"无 PIT 财报：{ticker} 在 trading-data/fundamentals 里没有公告日 ≤ {as_of}"
                  f" 的记录（该表只收录已合并公告日的行；无公告日 = 无法证明当时已知）")
        for key in ("gross_margin", "net_margin", "roe", "roa", "revenue_yoy", "net_profit_yoy"):
            reasons[key] = reason
        return values, {"ticker": ticker, "period_end": None, "announced_at": None,
                        "source": None, "reasons": reasons}

    gross_margin = _ratio_pct(fields.get("gross_profit"), fields.get("revenue"))
    net_margin = _ratio_pct(fields.get("net_profit"), fields.get("revenue"))
    if gross_margin is None:
        reasons["gross_margin"] = (f"报告期 {period} 缺毛利/营收科目"
                                   f"（gross_profit={fields.get('gross_profit')}、"
                                   f"revenue={fields.get('revenue')}）")
    else:
        values["gross_margin"] = gross_margin
    if net_margin is None:
        reasons["net_margin"] = (f"报告期 {period} 缺净利/营收科目"
                                 f"（net_profit={fields.get('net_profit')}、"
                                 f"revenue={fields.get('revenue')}）")
    else:
        values["net_margin"] = net_margin

    for key in ("roe", "roa"):
        value = fields.get(key)
        if value is None:
            reasons[key] = f"报告期 {period} 缺已公告的 {key}（本地存储百分数口径，不跨期补值）"
        else:
            values[key] = value

    # 成长：与**同报告期口径**的上一年比（``2026-06-30`` ↔ ``2025-06-30``）。
    # 库里没有上一年同期的 PIT 行 → null + 原因，绝不拿相邻期或全年数硬算同比。
    prior = None
    if period:
        target = f"{int(period[:4]) - 1}{period[4:]}"
        for row in rows:
            if str(row.get("period_end")) == target:
                prior = row
                break
    if prior is None:
        reason = (f"无上一年同期 PIT 记录（需要 {int(period[:4]) - 1}{period[4:]} 且已合并公告日），"
                  f"跨期/跨年推算同比会前视，故 {('revenue_yoy')} 返回 null")
        reasons["revenue_yoy"] = reason
        reasons["net_profit_yoy"] = reason
    else:
        prior_rows = [row for row in rows if str(row.get("period_end")) == prior.get("period_end")]
        prior_fields, _, _, _ = _latest_period_rows(prior_rows, ticker, as_of)
        revenue_yoy = _growth_pct(fields.get("revenue"), prior_fields.get("revenue"))
        profit_yoy = _growth_pct(fields.get("net_profit"), prior_fields.get("net_profit"))
        if revenue_yoy is None:
            reasons["revenue_yoy"] = (f"{period} 或 {prior.get('period_end')} 的营收缺失/基期非正"
                                      "（同比用同口径报告期，不做跨期推算）")
        else:
            values["revenue_yoy"] = revenue_yoy
        if profit_yoy is None:
            reasons["net_profit_yoy"] = (f"{period} 或 {prior.get('period_end')} 的净利润缺失/基期非正"
                                         "（同比用同口径报告期，不做跨期推算）")
        else:
            values["net_profit_yoy"] = profit_yoy
    return values, {"ticker": ticker, "period_end": period, "announced_at": announced,
                    "source": source, "reasons": reasons,
                    "comparison_period": None if prior is None else prior.get("period_end")}


def _growth_pct(current, prior):
    """同比（%）——基期缺失/非正 → ``None``（基期 ≤0 的同比无意义，不给假数）。"""
    if current is None or prior is None or prior <= 0:
        return None
    return v3_math.round_half_up((current / prior - 1.0) * 100.0, 4)


# ---------------------------------------------------------------------------
# 情绪因子·实时通道：v3_nlp 同一份打分器 + data.cache 的 PIT 闸门
# ---------------------------------------------------------------------------
#: 情绪读数的逐标的来源标注（响应 ``sentimentSources[ticker].origin``）：
#: 落库快照 / 实时 NLP（``v3_nlp`` 词典版本见 ``sentiment_factor_values`` 的 method）。
SENTIMENT_ORIGIN_SNAPSHOT = "snapshot"
SENTIMENT_ORIGIN_LIVE = "live:nlp-v1"
#: 实时情绪的资讯条数上限（``stock_news_em`` 实测每页 10 条；limit 只影响请求条数）。
SENTIMENT_LIVE_LIMIT = 20
#: 实时情绪的文档窗口（天）——与 ``sentiment_factor_values`` 的缺省窗口同一口径。
SENTIMENT_LIVE_WINDOW_DAYS = 7
#: 实时取数并发度上限（akshare 每标的 1~3s；逐标的独立失败，不互相牵连）。
SENTIMENT_LIVE_WORKERS = 4
#: 实时打分的时间半衰（小时）——与快照通道、``/api/v3/sentiment`` 同值。
SENTIMENT_LIVE_HALF_LIFE_HOURS = 48.0


def _nlp_guard():
    """``server.v3_nlp`` 的**可选**导入（别人的模块）：拿不到就降级为 ``(None, 原因)``。

    情绪因子是**附加值**：``v3_nlp`` 缺失/导入失败时，其余的价量/质量/成长列照常返回，
    情绪格如实写 ``null`` + 原因（绝不填 0，也不把整个矩阵打成错误）。
    """
    try:
        from server import v3_nlp
    except ImportError as error:  # pragma: no cover —— 部署里它总在；缺了也不该 500
        return None, f"server.v3_nlp 不可用（ImportError: {error}）"
    return v3_nlp, None


def default_news_fetch(home, ticker, limit=SENTIMENT_LIVE_LIMIT):
    """缺省的实时资讯取数：``v3_sources.fetch_news``（**惰性导入**，失败 → ``(None, 原因)``）。

    返回 ``(fetch, note)``：``fetch`` 是**零参**可调用对象，形状与 ``v3_nlp.sentiment_report``
    的 ``news_fetch`` 依赖同一份既有资讯通道（``akshare/stock_news_em`` + ``retry_akshare``
    退避重试与 ``attempts`` 留痕），检索关键字走 ``v3_nlp.news_keyword``（与在线情绪端点
    同一口径：A 股用 6 位代码、港美股用裸代码）。本模块**不自造第二数据源**，也不新建
    HTTP 客户端——只把既有通道包一层，并在测试里可整条替换。
    """
    nlp, error = _nlp_guard()
    if nlp is None:
        return None, error
    try:
        from server import v3_sources
    except ImportError as error:  # pragma: no cover —— 同上：缺失只降级不报错
        return None, f"server.v3_sources 不可用（ImportError: {error}）"
    keyword = nlp.news_keyword(ticker)
    deps = v3_sources.Deps(home=None if home is None else str(home))
    return (lambda: v3_sources.fetch_news(deps, keyword, limit)), f"v3_sources.fetch_news({keyword})"


def live_sentiment_factor_value(home, ticker, as_of, *,
                                window_days=SENTIMENT_LIVE_WINDOW_DAYS,
                                limit=SENTIMENT_LIVE_LIMIT,
                                half_life_hours=SENTIMENT_LIVE_HALF_LIFE_HOURS,
                                fetch=None):
    """单标的**实时**情绪分：资讯取数 → ``data.cache`` PIT 闸门（显式 ``as_of``）→ ``v3_nlp`` 打分。

    返回 ``(score, meta)``；``meta["origin"]`` 取到分时恒为 ``"live:nlp-v1"``，否则 ``None``
    + ``meta["reason"]``（**不是 0**：没有资讯 / 一篇都没命中词典 / 命中但半衰把权重压到 0
    都各自写明原因）。

    三道闸门（逐条可核验）::

      ① 取数：``fetch``（缺省 = ``default_news_fetch``：既有 ``v3_sources.fetch_news``）；
      ② **PIT**：文档发布时间经 ``server.data.cache`` 的 ``pit_rows``（唯一入口）按
         **显式 ``as_of`` + AS_OF_INCLUSIVE** 过滤——晚于 ``as_of``（UTC 日期）的文档被挡掉
         并计数（``rejected.future``，前视证据）；没有时间戳的文档**进不了读数**
         （``rejected.undated``）——实时资讯与落库快照不同：快照行自带的采集日是「当时已知」
         的凭证，而一篇无时间戳的实时文章无法证明当时已知，按 ``data.cache`` 的
         「宁缺毋假」处理；
      ③ 业务窗口：发布时间必须落在 ``[as_of 当日结束 − window_days, as_of 当日结束]``，
         窗口外的文档计数排除（``outsideWindow``），与快照通道同一口径。

    打分器是 ``v3_nlp.score_documents``（与快照通道、``/api/v3/sentiment`` 同一份实现，
    时间基准 = ``as_of`` 当日结束 UTC，半衰权重因此不含未来资讯）。**只读**：本函数不写盘、
    不下单、不切模式；资讯也不进 ``data.cache`` 的 TTL 层——与 ``data/cache.py`` 第三节
    「上游 ``fetch=`` 路径有意不缓存」同一条边界（新鲜度由上游端点自己的 TTL 负责）。
    """
    meta = {"origin": None, "source": None, "reason": None,
            "mode": pit_cache.AS_OF_INCLUSIVE, "window_days": int(window_days)}
    try:
        as_of = pit_cache.normalize_as_of(as_of)
    except pit_cache.PitError as error:
        meta["reason"] = f"as_of 不合法：{error}"
        return None, meta
    meta["as_of"] = as_of
    meta["semantics"] = pit_cache.semantics_text(pit_cache.AS_OF_INCLUSIVE, as_of)

    nlp, error = _nlp_guard()
    if nlp is None:
        meta["reason"] = error
        return None, meta
    if fetch is None:
        fetch, note = default_news_fetch(home, ticker, limit)
        if fetch is None:
            meta["reason"] = note
            return None, meta
    try:
        envelope = fetch()
    except Exception as error:  # noqa: BLE001 —— 取数抛异常按「取不到」处理，不冒泡成 500
        meta["reason"] = f"实时资讯取数异常：{type(error).__name__}: {error}"
        return None, meta
    if not isinstance(envelope, dict):
        meta["reason"] = f"资讯源返回非信封对象：{type(envelope).__name__}"
        return None, meta
    meta["source"] = str(envelope.get("source") or "akshare/stock_news_em") + "（实时）"
    if not envelope.get("ok"):
        detail = envelope.get("error") or {}
        code = str(detail.get("code") or "sentiment/news-failed")
        message = str(detail.get("message") or "资讯源失败且未提供错误明细")
        meta["reason"] = f"实时资讯取数失败（{code}）：{message}"
        return None, meta

    rows = [row for row in (envelope.get("rows") or []) if isinstance(row, dict)]
    prepared = [_normalize_doc_time(row) for row in rows]

    # ② PIT 闸门（唯一入口）：as_of 用 **UTC 日期**（与本模块其它 as_of 同口径）。
    gate = []
    for item in prepared:
        published = nlp._doc_time(item)
        stamp = None
        if isinstance(published, datetime):
            aware = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
            stamp = aware.astimezone(timezone.utc).date().isoformat()
        gate.append({"t": stamp, "doc": item})
    visible, pit_stats = pit_cache.pit_rows(gate, as_of, pit_cache.AS_OF_INCLUSIVE, key="t")
    meta["rejected"] = {"future": pit_stats.get("future"), "undated": pit_stats.get("undated"),
                        "kept": pit_stats.get("kept")}

    # ③ 业务窗口：与快照通道同一口径（as_of 当日结束 UTC 为右端，左端 −window_days）。
    span = max(1, int(window_days))
    cutoff = datetime.strptime(as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc) \
        + timedelta(days=1) - timedelta(microseconds=1)
    start = cutoff - timedelta(days=span)
    meta["window"] = {"from": start.date().isoformat(), "to": as_of}
    documents = []
    outside = 0
    for entry in visible:
        item = entry["doc"]
        published = nlp._doc_time(item)
        if not isinstance(published, datetime):
            outside += 1
            continue
        aware = published if published.tzinfo else published.replace(tzinfo=timezone.utc)
        if aware < start or aware > cutoff:
            outside += 1
            continue
        documents.append(item)
    meta["outsideWindow"] = outside
    if not documents:
        extra = ""
        if meta["rejected"]["future"] or meta["rejected"]["undated"]:
            extra = (f"（PIT 闸门：挡掉晚于 as_of 的 {meta['rejected']['future']} 篇、"
                     f"无时间戳 {meta['rejected']['undated']} 篇）")
        meta["reason"] = (f"{ticker} 在 {meta['window']['from']}..{as_of} 没有窗口内的实时资讯"
                          + (f"（{outside} 篇发布时间在窗口外，已排除）" if outside else "")
                          + extra + f"（{meta['source']}）")
        return None, meta

    scored = nlp.score_documents(documents, half_life_hours=half_life_hours, now=cutoff)
    meta.update({"documents": scored.get("documents"), "scored": scored.get("scored"),
                 "coverage": scored.get("coverage"), "method": scored.get("method"),
                 "half_life_hours": half_life_hours})
    score = scored.get("score")
    if score is None:
        # 与快照通道逐条同口径：命中 0 篇 / 半衰把权重和压到 0 → null（**不是 0 分**）。
        if scored.get("scored"):
            meta["reason"] = (f"{scored['documents']} 篇实时资讯里 {scored['scored']} 篇命中词典，"
                              f"但时间半衰（{half_life_hours}h）把权重和压到 0 → score=null（不是 0 分）")
        else:
            meta["reason"] = (f"{scored.get('documents', 0)} 篇实时资讯无一命中词典"
                              "（score=null，不是 0 分）")
        return None, meta
    meta["origin"] = SENTIMENT_ORIGIN_LIVE
    return score, meta


def live_sentiment_values(home, names, as_of, *, window_days=SENTIMENT_LIVE_WINDOW_DAYS,
                          limit=SENTIMENT_LIVE_LIMIT,
                          half_life_hours=SENTIMENT_LIVE_HALF_LIFE_HOURS,
                          fetch_factory=None):
    """批量实时情绪：``{ticker: (score, meta)}``（并发取数；逐标的失败互不牵连）。

    ``fetch_factory``（缺省 :func:`default_news_fetch`）是取数注入点，签名
    ``(home, ticker, limit) -> (零参取数函数|None, note)``——单测据此注入假资讯源，
    **整个测试面因此不触网**。
    """
    out = {}
    tickers = [str(name).strip() for name in (names or []) if str(name or "").strip()]
    if not tickers:
        return out
    factory = fetch_factory or default_news_fetch

    def worker(name):
        try:
            fetch, note = factory(home, name, limit)
        except Exception as error:  # noqa: BLE001 —— 取数装配失败也只影响这一个标的
            return name, (None, {"origin": None, "reason":
                                 f"实时取数初始化失败：{type(error).__name__}: {error}"})
        if fetch is None:
            return name, (None, {"origin": None, "reason": note})
        return name, live_sentiment_factor_value(
            home, name, as_of, window_days=window_days, limit=limit,
            half_life_hours=half_life_hours, fetch=fetch)

    workers = max(1, min(SENTIMENT_LIVE_WORKERS, len(tickers)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="v3-sentiment") as pool:
        for name, pair in pool.map(worker, tickers):
            out[name] = pair
    return out


def sentiment_factor_values(home, ticker, as_of, *, window_days=7, half_life_hours=48.0):
    """情绪因子：用 ``v3_nlp`` 同一份自研打分器**离线复算**已落库的资讯快照。

    **数据源**：``<home>/trading-data/trading.sqlite`` 的 ``sentiment_snapshots``
    （``sentiment_snapshot`` 作业每日落库，含 ``fin_sentiment``/``fin_news`` 两源原文）。
    **PIT（两道闸门，都要过）**：

      1. ``date <= as_of``（快照按采集日落库，绝不看未来采集）；
      2. **文档自身的发布时间**也要落在 ``[as_of - window_days, as_of]`` 内——快照里的
         ``items`` 常常是**历史长尾**（实测 ``fin_news`` 里混着 2–4 个月前的新闻），
         只按采集日过滤会让「窗口」名不副实；窗口外的文档被排除并**计数写进 meta**
         （``outsideWindow``），不静默丢弃也不硬算进分数。

    打分时间基准取 ``as_of`` 当日 00:00 UTC（半衰权重因此不含未来资讯），
    默认窗口 7 天与 ``GET /api/v3/sentiment`` 的 ``days`` 缺省一致。
    **不是 0**：没有文档 / 一篇都没命中词典 / 命中文档的权重和被半衰压到 0 →
    ``score=None`` + 各自原因（与在线端点的诚实口径逐条一致）。
    """
    conn = _open_trading_store(home)
    if conn is None:
        return None, {"reason": "交易库 trading-data/trading.sqlite 不存在或不可读（只读模式打开失败）",
                      "source": "store.sentiment_snapshots"}
    try:
        # FR-DATA-003：采集日闸门（``date <= as_of``）与「最近 60 条」都改由 PIT 唯一入口
        # 施加（``server.data.cache.read_sentiment_snapshots``）；本函数只保留第二道
        # **业务**闸门（文档自身发布时间落在窗口内）与打分口径。
        envelope = pit_cache.read_sentiment_snapshots(
            str(as_of), pit_cache.AS_OF_INCLUSIVE, symbol=str(ticker), conn=conn,
            limit=60, source="store.sentiment_snapshots")
        rows = envelope.get("rows") or []
    except pit_cache.PitSourceError as error:
        return None, {"reason": f"sentiment_snapshots 读取失败：{error}",
                      "source": "store.sentiment_snapshots"}
    finally:
        conn.close()

    try:
        day = datetime.strptime(str(as_of)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    # 「PIT 上界」= as_of **当日结束**（UTC）：as_of 日当天发布的资讯在收盘后是已知的，
    # 用当日 00:00 作上界会把它们当成「未来」剔除（实测 2026-09-10 的资讯正是这样被误剔的）。
    # 半衰权重的基准也用它：当日资讯的 age_hours ∈ [0, 24)，权重仍在同一量级。
    cutoff = day + timedelta(days=1) - timedelta(microseconds=1)
    floor = (cutoff - timedelta(days=max(1, int(window_days)))).date().isoformat()

    documents = []
    prepared = []
    dates = set()
    for row in rows:
        day = str(row["date"])
        if day < floor:
            continue
        dates.add(day)
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for key in ("items", "docs", "documents", "news"):
            items = payload.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        prepared.append(_normalize_doc_time(item))
                    elif isinstance(item, str) and item.strip():
                        prepared.append({"title": item})
    # 第二个 PIT 闸门：文档自身发布时间必须落在窗口内（无时间戳的按旧口径保留并计入 undated，
    # 由打分器按 as_of 计权并自己计数——它不猜时间，我们也不替它丢）。
    # 打分器的导入与实时通道走**同一个守卫**：``v3_nlp`` 缺失时情绪格是 null + 原因，
    # 而不是把整个矩阵打成 v3/internal（其余因子类别与价量列照常返回）。
    nlp, import_error = _nlp_guard()
    if nlp is None:
        return None, {"reason": import_error, "source": "store.sentiment_snapshots"}
    start = cutoff - timedelta(days=max(1, int(window_days)))
    outside = 0
    for item in prepared:
        published = nlp._doc_time(item)
        if published is None:
            documents.append(item)
            continue
        if published < start or published > cutoff:
            outside += 1
            continue
        documents.append(item)
    if not documents:
        reason = (f"{ticker} 在 {floor}..{as_of} 没有窗口内的情绪快照文档"
                  + (f"（快照里有 {outside} 篇原文发布时间在窗口外，已排除）" if outside else "")
                  + "（store.sentiment_snapshots）")
        return None, {"reason": reason, "source": "store.sentiment_snapshots",
                      "window_from": floor, "window_to": str(as_of)[:10], "documents": 0,
                      "outsideWindow": outside}
    scored = nlp.score_documents(documents, half_life_hours=half_life_hours, now=cutoff)
    # ``score=None`` 有两种原因，分开写清（页面/矩阵才不会把 no-data 读成「中性 0 分」）：
    #   ① 一篇都没命中词典（scored=0）；
    #   ② 有命中文档、但时间半衰把权重和压到 0（快照原文远早于 as_of：例如 last30days 的长尾）——
    #      半衰权重和 <= 0 时打分器按设计返回 null，这里如实转述，**不**另算一个数出来。
    score = scored["score"]
    reason = None
    if score is None:
        if scored.get("scored"):
            reason = (f"{scored['documents']} 篇文档里 {scored['scored']} 篇命中词典，"
                      f"但时间半衰（{half_life_hours}h）把权重和压到 0"
                      f"（原文远早于 as_of）→ score=null（不是 0 分）")
        else:
            reason = f"{scored.get('documents', 0)} 篇文档无一命中词典（score=null，不是 0 分）"
    return score, {
        "source": "store.sentiment_snapshots + server.v3_nlp.score_documents(离线复算)",
        "method": scored.get("method"), "window_from": floor, "window_to": str(as_of)[:10],
        "dates": sorted(dates), "documents": scored.get("documents"),
        "scored": scored.get("scored"), "coverage": scored.get("coverage"),
        "half_life_hours": half_life_hours, "outsideWindow": outside,
        "reason": reason,
    }


def _normalize_doc_time(doc):
    """把快照文档里的 **epoch 毫秒时间戳字符串**归一成 ISO —— 只做单位换算，不改语义。

    为什么需要（实测 2026-09-20）::

        sentiment_snapshots.fin_news 的 items[].time = "1787829257000"（**字符串形式的毫秒**）

    ``v3_nlp.parse_time`` 支持「epoch（秒/毫秒）」但只对**数值**成立：13 位字符串既不是
    ISO 也不是任何 ``_TIME_PATTERNS``，会返回 ``None`` → 半衰权重按 `w=1.0` 计（`age_hours=0`），
    在 as_of 回放里等于把「一个月前的资讯」当「今天」——**这是会算错分的**，不是显示问题。
    两种单位在本数据里可由数量级区分（秒 ≈1.8e9、毫秒 ≈1.8e12），因此用 1e11 作阈值显式归一，
    并在 10 位/13 位以外的数字上一律不动（无法判定就不猜单位，交给打分器按无时间处理）。
    """
    if not isinstance(doc, dict):
        return doc
    for key in ("time", "published_at", "published", "datetime", "date", "发布时间", "时间"):
        value = doc.get(key)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text.isdigit() or len(text) not in (10, 13):
            continue
        try:
            number = int(text)
        except ValueError:  # pragma: no cover —— isdigit 已保证可转
            continue
        seconds = number / 1000.0 if number > 100_000_000_000 else float(number)
        try:
            stamp = datetime.fromtimestamp(seconds, timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        # 输出成 ``...+00:00`` 的 ISO：``v3_nlp.parse_time`` 的 fromisoformat 分支能直接解析
        # （带 ``Z`` 会被它改写成 ``+00:00`` 同样走通；两种都行，这里用 ISO 省一步替换）。
        return {**doc, key: stamp.isoformat()}
    return doc


def alternative_factor_values(v3_run, ticker, *, days=20):
    """另类因子：``capital_flow``（主力净流入强度）+ ``short_interest``（空头占比）。

    数据源都是既有工具（富途实时读取，受全局限流器约束）。取不到 → 值 ``None`` + **上游
    错误原文**，绝不用 0 顶替；A 股无卖空数据是上游事实（``short_*`` 仅 HK/US 可卖空证券）。
    """
    values, meta = {}, {"ticker": ticker, "reasons": {}, "sources": {}}

    envelope = _call(v3_run, "capital_flow_history", {"code": ticker, "days": days})
    value = _value_of(envelope) or {}
    flows = value.get("flow_list") if isinstance(value.get("flow_list"), list) else []
    if not flows:
        meta["reasons"]["capital_flow"] = (
            f"capital_flow_history 未返回流水：{_error_message(envelope)}")
    else:
        main = 0.0
        gross = 0.0
        for row in flows:
            if not isinstance(row, dict):
                continue
            net = v3_math.to_float(row.get("main_in_flow"))
            if net is None:
                net = v3_math.to_float(row.get("in_flow"))
            if net is None:
                continue
            main += net
            gross += abs(net)
        if gross <= 0:
            meta["reasons"]["capital_flow"] = (
                f"{len(flows)} 日资金流水的净额全为 0/不可解析，无法算强度（不返回 0 分）")
        else:
            values["capital_flow"] = v3_math.round_half_up(main / gross, 4)
            meta["sources"]["capital_flow"] = ("futu/capital_flow_history（近 "
                                               f"{len(flows)} 个交易日主力净流入 / Σ|净额|）")

    envelope = _call(v3_run, "short_interest", {"code": ticker})
    value = _value_of(envelope) or {}
    ratio = None
    for key in ("short_interest_ratio", "short_ratio", "ratio", "short_percent"):
        ratio = v3_math.to_float(value.get(key))
        if ratio is not None:
            break
    rows = value.get("interest_list") or value.get("items") or value.get("list") or []
    if ratio is None and isinstance(rows, list) and rows and isinstance(rows[0], dict):
        for key in ("short_interest_ratio", "short_ratio", "ratio", "short_percent"):
            ratio = v3_math.to_float(rows[0].get(key))
            if ratio is not None:
                break
    if ratio is None:
        meta["reasons"]["short_interest"] = (
            f"short_interest 未给出空头占比：{_error_message(envelope)}")
    else:
        values["short_interest"] = ratio
        meta["sources"]["short_interest"] = "futu/short_interest（空头占比，原始字段直取）"
    return values, meta


# ---------------------------------------------------------------------------
# ① 杠杆率（真实账户字段推导；上游不给融资字段就如实 no-data）
# ---------------------------------------------------------------------------
def _parse_cash_fields(raw):
    """富途资金行 → 认识字段的字典（``None`` 保留原样：不认识就不知道自己不知道）。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ("total_assets", "total_asset", "power", "max_power_long", "available_funds",
                "cash", "balance", "hold", "mv", "long_mv", "short_mv",
                "unrealized_profit", "realized_profit"):
        if key in raw:
            out[key] = v3_math.to_float(raw.get(key))
    return out


def _account_groups(envelope):
    """账户类工具信封 → ``[group, ...]``；形状不认识 → 空列表（原因由调用方写）。"""
    value = _value_of(envelope)
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if not isinstance(value, dict):
        return []
    groups = value.get("groups")
    if not isinstance(groups, list):
        return []
    return [row for row in groups if isinstance(row, dict)]


def _chain_of_group(group):
    """账户分组 → 市场链（``market_id`` 实测表；表外 → ``None``，不猜）。"""
    market = group.get("market")
    if isinstance(market, bool):
        return None
    if isinstance(market, (int, float)) and float(market).is_integer():
        return SIM_MARKET_TO_CHAIN.get(int(market))
    for key in ("market_chain", "chain", "market_code"):
        text = str(group.get(key) or "").strip().upper()
        if text in ("SH", "SZ", "BJ", "HK", "US"):
            return text if text in ("HK", "US") else "SH"
    return None


def _position_key(group, row):
    """持仓行 → ``(market_chain, MARKET.CODE)``。"""
    chain = _chain_of_group(group)
    symbol = str(row.get("symbol") or row.get("code") or "").strip().upper()
    if "." in symbol:
        head, tail = symbol.split(".", 1)
        if head in ("SH", "SZ", "BJ", "HK", "US", "CN"):
            return ("SH" if head in ("SH", "SZ", "BJ", "CN") else head), symbol
        if tail in ("SH", "SZ", "BJ", "HK", "US"):
            return ("SH" if tail in ("SH", "SZ", "BJ") else tail), f"{tail}.{head}"
    if not chain:
        return None, symbol
    prefix = SIM_MARKET_TO_PREFIX.get(group.get("market")) if isinstance(
        group.get("market"), int) else None
    prefix = prefix or ("HK" if chain == "HK" else "US" if chain == "US" else "SH")
    return chain, f"{prefix}.{symbol}" if symbol else symbol


def portfolio_leverage(v3_run, *, market=None, funds_envelope=None, positions_envelope=None):
    """FR-EXEC-003 事中「杠杆率」：**从真实资金/持仓字段推导**，缺字段就 no-data。

    读数（全部来自券商原始字段，见 ``note``）::

        {source, as_of, mode, market, accounts[], readings:{...}, direction,
         leverage_ratio_pct, long_mv_ratio_pct, buying_power_ratio_pct, cash_ratio_pct,
         provider_note, note, error}

    上游事实（本机 2026-09-20 实测，写进 ``provider_note``，不假装有融资余额）:

      * 模拟盘 ``sim_trade_cash_info``：``balance`` / ``hold`` / ``max_power_long`` /
        ``total_asset`` / ``mv`` / ``long_mv`` / ``short_mv``；``max_power_long`` 即券商口径
        「最大可买」，**没有**独立的融资负债/保证金占用字段；
      * 实盘 ``account_funds``：``total_assets`` / ``power`` / ``available_funds`` / ``cash``，
        同样**没有**融资负债字段。

    因此本实现只给「真实净敞口比」（持仓市值 / 总资产）与「购买力可用比」
    （可用购买力 / 总资产）、「现金 / 总资产」，**不给**「融资负债 / 净资产」——
    后者上游不提供，返回 ``null`` 并写明原因。
    """
    funds = funds_envelope if funds_envelope is not None else _call(v3_run, "account_funds", {})
    positions = (positions_envelope if positions_envelope is not None
                 else _call(v3_run, "positions", {}))
    funds_value = _value_of(funds) or {}
    mode = funds_value.get("mode") or ((_value_of(positions) or {}).get("mode"))
    as_of = funds_value.get("as_of") or (_value_of(positions) or {}).get("as_of")

    funds_rows, mv_rows = [], []
    for group in _account_groups(funds):
        chain = _chain_of_group(group)
        if chain is None:
            continue
        if market and chain != market:
            continue
        cash = _parse_cash_fields(group.get("cash"))
        equity = cash.get("total_assets")
        if equity is None:
            equity = cash.get("total_asset")
        if equity is None:
            equity = cash.get("mv")
        power = cash.get("power")
        if power is None:
            power = cash.get("max_power_long")
        if power is None:
            power = cash.get("available_funds")
        funds_rows.append({"market": chain, "acc_id": group.get("acc_id"),
                           "equity": equity, "power": power,
                           "cash": cash.get("cash") if cash.get("cash") is not None
                           else cash.get("balance"),
                           "long_mv": cash.get("long_mv")})
    for group in _account_groups(positions):
        key, symbol = _position_key(group, {})
        if key is None:
            continue
        if market and key != market:
            continue
        total = 0.0
        found = False
        for row in group.get("positions") or []:
            if not isinstance(row, dict):
                continue
            value = v3_math.to_float(row.get("mv"))
            if value is None:
                value = v3_math.to_float(row.get("market_value"))
            if value is None:
                continue
            total += value
            found = True
        if found:
            mv_rows.append({"market": key, "acc_id": group.get("acc_id"), "long_mv": total})

    equity = sum(row["equity"] for row in funds_rows if row.get("equity") is not None) \
        if any(row.get("equity") is not None for row in funds_rows) else None
    # 持仓市值：优先逐持仓行求和（可核对）；持仓接口取不到时退回**资金响应自带的**
    # ``long_mv``（同一份券商数据，实测一致：376020 / 376020），并如实标注用哪一路。
    long_mv = sum(row["long_mv"] for row in mv_rows) if mv_rows else None
    if long_mv is None:
        cash_mv = [row["long_mv"] for row in funds_rows if row.get("long_mv") is not None]
        mv_source = ("资金响应的 long_mv 字段（持仓接口未返回可用市值）" if cash_mv else None)
        long_mv = sum(cash_mv) if cash_mv else None
    else:
        mv_source = "逐持仓行的 mv 字段"
    power = sum(row["power"] for row in funds_rows if row.get("power") is not None) \
        if any(row.get("power") is not None for row in funds_rows) else None
    cash_sum = sum(row["cash"] for row in funds_rows if row.get("cash") is not None) \
        if any(row.get("cash") is not None for row in funds_rows) else None

    readings = {"totalAssets": equity, "longMarketValue": long_mv,
                "buyingPower": power, "cash": cash_sum, "longMarketValueSource": mv_source,
                "accounts": len(funds_rows), "positionAccounts": len(mv_rows)}
    payload = {
        "source": funds_value.get("source") or (_value_of(positions) or {}).get("source"),
        "as_of": as_of, "mode": mode, "market": market,
        "accounts": funds_rows, "readings": readings,
        "leverage_ratio_pct": (_ratio_pct(long_mv, equity)),
        "long_mv_ratio_pct": (_ratio_pct(long_mv, equity)),
        "buying_power_ratio_pct": (_ratio_pct(power, equity)),
        "cash_ratio_pct": (_ratio_pct(cash_sum, equity)),
        "margin_debt_pct": None,
        "direction": "真实净敞口 = 持仓市值 / 总资产；不折算跨币种、不跨市场合并",
        "error": None,
    }
    if equity is None:
        payload["error"] = {
            "code": "leverage/no-equity",
            "message": (f"资金读数里没有可用的总资产字段（上游只给 "
                        f"{sorted(set(key for row in funds_rows for key in row)) or '空'}），"
                        f"杠杆率 = null"),
        }
    payload["provider_note"] = (
        "上游字段事实（2026-09-20 实测）：模拟盘 sim_trade_cash_info 给 balance/hold/"
        "max_power_long/total_asset/mv/long_mv/short_mv，实盘 account_funds 给 "
        "total_assets/power/available_funds/cash；**两者都没有独立的融资负债/保证金占用字段**，"
        "故 margin_debt_pct 恒为 null（no-data，不估算），杠杆读数只有"
        "「持仓市值 / 总资产」与「可用购买力 / 总资产」。")
    payload["note"] = (
        f"读数口径：{'多账户合计（同市场链）' if funds_rows else '无可用账户分组'}"
        f"；总资产来自 {funds_value.get('source') or '—'}，持仓市值来自 "
        f"{(_value_of(positions) or {}).get('source') or '—'}")
    return payload


# ---------------------------------------------------------------------------
# ② 流动性风险（订单金额 / 近 20 日 ADV）
# ---------------------------------------------------------------------------
def liquidity_risk(order_value, bars, *, window=ADV_WINDOW, market=None, ticker=None,
                   warn_pct=LIQUIDITY_WARN_PCT, block_pct=LIQUIDITY_BLOCK_PCT,
                   order_note=None):
    """参与率 = 订单金额 / ADV；ADV = 近 ``window`` 个交易日**日均成交额**。

    ``bars``：既有 K 线链路的日 K（``series`` 工具 ``period=1d``，字段 ``c``/``v``/``t``）。
    成交额按 ``close × volume`` 逐日算（上游不单列成交额），口径写进 ``adv_basis``。
    订单金额缺失 / K 线不足 / ADV 为 0 → ``participation_pct = None`` + 原因。
    """
    value = v3_math.to_float(order_value)
    rows = [bar for bar in (bars or []) if isinstance(bar, dict)]
    window = max(1, int(window))
    turnover = []
    for bar in rows[-window:]:
        close = v3_math.to_float(bar.get("c"))
        volume = v3_math.to_float(bar.get("v"))
        if close is None or volume is None:
            continue
        turnover.append(close * volume)
    payload = {
        "ticker": ticker, "market": market,
        "order_value": value,
        "adv_window_days": window,
        "adv_observations": len(turnover),
        "adv_source": (f"GET /api/v3/factors/matrix 同源的 K 线链路（series period=1d，"
                       f"近 {len(turnover)} 根日 K）" if turnover else None),
        "adv_basis": "ADV = mean(close × volume)，取最近 window 根日 K（上游不单列成交额）",
        "adv_window_from": (str(rows[-window].get("t")) if len(rows) >= window
                            else (str(rows[0].get("t")) if rows else None)),
        "adv_window_to": (str(rows[-1].get("t")) if rows else None),
        "adv_amount": (v3_math.round_half_up(sum(turnover) / len(turnover), 2)
                       if turnover else None),
        "participation_pct": None,
        "grade": "no-data",
        "grade_note": None,
        "thresholds": {"warnPct": warn_pct, "blockPct": block_pct},
        "reason": None,
    }
    if order_note:
        payload["order_note"] = order_note
    if value is None:
        payload["reason"] = "订单金额缺失（order_value=null）→ 参与率无法计算"
        return payload
    if not turnover:
        payload["reason"] = (f"K 线不足 {window} 根可用的 close/volume，无法算 ADV"
                             f"（拿到 {len(rows)} 根）")
        return payload
    if len(turnover) < window:
        # 只用半截窗口算出来的「ADV」不是 20 日均额，会系统性低估/高估参与率 → 如实 no-data
        payload["reason"] = (f"可用成交额样本仅 {len(turnover)} 个 < ADV 窗口 {window} 日，"
                            f"不拿不满窗口的均值冒充 ADV（参与率 = null）")
        return payload
    adv = sum(turnover) / len(turnover)
    if adv <= 0:
        payload["reason"] = f"ADV 非正（{adv}）→ 参与率无意义，返回 null"
        return payload
    participation = value / adv * 100.0
    payload["participation_pct"] = v3_math.round_half_up(participation, 4)
    if participation >= block_pct:
        payload["grade"] = "blocked"
        payload["grade_note"] = (f"参与率 {participation:.2f}% ≥ {block_pct:.0f}%（ADV 窗口 "
                                 f"{len(turnover)} 日）→ 建议拆分/改单后人工确认，禁止一次打满")
    elif participation >= warn_pct:
        payload["grade"] = "manual"
        payload["grade_note"] = (f"参与率 {participation:.2f}% ≥ {warn_pct:.0f}% → 建议人工确认"
                                 f"（冲击成本可能显著）")
    else:
        payload["grade"] = "auto"
        payload["grade_note"] = f"参与率 {participation:.2f}% < {warn_pct:.0f}%，流动性充裕"
    return payload


# ---------------------------------------------------------------------------
# ③ 绩效归因（逐标的 + 行业；因子维度如实缺失）
# ---------------------------------------------------------------------------
def portfolio_attribution(positions_envelope, *, market=None, industry_map=None,
                          industry_missing=None, weights=None, total_return_pct=None):
    """事后绩效归因：按标的、按行业分组；因子分组**如实缺失**（原因写进响应）。

    * ``byTicker``：逐标的 ``pl_val``（券商持仓的未实现盈亏）与 ``mv``；
    * ``contributionPct`` = ``pl_val / equity``（``equity`` = 分组 ``total_asset`` 或
      各持仓 ``mv`` 合计），**不是**时间加权收益——口径原文写进 ``methods``；
    * ``byIndustry``：用 ``industry_map``（``{MARKET.CODE: 行业名}``，来自富途板块链路）；
      取不到行业的标的进 ``industryMissing`` 并单独汇总，**不并入某个行业**；
    * ``byFactor``：恒为 ``null`` + ``factor_reason``（缺 PIT 建仓因子敞口）。
    """
    value = _value_of(positions_envelope) or {}
    by_ticker, by_industry = [], {}
    equity = 0.0
    equity_found = False
    missing = list(industry_missing or [])
    for group in _account_groups(positions_envelope):
        chain = _chain_of_group(group)
        if chain is None or (market and chain != market):
            continue
        declared = v3_math.to_float(group.get("total_asset"))
        if declared is not None:
            equity += declared
            equity_found = True
        for row in group.get("positions") or []:
            if not isinstance(row, dict):
                continue
            chain_of_row, key = _position_key(group, row)
            if chain_of_row is None or (market and chain_of_row != market):
                continue
            mv = v3_math.to_float(row.get("mv"))
            if mv is None:
                mv = v3_math.to_float(row.get("market_value"))
            pl = v3_math.to_float(row.get("pl_val"))
            if pl is None:
                pl = v3_math.to_float(row.get("unrealized_profit"))
            ratio = v3_math.to_float(row.get("pl_ratio"))
            if ratio is None:
                ratio = v3_math.to_float(row.get("profit_ratio"))
            entry = {"ticker": key, "market": chain_of_row,
                     "name": row.get("stock_name") or row.get("name"),
                     "marketValue": None if mv is None else v3_math.round_half_up(mv, 2),
                     "plValue": None if pl is None else v3_math.round_half_up(pl, 2),
                     "plRatioPct": None if ratio is None else v3_math.round_half_up(ratio, 4)}
            industry = (industry_map or {}).get(key)
            entry["industry"] = industry
            if industry is None:
                missing.append({"ticker": key, "reason": "行业映射缺失（full report）"})
            else:
                bucket = by_industry.setdefault(industry, {
                    "industry": industry, "plValue": 0.0, "marketValue": 0.0, "tickers": []})
                bucket["plValue"] += pl or 0.0
                bucket["marketValue"] += mv or 0.0
                bucket["tickers"].append(key)
            by_ticker.append(entry)

    if not equity_found:
        for entry in by_ticker:
            equity += entry["marketValue"] or 0.0
    total_pl = sum(entry["plValue"] or 0.0 for entry in by_ticker)
    for entry in by_ticker:
        entry["contributionPct"] = _ratio_pct(entry["plValue"], equity)
    industries = []
    for bucket in by_industry.values():
        industry_total_pl = sum(entry["plValue"] or 0.0
                                for entry in by_ticker
                                if entry.get("industry") == bucket["industry"])
        industries.append({
            "industry": bucket["industry"],
            "plValue": v3_math.round_half_up(industry_total_pl, 2),
            "marketValue": v3_math.round_half_up(bucket["marketValue"], 2),
            "tickers": sorted(bucket["tickers"]),
            "contributionPct": _ratio_pct(industry_total_pl, equity),
        })
    industries.sort(key=lambda item: (item["contributionPct"] is None,
                                      -(item["contributionPct"] or 0.0)))
    by_ticker.sort(key=lambda item: (item["contributionPct"] is None,
                                     -(item["contributionPct"] or 0.0)))

    payload = {
        "basis": "券商持仓未实现盈亏（``pl_val``）",
        "as_of": value.get("as_of"),
        "source": value.get("source"),
        "mode": value.get("mode"),
        "market": market,
        "equity": None if not equity else v3_math.round_half_up(equity, 2),
        "totalPlValue": v3_math.round_half_up(total_pl, 2),
        "totalContributionPct": _ratio_pct(total_pl, equity),
        "positions": len(by_ticker),
        "byTicker": by_ticker,
        "byIndustry": industries,
        "industryMissing": missing,
        "byFactor": None,
        "byPortfolio": None if total_return_pct is None else total_return_pct,
        "methods": {
            "ticker": ATTRIBUTION_TICKER_METHOD,
            "industry": ATTRIBUTION_INDUSTRY_METHOD,
            "industrySource": ATTRIBUTION_INDUSTRY_SOURCE if industry_map else None,
            "factor": ATTRIBUTION_FACTOR_REASON,
        },
        "coverage": {
            "positions": len(by_ticker),
            "withIndustry": sum(1 for entry in by_ticker if entry.get("industry")),
            "industryMissing": len([row for row in missing if row.get("ticker")]),
            "note": ("覆盖 = 本市场分组里有持仓行且能取到行业映射的部分；"
                     "取不到行业的持仓仍给出逐标的贡献，但不并入任何行业"),
        },
        "note": ("归因只用券商持仓的未实现盈亏与市值字段（不折算跨币种、不跨市场合并）；"
                 "因子归因因缺 PIT 建仓敞口缺失（见 methods.factor）"),
    }
    if not by_ticker:
        payload["error"] = {"code": "attribution/no-positions",
                            "message": "本市场分组里没有持仓行 → 归因无数据"}
    return payload


# ---------------------------------------------------------------------------
# ④ 资金检查（事前风控：订单金额 vs 真实可用资金/购买力）
# ---------------------------------------------------------------------------
def funding_check_data(v3_run, *, order_value=None, symbol=None, side=None, qty=None,
                       price=None, market=None, funds_envelope=None):
    """下单金额 vs **真实**可用资金/购买力（``account_funds``；模拟盘为 max_power_long）。

    返回 ``{ok, action, reason, orderValue, readings:{...}, funding_basis, ...}``：

      * ``action="blocked"``：订单金额 > 该市场可用购买力（**读数与字段名一并给出**）；
      * ``action="noted"``：资金充足（读数仍完整返回，供页面留痕）；
      * ``action="unknown"``：资金/购买力取不到或订单金额算不出 → **不改既有闸门语义**，
        只如实上报（调用方按 ``reason`` 决定是否退回人工确认）。

    **只读**：只调 ``account_funds``；不触任何写/交易端点，不做任何用户确认。
    """
    value = v3_math.to_float(order_value)
    if value is None:
        qty_number = v3_math.to_float(qty)
        price_number = v3_math.to_float(price)
        if qty_number is not None and price_number is not None:
            value = qty_number * price_number
    if value is None and (qty is not None or price is not None):
        reason = "下单金额算不出：qty/price 需同时为数值（或用 order_value 直接给金额）"
        return {"ok": False, "orderValue": None, "action": "unknown", "reason": reason,
                "error": {"code": "funds/bad-order", "message": reason}}

    envelope = funds_envelope if funds_envelope is not None else _call(v3_run, "account_funds", {})
    funds_value = _value_of(envelope) or {}
    rows = []
    for group in _account_groups(envelope):
        chain = _chain_of_group(group)
        if chain is None:
            continue
        cash = _parse_cash_fields(group.get("cash"))
        reading = {
            "market": chain, "acc_id": group.get("acc_id"),
            "totalAssets": cash.get("total_assets", cash.get("total_asset")),
            "buyingPower": cash.get("power", cash.get("max_power_long")),
            "availableFunds": cash.get("available_funds"),
            "cash": cash.get("cash", cash.get("balance")),
            "buyingPowerField": ("power" if cash.get("power") is not None
                                 else ("max_power_long" if cash.get("max_power_long") is not None
                                       else None)),
        }
        rows.append(reading)
    scoped = [row for row in rows if not market or row["market"] == market]
    if market and not scoped:
        available = sorted({row["market"] for row in rows})
        reason = (f"market={market} 没有对应的券商资金分组"
                  f"（账户列表里的市场：{'/'.join(available) or '空'}）")
        return {"ok": False, "orderValue": value, "action": "unknown", "reason": reason,
                "market": market, "accounts": rows,
                "error": {"code": "funds/no-account", "message": reason}}

    basis_values = [row["buyingPower"] for row in scoped if row["buyingPower"] is not None]
    cash_values = [row["cash"] for row in scoped if row["cash"] is not None]
    basis_total = sum(basis_values) if basis_values else None
    cash_total = sum(cash_values) if cash_values else None
    payload = {
        "ok": True,
        "market": market,
        "mode": funds_value.get("mode"),
        "source": funds_value.get("source"),
        "as_of": funds_value.get("as_of"),
        "orderValue": None if value is None else v3_math.round_half_up(value, 2),
        "symbol": symbol, "side": side,
        "readings": {"totalAssets": sum(row["totalAssets"] for row in scoped
                                        if row["totalAssets"] is not None) or None,
                     "buyingPower": basis_total, "cash": cash_total},
        "accounts": scoped,
        "funding_basis": "券商资金字段 max_power_long（sim）/ power（live），按市场分组求和",
        "funding_basis_field": sorted({row["buyingPowerField"] for row in scoped
                                       if row["buyingPowerField"]}),
        "basisNote": ("购买力只按**同市场链**的账户求和（不跨市场/币种折算）；"
                      "权益（total_asset/total_assets）不作可用资金"),
        "action": "unknown", "reason": None,
    }
    if value is None:
        payload["reason"] = "未提供订单金额（order_value 或 qty×price 至少给一个）"
        return payload
    if basis_total is None:
        payload["reason"] = (f"上游资金读数里没有购买力字段（max_power_long/power/available_funds），"
                             f"只有 {sorted({key for row in scoped for key in row})} → 资金检查 no-data，"
                             f"不改既有闸门语义")
        return payload
    payload["shortfall"] = (v3_math.round_half_up(value - basis_total, 2)
                            if value > basis_total else 0.0)
    if value > basis_total:
        payload["action"] = "blocked"
        payload["reason"] = (f"订单金额 {value:.2f} > 该市场可用购买力 {basis_total:.2f}"
                             f"（字段 {payload['funding_basis_field']}，来源 "
                             f"{funds_value.get('source') or '—'}，as_of "
                             f"{funds_value.get('as_of') or '—'}）→ 资金不足，建议退回人工/阻断")
    else:
        payload["action"] = "noted"
        payload["reason"] = (f"订单金额 {value:.2f} ≤ 可用购买力 {basis_total:.2f}"
                             f"（字段 {payload['funding_basis_field']}）→ 资金充足（仅留痕，不改既有判定）")
    return payload


# ---------------------------------------------------------------------------
# FR-STRAT-001 补全：因子注册表（六类因子 + 真实数据源 + PIT 口径 + 覆盖率）
# ---------------------------------------------------------------------------
#: 因子注册表：每个因子一条「真实数据源 + PIT 口径 + 方向」。**这是唯一事实来源**——
#: ``/api/v3/factors/registry`` 与矩阵的 ``coverage`` 都从这里生成，不在别处再抄一份。
#: ``class`` 取规格 FR-STRAT-001 的六类：value/momentum/quality/growth/sentiment/alternative。
FACTOR_REGISTRY = tuple([
    {"key": "mom_20", "class": "momentum", "direction": 1,
     "source": "workbench/factors（日 K 动量，series→factors 工具链）",
     "pit": "只用 ≤t 的日 K（因子在 t 收盘后可得）"},
    {"key": "mom_60", "class": "momentum", "direction": 1,
     "source": "workbench/factors（日 K 动量）", "pit": "只用 ≤t 的日 K"},
    {"key": "vol_20", "class": "momentum", "direction": -1,
     "source": "workbench/factors（20 日年化波动）", "pit": "只用 ≤t 的日 K"},
    {"key": "trend", "class": "momentum", "direction": 1,
     "source": "workbench/factors（close/MA20−1）", "pit": "只用 ≤t 的日 K"},
    {"key": "rsi_14", "class": "momentum", "direction": -1,
     "source": "workbench/factors（RSI14）", "pit": "只用 ≤t 的日 K"},
    {"key": "liq_ratio", "class": "alternative", "direction": 1,
     "source": "workbench/factors（20 日均量 / 60 日均量）", "pit": "只用 ≤t 的日 K 量"},
    {"key": "mdd_60", "class": "momentum", "direction": 1,
     "source": "workbench/factors（60 日最大回撤）", "pit": "只用 ≤t 的日 K"},
    {"key": "pe_ttm", "class": "value", "direction": -1,
     "source": "trading_core.factors（富途估值快照，失败退同花顺）",
     "pit": "估值快照按取得时点；历史分位序列见 pe_ttm_pct"},
    {"key": "pb", "class": "value", "direction": -1,
     "source": "trading_core.factors（富途估值快照）", "pit": "同上"},
    {"key": "ps", "class": "value", "direction": -1,
     "source": "trading_core.factors（富途估值快照）", "pit": "同上"},
    {"key": "peg", "class": "value", "direction": -1,
     "source": "trading_core.factors（富途估值快照）", "pit": "同上"},
    {"key": "pe_ttm_pct", "class": "value", "direction": -1,
     "source": "trading_core.factors（富途历史分位）", "pit": "分位只用 ≤t 的历史"},
    {"key": "pb_pct", "class": "value", "direction": -1,
     "source": "trading_core.factors（富途历史分位）", "pit": "同上"},
    {"key": "ps_pct", "class": "value", "direction": -1,
     "source": "trading_core.factors（富途历史分位）", "pit": "同上"},
    # ── 质量（FR-STRAT-001 缺失类 ①；实现口径与 plugins/workbench/python/quality.py 同源）──
    {"key": "gross_margin", "class": "quality", "direction": 1,
     "source": "trading-data/fundamentals（futu/statements 毛利/营收，公告日由 akshare/yjbb 合并）",
     "pit": "只认 announced_at 非空且 ≤t 的行（报告期不可当可得日）"},
    {"key": "net_margin", "class": "quality", "direction": 1,
     "source": "trading-data/fundamentals（futu/statements 净利/营收）",
     "pit": "同上"},
    {"key": "roe", "class": "quality", "direction": 1,
     "source": "trading-data/fundamentals.roe（v3_fundamentals_sync，Yahoo 季度净利/权益，%）",
     "pit": "最新可见报告期，announced_at 非空且 ≤t；A 股真实公告日，非 A 股同步日保守可得；不跨期补值、不年化"},
    {"key": "roa", "class": "quality", "direction": 1,
     "source": "trading-data/fundamentals.roa（v3_fundamentals_sync，Yahoo 季度净利/总资产，%）",
     "pit": "最新可见报告期，announced_at 非空且 ≤t；A 股真实公告日，非 A 股同步日保守可得；不跨期补值、不年化"},
    # ── 成长（缺失类 ②）──
    {"key": "revenue_yoy", "class": "growth", "direction": 1,
     "source": "trading-data/fundamentals（同报告期同比，2026-06-30 ↔ 2025-06-30）",
     "pit": "两个报告期都须有 announced_at ≤t 的行；缺同期基期 → null（不跨期推算）"},
    {"key": "net_profit_yoy", "class": "growth", "direction": 1,
     "source": "trading-data/fundamentals（同报告期净利润同比）", "pit": "同上"},
    # ── 情绪（缺失类 ③）──
    {"key": "sentiment", "class": "sentiment", "direction": 1,
     "source": "store.sentiment_snapshots（sentiment_snapshot 作业落库的 fin_sentiment/fin_news "
               "原文）∨ akshare/stock_news_em 实时资讯（仅两条矩阵路由；经 server.data.cache "
               "PIT 闸门）+ server.v3_nlp.score_documents 同一份打分器；逐标的来源见 "
               "sentimentSources；在线口径见 GET /api/v3/sentiment",
     "pit": "快照：date ≤t 且文档发布时间在 [t−7d, t] 内；实时：文档发布时间经 as_of（t）+ "
            "INCLUSIVE 闸门，晚于 t 的资讯被挡掉并计数；两者都用 t 当日结束 UTC 做时间半衰"},
    # ── 另类（缺失类 ④）──
    {"key": "capital_flow", "class": "alternative", "direction": 1,
     "source": "futu/capital_flow_history（近 20 个交易日主力净流入 / Σ|净额|）",
     "pit": "上游按日聚合的资金流水；只用请求时刻已发布的交易日"},
    {"key": "short_interest", "class": "alternative", "direction": -1,
     "source": "futu/short_interest（空头占比；仅 HK/US 可卖空证券，A 股上游无数据）",
     "pit": "上游返回的最新一期（PIT 由上游披露节奏决定，响应里带 as_of）"},
])

#: 因子类别 → 中文标签（页面与响应共用，避免两边各写一份）。
FACTOR_CLASS_LABELS = {"value": "价值", "momentum": "动量", "quality": "质量",
                       "growth": "成长", "sentiment": "情绪", "alternative": "另类"}


def factor_registry_data(matrix=None):
    """因子注册表 + **真实覆盖率**（每类因子有几个标的真的有值）。

    ``matrix`` 给定时（:func:`factors_matrix_data` 的 ``matrix``）用它统计覆盖；
    不给则只返回注册表本身（``coverage.available=false`` + 原因）。
    """
    coverage = []
    raw_rows = (matrix or {}).get("raw") if isinstance(matrix, dict) else None
    if not isinstance(raw_rows, list):
        return {"registry": [dict(entry) for entry in FACTOR_REGISTRY],
                "classes": dict(FACTOR_CLASS_LABELS),
                "coverage": {"available": False,
                             "reason": "未提供因子矩阵（先请求 /api/v3/factors/matrix）",
                             "factors": []}}
    by_ticker = {row.get("ticker"): (row.get("factors") or {})
                 for row in raw_rows if isinstance(row, dict)}
    for entry in FACTOR_REGISTRY:
        key = entry["key"]
        present = [ticker for ticker, values in by_ticker.items()
                   if values.get(key) is not None]
        coverage.append({
            "key": key, "class": entry["class"], "classLabel": FACTOR_CLASS_LABELS[entry["class"]],
            "covered": len(present), "total": len(by_ticker),
            "coveragePct": (v3_math.round_half_up(len(present) / len(by_ticker) * 100.0, 2)
                            if by_ticker else None),
            "tickers": sorted(present),
            "missingTickers": sorted(ticker for ticker in by_ticker if ticker not in present),
            "source": entry["source"], "pit": entry["pit"],
            "direction": entry["direction"],
        })
    return {"registry": [dict(entry) for entry in FACTOR_REGISTRY],
            "classes": dict(FACTOR_CLASS_LABELS),
            "coverage": {"available": True, "tickers": sorted(by_ticker), "factors": coverage}}


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------
def _ok(content):
    """所有 V3 分析接口都以 200 + 信封应答（``ok=false`` 由前端按错误提示展示）。"""
    return JSONResponse(status_code=200, content=content)


def _route_sentiment_fetch(app):
    """路由层的实时资讯取数接缝：``app.state.v3_sentiment_live_fetch``（缺省 ``None``）。

    与 ``v3_nlp.register`` 把 ``deps`` 挂进 ``app.state.v3_nlp`` 同一手法：生产不需要注入
    （``None`` → :func:`default_news_fetch` 走既有 akshare 通道），单测注入假资讯源后
    整条路由**不触网**。注入物签名与 :func:`default_news_fetch` 一致：
    ``(home, ticker, limit) -> (零参取数函数|None, note)``。
    """
    state = getattr(app, "state", None)
    fetch = getattr(state, "v3_sentiment_live_fetch", None)
    return fetch if callable(fetch) else None


def register(app, v3_run, home):
    """把 5 个分析类契约（6 条路由）挂到 FastAPI ``app`` 上。

    ``app.py`` 在静态兜底路由之前调用本函数；``v3_run`` 与 ``home`` 都是注入进来的，
    因此这里没有任何模块级全局状态，同一进程挂两次也不会互相污染（各自闭包独立）。

    末尾**代挂** ``v3_strategies.register``（FR-STRAT-002 事件驱动 + 统计套利两条路由）：
    ``app.py`` 的 V3 接线清单是固定模块列表（不在本任务改动范围），策略族与分析类同属
    「V3 只读研究面」，由本函数代挂后同样先于静态兜底注册、同样进 MCP 桥的路由推导。
    """

    @app.get("/api/v3/risk/analytics")
    async def v3_risk_analytics(limit: int = 250, confidence: float = 0.95,
                                benchmark: Optional[str] = None,
                                weights: Optional[str] = None, market: str = "SH",
                                details: bool = True):
        """组合风险量：history-simulation VaR/CVaR + Beta/Alpha/IR + Kupiec POF + 净值曲线。

        ``market`` 缺省 ``SH``（保持既有 A 股口径）；组合＝该市场宇宙等权（或该市场
        frozen 计划目标）；该市场无宇宙 → ``market/no-universe``。``benchmark`` 缺省
        按市场实测选取（SH.000300 / HK.800000 / US.SPY，逐级降级；全不可用 → null 且
        beta/alpha/ir 为 null）；显式给出时原样使用。

        ``details``（缺省 ``true``）：附带 FR-EXEC-003 补全块 ``risk_detail``（杠杆率 /
        流动性风险 / 绩效归因 / 资金检查；全部只读）。``details=false`` 时响应与历史逐字段
        一致，便于只要风险量的调用方省掉额外的账户读数。
        """
        def work():
            return risk_analytics(v3_run, home, limit=limit, confidence=confidence,
                                  benchmark=benchmark, weights_raw=weights, market=market,
                                  details=bool(details))

        return _ok(await asyncio.to_thread(work))

    @app.get("/api/v3/factors/matrix")
    async def v3_factors_matrix(tickers: Optional[str] = None, factor: str = "mom_20",
                                forward_days: Optional[int] = None,
                                forward: Optional[int] = None, market: str = "SH",
                                classes: str = "", as_of: str = ""):
        """横截面因子 z 矩阵 + 因子 IC 序列 + 六类因子的真实覆盖率（``factors`` 字段）。

        ``market`` 缺省 ``SH``：未给 ``tickers`` 时标的取该市场宇宙（与 watchlist 同一份
        解析）；显式 ``tickers`` 优先（此时 ``market`` 仅作标注）。``classes`` 选择要并入的
        新因子类别（``quality,growth,sentiment`` 缺省；``all`` 含 ``alternative`` 实时取数；
        ``none`` 只要价量/估值列）。``as_of``（``YYYY-MM-DD``）是 PIT 上界，缺省今天（UTC）。

        情绪列在本路由**打开实时兜底**（``live_sentiment=True``）：快照取不到分的标的再走
        ``v3_nlp`` 实时打分（经 ``server.data.cache`` 的 PIT 闸门，显式 ``as_of``），
        逐标的来源见 ``sentimentSources``。测试用 ``app.state.v3_sentiment_live_fetch``
        注入假资讯源（缺省 = 既有 ``v3_sources.fetch_news`` 通道）。
        """
        chosen_forward = forward if forward is not None else forward_days

        def work():
            return factors_matrix_data(v3_run, home, tickers_raw=tickers, factor=factor,
                                       forward_days=chosen_forward if chosen_forward is not None else 5,
                                       market=market, classes=classes or None,
                                       as_of=as_of or None, live_sentiment=True,
                                       sentiment_fetch=_route_sentiment_fetch(app))

        return _ok(await asyncio.to_thread(work))

    @app.get("/api/v3/factors/registry")
    async def v3_factors_registry(tickers: Optional[str] = None, market: str = "SH",
                                 as_of: str = "", classes: str = ""):
        """因子注册表（**六类因子 + 真实数据源 + PIT 口径**）+ 逐因子覆盖率。

        覆盖率与 ``/api/v3/factors/matrix`` **同一份计算**（不为页面另造一套统计）；
        默认只算本地三类（quality/growth/sentiment），``classes=all`` 才把实时另类因子
        （``capital_flow``/``short_interest``）一起取。情绪列的实时兜底与矩阵路由同一口径
        （``live_sentiment=True``），逐标的来源见响应 ``sentimentSources``。
        """
        def work():
            matrix = factors_matrix_data(v3_run, home, tickers_raw=tickers, factor="mom_20",
                                         forward_days=5, market=market,
                                         classes=classes or None, as_of=as_of or None,
                                         live_sentiment=True,
                                         sentiment_fetch=_route_sentiment_fetch(app))
            if not matrix.get("ok"):
                return matrix
            registry = factor_registry_data(matrix.get("matrix"))
            return {"ok": True, "market": matrix.get("market"), "asOf": matrix.get("asOf"),
                    "classes": matrix.get("classes"),
                    "registry": registry["registry"], "classLabels": registry["classes"],
                    "coverage": registry["coverage"],
                    "factorsMissing": matrix.get("factorsMissing"),
                    "sentimentSources": matrix.get("sentimentSources"),
                    "sources": matrix.get("sources")}

        return _ok(await asyncio.to_thread(work))

    @app.get("/api/v3/risk/funding-check")
    async def v3_risk_funding_check(order_value: Optional[float] = None,
                                    symbol: str = "", side: str = "",
                                    qty: Optional[float] = None, price: Optional[float] = None,
                                    market: str = ""):
        """**事前风控·资金检查（只读）**：订单金额 vs 真实可用购买力（``account_funds``）。

        金额 = ``order_value``，或 ``qty × price``。``market`` 给定时只按该市场链的账户分组
        求和（不跨币种折算）。资金不足 → ``action="blocked"`` + 读数与来源；不够读数 →
        ``action="unknown"`` + 原因（**不改既有下单前闸门语义**，只作读数与分级建议）。
        """
        def work():
            code = None
            if str(market or "").strip():
                code = v3_universe.normalize_market(market)
                if code is None:
                    return _error("market/bad-market", "market 需为 SH / HK / US",
                                  market=str(market))
            return funding_check_data(v3_run, order_value=order_value, symbol=symbol or None,
                                      side=side or None, qty=qty, price=price, market=code)

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

    # FR-STRAT-002 策略族补全（事件驱动 + 统计套利）：app.py 的接线清单不在本任务改动
    # 范围，由分析类模块代挂（延迟 import，避免模块级环）。注册失败不该拖垮分析类路由。
    try:
        from server import v3_strategies

        v3_strategies.register(app, v3_run, home)
    except Exception as error:  # pragma: no cover —— 接线失败要可见，不能静默吞掉
        import sys

        print(f"v3_strategies register failed: {type(error).__name__}: {error}",
              file=sys.stderr)
