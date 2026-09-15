#!/usr/bin/env python3
"""数据依赖核验聚合脚本（WP5 任务 4；规格 §2.2 协议 + §13 数据可得性审计）。

聚合各 WP 已锁定的外部依赖核验调用，逐项输出 OK/FAIL JSON（ensure_ascii=False）：
- 规格 §13.2 六项：指数K线基准 / 交易日历 / 当前指数成分 / 行业板块 / 基础信息 / 财报结构
- WP2 锁定：中证800 基准（SH.000906）、估值字段路径（valuation_values 唯一实现）
- WP3 锁定：券商工具名与必填参数常量、券商通道（sim_trade_account_list）

约定：富途侧发版异常（internal error 面扩大）时**先跑本脚本**定位漂移面。
失败项附「影响工作包」（workpackages）与「规格缺口编号」（gap，§13.4 缺口①—④）；
schema 是否漂移的离线断言在 tests/test_core_wp*_locks.py，本脚本只做真实通道体检。

用法：~/.dsh/trading-venv/bin/python -B scripts/verify-data-deps.py
（全部 OK 退出码 0，任一 FAIL 退出码 1。）
"""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))

# §13.2 实测锁定：标普指数代码无效，用 US.SPY ETF 替代
KLINE_INDEXES = ["SH.000300", "HK.800000", "US..IXIC", "US..DJI", "US.SPY"]


def _check_index_kline(fetcher):
    counts = {}
    for symbol in KLINE_INDEXES:
        # ktype 必须整数（日线=2）；end 必传（ret=-3 要求 end/next_time 二选一），
        # 口径同唯一锁定实现 market.fetch_futu（§13.2/§13.5）
        data = fetcher("quote_history_kline",
                       {"symbol": symbol, "ktype": 2, "num": 5,
                        "end": dt.date.today().isoformat()}) or {}
        counts[symbol] = len(data.get("kline_list") or [])
    missing = [s for s, n in counts.items() if not n]
    return (not missing,
            f"日线根数 {counts}" + (f"；空：{missing}" if missing else ""))


def _check_trading_days(fetcher):
    data = fetcher("quote_trading_days",
                   {"market": "SH", "start": "2026-09-01", "end": "2026-09-30"}) or {}
    days = data.get("trading_days") or []
    typed = [d for d in days if d.get("trade_date_type")]
    return bool(typed), f"9 月交易日 {len(days)} 天，含 trade_date_type {len(typed)} 条"


def _check_components(fetcher):
    data = fetcher("quote_valuation_index_component_stock_list",
                   {"symbol": "SH.000300", "limit": 50}) or {}
    stocks = data.get("stock_list") or []
    return bool(stocks), f"成分 {len(stocks)} 条（分页接口，取样首页）"


def _check_owner_plate(fetcher):
    data = fetcher("quote_owner_plate", {"symbol": "SH.600519"}) or {}
    lists = [v for v in data.values() if isinstance(v, list) and v]
    return bool(lists), f"非空列表字段 {len(lists)} 个（行业过滤按 plate_type，实现侧职责）"


def _check_basicinfo(fetcher):
    data = fetcher("quote_stock_basicinfo", {"code_list": ["SH.600519"]}) or {}
    items = data.get("basic_list") or []
    return bool(items), f"basic_list {len(items)} 条（lot_size/listing_date/suspension）"


def _check_financials(fetcher):
    data = fetcher("quote_financials_statements", {"symbol": "SH.600519"}) or {}
    reports = data.get("report_list") or []
    dated = [r for r in reports if r.get("date_time")]
    return bool(dated), f"带报告期 {len(dated)} 期（富途无公告日字段 → 缺口①，WP1 双源合并兜底）"


def _check_csi800(fetcher):
    from trading_datasource.market import INDEX_SYMBOLS
    symbol = INDEX_SYMBOLS["csi800"]
    data = fetcher("quote_history_kline",
                   {"symbol": symbol, "ktype": 2, "num": 5,
                    "end": dt.date.today().isoformat()}) or {}
    return bool(data.get("kline_list")), f"{symbol} 日线 {len(data.get('kline_list') or [])} 根"


def _check_valuation(fetcher):
    from trading_core.factors import valuation_values
    values, source = valuation_values("SH.600519", fetcher=fetcher)
    return "pe_ttm" in values, f"字段 {sorted(values)} 来源 {source or '空'}"


def _check_broker_constants(fetcher):
    from trading_core import broker
    # 与 tests/test_core_wp3_locks.py 同口径：逐键核对锁定名（TOOLS 另含 cash/max_buy_sell）
    locked = {"place": "sim_trade_input_order", "cancel": "sim_trade_cancel_order",
              "positions": "sim_trade_position_list", "accounts": "sim_trade_account_list",
              "history": "sim_trade_history_order_list"}
    drift = [f"{k}→{broker.TOOLS.get(k)}" for k, v in locked.items()
             if broker.TOOLS.get(k) != v]
    if set(broker.PLACE_REQUIRED) != {"acc_id", "market", "symbol",
                                      "order_type", "order_side", "qty"}:
        drift.append(f"PLACE_REQUIRED={broker.PLACE_REQUIRED}")
    return not drift, "与 WP3 锁定表一致" if not drift else f"漂移：{drift}"


def _check_broker_channel(fetcher):
    data = fetcher("sim_trade_account_list", {}) or {}
    return True, f"通道可达，返回预览 {json.dumps(data, ensure_ascii=False)[:120]}"


# (核验项, 影响工作包, 规格缺口编号§13.4, 检查函数)；gap=None 表示该项无缺口关联
CHECKS = [
    ("指数K线基准（沪深300/恒指/纳指/道指/SPY）", ["WP1", "WP2"], None, _check_index_kline),
    ("交易日历（market 大写/start-end 必传）", ["WP1", "WP3", "WP4"], None, _check_trading_days),
    ("当前指数成分（SH.000300）", ["WP2"],
     "缺口②（历史成分降级：当前成分 + bias_note 幸存者偏差标注）", _check_components),
    ("行业/板块归属（quote_owner_plate）", ["WP2", "WP4"], None, _check_owner_plate),
    ("基础信息（lot_size/上市日/停牌）", ["WP2", "WP3"], None, _check_basicinfo),
    ("财报报表结构（利润表科目/报告期）", ["WP1", "WP2"],
     "缺口①（富途无公告日，WP1 双源合并兜底）", _check_financials),
    ("中证800 基准（SH.000906）", ["WP2"], None, _check_csi800),
    ("估值字段路径（quote_valuation_detail×3）", ["WP2", "WP4"], None, _check_valuation),
    ("券商工具锁定常量（WP3 锁定表）", ["WP3"], None, _check_broker_constants),
    ("券商通道（sim_trade_account_list）", ["WP3", "WP4"], None, _check_broker_channel),
]


def build_report(fetcher=None, now=None):
    """逐项核验并聚合成 JSON 友好结构；fetcher 注入供离线测试（仓库既有模式）。"""
    if fetcher is None:
        from trading_datasource.futu_mcp import call_tool
        fetcher = call_tool
    stamp = now or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    results = []
    for item, workpackages, gap, fn in CHECKS:
        try:
            ok, detail = fn(fetcher)
        except Exception as error:  # noqa: BLE001 —— 单项失败不阻塞其余核验
            ok, detail = False, f"{type(error).__name__}: {str(error)[:140]}"
        results.append({"item": item, "ok": bool(ok), "detail": str(detail)[:200],
                        "workpackages": workpackages, "gap": gap})
    failed = sum(1 for r in results if not r["ok"])
    return {"script": "verify-data-deps", "as_of": stamp, "results": results,
            "summary": {"total": len(results), "ok": len(results) - failed,
                        "fail": failed}}


def main(argv=None, fetcher=None, now=None):
    report = build_report(fetcher=fetcher, now=now)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
