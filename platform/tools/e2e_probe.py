#!/usr/bin/env python3
"""V3 全量端到端探针（**只读**）。

覆盖：全部 /api/v3/* 路由 + 全部 /api/wb/<endpoint> 端点（82 项）。
   * 写类/下单类端点**一律跳过**（switch-mode / plan-execute / confirm-decide / trade_* /
     sim_trade_* / modify_user_security / oms-sync / credentials 写动作），只报「已跳过（写动作）」；
   * 其余端点先用空载荷探测，若返回「缺少必填字段 X」则按默认值表补齐重试（最多 3 轮）；
   * 记录：HTTP 状态、耗时、ok、错误码与消息、返回体量、以及是否拿到了真实数据。

用法：python3 platform/tools/e2e_probe.py [--base http://127.0.0.1:8397] [--json out.json] [--md out.md]
      python3 platform/tools/e2e_probe.py --markets SH,HK,US     # 三市场只读模式（见 MARKET_STEPS）
"""
import argparse
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date as _date
from datetime import timedelta as _timedelta

#: 绝不主动触发的写/交易端点（本探针只读；这些能力已由页面在人工确认下验证）
WRITE_ENDPOINTS = {
    "switch-mode", "plan-execute", "confirm-decide", "modify_user_security",
    "trade_place", "trade_modify", "trade_cancel", "sim_trade_place", "sim_trade_modify",
    "sim_trade_cancel", "plan_execute", "auto_pipeline", "openapi_config", "push_subscribe",
    "push_unsubscribe", "sentiment-snapshot", "rules-decide", "research-tasks-claim",
    "research-tasks-report", "record_observation",
}
#: 缺少必填字段时的默认值（只用于读类探测）
DEFAULTS = {
    "ticker": "SH.600000", "code": "SH.600000", "symbol": "600519", "ts_code": "600519.SH",
    "codes": ["SH.600000", "HK.00700", "US.NVDA"], "tickers": ["SH.600000", "SH.600009", "SH.600010"],
    "market": "SH", "plate_class": "ALL", "plate_code": "SH.LIST0970", "exchange": "SH",
    "group_name": "watchlist", "group_type": "all", "keyword": "新能源", "factor": "mom_20",
    "window": 120, "limit": 20, "num": 20, "days": 180, "forward": 5, "mode": "sim",
    "ktype": "K_DAY", "autype": "QFQ", "request_section": "basic", "section": "basic",
    "period": "1d", "order_type": "NORMAL", "price": 10.0, "qty": 100, "page_size": 10,
    "start": "2026-01-01", "end": "2026-09-19", "start_date": "20260101", "end_date": "20260919",
    "page_flag": 0, "sort_field": "price", "ascend": True, "strategy": "watchlist_rsi",
    "watchlist": "watchlist", "metric": "price", "fast_grid": 5, "slow_grid": 20,
    "retrieve_queries": [], "screen_queries": [], "sorts": [], "next_key": "",
    "market_type": 1, "is_delay": False, "only_count": False, "stock_owner": "SH.600000",
    "request_type": 1, "count": 10, "date": "2026-09-19", "timezone": 8, "search_type": 1,
    "news_type": 1, "sort_type": 1, "lang": 0, "divi_mode": 1, "price_type": 1,
    "leverage_direction": 0, "leverage_multiple": 1, "field_filter": [], "filter": {},
    "is_contain_ba": False, "is_contain_overnight": False, "extended_time": 0,
    "user_stock_list_mode": 0, "holding_stock_ids": [], "watchlist_stock_ids": [],
    "id": "", "decision": "approved", "plan_hash": "", "expected_mode": "sim",
    "action": "status", "key": "futu_appkey", "value": "", "no_probe": True,
    "confirmation": "", "older_than_minutes": 120, "run_id": "",
}
V3_GET = [
    "overview", "metrics", "brain", "market", "market/watchlist", "plates", "orderbook",
    "factors/matrix", "strategy", "risk", "risk/analytics", "execution", "oms/orders",
    "gateway", "tools", "settings", "events", "audit", "credentials", "research",
    "research/tasks", "news", "spot", "financials", "openbb", "ml/sweep",
]
#: 已知重端点（给更长超时）
HEAVY = {"factors/matrix", "risk/analytics", "ml/sweep", "openbb", "spot"}


def call(url, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"content-type": "application/json"} if data else {}
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        body, status = error.read(), error.code
    except Exception as error:  # noqa: BLE001
        return {"http": None, "ms": int((time.time() - started) * 1000), "ok": None,
                "error": {"code": "net", "message": str(error)[:200]}, "size": 0, "raw": ""}
    elapsed = int((time.time() - started) * 1000)
    text = body.decode("utf-8", "ignore")
    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:  # noqa: BLE001
        parsed = None
    ok = parsed.get("ok") if isinstance(parsed, dict) else (status == 200 and not text.startswith("<!"))
    error = parsed.get("error") if isinstance(parsed, dict) else None
    return {"http": status, "ms": elapsed, "ok": ok, "error": error, "size": len(body), "raw": text}


#: v3 读路由的必填查询参数（缺了会报 bad-args —— 那是探针问题，不是数据源缺口）
V3_PARAMS = {
    "financials": {"ticker": "AAPL", "statement": "income", "periods": 2},
    "news": {"symbol": "600519", "limit": 3},
    "openbb": {"symbol": "AAPL"},
    "events": {"ticker": "SH.600000", "window": 180},
    "audit": {"window": 120},
    "ml/sweep": {"ticker": "SH.600519", "windows": "10,20", "rebalance": "5,10"},
    "factors/matrix": {"tickers": "SH.600000,SH.600009,SH.600010"},
    "market": {"ticker": "SH.600000", "period": "1d", "limit": 60},
    "market/watchlist": {"n": 3},
    "orderbook": {"ticker": "HK.00700"},
    "plates": {"market": "SH", "plate_class": "ALL"},
}


def probe_v3(base, path, method="GET"):
    timeout = 180 if any(path.startswith(h) for h in HEAVY) else 45
    url = f"{base}/api/v3/{path}"
    if method == "GET":
        params = V3_PARAMS.get(path)
        if params:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(params)}"
        return call(url, timeout=timeout)
    if path == "ml/backtest":
        return call(url, {"ticker": "SH.600519", "window": 20, "rebalanceDays": 5, "limit": 500},
                    timeout=180)
    if path == "strategy/run":
        return {"skipped": "研究写入（会落一条研究轮记录），已在页面验证"}
    if path == "oms/sync":
        return {"skipped": "写动作（台账对账），按只读纪律跳过"}
    if path == "credentials":
        return call(url, {"action": "status", "key": "futu_appkey"}, timeout=30)
    return {"skipped": "未纳入探针"}


#: wb 端点的正确探测参数（枚举/列表类必须给对，否则报的是「参数非法」而不是数据缺口）
WB_PARAMS = {
    "factors": {"tickers": ["SH.600000", "SH.600009", "SH.600010"]},
    "ic": {"tickers": ["SH.600000", "SH.600009", "SH.600010"], "factor": "mom_20", "forward": 5},
    "correlation": {"tickers": ["SH.600000", "SH.600009"]},
    "f10_detail": {"code": "SH.600000", "section": "analyst_consensus"},
    "derivative_detail": {"code": "HK.00700", "section": "option_volatility"},
    "option_expiration": {"code": "HK.00700"},
    "option_chain": {"code": "HK.00700", "field_filter": []},
    "option_screen": {"filter": {"field_filter": []}},
    "short_daily_volume": {"code": "HK.00700"},
    "short_interest": {"code": "HK.00700"},
    "ipo_list": {"market": "hk", "request_type": 1},
    "rt_quote": {"codes": ["SH.600000"]},
    "info_basicinfo": {"codes": ["SH.600000"]},
    "market_snapshot": {"codes": ["SH.600000"]},
    "info_market_state": {"codes": ["SH.600000"]},
    "info_search": {"keyword": "新能源", "size": 3},
    "stock_screen": {"screen_queries": [{"field": "price", "min": 1}], "limit": 3},
    "openapi_oauth": {"action": "status"},
    "events": {"ticker": "SH.600000", "days": 180},
    "series": {"ticker": "SH.600000", "period": "1d", "limit": 30},
    "instrument": {"ticker": "SH.600000"},
    "quality": {"ticker": "SH.600000"},
    "sensitivity": {"ticker": "SH.600000"},
    "warrant_screen": {"market_type": 1, "is_delay": False, "only_count": True, "sorts": [], "limit": 3},
    "orders_detail": {"exchange": "SH", "order_ids": []},
}
#: 需要真实业务标识（订单号等）才能探测的端点：如实标注而非报缺口
NEEDS_BUSINESS_ID = {"orders_detail", "rules-decide", "confirm-decide"}


def probe_wb(base, endpoint):
    if endpoint in WRITE_ENDPOINTS:
        return {"skipped": "写/交易端点（人工确认入口，探针不触发）"}
    url = f"{base}/api/wb/{endpoint}"
    payload = dict(WB_PARAMS.get(endpoint, {}))
    if endpoint in NEEDS_BUSINESS_ID and not payload.get("order_ids"):
        return {"skipped": "需要真实业务标识（订单号/待确认 id），探针不构造" }
    last = None
    for _ in range(4):
        result = call(url, payload, timeout=60)
        last = result
        if result.get("ok") is True:
            return result
        message = str((result.get("error") or {}).get("message") or "")
        missing = re.findall(r"缺少必填字段\s*([A-Za-z_][A-Za-z0-9_]*)", message)
        unexpected = re.findall(r"Unexpected\s+([A-Za-z_][A-Za-z0-9_]*)\s+field", message)
        for name in unexpected:
            payload.pop(name, None)
        if not missing:
            return result
        filled = False
        for name in missing:
            if name in DEFAULTS:
                payload[name] = DEFAULTS[name]
                filled = True
            else:
                payload[name] = DEFAULTS.get("ticker")
                filled = True
        if not filled:
            return result
    return last


# ── 三市场模式（--markets SH,HK,US）─────────────────────────────────────────────
# 对每个市场跑同一组**只读**步骤，输出按市场分组的成功/失败表：
#   ① series（日 K，富途历史行情）          ② market_snapshot（实时快照，A 股权限 -9 是已知缺口）
#   ③ quote_history_kline_v2（历史 K 线 v2）④ /api/v3/financials（A股/港股走 f10、美股走 SEC）
#   ⑤ **按市场过滤**：/market/watchlist、/factors/matrix、/risk/analytics、/execution、
#      /research、/risk/industry（每个都带 market=，并核对返回条数与市场一致）
#   ⑥ /api/v3/markets/calendar            ⑦ /api/v3/execution/quality（成交质量，券商委托/成交）
# 全部 GET/POST 只读端点；写/交易端点在本模式下**一次都不碰**。
MARKET_PLAN = {
    "SH": {"quote": "SH.600000", "financials": "SH.600000", "financials_source": "futu/f10_detail/statements"},
    "HK": {"quote": "HK.00700", "financials": "HK.00700", "financials_source": "futu/f10_detail/statements"},
    "US": {"quote": "US.NVDA", "financials": "AAPL", "financials_source": "sec/companyconcept(us-gaap XBRL)"},
}
#: 三市场模式里的每一步 → (kind, name)：kind 为 wb 时走 /api/wb/<name>，v3 时走 /api/v3/<name>
MARKET_STEPS = (
    ("wb", "series", "日 K（富途历史行情）"),
    ("wb", "market_snapshot", "实时快照（A 股已知无实时权限 -9）"),
    ("wb", "quote_history_kline_v2", "历史 K 线 v2（K_DAY 3 根）"),
    ("v3", "financials", "三表（A股/港股 f10、美股 SEC）"),
    ("v3", "market/watchlist", "自选池（?market=，rows 只含该市场标的）"),
    ("v3", "factors/matrix", "因子矩阵（?market=，标的取该市场宇宙）"),
    ("v3", "risk/analytics", "组合风险（?market=，组合＝该市场宇宙等权/该市场计划）"),
    ("v3", "execution", "执行面（?market=，持仓/在途/成交与台账按市场过滤）"),
    ("v3", "research", "研报与研究 run（?market=，按 ticker 前缀过滤）"),
    ("v3", "risk/industry", "行业暴露（?market=，标的复用统一解析）"),
    ("v3", "markets/calendar", "三市场交易时段/节假日"),
    ("v3", "execution/quality", "成交质量（委托/成交回报）"),
)

#: 需要「按市场核对」的步骤（market 过滤 + 条数与市场一致）
MARKET_SCOPED_STEPS = ("market/watchlist", "factors/matrix", "risk/analytics", "execution",
                       "research", "risk/industry")
#: 探针侧的**市场前缀**口径（与 platform/server/v3_universe.py 的公开口径一致：
#: 数字 market_id 表与 trading_datasource.market_ids 同源；SH/SZ/BJ 同属 A 股＝SH）
PROBE_SIM_MARKET_IDS = {"SH": 3, "SZ": 3, "BJ": 3, "HK": 1, "US": 100}
PROBE_A_PREFIXES = ("SH", "SZ", "BJ")


def _probe_market_of_label(label):
    """账户/分组的市场标识 → SH/HK/US；认不出 → None（不猜）。"""
    if label is None or isinstance(label, bool):
        return None
    if isinstance(label, (int, float)) and float(label).is_integer():
        label = int(label)
    text = str(label).strip().upper()
    if text.isdigit():
        for name, market_id in PROBE_SIM_MARKET_IDS.items():
            if market_id == int(text):
                return "SH" if name in PROBE_A_PREFIXES else name
        return None
    if text in PROBE_SIM_MARKET_IDS:
        return "SH" if text in PROBE_A_PREFIXES else text
    return None


def _probe_market_of_ticker(ticker):
    """标的 → 市场前缀口径；裸代码 → None（无法判定）。"""
    text = str(ticker or "").strip().upper()
    if "." not in text:
        return None
    head, _, tail = text.partition(".")
    if head in ("SH", "SZ", "BJ", "HK", "US"):
        return "SH" if head in PROBE_A_PREFIXES else head
    if tail in ("SH", "SZ", "BJ", "HK", "US"):
        return "SH" if tail in PROBE_A_PREFIXES else tail
    return None


def _empty_reason(value, result):
    """空结果必须给出**真实原因**：从响应里找 errors/filter.note/note/missing。"""
    texts = []
    if isinstance(value, dict):
        for key in ("note", "marketNote", "universe_note"):
            if isinstance(value.get(key), str) and value[key].strip():
                texts.append(value[key].strip())
        errors = value.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0] if isinstance(errors[0], dict) else {"raw": errors[0]}
            texts.append(f"errors[0]={first.get('ticker') or first.get('tool') or ''}"
                         f"{first.get('error') or first.get('raw')}")
        missing = value.get("missing")
        if isinstance(missing, list) and missing:
            texts.append(f"missing={str(missing[0])[:120]}")
        filters = value.get("filter")
        if isinstance(filters, dict):
            if isinstance(filters.get("note"), str) and filters["note"].strip():
                texts.append(filters["note"].strip())
            for item in filters.values():
                if isinstance(item, dict) and isinstance(item.get("note"), str) \
                        and item["note"].strip():
                    texts.append(item["note"].strip())
    error = (result or {}).get("error")
    if isinstance(error, dict) and error.get("message"):
        texts.append(f"{error.get('code')}：{error.get('message')}")
    return texts[0] if texts else None


def _market_scope(market, step, value, result):
    """``(ok | None, note)``：核对「返回条数与市场一致」；为空必须带真实原因。"""
    if step not in MARKET_SCOPED_STEPS:
        return None, None
    if result.get("ok") is not True:
        return None, "如实报错，未核对条数"
    if not isinstance(value, dict):
        return False, "响应不是对象，无法核对市场归属"
    if step == "execution":
        groups = (value.get("positions") or {}).get("groups") or []
        labels = [group.get("market") for group in groups if isinstance(group, dict)]
        foreign = [label for label in labels if _probe_market_of_label(label) not in (None, market)]
        note = (f"positions 分组={len(groups)} 市场标签={labels} "
                f"filter={value.get('filter', {}).get('positions', {})}")
        if foreign:
            return False, f"{note}｜越界分组={foreign}"
        if not groups:
            reason = _empty_reason(value, result)
            return (bool(reason), f"{note}｜空：{reason or '没有任何原因说明（空壳）'}")
        return True, note
    if step == "risk/industry":
        tickers = list(value.get("universe") or [])
    elif step == "market/watchlist":
        tickers = [row.get("ticker") for row in (value.get("rows") or []) if isinstance(row, dict)]
    elif step == "factors/matrix":
        tickers = list((value.get("matrix") or {}).get("tickers") or [])
    elif step == "risk/analytics":
        tickers = list(((value.get("analytics") or {}).get("tickers") or {}).keys())
    elif step == "research":
        rows = list(value.get("runs") or []) + list(value.get("reports") or [])
        tickers = [row.get("ticker") for row in rows if isinstance(row, dict)]
    else:
        return None, None
    foreign = [ticker for ticker in tickers if _probe_market_of_ticker(ticker) not in (None, market)]
    unknown = [ticker for ticker in tickers if _probe_market_of_ticker(ticker) is None]
    source = value.get("universe_source") or value.get("source")
    note = (f"条数={len(tickers)} market={market} universe_source={source} "
            f"越界={foreign[:3] or '无'} 无法判定={unknown[:3] or '无'}")
    if foreign:
        return False, f"{note}｜过滤后仍含其它市场标的"
    if not tickers:
        reason = _empty_reason(value, result)
        return (bool(reason), f"{note}｜该市场无数据：{reason or '没有任何原因说明（空壳）'}")
    return True, note


def market_wb_payload(market, step, plan):
    """每个市场、每个 /api/wb 步骤的真实载荷（参数与工具面契约逐项对齐）。"""
    quote = plan["quote"]
    today = _date.today()
    if step == "series":
        return {"ticker": quote, "period": "1d", "limit": 20}, 45
    if step == "market_snapshot":
        return {"codes": [quote]}, 45
    if step == "quote_history_kline_v2":
        # ktype 是**整数**枚举（7=K_DAY）、end 必填；窗口取最近 10 天，num 3 根（轻量探测）
        return {"code": quote, "ktype": 7,
                "start": (today - _timedelta(days=10)).isoformat(),
                "end": today.isoformat(), "num": 3}, 60
    return None, 30


def market_v3_query(market, step, plan):
    """每个市场、每个 /api/v3 步骤的查询串（返回 (path_with_query, timeout)）。"""
    from urllib.parse import urlencode

    financials_ticker = plan["financials"]
    if step == "financials":
        query = urlencode({"ticker": financials_ticker, "statement": "income", "periods": 3})
        return f"financials?{query}", 90
    if step == "markets/calendar":
        return f"markets/calendar?{urlencode({'markets': 'SH,HK,US'})}", 30
    if step == "risk/industry":
        return f"risk/industry?{urlencode({'market': market, 'limit_pct': 20})}", 120
    if step == "execution/quality":
        return f"execution/quality?{urlencode({'market': market, 'mode': 'sim'})}", 90
    # ── 按市场过滤的只读端点（每个都显式带 market=）──────────────────────────────
    if step == "market/watchlist":
        return f"market/watchlist?{urlencode({'market': market, 'n': 6})}", 90
    if step == "factors/matrix":
        return f"factors/matrix?{urlencode({'market': market})}", 150
    if step == "risk/analytics":
        return f"risk/analytics?{urlencode({'market': market, 'limit': 120})}", 180
    if step == "execution":
        return f"execution?{urlencode({'market': market})}", 90
    if step == "research":
        return f"research?{urlencode({'market': market})}", 90
    return step, 45


def _market_result(base, market, kind, step, plan):
    """跑一步并把「真实证据」摘出来（成功看 source/条数，失败看错误码与原文）。"""
    if kind == "wb":
        payload, timeout = market_wb_payload(market, step, plan)
        result = call(f"{base}/api/wb/{step}", payload, timeout=timeout)
    else:
        path, timeout = market_v3_query(market, step, plan)
        result = call(f"{base}/api/v3/{path}", timeout=timeout)
    result["step"] = step
    result["kind"] = kind
    value = None
    if isinstance(result.get("raw"), str) and result["raw"].startswith("{"):
        try:
            parsed = json.loads(result["raw"])
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            value = parsed.get("value") if "value" in parsed else parsed
            if result.get("error") is None:
                result["error"] = parsed.get("error")
    elif result.get("ok") is False and result.get("error") is None:
        # SPA 兜底会把「路由不存在」渲染成 200 + HTML：这不是数据缺口，是**服务未加载**该模块
        result["error"] = {
            "code": "probe/not-json",
            "message": "响应不是 JSON（静态兜底返回了页面）→ 该 V3 路由尚未被进程加载，需重启服务",
        }
    result["value"] = value
    result["evidence"] = _evidence(step, value)
    # market= 步骤：核对返回条数与市场一致；不通过则按**探针失败**记（真实响应判定）
    scope_ok, scope_note = _market_scope(market, step, value, result)
    result["scope_ok"] = scope_ok
    result["scope"] = scope_note
    if result.get("ok") is True and scope_ok is False:
        result["ok"] = False
        result["error"] = {"code": "probe/market-scope", "message": scope_note}
    return result


def _evidence(step, value):
    """一句话说明「这一步真拿到了什么」——空壳一律说空壳，不美化。"""
    if not isinstance(value, (dict, list)):
        return "—"
    if step == "series":
        bars = value.get("bars") if isinstance(value, dict) else None
        return f"bars={len(bars or [])} source={value.get('source')}" if isinstance(value, dict) else "—"
    if step == "market_snapshot":
        return f"keys={sorted(value)[:6]}" if isinstance(value, dict) else "—"
    if step == "quote_history_kline_v2":
        rows = value.get("kline_list") if isinstance(value, dict) else None
        return f"kline_list={len(rows or [])}"
    if step == "financials":
        if not isinstance(value, dict):
            return "—"
        return (f"source={value.get('source')} lines={len(value.get('lines') or [])} "
                f"market={value.get('market')}")
    if step == "markets/calendar":
        markets = value.get("markets") if isinstance(value, dict) else None
        if not isinstance(markets, dict):
            return "—"
        return " · ".join(f"{key}:{item.get('session')}" for key, item in markets.items())
    if step == "risk/industry":
        if not isinstance(value, dict):
            return "—"
        top = value.get("top") or {}
        return (f"mapped={len(value.get('mapping') or {})} top={top.get('industry')}"
                f"({top.get('weightPct')}%) breach={value.get('breach')}")
    if step == "execution/quality":
        if not isinstance(value, dict):
            return "—"
        metrics = value.get("metrics") or {}
        return (f"orders={metrics.get('orders')} filled={metrics.get('filled')} "
                f"cancelled={metrics.get('cancelled')} src={value.get('sources', {}).get('orders')}")
    if step == "market/watchlist":
        if not isinstance(value, dict):
            return "—"
        rows = [row for row in (value.get("rows") or []) if isinstance(row, dict)]
        return (f"rows={len(rows)} market={value.get('market')} "
                f"universe_source={value.get('universe_source')} "
                f"首个={rows[0].get('ticker') if rows else None}")
    if step == "factors/matrix":
        if not isinstance(value, dict):
            return "—"
        matrix = value.get("matrix") if isinstance(value.get("matrix"), dict) else {}
        return (f"tickers={len(matrix.get('tickers') or [])} market={value.get('market')} "
                f"universe_source={value.get('universe_source')}")
    if step == "risk/analytics":
        if not isinstance(value, dict):
            return "—"
        analytics = value.get("analytics") if isinstance(value.get("analytics"), dict) else {}
        return (f"成分={len(analytics.get('tickers') or {})} market={value.get('market')} "
                f"benchmark={value.get('benchmark')} beta={analytics.get('beta')} "
                f"portfolioSource={value.get('portfolioSource')} "
                f"varDailyPct={analytics.get('varDailyPct')}")
    if step == "execution":
        if not isinstance(value, dict):
            return "—"
        groups = ((value.get("positions") or {}).get("groups") or [])
        return (f"positions分组={len(groups)} 各市场={[g.get('market') for g in groups]} "
                f"market={value.get('market')} "
                f"oms订单={len((value.get('oms') or {}).get('orders') or [])}")
    if step == "research":
        if not isinstance(value, dict):
            return "—"
        return (f"runs={len(value.get('runs') or [])} reports={len(value.get('reports') or [])} "
                f"market={value.get('market')} filter={value.get('filter', {}).get('keptRuns')}")
    if step == "risk/industry":
        if not isinstance(value, dict):
            return "—"
        top = value.get("top") or {}
        return (f"标的={len(value.get('universe') or [])} 行业数={len(value.get('exposures') or [])} "
                f"top={top.get('industry')}({top.get('weightPct')}%) market={value.get('market')} "
                f"universe_source={value.get('universe_source')}")
    return "—"


def run_market_mode(base, markets):
    """三市场模式主流程：返回 ``{"markets": {market: [rows]}}``。"""
    out = {}
    for market in markets:
        plan = MARKET_PLAN.get(market)
        if plan is None:
            out[market] = [{"step": "-", "kind": "-", "http": None, "ok": False, "ms": 0,
                            "error": {"code": "probe/unknown-market",
                                      "message": f"未知市场 {market}（支持 {sorted(MARKET_PLAN)}）"},
                            "evidence": "—"}]
            continue
        rows = []
        for kind, step, label in MARKET_STEPS:
            result = _market_result(base, market, kind, step, plan)
            result["label"] = label
            if step == "financials":
                # A股/港股必须是富途 f10；美股必须是 SEC——不是预期来源就标成失败（如实核对路由）
                actual = (result.get("value") or {}).get("source") if isinstance(result.get("value"), dict) else None
                expected = plan["financials_source"]
                result["expected_source"] = expected
                result["source_ok"] = (actual == expected)
                if result.get("ok") and not result["source_ok"]:
                    result["ok"] = False
                    result["error"] = {"code": "probe/unexpected-source",
                                       "message": f"期望 {expected}，实际 {actual}"}
            rows.append(result)
        out[market] = rows
    return out


def market_report(results):
    """按市场分组渲染文本表 + Markdown。"""
    lines = ["# V3 三市场只读探针（SH / HK / US）", ""]
    totals = {"ok": 0, "fail": 0, "scope_fail": 0}
    for market, rows in results.items():
        passed = sum(1 for row in rows if row.get("ok") is True)
        failed = sum(1 for row in rows if row.get("ok") is False)
        scope_failed = sum(1 for row in rows if row.get("scope_ok") is False)
        totals["ok"] += passed
        totals["fail"] += failed
        totals["scope_fail"] += scope_failed
        lines.append(f"## {market} · 成功 {passed} / 失败 {failed}"
                     f"（其中按市场核对不通过 {scope_failed}）")
        lines.append("")
        lines.append("| 步骤 | 端点 | ok | 耗时 | 来源/证据 | 按市场核对 | 错误 |")
        lines.append("|---|---|---|---|---|---|---|")
        for row in rows:
            error = row.get("error") or {}
            error_text = f"{error.get('code')}：{str(error.get('message') or '')[:120]}" if error else "—"
            endpoint = f"/api/{'wb' if row['kind'] == 'wb' else 'v3'}/{row['step']}"
            scope = row.get("scope")
            scope_text = ("—" if scope is None
                          else f"{'✅' if row.get('scope_ok') else '❌'} {str(scope)[:160]}")
            lines.append(
                f"| {row.get('label', '-')} | `{endpoint}` | "
                f"{'✅' if row.get('ok') is True else '❌'} | {row.get('ms')}ms | "
                f"{row.get('evidence') or '—'} | {scope_text} | {error_text} |")
        lines.append("")
    lines.insert(2, f"- 总成功 {totals['ok']} · 总失败 {totals['fail']}"
                    f"（按市场核对不通过 {totals['scope_fail']}；表内 ✅/❌ 均为真实响应判定）")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8397")
    parser.add_argument("--json", default="/tmp/e2e-probe.json")
    parser.add_argument("--md", default="/tmp/e2e-probe.md")
    parser.add_argument("--markets", default="",
                        help="三市场模式：逗号分隔的市场（如 SH,HK,US）。给了就只跑该模式。")
    args = parser.parse_args()

    if args.markets.strip():
        markets = [item.strip().upper() for item in args.markets.split(",") if item.strip()]
        results = run_market_mode(args.base, markets)
        report = market_report(results)
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({"markets": results}, handle, ensure_ascii=False, indent=1)
        with open(args.md, "w", encoding="utf-8") as handle:
            handle.write(report + "\n")
        print(report)
        print(f"\n[已写出] {args.json} / {args.md}")
        return

    snapshot = call(f"{args.base}/api/wb/snapshot", {})
    endpoints = []
    if isinstance(snapshot, dict):
        try:
            endpoints = json.loads(snapshot["raw"])["value"]["endpoints"]
        except Exception:  # noqa: BLE001
            endpoints = []

    rows = []
    for path in V3_GET:
        result = probe_v3(args.base, path)
        rows.append({"kind": "v3", "name": f"/api/v3/{path}", **result})
    for path, method in [("ml/backtest", "POST"), ("strategy/run", "POST"), ("oms/sync", "POST"),
                         ("credentials", "POST")]:
        result = probe_v3(args.base, path, method)
        rows.append({"kind": "v3", "name": f"POST /api/v3/{path}", **result})
    for endpoint in endpoints:
        result = probe_wb(args.base, endpoint)
        rows.append({"kind": "wb", "name": f"POST /api/wb/{endpoint}", **result})

    with open(args.json, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=1)

    ok = sum(1 for row in rows if row.get("ok") is True)
    failed = [row for row in rows if row.get("ok") is False]
    skipped = [row for row in rows if row.get("skipped")]
    netfail = [row for row in rows if row.get("http") is None and not row.get("skipped")]
    lines = ["# V3 全量端到端探针（只读）", "",
             f"- 总数 {len(rows)} · 成功 {ok} · 业务失败 {len(failed)} · 跳过 {len(skipped)} · 网络失败 {len(netfail)}", "",
             "## 业务失败（真实错误码与消息）", ""]
    for row in sorted(failed, key=lambda item: item["name"]):
        error = row.get("error") or {}
        lines.append(f"- `{row['name']}` → {error.get('code')}：{str(error.get('message'))[:160]}（{row.get('ms')}ms）")
    lines += ["", "## 跳过", ""]
    for row in skipped:
        lines.append(f"- `{row['name']}` → {row['skipped']}")
    lines += ["", "## 成功端点（按耗时降序，前 30）", ""]
    for row in sorted([r for r in rows if r.get("ok") is True], key=lambda item: -item.get("ms", 0))[:30]:
        lines.append(f"- `{row['name']}` · {row.get('ms')}ms · {row.get('size')}B")
    with open(args.md, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print("\n".join(lines[:40]))
    print(f"\n[已写出] {args.json} / {args.md}")


if __name__ == "__main__":
    main()
