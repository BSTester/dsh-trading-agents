#!/usr/bin/env python3
"""Reddit 搜索抓取 —— 与 X 共用同一专属浏览器配置（登录一次，两个渠道都可用）。

原理：复用 x_search 的浏览器生命周期（专属 user-data-dir + CDP），
打开 old.reddit.com 搜索页并提取帖子标题/分数/评论数；cookie 留在浏览器内。

前置：首次使用需登录一次
    python reddit_search.py --login      # 弹出专属浏览器窗口，登录 reddit.com
之后直接搜索：
    python reddit_search.py "AAPL stock" --count 15 --sort new

输出：JSON {query, count, items:[{title, subreddit, score, comments, url, time}], skipped?}
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from x_search import (DEBUG_PORT, PROFILE_DIR, close_browser_if_we_launched_it,  # noqa: E402
                      close_by_port_best_effort, debug_port_alive, ensure_browser)

LOGIN_URL = "https://www.reddit.com/login"
SEARCH_URL = "https://old.reddit.com/search"


def reddit_reachable(timeout=3):
    """快速预检：仅 TCP 连 reddit.com:443。"""
    import socket
    try:
        with socket.create_connection(("reddit.com", 443), timeout=timeout):
            return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default=None)
    ap.add_argument("--count", type=int, default=15)
    ap.add_argument("--sort", default="relevance", choices=["relevance", "new", "top", "comments"])
    ap.add_argument("--subreddit", default=None, help="限定子版块，如 stocks")
    ap.add_argument("--login", action="store_true", help="首次登录：打开窗口登录 reddit.com")
    ap.add_argument("--keep-browser", action="store_true")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"error": "缺少 playwright 库", "fix": "pip install playwright"}))
        return 1

    if not args.login and not reddit_reachable():
        print(json.dumps({"skip": True, "reason": "reddit.com 不可达，跳过 Reddit 渠道"}))
        return 0

    try:
        if not ensure_browser():
            return 1

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
            page = browser.contexts[0].new_page()

            if args.login:
                page.goto(LOGIN_URL, timeout=60000, wait_until="domcontentloaded")
                print(json.dumps({"status": "请在弹出的浏览器窗口中登录 reddit.com（登录态持久保存）",
                                  "profile": PROFILE_DIR}, ensure_ascii=False))
                for _ in range(120):  # 最多等 6 分钟
                    time.sleep(3)
                    if "login" not in page.url:
                        break
                print(json.dumps({"status": "登录完成（或已离开登录页）", "url": page.url[:90]},
                                 ensure_ascii=False))
                page.close()
                return 0

            if not args.query:
                print(json.dumps({"error": "缺少搜索词（或用 --login 登录）"}))
                return 1

            params = {"q": args.query, "sort": args.sort, "t": "month"}
            if args.subreddit:
                params["q"] = f"subreddit:{args.subreddit} {args.query}"
            url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            time.sleep(1.5)
            if "/login" in page.url or "login" in page.url.split("?")[0]:
                print(json.dumps({"error": "Reddit 需要登录（old.reddit.com 要求账号）",
                                  "fix": "python plugins/fin-data/python/reddit_search.py --login"},
                                 ensure_ascii=False))
                page.close()
                return 0
            page.wait_for_selector("div.search-result, div.thing", timeout=30000, state="attached")
            time.sleep(1.5)

            items = page.evaluate(
                """(limit) => Array.from(document.querySelectorAll('div.search-result, div.thing'))
                     .slice(0, limit).map((node) => {
                       const pick = (sel) => { const e = node.querySelector(sel); return e ? e.innerText.trim() : ''; };
                       const link = node.querySelector('a.search-title, a.title');
                       const attr = (sel, name) => { const e = node.querySelector(sel); return e ? e.getAttribute(name) : null; };
                       return {
                         title: pick('a.search-title, a.title'),
                         subreddit: pick('a.search-subreddit-link, a.subreddit'),
                         score: pick('.search-score, .score.unvoted'),
                         comments: pick('a.search-comments, a.comments'),
                         time: attr('time', 'datetime'),
                         url: link ? link.getAttribute('href') : null,
                       };
                     }).filter((row) => row.title)""",
                args.count,
            )
            page.close()
            base = "https://www.reddit.com"
            for item in items:
                if item.get("url", "").startswith("/"):
                    item["url"] = base + item["url"]
            print(json.dumps({"query": args.query, "sort": args.sort, "count": len(items), "items": items},
                             ensure_ascii=False, indent=1))
            return 0
    except Exception as error:
        print(json.dumps({"error": str(error)[:300],
                          "hint": "若提示登录/无内容：先运行 python reddit_search.py --login 登录一次"}))
        return 1
    finally:
        if not args.keep_browser:
            close_browser_if_we_launched_it()
            close_by_port_best_effort()


if __name__ == "__main__":
    sys.exit(main())
