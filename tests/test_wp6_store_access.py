"""WP6 补遗 B1 差分测试：store 访问层 Python 移植（平台侧只读快照/模式切换/管理动作/业务确认）。

对照基准是 plugins/workbench/src/store.js（WP7 面板退役后的服务锚版本）与
scripts/workbench_admin.mjs 的实际行为；用例逐条钉死字段名、过滤/倒序、2h 派生阈值、锁协议、错误消息。全部离线，临时 home 目录
显式传给每个 API——store_access 自己**不读** DSH_HOME（那是 config.py 的职责，见
tests/test_wp6_service_locks.py），所以这里不再改环境变量。

业务确认（2026-09-15 main 修订）另有一节：``request_confirmation``/``confirmation_view``/
``decide_confirmation`` 的 TTL 超时、signal 取消、重复决定、跨 home 隔离。确认是**进程内
内存态**（Node 侧原实现同语义，该实现已随 legacy 面板退役），因此这些用例不需要落盘校验之外的夹具。
"""
import json
import stat
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))
from server import store_access as sa  # noqa: E402


def _run(run_id, mode="sim", status="running", hours_ago=0.0, started_at=None, **extra):
    """构造 store.js 形状的 run 行（started_at 默认「现在」）。"""
    row = {
        "id": run_id,
        "ticker": "AAPL",
        "session_id": f"session-{run_id}",
        "mode": mode,
        "status": status,
        "started_at": started_at if started_at is not None else _iso_hours_ago(hours_ago),
    }
    row.update(extra)
    return row


def _iso_hours_ago(hours):
    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _broker_event(entry_id, at, tool, payload, mode="live", is_error=False):
    """构造一条 broker_response 观察（形状与 store 记录一致：业务 JSON 在 content[].text）。"""
    return {
        "id": entry_id, "at": at, "kind": "broker_response", "tool": f"mcp__futu__{tool}",
        "mode": mode, "session_id": "s-1", "is_error": is_error,
        "value": {"content": [{"type": "text",
                               "text": json.dumps(payload, ensure_ascii=False,
                                                  separators=(",", ":"))}]},
    }


class StoreAccessBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # —— 夹具 ——
    def write_state(self, state):
        sa.store_file(self.home).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    def write_mode(self, text):
        sa.mode_file(self.home).write_text(text, encoding="utf-8")

    def read_state(self):
        return json.loads(sa.store_file(self.home).read_text(encoding="utf-8"))

    def state(self, runs=None, reports=None, previews=None, activity=None, broker=None):
        return {"version": 1, "runs": runs or [], "reports": reports or [],
                "previews": previews or [], "activity": activity or [],
                "broker": broker if broker is not None else {}}

    def assert_no_lock_left(self):
        self.assertFalse(sa.lock_file(self.home).exists())


class ReadModeTest(StoreAccessBase):
    def test_missing_file_defaults_to_sim(self):
        self.assertEqual(sa.read_mode(self.home), "sim")

    def test_reads_valid_mode_and_trims(self):
        for text, expected in (("live\n", "live"), (" sim \n", "sim"), ("live", "live")):
            with self.subTest(text=text):
                self.write_mode(text)
                self.assertEqual(sa.read_mode(self.home), expected)

    def test_invalid_content_raises_node_message(self):
        self.write_mode("paper\n")
        with self.assertRaises(sa.WorkbenchError) as caught:
            sa.read_mode(self.home)
        self.assertEqual(str(caught.exception), "Invalid account mode; expected sim/live")

    def test_empty_file_is_invalid_not_sim(self):
        """空文件 ≠ 缺失：Node 的 trim() 得到空串，modeValue 抛错，不静默回退。"""
        self.write_mode("   \n")
        with self.assertRaises(sa.WorkbenchError):
            sa.read_mode(self.home)


class ReadStoreTest(StoreAccessBase):
    def test_missing_file_returns_empty_state(self):
        self.assertEqual(sa.read_store(self.home),
                         {"version": 1, "runs": [], "reports": [], "previews": [],
                          "activity": [], "broker": {}})

    def test_corrupt_json_raises_instead_of_fake_empty(self):
        sa.store_file(self.home).write_text("{oops", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            sa.read_store(self.home)

    def test_invalid_shape_raises_node_message(self):
        cases = {
            "version": self.state() | {"version": 2},
            "runs": self.state() | {"runs": {}},
            "reports": self.state() | {"reports": "x"},
            "previews": self.state() | {"previews": None},
            "activity": self.state() | {"activity": 3},
            "broker-missing": {"version": 1, "runs": [], "reports": [], "previews": [], "activity": []},
            "broker-scalar": self.state() | {"broker": 7},
            "broker-null": self.state() | {"broker": None},
        }
        for name, state in cases.items():
            with self.subTest(case=name):
                self.write_state(state)
                with self.assertRaises(sa.WorkbenchError) as caught:
                    sa.read_store(self.home)
                self.assertEqual(str(caught.exception),
                                 "Invalid workbench state; restore a valid backup")

    def test_valid_state_round_trips_verbatim(self):
        state = self.state(runs=[_run("r1")], broker={"sim": {"at": "2026-09-15T00:00:00.000Z"}})
        self.write_state(state)
        self.assertEqual(sa.read_store(self.home), state)


class WithRunStatusTest(StoreAccessBase):
    def test_threshold_boundary(self):
        now = 1_700_000_000_000
        exact = {"id": "r", "status": "running",
                 "started_at": sa._iso_now(now - sa.ABANDONED_AFTER_MS)}
        just_under = {"id": "r", "status": "running",
                      "started_at": sa._iso_now(now - sa.ABANDONED_AFTER_MS + 1)}
        self.assertEqual(sa.with_run_status(exact, now=now)["status"], "abandoned")
        self.assertEqual(sa.with_run_status(just_under, now=now)["status"], "running")
        # 派生只作用于读出的视图，不改写传入的历史记录
        self.assertEqual(exact["status"], "running")

    def test_non_running_and_unparseable_are_passed_through(self):
        now = 1_700_000_000_000
        completed = {"status": "completed", "started_at": sa._iso_now(now - 10 * 60 * 60 * 1000)}
        broken = {"status": "running", "started_at": "not-a-date"}
        self.assertIs(sa.with_run_status(completed, now=now), completed)
        self.assertIs(sa.with_run_status(broken, now=now), broken)
        self.assertIsNone(sa.with_run_status(None, now=now))


class SnapshotTest(StoreAccessBase):
    def _fixture(self):
        self.write_mode("live\n")
        state = self.state(
            runs=[_run("r_sim", mode="sim"),
                  _run("r_old", mode="live", hours_ago=3.0),
                  _run("r_new", mode="live"),
                  _run("r_done", mode="live", status="completed", hours_ago=1.0)],
            reports=[{"id": "rep_sim", "mode": "sim", "ticker": "MSFT"},
                     {"id": "rep_a", "mode": "live", "ticker": "AAPL"},
                     {"id": "rep_b", "mode": "live", "ticker": "TSLA"}],
            previews=[{"id": "p_sim", "mode": "sim"}, {"id": "p_1", "mode": "live"}, {"id": "p_2", "mode": "live"}],
            activity=[
                # sim 模式的响应不得进入 live 快照的 trade_summary
                _broker_event("a_sim", "2026-09-15T00:00:00.000Z", "sim_trade_history_order_list",
                              {"ret_code": 0, "data": {"orders": [{"order_id": "sim-1",
                                                                   "symbol": "MSFT"}]}},
                              mode="sim"),
                _broker_event("a_1", "2026-09-15T01:00:00.000Z", "sim_trade_history_order_list",
                              {"ret_code": 0, "data": {"orders": [{
                                  "order_id": "6526051", "symbol": "09961",
                                  "stock_name": "携程集团-S", "side": 2, "qty": "200",
                                  "price": "465.2", "avg_fill_price": "465.2", "cum_qty": "200",
                                  "status": 4, "create_time": "1768550082000000",
                                  "update_time": "1768550371000000"}]}}),
                _broker_event("a_2", "2026-09-15T02:00:00.000Z", "sim_trade_input_order",
                              {"ret_code": 0, "data": {"order_id": "7137730"}}),
                _broker_event("a_3", "2026-09-15T03:00:00.000Z", "sim_trade_cash_info",
                              {"ret_code": 0, "data": {"cash": 100}}),
            ],
            broker={"sim": {"at": "2026-09-15T00:00:00.000Z", "tool": "sim"},
                    "live": {"at": "2026-09-15T02:00:00.000Z", "tool": "live"}})
        self.write_state(state)
        return state

    def test_shape_filter_reverse_and_abandoned_derivation(self):
        self._fixture()
        before = sa.store_file(self.home).read_bytes()
        snap = sa.snapshot(self.home)

        self.assertEqual(snap["version"], 1)
        self.assertEqual(snap["mode"], "live")
        self.assertTrue(snap["generated_at"].endswith("Z"))
        datetime.fromisoformat(snap["generated_at"].replace("Z", "+00:00"))  # 可解析

        # 按模式过滤 + 文件内倒序（runs 再叠加 abandoned 派生）
        self.assertEqual([row["id"] for row in snap["runs"]], ["r_done", "r_new", "r_old"])
        self.assertEqual([row["status"] for row in snap["runs"]], ["completed", "running", "abandoned"])
        self.assertEqual([row["id"] for row in snap["reports"]], ["rep_b", "rep_a"])
        self.assertEqual([row["id"] for row in snap["previews"]], ["p_2", "p_1"])
        self.assertEqual([row["id"] for row in snap["activity"]], ["a_3", "a_2", "a_1"])

        # 字段齐备：与 store.js:210-223 同构（+ endpoints）
        self.assertEqual(set(snap), {"version", "mode", "generated_at", "runs", "reports", "previews",
                                     "activity", "trade_summary", "broker", "in_flight",
                                     "confirmation", "pending_observations", "recording_error",
                                     "notice", "endpoints"})
        self.assertEqual(snap["broker"], {"at": "2026-09-15T02:00:00.000Z", "tool": "live"})
        self.assertEqual(snap["in_flight"], 0)
        # 服务进程无待确认（内存态为空）→ 快照如实为 null，不伪造
        self.assertIsNone(snap["confirmation"])
        self.assertEqual(snap["pending_observations"], 0)
        self.assertIsNone(snap["recording_error"])
        self.assertEqual(snap["notice"], sa.NOTICE)
        self.assertIn("不是券商成交推送", snap["notice"])

        # runs 行字段名与 Node 完全一致（逐字段抄）
        self.assertEqual(set(snap["runs"][1]),
                         {"id", "ticker", "session_id", "mode", "status", "started_at"})

        # B2 接线：trade_summary 由过滤后的 activity 派生（store.js:217 同入参同位置）
        trade_summary = snap["trade_summary"]
        self.assertEqual(set(trade_summary), {"orders", "actions", "queries", "counts", "notice"})
        self.assertEqual(trade_summary["counts"],
                         {"responses": 3, "order_responses": 1, "orders": 1, "actions": 1, "errors": 0})
        self.assertEqual(trade_summary["queries"],
                         {"count": 1, "tools": [{"tool": "sim_trade_cash_info", "count": 1}]})

        order = trade_summary["orders"][0]
        self.assertEqual(order["order_id"], "6526051")
        self.assertEqual(order["symbol"], "09961")
        self.assertEqual(order["name"], "携程集团-S")
        self.assertEqual(order["side"], "卖出")
        self.assertEqual(order["fill"], "全部成交")
        self.assertEqual(order["amount"], 93040)              # 200 × 465.2
        self.assertEqual(order["status_code"], 4)
        self.assertEqual(order["ordered_at"], "2026-01-16T07:54:42.000Z")
        self.assertEqual(order["seen_at"], "2026-09-15T01:00:00.000Z")
        self.assertFalse(order["cancelled"])
        self.assertEqual(order["modified_count"], 0)

        action = trade_summary["actions"][0]
        self.assertEqual((action["action"], action["ok"], action["order_id"]),
                         ("下单", True, "7137730"))
        self.assertIn("不是券商成交推送", trade_summary["notice"])

        # 只读守护：快照不写盘
        self.assertEqual(sa.store_file(self.home).read_bytes(), before)
        self.assert_no_lock_left()

    def test_endpoints_manifest_is_77_and_cached(self):
        endpoints = sa.endpoints()
        # 61 项 = 22 项基础清单（WP7 面板退役后冻结在 sa._BASE_ENDPOINTS，原序=已删
        # endpoints.js 的数组原序）+ WP7 服务自有端点（store_access.WP7_ENDPOINTS；任务 3
        # 起含 factors-history + 6 个交易端点）+ WP8 富途实时直通 8 端点
        # （store_access.FUTU_ENDPOINTS）+ WP8 OpenAPI 行情 9 端点 + WP8 任务 3 的
        # OpenAPI 交易只读 6 端点 + WP8 任务 6 的推送订阅管理 3 端点 + WP8 设置页
        # 3 端点（openapi_config/openapi_test/openapi_oauth）+ WP10 端点 2 个
        # （流程页 pipeline + 自动流水线设置 auto_pipeline，
        # store_access.WP10_ENDPOINTS）+ WP11 端点 1 个（情绪快照 sentiment-history，
        # store_access.WP11_ENDPOINTS）+ WP12 数据面 16 个（store_access.WP12_ENDPOINTS）。
        # 整表钉死见 test_wp6_tables_lock。
        expected = (22 + len(sa.WP7_ENDPOINTS) + len(sa.FUTU_ENDPOINTS)
                    + len(sa.WP8_MARKET_ENDPOINTS) + len(sa.WP8_TRADE_ENDPOINTS)
                    + len(sa.WP8_PUSH_ENDPOINTS) + len(sa.WP8_SETTINGS_ENDPOINTS)
                    + len(sa.WP10_ENDPOINTS) + len(sa.WP11_ENDPOINTS)
                    + len(sa.WP12_ENDPOINTS))
        self.assertEqual(len(endpoints), expected)
        self.assertEqual(expected, 77)
        self.assertEqual(endpoints[0], "snapshot")
        self.assertEqual(endpoints[-55], "factors-history")
        self.assertEqual(endpoints[-54:-48],
                         ["trade_place", "trade_modify", "trade_cancel",
                          "account_positions", "account_orders", "account_funds"])
        self.assertEqual(endpoints[-48:-40], list(sa.FUTU_ENDPOINTS))
        self.assertEqual(endpoints[-40:-31], list(sa.WP8_MARKET_ENDPOINTS))
        self.assertEqual(endpoints[-31:-25], list(sa.WP8_TRADE_ENDPOINTS))
        self.assertEqual(endpoints[-25:-22], list(sa.WP8_PUSH_ENDPOINTS))
        self.assertEqual(endpoints[-22:-19], list(sa.WP8_SETTINGS_ENDPOINTS))
        self.assertEqual(endpoints[-19:-17], list(sa.WP10_ENDPOINTS))
        self.assertEqual(endpoints[-17:-16], list(sa.WP11_ENDPOINTS))
        self.assertEqual(endpoints[-16:], list(sa.WP12_ENDPOINTS))
        # 业务确认两端点必须在白名单里（HTTP 面据此注册路由）
        self.assertEqual(endpoints[-57:-55], ["confirmation", "confirm-decide"])
        self.assertEqual(len(set(endpoints)), expected)
        self.assertEqual(endpoints, sa.endpoints())
        # 缓存返回副本：调用方改动不会污染下一次
        endpoints.append("bogus")
        self.assertEqual(len(sa.endpoints()), expected)

    def test_snapshot_reports_pending_observations_without_merging(self):
        """有意差异 1：Python 只读快照不 flushObservations，pending 计数照抄文件系统。"""
        state = self._fixture_minimal()
        self.write_state(state)
        inbox = sa.observations_dir(self.home)
        inbox.mkdir()
        event = {"id": "obs-1", "at": "2026-09-15T03:00:00.000Z", "kind": "broker_response",
                 "tool": "history_order_list", "mode": "live", "session_id": "s", "is_error": False,
                 "value": {"orders": []}}
        (inbox / "obs-1.json").write_text(json.dumps(event), encoding="utf-8")
        (inbox / "obs-2.json").write_text(json.dumps(event | {"id": "obs-2"}), encoding="utf-8")
        (inbox / "notes.txt").write_text("ignored", encoding="utf-8")

        before_store = sa.store_file(self.home).read_bytes()
        before_mode = sa.mode_file(self.home).read_bytes()
        snap = sa.snapshot(self.home)

        self.assertEqual(snap["pending_observations"], 2)   # 只数 *.json
        self.assertEqual(snap["activity"], [])               # 未合并
        self.assertIsNone(snap["broker"])                    # broker 未更新
        self.assertEqual(sorted(p.name for p in inbox.iterdir()),
                         ["notes.txt", "obs-1.json", "obs-2.json"])  # 暂存文件未被消费
        self.assertEqual(sa.store_file(self.home).read_bytes(), before_store)
        self.assertEqual(sa.mode_file(self.home).read_bytes(), before_mode)
        self.assert_no_lock_left()

    def _fixture_minimal(self):
        self.write_mode("live\n")
        return self.state()

    def test_broker_missing_for_mode_is_null(self):
        self.write_mode("sim\n")
        self.write_state(self.state(broker={"live": {"at": "2026-09-15T00:00:00.000Z"}}))
        self.assertIsNone(sa.snapshot(self.home)["broker"])

    def test_empty_home_snapshot_is_valid(self):
        snap = sa.snapshot(self.home)
        self.assertEqual(snap["mode"], "sim")
        self.assertEqual(snap["runs"], [])
        self.assertEqual(snap["activity"], [])
        self.assertEqual(len(snap["endpoints"]),
                         22 + len(sa.WP7_ENDPOINTS) + len(sa.FUTU_ENDPOINTS)
                         + len(sa.WP8_MARKET_ENDPOINTS) + len(sa.WP8_TRADE_ENDPOINTS)
                         + len(sa.WP8_PUSH_ENDPOINTS) + len(sa.WP8_SETTINGS_ENDPOINTS)
                         + len(sa.WP10_ENDPOINTS) + len(sa.WP11_ENDPOINTS)
                         + len(sa.WP12_ENDPOINTS))
        self.assertIsNone(snap["confirmation"])
        self.assertFalse(sa.store_file(self.home).exists())  # 只读：连空 store 都不落盘

    def test_missing_home_snapshot_is_valid(self):
        """home 目录整个不存在也必须只是「空库」，而不是异常（Node 的 ENOENT 分支）。"""
        missing = self.home / "not-created-yet"
        snap = sa.snapshot(missing)
        self.assertEqual(snap["mode"], "sim")
        self.assertIsNone(snap["broker"])
        self.assertEqual(snap["pending_observations"], 0)
        self.assertFalse(missing.exists())


class SwitchModeTest(StoreAccessBase):
    def test_invalid_modes_rejected_before_anything_else(self):
        for kwargs in ({"mode": "paper", "expected_mode": "sim"},
                       {"mode": None, "expected_mode": "sim"},
                       {"mode": "sim", "expected_mode": "paper"},
                       {"mode": "sim", "expected_mode": None},
                       {"mode": "sim"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(sa.WorkbenchError) as caught:
                    sa.switch_mode(self.home, **kwargs)
                self.assertEqual(str(caught.exception),
                                 "Invalid account mode; expected sim/live")
        self.assertFalse(sa.mode_file(self.home).exists())

    def test_stale_expected_mode_rejected(self):
        self.write_mode("live\n")
        with self.assertRaises(sa.WorkbenchError) as caught:
            sa.switch_mode(self.home, mode="sim", expected_mode="sim")
        self.assertEqual(str(caught.exception), "Account mode changed; refresh before switching")
        self.assertEqual(sa.read_mode(self.home), "live")

    def test_active_lease_blocks_switch(self):
        """租约文件名以 store.js:243 `trading-call-<uuid>.active` 为准。"""
        lease = self.home / f"trading-call-{uuid.uuid4()}.active"
        lease.write_text(json.dumps({"pid": 1, "mode": "sim", "at": sa._iso_now()}), encoding="utf-8")
        with self.assertRaises(sa.WorkbenchError) as caught:
            sa.switch_mode(self.home, mode="sim", expected_mode="sim")
        self.assertEqual(str(caught.exception), "有账户调用正在进行，请结束后切换")
        # 校验顺序：租约检查先于口令检查（live 无口令也报租约）
        with self.assertRaises(sa.WorkbenchError) as caught:
            sa.switch_mode(self.home, mode="live", expected_mode="sim")
        self.assertEqual(str(caught.exception), "有账户调用正在进行，请结束后切换")
        # 租约被释放后（文件删除）即可切换
        lease.unlink()
        self.assertEqual(sa.switch_mode(self.home, mode="sim", expected_mode="sim")["mode"], "sim")

    def test_unrelated_files_are_not_leases(self):
        for name in (f"trading-call-{uuid.uuid4()}.lease",
                     f"{uuid.uuid4()}.active",
                     "trading-call-x.active.bak"):
            (self.home / name).write_text("{}", encoding="utf-8")
        self.assertFalse(sa._active_leases(self.home))
        self.assertEqual(sa.switch_mode(self.home, mode="sim", expected_mode="sim")["mode"], "sim")

    def test_live_requires_exact_confirmation_then_succeeds(self):
        for wrong in (None, "", "确认执行", "确认实盘 ", "实盘"):
            with self.subTest(confirmation=wrong):
                with self.assertRaises(sa.WorkbenchError) as caught:
                    sa.switch_mode(self.home, mode="live", expected_mode="sim", confirmation=wrong)
                self.assertEqual(str(caught.exception),
                                 "请输入「确认实盘」；切换模式不等于授权下单")
        self.assertEqual(sa.read_mode(self.home), "sim")

        result = sa.switch_mode(self.home, mode="live", expected_mode="sim", confirmation="确认实盘")
        self.assertEqual(result, {"mode": "live", "previous_mode": "sim", "order_authorized": False})
        self.assertEqual(sa.read_mode(self.home), "live")
        self.assertEqual(sa.mode_file(self.home).read_text(encoding="utf-8"), "live\n")
        self.assertEqual(stat.S_IMODE(sa.mode_file(self.home).stat().st_mode), 0o600)
        # 落盘的 store 也是 0600（atomicWrite）
        self.assertEqual(stat.S_IMODE(sa.store_file(self.home).stat().st_mode), 0o600)
        state = self.read_state()
        self.assertEqual([row["kind"] for row in state["activity"]], ["mode_changed"])
        self.assertEqual(state["activity"][0]["mode"], "live")
        self.assertEqual(state["activity"][0]["previous_mode"], "sim")
        self.assertTrue(state["activity"][0]["id"])
        self.assertTrue(state["activity"][0]["at"].endswith("Z"))
        self.assert_no_lock_left()

    def test_sim_to_sim_needs_no_confirmation_and_records_no_event(self):
        self.write_state(self.state(activity=[{"id": "a", "mode": "sim"}]))
        result = sa.switch_mode(self.home, mode="sim", expected_mode="sim")
        self.assertEqual(result, {"mode": "sim", "previous_mode": "sim", "order_authorized": False})
        self.assertEqual(sa.mode_file(self.home).read_text(encoding="utf-8"), "sim\n")
        self.assertEqual([row["id"] for row in self.read_state()["activity"]], ["a"])  # 未变模式不记事件

    def test_live_to_sim_needs_no_confirmation(self):
        self.write_mode("live\n")
        result = sa.switch_mode(self.home, mode="sim", expected_mode="live")
        self.assertEqual(result, {"mode": "sim", "previous_mode": "live", "order_authorized": False})
        self.assertEqual(sa.read_mode(self.home), "sim")
        self.assertEqual(self.read_state()["activity"][0]["kind"], "mode_changed")

    def test_live_to_live_needs_no_confirmation(self):
        self.write_mode("live\n")
        result = sa.switch_mode(self.home, mode="live", expected_mode="live")
        self.assertEqual(result, {"mode": "live", "previous_mode": "live", "order_authorized": False})
        self.assertEqual(self.read_state()["activity"], [])
        self.assert_no_lock_left()

    def test_corrupt_store_blocks_switch_because_update_reads_first(self):
        sa.store_file(self.home).write_text("{oops", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            sa.switch_mode(self.home, mode="live", expected_mode="sim", confirmation="确认实盘")
        self.assertFalse(sa.mode_file(self.home).exists())
        self.assert_no_lock_left()

    def test_written_store_is_compact_utf8_json(self):
        """Node JSON.stringify 形状：紧凑分隔符 + 不转义非 ASCII，便于双实现互读。"""
        self.write_state(self.state())
        sa.switch_mode(self.home, mode="live", expected_mode="sim", confirmation="确认实盘")
        text = sa.store_file(self.home).read_text(encoding="utf-8")
        self.assertNotIn('": ', text)
        self.assertNotIn(", ", text)
        self.assertEqual(json.loads(text)["version"], 1)

    def test_update_prunes_each_table_to_limit(self):
        activity = [{"id": f"a{i}", "mode": "sim", "at": "2026-09-15T00:00:00.000Z"} for i in range(105)]
        self.write_state(self.state(activity=activity))
        sa.switch_mode(self.home, mode="sim", expected_mode="sim")
        stored = self.read_state()["activity"]
        self.assertEqual(len(stored), sa.LIMIT)
        self.assertEqual(stored[0]["id"], "a5")     # 保留最后 100 条
        self.assertEqual(stored[-1]["id"], "a104")


class AdminStatusRunsTest(StoreAccessBase):
    def test_status_counts(self):
        self.write_state(self.state(runs=[_run("r1"), _run("r2")],
                                    reports=[{"id": "rep"}],
                                    previews=[{"id": "p1"}, {"id": "p2"}, {"id": "p3"}],
                                    activity=[{"id": "a1"}, {"id": "a2"}, {"id": "a3"}, {"id": "a4"}]))
        status = sa.admin_status(self.home)
        self.assertEqual(status["file"], str(sa.store_file(self.home)))
        self.assertTrue(status["file"].endswith("trading-workbench.json"))
        self.assertEqual((status["runs"], status["reports"], status["previews"], status["activity"]),
                         (2, 1, 3, 4))

    def test_status_on_empty_home_is_zeroed(self):
        status = sa.admin_status(self.home)
        self.assertEqual((status["runs"], status["reports"], status["previews"], status["activity"]),
                         (0, 0, 0, 0))

    def test_runs_age_and_unknown_age(self):
        self.write_state(self.state(runs=[
            _run("r_old", hours_ago=3.0),
            _run("r_bad", started_at="not-a-date"),
        ]))
        rows = sa.admin_runs(self.home)
        self.assertEqual(rows[0]["id"], "r_old")
        self.assertEqual(rows[0]["status"], "running")   # 管理视图不派生 abandoned（Node 一致）
        self.assertEqual(rows[0]["mode"], "sim")
        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertAlmostEqual(rows[0]["age_minutes"], 180.0, delta=2.0)
        self.assertIsNone(rows[1]["age_minutes"])         # 解析失败 -> None（脚本「年龄未知」）
        self.assertEqual(set(rows[0]), {"id", "status", "ticker", "mode", "age_minutes"})


class AdminCancelStaleTest(StoreAccessBase):
    def test_cancel_run_marks_and_keeps_record(self):
        self.write_state(self.state(runs=[_run("r1"), _run("r2", status="completed", hours_ago=5.0)]))
        settled = sa.admin_cancel_run(self.home, "r1")
        self.assertEqual(settled["status"], "cancelled")
        self.assertTrue(settled["settled_at"].endswith("Z"))
        self.assertEqual(settled["id"], "r1")

        state = self.read_state()
        self.assertEqual(len(state["runs"]), 2)           # 保留记录，不删除
        self.assertEqual(state["runs"][0]["status"], "cancelled")
        self.assertEqual(state["runs"][1]["status"], "completed")
        events = [row for row in state["activity"] if row["kind"] == "research_cancelled"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["run_id"], "r1")
        self.assertEqual(events[0]["mode"], "sim")
        self.assertEqual(events[0]["ticker"], "AAPL")
        self.assertEqual(events[0]["session_id"], "session-r1")
        self.assert_no_lock_left()

    def test_cancel_run_unknown_and_already_settled(self):
        self.write_state(self.state(runs=[_run("r1", status="cancelled")]))
        with self.assertRaises(sa.WorkbenchError) as caught:
            sa.admin_cancel_run(self.home, "nope")
        self.assertEqual(str(caught.exception), "Unknown run: nope")
        with self.assertRaises(sa.WorkbenchError) as caught:
            sa.admin_cancel_run(self.home, "r1")
        self.assertEqual(str(caught.exception), "Run is already settled: cancelled")

    def test_cancel_stale_only_touches_timed_out_running(self):
        self.write_state(self.state(runs=[
            _run("r_stale", hours_ago=3.0),
            _run("r_fresh"),
            _run("r_done", status="completed", hours_ago=3.0),
            _run("r_bad", started_at="not-a-date"),
        ]))
        self.assertEqual(sa.admin_cancel_stale(self.home), ["r_stale"])
        by_id = {row["id"]: row["status"] for row in self.read_state()["runs"]}
        self.assertEqual(by_id, {"r_stale": "cancelled", "r_fresh": "running",
                                 "r_done": "completed", "r_bad": "running"})
        self.assertEqual(len([r for r in self.read_state()["activity"]
                              if r["kind"] == "research_cancelled"]), 1)
        self.assert_no_lock_left()

    def test_cancel_stale_nothing_to_do(self):
        self.write_state(self.state(runs=[_run("r_fresh")]))
        self.assertEqual(sa.admin_cancel_stale(self.home), [])

    def test_cancel_stale_hours_override_matches_admin_script(self):
        self.write_state(self.state(runs=[_run("r_90m", hours_ago=1.5)]))
        self.assertEqual(sa.admin_cancel_stale(self.home, hours=1), ["r_90m"])
        # 非法/≤0 的 hours 退回默认 2h（workbench_admin.mjs hoursArg）
        for hours in (0, -1, None, float("nan"), float("inf")):
            with self.subTest(hours=hours):
                self.write_state(self.state(runs=[_run("r_90m", hours_ago=1.5)]))
                self.assertEqual(sa.admin_cancel_stale(self.home, hours=hours), [])


class AdminPruneRunsTest(StoreAccessBase):
    def test_prunes_only_timed_out_orphan_runs(self):
        self.write_state(self.state(
            runs=[_run("r_orphan", hours_ago=3.0),
                  _run("r_reported_id", hours_ago=3.0),
                  _run("r_reported_runid", hours_ago=3.0),
                  _run("r_fresh"),
                  _run("r_done", status="completed", hours_ago=3.0),
                  _run("r_bad", started_at="not-a-date")],
            reports=[{"id": "r_reported_id", "mode": "sim"},
                     {"id": "rep_other", "run_id": "r_reported_runid", "mode": "sim"}]))
        self.assertEqual(sa.admin_prune_runs(self.home), ["r_orphan"])
        remaining = [row["id"] for row in self.read_state()["runs"]]
        self.assertEqual(remaining, ["r_reported_id", "r_reported_runid", "r_fresh", "r_done", "r_bad"])
        self.assert_no_lock_left()

    def test_prune_hours_override_and_default(self):
        self.write_state(self.state(runs=[_run("r_90m", hours_ago=1.5)]))
        self.assertEqual(sa.admin_prune_runs(self.home, hours=1), ["r_90m"])
        for hours in (0, None, "abc"):
            with self.subTest(hours=hours):
                self.write_state(self.state(runs=[_run("r_90m", hours_ago=1.5)]))
                self.assertEqual(sa.admin_prune_runs(self.home, hours=hours), [])
                self.assertEqual(len(self.read_state()["runs"]), 1)


class ConfirmationTest(StoreAccessBase):
    """业务确认三方法（服务自有实现；TTL 超时 / signal 取消 / 重复决定 / 隔离）。

    Node 侧同名实现已随 legacy 面板退役（WP7），本类钉死的是服务侧行为。

    ``request_confirmation`` 是**阻塞**调用（Node 的 Promise 等价物），因此「等人作答」的
    用例在后台线程里发起，主线程用 ``confirmation_view`` 看到待确认项后再决定。超时与取消
    无需并发，直接在主线程调用（短 TTL / 预先 set 的 Event）。
    """

    TOOL = "mcp__futu__trading_input_order"
    ARGS = {"acc_id": "281756480774050900", "market": 100, "symbol": "TSLL",
            "order_type": 1, "order_side": 1, "qty": 4, "price": 9.30}

    # —— 夹具 ——
    def start(self, home=None, **overrides):
        """后台线程发起一笔确认，返回 ``(pending_view, outcome_box, thread)``。"""
        home = self.home if home is None else home
        kwargs = {"tool": self.TOOL, "mode": "live", "args": self.ARGS, "session_id": "s1"}
        kwargs.update(overrides)
        outcome = {}
        thread = threading.Thread(
            target=lambda: outcome.update(sa.request_confirmation(home, **kwargs)), daemon=True)
        thread.start()
        pending = self.wait_for_pending(home)
        return pending, outcome, thread

    def wait_for_pending(self, home=None, timeout=3.0):
        home = self.home if home is None else home
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = sa.confirmation_view(home)
            if view is not None:
                return view
            time.sleep(0.005)
        self.fail("3 秒内未出现待确认项")

    def decide(self, home=None, decision="approved", confirmation_id=None):
        home = self.home if home is None else home
        if confirmation_id is None:
            view = sa.confirmation_view(home)
            # 没有待确认时给一个占位编号：错误语义由 decide_confirmation 自己判（先判有无）
            confirmation_id = view["id"] if view else "no-pending"
        return sa.decide_confirmation(home, confirmation_id=confirmation_id, decision=decision)

    def activity(self, home=None):
        home = self.home if home is None else home
        return json.loads(sa.store_file(home).read_text(encoding="utf-8"))["activity"]

    # —— 常量与纯函数 ——
    def test_confirm_ttl_and_operations_are_frozen(self):
        """确认常量现为服务自有（Node 侧同名实现已随 legacy 面板退役），值钉死。"""
        self.assertEqual(sa.CONFIRM_TTL_MS, 120_000)
        self.assertEqual(sa.CONFIRM_OPERATIONS, {"input": "下单", "modify": "改单",
                                                 "cancel": "撤单"})
        self.assertEqual(sa.order_operation("mcp__futu__trading_input_order"), "input")
        self.assertEqual(sa.order_operation("mcp__futu__trading_modify_order"), "modify")
        self.assertEqual(sa.order_operation("mcp__futu__trading_cancel_order"), "cancel")
        self.assertIsNone(sa.order_operation("mcp__futu__trading_something_else"))
        self.assertIsNone(sa.order_operation(None))

    def test_view_has_no_pending_when_table_is_empty(self):
        self.assertIsNone(sa.confirmation_view(self.home))

    def test_summary_renders_chinese_order_fields(self):
        """中文可核对字段 + 未知枚举原样显示 + raw 原样保留（与退役前 Node 实现同语义）。"""
        summary = sa.describe_order_args(self.TOOL, self.ARGS)
        self.assertEqual(summary["tool"], self.TOOL)
        self.assertEqual(summary["raw"], self.ARGS)
        fields = {field["label"]: field["value"] for field in summary["fields"]}
        for label in ("账户", "市场", "标的", "方向", "数量", "价格"):
            self.assertIn(label, fields)
        self.assertEqual(fields["市场"], "100（美股）")
        self.assertEqual(fields["方向"], "买入（order_side=1）")
        self.assertEqual(fields["数量"], "4")
        self.assertEqual(fields["账户"], "281756480774050900")

        unknown = sa.describe_order_args(self.TOOL, {"market": 77, "order_side": 9, "qty": 0})
        unknown_fields = {field["label"]: field["value"] for field in unknown["fields"]}
        self.assertRegex(unknown_fields["市场"], r"未识别.*请核对")
        self.assertRegex(unknown_fields["方向"], r"未识别.*请核对")
        # 数量 0 必须显示（JS 的 push 只跳过 undefined/null/""）
        self.assertEqual(unknown_fields["数量"], "0")
        # 空值字段不出现（买卖方向缺失时连标签都没有）
        empty = sa.describe_order_args(self.TOOL, {"symbol": "", "price": None})
        self.assertEqual([field["label"] for field in empty["fields"]], [])

    # —— 批准 / 拒绝（唯一的作答通道）——
    def test_approve_resolves_the_waiter_and_clears_the_view(self):
        pending, outcome, thread = self.start()
        self.assertEqual(pending["operation"], "下单")
        self.assertEqual(pending["tool"], self.TOOL)
        self.assertEqual(pending["mode"], "live")
        self.assertEqual(pending["session_id"], "s1")
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(set(pending), {"id", "at", "expires_at", "mode", "tool", "operation",
                                        "session_id", "status", "summary"})
        self.assertEqual([field["label"] for field in pending["summary"]["fields"]][:2],
                         ["账户", "市场"])
        self.assertTrue(pending["expires_at"] > pending["at"])

        decided = self.decide(decision="approved")
        self.assertEqual(decided, {"id": pending["id"], "decision": "approved",
                                   "tool": self.TOOL, "operation": "下单"})
        thread.join(5)
        self.assertFalse(thread.is_alive(), "批准后等待方必须立即解除阻塞")
        self.assertEqual(outcome["decision"], "approved")
        self.assertEqual(outcome["reason"], "用户在工作台确认")
        self.assertEqual(outcome["id"], pending["id"])
        self.assertIsNone(sa.confirmation_view(self.home))

        kinds = [row["kind"] for row in self.activity()]
        self.assertEqual(kinds, ["confirmation_requested", "confirmation_approved"])
        approved = self.activity()[1]
        self.assertEqual(approved["confirmation_id"], pending["id"])
        self.assertEqual(approved["session_id"], "s1")
        self.assertEqual(approved["summary"][0]["label"], "账户")

    def test_reject_resolves_as_rejected_and_keeps_the_record(self):
        pending, outcome, thread = self.start()
        self.decide(decision="rejected")
        thread.join(5)
        self.assertEqual(outcome["decision"], "rejected")
        self.assertEqual(outcome["reason"], "用户在工作台拒绝")
        self.assertIsNone(sa.confirmation_view(self.home))
        self.assertEqual([row["kind"] for row in self.activity()],
                         ["confirmation_requested", "confirmation_rejected"])

    # —— 失败路径 ——
    def test_timeout_is_rejected_fail_closed(self):
        outcome = sa.request_confirmation(self.home, tool=self.TOOL, mode="live",
                                          args=self.ARGS, session_id="s1", ttl_ms=40)
        self.assertEqual(outcome["decision"], "rejected")
        self.assertIn("未确认", outcome["reason"])
        self.assertIsNone(sa.confirmation_view(self.home))
        stored = self.activity()
        self.assertIn("confirmation_expired", [row["kind"] for row in stored])
        expired = next(row for row in stored if row["kind"] == "confirmation_expired")
        self.assertIn("超过", expired["note"])

    def test_signal_already_set_cancels_immediately(self):
        signal = threading.Event()
        signal.set()
        outcome = sa.request_confirmation(self.home, tool=self.TOOL, mode="live",
                                          args=self.ARGS, session_id="s1", signal=signal)
        self.assertEqual(outcome["decision"], "rejected")
        self.assertEqual(outcome["reason"], "会话已中断")
        self.assertIsNone(sa.confirmation_view(self.home))
        self.assertIn("confirmation_cancelled", [row["kind"] for row in self.activity()])

    def test_signal_cancel_while_waiting(self):
        """等待中被取消：后台线程发起，主线程 set() 之后线程立即返回拒绝。"""
        signal = threading.Event()
        outcome = {}
        thread = threading.Thread(
            target=lambda: outcome.update(sa.request_confirmation(
                self.home, tool=self.TOOL, mode="live", args=self.ARGS, session_id="s1",
                signal=signal)), daemon=True)
        thread.start()
        self.wait_for_pending()
        signal.set()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "取消后等待方必须解除阻塞")
        self.assertEqual(outcome["decision"], "rejected")
        self.assertEqual(outcome["reason"], "会话已中断")
        self.assertIsNone(sa.confirmation_view(self.home))
        self.assertEqual([row["kind"] for row in self.activity()],
                         ["confirmation_requested", "confirmation_cancelled"])

    def test_unknown_id_and_invalid_decision_are_errors(self):
        self.start()
        with self.assertRaises(sa.WorkbenchError) as caught:
            self.decide(decision="maybe")
        self.assertEqual(str(caught.exception), "Invalid decision; expected approved/rejected")
        with self.assertRaises(sa.WorkbenchError) as caught:
            self.decide(confirmation_id="wrong")
        self.assertEqual(str(caught.exception), "确认编号不匹配；可能已被处理或已超时")
        # 编号不匹配不得顺手清掉待确认项：正确编号仍能批准
        self.assertIsNotNone(sa.confirmation_view(self.home))
        self.decide(decision="approved")
        self.assertIsNone(sa.confirmation_view(self.home))

    def test_decide_without_pending_and_repeat_decide(self):
        with self.assertRaises(sa.WorkbenchError) as caught:
            self.decide(decision="approved")
        self.assertEqual(str(caught.exception), "没有待确认的实盘操作（可能已超时或被处理）")

        pending, outcome, thread = self.start()
        self.decide(decision="approved")
        thread.join(5)
        with self.assertRaises(sa.WorkbenchError) as caught:
            self.decide(confirmation_id=pending["id"], decision="approved")
        self.assertEqual(str(caught.exception), "没有待确认的实盘操作（可能已超时或被处理）")
        self.assertEqual(outcome["decision"], "approved")

    def test_only_one_pending_at_a_time(self):
        _, first, thread = self.start()
        second = sa.request_confirmation(self.home, tool="mcp__futu__trading_cancel_order",
                                         mode="live", args={"order_id": "1"}, session_id="s1")
        self.assertEqual(second["decision"], "rejected")
        self.assertIsNone(second["id"])
        self.assertIn("已有一笔待确认", second["reason"])
        # 第一笔仍然有效
        self.decide(decision="approved")
        thread.join(5)
        self.assertEqual(first["decision"], "approved")

    def test_invalid_arguments_are_rejected_before_touching_the_table(self):
        cases = [
            ({"tool": self.TOOL, "mode": "sim"}, "只有实盘写操作需要业务确认"),
            ({"tool": self.TOOL, "mode": "paper"}, "Invalid account mode; expected sim/live"),
            ({"tool": "  ", "mode": "live"}, "Invalid tool name"),
            ({"tool": None, "mode": "live"}, "Invalid tool name"),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(sa.WorkbenchError) as caught:
                    sa.request_confirmation(self.home, ttl_ms=1, **kwargs)
                self.assertEqual(str(caught.exception), message)
        self.assertIsNone(sa.confirmation_view(self.home))

    def test_pending_table_is_per_home(self):
        """内存态按 home 分槽：同一进程里两个 home 的待确认互不可见（跨进程边界的最小类比）。"""
        other = self.home / "other"
        other.mkdir()
        self.start()
        self.assertIsNotNone(sa.confirmation_view(self.home))
        self.assertIsNone(sa.confirmation_view(other))
        with self.assertRaises(sa.WorkbenchError):
            sa.decide_confirmation(other, confirmation_id="x", decision="approved")
        self.decide(decision="approved")


class WriteLockTest(StoreAccessBase):
    def test_writer_lock_held_yields_busy_error(self):
        """Node update() 用 openSync(lock, "wx") 抢占；已存在即 WorkbenchBusyError。"""
        self.write_state(self.state(runs=[_run("r1")]))
        lock = sa.lock_file(self.home)
        lock.write_text("", encoding="utf-8")
        self.assertTrue(issubclass(sa.WorkbenchBusyError, sa.WorkbenchError))
        for call in (lambda: sa.switch_mode(self.home, mode="sim", expected_mode="sim"),
                     lambda: sa.admin_cancel_run(self.home, "r1"),
                     lambda: sa.admin_cancel_stale(self.home),
                     lambda: sa.admin_prune_runs(self.home)):
            with self.subTest(call=call):
                with self.assertRaises(sa.WorkbenchBusyError) as caught:
                    call()
                self.assertEqual(str(caught.exception),
                                 "Workbench is busy; retry after the other writer finishes")
        self.assertTrue(lock.exists())                       # 别人的锁不被抢走
        self.assertEqual(self.read_state()["runs"][0]["status"], "running")
        lock.unlink()
        self.assertEqual(sa.admin_cancel_run(self.home, "r1")["status"], "cancelled")
        self.assert_no_lock_left()

    def test_update_releases_lock_even_when_handler_raises(self):
        self.write_state(self.state(runs=[_run("r1", status="completed")]))
        with self.assertRaises(sa.WorkbenchError):
            sa.admin_cancel_run(self.home, "r1")
        self.assert_no_lock_left()

    def test_update_creates_home_directory(self):
        nested = self.home / "sub" / "dsh"
        self.assertEqual(sa.switch_mode(nested, mode="sim", expected_mode="sim")["mode"], "sim")
        self.assertTrue(sa.store_file(nested).exists())      # update 先 mkdir -p home


if __name__ == "__main__":
    unittest.main()
