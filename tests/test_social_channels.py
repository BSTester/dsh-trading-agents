"""社交渠道离线回归测试：API 优先 / DOM 降级 / 缓存 / 未登录处理。

全部离线——不发网络请求、不开浏览器。重点锁住"API 失败必须降级"这条路径，
因为它是唯一会静默失效的分支（渠道整体不可用往往由此产生）。
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIN = ROOT / "plugins" / "fin-data" / "python"
sys.path.insert(0, str(FIN))

import x_api  # noqa: E402
import reddit_search  # noqa: E402


class CookieCacheTests(unittest.TestCase):
    """cookie 缓存：命中即不碰浏览器，未命中才走 CDP。"""

    def setUp(self):
        self.calls = []

    def test_x_cache_hit_skips_browser(self):
        cached = {"cookies": {"auth_token": "t", "ct0": "c"}, "_at": time.time()}
        with patch.object(x_api, "_read_json", return_value=cached), \
             patch.object(x_api, "extract_cookies_via_browser",
                          side_effect=AssertionError("缓存命中时不应开浏览器")):
            self.assertEqual(x_api.get_cookies(), {"auth_token": "t", "ct0": "c"})

    def test_x_cache_expired_falls_back_to_browser(self):
        with patch.object(x_api, "_read_json", return_value=None), \
             patch.object(x_api, "extract_cookies_via_browser",
                          return_value={"auth_token": "new", "ct0": "c"}) as extract, \
             patch.object(x_api, "_write_json") as write:
            self.assertEqual(x_api.get_cookies(), {"auth_token": "new", "ct0": "c"})
            extract.assert_called_once()
            write.assert_called_once()  # 取到即落盘，下次免开浏览器

    def test_x_extraction_requires_login_cookies(self):
        """未登录（缺 auth_token）必须返回 None，不能返回半截 cookie 去发请求。"""
        import x_search
        with patch.object(x_search, "ensure_browser", return_value=True), \
             patch.object(x_search, "connect_browser", return_value=_FakeBrowser()), \
             patch.object(x_search, "acquire_scratch_page", return_value=None), \
             patch.object(x_search, "close_scratch_pages"), \
             patch.dict(sys.modules, {"playwright.sync_api": _FakePlaywrightModule()}):
            result = x_api.extract_cookies_via_browser(
                domains=("x.com",), required=("auth_token", "ct0"))
        self.assertIsNone(result)  # CDP 只返回 ct0，缺 auth_token → 判未登录

    def test_x_extraction_returns_cookies_when_logged_in(self):
        import x_search
        with patch.object(x_search, "ensure_browser", return_value=True), \
             patch.object(x_search, "connect_browser", return_value=_FakeBrowser(full=True)), \
             patch.object(x_search, "acquire_scratch_page", return_value=None), \
             patch.object(x_search, "close_scratch_pages"), \
             patch.dict(sys.modules, {"playwright.sync_api": _FakePlaywrightModule()}):
            result = x_api.extract_cookies_via_browser(
                domains=("x.com",), required=("auth_token", "ct0"))
        self.assertEqual(result, {"auth_token": "a", "ct0": "c"})

    def test_x_extraction_filters_by_domain(self):
        """只取目标站 cookie——否则会把无关站点的 cookie 一起发出去。"""
        import x_search
        with patch.object(x_search, "ensure_browser", return_value=True), \
             patch.object(x_search, "connect_browser", return_value=_FakeBrowser(full=True)), \
             patch.object(x_search, "acquire_scratch_page", return_value=None), \
             patch.object(x_search, "close_scratch_pages"), \
             patch.dict(sys.modules, {"playwright.sync_api": _FakePlaywrightModule()}):
            result = x_api.extract_cookies_via_browser(
                domains=("reddit",), required=("auth_token",))
        self.assertIsNone(result, "reddit 白名单不应取到 x.com 的 cookie")

    def test_reddit_cache_hit_skips_browser(self):
        cached = {"cookies": {"reddit_session": "s"}, "_at": time.time()}
        with patch.object(x_api, "_read_json", return_value=cached), \
             patch.object(x_api, "extract_cookies_via_browser",
                          side_effect=AssertionError("缓存命中时不应开浏览器")):
            self.assertEqual(reddit_search.get_reddit_cookies(), {"reddit_session": "s"})

    def test_reddit_cache_requires_session_cookie(self):
        """缓存里没有 reddit_session 视为未登录，必须重取。"""
        stale = {"cookies": {"other": "x"}, "_at": time.time()}
        with patch.object(x_api, "_read_json", return_value=stale), \
             patch.object(x_api, "extract_cookies_via_browser",
                          return_value={"reddit_session": "fresh"}) as extract, \
             patch.object(x_api, "_write_json"):
            self.assertEqual(reddit_search.get_reddit_cookies(), {"reddit_session": "fresh"})
            extract.assert_called_once()


class DegradationTests(unittest.TestCase):
    """API 不可用时必须返回 None（而非抛异常或返回空列表），调用方才好降级。"""

    def test_x_search_returns_none_without_cookies(self):
        with patch.object(x_api, "get_cookies", return_value=None):
            self.assertIsNone(x_api.search("$AAPL", 5))

    def test_x_search_returns_none_on_non_200(self):
        with patch.object(x_api, "get_cookies", return_value={"auth_token": "t", "ct0": "c"}), \
             patch.object(x_api, "_client_material", return_value=("<html></html>", "<js></js>", "QID")), \
             patch.object(x_api, "_http_get", return_value=(404, "{}")), \
             patch.dict(sys.modules, {"x_client_transaction": _FakeTransactionModule()}):
            self.assertIsNone(x_api.search("$AAPL", 5))

    def test_x_search_returns_none_when_material_unavailable(self):
        with patch.object(x_api, "get_cookies", return_value={"auth_token": "t", "ct0": "c"}), \
             patch.object(x_api, "_client_material", return_value=(None, None, None)), \
             patch.dict(sys.modules, {"x_client_transaction": _FakeTransactionModule()}):
            self.assertIsNone(x_api.search("$AAPL", 5))

    def test_reddit_http_returns_none_without_cookies(self):
        with patch.object(reddit_search, "get_reddit_cookies", return_value=None):
            self.assertIsNone(reddit_search.try_api_http("AAPL", 5, "relevance"))

    def test_reddit_http_returns_none_on_network_error(self):
        with patch.object(reddit_search, "get_reddit_cookies",
                          return_value={"reddit_session": "s"}), \
             patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertIsNone(reddit_search.try_api_http("AAPL", 5, "relevance"))


class RedditParsingTests(unittest.TestCase):
    """Reddit JSON → 统一条目结构（字段名必须与 X 侧对齐，工作台才能合并展示）。"""

    PAYLOAD = {"data": {"children": [
        {"data": {"title": "NVDA 讨论", "subreddit_name_prefixed": "r/stocks", "score": 120,
                  "num_comments": 45, "created_utc": 1757332659,
                  "permalink": "/r/stocks/comments/abc/nvda/"}},
        {"data": {"title": "", "subreddit_name_prefixed": "r/x"}},  # 无标题必须丢弃
    ]}}

    def test_items_shape(self):
        with patch.object(reddit_search, "get_reddit_cookies",
                          return_value={"reddit_session": "s"}), \
             patch("urllib.request.urlopen", return_value=_FakeResponse(self.PAYLOAD)):
            items = reddit_search.try_api_http("NVDA", 10, "relevance")
        self.assertEqual(len(items), 1)  # 空标题被过滤
        row = items[0]
        self.assertEqual(set(row), {"title", "subreddit", "score", "comments", "time", "url"})
        self.assertEqual(row["title"], "NVDA 讨论")
        self.assertEqual(row["score"], "120")          # 统一为字符串，与 X 一致
        self.assertEqual(row["comments"], "45")
        self.assertTrue(row["url"].startswith("https://www.reddit.com/r/stocks/"))
        self.assertRegex(row["time"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_subreddit_scoping_builds_reddit_query(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            return _FakeResponse({"data": {"children": []}})

        with patch.object(reddit_search, "get_reddit_cookies",
                          return_value={"reddit_session": "s"}), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            reddit_search.try_api_http("NVDA", 5, "new", subreddit="stocks")
        self.assertIn("subreddit%3Astocks+NVDA", captured["url"])
        self.assertIn("sort=new", captured["url"])


class QueryIdTests(unittest.TestCase):
    """queryId 只在 client-web/main.*.js 里——早期从首页 HTML 找必然失败。"""

    HOME = ('<html><script src="https://abs.twimg.com/responsive-web/client-web/'
            'main.abc123.js"></script></html>')
    MAIN_JS = '...{queryId:"KPSo2_UWdOMpPJwjhfT1Qg",operationName:"SearchTimeline"}...'

    def test_extracts_query_id_from_main_bundle(self):
        calls = []

        def fake_get(url, cookies, extra=None, timeout=30):
            calls.append(url)
            if "x.com/home" in url:
                return (200, self.HOME)
            if "ondemand" in url:
                return (200, "ondemand-js")
            return (200, self.MAIN_JS)

        fake_utils = _FakeUtilsModule()
        with patch.object(x_api, "_read_json", return_value=None), \
             patch.object(x_api, "_write_json") as write, \
             patch.object(x_api, "_http_get", side_effect=fake_get), \
             patch.dict(sys.modules, {"x_client_transaction.utils": fake_utils,
                                      "bs4": _FakeBs4Module()}):
            home, ondemand, qid = x_api._client_material({"auth_token": "t"})
        self.assertEqual(qid, "KPSo2_UWdOMpPJwjhfT1Qg")
        self.assertEqual(ondemand, "ondemand-js")
        self.assertTrue(any("main.abc123.js" in u for u in calls), "必须去抓 main 包")

    def test_missing_query_id_returns_none_triple(self):
        with patch.object(x_api, "_read_json", return_value=None), \
             patch.object(x_api, "_http_get", return_value=(200, "<html></html>")), \
             patch.dict(sys.modules, {"x_client_transaction.utils": _FakeUtilsModule(),
                                      "bs4": _FakeBs4Module()}):
            self.assertEqual(x_api._client_material({"auth_token": "t"}), (None, None, None))

    def test_material_cache_is_reused(self):
        cached = {"home": "<h>", "ondemand": "<o>", "query_id": "QID", "_at": time.time()}
        with patch.object(x_api, "_read_json", return_value=cached), \
             patch.object(x_api, "_http_get",
                          side_effect=AssertionError("缓存命中不应再发请求")):
            self.assertEqual(x_api._client_material({"auth_token": "t"}), ("<h>", "<o>", "QID"))


class XSearchCliTests(unittest.TestCase):
    """x_search CLI：API 成功即返回且标注 path，失败必须落回 DOM 分支。"""

    def test_api_success_path(self):
        import x_search
        argv = ["x_search.py", "$AAPL", "--count", "3"]
        with patch.object(sys, "argv", argv), \
             patch.dict(sys.modules, {"x_api": _StubModule(search=lambda *a, **k: [
                 {"time": "t", "text": "x"}])}), \
             patch.object(x_search, "x_reachable", return_value=True), \
             patch("builtins.print") as out:
            self.assertEqual(x_search.main(), 0)
        payload = json.loads(out.call_args[0][0])
        self.assertEqual(payload["path"], "api/graphql")
        self.assertEqual(payload["count"], 1)

    def test_api_failure_falls_back_to_dom(self):
        """API 返回 None 时必须继续走 DOM，而不是直接失败返回。"""
        import x_search
        argv = ["x_search.py", "$AAPL", "--count", "3"]
        reached = {"dom": False}

        def fake_ensure_browser():
            reached["dom"] = True
            return False  # 到此即证明进入了 DOM 分支

        with patch.object(sys, "argv", argv), \
             patch.dict(sys.modules, {"x_api": _StubModule(search=lambda *a, **k: None)}), \
             patch.object(x_search, "x_reachable", return_value=True), \
             patch.object(x_search, "ensure_browser", side_effect=fake_ensure_browser):
            self.assertEqual(x_search.main(), 1)
        self.assertTrue(reached["dom"], "API 失败后必须降级到 DOM 抓取")


# ---- 测试替身（不引入任何真实网络/浏览器依赖）----

def x_search_stub():
    import x_search
    return x_search


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubModule:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class _FakeTransactionModule:
    class ClientTransaction:
        def __init__(self, *a, **k):
            pass

        def generate_transaction_id(self, **k):
            return "tid"


class _FakeUtilsModule:
    @staticmethod
    def get_ondemand_file_url(soup):
        return "https://abs.twimg.com/responsive-web/client-web/ondemand.s.js"


class _FakeSoup:
    def __init__(self, markup, parser=None):
        self.markup = markup


class _FakeBs4Module:
    BeautifulSoup = _FakeSoup


class _FakeCdpSession:
    def __init__(self, full):
        self.full = full

    def send(self, method):
        cookies = [{"domain": ".x.com", "name": "ct0", "value": "c"},
                   {"domain": ".reddit.com", "name": "reddit_session", "value": "r"}]
        if self.full:
            cookies.append({"domain": ".x.com", "name": "auth_token", "value": "a"})
        return {"cookies": cookies}


class _FakeContext:
    def __init__(self, full):
        self.full = full

    def new_cdp_session(self, page):
        return _FakeCdpSession(self.full)


class _FakeBrowser:
    def __init__(self, full=False):
        self.contexts = [_FakeContext(full)]


class _FakePlaywrightModule:
    """最小替身：只需 sync_playwright() 能当上下文管理器用。"""

    @staticmethod
    def sync_playwright():
        return _FakePlaywright()


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stop(self):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
