"""「指标必须点名主语」的离线回归测试。

同一个「最大回撤」可能来自本地台账回放，也可能来自某一次回测预览，
两者覆盖的标的和策略未必相同。若不把标的与策略一起返回，界面只能显示
一串没有归属的数字——用户无法判断这个回撤是谁的回撤，也就无法据以决策。

本文件锁定这条链路：
  台账成交记录（含 strategy 字段）→ analytics.equity 返回 tickers/strategies → 界面可标注
全部离线：不访问网络，日线序列用桩替换。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "workbench" / "python"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ANALYTICS = load_module("wb_analytics_subject", ROOT / "plugins" / "workbench" / "python" / "analytics.py")

LEDGER_FIXTURE = {
    "sim": {
        "cash": 900000.0,
        "positions": {"000001": {"shares": 100, "stop": 9.0, "date": "2024-01-05"}},
        "history": [
            {"action": "BUY", "ticker": "600519", "shares": 100, "price": 1700.0,
             "stop": 1600.0, "strategy": "ma_cross(5,20)", "date": "2024-01-03", "fee": 5.0},
            {"action": "SELL", "ticker": "600519", "shares": 100, "price": 1750.0,
             "stop": None, "strategy": "ma_cross(5,20)", "date": "2024-01-04",
             "fee": 5.0, "return": 0.029},
            {"action": "BUY", "ticker": "000001", "shares": 100, "price": 10.0,
             "stop": 9.0, "strategy": "rsi(30,70)", "date": "2024-01-05", "fee": 5.0},
        ],
    }
}

CLOSES = {
    "600519": {"2024-01-03": 1700.0, "2024-01-04": 1750.0, "2024-01-05": 1760.0,
               "2024-01-08": 1740.0},
    "000001": {"2024-01-03": 9.8, "2024-01-04": 9.9, "2024-01-05": 10.0, "2024-01-08": 10.4},
}


class StrategySubjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "quant-ledger.json"
        self.ledger.write_text(json.dumps(LEDGER_FIXTURE), encoding="utf-8")
        patches = [
            patch.object(ANALYTICS, "LEDGER", self.ledger),
            patch.object(ANALYTICS, "daily_closes",
                         lambda ticker, window: dict(CLOSES.get(ticker, {}))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_equity_reports_which_tickers_and_strategies_it_covers(self):
        result = ANALYTICS.equity_curve(mode="sim", window=250)
        self.assertGreater(result["count"], 0)
        # 有标的、有策略，界面才可能写出「最大回撤（600519、000001 · ma_cross(5,20)、rsi(30,70)）」
        self.assertEqual(result["tickers"], ["000001", "600519"])
        self.assertEqual(result["strategies"], ["ma_cross(5,20)", "rsi(30,70)"])

    def test_open_position_without_history_still_counts_as_a_subject(self):
        """只有持仓、没有成交记录的标的也要算进覆盖范围，否则盯市来源被漏掉。"""
        fixture = json.loads(json.dumps(LEDGER_FIXTURE))
        fixture["sim"]["history"] = []
        fixture["sim"]["positions"] = {"601318": {"shares": 100, "stop": 40.0}}
        self.ledger.write_text(json.dumps(fixture), encoding="utf-8")
        with patch.object(ANALYTICS, "daily_closes",
                          lambda ticker, window: {"2024-01-08": 45.0} if ticker == "601318" else {}):
            result = ANALYTICS.equity_curve(mode="sim", window=250)
        self.assertEqual(result["tickers"], ["601318"])
        self.assertEqual(result["strategies"], [])

    def test_empty_ledger_returns_the_same_shape_not_a_missing_key(self):
        """空台账时字段也必须存在：界面用 .tickers 取值，缺键会渲染成 undefined。"""
        self.ledger.write_text(json.dumps({"sim": {"cash": 1e6, "positions": {}, "history": []}}),
                               encoding="utf-8")
        result = ANALYTICS.equity_curve(mode="sim", window=250)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["tickers"], [])
        self.assertEqual(result["strategies"], [])

    def test_ledger_without_strategy_field_degrades_to_empty_not_crash(self):
        """历史台账是加字段之前写下的，没有 strategy；必须容忍，不能报错。"""
        fixture = json.loads(json.dumps(LEDGER_FIXTURE))
        for row in fixture["sim"]["history"]:
            row.pop("strategy", None)
        self.ledger.write_text(json.dumps(fixture), encoding="utf-8")
        result = ANALYTICS.equity_curve(mode="sim", window=250)
        self.assertGreater(result["count"], 0)
        self.assertEqual(result["strategies"], [])
        self.assertEqual(result["tickers"], ["000001", "600519"])


class EngineLedgerStrategyTest(unittest.TestCase):
    """台账落盘必须保留 strategy：只写在自由文本 reason 里，界面无法归因。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["DSH_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "DSH_HOME", None)
        sys.path.insert(0, str(ROOT / "plugins" / "engine" / "python"))
        self.engine = load_module("engine_subject", ROOT / "plugins" / "engine" / "python" / "engine.py")

    def test_ledger_round_trip_keeps_strategy(self):
        ledger = self.engine.load_ledger("sim")
        ledger["history"].append({"action": "BUY", "ticker": "600519", "shares": 100,
                                  "price": 1700.0, "strategy": "rsi(30,70)",
                                  "date": "2024-01-03", "fee": 5.0})
        self.engine.save_ledger("sim", ledger)
        reread = self.engine.load_ledger("sim")
        self.assertEqual(reread["history"][-1]["strategy"], "rsi(30,70)")

    def test_order_construction_records_the_strategy(self):
        """源码级护栏：两条下单分支都必须写入 strategy。"""
        source = (ROOT / "plugins" / "engine" / "python" / "engine.py").read_text(encoding="utf-8")
        self.assertIn('"stop": None, "strategy": s["strategy"]', source)
        self.assertIn('"stop": stop, "strategy": s["strategy"]', source)


if __name__ == "__main__":
    unittest.main()
