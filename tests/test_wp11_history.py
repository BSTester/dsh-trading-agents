"""WP11 任务 3：``sentiment-history`` 查询（HTTP 端点 + MCP 工具；工具面 60 → 61）。

全部离线注入。覆盖计划任务 3 的测试清单与边界：

  * store/summary：当日多标的×多源计数、指定日期、空库空结构（空是事实不是错误）；
  * read_sentiments：倒序 + limit 边界 + PIT 上界（before 不得看到未来观测）；
  * CLI：两种形态共用同一出口（records/summary 两键恒在）；
  * compute：命令拼装、缺省 30、越界/非整数**起子进程前**拒绝、CLI 失败信封 → ComputeError；
  * 路由：envelope 契约、白名单、limit 越界 → core-unavailable、形状不符不缓存、
    TTL 命中与 ``_refresh`` 旁路；
  * MCP：工具在面内（61）、字段/区间/描述、端点工具集 ≡ 端点清单 − 排除集、派发透传；
  * 流程页：情绪阶段摘要追加积累事实（累计天数/连续交易日/最近日期），空库不产生噪声；
  * 只读性：端点调用后库与 kv 零变化。
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from fastapi.testclient import TestClient  # noqa: E402

from server import app as app_module  # noqa: E402
from server import caches, compute, mcp_tools, store_access  # noqa: E402
from trading_core import cli, pipeline, store  # noqa: E402

DATE = "2026-09-16"
PREV = "2026-09-15"


def seed_rows(conn):
    """两日 × 三行：当日 2 标的 3 条（fin_sentiment×2 + fin_news×1），前一日 1 条。"""
    store.insert_sentiment(conn, DATE, "SH.600519", "fin_sentiment", {"x": [1]},
                           fetched_at=f"{DATE} 16:25:00")
    store.insert_sentiment(conn, DATE, "SH.600519", "fin_news", {"news": []},
                           fetched_at=f"{DATE} 16:25:01")
    store.insert_sentiment(conn, DATE, "SZ.300750", "fin_sentiment", {"x": [2]},
                           fetched_at=f"{DATE} 16:25:02")
    store.insert_sentiment(conn, PREV, "SH.600519", "fin_sentiment", {"x": [0]},
                           fetched_at=f"{PREV} 16:25:00")


def fake_runner(stdout="", returncode=0, stderr=""):
    """``compute._spawn`` 同签名替身：记录命令行并回固定 stdout（parse_stdout 原样吃）。"""
    calls = []

    def run(command, timeout):
        calls.append((list(command), timeout))
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    run.calls = calls
    return run


class SentimentStoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_summary_counts_symbols_records_and_sources(self):
        seed_rows(self.conn)
        summary = store.sentiment_summary(self.conn)
        self.assertEqual(summary, {"date": DATE, "symbols": 2, "records": 3,
                                   "sources": {"fin_news": 1, "fin_sentiment": 2}})

    def test_summary_honours_explicit_date(self):
        seed_rows(self.conn)
        summary = store.sentiment_summary(self.conn, date=PREV)
        self.assertEqual(summary, {"date": PREV, "symbols": 1, "records": 1,
                                   "sources": {"fin_sentiment": 1}})

    def test_summary_empty_db_is_empty_structure_not_error(self):
        self.assertEqual(store.sentiment_summary(self.conn),
                         {"date": None, "symbols": 0, "records": 0, "sources": {}})
        self.assertEqual(store.sentiment_summary(self.conn, date=DATE),
                         {"date": DATE, "symbols": 0, "records": 0, "sources": {}})

    def test_read_sentiments_limit_and_pit_upper_bound(self):
        seed_rows(self.conn)
        newest = store.read_sentiments(self.conn, "SH.600519", limit=1)
        self.assertEqual([(r["date"], r["source"]) for r in newest],
                         [(DATE, "fin_news")], "同日倒序按 source，最新日期优先")
        self.assertEqual(len(store.read_sentiments(self.conn, "SH.600519", limit=120)), 3)
        only_prev = store.read_sentiments(self.conn, "SH.600519", before=PREV)
        self.assertEqual([r["date"] for r in only_prev], [PREV],
                         "before 是 PIT 上界：不得看到未来观测")
        self.assertEqual(store.read_sentiments(self.conn, "SH.600519", limit=1)[0]["payload"],
                         {"news": []}, "payload 反序列化为对象")


class SentimentCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "t.sqlite")
        conn = store.connect(self.db)
        try:
            seed_rows(conn)
        finally:
            conn.close()

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def test_summary_form_keeps_both_keys(self):
        out = self._run(["sentiment-history", "--db", self.db])
        self.assertTrue(out["ok"])
        self.assertIsNone(out["symbol"])
        self.assertEqual(out["records"], [])
        self.assertEqual(out["summary"]["records"], 3)

    def test_symbol_form_returns_records_and_null_summary(self):
        out = self._run(["sentiment-history", "--db", self.db,
                         "--symbol", "SH.600519", "--limit", "2"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["symbol"], "SH.600519")
        self.assertIsNone(out["summary"])
        self.assertEqual([r["date"] for r in out["records"]], [DATE, DATE])

    def test_empty_db_summary_is_empty_structure(self):
        empty = str(Path(self.tmp.name) / "empty.sqlite")
        conn = store.connect(empty)
        conn.close()
        out = self._run(["sentiment-history", "--db", empty])
        self.assertEqual(out["summary"],
                         {"date": None, "symbols": 0, "records": 0, "sources": {}})


class SentimentComputeTest(unittest.TestCase):
    def test_builds_command_with_symbol_and_limit(self):
        runner = fake_runner(stdout=json.dumps(
            {"ok": True, "symbol": "SH.600519", "summary": None, "records": []}))
        value = compute.sentiment_history("SH.600519", 5, runner=runner)
        self.assertEqual(value["symbol"], "SH.600519")
        self.assertEqual(runner.calls[0][0][1:],
                         ["-m", "trading_core", "sentiment-history", "--limit", "5",
                          "--symbol", "SH.600519"])

    def test_summary_form_omits_symbol_flag_and_defaults_limit_30(self):
        runner = fake_runner(stdout=json.dumps(
            {"ok": True, "symbol": None, "summary": {"date": None}, "records": []}))
        compute.sentiment_history(runner=runner)
        command = runner.calls[0][0]
        self.assertNotIn("--symbol", command)
        self.assertEqual(command[command.index("--limit") + 1], "30")

    def test_limit_out_of_range_rejected_before_spawn(self):
        runner = fake_runner()
        for bad in (0, 121, -1, "5", True, 1.5):
            with self.assertRaises(compute.ComputeError, msg=bad):
                compute.sentiment_history(None, bad, runner=runner)
        self.assertEqual(runner.calls, [], "校验失败不得起子进程")

    def test_cli_error_envelope_maps_to_compute_error(self):
        runner = fake_runner(stdout=json.dumps({"ok": False, "error": "库不可读"}),
                             returncode=1)
        with self.assertRaises(compute.ComputeError) as caught:
            compute.sentiment_history(runner=runner)
        self.assertIn("库不可读", str(caught.exception))


class SentimentHistoryBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        self.home.mkdir(parents=True)
        caches.configure(home=str(self.home))
        self.addCleanup(caches.configure)
        self.value = {"ok": True, "symbol": None, "records": [],
                      "summary": {"date": DATE, "symbols": 2, "records": 3,
                                  "sources": {"fin_sentiment": 2, "fin_news": 1}}}
        self.runner = fake_runner(stdout=json.dumps(self.value))
        self.core = {"sentiment-history": lambda symbol=None, limit=30:
                     compute.sentiment_history(symbol, limit, runner=self.runner)}

    def make_handler(self, core=None):
        return app_module.create_handler(str(self.home), analytics={}, series=None,
                                         core=self.core if core is None else core)

    def make_app(self, core=None):
        return app_module.create_app(home=str(self.home),
                                     dist=str(self.home.parent / "dist-missing"),
                                     analytics={}, core=self.core if core is None else core)

    def post(self, handler_or_client, payload):
        if isinstance(handler_or_client, TestClient):
            return handler_or_client.post("/api/wb/sentiment-history", json=payload).json()
        return handler_or_client("sentiment-history", payload)


class SentimentRouteTest(SentimentHistoryBase):
    def test_route_envelope_carries_summary_and_cache_fields(self):
        body = self.post(self.make_handler(), {})
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"], self.value)
        self.assertFalse(body["cached"])
        self.assertIn("cached_at", body)
        self.assertEqual(self.runner.calls[0][0][-1], "30", "缺省 limit 下发 30")

    def test_route_passes_symbol_and_limit(self):
        self.post(self.make_handler(), {"symbol": "SH.600519", "limit": 7})
        command = self.runner.calls[0][0]
        self.assertEqual(command[-2:], ["--symbol", "SH.600519"])
        self.assertIn("7", command)

    def test_route_whitelist_rejects_unknown_field(self):
        body = self.post(self.make_handler(), {"ticker": "SH.600519"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("Unexpected sentiment-history field", body["error"]["message"])
        self.assertEqual(self.runner.calls, [], "白名单拒绝不得起子进程")

    def test_route_limit_out_of_range_maps_to_core_unavailable(self):
        body = self.post(self.make_handler(), {"limit": 999})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/core-unavailable")
        self.assertIn("Invalid limit (1..120)", body["error"]["message"])

    def test_route_shape_mismatch_is_failure_not_cached(self):
        self.runner = fake_runner(stdout=json.dumps({"ok": True, "records": []}))
        body = self.post(self.make_handler(), {})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/core-unavailable")
        self.assertIn("载荷不完整", body["error"]["message"])

    def test_route_caches_within_ttl_and_force_refresh_bypasses(self):
        handler = self.make_handler()
        first = self.post(handler, {"symbol": "SH.600519"})
        second = self.post(handler, {"symbol": "SH.600519"})
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"], "TTL 内同载荷应命中缓存")
        self.assertEqual(len(self.runner.calls), 1, "命中缓存不得重起子进程")
        third = handler("sentiment-history", {"symbol": "SH.600519", "_refresh": True})
        self.assertFalse(third["cached"])
        self.assertEqual(len(self.runner.calls), 2, "_refresh 绕过缓存重取")

    def test_default_core_bridge_reaches_endpoint(self):
        original = compute.sentiment_history
        compute.sentiment_history = lambda symbol=None, limit=30: self.value
        self.addCleanup(setattr, compute, "sentiment_history", original)
        app = app_module.create_app(home=str(self.home),
                                    dist=str(self.home.parent / "dist-missing"),
                                    analytics={})  # core 不注入 → 默认桥
        body = self.post(TestClient(app), {})
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"]["summary"]["records"], 3)

    def test_endpoint_is_declared(self):
        self.assertIn("sentiment-history", store_access.endpoints())

    def test_query_is_read_only_on_real_db(self):
        db = str(self.home / "trading.sqlite")
        conn = store.connect(db)
        try:
            seed_rows(conn)
            before_rows = conn.execute(
                "SELECT COUNT(*) AS n FROM sentiment_snapshots").fetchone()["n"]
            before_state = store.kv_get(conn, "daemon:state", default=None)
        finally:
            conn.close()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["sentiment-history", "--db", db]), 0)
            self.assertEqual(cli.main(["sentiment-history", "--db", db,
                                      "--symbol", "SH.600519"]), 0)
        conn = store.connect(db)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) AS n FROM sentiment_snapshots").fetchone()["n"], before_rows)
            self.assertEqual(store.kv_get(conn, "daemon:state", default=None), before_state)
        finally:
            conn.close()


class SentimentMcpToolTest(SentimentHistoryBase):
    def test_tool_surface_is_61_and_includes_sentiment_history(self):
        self.assertEqual(mcp_tools.TOOL_COUNT, 61)
        self.assertEqual(len(mcp_tools.TOOLS), 61)
        self.assertIn("sentiment_history", mcp_tools.TOOL_NAMES)
        self.assertEqual(mcp_tools.ENDPOINT_TOOL_ENDPOINTS["sentiment_history"],
                         "sentiment-history")

    def test_endpoint_tool_set_equals_endpoints_minus_excluded(self):
        endpoints = store_access.endpoints()
        forwarded = set(mcp_tools.ENDPOINT_TOOL_ENDPOINTS.values())
        self.assertEqual(forwarded, set(endpoints) - mcp_tools.MCP_EXCLUDED_ENDPOINTS)

    def test_tool_fields_declare_symbol_limit_range(self):
        definition = next(t for t in mcp_tools.TOOLS if t.name == "sentiment_history")
        self.assertEqual(definition.fields, ("symbol", "limit", "refresh"))
        self.assertEqual([(p.minimum, p.maximum) for p in definition.params
                          if p.name == "limit"], [(1, 120)])
        self.assertIn("不参与信号计算", definition.description)

    def test_tool_dispatch_forwards_payload_to_handle(self):
        calls = []

        def recording(endpoint, payload):
            calls.append((endpoint, dict(payload)))
            return {"ok": True, "value": self.value}

        tool = next(t for t in mcp_tools.build_tools(recording,
                                                     mcp_tools.StoreApi(str(self.home)))
                    if t.name == "sentiment_history")
        body = mcp_tools.result_payload(tool.call({"symbol": "SH.600519", "limit": 9}))
        self.assertTrue(body["ok"])
        self.assertEqual(calls, [("sentiment-history", {"symbol": "SH.600519", "limit": 9})])


class PipelineSentimentStageTest(unittest.TestCase):
    """流程页情绪阶段：摘要追加积累事实；空库原样（不产生噪声）。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)

    def stage(self, market="SH"):
        out = pipeline.pipeline_snapshot(self.conn, self.home, date=DATE)
        return out["markets"][market]["stages"]["sentiment_snapshot"]

    def test_empty_db_keeps_stage_untouched(self):
        stage = self.stage()
        self.assertEqual(stage["status"], "pending")
        self.assertEqual(stage["summary"], "")
        self.assertEqual(stage["label"], "情绪快照")

    def test_records_add_accumulation_facts_to_summary(self):
        store.insert_sentiment(self.conn, DATE, "SH.600519", "fin_sentiment", {"x": 1})
        store.insert_sentiment(self.conn, PREV, "SH.600519", "fin_sentiment", {"x": 0})
        stage = self.stage()
        self.assertIn("已积累 2 天", stage["summary"])
        self.assertIn("连续 2 个交易日", stage["summary"])
        self.assertIn(f"最近 {DATE}", stage["summary"])
        self.assertEqual(stage["status"], "pending", "积累事实不改变作业状态归因")

    def test_alert_attribution_is_kept_before_accumulation_note(self):
        self.conn.execute(
            "INSERT INTO alerts(level,title,detail,created_at) VALUES(?,?,?,?)",
            ("warn", "情绪快照跳过", "market=SH", f"{DATE} 16:25:00"))
        self.conn.commit()
        store.insert_sentiment(self.conn, DATE, "SH.600519", "fin_sentiment", {"x": 1})
        stage = self.stage()
        self.assertEqual(stage["status"], "skipped")
        self.assertTrue(stage["summary"].startswith("情绪快照跳过；"),
                        f"归因文案在前、积累事实在后：{stage['summary']}")


if __name__ == "__main__":
    unittest.main()
