"""``server.v3_sources`` 的契约测试：全部注入假 fetch / 假三方模块，**不打网络**。

覆盖点（每条都对应一个「如实报错、不伪造」的承诺）：
  * news：symbol 归一（``SH.600519`` → ``600519``）、行形状、上游异常 → ``akshare/...`` 错误；
  * spot：上游常见的 ``RemoteDisconnected`` 必须原样进 error 信封，不返回空成功；
  * financials：form 过滤、按期末去重取最近 N 期、``stale`` 阈值、未申报标签进 missing、
    ticker→CIK 索引缓存落盘、statement 白名单；
  * tushare：**无 token 时一次网络调用都不发**、``code!=0`` 透传 ``msg``、字段展开成对象；
  * openbb：未安装 → ``openbb/unavailable`` 且不发请求、结果对象只留叶子字段（不序列化活对象）；
  * ``register(app, v3_run, home)`` 的三个位置参数形态与路由注册。

``setUp`` 里把 socket / urllib / httpx 三条真实出口全部封死：任何漏注入的取数都会让测试
显式失败，而不是悄悄联网后「碰巧通过」。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
import types
import unittest
from datetime import date, timedelta
from unittest import mock

from server import v3_fallback, v3_sources


# ── 测试替身 ───────────────────────────────────────────────────────────────────


class FakeFetch:
    """按 URL 路由的假取数器；记录每次调用以便断言「一次都没发」。"""

    def __init__(self, routes=None, default=None):
        self.routes = list(routes or [])
        self.default = default
        self.calls = []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        for matcher, responder in self.routes:
            if matcher in url:
                return responder(url, headers) if callable(responder) else responder
        if self.default is not None:
            return self.default(url, headers) if callable(self.default) else self.default
        return {"ok": False, "error": {"code": "test/unrouted", "message": f"未路由的 URL：{url}"}}

    @property
    def urls(self):
        return [call["url"] for call in self.calls]


class FakePostFetch:
    """假 POST 取数器：签名与 ``Deps.json_post`` 的注入契约一致（body 是命名参数）。"""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, url, body=None, headers=None, timeout=None):
        self.calls.append({"url": url, "body": body, "headers": dict(headers or {}), "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.response if self.response is not None else {"ok": False, "error": {"code": "test/unrouted", "message": url}}

    @property
    def bodies(self):
        return [call["body"] for call in self.calls]


class FakeAkshare:
    """假 akshare：只提供被白名单化的两个函数，且把收到的参数记下来。"""

    def __init__(self, news=None, spot=None, news_error=None, spot_error=None):
        self.calls = []
        self._news = news
        self._spot = spot
        self._news_error = news_error
        self._spot_error = spot_error

    def stock_news_em(self, symbol=None):
        self.calls.append(("stock_news_em", symbol))
        if self._news_error is not None:
            raise self._news_error
        return self._news

    def stock_zh_a_spot_em(self):
        self.calls.append(("stock_zh_a_spot_em",))
        if self._spot_error is not None:
            raise self._spot_error
        return self._spot


class FakeModel:
    """pydantic 风格的结果行（有 ``model_dump``）——验证只留叶子字段。"""

    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return dict(self._data)


class FakeObbOutput:
    def __init__(self, results):
        self.results = results


class FakeApp:
    """最小 FastAPI 替身：只要 ``get`` 装饰器能收集「路径 → 处理函数」。"""

    def __init__(self):
        self.routes = {}
        self.state = types.SimpleNamespace()

    def get(self, path):
        def decorator(func):
            self.routes[path] = func
            return func

        return decorator


class FakeV3Run:
    """``v3_run`` 替身（本模块只用它占位契约第三参数）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, payload))
        return {"ok": True, "value": None}


# ── 行构造助手 ─────────────────────────────────────────────────────────────────


def sec_point(end, val, form="10-Q", filed=None, start="2023-01-01", fy=2023, fp="Q1"):
    return {"end": end, "val": val, "form": form, "filed": filed or end, "start": start, "fy": fy, "fp": fp}


def concept_payload(label, unit, points, taxonomy="us-gaap"):
    return {"label": label, "taxonomy": taxonomy, "units": {unit: list(points)}}


def days_ago(days):
    """相对今天的日期串——``stale`` 是按 ``date.today()`` 判的，写死年月会在将来变味。"""
    return (date.today() - timedelta(days=days)).isoformat()


def tickers_payload(**entries):
    """``tickers_payload(AAPL=(320193, "Apple Inc."))`` → SEC company_tickers.json 形态。"""
    return {
        str(index): {"cik_str": cik, "ticker": ticker, "title": title}
        for index, (ticker, (cik, title)) in enumerate(entries.items())
    }


def sec_fetch(ticker_rows, concepts):
    """构造路由公司索引与 companyconcept 的假 fetch。"""

    def concept_responder(url, _headers):
        tag = url.rsplit("/", 1)[-1].replace(".json", "")
        payload = concepts.get(tag)
        if payload is None:
            return {"ok": False, "error": {"code": "sec/http-404", "message": f"{url} → HTTP 404"}}
        return {"ok": True, "value": payload}

    return FakeFetch(
        routes=[
            ("company_tickers.json", {"ok": True, "value": ticker_rows}),
            ("/api/xbrl/companyconcept/", concept_responder),
        ]
    )


class BlockRealNetwork(unittest.TestCase):
    """基类：把真实网络出口全部封死（漏注入的取数会显式失败）。

    不 patch ``socket.socket`` 本身：asyncio 的事件循环自建 socketpair 也走那里，封死会把
    ``asyncio.run`` 一起弄坏（表现为 coroutine never awaited + 事件循环析构报错）。这里封的
    是**连接动作**：``socket.socket.connect``、``socket.create_connection``、
    ``urllib.request.urlopen``、``httpx.Client``。
    """

    def setUp(self):
        patchers = [
            mock.patch("socket.socket.connect", side_effect=AssertionError("测试禁止真实网络：socket.connect")),
            mock.patch("socket.create_connection", side_effect=AssertionError("测试禁止真实网络：create_connection")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("测试禁止真实网络：urlopen")),
            mock.patch("httpx.Client", side_effect=AssertionError("测试禁止真实网络：httpx.Client")),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp(prefix="v3sources-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # 装配 + 调用：走真实的 ``register``，再按路径取处理函数（无 TestClient 依赖）
    def build(self, deps):
        app = FakeApp()
        self.app = app
        self.deps = v3_sources.register(app, FakeV3Run(), self.tmp, deps)
        return app

    def call(self, path, **params):
        handler = self.app.routes.get(path)
        self.assertIsNotNone(handler, f"路由未注册：{path}（已注册 {sorted(self.app.routes)}）")
        return asyncio.run(handler(**params))

    def assert_error(self, payload, prefix):
        self.assertFalse(payload.get("ok"), payload)
        code = payload["error"]["code"]
        self.assertTrue(code.startswith(prefix), f"错误码 {code!r} 不以 {prefix!r} 开头")
        self.assertTrue(payload["error"]["message"], "错误必须带真实 message")
        return payload["error"]


# ── 纯函数 ─────────────────────────────────────────────────────────────────────


class PureFunctionTests(unittest.TestCase):
    def test_normalize_a_share_symbol(self):
        cases = {
            "600519": "600519",
            "SH.600519": "600519",
            "SZ.000001": "000001",
            "BJ.430047": "430047",
            "sh600519": "600519",
            "600519.SH": "600519",
            "  600519  ": "600519",
            "": "",
            "AAPL": "AAPL",
        }
        for raw, expected in cases.items():
            self.assertEqual(v3_sources.normalize_a_share_symbol(raw), expected, raw)

    def test_to_int_clamps_and_defaults(self):
        self.assertEqual(v3_sources.to_int("25", 10), 25)
        self.assertEqual(v3_sources.to_int("", 10), 10)
        self.assertEqual(v3_sources.to_int("abc", 10), 10)
        self.assertEqual(v3_sources.to_int(None, 10), 10)
        self.assertEqual(v3_sources.to_int("0", 10), 1)          # 下限
        self.assertEqual(v3_sources.to_int("10", 10, maximum=5), 5)  # 上限

    def test_as_number_does_not_fabricate_zero(self):
        self.assertIsNone(v3_sources.as_number(None))
        self.assertIsNone(v3_sources.as_number(float("nan")))
        self.assertIsNone(v3_sources.as_number(""))
        self.assertIsNone(v3_sources.as_number("--"))
        self.assertIsNone(v3_sources.as_number(True))
        self.assertEqual(v3_sources.as_number(0), 0.0)
        self.assertEqual(v3_sources.as_number("3.5"), 3.5)

    def test_as_text_normalizes_missing_markers(self):
        self.assertEqual(v3_sources.as_text(None), "")
        self.assertEqual(v3_sources.as_text(float("nan")), "")
        self.assertEqual(v3_sources.as_text("  x "), "x")
        self.assertEqual(v3_sources.as_text("None"), "")

    def test_age_days_since(self):
        self.assertEqual(v3_sources.age_days_since((date.today() - timedelta(days=10)).isoformat()), 10)
        self.assertIsNone(v3_sources.age_days_since(None))
        self.assertIsNone(v3_sources.age_days_since("not-a-date"))

    def test_statement_tags_match_spec(self):
        self.assertEqual(v3_sources.STATEMENT_TAGS["income"][0], "Revenues")
        self.assertIn("EarningsPerShareDiluted", v3_sources.STATEMENT_TAGS["income"])
        self.assertIn("LongTermDebtNoncurrent", v3_sources.STATEMENT_TAGS["balance"])
        self.assertIn(
            "PaymentsToAcquirePropertyPlantAndEquipment", v3_sources.STATEMENT_TAGS["cashflow"]
        )
        self.assertEqual(v3_sources.SEC_STALE_DAYS, 400)

    def test_jsonable_keeps_leaf_fields_only(self):
        value = FakeModel({"a": 1, "nested": [FakeModel({"b": "x"})], "obj": types.SimpleNamespace(s="1")})
        out = v3_sources.jsonable(value)
        self.assertEqual(out["a"], 1)
        self.assertEqual(out["nested"][0]["b"], "x")
        self.assertIsInstance(out["obj"], str)  # 不认识的对象 → 文本，绝不原样透传
        json.dumps(out)  # 必须可 JSON 序列化

    def test_rows_from_frame_accepts_list_and_iterrows(self):
        self.assertEqual(v3_sources._rows_from_frame([{"a": 1}]), [{"a": 1}])

        class Frame:
            def iterrows(self):
                return iter([(0, {"a": 2})])

        self.assertEqual(v3_sources._rows_from_frame(Frame()), [{"a": 2}])
        self.assertEqual(v3_sources._rows_from_frame(None), [])


# ── /api/v3/news ───────────────────────────────────────────────────────────────


class NewsRouteTests(BlockRealNetwork):
    def test_success_row_shape(self):
        ak = FakeAkshare(
            news=[
                {
                    "新闻标题": "贵州茅台：中报净利润 445.17 亿元",
                    "新闻内容": "正文" * 10,
                    "发布时间": "2026-08-15 10:11:51",
                    "文章来源": "界面新闻",
                    "新闻链接": "https://example.com/a",
                    "关键词": "600519",
                },
                {"新闻标题": "第二条", "新闻内容": "", "发布时间": "", "文章来源": "", "新闻链接": "", "关键词": ""},
            ]
        )
        self.build(v3_sources.Deps(akshare=ak, home=self.tmp))
        payload = self.call("/api/v3/news", symbol="600519", limit=10)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["source"], "akshare/stock_news_em")
        self.assertEqual(payload["symbol"], "600519")
        self.assertEqual(len(payload["rows"]), 2)
        row = payload["rows"][0]
        self.assertEqual(
            sorted(row), ["keyword", "published_at", "source", "summary", "title", "url"]
        )
        self.assertEqual(row["title"], "贵州茅台：中报净利润 445.17 亿元")
        self.assertEqual(row["published_at"], "2026-08-15 10:11:51")
        self.assertTrue(payload["as_of"].endswith("+00:00"), payload["as_of"])
        self.assertEqual(ak.calls, [("stock_news_em", "600519")])

    def test_prefixed_symbol_is_stripped(self):
        ak = FakeAkshare(news=[{"新闻标题": "x"}])
        self.build(v3_sources.Deps(akshare=ak, home=self.tmp))
        payload = self.call("/api/v3/news", symbol="SH.600519", limit=1)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(ak.calls, [("stock_news_em", "600519")])
        self.assertEqual(payload["symbol"], "600519")

    def test_limit_is_applied(self):
        rows = [{"新闻标题": f"n{i}"} for i in range(10)]
        self.build(v3_sources.Deps(akshare=FakeAkshare(news=rows), home=self.tmp))
        payload = self.call("/api/v3/news", symbol="600519", limit=3)
        self.assertEqual(len(payload["rows"]), 3)

    def test_upstream_failure_is_reported_truthfully(self):
        error = ConnectionError(
            "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
        )
        self.build(v3_sources.Deps(akshare=FakeAkshare(news_error=error), home=self.tmp))
        payload = self.call("/api/v3/news", symbol="600519", limit=10)
        detail = self.assert_error(payload, "akshare/")
        self.assertEqual(detail["code"], "akshare/stock_news_em")
        self.assertIn("RemoteDisconnected", detail["message"])

    def test_empty_symbol_fails_before_touching_akshare(self):
        ak = FakeAkshare(news=[])
        self.build(v3_sources.Deps(akshare=ak, home=self.tmp))
        detail = self.assert_error(self.call("/api/v3/news", symbol="", limit=10), "akshare/")
        self.assertEqual(detail["code"], "akshare/bad-args")
        self.assertEqual(ak.calls, [])

    def test_missing_akshare_reports_missing(self):
        def boom():
            raise ImportError("No module named 'akshare'")

        self.build(v3_sources.Deps(akshare=boom, home=self.tmp))
        detail = self.assert_error(self.call("/api/v3/news", symbol="600519", limit=10), "akshare/")
        self.assertEqual(detail["code"], "akshare/missing")
        self.assertIn("akshare", detail["message"])


# ── /api/v3/spot ───────────────────────────────────────────────────────────────


class SpotRouteTests(BlockRealNetwork):
    def test_success_maps_columns_to_contract(self):
        ak = FakeAkshare(
            spot=[
                {
                    "代码": "600519",
                    "名称": "贵州茅台",
                    "最新价": 1500.5,
                    "涨跌幅": -1.25,
                    "换手率": 0.42,
                    "量比": 1.08,
                    "市盈率-动态": 22.3,
                    "市净率": 7.8,
                },
                {"代码": "000001", "名称": "平安银行", "最新价": float("nan"), "涨跌幅": None},
            ]
        )
        self.build(v3_sources.Deps(akshare=ak, home=self.tmp))
        payload = self.call("/api/v3/spot", limit=20)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["source"], "akshare/stock_zh_a_spot_em")
        first = payload["rows"][0]
        self.assertEqual(
            sorted(first),
            ["change_pct", "code", "name", "pb", "pe", "price", "turnover_rate", "volume_ratio"],
        )
        self.assertEqual(first["name"], "贵州茅台")
        self.assertEqual(first["price"], 1500.5)
        self.assertEqual(first["change_pct"], -1.25)
        # 缺值就是 None，不许补 0
        self.assertIsNone(payload["rows"][1]["price"])
        self.assertIsNone(payload["rows"][1]["pe"])
        self.assertEqual(ak.calls, [("stock_zh_a_spot_em",)])

    def test_remote_disconnected_is_returned_as_error_not_faked(self):
        error = ConnectionError(
            "('Connection aborted.', RemoteDisconnected('Remote end closed connection without response'))"
        )
        self.build(v3_sources.Deps(akshare=FakeAkshare(spot_error=error), home=self.tmp))
        payload = self.call("/api/v3/spot", limit=20)
        detail = self.assert_error(payload, "akshare/")
        self.assertEqual(detail["code"], "akshare/stock_zh_a_spot_em")
        self.assertIn("RemoteDisconnected", detail["message"])
        self.assertNotIn("rows", payload)

    def test_limit_applied(self):
        rows = [{"代码": str(600000 + i), "名称": f"n{i}"} for i in range(50)]
        self.build(v3_sources.Deps(akshare=FakeAkshare(spot=rows), home=self.tmp))
        payload = self.call("/api/v3/spot", limit=5)
        self.assertEqual(len(payload["rows"]), 5)


# ── /api/v3/financials ─────────────────────────────────────────────────────────


class FinancialsRouteTests(BlockRealNetwork):
    def _deps(self, fetch, **kwargs):
        return v3_sources.Deps(fetch_json=fetch, home=self.tmp, **kwargs)

    def test_income_filters_forms_dedupes_and_flags_stale(self):
        tickers = tickers_payload(AAPL=(320193, "Apple Inc."))
        # 近期/陈旧两个期末都相对今天算，测试不会随时间腐烂
        recent_end = days_ago(45)
        stale_end = days_ago(600)
        concepts = {
            "Revenues": concept_payload(
                "Revenues",
                "USD",
                [sec_point(stale_end, 62900000000, form="10-K"), sec_point("2017-09-30", 1, form="10-K")],
            ),
            "RevenueFromContractWithCustomerExcludingAssessedTax": concept_payload(
                "Revenue from Contract with Customer, Excluding Assessed Tax",
                "USD",
                [
                    sec_point(days_ago(120), 1, form="10-Q"),
                    sec_point(recent_end, 2, form="10-Q", filed=days_ago(35), start=days_ago(120)),
                    # 8-K 口径必须被过滤掉
                    sec_point("2026-01-01", 999, form="8-K"),
                    # 同一期末多口径（10-K 里的年度值与 Q4 值共用 end）：start 更早（覆盖期更长）
                    # 的那条必须胜出，且与 filed 顺序无关。
                    sec_point(recent_end, 3, form="10-Q", filed=days_ago(50), start=days_ago(400)),
                ],
            ),
            "GrossProfit": concept_payload("Gross Profit", "USD", [sec_point(recent_end, 54.7)]),
            "OperatingIncomeLoss": concept_payload("Operating Income (Loss)", "USD", [sec_point(recent_end, 29.6)]),
            "NetIncomeLoss": concept_payload("Net Income (Loss)", "USD", [sec_point(recent_end, 29.8)]),
            "EarningsPerShareDiluted": concept_payload("Earnings Per Share, Diluted", "USD/shares", [sec_point(recent_end, 2.02)]),
        }
        fetch = sec_fetch(tickers, concepts)
        self.build(self._deps(fetch))
        payload = self.call("/api/v3/financials", ticker="aapl", statement="income", periods=4)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["ticker"], "AAPL")
        self.assertEqual(payload["cik"], "0000320193")
        self.assertEqual(payload["company"], "Apple Inc.")
        self.assertEqual(payload["statement"], "income")
        self.assertEqual(payload["source"], "sec/companyconcept(us-gaap XBRL)")
        self.assertEqual(payload["missing"], [])
        by_tag = {line["tag"]: line for line in payload["lines"]}
        self.assertEqual(len(by_tag), 6)

        revenues = by_tag["Revenues"]
        self.assertEqual(revenues["latestEnd"], stale_end)
        self.assertTrue(revenues["stale"], "期末超过 400 天必须标 stale（AAPL 的 Revenues 就停在 2018）")
        self.assertGreater(revenues["ageDays"], 400)
        self.assertLessEqual(abs(revenues["ageDays"] - 600), 2)

        contract = by_tag["RevenueFromContractWithCustomerExcludingAssessedTax"]
        ends = [point["end"] for point in contract["points"]]
        self.assertEqual(ends, [days_ago(120), recent_end], "8-K 行必须被过滤掉")
        self.assertEqual(contract["points"][-1]["val"], 3, "同一期末应取覆盖期最长（start 最早）的那条")
        self.assertFalse(contract["stale"])
        self.assertLessEqual(contract["ageDays"], 400)
        self.assertEqual(contract["unit"], "USD")
        self.assertEqual(by_tag["EarningsPerShareDiluted"]["unit"], "USD/shares")
        # 请求头必须带可识别 UA（SEC 使用政策）
        concept_calls = [call for call in fetch.calls if "companyconcept" in call["url"]]
        self.assertTrue(concept_calls)
        for call in concept_calls:
            self.assertIn("/CIK0000320193/", call["url"], "companyconcept 路径要求 CIK 补零到 10 位")
            self.assertEqual(call["headers"]["user-agent"], v3_sources.DEFAULT_SEC_UA)
            self.assertIsNotNone(call["timeout"], "外部调用必须带超时")

    def test_periods_caps_points_from_the_tail(self):
        tickers = tickers_payload(AAPL=(320193, "Apple Inc."))
        points = [sec_point(f"20{20 + i}-06-28", i) for i in range(6)]
        concepts = {"Assets": concept_payload("Assets", "USD", points)}
        self.build(self._deps(sec_fetch(tickers, concepts)))
        payload = self.call("/api/v3/financials", ticker="AAPL", statement="balance", periods=3)
        assets = [line for line in payload["lines"] if line["tag"] == "Assets"][0]
        self.assertEqual(len(assets["points"]), 3)
        self.assertEqual([p["end"] for p in assets["points"]], ["2023-06-28", "2024-06-28", "2025-06-28"])
        self.assertEqual(assets["latestEnd"], "2025-06-28")

    def test_unreported_tags_go_to_missing_and_are_not_invented(self):
        tickers = tickers_payload(AAPL=(320193, "Apple Inc."))
        concepts = {"Assets": concept_payload("Assets", "USD", [sec_point("2025-06-28", 383266000000)])}
        fetch = sec_fetch(tickers, concepts)
        self.build(self._deps(fetch))
        payload = self.call("/api/v3/financials", ticker="AAPL", statement="balance", periods=4)
        self.assertTrue(payload["ok"], payload)
        tags = [line["tag"] for line in payload["lines"]]
        self.assertEqual(tags, ["Assets"])
        missing = {entry["tag"]: entry for entry in payload["missing"]}
        self.assertEqual(
            sorted(missing),
            ["CashAndCashEquivalentsAtCarryingValue", "Liabilities", "LongTermDebtNoncurrent", "StockholdersEquity"],
        )
        self.assertEqual(missing["Liabilities"]["error"], "sec/http-404")
        self.assertTrue(missing["Liabilities"]["message"])

    def test_label_with_no_points_is_missing_not_empty_line(self):
        tickers = tickers_payload(AAPL=(320193, "Apple Inc."))
        concepts = {"Assets": concept_payload("Assets", "USD", [sec_point("2025-06-28", 1, form="8-K")])}
        self.build(self._deps(sec_fetch(tickers, concepts)))
        payload = self.call("/api/v3/financials", ticker="AAPL", statement="balance", periods=4)
        self.assertEqual([line["tag"] for line in payload["lines"]], [])
        entry = [m for m in payload["missing"] if m["tag"] == "Assets"][0]
        self.assertEqual(entry["error"], "sec/no-points")

    def test_ticker_index_is_cached_to_home_and_reused_within_process(self):
        tickers = tickers_payload(AAPL=(320193, "Apple Inc."))
        concepts = {"Assets": concept_payload("Assets", "USD", [sec_point("2025-06-28", 1)])}
        fetch = sec_fetch(tickers, concepts)
        self.build(self._deps(fetch))
        self.call("/api/v3/financials", ticker="AAPL", statement="balance", periods=1)
        self.call("/api/v3/financials", ticker="AAPL", statement="balance", periods=1)
        ticker_calls = [url for url in fetch.urls if "company_tickers" in url]
        self.assertEqual(len(ticker_calls), 1, "同一进程内索引只取一次")
        cached = os.path.join(self.tmp, v3_sources.SEC_TICKERS_CACHE)
        self.assertTrue(os.path.exists(cached), "索引必须缓存到 home 下")
        with open(cached, "r", encoding="utf-8") as handle:
            self.assertIn("AAPL", json.dumps(json.load(handle)))

    def test_unknown_ticker(self):
        fetch = sec_fetch(tickers_payload(AAPL=(320193, "Apple Inc.")), {})
        self.build(self._deps(fetch))
        detail = self.assert_error(self.call("/api/v3/financials", ticker="NOPE", statement="income", periods=4), "sec/")
        self.assertEqual(detail["code"], "sec/unknown-ticker")
        self.assertFalse([url for url in fetch.urls if "companyconcept" in url])

    def test_bad_statement_short_circuits_before_network(self):
        fetch = FakeFetch()
        self.build(self._deps(fetch))
        detail = self.assert_error(self.call("/api/v3/financials", ticker="AAPL", statement="cashflow2", periods=4), "sec/")
        self.assertEqual(detail["code"], "sec/bad-statement")
        self.assertEqual(fetch.calls, [])

    def test_index_network_failure_is_reported(self):
        fetch = FakeFetch(default={"ok": False, "error": {"code": "sec/network", "message": "boom"}})
        self.build(self._deps(fetch))
        detail = self.assert_error(self.call("/api/v3/financials", ticker="AAPL", statement="income", periods=4), "sec/")
        self.assertEqual(detail["code"], "sec/network")

    def test_index_failure_falls_back_to_file_cache(self):
        cached = os.path.join(self.tmp, v3_sources.SEC_TICKERS_CACHE)
        with open(cached, "w", encoding="utf-8") as handle:
            json.dump(tickers_payload(AAPL=(320193, "Apple Inc.")), handle)
        concepts = {"Assets": concept_payload("Assets", "USD", [sec_point("2025-06-28", 7)])}

        def responder(url, _headers):
            if "company_tickers" in url:
                return {"ok": False, "error": {"code": "sec/network", "message": "offline"}}
            return {"ok": True, "value": concepts["Assets"]}

        self.build(self._deps(FakeFetch(routes=[("company_tickers", responder), ("companyconcept", responder)])))
        payload = self.call("/api/v3/financials", ticker="AAPL", statement="balance", periods=1)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["cik"], "0000320193")
        self.assertEqual(payload["lines"][0]["points"][0]["val"], 7)


# ── /api/v3/tushare ────────────────────────────────────────────────────────────


class TushareRouteTests(BlockRealNetwork):
    def test_no_token_sends_no_request(self):
        fetch = FakePostFetch()
        self.build(v3_sources.Deps(fetch_post=fetch, env={}, home=self.tmp))
        payload = self.call("/api/v3/tushare", api="daily", ts_code="600519.SH", period="", limit=60)
        detail = self.assert_error(payload, "tushare/")
        self.assertEqual(detail["code"], "tushare/no-token")
        # 文案有意指向「页面可配置」（/api/v3/credentials），与 server/v3_sources.py 同步
        self.assertEqual(detail["message"],
                         "TUSHARE_TOKEN 未注入（可在「接入与授权」页配置，或用环境变量）")
        self.assertEqual(fetch.calls, [], "无 token 时绝不能发请求")

    def test_empty_token_string_also_counts_as_missing(self):
        fetch = FakePostFetch()
        self.build(v3_sources.Deps(fetch_post=fetch, env={"TUSHARE_TOKEN": ""}, home=self.tmp))
        detail = self.assert_error(self.call("/api/v3/tushare", api="daily", ts_code="600519.SH"), "tushare/")
        self.assertEqual(detail["code"], "tushare/no-token")
        self.assertEqual(fetch.calls, [])

    def test_success_expands_fields_into_objects(self):
        body = {
            "code": 0,
            "msg": None,
            "data": {
                "fields": ["ts_code", "end_date", "revenue", "n_income"],
                "items": [["600519.SH", "20241231", 174144000000.0, 86228000000.0]],
            },
        }
        fetch = FakePostFetch(response={"ok": True, "value": body})
        self.build(v3_sources.Deps(fetch_post=fetch, env={"TUSHARE_TOKEN": "tok"}, home=self.tmp))
        payload = self.call("/api/v3/tushare", api="income", ts_code="600519.SH", period="20241231", limit=8)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["api"], "income")
        self.assertEqual(payload["source"], "tushare/income")
        self.assertEqual(
            payload["rows"],
            [{"ts_code": "600519.SH", "end_date": "20241231", "revenue": 174144000000.0, "n_income": 86228000000.0}],
        )
        call = fetch.calls[0]
        self.assertEqual(call["url"], v3_sources.TUSHARE_ENDPOINT)
        self.assertIsNotNone(call["timeout"], "外部调用必须带超时")
        sent = call["body"]
        self.assertEqual(sent["api_name"], "income")
        self.assertEqual(sent["token"], "tok")
        self.assertEqual(sent["params"]["ts_code"], "600519.SH")
        self.assertEqual(sent["params"]["period"], "20241231")
        self.assertIn("n_income", sent["fields"])

    def test_upstream_error_code_passes_msg_through(self):
        body = {"code": 2002, "msg": "抱歉，您没有接口访问权限", "data": None}
        fetch = FakePostFetch(response={"ok": True, "value": body})
        self.build(v3_sources.Deps(fetch_post=fetch, env={"TUSHARE_TOKEN": "tok"}, home=self.tmp))
        detail = self.assert_error(self.call("/api/v3/tushare", api="income", ts_code="600519.SH"), "tushare/")
        self.assertEqual(detail["code"], "tushare/api")
        self.assertEqual(detail["message"], "抱歉，您没有接口访问权限")

    def test_http_failure_and_timeout_are_reported(self):
        fetch = FakePostFetch(response={"ok": False, "error": {"code": "tushare/http-500", "message": "HTTP 500"}})
        self.build(v3_sources.Deps(fetch_post=fetch, env={"TUSHARE_TOKEN": "tok"}, home=self.tmp))
        detail = self.assert_error(self.call("/api/v3/tushare", api="daily", ts_code="600519.SH"), "tushare/")
        self.assertEqual(detail["code"], "tushare/http-500")

        raiser = FakePostFetch(error=TimeoutError("read timeout"))
        self.build(v3_sources.Deps(fetch_post=raiser, env={"TUSHARE_TOKEN": "tok"}, home=self.tmp))
        detail = self.assert_error(self.call("/api/v3/tushare", api="daily", ts_code="600519.SH"), "tushare/")
        self.assertEqual(detail["code"], "tushare/network")
        self.assertIn("read timeout", detail["message"])

    def test_unknown_api_and_missing_ts_code(self):
        fetch = FakePostFetch(response={"ok": True, "value": {"code": 0, "data": {"fields": [], "items": []}}})
        self.build(v3_sources.Deps(fetch_post=fetch, env={"TUSHARE_TOKEN": "tok"}, home=self.tmp))
        detail = self.assert_error(self.call("/api/v3/tushare", api="nope", ts_code="600519.SH"), "tushare/")
        self.assertEqual(detail["code"], "tushare/unknown-api")
        detail = self.assert_error(self.call("/api/v3/tushare", api="income", ts_code=""), "tushare/")
        self.assertEqual(detail["code"], "tushare/bad-args")
        self.assertEqual(fetch.calls, [])

    def test_stock_basic_has_no_ts_code_requirement(self):
        body = {"code": 0, "data": {"fields": ["ts_code", "name"], "items": [["600519.SH", "贵州茅台"]]}}
        fetch = FakePostFetch(response={"ok": True, "value": body})
        self.build(v3_sources.Deps(fetch_post=fetch, env={"TUSHARE_TOKEN": "tok"}, home=self.tmp))
        payload = self.call("/api/v3/tushare", api="stock_basic", ts_code="", period="", limit=20)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["rows"], [{"ts_code": "600519.SH", "name": "贵州茅台"}])


# ── /api/v3/openbb ─────────────────────────────────────────────────────────────


def fake_openbb_module(metrics=None, error=None):
    def metrics_func(**kwargs):
        metrics_func.calls.append(kwargs)
        if error is not None:
            raise error
        return metrics

    metrics_func.calls = []
    module = types.ModuleType("openbb")
    module.obb = types.SimpleNamespace(
        equity=types.SimpleNamespace(fundamental=types.SimpleNamespace(metrics=metrics_func))
    )
    return module


class OpenbbRouteTests(BlockRealNetwork):
    def test_unavailable_reports_without_network(self):
        def boom():
            raise ImportError("No module named 'openbb'")

        self.build(v3_sources.Deps(openbb=boom, home=self.tmp))
        payload = self.call("/api/v3/openbb", symbol="AAPL")
        detail = self.assert_error(payload, "openbb/")
        self.assertEqual(detail["code"], "openbb/unavailable")
        self.assertIn("pip install openbb", detail["message"])

    def test_success_returns_leaf_fields_only(self):
        output = FakeObbOutput([FakeModel({"symbol": "AAPL", "pe_ratio": 38.59, "market_cap": 4.9e12, "obj": types.SimpleNamespace(x=1)})])
        module = fake_openbb_module(metrics=output)
        self.build(v3_sources.Deps(openbb=module, home=self.tmp))
        payload = self.call("/api/v3/openbb", symbol="aapl")
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["source"], "openbb/equity.fundamental.metrics")
        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["rows"][0]["pe_ratio"], 38.59)
        json.dumps(payload)  # 结果必须可序列化（不残留活对象）
        self.assertEqual(module.obb.equity.fundamental.metrics.calls[0]["symbol"], "AAPL")

    def test_provider_error_is_surfaced(self):
        module = fake_openbb_module(error=RuntimeError("Provider 'yfinance' is not supported for this endpoint"))
        self.build(v3_sources.Deps(openbb=module, home=self.tmp))
        detail = self.assert_error(self.call("/api/v3/openbb", symbol="AAPL"), "openbb/")
        self.assertEqual(detail["code"], "openbb/provider-error")
        self.assertIn("yfinance", detail["message"])

    def test_missing_capability_and_empty_symbol(self):
        module = types.ModuleType("openbb")
        module.obb = types.SimpleNamespace(equity=types.SimpleNamespace(fundamental=types.SimpleNamespace()))
        self.build(v3_sources.Deps(openbb=module, home=self.tmp))
        detail = self.assert_error(self.call("/api/v3/openbb", symbol="AAPL"), "openbb/")
        self.assertEqual(detail["code"], "openbb/capability-missing")

        self.build(v3_sources.Deps(openbb=module, home=self.tmp))
        detail = self.assert_error(self.call("/api/v3/openbb", symbol="  "), "openbb/")
        self.assertEqual(detail["code"], "openbb/bad-args")


# ── register() 契约 ────────────────────────────────────────────────────────────


class RegisterContractTests(BlockRealNetwork):
    def test_registers_all_five_routes_and_exposes_state(self):
        app = FakeApp()
        deps = v3_sources.Deps(home=self.tmp)
        returned = v3_sources.register(app, FakeV3Run(), self.tmp, deps)
        self.assertIs(returned, deps)
        self.assertEqual(
            sorted(app.routes),
            [
                "/api/v3/financials",
                "/api/v3/news",
                "/api/v3/openbb",
                "/api/v3/spot",
                "/api/v3/tushare",
            ],
        )
        self.assertEqual(app.state.v3_sources["deps"], deps)
        self.assertIsInstance(app.state.v3_sources["sec"], v3_sources.SecSource)

    def test_positional_three_arg_signature_builds_default_deps(self):
        app = FakeApp()
        deps = v3_sources.register(app, FakeV3Run(), self.tmp)
        self.assertIsInstance(deps, v3_sources.Deps)
        self.assertEqual(deps.home, self.tmp)
        self.assertEqual(sorted(app.routes), sorted(app.state.v3_sources["routes"]))

    def test_register_does_not_import_heavy_third_parties(self):
        """装配期不许 import akshare/openbb/pandas：否则服务启动与测试被十几秒拖住。"""
        import sys

        saved = {name: sys.modules.pop(name, None) for name in ("akshare", "openbb")}
        try:
            app = FakeApp()
            v3_sources.register(app, FakeV3Run(), self.tmp, v3_sources.Deps(home=self.tmp))
            self.assertNotIn("akshare", sys.modules)
            self.assertNotIn("openbb", sys.modules)
        finally:
            for name, module in saved.items():
                if module is not None:
                    sys.modules[name] = module


# ── AKShare 自动重试（v3_fallback.retry_akshare，2026-09-21）────────────────────


class TickingClock:
    """每次读都前进 ``step`` 秒的假时钟（与 ``time.monotonic`` 同单位，用来断言 ``ms``）。"""

    def __init__(self, step=0.1):
        self.now = 0.0
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


def remote_disconnected():
    """真机实测的上游断连异常（``RemoteDisconnected`` 是 ``ConnectionResetError`` 子类）。"""
    from http.client import RemoteDisconnected

    return RemoteDisconnected("Remote end closed connection without response")


class RetryAkshareTests(unittest.TestCase):
    """重试策略：连接/超时/5xx 才重试；业务错误与空结果不重试；退避可注入、可断言。"""

    def test_first_failure_then_success_retries_once(self):
        state = {"calls": 0}

        def flaky():
            state["calls"] += 1
            if state["calls"] == 1:
                raise remote_disconnected()
            return [{"代码": "600519"}]

        sleeps = []
        value, meta = v3_fallback.retry_akshare(flaky, sleep=sleeps.append, rand=lambda: 0.0)
        self.assertEqual(value, [{"代码": "600519"}])
        self.assertEqual([item["attempt"] for item in meta], [1, 2])
        self.assertFalse(meta[0]["ok"])
        self.assertTrue(meta[0]["retryable"])
        self.assertEqual(meta[0]["wait_ms"], v3_fallback.AKSHARE_RETRY_BASE_MS)
        self.assertIn("RemoteDisconnected", meta[0]["error"]["message"])
        self.assertTrue(meta[1]["ok"])
        self.assertEqual(sleeps, [v3_fallback.AKSHARE_RETRY_BASE_MS / 1000.0])

    def test_three_failures_return_none_with_full_attempts(self):
        calls = []

        def always():
            calls.append(1)
            raise ConnectionError("('Connection aborted.', RemoteDisconnected('x'))")

        value, meta = v3_fallback.retry_akshare(always, attempts=3, base_ms=100, max_ms=250,
                                               sleep=lambda _s: None, rand=lambda: 0.0)
        self.assertIsNone(value)
        self.assertEqual(len(meta), 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual([item.get("wait_ms") for item in meta if item.get("wait_ms")], [100, 200],
                         "第三次用尽即返回，不再等待")
        for item in meta:
            self.assertFalse(item["ok"])
            self.assertIn("RemoteDisconnected", item["error"]["message"])

    def test_business_error_is_not_retried(self):
        calls = []

        def bad_args():
            calls.append(1)
            raise ValueError("symbol 参数非法")

        value, meta = v3_fallback.retry_akshare(bad_args, sleep=lambda _s: (_ for _ in ()).throw(
            AssertionError("业务错误不得等待退避")), rand=lambda: 0.0)
        self.assertIsNone(value)
        self.assertEqual(len(calls), 1, "业务错误只尝试一次")
        self.assertEqual(len(meta), 1)
        self.assertFalse(meta[0]["retryable"])
        self.assertEqual(meta[0]["error"]["code"], "akshare/business-error")
        self.assertIn("ValueError", meta[0]["error"]["message"])
        self.assertNotIn("wait_ms", meta[0])

    def test_backoff_is_exponential_with_jitter_and_capped(self):
        sleeps = []

        def always():
            raise TimeoutError("read timeout")

        value, meta = v3_fallback.retry_akshare(always, attempts=4, base_ms=100, max_ms=250,
                                               sleep=sleeps.append, rand=lambda: 0.0)
        self.assertIsNone(value)
        self.assertEqual(sleeps, [0.1, 0.2, 0.25], "100 → 200 → 封顶 250")
        sleeps.clear()
        v3_fallback.retry_akshare(always, attempts=3, base_ms=100, max_ms=8000,
                                  sleep=sleeps.append, rand=lambda: 0.5)
        self.assertEqual(sleeps, [0.112, 0.225], "叠加 [0,25%) 抖动（毫秒取整）")

    def test_ms_comes_from_injected_clock(self):
        def always():
            raise TimeoutError("t")

        _value, meta = v3_fallback.retry_akshare(always, attempts=2, base_ms=10,
                                                sleep=lambda _s: None, clock=TickingClock(0.25),
                                                rand=lambda: 0.0)
        self.assertEqual([item["ms"] for item in meta], [250, 250])

    def test_empty_result_is_returned_without_retry(self):
        calls = []

        def empty():
            calls.append(1)
            return []

        value, meta = v3_fallback.retry_akshare(empty, sleep=lambda _s: None)
        self.assertEqual(value, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(meta), 1)
        self.assertTrue(meta[0]["ok"])
        self.assertTrue(meta[0]["empty"])

    def test_http_status_classification(self):
        import urllib.error

        def http(code):
            return urllib.error.HTTPError("http://x", code, "err", {}, None)

        self.assertTrue(v3_fallback.is_retryable_akshare(http(503)))
        self.assertTrue(v3_fallback.is_retryable_akshare(remote_disconnected()))
        self.assertTrue(v3_fallback.is_retryable_akshare(TimeoutError("t")))
        self.assertFalse(v3_fallback.is_retryable_akshare(http(404)), "4xx 是业务错误，不重试")
        self.assertFalse(v3_fallback.is_retryable_akshare(ValueError("bad")))
        self.assertFalse(v3_fallback.is_retryable_akshare(None))
        self.assertEqual(v3_fallback.RETRYABLE_ERROR_NAMES & {"ConnectionError", "TimeoutError"},
                         {"ConnectionError", "TimeoutError"})

    def test_budget_stops_retrying_without_dishonest_success(self):
        calls = []

        def slow():
            calls.append(1)
            raise TimeoutError("slow timeout")

        value, meta = v3_fallback.retry_akshare(slow, attempts=5, base_ms=10,
                                               sleep=lambda _s: None, clock=TickingClock(0.3),
                                               rand=lambda: 0.0, budget_ms=250)
        self.assertIsNone(value)
        self.assertEqual(len(calls), 1, "已花时间达到预算 → 不再重试")
        self.assertEqual(meta[-1]["stopped"], "budget")
        self.assertEqual(meta[-1]["budget_ms"], 250)
        # 没有预算时同样的调用会一直试到 attempts 次
        _value, meta_all = v3_fallback.retry_akshare(slow, attempts=3, base_ms=10,
                                                    sleep=lambda _s: None, clock=TickingClock(0.3),
                                                    rand=lambda: 0.0)
        self.assertEqual(len(meta_all), 3)

    def test_defaults_match_the_contract(self):
        self.assertEqual(v3_fallback.AKSHARE_RETRY_ATTEMPTS, 3)
        self.assertEqual(v3_fallback.AKSHARE_RETRY_BASE_MS, 1200)
        self.assertEqual(v3_fallback.AKSHARE_RETRY_MAX_MS, 8000)


# ── /api/v3/spot 多接口降级（2026-09-21）────────────────────────────────────────


def spot_retry(**overrides):
    """注入式重试参数：不真等、退避确定（单测断言用）。"""
    options = {"attempts": 2, "base_ms": 10, "sleep": lambda _s: None, "rand": lambda: 0.0}
    options.update(overrides)
    return options


class FakeSpotAkshare:
    """假 akshare 现货面：每个接口一份「脚本」（列表按序消费，最后一个重复）。

    脚本项是异常 → 抛；否则作为返回值。未在 ``responses`` 里出现的接口=该版本没有这个函数。
    """

    def __init__(self, responses=None):
        self.responses = dict(responses or {})
        self.calls = []
        for name in self.responses:
            setattr(self, name, self._make(name))

    def _make(self, name):
        queue = list(self.responses[name])

        def func(*_args, **_kwargs):
            self.calls.append(name)
            step = queue[0] if len(queue) == 1 else queue.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step

        return func

    def calls_of(self, name):
        return [item for item in self.calls if item == name]


SINA_ROWS = [{"代码": "sh600519", "名称": "贵州茅台", "最新价": 1500.5, "涨跌幅": -1.25,
              "换手率": 0.42, "市盈率": 22.3, "市净率": 7.8}]


class SpotFallbackTests(BlockRealNetwork):
    def build_spot(self, responses, **deps_kwargs):
        ak = FakeSpotAkshare(responses)
        deps_kwargs.setdefault("akshare_retry", spot_retry())
        self.build(v3_sources.Deps(akshare=ak, home=self.tmp, **deps_kwargs))
        self.ak = ak
        return ak

    def test_first_interface_failing_falls_back_in_declared_order(self):
        self.build_spot({"stock_zh_a_spot_em": [remote_disconnected()],
                         "stock_sh_a_spot_em": [[{"代码": "600519", "名称": "贵州茅台",
                                                  "最新价": 1500.5, "涨跌幅": -1.25,
                                                  "换手率": 0.42, "量比": 1.08,
                                                  "市盈率-动态": 22.3, "市净率": 7.8}]]})
        payload = self.call("/api/v3/spot", limit=10)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["source"], "akshare/stock_sh_a_spot_em")
        self.assertEqual(payload["used_source"], "akshare/stock_sh_a_spot_em")
        self.assertEqual([item["source"] for item in payload["chain"]],
                         ["akshare/stock_zh_a_spot_em", "akshare/stock_sh_a_spot_em"])
        self.assertFalse(payload["chain"][0]["ok"])
        self.assertTrue(payload["chain"][1]["ok"])
        # 第一个接口重试了 2 次（attempts=2），明细进 chain[0].attempts
        self.assertEqual(self.ak.calls_of("stock_zh_a_spot_em"), ["stock_zh_a_spot_em"] * 2)
        self.assertEqual(len(payload["chain"][0]["attempts"]), 2)
        self.assertIn("RemoteDisconnected", payload["chain"][0]["error"]["message"])
        self.assertEqual(payload["rows"][0]["code"], "600519")
        self.assertEqual(payload["rows"][0]["pe"], 22.3)
        self.assertEqual(self.ak.calls_of("stock_zh_a_spot"), [], "慢接口不该被白试")

    def test_sina_rows_are_normalized_and_the_slow_interface_goes_last(self):
        # 只有新浪接口存在（其余版本里没有）→ 命中最后一级，代码归一 + 列名回落
        self.build_spot({"stock_zh_a_spot": [SINA_ROWS]})
        payload = self.call("/api/v3/spot", limit=10)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["source"], "akshare/stock_zh_a_spot")
        self.assertEqual(payload["market_scope"], "A股全市场（新浪，分页接口，最慢）")
        self.assertEqual([spec["source"] for spec in v3_sources.AKSHARE_SPOT_CHAIN][-1],
                         "akshare/stock_zh_a_spot", "分页慢接口必须排在链尾")
        self.assertEqual(payload["rows"][0]["code"], "600519", "sh600519 → 600519")
        self.assertEqual(payload["rows"][0]["pe"], 22.3, "市盈率 列名回落")
        self.assertIsNone(payload["rows"][0]["volume_ratio"], "新浪没有量比 → null，不补 0")

    def test_partial_market_fallback_is_labelled_not_mistaken_for_full_market(self):
        self.build_spot({"stock_zh_a_spot_em": [remote_disconnected()],
                         "stock_sh_a_spot_em": [remote_disconnected()],
                         "stock_sz_a_spot_em": [[{"代码": "000001", "名称": "平安银行",
                                                  "最新价": 10.5}]]})
        payload = self.call("/api/v3/spot", limit=5)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["source"], "akshare/stock_sz_a_spot_em")
        self.assertEqual(payload["market_scope"], "深市（分市场接口）")
        self.assertIn("只覆盖该市场", payload["scope_note"])
        self.assertEqual([item["source"] for item in payload["chain"]],
                         ["akshare/stock_zh_a_spot_em", "akshare/stock_sh_a_spot_em",
                          "akshare/stock_sz_a_spot_em"])

    def test_all_interfaces_fail_reports_every_real_error(self):
        self.build_spot({"stock_zh_a_spot_em": [remote_disconnected()],
                         "stock_zh_a_spot": [remote_disconnected()]})
        payload = self.call("/api/v3/spot", limit=10)
        detail = self.assert_error(payload, "akshare/")
        self.assertEqual(detail["code"], "akshare/stock_zh_a_spot_em")
        self.assertIn("RemoteDisconnected", detail["message"])
        self.assertIn("降级链", detail["message"])
        self.assertNotIn("rows", payload)
        self.assertEqual([item["source"] for item in payload["chain"]],
                         [spec["source"] for spec in v3_sources.AKSHARE_SPOT_CHAIN])
        self.assertEqual(payload["error"]["tried"],
                         [spec["source"] for spec in v3_sources.AKSHARE_SPOT_CHAIN])
        # 版本里没有的接口如实记 missing-func，而不是记成「也失败了」
        self.assertEqual(payload["chain"][2]["error"]["code"], "akshare/missing-func")
        self.assertEqual(payload["chain"][0]["attempts"][0]["attempt"], 1)
        self.assertTrue(payload["as_of"])

    def test_business_error_is_not_retried_and_next_interface_is_tried(self):
        self.build_spot({"stock_zh_a_spot_em": [ValueError("symbol 参数非法")],
                         "stock_sh_a_spot_em": [[{"代码": "600519", "名称": "贵州茅台"}]]})
        payload = self.call("/api/v3/spot", limit=5)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(self.ak.calls_of("stock_zh_a_spot_em"), ["stock_zh_a_spot_em"],
                         "业务错误不得重试")
        self.assertIn("业务错误（不重试）", payload["chain"][0]["error"]["message"])

    def test_budget_marks_remaining_interfaces_as_skipped(self):
        # 保留真实 sleep（base_ms=50）→ 第一个接口就耗掉 1ms 预算，后续接口应记 skipped。
        self.build_spot({"stock_zh_a_spot_em": [remote_disconnected()]},
                        akshare_retry={"attempts": 2, "base_ms": 50, "rand": lambda: 0.0},
                        env={v3_sources.SPOT_CHAIN_BUDGET_ENV: "1"})
        payload = self.call("/api/v3/spot", limit=5)
        self.assertFalse(payload["ok"])
        skips = payload["chain"][1:]
        self.assertTrue(skips, payload["chain"])
        for item in skips:
            self.assertTrue(item["skipped"], item)
            self.assertEqual(item["error"]["code"], "chain/timeout")
        self.assertEqual(self.ak.calls_of("stock_zh_a_spot"), [],
                         "预算耗尽后不得再试后面的接口")

    def test_success_payload_keeps_contract_and_adds_attempts(self):
        self.build_spot({"stock_zh_a_spot_em": [[{"代码": "600519", "名称": "贵州茅台",
                                                  "最新价": 1500.5, "涨跌幅": -1.25,
                                                  "换手率": 0.42, "量比": 1.08,
                                                  "市盈率-动态": 22.3, "市净率": 7.8}]]})
        payload = self.call("/api/v3/spot", limit=20)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["source"], "akshare/stock_zh_a_spot_em")
        self.assertEqual(payload["market_scope"], "A股全市场")
        self.assertEqual(sorted(payload["rows"][0]),
                         ["change_pct", "code", "name", "pb", "pe", "price",
                          "turnover_rate", "volume_ratio"])
        self.assertEqual([item["attempt"] for item in payload["attempts"]], [1])
        self.assertEqual(self.ak.calls_of("stock_zh_a_spot_em"), ["stock_zh_a_spot_em"])


class NewsRetryTests(BlockRealNetwork):
    def test_news_retries_and_exposes_attempts(self):
        class FlakyNews:
            def __init__(self):
                self.calls = []
                self.queue = [remote_disconnected(), [{"新闻标题": "贵州茅台公告",
                                                       "新闻内容": "正文",
                                                       "发布时间": "2026-09-18 09:00:00",
                                                       "文章来源": "测试源",
                                                       "新闻链接": "http://e.invalid/1",
                                                       "关键词": "600519"}]]

            def stock_news_em(self, symbol=None):
                self.calls.append(symbol)
                step = self.queue[0] if len(self.queue) == 1 else self.queue.pop(0)
                if isinstance(step, BaseException):
                    raise step
                return step

        ak = FlakyNews()
        self.build(v3_sources.Deps(akshare=ak, home=self.tmp, akshare_retry=spot_retry()))
        payload = self.call("/api/v3/news", symbol="SH.600519", limit=10)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["source"], "akshare/stock_news_em")
        self.assertEqual(ak.calls, ["600519", "600519"], "第一次断连 → 重试一次后成功")
        self.assertEqual([item["attempt"] for item in payload["attempts"]], [1, 2])
        self.assertTrue(payload["attempts"][1]["ok"])
        self.assertEqual(payload["rows"][0]["title"], "贵州茅台公告")
        self.assertEqual(payload["rows"][0]["keyword"], "600519")

    def test_news_failure_carries_attempts_detail(self):
        ak = FakeSpotAkshare({"stock_news_em": [remote_disconnected()]})
        self.build(v3_sources.Deps(akshare=ak, home=self.tmp,
                                   akshare_retry=spot_retry(attempts=3, base_ms=100)))
        payload = self.call("/api/v3/news", symbol="600519", limit=10)
        detail = self.assert_error(payload, "akshare/")
        self.assertEqual(detail["code"], "akshare/stock_news_em")
        self.assertIn("尝试 3 次仍失败", detail["message"])
        self.assertIn("RemoteDisconnected", detail["message"])
        self.assertEqual(len(detail["attempts"]), 3)
        self.assertEqual([item["wait_ms"] for item in detail["attempts"][:2]], [100, 200])
        self.assertEqual(ak.calls_of("stock_news_em"), ["stock_news_em"] * 3)


if __name__ == "__main__":
    unittest.main()
