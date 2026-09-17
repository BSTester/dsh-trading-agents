"""WP13 任务 3：双通道等价 + sim 全链路回归（规格 §8.3 / 计划任务 3）。

**为什么需要本文件**（任务 1/2 已覆盖单元级语义）：任务 1 的
``test_wp13_sync_channel.py`` 证明「sync 五项」两通道落库同形；任务 2 的
``test_wp13_simtrade.py`` 证明「模拟交易九端点」出站逐字一致与单函数形状。两者都在
**单函数/单端点**层面。本文件补的是**整条链**：同一份种子数据、同一条 WP9 两交易日
时间线，分别在 mcp 与 openapi 通道下跑完「收盘生成计划 → 次日自动执行 → 晚间对账」，
断言：

 ① 可观测结果**逐字段等价**（计划/订单/风控预检行；uuid、时间戳等非确定性字段排除）；
 ② openapi 通道下**全链路走 REST**（账户/持仓/资金/历史/下单五类路径都出现），且 MCP
    替身 **零** ``sim_trade_*`` 调用——本任务暴露并修复的通道分裂的回归钉；
 ③ 凭据缺失时**整链回退 mcp**（不是只回退某一段），结果与 mcp 基线等价；
 ④ REST 失败**不静默换通道**：如实跳过/失败 + 告警，MCP 零调用、零占位行；
 ⑤ 两通道皆不可用 → 零占位行（不写「无差异」、不伪造计划）。

**本任务同时收口三处通道分裂**（①②③④ 就是它们的回归）：``planner.plan_auto`` 与
``reconcile.daily`` 曾硬编码 ``trading_datasource.futu_mcp.call_tool`` 作缺省通道，
于是 ``futu_channel=openapi`` 下「下单执行走 REST、取持仓与对账走 MCP」。现统一经
``core_broker.sim_call(home)``（唯一实现在 ``trading_datasource.channel``）。

**已知遗留（如实登记，不在本任务修）**：sim 调用路径丢弃 ``channel`` 回退标记
（``sim_call`` 出来的 callable 只返回数据、不回传 ``"mcp(fallback)"``）——调用点因此
看不到「本该 REST 却回退」的观测信号，只有 ``fetch``（sync 五项）携带该标记。运维侧
当前靠「凭据是否配置」判断，见 ``docs/HANDOVER.md``。
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
sys.path.insert(0, str(ROOT / "tests"))

from server import scheduler  # noqa: E402
from trading_core import cli, clock, daemon, store  # noqa: E402
from trading_datasource import channel  # noqa: E402

PREV = "2026-09-15"
D1 = "2026-09-16"
D2 = "2026-09-17"
D3 = "2026-09-18"
SYMBOL = "SH.600519"
ACC = "3182575"
MARKET_ID = 3
AUTO_COMMANDS = ("plan-auto", "auto-execute", "reconcile-daily")


class SimBackend:
    """券商后端状态：REST 与 MCP 两个替身**共享同一份事实**（同一账户/持仓/订单）。

    共享是为了让「同一份种子数据喂两个通道」成立——否则等价性断言会退化成「两个假件
    各自返回了各自写死的东西」。
    """

    def __init__(self, held=0):
        self.held = held
        self.order_seq = 0

    def new_order_id(self):
        self.order_seq += 1
        return f"SIM-O{self.order_seq}"


class RestFake:
    """``OpenApiClient`` 替身：按官方路径返回 d 形状；记录每次 (method, path)。"""

    def __init__(self, backend, error=None, fail_paths=()):
        self.backend = backend
        self.calls = []
        self.error = error
        self.fail_paths = tuple(fail_paths)

    def request(self, method, path, query=None, json_body=None):
        self.calls.append((method, path, query, json_body))
        if self.error is not None and any(part in path for part in self.fail_paths):
            raise self.error
        # 顺序敏感：history-orders 也以 "orders" 结尾，必须先判
        if "history-orders" in path:
            return {"orders": [], "pagination": {"has_more": False, "next_key": "-1"}}
        if path.endswith("/accounts"):
            return {"accounts": [{"account_id": ACC, "market_id": MARKET_ID,
                                  "account_title": "模拟A股"}]}
        if path.endswith("/positions"):
            rows = [{"symbol": "600519", "qty": str(self.backend.held)}] \
                if self.backend.held else []
            return {"positions": rows}
        if path.endswith("/cash-info"):
            return {"balance": 1_000_000.0, "total_asset": 1_000_000.0}
        if path.endswith("/orders") and method == "POST":
            return {"order_id": self.backend.new_order_id()}
        if path.endswith("/orders"):
            return {"orders": []}
        raise AssertionError(f"未预期的 REST 路径：{method} {path}")  # pragma: no cover

    def request_meta(self, method, path, query=None, json_body=None):
        return self.request(method, path, query, json_body), None

    # ---- 观测 ----

    def paths(self):
        return [path for _method, path, _query, _body in self.calls]

    def kinds(self):
        """路径归一为五类观测名（断言用；不暴露具体 acc_id/query）。"""
        out = []
        for _method, path, _query, _body in self.calls:
            for key, marker in (("accounts", "/accounts"), ("history", "history-orders"),
                                ("positions", "/positions"), ("cash", "/cash-info"),
                                ("place", "/orders")):
                if marker in path or path.endswith(marker):
                    out.append(key)
                    break
            else:  # pragma: no cover —— 新路径必须在上表登记
                raise AssertionError(f"未登记的 REST 路径：{path}")
        return out


def mcp_fake(backend, error=None, log=None):
    """``futu_mcp.call_tool`` 替身：与 WP9 e2e 同形状（mcp 侧参数口径）。"""
    calls = log if log is not None else []

    def call(name, args=None, timeout=30, **kwargs):
        calls.append((name, dict(args or {})))
        if error is not None:
            raise error
        if name == "sim_trade_account_list":
            return {"accounts": [{"account_id": ACC, "market_id": MARKET_ID,
                                  "account_title": "模拟A股"}]}
        if name == "sim_trade_position_list":
            rows = [{"symbol": "600519", "qty": backend.held}] if backend.held else []
            return {"positions": rows}
        if name == "sim_trade_cash_info":
            return {"balance": 1_000_000.0, "total_asset": 1_000_000.0}
        if name == "sim_trade_input_order":
            return {"order_id": backend.new_order_id()}
        if name == "sim_trade_history_order_list":
            return {"orders": []}
        raise AssertionError(f"意外券商工具 {name}")  # pragma: no cover

    return call, calls


class _ChannelRun:
    """一次「通道 → 两交易日时间线」的完整运行与其可观测量。"""

    def __init__(self):
        self.snapshot = None
        self.rest = None
        self.mcp_calls = None
        self.alerts = []
        #: 调度器如实上报的作业失败（``build_tick`` 在两段都跑完后才抛）——通道失败场景
        #: 里这是**预期观测**（fail-closed 必须可见），不是测试事故。
        self.tick_errors = []


class ChannelE2E(unittest.TestCase):
    """同一时间线在两个通道下各跑一次，比较可观测结果。"""

    def setUp(self):
        self._saved_home = os.environ.get("DSH_HOME")
        self._saved_now = os.environ.get(daemon.FAKE_NOW_ENV)
        self.addCleanup(self._restore_env)
        clock._fake_now_alerted = False
        self.addCleanup(setattr, clock, "_fake_now_alerted", False)

    def _restore_env(self):
        for key, value in (("DSH_HOME", self._saved_home),
                           (daemon.FAKE_NOW_ENV, self._saved_now)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---- 种子 ----

    def _seed_home(self, tmp, channel_name):
        home = Path(tmp)
        (home / "trading-platform.json").write_text(json.dumps({
            "futu_channel": channel_name,
            "watchlist": [SYMBOL],
            "auto_pipeline": {
                "enabled": True,
                "strategies": [{"market": "SH", "strategy": "watchlist_rsi",
                                "watchlist": "watchlist"}],
                "exec_at": {"SH": "09:35", "HK": "09:45", "US": "22:35"},
                "reconcile_at": "19:00"}}, ensure_ascii=False), encoding="utf-8")
        (home / "trading-account-mode").write_text("sim", encoding="utf-8")
        return home

    def _seed_db(self, home):
        conn = store.connect(store.db_path(str(home)))
        store.upsert_calendar(conn, "SH", [
            {"day": day, "trade_date_type": "WHOLE", "trade_second": 14400}
            for day in (PREV, D1, D2, D3)])
        # 28 根缓涨 + 末两根急跌（PREV, D1）→ 末根 RSI 下穿 25（rsi_signal 是穿越事件）
        days = [f"2026-08-{d:02d}" for d in range(1, 29)] + [PREV, D1]
        closes = [100.0 + 0.5 * i for i in range(28)] + [95.0, 80.0]
        store.upsert_bars(conn, SYMBOL, "1d", [
            {"t": day, "o": close, "h": close, "l": close, "c": close, "v": 1000.0}
            for day, close in zip(days, closes)], source="test")
        return conn

    def _runner(self, home):
        """进程内假 runner：真实 CLI 入口 + 真实调度（假时钟经环境变量生效）。"""
        dispatched = []

        def runner(cmd):
            resolved = daemon.resolve_command(cmd, str(home))
            if resolved is None:
                return {"skipped": "关注池为空"}
            name = resolved[0]
            if name not in AUTO_COMMANDS:
                return 0  # 数据作业：本测试以种子数据代替（等价性只关心同一条链）
            dispatched.append(name)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(resolved)
            if code:
                raise AssertionError(f"{name} 退出码 {code}：{out.getvalue()[:400]}")
            return code

        return runner, dispatched

    # ---- 运行 ----

    def _run(self, channel_name, *, credentials=True, rest_error=None, fail_paths=(),
             mcp_error=None):
        """跑完整两日时间线，返回 ``_ChannelRun``（快照 + 两通道观测）。"""
        run = _ChannelRun()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = self._seed_home(tmp.name, channel_name)
        os.environ["DSH_HOME"] = str(home)
        os.environ.pop(daemon.FAKE_NOW_ENV, None)
        conn = store.connect(store.db_path(str(home)))
        self.addCleanup(conn.close)
        conn2 = self._seed_db(home)
        self.addCleanup(conn2.close)

        backend = SimBackend()
        rest = RestFake(backend, error=rest_error, fail_paths=fail_paths)
        call, mcp_calls = mcp_fake(backend, error=mcp_error)
        runner, _dispatched = self._runner(home)
        run.rest = rest
        run.mcp_calls = mcp_calls

        def tick(stamp):
            os.environ[daemon.FAKE_NOW_ENV] = stamp
            # 每个时点独立跑：作业失败（fail-closed）由 build_tick 如实抛出，本测试把它
            # 记为观测继续跑后续时点——通道故障下「后续时点仍然存在且不产生占位」是被测
            # 语义本身，不能因为一次抛出就丢掉后半段时间线。
            try:
                return scheduler.build_tick(str(home), conn=conn2)()
            except RuntimeError as error:
                run.tick_errors.append(str(error))
                return None

        with mock.patch.object(channel, "openapi_ready", return_value=credentials), \
                mock.patch.object(channel, "openapi_client", return_value=rest), \
                mock.patch("trading_datasource.futu_mcp.call_tool", call), \
                mock.patch.object(daemon, "_subprocess_runner", runner):
            tick(f"{D1} 16:20:00")   # 收盘链：plan-auto（取持仓/资金 → 冻结计划）
            tick(f"{D1} 19:00:00")   # GLOBAL 链：reconcile-daily（券商店铺/持仓 → 对账）
            tick(f"{D2} 09:35:00")   # 次日开盘+延迟：auto-execute + 同轮指令轮询

        run.snapshot = self._snapshot(conn2)
        run.alerts = [(row["level"], row["title"]) for row in conn2.execute(
            "SELECT level, title FROM alerts ORDER BY id").fetchall()]
        return run

    # ---- 观测 ----

    def _snapshot(self, conn):
        """可观测结果的稳定投影（排除 uuid/时间戳等非确定性字段）。"""
        return {
            "plans": [tuple(row) for row in conn.execute(
                "SELECT as_of, mode, status, origin, market FROM plans"
                " ORDER BY rowid").fetchall()],
            "orders": [tuple(row) for row in conn.execute(
                "SELECT symbol, side, qty, price, status FROM orders"
                " ORDER BY rowid").fetchall()],
            "checks": [tuple(row) for row in conn.execute(
                "SELECT rule, allowed FROM risk_checks ORDER BY id").fetchall()],
        }

    def _sim_mcp_names(self, run):
        return [name for name, _args in run.mcp_calls if name.startswith("sim_trade_")]

    # ---- ① 等价 + ② REST 全链路 ----

    def test_openapi_snapshot_equals_mcp_snapshot(self):
        """同一时间线两通道跑完 → 计划/订单/预检逐字段等价（通道不改变业务结果）。"""
        base = self._run("mcp")
        rest = self._run("openapi")
        self.assertEqual(rest.snapshot, base.snapshot)
        self.assertTrue(base.snapshot["plans"], "基线必须真的生成计划（否则等价是空真）")
        self.assertTrue(base.snapshot["orders"], "基线必须真的下单")

    def test_openapi_chain_covers_every_sim_leg_through_rest(self):
        """openapi 通道：账户/持仓/资金/历史/下单五类全走 REST，MCP 零 sim 调用。"""
        run = self._run("openapi")
        kinds = set(run.rest.kinds())
        self.assertEqual(kinds, {"accounts", "positions", "cash", "history", "place"},
                         f"五类腿必须全走 REST，实际 {sorted(kinds)}")
        self.assertEqual(self._sim_mcp_names(run), [],
                         "openapi 通道下不允许任何 sim_trade_* 走 MCP（通道分裂回归钉）")

    def test_mcp_channel_never_touches_rest(self):
        """反向：默认 mcp 通道下 REST 替身零调用（回退不是「两边都发」）。"""
        run = self._run("mcp")
        self.assertEqual(run.rest.calls, [])
        self.assertIn("sim_trade_input_order", [name for name, _a in run.mcp_calls])

    # ---- ③ 回退语义 ----

    def test_credentials_absent_falls_back_for_the_whole_chain(self):
        """openapi 配置但凭据缺失 → 整链回退 mcp，结果与 mcp 基线等价。"""
        base = self._run("mcp")
        fallback = self._run("openapi", credentials=False)
        self.assertEqual(fallback.rest.calls, [], "无凭据时不应尝试 REST（避免注定失败的请求）")
        self.assertEqual(fallback.snapshot, base.snapshot)
        self.assertIn("sim_trade_input_order",
                      [name for name, _a in fallback.mcp_calls])

    # ---- ④ REST 失败不静默换通道 ----

    def test_rest_failure_does_not_switch_to_mcp(self):
        """REST 持仓取数失败 → 如实跳过计划 + 告警；MCP 零调用、零占位行。"""
        run = self._run("openapi", rest_error=RuntimeError("rest down"),
                        fail_paths=("/positions",))
        self.assertEqual(self._sim_mcp_names(run), [],
                         "REST 失败后换 MCP 会把限频/权限错误伪装成 MCP 行为——禁止")
        self.assertEqual(run.snapshot["plans"], [], "通道失败不得产出计划（零占位）")
        self.assertEqual(run.snapshot["orders"], [])
        titles = [title for _level, title in run.alerts]
        self.assertIn("券商通道不可用", titles)
        self.assertTrue(run.tick_errors, "对账通道失败必须由调度器如实上报（不是静默跳过）")
        self.assertTrue(any("rest down" in message for message in run.tick_errors),
                        f"失败原因必须原样可见：{run.tick_errors}")

    # ---- ⑤ 两通道皆不可用 ----

    def test_both_channels_unavailable_leaves_no_placeholder(self):
        """REST 与 MCP 都失败 → 零计划/零订单/零预检行（宁缺毋假）。"""
        run = self._run("openapi", rest_error=RuntimeError("rest down"),
                        fail_paths=("/accounts",), mcp_error=RuntimeError("mcp down"))
        self.assertEqual(run.snapshot["plans"], [])
        self.assertEqual(run.snapshot["orders"], [])
        self.assertEqual(run.snapshot["checks"], [])
        titles = [title for _level, title in run.alerts]
        self.assertTrue(any("不可用" in title for title in titles), run.alerts)
        self.assertTrue(run.tick_errors, "两通道皆不可用时作业失败必须可见")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
