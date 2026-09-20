"""``server.v3_fallback`` 契约测试：**离线**、注入替身与假时钟。

覆盖点（每条都对应一个「不吞异常、不编造成功」的承诺）:
  * ``run_chain``：命中即停、失败继续、全失败 ``(None, None, attempts)``；
  * 每一级抛出的异常原文进 ``attempts``（``chain/exception``）；空结果视同失败（``chain/empty``）；
    带 ``ok=false`` 的返回保留**上游错误码**原文；
  * 墙钟预算耗尽 → 剩余源记 ``skipped=true`` + ``chain/timeout``（不假装试过）；
  * 链元素形态非法 → ``TypeError``（编程错误不得伪装成上游失败）；
  * ``attempts_chain`` / ``describe_attempts`` / ``last_error``；
  * ``probe_chains``：主源命中/降级命中/全失败的三种可用性，且**只调只读工具**；
  * ``register``：``/api/v3/sources/status`` 的信封、``keys`` 过滤、
    探测器抛错/返回非列表 → 错误信封。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_fallback -v``
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from server import v3_fallback, v3_sources  # noqa: E402

#: 只读工具白名单：探测期间**任何**写/交易工具名出现即失败（本模块的硬性纪律）
READ_ONLY_TOOLS = {
    "series", "quote_history_kline_v2", "market_snapshot", "f10_detail", "info_search",
    "info_owner_plate", "plate_stock", "plate_list", "orders_history", "deals_history",
    "snapshot", "equity", "plan", "risk", "sources",
}


class FakeApp:
    def __init__(self):
        self.routes = {}
        self.state = types.SimpleNamespace()

    def get(self, path):
        def decorator(func):
            self.routes[path] = func
            return func

        return decorator


class FakeV3Run:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, dict(payload or {})))
        value = self.values.get(name)
        if value is None:
            return {"ok": False, "error": {"code": "test/unrouted", "message": f"未预置 {name}"}}
        return value

    @property
    def names(self):
        return [name for name, _payload in self.calls]


class FakeClock:
    """可推进的假时钟：每次读返回 ``step`` 秒后的时间，用于精确触发链预算。"""

    def __init__(self, step=0.0):
        self.now = 0.0
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


class FakeAkshare:
    """假 akshare：默认每个函数都返回「上游断连」，用来验证降级失败也被如实记录。"""

    def stock_zh_a_hist(self, **kwargs):
        raise ConnectionError("RemoteDisconnected('Remote end closed connection without response')")

    def stock_zh_a_spot_em(self):
        raise ConnectionError("RemoteDisconnected('Remote end closed connection without response')")

    def stock_financial_abstract(self, symbol=None):
        raise ConnectionError("RemoteDisconnected")

    def stock_news_em(self, symbol=None):
        return [{"新闻标题": "测试资讯", "新闻内容": "正文", "发布时间": "2026-09-18 09:00:00",
                 "文章来源": "测试源", "新闻链接": "http://example.invalid/1", "关键词": symbol}]


# ── run_chain ─────────────────────────────────────────────────────────────────


class RunChainTests(unittest.TestCase):
    def test_first_success_stops_the_chain(self):
        calls = []

        def first():
            calls.append("first")
            return {"ok": True, "value": 1}

        def second():
            calls.append("second")
            return {"ok": True, "value": 2}

        value, used, attempts = v3_fallback.run_chain([("a", first), ("b", second)])
        self.assertEqual(value["value"], 1)
        self.assertEqual(used, "a")
        self.assertEqual(calls, ["first"], "命中后不得再试后面的源")
        self.assertEqual(attempts, [{"source": "a", "ok": True, "ms": attempts[0]["ms"]}])
        self.assertGreaterEqual(attempts[0]["ms"], 0)

    def test_failure_falls_through_and_records_upstream_code(self):
        def bad():
            return {"ok": False, "error": {"code": "futu/errcode-9", "message": "realtime quote permission required"}}

        def good():
            return {"ok": True, "value": "bars"}

        value, used, attempts = v3_fallback.run_chain([("primary", bad), ("fallback", good)])
        self.assertEqual(used, "fallback")
        self.assertEqual(value["value"], "bars", "run_chain 原样返回命中源的结果，不改写")
        self.assertFalse(attempts[0]["ok"])
        self.assertEqual(attempts[0]["error"]["code"], "futu/errcode-9")
        self.assertIn("realtime quote permission required", attempts[0]["error"]["message"])
        self.assertTrue(attempts[1]["ok"])

    def test_all_failed_returns_none_none_and_full_timeline(self):
        def boom():
            raise ValueError("上游炸了")

        def empty():
            return []

        value, used, attempts = v3_fallback.run_chain([("boom", boom), ("empty", empty)])
        self.assertIsNone(value)
        self.assertIsNone(used)
        self.assertEqual([item["source"] for item in attempts], ["boom", "empty"])
        self.assertEqual(attempts[0]["error"]["code"], "chain/exception")
        self.assertIn("ValueError", attempts[0]["error"]["message"])
        self.assertIn("上游炸了", attempts[0]["error"]["message"])
        self.assertEqual(attempts[1]["error"]["code"], "chain/empty")

    def test_none_and_empty_results_are_failures(self):
        self.assertIsNotNone(v3_fallback.failure_of(None))
        self.assertIsNotNone(v3_fallback.failure_of([]))
        self.assertIsNotNone(v3_fallback.failure_of(""))
        self.assertIsNotNone(v3_fallback.failure_of({"ok": False}))
        self.assertIsNone(v3_fallback.failure_of({"ok": True, "value": None}))
        self.assertIsNone(v3_fallback.failure_of({"bars": []}), "无 ok 键的非空映射按成功处理")
        self.assertIsNone(v3_fallback.failure_of([1]))

    def test_kwargs_are_forwarded(self):
        seen = {}

        def func(ticker, limit):
            seen.update({"ticker": ticker, "limit": limit})
            return {"ok": True}

        v3_fallback.run_chain([("x", func, {"ticker": "SH.600000", "limit": 3})])
        self.assertEqual(seen, {"ticker": "SH.600000", "limit": 3})

    def test_budget_exhaustion_marks_remaining_as_skipped(self):
        def slow_failure():
            return {"ok": False, "error": {"code": "x", "message": "no"}}

        clock = FakeClock(step=10.0)
        value, used, result = v3_fallback.run_chain(
            [("first", slow_failure), ("second", lambda: {"ok": True})],
            timeout=5.0, clock=clock)
        self.assertIsNone(value)
        self.assertIsNone(used)
        self.assertEqual(result[1]["source"], "second")
        self.assertTrue(result[1]["skipped"])
        self.assertEqual(result[1]["error"]["code"], "chain/timeout")
        self.assertIn("未尝试", result[1]["error"]["message"])

    def test_malformed_entry_raises_type_error(self):
        with self.assertRaises(TypeError):
            v3_fallback.run_chain([("bad", "not-callable")])
        with self.assertRaises(TypeError):
            v3_fallback.run_chain([("bad", lambda: None, ["not-a-dict"])])

    def test_describe_and_last_error(self):
        _, _, attempts = v3_fallback.run_chain([
            ("a", lambda: {"ok": False, "error": {"code": "c1", "message": "m1"}}),
            ("b", lambda: (_ for _ in ()).throw(RuntimeError("m2"))),
        ])
        text = v3_fallback.describe_attempts(attempts)
        self.assertIn("a=c1: m1", text)
        self.assertIn("b=chain/exception", text)
        self.assertEqual(v3_fallback.last_error(attempts)["code"], "chain/exception")
        self.assertEqual(v3_fallback.last_error([])["code"], "chain/all-failed")
        self.assertEqual(v3_fallback.attempts_chain(attempts), attempts)


# ── probe_chains ──────────────────────────────────────────────────────────────


SERIES_OK = {"ok": True, "value": {"ticker": "SH.600000", "source": "futu/quote_history_kline",
                                   "as_of": "2026-09-18", "bars": [{"t": "2026-09-18", "c": 9.2}]}}


def sources_deps(tmp):
    """假 akshare + 假 SEC fetch 的依赖（探测降级源时不会打网络）。"""
    return v3_sources.Deps(
        home=tmp, timeout=5.0,
        akshare=FakeAkshare(),
        fetch_json=lambda url, headers, timeout: {"ok": False, "error": {
            "code": "sec/network", "message": "offline"}},
    )


class ProbeChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v3fb-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_probe_rows_have_contract_fields_and_real_timeline(self):
        run = FakeV3Run({
            "series": SERIES_OK,
            "market_snapshot": {"ok": False, "error": {"code": "trading/futu-error",
                                                       "message": "富途业务错误（errcode=-9）：realtime quote permission required"}},
            "f10_detail": {"ok": True, "value": {"report_list": [{"item_list": [{"display_name": "a"}]}]}},
            "info_owner_plate": {"ok": True, "value": {"sectors": [{"plate_type": "INDUSTRY"}]}},
            "orders_history": {"ok": True, "value": {"groups": [{"market": "SH", "rows": [{"order_id": "1"}]}]}},
            "info_search": {"ok": False, "error": {"code": "trading/futu-unavailable",
                                                   "message": "info_search 返回的载荷不完整，已按失败处理"}},
        })
        rows = v3_fallback.probe_chains(run, self.tmp, sources_deps=sources_deps(self.tmp))
        by_key = {row["key"]: row for row in rows}
        self.assertEqual(sorted(by_key), sorted(spec["key"] for spec in v3_fallback.CHAIN_SPECS))
        for row in rows:
            for field in ("key", "label", "primary", "fallback", "available", "last_source",
                          "last_ok", "checked_at"):
                self.assertIn(field, row)
        # 主源命中
        self.assertTrue(by_key["kline"]["available"])
        self.assertEqual(by_key["kline"]["last_source"], "futu/quote_history_kline")
        self.assertEqual(by_key["kline"]["rows"], 1)
        # 主源 -9 → 降级到假 akshare（也失败）→ available=false，错误原文保留
        snapshot = by_key["snapshot"]
        self.assertFalse(snapshot["available"])
        self.assertEqual([item["source"] for item in snapshot["attempts"]],
                         ["futu/market_snapshot", "akshare/stock_zh_a_spot_em"])
        self.assertIn("-9", snapshot["attempts"][0]["error"]["message"])
        self.assertIn("RemoteDisconnected", snapshot["attempts"][1]["error"]["message"])
        self.assertEqual(snapshot["error"]["code"], "akshare/stock_zh_a_spot_em")
        # 资讯：富途失败 → 假 akshare 命中（真实降级路径）
        news = by_key["news"]
        self.assertTrue(news["available"])
        self.assertEqual(news["last_source"], "akshare/stock_news_em")
        # 成交质量没有开源替代：链只有一级
        self.assertEqual(by_key["quality"]["chain_size"], 1)
        self.assertEqual(by_key["quality"]["fallback"], "")
        # 美股财务：SEC 假 fetch 失败 → openbb 是重依赖，如实记「未探测」而不是硬等 44~57s
        us = by_key["financials_us"]
        self.assertFalse(us["available"])
        self.assertEqual([item["source"] for item in us["attempts"]],
                         ["sec/companyconcept(us-gaap XBRL)", "openbb/equity.fundamental"])
        self.assertEqual(us["attempts"][1]["error"]["code"], "probe/skipped-heavy")
        self.assertIn("44~57s", us["attempts"][1]["error"]["message"])
        # 探测只碰只读工具
        self.assertTrue(set(run.names) <= READ_ONLY_TOOLS, f"探测了非只读工具：{run.names}")

    def test_probe_keys_filter(self):
        run = FakeV3Run({"series": SERIES_OK})
        rows = v3_fallback.probe_chains(run, self.tmp, keys=["kline"], sources_deps=sources_deps(self.tmp))
        self.assertEqual([row["key"] for row in rows], ["kline"])

    def test_probe_reports_shape_mismatch_as_failure(self):
        run = FakeV3Run({"series": {"ok": True, "value": {"bars": []}}})
        rows = v3_fallback.probe_chains(run, self.tmp, keys=["kline"],
                                        sources_deps=sources_deps(self.tmp))
        kline = rows[0]
        self.assertFalse(kline["available"], "空 bars 不能算可用")
        self.assertEqual(kline["attempts"][0]["error"]["code"], "probe/no-data")


# ── register ──────────────────────────────────────────────────────────────────


class RegisterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v3fb-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_route_returns_chains_and_note(self):
        app = FakeApp()
        fake_rows = [{"key": "kline", "label": "K 线/历史行情", "primary": "futu/x",
                      "fallback": "akshare/y", "available": True, "last_source": "futu/x",
                      "last_ok": True, "checked_at": "2026-09-20T00:00:00Z", "error": None}]
        v3_fallback.register(app, FakeV3Run(), self.tmp, deps={"probe": lambda keys: fake_rows})
        self.assertEqual(list(app.routes), ["/api/v3/sources/status"])
        payload = asyncio.run(app.routes["/api/v3/sources/status"](keys=""))
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["chains"], fake_rows)
        self.assertIn("不返回占位数据", payload["note"])
        self.assertTrue(payload["as_of"])

    def test_route_passes_keys_filter(self):
        seen = {}

        def probe(keys):
            seen["keys"] = keys
            return []

        app = FakeApp()
        v3_fallback.register(app, FakeV3Run(), self.tmp, deps={"probe": probe})
        asyncio.run(app.routes["/api/v3/sources/status"](keys="kline, news ,"))
        self.assertEqual(seen["keys"], ["kline", "news"])

    def test_route_error_envelopes(self):
        app = FakeApp()
        v3_fallback.register(app, FakeV3Run(), self.tmp,
                             deps={"probe": lambda keys: (_ for _ in ()).throw(RuntimeError("boom"))})
        payload = asyncio.run(app.routes["/api/v3/sources/status"](keys=""))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "sources/internal")
        self.assertIn("boom", payload["error"]["message"])

        app2 = FakeApp()
        v3_fallback.register(app2, FakeV3Run(), self.tmp, deps={"probe": lambda keys: "not-a-list"})
        payload = asyncio.run(app2.routes["/api/v3/sources/status"](keys=""))
        self.assertEqual(payload["error"]["code"], "sources/bad-probe")


class SpecTests(unittest.TestCase):
    def test_chain_specs_are_complete_and_unique(self):
        keys = [spec["key"] for spec in v3_fallback.CHAIN_SPECS]
        self.assertEqual(len(keys), len(set(keys)))
        expected = {"kline", "snapshot", "financials_cn", "financials_us", "news",
                    "industry", "quality", "spot"}
        self.assertEqual(set(keys), expected)
        for spec in v3_fallback.CHAIN_SPECS:
            self.assertTrue(spec["primary"], spec["key"])
            self.assertTrue(spec["label"], spec["key"])
        quality = v3_fallback.SPEC_BY_KEY["quality"]
        self.assertEqual(quality["fallback"], "", "成交质量没有开源替代 → 降级源必须为空")

    def test_every_chain_has_a_probe(self):
        probes = v3_fallback.build_probes(FakeV3Run(), None)
        for spec in v3_fallback.CHAIN_SPECS:
            self.assertIn(spec["key"], probes)
            self.assertTrue(probes[spec["key"]], spec["key"])


if __name__ == "__main__":
    unittest.main()
