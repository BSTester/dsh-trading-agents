"""WP2 端到端（离线）：合成库上走 ic / backtest CLI 子命令，产出 OOS 报告与 IC 检验 JSON。

计划修正：引擎交易日取自 store.trading_days，fixture 补日历；数据 6 个月使
train=90/test=20/step=20 恰好 3 折（与 walkforward 单测同口径）。
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
from trading_core import cli, store  # noqa: E402

DAYS = [f"2025-{m:02d}-{d:02d}" for m in range(1, 7) for d in range(1, 26)]
SYMS = ["SH.600519", "SH.000858", "SZ.300750", "SH.601899", "SH.600036",
        "SZ.000333", "SH.601318", "SZ.002415"]


def _seed(conn):
    store.upsert_calendar(conn, "SH", [{"day": d, "trade_date_type": "WHOLE"} for d in DAYS])
    for k, s in enumerate(SYMS):
        bars = [{"t": d, "o": 10.0 + k, "h": 11.0 + k, "l": 9.0 + k,
                 "c": 10.0 + k + ((DAYS.index(d) * 7 + k * 3) % 11 - 5) * 0.1, "v": 10000}
                for d in DAYS]
        store.upsert_bars(conn, s, "1d", bars, "test")
    store.upsert_bars(conn, "SH.000300", "1d",
                      [{"t": d, "o": 4000, "h": 4010, "l": 3990, "c": 4000 + DAYS.index(d), "v": 1}
                       for d in DAYS], "test")
    store.store_universe(conn, DAYS[0], "SH.000300", [s.split(".")[1] for s in SYMS], "test")
    for day in DAYS[:10]:
        for k, s in enumerate(SYMS[:5]):
            store.upsert_valuations(conn, s, day, {"pe_ttm": 8.0 + k * 4.0}, "test")


class Wp2E2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.sqlite")
        conn = store.connect(self.db)
        try:
            _seed(conn)
        finally:
            conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli.main(argv)
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def test_backtest_cli_reports_oos(self):
        out = self._run(["backtest", "--strategy", "momentum_value_top5",
                         "--start", DAYS[0], "--end", DAYS[-1],
                         "--train", "90", "--test", "20", "--step", "20", "--db", self.db])
        self.assertIn("summary", out)
        self.assertEqual(out["summary"]["param_groups"], 2)  # top_n 3/5 → 自曝 2 组
        self.assertEqual(out["summary"]["folds"], 3)
        self.assertIn("oos_sharpe", out["summary"])
        self.assertTrue(out["oos_curve"])

    def test_ic_cli_reports_rank_ic(self):
        out = self._run(["ic", "--factor", "momentum_60",
                         "--symbols", "SH.600519,SH.000858,SH.601899,SH.600036,SZ.300750",
                         "--as-of", "2025-04-15", "--horizon", "20", "--db", self.db])
        self.assertEqual(out["factor"], "momentum_60")
        self.assertEqual(out["samples"], 5)
        self.assertIsInstance(out["rank_ic"], float)
        self.assertLessEqual(out["rank_ic"], 1.0)


if __name__ == "__main__":
    unittest.main()
