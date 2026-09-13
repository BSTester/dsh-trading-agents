#!/usr/bin/env python3
"""统一财经快讯获取：富途公开快讯 → AKShare(A股) → Yahoo RSS(港美) 自动路由。

用法:
  python fin_news.py --ticker 00700.HK --count 8 [--name 腾讯] [--lang zh-HK]
输出: JSON {ticker, items:[{source,title,url,time}], sources_status:{...}}
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

# A 股判定只有一份实现：显式市场标注优先（000001.HK 是港股，不是平安银行）
from trading_datasource.market import (  # noqa: E402
    is_a_share, to_futu_symbol, to_yahoo_symbol)


def http_get(url, headers=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def futu_news(keyword, count, lang):
    """富途公开快讯接口（免鉴权，实测可用）。"""
    base = "https://ai-news-search.moomoo.com/news_search"
    qs = urllib.parse.urlencode({"keyword": keyword, "size": min(count, 20),
                                 "news_type": 1, "lang": lang, "sort_type": 2})
    data = json.loads(http_get(f"{base}?{qs}"))
    if data.get("code") != 0:
        raise RuntimeError(f"futu code={data.get('code')}")
    strip = lambda s: re.sub(r"</?em>", "", s or "")
    return [{"source": "futu", "title": strip(n.get("title")),
             "url": n.get("url"), "time": n.get("publish_time")}
            for n in data.get("data", [])[:count]]


def akshare_news(ticker, count):
    """A 股财经快讯（东财）。自身判市场，避免被误调后拿错标的的新闻。"""
    if not is_a_share(ticker):
        raise RuntimeError(f"东财快讯仅支持 A 股：{ticker}")
    import akshare as ak
    df = ak.stock_news_em(symbol=to_futu_symbol(ticker).split(".")[1])
    rows = df.head(count).to_dict("records")
    return [{"source": "akshare", "title": r.get("新闻标题"),
             "url": r.get("新闻链接"), "time": str(r.get("发布时间"))} for r in rows]


def yahoo_news(ticker, count):
    """Yahoo RSS（港美股）。

    必须走共享的符号归一：Yahoo 的港股是 4 位代码，直接拿 `00700.HK` 去查
    返回 0 条（实测 `0700.HK` 有 17 条），港股快讯就只剩富途那一条。
    """
    symbol = to_yahoo_symbol(ticker) or ticker.upper()
    qs = urllib.parse.urlencode({"s": symbol, "region": "US", "lang": "en-US"})
    xml = http_get(f"https://feeds.finance.yahoo.com/rss/2.0/headline?{qs}")
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S)[:count]:
        def pick(tag):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
            return m.group(1).strip() if m else None
        items.append({"source": "yahoo", "title": pick("title"),
                      "url": pick("link"), "time": pick("pubDate")})
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--name", default=None, help="公司名（富途快讯关键词更准）")
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--lang", default="zh-HK")
    args = ap.parse_args()

    keyword = args.name or args.ticker
    items, status = [], {}

    for label, fn in (
        ("futu", lambda: futu_news(keyword, args.count, args.lang)),
        ("akshare", lambda: akshare_news(args.ticker, args.count) if is_a_share(args.ticker) else (_ for _ in ()).throw(RuntimeError("A股专用"))),
        ("yahoo", lambda: yahoo_news(args.ticker, args.count)),
    ):
        try:
            got = fn()
            status[label] = "ok" if got else "empty"
            items.extend(got)
        except Exception as e:
            status[label] = f"fail: {str(e)[:80]}"
        if len(items) >= args.count:
            break

    print(json.dumps({"ticker": args.ticker, "items": items[:args.count],
                      "sources_status": status}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
