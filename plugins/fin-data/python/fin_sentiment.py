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


def is_a_share(ticker):
    return bool(re.fullmatch(r"\d{6}", ticker.split(".")[0]))


def x_sentiment(query, count):
    """调用同包 x_search.py（复用其可达性预检/自动关浏览器逻辑）。"""
    here = Path(__file__).resolve().parent
    script = here / "x_search.py"
    if not script.exists():
        raise RuntimeError("x_search.py missing")
    venv_py = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "trading-venv"
    py = venv_py / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    exe = [str(py)] if py.exists() else [sys.executable]
    out = subprocess.run(exe + [str(script), query, "--live", "--count", str(count)],
                         capture_output=True, text=True, timeout=180)
    stdout = out.stdout
    data = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
    if data.get("skip"):
        return {"skipped": True, "reason": data.get("reason")}
    if data.get("error"):
        raise RuntimeError(data["error"][:80])
    return {"skipped": False, "items": data.get("items", [])}


def reddit_sentiment(query, count):
    """Reddit 讨论（与 X 共用专属浏览器登录态；未登录/不可达时明确跳过）。"""
    here = Path(__file__).resolve().parent
    script = here / "reddit_search.py"
    if not script.exists():
        raise RuntimeError("reddit_search.py missing")
    venv_py = Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "trading-venv"
    py = venv_py / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    exe = [str(py)] if py.exists() else [sys.executable]
    out = subprocess.run(exe + [str(script), query, "--count", str(count)],
                         capture_output=True, text=True, timeout=180)
    stdout = out.stdout
    data = json.loads(stdout[stdout.index("{"):]) if "{" in stdout else {}
    if data.get("skip"):
        return {"skipped": True, "reason": data.get("reason")}
    if data.get("error"):
        raise RuntimeError(str(data.get("error"))[:90])
    return {"skipped": False, "items": data.get("items", [])}


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
        result["sources_status"]["x"] = "skipped" if result["x"].get("skipped") else "ok"
    except Exception as e:
        result["x"] = None
        result["sources_status"]["x"] = f"fail: {str(e)[:80]}"

    try:
        result["reddit"] = reddit_sentiment(args.reddit_query or args.ticker, args.count)
        result["sources_status"]["reddit"] = "skipped" if result["reddit"].get("skipped") else "ok"
    except Exception as e:
        result["reddit"] = None
        result["sources_status"]["reddit"] = f"fail: {str(e)[:80]}"

    if is_a_share(args.ticker):
        try:
            result["a_share_comment"] = a_share_comment(args.ticker)
            result["sources_status"]["akshare"] = "ok"
        except Exception as e:
            result["sources_status"]["akshare"] = f"fail: {str(e)[:80]}"

    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
