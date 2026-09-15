"""因子注册表单测：注册机制 + 价格域因子数值（合成序列，手算对照）。"""
import math, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import factors, store  # noqa: E402


class FactorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        bars = [{"t": f"2026-{m:02d}-{d:02d}", "o": 10, "h": 10.5, "l": 9.5, "c": 10 + i * 0.1, "v": 100}
                for i, (m, d) in enumerate([(m, d) for m in range(1, 9) for d in range(1, 29)])]
        # 计划修正：因子一律收 futu 符号（strategies/IC/CLI 同口径），bars 须按 SH.600519 落库
        store.upsert_bars(self.conn, "SH.600519", "1d", bars, "test")

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_registry_contains_price_domain(self):
        for name in ("momentum_20", "momentum_60", "momentum_120", "volatility_20"):
            self.assertIn(name, factors.REGISTRY)

    def test_momentum_20_value(self):
        v = factors.REGISTRY["momentum_20"](self.conn, "SH.600519", "2026-07-18")
        self.assertIsInstance(v, float)
        self.assertFalse(math.isnan(v))

    def test_missing_history_returns_none(self):
        v = factors.REGISTRY["momentum_120"](self.conn, "SH.600519", "2026-02-05")
        self.assertIsNone(v)  # 历史不足 → None（宁缺毋假），上层记 missing

    def test_valuation_factor_reads_store(self):
        # 字段名用 pe_ttm：依赖锁定表规定估值字段路径以 valuation_values 实测实现为准
        store.upsert_valuations(self.conn, "SH.600519", "2026-07-18", {"pe_ttm": 22.5}, "t")
        self.assertEqual(factors.REGISTRY["ep"](self.conn, "SH.600519", "2026-07-18"), 1 / 22.5)
        self.assertIsNone(factors.REGISTRY["ep"](self.conn, "SH.600519", "2026-07-01"))

    def test_zscore_mad_winsorize(self):
        vals = {"A": 1.0, "B": 1.1, "C": 0.9, "D": 1.05, "E": 100.0}  # E 为离群
        z = factors.cross_sectional_zscore(vals)
        # 计划修正：MAD 截断后 E 仍是截面最大值，原断言 |z_E|<|z_B| 恒不成立；
        # 等价可判定性质——离群点被夹住（与正常点 z 差距有界，不裁剪时 ≈2.23），
        # 且正常点保持分散（不裁剪时整截面被压扁，Spread 仅 ≈0.007）
        self.assertLess(z["E"] - z["B"], 2.0)
        pack = [z[k] for k in ("A", "B", "C", "D")]
        self.assertGreater(max(pack) - min(pack), 1.0)
        score = factors.composite_score({"A": {"f1": 1.0}, "B": {"f1": 2.0}}, weights={"f1": 1.0})
        self.assertGreater(score["B"], score["A"])


if __name__ == "__main__":
    unittest.main()
