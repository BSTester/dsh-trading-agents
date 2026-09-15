"""对账（数量不一致即差异）与 TCA（到达价 vs 成交价 bps）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import reconcile, store, tca  # noqa: E402


class ReconcileTest(unittest.TestCase):
    def test_qty_mismatch_is_diff_and_halts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        local = {"SH.600519": 300}
        broker = {"SH.600519": 320}
        diffs = reconcile.compare(conn, local, broker, value_tolerance=0.005)
        self.assertEqual(diffs[0]["symbol"], "SH.600519")
        self.assertEqual(diffs[0]["qty_diff"], -20)

    def test_missing_side_is_diff(self):
        diffs = reconcile.compare(None, {"SH.600519": 300}, {"SZ.300750": 0})
        kinds = {(d["symbol"], d["kind"]) for d in diffs}
        self.assertIn(("SH.600519", "missing_side"), kinds)
        # 券商为 0/缺位、本地亦应记差异
        self.assertIn(("SZ.300750", "missing_side"), kinds)

    def test_value_diff_beyond_tolerance(self):
        local = {"SH.600519": {"qty": 300, "value": 474000.0}}
        broker = {"SH.600519": {"qty": 300, "value": 480000.0}}
        diffs = reconcile.compare(None, local, broker, value_tolerance=0.005)
        self.assertEqual(diffs[0]["kind"], "value")

    def test_consistent_positions_no_diff(self):
        local = {"SH.600519": {"qty": 300, "value": 474000.0}}
        broker = {"SH.600519": {"qty": 300, "value": 474100.0}}  # 0.02% < 0.5% 容差
        self.assertEqual(reconcile.compare(None, local, broker), [])


class TcaTest(unittest.TestCase):
    def test_tca_bps(self):
        bps = tca.slippage_bps(arrival=1580.0, filled=1580.30, side="BUY")
        self.assertAlmostEqual(bps, 1.898, places=2)


class TcaStoreTest(unittest.TestCase):
    def test_record_and_aggregate(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        tca.record(conn, "ORD-1", "SH.600519", arrival=100.0, filled=100.4, side="BUY")
        agg = tca.aggregate(conn)
        self.assertEqual(agg["count"], 1)
        self.assertGreater(agg["avg_bps"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
