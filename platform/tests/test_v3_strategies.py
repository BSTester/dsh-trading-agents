import copy
import datetime as dt
import math
import random
import unittest
from unittest.mock import patch

from server import v3_analytics, v3_math, v3_nlp, v3_sources, v3_strategies as strategies


AS_OF = "2026-09-21"
TICKER = "SH.600000"


def bars(values, start="2026-09-17"):
    day = dt.date.fromisoformat(start)
    return [{"t": (day + dt.timedelta(days=i)).isoformat(), "c": value}
            for i, value in enumerate(values)]


def event(day="2026-09-17", kind="buyback", **extra):
    return {"announced": day, "type": kind, "source": "fixture/announcement", **extra}


class OfflineCase(unittest.TestCase):
    def setUp(self):
        self.news = self.enterContext(patch.object(
            v3_sources, "fetch_news", return_value={"ok": True, "source": "fixture/news", "rows": []}))
        self.enterContext(patch.object(v3_analytics, "_series_as_of", return_value=AS_OF))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
        self.calls = []
        self.events = {TICKER: [event()]}
        self.prices = {TICKER: bars([90, 100, 110, 121])}

    def run_tool(self, name, payload):
        self.calls.append((name, dict(payload)))
        ticker = payload["ticker"]
        if name == "events":
            return {"ok": True, "value": {"events": self.events.get(ticker, [])}}
        if name == "series":
            return {"ok": True, "value": {"bars": self.prices.get(ticker, []), "source": "fixture/bars"}}
        raise AssertionError(name)

    def study(self, **kwargs):
        options = {"tickers_raw": [TICKER], "horizon": 2, "min_events": 1, "days": 30}
        options.update(kwargs)
        return strategies.event_study(self.run_tool, **options)

    def arb(self, **kwargs):
        options = {"tickers_raw": list(self.prices), "z_window": 10}
        options.update(kwargs)
        return strategies.stat_arb(self.run_tool, **options)


class EventStudyTests(OfflineCase):
    def test_daily_and_cumulative_returns_are_not_summed_twice(self):
        result = self.study()
        self.assertTrue(result["ok"], result)
        self.assertNotIn("carCurve", result)
        self.assertNotIn("caarPct", result["summary"])
        self.assertEqual(result["returnCurve"], [
            {"offset": 1, "meanDailyRetPct": 10.0, "meanCumulativeRetPct": 10.0, "n": 1},
            {"offset": 2, "meanDailyRetPct": 10.0, "meanCumulativeRetPct": 21.0, "n": 1}])
        self.assertEqual(result["summary"]["meanRetPct"], 21.0)
        self.assertEqual(result["events"][0]["entryClose"], 100.0)
        self.assertEqual(result["summary"]["byType"][0]["measured"], 1)
        self.assertEqual(result["equityCurve"][-1]["value"], 1.21)
        self.assertIn("活跃事件", result["method"])
        self.assertIn("无基准", result["method"])

    def test_every_price_in_forward_window_must_be_finite_and_positive(self):
        invalid_prices = [{}, {"c": None}, {"c": 0}, {"c": -1},
                          {"c": math.nan}, {"c": math.inf}, {"c": -math.inf}]
        for index in (1, 2, 3):  # entry、持有中间日、exit 都属于完整路径。
            for price in invalid_prices:
                with self.subTest(index=index, price=price):
                    self.prices[TICKER] = bars([90, 100, 110, 121])
                    self.prices[TICKER][index] = {"t": self.prices[TICKER][index]["t"], **price}
                    result = self.study()
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["summary"]["sampled"], 1)
                    self.assertEqual(result["summary"]["measured"], 0)
                    self.assertEqual(result["summary"]["excluded"],
                                     {"noBarAfterAnnounce": 0, "noForwardWindow": 1, "noBars": 0})
                    self.assertEqual(result["events"][0]["status"], "noForwardWindow")
                    self.assertNotIn("retPct", result["events"][0])
                    self.assertIsNone(result["summary"]["meanRetPct"])
                    self.assertEqual(result["summary"]["byType"], [])
                    self.assertFalse(result["sample"]["sufficient"])
                    self.assertEqual(result["returnCurve"], [
                        {"offset": offset, "meanDailyRetPct": None,
                         "meanCumulativeRetPct": None, "n": 0} for offset in (1, 2)])
                    self.assertEqual(result["equityCurve"], [])

    def test_incomplete_path_does_not_dilute_complete_event_returns(self):
        self.events["SH.600001"] = [event()]
        self.prices["SH.600001"] = bars([90, 100, None, 121])
        result = self.study(tickers_raw=list(self.prices))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"]["measured"], 1)
        self.assertEqual(result["summary"]["excluded"]["noForwardWindow"], 1)
        self.assertEqual(result["summary"]["meanRetPct"], 21.0)
        self.assertEqual(result["returnCurve"], [
            {"offset": 1, "meanDailyRetPct": 10.0, "meanCumulativeRetPct": 10.0, "n": 1},
            {"offset": 2, "meanDailyRetPct": 10.0, "meanCumulativeRetPct": 21.0, "n": 1}])
        self.assertEqual(result["equityCurve"][-1]["value"], 1.21)
        self.assertEqual(result["equityCurve"][-1]["active"], 1)

    def test_return_curve_aggregates_unrounded_daily_and_cumulative_returns(self):
        self.prices = {TICKER: bars([90, 100, 110.0004, 121.0004]),
                       "SH.600001": bars([90, 100, 110.0004, 121.0004]),
                       "SH.600002": bars([90, 100, 110.0014, 121.0014])}
        self.events = {ticker: [event()] for ticker in self.prices}
        result = self.study(tickers_raw=list(self.prices))
        self.assertEqual(result["returnCurve"], [
            {"offset": 1, "meanDailyRetPct": 10.001, "meanCumulativeRetPct": 10.001, "n": 3},
            {"offset": 2, "meanDailyRetPct": 10.0, "meanCumulativeRetPct": 21.001, "n": 3}])
        self.assertEqual(result["equityCurve"][-1]["value"], 1.210007)

    def test_empty_sample_has_null_returns_and_zero_counts(self):
        self.events = {}
        result = self.study()
        self.assertIsNone(result["summary"]["meanRetPct"])
        self.assertFalse(result["sample"]["sufficient"])
        for point in result["returnCurve"]:
            self.assertEqual(point["n"], 0)
            self.assertIsNone(point["meanDailyRetPct"])
            self.assertIsNone(point["meanCumulativeRetPct"])

    def test_aggregate_uses_unrounded_returns(self):
        self.prices = {TICKER: bars([90, 100, 101.004]), "SH.600001": bars([90, 100, 101.004]),
                       "SH.600002": bars([90, 100, 101.014])}
        self.events = {ticker: [event()] for ticker in self.prices}
        result = self.study(tickers_raw=list(self.prices), horizon=1)
        self.assertEqual(result["summary"]["meanRetPct"], 1.01)
        self.assertEqual(result["summary"]["byType"][0]["meanRetPct"], 1.01)
        self.assertEqual(result["summary"]["byType"][0]["measured"], 3)

    def test_one_day_window_includes_only_as_of_day(self):
        self.events[TICKER] = [event("2026-09-20"), event(AS_OF)]
        result = self.study(days=1)
        self.assertEqual([row["announced"] for row in result["events"]], [AS_OF])

    def test_window_and_ticker_filters_run_before_merge(self):
        self.events[TICKER] = [
            event("2026-08-23", "start"), event(AS_OF, "end"),
            event("2026-08-22", "old"), event("2026-09-22", "future"),
            event(None, "undated"), event("invalid", "invalid"),
            event(ticker="US.AAPL"), event("2026-09-16T18:00:00Z", "tz"),
            event(negated=True),
        ]
        result = self.study(tickers_raw=["600000.SH", "sh.600000"])
        self.assertTrue(result["ok"], result)
        self.assertEqual({row["type"] for row in result["events"]}, {"start", "end", "tz"})
        self.assertEqual(next(row for row in result["events"] if row["type"] == "tz")["announced"],
                         "2026-09-17")
        report = result["sources"]["futuEvents"]["byTicker"][TICKER]
        for key, count in {"droppedNoAnnounceTime": 2, "droppedFuture": 1,
                           "droppedOutsideWindow": 1, "droppedTicker": 1, "droppedNegated": 1}.items():
            self.assertEqual(report[key], count, key)
        self.assertEqual(len([call for call in self.calls if call[0] == "events"]), 1)

    def test_no_bars_short_forward_no_later_bar_and_insufficient_sample(self):
        self.events[TICKER] = [event("2026-09-19"), event("2026-09-20", "halt")]
        self.events["SH.600001"] = [event()]
        result = self.study(tickers_raw=[TICKER, "SH.600001"], min_events=5)
        self.assertEqual(result["summary"]["excluded"],
                         {"noBarAfterAnnounce": 1, "noForwardWindow": 1, "noBars": 1})
        self.assertFalse(result["sample"]["sufficient"])
        self.assertEqual(result["summary"]["measured"], 0)

    def test_weekend_entry_is_first_later_trading_close(self):
        self.events[TICKER] = [event("2026-09-19")]
        self.prices[TICKER] = [{"t": day, "c": close} for day, close in
                               [("2026-09-18", 90), ("2026-09-21", 100), ("2026-09-22", 110)]]
        with patch.object(v3_analytics, "_series_as_of", return_value="2026-09-22"):
            result = self.study(horizon=1)
        self.assertEqual(result["events"][0]["entryDate"], "2026-09-21")
        self.assertEqual(result["events"][0]["retPct"], 10.0)

    def test_multitype_dedupe_and_pit_bars(self):
        self.events[TICKER] = [event(), event(), event(kind="dividend")]
        self.prices[TICKER].append({"t": "2026-09-22", "c": 9999})
        self.news.return_value["rows"] = [{"title": "公司回购", "published_at": "2026-09-17",
                                           "ticker": TICKER, "source": "证券时报"}]
        result = self.study()
        self.assertEqual(result["summary"]["measured"], 2)
        self.assertEqual(result["summary"].get("deduplicated"), 2)
        self.assertTrue(result["sources"]["nlpClassifyEvents"]["ok"])
        self.assertEqual(result["sources"]["nlpClassifyEvents"]["events"], 1)
        self.assertEqual({row["type"]: row["measured"] for row in result["summary"]["byType"]},
                         {"buyback": 1, "dividend": 1})
        self.assertEqual(result["pit"]["rejectedFuture"], 1)

    def test_active_event_equity_is_not_final_event_return_average(self):
        self.events["SH.600001"] = [event("2026-09-18")]
        self.prices["SH.600001"] = bars([90, 100, 90, 90], "2026-09-18")
        result = self.study(tickers_raw=list(self.prices))
        self.assertEqual(result["summary"]["meanRetPct"], 5.5)
        self.assertEqual(result["equityCurve"][-1]["value"], 1.1)
        self.assertEqual(result["returnCurve"][-1]["meanCumulativeRetPct"], 5.5)

    def test_futu_envelope_ticker_mismatch_is_not_relabelled(self):
        def run(name, payload):
            if name == "events":
                return {"ok": True, "value": {"ticker": "US.AAPL", "events": [event()]}}
            return self.run_tool(name, payload)
        result = strategies.event_study(run, tickers_raw=[TICKER])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["sources"]["futuEvents"]["byTicker"][TICKER]["droppedTicker"], 1)

    def test_bad_upstream_envelopes_are_observable(self):
        result = strategies.event_study(lambda *_: [], tickers_raw=[TICKER])
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["sources"]["futuEvents"]["ok"])
        self.assertEqual(result["sources"]["futuEvents"]["byTicker"][TICKER]["error"]["code"],
                         "v3/bad-envelope")
        self.assertTrue(result["seriesErrors"])
        with patch.object(strategies, "_load_pit_bars", side_effect=ValueError("fixture crash")):
            result = self.study()
        self.assertEqual(result["error"]["code"], "v3/internal")
        self.assertIn("fixture crash", result["error"]["message"])


class NLPCollectorTests(OfflineCase):
    def collect(self, tickers=None, **kwargs):
        return strategies.collect_nlp_events(None, tickers or [TICKER], market="SH",
                                             days=30, as_of=AS_OF, **kwargs)

    def test_production_signatures_run_real_report_and_classifier(self):
        self.news.return_value = {"ok": True, "source": "fixture/news", "rows": [
            {"ticker": TICKER, "source": "证券时报", **row} for row in [
                {"title": "公司回购并分红", "published_at": "2026-09-17T18:00:00Z"},
                {"title": "公司未增持", "published_at": "2026-09-18"},
                {"title": "公司回购", "published_at": None},
                {"title": "公司回购", "published_at": "2026-09-22"},
                {"title": "公司回购", "published_at": "2026-08-22"},
                {"title": "公司回购", "published_at": "2026-08-23T00:00:00+08:00"},
            ]]}
        deps = v3_sources.Deps(home="/unused-fixture")
        with patch.object(v3_nlp, "sentiment_report", wraps=v3_nlp.sentiment_report) as report_call, \
                patch.object(v3_nlp, "classify_events", wraps=v3_nlp.classify_events) as classify_call:
            events, report = self.collect(deps=deps)
        self.assertEqual(report_call.call_count, 1)
        self.assertEqual(classify_call.call_count, 1)
        self.assertIs(self.news.call_args.args[0], deps)
        self.assertEqual(self.news.call_args.args[1], "600000")
        self.assertEqual({row["type"] for row in events}, {"buyback", "dividend"})
        self.assertTrue(all(row["ticker"] == TICKER and row["source"] == "证券时报" for row in events))
        self.assertTrue(all(row["negated"] is False for row in events))
        self.assertTrue(any(row["announced"] == "2026-08-23" for row in events))
        self.assertTrue(any(row["announced"] == "2026-09-18" and row["published_at"].endswith("+00:00")
                            for row in events))
        info = report["byTicker"][TICKER]
        for key in ("droppedNegated", "droppedNoAnnounceTime", "droppedFuture", "droppedOutsideWindow"):
            self.assertEqual(info[key], 1, key)
        self.assertIn("历史覆盖有限", report["note"])

    def test_real_news_other_company_is_not_relabelled_as_requested_ticker(self):
        self.news.return_value["rows"] = [
            {"ticker": "US.MSFT", "source": "Reuters", "keyword": "AAPL",
             "title": "微软回购股份", "published_at": "2026-09-18T02:00:00Z"}]
        events, report = self.collect(["US.AAPL"])
        self.assertEqual(events, [])
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["byTicker"]["US.AAPL"]["droppedTicker"], 1)
        self.assertEqual(self.news.call_args.args[1], "AAPL")

    def test_real_news_keeps_document_attribution_publisher_and_title(self):
        for key in ("ticker", "symbol"):
            with self.subTest(key=key):
                self.news.return_value["rows"] = [
                    {key: "AAPL", "source": "Reuters", "keyword": "AAPL",
                     "title": "苹果回购股份", "published_at": "2026-09-18T02:00:00Z"}]
                events, report = self.collect(["US.AAPL"])
                self.assertTrue(report["ok"], report)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["ticker"], "US.AAPL")
                self.assertEqual(events[0]["source"], "Reuters")
                self.assertEqual(events[0]["detail"], "苹果回购股份")
                self.assertEqual(events[0]["announced"], "2026-09-17")
                self.assertEqual(report["byTicker"]["US.AAPL"]["droppedTicker"], 0)

    def test_real_news_without_document_attribution_is_excluded(self):
        for attribution in ({}, {"ticker": None, "symbol": ""}):
            with self.subTest(attribution=attribution):
                self.news.return_value["rows"] = [
                    {**attribution, "source": "Reuters", "keyword": "AAPL",
                     "title": "苹果回购股份", "published_at": "2026-09-18T02:00:00Z"}]
                events, report = self.collect(["US.AAPL"])
                self.assertEqual(events, [])
                self.assertTrue(report["ok"], report)
                self.assertEqual(report["byTicker"]["US.AAPL"]["droppedTicker"], 1)
                self.assertIn("上游未提供逐文档标的归属的新闻会被排除", report["note"])

    def test_real_news_missing_publisher_is_not_filled_from_feed_source(self):
        for publisher in ({}, {"source": None}, {"source": ""}):
            with self.subTest(publisher=publisher):
                self.news.return_value["rows"] = [
                    {"ticker": "US.AAPL", **publisher,
                     "title": "苹果回购股份", "published_at": "2026-09-18T02:00:00Z"}]
                events, report = self.collect(["US.AAPL"])
                self.assertTrue(report["ok"], report)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["source"], publisher.get("source"))
                self.assertEqual(report["byTicker"]["US.AAPL"]["source"], "fixture/news")

    def test_default_deps_keeps_home(self):
        with patch.object(v3_sources, "Deps", wraps=v3_sources.Deps) as constructor:
            strategies.collect_nlp_events("/unused-fixture", [TICKER], market="SH", days=30, as_of=AS_OF)
        constructor.assert_called_once_with(home="/unused-fixture")

    def test_failure_is_not_empty_and_keeps_original_error(self):
        original = {"code": "fixture/denied", "message": "原始失败 timeout 429"}
        self.news.return_value = {"ok": False, "error": original}
        events, report = self.collect()
        self.assertEqual(events, [])
        self.assertFalse(report["ok"])
        self.assertEqual(report["byTicker"][TICKER]["error"], original)
        self.news.return_value = {"ok": True, "rows": []}
        events, report = self.collect()
        self.assertTrue(report["ok"])
        self.assertEqual(report["byTicker"][TICKER]["documents"], 0)
        self.assertEqual(events, [])

    def test_payload_exceptions_and_malformed_success_are_failures(self):
        for payload in ([], {"ok": True, "events": [{"type": "buyback"}]}):
            with self.subTest(payload=payload), patch.object(v3_nlp, "sentiment_payload", return_value=payload):
                events, report = self.collect()
                self.assertEqual(events, [])
                self.assertFalse(report["ok"])
        with patch.object(v3_nlp, "sentiment_payload", side_effect=RuntimeError("original failure")):
            events, report = self.collect()
        self.assertFalse(report["ok"])
        self.assertIn("original failure", str(report))

    def test_malformed_provenance_cannot_leave_events_from_failed_source(self):
        payload = {"ok": True, "chain": [None], "source": "fixture/news", "doc_events": [
            {"ticker": TICKER, "source": "证券时报", "published_at": AS_OF,
             "events": [{"type": "buyback", "negated": False}]}]}
        with patch.object(v3_nlp, "sentiment_payload", return_value=payload):
            events, report = self.collect()
        self.assertFalse(report["ok"])
        self.assertEqual(events, [])

    def test_no_aggregate_events_and_us_market_date(self):
        payload = {"ok": True, "symbol": "US.AAPL", "source": "fixture/news", "documents": 2,
                   "events": [{"type": "aggregate-only", "latest_at": AS_OF}], "doc_events": [
                       {"ticker": "US.AAPL", "source": "Reuters", "published_at": "2026-09-18T02:00:00Z",
                        "events": [{"type": "buyback", "negated": False}]},
                       {"ticker": "US.MSFT", "source": "Reuters", "published_at": AS_OF,
                        "events": [{"type": "buyback"}]}]}
        with patch.object(v3_nlp, "sentiment_payload", return_value=payload) as call:
            events, report = self.collect(["US.AAPL"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["announced"], "2026-09-17")
        self.assertEqual(report["byTicker"]["US.AAPL"]["droppedTicker"], 1)
        self.assertEqual(call.call_args.kwargs["market"], "US")
        self.assertTrue(call.call_args.kwargs["include_docs"])


# Reference: statsmodels v0.14.5 tsa/adfvalues.py, mackinnonp(c, N=1).
# https://github.com/statsmodels/statsmodels/blob/v0.14.5/statsmodels/tsa/adfvalues.py
# Frozen values evaluated from its scaled coefficients with stdlib erfc; no runtime dependency.
MACKINNON_FIXTURES = [
    (-18.830001, 0.0), (-18.83, 2.0221237233452764e-30),
    (-18.829999, 2.0221237231013727e-30), (-10, 1.8953190533444687e-17),
    (-5, 2.219315471395629e-5), (-3.43, 0.00997770939877972),
    (-2.8616, 0.04999908779336027), (-2.8615, 0.050011693936423296),
    (-2.86, 0.050201099882003095), (-1.950001, 0.3089166673670562),
    (-1.95, 0.30891712246294933), (-1.949999, 0.30891757755916266),
    (-1.610001, 0.4779751276002049), (-1.61, 0.4779756525941893),
    (-1.609999, 0.47856896338985966), (0, 0.9585320860600559),
    (1, 0.9942659485477608), (2.739999, 0.9990880801039574),
    (2.74, 0.9990880801041981), (2.740001, 1.0), (2491, 1.0),
]


class ADFTests(unittest.TestCase):
    def test_official_fixed_statistic_fixtures(self):
        self.assertTrue(callable(getattr(v3_math, "mackinnon_pvalue", None)))
        for statistic, expected in MACKINNON_FIXTURES:
            with self.subTest(statistic=statistic):
                actual = v3_math.mackinnon_pvalue(statistic)
                self.assertTrue(math.isclose(actual, expected, rel_tol=2e-12, abs_tol=1e-40),
                                (statistic, actual, expected))

    def test_monotonic_over_relevant_grid_and_extreme_tails(self):
        self.assertTrue(callable(getattr(v3_math, "mackinnon_pvalue", None)))
        grid = [-1000000] + [i / 100 for i in range(-1800, 276)] + [1000000]
        values = [v3_math.mackinnon_pvalue(t) for t in grid]
        self.assertEqual(values, sorted(values))
        self.assertEqual((values[0], values[-1]), (0.0, 1.0))

    def test_seeded_stationary_random_walk_explosive(self):
        results = {}
        for name, coefficient in [("stationary", 0.3), ("walk", 1.0), ("explosive", 1.08)]:
            rng = random.Random(42)
            values = [1.0]
            for _ in range(300):
                values.append(coefficient * values[-1] + rng.gauss(0, 1))
            results[name] = v3_math.adf_test(values)
        self.assertLess(results["stationary"]["pValue"], 0.05)
        self.assertGreater(results["walk"]["pValue"], 0.05)
        self.assertGreater(results["explosive"]["tStat"], 100)
        self.assertEqual(results["explosive"]["pValue"], 1.0)

    def test_short_constant_and_missing_samples_are_not_tested(self):
        for values in ([], list(range(40)), [1.0] * 100, [None] * 100):
            with self.subTest(values=values[:3]):
                result = v3_math.adf_test(values)
                self.assertIsNone(result["tStat"])
                self.assertIsNone(result["pValue"])

    def test_adf_does_not_round_probability_before_screening(self):
        rng = random.Random(27)
        values = [rng.gauss(0, 1) for _ in range(100)]
        with patch.object(v3_math, "mackinnon_pvalue", return_value=0.04999908779336027, create=True):
            result = v3_math.adf_test(values)
        self.assertEqual(result["pValue"], 0.04999908779336027)


class StatArbTests(OfflineCase):
    def setUp(self):
        super().setUp()
        rng = random.Random(17)
        x, y, z = [], [], []
        level = 4.0
        for _ in range(200):
            level += rng.gauss(0, .012)
            x.append(math.exp(level))
            y.append(math.exp(1.2 * level + rng.gauss(0, .007)))
            z.append(math.exp(.8 * level + rng.gauss(0, .02)))
        self.prices = {TICKER: bars(x, "2026-01-01"), "SH.600001": bars(y, "2026-01-01"),
                       "SH.600002": bars(z, "2026-01-01")}

    def test_screen_is_explicitly_heuristic_not_cointegration(self):
        result = self.arb()
        self.assertTrue(result["ok"], result)
        self.assertNotIn("cointegrated", result)
        self.assertTrue(result["screenPassed"])
        self.assertIn("启发式", result["impl"])
        self.assertIn("Engle-Granger", result["impl"])
        self.assertIn("单序列", result["note"])
        self.assertEqual(result["backtest"]["window"]["phase"], "out-of-sample")

    def test_only_test_window_changes_cannot_change_training_or_selection(self):
        before = self.arb()
        self.assertIsNotNone(before["selected"])
        train_n = before["params"]["trainDays"]
        for values in self.prices.values():
            for i in range(train_n, len(values)):
                values[i]["c"] *= 1 + (i - train_n + 1) * .03
        after = self.arb()
        self.assertEqual(before["candidates"], after["candidates"])
        self.assertEqual(before["selected"], after["selected"])
        self.assertEqual(before.get("screenPassed"), after.get("screenPassed"))
        self.assertNotEqual(before["backtest"], after["backtest"])

    def test_no_passing_candidate_is_not_force_selected(self):
        for p in (1.0, None):
            with self.subTest(p=p), patch.object(v3_math, "adf_test", return_value={"tStat": 2491, "pValue": p}):
                result = self.arb()
            self.assertFalse(result.get("screenPassed", True))
            self.assertIsNone(result["selected"])
            self.assertIsNone(result["backtest"])
            self.assertNotIn("无协整对", result["note"])
            self.assertEqual(result["candidates"][0]["adfTStat"], 2491)

    def test_threshold_uses_raw_p(self):
        for p, passed in ((.04999908779336027, True), (.050011693936423296, False)):
            with self.subTest(p=p), patch.object(v3_math, "adf_test", return_value={"tStat": -2.8616, "pValue": p}):
                result = self.arb()
            self.assertEqual(result.get("screenPassed"), passed)
            self.assertTrue(all(row["adfPass"] == passed for row in result["candidates"]))

    def test_cross_market_short_and_missing_data(self):
        original = copy.deepcopy(self.prices)
        self.prices = {TICKER: original[TICKER], "US.AAPL": original["SH.600001"]}
        self.assertEqual(self.arb()["error"]["code"], "statarb/no-pairs")
        self.prices = {key: values[:79] for key, values in original.items()}
        self.assertEqual(self.arb()["error"]["code"], "statarb/insufficient")
        self.prices = {key: [] for key in original}
        self.assertEqual(self.arb()["error"]["code"], "statarb/insufficient")

    def test_spread_signal_uses_t_minus_one(self):
        before = self.arb()
        train_n = before["params"]["trainDays"]
        self.prices[before["selected"]["pair"][1]][train_n]["c"] *= 2
        after = self.arb()
        first_before = before["backtest"]["equityCurve"][0]
        first_after = after["backtest"]["equityCurve"][0]
        self.assertEqual(first_before["z"], first_after["z"])
        self.assertEqual(first_before["position"], first_after["position"])


if __name__ == "__main__":
    unittest.main()
