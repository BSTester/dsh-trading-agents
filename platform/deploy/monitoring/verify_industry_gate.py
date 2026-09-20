#!/usr/bin/env python
"""行业红线闸门：**真机只读**验证（不重启服务、不下单、不写任何文件）。

用途：拿 ``<DSH_HOME>/v3-risk-probe.json`` 的**真实**行业暴露读数，构造台账订单过
``v3_ops.check_order`` / ``OmsLedger._upsert`` 这条判定链，打印放行/阻断的真实
``reasons`` 与 ``history`` 留痕；再对比「缓存过期 → fail-open」与
``/api/v3/metrics`` 的 ``industryGate`` 计数。

只读保证：
  * 只读 ``v3-risk-probe.json``（``observability.load_risk_probe``）；
  * 台账用**内存里的构造值**（不写 ``v3.db``、不写 ``v3-oms-orders.json``）；
  * 工具面替身只回只读工具的空信封，且**行业读数全部来自缓存**（缓存新鲜 → 一次上游不打）；
  * 不触达 ``trade_*`` / ``sim_trade_*`` / ``plan-execute`` / ``confirm-decide``。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B deploy/monitoring/verify_industry_gate.py``
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PLATFORM = Path(__file__).resolve().parents[2]  # platform/
sys.path.insert(0, str(PLATFORM))

from server import observability, v3_risk_gate  # noqa: E402
from server import v3_ops  # noqa: E402

HOME = Path.home() / ".dsh"
NAV = 100000.0


def read_only_caller(name, payload=None):
    """工具面替身：只回只读空信封（``/api/v3/risk/industry`` 的板块工具不在其中）。"""
    values = {
        "plan": {"ok": True, "value": {"plans": [], "alerts": []}},
        "equity": {"ok": True, "value": {"current": NAV, "max_drawdown": None}},
        "positions": {"ok": True, "value": {"groups": []}},
        "orders_open": {"ok": True, "value": {"groups": []}},
        "confirmation": {"ok": True, "value": {"pending": []}},
    }
    return values.get(name, {"ok": False, "error": {"code": "verify/not-routed",
                                                    "message": f"只读验证不路由 {name}"}})


def show(title):
    print(f"\n=== {title} ===")


def main():
    raw = observability.load_risk_probe(HOME)
    if raw is None:
        print("没有可用的 <DSH_HOME>/v3-risk-probe.json —— 先跑 "
              "curl -fsS -X POST 'http://127.0.0.1:8397/api/v3/metrics/probe/refresh'")
        return 1
    show("0. 落盘探测缓存（真实读数）")
    print(f"generated_at={raw['generated_at']} ({raw.get('generated_at_iso')}) "
          f"limit_pct={raw['limit_pct']} max_age_s={observability.risk_probe_max_age():.0f} "
          f"age_h={(time.time() - raw['generated_at']) / 3600:.2f}")
    for market, row in sorted(raw["per_market"].items()):
        print(f"  {market}: top={row.get('top_industry')} {row.get('top_weight_pct')}% "
              f"breach={row.get('breach')} source={row.get('source')} "
              f"missing={row.get('missing')} universe={row.get('universe')}")

    ledger = v3_ops.OmsLedger(read_only_caller, HOME)
    probe_plan = [{"plan_id": "P-verify", "status": "frozen", "mode": "sim",
                   "orders": [{"client_order_id": "V-SH", "symbol": "SH.600000"},
                              {"client_order_id": "V-HK", "symbol": "HK.00700"},
                              {"client_order_id": "V-US", "symbol": "US.NVDA"}]}]
    contexts, error, base = ledger._industry_contexts(plans=probe_plan)
    show("1. industry_context（真缓存 → 逐市场读数，全部 cache_state=fresh）")
    print(json.dumps({key: {k: value.get(k) for k in
                            ("industry_pct", "industry_source", "top_industry", "cache_state",
                             "origin", "probe_age_ms", "missing", "reason")}
                      for key, value in contexts.items()},
                     ensure_ascii=False, indent=2)[:2000])
    print(f"industry_error={error}")

    show("2. 构造订单过「下单前闸门」（check_order，纯函数；不落盘、不下单）")
    cases = [
        ("SH-1pct", "SH.600000", 100.0, 10.0),     # 1000/100000 = 1% → 单笔也不触线
        ("SH-40pct", "SH.600000", 4000.0, 10.0),   # 40% → 单笔 manual + 行业阻断
        ("HK-1pct", "HK.00700", 100.0, 10.0),
        ("US-1pct", "US.NVDA", 100.0, 10.0),
        ("RAW-no-prefix", "600000", 100.0, 10.0),  # 认不出市场 → 各市场最严（US 50%）
    ]
    for order_id, symbol, qty, price in cases:
        market = v3_risk_gate.market_of_order(symbol)
        industry = contexts[v3_risk_gate.industry_context_key(market)]
        context = industry["context"]
        value = qty * price
        action, reasons = v3_ops.check_order(
            value, context["nav"], context["industry_pct"], context["drawdown_pct"],
            industry_source=context.get("industry_source"),
            industry_top=context.get("industry_top"),
            industry_as_of=context.get("industry_as_of"),
            industry_probe_age_ms=context.get("industry_probe_age_ms"),
            industry_missing=context.get("industry_missing"),
            industry_universe=context.get("industry_universe"),
            industry_market=context.get("industry_market"),
            industry_reason=context.get("industry_reason"))
        stage = {"blocked_industry": "blocked_industry", "blocked": "blocked",
                 "manual": "manual"}.get(action, "risk_passed")
        print(f"\n[{order_id}] {symbol} value={value:.0f} ({value / context['nav'] * 100:.2f}% NAV)"
              f" 行业读数={context['industry_pct']}%（{context.get('industry_top')}）")
        print(f"  action={action} stage={stage}")
        for reason in reasons:
            print(f"  reason: {reason}")

    show("3. 台账留痕（_upsert → history，内存对象，不写盘）")
    record = ledger._upsert({}, {"client_order_id": "AUDIT-SH-40pct", "symbol": "SH.600000",
                                 "side": "BUY", "qty": 40000.0, "price": 1.0,
                                 "status": "frozen"},
                            context=contexts["SH"]["context"],
                            plan_record={"plan_id": "P-verify", "status": "frozen",
                                         "mode": "sim"},
                            industry=contexts["SH"])
    print(json.dumps({"id": record["id"], "stage": record["stage"],
                      "industry_pct": record["industry_pct"],
                      "industry_source": record["industry_source"],
                      "industry_top": record["industry_top"],
                      "industry_as_of": record["industry_as_of"],
                      "risk": record["risk"], "history": record["history"]},
                     ensure_ascii=False, indent=2)[:2600])

    show("4. fail-open：把最大年龄压到 1ms（模拟缓存过期）→ 不阻断但必须留痕")
    stale = v3_risk_gate.industry_context(HOME, "SH", max_age_ms=1)
    action, reasons = v3_ops.check_order(1000.0, NAV, stale["industry_pct"],
                                        industry_source=stale["industry_source"],
                                        industry_reason=stale["reason"])
    print(f"cache_state={stale['cache_state']} industry_pct={stale['industry_pct']} "
          f"industry_source={stale['industry_source']}")
    print(f"reason={stale['reason']}")
    print(f"check_order → action={action}（不阻断）reasons={reasons}")

    show("5. /api/v3/metrics 口径（gate_view，只读缓存、零工具调用）")
    print(json.dumps(ledger.gate_view(), ensure_ascii=False, indent=2))

    show("6. 真机服务只读对照（同一份缓存口径的端点；不重启服务）")
    print("curl -fsS 'http://127.0.0.1:8397/api/v3/oms/orders' | "
          "jq '{industry_pct,industry_source,industry_markets}'")
    print("curl -fsS 'http://127.0.0.1:8397/api/v3/risk/industry?market=SH&limit_pct=20' | "
          "jq '{ok,breach,top,sources,missing}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
