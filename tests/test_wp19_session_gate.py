"""WP19 第四组：人工下单**时段闸门**（平台前置校验）与规则 3 文案纠正。

闸门覆盖三条**人工**写入路径：临时下单 ``trade_place``、改单 ``trade_modify``，以及工作台
的 ``plan-execute`` 人工触发（入队前判）。**撤单一律放行**（减少敞口，不新增风险）。

不在本闸门覆盖内的两条路径（刻意，见 ``trading.TradeGate._write`` 与
``plan_session_refusal`` 的 docstring）：
  * 自动执行链 ``autopilot.auto_execute`` → 指令轮询 → ``daemon.handle_command`` →
    ``execute.run``：由守卫 9（执行窗口）+ 风控 8 规则把关，且**不经过**本收口；
  * 平台层的字段校验 / live 写能力 / sim 能力边界等既有本地前置拒绝：顺序不变（本闸门
    排在它们之后，以免改写既有错误码契约）。

全部离线：broker 用计数替身、确认用假件、时钟**注入固定的北京时刻**（闸门必须可注入
时钟，否则用例会随真实运行时刻飘）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from server import app as app_module  # noqa: E402
from server import trading  # noqa: E402
from trading_core import sessions  # noqa: E402
from trading_core import store as core_store  # noqa: E402

# 固定时钟（北京时间）：闸门只看「市场本地时刻」，与真实运行时刻无关。
SH_OPEN = "2026-09-18 10:00:00"      # 沪市连续竞价内
SH_BEFORE = "2026-09-18 09:00:00"    # 开盘集合竞价之前
SH_LUNCH = "2026-09-18 12:15:00"     # 午休（刻意算在窗口内）
SH_AFTER = "2026-09-18 15:30:00"     # 收盘后
HK_HALF_AFTER = "2026-09-18 12:30:00"  # 港股半日市（真实收盘 12:00）之后
US_RTH = "2026-09-18 22:00:00"       # 北京 22:00 = ET 10:00（EDT）→ 常规时段内
US_PRE = "2026-09-18 17:00:00"       # 北京 17:00 = ET 05:00 → 仅盘前
US_CLOSED = "2026-09-18 05:00:00"    # 北京 05:00 = ET 17:00（EDT）→ 常规时段外
SH_ORDER = {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 123.5}
US_ORDER = {"symbol": "US.AAPL", "side": "BUY", "qty": 10, "price": 150.5}
HK_ORDER = {"symbol": "HK.00700", "side": "BUY", "qty": 100, "price": 300.0}

RISK_CONFIG = {"risk_per_trade": 0.01, "max_positions": 5, "max_position_pct": 0.25,
               "daily_loss_limit_pct": 0.03}


def fixed_ctx(**over):
    def builder(conn, mode, order, operation, home, today):
        ctx = {"mode": mode, "kill_path": str(Path(home) / "trading-kill"),
               "equity": 10_000_000.0, "positions_value": {}, "positions_count": 0,
               "day_pnl_pct": 0.0, "is_trading_day": True, "config": dict(RISK_CONFIG),
               "plan_hash": None, "plan_status": "approved"}
        ctx.update(over)
        return ctx
    return builder


class FakeBroker:
    supports_live_write = True

    def __init__(self):
        self.calls = []

    def place(self, order, mode):
        self.calls.append(("place", dict(order)))
        return {"status": "submitted", "broker_order_id": "B-1"}

    def modify(self, order, mode):
        self.calls.append(("modify", dict(order)))
        return {"status": "submitted", "broker_order_id": "B-2"}

    def cancel(self, order, mode):
        self.calls.append(("cancel", dict(order)))
        return {"status": "cancelled", "order_id": order.get("order_id")}


class FakeConfirm:
    def __init__(self):
        self.requests = []

    def request(self, home, tool, mode, args, session_id, ttl_ms):
        self.requests.append({"tool": tool, "args": args})
        return {"decision": "approved", "id": None}

    def view(self, home):
        return None


class GateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "trading-account-mode").write_text("sim\n", encoding="utf-8")
        self.broker = FakeBroker()
        self.confirm = FakeConfirm()

    def gate(self, now=SH_OPEN, **kw):
        kw.setdefault("broker", self.broker)
        kw.setdefault("confirm", self.confirm)
        kw.setdefault("ctx_builder", fixed_ctx())
        kw.setdefault("now", now)
        return trading.TradeGate(str(self.home), **kw)

    def live_gate(self, now=SH_OPEN, **kw):
        """live 模式闸门：``session`` 字段只在 live 可用（sim 通道能力边界在其上游拒绝）。"""
        (self.home / "trading-account-mode").write_text("live\n", encoding="utf-8")
        return self.gate(now=now, **kw)

    def conn(self):
        return core_store.connect(core_store.db_path(str(self.home)))

    def counts(self):
        conn = self.conn()
        try:
            return (conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM risk_checks").fetchone()[0])
        finally:
            conn.close()

    def seed_hk(self, day, trade_second):
        conn = self.conn()
        try:
            core_store.upsert_calendar(conn, "HK", [
                {"day": day, "trade_date_type": "WHOLE", "trade_second": trade_second}])
        finally:
            conn.close()


class OrderSessionGateTest(GateBase):
    """单笔人工写入的时段判定（place / modify；cancel 放行）。"""

    def test_before_open_is_refused(self):
        out = self.gate(now=SH_BEFORE).place(dict(SH_ORDER))
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["error"]["code"], "trading/order-rejected")
        self.assertIn("时段闸门", out["error"]["message"])

    def test_continuous_auction_is_allowed(self):
        out = self.gate(now=SH_OPEN).place(dict(SH_ORDER))
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.broker.calls[0][0], "place")

    def test_lunch_break_is_allowed(self):
        """午休刻意算在窗口内：券商普遍接受午间报单并排队，挡掉即错杀。"""
        out = self.gate(now=SH_LUNCH).place(dict(SH_ORDER))
        self.assertTrue(out["ok"], out)

    def test_after_close_is_refused(self):
        out = self.gate(now=SH_AFTER).place(dict(SH_ORDER))
        self.assertFalse(out["ok"], out)
        self.assertIn("15:00", out["error"]["message"])

    def test_refusal_message_carries_market_time_windows_and_precheck_note(self):
        out = self.gate(now=SH_AFTER).place(dict(SH_ORDER))
        message = out["error"]["message"]
        self.assertIn("SH", message)
        self.assertIn("2026-09-18 15:30", message)
        self.assertIn("09:15", message)
        self.assertIn("15:00", message)
        self.assertIn("平台前置校验", message)
        self.assertIn("券商", message)

    def test_modify_goes_through_the_same_gate(self):
        out = self.gate(now=SH_AFTER).modify({"order_id": "O-1", **SH_ORDER})
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["error"]["code"], "trading/order-rejected")
        self.assertEqual(self.broker.calls, [])

    def test_cancel_is_always_allowed_even_when_every_market_is_closed(self):
        """撤单减少敞口、不新增风险——闸门一律放行（否则闭市时连风险处置都做不了）。"""
        out = self.gate(now=US_CLOSED).cancel({"symbol": "US.AAPL", "order_id": "O-1"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.broker.calls[0][0], "cancel")

    def test_refusal_touches_nothing(self):
        """本地前置拒绝的零上游口径：零券商调用、零确认、不落 OMS/风控行。"""
        out = self.gate(now=SH_BEFORE).place(dict(SH_ORDER, client_order_id="CID-1"))
        self.assertFalse(out["ok"])
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.confirm.requests, [])
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(out["error"]["details"], {})

    def test_live_write_precheck_still_wins_earlier(self):
        """本闸门排在既有本地前置拒绝（2.5 live 写能力）之后：既有错误码契约不变。"""
        (self.home / "trading-account-mode").write_text("live\n", encoding="utf-8")
        out = self.gate(now=SH_BEFORE, broker=trading.FutuBroker()).place(dict(SH_ORDER))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/broker-unavailable")
        self.assertEqual(out["error"]["message"], trading.LIVE_WRITE_UNAVAILABLE)

    def test_split_share_markets_are_not_port_blocked(self):
        """A 股标记的标的（SH/SZ/BJ）共享同一窗口与失败口径。"""
        for market in ("SH", "SZ", "BJ"):
            order = dict(SH_ORDER, symbol=f"{market}.600519")
            with self.subTest(market=market):
                out = self.gate(now=SH_OPEN).place(dict(order, client_order_id=None))
                self.assertTrue(out["ok"], out)
        for market in ("SH", "SZ", "BJ"):
            order = dict(SH_ORDER, symbol=f"{market}.600519")
            with self.subTest(market=market):
                out = self.gate(now=SH_AFTER).place(dict(order))
                self.assertFalse(out["ok"], out)


class HalfDayGateTest(GateBase):
    """半日/提前收盘必须缩短窗口：日历里的 ``trade_second`` 是唯一判据。"""

    def test_hk_half_day_1230_is_refused(self):
        """港股半日市真实收盘 12:00——12:30 必须拒绝（旧口径按 16:10 放行）。"""
        self.seed_hk("2026-09-18", 9000)
        out = self.gate(now=HK_HALF_AFTER).place(dict(HK_ORDER))
        self.assertFalse(out["ok"], out)
        self.assertIn("12:00", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])

    def test_hk_full_day_same_moment_is_allowed(self):
        """对照：同一时刻当日是全天行（trade_second=19800）→ 放行。"""
        self.seed_hk("2026-09-18", 19800)
        out = self.gate(now=HK_HALF_AFTER).place(dict(HK_ORDER))
        self.assertTrue(out["ok"], out)

    def test_hk_half_day_before_real_close_is_allowed(self):
        self.seed_hk("2026-09-18", 9000)
        out = self.gate(now="2026-09-18 11:30:00").place(dict(HK_ORDER))
        self.assertTrue(out["ok"], out)

    def test_missing_calendar_row_falls_back_to_full_day_window(self):
        """日历没这一行（休市日/未同步）→ 按全天窗口判时刻；非交易日由规则 3 兜底。"""
        out = self.gate(now="2026-09-18 09:30:00").place(dict(HK_ORDER))
        self.assertTrue(out["ok"], out)


class UsSessionGateTest(GateBase):
    """美股：``session`` 按官方 ``PLACE_SESSIONS`` 实际取值分派（本轮实测 4 值）。"""

    def test_rth_window_by_default(self):
        self.assertTrue(self.gate(now=US_RTH).place(dict(US_ORDER))["ok"])

    def test_premarket_window_requires_session(self):
        """未请求盘前 → 常规窗口（09:30–16:00）判：北京 17:00 = ET 05:00 → 拒绝。"""
        denied = self.gate(now=US_PRE).place(dict(US_ORDER))
        self.assertFalse(denied["ok"], denied)

    def test_premarket_window_allowed_when_session_requested(self):
        allowed = self.live_gate(now=US_PRE).place(
            dict(US_ORDER, session="RTH+Pre/Post-Mkt"))
        self.assertTrue(allowed["ok"], allowed)
        self.assertEqual(self.broker.calls[0][0], "place")

    def test_regular_session_outside_window_is_refused(self):
        out = self.gate(now=US_CLOSED).place(dict(US_ORDER))
        self.assertFalse(out["ok"], out)
        self.assertEqual(self.broker.calls, [])

    def test_extended_sessions_are_all_inside_the_extended_window(self):
        for index, session in enumerate(("RTH+Pre/Post-Mkt", "OVERNIGHT", "ALL_DAY")):
            with self.subTest(session=session):
                out = self.live_gate(now=US_PRE).place(
                    dict(US_ORDER, symbol=f"US.SYM{index}", session=session))
                self.assertTrue(out["ok"], out)

    def test_official_sessions_are_fully_mapped_no_drift(self):
        """锁：``PLACE_SESSIONS`` 的每个取值都必须被 sessions 分派（防两侧漂移）。"""
        mapped = {"RTH", *sessions.US_EXTENDED_SESSIONS}
        self.assertEqual(mapped, set(trading.PLACE_SESSIONS))
        self.assertEqual(sessions.US_REGULAR_SESSIONS, (None, "RTH"))

    def test_session_values_outside_official_enum_are_rejected_by_field_layer(self):
        """未知 session 到不了闸门：字段校验层先拒（错误码与既有契约一致）。"""
        out = self.gate(now=US_RTH).place(dict(US_ORDER, session="NOPE"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")


class SessionRefusalUnitTest(GateBase):
    """``TradeGate.session_refusal``：fail-closed 分支与可读原因（不经字段校验层）。"""

    def test_unknown_market_fails_closed(self):
        conn = self.conn()
        try:
            message = self.gate().session_refusal(conn, {"symbol": "XX.AAPL"})
        finally:
            conn.close()
        self.assertIsNotNone(message)
        self.assertIn("未知市场", message)

    def test_unknown_us_session_fails_closed(self):
        conn = self.conn()
        try:
            message = self.gate().session_refusal(
                conn, {"symbol": "US.AAPL", "session": "NOPE"})
        finally:
            conn.close()
        self.assertIsNotNone(message)
        self.assertIn("NOPE", message)

    def test_injected_clock_is_the_only_time_source(self):
        """同一份上下文，换注入时刻即换结论——证明判定只依赖注入时钟。"""
        conn = self.conn()
        try:
            self.assertIsNone(self.gate(now=SH_OPEN).session_refusal(
                conn, dict(SH_ORDER)))
            self.assertIsNotNone(self.gate(now=SH_AFTER).session_refusal(
                conn, dict(SH_ORDER)))
        finally:
            conn.close()


class PlanExecuteGateTest(GateBase):
    """人工 ``plan-execute`` 入队前的时段闸门（工作台唯一的人工执行入口）。"""

    def setUp(self):
        super().setUp()
        self.handle = None
        self.pending = self.home / "trading-commands" / "pending"

    def seed_plan(self, plan_hash, markets, plan_id=None):
        """登记一份冻结计划 + 其订单（``plan_id`` 主键，重复调用须给不同 ``plan_id``）。"""
        plan_id = plan_id or f"P-{plan_hash}"
        conn = self.conn()
        try:
            conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,"
                         "content_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (plan_id, "2026-09-18", "sim", "s", "{}", plan_hash,
                          "frozen", "2026-09-18 09:00:00"))
            for index, market in enumerate(markets):
                conn.execute(
                    "INSERT INTO orders(client_order_id,plan_id,symbol,market,side,qty,"
                    "price,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (f"C-{plan_id}-{index}", plan_id, f"{market}.X", market,
                     "BUY", 10, 1.0, "draft", "sim", "2026-09-18 09:00:00",
                     "2026-09-18 09:00:00"))
            conn.commit()
        finally:
            conn.close()

    def handler(self, now):
        gate = self.gate(now=now)
        return app_module.create_handler(str(self.home), analytics={}, series=None,
                                         core={}, trade=gate)

    def files(self):
        return sorted(self.pending.glob("*.json")) if self.pending.is_dir() else []

    def test_all_markets_closed_is_refused_without_writing_a_command(self):
        self.seed_plan("H-CLOSED", ["US"])
        body = self.handler(US_CLOSED)("plan-execute",
                                       {"plan_hash": "H-CLOSED", "expected_mode": "sim"})
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["error"]["code"], "trading/order-rejected")
        self.assertIn("时段闸门", body["error"]["message"])
        self.assertEqual(self.files(), [], "拒绝时不得落指令文件")

    def test_open_market_plan_is_queued(self):
        self.seed_plan("H-OPEN", ["SH"])
        body = self.handler(SH_OPEN)("plan-execute",
                                     {"plan_hash": "H-OPEN", "expected_mode": "sim"})
        self.assertTrue(body["ok"], body)
        self.assertEqual(len(self.files()), 1)
        saved = json.loads(self.files()[0].read_text(encoding="utf-8"))
        self.assertEqual(saved["type"], "execute_plan")
        self.assertEqual(saved["plan_hash"], "H-OPEN")

    def test_one_open_market_is_enough_to_queue_a_mixed_plan(self):
        """宁可放过、不可错杀：计划里有任一市场开市就不连坐（其余市场由券商/守卫判）。"""
        self.seed_plan("H-MIX", ["US", "SH"])
        body = self.handler(SH_OPEN)("plan-execute",
                                     {"plan_hash": "H-MIX", "expected_mode": "sim"})
        self.assertTrue(body["ok"], body)

    def test_duplicate_content_hash_markets_are_merged(self):
        """``content_hash`` 不是主键：同 hash 的多份计划要并起来判。

        取一条「只看第一份（US）会拒、并起来（US+SH）才放行」的时刻：北京 10:00 沪市开市
        而美股闭市。旧写法按 ``content_hash`` 取 ``fetchone()`` 会漏掉第二份计划的市场。
        """
        self.seed_plan("H-DUP", ["US"], plan_id="P-DUP-1")
        self.seed_plan("H-DUP", ["SH"], plan_id="P-DUP-2")
        body = self.handler(SH_OPEN)("plan-execute",
                                     {"plan_hash": "H-DUP", "expected_mode": "sim"})
        self.assertTrue(body["ok"], body)

    def test_unknown_plan_is_not_blocked_by_this_gate(self):
        """计划合法性由 execute.run 把关；本闸门只判时段（不存在的计划不在此判）。"""
        body = self.handler(US_CLOSED)("plan-execute",
                                       {"plan_hash": "H-MISSING", "expected_mode": "sim"})
        self.assertTrue(body["ok"], body)

    def test_cancel_action_is_never_blocked(self):
        body = self.handler(US_CLOSED)("plan-execute",
                                       {"plan_hash": "H-OPEN", "action": "cancel"})
        self.assertTrue(body["ok"], body)
        self.assertEqual(len(self.files()), 1)


class Rule3WordingTest(unittest.TestCase):
    """规则 3 的文案必须只说它真正检查的事（``is_trading_day``，日粒度）。"""

    def test_rule3_reason_matches_its_check(self):
        from trading_core import risk
        verdict = risk.pre_trade_checks(
            {"mode": "sim", "symbol": "SH.600519", "qty": 100, "price": 1.0},
            {"config": {"risk_per_trade": 0.01, "max_positions": 5,
                        "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03},
             "kill_path": "/nonexistent", "mode": "sim", "is_trading_day": False,
             "equity": 1_000_000.0, "positions_value": {}, "positions_count": 0,
             "day_pnl_pct": 0.0, "plan_hash": None, "plan_status": "approved"})
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.rule, 3)
        self.assertEqual(verdict.reason, "非交易日：不提交订单")
        self.assertNotIn("连续竞价", verdict.reason, "该规则没有时钟，不得声称检查时段")

    def test_rule3_allow_path_unchanged_with_trading_day(self):
        from trading_core import risk
        verdict = risk.pre_trade_checks(
            {"mode": "sim", "symbol": "SH.600519", "qty": 100, "price": 1.0,
             "stop_dist": 0.5},
            {"config": {"risk_per_trade": 0.01, "max_positions": 5,
                        "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03},
             "kill_path": "/nonexistent", "mode": "sim", "is_trading_day": True,
             "equity": 1_000_000.0, "positions_value": {}, "positions_count": 0,
             "day_pnl_pct": 0.0, "plan_hash": None, "plan_status": "approved"})
        self.assertTrue(verdict.allowed, verdict)


if __name__ == "__main__":
    unittest.main(verbosity=2)
