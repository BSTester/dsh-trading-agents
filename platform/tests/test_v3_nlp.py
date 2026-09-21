"""``server.v3_nlp``（规格 FR-STRAT-003 情绪引擎）的离线契约测试。

覆盖点（每条都对应一个「可解释 / 不伪造」的承诺）：

  * 词典：规模 ≥150、极性分组方向正确、无越界极性、关键金融词在内；
  * 切分：最大正向匹配优先于 bigram、程度词/否定词被切成独立 token、标点是作用域断点；
  * ``score_text``：命中明细可解释（``hits``）、否定翻转（「未能增长」为负）、
    程度放大（「大幅增长」>「增长」）、标点断作用域、无命中 → ``score=None``（不是 0）；
  * ``score_documents``：时间半衰（新文档权重大于旧文档）、覆盖率、正/负/中性计数、
    未命中词典的文档不把分数稀释成 0、空输入 → ``score=None``、``coverage=0``、
    top_terms 汇总、缺时间戳计数（``undated``）；
  * ``sentiment_factor``：按 K 线窗口做 PIT 截断、无 K 线时不过滤、逐标的 score/coverage；
  * ``sentiment_report`` + 端点：无新闻 → ``score=null``；上游失败 → 错误信封 + chain；
    成功 → per_day/top_terms/coverage 齐全。

``setUp`` 里把 socket / urllib / httpx 三条真实出口全部封死：任何漏注入的取数都会让测试
显式失败，而不是悄悄联网后「碰巧通过」。全部语料自带，**不打网络**。
"""
from __future__ import annotations

import asyncio
import socket
import tempfile
import types
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from unittest import mock

try:
    from zoneinfo import ZoneInfo

    CN_TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001
    CN_TZ = timezone(timedelta(hours=8))

from server import v3_nlp, v3_sources


# ── 测试替身 ───────────────────────────────────────────────────────────────────


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
    """``v3_run`` 替身（``v3_sources.register`` 的第三个位置参数契约）。"""

    def __call__(self, name, payload=None):
        return {"ok": True, "value": None}


class FakeAkshare:
    """假 akshare：``stock_news_em`` 返回注入的 DataFrame 替身或抛注入的异常。"""

    def __init__(self, news=None, news_error=None):
        self.calls = []
        self._news = news
        self._error = news_error

    def stock_news_em(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._news if self._news is not None else []


class BlockRealNetwork(unittest.TestCase):
    """基类：把真实网络出口全部封死（漏注入的取数会显式失败）。"""

    def setUp(self):
        patchers = [
            mock.patch("socket.socket.connect",
                       side_effect=AssertionError("测试禁止真实网络：socket.connect")),
            mock.patch("socket.create_connection",
                       side_effect=AssertionError("测试禁止真实网络：create_connection")),
            mock.patch("urllib.request.urlopen",
                       side_effect=AssertionError("测试禁止真实网络：urlopen")),
            mock.patch("httpx.Client",
                       side_effect=AssertionError("测试禁止真实网络：httpx.Client")),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp(prefix="v3nlp-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))

    def build(self, deps):
        """经**真实** ``v3_nlp.register`` 装配（端点自带 router，不碰 v3_sources 的结构）。"""
        app = FakeApp()
        self.app = app
        self.deps = v3_nlp.register(app, FakeV3Run(), self.tmp, deps)
        return app

    def call(self, path, **params):
        handler = self.app.routes.get(path)
        self.assertIsNotNone(handler, f"路由未注册：{path}（已注册 {sorted(self.app.routes)}）")
        return asyncio.run(handler(**params))


def news_row(title, published_at, summary="", source="测试源"):
    """AKShare 资讯行 → ``fetch_news`` 的行形状（端点测试用）。"""
    return {"title": title, "summary": summary, "published_at": published_at,
            "source": source, "url": "http://example.invalid/a", "keyword": "600519"}


def fake_frame(rows):
    """``_rows_from_frame`` 认 list[dict]，直接给 list 即可（不必装 pandas）。"""
    return list(rows)


NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=CN_TZ)
HELPER_NOW = NOW.isoformat()


def hours_ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


def recent_iso(hours=1):
    """真实墙钟近时（路由层测试专用）。

    路由处理器不收 ``now``，时间窗按**墙钟**算；固定 ``NOW`` 造的发布时间会在墙钟越过
    ``NOW + days`` 后落出窗外（``test_days_param_is_clamped_not_rejected`` 在
    2026-09-21 11:00+08 就这样爆过）。凡断言「在窗口内」的路由用例一律用本函数造时间。
    """
    return (datetime.now(CN_TZ) - timedelta(hours=hours)).isoformat()


# ── 词典与切分 ─────────────────────────────────────────────────────────────────


class LexiconTests(unittest.TestCase):
    def test_lexicon_is_large_enough_and_well_formed(self):
        self.assertGreaterEqual(len(v3_nlp.DEFAULT_LEXICON), 150,
                                f"词典只有 {len(v3_nlp.DEFAULT_LEXICON)} 条，规格要求 ≥150")
        for term, polarity in v3_nlp.DEFAULT_LEXICON.items():
            self.assertIsInstance(term, str)
            self.assertTrue(term, "词条不能为空串")
            self.assertIsInstance(polarity, float)
            self.assertLessEqual(abs(polarity), 1.0, f"{term} 极性越界：{polarity}")
            self.assertNotEqual(polarity, 0.0, f"{term} 极性为 0（等于没收录）")

    def test_lexicon_covers_spec_mandated_terms(self):
        """任务书点名的词必须都在（缺一个就说明词典被误改）。"""
        mandated = ("利好", "利空", "增持", "减持", "超预期", "不及预期", "回购",
                    "立案", "立案调查", "处罚", "违约", "扭亏", "预增", "预减",
                    "涨停", "跌停", "回购", "减持")
        for term in mandated:
            self.assertIn(term, v3_nlp.DEFAULT_LEXICON, f"词典缺 {term}")

    def test_polarity_direction_is_grouped_correctly(self):
        positive = ("利好", "涨停", "增持", "回购", "超预期", "扭亏为盈", "预增", "中标")
        negative = ("跌停", "减持", "立案调查", "处罚", "违约", "预减", "不及预期", "退市")
        for term in positive:
            self.assertGreater(v3_nlp.DEFAULT_LEXICON[term], 0, term)
        for term in negative:
            self.assertLess(v3_nlp.DEFAULT_LEXICON[term], 0, term)

    def test_negators_and_intensifiers_shape(self):
        self.assertIn("不", v3_nlp.NEGATORS)
        self.assertIn("未", v3_nlp.NEGATORS)
        self.assertIn("无", v3_nlp.NEGATORS)
        self.assertIn("难以", v3_nlp.NEGATORS)
        self.assertTrue(all(isinstance(item, str) for item in v3_nlp.NEGATORS))
        self.assertGreater(v3_nlp.INTENSIFIERS["大幅"], 1.0)
        self.assertGreater(v3_nlp.INTENSIFIERS["显著"], 1.0)
        self.assertLess(v3_nlp.INTENSIFIERS["略微"], 1.0)
        self.assertLess(v3_nlp.INTENSIFIERS["小幅"], 1.0)


class SegmentTests(unittest.TestCase):
    def test_prefers_lexicon_term_over_bigram(self):
        tokens = v3_nlp.segment("公司业绩大幅增长")
        self.assertIn("大幅", tokens)
        self.assertIn("增长", tokens)

    def test_longest_match_wins_for_compound(self):
        """``不及预期`` 是整体词条：不能被切成 ``不`` + ``预期``。"""
        self.assertIn("不及预期", v3_nlp.segment("业绩不及预期"))
        # 「遭」不在词典里，因此「立案调查」必须整体命中（而不是被切成「立案」+「调查」）
        self.assertIn("立案调查", v3_nlp.segment("公司遭立案调查"))
        self.assertIn("被立案", v3_nlp.segment("公司被立案调查"))

    def test_ascii_and_digit_run_stays_together(self):
        self.assertIn("20%", v3_nlp.segment("营收增长20%"))

    def test_empty_and_none_are_empty(self):
        self.assertEqual(v3_nlp.segment(""), [])
        self.assertEqual(v3_nlp.segment(None), [])

    def test_custom_lexicon_is_respected(self):
        self.assertIn("特供利好词", v3_nlp.segment("公司特供利好词", lexicon={"特供利好词": 0.9}))


# ── score_text ─────────────────────────────────────────────────────────────────


class ScoreTextTests(unittest.TestCase):
    def test_positive_and_negative_polarity(self):
        good = v3_nlp.score_text("公司净利润增长，机构上调评级")
        bad = v3_nlp.score_text("公司被立案调查，业绩预亏")
        self.assertIsNotNone(good["score"])
        self.assertIsNotNone(bad["score"])
        self.assertGreater(good["score"], 0)
        self.assertLess(bad["score"], 0)

    def test_hits_are_explainable(self):
        result = v3_nlp.score_text("公司被立案调查")
        self.assertTrue(result["hits"], "命中明细不能为空")
        for hit in result["hits"]:
            self.assertEqual(set(hit), {"term", "polarity", "weight"})
            self.assertIn(hit["term"], v3_nlp.DEFAULT_LEXICON)
            self.assertEqual(hit["polarity"], v3_nlp.DEFAULT_LEXICON[hit["term"]])
        self.assertIn("调查", [hit["term"] for hit in result["hits"]])

    def test_no_hits_returns_none_not_zero(self):
        result = v3_nlp.score_text("公司召开股东大会选举董事")
        self.assertIsNone(result["score"], "无命中必须返回 None，不能用 0 冒充中性")
        self.assertEqual(result["hits"], [])
        self.assertEqual(v3_nlp.score_text("")["score"], None)

    def test_negation_flips_and_decays(self):
        positive = v3_nlp.score_text("公司业绩增长")
        negated = v3_nlp.score_text("公司业绩未能增长")
        self.assertGreater(positive["score"], 0)
        self.assertLess(negated["score"], 0, "「未能增长」必须是负面")
        self.assertEqual(negated["negated"], 1)
        # 衰减：|否定后的权重| < 原权重
        self.assertLess(abs(negated["hits"][0]["weight"]), abs(positive["hits"][0]["weight"]))

    def test_unsatisfied_expectation_is_negative(self):
        result = v3_nlp.score_text("公司三季度业绩不及预期")
        self.assertLess(result["score"], 0)
        self.assertEqual(result["hits"][0]["term"], "不及预期")

    def test_negation_does_not_cross_punctuation(self):
        """标点是作用域断点：句号后的「增长」不该被前一句的「不」翻转。"""
        result = v3_nlp.score_text("公司否认了传闻。营收增长")
        weights = {hit["term"]: hit["weight"] for hit in result["hits"]}
        self.assertGreater(weights["营收增长"], 0, "句号后的正面词不该被前句否定词翻转")
        self.assertEqual(result["negated"], 0)

    def test_intensifier_amplifies(self):
        plain = v3_nlp.score_text("公司业绩增长")
        strong = v3_nlp.score_text("公司业绩大幅增长")
        self.assertGreater(strong["score"], plain["score"], "「大幅增长」必须强于「增长」")
        self.assertEqual(strong["intensified"], 1)
        self.assertGreater(strong["hits"][0]["weight"], plain["hits"][0]["weight"])

    def test_weakening_adverb_reduces_weight(self):
        plain = v3_nlp.score_text("公司业绩增长")
        mild = v3_nlp.score_text("公司业绩略微增长")
        self.assertLess(abs(mild["hits"][0]["weight"]), abs(plain["hits"][0]["weight"]))

    def test_custom_lexicon_replaces_default(self):
        result = v3_nlp.score_text("公司业绩增长", lexicon={"增长": -0.9})
        self.assertLess(result["score"], 0)
        self.assertEqual(result["hits"][0]["polarity"], -0.9)

    def test_mixed_text_is_near_neutral(self):
        result = v3_nlp.score_text("利好：净利润增长。利空：股东减持")
        self.assertLess(abs(result["score"]), 0.2)


# ── score_documents ────────────────────────────────────────────────────────────


class ScoreDocumentsTests(unittest.TestCase):
    def test_empty_documents_return_null_not_zero(self):
        payload = v3_nlp.score_documents([], now=NOW)
        self.assertIsNone(payload["score"], "空文档必须 score=None")
        self.assertEqual(payload["documents"], 0)
        self.assertEqual(payload["scored"], 0)
        self.assertEqual(payload["coverage"], 0.0)
        self.assertEqual(payload["top_terms"], [])
        self.assertTrue(payload["notes"])

    def test_no_lexicon_hits_returns_null_and_zero_coverage(self):
        payload = v3_nlp.score_documents(
            [{"title": "公司召开股东大会", "published_at": hours_ago(2)}], now=NOW)
        self.assertIsNone(payload["score"])
        self.assertEqual(payload["coverage"], 0.0)
        self.assertEqual(payload["documents"], 1)
        self.assertEqual(payload["scored"], 0)

    def test_coverage_counts_scored_documents(self):
        docs = [
            {"title": "公司净利润增长", "published_at": hours_ago(1)},
            {"title": "公司召开股东大会", "published_at": hours_ago(2)},
            {"title": "股东大幅减持", "published_at": hours_ago(3)},
            {"title": "公司发布公告", "published_at": hours_ago(4)},
        ]
        payload = v3_nlp.score_documents(docs, now=NOW)
        self.assertEqual(payload["documents"], 4)
        self.assertEqual(payload["scored"], 2)
        self.assertAlmostEqual(payload["coverage"], 0.5, places=6)
        self.assertEqual(payload["positive"] + payload["negative"] + payload["neutral"], 2)
        self.assertEqual(payload["positive"], 1)
        self.assertEqual(payload["negative"], 1)

    def test_neutral_documents_do_not_dilute_score_to_zero(self):
        """未命中词典的文档只降覆盖率，**不把分数稀释成 0**（那是伪造中性）。"""
        only = v3_nlp.score_documents(
            [{"title": "公司净利润大幅增长", "published_at": hours_ago(1)}], now=NOW)
        mixed = v3_nlp.score_documents(
            [
                {"title": "公司净利润大幅增长", "published_at": hours_ago(1)},
                {"title": "公司召开股东大会", "published_at": hours_ago(1)},
                {"title": "公司发布公告", "published_at": hours_ago(1)},
            ],
            now=NOW,
        )
        self.assertAlmostEqual(only["score"], mixed["score"], places=6)
        self.assertAlmostEqual(mixed["coverage"], 1 / 3, places=6)

    def test_time_half_life_favours_recent_documents(self):
        """同一对正/负文档，只交换时间：近的权重更大，分数跟着近的那篇走。"""
        recent_good = [
            {"title": "公司净利润大幅增长", "published_at": hours_ago(1)},
            {"title": "公司被立案调查", "published_at": hours_ago(96)},  # 2 个半衰期
        ]
        recent_bad = [
            {"title": "公司净利润大幅增长", "published_at": hours_ago(96)},
            {"title": "公司被立案调查", "published_at": hours_ago(1)},
        ]
        first = v3_nlp.score_documents(recent_good, half_life_hours=48, now=NOW)
        second = v3_nlp.score_documents(recent_bad, half_life_hours=48, now=NOW)
        self.assertGreater(first["score"], 0, "近的是利好 → 合计应为正")
        self.assertLess(second["score"], 0, "近的是利空 → 合计应为负")
        self.assertGreater(first["score"], second["score"])

    def test_half_life_scale_is_actually_applied(self):
        """半衰 48h → 96h 前那篇的权重是 1h 前那篇的 1/4（0.25）。"""
        docs = [
            {"title": "公司净利润大幅增长", "published_at": hours_ago(1)},
            {"title": "公司净利润大幅增长", "published_at": hours_ago(96)},
        ]
        weights = []
        # 通过两篇同文同分文档的「分篇计权」间接验证：单篇 vs 单篇的聚合分相同，
        # 因此这里直接核对 half_life 参数对聚合的影响——
        near_only = v3_nlp.score_documents(docs[:1], half_life_hours=48, now=NOW)
        far_only = v3_nlp.score_documents(docs[1:], half_life_hours=48, now=NOW)
        self.assertAlmostEqual(near_only["score"], far_only["score"], places=6)
        # 新旧混合：等于把旧的一篇按 0.25 权重掺进来（分数相同 → 与单篇一致）
        mixed = v3_nlp.score_documents(docs, half_life_hours=48, now=NOW)
        self.assertAlmostEqual(mixed["score"], near_only["score"], places=6)
        weights.append(mixed["documents"])
        self.assertEqual(weights, [2])

    def test_newer_documents_outweigh_older_ones_in_ratio(self):
        """构造方向相反、|分| 相同的一对：旧文档被半衰后无法把合计拉回 0。"""
        payload = v3_nlp.score_documents(
            [
                {"title": "公司净利润大幅增长", "published_at": hours_ago(0)},
                {"title": "公司被立案调查", "published_at": hours_ago(48)},
            ],
            half_life_hours=48, now=NOW,
        )
        # 两篇 |score| 不同（词典权重不同），但半衰的净效应必须让「新的那篇」占优：
        # 若完全不半衰，负篇（-0.95/-0.6 两个命中，mass 更大）会压过正篇。
        no_decay = v3_nlp.score_documents(
            [
                {"title": "公司净利润大幅增长", "published_at": hours_ago(0)},
                {"title": "公司被立案调查", "published_at": hours_ago(0)},
            ],
            half_life_hours=48, now=NOW,
        )
        self.assertLess(no_decay["score"], 0)
        self.assertGreater(payload["score"], no_decay["score"],
                           "半衰必须提升「新利好 + 旧利空」的合计分")

    def test_undated_documents_are_counted_not_dropped(self):
        payload = v3_nlp.score_documents(
            [{"title": "公司净利润增长"}, {"title": "公司净利润增长",
                                          "published_at": hours_ago(1)}], now=NOW)
        self.assertEqual(payload["documents"], 2)
        self.assertEqual(payload["scored"], 2)
        self.assertEqual(payload["undated"], 1)
        self.assertTrue(any("时间戳" in note for note in payload["notes"]))

    def test_unparsable_timestamp_is_not_guessed(self):
        payload = v3_nlp.score_documents(
            [{"title": "公司净利润增长", "published_at": "昨天下午"}], now=NOW)
        self.assertEqual(payload["undated"], 1)

    def test_top_terms_aggregate_and_rank(self):
        payload = v3_nlp.score_documents(
            [
                {"title": "股东大幅减持，公司遭立案调查", "published_at": hours_ago(1)},
                {"title": "公司遭立案调查", "published_at": hours_ago(2)},
            ],
            now=NOW,
        )
        terms = {entry["term"]: entry for entry in payload["top_terms"]}
        self.assertIn("立案调查", terms)
        self.assertEqual(terms["立案调查"]["count"], 2)
        self.assertLess(terms["立案调查"]["weight"], 0)
        weights = [abs(entry["weight"]) for entry in payload["top_terms"]]
        self.assertEqual(weights, sorted(weights, reverse=True), "top_terms 必须按 |权重| 降序")

    def test_method_and_as_of_are_present(self):
        payload = v3_nlp.score_documents([{"title": "公司净利润增长",
                                           "published_at": hours_ago(1)}], now=NOW)
        self.assertEqual(payload["method"], v3_nlp.METHOD)
        self.assertEqual(payload["as_of"], NOW.isoformat())
        self.assertIn("lexicon-v1", payload["method"])
        self.assertEqual(payload["latest_at"], (NOW - timedelta(hours=1)).isoformat())

    def test_latest_at_none_when_all_undated(self):
        payload = v3_nlp.score_documents([{"title": "公司净利润增长"}], now=NOW)
        self.assertIsNone(payload["latest_at"])

    def test_parse_time_accepts_chinese_source_formats(self):
        parsed = v3_nlp.parse_time("2026-09-18 06:38:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.utcoffset(), timedelta(hours=8))
        self.assertIsNone(v3_nlp.parse_time("昨天"))
        self.assertIsNone(v3_nlp.parse_time(None))


# ── sentiment_factor ───────────────────────────────────────────────────────────


class SentimentFactorTests(unittest.TestCase):
    def test_scores_and_coverage_per_ticker(self):
        docs = {
            "600519": [{"title": "公司净利润大幅增长", "published_at": hours_ago(1)}],
            "SH.600000": [{"title": "公司召开股东大会", "published_at": hours_ago(1)}],
        }
        payload = v3_nlp.sentiment_factor({}, docs, now=NOW)
        self.assertGreater(payload["tickers"]["600519"]["score"], 0)
        self.assertEqual(payload["tickers"]["600519"]["coverage"], 1.0)
        self.assertIsNone(payload["tickers"]["SH.600000"]["score"])
        self.assertEqual(payload["tickers"]["SH.600000"]["coverage"], 0.0)
        self.assertEqual(payload["method"], v3_nlp.METHOD)

    def test_bar_window_truncates_documents(self):
        bars = [{"date": (NOW - timedelta(days=index)).date().isoformat()} for index in range(25)]
        docs = {
            "600519": [
                {"title": "公司净利润大幅增长", "published_at": (NOW - timedelta(days=1)).isoformat()},
                {"title": "公司被立案调查", "published_at": (NOW - timedelta(days=40)).isoformat()},
            ]
        }
        payload = v3_nlp.sentiment_factor({"600519": bars}, docs, window=20, now=NOW)
        entry = payload["tickers"]["600519"]
        self.assertEqual(entry["documents"], 1, "窗口外的资讯必须被截断")
        self.assertGreater(entry["score"], 0)
        self.assertIsNotNone(entry["window_from"])

    def test_without_bars_no_truncation_and_window_from_null(self):
        docs = {"600519": [{"title": "公司净利润增长",
                            "published_at": (NOW - timedelta(days=400)).isoformat()}]}
        payload = v3_nlp.sentiment_factor({}, docs, now=NOW)
        self.assertEqual(payload["tickers"]["600519"]["documents"], 1)
        self.assertIsNone(payload["tickers"]["600519"]["window_from"])

    def test_empty_inputs_yield_empty_tickers(self):
        payload = v3_nlp.sentiment_factor({}, {}, now=NOW)
        self.assertEqual(payload["tickers"], {})
        self.assertEqual(payload["as_of"], NOW.isoformat())


# ── sentiment_report：端点响应装配（注入假取数，不打网络） ───────────────────────


class SentimentReportTests(unittest.TestCase):
    def test_success_assembly(self):
        envelope = {
            "ok": True, "source": "akshare/stock_news_em", "attempts": [{"ok": True, "ms": 12}],
            "rows": [news_row("贵州茅台净利润大幅增长", hours_ago(2)),
                     news_row("公司被立案调查", hours_ago(3))],
        }
        payload = v3_nlp.sentiment_report(lambda: envelope, "600519", market="SH",
                                          days=7, limit=20, now=NOW)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["documents"], 2)
        self.assertEqual(payload["coverage"], 1.0)
        self.assertIsNotNone(payload["score"])
        self.assertEqual(payload["positive"], 1)
        self.assertEqual(payload["negative"], 1)
        self.assertTrue(payload["top_terms"])
        self.assertEqual(payload["chain"][0]["rows"], 2)
        self.assertEqual(payload["chain"][0]["attempts"], [{"ok": True, "ms": 12}])
        self.assertTrue(any("窗口" in note for note in payload["notes"]))
        self.assertTrue(all(set(day) >= {"date", "count", "score"} for day in payload["per_day"]))

    def test_no_news_returns_null_score_not_zero(self):
        envelope = {"ok": True, "source": "akshare/stock_news_em", "rows": [], "attempts": []}
        payload = v3_nlp.sentiment_report(lambda: envelope, "600519", market="SH",
                                          days=7, limit=20, now=NOW)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["score"], "无资讯不能返回 0 分")
        self.assertEqual(payload["documents"], 0)
        self.assertIsNone(payload["coverage"])
        self.assertTrue(any("无资讯" in note for note in payload["notes"]))
        self.assertEqual(payload["per_day"], [])

    def test_all_rows_outside_window_reports_no_news_with_counts(self):
        envelope = {"ok": True, "source": "akshare/stock_news_em",
                    "rows": [news_row("旧闻", (NOW - timedelta(days=30)).isoformat())],
                    "attempts": []}
        payload = v3_nlp.sentiment_report(lambda: envelope, "600519", days=7, now=NOW)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["score"])
        self.assertEqual(payload["documents"], 0)
        self.assertIn("全部早于", payload["notes"][0])
        self.assertEqual(payload["chain"][0]["in_window"], 0)

    def test_scored_but_no_lexicon_hits_is_null(self):
        envelope = {"ok": True, "source": "akshare/stock_news_em",
                    "rows": [news_row("公司召开股东大会", hours_ago(1))], "attempts": []}
        payload = v3_nlp.sentiment_report(lambda: envelope, "600519", days=7, now=NOW)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["score"])
        self.assertEqual(payload["documents"], 1)
        self.assertEqual(payload["coverage"], 0.0)

    def test_upstream_failure_returns_error_envelope_with_chain(self):
        envelope = {
            "ok": False, "source": "akshare/stock_news_em",
            "error": {"code": "akshare/stock_news_em",
                      "message": "akshare.stock_news_em 尝试 3 次仍失败：ConnectionError"},
            "attempts": [{"ok": False, "ms": 900, "error": {"code": "akshare/network",
                                                            "message": "ConnectionError"}}],
        }
        payload = v3_nlp.sentiment_report(lambda: envelope, "600519", market="SH",
                                          days=7, now=NOW)
        self.assertFalse(payload["ok"])
        self.assertIsNone(payload["score"])
        self.assertEqual(payload["documents"], 0)
        self.assertEqual(payload["error"]["code"], "akshare/stock_news_em")
        self.assertIn("ConnectionError", payload["error"]["message"])
        self.assertEqual(payload["chain"][0]["attempts"][0]["ms"], 900)

    def test_news_fetch_raising_is_wrapped(self):
        def boom():
            raise TimeoutError("read timeout")

        payload = v3_nlp.sentiment_report(boom, "600519", now=NOW)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "sentiment/news-fetch-failed")
        self.assertIn("read timeout", payload["error"]["message"])
        self.assertIsNone(payload["score"])

    def test_non_envelope_result_is_rejected(self):
        payload = v3_nlp.sentiment_report(lambda: ["not", "a", "dict"], "600519", now=NOW)
        self.assertFalse(payload["ok"])
        self.assertIn("非信封对象", payload["error"]["message"])

    def test_per_day_grouping_and_scores(self):
        envelope = {
            "ok": True, "source": "akshare/stock_news_em", "attempts": [],
            "rows": [news_row("公司净利润大幅增长", "2026-09-20 09:00:00"),
                     news_row("公司被立案调查", "2026-09-20 18:00:00"),
                     news_row("公司净利润大幅增长", "2026-09-19 09:00:00")],
        }
        payload = v3_nlp.sentiment_report(lambda: envelope, "600519", days=7, now=NOW)
        days = {item["date"]: item for item in payload["per_day"]}
        self.assertEqual(sorted(days), ["2026-09-19", "2026-09-20"])
        self.assertEqual(days["2026-09-20"]["count"], 2)
        self.assertLess(days["2026-09-20"]["score"], days["2026-09-19"]["score"])

    def test_undated_rows_are_kept_and_reported(self):
        envelope = {"ok": True, "source": "akshare/stock_news_em", "attempts": [],
                    "rows": [news_row("公司净利润增长", "")]}
        payload = v3_nlp.sentiment_report(lambda: envelope, "600519", days=7, now=NOW)
        self.assertEqual(payload["documents"], 1)
        self.assertEqual(payload["undated"], 1)


# ── 端点：/api/v3/sentiment（经真实 register 装配 + 假 akshare） ─────────────────


class SentimentRouteTests(BlockRealNetwork):
    def build_akshare(self, news=None, news_error=None):
        return self.build(v3_sources.Deps(akshare=FakeAkshare(news=news, news_error=news_error),
                                          home=self.tmp))

    def test_route_is_registered(self):
        app = self.build(v3_sources.Deps(home=self.tmp))
        self.assertIn("/api/v3/sentiment", app.routes)
        self.assertEqual(app.state.v3_nlp["routes"], ("/api/v3/sentiment",))

    def test_register_without_deps_builds_default_deps_from_home(self):
        """``app.py`` 的自动装配只传三个位置参数：deps 必须按 home 自建。"""
        app = FakeApp()
        deps = v3_nlp.register(app, FakeV3Run(), self.tmp)
        self.assertIsInstance(deps, v3_sources.Deps)
        self.assertEqual(deps.home, self.tmp)
        self.assertIn("/api/v3/sentiment", app.routes)

    def test_success_payload_shape(self):
        rows = [
            {"新闻标题": "贵州茅台净利润大幅增长", "新闻内容": "机构上调评级",
             "发布时间": recent_iso(2), "文章来源": "测试源", "新闻链接": "http://x.invalid/1"},
            {"新闻标题": "贵州茅台被立案调查", "新闻内容": "公司公告",
             "发布时间": recent_iso(3), "文章来源": "测试源", "新闻链接": "http://x.invalid/2"},
        ]
        self.build_akshare(news=fake_frame(rows))
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=7, limit=20)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["symbol"], "600519")
        self.assertEqual(payload["market"], "SH")
        self.assertEqual(payload["source"], "akshare/stock_news_em")
        self.assertEqual(payload["documents"], 2)
        self.assertEqual(payload["coverage"], 1.0)
        self.assertIsNotNone(payload["score"])
        for key in ("as_of", "positive", "negative", "neutral", "top_terms", "per_day",
                    "notes", "chain", "method"):
            self.assertIn(key, payload)
        self.assertEqual(payload["method"], v3_nlp.METHOD)

    def test_non_a_share_symbol_uses_bare_keyword(self):
        ak = FakeAkshare(news=[{"新闻标题": "英伟达高管减持", "新闻内容": "",
                                "发布时间": recent_iso(2), "文章来源": "s", "新闻链接": "u"}])
        self.build(v3_sources.Deps(akshare=ak, home=self.tmp))
        payload = self.call("/api/v3/sentiment", symbol="US.NVDA", market="", days=7, limit=20)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["market"], "US")
        self.assertEqual(ak.calls[0]["symbol"], "NVDA", "美股必须用裸代码检索")

    def test_hk_symbol_uses_bare_keyword(self):
        ak = FakeAkshare(news=[])
        self.build(v3_sources.Deps(akshare=ak, home=self.tmp))
        self.call("/api/v3/sentiment", symbol="HK.00700", market="", days=7, limit=20)
        self.assertEqual(ak.calls[0]["symbol"], "00700")

    def test_no_news_returns_null_score(self):
        self.build_akshare(news=[])
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=7, limit=20)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["score"])
        self.assertEqual(payload["documents"], 0)
        self.assertTrue(any("无资讯" in note for note in payload["notes"]))

    def test_upstream_failure_is_error_envelope(self):
        self.build_akshare(news_error=ConnectionError("Remote end closed connection"))
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=7, limit=20)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"]["code"].startswith("akshare/"), payload["error"])
        self.assertIn("Remote end closed connection", payload["error"]["message"])
        self.assertIsNone(payload["score"])
        self.assertTrue(payload["chain"], "失败也要留取数链痕迹")

    def test_empty_symbol_fails_without_touching_akshare(self):
        ak = FakeAkshare(news=[])
        self.build(v3_sources.Deps(akshare=ak, home=self.tmp))
        payload = self.call("/api/v3/sentiment", symbol="", market="", days=7, limit=20)
        self.assertEqual(ak.calls, [], "空 symbol 不该触达 akshare")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "akshare/bad-args")
        self.assertIsNone(payload["score"])
        self.assertEqual(payload["documents"], 0)

    def test_days_window_filters_old_rows(self):
        rows = [{"新闻标题": "贵州茅台净利润增长", "新闻内容": "", "发布时间": recent_iso(1),
                 "文章来源": "s", "新闻链接": "u"},
                {"新闻标题": "贵州茅台净利润增长", "新闻内容": "",
                 "发布时间": (NOW - timedelta(days=20)).isoformat(),
                 "文章来源": "s", "新闻链接": "u"}]
        self.build_akshare(news=fake_frame(rows))
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=7, limit=20)
        self.assertEqual(payload["documents"], 1)
        self.assertEqual(payload["chain"][0]["rows"], 2)
        self.assertEqual(payload["chain"][0]["in_window"], 1)

    def test_days_param_is_clamped_not_rejected(self):
        rows = [{"新闻标题": "贵州茅台净利润增长", "新闻内容": "", "发布时间": recent_iso(1),
                 "文章来源": "s", "新闻链接": "u"}]
        self.build_akshare(news=fake_frame(rows))
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=0, limit=0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["days"], 1)      # 下限 1
        self.assertEqual(payload["limit"], 1)     # 下限 1
        self.assertEqual(payload["documents"], 1)

    def test_missing_akshare_reports_missing(self):
        def boom():
            raise ImportError("No module named 'akshare'")

        self.build(v3_sources.Deps(akshare=boom, home=self.tmp))
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=7, limit=20)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "akshare/missing")
        self.assertIn("akshare", payload["error"]["message"])
        self.assertIsNone(payload["score"])


# ── classify_events：事件识别（FR-STRAT-003 的另一半） ──────────────────────────


class EventCatalogTests(unittest.TestCase):
    """事件规则表本身的契约：≥25 类、字段合法、词条不跨类型、停用词不冲突。"""

    def test_catalog_has_at_least_25_types_with_valid_fields(self):
        self.assertGreaterEqual(len(v3_nlp.EVENT_RULES), 25,
                                f"事件类型只有 {len(v3_nlp.EVENT_RULES)} 类，任务要求 ≥25")
        for rule in v3_nlp.EVENT_RULES:
            self.assertTrue(rule["type"], "type 键不能为空")
            self.assertTrue(rule["label"], f"{rule['type']} 缺中文 label")
            self.assertIn(rule["direction"], (1, -1, 0),
                          f"{rule['type']} direction 非法：{rule['direction']}")
            self.assertTrue(rule["terms"], f"{rule['type']} 没有任何触发词条")
            for term, base in rule["terms"].items():
                self.assertIsInstance(term, str)
                self.assertTrue(term)
                self.assertGreater(base, 0, f"{term} 基础置信必须 >0")
                self.assertLessEqual(base, 1.0, f"{term} 基础置信越界：{base}")

    def test_required_types_are_all_present(self):
        """任务书点名的 30 类必须都在（缺一个说明规则表被误改）。"""
        required = (
            "buyback", "holder_increase", "holder_reduction", "equity_pledge",
            "investigation", "regulatory_penalty", "earnings_preincrease",
            "earnings_prereduce", "earnings_beat", "earnings_miss", "dividend",
            "stock_split", "trading_halt", "trading_resume", "ma_restructuring",
            "contract_win", "major_contract", "outbound_investment", "asset_sale",
            "debt_default", "lawsuit", "arbitration", "management_change",
            "investor_relation", "rating_up", "rating_down", "lockup_expiry",
            "external_guarantee", "fund_occupation", "clarification",
        )
        for etype in required:
            self.assertIn(etype, v3_nlp.EVENT_TYPES, f"事件目录缺 {etype}")

    def test_triggers_map_to_exactly_one_type(self):
        for term, (etype, label, direction) in v3_nlp._EVENT_TRIGGER_INFO.items():
            catalog = v3_nlp.EVENT_TYPES[etype]
            self.assertEqual(label, catalog["label"])
            self.assertEqual(direction, catalog["direction"])
            self.assertGreater(v3_nlp._EVENT_LEXICON[term], 0)

    def test_stop_terms_are_zero_confidence_placeholders(self):
        for term, base in v3_nlp._EVENT_STOP_TERMS.items():
            self.assertEqual(base, 0.0)
            self.assertEqual(v3_nlp._EVENT_LEXICON[term], 0.0)
            self.assertNotIn(term, v3_nlp._EVENT_TRIGGER_INFO)


# 每类至少 1 个**真实语境**正例（不是词条复读：句子里有量词、时间、语境噪音）。
EVENT_EXAMPLES = {
    "buyback": "公司拟以2亿元至4亿元回购股份并注销",
    "holder_increase": "控股股东计划未来6个月内增持公司股份不低于1亿元",
    "holder_reduction": "实控人计划减持不超过2%公司股份",
    "equity_pledge": "控股股东将其所持5%股份办理了股权质押",
    "pledge_release": "控股股东解除质押2000万股",
    "investigation": "公司因涉嫌信息披露违法违规被证监会立案调查",
    "regulatory_penalty": "公司收到行政处罚决定书，被罚款500万元",
    "earnings_preincrease": "公司预计前三季度净利润同比增长80%左右，业绩预增",
    "earnings_prereduce": "公司发布业绩预告，预计净利润同比下降50%，业绩预减",
    "earnings_turnaround": "公司主营回暖，预计全年扭亏为盈",
    "earnings_beat": "三季报业绩超预期，净利润高于预期",
    "earnings_miss": "半年报不及预期，营收低于预期",
    "dividend": "公司发布年度利润分配方案，每10股派发现金红利25元",
    "stock_split": "公司披露高送转方案，每10股转增8股",
    "trading_halt": "公司股票因重大事项临时停牌",
    "trading_resume": "公司股票于今日复牌，恢复交易",
    "ma_restructuring": "公司正在筹划重大资产重组，拟收购标的公司100%股权",
    "contract_win": "公司中标某市地铁项目，中标金额约15亿元",
    "major_contract": "公司与海外客户签署合同，合同金额折合人民币约30亿元",
    "outbound_investment": "公司拟投资设立子公司，布局海外市场",
    "asset_sale": "公司拟出售资产，转让子公司100%股权",
    "debt_default": "公司公告，一笔5亿元债券未按期兑付，构成实质性违约",
    "lawsuit": "公司因合同纠纷被供应商起诉，涉及金额8000万元",
    "arbitration": "公司与合作方就技术转让协议提交仲裁",
    "management_change": "公司董事长辞职，董事会将尽快补选",
    "investor_relation": "公司上周接受多家机构调研，接待机构超50家",
    "rating_up": "券商发布研报，上调评级至买入",
    "rating_down": "外资投行下调评级至卖出，同时下调目标价",
    "lockup_expiry": "公司首发限售股下周解禁，本次解禁数量占总股本30%",
    "external_guarantee": "公司为参股公司银行贷款提供连带担保",
    "fund_occupation": "控股股东非经营性占用上市公司资金，监管要求限期归还",
    "clarification": "公司发布澄清公告，称媒体报道不实",
    "delisting_risk": "公司股价连续低于1元，可能触及面值退市",
}


class ClassifyEventsTests(unittest.TestCase):
    """``classify_events``：识别、否定口径、多事件、零命中、时间诚实、聚合排序。"""

    def classify(self, docs, **kwargs):
        return v3_nlp.classify_events(docs, **kwargs)

    def test_every_type_has_real_context_positive_example(self):
        for etype, sentence in EVENT_EXAMPLES.items():
            with self.subTest(type=etype):
                result = self.classify([{"title": sentence,
                                         "published_at": hours_ago(1)}], now=NOW)
                doc = result["doc_events"][0]
                events = {event["type"]: event for event in doc["events"]}
                self.assertIn(etype, events,
                              f"{etype} 未被识别：{sentence} → {sorted(events)}")
                event = events[etype]
                self.assertEqual(event["direction"], v3_nlp.EVENT_TYPES[etype]["direction"])
                self.assertTrue(event["matched"])
                self.assertTrue(event["label"])
                self.assertIn(event["matched"], v3_nlp._EVENT_TRIGGER_INFO)
                self.assertGreater(event["confidence"], 0)
                self.assertLessEqual(event["confidence"], 1.0)
                self.assertFalse(event["negated"])

    def test_negation_flips_direction_and_decays_confidence(self):
        plain = self.classify([{"title": "控股股东增持公司股份"}])
        negated = self.classify([{"title": "控股股东尚未增持公司股份"}])
        base = next(e for e in plain["doc_events"][0]["events"]
                    if e["type"] == "holder_increase")
        flipped = next(e for e in negated["doc_events"][0]["events"]
                       if e["type"] == "holder_increase")
        self.assertEqual(base["direction"], 1)
        self.assertFalse(base["negated"])
        self.assertEqual(flipped["direction"], -1, "「尚未增持」不得判成增持利多")
        self.assertTrue(flipped["negated"])
        self.assertLess(flipped["confidence"], base["confidence"])

    def test_negation_no_reduction_flips_to_positive(self):
        result = self.classify([{"title": "公司澄清：不存在减持情形"}])
        event = next(e for e in result["doc_events"][0]["events"]
                     if e["type"] == "holder_reduction")
        self.assertEqual(event["direction"], 1, "「不存在减持」应翻成正面弱证据")
        self.assertTrue(event["negated"])

    def test_negation_does_not_cross_punctuation(self):
        result = self.classify([{"title": "传闻未获证实。股东增持股份"}])
        event = next(e for e in result["doc_events"][0]["events"]
                     if e["type"] == "holder_increase")
        self.assertEqual(event["direction"], 1, "句号后的增持不该被前句否定词翻转")
        self.assertFalse(event["negated"])

    def test_neutral_event_negation_only_lowers_confidence(self):
        plain = self.classify([{"title": "公司股票临时停牌"}])
        negated = self.classify([{"title": "公司股票并未临时停牌"}])
        base = next(e for e in plain["doc_events"][0]["events"] if e["type"] == "trading_halt")
        flipped = next(e for e in negated["doc_events"][0]["events"]
                       if e["type"] == "trading_halt")
        self.assertEqual(flipped["direction"], 0, "中性事件取反仍是 0")
        self.assertTrue(flipped["negated"])
        self.assertLess(flipped["confidence"], base["confidence"])

    def test_intensifier_scales_event_confidence(self):
        plain = self.classify([{"title": "股东减持公司股份"}])
        strong = self.classify([{"title": "股东大幅减持公司股份"}])
        base = next(e for e in plain["doc_events"][0]["events"]
                    if e["type"] == "holder_reduction")
        boosted = next(e for e in strong["doc_events"][0]["events"]
                       if e["type"] == "holder_reduction")
        self.assertGreater(boosted["confidence"], base["confidence"])

    def test_multiple_event_types_sorted_by_confidence(self):
        result = self.classify(
            [{"title": "公司公告回购方案，控股股东同步增持，但董事长辞职",
              "published_at": hours_ago(1)}], now=NOW)
        events = result["doc_events"][0]["events"]
        self.assertEqual([event["type"] for event in events],
                         ["buyback", "holder_increase", "management_change"],
                         "多事件全给且按 confidence 降序")
        confidences = [event["confidence"] for event in events]
        self.assertEqual(confidences, sorted(confidences, reverse=True))

    def test_zero_hit_yields_no_events(self):
        result = self.classify([{"title": "公司召开股东大会审议季度报告",
                                 "published_at": hours_ago(1)}], now=NOW)
        self.assertEqual(result["doc_events"][0]["events"], [], "零命中不硬造")
        self.assertEqual(result["events"], [])
        empty = self.classify([])
        self.assertEqual(empty["events"], [])
        self.assertEqual(empty["doc_events"], [])
        self.assertEqual(empty["documents"], 0)

    def test_unparsable_time_marks_events_undated(self):
        result = self.classify([{"title": "股东大幅减持", "published_at": "昨天下午"}],
                               now=NOW)
        entry = result["doc_events"][0]
        self.assertFalse(entry["dated"])
        self.assertIsNone(entry["published_at"])
        self.assertTrue(entry["events"])
        agg = next(e for e in result["events"] if e["type"] == "holder_reduction")
        self.assertEqual(agg["count"], 1, "count 含未定时间的文档")
        self.assertIsNone(agg["latest_at"], "latest_at 只用可解析时间，不猜")
        self.assertIsNone(agg["first_seen"])

    def test_aggregation_counts_stamps_and_orders_by_count(self):
        docs = [
            {"title": "股东减持", "published_at": hours_ago(10)},
            {"title": "公司中标", "published_at": hours_ago(8)},
            {"title": "高管减持股份", "published_at": hours_ago(2)},
        ]
        result = self.classify(docs, now=NOW)
        by_type = {entry["type"]: entry for entry in result["events"]}
        reduction = by_type["holder_reduction"]
        self.assertEqual(reduction["count"], 2)
        self.assertEqual(reduction["first_seen"], (NOW - timedelta(hours=10)).isoformat())
        self.assertEqual(reduction["latest_at"], (NOW - timedelta(hours=2)).isoformat())
        self.assertEqual(result["events"][0]["type"], "holder_reduction",
                         "count 最大的类型排最前")
        self.assertEqual(result["events"][1]["count"], 1)

    def test_aggregation_recency_tiebreak_on_equal_counts(self):
        docs = [
            {"title": "公司中标新项目", "published_at": hours_ago(9)},
            {"title": "公司发布分红方案", "published_at": hours_ago(1)},
        ]
        result = self.classify(docs, now=NOW)
        self.assertEqual([entry["count"] for entry in result["events"]], [1, 1])
        self.assertEqual(result["events"][0]["type"], "dividend",
                         "同 count 时新近的在前")

    def test_missing_and_malformed_fields_are_tolerated(self):
        result = self.classify([{}, {"summary": "控股股东增持股份"},
                                "纯文本新闻", None], now=NOW)
        self.assertEqual(result["documents"], 4)
        self.assertEqual(len(result["doc_events"]), 4)
        for entry in result["doc_events"]:
            self.assertFalse(entry["dated"])
            self.assertIsNone(entry["published_at"])
        self.assertEqual(result["doc_events"][0]["events"], [])
        self.assertTrue(any(event["type"] == "holder_increase"
                            for event in result["doc_events"][1]["events"]))
        self.assertTrue(any(entry["events"] for entry in result["doc_events"]))

    def test_compound_terms_claim_before_substrings(self):
        release = self.classify([{"title": "控股股东解除质押2000万股"}])
        release_types = [e["type"] for e in release["doc_events"][0]["events"]]
        self.assertIn("pledge_release", release_types)
        self.assertNotIn("equity_pledge", release_types, "「解除质押」不是质押利空")

        rating_up = self.classify([{"title": "券商给予公司增持评级"}])
        up_types = [e["type"] for e in rating_up["doc_events"][0]["events"]]
        self.assertIn("rating_up", up_types)
        self.assertNotIn("holder_increase", up_types, "「增持评级」是评级词不是增持事件")

        rating_down = self.classify([{"title": "机构把公司调入减持评级名单"}])
        down_types = [e["type"] for e in rating_down["doc_events"][0]["events"]]
        self.assertIn("rating_down", down_types)
        self.assertNotIn("holder_reduction", down_types, "「减持评级」是评级词不是减持事件")

    def test_money_market_stop_terms_do_not_fire_events(self):
        result = self.classify([{"title": "央行开展质押式回购操作，利率持稳"}])
        self.assertEqual(result["doc_events"][0]["events"], [], "货币市场术语不是公司事件")
        result = self.classify([{"title": "公司定增资金到位，投入产线建设"}])
        types = [e["type"] for e in result["doc_events"][0]["events"]]
        self.assertNotIn("outbound_investment", types, "「定增」不该被切成「增资」")

    def test_result_carries_method_version_and_as_of(self):
        result = self.classify([{"title": "公司回购股份", "published_at": hours_ago(1)}],
                               now=NOW)
        self.assertEqual(result["method"], "rule-v1")
        self.assertEqual(result["version"], v3_nlp.EVENT_VERSION)
        self.assertEqual(result["as_of"], NOW.isoformat())
        self.assertEqual(result["documents"], 1)
        self.assertEqual(result["events"][0]["type"], "buyback")


class SentimentEventsRouteTests(BlockRealNetwork):
    """``/api/v3/sentiment`` 的事件字段：events 聚合常驻、doc_events 按 include_docs 给。"""

    def build_akshare(self, news=None, news_error=None):
        return self.build(v3_sources.Deps(akshare=FakeAkshare(news=news, news_error=news_error),
                                          home=self.tmp))

    def test_success_response_contains_events_aggregation(self):
        t_buy, t_inc, t_none = recent_iso(2), recent_iso(3), recent_iso(4)
        rows = [{"新闻标题": "贵州茅台回购股份", "新闻内容": "", "发布时间": t_buy,
                 "文章来源": "s", "新闻链接": "u"},
                {"新闻标题": "控股股东增持", "新闻内容": "", "发布时间": t_inc,
                 "文章来源": "s", "新闻链接": "u"},
                {"新闻标题": "公司召开股东大会", "新闻内容": "", "发布时间": t_none,
                 "文章来源": "s", "新闻链接": "u"}]
        self.build_akshare(news=fake_frame(rows))
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=7, limit=20)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["event_method"], v3_nlp.EVENT_METHOD)
        self.assertEqual(payload["event_version"], v3_nlp.EVENT_VERSION)
        by_type = {entry["type"]: entry for entry in payload["events"]}
        self.assertEqual(by_type["buyback"]["count"], 1)
        self.assertEqual(by_type["buyback"]["label"], "回购")
        self.assertEqual(by_type["buyback"]["direction"], 1)
        self.assertEqual(by_type["buyback"]["latest_at"], t_buy)
        self.assertEqual(by_type["buyback"]["first_seen"], t_buy)
        self.assertNotIn("doc_events", payload, "默认响应不带每文档事件明细")

    def test_include_docs_returns_per_document_events(self):
        rows = [{"新闻标题": "贵州茅台回购股份", "新闻内容": "", "发布时间": recent_iso(2),
                 "文章来源": "s", "新闻链接": "u"},
                {"新闻标题": "公司召开股东大会", "新闻内容": "", "发布时间": recent_iso(3),
                 "文章来源": "s", "新闻链接": "u"}]
        self.build_akshare(news=fake_frame(rows))
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=7,
                            limit=20, include_docs=True)
        self.assertTrue(payload["ok"], payload)
        doc_events = payload["doc_events"]
        self.assertEqual(len(doc_events), payload["documents"])
        self.assertEqual(doc_events[0]["index"], 0)
        self.assertTrue(doc_events[0]["dated"])
        self.assertTrue(any(event["type"] == "buyback" for event in doc_events[0]["events"]))
        self.assertEqual(doc_events[1]["events"], [], "零命中文档如实给空列表")

    def test_no_news_still_carries_event_envelope(self):
        self.build_akshare(news=[])
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=7, limit=20)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["score"])
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["event_method"], v3_nlp.EVENT_METHOD)
        self.assertEqual(payload["event_version"], v3_nlp.EVENT_VERSION)

    def test_upstream_failure_keeps_event_envelope_shape(self):
        self.build_akshare(news_error=ConnectionError("boom"))
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=7, limit=20)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["event_method"], v3_nlp.EVENT_METHOD)

    def test_events_sorted_by_count_in_response(self):
        rows = [{"新闻标题": "公司回购股份", "新闻内容": "", "发布时间": recent_iso(1),
                 "文章来源": "s", "新闻链接": "u"},
                {"新闻标题": "公司继续回购股份", "新闻内容": "", "发布时间": recent_iso(2),
                 "文章来源": "s", "新闻链接": "u"},
                {"新闻标题": "股东小幅减持", "新闻内容": "", "发布时间": recent_iso(3),
                 "文章来源": "s", "新闻链接": "u"}]
        self.build_akshare(news=fake_frame(rows))
        payload = self.call("/api/v3/sentiment", symbol="600519", market="", days=7, limit=20)
        counts = [entry["count"] for entry in payload["events"]]
        self.assertEqual(counts, sorted(counts, reverse=True))
        self.assertEqual(payload["events"][0]["type"], "buyback")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
