"""WP7 任务 3：交易闸门 + 受约束交易工具（trade_place/modify/cancel + account_*）。

全部离线：broker 用计数替身（FakeBroker），确认用假确认（FakeConfirm）或
真实 store_access 三方法（进程内作答，短 TTL 当假时钟），日历/时钟用注入或当日种子。
覆盖：
  * 闸门链顺序：模式非法 / kill（规则 1）/ 风控规则 N / 确认拒绝 / 确认超时 →
    各自 ``{ok:false}`` 且 **broker 零调用**；
  * 确认流（live，真实 store_access）：trade_place 阻塞 → confirmation_view 出现
    待确认（operation=下单，摘要含标的/数量/价格）→ decide(approved) → broker 恰好
    一次下单且参数逐键一致；rejected → broker 零调用 + 拒单原因；
  * 幂等：同一 client_order_id 重复提交不重复下单（对齐 OMS：cid 主键 + 在途查重）；
  * 查询直通：account_positions/orders/funds 受 mode 约束；broker 抛错 → 信封不 500；
  * 默认 broker 适配（FutuBroker）sim 分支：工具名/参数对齐 trading_core.broker 锁定表；
  * live 写前置拒绝：默认适配器（supports_live_write=False）确认零调用、broker 零调用、
    不落 OMS/风控行；声明支持 live 写的适配器才走完整确认流；
  * HTTP 路由：白名单、信封透传、写端点不进缓存。
"""
import sys
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))

from server import app as app_module  # noqa: E402
from server import store_access, trading  # noqa: E402
from trading_core import daemon as core_daemon  # noqa: E402
from trading_core import risk as core_risk  # noqa: E402
from trading_core import store as core_store  # noqa: E402

ORDER = {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 123.5}
RISK_CONFIG = {"risk_per_trade": 0.01, "max_positions": 5, "max_position_pct": 0.25,
               "daily_loss_limit_pct": 0.03}


def fixed_ctx(**over):
    """注入用 ctx 构造器：全部合法默认（交易日 True），over 覆盖单项。
    equity 给 10_000_000 让示例订单（100×123.5=12350）落在规则 4 限额内。"""
    def builder(conn, mode, order, operation, home, today):
        ctx = {"mode": mode, "kill_path": str(Path(home) / "trading-kill"),
               "equity": 10_000_000.0, "positions_value": {}, "positions_count": 0,
               "day_pnl_pct": 0.0, "is_trading_day": True, "config": dict(RISK_CONFIG),
               "plan_hash": None, "plan_status": "approved"}
        ctx.update(over)
        return ctx
    return builder


class FakeBroker:
    """计数替身：记录每次 (op, 入参)；error 注入时抛异常模拟 broker 故障。

    supports_live_write=True：替身模拟「已接入 live 写的自定义适配器」，既有 live
    用例（确认流/拒绝流/改撤单）因此继续走完整闸门链不回归；默认适配器
    （FutuBroker，supports_live_write=False）的确认前快速拒绝见 LiveWriteGateTest。
    """

    supports_live_write = True

    def __init__(self, error=None, place_result=None):
        self.calls = []
        self.error = error
        self.place_result = place_result

    def _record(self, op, payload):
        self.calls.append((op, payload))
        if self.error is not None:
            raise self.error

    def place(self, order, mode):
        self._record("place", dict(order, mode=mode))
        result = {"status": "submitted", "broker_order_id": "B-1"}
        if self.place_result:
            result.update(self.place_result)
        return result

    def modify(self, order, mode):
        self._record("modify", dict(order, mode=mode))
        return {"status": "submitted", "broker_order_id": "B-2"}

    def cancel(self, order, mode):
        self._record("cancel", dict(order, mode=mode))
        return {"status": "cancelled", "order_id": order.get("order_id")}

    def positions(self, mode):
        self._record("positions", {"mode": mode})
        return {"mode": mode, "groups": [{"acc_id": "A1"}]}

    def orders(self, mode):
        self._record("orders", {"mode": mode})
        return {"mode": mode, "groups": []}

    def funds(self, mode):
        self._record("funds", {"mode": mode})
        return {"mode": mode, "groups": [{"acc_id": "A1", "cash": 1.0}]}

    def count(self, op):
        return sum(1 for name, _ in self.calls if name == op)


class FakeConfirm:
    """假确认：request 直接返回预定结论（拒绝/批准），绝不阻塞。"""

    def __init__(self, outcome=None):
        self.requests = []
        self.outcome = outcome or {"decision": "rejected", "id": None,
                                   "reason": "假确认：无人应答"}

    def request(self, home, tool, mode, args, session_id, ttl_ms):
        self.requests.append({"tool": tool, "mode": mode, "args": args,
                              "session_id": session_id, "ttl_ms": ttl_ms})
        return dict(self.outcome)

    def view(self, home):
        return None

    def decide(self, home, confirmation_id, decision):  # pragma: no cover - 假件不裁决
        raise AssertionError("FakeConfirm 不提供 decide")


class GateTestBase(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.set_mode("sim")
        self.broker = FakeBroker()
        self.confirm = FakeConfirm()

    def set_mode(self, mode):
        (self.home / "trading-account-mode").write_text(f"{mode}\n")

    def kill_on(self):
        (self.home / "trading-kill").write_text("")

    def gate(self, **kw):
        kw.setdefault("broker", self.broker)
        kw.setdefault("confirm", self.confirm)
        kw.setdefault("ctx_builder", fixed_ctx())
        return trading.TradeGate(str(self.home), **kw)

    def order_row(self, cid):
        conn = core_store.connect(core_store.db_path(str(self.home)))
        try:
            return conn.execute("SELECT * FROM orders WHERE client_order_id=?",
                                (cid,)).fetchone()
        finally:
            conn.close()

    def risk_rows(self):
        conn = core_store.connect(core_store.db_path(str(self.home)))
        try:
            return conn.execute("SELECT * FROM risk_checks").fetchall()
        finally:
            conn.close()


class GateChainTest(GateTestBase):
    """闸门链顺序：任何前置失败 → {ok:false} 且 broker 零调用。"""

    def test_invalid_mode_file_rejects_before_anything(self):
        (self.home / "trading-account-mode").write_text("yolo\n")
        out = self.gate().place(dict(ORDER))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/order-rejected")
        self.assertIn("模式", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.confirm.requests, [])

    def test_field_validation_rejects_before_mode_and_broker(self):
        """字段校验层失败 = 载荷非法 → ``trading/invalid-operation``（WP8 任务 6 起与
        HTTP/MCP 白名单同码族，消息带官方允许值）；风控/确认/券商拒绝仍是 order-rejected。"""
        cases = [
            {"side": "BUY", "qty": 100, "price": 1.0},                    # 缺 symbol
            dict(ORDER, symbol="600519"),                                  # 无市场前缀
            dict(ORDER, symbol="XX.1234"),                                 # 未知市场
            dict(ORDER, side="buy"),                                       # 方向枚举
            dict(ORDER, qty=0),                                            # 数量范围
            dict(ORDER, qty=100.5),                                        # 数量非整
            dict(ORDER, qty=True),                                         # bool 冒充 int
            dict(ORDER, price=0),                                          # 价格范围
            dict(ORDER, price=-1),                                         # 负价格
            dict(ORDER, price="12.3"),                                     # 价格非数值
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                out = self.gate().place(payload)
                self.assertFalse(out["ok"], payload)
                self.assertEqual(out["error"]["code"], "trading/invalid-operation")
                self.assertEqual(self.broker.calls, [])
                self.assertEqual(self.confirm.requests, [])

    def test_kill_file_rule_1_rejects_before_confirm_and_broker(self):
        self.kill_on()
        out = self.gate().place(dict(ORDER))
        self.assertFalse(out["ok"])
        self.assertIn("kill", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.confirm.requests, [])

    def test_risk_rule_7_rejects_before_confirm_and_broker(self):
        out = self.gate(ctx_builder=fixed_ctx(day_pnl_pct=-0.05)).place(dict(ORDER))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/order-rejected")
        self.assertIn("7", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.confirm.requests, [])
        # 预检留痕进 risk_checks（复用既有审计表），订单行落 cancelled
        row = self.order_row(out["error"]["details"].get("client_order_id") or "")
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "cancelled")
        self.assertTrue(row["err"].startswith("risk:"))
        self.assertEqual([bool(r["allowed"]) for r in self.risk_rows()], [False])

    def test_confirm_rejected_rejects_with_zero_broker_calls(self):
        self.set_mode("live")
        out = self.gate().place(dict(ORDER))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/order-rejected")
        self.assertIn("假确认", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(len(self.confirm.requests), 1)
        self.assertEqual(self.order_row(out["error"]["details"]["client_order_id"])["status"],
                         "cancelled")

    def test_confirm_timeout_is_fail_closed_with_short_ttl(self):
        """假时钟：真实 store_access 确认 + 40ms TTL → 到期自动拒绝，broker 零调用。"""
        self.set_mode("live")
        out = self.gate(confirm=trading.StoreConfirm(),
                        confirm_ttl_ms=40).place(dict(ORDER))
        self.assertFalse(out["ok"])
        self.assertIn("未确认", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])
        self.assertIsNone(store_access.confirmation_view(str(self.home)))

    def test_sim_order_needs_no_confirmation(self):
        out = self.gate().place(dict(ORDER, client_order_id="CID-SIM"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.confirm.requests, [], "sim 写操作不发起业务确认")
        self.assertEqual(self.broker.count("place"), 1)


class ConfirmFlowTest(GateTestBase):
    """确认流：真实 store_access 三方法，进程内阻塞与作答。"""

    def setUp(self):
        super().setUp()
        self.set_mode("live")
        self.gate = self.gate(confirm=trading.StoreConfirm())

    def decide(self, confirmation_id, decision):
        return store_access.decide_confirmation(str(self.home),
                                                confirmation_id=confirmation_id,
                                                decision=decision)

    def test_approved_order_reaches_broker_exactly_once(self):
        result = {}

        def submit():
            result["out"] = self.gate.place(dict(ORDER, client_order_id="CID-LIVE"))

        worker = threading.Thread(target=submit)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            pending = None
            while time.monotonic() < deadline:
                pending = store_access.confirmation_view(str(self.home))
                if pending is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(pending, "阻塞期间应出现待确认")
            self.assertEqual(pending["operation"], "下单")
            self.assertEqual(pending["tool"], "trade_input_order")
            labels = {f["label"]: f["value"] for f in pending["summary"]["fields"]}
            self.assertEqual(labels.get("标的"), "SH.600519")
            self.assertEqual(labels.get("数量"), "100")
            self.assertEqual(labels.get("价格"), "123.5")
            self.assertEqual(self.broker.calls, [], "确认前 broker 必须零调用")
            decided = self.decide(pending["id"], "approved")
            self.assertEqual(decided["decision"], "approved")
        finally:
            worker.join(5)
        self.assertFalse(worker.is_alive(), "批准后 place 必须解除阻塞")
        out = result["out"]
        self.assertTrue(out["ok"], out)
        value = out["value"]
        self.assertEqual(value["status"], "submitted")
        self.assertEqual(value["client_order_id"], "CID-LIVE")
        self.assertEqual(value["broker_order_id"], "B-1")
        # broker 恰好一次下单，参数逐键一致
        self.assertEqual(self.broker.count("place"), 1)
        op, payload = self.broker.calls[0]
        self.assertEqual(op, "place")
        self.assertEqual(payload, {"symbol": "SH.600519", "side": "BUY", "qty": 100,
                                   "price": 123.5, "mode": "live"})
        row = self.order_row("CID-LIVE")
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["broker_order_id"], "B-1")
        self.assertIsNone(store_access.confirmation_view(str(self.home)))

    def test_rejected_order_never_reaches_broker(self):
        result = {}

        def submit():
            result["out"] = self.gate.place(dict(ORDER, client_order_id="CID-REJ"))

        worker = threading.Thread(target=submit)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            pending = None
            while time.monotonic() < deadline:
                pending = store_access.confirmation_view(str(self.home))
                if pending is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(pending)
            self.decide(pending["id"], "rejected")
        finally:
            worker.join(5)
        out = result["out"]
        self.assertFalse(out["ok"])
        self.assertIn("拒绝", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])
        self.assertEqual(self.order_row("CID-REJ")["status"], "cancelled")


class LiveWriteGateTest(GateTestBase):
    """live 写前置拒绝（审查必修 2）：默认适配器在确认之前快速失败——不发起确认、
    不产生待确认、不落 OMS/风控行；声明支持 live 写的适配器才走完整确认流。"""

    def test_default_adapter_live_write_rejects_before_confirm_and_broker(self):
        self.set_mode("live")
        self.assertFalse(trading.FutuBroker.supports_live_write)
        broker = trading.FutuBroker()  # 不注入 _call：一旦被触达就会惰性导入并暴露
        gate = trading.TradeGate(str(self.home), broker=broker, confirm=self.confirm,
                                 ctx_builder=fixed_ctx())
        cases = [("place", dict(ORDER)),
                 ("modify", {"order_id": "1", **ORDER}),
                 ("cancel", {"order_id": "1", "symbol": "SH.600519"})]
        for op, payload in cases:
            with self.subTest(op=op):
                out = getattr(gate, op)(payload)
                self.assertFalse(out["ok"])
                self.assertEqual(out["error"]["code"], "trading/broker-unavailable")
                self.assertIn("当前仅 sim 可交易", out["error"]["message"])
                self.assertEqual(self.confirm.requests, [], "确认零调用")
                self.assertIsNone(broker._call, "broker 零触达（惰性导入未触发）")
        # 快速失败不落任何痕迹：无订单行、无风控留痕、无待确认
        conn = core_store.connect(core_store.db_path(str(self.home)))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM risk_checks").fetchone()[0], 0)
        finally:
            conn.close()
        self.assertIsNone(store_access.confirmation_view(str(self.home)))

    def test_adapter_without_supports_live_write_attr_is_treated_as_unsupported(self):
        """鸭子类型适配器未声明 supports_live_write → 按「不支持」处理（fail-safe）。"""
        self.set_mode("live")

        class BareBroker:
            def place(self, order, mode):  # pragma: no cover —— 断言不得触达
                raise AssertionError("live 写不得触达未声明支持的适配器")

        gate = trading.TradeGate(str(self.home), broker=BareBroker(),
                                 confirm=self.confirm, ctx_builder=fixed_ctx())
        out = gate.place(dict(ORDER))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/broker-unavailable")
        self.assertEqual(self.confirm.requests, [])

    def test_adapter_declaring_live_write_support_goes_through_confirmation(self):
        """自定义适配器 supports_live_write=True → 走完整确认流：批准后恰好一次下单。"""
        self.set_mode("live")
        confirm = FakeConfirm(outcome={"decision": "approved", "id": "c1",
                                       "reason": "用户在工作台确认"})
        gate = self.gate(confirm=confirm)
        self.assertTrue(type(gate.broker).supports_live_write)
        out = gate.place(dict(ORDER, client_order_id="CID-LIVE-SUPPORTED"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(len(confirm.requests), 1, "支持 live 写也必须过业务确认")
        self.assertEqual(self.broker.count("place"), 1)
        self.assertEqual(self.order_row("CID-LIVE-SUPPORTED")["status"], "submitted")


class IdempotencyTest(GateTestBase):
    def test_same_client_order_id_does_not_re_trade(self):
        gate = self.gate()
        first = gate.place(dict(ORDER, client_order_id="CID-1"))
        self.assertTrue(first["ok"], first)
        second = gate.place(dict(ORDER, client_order_id="CID-1"))
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["value"]["duplicate"])
        self.assertEqual(second["value"]["status"], "submitted")
        self.assertEqual(second["value"]["broker_order_id"], "B-1")
        self.assertEqual(self.broker.count("place"), 1, "重复 cid 不得二次下单")

    def test_different_symbols_get_unique_ids_and_both_reach_broker(self):
        gate = self.gate()
        one = gate.place(dict(ORDER))
        two = gate.place(dict(ORDER, symbol="SZ.300750"))
        self.assertTrue(one["ok"] and two["ok"])
        self.assertNotEqual(one["value"]["client_order_id"], two["value"]["client_order_id"],
                            "缺省编号由 OMS uuid 口径生成，绝不复用")
        self.assertEqual(self.broker.count("place"), 2)

    def test_duplicate_open_adhoc_order_is_rejected_like_oms(self):
        """OMS 幂等三件套之二：同标的同方向在途单（adhoc 槽）拒绝重复登记。"""
        gate = self.gate()
        self.assertTrue(gate.place(dict(ORDER))["ok"])
        other = gate.place(dict(ORDER, price=124.0))
        self.assertFalse(other["ok"])
        self.assertIn("在途", other["error"]["message"])
        self.assertEqual(self.broker.count("place"), 1)


class SellAndOpsTest(GateTestBase):
    """改单/撤单的成功路径：live + 假确认批准（闸门前置失败见 GateChainTest）。"""

    def approved_confirm(self):
        return FakeConfirm(outcome={"decision": "approved", "id": "c1",
                                    "reason": "用户在工作台确认"})

    def test_modify_passes_gate_and_hits_broker_once(self):
        self.set_mode("live")
        confirm = self.approved_confirm()
        out = self.gate(confirm=confirm).modify(
            {"order_id": "7100001", "symbol": "SH.600519", "side": "BUY",
             "qty": 200, "price": 130.0})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.broker.count("modify"), 1)
        self.assertEqual(len(confirm.requests), 1)
        self.assertEqual(confirm.requests[0]["tool"], "trade_modify_order")

    def test_modify_risk_uses_new_parameters(self):
        self.set_mode("live")
        out = self.gate(ctx_builder=fixed_ctx(equity=1_000.0)).modify(
            {"order_id": "7100001", "symbol": "SH.600519", "side": "BUY",
             "qty": 100, "price": 130.0})
        self.assertFalse(out["ok"])
        self.assertIn("单笔风险", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])

    def test_cancel_passes_gate_and_hits_broker_once(self):
        self.set_mode("live")
        confirm = self.approved_confirm()
        out = self.gate(confirm=confirm).cancel(
            {"order_id": "7100001", "symbol": "SH.600519"})
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.broker.count("cancel"), 1)
        self.assertEqual(confirm.requests[0]["tool"], "trade_cancel_order")

    def test_cancel_requires_order_id(self):
        out = self.gate().cancel({"symbol": "SH.600519"})
        self.assertFalse(out["ok"])
        self.assertEqual(self.broker.calls, [])

    def test_cancel_binding_rules_are_kill_mode_trading_day(self):
        """撤单不新增敞口：规则 4-7 以中性参数过闸（披露），规则 1/2/3 真实约束。"""
        gate = self.gate(ctx_builder=fixed_ctx(is_trading_day=False))
        out = gate.cancel({"order_id": "7100001", "symbol": "SH.600519"})
        self.assertFalse(out["ok"])
        self.assertIn("3", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])


class QueryTest(GateTestBase):
    def test_queries_pass_through_with_mode_file(self):
        gate = self.gate()
        for name in ("positions", "orders", "funds"):
            out = getattr(gate, name)()
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["value"]["mode"], "sim")
        self.assertEqual(self.broker.count("positions"), 1)
        self.assertEqual(self.broker.count("orders"), 1)
        self.assertEqual(self.broker.count("funds"), 1)

    def test_query_mode_payload_overrides_mode_file(self):
        self.gate().positions(mode="live")
        self.assertEqual(self.broker.calls[0], ("positions", {"mode": "live"}))

    def test_query_invalid_mode_is_rejected(self):
        out = self.gate().positions(mode="paper")
        self.assertFalse(out["ok"])
        self.assertEqual(self.broker.calls, [])

    def test_broker_exception_becomes_envelope_not_crash(self):
        gate = self.gate(broker=FakeBroker(error=RuntimeError("boom")))
        for name in ("positions", "orders", "funds"):
            out = getattr(gate, name)()
            self.assertFalse(out["ok"])
            self.assertEqual(out["error"]["code"], "trading/broker-unavailable")
            self.assertIn("boom", out["error"]["message"])

    def test_broker_exception_on_place_is_envelope(self):
        gate = self.gate(broker=FakeBroker(error=RuntimeError("链接断开")))
        out = gate.place(dict(ORDER))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/broker-unavailable")
        row = self.order_row(out["error"]["details"]["client_order_id"])
        self.assertEqual(row["status"], "rejected")


class DefaultCtxTest(GateTestBase):
    """默认 ctx 构造：日历取真值、风控配置取真值、敞口字段保守默认（披露口径）。"""

    def setUp(self):
        super().setUp()
        conn = core_store.connect(core_store.db_path(str(self.home)))
        today = datetime.now().strftime("%Y-%m-%d")
        conn.execute("INSERT INTO calendar(market, day, trade_date_type) VALUES('SH',?, 'MORNING')",
                     (today,))
        conn.commit()
        conn.close()
        self.captured = {}

        def recording_risk(order, ctx):
            self.captured.update(ctx)
            return core_risk.Verdict(True)

        self.risk = recording_risk

    def test_default_builder_uses_calendar_and_risk_config(self):
        out = self.gate(risk_fn=self.risk,
                        ctx_builder=trading.default_ctx_builder).place(dict(ORDER))
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.captured["mode"], "sim")
        self.assertTrue(self.captured["is_trading_day"])
        self.assertEqual(self.captured["config"], core_daemon.risk_config(str(self.home)))
        self.assertEqual(self.captured["kill_path"], str(self.home / "trading-kill"))
        # 临时订单的规则 8 口径（如实断言，见 trading.py 披露注释）：
        self.assertIsNone(self.captured["plan_hash"])
        self.assertEqual(self.captured["plan_status"], "approved")
        # 敞口三字段取不到真值时的保守默认（daemon 离线口径）
        self.assertEqual(self.captured["equity"], 1_000_000.0)
        self.assertEqual(self.captured["positions_value"], {})
        self.assertEqual(self.captured["day_pnl_pct"], 0.0)

    def test_default_builder_fail_closed_without_calendar(self):
        home2 = self.home / "empty"
        home2.mkdir()
        (home2 / "trading-account-mode").write_text("sim\n")
        gate = trading.TradeGate(str(home2), broker=self.broker, confirm=self.confirm)
        out = gate.place(dict(ORDER))
        self.assertFalse(out["ok"])
        self.assertIn("3", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])

    def test_default_builder_kill_file_blocks(self):
        self.kill_on()
        out = self.gate(ctx_builder=trading.default_ctx_builder).place(dict(ORDER))
        self.assertFalse(out["ok"])
        self.assertIn("kill", out["error"]["message"])
        self.assertEqual(self.broker.calls, [])


class FutuBrokerAdapterTest(unittest.TestCase):
    """默认 broker 适配（sim 分支经 trading_core.broker 锁定工具名；注入假 call_tool）。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fake_call(self, fn):
        calls, results = [], {"sim_trade_account_list": {"accounts": [
            {"account_id": "A-9", "market_id": 3}]}}
        def call(name, args, timeout=30):
            calls.append((name, args))
            return results.get(name, {"order_id": "7100001"})
        fn.calls, fn._call = calls, call
        return call

    def test_sim_place_uses_locked_tool_and_account_resolution(self):
        broker = trading.FutuBroker()
        self.fake_call(broker)
        out = broker.place(dict(ORDER), mode="sim")
        self.assertEqual(out["status"], "submitted")
        # calls[0] 是 sim_trade_account_list 的账户解析（P4：先查账户再用实际 id）
        self.assertEqual(broker.calls[0][0], "sim_trade_account_list")
        name, args = broker.calls[1]
        self.assertEqual(name, "sim_trade_input_order")
        self.assertEqual(args["acc_id"], "A-9")
        self.assertEqual(args["order_side"], 1)
        self.assertEqual(args["qty"], 100)
        self.assertEqual(args["price"], 123.5)

    def test_sim_modify_is_cancel_then_place(self):
        broker = trading.FutuBroker()
        self.fake_call(broker)
        out = broker.modify({"order_id": "7100000", "symbol": "SH.600519", "side": "SELL",
                             "qty": 100, "price": 130.0}, mode="sim")
        self.assertEqual(out["status"], "submitted")
        names = [name for name, _ in broker.calls]
        self.assertEqual(names, ["sim_trade_account_list", "sim_trade_cancel_order",
                                 "sim_trade_input_order"])
        self.assertEqual(broker.calls[1][1]["order_id"], "7100000")
        self.assertEqual(broker.calls[2][1]["order_side"], 2)

    def test_sim_cancel_uses_locked_tool(self):
        broker = trading.FutuBroker()
        self.fake_call(broker)
        out = broker.cancel({"order_id": "7100000", "symbol": "SH.600519"}, mode="sim")
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(broker.calls[1][0], "sim_trade_cancel_order")

    def test_live_write_is_refused_with_disclosure(self):
        broker = trading.FutuBroker()
        self.fake_call(broker)
        for op, payload in (("place", dict(ORDER)),
                            ("modify", {"order_id": "1", **ORDER}),
                            ("cancel", {"order_id": "1", "symbol": "SH.600519"})):
            with self.subTest(op=op):
                with self.assertRaises(trading.BrokerUnavailable):
                    getattr(broker, op)(payload, mode="live")
        self.assertEqual(broker.calls, [], "live 写路径未接入，不得触达任何工具")

    def test_live_positions_uses_authorized_accounts(self):
        broker = trading.FutuBroker()
        def call(name, args, timeout=30):
            if name == "account_authorized_trd_accs":
                return {"accounts": [{"account_id": "L-1"}]}
            if name == "account_positions":
                return {"positions": [{"code": "HK.00700", "qty": "100"}]}
            raise AssertionError(f"意外工具 {name}")
        broker._call = call
        out = broker.positions(mode="live")
        self.assertEqual(out["mode"], "live")
        self.assertEqual(out["groups"][0]["acc_id"], "L-1")
        self.assertEqual(out["groups"][0]["positions"][0]["code"], "HK.00700")

    def test_sim_positions_collects_errors_without_masking(self):
        broker = trading.FutuBroker()
        def call(name, args, timeout=30):
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3},
                                     {"account_id": "A-2", "market_id": 1}]}
            if name == "sim_trade_position_list":
                if args["acc_id"] == "A-1":
                    raise RuntimeError("断线")
                return {"positions": []}
            raise AssertionError(f"意外工具 {name}")
        broker._call = call
        out = broker.positions(mode="sim")
        self.assertEqual(out["errors"][0]["acc_id"], "A-1")
        self.assertEqual(len(out["groups"]), 0)


class RoutingTest(unittest.TestCase):
    """HTTP 路由：白名单、信封透传、写端点不进缓存（与 plan-execute 同类）。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gate = FakeBroker()
        self.gate_calls = []

        class RecordingGate:
            def __init__(self, inner):
                self.inner = inner
            def __getattr__(self, name):
                return getattr(self.inner, name)
            def place(self, payload, session_id="mcp"):
                self.inner.calls.append(("trade_place", dict(payload)))
                return {"ok": True, "value": {"routed": "trade_place"}}
            def modify(self, payload, session_id="mcp"):
                self.inner.calls.append(("trade_modify", dict(payload)))
                return {"ok": True, "value": {"routed": "trade_modify"}}
            def cancel(self, payload, session_id="mcp"):
                self.inner.calls.append(("trade_cancel", dict(payload)))
                return {"ok": True, "value": {"routed": "trade_cancel"}}
            def positions(self, mode=None):
                self.inner.calls.append(("account_positions", {"mode": mode}))
                return {"ok": True, "value": {"routed": "account_positions", "mode": mode}}

        self.handle = app_module.create_handler(str(self.tmp), analytics={}, series=None,
                                                core={}, trade=RecordingGate(self.gate))

    def test_trade_routes_reach_gate_and_passthrough_envelope(self):
        out = self.handle("trade_place", dict(ORDER, client_order_id="C-1"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["value"]["routed"], "trade_place")
        self.assertEqual(self.gate.calls[0][0], "trade_place")
        self.assertEqual(self.gate.calls[0][1]["client_order_id"], "C-1")
        out = self.handle("trade_modify", {"order_id": "1", **ORDER})
        self.assertEqual(out["value"]["routed"], "trade_modify")
        out = self.handle("trade_cancel", {"order_id": "1", "symbol": "SH.600519"})
        self.assertEqual(out["value"]["routed"], "trade_cancel")

    def test_unknown_field_is_rejected_by_whitelist(self):
        out = self.handle("trade_place", dict(ORDER, mode="live"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")
        out = self.handle("account_positions", {"mode": "sim", "extra": 1})
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"]["code"], "trading/invalid-operation")

    def test_account_positions_mode_payload_forwarded(self):
        out = self.handle("account_positions", {"mode": "live"})
        self.assertTrue(out["ok"])
        self.assertEqual(self.gate.calls[-1], ("account_positions", {"mode": "live"}))

    def test_trade_endpoints_are_not_cached(self):
        self.handle("trade_place", dict(ORDER, client_order_id="C-1"))
        self.handle("trade_place", dict(ORDER, client_order_id="C-1"))
        self.assertEqual(len(self.gate.calls), 2, "写端点不得进缓存")
        self.handle("account_positions", {"mode": "sim"})
        self.handle("account_positions", {"mode": "sim"})
        self.assertEqual(len(self.gate.calls), 4, "实时查询端点不得进缓存")

    def test_new_endpoints_registered_in_manifest(self):
        from server import store_access as sa
        endpoints = sa.endpoints()
        for name in ("trade_place", "trade_modify", "trade_cancel",
                     "account_positions", "account_orders", "account_funds"):
            self.assertIn(name, endpoints, name)


class SurfaceLockTest(unittest.TestCase):
    """工具面 61（WP11 任务 3 起）：新工具进面、confirm-decide 仍排除、字段集与 HTTP 白名单同形。"""

    def test_tool_surface_is_74(self):
        from server import mcp_tools
        self.assertEqual(mcp_tools.TOOL_COUNT, 74)
        self.assertEqual(len(mcp_tools.TOOLS), 74)
        names = {t.name for t in mcp_tools.TOOLS}
        self.assertLessEqual({"trade_place", "trade_modify", "trade_cancel",
                              "account_positions", "account_orders", "account_funds"}, names)
        self.assertNotIn("confirm_decide", names)

    def test_write_tool_descriptions_mention_web_confirmation(self):
        from server import mcp_tools
        for tool in mcp_tools.TOOLS:
            if tool.name in ("trade_place", "trade_modify", "trade_cancel"):
                self.assertIn("确认", tool.description, tool.name)
                self.assertIn("120", tool.description, tool.name)

    def test_endpoint_tool_set_equals_endpoints_minus_excluded(self):
        from server import mcp_tools, store_access
        forwarded = set(mcp_tools.ENDPOINT_TOOL_ENDPOINTS.values())
        self.assertEqual(forwarded,
                         set(store_access.endpoints()) - mcp_tools.MCP_EXCLUDED_ENDPOINTS)

    def test_write_endpoints_absent_from_ttl_and_shape_tables(self):
        from server import caches
        for name in ("trade_place", "trade_modify", "trade_cancel",
                     "account_positions", "account_orders", "account_funds"):
            self.assertNotIn(name, caches.CACHE_TTL_MS, name)
            self.assertNotIn(name, caches.ENDPOINT_SHAPE, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
