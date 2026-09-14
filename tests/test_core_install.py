"""安装器接线测试：core 必须进入 LIBRARIES/UNIFIED_PYTHON，.pth 必须覆盖两个库。"""
import importlib.util
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _load_installer():
    spec = importlib.util.spec_from_file_location(
        "install_plugins_under_test", _REPO / "scripts" / "install_plugins.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerCoreWiring(unittest.TestCase):
    def setUp(self):
        self.mod = _load_installer()

    def test_core_in_libraries_and_unified(self):
        self.assertIn("core", self.mod.LIBRARIES)
        self.assertIn("core", self.mod.UNIFIED_PYTHON)

    def test_core_package_name_registered(self):
        self.assertEqual(self.mod.PACKAGE_NAMES.get("core"), "@bstester/dsh-trading-core")

    def test_library_markers_cover_core(self):
        self.assertEqual(self.mod.LIBRARY_MARKERS["core"], "trading_core")
        self.assertEqual(self.mod.LIBRARY_MARKERS["datasource"], "trading_datasource")

    def test_pth_lines_cover_both_libraries(self):
        lines, missing = self.mod.data_layer_pth_lines(_REPO / "nonexistent-home")
        # home 不存在时两个库都缺，但函数必须同时列出两个候选目录
        self.assertEqual(len(lines), 0)
        self.assertEqual(sorted(missing), ["core", "datasource"])


if __name__ == "__main__":
    unittest.main()
