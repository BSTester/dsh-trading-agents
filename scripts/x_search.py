#!/usr/bin/env python3
"""X (Twitter) 搜索抓取 —— 复用系统已登录浏览器的会话（CDP 方式，不解密 cookie）

原理：以远程调试模式启动本机 Chrome/Edge（沿用你已登录 x.com 的用户配置），
通过 CDP 连接后打开 x.com 搜索页，提取推文文本。cookie 留在浏览器内，脚本不接触明文。

前置条件：
  1. 本机装有 Chrome 或 Edge，且已在其中登录 x.com；
  2. pip install playwright（只需库，无需 playwright install 下载浏览器）；
  3. 运行前请关闭所有 Chrome/Edge 窗口（调试端口需占用默认用户配置目录）。

用法：
  python scripts/x_search.py "贵州茅台" [--count 10] [--live]
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

DEBUG_PORT = 9222
CHROME_CANDIDATES = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
    "linux": [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ],
}


def find_browser():
    for p in CHROME_CANDIDATES.get(sys.platform, []):
        if os.path.exists(p):
            return p
    for name in ("google-chrome", "chromium", "msedge"):
        path = os.popen(f"command -v {name}").read().strip()
        if path:
            return path
    return None


def debug_port_alive():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_browser():
    if debug_port_alive():
        return True
    exe = find_browser()
    if not exe:
        print(json.dumps({"error": "未找到 Chrome/Edge，请安装或手动以 --remote-debugging-port=9222 启动"}))
        return False
    subprocess.Popen(
        [exe, f"--remote-debugging-port={DEBUG_PORT}", "--restore-last-session=false"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        time.sleep(0.5)
        if debug_port_alive():
            return True
    print(json.dumps({"error": "浏览器调试端口启动失败（请先关闭所有 Chrome/Edge 窗口后重试）"}))
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--live", action="store_true", help="按最新排序（默认热门）")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"error": "缺少 playwright 库", "fix": "pip install playwright"}))
        return 1

    if not ensure_browser():
        return 1

    url = f"https://x.com/search?q={urllib.parse.quote(args.query)}" + ("&f=live" if args.live else "")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
            context = browser.contexts[0]
            page = context.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
            page.wait_for_selector("article", timeout=20000)
            time.sleep(2)  # 等懒加载
            items = page.evaluate(
                """(n) => Array.from(document.querySelectorAll('article')).slice(0, n).map(a => {
                     const t = a.querySelector('time');
                     const spans = Array.from(a.querySelectorAll('span'));
                     const text = spans.map(s => s.innerText).join(' ').replace(/\\s+/g, ' ').trim();
                     return { time: t ? t.getAttribute('datetime') : null, text: text.slice(0, 500) };
                   })""",
                args.count,
            )
            page.close()
            print(json.dumps({"query": args.query, "count": len(items), "items": items},
                             ensure_ascii=False, indent=1))
            return 0
    except Exception as e:
        print(json.dumps({"error": str(e)[:300],
                          "hint": "确认已在默认浏览器登录 x.com，且运行前关闭了所有 Chrome/Edge 窗口"}))
        return 1


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
