#!/usr/bin/env python3
"""X (Twitter) 搜索的 API 路径 —— 基于社区实现 XClientTransaction。

思路：
  1. 通过 CDP 从已登录的专属浏览器取出 cookie（浏览器负责解密，我们不碰密文）；
     cookie 缓存到 ~/.dsh/x-cookies.json，避免每次查询都开浏览器；
  2. 拉取 x.com 首页 + ondemand 脚本，用 x_client_transaction.ClientTransaction
     生成反爬头 x-client-transaction-id（这是 GraphQL 之前 404 的原因）；
  3. 直接以 HTTP 调 SearchTimeline GraphQL，返回结构化推文。
  任一环节失败返回 None，由调用方降级到浏览器 DOM 抓取。

实测：HTTP 200 / 约 1.1s / 19 条推文（DOM 路径约 40-50s）。
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

DSH = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh").expanduser()
COOKIE_FILE = DSH / "x-cookies.json"
MATERIAL_FILE = DSH / "x-client-material.json"
COOKIE_TTL_SECONDS = 3 * 24 * 3600      # cookie 有效期内复用（auth_token 通常可存续数月）
MATERIAL_TTL_SECONDS = 30 * 60          # 首页/ondemand 变化不频繁，缓存 30 分钟

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/152.0.0.0 Safari/537.36")
BEARER = ("AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
          "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA")
GRAPHQL_FEATURES = {"responsive_web_graphql_timeline_navigation_enabled": True}


def _read_json(path, ttl):
    try:
        data = json.loads(path.read_text())
        if time.time() - float(data.get("_at", 0)) < ttl:
            return data
    except (OSError, ValueError, TypeError):
        pass
    return None


def _write_json(path, payload):
    payload = {**payload, "_at": time.time()}
    DSH.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False))
    temp.replace(path)
    try:
        path.chmod(0o600)  # 含会话 cookie，仅本人可读
    except OSError:
        pass


def extract_cookies_via_browser(domains=("x.com", "twitter"), required=("auth_token", "ct0")):
    """用 CDP 从专属浏览器取 cookie（浏览器已完成解密，我们不碰密文）。

    domains: 域名关键字白名单；required: 必须齐备的 cookie 名，缺失即判定未登录。
    """
    from x_search import (SCRATCH_BASE, acquire_scratch_page,  # 延迟导入避免循环
                          close_scratch_pages, connect_browser, ensure_browser)
    from playwright.sync_api import sync_playwright

    if not ensure_browser():
        return None
    cookies = {}
    with sync_playwright() as pw:
        browser = connect_browser(pw)
        context = browser.contexts[0]
        page = acquire_scratch_page(context, SCRATCH_BASE)
        try:
            session = context.new_cdp_session(page)
            for cookie in session.send("Network.getAllCookies").get("cookies", []):
                if any(d in cookie.get("domain", "") for d in domains):
                    cookies[cookie["name"]] = cookie["value"]
        finally:
            close_scratch_pages(context)
    if any(name not in cookies for name in required):
        return None
    return cookies


def get_cookies(force=False):
    if not force:
        cached = _read_json(COOKIE_FILE, COOKIE_TTL_SECONDS)
        if cached and cached.get("cookies", {}).get("auth_token"):
            return cached["cookies"]
    cookies = extract_cookies_via_browser()
    if cookies:
        _write_json(COOKIE_FILE, {"cookies": cookies})
    return cookies


def _http_get(url, cookies, extra_headers=None, timeout=30):
    request = urllib.request.Request(url, headers={
        "user-agent": USER_AGENT,
        "accept-language": "en-US,en;q=0.9",
        "cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        **(extra_headers or {}),
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", "replace")


def _client_material(cookies):
    """首页 HTML + ondemand 脚本 + SearchTimeline queryId，带缓存。

    queryId 不在首页 HTML 里，而在 client-web 的 main 包中——必须单独抓取 main.js 提取。
    """
    cached = _read_json(MATERIAL_FILE, MATERIAL_TTL_SECONDS)
    if cached and cached.get("home") and cached.get("ondemand") and cached.get("query_id"):
        return cached["home"], cached["ondemand"], cached["query_id"]

    from x_client_transaction.utils import get_ondemand_file_url
    from bs4 import BeautifulSoup

    _, home_html = _http_get("https://x.com/home", cookies)
    soup = BeautifulSoup(home_html, "html.parser")
    ondemand_url = get_ondemand_file_url(soup)
    if not ondemand_url:
        return None, None, None
    _, ondemand_js = _http_get(str(ondemand_url), cookies)

    query_id = None
    for match in re.finditer(r'src="(https://abs\.twimg\.com/responsive-web/client-web/main\.[^"]+\.js)"', home_html):
        try:
            _, main_js = _http_get(match.group(1), cookies)
        except Exception:
            continue
        found = re.search(r'queryId:"([^"]+)",operationName:"SearchTimeline"', main_js)
        if found:
            query_id = found.group(1)
            break
    if not query_id:
        return None, None, None

    _write_json(MATERIAL_FILE, {"home": home_html, "ondemand": ondemand_js,
                                "ondemand_url": str(ondemand_url), "query_id": query_id})
    return home_html, ondemand_js, query_id


def _dig_tweets(payload, count):
    instructions = (((payload.get("data") or {}).get("search_by_raw_query") or {})
                    .get("search_timeline") or {}).get("timeline", {}).get("instructions", [])
    items = []
    for instruction in instructions:
        for entry in instruction.get("entries", []) or []:
            result = (((entry.get("content") or {}).get("itemContent") or {})
                      .get("tweet_results") or {}).get("result") or {}
            legacy = result.get("legacy") or (result.get("tweet") or {}).get("legacy") or {}
            text = legacy.get("full_text")
            if not text:
                continue
            items.append({"time": legacy.get("created_at"), "text": text[:500]})
            if len(items) >= count:
                return items
    return items


def search(query, count=10, live=True, cookies=None):
    """返回 items 列表；不可用时返回 None（调用方降级 DOM）。"""
    try:
        from bs4 import BeautifulSoup
        from x_client_transaction import ClientTransaction
    except ImportError:
        return None

    cookies = cookies or get_cookies()
    if not cookies:
        return None
    home_html, ondemand_js, query_id = _client_material(cookies)
    if not home_html or not ondemand_js or not query_id:
        return None

    transaction = ClientTransaction(BeautifulSoup(home_html, "html.parser"),
                                    BeautifulSoup(ondemand_js, "html.parser"))
    path = f"/i/api/graphql/{query_id}/SearchTimeline"
    params = {
        "variables": json.dumps({"rawQuery": query, "count": max(20, min(count * 2, 50)),
                                 "querySource": "typed_query",
                                 "product": "Latest" if live else "Top"}, separators=(",", ":")),
        "features": json.dumps(GRAPHQL_FEATURES, separators=(",", ":")),
    }
    url = f"https://x.com{path}?{urllib.parse.urlencode(params)}"
    try:
        status, body = _http_get(url, cookies, {
            "authorization": f"Bearer {BEARER}",
            "x-csrf-token": cookies.get("ct0", ""),
            "x-client-transaction-id": transaction.generate_transaction_id(method="GET", path=path),
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "referer": "https://x.com/search",
        })
    except Exception:
        return None
    if status != 200:
        return None  # 401/403/404 → 由调用方降级并可在下次重建 cookie
    try:
        items = _dig_tweets(json.loads(body), count)
    except ValueError:
        return None
    return items or None
