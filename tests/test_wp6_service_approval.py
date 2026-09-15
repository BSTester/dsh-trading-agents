"""WP6 补遗任务 D 服务面审批回归（规格 §5.2 Python 层 R3–R6，FastAPI 架构修订版）。

覆盖（全部离线；TestClient + 临时 DSH_HOME + MCP 工具函数直调）：

  * R3 —— 服务层 ``switch_mode`` 全矩阵（无口令/错口令/对口令 + order_authorized:false/
    expected_mode 过期/在途租约），以及**MCP 通道分级**：``mode:"live"`` 无论口令一律拒
    （trading/live-switch-web-only），且该分支不触达 handle、不触达 store（记录型替身零调用）；
  * R4 —— 服务层 ``plan_execute``：live 无口令拒、带口令 → queued+nonce、指令文件含
    plan_hash/expected_mode 且无口令字段、action 四映射、白名单外 action 拒；
  * R5 —— 工具面封闭：tools/list 恰 25、名单 ≡ mcp_tools 清单、端点工具集 ≡ store_access.endpoints()、
    输入字段与规格 §3.2/§3.4 逐项一致、无黑名单名、未知名不触达 handle；增补 5 个维护工具在真
    run 的临时 store 上的全量行为（status/runs/cancel_run/cancel_stale/prune_runs）与 ``hours``
    阈值语义（锚 ``scripts/workbench_admin.mjs`` 的 ``hoursArg``）；
  * R6 —— HTTP 与 MCP 同源：``app.state.handle`` 同一实例、同一 TTL 缓存、同一 payload 同结果。

MCP 会话客户端（``mcp.client.streamable_http``）只在真进程 loopback 场景才有意义，本文件走
**工具函数直调**（``BoundTool.call``，与 SDK 注册的是同一个函数体）；协议线格式由
``tests/test_wp6_mcp.py``（S1–S4）用真实 uvicorn + 官方客户端覆盖。两者合起来才是完整证据。
"""
import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))

from fastapi.testclient import TestClient  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

from server import app as app_module  # noqa: E402
from server import caches, mcp_tools, store_access  # noqa: E402

# R4 的动作 → 指令类型（规格 §8.2 五种指令里服务面可达的四种）。
ACTION_COMMANDS = {"execute": "execute_plan", "cancel": "cancel_plan",
                   "kill": "kill", "unkill": "unkill"}

# R5 的独立转录：规格 §3.2（表 1-20）/§3.4（表 21-25）「输入」列，``*`` 进必填。
# 与 mcp_tools.TOOLS 是两份分别书写的版本，因此能抓到清单本身的漂移。
SPEC_FIELDS = {
    "snapshot": ((), ("refresh",)),
    "switch_mode": (("mode", "expected_mode"), ("confirmation", "refresh")),
    "series": (("ticker",), ("period", "limit", "refresh")),
    "equity": ((), ("mode", "window", "refresh")),
    "positions": ((), ("mode", "window", "refresh")),
    "correlation": (("tickers",), ("window", "refresh")),
    "sensitivity": ((), ("ticker", "strategy", "metric", "fast_grid", "slow_grid",
                         "buy_grid", "sell_grid", "start", "refresh")),
    "risk": ((), ("refresh",)),
    "trades": ((), ("mode", "limit", "refresh")),
    "events": (("ticker",), ("days", "refresh")),
    "factors": (("tickers",), ("window", "refresh")),
    "ic": (("tickers",), ("factor", "forward", "window", "refresh")),
    "audit": ((), ("refresh",)),
    "sources": ((), ("no_probe", "refresh")),
    "instrument": (("ticker",), ("refresh",)),
    "quality": (("ticker",), ("refresh",)),
    "plan": ((), ("refresh",)),
    "plan_execute": ((), ("plan_hash", "expected_mode", "confirmation", "action", "refresh")),
    "schedule": ((), ("refresh",)),
    "reconcile": ((), ("refresh",)),
    "admin_status": ((), ()),
    "admin_runs": ((), ()),
    "admin_cancel_run": (("run_id",), ()),
    "admin_cancel_stale": ((), ("hours",)),
    "admin_prune_runs": ((), ("hours",)),
}


def iso_hours_ago(hours):
    """store.js 形状的 ``started_at``（UTC、毫秒精度、``Z`` 结尾）：``hours`` 小时前。"""
    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def run_row(run_id, hours_ago=0.0, status="running", started_at=None, mode="sim"):
    """一条真 run（字段与 store.js 写入的一致），供维护工具用例落盘。"""
    return {
        "id": run_id, "ticker": "AAPL", "session_id": f"session-{run_id}", "mode": mode,
        "status": status,
        "started_at": started_at if started_at is not None else iso_hours_ago(hours_ago),
    }


class RecordingStore:
    """store 访问层替身：任何方法调用都记录下来（通道分级分支必须零调用）。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            raise AssertionError(f"store.{name} 不应被触达")
        return call


def recording_handle():
    """handle 替身：记录 ``(endpoint, payload)`` 并回成功信封。"""
    calls = []

    def handle(endpoint, payload):
        calls.append((endpoint, dict(payload)))
        return {"ok": True, "value": {"endpoint": endpoint}}

    return handle, calls


def fake_analytics(values):
    """``create_app(analytics=...)`` 的替身：``{endpoint: provider(payload, force)}``。"""
    def provider(name):
        def call(_payload, _force):
            return values.get(name, {"config": {}})
        return call
    return {name: provider(name) for name in app_module.ANALYTICS_ENDPOINTS}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        # 缓存目录隔离：caches 是模块级单例，不换 home 会把用例数据写进真实用户缓存
        # （HTTP 与 MCP 共用它，正是 R6 要断言的那一份）。
        caches.configure(home=str(self.home))

    def make_app(self, **kwargs):
        kwargs.setdefault("home", str(self.home))
        kwargs.setdefault("dist", str(Path(self._tmp.name) / "dist-missing"))
        return app_module.create_app(**kwargs)

    def client(self, app):
        return TestClient(app, raise_server_exceptions=False)

    def post(self, client, endpoint, payload=None):
        return client.post(f"/api/wb/{endpoint}", json={} if payload is None else payload)

    def set_mode(self, mode):
        (self.home / "trading-account-mode").write_text(f"{mode}\n", encoding="utf-8")

    def pending(self):
        folder = self.home / "trading-commands" / "pending"
        return sorted(folder.glob("*.json")) if folder.is_dir() else []

    def tool(self, app, name):
        """按名字取 ``app.state.mcp_tools`` 里的绑定工具（与 HTTP 面同一 handle）。"""
        return next(item for item in app.state.mcp_tools if item.name == name)

    def mcp_call(self, app, name, arguments):
        """工具函数直调：返回 envelope（断言 isError=false 的业务失败语义）。"""
        result = self.tool(app, name).call(arguments)
        body = mcp_tools.result_payload(result)
        self.assertFalse(result.is_error, body)
        return body

    def write_store(self, runs=(), reports=()):
        """把真 run/研报落成 store 文件（格式与 ``store_access.read_store`` 的校验一致）。"""
        store_access.store_file(self.home).write_text(
            json.dumps({"version": 1, "runs": list(runs), "reports": list(reports),
                        "previews": [], "activity": [], "broker": {}}, ensure_ascii=False),
            encoding="utf-8")

    def store_state(self):
        return json.loads(store_access.store_file(self.home).read_text(encoding="utf-8"))

    def run_statuses(self):
        return {row["id"]: row["status"] for row in self.store_state()["runs"]}

    def run_ids(self):
        return [row["id"] for row in self.store_state()["runs"]]


class R3SwitchModeTests(Base):
    """R3：服务层 switch_mode 矩阵 + MCP 通道分级。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.client = self.client(self.app)

    def test_live_without_passphrase_is_rejected(self):
        self.set_mode("sim")
        body = self.post(self.client, "switch-mode",
                         {"mode": "live", "expected_mode": "sim"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "trading/invalid-operation")
        self.assertEqual(body["error"]["message"], "请输入「确认实盘」；切换模式不等于授权下单")
        self.assertEqual(store_access.read_mode(self.home), "sim")

    def test_live_with_wrong_passphrase_is_rejected(self):
        self.set_mode("sim")
        body = self.post(self.client, "switch-mode",
                         {"mode": "live", "expected_mode": "sim", "confirmation": "确认"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(store_access.read_mode(self.home), "sim")

    def test_live_with_passphrase_succeeds_and_never_authorizes_orders(self):
        self.set_mode("sim")
        body = self.post(self.client, "switch-mode",
                         {"mode": "live", "expected_mode": "sim",
                          "confirmation": "确认实盘"}).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"], {"mode": "live", "previous_mode": "sim",
                                         "order_authorized": False})
        self.assertEqual(store_access.read_mode(self.home), "live")

    def test_stale_expected_mode_is_rejected(self):
        self.set_mode("live")
        body = self.post(self.client, "switch-mode",
                         {"mode": "sim", "expected_mode": "sim"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "Account mode changed; refresh before switching")
        self.assertEqual(store_access.read_mode(self.home), "live")

    def test_active_lease_is_rejected(self):
        self.set_mode("sim")
        (self.home / "trading-call-abc.active").write_text("", encoding="utf-8")
        body = self.post(self.client, "switch-mode",
                         {"mode": "sim", "expected_mode": "sim"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "有账户调用正在进行，请结束后切换")

    def test_mcp_channel_refuses_live_regardless_of_passphrase(self):
        """通道分级在工具函数层封死：口令对/错/缺，一律拒；handle 与 store 零调用。"""
        handle, handle_calls = recording_handle()
        store_api = RecordingStore()
        tools = {item.name: item for item in mcp_tools.build_tools(handle, store_api)}
        for confirmation in (None, "确认实盘", "错误口令"):
            arguments = {"mode": "live", "expected_mode": "sim"}
            if confirmation is not None:
                arguments["confirmation"] = confirmation
            body = mcp_tools.result_payload(tools["switch_mode"].call(arguments))
            self.assertFalse(body["ok"], confirmation)
            self.assertEqual(body["error"]["code"], mcp_tools.LIVE_SWITCH_CODE)
            self.assertEqual(body["error"]["message"], mcp_tools.LIVE_SWITCH_MESSAGE)
            self.assertEqual(body["error"]["details"], {})
        self.assertEqual(handle_calls, [], "通道分级必须在触达 handle 之前返回")
        self.assertEqual(store_api.calls, [], "通道分级不得触达 store 访问层")
        self.assertFalse((self.home / "trading-account-mode").exists())

    def test_mcp_channel_forwards_sim_and_shares_the_app_handle(self):
        """只接受切到 sim：sim 才转 handle，且用的就是 app.state.handle 同一个实例。"""
        tool = self.tool(self.app, "switch_mode")
        self.assertIs(tool.handle, self.app.state.handle)
        body = self.mcp_call(self.app, "switch_mode", {"mode": "sim", "expected_mode": "sim"})
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"]["mode"], "sim")
        self.assertIs(body["value"]["order_authorized"], False)
        # HTTP 侧走的是同一份 handle：同一 payload 得到同一形状的结果
        http = self.post(self.client, "switch-mode",
                         {"mode": "sim", "expected_mode": "sim"}).json()
        self.assertEqual(http["value"], body["value"])

    def test_mcp_channel_live_refusal_never_touches_the_app_handle(self):
        """在真实 app 上再证一次：live 分支不落模式文件、模式仍是 sim。"""
        self.set_mode("sim")
        body = self.mcp_call(self.app, "switch_mode",
                             {"mode": "live", "expected_mode": "sim",
                              "confirmation": "确认实盘"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], mcp_tools.LIVE_SWITCH_CODE)
        self.assertEqual(store_access.read_mode(self.home), "sim")
        self.assertEqual(self.pending(), [])


class R4PlanExecuteTests(Base):
    """R4：服务层 plan_execute 矩阵（HTTP 与 MCP 同 handler）。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.client = self.client(self.app)

    def command_bodies(self):
        return [json.loads(path.read_text(encoding="utf-8")) for path in self.pending()]

    def test_live_requires_passphrase_and_writes_nothing(self):
        self.set_mode("live")
        body = self.post(self.client, "plan-execute",
                         {"plan_hash": "h1", "expected_mode": "live"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "实时账户执行需输入口令「确认执行」")
        self.assertEqual(self.pending(), [], "口令不通过时不得写指令文件")

    def test_live_with_passphrase_queues_without_persisting_it(self):
        self.set_mode("live")
        body = self.post(self.client, "plan-execute",
                         {"plan_hash": "h1", "expected_mode": "live",
                          "confirmation": "确认执行"}).json()
        self.assertTrue(body["ok"], body)
        self.assertIs(body["value"]["queued"], True)
        self.assertTrue(body["value"]["nonce"])
        self.assertEqual(body["value"]["action"], "execute")
        files = self.pending()
        self.assertEqual(len(files), 1)
        raw = files[0].read_text(encoding="utf-8")
        saved = json.loads(raw)
        self.assertEqual(saved["type"], "execute_plan")
        self.assertEqual(saved["plan_hash"], "h1")
        self.assertEqual(saved["expected_mode"], "live")
        self.assertNotIn("confirmation", saved)
        self.assertNotIn("确认执行", raw)

    def test_actions_map_to_whitelisted_commands(self):
        for action, expected in (("execute", "execute_plan"), ("cancel", "cancel_plan"),
                                 ("kill", "kill"), ("unkill", "unkill")):
            before = {path.name for path in self.pending()}
            payload = {"action": action}
            if action in ("execute", "cancel"):
                payload.update({"plan_hash": f"h-{action}", "expected_mode": "sim"})
            body = self.post(self.client, "plan-execute", payload).json()
            self.assertTrue(body["ok"], action)
            self.assertEqual(body["value"]["action"], action)
            fresh = [path for path in self.pending() if path.name not in before]
            self.assertEqual(len(fresh), 1, action)
            saved = json.loads(fresh[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["type"], expected, action)
            self.assertEqual(saved["nonce"], body["value"]["nonce"], action)

    def test_action_outside_the_whitelist_is_rejected(self):
        body = self.post(self.client, "plan-execute", {"action": "sell_all"}).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "Unknown plan-execute action: sell_all")
        self.assertEqual(self.pending(), [])

    def test_mcp_tool_forwards_every_whitelisted_action(self):
        for action, expected in ACTION_COMMANDS.items():
            before = {path.name for path in self.pending()}
            payload = {"action": action}
            if action in ("execute", "cancel"):
                payload.update({"plan_hash": f"mcp-{action}", "expected_mode": "sim"})
            body = self.mcp_call(self.app, "plan_execute", payload)
            self.assertTrue(body["ok"], action)
            self.assertEqual(body["value"]["action"], action)
            fresh = [path for path in self.pending() if path.name not in before]
            self.assertEqual(len(fresh), 1, action)
            self.assertEqual(json.loads(fresh[0].read_text(encoding="utf-8"))["type"], expected)

    def test_mcp_tool_rejects_action_outside_the_whitelist(self):
        body = self.mcp_call(self.app, "plan_execute", {"action": "sell_all"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["message"], "Unknown plan-execute action: sell_all")
        self.assertEqual(self.pending(), [])


class R5ToolSurfaceTests(Base):
    """R5：工具面封闭（恰 25 / 端点对等 / 输入字段 / 黑名单 / 未知名不触达 handle）。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def registered(self):
        return asyncio.run(self.app.state.mcp.list_tools())

    def test_exactly_25_tools_with_the_declared_names(self):
        tools = self.registered()
        self.assertEqual(len(tools), 25)
        self.assertEqual(len(tools), mcp_tools.TOOL_COUNT)
        self.assertEqual([tool.name for tool in tools],
                         [definition.name for definition in mcp_tools.TOOLS])

    def test_endpoint_tool_set_equals_store_endpoints(self):
        """端点工具集 ≡ 从 endpoints.js 文本提取的 20 端点（§3.2 的对等性断言）。"""
        endpoints = store_access.endpoints()
        self.assertEqual(len(endpoints), 20)
        forwarded = [definition.endpoint for definition in mcp_tools.TOOLS
                     if definition.endpoint]
        self.assertEqual(len(forwarded), 20)
        self.assertEqual(sorted(forwarded), sorted(endpoints))
        self.assertEqual(sorted(mcp_tools.ENDPOINT_TOOL_ENDPOINTS.values()), sorted(endpoints))

    def test_input_fields_match_the_spec_table(self):
        definitions = {definition.name: definition for definition in mcp_tools.TOOLS}
        self.assertEqual(set(definitions), set(SPEC_FIELDS))
        for name, (required, optional) in SPEC_FIELDS.items():
            definition = definitions[name]
            self.assertEqual({param.name for param in definition.params if param.required},
                             set(required), name)
            self.assertEqual({param.name for param in definition.params if not param.required},
                             set(optional), name)

    def test_published_schemas_are_closed(self):
        definitions = {definition.name: definition for definition in mcp_tools.TOOLS}
        for tool in self.registered():
            definition = definitions[tool.name]
            schema = tool.input_schema
            self.assertEqual(set(schema["properties"]), set(definition.fields), tool.name)
            self.assertIs(schema.get("additionalProperties"), False, tool.name)
            self.assertEqual({param.name for param in definition.params if param.required},
                             set(schema.get("required", [])), tool.name)
        # 动作字段是枚举：白名单外 action 在 schema 层就被拒（handler 白名单是第二道）
        execute = next(tool for tool in self.registered() if tool.name == "plan_execute")
        self.assertEqual(execute.input_schema["properties"]["action"]["anyOf"][0]["enum"],
                         ["execute", "cancel", "kill", "unkill"])

    def test_no_blacklisted_names(self):
        names = [definition.name for definition in mcp_tools.TOOLS]
        for banned in ("exec", "shell", "file_read", "file_write", "read_file", "write_file",
                       "token"):
            self.assertIn(banned, mcp_tools.TOOL_NAME_BLACKLIST)
            self.assertFalse(any(mcp_tools.is_blacklisted(name) for name in names), banned)
        # 分段精确匹配：plan_execute 不因子串 "exec" 误伤，合成名仍被拦死
        self.assertFalse(mcp_tools.is_blacklisted("plan_execute"))
        self.assertTrue(mcp_tools.is_blacklisted("run_shell"))
        self.assertTrue(mcp_tools.is_blacklisted("exec_cmd"))
        self.assertTrue(mcp_tools.is_blacklisted("token"))

    def test_forbid_extra_fields_leaves_foreign_tools_untouched(self):
        """改动面收窄：同进程里**先注册的别的工具**不被本模块的 extra=forbid 顺手改写。"""
        server = MCPServer(name="foreign", version="0")

        def foreign_tool(alpha: str) -> str:
            return alpha

        server.add_tool(foreign_tool, name="foreign_tool", description="同进程的另一个工具")
        before = {tool.name: tool.input_schema
                  for tool in asyncio.run(server.list_tools())}["foreign_tool"]
        self.assertIsNot(before.get("additionalProperties"), False)
        mcp_tools.register(server, recording_handle()[0], mcp_tools.StoreApi(self.home))
        schemas = {tool.name: tool.input_schema for tool in asyncio.run(server.list_tools())}
        self.assertEqual(len(schemas), 26)
        self.assertEqual(schemas["foreign_tool"], before,
                         "本模块只应封闭自己注册的 25 个工具")
        self.assertIs(schemas["series"]["additionalProperties"], False)

    def test_unknown_tool_never_reaches_the_handle(self):
        handle, calls = recording_handle()
        server = MCPServer(name=mcp_tools.SERVER_NAME, version=mcp_tools.SERVER_VERSION)
        mcp_tools.register(server, handle, mcp_tools.StoreApi(self.home))
        self.assertEqual(len(asyncio.run(server.list_tools())), 25)
        with self.assertRaises(Exception) as caught:
            asyncio.run(server.call_tool("not_a_tool", {}))
        self.assertIn("not_a_tool", str(caught.exception))
        self.assertEqual(calls, [])

    def test_tool_exceptions_outside_handler_are_tool_failed(self):
        """handler 之外的程序异常 → isError=true + trading/tool-failed（规格 §3.2）。"""
        def boom(_endpoint, _payload):
            raise RuntimeError("boom")

        tool = mcp_tools.build_tools(boom, RecordingStore())[0]
        result = tool.call({})
        self.assertTrue(result.is_error)
        body = mcp_tools.result_payload(result)
        self.assertEqual(body["error"]["code"], mcp_tools.TOOL_FAILED_CODE)
        self.assertEqual(body["error"]["message"], "boom")

    def test_admin_tool_store_errors_are_business_failures(self):
        """维护工具的 store 抛错按业务失败（isError=false + trading/invalid-operation）。"""
        body = self.mcp_call(self.app, "admin_cancel_run", {"run_id": "nope"})
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], mcp_tools.INVALID_OPERATION_CODE)
        self.assertEqual(body["error"]["message"], "Unknown run: nope")
        status = self.mcp_call(self.app, "admin_status", {})
        self.assertTrue(status["ok"])
        self.assertEqual(status["value"]["runs"], 0)


class R5AdminToolTests(Base):
    """R5 增补：5 个维护工具全量 + ``hours`` 阈值语义（MCP 工具调用与 store_access 直调各覆盖）。

    阈值语义锚 ``scripts/workbench_admin.mjs`` 的 ``hoursArg()``：``Number.isFinite(v) && v > 0``
    才采用 ``v`` 小时，否则退回默认 2h；Python 侧同一份实现是 ``store_access._hours_to_ms``。
    夹具是真 run 的临时 store（数据文件由 ``store_access`` 原样读写，不经任何替身）。
    """

    def setUp(self):
        super().setUp()
        self.app = self.make_app()

    def seed(self, reports=()):
        """超时 / 未超时、running / 已结算、``started_at`` 解析失败（年龄未知）各一条。"""
        self.write_store(runs=(
            run_row("r_45m", hours_ago=0.75),
            run_row("r_90m", hours_ago=1.5),
            run_row("r_3h", hours_ago=3.0),
            run_row("r_done", hours_ago=3.0, status="completed"),
            run_row("r_bad", started_at="not-a-date"),
        ), reports=reports)

    def test_admin_status_and_runs_read_the_real_store(self):
        self.seed(reports=[{"id": "r_3h", "mode": "sim"}])
        status = self.mcp_call(self.app, "admin_status", {})
        self.assertEqual(status["value"], {
            "file": str(store_access.store_file(self.home)),
            "runs": 5, "reports": 1, "previews": 0, "activity": 0,
        })
        rows = {row["id"]: row for row in self.mcp_call(self.app, "admin_runs", {})["value"]}
        self.assertEqual(set(rows), {"r_45m", "r_90m", "r_3h", "r_done", "r_bad"})
        self.assertEqual(rows["r_3h"]["status"], "running")
        self.assertEqual(rows["r_3h"]["ticker"], "AAPL")
        self.assertEqual(rows["r_3h"]["mode"], "sim")
        self.assertAlmostEqual(rows["r_3h"]["age_minutes"], 180, delta=1)
        self.assertIsNone(rows["r_bad"]["age_minutes"], "started_at 解析失败 → 年龄未知")

    def test_admin_cancel_run_marks_cancelled_and_keeps_the_record(self):
        self.seed()
        body = self.mcp_call(self.app, "admin_cancel_run", {"run_id": "r_3h"})
        self.assertEqual(body["value"]["status"], "cancelled")
        self.assertEqual(self.run_statuses()["r_3h"], "cancelled")
        self.assertEqual(len(self.run_ids()), 5, "取消只标记，不删记录")
        for run_id in ("r_3h", "nope"):  # 已结算 / 未知 id 都是业务失败（isError=false）
            with self.subTest(run_id=run_id):
                failed = self.mcp_call(self.app, "admin_cancel_run", {"run_id": run_id})
                self.assertFalse(failed["ok"])
                self.assertEqual(failed["error"]["code"], mcp_tools.INVALID_OPERATION_CODE)

    def test_admin_cancel_stale_only_touches_timed_out_running(self):
        self.seed()
        body = self.mcp_call(self.app, "admin_cancel_stale", {"hours": 1})
        self.assertEqual(body["value"], ["r_90m", "r_3h"])
        self.assertEqual(self.run_statuses(), {"r_45m": "running", "r_90m": "cancelled",
                                               "r_3h": "cancelled", "r_done": "completed",
                                               "r_bad": "running"})

    def test_admin_cancel_stale_accepts_sub_hour_thresholds(self):
        """``hours=0.5`` 就是 0.5h：不能被抬成 1h/2h（``hoursArg`` 只在 ``v > 0`` 时采用）。"""
        self.seed()
        body = self.mcp_call(self.app, "admin_cancel_stale", {"hours": 0.5})
        self.assertEqual(body["value"], ["r_45m", "r_90m", "r_3h"])

    def test_admin_prune_runs_removes_only_timed_out_orphans(self):
        self.seed(reports=[{"id": "r_3h", "mode": "sim"}])
        body = self.mcp_call(self.app, "admin_prune_runs", {"hours": 1})
        self.assertEqual(body["value"], ["r_90m"], "有研报的 r_3h、未超时的 r_45m 必须保留")
        self.assertEqual(self.run_ids(), ["r_45m", "r_3h", "r_done", "r_bad"])

    def test_hours_bounds_fall_back_to_two_hours_via_store_access(self):
        """直调 store_access：``0``/负数/非法 → 默认 2h；``0.5`` → 0.5h，与 MCP 侧同一份实现。"""
        for hours in (0, -1, 0.0, "abc", None, float("nan"), float("inf")):
            with self.subTest(hours=hours):
                self.seed()
                self.assertEqual(store_access.admin_cancel_stale(self.home, hours=hours), ["r_3h"])
        self.seed()
        self.assertEqual(store_access.admin_cancel_stale(self.home, hours=0.5),
                         ["r_45m", "r_90m", "r_3h"])

    def test_prune_hours_and_report_keys_via_store_access(self):
        """直调 store_access：``run_id`` 键的研报也算已发布（挡住孤儿判定）；``0`` 退回 2h。"""
        self.seed(reports=[{"id": "rep_other", "run_id": "r_3h", "mode": "sim"}])
        self.assertEqual(store_access.admin_prune_runs(self.home, hours=1), ["r_90m"])
        self.assertIn("r_3h", self.run_ids())
        self.seed()
        self.assertEqual(store_access.admin_prune_runs(self.home, hours=0), ["r_3h"])
        self.seed()
        self.assertEqual(store_access.admin_prune_runs(self.home, hours=0.5),
                         ["r_45m", "r_90m", "r_3h"])


class R6SameSourceTests(Base):
    """R6：HTTP 与 MCP 同源（同一 handle 实例 / 同一缓存 / 同一 payload 同结果）。"""

    def setUp(self):
        super().setUp()
        self.app = self.make_app(analytics=fake_analytics({"risk": {"config": {"max": 1}}}))
        self.client = self.client(self.app)

    def test_every_tool_shares_the_app_handle(self):
        self.assertEqual(len(self.app.state.mcp_tools), 25)
        for tool in self.app.state.mcp_tools:
            self.assertIs(tool.handle, self.app.state.handle, tool.name)

    def test_snapshot_stable_fields_match_across_channels(self):
        http = self.post(self.client, "snapshot", {}).json()
        mcp = self.mcp_call(self.app, "snapshot", {})
        self.assertTrue(http["ok"], http)
        self.assertTrue(mcp["ok"], mcp)
        for field in ("mode", "version", "endpoints", "in_flight"):
            self.assertEqual(http["value"][field], mcp["value"][field], field)

    def test_both_channels_share_one_ttl_cache(self):
        """同一 payload 先经 HTTP 取数、再经 MCP 调用 → 命中同一条缓存（cached_at 同值）。"""
        first = self.post(self.client, "risk", {}).json()
        second = self.mcp_call(self.app, "risk", {})
        self.assertTrue(first["ok"], first)
        self.assertTrue(second["ok"], second)
        self.assertIs(first["cached"], False)
        self.assertIs(second["cached"], True, "MCP 必须命中 HTTP 刚写入的同一份缓存")
        self.assertEqual(second["cached_at"], first["cached_at"])
        self.assertEqual(second["value"], first["value"])

    def test_mcp_refresh_bypasses_the_shared_cache(self):
        """``refresh: true`` → 载荷 ``_refresh`` → 强制重取（规格 §3.2 表头）。"""
        self.post(self.client, "risk", {})  # 先写入缓存
        fresh = self.mcp_call(self.app, "risk", {"refresh": True})
        self.assertIs(fresh["cached"], False, "refresh=true 必须绕过 TTL 缓存")

    def test_business_failures_are_identical_across_channels(self):
        self.set_mode("live")
        payload = {"mode": "sim", "expected_mode": "sim"}
        http = self.post(self.client, "switch-mode", payload).json()
        mcp = self.mcp_call(self.app, "switch_mode", payload)
        self.assertFalse(http["ok"])
        self.assertEqual(mcp["error"], http["error"])


if __name__ == "__main__":
    unittest.main()
