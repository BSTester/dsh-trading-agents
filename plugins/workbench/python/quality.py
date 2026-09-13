#!/usr/bin/env python3
"""质量因子（基本面）—— 从富途财报原文计算，不做任何推测。

数据源：`quote_financials_statements`，返回的是**利润表**科目（营收/成本/毛利/营业利润/
净利润/研发/税/每股收益/每股股息）。

可算的：毛利率、营业利润率、净利率、研发占比、实际税率、稀释 EPS、每股股息、同比。
**算不出的**：ROE / ROA —— 实测富途 91 个工具里**没有资产负债表接口**，
所有报表类型（financial_type 1/2/3/4/7）返回的都是利润表科目。
因此本模块把 ROE 明确列入 `unavailable` 并给出原因，**不估算、不用行业均值顶替**。

用法:
  python quality.py --ticker US.AAPL
输出: JSON {ticker, currency, periods:[...], latest:{...}, unavailable:[...]}
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trading_datasource.futu_mcp import FutuUnavailable, call_tool  # noqa: E402

# 富途科目名 → 内部键。只映射真实存在的科目；缺科目就缺指标，不用 0 顶替。
ITEM_KEYS = {
    "Total Revenue as Reported": "revenue",
    "Cost of Revenue": "cost_of_revenue",
    "Gross Profit": "gross_profit",
    "Operating Profit": "operating_profit",
    "Research & Development": "research_development",
    "Pretax Profit": "pretax_profit",
    "Tax": "tax",
    "Net Profit": "net_profit",
    "Diluted EPS": "diluted_eps",
    "Dividend Per Share": "dividend_per_share",
}

# 本工具无法提供的指标，连同原因一起返回（宁可为空，不得估算）
UNAVAILABLE = [
    {"key": "roe", "label": "净资产收益率 ROE",
     "reason": "富途 91 个工具中没有资产负债表接口，无法取得股东权益"},
    {"key": "roa", "label": "总资产收益率 ROA",
     "reason": "同上：缺少资产负债表，无法取得总资产"},
]


def parse_report(report):
    """把一张报表解析成 {键: 值} 加同比，缺失科目直接不出现。"""
    row = {"raw": {}}
    for item in report.get("item_list") or []:
        name = item.get("display_name")
        key = ITEM_KEYS.get(name)
        if not key:
            continue
        value = item.get("data")
        row[key] = float(value) if isinstance(value, (int, float)) else None
        # 同比由服务端直接给出，直接用，不自己跨期推算
        if isinstance(item.get("yoy"), (int, float)):
            row[f"{key}_yoy"] = float(item["yoy"])
    return row


def ratio(numerator, denominator):
    """比值为 None 或分母非正时返回 None —— 不用 0 假冒「没有毛利」。"""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return round(numerator / denominator * 100, 2)


def period_row(report):
    row = parse_report(report)
    stamp = report.get("date_time")
    try:
        end = datetime.fromtimestamp(float(stamp) / 1000).strftime("%Y-%m-%d") if stamp else None
    except (TypeError, ValueError, OSError):
        end = None
    revenue = row.get("revenue")
    return {
        "fiscal_year": report.get("fiscal_year"),
        # 服务端未给出 financial_type 的枚举含义，因此原样带上供核对，不自造标签
        "financial_type": report.get("financial_type"),
        "period_end": end,
        "currency": report.get("currency_code"),
        "accounting_standards": report.get("accounting_standards"),
        "revenue": revenue,
        "gross_profit": row.get("gross_profit"),
        "operating_profit": row.get("operating_profit"),
        "net_profit": row.get("net_profit"),
        "research_development": row.get("research_development"),
        "diluted_eps": row.get("diluted_eps"),
        "dividend_per_share": row.get("dividend_per_share"),
        "gross_margin": ratio(row.get("gross_profit"), revenue),
        "operating_margin": ratio(row.get("operating_profit"), revenue),
        "net_margin": ratio(row.get("net_profit"), revenue),
        "rd_ratio": ratio(row.get("research_development"), revenue),
        "effective_tax_rate": ratio(row.get("tax"), row.get("pretax_profit")),
        "revenue_yoy": row.get("revenue_yoy"),
        "net_profit_yoy": row.get("net_profit_yoy"),
        "_sort": stamp or 0,
    }


def collect(ticker):
    symbol = ticker
    data = call_tool("quote_financials_statements", {"symbol": symbol}, timeout=30) or {}
    reports = data.get("report_list") or []
    periods = [period_row(report) for report in reports]
    periods.sort(key=lambda row: row.pop("_sort"), reverse=True)
    if not periods:
        raise RuntimeError("富途未返回任何财报数据")
    return {
        "ticker": ticker,
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "source": "futu/quote_financials_statements",
        "currency": periods[0].get("currency"),
        "periods": periods,
        "latest": periods[0],
        "unavailable": UNAVAILABLE,
        "note": "指标由富途财报原文计算（比率 = 对应科目 / 营业收入）；"
                "服务端未给出 financial_type 的枚举含义，故原样保留；"
                "ROE/ROA 因缺少资产负债表而不可得，未做任何估算。",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    args = ap.parse_args()
    try:
        payload = collect(args.ticker)
    except (FutuUnavailable, RuntimeError) as error:
        print(json.dumps({"error": str(error)[:220]}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
