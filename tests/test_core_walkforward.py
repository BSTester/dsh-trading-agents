"""walk-forward：训练 504/测试 63/步长 63 可配置为小窗口做离线测试（90/20/20）。

计划修正：(1) 引擎交易日取自 store.trading_days，setUp 补日历 fixture；
(2) 合成数据取 6 个月（150 个 bar 日）——train=90/test=20/step=20 下恰好
3 折，与计划断言 len(folds)==3 一致（计划原数据 12 个月会出 10 折，自相矛盾）。
"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import store, walkforward  # noqa: E402


class WfTest(unittest.TestCase):
    def test_oos_concat_and_param_groups(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        days = [f"2025-{m:02d}-{d:02d}" for m in range(1, 7) for d in range(1, 26)]
        store.upsert_calendar(conn, "SH",
                              [{"day": d, "trade_date_type": "WHOLE"} for d in days])
        for s in ("SH.600519", "SH.000858", "SZ.300750", "SH.601899", "SH.600036"):
            store.upsert_bars(conn, s, "1d",
                              [{"t": d, "o": 1, "h": 2, "l": 0.5,
                                "c": 1 + (int(d[8:]) + len(s)) % 7 * 0.1,
                                "v": 1} for d in days], "t")
        result = walkforward.run(conn, strategy_id="momentum_value_top5",
                                 train=90, test=20, step=20,
                                 grid={"top_n": [3, 5]}, start=days[0], end=days[-1])
        self.assertEqual(result["summary"]["param_groups"], 2)
        self.assertTrue(result["oos_curve"])
        self.assertEqual(len(result["folds"]), 3)
        # OOS 拼接曲线长度 = 测试窗 × 折数
        self.assertEqual(len(result["oos_curve"]), 20 * 3)
        # 报告头牌指标：OOS sharpe 由各折 oos_sharpe 汇总而来（任务 9 e2e 依赖）
        self.assertIn("oos_sharpe", result["summary"])
        for fold in result["folds"]:
            self.assertIn("params", fold)
            self.assertIn("oos_sharpe", fold)


if __name__ == "__main__":
    unittest.main()
