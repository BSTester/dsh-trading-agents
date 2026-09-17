"""WP16 实机缺陷回归：模拟盘自动执行的下单腿必须落到真实模拟账户。

背景（2026-09-17 实机验证「模拟盘全自动闭环」时暴露）：``execute.run`` 一直用
``ctx.get("acc_id", "SIM")`` 的**占位符**下单。MCP 通道下上游忽略它（所以历史上没
暴露），切到 ``futu_channel=openapi`` 后 REST 把 acc_id 拼进 URL ——
``POST /api/v1.0/sim-trade/SIM/orders`` —— 券商如实回 ``errcode=-3 invalid parameter``。
也就是说：计划冻结、九守卫、指令轮询、风控 8 规则全部正常，**订单却从未真正到达模拟
账户**（实机证据见 docs/TOOL-LIMITS.md「模拟盘自动执行的下单账户」一节）。

修复口径：下单账户按**市场**从 ``sim_trade_account_list`` 的真实 ``market_id`` 解析
（与 ``broker._sim_positions_and_equity`` 同一张事实表，不新增第二份市场判定）；
解析不到/查询失败时保留既有占位符行为（MCP 通道的历史口径，由券商如实报错）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "core" / "python"))
sys.path.insert(0, str(ROOT / "plugins" / "datasource" / "python"))

from trading_core import execute, oms, store  # noqa: E402
from trading_datasource import channel  # noqa: E402

#: 实机事实（2026-09-17 ``sim_trade_account_list``）：HK 1 / A 股 3 / US 100。
ACCOUNTS = {"accounts": [
    {"account_id": "9393", "market_id": 1, "acc_type": 1},
    {"account_id": "3182575", "market_id": 3, "acc_type": 1},
    {"account_id": "11587526", "market_id": 100, "acc_type": 1},
]}

RISK_CTX = {"mode": "sim", "kill_path": "/nonexistent", "equity": 1_000_000.0,
            "positions_value": {}, "positions_count": 0,
            "day_pnl_pct": 0.0, "is_trading_day": True,
            "config": {"risk_per_trade": 0.01, "max_positions": 5,
                       "max_position_pct": 0.25, "daily_loss_limit_pct": 0.03}}


class _FakeBroker:
    """通道替身：记下 ``(tool, args)``；账户表可注入/可失败。"""

    def __init__(self, accounts=None, fail_accounts=False):
        self.calls = []
        self.accounts = ACCOUNTS if accounts is None else accounts
        self.fail_accounts = fail_accounts

    def __call__(self, name, args, timeout=30):
        self.calls.append((name, dict(args)))
        if name == "sim_trade_account_list":
            if self.fail_accounts:
                raise RuntimeError("通道不可用")
            return self.accounts
        return {"order_id": "7149999"}

    def args_of(self, name):
        return [args for tool, args in self.calls if tool == name]


def _plan_with_orders(tmp, orders):
    """建库 + 冻结计划 + 登记订单（冻结即登记，execute.run 的唯一取单来源）。"""
    conn = store.connect(str(Path(tmp) / "t.sqlite"))
    conn.execute("INSERT INTO plans(plan_id,as_of,mode,strategy_id,target,content_hash,"
                 "status,created_at,origin,market) VALUES('P1','2026-09-17','sim','s',"
                 "'{}','h1','frozen','t','auto','SH')")
    conn.commit()
    for symbol, market in orders:
        oms.register_order(conn, plan_id="P1", symbol=symbol, market=market,
                           side="BUY", qty=100, price=10.0, mode="sim", plan_hash="h1")
    return conn


def _run(conn, broker_call, **ctx_over):
    ctx = dict(RISK_CTX, **ctx_over)
    return execute.run(conn, plan_id="P1", plan_hash="h1", ctx=ctx,
                       broker_call=broker_call, price_of=lambda s: 10.0,
                       stop_dist_of=lambda s: 1.0)


class AccountResolutionTest(unittest.TestCase):
    """核心反证：占位符 ``SIM`` 必须被真实账户替换（修复前这里恒为 ``SIM``）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_sh_order_uses_a_share_sim_account(self):
        conn = _plan_with_orders(self.tmp.name, [("SH.600010", "SH")])
        broker = _FakeBroker()
        out = _run(conn, broker)
        self.assertEqual(out["submitted"], 1)
        placed = broker.args_of("sim_trade_input_order")
        self.assertEqual(len(placed), 1)
        self.assertEqual(placed[0]["acc_id"], "3182575",
                         "A 股（SH 链）订单必须下到 market_id=3 的模拟账户")

    def test_each_market_resolves_to_its_own_account(self):
        conn = _plan_with_orders(self.tmp.name, [("SH.600010", "SH"), ("US.NVDA", "US")])
        broker = _FakeBroker()
        _run(conn, broker)
        self.assertEqual([args["acc_id"] for args in broker.args_of("sim_trade_input_order")],
                         ["3182575", "11587526"])

    def test_account_list_is_queried_once_per_market(self):
        conn = _plan_with_orders(self.tmp.name,
                                 [("SH.600010", "SH"), ("SH.600011", "SH"), ("HK.00700", "HK")])
        broker = _FakeBroker()
        _run(conn, broker)
        self.assertEqual(len(broker.args_of("sim_trade_account_list")), 2,
                         "同一市场只解析一次账户表（逐单查询会放大通道往返）")

    def test_explicit_ctx_account_wins(self):
        conn = _plan_with_orders(self.tmp.name, [("SH.600010", "SH")])
        broker = _FakeBroker()
        _run(conn, broker, acc_id="ACC-9")
        self.assertEqual(broker.args_of("sim_trade_input_order")[0]["acc_id"], "ACC-9")
        self.assertEqual(broker.args_of("sim_trade_account_list"), [],
                         "显式指定账户时不得再查账户表")

    def test_unknown_market_keeps_legacy_placeholder(self):
        conn = _plan_with_orders(self.tmp.name, [("JP.7203", "JP")])
        broker = _FakeBroker()
        _run(conn, broker)
        self.assertEqual(broker.args_of("sim_trade_input_order")[0]["acc_id"], "SIM",
                         "解析不到账户时保留既有占位符口径（不猜、不改写为别的账户）")

    def test_account_lookup_failure_keeps_legacy_placeholder(self):
        conn = _plan_with_orders(self.tmp.name, [("SH.600010", "SH")])
        broker = _FakeBroker(fail_accounts=True)
        out = _run(conn, broker)
        # 账户表不可用不改变既有行为：照旧尝试下单（券商如实报错），不静默丢弃订单
        self.assertEqual(broker.args_of("sim_trade_input_order")[0]["acc_id"], "SIM")
        self.assertIn(out["submitted"], (0, 1))


class _RecordingRestClient:
    """按路径应答的 ``OpenApiClient`` 替身：记下 (method, path, query, body)；
    ``/sim-trade/accounts`` 回账户事实（解析账户要用），下单回订单号。"""

    def __init__(self, accounts=None, order_id="7149999"):
        self.accounts = ACCOUNTS["accounts"] if accounts is None else accounts
        self.order_id = order_id
        self.calls = []

    def request(self, method, path, query=None, json_body=None):
        self.calls.append((method, path, query, json_body))
        if path.endswith("/sim-trade/accounts"):
            return {"accounts": self.accounts}
        if method == "POST":
            return {"order_id": self.order_id}
        return {}


class RestUrlTest(unittest.TestCase):
    """端到端形状：解析出的账户必须出现在 REST 路径里（券商侧真实可见的那一跳）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def _openapi_call(self, recorder):
        (self.home / "trading-platform.json").write_text(
            json.dumps({"futu_channel": "openapi"}), encoding="utf-8")
        cred = self.home / "cred.json"
        cred.write_text(json.dumps({"mode": "oauth", "access_token": "t",
                                    "refresh_token": "r"}), encoding="utf-8")
        return channel.sim_call(str(self.home), client=recorder,
                                credential_path=str(cred),
                                mcp_call=lambda *a, **k: ACCOUNTS)

    def test_rest_path_carries_resolved_account_not_placeholder(self):
        recorder = _RecordingRestClient()
        conn = _plan_with_orders(self.tmp.name, [("SH.600010", "SH")])
        call = self._openapi_call(recorder)
        _run(conn, call)
        posts = [(method, path) for method, path, _q, _b in recorder.calls
                 if method == "POST"]
        self.assertEqual(posts, [("POST", "/api/v1.0/sim-trade/3182575/orders")])
        body = [b for m, _p, _q, b in recorder.calls if m == "POST"][0]
        self.assertEqual(body["market"], 3)
        self.assertEqual(body["symbol"], "600010")
        self.assertEqual(body["qty"], "100")

    def test_fix_is_reverse_proof_against_placeholder_url(self):
        """反向证明：修复前出站路径是 ``/sim-trade/SIM/orders``（券商 -3 的直接原因）。"""
        recorder = _RecordingRestClient()
        conn = _plan_with_orders(self.tmp.name, [("SH.600010", "SH")])
        call = self._openapi_call(recorder)
        _run(conn, call, acc_id="SIM")  # 显式传占位符 = 修复前的行为
        posts = [(method, path) for method, path, _q, _b in recorder.calls
                 if method == "POST"]
        self.assertEqual(posts, [("POST", "/api/v1.0/sim-trade/SIM/orders")],
                         "该路径即实机 -3 invalid parameter 的来源；修复后不得再出现")


if __name__ == "__main__":
    unittest.main(verbosity=2)
