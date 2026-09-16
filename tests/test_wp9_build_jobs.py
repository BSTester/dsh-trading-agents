"""WP9 任务 6a：build_jobs 配置驱动装配 + tick 的 GLOBAL 链（规格 §4.1/§4.4）。

装配契约（规格 §4.1 硬约束）：enabled=False（或未配置）时返回值与 JOBS_DEFAULT
**逐键逐值相等**——关闭功能不改变任何现有调度行为；enabled=True 时在既有链**链尾追加**
build_plan（该市场 factors_snapshot + 5 分钟）与 auto_execute（配置 exec_at），
并以 GLOBAL 键承载 reconcile（配置 reconcile_at，不查交易日历）。

GLOBAL 链语义（规格 §4.4）：全局作业只受「时点已到 + 当日未跑」约束，不绑市场日历；
市场链在日历缺失时照旧跳过并告警。
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import alerts, daemon, store  # noqa: E402

STRATEGY_SH = {"market": "SH", "strategy": "watchlist_rsi", "watchlist": "SH"}


class BuildJobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))

    def _write_cfg(self, cfg):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"auto_pipeline": cfg}), encoding="utf-8")

    def _titles(self):
        return [row["title"] for row in alerts.list_recent(self.conn)]

    def _ran_keys(self):
        return store.kv_get(self.conn, "daemon:state", default={"ran": {}})["ran"]

    # ① 关闭态：与 JOBS_DEFAULT 逐键逐值相等（缺配置与显式 enabled=False 同契约）
    def test_disabled_equals_jobs_default(self):
        self.assertEqual(daemon.build_jobs(str(self.home), self.conn), daemon.JOBS_DEFAULT)
        self._write_cfg({"enabled": False})
        self.assertEqual(daemon.build_jobs(str(self.home), self.conn), daemon.JOBS_DEFAULT)
        self.assertFalse(self._titles())  # 关闭是默认态，不留告警

    # ② 开启态：链尾追加，既有链前缀不变，自动作业用配置时刻（含覆盖）
    def test_enabled_appends_auto_jobs(self):
        self._write_cfg({"enabled": True, "strategies": [STRATEGY_SH],
                         "exec_at": {"SH": "09:40"}, "reconcile_at": "19:30"})
        jobs = daemon.build_jobs(str(self.home), self.conn)

        sh = jobs["SH"]
        self.assertEqual(len(sh), len(daemon.JOBS_DEFAULT["SH"]) + 2)
        self.assertEqual(sh[:len(daemon.JOBS_DEFAULT["SH"])], daemon.JOBS_DEFAULT["SH"])
        self.assertEqual(sh[-2]["name"], "build_plan")
        self.assertEqual(sh[-2]["at"], "16:20")  # SH factors_snapshot 16:15 + 5
        self.assertEqual(sh[-2]["cmd"], ["plan-auto", "--market", "SH"])
        self.assertEqual(sh[-1]["name"], "auto_execute")
        self.assertEqual(sh[-1]["at"], "09:40")  # 配置覆盖默认 09:35
        self.assertEqual(sh[-1]["cmd"], ["auto-execute", "--market", "SH"])

        # 未覆盖的市场用默认时刻：HK 16:35→16:40 / 09:45；US 05:35→05:40 / 22:35
        self.assertEqual([j["at"] for j in jobs["HK"][-2:]], ["16:40", "09:45"])
        self.assertEqual(jobs["HK"][-1]["cmd"], ["auto-execute", "--market", "HK"])
        self.assertEqual([j["at"] for j in jobs["US"][-2:]], ["05:40", "22:35"])

        g = jobs[daemon.GLOBAL_CHAIN]
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0]["name"], "reconcile")
        self.assertEqual(g[0]["at"], "19:30")
        self.assertEqual(g[0]["cmd"], ["reconcile-daily"])
        # deepcopy：不改动全局常量，也不残留上一次装配的痕迹
        self.assertNotIn(daemon.GLOBAL_CHAIN, daemon.JOBS_DEFAULT)
        self.assertEqual(len(daemon.JOBS_DEFAULT["SH"]), 6)  # WP11 起含 sentiment_snapshot

    # ③ GLOBAL 链不查市场日历：日历缺失时市场链跳过并告警，全局链照常执行
    def test_global_chain_ignores_calendar(self):
        self._write_cfg({"enabled": True, "strategies": [STRATEGY_SH]})
        ran = []
        jobs = daemon.build_jobs(str(self.home), self.conn)
        # 用 fn 形式替换 GLOBAL 作业体：本任务不依赖 reconcile-daily 是否已实现
        jobs[daemon.GLOBAL_CHAIN] = [{"name": "reconcile", "at": "19:00",
                                      "fn": lambda ctx: ran.append("reconcile") or {}}]
        daemon.tick(self.conn, home=str(self.home), jobs=jobs,
                    now=lambda: "2026-09-14 19:00:30")

        self.assertEqual(ran, ["reconcile"])
        self.assertIn("日历未同步", self._titles())
        ran_keys = self._ran_keys()
        self.assertFalse([k for k in ran_keys if k.startswith("SH:")])
        self.assertTrue([k for k in ran_keys if k.startswith(daemon.GLOBAL_CHAIN + ":")])

    # ④ tick 缺省作业表走 build_jobs：到点的自动作业进入执行
    def test_tick_uses_build_jobs_by_default(self):
        self._write_cfg({"enabled": True, "strategies": [STRATEGY_SH]})
        store.upsert_calendar(self.conn, "SH", [{"day": "2026-09-14",
                                                 "trade_date_type": "WHOLE",
                                                 "trade_second": 14400}])
        seen = []
        original = daemon._run_job
        daemon._run_job = lambda conn, job, home, runner=None: seen.append(job["name"])
        self.addCleanup(setattr, daemon, "_run_job", original)

        daemon.tick(self.conn, home=str(self.home), now=lambda: "2026-09-14 16:20:30")

        # 16:20 时点：build_plan（16:20）刚到点；auto_execute（09:35）已过点——
        # tick 的既有补跑语义（「时点已到且当日未跑」即执行）会把它一并补跑，
        # 与既有数据作业一致（服务启动晚于时点即补跑当日作业）。
        self.assertIn("build_plan", seen)
        self.assertIn("auto_execute", seen)
        self.assertNotIn("reconcile", seen)     # 19:00 未到
        # 无日历的 HK/US 市场被跳过：装配出的自动作业不绕过日历判定
        self.assertEqual(seen.count("sync_bars"), 1)  # 仅 SH 链跑了自己的 sync_bars

    # ⑤ 配置非法：退化 + warn 告警，调度不崩（fail-soft 只在装配层）
    def test_invalid_config_degrades_with_warning(self):
        self._write_cfg({"enabled": True, "exec_at": {"SH": "9:35"}})
        jobs = daemon.build_jobs(str(self.home), self.conn)
        self.assertEqual(jobs, daemon.JOBS_DEFAULT)
        self.assertIn("auto_pipeline 配置非法", self._titles())

        daemon.tick(self.conn, home=str(self.home), now=lambda: "2026-09-14 16:20:30")
        self.assertIn("日历未同步", self._titles())  # 退化后照旧走市场日历判定

    # ⑥ run_job 指令可寻址自动作业（作业表升级后 run_job 不再只见 JOBS_DEFAULT）
    def test_run_job_can_address_auto_jobs(self):
        self._write_cfg({"enabled": True, "strategies": [STRATEGY_SH]})
        called = []
        out = daemon.handle_command(self.conn, str(self.home),
                                    {"type": "run_job", "job": "auto_execute"},
                                    runner=lambda cmd: called.append(cmd))
        self.assertTrue(out["ok"])
        self.assertEqual(called, [["auto-execute", "--market", "SH"]])

    # ⑦ build_jobs 返回新对象：调用方原地修改不污染JOBS_DEFAULT
    def test_build_jobs_returns_fresh_copy(self):
        before = copy.deepcopy(daemon.JOBS_DEFAULT)
        jobs = daemon.build_jobs(str(self.home), self.conn)
        jobs["SH"].append({"name": "x", "at": "00:00", "cmd": ["x"]})
        self.assertEqual(daemon.JOBS_DEFAULT, before)


if __name__ == "__main__":
    unittest.main()
