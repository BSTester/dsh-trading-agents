"""``server.v3_industry`` 契约测试：**离线**、注入假 ``v3_run`` 与临时 home。

覆盖点（每条都对应一个「不猜行业、不臆造权重」的承诺）:
  * 行业取 ``info_owner_plate`` 的 ``plate_type=INDUSTRY``，名称优先中文 ``plate_sc_name``；
  * 上游没标 ``plate_type`` 时用 ``plate_list(INDUSTRY)`` 代码集求交集（兜底 A）；
  * ``info_owner_plate`` 整体失败（限频 -12006）→ **重试一次** → 仍失败则 ``plate_stock``
    反查（兜底 B，有扫描上限）；取不到 → 进 ``missing`` 并写明真实原因；
  * 权重：``plan`` 的 frozen 计划优先，其次自选池等权；显式 ``tickers`` 覆盖标的集，
    不在组合里的标的 weightPct 记 0（**不臆造权重**）；
  * ``exposures`` 按行业聚合、``breach = top.weightPct > limit_pct``、
    ``value = 权重 × equity.current``（权益取不到 → ``value=null``）；
  * ``market`` 过滤不跨市场合并；无组合/无映射/坏 limit → 错误信封；
  * ``register`` 只挂 1 条路由。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_industry -v``
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from server import v3_industry  # noqa: E402


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
    """记录型 ``v3_run``：``values`` 可以给「信封」或「按调用次序返回的信封列表」。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, dict(payload or {})))
        value = self.values.get(name)
        if value is None:
            return {"ok": False, "error": {"code": "test/unrouted", "message": f"未预置 {name}"}}
        if isinstance(value, list):
            index = min(sum(1 for call in self.calls if call[0] == name) - 1, len(value) - 1)
            return value[index]
        if callable(value):
            return value(dict(payload or {}))
        return value

    def payloads(self, name):
        return [payload for tool, payload in self.calls if tool == name]


OWNER_PLATE = {
    "ok": True,
    "value": {"sectors": [
        {"plate_code": "SH.LIST0439", "plate_type": "CONCEPT", "plate_sc_name": "沪股通",
         "sc_name": "浦发银行"},
        {"plate_code": "SH.LIST0949", "plate_type": "INDUSTRY", "plate_sc_name": "银行",
         "plate_name": "Joint Stock Banks", "sc_name": "浦发银行"},
    ]},
}
OWNER_PLATE_NO_INDUSTRY_TYPE = {
    "ok": True,
    "value": {"sectors": [
        {"plate_code": "SH.LIST0939", "plate_type": "ALL", "plate_sc_name": "航空机场",
         "plate_name": "Airports", "sc_name": "上海机场"},
        {"plate_code": "SH.LIST0439", "plate_type": "CONCEPT", "plate_sc_name": "沪股通",
         "sc_name": "上海机场"},
    ]},
}
RATE_LIMITED = {"ok": False, "error": {"code": "trading/futu-unavailable",
                                       "message": "非预期响应（HTTP 403）：b'{\"code\":-12006}'"}}
PLATE_LIST_SH = {"ok": True, "value": {"plate_list": [
    {"code": "SH.LIST0939", "sc_name": "航空机场", "plate_name": "Airports"},
    {"code": "SH.LIST0918", "sc_name": "普钢", "plate_name": "Steel"},
    {"code": "SH.LIST0949", "sc_name": "银行", "plate_name": "Banks"},
]}}


def watchlist_home(tmp, tickers):
    with open(os.path.join(tmp, "trading-platform.json"), "w", encoding="utf-8") as handle:
        json.dump({"watchlist": list(tickers)}, handle)


class IndustryMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v3ind-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        watchlist_home(self.tmp, ["SH.600000", "SH.600009", "SH.600010"])

    def _run(self, values, **kwargs):
        return v3_industry.industry_exposure(
            FakeV3Run(values), self.tmp, nav=kwargs.pop("nav", 900000.0), sleep=lambda _s: None,
            **kwargs)

    def test_mapping_uses_industry_plate_and_chinese_name(self):
        payload = self._run({
            "plan": {"ok": True, "value": {"plans": []}},
            "info_owner_plate": OWNER_PLATE,
        })
        self.assertTrue(payload["ok"], payload)
        entry = payload["mapping"]["SH.600000"]
        self.assertEqual(entry["industry"], "银行", "板块名优先中文 plate_sc_name")
        self.assertEqual(entry["plates"], ["SH.LIST0949"], "只把行业板块进 plates")
        self.assertIn("SH.LIST0439", entry["allPlates"])
        self.assertEqual(payload["sources"]["plate"], "futu/info_owner_plate")
        self.assertIn("自选池等权", payload["sources"]["weights"])

    def test_weights_are_equal_and_value_uses_equity(self):
        payload = self._run({
            "plan": {"ok": True, "value": {"plans": []}},
            "info_owner_plate": OWNER_PLATE,
        })
        self.assertEqual(payload["universe"], ["SH.600000", "SH.600009", "SH.600010"])
        top = payload["top"]
        self.assertEqual(top["industry"], "银行", "三只标的同属银行 → 聚合后 100%")
        self.assertAlmostEqual(top["weightPct"], 100.0, places=4)
        exposure = payload["exposures"][0]
        self.assertEqual(exposure["tickers"], ["SH.600000", "SH.600009", "SH.600010"])
        self.assertAlmostEqual(exposure["value"], 900000.0, places=2)

    def test_breach_when_top_exceeds_limit(self):
        values = {"plan": {"ok": True, "value": {"plans": []}}, "info_owner_plate": OWNER_PLATE}
        self.assertTrue(self._run(values, limit_pct=20)["breach"], "100% > 20% → 超限")
        relaxed = self._run(values, limit_pct=100)
        self.assertFalse(relaxed["breach"], "100% 不 > 100% → 未超限（严格大于）")

    def test_frozen_plan_weights_win_over_watchlist(self):
        payload = self._run({
            "plan": {"ok": True, "value": {"plans": [{
                "plan_id": "P-1", "status": "frozen",
                "target": {"SH.600000": 0.5, "SH.600009": 0.5}}]}},
            "info_owner_plate": OWNER_PLATE,
        })
        self.assertIn("frozen 计划 P-1", payload["sources"]["weights"])
        self.assertEqual(payload["universe"], ["SH.600000", "SH.600009"])
        self.assertAlmostEqual(payload["top"]["weightPct"], 100.0, places=4)

    def test_plate_list_intersection_when_plate_type_missing(self):
        payload = self._run({
            "plan": {"ok": True, "value": {"plans": []}},
            "info_owner_plate": OWNER_PLATE_NO_INDUSTRY_TYPE,
            "plate_list": PLATE_LIST_SH,
        })
        entry = payload["mapping"]["SH.600000"]
        self.assertEqual(entry["industry"], "航空机场")
        self.assertEqual(entry["plates"], ["SH.LIST0939"])
        self.assertEqual(payload["sources"]["plate"], "futu/info_owner_plate")
        self.assertTrue(any("求交集" in note for note in payload["notes"]), payload["notes"])

    def test_rate_limit_is_retried_then_plate_stock_reverse_lookup(self):
        def plate_stock(payload):
            # 只有「普钢」板块真的含有 SH.600010（反查必须逐板块比对成分股）
            if payload.get("plate_code") == "SH.LIST0918":
                return {"ok": True, "value": {"stock_list": [{"code": "SH.600010"}]}}
            return {"ok": True, "value": {"stock_list": []}}

        run = FakeV3Run({
            "plan": {"ok": True, "value": {"plans": []}},
            "info_owner_plate": [
                RATE_LIMITED,                                            # 首次：限频
                {"ok": True, "value": {"sectors": []}},                   # 重试：成功但无板块
            ],
            "plate_list": PLATE_LIST_SH,
            "plate_stock": plate_stock,
        })
        payload = v3_industry.industry_exposure(run, self.tmp, nav=900000.0, sleep=lambda _s: None)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["mapping"]["SH.600010"]["industry"], "普钢",
                         "plate_stock 反查必须给出真实板块名（成分股命中才算）")
        self.assertEqual(payload["sources"]["plate"], "futu/plate_stock")
        self.assertEqual(sorted(payload["missing"][0]), ["reason", "ticker"])
        self.assertEqual(len(run.payloads("info_owner_plate")), 4,
                         "3 只标的各 1 次 + 1 次限频重试")
        self.assertEqual(sorted(run.payloads("plate_stock")[0]), ["limit", "plate_code"])

    def test_rate_limit_exhausted_reports_upstream_error_in_reason(self):
        run = FakeV3Run({
            "plan": {"ok": True, "value": {"plans": []}},
            "info_owner_plate": RATE_LIMITED,
            "plate_list": PLATE_LIST_SH,
            "plate_stock": {"ok": True, "value": {"stock_list": []}},
        })
        payload = v3_industry.industry_exposure(run, self.tmp, nav=900000.0, sleep=lambda _s: None)
        self.assertFalse(payload["ok"], payload)
        reason = payload["error"]["missing"][0]["reason"]
        self.assertIn("-12006", reason, "真实上游错误必须写进 reason")
        self.assertIn("反查", reason)

    def test_reverse_lookup_scan_limit_is_disclosed_as_missing(self):
        payload = self._run({
            "plan": {"ok": True, "value": {"plans": []}},
            "info_owner_plate": {"ok": False, "error": {"code": "trading/futu-unavailable",
                                                        "message": "HTTP 403"}},
            "plate_list": PLATE_LIST_SH,
            "plate_stock": {"ok": True, "value": {"stock_list": [{"code": "SH.999999"}]}},
        }, plate_scan_limit=1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "industry/no-mapping")
        reasons = [item["reason"] for item in payload["error"]["missing"]]
        self.assertTrue(any("扫描上限" in reason for reason in reasons), reasons)

    def test_missing_reason_is_the_real_upstream_error(self):
        payload = self._run({
            "plan": {"ok": True, "value": {"plans": []}},
            "info_owner_plate": {"ok": True, "value": {"sectors": [
                {"plate_code": "SH.LIST0439", "plate_type": "CONCEPT", "plate_sc_name": "沪股通"}]}},
            "plate_list": PLATE_LIST_SH,
        })
        self.assertFalse(payload["ok"], "全部标的都没行业 → 不返回成功空壳")
        reason = payload["error"]["missing"][0]["reason"]
        self.assertIn("未返回所属行业板块", reason)
        self.assertIn("SH.LIST0439", reason)

    def test_explicit_tickers_override_universe_and_zero_weight_is_not_invented(self):
        payload = self._run({
            "plan": {"ok": True, "value": {"plans": []}},
            "info_owner_plate": OWNER_PLATE,
        }, tickers_raw="SH.600000,US.NVDA")
        self.assertEqual(payload["universe"], ["SH.600000", "US.NVDA"])
        self.assertAlmostEqual(payload["mapping"]["SH.600000"]["weight"], 1 / 3, places=6)
        self.assertEqual(payload["mapping"]["US.NVDA"]["weight"], 0.0)
        self.assertTrue(any("没有对应" in note for note in payload["notes"]))

    def test_market_filter_does_not_merge_markets(self):
        payload = self._run({
            "plan": {"ok": True, "value": {"plans": []}},
            "info_owner_plate": OWNER_PLATE,
        }, market="US")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "industry/no-universe")

    def test_nav_missing_leaves_value_null(self):
        payload = v3_industry.industry_exposure(
            FakeV3Run({"plan": {"ok": True, "value": {"plans": []}},
                       "info_owner_plate": OWNER_PLATE}),
            self.tmp, sleep=lambda _s: None)
        self.assertTrue(payload["ok"], payload)
        self.assertIsNone(payload["exposures"][0]["value"], "权益取不到 → value=null，不估算")
        self.assertIsNone(payload["sources"]["nav"])
        self.assertTrue(any("equity.current 取不到" in note for note in payload["notes"]))

    def test_equity_tool_is_used_for_nav(self):
        run = FakeV3Run({"plan": {"ok": True, "value": {"plans": []}},
                         "equity": {"ok": True, "value": {"current": 500000.0}},
                         "info_owner_plate": OWNER_PLATE})
        payload = v3_industry.industry_exposure(run, self.tmp, sleep=lambda _s: None)
        self.assertAlmostEqual(payload["exposures"][0]["value"], 500000.0, places=2)
        self.assertEqual(run.payloads("equity"), [{"window": 30}])


class ErrorBranchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="v3ind-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_no_portfolio(self):
        watchlist_home(self.tmp, [])
        payload = v3_industry.industry_exposure(
            FakeV3Run({"plan": {"ok": True, "value": {"plans": []}}}), self.tmp, sleep=lambda _s: None)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "industry/no-portfolio")
        self.assertIn("自选池", payload["error"]["message"])

    def test_bad_limit_and_bad_market(self):
        watchlist_home(self.tmp, ["SH.600000"])
        payload = v3_industry.industry_exposure(FakeV3Run(), self.tmp, limit_pct="abc")
        self.assertEqual(payload["error"]["code"], "industry/bad-limit")
        payload = v3_industry.industry_exposure(FakeV3Run(), self.tmp, limit_pct=150)
        self.assertEqual(payload["error"]["code"], "industry/bad-limit")
        payload = v3_industry.industry_exposure(
            FakeV3Run({"plan": {"ok": True, "value": {"plans": []}}}), self.tmp, market="MARS")
        self.assertEqual(payload["error"]["code"], "industry/bad-market")

    def test_tickers_without_market_prefix_are_kept(self):
        watchlist_home(self.tmp, ["SH.600000"])
        payload = v3_industry.industry_exposure(
            FakeV3Run({"plan": {"ok": True, "value": {"plans": []}}}), self.tmp,
            tickers_raw="裸代码", sleep=lambda _s: None)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "industry/no-mapping")
        self.assertIn("无法反查", payload["error"]["missing"][0]["reason"])


class HelperTests(unittest.TestCase):
    def test_market_of_and_lookup_weight_normalisation(self):
        self.assertEqual(v3_industry.market_of("SH.600000"), "SH")
        self.assertEqual(v3_industry.market_of("600000"), "")
        weights = {"SH.600000": 0.5, "US.NVDA": 0.25}
        self.assertEqual(v3_industry._lookup_weight(weights, "sh.600000"), 0.5)
        self.assertEqual(v3_industry._lookup_weight(weights, "600000.SH"), 0.5)
        self.assertEqual(v3_industry._lookup_weight(weights, "600519.SH"), None)

    def test_read_watchlist_tolerates_broken_file(self):
        tmp = tempfile.mkdtemp(prefix="v3ind-")
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertEqual(v3_industry.read_watchlist(tmp), [])
        with open(os.path.join(tmp, "trading-platform.json"), "w", encoding="utf-8") as handle:
            handle.write("{broken")
        self.assertEqual(v3_industry.read_watchlist(tmp), [])
        with open(os.path.join(tmp, "trading-platform.json"), "w", encoding="utf-8") as handle:
            json.dump({"watchlist": ["SH.600000", "SH.600000", ""]}, handle)
        self.assertEqual(v3_industry.read_watchlist(tmp), ["SH.600000"])


class RegisterTests(unittest.TestCase):
    def test_register_route_and_envelope(self):
        tmp = tempfile.mkdtemp(prefix="v3ind-")
        self.addCleanup(shutil.rmtree, tmp, True)
        watchlist_home(tmp, ["SH.600000"])
        app = FakeApp()
        run = FakeV3Run({"plan": {"ok": True, "value": {"plans": []}},
                         "info_owner_plate": OWNER_PLATE})
        v3_industry.register(app, run, tmp, deps={"nav": 1000000.0, "sleep": lambda _s: None})
        self.assertEqual(list(app.routes), ["/api/v3/risk/industry"])
        payload = asyncio.run(app.routes["/api/v3/risk/industry"](tickers="", market="", limit_pct=20))
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["limitPct"], 20.0)

    def test_route_bad_limit_becomes_envelope(self):
        tmp = tempfile.mkdtemp(prefix="v3ind-")
        self.addCleanup(shutil.rmtree, tmp, True)
        app = FakeApp()
        v3_industry.register(app, FakeV3Run(), tmp)
        payload = asyncio.run(app.routes["/api/v3/risk/industry"](tickers="", market="", limit_pct=-1))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "industry/bad-limit")


if __name__ == "__main__":
    unittest.main()
