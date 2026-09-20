"""``server/observability.py`` 契约测试（规格 §8.3 Prometheus 文本出口 ``GET /metrics``）。

全部离线：注入记录型 ``v3_run`` 替身（不打网络、不依赖运行中的 8397、每个用例用临时 home）。

覆盖点逐条：
  * **格式合法性**：``# HELP``/``# TYPE`` 在样本前、**同一 family 的样本必须连续**
    （exposition format 的硬要求；交错会让 Prometheus 抓取解析失败）、计数器带 ``_total``；
  * **不编造**：没有真实读数就**不导出**该指标（而不是导出 0）——首次抓取尚无探测快照时
    探测类指标缺席；数据源缓存缺失时降级链指标缺席；
  * **不污染被测对象**：``/metrics`` 的缓存探测走 app 级 ``v3_run``，**不经过** ``v3_ops``
    的计数调用器，因此不会把 ``quantwb_mcp_calls_total`` 灌大（否则失败率会被稀释）；
  * **抓取路径不阻塞**：缓存过期时后台线程刷新，``snapshot()`` 立即返回旧快照；
  * **抓取路径不打外部源**：降级链指标只读落盘缓存，富途探测绝不在此发起；
  * **行业集中度红线**（§7）：`quantwb_risk_industry_pct` 只读 `<home>/v3-risk-probe.json`；
    有缓存导出真实值、缓存过期/没有即**不导出**（不是 0）、缓存里的 `error` 由
    `quantwb_risk_industry_probe_failed` 的 error 标签承载；写入器是
    `POST /api/v3/metrics/probe/refresh`（只读取数 + 本地落盘，不碰交易端点）；
  * **路由优先级**：``/metrics`` 必须排在 ``/{path:path}`` 静态兜底**之前**注册，
    否则会被 SPA 兜底吃掉（返回 index.html）；
  * **旁路容错**：推送状态取不到不得让整个抓取失败。

运行：``cd platform && ~/.dsh/trading-venv/bin/python -B -m unittest tests.test_observability -v``
"""
import ast
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # platform/
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import observability, v3_ops, v3_ratelimit  # noqa: E402

COUNTER_SUFFIX = "_total"


# ---------------------------------------------------------------------------
# exposition format 校验器（本文件自己要断言的核心不变量）
# ---------------------------------------------------------------------------
def parse_exposition(text):
    """解析 Prometheus 文本，返回 ``(families, errors)``。

    ``families`` 是 ``{name: {"type": str|None, "help": bool, "samples": [labels, ...]}}``。
    ``errors`` 列出结构性违规：TYPE/HELP 出现在样本之后、同 family 样本不连续、重复 TYPE。
    """
    families = {}
    errors = []
    order = []
    current = None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# TYPE "):
            parts = line.split()
            if len(parts) != 4:
                errors.append(f"L{number}: TYPE 行字段数不对：{raw!r}")
                continue
            name, kind = parts[2], parts[3]
            if name in families and families[name]["type"] is not None:
                errors.append(f"L{number}: family {name} 重复 TYPE")
            entry = families.setdefault(name, {"type": None, "help": False, "samples": []})
            entry["type"] = kind
            if entry["samples"]:
                errors.append(f"L{number}: family {name} 的 TYPE 出现在样本之后")
            continue
        if line.startswith("# HELP "):
            name = line.split()[2]
            entry = families.setdefault(name, {"type": None, "help": False, "samples": []})
            entry["help"] = True
            if entry["samples"]:
                errors.append(f"L{number}: family {name} 的 HELP 出现在样本之后")
            continue
        if line.startswith("#"):
            continue
        # 样本行：<name>{labels} <value> [timestamp]
        head = line.split(" ")[0]
        name = head.split("{")[0]
        if name not in families:
            errors.append(f"L{number}: 样本 {name} 没有 HELP/TYPE 声明")
            families[name] = {"type": None, "help": False, "samples": []}
        if name not in order:
            order.append(name)
        elif order[-1] != name:
            errors.append(f"L{number}: family {name} 的样本不连续（被 {order[-1]} 隔开）")
        families[name]["samples"].append(head)
    return families, errors


def sample_value(text, head, label_fragment=""):
    """取一条样本的值；不存在返回 ``None``。``head`` 不带标签（如 ``quantwb_up``）。

    用正则而不是 ``split(" ")``：标签值里可能有空格（如 ``error="upstream: 超时"``），
    按空格切会把标签内容当成值。
    """
    pattern = re.compile(r"^(?P<name>[^\s{]+)(?P<labels>\{.*\})?\s+(?P<value>[-+0-9.eE]+)"
                         r"(?:\s+\d+)?$")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = pattern.match(stripped)
        if not match or match.group("name") != head:
            continue
        if label_fragment and label_fragment not in (match.group("labels") or ""):
            continue
        return float(match.group("value"))
    return None


# ---------------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------------
class FakeWorkbench:
    """记录型 ``v3_run`` 替身；``values`` 决定每个工具的信封。"""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []

    def __call__(self, name, payload=None):
        self.calls.append((name, dict(payload or {})))
        if name not in self.values:
            return {"ok": False, "error": {"code": "v3/unknown-tool",
                                           "message": f"未知工具 {name}"}}
        return self.values[name]

    def names(self):
        return [name for name, _ in self.calls]


def wb_ok(name="schedule", value=None):
    return {name: {"ok": True, "value": value if value is not None else {}}}


class ObservabilityTestCase(unittest.TestCase):
    def setUp(self):
        v3_ops.reset_counters()
        v3_ratelimit.reset_limiter()
        self.home = Path(tempfile.mkdtemp(prefix="obs-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.fake = FakeWorkbench({
            **wb_ok("schedule", {"heartbeat": {"pid": 1}}),
            **wb_ok("equity", {"current": 250000.0, "max_drawdown": -0.0625}),
        })
        self.app = FastAPI()
        self.cache = observability.register(self.app, self.fake, str(self.home))
        self.client = TestClient(self.app)

    def metrics(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200, response.text)
        return response.text

    def has_family(self, text, name):
        return any(line.startswith(f"# TYPE {name} ") for line in text.splitlines())


# ---------------------------------------------------------------------------
# 1. 格式与路由
# ---------------------------------------------------------------------------
class FormatTests(ObservabilityTestCase):
    def test_content_type_is_prometheus_text(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertIn("version=0.0.4", response.headers["content-type"])

    def test_exposition_format_is_structurally_valid(self):
        self.cache.refresh_now()
        text = self.metrics()
        families, errors = parse_exposition(text)
        self.assertEqual(errors, [], f"exposition format 违规：{errors}")
        self.assertGreater(len(families), 20, "指标家族数量异常偏少")

    def test_counters_carry_total_suffix_and_gauges_do_not(self):
        """Prometheus 约定：``_total`` 专属于计数器。gauge 带该后缀会被 promtool 判为可疑。"""
        self.cache.refresh_now()
        families, _ = parse_exposition(self.metrics())
        for name, entry in families.items():
            if entry["type"] == "counter":
                self.assertTrue(name.endswith(COUNTER_SUFFIX),
                                f"计数器 {name} 缺少 {COUNTER_SUFFIX} 后缀")
            if entry["type"] == "gauge":
                self.assertFalse(name.endswith(COUNTER_SUFFIX),
                                 f"gauge {name} 不该带 {COUNTER_SUFFIX} 后缀")

    def test_route_wins_over_spa_catch_all(self):
        """/metrics 必须排在静态兜底之前，否则会被 SPA 兜底吃掉（返回 index.html）。"""
        app = FastAPI()
        observability.register(app, self.fake, str(self.home))

        @app.get("/{path:path}")
        async def static_files(path: str):  # noqa: ARG001 —— 模拟 app.py 的 SPA 兜底
            return PlainTextResponse("<!DOCTYPE html><html>SPA</html>",
                                     media_type="text/html")

        body = TestClient(app).get("/metrics").text
        self.assertNotIn("<!DOCTYPE html>", body)
        self.assertIn("quantwb_up 1", body)


# ---------------------------------------------------------------------------
# 2. 进程 / 构建信息
# ---------------------------------------------------------------------------
class ProcessTests(ObservabilityTestCase):
    def test_up_and_build_info(self):
        text = self.metrics()
        self.assertEqual(sample_value(text, "quantwb_up"), 1.0)
        self.assertEqual(sample_value(text, "quantwb_tools"), float(v3_ops.catalog_total()))
        self.assertEqual(sample_value(text, "quantwb_build_info"), 1.0)
        self.assertIn('tools="%d"' % v3_ops.catalog_total(), text)
        self.assertIn('domains="6"', text)

    def test_start_time_and_uptime_are_real(self):
        text = self.metrics()
        started = sample_value(text, "quantwb_process_start_time_seconds")
        uptime = sample_value(text, "quantwb_process_uptime_seconds")
        self.assertIsNotNone(started)
        self.assertIsNotNone(uptime)
        self.assertGreater(started, 1_600_000_000)  # 2020 年之后，排除秒/毫秒单位写错
        self.assertGreaterEqual(uptime, 0.0)
        self.assertLess(uptime, 86_400.0)
        # 与模块记录的启动时刻同源（允许测试自身的执行间隙）
        self.assertAlmostEqual(started, observability.process_started_at(), delta=5.0)

    def test_uptime_grows_between_scrapes(self):
        first = sample_value(self.metrics(), "quantwb_process_uptime_seconds")
        time.sleep(0.02)
        second = sample_value(self.metrics(), "quantwb_process_uptime_seconds")
        self.assertGreater(second, first)


# ---------------------------------------------------------------------------
# 3. 计数类指标必须来自既有唯一事实源
# ---------------------------------------------------------------------------
class CounterTests(ObservabilityTestCase):
    def test_traffic_metrics_track_v3_ops_snapshot(self):
        app = FastAPI()
        v3_ops.register(app, self.fake, str(self.home))
        observability.register(app, self.fake, str(self.home))
        client = TestClient(app)
        client.get("/api/v3/metrics")  # v3_ops 内部调用 schedule 一次 → 计数 +1
        snapshot = v3_ops.metrics_snapshot()
        text = client.get("/metrics").text
        self.assertEqual(sample_value(text, "quantwb_http_requests_total"),
                         float(snapshot["http"]["requests"]))
        self.assertEqual(sample_value(text, "quantwb_mcp_calls_total"),
                         float(snapshot["mcp"]["calls"]))
        self.assertEqual(sample_value(text, "quantwb_mcp_errors_total"),
                         float(snapshot["mcp"]["errors"]))
        self.assertEqual(sample_value(text, "quantwb_tool_calls_total",
                                      'tool="schedule"'),
                         float(snapshot["mcp"]["tools"]["schedule"]))

    def test_probe_does_not_pollute_mcp_counters(self):
        """抓取本身不得把被测的调用计数灌大——否则失败率被稀释，告警失真。"""
        app = FastAPI()
        v3_ops.register(app, self.fake, str(self.home))
        cache = observability.register(app, self.fake, str(self.home))
        client = TestClient(app)
        before = v3_ops.metrics_snapshot()["mcp"]["calls"]
        cache.refresh_now()  # 真实探测：会调 schedule + equity
        client.get("/metrics")
        after = v3_ops.metrics_snapshot()["mcp"]["calls"]
        self.assertEqual(before, after, "探测调用污染了 v3_ops 的 MCP 计数")
        self.assertIn("schedule", self.fake.names(), "探测确实调用了 schedule")
        self.assertIn("equity", self.fake.names(), "探测确实调用了 equity")

    def test_duration_is_exported_in_seconds_not_milliseconds(self):
        """Prometheus 约定用基本单位（秒）；``promtool check metrics`` 会给 ``_ms`` 记 lint。"""
        snapshot = v3_ops.metrics_snapshot()
        text = self.metrics()
        expected = (snapshot["mcp"]["avgMs"] or 0) / 1000.0
        self.assertAlmostEqual(sample_value(text, "quantwb_mcp_call_duration_seconds"),
                               expected, places=9)
        self.assertFalse(self.has_family(text, "quantwb_mcp_call_duration_ms_avg"))

    def test_futu_metrics_track_limiter_view(self):
        view = v3_ratelimit.metrics_view()
        text = self.metrics()
        self.assertEqual(sample_value(text, "futu_enabled"), float(bool(view["enabled"])))
        self.assertEqual(sample_value(text, "futu_calls_total"), float(view["calls"]))
        self.assertEqual(sample_value(text, "futu_rate_limited_total"),
                         float(view["rateLimited"]))
        self.assertEqual(sample_value(text, "futu_cooldown_remaining_ms"),
                         float(view["cooldownRemainingMs"]))
        self.assertEqual(sample_value(text, "futu_in_flight"), float(view["inFlight"]))
        self.assertEqual(sample_value(text, "futu_queued"), float(view["queued"]))

    def test_oms_stage_counts_come_from_ledger_file(self):
        ledger = v3_ops.OmsLedger(None, self.home)
        ledger.write({
            "A": {"id": "A", "stage": "manual", "updated_at": "2026-09-20T01:00:00+00:00"},
            "B": {"id": "B", "stage": "blocked", "updated_at": "2026-09-20T02:00:00+00:00"},
            "C": {"id": "C", "stage": "weird", "updated_at": "2026-09-20T03:00:00+00:00"},
        })
        text = self.metrics()
        self.assertEqual(sample_value(text, "quantwb_oms_orders", 'stage="manual"'), 1.0)
        self.assertEqual(sample_value(text, "quantwb_oms_orders", 'stage="blocked"'), 1.0)
        self.assertEqual(sample_value(text, "quantwb_oms_orders", 'stage="rejected"'), 0.0)
        # 表外阶段如实另立一个样本，不并进表内阶段、也不丢弃
        self.assertEqual(sample_value(text, "quantwb_oms_orders", 'stage="weird"'), 1.0)


# ---------------------------------------------------------------------------
# 4. 工作台探测：缓存、非阻塞、失败如实
# ---------------------------------------------------------------------------
class ProbeTests(ObservabilityTestCase):
    def test_first_scrape_omits_probe_metrics_until_snapshot_exists(self):
        """探测在飞时**不编造**：宁可缺席，也不导出 0。"""
        slow = {"done": threading.Event()}

        def loader():
            slow["done"].wait(timeout=2.0)
            return {"probed_at": time.time(), "workbench_up": 1}

        app = FastAPI()
        observability.register(app, self.fake, str(self.home),
                               deps={"probe": loader, "probe_ttl": 60.0})
        text = TestClient(app).get("/metrics").text
        self.assertFalse(self.has_family(text, "quantwb_workbench_up"),
                         "尚无探测快照时不该导出 workbench_up")
        self.assertFalse(self.has_family(text, "quantwb_risk_drawdown_pct"))
        slow["done"].set()

    def test_probe_snapshot_is_cached_and_reused(self):
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            return {"probed_at": time.time(), "workbench_up": 1, "nav": 1.0,
                    "drawdown_pct": 0.0}

        cache = observability.reset_probe_cache(loader=loader, ttl=60.0)
        cache.refresh_now()
        cache.snapshot()
        cache.snapshot()
        self.assertEqual(calls["n"], 1, "TTL 内不该重复探测")

    def test_expired_cache_does_not_block_the_scrape(self):
        """缓存过期时立刻返回旧快照，刷新交给后台线程——抓取路径绝不阻塞。"""
        gate = threading.Event()
        calls = {"n": 0}

        def loader():
            calls["n"] += 1
            if calls["n"] == 1:
                return {"probed_at": time.time() - 999, "workbench_up": 1}
            gate.wait(timeout=2.0)
            return {"probed_at": time.time(), "workbench_up": 1}

        cache = observability.reset_probe_cache(loader=loader, ttl=0.001)
        cache.refresh_now()
        started = time.perf_counter()
        snapshot = cache.snapshot()  # 已过期 → 触发后台刷新，但立即返回
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 0.5, "snapshot() 阻塞了抓取路径")
        self.assertEqual(snapshot["workbench_up"], 1, "过期时应返回上一次快照")
        gate.set()

    def test_workbench_down_is_reported_not_faked(self):
        def loader():
            return observability._probe_workbench(  # noqa: SLF001 —— 直接验真实探测器
                lambda name, payload=None: {"ok": False,
                                            "error": {"code": "trading/core-unavailable",
                                                      "message": "core bridge down"}})

        cache = observability.reset_probe_cache(loader=loader, ttl=60.0)
        cache.refresh_now()
        text = self.metrics()
        self.assertEqual(sample_value(text, "quantwb_workbench_up"), 0.0)
        self.assertIn("core bridge down", text)
        self.assertTrue(self.has_family(text, "quantwb_workbench_probe_failed"))
        # 工作台不可达时不再多打一次上游问 equity（如实停在「不可达」）
        self.assertFalse(self.has_family(text, "quantwb_workbench_probe_nav"))

    def test_workbench_up_reads_real_drawdown_percent(self):
        cache = observability.reset_probe_cache(
            loader=lambda: observability._probe_workbench(self.fake), ttl=60.0)  # noqa: SLF001
        cache.refresh_now()
        text = self.metrics()
        self.assertEqual(sample_value(text, "quantwb_workbench_up"), 1.0)
        self.assertEqual(sample_value(text, "quantwb_workbench_probe_nav"), 250000.0)
        # -0.0625 → 6.25%（比例制自动换算成百分数）
        self.assertAlmostEqual(sample_value(text, "quantwb_risk_drawdown_pct"), 6.25, places=6)

    def test_drawdown_accepts_already_percent_shaped_value(self):
        fake = FakeWorkbench({**wb_ok("schedule"),
                              **wb_ok("equity", {"current": 1.0, "max_drawdown": -18.0})})
        snapshot = observability._probe_workbench(fake)  # noqa: SLF001
        self.assertAlmostEqual(snapshot["drawdown_pct"], 18.0)

    def test_probe_exception_never_breaks_the_scrape(self):
        def loader():
            raise RuntimeError("探测器炸了")

        cache = observability.reset_probe_cache(loader=loader, ttl=60.0)
        data = cache.refresh_now()
        self.assertEqual(data["workbench_up"], 0)
        self.assertIn("探测器炸了", data["error"])
        text = self.metrics()
        self.assertEqual(sample_value(text, "quantwb_workbench_up"), 0.0)


# ---------------------------------------------------------------------------
# 5. 数据源降级链缓存
# ---------------------------------------------------------------------------
class DatasourceTests(ObservabilityTestCase):
    CHAINS = [
        {"key": "kline", "label": "K 线/历史行情", "primary": "futu/quote_history_kline",
         "fallback": "akshare/stock_zh_a_hist", "available": True,
         "last_source": "akshare/stock_zh_a_hist", "chain_size": 2, "error": None},
        {"key": "news", "label": "个股资讯", "primary": "futu/info_search",
         "fallback": "akshare/stock_news_em", "available": False,
         "last_source": "akshare/stock_news_em", "chain_size": 2, "error": "HTTP 500"},
    ]

    def test_missing_cache_omits_datasource_families(self):
        text = self.metrics()
        self.assertFalse(self.has_family(text, "quantwb_datasource_available"))
        self.assertIsNone(observability.load_datasource_probe(str(self.home)))

    def test_record_then_render(self):
        payload = observability.record_datasource_probe(str(self.home), self.CHAINS)
        self.assertIsNotNone(payload)
        self.assertEqual(observability.datasource_probe_path(str(self.home)).name,
                         observability.DATASOURCE_PROBE_FILENAME)
        text = self.metrics()
        self.assertEqual(sample_value(text, "quantwb_datasource_chains"), 2.0)
        self.assertEqual(sample_value(text, "quantwb_datasource_available_chains"), 1.0)
        self.assertEqual(sample_value(text, "quantwb_datasource_available", 'chain="kline"'), 1.0)
        self.assertEqual(sample_value(text, "quantwb_datasource_available", 'chain="news"'), 0.0)
        # kline 命中的是降级源 → is_fallback=1；news 整链不可用 → is_fallback=0（不是「降级成功」）
        self.assertEqual(sample_value(text, "quantwb_datasource_is_fallback",
                                      'chain="kline"'), 1.0)
        self.assertEqual(sample_value(text, "quantwb_datasource_is_fallback",
                                      'chain="news"'), 0.0)
        self.assertIn('source="akshare/stock_zh_a_hist"', text)

    def test_primary_hit_is_not_marked_as_fallback(self):
        observability.record_datasource_probe(str(self.home), [
            {"key": "kline", "primary": "futu/q", "fallback": "ak/a", "available": True,
             "last_source": "futu/q"}])
        text = self.metrics()
        self.assertEqual(sample_value(text, "quantwb_datasource_is_fallback",
                                      'chain="kline"'), 0.0)

    def test_corrupt_cache_is_ignored_not_guessed(self):
        observability.datasource_probe_path(str(self.home)).write_text(
            "{ 这不是 JSON", encoding="utf-8")
        self.assertIsNone(observability.load_datasource_probe(str(self.home)))
        self.assertFalse(self.has_family(self.metrics(), "quantwb_datasource_available"))

    def test_cache_without_chains_key_is_ignored(self):
        observability.datasource_probe_path(str(self.home)).write_text(
            json.dumps({"version": 1, "chains": "not-a-list"}), encoding="utf-8")
        self.assertIsNone(observability.load_datasource_probe(str(self.home)))

    def test_record_is_best_effort_on_unwritable_home(self):
        missing_parent = self.home / "no" / "such" / "dir"
        # 父目录不存在但可创建 → 成功；换成写入不了的目标则返回 None 而不抛
        self.assertIsNotNone(observability.record_datasource_probe(str(missing_parent),
                                                                   self.CHAINS))
        blocked = self.home / "blocked"
        blocked.write_text("我是文件，不是目录", encoding="utf-8")
        self.assertIsNone(observability.record_datasource_probe(str(blocked), self.CHAINS))

    def test_recorded_probe_is_readable_by_metrics_endpoint(self):
        """端到端：``/api/v3/sources/status`` 落盘 → ``/metrics`` 读回同一条事实。"""
        from server import v3_fallback

        app = FastAPI()
        chains = [row for row in self.CHAINS]
        v3_fallback.register(app, self.fake, str(self.home),
                             deps={"probe": lambda keys: chains})
        observability.register(app, self.fake, str(self.home))
        client = TestClient(app)
        self.assertEqual(client.get("/api/v3/sources/status").json()["ok"], True)
        text = client.get("/metrics").text
        self.assertEqual(sample_value(text, "quantwb_datasource_available", 'chain="news"'), 0.0)


# ---------------------------------------------------------------------------
# 6. 标签转义与旁路容错
# ---------------------------------------------------------------------------
class RobustnessTests(ObservabilityTestCase):
    def test_label_values_are_escaped(self):
        observability.record_datasource_probe(str(self.home), [
            {"key": "kline", "primary": 'we"ird\\path', "fallback": "f",
             "available": True, "last_source": "a\nb"}])
        text = self.metrics()
        families, errors = parse_exposition(text)
        self.assertEqual(errors, [], f"转义后仍非法：{errors}")
        self.assertIn("\\n", text, "换行必须转义成 \\n")
        self.assertIn('\\"', text, "双引号必须转义")
        self.assertIn("\\\\", text, "反斜杠必须转义")

    def test_help_text_newlines_are_escaped(self):
        self.assertNotIn('\n# HELP', observability._escape("a\nb"))  # noqa: SLF001

    def test_push_failure_does_not_break_metrics(self):
        """推送是旁路：``status()`` 抛异常也必须给出同形状的 disabled 态，且不影响主字段。"""
        class ExplodingPush:
            def status(self):
                raise RuntimeError("推送运行时炸了")

        app = FastAPI()
        app.state.push = ExplodingPush()
        observability.register(app, self.fake, str(self.home))
        text = TestClient(app).get("/metrics").text
        self.assertIn("quantwb_up 1", text)
        # 同形状失败态：两条通道都报未连接（不是缺席，也不是「已连接」）
        self.assertEqual(sample_value(text, "quantwb_push_connected", 'channel="quote"'), 0.0)
        self.assertEqual(sample_value(text, "quantwb_push_connected", 'channel="trade"'), 0.0)
        self.assertEqual(sample_value(text, "quantwb_push_enabled"), 0.0)
        families, errors = parse_exposition(text)
        self.assertEqual(errors, [])

    def test_scheduler_metrics_come_from_app_state(self):
        class FakeScheduler:
            alive = True
            last_error = "Traceback (most recent call last): boom"

        class DeadScheduler:
            alive = False
            last_error = None

        app = FastAPI()
        app.state.scheduler = FakeScheduler()
        observability.register(app, self.fake, str(self.home))
        text = TestClient(app).get("/metrics").text
        self.assertEqual(sample_value(text, "quantwb_scheduler_alive"), 1.0)
        self.assertEqual(sample_value(text, "quantwb_scheduler_last_error"), 1.0)

        app2 = FastAPI()
        app2.state.scheduler = DeadScheduler()
        observability.register(app2, self.fake, str(self.home))
        text2 = TestClient(app2).get("/metrics").text
        self.assertEqual(sample_value(text2, "quantwb_scheduler_alive"), 0.0)
        self.assertEqual(sample_value(text2, "quantwb_scheduler_last_error"), 0.0)

    def test_absent_scheduler_is_omitted_not_zeroed(self):
        app = FastAPI()
        observability.register(app, self.fake, str(self.home))
        text = TestClient(app).get("/metrics").text
        self.assertFalse(self.has_family(text, "quantwb_scheduler_alive"),
                         "拿不到调度器时不该导出 0（0 会被读成「调度器已死」）")

    def test_reset_probe_cache_replaces_the_live_route_cache(self):
        """``reset_probe_cache`` 必须真的换掉 ``/metrics`` 正在用的那份缓存（否则运维改了没用）。"""
        gate = threading.Event()

        def loader():
            gate.wait(timeout=2.0)
            return {"probed_at": time.time(), "workbench_up": 1}

        cache = observability.reset_probe_cache(loader=loader, ttl=5.0)
        # 首次抓取：刷新在后台，快照还没到 → 该 family 缺席（不编造 0）
        self.assertFalse(self.has_family(self.metrics(), "quantwb_workbench_up"))
        gate.set()
        deadline = time.time() + 2.0
        while time.time() < deadline and cache.snapshot() is None:
            time.sleep(0.01)
        self.assertEqual(cache.snapshot()["workbench_up"], 1)
        self.assertEqual(sample_value(self.metrics(), "quantwb_workbench_up"), 1.0)


# ---------------------------------------------------------------------------
# 7. 行业集中度红线：探测写入器 + 只读渲染 +「没有读数就不导出」
# ---------------------------------------------------------------------------
class RiskIndustryTests(ObservabilityTestCase):
    """``quantwb_risk_industry_pct`` 必须**只读落盘缓存**，抓取路径绝不打富途。

    上一轮的结论「平台没有行业分类数据源」已过时：``GET /api/v3/risk/industry`` 能拿到
    真实富途板块行业映射。本组用例钉住新链路的四条纪律：
      1. 有缓存 → 导出**真实值**（含 scope="max" 与各市场）；
      2. 无缓存 / 缓存过期 / 缓存损坏 → **不导出该指标**（而不是导出 0）；
      3. 缓存里的 ``error`` 字段必须能被规则/值班感知（``..._probe_failed`` 的 error 标签）；
      4. 写入器是 ``POST /api/v3/metrics/probe/refresh``：只读取数 + 本地落盘。
    """

    SH_OK = {
        "ok": True, "limitPct": 20.0, "breach": True,
        "top": {"industry": "股份制银行Ⅱ", "weightPct": 37.5},
        "sources": {"plate": "futu/info_owner_plate", "weights": "平台组合等权"},
        "missing": [], "universe": ["SH.600000", "SH.600036"],
    }
    US_OK = {
        "ok": True, "limitPct": 20.0, "breach": False,
        "top": {"industry": "半导体", "weightPct": 12.5},
        "sources": {"plate": "futu/info_owner_plate", "weights": "US 市场宇宙等权"},
        "missing": [{"ticker": "US.XXX", "reason": "富途未返回所属板块"}],
        "universe": ["US.NVDA"],
    }
    NO_UNIVERSE = {"ok": False, "error": {"code": "industry/no-universe",
                                          "message": "market=HK 既没有配置自选池、也没有真实持仓"}}
    UPSTREAM_DOWN = {"ok": False, "error": {"code": "industry/upstream",
                                            "message": "info_owner_plate 超时"}}

    def record(self, per_market, **kwargs):
        payload = observability.build_risk_probe_payload(per_market, **kwargs)
        self.assertIsNotNone(observability.record_risk_probe(str(self.home), payload))
        return payload

    # ---- 1. 有缓存 → 真实值 ----
    def test_fresh_cache_exports_real_values(self):
        self.record({"SH": self.SH_OK, "HK": self.NO_UNIVERSE, "US": self.US_OK})
        text = self.metrics()
        families, errors = parse_exposition(text)
        self.assertEqual(errors, [], f"exposition format 违规：{errors}")
        self.assertEqual(sample_value(text, "quantwb_risk_industry_pct", 'scope="max"'), 37.5)
        self.assertEqual(sample_value(text, "quantwb_risk_industry_pct", 'scope="SH"'), 37.5)
        self.assertEqual(sample_value(text, "quantwb_risk_industry_pct", 'scope="US"'), 12.5)
        # 没有 universe 的市场**不导出样本**（缺席 = 该市场没有可分析的标的，不是 0%）
        self.assertNotIn('scope="HK"', text)
        self.assertEqual(sample_value(text, "quantwb_risk_industry_breach"), 1.0)
        self.assertEqual(sample_value(text, "quantwb_risk_industry_limit_pct"), 20.0)
        self.assertEqual(sample_value(text, "quantwb_risk_industry_missing",
                                      'market="SH"'), 0.0)
        self.assertEqual(sample_value(text, "quantwb_risk_industry_probe_info"), 1.0)
        self.assertIn('industry="股份制银行Ⅱ"', text)
        self.assertIn('source="futu/info_owner_plate"', text)
        stamp = sample_value(text, "quantwb_risk_industry_probe_timestamp_seconds")
        self.assertAlmostEqual(stamp, time.time(), delta=30.0)
        # 市场无标的 / 没有映射**不算探测失败**（不导出 failed）
        self.assertFalse(self.has_family(text, "quantwb_risk_industry_probe_failed"))

    def test_missing_cache_omits_industry_families(self):
        self.assertIsNone(observability.load_risk_probe(str(self.home)))
        text = self.metrics()
        self.assertFalse(self.has_family(text, "quantwb_risk_industry_pct"))
        self.assertFalse(self.has_family(text, "quantwb_risk_industry_breach"))
        self.assertFalse(
            self.has_family(text, "quantwb_risk_industry_probe_timestamp_seconds"),
            "没有缓存时不应导出时间戳（没有探测过就没有时刻可言）")

    def test_stale_cache_is_omitted_not_zeroed(self):
        """缓存过期 ⇒ 没有结论：只留时间戳（让「过期」可见），**不导出**取值类指标。"""
        self.record({"SH": self.SH_OK}, generated_at=time.time() - 99999.0)
        text = self.metrics()
        self.assertIsNotNone(sample_value(text, "quantwb_risk_industry_probe_timestamp_seconds"))
        self.assertFalse(self.has_family(text, "quantwb_risk_industry_pct"),
                         "过期缓存不能拿旧值下结论")
        self.assertFalse(self.has_family(text, "quantwb_risk_industry_breach"))
        self.assertFalse(self.has_family(text, "quantwb_risk_industry_missing"))

    def test_max_age_is_configurable_and_parsed_safely(self):
        self.record({"SH": self.SH_OK}, generated_at=time.time() - 3600.0)
        self.assertIn("quantwb_risk_industry_pct", self.metrics())  # 1h 内 → 默认 6h 内可信
        with unittest.mock.patch.dict("os.environ",
                                      {observability.RISK_PROBE_MAX_AGE_ENV: "60"}):
            self.assertNotIn("quantwb_risk_industry_pct", self.metrics())
        with unittest.mock.patch.dict("os.environ",
                                      {observability.RISK_PROBE_MAX_AGE_ENV: "不是数字"}):
            self.assertEqual(observability.risk_probe_max_age(),
                             observability.DEFAULT_RISK_PROBE_MAX_AGE)

    def test_corrupt_or_timestampless_cache_is_ignored(self):
        path = observability.risk_probe_path(str(self.home))
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertIsNone(observability.load_risk_probe(str(self.home)))
        self.assertFalse(self.has_family(self.metrics(), "quantwb_risk_industry_pct"))
        path.write_text(json.dumps({"version": 1, "top_weight_pct": 37.5}), encoding="utf-8")
        self.assertIsNone(observability.load_risk_probe(str(self.home)),
                          "没有 generated_at 就无法判新鲜度，一律当没有")
        self.assertFalse(self.has_family(self.metrics(), "quantwb_risk_industry_pct"))

    # ---- 3. error 字段可见 ----
    def test_error_only_cache_is_visible_and_exports_no_value(self):
        self.record({"SH": self.UPSTREAM_DOWN})
        text = self.metrics()
        self.assertFalse(self.has_family(text, "quantwb_risk_industry_pct"),
                         "一个市场都没给出读数时不能导出 0")
        self.assertEqual(sample_value(text, "quantwb_risk_industry_probe_failed"), 1.0)
        self.assertIn("info_owner_plate 超时", text)
        self.assertIsNotNone(
            sample_value(text, "quantwb_risk_industry_probe_timestamp_seconds"))

    def test_partial_failure_keeps_the_reading_and_flags_the_error(self):
        self.record({"SH": self.SH_OK, "US": self.UPSTREAM_DOWN})
        text = self.metrics()
        self.assertEqual(sample_value(text, "quantwb_risk_industry_pct", 'scope="max"'), 37.5)
        self.assertEqual(sample_value(text, "quantwb_risk_industry_probe_failed"), 1.0)
        self.assertIn("US", text)

    def test_record_is_best_effort_on_unwritable_home(self):
        blocked = self.home / "blocked"
        blocked.write_text("我是文件，不是目录", encoding="utf-8")
        self.assertIsNone(observability.record_risk_probe(str(blocked), self.SH_OK))

    # ---- 4. 写入器（触发者）----
    def test_refresh_endpoint_records_cache_and_metrics_read_it(self):
        calls = {}

        def probe(markets, limit_pct):
            calls["markets"] = list(markets)
            calls["limit_pct"] = limit_pct
            return {"SH": self.SH_OK, "HK": self.NO_UNIVERSE, "US": self.NO_UNIVERSE}

        app = FastAPI()
        observability.register(app, self.fake, str(self.home),
                               deps={"industry_probe": probe})
        client = TestClient(app)
        response = client.post("/api/v3/metrics/probe/refresh")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["written"])
        self.assertEqual(calls["markets"], ["SH", "HK", "US"])
        self.assertEqual(calls["limit_pct"], observability.RISK_PROBE_LIMIT_PCT)
        self.assertIsNotNone(observability.load_risk_probe(str(self.home)))
        text = client.get("/metrics").text
        self.assertEqual(sample_value(text, "quantwb_risk_industry_pct", 'scope="max"'), 37.5)
        # 写入器走的是注入的行业探测器：**没有**任何富途工具被它经 v3_run 触发
        self.assertNotIn("info_owner_plate", self.fake.names())
        self.assertNotIn("plate_list", self.fake.names())

    def test_refresh_endpoint_honours_markets_and_limit_params(self):
        seen = {}

        def probe(markets, limit_pct):
            seen["markets"], seen["limit_pct"] = list(markets), limit_pct
            return {"SH": dict(self.SH_OK, breach=True)}

        app = FastAPI()
        observability.register(app, self.fake, str(self.home), deps={"industry_probe": probe})
        body = TestClient(app).post("/api/v3/metrics/probe/refresh",
                                    params={"markets": "sh,us", "limit_pct": 15}).json()
        self.assertEqual(seen["markets"], ["SH", "US"])
        self.assertEqual(seen["limit_pct"], 15.0)
        self.assertTrue(body["probe"]["breach"])
        self.assertEqual(body["probe"]["limit_pct"], 15.0)

    def test_refresh_endpoint_is_post_only_and_read_only(self):
        """写入器必须只读语义：注册的是 POST，且不新增任何写业务路径。"""
        app = FastAPI()
        observability.register(app, self.fake, str(self.home))
        methods = {}
        for route in app.routes:
            path = getattr(route, "path", "")
            if path == "/api/v3/metrics/probe/refresh":
                methods[path] = set(getattr(route, "methods", set()))
        self.assertEqual(methods.get("/api/v3/metrics/probe/refresh"), {"POST"})
        self.assertEqual(app.state.observability["routes"],
                         ("/metrics", "/api/v3/metrics/probe/refresh"))

    def test_refresh_endpoint_survives_a_probe_exception(self):
        def probe(markets, limit_pct):
            raise RuntimeError("探测器炸了")

        app = FastAPI()
        observability.register(app, self.fake, str(self.home), deps={"industry_probe": probe})
        body = TestClient(app).post("/api/v3/metrics/probe/refresh").json()
        self.assertFalse(body["ok"])
        self.assertIn("探测器炸了", body["error"]["message"])
        self.assertIsNone(observability.load_risk_probe(str(self.home)),
                          "探测抛异常时不该留下半份缓存")

    def test_refresh_refuses_to_guess_when_envelope_is_not_a_dict(self):
        payload = observability.build_risk_probe_payload({"SH": "not-an-envelope"})
        self.assertIsNone(payload["top_weight_pct"])
        self.assertIsNone(payload["breach"])
        self.assertIn("不是信封对象", payload["error"])


# ---------------------------------------------------------------------------
# 8. 单笔占比红线（2%）：读既有 OMS 台账，不新增探测
# ---------------------------------------------------------------------------
class SingleOrderRedlineTests(ObservabilityTestCase):
    def _order(self, identifier, stage, value, nav=100000.0):
        return (identifier, {"id": identifier, "stage": stage, "value": value,
                             "nav_used": nav, "ticker": "SH.600000",
                             "updated_at": "2026-09-20T00:00:00+00:00"})

    def test_max_single_pct_comes_from_manual_orders(self):
        ledger = v3_ops.OmsLedger(None, self.home)
        ledger.write(dict([
            self._order("A", "manual", 3000.0),
            self._order("B", "manual", 2500.0),
            self._order("C", "filled", 9000.0),  # 已成交的不算「待人工确认」
        ]))
        text = self.metrics()
        self.assertAlmostEqual(sample_value(text, "quantwb_risk_single_order_pct_max"), 3.0)

    def test_no_manual_order_omits_the_family(self):
        ledger = v3_ops.OmsLedger(None, self.home)
        ledger.write(dict([self._order("C", "filled", 9000.0)]))
        self.assertFalse(self.has_family(self.metrics(), "quantwb_risk_single_order_pct_max"),
                         "没有待确认单时不该导出（缺席 ≠ 0%）")



# ---------------------------------------------------------------------------
# 9. 告警规则与指标出口的一致性（防止规则写了一个平台根本不会输出的指标名）
# ---------------------------------------------------------------------------
DEPLOY = ROOT / "deploy" / "monitoring"


def declared_metric_names():
    """``observability.py`` 里**所有可能导出**的指标名（静态 AST 提取，不依赖运行时条件）。

    用 AST 而不是运行时抓取：像 ``quantwb_workbench_probe_failed`` 只在探测失败时
    才出现，运行时抓取会漏掉它，导致规则里的合法引用被误判成「编造的指标名」。
    """
    tree = ast.parse((ROOT / "server" / "observability.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in ("gauge", "counter"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            names.add(node.args[0].value)
    return names


def extract_alert_rules(text):
    """从 alerts.yml 抽 ``[{name, expr, for, severity}]``。

    不引 PyYAML（trading-venv 里没有），所以按缩进做最小解析：只认本文件里
    实际使用的结构（``- alert:`` / ``expr:``（含 ``>-`` 块）/ ``for:`` / ``severity:``）。
    """
    lines = text.splitlines()
    rules = []
    current = None
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("- alert:"):
            current = {"name": stripped.split(":", 1)[1].strip(),
                       "expr": "", "for": None, "severity": None, "line": index + 1}
            rules.append(current)
        elif current is not None and stripped.startswith("expr:"):
            inline = stripped.split(":", 1)[1].strip()
            if inline in (">-", ">", "|", "|-"):
                indent = len(line) - len(line.lstrip())
                block = []
                index += 1
                while index < len(lines):
                    nxt = lines[index]
                    if not nxt.strip():
                        index += 1
                        continue
                    if len(nxt) - len(nxt.lstrip()) <= indent:
                        break
                    block.append(nxt.strip())
                    index += 1
                current["expr"] = " ".join(block)
                continue
            current["expr"] = inline
        elif current is not None and stripped.startswith("for:"):
            current["for"] = stripped.split(":", 1)[1].strip()
        elif current is not None and stripped.startswith("severity:"):
            current["severity"] = stripped.split(":", 1)[1].strip()
        index += 1
    return rules


class AlertRulesTests(unittest.TestCase):
    """规则文件必须与指标出口**对得上**——这是本节唯一的存在理由。"""

    @classmethod
    def setUpClass(cls):
        cls.alerts_text = (DEPLOY / "alerts.yml").read_text(encoding="utf-8")
        cls.rules = extract_alert_rules(cls.alerts_text)
        cls.declared = declared_metric_names()

    def test_at_least_six_rules_exist(self):
        self.assertGreaterEqual(len(self.rules), 6,
                                "规格 §8.3 要求至少 6 条告警规则")

    def test_every_rule_has_name_expr_for_and_severity(self):
        for rule in self.rules:
            self.assertTrue(rule["name"], f"L{rule['line']}: 规则缺名字")
            self.assertTrue(rule["expr"], f"L{rule['line']}: {rule['name']} 缺 expr")
            self.assertIsNotNone(rule["for"], f"L{rule['line']}: {rule['name']} 缺 for")
            self.assertIn(rule["severity"], ("critical", "warning", "info"),
                          f"L{rule['line']}: {rule['name']} 的 severity 非法")

    def test_rule_names_are_unique(self):
        names = [rule["name"] for rule in self.rules]
        self.assertEqual(len(names), len(set(names)), "告警名重复")

    def test_every_referenced_metric_is_really_exported(self):
        """expr 里出现的每个 ``quantwb_*``/``futu_*`` 名字都必须是出口真会输出的。"""
        pattern = re.compile(r"\b(?:quantwb_|futu_)[a-z0-9_]+\b")
        problems = []
        for rule in self.rules:
            for token in sorted(set(pattern.findall(rule["expr"]))):
                if token not in self.declared:
                    problems.append(f"{rule['name']}: 引用了不存在的指标 {token}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_all_declared_metrics_are_documented_somewhere(self):
        """反向检查：出口导出的指标必须在规则或 README 里被提到，避免「导了没人用」。"""
        readme = (DEPLOY / "README.md").read_text(encoding="utf-8")
        undocumented = sorted(
            name for name in self.declared
            if name not in self.alerts_text and name not in readme)
        self.assertEqual(undocumented, [],
                         f"这些指标既没有规则也没有文档：{undocumented}")

    def test_required_rule_topics_are_covered(self):
        """规格 §8.3 点名的六类告警至少要各有归属。"""
        names = " ".join(rule["name"] for rule in self.rules)
        for required in ("FutuRateLimited", "QuantWorkbenchDown",
                         "QuantWorkbenchToolfaceUnreachable",
                         "QuantMcpToolCallFailureRateHigh",
                         "DataSourceChainUnavailable", "RiskDrawdownRedLineBreached",
                         "QuantWorkbenchRestartLoop"):
            self.assertIn(required, names, f"缺少规格要求的告警：{required}")

    def test_probe_backed_rules_guard_on_freshness(self):
        """探测类指标来自缓存：相关规则必须带时间守卫，否则会拿旧快照下结论。"""
        guarded = 0
        for rule in self.rules:
            if ("quantwb_workbench_" in rule["expr"] or "quantwb_datasource_" in rule["expr"]
                    or "quantwb_risk_industry_" in rule["expr"]):
                self.assertIn("time()", rule["expr"],
                              f"{rule['name']} 用了探测类指标却没有新鲜度守卫")
                guarded += 1
        self.assertGreaterEqual(guarded, 8, "探测类守卫规则数量异常偏少")

    def test_industry_redline_rules_match_the_platform_limit(self):
        """行业红线阈值必须与 ``v3_ops.LIMITS["industryPct"]`` 同源，不另立第二套数字。"""
        from server import v3_ops  # 局部 import：本文件其余部分不需要它

        limit = f'{v3_ops.LIMITS["industryPct"]:.0f}'
        by_name = {rule["name"]: rule for rule in self.rules}
        critical = by_name["RiskIndustryConcentrationBreached"]
        self.assertEqual(critical["severity"], "critical")
        self.assertEqual(critical["for"], "5m")
        self.assertIn('quantwb_risk_industry_pct{scope="max"}', critical["expr"])
        self.assertIn(f"> {limit}", critical["expr"])
        # 接近红线的预警必须是更低阈值 + 更宽松的 for
        approaching = by_name["RiskIndustryConcentrationApproaching"]
        self.assertEqual(approaching["severity"], "warning")
        self.assertIn('quantwb_risk_industry_pct{scope="max"}', approaching["expr"])
        self.assertIn("> 15", approaching["expr"])

    def test_industry_cache_error_and_staleness_are_covered_by_rules(self):
        """缓存里的 ``error`` 字段与「缓存过期」都必须有规则接住，否则静默。"""
        exprs = " ".join(rule["expr"] for rule in self.rules)
        self.assertIn("quantwb_risk_industry_probe_failed", exprs)
        self.assertIn("quantwb_risk_industry_probe_timestamp_seconds", exprs)
        names = {rule["name"]: rule for rule in self.rules}
        self.assertIn("RiskIndustryProbeFailed", names)
        self.assertIn("RiskIndustryProbeStale", names)

    def test_prometheus_yml_targets_the_real_metrics_path(self):
        text = (DEPLOY / "prometheus.yml").read_text(encoding="utf-8")
        self.assertIn("metrics_path: /metrics", text)
        self.assertIn("127.0.0.1:8397", text)
        self.assertIn("rule_files:", text)
        self.assertIn("- alerts.yml", text)

    def test_declared_metric_universe_is_non_trivial(self):
        # 防「AST 提取失手后所有检查一起假通过」
        self.assertGreater(len(self.declared), 25)
        for name in ("quantwb_up", "futu_rate_limited_total",
                     "futu_cooldown_remaining_ms", "quantwb_datasource_available"):
            self.assertIn(name, self.declared)


if __name__ == "__main__":
    unittest.main()
