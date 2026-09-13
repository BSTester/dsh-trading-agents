"""质量因子的离线回归测试。

核心是**不许估算**：可算的从财报原文算，算不出的（ROE/ROA，因为富途没有资产负债表
接口）必须明确列为不可得，而不是用 0、行业均值或别的科目顶替。
全部离线：mock 掉共享 MCP 客户端。
"""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "workbench" / "python"))

from trading_datasource.futu_mcp import FutuUnavailable  # noqa: E402


def load_quality():
    spec = importlib.util.spec_from_file_location(
        "workbench_quality", ROOT / "plugins" / "workbench" / "python" / "quality.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def item(name, data, yoy=None):
    return {"display_name": name, "data": data, "yoy": yoy, "value_type": "amount"}


def report(fiscal_year, financial_type, date_time, items):
    return {"fiscal_year": fiscal_year, "financial_type": financial_type,
            "date_time": date_time, "currency_code": "USD",
            "accounting_standards": "US_GAAP", "item_list": items}


INCOME = [
    item("Total Revenue as Reported", 1000.0, yoy=10.0),
    item("Cost of Revenue", 400.0),
    item("Gross Profit", 600.0),
    item("Operating Profit", 300.0),
    item("Research & Development", 100.0),
    item("Pretax Profit", 280.0),
    item("Tax", 56.0),
    item("Net Profit", 224.0, yoy=20.0),
    item("Diluted EPS", 2.5),
    item("Dividend Per Share", 0.25),
]


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.module = load_quality()

    def _collect(self, reports):
        with patch.object(self.module, "call_tool", return_value={"report_list": reports}), \
             patch.object(self.module, "load_returns",
                          return_value={"available": False, "roe": None, "roa": None,
                                        "reason": "测试默认：备用源未取到"}):
            return self.module.collect("US.AAPL")

    def test_ratios_are_computed_from_reported_figures(self):
        payload = self._collect([report(2026, 3, 1782489600000, INCOME)])
        latest = payload["latest"]
        self.assertEqual(latest["gross_margin"], 60.0)        # 600 / 1000
        self.assertEqual(latest["operating_margin"], 30.0)    # 300 / 1000
        self.assertEqual(latest["net_margin"], 22.4)          # 224 / 1000
        self.assertEqual(latest["rd_ratio"], 10.0)            # 100 / 1000
        self.assertEqual(latest["effective_tax_rate"], 20.0)  # 56 / 280
        self.assertEqual(latest["diluted_eps"], 2.5)

    def test_year_over_year_comes_from_the_server_not_recomputed(self):
        """同自由服务端给出，避免自己跨期推算引入口径错误。"""
        latest = self._collect([report(2026, 3, 1782489600000, INCOME)])["latest"]
        self.assertEqual(latest["revenue_yoy"], 10.0)
        self.assertEqual(latest["net_profit_yoy"], 20.0)

    def test_missing_line_item_yields_none_not_zero(self):
        """缺科目就是缺指标——用 0 会假装「没有毛利」。"""
        partial = [item("Total Revenue as Reported", 1000.0), item("Net Profit", 100.0)]
        latest = self._collect([report(2026, 3, 1782489600000, partial)])["latest"]
        self.assertIsNone(latest["gross_margin"])
        self.assertIsNone(latest["rd_ratio"])
        self.assertEqual(latest["net_margin"], 10.0)
        self.assertIsNone(latest["gross_profit"])

    def test_zero_or_negative_revenue_does_not_divide(self):
        rows = [item("Total Revenue as Reported", 0.0), item("Gross Profit", 10.0)]
        latest = self._collect([report(2026, 3, 1782489600000, rows)])["latest"]
        self.assertIsNone(latest["gross_margin"])

    def test_roe_and_roa_are_declared_unavailable_with_reason(self):
        """富途没有资产负债表接口；备用源也拿不到时必须明说，不许估算。"""
        with patch.object(self.module, "load_returns",
                          return_value={"available": False, "roe": None, "roa": None,
                                        "reason": "备用源未取到"}):
            payload = self._collect([report(2026, 3, 1782489600000, INCOME)])
        keys = {row["key"] for row in payload["unavailable"]}
        self.assertEqual(keys, {"roe", "roa"})
        for row in payload["unavailable"]:
            self.assertIn("资产负债表", row["reason"])
        note = payload["note"]
        self.assertIn("不会", note.replace("未做任何估算", "不会估算") + "估算")

    def test_periods_sorted_newest_first(self):
        payload = self._collect([
            report(2024, 7, 1700000000000, INCOME),
            report(2026, 3, 1782489600000, INCOME),
            report(2025, 4, 1740000000000, INCOME),
        ])
        self.assertEqual([row["fiscal_year"] for row in payload["periods"]], [2026, 2025, 2024])

    def test_financial_type_is_preserved_raw(self):
        """服务端未给出该枚举含义，因此原样保留，不自造「年报/季报」标签。"""
        latest = self._collect([report(2026, 3, 1782489600000, INCOME)])["latest"]
        self.assertEqual(latest["financial_type"], 3)
        self.assertNotIn("period_label", latest)

    def test_empty_report_list_is_an_error(self):
        with patch.object(self.module, "call_tool", return_value={"report_list": []}):
            with self.assertRaises(RuntimeError):
                self.module.collect("US.AAPL")

    def test_unavailable_tool_surfaces_as_execution_error(self):
        with patch.object(self.module, "call_tool",
                          side_effect=FutuUnavailable("quote_financials_statements: 无权限")):
            with self.assertRaises(FutuUnavailable):
                self.module.collect("US.AAPL")

    def test_cli_reports_error_as_json_with_nonzero_exit(self):
        with patch.object(sys, "argv", ["quality.py", "--ticker", "US.AAPL"]), \
             patch.object(self.module, "collect", side_effect=RuntimeError("boom")), \
             patch("builtins.print") as out:
            self.assertEqual(self.module.main(), 1)
        self.assertIn("boom", out.call_args[0][0])



# 实测的三种市场科目名（2026-09-13 从富途返回原文抄录）
US_ITEMS = ["Total Revenue as Reported", "Total Operating Revenue", "Cost of Revenue",
            "Gross Profit", "Research & Development", "Operating Profit", "Pretax Profit",
            "Tax", "Net Profit", "Diluted EPS", "Dividend Per Share"]
HK_ITEMS = ["Total Revenue", "Operating Revenue", "Cost of Revenue", "Cost of Goods Sold",
            "Gross Profit", "Operating Expense", "Operating Profit", "Pretax Profit",
            "Tax", "Net Profit", "Diluted EPS"]
A_ITEMS = ["Total Operating Revenue", "Operating Revenue", "Cost of Sales",
           "Research and Development", "Operating Profit", "Gross Profit",
           "Less:Income tax", "Net Profit", "Net Profit of Parent Company Owners"]


def named_report(items, values, fiscal_year=2026, financial_type=3, date_time=1782489600000):
    return report(fiscal_year, financial_type, date_time,
                  [item(name, values.get(name)) for name in items])


class MarketAliasTests(unittest.TestCase):
    """富途的科目名按市场不同——只认一套名字会让另两个市场的指标全空。"""

    def setUp(self):
        self.module = load_quality()

    def _latest(self, items, values):
        with patch.object(self.module, "call_tool",
                          return_value={"report_list": [named_report(items, values)]}):
            return self.module.collect("X")["latest"]

    def test_us_names(self):
        values = {"Total Revenue as Reported": 1000.0, "Cost of Revenue": 400.0,
                  "Gross Profit": 600.0, "Research & Development": 100.0,
                  "Operating Profit": 300.0, "Pretax Profit": 280.0, "Tax": 56.0,
                  "Net Profit": 224.0, "Diluted EPS": 2.5}
        row = self._latest(US_ITEMS, values)
        self.assertEqual(row["gross_margin"], 60.0)
        self.assertEqual(row["rd_ratio"], 10.0)
        self.assertEqual(row["effective_tax_rate"], 20.0)
        self.assertEqual(row["diluted_eps"], 2.5)

    def test_hk_names(self):
        """港股用 Total Revenue / Cost of Goods Sold，且没有研发科目。"""
        values = {"Total Revenue": 1000.0, "Cost of Goods Sold": 200.0, "Gross Profit": 800.0,
                  "Operating Profit": 350.0, "Pretax Profit": 300.0, "Tax": 45.0,
                  "Net Profit": 255.0, "Diluted EPS": 3.0}
        row = self._latest(HK_ITEMS, values)
        self.assertEqual(row["gross_margin"], 80.0)
        self.assertEqual(row["operating_margin"], 35.0)
        self.assertEqual(row["net_margin"], 25.5)
        self.assertIsNone(row["rd_ratio"], "港股没有研发科目，必须为空而不是 0")
        self.assertEqual(row["effective_tax_rate"], 15.0)

    def test_a_share_names(self):
        """A股用 Cost of Sales / Less:Income tax / Research and Development，且没有 EPS。"""
        values = {"Total Operating Revenue": 1000.0, "Cost of Sales": 300.0,
                  "Gross Profit": 700.0, "Research and Development": 20.0,
                  "Operating Profit": 400.0, "Less:Income tax": 100.0, "Net Profit": 300.0}
        row = self._latest(A_ITEMS, values)
        self.assertEqual(row["gross_margin"], 70.0)
        self.assertEqual(row["rd_ratio"], 2.0)
        self.assertEqual(row["net_margin"], 30.0)
        self.assertIsNone(row["diluted_eps"], "A股无该科目，必须为空")
        # A股没有 Pretax Profit 科目：不自行用 净利+税 反推，如实为空
        self.assertIsNone(row["effective_tax_rate"])

    def test_alias_order_prefers_first_candidate(self):
        values = {"Total Revenue as Reported": 100.0, "Total Revenue": 999.0,
                  "Gross Profit": 50.0}
        row = self._latest(["Total Revenue as Reported", "Total Revenue", "Gross Profit"], values)
        self.assertEqual(row["revenue"], 100.0)


class ReturnsFallbackTests(unittest.TestCase):
    """ROE/ROA：富途不可得，按渠道优先级落到备用源；都拿不到就明说不可得。"""

    def setUp(self):
        self.module = load_quality()

    def _collect(self, returns):
        with patch.object(self.module, "call_tool",
                          return_value={"report_list": [report(2026, 3, 1782489600000, INCOME)]}), \
             patch.object(self.module, "load_returns", return_value=returns):
            return self.module.collect("X")

    def test_returns_included_when_fallback_succeeds(self):
        payload = self._collect({"available": True, "roe": 19.48, "roa": 11.03,
                                 "source": "yahoo/yfinance", "balance_period": "2025-12-31",
                                 "income_period": "2025-12-31"})
        self.assertEqual(payload["returns"]["roe"], 19.48)
        self.assertEqual(payload["unavailable"], [], "备用源补齐后不应再列为不可得")
        self.assertIn("备用源", payload["note"])

    def test_unavailable_lists_both_reasons_when_all_sources_fail(self):
        payload = self._collect({"available": False, "roe": None, "roa": None,
                                 "reason": "备用源也未取到"})
        keys = {row["key"] for row in payload["unavailable"]}
        self.assertEqual(keys, {"roe", "roa"})
        for row in payload["unavailable"]:
            self.assertIn("资产负债表", row["reason"])
            self.assertIn("备用源也未取到", row["reason"])
        self.assertIn("未做任何估算", payload["note"])


class FutuSymbolTests(unittest.TestCase):
    """调用富途前必须归一化成 MARKET.CODE。

    实测：直接传 "TSLA" 会被富途参数校验拒绝
    （`ret=-3 parameter 'symbol' does not match pattern '^[A-Z0-9_]+\\.[...]$'`），
    而用户输入与 K 线页传过来的往往就是裸代码，表现为质量因子整卡报错。
    """

    CASES = [("TSLA", "US.TSLA"), ("US.TSLA", "US.TSLA"), ("aapl", "US.AAPL"),
             ("600519", "SH.600519"), ("600519.SH", "SH.600519"),
             ("700", "HK.00700"), ("HK.00700", "HK.00700"), ("00700.HK", "HK.00700")]

    def test_normalizes_before_calling_futu(self):
        module = load_quality()
        seen = []

        def call(name, arguments, **kwargs):
            seen.append((name, arguments["symbol"]))
            return {"report_list": [report(2026, 3, 1782489600000, INCOME)]}

        for raw, expected in self.CASES:
            seen.clear()
            with patch.object(module, "call_tool", side_effect=call), \
                 patch.object(module, "load_returns",
                              return_value={"available": False, "roe": None, "roa": None,
                                            "reason": "测试"}):
                payload = module.collect(raw)
            self.assertEqual(seen, [("quote_financials_statements", expected)], raw)
            self.assertEqual(payload["symbol"], expected, raw)
            self.assertEqual(payload["ticker"], raw, "对外仍保留用户输入的原写法")


class YahooSymbolTests(unittest.TestCase):
    """符号映射错了会静默取到别的公司的数据，比取不到更危险。"""

    def setUp(self):
        from trading_datasource import fundamentals
        self.fundamentals = fundamentals

    def test_hk_uses_four_digit_yahoo_code(self):
        for raw in ("HK.00700", "00700.HK", "700", "0700", "00700"):
            self.assertEqual(self.fundamentals.to_yahoo_symbol(raw), "0700.HK", raw)

    def test_hk_alibaba(self):
        self.assertEqual(self.fundamentals.to_yahoo_symbol("09988"), "9988.HK")

    def test_a_share_suffixes(self):
        self.assertEqual(self.fundamentals.to_yahoo_symbol("600519"), "600519.SS")
        self.assertEqual(self.fundamentals.to_yahoo_symbol("SH.600519"), "600519.SS")
        self.assertEqual(self.fundamentals.to_yahoo_symbol("000001"), "000001.SZ")
        self.assertEqual(self.fundamentals.to_yahoo_symbol("300750"), "300750.SZ")

    def test_us_passthrough(self):
        self.assertEqual(self.fundamentals.to_yahoo_symbol("AAPL"), "AAPL")
        self.assertEqual(self.fundamentals.to_yahoo_symbol("US.AAPL"), "AAPL")

    def test_empty_is_none(self):
        self.assertIsNone(self.fundamentals.to_yahoo_symbol(""))

    def test_returns_of_guards_bad_denominators(self):
        self.assertIsNone(self.fundamentals.returns_of(100.0, 0.0, 100.0)["roe"])
        self.assertIsNone(self.fundamentals.returns_of(100.0, None, None)["roe"])

    def test_load_returns_reports_failure_without_raising(self):
        with patch.object(self.fundamentals, "from_yahoo", return_value=None), \
             patch.object(self.fundamentals, "from_akshare", side_effect=RuntimeError("boom")):
            result = self.fundamentals.load_returns("X")
        self.assertFalse(result["available"])
        self.assertEqual(len(result["tried"]), 2)
        self.assertIn("未取到", result["reason"])

    def test_load_returns_prefers_the_first_working_source(self):
        yahoo = {"available": True, "roe": 1.0, "roa": 2.0, "source": "yahoo/yfinance"}
        with patch.object(self.fundamentals, "from_yahoo", return_value=yahoo), \
             patch.object(self.fundamentals, "from_akshare") as ak:
            result = self.fundamentals.load_returns("600519")
        self.assertEqual(result["source"], "yahoo/yfinance")
        ak.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
