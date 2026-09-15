"""告警：分级落表、critical 置心跳标志、可选桌面通知（探测为 None 时不发）。"""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import alerts, store  # noqa: E402


class AlertsTest(unittest.TestCase):
    def test_level_store_and_critical_flag(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        conn = store.connect(str(home / "t.sqlite"))
        alerts.emit(conn, home=str(home), level="warn", title="缺口", detail="600519 缺 2 日")
        alerts.emit(conn, home=str(home), level="critical", title="对账差异", detail="AAPL −20")
        rows = alerts.list_recent(conn, limit=10)
        self.assertEqual([r["level"] for r in rows], ["critical", "warn"])  # 倒序
        hb = json.loads((home / "trading-daemon.json").read_text())
        self.assertTrue(hb["critical"])  # critical 置心跳标志位（工作台红点依据）


if __name__ == "__main__":
    unittest.main()
