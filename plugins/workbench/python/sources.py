#!/usr/bin/env python3
"""数据源与授权状态自检：富途 token、X 登录态、AKShare、行情缓存、工作台配置。

用法:
  python sources.py [--probe-futu]
输出: JSON {checked_at, sources:[{key,label,status,detail,fix}], summary:{ok,warn,fail}}
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DSH = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
VENV = DSH / "trading-venv"
SERIES = DSH / "trading-series"


def age_text(path):
    try:
        seconds = max(0, time.time() - path.stat().st_mtime)
    except OSError:
        return None
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def probe_futu_token():
    """真正调一次 MCP initialize，判断 token 是否仍然有效。"""
    token_path = DSH / "futu-token"
    if not token_path.exists() or not token_path.read_text().strip():
        return False, "未授权（无 token 文件）"
    token = token_path.read_text().strip()
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "workbench-sources", "version": "1"}},
    }).encode()
    request = urllib.request.Request("https://mcp.futunn.com/mcp", data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    })
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = response.read().decode()
        if '"result"' in payload:
            return True, "token 有效"
        if "invalid_token" in payload or "401" in payload:
            return False, "token 已过期或被吊销"
        return False, payload[:120]
    except Exception as error:
        return None, f"探测失败：{str(error)[:100]}"  # None = 未知（网络问题），不误报过期


def session_cookies(profile):
    """只读检查 cookie 库里的 (域, 名称) 对 —— 只读名称，不解密任何值。

    用「会话 cookie 名称」判断登录态，比只看域可靠（访问登录墙也会种匿名 cookie）。
    """
    import shutil
    import sqlite3
    import tempfile
    source = Path(profile) / "Default" / "Cookies"
    if not source.exists():
        return set()
    pairs = set()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "Cookies"
            shutil.copy2(source, copy)  # 运行中的浏览器会锁库，先复制再读
            connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            try:
                for host, name in connection.execute("SELECT DISTINCT host_key, name FROM cookies"):
                    pairs.add((str(host).lower(), str(name)))
            finally:
                connection.close()
    except Exception:
        return set()
    return pairs


def check(probe=True):
    sources = []

    # 1) 富途授权
    token_path = DSH / "futu-token"
    refresh_path = DSH / "futu-refresh"
    if probe:
        valid, detail = probe_futu_token()
    else:
        valid, detail = (token_path.exists(), "未探测")
    if valid is True:
        status = "ok"
    elif valid is False:
        status = "fail"
    else:
        status = "warn"
    sources.append({
        "key": "futu", "label": "富途远程 MCP（行情/账户/交易）", "status": status,
        "detail": f"{detail} · token 更新于 {age_text(token_path) or '未知'}"
                  + (f" · refresh_token {'有' if refresh_path.exists() else '无'}" if token_path.exists() else ""),
        "fix": "已过期时执行：python scripts/futu_auth.py --refresh（免交互续期）；"
               "refresh 也失效才需重新完整授权：python scripts/futu_auth.py",
    })

    # 2) 社交渠道登录态（X 必取；Reddit 同配置）
    profile = DSH / "x-profile"
    cookies = session_cookies(profile)
    x_names = {"auth_token", "ct0", "twid", "kdt"}
    # 仅 reddit_session 代表已登录；token_v2/session_tracker 匿名访问也会被种下
    reddit_names = {"reddit_session"}
    has_x = any(("x.com" in host or "twitter" in host) and name in x_names for host, name in cookies)
    has_reddit = any("reddit" in host and name in reddit_names for host, name in cookies)
    sources.append({"key": "x", "label": "X / Twitter（社交舆情，必取渠道）",
                    "status": "ok" if has_x else "warn",
                    "detail": ("已登录（专属浏览器）" if has_x else "未登录：需登录一次，登录态持久保存")
                              + (f" · 识别到会话 cookie" if has_x else " · 未发现 X 会话 cookie"),
                    "fix": "python plugins/fin-data/python/x_search.py --login"})
    sources.append({"key": "reddit", "label": "Reddit（社交舆情）",
                    "status": "ok" if has_reddit else "warn",
                    "detail": "已登录（与 X 共用专属浏览器）" if has_reddit
                              else "未登录：old.reddit.com 强制要求账号，需登录一次"
                                   + ("（已存在匿名 cookie）" if any("reddit" in h for h, _ in cookies) else ""),
                    "fix": "python plugins/fin-data/python/reddit_search.py --login"})

    # 3) Python 数据环境
    python_bin = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if python_bin.exists():
        sources.append({"key": "venv", "label": "数据环境（akshare / playwright）", "status": "ok",
                        "detail": f"{VENV}", "fix": "重装：bash install.sh"})
    else:
        sources.append({"key": "venv", "label": "数据环境（akshare / playwright）", "status": "fail",
                        "detail": f"缺少虚拟环境 {VENV}", "fix": "bash install.sh"})

    # 4) 行情缓存新鲜度
    if SERIES.exists():
        files = sorted(SERIES.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            newest = files[0]
            hours = (time.time() - newest.stat().st_mtime) / 3600
            sources.append({"key": "cache", "label": "行情序列缓存", "status": "ok" if hours < 72 else "warn",
                            "detail": f"{len(files)} 个序列 · 最新 {newest.name}（{age_text(newest)}）",
                            "fix": "缓存过期不影响取数（会自动刷新）"})
        else:
            sources.append({"key": "cache", "label": "行情序列缓存", "status": "warn",
                            "detail": "暂无缓存（首次打开行情页会创建）", "fix": "-"})
    else:
        sources.append({"key": "cache", "label": "行情序列缓存", "status": "warn",
                        "detail": "缓存目录尚未创建", "fix": "-"})

    # 5) 账户模式与风控
    mode_path = DSH / "trading-account-mode"
    mode = mode_path.read_text().strip() if mode_path.exists() else "sim"
    risk_path = DSH / "trading-risk.json"
    sources.append({"key": "risk", "label": "账户模式与风控参数", "status": "ok",
                    "detail": f"模式={mode} · 风控配置={'自定义' if risk_path.exists() else '默认值'}",
                    "fix": "切换模式请在 Harness 会话中确认；风控参数见 scripts/risk_config.py"})

    summary = {"ok": sum(1 for s in sources if s["status"] == "ok"),
               "warn": sum(1 for s in sources if s["status"] == "warn"),
               "fail": sum(1 for s in sources if s["status"] == "fail")}
    return {"checked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "sources": sources, "summary": summary}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-probe", action="store_true", help="跳过富途 token 网络探测（离线场景）")
    args = ap.parse_args()
    print(json.dumps(check(probe=not args.no_probe), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
