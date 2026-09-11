#!/usr/bin/env python3
"""X (Twitter) 搜索抓取 —— 专属持久浏览器配置（登录一次，永久复用）

原理：使用专属配置目录 ~/.dsh/x-profile 启动 Chrome/Edge（与日常浏览器互不干扰，
无需关闭正在使用的浏览器窗口）。首次用 --login 在弹出的浏览器里登录 x.com，
登录态持久保存在该配置目录，之后的搜索直接复用，cookie 不落明文、不进脚本。

前置条件：
  1. 本机装有 Chrome 或 Edge；pip install playwright（只需库，无需下载浏览器）。

用法：
  python scripts/x_search.py --login              # 首次：弹浏览器登录 x.com，完成后回车保存
  python scripts/x_search.py "贵州茅台" --count 10 --live
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

DEBUG_PORT = 9222
# 专属持久配置目录：登录态保存在这里，与日常浏览器互不干扰
PROFILE_DIR = os.path.join(os.environ.get("DSH_HOME", os.path.expanduser("~/.dsh")), "x-profile")
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
    # 专属配置目录：不需要关闭日常浏览器窗口
    os.makedirs(PROFILE_DIR, exist_ok=True)
    subprocess.Popen(
        [exe, f"--remote-debugging-port={DEBUG_PORT}",
         f"--user-data-dir={PROFILE_DIR}", "--restore-last-session=false",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        time.sleep(0.5)
        if debug_port_alive():
            return True
    print(json.dumps({"error": "浏览器调试端口启动失败（检查端口 9222 是否被占用）"}))
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default=None)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--live", action="store_true", help="按最新排序（默认热门）")
    ap.add_argument("--login", action="store_true", help="首次登录：打开 x.com 让用户登录，登录态持久保存")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(json.dumps({"error": "缺少 playwright 库", "fix": "pip install playwright"}))
        return 1

    if not ensure_browser():
        return 1

    url = "https://x.com/login" if args.login else (
        f"https://x.com/search?q={urllib.parse.quote(args.query)}" + ("&f=live" if args.live else ""))

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
            context = browser.contexts[0]
            page = context.new_page()
            page.goto(url, timeout=45000, wait_until="domcontentloaded")

            if args.login:
                print(json.dumps({"status": "请在弹出的浏览器窗口中登录 x.com",
                                  "profile": PROFILE_DIR}, ensure_ascii=False))
                page.wait_for_url("**/home**", timeout=300000)  # 登录成功后跳到 /home
                print(json.dumps({"status": "登录成功，登录态已保存到专属配置，下次直接搜索即可"},
                                 ensure_ascii=False))
                page.close()
                return 0

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
                          "hint": "若提示登录/无内容：先运行 python scripts/x_search.py --login 在专属浏览器窗口登录 x.com（一次即可，登录态持久保存）"}))
        return 1


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
