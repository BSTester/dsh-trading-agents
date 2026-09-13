"""券商持仓读取的离线回归测试。

重点不是「能读到数字」，而是**读不到时不许编**、**不许跨账户/币种合并**：
面板展示的是券商真实持仓，一旦悄悄拼凑或求和，用户会拿它当账户事实做决策。
全部离线：不访问网络，mock 掉共享 MCP 客户端。
"""
import importlib.util
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "workbench" / "python"))

from trading_datasource.futu_mcp import FutuUnavailable  # noqa: E402


def load_positions():
    spec = importlib.util.spec_from_file_location(
        "workbench_positions", ROOT / "plugins" / "workbench" / "python" / "positions.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SIM_ACCOUNTS = {"accounts": [
    {"account_id": "11587526", "account_title": "美股融资融券模拟账户", "market_id": 100},
    {"account_id": "9393", "account_title": "港股模拟账户", "market_id": 1},
]}
SIM_POSITIONS = {
    "11587526": {"positions": [{"symbol": "NVDA", "stock_name": "英伟达", "qty": "800",
                                "qty_avbl": "800", "cost_price": "178.585", "cur_price": "218.29",
                                "mv": "174632", "profit": "31764", "profit_ratio": "22.23"}]},
    "9393": {"positions": [{"symbol": "09988", "stock_name": "阿里巴巴-W", "qty": "300",
                            "qty_avbl": "300", "cost_price": "165.4", "cur_price": "107.5",
                            "mv": "32250", "profit": "-11370", "profit_ratio": "-35.01"}]},
}
REAL_ACCOUNTS = {"accounts": [
    {"account_id": 281756480774050900, "acc_type": "margin", "enable_market": [1, 2]},
]}
REAL_POSITIONS = [
    {"code": "US.TSLA", "stock_name": "Tesla", "qty": "12", "can_sell_qty": "12",
     "currency": "USD", "cost_price": "291.234", "nominal_price": "365.44",
     "market_val": "4385.28", "pl_val": "890.47", "pl_ratio": "25.48"},
    {"code": "SEHK.09988", "stock_name": "BABA-W", "qty": "100", "can_sell_qty": "100",
     "currency": "HKD", "cost_price": "121.874", "nominal_price": "107.5",
     "market_val": "10750.00", "pl_val": "-1437.37", "pl_ratio": "-11.79"},
]


class SimPositionsTests(unittest.TestCase):
    def setUp(self):
        self.module = load_positions()

    def _fake_call(self, fail_on=None):
        calls = []

        def call(name, arguments, **kwargs):
            calls.append((name, arguments))
            if name == "sim_trade_account_list":
                return SIM_ACCOUNTS
            if fail_on and str(arguments.get("acc_id")) == fail_on:
                raise FutuUnavailable("sim_trade_position_list: ret=-5 backend business error")
            return SIM_POSITIONS[str(arguments["acc_id"])]

        return call, calls

    def test_reads_positions_per_account(self):
        call, calls = self._fake_call()
        with patch.object(self.module, "call_tool", side_effect=call):
            payload = self.module.collect("sim")
        self.assertEqual(payload["counts"]["positions"], 2)
        self.assertEqual(payload["counts"]["accounts_checked"], 2)
        self.assertEqual(payload["counts"]["accounts_with_positions"], 2)
        self.assertEqual(payload["errors"], [])

    def test_market_parameter_is_sent(self):
        """缺 market 会报 ret=-5，因此必须带上。"""
        call, calls = self._fake_call()
        with patch.object(self.module, "call_tool", side_effect=call):
            self.module.collect("sim")
        position_calls = [args for name, args in calls if name == "sim_trade_position_list"]
        self.assertEqual(len(position_calls), 2)
        for args in position_calls:
            self.assertIn("market", args, "模拟盘持仓必须带 market 参数")

    def test_no_currency_is_invented(self):
        """模拟盘响应没有币种字段 —— 不许推断。"""
        call, _ = self._fake_call()
        with patch.object(self.module, "call_tool", side_effect=call):
            payload = self.module.collect("sim")
        for group in payload["groups"]:
            for row in group["positions"]:
                self.assertIsNone(row["currency"])

    def test_subtotals_are_per_account_never_summed(self):
        """不同账户可能不同币种，只做按账户小计，绝不出现跨账户合计。"""
        call, _ = self._fake_call()
        with patch.object(self.module, "call_tool", side_effect=call):
            payload = self.module.collect("sim")
        self.assertNotIn("total", payload)
        self.assertNotIn("market_value", payload)
        by_account = {g["account"]: g["market_value"] for g in payload["groups"]}
        self.assertEqual(by_account["美股融资融券模拟账户"], 174632.0)
        self.assertEqual(by_account["港股模拟账户"], 32250.0)

    def test_one_account_failure_does_not_hide_the_others(self):
        call, _ = self._fake_call(fail_on="9393")
        with patch.object(self.module, "call_tool", side_effect=call):
            payload = self.module.collect("sim")
        self.assertEqual(payload["counts"]["positions"], 1, "失败账户不影响其他账户")
        self.assertEqual(len(payload["errors"]), 1)
        self.assertIn("港股模拟账户", payload["errors"][0]["account"])
        self.assertTrue(payload["errors"][0]["reason"])

    def test_account_without_positions_is_omitted_but_counted(self):
        def call(name, arguments, **kwargs):
            if name == "sim_trade_account_list":
                return {"accounts": SIM_ACCOUNTS["accounts"] + [
                    {"account_id": "9999", "account_title": "日本期货模拟账户", "market_id": 13}]}
            if str(arguments["acc_id"]) == "9999":
                return {"positions": []}
            return SIM_POSITIONS[str(arguments["acc_id"])]

        with patch.object(self.module, "call_tool", side_effect=call):
            payload = self.module.collect("sim")
        self.assertEqual(payload["counts"]["accounts_checked"], 3)
        self.assertEqual(payload["counts"]["accounts_with_positions"], 2)


class LivePositionsTests(unittest.TestCase):
    def setUp(self):
        self.module = load_positions()

    def test_multi_currency_is_subtotalled_not_summed(self):
        """实盘账户可同时持有港币与美元标的，直接相加是错的。"""
        def call(name, arguments, **kwargs):
            return REAL_ACCOUNTS if name == "account_authorized_trd_accs" else REAL_POSITIONS

        with patch.object(self.module, "call_tool", side_effect=call):
            payload = self.module.collect("live")
        group = payload["groups"][0]
        self.assertIsNone(group["market_value"], "跨币种不产生单一市值")
        self.assertIsNone(group["pl_val"])
        subtotals = {row["currency"]: row for row in group["subtotals"]}
        self.assertEqual(subtotals["USD"]["market_value"], 4385.28)
        self.assertEqual(subtotals["USD"]["pl_val"], 890.47)
        self.assertEqual(subtotals["HKD"]["market_value"], 10750.0)
        self.assertEqual(subtotals["HKD"]["pl_val"], -1437.37)

    def test_real_account_name_does_not_leak_full_id(self):
        def call(name, arguments, **kwargs):
            return REAL_ACCOUNTS if name == "account_authorized_trd_accs" else REAL_POSITIONS

        with patch.object(self.module, "call_tool", side_effect=call):
            payload = self.module.collect("live")
        acc_id = str(REAL_ACCOUNTS["accounts"][0]["account_id"])
        self.assertNotIn(acc_id, payload["groups"][0]["account"])
        self.assertTrue(payload["groups"][0]["account"].endswith(acc_id[-4:]))


class EquityMarkTests(unittest.TestCase):
    """每日盯市：只从今天开始累积，不回溯伪造历史。"""

    def setUp(self):
        self.module = load_positions()
        self.scratch = Path(__file__).resolve().parents[1] / ".install-test-equity"
        self.scratch.mkdir(exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.scratch, ignore_errors=True))
        self.patch = patch.object(self.module, "equity_path",
                                  side_effect=lambda mode: self.scratch / f"equity-{mode}.json")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_first_mark_is_recorded(self):
        marks = self.module.append_mark("sim", [{"account": "A", "total_asset": 100.0}], 3)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["positions"], 3)
        self.assertEqual(marks[0]["accounts"][0]["total_asset"], 100.0)

    def test_same_day_overwrites_instead_of_duplicating(self):
        self.module.append_mark("sim", [{"account": "A", "total_asset": 100.0}], 3)
        marks = self.module.append_mark("sim", [{"account": "A", "total_asset": 250.0}], 5)
        self.assertEqual(len(marks), 1, "同一天不应出现两个点")
        self.assertEqual(marks[0]["accounts"][0]["total_asset"], 250.0)
        self.assertEqual(marks[0]["positions"], 5)

    def test_history_is_capped_and_sorted(self):
        for day in range(1, 8):
            marks = self.module.append_mark("sim", [{"account": "A"}], day)
        self.assertEqual([row["date"] for row in marks], sorted(row["date"] for row in marks))

    def test_marks_survive_across_reads(self):
        self.module.append_mark("sim", [{"account": "A", "total_asset": 1.0}], 1)
        self.assertEqual(len(self.module.read_marks("sim")), 1)
        self.assertEqual(self.module.read_marks("live"), [], "两种模式分开存储")

    def test_corrupt_marks_file_is_ignored(self):
        (self.scratch / "equity-sim.json").write_text("{not json")
        self.assertEqual(self.module.read_marks("sim"), [])

    def test_collect_records_a_mark_and_returns_it(self):
        with patch.object(self.module, "call_tool", side_effect=self._fake_call()), \
             patch.object(self.module, "append_mark", wraps=self.module.append_mark):
            payload = self.module.collect("sim")
        self.assertIn("equity_marks", payload)
        self.assertEqual(len(payload["equity_marks"]), 1)
        self.assertIn("不回溯", payload["note"])

    def _fake_call(self):
        def call(name, arguments, **kwargs):
            if name == "sim_trade_account_list":
                return SIM_ACCOUNTS
            if name == "sim_trade_cash_info":
                return {"balance": "100", "total_asset": "250"}
            return SIM_POSITIONS[str(arguments["acc_id"])]
        return call


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.module = load_positions()
        self.scratch = Path(__file__).resolve().parents[1] / ".install-test-positions"
        self.scratch.mkdir(exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(self.scratch, ignore_errors=True))

    def _patch_paths(self):
        return patch.object(self.module, "cache_path",
                            side_effect=lambda mode: self.scratch / f"positions-{mode}.json")

    def test_cache_round_trip_and_stale_fallback(self):
        """实时读取失败时必须回退到上次缓存，并明确标记 stale。"""
        payload = {"mode": "sim", "as_of": datetime.now().isoformat(timespec="seconds"),
                   "groups": [], "counts": {}, "errors": [], "stale": False}
        with self._patch_paths():
            self.module.write_cache("sim", payload)
            self.assertEqual(self.module.read_cache("sim")["mode"], "sim")
            # 用真实 main() 走一遍缓存命中
            with patch.object(sys, "argv", ["positions.py", "--mode", "sim"]), \
                 patch.object(self.module, "collect",
                              side_effect=AssertionError("缓存命中不应重新取数")), \
                 patch("builtins.print") as out:
                self.assertEqual(self.module.main(), 0)
            body = json.loads(out.call_args[0][0])
            self.assertTrue(body["cached"])
            self.assertFalse(body["stale"])

    def test_stale_fallback_when_fetch_fails(self):
        payload = {"mode": "sim", "as_of": (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"),
                   "groups": [{"account": "旧数据", "positions": []}], "counts": {}, "errors": [], "stale": False}
        with self._patch_paths():
            self.module.write_cache("sim", payload)
            with patch.object(sys, "argv", ["positions.py", "--mode", "sim"]), \
                 patch.object(self.module, "collect", side_effect=RuntimeError("network down")), \
                 patch("builtins.print") as out:
                self.assertEqual(self.module.main(), 0, "有缓存时应降级而不是报错退出")
            body = json.loads(out.call_args[0][0])
            self.assertTrue(body["stale"])
            self.assertTrue(body["cached"])
            self.assertIn("network down", body["error"])

    def test_no_cache_and_failure_reports_error(self):
        with self._patch_paths():
            with patch.object(sys, "argv", ["positions.py", "--mode", "live"]), \
                 patch.object(self.module, "collect", side_effect=RuntimeError("boom")), \
                 patch("builtins.print") as out:
                self.assertEqual(self.module.main(), 1)
            body = json.loads(out.call_args[0][0])
            self.assertIn("boom", body["error"])

    def test_refresh_bypasses_cache(self):
        payload = {"mode": "sim", "as_of": datetime.now().isoformat(timespec="seconds"),
                   "groups": [], "counts": {}, "errors": [], "stale": False}
        fresh = {**payload, "counts": {"positions": 99}}
        with self._patch_paths():
            self.module.write_cache("sim", payload)
            with patch.object(sys, "argv", ["positions.py", "--mode", "sim", "--refresh"]), \
                 patch.object(self.module, "collect", return_value=fresh) as collect, \
                 patch("builtins.print") as out:
                self.assertEqual(self.module.main(), 0)
            collect.assert_called_once()
            body = json.loads(out.call_args[0][0])
            self.assertFalse(body["cached"])
            self.assertEqual(body["counts"]["positions"], 99)

    def test_corrupt_cache_is_ignored(self):
        with self._patch_paths():
            (self.scratch / "positions-sim.json").write_text("{not json")
            self.assertIsNone(self.module.read_cache("sim"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
