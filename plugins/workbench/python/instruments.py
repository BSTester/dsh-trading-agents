#!/usr/bin/env python3
"""标的卡片：名称/最新价/涨跌/区间/成交 + 富途跳转链接。

**富途优先**：实时快照（港股/美股可用；A股实时行情需权限）→
回退静态信息 + 最近日线收盘（A股同样可用，标注数据来源与日期）。

用法:
  python instruments.py --ticker 00700.HK
输出: JSON {ticker, symbol, market, name, price, change_pct, ..., futu_url, source}
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from trading_datasource.market import load_bars, to_futu_symbol  # noqa: E402
from trading_datasource.futu_mcp import FutuUnavailable, call_tool  # noqa: E402

FUTU_STOCK_URL = "https://www.futunn.com/stock/{code}-{market}"


def instrument(ticker):
    symbol = to_futu_symbol(ticker)
    market, _, code = symbol.partition(".")
    card = {
        "ticker": ticker, "symbol": symbol, "market": market,
        "futu_url": FUTU_STOCK_URL.format(code=code, market=market),
        "name": None, "name_en": None, "price": None, "prev_close": None,
        "change_pct": None, "open": None, "high": None, "low": None,
        "volume": None, "turnover": None, "lot_size": None,
        "as_of": None, "source": None, "note": None,
    }

    # ① 富途实时快照（优先；A股需行情权限，失败即回退）
    try:
        data = call_tool("quote_stock_quote", {"code_list": [symbol]})
        quote = (data.get("quote_list") or [{}])[0]
        card.update({
            "name": quote.get("sc_name") or quote.get("name"),
            "name_en": quote.get("name"),
            "price": quote.get("last_price"),
            "prev_close": quote.get("prev_close_price"),
            "open": quote.get("open_price"),
            "high": quote.get("high_price"),
            "low": quote.get("low_price"),
            "volume": quote.get("volume"),
            "turnover": quote.get("turnover"),
            "as_of": str(quote.get("data_date") or ""),
            "source": "futu/quote_stock_quote(实时快照)",
        })
    except FutuUnavailable as error:
        card["note"] = f"实时快照不可用（{str(error)[:60]}）；以下为最近日线收盘"

    # ② 静态信息（名称/每手股数）
    if card["name"] is None or card["lot_size"] is None:
        try:
            basic = call_tool("quote_stock_basicinfo", {"code_list": [symbol]})
            item = (basic.get("basic_list") or [{}])[0]
            card["name"] = card["name"] or item.get("name")
            card["lot_size"] = item.get("lot_size")
        except FutuUnavailable:
            pass

    # ③ 价格回退：最近日线（富途优先，A股长历史走新浪）
    if card["price"] is None:
        try:
            bars, source, _stale = load_bars(ticker, "1d", 2)
            if bars:
                last = bars[-1]
                previous = bars[-2] if len(bars) > 1 else None
                card.update({"price": last["c"], "prev_close": previous["c"] if previous else None,
                             "open": last["o"], "high": last["h"], "low": last["l"],
                             "volume": last["v"], "as_of": last["t"], "source": source + "(日线收盘)"})
        except Exception as error:  # noqa: BLE001
            card["note"] = f"无法取到该标的行情：{str(error)[:100]}"

    if card["price"] is not None and card["prev_close"]:
        try:
            card["change_pct"] = round((float(card["price"]) / float(card["prev_close"]) - 1) * 100, 2)
        except (TypeError, ZeroDivisionError):
            card["change_pct"] = None
    return card


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    args = ap.parse_args()
    try:
        card = instrument(args.ticker)
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"error": str(error)[:200]}, ensure_ascii=False))
        return 1
    print(json.dumps(card, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
