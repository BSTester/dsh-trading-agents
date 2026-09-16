"""WP9 任务 8：端到端假时钟两交易日 —— sim 自动流水线闭环（零人工）。

真实调度结构是「父进程 tick → cmd 形式作业的子进程 CLI」，``tick(now=…)`` 的注入到
不了子进程；本测试因此：
  * 用**进程内假 runner** 替掉 ``daemon._subprocess_runner``：保留真实 CLI 入口
    （``cli.main`` 解析同一批 argv），同时让 ``DSH_FAKE_NOW`` 与券商 monkeypatch 对
    「子进程」生效；
  * 走**真实服务调度器** ``server.scheduler.build_tick``（作业链 + 同轮指令轮询），
    而不是只调 ``daemon.tick``——「写指令 → 同轮消费」是 WP9 任务 7 的接线，必须在
    端到端里被覆盖；
  * 数据作业（sync/fundamentals/quality/factors_snapshot）不在本链路验证范围：数据以
    种子（日历/bars）代替，假 runner 对它们返回 0（等价于「数据基座已就绪」）。

五个场景把链路讲完整（每条都是平台真实行为，不是为测试方便而设计的假路径）：

  ① 满仓单（**定量自洽后正常成交**）：策略上限把 1/N 压到 max_position_pct，计划再按
     风险预算定量 → 规则 4/5 放行 → **券商真实收到下单**、订单 submitted；
  ② 小权重单：策略给出 0.8% 目标权重（100 股 / 8,000 元）→ 权重臂成为约束臂 →
     同样提交（与 ① 互补：① 由预算臂定量，② 由权重臂定量）；
  ③ 对账差异：券商持仓与本地台账不一致 → critical 差异 → 自动暂停 → 次日自动执行被
     守卫 4（熔断）拦下（**差异只暂停不平仓**，这是规格 §6.3 的既定语义）；
  ④ 关闭态回归：``enabled=false`` 时同一时间线零自动产物；
  ⑤ 假时钟告警：``DSH_FAKE_NOW`` 生效时工作台可见一条 warn（生产防误用）；
  ⑥ **硬规则反证**：人为放大 qty 的超限订单仍被规则 4 拦下、券商零调用
     ——定量口径的修复**没有削弱**任何一条硬拦截。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from datetime import date, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from server import scheduler  # noqa: E402
from trading_core import cli, daemon, oms, store, strategies  # noqa: E402

PREV = "2026-09-15"
D1 = "2026-09-16"
D2 = "2026-09-17"
D3 = "2026-09-18"
SYMBOL = "SH.600519"
OUTSIDE = "SH.601398"      # 受管集合之外的持仓（用户手工持有）：自动链不得触碰
AUTO_COMMANDS = ("plan-auto", "auto-execute", "reconcile-daily")


class _SmallWeightStrategy:
    """小额目标权重的测试替身：验证**权重臂**成为约束臂时的提交路径。

    ``watchlist_rsi`` 的单票上限是 max_position_pct(25%)，在单标的关注池下算出 25% ——
    该权重再由计划侧的风险预算定量压到预算内（场景 ①）。本替身给出更小的权重
    （0.8%），使 min(权重定量, 预算定量) 由**权重臂**决定，把「小单照常提交」这一支
    单独钉住。
    """

    id = "wp9_small_weight"

    def universe(self, conn, as_of, home=None):
        return [SYMBOL]

    def target_weights(self, conn, as_of, home=None):
        return {SYMBOL: 0.008}   # 100 股 / 8,000 元：规则 4 的 1 万元预算之内


class AutoPipelineE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self._saved_env = {key: os.environ.get(key)
                           for key in ("DSH_HOME", daemon.FAKE_NOW_ENV)}
        os.environ["DSH_HOME"] = str(self.home)
        os.environ.pop(daemon.FAKE_NOW_ENV, None)
        self.addCleanup(self._restore_env)
        daemon._fake_now_alerted = False
        self.addCleanup(setattr, daemon, "_fake_now_alerted", False)

        # 父进程与 CLI 子进程必须落同一个库：CLI 按 DSH_HOME 推 db_path()
        self.conn = store.connect(store.db_path(str(self.home)))
        self.addCleanup(self.conn.close)
        self.cmd_root = self.home / "trading-commands"

        strategies.REGISTRY[_SmallWeightStrategy.id] = _SmallWeightStrategy()
        self.addCleanup(strategies.REGISTRY.pop, _SmallWeightStrategy.id, None)

    def _restore_env(self):
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---- 种子 ----

    def _config(self, enabled=True, strategy="watchlist_rsi"):
        # watchlist 是**池键名**（I1）：缺省池就是顶层 "watchlist" 列表，故显式写
        # "watchlist"（值 "SH" 会被当成一个不存在的池键 → 该策略当日 fail-closed 跳过）
        (self.home / "trading-platform.json").write_text(json.dumps({
            "watchlist": [SYMBOL],
            "auto_pipeline": {
                "enabled": enabled,
                "strategies": [{"market": "SH", "strategy": strategy,
                                "watchlist": "watchlist"}],
                "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
                "reconcile_at": "19:00"}}, ensure_ascii=False), encoding="utf-8")

    def _mode(self, value="sim"):
        (self.home / "trading-account-mode").write_text(value, encoding="utf-8")

    def _calendar(self):
        store.upsert_calendar(self.conn, "SH", [
            {"day": day, "trade_date_type": "WHOLE", "trade_second": 14400}
            for day in (PREV, D1, D2, D3)])

    def _bars(self):
        """30 根：前 28 根缓涨 + 末两根急跌 → 末根 RSI 下穿 25（rsi_signal 是穿越
        事件，平稳趋势不产出 BUY）；最后一根日期 = D1（数据就绪门）。"""
        days = [f"2026-08-{d:02d}" for d in range(1, 29)] + [PREV, D1]
        closes = [100.0 + 0.5 * i for i in range(28)] + [95.0, 80.0]
        store.upsert_bars(self.conn, SYMBOL, "1d", [
            {"t": day, "o": close, "h": close, "l": close, "c": close, "v": 1000.0}
            for day, close in zip(days, closes)], source="test")

    def _flat_bars(self, last=D2, close=200.0, half_range=0.25, count=30):
        """平盘窄幅 30 根（末日 = D2）：RSI 无穿越 → HOLD（策略不给权重）。

        用途是场景 ⑦：策略**不再**选中该标的，受管集合必须让它进入 diff 生成清仓单。
        窄幅使 ATR = 0.5 → 止损距离 1.0，规则 4 对 1700 股只计 1,700 元，从而让判定
        前进到规则 5（本场景要验的正是超上限仓位的退出）。
        """
        end = date.fromisoformat(last)
        days = [(end - timedelta(days=count - 1 - i)).isoformat() for i in range(count)]
        store.upsert_bars(self.conn, SYMBOL, "1d", [
            {"t": d, "o": close, "h": close + half_range, "l": close - half_range,
             "c": close, "v": 1000.0} for d in days], source="test")

    # ---- 假件 ----

    def _broker(self, held=0, extra=None):
        """sim 假券商：账户/持仓/资金/下单/订单历史。

        ``held`` = 受管标的（watchlist 内）持仓；``extra`` = 受管集合**之外**的持仓
        （用户手工持有，用于验证自动链不去清理它）。都不做成交回填——场景 ⑦ 只跑
        「计划 → 执行」两跳，不跑对账（对账口径由场景 ③ 覆盖）。
        """
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append((name, dict(args or {})))
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3,
                                      "account_title": "模拟A股"}]}
            if name == "sim_trade_position_list":
                rows = [{"symbol": SYMBOL, "qty": held}] if held else []
                rows += [{"symbol": s, "qty": q} for s, q in (extra or {}).items()]
                return {"positions": rows}
            if name == "sim_trade_cash_info":
                return {"balance": 1_000_000.0, "total_asset": 1_000_000.0}
            if name == "sim_trade_input_order":
                return {"order_id": f"SIM-O{len(calls)}"}
            if name == "sim_trade_history_order_list":
                return {"orders": []}
            raise AssertionError(f"意外券商工具 {name}")

        return call, calls

    def _runner(self):
        """进程内假 runner：CLI 入口真实、时间与券商注入生效；数据作业以种子代替。"""
        dispatched = []

        def runner(cmd):
            resolved = daemon.resolve_command(cmd, str(self.home))
            if resolved is None:
                return {"skipped": "关注池为空"}
            name = resolved[0]
            if name not in AUTO_COMMANDS:
                return 0  # 数据作业：本 e2e 用种子数据代替（见文件头）
            dispatched.append(name)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(resolved)
            if code:
                raise AssertionError(f"{name} 退出码 {code}：{out.getvalue()[:400]}")
            return code

        return runner, dispatched

    @contextlib.contextmanager
    def _running(self, held=0, extra=None):
        """整段两日时间线共用一个假券商与假 runner。"""
        call, broker_calls = self._broker(held=held, extra=extra)
        runner, dispatched = self._runner()
        with mock.patch("trading_datasource.futu_mcp.call_tool", call), \
                mock.patch.object(daemon, "_subprocess_runner", runner):
            yield broker_calls, dispatched

    def _tick(self, stamp):
        """一轮真实服务调度（作业链 + 同轮指令轮询），时钟走 DSH_FAKE_NOW。"""
        os.environ[daemon.FAKE_NOW_ENV] = stamp
        return scheduler.build_tick(str(self.home), conn=self.conn)()

    def _day_one(self):
        self._tick(f"{D1} 16:20:00")   # 收盘链：数据就绪 → build_plan
        self._tick(f"{D1} 19:00:00")   # GLOBAL 链：reconcile → tca → digest

    # ---- 观测 ----

    def _pending(self):
        root = self.cmd_root / "pending"
        return sorted(p.name for p in root.glob("*.json")) if root.exists() else []

    def _processed(self):
        root = self.cmd_root / "processed"
        return sorted(p.name for p in root.glob("*.json")) if root.exists() else []

    def _plans(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT plan_id, as_of, mode, status, origin, market FROM plans"
            " ORDER BY created_at, rowid").fetchall()]

    def _orders(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT symbol, side, qty, price, status, err FROM orders"
            " ORDER BY rowid").fetchall()]

    def _risk_checks(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT rule, allowed, reason FROM risk_checks ORDER BY id").fetchall()]

    def _places(self, broker_calls):
        return [(name, args) for name, args in broker_calls
                if name == "sim_trade_input_order"]

    # ---- ① 满仓单：定量自洽 → 正常成交 ----

    def test_full_chain_fills_end_to_end(self):
        """权重 1.0 → 策略上限 0.25 → 风险预算定量 → 规则 4/5 放行 → 券商收到下单。

        定量算术（种子 bars 为 h=l=o=c，TR 由相邻收盘差决定）：
          最近 14 个 TR = 12×0.5（缓涨段）+ 18.5（95←113.5）+ 15.0（80←95）= 39.5
          ATR = 39.5 ÷ 14 ≈ 2.8214 → 止损距离 = 2 × ATR ≈ 5.6429
          权重定量 = 1e6 × 0.25 ÷ 80 = 3125 → 整手 3100
          预算定量 = floor(1e6 × 1% ÷ 5.6429 ÷ 100) × 100 = 1700
          下单量 = min(3100, 1700) = 1700（预算臂约束；名义 13.6% ≤ 规则 5 的 25%）
        """
        self._config(enabled=True)
        self._mode()
        self._calendar()
        self._bars()

        with self._running() as (broker_calls, dispatched):
            self._tick(f"{D1} 16:20:00")
            # 收盘链产物：auto 计划冻结 + 订单登记（执行链路的可执行物）
            plans = self._plans()
            self.assertEqual(len(plans), 1, plans)
            self.assertEqual((plans[0]["origin"], plans[0]["market"],
                              plans[0]["status"], plans[0]["as_of"]),
                             ("auto", "SH", "frozen", D1))
            orders = self._orders()
            self.assertEqual(len(orders), 1, orders)
            self.assertEqual((orders[0]["side"], orders[0]["qty"]), ("BUY", 1700), orders)

            self._tick(f"{D1} 19:00:00")
            self.assertIsNotNone(store.kv_get(self.conn, "reconcile:latest"))
            self.assertIsNotNone(store.kv_get(self.conn, "daily:digest"))

            self._tick(f"{D2} 09:35:00")
            # 指令写出并**同轮消费**
            self.assertEqual(self._pending(), [])
            self.assertEqual(len(self._processed()), 1, self._processed())
            self.assertTrue(store.kv_get(self.conn, f"auto_exec:SH:{D2}"))

        orders = self._orders()
        self.assertEqual(orders[0]["status"], "submitted", orders)
        self.assertIsNone(orders[0]["err"], orders)
        # 风控留痕：全部放行（Verdict 只在拒绝时带规则号，放行为 rule=0）
        verdicts = self._risk_checks()
        self.assertTrue(any(v["allowed"] for v in verdicts), verdicts)
        self.assertFalse(any(not v["allowed"] for v in verdicts), verdicts)
        places = self._places(broker_calls)
        self.assertEqual(len(places), 1, broker_calls)     # 券商真实收到下单
        self.assertEqual(places[0][1]["qty"], 1700)
        self.assertEqual(sorted(set(dispatched)),
                         ["auto-execute", "plan-auto", "reconcile-daily"])

    # ---- ② 小权重单：规则 4 放行 → 券商真实收到下单 ----

    def test_small_weight_order_submits_end_to_end(self):
        self._config(enabled=True, strategy=_SmallWeightStrategy.id)
        self._mode()
        self._calendar()
        self._bars()

        with self._running() as (broker_calls, _):
            self._day_one()
            self._tick(f"{D2} 09:35:00")

        orders = self._orders()
        self.assertEqual(len(orders), 1, orders)
        self.assertEqual(orders[0]["qty"], 100, orders)      # 0.8% × 1e6 / 80 = 100 股
        self.assertEqual(orders[0]["status"], "submitted", orders)
        places = self._places(broker_calls)
        self.assertEqual(len(places), 1, broker_calls)
        self.assertEqual(places[0][1]["qty"], 100)
        self.assertTrue(store.kv_get(self.conn, f"auto_exec:SH:{D2}"))

    # ---- ③ 对账差异 → 熔断 → 自动执行被守卫 4 拦下 ----

    def test_reconcile_diff_halts_and_blocks_auto_execution(self):
        self._config(enabled=True, strategy=_SmallWeightStrategy.id)
        self._mode()
        self._calendar()
        self._bars()

        # 券商持 12490 股，本地台账（OMS fills）零记录 → 对账必然报差异
        with self._running(held=12490) as (broker_calls, _):
            self._day_one()
            self.assertTrue(store.is_halted(self.conn), "对账差异应自动暂停执行")
            self._tick(f"{D2} 09:35:00")

        # 守卫 4（熔断）在写指令之前拦下：零指令、零下单、订单仍停在 draft
        self.assertEqual(self._pending(), [])
        self.assertEqual(self._processed(), [])
        self.assertIsNone(store.kv_get(self.conn, f"auto_exec:SH:{D2}"))
        self.assertEqual(self._places(broker_calls), [])
        self.assertEqual(self._orders()[0]["status"], "draft")
        self.assertTrue(any("熔断生效" in a["detail"] for a in self._alerts()))

    def _alerts(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT level, title, detail FROM alerts ORDER BY id").fetchall()]

    # ---- ④ 关闭态回归：零自动产物 ----

    def test_disabled_pipeline_produces_no_artifacts(self):
        self._config(enabled=False)
        self._mode()
        self._calendar()
        self._bars()

        with self._running() as (broker_calls, dispatched):
            self._day_one()
            self._tick(f"{D2} 09:35:00")

        self.assertEqual(self._plans(), [])
        self.assertEqual(self._orders(), [])
        self.assertEqual(self._pending(), [])
        self.assertEqual(self._processed(), [])
        self.assertIsNone(store.kv_get(self.conn, "reconcile:latest"))
        self.assertIsNone(store.kv_get(self.conn, "daily:digest"))
        self.assertEqual(dispatched, [])            # 关闭态不派发任何自动作业
        self.assertEqual(self._places(broker_calls), [])

    # ---- ⑤ 假时钟告警：工作台可见 ----

    def test_fake_clock_alert_visible(self):
        self._config(enabled=False)
        self._mode()
        self._calendar()
        self._bars()

        with self._running():
            self._day_one()

        rows = [a for a in self._alerts() if a["title"] == "假时钟生效"]
        self.assertEqual(len(rows), 1, rows)        # 每进程一次
        self.assertEqual(rows[0]["level"], "warn")
        self.assertIn(daemon.FAKE_NOW_ENV, rows[0]["detail"])

    # ---- ⑥ 硬规则反证：人为放大 qty 仍被规则 4 拦下 ----

    def test_oversized_order_still_blocked_by_risk_rules(self):
        """定量修复**不削弱**硬规则：人为放大的超限订单仍被拦、券商零调用。

        构造方式刻意绕过计划侧定量（直接登记 100,000 股 @80 的订单）——模拟
        「定量逻辑被绕过 / 被喂了宽松风险配置」的最坏情形，验证规则 4 是最后防线：
          单笔风险 = 100,000 × 止损距离(≈5.6429) ≈ 564,290 > 权益 × 1% = 10,000
          （且 100,000 × 80 = 8,000,000 也远超规则 5 的权益 × 25%）
        取不到 ATR 时口径更严（全额名义），本反证在两种情形下都成立。
        """
        self._config(enabled=False)   # 不经自动链，直接构造超限订单
        self._mode()
        self._calendar()
        self._bars()
        self.conn.execute(
            "INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,status,"
            "created_at,origin,market) VALUES('PLN-BIG',?,'sim','s','{}','hash-big',"
            "'frozen','2026-09-16 16:20:00','manual',NULL)", (D1,))
        self.conn.commit()
        order = oms.register_order(self.conn, "PLN-BIG", SYMBOL, "SH", "BUY",
                                   100000, 80.0, "sim", "hash-big")
        call, broker_calls = self._broker()

        out = daemon._execute_plan(self.conn, str(self.home), {"plan_hash": "hash-big"},
                                   broker_call=call, equity=1_000_000.0, today=D2,
                                   calendar_ok=True)

        self.assertTrue(out["ok"], out)
        self.assertEqual((out["submitted"], out["blocked"]), (0, 1), out)
        row = self.conn.execute("SELECT status, err FROM orders WHERE client_order_id=?",
                                (order["client_order_id"],)).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertIn("risk:", row["err"] or "")
        self.assertEqual(self._places(broker_calls), [])   # 拦截在触达券商之前
        verdicts = self._risk_checks()
        self.assertTrue(any(v["rule"] == 4 and not v["allowed"] for v in verdicts), verdicts)


    # ---- ⑦ 退出链路：受管集合让「不再给权重」的持仓被清仓 ----

    def test_exit_chain_sells_dropped_holding_end_to_end(self):
        """规格 §4.2 第 6/7 点：策略不再选中的已持仓标的 → 次日计划 SELL → 执行成交。

        构造：券商持有 SYMBOL 1700 股（价 200 → 340,000 = 权益 34%，**超单票上限**），
        另持有受管集合之外的 ``SH.601398`` 500 股（用户手工持仓）。D2 平盘 bars →
        策略 target 为空 → 受管集合让 diff 看到已持仓 → 生成清仓单 → D3 09:35 执行。

        钉住的三件事：
          * 清仓单真的生成并到达券商（修复前该单根本不生成——只买不退）；
          * 规则 5 对 34% 名义的清仓单**放行**——执行侧「券商持仓 + 卖出方向折算」的
            端到端证据（同一单在离线 ctx 下必被规则 5 拦，反证见
            ``tests/test_wp9_exec_ctx.py``）；
          * 受管集合之外的持仓零订单（不清理用户手工持仓）。

        本场景刻意**不跑 19:00 对账**：受管外持仓与本地台账必然不同，会按规格 §6.3
        触发熔断（那条语义由场景 ③ 覆盖），与退出路径的验证无关。
        """
        self._config(enabled=True)
        self._mode()
        self._calendar()
        self._flat_bars()

        with self._running(held=1700, extra={OUTSIDE: 500}) as (broker_calls, _):
            self._tick(f"{D2} 16:20:00")
            plans = self._plans()
            self.assertEqual(len(plans), 1, plans)
            self.assertEqual((plans[0]["origin"], plans[0]["market"], plans[0]["as_of"]),
                             ("auto", "SH", D2), plans)
            orders = self._orders()
            self.assertEqual([(o["symbol"], o["side"], o["qty"]) for o in orders],
                             [(SYMBOL, "SELL", 1700)], orders)

            self._tick(f"{D3} 09:35:00")
            self.assertTrue(store.kv_get(self.conn, f"auto_exec:SH:{D3}"))

        orders = self._orders()
        self.assertEqual(orders[0]["status"], "submitted", orders)
        self.assertIsNone(orders[0]["err"], orders)
        self.assertNotIn(OUTSIDE, [o["symbol"] for o in orders])
        verdicts = self._risk_checks()
        self.assertTrue(any(v["allowed"] for v in verdicts), verdicts)
        self.assertFalse(any(not v["allowed"] for v in verdicts), verdicts)
        places = self._places(broker_calls)
        self.assertEqual(len(places), 1, broker_calls)
        # broker.place 的 sim 参数字段：order_side 1=BUY 2=SELL（TOOL-LIMITS 实测口径）
        self.assertEqual((places[0][1]["order_side"], places[0][1]["qty"]), (2, 1700))
        self.assertEqual(places[0][1]["symbol"], SYMBOL.split(".")[-1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
