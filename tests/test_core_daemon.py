"""daemon 单测：假时钟驱动一轮调度；心跳与作业历史落盘；休市跳过；
执行体接线（runner 注入）；指令分派（kill/unkill/execute_plan/cancel_plan/run_job）。"""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import commands, daemon, execute, oms, store  # noqa: E402

RISK_CFG = {"risk_per_trade": 0.01, "max_positions": 5,
            "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03}


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

    def test_tick_records_alert_when_calendar_missing(self):
        """日历未同步的市场：跳过并告警，不让单市场缺日历拖垮整个调度循环。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        conn = store.connect(str(home / "t.sqlite"))
        ran = []
        daemon.tick(conn, home=str(home),
                    jobs={"SH": [{"name": "sync_bars", "at": "16:00",
                                  "fn": lambda ctx: ran.append("x") or {}}]},
                    now=lambda: "2026-09-14 16:00:30")
        self.assertEqual(ran, [])
        rows = __import__("trading_core.alerts", fromlist=["x"]).list_recent(conn)
        # 按标题过滤：同一次 tick 还可能发「关注池未配置」的首启告警（空 home 必然触发），
        # 那是另一件事（配置缺口），本用例只断言日历缺失告警。
        calendar_rows = [r for r in rows if r["title"] == "日历未同步"]
        self.assertEqual(len(calendar_rows), 1, rows)
        self.assertEqual(calendar_rows[0]["level"], "warn")


class RunJobTest(unittest.TestCase):
    def test_run_job_via_runner(self):
        seen = []
        job = {"name": "sync", "cmd": ["sync-bars", "--tickers", "600519"]}
        daemon._run_job(None, job, "/tmp", runner=lambda cmd: seen.append(cmd))
        self.assertEqual(seen[0][0:2], ["sync-bars", "--tickers"])

    def test_subprocess_runner_resolves_watchlist_placeholder(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        (home / "trading-platform.json").write_text(
            json.dumps({"watchlist": ["SH.600519", "HK.00700"]}), encoding="utf-8")
        resolved = daemon.resolve_command(["sync-bars", "--tickers", "@watchlist"], home)
        self.assertEqual(resolved, ["sync-bars", "--tickers", "SH.600519,HK.00700"])
        # 关注池为空：不跑作业（宁可不跑，也不给 CLI 喂空参数）
        (home / "trading-platform.json").write_text("{}", encoding="utf-8")
        self.assertIsNone(daemon.resolve_command(["sync-bars", "--tickers", "@watchlist"], home))
        self.assertEqual(daemon.resolve_command(["quality", "--market", "SH"], home),
                         ["quality", "--market", "SH"])


class HandleCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)

    def _poll(self, **kwargs):
        return commands.poll(str(self.home), handler=lambda cmd: daemon.handle_command(
            self.conn, str(self.home), cmd, **kwargs))

    def test_kill_and_unkill_manage_kill_file(self):
        commands.write_command(str(self.home), "kill", {})
        self._poll()
        self.assertTrue(daemon.kill_path(self.home).exists())
        commands.write_command(str(self.home), "unkill", {})
        self._poll()
        self.assertFalse(daemon.kill_path(self.home).exists())

    def test_run_job_immediately_via_injected_runner(self):
        seen = []
        commands.write_command(str(self.home), "run_job", {"job": "sync_bars"})
        out = self._poll(runner=lambda cmd: seen.append(cmd))
        self.assertTrue(out[0]["result"]["ok"])
        self.assertEqual(seen[0][0], "sync-bars")

    def test_execute_plan_dispatches_with_injected_executor(self):
        commands.write_command(str(self.home), "execute_plan",
                               {"plan_hash": "h1", "expected_mode": "SIM"})
        seen = []
        out = self._poll(executor=lambda conn, home, cmd: seen.append(cmd["plan_hash"]) or {"ok": True})
        self.assertTrue(out[0]["result"]["ok"])
        self.assertEqual(seen, ["h1"])

    def test_execute_plan_runs_frozen_plan_and_advances_state_machine(self):
        self.conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,"
                          "content_hash,status,created_at) VALUES('P1','2026-09-13',"
                          "'SIM','s','{}','h1','frozen','2026-09-13 10:00:00')")
        self.conn.commit()
        o = oms.register_order(self.conn, "P1", "SH.600519", "SH", "BUY", 100, 100.0, "SIM", "h1")
        oms.transition(self.conn, o["client_order_id"], "frozen")
        commands.write_command(str(self.home), "execute_plan",
                               {"plan_hash": "h1", "expected_mode": "SIM"})
        out = self._poll(executor=lambda conn, home, cmd: daemon._execute_plan(
            conn, home, cmd, broker_call=lambda name, args, timeout=30: {"order_id": "1"},
            equity=1_000_000.0, today="2026-09-14", calendar_ok=True))
        self.assertTrue(out[0]["result"]["ok"])
        statuses = {row["status"] for row in store.get_orders_by_plan(self.conn, "P1")}
        self.assertEqual(statuses, {"submitted"})

    def test_execute_plan_refuses_without_broker_channel(self):
        self.conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,"
                          "content_hash,status,created_at) VALUES('P1','2026-09-13',"
                          "'SIM','s','{}','h1','frozen','2026-09-13 10:00:00')")
        self.conn.commit()
        result = daemon._execute_plan(self.conn, str(self.home), {"plan_hash": "h1"})
        self.assertFalse(result["ok"])
        self.assertIn("券商通道", result["error"])

    def test_execute_plan_refuses_unknown_or_non_frozen_hash(self):
        result = daemon._execute_plan(self.conn, str(self.home), {"plan_hash": "nope"})
        self.assertFalse(result["ok"])

    def test_cancel_plan_cancels_only_local_unsubmitted_orders(self):
        self.conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,"
                          "content_hash,status,created_at) VALUES('P1','2026-09-13',"
                          "'SIM','s','{}','h1','frozen','2026-09-13 10:00:00')")
        self.conn.commit()
        draft = oms.register_order(self.conn, "P1", "SH.600519", "SH", "BUY", 100, 100.0, "SIM", "h1")
        submitted = oms.register_order(self.conn, "P1", "SZ.300750", "SZ", "BUY", 100, 50.0, "SIM", "h1")
        oms.transition(self.conn, submitted["client_order_id"], "frozen")
        oms.transition(self.conn, submitted["client_order_id"], "submitting")
        oms.transition(self.conn, submitted["client_order_id"], "submitted", broker_order_id="9")
        result = daemon.handle_command(self.conn, str(self.home),
                                       {"type": "cancel_plan", "plan_hash": "h1"})
        self.assertTrue(result["ok"])
        by_id = {row["client_order_id"]: row["status"] for row in store.get_orders_by_plan(self.conn, "P1")}
        self.assertEqual(by_id[draft["client_order_id"]], "cancelled")      # 未提交：本地撤销
        self.assertEqual(by_id[submitted["client_order_id"]], "submitted")  # 在途：留给对账兜底，不自动清


class CliDaemonTest(unittest.TestCase):
    """daemon 子命令 --once：一轮调度 + 指令轮询后退出（常驻模式不自动化测试）。"""

    def test_cli_daemon_once_runs_tick_and_dispatches_commands(self):
        import contextlib
        import io
        from trading_core import cli
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        conn = store.connect(str(home / "t.sqlite"))
        conn.close()
        commands.write_command(str(home), "kill", {})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli.main(["daemon", "--once", "--db", str(home / "t.sqlite"),
                             "--home", str(home)])
        self.assertEqual(code, 0)
        self.assertTrue(daemon.kill_path(home).exists())            # 指令被消费
        self.assertTrue(daemon.heartbeat_path(home).exists())       # 心跳落盘
        self.assertIn("once", buffer.getvalue())                    # JSON 摘要


class AutoPipelineConfigTest(unittest.TestCase):
    """auto_pipeline 配置（WP9 任务 3，规格 §4.1）：键缺省补默认；非法 fail-closed 报错。

    缺失与非法是两回事：缺失=用默认值（功能关闭是默认态），非法=ValueError
    （静默降级成默认会让「配置写错了」伪装成「功能没开」，比报错更危险）。
    """

    def _home(self, config=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        if config is not None:
            (home / "trading-platform.json").write_text(
                json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return home

    def test_defaults_when_missing(self):
        """无文件、无 auto_pipeline 键 → 功能关闭 + 默认时刻表。"""
        for home in (self._home(), self._home({"watchlist": ["SH.600519"]})):
            cfg = daemon.auto_pipeline_config(str(home))
            self.assertFalse(cfg["enabled"])
            self.assertEqual(cfg["strategies"], [])
            self.assertEqual(cfg["exec_at"], {"SH": "09:35", "HK": "09:45", "US": "22:35"})
            self.assertEqual(cfg["reconcile_at"], "19:00")

    def test_full_valid_config_roundtrip(self):
        home = self._home({"auto_pipeline": {
            "enabled": True,
            "strategies": [{"market": "SH", "strategy": "watchlist_rsi", "watchlist": "SH"}],
            "exec_at": {"SH": "09:40", "HK": "09:50", "US": "22:40"},
            "reconcile_at": "19:30"}})
        cfg = daemon.auto_pipeline_config(str(home))
        self.assertEqual(cfg, {"enabled": True,
                               "strategies": [{"market": "SH", "strategy": "watchlist_rsi",
                                               "watchlist": "SH"}],
                               "exec_at": {"SH": "09:40", "HK": "09:50", "US": "22:40"},
                               # 未给 exec_window_minutes → 补默认 30（规格 §4.1）
                               "exec_window_minutes": 30,
                               "reconcile_at": "19:30"})

    def test_exec_window_minutes_roundtrip(self):
        """执行窗口：显式给值覆盖默认；默认值随缺省补全。"""
        home = self._home({"auto_pipeline": {"enabled": True, "exec_window_minutes": 5}})
        self.assertEqual(daemon.auto_pipeline_config(str(home))["exec_window_minutes"], 5)
        self.assertEqual(
            daemon.auto_pipeline_config(str(self._home()))["exec_window_minutes"], 30)

    def test_exec_window_minutes_upper_bound(self):
        """上界闭区间：设到上界放行、超过即拒绝（N2——窗口越长守卫越接近失效）。"""
        home = self._home({"auto_pipeline": {
            "exec_window_minutes": daemon.EXEC_WINDOW_MAX_MINUTES}})
        self.assertEqual(daemon.auto_pipeline_config(str(home))["exec_window_minutes"],
                         daemon.EXEC_WINDOW_MAX_MINUTES)
        with self.assertRaises(ValueError) as caught:
            daemon.auto_pipeline_config(str(self._home({"auto_pipeline": {
                "exec_window_minutes": daemon.EXEC_WINDOW_MAX_MINUTES + 1}})))
        self.assertIn(str(daemon.EXEC_WINDOW_MAX_MINUTES), str(caught.exception))

    def test_partial_keys_fill_defaults(self):
        """单键缺省补默认：只给 enabled 或只给 exec_at 的一个市场。"""
        cfg = daemon.auto_pipeline_config(str(self._home({"auto_pipeline": {"enabled": True}})))
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["exec_at"], {"SH": "09:35", "HK": "09:45", "US": "22:35"})
        cfg = daemon.auto_pipeline_config(
            str(self._home({"auto_pipeline": {"exec_at": {"SH": "09:40"}}})))
        self.assertEqual(cfg["exec_at"], {"SH": "09:40", "HK": "09:45", "US": "22:35"})

    def test_strategy_watchlist_defaults_to_watchlist_pool(self):
        """I1：``watchlist`` 是池键名、可省略（缺省池 watchlist）——省略即补全，
        下游无需各自处理 None；显式给出的池键原样保留。"""
        home = self._home({"auto_pipeline": {"enabled": True, "strategies": [
            {"market": "SH", "strategy": "watchlist_rsi"}]}})
        cfg = daemon.auto_pipeline_config(str(home))
        self.assertEqual(cfg["strategies"],
                         [{"market": "SH", "strategy": "watchlist_rsi",
                           "watchlist": "watchlist"}])
        home = self._home({"auto_pipeline": {"enabled": True, "strategies": [
            {"market": "US", "strategy": "watchlist_rsi", "watchlist": "us_pool"}]}})
        self.assertEqual(daemon.auto_pipeline_config(str(home))["strategies"][0]["watchlist"],
                         "us_pool")
        # 空串仍是非法值（显式给错的池键名要被拦下，而不是静默当缺省）
        with self.assertRaises(ValueError):
            daemon.auto_pipeline_config(str(self._home({"auto_pipeline": {
                "enabled": True, "strategies": [
                    {"market": "SH", "strategy": "watchlist_rsi", "watchlist": " "}]}})))

    def test_invalid_configs_raise(self):
        """非法配置一律 ValueError（fail-closed），不做静默降级。"""
        cases = {
            "auto_pipeline 非对象": {"auto_pipeline": []},
            "enabled 非布尔": {"auto_pipeline": {"enabled": "yes"}},
            "strategies 非列表": {"auto_pipeline": {"strategies": {}}},
            "策略项非对象": {"auto_pipeline": {"strategies": ["rsi"]}},
            "策略项缺字段": {"auto_pipeline": {"strategies": [{"market": "SH"}]}},
            "策略项字段类型错": {"auto_pipeline": {"strategies": [
                {"market": "SH", "strategy": 1, "watchlist": "SH"}]}},
            "策略项未知键": {"auto_pipeline": {"strategies": [
                {"market": "SH", "strategy": "watchlist_rsi", "watchlist": "SH",
                 "extra": 1}]}},
            "exec_at 非对象": {"auto_pipeline": {"exec_at": "09:35"}},
            "exec_at 格式错": {"auto_pipeline": {"exec_at": {"SH": "9:35"}}},
            "exec_at 越界": {"auto_pipeline": {"exec_at": {"SH": "25:00"}}},
            "exec_at 未知市场": {"auto_pipeline": {"exec_at": {"SZ": "09:35"}}},
            "exec_window 零": {"auto_pipeline": {"exec_window_minutes": 0}},
            "exec_window 负数": {"auto_pipeline": {"exec_window_minutes": -1}},
            "exec_window 字符串": {"auto_pipeline": {"exec_window_minutes": "30"}},
            "exec_window 布尔": {"auto_pipeline": {"exec_window_minutes": True}},
            "exec_window 超上界": {"auto_pipeline": {"exec_window_minutes":
                                                     daemon.EXEC_WINDOW_MAX_MINUTES + 1}},
            "reconcile_at 格式错": {"auto_pipeline": {"reconcile_at": "19:00:00"}},
            "顶层未知键": {"auto_pipeline": {"enabled": True, "strategy": "rsi"}},
        }
        for label, config in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    daemon.auto_pipeline_config(str(self._home(config)))

    def test_returns_fresh_copy(self):
        """返回值不得与模块级默认共享可变对象（调用方改 exec_at 不能污染下次读取）。"""
        home = self._home()
        first = daemon.auto_pipeline_config(str(home))
        first["exec_at"]["SH"] = "23:59"
        first["strategies"].append({"market": "SH", "strategy": "x", "watchlist": "SH"})
        second = daemon.auto_pipeline_config(str(home))
        self.assertEqual(second["exec_at"]["SH"], "09:35")
        self.assertEqual(second["strategies"], [])


if __name__ == "__main__":
    unittest.main()
