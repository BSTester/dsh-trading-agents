"""``server.v3_universe`` 市场宇宙解析 + ``market=`` 端点的契约测试（**离线**）。

全部离线：注入记录型 ``wb_call``/``v3_run`` 替身、临时 ``home``，不打网络、不读真实
``~/.dsh``、不依赖运行中的 8397 服务。

覆盖点（每条都对应一个「不发明自选池」的承诺）:
  * ``market`` 归一：缺省 ``SH``、``SZ``/``BJ`` 归入 ``SH``、非法 → ``None``；
  * 标的归一：裸代码按 **A 股首位规则**分 SH/SZ/BJ、港股补零到 5 位、其余按美股；
    账户分组声明的市场 id / 链名全部复用仓库既有常量（``SIM_MARKET_IDS`` /
    ``v3_quality.MARKET_TRD_CODES``），不自造第二套映射；
  * 优先级：``watchlists.<market>`` → 旧顶层 ``watchlist``（＝SH，且**按市场过滤**）→
    真实持仓 ``positions``（数值 ``market_id`` 与链名都能识别；未登记的 market_id
    如实排除并计数）→ 都空 → ``None``；
  * ``QUOTE_UNIVERSE_TTL_MS`` TTL 缓存（成功 60s / 失败更短的负面 TTL / 0 = 关闭）；
  * ``/api/v3/market/watchlist?market=`` 的 rows 只含该市场标的、错误信封与 ``detail``；
  * ``/api/v3/research?market=`` 与 ``/research/tasks?market=`` 的过滤与计数。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_v3_universe -v``
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
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from server import v3_market, v3_research, v3_universe  # noqa: E402


class FakeApp:
    def __init__(self):
        self.routes = {}
        self.state = types.SimpleNamespace()

    def get(self, path):
        def decorator(func):
            self.routes[path] = func
            return func

        return decorator


class FakeCall:
    """记录型工具面调用器：``values`` 给信封或按载荷回放的 callable；未预置 → unknown-tool。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, dict(payload or {})))
        if name not in self.values:
            return {"ok": False, "error": {"code": "v3/unknown-tool",
                                           "message": f"未知工具 {name}"}}
        value = self.values[name]
        return value(dict(payload or {})) if callable(value) else value

    def payloads(self, name):
        return [payload for tool, payload in self.calls if tool == name]


def write_config(tmp, payload):
    with open(os.path.join(tmp, "trading-platform.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def positions_envelope(groups, source="futu/sim_trade_position_list"):
    return {"ok": True, "value": {"mode": "sim", "as_of": "2026-09-20", "groups": groups,
                                  "source": source, "errors": []}}


def bars(closes):
    return [{"t": f"2026-01-{index + 1:02d}", "c": float(close)}
            for index, close in enumerate(closes)]


# ---------------------------------------------------------------------------
# 1. 归一工具
# ---------------------------------------------------------------------------
class NormalisationTests(unittest.TestCase):
    def test_normalize_market_default_and_a_share_collapse(self):
        self.assertEqual(v3_universe.normalize_market(None), "SH")
        self.assertEqual(v3_universe.normalize_market(""), "SH")
        self.assertEqual(v3_universe.normalize_market(" sh "), "SH")
        for prefix in ("SH", "SZ", "BJ"):  # A 股三所同一口径
            self.assertEqual(v3_universe.normalize_market(prefix), "SH")
        self.assertEqual(v3_universe.normalize_market("hk"), "HK")
        self.assertEqual(v3_universe.normalize_market("US"), "US")
        for bad in ("MARS", "NASDAQ", True, 3.5):
            self.assertIsNone(v3_universe.normalize_market(bad), bad)

    def test_market_of_ticker_handles_prefix_suffix_and_boards(self):
        self.assertEqual(v3_universe.market_of_ticker("SH.600000"), "SH")
        self.assertEqual(v3_universe.market_of_ticker("SZ.002475"), "SH", "深市归入 A 股口径")
        self.assertEqual(v3_universe.market_of_ticker("600000.SH"), "SH")
        self.assertEqual(v3_universe.market_of_ticker("hk.00700"), "HK")
        self.assertEqual(v3_universe.market_of_ticker("US.NVDA"), "US")
        # 过滤口径就是**市场前缀**（与 execution/oms 规则同字面）：板块代码也按其前缀归市场
        self.assertEqual(v3_universe.market_of_ticker("SH.LIST0949"), "SH")
        self.assertIsNone(v3_universe.market_of_ticker("600000"), "裸代码没有市场信息")

    def test_canonical_ticker_uses_a_share_head_rules(self):
        self.assertEqual(v3_universe.canonical_ticker("603993"), "SH.603993")
        self.assertEqual(v3_universe.canonical_ticker("002475"), "SZ.002475",
                         "深市代码必须归一到 SZ（否则 series 取不到数）")
        self.assertEqual(v3_universe.canonical_ticker("830799"), "BJ.830799")
        self.assertEqual(v3_universe.canonical_ticker("00100"), "HK.00100")
        self.assertEqual(v3_universe.canonical_ticker("700"), "HK.00700")
        self.assertEqual(v3_universe.canonical_ticker("NVDA"), "US.NVDA")
        self.assertEqual(v3_universe.canonical_ticker("600000.SH"), "SH.600000")
        self.assertEqual(v3_universe.canonical_ticker("sh.600000"), "SH.600000")
        self.assertEqual(v3_universe.canonical_ticker("SH.LIST0949"), "SH.LIST0949",
                         "已带市场前缀的写法原样保留（是不是真标的由上游判定）")
        self.assertIsNone(v3_universe.canonical_ticker(""))
        self.assertIsNone(v3_universe.canonical_ticker("SG.XYZ"), "未知市场前缀 → 不归一")
        self.assertEqual(v3_universe.canonical_ticker("700", "HK"), "HK.00700")

    def test_market_of_account_label_reuses_the_canonical_id_table(self):
        self.assertEqual(v3_universe.market_of_account_label(1), "HK")
        self.assertEqual(v3_universe.market_of_account_label("3"), "SH", "A 股 market_id=3")
        self.assertEqual(v3_universe.market_of_account_label(100), "US")
        self.assertEqual(v3_universe.market_of_account_label("HK"), "HK")
        self.assertEqual(v3_universe.market_of_account_label("SZ"), "SH")
        for unknown in (9, "10", "16", None, "", "FUTURES", True, "MARS"):
            self.assertIsNone(v3_universe.market_of_account_label(unknown), unknown)


# ---------------------------------------------------------------------------
# 2. 配置来源
# ---------------------------------------------------------------------------
class ConfigSourceTests(unittest.TestCase):
    def setUp(self):
        v3_universe.clear_cache()
        self.tmp = tempfile.mkdtemp(prefix="v3uni-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_watchlists_key_wins_and_is_filtered(self):
        write_config(self.tmp, {"watchlists": {
            "SH": ["SH.600000", "SZ.002475", "600001"],
            "HK": ["HK.00100", "HK.00981", "US.NVDA", "HK.00100"],
        }})
        run = FakeCall()
        hk = v3_universe.resolve_universe(run, self.tmp, "HK")
        self.assertEqual(hk["market"], "HK")
        self.assertEqual(hk["tickers"], ["HK.00100", "HK.00981", "US.NVDA"][:2],
                         "按市场过滤（US 不进 HK），去重保序")
        self.assertEqual(hk["source"], "config/trading-platform.json#watchlists.HK")
        self.assertIn("共 2 只", hk["note"])
        self.assertIn("剔除 1 只", hk["note"])
        self.assertEqual(run.calls, [], "配置池命中时不该打富途（避免频控 -12006）")

        sh = v3_universe.resolve_universe(FakeCall(), self.tmp, "SH")
        self.assertEqual(sh["tickers"], ["SH.600000", "SZ.002475", "SH.600001"],
                         "SH 与 SZ 同属 A 股口径，裸 6 位代码按首位归一")
        self.assertEqual(sh["source"], "config/trading-platform.json#watchlists.SH")

    def test_legacy_top_level_watchlist_is_sh_and_still_filtered(self):
        write_config(self.tmp, {"watchlist": ["SH.600000", "HK.00100", "US.NVDA", "SH.600009"]})
        run = FakeCall()
        payload = v3_universe.resolve_universe(run, self.tmp, "SH")
        self.assertEqual(payload["tickers"], ["SH.600000", "SH.600009"])
        self.assertEqual(payload["source"], "config/trading-platform.json#watchlist")
        self.assertIn("剔除 2 只非本市场标的", payload["note"])
        self.assertEqual(run.calls, [])

        # 旧顶层键只服务 SH：HK 视图必须回到真实持仓（这里没有 → None）
        self.assertIsNone(v3_universe.resolve_universe(FakeCall(), self.tmp, "HK"))
        self.assertIn("没有 watchlists.HK", v3_universe.universe_note(self.tmp, "HK"))

    def test_empty_config_pool_falls_through_to_positions(self):
        write_config(self.tmp, {"watchlists": {"SH": ["US.NVDA"]},
                                "watchlist": ["SH.600000"]})
        run = FakeCall({"positions": positions_envelope([
            {"acc_id": "3182575", "market": 3,
             "positions": [{"symbol": "603993"}, {"symbol": "002475"}]}])})
        payload = v3_universe.resolve_universe(run, self.tmp, "SH")
        self.assertEqual(payload["tickers"], ["SH.603993", "SZ.002475"])
        self.assertEqual(payload["source"], "futu/sim_trade_position_list#SH")
        self.assertIn("watchlists.SH", payload["note"])
        self.assertIn("继续看真实持仓", payload["note"])
        self.assertEqual(run.payloads("positions"), [{}])

    def test_broken_config_is_reported_not_faked(self):
        with open(os.path.join(self.tmp, "trading-platform.json"), "w", encoding="utf-8") as handle:
            handle.write("{broken")
        self.assertIsNone(v3_universe.read_config(self.tmp))
        self.assertIsNone(v3_universe.resolve_universe(FakeCall(), self.tmp, "SH"))
        self.assertIn("没有 watchlists.SH", v3_universe.universe_note(self.tmp, "SH"))

    def test_bad_market_returns_none(self):
        self.assertIsNone(v3_universe.resolve_universe(FakeCall(), self.tmp, "MARS"))
        self.assertEqual(v3_universe.clear_cache(), None)


# ---------------------------------------------------------------------------
# 3. 真实持仓来源
# ---------------------------------------------------------------------------
class PositionsSourceTests(unittest.TestCase):
    def setUp(self):
        v3_universe.clear_cache()
        self.tmp = tempfile.mkdtemp(prefix="v3uni-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _groups(self):
        return [
            {"acc_id": "9393", "market": 1, "kind": "simulated",
             "positions": [{"symbol": "00100"}, {"symbol": "00981"}]},
            {"acc_id": "3182575", "market": 3, "kind": "simulated",
             "positions": [{"symbol": "603993"}, {"symbol": "002475"}]},
            {"acc_id": "11587526", "market": 100, "kind": "simulated",
             "positions": [{"symbol": "NVDA"}]},
            {"acc_id": "3182829", "market": 9, "kind": "simulated",
             "positions": [{"symbol": "00700"}]},
        ]

    def test_numeric_market_ids_are_mapped_per_account(self):
        run = FakeCall({"positions": positions_envelope(self._groups())})
        hk = v3_universe.resolve_universe(run, self.tmp, "HK")
        self.assertEqual(hk["tickers"], ["HK.00100", "HK.00981"],
                         "market_id=9（港期权）不猜成港股：未登记的市场标识一律排除")
        us = v3_universe.resolve_universe(run, self.tmp, "US")
        self.assertEqual(us["tickers"], ["US.NVDA"])
        self.assertIn("无法归入 SH/HK/US", us["note"], "未登记的 market_id 如实说明，不猜")

    def test_a_share_holdings_keep_their_real_exchange_prefix(self):
        run = FakeCall({"positions": positions_envelope(self._groups())})
        sh = v3_universe.resolve_universe(run, self.tmp, "SH")
        self.assertEqual(sh["tickers"], ["SH.603993", "SZ.002475"],
                         "A 股持仓按代码首位分交易所，深市不能被写成 SH.002475")
        self.assertEqual(sh["source"], "futu/sim_trade_position_list#SH")
        self.assertIn("另有 2 个账户属于其它市场", sh["note"])
        self.assertIn("1 个账户的市场标识无法归入 SH/HK/US（如 ['9']）", sh["note"])

    def test_live_groups_without_market_use_the_row_symbol(self):
        run = FakeCall({"positions": positions_envelope(
            [{"acc_id": "REAL-1", "market": None,
              "positions": [{"code": "SH.600000"}, {"code": "HK.00700"}]}],
            source="futu/account_positions")})
        hk = v3_universe.resolve_universe(run, self.tmp, "HK")
        self.assertEqual(hk["tickers"], ["HK.00700"])
        self.assertEqual(hk["source"], "futu/account_positions#HK")

    def test_positions_failure_is_reported_with_the_real_error(self):
        run = FakeCall({"positions": {"ok": False, "error": {
            "code": "trading/futu-unavailable",
            "message": "非预期响应（HTTP 403）：b'{\"code\":-12006}'"}}})
        self.assertIsNone(v3_universe.resolve_universe(run, self.tmp, "US"))
        note = v3_universe.universe_note(self.tmp, "US")
        self.assertIn("-12006", note, "频控原文必须透传，不能吞成「没有持仓」")

    def test_no_group_means_no_universe(self):
        run = FakeCall({"positions": {"ok": True, "value": {"groups": [], "source": "x"}}})
        self.assertIsNone(v3_universe.resolve_universe(run, self.tmp, "SH"))
        self.assertIn("真实持仓为空", v3_universe.universe_note(self.tmp, "SH"))


# ---------------------------------------------------------------------------
# 4. TTL 缓存
# ---------------------------------------------------------------------------
class CacheTests(unittest.TestCase):
    def setUp(self):
        v3_universe.clear_cache()
        self.tmp = tempfile.mkdtemp(prefix="v3uni-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = {"now": 1000.0}

    def _clock(self):
        return self.clock["now"]

    def _run(self):
        return FakeCall({"positions": positions_envelope(
            [{"acc_id": "1", "market": 1, "positions": [{"symbol": "00700"}]}])})

    def test_second_call_hits_the_cache(self):
        run = self._run()
        first = v3_universe.resolve_universe(run, self.tmp, "HK", clock=self._clock)
        second = v3_universe.resolve_universe(run, self.tmp, "HK", clock=self._clock)
        self.assertEqual(first["tickers"], ["HK.00700"])
        self.assertEqual(second["tickers"], ["HK.00700"])
        self.assertEqual(len(run.payloads("positions")), 1, "TTL 内不重复打富途")
        self.assertIn("命中进程内 TTL 缓存", second["note"])

    def test_ttl_expiry_re_resolves_and_zero_disables_cache(self):
        run = self._run()
        v3_universe.resolve_universe(run, self.tmp, "HK", clock=self._clock)
        self.clock["now"] += 61.0
        v3_universe.resolve_universe(run, self.tmp, "HK", clock=self._clock)
        self.assertEqual(len(run.payloads("positions")), 2, "TTL 过后续期重解析")
        run2 = self._run()
        v3_universe.resolve_universe(run2, self.tmp, "HK", ttl_ms=0, clock=self._clock)
        v3_universe.resolve_universe(run2, self.tmp, "HK", ttl_ms=0, clock=self._clock)
        self.assertEqual(len(run2.payloads("positions")), 2, "ttl_ms=0 → 缓存关闭")

    def test_env_override_is_honoured(self):
        run = self._run()
        with unittest.mock.patch.dict(os.environ, {"QUOTE_UNIVERSE_TTL_MS": "0"}):
            v3_universe.resolve_universe(run, self.tmp, "HK", clock=self._clock)
            v3_universe.resolve_universe(run, self.tmp, "HK", clock=self._clock)
        self.assertEqual(len(run.payloads("positions")), 2)

    def test_failures_use_a_shorter_negative_ttl(self):
        run = FakeCall()
        self.assertIsNone(v3_universe.resolve_universe(run, self.tmp, "US", clock=self._clock))
        self.assertIsNone(v3_universe.resolve_universe(run, self.tmp, "US", clock=self._clock))
        self.assertEqual(len(run.payloads("positions")), 1, "失败也缓存，避免连环打上游")
        self.clock["now"] += 16.0  # > NEGATIVE_TTL_MS(15s)，仍 < 60s
        self.assertIsNone(v3_universe.resolve_universe(run, self.tmp, "US", clock=self._clock))
        self.assertEqual(len(run.payloads("positions")), 2, "负面缓存更短：恢复后立刻能重试")


# ---------------------------------------------------------------------------
# 5. 市场基准（benchmark_for）
# ---------------------------------------------------------------------------
def series_ok(code, count=30, source="futu/quote_history_kline"):
    return {"ok": True, "value": {"ticker": code, "period": "1d", "source": source,
                                  "bars": bars([100.0 + index for index in range(count)])}}


def series_fail(code, message, error_code="trading/futu-unavailable"):
    return {"ok": False, "error": {"code": error_code, "message": message}}


class BenchmarkForTests(unittest.TestCase):
    """各市场基准：优先级探测 / 降级 / 全不可用 → None（绝不跨市场兜底）。"""

    def setUp(self):
        v3_universe.clear_cache()

    def _run(self, outcomes):
        """``outcomes``: {ticker: 信封}；未登记 → 富途 invalid symbol（真实降级路径）。"""
        def call(payload=None):
            ticker = (payload or {}).get("ticker")
            outcome = outcomes.get(ticker)
            if outcome is None:
                return series_fail(ticker, "取数失败：[errcode=-7] invalid symbol")
            return outcome
        return FakeCall({"series": call})

    def test_first_choice_is_used_per_market(self):
        self.assertEqual(v3_universe.BENCHMARKS["SH"][0], "SH.000300")
        self.assertEqual(v3_universe.BENCHMARKS["HK"][0], "HK.800000")
        self.assertEqual(v3_universe.BENCHMARKS["US"][0], "US.SPY")
        for market, ticker in (("SH", "SH.000300"), ("HK", "HK.800000"), ("US", "US.SPY")):
            run = self._run({ticker: series_ok(ticker)})
            found = v3_universe.benchmark_for(market, run)
            self.assertEqual(found["market"], market)
            self.assertEqual(found["ticker"], ticker)
            self.assertEqual(found["source"], "futu/quote_history_kline")
            self.assertEqual(run.payloads("series")[0],
                             {"ticker": ticker, "period": "1d", "limit": 250})

    def test_falls_back_when_the_first_candidate_is_unavailable(self):
        run = self._run({
            "HK.800000": series_fail("HK.800000",
                                     "非预期响应（HTTP 403）：b'{\"code\":-12006}'"),
            "HK.800700": series_ok("HK.800700"),
        })
        found = v3_universe.benchmark_for("HK", run)
        self.assertEqual(found["ticker"], "HK.800700", "首选失败 → 降级到次选")
        self.assertIn("-12006", found["note"], "失败候选的真实原因必须留存")
        self.assertEqual([payload["ticker"] for payload in run.payloads("series")],
                         ["HK.800000", "HK.800700"])

    def test_insufficient_bars_counts_as_unavailable(self):
        run = self._run({"HK.800000": series_ok("HK.800000", count=1),
                         "HK.800700": series_ok("HK.800700")})
        found = v3_universe.benchmark_for("HK", run)
        self.assertEqual(found["ticker"], "HK.800700")
        self.assertIn("日 K 仅 1 根", found["note"])

    def test_all_candidates_unavailable_returns_none_with_candidates_in_the_note(self):
        run = self._run({})
        self.assertIsNone(v3_universe.benchmark_for("US", run))
        note = v3_universe.benchmark_note("US")
        for ticker in v3_universe.BENCHMARKS["US"]:
            self.assertIn(ticker, note)
        self.assertIn("invalid symbol", note)
        self.assertIn("beta/alpha/ir 置空", note)
        self.assertEqual([payload["ticker"] for payload in run.payloads("series")],
                         list(v3_universe.BENCHMARKS["US"]))

    def test_bad_market_is_none_and_never_calls_the_tool(self):
        run = self._run({})
        self.assertIsNone(v3_universe.benchmark_for("MARS", run))
        self.assertEqual(run.calls, [], "非法市场不该探测任何候选")
        # market 缺省（None/空）与 resolve_universe 同口径 → SH
        self.assertEqual(v3_universe.benchmark_for(None, self._run({"SH.000300": series_ok("SH.000300")}))["ticker"],
                         "SH.000300")

    def test_result_is_cached_within_the_ttl(self):
        clock = {"now": 500.0}
        run = self._run({"US.SPY": series_ok("US.SPY")})
        first = v3_universe.benchmark_for("US", run, clock=lambda: clock["now"])
        second = v3_universe.benchmark_for("US", run, clock=lambda: clock["now"])
        self.assertEqual(first["ticker"], "US.SPY")
        self.assertEqual(second["ticker"], "US.SPY")
        self.assertEqual(len(run.payloads("series")), 1, "TTL 内不重复探测上游")
        self.assertIn("命中进程内 TTL 缓存", second["note"])
        clock["now"] += 61.0
        v3_universe.benchmark_for("US", run, clock=lambda: clock["now"])
        self.assertEqual(len(run.payloads("series")), 2, "TTL 过后续期重探")


# ---------------------------------------------------------------------------
# 6. /api/v3/market/watchlist?market=
# ---------------------------------------------------------------------------
class WatchlistRouteTests(unittest.TestCase):
    def setUp(self):
        v3_universe.clear_cache()
        self.tmp = tempfile.mkdtemp(prefix="v3uni-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.series = lambda payload: {"ok": True, "value": {
            "ticker": payload["ticker"], "period": "1d", "source": "futu/quote_history_kline",
            "bars": bars([10.0, 11.0, 12.0])}}

    def _handler(self, run):
        app = FakeApp()
        v3_market.register(app, run, self.tmp)
        return app.routes["/api/v3/market/watchlist"]

    def test_rows_only_contain_the_requested_market(self):
        write_config(self.tmp, {"watchlist": ["SH.600000", "HK.00100", "US.NVDA"],
                                "watchlists": {"HK": ["HK.00100", "HK.00981"]}})
        handler = self._handler(FakeCall({"series": self.series}))
        payload = asyncio.run(handler(n=6, market="HK"))
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["market"], "HK")
        self.assertEqual([row["ticker"] for row in payload["rows"]], ["HK.00100", "HK.00981"])
        self.assertEqual(payload["universe_source"], "config/trading-platform.json#watchlists.HK")
        self.assertIn("共 2 只", payload["universe_note"])
        self.assertEqual(payload["sources"]["kline"], "futu/quote_history_kline")

    def test_default_market_is_sh_and_legacy_pool_is_filtered(self):
        write_config(self.tmp, {"watchlist": ["SH.600000", "HK.00100"]})
        handler = self._handler(FakeCall({"series": self.series}))
        payload = asyncio.run(handler(n=6, market="SH"))
        self.assertEqual([row["ticker"] for row in payload["rows"]], ["SH.600000"])
        self.assertEqual(payload["market"], "SH")

    def test_no_universe_is_an_honest_error(self):
        write_config(self.tmp, {"watchlist": ["SH.600000"]})
        handler = self._handler(FakeCall({"series": self.series}))
        payload = asyncio.run(handler(n=6, market="US"))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "market/no-universe")
        self.assertEqual(payload["error"]["message"], "该市场没有配置自选池、也没有真实持仓")
        self.assertIn("没有 watchlists.US", payload["error"]["detail"])

    def test_bad_market_is_rejected(self):
        handler = self._handler(FakeCall({"series": self.series}))
        payload = asyncio.run(handler(n=6, market="MARS"))
        self.assertEqual(payload["error"]["code"], "market/bad-market")
        self.assertEqual(payload["error"]["message"], "market 需为 SH / HK / US")


# ---------------------------------------------------------------------------
# 6. /api/v3/research?market= 与 /research/tasks?market=
# ---------------------------------------------------------------------------
SNAPSHOT = {"mode": "sim", "generated_at": "2026-09-20T00:00:00Z",
            "runs": [{"id": "r1", "ticker": "SH.600000"}, {"id": "r2", "ticker": "HK.00700"}],
            "reports": [{"id": "p1", "ticker": "SH.600009"}, {"id": "p2", "ticker": "HK.00005"}],
            "previews": [{"id": "v1", "value": {"ticker": "SH.600000"}},
                         {"id": "v2", "value": {"ticker": "US.NVDA"}},
                         {"id": "v3", "value": {}}],
            "activity": [{"id": "a1", "ticker": "SH.600010"},
                         {"id": "a2", "tool": "futu/sim_trade_history_order_list"}]}
TASKS = [{"task_id": "t1", "market": "SH", "kind": "daily_brief"},
         {"task_id": "t2", "market": "HK", "kind": "factor_patrol"},
         {"task_id": "t3", "kind": "mining_round"}]


class ResearchRouteTests(unittest.TestCase):
    def setUp(self):
        v3_universe.clear_cache()
        self.tmp = tempfile.mkdtemp(prefix="v3uni-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _app(self):
        run = FakeCall({"snapshot": {"ok": True, "value": SNAPSHOT}})

        def wb_http(endpoint, payload=None):
            return {"ok": True, "value": {"tasks": TASKS}}

        app = FakeApp()
        v3_research.register(app, run, self.tmp, wb_http=wb_http)
        return app

    def test_research_filters_every_list_by_ticker_prefix(self):
        app = self._app()
        payload = asyncio.run(app.routes["/api/v3/research"](market="SH"))
        self.assertEqual([row["id"] for row in payload["runs"]], ["r1"])
        self.assertEqual([row["id"] for row in payload["reports"]], ["p1"])
        self.assertEqual([row["id"] for row in payload["previews"]], ["v1"],
                         "previews 的标的在 value.ticker 里，同样按市场过滤")
        self.assertEqual([row["id"] for row in payload["activity"]], ["a1"])
        self.assertEqual(payload["market"], "SH")
        self.assertEqual(payload["filter"]["runs"], 2)
        self.assertEqual(payload["filter"]["keptRuns"], 1)
        self.assertEqual(payload["filter"]["keptPreviews"], 1)
        self.assertEqual(payload["filter"]["keptActivity"], 1)
        self.assertEqual(payload["filter"]["unattributed"], 2,
                         "没有标的的行（v3 / broker_response）无法归属 → 如实计数，不留在本市场列表")

    def test_research_without_market_is_unchanged(self):
        app = self._app()
        payload = asyncio.run(app.routes["/api/v3/research"](market=""))
        self.assertEqual(len(payload["runs"]), 2)
        self.assertEqual(len(payload["previews"]), 3)
        self.assertEqual(len(payload["activity"]), 2)
        self.assertNotIn("market", payload)
        self.assertEqual(payload["source"], "workbench/snapshot")

    def test_research_empty_market_says_so(self):
        app = self._app()
        payload = asyncio.run(app.routes["/api/v3/research"](market="US"))
        self.assertEqual(payload["runs"], [])
        self.assertEqual(payload["reports"], [])
        self.assertEqual([row["id"] for row in payload["previews"]], ["v2"],
                         "US 的 preview 仍按市场返回（该市场有新信号，只是没有 run/研报）")
        self.assertEqual(payload["filter"]["otherMarkets"], ["SH", "HK"])
        self.assertIn("不退回全部市场", payload["filter"]["note"])

    def test_research_tasks_filter_by_market_field(self):
        app = self._app()
        payload = asyncio.run(app.routes["/api/v3/research/tasks"](market="HK"))
        self.assertEqual([row["task_id"] for row in payload["tasks"]], ["t2"])
        self.assertEqual(payload["filter"]["tasks"], 3)
        self.assertEqual(payload["filter"]["unattributed"], 1, "无 market 标注的任务如实计数")
        self.assertEqual(payload["filter"]["otherMarkets"], ["SH"])

        raw = asyncio.run(app.routes["/api/v3/research/tasks"](market=""))
        self.assertEqual(len(raw["tasks"]), 3, "不传 market 时与历史一致")

    def test_research_bad_market(self):
        app = self._app()
        for path in ("/api/v3/research", "/api/v3/research/tasks"):
            payload = asyncio.run(app.routes[path](market="MARS"))
            self.assertEqual(payload["error"]["code"], "market/bad-market")


if __name__ == "__main__":
    unittest.main()
