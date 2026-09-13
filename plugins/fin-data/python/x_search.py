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
from pathlib import Path

import urllib.parse
import urllib.error

DSH_HOME = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh"))
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


def clear_stale_profile_locks():
    """清理被强杀实例残留的 Singleton* 锁。

    Chromium 被 SIGKILL 后会留下 SingletonLock，新实例据此判定 profile 仍被占用
    而直接退出 —— 表现为“调试端口启动失败”。这里只在锁指向的进程已死时清理。
    """
    import re
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = os.path.join(PROFILE_DIR, name)
        try:
            if os.path.islink(path):
                match = re.search(r"-(\d+)$", os.readlink(path))
                pid = int(match.group(1)) if match else None
                if pid is not None:
                    try:
                        os.kill(pid, 0)
                        continue  # 持有者仍活着，不动
                    except OSError:
                        pass
                os.unlink(path)
            elif os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass


def terminate_pid(pid):
    """先 SIGTERM 优雅退出（会释放锁），超时再 SIGKILL，避免留下陈旧锁。"""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(6):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# ── 标签页复用（只操作带我方标记的标签，绝不触碰用户自己的标签）────────────────
SCRATCH_MARK = "dsh_scratch=1"
SCRATCH_BASE = "https://x.com/?dsh_scratch=1"


def browser_persist_requested():
    """父进程（fin_sentiment）要求保留浏览器以复用；任务结束后由父进程统一关闭。"""
    return os.environ.get("SOCIAL_BROWSER_PERSIST") == "1"


def terminate_browser_for_retry():
    """关闭**自有**浏览器实例并清理锁（不触碰用户自己的浏览器）。"""
    close_browser_if_we_launched_it()
    close_by_port_best_effort()
    clear_stale_profile_locks()
    time.sleep(1)


def connect_browser(pw, timeout_ms=20000, retries=3):
    """连接 CDP：短超时 + 有界重试。

    关键：连接失败但端口仍活着时**只等待重试，绝不杀浏览器**——早期实现在这里误杀
    正在初始化的浏览器，导致第二次连接必然 ECONNREFUSED。只有端口确认已死才重建。
    """
    last = None
    for _ in range(retries + 1):
        try:
            return pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}", timeout=timeout_ms)
        except Exception as error:  # noqa: BLE001
            last = error
            if debug_port_alive():
                time.sleep(2)
                continue
            terminate_browser_for_retry()
            if not ensure_browser():
                break
    raise RuntimeError(f"CDP 连接失败：{str(last)[:120]}")


def acquire_scratch_page(context, base_url):
    """复用已有标记标签，没有才新建。"""
    for page in list(context.pages):
        try:
            if SCRATCH_MARK in (page.url or ""):
                page.goto(base_url, timeout=45000, wait_until="domcontentloaded")
                return page
        except Exception:
            continue
    page = context.new_page()
    page.goto(base_url, timeout=45000, wait_until="domcontentloaded")
    return page


def close_scratch_pages(context):
    """关闭我方标记标签，及时释放资源（不动用户标签）。"""
    for page in list(context.pages):
        try:
            if SCRATCH_MARK in (page.url or ""):
                page.close()
        except Exception:
            pass



def ensure_browser():
    if debug_port_alive():
        return True
    exe = find_browser()
    if not exe:
        print(json.dumps({"error": "未找到 Chrome/Edge，请安装或手动以 --remote-debugging-port=9222 启动"}))
        return False
    # 专属配置目录：不需要关闭日常浏览器窗口
    os.makedirs(PROFILE_DIR, exist_ok=True)
    clear_stale_profile_locks()
    proc = subprocess.Popen(
        [exe, f"--remote-debugging-port={DEBUG_PORT}",
         f"--user-data-dir={PROFILE_DIR}", "--restore-last-session=false",
         # 容器/受限环境必需：否则 Chromium 会启动但 CDP 无响应（表现为连接超时）
         "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # 记录 PID：搜索完成后自动关闭该浏览器
    (DSH_HOME / "x-chrome.pid").write_text(str(proc.pid))
    # 冷启动（VNC/GPU 初始化）可能需要 10~30 秒，耐心轮询，避免误判为占用
    for _ in range(60):
        time.sleep(1)
        if debug_port_alive():
            return True
    print(json.dumps({"error": "浏览器调试端口启动失败（检查端口 9222 是否被占用或浏览器是否可用）"}))
    return False


def close_browser_if_we_launched_it():
    pid_file = DSH_HOME / "x-chrome.pid"
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        else:
            terminate_pid(pid)
        pid_file.unlink()
        clear_stale_profile_locks()
    except (ValueError, ProcessLookupError, OSError):
        pass


def close_by_port_best_effort(force=False):
    """关闭调试端口上的浏览器。

    默认**只关闭由本脚本启动的实例**（依据 pid 文件），不误杀用户自己的浏览器——
    早期实现按端口无条件清理，导致每次查询都冷启动、反复开窗。
    force=True（CLI --force-clean）时按端口强制清理。
    """
    import signal
    pid_file = DSH_HOME / "x-chrome.pid"
    pids = set()
    if pid_file.exists():
        try:
            pids.add(int(pid_file.read_text().strip()))
        except (ValueError, OSError):
            pass
    if force:
        try:
            if sys.platform == "win32":
                out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
                pids |= {line.split()[-1] for line in out.splitlines()
                         if f":{DEBUG_PORT}" in line and "LISTENING" in line.upper()}
            else:
                out = subprocess.run(["fuser", f"{DEBUG_PORT}/tcp"], capture_output=True, text=True).stdout
                pids |= {p for p in out.split() if p.isdigit()}
        except Exception:
            pass
    for value in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(value), "/T", "/F"], capture_output=True)
            else:
                os.kill(int(value), signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass
    if pids:
        time.sleep(1.5)
        clear_stale_profile_locks()
    pid_file.unlink(missing_ok=True)


def x_reachable(timeout=3):
    """快速预检 x.com 连通性：仅 TCP 连 443 端口，3秒超时，不开浏览器不加载页面。"""
    import socket
    try:
        with socket.create_connection(("x.com", 443), timeout=timeout):
            return True
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default=None)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--live", action="store_true", help="按最新排序（默认热门）")
    ap.add_argument("--login", action="store_true", help="首次登录：打开 x.com 让用户登录，登录态持久保存")
    ap.add_argument("--keep-browser", action="store_true", help="完成后保持浏览器打开（默认自动关闭）")
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

    if not x_reachable():
        print(json.dumps({"skip": True, "reason": "x.com 不可达，跳过 X 渠道"}))
        return 0

    # ---- API 优先：走社区 XClientTransaction 生成反爬头，直接调 GraphQL ----
    # 失败（未登录/风控/接口变更）自动降级到下面的 DOM 抓取，两条路径结果结构一致。
    api_items = None
    if not args.login:
        try:
            import x_api
            api_items = x_api.search(args.query, args.count, live=args.live)
        except Exception as e:
            sys.stderr.write(f"[x_search] API 路径异常，降级 DOM：{str(e)[:160]}\n")
            api_items = None
        if api_items:
            close_browser_if_we_launched_it()
            close_by_port_best_effort()
            print(json.dumps({"query": args.query, "count": len(api_items),
                              "path": "api/graphql", "items": api_items},
                             ensure_ascii=False, indent=1))
            return 0

    try:
        if not ensure_browser():
            return 1

        url = "https://x.com/login" if args.login else (
            f"https://x.com/search?q={urllib.parse.quote(args.query)}"
            + ("&f=live" if args.live else ""))

        with sync_playwright() as pw:
            browser = connect_browser(pw)
            context = browser.contexts[0]
            page = acquire_scratch_page(context, SCRATCH_BASE)
            page.goto(url, timeout=45000, wait_until="domcontentloaded")

            if args.login:
                print(json.dumps({"status": "请在弹出的浏览器窗口中登录 x.com",
                                  "profile": PROFILE_DIR}, ensure_ascii=False))
                page.wait_for_url("**/home**", timeout=300000)
                print(json.dumps({"status": "登录成功，登录态已保存到专属配置，下次直接搜索即可"},
                                 ensure_ascii=False))
                close_scratch_pages(context)
                return 0

            page.wait_for_selector("article", timeout=30000, state="attached")
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
            close_scratch_pages(context)
            print(json.dumps({"query": args.query, "count": len(items),
                              "path": "web/dom", "items": items},
                             ensure_ascii=False, indent=1))
            return 0
    except Exception as e:
        print(json.dumps({"error": str(e)[:300],
                          "hint": "若提示登录/无内容：先运行 python scripts/x_search.py --login 在专属浏览器窗口登录 x.com（一次即可，登录态持久保存）"}))
        return 1
    finally:
        # 完成后自动关闭专用浏览器（--keep-browser 可保留）
        if not args.keep_browser:
            close_browser_if_we_launched_it()
            close_by_port_best_effort()


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
