"""WP5 数据依赖核验聚合脚本：注入假 fetcher 逐项断言 OK/FAIL 与失败归因字段。

核验项 = 规格 §13.2 六项 + WP2（中证800/估值字段路径）+ WP3（工具锁定常量/券商通道）。
测试全部离线；真实调用由 scripts/verify-data-deps.py 手动执行
（约定：富途侧发版异常、internal error 面扩大时先跑该脚本）。
"""
import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "plugins" / "core" / "python"))
sys.path.insert(0, str(_REPO / "plugins" / "datasource" / "python"))
_spec = importlib.util.spec_from_file_location(
    "verify_data_deps_under_test", _REPO / "scripts" / "verify-data-deps.py")
vd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vd)


def _ok_fetcher(name, args, timeout=30, client_name=None):
    if name == "quote_history_kline":
        return {"kline_list": [{"date": "20260912", "open": 1.0, "high": 1.0,
                                "low": 1.0, "close": 1.0, "volume": 1}]}
    if name == "quote_trading_days":
        return {"trading_days": [{"time": "2026-09-14", "trade_date_type": "WHOLE",
                                  "trade_second": 14400}]}
    if name == "quote_valuation_index_component_stock_list":
        return {"stock_list": [{"symbol": "SH.600519"}]}
    if name == "quote_owner_plate":
        return {"plate_list": [{"name": "白酒", "plate_type": 2}]}
    if name == "quote_stock_basicinfo":
        return {"basic_list": [{"name": "贵州茅台", "lot_size": 100}]}
    if name == "quote_financials_statements":
        return {"report_list": [{"date_time": 1756684800000, "item_list": []}]}
    if name == "quote_valuation_detail":
        return {"trend": {"current_value": 25.5, "valuation_percentile": 40.0}}
    if name == "sim_trade_account_list":
        return {"acc_list": [{"acc_id": "A1", "market": 1}]}
    raise AssertionError(f"假 fetcher 未覆盖工具 {name}")


class VerifyDataDepsTest(unittest.TestCase):
    def test_all_ok_with_healthy_channel(self):
        report = vd.build_report(fetcher=_ok_fetcher, now="2026-09-15 10:00:00")
        self.assertEqual(report["summary"]["fail"], 0)
        self.assertEqual(report["summary"]["ok"], report["summary"]["total"])
        # §13.2 六项 + WP2 两项 + WP3 两项
        self.assertGreaterEqual(report["summary"]["total"], 10)

    def test_every_result_carries_impact_and_gap_fields(self):
        report = vd.build_report(fetcher=_ok_fetcher, now="2026-09-15 10:00:00")
        for row in report["results"]:
            for key in ("item", "ok", "detail", "workpackages", "gap"):
                self.assertIn(key, row, f"{row.get('item')} 缺字段 {key}")

    def test_failure_attaches_workpackages_and_gap(self):
        def broken(name, args, timeout=30, client_name=None):
            if name == "quote_financials_statements":
                raise RuntimeError("internal error")
            return _ok_fetcher(name, args, timeout=timeout, client_name=client_name)

        report = vd.build_report(fetcher=broken, now="2026-09-15 10:00:00")
        bad = [r for r in report["results"] if not r["ok"]]
        self.assertEqual(len(bad), 1)
        self.assertIn("WP1", bad[0]["workpackages"])
        self.assertIn("①", bad[0]["gap"])
        self.assertIn("internal error", bad[0]["detail"])
        self.assertEqual(report["summary"]["fail"], 1)

    def test_offline_constant_check_passes_even_when_channel_dead(self):
        def dead(name, args, timeout=30, client_name=None):
            raise RuntimeError("internal error")

        report = vd.build_report(fetcher=dead, now="2026-09-15 10:00:00")
        constants = [r for r in report["results"] if "常量" in r["item"]]
        self.assertEqual([r["ok"] for r in constants], [True])
        self.assertEqual(report["summary"]["fail"], report["summary"]["total"] - 1)

    def test_kline_calls_use_integer_ktype(self):
        seen = []

        def spy(name, args, timeout=30, client_name=None):
            if name == "quote_history_kline":
                seen.append(args["ktype"])  # §13.2：字符串会报 ret=-3，必须整数
            return _ok_fetcher(name, args, timeout=timeout, client_name=client_name)

        vd.build_report(fetcher=spy, now="2026-09-15 10:00:00")
        self.assertTrue(seen)
        self.assertEqual(set(seen), {2})

    def test_main_prints_json_and_exit_code(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = vd.main(fetcher=_ok_fetcher, now="2026-09-15 10:00:00")
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["summary"]["fail"], 0)
        self.assertEqual(payload["as_of"], "2026-09-15 10:00:00")
        row = payload["results"][0]
        self.assertTrue(row["workpackages"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
