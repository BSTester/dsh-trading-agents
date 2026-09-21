"""WP6 补遗任务 D 服务面审批回归（规格 §5.2 Python 层 R2′/R3–R6，FastAPI 架构修订版）。

覆盖（全部离线；TestClient + 临时 DSH_HOME + MCP 工具函数直调）：

  * R2′ —— 服务侧业务确认（2026-09-15 main 修订）：HTTP ``confirmation`` 空载荷读待确认项
    （含 ttl_ms、不进缓存）、``confirm-decide`` 批准/拒绝改变 ``confirmation_view``、未知编号/
    非法 decision/多余字段的信封；**MCP 工具面不含 ``confirm_decide``**（模型不能自批实盘单）；
  * R3 —— 服务层 ``switch_mode`` 全矩阵（无口令/错口令/对口令 + order_authorized:false/
    expected_mode 过期/在途租约），以及**MCP 通道分级**：``mode:"live"`` 无论口令一律拒
    （trading/live-switch-web-only），且该分支不触达 handle、不触达 store（记录型替身零调用）；
  * R4 —— 服务层 ``plan_execute``：live 无口令拒、带口令 → queued+nonce、指令文件含
    plan_hash/expected_mode 且无口令字段、action 四映射、白名单外 action 拒；
  * R5 —— 工具面封闭（**两种表面模式各一套精确断言**，期望值全部推导、不写死桥接条数）：
    **direct** ≡ 工作台基础 77（tools/list 逐名 ≡ ``mcp_tools`` 清单，``mcp_tools.TOOL_COUNT``
    仍是这个基础注册表的 77）++ **全部 V3 桥接件**（``/api/v3/*`` 路由经 ``v3_mcp`` 桥接，
    全部 ``v3_`` 前缀、与路由表一一对应、只读标注按路由声明的只读集合推导）；
    **discovery**（默认）≡ 4 件直连保留 ++ ``list_tools``/``call_tool``，且代理可达集合 ≡
    direct 面（发现代理没丢能力，规格 FR-TOOLS-003 / §10 决策 3）；基础面端点
    工具集 ≡ store_access.endpoints() − MCP_EXCLUDED_ENDPOINTS（60 − 5 = 55，排除
    confirm-decide、设置页三端点 openapi_config/openapi_test/openapi_oauth 与 auto_pipeline）、
    输入字段与规格 §3.2（含 20b confirmation）/§3.4 逐项一致、两层都无黑名单名、未知名不触达
    handle；增补 5 个维护工具在真 run 的临时 store 上的全量行为
    （status/runs/cancel_run/cancel_stale/prune_runs）与 ``hours`` 阈值语义
    （锚 ``scripts/workbench_admin.mjs`` 的 ``hoursArg``）；
  * R6 —— HTTP 与 MCP 同源：``app.state.handle`` 同一实例、同一 TTL 缓存、同一 payload 同结果。

MCP 会话客户端（``mcp.client.streamable_http``）只在真进程 loopback 场景才有意义，本文件走
**工具函数直调**（``BoundTool.call``，与 SDK 注册的是同一个函数体）；协议线格式由
``tests/test_wp6_mcp.py``（S1–S4）用真实 uvicorn + 官方客户端覆盖。两者合起来才是完整证据。
"""
import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))

from fastapi.testclient import TestClient  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

from server import app as app_module  # noqa: E402
from server import caches, mcp_discovery, mcp_tools, store_access, v3_mcp  # noqa: E402

# ---------------------------------------------------------------------------
# 仓库级锁定契约：MCP 面 = 工作台基础工具面 + V3 桥接面（2026-09-20 起）
# ---------------------------------------------------------------------------
# 与 tests/test_wp6_mcp.py 各自独立断言同一组不变式（两份分别钉，任一份漂了都会红），
# 但**都不写死桥接条数**：桥接面 = ``/api/v3/*`` 路由表的一次遍历，路由正在演进
# （v3_sdk / v3_headless / v3_alerts 并行落地）。期望值一律推导：
#   * direct    —— ``tools/list`` ≡ 基础 77 件 ++ ``bridge.names``（顺序也锁定）；
#   * discovery —— ``tools/list`` ≡ 直连保留子集 ++ ``list_tools``/``call_tool``；
#   * 只读数按路由声明的只读集合（``v3_mcp.NON_READONLY_PATHS``）推导。
# 注意 ``app.state.mcp_tools`` 只是**基础注册表**的绑定清单（两种模式都是 77 件）；
# 桥接面在 ``app.state.v3_mcp_tools`` / ``app.state.v3_mcp_bridge``。
BASE_TOOLS = 77           # 工作台基础工具面（mcp_tools.TOOLS；逐名 + 逐件 schema 锁定）
DISCOVERY_KEEP = list(mcp_discovery.DIRECT_KEEP)
DISCOVERY_PROXY = list(mcp_discovery.PROXY_NAMES)
DISCOVERY_SURFACE = len(DISCOVERY_KEEP) + len(DISCOVERY_PROXY)
#: 桥接面里**不**标只读的写类工具（逐名钉死，防「悄悄把写类标成只读」；条数不写死——
#: 路由在演进，断言的是「这个集合 ≡ NON_READONLY_PATHS 的映射」）。
V3_NON_READONLY = frozenset({"v3_strategy_run", "v3_oms_sync", "v3_credentials",
                             "v3_sdk_prompt"})
V3_PREFIX = "v3_"
V3_ROUTE_PREFIX = "/api/v3/"


def v3_route_paths(app):
    """``/api/v3/*`` 路由表（去重排序）——桥接面的唯一事实来源。"""
    return sorted({route.path for route in app.routes
                   if getattr(route, "path", "").startswith(V3_ROUTE_PREFIX)})


def declared_readonly_bridge_names(app):
    """按路由声明推导的桥接只读工具名集合（``NON_READONLY_PATHS`` 是唯一口径）。"""
    return {v3_mcp.tool_name(path) for path in v3_route_paths(app)
            if path not in v3_mcp.NON_READONLY_PATHS}

# R4 的动作 → 指令类型（规格 §8.2 五种指令里服务面可达的四种）。
ACTION_COMMANDS = {"execute": "execute_plan", "cancel": "cancel_plan",
                   "kill": "kill", "unkill": "unkill"}

# R5 的独立转录：规格 §3.2（表 1-20 + 20b confirmation）/§3.4（表 21-25）「输入」列，``*`` 进必填。
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
    "factors": (("tickers",), ("window", "refresh", "as_of")),
    "ic": (("tickers",), ("factor", "forward", "window", "refresh")),
    "audit": ((), ("refresh",)),
    "sources": ((), ("no_probe", "refresh")),
    "instrument": (("ticker",), ("refresh",)),
    "quality": (("ticker",), ("refresh",)),
    "plan": ((), ("refresh",)),
    # §3.2 表 20b：只读待确认列表，无业务字段（refresh 与其余无业务字段端点同形）
    "confirmation": ((), ("refresh",)),
    "plan_execute": ((), ("plan_hash", "expected_mode", "confirmation", "action", "refresh")),
    "schedule": ((), ("refresh",)),
    "reconcile": ((), ("refresh",)),
    "pipeline": ((), ("refresh",)),
    # WP7 任务 2：因子快照历史（服务定时收集），字段与 HTTP 面白名单 ["limit"] 同步
    "factors_history": ((), ("limit", "refresh")),
    # WP11 任务 3：情绪快照历史/摘要（按日采集，只读），字段与 HTTP 面白名单
    # ["symbol", "limit"] 同步
    "sentiment_history": ((), ("symbol", "limit", "refresh")),
    # WP14 任务 4：规则候选池（只读；rules-decide 是审批通道，进排除集不进工具面）
    "rules": ((), ("status", "refresh")),
    # WP15 任务 3：值班研究员队列（领取无字段——领取=取队首；回报只有结果三件套）
    "research_tasks_claim": ((), ()),
    "research_tasks_report": (("task_id", "ok"), ("result_ref", "err")),
    # WP12 任务 4：富途数据面（直通 11 + 聚合 2；HTTP-only 三端点不在工具面）
    "stock_screen": (("screen_queries",),
                     ("retrieve_queries", "sort", "sorts", "next_key", "limit",
                      "watchlist_stock_ids", "holding_stock_ids", "user_stock_list_mode",
                      "refresh")),
    "plate_list": (("market", "plate_class"), ("refresh",)),
    "plate_stock": (("plate_code",),
                    ("sort_field", "ascend", "price_type", "leverage_direction",
                     "leverage_multiple", "next_key", "limit", "refresh")),
    "short_daily_volume": (("code",), ("count", "refresh")),
    "short_interest": (("code",), ("count", "refresh")),
    "ipo_list": (("market",), ("request_type", "refresh")),
    "economic_calendar_hot": ((), ("limit", "next_key", "date", "timezone", "refresh")),
    "economic_calendar_search": (("keyword", "search_type"),
                                 ("limit", "next_key", "time_order_type", "refresh")),
    "info_owner_plate": (("code",), ("refresh",)),
    "watchlist_list": (("group_name",), ("refresh",)),
    "watchlist_groups": ((), ("group_type", "refresh")),
    "f10_detail": (("code", "section"), ("params", "refresh")),
    "derivative_detail": (("code", "section"), ("params", "refresh")),
    # WP7 任务 3：受约束交易工具（写三个过闸门链 + Web 确认；读三个 mode 直通 broker），
    # 字段与 app.py 的 TRADE_*/ACCOUNT_QUERY 白名单逐键同形；trade_* 不带 mode（模式只认
    # 模式文件）也不带口令（live 授权=Web 确认卡片）。
    # WP8 任务 6：place 补官方全字段（order_type/time_in_force/session/aux_price/lot_type/
    # remark/order_class/multi_leg_info；price 改为按 order_type 条件必填——必填判定在闸门
    # 字段校验层，工具面因此把它列为可选）；modify 补 aux_price。
    "trade_place": (("symbol", "side", "qty"),
                    ("order_type", "price", "time_in_force", "session", "aux_price",
                     "lot_type", "remark", "order_class", "multi_leg_info",
                     "client_order_id")),
    "trade_modify": (("order_id", "symbol", "side", "qty", "price"),
                     ("aux_price", "client_order_id")),
    "trade_cancel": (("order_id", "symbol"), ("client_order_id",)),
    "account_positions": ((), ("mode",)),
    "account_orders": ((), ("mode",)),
    "account_funds": ((), ("mode",)),
    # WP8：富途实时直通（8 个；字段与 app.py 的 FUTU_FIELDS 白名单逐键同形；
    # option_screen 的 filter 对象必填——上游 field_filter/strategy 必填在 futu_data 校验）
    "rt_quote": (("codes",), ()),
    "rt_order_book": (("code",), ()),
    "capital_flow": (("code",), ()),
    "capital_flow_history": (("code",), ("days",)),
    "capital_distribution": (("code",), ()),
    "option_expiration": (("code",), ()),
    "option_chain": (("code",), ("field_filter",)),
    "option_screen": (("filter",), ()),
    # WP8 任务 2：OpenAPI 行情接入（9 个；字段与 app.py 的 FUTU_FIELDS 白名单逐键同形；
    # 进 TTL 缓存的 5 个带 refresh 旁路——与 equity/series 等缓存工具同房规）
    "market_snapshot": (("codes",), ()),
    "cur_kline": (("code", "num"), ("ktype", "autype", "extended_time")),
    "rt_data": (("code",), ("request_section",)),
    "rt_ticker": (("code",), ("num", "period")),
    "info_basicinfo": (("codes",), ("refresh",)),
    "info_trading_days": (("market", "start", "end"), ("refresh",)),
    "info_search": (("keyword",), ("size", "news_type", "sort_type", "lang", "refresh")),
    "info_market_state": (("codes",), ("is_contain_ba", "is_contain_overnight", "refresh")),
    "quote_history_kline_v2": (("code", "end"), ("start", "ktype", "autype", "num",
                                               "extended_time", "refresh")),
    # WP8 任务 3：OpenAPI 交易只读工具（6 个；需 futu_channel=openapi，mcp 通道下返回
    # trading/openapi-unavailable）。字段与 app.py 的 OPENAPI_TRADE_FIELDS 白名单逐键
    # 同形；全部实时直通（无 refresh 旁路）、mode 受约束（缺省读模式文件）。
    "trade_max_qty": (("code", "order_type"), ("price", "order_id", "mode")),
    "orders_open": (("market",), ("page_flag", "page_size", "mode")),
    "orders_history": (("market",), ("code", "start", "end", "page_flag", "page_size",
                                    "mode")),
    "orders_detail": (("exchange", "order_ids"), ("mode",)),
    "deals_today": (("market",), ("page_flag", "page_size", "mode")),
    "deals_history": (("market",), ("code", "start", "end", "page_flag", "page_size",
                                   "mode")),
    # WP8 任务 6：推送订阅管理面（3 个；**非交易**：只改本地连接订阅意图，不改模式、
    # 不过风控、不产生订单；TTL 0 实时直通、无 refresh 旁路）。字段与 app.py 的
    # PUSH_STATUS_FIELDS/PUSH_SUBSCRIBE_FIELDS 白名单逐键同形；推送未启用时 subscribe/
    # unsubscribe 返回 trading/push-unavailable。
    "push_status": ((), ()),
    "push_subscribe": ((), ("quote", "order_book", "ticker", "kline")),
    "push_unsubscribe": ((), ("quote", "order_book", "ticker", "kline")),
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


class R2ConfirmationTests(Base):
    """R2′：服务侧业务确认（规格 §5.2 R2′ + §5.1 A2/A7）。

    ``store_access.request_confirmation`` 是阻塞调用（Node 的 Promise 等价物），因此「等人作答」
    的用例在后台线程发起、主线程经 HTTP 端点读取与决定。**跨进程边界**：待确认表是**服务进程
    的内存态**，本类只覆盖服务侧自己发起的确认；Harness（Node 进程）发起的确认在独立 Web 上
    看不到——那条链路必须回 Harness 面板作答（见 ``store_access`` 文件头与
    ``docs/architecture.md`` 的端点表说明）。
    """

    TOOL = "mcp__futu__trading_input_order"
    ARGS = {"acc_id": "A1", "market": 100, "symbol": "TSLL", "order_type": 1,
            "order_side": 1, "qty": 4, "price": 9.30}

    def setUp(self):
        super().setUp()
        self.app = self.make_app()
        self.client = self.client(self.app)

    def pending(self):
        return self.post(self.client, "confirmation").json()["value"]["pending"]

    def activity_kinds(self):
        state = json.loads(store_access.store_file(self.home).read_text(encoding="utf-8"))
        return [row["kind"] for row in state["activity"]]

    def start(self, **overrides):
        """后台线程发起一笔服务侧确认，返回 ``(pending_view, outcome_box, thread)``。"""
        kwargs = {"tool": self.TOOL, "mode": "live", "args": self.ARGS, "session_id": "s1"}
        kwargs.update(overrides)
        outcome = {}
        thread = threading.Thread(
            target=lambda: outcome.update(
                store_access.request_confirmation(str(self.home), **kwargs)), daemon=True)
        thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            view = self.pending()
            if view is not None:
                return view, outcome, thread
            time.sleep(0.005)
        self.fail("3 秒内未出现待确认项")

    def test_confirmation_endpoint_is_empty_payload_and_not_cached(self):
        body = self.post(self.client, "confirmation", {}).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["value"], {"pending": None, "ttl_ms": store_access.CONFIRM_TTL_MS})
        self.assertNotIn("cached", body, "confirmation 不进缓存")
        again = self.post(self.client, "confirmation", {}).json()
        self.assertNotIn("cached", again, "再读一次同样没有 cached（未走 caches.cached）")
        bad = self.post(self.client, "confirmation", {"mode": "sim"}).json()
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"]["code"], "trading/invalid-operation")
        self.assertEqual(bad["error"]["message"], "confirmation takes no payload")
        # 两端点都已在白名单里（否则路由层直接 404）
        for endpoint in ("confirmation", "confirm-decide"):
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint,
                              self.post(self.client, "snapshot").json()["value"]["endpoints"])
                self.assertEqual(self.post(self.client, endpoint, {}).status_code, 200)

    def test_confirm_decide_envelope_for_invalid_inputs(self):
        cases = [
            ({}, "Invalid decision; expected approved/rejected"),
            ({"id": "x", "decision": "maybe"}, "Invalid decision; expected approved/rejected"),
            ({"id": "x", "decision": "approved"}, "没有待确认的实盘操作（可能已超时或被处理）"),
            ({"id": "x", "decision": "approved", "price": 1}, "Unexpected confirm-decide field"),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                body = self.post(self.client, "confirm-decide", payload).json()
                self.assertFalse(body["ok"], body)
                self.assertEqual(body["error"]["code"], "trading/invalid-operation")
                self.assertEqual(body["error"]["message"], message)

    def test_http_approval_is_the_only_channel_and_lets_the_waiting_call_through(self):
        view, outcome, thread = self.start()
        self.assertEqual(view["operation"], "下单")
        self.assertEqual(view["tool"], self.TOOL)
        self.assertEqual(view["session_id"], "s1")
        # 只读通道不能批准：HTTP 读 + MCP 读各一次后仍是同一笔待确认、等待方仍未兑现
        self.assertEqual(self.pending()["id"], view["id"])
        tool_value = self.mcp_call(self.app, "confirmation", {})["value"]
        self.assertEqual(tool_value["pending"]["id"], view["id"])
        self.assertEqual(tool_value["ttl_ms"], store_access.CONFIRM_TTL_MS)
        self.assertEqual(outcome, {}, "只读通道不得兑现请求")
        self.assertIsNotNone(self.pending())

        decided = self.post(self.client, "confirm-decide",
                            {"id": view["id"], "decision": "approved"}).json()
        self.assertTrue(decided["ok"], decided)
        self.assertEqual(decided["value"], {"id": view["id"], "decision": "approved",
                                            "tool": self.TOOL, "operation": "下单"})
        thread.join(5)
        self.assertFalse(thread.is_alive(), "批准后等待方必须解除阻塞")
        self.assertEqual(outcome["decision"], "approved")
        self.assertEqual(outcome["reason"], "用户在工作台确认")
        self.assertIsNone(self.pending())
        self.assertEqual(self.activity_kinds(),
                         ["confirmation_requested", "confirmation_approved"])

    def test_reject_unknown_id_and_repeat_decide(self):
        view, outcome, thread = self.start()
        wrong = self.post(self.client, "confirm-decide",
                          {"id": "nope", "decision": "approved"}).json()
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["error"]["message"], "确认编号不匹配；可能已被处理或已超时")
        self.assertIsNotNone(self.pending(), "编号不匹配不得顺手清掉待确认项")

        rejected = self.post(self.client, "confirm-decide",
                             {"id": view["id"], "decision": "rejected"}).json()
        self.assertTrue(rejected["ok"], rejected)
        self.assertEqual(rejected["value"]["decision"], "rejected")
        thread.join(5)
        self.assertEqual(outcome["decision"], "rejected")
        self.assertEqual(outcome["reason"], "用户在工作台拒绝")
        self.assertEqual(self.activity_kinds(),
                         ["confirmation_requested", "confirmation_rejected"])
        again = self.post(self.client, "confirm-decide",
                          {"id": view["id"], "decision": "approved"}).json()
        self.assertFalse(again["ok"])
        self.assertEqual(again["error"]["message"], "没有待确认的实盘操作（可能已超时或被处理）")

    def test_timeout_is_rejected_and_the_view_empties(self):
        outcome = store_access.request_confirmation(
            str(self.home), tool=self.TOOL, mode="live", args=self.ARGS,
            session_id="s1", ttl_ms=40)
        self.assertEqual(outcome["decision"], "rejected")
        self.assertIn("未确认", outcome["reason"])
        self.assertIsNone(self.pending())
        self.assertIn("confirmation_expired", self.activity_kinds())

    def test_confirmation_read_tool_is_present_and_confirm_decide_is_absent(self):
        # app.state.mcp_tools 是**基础注册表**的绑定清单（77 件）；MCP 面总数（含 V3 桥接
        # + 全部桥接件）由 R5ToolSurfaceTests 锁定；两种表面模式各一套。
        names = [tool.name for tool in self.app.state.mcp_tools]
        self.assertEqual(len(names), BASE_TOOLS)
        self.assertIn("confirmation", names)
        self.assertNotIn("confirm_decide", names)
        self.assertNotIn("confirm-decide", names)

    def test_confirmation_view_only_reflects_this_process_table(self):
        """诚实边界：HTTP ``confirmation`` 是服务进程内存表的直读，不做任何跨进程合并。

        服务进程没有 Harness（Node）进程的待确认表，所以页面看到的就是本进程的事实；
        断言「返回内容 ≡ ``store_access.confirmation_view``」把这条限制钉成契约而非口头承诺。
        """
        self.assertIsNone(self.pending())
        view, _outcome, thread = self.start()
        self.assertEqual(self.pending(), store_access.confirmation_view(str(self.home)))
        self.post(self.client, "confirm-decide", {"id": view["id"], "decision": "approved"})
        thread.join(5)


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
    """R5：工具面封闭（两种表面模式各自的精确集合 / 端点对等 − 排除集 /
    输入字段 / 黑名单 / 未知名不触达 handle）。"""

    #: 本类默认在 **discovery**（规格要求的方向）下跑：断言「省的是 schema，不是能力」。
    surface = mcp_discovery.DISCOVERY

    def setUp(self):
        super().setUp()
        self.app = self.make_app(mcp_surface=self.surface)

    def registered(self):
        return asyncio.run(self.app.state.mcp.list_tools())

    def registered_names(self):
        return [tool.name for tool in self.registered()]

    def catalog(self):
        """工具目录（两种模式都可读）：discovery 用代理实例，direct 现算同一份定义。"""
        proxy = self.app.state.mcp_discovery
        if proxy is not None:
            return proxy.catalog
        return mcp_discovery.build_catalog(mcp_tools.TOOLS, self.app.state.v3_mcp_bridge)

    def v3_route_paths(self):
        """``/api/v3/*`` 路由表（去重排序）——桥接面的唯一事实来源。"""
        return v3_route_paths(self.app)

    def test_surface_is_exactly_the_declared_names_for_this_mode(self):
        """各模式 ``tools/list`` 精确相等（不放宽成 >=），期望值全部推导。

        * ``direct``    —— 基础 77 件 ++ ``bridge.names``（顺序也锁定），条数 =
          77 + ``len(routes)``（**路由数从路由表推，不写死**）；
        * ``discovery`` —— 4 件直连保留 ++ ``list_tools``/``call_tool``；
        * 两种模式共同：名字唯一；代理可达集合（discovery）≡ direct 面。
        """
        tools = self.registered()
        names = [tool.name for tool in tools]
        self.assertEqual(len(set(names)), len(names), "工具名必须唯一（两层不得重名）")

        self.assertEqual(mcp_tools.TOOL_COUNT, BASE_TOOLS)
        base_names = [definition.name for definition in mcp_tools.TOOLS]
        self.assertEqual(len(base_names), BASE_TOOLS)

        bridge = self.app.state.v3_mcp_bridge
        bridge_names = list(bridge.names)
        routes = self.v3_route_paths()
        self.assertTrue(all(name.startswith(V3_PREFIX) for name in bridge_names))
        # 桥接面 ⇄ /api/v3/* 路由表一一对应（不重复实现平台侧双射）
        self.assertEqual(sorted(bridge.paths), routes)
        self.assertEqual(sorted(bridge_names),
                         sorted(v3_mcp.tool_name(path) for path in routes))
        self.assertEqual(len(bridge.definitions), len(bridge.paths))

        if self.surface == mcp_discovery.DIRECT:
            self.assertEqual(len(tools), BASE_TOOLS + len(routes))
            self.assertEqual(names, base_names + bridge_names)
            # 只读标注：按路由声明的只读集合推导（不写死 36/38）
            readonly = {tool.name for tool in tools
                        if tool.name.startswith(V3_PREFIX)
                        and getattr(tool.annotations, "read_only_hint", None) is True}
            self.assertEqual(readonly, declared_readonly_bridge_names(self.app))
            self.assertEqual(set(bridge_names) - readonly, set(V3_NON_READONLY))
            self.assertEqual(set(V3_NON_READONLY),
                             {v3_mcp.tool_name(path) for path in v3_mcp.NON_READONLY_PATHS})
        else:
            self.assertEqual(len(tools), DISCOVERY_SURFACE)
            self.assertEqual(names, DISCOVERY_KEEP + DISCOVERY_PROXY)
            self.assertEqual(self.app.state.mcp_surface, mcp_discovery.DISCOVERY)
            # 发现代理没丢能力：代理可达集合 ≡ direct 面（推导式，不写死条数）
            self.assertEqual(set(self.app.state.mcp_discovery.catalog),
                             set(base_names) | set(bridge_names))
            self.assertEqual(len(self.app.state.mcp_discovery.catalog),
                             BASE_TOOLS + len(routes))

    def test_confirm_decide_is_not_a_tool_and_never_reaches_any_channel(self):
        """不变式 1（规格 §5.1 A7）：``confirm-decide`` 绝不进 MCP 工具面。

        唯一能批准实盘操作的通道必须只由独立 Web 的用户点击触发；做成工具就等于让模型
        自己发起、自己批准。这里同时断言工具名（连中划线/下划线两种写法都没有）与端点映射表。
        discovery 模式下再加一条：**代理也无法凭空造出它**（``call_tool`` 报未知工具）。
        """
        names = [definition.name for definition in mcp_tools.TOOLS]
        self.assertNotIn("confirm_decide", names)
        self.assertNotIn("confirm-decide", names)
        self.assertNotIn("confirm_decide", self.registered_names())
        self.assertNotIn("confirm-decide", set(mcp_tools.ENDPOINT_TOOL_ENDPOINTS.values()))
        # WP8 任务 7：设置页三端点（凭据读/写/授权）与 confirm-decide 同类——有意排除集；
        # WP10 任务 2 的 auto_pipeline（自动执行总开关）同理：模型不得自拨
        # WP12 任务 4：数据面三端点有意 HTTP-only（窝轮数据/写用户自选/同步内部复权）
        self.assertEqual(mcp_tools.MCP_EXCLUDED_ENDPOINTS,
                         frozenset({"confirm-decide", "openapi_config", "openapi_test",
                                    "openapi_oauth", "auto_pipeline", "warrant_screen",
                                    "modify_user_security", "info_rehab", "rules-decide",
                                # WP15 任务 3：值班队列的只读列表有意 HTTP-only
                                "research-tasks-list"}))
        # 只读的 confirmation 工具**在**工具面（direct）与代理目录（discovery）里——
        # 读待确认不是批准。代理目录两种模式都有（它来自两个注册表，与表面模式无关）。
        self.assertIn("confirmation", names)
        self.assertIn("confirmation", set(self.catalog()))
        with self.assertRaises(Exception) as caught:
            asyncio.run(self.app.state.mcp.call_tool("confirm_decide", {}))
        self.assertIn("confirm_decide", str(caught.exception))

    def test_unknown_name_never_reaches_the_handle_in_either_mode(self):
        """未知名**不触达 handle**：direct 由 SDK 报「未知工具」，discovery 由代理报
        ``mcp/unknown-tool`` + 「可用 list_tools 检索」——绝不静默返回空。"""
        proxy = self.app.state.mcp_discovery
        if proxy is None:
            with self.assertRaises(Exception) as caught:
                asyncio.run(self.app.state.mcp.call_tool("not_a_tool", {}))
            self.assertIn("not_a_tool", str(caught.exception))
            return
        result, card, target = asyncio.run(proxy.call("not_a_tool", {}))
        self.assertIsNone(card)
        self.assertIsNone(target)
        body = mcp_tools.result_payload(result)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"]["code"], "mcp/unknown-tool")
        self.assertIn("list_tools", body["error"]["message"])

    def test_no_trade_write_endpoint_is_bridged_in_this_mode(self):
        """两种模式下桥里都绝无交易写端点（下单/改单/撤单/切模式/执行计划）。"""
        forbidden = ("trade", "switch-mode", "plan-execute", "confirm-decide", "sim_trade")
        for path in self.app.state.v3_mcp_bridge.paths:
            lowered = path.lower()
            self.assertFalse(any(token in lowered for token in forbidden), path)
        # 六域目录里：交易写工具的卡片绝不标只读（``store_access.order_operation`` 判定，
        # 与交易闸门同源）——「代理能把写类当只读悄悄转发」这条不可能发生。
        catalog = self.catalog()
        for name in ("trade_place", "trade_modify", "trade_cancel"):
            self.assertIn(name, catalog)
            self.assertIsNot(catalog[name].readonly, True,
                             f"{name} 是写类工具，卡片不得标只读")


class R5DirectToolSurfaceTests(R5ToolSurfaceTests):
    """同一套 R5 断言在 ``QUANT_MCP_SURFACE=direct``（全量直暴露，向后兼容口径）下再跑一遍。"""

    surface = mcp_discovery.DIRECT


class R5ToolSurfaceShapeTests(R5ToolSurfaceTests):
    """R5 的**形状**断言（端点对等 / 输入字段 / 黑名单 / 封闭 schema）：与表面模式无关。"""

    def test_endpoint_tool_set_equals_store_endpoints_minus_excluded(self):
        """端点工具集 ≡ 端点清单（22 legacy + WP7 7 + WP8 直通 8 + WP8 行情 9
        + WP8 交易 6 + WP8 推送 3 + WP8 设置 3 + WP10 流程 2 + WP11 情绪 1
        + WP12 数据面 16 + WP14 规则 2 + WP15 值班队列 3）− 有意排除集（§3.2 对等性）。"""
        endpoints = store_access.endpoints()
        self.assertEqual(len(endpoints), 82)
        forwarded = [definition.endpoint for definition in mcp_tools.TOOLS
                     if definition.endpoint]
        self.assertEqual(len(forwarded), 72)
        self.assertEqual(sorted(forwarded),
                         sorted(set(endpoints) - mcp_tools.MCP_EXCLUDED_ENDPOINTS))
        self.assertEqual(sorted(mcp_tools.ENDPOINT_TOOL_ENDPOINTS.values()),
                         sorted(set(endpoints) - mcp_tools.MCP_EXCLUDED_ENDPOINTS))

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
        """面上每件工具都封闭：``additionalProperties:false`` + 字段集/必填集 ≡ 定义。

        两种模式各按自己的面断言（发现代理的 schema 成本是**省下**了，不是没校验）：

        * ``direct``    —— 基础 77 件对 ``mcp_tools.TOOLS``、全部桥接件对 ``bridge.definitions``；
        * ``discovery`` —— 面上的 6 件逐件对「基础 ``TOOLS`` / 桥接 ``definitions`` /
          代理自己的签名（``mcp_discovery.list_tools_signature|call_tool_signature``）」；
        * 字段集漂一格就红（不放宽）。
        """
        tools = self.registered()
        bridge = self.app.state.v3_mcp_bridge
        registry = {definition.name: definition for definition in mcp_tools.TOOLS}
        registry.update((definition.name, definition) for definition in bridge.definitions)
        registry.setdefault(mcp_discovery.LIST_TOOL, mcp_tools.ToolDefinition(
            mcp_discovery.LIST_TOOL, "", None, tuple(mcp_discovery.list_tools_signature())))
        registry.setdefault(mcp_discovery.CALL_TOOL, mcp_tools.ToolDefinition(
            mcp_discovery.CALL_TOOL, "", None, tuple(mcp_discovery.call_tool_signature())))
        for tool in tools:
            definition = registry[tool.name]
            schema = tool.input_schema
            self.assertEqual(set(schema["properties"]), set(definition.fields), tool.name)
            self.assertIs(schema.get("additionalProperties"), False, tool.name)
            self.assertEqual({param.name for param in definition.params if param.required},
                             set(schema.get("required", [])), tool.name)
        self.assertEqual(len(tools), DISCOVERY_SURFACE if self.surface == mcp_discovery.DISCOVERY
                         else BASE_TOOLS + len(self.v3_route_paths()))
        # 动作字段是枚举（在 direct 面直连发布；discovery 面经 ``call_tool`` 转发，
        # 枚举校验在实现内部——这里断言的是定义里的取值域本身没有退化）
        if self.surface == mcp_discovery.DIRECT:
            execute = next(tool for tool in tools if tool.name == "plan_execute")
            self.assertEqual(execute.input_schema["properties"]["action"]["anyOf"][0]["enum"],
                             ["execute", "cancel", "kill", "unkill"])
        else:
            plan = registry["plan_execute"]
            action = next(param for param in plan.params if param.name == "action")
            self.assertEqual(action.kind, "action")  # Literal[...] 取值域由 _TYPES 决定

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
        self.assertEqual(len(schemas), 78)  # 77 工作台基础工具 + 1 外来工具
        self.assertEqual(schemas["foreign_tool"], before,
                         "本模块只应封闭自己注册的 77 个工具")
        self.assertIs(schemas["series"]["additionalProperties"], False)

    def test_unknown_tool_never_reaches_the_handle(self):
        handle, calls = recording_handle()
        server = MCPServer(name=mcp_tools.SERVER_NAME, version=mcp_tools.SERVER_VERSION)
        mcp_tools.register(server, handle, mcp_tools.StoreApi(self.home))
        # 这里只注册**基础面**（V3 桥接是 create_app 的活，见 v3_mcp.register）
        self.assertEqual(len(asyncio.run(server.list_tools())), BASE_TOOLS)
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
        # 基础面 77 件都挂 app.state.handle；桥接面在 app.state.v3_mcp_bridge（不并进这份清单）。
        self.assertEqual(len(self.app.state.mcp_tools), BASE_TOOLS)
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
