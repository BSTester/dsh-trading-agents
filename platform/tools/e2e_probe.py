#!/usr/bin/env python3
"""V3 全量端到端探针（**只读**）。

覆盖：全部 /api/v3/* 路由 + 全部 /api/wb/<endpoint> 端点（82 项）。
   * 写类/下单类端点**一律跳过**（switch-mode / plan-execute / confirm-decide / trade_* /
     sim_trade_* / modify_user_security / oms-sync / credentials 写动作），只报「已跳过（写动作）」；
   * 其余端点先用空载荷探测，若返回「缺少必填字段 X」则按默认值表补齐重试（最多 3 轮）；
   * 记录：HTTP 状态、耗时、ok、错误码与消息、返回体量、以及是否拿到了真实数据。

用法：python3 platform/tools/e2e_probe.py [--base http://127.0.0.1:8397] [--json out.json] [--md out.md]
"""
import argparse
import json
import re
import time
import urllib.error
import urllib.request

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
    "action": "status", "key": "tushare_token", "value": "", "no_probe": True,
    "confirmation": "", "older_than_minutes": 120, "run_id": "",
}
V3_GET = [
    "overview", "metrics", "brain", "market", "market/watchlist", "plates", "orderbook",
    "factors/matrix", "strategy", "risk", "risk/analytics", "execution", "oms/orders",
    "gateway", "tools", "settings", "events", "audit", "credentials", "research",
    "research/tasks", "news", "spot", "financials", "tushare", "openbb", "ml/sweep",
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
    "tushare": {"api": "income", "ts_code": "600519.SH", "period": "20260630"},
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
        return call(url, {"action": "status", "key": "tushare_token"}, timeout=30)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8397")
    parser.add_argument("--json", default="/tmp/e2e-probe.json")
    parser.add_argument("--md", default="/tmp/e2e-probe.md")
    args = parser.parse_args()

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
