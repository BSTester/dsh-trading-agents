"""只读 factors 的历史隔离与 HTTP/MCP → compute → CLI 契约；全程离线、不落盘。"""
import asyncio
import contextlib
from datetime import date, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
for directory in (ROOT / "platform", ROOT / "plugins/core/python",
                  ROOT / "plugins/datasource/python"):
    sys.path.insert(0, str(directory))

from fastapi.testclient import TestClient
from server import app as app_module
from server import caches, compute, mcp_tools, v3_mcp

SCRIPT = ROOT / "plugins/workbench/python/factors.py"
spec = importlib.util.spec_from_file_location("workbench_factors_as_of_test", SCRIPT)
factors = importlib.util.module_from_spec(spec)
with patch.object(sys, "path", list(sys.path)):
    spec.loader.exec_module(factors)

TICKERS = ["600519", "000001", "601318"]
DAY = "2024-04-09"  # 自 1 月 1 日起，第 100 根 fixture 日线。
PRICE_KEYS = ("mom_20", "mom_60", "vol_20", "trend", "rsi_14", "liq_ratio", "mdd_60")
VALUATION_KEYS = ("pe_ttm", "pb", "peg", "ps", "pe_ttm_pct", "pb_pct", "ps_pct")
INVALID_STRINGS = (
    "2024-02-30", "2023-02-29", "2024-13-01", "0000-01-01",
    "2024-04-09T12:00:00", "2024-04-09junk", "2024-04-09\n",
    " 2024-04-09", "2024-04-09 ", "2024-4-09", "20240409", " ",
)
INVALID_VALUES = (*INVALID_STRINGS, 20240409, 0, False, True, [], {}, date(2024, 4, 9))


def bars(count=100, slope=1):
    return [{"t": (date(2024, 1, 1) + timedelta(days=i)).isoformat(),
             "c": 100 + slope * i + (i % 7) * 0.2, "v": 1000 + i * slope}
            for i in range(count)]


def fixture_load(ticker, period, limit):
    return bars(110, TICKERS.index(ticker) + 1)[-limit:], "fixture-bars", False


def run_cli(args):
    stdout, stderr = io.StringIO(), io.StringIO()
    with patch.object(sys, "argv", [str(SCRIPT), *args]), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = factors.main()
        except SystemExit as error:
            code = error.code
    return SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


class HistoricalSnapshotTests(unittest.TestCase):
    def test_explicit_date_never_calls_live_valuation(self):
        with patch.object(factors, "load_bars", side_effect=fixture_load), \
                patch.object(factors, "valuation_values", side_effect=AssertionError("live valuation")) as live:
            result = factors.snapshot(TICKERS, 250, as_of=DAY)
        # snapshot 旧代码会吞掉 AssertionError，因此还要明确钉住零调用。
        live.assert_not_called()
        self.assertEqual(result["pit"], {"asOf": DAY, "mode": "inclusive"})
        self.assertEqual(result["sources"], ["fixture-bars"])

    def test_raw_zscores_and_composite_exclude_live_valuation(self):
        def live(ticker):
            return {key: (TICKERS.index(ticker) + 1) * 100 for key in VALUATION_KEYS}, "live"

        with patch.object(factors, "load_bars", side_effect=fixture_load), \
                patch.object(factors, "valuation_values", side_effect=live):
            result = factors.snapshot(TICKERS, 250, as_of=DAY)
        self.assertNotIn("live", result["sources"])
        for row in result["rows"]:
            for key in VALUATION_KEYS:
                self.assertIsNone(row["factors"].get(key))
                self.assertIsNone(row["z"].get(key))
            expected = round(sum(row["z"][key] * factors.FACTOR_SIGN[key]
                                 for key in PRICE_KEYS) / len(PRICE_KEYS), 4)
            self.assertEqual(row["score"], expected)
        for text in ("估值", "无 PIT", "未并入", "最近", "历史"):
            self.assertIn(text, result["note"])

    def test_future_append_does_not_change_same_visible_window(self):
        history = {ticker: bars(100, i + 1) for i, ticker in enumerate(TICKERS)}
        extended = {ticker: values + [dict(bar, c=999999, v=999999)
                                     for bar in bars(115)[100:]] + [{"c": 999999, "v": 999999}]
                    for ticker, values in history.items()}
        with patch.object(factors, "valuation_values", return_value=({}, "")):
            with patch.object(factors, "load_bars", side_effect=lambda t, p, n: (history[t], "fixture", False)):
                before = factors.snapshot(TICKERS, 250, as_of=DAY)
            with patch.object(factors, "load_bars", side_effect=lambda t, p, n: (extended[t], "fixture", False)):
                after = factors.snapshot(TICKERS, 250, as_of=DAY)
        self.assertEqual(before, after)
        for row in after["rows"]:
            self.assertEqual(row["as_of"], DAY)
            expected = factors.factor_values(history[row["ticker"]])
            for key, value in expected.items():
                self.assertEqual(row["factors"][key], round(value, 5))

    def test_latest_and_empty_date_keep_valuation_including_zero(self):
        def live(ticker):
            return {key: TICKERS.index(ticker) for key in VALUATION_KEYS}, "fixture-valuation"

        with patch.object(factors, "load_bars", side_effect=fixture_load), \
                patch.object(factors, "valuation_values", side_effect=live) as provider:
            latest = factors.snapshot(TICKERS, 250)
            self.assertEqual(provider.call_count, len(TICKERS))
            self.assertEqual(factors.snapshot(TICKERS, 250, as_of=""), latest)
        first = next(row for row in latest["rows"] if row["ticker"] == TICKERS[0])
        self.assertEqual(first["factors"]["pe_ttm"], 0)
        self.assertIsNotNone(first["z"]["pe_ttm"])
        self.assertIn("fixture-valuation", latest["sources"])
        self.assertNotIn("pit", latest)

    def test_historical_zero_is_not_missing(self):
        flat = [{"t": row["t"], "c": 100, "v": 1000} for row in bars()]
        with patch.object(factors, "load_bars", return_value=(flat, "fixture", False)), \
                patch.object(factors, "valuation_values", return_value=({}, "")):
            result = factors.snapshot(TICKERS, 250, as_of=DAY)
        for row in result["rows"]:
            for key in ("mom_20", "mom_60", "vol_20", "trend", "mdd_60"):
                self.assertEqual(row["factors"][key], 0)
                self.assertEqual(row["z"][key], 0)
            self.assertEqual(row["score"], 0)

    def test_65_visible_bars_are_required(self):
        with patch.object(factors, "load_bars", return_value=(bars(65), "fixture", False)):
            visible, _ = factors.closes_volumes("600519", 250, as_of="2024-03-05")
            self.assertEqual(len(visible), 65)
            with self.assertRaisesRegex(RuntimeError, "日线不足.*64"):
                factors.closes_volumes("600519", 250, as_of="2024-03-04")

    def test_recent_window_exhaustion_reports_missing_history(self):
        # 真实 load_bars 只给最近 limit 根；追加未来后旧窗口可能已被挤出，不能伪造历史。
        with patch.object(factors, "load_bars", side_effect=lambda t, p, n: (bars(200)[-n:], "fixture", False)), \
                patch.object(factors, "valuation_values", side_effect=AssertionError("live")) as live:
            with self.assertRaisesRegex(RuntimeError, "不足.*最近.*历史"):
                factors.snapshot(TICKERS, 80, as_of=DAY)
        live.assert_not_called()

    def test_partial_missing_history_remains_a_failure_not_a_filled_row(self):
        def load(ticker, period, limit):
            return bars(64 if ticker == TICKERS[2] else 100), "fixture", False

        with patch.object(factors, "load_bars", side_effect=load), \
                patch.object(factors, "valuation_values", return_value=({}, "")):
            result = factors.snapshot(TICKERS, 250, as_of=DAY)
        self.assertEqual(set(result["tickers"]), set(TICKERS[:2]))
        self.assertIn("不足", result["failures"][TICKERS[2]])
        self.assertIn("历史", result["note"])


class DateValidationTests(unittest.TestCase):
    def test_compute_rejects_noncanonical_dates_and_nonstrings(self):
        for value in INVALID_VALUES:
            with self.subTest(value=value), self.assertRaisesRegex(compute.PayloadError, "as_of"):
                compute._factors_args({"tickers": TICKERS, "as_of": value}, False)

    def test_compute_preserves_valid_dates_and_missing_defaults(self):
        base = ["snapshot", "--tickers", ",".join(TICKERS), "--window", "250"]
        self.assertEqual(compute._factors_args({"tickers": TICKERS}, False), base)
        for value in (None, ""):
            self.assertEqual(compute._factors_args({"tickers": TICKERS, "as_of": value}, False), base)
        self.assertEqual(compute._factors_args({"tickers": TICKERS, "as_of": "2024-02-29"}, False),
                         base + ["--as-of", "2024-02-29"])

    def test_snapshot_rejects_invalid_date_before_any_data_access(self):
        with patch.object(factors, "load_bars", side_effect=AssertionError("unexpected bars")) as load, \
                patch.object(factors, "valuation_values", side_effect=AssertionError("unexpected valuation")) as live:
            for value in INVALID_VALUES:
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "as_of"):
                    factors.snapshot(TICKERS, 250, as_of=value)
            load.assert_not_called()
            live.assert_not_called()

    def test_cli_rejects_invalid_dates_before_dispatch(self):
        with patch.object(factors, "snapshot", return_value={}) as snapshot:
            for value in INVALID_STRINGS:
                with self.subTest(value=value):
                    result = run_cli(["snapshot", "--tickers", ",".join(TICKERS), "--as-of", value])
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("as_of", result.stdout + result.stderr)
            snapshot.assert_not_called()

    def test_cli_valid_and_empty_dates_keep_defaults(self):
        with patch.object(factors, "snapshot", return_value={}) as snapshot:
            for flags, expected in (([], None), (["--as-of", ""], None),
                                    (["--as-of", "2024-02-29"], "2024-02-29")):
                result = run_cli(["snapshot", "--tickers", ",".join(TICKERS), *flags])
                self.assertEqual(result.returncode, 0)
                snapshot.assert_called_with(TICKERS, 250, as_of=expected)

    def test_standalone_cli_date_validation_does_not_import_server(self):
        # 真子进程；只替代数据源边界，不能碰行情、DB、缓存，也不能依赖整个 server。
        code = '''import builtins, runpy, sys, types
original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == "server" or name.startswith("server."):
        raise AssertionError("CLI imported server")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
market = types.ModuleType("trading_datasource.market")
def no_load(*args, **kwargs):
    raise AssertionError("unexpected data access")
market.load_bars = no_load
sys.modules["trading_datasource"] = types.ModuleType("trading_datasource")
sys.modules["trading_datasource.market"] = market
sys.argv = [sys.argv[1], "snapshot", "--tickers", "600519,000001", "--as-of", "2024-02-30"]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        result = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPT)],
                                capture_output=True, text=True, timeout=10, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("as_of", result.stdout + result.stderr)
        self.assertNotIn("CLI imported server", result.stdout + result.stderr)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.commands = []
        self.cache = caches.TtlCache(now=lambda: 1000)
        for patcher in (
            patch.dict(os.environ, {"QUANT_ALERTS_SAMPLER": "0"}),
            patch("sqlite3.connect", side_effect=AssertionError("database access forbidden")),
            patch.object(self.cache, "_ensure_dir", return_value=False),
            patch.object(caches, "_CACHE", self.cache),
            patch.object(app_module.v3_db, "init_db", return_value={"ok": True}),
            patch.object(app_module.trading, "TradeGate", return_value=object()),
            patch.object(factors, "load_bars", side_effect=fixture_load),
            patch.object(factors, "valuation_values", side_effect=AssertionError("live valuation")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        def runner(command, timeout):
            self.commands.append(list(command))
            self.assertEqual(command[1], str(SCRIPT))
            return run_cli(command[2:])

        self.providers = compute.analytics_providers(runner, now=lambda: 1000)
        self.scheduler = Mock()
        with patch("threading.Thread.start") as thread_start:
            self.app = app_module.create_app(home="/factors-as-of-tests-no-io", config={},
                                             analytics=self.providers, core={},
                                             scheduler=self.scheduler, futu=object(), push=object(),
                                             mcp_surface="direct")
        self.registration_thread_start = thread_start
        # 不进入 lifespan：不启动调度、WS 或真实 MCP 网络会话。
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def post(self, **payload):
        return self.client.post("/api/wb/factors", json={"tickers": TICKERS, **payload}).json()

    def test_registration_does_not_start_background_threads(self):
        self.registration_thread_start.assert_not_called()

    def test_http_through_compute_and_cli(self):
        result = self.post(as_of=DAY)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"]["pit"]["asOf"], DAY)
        self.assertEqual(self.commands[0][-2:], ["--as-of", DAY])
        self.scheduler.start.assert_not_called()

    def test_different_dates_do_not_share_inner_or_outer_cache(self):
        first = self.post(as_of="2024-04-08")
        second = self.post(as_of=DAY)
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertNotEqual(first["value"]["rows"], second["value"]["rows"])
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(len(self.cache._memory), 2)
        self.assertNotEqual(caches.cache_key("factors", {"as_of": DAY}),
                            caches.cache_key("factors", {"as_of": "2024-04-08"}))
        again = self.post(as_of="2024-04-08")
        self.assertTrue(again["cached"])
        self.assertEqual(again["value"], first["value"])
        # 强制绕过外层后，compute 内层仍按各自日期复用。
        forced = self.post(as_of=DAY, _refresh=True)
        self.assertEqual(forced["value"], second["value"])
        self.assertEqual(len(self.commands), 2)

    def test_http_rejects_bad_dates_before_cli(self):
        for value in INVALID_VALUES[:-1]:  # date 对象不是 JSON 值，已由 compute 单测覆盖。
            with self.subTest(value=value):
                result = self.post(as_of=value)
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "trading/invalid-operation")
                self.assertIn("as_of", result["error"]["message"])
        self.assertEqual(self.commands, [])

    def test_unknown_fields_still_rejected_by_http_and_mcp(self):
        result = self.post(as_of=DAY, unexpected=True)
        self.assertFalse(result["ok"])
        self.assertIn("Unexpected factors field", result["error"]["message"])
        tool = self.app.state.mcp._tool_manager.get_tool("factors")
        with self.assertRaises(ValueError):
            tool.fn_metadata.arg_model.model_validate({"tickers": TICKERS, "unexpected": True})
        self.assertEqual(self.commands, [])

    def test_mcp_factors_schema_exposes_optional_as_of(self):
        tools = asyncio.run(self.app.state.mcp.list_tools())
        schema = next(tool.input_schema for tool in tools if tool.name == "factors")
        self.assertIn("as_of", schema["properties"])
        self.assertNotIn("as_of", schema.get("required", []))
        self.assertFalse(schema["additionalProperties"])

    def test_mcp_through_compute_and_cli(self):
        # 必须经过真实发布的参数模型，不能直接调 fn(**kwargs) 绕过 schema 伪装透传成功。
        from mcp.server.mcpserver.exceptions import ToolError
        try:
            result = asyncio.run(self.app.state.mcp.call_tool(
                "factors", {"tickers": TICKERS, "as_of": DAY}))
        except ToolError as error:
            self.fail(f"MCP factors 未能透传 as_of: {error}")
        self.assertFalse(result.is_error, result)
        envelope = mcp_tools.result_payload(result)
        self.assertTrue(envelope["ok"], envelope)
        self.assertEqual(envelope["value"]["pit"]["asOf"], DAY)
        self.assertEqual(self.commands[0][-2:], ["--as-of", DAY])

    def test_ic_schema_and_http_remain_closed_to_as_of(self):
        tools = asyncio.run(self.app.state.mcp.list_tools())
        schema = next(tool.input_schema for tool in tools if tool.name == "ic")
        self.assertNotIn("as_of", schema["properties"])
        result = self.client.post("/api/wb/ic", json={"tickers": TICKERS, "as_of": DAY}).json()
        self.assertFalse(result["ok"])
        self.assertIn("Unexpected ic field", result["error"]["message"])
        self.assertNotIn("--as-of", compute._ic_args({"tickers": TICKERS}, False))
        self.assertEqual(self.commands, [])


class ParameterDocumentationTests(unittest.TestCase):
    def test_strategy_tool_docs_match_return_and_screen_contracts(self):
        event_doc = v3_mcp.TOOL_DOCS["/api/v3/strategies/event-study"]
        self.assertNotIn("CAR", event_doc)
        self.assertIn("returnCurve", event_doc)
        self.assertIn("无基准", event_doc)
        arb_doc = v3_mcp.TOOL_DOCS["/api/v3/strategies/stat-arb"]
        self.assertNotIn("cointegrated", arb_doc)
        self.assertNotIn("numpy", arb_doc)
        self.assertIn("screenPassed", arb_doc)
        self.assertIn("非校准", arb_doc)

    def test_factors_date_docs_disclose_latest_default_and_no_pit_valuation(self):
        doc = v3_mcp.PARAM_DOCS["as_of"]
        for text in ("YYYY-MM-DD", "factors", "最新", "估值", "PIT", "未并入", "最近"):
            self.assertIn(text, doc)


if __name__ == "__main__":
    unittest.main()
