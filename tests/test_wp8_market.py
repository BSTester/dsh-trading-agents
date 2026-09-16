"""WP8 任务 2：富途 OpenAPI 行情接入 + 通道路由 + 工具面 41→50（任务 3 起 56）。

全部离线（OpenAPI 客户端与 OpenApiMarket、MCP 通道均用计数替身）。覆盖：

* OpenApiMarket（REST 方法组，路径/参数按官方文档 2026-09-16 实抓，逐端点断言
  method/path/query/body）：market-snapshot/stock-quote/order-book/cur-kline/rt-data/
  rt-ticker/history-kline/stock-basicinfo/trading-days/market-state/search(find-news+
  find-community) + 资金/期权 6 个（8 既有端点的 OpenAPI 后端）；
* 通道路由：``~/.dsh/trading-platform.json`` 的 ``futu_channel``（openapi|mcp，默认
  mcp，读取容错）；channel=openapi 且凭据可用 → OpenAPI 后端，否则 MCP 直通（既有
  行为零变化）；无凭据 + channel=openapi → ``trading/openapi-unavailable`` 信封；
* 9 个新端点/工具（market_snapshot/cur_kline/rt_data/rt_ticker/info_basicinfo/
  info_trading_days/info_search/info_market_state/quote_history_kline_v2）：参数
  白名单 → 双后端；TTL 0（实时四类）/ 5m（基本四类）/ 10m（历史 K 线 v2）；
* 归一化 fixture：两通道产出同形状（rt_quote/order_book/capital_flow 三例对拍）；
* 出站 URL 字符串断言（2026-09-16 审查补齐）：rt-ticker 的 period 同名多值展开与顺序、
  capital-flow/history 与 history-kline 的 None 省略与键序、option-expiration 的
  filter_expiration_cycles 逗号串 %2C 编码；capital-flow.section 是独立 4 值枚举
  （非 rt-data 的 6 值）；客户端契约（注入传输异常 → TransportError；5xx/429/非 JSON
  → UnexpectedResponse 且读路径落 trading/futu-unavailable，429 的 Retry-After 进 details）；
* 工具面 50 锁定（WP8 任务 6 起 59）+ 端点清单 46 锁定（任务 6 起 55）。

官方文档记录（2026-09-16 web_fetch 实抓，路径前缀 ``/api/v1.0/quote``）：
  market-snapshot  POST /quote/snapshot            body {code_list 1..400}
  stock-quote      POST /quote/stock-quote         body {code_list}
  order-book       POST /quote/order-book          body {code, num? 1..60}
  cur-kline        GET  /quote/{symbol}/cur-kline  query {num! 1..370, ktype?=2, autype?=1, extended_time?=0}
  rt-data          GET  /quote/{symbol}/rt-data    query {request_section?=NORMAL}（6 值）
  rt-ticker        GET  /quote/{symbol}/rt-ticker  query {num? 1..750=500, period?[]}
                   （period 是**同名多值**：?period=BEFORE&period=AFTER，不是 Python repr）
  history-kline    GET  /quote/{symbol}/history-kline query {end!, start?, ktype?=2, autype?=1, num?<=370, extended_time?=0}
  stock-basicinfo  POST /quote/stock-basicinfo     body {code_list 1..400}
  trading-days     GET  /quote/trading-days        query {market!, start!, end!}
  market-state     POST /quote/market-state        body {code_list, is_contain_ba?, is_contain_overnight?}
  search           GET  /quote/find-news | /quote/find-community  query {symbol!, size? 1..50, ...}
  capital-flow     GET  /quote/{symbol}/capital-flow            query {section?=NORMAL}
                   （section 只有 4 值 NORMAL/FULL/PREMARKET/AFTERHOURS，**不是** rt-data 的 6 值）
  capital-flow-hist GET /quote/{symbol}/capital-flow/history    query {period_type?=DAY, start?, end?, count? 1..1000}
  capital-distrib  GET  /quote/{symbol}/capital-distribution
  option-expiration GET /quote/{symbol}/option-expiration       query {index_option_type?, filter_standard?, filter_expiration_cycles?}
                   （filter_expiration_cycles 官方是**逗号分隔字符串**，非数组）
  option-chain     GET  /quote/{symbol}/option-chain            query {start?, end?, index_option_type?, filter_standard?}
  option-screen    POST /quote/option-screen          body {strategy!, field_filter?, sort_obj?, next_key?, limit?, request_exact_data?, strategy_param?}
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from server import app as app_module  # noqa: E402
from server import caches, futu_data, mcp_tools, store_access  # noqa: E402
from trading_datasource import futu_openapi as fo  # noqa: E402
from trading_datasource.futu_mcp import FutuUnavailable  # noqa: E402
from trading_datasource.futu_openapi import (  # noqa: E402
    CredentialStore, OpenApiClient, OpenApiError, OpenApiMarket, TransportError,
    UnexpectedResponse)

# 9 个新端点/工具（任务 C 清单原序）：8 个按官方文档命名 + history-kline 新端点。
WP8_MARKET_ENDPOINTS = ("market_snapshot", "cur_kline", "rt_data", "rt_ticker",
                        "info_basicinfo", "info_trading_days", "info_search",
                        "info_market_state", "quote_history_kline_v2")


class RecordingClient:
    """OpenApiClient 替身：记录 request / request_meta 入参，返回预设 d/pagination。"""

    def __init__(self, d=None, pagination=None, error=None):
        self.calls = []
        self.d = {"echo": "d"} if d is None else d
        self.pagination = pagination
        self.error = error

    def _record(self, name, method, path, query, json_body):
        self.calls.append((name, method, path, query, json_body))
        if self.error is not None:
            raise self.error
        return dict(self.d), self.pagination

    def request(self, method, path, query=None, json_body=None):
        d, _ = self._record("request", method, path, query, json_body)
        return d

    def request_meta(self, method, path, query=None, json_body=None):
        return self._record("request_meta", method, path, query, json_body)


def make_market(d=None, pagination=None, error=None):
    return OpenApiMarket(RecordingClient(d=d, pagination=pagination, error=error))


class OpenApiMarketDocTest(unittest.TestCase):
    """OpenApiMarket 逐端点：官方路径/参数 → request 入参断言（任务 A）。"""

    def test_market_snapshot_posts_code_list_with_official_bounds(self):
        market = make_market()
        market.market_snapshot(["HK.00700", "US.AAPL"])
        self.assertEqual(market.client.calls, [
            ("request", "POST", "/api/v1.0/quote/snapshot", None,
             {"code_list": ["HK.00700", "US.AAPL"]})])
        for bad in ([], ["HK.00700"] * 401, "HK.00700", [42]):
            with self.assertRaises(ValueError, msg=bad):
                make_market().market_snapshot(bad)

    def test_stock_quote_posts_stock_quote_path(self):
        market = make_market()
        market.stock_quote(["SH.600519"])
        self.assertEqual(market.client.calls, [
            ("request", "POST", "/api/v1.0/quote/stock-quote", None,
             {"code_list": ["SH.600519"]})])

    def test_order_book_posts_code_and_optional_num(self):
        market = make_market()
        market.order_book("HK.00700")
        self.assertEqual(market.client.calls[-1],
                         ("request", "POST", "/api/v1.0/quote/order-book", None,
                          {"code": "HK.00700"}))
        market.order_book("HK.00700", num=3)
        self.assertEqual(market.client.calls[-1][4], {"code": "HK.00700", "num": 3})
        for num in (0, 61, "3", 1.5, True):
            with self.assertRaises(ValueError, msg=num):
                make_market().order_book("HK.00700", num=num)

    def test_cur_kline_gets_symbol_path_with_required_num(self):
        market = make_market()
        market.cur_kline("HK.00700", num=5, ktype=6)
        self.assertEqual(market.client.calls, [
            ("request", "GET", "/api/v1.0/quote/HK.00700/cur-kline",
             {"num": 5, "ktype": 6, "autype": 1, "extended_time": 0}, None)])
        for num in (None, 0, 371, "5"):
            with self.assertRaises(ValueError, msg=num):
                make_market().cur_kline("HK.00700", num=num)
        for ktype in (0, 12, "2"):
            with self.assertRaises(ValueError, msg=ktype):
                make_market().cur_kline("HK.00700", num=1, ktype=ktype)
        with self.assertRaises(ValueError):
            make_market().cur_kline("HK.00700", num=1, autype=9)
        with self.assertRaises(ValueError):
            make_market().cur_kline("HK.00700", num=1, extended_time=3)

    def test_rt_data_gets_section_enum(self):
        market = make_market()
        market.rt_data("HK.00700")
        self.assertEqual(market.client.calls, [
            ("request", "GET", "/api/v1.0/quote/HK.00700/rt-data",
             {"request_section": "NORMAL"}, None)])
        market.rt_data("US.AAPL", request_section="PREMARKET")
        self.assertEqual(market.client.calls[-1][3], {"request_section": "PREMARKET"})
        with self.assertRaises(ValueError):
            make_market().rt_data("HK.00700", request_section="NOON")

    def test_rt_ticker_gets_num_and_period_list(self):
        market = make_market()
        market.rt_ticker("HK.00700")
        self.assertEqual(market.client.calls[-1],
                         ("request", "GET", "/api/v1.0/quote/HK.00700/rt-ticker",
                          {"num": 500}, None))
        market.rt_ticker("HK.00700", num=2, period=["BEFORE", "AFTER"])
        self.assertEqual(market.client.calls[-1][3],
                         {"num": 2, "period": ["BEFORE", "AFTER"]})
        for num in (0, 751, "500"):
            with self.assertRaises(ValueError, msg=num):
                make_market().rt_ticker("HK.00700", num=num)
        with self.assertRaises(ValueError):
            make_market().rt_ticker("HK.00700", period=["NOON"])

    def test_history_kline_requires_end_and_merges_envelope_pagination(self):
        market = make_market(d={"kline_list": [1]}, pagination={"has_more": True})
        d = market.history_kline("HK.00700", end="2026-09-10", num=2)
        self.assertEqual(market.client.calls, [
            ("request_meta", "GET", "/api/v1.0/quote/HK.00700/history-kline",
             {"start": None, "end": "2026-09-10", "ktype": 2, "autype": 1,
              "num": 2, "extended_time": 0}, None)])
        self.assertEqual(d, {"kline_list": [1], "pagination": {"has_more": True}})
        with self.assertRaises(ValueError):
            make_market().history_kline("HK.00700", end="")
        for num in (0, 371):
            with self.assertRaises(ValueError, msg=num):
                make_market().history_kline("HK.00700", end="2026-09-10", num=num)
        with self.assertRaises(ValueError):
            make_market().history_kline("HK.00700", end="2026-13-40")

    def test_stock_basicinfo_posts_basicinfo_path(self):
        market = make_market()
        market.stock_basicinfo(["HK.00700"])
        self.assertEqual(market.client.calls, [
            ("request", "POST", "/api/v1.0/quote/stock-basicinfo", None,
             {"code_list": ["HK.00700"]})])

    def test_trading_days_gets_market_start_end(self):
        market = make_market()
        market.trading_days("HK", "2025-12-22", "2025-12-26")
        self.assertEqual(market.client.calls, [
            ("request", "GET", "/api/v1.0/quote/trading-days",
             {"market": "HK", "start": "2025-12-22", "end": "2025-12-26"}, None)])
        for market_name in ("hk", "XX", "", None):
            with self.assertRaises(ValueError, msg=market_name):
                make_market().trading_days(market_name, "2025-12-22", "2025-12-26")
        with self.assertRaises(ValueError):
            make_market().trading_days("HK", "2025-12-26", "2025-12-22")
        with self.assertRaises(ValueError):
            make_market().trading_days("HK", "2025/12/22", "2025-12-26")

    def test_market_state_posts_code_list_and_flags(self):
        market = make_market()
        market.market_state(["HK.00700", "US.AAPL"], is_contain_ba=True)
        self.assertEqual(market.client.calls, [
            ("request", "POST", "/api/v1.0/quote/market-state", None,
             {"code_list": ["HK.00700", "US.AAPL"], "is_contain_ba": True})])
        with self.assertRaises(ValueError):
            make_market().market_state(["HK.00700"], is_contain_ba="yes")

    def test_search_news_and_community_hit_official_sub_endpoints(self):
        market = make_market()
        market.search_news("腾讯", size=3, news_type=2, sort_type=2, lang="zh-CN")
        self.assertEqual(market.client.calls[-1],
                         ("request", "GET", "/api/v1.0/quote/find-news",
                          {"symbol": "腾讯", "size": 3, "news_type": 2,
                           "sort_type": 2, "lang": "zh-CN"}, None))
        market.search_news("AAPL")
        self.assertEqual(market.client.calls[-1][2:], (
            "/api/v1.0/quote/find-news", {"symbol": "AAPL", "size": 10}, None))
        market.search_community("腾讯", size=2, community_type=1)
        self.assertEqual(market.client.calls[-1],
                         ("request", "GET", "/api/v1.0/quote/find-community",
                          {"symbol": "腾讯", "size": 2, "community_type": 1}, None))
        for size in (0, 51):
            with self.assertRaises(ValueError, msg=size):
                make_market().search_news("腾讯", size=size)
        for news_type in (0, 4):
            with self.assertRaises(ValueError, msg=news_type):
                make_market().search_news("腾讯", news_type=news_type)
        with self.assertRaises(ValueError):
            make_market().search_news("腾讯", lang="xx")
        with self.assertRaises(ValueError):
            make_market().search_news("")
        with self.assertRaises(ValueError):
            make_market().search_community("腾讯", community_type=9)

    def test_capital_family_hits_official_paths(self):
        market = make_market()
        market.capital_flow("HK.00700")
        self.assertEqual(market.client.calls[-1],
                         ("request", "GET", "/api/v1.0/quote/HK.00700/capital-flow",
                          {"section": "NORMAL"}, None))
        market.capital_flow_history("HK.00700", count=7)
        self.assertEqual(market.client.calls[-1][:3],
                         ("request_meta", "GET",
                          "/api/v1.0/quote/HK.00700/capital-flow/history"))
        self.assertEqual(market.client.calls[-1][3],
                         {"period_type": "DAY", "start": None, "end": None, "count": 7})
        market.capital_distribution("SZ.000001")
        self.assertEqual(market.client.calls[-1][2],
                         "/api/v1.0/quote/SZ.000001/capital-distribution")
        with self.assertRaises(ValueError):
            make_market().capital_flow_history("HK.00700", count=1001)
        with self.assertRaises(ValueError):
            make_market().capital_flow_history("HK.00700", period_type="YEAR")

    def test_capital_flow_section_is_four_values_not_rt_sections(self):
        """capital-flow.section 是独立枚举（4 值）；rt-data 的 6 值不得复用（live -3 已复现）。"""
        self.assertEqual(OpenApiMarket.CAPITAL_FLOW_SECTIONS,
                         frozenset({"NORMAL", "FULL", "PREMARKET", "AFTERHOURS"}))
        # rt-data 仍保留 6 值（HK_DARK/OVERNIGHT 只属于 request_section）
        self.assertEqual(OpenApiMarket.RT_SECTIONS - OpenApiMarket.CAPITAL_FLOW_SECTIONS,
                         frozenset({"HK_DARK", "OVERNIGHT"}))
        market = make_market()
        for section in ("NORMAL", "FULL", "PREMARKET", "AFTERHOURS"):
            market.capital_flow("HK.00700", section=section)
            self.assertEqual(market.client.calls[-1][3], {"section": section})
        # 默认 NORMAL 只发一次请求；越界值与 rt-data 专有值一律本地拒绝（零网络往返）
        market.capital_flow("HK.00700")
        self.assertEqual(market.client.calls[-1][3], {"section": "NORMAL"})
        for bad in ("OVERNIGHT", "HK_DARK", "NONE", "normal", ""):
            with self.subTest(section=bad):
                client = RecordingClient()
                with self.assertRaises(ValueError) as cm:
                    OpenApiMarket(client).capital_flow("HK.00700", section=bad)
                self.assertEqual(client.calls, [], "坏 section 不得触达通道")
                for allowed in ("NORMAL", "FULL", "PREMARKET", "AFTERHOURS"):
                    self.assertIn(allowed, str(cm.exception), "错误消息须列出 4 值")

    def test_option_family_hits_official_paths(self):
        market = make_market()
        market.option_expiration("HK.00700")
        self.assertEqual(market.client.calls[-1][:3],
                         ("request", "GET", "/api/v1.0/quote/HK.00700/option-expiration"))
        market.option_chain("HK.00700", start="2026-09-18", end="2026-10-29")
        self.assertEqual(market.client.calls[-1][:3],
                         ("request", "GET", "/api/v1.0/quote/HK.00700/option-chain"))
        self.assertEqual(market.client.calls[-1][3],
                         {"start": "2026-09-18", "end": "2026-10-29",
                          "filter_standard": "ALL"})
        with self.assertRaises(ValueError):
            make_market().option_expiration("HK.00700", filter_standard="FREE")

    def test_option_expiration_cycles_is_csv_string_not_list(self):
        """filter_expiration_cycles 官方是逗号分隔字符串：list 本地拒绝，str 归一后透传。"""
        self.assertIn("WEEK", OpenApiMarket.EXPIRATION_CYCLES)
        market = make_market()
        market.option_expiration("HK.00700")
        self.assertEqual(market.client.calls[-1][3], {"filter_standard": "ALL"},
                         "未提供 cycles 时不得出现该 query 键")
        market.option_expiration("HK.00700", filter_expiration_cycles="WEEK,MONTH")
        self.assertEqual(market.client.calls[-1][3],
                         {"filter_standard": "ALL",
                          "filter_expiration_cycles": "WEEK,MONTH"})
        market.option_expiration("HK.00700", filter_expiration_cycles=" WEEK , WEEKMON ")
        self.assertEqual(market.client.calls[-1][3]["filter_expiration_cycles"],
                         "WEEK,WEEKMON", "元素两端空白归一，仍按官方 pattern 拼接")
        market.option_expiration("HK.00700", filter_expiration_cycles="")
        self.assertEqual(market.client.calls[-1][3], {"filter_standard": "ALL"},
                         "空串视为未提供")
        for bad in (["WEEK", "MONTH"], ["WEEK"], ("WEEK",), 7, "WEEK,NOPE", "NOPE",
                    "WEEK,,MONTH"):
            with self.subTest(cycles=bad):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiMarket(client).option_expiration(
                        "HK.00700", filter_expiration_cycles=bad)
                self.assertEqual(client.calls, [], "坏 cycles 不得触达通道")

    def test_option_screen_posts_strategy_body_and_merges_pagination(self):
        market = make_market(d={"option_list": [1]}, pagination={"has_more": False})
        strategy = {"market_category_list": [1], "filter_group_list": []}
        d = market.option_screen(strategy=strategy, field_filter={"last_price": 1},
                                 limit=50)
        self.assertEqual(market.client.calls, [
            ("request_meta", "POST", "/api/v1.0/quote/option-screen", None,
             {"strategy": strategy, "field_filter": {"last_price": 1}, "limit": 50})])
        self.assertEqual(d, {"option_list": [1], "pagination": {"has_more": False}})
        with self.assertRaises(ValueError):
            make_market().option_screen(strategy={})
        with self.assertRaises(ValueError):
            make_market().option_screen(strategy=strategy, limit=1001)

    def test_envelope_error_propagates_as_openapi_error(self):
        market = make_market(error=OpenApiError("权限不足", errcode=-9))
        with self.assertRaises(OpenApiError):
            market.capital_flow("HK.00700")


# ---------------------------------------------------------------------------
# 出站 URL 字符串 + 客户端传输/信封契约（2026-09-16 审查项）
# ---------------------------------------------------------------------------
def _ed25519_pem():
    """一组 PKCS8 Ed25519 私钥（只用于签名器构造/凭据判定，本文件不验签）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())


class UrlRecordingHttp:
    """真传输替身：记录出站 ``(method, url, headers, body)``，默认回 OK 行情信封。

    把断言钉在**出站 URL 字符串**上——此前缺口：只记录/断言序列化前的 query dict，
    而「list 被序列化成 Python repr」这类缺陷在 dict 层完全看不见。
    """

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body})
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return 200, json.dumps({"ret_code": 0, "data": {"ok": 1}}).encode("utf-8"), {}


class OpenApiOutboundUrlTest(unittest.TestCase):
    """出站 URL 逐字节断言：多值展开/顺序/编码/None 省略（审查必修 2）。"""

    HOST = "https://webapi.futunn.com"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.http = UrlRecordingHttp()
        self.market = OpenApiMarket(self._client(self.tmp.name, self.http))

    def _client(self, tmp, http, cred=None):
        path = Path(tmp) / "cred.json"
        path.write_text(json.dumps(cred or {"mode": "oauth", "access_token": "tok"}),
                        encoding="utf-8")
        return OpenApiClient(CredentialStore(path), http=http, host=self.HOST)

    def test_rt_ticker_period_is_same_name_multi_value(self):
        """period 同名多值：?num=2&period=BEFORE&period=AFTER（不是 Python repr）。"""
        self.market.rt_ticker("HK.00700", num=2, period=["BEFORE", "AFTER"])
        url = self.http.calls[-1]["url"]
        self.assertEqual(url, self.HOST +
                         "/api/v1.0/quote/HK.00700/rt-ticker"
                         "?num=2&period=BEFORE&period=AFTER")
        self.assertNotIn("%5B", url, "不得出现 list 的 repr（%5B = '['）")
        self.assertNotIn("%27", url, "不得出现引号转义（%27 = \"'\")")
        self.assertEqual(url.count("period="), 2, "同名键按值各出现一次")
        self.market.rt_ticker("HK.00700", num=3, period=["AFTER", "BEFORE", "OVERNIGHT"])
        self.assertTrue(self.http.calls[-1]["url"].endswith(
            "?num=3&period=AFTER&period=BEFORE&period=OVERNIGHT"),
            "多值顺序 = 传入顺序（不排序）")

    def test_history_queries_omit_none_and_keep_exact_order(self):
        """None = 未提供 → 整条省略（曾出站 start=None，网关 -3，已 live 复现）。"""
        self.market.capital_flow_history("HK.00700", count=5)
        self.assertEqual(self.http.calls[-1]["url"], self.HOST +
                         "/api/v1.0/quote/HK.00700/capital-flow/history"
                         "?period_type=DAY&count=5")
        self.market.capital_flow_history("HK.00700", start="2026-08-01",
                                         end="2026-09-01", count=7)
        self.assertEqual(self.http.calls[-1]["url"], self.HOST +
                         "/api/v1.0/quote/HK.00700/capital-flow/history"
                         "?period_type=DAY&start=2026-08-01&end=2026-09-01&count=7")
        self.market.history_kline("HK.00700", end="2026-09-10", num=2)
        self.assertEqual(self.http.calls[-1]["url"], self.HOST +
                         "/api/v1.0/quote/HK.00700/history-kline"
                         "?end=2026-09-10&ktype=2&autype=1&num=2&extended_time=0")

    def test_option_expiration_cycles_csv_is_percent_encoded(self):
        self.market.option_expiration("HK.00700", filter_expiration_cycles="WEEK,MONTH")
        self.assertEqual(self.http.calls[-1]["url"], self.HOST +
                         "/api/v1.0/quote/HK.00700/option-expiration"
                         "?filter_standard=ALL&filter_expiration_cycles=WEEK%2CMONTH")
        self.market.option_expiration("HK.00700")
        self.assertEqual(self.http.calls[-1]["url"], self.HOST +
                         "/api/v1.0/quote/HK.00700/option-expiration?filter_standard=ALL")

    def test_query_string_contract(self):
        self.assertEqual(fo.query_string(None), "")
        self.assertEqual(fo.query_string("a=1&b=2"), "a=1&b=2", "str 原样（签名口径）")
        self.assertEqual(fo.query_string({"period": ["BEFORE", "AFTER"], "num": 2}),
                         "period=BEFORE&period=AFTER&num=2")
        self.assertEqual(fo.query_string({"period": ["BEFORE"]}), "period=BEFORE",
                         "单元素列表同样展开（不是 ['BEFORE']）")
        self.assertEqual(fo.query_string({"start": None, "end": "2026-09-10"}),
                         "end=2026-09-10")
        self.assertEqual(fo.query_string({"q": "a b/c&d=e"}), "q=a%20b%2Fc%26d%3De",
                         "safe=\"\"：空格/斜杠/&/=\\u0020全部转义")


class OpenApiClientContractTest(unittest.TestCase):
    """客户端契约（审查 D）：注入传输异常 → TransportError；非信封 → UnexpectedResponse。

    与 ``test_wp8_openapi_client.py`` 的既有用例互补（那里锁的是刷新/签名等既有行为），
    这里只锁本次审查的「出站传输收敛」与「业务结论 vs 非业务结论」分类。
    """

    HOST = "https://webapi.futunn.com"

    def _client(self, tmp, http, cred=None):
        path = Path(tmp) / "cred.json"
        path.write_text(json.dumps(cred or {"mode": "oauth", "access_token": "tok"}),
                        encoding="utf-8")
        return OpenApiClient(CredentialStore(path), http=http, host=self.HOST)

    def test_injected_transport_exception_is_transport_error(self):
        """注入传输抛裸异常（oauth 与 appkey 两条路径）→ 契约要求的 TransportError。"""
        for mode_name, cred in (("oauth", None), ("appkey", None)):
            with self.subTest(mode=mode_name), tempfile.TemporaryDirectory() as tmp:
                if mode_name == "appkey":
                    pem = Path(tmp) / "key.pem"
                    pem.write_bytes(_ed25519_pem())
                    cred = {"mode": "appkey", "app_key": "ak",
                            "private_key_path": str(pem), "algorithm": "Ed25519"}
                http = UrlRecordingHttp([ConnectionResetError("peer reset")])
                client = self._client(tmp, http, cred)
                with self.assertRaises(TransportError) as cm:
                    client.request("GET", "/v4/x")
                self.assertIsInstance(cm.exception, OpenApiError, "仍是 OpenApiError 子类")
                self.assertIsNone(cm.exception.errcode, "传输异常没有业务 errcode")
                self.assertIn("ConnectionResetError", str(cm.exception))
                self.assertEqual(len(http.calls), 1, "传输失败不自动重试")

    def test_non_envelope_5xx_and_non_json_are_unexpected_response(self):
        for label, response in (("500", (500, b"<html>bad gateway</html>", {})),
                                ("非 JSON", (200, b"<html>not json</html>", {})),
                                ("非信封 JSON", (200, b'{"foo":1}', {}))):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                http = UrlRecordingHttp([response])
                client = self._client(tmp, http)
                with self.assertRaises(UnexpectedResponse) as cm:
                    client.request("GET", "/v4/x")
                self.assertIsInstance(cm.exception, OpenApiError)
                self.assertEqual(cm.exception.errcode, response[0],
                                 "errcode = HTTP status（非业务 errcode）")
                self.assertEqual(len(http.calls), 1, "非信封不自动重试")

    def test_429_retry_after_rides_on_unexpected_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            http = UrlRecordingHttp([(429, b"rate limited", {"Retry-After": "3"})])
            client = self._client(tmp, http)
            with self.assertRaises(UnexpectedResponse) as cm:
                client.request("GET", "/v4/x")
            self.assertEqual(cm.exception.errcode, 429)
            self.assertEqual(cm.exception.retry_after, "3")
            self.assertEqual(len(http.calls), 1)

    def test_business_envelopes_are_not_unexpected_response(self):
        """真业务信封（交易 s==error / 行情 ret_code!=0）保持业务语义（写路径 → rejected）。"""
        cases = ((b'{"s":"error","errcode":-2000,"errmsg":"no funds"}', -2000),
                 (b'{"ret_code":-3,"ret_msg":"bad param"}', -3))
        for body, errcode in cases:
            with self.subTest(errcode=errcode), tempfile.TemporaryDirectory() as tmp:
                http = UrlRecordingHttp([(200, body, {})])
                client = self._client(tmp, http)
                with self.assertRaises(OpenApiError) as cm:
                    client.request("POST", "/v4/orders")
                self.assertNotIsInstance(cm.exception, UnexpectedResponse)
                self.assertEqual(cm.exception.errcode, errcode)


# ---------------------------------------------------------------------------
# 通道路由（任务 B/D）
# ---------------------------------------------------------------------------

class FakeCall:
    """MCP 通道计数替身（同 test_wp7_futu_data 的口径）。"""

    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"echo": "mcp"} if result is None else result
        self.error = error

    def __call__(self, name, arguments, timeout=30, auto_refresh=True, client_name=None):
        self.calls.append((name, dict(arguments)))
        if self.error is not None:
            raise self.error
        if isinstance(self.result, list):
            return list(self.result)
        return dict(self.result)

    def count(self):
        return len(self.calls)


class RecordingMarket:
    """OpenApiMarket 替身：记录 (方法名, kwargs)；任意方法名都记录。"""

    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = {"echo": "openapi"} if result is None else result
        self.error = error

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, kwargs))
            if self.error is not None:
                raise self.error
            if isinstance(self.result, list):
                return list(self.result)
            return dict(self.result)
        return method


def make_futu(result=None, call=None, market=None, channel=None, credential_path=None):
    return futu_data.FutuData(call=call if call is not None else FakeCall(result=result),
                              channel=channel, market=market,
                              credential_path=credential_path)


class CacheIsolatedTest(unittest.TestCase):
    """每个用例一份全新两级缓存（内存+磁盘是模块级单例；跨用例 TTL 命中会掩盖通道调用）。"""

    def setUp(self):
        cache_dir = tempfile.TemporaryDirectory()
        caches.configure(home=cache_dir.name)
        self.addCleanup(caches.configure)
        self.addCleanup(cache_dir.cleanup)


class ChannelConfigTest(unittest.TestCase):
    """futu_channel 配置：默认 mcp、合法值、容错语义。"""

    def test_missing_config_defaults_to_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(futu_data.load_channel(tmp), "mcp")

    def test_explicit_openapi_and_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading-platform.json"
            path.write_text('{"futu_channel": "openapi"}', encoding="utf-8")
            self.assertEqual(futu_data.load_channel(tmp), "openapi")
            path.write_text('{"futu_channel": "mcp"}', encoding="utf-8")
            self.assertEqual(futu_data.load_channel(tmp), "mcp")

    def test_invalid_values_fall_back_to_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading-platform.json"
            for text in ('{"futu_channel": "rest"}', '{}', '[]'):
                path.write_text(text, encoding="utf-8")
                self.assertEqual(futu_data.load_channel(tmp), "mcp", text)

    def test_broken_json_raises_like_service_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trading-platform.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                futu_data.load_channel(tmp)


class RouteSwitchTest(CacheIsolatedTest):
    """8 既有端点后端切换（mock 两通道）+ 9 新端点双后端。"""

    def test_default_channel_is_mcp_when_call_injected(self):
        futu = make_futu()
        self.assertEqual(futu.channel, "mcp", "注入 call 替身即钉住 MCP 通道（既有行为零变化）")

    def test_eight_existing_endpoints_switch_backend_by_channel(self):
        for endpoint, payload in MIN_PAYLOAD_EXISTING.items():
            futu = make_futu(channel="openapi", market=RecordingMarket())
            out = futu.handle(endpoint, payload)
            self.assertTrue(out["ok"], (endpoint, out))
            self.assertEqual(futu.market.calls[0][0],
                             futu_data.OPENAPI_METHODS[endpoint], endpoint)
            self.assertEqual(futu._call.count(), 0, endpoint)
            futu = make_futu(channel="mcp", market=RecordingMarket())
            self.assertTrue(futu.handle(endpoint, payload)["ok"], endpoint)
            self.assertEqual(futu.market.calls, [], endpoint)
            self.assertEqual(futu._call.count(), 1, endpoint)

    def test_nine_new_endpoints_route_to_openapi_with_expected_methods(self):
        for endpoint, payload in MIN_PAYLOAD_NEW.items():
            futu = make_futu(channel="openapi",
                             market=RecordingMarket(
                                 result=result_for(endpoint, {"echo": "openapi"})))
            out = futu.handle(endpoint, payload)
            self.assertTrue(out["ok"], (endpoint, out))
            self.assertEqual(futu.market.calls[0][0],
                             futu_data.OPENAPI_METHODS[endpoint], endpoint)
            self.assertEqual(futu._call.count(), 0, endpoint)

    def test_nine_new_endpoints_route_to_mcp_tools(self):
        for endpoint, payload in MIN_PAYLOAD_NEW.items():
            futu = make_futu(channel="mcp",
                             result=result_for(endpoint, {"echo": "mcp"}))
            out = futu.handle(endpoint, payload)
            self.assertTrue(out["ok"], (endpoint, out))
            name, arguments = futu._call.calls[-1]
            self.assertEqual(name, futu_data.FUTU_TOOLS[endpoint], endpoint)
            self.assertIsNone(futu._market_override, endpoint)  # mcp 通道不注入 market
        # MCP 工具名锁定（实测 2026-09-16 tools/list：101 工具清单逐一核对）
        self.assertEqual({k: futu_data.FUTU_TOOLS[k] for k in WP8_MARKET_ENDPOINTS}, {
            "market_snapshot": "quote_market_snapshot",
            "cur_kline": "quote_cur_kline",
            "rt_data": "quote_rt_data",
            "rt_ticker": "quote_rt_ticker",
            "info_basicinfo": "quote_stock_basicinfo",
            "info_trading_days": "quote_trading_days",
            "info_search": "quote_news_search",
            "info_market_state": "quote_market_state",
            "quote_history_kline_v2": "quote_history_kline",
        })

    def test_openapi_without_credentials_gives_unavailable_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "futu-openapi.json")
            for endpoint, payload in list(MIN_PAYLOAD_EXISTING.items())[:2] + \
                    list(MIN_PAYLOAD_NEW.items())[:2]:
                futu = make_futu(channel="openapi", credential_path=missing)
                out = futu.handle(endpoint, payload)
                self.assertFalse(out["ok"], endpoint)
                self.assertEqual(out["error"]["code"], "trading/openapi-unavailable",
                                 endpoint)
                self.assertIn("futu_auth.py", out["error"]["message"], endpoint)

    def test_openapi_business_error_maps_to_futu_error(self):
        futu = make_futu(channel="openapi",
                         market=RecordingMarket(error=OpenApiError(
                             "realtime quote permission required", errcode=-9)))
        out = futu.handle("rt_quote", {"codes": ["SH.600519"]})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/futu-error")
        self.assertEqual(out["error"]["details"], {"errcode": -9})

    def test_openapi_param_error_maps_to_invalid_operation(self):
        futu = make_futu(channel="openapi", market=RecordingMarket())
        out = futu.handle("cur_kline", {"code": "HK.00700", "num": 999})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")
        self.assertEqual(futu.market.calls, [], "参数在触达通道前拒绝")

    def test_openapi_network_error_maps_to_unavailable(self):
        futu = make_futu(channel="openapi",
                         market=RecordingMarket(error=FutuUnavailable("timed out")))
        out = futu.handle("capital_flow", {"code": "HK.00700"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/futu-unavailable")

    def test_openapi_non_envelope_maps_to_unavailable_not_business(self):
        """5xx/429/非 JSON（UnexpectedResponse）是「没有业务结论」→ unavailable，不是 futu-error。"""
        for status, body in ((500, b"<html>bad gateway</html>"),
                             (429, b"rate limited"), (200, b"<html>not json</html>")):
            with self.subTest(status=status):
                try:
                    fo.parse_envelope_meta(status, body)
                except UnexpectedResponse as exc:  # except-as 出块即解绑 → 先转存
                    failure = exc
                else:  # pragma: no cover —— 分类前提不成立就该失败
                    self.fail(f"HTTP {status} 应为 UnexpectedResponse")
                futu = make_futu(channel="openapi",
                                 market=RecordingMarket(error=failure))
                out = futu.handle("capital_flow", {"code": "HK.00700"})
                self.assertFalse(out["ok"], out)
                self.assertEqual(out["error"]["code"], "trading/futu-unavailable")
                self.assertIn("OpenAPI 通道不可用", out["error"]["message"])

    def test_openapi_429_retry_after_in_error_details(self):
        error = UnexpectedResponse("rate limited", errcode=429, retry_after="3")
        futu = make_futu(channel="openapi", market=RecordingMarket(error=error))
        out = futu.handle("capital_flow", {"code": "HK.00700"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/futu-unavailable")
        self.assertEqual(out["error"]["details"], {"retry_after": "3"})

    def test_openapi_info_search_empty_payload_normalized_to_shape(self):
        """OpenAPI 侧合法空结果（{}）归一化为 {"news_list": []}，与 MCP 侧同规。"""
        futu = make_futu(channel="openapi", market=RecordingMarket(result={}))
        out = futu.handle("info_search", {"keyword": "腾讯"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"], {"news_list": []})
        self.assertTrue(caches.matches_shape("info_search", out["value"]),
                        "归一化后必须通过形状表校验（否则被 ENDPOINT_SHAPE 判载荷不完整）")
        # 反证：未归一化的空对象会被形状校验判失败
        self.assertFalse(caches.matches_shape("info_search", {}))


MIN_PAYLOAD_EXISTING = {
    "rt_quote": {"codes": ["HK.00700"]},
    "rt_order_book": {"code": "HK.00700"},
    "capital_flow": {"code": "HK.00700"},
    "capital_flow_history": {"code": "HK.00700"},
    "capital_distribution": {"code": "HK.00700"},
    "option_expiration": {"code": "HK.00700"},
    "option_chain": {"code": "HK.00700"},
    "option_screen": {"filter": {"strategy": {"market_category_list": [1]},
                                 "field_filter": {"last_price": True}}},
}

MIN_PAYLOAD_NEW = {
    "market_snapshot": {"codes": ["HK.00700"]},
    "cur_kline": {"code": "HK.00700", "num": 5},
    "rt_data": {"code": "HK.00700"},
    "rt_ticker": {"code": "HK.00700"},
    "info_basicinfo": {"codes": ["HK.00700"]},
    "info_trading_days": {"market": "HK", "start": "2025-12-22", "end": "2025-12-26"},
    "info_search": {"keyword": "腾讯"},
    "info_market_state": {"codes": ["HK.00700"]},
    "quote_history_kline_v2": {"code": "HK.00700", "end": "2026-09-10"},
}


class NewEndpointValidationTest(CacheIsolatedTest):
    """9 新端点的参数白名单/必填/区间（两通道共用同一份校验）。"""

    def test_market_snapshot_codes_bounds(self):
        for codes in ([], ["HK.00700"] * 401, "HK.00700", [42]):
            futu = make_futu()
            out = futu.handle("market_snapshot", {"codes": codes})
            self.assertFalse(out["ok"], codes)
            self.assertEqual(out["error"]["code"], "trading/invalid-operation", codes)
            self.assertEqual(futu._call.count(), 0, codes)

    def test_cur_kline_num_required_and_ranges(self):
        for payload in ({"code": "HK.00700"}, {"code": "HK.00700", "num": 0},
                        {"code": "HK.00700", "num": 371},
                        {"code": "HK.00700", "num": "5"},
                        {"code": "HK.00700", "num": 5, "ktype": 99},
                        {"num": 5}):
            futu = make_futu()
            out = futu.handle("cur_kline", payload)
            self.assertFalse(out["ok"], payload)
            self.assertEqual(futu._call.count(), 0, payload)
        futu = make_futu()
        out = futu.handle("cur_kline", {"code": "700", "num": 5, "ktype": 6})
        self.assertTrue(out["ok"], out)
        self.assertEqual(futu._call.calls[-1],
                         ("quote_cur_kline",
                          {"symbol": "HK.00700", "num": 5, "ktype": 6,
                           "autype": 1, "extended_time": 0}))

    def test_rt_data_section_enum(self):
        futu = make_futu()
        self.assertFalse(futu.handle("rt_data", {"code": "HK.00700",
                                                 "request_section": "NOON"})["ok"])
        futu = make_futu()
        self.assertTrue(futu.handle("rt_data", {"code": "HK.00700"})["ok"])
        self.assertEqual(futu._call.calls[-1],
                         ("quote_rt_data", {"symbol": "HK.00700",
                                            "request_section": "NORMAL"}))

    def test_rt_ticker_num_and_period(self):
        futu = make_futu()
        self.assertFalse(futu.handle("rt_ticker", {"code": "HK.00700", "num": 751})["ok"])
        futu = make_futu()
        self.assertTrue(futu.handle("rt_ticker",
                                    {"code": "HK.00700", "num": 3,
                                     "period": ["BEFORE"]})["ok"])
        self.assertEqual(futu._call.calls[-1],
                         ("quote_rt_ticker", {"symbol": "HK.00700", "num": 3,
                                              "period": ["BEFORE"]}))
        futu = make_futu()
        self.assertFalse(futu.handle("rt_ticker",
                                     {"code": "HK.00700", "period": "BEFORE"})["ok"])

    def test_info_trading_days_market_and_dates(self):
        for payload in ({"start": "2025-12-22", "end": "2025-12-26"},
                        {"market": "XX", "start": "2025-12-22", "end": "2025-12-26"},
                        {"market": "HK", "end": "2025-12-26"},
                        {"market": "HK", "start": "2025-12-22"}):
            futu = make_futu()
            out = futu.handle("info_trading_days", payload)
            self.assertFalse(out["ok"], payload)
            self.assertEqual(futu._call.count(), 0, payload)
        futu = make_futu(result=SHAPE_RESULT["info_trading_days"])
        self.assertTrue(futu.handle("info_trading_days",
                                    {"market": "hk", "start": "2025-12-22",
                                     "end": "2025-12-26"})["ok"])
        self.assertEqual(futu._call.calls[-1],
                         ("quote_trading_days", {"market": "HK",
                                                 "start": "2025-12-22",
                                                 "end": "2025-12-26"}))

    def test_info_search_keyword_and_option_ranges(self):
        for payload in ({}, {"keyword": ""}, {"keyword": "腾讯", "size": 0},
                        {"keyword": "腾讯", "size": 51},
                        {"keyword": "腾讯", "news_type": 4},
                        {"keyword": "腾讯", "sort_type": 3},
                        {"keyword": "腾讯", "lang": "xx"}):
            futu = make_futu()
            out = futu.handle("info_search", payload)
            self.assertFalse(out["ok"], payload)
            self.assertEqual(futu._call.count(), 0, payload)
        futu = make_futu(result=SHAPE_RESULT["info_search"])
        self.assertTrue(futu.handle("info_search", {"keyword": "腾讯"})["ok"])
        self.assertEqual(futu._call.calls[-1],
                         ("quote_news_search", {"symbol": "腾讯", "size": 10}))

    def test_info_market_state_flags_type(self):
        futu = make_futu()
        out = futu.handle("info_market_state",
                          {"codes": ["HK.00700"], "is_contain_ba": "yes"})
        self.assertFalse(out["ok"])
        futu = make_futu(result=SHAPE_RESULT["info_market_state"])
        self.assertTrue(futu.handle("info_market_state",
                                    {"codes": ["HK.00700"],
                                     "is_contain_overnight": True})["ok"])

    def test_history_kline_v2_end_required_and_num_bounds(self):
        for payload in ({"code": "HK.00700"}, {"code": "HK.00700", "end": "2026-13-40"},
                        {"code": "HK.00700", "end": "2026-09-10", "num": 371},
                        {"end": "2026-09-10"}):
            futu = make_futu()
            out = futu.handle("quote_history_kline_v2", payload)
            self.assertFalse(out["ok"], payload)
            self.assertEqual(futu._call.count(), 0, payload)
        futu = make_futu(result=SHAPE_RESULT["quote_history_kline_v2"])
        out = futu.handle("quote_history_kline_v2",
                          {"code": "600519", "end": "2026-09-10", "num": 2})
        self.assertTrue(out["ok"], out)
        self.assertEqual(futu._call.calls[-1],
                         ("quote_history_kline",
                          {"symbol": "SH.600519", "end": "2026-09-10",
                           "ktype": 2, "autype": 1, "num": 2, "extended_time": 0}))


class NormalizationFixtureTest(CacheIsolatedTest):
    """归一化 fixture：两通道产出同形状（rt_quote/order_book/capital_flow 三例对拍）。

    两个通道都打同一官方网关，``data`` 层形状一致（2026-09-16 实测 MCP tools/call
    与官方文档 REST 响应逐键比对）。fixture 用同一份 data 喂两个替身，断言
    handle 的 value 形状一致。
    """

    FIXTURES = {
        "rt_quote": ({"codes": ["HK.00700"]},
                     {"quote_list": [{"code": "HK.00700", "last_price": 434.8}]}),
        "rt_order_book": ({"code": "HK.00700"},
                          [{"books": [{"bid_list": [{"price": 434.6}], "ask_list": []}]}]),
        "capital_flow": ({"code": "HK.00700"},
                         {"flow_list": [{"in_flow": 1}], "last_valid_time": 1789531197000}),
    }

    def test_both_channels_yield_same_shape(self):
        for endpoint, (payload, data) in self.FIXTURES.items():
            mcp = make_futu(call=FakeCall(result=data), channel="mcp")
            openapi = make_futu(call=FakeCall(result=data), channel="openapi",
                                market=RecordingMarket(result=data))
            left = mcp.handle(endpoint, payload)
            right = openapi.handle(endpoint, payload)
            self.assertTrue(left["ok"] and right["ok"], endpoint)
            self.assertEqual(type(left["value"]), type(right["value"]), endpoint)
            self.assertEqual(left["value"], right["value"], endpoint)

    def test_paginated_endpoint_merges_envelope_pagination_on_openapi(self):
        # OpenAPI 信封顶层 pagination 并入 d（与 futu_mcp._unwrap 同规则）。
        data = {"flow_list": [{"in_flow": 1}]}
        futu = make_futu(channel="openapi",
                         market=RecordingMarket(
                             result={**data, "pagination": {"has_more": True}}))
        out = futu.handle("capital_flow_history", {"code": "HK.00700", "days": 7})
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["value"], {**data, "pagination": {"has_more": True}})


# 形状表（caches.ENDPOINT_SHAPE）要求的 data 形状：5 个进缓存端点的替身默认值
# （其余端点用 {"echo": ...} 即可——它们不进形状校验）。
SHAPE_RESULT = {
    "info_basicinfo": {"basic_list": [{"code": "HK.00700"}]},
    "info_trading_days": {"trading_days": []},
    "info_search": {"news_list": []},
    "info_market_state": {"market_state_list": []},
    "quote_history_kline_v2": {"kline_list": []},
}


def result_for(endpoint, default):
    return SHAPE_RESULT.get(endpoint, default)


class FakeFutu:
    """路由替身：记录 (endpoint, payload)，返回固定 envelope。"""

    def __init__(self, envelope=None):
        self.calls = []
        self.envelope = envelope if envelope is not None else {"ok": True, "value": {"echo": 1}}

    def handle(self, endpoint, payload):
        self.calls.append((endpoint, dict(payload)))
        return self.envelope


class AppRoutingTest(CacheIsolatedTest):
    """HTTP/MCP 共用 handle：9 新端点白名单路由 + TTL 缓存语义。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.futu = FakeFutu()
        cls.handler = staticmethod(app_module.create_handler(cls.tmp.name, futu=cls.futu))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def call(self, endpoint, payload):
        return self.handler(endpoint, payload)

    def test_new_endpoints_route_to_futu_provider(self):
        for endpoint, payload in MIN_PAYLOAD_NEW.items():
            out = self.call(endpoint, payload)
            self.assertTrue(out["ok"], endpoint)
            self.assertEqual(self.futu.calls[-1], (endpoint, payload), endpoint)

    def test_field_whitelist_rejects_before_provider(self):
        before = len(self.futu.calls)
        out = self.call("market_snapshot", {"codes": ["HK.00700"], "ticker": "x"})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")
        out = self.call("info_search", {"keyword": "x", "codes": ["HK.00700"]})
        self.assertFalse(out["ok"])
        self.assertEqual(len(self.futu.calls), before, "白名单外字段不得触达通道")

    def test_realtime_new_endpoints_never_cached(self):
        for endpoint, payload in (("market_snapshot", {"codes": ["HK.00700"]}),
                                  ("cur_kline", {"code": "HK.00700", "num": 5}),
                                  ("rt_data", {"code": "HK.00700"}),
                                  ("rt_ticker", {"code": "HK.00700"})):
            futu = make_futu(call=FakeCall(), channel="mcp")
            for _ in range(2):
                out = futu.handle(endpoint, payload)
                self.assertTrue(out["ok"], endpoint)
                self.assertNotIn("cached", out, endpoint)
            self.assertEqual(futu._call.count(), 2, endpoint)

    def test_basic_endpoints_cached_with_ttl(self):
        calls = FakeCall(result={"basic_list": [{"code": "HK.00700"}]})
        futu = make_futu(call=calls, channel="mcp")
        first = futu.handle("info_basicinfo", {"codes": ["HK.00700"]})
        second = futu.handle("info_basicinfo", {"codes": ["HK.00700"]})
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(first.get("cached"), False)
        self.assertEqual(second.get("cached"), True, "TTL 内第二次命中缓存")
        self.assertEqual(calls.count(), 1, "缓存命中不触达通道")
        # _refresh 旁路：强制重取
        refreshed = futu.handle("info_basicinfo", {"codes": ["HK.00700"], "_refresh": True})
        self.assertEqual(refreshed.get("cached"), False)
        self.assertEqual(calls.count(), 2)
        # TTL 锁定：基本类 5m，历史 K 线 v2 10m，实时四类不在表
        for endpoint, ttl in (("info_basicinfo", 5 * 60_000),
                              ("info_trading_days", 5 * 60_000),
                              ("info_search", 5 * 60_000),
                              ("info_market_state", 5 * 60_000),
                              ("quote_history_kline_v2", 10 * 60_000)):
            self.assertEqual(caches.CACHE_TTL_MS.get(endpoint), ttl, endpoint)
        for endpoint in ("market_snapshot", "cur_kline", "rt_data", "rt_ticker"):
            self.assertNotIn(endpoint, caches.CACHE_TTL_MS, endpoint)
            self.assertEqual(caches.CACHE_TTL_MS.get(endpoint, 0), 0, endpoint)
            self.assertNotIn(endpoint, caches.ENDPOINT_SHAPE, endpoint)

    def test_error_envelope_passthrough_without_rewrap(self):
        failing = FakeFutu(envelope={"ok": False,
                                     "error": {"code": "trading/futu-error",
                                               "message": "boom", "details": {}}})
        handler = app_module.create_handler(self.tmp.name, futu=failing)
        out = handler("info_basicinfo", {"codes": ["HK.00700"]})
        self.assertEqual(out, failing.envelope)


class ToolSurfaceTest(unittest.TestCase):
    """工具面 50 锁定（WP8 任务 3 起 56）+ 9 新工具契约。"""

    def test_tool_surface_is_56(self):
        self.assertEqual(mcp_tools.TOOL_COUNT, 61)
        self.assertEqual(len(mcp_tools.TOOLS), 61)
        names = {tool.name for tool in mcp_tools.TOOLS}
        self.assertLessEqual(set(WP8_MARKET_ENDPOINTS), names)
        self.assertNotIn("confirm-decide", names)

    def test_new_tools_map_to_same_named_endpoints(self):
        definitions = {tool.name: tool for tool in mcp_tools.TOOLS}
        for name in WP8_MARKET_ENDPOINTS:
            tool = definitions[name]
            self.assertEqual(tool.kind, "endpoint", name)
            self.assertEqual(tool.endpoint, name, name)

    def test_new_tool_fields_match_http_whitelist(self):
        expected = {
            "market_snapshot": ("codes",),
            "cur_kline": ("code", "num", "ktype", "autype", "extended_time"),
            "rt_data": ("code", "request_section"),
            "rt_ticker": ("code", "num", "period"),
            "info_basicinfo": ("codes",),
            "info_trading_days": ("market", "start", "end"),
            "info_search": ("keyword", "size", "news_type", "sort_type", "lang"),
            "info_market_state": ("codes", "is_contain_ba", "is_contain_overnight"),
            "quote_history_kline_v2": ("code", "start", "end", "ktype", "autype",
                                       "num", "extended_time"),
        }
        definitions = {tool.name: tool for tool in mcp_tools.TOOLS}
        for name, fields in expected.items():
            # 进 TTL 缓存的工具带 refresh 旁路参数（载荷里映射成 _refresh），其余不带
            if name in futu_data.CACHED_FUTU_ENDPOINTS:
                self.assertEqual(definitions[name].fields, fields + ("refresh",), name)
            else:
                self.assertEqual(definitions[name].fields, fields, name)
            self.assertEqual([p.name for p in definitions[name].params if p.required][0],
                             fields[0], f"{name} 首字段必填")
            self.assertEqual(tuple(app_module.FUTU_FIELDS[name]), fields, name)
        self.assertEqual(set(app_module.FUTU_FIELDS),
                         set(futu_data.FUTU_TOOLS), "HTTP 白名单与通道表同键集")

    def test_required_fields_follow_official_docs(self):
        definitions = {tool.name: tool for tool in mcp_tools.TOOLS}
        self.assertEqual([p.name for p in definitions["cur_kline"].params if p.required],
                         ["code", "num"], "官方文档 num 必填")
        self.assertEqual([p.name for p in definitions["info_trading_days"].params
                          if p.required], ["market", "start", "end"])
        self.assertEqual([p.name for p in definitions["quote_history_kline_v2"].params
                          if p.required], ["code", "end"])

    def test_new_descriptions_state_channel_and_realtime(self):
        for tool in mcp_tools.TOOLS:
            if tool.name not in WP8_MARKET_ENDPOINTS:
                continue
            self.assertIn("富途", tool.description, tool.name)
        for name in ("market_snapshot", "cur_kline", "rt_data", "rt_ticker"):
            tool = next(t for t in mcp_tools.TOOLS if t.name == name)
            self.assertIn("实时", tool.description, name)

    def test_endpoint_registry_is_61(self):
        endpoints = store_access.endpoints()
        self.assertEqual(len(endpoints), 61)
        self.assertEqual(tuple(store_access.WP8_MARKET_ENDPOINTS), WP8_MARKET_ENDPOINTS)
        # WP8 任务 3 起尾部再追加 6 个 OpenAPI 交易只读端点、任务 6 追加 3 个推送端点、
        # 任务 7 追加设置页端点（openapi_config/openapi_test；OAuth 集成再增 openapi_oauth，
        # 行情 9 项落在它们之前）
        self.assertEqual(endpoints[-24:-15], list(WP8_MARKET_ENDPOINTS))
        self.assertEqual(len(set(endpoints)), 61)


class RealChannelComparisonTest(unittest.TestCase):
    """真实通道对拍（**可选慢测**，任务 B）：``DSH_WP8_SLOW=1`` 且两通道凭据齐备才跑。

    对 rt_quote/rt_order_book/capital_flow/capital_distribution/option_expiration 五个
    端点各调一次真实 MCP 通道与真实 OpenAPI 通道（同一官方网关），断言两通道产出
    同形状（dict 顶层键集一致；list 则比对元素键集）。任何一侧通道不可用（无 MCP
    token / 无 openapi 凭据 / 网络）则 skipUnless 直接跳过——CI 与日常开发不触网。
    """

    SLOW_PAIRS = (
        ("rt_quote", {"codes": ["HK.00700"]}, {"codes": ["HK.00700"]}),
        ("rt_order_book", {"code": "HK.00700"}, {"code": "HK.00700"}),
        ("capital_flow", {"code": "HK.00700"}, {"code": "HK.00700"}),
        ("capital_distribution", {"code": "HK.00700"}, {"code": "HK.00700"}),
        ("option_expiration", {"code": "HK.00700"}, {"code": "HK.00700"}),
    )

    def setUp(self):
        if os.environ.get("DSH_WP8_SLOW") != "1":
            self.skipTest("慢测：设 DSH_WP8_SLOW=1 才跑真实通道对拍")
        from trading_datasource import futu_mcp
        if not futu_mcp.has_token():
            self.skipTest("慢测：MCP 通道无 token")
        from trading_datasource.futu_openapi import CredentialStore
        futu = futu_data.FutuData(home=None)
        if not futu._openapi_ready():
            self.skipTest("慢测：openapi 无凭据（先运行 scripts/futu_auth.py --openapi）")

    def test_both_live_channels_yield_same_shape(self):
        mcp_side = futu_data.FutuData(channel="mcp")
        openapi_side = futu_data.FutuData(channel="openapi")
        for endpoint, mcp_payload, openapi_payload in self.SLOW_PAIRS:
            with self.subTest(endpoint=endpoint):
                left = mcp_side.handle(endpoint, mcp_payload)
                right = openapi_side.handle(endpoint, openapi_payload)
                self.assertTrue(left["ok"], (endpoint, left))
                self.assertTrue(right["ok"], (endpoint, right))
                self.assertEqual(
                    _shape(left["value"]), _shape(right["value"]),
                    f"{endpoint}: 两通道形状不一致（已知残余差异登记在 futu_data 文件头）")


def _shape(value):
    """递归形状：dict → 键 → 子形状；list → 元素形状（取第一个）；标量 → 类型名。"""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


if __name__ == "__main__":
    unittest.main(verbosity=2)
