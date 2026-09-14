"""日历抓取单测：注入假 fetcher，锁定 market 大写与 start/end 必传的调用口径。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "datasource" / "python"))
from trading_core import calendar, store  # noqa: E402

FUTU_PAYLOAD = {"trading_days": [
    {"time": "2026-09-11", "trade_date_type": "WHOLE", "trade_second": 14400},
    {"time": "2026-09-12", "trade_date_type": "CLOSE", "trade_second": 0}]}


class CalendarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        self.seen = []

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def fake_fetch(self, market, start, end):
        self.seen.append((market, start, end))
        return FUTU_PAYLOAD

    def test_sync_uses_uppercase_market_and_range(self):
        n = calendar.sync_calendar(self.conn, "sh", "2026-09-11", "2026-09-12",
                                   fetcher=self.fake_fetch)
        self.assertEqual(n, 2)
        self.assertEqual(self.seen, [("SH", "2026-09-11", "2026-09-12")])
        self.assertTrue(store.is_trading_day(self.conn, "SH", "2026-09-11"))
        self.assertFalse(store.is_trading_day(self.conn, "SH", "2026-09-12"))

    def test_trading_days_range(self):
        calendar.sync_calendar(self.conn, "SH", "2026-09-11", "2026-09-12",
                               fetcher=self.fake_fetch)
        self.assertEqual(store.trading_days(self.conn, "SH", "2026-09-01", "2026-09-30"),
                         ["2026-09-11"])


class MarketConstantsTest(unittest.TestCase):
    """2026-09-14 实测口径锁死（规格 §13.2/§13.5）：ktype 整数、指数代码映射。"""

    def test_ktype_values_are_integers(self):
        from trading_datasource.market import PERIOD_TO_FUTU_KTYPE
        self.assertTrue(all(isinstance(v, int) for v in PERIOD_TO_FUTU_KTYPE.values()))
        self.assertEqual(PERIOD_TO_FUTU_KTYPE["1d"], 2)

    def test_index_symbol_mapping(self):
        from trading_datasource.market import INDEX_SYMBOLS
        self.assertEqual(INDEX_SYMBOLS["csi300"], "SH.000300")
        self.assertEqual(INDEX_SYMBOLS["hsi"], "HK.800000")
        self.assertEqual(INDEX_SYMBOLS["ixic"], "US..IXIC")
        self.assertEqual(INDEX_SYMBOLS["dji"], "US..DJI")
        self.assertEqual(INDEX_SYMBOLS["sp500_proxy"], "US.SPY")  # 标普指数代码无效，SPY 替代


if __name__ == "__main__":
    unittest.main()
