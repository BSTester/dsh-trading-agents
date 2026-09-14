# WP3 执行闭环 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法跟踪进度。
> **全局约定**：见 `2026-09-14-platform-plan-index.md`。本包无 UI 页面（执行入口的 UI 在 WP4）。

**目标：** 风控硬拦截八规则、计划冻结与 hash、OMS 订单状态机、模拟盘券商适配器、对账与 TCA——达成「sim 全链路：计划→执行→成交→台账一致」（规格 §十 WP3）。

**架构：** `risk.py`（唯一提交入口的 8 规则）→ `planner.py`（目标 vs 实际 → 冻结计划）→ `oms.py`（状态机/幂等）→ `broker.py`（sim 账户适配器，唯一接触券商的模块）→ `reconcile.py`/`tca.py`。存储迁移 v3：plans/orders/fills/risk_checks 四表。

**技术栈：** 同 WP1/WP2；券商调用经 `trading_datasource.futu_mcp.call_tool`（账户类信封 `{"s":"ok","d":...}` 已由其归一化）。

---

## 依赖锁定表（2026-09-14 tools/list schema 实测）

| 工具 | required | 全部参数 | 用途 |
|---|---|---|---|
| `sim_trade_account_list` | — | — | 列模拟账户（acc_id/market） |
| `sim_trade_position_list` | — | acc_id, market（**market 缺省报 ret=-5**，P4 教训） | 券商实际持仓 |
| `sim_trade_cash_info` | — | acc_id | 可用资金 |
| `sim_trade_input_order` | acc_id, market, symbol, order_type, order_side, qty | +price, order_trade_time_type, text | **下单（名称锁定：不叫 place_order）** |
| `sim_trade_cancel_order` | acc_id, order_id, market | — | 撤单 |
| `sim_trade_modify_order` | — | 间歇性 -5 → **一律撤单+重下**（TOOL-LIMITS） | 不使用 |
| `sim_trade_history_order_list` | — | 含 start/end（不传静默返回纯文本 no data） | 订单历史 |
| `sim_trade_max_buy_sell` | — | acc_id/symbol/price | 最大可买卖 |
| `trading_order_place`（live） | acc_id, side, order_type, qty, time_in_force | +code, price, ... | 实盘下单（WP3 只锁 schema，不实现执行） |

铁律（P4 实测教训代码化）：提交超时→`unknown`→**只查询不重放**；`isError=false` 仍需查业务码；MCP `need_order_confirm` 按券商要求再次确认。

### 任务 0：依赖锁定测试 + 存储迁移 v3

**文件：** 创建 `tests/test_core_wp3_locks.py`；修改 `store.py`（v3 迁移 + 4 表读写函数）

- [ ] 步骤 1：失败测试：

```python
"""WP3 依赖锁定：券商工具名与必填参数（tools/list schema 实测口径，2026-09-14）。"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import broker  # noqa: E402


class Wp3Locks(unittest.TestCase):
    def test_broker_tool_names_locked(self):
        self.assertEqual(broker.TOOLS["place"], "sim_trade_input_order")
        self.assertEqual(broker.TOOLS["cancel"], "sim_trade_cancel_order")
        self.assertEqual(broker.TOOLS["positions"], "sim_trade_position_list")
        self.assertEqual(broker.TOOLS["accounts"], "sim_trade_account_list")
        self.assertEqual(broker.TOOLS["history"], "sim_trade_history_order_list")

    def test_place_required_params(self):
        self.assertEqual(set(broker.PLACE_REQUIRED),
                         {"acc_id", "market", "symbol", "order_type", "order_side", "qty"})
```

- [ ] 步骤 2：确认 FAIL；步骤 3：`store.py` —— `SCHEMA_VERSION = 3`，`_SCHEMA` 追加：

```sql
CREATE TABLE IF NOT EXISTS plans(
  plan_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, mode TEXT NOT NULL,
  strategy_id TEXT NOT NULL, target TEXT NOT NULL, content_hash TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, approved_at TEXT, approved_by TEXT);
CREATE TABLE IF NOT EXISTS orders(
  client_order_id TEXT PRIMARY KEY, plan_id TEXT, symbol TEXT NOT NULL,
  market TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
  price REAL, status TEXT NOT NULL, broker_order_id TEXT, mode TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, err TEXT);
CREATE TABLE IF NOT EXISTS fills(
  fill_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL, price REAL NOT NULL,
  qty INTEGER NOT NULL, traded_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_checks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT, symbol TEXT, rule INTEGER NOT NULL,
  allowed INTEGER NOT NULL, reason TEXT, checked_at TEXT NOT NULL);
```

并追加读写函数（供后续任务使用，TDD 在各任务内展开）：
`insert_plan/upsert_plan_status/get_plan/list_plans`、`insert_order/update_order_status/get_orders_by_plan/get_open_orders`、`insert_fill/fills_by_order`、`insert_risk_check/risk_checks_by_plan`、`set_halt/is_halted/clear_halt`（halt 落 kv 键 `halt:active`）。

- [ ] 步骤 4：broker.py 常量先行（使锁定测试过）：

```python
# plugins/core/python/trading_core/broker.py（本任务先只放锁定常量，任务 4 补齐行为）
TOOLS = {"place": "sim_trade_input_order", "cancel": "sim_trade_cancel_order",
         "positions": "sim_trade_position_list", "accounts": "sim_trade_account_list",
         "history": "sim_trade_history_order_list", "cash": "sim_trade_cash_info",
         "max_buy_sell": "sim_trade_max_buy_sell"}
PLACE_REQUIRED = ("acc_id", "market", "symbol", "order_type", "order_side", "qty")
```

- [ ] 步骤 5：PASS；`git commit -m "feat(core): WP3 依赖锁定与存储迁移 v3（plans/orders/fills/risk_checks）"`

### 任务 1：risk.py —— 八规则硬拦截

**文件：** 创建 `risk.py`；测试 `tests/test_core_risk.py`

- [ ] 步骤 1：失败测试（每条规则一个拒绝用例 + 全过用例）：

```python
"""风控八规则：逐条拒绝 + 计划一致性（规格 §七）。kill 文件用 tmp 路径注入。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import risk, store  # noqa: E402


def _ctx(**kw):
    base = {"mode": "SIM", "kill_path": kw.pop("kill_path", "/nonexistent-kill"),
            "equity": 1_000_000.0, "positions_value": {}, "positions_count": 2,
            "day_pnl_pct": -0.01, "is_trading_day": True, "conn": None,
            "plan_hash": "a3f8", "plan_status": "frozen",
            "config": {"risk_per_trade": 0.01, "max_positions": 5,
                        "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03},
            **kw}
    return base


ORDER = {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 1580.0,
         "mode": "SIM", "plan_id": "P1", "plan_hash": "a3f8", "stop_dist": 50.0}


class RiskTest(unittest.TestCase):
    def test_all_pass(self):
        verdict = risk.pre_trade_checks(ORDER, _ctx())
        self.assertTrue(verdict.allowed)

    def test_rule1_kill_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "trading-kill"; p.write_text("")
            v = risk.pre_trade_checks(ORDER, _ctx(kill_path=str(p)))
        self.assertFalse(v.allowed); self.assertEqual(v.rule, 1)

    def test_rule2_mode_mismatch(self):
        v = risk.pre_trade_checks(ORDER, _ctx(mode="LIVE"))
        self.assertEqual(v.rule, 2)

    def test_rule3_not_trading_day(self):
        v = risk.pre_trade_checks(ORDER, _ctx(is_trading_day=False))
        self.assertEqual(v.rule, 3)

    def test_rule4_risk_per_trade(self):
        bad = dict(ORDER, stop_dist=20000.0)  # 风险 2万 > 1% 权益
        self.assertEqual(risk.pre_trade_checks(bad, _ctx()).rule, 4)

    def test_rule5_max_position_pct(self):
        v = risk.pre_trade_checks(ORDER, _ctx(positions_value={"SH.600519": 260000.0}))
        self.assertEqual(v.rule, 5)  # 成交后 41.8万 > 25% × 100万

    def test_rule6_max_positions(self):
        v = risk.pre_trade_checks(ORDER, _ctx(positions_count=5))
        self.assertEqual(v.rule, 6)

    def test_rule7_daily_loss_halt(self):
        v = risk.pre_trade_checks(ORDER, _ctx(day_pnl_pct=-0.031))
        self.assertEqual(v.rule, 7)

    def test_rule8_plan_consistency(self):
        v = risk.pre_trade_checks(dict(ORDER, plan_hash="dead"), _ctx())
        self.assertEqual(v.rule, 8)
        v2 = risk.pre_trade_checks(ORDER, _ctx(plan_status="expired"))
        self.assertEqual(v2.rule, 8)
```

- [ ] 步骤 2：FAIL；步骤 3：实现 `risk.py`：

```python
"""风控硬拦截（规格 §七）：pre_trade_checks 是提交前唯一强制入口。

本文件只做判定；执行侧（oms/execute_plan）必须先调用本函数并落 risk_checks，
再决定是否提交——拒绝即跳过该单并记录，无覆盖按钮。
"""
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Verdict:
    allowed: bool
    rule: int = 0
    reason: str = ""


def _kill_exists(kill_path):
    return Path(kill_path).exists()


def pre_trade_checks(order, ctx):
    cfg = ctx["config"]
    # 规则 1 kill switch
    if _kill_exists(ctx["kill_path"]):
        return Verdict(False, 1, "kill switch 生效：拒绝一切订单")
    # 规则 2 模式一致
    if order["mode"] != ctx["mode"]:
        return Verdict(False, 2, f"订单模式 {order['mode']} != 账户模式 {ctx['mode']}")
    # 规则 3 交易时段
    if not ctx["is_trading_day"]:
        return Verdict(False, 3, "非交易日/非连续竞价时段")
    # 规则 4 单笔风险
    risk_amt = order["qty"] * (order.get("stop_dist") or order["price"])
    if risk_amt > ctx["equity"] * cfg["risk_per_trade"]:
        return Verdict(False, 4, f"单笔风险 {risk_amt:.0f} > 权益×{cfg['risk_per_trade']}")
    # 规则 5 单票市值上限（成交后口径）
    after = ctx["positions_value"].get(order["symbol"], 0.0) + order["qty"] * order["price"]
    if after > ctx["equity"] * cfg["max_position_pct"]:
        return Verdict(False, 5,
                       f"成交后单票市值 {after / ctx['equity']:.1%} > {cfg['max_position_pct']:.0%}")
    # 规则 6 最大持仓数（成交后口径）
    if ctx["positions_count"] + (1 if order["symbol"] not in ctx["positions_value"] else 0) > cfg["max_positions"]:
        return Verdict(False, 6, f"成交后持仓数 > {cfg['max_positions']}")
    # 规则 7 日内熔断
    if ctx["day_pnl_pct"] <= -cfg["daily_loss_limit_pct"]:
        return Verdict(False, 7, f"日内亏损 {ctx['day_pnl_pct']:.2%} 触发熔断 {cfg['daily_loss_limit_pct']:.0%}")
    # 规则 8 计划一致性
    if order.get("plan_hash") != ctx["plan_hash"] or ctx["plan_status"] not in ("frozen", "approved"):
        return Verdict(False, 8, "订单不属于 frozen 且 hash 匹配的计划")
    return Verdict(True)
```

- [ ] 步骤 4：PASS；`git commit -m "feat(core): 风控硬拦截八规则"`

### 任务 2：planner.py —— 计划生成与冻结

**文件：** 创建 `planner.py`；测试 `tests/test_core_planner.py`

- [ ] 步骤 1：失败测试：

```python
"""planner：目标权重 vs 券商实际 → 订单 diff → 冻结（hash）。"""
import json, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import planner, store  # noqa: E402


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_freeze_plan_hash_stable_and_diff(self):
        def broker_positions(mode):
            return {"SH.600519": {"qty": 400, "price": 1580.0}}, 1_000_000.0

        plan = planner.build_and_freeze(
            self.conn, mode="SIM", strategy_id="momentum_value_top5",
            target={"SH.600519": 0.5, "SZ.300750": 0.3},
            broker_positions=broker_positions, prices={"SH.600519": 1580.0, "SZ.300750": 201.8},
            as_of="2026-09-13", lot=100)
        self.assertEqual(plan["status"], "frozen")
        self.assertEqual(len(plan["orders"]), 2)
        again = planner.build_and_freeze(
            self.conn, mode="SIM", strategy_id="momentum_value_top5",
            target={"SH.600519": 0.5, "SZ.300750": 0.3},
            broker_positions=broker_positions, prices={"SH.600519": 1580.0, "SZ.300750": 201.8},
            as_of="2026-09-13", lot=100)
        self.assertNotEqual(plan["content_hash"], again["content_hash"])  # 含 plan_id，防重放
```

- [ ] 步骤 2：FAIL；步骤 3：实现 `planner.py`：

```python
"""计划生成与冻结（规格 §6.1）：diff 只生成必要的整手订单；冻结后不可变。"""
import hashlib, json, uuid
from datetime import datetime, timezone, timedelta

_TZ8 = timezone(timedelta(hours=8))


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def build_and_freeze(conn, mode, strategy_id, target, broker_positions, prices,
                     as_of, lot=100):
    positions, equity = broker_positions(mode)
    orders, plan_id = [], f"PLN-{as_of.replace('-', '')}-{mode}-{uuid.uuid4().hex[:4].upper()}"
    for symbol, weight in target.items():
        px = prices.get(symbol)
        if not px:
            continue  # 无价（停牌/无行情）：跳过并在审计可见，不猜价
        want_qty = int(equity * weight / px // lot * lot)
        have_qty = positions.get(symbol, {}).get("qty", 0)
        delta = want_qty - have_qty
        if delta == 0:
            continue
        orders.append({"symbol": symbol, "market": symbol.split(".")[0],
                       "side": "BUY" if delta > 0 else "SELL", "qty": abs(delta),
                       "price": px})
    content = json.dumps({"plan_id": plan_id, "as_of": as_of, "mode": mode,
                          "strategy_id": strategy_id, "target": target,
                          "orders": orders}, sort_keys=True, ensure_ascii=False)
    content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
                 "status,created_at) VALUES(?,?,?,?,?,?,'frozen',?)",
                 (plan_id, as_of, mode, strategy_id, json.dumps(target, ensure_ascii=False),
                  content_hash, _now()))
    conn.commit()
    return {"plan_id": plan_id, "as_of": as_of, "mode": mode, "status": "frozen",
            "orders": orders, "content_hash": content_hash}
```

- [ ] 步骤 4：PASS；`git commit -m "feat(core): 计划生成与冻结（content_hash）"`

### 任务 3：oms.py —— 订单状态机

**文件：** 创建 `oms.py`；测试 `tests/test_core_oms.py`

- [ ] 步骤 1：失败测试：

```python
"""OMS 状态机：合法迁移白名单、非法迁移拒绝、幂等（同计划同标的方向唯一在途）。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import oms, store  # noqa: E402


class OmsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(str(Path(self.tmp.name) / "t.sqlite"))
        conn = self.conn
        conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
                     "status,created_at) VALUES('P1','2026-09-13','SIM','s','{}','a3f8',"
                     "'frozen','t')")
        conn.commit()

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_register_and_transition(self):
        o = oms.register_order(self.conn, plan_id="P1", symbol="SH.600519", market="SH",
                              side="BUY", qty=100, price=1580.0, mode="SIM",
                              plan_hash="a3f8")
        self.assertEqual(o["status"], "draft")
        oms.transition(self.conn, o["client_order_id"], "frozen")
        oms.transition(self.conn, o["client_order_id"], "submitting")
        oms.transition(self.conn, o["client_order_id"], "submitted", broker_order_id="714")
        with self.assertRaises(ValueError):
            oms.transition(self.conn, o["client_order_id"], "draft")  # 非法回退

    def test_idempotent_open_order_per_symbol_side(self):
        o = oms.register_order(self.conn, plan_id="P1", symbol="SH.600519", market="SH",
                              side="BUY", qty=100, price=1580.0, mode="SIM", plan_hash="a3f8")
        with self.assertRaises(oms.DuplicateOpenOrder):
            oms.register_order(self.conn, plan_id="P1", symbol="SH.600519", market="SH",
                               side="BUY", qty=100, price=1580.0, mode="SIM", plan_hash="a3f8")

    def test_unknown_is_terminal_until_query(self):
        o = oms.register_order(self.conn, plan_id="P1", symbol="SH.600519", market="SH",
                              side="BUY", qty=100, price=1580.0, mode="SIM", plan_hash="a3f8")
        oms.transition(self.conn, o["client_order_id"], "frozen")
        oms.transition(self.conn, o["client_order_id"], "unknown")
        # unknown 只能被查询结果迁移到 submitted/filled/cancelled，不允许重发
        oms.transition(self.conn, o["client_order_id"], "submitted", broker_order_id="715")
```

- [ ] 步骤 2：FAIL；步骤 3：实现 `oms.py`：

```python
"""OMS（规格 §6.2）：状态机白名单 + 幂等三件套 + unknown 只查不重放。"""
import uuid
from datetime import datetime, timezone, timedelta

_TZ8 = timezone(timedelta(hours=8))

STATES = {"draft", "frozen", "submitting", "submitted", "partial", "filled",
          "cancelled", "rejected", "unknown"}
TRANSITIONS = {
    "draft": {"frozen"},
    "frozen": {"submitting"},
    "submitting": {"submitted", "unknown", "rejected"},
    "submitted": {"partial", "filled", "cancelled"},
    "partial": {"partial", "filled", "cancelled"},
    "unknown": {"submitted", "partial", "filled", "cancelled"},  # 仅查询结果可迁出
}
OPEN_STATES = {"draft", "frozen", "submitting", "submitted", "partial", "unknown"}


class DuplicateOpenOrder(Exception):
    pass


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def register_order(conn, plan_id, symbol, market, side, qty, price, mode, plan_hash):
    row = conn.execute(
        "SELECT 1 FROM orders WHERE plan_id=? AND symbol=? AND side=? AND status IN "
        f"({','.join('?' * len(OPEN_STATES))})",
        (plan_id, symbol, side, *OPEN_STATES)).fetchone()
    if row:
        raise DuplicateOpenOrder(f"{symbol} {side} 已有在途单（幂等三件套之二）")
    cid = uuid.uuid4().hex
    now = _now()
    conn.execute("INSERT INTO orders(client_order_id,plan_id,symbol,market,side,qty,price,"
                 "status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (cid, plan_id, symbol, market, side, qty, price, "draft", mode, now, now))
    conn.commit()
    return {"client_order_id": cid, "status": "draft"}


def transition(conn, client_order_id, to_state, broker_order_id=None, err=None):
    if to_state not in STATES:
        raise ValueError(f"未知状态 {to_state}")
    row = conn.execute("SELECT status FROM orders WHERE client_order_id=?",
                       (client_order_id,)).fetchone()
    if not row:
        raise ValueError(f"订单不存在 {client_order_id}")
    if to_state not in TRANSITIONS.get(row["status"], set()):
        raise ValueError(f"非法迁移 {row['status']} → {to_state}")
    conn.execute("UPDATE orders SET status=?, broker_order_id=COALESCE(?,broker_order_id),"
                 " err=COALESCE(?,err), updated_at=? WHERE client_order_id=?",
                 (to_state, broker_order_id, err, _now(), client_order_id))
    conn.commit()
```

- [ ] 步骤 4：PASS；`git commit -m "feat(core): OMS 状态机（迁移白名单+幂等在途唯一）"`

### 任务 4：broker.py —— 模拟盘适配器

**文件：** 修改 `broker.py`；测试追加 `tests/test_core_broker.py`

- [ ] 步骤 1：失败测试（注入假 call_tool；超时→unknown 语义；改单=撤+重下说明落在函数 docstring）：

```python
"""券商适配器：参数拼装、信封信任 call_tool 归一化、超时语义。全部假通道。"""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import broker  # noqa: E402


class BrokerTest(unittest.TestCase):
    def test_place_assembles_required_args(self):
        seen = {}

        def fake_call(name, args, timeout=30):
            seen.update(name=name, args=args)
            return {"order_id": "7142358"}

        out = broker.place(fake_call, acc_id="A1", market="SH", symbol="600519",
                           side="BUY", qty=100, price=1580.0, order_type=1)
        self.assertEqual(out["broker_order_id"], "7142358")
        self.assertEqual(seen["name"], "sim_trade_input_order")
        self.assertEqual(seen["args"]["symbol"], "600519")
        self.assertEqual(seen["args"]["order_side"], 1)  # 1=Buy 2=Sell（schema 明文）
        self.assertEqual(seen["args"]["qty"], 100)

    def test_timeout_maps_to_unknown(self):
        def fake_call(name, args, timeout=30):
            raise TimeoutError("timed out")

        out = broker.place(fake_call, acc_id="A1", market="SH", symbol="600519",
                           side="BUY", qty=100, price=1580.0, order_type=1)
        self.assertEqual(out["status"], "unknown")   # 绝不重放，交上层查询
        self.assertIsNone(out.get("broker_order_id"))

    def test_positions_passthrough(self):
        def fake_call(name, args, timeout=30):
            assert name == "sim_trade_position_list" and args["market"] == 1
            return {"position_list": [{"code": "600519", "qty": 400}]}

        rows = broker.positions(fake_call, acc_id="A1", market_id=1)
        self.assertEqual(rows[0]["code"], "600519")
```

- [ ] 步骤 2：FAIL；步骤 3：实现（`broker.py` 追加）：

```python
def place(call, acc_id, market, symbol, side, qty, price, order_type=1, timeout=30):
    """下单。铁律：超时/异常 → {"status": "unknown"}，绝不重发（P4 教训代码化）。
    改单需求一律「撤单 + 重新下单」：sim_trade_modify_order 间歇性 -5（TOOL-LIMITS）。"""
    side_code = 1 if side == "BUY" else 2
    args = {"acc_id": acc_id, "market": market, "symbol": symbol.lstrip("SH.ZBJ."),
            "order_type": order_type, "order_side": side_code, "qty": int(qty)}
    if price:
        args["price"] = float(price)
    try:
        data = call(TOOLS["place"], args, timeout=timeout) or {}
        return {"status": "submitted", "broker_order_id": str(data.get("order_id") or ""),
                "args": args}
    except TimeoutError as error:
        return {"status": "unknown", "broker_order_id": None, "err": str(error)[:120],
                "args": args}
    except Exception as error:  # noqa: BLE001 —— 拒单/业务失败如实上抛信息
        return {"status": "rejected", "broker_order_id": None, "err": str(error)[:160],
                "args": args}


def cancel(call, acc_id, market, order_id, timeout=30):
    return call(TOOLS["cancel"], {"acc_id": acc_id, "order_id": str(order_id),
                                  "market": market}, timeout=timeout)


def positions(call, acc_id, market_id, timeout=30):
    data = call(TOOLS["positions"], {"acc_id": acc_id, "market": market_id},
                timeout=timeout) or {}
    return data.get("position_list") or []


def accounts(call, timeout=30):
    return call(TOOLS["accounts"], {}, timeout=timeout) or {}
```

- [ ] 步骤 4：PASS；`git commit -m "feat(core): 模拟盘适配器（下单/撤单/持仓，超时→unknown）"`

### 任务 5：execute_plan —— 执行编排

**文件：** 创建 `execute.py`；测试 `tests/test_core_execute.py`

- [ ] 步骤 1：失败测试（冻结计划 → 逐单预检（含一条被规则5拒绝）→ 提交 → 部分成交 → 日内熔断撤余单）：

```python
"""执行编排：预检→提交→成交回写；被拒跳过；熔断撤余单。全部假 broker。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import execute, oms, store  # noqa: E402


class ExecuteTest(unittest.TestCase):
    def test_flow_with_blocked_order_and_halt(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
                     "status,created_at) VALUES('P1','2026-09-13','SIM','s','{}','a3f8',"
                     "'frozen','t')")
        conn.commit()
        big = oms.register_order(conn, plan_id="P1", symbol="SH.600519", market="SH",
                                 side="BUY", qty=100, price=1580.0, mode="SIM", plan_hash="a3f8")
        small = oms.register_order(conn, plan_id="P1", symbol="SZ.300750", market="SZ",
                                   side="BUY", qty=100, price=201.8, mode="SIM", plan_hash="a3f8")

        def broker_call(name, args, timeout=30):
            return {"order_id": "7149999"}

        result = execute.run(
            conn, plan_id="P1", plan_hash="a3f8",
            ctx={"mode": "SIM", "kill_path": "/nonexistent", "equity": 1_000_000.0,
                 "positions_value": {"SH.600519": 260000.0}, "positions_count": 2,
                 "day_pnl_pct": -0.005, "is_trading_day": True,
                 "config": {"risk_per_trade": 0.01, "max_positions": 5,
                            "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03}},
            broker_call=broker_call,
            price_of=lambda s: {"SH.600519": 1580.0, "SZ.300750": 201.8}[s],
            stop_dist_of=lambda s: 20.0)
        self.assertEqual(result["blocked"], 1)   # 600519 被规则5+规则7 拦截
        self.assertGreaterEqual(result["submitted"], 1)
        checks = store.risk_checks_by_plan(conn, "P1")
        self.assertTrue(any(not c["allowed"] for c in checks))
```

- [ ] 步骤 2：FAIL；步骤 3：实现 `execute.py`：

```python
"""执行编排（规格 §6.2/§七）：逐单 pre_trade_checks → broker.place → 状态机。
被拒订单跳过并落 risk_checks；熔断触发时撤销计划内未提交订单。"""
from datetime import datetime, timezone, timedelta

from . import broker, oms, risk, store

_TZ8 = timezone(timedelta(hours=8))


def _now():
    return datetime.now(_TZ8).strftime("%Y-%m-%d %H:%M:%S")


def run(conn, plan_id, plan_hash, ctx, broker_call, price_of, stop_dist_of):
    plan = store.get_plan(conn, plan_id)
    if plan["status"] not in ("frozen", "approved") or plan["content_hash"] != plan_hash:
        raise ValueError("计划未冻结或 hash 不匹配（规则 8 前置校验）")
    store.upsert_plan_status(conn, plan_id, "executing")
    orders = store.get_orders_by_plan(conn, plan_id)
    blocked = submitted = 0
    halted = False
    for o in orders:
        order = {"symbol": o["symbol"], "side": o["side"], "qty": o["qty"],
                 "price": o["price"] or price_of(o["symbol"]), "mode": o["mode"],
                 "plan_id": plan_id, "plan_hash": plan_hash,
                 "stop_dist": stop_dist_of(o["symbol"])}
        verdict = risk.pre_trade_checks(order, dict(ctx, plan_hash=plan_hash,
                                                    plan_status="executing"))
        store.insert_risk_check(conn, plan_id, o["symbol"], verdict.rule,
                                verdict.allowed, verdict.reason)
        if not verdict.allowed:
            blocked += 1
            if verdict.rule == 7:
                halted = True
            oms.transition(conn, o["client_order_id"], "cancelled",
                           err="risk:" + verdict.reason)
            continue
        oms.transition(conn, o["client_order_id"], "submitting")
        out = broker.place(broker_call, acc_id=ctx.get("acc_id", "SIM"),
                           market=o["market"], symbol=o["symbol"].split(".")[-1],
                           side=o["side"], qty=o["qty"], price=o["price"])
        if out["status"] == "submitted":
            oms.transition(conn, o["client_order_id"], "submitted",
                           broker_order_id=out["broker_order_id"])
            submitted += 1
        else:
            oms.transition(conn, o["client_order_id"], out["status"], err=out.get("err"))
            if out["status"] == "rejected":
                store.kv_set(conn, "last_reject", {"order": o["client_order_id"],
                                                   "err": out.get("err")})
    if halted:
        store.set_halt(conn, True, reason="daily_loss")
        for o in store.get_open_orders(conn, plan_id):
            if o["status"] in ("draft", "frozen"):
                oms.transition(conn, o["client_order_id"], "cancelled", err="halt")
    return {"submitted": submitted, "blocked": blocked, "halted": halted}
```

- [ ] 步骤 4：PASS；`git commit -m "feat(core): 执行编排（预检→提交→熔断撤余单）"`

### 任务 6：reconcile.py + tca.py

**文件：** 创建 `reconcile.py`、`tca.py`；测试 `tests/test_core_reconcile.py`

- [ ] 步骤 1：失败测试：

```python
"""对账（数量不一致即差异）与 TCA（到达价 vs 成交价 bps）。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import reconcile, store, tca  # noqa: E402


class ReconcileTest(unittest.TestCase):
    def test_qty_mismatch_is_diff_and_halts(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        local = {"SH.600519": 300}
        broker = {"SH.600519": 320}
        diffs = reconcile.compare(conn, local, broker, value_tolerance=0.005)
        self.assertEqual(diffs[0]["symbol"], "SH.600519")
        self.assertEqual(diffs[0]["qty_diff"], -20)

    def test_tca_bps(self):
        bps = tca.slippage_bps(arrival=1580.0, filled=1580.30, side="BUY")
        self.assertAlmostEqual(bps, 1.898, places=2)


class TcaStoreTest(unittest.TestCase):
    def test_record_and_aggregate(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        tca.record(conn, "ORD-1", "SH.600519", arrival=100.0, filled=100.4, side="BUY")
        agg = tca.aggregate(conn)
        self.assertEqual(agg["count"], 1)
        self.assertGreater(agg["avg_bps"], 0)
```

- [ ] 步骤 2：FAIL；步骤 3：实现 `reconcile.py`：

```python
"""对账（规格 §6.3）：数量不一致即差异；价值口径差异 > 容差为差异；
差异 → 上层置 critical 告警 + 暂停执行（本模块只判定，不自动平仓）。"""


def compare(conn, local_positions, broker_positions, value_tolerance=0.005):
    symbols = set(local_positions) | set(broker_positions)
    diffs = []
    for s in sorted(symbols):
        l = local_positions.get(s)
        b = broker_positions.get(s)
        if l is None or b is None:
            diffs.append({"symbol": s, "local": l, "broker": b, "kind": "missing_side"})
            continue
        lq, bq = int(l["qty"] if isinstance(l, dict) else l), int(b["qty"] if isinstance(b, dict) else b)
        if lq != bq:
            diffs.append({"symbol": s, "local": lq, "broker": bq, "qty_diff": lq - bq,
                          "kind": "qty"})
        else:
            lv = float(l.get("value", 0) or 0) if isinstance(l, dict) else 0.0
            bv = float(b.get("value", 0) or 0) if isinstance(b, dict) else 0.0
            if lv and bv and abs(lv - bv) > value_tolerance * max(lv, bv):
                diffs.append({"symbol": s, "local_value": lv, "broker_value": bv,
                              "kind": "value"})
    return diffs
```

实现 `tca.py`：

```python
"""TCA（规格 §6.3）：到达价 vs 成交价，滑点 bps，按标的/日聚合。"""
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS tca(
  client_order_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, day TEXT NOT NULL,
  arrival REAL NOT NULL, filled REAL NOT NULL, side TEXT NOT NULL,
  bps REAL NOT NULL, created_at TEXT NOT NULL);
"""


def slippage_bps(arrival, filled, side):
    direction = 1 if side == "BUY" else -1
    return (filled - arrival) / arrival * 10000 * direction


def _ensure(conn):
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("需要 sqlite 连接")
    conn.executescript(SCHEMA)


def record(conn, client_order_id, symbol, arrival, filled, side):
    import datetime as dt
    _ensure(conn)
    bps = slippage_bps(arrival, filled, side)
    day = dt.date.today().isoformat()
    conn.execute("INSERT OR REPLACE INTO tca VALUES(?,?,?,?,?,?,?,?)",
                 (client_order_id, symbol, day, arrival, filled, side, bps,
                  dt.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return bps


def aggregate(conn):
    _ensure(conn)
    row = conn.execute("SELECT COUNT(*) AS n, AVG(bps) AS avg FROM tca").fetchone()
    return {"count": row["n"], "avg_bps": round(row["avg"] or 0.0, 2)}
```

- [ ] 步骤 4：PASS；`git commit -m "feat(core): 对账判定与 TCA 落库聚合"`

### 任务 7：CLI 子命令

**文件：** 修改 `cli.py`；测试追加 `tests/test_core_cli.py`

- [ ] 步骤 1：失败测试（`plan-build` 用注入假 broker_positions 输出冻结计划 JSON；`reconcile-diff` 离线比对两个 JSON 文件）；步骤 2：FAIL；步骤 3：`cli.py` 追加子命令：

```python
    s = sub.add_parser("plan-build", help="生成并冻结计划（目标权重 JSON 内联提供）")
    s.add_argument("--mode", default="SIM")
    s.add_argument("--strategy", default="momentum_value_top5")
    s.add_argument("--target", required=True, help='JSON，如 {"SH.600519": 0.5}')
    s.add_argument("--prices", required=True, help="JSON，标的→限价")
    s.add_argument("--as-of", required=True)
    _add_db(s)

    s = sub.add_parser("reconcile-diff", help="离线比对本地与券商持仓 JSON")
    s.add_argument("--local", required=True)
    s.add_argument("--broker", required=True)
    _add_db(s)
```

分派：

```python
        elif args.cmd == "plan-build":
            from . import planner
            target = json.loads(args.target)
            prices = json.loads(args.prices)

            def broker_positions(mode):
                return {}, 1_000_000.0  # 无券商通道时的离线口径；真实通道走 daemon（WP4）

            result = planner.build_and_freeze(conn, mode=args.mode, strategy_id=args.strategy,
                                              target=target, broker_positions=broker_positions,
                                              prices=prices, as_of=args.as_of)
        elif args.cmd == "reconcile-diff":
            from . import reconcile
            result = {"diffs": reconcile.compare(
                conn, json.loads(Path(args.local).read_text()),
                json.loads(Path(args.broker).read_text()))}
```

（`Path` 已在 cli 顶部 import argparse 处补充 `from pathlib import Path`。）

- [ ] 步骤 4：PASS；`git commit -m "feat(core): plan-build/reconcile-diff CLI"`

### 任务 8：离线端到端（状态机全景）

**文件：** 创建 `tests/test_core_wp3_e2e.py`

- [ ] 步骤 1：编写并跑通（场景：冻结→批准→执行（1 成交/1 拒/1 unknown）→查询迁出 unknown→对账差异→halt→kill 后全拒）：

```python
"""WP3 端到端：规格 §6.2 状态机全景 + 熔断/kill 语义。全部假 broker。"""
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "core" / "python"))
from trading_core import execute, oms, risk, store  # noqa: E402


class E2eTest(unittest.TestCase):
    def test_full_state_machine(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conn = store.connect(str(Path(tmp.name) / "t.sqlite"))
        conn.execute("INSERT INTO plans VALUES('P1','2026-09-13','SIM','s','{}','h1',"
                     "'frozen','t',NULL,NULL)")
        conn.commit()
        for i, sym in enumerate(("SH.600519", "SZ.300750", "SH.601899")):
            o = oms.register_order(conn, "P1", sym, sym.split(".")[0], "BUY",
                                   100, 100.0 + i, "SIM", "h1")
            oms.transition(conn, o["client_order_id"], "frozen")

        calls = {"n": 0}

        def broker_call(name, args, timeout=30):
            calls["n"] += 1
            if calls["n"] == 2:
                raise TimeoutError("sim timeout")   # 第二单 → unknown
            return {"order_id": str(710000 + calls["n"])}

        ctx = {"mode": "SIM", "kill_path": "/nonexistent", "equity": 1_000_000.0,
               "positions_value": {}, "positions_count": 0, "day_pnl_pct": -0.001,
               "is_trading_day": True,
               "config": {"risk_per_trade": 0.01, "max_positions": 5,
                          "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03}}
        result = execute.run(conn, "P1", "h1", ctx, broker_call,
                             price_of=lambda s: 100.0, stop_dist_of=lambda s: 5.0)
        self.assertEqual(result["submitted"], 2)
        self.assertEqual(result["blocked"], 0)
        # unknown 迁出：查询后转 submitted
        unk = [o for o in store.get_orders_by_plan(conn, "P1") if o["status"] == "unknown"]
        self.assertEqual(len(unk), 1)
        oms.transition(conn, unk[0]["client_order_id"], "submitted", broker_order_id="719999")

        # kill switch 生效后：新预检全拒
        with tempfile.TemporaryDirectory() as d:
            kill = Path(d) / "trading-kill"; kill.write_text("")
            v = risk.pre_trade_checks(
                {"symbol": "SH.600519", "side": "BUY", "qty": 100, "price": 100.0,
                 "mode": "SIM", "plan_id": "P1", "plan_hash": "h1", "stop_dist": 5.0},
                dict(ctx, kill_path=str(kill), plan_hash="h1", plan_status="frozen"))
            self.assertFalse(v.allowed)
            self.assertEqual(v.rule, 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] 步骤 2：PASS；`git commit -m "test(core): WP3 状态机端到端"`

### 任务 9：真实 sim 冒烟（人工 runbook，用户在场）

- [ ] **步骤 1（手动，非测试套件）**：按 P4 口径执行并在 `docs/P4-live-trading.md` 的模拟联调记录追加：
  1. `python -m trading_core calendar --market HK --start 2026-09-01 --end 2026-09-30`（日历就绪）；
  2. 对话内由用户逐笔确认后，用最小数量（1 手/1 股）走 `sim_trade_input_order` 下单 → 查询受理 → `sim_trade_cancel_order` 撤单 → 核对资金释放；
  3. 超时场景故意观察一次（断网 60s）：确认适配器返回 unknown 而非重发；
  4. 记录原始响应 JSON 到 P4 文档联调小节。
- [ ] **步骤 2：WP3 验收记录**追加到本文件末尾（测试输出与冒烟 JSON 原文）。

### WP3 验收记录（执行时填写）

- 离线端到端测试输出：（执行时粘贴）
- sim 冒烟原始响应（脱敏后）：（执行时粘贴）
