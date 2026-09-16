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

  ① 满仓单：链路跑通，**窄门如期拦截**——规则 4（单笔风险 ≤ 权益×1%）拒掉按权重定量
     的满仓单；订单 cancelled + 风控留痕 + 券商零调用；
  ② 小权重单：策略给出 0.8% 目标权重（100 股 / 8,000 元）→ 规则 4 放行 →
     **券商真实收到下单**、订单 submitted；
  ③ 对账差异：券商持仓与本地台账不一致 → critical 差异 → 自动暂停 → 次日自动执行被
     守卫 4（熔断）拦下（**差异只暂停不平仓**，这是规格 §6.3 的既定语义）；
  ④ 关闭态回归：``enabled=false`` 时同一时间线零自动产物；
  ⑤ 假时钟告警：``DSH_FAKE_NOW`` 生效时工作台可见一条 warn（生产防误用）。
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from server import scheduler  # noqa: E402
from trading_core import cli, daemon, store, strategies  # noqa: E402

PREV = "2026-09-15"
D1 = "2026-09-16"
D2 = "2026-09-17"
SYMBOL = "SH.600519"
AUTO_COMMANDS = ("plan-auto", "auto-execute", "reconcile-daily")


class _SmallWeightStrategy:
    """小额目标权重的测试替身：证明「风险预算内的单可被自动提交」。

    真实 ``watchlist_rsi`` 是等权满仓（1/N），在当前定量口径下必然被规则 4 拦截
    （见场景 ①）——本替身不掩盖该事实，只把**提交路径**单独验证出来。
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
        (self.home / "trading-platform.json").write_text(json.dumps({
            "watchlist": [SYMBOL],
            "auto_pipeline": {
                "enabled": enabled,
                "strategies": [{"market": "SH", "strategy": strategy,
                                "watchlist": "SH"}],
                "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
                "reconcile_at": "19:00"}}, ensure_ascii=False), encoding="utf-8")

    def _mode(self, value="sim"):
        (self.home / "trading-account-mode").write_text(value, encoding="utf-8")

    def _calendar(self):
        store.upsert_calendar(self.conn, "SH", [
            {"day": day, "trade_date_type": "WHOLE", "trade_second": 14400}
            for day in (PREV, D1, D2)])

    def _bars(self):
        """30 根：前 28 根缓涨 + 末两根急跌 → 末根 RSI 下穿 25（rsi_signal 是穿越
        事件，平稳趋势不产出 BUY）；最后一根日期 = D1（数据就绪门）。"""
        days = [f"2026-08-{d:02d}" for d in range(1, 29)] + [PREV, D1]
        closes = [100.0 + 0.5 * i for i in range(28)] + [95.0, 80.0]
        store.upsert_bars(self.conn, SYMBOL, "1d", [
            {"t": day, "o": close, "h": close, "l": close, "c": close, "v": 1000.0}
            for day, close in zip(days, closes)], source="test")

    # ---- 假件 ----

    def _broker(self, held=0):
        """sim 假券商：账户/持仓/资金/下单/订单历史。"""
        calls = []

        def call(name, args=None, timeout=30, **kwargs):
            calls.append((name, dict(args or {})))
            if name == "sim_trade_account_list":
                return {"accounts": [{"account_id": "A-1", "market_id": 3,
                                      "account_title": "模拟A股"}]}
            if name == "sim_trade_position_list":
                return {"positions": [{"symbol": SYMBOL, "qty": held}] if held else []}
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
    def _running(self, held=0):
        """整段两日时间线共用一个假券商与假 runner。"""
        call, broker_calls = self._broker(held=held)
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

    # ---- ① 满仓单：链路跑通、窄门如期拦截 ----

    def test_full_chain_blocks_oversized_order(self):
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

            self._tick(f"{D1} 19:00:00")
            self.assertIsNotNone(store.kv_get(self.conn, "reconcile:latest"))
            self.assertIsNotNone(store.kv_get(self.conn, "daily:digest"))

            self._tick(f"{D2} 09:35:00")
            # 指令写出并**同轮消费**
            self.assertEqual(self._pending(), [])
            self.assertEqual(len(self._processed()), 1, self._processed())
            self.assertTrue(store.kv_get(self.conn, f"auto_exec:SH:{D2}"))

        orders = self._orders()
        self.assertEqual(orders[0]["status"], "cancelled", orders)
        self.assertIn("risk:", orders[0]["err"] or "")
        verdicts = self._risk_checks()
        self.assertTrue(any(v["rule"] == 4 and not v["allowed"] for v in verdicts), verdicts)
        self.assertEqual(self._places(broker_calls), [])   # 拦截在触达券商之前
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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
