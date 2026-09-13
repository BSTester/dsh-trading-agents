#!/usr/bin/env python3
"""统一情绪/舆情获取：X + Reddit（经已登录专属浏览器，CDP）+ AKShare 千股千评（A股）。

用法:
  python fin_sentiment.py --ticker 600519 [--x-query "Kweichow Moutai"] [--count 8]
输出: JSON {ticker, x:[...], a_share_comment:{...}, sources_status:{...}}
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# A 股判定只有一份实现：显式市场标注优先（000001.HK 是港股，不是平安银行）
from trading_datasource.market import is_a_share  # noqa: E402


def release_browser():
    """释放子脚本留存的浏览器（避免窗口/进程堆积）。"""
    here = Path(__file__).resolve().parent
    cleanup = (
        "import sys; sys.path.insert(0, r'%s');"
        "from x_search import close_browser_if_we_launched_it, close_by_port_best_effort;"
        "close_browser_if_we_launched_it(); close_by_port_best_effort()" % here
    )
    venv_py = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "trading-venv"
    py = venv_py / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    exe = [str(py)] if py.exists() else [sys.executable]
    try:
        subprocess.run(exe + ["-c", cleanup], capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def x_sentiment(query, count):
    """调用同包 x_search.py（复用其可达性预检/自动关浏览器逻辑）。"""
    here = Path(__file__).resolve().parent
    script = here / "x_search.py"
    if not script.exists():
        raise RuntimeError("x_search.py missing")
    venv_py = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "trading-venv"
    py = venv_py / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    exe = [str(py)] if py.exists() else [sys.executable]
    env = {**os.environ, "SOCIAL_BROWSER_PERSIST": "1"}  # 复用浏览器，最后由本进程统一关闭
    out = subprocess.run(exe + [str(script), query, "--live", "--count", str(count)],
                         capture_output=True, text=True, timeout=180, env=env)
    stdout = out.stdout
    data = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
    if data.get("skip"):
        return {"skipped": True, "reason": data.get("reason")}
    if data.get("error"):
        raise RuntimeError(data["error"][:80])
    return {"skipped": False, "path": data.get("path"), "items": data.get("items", [])}


def reddit_sentiment(query, count):
    """Reddit 讨论（与 X 共用专属浏览器登录态；未登录/不可达时明确跳过）。"""
    here = Path(__file__).resolve().parent
    script = here / "reddit_search.py"
    if not script.exists():
        raise RuntimeError("reddit_search.py missing")
    venv_py = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "trading-venv"
    py = venv_py / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    exe = [str(py)] if py.exists() else [sys.executable]
    env = {**os.environ, "SOCIAL_BROWSER_PERSIST": "1"}  # 与 X 复用同一浏览器
    out = subprocess.run(exe + [str(script), query, "--count", str(count)],
                         capture_output=True, text=True, timeout=180, env=env)
    stdout = out.stdout
    data = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
    if data.get("skip"):
        return {"skipped": True, "reason": data.get("reason")}
    if data.get("error"):
        raise RuntimeError(str(data.get("error"))[:90])
    return {"skipped": False, "path": data.get("path"), "items": data.get("items", [])}


def a_share_comment(ticker):
    import akshare as ak
    df = ak.stock_comment_em()
    row = df[df["代码"].astype(str) == ticker.split(".")[0]
             if "代码" in df.columns else df.iloc[:, 0].astype(str) == ticker.split(".")[0]]
    if row.empty:
        raise RuntimeError("no comment row")
    r = row.tail(1).to_dict("records")[0]
    keep = ("综合得分", "关注指数", "机构参与度", "上升", "目前排名", "主力成本", "换手率")
    return {k: str(v) for k, v in r.items() if k in keep}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--x-query", default=None, help="X 搜索词（英文覆盖更广）；默认用 ticker")
    ap.add_argument("--reddit-query", default=None, help="Reddit 搜索词；默认用 ticker")
    ap.add_argument("--count", type=int, default=8)
    args = ap.parse_args()

    result = {"ticker": args.ticker, "sources_status": {}}

    try:
        result["x"] = x_sentiment(args.x_query or f"${args.ticker.split('.')[0]}", args.count)
        result["sources_status"]["x"] = ("skipped" if result["x"].get("skipped")
                                         else "ok:" + str(result["x"].get("path")))
    except Exception as e:
        result["x"] = None
        result["sources_status"]["x"] = f"fail: {str(e)[:80]}"

    try:
        result["reddit"] = reddit_sentiment(args.reddit_query or args.ticker, args.count)
        result["sources_status"]["reddit"] = ("skipped" if result["reddit"].get("skipped")
                                              else "ok:" + str(result["reddit"].get("path")))
    except Exception as e:
        result["reddit"] = None
        result["sources_status"]["reddit"] = f"fail: {str(e)[:80]}"

    if is_a_share(args.ticker):
        try:
            result["a_share_comment"] = a_share_comment(args.ticker)
            result["sources_status"]["akshare"] = "ok"
        except Exception as e:
            result["sources_status"]["akshare"] = f"fail: {str(e)[:80]}"

    release_browser()
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
