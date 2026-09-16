"""WP12 任务 3：``OpenApiF10`` 方法组（个股深度数据，锁定表 §C.5 的 26 项）。

**唯一事实源**：`docs/superpowers/plans/wp12-endpoint-lock.md` §C.5（官方 7 个命名空间：
``financials/`` / ``research/`` / ``valuation/`` / ``corporate-actions/`` / ``shareholders/`` /
``company/`` / ``top-brokers/``，2026-09-16 逐页核对；``llms.txt`` 的 ``/f10/*.md`` 已实测
**全部 404**，严禁按 llms.txt 猜路径）。

本文件守五件事：

* **表驱动逐行一致**：26 个 section 各一条用例，(method, path, query/body) 与锁定表路径/
  参数名逐字一致（``RecordingClient`` 入参断言，不经过序列化层）；
* **未知 section 本地拒绝**：``f10()`` 分发面对白名单外 section 抛 ``ValueError`` 且零 HTTP；
* **响应原样透传**：``pub_trading_day``/``period_text``/``announced_at`` 等字段不被解释、
  不被归一、不被丢弃（PIT 口径属落库层，传输层只搬运）；
* **错误码分派**：``-10 no_data`` 只在锁定表列出该语义的 section 上转空（``{"no_data": True}``）；
  未列出的 section 与 ``-9``/``-3`` 一律如实抛出、不重试、不吞错；
* **锁定表绑定**：26 个 (方法, 路径) 必须能在锁定表找到同行，防路径漂移与猜路径回流。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
sys.path.insert(0, str(ROOT / "tests"))

from test_wp12_transport import lock_pairs  # noqa: E402  （复用锁定表解析，不另造一套）
from test_wp8_market import RecordingClient  # noqa: E402  （复用既有替身）

from trading_datasource.futu_openapi import (  # noqa: E402
    WP12_F10_ENDPOINTS,
    OpenApiError,
    OpenApiF10,
)

QUOTE = "/api/v1.0/quote"


def make(d=None, pagination=None, error=None):
    return OpenApiF10(RecordingClient(d=d, pagination=pagination, error=error))


def call(f10, section, symbol, **params):
    return f10.f10(symbol, section, **params)


#: (section, symbol, kwargs, 记录名, HTTP 方法, 路径, query/body)
#: 路径与参数名逐字对照锁定表 §C.5；分页并入的 section 用 request_meta（锁定表 §A：
#: 游标来自信封 ``pagination.next_key``）。
CALLS = (
    ("earnings_price_move", "HK.00700", {"count": 50, "overview_count": 8},
     "request", "GET", f"{QUOTE}/HK.00700/financials/earnings-price-move",
     {"count": 50, "overview_count": 8}),
    ("earnings_price_history", "US.AAPL", {},
     "request", "GET", f"{QUOTE}/US.AAPL/financials/earnings-price-history", None),
    ("statements", "HK.00700", {"statement_type": 1, "limit": 10},
     "request_meta", "GET", f"{QUOTE}/HK.00700/financials/statements",
     {"statement_type": 1, "limit": 10}),
    ("revenue_breakdown", "HK.00700", {"date": "2026-06-30"},
     "request", "GET", f"{QUOTE}/HK.00700/financials/revenue-breakdown",
     {"date": "2026-06-30"}),
    ("analyst_consensus", "HK.00700", {},
     "request", "GET", f"{QUOTE}/HK.00700/research/analyst-consensus", None),
    ("rating_summary", "HK.00700", {"rating_dimension_type": 1, "limit": 20},
     "request_meta", "GET", f"{QUOTE}/HK.00700/research/rating-summary",
     {"rating_dimension_type": 1, "limit": 20}),
    ("morningstar", "HK.00700", {},
     "request", "GET", f"{QUOTE}/HK.00700/research/morningstar", None),
    ("valuation_detail", "HK.00700", {"valuation_type": 1, "interval_type": 10},
     "request", "GET", f"{QUOTE}/HK.00700/valuation/detail",
     {"valuation_type": 1, "interval_type": 10}),
    ("valuation_plate_stocks", "HK.LIST23618", {"limit": 50},
     "request_meta", "GET", f"{QUOTE}/valuation/plate-stocks",
     {"symbol": "HK.LIST23618", "limit": 50}),
    ("valuation_index_stocks", "SH.000300", {"limit": 50},
     "request_meta", "GET", f"{QUOTE}/valuation/index-stocks",
     {"symbol": "SH.000300", "limit": 50}),
    ("valuation_index_stock_plates", "SH.000300", {},
     "request", "GET", f"{QUOTE}/valuation/index-stock-plates",
     {"symbol": "SH.000300"}),
    ("dividends", "HK.00700", {},
     "request", "GET", f"{QUOTE}/HK.00700/corporate-actions/dividends", None),
    ("buybacks", "HK.00700", {"limit": 50},
     "request_meta", "GET", f"{QUOTE}/HK.00700/corporate-actions/buybacks",
     {"limit": 50}),
    ("splits", "HK.00700", {},
     "request", "GET", f"{QUOTE}/HK.00700/corporate-actions/splits", None),
    ("shareholders_overview", "HK.00700", {"period_id": "2026"},
     "request", "GET", f"{QUOTE}/HK.00700/shareholders/overview",
     {"period_id": "2026"}),
    ("holding_changes", "HK.00700", {"limit": 10},
     "request_meta", "GET", f"{QUOTE}/HK.00700/shareholders/holding-changes",
     {"limit": 10}),
    ("holder_detail", "HK.00700", {"limit": 10},
     "request_meta", "GET", f"{QUOTE}/HK.00700/shareholders/holder-detail",
     {"limit": 10}),
    ("institutional", "HK.00700", {"limit": 10},
     "request_meta", "GET", f"{QUOTE}/HK.00700/shareholders/institutional",
     {"limit": 10}),
    ("insider_holders", "US.AAPL", {"limit": 30},
     "request_meta", "GET", f"{QUOTE}/US.AAPL/shareholders/insider-holders",
     {"limit": 30}),
    ("insider_trades", "US.AAPL", {"limit": 50},
     "request_meta", "GET", f"{QUOTE}/US.AAPL/shareholders/insider-trades",
     {"limit": 50}),
    ("company_profile", "HK.00700", {},
     "request", "GET", f"{QUOTE}/HK.00700/company/profile", None),
    ("company_executives", "HK.00700", {},
     "request", "GET", f"{QUOTE}/HK.00700/company/executives", None),
    ("company_executive_background", "HK.00700", {"leader_name": "张三"},
     "request", "GET", f"{QUOTE}/HK.00700/company/executive-background",
     {"leader_name": "张三"}),
    ("company_operational_efficiency", "HK.00700", {"limit": 100, "financial_type": 7},
     "request_meta", "GET", f"{QUOTE}/HK.00700/company/operational-efficiency",
     {"limit": 100, "financial_type": 7}),
    ("top_brokers", "HK.00700", {"date": "2026-09-16"},
     "request", "GET", f"{QUOTE}/HK.00700/top-brokers",
     {"date": "2026-09-16"}),
    ("top_brokers_history", "HK.00700", {"days_before": 365},
     "request", "GET", f"{QUOTE}/HK.00700/top-brokers-history", {"days_before": 365}),
)

#: 锁定表 §C.5 逐行声明的 section（26 项）——测试自身也以此断言分发面完整。
LOCK_SECTIONS = (
    "earnings_price_move", "earnings_price_history", "statements", "revenue_breakdown",
    "analyst_consensus", "rating_summary", "morningstar",
    "valuation_detail", "valuation_plate_stocks", "valuation_index_stocks",
    "valuation_index_stock_plates",
    "dividends", "buybacks", "splits",
    "shareholders_overview", "holding_changes", "holder_detail", "institutional",
    "insider_holders", "insider_trades",
    "company_profile", "company_executives", "company_executive_background",
    "company_operational_efficiency",
    "top_brokers", "top_brokers_history",
)

#: 锁定表 §C.5 显式列出 ``-10 no_data`` 的 section（其余不得把 -10 吞成空）。
NO_DATA_SECTIONS = frozenset({
    "earnings_price_history", "statements", "revenue_breakdown", "morningstar",
    "valuation_plate_stocks", "valuation_index_stocks",
    "holding_changes", "holder_detail", "institutional",
    "company_profile", "company_executives", "company_executive_background",
    "company_operational_efficiency",
})


class F10SurfaceTests(unittest.TestCase):
    """分发面：白名单 26 项、未知 section 本地拒绝、每 section 有专用方法。"""

    def test_section_whitelist_is_exactly_lock_table(self):
        self.assertEqual(OpenApiF10.SECTIONS, frozenset(LOCK_SECTIONS))
        self.assertEqual(len(OpenApiF10.SECTIONS), 26)

    def test_every_section_has_dedicated_method(self):
        for section in LOCK_SECTIONS:
            with self.subTest(section=section):
                self.assertTrue(callable(getattr(OpenApiF10, section, None)),
                                f"{section} 缺少专用方法")

    def test_unknown_section_is_local_rejection(self):
        client = RecordingClient()
        for section in ("", "company", "f10", "rehab", None, "ANALYST_CONSENSUS"):
            with self.subTest(section=section):
                with self.assertRaises(ValueError) as ctx:
                    OpenApiF10(client).f10("HK.00700", section)
                self.assertIn("section", str(ctx.exception))
                self.assertEqual(client.calls, [], "未知 section 不得触达网络")

    def test_f10_dispatch_reaches_dedicated_method(self):
        f10 = make(d={"rating_type": "5"})
        out = call(f10, "morningstar", "HK.00700")
        self.assertEqual(out, {"rating_type": "5"})
        self.assertEqual(f10.client.calls, [
            ("request", "GET", f"{QUOTE}/HK.00700/research/morningstar", None, None)])


class F10OutboundTests(unittest.TestCase):
    """表驱动：26 个 section 的 (记录名, 方法, 路径, query) 与锁定表逐字一致。"""

    def test_all_sections_outbound_exactly_matches_lock_table(self):
        for section, symbol, kwargs, name, method, path, query in CALLS:
            with self.subTest(section=section):
                f10 = make(d={"ok": 1}, pagination={"next_key": "-1"})
                call(f10, section, symbol, **kwargs)
                self.assertEqual(f10.client.calls, [(name, method, path, query, None)])

    def test_sections_with_cursor_merge_envelope_pagination(self):
        for section, symbol, kwargs, name, _method, _path, _query in CALLS:
            if name != "request_meta":
                continue
            with self.subTest(section=section):
                f10 = make(d={"items": []}, pagination={"next_key": "3", "total": 9})
                out = call(f10, section, symbol, **kwargs)
                self.assertEqual(out["pagination"], {"next_key": "3", "total": 9})

    def test_sections_without_cursor_do_not_invent_pagination(self):
        f10 = make(d={"records": []}, pagination={"next_key": "3"})
        out = call(f10, "earnings_price_move", "HK.00700")
        self.assertNotIn("pagination", out)

    def test_symbol_is_uppercased_like_other_groups(self):
        f10 = make(d={"items": []})
        call(f10, "analyst_consensus", "hk.00700")
        self.assertEqual(f10.client.calls[0][2], f"{QUOTE}/HK.00700/research/analyst-consensus")

    def test_optional_params_omitted_when_not_given(self):
        f10 = make(d={"item_list": []})
        call(f10, "statements", "HK.00700")
        self.assertEqual(f10.client.calls, [
            ("request_meta", "GET", f"{QUOTE}/HK.00700/financials/statements", {}, None)])


class F10ValidationTests(unittest.TestCase):
    """锁定表给出区间/枚举/必填的字段：本地拒绝且零网络往返；未给出的不发明。"""

    def test_bad_params_are_rejected_locally(self):
        cases = (
            ("earnings_price_move", {"count": 51}),
            ("earnings_price_move", {"count": 0}),
            ("earnings_price_move", {"overview_count": 51}),
            ("statements", {"statement_type": 5}),
            ("statements", {"statement_type": 0}),
            ("rating_summary", {"rating_dimension_type": 3}),
            ("rating_summary", {"limit": 21}),
            ("valuation_detail", {"valuation_type": 4}),
            ("valuation_detail", {"interval_type": 11}),
            ("valuation_plate_stocks", {"limit": 51}),
            ("buybacks", {"limit": 51}),
            ("insider_holders", {"limit": 31}),
            ("insider_trades", {"limit": 51}),
            ("company_operational_efficiency", {"limit": 101}),
            ("company_operational_efficiency", {"financial_type": 8}),
            ("top_brokers_history", {"days_before": 366}),
            ("top_brokers_history", {"days_before": 0}),
            ("revenue_breakdown", {"date": "2026-13-01"}),
            ("top_brokers", {"date": "16/09/2026"}),
        )
        for section, kwargs in cases:
            with self.subTest(section=section, kwargs=kwargs):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiF10(client).f10("HK.00700", section, **kwargs)
                self.assertEqual(client.calls, [])

    def test_required_params_are_enforced_by_signature(self):
        """必填参数由签名强制（TypeError），不是 ValueError——零网络往返。"""
        cases = (
            ("company_executive_background", {}),
            ("top_brokers_history", {}),
        )
        for section, kwargs in cases:
            with self.subTest(section=section):
                client = RecordingClient()
                with self.assertRaises(TypeError):
                    OpenApiF10(client).f10("HK.00700", section, **kwargs)
                self.assertEqual(client.calls, [])

    def test_symbol_is_required_positionally(self):
        """专用方法缺 symbol → TypeError（签名即白名单，零网络往返）。"""
        client = RecordingClient()
        f10 = OpenApiF10(client)
        for section in ("valuation_plate_stocks", "valuation_index_stocks",
                        "valuation_index_stock_plates", "earnings_price_history"):
            with self.subTest(section=section):
                with self.assertRaises(TypeError):
                    getattr(f10, section)()
                self.assertEqual(client.calls, [])

    def test_fields_without_documented_bounds_pass_through_as_scalars(self):
        """锁定表未给区间的字段（如 institutional.limit）不发明上限。"""
        f10 = make(d={"items": []})
        call(f10, "institutional", "HK.00700", limit=9999)
        self.assertEqual(f10.client.calls, [
            ("request_meta", "GET", f"{QUOTE}/HK.00700/shareholders/institutional",
             {"limit": 9999}, None)])

    def test_symbol_market_guards_are_local(self):
        """本地可判定的 -8：财报日股价仅 HK/US/SH/SZ；十大经纪商仅港股。"""
        cases = (
            ("earnings_price_history", "SG.D05", {}),
            ("earnings_price_history", "JP.7203", {}),
            ("top_brokers", "US.AAPL", {}),
            ("top_brokers_history", "SH.600519", {"days_before": 1}),
        )
        for section, symbol, kwargs in cases:
            with self.subTest(section=section, symbol=symbol):
                client = RecordingClient()
                with self.assertRaises(ValueError):
                    OpenApiF10(client).f10(symbol, section, **kwargs)
                self.assertEqual(client.calls, [], "-8 本地可判定 → 零网络")

    def test_unknown_keyword_is_rejected_by_signature(self):
        client = RecordingClient()
        with self.assertRaises(TypeError):
            OpenApiF10(client).f10("HK.00700", "analyst_consensus", market="HK")
        self.assertEqual(client.calls, [])


class F10SemanticsTests(unittest.TestCase):
    """响应透传保真 + 错误码分派。"""

    def test_response_passes_through_untouched(self):
        payload = {
            "records": [{"fiscal_year": 2026, "period_text": "2026/Q2",
                         "pub_trading_day": "2026-08-15",
                         "announced_at": "2026-08-14T18:00:00", "period_end": "2026-06-30"}],
            "overview_avg_earnings_day_change_pct": 1.23,
            "unknown_future_field": {"nested": [1, 2, 3]},
        }
        f10 = make(d=payload)
        out = call(f10, "earnings_price_move", "HK.00700")
        self.assertEqual(out, payload, "传输层不得解释/归一/丢弃任何字段")
        self.assertIs(out["records"][0], payload["records"][0],
                      "不得重建对象（原样搬运）")

    def test_no_data_sections_become_empty(self):
        for section in sorted(NO_DATA_SECTIONS):
            symbol = "HK.00700"
            kwargs = {"leader_name": "张三"} if section == "company_executive_background" \
                else {}
            with self.subTest(section=section):
                f10 = make(error=OpenApiError("no_data", errcode=-10))
                self.assertEqual(call(f10, section, symbol, **kwargs), {"no_data": True})

    def test_no_data_not_documented_sections_raise(self):
        """锁定表未列 -10 的 section：-10 如实抛出，不吞成空数据。"""
        for section in ("analyst_consensus", "dividends", "splits", "top_brokers",
                        "valuation_index_stock_plates"):
            with self.subTest(section=section):
                f10 = make(error=OpenApiError("no_data", errcode=-10))
                with self.assertRaises(OpenApiError) as ctx:
                    call(f10, section, "HK.00700")
                self.assertEqual(ctx.exception.errcode, -10)

    def test_permission_and_param_errors_pass_through(self):
        for errcode in (-9, -3, -7, -8, -4, -6):
            with self.subTest(errcode=errcode):
                f10 = make(error=OpenApiError("official message", errcode=errcode))
                with self.assertRaises(OpenApiError) as ctx:
                    call(f10, "company_profile", "HK.00700")
                self.assertEqual(ctx.exception.errcode, errcode)
                self.assertEqual(str(ctx.exception), "official message", "不改写官方原文")
                self.assertEqual(len(f10.client.calls), 1, "不重试")


class F10LockTableBindingTests(unittest.TestCase):
    """防猜路径：26 个 (方法, 路径) 必须在锁定表 §C.5 有同行。"""

    def test_binding_surface_is_the_26_sections(self):
        self.assertEqual(len(WP12_F10_ENDPOINTS), 26)
        self.assertEqual({key[0] for key in WP12_F10_ENDPOINTS}, {"OpenApiF10"})
        self.assertEqual({key[1] for key in WP12_F10_ENDPOINTS}, set(LOCK_SECTIONS))

    def test_every_f10_endpoint_exists_in_lock_table(self):
        pairs = lock_pairs()
        for (cls_name, method_name), (http_method, path) in WP12_F10_ENDPOINTS.items():
            with self.subTest(endpoint=f"{cls_name}.{method_name}", path=path):
                self.assertIn((http_method, path), pairs,
                              f"{http_method} {path} 不在锁定表——禁止未核对路径落地")

    def test_task2_surface_does_not_contain_f10(self):
        """任务边界：F10 只属任务 3 的绑定面（互不混入）。"""
        from trading_datasource.futu_openapi import WP12_TRANSPORT_ENDPOINTS
        task2_paths = {path for _, path in WP12_TRANSPORT_ENDPOINTS.values()}
        f10_paths = {path for _, path in WP12_F10_ENDPOINTS.values()}
        self.assertEqual(task2_paths & f10_paths, set())


if __name__ == "__main__":
    unittest.main()
