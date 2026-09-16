"""WP9 任务 4：plan-auto 作业（build_plan）——策略驱动计划生成 + 数据就绪门 + 过期语义。

全部离线：券商通道注入假 call，日期显式注入，日历与 bars 直接种子。
覆盖计划任务 4 的四条与边界：
  * ① 数据就绪门：某标的最后 bar 落后最近交易日 → 跳过 + warn 告警 + **零计划**；
  * ② 就绪 → 策略权重按市场切片 → build_and_freeze(origin=auto, market=SH) 落库；
  * ③ 过期语义：昨日 frozen auto → cancelled；手工 frozen 计划不动；
  * ④ planner 既有调用（不传 origin/market）→ 默认 manual/NULL 回归；
  * 边界：配置关闭静默 / 无匹配策略 / 日历缺失 / 券商通道失败 / live 权益不可用。
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from server import trading as server_trading  # noqa: E402
from trading_core import broker, cli, planner, strategies, store  # noqa: E402

TODAY = "2026-09-16"
PREV = "2026-09-15"
WATCHLIST = ["SH.600519", "SZ.300750", "HK.00700"]


class _FakeStrategy:
    """测试替身：固定权重。

    关注池过滤/RSI 信号判定由 test_core_strategies 覆盖；本文件只验证 plan-auto 的
    编排（数据门/切片/持久化/过期），因此策略必须是确定值而不是真信号。
    返回的权重刻意含港股，用来断言「按 market 切片」。
    """

    id = "wp9_fake"

    def universe(self, conn, as_of, home=None):
        return ["SH.600519", "SZ.300750", "HK.00700"]

    def target_weights(self, conn, as_of, home=None):
        return {"SH.600519": 0.5, "SZ.300750": 0.3, "HK.00700": 0.2}


class PlanAutoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.conn = store.connect(str(self.home / "t.sqlite"))
        self.addCleanup(self.conn.close)
        strategies.REGISTRY["wp9_fake"] = _FakeStrategy()
        self.addCleanup(strategies.REGISTRY.pop, "wp9_fake", None)

    # ---- 种子工具 ----

    def _config(self, enabled=True, strategies_cfg=None, watchlist=None):
        cfg = {"watchlist": WATCHLIST if watchlist is None else watchlist,
               "auto_pipeline": {
                   "enabled": enabled,
                   "strategies": strategies_cfg if strategies_cfg is not None else [
                       {"market": "SH", "strategy": "wp9_fake", "watchlist": "SH"}],
                   "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
                   "reconcile_at": "19:00"}}
        (self.home / "trading-platform.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def _calendar(self, market="SH", days=(PREV, TODAY)):
        store.upsert_calendar(self.conn, market, [
            {"day": d, "trade_date_type": "WHOLE", "trade_second": 14400} for d in days])

    def _bars(self, symbol, days=(PREV, TODAY), close=100.0):
        store.upsert_bars(self.conn, symbol, "1d", [
            {"t": d, "o": close, "h": close, "l": close, "c": close, "v": 1000.0}
            for d in days], source="test")

    def _mode(self, value):
        (self.home / "trading-account-mode").write_text(value, encoding="utf-8")

    def _broker(self, positions=None, equity=1_000_000.0):
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append((name, args))
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3,
                                      "account_title": "模拟A股"}]}
            if name == "sim_trade_position_list":
                return {"positions": positions or []}
            if name == "sim_trade_cash_info":
                return {"balance": equity, "total_asset": equity}
            raise AssertionError(f"意外工具 {name}")

        return call, calls

    def _live_broker(self, positions=None, equity="1000000.00", missing_equity=False,
                     fail=None):
        """live 假通道：账户（enable_market[4]=A股）/ 持仓（code 前缀）/ 资金（total_assets）。"""
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append((name, args))
            if fail and name == fail:
                raise RuntimeError(f"{name} 通道断线")
            if name == "account_authorized_trd_accs":
                return {"accounts": [{"account_id": "LIVE-1", "enable_market": [4],
                                      "acc_type": "margin"}]}
            if name == "account_positions":
                return {"positions": positions if positions is not None else [
                    {"code": "SH.600519", "qty": 400},
                    {"code": "US.AAPL", "qty": 10}]}  # 非本市场持仓须被过滤
            if name == "account_funds":
                return {} if missing_equity else {"total_assets": equity}
            raise AssertionError(f"意外工具 {name}")

        return call, calls

    def _alerts(self, level=None):
        rows = self.conn.execute(
            "SELECT level, title, detail FROM alerts ORDER BY id").fetchall()
        return [dict(r) for r in rows if level is None or r["level"] == level]

    def _plan_rows(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT plan_id, as_of, mode, status, origin, market, target FROM plans"
            " ORDER BY created_at, rowid").fetchall()]

    # ---- ① 数据就绪门 ----

    def test_stale_data_skips_with_warn_and_no_plan(self):
        """关注池任一标的落后最近交易日 → 跳过 + warn + 零计划（宁缺毋假）。"""
        self._config()
        self._calendar()
        self._bars("SH.600519", days=(PREV,))          # 落后：最后 bar = PREV
        self._bars("SZ.300750", days=(PREV, TODAY))
        call, _ = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"])
        self.assertIn("数据未就绪", result["skipped"])
        self.assertIn("SH.600519", result["skipped"])
        self.assertEqual(self._plan_rows(), [])
        warns = self._alerts("warn")
        self.assertTrue(any("数据未就绪" in a["title"] or "数据未就绪" in a["detail"]
                            for a in warns), warns)

    # ---- ② 就绪生成 auto 计划 ----

    def test_ready_creates_auto_plan_for_market_only(self):
        self._config()
        self._calendar()
        self._bars("SH.600519", close=100.0)
        self._bars("SZ.300750", close=200.0)
        call, calls = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"], result)
        plan = result["plan"]
        self.assertEqual(plan["status"], "frozen")
        # 港股被 market 切片排除（港股属 HK 链，不在 SH 作业里下单）
        self.assertEqual([o["symbol"] for o in plan["orders"]], ["SH.600519", "SZ.300750"])
        # 整手与权益口径：1_000_000 × 0.5 / 100 = 5000 股；× 0.3 / 200 = 1500 股
        self.assertEqual({o["symbol"]: o["qty"] for o in plan["orders"]},
                         {"SH.600519": 5000, "SZ.300750": 1500})
        rows = self._plan_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["origin"], "auto")
        self.assertEqual(rows[0]["market"], "SH")
        self.assertEqual(rows[0]["as_of"], TODAY)
        self.assertEqual(rows[0]["status"], "frozen")
        self.assertEqual(json.loads(rows[0]["target"]), {"SH.600519": 0.5, "SZ.300750": 0.3})
        # 券商查询走了既有 sim 通道（账户→持仓→资金），且带 market_id
        names = [name for name, _ in calls]
        self.assertIn("sim_trade_position_list", names)
        self.assertEqual(
            [args["market"] for name, args in calls if name == "sim_trade_position_list"], [3])
        self.assertNotIn("warn", [a["level"] for a in self._alerts()])
        # 冻结即登记：订单进 OMS（execute.run 的唯一取单来源）
        order_rows = store.get_orders_by_plan(self.conn, plan["plan_id"])
        self.assertEqual(len(order_rows), len(plan["orders"]))
        self.assertEqual({r["symbol"] for r in order_rows}, {"SH.600519", "SZ.300750"})
        self.assertEqual({r["status"] for r in order_rows}, {"draft"})

    # ---- ③ 过期语义 ----

    def test_stale_auto_expired_manual_untouched(self):
        self._config()
        self._calendar()
        self._bars("SH.600519")
        self._bars("SZ.300750")
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,status,"
            "created_at,origin,market) VALUES('PLN-OLD',?, 'sim','wp9_fake','{}','h-old',"
            "'frozen','2026-09-15 16:20:00','auto','SH')", (PREV,))
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,status,"
            "created_at,origin,market) VALUES('PLN-MANUAL',?, 'sim','wp9_fake','{}','h-man',"
            "'frozen','2026-09-15 16:20:00','manual',NULL)", (PREV,))
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,status,"
            "created_at,origin,market) VALUES('PLN-EXEC',?, 'sim','wp9_fake','{}','h-exec',"
            "'executing','2026-09-15 16:20:00','auto','SH')", (PREV,))
        self.conn.commit()
        call, _ = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertEqual(result["expired"], ["PLN-OLD"])
        status = {r["plan_id"]: r["status"] for r in self._plan_rows()}
        self.assertEqual(status["PLN-OLD"], "cancelled")
        self.assertEqual(status["PLN-MANUAL"], "frozen")   # 手工计划不动
        self.assertEqual(status["PLN-EXEC"], "executing")  # 非 frozen 不动
        self.assertEqual(len(self._plan_rows()), 4)        # 新增当日计划

    # ---- ④ planner 既有调用回归 ----

    def test_planner_default_call_stays_manual(self):
        def broker_positions(mode):
            return {}, 1_000_000.0

        plan = planner.build_and_freeze(
            self.conn, mode="sim", strategy_id="wp9_fake",
            target={"SH.600519": 0.5}, broker_positions=broker_positions,
            prices={"SH.600519": 100.0}, as_of=TODAY)
        row = self.conn.execute("SELECT origin, market FROM plans WHERE plan_id=?",
                                (plan["plan_id"],)).fetchone()
        self.assertEqual(row["origin"], "manual")
        self.assertIsNone(row["market"])

    # ---- 边界 ----

    def test_disabled_config_skips_silently(self):
        self._config(enabled=False)
        self._calendar()
        self._bars("SH.600519")
        call, calls = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"])
        self.assertIn("未启用", result["skipped"])
        self.assertEqual(self._plan_rows(), [])
        self.assertEqual(calls, [])          # 关闭即零券商调用
        self.assertEqual(self._alerts(), [])  # 静默：不告警

    def test_no_strategy_for_market_skips(self):
        self._config(strategies_cfg=[{"market": "HK", "strategy": "wp9_fake",
                                      "watchlist": "HK"}])
        self._calendar()
        self._bars("SH.600519")
        call, _ = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)
        self.assertTrue(result["ok"])
        self.assertIn("无匹配策略", result["skipped"])
        self.assertEqual(self._plan_rows(), [])

    def test_calendar_missing_skips_with_warn(self):
        self._config()
        self._bars("SH.600519")
        call, _ = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)
        self.assertTrue(result["ok"])
        self.assertIn("日历未同步", result["skipped"])
        self.assertEqual(self._plan_rows(), [])
        self.assertTrue(self._alerts("warn"), "日历缺失必须告警")

    def test_broker_failure_skips_without_plan(self):
        """券商通道失败 → 跳过 + warn，**不用本地台账、不编造权益**。"""
        self._config()
        self._calendar()
        self._bars("SH.600519")
        self._bars("SZ.300750")

        def failing_call(name, args=None, timeout=30, **kwargs):
            raise RuntimeError("通道断线")

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=failing_call)
        self.assertTrue(result["ok"])
        self.assertIn("券商通道不可用", result["skipped"])
        self.assertEqual(self._plan_rows(), [])
        self.assertTrue(self._alerts("warn"), "券商失败必须告警")

    def test_live_mode_generates_plan_for_human_execution(self):
        """live 也自动生成计划（规格 §4.2 双模式都生成）；执行仍全人工——本路径零写操作。"""
        self._config()
        self._calendar()
        self._bars("SH.600519", close=100.0)
        self._bars("SZ.300750", close=200.0)
        self._mode("live")
        call, calls = self._live_broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"], result)
        plan = result["plan"]
        self.assertEqual(plan["mode"], "live")
        self.assertEqual([o["symbol"] for o in plan["orders"]], ["SH.600519", "SZ.300750"])
        rows = self._plan_rows()
        self.assertEqual([(r["origin"], r["market"], r["mode"], r["status"]) for r in rows],
                         [("auto", "SH", "live", "frozen")])
        # 订单已登记进 OMS；live 的执行仍由工作台口令 + Web 确认卡片把守（本作业只读+冻结）
        order_rows = store.get_orders_by_plan(self.conn, plan["plan_id"])
        self.assertEqual(len(order_rows), len(plan["orders"]))
        self.assertEqual({r["mode"] for r in order_rows}, {"live"})
        names = [name for name, _ in calls]
        self.assertEqual(names.count("account_authorized_trd_accs"), 1)
        self.assertIn("account_positions", names)
        self.assertIn("account_funds", names)
        self.assertNotIn("warn", [a["level"] for a in self._alerts()])

    def test_live_equity_missing_skips_with_warn(self):
        """官方权益字段缺失 → 跳过 + warn + 零计划（绝不编造权益）。"""
        self._config()
        self._calendar()
        self._bars("SH.600519")
        self._bars("SZ.300750")
        self._mode("live")
        call, _ = self._live_broker(missing_equity=True)

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"])
        self.assertIn("权益不可用", result["skipped"])
        self.assertEqual(self._plan_rows(), [])
        self.assertTrue(self._alerts("warn"), "权益缺失必须告警")

    def test_live_account_query_failure_skips_with_warn(self):
        """live 账户查询失败 → 跳过 + warn（不用本地台账兜底）。"""
        self._config()
        self._calendar()
        self._bars("SH.600519")
        self._bars("SZ.300750")
        self._mode("live")
        call, _ = self._live_broker(fail="account_positions")

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"])
        self.assertIn("券商通道不可用", result["skipped"])
        self.assertEqual(self._plan_rows(), [])
        self.assertTrue(self._alerts("warn"))

    def test_invalid_config_fails_closed(self):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"auto_pipeline": {"enabled": "yes"}}), encoding="utf-8")
        self._calendar()
        result = planner.plan_auto(self.conn, str(self.home), "SH", today=TODAY)
        self.assertFalse(result["ok"])
        self.assertIn("配置非法", result["error"])

    # ---- 口径锁定：日历市场映射与平台侧同源 ----

    def test_calendar_market_mapping_matches_platform(self):
        """core 不能 import server 包，映射是镜像——漂移必须在此处炸掉。"""
        self.assertEqual(planner.CALENDAR_MARKET, server_trading.CALENDAR_MARKET)

    def test_market_ids_mapping_matches_platform(self):
        """模拟账户 market_id 口径同为镜像：港股 1 / A股 3 / 美股 100。"""
        self.assertEqual(broker.MARKET_IDS, server_trading.PREFIX_MARKET_ID)

    def test_openapi_enable_market_matches_platform(self):
        """live 账户挑选用 enable_market（另一套数字口径）——镜像漂移必须在此处炸掉。"""
        self.assertEqual(broker.OPENAPI_ENABLE_MARKET, server_trading.OPENAPI_ENABLE_MARKET)

    def test_chain_prefixes_cover_calendar_markets(self):
        """持仓过滤前缀集合与日历市场映射同源（SH 链含 SZ/BJ）。"""
        self.assertEqual({p for prefixes in broker.CHAIN_PREFIXES.values() for p in prefixes},
                         set(planner.CALENDAR_MARKET))

    # ---- CLI 接线 ----

    def test_cli_plan_auto_skips_with_exit_zero(self):
        """功能关闭时 CLI 退出 0 且打印跳过原因（软跳过不是失败）。"""
        self._config(enabled=False)
        self._calendar()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["plan-auto", "--market", "SH", "--home", str(self.home),
                             "--today", TODAY, "--db", str(self.home / "cli.sqlite")])
        self.assertEqual(code, 0)
        self.assertIn("未启用", out.getvalue())

    def test_cli_plan_auto_invalid_config_exits_nonzero(self):
        """配置非法：fail-closed 非零退出 + JSON 原因（调度链能看见）。"""
        (self.home / "trading-platform.json").write_text(
            json.dumps({"auto_pipeline": {"nope": 1}}), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["plan-auto", "--market", "SH", "--home", str(self.home),
                             "--today", TODAY, "--db", str(self.home / "cli.sqlite")])
        self.assertEqual(code, 1)
        self.assertIn("配置非法", out.getvalue())

    def test_cli_rejects_unknown_market(self):
        self._config()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["plan-auto", "--market", "XX", "--home", str(self.home),
                             "--today", TODAY, "--db", str(self.home / "cli.sqlite")])
        self.assertEqual(code, 1)
        self.assertIn("未知市场链", out.getvalue())


if __name__ == "__main__":
    unittest.main()
