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
        with patch.object(self.module, "call_tool", return_value={"report_list": reports}):
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
        """富途 91 个工具里没有资产负债表接口——不许估算，必须明说。"""
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
