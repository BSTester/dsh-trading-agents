"""统一数据层的离线回归测试。

这组测试专门锁住本次重构的两个目标：
  1. **只有一个实现** —— 行情路由、富途 MCP 客户端、回测核心都不得再出现副本；
     副本的真正危害不是重复代码，而是「修了一处、漏了另一处」的静默漂移。
  2. **缺失时可预期地降级** —— 共享层不在、情绪渠道不在、凭证不在时，
     必须明确报「不可用」，绝不用占位数据冒充真实数据。

全部离线：不访问网络、不启动浏览器、不读写真实台账。
"""
import importlib.util
import json
import os
import sys
import unittest
from datetime import date, timedelta
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

    def test_a_share_detection_exists_only_in_shared_layer(self):
        """A 股判定只能有一份实现。

        此前 events.py / fin_news.py / fin_sentiment.py / fundamentals.py 各写一份
        `re.fullmatch(r"\d{6}", ticker.split(".")[0])`：全都忽略显式市场标注，
        于是 000001.HK（港股长和）被当成 A 股平安银行。
        """
        offenders = []
        for path in (ROOT / "plugins").rglob("*.py"):
            if "datasource" in path.parts or "__pycache__" in path.parts:
                continue
            if "is_a_share" in path.read_text(encoding="utf-8") and "def is_a_share" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "这些文件自带了 is_a_share，应 import 共享实现")

    def test_yahoo_symbol_conversion_exists_only_in_shared_layer(self):
        """富途↔Yahoo 符号归一也只能有一份（港股 4 位 vs 5 位最容易写错）。"""
        offenders = []
        for path in (ROOT / "plugins").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if path.name == "market.py" and "datasource" in path.parts:
                continue
            if "def to_yahoo_symbol" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], "这些文件自带了 to_yahoo_symbol，应 import 共享实现")

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
        for ticker in ("00700.HK", "AAPL"):
            self.assertNotIn("sina", self.labels(ticker, "1d", 900), ticker)
            self.assertNotIn("sina", self.labels(ticker, "1d", 250), ticker)

    def test_non_a_share_long_history_goes_yahoo_first(self):
        """港美股超过富途单次上限时走 Yahoo 长历史（富途最多 370 根，给不了）。"""
        for ticker in ("00700.HK", "AAPL"):
            self.assertEqual(self.labels(ticker, "1d", market.FUTU_MAX_BARS + 1),
                             ["yahoo", "futu"], ticker)
            self.assertEqual(self.labels(ticker, "1d", market.FUTU_MAX_BARS),
                             ["futu", "yahoo"], ticker)

    def test_yahoo_channel_is_daily_only(self):
        """Yahoo 没有分钟数据，不得把必然失败的通道挂在分钟级上。"""
        for ticker in ("00700.HK", "AAPL"):
            self.assertEqual(self.labels(ticker, "5m", 300), ["futu"], ticker)

    def test_a_share_is_detected_by_explicit_market_not_digit_count(self):
        """000001.HK 是港股长和，不是平安银行。

        此前 is_a_share 只看点号前的数字，6 位港股代码被判定成 A 股：
        回测被路由到新浪、取到 sz000001 的行情，静默跑了一遍另一个标的。
        """
        for ticker in ("00700.HK", "000001.HK", "00001.HK", "09988.HK", "AAPL", "TSLA"):
            self.assertFalse(market.is_a_share(ticker), ticker)
        for ticker in ("600519", "000001", "000001.SZ", "688981", "SH.600519", "300750"):
            self.assertTrue(market.is_a_share(ticker), ticker)

    def test_yahoo_symbol_conversion(self):
        """Yahoo 的港股是 4 位代码，直接拿 5 位去查会返回空。"""
        self.assertEqual(market.to_yahoo_symbol("00700.HK"), "0700.HK")
        self.assertEqual(market.to_yahoo_symbol("00001.HK"), "0001.HK")
        self.assertEqual(market.to_yahoo_symbol("09988.HK"), "9988.HK")
        self.assertEqual(market.to_yahoo_symbol("AAPL"), "AAPL")
        self.assertEqual(market.to_yahoo_symbol("600519"), "600519.SS")
        self.assertEqual(market.to_yahoo_symbol("000001"), "000001.SZ")

    def test_load_bars_rejects_unknown_period(self):
        with self.assertRaises(ValueError):
            market.load_bars("600519", "3s", 100)

    def test_load_bars_falls_back_to_cache_and_marks_stale(self):
        cached = {"bars": [{"t": "2026-01-01", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}],
                  "source": "futu/quote_history_kline"}
        # 路由里港美股日线有 futu 与 yahoo 两条通道，必须都失败才谈得上"全部失败"
        with patch.object(market, "fetch_futu", side_effect=RuntimeError("boom")), \
                patch.object(market, "fetch_yahoo", side_effect=RuntimeError("boom")):
            bars, source, stale = market.load_bars("AAPL", "1d", 30, cached=cached)
        self.assertEqual(bars, cached["bars"])
        self.assertTrue(stale, "全部数据源失败时必须标记 stale")
        self.assertIn("缓存", source)

    def test_load_bars_raises_when_every_source_fails(self):
        """没有缓存又全部失败时，必须报错 —— 不能返回空列表冒充「无行情」。"""
        with patch.object(market, "fetch_futu", side_effect=RuntimeError("boom")), \
                patch.object(market, "fetch_yahoo", side_effect=RuntimeError("boom")):
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


def _futu_daily_row(yyyymmdd, close):
    """构造富途 quote_history_kline 的一行日线（date 为 8 位数字串的实测形状）。"""
    return {"date": yyyymmdd, "open": close - 1, "high": close + 1,
            "low": close - 2, "close": close, "volume": 1000}


class RawChunkTests(unittest.TestCase):
    """load_raw_bars（K1 方案 A）：富途 ≤370 根分块向后翻页取**原始价**。

    背景：回填此前经 load_bars 长历史路由拿到的是复权价（新浪 qfq / Yahoo
    auto_adjusted），违反规格 §4.2 规则 2「落库一律原始价 + 因子表」——
    这里锁住分块合并、按 t 去重、游标逐块前移与三种终止路径。
    全部离线：假 call_tool 按页发数。
    """

    def _patched_pages(self, pages):
        """按调用次序发页；记录每次调用的 (tool 名, 参数)。"""
        calls = []

        def fake_call_tool(name, args, **kwargs):
            calls.append((name, dict(args)))
            return pages[len(calls) - 1]

        return patch.object(market, "call_tool", side_effect=fake_call_tool), calls

    def test_chunks_merge_dedup_and_stop_at_history_end(self):
        """两页有重叠 + 空页终止：合并去重、游标取「本批最早日期的前一天」、ktype=2。"""
        pages = [
            # 第一页（end=今天）：最新 3 根
            {"kline_list": [_futu_daily_row("20260107", 7), _futu_daily_row("20260106", 6),
                            _futu_daily_row("20260105", 5)]},
            # 第二页（end=2026-01-04）：更早 2 根 + 页边界重叠 1 根 → 按 t 去重
            {"kline_list": [_futu_daily_row("20260105", 5), _futu_daily_row("20251231", 31),
                            _futu_daily_row("20251230", 30)]},
            # 历史翻尽：空页 → 正常终止，不报错
            {"kline_list": []},
        ]
        patched, calls = self._patched_pages(pages)
        with patched:
            bars, source, stale = market.load_raw_bars("600519", "1d", 2000)

        self.assertEqual(source, "futu/raw_chunk")
        self.assertFalse(stale)
        self.assertEqual([b["t"] for b in bars],
                         ["2025-12-30", "2025-12-31", "2026-01-05",
                          "2026-01-06", "2026-01-07"])
        self.assertEqual(bars[0]["c"], 30.0)   # 去重保留的是同一根原始价，不被复权值覆盖
        self.assertEqual([name for name, _ in calls], ["quote_history_kline"] * 3)
        ends = [args["end"] for _, args in calls]
        self.assertEqual(ends[0], date.today().isoformat())  # 首块从今天向前
        self.assertEqual(ends[1], "2026-01-04")              # 本批最早(01-05)的前一天
        self.assertEqual(ends[2], "2025-12-29")              # 逐块前移
        for _, args in calls:
            self.assertEqual(args["ktype"], 2, "1d 必须映射富途 ktype=2")
            self.assertEqual(args.get("autype"), "0",
                             "必须显式 autype=0（不复权）——富途服务端默认 autype=1 前复权，"
                             "不传就会落库复权价（K1 的根因）")
            self.assertEqual(args["num"], market.FUTU_MAX_BARS)
            self.assertEqual(args["symbol"], "SH.600519")

    def test_stops_when_cursor_stops_moving(self):
        """富途无视 end 每次都回同一页时必须终止（防死循环），不得永远翻下去。"""
        page = {"kline_list": [_futu_daily_row("20260107", 7), _futu_daily_row("20260106", 6),
                               _futu_daily_row("20260105", 5)]}
        patched, calls = self._patched_pages([page, page, page, page])
        with patched:
            bars, _source, _stale = market.load_raw_bars("600519", "1d", 2000)

        self.assertEqual(len(calls), 2, "第二批无进展（游标不再前移）就必须停")
        self.assertEqual(len(bars), 3)

    def _page(self, end_day, count):
        """以 end_day 为最新一根、向前数 count 个自然日的假富途页（原始价形状）。"""
        base = date.fromisoformat(end_day)
        return {"kline_list": [_futu_daily_row((base - timedelta(days=i)).strftime("%Y%m%d"),
                                               100 + i) for i in range(count)]}

    def test_stops_when_limit_reached_without_extra_page(self):
        """凑满 limit 根即停：不再多发一页请求，也不截断已合并的整页。

        limit 与 load_bars 同口径经 normalize_limit（下限 MIN_BARS=20），
        故用 10+10 两页对齐 20 根边界。
        """
        patched, calls = self._patched_pages([self._page("2026-01-15", 10),
                                              self._page("2025-12-22", 10)])
        with patched:
            bars, _source, _stale = market.load_raw_bars("600519", "1d", 20)
        self.assertEqual(len(calls), 2, "10 根 < 20 根才翻第二页；20 根凑满不再翻第三页")
        self.assertEqual(len(bars), 20, "凑满即停：整页合并不截断")

    def test_first_batch_failure_raises_runtime_error(self):
        """首批就失败（通道故障）必须按 load_bars 风格抛 RuntimeError，不返回空冒充成功。"""
        with patch.object(market, "call_tool",
                          side_effect=futu_mcp.FutuUnavailable("MCP 请求失败：timeout")):
            with self.assertRaises(RuntimeError) as caught:
                market.load_raw_bars("600519", "1d", 2000)
        self.assertIn("取数失败", str(caught.exception))

    def test_empty_history_on_first_batch_is_an_error_not_empty_success(self):
        """首批即空 K 线：0 行「成功」是假成功，必须报错（宁缺毋假）。"""
        with patch.object(market, "call_tool", return_value={"kline_list": []}):
            with self.assertRaises(RuntimeError):
                market.load_raw_bars("688888", "1d", 2000)

    def test_fetch_futu_without_autype_keeps_legacy_server_default(self):
        """不传 autype 时 fetch_foutu 维持旧行为（不带该键，服务端默认前复权）。

        load_bars 供回测/展示消费，复权口径不因本次修复而变——原始价只由
        load_raw_bars 显式声明（落库口径，规格 §4.2 规则 2）。
        """
        captured = {}

        def fake_call_tool(name, args, **kwargs):
            captured.update(args)
            return {"kline_list": [{"date": "20260625", "open": 1, "high": 2,
                                    "low": 0.5, "close": 1.5, "volume": 10}]}

        with patch.object(market, "call_tool", side_effect=fake_call_tool):
            bars, source = market.fetch_futu("600519", "1d", 5)
        self.assertEqual(source, "futu/quote_history_kline")
        self.assertEqual(captured["end"], date.today().isoformat(), "end 缺省=今天（向后兼容）")
        self.assertNotIn("autype", captured)


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


class TokenLifecycleTests(unittest.TestCase):
    """access_token 只有 2 小时（官方 expires_in=7200），过期必须能自动续期。

    服务端在凭证失效时返回的是 internal error 而不是 401，所以"过期"和"服务端故障"
    在报文上无法区分 —— 这一点此前导致渠道状态页长期误报「token 有效」。
    """

    def setUp(self):
        self.calls = []

    def test_both_response_envelopes_are_understood(self):
        """实测服务端有两种信封：行情类 ret_code/data，账户类 s/d。

        只认一种会把另一种的成功响应判成失败——account_authorized_trd_accs 曾因此
        报 "ret=None None"，拿不到 acc_id，进而持仓必然读不出来。
        """
        quote = {"jsonrpc": "2.0", "id": 2, "result": {"content": [
            {"type": "text", "text": '{"ret_code": 0, "data": {"kline_list": [1]}}'}]}}
        account = {"jsonrpc": "2.0", "id": 2, "result": {"content": [
            {"type": "text", "text": '{"s": "ok", "d": {"accounts": [{"account_id": 1}]}}'}]}}
        self.assertEqual(futu_mcp._unwrap("quote", quote), {"kline_list": [1]})
        self.assertEqual(futu_mcp._unwrap("account", account), {"accounts": [{"account_id": 1}]})

    def test_pagination_envelope_is_passed_through(self):
        """行情类信封可能带 pagination（翻页游标，实测成分股工具）：必须透出。

        剥层时丢弃 pagination 会让调用方永远只见第一页——键集分页形同虚设。
        无 pagination 的响应不受影响（向后兼容）。
        """
        paged = {"jsonrpc": "2.0", "id": 2, "result": {"content": [
            {"type": "text", "text": json.dumps({
                "ret_code": 0, "ret_msg": "success",
                "data": {"stock_list": [{"symbol": "SZ.300896"}]},
                "pagination": {"has_more": True, "next_key": "50", "total": 300}})}]}}
        unwrapped = futu_mcp._unwrap("quote_valuation_index_component_stock_list", paged)
        self.assertEqual(unwrapped["pagination"],
                         {"has_more": True, "next_key": "50", "total": 300})
        self.assertEqual(unwrapped["stock_list"], [{"symbol": "SZ.300896"}])

        bare = {"jsonrpc": "2.0", "id": 2, "result": {"content": [
            {"type": "text", "text": '{"ret_code": 0, "data": {"kline_list": [1]}}'}]}}
        self.assertEqual(futu_mcp._unwrap("quote", bare),
                         {"kline_list": [1]})  # 无 pagination：返回值形状不变

    def test_account_envelope_error_status_is_reported(self):
        payload = {"jsonrpc": "2.0", "id": 2, "result": {"content": [
            {"type": "text", "text": '{"s": "error", "m": "account not found"}'}]}}
        with self.assertRaises(futu_mcp.FutuUnavailable) as caught:
            futu_mcp._unwrap("account_positions", payload)
        self.assertIn("account not found", str(caught.exception))

    def test_unknown_envelope_is_reported_not_silently_accepted(self):
        payload = {"jsonrpc": "2.0", "id": 2, "result": {"content": [
            {"type": "text", "text": '{"something": "else"}'}]}}
        with self.assertRaises(futu_mcp.FutuUnavailable) as caught:
            futu_mcp._unwrap("mystery", payload)
        self.assertIn("未识别的返回信封", str(caught.exception))

    def test_auth_failure_signature_is_recognized(self):
        self.assertTrue(futu_mcp._looks_like_auth_failure("quote_x: internal error"))
        self.assertTrue(futu_mcp._looks_like_auth_failure("invalid_token"))
        self.assertFalse(futu_mcp._looks_like_auth_failure("quote_x: ret=-3 invalid parameter"))

    def test_call_tool_refreshes_once_then_retries(self):
        """internal error → 续期一次 → 重试成功，用户不必手工处理。"""
        attempts = []

        def fake_call_once(name, arguments, timeout, client_name):
            attempts.append(name)
            if len(attempts) == 1:
                raise futu_mcp.FutuUnavailable(f"{name}: internal error")
            return {"ok": True}

        with patch.object(futu_mcp, "_call_once", side_effect=fake_call_once), \
             patch.object(futu_mcp, "refresh_access_token", return_value=True) as refresh:
            self.assertEqual(futu_mcp.call_tool("quote_trading_days", {}), {"ok": True})
        self.assertEqual(len(attempts), 2, "必须重试恰好一次")
        refresh.assert_called_once()

    def test_call_tool_does_not_retry_business_errors(self):
        """业务错误（参数错/无权限）不该触发续期，否则会掩盖真实原因。"""
        with patch.object(futu_mcp, "_call_once",
                          side_effect=futu_mcp.FutuUnavailable("x: ret=-3 invalid parameter")), \
             patch.object(futu_mcp, "refresh_access_token") as refresh:
            with self.assertRaises(futu_mcp.FutuUnavailable):
                futu_mcp.call_tool("x", {})
        refresh.assert_not_called()

    def test_call_tool_gives_up_when_refresh_fails(self):
        with patch.object(futu_mcp, "_call_once",
                          side_effect=futu_mcp.FutuUnavailable("x: internal error")), \
             patch.object(futu_mcp, "refresh_access_token", return_value=False):
            with self.assertRaises(futu_mcp.FutuUnavailable):
                futu_mcp.call_tool("x", {})

    def test_auto_refresh_can_be_disabled(self):
        with patch.object(futu_mcp, "_call_once",
                          side_effect=futu_mcp.FutuUnavailable("x: internal error")), \
             patch.object(futu_mcp, "refresh_access_token") as refresh:
            with self.assertRaises(futu_mcp.FutuUnavailable):
                futu_mcp.call_tool("x", {}, auto_refresh=False)
        refresh.assert_not_called()

    def test_expiry_round_trip(self):
        import tempfile
        from datetime import datetime, timedelta
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "futu-token-expiry"
            with patch.object(futu_mcp, "EXPIRY_FILE", path):
                moment = futu_mcp.record_expiry(7200)
                self.assertIsNotNone(moment)
                self.assertAlmostEqual(futu_mcp.seconds_until_expiry(), 7200, delta=5)
                path.write_text((datetime.now() - timedelta(minutes=1)).isoformat())
                self.assertLess(futu_mcp.seconds_until_expiry(), 0)

    def test_expiry_unknown_is_not_treated_as_valid(self):
        with patch.object(futu_mcp, "EXPIRY_FILE", Path("/nonexistent/expiry")):
            self.assertIsNone(futu_mcp.seconds_until_expiry())
            self.assertIsNone(futu_mcp.token_expiry())

    def test_refresh_records_expiry_from_the_response(self):
        """续期成功后必须按响应里的 expires_in 记账，状态页才能显示剩余时间。"""
        import json as jsonlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "futu-client-id").write_text("cid")
            (tmp / "futu-refresh").write_text("rtok")
            (tmp / "futu-token").write_text("old")
            payload = jsonlib.dumps({"access_token": "brand-new", "expires_in": 7200}).encode()
            with patch.object(futu_mcp, "CLIENT_FILE", tmp / "futu-client-id"), \
                 patch.object(futu_mcp, "REFRESH_FILE", tmp / "futu-refresh"), \
                 patch.object(futu_mcp, "token_path", return_value=tmp / "futu-token"), \
                 patch.object(futu_mcp, "EXPIRY_FILE", tmp / "futu-token-expiry"), \
                 patch.object(futu_mcp, "reset_session"), \
                 patch("urllib.request.urlopen", return_value=_FakeResponse(payload)):
                self.assertTrue(futu_mcp.refresh_access_token())
                # 断言必须在 patch 生效期间：否则会读到真实 ~/.dsh 的到期文件
                self.assertAlmostEqual(futu_mcp.seconds_until_expiry(), 7200, delta=15)
            self.assertEqual((tmp / "futu-token").read_text(), "brand-new")


class EnsureFreshTests(unittest.TestCase):
    """保活判断：只在确实快过期时才换 token。

    换发新 token 可能让仍在被使用的旧 token 失效，所以"无条件续期"是有害的——
    这组测试锁住"不该续期时绝不续期"。
    """

    def test_skips_when_plenty_of_time_left(self):
        with patch.object(futu_mcp, "seconds_until_expiry", return_value=5400), \
             patch.object(futu_mcp, "refresh_access_token") as refresh:
            refreshed, detail = futu_mcp.ensure_fresh(1800)
        self.assertFalse(refreshed)
        self.assertIn("90", detail.replace("分钟", ""))
        refresh.assert_not_called()

    def test_refreshes_when_near_expiry(self):
        with patch.object(futu_mcp, "seconds_until_expiry", return_value=300), \
             patch.object(futu_mcp, "refresh_access_token", return_value=True) as refresh:
            refreshed, detail = futu_mcp.ensure_fresh(1800)
        self.assertTrue(refreshed)
        refresh.assert_called_once()
        self.assertIn("已续期", detail)

    def test_reports_failure_without_raising(self):
        with patch.object(futu_mcp, "seconds_until_expiry", return_value=-60), \
             patch.object(futu_mcp, "refresh_access_token", return_value=False):
            refreshed, detail = futu_mcp.ensure_fresh(1800)
        self.assertFalse(refreshed)
        self.assertIn("重新完整授权", detail)

    def test_force_refreshes_regardless(self):
        with patch.object(futu_mcp, "seconds_until_expiry", return_value=999999), \
             patch.object(futu_mcp, "refresh_access_token", return_value=True) as refresh:
            refreshed, _ = futu_mcp.ensure_fresh(1800, force=True)
        self.assertTrue(refreshed)
        refresh.assert_called_once()

    def test_estimate_falls_back_to_file_age(self):
        """缺少到期记录时用 token 文件写入时间 + 官方 TTL 估算（老安装也能保活）。"""
        import tempfile
        import time as _time
        with tempfile.TemporaryDirectory() as tmp:
            token = Path(tmp) / "futu-token"
            token.write_text("x")
            with patch.object(futu_mcp, "EXPIRY_FILE", Path(tmp) / "missing-expiry"), \
                 patch.object(futu_mcp, "token_path", return_value=token):
                remaining = futu_mcp.estimated_remaining_seconds()
        self.assertIsNotNone(remaining)
        # 刚写入的文件 → 剩余应接近完整 TTL
        self.assertGreater(remaining, futu_mcp.ACCESS_TOKEN_TTL_SECONDS - 60)

    def test_estimate_is_none_without_any_signal(self):
        with patch.object(futu_mcp, "seconds_until_expiry", return_value=None), \
             patch.object(futu_mcp, "token_path", return_value=Path("/nonexistent/token")):
            self.assertIsNone(futu_mcp.estimated_remaining_seconds())

    def test_official_ttl_constant_matches_measurement(self):
        self.assertEqual(futu_mcp.ACCESS_TOKEN_TTL_SECONDS, 7200)


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
        with patch.object(engine, "load_data", return_value=(frame, "stub")):
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


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


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


class SkillGuidanceTests(unittest.TestCase):
    """skill 里的富途自检必须真调工具。

    历史教训：自检原先只看「工具列表里有没有 mcp__futu__ 前缀」，而 token 过期时
    工具**仍然在列表里**、只是每次调用返回 internal error —— 于是自检通过、数据全取不到。
    """

    def _skill(self):
        return (ROOT / "skills" / "trading-agents" / "SKILL.md").read_text(encoding="utf-8")

    def test_requires_an_actual_tool_call(self):
        text = self._skill()
        self.assertIn("必须真调一次工具", text)
        self.assertIn("工具在列表里 **不等于** 能用", text)

    def test_documents_the_two_hour_token_and_internal_error_symptom(self):
        text = self._skill()
        self.assertIn("2 小时", text)
        self.assertIn("expires_in=7200", text)
        self.assertIn("internal error", text)

    def test_names_a_concrete_probe_tool(self):
        self.assertIn("mcp__futu__quote_trading_days", self._skill())

    def test_does_not_claim_the_current_session_hot_reloads(self):
        """实测 preset 目录无 watcher；不能再宣称 touch 会让当前会话生效。"""
        text = self._skill()
        self.assertNotIn("会自动触发当前会话的组合重载", text)
        self.assertIn("新建会话", text)

    def test_auth_script_message_matches_reality(self):
        script = (ROOT / "scripts" / "futu_auth.py").read_text(encoding="utf-8")
        self.assertNotIn("已触发当前会话的组合重载", script)
        self.assertIn("没有任何 watcher", script)


class InstallDocTests(unittest.TestCase):
    """安装说明必须能被 AI 按字面执行到"完整安装"。

    背景：方式 C 原先写着"如需完整工作台，运行 install.sh……三个插件"——
    措辞把完整安装写成可选项，且插件数量已过时（实际 4 个）。
    AI 照做就可能只装出 skill 基础模式，用户以为装全了。
    """

    def _readme(self):
        return (ROOT / "README.md").read_text(encoding="utf-8")

    def test_ai_instruction_demands_a_full_install(self):
        text = self._readme()
        section = text[text.index("方式 C · 让 AI 帮你装"):text.index("## 一键启动")]
        self.assertIn("不要只装 skill 基础模式", section)
        self.assertNotIn("如需完整工作台", section, "不得把完整安装写成可选项")

    def test_ai_instruction_names_every_plugin(self):
        text = self._readme()
        section = text[text.index("方式 C · 让 AI 帮你装"):text.index("## 一键启动")]
        for plugin in ("workbench", "fin-data", "trading-engine", "futu-keepalive"):
            self.assertIn(plugin, section, f"安装说明漏了插件 {plugin}")
        self.assertNotIn("三个插件", section, "插件数量已过时（实际 4 个）")

    def test_ai_instruction_includes_the_self_check(self):
        text = self._readme()
        section = text[text.index("方式 C · 让 AI 帮你装"):text.index("## 一键启动")]
        self.assertIn("install_plugins.py", section)
        self.assertIn("check", section)
        self.assertIn("✅ 安装完整", section, "缺少可验收的通过标准")

    def test_ai_instruction_tells_the_user_to_restart(self):
        text = self._readme()
        section = text[text.index("方式 C · 让 AI 帮你装"):text.index("## 一键启动")]
        self.assertIn("重启 dsh web", section, "Host 插件需重启才加载，必须写进说明")

    def test_documented_plugin_list_matches_the_installer(self):
        """文档里列的插件必须与安装器的 PLUGINS 完全一致，避免再次过时。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "install_plugins_doc", ROOT / "scripts" / "install_plugins.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # 文档用的是展示名，与安装器的目录名不完全一致（engine → trading-engine）
        display = {"workbench": "workbench", "fin-data": "fin-data",
                   "engine": "trading-engine", "futu-keepalive": "futu-keepalive"}
        self.assertEqual(set(display), set(module.PLUGINS),
                         "新增插件后要同步这里的展示名映射")
        text = self._readme()
        section = text[text.index("方式 C · 让 AI 帮你装"):text.index("## 一键启动")]
        for plugin in module.PLUGINS:
            self.assertIn(display[plugin], section, f"文档漏了安装器实际会装的插件：{plugin}")




class MarketGuardTests(unittest.TestCase):
    """A 股专用通道必须自己判市场，不能靠调用方记得判。

    这一类的共同失败模式是**静默取到别的标的**：`ticker.split(".")[0]` 把
    `000001.HK`（港股长和）变成 `000001`（A 股平安银行），既可能报错，
    也可能悄悄返回另一个标的的行情/估值/舆情。
    """

    def test_fetch_a_share_refuses_non_a_share(self):
        for ticker in ("00001.HK", "00700.HK", "AAPL", "TSLA"):
            with self.subTest(ticker=ticker):
                with self.assertRaises(ValueError):
                    market.fetch_a_share(ticker, "1d", 250)

    def test_fetch_yahoo_refuses_intraday(self):
        with self.assertRaises(ValueError):
            market.fetch_yahoo("AAPL", "5m", 200)

    def test_fetch_yahoo_rejects_empty_ticker(self):
        with self.assertRaises(ValueError):
            market.fetch_yahoo("", "1d", 200)

    def test_valuation_fallback_is_a_share_only(self):
        """同花顺估值只服务 A 股；港股不能混入同代码的 A 股估值。"""
        source = (ROOT / "plugins" / "workbench" / "python" / "factors.py").read_text(encoding="utf-8")
        self.assertIn("if not is_a_share(ticker):", source,
                      "同花顺估值兜底必须显式判市场")
        self.assertIn("from trading_datasource.market import is_a_share", source)

    def test_yahoo_channels_use_the_shared_symbol_conversion(self):
        """凡是用 Yahoo 的通道都必须走符号归一。

        Yahoo 的港股是 4 位代码：`00700.HK` 查新闻返回 0 条，`0700.HK` 返回 17 条。
        价格通道已修，新闻通道又漏了一次 —— 所以这里按"用到 Yahoo"来扫，
        而不是逐个记哪里修过。
        """
        offenders = []
        for path in (ROOT / "plugins").rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "yahoo" not in text.lower():
                continue
            if path.name == "market.py" and "datasource" in path.parts:
                continue   # 归一实现的所在地
            # 出现 yahoo.finance 域名或 yf.download，却没用共享转换
            uses_yahoo = ("finance.yahoo.com" in text) or ("yf.download" in text)
            # 必须是真的**调用**（带括号），只 import 不算 —— 否则护栏会被导入行骗过
            if uses_yahoo and "to_yahoo_symbol(" not in text:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], f"这些文件用了 Yahoo 但没走 to_yahoo_symbol：{offenders}")

    def test_a_share_only_fetchers_guard_themselves(self):
        """东财快讯 / 千股千评也要自己判市场，而不是只依赖调用方。"""
        checks = [
            (ROOT / "plugins" / "fin-data" / "python" / "fin_news.py", "def akshare_news"),
            (ROOT / "plugins" / "fin-data" / "python" / "fin_sentiment.py", "def a_share_comment"),
        ]
        for path, marker in checks:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                start = text.index(marker)
                body = text[start:start + 600]
                self.assertIn("if not is_a_share(", body,
                              f"{path.name} 的 A 股专用函数未自行判市场")


if __name__ == "__main__":
    unittest.main(verbosity=2)
