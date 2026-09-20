#!/usr/bin/env python3
"""离线导出 ``GET /metrics`` 的 Prometheus 文本（供 ``promtool check metrics`` 自检）。

用途：平台服务重启前、或 CI 里没有运行中的 8397 时，也能对指标出口做**真实**校验：

    cd platform
    ~/.dsh/trading-venv/bin/python -B deploy/monitoring/dump_metrics.py > /tmp/metrics.txt
    docker run --rm -i --entrypoint promtool prom/prometheus check metrics < /tmp/metrics.txt

它用 ``server.app.create_app`` 建一个**真实**应用（全部子模块按生产路径接线），
只是不跑 lifespan（不起调度器、不连富途），因此不会产生任何外部调用和写操作。

可选参数：
  ``--wait SECONDS``   等探测快照就绪的最长秒数（默认 5；探测在后台线程里跑，
                       拿不到就**不导出**探测类指标——这是有意的诚实行为）。
  ``--with-probe-cache`` 额外造一份数据源降级链缓存，让 ``quantwb_datasource_*`` 也出现
                       （仅为让清单完整；**不是**真实探测结果，故默认关闭）。
  ``--with-risk-probe-cache`` 额外造一份**示例**行业暴露探测缓存，让
                       ``quantwb_risk_industry_*`` 也出现（同上：只为清单完整，
                       不是真实探测结果；真实缓存由
                       ``POST /api/v3/metrics/probe/refresh`` 写）。
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # platform/
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from server import observability  # noqa: E402
from server.app import create_app  # noqa: E402

#: 仅为让降级链 family 出现在清单里；值是**标注出来的假数据**，不是探测结果。
SAMPLE_CHAINS = [
    {"key": "kline", "label": "K 线/历史行情", "primary": "futu/quote_history_kline",
     "fallback": "akshare/stock_zh_a_hist", "available": True,
     "last_source": "akshare/stock_zh_a_hist", "chain_size": 2, "error": None},
    {"key": "news", "label": "个股资讯", "primary": "futu/info_search",
     "fallback": "akshare/stock_news_em", "available": False,
     "last_source": "akshare/stock_news_em", "chain_size": 2, "error": "HTTP 500"},
]

#: 仅为让行业红线 family 出现在清单里；值是**标注出来的假数据**，不是探测结果。
SAMPLE_RISK_PER_MARKET = {
    "SH": {"ok": True, "as_of": "2026-09-20T00:00:00+00:00", "limitPct": 20.0,
           "breach": True, "top": {"industry": "示例行业", "weightPct": 37.5},
           "sources": {"plate": "futu/info_owner_plate"}, "missing": [],
           "universe": ["SH.600000"]},
    "US": {"ok": False, "error": {"code": "industry/no-universe",
                                  "message": "market=US 既没有配置自选池、也没有真实持仓"}},
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", type=float, default=5.0,
                        help="等探测快照就绪的最长秒数（默认 5）")
    parser.add_argument("--with-probe-cache", action="store_true",
                        help="造一份**示例**降级链缓存，让 quantwb_datasource_* 出现")
    parser.add_argument("--with-risk-probe-cache", action="store_true",
                        help="造一份**示例**行业暴露缓存，让 quantwb_risk_industry_* 出现")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="quant-metrics-dump-") as home:
        if args.with_probe_cache:
            observability.record_datasource_probe(home, SAMPLE_CHAINS)
        if args.with_risk_probe_cache:
            observability.record_risk_probe(
                home,
                observability.build_risk_probe_payload(
                    SAMPLE_RISK_PER_MARKET, limit_pct=observability.RISK_PROBE_LIMIT_PCT))
        app = create_app(home=home)
        client = TestClient(app)
        client.get("/metrics")  # 首次抓取会触发后台探测
        deadline = time.time() + max(0.0, args.wait)
        while time.time() < deadline:
            if "quantwb_workbench_up" in client.get("/metrics").text:
                break
            time.sleep(0.2)
        sys.stdout.write(client.get("/metrics").text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
