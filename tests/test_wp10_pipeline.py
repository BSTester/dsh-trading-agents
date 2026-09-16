"""WP10 任务 1：流程快照（``snapshot-pipeline`` / ``pipeline`` 端点）——只读阶段状态推导。

全部离线：临时 home + 临时库；``auto_pipeline`` 配置写入 ``trading-platform.json``；
ran 标记与告警按**指定日期**构造（``alerts.emit`` 用真实时间，故归因用例直接 INSERT
指定 ``created_at``，让日期过滤可测且不受执行日影响）。

覆盖（对照计划任务 1 的测试清单）：
  ① 阶段推导（ran → ok / 无标记 → pending）+ 作业链顺序与 scheduled 时刻；
  ② 告警归因（skipped/failed）+ 跨市场消歧 + 链层/配置类告警；
  ③ plan/execute 两阶段（auto 计划与订单状态分布；cancelled/手工计划的口径）；
  ④ auto_pipeline 摘要（关闭 / 非法配置下端点仍 ok）；
  ⑤ 只读性（库与 kv 零变化、非法配置不写告警）；
  ⑥ 端点 envelope 契约与空载荷校验。
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from server import app as app_module  # noqa: E402
from server import compute  # noqa: E402
from trading_core import oms, pipeline, store  # noqa: E402

DATE = "2026-09-16"


class PipelineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)

    def enable_auto(self, **extra):
        cfg = {"enabled": True,
               "strategies": [{"market": "SH", "strategy": "watchlist_rsi"}],
               "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"}}
        cfg.update(extra)
        self.write_platform({"auto_pipeline": cfg})

    def write_platform(self, payload):
        (self.home / "trading-platform.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def mark_ran(self, market, job, at="2026-09-16 16:00:05", date=DATE):
        state = store.kv_get(self.conn, "daemon:state", default={"ran": {}}) or {"ran": {}}
        state.setdefault("ran", {})[f"{market}:{job}:{date}"] = at
        store.kv_set(self.conn, "daemon:state", state)

    def emit_alert(self, title, detail="", level="warn", date=DATE):
        """按指定日期直接落告警（alerts.emit 用真实时间，演练下日期不可控）。"""
        self.conn.execute("INSERT INTO alerts(level,title,detail,created_at) VALUES(?,?,?,?)",
                          (level, title, detail, f"{date} 16:20:00"))
        self.conn.commit()

    def snapshot(self, **kwargs):
        kwargs.setdefault("date", DATE)
        return pipeline.pipeline_snapshot(self.conn, self.home, **kwargs)

    def stages(self, out, market="SH"):
        return out["markets"][market]["stages"]

    def seed_auto_plan(self, market="SH", as_of="2026-09-15", status="frozen",
                       plan_id="PLN-20260915-SIM-AAAA"):
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
            "status,created_at,origin,market) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (plan_id, as_of, "sim", "watchlist_rsi", '{"SH.600519": 1.0}', "h" + plan_id[-4:],
             status, "2026-09-15 16:20:00", "auto", market))
        self.conn.commit()
        return plan_id

    def seed_order(self, plan_id, status="submitted", symbol="SH.600519"):
        """按结局构造订单。``cancelled`` 走**风控拒单的真实路径**（frozen → cancelled，
        见 ``execute.run`` 的被拒分支）；``submitting → cancelled`` 不是合法迁移，
        不要把「从未提交」写成「提交后被撤」。"""
        order = oms.register_order(self.conn, plan_id, symbol, symbol.split(".")[0],
                                   "BUY", 100, 100.0, "sim", "h" + plan_id[-4:])
        cid = order["client_order_id"]
        oms.transition(self.conn, cid, "frozen")
        if status == "cancelled":
            oms.transition(self.conn, cid, "cancelled", err="risk")
            return cid
        oms.transition(self.conn, cid, "submitting")
        if status == "submitted":
            oms.transition(self.conn, cid, "submitted", broker_order_id="9")
        elif status == "rejected":
            oms.transition(self.conn, cid, "rejected", err="broker")
        elif status == "unknown":
            oms.transition(self.conn, cid, "unknown", err="timeout")
        return cid


class StageDerivationTests(PipelineBase):
    def test_disabled_config_keeps_base_chain_and_no_auto_stages(self):
        """关闭态：只有基础链阶段（+ 表驱动的 plan/execute），且全部 pending。"""
        out = self.snapshot()
        stages = self.stages(out)
        self.assertEqual(list(stages), ["sync_bars", "sync_fundamentals", "merge_announcements",
                                        "quality", "factors_snapshot", "plan", "execute"])
        self.assertEqual({s["status"] for s in stages.values()}, {"pending"})
        self.assertFalse(out["auto_pipeline"]["enabled"])
        self.assertEqual(out["date"], DATE)

    def test_ran_marks_become_ok_with_scheduled_times(self):
        self.enable_auto()
        self.mark_ran("SH", "sync_bars", at="2026-09-16 16:00:05")
        self.mark_ran("SH", "build_plan", at="2026-09-16 16:22:10")
        out = self.snapshot()
        stages = self.stages(out)
        # 阶段顺序：真实作业链 + plan 紧跟 build_plan、execute 紧跟 auto_execute
        self.assertEqual(list(stages), ["sync_bars", "sync_fundamentals", "merge_announcements",
                                        "quality", "factors_snapshot", "build_plan", "plan",
                                        "auto_execute", "execute"])
        sync = stages["sync_bars"]
        self.assertEqual(sync["status"], "ok")
        self.assertEqual(sync["at"], "2026-09-16 16:00:05")
        self.assertEqual(sync["scheduled"], "16:00")
        self.assertEqual(sync["label"], "行情同步")
        build = stages["build_plan"]
        self.assertEqual(build["status"], "ok")
        self.assertEqual(build["scheduled"], "16:20")   # factors_snapshot 16:15 + 5
        self.assertEqual(stages["auto_execute"]["scheduled"], "09:35")
        # 未跑的阶段：pending 且 at 为空（不造时刻）
        quality = stages["quality"]
        self.assertEqual(quality["status"], "pending")
        self.assertIsNone(quality["at"])
        self.assertEqual(quality["scheduled"], "16:10")

    def test_ran_mark_of_other_date_does_not_count(self):
        self.enable_auto()
        self.mark_ran("SH", "sync_bars", date="2026-09-15")
        stages = self.stages(self.snapshot())
        self.assertEqual(stages["sync_bars"]["status"], "pending")


class AlertAttributionTests(PipelineBase):
    def test_build_plan_skip_alert_is_attributed_by_market(self):
        """detail 里的市场前缀决定归属：SH 的告警不污染 HK 的同名阶段。"""
        self.enable_auto()
        self.emit_alert("数据未就绪", "数据未就绪（应有最后交易日 2026-09-16）：SH.600519")
        out = self.snapshot()
        self.assertEqual(self.stages(out)["build_plan"]["status"], "skipped")
        self.assertEqual(self.stages(out)["build_plan"]["summary"], "数据未就绪")
        self.assertEqual(self.stages(out, "HK")["build_plan"]["status"], "pending")

    def test_failure_alerts_map_to_failed(self):
        self.enable_auto()
        self.emit_alert("策略权重失败", "策略权重计算失败：market=SH 权重非法")
        self.assertEqual(self.stages(self.snapshot())["build_plan"]["status"], "failed")

    def test_critical_alert_escalates_to_failed(self):
        self.enable_auto()
        self.mark_ran("GLOBAL", "reconcile", at="2026-09-16 19:00:02")
        self.emit_alert("对账差异", "market=SH 数量不一致", level="critical")
        out = self.snapshot()
        # ran 标记为准：已跑过的阶段不被 later 告警改写
        self.assertEqual(out["global"]["stages"]["reconcile"]["status"], "ok")
        # 未跑的市场链阶段：critical 只影响归因到它的阶段（此处无匹配 → 仍 pending）
        self.assertEqual(self.stages(out)["sync_bars"]["status"], "pending")

    def test_chain_alert_affects_all_data_stages(self):
        """日历未同步由 tick 在链层发出：该市场全部数据作业如实标 skipped。"""
        self.enable_auto()
        self.emit_alert("日历未同步", "日历 2026 未同步")
        stages = self.stages(self.snapshot())
        self.assertEqual(stages["sync_bars"]["status"], "skipped")
        self.assertEqual(stages["factors_snapshot"]["status"], "skipped")
        self.assertEqual(stages["sync_bars"]["summary"], "日历未同步")

    def test_config_alert_affects_auto_stages_only(self):
        self.enable_auto()
        self.emit_alert("auto_pipeline 配置非法", "exec_at 需为 HH:MM 格式")
        stages = self.stages(self.snapshot())
        self.assertEqual(stages["build_plan"]["status"], "skipped")
        self.assertEqual(stages["auto_execute"]["status"], "skipped")
        self.assertEqual(stages["quality"]["status"], "pending")

    def test_alert_of_other_date_is_ignored(self):
        self.enable_auto()
        self.emit_alert("数据未就绪", "market=SH", date="2026-09-15")
        self.assertEqual(self.stages(self.snapshot())["build_plan"]["status"], "pending")


class PlanExecuteStageTests(PipelineBase):
    def test_plan_and_execute_summaries(self):
        self.enable_auto()
        plan_id = self.seed_auto_plan()
        self.seed_order(plan_id, status="submitted")
        self.seed_order(plan_id, status="submitted", symbol="SH.000001")
        stages = self.stages(self.snapshot())
        self.assertEqual(stages["plan"]["status"], "ok")
        self.assertEqual(stages["plan"]["at"], "2026-09-15 16:20:00")
        self.assertIn(plan_id, stages["plan"]["summary"])
        self.assertIn("frozen", stages["plan"]["summary"])
        self.assertIn("2 单", stages["plan"]["summary"])
        self.assertEqual(stages["execute"]["status"], "ok")
        self.assertEqual(stages["execute"]["summary"], "submitted 2")

    def test_cancelled_auto_plan_is_skipped(self):
        self.enable_auto()
        self.seed_auto_plan(status="cancelled")
        stages = self.stages(self.snapshot())
        self.assertEqual(stages["plan"]["status"], "skipped")
        self.assertIn("cancelled", stages["plan"]["summary"])
        self.assertEqual(stages["execute"]["status"], "pending")
        self.assertEqual(stages["execute"]["summary"], "无订单")

    def test_manual_plan_is_not_pipeline_plan(self):
        """流程页的 plan 阶段只看 auto 计划；手工计划归「计划」页，不在此冒充。"""
        self.enable_auto()
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
            "status,created_at) VALUES('P-MANUAL','2026-09-15','sim','rsi','{}','hm',"
            "'frozen','2026-09-15 17:00:00')")
        self.conn.commit()
        stages = self.stages(self.snapshot())
        self.assertEqual(stages["plan"]["status"], "pending")
        self.assertEqual(stages["plan"]["summary"], "无自动计划")

    def test_latest_auto_plan_wins(self):
        self.enable_auto()
        self.seed_auto_plan(as_of="2026-09-14", plan_id="PLN-20260914-SIM-OLD1")
        self.seed_auto_plan(as_of="2026-09-15", plan_id="PLN-20260915-SIM-NEW1")
        stages = self.stages(self.snapshot())
        self.assertIn("PLN-20260915-SIM-NEW1", stages["plan"]["summary"])


class GlobalStageTests(PipelineBase):
    def test_reconcile_and_digest_stages(self):
        self.enable_auto()
        self.mark_ran("GLOBAL", "reconcile", at="2026-09-16 19:00:02")
        store.kv_set(self.conn, "daily:digest",
                     {"as_of": DATE, "orders": {"submitted": 2, "filled": 1}})
        out = self.snapshot()
        stages = out["global"]["stages"]
        self.assertEqual(list(stages), ["reconcile", "digest"])
        self.assertEqual(stages["reconcile"]["status"], "ok")
        self.assertEqual(stages["reconcile"]["scheduled"], "19:00")
        self.assertEqual(stages["digest"]["status"], "ok")
        self.assertEqual(stages["digest"]["summary"], "filled 1, submitted 2")

    def test_digest_of_other_date_is_pending(self):
        self.enable_auto()
        store.kv_set(self.conn, "daily:digest", {"as_of": "2026-09-15", "orders": {}})
        self.assertEqual(self.snapshot()["global"]["stages"]["digest"]["status"], "pending")

    def test_disabled_config_has_digest_only(self):
        out = self.snapshot()
        self.assertEqual(list(out["global"]["stages"]), ["digest"])


class ConfigAndSafetyTests(PipelineBase):
    def test_disabled_summary_fields(self):
        out = self.snapshot()
        cfg = out["auto_pipeline"]
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["exec_at"]["SH"], "09:35")
        self.assertIn("exec_window_minutes", cfg)
        self.assertIn("reconcile_at", cfg)

    def test_invalid_config_reports_error_without_failing(self):
        self.write_platform({"auto_pipeline": {"enabled": "yes"}})
        out = self.snapshot()
        self.assertFalse(out["auto_pipeline"]["enabled"])
        self.assertIn("enabled", out["auto_pipeline"]["error"])
        # 装配层 fail-soft：阶段表退回基础链，端点不失败
        self.assertIn("quality", self.stages(out))

    def test_kill_and_halt_are_reported(self):
        from trading_core import daemon
        daemon.kill_path(self.home).touch()
        store.set_halt(self.conn, True, reason="daily_loss")
        out = self.snapshot()
        self.assertTrue(out["kill"])
        self.assertEqual(out["halt"], {"halted": True, "halt_reason": "daily_loss"})

    def test_snapshot_is_read_only(self):
        """只读反证：库行数与 kv 完全不变；非法配置也不写告警（build_jobs 不传 conn）。"""
        self.write_platform({"auto_pipeline": {"enabled": "yes"}})
        before_alerts = self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        before_plans = self.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
        before_kv = self.conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0]
        out = self.snapshot()
        self.assertIn("error", out["auto_pipeline"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],
                         before_alerts)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0],
                         before_plans)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM kv").fetchone()[0], before_kv)

    def test_alerts_listing_respects_limit(self):
        for index in range(3):
            self.emit_alert("缺口", f"缺 {index} 日")
        out = self.snapshot(alert_limit=2)
        self.assertEqual(len(out["alerts"]), 2)
        # 告警按 id 倒序：最近一条在前
        self.assertEqual(out["alerts"][0]["detail"], "缺 2 日")


class ReviewFixTests(PipelineBase):
    """WP10 代码质量审查（基线 b62e1b7）的修复回归：K1 / I1 / I2 / I3。

    每条用例先复现审查者实证的错态，再由修复转绿——不是对实现的同义改写。
    """

    # ---- K1：GLOBAL 链不做市场消歧（critical 对账差异必须可见） ----
    def test_global_reconcile_critical_diff_is_failed(self):
        """对账差异 detail 里是差异标的（SH.600519/HK.00700），不是归属市场。

        修复前：``_applies(alert, "GLOBAL")`` 因 detail 含 ``SH.`` 判为「不属于 GLOBAL」
        → reconcile 停在 pending，页面上对账差异完全不可见（与 ``_is_failure`` 的
        「critical 一律 failed」注释直接矛盾）。
        """
        self.enable_auto()
        self.emit_alert("对账差异", "2 条差异（2026-09-16）：SH.600519,HK.00700",
                        level="critical")
        stages = self.snapshot()["global"]["stages"]
        self.assertEqual(stages["reconcile"]["status"], "failed")
        self.assertEqual(stages["reconcile"]["summary"], "对账差异")

    def test_global_reconcile_no_diff_still_ok(self):
        """反向钉子：无差异的 info 告警仍按表口径判 ok（GLOBAL 分支不把一切告警当失败）。"""
        self.enable_auto()
        self.emit_alert("对账无差异", "2026-09-16 订单/持仓与券商一致", level="info")
        self.assertEqual(self.snapshot()["global"]["stages"]["reconcile"]["status"], "ok")

    # ---- I1：市场链层告警不误伤 GLOBAL 链 ----
    def test_chain_alert_does_not_touch_global_stage(self):
        """「日历未同步」作用于市场链数据作业，但对账不依赖交易日历。

        修复前该告警（detail 无市场标记 → owner None）作用于**所有**市场**以及** GLOBAL
        链，于是 GLOBAL reconcile 显示 ``skipped | 日历未同步``——把别处故障当本阶段原因。
        """
        self.enable_auto()
        self.emit_alert("日历未同步", "日历未同步：SH（交易日历未同步）", level="warn")
        out = self.snapshot()
        reconcile = out["global"]["stages"]["reconcile"]
        self.assertEqual(reconcile["status"], "pending")
        self.assertEqual(reconcile["summary"], "")
        # 既有市场链行为不变：同一告警仍让该市场数据作业显示跳过
        self.assertEqual(self.stages(out)["sync_bars"]["status"], "skipped")
        self.assertEqual(self.stages(out)["sync_bars"]["summary"], "日历未同步")

    # ---- I2：执行阶段按结局派生状态（整批被拒不是绿色「已完成」） ----
    def test_execute_stage_all_rejected_is_failed(self):
        """自动链下风控/券商拒整批是可达路径——此时显示绿色与「闭环是否正常」相悖。"""
        self.enable_auto()
        plan_id = self.seed_auto_plan()
        self.seed_order(plan_id, status="rejected")
        stages = self.stages(self.snapshot())
        self.assertEqual(stages["execute"]["status"], "failed")
        self.assertEqual(stages["execute"]["summary"], "rejected 1")  # 摘要保持事实口径

    def test_execute_stage_all_cancelled_is_failed(self):
        self.enable_auto()
        plan_id = self.seed_auto_plan()
        self.seed_order(plan_id, status="cancelled")
        stages = self.stages(self.snapshot())
        self.assertEqual(stages["execute"]["status"], "failed")
        self.assertEqual(stages["execute"]["summary"], "cancelled 1")

    def test_execute_stage_one_reached_keeps_ok(self):
        """部分到达即算执行发生（unknown 代表可能已触达券商，不判失败）。"""
        self.enable_auto()
        plan_id = self.seed_auto_plan()
        self.seed_order(plan_id, status="cancelled")
        self.seed_order(plan_id, status="submitted", symbol="SH.000001")
        stages = self.stages(self.snapshot())
        self.assertEqual(stages["execute"]["status"], "ok")
        self.assertEqual(stages["execute"]["summary"], "cancelled 1, submitted 1")

    def test_execute_stage_unknown_only_is_ok(self):
        self.enable_auto()
        plan_id = self.seed_auto_plan()
        self.seed_order(plan_id, status="unknown")
        self.assertEqual(self.stages(self.snapshot())["execute"]["status"], "ok")

    # ---- I3：坏配置文件不得伪装成「功能关闭」 ----
    def test_unparsable_config_is_reported(self):
        """JSON 解析失败走 ``platform_config`` 容错（按空配置跑，不阻塞调度）——但摘要
        必须能区分「配置坏了」与「功能没开」：补 ``config_error``，并保留默认值字段。"""
        (self.home / "trading-platform.json").write_text("{ not json", encoding="utf-8")
        out = self.snapshot()["auto_pipeline"]
        self.assertEqual(out["config_error"], "配置文件无法解析")
        self.assertFalse(out["enabled"])          # 默认值字段保留（摘要仍完整可渲染）
        self.assertIn("exec_at", out)
        self.assertIn("exec_window_minutes", out)
        self.assertNotIn("error", out)            # 与语义非法（error）分开报

    def test_unparsable_config_keeps_semantic_error_priority(self):
        """文件能解析但语义非法 → 仍走 ``error``（两条路径不互相覆盖）。"""
        self.write_platform({"auto_pipeline": {"enabled": "yes"}})
        out = self.snapshot()["auto_pipeline"]
        self.assertIn("error", out)
        self.assertNotIn("config_error", out)

    def test_missing_config_has_no_config_error(self):
        """文件不存在 = 功能未启用（默认态），不是配置错误。"""
        self.assertNotIn("config_error", self.snapshot()["auto_pipeline"])


class PipelineEndpointTests(PipelineBase):
    """服务侧接线：空载荷校验 + envelope 契约（core 桥注入替身，离线）。"""

    def handler(self, value=None):
        payload = value if value is not None else {"date": DATE, "markets": {},
                                                   "global": {"stages": {}},
                                                   "auto_pipeline": {"enabled": False}}
        return app_module.create_handler(
            str(self.home), core={"snapshot-pipeline": lambda: payload})

    def test_pipeline_endpoint_envelope(self):
        body = self.handler()("pipeline", {})
        self.assertTrue(body["ok"])
        self.assertEqual(body["value"]["date"], DATE)
        self.assertFalse(body["value"]["auto_pipeline"]["enabled"])

    def test_pipeline_rejects_payload(self):
        body = self.handler()("pipeline", {"_refresh": True, "extra": 1})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("pipeline", body["error"]["message"])

    def test_missing_core_bridge_reports_unavailable(self):
        handle = app_module.create_handler(str(self.home), core={})
        body = handle("pipeline", {})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertIn("Core bridge unavailable", body["error"]["message"])


class SubprocessBridgeTests(PipelineBase):
    """真实子进程桥（不经任何假件）：证明新子命令在服务调用口径下真的可用。

    执行期发现：venv 的 ``dsh-trading-python.pth`` 指向安装器解出的**副本**，而
    ``subprocess`` 起的子进程不继承父进程 sys.path——仓库内运行时会解析到旧副本，
    新子命令报 ``invalid choice``（与 commit 72bb86e 修的是同一类问题，只是跨进程）。
    ``compute._subprocess_env`` 把仓库数据层经 PYTHONPATH 前置后修复。
    """

    def test_subprocess_env_prefers_repo_data_layer(self):
        env = compute._subprocess_env()
        self.assertIsNotNone(env, "仓库存在时必须给出 PYTHONPATH")
        parts = env["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(parts[0], str(ROOT / "plugins" / "datasource" / "python"))
        self.assertEqual(parts[1], str(ROOT / "plugins" / "core" / "python"))

    def test_subprocess_env_preserves_existing_pythonpath(self):
        with unittest.mock.patch.dict(os.environ, {"PYTHONPATH": "/custom/path"}):
            parts = compute._subprocess_env()["PYTHONPATH"].split(os.pathsep)
        self.assertIn("/custom/path", parts)
        self.assertEqual(parts[-1], "/custom/path")

    def test_snapshot_pipeline_runs_through_compute_bridge(self):
        with unittest.mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)}):
            value = compute.snapshot_cli("snapshot-pipeline")
        self.assertTrue({"date", "markets", "global", "auto_pipeline"} <= set(value))
        self.assertEqual(list(value["markets"]), list(pipeline.MARKETS))
        self.assertIn("stages", value["markets"]["SH"])
        self.assertIn("quality", value["markets"]["SH"]["stages"])
        self.assertIn("digest", value["global"]["stages"])
        self.assertFalse(value["auto_pipeline"]["enabled"])


if __name__ == "__main__":
    unittest.main()
