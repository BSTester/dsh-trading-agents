"""V3 行情类接口（行情与信号页消费）。

为什么单独一个模块：行情四件套（K 线 / 自选快照 / 板块 / 盘口）与既有工具面的字段名
差异较大（``series``/``watchlist_list``/``plate_list``/``rt_order_book``），需要一个
薄的适配层把「工作台字段」翻成「V3 契约字段」；计算与取数仍然全部经 ``v3_run`` 走既有
56 工具面（同一 handle），不新增第二条数据路径。

数据诚实：富途账号未开通实时行情权限时 ``rt_order_book`` 返回 ``errcode=-9``，本层
**原样透传错误**（``ok=false`` + 真实 message），让页面显示「无数据源 + 原因」而不是
编造盘口。

市场口径（2026-09-20 新增）：``/market/watchlist`` 支持 ``?market=SH|HK|US``（缺省 ``SH``），
池子来自 ``server/v3_universe.resolve_universe``（配置 ``watchlists.<market>`` → 富途真实
持仓 ``account_positions``，**不发明自选池**）；``rows`` 只含该市场标的，响应带 ``market``
与 ``universe_source``；该市场既无配置池也无真实持仓 → ``market/no-universe``（含 ``detail``
写明真实原因），不给空数组冒充成功。
"""
import asyncio

from server import v3_universe

# 周期 → 工作台 series 的 period 取值（工作台只认这几个，其余原样传入由工作台校验）
PERIODS = {"1d": "1d", "5m": "5m", "60m": "60m", "15m": "15m", "30m": "30m", "1w": "1w", "1M": "1M"}


def _value(envelope):
    return envelope.get("value") if isinstance(envelope, dict) and envelope.get("ok") else None


def _error(envelope, fallback_code="v3/upstream"):
    if isinstance(envelope, dict) and isinstance(envelope.get("error"), dict):
        return envelope["error"]
    return {"code": fallback_code, "message": "上游工具调用失败"}


def _first_list(value):
    """从工具返回里尽力取到第一层列表（不同工具用了不同键名）。"""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("bars", "rows", "items", "list", "groups", "positions", "data"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


def register(app, v3_run, home):
    """注册 /api/v3/market、/market/watchlist、/plates、/orderbook。"""

    def call(tool, payload=None):
        return v3_run(tool, payload or {})

    # ── K 线 ────────────────────────────────────────────────────────────────
    @app.get("/api/v3/market")
    async def v3_market(ticker: str = "SH.600519", period: str = "1d", limit: int = 120):
        def build():
            envelope = call("series", {"ticker": ticker, "period": PERIODS.get(period, period),
                                       "limit": max(20, min(int(limit), 2000))})
            if not envelope.get("ok"):
                return {"ok": False, "error": _error(envelope, "market/series-unavailable")}
            value = _value(envelope) or {}
            bars = value.get("bars") or _first_list(value)
            return {
                "ok": True,
                "data": {
                    "ticker": value.get("ticker", ticker),
                    "period": period,
                    "source": value.get("source", "futu/quote_history_kline"),
                    "as_of": value.get("as_of") or (bars[-1].get("t") if bars else None),
                    "count": len(bars),
                    "bars": bars,
                },
            }

        return await asyncio.to_thread(build)

    # ── 自选池快照（逐票最近日 K + 因子）────────────────────────────────────
    @app.get("/api/v3/market/watchlist")
    async def v3_watchlist(n: int = 6, market: str = "SH"):
        """该市场的池子快照：池子取统一解析（配置 → 真实持仓），``rows`` 只含本市场标的。"""

        def build():
            code = v3_universe.normalize_market(market)
            if code is None:
                return {"ok": False, "error": {"code": "market/bad-market",
                                               "message": "market 需为 SH / HK / US"}}
            universe = v3_universe.resolve_universe(call, home, code)
            if universe is None:
                error = {"code": "market/no-universe",
                         "message": "该市场没有配置自选池、也没有真实持仓"}
                detail = v3_universe.universe_note(home, code)
                if detail:
                    error["detail"] = detail
                return {"ok": False, "error": error}
            try:
                limit = max(1, min(int(n), 20))
            except (TypeError, ValueError):
                limit = 6
            tickers = list(universe.get("tickers") or [])[:limit]
            rows, errors, source = [], [], None
            for ticker in tickers:
                envelope = call("series", {"ticker": ticker, "period": "1d", "limit": 30})
                if not envelope.get("ok"):
                    errors.append({"ticker": ticker, "error": _error(envelope)})
                    continue
                value = _value(envelope) or {}
                bars = value.get("bars") or []
                source = source or value.get("source")
                closes = [float(b.get("c")) for b in bars if b.get("c") is not None]
                if len(closes) < 2:
                    errors.append({"ticker": ticker, "error": {"code": "market/insufficient",
                                                               "message": "日 K 不足 2 根"}})
                    continue
                mom = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else None
                rows.append({
                    "ticker": ticker,
                    "close": closes[-1],
                    "changePct": (closes[-1] / closes[-2] - 1) * 100,
                    "mom20Pct": mom,
                    "asOf": bars[-1].get("t"),
                })
            return {"ok": True, "market": code,
                    "universe_source": universe.get("source"),
                    "universe_note": universe.get("note"),
                    "rows": rows, "errors": errors,
                    "sources": {"kline": source or "futu/quote_history_kline",
                                "factors": "workbench/factors"}}

        return await asyncio.to_thread(build)

    # ── 板块列表 ────────────────────────────────────────────────────────────
    @app.get("/api/v3/plates")
    async def v3_plates(market: str = "SH", plate_class: str = "ALL"):

        def build():
            envelope = call("plate_list", {"market": market, "plate_class": plate_class})
            if not envelope.get("ok"):
                return {"ok": False, "error": _error(envelope, "market/plates-unavailable")}
            value = _value(envelope) or {}
            return {"ok": True, "data": value,
                    "note": "板块涨跌幅需富途实时行情权限；无权限时只有板块清单，不填占位"}

        return await asyncio.to_thread(build)

    # ── 盘口五档（权限缺口时原样透传错误）──────────────────────────────────
    @app.get("/api/v3/orderbook")
    async def v3_orderbook(ticker: str = "SH.600519"):

        def build():
            envelope = call("rt_order_book", {"code": ticker})
            if not envelope.get("ok"):
                # 典型：富途 errcode=-9 realtime quote permission required → 页面标注无数据源
                return {"ok": False, "error": _error(envelope, "market/orderbook-unavailable")}
            return {"ok": True, "data": _value(envelope), "ticker": ticker}

        return await asyncio.to_thread(build)
