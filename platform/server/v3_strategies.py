"""V3 **策略族补全**（FR-STRAT-002）：事件驱动策略与统计套利策略（只读研究，不出订单）。

规格原文（``docs/v3-spec.md:301``）::

    支持多因子选股策略、机器学习策略（Lasso/LightGBM/MLP）、事件驱动策略、统计套利策略。
    参数优化支持数千次完整回测和热力图可视化。

多因子（``v3_analytics.strategy_run``）与机器学习（``v3_ml.ml_models``）已在前几轮落地；
本模块补齐缺的两个策略族：

===========================================  ======  ==============================
路径                                          方法    策略族
===========================================  ======  ==============================
``/api/v3/strategies/event-study``           GET     事件驱动（公告后漂移/事件窗）
``/api/v3/strategies/stat-arb``              GET     统计套利（协整价差回归）
===========================================  ======  ==============================

两条都是**只读研究端点**：输出统计与研究结论，不产生任何订单/提案执行——执行仍走工作台
受约束入口。接线：``app.py`` 的 V3 子模块自动接线循环只认识固定的模块清单（那份清单在
``app.py``，本任务不改），因此本模块由 ``v3_analytics.register`` **代挂**（分析类同族，
``v3_run``/``home`` 原样注入）；注册发生在静态兜底路由之前，MCP 桥按路由表推导工具，
「路由 ⇄ 工具」双射由 ``v3_mcp`` 构造保证。

事件源（真实、可核验；事件必须带**可核验的公告时点**，取不到时点的事件不得入样）：

① 富途 ``events`` 工具链（公告/事件时间线，既有）：分红/除权除息事件带 ``announced``
   （公告日 ``pub_date``/公告日期）；**没有 ``announced`` 的事件（如财报披露预约的
   「预约日」不是公告时点）一律不入样**，逐源计数（``droppedNoAnnounceTime``）。
② ``server.v3_nlp.classify_events``（NLP 事件识别，另一工作流在建）：**守卫导入**——
   缺席/失败 = 该源 0 事件 + 原因如实写进响应（``sources.nlp``），绝不硬凑。

PIT 纪律（与 FR-DATA-003 一致）：

* K 线读取**只**经 ``server.data.cache.read_bars``（``fetch`` = ``series`` 工具），
  ``as_of`` = 今天（UTC）、``AS_OF_INCLUSIVE``；未来行被闸门挡掉并计数
  （``rejectedFuture`` 逐标的可见）。
* 事件日 ``t`` = 公告日；公告在 ``t`` 日收盘后才可知 ⇒ ``t`` 日**不建仓**，次日
  （首个 ``t' > t`` 的交易日）收盘建仓，持有 ``H`` 个交易日平仓。任何一天用到的信息
  都不晚于决策时点——回测里没有一条收益用了决策前的价格。
* 事件窗内数据不足（公告晚于最后一根 K 线 / 前瞻不足 ``H`` 根）→ 该事件按
  ``noForwardWindow`` 排除并计数，**不截短窗口硬算**。

统计套利方法（numpy 实现；venv 无 statsmodels，``impl`` 字段如实标注）：

同市场标的对 → 训练窗 OLS 对冲比率（对数价格，无截距）→ 残差 DF/ADF 平稳性检验
（t 统计 + MacKinnon(1994) 响应面**近似** p 值，见 ``v3_math.ADF_IMPL_NOTE``）→
候选对按 p 升序；**p ≥ 0.05 的对一律不入选**——检验就是检验，没有协整对就如实说
「无协整对」，绝不把检验写死成恒通过。入选对在**样本外测试窗**回测：价差 z-score
（滚动 ``z_window``、只用 ≤ t-1 的价差）``|z| ≥ z_in`` 进、``|z| ≤ z_out`` 平，
双腿按 ``Δpos`` 计双边成本。半衰期为 AR(1) 口径。
"""

import asyncio
import itertools
import math
from typing import Optional

from starlette.responses import JSONResponse

from server import v3_math, v3_universe
from server.data import cache as pit_cache

# v3_analytics 只在本模块**函数内**延迟使用其信封小工具（_call/_error/...）：
# v3_analytics.register 会延迟 import 本模块，模块顶层互相 import 会成环。
#: 事件驱动宇宙上限（与 FACTOR_LIMIT 同档：横截面研究不超过 8 只，取数受全局限流约束）。
EVENT_UNIVERSE_LIMIT = 8
#: 统计套利宇宙上限（8 只 → 28 个候选对，训练窗 OLS 全量可算）。
PAIR_UNIVERSE_LIMIT = 8
#: 对齐后交易日下限：少于它连「训练 60 + 测试 20」都凑不出 → statarb/insufficient。
MIN_ALIGNED_DAYS = 80
#: 训练窗下限（ADF 在更短的窗上没有意义）与测试窗下限（少于 20 天的样本外无说服力）。
MIN_TRAIN_DAYS = 60
MIN_TEST_DAYS = 20
#: ADF 显著性门槛（协整入选线）；检验不显著就是「无协整对」，不放宽。
ADF_PASS_P = 0.05
#: 事件驱动策略的缺省参数（写进响应，可被查询参数覆盖）。
DEFAULT_EVENT_DAYS = 730
DEFAULT_HORIZON = 5
DEFAULT_MIN_EVENTS = 5

__all__ = [
    "ADF_PASS_P",
    "DEFAULT_HORIZON",
    "DEFAULT_MIN_EVENTS",
    "EVENT_UNIVERSE_LIMIT",
    "MIN_ALIGNED_DAYS",
    "PAIR_UNIVERSE_LIMIT",
    "event_study",
    "register",
    "stat_arb",
]


# ---------------------------------------------------------------------------
# 信封小工具（与 v3_analytics 同一份实现——延迟 import 复用，不另造第二套口径）
# ---------------------------------------------------------------------------
def _analytics():
    from server import v3_analytics

    return v3_analytics


def _error(code, message, **extra):
    return _analytics()._error(code, message, **extra)


def _call(v3_run, name, payload):
    return _analytics()._call(v3_run, name, payload)


def _value_of(envelope):
    return _analytics()._value_of(envelope)


def _tool_error(envelope):
    return _analytics()._tool_error(envelope)


def _clamp_int(value, default, minimum, maximum):
    return _analytics()._clamp_int(value, default, minimum, maximum)


def _clamp_float(value, default, minimum, maximum):
    number = v3_math.to_float(value)
    if number is None:
        return default
    return max(minimum, min(maximum, number))


def _pit_date(as_of):
    return _analytics()._pit_date(as_of)


def _ok(content):
    return JSONResponse(status_code=200, content=content)


def _parse_day(value):
    """``YYYY-MM-DD`` 前缀的严格解析（公告时点必须可核验）；解析不了 → ``None``。"""
    if value in (None, ""):
        return None
    text = str(value).strip()[:10]
    try:
        import datetime as _dt

        _dt.datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


# ---------------------------------------------------------------------------
# PIT K 线读取
# ---------------------------------------------------------------------------
def _load_pit_bars(v3_run, tickers, limit, *, as_of=None, mode=None):
    """多标的日 K 的 PIT 信封——**唯一实现在** ``v3_analytics.load_pit_bars``（不复制）。"""
    return _analytics().load_pit_bars(v3_run, tickers, limit, as_of=as_of, mode=mode)


# ---------------------------------------------------------------------------
# 事件源
# ---------------------------------------------------------------------------
def collect_futu_events(v3_run, tickers, days):
    """事件源①：富途 ``events`` 工具链。返回 ``(events, sources_report)``。

    只有带**可核验公告时点**（``announced`` 能解析成 ``YYYY-MM-DD``）的事件才入样；
    缺时点的逐标的计数（``droppedNoAnnounceTime``）——披露预约的「预约日」不是公告时点，
    不能假装它是。
    """
    events = []
    report = {}
    for ticker in tickers:
        envelope = _call(v3_run, "events", {"ticker": ticker, "days": days})
        if not envelope.get("ok"):
            report[ticker] = {"ok": False, "error": _tool_error(envelope)}
            continue
        value = _value_of(envelope) or {}
        kept = 0
        dropped = 0
        for item in (value.get("events") or []):
            if not isinstance(item, dict):
                continue
            announced = _parse_day(item.get("announced"))
            if announced is None:
                dropped += 1
                continue
            events.append({
                "ticker": ticker,
                "announced": announced,
                "type": str(item.get("type") or "未分类"),
                "detail": str(item.get("detail") or "")[:120],
                "source": str(item.get("source") or "futu/events"),
            })
            kept += 1
        report[ticker] = {"ok": True, "events": kept, "droppedNoAnnounceTime": dropped,
                          "upstreamStatus": value.get("sources_status")}
    return events, report


def collect_nlp_events(home):
    """事件源②：``server.v3_nlp.classify_events``（**守卫导入**；缺席 = 0 事件 + 说明）。

    该工具由另一工作流在建，签名尚未冻结：这里做**防御性调用**——返回列表或
    ``{"events": [...]}`` 都认，条目按 ``ticker/symbol`` + ``announced/published_at/
    time/date`` + ``type/label`` 归一；任何失败（缺属性/TypeError/字段缺时点）都只
    记原因，不拖垮事件研究。
    """
    try:
        from server import v3_nlp
    except Exception as error:  # pragma: no cover —— v3_nlp 常在，防御性保留
        return [], {"ok": False, "events": 0, "note": f"导入 server.v3_nlp 失败：{error}"}
    classify = getattr(v3_nlp, "classify_events", None)
    if not callable(classify):
        return [], {"ok": False, "events": 0,
                    "note": "server.v3_nlp.classify_events 尚未实现（另一工作流在建），"
                            "本源不产生事件——如实说明，不硬凑"}
    try:
        raw = classify()
    except Exception as error:
        return [], {"ok": False, "events": 0,
                    "note": f"classify_events 调用失败：{type(error).__name__}: {error}"}
    if isinstance(raw, dict):
        raw = raw.get("events")
    events = []
    dropped = 0
    for item in (raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("ticker") or item.get("symbol") or item.get("code") or "").strip()
        announced = _parse_day(item.get("announced") or item.get("published_at")
                               or item.get("time") or item.get("date"))
        if not ticker or announced is None:
            dropped += 1
            continue
        events.append({"ticker": ticker, "announced": announced,
                       "type": str(item.get("type") or item.get("label") or "nlp_event"),
                       "detail": str(item.get("detail") or item.get("title") or "")[:120],
                       "source": "v3_nlp/classify_events"})
    note = None
    if dropped:
        note = f"{dropped} 条缺标的或缺可核验时点，未入样"
    return events, {"ok": True, "events": len(events), "note": note}


def _dedupe_events(events):
    """同标的 + 同公告日 + 同类型去重（富途与 AKShare/NLP 源可能覆盖同一公告）。"""
    seen = set()
    out = []
    for event in events:
        key = (event.get("ticker"), event.get("announced"), event.get("type"))
        if key in seen:
            continue
        seen.add(key)
        out.append(event)
    out.sort(key=lambda item: (item.get("announced") or "", item.get("ticker") or ""))
    return out


# ---------------------------------------------------------------------------
# GET /api/v3/strategies/event-study
# ---------------------------------------------------------------------------
def event_study(v3_run, home=None, *, tickers_raw=None, market="SH", days=DEFAULT_EVENT_DAYS,
                horizon=DEFAULT_HORIZON, min_events=DEFAULT_MIN_EVENTS, limit=500):
    """事件驱动策略（公告后漂移/事件窗口）——只读研究，不出订单。

    契约::

        {ok, market, universeSource, params, sources, pit,
         events: [{ticker, type, announced, detail, source, status,
                   entryDate?, exitDate?, retPct?, entryClose?, exitClose?}],
         summary: {sampled, measured, excluded, meanRetPct, winRatePct,
                   caarPct, byType: [...]},
         carCurve: [{offset, aarPct, carPct, n}],
         equityCurve: [{t, value, active}],
         sample: {minEvents, sufficient, note}}

    口径（全部写进响应，可核验）：

      * 事件日 ``t`` = 公告日（PIT：``t`` 当日收盘后可知）⇒ 次日收盘建仓、持有 ``H``
        个交易日平仓；``retPct = close[exit]/close[entry] - 1``。
      * ``carCurve``：经典等权 CAR——``AAR(k)`` = 各事件第 k 日累计收益的均值，
        ``CAR(k) = Σ AAR``（未做市场调整，无基准模型——``method`` 如实标注）。
      * ``equityCurve``：等权组合口径——每个交易日对**活跃事件**（已建仓未平仓）的当日
        收益取平均并复利；无活跃事件的交易日净值持平。
      * 样本不足（实测事件数 < ``min_events``，缺省 5）→ ``sample.sufficient=false`` +
        顶层 ``note``，如实说明，不硬凑结论。

    事件源与排除原因见模块 docstring；K 线全量经 ``server.data.cache`` PIT 闸门。
    """
    v3_analytics = _analytics()
    try:
        days = _clamp_int(days, DEFAULT_EVENT_DAYS, 30, 2000)
        horizon = _clamp_int(horizon, DEFAULT_HORIZON, 1, 60)
        min_events = _clamp_int(min_events, DEFAULT_MIN_EVENTS, 1, 500)
        limit = _clamp_int(limit, 500, 60, 2000)
        code = None
        if market is not None and str(market).strip():
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
        if requested:
            names = requested[:EVENT_UNIVERSE_LIMIT]
        elif code:
            universe = v3_universe.resolve_universe(v3_run, home, code)
            if universe is None:
                detail = v3_universe.universe_note(home, code) or ""
                return _error("market/no-universe",
                              "该市场没有配置自选池、也没有真实持仓",
                              market=code, detail=detail)
            universe_source = universe.get("source")
            names = list(universe.get("tickers") or [])[:EVENT_UNIVERSE_LIMIT]
        else:
            names = v3_analytics.read_watchlist(home)[:EVENT_UNIVERSE_LIMIT]
        if not names:
            return _error("event/no-universe",
                          "事件研究需要至少一个标的（请求未给 tickers，自选池也为空）",
                          market=code)

        futu_events, futu_report = collect_futu_events(v3_run, names, days)
        nlp_events, nlp_report = collect_nlp_events(home)
        events = _dedupe_events(futu_events + nlp_events)

        as_of = v3_analytics._series_as_of()
        envelopes, load_errors = _load_pit_bars(v3_run, names, limit, as_of=as_of)
        bars_by_ticker = {ticker: list(envelope.get("bars") or [])
                          for ticker, envelope in envelopes.items()}
        rejected_future = sum(int((envelope.get("rejected") or {}).get("future") or 0)
                              for envelope in envelopes.values())
        undated = sum(int((envelope.get("rejected") or {}).get("undated") or 0)
                      for envelope in envelopes.values())
        windows = {ticker: (envelope.get("window") or {})
                   for ticker, envelope in envelopes.items()}

        rows = []
        excluded = {"noBarAfterAnnounce": 0, "noForwardWindow": 0, "noBars": 0}
        measured = []
        for event in events:
            ticker = event["ticker"]
            bars = bars_by_ticker.get(ticker) or []
            row = {**event, "status": None}
            if not bars:
                excluded["noBars"] += 1
                row["status"] = "noBars"
                rows.append(row)
                continue
            stamps = [str(bar.get("t")) for bar in bars]
            closes = [v3_math.to_float(bar.get("c")) for bar in bars]
            entry = next((index for index, stamp in enumerate(stamps)
                          if stamp > event["announced"]), None)
            if entry is None:
                excluded["noBarAfterAnnounce"] += 1
                row["status"] = "noBarAfterAnnounce"
                rows.append(row)
                continue
            if entry + horizon > len(stamps) - 1:
                excluded["noForwardWindow"] += 1
                row["status"] = "noForwardWindow"
                rows.append(row)
                continue
            entry_close = closes[entry]
            exit_close = closes[entry + horizon]
            if not entry_close or exit_close is None:
                excluded["noForwardWindow"] += 1
                row["status"] = "noForwardWindow"
                rows.append(row)
                continue
            ret_pct = (exit_close / entry_close - 1) * 100
            row.update({
                "status": "measured",
                "entryDate": stamps[entry],
                "exitDate": stamps[entry + horizon],
                "entryClose": v3_math.round_half_up(entry_close, 4),
                "exitClose": v3_math.round_half_up(exit_close, 4),
                "retPct": v3_math.round_half_up(ret_pct, 2),
            })
            measured.append({**row, "_entryIndex": entry})
            rows.append(row)

        car_curve = []
        for offset in range(1, horizon + 1):
            values = []
            for item in measured:
                bars = bars_by_ticker[item["ticker"]]
                entry = item["_entryIndex"]
                exit_close = v3_math.to_float(bars[entry + offset].get("c"))
                entry_close = v3_math.to_float(bars[entry].get("c"))
                if exit_close is None or not entry_close:
                    continue
                values.append((exit_close / entry_close - 1) * 100)
            aar = v3_math.mean(values) if values else None
            car_curve.append({
                "offset": offset,
                "aarPct": None if aar is None else v3_math.round_half_up(aar, 3),
                "n": len(values),
            })
        running = 0.0
        for point in car_curve:
            running += point["aarPct"] or 0.0
            point["carPct"] = v3_math.round_half_up(running, 3)

        equity_curve = _event_daily_returns(measured, bars_by_ticker)
        returns = [item["retPct"] for item in measured]
        wins = sum(1 for value in returns if value > 0)
        by_type = {}
        for item in measured:
            bucket = by_type.setdefault(item["type"], {"type": item["type"], "measured": 0,
                                                       "returns": []})
            bucket["returns"].append(item["retPct"])
        type_rows = []
        for bucket in sorted(by_type.values(), key=lambda item: item["type"]):
            values = bucket.pop("returns")
            bucket.update({
                "events": len(values),
                "meanRetPct": v3_math.round_half_up(v3_math.mean(values), 2),
                "winRatePct": v3_math.round_half_up(
                    (sum(1 for value in values if value > 0) / len(values)) * 100, 1),
            })
            type_rows.append(bucket)

        measured_count = len(measured)
        sufficient = measured_count >= min_events
        caar = car_curve[-1]["carPct"] if car_curve else None
        summary = {
            "sampled": len(events),
            "measured": measured_count,
            "excluded": excluded,
            "meanRetPct": v3_math.round_half_up(v3_math.mean(returns), 2) if returns else None,
            "winRatePct": (v3_math.round_half_up(wins / measured_count * 100, 1)
                           if measured_count else None),
            "caarPct": caar,
            "byType": type_rows,
        }
        sample = {
            "minEvents": min_events,
            "sufficient": sufficient,
            "note": (None if sufficient
                     else f"实测事件数 {measured_count} < 阈值 {min_events}：样本不足，"
                          f"以下统计只是对样本的描述，不构成策略结论"),
        }
        return {
            "ok": True,
            "market": code,
            "universeSource": universe_source,
            "params": {"days": days, "horizon": horizon, "minEvents": min_events,
                       "limit": limit},
            "method": ("公告日 t 收盘后可知 → 次日收盘建仓、持有 H 个交易日平仓；"
                       "CAR 未做市场调整（无基准模型），等权口径"),
            "sources": {"futuEvents": {"ok": True, "byTicker": futu_report},
                        "nlpClassifyEvents": nlp_report},
            "pit": {"asOf": as_of, "mode": pit_cache.AS_OF_INCLUSIVE,
                    "semantics": pit_cache.semantics_text(pit_cache.AS_OF_INCLUSIVE, as_of),
                    "rejectedFuture": rejected_future, "rejectedUndated": undated,
                    "windows": windows},
            "seriesErrors": load_errors or None,
            "events": rows,
            "summary": summary,
            "carCurve": car_curve,
            "equityCurve": equity_curve,
            "sample": sample,
            **({} if sufficient else {"note": sample["note"]}),
        }
    except Exception as error:  # noqa: BLE001 —— 与分析类接口同一口径：信封化，不抛 500
        return _error("v3/internal", error)


def _event_daily_returns(measured, bars_by_ticker, horizon=DEFAULT_HORIZON):
    """逐日等权收益 + 复利净值：``[{t, value, active}]``。

    事件窗由 ``entryDate``/``exitDate``（建仓次日 .. 建仓后第 ``horizon`` 日）给出；
    每个交易日对活跃事件的**当日收益**取平均并复利，无活跃事件的交易日净值持平。
    ``horizon`` 参数保留口径一致性（窗长已由 ``event_study`` 用同一 ``horizon`` 校验）。
    """
    # 逐标的：交易日 → 当日收益
    returns_by_ticker = {}
    stamps_all = set()
    for item in measured:
        ticker = item["ticker"]
        if ticker in returns_by_ticker:
            continue
        bars = bars_by_ticker.get(ticker) or []
        daily = {}
        for index in range(1, len(bars)):
            previous = v3_math.to_float(bars[index - 1].get("c"))
            current = v3_math.to_float(bars[index].get("c"))
            if not previous or current is None:
                continue
            daily[str(bars[index].get("t"))] = current / previous - 1
        returns_by_ticker[ticker] = daily
        stamps_all.update(str(bar.get("t")) for bar in bars)

    equity = 1.0
    curve = []
    for stamp in sorted(stamps_all):
        values = []
        for item in measured:
            entry_date = item.get("entryDate")
            exit_date = item.get("exitDate")
            if not entry_date or not exit_date:
                continue
            if not (entry_date < stamp <= exit_date):
                continue
            value = returns_by_ticker.get(item["ticker"], {}).get(stamp)
            if value is not None:
                values.append(value)
        if values:
            equity *= 1 + v3_math.mean(values)
        curve.append({"t": stamp, "value": v3_math.round_half_up(equity, 6),
                      "active": len(values)})
    return curve


# ---------------------------------------------------------------------------
# GET /api/v3/strategies/stat-arb
# ---------------------------------------------------------------------------
def stat_arb(v3_run, home=None, *, market="SH", tickers_raw=None, limit=500,
             train_ratio=0.7, z_window=60, z_in=2.0, z_out=0.5, cost_bps=5.0):
    """统计套利策略（协整价差回归）——只读研究，不出订单。

    契约::

        {ok, market, universeSource, impl, params, pit, aligned,
         candidates: [{pair, hedgeRatio, adfTStat, adfPValue, adfPass,
                       halfLifeDays, trainCorr, observations}],
         cointegrated: bool, selected: {...} | null,
         backtest: {window, metrics, equityCurve, turnover, trades, roundTrips,
                    winRatePct, exposurePct, costPaidPct} | null,
         sample: {sufficient, note}, note?}

    方法（``impl`` 如实标注 numpy 自实现；venv 无 statsmodels）：

      1. 同市场标的对（显式 ``tickers`` 优先，否则该市场宇宙前 8 只）；
      2. 训练窗（前 ``train_ratio``）对数价格 OLS 对冲比率（无截距）；
      3. 训练窗残差 DF/ADF 检验：t 统计 + MacKinnon(1994) **近似** p 值
         （``v3_math.ADF_IMPL_NOTE``）；半衰期 = AR(1)；
      4. ``adfPValue < 0.05`` 才算协整候选；**一个都没有 → ``cointegrated: false``**
         + 说明（检验不显著就如实说，绝不硬选）；
      5. 入选对在**样本外测试窗**回测：价差 z-score（滚动 ``z_window``，只用 ≤ t-1 的
         价差——PIT：决策不含当日）``|z| ≥ z_in`` 进（反向），``|z| ≤ z_out`` 平；
         双腿成本按 ``|Δpos| × (1+|β|) × cost_bps`` 计。

    选对在训练窗完成（in-sample），权益曲线在测试窗（out-of-sample）；多重比较
    （28 对里挑 1）的偏差在 ``note`` 如实声明。
    """
    v3_analytics = _analytics()
    try:
        code = None
        if market is not None and str(market).strip():
            code = v3_universe.normalize_market(market)
            if code is None:
                return _error("market/bad-market", "market 需为 SH / HK / US",
                              market=str(market))
        limit = _clamp_int(limit, 500, 60, 2000)
        z_window = _clamp_int(z_window, 60, 5, 250)
        train_ratio = _clamp_float(train_ratio, 0.7, 0.3, 0.9)
        z_in = _clamp_float(z_in, 2.0, 0.5, 6.0)
        z_out = _clamp_float(z_out, 0.5, 0.0, 3.0)
        cost_bps = _clamp_float(cost_bps, 5.0, 0.0, 200.0)

        if isinstance(tickers_raw, (list, tuple)):
            requested = [str(item).strip() for item in tickers_raw]
        else:
            requested = [item.strip() for item in str(tickers_raw or "").split(",")]
        requested = [item for item in requested if item]
        universe_source = None
        if len(requested) >= 2:
            names = requested[:PAIR_UNIVERSE_LIMIT]
        elif code:
            universe = v3_universe.resolve_universe(v3_run, home, code)
            if universe is None:
                detail = v3_universe.universe_note(home, code) or ""
                return _error("market/no-universe",
                              "该市场没有配置自选池、也没有真实持仓",
                              market=code, detail=detail)
            universe_source = universe.get("source")
            names = list(universe.get("tickers") or [])[:PAIR_UNIVERSE_LIMIT]
        else:
            names = v3_analytics.read_watchlist(home)[:PAIR_UNIVERSE_LIMIT]
        if len(names) < 2:
            return _error("statarb/too-few",
                          "统计套利需要至少 2 个标的（请求未给 tickers，自选池也不足）",
                          market=code)

        as_of = v3_analytics._series_as_of()
        envelopes, load_errors = _load_pit_bars(v3_run, names, limit, as_of=as_of)
        bars_by_ticker = {ticker: list(envelope.get("bars") or [])
                          for ticker, envelope in envelopes.items()}
        rejected_future = sum(int((envelope.get("rejected") or {}).get("future") or 0)
                              for envelope in envelopes.values())
        usable = {ticker: bars for ticker, bars in bars_by_ticker.items() if bars}
        if len(usable) < 2:
            return _error("statarb/insufficient",
                          "可用日 K 不足 2 只，无法配对" +
                          ("；取数失败：" + "; ".join(
                              f"{item['ticker']}: {item['error']}" for item in load_errors)
                           if load_errors else ""),
                          market=code)

        dates, closes = v3_math.align_series(usable)
        total = len(dates)
        if total < MIN_ALIGNED_DAYS:
            return _error("statarb/insufficient",
                          f"对齐后交易日不足（{total} < {MIN_ALIGNED_DAYS}），"
                          f"无法划分训练/测试窗", market=code)
        train_n = int(total * train_ratio)
        train_n = max(MIN_TRAIN_DAYS, min(train_n, total - MIN_TEST_DAYS))
        test_n = total - train_n
        if train_n < MIN_TRAIN_DAYS or test_n < MIN_TEST_DAYS:
            return _error("statarb/insufficient",
                          f"训练/测试窗不足（train={train_n} < {MIN_TRAIN_DAYS} 或 "
                          f"test={test_n} < {MIN_TEST_DAYS}）", market=code)

        # 同市场配对：显式标的可能跨市场，只有同市场前缀的对才进候选（规格口径）。
        market_of = {ticker: v3_universe.market_of_ticker(ticker) for ticker in closes}
        pairs = [pair for pair in itertools.combinations(sorted(closes), 2)
                 if market_of.get(pair[0]) and market_of.get(pair[0]) == market_of.get(pair[1])]
        if not pairs:
            return _error("statarb/no-pairs",
                          "没有同市场的标的对（跨市场不配对）", market=code)

        log_closes = {ticker: [math.log(value) for value in closes[ticker]]
                      for ticker in closes}
        candidates = []
        for left, right in pairs:
            x_train = log_closes[left][:train_n]
            y_train = log_closes[right][:train_n]
            beta = v3_math.ols_slope(x_train, y_train)
            if beta is None:
                continue
            residuals = [y_train[i] - beta * x_train[i] for i in range(train_n)]
            adf = v3_math.adf_test(residuals)
            correlation = v3_math.pearson(x_train, y_train)
            p_value = adf.get("pValue")
            candidates.append({
                "pair": [left, right],
                "hedgeRatio": v3_math.round_half_up(beta, 4),
                "adfTStat": adf.get("tStat"),
                "adfPValue": p_value,
                "adfPass": bool(p_value is not None and p_value < ADF_PASS_P),
                "halfLifeDays": v3_math.half_life(residuals),
                "trainCorr": (None if correlation is None
                              else v3_math.round_half_up(correlation, 3)),
                "observations": train_n,
            })
        candidates.sort(key=lambda item: (item["adfPValue"] is None,
                                          item["adfPValue"] if item["adfPValue"] is not None
                                          else 1.0))
        passing = [item for item in candidates if item["adfPass"]]

        pit_block = {
            "asOf": as_of, "mode": pit_cache.AS_OF_INCLUSIVE,
            "semantics": pit_cache.semantics_text(pit_cache.AS_OF_INCLUSIVE, as_of),
            "rejectedFuture": rejected_future,
        }
        base = {
            "ok": True,
            "market": code,
            "universeSource": universe_source,
            "impl": ("numpy 自实现（venv 无 statsmodels/scipy）；p 值为 MacKinnon(1994) "
                     "响应面近似口径，非精确分布"),
            "params": {"trainRatio": train_ratio, "zWindow": z_window, "zIn": z_in,
                       "zOut": z_out, "costBps": cost_bps, "adfPassP": ADF_PASS_P,
                       "trainDays": train_n, "testDays": test_n},
            "pit": pit_block,
            "seriesErrors": load_errors or None,
            "aligned": {"days": total, "from": dates[0], "to": dates[-1],
                        "pairsConsidered": len(pairs)},
            "candidates": candidates,
            "cointegrated": bool(passing),
        }
        if not passing:
            base["selected"] = None
            base["backtest"] = None
            base["sample"] = {"sufficient": True}
            base["note"] = (f"无协整对：{len(candidates)} 个候选对的近似 p 值全部 ≥ "
                            f"{ADF_PASS_P}——检验不显著就如实说明，不硬凑价差策略")
            return base

        selected = passing[0]
        backtest = _spread_backtest(dates, closes, log_closes, selected, train_n,
                                    z_window=z_window, z_in=z_in, z_out=z_out,
                                    cost_bps=cost_bps)
        base["selected"] = {key: selected[key] for key in
                            ("pair", "hedgeRatio", "adfTStat", "adfPValue", "halfLifeDays",
                             "trainCorr", "observations")}
        base["backtest"] = backtest
        base["sample"] = {"sufficient": True}
        base["note"] = ("对选取用训练窗 ADF（in-sample），权益曲线在样本外测试窗（out-of-sample）；"
                        "从多个候选对里挑 1 个存在多重比较偏差，样本外表现不代表未来")
        return base
    except Exception as error:  # noqa: BLE001
        return _error("v3/internal", error)


def _spread_backtest(dates, closes, log_closes, candidate, train_n, *, z_window, z_in,
                     z_out, cost_bps):
    """价差 z-score 双腿回测（**样本外**：只在 ``train_n`` 之后的测试窗交易）。

    PIT：``t`` 日的 z 用 ``spread[t-z_window : t]``（即 ≤ t-1）——决策不含当日；
    对冲比率是训练窗拟合值，测试窗不重估（不偷看测试数据）。
    """
    left, right = candidate["pair"]
    beta = candidate["hedgeRatio"]
    spread = [log_closes[right][i] - beta * log_closes[left][i] for i in range(len(dates))]

    def simple_returns(ticker):
        series = closes[ticker]
        out = {index: series[index] / series[index - 1] - 1
               for index in range(1, len(series))
               if series[index - 1]}
        return out

    returns_left = simple_returns(left)
    returns_right = simple_returns(right)

    position = 0.0
    entry_index = None
    entries = 0
    round_trips = 0
    round_trip_pnl = []
    holding_pnl = 0.0
    turnover = 0.0
    cost_paid = 0.0
    equity = 1.0
    curve = []
    held_days = 0
    cost_rate = cost_bps / 1e4
    leg_notional = 1.0 + abs(beta)
    daily_returns = []

    for index in range(train_n, len(dates)):
        z = None
        start = index - z_window
        if start >= 0:
            window = spread[start:index]  # ≤ t-1（不含当日）
            mean_spread = sum(window) / len(window)
            variance = (sum((value - mean_spread) ** 2 for value in window)
                        / (len(window) - 1))
            if variance > 0:
                z = (spread[index - 1] - mean_spread) / math.sqrt(variance)
        new_position = position
        if position == 0:
            if z is not None and abs(z) >= z_in:
                new_position = -1.0 if z > 0 else 1.0
                entry_index = index
                entries += 1
                holding_pnl = 0.0
        else:
            if z is not None and abs(z) <= z_out:
                new_position = 0.0
        delta = abs(new_position - position)
        cost = delta * leg_notional * cost_rate if delta > 0 else 0.0
        if delta > 0:
            cost_paid += cost
        turnover += delta
        position = new_position
        # 当日损益 = 仓位 × 价差日收益（右腿 − β×左腿）− 调仓成本（换仓当天计）。
        day_return = position * (returns_right.get(index, 0.0)
                                 - beta * returns_left.get(index, 0.0)) - cost
        daily_returns.append(day_return)
        equity *= 1 + day_return
        if position != 0:
            held_days += 1
            holding_pnl += day_return
        if entry_index is not None and position == 0 and delta > 0:
            round_trips += 1
            round_trip_pnl.append(holding_pnl)
            entry_index = None
        curve.append({"t": dates[index], "value": v3_math.round_half_up(equity, 6),
                      "z": None if z is None else v3_math.round_half_up(z, 2),
                      "position": position})

    count = len(daily_returns)
    average = v3_math.mean(daily_returns)
    deviation = (math.sqrt(sum((value - average) ** 2 for value in daily_returns)
                           / (count - 1)) if count > 1 else 0.0)
    sharpe = (average / deviation) * math.sqrt(252) if deviation > 0 else None
    metrics = {
        "days": count,
        "totalReturnPct": v3_math.round_half_up((equity - 1) * 100, 2),
        "annReturnPct": v3_math.round_half_up(average * 252 * 100, 2),
        "annVolPct": v3_math.round_half_up(deviation * math.sqrt(252) * 100, 2),
        "sharpe": None if sharpe is None else v3_math.round_half_up(sharpe, 3),
        "maxDrawdownPct": v3_math.round_half_up(v3_math.max_drawdown(
            [point["value"] for point in curve]) * 100, 2),
    }
    wins = sum(1 for value in round_trip_pnl if value > 0)
    return {
        "window": {"phase": "out-of-sample", "from": dates[train_n], "to": dates[-1],
                   "days": count},
        "metrics": metrics,
        "equityCurve": curve,
        "entries": entries,
        "roundTrips": round_trips,
        "winRatePct": (v3_math.round_half_up(wins / len(round_trip_pnl) * 100, 1)
                       if round_trip_pnl else None),
        "exposurePct": (v3_math.round_half_up(held_days / count * 100, 1) if count else None),
        "turnover": v3_math.round_half_up(turnover, 2),
        "costPaidPct": v3_math.round_half_up(cost_paid * 100, 3),
    }


# ---------------------------------------------------------------------------
# 路由注册（由 v3_analytics.register 代挂——app.py 的接线清单本任务不改）
# ---------------------------------------------------------------------------
def register(app, v3_run, home):
    """把两个策略族端点挂到 FastAPI ``app`` 上（只读，全部 200 + 信封）。"""

    @app.get("/api/v3/strategies/event-study")
    async def v3_strategies_event_study(tickers: Optional[str] = None, market: str = "SH",
                                        days: int = DEFAULT_EVENT_DAYS,
                                        horizon: int = DEFAULT_HORIZON,
                                        min_events: int = DEFAULT_MIN_EVENTS,
                                        limit: int = 500):
        """事件驱动策略（公告后漂移/事件窗口）：真实公告事件 + 每事件收益 + CAR + 等权曲线。

        事件必须带可核验公告时点（缺时点不入样）；样本 < ``min_events`` 时如实返回
        「样本不足」。``market`` 缺省 SH；``tickers`` 显式给出时优先（cap 8 只）。
        """
        def work():
            return event_study(v3_run, home, tickers_raw=tickers, market=market, days=days,
                               horizon=horizon, min_events=min_events, limit=limit)

        return _ok(await asyncio.to_thread(work))

    @app.get("/api/v3/strategies/stat-arb")
    async def v3_strategies_stat_arb(market: str = "SH", tickers: Optional[str] = None,
                                     limit: int = 500, train_ratio: float = 0.7,
                                     z_window: int = 60, z_in: float = 2.0,
                                     z_out: float = 0.5, cost_bps: float = 5.0):
        """统计套利策略（协整价差回归）：OLS 对冲比率 + ADF（近似 p）+ 价差 z 回测。

        无协整对时如实返回 ``cointegrated=false``（检验不显著不硬选）；权益曲线在
        样本外测试窗。``impl`` 如实标注 numpy 自实现与近似口径。
        """
        def work():
            return stat_arb(v3_run, home, market=market, tickers_raw=tickers, limit=limit,
                            train_ratio=train_ratio, z_window=z_window, z_in=z_in,
                            z_out=z_out, cost_bps=cost_bps)

        return _ok(await asyncio.to_thread(work))
