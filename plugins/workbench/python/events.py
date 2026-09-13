#!/usr/bin/env python3
"""事件与日历：分红/除权除息、财报披露预约（A股，AKShare 源）。

用法:
  python events.py --ticker 600519 [--days 180]
输出: JSON {ticker, as_of, events:[{date, type, detail, source}], sources_status}
"""
import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta


def is_a_share(ticker):
    return bool(re.fullmatch(r"\d{6}", str(ticker).split(".")[0]))


def parse_date(value):
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    return None


def dividends(code):
    import akshare as ak
    df = ak.stock_history_dividend_detail(symbol=code, indicator="分红")
    rows = []
    for record in df.to_dict("records"):
        ex_date = parse_date(record.get("除权除息日"))
        announce = parse_date(record.get("公告日期"))
        when = ex_date or announce
        if when is None:
            continue
        detail = "送{0} 转{1} 派{2} · {3}".format(
            record.get("送股", "-"), record.get("转增", "-"), record.get("派息", "-"),
            record.get("进度", "-"))
        rows.append({"date": when.isoformat(), "type": "分红/除权除息",
                     "detail": detail, "announced": announce.isoformat() if announce else None,
                     "source": "akshare/stock_history_dividend_detail"})
    return rows


def disclosures(code, period=None):
    """财报披露预约。逐个报告期尝试——当年报告期常尚未发布（空表），需跳过而非报错。"""
    import akshare as ak
    year = date.today().year
    periods = [period] if period else [f"{year}年报", f"{year}半年报", f"{year}三季报",
                                       f"{year}一季报", f"{year - 1}年报"]
    rows = []
    for name in periods:
        try:
            df = ak.stock_report_disclosure(market="沪深京", period=name)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        for record in df.to_dict("records"):
            if str(record.get("股票代码")) != code:
                continue
            for label in ("首次预约", "初次变更", "二次变更", "三次变更"):
                when = parse_date(record.get(label))
                if when is None:
                    continue
                rows.append({"date": when.isoformat(), "type": "财报披露",
                             "detail": f"{name} · {label}", "source": "akshare/stock_report_disclosure"})
        if rows:
            break
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--days", type=int, default=180, help="仅保留今天前后该天数内的事件")
    args = ap.parse_args()

    code = str(args.ticker).split(".")[0]
    if not is_a_share(args.ticker):
        print(json.dumps({"error": "事件数据当前仅支持 A 股（AKShare 源）；港美股请用富途工具查询"},
                         ensure_ascii=False))
        return 0

    events, status = [], {}
    today = date.today()
    horizon = today + timedelta(days=args.days)
    floor = today - timedelta(days=args.days)

    for name, producer in (("dividend", lambda: dividends(code)),
                           ("disclosure", lambda: disclosures(code))):
        try:
            produced = producer()
            status[name] = "ok" if produced else "empty"
            events.extend(produced)
        except Exception as error:
            status[name] = f"fail: {str(error)[:80]}"

    kept = []
    for event in events:
        when = parse_date(event["date"])
        if when is None or not (floor <= when <= horizon):
            continue
        event["days_until"] = (when - today).days
        kept.append(event)
    kept.sort(key=lambda e: e["date"])

    print(json.dumps({"ticker": args.ticker, "as_of": today.isoformat(),
                      "window_days": args.days, "events": kept[:40],
                      "sources_status": status,
                      "note": "事件来自公开披露源，日期可能变更；交易前请以交易所/公司公告为准。"},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
