"""WP7 服务内调度器：线程按间隔 tick、stop 可停、tick 组装复用 daemon 协议。全部离线。"""
import json, sys, tempfile, threading, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
from server import scheduler  # noqa: E402
from trading_core import daemon, store  # noqa: E402


class SchedulerThreadTest(unittest.TestCase):
    def test_runs_tick_on_interval_and_stops(self):
        calls = []
        done = threading.Event()
        def tick():
            calls.append(time.monotonic())
            if len(calls) >= 3:
                done.set()
        s = scheduler.Scheduler(tick, interval=0.02)
        s.start()
        self.assertTrue(done.wait(5), "interval 到期应重复 tick")
        s.stop()
        count_after_stop = len(calls)
        time.sleep(0.1)
        self.assertEqual(len(calls), count_after_stop, "stop 后不得再 tick")
        self.assertFalse(s.alive or s._thread.is_alive())

    def test_non_positive_interval_is_rejected(self):
        """序言修复：interval<=0 直接拒绝，不得静默钳制（亚秒间隔要真实生效）。"""
        for bad in (0, -1, 0.0, -0.5):
            with self.assertRaises(ValueError, msg=bad):
                scheduler.Scheduler(lambda: None, interval=bad)

    def test_small_interval_ticks_have_sub_second_median_gap(self):
        """序言修复：interval=0.05 相邻 tick 间距中位数 < 0.5s——证明小间隔真实生效，
        而不是被钳制成 1s 慢慢爬（任务 1 审查：max(1.0, interval) 掩盖了重复 tick 语义）。"""
        ticks = []
        done = threading.Event()

        def tick():
            ticks.append(time.monotonic())
            if len(ticks) >= 8:
                done.set()

        s = scheduler.Scheduler(tick, interval=0.05)
        s.start()
        self.assertTrue(done.wait(5), "0.05s 间隔应在 5s 内跑满 8 个 tick")
        s.stop()
        gaps = sorted(b - a for a, b in zip(ticks, ticks[1:]))
        self.assertGreater(len(gaps), 0)
        self.assertLess(gaps[len(gaps) // 2], 0.5,
                        f"中位间距 {gaps[len(gaps) // 2]:.3f}s 表明间隔被钳制")

    def test_tick_exception_does_not_kill_thread(self):
        state = {"n": 0}
        event = threading.Event()
        def flaky():
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("boom")
            if state["n"] >= 3:
                event.set()
        s = scheduler.Scheduler(flaky, interval=0.02)
        s.start()
        self.assertTrue(event.wait(5))
        s.stop()
        self.assertIsNotNone(s.last_error)


class BuildTickTest(unittest.TestCase):
    def test_build_tick_runs_due_jobs_and_writes_heartbeat(self):
        with tempfile.TemporaryDirectory() as home:
            conn = store.connect(str(Path(home) / "t.sqlite"))
            store.upsert_calendar(conn, "SH", [{"day": "2026-09-16", "trade_date_type": "WHOLE",
                                                "trade_second": 14400}])
            ran = []
            jobs = {"SH": [{"name": "sync", "at": "16:00",
                            "fn": lambda ctx: ran.append("sync") or {"ok": True}}]}
            tick = scheduler.build_tick(home, jobs=jobs, now=lambda: "2026-09-16 16:00:30",
                                        conn=lambda: conn)
            tick()
            conn.close()
            self.assertEqual(ran, ["sync"])
            hb = json.loads((Path(home) / "trading-daemon.json").read_text(encoding="utf-8"))
            self.assertIn("heartbeat", hb)


class IdleScheduler:
    """healthz 用的调度器替身：start/stop 只记账，存活态可指定。"""

    def __init__(self, alive=False, error=None):
        self.calls = []
        self.alive = alive
        self.last_error = error

    def start(self):
        self.calls.append("start")

    def stop(self):
        self.calls.append("stop")


class HealthzSchedulerTest(unittest.TestCase):
    """healthz 暴露调度器存活态；create_app(scheduler=...) 注入即用。"""

    def test_healthz_reports_scheduler_state_of_injected_object(self):
        from fastapi.testclient import TestClient
        from server import app as app_module
        with tempfile.TemporaryDirectory() as tmp:
            stub = IdleScheduler(alive=True, error="boom")
            app = app_module.create_app(home=tmp, dist=str(Path(tmp) / "dist-missing"),
                                        scheduler=stub)
            with TestClient(app) as client:
                body = client.get("/healthz").json()
            self.assertEqual(body["scheduler"], {"alive": True, "last_error": "boom"})
            self.assertEqual(stub.calls, ["start", "stop"], "lifespan 应启停注入的调度器")

    def test_healthz_truncates_last_error_to_300_chars(self):
        """序言修复：healthz 的 last_error 截断 ≤300 字符（对齐 str(error)[:300] 惯例）。"""
        from fastapi.testclient import TestClient
        from server import app as app_module
        with tempfile.TemporaryDirectory() as tmp:
            stub = IdleScheduler(alive=True, error="x" * 500)
            app = app_module.create_app(home=tmp, dist=str(Path(tmp) / "dist-missing"),
                                        scheduler=stub)
            client = TestClient(app)
            body = client.get("/healthz").json()
        last_error = body["scheduler"]["last_error"]
        self.assertEqual(len(last_error), 300)
        self.assertTrue(last_error.startswith("xxx"))

    def test_default_create_app_has_idle_scheduler_state(self):
        from fastapi.testclient import TestClient
        from server import app as app_module
        with tempfile.TemporaryDirectory() as tmp:
            app = app_module.create_app(home=tmp, dist=str(Path(tmp) / "dist-missing"))
            self.assertTrue(hasattr(app.state, "scheduler"), "默认应建调度器（未启不动）")
            client = TestClient(app)
            body = client.get("/healthz").json()
            self.assertEqual(body["scheduler"], {"alive": False, "last_error": None})


if __name__ == "__main__":
    unittest.main()
