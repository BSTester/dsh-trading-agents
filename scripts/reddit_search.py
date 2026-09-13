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
# 注意：old.reddit.com 要求单独登录（与 www 不共享会话）；www 的搜索结果属性可直接读取。
SEARCH_URL = "https://www.reddit.com/search/"


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

            params = {"q": args.query, "sort": args.sort, "type": "posts"}
            if args.subreddit:
                params["q"] = f"subreddit:{args.subreddit} {args.query}"
            url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            time.sleep(4)  # 新 Reddit 为 Web Component，需等其挂载
            if "/login" in page.url:
                print(json.dumps({"error": "Reddit 需要登录",
                                  "fix": "python plugins/fin-data/python/reddit_search.py --login"},
                                 ensure_ascii=False))
                page.close()
                return 0
            try:
                page.wait_for_selector('a[href*="/comments/"]', timeout=25000, state="attached")
            except Exception:
                print(json.dumps({"error": "搜索页未返回结果（可能未登录或被限流）",
                                  "fix": "python plugins/fin-data/python/reddit_search.py --login"}))
                page.close()
                return 0
            page.mouse.wheel(0, 1200)  # 触发懒加载
            time.sleep(2)

            items = page.evaluate(
                """(limit) => {
                  const seenUrl = new Set();
                  const rows = [];
                  for (const link of document.querySelectorAll('a[href*="/comments/"]')) {
                    const href = link.getAttribute('href') || '';
                    const title = (link.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (!title || title.length < 8 || !href) continue;
                    if (seenUrl.has(href)) continue;   // 同一帖子有多个链接（标题/缩略图），按 URL 去重
                    seenUrl.add(href);

                    // 帖子级容器：优先 search-telemetry-tracker，其次向上最多 4 层，
                    // 并要求文本长度合理（避免误取整个结果列表容器）。
                    const candidateOf = (start) => {
                      const byTracker = start.closest('search-telemetry-tracker');
                      if (byTracker && (byTracker.innerText || '').length < 1500) return byTracker;
                      let node = start;
                      for (let i = 0; i < 4 && node; i += 1) {
                        node = node.parentElement;
                        if (!node) break;
                        const text = node.innerText || '';
                        if (/comment/i.test(text) && text.length < 1500) return node;
                      }
                      return null;
                    };
                    const container = candidateOf(link);
                    let score = '', comments = '';
                    if (container) {
                      const nums = Array.from(container.querySelectorAll('faceplate-number'))
                        .map((el) => el.getAttribute('number') || el.innerText || '')
                        .filter((value) => value && /[0-9]/.test(value));
                      if (nums.length >= 2) { score = nums[0]; comments = nums[1]; }
                      else if (nums.length === 1) { comments = nums[0]; }
                      const text = container.innerText || '';
                      if (!score) score = (text.match(/([\\d.,]+[KM]?)\\s*(upvote|vote)/i) || [])[1] || '';
                      if (!comments) comments = (text.match(/([\\d.,]+[KM]?)\\s*comment/i) || [])[1] || '';
                    }
                    const sub = href.match(/\\/r\\/([^\\/]+)\\//);
                    const time = (link.closest('search-telemetry-tracker') || container || link)
                      .querySelector('time')?.getAttribute('datetime') || '';
                    rows.push({ title, subreddit: sub ? 'r/' + sub[1] : '', score, comments, time,
                                url: href.startsWith('/') ? 'https://www.reddit.com' + href : href });
                    if (rows.length >= limit) break;
                  }
                  return rows;
                }""",
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
