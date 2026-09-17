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
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from server import trading as server_trading  # noqa: E402
from trading_core import broker, cli, planner, strategies, store  # noqa: E402
from trading_datasource.market_ids import (  # noqa: E402
    OPENAPI_ENABLE_MARKET as CANON_OPENAPI_ENABLE_MARKET,
    SIM_MARKET_IDS)

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
        return ["SH.600519", "SZ.300750", "HK.00700", "US.AAPL"]

    def target_weights(self, conn, as_of, home=None):
        return {"SH.600519": 0.5, "SZ.300750": 0.3, "HK.00700": 0.2, "US.AAPL": 0.4}


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

    def _bars(self, symbol, days=None, close=100.0):
        """铺日线：默认最后 20 个自然日（末日 = TODAY，过数据就绪门）。

        每根 h=c+0.5 / l=c−0.5 / o=c → 每根 TR=1.0 → ATR(14)=1.0 → 止损距离=2.0 →
        风险预算 = floor(1e6 × 1% ÷ 2.0 ÷ 100) × 100 = 5000 股：本文件期望的
        5000/1500 由**权重臂**约束（与定量修复前的期望一致，见规格 §4.2 第 5 点）。
        显式传 days 时按传入日期铺（落后日期用于数据门用例）。
        """
        if days is None:
            end = date.fromisoformat(TODAY)
            days = [(end - timedelta(days=19 - i)).isoformat() for i in range(20)]
        store.upsert_bars(self.conn, symbol, "1d", [
            {"t": d, "o": close, "h": close + 0.5, "l": close - 0.5, "c": close,
             "v": 1000.0} for d in days], source="test")

    def _bars_ending(self, symbol, end, count=20, close=100.0):
        """铺到指定日期为止的日线（美股链用：最后 bar = 会话本地日）。"""
        days = [(date.fromisoformat(end) - timedelta(days=count - 1 - i)).isoformat()
                for i in range(count)]
        self._bars(symbol, days=days, close=close)

    def _mode(self, value):
        (self.home / "trading-account-mode").write_text(value, encoding="utf-8")

    def _broker(self, positions=None, equity=1_000_000.0, market_id=3):
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append((name, args))
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": market_id,
                                      "account_title": "模拟账户"}]}
            if name == "sim_trade_position_list":
                return {"positions": positions or []}
            if name == "sim_trade_cash_info":
                return {"balance": equity, "total_asset": equity}
            raise AssertionError(f"意外工具 {name}")

        return call, calls

    def _live_broker(self, positions=None, equity="1000000.00", missing_equity=False,
                     fail=None, cash="1000000.00"):
        """live 假通道：账户（enable_market[4]=A股）/ 持仓（code 前缀）/ 资金。

        资金响应按**官方 get-funds.md 口径**给足字段：``total_assets``（权益）与
        ``power``（最大购买力 = 可用现金；2026-09-17 现金封顶修订）。此前假件只给权益，
        新语义下「现金不可得 → 不生成买单」会把该用例打成零订单——那是假件不完备，
        不是语义回归。``cash=None`` 用于构造「现金字段缺失」场景。
        """
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
                if missing_equity:
                    return {}
                return {"total_assets": equity, "power": cash} if cash is not None \
                    else {"total_assets": equity}
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

    def test_strategy_receives_market_and_named_pool(self):
        """K1/I1 接线证明：plan_auto 把 market 与配置池键传给接受它们的策略。

        策略层的「按市场算分母/截断」由 test_core_strategies 反证；这里只证明
        编排侧没有把这两个参数丢掉（旧实现只传 home，市场维度无从生效）。
        """
        calls = []

        class _Recording:
            id = "wp9_recording"

            def universe(self, conn, as_of, home=None, market=None, watchlist=None):
                calls.append(("universe", market, watchlist))
                return []

            def target_weights(self, conn, as_of, home=None, market=None, watchlist=None):
                calls.append(("target_weights", market, watchlist))
                return {"SH.600519": 0.5}

        strategies.REGISTRY["wp9_recording"] = _Recording()
        self.addCleanup(strategies.REGISTRY.pop, "wp9_recording", None)
        self._config(strategies_cfg=[{"market": "SH", "strategy": "wp9_recording",
                                      "watchlist": "sh_pool"}],
                     watchlist=["SH.600519"])
        self._calendar()
        self._bars("SH.600519")
        call, _ = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"], result)
        self.assertIn(("target_weights", "SH", "sh_pool"), calls)
        self.assertIn(("universe", "SH", "sh_pool"), calls)

    def test_missing_named_pool_skips_with_warn(self):
        """I1：显式池键不存在 → 软跳过 + warn（fail-closed，不静默换池子）。"""
        self._config(strategies_cfg=[{"market": "SH", "strategy": "watchlist_rsi",
                                      "watchlist": "nope_pool"}],
                     watchlist=["SH.600519"])
        self._calendar()
        self._bars("SH.600519")
        call, _ = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)

        self.assertTrue(result["ok"])
        self.assertIn("关注池键不存在", result["skipped"])
        self.assertEqual(self._plan_rows(), [])
        self.assertTrue(any("策略权重失败" in a["title"] for a in self._alerts("warn")),
                        self._alerts("warn"))

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
        """模拟账户 market_id 口径：core 与 platform 引用**同一个规范对象**
        （WP13 审查 M1 收敛镜像——不再用等值断言维护重复）。"""
        self.assertIs(broker.MARKET_IDS, SIM_MARKET_IDS)
        self.assertIs(server_trading.PREFIX_MARKET_ID, broker.MARKET_IDS)

    def test_openapi_enable_market_matches_platform(self):
        """live 账户挑选用 enable_market（另一套数字口径）：同为规范对象。"""
        self.assertIs(broker.OPENAPI_ENABLE_MARKET, server_trading.OPENAPI_ENABLE_MARKET)
        self.assertIs(server_trading.OPENAPI_ENABLE_MARKET, CANON_OPENAPI_ENABLE_MARKET)

    def test_chain_prefixes_cover_calendar_markets(self):
        """持仓过滤前缀集合与日历市场映射同源（SH 链含 SZ/BJ）。"""
        self.assertEqual({p for prefixes in broker.CHAIN_PREFIXES.values() for p in prefixes},
                         set(planner.CALENDAR_MARKET))

    # ---- ①b 日期空间：美股链（修复 F1：会话收盘落在北京次日） ----

    def test_us_chain_ready_after_session_close(self):
        """美股链就绪：北京 D 05:40 负责 ET D−1 会话，数据门按**本地日**判定。

        修复前把北京日当本地日用 → expected 超前一天 → 每天静默跳过
        （数据未就绪）→ **美股自动计划永不生成**。
        """
        self._config(strategies_cfg=[{"market": "US", "strategy": "wp9_fake",
                                      "watchlist": "US"}],
                     watchlist=["US.AAPL"])
        self._calendar("US", days=(PREV, TODAY))
        self._bars_ending("US.AAPL", PREV)          # 最后 bar = ET D−1（真实落库口径）
        call, _ = self._broker(market_id=100)       # 美股模拟账户 market_id=100

        result = planner.plan_auto(self.conn, str(self.home), "US",
                                   today=f"{TODAY} 05:40:00", broker_call=call)

        self.assertTrue(result["ok"], result)
        self.assertNotIn("skipped", result)
        rows = self._plan_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market"], "US")
        self.assertEqual(rows[0]["origin"], "auto")
        self.assertEqual(rows[0]["as_of"], PREV)    # 本地会话日，不是北京日 TODAY
        self.assertEqual(len(store.get_orders_by_plan(self.conn, rows[0]["plan_id"])), 1)

    def test_us_chain_before_session_close_skips(self):
        """边界：北京 03:00（负责的会话尚未收盘）→ 保守跳过、零计划。

        不拿上一场的收盘当本场：否则当日 kv ran 被消耗，真到收盘后不再补跑。
        """
        self._config(strategies_cfg=[{"market": "US", "strategy": "wp9_fake",
                                      "watchlist": "US"}],
                     watchlist=["US.AAPL"])
        self._calendar("US", days=(PREV, TODAY))
        self._bars_ending("US.AAPL", PREV)
        call, _ = self._broker(market_id=100)

        result = planner.plan_auto(self.conn, str(self.home), "US",
                                   today=f"{TODAY} 03:00:00", broker_call=call)

        self.assertTrue(result["ok"])
        self.assertIn("尚未收盘", result["skipped"])
        self.assertEqual(self._plan_rows(), [])

    def test_us_without_calendar_fails_closed(self):
        """美股日历未同步 → warn 跳过 + 零计划（不猜日期，不照北京日跑）。"""
        self._config(strategies_cfg=[{"market": "US", "strategy": "wp9_fake",
                                      "watchlist": "US"}],
                     watchlist=["US.AAPL"])
        self._bars_ending("US.AAPL", PREV)
        call, _ = self._broker(market_id=100)

        result = planner.plan_auto(self.conn, str(self.home), "US",
                                   today=f"{TODAY} 05:40:00", broker_call=call)

        self.assertTrue(result["ok"])
        self.assertIn("日历未同步", result["skipped"])
        self.assertEqual(self._plan_rows(), [])
        self.assertTrue(any(a["level"] == "warn" for a in self._alerts()))

    def test_sh_hk_gate_unchanged_by_date_space_fix(self):
        """回归：沪深/港股与北京同日 → 行为逐字不变（as_of 仍为当日）。

        修复只应改变美股链的日期空间：同日市场（收盘落在北京当日）必须原样通过。
        """
        self._config()
        self._calendar()
        self._bars("SH.600519")
        self._bars("SZ.300750")
        call, _ = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=TODAY, broker_call=call)
        self.assertTrue(result["ok"], result)
        self.assertNotIn("skipped", result)

        self._config(strategies_cfg=[{"market": "HK", "strategy": "wp9_fake",
                                      "watchlist": "HK"}],
                     watchlist=["HK.00700"])
        self._calendar("HK", days=(PREV, TODAY))
        self._bars("HK.00700")
        hk_call, _ = self._broker(market_id=1)

        hk = planner.plan_auto(self.conn, str(self.home), "HK",
                               today=f"{TODAY} 16:40:00", broker_call=hk_call)

        self.assertTrue(hk["ok"], hk)
        self.assertNotIn("skipped", hk)
        self.assertEqual([r["as_of"] for r in self._plan_rows()], [TODAY, TODAY])

    def test_invalid_today_injection_fails_closed(self):
        """非法 ``--today`` 注入（日期合法、时分非法）→ fail-closed，不抛穿契约。

        ``today`` 注入不走 ``daemon.now_stamp`` 的格式校验，因此由 ``data_date_for``
        兜底：返回 ``ok=False`` 让 CLI 非零退出，而不是把 ValueError 抛给调用方
        （作业契约「永不抛」）。
        """
        self._config()
        self._calendar()
        call, _ = self._broker()

        result = planner.plan_auto(self.conn, str(self.home), "SH",
                                   today=f"{TODAY} 25:00:00", broker_call=call)

        self.assertFalse(result["ok"])
        self.assertIn("YYYY-MM-DD", result["error"])
        self.assertEqual(self._plan_rows(), [])

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
