"""统一数据层的离线回归测试。

这组测试专门锁住本次重构的两个目标：
  1. **只有一个实现** —— 行情路由、富途 MCP 客户端、回测核心都不得再出现副本；
     副本的真正危害不是重复代码，而是「修了一处、漏了另一处」的静默漂移。
  2. **缺失时可预期地降级** —— 共享层不在、情绪渠道不在、凭证不在时，
     必须明确报「不可用」，绝不用占位数据冒充真实数据。

全部离线：不访问网络、不启动浏览器、不读写真实台账。
"""
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DATASOURCE_PYTHON = ROOT / "plugins" / "datasource" / "python"
sys.path.insert(0, str(DATASOURCE_PYTHON))
sys.path.insert(0, str(ROOT / "plugins" / "engine" / "python"))   # engine.py 需要同级 risk_config

from trading_datasource import futu_mcp, locate, market  # noqa: E402


def load_module(name, path):
    """按文件路径加载模块（模拟插件里 `import backtest` / `import bars` 的用法）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SingleImplementationTests(unittest.TestCase):
    """同一个能力不允许存在第二份实现。"""

    def test_market_router_exists_only_in_shared_layer(self):
        """行情路由只能有一份 —— 旧的两份副本（bars.py / market_data.py）必须消失。"""
        self.assertFalse((ROOT / "plugins" / "engine" / "python" / "market_data.py").exists(),
                         "engine/market_data.py 是已被合并的副本，不应再存在")
        for text in (ROOT / "plugins" / "engine" / "python" / "engine.py",
                     ROOT / "plugins" / "workbench" / "python" / "bars.py"):
            content = text.read_text(encoding="utf-8")
            self.assertNotIn("def load_bars", content,
                             f"{text.name} 不应再自带 load_bars 实现")
            self.assertIn("trading_datasource", content,
                          f"{text.name} 应改为依赖统一数据层")

    def test_backtest_implementation_exists_only_in_shared_layer(self):
        self.assertFalse((ROOT / "plugins" / "workbench" / "python" / "backtest.py").exists(),
                         "workbench/python/backtest.py 是仅差一行 import 的副本，不应再存在")
        canonical = DATASOURCE_PYTHON / "trading_datasource" / "backtest.py"
        self.assertTrue(canonical.is_file())
        engine_shim = (ROOT / "plugins" / "engine" / "python" / "backtest.py").read_text(encoding="utf-8")
        self.assertLess(len(engine_shim.splitlines()), 30,
                        "engine 的 backtest.py 应是薄入口，不应再含实现")

    def test_futu_mcp_client_exists_only_in_shared_layer(self):
        """富途 JSON-RPC 握手此前有 4 份实现，现在只允许共享层持有。"""
        self.assertFalse((ROOT / "plugins" / "workbench" / "python" / "futu_client.py").exists())
        offenders = []
        for path in (ROOT / "plugins").rglob("*.py"):
            if "datasource" in path.parts or "__pycache__" in path.parts:
                continue
            if "mcp.futunn.com" in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"这些文件仍在自建 MCP 端点调用：{offenders}")

    def test_engine_and_workbench_resolve_the_same_router(self):
        """两个插件最终拿到的必须是同一个函数对象。"""
        sys.path.insert(0, str(ROOT / "plugins" / "workbench" / "python"))
        sys.path.insert(0, str(ROOT / "plugins" / "engine" / "python"))
        workbench_bars = load_module("wb_bars_check", ROOT / "plugins" / "workbench" / "python" / "bars.py")
        self.assertIs(workbench_bars.load_bars, market.load_bars,
                      "工作台 CLI 必须直接复用共享层的 load_bars")

    def test_shared_backtest_matches_engine_shim(self):
        """engine 的兼容入口必须再导出同一批对象，否则旧调用方会静默拿到别的东西。"""
        shim = load_module("engine_backtest_shim", ROOT / "plugins" / "engine" / "python" / "backtest.py")
        from trading_datasource import backtest as canonical
        for name in ("load_data", "validate_data", "ma_cross_signal", "rsi_signal", "run", "metrics"):
            self.assertIs(getattr(shim, name), getattr(canonical, name), name)
        self.assertEqual(shim.COMMISSION, canonical.COMMISSION)


class RoutingTests(unittest.TestCase):
    """渠道优先级只定义一次，且行为与文档一致。"""

    def labels(self, ticker, period, limit):
        return [label for label, _ in market.route(ticker, period, limit)]

    def test_minute_prefers_futu_for_every_market(self):
        """富途优先；A 股额外保留新浪备用源（akshare 支持分钟），港美股没有该通道。"""
        self.assertEqual(self.labels("600519", "5m", 300), ["futu", "sina"])
        for ticker in ("00700.HK", "AAPL"):
            self.assertEqual(self.labels(ticker, "5m", 300), ["futu"], ticker)

    def test_a_share_long_history_goes_sina_first(self):
        """A 股请求超过富途单次上限时，先走新浪长历史。"""
        self.assertEqual(self.labels("600519", "1d", market.FUTU_MAX_BARS + 1), ["sina", "futu"])
        self.assertEqual(self.labels("600519", "1d", market.FUTU_MAX_BARS), ["futu", "sina"])

    def test_non_a_share_has_no_sina_fallback(self):
        """港美股没有 A 股专属的新浪通道，不应假装有备用源。"""
        self.assertEqual(self.labels("00700.HK", "1d", 900), ["futu"])
        self.assertEqual(self.labels("AAPL", "1d", 900), ["futu"])

    def test_load_bars_rejects_unknown_period(self):
        with self.assertRaises(ValueError):
            market.load_bars("600519", "3s", 100)

    def test_load_bars_falls_back_to_cache_and_marks_stale(self):
        cached = {"bars": [{"t": "2026-01-01", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}],
                  "source": "futu/quote_history_kline"}
        with patch.object(market, "fetch_futu", side_effect=RuntimeError("boom")):
            bars, source, stale = market.load_bars("AAPL", "1d", 30, cached=cached)
        self.assertEqual(bars, cached["bars"])
        self.assertTrue(stale, "全部数据源失败时必须标记 stale")
        self.assertIn("缓存", source)

    def test_load_bars_raises_when_every_source_fails(self):
        """没有缓存又全部失败时，必须报错 —— 不能返回空列表冒充「无行情」。"""
        with patch.object(market, "fetch_futu", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                market.load_bars("AAPL", "1d", 30)

    def test_futu_failure_falls_through_to_sina(self):
        calls = []

        def fake_sina(ticker, period, limit):
            calls.append("sina")
            return [{"t": "2026-01-01", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}], "akshare/sina"

        with patch.object(market, "fetch_futu", side_effect=RuntimeError("futu down")), \
             patch.object(market, "fetch_a_share", side_effect=fake_sina):
            bars, source, stale = market.load_bars("600519", "1d", 30)
        self.assertEqual(calls, ["sina"])
        self.assertEqual(source, "akshare/sina")
        self.assertFalse(stale)


class FutuClientTests(unittest.TestCase):
    """共享客户端的错误语义：可降级 vs 必须上报。"""

    def test_missing_token_is_unavailable(self):
        with patch.object(futu_mcp, "token_path", return_value=Path("/nonexistent/futu-token")):
            with self.assertRaises(futu_mcp.FutuUnavailable):
                futu_mcp.call_tool("quote_history_kline", {})

    def test_empty_result_text_is_not_an_error(self):
        """实测 account_funds 无数据时返回文本 "no data" —— 合法空结果，不是结构异常。"""
        payload = {"jsonrpc": "2.0", "id": 2,
                   "result": {"content": [{"type": "text", "text": "no data"}]}}
        self.assertEqual(futu_mcp._unwrap("account_funds", payload), {})

    def test_non_empty_non_json_text_is_reported(self):
        payload = {"jsonrpc": "2.0", "id": 2,
                   "result": {"content": [{"type": "text", "text": "<html>gateway</html>"}]}}
        with self.assertRaises(futu_mcp.FutuUnavailable):
            futu_mcp._unwrap("quote_history_kline", payload)

    def test_business_error_carries_ret_code(self):
        payload = {"jsonrpc": "2.0", "id": 2, "result": {"content": [
            {"type": "text", "text": '{"ret_code": -9, "ret_msg": "no permission"}'}]}}
        with self.assertRaises(futu_mcp.FutuUnavailable) as caught:
            futu_mcp._unwrap("quote_history_kline", payload)
        self.assertIn("-9", str(caught.exception))

    def test_probe_reports_unavailable_without_token(self):
        with patch.object(futu_mcp, "has_token", return_value=False):
            ok, detail = futu_mcp.probe()
        self.assertFalse(ok)
        self.assertIn("未授权", detail)

    def test_probe_detects_expired_token_instead_of_reporting_valid(self):
        """实测 token 过期时 initialize 仍成功、所有 tools/call 报 internal error。

        旧探测只做 initialize，因此长期误报「token 有效」。这里锁住正确行为。
        """
        with patch.object(futu_mcp, "has_token", return_value=True), \
             patch.object(futu_mcp, "call_tool",
                          side_effect=futu_mcp.FutuUnavailable("quote_trading_days: internal error")):
            ok, detail = futu_mcp.probe()
        self.assertFalse(ok, "internal error 不能被当作「token 有效」")
        self.assertIn("续期", detail)

    def test_probe_returns_none_when_network_is_down(self):
        """网络不可达是「未知」，不能误报成 token 过期。"""
        with patch.object(futu_mcp, "has_token", return_value=True), \
             patch.object(futu_mcp, "call_tool",
                          side_effect=futu_mcp.FutuUnavailable("MCP 请求失败：timeout")):
            ok, detail = futu_mcp.probe()
        self.assertIsNone(ok)
        self.assertIn("网络", detail)

    def test_probe_succeeds_on_real_tool_call(self):
        with patch.object(futu_mcp, "has_token", return_value=True), \
             patch.object(futu_mcp, "call_tool", return_value={"trading_days": []}):
            ok, _detail = futu_mcp.probe()
        self.assertTrue(ok)


class LocateTests(unittest.TestCase):
    """跨插件定位：找不到就返回 None，让调用方明确降级。"""

    def test_repo_layout_finds_fin_data(self):
        found = locate.find_script("fin-data", "fin_sentiment.py")
        self.assertIsNotNone(found)
        self.assertTrue(found.is_file())

    def test_missing_plugin_returns_none(self):
        with patch.object(locate, "_REPO_PLUGINS", Path("/nonexistent/plugins")), \
             patch.object(locate, "unified_root", return_value=Path("/nonexistent/unified")):
            self.assertIsNone(locate.plugin_python_dir("fin-data"))
            self.assertIsNone(locate.find_script("fin-data", "fin_sentiment.py"))

    def test_missing_script_returns_none(self):
        self.assertIsNone(locate.find_script("fin-data", "no_such_script.py"))

    def test_run_script_returns_none_when_script_missing(self):
        with patch.object(locate, "find_script", return_value=None):
            self.assertIsNone(locate.run_script("fin-data", "fin_sentiment.py"))

    def test_unified_root_env_override(self):
        with patch.dict(os.environ, {"DSH_TRADING_PYTHON": "/tmp/custom-root"}):
            self.assertEqual(locate.unified_root(), Path("/tmp/custom-root"))

    def test_python_executable_prefers_trading_venv(self):
        executable = locate.python_executable()
        self.assertTrue(os.path.isabs(executable))


class QuantSentimentTests(unittest.TestCase):
    """量化侧接入 fin-data 情绪渠道：并列输入，不参与信号计算。"""

    def _engine(self):
        return load_module("engine_under_test", ROOT / "plugins" / "engine" / "python" / "engine.py")

    def test_sentiment_reports_unavailable_without_channel(self):
        engine = self._engine()
        with patch.object(locate, "find_script", return_value=None):
            result = engine.read_sentiment("600519")
        self.assertFalse(result["available"])
        self.assertIn("reason", result)

    def test_sentiment_summarizes_shared_channel_output(self):
        engine = self._engine()
        payload = {"sources_status": {"x": "ok:api/graphql", "reddit": "ok:api/json"},
                   "x": {"items": [{"text": "a"}] * 3},
                   "reddit": {"items": [{"title": "b"}] * 2}}
        with patch.object(locate, "find_script", return_value=Path("fin_sentiment.py")), \
             patch.object(locate, "run_script", return_value=payload):
            result = engine.read_sentiment("AAPL")
        self.assertTrue(result["available"])
        self.assertEqual(result["x_count"], 3)
        self.assertEqual(result["reddit_count"], 2)
        self.assertEqual(result["sources_status"]["x"], "ok:api/graphql")

    def test_sentiment_failure_does_not_raise(self):
        """渠道故障不能打断信号计算 —— 情绪只是附加参考。"""
        engine = self._engine()
        with patch.object(locate, "find_script", return_value=Path("fin_sentiment.py")), \
             patch.object(locate, "run_script", return_value=None):
            result = engine.read_sentiment("AAPL")
        self.assertFalse(result["available"])

    def test_signal_is_unchanged_by_sentiment(self):
        """关键性质：带不带情绪，信号与 ATR 必须完全一致（否则回测不可复现）。"""
        engine = self._engine()
        frame = _synthetic_frame()
        payload = {"sources_status": {"x": "ok:api/graphql"}, "x": {"items": []}, "reddit": {"items": []}}
        with patch.object(engine, "load_data", return_value=frame):
            plain = engine.compute_signal("AAPL", "ma_cross")
            with patch.object(locate, "find_script", return_value=Path("fin_sentiment.py")), \
                 patch.object(locate, "run_script", return_value=payload):
                enriched = engine.compute_signal("AAPL", "ma_cross", include_sentiment=True)
        for field in ("signal", "price", "atr", "date", "strategy"):
            self.assertEqual(plain[field], enriched[field], field)
        self.assertNotIn("sentiment", plain)
        self.assertTrue(enriched["sentiment"]["available"])

    def test_cli_flag_is_opt_in(self):
        """默认不取情绪 —— 否则每次信号都多花约 10 秒并依赖外部渠道。"""
        source = (ROOT / "plugins" / "engine" / "python" / "engine.py").read_text(encoding="utf-8")
        self.assertIn('"--sentiment"', source)
        self.assertIn("include_sentiment=getattr(args, \"sentiment\", False)", source)


def _synthetic_frame():
    """构造满足 validate_data 要求的日线数据（离线）。"""
    import pandas as pd
    days = pd.bdate_range("2024-01-01", periods=120)
    closes = [10 + (index % 7) * 0.5 + index * 0.05 for index in range(len(days))]
    return pd.DataFrame({
        "date": days.strftime("%Y-%m-%d"),
        "open": closes, "close": closes,
        "high": [value + 0.4 for value in closes],
        "low": [value - 0.4 for value in closes],
        "volume": [1000] * len(days),
    })


if __name__ == "__main__":
    unittest.main(verbosity=2)
