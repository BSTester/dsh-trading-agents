"""关注池读取唯一实现（WP9 修订 I4/K1）：命名池 + 市场分片 + strict 语义。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))

from trading_core import watchlist  # noqa: E402


class WatchlistSymbolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def _config(self, payload):
        (self.home / "trading-platform.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_default_pool_and_market_filter(self):
        """缺省池 = watchlist；market 给定时只取该市场链（SZ/BJ 归 SH）。"""
        self._config({"watchlist": ["SH.600519", "sz.300750", "HK.00700",
                                    "US.AAPL", "BJ.430047"]})
        self.assertEqual(
            watchlist.watchlist_symbols(str(self.home)),
            ["SH.600519", "SZ.300750", "HK.00700", "US.AAPL", "BJ.430047"])
        self.assertEqual(
            watchlist.watchlist_symbols(str(self.home), market="SH"),
            ["SH.600519", "SZ.300750", "BJ.430047"])
        self.assertEqual(watchlist.watchlist_symbols(str(self.home), market="HK"),
                         ["HK.00700"])
        self.assertEqual(watchlist.watchlist_symbols(str(self.home), market="US"),
                         ["US.AAPL"])

    def test_comma_string_and_trim(self):
        """字符串值按逗号切分并去空、大写归一（既有 ``@watchlist`` 写法兼容）。"""
        self._config({"watchlist": " sh.600519 , HK.00700 ,, "})
        self.assertEqual(watchlist.watchlist_symbols(str(self.home)),
                         ["SH.600519", "HK.00700"])

    def test_named_pool_and_missing_semantics(self):
        """命名池：存在即读；缺失时非 strict 返回空、strict 抛错（fail-closed）。"""
        self._config({"watchlist": ["SH.600519"], "us_pool": ["US.AAPL", "US.MSFT"]})
        self.assertEqual(watchlist.watchlist_symbols(str(self.home), key="us_pool"),
                         ["US.AAPL", "US.MSFT"])
        # 键存在但为空列表：合法空池（不抛）
        self._config({"watchlist": [], "empty_pool": []})
        self.assertEqual(watchlist.watchlist_symbols(str(self.home), key="empty_pool"), [])
        # 键不存在：非 strict 空返回；strict 抛 ValueError
        self.assertEqual(watchlist.watchlist_symbols(str(self.home), key="nope"), [])
        with self.assertRaises(ValueError):
            watchlist.watchlist_symbols(str(self.home), key="nope", strict=True)

    def test_read_pool_reports_existence(self):
        """read_pool 的 exists 区分「键不存在」与「键为空」（strict 判据）。"""
        self._config({"watchlist": []})
        self.assertEqual(watchlist.read_pool(str(self.home)), ([], True))
        self.assertEqual(watchlist.read_pool(str(self.home), key="other"), ([], False))

    def test_missing_config_file_is_empty_not_error(self):
        """无配置文件：池为空、不存在（配置缺失与配置错误分级不同）。"""
        self.assertEqual(watchlist.watchlist_symbols(str(self.home)), [])
        self.assertEqual(watchlist.read_pool(str(self.home)), ([], False))


if __name__ == "__main__":
    unittest.main()
