"""WP2 依赖锁定：上游 schema 漂移时这里的断言会先红（规格 §2.2 协议）。"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
from trading_datasource.market import INDEX_SYMBOLS, PERIOD_TO_FUTU_KTYPE  # noqa: E402


class Wp2Locks(unittest.TestCase):
    def test_csi800_index_available(self):
        self.assertEqual(INDEX_SYMBOLS["csi800"], "SH.000906")

    def test_valuation_source_converged(self):
        import importlib.util
        p = Path(__file__).resolve().parents[1] / "plugins" / "workbench" / "python" / "factors.py"
        self.assertTrue(p.is_file(), "估值因子唯一实现必须存在（收敛前）")


if __name__ == "__main__":
    unittest.main()
