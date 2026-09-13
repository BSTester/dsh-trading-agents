#!/usr/bin/env python3
"""质量因子（基本面）—— 从富途财报原文计算，不做任何推测。

数据源：`quote_financials_statements`，返回的是**利润表**科目（营收/成本/毛利/营业利润/
净利润/研发/税/每股收益/每股股息）。

可算的：毛利率、营业利润率、净利率、研发占比、实际税率、稀释 EPS、每股股息、同比。
注意**科目名按市场不同**（实测）：美股 `Total Revenue as Reported` / `Cost of Revenue` /
`Research & Development`；港股 `Total Revenue` / `Cost of Goods Sold` 且无研发科目；
A股 `Total Operating Revenue` / `Cost of Sales` / `Research and Development` /
`Less:Income tax` 且无 EPS 与 Pretax Profit。因此按候选名依次匹配，缺科目就是缺指标。

**富途算不出的**：ROE / ROA —— 实测富途 91 个工具里**没有资产负债表接口**，
所有报表类型（financial_type 1/2/3/4/7）返回的都是利润表科目。
按渠道优先级（富途优先、其余备用）落到 `trading_datasource.fundamentals`
（Yahoo Finance，A 股再退 AKShare）；两条都拿不到时才列入 `unavailable`，
**不估算、不用行业均值顶替**。

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

from trading_datasource.fundamentals import load_returns  # noqa: E402
from trading_datasource.market import to_futu_symbol  # noqa: E402
from trading_datasource.futu_mcp import FutuUnavailable, call_tool  # noqa: E402

# 富途科目名按市场不同（实测）：
#   美股 Total Revenue as Reported / Cost of Revenue / Research & Development / Tax
#   港股 Total Revenue / Cost of Goods Sold / 无研发 / Tax
#   A股  Total Operating Revenue / Cost of Sales / Research and Development / Less:Income tax
# 因此每个内部键给一串候选名，**按顺序取第一个存在的**；都没有就没有该指标。
ITEM_ALIASES = {
    "revenue": ["Total Revenue as Reported", "Total Revenue", "Total Operating Revenue",
                "Operating Revenue"],
    "cost_of_revenue": ["Cost of Revenue", "Cost of Goods Sold", "Cost of Sales"],
    "gross_profit": ["Gross Profit"],
    "operating_profit": ["Operating Profit"],
    "research_development": ["Research & Development", "Research and Development"],
    "pretax_profit": ["Pretax Profit"],
    "tax": ["Tax", "Less:Income tax", "Income Tax Expense"],
    "net_profit": ["Net Profit", "Net Income to Parent Company", "Net Profit of Parent Company Owners"],
    "diluted_eps": ["Diluted EPS"],
    "dividend_per_share": ["Dividend Per Share"],
}

# 富途侧无法提供的指标；缺失时以「不可得 + 原因」返回（宁可为空，不得估算）。
# ROE/ROA 由备用源补齐，见 trading_datasource.fundamentals。
FUTU_UNAVAILABLE = {
    "roe": {"key": "roe", "label": "净资产收益率 ROE",
            "reason": "富途 91 个工具中没有资产负债表接口，无法取得股东权益"},
    "roa": {"key": "roa", "label": "总资产收益率 ROA",
            "reason": "富途 91 个工具中没有资产负债表接口，无法取得总资产"},
}


def parse_report(report):
    """把一张报表解析成 {键: 值} 加同比；某键的候选名都不存在时该键不出现。"""
    by_name = {item.get("display_name"): item for item in report.get("item_list") or []}
    row = {}
    for key, aliases in ITEM_ALIASES.items():
        item = next((by_name[name] for name in aliases if name in by_name), None)
        if item is None:
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
    # 富途要求 MARKET.CODE 格式（^[A-Z0-9_]+\.[...]$）；直接传 "TSLA" 会被拒
    # 参数校验（ret=-3），而用户输入与 K 线页传过来的往往就是裸代码。
    symbol = to_futu_symbol(ticker)
    data = call_tool("quote_financials_statements", {"symbol": symbol}, timeout=30) or {}
    reports = data.get("report_list") or []
    periods = [period_row(report) for report in reports]
    periods.sort(key=lambda row: row.pop("_sort"), reverse=True)
    if not periods:
        raise RuntimeError("富途未返回任何财报数据")

    # ROE/ROA：富途没有资产负债表接口，按「富途优先、其余备用」落到 Yahoo/AKShare
    returns = load_returns(ticker)
    unavailable = []
    for key, entry in FUTU_UNAVAILABLE.items():
        if returns.get(key) is None:
            reason = entry["reason"]
            if not returns.get("available"):
                reason = f"{reason}；备用源也未取到（{returns.get('reason', '')}）"
            unavailable.append({**entry, "reason": reason})
        else:
            unavailable.append({**entry, "reason": "富途不可得，已由备用源提供",
                                "provided_by": returns.get("source")})
    if returns.get("available"):
        unavailable = [row for row in unavailable if not row.get("provided_by")]

    return {
        "ticker": ticker,
        "symbol": symbol,
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "source": "futu/quote_financials_statements",
        "currency": periods[0].get("currency"),
        "periods": periods,
        "latest": periods[0],
        "returns": returns,
        "unavailable": unavailable,
        "note": "利润率/研发占比/税率由富途财报原文计算（比率 = 对应科目 / 营业收入）；"
                "服务端未给出 financial_type 的枚举含义，故原样保留；"
                + ("ROE/ROA 富途不可得，已按渠道优先级落到备用源，来源与报表期见 returns。"
                   if returns.get("available")
                   else "ROE/ROA 富途与备用源均不可得，未做任何估算。"),
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
