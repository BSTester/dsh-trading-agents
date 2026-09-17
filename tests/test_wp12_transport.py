"""WP12 任务 2：数据面传输方法组七类（筛选/板块/做空/基础数据/IPO/自选/衍生品）。

**唯一事实源**：`docs/superpowers/plans/wp12-endpoint-lock.md`（2026-09-16 官方文档逐页核对，
53 条新接入目标；`llms.txt` 的 F10 链接已实测 404，严禁按 llms.txt 猜路径）。

本文件守四件事：

* **出站逐字一致**：每个方法的 (method, path, query/body) 与锁定表路径/参数名一致
  （``RecordingClient`` 入参断言——不经过任何序列化层，缺陷无处可藏）；
* **坏参数本地拒绝、零网络往返**：白名单外关键字 → ``TypeError``（签名即白名单）；
  枚举/区间/结构非法 → ``ValueError``，且 ``client.calls == []``；
* **错误码语义**：``-10 no_data`` → 空而非错（``{"no_data": True}``，不伪造 items/字段）；
  ``-8 unsupported`` 中**本地可判定**的情形（板块 REGION 非 SH/SZ、做空非 HK/US）前置拒绝；
  ``-9``（无权限/用户身份无效）如实抛出且消息可读——**不重试、不当空数据**；
* **锁定表绑定**：已实现端点必须能在锁定表里找到同 (方法, 路径模板) 行，防路径漂移。

范围：任务 2 的七类 18 个端点；F10 26 项属任务 3，模拟交易 9 项属任务 4/5。
"""
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
sys.path.insert(0, str(ROOT / "tests"))

from test_wp8_market import RecordingClient  # noqa: E402  （复用既有替身，不另造一套）

from trading_datasource.futu_openapi import (  # noqa: E402
    WP12_TRANSPORT_ENDPOINTS,
    OpenApiBasicData,
    OpenApiDerivatives,
    OpenApiError,
    OpenApiIpo,
    OpenApiPlate,
    OpenApiScreen,
    OpenApiShort,
    OpenApiWatchlist,
)

LOCK = ROOT / "docs" / "superpowers" / "plans" / "wp12-endpoint-lock.md"
METHODS = ("GET", "POST", "PUT", "DELETE")
ROW_RE = re.compile(r"\b(GET|POST|PUT|DELETE)\s+`(/api/v1\.0/[^`]+)`")


def lock_pairs():
    """锁定表的 (方法, 路径模板) 集合（§C 清单章节；§C.10 范围外族不参与）。"""
    section = ""
    pairs = set()
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        if line.startswith("### "):
            section = line[4:].strip()
        if section.startswith("C.10") or not line.startswith("|"):
            continue
        found = ROW_RE.search(line)
        if found:
            pairs.add((found.group(1), found.group(2)))
    return pairs


def make(cls, d=None, pagination=None, error=None):
    return cls(RecordingClient(d=d, pagination=pagination, error=error))


class ScreenTransportTests(unittest.TestCase):
    """§C.3 全市场筛选：stock-screen（11 选 1 查询）/ warrant-screen。"""

    def test_stock_screen_posts_queries_and_merges_pagination(self):
        screen = make(OpenApiScreen, d={"items": []}, pagination={"total": 0})
        out = screen.stock_screen(
            screen_queries=[{"simple_field_query": {"simple_field": 1,
                                                    "screen_value_list": [1]}}],
            retrieve_queries=[{"simple_property": {"name": 2201}}],
            sort={"direction": 2, "simple_property": {"name": 2301}},
            next_key=None, limit=300, user_stock_list_mode=1)
        self.assertEqual(screen.client.calls, [
            ("request_meta", "POST", "/api/v1.0/quote/stock-screen", None, {
                "screen_queries": [{"simple_field_query": {"simple_field": 1,
                                                           "screen_value_list": [1]}}],
                "retrieve_queries": [{"simple_property": {"name": 2201}}],
                "sort": {"direction": 2, "simple_property": {"name": 2301}},
                "limit": 300,
                "user_stock_list_mode": 1})])
        self.assertEqual(out["pagination"], {"total": 0})

    def test_stock_screen_rejects_bad_shape_locally(self):
        for kwargs in (
            {},                                                     # 缺 screen_queries
            {"screen_queries": []},                                 # 空数组
            {"screen_queries": "x"},                                # 非数组
            {"screen_queries": [{}, {}]},                           # 元素缺键
            {"screen_queries": [{"a": 1, "b": 2}]},                 # 两个键（11 选 1）
            {"screen_queries": [{"unknown_query": 1}]},             # 白名单外
            {"screen_queries": [{"plate_query": 1}],
             "retrieve_queries": [{"nope": 1}]},                    # retrieve 白名单外
            {"screen_queries": [{"plate_query": 1}], "limit": 301},
            {"screen_queries": [{"plate_query": 1}], "limit": 0},
            {"screen_queries": [{"plate_query": 1}], "user_stock_list_mode": 3},
            {"screen_queries": [{"plate_query": 1}],
             "sort": {"direction": 5}},
            {"screen_queries": [{"plate_query": 1}], "sorts": ["x"]},
        ):
            with self.subTest(kwargs=kwargs):
                client = RecordingClient()
                with self.assertRaises((ValueError, TypeError)):
                    OpenApiScreen(client).stock_screen(**kwargs)
                self.assertEqual(client.calls, [], "坏参数必须零网络往返")

    def test_stock_screen_unknown_keyword_is_type_error(self):
        client = RecordingClient()
        with self.assertRaises(TypeError):
            OpenApiScreen(client).stock_screen([{"plate_query": 1}], market="HK")
        self.assertEqual(client.calls, [])

    def test_warrant_screen_posts_official_fields(self):
        screen = make(OpenApiScreen, d={"items": []})
        screen.warrant_screen(market_type=1, limit=1000, stock_owner="HK.00700")
        self.assertEqual(screen.client.calls[-1],
                         ("request_meta", "POST", "/api/v1.0/quote/warrant-screen",
                          None, {"market_type": 1, "limit": 1000,
                                 "stock_owner": "HK.00700"}))
        for kwargs in ({"market_type": 2}, {"market_type": 1, "limit": 1001},
                       {"market_type": 1, "sorts": "x"}):
            with self.subTest(kwargs=kwargs):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiScreen(client).warrant_screen(**kwargs)
                self.assertEqual(client.calls, [])


class PlateTransportTests(unittest.TestCase):
    """§C.2 板块：plate-list（REGION 仅 SH/SZ）/ plate-stock。"""

    def test_plate_list_gets_market_and_class(self):
        plate = make(OpenApiPlate, d={"plate_list": []})
        plate.plate_list("HK", "INDUSTRY")
        self.assertEqual(plate.client.calls, [
            ("request", "GET", "/api/v1.0/quote/plate-list",
             {"market": "HK", "plate_class": "INDUSTRY"}, None)])

    def test_plate_list_region_unsupported_pre_rejected(self):
        for market in ("HK", "US"):
            with self.subTest(market=market):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiPlate(client).plate_list(market, "REGION")
                self.assertEqual(client.calls, [], "-8 本地可判定 → 前置拒绝，零网络")
        make(OpenApiPlate).plate_list("SH", "REGION")  # SH/SZ 合法
        make(OpenApiPlate).plate_list("SZ", "REGION")

    def test_plate_list_bad_enum_and_market(self):
        for market, plate_class in (("HK", "industry"), ("HK", "ALLS"),
                                    ("", "ALL"), ("hk", "ALL"), (None, "ALL")):
            with self.subTest(market=market, plate_class=plate_class):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiPlate(client).plate_list(market, plate_class)
                self.assertEqual(client.calls, [])

    def test_plate_stock_merges_pagination(self):
        plate = make(OpenApiPlate, d={"stock_list": []},
                     pagination={"has_more": True, "next_key": "2"})
        out = plate.plate_stock("HK.LIST23618", limit=50, ascend=1)
        self.assertEqual(plate.client.calls[-1],
                         ("request_meta", "GET", "/api/v1.0/quote/plate-stock",
                          {"plate_code": "HK.LIST23618", "limit": 50, "ascend": 1}, None))
        self.assertEqual(out["pagination"]["next_key"], "2")
        client = RecordingClient()
        with self.assertRaises(ValueError):
            OpenApiPlate(client).plate_stock(None)
        with self.assertRaises(ValueError):
            OpenApiPlate(client).plate_stock("HK.LIST23618", ascend={"a": 1})
        self.assertEqual(client.calls, [])


class ShortTransportTests(unittest.TestCase):
    """§C.6 卖空：仅 HK/US 可卖空证券；``-10`` 视为无数据。"""

    def test_short_daily_volume_path_and_count(self):
        short = make(OpenApiShort, d={"items": []})
        short.short_daily_volume("HK.00700", count=90)
        self.assertEqual(short.client.calls, [
            ("request", "GET", "/api/v1.0/quote/HK.00700/short/daily-volume",
             {"count": 90}, None)])
        short.short_interest("US.AAPL")
        self.assertEqual(short.client.calls[-1],
                         ("request", "GET", "/api/v1.0/quote/US.AAPL/short/interest",
                          {}, None))

    def test_short_market_guard_is_local(self):
        for symbol in ("SH.600519", "SZ.000001", "SG.D05", "HK", "", "600519"):
            with self.subTest(symbol=symbol):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiShort(client).short_daily_volume(symbol)
                self.assertEqual(client.calls, [], "仅 HK/US（-8）本地可判定 → 零网络")

    def test_short_symbol_is_normalized_like_trade_group(self):
        """小写入参归一为大写（与 OpenApiTrade._symbol 同一实现，非本地拒绝）。"""
        short = make(OpenApiShort, d={"items": []})
        short.short_daily_volume("hk.00700")
        self.assertEqual(short.client.calls, [
            ("request", "GET", "/api/v1.0/quote/HK.00700/short/daily-volume",
             {}, None)])

    def test_short_count_bounds(self):
        for count in (0, 91, True, "30"):
            with self.subTest(count=count):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiShort(client).short_daily_volume("HK.00700", count=count)
                self.assertEqual(client.calls, [])

    def test_short_no_data_is_empty_not_error(self):
        for call in ("daily", "interest"):
            with self.subTest(call=call):
                short = make(OpenApiShort, error=OpenApiError("no_data", errcode=-10))
                out = (short.short_daily_volume("HK.00700") if call == "daily"
                       else short.short_interest("HK.00700"))
                self.assertEqual(out, {"no_data": True})

    def test_short_server_unsupported_still_raises(self):
        """非本地可判定的 -8（如标的存在但不可卖空）如实抛出，不当空数据。"""
        short = make(OpenApiShort, error=OpenApiError("unsupported", errcode=-8))
        with self.assertRaises(OpenApiError) as ctx:
            short.short_daily_volume("HK.00700")
        self.assertEqual(ctx.exception.errcode, -8)


class BasicDataTransportTests(unittest.TestCase):
    """§C.1 基础数据：经济日历 hot/search、所属板块、复权因子。"""

    def test_economic_calendar_hot_gets_bounded_limit(self):
        basic = make(OpenApiBasicData, d={"economic_calendar_list": []},
                     pagination={"next_key": "-1"})
        basic.economic_calendar_hot(limit=20, date="2026-09-16")
        self.assertEqual(basic.client.calls, [
            ("request_meta", "GET", "/api/v1.0/quote/economic-calendar/hot",
             {"limit": 20, "date": "2026-09-16"}, None)])
        for kwargs in ({"limit": 21}, {"limit": 0}, {"date": "2026-13-01"},
                       {"date": "16/09/2026"}):
            with self.subTest(kwargs=kwargs):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiBasicData(client).economic_calendar_hot(**kwargs)
                self.assertEqual(client.calls, [])

    def test_economic_calendar_search_requires_keyword_and_type(self):
        basic = make(OpenApiBasicData, d={"economic_calendar_list": []})
        basic.economic_calendar_search("CPI", 1)
        self.assertEqual(basic.client.calls, [
            ("request_meta", "GET", "/api/v1.0/quote/economic-calendar/search",
             {"keyword": "CPI", "search_type": 1}, None)])
        for args in ((), ("CPI",), ("", 1), ("CPI", 0), ("CPI", 5), ("CPI", "1")):
            with self.subTest(args=args):
                client = RecordingClient()
                with self.assertRaises((ValueError, TypeError)):
                    OpenApiBasicData(client).economic_calendar_search(*args)
                self.assertEqual(client.calls, [])

    def test_economic_calendar_search_list_data_keeps_pagination(self):
        """实测：该端点 data 层是**数组**，信封顶层才带 pagination。

        `{**d, "pagination": ...}` 对数组直接 TypeError（真机 --dataplane 自检抓到）。
        数组无法承载合并键，故按官方 screen 一族的词汇归一为
        `{"items": [...], "pagination": {...}}`——既不丢游标，也不假装数组是对象。
        """

        class _ListClient:
            def __init__(self, d, pagination=None):
                self.calls = []
                self.d = d
                self.pagination = pagination

            def request(self, method, path, query=None, json_body=None):
                self.calls.append(("request", method, path, query, json_body))
                return self.d

            def request_meta(self, method, path, query=None, json_body=None):
                self.calls.append(("request_meta", method, path, query, json_body))
                return self.d, self.pagination

        rows = [{"event_text": "澳大利亚 CPI 年率"}]
        client = _ListClient(rows, {"has_more": True, "next_key": "20"})
        out = OpenApiBasicData(client).economic_calendar_search("CPI", 1)
        self.assertEqual(out, {"items": rows, "pagination": {"has_more": True, "next_key": "20"}})
        # 无分页时数组原样返回（不包壳）
        client = _ListClient(rows)
        self.assertEqual(OpenApiBasicData(client).economic_calendar_search("CPI", 1), rows)

    def test_owner_plate_path_and_symbol_shape(self):
        basic = make(OpenApiBasicData, d={"plate_list": []})
        basic.owner_plate("SH.600519")
        self.assertEqual(basic.client.calls, [
            ("request", "GET", "/api/v1.0/quote/SH.600519/owner-plate",
             None, None)])
        for symbol in (None, "", "600519", "A" * 40):
            with self.subTest(symbol=symbol):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiBasicData(client).owner_plate(symbol)
                self.assertEqual(client.calls, [])

    def test_rehab_uses_corporate_actions_path(self):
        """锁定表 §C.1 路径注意：文档归属基本数据，REST 在 /corporate-actions/rehab。"""
        basic = make(OpenApiBasicData, d={"rehabs": []})
        basic.rehab("HK.00700", divi_mode=1)
        self.assertEqual(basic.client.calls, [
            ("request", "GET", "/api/v1.0/quote/HK.00700/corporate-actions/rehab",
             {"divi_mode": 1}, None)])


class IpoTransportTests(unittest.TestCase):
    """§C.4 IPO：每市场独立路径，market 小写。"""

    def test_ipo_list_market_is_lowercase_path_segment(self):
        ipo = make(OpenApiIpo, d={"ipo_list": []})
        ipo.ipo_list("hk")
        self.assertEqual(ipo.client.calls, [
            ("request", "GET", "/api/v1.0/quote/ipo-list/hk", {}, None)])
        ipo.ipo_list("cn", request_type=4)
        self.assertEqual(ipo.client.calls[-1],
                         ("request", "GET", "/api/v1.0/quote/ipo-list/cn",
                          {"request_type": 4}, None))

    def test_ipo_list_rejects_market_and_request_type(self):
        for market, request_type in (("HK", None), ("jp", None), (None, None),
                                     ("hk", 12), ("hk", 0), ("hk", True)):
            with self.subTest(market=market, request_type=request_type):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiIpo(client).ipo_list(market, request_type=request_type)
                self.assertEqual(client.calls, [])


class WatchlistTransportTests(unittest.TestCase):
    """§C.8 自选：列表/分组只读；修改自选为 HTTP-only（注册面由任务 4 决定）。"""

    def test_watchlist_list_requires_group_name(self):
        watch = make(OpenApiWatchlist, d={"stock_list": []})
        watch.watchlist_list("默认分组")
        self.assertEqual(watch.client.calls, [
            ("request", "GET", "/api/v1.0/quote/user-security",
             {"group_name": "默认分组"}, None)])
        for group in (None, "", "x" * 101):
            with self.subTest(group=group):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiWatchlist(client).watchlist_list(group)
                self.assertEqual(client.calls, [])

    def test_watchlist_groups_enum_is_case_sensitive(self):
        watch = make(OpenApiWatchlist, d={"group_list": []})
        watch.watchlist_groups("CUSTOM")
        self.assertEqual(watch.client.calls, [
            ("request", "GET", "/api/v1.0/quote/user-security-group",
             {"group_type": "CUSTOM"}, None)])
        watch.watchlist_groups()
        self.assertEqual(watch.client.calls[-1],
                         ("request", "GET", "/api/v1.0/quote/user-security-group",
                          {}, None))
        for group_type in ("custom", "ALLS", 1):
            with self.subTest(group_type=group_type):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiWatchlist(client).watchlist_groups(group_type)
                self.assertEqual(client.calls, [])

    def test_modify_user_security_posts_op_and_codes(self):
        watch = make(OpenApiWatchlist, d={"result_code": 0})
        watch.modify_user_security("ADD", ["HK.00700"], group_name="自选")
        self.assertEqual(watch.client.calls, [
            ("request", "POST", "/api/v1.0/quote/modify-user-security", None,
             {"op": "ADD", "code_list": ["HK.00700"], "group_name": "自选"})])
        for kwargs in ({}, {"op": "ADD"}, {"op": "ADD", "code_list": []},
                       {"op": "ADD", "code_list": ["HK.00700"] * 201},
                       {"op": "", "code_list": ["HK.00700"]},
                       {"op": "ADD", "code_list": ["HK.00700"],
                        "group_name": "x" * 101}):
            with self.subTest(kwargs=kwargs):
                client = RecordingClient()
                with self.assertRaises((ValueError, TypeError)):
                    OpenApiWatchlist(client).modify_user_security(**kwargs)
                self.assertEqual(client.calls, [])

    def test_modify_user_security_op_whitelist(self):
        """``op`` 白名单来自真机 -3 原文（``allowed: [ADD, DEL, MOVE_OUT]``）。

        官方文档只写「op 非法 → -3」不列枚举，因此取值依据是上游错误原文（E2E 2026-09-17）。
        非法值必须**本地拒绝且零网络往返**——此前非空字符串直通，调用方会拿到上游
        ``futu-error``，被误导去查通道而不是查自己的载荷。
        """
        # 合法值（含常见小写写法）→ 归一到官方大写 token 后发一次
        for given, sent in (("ADD", "ADD"), ("add", "ADD"), ("Add", "ADD"),
                            ("DEL", "DEL"), ("MOVE_OUT", "MOVE_OUT"), ("del", "DEL")):
            with self.subTest(op=given):
                client = RecordingClient(d={"result_code": 0})
                OpenApiWatchlist(client).modify_user_security(given, ["HK.00700"])
                self.assertEqual(client.calls[-1][4]["op"], sent)

        # 非法值 → ValueError（消息含允许值）、零调用
        for op in ("NOT_A_REAL_OP", "MOVE", "ADDS", "DELETE", "add x"):
            with self.subTest(op=op):
                client = RecordingClient()
                with self.assertRaises(ValueError) as ctx:
                    OpenApiWatchlist(client).modify_user_security(op, ["HK.00700"])
                self.assertIn("ADD", str(ctx.exception))
                self.assertIn("MOVE_OUT", str(ctx.exception))
                self.assertEqual(client.calls, [], "非法 op 不得发起任何上游调用")

        # 空白值走「必填」分支（`_text` 的非空判定），同样是本地拒绝 + 零调用
        for op in ("", "   "):
            with self.subTest(op=op):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiWatchlist(client).modify_user_security(op, ["HK.00700"])
                self.assertEqual(client.calls, [])
        self.assertIn("ADD", OpenApiWatchlist.MODIFY_OPS)
        self.assertEqual(OpenApiWatchlist.MODIFY_OPS, ("ADD", "DEL", "MOVE_OUT"))


class DerivativesTransportTests(unittest.TestCase):
    """§C.7 衍生品：future-info（批量）/ reference-future / 波动率 / 行权概率。"""

    def test_future_info_posts_code_list(self):
        deriv = make(OpenApiDerivatives, d={"future_list": []})
        deriv.future_info(["HK.HSImain"])
        self.assertEqual(deriv.client.calls, [
            ("request", "POST", "/api/v1.0/quote/future-info", None,
             {"code_list": ["HK.HSImain"]})])
        client = RecordingClient()
        with self.assertRaises(ValueError):
            OpenApiDerivatives(client).future_info([])
        with self.assertRaises(ValueError):
            OpenApiDerivatives(client).future_info(["X"] * 401)
        self.assertEqual(client.calls, [])

    def test_reference_future_path(self):
        deriv = make(OpenApiDerivatives, d={"reference_list": []})
        deriv.reference_future("HK.00700")
        self.assertEqual(deriv.client.calls, [
            ("request", "GET", "/api/v1.0/quote/HK.00700/reference-future",
             None, None)])

    def test_option_volatility_periods(self):
        deriv = make(OpenApiDerivatives, d={"implied_volatility": 0.3})
        deriv.option_volatility("HK.HSI26000", query_time_period=5, hv_time_period=250)
        self.assertEqual(deriv.client.calls, [
            ("request", "GET", "/api/v1.0/quote/HK.HSI26000/option-volatility",
             {"query_time_period": 5, "hv_time_period": 250}, None)])
        for kwargs in ({"query_time_period": 0}, {"query_time_period": 6},
                       {"hv_time_period": 4}, {"hv_time_period": 251}):
            with self.subTest(kwargs=kwargs):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiDerivatives(client).option_volatility("HK.HSI26000", **kwargs)
                self.assertEqual(client.calls, [])

    def test_option_volatility_no_data_is_empty(self):
        deriv = make(OpenApiDerivatives, error=OpenApiError("no data", errcode=-10))
        self.assertEqual(deriv.option_volatility("HK.HSI26000"), {"no_data": True})

    def test_option_exercise_probability_limit_and_path(self):
        deriv = make(OpenApiDerivatives, d={"strike_probability": []})
        deriv.option_exercise_probability("HK.HSI26000", limit=1000)
        self.assertEqual(deriv.client.calls, [
            ("request", "GET",
             "/api/v1.0/quote/HK.HSI26000/option-exercise-probability",
             {"limit": 1000}, None)])
        for limit in (0, 1001, "10"):
            with self.subTest(limit=limit):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiDerivatives(client).option_exercise_probability(
                        "HK.HSI26000", limit=limit)
                self.assertEqual(client.calls, [])


class PermissionSemanticsTests(unittest.TestCase):
    """``-9``：如实抛出且可读，绝不退化成空数据（锁定表 §D.3 期权权限项）。"""

    def test_exercise_probability_permission_error_is_readable(self):
        deriv = make(OpenApiDerivatives,
                     error=OpenApiError("no permission", errcode=-9))
        with self.assertRaises(OpenApiError) as ctx:
            deriv.option_exercise_probability("HK.HSI26000")
        error = ctx.exception
        self.assertEqual(error.errcode, -9)
        self.assertIn("权限", str(error))
        self.assertIn("no permission", str(error), "官方原文必须保留")
        self.assertEqual(len(deriv.client.calls), 1, "不重试")

    def test_watchlist_identity_error_is_readable(self):
        watch = make(OpenApiWatchlist, error=OpenApiError("invalid uid", errcode=-9))
        with self.assertRaises(OpenApiError) as ctx:
            watch.watchlist_list("默认分组")
        self.assertEqual(ctx.exception.errcode, -9)
        self.assertIn("身份", str(ctx.exception))
        self.assertEqual(len(watch.client.calls), 1, "不重试")

    def test_other_errors_pass_through_untouched(self):
        for errcode in (-3, -5, -7, -8):
            with self.subTest(errcode=errcode):
                plate = make(OpenApiPlate, error=OpenApiError("e", errcode=errcode))
                with self.assertRaises(OpenApiError) as ctx:
                    plate.plate_list("HK", "ALL")
                self.assertEqual(ctx.exception.errcode, errcode)
                # 缺陷 2（E2E 2026-09-17）：错误文本现在**带上游错误码**，原文逐字保留。
                # 此前只有 "e"，写进 OMS/日志后无法判断是权限、限频还是参数问题。
                self.assertEqual(str(ctx.exception), f"[errcode={errcode}] e")
                self.assertIn("e", str(ctx.exception), "原文必须保留")


class LockTableBindingTests(unittest.TestCase):
    """防猜路径：实现用到的每个 (方法, 路径模板) 必须在锁定表里有同形行。"""

    def test_binding_surface_covers_task2_endpoints(self):
        self.assertEqual(len(WP12_TRANSPORT_ENDPOINTS), 18,
                         "任务 2 七类共 18 个端点（F10 26 项属任务 3、模拟交易属任务 4/5）")
        classes = {key[0] for key in WP12_TRANSPORT_ENDPOINTS}
        self.assertEqual(classes, {"OpenApiScreen", "OpenApiPlate", "OpenApiShort",
                                   "OpenApiBasicData", "OpenApiIpo", "OpenApiWatchlist",
                                   "OpenApiDerivatives"})

    def test_every_implemented_endpoint_exists_in_lock_table(self):
        pairs = lock_pairs()
        self.assertGreater(len(pairs), 40, "锁定表解析失败（应含 40+ 条端点行）")
        for (cls_name, method_name), (http_method, path) in WP12_TRANSPORT_ENDPOINTS.items():
            with self.subTest(endpoint=f"{cls_name}.{method_name}", path=path):
                self.assertIn((http_method, path), pairs,
                              f"{http_method} {path} 不在锁定表——禁止未核对路径落地")

    def test_f10_and_sim_trade_are_not_in_task2_surface(self):
        """任务边界：F10（任务 3）与模拟交易（任务 4/5）不得混入任务 2 的绑定面。"""
        paths = {path for _, path in WP12_TRANSPORT_ENDPOINTS.values()}
        for forbidden in ("financials/", "research/", "shareholders/", "company/",
                          "top-brokers", "valuation/", "sim-trade"):
            with self.subTest(fragment=forbidden):
                self.assertFalse([p for p in paths if forbidden in p],
                                 f"{forbidden} 不属于任务 2")


if __name__ == "__main__":
    unittest.main()
