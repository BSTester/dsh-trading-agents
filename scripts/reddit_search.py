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

from x_search import (DEBUG_PORT, PROFILE_DIR, acquire_scratch_page, browser_persist_requested,  # noqa: E402
                      close_browser_if_we_launched_it, close_by_port_best_effort,
                      close_scratch_pages, connect_browser, debug_port_alive, ensure_browser)

SCRATCH_BASE = "https://www.reddit.com/?dsh_scratch=1"

LOGIN_URL = "https://www.reddit.com/login"
# 注意：old.reddit.com 要求单独登录（与 www 不共享会话）；www 的搜索结果属性可直接读取。
SEARCH_URL = "https://www.reddit.com/search/"



def try_api_search(page, query, count, sort, subreddit=None):
    """Reddit 同源 JSON API（复用登录态；结构化且比 DOM 快）。失败返回 None 以触发降级。"""
    try:
        if "reddit.com" not in page.url:
            page.goto("https://www.reddit.com/", timeout=45000, wait_until="domcontentloaded")
            time.sleep(1.0)
        result = page.evaluate(
            """async (args) => {
              const q = args.subreddit ? `subreddit:${args.subreddit} ${args.query}` : args.query;
              const params = new URLSearchParams({ q, limit: String(args.limit), sort: args.sort,
                                                   type: 'link', raw_json: '1' });
              const response = await fetch('/search.json?' + params.toString(),
                { credentials: 'include', headers: { Accept: 'application/json' } });
              if (!response.ok) return { error: 'HTTP ' + response.status };
              const data = await response.json();
              const children = (data && data.data && data.data.children) || [];
              return { items: children.map((child) => {
                const d = child.data || {};
                return { title: d.title || '', subreddit: d.subreddit_name_prefixed || '',
                         score: String(d.score ?? ''), comments: String(d.num_comments ?? ''),
                         time: d.created_utc ? new Date(d.created_utc * 1000).toISOString() : '',
                         url: d.permalink ? 'https://www.reddit.com' + d.permalink : (d.url || '') };
              }) };
            }""",
            {"query": query, "limit": max(count * 2, 25), "sort": sort, "subreddit": subreddit},
        )
        if not isinstance(result, dict) or result.get("error"):
            return None
        items = [row for row in (result.get("items") or []) if row.get("title")][:count]
        return items or None
    except Exception:
        return None


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
    ap.add_argument("--force-clean", action="store_true", help="强制清理占用调试端口的浏览器")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"error": "缺少 playwright 库", "fix": "pip install playwright"}))
        return 1

    if getattr(args, "force_clean", False):
        close_by_port_best_effort(force=True)
        print(json.dumps({"status": "已强制清理调试端口上的浏览器"}, ensure_ascii=False))
        return 0

    if not args.login and not reddit_reachable():
        print(json.dumps({"skip": True, "reason": "reddit.com 不可达，跳过 Reddit 渠道"}))
        return 0

    try:
        if not ensure_browser():
            return 1

        with sync_playwright() as pw:
            browser = connect_browser(pw)
            context = browser.contexts[0]
            page = acquire_scratch_page(context, SCRATCH_BASE)

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
                close_scratch_pages(context)
                return 0

            if not args.query:
                print(json.dumps({"error": "缺少搜索词（或用 --login 登录）"}))
                return 1

            # ① API 路径（同源 JSON，最快；走登录态）
            items = try_api_search(page, args.query, args.count, args.sort, args.subreddit)
            api_used = items is not None

            # ② 降级：DOM 抓取
            if items is None:
                params = {"q": args.query, "sort": args.sort, "type": "posts"}
                if args.subreddit:
                    params["q"] = f"subreddit:{args.subreddit} {args.query}"
                url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
                time.sleep(4)
                if "/login" in page.url:
                    print(json.dumps({"error": "Reddit 需要登录",
                                      "fix": "python plugins/fin-data/python/reddit_search.py --login"},
                                     ensure_ascii=False))
                    close_scratch_pages(context)
                    return 0
                try:
                    page.wait_for_selector('a[href*="/comments/"]', timeout=25000, state="attached")
                except Exception:
                    print(json.dumps({"error": "搜索页未返回结果（可能未登录或被限流）",
                                      "fix": "python plugins/fin-data/python/reddit_search.py --login"}))
                    close_scratch_pages(context)
                    return 0
                page.mouse.wheel(0, 1200)
                time.sleep(2)
                items = page.evaluate(
                    """(limit) => {
                      const seenUrl = new Set();
                      const rows = [];
                      for (const link of document.querySelectorAll('a[href*="/comments/"]')) {
                        const href = link.getAttribute('href') || '';
                        const title = (link.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (!title || title.length < 8 || !href || seenUrl.has(href)) continue;
                        seenUrl.add(href);
                        let node = link, container = null;
                        for (let i = 0; i < 4 && node; i += 1) {
                          node = node.parentElement;
                          if (!node) break;
                          const text = node.innerText || '';
                          if (/comment/i.test(text) && text.length < 1500) { container = node; break; }
                        }
                        let score = '', comments = '';
                        if (container) {
                          const nums = Array.from(container.querySelectorAll('faceplate-number'))
                            .map((el) => el.getAttribute('number') || el.innerText || '')
                            .filter((v) => v && /[0-9]/.test(v));
                          if (nums.length >= 2) { score = nums[0]; comments = nums[1]; }
                          else if (nums.length === 1) { comments = nums[0]; }
                          const text = container.innerText || '';
                          if (!score) score = (text.match(/([\\d.,]+[KM]?)\\s*(upvote|vote)/i) || [])[1] || '';
                          if (!comments) comments = (text.match(/([\\d.,]+[KM]?)\\s*comment/i) || [])[1] || '';
                        }
                        const sub = href.match(/\\/r\\/([^\\/]+)\\//);
                        rows.push({ title, subreddit: sub ? 'r/' + sub[1] : '', score, comments, time: '',
                                    url: href.startsWith('/') ? 'https://www.reddit.com' + href : href });
                        if (rows.length >= limit) break;
                      }
                      return rows;
                    }""",
                    args.count,
                )
            close_scratch_pages(context)
            base = "https://www.reddit.com"
            for item in items:
                if item.get("url", "").startswith("/"):
                    item["url"] = base + item["url"]
            print(json.dumps({"query": args.query, "sort": args.sort, "count": len(items), "items": items,
                              "path": "api/json" if api_used else "web/dom"},
                             ensure_ascii=False, indent=1))
            return 0
    except Exception as error:
        print(json.dumps({"error": str(error)[:300],
                          "hint": "若提示登录/无内容：先运行 python reddit_search.py --login 登录一次"}))
        return 1
    finally:
        if not args.keep_browser and not browser_persist_requested():
            close_browser_if_we_launched_it()
            close_by_port_best_effort()


if __name__ == "__main__":
    sys.exit(main())
