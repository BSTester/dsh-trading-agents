"""daemon 单测：假时钟驱动一轮调度；心跳与作业历史落盘；休市跳过。"""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import daemon, store  # noqa: E402


class DaemonTest(unittest.TestCase):
    def test_tick_runs_due_jobs_and_writes_heartbeat(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        conn = store.connect(str(home / "t.sqlite"))
        store.upsert_calendar(conn, "SH", [{"day": "2026-09-14", "trade_date_type": "WHOLE",
                                            "trade_second": 14400}])
        ran = []
        jobs = {"SH": [{"name": "sync_bars", "at": "16:00",
                        "fn": lambda ctx: ran.append("sync") or {"ok": True}}]}
        clock = {"now": "2026-09-14 16:00:30"}
        daemon.tick(conn, home=str(home), jobs=jobs, now=lambda: clock["now"])
        self.assertEqual(ran, ["sync"])
        hb = json.loads((home / "trading-daemon.json").read_text())
        self.assertEqual(hb["last_job"], "sync_bars")
        self.assertIn("heartbeat", hb)

    def test_closed_market_skips(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        conn = store.connect(str(home / "t.sqlite"))
        store.upsert_calendar(conn, "SH", [{"day": "2026-09-14", "trade_date_type": "CLOSE",
                                            "trade_second": 0}])
        ran = []
        daemon.tick(conn, home=str(home),
                    jobs={"SH": [{"name": "sync_bars", "at": "16:00",
                                  "fn": lambda ctx: ran.append("x") or {}}]},
                    now=lambda: "2026-09-14 16:00:30")
        self.assertEqual(ran, [])  # 休市不跑


if __name__ == "__main__":
    unittest.main()
